# 可恢复的导出、媒体处理和便携阅读

`python -m unified_mcp.workflow` 将原来的三个独立步骤连成一个本地任务：导出消息、处理媒体、生成阅读页。它使用真实配置的本地读取器；下列 ID、日期和目录均为示例，使用前先通过联系人/群解析工具确认稳定 ID。

```powershell
python -X utf8 -m unified_mcp.workflow --wechat "wxid_example" --qq "example-qq-id" --after "2026-10-01" --before "2026-10-02" --output "D:\ChatExports\day-example" --transcribe --portable
```

- 仅微信：省略 `--qq`。仅 QQ：省略 `--wechat`。
- QQ 群：`--qq "example-group-id" --qq-chat-type group`；讨论组使用 `discuss`。
- 本模块的时间范围使用左闭右开 `[after, before)`；纯日期按 +08:00 的当天 00:00 解释，所以 `--after 2026-10-01 --before 2026-10-02` 正好一天。实际发给两个读取器的是 Unix 秒，避免底层对纯日期截止边界理解不一致。
- 全部本地历史：省略 `--after`。省略 `--before` 时，首次运行会固定当前时刻为查询上界，恢复时继续使用该上界。
- `--transcribe` 明确启用本地语音识别。没有此选项时只读取已有成功缓存；未识别记录会明确显示在结果状态中。本地模型和识别依赖仍需安装。
- `--portable` 复制已有媒体到包内并使用相对路径。未启用时只引用本机文件。
- `--chunk-size 2000` 是默认分片大小。长历史不会一次把全部记录注入一个网页。分片页的搜索只覆盖当前片，目录页明确显示各片时间范围。

中断后，以相同查询参数和相同目录增加 `--resume`：

```powershell
python -X utf8 -m unified_mcp.workflow --wechat "wxid_example" --qq "example-qq-id" --after "2026-10-01" --before "2026-10-02" --output "D:\ChatExports\day-example" --transcribe --portable --resume --retry-failed
```

`--retry-failed` 重试失败阶段。`--refresh` 重新检查选中的阶段，仍会使用内容地址缓存；它不表示无条件重新识别所有成功语音。模型路径或本地模型文件版本变化后，语音阶段自动失效并重新调用当前识别服务。可以通过 `--stages media voice video ocr` 限定阶段，或只运行其中一部分。

输出目录包含：

- `job.json`：任务范围、当前阶段、结果状态及阅读页版本。
- `snapshot/coverage.json`：来源、分页终点、记录数、查询范围和文件哈希。
- `snapshot/wechat.jsonl`、`qq.jsonl`：实际选择的平台；没有选中的平台不会被强行打开。
- `snapshot/merged.jsonl`：按时间与来源稳定归并的结果。
- `media/media-messages.jsonl`：媒体补充结果，以及每条记录各阶段的成功、失败或不支持状态。
- `media/summary.json`：处理状态和分阶段计数。
- `reader-0001/reader.html`：阅读页或分片目录。后续媒体补充改变时产生新的 `reader-0002`，不会覆盖此前交付。
- 隐藏 SQLite 检查点：恢复任务所需的页、去重索引与阶段记录，包含聊天数据，应和导出本身一样保管。

可单独使用原来的三个模块，原有基本参数仍然可用：

```powershell
python -X utf8 -m unified_mcp.export_snapshot --qq "example-group-id" --qq-chat-type group --output "D:\ChatExports\group-snapshot"
python -X utf8 -m unified_mcp.process_media --snapshot "D:\ChatExports\group-snapshot" --output "D:\ChatExports\group-media"
python -X utf8 -m unified_mcp.reader_export --input "D:\ChatExports\group-snapshot\merged.jsonl" --media-updates "D:\ChatExports\group-media\media-messages.jsonl" --output "D:\ChatExports\portable-package\reader.html" --portable
```

`process_media` 可额外接受 `--qq-data-root`，在重试时重新寻找 QQ 本地图片；未提供时先使用已配置的 `QQ_MCP_DATA_ROOT`，或从明确配置的 `QQ_MCP_DB_ROOT` 推导对应的 `nt_data`；没有配置时使用快照或 `--qq-enriched` 中已有的解析结果。QQ 语音和视频尚未具备与微信同等的识别链路，结果会标明不支持。微信图片、视频或新语音缺少元数据时，会按会话、原生消息 ID 和时间窗懒加载本地读取器补充；身份歧义或原文件缺失仍明确失败，不猜测对应关系。

## 恢复和完整性的边界

导出每页在 SQLite 中事务提交后才推进游标；QQ 支持时使用数据库 keyset 游标。恢复会重新查询每个已提交页并比较源消息内容摘要，检测早期消息插入、删除、内容变更和分页终点变化。OCR、图片路径和媒体可用性等派生字段不纳入源消息摘要，因此补下载媒体不会被误判为篡改聊天。发现变化时拒绝续接并保留原结果。修改联系人、群类型、日期或页大小也会被拒绝。精确重复的来源记录会去重；相同服务器消息 ID 对应不同本地记录会保留。

SQLite 是检查点的权威来源，JSONL 是其可重建投影。中断或 JSONL 截断后，恢复可以重建已提交记录，不会把半行 JSON 当成完成。旧版仅有 JSONL、没有事务检查点的任务无法伪装成可安全恢复任务，需在新目录重新生成。各输出目录使用操作系统文件锁阻止两个进程同时写入。

恢复时对历史页的检查需要额外读取时间。这不等于锁住微信/QQ 原数据库，也无法保证导出期间外部同步没有发生；工具明确保留“本机已同步范围、非冻结数据库快照”的边界。没有消息文件、媒体实体、识别模型或解析支持时，任务可能返回 `complete_with_errors`：各记录仍交付，但不能称为所有媒体已读懂。

## 便携包与资源占用

将整个 `reader-0001` 目录复制到另一位置，可离线打开。图片、音频、视频和封面都使用相对路径；同一文件可以只复制一份，但同一消息里重复出现的媒体引用仍保留。`reader.media.jsonl` 逐引用记录来源、消息本地 ID、角色、出现序号、文件大小和 SHA-256。HTTP 地址、UNC 网络路径和不存在的文件不会自动下载或成为页面中的远程请求。

源聊天记录和源媒体不会被覆盖。便携包需要额外磁盘空间；复制、输入指纹和恢复核验会增加磁盘读取。旧阅读版本会保留，不会自动清理。

去重索引、补充记录索引和阶段状态保存在磁盘 SQLite，缓存限制为约 2 MiB；最终来源归并只保留每个来源的一条待归并消息。阅读页构建最多保存当前分片条目，默认 2000 条。识别模型的内存占用属于另外的处理阶段，不能把流式合并的内存数字当作 OCR/ASR 整体峰值。

## 已验证的合成场景

`python -X utf8 -m unittest unified_mcp.test_export_workflow unified_mcp.test_reader_export -v` 覆盖单平台、双平台、QQ群游标、重复与碰撞、中断/取消、截断恢复、历史变化拒绝、模型变化、阶段失败重试、未缓存语音补元数据、便携包搬移、分片引用，以及 100,000 条记录的有序流式归并。该归并测试要求 Python 跟踪分配峰值小于 4 MiB，不代表真实数据库或模型占用。

另有使用真实合成 PNG、WAV、GIF 的浏览器验收：复制整个包后在浏览器离线模式打开，检查图片解码、音频时长、分页、筛选、搜索、emoji 和缺失项提示，并检查没有网络请求或旧目录依赖。
