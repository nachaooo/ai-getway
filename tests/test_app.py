import os
import sys
import json
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# 确保项目根目录在路径中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 设置测试用的临时数据库，避免污染真实数据
_test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DB_PATH"] = _test_db.name
# 单库合并后 CONV_DB_PATH == DB_PATH；此变量仅用于定位旧版独立库，指向不存在路径避免误触发合并
os.environ["CONV_DB_PATH"] = tempfile.mktemp(suffix="_conv.db")
os.environ["PORT"] = "59999"

import app


class TestTokenizers(unittest.TestCase):
    def test_count_tokens_deepseek_fallback(self):
        """DeepSeek tokenizer 未加载时回退到字符/4"""
        text = "hello world"
        result = app.count_tokens("deepseek-chat", text)
        self.assertGreaterEqual(result, 1)

    def test_count_tokens_tiktoken(self):
        """tiktoken 正常计数"""
        text = "hello world"
        result = app.count_tokens("gpt-4", text)
        self.assertGreaterEqual(result, 2)

    def test_count_tokens_empty(self):
        """空文本返回 0"""
        self.assertEqual(app.count_tokens("gpt-4", ""), 0)

    def test_count_messages_tokens_plain(self):
        """普通消息列表计数"""
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        result = app.count_messages_tokens("gpt-4", messages)
        self.assertGreaterEqual(result, 2)

    def test_count_messages_tokens_list_content(self):
        """content 为列表格式时提取 text"""
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        ]
        result = app.count_messages_tokens("gpt-4", messages)
        self.assertGreaterEqual(result, 1)


class TestExtractContent(unittest.TestCase):
    def test_openai_choices(self):
        """标准 OpenAI 响应提取 content"""
        data = {"choices": [{"message": {"content": "hello"}}]}
        self.assertEqual(app._extract_content(data), "hello")

    def test_openai_reasoning_content(self):
        """提取 reasoning_content"""
        data = {"choices": [{"message": {"content": "", "reasoning_content": "think"}}]}
        self.assertEqual(app._extract_content(data), "think")

    def test_anthropic_content_list(self):
        """Anthropic 列表格式 content"""
        data = {"content": [{"type": "text", "text": "hello"}]}
        self.assertEqual(app._extract_content(data), "hello")

    def test_anthropic_content_string(self):
        """Anthropic 字符串 content"""
        data = {"content": "hello"}
        self.assertEqual(app._extract_content(data), "hello")

    def test_extract_empty(self):
        """异常数据返回空字符串"""
        self.assertEqual(app._extract_content({}), "")


class TestCleanMessages(unittest.TestCase):
    def test_keep_valid(self):
        """正常消息不过滤"""
        body = {"messages": [{"role": "user", "content": "hi"}]}
        result = app._clean_messages(body)
        self.assertEqual(len(result["messages"]), 1)

    def test_filter_empty(self):
        """过滤空 content 消息"""
        body = {"messages": [
            {"role": "system", "content": ""},
            {"role": "user", "content": "hi"},
        ]}
        result = app._clean_messages(body)
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(result["messages"][0]["content"], "hi")

    def test_keep_tool_calls(self):
        """保留 tool_calls 消息"""
        body = {"messages": [{"role": "assistant", "tool_calls": [{}]}]}
        result = app._clean_messages(body)
        self.assertEqual(len(result["messages"]), 1)


class TestReasoningReplay(unittest.TestCase):
    """DeepSeek 思考模式 + tools 的 reasoning_content 回填契约。"""

    def setUp(self):
        # 每个用例隔离缓存，避免相互污染
        app._reasoning_by_toolcall.clear()
        app._reasoning_by_content.clear()

    def test_patch_by_toolcall_id(self):
        """客户端重放历史时丢弃 reasoning_content，按 tool_call id 回填"""
        app._reasoning_cache_put(["call_abc"], "I will check the file", "thinking...")
        body = {
            "tools": [{"type": "function", "function": {"name": "read"}}],
            "messages": [
                {"role": "user", "content": "read it"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "call_abc"}]},
                {"role": "tool", "tool_call_id": "call_abc", "content": "ok"},
            ],
        }
        result = app._replay_reasoning(body)
        self.assertEqual(result["messages"][1].get("reasoning_content"), "thinking...")

    def test_patch_by_content_fingerprint(self):
        """无 tool_calls 的轮次按 assistant content 指纹兜底回填"""
        app._reasoning_cache_put([], "final answer", "reasoning without tools")
        body = {
            "tools": [{"type": "function", "function": {"name": "read"}}],
            "messages": [
                {"role": "assistant", "content": "final answer"},
            ],
        }
        result = app._replay_reasoning(body)
        self.assertEqual(result["messages"][0].get("reasoning_content"), "reasoning without tools")

    def test_skip_without_tools(self):
        """未携带 tools 时不处理（非 DeepSeek 思考模式契约的触发条件）"""
        app._reasoning_cache_put(["call_abc"], "", "should not be injected")
        body = {"messages": [{"role": "assistant", "content": "", "tool_calls": [{"id": "call_abc"}]}]}
        result = app._replay_reasoning(body)
        self.assertNotIn("reasoning_content", result["messages"][0])

    def test_do_not_overwrite_existing(self):
        """客户端已带 reasoning_content 时原样保留，绝不覆盖"""
        app._reasoning_cache_put(["call_abc"], "", "cached value")
        body = {
            "tools": [{"type": "function", "function": {"name": "read"}}],
            "messages": [
                {"role": "assistant", "content": "", "tool_calls": [{"id": "call_abc"}],
                 "reasoning_content": "client value"},
            ],
        }
        result = app._replay_reasoning(body)
        self.assertEqual(result["messages"][0]["reasoning_content"], "client value")

    def test_replay_disabled(self):
        """开关关闭时完全不介入"""
        app._reasoning_cache_put(["call_abc"], "", "cached value")
        original = app.ENABLE_REASONING_REPLAY
        try:
            app.ENABLE_REASONING_REPLAY = False
            body = {
                "tools": [{"type": "function", "function": {"name": "read"}}],
                "messages": [{"role": "assistant", "content": "", "tool_calls": [{"id": "call_abc"}]}],
            }
            result = app._replay_reasoning(body)
            self.assertNotIn("reasoning_content", result["messages"][0])
        finally:
            app.ENABLE_REASONING_REPLAY = original

    def test_cache_roundtrip_and_eviction(self):
        """缓存读写往返正常，且超上限后按 LRU 淘汰最旧项"""
        app._reasoning_cache_put(["id1"], "", "r1")
        self.assertEqual(app._reasoning_cache_get(["id1"], ""), "r1")

        limit = app._REASONING_CACHE_MAX
        original = app._REASONING_CACHE_MAX
        try:
            app._REASONING_CACHE_MAX = 2
            app._reasoning_cache_put(["a"], "", "ra")
            app._reasoning_cache_put(["b"], "", "rb")
            app._reasoning_cache_put(["c"], "", "rc")
            self.assertIsNone(app._reasoning_cache_get(["a"], ""))
            self.assertEqual(app._reasoning_cache_get(["c"], ""), "rc")
        finally:
            app._REASONING_CACHE_MAX = original
            self.assertTrue(limit >= 1)

    def test_empty_reasoning_not_cached(self):
        """空 reasoning 不入缓存，避免无意义占用"""
        app._reasoning_cache_put(["call_x"], "content", "")
        self.assertIsNone(app._reasoning_cache_get(["call_x"], "content"))

    def test_concurrent_put_get_no_exception(self):
        """waitress 多线程并发读写不应抛异常（LRU 操作需加锁）"""
        import threading
        errors = []

        def worker(n):
            try:
                for i in range(200):
                    app._reasoning_cache_put([f"tc-{n}-{i}"], f"content-{n}-{i}", f"r-{n}-{i}")
                    app._reasoning_cache_get([f"tc-{n}-{i}"], f"content-{n}-{i}")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        original = app._REASONING_CACHE_MAX
        try:
            app._REASONING_CACHE_MAX = 8  # 强制高频淘汰，放大竞态
            threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            app._REASONING_CACHE_MAX = original
        self.assertEqual(errors, [], f"并发读写抛异常: {errors[:3]}")


class TestDatabaseAPI(unittest.TestCase):
    def setUp(self):
        """每个测试前重新初始化数据库并插入测试数据"""
        app.init_db()
        self.conn = app.get_db()
        self.conn.execute("DELETE FROM usage_logs")
        self.conn.execute(
            "INSERT INTO usage_logs (model, provider, prompt_tokens, completion_tokens, total_tokens, request_time, endpoint) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("gpt-4", "openai", 100, 50, 150, "2026-05-20 10:00:00", "/v1/chat/completions"),
        )
        self.conn.execute(
            "INSERT INTO usage_logs (model, provider, prompt_tokens, completion_tokens, total_tokens, request_time, endpoint) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("deepseek-chat", "deepseek", 200, 100, 300, "2026-05-20 11:00:00", "/v1/chat/completions"),
        )
        self.conn.commit()
        self.conn.close()
        self.client = app.app.test_client()

    def test_api_usage_totals(self):
        """api/usage 返回正确的汇总数据"""
        resp = self.client.get("/api/usage?start=2026-05-20 00:00:00&end=2026-05-20 23:59:59&group_by=hour")
        data = json.loads(resp.data)
        self.assertEqual(data["totals"]["request_count"], 2)
        self.assertEqual(data["totals"]["prompt_tokens"], 300)
        self.assertEqual(data["totals"]["completion_tokens"], 150)
        self.assertEqual(data["totals"]["total_tokens"], 450)

    def test_api_usage_billing(self):
        """api/usage billing 按 provider 分组"""
        resp = self.client.get("/api/usage?start=2026-05-20 00:00:00&end=2026-05-20 23:59:59&group_by=hour")
        data = json.loads(resp.data)
        billing = data["billing"]
        self.assertIn("openai", billing)
        self.assertIn("deepseek", billing)
        self.assertEqual(billing["openai"]["month_used"], 1)
        self.assertEqual(billing["deepseek"]["prompt_tokens"], 200)

    def test_api_recent_pagination(self):
        """api/recent 分页正常"""
        resp = self.client.get("/api/recent?page=1&page_size=10")
        data = json.loads(resp.data)
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["page"], 1)
        self.assertEqual(len(data["items"]), 2)
        # 按时间倒序
        self.assertEqual(data["items"][0]["provider"], "deepseek")

    def test_api_recent_time_filter(self):
        """api/recent 时间范围过滤"""
        resp = self.client.get("/api/recent?start=2026-05-20 10:30:00&end=2026-05-20 11:30:00")
        data = json.loads(resp.data)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["provider"], "deepseek")

    def test_dashboard_loads(self):
        """首页能正常加载"""
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"AI Gateway", resp.data)

    def test_db_upload_restores_usage_db(self):
        """导入 usage.db 应整体覆盖现有数据（backup API 原地还原）"""
        upload_path = tempfile.mktemp(suffix=".db")
        conn = sqlite3.connect(upload_path)
        conn.execute("PRAGMA journal_mode=WAL")
        app.ensure_schema(conn)
        conn.execute(
            "INSERT INTO usage_logs (model, provider, prompt_tokens, completion_tokens, total_tokens, request_time, endpoint) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("imported-model", "imported-provider", 10, 20, 30, "2026-01-01 00:00:00", "/v1/chat/completions"),
        )
        conn.commit()
        conn.close()

        try:
            with open(upload_path, "rb") as fp:
                resp = self.client.post(
                    "/api/db/upload",
                    data={"file": (fp, "usage.db")},
                    content_type="multipart/form-data",
                )
            self.assertEqual(resp.status_code, 200, resp.data)

            conn = app.get_db()
            rows = conn.execute("SELECT provider, total_tokens FROM usage_logs").fetchall()
            conn.close()
            self.assertEqual([(r["provider"], r["total_tokens"]) for r in rows], [("imported-provider", 30)])
        finally:
            if os.path.exists(upload_path):
                os.unlink(upload_path)

    def test_db_upload_rejects_invalid_file(self):
        """导入非法文件应返回 400 而不是覆盖现有库"""
        upload_path = tempfile.mktemp(suffix=".db")
        with open(upload_path, "wb") as f:
            f.write(b"not a sqlite file")

        try:
            with open(upload_path, "rb") as fp:
                resp = self.client.post(
                    "/api/db/upload",
                    data={"file": (fp, "usage.db")},
                    content_type="multipart/form-data",
                )
            self.assertEqual(resp.status_code, 400, resp.data)

            conn = app.get_db()
            n = conn.execute("SELECT COUNT(*) AS n FROM usage_logs").fetchone()["n"]
            conn.close()
            self.assertEqual(n, 2)
        finally:
            if os.path.exists(upload_path):
                os.unlink(upload_path)


class TestProxyValidation(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def test_proxy_missing_by(self):
        """缺少 ?by= 参数返回 400"""
        resp = self.client.post("/v1/chat/completions", json={"model": "gpt-4"})
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.data)
        self.assertIn("?by=", data["error"])

    def test_proxy_v1_no_query_no_model(self):
        """POST /v1 无参数无 model 返回 JSON 400（非 HTML）"""
        resp = self.client.post("/v1", json={})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("application/json", resp.content_type)
        data = json.loads(resp.data)
        self.assertIn("model is required", data["error"])

    def test_proxy_v1_arbitrary_query_no_model(self):
        """POST /v1?任意参数 应返回 JSON 400（非 HTML 400）"""
        resp = self.client.post("/v1?foo=bar", json={})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("application/json", resp.content_type)
        data = json.loads(resp.data)
        self.assertIn("model is required", data["error"])

    def test_proxy_v1_by_param_with_model(self):
        """POST /v1?by=url 正确转发上游（mock）"""
        with patch("app.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
            mock_post.return_value = mock_resp

            resp = self.client.post("/v1?by=https://api.test.com/v1", json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual(resp.status_code, 200)
            data = json.loads(resp.data)
            self.assertEqual(data["choices"][0]["message"]["content"], "ok")

    def test_proxy_v1_chat_completions_no_query(self):
        """POST /v1/chat/completions 无参数返回 JSON 400（非 HTML）"""
        resp = self.client.post("/v1/chat/completions", json={})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("application/json", resp.content_type)
        data = json.loads(resp.data)
        self.assertIn("model is required", data["error"])

    def test_proxy_v1_chat_completions_arbitrary_query(self):
        """POST /v1/chat/completions?任意参数 应返回 JSON 400（非 HTML 400）"""
        resp = self.client.post("/v1/chat/completions?foo=bar", json={})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("application/json", resp.content_type)
        data = json.loads(resp.data)
        self.assertIn("model is required", data["error"])


class TestUpstreamHeaders(unittest.TestCase):
    """OpenCode Go 端点强制要求：x-opencode-session + coding-agent UA"""

    def setUp(self):
        self.client = app.app.test_client()
        self.body = {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]}

    def test_session_generated_when_missing(self):
        """客户端未带 x-opencode-session 时自动派生稳定会话 ID"""
        with patch("app.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
            mock_post.return_value = mock_resp
            resp = self.client.post("/v1/chat/completions?by=https://opencode.ai/zen/go/v1", json=self.body)
            self.assertEqual(resp.status_code, 200)
            headers = mock_post.call_args.kwargs["headers"]
            self.assertTrue(headers["x-opencode-session"].startswith("gw-"))

    def test_session_stable_per_conversation(self):
        """同一会话（首条消息相同）派生的会话 ID 保持稳定"""
        with patch("app.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
            mock_post.return_value = mock_resp
            self.client.post("/v1/chat/completions?by=https://opencode.ai/zen/go/v1", json=self.body)
            self.client.post("/v1/chat/completions?by=https://opencode.ai/zen/go/v1", json=dict(self.body))
            calls = mock_post.call_args_list
            self.assertEqual(calls[0].kwargs["headers"]["x-opencode-session"],
                             calls[1].kwargs["headers"]["x-opencode-session"])

    def test_session_forwarded_when_present(self):
        """客户端已带 x-opencode-session 时原样透传"""
        with patch("app.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
            mock_post.return_value = mock_resp
            resp = self.client.post(
                "/v1/chat/completions?by=https://opencode.ai/zen/go/v1",
                json=self.body,
                headers={"x-opencode-session": "my-session-123"},
            )
            self.assertEqual(resp.status_code, 200)
            headers = mock_post.call_args.kwargs["headers"]
            self.assertEqual(headers["x-opencode-session"], "my-session-123")

    def test_generic_user_agent_replaced(self):
        """通用库 UA（python-requests/curl 等）替换为 ai-gateway 自有标识"""
        with patch("app.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
            mock_post.return_value = mock_resp
            for ua in ("python-requests/2.31.0", "curl/8.0.1", "Go-http-client/2.0", "node"):
                self.client.post("/v1/chat/completions?by=https://opencode.ai/zen/go/v1",
                                 json=self.body, headers={"User-Agent": ua})
                sent_ua = mock_post.call_args.kwargs["headers"]["User-Agent"]
                self.assertTrue(sent_ua.startswith("ai-gateway/"), f"UA '{ua}' 未被替换: {sent_ua}")

    def test_self_identified_user_agent_forwarded(self):
        """测试客户端自带的自标识 UA（Werkzeug/x.y）原样透传，不被替换"""
        with patch("app.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
            mock_post.return_value = mock_resp
            resp = self.client.post("/v1/chat/completions?by=https://opencode.ai/zen/go/v1", json=self.body)
            self.assertEqual(resp.status_code, 200)
            sent_ua = mock_post.call_args.kwargs["headers"]["User-Agent"]
            self.assertTrue(sent_ua.startswith("Werkzeug/"), f"自标识 UA 被意外替换: {sent_ua}")

    def test_agent_user_agent_forwarded(self):
        """coding-agent UA 原样透传"""
        with patch("app.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
            mock_post.return_value = mock_resp
            self.client.post("/v1/chat/completions?by=https://opencode.ai/zen/go/v1",
                             json=self.body, headers={"User-Agent": "opencode/1.5.0"})
            self.assertEqual(mock_post.call_args.kwargs["headers"]["User-Agent"], "opencode/1.5.0")


class TestProxyModels(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def test_models_requires_by(self):
        """缺少 ?by= 返回 400"""
        resp = self.client.get("/v1/models")
        self.assertEqual(resp.status_code, 400)

    def test_models_passthrough(self):
        """GET /v1/models?by= 正确拼接 /models 并透传响应"""
        with patch("app.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = b'{"object":"list","data":[{"id":"kimi-k3"}]}'
            mock_resp.headers = {"content-type": "application/json"}
            mock_get.return_value = mock_resp
            resp = self.client.get("/v1/models?by=https://opencode.ai/zen/go/v1",
                                   headers={"Authorization": "Bearer sk-test"})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(mock_get.call_args.args[0], "https://opencode.ai/zen/go/v1/models")
            self.assertEqual(mock_get.call_args.kwargs["headers"]["Authorization"], "Bearer sk-test")


class TestInsights(unittest.TestCase):
    def test_normalize_insight_with_suggestions(self):
        """解析 LLM 输出，正确提取 summary/topics/suggestions"""
        data = {
            "summary": "修复前端构建问题",
            "topics": [{"topic": "前端/UI", "activities": ["修复样式", "调整布局"]}],
            "suggestions": ["拆分任务", "增加测试"],
        }
        r = app._normalize_insight(data)
        self.assertEqual(r["summary"], "修复前端构建问题")
        self.assertEqual(r["topics"][0]["topic"], "前端/UI")
        self.assertEqual(r["suggestions"], ["拆分任务", "增加测试"])

    def test_normalize_insight_empty(self):
        """空输入返回 None"""
        self.assertIsNone(app._normalize_insight(None))
        self.assertIsNone(app._normalize_insight({}))

    def test_insight_bucket_key_day(self):
        """按天分桶"""
        self.assertEqual(app._insight_bucket_key("2026-08-04 10:30:00", "day"), "2026-08-04")

    def test_insight_bucket_key_week(self):
        """按周分桶（ISO 周）"""
        key = app._insight_bucket_key("2026-08-04 10:30:00", "week")
        self.assertRegex(key, r"^2026-W\d{2}$")


class TestMergeConvDb(unittest.TestCase):
    def setUp(self):
        import app as app_mod
        self.app_mod = app_mod
        self.legacy_dir = tempfile.mkdtemp()
        self.legacy_path = os.path.join(self.legacy_dir, "conversations.db")
        self.legacy = sqlite3.connect(self.legacy_path)
        self.legacy.row_factory = sqlite3.Row
        self.legacy.execute("PRAGMA journal_mode=WAL")
        self.app_mod.ensure_conv_schema(self.legacy)
        self.legacy.execute(
            "INSERT INTO conversations (id, model, provider, request_time, last_user_message, response) VALUES (?, ?, ?, ?, ?, ?)",
            (1, "deepseek-chat", "deepseek", "2026-01-01 10:00:00", "hi", "hello"),
        )
        self.legacy.commit()
        self._tmp_db = tempfile.mktemp(suffix=".db")

    def tearDown(self):
        self.legacy.close()
        shutil.rmtree(self.legacy_dir, ignore_errors=True)
        for p in (self._tmp_db, self._tmp_db + "-wal", self._tmp_db + "-shm"):
            if os.path.exists(p):
                os.unlink(p)

    def test_merge_legacy_conv_db(self):
        """旧版 conversations.db 自动合并进单库，旧库保留"""
        with patch.object(self.app_mod, "DB_PATH", self._tmp_db), patch.object(self.app_mod, "CONV_DB_PATH", self._tmp_db), patch.object(self.app_mod, "LEGACY_CONV_DB_PATH", self.legacy_path):
            self.app_mod.init_db()
            self.app_mod._merge_legacy_conv_db()
            conn = self.app_mod.get_conv_db()
            rows = conn.execute("SELECT * FROM conversations").fetchall()
            conn.close()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["model"], "deepseek-chat")
        self.assertTrue(os.path.exists(self.legacy_path))

    def test_merge_idempotent(self):
        """重复合并不重复插入"""
        with patch.object(self.app_mod, "DB_PATH", self._tmp_db), patch.object(self.app_mod, "CONV_DB_PATH", self._tmp_db), patch.object(self.app_mod, "LEGACY_CONV_DB_PATH", self.legacy_path):
            self.app_mod.init_db()
            self.app_mod._merge_legacy_conv_db()
            self.app_mod._merge_legacy_conv_db()
            conn = self.app_mod.get_conv_db()
            n = conn.execute("SELECT COUNT(*) as n FROM conversations").fetchone()["n"]
            conn.close()
            self.assertEqual(n, 1)


class TestOversizedRequestGuards(unittest.TestCase):
    """2026-10-22 全站 504 的回归防线。

    事故链路：43MB body → request.get_json 全量解析（放大 5.9 倍）→ json.dumps
    整份重序列化（C 编码器持 GIL）→ 撞 cgroup MemoryHigh 后分配重试循环空转
    → 32 个 waitress worker 全部饿死 → 连 GET / 都 504。
    """

    def setUp(self):
        self.client = app.app.test_client()
        self._orig_stateless = app._STATELESS_BODY_BYTES
        self._orig_max = app.app.config["MAX_CONTENT_LENGTH"]

    def tearDown(self):
        app._STATELESS_BODY_BYTES = self._orig_stateless
        app.app.config["MAX_CONTENT_LENGTH"] = self._orig_max

    # ── tiktoken O(n²) 地雷 ──────────────────────────────

    def test_count_tokens_large_text_skips_tiktoken(self):
        """超长文本不走 tiktoken（256KB 连续同字符实测要 45s）"""
        text = "x" * (app.TOKENIZE_MAX_BYTES + 1)
        with patch("tiktoken.get_encoding") as mock_enc:
            n = app.count_tokens("mimo-v2.6-flash", text)
            mock_enc.assert_not_called()
        self.assertEqual(n, len(text) // 4)

    def test_count_tokens_small_text_still_tiktoken(self):
        """小文本仍走 tiktoken，保证记账精度"""
        text = "hello world " * 10
        self.assertGreater(app.count_tokens("gpt-4", text), 10)

    # ── 入库内容收缩 ──────────────────────────────────────

    def test_shrink_text_strips_data_uri(self):
        """内联图片（data-uri）不入库，保留媒体类型提示"""
        raw = "看图：" + "data:image/png;base64," + "A" * 5000 + " 完毕"
        out = app._shrink_text(raw)
        self.assertIn("[image/png omitted]", out)
        self.assertNotIn("base64,", out)
        self.assertIn("完毕", out)

    def test_shrink_text_strips_bare_base64_blob(self):
        """裸 base64 大块（≥4KB）不入库"""
        raw = "结果 " + "QUJD" * 2000 + " 结束"
        out = app._shrink_text(raw)
        self.assertIn("[base64 omitted]", out)
        self.assertLess(len(out), len(raw))

    def test_shrink_text_truncates_oversized(self):
        """超长文本截断并标注原始长度"""
        raw = "中" * (app.CONV_CONTENT_MAX_BYTES + 5000)
        out = app._shrink_text(raw)
        self.assertLess(len(out), app.CONV_CONTENT_MAX_BYTES + 200)
        self.assertIn("omitted", out)

    def test_shrink_obj_strips_image_parts(self):
        """content list 里的 image/file 二进制块换成占位符，文本保留"""
        content = [
            {"type": "text", "text": "这是问题"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "B" * 9000}},
            {"type": "file", "data": "C" * 9000, "filename": "a.pdf"},
        ]
        out = app._shrink_obj(content)
        self.assertEqual(out[0]["text"], "这是问题")
        self.assertEqual(out[1]["image_url"]["url"], app._BIN_PROMPT)
        self.assertEqual(out[2]["data"], app._BIN_PROMPT)
        self.assertEqual(out[2]["filename"], "a.pdf")

    def test_shrink_keeps_small_text_intact(self):
        """小文本与 text/* data-uri 原样保留，不做无谓裁剪"""
        content = [
            {"type": "text", "text": "短文本"},
            {"type": "text", "text": "data:text/plain;base64,SGVsbG8="},
        ]
        out = app._shrink_obj(content)
        self.assertEqual(out[0]["text"], "短文本")
        self.assertEqual(out[1]["text"], "data:text/plain;base64,SGVsbG8=")

    def test_shrink_strips_small_image_data_uri(self):
        """即使很小的图片 data-uri 也不入库（图片一律不存）"""
        out = app._shrink_obj([{"type": "image_url",
                                "image_url": {"url": "data:image/png;base64," + "D" * 100}}])
        self.assertIn("[image/png omitted]", out[0]["image_url"]["url"])
        self.assertNotIn("DDDD", out[0]["image_url"]["url"])

    # ── 请求体硬上限 ──────────────────────────────────────

    def test_413_when_body_exceeds_limit(self):
        """超过 MAX_CONTENT_LENGTH 返回 413（不再进入解析路径）"""
        app.app.config["MAX_CONTENT_LENGTH"] = 1024
        resp = self.client.post("/v1?by=https://api.test.com/v1",
                                data=b"{" + b" " * 4096 + b"}", content_type="application/json")
        self.assertEqual(resp.status_code, 413)
        self.assertIn("too large", json.loads(resp.data)["error"])

    # ── 无状态直通 ────────────────────────────────────────

    def _sse_resp(self, lines):
        m = MagicMock()
        m.ok = True
        m.headers = {"content-type": "text/event-stream"}
        m.iter_lines.return_value = iter(lines)
        return m

    def test_large_body_forwarded_verbatim(self):
        """大 body 原样透传：上游收到的字节与客户端发出的完全一致（不重新序列化）"""
        app._STATELESS_BODY_BYTES = 256
        payload = json.dumps({"model": "mimo-v2.6-flash", "messages": [
            {"role": "user", "content": "x" * 1000}]}, ensure_ascii=False).encode("utf-8")
        self.assertGreater(len(payload), 256)

        with patch("app.requests.post") as mock_post:
            mock_post.return_value = self._sse_resp([
                'data: {"model":"mimo-v2.6-flash","usage":{"prompt_tokens":10,"completion_tokens":5}}',
                "data: [DONE]",
            ])
            resp = self.client.post("/v1?by=https://api.test.com/zen/go/v1/chat/completions",
                                    data=payload, content_type="application/json")
            self.assertEqual(resp.status_code, 200)
            resp.get_data()

        sent = mock_post.call_args.kwargs["data"]
        self.assertEqual(sent, payload, "上游收到的必须是原始字节，不能被 json.dumps 重新序列化")

    def test_large_body_skips_reasoning_replay_and_conversation_log(self):
        """大 body 不解析：不写 conversations 表（只记 usage）"""
        app._STATELESS_BODY_BYTES = 256
        payload = json.dumps({"model": "mimo-v2.6-flash", "messages": [
            {"role": "assistant", "content": "y" * 1000, "tool_calls": [{"id": "t1"}]}]}).encode()

        conn = app.get_conv_db()
        before = conn.execute("SELECT COUNT(*) n FROM conversations").fetchone()["n"]
        conn.close()

        with patch("app.requests.post") as mock_post:
            mock_post.return_value = self._sse_resp([
                'data: {"model":"mimo-v2.6-flash","usage":{"prompt_tokens":7,"completion_tokens":3}}'])
            resp = self.client.post("/v1?by=https://api.test.com/v1", data=payload,
                                    content_type="application/json")
            resp.get_data()

        conn = app.get_conv_db()
        after = conn.execute("SELECT COUNT(*) n FROM conversations").fetchone()["n"]
        conn.close()
        self.assertEqual(before, after, "大 body 不应写对话库")

    def test_large_body_usage_logged_from_response(self):
        """大 body 的 token 记账取自上游响应里的 usage"""
        app._STATELESS_BODY_BYTES = 256
        payload = json.dumps({"model": "mimo-v2.6-flash", "messages": [
            {"role": "user", "content": "z" * 1000}]}).encode()

        with patch("app.requests.post") as mock_post:
            mock_post.return_value = self._sse_resp([
                'data: {"usage":{"prompt_tokens":1234,"completion_tokens":56,'
                '"prompt_tokens_details":{"cached_tokens":1200},'
                '"completion_tokens_details":{"reasoning_tokens":6}}}'])
            self.client.post("/v1?by=https://api.test.com/v1", data=payload,
                             content_type="application/json").get_data()

        conn = app.get_db()
        row = conn.execute("SELECT * FROM usage_logs ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        self.assertEqual(row["model"], "mimo-v2.6-flash")
        self.assertEqual(row["prompt_tokens"], 1234)
        self.assertEqual(row["completion_tokens"], 56)
        self.assertEqual(row["cached_tokens"], 1200)
        self.assertEqual(row["reasoning_tokens"], 6)

    def test_large_body_releases_concurrency_slot(self):
        """大请求结束后释放闸门，不会几次之后全部 503"""
        app._STATELESS_BODY_BYTES = 256
        payload = json.dumps({"model": "m", "messages": [{"role": "user", "content": "q" * 800}]}).encode()
        with patch("app.requests.post") as mock_post:
            mock_post.return_value = self._sse_resp(['data: {"usage":{"prompt_tokens":1,"completion_tokens":1}}'])
            for _ in range(app.LARGE_BODY_CONCURRENCY + 3):
                resp = self.client.post("/v1?by=https://api.test.com/v1", data=payload,
                                        content_type="application/json")
                resp.get_data()
                self.assertEqual(resp.status_code, 200)

    def test_small_body_still_full_path_and_replay(self):
        """小 body 行为不变：reasoning_content 仍会回填"""
        app._STATELESS_BODY_BYTES = 10 * 1024 * 1024
        app._reasoning_by_toolcall.clear()
        app._reasoning_cache_put(["call_x"], "prev", "thinking-abc")
        body = {"model": "deepseek-v4.1-flash", "stream": False,
                "tools": [{"type": "function", "function": {"name": "f"}}],
                "messages": [{"role": "assistant", "content": "prev", "tool_calls": [{"id": "call_x"}]}]}

        with patch("app.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
            mock_post.return_value = mock_resp
            resp = self.client.post("/v1?by=https://api.test.com/v1", json=body)
            self.assertEqual(resp.status_code, 200)

        sent = json.loads(mock_post.call_args.kwargs["data"])
        self.assertEqual(sent["messages"][0]["reasoning_content"], "thinking-abc")


class TestHealthEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def test_health_ok(self):
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["version"], app.VERSION)


if __name__ == "__main__":
    unittest.main()