# 修复网关 504：43MB 请求体触发 GIL 冻结

日期：2026-10-22
服务器：162.14.120.45 (ai-getway.fangna.tech)

## 现象
- 2026-10-22 10:07~10:17 期间 `https://ai-getway.fangna.tech/` 持续 504
- nginx：`upstream timed out (110)`，**连 `GET /`、`/favicon.ico` 也超时**
- systemd 显示 `active`、`NRestarts=0`（进程活着但完全无响应）

## 根因链（证据）

| 环节 | 证据 |
|---|---|
| 触发器：43.9MB 请求体 | journal `INCOMING HEADERS ... Content-Length: 43936558`（同客户端正常请求 439KB~788KB，异常 100 倍） |
| 内存耗尽 | `memory.stat anon = 2.08GB`；`memory.events high` 10 秒内 11946→11966（`MemoryHigh=2000M` 持续限流）；`MemoryCurrent=2.13GB` |
| GIL 独占 | py-spy：30/32 worker 卡在 `wait (threading.py:327)`（内核 wchan=futex_wait_queue），仅 waitress-6 持 GIL 停在 `app.py:904` |
| 卡死非变慢 | waitress-6 10 秒内 `stime +997 tick`（100% 内核态）、`utime` 零增长、RSS 不增长、minflt 仅 +1 → 原地空转 |
| 全局冻结 | `json.dumps` 的 C 编码器全程持 GIL → 30 worker + accept 线程全部饿死 → 网关零响应 |
| 上游也卡 | 另一线程卡在 `ssl.sendall`（`count:0`，一字节未发出）向 opencode.ai 发 792KB |
| 504 表现 | nginx `proxy_read_timeout 180s` → 所有请求（含首页）超时 → 504 |
| 假死不自愈 | `Restart=on-failure` 对「进程活着但无响应」无效 → 卡死持续 30+ 分钟需人工重启 |
| 副作用 | usage.db 涨到 **2.8GB**（43MB 请求体整段入库 `conversations`/`message_contents`）；磁盘 60G 已用 78% |

## 本地实测数据（复现）

### 单请求内存放大
23.1MB 请求体（16830 个 message 对象）：

| 步骤 | 耗时 | 峰值额外内存 |
|---|---|---|
| `json.loads`（Flask `get_json`） | 0.42s | +66.7MB |
| `count_messages_tokens`（tiktoken 全量） | 3.81s | +1.1MB |
| `json.dumps+encode`（app.py:904） | 0.72s | +46.2MB |

→ **单请求 136MB，放大 5.9 倍**；×32 线程 = **4.3GB**（物理内存 3655MB，cgroup 上限 2000MB）

### tiktoken O(n²) 地雷
`count_tokens` → `tiktoken.get_encoding("cl100k_base").encode(text)`：

| 输入 | 耗时 | ns/byte |
|---|---|---|
| 16KB 连续同字符 | 0.094s | 5,721 |
| 64KB 连续同字符 | 1.510s | 23,038 |
| 256KB 连续同字符 | **45.733s** | 174,458 |
| 256KB 有词边界普通文本 | 0.019s | 71 |
| 4MB 有词边界普通文本 | 0.265s | 63 |
| 1MB 源码风格 | 0.210s | 200 |

→ 退化只发生在「超长无空白 run」（minified JS / base64 / 长 hash / data URI）被 BPE 预分词器切成单个巨型 piece 后。
→ **tiktoken 释放 GIL**（心跳线程 8/8 正常），所以它不会冻结全站，但会**占死 worker 线程数分钟**（线程池耗尽 → 排队 → 同样 504）。

## 修复方案

### 架构调整：转发路径无状态，记账路径按需

```
POST /v1?by=...
  │
  ├─ body < 4MB  ──→ 完整路径（现状）：json.loads → _clean_messages/_replay_reasoning
  │                          → 精确 token → 过滤后入库对话
  │
  └─ body >= 4MB ──→ 无状态路径：raw bytes 原样转发上游
                             ├─ 前缀扫描（前 1MB 正则）取 model / stream
                             ├─ token 记账改用上游响应里的 usage（本来就优先用）
                             ├─ 跳过对话入库
                             ├─ 无 json.dumps → 不再长时间持 GIL
                             └─ 峰值内存 ≈ body 大小（43MB 请求 ≈ 90MB，而非 250MB）
```

关键前提（已核实）：**token 记账本来就优先用上游响应里的 `usage`**（`app.py:936-938`；流式路径 `_handle_stream` 的 `usage_data`），
本地 token 计数只是「上游没返回 usage 时」的兜底 → 无状态路径不需要解析请求体。
唯一需要从 body 拿的是 `model`（看板分组用）和 `stream`（决定响应处理方式）→ 前缀扫描足够
（ai-sdk 生成的 payload 里 `model` 就在开头）。

### 1. 消除 tiktoken O(n²) 地雷
- `count_tokens`：单段文本超过阈值（如 64KB）时改用 `len//4` 估算，**不再进 tiktoken**（token 数仅用于记账，误差可接受）

### 2. 请求体硬上限
- `app.py`：`MAX_CONTENT_LENGTH`（env `MAX_REQUEST_MB`，默认 64MB），超限 413
- `nginx.conf`：`client_max_body_size` 2g → 64m（与应用一致）

### 3. 大请求并发闸门（钉死峰值内存）
- body >= 4MB 的请求全局最多同时 N 个（默认 4）持闸，其余排队
- 峰值 = 4 × body 大小，可控

### 4. 只入库有效对话，不入库图片类大数据
- `message_contents` 入库前过滤：
  - `data:image/...;base64,...` 图片块 → 剔除（只留占位符 + 原始字节数）
  - `attachments` 字段（opencode 风格）→ 不存
  - 单条内容超过 256KB → 截断 + 记原始长度/哈希
- 止住 usage.db 膨胀（已 2.8GB，磁盘 60G 已用 78%）

### 5. 健康检查 watchdog（假死自愈）
- systemd timer 每 60s `curl -m 10 http://127.0.0.1:6770/`，连续 2 次失败 → `systemctl restart ai-getway`
- 把「30 分钟人工介入」压缩到「~2 分钟自动恢复」
- 新增 `/api/health` 轻量端点（不查库，供 watchdog 使用）

### 6. 内存水位
- 调整 `MemoryHigh`/`MemoryMax`，配合体量上限，避免正常流量被限流饿死

## 默认阈值（可改）
| 参数 | 默认 | 环境变量 |
|---|---|---|
| 无状态路径阈值 | 4MB | `STATELESS_BODY_MB` |
| 请求体硬上限 | 64MB | `MAX_REQUEST_MB` |
| 大请求并发闸门 | 4 | `LARGE_BODY_CONCURRENCY` |
| tiktoken 单段上限 | 64KB | `TOKENIZE_MAX_BYTES` |
| 入库单条上限 | 256KB | `CONV_CONTENT_MAX_BYTES` |

## 验证
- 单测：大 body 走无状态路径（上游收到原始字节不变）、model 提取正确、超限 413、tiktoken 跳过、对话过滤掉 base64
- 压测：并发 43MB 请求不再冻结全站，`GET /` 始终 200
- 部署后：`systemctl is-active` + `curl localhost:6770` + `curl https://ai-getway.fangna.tech/` 均 200
