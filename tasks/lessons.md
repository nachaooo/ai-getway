# 经验教训

## conversations.db 迁移（2026-08-13）
- **运行中 sqlite 库不能文件级复制**（`shutil.copy2` 会得到损坏副本）：必须用 `sqlite3 backup()` API（自动合并 WAL）。迁移脚本备份正是用它。
- **VACUUM 需独占锁，服务运行中必然失败**：改为失败时写 `<db>.needs_vacuum` 标记，app 启动时独占连接压缩并删除标记。本次正式迁移因切表后短暂无并发写入，VACUUM 直接成功。
- **历史数据可能含无效 UTF-8 字节**（`Could not decode to UTF-8 column 'messages'`）：`get_conv_db` 与迁移脚本均设 `conn.text_factory = lambda b: b.decode("utf-8", errors="replace")`，避免单条坏记录中断整次迁移/读取。
- **服务运行中迁移**：批量提交（500 条）释放写锁 + 循环增量追平 + `BEGIN IMMEDIATE` 排他锁内最后一次追平再原子切表，杜绝"追平后又有新写入"的竞态窗口。切表后旧进程写对话失败被 try/except 吞掉（AI 转发不受影响），需重启进程加载新代码。
- **测试假象警示**：用"副本 + 并发 writer 模拟"验证增量追平时，writer 写入的测试行会占用真实源库的 id 区间；对比时若拿在线源库（已增长）对副本，会误报"不一致"。应只对比快照范围内 id，或对比 writer 写入的行本身。
- **`?by=` 代理参数**：app.py 会把 `?by=<base>` 拼上 `/chat/completions` 转发，mock 上游应监听 `/chat/completions`（不是 `/v1/chat/completions`），否则 404。
- **托盘不监控 worker 崩溃**：`scripts/tray.py` 只在启动时 `start_gateway()` 拉起 worker，worker 被杀后托盘不会自动重启它；手动重启需照原命令行 `pythonw.exe C:\...\app.py`。
