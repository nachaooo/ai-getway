# Changelog

## [Unreleased]

### Added
- 用量面板「各平台本月用量」新增展示：调用次数、输入 Token 合计、输出 Token 合计。
- 各平台本月用量按调用次数降序排列。

### Changed
- 后端 `/api/usage` 接口的月度统计查询中增加 `SUM(prompt_tokens)` 与 `SUM(completion_tokens)` 聚合字段。
- 前端「各平台本月用量」由列表改为表格展示，字段更清晰。
