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
from meeting_subtitles.client import (
    EngineTooOld,
    Snapshot,
    TranscriptionClient,
    engine_status_url,
    fetch_engine_status,
)
from meeting_subtitles.envfix import normalize_proxy_env
from meeting_subtitles.overlay import SubtitleOverlay
from meeting_subtitles.recorder import TranscriptRecorder
from meeting_subtitles.refine import DEFAULT_MODEL, RefinementMerger, TranslationRefiner
from meeting_subtitles.segment import resegment
from meeting_subtitles.tkfix import ensure_cjk_tk
from meeting_subtitles.watchdog import (
    BackendWatch,
    StallWatchdog,
    SystemAudioWatch,
    transcript_signature,
)

logger = logging.getLogger("meeting")

#: How often the engine's failure counters are read. A failing backend raises
#: many times a second, so two seconds is plenty to see a streak.
ENGINE_POLL_SECONDS = 2.0

UNHEARD_NOTICE = (
    "这台电脑没有在播放其他参会者的声音，字幕只能从麦克风里听到他们。\n"
    "在这台电脑上加入会议音频、打开扬声器（耳机或音箱都行），并把 Zoom 的音量调高；"
    "如果对方一直没说话，可以忽略这条提示。"
)


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
    """Pick the monitor/mic source names, honouring the --no-* switches.

    Returns ``(monitor, mic, route)``; ``route`` says why that monitor was
    chosen, and is None when the user named it (or turned it off), in which
    case nothing may move it later either.
    """
    monitor = None
    mic = None
    route = None
    if not args.no_system:
        if args.monitor_source:
            monitor = args.monitor_source
        else:
            monitor, route = audio.meeting_monitor_source()
    if not args.no_mic:
        mic = args.mic_source or audio.default_mic_source()
        if monitor and mic == monitor:
            # Happens when the default *source* is itself a monitor; capturing it
            # twice would just double the same signal.
            logger.warning("麦克风与系统声音是同一个源，已跳过麦克风。")
            mic = None
    if not monitor and not mic:
        raise ValueError("--no-mic 和 --no-system 不能同时使用。")
    return monitor, mic, route


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
                 merger: RefinementMerger | None = None,
                 follow_route: bool = False) -> None:
        self.args = args
        self.recorder = recorder
        self.merger = merger
        self.monitor = monitor
        self.mic = mic
        self.overlay = overlay
        #: Move the system-audio capture to wherever the meeting plays; off
        #: when the user named the monitor themselves.
        self.follow_route = follow_route
        self.stop_event: asyncio.Event | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.error: BaseException | None = None
        self._last_snapshot: Snapshot | None = None
        self.watchdog = StallWatchdog()
        self.system_watch = SystemAudioWatch()
        self._route_clear: asyncio.TimerHandle | None = None

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

    @property
    def refiner(self) -> TranslationRefiner | None:
        return self.merger.refiner if self.merger is not None else None

    def _on_snapshot(self, snapshot: Snapshot) -> None:
        was_stalled = self.watchdog.reported
        self.watchdog.note_text(transcript_signature(snapshot))
        if was_stalled and not self.watchdog.reported:
            # Text is moving again; leaving the warning up would be a lie.
            logger.info("识别已恢复。")
            if self.overlay:
                self.overlay.set_notice("", key="stall")
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

    def _report_stall(self) -> None:
        """Say so, loudly, rather than letting the meeting look fine.

        The fallback for an engine that stops without raising, or one too old
        to report its failures; :meth:`_on_engine_failing` is usually first.
        It does not unload the refiner: speech-level audio that yields no text
        can also be music, and the polish should not go for that.
        """
        message = ("识别停止了：还在收音，但 "
                   f"{int(self.watchdog.stall_seconds)} 秒的语音没有产出文字。\n"
                   "多半是显存被占满了：用 nvidia-smi 看是谁占着并关掉它，识别会自己恢复；"
                   "仍不恢复就结束会议，在启动器里重启引擎。")
        logger.error("%s", message.replace("\n", " "))
        if self.overlay:
            self.overlay.set_notice(message, key="stall")

    def _on_engine_failing(self, watch: BackendWatch) -> None:
        """The engine is failing every chunk: free what we can, and say why.

        The recogniser recovers by itself on the next chunk once memory is
        available, so when the failure is out-of-memory the refiner's model
        goes -- the polish is worth less than the transcript.
        """
        refiner = self.refiner
        if watch.out_of_memory:
            if refiner is not None and refiner.release("识别引擎显存不足，润色模型让出了显存"):
                message = ("识别引擎显存不足，已自动关闭整句润色，把显存让给识别，字幕应在几秒内恢复。\n"
                           "以后显存紧张时，开会前在启动器里关掉「整句润色」。")
            else:
                message = ("识别引擎显存不足，字幕暂停了。\n"
                           "用 nvidia-smi 看是谁占着显存并关掉它，字幕会自动恢复，不用重启。")
        else:
            message = (f"识别引擎出错，字幕暂停了：{watch.last_error[:120]}\n"
                       f"运行 meeting-subtitles-doctor 检查；引擎日志在 {paths.server_log()}")
        logger.error("%s", message.replace("\n", " "))
        if self.overlay:
            self.overlay.set_notice(message, key="engine")

    def _on_engine_recovered(self) -> None:
        logger.warning("识别引擎已恢复。")
        if self.overlay:
            self.overlay.set_notice("", key="engine")
            refiner = self.refiner
            if refiner is not None and refiner.released:
                self.overlay.set_notice("整句润色已在会议中途关闭，显存让给了识别。", key="refine")

    async def _watch_engine(self) -> None:
        """Read the engine's failure counters; see :class:`BackendWatch`."""
        url = engine_status_url(self.args.server)
        watch = BackendWatch()
        while True:
            try:
                status = await asyncio.to_thread(fetch_engine_status, url)
            except EngineTooOld:
                logger.warning("引擎是旧版本，不报告识别故障；在启动器里重启一次引擎即可启用。")
                return
            if status is not None:
                try:
                    edge = watch.update(int(status.get("failures", 0)),
                                        int(status.get("out_of_memory", 0)),
                                        str(status.get("last_error", "")))
                except (TypeError, ValueError):
                    edge = None
                if edge == "failing":
                    self._on_engine_failing(watch)
                elif edge == "recovered":
                    self._on_engine_recovered()
            await asyncio.sleep(ENGINE_POLL_SECONDS)

    def _note_branches(self, system: bytes, mic: bytes) -> None:
        edge = self.system_watch.note(system, mic)
        if edge == "unheard":
            logger.warning("%s", UNHEARD_NOTICE.replace("\n", " "))
            if self.overlay:
                self.overlay.set_notice(UNHEARD_NOTICE, key="unheard")
        elif edge == "heard":
            logger.warning("系统声音里又有声音了。")
            if self.overlay:
                self.overlay.set_notice("", key="unheard")

    def _route_changed(self, sink: audio.Sink, reason: str) -> None:
        """Called from the router's thread when it moves the capture."""
        loop = self.loop
        if loop is None or loop.is_closed() or not self.overlay:
            return
        try:
            loop.call_soon_threadsafe(self._show_route, sink, reason)
        except RuntimeError:
            pass    # the loop closed in between; the meeting is over anyway

    def _show_route(self, sink: audio.Sink, reason: str) -> None:
        overlay = self.overlay
        if overlay is None:
            return
        overlay.set_notice(f"系统声音改为录「{sink.description}」：{reason}", key="route")
        if self._route_clear is not None:
            self._route_clear.cancel()
        self._route_clear = asyncio.get_running_loop().call_later(
            10, lambda: overlay.set_notice("", key="route"))

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
                            chunk = next_task.result()
                        except StopAsyncIteration:
                            return
                        self.watchdog.note_audio(chunk.pcm)
                        if self.watchdog.take_report():
                            self._report_stall()
                        if chunk.system is not None and chunk.mic is not None:
                            self._note_branches(chunk.system, chunk.mic)
                        yield chunk.pcm
                finally:
                    stop_task.cancel()

            helpers = [asyncio.create_task(self._watch_engine())]
            if self.follow_route and capture.process is not None:
                router = audio.MeetingAudioRouter(capture.process.pid, self._route_changed)
                helpers.append(asyncio.create_task(router.run()))
            try:
                await client.run(pcm_source(), self._on_snapshot, self._on_status)
            finally:
                for task in helpers:
                    task.cancel()
                await asyncio.gather(*helpers, return_exceptions=True)

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
            monitor, route = audio.meeting_monitor_source()
            print(f"\n系统声音: {monitor}（{route}）")
            print(f"默认麦克风: {audio.default_mic_source()}")
        except RuntimeError as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 2
        sinks = {sink.index: sink for sink in audio.list_sinks()}
        for stream in audio.playback_streams():
            if stream.is_zoom:
                sink = sinks.get(stream.sink)
                where = sink.description if sink else f"#{stream.sink}"
                state = "静音" if stream.muted else f"音量 {stream.volume}%"
                print(f"Zoom 正在播放到: {where}（{state}）")
        return 0

    parsed = urlparse(args.server)
    if parsed.scheme not in ("ws", "wss"):
        print(f"错误: --server 需要 ws:// 或 wss:// 地址，收到 {args.server!r}", file=sys.stderr)
        return 2

    try:
        monitor, mic, route = resolve_sources(args)
    except (RuntimeError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    print(f"系统声音: {monitor or '(关闭)'}" + (f"（{route}）" if route else ""))
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
    runner = MeetingRunner(args, recorder, monitor, mic, overlay=None, merger=merger,
                           follow_route=route is not None)

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

    if refiner is not None and overlay is not None:
        def announce_refiner() -> None:
            """Tell the user when the polish is not coming.

            The load runs in the background so the meeting can start at once,
            which also means its failure lands in a log nobody is watching --
            the launcher starts this process with no terminal attached. Until
            this existed, "显存不足" and "model still loading" looked identical
            from the only place the user is looking.
            """
            refiner.ready.wait()
            if refiner.failed:
                overlay.set_notice(f"整句润色未启用，只显示流式译文。\n{refiner.failed}",
                                   key="refine")

        threading.Thread(target=announce_refiner, name="refiner-status",
                         daemon=True).start()

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
        # A released refiner has no worker left to drain its queue.
        while (not refiner.queue.empty() and not refiner.released
               and time.monotonic() < deadline):
            time.sleep(0.3)
        time.sleep(0.5)
        if runner._last_snapshot is not None:
            recorder.update(merger.process(runner._last_snapshot))
        if refiner.stats["count"]:
            print(f"整句润色: {refiner.stats['count']} 句，"
                  f"平均 {refiner.average_seconds:.2f} 秒/句")
        elif refiner.failed:
            print(f"整句润色未生效: {refiner.failed}")
        if refiner.released:
            print(f"整句润色在会议中途关闭: {refiner.released}")
        refiner.stop(timeout=5)

    watch = runner.system_watch
    if watch.total_mic_seconds or watch.total_system_seconds:
        # One line in the journal that answers "was the other side even
        # reaching us?" after the fact, without digging through the wav.
        print(f"有声音的时长：电脑播放 {watch.total_system_seconds:.0f} 秒，"
              f"麦克风 {watch.total_mic_seconds:.0f} 秒")
    print(recorder.close())
    if runner.error is not None:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
