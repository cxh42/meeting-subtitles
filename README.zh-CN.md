# 会议字幕

**开会时实时生成中英对照字幕，全部在自己的显卡上跑，不联网、不调 API。**

[English](README.md) · [设计笔记](docs/design-notes.zh-CN.md)

屏幕底部浮一条字幕。最上面一行是刚说完那句话的完整中文翻译；下面是实时滚动的英文原文，
再下面是随口蹦出来的中文草稿。模型缓存好之后全程不需要网络，音频不会离开这台机器。

![字幕条](docs/images/subtitle-bar.png)

做这个是为了一个具体场景：母语不是英语，要和外国同事开会，其中一些人口音很重，
既想跟上、又想留一份转录。它识别英文语音，翻译成中文。

---

## 和会议软件自带的字幕比

|  | Zoom / Teams 自带字幕 | 这个 |
|---|---|---|
| 音频去哪 | 厂商的服务器 | 哪也不去 |
| 中文翻译 | 经常没有，或者要付费 | 一直有，本地跑 |
| 适用范围 | 只有那一个软件 | 电脑在放的任何声音 |
| 转录记录 | 各家一套，有时要收费 | 一个属于你的 Markdown 文件 |
| 领域术语 | 通用 | 自己定的术语表 |

它抓的是**扬声器正在放的东西**，不是钩进某个特定程序，所以 Zoom、Teams、Meet、
浏览器标签页、本地视频播放器都一样能用——不需要插件，也不需要那个程序配合。

## 工作方式

```
系统声音（对方说的话）┐
                     ├─ ffmpeg 混音 → 16kHz 单声道 PCM
麦克风（你说的话）    ┘        │
                               ▼
                  WhisperLiveKit  ── ws://127.0.0.1:8000/asr
                  Whisper large-v3  →  英文原文，流式
                  NLLB-1.3B         →  中文草稿，流式
                                       │
                                       ▼
                  Qwen3-4B  → 一句说完后，整句重新翻译
                                       │
                ┌──────────────────────┴──────────────────────┐
                ▼                                             ▼
           悬浮字幕条                                    transcript.md
```

**两级翻译**是让它读起来顺的关键。流式翻译必须在句子结束前就把词吐出来，
结果就是逐词硬翻、读着费劲。那份草稿仍然会显示——用灰色，让你**立刻**有东西可看——
但一旦一句话结束，本地的指令模型会把整句重新翻一遍，用橙色钉在最上面。
草稿延迟约 1 秒；整句润色在说话人停下后 0.1–0.4 秒出现。

## 需要什么

| | |
|---|---|
| 系统 | Linux，PulseAudio 或 PipeWire。在 Ubuntu 24.04（GNOME，Wayland 经 XWayland）上测试 |
| 显卡 | NVIDIA，开整句润色约 **22 GB 显存**，用 `--no-refine` 约 **14 GB** |
| 硬盘 | 模型约 18 GB，只下一次 |
| Python | 3.11 以上，需要 `python3-tk` |

没有显卡或显存不够也能在 CPU 上跑，但达不到实时。那种情况这个工具不合适。

## 安装

```bash
git clone https://github.com/cxh42/meeting-subtitles
cd meeting-subtitles
./install.sh
```

`install.sh` 会检查缺哪些系统包（缺了就打印出那一条 `apt` 命令让你运行）、
建虚拟环境、装依赖、注册桌面入口，最后跑一遍环境自检。

自检随时可以单独跑——系统升级之后，或者哪天不好使了：

```bash
.venv/bin/meeting-subtitles-doctor
```

它把 Python、ffmpeg、音频服务、系统声音源、CUDA、中文字体、显示服务器、模型缓存
分开报，哪一项不过就直接指向要修的那一件事。

## 使用

![启动器](docs/images/launcher.png)

在应用列表里搜「**会议字幕**」，或者运行 `.venv/bin/meeting-subtitles`。

1. 填会议名称，调好字幕大小和透明度。
2. 首次使用要加载引擎（模型已缓存时 20–40 秒）。「开始会议」变亮就是好了。
3. 启动器隐藏，字幕条出现。
4. 开完点字幕条上的「**结束并保存**」。

结果都在 `~/Meetings/<日期>_<名称>/`：`transcript.md` 每 3 秒原子写入一次，
还有 `audio.wav`，方便事后重新转录。

点字幕条上的「**历史**」看到目前为止的全部转录。**往上滚会冻结**——
新句子不会再推着你正在读的内容跑——同时浮出一个按钮，一键跳回最新。

![历史窗口](docs/images/history.png)

### 命令行

```bash
.venv/bin/meeting-subtitles-engine          # 启动引擎，保持运行
.venv/bin/meeting-subtitles-run --help      # 跑一场会议，不要图形界面
```

常用参数：

```bash
--context "Kubernetes, Anirudh, SLO"   # 这次会议的术语和人名
--no-mic                               # 只转录对方，不录自己
--no-refine                            # 关掉整句润色，省约 8 GB 显存
--no-overlay                           # 只记录转录，不显示窗口
--domain general                       # 不用内置的 CS/AI 术语表
--font-size 24 --opacity 0.8
```

### 领域术语

默认按 **CS / AI 研究**配置：62 个术语传给识别模型，让缩写不被音译
（LoRA、RLHF、vLLM、KV cache、NeurIPS、FLOPs），同时把中文对照规范给润色模型，
所以 `ablation study` 出来是「消融实验」而不是直译。
用 `--domain general` 关掉，或者改 `meeting_subtitles/domain.py` 加你自己的领域。

## 模型许可证

代码本身是 Apache-2.0。但它下载的模型许可证不一样，其中一个在公司里用之前要知道：

| 模型 | 许可证 |
|---|---|
| Whisper large-v3 | MIT |
| faster-whisper large-v3 | MIT |
| **NLLB-200-distilled-1.3B** | **CC-BY-NC-4.0 — 仅限非商业用途** |
| Qwen3-4B-Instruct-2507 | Apache-2.0 |

NLLB 是那个流式翻译器。需要商用的话，换一个翻译后端
（见 `whisperlivekit --help`），或者用 `--target-language ""` 只靠整句润色。

## 备份模型

18 GB 下第二遍很慢，尤其是 `huggingface.co` 连不上的网络环境。清盘重装之前：

```bash
.venv/bin/python tools/models.py status
.venv/bin/python tools/models.py backup --to /mnt/backup/meeting-models
```

装好新系统之后：

```bash
.venv/bin/python tools/models.py restore --from /mnt/backup/meeting-models
```

## 已知限制

- **只支持 Linux。** macOS 需要 BlackHole 一类的虚拟声卡；Windows 需要
  WASAPI loopback 后端。两个都没做。
- **英文进、中文出。** 源语言固定为英文是有意的——让 Whisper 自动检测语言，
  在重口音下的准确率损失很明显，而重口音正是这东西存在的理由。
- **单路混音**，所以转录里分不出谁说的话。需要区分说话人的话，
  引擎加 `--diarization` 启动。
- 长时间静音时 Whisper 偶尔会幻觉出一句短话。真实会议有持续的环境底噪，
  这个问题比纯数字静音下轻得多。

## 致谢

转录引擎是 Quentin Fuxa 的 [WhisperLiveKit](https://github.com/QuentinFuxa/WhisperLiveKit)
（Apache-2.0），本项目把它作为依赖安装并驱动，没有复制它的任何源码。
它自身又建立在 SimulStreaming 和 whisper_streaming（ÚFAL）、silero-vad（snakers4）、
NeMo（NVIDIA）之上。详见 [NOTICE](NOTICE)。

本项目采用 Apache-2.0 许可证，见 [LICENSE](LICENSE)。
