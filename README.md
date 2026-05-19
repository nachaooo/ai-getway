# AI Gateway

零配置 AI API 网关，转发 opencode 所有模型调用并统计 Token 用量，提供 Web 仪表盘。

## 快速开始

```bash
pip install -r requirements.txt
python app.py
```

访问 `http://localhost:5000` 查看仪表盘。

首次启动时自动从 `import_routes.json` 导入预配置路由。

## 工作原理

所有 provider 的 `baseURL` 指向网关：

```json
"baseURL": "http://localhost:5000/v1?by=<上游API地址>"
```

- 网关收到请求，将 `?by=` 参数中的上游 URL 作为目标
- 透传 HTTP 方法、请求体、`Authorization` 和 `User-Agent` 头
- 流式与非流式响应均支持
- 每次请求后记录 prompt / completion token 数到 SQLite

## 添加新 Provider

只需在 `opencode.json` 中新增一条即可，无需修改网关代码：

```json
"my-provider": {
  "npm": "@ai-sdk/openai-compatible",
  "name": "my-provider",
  "options": {
    "baseURL": "http://localhost:5000/v1?by=https://api.example.com/v1",
    "apiKey": "sk-xxx"
  }
}
```

## 仪表盘

`http://localhost:5000` 提供：

- **统计卡片**：对话次数、输入 Token、输出 Token
- **本月用量**：按平台统计请求次数与预估费用
- **最近请求**：最近 500 条请求明细，每 15 秒自动刷新
- **时间范围**：1 小时 / 24 小时 / 7 天 / 30 天 切换

## 特性

| 特性 | 说明 |
|------|------|
| 无状态转发 | 不需预配路由，`?by=` 动态指定上游 |
| Token 计数 | DeepSeek 用本地 LlamaTokenizer，其它用 `tiktoken` |
| 思考 Token | `reasoning_content` 正确计入输出 Token |
| Kimi 精确估算 | 调用官方 `tokenizers/estimate-token-count` API 获取精确 prompt 值 |
| SSE 兼容 | 支持 `data:` 和 `data: ` 两种前缀格式 |
| 流式缓冲 | 以 `\n\n` 分割 SSE 事件，防 JSON 截断 |
| 用量趋势图 | 双轴折线图展示 Token / 请求数时间序列 |
| 空消息过滤 | 自动过滤空 `assistant` 消息（Kimi 兼容） |
| User-Agent 透传 | 保留 SDK 原始 UA（Kimi 白名单需要） |

## 路由管理

API 接口 `/api/routes`：

```bash
# 查看所有路由
curl http://localhost:5000/api/routes

# 添加路由
curl -X POST http://localhost:5000/api/routes \
  -H "Content-Type: application/json" \
  -d '{"route_name":"my-route","upstream_base_url":"https://...","upstream_api_key":"sk-xxx"}'

# 删除路由
curl -X DELETE http://localhost:5000/api/routes \
  -H "Content-Type: application/json" \
  -d '{"route_name":"my-route"}'
```

## 项目结构

```
ai-gateway/
├── app.py               # 主程序（代理 + DB + API）
├── import_routes.json   # 首次启动导入的路由表
├── requirements.txt
├── templates/
│   └── index.html       # Web 仪表盘
├── tokenizers/
│   └── deepseek/        # DeepSeek V3 LlamaTokenizer
└── usage.db             # SQLite 用量记录（自动创建）
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PORT` | `5000` | 监听端口 |
| `DB_PATH` | `./usage.db` | SQLite 数据库路径 |
