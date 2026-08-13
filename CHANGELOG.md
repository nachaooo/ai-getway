# Changelog

## [Unreleased]

## [0.6.15] - 2026-08-13

### Changed
- **对话存储重构（拆表 + 内容哈希去重）**：旧结构 `conversations` 单表把整段消息 JSON 存在 `messages` 大字段里，重复内容（system prompt、tool 输出、缓存助手回复）每条会话存一份，导致库膨胀到 **7.5GB**。现拆为 4 张表，重复内容只存一份，正式迁移后压缩至 **约 450MB（-94%）**。
- 升级后首次启动自动迁移旧库（无需手动操作），迁移前自动备份为 `conversations.db.bak`。
- 现有 API 返回结构与旧版完全一致，外部调用方无感知。

### 新数据库结构（`%APPDATA%/AI Gateway/conversations.db`）

#### 1. `conversations` — 会话元数据（最外层表）
一次 AI 对话一行，存与内容无关的统计和摘要字段：

| 字段 | 含义 |
|---|---|
| `id` | 会话 ID（**保留旧值**，与 `usage_logs.conversation_id` 关联） |
| `model` / `provider` / `endpoint` | 请求的模型 / 平台 / 端点 |
| `request_time` | 请求时间 |
| `system_prompt_id` | 外键 → `system_prompts.id`（该会话用的系统提示词） |
| `last_user_message` | 最后一条用户消息（摘要） |
| `response` | 最终回复文本（摘要） |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | Token 统计 |
| `duration_ms` | 耗时（毫秒） |

> **做用量/性能分析用这张表**：`SELECT model, SUM(total_tokens), AVG(duration_ms) ... GROUP BY model`。

#### 2. `messages` — 消息序列（会话 → 内容 的桥）
记录每条消息属于哪个会话、第几条、什么角色、内容存哪：

| 字段 | 含义 |
|---|---|
| `id` | 消息 ID |
| `conversation_id` | 外键 → `conversations.id` |
| `seq` | 消息序号（0,1,2…），按此还原消息顺序 |
| `role` | 消息角色：`system` / `user` / `assistant` / `tool` |
| `content_id` | 外键 → `message_contents.id` |

> 这张表本身**不存内容**，只存指向内容的指针。同一条去重内容可能被多张表的 `content_id` 引用。

#### 3. `message_contents` — 去重消息内容（核心去重表）
按 `(content_hash, extra_hash)` 唯一约束，相同内容只存一份：

| 字段 | 含义 |
|---|---|
| `content_hash` | SHA-256 哈希（`s:` 开头=字符串，`l:` 开头=数组） |
| `extra_hash` | 附加字段哈希（`tool_call_id`、`tool_calls`、`name` 等），无附加则为空串 |
| `content` | 消息正文（字符串或 JSON 数组） |
| `extra` | 附加字段 JSON（如工具调用信息） |
| `is_list` | 1=content 是数组（OpenAI 多段 content），0=是字符串 |

> **这张表是空间收益来源**：242 万条消息 → 仅 7.3 万份唯一内容（去重率 97%）。**不要直接对它做业务查询**（没有会话/时间维度），它只负责"存"。

#### 4. `system_prompts` — 去重系统提示词
| 字段 | 含义 |
|---|---|
| `id` | 主键 |
| `content_hash` | SHA-256（唯一约束） |
| `content` | 提示词全文 |

> 同样的 system prompt 全局只存一份（本次 2.4 万会话 → 仅 234 份）。

### 表关系
```
conversations (1) ──┬──< (N) messages ──> (1) message_contents
                    └────> (1) system_prompts
```
- 1 个会话含 N 条消息（`conversations.id` = `messages.conversation_id`）
- N 条消息可指向同一份去重内容（`messages.content_id` = `message_contents.id`）
- 1 个会话引用 1 份系统提示词（`conversations.system_prompt_id` = `system_prompts.id`）

### 如何查数据（SQL 示例）
```sql
-- 1. 按会话取完整消息（还原旧版 messages JSON 数组）：
SELECT m.seq, m.role, mc.content, mc.extra, mc.is_list
FROM messages m
JOIN message_contents mc ON m.content_id = mc.id
WHERE m.conversation_id = 12345
ORDER BY m.seq;

-- 2. 取某会话的系统提示词：
SELECT sp.content
FROM conversations c
JOIN system_prompts sp ON c.system_prompt_id = sp.id
WHERE c.id = 12345;

-- 3. 按内容反查（哪些会话用过同一条 system prompt）：
SELECT c.id, c.request_time
FROM conversations c
WHERE c.system_prompt_id = (SELECT id FROM system_prompts WHERE content_hash = 's:xxxx');

-- 4. 删除某会话时清理不再被引用的去重内容：
DELETE FROM messages WHERE conversation_id = 12345;
DELETE FROM conversations WHERE id = 12345;
-- 若某 content_id 无任何 messages 引用，再删 message_contents 对应行
```

### 分析该用哪些数据
| 分析目标 | 用哪张表 | 说明 |
|---|---|---|
| 请求数 / Token / 耗时 / 模型 / 平台统计 | `usage_logs`（usage.db） | 按 `request_time` 聚合（`/api/usage` 用的就是它） |
| 会话级 Token / 耗时 / 模型 | `conversations` | 一条会话一行，直接 SUM/AVG/GROUP BY |
| 对话内容 / 主题分类 / 语义分析 | `conversations` + `messages` + `message_contents` | 先查会话元数据，再 JOIN 重建消息数组 |
| 某模型/某平台用了哪些 system prompt | `conversations` + `system_prompts` | 按 `system_prompt_id` 关联 |
| 内容去重收益 / 空间占用审计 | `message_contents` | 只看唯一内容有多少份 |

## [0.6.0] - 2026-05-24

### Added
- 各模型使用量统计：与各平台使用量同列（模型、请求数、输入、输出、Token/s），置于各平台使用量下方。
- 托盘图标左键单击直接打开看板（右键菜单保持不变）。

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
