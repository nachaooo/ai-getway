# Changelog

## [0.6.33] - 2026-10-26

### Fixed
- **看板收尾细节**：平台色点仍沿用默认模式的荧光色（`#d8ff3e` / `#ff8a5c` …），在黄铜配色里很跳；`providerColor()` 改为按布局取色，看板用低饱和的琥珀/鼠尾草/钢蓝一组，默认模式保持不变。
- **实时流水表头透出背景网格**：`thead` 用半透明 `--k-surface-2`，`body::after` 的 96px 网格线会透出来，形成一条色差竖线；新增不透明 `--k-head-bg`（暗 `#12100c` / 浅 `#fbf8f1`）替代。

## [0.6.32] - 2026-10-26

### Changed
- **看板模式全面重做：全屏「黑曜石账本」（Obsidian Ledger）**，替换原 acid-green 终端风格。默认模式（`/`）的紧凑卡片布局完全不变，所有新样式都挂在 `[data-layout="kanban"]` 下。
  - **版式**：报头改为刊物式——超大 Bodoni Moda 字标 + 双线 + 刻度尺；主指标区做成「头版大数字」，对话次数占 6 列、字号 `clamp(48px, min(6.6vw, 11vh), 122px)`，其余两项各占 3 列；面板按 01–06 编号，四角裁切标记 + 内嵌细框 + 胶片颗粒。
  - **配色**：近黑暖墨底（`#080706`）+ 黄铜（`#d9b979` / `#f3dda9`）+ 铜（`#d2633c`），浅色主题改为象牙纸（`#f1ece1`）+ 深铜墨。
  - **字体**：Bodoni Moda（衬线数字/标题）+ Jost（正文）+ IBM Plex Mono（标签/数据），全部**自托管**到 `static/fonts/`（4 个 woff2，共约 100KB，带 `<link rel="preload">`），移除了 Google Fonts 外链——与之前「内置 chart.js」同一个原因：外部 CDN 不可达会让页面长时间白屏。
  - **交互**：区间切换从药丸按钮改为下划线 tab（铜色细线从左展开），卡片 hover 时顶部铜线扫入，排名条与数字有入场动画，主指标数字带 620ms 缓动计数（值不变则不重播）。
  - **路由**：`/dashboard` 现在默认进入看板模式；新增 `?layout=kanban|default` 与 `?theme=dark|light` 深链参数，便于直接分享看板地址。
  - **修复/细节**：环比百分比超过 ±1000% 时显示 `>999%`（原先会出现 `+66700.0%`）；`updateMom` 补上 up/down 类名（原来颜色逻辑是死代码）；图表 x 轴禁止倾斜并降低刻度密度；排名列表底部加渐隐遮罩，滚动截断不再像渲染错误；`insights` 的 active 标签不再继承默认模式的蓝色底。
  - **响应式**：≥1500px 为 8/4 + 7/5 + 8/4 三排满屏；1120–1500px 收窄列宽；≤1120px 关闭固定行高、改为 `grid-auto-rows` 滚动；≤820px 回落单列自然滚动。
  - 涉及文件：`templates/index.html`（CSS/JS）、`static/fonts/*.woff2`（新增）、`AI Gateway.spec` 无需改动（已整目录打包 `static`）。

## [0.6.31] - 2026-10-26

### Fixed
- **大请求体 SSE 中文乱码 + `JSON parse error: Unterminated string`**：opencode 大会话（2095 条消息、约 29MB 历史，请求体 ≥4MB）走 `_proxy_stateless()` 时，中文 reasoning 全部变 mojibake，并随机中断为 `AI_JSONParseError: JSON parsing failed ... Unterminated string`。
  - **根因链**：`_proxy_stateless` 用 `resp.iter_lines(decode_unicode=True)` 但没有设置 `resp.encoding`；上游返回 `Content-Type: text/event-stream`（无 charset），`requests` 对 `text/*` 默认 **ISO-8859-1**。UTF-8 中文被按 Latin-1 解码（`继续` → `ç»§ç»­`）；更关键的是汉字 UTF-8 中间字节 `0x85`（如 `先` = `E5 85 88`、`全` = `E5 85 A8`）解码成 `U+0085`，而 `str.splitlines()` 把它当换行符，把一条 SSE JSON 从字符串中间劈开，客户端收到半截 `data:` 后 `JSON.parse` 报 `Unterminated string`。
  - **为什么只有大会话出错**：<4MB 的请求走 `_handle_stream()`，那里显式设了 `resp.encoding = "utf-8"`；≥4MB 才走 0.6.30 新增的无状态直通路径，遗漏了同一设置。
  - **修复**：新增 `_iter_sse_raw_lines()` / `_iter_sse_text_lines()`，只按 `b"\n"` 切分 SSE 行（不经 `requests.iter_lines` / `str.splitlines()`）；`_proxy_stateless()` 改为**原始字节直通**（`iter_content` 原样 `yield`，不解码、不重编码，仅把完整行按 UTF-8 解码一份给 usage 抓取）；`_handle_stream()` 同样改用字节分行；两条路径的 SSE 响应补 `charset=utf-8`。
  - 涉及文件：`app.py`（`_iter_sse_raw_lines`、`_iter_sse_text_lines`、`_handle_stream`、`_proxy_stateless`）、`tests/test_app.py`。
### Tests
- 新增 3 个回归用例：大 body 的 `0x85` 不再劈行且下游字节与上游完全一致、JSON 字符串内的 `U+2028` 不被当 SSE 行边界、小 body 路径同样保持完整 UTF-8；全部 66 个单测通过。

## [0.6.30] - 2026-10-22

### Fixed
- **43MB 请求体把网关打到全站 504（进程假死 30+ 分钟）**：2026-10-22 10:07~10:17 `https://ai-getway.fangna.tech/` 持续 `504 Gateway Time-out`，连 `GET /`、`/favicon.ico` 都超时，而 systemd 显示 `active` / `NRestarts=0`（进程活着但完全无响应）。逐层取证定位到完整链条：
  - **触发器**：opencode 发来 `Content-Length: 43936558`（43.9MB）的请求体；同客户端正常请求仅 439KB~788KB，属 100 倍异常值。
  - **内存放大 5.9 倍**：`request.get_json()` 把 body 全量解析成 Python 对象（实测 23.1MB body → 解析对象 +66.7MB），随后 `json.dumps(body).encode()` 又整份重新序列化（+46.2MB）。**同一时刻内存里有 4 份 body**：Werkzeug 缓存的原始 bytes、解析出的 dict、重序列化的 bytes、urllib3 发送缓冲 → 单请求 ≈ 250MB；`THREADS=32` 时理论峰值 4.3GB，而物理内存仅 3655MB、cgroup 上限 2000MB。
  - **这些副本不会「转发后立即释放」**：`_handle_stream(target_url, headers, body_data, ..., raw_body, ...)` 把 dict 与 bytes 都作为函数参数传入，帧在整个流式响应期间都活着。
  - **致命点：GIL**。`json` 的 C 编码器全程持有 GIL，而 waitress 是单进程多线程 —— py-spy 实测 30/32 worker 卡在 `wait (threading.py:327)`（内核 `wchan=futex_wait_queue`，即在等 GIL），仅 waitress-6 持 GIL 停在 `app.py:904` 的 `json.dumps`。
  - **卡死而非变慢**：该线程 10 秒内 `stime +997 tick`（100% 内核态）、`utime` 零增长、RSS 不增长、`minflt` 仅 +1。此时进程 `anon` 已达 2.08GB，`memory.events high` 10 秒内 11946→11966（`MemoryHigh=2000M` 持续限流），大块内存分配进入「回收失败→重试」循环，原地空转永不返回。
  - **不自愈**：`Restart=on-failure` 对「进程活着但无响应」无效，故障持续到人工重启。
  - 修复：**按 body 大小分层**。`< STATELESS_BODY_MB`（默认 4MB）走原逻辑，行为完全不变（reasoning 回填、精确记账、对话入库）；`>= 4MB` 改走 `_proxy_stateless()` **原始字节直通** —— 不解析、不重序列化、不入对话库，只透传 raw bytes 并从上游响应里抓 `usage` 记账。峰值内存从 ≈250MB 降到 ≈1 份 body。
  - 前提已实证：上游稳定返回 `usage`（10-21 全天 3548 请求 → 3398 条 `upstream usage`，`fallback count` 0 次；DB 内 `cached_tokens>0` 占比 deepseek 98.2% / mimo 96.7% / minimax 100%），因此记账不需要解析请求体。差的 150 条为 502/上游报错，本无 usage。
- **tiktoken 对「超长无空白 run」退化成 O(n²)**：`count_tokens` 直接用 `tiktoken.encode(text)`，实测连续同字符 16KB→0.094s、64KB→1.510s、**256KB→45.733s**、1MB≈11 分钟（minified JS / base64 / 长 hash / data URI 都是这种形状；有词边界的普通文本是线性的，4MB 仅 0.265s）。虽然 tiktoken 会释放 GIL（心跳线程实测 8/8 正常），但会把 worker 线程占死数分钟，线程池耗尽后同样引发 504。修复：单段文本超过 `TOKENIZE_MAX_BYTES`（默认 64KB）直接按 `len//4` 估算，不再进 tiktoken。计数只用于「上游没返回 usage」时兜底，精度损失可接受。
- **`/v1` 与 `/v1/chat/completions` 逻辑重复**：抽出 `_dispatch_proxy(by_suffix)`，两个入口共用；`?by=` 时的路径拼接规则与原实现逐字保持（`/v1` 原样透传，`/v1/chat/completions` 补 `/chat/completions`）。
- **非法 JSON body 报错不明确**：原先依赖 `get_json(force=True)` 抛 Werkzeug 400，现显式 `json.loads` 并返回 `{"error": "invalid JSON body: ..."}`；非 object 顶层返回 400。
- **托盘/worker 模式一直用 Flask 开发服务器**：`main.py` 的 `--worker` 分支直接调 `app.app.run(host='0.0.0.0', port=app.PORT)`，**绕过了 `app._serve()`** —— 0.6.28 引入的 waitress 线程池、以及本次新增的独立健康端口在打包 exe 里都没生效（每请求新建线程、并发无上限）。改为调用 `app._serve()`，桌面端与服务端行为一致。服务器部署走 `python3 app.py` 直接进 `_serve()`，不受影响。

### Added
- **请求体硬上限**：`MAX_CONTENT_LENGTH`（`MAX_REQUEST_MB`，默认 64MB）+ 413 JSON 处理器；`nginx.conf` 的 `client_max_body_size` 由 `2g` 收紧到 `64m` 与之对齐。
- **大请求并发闸门**：`_large_body_sem`（`LARGE_BODY_CONCURRENCY`，默认 4），把同时在途的大请求数钉死，峰值内存 ≈ N × body 大小；等待超 120s 返回 503。信号量在生成器 `finally` 里释放（`call_on_close` 在部分环境/测试客户端不触发，只靠它会漏放）。
- **对话入库内容收缩**：`_shrink_text()` / `_shrink_obj()` 在写库前剔除内联图片（`data:image|audio|video|application/*;base64,...` → `[image/png omitted]` 等）、裸 base64 大块（≥4096 字符）、`data`/`base64`/`b64_json`/`image_url`/`source.data` 等二进制字段，单条内容超过 `CONV_CONTENT_MAX_BYTES`（默认 256KB）截断并标注原始长度。**只影响入库副本，转发给上游的内容完全不受影响**。用于止住 `usage.db` 膨胀（已 2.8GB，磁盘 60G 已用 78%）。
- **`GET /api/health`**：轻量健康检查（不查库、不解析 body），返回 `{ok, version, threads, stateless_threshold_mb}`。
- **独立健康端口 `HEALTH_PORT`（默认 6771，仅 127.0.0.1）**：单独线程 + 独立端口，不受 waitress 线程池占满影响 —— 线程池全忙（长流式请求）时仍能应答，只有进程真正假死（GIL 被独占）才不应答。`HEALTH_PORT=0` 可关闭。
- **systemd 假死看门狗**：`ai-getway-watchdog.service` + `.timer`，每 30s 探活，连续 3 次失败（约 90s）才 `systemctl restart`，避免误杀长流式请求。把「30 分钟人工介入」压缩到「约 1.5 分钟自动恢复」。
- **`deploy.ps1` 自动同步 `nginx.conf`**：此前该文件不在同步范围，导致仓库与线上长期不一致。现在部署时 `cp` 到 `/etc/nginx/conf.d/<app>.conf`，`nginx -t` 通过才 reload，失败自动回滚旧配置。
- **`config.conf` 新增 `HEALTH_PORT=6771`**。

### Changed
- **systemd 内存水位收紧**：`MemoryHigh` 2000M→1800M、`MemoryMax` 2500M→2000M。原配置在 3.6GB 机器上既容易先进入「限流空转」而非快速失败，又曾因整机内存打满触发全机 OOM（0.6.28）。配合体量上限后正常占用仅 50~150MB，该限额只作为回归护栏。
- **`deploy.ps1` systemd 单元新增环境变量**：`HEALTH_PORT`、`MAX_REQUEST_MB=64`、`STATELESS_BODY_MB=4`、`LARGE_BODY_CONCURRENCY=4`、`TOKENIZE_MAX_BYTES=65536`、`CONV_CONTENT_MAX_BYTES=262144`；部署末尾改为探测独立健康端口。

### Tests
- 新增 `TestOversizedRequestGuards`（14 用例）：tiktoken 跳过阈值 / 小文本仍走 tiktoken、data-uri 与裸 base64 剔除、超大文本截断、content list 图片与文件块置换、413 上限、**大 body 原样透传（断言上游收到的字节与客户端发出的完全一致）**、大 body 不写对话库、usage 取自上游响应（含 cached/reasoning）、并发闸门释放、小 body 仍做 reasoning 回填；`TestHealthEndpoint`。
- 全部 63 个单测通过。

### E2E 实测
- 假上游 + 网关子进程，POST **40MB** body（内含 20 个 2MB 连续无空白 run）：`status=200`、**0.14s 完成**、峰值 RSS **97MB（增量仅 46MB）**、并发探活 `fail=0 / worst=4ms`、`usage_logs` 正确记录 `p=180000 c=42 cached=179000 reasoning=7`、`conversations` 未写入。
- 旧实现下同一请求会走 tiktoken O(n²) + `json.dumps` 持 GIL，必现假死。

## [Unreleased]

### Changed
- **`deploy.ps1` 部署前备份改为服务器本地快照**：原实现把服务器 `usage.db` 经 `gzip → split 30M → 逐块 scp` 拉回本地，受 ~0.5MB/s 带宽限制耗时十几分钟且易中断。改为在服务器本地 `tar -czf` 出 `backups/usage_YYYYMMDD_HHMMSS.tar.gz`（含 `-wal`/`-shm`），零带宽、秒级，保留最近 5 份。**不变更也不覆盖服务器数据库。**
  - 弃用 `sqlite3 .backup`：该库 1.9GB 且非 WAL，`.backup` 在持续写入下反复重试、实测卡死 10 分钟跑不完；`tar` 无此问题。
  - `tar` 对「读取中被写入的文件」返回退出码 1（warning），已放宽为容忍 0/1，避免 `set -e` 误判失败。
  - 同步排除项新增 `--exclude backups`：此前 `backups/` 未排除，本地备份碎片被一并同步到服务器（实测污染出 420MB 的 `parts_*` 目录）。
  - 移除已无用的 `$SCP_ARGS`。
- **同步 `nginx.conf` 到服务器**（0.6.28 遗留项，`deploy.ps1` 不覆盖该文件）：`ai-getway.conf` 的 `proxy_read_timeout` 300s → 180s，新增 `proxy_http_version 1.1`、`Connection ""`、`proxy_connect_timeout 5s`、`proxy_send_timeout 180s`；保留服务器独有的 `client_max_body_size 2g`。

### Deployed
- **0.6.29（含 DeepSeek 思考模式 `reasoning_content` 回填修复）此前未部署到生产**（服务器仍为 0.6.27）。本次已执行 `deploy.ps1` 并验证：服务器 `VERSION=0.6.29`、`_replay_reasoning` 代码就位、`ai-getway.service` active、`localhost:6770` 与 `https://ai-getway.fangna.tech/` 均 200。

## [0.6.29] - 2026-10-19

### Fixed
- **DeepSeek 思考模式 + tools 全链路 400，对话频繁中断**：请求携带 `tools` 参数时，DeepSeek 思考模式契约要求历史中**每个** assistant 消息的 `reasoning_content` 原样回传（含未发生工具调用的轮次），缺失即返回
  `400 invalid_request_error: The 'reasoning_content' in the thinking mode must be passed back to the API.`
  而 `reasoning_content` 不属于 OpenAI 标准 schema，客户端（WorkBuddy / opencode / Cursor 等）重建历史时普遍只保留 `role` / `content` / `tool_calls`，该字段被丢弃 —— 于是**第一轮工具调用之后的所有请求全部 400**，表现为「对话总是中断」。
  实测日志佐证：2026-10-19 单日 17 次会话中断，其中 9 次为该 400（`stream loop failed: 400 ... reasoning_content ... must be passed back`），且集中在工具调用高频时段。
  - 修复落点选在网关：它是整条链路（客户端 → 网关 → opencode zen go → DeepSeek）中**唯一同时看到「上游响应」与「下一轮请求」**的一环，客户端不可控，只能在此补偿。
  - 新增 `_reasoning_cache_put()` / `_reasoning_cache_get()`：响应侧缓存 `reasoning_content`，采用**双键**——`tool_call id`（首选，唯一且稳定）+ `assistant content` 的 sha1 指纹（兜底，覆盖无 tool_calls 的收尾轮次）。缓存为进程内 LRU，默认上限 4096 条（`REASONING_CACHE_MAX` 可调）。
  - 新增 `_replay_reasoning()`：请求侧为历史中缺失 `reasoning_content` 的 assistant 消息回填缓存值。仅在请求携带 `tools` 时介入（该契约的触发条件），**已带 `reasoning_content` 的消息原样保留、绝不覆盖**。
  - 缓存写入挂在流式响应的 `generate()` 收尾（`finally`）与非流式响应解析处，不依赖 `response.call_on_close` —— 后者在部分运行环境/测试客户端下不触发，会导致缓存写空、修复失效。
  - 流式解析中**将正文与思维链分开累计**（`pure_content` / `reasoning_acc`）：正文用于指纹键，思维链单独留存用于回填；此前二者混入同一 `complete_content`，会使指纹键失配。
  - LRU 操作加 `threading.Lock`：waitress 为多线程（默认 `THREADS=32`），`OrderedDict.move_to_end` / `popitem` 非原子，并发淘汰时可能抛 `KeyError` 把请求打成 500。
  - 新增开关 `ENABLE_REASONING_REPLAY`（默认 `1`）：置 `0` 可完全关闭回填，行为退回纯透传。
  - 涉及文件：`app.py`、`tests/test_app.py`（新增 `TestReasoningReplay` 8 用例）、`tests/e2e_reasoning_replay.py`（新增）。

### Added
- **`tests/e2e_reasoning_replay.py`**：端到端契约验证。内置假上游，复刻 DeepSeek 的校验逻辑，走「网关收到第一轮带 reasoning+tool_calls 的响应 → 客户端按标准 schema 重建历史（丢弃 reasoning_content）→ 发第二轮」完整流程。修复前第二轮返回 400，修复后返回 200 且上游收到回填的 reasoning。用 Flask 测试客户端 + 内存假上游实现，不占用真实端口。
- **`TestReasoningReplay` 单元测试**：覆盖 tool_call id 命中、content 指纹兜底、未携带 tools 时不介入、不覆盖客户端已有值、开关关闭、LRU 淘汰、空 reasoning 不入缓存、多线程并发读写无异常。

## [0.6.28] - 2026-09-28

### Fixed
- **服务器内存耗尽导致整机假死、网关全面不可用**：生产服务器（`162.14.120.45`）内存被打满（监控实测 97.04% / 3324MB），SSH 登录报 `Connection timed out during banner exchange`（TCP 可握手但 sshd 连 banner 都发不出），`ai-getway.fangna.tech` 的请求全部无限挂起，opencode 侧表现为 `ProviderHeaderTimeoutError: Provider response headers timed out after 300000ms`。逐层定位到三处叠加原因：
  - **① 重型 tokenizer 常驻内存**：`requirements.txt` 装着 `transformers`，而服务器以 `python3 app.py` 直跑源码（不像 exe 打包时 `AI Gateway.spec` 已把 `transformers` 列入 `excludes`），因此 `get_deepseek_tokenizer()` 会真实加载 `transformers` + DeepSeek 词表并常驻数百 MB 内存。改为默认禁用——新增环境变量 `ENABLE_DEEPSEEK_TOKENIZER`（默认 `0`），关闭时直接走轻量估算，与 exe 行为对齐；`requirements.txt` 中 `transformers` 降级为可选依赖并注释说明。
  - **② Flask 内置开发服务器上生产**：`app.run(host="0.0.0.0", port=PORT)` 每请求新建线程且并发无上限，并发流式请求下线程数与内存双双失控。改为优先使用 waitress（线程池有硬上限，`THREADS` 可配，默认 32），未安装时回落开发服务器并打印警告。
  - **③ nginx 超时过长放大故障**：`proxy_read_timeout 300s` 使后端假死时 nginx 干等 5 分钟才释放连接，造成连接大量堆积（监控实测 TCP 连接数稳定在 225~234）。收紧为 `proxy_read_timeout 180s` / `proxy_send_timeout 180s` / `proxy_connect_timeout 5s`（`read_timeout` 是「两次数据之间的间隔」而非总时长，180s 对连续流式输出极宽松，如需更长可调回），并补 `proxy_http_version 1.1` + `Connection ""` 以复用上游长连接。
- **tiktoken 编码表重复构建**：`count_tokens` 每次调用都走 `tiktoken.get_encoding("cl100k_base")`。改为 `_get_tiktoken_encoder()` 单例缓存，避免重复加载词表。
- **日志泄漏 API key**：`_proxy_passthrough` 的 `INCOMING HEADERS` 直接打印 `dict(request.headers)`，会把 `Authorization` 明文写进日志文件。新增 `_mask_headers()` 统一脱敏（`authorization` / `x-api-key` / `api-key` / `cookie` / `proxy-authorization`）。

### Changed
- **systemd 服务加内存护栏**：`deploy.ps1` 生成的单元新增 `MemoryHigh=2000M`、`MemoryMax=2500M`、`MemorySwapMax=512M`、`OOMPolicy=stop`，超限时只回收本服务，避免再次拖死整机；`Restart=always` + `RestartSec=5` 改为 `Restart=on-failure` + `RestartSec=10`，并加 `StartLimitIntervalSec=120`/`StartLimitBurst=5` 抑制重启风暴；新增 `ENABLE_DEEPSEEK_TOKENIZER=0`、`THREADS=32`、`PYTHONUNBUFFERED=1` 环境变量。
- **部署流程补 swap 与内存快照**：`deploy.ps1` 远程步骤在装依赖前检测并创建 2G `/swapfile`（并写入 `/etc/fstab`）、设置 `vm.swappiness=10`，为内存触顶提供缓冲；部署末尾输出 `free -m` 与服务实际占用 `MemoryCurrent`，便于确认水位。
- 涉及文件：`app.py`、`requirements.txt`、`deploy.ps1`、`nginx.conf`。**注意 `nginx.conf` 不在 `deploy.ps1` 的同步范围内，需手动覆盖到服务器 `/etc/nginx/` 对应站点配置后 `nginx -t && systemctl reload nginx`。**

## [0.6.27] - 2026-09-15

### Fixed
- **Web 看板打开极慢（20–30 秒）**：`templates/index.html` 在 `<body>` 内同步加载外部 CDN `https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js`。该 CDN 在部分网络（如国内多数宽带）不可达，实测下载超时约 21 秒、0 字节，且该脚本位于全部业务 JS 之前，导致整个看板的数据加载与图表渲染被阻塞直到超时。改为随包内置：新增 `static/chart.umd.min.js`（Chart.js v4.4.7，约 201KB），`index.html` 引用改为同源 `/static/chart.umd.min.js`，彻底去掉运行时对外部 CDN 的依赖。
  - 涉及文件：`templates/index.html`（引用改为 `/static/chart.umd.min.js`）、`static/chart.umd.min.js`（新增，随 `AI Gateway.spec` 的 `static` 目录一并打包）。
- **`deploy.ps1` 无法运行/连接失败，部署脚本整体失效**：三处问题叠加。① `"$DEPLOY_USER@$DEPLOY_HOST:/tmp/..."` 中 `$DEPLOY_HOST:` 被 PowerShell 解析为「变量+冒号」在解析期直接报错，脚本根本无法启动；② SSH 检查/建目录/远端执行三处只用了 `"$DEPLOY_HOST"`，漏掉用户名，导致 ssh 以当前 Windows 用户（如 `15680`）连接而 `Permission denied`；③ `$SSH_ARGS` 是单个字符串，传给 ssh/scp 会被当成一个参数；且 ssh 用 `-p`、scp 用 `-P` 指定端口，共用同一份参数会让 scp 把端口号 `22` 误当文件。
  - 修复：统一改用 `${DEPLOY_USER}@${DEPLOY_HOST}`；`$SSH_ARGS`/新增 `$SCP_ARGS` 改为参数数组分别用于 ssh（`-p`）与 scp（`-P`）；`Get-Content "config.conf"` 增加 `-Encoding UTF8`，避免中文注释行被按 ANSI 解读时吞掉下一行，导致 `VERSION`、`WEB_PORT` 解析为空。
  - 涉及文件：`deploy.ps1`。

## [0.6.26] - 2026-08-29

### Fixed
- **修复 `opencode.ai/zen/go/v1`（OpenCode Go）经网关代理失败**：OpenCode Go 端点对 `/chat/completions`、`/models` 强制要求 `x-opencode-session` 会话头，缺失时返回 400 `MissingSessionID`，且要求请求携带 `coding-agent` 风格的 `User-Agent`。此前网关 `_proxy_passthrough` 只原样透传 `Authorization`/`User-Agent`，既不转发也不生成该头，导致走网关调用 opencode-go 系列模型（如 `deepseek-v4-flash`）全部失败。
  - 新增 `_upstream_headers(body, model)` 辅助函数：透传上游所需头；`User-Agent` 若缺失或为通用库名（python-requests/curl/node/axios/okhttp/ai-sdk 等）则替换为 `ai-gateway/{VERSION}`；`x-opencode-session` 若客户端未携带，则从 `model + 首条消息/输入` 派生稳定的 `gw-` 前缀会话 ID（同一会话内不变，利于上游路由与缓存）。
  - 新增 `GET /v1/models?by=` 透传路由，补上此前缺失的模型列表接口（修复 `/models` 404）。
   - 涉及文件：`app.py`（`_upstream_headers`、`_proxy_passthrough`、`/v1/models`）、`tests/test_app.py`（新增 `TestUpstreamHeaders` 5 用例 + `TestProxyModels` 2 用例，覆盖会话派生/稳定/转发、通用 UA 替换、agent UA 透传、models 透传与 `?by=` 校验）。
### Changed
- **部署前自动备份服务器数据库到本地**：`deploy.ps1` 同步代码前先备份 `usage.db`（位于 `/opt/ai-getway/data/usage.db`，当前 884M 且服务正在使用，`deploy.ps1` 已排除 `*.db*` 绝不覆盖/删除）。因服务器带宽有限（约 0.5MB/s）且单文件大 scp 会卡死，采用 gzip 压缩（884M→264M）+ 远程 split 成 30M 小块 + 逐块 scp + 本地合并（`cmd type`）。备份存 `backups/usage_<host>_<timestamp>.db.gz`（`.gitignore` 已排除，不入库）。涉及文件：`deploy.ps1`、`.gitignore`。

## [0.6.24] - 2026-08-28

### Changed
- **对话存储合并到单库**：`conversations`、`system_prompts`、`message_contents`、`messages` 四张表从独立 `conversations.db` 合并进 `usage.db`（`CONV_DB_PATH` 指向 `DB_PATH`），全量数据（用量 + 对话）统一存 `%APPDATA%/AI Gateway/usage.db` 单文件，方便备份、同步、导入导出。
- 升级后首次启动自动合并旧库：检测到旧版独立 `conversations.db` 存在且新库无对话数据时，通过 `ATTACH` 把四张表整体搬入 `usage.db`（保留原 id、内容哈希去重结构），合并后 VACUUM 压缩。**旧 `conversations.db` 保留不删除**，可确认无误后手动删除以释放空间。
- `/api/db/upload` 导入时补齐 `ensure_conv_schema`，保证导入的单文件库包含完整对话表结构（兼容旧版导出的 usage.db）。
- 涉及文件：`app.py`（路径定义、`_merge_legacy_conv_db`、`init_db`、`api_db_upload`）、`tests/test_app.py`（新增合并迁移 + 幂等 2 个用例）。

## [0.6.22] - 2026-08-28

### Fixed
- **导入 usage.db 报「导入失败」**：`/api/db/upload` 原来用「删除现有库 + `shutil.move` 移入新文件」的方式替换数据库。服务运行中 SQLite 文件被写锁占用（网关实时记录用量），Windows 上删除文件直接抛 `WinError 32`，而重试仅 5×0.3s=1.5s，一次长请求就会耗尽重试导致 500。且替换后残留的旧 `-wal`/`-shm` 会被 SQLite 误套用到新库上，存在数据错乱隐患。
- 修复方式：改用 SQLite 官方 backup API 原地还原——先把上传文件校验（`integrity_check` + schema 补齐），再通过 `src.backup(dst)` 将内容整体覆盖进现有库，全程不删除/移动主文件，天然避开文件占用竞态；目标连接设 `timeout=15s` 容忍瞬时写锁，完成后 `wal_checkpoint(TRUNCATE)` 落盘。非法文件（非 SQLite）返回 400 并提示具体原因。
- 涉及文件：`app.py`（`api_db_upload`）、`tests/test_app.py`（新增 2 个导入用例：覆盖还原 + 非法文件拒绝）。

## [0.6.20] - 2026-08-14

### Added
- **托盘菜单显示版本号**：`main.py`（exe 托盘入口）和 `scripts/tray.py`（开发模式托盘启动器）的右键菜单顶部新增只读项 `AI Gateway vX.Y.Z`，方便确认当前运行版本。版本号通过正则从 `app.py` 的 `VERSION` 常量读取（避免 `import app` 触发 Flask/DB 初始化）。

## [0.6.18] - 2026-08-14

### Added
- **支持 OpenAI Responses API 直通与记账**：`@ai-sdk/openai`（opencode 的 DeepSeek V4-Flash 适配）会调用 `/responses` 端点而非 `/chat/completions`，请求体结构不同（无 `messages`，改 `input` 数组；usage 字段为 `input_tokens`/`output_tokens`）。本次为 `_proxy_passthrough` 增加 Responses 识别与记账适配：
  - `_extract_content` 支持从 Responses 的 `output` 数组提取 `output_text`/`text`/`reasoning_text`。
  - `_extract_input_messages` 从 `input` 数组提取消息用于对话记录。
  - `_extract_cached_tokens` 补充 `input_tokens_details.cached_tokens`。
  - `_handle_stream` 识别 `response.output_text.delta` 累积内容，`response.completed`/`response.incomplete` 事件提取 `response.usage`；`prompt_tokens`/`completion_tokens` 兼容 `input_tokens`/`output_tokens`。
  - `_proxy_passthrough` 非流式分支同步兼容 Responses 的 usage 字段映射。
- 数据库结构无变更；usage 记录中 `reasoning_tokens` 仍从 `output_tokens_details.reasoning_tokens` 提取。

## [0.6.16] - 2026-08-13

### Fixed
- **活动分析报"未配置分类模型"**：exe 运行时代码从 exe 同目录的 `dist/config.json` 读取配置，但构建流程从不把项目根 `config.json` 同步到 `dist`，导致 `insights` 块（分类模型）缺失；且 `models` 表为空、前端又不传 `model` 参数，三个来源全部落空后返回 400 报错。
- 修复方式：`build.vbs` 在构建成功后自动将项目根 `config.json` 复制到 `dist\config.json`，保证 exe 每次拿到最新配置（含 `insights` 分类模型）；当前 `dist\config.json` 已同步含 `insights` 块的版本。

## [0.6.15] - 2026-08-13

### Changed
- **对话存储重构（拆表 + 内容哈希去重）**：旧结构 `conversations` 单表把整段消息 JSON 存在 `messages` 大字段里，重复内容（system prompt、tool 输出、缓存助手回复）每条会话存一份，导致库膨胀到 **7.5GB**。现拆为 4 张表，重复内容只存一份，正式迁移后压缩至 **约 450MB（-94%）**。
- 升级后首次启动自动迁移旧库（无需手动操作），迁移前自动备份为 `conversations.db.bak`。
- 现有 API 返回结构与旧版完全一致，外部调用方无感知。

### 新数据库结构（`%APPDATA%/AI Gateway/conversations.db`）

#### 1. `conversations` — 会话元数据（最外层表）
一次 AI 对话一行，存与内容无关的统计和摘要字段：

| 字段 | 含义 |
|---|---|
| `id` | 会话 ID（**保留旧值**，与 `usage_logs.conversation_id` 关联） |
| `model` / `provider` / `endpoint` | 请求的模型 / 平台 / 端点 |
| `request_time` | 请求时间 |
| `system_prompt_id` | 外键 → `system_prompts.id`（该会话用的系统提示词） |
| `last_user_message` | 最后一条用户消息（摘要） |
| `response` | 最终回复文本（摘要） |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | Token 统计 |
| `duration_ms` | 耗时（毫秒） |

> **做用量/性能分析用这张表**：`SELECT model, SUM(total_tokens), AVG(duration_ms) ... GROUP BY model`。

#### 2. `messages` — 消息序列（会话 → 内容 的桥）
记录每条消息属于哪个会话、第几条、什么角色、内容存哪：

| 字段 | 含义 |
|---|---|
| `id` | 消息 ID |
| `conversation_id` | 外键 → `conversations.id` |
| `seq` | 消息序号（0,1,2…），按此还原消息顺序 |
| `role` | 消息角色：`system` / `user` / `assistant` / `tool` |
| `content_id` | 外键 → `message_contents.id` |

> 这张表本身**不存内容**，只存指向内容的指针。同一条去重内容可能被多张表的 `content_id` 引用。

#### 3. `message_contents` — 去重消息内容（核心去重表）
按 `(content_hash, extra_hash)` 唯一约束，相同内容只存一份：

| 字段 | 含义 |
|---|---|
| `content_hash` | SHA-256 哈希（`s:` 开头=字符串，`l:` 开头=数组） |
| `extra_hash` | 附加字段哈希（`tool_call_id`、`tool_calls`、`name` 等），无附加则为空串 |
| `content` | 消息正文（字符串或 JSON 数组） |
| `extra` | 附加字段 JSON（如工具调用信息） |
| `is_list` | 1=content 是数组（OpenAI 多段 content），0=是字符串 |

> **这张表是空间收益来源**：242 万条消息 → 仅 7.3 万份唯一内容（去重率 97%）。**不要直接对它做业务查询**（没有会话/时间维度），它只负责"存"。

#### 4. `system_prompts` — 去重系统提示词
| 字段 | 含义 |
|---|---|
| `id` | 主键 |
| `content_hash` | SHA-256（唯一约束） |
| `content` | 提示词全文 |

> 同样的 system prompt 全局只存一份（本次 2.4 万会话 → 仅 234 份）。

### 表关系
```
conversations (1) ──┬──< (N) messages ──> (1) message_contents
                    └────> (1) system_prompts
```
- 1 个会话含 N 条消息（`conversations.id` = `messages.conversation_id`）
- N 条消息可指向同一份去重内容（`messages.content_id` = `message_contents.id`）
- 1 个会话引用 1 份系统提示词（`conversations.system_prompt_id` = `system_prompts.id`）

### 如何查数据（SQL 示例）
```sql
-- 1. 按会话取完整消息（还原旧版 messages JSON 数组）：
SELECT m.seq, m.role, mc.content, mc.extra, mc.is_list
FROM messages m
JOIN message_contents mc ON m.content_id = mc.id
WHERE m.conversation_id = 12345
ORDER BY m.seq;

-- 2. 取某会话的系统提示词：
SELECT sp.content
FROM conversations c
JOIN system_prompts sp ON c.system_prompt_id = sp.id
WHERE c.id = 12345;

-- 3. 按内容反查（哪些会话用过同一条 system prompt）：
SELECT c.id, c.request_time
FROM conversations c
WHERE c.system_prompt_id = (SELECT id FROM system_prompts WHERE content_hash = 's:xxxx');

-- 4. 删除某会话时清理不再被引用的去重内容：
DELETE FROM messages WHERE conversation_id = 12345;
DELETE FROM conversations WHERE id = 12345;
-- 若某 content_id 无任何 messages 引用，再删 message_contents 对应行
```

### 分析该用哪些数据
| 分析目标 | 用哪张表 | 说明 |
|---|---|---|
| 请求数 / Token / 耗时 / 模型 / 平台统计 | `usage_logs`（usage.db） | 按 `request_time` 聚合（`/api/usage` 用的就是它） |
| 会话级 Token / 耗时 / 模型 | `conversations` | 一条会话一行，直接 SUM/AVG/GROUP BY |
| 对话内容 / 主题分类 / 语义分析 | `conversations` + `messages` + `message_contents` | 先查会话元数据，再 JOIN 重建消息数组 |
| 某模型/某平台用了哪些 system prompt | `conversations` + `system_prompts` | 按 `system_prompt_id` 关联 |
| 内容去重收益 / 空间占用审计 | `message_contents` | 只看唯一内容有多少份 |

## [0.6.0] - 2026-05-24

### Added
- 各模型使用量统计：与各平台使用量同列（模型、请求数、输入、输出、Token/s），置于各平台使用量下方。
- 托盘图标左键单击直接打开看板（右键菜单保持不变）。

## [0.5.0] - 2026-05-21

### Added
- 各平台统计新增 **Token/s** 列，按平台汇总计算平均生成速率（`completion_tokens × 1000 / duration_ms`）。
- 最近请求列表新增 **Token/s** 列，单条请求实时展示生成速度。
- 托盘图标重启时显示红色圆点提示，服务恢复后自动消失。
- 请求日志新增 `duration_ms` 字段，自动记录每条请求的耗时（兼容旧数据库）。

### Fixed
- 修复「各平台使用量」表格中表头与数据的错位（请求数与输入 Token 列对调）。

## [0.4.0] - 2026-05-21

### Added
- 统计卡片支持环比显示：所有时间维度（1h/24h/7d/30d/今日/昨日）均展示与上一周期的百分比对比。
- 「今日」维度环比精确对比「昨日同时段」（如今天 0:00~11:40 对比昨天 0:00~11:40）。
- 其他维度按自身时长平移对比（如 24h 对比前 24h，7d 对比前 7d）。

### Changed
- 统一数据目录为 `%APPDATA%\AI Gateway`，避免 vbs 与 exe 启动时使用不同数据库。
- `config.json` 中的相对路径 `db_path` 现在基于 `EXE_DIR` 解析。

## [0.3.0] - 2026-05-20

### Added
- PyInstaller 单文件 exe 构建支持（`main.py` + `AI Gateway.spec`），输出约 26 MB 的独立可执行文件。
- 系统托盘支持日期命名日志文件（`logs/AI-Gateway-YYYY-MM-DD.log`）。
- Windows 启动列表显示项目图标和正确名称（通过 `.lnk` 快捷方式实现）。

### Changed
- 开机自启动由注册表改为启动文件夹 `.lnk` 快捷方式，同步显示在 Windows 设置 → 应用 → 启动。
- 移除 `config.json` 中的 `autostart` 字段，完全由托盘菜单/系统设置控制。
- 目录整理：`build/`、`dist/`、`logs/` 等加入 `.gitignore`。

### Removed
- 移除 Kimi 官方 `estimate-token-count` 精确估算接口（因认证问题频繁失败），统一使用通用 token 估算。

## [0.2.0] - 2026-05-20

### Added
- Windows 托盘启动器支持开机自启动，可通过 `config.json` 的 `autostart` 字段或托盘右键菜单一键开关。

### Changed
- 开机自启动配置由独立 `setup-autostart.bat` 脚本改为 `config.json` 统一管理。

### Removed
- `setup-autostart.bat`（自启动功能已集成至 `scripts/tray.py`）。

## [0.1.0] - 2026-05-19

### Added
- 用量面板「各平台本月用量」新增展示：调用次数、输入 Token 合计、输出 Token 合计。
- 各平台本月用量按调用次数降序排列。
- 最近请求分页新增「上一页」「下一页」导航，页码过多时自动缩略显示（`...`）。
- 新增 `/api/recent` 接口，支持后端真分页（`page` + `page_size`）。
- 后端 `/api/usage` 接口新增 `group_by=minute` 聚合粒度支持。

### Changed
- 后端 `/api/usage` 接口的月度统计查询中增加 `SUM(prompt_tokens)` 与 `SUM(completion_tokens)` 聚合字段。
- 前端「各平台本月用量」由列表改为表格展示，字段更清晰。
- 最近请求默认展示条数由 50 条调整为 100 条，前端分页大小同步调整为 100。
- **目录整理**：`tray.py` 移至 `scripts/`、`icon.png` 移至 `static/`，根目录更清爽。
- 用量趋势图表按时间范围自动切换聚合粒度：1 小时→分钟、24 小时→小时、7 天/30 天→天。
- 「各平台本月用量」改为「各平台使用量」，统计数据随时间范围联动。
- 「最近请求」列表随时间范围联动过滤。

### Fixed
- 修复分页页码平铺过多导致展示异常的问题。
- 修复分页页码可能越界（小于 1）的边界情况。
- 修复分页实际未生效的问题（后端改为真分页，前端按页请求数据）。
- 修复同一小时内多模型请求导致用量趋势图表 X 轴出现重复刻度的问题（前端按 period 合并后再绘图）。
- 修复「各平台使用量」未随时间范围变化的问题（后端 billing 查询改用传入的 start/end 参数）。
