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
import os
import sys
from pathlib import Path

from meeting_subtitles import paths

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
    explicit = os.environ.get("HUGGINGFACE_HUB_CACHE")
    if explicit:
        return Path(explicit)
    home = os.environ.get("HF_HOME")
    if home:
        return Path(home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def whisper_cache() -> Path:
    """Where openai-whisper puts its ``.pt`` checkpoints, on every platform."""
    default = Path.home() / ".cache"
    return Path(os.environ.get("XDG_CACHE_HOME", default)) / "whisper"


def has_snapshot(repo_dir: str, pattern: str) -> bool:
    """True when a Hub repo is cached with at least one file matching."""
    snapshots = hf_cache() / repo_dir / "snapshots"
    if not snapshots.is_dir():
        return False
    return any(revision.glob(pattern) for revision in snapshots.iterdir())


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
        "--log-level", args.log_level,
    ]
    extra = [a for a in args.rest if a != "--"]
    return argv + extra


def main(argv: list[str] | None = None) -> int:
    apply_env_file()
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

    # Imported only now: the environment above has to be final before
    # huggingface_hub and transformers read it at *their* import time.
    sys.argv = server_argv
    from whisperlivekit.basic_server import main as serve
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
