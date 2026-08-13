# AI Gateway - 会话记忆

更新时间：2026-08-13

## 项目约定
- 版本号只在 `app.py` 的 `VERSION = "x.y.z"` 维护，构建时 `scripts/bump_version.py` 自动升 patch
- 打包入口是 `main.py`（托盘模式），spec 中 `console=False`
- 路径双目录约定：`_EXE_DIR`（exe 目录，可写，放 config.json/logs）、`_RES_DIR`（frozen 时为 `sys._MEIPASS`，放打包资源 static/templates/tokenizers），`app.py` 中对应 `EXE_DIR`/`RESOURCE_DIR`

## 踩过的坑
- ~~修复托盘启动 NameError~~（v0.6.12 已解决）：提交 880977d 移除 `BASE` 定义但残留 7 处引用，导致 frozen 托盘模式模块级代码 `ICON_PNG = os.path.join(BASE,...)` 立即 `NameError` 崩溃，`console=False` 下表现为"双击无反应"。教训：删全局变量时要全局搜索引用。
- 构建输出 exe 的 LastWriteTime 早于 git commit 时间属正常（先改代码→构建→再提交）
- PowerShell 中内联 python 的引号容易转义错乱，改用 grep/read 查版本
- ~~运行中 sqlite 库不能 `shutil.copy2` 复制~~（会得到损坏副本）：必须用 `sqlite3 backup()` API（WAL 合并）
- ~~VACUUM 运行中执行必然失败~~（database is locked）：改为失败时写 `<db>.needs_vacuum` 标记，app 启动时独占压缩并删标记
- ~~历史记录可能含无效 UTF-8 字节~~（`Could not decode to UTF-8`）：`get_conv_db` 与迁移脚本均设 `conn.text_factory = lambda b: b.decode("utf-8", errors="replace")`

## 经验
- 验证打包产物是否可启动：Start-Process exe → 检查进程存活 + `Get-NetTCPConnection -LocalPort 6770` + `Invoke-WebRequest http://localhost:6770` HTTP 200
- 单文件 PyInstaller 进程树：托盘(PID A) → bootloader 子进程(PID B) → worker(PID C, --worker 参数)

## 当前任务：conversations.db 重构迁移（2026-08-13 已完成）
- ~~目标：7.5GB 库拆表 + 内容哈希去重~~ 已完成：`scripts/migrate_conv_db.py` 运行中迁移 11 分钟，27903→27918 条，新库 446MB（压缩 94%），VACUUM 直接成功，`.bak` 备份保留在库旁
- ~~服务源码直跑（pythonw app.py，PID 7880）~~ 已重启为新 worker PID 17772 加载新代码
- 验证全绿：列表/详情/archive API HTTP 200；端到端写入（mock 上游 → id=27919）成功，测试数据已清理
- 遗留注意：托盘进程（PID 5688）本次已消失未重启，仅 worker 直跑；`%APPDATA%/AI Gateway/conversations.db.bak`（7.6GB）可确认无误后删除回收空间
