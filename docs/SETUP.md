# 安装与配置

## 1. 安装 Python 网关

在 Windows x64 上安装 Python 3.12，然后在源码目录执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

安装只提供网关及其 Python 依赖，不会下载微信读取器、QQ DLL 或语音模型，也不会扫描账号或修改客户端配置。

## 2. 外部微信读取器

先确认读取器可以独立读取本人账号的数据。设置 `UNIFIED_WECHAT_COMMAND` 和可选 JSON 数组 `UNIFIED_WECHAT_ARGS`。默认查找 `%LOCALAPPDATA%/wx-mcp/wx-mcp.exe`；若不存在，微信调用会报告错误。

此网关使用既定的 `chat_timeline`、`messages`、`media_resources` 等接口与字段。仅仅同样支持 MCP 的其他微信项目不一定能替换；更换核心应核对接口并实测消息身份、分页和媒体输出。

底层读取器自己的数据库密钥、图片密钥、账号选择和缓存初始化依照该读取器配置完成。公开仓库不提供这些个人凭据或二进制。

## 3. 可选 QQ

设置本人账号的路径：

```powershell
$env:QQ_MCP_DB_ROOT = 'C:/path/to/account/nt_qq/nt_db'
$env:QQ_MCP_EXTENSION = 'C:/path/to/sqlite_ext_ntqq_db.dll'
```

`QQ_MCP_DB_ROOT` 不设时，QQ 工具仍会列出，但调用时明确提示尚未配置。程序不会自动选择账户。扩展 DLL 独立来自 [ntdb_unwrap](https://github.com/artiga033/ntdb_unwrap)，下载或构建时请核对来源与许可。

本项目可使用已有的受保护密钥文件。若需要为本人正在运行的 QQ 初始化每数据库密钥，**显式**运行：

```powershell
.\.venv\Scripts\python.exe -m unified_mcp.qq_keys
```

该命令读取本机 QQ 进程中候选数据，只保存经本地数据库验证的密钥，并用当前 Windows 用户的 DPAPI 保护。不会在 MCP 启动时自动执行。它可能受 QQ 版本、进程权限或数据库变化影响；失败并不表示聊天记录不存在。

然后运行：

```powershell
.\.venv\Scripts\python.exe -m unified_mcp.server --call unified_sources
```

如果已有兼容文件，用 `QQ_MCP_RAW_KEY_FILE` 指定每数据库 DPAPI 密钥表，或 `QQ_MCP_KEY_FILE` 指定兼容的单密钥文件。不要把密钥值放入仓库或公开日志。

## 4. 客户端

`examples/codex.toml` 和 `examples/claude.mcp.json` 使用相同的 Python 模块入口。必须把示例路径改为自己的安装位置；QQ 不使用时可移除相关环境变量。

启用统一入口后，原来独立的 QQ MCP 可以在客户端配置中停用，避免重复工具与后台进程。多个客户端同时运行仍可能各自启动一个网关；共享后台属于尚未完成的优化。

## 5. 可选本地语音

从仓库根运行 `scripts/setup_voice.py --model sensevoice --plan` 查看计划，去掉 `--plan` 后才会建立独立环境并下载指定官方模型。

模型有独立条款，详见安装计划中的链接及 THIRD_PARTY_NOTICES。安装器验证下载大小与 SHA-256，只将固定模型文件放在包外目录；不会覆盖已存在但内容不匹配的模型目录。

`both` 安装 SenseVoice 与 Whisper，适合需要默认中文识别与可选复核的用户。`whisper` 只安装 Whisper 模型。运行结果标出实际引擎，成功结果可复用，失败不会永久阻止后续重试。ASR worker 空闲 60 秒退出。

使用自定义 `WX_UNIFIED_ASR_PYTHON` 时，安装脚本要求它与 `WX_UNIFIED_ASR_HOME/venv/Scripts/python.exe` 一致，防止误向任意已有环境安装包。也可以自行准备环境并仅配置运行路径。

## 6. 图片与视频

Windows OCR 需要系统已安装相应语言。`include_image_text=false` 可跳过 OCR，`include_media_paths=false` 可用于先读取纯消息快照。OCR 不是图像场景理解，需查看内容时使用图片预览工具或直接打开文件。

FFmpeg 独立安装，FFprobe 可选。首帧解码验证需要 FFmpeg，只有 FFprobe 不足以完成该验证。设置 `UNIFIED_FFMPEG`、`UNIFIED_FFPROBE` 可指定实际可执行文件。视频输出明确说明探测范围，不把封面当作完整视频。

## 配置变量

| 变量 | 用途 |
|---|---|
| `WXQQ_DATA_DIR` | Python 网关数据目录；默认 `%LOCALAPPDATA%/wx-qq-mcp` |
| `UNIFIED_WECHAT_COMMAND` / `UNIFIED_WECHAT_ARGS` | 外部微信读取器与参数数组 |
| `UNIFIED_IDLE_SECONDS` | 微信子进程空闲退出时间，默认 60 |
| `QQ_MCP_DB_ROOT` | 明确选择的 QQ 账号数据库目录 |
| `QQ_MCP_EXTENSION` | 外置 NTQQ VFS 扩展 DLL |
| `QQ_MCP_DATA_ROOT` | QQ 媒体数据目录，默认从数据库目录推导 |
| `QQ_MCP_RAW_KEY_FILE` / `QQ_MCP_KEY_FILE` | 已有的 DPAPI 保护文件 |
| `UNIFIED_MEDIA_METADATA_DIR` | 媒体元数据侧车目录，默认数据目录中的 `media_metadata` |
| `WX_UNIFIED_ASR_HOME` | 独立语音环境与模型根目录 |
| `WX_UNIFIED_ASR_PYTHON` | 语音工作进程的 Python |
| `WX_UNIFIED_SENSE_MODEL` | SenseVoice 模型目录 |
| `WX_UNIFIED_ASR_MODEL` | Whisper 模型目录 |
| `WX_UNIFIED_VOICE_CACHE` | 成功转写及解码音频缓存 |
| `WX_UNIFIED_VOICE_MEDIA_CACHE` | 既有微信语音缓存位置 |
| `WX_UNIFIED_ASR_ENGINE` | 可显式设为 `whisper`，否则优先已安装的默认引擎 |
| `UNIFIED_FFMPEG` / `UNIFIED_FFPROBE` | 视频处理程序路径 |

`WXQQ_DATA_DIR` 不接管外部微信核心自己的缓存。导出命令的 `--output` 也是独立的显式路径。
