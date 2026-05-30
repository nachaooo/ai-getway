import os
import sys
import json
import sqlite3
from datetime import datetime, timedelta

import requests
from flask import Flask, request, jsonify, render_template, Response, stream_with_context

VERSION = "0.6.1"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# PyInstaller 支持：exe 所在目录为可写目录，sys._MEIPASS 为打包资源目录
if getattr(sys, 'frozen', False):
    EXE_DIR = os.path.dirname(sys.executable)
    RESOURCE_DIR = sys._MEIPASS
else:
    EXE_DIR = BASE_DIR
    RESOURCE_DIR = BASE_DIR

CONFIG = {}
_config_path = os.path.join(EXE_DIR, "config.json")
if os.path.exists(_config_path):
    try:
        with open(_config_path, encoding="utf-8") as f:
            CONFIG = json.load(f)
    except Exception:
        pass

def _cfg(key, env_var, default):
    return os.environ.get(env_var) or CONFIG.get(key, default)

app = Flask(
    __name__,
    template_folder=os.path.join(RESOURCE_DIR, "templates"),
    static_folder=os.path.join(RESOURCE_DIR, "static"),
)

PORT = int(_cfg("port", "PORT", 5000))

# 统一数据目录：无论通过 vbs 还是 exe 启动，默认都用同一个位置
DATA_DIR = os.path.join(os.path.expandvars("%APPDATA%"), "AI Gateway")
os.makedirs(DATA_DIR, exist_ok=True)

_db_path = _cfg("db_path", "DB_PATH", os.path.join(DATA_DIR, "usage.db"))
# 兼容 config.json 中可能存在的相对路径：基于 EXE_DIR 解析为绝对路径
DB_PATH = os.path.join(EXE_DIR, _db_path) if not os.path.isabs(_db_path) else _db_path

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
            scope TEXT NOT NULL DEFAULT 'default',
            model TEXT NOT NULL,
            provider TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            request_time DATETIME DEFAULT (datetime('now', 'localtime')),
            endpoint TEXT,
            duration_ms INTEGER DEFAULT 0
        )
    """)
    # 用户模型配置表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scope_models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            model TEXT NOT NULL,
            api_base TEXT NOT NULL,
            api_key TEXT DEFAULT '',
            is_default INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT (datetime('now', 'localtime')),
            UNIQUE(scope, model)
        )
    """)
    # 兼容旧数据库：添加新列
    for col, dtype in [("duration_ms", "INTEGER DEFAULT 0"), ("scope", "TEXT NOT NULL DEFAULT 'default'")]:
        try:
            conn.execute(f"ALTER TABLE usage_logs ADD COLUMN {col} {dtype}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()

# ── tokenizer ──────────────────────────────────────────────────

DEEPSEEK_TOKENIZER_DIR = os.path.join(RESOURCE_DIR, "tokenizers", "deepseek")
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

def _log_usage(scope, model, provider, prompt_tokens, completion_tokens, total_tokens, endpoint, duration_ms=0):
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO usage_logs (scope, model, provider, prompt_tokens, completion_tokens, total_tokens, endpoint, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (scope, model, provider, prompt_tokens, completion_tokens, total_tokens, endpoint, duration_ms),
        )
        conn.commit()
        conn.close()
        speed = f" {(total_tokens * 1000 // max(duration_ms, 1))}t/s" if duration_ms else ""
        print(f"  LOG: [{scope}] {model} | {provider} | +{prompt_tokens}p +{completion_tokens}c = {total_tokens}t | {duration_ms}ms{speed}")
    except Exception as e:
        print(f"  LOG FAIL: {e}")

def _handle_stream(scope, target_url, headers, body_data, model, provider_label, prompt_tokens, raw_body=None):
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
    start_time = datetime.now()
    state = {"end_time": None}

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
        state["end_time"] = datetime.now()

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
        end_time = state.get("end_time") or datetime.now()
        duration_ms = int((end_time - start_time).total_seconds() * 1000)
        _log_usage(scope, model, provider_label, pt, ct, pt + ct, path, duration_ms)

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


def _proxy_passthrough(scope, upstream_url, body, model, stream):
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
    raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")

    if stream:
        return _handle_stream(scope, upstream_url, headers, body, model, provider_label, prompt_tokens, raw_body)

    print(f"  POST {upstream_url} | model={model} | stream={stream} | body={raw_body[:200].decode('utf-8', errors='replace')}")
    start_time = datetime.now()
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
    duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
    _log_usage(scope, model, provider_label, prompt_tokens, completion_tokens, prompt_tokens + completion_tokens, request.path, duration_ms)
    return jsonify(data)


@app.route("/v1/chat/completions", methods=["POST"])
@app.route("/<scope>/v1/chat/completions", methods=["POST"])
def proxy(scope="default"):
    body = request.get_json(force=True)
    model = body.get("model", "")
    stream = body.get("stream", False)

    upstream_url = request.args.get("by")
    if upstream_url:
        return _proxy_passthrough(scope, upstream_url.rstrip("/") + "/chat/completions", body, model, stream)
    
    # 没有 by 参数时，查找默认模型配置
    if not model:
        return jsonify({"error": "model is required when ?by is not specified"}), 400
    
    conn = get_db()
    row = conn.execute(
        "SELECT api_base, api_key FROM scope_models WHERE scope = ? AND (model = ? OR is_default = 1) ORDER BY is_default DESC LIMIT 1",
        (scope, model)
    ).fetchone()
    conn.close()
    
    if not row:
        return jsonify({"error": f"No model config found for scope '{scope}' and model '{model}'. Use ?by=<upstream_url> or configure models."}), 400
    
    api_base = row["api_base"].rstrip("/")
    api_key = row["api_key"]
    
    # 构建请求头
    if api_key:
        request.headers = {**request.headers, "Authorization": f"Bearer {api_key}"}
    
    return _proxy_passthrough(scope, api_base + "/chat/completions", body, model, stream)


@app.route("/v1", methods=["POST"])
@app.route("/<scope>/v1", methods=["POST"])
def proxy_v1(scope="default"):
    """SDK appends /chat/completions to baseURL, but when ?by= has query,
    the append lands inside the query value. Catch that here."""
    body = request.get_json(force=True)
    model = body.get("model", "")
    stream = body.get("stream", False)

    upstream_url = request.args.get("by")
    if upstream_url:
        return _proxy_passthrough(scope, upstream_url, body, model, stream)
    
    # 没有 by 参数时，查找默认模型配置
    if not model:
        return jsonify({"error": "model is required when ?by is not specified"}), 400
    
    conn = get_db()
    row = conn.execute(
        "SELECT api_base, api_key FROM scope_models WHERE scope = ? AND (model = ? OR is_default = 1) ORDER BY is_default DESC LIMIT 1",
        (scope, model)
    ).fetchone()
    conn.close()
    
    if not row:
        return jsonify({"error": f"No model config found for scope '{scope}' and model '{model}'. Use ?by=<upstream_url> or configure models."}), 400
    
    api_base = row["api_base"].rstrip("/")
    api_key = row["api_key"]
    
    # 构建请求头
    if api_key:
        request.headers = {**request.headers, "Authorization": f"Bearer {api_key}"}
    
    return _proxy_passthrough(scope, api_base, body, model, stream)


@app.route("/api/usage")
def api_usage():
    start = request.args.get("start", (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"))
    end = request.args.get("end", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    group_by = request.args.get("group_by", "hour")
    scope = request.args.get("scope")

    if group_by == "minute":
        sql_format = "strftime('%Y-%m-%d %H:%M', request_time)"
    elif group_by == "hour":
        sql_format = "strftime('%Y-%m-%d %H:00', request_time)"
    elif group_by == "day":
        sql_format = "date(request_time)"
    else:
        sql_format = "strftime('%Y-%m-%d %H:00', request_time)"

    conn = get_db()
    
    # 构建 WHERE 条件
    where_conditions = ["request_time BETWEEN ? AND ?"]
    params = [start, end]
    if scope:
        where_conditions.append("scope = ?")
        params.append(scope)
    where_clause = " AND ".join(where_conditions)
    
    total_in_range = conn.execute(f"SELECT COUNT(*) as n FROM usage_logs WHERE {where_clause}", params).fetchone()["n"]
    total_all = conn.execute("SELECT COUNT(*) as n FROM usage_logs").fetchone()["n"]
    print(f"  USAGE API: start={start} end={end} scope={scope} | in_range={total_in_range} total_all={total_all}")
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
        WHERE {where_clause}
        GROUP BY period, model, provider
        ORDER BY period ASC
        """,
        params,
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
        f"SELECT provider, COUNT(*) as cnt, SUM(prompt_tokens) as prompt_tokens, SUM(completion_tokens) as completion_tokens, SUM(duration_ms) as duration_ms FROM usage_logs WHERE {where_clause} GROUP BY provider",
        params,
    ).fetchall()

    model_rows = conn.execute(
        f"SELECT model, COUNT(*) as cnt, SUM(prompt_tokens) as prompt_tokens, SUM(completion_tokens) as completion_tokens, SUM(duration_ms) as duration_ms FROM usage_logs WHERE {where_clause} GROUP BY model",
        params,
    ).fetchall()
    conn.close()

    billing_by_provider = {}
    for row in month_rows:
        prov = row["provider"]
        total_duration_ms = row["duration_ms"] or 0
        total_completion = row["completion_tokens"] or 0
        tokens_per_second = 0
        if total_duration_ms > 0:
            tokens_per_second = round(total_completion * 1000 / total_duration_ms, 1)
        billing_by_provider[prov] = {
            "month_used": row["cnt"],
            "prompt_tokens": row["prompt_tokens"] or 0,
            "completion_tokens": row["completion_tokens"] or 0,
            "tokens_per_second": tokens_per_second,
        }

    billing_by_model = {}
    for row in model_rows:
        mdl = row["model"]
        total_duration_ms = row["duration_ms"] or 0
        total_completion = row["completion_tokens"] or 0
        tokens_per_second = 0
        if total_duration_ms > 0:
            tokens_per_second = round(total_completion * 1000 / total_duration_ms, 1)
        billing_by_model[mdl] = {
            "month_used": row["cnt"],
            "prompt_tokens": row["prompt_tokens"] or 0,
            "completion_tokens": row["completion_tokens"] or 0,
            "tokens_per_second": tokens_per_second,
        }

    return jsonify({
        "totals": totals,
        "by_model": by_model,
        "timeline": timeline,
        "period": {"start": start, "end": end},
        "billing": billing_by_provider,
        "billing_model": billing_by_model,
    })


@app.route("/api/recent")
def api_recent():
    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 100, type=int)
    start = request.args.get("start")
    end = request.args.get("end")
    scope = request.args.get("scope")
    if page < 1:
        page = 1
    if page_size < 1:
        page_size = 100
    offset = (page - 1) * page_size

    conn = get_db()
    where_conditions = []
    params = []
    if start and end:
        where_conditions.append("request_time BETWEEN ? AND ?")
        params.extend([start, end])
    if scope:
        where_conditions.append("scope = ?")
        params.append(scope)
    
    where = "WHERE " + " AND ".join(where_conditions) if where_conditions else ""

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


@app.route("/api/scopes")
def api_scopes():
    """获取所有命名空间列表"""
    conn = get_db()
    rows = conn.execute(
        "SELECT scope, COUNT(*) as request_count, SUM(total_tokens) as total_tokens FROM usage_logs GROUP BY scope ORDER BY request_count DESC"
    ).fetchall()
    conn.close()
    
    scopes = [dict(r) for r in rows]
    return jsonify({"scopes": scopes})


@app.route("/api/scope/<scope>/models", methods=["GET"])
def get_scope_models(scope):
    """获取指定命名空间的模型配置"""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM scope_models WHERE scope = ? ORDER BY is_default DESC, model ASC",
        (scope,)
    ).fetchall()
    conn.close()
    
    models = [dict(r) for r in rows]
    return jsonify({"scope": scope, "models": models})


@app.route("/api/scope/<scope>/models", methods=["POST"])
def add_scope_model(scope):
    """添加模型配置"""
    data = request.get_json(force=True)
    model = data.get("model", "").strip()
    api_base = data.get("api_base", "").strip()
    api_key = data.get("api_key", "").strip()
    is_default = 1 if data.get("is_default") else 0
    
    if not model or not api_base:
        return jsonify({"error": "model and api_base are required"}), 400
    
    conn = get_db()
    try:
        # 如果设置为默认，先取消其他默认
        if is_default:
            conn.execute("UPDATE scope_models SET is_default = 0 WHERE scope = ?", (scope,))
        
        conn.execute(
            "INSERT OR REPLACE INTO scope_models (scope, model, api_base, api_key, is_default) VALUES (?, ?, ?, ?, ?)",
            (scope, model, api_base, api_key, is_default)
        )
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500
    conn.close()
    
    return jsonify({"success": True})


@app.route("/api/scope/<scope>/models/<int:model_id>", methods=["DELETE"])
def delete_scope_model(scope, model_id):
    """删除模型配置"""
    conn = get_db()
    conn.execute("DELETE FROM scope_models WHERE id = ? AND scope = ?", (model_id, scope))
    conn.commit()
    conn.close()
    
    return jsonify({"success": True})


@app.route("/api/scope/<scope>/models/<int:model_id>/default", methods=["PUT"])
def set_default_model(scope, model_id):
    """设置默认模型"""
    conn = get_db()
    # 取消该命名空间下所有默认
    conn.execute("UPDATE scope_models SET is_default = 0 WHERE scope = ?", (scope,))
    # 设置指定模型为默认
    conn.execute("UPDATE scope_models SET is_default = 1 WHERE id = ? AND scope = ?", (model_id, scope))
    conn.commit()
    conn.close()
    
    return jsonify({"success": True})


@app.route("/")
def landing():
    return render_template("landing.html")


@app.route("/dashboard")
@app.route("/<scope>")
def dashboard(scope=None):
    # 排除 API 路径和其他静态资源
    if scope and (scope.startswith('api') or scope.startswith('static') or scope == 'favicon.ico'):
        return jsonify({"error": "Not found"}), 404
    return render_template("index.html", scope=scope or "")


init_db()

if __name__ == "__main__":
    print(f"AI Gateway running on http://0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT)
