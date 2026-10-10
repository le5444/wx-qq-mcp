# QQ 读取、分页和缓存的实现边界

本页描述优化后的 QQ 数据层。测试全部采用合成 SQLite 数据，不使用真实私聊。

## 身份与发送者

实际读取只接受精确且唯一的聊天身份。数字按 QQ 号或群号精确查找；`u_` 开头按 UID 精确查找。资料库中不存在该 ID 时，不会退回“只有一个相似号码/名字”的候选。其他名字可以精确匹配备注或昵称，但唯一性从完整资料库独立检查，不依赖发现工具返回的前几条。

`qq_resolve_contact` / `qq_resolve_group` 仍供模糊发现，返回 `complete`、`truncated`，并明确这些是候选，不是已确认身份。大量匹配时，不能根据截断列表判断唯一；读取工具会重新做精确验证。数字或 `u_` 开头的特殊昵称可通过 `name:` 前缀明确指定名字查询。缺 UID 的联系人、互相矛盾的 UID/UIN 映射会拒绝读取，避免误读或把查错字段导致的空结果当作没有历史。

返回的 `chat` / `contact` 含 `canonical_id`、`aliases` 和 `identity_verified`。私聊 canonical ID 是 UID；别名只包含在 profile 中精确且无冲突验证过的 UID/UIN，昵称不会成为身份别名。群使用精确群 ID，讨论组使用明确输入的数字 ID（这不额外证明本机存在该讨论组历史）。不要同时传不同的 `contact`、`chat`、`group`、`discuss`；混合私聊和群聊目标或相互矛盾的类型会报错。

本人身份只按配置或账号目录得到的 UID/UIN 精确验证。如果本人 profile 缺失或配置冲突，消息方向保留 `unknown`、附上 `self_identity_unresolved`，不把模糊匹配到的朋友叫作“我”。同一消息的发送者 UID/UIN 与已知身份矛盾时，返回 `identity_status=unresolved`、`sender_identity_conflict`，不猜测发送方向。查询本人 UID 只查看以本人为对端的会话，不聚合发给其他人的所有出信。

原生 `qq_stats.by_sender` 改为按 `uid:...` / `uin:...` 计数，并通过 `senders[].display_names` 保留名字；同名成员不会合并。没有稳定发送者 ID、或已经标记身份未解决的消息计入 `unknown_sender_count`；身份冲突及本人身份未确认造成的归属不确定另外计入 `unresolved_sender_count`，不会再用其存在冲突的 UID 排成员名次。这个键格式变化需要下游展示代码读取 `display_names`，不能再把 `by_sender` 的键当昵称。

## 查询与分页

`qq_messages`、`qq_chat_timeline`、`qq_search` 保留原来的 `limit`、`offset`、`order`、`display_order` 参数，同时新增 `cursor`。

第一次查询的 `query.next_cursor` 可原样作为下一次相同查询的 `cursor`。不要同时传非零 `offset`。游标绑定实际聊天 ID、聊天类型、账号数据库目录、时间范围、关键词、发送者、类型和排序方向；换查询应从头开始。页大小可以变化。

```json
{"contact":"resolved-uid", "chat_type":"private", "order":"asc", "limit":100}
```

后续页：

```json
{"contact":"resolved-uid", "chat_type":"private", "order":"asc", "limit":100, "cursor":"previous query.next_cursor"}
```

底层按 `(create_time, seq, msg_id)` 续页。主表和全文索引分别最多读取 256 行，逐步合并。主表优先，全文索引中独有的记录保留 `source=fts_only`。同一秒的不同消息不会因为时间相同而漏掉。主表打不开会报错；全文索引不可用时明确提示只使用主表，若续页过程中可用状态改变则要求重新查询。

关键词需要兼容主表内的二进制文本，因此在每个小批次解码后过滤。中间批次没有命中时仍继续读取；只有找到下一条匹配消息才会宣称 `has_more=true`。这保证结果完整，但稀有关键词搜索仍可能扫描很多记录。

`query.total` 在已经确认范围终点时给出精确数目，配套 `total_exact=true`；尚未读完时为 `null` 和 `false`。它不会为了显示总数而先将全库装入内存。需要总数或分布请调用 `qq_stats`。`next_offset` 继续兼容旧客户端，但深层 offset 要从头流式跳过，连续导出应优先 cursor。

顶层及 `query.coverage` 分开表示 `pagination_complete`（这次可用来源是否读到页尾）、`source_complete`（所需本地来源是否可读）和 `identity_complete`（已解码记录是否存在身份冲突）。例如 FTS 不可用时，即使 `has_more=false`，仍返回 `source_complete=false, reason=fts_unavailable` 并保留 warnings。此时 total 只统计实际读到的来源。`source_complete=true` 也不表示微信/QQ云端所有历史都已同步，只描述这次本地读取。原生导出在来源不完整时返回 `complete=false`，即使没有达到消息上限。

发送者使用一个明确的匹配范围：`sender_uid`、`sender_uin`、`sender_name`、`sender_direction` 四选一；也可使用兼容参数 `sender` 的 `uid:`、`uin:`、`name:`、`direction:` 前缀。无前缀数字只匹配 UIN，`u_` 开头只匹配 UID，`from_me` / `from_contact` / `from_member` / `unknown` 只匹配方向，其余文本只匹配精确展示名。数字昵称应明确传 `sender_name` 或 `sender=name:数字`，不会再与另一个人的 QQ 号混在一起。多个发送者参数同时出现会拒绝，发送者匹配范围也绑定分页游标。

`kind_name` 精确匹配返回的归一化类型，如 `text`、`image`、`mixed/text-image`、`reply/quote`，并不承诺所有 QQ 私有消息格式都已解析。

## 内存与性能

数据层每个数据库连接将 SQLite 页面缓存目标设为约 2 MiB，临时排序使用磁盘文件；这不会修改账号数据库结构或写入索引。Python 只保持少量批次和请求结果。`query.read_metrics` 暴露 SQL 批次数、最大批次行数和解码记录数。

在 120,000 条合成主表记录及对应全文索引上，连续读取两页 25 条的 Python 分配峰值约 1.64 MiB，每个源每页一个 256 行批次；同一测试还检查了第 100,001 条起的游标页。这是合成 SQLite 的 `tracemalloc` 数据，**不包括 SQLite 原生分配、读取器、媒体识别和进程整体内存，也不能当作真实 SQLCipher 性能承诺**。

现有账号数据库的索引形状仍影响查询时间。空值安全的复合排序未必能直接命中现有覆盖索引，低内存不代表每页固定耗时。统计、稀有词筛选、深层 offset 和撤回候选核实均可能读取较多行。

分页针对正在使用的本地库，不是冻结副本。补同步、迁移、历史删除或改写可能影响跨页结果；固定时间范围也不能完全消除这种变化。需要固定交付文件时应使用带核验与恢复记录的快照导出。

## 导出与缓存

`qq_export_messages` 流式输出 JSONL 或 Markdown，支持私聊、群聊、讨论组。默认拒绝已有目标；只有显式布尔值 `overwrite=true` 才允许覆盖。先在同目录写临时文件，成功后原子发布；失败不替换旧结果。导出上限真的截断时返回 `truncated=true` / `complete=false`，不把触顶冒充完成。

`cache_recent` 的读取、合并、写入全过程由 OS 文件锁保护，缓存用同目录临时文件和原子替换提交。不同 Codex / Claude 进程同时更新时不会各自覆盖另一进程的新记录。锁文件会保留，但锁由操作系统管理，进程退出后释放。缓存格式损坏会报错并保留原文件，防止忽略坏行后覆盖掉证据。

缓存仍采用 JSONL，单次合并会在内存里加载该会话已经缓存的条目。查询数据层的分页优化不等于无限增长的缓存也已变成数据库。应控制 `cache_recent` 的使用范围；后续可迁移到事务型存储。

## 结构与原文

新增 `segments` 按 payload 中已知字段的出现顺序保留文本，重复文本和重复图片引用不会在这个结构中去重。已确认的文字字段是 `45101`。文件名和未知字段只作为线索；`media.text_hints` **不再回填为正文**。

`payload_parse` 标注部分解析、未知字段数、畸形/截断状态、原始字节长度和摘要。此解析器没有完整实现 QQ 私有协议；回复、合并转发及其他结构仍需进一步扩展，不能用“看到了文本片段”宣称全部还原。媒体线索也不代表本地文件存在或能够解码。
