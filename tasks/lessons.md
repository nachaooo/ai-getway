# 经验教训

## conversations.db 迁移（2026-08-13）
- **运行中 sqlite 库不能文件级复制**（`shutil.copy2` 会得到损坏副本）：必须用 `sqlite3 backup()` API（自动合并 WAL）。迁移脚本备份正是用它。
- **VACUUM 需独占锁，服务运行中必然失败**：改为失败时写 `<db>.needs_vacuum` 标记，app 启动时独占连接压缩并删除标记。本次正式迁移因切表后短暂无并发写入，VACUUM 直接成功。
- **历史数据可能含无效 UTF-8 字节**（`Could not decode to UTF-8 column 'messages'`）：`get_conv_db` 与迁移脚本均设 `conn.text_factory = lambda b: b.decode("utf-8", errors="replace")`，避免单条坏记录中断整次迁移/读取。
- **服务运行中迁移**：批量提交（500 条）释放写锁 + 循环增量追平 + `BEGIN IMMEDIATE` 排他锁内最后一次追平再原子切表，杜绝"追平后又有新写入"的竞态窗口。切表后旧进程写对话失败被 try/except 吞掉（AI 转发不受影响），需重启进程加载新代码。
- **测试假象警示**：用"副本 + 并发 writer 模拟"验证增量追平时，writer 写入的测试行会占用真实源库的 id 区间；对比时若拿在线源库（已增长）对副本，会误报"不一致"。应只对比快照范围内 id，或对比 writer 写入的行本身。
- **`?by=` 代理参数**：app.py 会把 `?by=<base>` 拼上 `/chat/completions` 转发，mock 上游应监听 `/chat/completions`（不是 `/v1/chat/completions`），否则 404。
- **托盘不监控 worker 崩溃**：`scripts/tray.py` 只在启动时 `start_gateway()` 拉起 worker，worker 被杀后托盘不会自动重启它；手动重启需照原命令行 `pythonw.exe C:\...\app.py`。

## 全站 504 事故（2026-10-22，v0.6.30）
- **"进程活着"不等于"服务可用"**：单个大 body 触发 `json.dumps` 持 GIL，32 个线程全卡死，systemd 仍显示 active，`Restart=on-failure` 永不触发。必须有独立线程的健康端口 + 主动看门狗。
- **判断假死的证据链**（比 `ps`/`systemctl` 可靠得多）：
  - `py-spy dump --pid <pid>` → 看是全员 `futex_wait_queue`（GIL 争用）还是真在算
  - `/proc/PID/stat` 的 utime/stime 两次采样 → 本例 stime 猛涨、utime 不动 = 全在内核态自旋
  - `/proc/PID/stat` 的 minflt 冻结 → 分配器卡在同一个块上反复失败，没有新进展
  - `cgroup memory.events` 的 `high` 持续增长 → 命中 `MemoryHigh` 被限流
- **`MemoryHigh` 限流的失败形态是"零进展自旋"**，不是 OOM kill，也不抛异常；程序看起来在跑，实际 10 分钟做不完 1 秒的活。
- **`tiktoken.encode()` 对超长连续同字符是 O(n²)**：16KB→0.09s、64KB→1.5s、256KB→45.7s、1MB≈11min；正常有词边界的文本是线性（4MB 仅 0.27s）。凡是对用户可控文本调 tiktoken，必须先设字节上限。tiktoken 会释放 GIL，所以它只拖慢单请求，不会冻结整个进程。
- **大 body 的优雅解是"不解析"**：按大小分层，超阈值走原始字节直通（同时省掉 4 份副本），记账交给上游 usage。前提是先实证上游确实稳定返回 usage。
- **PowerShell 双引号 here-string 会静默清空未定义的 `$var`**：`deploy.ps1` 里 `$remoteScript = @"..."@` 中写 bash 局部变量（如 `$NGX`），命令会变成 `cp /etc/nginx/conf.d/.conf` 且不报错、还打印成功。改完必须做一次展开检查，并对 `cp` 之类做返回值判断。
- **`Set-Content -Encoding UTF8`（PS 5.1）写 BOM**，经 ssh 传给 `bash -s` 会让首行变 `﻿#!/bin/bash` 直接报错。远程脚本用 write 工具生成，或直接 here-string 管道。
- **验证要在生产形态下做，且不污染数据**：用独立端口 + `PORT/HEALTH_PORT/DB_PATH` 环境变量起第二个实例跑冒烟，既验证真实 waitress/线程模型，又不碰生产 `usage.db`。
