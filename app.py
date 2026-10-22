import os
import re
import sys
import json
import shutil
import time
import hashlib
import sqlite3
import tempfile
import threading
from collections import OrderedDict
from datetime import datetime, timedelta

import requests
from flask import Flask, request, jsonify, render_template, Response, stream_with_context, send_file

VERSION = "0.6.30"

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

# ── 请求体体量护栏 ────────────────────────────────────────────
# 背景（2026-10-22 全站 504）：一个 43MB 请求体把网关打到假死。
#   ① request.get_json() 把 body 全量解析成 Python 对象，实测放大 5.9 倍
#      （23MB body → 136MB：原始 bytes + 解析对象 + 重序列化 bytes）；
#   ② json.dumps() 把解析结果整份重新序列化，json 的 C 编码器全程持有 GIL；
#   ③ 进程 RSS 撞到 cgroup MemoryHigh 后，分配进入「回收失败→重试」循环，
#      该线程 100% 内核态空转、零进展，32 个 waitress worker 全部拿不到 GIL，
#      连 GET / 都无响应 → nginx 180s 后 504。
# 对策：小请求保持原逻辑（reasoning 回填 + 精确记账 + 对话入库）；
#      大请求改走「原始字节直通」——不解析、不重序列化、不入库，
#      token 记账用上游返回的 usage（已实证上游稳定返回，见 CHANGELOG 0.6.30）。
MAX_REQUEST_MB = int(_cfg("max_request_mb", "MAX_REQUEST_MB", 64))
STATELESS_BODY_MB = int(_cfg("stateless_body_mb", "STATELESS_BODY_MB", 4))
TOKENIZE_MAX_BYTES = int(_cfg("tokenize_max_bytes", "TOKENIZE_MAX_BYTES", 64 * 1024))
CONV_CONTENT_MAX_BYTES = int(_cfg("conv_content_max_bytes", "CONV_CONTENT_MAX_BYTES", 256 * 1024))
LARGE_BODY_CONCURRENCY = int(_cfg("large_body_concurrency", "LARGE_BODY_CONCURRENCY", 4))
HEALTH_PORT = int(_cfg("health_port", "HEALTH_PORT", 6771))

app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_MB * 1024 * 1024
_STATELESS_BODY_BYTES = STATELESS_BODY_MB * 1024 * 1024
# 大请求并发闸门：把同时在途的大请求数钉死，峰值内存 ≈ N × body 大小
_large_body_sem = threading.BoundedSemaphore(max(1, LARGE_BODY_CONCURRENCY))


@app.errorhandler(413)
def _payload_too_large(_e):
    mb = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
    return jsonify({"error": f"request body too large (limit {mb}MB)"}), 413


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

# 单一数据库文件：对话内容与用量统一存储，方便备份/同步
# 旧版本（≤0.6.27）的 conversations.db 独立文件，在 init_db 中自动合并迁移
CONV_DB_PATH = DB_PATH
_legacy_conv_path = os.path.expandvars(_cfg("conv_db_path", "CONV_DB_PATH", os.path.join(DATA_DIR, "conversations.db")))
LEGACY_CONV_DB_PATH = os.path.join(EXE_DIR, _legacy_conv_path) if not os.path.isabs(_legacy_conv_path) else _legacy_conv_path

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
    # 历史数据可能含无效 UTF-8 字节，宽松解码避免读取报错
    conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def ensure_conv_schema(conn):
    """新结构：conversations 精简 + 内容去重表（system_prompts / message_contents / messages）。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(conversations)")]
    if cols and "messages" in cols:
        raise RuntimeError(
            "conversations.db 仍是旧结构（含 messages 大字段）。请先运行 python scripts/migrate_conv_db.py 迁移后再启动。"
        )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY,
            model TEXT NOT NULL,
            provider TEXT NOT NULL,
            endpoint TEXT,
            request_time DATETIME DEFAULT (datetime('now', 'localtime')),
            system_prompt_id INTEGER,
            last_user_message TEXT,
            response TEXT NOT NULL DEFAULT '',
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            duration_ms INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS system_prompts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash TEXT NOT NULL UNIQUE,
            content TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS message_contents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash TEXT NOT NULL,
            extra_hash TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            extra TEXT NOT NULL DEFAULT '',
            is_list INTEGER NOT NULL DEFAULT 0,
            UNIQUE(content_hash, extra_hash)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            role TEXT NOT NULL,
            content_id INTEGER NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_time ON conversations(request_time)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, seq)")
    conn.commit()

def _merge_legacy_conv_db():
    """将旧版独立 conversations.db 合并进单库（不删除旧库，幂等）。"""
    if not os.path.exists(LEGACY_CONV_DB_PATH):
        return
    if os.path.abspath(LEGACY_CONV_DB_PATH) == os.path.abspath(DB_PATH):
        return
    try:
        legacy = sqlite3.connect(LEGACY_CONV_DB_PATH, timeout=30)
        legacy.row_factory = sqlite3.Row
        legacy_cols = [r[1] for r in legacy.execute("PRAGMA table_info(conversations)")]
        if "messages" in legacy_cols:
            print("旧 conversations.db 仍是未迁移的旧结构（含 messages 大字段），跳过自动合并。")
            print("请先运行 python scripts/migrate_conv_db.py 迁移后再启动。")
            legacy.close()
            return
        n = legacy.execute("SELECT COUNT(*) as n FROM conversations").fetchone()["n"]
        legacy.close()
        if n == 0:
            return

        dst = sqlite3.connect(DB_PATH, timeout=30)
        try:
            dst.execute("PRAGMA journal_mode=WAL")
            ensure_conv_schema(dst)
            has = dst.execute("SELECT COUNT(*) as n FROM conversations").fetchone()[0]
            if has > 0:
                print("conversations 表已有数据，跳过合并")
                return
            dst.execute("ATTACH DATABASE ? AS legacy", (LEGACY_CONV_DB_PATH,))
            dst.execute("INSERT INTO conversations SELECT * FROM legacy.conversations")
            dst.execute("INSERT INTO system_prompts SELECT * FROM legacy.system_prompts")
            dst.execute("INSERT INTO message_contents SELECT * FROM legacy.message_contents")
            dst.execute("INSERT INTO messages SELECT * FROM legacy.messages")
            dst.execute("CREATE INDEX IF NOT EXISTS idx_conv_time ON conversations(request_time)")
            dst.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, seq)")
            dst.commit()
            print(f"已合并旧 conversations.db（{n} 条会话）到单库 {DB_PATH}，旧库保留未删除")
            try:
                dst.execute("VACUUM")
                dst.commit()
                print("合并后 VACUUM 完成")
            except Exception as e:
                print(f"合并后 VACUUM 失败（将在下次启动重试）: {e}")
        finally:
            try:
                dst.execute("DETACH DATABASE legacy")
            except Exception:
                pass
            dst.close()
    except Exception as e:
        print(f"合并旧 conversations.db 失败: {e}")

def init_db():
    conn = get_db()
    ensure_schema(conn)
    conn.close()
    conn = get_conv_db()
    ensure_conv_schema(conn)
    conn.close()
    # 旧版本（≤0.6.27）conversations 独立文件：启动时自动合并进单库
    _merge_legacy_conv_db()
    # 迁移脚本在服务运行中无法 VACUUM（需独占锁），留下标记；启动时独占连接压缩
    marker = CONV_DB_PATH + ".needs_vacuum"
    if os.path.exists(marker):
        try:
            print("检测到 VACUUM 标记，压缩 conversations.db ...")
            conn = sqlite3.connect(CONV_DB_PATH, timeout=60)
            conn.execute("VACUUM")
            conn.commit()
            conn.close()
            os.remove(marker)
            print("VACUUM 完成")
        except Exception as e:
            print(f"启动 VACUUM 失败: {e}")

# ── 对话内容去重与重建 ────────────────────────────────────────

def _content_hash(content, is_list):
    """内容哈希，与 scripts/migrate_conv_db.py 保持一致（str/list 前缀区分）。"""
    if is_list:
        raw = "l:" + json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    else:
        raw = "s:" + str(content)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def _get_or_insert_system_prompt(conn, sp_text):
    if not sp_text:
        return None
    h = _content_hash(sp_text, False)
    row = conn.execute("SELECT id FROM system_prompts WHERE content_hash = ?", (h,)).fetchone()
    if row:
        return row[0]
    conn.execute("INSERT OR IGNORE INTO system_prompts (content_hash, content) VALUES (?, ?)", (h, sp_text))
    return conn.execute("SELECT id FROM system_prompts WHERE content_hash = ?", (h,)).fetchone()[0]

def _get_or_insert_message_content(conn, content, is_list, extra):
    h = _content_hash(content, is_list)
    eh = _content_hash(extra, False) if extra else ""
    row = conn.execute(
        "SELECT id FROM message_contents WHERE content_hash = ? AND extra_hash = ?", (h, eh)
    ).fetchone()
    if row:
        return row[0]
    stored = json.dumps(content, ensure_ascii=False) if is_list else content
    conn.execute(
        "INSERT OR IGNORE INTO message_contents (content_hash, extra_hash, content, extra, is_list) VALUES (?, ?, ?, ?, ?)",
        (h, eh, stored, extra, 1 if is_list else 0),
    )
    return conn.execute(
        "SELECT id FROM message_contents WHERE content_hash = ? AND extra_hash = ?", (h, eh)
    ).fetchone()[0]

def _rebuild_messages(conn, conv_id):
    """从 messages + message_contents 重建原始 messages 数组。"""
    rows = conn.execute(
        """SELECT m.seq, m.role, mc.content, mc.extra, mc.is_list FROM messages m
           JOIN message_contents mc ON m.content_id = mc.id
           WHERE m.conversation_id = ? ORDER BY m.seq""",
        (conv_id,),
    ).fetchall()
    out = []
    for seq, role, content, extra, is_list in rows:
        msg = {"role": role}
        if is_list:
            try:
                msg["content"] = json.loads(content)
            except Exception:
                msg["content"] = []
        else:
            msg["content"] = content
        if extra:
            try:
                msg.update(json.loads(extra))
            except Exception:
                pass
        out.append(msg)
    return out

def _conv_to_dict(conn, row):
    d = dict(row)
    try:
        d["messages"] = _rebuild_messages(conn, d["id"])
    except Exception:
        d["messages"] = []
    return d

def _conv_to_dict_batch(conn, rows):
    """批量重建多条会话的 messages + system_prompt，避免 N+1 查询。"""
    items = []
    if not rows:
        return items
    conv_ids = [r["id"] for r in rows]
    id_ph = ",".join("?" * len(conv_ids))

    msgs_map = {cid: [] for cid in conv_ids}
    msg_rows = conn.execute(
        f"""SELECT m.conversation_id, m.seq, m.role, mc.content, mc.extra, mc.is_list
            FROM messages m JOIN message_contents mc ON m.content_id = mc.id
            WHERE m.conversation_id IN ({id_ph}) ORDER BY m.conversation_id, m.seq""",
        conv_ids,
    ).fetchall()
    for cid, seq, role, content, extra, is_list in msg_rows:
        msg = {"role": role}
        if is_list:
            try:
                msg["content"] = json.loads(content)
            except Exception:
                msg["content"] = []
        else:
            msg["content"] = content
        if extra:
            try:
                msg.update(json.loads(extra))
            except Exception:
                pass
        msgs_map[cid].append(msg)

    sp_ids = set(r["system_prompt_id"] for r in rows if r["system_prompt_id"])
    sp_map = {}
    if sp_ids:
        sp_ph = ",".join("?" * len(sp_ids))
        for sid, content in conn.execute(
            f"SELECT id, content FROM system_prompts WHERE id IN ({sp_ph})", list(sp_ids)
        ).fetchall():
            sp_map[sid] = content

    for r in rows:
        d = dict(r)
        d["messages"] = msgs_map.get(r["id"], [])
        sp_id = d.get("system_prompt_id")
        d["system_prompt"] = sp_map.get(sp_id, "") if sp_id else ""
        items.append(d)
    return items

# ── 入库内容收缩（图片 / base64 / 超长文本）────────────────────
# 对话库只存「有效对话」文本：内联图片、附件、base64 大块对回看没有价值，
# 却让 usage.db 无界膨胀（2026-10-22 已达 2.8GB / 磁盘 78%）。
# 收缩只作用于入库副本，转发给上游的内容完全不受影响。
_MAX_INLINE_BINARY = 4096
_BIN_PROMPT = "[binary omitted]"
_BASE64_BLOB_RE = re.compile(r"[A-Za-z0-9+/]{4096,}={0,2}")
# 只剔二进制媒体（image/audio/video/application）的内联 data-uri；
# text/* 的 data-uri 属于文本内容，保留（超长时仍会被截断）。
_DATA_URI_RE = re.compile(r"data:((?:image|audio|video|application)/[\w.+-]+);base64,[A-Za-z0-9+/=]*")
_BINARY_KEYS = ("data", "base64", "b64_json")


def _shrink_text(text):
    """入库副本：剔除 data-uri / 裸 base64 大块，超长则截断并标注原始长度。"""
    if not isinstance(text, str) or not text:
        return text
    omitted = 0
    if len(text) > CONV_CONTENT_MAX_BYTES:
        omitted += len(text) - CONV_CONTENT_MAX_BYTES
        text = text[:CONV_CONTENT_MAX_BYTES]
    before = len(text)
    text = _DATA_URI_RE.sub(lambda m: f"[{m.group(1)} omitted]", text)
    text = _BASE64_BLOB_RE.sub("[base64 omitted]", text)
    omitted += before - len(text)
    if omitted:
        text = f"{text}\n...[omitted {omitted} bytes]"
    return text


def _shrink_obj(obj):
    """递归收缩待入库结构：二进制大字段换占位符，超长文本截断。"""
    if isinstance(obj, str):
        return _shrink_text(obj)
    if isinstance(obj, list):
        return [_shrink_obj(x) for x in obj]
    if not isinstance(obj, dict):
        return obj
    out = {}
    for k, v in obj.items():
        if k in _BINARY_KEYS and isinstance(v, str) and len(v) > _MAX_INLINE_BINARY:
            out[k] = _BIN_PROMPT
        elif k == "image_url" and isinstance(v, str) and len(v) > _MAX_INLINE_BINARY:
            out[k] = _BIN_PROMPT
        elif k == "image_url" and isinstance(v, dict) and isinstance(v.get("url"), str) and len(v["url"]) > _MAX_INLINE_BINARY:
            out[k] = {**v, "url": _BIN_PROMPT}
        elif k == "source" and isinstance(v, dict) and isinstance(v.get("data"), str) and len(v["data"]) > _MAX_INLINE_BINARY:
            out[k] = {**v, "data": _BIN_PROMPT}
        else:
            out[k] = _shrink_obj(v)
    return out


def _msg_content(content):
    """content 归一化为 (value, is_list)，与迁移脚本一致。"""
    if isinstance(content, list):
        return content, True
    return str(content or ""), False

# ── tokenizer ──────────────────────────────────────────────────

DEEPSEEK_TOKENIZER_DIR = os.path.join(RESOURCE_DIR, "tokenizers", "deepseek")
_deepseek_tokenizer = None

# 本地 transformers tokenizer 开关，默认关闭。
# 加载 transformers + DeepSeek 词表会常驻数百 MB 内存，在小内存云主机上极易触发 OOM
# （现象是整机假死：TCP 能握手但应用不响应）。token 计数以请求完成后上游返回的
# usage 为准，本地计数只在缺失时兜底，轻量估算完全够用，不值得为记账引入几百 MB 常驻开销。
# 确需精确计数时设置环境变量 ENABLE_DEEPSEEK_TOKENIZER=1。
ENABLE_DEEPSEEK_TOKENIZER = os.environ.get("ENABLE_DEEPSEEK_TOKENIZER", "0") == "1"

_tiktoken_encoder = None


def _get_tiktoken_encoder():
    """cl100k_base 编码表单例：只构建一次，避免重复加载词表。"""
    global _tiktoken_encoder
    if _tiktoken_encoder is None:
        import tiktoken
        _tiktoken_encoder = tiktoken.get_encoding("cl100k_base")
    return _tiktoken_encoder


def get_deepseek_tokenizer():
    global _deepseek_tokenizer
    if _deepseek_tokenizer is not None:
        return _deepseek_tokenizer if _deepseek_tokenizer else None
    if not ENABLE_DEEPSEEK_TOKENIZER:
        _deepseek_tokenizer = False
        return None
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
    # tiktoken 的 BPE 预分词对「超长无空白 run」会退化成 O(n²)：实测连续同字符
    # 64KB 需 1.5s、256KB 需 45.7s、1MB 约 11 分钟（minified JS / base64 / 长 hash
    # 都是这种形状）。计数只用于「上游没返回 usage」时兜底，不值得付出分钟级 CPU
    # 占死 worker 线程，超阈值直接按 4 字节/token 估算。
    if len(text) > TOKENIZE_MAX_BYTES:
        return max(1, len(text) // 4)
    try:
        import tiktoken
        try:
            enc = tiktoken.encoding_for_model(model)
        except KeyError:
            enc = _get_tiktoken_encoder()
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
        if isinstance(data.get("output"), list):
            texts = []
            for item in data["output"]:
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") in ("output_text", "text", "reasoning_text"):
                        texts.append(part.get("text", ""))
            return " ".join(t for t in texts if t)
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
    - Responses API: usage.input_tokens_details.cached_tokens
    - Anthropic 原生: usage.cache_read_input_tokens
    - fangna 中转扩展: usage.prompt_cache_hit_tokens
    """
    if not isinstance(usage, dict):
        return 0
    candidates = []
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
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
        system_prompt = _shrink_text(system_prompt)
        last_user = _shrink_text(last_user)
        conn = get_conv_db()
        sp_id = _get_or_insert_system_prompt(conn, system_prompt)
        cur = conn.execute(
            "INSERT INTO conversations (model, provider, endpoint, system_prompt_id, last_user_message, response, prompt_tokens, completion_tokens, total_tokens, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                model, provider, endpoint,
                sp_id, last_user,
                _shrink_text(response or ""),
                prompt_tokens, completion_tokens, total_tokens, duration_ms,
            ),
        )
        conv_id = cur.lastrowid
        msg_rows = []
        for seq, m in enumerate(messages or []):
            if not isinstance(m, dict):
                continue
            role = str(m.get("role", "unknown"))
            content, is_list = _msg_content(_shrink_obj(m.get("content")))
            extra = {k: v for k, v in m.items() if k not in ("role", "content")}
            extra = _shrink_obj(extra) if extra else {}
            extra_str = json.dumps(extra, ensure_ascii=False) if extra else ""
            content_id = _get_or_insert_message_content(conn, content, is_list, extra_str)
            msg_rows.append((conv_id, seq, role, content_id))
        if msg_rows:
            conn.executemany(
                "INSERT INTO messages (conversation_id, seq, role, content_id) VALUES (?, ?, ?, ?)",
                msg_rows,
            )
        conn.commit()
        conn.close()
        return conv_id
    except Exception as e:
        print(f"  CONV LOG FAIL: {e}")
        return None

def _extract_input_messages(input_data):
    """从 Responses API 请求体的 input 中提取 messages 结构（用于记账/对话记录）。"""
    messages = []
    if isinstance(input_data, str):
        if input_data.strip():
            messages.append({"role": "user", "content": input_data})
        return messages
    if isinstance(input_data, list):
        for item in input_data:
            if isinstance(item, dict) and item.get("type") == "message":
                role = item.get("role", "user")
                content = item.get("content")
                messages.append({"role": role, "content": content})
    return messages


def _handle_stream(target_url, headers, body_data, model, provider_label, prompt_tokens, raw_body=None, messages=None, is_responses=False):
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
    reasoning_acc = ""
    pure_content = ""
    tool_call_ids = {}
    usage_data = None
    path = request.path
    start_time = datetime.now()
    state = {"end_time": None}

    def generate():
        nonlocal complete_content, usage_data, reasoning_acc, pure_content
        resp.encoding = "utf-8"
        buf = []
        try:
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
                            if is_responses:
                                ctype = chunk.get("type")
                                if ctype == "response.output_text.delta":
                                    delta_text = chunk.get("delta") or ""
                                    complete_content += delta_text
                                    pure_content += delta_text
                                elif ctype in ("response.completed", "response.incomplete"):
                                    resp_obj = chunk.get("response") or {}
                                    if resp_obj.get("usage"):
                                        usage_data = resp_obj["usage"]
                            elif provider_label == "anthropic":
                                if chunk.get("type") == "content_block_delta":
                                    delta_text = chunk.get("delta", {}).get("text") or ""
                                    complete_content += delta_text
                                    pure_content += delta_text
                                elif chunk.get("type") == "message_delta":
                                    usage_data = chunk.get("usage", {})
                            else:
                                choices = chunk.get("choices", [])
                                if choices and len(choices) > 0:
                                    delta = choices[0].get("delta", {})
                                    msg = choices[0].get("message", {})
                                    # 真实正文与思维链分开累计：正文用于缓存指纹，
                                    # 思维链单独留存以便下一轮回填
                                    real = delta.get("content") or msg.get("content")
                                    if real:
                                        complete_content += real
                                        pure_content += real
                                    think = delta.get("reasoning_content")
                                    if think:
                                        reasoning_acc += think
                                        complete_content += think
                                    for tc in (delta.get("tool_calls") or msg.get("tool_calls") or []):
                                        if isinstance(tc, dict):
                                            idx = tc.get("index", 0)
                                            if tc.get("id"):
                                                tool_call_ids[idx] = tc["id"]
                                if chunk.get("usage"):
                                    usage_data = chunk["usage"]
                        except json.JSONDecodeError:
                            pass
                    continue
                buf.append(raw_line)
            if buf:
                yield "\n".join(buf) + "\n\n"
        finally:
            # 流结束（或客户端提前断开）即写入缓存，保证下一轮能回填。
            # 不依赖 call_on_close —— 它在部分运行环境/测试客户端下不触发。
            state["end_time"] = datetime.now()
            _reasoning_cache_put(
                [tool_call_ids[k] for k in sorted(tool_call_ids)],
                pure_content,
                reasoning_acc,
            )
        yield "data: [DONE]\n\n"

    def do_log():
        if usage_data:
            pt = usage_data.get("prompt_tokens", 0) or usage_data.get("input_tokens", 0) or prompt_tokens
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


# ── reasoning_content 回填（DeepSeek 思考模式 + tools 的 400 修复）──────────
# 背景：DeepSeek 思考模式在请求携带 tools 时，要求历史中 assistant 消息的
# reasoning_content 原样回传（含未发生工具调用的轮次），否则上游返回
#   400 invalid_request_error:
#   The `reasoning_content` in the thinking mode must be passed back to the API.
# 但 reasoning_content 不属于 OpenAI 标准 schema，客户端（WorkBuddy / opencode /
# Cursor 等）重建消息历史时普遍只保留 role / content / tool_calls，该字段被丢弃，
# 于是第一轮工具调用之后的每个请求都 400。
# 网关是整条链路中唯一同时看到「上游响应」与「下一轮请求」的位置，因此在此
# 响应侧缓存、请求侧回填。缓存以 tool_call id 为首选键（唯一且稳定），
# 以 assistant content 指纹为兜底键（无 tool_calls 的轮次）。
# 关闭开关：ENABLE_REASONING_REPLAY=0
ENABLE_REASONING_REPLAY = os.environ.get("ENABLE_REASONING_REPLAY", "1") != "0"
_REASONING_CACHE_MAX = int(os.environ.get("REASONING_CACHE_MAX", "4096"))

# tool_call id -> reasoning_content（首选键）
_reasoning_by_toolcall = OrderedDict()
# assistant content 的 sha1 -> reasoning_content（兜底键）
_reasoning_by_content = OrderedDict()
# waitress 多线程并发读写，LRU 的 move_to_end/popitem 非原子，必须加锁，
# 否则并发淘汰时可能抛 KeyError 把请求打成 500
_reasoning_lock = threading.Lock()


def _reasoning_cache_put(toolcall_ids, content, reasoning):
    """缓存一轮 assistant 的 reasoning_content，按 tool_call id 与内容指纹双键写入。"""
    if not ENABLE_REASONING_REPLAY or not reasoning:
        return
    with _reasoning_lock:
        for tid in toolcall_ids or []:
            if tid:
                _reasoning_by_toolcall[tid] = reasoning
                _reasoning_by_toolcall.move_to_end(tid)
        while len(_reasoning_by_toolcall) > _REASONING_CACHE_MAX:
            _reasoning_by_toolcall.popitem(last=False)
        if content:
            fp = hashlib.sha1(content.encode("utf-8")).hexdigest()
            _reasoning_by_content[fp] = reasoning
            _reasoning_by_content.move_to_end(fp)
            while len(_reasoning_by_content) > _REASONING_CACHE_MAX:
                _reasoning_by_content.popitem(last=False)


def _reasoning_cache_get(toolcall_ids, content):
    """按 tool_call id 优先、内容指纹兜底的顺序取回 reasoning_content。"""
    if not ENABLE_REASONING_REPLAY:
        return None
    with _reasoning_lock:
        for tid in toolcall_ids or []:
            if tid and tid in _reasoning_by_toolcall:
                _reasoning_by_toolcall.move_to_end(tid)
                return _reasoning_by_toolcall[tid]
        if content:
            fp = hashlib.sha1(content.encode("utf-8")).hexdigest()
            if fp in _reasoning_by_content:
                _reasoning_by_content.move_to_end(fp)
                return _reasoning_by_content[fp]
    return None


def _replay_reasoning(body_dict: dict) -> dict:
    """给历史中缺失 reasoning_content 的 assistant 消息回填缓存值。

    只在请求携带 tools（DeepSeek 契约的触发条件）时处理；已带
    reasoning_content 的消息原样保留，绝不覆盖客户端自带的值。
    """
    if not ENABLE_REASONING_REPLAY or not body_dict.get("tools"):
        return body_dict
    messages = body_dict.get("messages")
    if not isinstance(messages, list):
        return body_dict
    patched = 0
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        if m.get("reasoning_content"):
            continue
        tool_ids = [tc.get("id") for tc in (m.get("tool_calls") or []) if isinstance(tc, dict)]
        content = m.get("content")
        text = content if isinstance(content, str) else _msg_text(content)
        cached = _reasoning_cache_get(tool_ids, text)
        if cached:
            m["reasoning_content"] = cached
            patched += 1
    if patched:
        print(f"  REASONING REPLAY: patched {patched} assistant message(s)")
    return body_dict


# 通用 HTTP 库/SDK 的 User-Agent 前缀：OpenCode Go 等上游要求 coding-agent 自有身份，需替换
_GENERIC_UA_PREFIXES = (
    "python-requests", "python-httpx", "curl", "wget", "go-http-client",
    "node", "undici", "axios", "okhttp", "java/", "apache-httpclient", "ai-sdk",
)

# 日志脱敏：这些头原样打印会把 API key 落到日志文件里
_SENSITIVE_HEADERS = {"authorization", "x-api-key", "api-key", "cookie", "proxy-authorization"}


def _mask_headers(headers) -> dict:
    """返回脱敏后的请求头副本，供日志打印使用。"""
    masked = {}
    for k, v in headers.items():
        if k.lower() in _SENSITIVE_HEADERS:
            v = (v[:12] + "...") if isinstance(v, str) and len(v) > 12 else "***"
        masked[k] = v
    return masked


def _upstream_headers(body, model):
    """构造上游请求头：透传 Authorization / User-Agent / x-opencode-session，
    并补齐 OpenCode Go 端点（opencode.ai/zen/go/v1）的强制要求：
    - x-opencode-session 会话头（缺失时上游返回 400 MissingSessionID），
      客户端未携带时从会话首条消息派生稳定 ID（同一会话内不变，利于上游路由与提示缓存）
    - coding-agent 风格 User-Agent（通用库 UA 替换为网关自身标识）
    """
    headers = {"Content-Type": "application/json"}
    for h in ("Authorization", "User-Agent", "x-opencode-session"):
        v = request.headers.get(h)
        if v:
            headers[h] = v
    ua = headers.get("User-Agent", "")
    if not ua or ua.lower().startswith(_GENERIC_UA_PREFIXES):
        headers["User-Agent"] = f"ai-gateway/{VERSION}"
    if not headers.get("x-opencode-session"):
        seed = model or ""
        msgs = body.get("messages") or []
        seed_source = msgs[0] if msgs else body.get("input")
        if seed_source is not None:
            seed += "|" + json.dumps(seed_source, ensure_ascii=False, sort_keys=True)
        headers["x-opencode-session"] = "gw-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    return headers


def _proxy_passthrough(upstream_url, body, model, stream):
    headers = _upstream_headers(body, model)

    print(f"  INCOMING HEADERS: {_mask_headers(request.headers)}")
    body = _clean_messages(body)
    body = _replay_reasoning(body)
    model = body.get("model", "")
    hostname = upstream_url.replace("https://", "").replace("http://", "").split("/")[0]
    provider_label = hostname.split(":")[0]

    is_responses = "messages" not in body and "input" in body
    if is_responses:
        messages = _extract_input_messages(body.get("input"))
    else:
        messages = body.get("messages", [])
    prompt_tokens = count_messages_tokens(model, messages)
    raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")

    if stream:
        return _handle_stream(upstream_url, headers, body, model, provider_label, prompt_tokens, raw_body, messages, is_responses=is_responses)

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
    # 缓存本轮 reasoning_content，供下一轮请求回填（DeepSeek 思考模式契约）
    try:
        _msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
        _reasoning_cache_put(
            [tc.get("id") for tc in (_msg.get("tool_calls") or []) if isinstance(tc, dict)],
            _msg.get("content"),
            _msg.get("reasoning_content"),
        )
    except Exception:
        pass
    usage = data.get("usage", {})
    if usage.get("prompt_tokens"):
        prompt_tokens = usage["prompt_tokens"]
    elif usage.get("input_tokens"):
        prompt_tokens = usage["input_tokens"]
    content = _extract_content(data)
    completion_tokens = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0) or count_tokens(model, content)
    cached_tokens = _extract_cached_tokens(usage)
    reasoning_tokens = _extract_reasoning_tokens(usage)
    if usage:
        print(f"  LOG: upstream usage: {json.dumps(usage, ensure_ascii=False)}")
    duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
    conv_id = _log_conversation(model, provider_label, request.path, messages, content, prompt_tokens, completion_tokens, prompt_tokens + completion_tokens, duration_ms)
    _log_usage(model, provider_label, prompt_tokens, completion_tokens, prompt_tokens + completion_tokens, request.path, duration_ms, conv_id, cached_tokens, reasoning_tokens)
    return jsonify(data)


# ── 大请求体：无状态直通 ──────────────────────────────────────
# 目标：≥ STATELESS_BODY_MB 的请求不再进入「解析成对象 + 重新序列化」路径。
# 只做三件事：透传原始字节、从响应里抓 usage 记账、把响应原样流回客户端。
# 代价：大请求不做 reasoning_content 回填、不入对话库（两者都需要解析后的
# messages，而那正是把 body 放大 5.9 倍、进而拖死全站的根因）。
# 正常 opencode 会话请求体 0.4~0.8MB，远低于阈值，行为完全不变。
_MODEL_RE = re.compile(rb'"model"\s*:\s*"([^"\\]{1,200})"')


def _scan_body_meta(raw):
    """从前 8KB 扫出 model，避免解析整个大 body。

    ai-sdk / opencode 生成的 payload 顶层 model 位于最前面，前缀扫描足够；
    扫不到就留空，后续用响应里的 model 补。
    """
    m = _MODEL_RE.search(raw[:8192])
    return m.group(1).decode("utf-8", "replace") if m else ""


def _upstream_headers_raw(model, raw):
    """大请求头部构造：规则同 _upstream_headers，但会话 ID 改用 body 头部指纹。"""
    headers = {"Content-Type": "application/json"}
    for h in ("Authorization", "User-Agent", "x-opencode-session"):
        v = request.headers.get(h)
        if v:
            headers[h] = v
    ua = headers.get("User-Agent", "")
    if not ua or ua.lower().startswith(_GENERIC_UA_PREFIXES):
        headers["User-Agent"] = f"ai-gateway/{VERSION}"
    if not headers.get("x-opencode-session"):
        # 无法解析 messages[0]，改用 body 头部指纹：同一会话历史只追加、不改头部，
        # 指纹在会话内稳定，仍能满足上游的会话路由与提示缓存。
        fp = hashlib.sha1(raw[:4096]).hexdigest()
        headers["x-opencode-session"] = "gw-" + hashlib.sha1(f"{model}|{fp}".encode()).hexdigest()[:16]
    return headers


def _usage_of(chunk):
    """从单个 chunk 提取 usage，兼容 OpenAI / Responses / Anthropic。"""
    if not isinstance(chunk, dict):
        return None
    if isinstance(chunk.get("usage"), dict):
        return chunk["usage"]
    inner = chunk.get("response")
    if isinstance(inner, dict) and isinstance(inner.get("usage"), dict):
        return inner["usage"]
    return None


def _proxy_stateless(target_url, raw):
    """大请求体直通：原始字节发给上游，响应原样流回，仅抓 usage 记账。"""
    model = _scan_body_meta(raw)
    headers = _upstream_headers_raw(model, raw)
    hostname = target_url.replace("https://", "").replace("http://", "").split("/")[0]
    provider_label = hostname.split(":")[0]
    path = request.path
    start_time = datetime.now()
    state = {"usage": None, "model": model, "out_chars": 0}

    # 并发闸门：把同时在途的大请求数钉死，峰值内存 ≈ N × body 大小。
    # 只在「超大 body」路径生效，正常请求不受影响。
    if not _large_body_sem.acquire(timeout=120):
        return jsonify({"error": "gateway busy: too many large requests in flight, retry later"}), 503
    released = threading.Event()

    def _release():
        if not released.is_set():
            released.set()
            _large_body_sem.release()

    try:
        resp = requests.post(target_url, headers=headers, data=raw, stream=True, timeout=300, proxies=PROXIES)
    except requests.RequestException as e:
        _release()
        err = f"UPSTREAM REQUEST FAILED: {type(e).__name__}: {e}"
        print(f"  {err}")
        print(f"  TARGET: {target_url}")
        return jsonify({"error": err}), 502
    except BaseException:
        _release()
        raise

    if not resp.ok:
        _release()
        try:
            err = resp.text[:500] if resp.text else "(empty body)"
        except Exception:
            err = "(unreadable body)"
        print(f"  UPSTREAM {resp.status_code} (stateless, {len(raw)}B body): {err}")
        return jsonify({"error": err}), resp.status_code

    ctype = resp.headers.get("content-type", "application/json")
    is_sse = "event-stream" in ctype

    def _capture(line):
        if not line or not line.startswith("data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return
        try:
            chunk = json.loads(payload)
        except ValueError:
            return
        if isinstance(chunk, dict) and chunk.get("model") and not state["model"]:
            state["model"] = chunk["model"]
        usage = _usage_of(chunk)
        if usage:
            state["usage"] = usage

    def generate():
        try:
            if is_sse:
                for line in resp.iter_lines(decode_unicode=True):
                    line = line or ""
                    state["out_chars"] += len(line)
                    _capture(line)
                    yield line + "\n"
            else:
                data = resp.content
                try:
                    obj = json.loads(data)
                    if isinstance(obj, dict):
                        if obj.get("model"):
                            state["model"] = obj["model"]
                        state["usage"] = _usage_of(obj) or state["usage"]
                        state["out_chars"] += len(data)
                except ValueError:
                    pass
                yield data
        finally:
            duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            usage = state["usage"] or {}
            if usage:
                pt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
                ct = usage.get("completion_tokens") or usage.get("output_tokens") or 0
                print(f"  LOG: upstream usage: {json.dumps(usage, ensure_ascii=False)}")
            else:
                # 上游没给 usage 时不再伪造 prompt 数，只在日志里显式暴露，便于发现契约变化
                pt, ct = 0, state["out_chars"] // 4
                print(f"  STATELESS WARN: no usage in upstream response ({len(raw)}B body)")
            cached = _extract_cached_tokens(usage)
            reasoning = _extract_reasoning_tokens(usage)
            print(f"  STATELESS: body={len(raw)}B stream={is_sse} model={state['model'] or '(unknown)'}")
            _log_usage(state["model"], provider_label, pt, ct, pt + ct, path, duration_ms, None, cached, reasoning)
            try:
                resp.close()
            except Exception:
                pass
            # 必须在生成器 finally 里放行：call_on_close 在部分运行环境/测试客户端下
            # 不触发，只靠它会漏放信号量，几次大请求后全部 503。
            _release()

    response = Response(stream_with_context(generate()), content_type=ctype)
    response.call_on_close(_release)
    return response


def _dispatch_proxy(by_suffix=""):
    """两个代理入口的公共逻辑。

    小请求走原路径（解析 → 清理/replay → 精确记账 → 对话入库）；
    大请求走无状态直通。by_suffix 仅在通过 ?by= 指定上游时追加
    （/v1/chat/completions 需要补 /chat/completions，/v1 原样透传）。
    """
    raw = request.get_data()
    upstream_url = request.args.get("by")
    large = len(raw) >= _STATELESS_BODY_BYTES

    if large:
        model = _scan_body_meta(raw)
        if upstream_url:
            target = upstream_url.rstrip("/") + by_suffix if by_suffix else upstream_url
        else:
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
            target = row["api_base"].rstrip("/")
            if row["api_key"]:
                request.headers = {**request.headers, "Authorization": f"Bearer {row['api_key']}"}
        return _proxy_stateless(target, raw)

    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as e:
        return jsonify({"error": f"invalid JSON body: {e}"}), 400
    if not isinstance(body, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400
    model = body.get("model", "")
    stream = body.get("stream", False)

    if upstream_url:
        target = upstream_url.rstrip("/") + by_suffix if by_suffix else upstream_url
        return _proxy_passthrough(target, body, model, stream)

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


@app.route("/v1/chat/completions", methods=["POST"])
def proxy():
    return _dispatch_proxy("/chat/completions")


@app.route("/v1", methods=["POST"])
def proxy_v1():
    return _dispatch_proxy("")



@app.route("/v1/models", methods=["GET"])
def proxy_models():
    """GET /v1/models?by=<base_url> — 透传上游模型列表（如 opencode.ai/zen/go/v1/models）"""
    upstream_url = request.args.get("by")
    if not upstream_url:
        return jsonify({"error": "?by=<upstream base_url> is required"}), 400
    target = upstream_url.rstrip("/") + "/models"
    headers = {"User-Agent": f"ai-gateway/{VERSION}"}
    for h in ("Authorization", "x-opencode-session"):
        v = request.headers.get(h)
        if v:
            headers[h] = v
    try:
        resp = requests.get(target, headers=headers, timeout=30, proxies=PROXIES)
    except requests.RequestException as e:
        err = f"UPSTREAM REQUEST FAILED: {type(e).__name__}: {e}"
        print(f"  {err}")
        print(f"  TARGET: {target}")
        return jsonify({"error": err}), 502
    return Response(resp.content, status=resp.status_code,
                    content_type=resp.headers.get("content-type", "application/json"))


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
    items = _conv_to_dict_batch(conn, rows)
    conn.close()

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
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404
    d = _conv_to_dict_batch(conn, [row])[0]
    conn.close()
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
    items = _conv_to_dict_batch(conn, rows)
    conn.close()
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


@app.route("/api/health")
def api_health():
    """轻量健康检查：不查库、不解析 body，供部署校验与人工探活使用。"""
    return jsonify({
        "ok": True,
        "version": VERSION,
        "threads": threading.active_count(),
        "stateless_threshold_mb": STATELESS_BODY_MB,
    })


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

        src = sqlite3.connect(tmp_path)
        try:
            src.execute("PRAGMA journal_mode=WAL")
            ensure_schema(src)
            ensure_conv_schema(src)
            integrity = src.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                return jsonify({"error": f"导入失败：文件完整性校验未通过（{integrity}）"}), 400
            src.commit()
        except sqlite3.DatabaseError:
            return jsonify({"error": "导入失败：文件不是有效的 SQLite 数据库"}), 400
        finally:
            src.close()

        if os.path.exists(target_path):
            _src = sqlite3.connect(target_path, timeout=15)
            try:
                _dst = sqlite3.connect(target_path + ".bak")
                try:
                    _src.backup(_dst)
                finally:
                    _dst.close()
            finally:
                _src.close()

        dst = sqlite3.connect(target_path, timeout=15)
        try:
            _src = sqlite3.connect(tmp_path)
            try:
                _src.backup(dst)
            finally:
                _src.close()
            dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            dst.commit()
        finally:
            dst.close()
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
INSIGHTS_SCHEMA = 2


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
    suggestions = [str(s).strip() for s in (data.get("suggestions") or []) if str(s).strip()][:6]
    if not topics and not summary and not suggestions:
        return None
    return {"summary": summary, "topics": topics, "suggestions": suggestions}


def _llm_classify_insight(model_cfg, label, messages, total_tokens, total_duration_ms=0):
    api_base = model_cfg["api_base"].rstrip("/")
    headers = {"Content-Type": "application/json"}
    if model_cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {model_cfg['api_key']}"
    sys_prompt = (
        "你是 AI 使用记录分析助手。根据用户在指定时间段内发给 AI 的消息，归纳出用户实际在做什么工作。\n"
        "输出必须 ONLY 是合法 JSON（不要任何其他文字），格式：\n"
        '{"summary": "一句话总结该时段用户在做什么", "topics": [{"topic": "主题名", "activities": ["具体活动1", "具体活动2"]}], "suggestions": ["建议1", "建议2"]}\n'
        "要求：\n"
        "1. topic 只能从以下主题中选择：" + "、".join(INSIGHTS_TOPICS) + "。\n"
        "2. 忽略系统提示、指令类（如\"继续\"\"执行\"）以及纯工具操作消息，聚焦用户真实意图。\n"
        "3. activities 每条用简短动词短语概括一项实际工作，尽量具体。\n"
        "4. 没有匹配的主题就不列出，summary 允许留空字符串。\n"
        "5. suggestions 给出 2-4 条针对该时段工作情况的建议（如时间分配、改进方向），简短具体，没有则给空数组。"
    )
    msg_list = "\n".join(f"- {m}" for m in messages)
    user_msg = f"时间段：{label}（{len(messages)} 条去重消息，总消耗约 {total_tokens} tokens，总耗时约 {total_duration_ms / 60000:.0f} 分钟）\n\n用户消息：\n{msg_list}"
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

    cache_key = f"{INSIGHTS_SCHEMA}|{granularity}|{start}|{end}|{model_name}"
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
        "SELECT request_time, last_user_message, total_tokens, duration_ms FROM conversations WHERE request_time BETWEEN ? AND ? ORDER BY request_time ASC",
        (start, end),
    ).fetchall()
    conn.close()

    buckets = {}
    for r in rows:
        key = _insight_bucket_key(r["request_time"], granularity)
        b = buckets.setdefault(key, {"messages": set(), "tokens": 0, "duration": 0})
        msg = _clean_insight_message(r["last_user_message"])
        if msg:
            b["messages"].add(msg)
        b["tokens"] += r["total_tokens"] or 0
        b["duration"] += r["duration_ms"] or 0

    periods = []
    failed = []
    for label in sorted(buckets):
        b = buckets[label]
        msgs = sorted(b["messages"])[:INSIGHTS_MAX_MSGS]
        if not msgs:
            continue
        result = _llm_classify_insight(model_cfg, label, msgs, b["tokens"], b["duration"])
        if result is None:
            failed.append(label)
            continue
        periods.append({
            "period": label,
            "summary": result["summary"],
            "topics": result["topics"],
            "suggestions": result["suggestions"],
            "message_count": len(msgs),
            "total_tokens": b["tokens"],
            "duration_ms": b["duration"],
        })

    total_msgs = sum(p["message_count"] for p in periods)
    total_tokens = sum(p["total_tokens"] for p in periods)
    for p in periods:
        p["message_share"] = round(p["message_count"] * 100.0 / total_msgs, 1) if total_msgs else 0
        p["token_share"] = round(p["total_tokens"] * 100.0 / total_tokens, 1) if total_tokens else 0

    data = {
        "granularity": granularity,
        "periods": periods,
        "failed": failed,
        "truncated": truncated,
        "has_conversations": len(rows) > 0,
        "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "totals": {
            "messages": total_msgs,
            "tokens": total_tokens,
            "duration_ms": sum(p["duration_ms"] for p in periods),
        },
    }
    INSIGHTS_CACHE[cache_key] = {"ts": time.time(), "data": data}
    return jsonify(data)


init_db()


def _start_health_server(port):
    """独立端口的极简健康检查（仅监听 127.0.0.1）。

    单独线程 + 独立端口，不受 waitress 线程池占满影响：线程池全忙（长流式请求）
    时仍能应答；只有进程真正假死（GIL 被 C 扩展独占）才不应答。watchdog 因此
    能区分「忙」和「死」，不会误杀正在跑长请求的进程。
    """
    if not port:
        return
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    try:
        srv = _Server(("127.0.0.1", port), _Handler)
    except OSError as e:
        print(f"  警告：健康检查端口 {port} 启动失败：{e}")
        return
    threading.Thread(target=srv.serve_forever, name="health", daemon=True).start()
    print(f"  health check on http://127.0.0.1:{port}/")


def _serve():
    """启动 HTTP 服务。

    优先使用 waitress（生产级 WSGI 容器，线程池有硬上限，内存可控）；
    未安装时回落到 Flask 内置开发服务器，仅供本地调试。
    Flask 开发服务器每请求新建线程且无上限，并发流式请求下内存会失控，线上不可用。
    """
    _start_health_server(HEALTH_PORT)
    threads = int(os.environ.get("THREADS", "32"))
    try:
        from waitress import serve
    except ImportError:
        print(f"AI Gateway (flask dev server) running on http://0.0.0.0:{PORT}")
        print("  警告：当前为开发服务器，并发与内存均无保障。请安装 waitress：pip install waitress")
        app.run(host="0.0.0.0", port=PORT, threaded=True)
        return
    # channel_timeout 需大于上游最慢的首字节延迟，否则长思考请求会被容器提前掐断
    print(f"AI Gateway (waitress) running on http://0.0.0.0:{PORT} | threads={threads}")
    serve(app, host="0.0.0.0", port=PORT, threads=threads, channel_timeout=300)


if __name__ == "__main__":
    _serve()
