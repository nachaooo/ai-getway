# AI Gateway

> Zero-config API proxy with token usage dashboard.  
> Works with any OpenAI-compatible client (opencode, Cursor, Continue, Aider, etc.)

[中文说明](#chinese)

---

## Features

- **Zero-config passthrough** — Set `?by=<upstream_url>` dynamically, no route pre-configuration needed
- **Token counting** — DeepSeek uses local LlamaTokenizer; others fallback to `tiktoken` / char estimation
- **Thinking tokens** — `reasoning_content` counted as output tokens (DeepSeek R1, Kimi K2, etc.)
- **Kimi precise estimation** — Calls official `tokenizers/estimate-token-count` API for accurate prompt tokens
- **SSE compatible** — Supports both `data:` and `data: ` prefix formats
- **Stream buffering** — Parses SSE events by `\n\n` delimiter, prevents JSON truncation
- **Empty message filter** — Auto-filters empty assistant messages (required by Kimi)
- **Dashboard** — Dark-themed stats + timeline chart + billing + recent requests (500 rows), with period-over-period comparison on stat cards
- **Tray launcher** — Windows system tray icon with right-click menu (`start.vbs`)
- **Auto-start** — Toggle from the tray menu (appears in Windows Settings)

## Quick Start

```bash
pip install -r requirements.txt
python app.py
```

Open `http://localhost:5000` for the dashboard.

## How it Works

Point your client's baseURL to the gateway:

```json
{
  "baseURL": "http://localhost:5000/v1?by=https://api.deepseek.com",
  "apiKey": "sk-xxx"
}
```

The gateway:
1. Extracts the upstream URL from `?by=` parameter
2. Forwards the request body, `Authorization`, and `User-Agent` headers
3. Supports both streaming and non-streaming responses
4. Logs prompt/completion tokens to SQLite after each request

## Add a Provider

No gateway code changes needed. Just add an entry to your client config:

```json
{
  "my-provider": {
    "baseURL": "http://localhost:5000/v1?by=https://api.example.com/v1",
    "apiKey": "sk-xxx"
  }
}
```

## Dashboard

`http://localhost:5000` provides:

| Widget | Description |
|--------|-------------|
| Stats cards | Total conversations, input tokens, output tokens |
| Trend chart | Dual-axis line chart (tokens + request count over time) |
| Monthly billing | Per-provider request count with time-range selector |
| Recent requests | Last 500 requests with auto-refresh every 15s |

## Launch Methods

**Terminal (all platforms):**
```bash
python app.py
```

**System tray (Windows):**
Double-click `start.vbs` — installs dependencies automatically, runs in background with tray icon.

**Build standalone executable (Windows):**
```bash
# One-click build (auto-bumps patch version)
build.vbs

# Or manually:
pip install pyinstaller
pyinstaller "AI Gateway.spec"
```
Output: `dist/AI Gateway vX.Y.Z.exe` (~26 MB, single file, no console window).

> **Note**: `build.vbs` automatically increments the patch version (e.g. 0.6.0 → 0.6.1) in `app.py` before building.

**Auto-start (Windows):**
Toggle it directly from the tray icon right-click menu. It will appear in Windows Settings → Apps → Startup, where you can also manage it.

## Project Structure

```
ai-gateway/
├── app.py               # Core: proxy, DB, API endpoints
├── config.json          # Configuration file
├── scripts/
│   ├── tray.py          # System tray launcher
│   └── start.vbs        # VBS launcher (auto-dep-install)
├── requirements.txt
├── Dockerfile
├── README.md
├── CHANGELOG.md
├── usage.db             # SQLite (auto-created)
├── templates/
│   └── index.html       # Web dashboard
├── static/
│   └── icon.png         # Tray icon
└── tokenizers/
    └── deepseek/        # DeepSeek LlamaTokenizer
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `5000` | Listening port |
| `DB_PATH` | `./usage.db` | SQLite database path |
| `KIMI_ESTIMATE_URL` | `https://api.moonshot.cn/v1/tokenizers/estimate-token-count` | Kimi token estimation API |

## Docker

```bash
docker build -t ai-gateway .
docker run -d -p 5000:5000 --name ai-gateway ai-gateway
```

## Comparison

| Feature | AI Gateway | Other proxies |
|---------|-----------|---------------|
| Dynamic upstream | ✅ `?by=` | ❌ Static routes |
| Token counting | ✅ Built-in | ❌ Not included |
| Thinking tokens | ✅ `reasoning_content` | ❌ Counted as 0 |
| SSE edge cases | ✅ `data:` / `data: ` | ❌ May crash |
| Kimi precise count | ✅ Official API | ❌ Character estimate |
| Dashboard | ✅ Dark theme + chart | ❌ CLI only |
| Setup | ✅ One baseURL change | ❌ Complex config |

---

## <span id="chinese">中文说明</span>

**AI Gateway** 是一个零配置的 AI API 网关，转发任意兼容 OpenAI 格式的模型请求，自动统计 Token 用量，并提供 Web 仪表盘。

### 快速开始

```bash
pip install -r requirements.txt
python app.py
```

访问 `http://localhost:5000` 查看仪表盘。

### 工作原理

将所有 provider 的 `baseURL` 指向网关，通过 `?by=` 参数动态指定上游地址，无需在网关中预配任何路由。

**支持所有 OpenAI 兼容的客户端**：opencode、Cursor、Continue、Aider 等。

### 启动方式

- **命令行**：`python app.py`
- **Windows 托盘**：双击 `scripts/start.vbs`
- **Docker**：`docker run -d -p 5000:5000 ai-gateway`

### 构建独立可执行文件

```bash
pip install pyinstaller
pyinstaller "AI Gateway.spec"
```

输出：`dist/AI Gateway.exe`（约 26 MB，单文件，无控制台窗口）。

### 开机自启动

右键托盘图标 → 「开机自启动」一键开关。该选项会同步出现在 Windows 设置 → 应用 → 启动 中，也可以在那里管理。

### 特性

- 🚀 零配置，一行 baseURL 搞定
- 📊 用量仪表盘（趋势图 + 统计数据）
- 🧮 精确 Token 计数（含 reasoning_content）
- 🔌 兼容任何 OpenAI 兼容客户端
- 🪟 Windows 托盘后台运行
- 🔄 开机自启动（托盘菜单一键开关，同步到系统启动设置）

---

## License

MIT
