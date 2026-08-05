import os
import sys
import json
import shutil
import time
import sqlite3
import tempfile
from datetime import datetime, timedelta

import requests
from flask import Flask, request, jsonify, render_template, Response, stream_with_context, send_file

VERSION = "0.6.12"

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

# 出站代理：解决云服务器 IP 被上游 AI API 封禁问题
# 支持 http://host:port、socks5://host:port；留空则直连
OUTBOUND_PROXY = _cfg("outbound_proxy", "OUTBOUND_PROXY", "") or None
PROXIES = {"http": OUTBOUND_PROXY, "https": OUTBOUND_PROXY} if OUTBOUND_PROXY else None

# 统一数据目录：无论通过 vbs 还是 exe 启动，默认都用同一个位置
DATA_DIR = os.path.join(os.path.expandvars("%APPDATA%"), "AI Gateway")
os.makedirs(DATA_DIR, exist_ok=True)

_db_path = os.path.expandvars(_cfg("db_path", "DB_PATH", os.path.join(DATA_DIR, "usage.db")))
# 兼容 config.json 中可能存在的相对路径：基于 EXE_DIR 解析为绝对路径
DB_PATH = os.path.join(EXE_DIR, _db_path) if not os.path.isabs(_db_path) else _db_path

# 对话内容单独存储，与 usage.db 隔离，便于单独删除/导出
_conv_db_path = os.path.expandvars(_cfg("conv_db_path", "CONV_DB_PATH", os.path.join(DATA_DIR, "conversations.db")))
CONV_DB_PATH = os.path.join(EXE_DIR, _conv_db_path) if not os.path.isabs(_conv_db_path) else _conv_db_path

# ── database ──────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model TEXT NOT NULL,
            provider TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            cached_tokens INTEGER NOT NULL DEFAULT 0,
            reasoning_tokens INTEGER NOT NULL DEFAULT 0,
            request_time DATETIME DEFAULT (datetime('now', 'localtime')),
            endpoint TEXT,
            duration_ms INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model TEXT NOT NULL UNIQUE,
            api_base TEXT NOT NULL,
            api_key TEXT DEFAULT '',
            is_default INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT (datetime('now', 'localtime'))
        )
    """)
    for col, dtype in [("duration_ms", "INTEGER DEFAULT 0"), ("conversation_id", "INTEGER"), ("cached_tokens", "INTEGER DEFAULT 0"), ("reasoning_tokens", "INTEGER DEFAULT 0")]:
        try:
            conn.execute(f"ALTER TABLE usage_logs ADD COLUMN {col} {dtype}")
        except sqlite3.OperationalError:
            pass
    conn.commit()

def get_conv_db():
    conn = sqlite3.connect(CONV_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def ensure_conv_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model TEXT NOT NULL,
            provider TEXT NOT NULL,
            endpoint TEXT,
            request_time DATETIME DEFAULT (datetime('now', 'localtime')),
            messages TEXT NOT NULL,
            response TEXT NOT NULL DEFAULT '',
            system_prompt TEXT DEFAULT '',
            last_user_message TEXT DEFAULT '',
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            duration_ms INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_time ON conversations(request_time)")
    conn.commit()

def init_db():
    conn = get_db()
    ensure_schema(conn)
    conn.close()
    conn = get_conv_db()
    ensure_conv_schema(conn)
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


# ── glm tokenizer ─────────────────────────────────────────────

GLM_TOKENIZER_URL = _cfg("glm_tokenizer_url", "GLM_TOKENIZER_URL", "https://open.bigmodel.cn/api/paas/v4/tokenizer")


def glm_count_messages_tokens(model: str, messages: list) -> int | None:
    """Use GLM's official tokenizer API for accurate counting. Returns None on failure."""
    api_key = CONFIG.get("glm_api_key", "") or os.environ.get("GLM_API_KEY", "")
    if not api_key:
        return None
    try:
        resp = requests.post(
            GLM_TOKENIZER_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json={"model": model, "messages": messages},
            timeout=10,
            proxies=PROXIES,
        )
        if resp.ok:
            return resp.json()["usage"]["prompt_tokens"]
    except Exception:
        pass
    return None


def count_messages_tokens(model: str, messages: list) -> int:
    # GLM models: use official tokenizer API for accuracy
    if model.lower().startswith("glm"):
        glm_tokens = glm_count_messages_tokens(model, messages)
        if glm_tokens is not None:
            return glm_tokens
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

def _msg_text(content) -> str:
    if isinstance(content, list):
        return " ".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return str(content or "")

def _extract_cached_tokens(usage):
    """从 usage 中提取缓存命中 token 数，兼容多种上游返回格式。
    - OpenAI / DeepSeek 兼容: usage.prompt_tokens_details.cached_tokens
    - Anthropic 原生: usage.cache_read_input_tokens
    - fangna 中转扩展: usage.prompt_cache_hit_tokens
    """
    if not isinstance(usage, dict):
        return 0
    candidates = []
    details = usage.get("prompt_tokens_details") or {}
    if details.get("cached_tokens"):
        candidates.append(int(details["cached_tokens"]))
    if usage.get("cache_read_input_tokens"):
        candidates.append(int(usage["cache_read_input_tokens"]))
    if usage.get("prompt_cache_hit_tokens"):
        candidates.append(int(usage["prompt_cache_hit_tokens"]))
    return max(candidates) if candidates else 0


def _extract_reasoning_tokens(usage):
    """从 usage 中提取推理 token 数（思维链消耗），兼容多种上游返回格式。
    - OpenAI / DeepSeek / fangna 兼容: usage.completion_tokens_details.reasoning_tokens
    - Anthropic 原生: usage.output_tokens_details.reasoning_tokens
    """
    if not isinstance(usage, dict):
        return 0
    details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    rt = details.get("reasoning_tokens")
    return int(rt) if rt else 0


def _log_usage(model, provider, prompt_tokens, completion_tokens, total_tokens, endpoint, duration_ms=0, conversation_id=None, cached_tokens=0, reasoning_tokens=0):
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO usage_logs (model, provider, prompt_tokens, completion_tokens, total_tokens, cached_tokens, reasoning_tokens, endpoint, duration_ms, conversation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (model, provider, prompt_tokens, completion_tokens, total_tokens, cached_tokens, reasoning_tokens, endpoint, duration_ms, conversation_id),
        )
        conn.commit()
        conn.close()
        speed = f" {(total_tokens * 1000 // max(duration_ms, 1))}t/s" if duration_ms else ""
        print(f"  LOG: {model} | {provider} | +{prompt_tokens}p +{completion_tokens}c = {total_tokens}t | cache={cached_tokens} reasoning={reasoning_tokens} | {duration_ms}ms{speed}")
    except Exception as e:
        print(f"  LOG FAIL: {e}")

def _log_conversation(model, provider, endpoint, messages, response, prompt_tokens=0, completion_tokens=0, total_tokens=0, duration_ms=0):
    try:
        system_prompt = ""
        last_user = ""
        for m in messages or []:
            role = m.get("role")
            if role == "system":
                system_prompt = _msg_text(m.get("content"))
            elif role == "user":
                last_user = _msg_text(m.get("content"))
        conn = get_conv_db()
        cur = conn.execute(
            "INSERT INTO conversations (model, provider, endpoint, messages, response, system_prompt, last_user_message, prompt_tokens, completion_tokens, total_tokens, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                model, provider, endpoint,
                json.dumps(messages or [], ensure_ascii=False),
                response or "",
                system_prompt, last_user,
                prompt_tokens, completion_tokens, total_tokens, duration_ms,
            ),
        )
        conv_id = cur.lastrowid
        conn.commit()
        conn.close()
        return conv_id
    except Exception as e:
        print(f"  CONV LOG FAIL: {e}")
        return None

def _handle_stream(target_url, headers, body_data, model, provider_label, prompt_tokens, raw_body=None, messages=None):
    try:
        if raw_body:
            resp = requests.post(target_url, headers=headers, data=raw_body, stream=True, timeout=300, proxies=PROXIES)
        else:
            resp = requests.post(target_url, headers=headers, json=body_data, stream=True, timeout=300, proxies=PROXIES)
    except requests.RequestException as e:
        err = f"UPSTREAM REQUEST FAILED: {type(e).__name__}: {e}"
        print(f"  {err}")
        print(f"  TARGET: {target_url}")
        return jsonify({"error": err}), 502
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
            cached = _extract_cached_tokens(usage_data)
            reasoning = _extract_reasoning_tokens(usage_data)
            print(f"  LOG: upstream usage: {json.dumps(usage_data, ensure_ascii=False)}")
        else:
            pt = prompt_tokens
            ct = count_tokens(model, complete_content)
            cached = 0
            reasoning = 0
        end_time = state.get("end_time") or datetime.now()
        duration_ms = int((end_time - start_time).total_seconds() * 1000)
        conv_id = _log_conversation(model, provider_label, path, messages, complete_content, pt, ct, pt + ct, duration_ms)
        _log_usage(model, provider_label, pt, ct, pt + ct, path, duration_ms, conv_id, cached, reasoning)

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

    messages = body.get("messages", [])
    prompt_tokens = count_messages_tokens(model, messages)
    raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")

    if stream:
        return _handle_stream(upstream_url, headers, body, model, provider_label, prompt_tokens, raw_body, messages)

    print(f"  POST {upstream_url} | model={model} | stream={stream} | body={raw_body[:200].decode('utf-8', errors='replace')}")
    start_time = datetime.now()
    try:
        resp = requests.post(upstream_url, headers=headers, data=raw_body, timeout=300, proxies=PROXIES)
    except requests.RequestException as e:
        err = f"UPSTREAM REQUEST FAILED: {type(e).__name__}: {e}"
        print(f"  {err}")
        print(f"  TARGET: {upstream_url}")
        return jsonify({"error": err}), 502
    if not resp.ok:
        err = resp.text[:500] if resp.text else "(empty body)"
        print(f"  UPSTREAM {resp.status_code}: {err}")
        print(f"  REQUEST HEADERS: { {k:v for k,v in headers.items() if k.lower() != 'authorization'} }")
        return jsonify({"error": err}), resp.status_code

    data = resp.json()
    usage = data.get("usage", {})
    if usage.get("prompt_tokens"):
        prompt_tokens = usage["prompt_tokens"]
    content = _extract_content(data)
    completion_tokens = usage.get("completion_tokens", 0) or count_tokens(model, content)
    cached_tokens = _extract_cached_tokens(usage)
    reasoning_tokens = _extract_reasoning_tokens(usage)
    if usage:
        print(f"  LOG: upstream usage: {json.dumps(usage, ensure_ascii=False)}")
    duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
    conv_id = _log_conversation(model, provider_label, request.path, messages, content, prompt_tokens, completion_tokens, prompt_tokens + completion_tokens, duration_ms)
    _log_usage(model, provider_label, prompt_tokens, completion_tokens, prompt_tokens + completion_tokens, request.path, duration_ms, conv_id, cached_tokens, reasoning_tokens)
    return jsonify(data)


@app.route("/v1/chat/completions", methods=["POST"])
def proxy():
    body = request.get_json(force=True)
    model = body.get("model", "")
    stream = body.get("stream", False)

    upstream_url = request.args.get("by")
    if upstream_url:
        return _proxy_passthrough(upstream_url.rstrip("/") + "/chat/completions", body, model, stream)
    
    if not model:
        return jsonify({"error": "model is required when ?by is not specified"}), 400
    
    conn = get_db()
    row = conn.execute(
        "SELECT api_base, api_key FROM models WHERE (model = ? OR is_default = 1) ORDER BY is_default DESC LIMIT 1",
        (model,)
    ).fetchone()
    conn.close()
    
    if not row:
        return jsonify({"error": f"No model config found for '{model}'. Use ?by=<upstream_url> or configure models."}), 400
    
    api_base = row["api_base"].rstrip("/")
    api_key = row["api_key"]
    
    if api_key:
        request.headers = {**request.headers, "Authorization": f"Bearer {api_key}"}
    
    return _proxy_passthrough(api_base, body, model, stream)


@app.route("/v1", methods=["POST"])
def proxy_v1():
    upstream_url = request.args.get("by")
    body = request.get_json(force=True)
    model = body.get("model", "")
    stream = body.get("stream", False)

    if upstream_url:
        return _proxy_passthrough(upstream_url, body, model, stream)
    
    if not model:
        return jsonify({"error": "model is required when ?by is not specified"}), 400
    
    conn = get_db()
    row = conn.execute(
        "SELECT api_base, api_key FROM models WHERE (model = ? OR is_default = 1) ORDER BY is_default DESC LIMIT 1",
        (model,)
    ).fetchone()
    conn.close()
    
    if not row:
        return jsonify({"error": f"No model config found for '{model}'. Use ?by=<upstream_url> or configure models."}), 400
    
    api_base = row["api_base"].rstrip("/")
    api_key = row["api_key"]
    
    if api_key:
        request.headers = {**request.headers, "Authorization": f"Bearer {api_key}"}
    
    return _proxy_passthrough(api_base, body, model, stream)


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
    
    where_conditions = ["request_time BETWEEN ? AND ?"]
    params = [start, end]
    where_clause = " AND ".join(where_conditions)
    
    total_in_range = conn.execute(f"SELECT COUNT(*) as n FROM usage_logs WHERE {where_clause}", params).fetchone()["n"]
    total_all = conn.execute("SELECT COUNT(*) as n FROM usage_logs").fetchone()["n"]
    print(f"  USAGE API: start={start} end={end} | in_range={total_in_range} total_all={total_all}")
    rows = conn.execute(
        f"""
        SELECT {sql_format} as period,
               SUM(prompt_tokens) as prompt_tokens,
               SUM(completion_tokens) as completion_tokens,
               SUM(total_tokens) as total_tokens,
               SUM(cached_tokens) as cached_tokens,
               SUM(reasoning_tokens) as reasoning_tokens,
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
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cached_tokens": 0, "reasoning_tokens": 0, "request_count": 0}
    timeline = []

    for r in rows:
        key = f"{r['model']} ({r['provider']})"
        if key not in by_model:
            by_model[key] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cached_tokens": 0, "reasoning_tokens": 0, "request_count": 0}
        by_model[key]["prompt_tokens"] += r["prompt_tokens"]
        by_model[key]["completion_tokens"] += r["completion_tokens"]
        by_model[key]["total_tokens"] += r["total_tokens"]
        by_model[key]["cached_tokens"] += r["cached_tokens"]
        by_model[key]["reasoning_tokens"] += r["reasoning_tokens"]
        by_model[key]["request_count"] += r["request_count"]

        totals["prompt_tokens"] += r["prompt_tokens"]
        totals["completion_tokens"] += r["completion_tokens"]
        totals["total_tokens"] += r["total_tokens"]
        totals["cached_tokens"] += r["cached_tokens"]
        totals["reasoning_tokens"] += r["reasoning_tokens"]
        totals["request_count"] += r["request_count"]

        timeline.append({
            "period": r["period"],
            "model": r["model"],
            "total_tokens": r["total_tokens"],
            "request_count": r["request_count"],
        })

    month_rows = conn.execute(
        f"SELECT provider, COUNT(*) as cnt, SUM(prompt_tokens) as prompt_tokens, SUM(completion_tokens) as completion_tokens, SUM(cached_tokens) as cached_tokens, SUM(reasoning_tokens) as reasoning_tokens, SUM(duration_ms) as duration_ms FROM usage_logs WHERE {where_clause} GROUP BY provider",
        params,
    ).fetchall()

    model_rows = conn.execute(
        f"SELECT model, COUNT(*) as cnt, SUM(prompt_tokens) as prompt_tokens, SUM(completion_tokens) as completion_tokens, SUM(cached_tokens) as cached_tokens, SUM(reasoning_tokens) as reasoning_tokens, SUM(duration_ms) as duration_ms FROM usage_logs WHERE {where_clause} GROUP BY model",
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
            "cached_tokens": row["cached_tokens"] or 0,
            "reasoning_tokens": row["reasoning_tokens"] or 0,
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
            "cached_tokens": row["cached_tokens"] or 0,
            "reasoning_tokens": row["reasoning_tokens"] or 0,
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


@app.route("/api/conversations")
def api_conversations():
    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 50, type=int)
    start = request.args.get("start")
    end = request.args.get("end")
    q = request.args.get("q")
    if page < 1:
        page = 1
    if page_size < 1:
        page_size = 50
    offset = (page - 1) * page_size

    conn = get_conv_db()
    where_conditions = []
    params = []
    if start and end:
        where_conditions.append("request_time BETWEEN ? AND ?")
        params.extend([start, end])
    if q:
        where_conditions.append("(last_user_message LIKE ? OR response LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    where = "WHERE " + " AND ".join(where_conditions) if where_conditions else ""

    total = conn.execute(f"SELECT COUNT(*) as n FROM conversations {where}", params).fetchone()["n"]
    rows = conn.execute(
        f"SELECT * FROM conversations {where} ORDER BY request_time DESC LIMIT ? OFFSET ?",
        params + [page_size, offset],
    ).fetchall()
    conn.close()

    items = []
    for r in rows:
        d = dict(r)
        try:
            d["messages"] = json.loads(d.get("messages") or "[]")
        except Exception:
            d["messages"] = []
        items.append(d)

    return jsonify({
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": items,
    })


@app.route("/api/conversations/<int:conv_id>")
def api_conversation_detail(conv_id):
    conn = get_conv_db()
    row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    d = dict(row)
    try:
        d["messages"] = json.loads(d.get("messages") or "[]")
    except Exception:
        d["messages"] = []
    return jsonify(d)


ARCHIVE_DIR = os.path.join(DATA_DIR, "archives")


def _fetch_conversations(start=None, end=None):
    conn = get_conv_db()
    where = ""
    params = []
    if start and end:
        where = "WHERE request_time BETWEEN ? AND ?"
        params = [start, end]
    rows = conn.execute(
        f"SELECT * FROM conversations {where} ORDER BY request_time ASC", params
    ).fetchall()
    conn.close()
    items = []
    for r in rows:
        d = dict(r)
        try:
            d["messages"] = json.loads(d.get("messages") or "[]")
        except Exception:
            d["messages"] = []
        items.append(d)
    return items


@app.route("/api/conversations/export")
def api_conversations_export():
    start = request.args.get("start")
    end = request.args.get("end")
    items = _fetch_conversations(start, end)
    return jsonify({
        "period": {"start": start, "end": end},
        "count": len(items),
        "conversations": items,
    })


@app.route("/api/conversations/archive", methods=["POST"])
def api_conversations_archive():
    data = request.get_json(silent=True) or {}
    start = data.get("start") or request.args.get("start")
    end = data.get("end") or request.args.get("end")
    if not (start and end):
        return jsonify({"error": "start and end are required"}), 400

    items = _fetch_conversations(start, end)
    if not items:
        return jsonify({"success": True, "archived": 0, "message": "该时间范围无对话记录"})

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"conversations_{start[:10]}_{end[:10]}_{stamp}.json"
    fpath = os.path.join(ARCHIVE_DIR, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump({
            "period": {"start": start, "end": end},
            "count": len(items),
            "archived_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "conversations": items,
        }, f, ensure_ascii=False, indent=2)

    conn = get_conv_db()
    cur = conn.execute(
        "DELETE FROM conversations WHERE request_time BETWEEN ? AND ?", (start, end)
    )
    deleted = cur.rowcount
    conn.commit()
    conn.close()

    print(f"  ARCHIVE: {deleted} conversations -> {fpath}")
    return jsonify({
        "success": True,
        "archived": deleted,
        "file": fpath,
    })


@app.route("/api/models", methods=["GET"])
def get_models():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM models ORDER BY is_default DESC, model ASC"
    ).fetchall()
    conn.close()
    
    return jsonify({"models": [dict(r) for r in rows]})


@app.route("/api/models", methods=["POST"])
def add_model():
    data = request.get_json(force=True)
    model = data.get("model", "").strip()
    api_base = data.get("api_base", "").strip()
    api_key = data.get("api_key", "").strip()
    is_default = 1 if data.get("is_default") else 0
    
    if not model or not api_base:
        return jsonify({"error": "model and api_base are required"}), 400
    
    conn = get_db()
    try:
        if is_default:
            conn.execute("UPDATE models SET is_default = 0")
        
        conn.execute(
            "INSERT OR REPLACE INTO models (model, api_base, api_key, is_default) VALUES (?, ?, ?, ?)",
            (model, api_base, api_key, is_default)
        )
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500
    conn.close()
    
    return jsonify({"success": True})


@app.route("/api/models/<int:model_id>", methods=["DELETE"])
def delete_model(model_id):
    conn = get_db()
    conn.execute("DELETE FROM models WHERE id = ?", (model_id,))
    conn.commit()
    conn.close()
    
    return jsonify({"success": True})


@app.route("/api/models/<int:model_id>/default", methods=["PUT"])
def set_default_model(model_id):
    conn = get_db()
    conn.execute("UPDATE models SET is_default = 0")
    conn.execute("UPDATE models SET is_default = 1 WHERE id = ?", (model_id,))
    conn.commit()
    conn.close()
    
    return jsonify({"success": True})


@app.route("/")
def landing():
    return render_template("index.html")


@app.route("/favicon.ico")
def favicon():
    return app.send_static_file("icon.ico")

@app.route("/dashboard")
def dashboard():
    return render_template("index.html")


@app.route("/api/db/download")
def api_db_download():
    if not os.path.exists(DB_PATH):
        return jsonify({"error": "数据库文件不存在"}), 404
    return send_file(DB_PATH, as_attachment=True, download_name="usage.db")


@app.route("/api/db/upload", methods=["POST"])
def api_db_upload():
    if "file" not in request.files:
        return jsonify({"error": "未选择文件"}), 400
    f = request.files["file"]
    if f.filename == "":
        return jsonify({"error": "文件名为空"}), 400
    if not f.filename.endswith(".db"):
        return jsonify({"error": "仅支持 .db 文件"}), 400

    target_path = DB_PATH

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp_path = tmp.name
    try:
        f.save(tmp_path)
        tmp.close()

        conn = sqlite3.connect(tmp_path)
        conn.execute("PRAGMA journal_mode=WAL")
        ensure_schema(conn)
        conn.close()

        if os.path.exists(target_path):
            for i in range(5):
                try:
                    _src = sqlite3.connect(target_path)
                    try:
                        _dst = sqlite3.connect(target_path + ".bak")
                        try:
                            _src.backup(_dst)
                        finally:
                            _dst.close()
                    finally:
                        _src.close()
                    break
                except PermissionError:
                    if i == 4:
                        raise
                    time.sleep(0.3)

        for i in range(5):
            try:
                if os.path.exists(target_path):
                    os.remove(target_path)
                shutil.move(tmp_path, target_path)
                tmp_path = None
                break
            except PermissionError:
                if i == 4:
                    raise
                time.sleep(0.3)
    except Exception as e:
        return jsonify({"error": f"导入失败：{e}"}), 500
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    return jsonify({"success": True, "message": "导入成功"})


# ── insights: 每日/每周活动分析 ──────────────────────────────

INSIGHTS_TOPICS = ["前端/UI", "后端/API", "数据分析", "模型评测", "构建部署", "性能优化", "文档记录", "通用编程", "其他"]
INSIGHTS_CACHE = {}
INSIGHTS_CACHE_TTL = 1800
INSIGHTS_MAX_DAYS = 60
INSIGHTS_MAX_MSGS = 30


def _clean_insight_message(text):
    if not text:
        return None
    if "<system-reminder>" in text:
        text = text.split("<system-reminder>")[0]
    text = text.strip()
    if not text or len(text) < 3:
        return None
    lower = text.lower()
    if lower.startswith(("continue if", "help me plan", "update the")):
        return None
    if "operational mode has changed" in lower:
        return None
    return text


def _insight_bucket_key(dt_str, granularity):
    try:
        d = datetime.strptime(dt_str[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return dt_str[:10]
    if granularity == "day":
        return d.strftime("%Y-%m-%d")
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _get_insight_model(model_name=""):
    """分类模型来源优先级：1) config.json insights 块  2) models 表指定模型  3) models 表默认模型"""
    icfg = CONFIG.get("insights") or {}
    if model_name:
        if icfg.get("model") == model_name and icfg.get("api_base"):
            return {"model": icfg["model"], "api_base": icfg["api_base"], "api_key": icfg.get("api_key", "")}
        conn = get_db()
        try:
            return conn.execute("SELECT model, api_base, api_key FROM models WHERE model = ?", (model_name,)).fetchone()
        finally:
            conn.close()
    if icfg.get("api_base"):
        return {"model": icfg.get("model", ""), "api_base": icfg["api_base"], "api_key": icfg.get("api_key", "")}
    conn = get_db()
    try:
        return conn.execute("SELECT model, api_base, api_key FROM models WHERE is_default = 1 ORDER BY id LIMIT 1").fetchone()
    finally:
        conn.close()


def _parse_insight_json(content):
    if not content:
        return None
    content = content.strip()
    try:
        return json.loads(content)
    except Exception:
        pass
    try:
        s = content.find("{")
        e = content.rfind("}")
        if 0 <= s < e:
            return json.loads(content[s:e + 1])
    except Exception:
        pass
    return None


def _normalize_insight(data):
    if not isinstance(data, dict):
        return None
    summary = str(data.get("summary", "") or "").strip()
    topics = []
    for t in data.get("topics") or []:
        if not isinstance(t, dict):
            continue
        name = str(t.get("topic", "") or "").strip()
        acts = [str(a).strip() for a in (t.get("activities") or []) if str(a).strip()]
        if name:
            topics.append({"topic": name, "activities": acts[:20], "count": len(acts)})
    if not topics and not summary:
        return None
    return {"summary": summary, "topics": topics}


def _llm_classify_insight(model_cfg, label, messages, total_tokens):
    api_base = model_cfg["api_base"].rstrip("/")
    headers = {"Content-Type": "application/json"}
    if model_cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {model_cfg['api_key']}"
    sys_prompt = (
        "你是 AI 使用记录分析助手。根据用户在指定时间段内发给 AI 的消息，归纳出用户实际在做什么工作。\n"
        "输出必须 ONLY 是合法 JSON（不要任何其他文字），格式：\n"
        '{"summary": "一句话总结该时段用户在做什么", "topics": [{"topic": "主题名", "activities": ["具体活动1", "具体活动2"]}]}\n'
        "要求：\n"
        "1. topic 只能从以下主题中选择：" + "、".join(INSIGHTS_TOPICS) + "。\n"
        "2. 忽略系统提示、指令类（如\"继续\"\"执行\"）以及纯工具操作消息，聚焦用户真实意图。\n"
        "3. activities 每条用简短动词短语概括一项实际工作，尽量具体。\n"
        "4. 没有匹配的主题就不列出，summary 允许留空字符串。"
    )
    msg_list = "\n".join(f"- {m}" for m in messages)
    user_msg = f"时间段：{label}（{len(messages)} 条去重消息，总消耗约 {total_tokens} tokens）\n\n用户消息：\n{msg_list}"
    body = {
        "model": model_cfg["model"],
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        "temperature": 0.2,
    }
    try:
        resp = requests.post(api_base + "/chat/completions", headers=headers, json=body, timeout=120, proxies=PROXIES)
        if not resp.ok:
            print(f"  INSIGHT LLM {resp.status_code}: {resp.text[:300]}")
            return None
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return _normalize_insight(_parse_insight_json(content))
    except Exception as e:
        print(f"  INSIGHT LLM FAIL: {e}")
        return None


@app.route("/api/insights")
def api_insights():
    granularity = request.args.get("granularity", "day")
    if granularity not in ("day", "week"):
        granularity = "day"
    start = request.args.get("start") or (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00")
    end = request.args.get("end") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    model_name = request.args.get("model", "")
    refresh = request.args.get("refresh") == "1"

    try:
        s_dt = datetime.strptime(start[:10], "%Y-%m-%d")
        e_dt = datetime.strptime(end[:10], "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "无效的时间范围"}), 400

    truncated = False
    if (e_dt - s_dt).days > INSIGHTS_MAX_DAYS:
        e_dt = s_dt + timedelta(days=INSIGHTS_MAX_DAYS)
        end = e_dt.strftime("%Y-%m-%d 23:59:59")
        truncated = True

    cache_key = f"{granularity}|{start}|{end}|{model_name}"
    if not refresh:
        hit = INSIGHTS_CACHE.get(cache_key)
        if hit and time.time() - hit["ts"] < INSIGHTS_CACHE_TTL:
            return jsonify(hit["data"])

    row = _get_insight_model(model_name)
    if not row:
        return jsonify({"error": "未配置分类模型：请在 模型 中添加，或 ?model=<模型名>"}), 400
    model_cfg = {"model": row["model"], "api_base": row["api_base"], "api_key": row["api_key"]}

    conn = get_conv_db()
    rows = conn.execute(
        "SELECT request_time, last_user_message, total_tokens FROM conversations WHERE request_time BETWEEN ? AND ? ORDER BY request_time ASC",
        (start, end),
    ).fetchall()
    conn.close()

    buckets = {}
    for r in rows:
        key = _insight_bucket_key(r["request_time"], granularity)
        b = buckets.setdefault(key, {"messages": set(), "tokens": 0})
        msg = _clean_insight_message(r["last_user_message"])
        if msg:
            b["messages"].add(msg)
        b["tokens"] += r["total_tokens"] or 0

    periods = []
    failed = []
    for label in sorted(buckets):
        b = buckets[label]
        msgs = sorted(b["messages"])[:INSIGHTS_MAX_MSGS]
        result = _llm_classify_insight(model_cfg, label, msgs, b["tokens"])
        if result is None:
            failed.append(label)
            continue
        periods.append({
            "period": label,
            "summary": result["summary"],
            "topics": result["topics"],
            "message_count": len(msgs),
            "total_tokens": b["tokens"],
        })

    data = {
        "granularity": granularity,
        "periods": periods,
        "failed": failed,
        "truncated": truncated,
        "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    INSIGHTS_CACHE[cache_key] = {"ts": time.time(), "data": data}
    return jsonify(data)


init_db()

if __name__ == "__main__":
    print(f"AI Gateway running on http://0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT)
