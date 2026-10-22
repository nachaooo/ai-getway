# 任务记录：大请求体触发 GIL 冻结导致全站 504（v0.6.30）

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
