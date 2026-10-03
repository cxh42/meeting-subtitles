"""Start the transcription engine with the environment it needs.

This is what ``start-meeting-server.sh`` and the GUI launcher both run. Doing
it in Python rather than in shell buys two things: the process the launcher
watches *is* the server, so its liveness check is real; and every environment
variable is set before ``whisperlivekit`` is imported -- which matters more
than it looks, because ``huggingface_hub`` reads ``HF_HUB_OFFLINE`` into a
module constant at import time, and setting it afterwards does nothing at all.

    python -m meeting_subtitles.serve
    python -m meeting_subtitles.serve --model large-v3-turbo --port 8010

Three environment problems are handled here, all of them learned the hard way
on this machine:

* GNOME hands out ``socks://`` proxies, which httpx rejects outright ("Unknown
  scheme for proxy URL") -- enough to abort startup while building an HTTP
  client that is never used.
* Upper- and lower-case proxy variables can disagree, and the native HTTP
  clients inside ``huggingface_hub`` read the upper-case ones.
* ``huggingface.co`` revalidates every cached file on load. Where that hostname
  resolves to a black-holed address, the connection sits in SYN-SENT for
  minutes before falling back to the cache -- indistinguishable from a slow
  model load. Once the weights are on disk there is nothing to revalidate, so
  we go fully offline and drop the proxies with it.
"""

import argparse
import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from meeting_subtitles import paths

logger = logging.getLogger(__name__)

#: Where the engine reports the backend failures it otherwise swallows. The
#: meeting process polls it; see :class:`BackendFailures`.
BACKEND_STATUS_PATH = "/meeting-subtitles/backend"

#: Freed memory CTranslate2's CUDA pool may keep rather than hand back to the
#: driver. Its whole working set is the large-v3 encoder's ~140 MB per 30 s
#: window; the cap only bounds the unforeseen.
CT2_POOL_KEEP_BYTES = 1 << 30

_CU_MEMPOOL_ATTR_RELEASE_THRESHOLD = 4

#: A failure after this much quiet starts a new streak, which is when the
#: GPU's state is worth writing down.
_STREAK_GAP_SECONDS = 10.0

DEFAULTS = {
    "MODEL": "large-v3",
    "LANGUAGE": "en",
    "TARGET_LANGUAGE": "zh",
    "NLLB_SIZE": "1.3B",
    "PORT": "8000",
    # The server's own default is 5 s, which almost never happens in a meeting,
    # so the whole call ends up as one giant transcript line. ~1.2 s breaks at
    # natural sentence gaps.
    "PAUSE_SECONDS": "1.2",
}

PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")


def configure_allocator() -> None:
    """Stop the engine's PyTorch cache from ballooning during long speech.

    SimulStreaming re-runs the decoder over a prompt that grows token by token,
    so every call asks for slightly larger blocks than the last, and PyTorch's
    default allocator cannot reuse the old ones: it caches them and asks the
    driver for more. Measured on two minutes of pause-free speech, the engine
    grew from 11.3 GB to 22.6 GB; with expandable segments it peaked at
    14.3 GB with the same transcript. The cache is only emptied after a pause
    of 5 s or more, so a long monologue next to the 8 GB refiner filled the
    32 GB card -- the out-of-memory that stopped a meeting at 2:19.

    ``setdefault``: an explicit setting in the environment wins. Must run before
    torch is imported, which reads it once.
    """
    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")


def apply_env_file() -> None:
    """Load the optional environment file next to the settings.

    A desktop launcher starts the app with none of the shell's environment, so
    anything set in ``.bashrc`` -- notably the proxy -- is invisible here.
    """
    for key, value in paths.read_env_file().items():
        os.environ[key] = value


def normalise_proxies() -> None:
    for name in list(os.environ):
        if name.lower() not in PROXY_VARS:
            continue
        value = os.environ[name]
        if value.startswith("socks://"):
            os.environ[name] = "socks5://" + value[len("socks://"):]
    # Mirror lower-case onto upper-case; native clients read the upper ones.
    for name in PROXY_VARS:
        value = os.environ.get(name)
        if value:
            os.environ.setdefault(name.upper(), value)


def hf_cache() -> Path:
    """Mirror of huggingface_hub's own cache resolution.

    Deliberately not routed through :mod:`meeting.paths`: the point is to look
    in exactly the place the library will look, not in the place this app
    would have chosen.
    """
    explicit = (os.environ.get("HF_HUB_CACHE")
                or os.environ.get("HUGGINGFACE_HUB_CACHE"))
    if explicit:
        return Path(explicit).expanduser()
    home = os.environ.get("HF_HOME")
    if home:
        return Path(home).expanduser() / "hub"
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache.expanduser() / "huggingface" / "hub"


def whisper_cache() -> Path:
    """Where openai-whisper puts its ``.pt`` checkpoints, on every platform."""
    default = Path.home() / ".cache"
    return Path(os.environ.get("XDG_CACHE_HOME", default)) / "whisper"


def has_snapshot(repo_dir: str, pattern: str) -> bool:
    """True when matching files and every indexed weight shard are cached."""
    snapshots = hf_cache() / repo_dir / "snapshots"
    if not snapshots.is_dir():
        return False
    for revision in snapshots.iterdir():
        # A glob iterator itself is always truthy, even for an empty snapshot.
        if not any(item.is_file() for item in revision.glob(pattern)):
            continue
        complete = True
        for index in revision.glob(f"{pattern}.index.json"):
            try:
                required = set(json.loads(index.read_text())["weight_map"].values())
                # One small Qwen shard can finish well before the large ones.
                # Calling that model cached prematurely forces a failed offline load.
                complete = bool(required) and all(
                    (revision / name).is_file() for name in required)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                complete = False
            if not complete:
                break
        if complete:
            return True
    return False


def models_cached(model: str, target_language: str, nllb_size: str) -> bool:
    """Whether every model this configuration needs is already on disk."""
    if not (whisper_cache() / f"{model}.pt").is_file():
        return False
    if not has_snapshot(f"models--Systran--faster-whisper-{model}", "model.bin"):
        return False
    if not target_language:
        return True
    repo = f"models--facebook--nllb-200-distilled-{nllb_size}"
    return has_snapshot(repo, "*.bin") or has_snapshot(repo, "*.safetensors")


def configure_hub(model: str, target_language: str, nllb_size: str) -> bool:
    """Set the Hub environment. Returns True when starting fully offline."""
    # The Xet transfer backend fails behind many local proxies; the plain HTTPS
    # CDN path works and is only marginally slower.
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    if os.environ.get("HF_HUB_OFFLINE") == "1":
        return True
    if not models_cached(model, target_language, nllb_size):
        # Still online: fail fast instead of hanging on a dropped SYN.
        os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "5")
        return False
    os.environ["HF_HUB_OFFLINE"] = "1"
    # Nothing will be fetched, so a proxy can only cause harm: a malformed one
    # is enough to abort startup while building a client that is never used.
    for name in PROXY_VARS[:3]:
        os.environ.pop(name, None)
        os.environ.pop(name.upper(), None)
    return True


def keep_ct2_working_set(keep: int = CT2_POOL_KEEP_BYTES) -> str | None:
    """Make CTranslate2 keep the encoder's working memory between calls.

    CTranslate2 allocates through CUDA's stream-ordered pool and leaves its
    release threshold at zero, so every encoder call borrows its working set
    from the driver and hands it back at the next synchronisation. Anything
    else on the card can take it in between -- the refiner, the engine's own
    PyTorch cache, a video call -- and the next encode then fails with
    out-of-memory on every chunk for as long as the other allocation lives,
    each failure swallowed by the server. Measured on large-v3: with the
    threshold raised the process keeps its ~140 MB working set after the
    first encode (the warm-up); without it, it drops back to its weights after
    every call. The encoder always runs on a padded 30 s window, so the
    warm-up's working set is the largest it will ever need.

    Returns what went wrong, or None. Nothing here may stop the engine: the
    default behaviour is only less robust.
    """
    try:
        cuda = ctypes.CDLL("libcuda.so.1")
    except OSError:
        return None   # no NVIDIA driver, so nothing runs on CUDA
    try:
        cuda.cuDeviceGetDefaultMemPool.argtypes = [ctypes.POINTER(ctypes.c_void_p),
                                                   ctypes.c_int]
        cuda.cuMemPoolSetAttribute.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                               ctypes.c_void_p]
    except AttributeError:
        return "显卡驱动太旧，不支持显存池（不影响启动）"
    count = ctypes.c_int()
    if cuda.cuInit(0) != 0 or cuda.cuDeviceGetCount(ctypes.byref(count)) != 0:
        return "CUDA 驱动初始化失败，跳过显存池设置（不影响启动）"
    value = ctypes.c_uint64(keep)
    for ordinal in range(count.value):
        device = ctypes.c_int()
        pool = ctypes.c_void_p()
        if (cuda.cuDeviceGet(ctypes.byref(device), ordinal) != 0
                or cuda.cuDeviceGetDefaultMemPool(ctypes.byref(pool), device) != 0
                or cuda.cuMemPoolSetAttribute(pool, _CU_MEMPOOL_ATTR_RELEASE_THRESHOLD,
                                              ctypes.byref(value)) != 0):
            return (f"无法设置第 {ordinal} 块显卡的显存池（不影响启动，"
                    "只是识别更容易被别的程序挤掉显存）")
    return None


def is_out_of_memory(text: str) -> bool:
    """CTranslate2, PyTorch, cuBLAS and cuDNN each word it differently."""
    lowered = text.lower()
    return ("out of memory" in lowered or "alloc_failed" in lowered
            or "memoryallocation" in lowered)


def _log_gpu_memory(error: str) -> None:
    """Write down who holds the card, at the moment the ASR started failing.

    The investigation that led here had to reconstruct this after the fact
    and never could. Runs on its own thread: the failing backend thread
    should not wait for nvidia-smi, and CTranslate2 leaves a stale CUDA error
    on that thread which a CUDA call of ours could trip over.
    """
    lines = [f"识别后端开始出错: {error}"]
    torch = sys.modules.get("torch")
    try:
        if torch is not None and torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            mib = 1024 ** 2
            lines.append(f"显存空闲 {free // mib} MiB / 共 {total // mib} MiB，"
                         f"引擎 PyTorch 缓存 {torch.cuda.memory_reserved() // mib} MiB")
    except Exception as exc:
        lines.append(f"读取显存失败: {exc}")
    try:
        apps = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
             "--format=csv,noheader"], capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if apps:
            lines.append("占用显存的进程:\n" + apps)
    except (OSError, subprocess.SubprocessError):
        pass
    logger.error("\n".join(lines))


def _report_gpu_memory(error: str) -> None:
    threading.Thread(target=_log_gpu_memory, args=(error,),
                     name="gpu-memory-report", daemon=True).start()


class BackendFailures(logging.Handler):
    """Counts the exceptions WhisperLiveKit logs and then swallows.

    The backend catches whatever a chunk raises, logs it with its traceback
    and returns no words, and the server never tells the client: its status
    stays "active_transcription" while the ASR fails every chunk. The log is
    the only place a dead recogniser shows, so this sits on the log and
    counts. Only records with an exception attached count; the server's own
    "no output after N s" alarm has none, and it fires on a long opening
    silence rather than on a failure.
    """

    def __init__(self, on_streak: Callable[[str], None] | None = None,
                 streak_gap: float = _STREAK_GAP_SECONDS) -> None:
        super().__init__(level=logging.ERROR)
        self.failures = 0
        self.out_of_memory = 0
        self.last_error = ""
        #: Called with the error that starts each streak of failures.
        self.on_streak = on_streak if on_streak is not None else _report_gpu_memory
        self.streak_gap = streak_gap
        self._last_at = float("-inf")

    def emit(self, record: logging.LogRecord) -> None:
        error = record.exc_info[1] if record.exc_info else None
        if error is None:
            return
        text = f"{type(error).__name__}: {error}"[:300]
        self.failures += 1
        if is_out_of_memory(text):
            self.out_of_memory += 1
        self.last_error = text
        now = time.monotonic()
        if now - self._last_at > self.streak_gap:
            self.on_streak(text)
        self._last_at = now

    def snapshot(self) -> dict:
        self.acquire()
        try:
            return {"failures": self.failures, "out_of_memory": self.out_of_memory,
                    "last_error": self.last_error}
        finally:
            self.release()


class _SkipStatusPolls(logging.Filter):
    """A meeting polls the status route every two seconds; an access-log line
    per poll would bury everything else in the engine log."""

    def filter(self, record: logging.LogRecord) -> bool:
        return BACKEND_STATUS_PATH not in record.getMessage()


def report_backend_failures(app) -> BackendFailures:
    """Count backend exceptions and serve the counts on the status route."""
    from fastapi.responses import JSONResponse

    failures = BackendFailures()
    logging.getLogger("whisperlivekit").addHandler(failures)
    # A filter on the logger itself survives uvicorn's dictConfig, which only
    # replaces handlers.
    logging.getLogger("uvicorn.access").addFilter(_SkipStatusPolls())

    async def backend_status() -> JSONResponse:
        return JSONResponse(failures.snapshot())

    app.add_api_route(BACKEND_STATUS_PATH, backend_status, methods=["GET"])
    return failures


def parse_args(argv=None) -> argparse.Namespace:
    def env(key: str) -> str:
        return os.environ.get(key, DEFAULTS[key])

    parser = argparse.ArgumentParser(
        prog="python -m meeting_subtitles.serve",
        description="启动会议转录引擎（英文识别 + 中文翻译，全部本地运行）",
    )
    parser.add_argument("--model", default=env("MODEL"))
    parser.add_argument("--language", default=env("LANGUAGE"))
    parser.add_argument("--target-language", default=env("TARGET_LANGUAGE"))
    parser.add_argument("--nllb-size", default=env("NLLB_SIZE"))
    parser.add_argument("--port", default=env("PORT"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--pause-seconds", default=env("PAUSE_SECONDS"))
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将要使用的配置，不加载模型")
    parser.add_argument("rest", nargs=argparse.REMAINDER,
                        help="其余参数原样传给服务器")
    return parser.parse_args(argv)


def build_argv(args: argparse.Namespace) -> list[str]:
    # Upstream's default downloads a sample into /tmp even in Hub offline mode.
    # The deployment tool caches it persistently; without it, warm up on speech.
    warmup = paths.cache_dir() / "warmup.wav"
    argv = [
        "wlk",
        "--host", args.host,
        "--port", str(args.port),
        "--model", args.model,
        "--language", args.language,
        "--target-language", args.target_language,
        "--nllb-size", args.nllb_size,
        "--pause-segmentation-seconds", str(args.pause_seconds),
        "--pcm-input",
        "--warmup-file", str(warmup) if warmup.is_file() else "",
        "--log-level", args.log_level,
    ]
    extra = [a for a in args.rest if a != "--"]
    return argv + extra


def main(argv: list[str] | None = None) -> int:
    apply_env_file()
    configure_allocator()
    normalise_proxies()
    args = parse_args(argv)
    offline = configure_hub(args.model, args.target_language, args.nllb_size)

    print(f"模型: {args.model}   语言: {args.language} -> {args.target_language}"
          f"   NLLB: {args.nllb_size}")
    print(f"端口: {args.port}   分句停顿阈值: {args.pause_seconds}s")
    if offline:
        print("模型已在本地，离线启动（全程不联网），约 20-40 秒。")
    else:
        print("本地缺少部分模型，需要联网下载，首次可能要几分钟。")
        if not (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")):
            print("警告: 未检测到代理设置。若 huggingface.co 无法直连，下载会长时间卡住。")
            print(f"      可把代理写入 {paths.env_path()}，例如：")
            print("      https_proxy=http://127.0.0.1:7897")
    print(flush=True)

    server_argv = build_argv(args)
    if args.dry_run:
        print("将要执行:", " ".join(server_argv))
        print(f"HF_HUB_OFFLINE={os.environ.get('HF_HUB_OFFLINE', '(未设置)')}  "
              f"HF_HUB_DISABLE_XET={os.environ.get('HF_HUB_DISABLE_XET')}")
        proxies = {k: v for k, v in os.environ.items()
                   if k.lower() in PROXY_VARS and k.islower()}
        print(f"代理: {proxies or '(已清除)'}")
        return 0

    # First basicConfig call wins, which makes the server's own a no-op. The
    # times are for the next post-mortem: without them, working out when a
    # meeting broke meant re-running the VAD over its recording.
    logging.basicConfig(format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
                        datefmt="%H:%M:%S")
    problem = keep_ct2_working_set()
    if problem:
        print(f"警告: {problem}", flush=True)

    # Imported only now: the environment above has to be final before
    # huggingface_hub and transformers read it at *their* import time.
    sys.argv = server_argv
    from whisperlivekit import basic_server
    report_backend_failures(basic_server.app)
    basic_server.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
