# 迁移计划：conversations.db 拆表 + 内容哈希去重

## 背景
- conversations.db 达 7.3GB，O(N²) 重复存储：system_prompt、tool 输出等重复内容每条对话存一份
- 目标：保留原 id（usage_logs.conversation_id 关联）、API 返回结构不变，迁移全部历史数据

## 最终表结构

```
system_prompts   (id, content_hash UNIQUE, content)
message_contents (id, content_hash, extra_hash, content, extra, is_list, UNIQUE(content_hash, extra_hash))
messages         (id, conversation_id, seq, role, content_id)
conversations    (id 保留原值, model, provider, endpoint, request_time,
                  system_prompt_id, last_user_message, response, 各 token 列, duration_ms)
```

### 关键设计决策
1. **组合去重**：`message_contents` 同时存 content 和 extra（tool_call_id、tool_calls、name 等附加字段），以 (content_hash, extra_hash) 组合唯一。避免丢失附加字段（早期方案丢失了）。
2. **哈希规则**：str 前缀 `s:`，list 前缀 `l:` + `json.dumps(sort_keys=True, separators=(",", ":"))`；app.py 与迁移脚本保持一致。
3. **消息重建**：role 存 messages 表，content/extra 从 message_contents JOIN 恢复；批量重建避免 N+1。
4. **兼容检测**：`ensure_conv_schema` 发现旧结构（conversations 含 messages 列）即 raise，强制先迁移。

## 验证结果（副本 conv_test_copy4.db，原 7.5GB）
- 迁移后 **436 MB**（压缩 94%）：conversations=27410, messages=2428644, system_prompts=233, message_contents=72113
- 全量抽样（id%7==0，3915 条）messages/system_prompt/元数据 **全部一致**
- app.py 读写冒烟测试通过：列表/详情/export/archive + 写入重建一致（含 extra）+ 去重验证
- 现有 pytest 28 个全部通过（测试已隔离 CONV_DB_PATH）

## 执行步骤
- [x] 编写迁移脚本 `scripts/migrate_conv_db.py`（备份/事务/校验/表切换/VACUUM）
- [x] app.py 改造：schema、哈希去重读写、批量重建
- [x] 副本试跑 + 数据一致性验证
- [x] app.py 对新库结构读写冒烟测试
- [x] tests/test_app.py 隔离 CONV_DB_PATH，28 测试通过
- [x] **正式迁移**（2026-08-13 完成）：`python -X utf8 scripts/migrate_conv_db.py` 运行中迁移 11 分钟，27903→27918 条（追平 15 条），新库 446MB（压缩 94%），VACUUM 直接成功，`.bak` 备份保留
- [x] 重启 worker（pythonw app.py，新 PID 17772）+ 验证 HTTP 200 + 列表/详情/archive API
- [x] 端到端写入验证：mock 上游走真实代理链路写入 id=27919，列表正确读取，测试数据已清理
- [x] 记录结果到 tasks/todo.md、经验到 tasks/lessons.md

## 风险与注意
- AGENTS.md 绝对规则：不可触碰 usage.db；conversations.db 正式迁移前会自动备份为 `.bak`
- 服务运行中迁移：备份用 sqlite backup API（WAL 合并），文件级 copy2 会得到损坏副本（已验证）
- 正式迁移前建议停服务避免写入并发；若不停，新写入走新 app.py 逻辑，表切换事务可容忍少量并发
