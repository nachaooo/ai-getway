# Changelog

## [Unreleased]

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
