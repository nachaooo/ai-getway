# AI Gateway - Agent Guide

## 绝对规则

### 🔴 禁止删除数据库文件
- **绝不允许删除、移动、覆盖或修改 `usage.db` 或任何 `*.db` 文件**
- `usage.db` 存储在 `%APPDATA%/AI Gateway/usage.db`，包含用户所有使用记录
- `config.json` 中 `db_path` 已设为 `%APPDATA%` 路径，但任何时候都不应触碰 DB 文件
- 运行 `Remove-Item`、`git checkout` 等命令前，先确认不会影响 DB 文件
- 清理构建产物时只操作 `dist/` 和 `build/` 目录，不要扩大范围

### 🔴 代码/功能变更必须升级版本、构建、提交并部署
每次修改代码或功能升级，**完成后必须自动完成以下五步**（无需用户提醒）：
1. **升级版本**：运行 `build.vbs`（或先手动 `python scripts/bump_version.py`）自动 bump patch 版本号
2. **记录版本说明**：在 `CHANGELOG.md` 顶部 `[Unreleased]` 下新增本次版本的详细条目，包含：
   - 变更分类（Added / Changed / Fixed / Removed）
   - 改了哪些文件、实现了什么功能、为什么改
   - 数据库结构变更时，附表结构说明、表关系、查询方式（参考 0.6.15 的写法）
3. **完成构建**：确保 `dist/AI Gateway vX.Y.Z.exe` 产物已生成
4. **自动提交**：`git add` 本次变更文件后 `git commit`，提交信息按仓库风格写清变更内容。**禁止提交**：`config.json`（含 API 密钥，未被 git 追踪，保持不追踪）、`*.db`/`*.db-wal`/`*.db-shm`、`deploy.ps1.bak` 等临时文件
5. **自动部署**：运行 `deploy.ps1` 同步代码到服务器并重启服务（见下节「部署」），完成后验证服务器 HTTP 200

版本号规则：patch 位自动 +1（如 0.6.14 → 0.6.15），`VERSION` 只在 `app.py` 维护。

## 部署

```powershell
# 一键部署（tar 流式同步代码 + 远端 pip install + systemd 重启）
deploy.ps1
```

- 配置读取 `config.conf`（服务器地址/SSH 密钥/部署路径）
- 依赖：本地 Windows 自带 `ssh` + `tar`（无需 rsync）
- `deploy.ps1` 已排除 `*.db*`，**绝不会覆盖服务器上的 `usage.db` 数据**（位于 `/opt/ai-getway/data/usage.db`，由 systemd `DB_PATH` 指定）
- 服务器 `%APPDATA%` 不展开，数据库路径必须经 `DB_PATH` 环境变量注入，勿依赖 config.json 的 `%APPDATA%` 写法
- 部署后验证：`systemctl status ai-getway` 为 active、`curl -s -o /dev/null -w '%{http_code}' http://localhost:6770/` 返回 200

## 构建

```powershell
# 一键构建（自动升级 patch 版本）
build.vbs

# 或手动
pyinstaller "AI Gateway.spec"
```

构建后必须验证：
- `dist/AI Gateway vX.Y.Z.exe` 存在且时间戳为新
- `app.py` 中 `VERSION` 已正确 bump
- 如服务在运行，确认新进程可启动、HTTP 200

## 项目结构

```
ai-gateway/
├── main.py               # 托盘入口（tray + worker 双模式）
├── app.py                # 核心：代理、DB、API
├── build.vbs             # 构建脚本
├── scripts/
│   ├── bump_version.py   # 版本号升级（UTF-8 安全）
│   ├── tray.py           # 系统托盘启动器
│   └── start.vbs         # VBS 启动器
├── config.json           # 配置（db_path 指向 %APPDATA%）
├── AI Gateway.spec       # PyInstaller 构建配置
└── .gitignore            # usage.db 已忽略追踪
```

## 关键说明

- 数据库实际路径：`%APPDATA%\AI Gateway\usage.db`
- 构建输出：`dist/AI Gateway vX.Y.Z.exe`（带版本号）
- 开机自启动通过 `main.py` 托盘菜单设置，不带 `--worker` 参数
