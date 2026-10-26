# 任务记录：看板模式重做「黑曜石账本」（v0.6.32）

## 目标
看板模式全屏重做，要求「大气、高级」；`/` 默认布局不动。

## 设计方向
- 概念：年报 / 私人账簿（Obsidian Ledger）。靠字号对比、发丝线、编号、留白出高级感，不靠装饰。
- 版式：刊物式报头（Bodoni 字标 + 双线 + 刻度尺）→ 头版大数字（6/3/3）→ 01–06 编号面板 → 四角裁切标记 + 内嵌细框 + 颗粒。
- 配色：暖墨近黑 `#080706` + 黄铜 `#d9b979`/`#f3dda9` + 铜 `#d2633c`；浅色为象牙纸 `#f1ece1` + 深铜墨。
- 字体：Bodoni Moda / Jost / IBM Plex Mono，**自托管**到 `static/fonts/`（100KB），去掉 Google Fonts 外链。

## 完成情况
- [x] 重写 `[data-layout="kanban"]` 全部样式（~600 行）
- [x] 字体下载 + `@font-face` + preload
- [x] `?layout=` / `?theme=` 深链；`/dashboard` 默认进看板
- [x] 主数字缓动计数、环比 >999% 截断、up/down 类名修复
- [x] 图表配色改黄铜系、x 轴禁止倾斜、图例右对齐
- [x] 三档响应式（1500 / 1120 / 820）
- [x] 66 个单测通过；未碰 app.py 逻辑

## 审查
- 用 headless Chrome 逐档截图核对（1920×1080 / 1440×900 / 1100×800 / 1280×800，明暗两套）
- 踩坑：**Jinja 模板有缓存**（`TEMPLATES_AUTO_RELOAD` 未开），改完 HTML 必须重启进程才生效，否则会被旧页面误导
- 踩坑：`grid-template-rows` 写死 4 行，在窄屏换成 6 排后隐式行高度塌成 0，多个面板叠在一起；窄屏必须重置成 `grid-template-rows: none` + `grid-auto-rows`
- 踩坑：headless + `--virtual-time-budget` 不会推进 CSS 动画，`animation-fill-mode: both` 的元素会停在 opacity 0，截图看起来「面板消失」；加 `--force-prefers-reduced-motion` 才是真实布局

# 上一任务：大 body SSE 中文乱码 / Unterminated string（v0.6.31）

## 现象
- 2026-10-26 opencode 大会话（2095 条消息、约 29MB 历史）请求 deepseek-v4.1-flash 时，reasoning 中文全变 `å¾...` 乱码
- 随机中断：`AI_JSONParseError: JSON parsing failed ... Unterminated string`；同一会话小请求正常

## 根因
1. 请求体 ≥4MB → 走 `_proxy_stateless()`
2. `resp.iter_lines(decode_unicode=True)` 未设 `resp.encoding`；上游 `text/event-stream` 无 charset，requests 按 ISO-8859-1 解码
3. 中文 mojibake；汉字 UTF-8 中间字节 `0x85`（先/全）→ `U+0085`，被 `str.splitlines()` 当换行，JSON 从字符串中间劈开

## 方案
- 新增 `_iter_sse_raw_lines()` / `_iter_sse_text_lines()`：只按 `b"\n"` 分行
- `_proxy_stateless()` 原始字节直通；`_handle_stream()` 同样字节分行；SSE 响应补 `charset=utf-8`
- 回归测试覆盖 `0x85`、`U+2028`、跨包 UTF-8

## 完成情况
- [x] app.py 修复
- [x] tests/test_app.py 新增 3 个回归用例（66 全过）
- [x] VERSION → 0.6.31、CHANGELOG、会话记忆
- [x] 部署 + 生产验证：`/api/health` 返回 `0.6.31`，服务器 app.py 已含 `_iter_sse_raw_lines`（桌面 exe 未重建）

# 上一任务：大请求体触发 GIL 冻结导致全站 504（v0.6.30）

## 现象
- 2026-10-22 `https://ai-getway.fangna.tech/` 返回 504（nginx/1.26.3），持续 30+ 分钟
- 进程活着（systemd active），但连 `GET /` 都不响应；`Restart=on-failure` 不会触发

## 根因（全部有实测证据）
1. 单次 43.9MB 请求体（正常 0.4~0.8MB）
2. `request.get_json()` 全量解析 → 内存放大 5.9 倍（23MB → 136MB），随后 `json.dumps` 整份重序列化
3. json 的 C 编码器**全程持 GIL**；py-spy 显示 30/32 worker 卡在 `futex_wait_queue`，仅 1 线程持 GIL 卡在 `app.py:904`
4. 该线程 10s 内 `stime +997 tick`（100% 内核态）、`utime` 零增长、RSS 不增、minflt +1 → 进程撞 cgroup `MemoryHigh=2000M` 后陷入「回收失败→重试」死循环
5. 顺带发现 `tiktoken.encode()` 对超长无空白 run 是 O(n²)：256KB 需 45.7s，1MB≈11 分钟

## 方案：按 body 大小分层
- `< STATELESS_BODY_MB(4)`：原逻辑（reasoning 回填、精确 token 记账、对话入库）
- `>= 4MB`：**原始字节直通**，不解析、不重序列化、不入对话库，记账取上游 usage

## 完成情况
### app.py
- [x] 护栏常量：`MAX_REQUEST_MB=64`、`STATELESS_BODY_MB=4`、`LARGE_BODY_CONCURRENCY=4`、`TOKENIZE_MAX_BYTES=65536`、`CONV_CONTENT_MAX_BYTES=262144`、`HEALTH_PORT=6771`
- [x] `app.config["MAX_CONTENT_LENGTH"]` + `@app.errorhandler(413)`
- [x] `count_tokens` 字节上限守卫（超限 `len/4` 估算，绕开 tiktoken O(n²)）
- [x] `_shrink_text` / `_shrink_obj`：入库剔除 data-uri 与 base64 大块
- [x] `_scan_body_meta` / `_upstream_headers_raw` / `_usage_of` / `_proxy_stateless`（信号量在生成器 finally 释放）
- [x] `_dispatch_proxy` 统一分流，`/v1` 与 `/v1/chat/completions` 共用
- [x] `/api/health` + `_start_health_server`（独立线程，不受 GIL 阻塞）
- [x] `_serve()` 启动健康端口

### main.py / nginx.conf / deploy.ps1
- [x] `--worker` 由 `app.app.run()` 改为 `app._serve()`（原来绕过 waitress 与健康端口）
- [x] nginx `client_max_body_size` 2g → 64m
- [x] systemd 注入新环境变量；`MemoryHigh` 2000→1800M、`MemoryMax` 2500→2000M
- [x] 新增 `ai-getway-watchdog.service/.timer`（30s 探活，连续 3 次失败约 90s 才 restart）
- [x] 部署时同步 nginx 配置（备份 → `nginx -t` 通过才 reload，失败回滚）

### 验证
- [x] pytest 63 个通过（新增 15 个：大请求护栏 14 + 健康端点 1）
- [x] 本地 EXE 冒烟：独立健康端口 200，`/api/health` 返回 0.6.30
- [x] 本地 E2E：40MB → 200 / 0.14s / RSS +46MB / 探活 0 失败 / 对话未入库
- [x] 生产冒烟（独立实例 + 临时库，未碰 usage.db）：41MB → 200 / 263ms / RSS 增量 1MB / 200 次探活 0 失败；65MB → 413
- [x] 部署上线：服务 active、内存 40MB、`client_max_body_size 64m` 已生效、watchdog timer enabled、公网 200

## 审查
- 大请求的正确性代价：这条路径拿不到 reasoning_content 回填，token 依赖上游 usage（10-21 全天 3548 请求 fallback 0 次，可靠）
- tiktoken 的上限只解决单请求卡死，不解决内存放大；真正的兜底仍是「大 body 不解析」
- 看门狗分辨不了「确实在跑长任务」与「假死」——健康检查在独立线程，长任务不会误杀
- 遗留：`.bak` 未清理（nginx 配置备份在 reload 成功后已删）；桌面端需手动重启才能用上 `main.py` 修复
