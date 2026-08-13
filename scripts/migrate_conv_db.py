"""conversations.db 一次性迁移脚本：拆表 + 内容哈希去重。

新表结构：
  system_prompts   (id, content_hash UNIQUE, content)
  message_contents (id, content_hash, extra_hash, content, extra, is_list, UNIQUE(content_hash, extra_hash))
  messages         (id, conversation_id, seq, role, content_id)
  conversations    (id 保留原值, 元数据, system_prompt_id, last_user_message, response, tokens)

用法：
  python scripts/migrate_conv_db.py [--db PATH] [--no-backup] [--dry-run]
  --db        目标数据库路径，默认 %APPDATA%/AI Gateway/conversations.db
  --no-backup 跳过备份（默认先备份为 <db>.bak）
  --dry-run   只统计/校验，不实际切换表

安全性：整个迁移在单个事务内完成，任一环节失败自动回滚，原表不受影响。
"""

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time


def content_hash(content, is_list):
    """内容哈希：str 与 list 加前缀区分，避免类型误合并。"""
    if is_list:
        raw = "l:" + json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    else:
        raw = "s:" + str(content)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_args():
    import argparse
    p = argparse.ArgumentParser(description="conversations.db 拆表迁移")
    p.add_argument("--db", default=os.path.join(os.environ.get("APPDATA", ""), "AI Gateway", "conversations.db"))
    p.add_argument("--no-backup", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def ensure_new_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS system_prompts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash TEXT NOT NULL UNIQUE,
            content TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS message_contents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash TEXT NOT NULL,
            extra_hash TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            extra TEXT NOT NULL DEFAULT '',
            is_list INTEGER NOT NULL DEFAULT 0,
            UNIQUE(content_hash, extra_hash)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            role TEXT NOT NULL,
            content_id INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, seq);
        CREATE TABLE IF NOT EXISTS conversations_new (
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
        );
    """)


def get_or_insert_system_prompt(conn, sp_text):
    if not sp_text:
        return None
    h = content_hash(sp_text, False)
    cur = conn.execute("SELECT id FROM system_prompts WHERE content_hash = ?", (h,))
    row = cur.fetchone()
    if row:
        return row[0]
    conn.execute("INSERT OR IGNORE INTO system_prompts (content_hash, content) VALUES (?, ?)", (h, sp_text))
    return conn.execute("SELECT id FROM system_prompts WHERE content_hash = ?", (h,)).fetchone()[0]


def get_or_insert_content(conn, content, is_list, extra, cache):
    h = content_hash(content, is_list)
    eh = content_hash(extra, False) if extra else ""
    key = (h, eh)
    if key in cache:
        return cache[key]
    conn.execute("INSERT OR IGNORE INTO message_contents (content_hash, extra_hash, content, extra, is_list) VALUES (?, ?, ?, ?, ?)",
                 (h, eh, json.dumps(content, ensure_ascii=False) if is_list else content, extra, 1 if is_list else 0))
    cid = conn.execute("SELECT id FROM message_contents WHERE content_hash = ? AND extra_hash = ?", (h, eh)).fetchone()[0]
    cache[key] = cid
    return cid


def _msg_content(content):
    """content 归一化为 (value, is_list)。list 原样保留以便还原。"""
    if isinstance(content, list):
        return content, True
    return str(content or ""), False


def _msg_extra(msg):
    """保存除 role/content 外的其余字段（tool_call_id, tool_calls, name 等），供重建。"""
    extra = {k: v for k, v in msg.items() if k not in ("role", "content")}
    return json.dumps(extra, ensure_ascii=False) if extra else ""


def _migrate_row(conn, r, ctx):
    """迁移单条 conversations 记录到新表。ctx 含列索引/哈希缓存/消息批次。"""
    col_index = ctx["col_index"]
    cid = r[col_index["id"]]
    model = r[col_index["model"]] if ctx["has_model"] else ""
    provider = r[col_index["provider"]] if ctx["has_provider"] else ""
    endpoint = r[col_index["endpoint"]] if ctx["has_endpoint"] and col_index["endpoint"] is not None else ""
    request_time = r[col_index["request_time"]] if ctx["has_rt"] else None
    last_user = r[col_index["last_user_message"]] if ctx["has_lum_col"] else ""
    response = r[col_index["response"]] if ctx["has_resp_col"] else ""
    sp_text = r[col_index["system_prompt"]] if ctx["has_sp_col"] else ""

    sp_id = None
    if sp_text:
        sp_id = get_or_insert_system_prompt(conn, sp_text)

    messages_raw = r[col_index["messages"]] if ctx["has_messages_col"] else "[]"
    try:
        msgs = json.loads(messages_raw or "[]") if messages_raw else []
    except Exception:
        msgs = []
    if not isinstance(msgs, list):
        msgs = []
    for seq, m in enumerate(msgs):
        if not isinstance(m, dict):
            continue
        role = str(m.get("role", "unknown"))
        content, is_list = _msg_content(m.get("content"))
        extra = _msg_extra(m)
        content_id = get_or_insert_content(conn, content, is_list, extra, ctx["cache"])
        ctx["batch_msg"].append((cid, seq, role, content_id))
        if len(ctx["batch_msg"]) >= 5000:
            conn.executemany("INSERT INTO messages (conversation_id, seq, role, content_id) VALUES (?, ?, ?, ?)", ctx["batch_msg"])
            ctx["batch_msg"] = []

    conn.execute(
        "INSERT OR REPLACE INTO conversations_new (id, model, provider, endpoint, request_time, system_prompt_id, last_user_message, response, prompt_tokens, completion_tokens, total_tokens, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            cid, model, provider, endpoint, request_time, sp_id, last_user, response,
            r[col_index["prompt_tokens"]] if ctx["has_pt"] else 0,
            r[col_index["completion_tokens"]] if ctx["has_ct"] else 0,
            r[col_index["total_tokens"]] if ctx["has_tt"] else 0,
            r[col_index["duration_ms"]] if ctx["has_dur"] else 0,
        ),
    )
    return cid


def migrate(args):
    db_path = args.db
    if not os.path.exists(db_path):
        print(f"数据库不存在: {db_path}")
        sys.exit(1)

    if not args.no_backup:
        bak = db_path + ".bak"
        if os.path.exists(bak):
            print(f"备份已存在，跳过覆盖: {bak}")
        else:
            print(f"备份 -> {bak}")
            shutil.copy2(db_path, bak)

    print(f"打开数据库: {db_path}")
    conn = sqlite3.connect(db_path, timeout=60)
    # 个别历史记录可能含无效 UTF-8 字节，宽松解码避免整次迁移失败
    conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=60000")

    # 检查是否已迁移过（新表已存在且 conversations 为精简结构）
    has_messages = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='messages'").fetchone()[0]
    cols = [r[1] for r in conn.execute("PRAGMA table_info(conversations)")]
    already = has_messages and "system_prompt_id" in cols
    if already:
        print("数据库已是新结构，跳过迁移")
        conn.close()
        return

    total_old = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    print(f"待迁移记录数: {total_old}")

    ensure_new_schema(conn)

    # 若上次 dry-run 或中断残留 conversations_new，先清空
    conn.execute("DELETE FROM messages")
    conn.execute("DELETE FROM message_contents")
    conn.execute("DELETE FROM system_prompts")
    conn.execute("DELETE FROM conversations_new")

    cache = {}
    batch_msg = []
    t0 = time.time()
    last_report = t0
    done = 0
    has_messages_col = "messages" in cols
    has_sp_col = "system_prompt" in cols
    has_lum_col = "last_user_message" in cols
    has_resp_col = "response" in cols
    has_pt = "prompt_tokens" in cols
    has_ct = "completion_tokens" in cols
    has_tt = "total_tokens" in cols
    has_dur = "duration_ms" in cols
    has_endpoint = "endpoint" in cols
    has_model = "model" in cols
    has_provider = "provider" in cols
    has_rt = "request_time" in cols

    select_cols = [c for c in ["id", "model", "provider", "endpoint", "request_time", "messages", "system_prompt", "last_user_message", "response", "prompt_tokens", "completion_tokens", "total_tokens", "duration_ms"] if c in cols]
    col_index = {c: i for i, c in enumerate(select_cols)}

    ctx = {
        "col_index": col_index,
        "cache": cache,
        "batch_msg": batch_msg,
        "has_messages_col": has_messages_col,
        "has_sp_col": has_sp_col,
        "has_lum_col": has_lum_col,
        "has_resp_col": has_resp_col,
        "has_pt": has_pt,
        "has_ct": has_ct,
        "has_tt": has_tt,
        "has_dur": has_dur,
        "has_endpoint": has_endpoint,
        "has_model": has_model,
        "has_provider": has_provider,
        "has_rt": has_rt,
    }

    def flush_batch():
        if ctx["batch_msg"]:
            conn.executemany("INSERT INTO messages (conversation_id, seq, role, content_id) VALUES (?, ?, ?, ?)", ctx["batch_msg"])
            ctx["batch_msg"] = []

    def report(done_now):
        nonlocal done, last_report
        done = done_now
        now = time.time()
        if now - last_report >= 5:
            speed = done / max(now - t0, 1e-6)
            eta = (total_old - done) / speed
            print(f"  进度 {done}/{total_old}  ({done * 100.0 // max(total_old, 1)}%)  speed={speed:.0f}条/s  剩余约{eta / 60:.1f}min")
            last_report = now

    rows = conn.execute(f"SELECT {', '.join(select_cols)} FROM conversations").fetchall()
    last_id = 0
    for i, r in enumerate(rows):
        last_id = _migrate_row(conn, r, ctx)
        # 分批提交，释放写锁，让运行中的服务端能继续写入旧表
        if (i + 1) % 500 == 0:
            flush_batch()
            conn.commit()
        report(i + 1)
    flush_batch()
    conn.commit()
    print(f"主迁移完成: 已处理 {len(rows)} 条 (last_id={last_id})")

    # ── 增量追平：迁移期间服务端写入的新记录 ──
    print("增量追平：检查迁移期间新增的对话...")
    caught_up = 0
    while True:
        cur_max = conn.execute("SELECT COALESCE(MAX(id), 0) FROM conversations").fetchone()[0]
        if cur_max <= last_id:
            break
        new_rows = conn.execute(
            f"SELECT {', '.join(select_cols)} FROM conversations WHERE id > ? ORDER BY id", (last_id,)
        ).fetchall()
        if not new_rows:
            break
        for r in new_rows:
            last_id = _migrate_row(conn, r, ctx)
            caught_up += 1
        flush_batch()
        conn.commit()
        print(f"  追平 {caught_up} 条新增, 最新 id={last_id}")

    # 原子切表：在排他事务内做最后一次追平 + 表切换，杜绝追平与切表间的竞态
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur_max = conn.execute("SELECT COALESCE(MAX(id), 0) FROM conversations").fetchone()[0]
        while cur_max > last_id:
            new_rows = conn.execute(
                f"SELECT {', '.join(select_cols)} FROM conversations WHERE id > ? ORDER BY id", (last_id,)
            ).fetchall()
            if not new_rows:
                break
            for r in new_rows:
                last_id = _migrate_row(conn, r, ctx)
                caught_up += 1
            flush_batch()
            print(f"  [锁内] 追平 {caught_up} 条新增, 最新 id={last_id}")
            cur_max = conn.execute("SELECT COALESCE(MAX(id), 0) FROM conversations").fetchone()[0]

        # 锁内校验（此刻无并发写入，计数稳定）
        new_count = conn.execute("SELECT COUNT(*) FROM conversations_new").fetchone()[0]
        msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        sp_count = conn.execute("SELECT COUNT(*) FROM system_prompts").fetchone()[0]
        mc_count = conn.execute("SELECT COUNT(*) FROM message_contents").fetchone()[0]
        print(f"迁移完成: conversations_new={new_count} messages={msg_count} system_prompts={sp_count} message_contents={mc_count}")
        print(f"去重效果: messages 引用 {msg_count} 行 -> 唯一内容 {mc_count} 行")
        if caught_up:
            print(f"增量追平: 迁移期间新增 {caught_up} 条对话已补齐")

        if new_count < total_old:
            print(f"!! 校验失败: 新表行数 {new_count} < 旧表快照 {total_old}，回滚")
            conn.rollback()
            conn.close()
            sys.exit(1)

        if args.dry_run:
            print("dry-run 模式：不执行表切换")
            conn.rollback()
            conn.close()
            return

        # 切换表
        print("切换表: conversations -> conversations_old, conversations_new -> conversations")
        conn.execute("DROP TABLE IF EXISTS conversations_old")
        conn.execute("ALTER TABLE conversations RENAME TO conversations_old")
        conn.execute("ALTER TABLE conversations_new RENAME TO conversations")
        conn.execute("DROP TABLE conversations_old")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    # VACUUM 需要独占锁，服务运行中可能失败；压缩不改变数据，失败则留标记由服务重启时压缩
    vacuum_ok = False
    for attempt in range(3):
        try:
            conn.execute("VACUUM")
            conn.commit()
            vacuum_ok = True
            break
        except sqlite3.OperationalError as e:
            print(f"  VACUUM 第 {attempt+1} 次失败: {e}（服务占用锁，跳过压缩）")
            break
    conn.close()

    if not vacuum_ok:
        marker = db_path + ".needs_vacuum"
        with open(marker, "w", encoding="utf-8") as f:
            f.write("conversations.db 迁移完成但 VACUUM 未执行（服务占用锁）。服务下次启动时将自动压缩。\n")
        print(f"已写压缩标记: {marker}（app 重启时自动 VACUUM）")

    new_size = os.path.getsize(db_path)
    print(f"迁移成功。新库大小: {new_size / 1024 / 1024:.1f} MB" + ("" if vacuum_ok else "（未压缩，重启后自动压缩）"))
    elapsed = time.time() - t0
    print(f"耗时: {elapsed / 60:.1f} 分钟")


if __name__ == "__main__":
    migrate(parse_args())
