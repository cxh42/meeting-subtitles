"""Entry point: capture the meeting, subtitle it, and write the transcript.

    python -m meeting_subtitles                 # subtitles + transcript, default devices
    python -m meeting_subtitles --list-devices  # show the audio sources it can use
    python -m meeting_subtitles --no-mic        # transcribe only what the others say
    python -m meeting_subtitles --no-overlay    # headless: transcript files only

Requires the transcription engine to be running (``python -m meeting_subtitles.serve``,
which the GUI launcher starts for you).
"""

import argparse
import asyncio
import logging
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from meeting_subtitles import audio, paths
from meeting_subtitles import domain as domains
from meeting_subtitles.client import Snapshot, TranscriptionClient
from meeting_subtitles.envfix import normalize_proxy_env
from meeting_subtitles.overlay import SubtitleOverlay
from meeting_subtitles.recorder import TranscriptRecorder
from meeting_subtitles.refine import DEFAULT_MODEL, RefinementMerger, TranslationRefiner
from meeting_subtitles.segment import resegment
from meeting_subtitles.tkfix import ensure_cjk_tk

logger = logging.getLogger("meeting")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="meeting-subtitles-run",
        description="实时中英对照会议字幕与转录记录",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--server", default="ws://127.0.0.1:8000",
                        help="WhisperLiveKit WebSocket 地址")
    parser.add_argument("--language", default="en", help="源语言 (默认 en)")
    parser.add_argument("--target-language", default="zh", help="翻译目标语言 (默认 zh)")
    parser.add_argument("--context", default=None,
                        help="术语/人名提示，提高专有名词识别率，例如 'Kubernetes, Anirudh, SLO'")
    parser.add_argument("--token", default=None, help="服务器 --api-token 对应的令牌")

    parser.add_argument("--monitor-source", default=None,
                        help=f"系统声音采集源，{audio.SOURCE_HINT} (默认: 当前输出设备)")
    parser.add_argument("--mic-source", default=None,
                        help=f"麦克风采集源，{audio.SOURCE_HINT}")
    parser.add_argument("--no-mic", action="store_true", help="不采集麦克风，只转录对方")
    parser.add_argument("--no-system", action="store_true", help="不采集系统声音，只转录麦克风")
    parser.add_argument("--mic-gain", type=float, default=1.0, help="麦克风增益")
    parser.add_argument("--system-gain", type=float, default=1.0, help="系统声音增益")

    parser.add_argument("--output-dir", default=str(paths.default_output_dir()),
                        help=f"转录保存目录 (默认 {paths.default_output_dir()})")
    parser.add_argument("--session-dir", default=None,
                        help="直接指定本场会议的目录，跳过自动命名 (供 GUI 启动器使用)")
    parser.add_argument("--title", default=None, help="本次会议标题")
    parser.add_argument("--no-audio-file", action="store_true",
                        help="不保存 wav 录音 (默认保存，便于事后重新转录)")
    parser.add_argument("--formats", default="md",
                        help="转录文件格式，逗号分隔: md,srt,json (默认只写 md)")

    parser.add_argument("--domain", default=domains.DEFAULT_DOMAIN,
                        choices=sorted(domains.DOMAINS),
                        help="会议领域，决定内置术语表和翻译风格 (默认 cs-ai)")
    parser.add_argument("--no-sentence-split", action="store_true",
                        help="不按标点重新断句，沿用服务器基于停顿的分段")

    parser.add_argument("--no-refine", action="store_true",
                        help="关闭整句润色，只用服务器的流式译文（省约 8GB 显存）")
    parser.add_argument("--refine-model", default=DEFAULT_MODEL,
                        help="整句润色使用的本地模型")

    parser.add_argument("--no-overlay", action="store_true", help="不显示悬浮字幕窗口")
    parser.add_argument("--font-size", type=int, default=19, help="字幕字号")
    parser.add_argument("--width-ratio", type=float, default=0.78, help="字幕窗宽度占屏幕比例")
    parser.add_argument("--opacity", type=float, default=0.94, help="字幕窗不透明度 0-1")
    parser.add_argument("--theme", choices=("light", "dark"), default=None,
                        help="界面外观：light 浅色、dark 深色（默认沿用上次选择）")

    parser.add_argument("--list-devices", action="store_true", help="列出可用音频源后退出")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args(argv)


def resolve_sources(args: argparse.Namespace):
    """Pick the monitor/mic source names, honouring the --no-* switches."""
    monitor = None
    mic = None
    if not args.no_system:
        monitor = args.monitor_source or audio.default_monitor_source()
    if not args.no_mic:
        mic = args.mic_source or audio.default_mic_source()
        if monitor and mic == monitor:
            # Happens when the default *source* is itself a monitor; capturing it
            # twice would just double the same signal.
            logger.warning("麦克风与系统声音是同一个源，已跳过麦克风。")
            mic = None
    if not monitor and not mic:
        raise ValueError("--no-mic 和 --no-system 不能同时使用。")
    return monitor, mic


def make_session_dir(root: str, title: str | None) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    slug = (title or "meeting").strip().replace("/", "-").replace(" ", "_")
    directory = Path(root).expanduser() / f"{stamp}_{slug}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


class MeetingRunner:
    """Owns the asyncio side: ffmpeg capture -> WebSocket -> consumers."""

    def __init__(self, args: argparse.Namespace, recorder: TranscriptRecorder,
                 monitor: str | None, mic: str | None,
                 overlay: SubtitleOverlay | None,
                 merger: RefinementMerger | None = None) -> None:
        self.args = args
        self.recorder = recorder
        self.merger = merger
        self.monitor = monitor
        self.mic = mic
        self.overlay = overlay
        self.stop_event: asyncio.Event | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.error: BaseException | None = None
        self._last_snapshot: Snapshot | None = None

    def request_stop(self) -> None:
        """Safe to call from any thread (the Tk thread does).

        The worker may already have finished -- the audio source ended, or the
        connection dropped -- which closes the loop. Closing the window or
        pressing Ctrl-C after that must be a no-op, not a crash.
        """
        loop, event = self.loop, self.stop_event
        if loop is None or event is None or loop.is_closed() or event.is_set():
            return
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            # The loop closed between the check above and this call.
            pass

    def _on_snapshot(self, snapshot: Snapshot) -> None:
        if not self.args.no_sentence_split:
            # Split first: refinement and the transcript should both work in
            # sentences, not in whatever block the speaker's pauses produced.
            snapshot = resegment(snapshot)
        if self.merger is not None:
            snapshot = self.merger.process(snapshot)
            self._last_snapshot = snapshot
        self.recorder.update(snapshot)
        if self.overlay:
            self.overlay.push(snapshot)

    def _on_status(self, status: str) -> None:
        logger.info("状态: %s", status)
        if self.overlay:
            self.overlay.set_status(status)

    async def _run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()

        monitor, mic = self.monitor, self.mic
        wav_path = None if self.args.no_audio_file else self.recorder.audio_path
        capture = audio.AudioCapture(
            monitor=monitor, mic=mic, wav_path=wav_path,
            mic_gain=self.args.mic_gain, monitor_gain=self.args.system_gain,
        )
        client = TranscriptionClient(
            server=self.args.server,
            language=self.args.language,
            target_language=self.args.target_language,
            context=self.args.context,
            token=self.args.token,
        )

        async with capture:
            async def pcm_source():
                """Yield PCM until ffmpeg stops or a stop is requested."""
                chunks = capture.chunks()
                stop_task = asyncio.ensure_future(self.stop_event.wait())
                try:
                    while True:
                        next_task = asyncio.ensure_future(chunks.__anext__())
                        done, _ = await asyncio.wait(
                            {next_task, stop_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if stop_task in done:
                            next_task.cancel()
                            logger.info("收到停止信号，正在结束录制…")
                            return
                        try:
                            yield next_task.result()
                        except StopAsyncIteration:
                            return
                finally:
                    stop_task.cancel()

            await client.run(pcm_source(), self._on_snapshot, self._on_status)

    def run_forever(self) -> None:
        try:
            asyncio.run(self._run())
        except BaseException as exc:  # surfaced by the caller after Tk exits
            self.error = exc
            logger.error("采集/转录中断: %s", exc)
        finally:
            if self.overlay:
                self.overlay.set_status("finished")
                self.overlay.request_close()


def main(argv=None) -> int:
    normalize_proxy_env()
    args = parse_args(argv)
    if not args.no_overlay and not args.list_devices:
        # May re-exec; nothing with side effects may run before this.
        ensure_cjk_tk(module="meeting_subtitles")
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.list_devices:
        print(f"音频后端: {audio.BACKEND}\n")
        print("可用音频源:\n")
        for source in audio.list_sources():
            print(f"  {source}")
        try:
            print(f"\n默认系统声音: {audio.default_monitor_source()}")
            print(f"默认麦克风  : {audio.default_mic_source()}")
        except RuntimeError as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 2
        return 0

    parsed = urlparse(args.server)
    if parsed.scheme not in ("ws", "wss"):
        print(f"错误: --server 需要 ws:// 或 wss:// 地址，收到 {args.server!r}", file=sys.stderr)
        return 2

    try:
        monitor, mic = resolve_sources(args)
    except (RuntimeError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    print(f"系统声音: {monitor or '(关闭)'}")
    print(f"麦克风  : {mic or '(关闭)'}")

    if args.session_dir:
        session_dir = Path(args.session_dir).expanduser()
        session_dir.mkdir(parents=True, exist_ok=True)
    else:
        session_dir = make_session_dir(args.output_dir, args.title)
    formats = tuple(f.strip() for f in args.formats.split(",") if f.strip())
    try:
        recorder = TranscriptRecorder(
            session_dir, title=args.title or "会议记录", formats=formats,
        )
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    print(f"转录保存目录: {session_dir}")

    domain = domains.get(args.domain)
    asr_context = domains.build_context(args.domain, args.context or "")
    args.context = asr_context or None
    print(f"领域: {domain.label}（内置 {len(domain.terms)} 个术语）")

    refiner: TranslationRefiner | None = None
    if not args.no_refine:
        refiner = TranslationRefiner(
            model_id=args.refine_model,
            glossary=(args.context or ""),
            guidance=domain.guidance,
        )
        refiner.start()   # loads in the background; the meeting starts now
        print("整句润色: 已启用（模型后台加载中，前几句仍用流式译文）")
    else:
        print("整句润色: 已关闭")
    merger = RefinementMerger(refiner)

    overlay: SubtitleOverlay | None = None
    runner = MeetingRunner(args, recorder, monitor, mic, overlay=None, merger=merger)

    if not args.no_overlay:
        try:
            overlay = SubtitleOverlay(
                theme=args.theme,
                on_close=runner.request_stop,
                font_size=args.font_size,
                width_ratio=args.width_ratio,
                opacity=args.opacity,
            )
        except Exception as exc:
            logger.warning("无法创建字幕窗口 (%s)，退回无窗口模式。", exc)
            overlay = None
    runner.overlay = overlay

    def handle_signal(_signum, _frame):
        logger.info("收到中断信号，正在保存转录…")
        runner.request_stop()
        if overlay:
            overlay.request_close()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    worker = threading.Thread(target=runner.run_forever, name="meeting-io", daemon=True)
    worker.start()

    if overlay:
        overlay.run()
        runner.request_stop()
        print("正在等待最后几句转录完成并保存…")
    else:
        # Headless: keep the main thread alive until the worker finishes so the
        # signal handlers above still run.
        while worker.is_alive():
            worker.join(timeout=0.5)

    worker.join(timeout=35)

    if refiner is not None:
        # The last sentence never got superseded, so it was never queued.
        if runner._last_snapshot is not None:
            merger.flush_last(runner._last_snapshot)
        deadline = time.monotonic() + 20
        while not refiner.queue.empty() and time.monotonic() < deadline:
            time.sleep(0.3)
        time.sleep(0.5)
        if runner._last_snapshot is not None:
            recorder.update(merger.process(runner._last_snapshot))
        if refiner.stats["count"]:
            print(f"整句润色: {refiner.stats['count']} 句，"
                  f"平均 {refiner.average_seconds:.2f} 秒/句")
        elif refiner.failed:
            print(f"整句润色未生效: {refiner.failed}")
        refiner.stop(timeout=5)

    print(recorder.close())
    if runner.error is not None:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
