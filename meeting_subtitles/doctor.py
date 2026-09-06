"""Check that this machine can run the app, and say what to do when it cannot.

Run this first, after installing and after any system upgrade::

    meeting-subtitles-doctor
    python -m meeting_subtitles.doctor

Everything the app needs is checked separately, so a failure names one thing to
fix rather than leaving you to guess which of ffmpeg, PulseAudio, CUDA, Tk or
the model cache went missing. Checks that only degrade the experience report a
warning and never fail the run.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import unicodedata

OK, WARN, FAIL = "ok", "warn", "fail"
MARK = {OK: "\033[32m✓\033[0m", WARN: "\033[33m!\033[0m", FAIL: "\033[31m✗\033[0m"}

LABEL_WIDTH = 18


def _pad(text: str, width: int = LABEL_WIDTH) -> str:
    """Pad to a terminal *column* count, not a character count.

    Chinese labels are double-width, so ``f"{label:<18}"`` overshoots by one
    column per character and the detail column comes out ragged.
    """
    columns = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1
                  for c in text)
    return text + " " * max(0, width - columns)

#: Minimum interpreter. Matches whisperlivekit's own floor.
MIN_PYTHON = (3, 11)

#: Measured on this configuration: whisper large-v3 plus its CTranslate2
#: encoder plus NLLB-1.3B, and Qwen3-4B in bfloat16 with room to generate.
ENGINE_VRAM_GB = 14
REFINE_VRAM_GB = 9


def _engine_is_up(timeout: float = 2) -> bool:
    """Health-check the engine, bypassing any configured proxy.

    Without an empty ProxyHandler urllib sends 127.0.0.1 through the SOCKS
    proxy the desktop exports, and the check fails on a running engine.
    """
    from urllib.error import URLError
    from urllib.request import ProxyHandler, build_opener
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:8000/health", timeout=timeout) as response:
            return response.status == 200
    except (URLError, OSError, ValueError):
        return False


class Result:
    def __init__(self, status: str, label: str, detail: str = "",
                 fix: str = "") -> None:
        self.status, self.label, self.detail, self.fix = status, label, detail, fix


def _run(command: list[str], timeout: float = 5) -> str | None:
    try:
        out = subprocess.run(command, capture_output=True, text=True,
                             timeout=timeout, check=True,
                             env=dict(os.environ, LC_ALL="C", LANG="C"))
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip()


# ------------------------------------------------------------------ checks

def check_python() -> Result:
    version = sys.version_info
    text = f"{version.major}.{version.minor}.{version.micro}"
    if version[:2] < MIN_PYTHON:
        return Result(FAIL, "Python", text,
                      f"需要 Python {'.'.join(map(str, MIN_PYTHON))} 或更高。")
    return Result(OK, "Python", text)


def check_ffmpeg() -> Result:
    if shutil.which("ffmpeg") is None:
        return Result(FAIL, "ffmpeg", "未安装",
                      "sudo apt install ffmpeg")
    banner = (_run(["ffmpeg", "-version"]) or "").splitlines()
    version = banner[0].split(" ")[2] if banner else "?"
    formats = _run(["ffmpeg", "-hide_banner", "-devices"]) or ""
    if "pulse" not in formats:
        return Result(FAIL, "ffmpeg", f"{version}（缺少 pulse 输入设备）",
                      "发行版自带的 ffmpeg 通常带 pulse；静态构建版可能不带。\n"
                      "sudo apt install --reinstall ffmpeg")
    return Result(OK, "ffmpeg", version)


def check_audio_server() -> Result:
    if shutil.which("pactl") is None:
        return Result(FAIL, "音频服务", "找不到 pactl",
                      "sudo apt install pulseaudio-utils")
    info = _run(["pactl", "info"])
    if info is None:
        return Result(FAIL, "音频服务", "pactl 无法连接",
                      "确认桌面会话中的 PipeWire/PulseAudio 正在运行：\n"
                      "systemctl --user status pipewire pipewire-pulse")
    server = next((line.split(":", 1)[1].strip()
                   for line in info.splitlines()
                   if line.startswith("Server Name:")), "?")
    return Result(OK, "音频服务", server)


def check_monitor_source() -> Result:
    try:
        from meeting_subtitles import audio
        monitor = audio.default_monitor_source()
    except Exception as exc:
        return Result(FAIL, "系统声音源", str(exc)[:70],
                      "确认「设置 → 声音」里选中了一个输出设备。")
    return Result(OK, "系统声音源", monitor)


def check_microphone() -> Result:
    try:
        from meeting_subtitles import audio
        mic = audio.default_mic_source()
    except Exception as exc:
        return Result(WARN, "麦克风", str(exc)[:70],
                      "只转录对方的话时不需要麦克风；用 --no-mic 关掉即可。")
    return Result(OK, "麦克风", mic)


def check_gpu() -> Result:
    try:
        import torch
    except ImportError:
        return Result(FAIL, "PyTorch", "未安装", "pip install -e .")
    if not torch.cuda.is_available():
        return Result(WARN, "GPU", f"torch {torch.__version__}，CUDA 不可用",
                      "会退到 CPU 运行，实时性达不到要求。\n"
                      "检查 nvidia-smi，以及 torch 是否是 CUDA 版本。")
    name = torch.cuda.get_device_name(0)
    total = torch.cuda.get_device_properties(0).total_memory / 1024 ** 3
    detail = f"{name}, {total:.0f} GB"
    if total < 12:
        return Result(WARN, "GPU", detail,
                      "显存偏小。用 --no-refine 关掉整句润色可省约 8 GB。")
    return Result(OK, "GPU", detail)


def check_ctranslate2_cuda() -> Result:
    """CTranslate2 needs CUDA 12, and nothing else notices when it is missing.

    This one is worth its own check because of how it fails: the engine starts
    normally, answers health checks and accepts audio, then throws
    "Library libcublas.so.12 is not found" on *every* chunk and produces an
    empty transcript. ``ctranslate2.get_cuda_device_count()`` still returns 1,
    so the only honest probe is to try loading the library itself.
    """
    import ctypes
    try:
        import torch
    except ImportError:
        return Result(FAIL, "CTranslate2", "PyTorch 未安装", "pip install -e .")
    if not torch.cuda.is_available():
        return Result(WARN, "CTranslate2", "CUDA 不可用，跳过检查")
    cuda = torch.version.cuda or "?"
    try:
        ctypes.CDLL("libcublas.so.12")
    except OSError:
        return Result(
            FAIL, "CTranslate2", f"找不到 libcublas.so.12（当前 torch 带的是 CUDA {cuda}）",
            "引擎会正常启动但每一段音频都识别失败，转录是空的。\n"
            "PyPI 上默认的 torch 现在带 CUDA 13，而 CTranslate2 需要 CUDA 12：\n"
            "pip install --force-reinstall torch torchaudio "
            "--index-url https://download.pytorch.org/whl/cu129")
    return Result(OK, "CTranslate2", f"libcublas.so.12 可加载（CUDA {cuda}）")


def check_engine_package() -> Result:
    if importlib.util.find_spec("whisperlivekit") is None:
        return Result(FAIL, "whisperlivekit", "未安装", "pip install -e .")
    try:
        from importlib.metadata import version
        installed = version("whisperlivekit")
    except Exception:
        installed = "?"
    # The glossary is passed as a ?context= query parameter, which older
    # servers silently ignore rather than reject -- so probe for the module
    # that implements it instead of trusting the version number.
    if importlib.util.find_spec("whisperlivekit.session_asr_proxy") is None:
        supported = False
    else:
        from whisperlivekit import session_asr_proxy
        supported = hasattr(session_asr_proxy, "session_context_capability")
    if not supported:
        return Result(WARN, "whisperlivekit", f"{installed}（不支持术语条件化）",
                      "领域术语表会被静默忽略，识别专有名词的准确率会下降。\n"
                      "装带该功能的版本：\n"
                      "pip install 'whisperlivekit @ "
                      "git+https://github.com/QuentinFuxa/WhisperLiveKit@b781ce9'")
    return Result(OK, "whisperlivekit", f"{installed}（支持术语条件化）")


def check_models() -> Result:
    from meeting_subtitles import serve
    missing = []
    if not (serve.whisper_cache() / "large-v3.pt").is_file():
        missing.append("whisper large-v3")
    if not serve.has_snapshot("models--Systran--faster-whisper-large-v3",
                              "model.bin"):
        missing.append("faster-whisper large-v3")
    repo = "models--facebook--nllb-200-distilled-1.3B"
    if not (serve.has_snapshot(repo, "*.bin")
            or serve.has_snapshot(repo, "*.safetensors")):
        missing.append("NLLB-1.3B")
    if missing:
        return Result(WARN, "模型缓存", "缺少 " + "、".join(missing),
                      "首次启动引擎时会自动下载（约 18 GB，需要能连上 "
                      "huggingface.co）。\n"
                      "已有备份的话：python tools/models.py restore --from <目录>")
    return Result(OK, "模型缓存", "已就绪，可离线启动")


def check_free_vram() -> Result:
    """Free VRAM *right now*, which is what the next meeting actually gets.

    Worth its own check because of how running out fails: the refiner and the
    engine are separate processes on one card, so the shortfall lands on
    whichever allocates next. That is the ASR, which then throws on every chunk
    while its WebSocket stays open and its transcript file stays readable --
    a meeting that looks healthy and silently stops producing text.
    """
    try:
        import torch
    except ImportError:
        return Result(FAIL, "可用显存", "PyTorch 未安装", "pip install -e .")
    if not torch.cuda.is_available():
        return Result(WARN, "可用显存", "CUDA 不可用，跳过检查")
    gb = 1024 ** 3
    free, total = torch.cuda.mem_get_info()
    # An engine that is already up has its ~14 GB, and asking for it twice
    # would report a shortage on a machine that is about to work perfectly.
    engine_up = _engine_is_up()
    needed_engine = 0 if engine_up else ENGINE_VRAM_GB
    detail = (f"{free / gb:.1f} GB 空闲 / 共 {total / gb:.1f} GB"
              + ("（引擎已加载）" if engine_up else ""))
    if free / gb < needed_engine:
        return Result(FAIL, "可用显存", detail,
                      f"引擎本身就需要约 {ENGINE_VRAM_GB} GB。"
                      "用 nvidia-smi 看是谁占着，先把它关掉。")
    if free / gb < needed_engine + REFINE_VRAM_GB:
        return Result(WARN, "可用显存", detail,
                      f"够跑引擎，但不够再加整句润色（约 {REFINE_VRAM_GB} GB）。\n"
                      "润色会自动跳过并在字幕条上说明；想要润色就先腾出显存。")
    return Result(OK, "可用显存", detail)


def check_refine_model() -> Result:
    """The polish model is downloaded separately, and its absence is quiet."""
    from meeting_subtitles import refine
    if not refine.is_cached(refine.DEFAULT_MODEL):
        return Result(WARN, "润色模型", f"未下载 {refine.DEFAULT_MODEL}",
                      "整句润色会自动跳过，字幕只显示流式译文。\n"
                      "要用的话先下载一次（约 7.6 GB，需要能连上 huggingface.co）：\n"
                      "HF_HUB_OFFLINE=0 .venv/bin/python -c \"from huggingface_hub import "
                      f"snapshot_download; snapshot_download('{refine.DEFAULT_MODEL}')\"")
    size = refine.weights_bytes(refine.DEFAULT_MODEL) / 1024 ** 3
    return Result(OK, "润色模型", f"{refine.DEFAULT_MODEL}（{size:.1f} GB，可离线加载）")


def check_tk_fonts() -> Result:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return Result(WARN, "图形界面", "当前没有图形会话",
                      "无法检查字体。字幕窗口需要图形会话；"
                      "纯命令行下可用 --no-overlay。")
    try:
        from meeting_subtitles.tkfix import cjk_families
    except ImportError as exc:
        return Result(FAIL, "Tk", str(exc)[:70], "sudo apt install python3-tk")
    families = cjk_families()
    if families:
        return Result(OK, "中文字体", families[0])

    # Tk seeing no CJK family has two very different causes, and telling the
    # user to install a font they already have is worse than saying nothing.
    installed = _run(["fc-list", ":lang=zh-cn", "family"]) or ""
    if installed.strip():
        first = installed.splitlines()[0].split(",")[0]
        return Result(OK, "中文字体", f"{first}（Tk 需要预加载系统 Tcl/Tk）",
                      "这个 Python 的 Tk 是没有 Xft 的构建（Anaconda 常见），"
                      "看不到系统字体。\n"
                      "启动时会自动改用系统 Tcl/Tk 重新执行一次，中文能正常显示。")
    return Result(WARN, "中文字体", "系统里没有安装中文字体",
                  "界面里的中文会显示成方块。\n"
                  "sudo apt install fonts-noto-cjk")


def check_display_server() -> Result:
    session = os.environ.get("XDG_SESSION_TYPE", "?")
    if session == "wayland":
        if os.environ.get("DISPLAY"):
            return Result(OK, "显示服务器", "wayland（经 XWayland，功能完整）")
        return Result(WARN, "显示服务器", "wayland，且没有 XWayland",
                      "字幕窗口无法置顶也无法圆角。请确认已安装 "
                      "xwayland 包。")
    return Result(OK, "显示服务器", session)


def check_rounding() -> Result:
    if importlib.util.find_spec("Xlib") is None:
        return Result(WARN, "窗口圆角", "未安装 python-xlib",
                      "窗口会是直角，功能不受影响。\n     pip install python-xlib")
    return Result(OK, "窗口圆角", "python-xlib 可用")


def check_process_control() -> Result:
    if importlib.util.find_spec("psutil") is None:
        return Result(WARN, "引擎关闭", "未安装 psutil",
                      "启动器里的「关闭引擎」按钮会失效。\npip install psutil")
    return Result(OK, "引擎关闭", "psutil 可用")


def check_engine_running() -> Result:
    if _engine_is_up():
        return Result(OK, "引擎状态", "正在运行")
    return Result(WARN, "引擎状态", "未运行",
                  "这是正常的——启动器会在需要时自动拉起。\n"
                  "也可以手动启动：meeting-subtitles-engine")


CHECKS = (
    ("环境", [check_python, check_engine_package, check_gpu,
              check_ctranslate2_cuda, check_free_vram]),
    ("音频", [check_ffmpeg, check_audio_server, check_monitor_source,
              check_microphone]),
    ("界面", [check_display_server, check_tk_fonts, check_rounding]),
    ("运行", [check_models, check_refine_model, check_process_control,
              check_engine_running]),
)


def main(argv: list[str] | None = None) -> int:
    del argv
    print("会议字幕环境自检\n")
    failures = warnings = 0
    for section, checks in CHECKS:
        print(f"\033[1m{section}\033[0m")
        for check in checks:
            try:
                result = check()
            except Exception as exc:                # a check must never crash
                result = Result(FAIL, check.__name__, f"检查本身出错: {exc}")
            print(f"  {MARK[result.status]} {_pad(result.label)}{result.detail}")
            if result.fix and result.status != OK:
                for line in result.fix.splitlines():
                    print(f"      \033[2m{line.strip()}\033[0m")
            failures += result.status == FAIL
            warnings += result.status == WARN
        print()

    if failures:
        print(f"\033[31m{failures} 项不通过\033[0m"
              + (f"，{warnings} 项提示。" if warnings else "。")
              + " 先修掉不通过的项。")
        return 1
    if warnings:
        print(f"\033[33m全部必要项通过，{warnings} 项提示\033[0m（不影响启动）。")
        return 0
    print("\033[32m全部通过。\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
