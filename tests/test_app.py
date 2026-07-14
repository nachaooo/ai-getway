import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# 确保项目根目录在路径中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 设置测试用的临时数据库，避免污染真实数据
_test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DB_PATH"] = _test_db.name
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


if __name__ == "__main__":
    unittest.main()
