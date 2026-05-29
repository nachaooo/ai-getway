# AI Gateway - Agent Guide

## 绝对规则

### 🔴 禁止删除数据库文件
- **绝不允许删除、移动、覆盖或修改 `usage.db` 或任何 `*.db` 文件**
- `usage.db` 存储在 `%APPDATA%/AI Gateway/usage.db`，包含用户所有使用记录
- `config.json` 中 `db_path` 已设为 `%APPDATA%` 路径，但任何时候都不应触碰 DB 文件
- 运行 `Remove-Item`、`git checkout` 等命令前，先确认不会影响 DB 文件
- 清理构建产物时只操作 `dist/` 和 `build/` 目录，不要扩大范围

## 构建

```powershell
# 一键构建（自动升级 patch 版本）
build.vbs

# 或手动
pyinstaller "AI Gateway.spec"
```

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
