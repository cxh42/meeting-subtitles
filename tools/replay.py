"""Replay a recorded meeting through the engine and print what comes back.

This is the tool that separates "the server is wrong" from "our processing is
wrong". A live meeting cannot be run twice; a wav can, so a change to
segmentation, refinement or the stall watchdog can be judged against the same
audio it was judged against before.

    .venv/bin/python tools/replay.py ~/Meetings/<session>/audio.wav
    .venv/bin/python tools/replay.py <wav> --raw --seconds 60
    .venv/bin/python tools/replay.py <wav> --no-refine --speed 4

The audio is paced in real time by default. That matters: the server segments
on pauses, so a wav pushed through as fast as it will go arrives as one
undifferentiated block and segments nothing like the meeting it came from.
``--speed`` is for when only the text pipeline is under test.
"""

import argparse
import asyncio
import contextlib
import socket
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from meeting_subtitles import audio as audio_mod  # noqa: E402
from meeting_subtitles import domain as domains  # noqa: E402
from meeting_subtitles.client import Snapshot, TranscriptionClient  # noqa: E402
from meeting_subtitles.refine import (  # noqa: E402
    RefinementMerger,
    TranslationRefiner,
)
from meeting_subtitles.segment import resegment  # noqa: E402
from meeting_subtitles.watchdog import StallWatchdog  # noqa: E402

CHUNK_MS = 100


class ConnectionLog:
    """Record every outbound socket connect.

    Both model loads are supposed to be offline, and "offline" is a claim worth
    checking rather than believing: the failure mode is not an error but a wait
    on a hostname that does not resolve, which reads as a slow model.
    """

    def __init__(self) -> None:
        self.targets: set[str] = set()
        self._original = socket.socket.connect

    def __enter__(self) -> "ConnectionLog":
        log = self

        def traced(self, address):
            with contextlib.suppress(Exception):
                host = address[0] if isinstance(address, tuple) else str(address)
                if host not in ("127.0.0.1", "::1", "localhost"):
                    log.targets.add(str(host))
            return log._original(self, address)

        socket.socket.connect = traced
        return self

    def __exit__(self, *_exc) -> None:
        socket.socket.connect = self._original


def read_pcm(path: Path, seconds: float | None) -> bytes:
    """The wav as 16 kHz mono s16le, which is what the engine is fed live."""
    with wave.open(str(path), "rb") as stream:
        if stream.getsampwidth() != 2 or stream.getnchannels() != 1:
            raise SystemExit(
                f"错误: 需要 16 位单声道 wav，{path} 是 "
                f"{stream.getsampwidth() * 8} 位 {stream.getnchannels()} 声道。"
            )
        rate = stream.getframerate()
        if rate != audio_mod.SAMPLE_RATE:
            raise SystemExit(
                f"错误: 需要 {audio_mod.SAMPLE_RATE} Hz，{path} 是 {rate} Hz。"
            )
        frames = stream.getnframes()
        if seconds:
            frames = min(frames, int(seconds * rate))
        return stream.readframes(frames)


def describe(snapshot: Snapshot) -> str:
    lines = snapshot.speech_lines
    refined = sum(1 for line in lines if line.refined)
    return (f"{len(lines)} 段（{refined} 段已润色）"
            f" | 缓冲: {snapshot.buffer_transcription[:40]!r}")


async def run(args) -> int:
    pcm = read_pcm(Path(args.wav).expanduser(), args.seconds)
    duration = len(pcm) / audio_mod.BYTES_PER_SECOND
    print(f"音频: {args.wav}  {duration:.1f} 秒", flush=True)

    refiner = None
    if not args.no_refine:
        refiner = TranslationRefiner(
            glossary=domains.build_context(args.domain, ""),
            guidance=domains.get(args.domain).guidance,
        )
        refiner.start()
    merger = RefinementMerger(refiner)
    watchdog = StallWatchdog()

    snapshots = 0
    last: Snapshot | None = None
    stalls = 0

    def on_snapshot(snapshot: Snapshot) -> None:
        nonlocal snapshots, last
        snapshots += 1
        parts = [line.text for line in snapshot.lines]
        parts += [snapshot.buffer_transcription, snapshot.buffer_translation]
        watchdog.note_text("\x1f".join(parts))
        if not args.no_sentence_split:
            snapshot = resegment(snapshot)
        last = merger.process(snapshot)
        if args.raw:
            print(f"  [{snapshots:4d}] {describe(last)}", flush=True)

    client = TranscriptionClient(
        server=args.server, language="en", target_language="zh",
        context=domains.build_context(args.domain, "") or None,
    )

    chunk_bytes = audio_mod.BYTES_PER_SECOND * CHUNK_MS // 1000
    started = time.monotonic()

    async def source():
        nonlocal stalls
        for offset in range(0, len(pcm), chunk_bytes):
            chunk = pcm[offset:offset + chunk_bytes]
            watchdog.note_audio(chunk)
            if watchdog.take_report():
                stalls += 1
                print(f"  !! 看门狗: 已喂入 {watchdog.speech_seconds:.0f} 秒语音"
                      " 但没有新文本", flush=True)
            yield chunk
            if args.speed > 0:
                # Pace against the wall clock rather than sleeping a fixed
                # amount, so a slow snapshot callback does not make the replay
                # drift ever further behind real time.
                target = started + (offset + chunk_bytes) / (
                    audio_mod.BYTES_PER_SECOND * args.speed)
                delay = target - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)

    await client.run(source(), on_snapshot,
                     lambda status: print(f"  状态: {status}", flush=True))

    if refiner is not None:
        if last is not None:
            merger.flush_last(last)
        deadline = time.monotonic() + 30
        while not refiner.queue.empty() and time.monotonic() < deadline:
            await asyncio.sleep(0.3)
        await asyncio.sleep(0.5)
        if last is not None:
            last = merger.process(last)

    print(f"\n收到 {snapshots} 个快照，看门狗告警 {stalls} 次")
    if refiner is not None:
        if refiner.failed:
            print(f"润色未生效: {refiner.failed}")
        else:
            print(f"润色 {refiner.stats['count']} 句，"
                  f"平均 {refiner.average_seconds:.2f} 秒/句")

    if last is not None:
        print("\n--- 最终文本 ---")
        for line in last.speech_lines:
            mark = "润色" if line.refined else "流式"
            print(f"\n[{mark}] {line.text}\n       {line.translation}")

    if refiner is not None:
        refiner.stop(timeout=10)
    return 0 if snapshots else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把录好的 audio.wav 重放进引擎，检查识别与润色是否正常。")
    parser.add_argument("wav", help="会议目录里的 audio.wav")
    parser.add_argument("--server", default="ws://127.0.0.1:8000")
    parser.add_argument("--domain", default=domains.DEFAULT_DOMAIN,
                        choices=sorted(domains.DOMAINS))
    parser.add_argument("--seconds", type=float, default=None,
                        help="只重放前 N 秒")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="倍速；0 表示不限速（会打乱服务器的停顿分句）")
    parser.add_argument("--raw", action="store_true", help="打印每个快照")
    parser.add_argument("--no-refine", action="store_true")
    parser.add_argument("--no-sentence-split", action="store_true")
    args = parser.parse_args(argv)

    with ConnectionLog() as connections:
        code = asyncio.run(run(args))
    if connections.targets:
        print(f"\n注意: 本次运行连接了外部地址: {', '.join(sorted(connections.targets))}")
    else:
        print("\n本次运行没有连接任何外部地址（全程离线）。")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
