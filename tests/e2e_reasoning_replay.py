"""端到端验证 reasoning_content 回填：模拟 DeepSeek 思考模式 + tools 契约。

不走真实网络（沙箱限制 socket 监听），用 Flask 测试客户端 + 内存假上游：
  1. 假上游第一轮返回 reasoning_content + tool_calls，并像 DeepSeek 一样
     「校验」后续请求必须带 reasoning_content，否则回 400。
  2. 请求网关第一轮，客户端拿到响应。
  3. 客户端按 OpenAI 标准 schema 重建历史（丢弃 reasoning_content）再发第二轮。
  4. 若网关回填生效，假上游校验通过 → 第二轮成功；否则复现 400。
"""
import json
import os
import sys
import tempfile

os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["CONV_DB_PATH"] = tempfile.mktemp(suffix="_conv.db")
os.environ["PORT"] = "59871"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app

REASONING = "I should read the file first."
TOOL_CALL_ID = "call_e2e_001"

state = {"round": 0, "got_reasoning": []}


class FakeResp:
    """模拟 requests 的流式响应对象。"""

    def __init__(self, status_code, body_text):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.text = body_text
        self.encoding = "utf-8"
        self.headers = {"content-type": "text/event-stream"}

    def iter_lines(self, decode_unicode=False):
        for line in self.text.split("\n"):
            yield line


def fake_post(url, headers=None, data=None, json_body=None, **kwargs):
    """假上游：复刻 DeepSeek 思考模式 + tools 的 reasoning_content 契约。"""
    body = json.loads(data) if data else (json_body or {})
    state["round"] += 1
    messages = body.get("messages", [])

    if body.get("tools"):
        for m in messages:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                if not m.get("reasoning_content"):
                    err = json.dumps({"error": {
                        "type": "invalid_request_error",
                        "code": "invalid_request_error",
                        "message": ("Upstream request failed: [invalid_request_error] "
                                    "The `reasoning_content` in the thinking mode "
                                    "must be passed back to the API."),
                    }})
                    return FakeResp(400, err)
                state["got_reasoning"].append(m["reasoning_content"])

    if state["round"] == 1:
        delta_seq = [
            {"choices": [{"index": 0, "delta": {"reasoning_content": REASONING}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": TOOL_CALL_ID, "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
        ]
    else:
        delta_seq = [
            {"choices": [{"index": 0, "delta": {"content": "done"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23}},
        ]
    sse = "".join("data: " + json.dumps(d) + "\n\n" for d in delta_seq) + "data: [DONE]\n\n"
    return FakeResp(200, sse)


def main():
    def post(url, headers=None, data=None, json_body=None, **kw):
        return fake_post(url, headers=headers, data=data, json_body=json_body, **kw)

    app.requests.post = post  # 拦住网关的出站请求
    app._reasoning_by_toolcall.clear()
    app._reasoning_by_content.clear()

    client = app.app.test_client()
    tools = [{"type": "function", "function": {"name": "read_file"}}]

    # 第一轮
    r1 = client.post("/v1/chat/completions?by=https://fake.upstream/v1", json={
        "model": "deepseek-v4-flash",
        "tools": tools,
        "stream": True,
        "messages": [{"role": "user", "content": "read the file"}],
    })
    assert r1.status_code == 200, f"round1 failed: {r1.status_code} {r1.get_data(as_text=True)[:300]}"
    sse1 = r1.get_data(as_text=True)
    assert REASONING in sse1, "round1 响应未包含 reasoning_content"
    print("  第一轮: 200，上游返回 reasoning_content + tool_calls")

    # 客户端重建历史：按 OpenAI 标准 schema，丢弃 reasoning_content
    history = [
        {"role": "user", "content": "read the file"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": TOOL_CALL_ID, "type": "function",
                         "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": TOOL_CALL_ID, "content": "file body"},
    ]
    print("  客户端重放历史:", [(m["role"], "reasoning_content" in m) for m in history])
    assert "reasoning_content" not in history[1], "客户端本应丢弃该字段"

    # 第二轮 —— 修复前这里必然 400
    r2 = client.post("/v1/chat/completions?by=https://fake.upstream/v1", json={
        "model": "deepseek-v4-flash",
        "tools": tools,
        "stream": True,
        "messages": history,
    })
    print(f"  第二轮状态码: {r2.status_code}")
    print(f"  上游收到的 reasoning_content: {state['got_reasoning']}")
    assert r2.status_code == 200, f"round2 仍然失败: {r2.status_code} {r2.get_data(as_text=True)[:300]}"
    assert state["got_reasoning"] == [REASONING], "回填内容不符"
    print("\n  PASS: 网关成功回填 reasoning_content，400 已消除")


if __name__ == "__main__":
    main()
