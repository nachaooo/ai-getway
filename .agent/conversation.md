# AI Gateway - 会话记忆

更新时间：2026-10-26

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
- **大 body 会冻结整个 waitress 进程**（v0.6.30 已解决）：Flask `request.get_json()` 解析 + `json.dumps` 重序列化，json 的 C 编码器全程持 GIL，其它 31 个线程全卡在 GIL 上；进程撞 cgroup `MemoryHigh` 后分配进入「回收失败→重试」循环，`utime` 不涨、`stime` 猛涨、RSS 不变。`Restart=on-failure` 对「进程活着但假死」无效 → 必须独立线程健康端口 + systemd 看门狗。
- **`tiktoken.encode()` 对超长连续同字符是 O(n²)**：16KB→0.09s、64KB→1.5s、256KB→45.7s、1MB≈11min（有词边界的正常文本是线性，4MB 仅 0.27s）。tiktoken 会释放 GIL（不冻结进程），但单请求会挂几分钟。已加 `TOKENIZE_MAX_BYTES=65536` 字节上限，超限用 `len/4` 估算。
- **PowerShell 双引号 here-string 会静默清空未定义的 `$var`**：`deploy.ps1` 里 `$remoteScript = @"..."@` 中的 bash 局部变量（如 `$NGX`）会被 PowerShell 当成空串替换，命令变成 `cp /etc/nginx/conf.d/.conf`，且不报错。只能写 PowerShell 里已定义的 `${APP_NAME}` 等形式；改完必须做一次展开检查。
- `Set-Content -Encoding UTF8`（PS 5.1）会写 BOM，经 ssh 传到 Linux 执行 `bash -s` 时首行变 `﻿#!/bin/bash` → 报错。远程脚本用 write 工具生成或直接 here-string 管道。
- 远程诊断常用组合：`py-spy dump --pid`、`/proc/PID/task/*/schedstat`、`/proc/PID/stat` 的 utime/stime、`/proc/PID/stat` 的 minflt、`cgroup memory.events`（看 `high`/`max` 是否在涨）
- **大 body SSE 中文乱码 + `Unterminated string`**（v0.6.31 已解决）：`_proxy_stateless` 用 `requests.iter_lines(decode_unicode=True)` 但没设 `resp.encoding`；无 charset 的 `text/event-stream` 被 requests 按 ISO-8859-1 解码，中文变 mojibake，汉字 UTF-8 中间字节 `0x85`（先/全）变 `U+0085` 后被 `str.splitlines()` 当换行，把 JSON 劈成半截。教训：**SSE 框架只能按 `b"\n"` 字节分行并原样透传，不能依赖 `iter_lines()` + `splitlines()`**。已部署 0.6.31（`/api/health` 验证）。

## 经验
- 验证打包产物是否可启动：Start-Process exe → 检查进程存活 + `Get-NetTCPConnection -LocalPort 6770` + `Invoke-WebRequest http://localhost:6770` HTTP 200
- 单文件 PyInstaller 进程树：托盘(PID A) → bootloader 子进程(PID B) → worker(PID C, --worker 参数)

## 当前任务：大 body 触发全站 504 的修复（v0.6.30，2026-10-22 已部署）
- 根因：43.9MB 请求体（正常 0.4~0.8MB）→ 解析 + `json.dumps` 持 GIL → 32 线程全卡 → 连 `GET /` 都 504
- 方案：按 body 大小分层。<4MB 走原逻辑（reasoning 回填 + 精确记账 + 对话入库）；≥4MB（`STATELESS_BODY_MB`）走**原始字节直通**，不解析/不重序列化/不入库，记账取上游 usage
- 已实证上游 usage 可靠：10-21 全天 3548 请求全部命中 `upstream usage`，fallback 0 次
- 护栏：`MAX_REQUEST_MB=64` + nginx `client_max_body_size 64m`（原 2g）、大请求并发闸门 4、入库剔图片/base64（`CONV_CONTENT_MAX_BYTES`）
- 可观测：`/api/health` + 独立线程健康端口 6771（不受 GIL 阻塞）+ 30s systemd 看门狗（连续 3 次失败才 restart）
- 生产冒烟（独立实例 + 临时库，未碰 usage.db）：41MB → 200 / 263ms / RSS 增量 1MB / 200 次探活 0 失败；65MB → 413
- 注意：`main.py --worker` 原来直接 `app.app.run()`，绕过了 waitress 和健康端口，已改调 `_serve()`。桌面端重启才生效
- ~~待办：提交 + 部署~~ 已完成（commit b548f1b / 5cb4407，deploy 验证全绿）

## 上一任务：conversations.db 重构迁移（2026-08-13 已完成）
- ~~目标：7.5GB 库拆表 + 内容哈希去重~~ 已完成：`scripts/migrate_conv_db.py` 运行中迁移 11 分钟，27903→27918 条，新库 446MB（压缩 94%），VACUUM 直接成功，`.bak` 备份保留在库旁
- ~~服务源码直跑（pythonw app.py，PID 7880）~~ 已重启为新 worker PID 17772 加载新代码
- 验证全绿：列表/详情/archive API HTTP 200；端到端写入（mock 上游 → id=27919）成功，测试数据已清理
- 遗留注意：托盘进程（PID 5688）本次已消失未重启，仅 worker 直跑；`%APPDATA%/AI Gateway/conversations.db.bak`（7.6GB）可确认无误后删除回收空间
