# 工具说明

本文对应版本 **0.3.1 Alpha**。统一入口注册 **44 个 MCP 工具：25 个微信工具、10 个 QQ 工具、9 个统一工具**。清单来自 `wechat_legacy_tools.json`、`qq_mcp_server.TOOLS` 和 `Gateway.tools()`；具体字段以客户端本次连接返回的 `tools/list` 为准。

微信工具保留原名称，QQ 工具添加 `qq_` 前缀，跨平台能力使用 `unified_` 前缀。工具能够注册，不等于对应数据库、媒体、密钥和可选依赖都已就绪。

## 使用顺序

1. 用 `unified_health` 查看进程、队列和模型是否已初始化，它不打开数据库或加载模型；用 `unified_sources` 查看配置状态；需要 QQ 详情时用 `qq_diagnose`。微信侧状态只说明读取器文件与当前进程状态，仍需实际查询验证可用性。
2. 用 `resolve_chat`、`qq_resolve_contact`、`qq_resolve_group` 或 `unified_resolve_chat` 找到候选会话，再使用稳定标识查询。相同昵称不会自动认定为同一人。
3. 日常阅读使用 `chat_timeline`、`qq_chat_timeline` 或 `unified_timeline`；搜索命中后用 `unified_message` 精确定位、`unified_context` 展开前后文，群计数用 `unified_group_stats`。
4. 有后续页时按返回的分页字段继续。检查 `warnings`、来源状态及媒体状态后，再判断覆盖范围。

读取范围限本机已同步且可访问的数据。没有读取到某条记录或媒体，不能推出它从未存在。

## 微信工具：25 项

以下工具继承外置微信读取器的接口，基于保留的 v1.5.4 工具定义适配。公开网关没有重新实现其全部数据库查询，也不保证任意新版读取器或微信版本兼容。统一层会为部分消息和媒体结果追加本地媒体验证、OCR、语音转写等处理。

| 工具 | 用途 | 常用参数与边界 |
| --- | --- | --- |
| `sessions` | 列出会话及最近消息摘要 | `keyword`、`type_filter`、`limit`；排序时间可能受置顶影响，实际消息时间另有字段 |
| `resolve_chat` | 将名称、备注、群名等解析为候选会话 | `query` / `chat` / `keyword`、`type_filter`；确认候选后使用 `username` / `talker` |
| `contacts` | 查询联系人和群 | `keyword`、`friends_only`、`groups_only`、`limit` |
| `messages` | 查询指定会话的消息 | `chat` / `talker`、时间、关键词、发送者、类型、分页；`view=agent` 返回带分页信息的消息对象 |
| `chat_timeline` | 返回适合阅读的会话时间线 | `chat` / `talker`、时间、`limit`、`offset`、`order`、`display_order`；默认取最近窗口，窗口内按时间展示 |
| `media_resources` | 按消息或时间定位媒体资源 | 会话、`local_id` / `server_id_str`、时间、类型；找到资源记录不等于本地文件可读 |
| `group_members` | 查询群成员 | `chat` / `chatroom_id`、`limit`、`offset`；`stats=true` 会额外扫描消息统计 |
| `sns` | 查询本地朋友圈时间线 | `user`、`keyword`、时间、`limit`、`offset`；数据取决于本地缓存 |
| `sns_feed` | 朋友圈时间线的语义别名 | 参数与 `sns` 一致 |
| `sns_search` | 搜索本地朋友圈内容 | 必填 `keyword`，可加用户、时间和分页条件 |
| `sns_notifications` | 查询朋友圈互动通知 | 时间、`limit`；`include_read=true` 包含已读通知 |
| `search` | 搜索微信消息，可限定会话 | 必填 `keyword`；可加会话、时间、发送者、消息类型、`search_mode` |
| `sql` | 对外置读取器允许的数据库执行只读 SQL | 必填 `query`，可指定 `subdir`、`file`、`limit`；只读约束由外置读取器执行 |
| `transfers` | 读取本地转账记录和关联消息信息 | 时间、`limit`；不执行转账操作 |
| `red_packets` | 读取本地红包消息记录 | 会话、时间、发送者、`limit`；不是领取接口，不保证具有红包金额 |
| `favorites` | 读取本地收藏记录 | 时间、`limit`；部分内容为结构化字段或原始 XML |
| `chatroom_announcements` | 查询群公告 | `chatroom_id`、时间、`limit` |
| `forward_history` | 查看最近转发目标会话 | 时间、`limit`；不是所有已转发消息的内容历史 |
| `schema` | 查看数据库和表结构 | 可选 `subdir`、`file`；未指定时列出可用结构 |
| `cache_status` | 查看微信读取器的元数据缓存状态 | 不表示所有正文和媒体都已缓存或已读取 |
| `cache_refresh` | 刷新微信元数据缓存及索引 | `background`、`force`；会写读取器缓存，后台提交成功不等于刷新已结束 |
| `cache_rebuild` | 删除并重建微信读取器缓存 | 会改变读取器缓存目录；不是修改或删除原聊天数据库 |
| `unread` | 查询未读会话 | `type_filter` / `filter`、`limit`；依据读取器元数据缓存 |
| `stats` | 查询微信元数据缓存统计 | 主要反映联系人和会话，不是完整消息总量统计 |
| `export_messages` | 将指定会话导出到本地文件 | 必填 `path`，同时选择会话；支持 `jsonl`、`markdown`、`html` 及筛选条件；会写输出文件 |

`messages`、`chat_timeline`、`media_resources` 增加 `include_image_text`，默认开启本地 OCR。`include_media_paths=false`，以及相应工具的 `include_images=false` / `include_local_paths=false`，可跳过该次结果的媒体补充处理。

`fields=full`、`include_debug=true` 等选项可能返回原始 XML、资源线索或调试字段；它们属于底层兼容接口，不适合作为普通阅读的默认输出。原始字段和路径是否应进入模型上下文，由使用方决定。

## QQ 工具：10 项

QQ 读取通过本地 Python 适配层完成，必须先明确配置目标账号的数据库目录。未配置时 QQ 工具会提示配置，微信仍可独立使用。下列会话工具支持私聊、群聊和讨论组，具体使用 `contact` / `group` 与 `chat_type`；歧义候选需要先解析，不会任选一个联系人。

| 工具 | 用途 | 常用参数与边界 |
| --- | --- | --- |
| `qq_diagnose` | 检查 QQ 路径、SQLCipher、扩展及密钥是否可用 | 不返回密钥正文；可能返回本地路径，已配置时会尝试打开资料库 |
| `qq_resolve_contact` | 按备注、昵称、QQ 标识或 UID 查找私聊候选 | 必填 `query`，可选 `limit` |
| `qq_resolve_group` | 按群名或群标识查找群候选 | 必填 `query`，可选 `limit` |
| `qq_messages` | 读取一个会话的消息页 | 会话、时间、`keyword`、`sender`、`kind_name`、`limit`、`cursor` / 兼容 `offset`、排序；`include_media` 控制媒体信息 |
| `qq_chat_timeline` | 返回适合阅读的 QQ 时间线 | 会话、时间、关键词、分页和排序；默认取最近窗口，再按聊天顺序展示 |
| `qq_search` | 在一个 QQ 会话内搜索 | 必填 `keyword`，同时指定会话；不是跨全部 QQ 会话搜索 |
| `qq_cache_recent` | 将当前可读消息和可用媒体副本保存到本地缓存 | 会话、时间、关键词、`limit`、`order`、`include_media`；写缓存，不会自动持续监听 |
| `qq_recall_events` | 查看撤回提示、附近上下文和缓存候选 | 会话、时间、`context_window`、`cache_window_seconds`；候选不应直接当作已确认的撤回原文 |
| `qq_stats` | 统计一个会话的本地消息 | 可按时间、关键词筛选；按发送者、月份和类型汇总 |
| `qq_export_messages` | 导出一个会话的消息 | 必填 `path`，同时指定会话；支持 `jsonl` / `markdown`；默认拒绝已有文件；显式 `overwrite=true` 才允许覆盖，成功后原子发布 |

网关给 `qq_messages`、`qq_chat_timeline` 增加 `include_image_text`。QQ 图片定位使用消息线索与本地缓存，并检验图片能否完整解码；路径无法确认或存在冲突时返回相应状态。QQ 语音、视频、文件和复杂消息目前不具备与微信一致的完整媒体处理能力，媒体线索也不代表已取得文件。

撤回恢复只能利用原文仍在本地数据库中的情况，或先前 `qq_cache_recent` 已保存的内容；没有原文和历史缓存时，只能提供撤回提示及仍可读取的上下文。

## 统一工具：9 项

| 工具 | 用途 | 参数与返回重点 |
| --- | --- | --- |
| `unified_sources` | 查看两个来源的配置及当前运行状态 | 无参数；微信侧是读取器文件、是否活跃、会话内启动状态，QQ 侧执行诊断；不证明所有媒体或查询均正常 |
| `unified_resolve_chat` | 同时查找微信和 QQ 候选 | 必填 `query`；`qq_chat_type=group` 查 QQ 群，否则查私聊联系人；不会按同名自动合并身份 |
| `unified_timeline` | 按时间合并一个或两个平台的会话 | 至少提供 `wechat_chat` 或 `qq_chat`；支持 `date` 或起止时间、关键词、`sender`、`kind_name`、排序、`cursor`、媒体及 OCR 开关 |
| `unified_search` | 在指定的一个或两个平台会话中搜索并合并 | 时间线参数加非空 `keyword`；不是全账号跨平台搜索 |
| `unified_read_image` | 读取一个本地图片或表情的 OCR 与图片预览 | 必填 `path`；`include_image=false` 不附图片块；OCR 不可用时可解码图片仍可预览，动画预览只取首帧 |
| `unified_message` | 精确读取某条文字、语音、图片或表情消息 | 必填 `source`、`chat_id`，`record_id` / `message_id` 二选一；原生 ID 碰撞返回 `ambiguous`；可选日期、群聊类型、媒体开关及 `max_scan` |
| `unified_context` | 展开某条记录前后文 | 精确定位参数，加 `before_count`、`after_count`，默认各 10 条、最多各 50 条；同秒消息仍按来源顺序保留 |
| `unified_group_stats` | 群聊的事实计数 | 必填 `source`、稳定群 `chat_id`；支持日期或范围、`chat_type`、`max_messages`，按发送者 ID、类型和日期计数；不自动推断关系或情绪 |
| `unified_health` | 无数据读取的快速健康检查 | 无参数；返回版本、PID、微信进程、QQ 队列和识别服务状态，不证明数据库、媒体或模型能成功读取 |

`date=YYYY-MM-DD` 使用 +08:00 自然日，不能与 `after` / `before` 同传。精确定位与上下文默认 `max_scan=20000`，群统计默认 `max_messages=20000`，最多可显式设为 1000000；扫描触顶返回 `partial`，不能当作不存在或全量群统计。

统一时间线每页 `limit` 为 1～500，默认 100。统一结果保留 `source`、`chat_id`、`message_id`、`record_id`、发送者、方向、时间、类型和 `original`。`record_id` 用于区分本地记录，不应仅按 `message_id` 删除看似重复的消息。导出碰撞记录带 `:sha256:` 消歧后缀，精确读取和上下文支持该后缀及旧版无标签摘要。

单平台可传 `sender`；同时读取两平台时，分别用 `wechat_sender` 与 `qq_sender`，不会假设两个平台同名或同一字符串代表同一个人。QQ 支持 `uid:`、`uin:`、`name:`、`direction:` 选择器；数字默认为号码，数字昵称需 `name:` 前缀。QQ 号码与 UID 经精确别名验证后返回规范 UID，继续分页时不允许该映射改变。

## 分页、失败与媒体状态

- 微信原生时间线查看 `query.has_more` 和 `query.next_offset`；QQ 优先使用 `query.next_cursor`（不能同时传非零 offset），统一时间线使用 `has_more` 与 `next_cursor`。不能因为某一页为空或少于预期，就自行认定已经读完。
- 统一游标绑定会话、时间、关键词、顺序与媒体选项。改变这些条件后应从头查询，不能复用原游标。
- `snapshot_before` 固定的是首轮读取的时间上界，不是数据库事务快照。历史同步、迁移、删除或撤回可能改变旧记录及偏移，发生这些变化后应重新分页。
- 任一请求的来源读取失败，统一时间线返回 `status=partial`、来源错误和已经通过身份/范围校验的 `available_source_pages`，主 `messages` 为空、游标不推进。被判定为其他会话或无效页的正文不会留在诊断输出。先修复失败来源，或明确只查询可用来源；不能把它解释为“没有消息”。
- QQ 分批查询不先加载整个范围，尚未证实终点时 `query.total=null`、`total_exact=false`。兼容的深层 offset 仍需从头跳过；完整统计调用 `qq_stats`，实现范围详见 [QQ_DATA.md](QQ_DATA.md)。
- QQ 主消息库不可读会报错；QQ 全文检索辅助库不可读时可退回主库内容，并返回警告。这时文本覆盖可能降低。
- 精确消息和群统计的 `coverage.pagination_complete` 表示已到分页终点，`source_complete` 表示本次依赖的数据来源是否完整可读，两者不同。来源降级时保留警告并返回 `partial`，不会证明某消息不存在。`identity_complete` 单独标记已读记录的身份冲突；未知身份不强行归给某人。
- `media_enrichment_failed` 等警告表示媒体补充处理失败，已取得的消息仍被保留。查看具体媒体状态，不要把查询成功等同于媒体完整。
- OCR 的 `ok` / `no_text` 和语音的 `ok` / `no_speech` 都是自动结果。`no_text` 不证明图片没有文字，`no_speech` 不证明录音没有有效内容。`partial`、`unavailable`、`audio_missing`、`not_transcribed` 等状态应原样保留。

工具会读取原聊天数据，也可能写入派生缓存、图片修复副本、转写结果和显式指定的导出文件。原库只读与整个程序不写文件不是同一回事。处理位置、客户端数据边界及缓存布局见 [架构说明](ARCHITECTURE.md)。
