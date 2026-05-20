import os
import json
import sqlite3
from datetime import datetime, timedelta

import requests
from flask import Flask, request, jsonify, render_template, Response, stream_with_context

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG = {}
_config_path = os.path.join(BASE_DIR, "config.json")
if os.path.exists(_config_path):
    try:
        with open(_config_path, encoding="utf-8") as f:
            CONFIG = json.load(f)
    except Exception:
        pass

def _cfg(key, env_var, default):
    return os.environ.get(env_var) or CONFIG.get(key, default)

app = Flask(__name__)

PORT = int(_cfg("port", "PORT", 5000))
DB_PATH = _cfg("db_path", "DB_PATH", os.path.join(BASE_DIR, "usage.db"))
KIMI_ESTIMATE_URL = _cfg("kimi_estimate_url", "KIMI_ESTIMATE_URL", "https://api.moonshot.cn/v1/tokenizers/estimate-token-count")

# ── database ──────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model TEXT NOT NULL,
            provider TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            request_time DATETIME DEFAULT (datetime('now', 'localtime')),
            endpoint TEXT
        )
    """)
    conn.commit()
    conn.close()

# ── tokenizer ──────────────────────────────────────────────────

DEEPSEEK_TOKENIZER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tokenizers", "deepseek")
_deepseek_tokenizer = None

def get_deepseek_tokenizer():
    global _deepseek_tokenizer
    if _deepseek_tokenizer is not None:
        return _deepseek_tokenizer if _deepseek_tokenizer else None
    try:
        from transformers import AutoTokenizer
        _deepseek_tokenizer = AutoTokenizer.from_pretrained(DEEPSEEK_TOKENIZER_DIR, trust_remote_code=True)
        return _deepseek_tokenizer
    except Exception:
        _deepseek_tokenizer = False
        return None

def count_tokens(model: str, text: str) -> int:
    if not text:
        return 0
    if model.startswith("deepseek-"):
        tok = get_deepseek_tokenizer()
        if tok:
            return len(tok.encode(text))
        return max(1, len(text) // 4)
    try:
        import tiktoken
        try:
            enc = tiktoken.encoding_for_model(model)
            return len(enc.encode(text))
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
    except ImportError:
        pass
    return max(1, len(text) // 4)

def count_messages_tokens(model: str, messages: list) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += count_tokens(model, part.get("text", ""))
        else:
            total += count_tokens(model, str(content))
    return total

# ── Kimi estimate-token-count ─────────────────────────────────

def _kimi_estimate_tokens(model: str, messages: list, api_key: str) -> int | None:
    """调用 Kimi 官方 Token 估算 API 获取 prompt tokens 精确值"""
    url = KIMI_ESTIMATE_URL
    try:
        resp = requests.post(
            url,
            headers={"Authorization": api_key if api_key.startswith("Bearer ") else f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={"model": model, "messages": messages},
            timeout=10,
        )
        if resp.ok:
            total = resp.json().get("data", {}).get("total_tokens")
            if total is not None:
                print(f"  KIMI ESTIMATE: {total} tokens")
                return total
        print(f"  KIMI ESTIMATE FAIL: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        print(f"  KIMI ESTIMATE ERROR: {e}")
    return None

# ── core proxy logic ──────────────────────────────────────────

def _extract_content(data: dict) -> str:
    try:
        if "choices" in data:
            msg = data["choices"][0].get("message", {})
            parts = [msg.get("content", "") or "", msg.get("reasoning_content", "") or ""]
            return " ".join(p for p in parts if p)
        if "content" in data:
            parts = data["content"]
            if isinstance(parts, list):
                return " ".join(p.get("text", "") for p in parts if p.get("type") == "text")
            return str(parts)
    except Exception:
        pass
    return ""

def _log_usage(model, provider, prompt_tokens, completion_tokens, total_tokens, endpoint):
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO usage_logs (model, provider, prompt_tokens, completion_tokens, total_tokens, endpoint) VALUES (?, ?, ?, ?, ?, ?)",
            (model, provider, prompt_tokens, completion_tokens, total_tokens, endpoint),
        )
        conn.commit()
        conn.close()
        print(f"  LOG: {model} | {provider} | +{prompt_tokens}p +{completion_tokens}c = {total_tokens}t")
    except Exception as e:
        print(f"  LOG FAIL: {e}")

def _handle_stream(target_url, headers, body_data, model, provider_label, prompt_tokens, raw_body=None):
    if raw_body:
        resp = requests.post(target_url, headers=headers, data=raw_body, stream=True, timeout=300)
    else:
        resp = requests.post(target_url, headers=headers, json=body_data, stream=True, timeout=300)
    if not resp.ok:
        err = resp.text[:500] if resp.text else "(empty body)"
        print(f"  UPSTREAM {resp.status_code} (stream): {err}")
        print(f"  REQUEST URL: {target_url}")
        auth = headers.get("Authorization", "")
        masked = {k:v if k.lower() != 'authorization' else (auth[:15]+'...' if auth else '(none)') for k,v in headers.items()}
        print(f"  REQUEST HEADERS: {masked}")
        return jsonify({"error": err}), resp.status_code

    complete_content = ""
    usage_data = None
    path = request.path

    def generate():
        nonlocal complete_content, usage_data
        resp.encoding = "utf-8"
        buf = []
        for raw_line in resp.iter_lines(decode_unicode=True):
            if raw_line == "":
                if not buf:
                    continue
                event = "\n".join(buf)
                buf = []
                yield event + "\n\n"
                for l in event.split("\n"):
                    if not l.startswith("data:"):
                        continue
                    payload = l[5:].lstrip()
                    if payload == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(payload)
                        if provider_label == "anthropic":
                            if chunk.get("type") == "content_block_delta":
                                complete_content += chunk.get("delta", {}).get("text") or ""
                            elif chunk.get("type") == "message_delta":
                                usage_data = chunk.get("usage", {})
                        else:
                            choices = chunk.get("choices", [])
                            if choices and len(choices) > 0:
                                content = choices[0].get("delta", {}).get("content") or choices[0].get("message", {}).get("content")
                                content = content or choices[0].get("delta", {}).get("reasoning_content")
                                if content:
                                    complete_content += content
                            if chunk.get("usage"):
                                usage_data = chunk["usage"]
                    except json.JSONDecodeError:
                        pass
                continue
            buf.append(raw_line)
        if buf:
            yield "\n".join(buf) + "\n\n"
        yield "data: [DONE]\n\n"

    def do_log():
        if usage_data:
            pt = usage_data.get("prompt_tokens", 0) or prompt_tokens
            ct = usage_data.get("completion_tokens", 0) or usage_data.get("output_tokens", 0)
            if not ct:
                ct = count_tokens(model, complete_content)
                print(f"  LOG: usage had 0 completion, fallback count={ct}")
        else:
            pt = prompt_tokens
            ct = count_tokens(model, complete_content)
        _log_usage(model, provider_label, pt, ct, pt + ct, path)

    response = Response(stream_with_context(generate()), content_type=resp.headers.get("content-type", "text/event-stream"))
    response.call_on_close(do_log)
    return response

# ── endpoints ─────────────────────────────────────────────────

def _clean_messages(body_dict: dict) -> dict:
    msgs = body_dict.get("messages", [])
    if not msgs:
        return body_dict
    cleaned = [m for m in msgs if m.get("content") or m.get("tool_calls") or m.get("function_call")]
    if len(cleaned) == len(msgs):
        return body_dict
    body_dict["messages"] = cleaned
    return body_dict


def _proxy_passthrough(upstream_url, body, model, stream):
    headers = {"Content-Type": "application/json"}
    for h in ("Authorization", "User-Agent"):
        v = request.headers.get(h)
        if v:
            headers[h] = v

    print(f"  INCOMING HEADERS: {dict(request.headers)}")
    body = _clean_messages(body)
    model = body.get("model", "")
    hostname = upstream_url.replace("https://", "").replace("http://", "").split("/")[0]
    provider_label = hostname.split(":")[0]

    prompt_tokens = count_messages_tokens(model, body.get("messages", []))
    if "kimi" in hostname or "moonshot" in hostname or os.environ.get("KIMI_FORCE_ESTIMATE"):
        estimated = _kimi_estimate_tokens(model, body.get("messages", []), headers.get("Authorization", ""))
        if estimated is not None:
            prompt_tokens = estimated
    raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")

    if stream:
        return _handle_stream(upstream_url, headers, body, model, provider_label, prompt_tokens, raw_body)

    print(f"  POST {upstream_url} | model={model} | stream={stream} | body={raw_body[:200].decode('utf-8', errors='replace')}")
    resp = requests.post(upstream_url, headers=headers, data=raw_body, timeout=300)
    if not resp.ok:
        err = resp.text[:500] if resp.text else "(empty body)"
        print(f"  UPSTREAM {resp.status_code}: {err}")
        print(f"  REQUEST HEADERS: { {k:v for k,v in headers.items() if k.lower() != 'authorization'} }")
        return jsonify({"error": err}), resp.status_code

    data = resp.json()
    usage = data.get("usage", {})
    if usage.get("prompt_tokens"):
        prompt_tokens = usage["prompt_tokens"]
    completion_tokens = usage.get("completion_tokens", 0) or count_tokens(model, _extract_content(data))
    _log_usage(model, provider_label, prompt_tokens, completion_tokens, prompt_tokens + completion_tokens, request.path)
    return jsonify(data)


@app.route("/v1/chat/completions", methods=["POST"])
def proxy():
    body = request.get_json(force=True)
    model = body.get("model", "")
    stream = body.get("stream", False)

    upstream_url = request.args.get("by")
    if upstream_url:
        return _proxy_passthrough(upstream_url.rstrip("/") + "/chat/completions", body, model, stream)

    return jsonify({"error": "?by=<upstream_url> required"}), 400


@app.route("/v1", methods=["POST"])
def proxy_v1():
    """SDK appends /chat/completions to baseURL, but when ?by= has query,
    the append lands inside the query value. Catch that here."""
    body = request.get_json(force=True)
    model = body.get("model", "")
    stream = body.get("stream", False)

    upstream_url = request.args.get("by")
    if not upstream_url:
        return jsonify({"error": "?by=<upstream_url> required"}), 400

    return _proxy_passthrough(upstream_url, body, model, stream)


@app.route("/api/usage")
def api_usage():
    start = request.args.get("start", (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"))
    end = request.args.get("end", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    group_by = request.args.get("group_by", "hour")

    if group_by == "minute":
        sql_format = "strftime('%Y-%m-%d %H:%M', request_time)"
    elif group_by == "hour":
        sql_format = "strftime('%Y-%m-%d %H:00', request_time)"
    elif group_by == "day":
        sql_format = "date(request_time)"
    else:
        sql_format = "strftime('%Y-%m-%d %H:00', request_time)"

    conn = get_db()
    total_in_range = conn.execute("SELECT COUNT(*) as n FROM usage_logs WHERE request_time BETWEEN ? AND ?", (start, end)).fetchone()["n"]
    total_all = conn.execute("SELECT COUNT(*) as n FROM usage_logs").fetchone()["n"]
    print(f"  USAGE API: start={start} end={end} | in_range={total_in_range} total_all={total_all}")
    rows = conn.execute(
        f"""
        SELECT {sql_format} as period,
               SUM(prompt_tokens) as prompt_tokens,
               SUM(completion_tokens) as completion_tokens,
               SUM(total_tokens) as total_tokens,
               COUNT(*) as request_count,
               model,
               provider
        FROM usage_logs
        WHERE request_time BETWEEN ? AND ?
        GROUP BY period, model, provider
        ORDER BY period ASC
        """,
        (start, end),
    ).fetchall()

    by_model = {}
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "request_count": 0}
    timeline = []

    for r in rows:
        key = f"{r['model']} ({r['provider']})"
        if key not in by_model:
            by_model[key] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "request_count": 0}
        by_model[key]["prompt_tokens"] += r["prompt_tokens"]
        by_model[key]["completion_tokens"] += r["completion_tokens"]
        by_model[key]["total_tokens"] += r["total_tokens"]
        by_model[key]["request_count"] += r["request_count"]

        totals["prompt_tokens"] += r["prompt_tokens"]
        totals["completion_tokens"] += r["completion_tokens"]
        totals["total_tokens"] += r["total_tokens"]
        totals["request_count"] += r["request_count"]

        timeline.append({
            "period": r["period"],
            "model": r["model"],
            "total_tokens": r["total_tokens"],
            "request_count": r["request_count"],
        })

    month_rows = conn.execute(
        "SELECT provider, COUNT(*) as cnt, SUM(prompt_tokens) as prompt_tokens, SUM(completion_tokens) as completion_tokens FROM usage_logs WHERE request_time BETWEEN ? AND ? GROUP BY provider",
        (start, end),
    ).fetchall()
    conn.close()

    billing_by_provider = {}
    for row in month_rows:
        prov = row["provider"]
        billing_by_provider[prov] = {
            "month_used": row["cnt"],
            "prompt_tokens": row["prompt_tokens"] or 0,
            "completion_tokens": row["completion_tokens"] or 0,
        }

    return jsonify({
        "totals": totals,
        "by_model": by_model,
        "timeline": timeline,
        "period": {"start": start, "end": end},
        "billing": billing_by_provider,
    })


@app.route("/api/recent")
def api_recent():
    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 100, type=int)
    start = request.args.get("start")
    end = request.args.get("end")
    if page < 1:
        page = 1
    if page_size < 1:
        page_size = 100
    offset = (page - 1) * page_size

    conn = get_db()
    where = ""
    params = []
    if start and end:
        where = "WHERE request_time BETWEEN ? AND ?"
        params = [start, end]

    total = conn.execute(f"SELECT COUNT(*) as n FROM usage_logs {where}", params).fetchone()["n"]
    rows = conn.execute(
        f"SELECT * FROM usage_logs {where} ORDER BY request_time DESC LIMIT ? OFFSET ?",
        params + [page_size, offset],
    ).fetchall()
    conn.close()

    return jsonify({
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [dict(r) for r in rows],
    })


@app.route("/")
def dashboard():
    refresh_interval = int(_cfg("refresh_interval", "REFRESH_INTERVAL", 15)) * 1000
    return render_template("index.html", refresh_interval=refresh_interval)


init_db()

if __name__ == "__main__":
    print(f"AI Gateway running on http://0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT)
