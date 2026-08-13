# 任务记录：conversations.db 拆表 + 内容哈希去重迁移

## 目标
将 7.5GB 的 conversations.db 重构为拆表 + 内容哈希去重结构，保留原 id，压缩约 94%，运行中迁移不停服。

## 完成情况

### 迁移脚本 scripts/migrate_conv_db.py
- [x] 备份（sqlite backup API，WAL 合并，自动 .bak）
- [x] 建新表 system_prompts / message_contents / messages / conversations
- [x] 批量事务迁移（每 500 条提交，释放写锁让服务端继续写入）
- [x] 增量追平（迁移期间新增对话，循环补录）
- [x] `BEGIN IMMEDIATE` 排他锁内最后一次追平 + 原子切表（杜绝竞态窗口）
- [x] 行数校验（新表 < 旧表快照则回滚）
- [x] VACUUM 容错：锁占用失败时写 `.needs_vacuum` 标记
- [x] text_factory 宽松解码（历史数据无效 UTF-8 字节不中断迁移）

### app.py 改造
- [x] `ensure_conv_schema`：组合去重 schema + 旧结构检测 raise
- [x] `_content_hash` / `_get_or_insert_system_prompt` / `_get_or_insert_message_content` / `_rebuild_messages` / `_conv_to_dict_batch`
- [x] `_log_conversation` 写 message_contents(含 extra) + messages(无 extra)
- [x] 读路径兼容：列表/详情/export/archive/insights
- [x] 启动自动 VACUUM（检测 `.needs_vacuum` 标记）
- [x] `get_conv_db` text_factory 宽松解码

### 验证
- [x] 副本全量抽样（id%7==0，3915 条）messages/system_prompt/元数据全部一致
- [x] 并发 writer 增量追平：58/58 条 0 缺失
- [x] pytest 28 个通过
- [x] 正式迁移：27903→27918 条，追平 15 条，新库 446MB（压缩 94%）
- [x] 重启 worker 加载新代码，HTTP 200，列表/详情/archive 正常
- [x] 端到端写入验证：mock 上游真实代理链路写入 id=27919 成功，测试数据已清理

## 审查
- 正式迁移后 `.bak` 备份保留在 `%APPDATA%/AI Gateway/conversations.db.bak`（7.6GB）
- 新库 447MB，去重效果：messages 引用 2482751 行 → 唯一内容 73370 行
- 服务正常监听 6770，托盘进程需手动重启（本次托盘 5688 已消失，新 worker 17772 直跑）
