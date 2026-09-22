# 用 uv 部署会议字幕

当前锁定环境为 Linux x86_64、Python 3.12.11、PyTorch CUDA 12.8，以及支持每场会议术语表的 WhisperLiveKit 提交。`.python-version` 固定解释器版本，`uv.lock` 同时用于安装和日常运行，避免普通 `uv run` 意外换成 CUDA 13 或不支持术语表的引擎版本。

## 首次安装

在项目目录运行：

```bash
export http_proxy=http://127.0.0.1:7897
export https_proxy=http://127.0.0.1:7897
export no_proxy=localhost,127.0.0.1,::1
./install.sh
```

安装脚本会检查系统依赖，缺少时给出对应的 `apt` 命令；没有 uv 时从 Astral 官方安装到 `~/.local/bin`，并安装 Python 3.12、同步锁定依赖、创建桌面入口。若当前终端还找不到新安装的 uv，可运行 `export PATH="$HOME/.local/bin:$PATH"`。

Python 默认安装到稳定的 `~/.local/share/uv/python`，避免引用 Snap 编辑器的临时版本目录。脚本尊重自定义的 `UV_PYTHON_INSTALL_DIR`。Ubuntu 上需要 `libtk8.6` 才能让 uv 自带的 Tk 正常显示中文；应用会验证系统 Tk 的字体和工作线程回调，再自动重新启动。实测 uv 的 Python 3.12.12～3.12.14 使用 Tk 9，会丢失后台回调，因此固定使用 3.12.11。`build-essential` 用于编译 Triton 的 GPU 调用代码，避免识别时退回较慢实现。

脚本首次创建 `~/.config/meeting-subtitles/env` 时会保存代理。桌面启动器读取这份文件，因为它不继承终端里的代理变量。已有配置不会被安装脚本覆盖，需要换代理时修改该文件。

## 下载模型

```bash
uv run --locked python tools/models.py download --proxy http://127.0.0.1:7897
uv run --locked meeting-subtitles-doctor
```

下载命令默认准备以下四份权重和对应的分词器、配置文件，总计约 18.4 GiB，此外还需要为 Python 依赖和下载缓存预留空间：

| 用途 | 模型 | 权重大小约 |
| --- | --- | --- |
| 流式识别解码器 | Whisper `large-v3.pt` | 2.9 GiB |
| 识别编码器 | `Systran/faster-whisper-large-v3` | 2.9 GiB |
| 流式中文翻译 | `facebook/nllb-200-distilled-1.3B` | 5.1 GiB |
| 整句润色 | `Qwen/Qwen3-4B-Instruct-2507` | 7.5 GiB |

引擎同时需要原始 Whisper 和 faster-whisper 权重。NLLB 使用 Transformers 后端，不需要另行转换为 CTranslate2 格式。下载不会加载模型或占用显卡；原始 Whisper 文件通过 SHA256 校验后才写入正式缓存路径。

下载命令还会把上游的短预热音频保存到 `~/.cache/meeting-subtitles/warmup.wav`，防止引擎启动时临时联网获取示例。没有这份音频时会跳过预热，首次语音会承担 GPU 预热开销。

中断后重新运行同一命令，Hugging Face 会复用缓存并继续未完成下载，Whisper 的 `.partial` 文件也支持续传。不要在下载过程中运行 `prune --yes`，以免删除正在写入的临时文件。可单独下载或先预览：

```bash
uv run --locked python tools/models.py download --only engine
uv run --locked python tools/models.py download --only refine
uv run --locked python tools/models.py download --dry-run
uv run --locked python tools/models.py status
```

默认 Hugging Face 缓存为 `~/.cache/huggingface/hub`，原始 Whisper 为 `~/.cache/whisper`。下载命令和引擎均读取配置文件；使用 `HF_HUB_CACHE`、`HF_HOME` 或 `XDG_CACHE_HOME` 自定义位置时，请在终端与配置文件中保持一致。下载阶段会临时启用联网并禁用 Xet；会议启动检测到本地模型后自动离线运行。

## 启动与维护

在应用列表里打开「会议字幕」，或运行：

```bash
uv run --locked meeting-subtitles
```

只启动引擎或检查配置：

```bash
uv run --locked meeting-subtitles-engine
uv run --locked meeting-subtitles-engine --dry-run
uv run --locked meeting-subtitles-doctor
```

完整模型需要较多显存。自检会检查 GPU；「整句润色」也会在加载前检查剩余显存。显存不足时可在启动器中关闭润色，保留识别和流式翻译。

更新代码后运行 `uv sync --locked`。修改依赖约束时先运行 `uv lock`，检查锁文件变更后再同步。开发验证：

```bash
uv run --locked python -m pytest tests -q
uv run --locked ruff check meeting_subtitles tools tests
```

备份与恢复模型：

```bash
uv run --locked python tools/models.py backup --to /mnt/backup/meeting-models
uv run --locked python tools/models.py restore --from /mnt/backup/meeting-models
```

安装来源与上游说明：[uv 安装文档](https://docs.astral.sh/uv/getting-started/installation/)、[PyTorch 安装版本](https://pytorch.org/get-started/previous-versions/)、[WhisperLiveKit](https://github.com/QuentinFuxa/WhisperLiveKit)、[NLLB 模型](https://huggingface.co/facebook/nllb-200-distilled-1.3B)、[Qwen 润色模型](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507)。
