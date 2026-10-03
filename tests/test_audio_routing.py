"""Tests for choosing, and keeping, the sink the meeting plays to.

The pactl text below is real output from a PipeWire 1.6 desktop (trimmed to
the fields that matter, whitespace intact), taken with a stream that announces
itself the way Zoom 7.2 does. Each test corresponds to a way the other side of
a call drops out of the recording while the microphone carries on:

* Zoom plays to the speaker chosen in its own settings, not the default;
* "share computer sound" makes Zoom's combine sink the default while Zoom's
  own voices stay on the real device;
* ``-ac 3`` silently remixes the three-channel capture, burying the
  microphone in the other two channels.
"""

import array

from meeting_subtitles import audio

SINKS = (
    "Sink #60\n"
    "\tState: SUSPENDED\n"
    "\tName: alsa_output.pci-0000_00_1f.3.analog-stereo\n"
    "\tDescription: 内置音频 模拟立体声\n"
    "\tDriver: PipeWire\n"
    "\tOwner Module: 4294967295\n"
    "\tMute: no\n"
    "\tVolume: front-left: 22160 /  34% / -28.25 dB,   front-right: 22160 /  34% / -28.25 dB\n"
    "\t        balance 0.00\n"
    "\tMonitor Source: alsa_output.pci-0000_00_1f.3.analog-stereo.monitor\n"
    "\tProperties:\n"
    '\t\tdevice.class = "sound"\n'
    '\t\tnode.name = "alsa_output.pci-0000_00_1f.3.analog-stereo"\n'
    "\tPorts:\n"
    "\t\tanalog-output-lineout: 线路输出 (type: Line, priority: 9000, availability unknown)\n"
    "\tActive Port: analog-output-lineout\n"
    "\tFormats:\n"
    "\t\tpcm\n"
    "\n"
    "Sink #1019\n"
    "\tState: RUNNING\n"
    "\tName: zoomcombine\n"
    "\tDescription: zoom_combine_device\n"
    "\tDriver: PipeWire\n"
    "\tMonitor Source: zoomcombine.monitor\n"
    "\tProperties:\n"
    '\t\tnode.name = "zoomcombine"\n'
    "\tFormats:\n"
    "\t\tpcm\n"
)

SINK_INPUTS = (
    "Sink Input #1026\n"
    "\tDriver: PipeWire\n"
    "\tOwner Module: n/a\n"
    "\tClient: 1024\n"
    "\tSink: 1019\n"
    "\tFormat: pcm, format.sample_format = \"\\\"s16le\\\"\"  format.rate = \"48000\"\n"
    "\tCorked: no\n"
    "\tMute: no\n"
    "\tVolume: front-left: 65536 / 100% / 0.00 dB,   front-right: 65536 / 100% / 0.00 dB\n"
    "\t        balance 0.00\n"
    "\tProperties:\n"
    '\t\tapplication.name = "Firefox"\n'
    '\t\tmedia.name = "AudioStream"\n'
    '\t\tapplication.process.binary = "firefox"\n'
    "\n"
    "Sink Input #1027\n"
    "\tDriver: PipeWire\n"
    "\tOwner Module: n/a\n"
    "\tClient: 1025\n"
    "\tSink: 60\n"
    "\tCorked: no\n"
    "\tMute: no\n"
    "\tVolume: front-left: 19661 /  30% / -31.37 dB,   front-right: 19661 /  30% / -31.37 dB\n"
    "\t        balance 0.00\n"
    "\tProperties:\n"
    '\t\tapplication.name = "ZOOM VoiceEngine"\n'
    '\t\tmedia.name = "playStream"\n'
    '\t\tapplication.process.binary = "zoom"\n'
)

SOURCE_OUTPUTS = (
    "Source Output #1037\n"
    "\tDriver: PipeWire\n"
    "\tOwner Module: n/a\n"
    "\tClient: 1036\n"
    "\tSource: 1012\n"
    "\tCorked: no\n"
    "\tMute: no\n"
    "\tProperties:\n"
    '\t\tapplication.name = "meeting-subtitles-system"\n'
    '\t\tapplication.process.id = "92232"\n'
    '\t\ttarget.object = "mstest_hw"\n'
    '\t\tstream.capture.sink = "true"\n'
)


# ------------------------------------------------------------------- parsing

def test_sinks_keep_their_localised_description():
    sinks = audio.parse_sinks(SINKS)
    assert [sink.name for sink in sinks] == [
        "alsa_output.pci-0000_00_1f.3.analog-stereo", "zoomcombine"]
    assert sinks[0].description == "内置音频 模拟立体声"
    assert sinks[0].monitor == "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor"


def test_port_lines_are_not_mistaken_for_properties():
    """Two-tab lines outside ``Properties:`` (ports, formats) are skipped."""
    sink = audio.parse_sinks(SINKS)[0]
    assert sink.index == 60
    assert not sink.is_zoom_share


def test_zoom_share_sinks_are_recognised():
    combine = audio.parse_sinks(SINKS)[1]
    assert combine.is_zoom_share


def test_playback_streams_read_sink_volume_and_identity():
    streams = audio.parse_playback_streams(SINK_INPUTS)
    firefox, zoom = streams
    assert (firefox.sink, firefox.volume, firefox.is_zoom) == (1019, 100, False)
    assert (zoom.index, zoom.sink, zoom.volume, zoom.is_zoom) == (1027, 60, 30, True)
    assert not zoom.corked and not zoom.muted


def test_capture_stream_is_found_by_its_name():
    (stream,) = audio.parse_capture_streams(SOURCE_OUTPUTS)
    assert (stream.index, stream.source, stream.pid) == (1037, 1012, 92232)
    assert stream.application == audio.SYSTEM_STREAM_NAME


# ------------------------------------------------------------------ choosing

def _sinks():
    return audio.parse_sinks(SINKS)


def test_zoom_stream_decides_even_when_the_default_is_elsewhere():
    """Zoom plays to the speaker in its own settings, not the default."""
    sink, reason = audio.choose_meeting_sink(
        _sinks(), audio.parse_playback_streams(SINK_INPUTS), default="zoomcombine")
    assert sink.index == 60
    assert "Zoom" in reason


def test_share_sound_default_is_skipped_without_a_zoom_stream():
    """The combine sink's monitor has everything except the meeting."""
    firefox_only = [s for s in audio.parse_playback_streams(SINK_INPUTS) if not s.is_zoom]
    sink, reason = audio.choose_meeting_sink(_sinks(), firefox_only, default="zoomcombine")
    assert sink.name.startswith("alsa_output")
    assert "共享" in reason


def test_without_zoom_the_default_sink_is_used():
    sink, _ = audio.choose_meeting_sink(
        _sinks(), [], default="alsa_output.pci-0000_00_1f.3.analog-stereo")
    assert sink.index == 60


def test_a_running_zoom_stream_beats_a_corked_one():
    corked = audio.PlaybackStream(1, 1019, "ZOOM VoiceEngine", "zoom", True, False, 100)
    running = audio.PlaybackStream(2, 60, "ZOOM VoiceEngine", "zoom", False, False, 100)
    sink, _ = audio.choose_meeting_sink(_sinks(), [corked, running], default="zoomcombine")
    assert sink.index == 60


def test_no_sinks_means_no_choice():
    assert audio.choose_meeting_sink([], [], default="") is None


# ----------------------------------------------------------------- capturing

def test_both_inputs_give_three_channels_without_a_remix():
    command = audio.build_ffmpeg_command("sink.monitor", "mic")
    assert audio.output_channels("sink.monitor", "mic") == 3
    assert "-ac" not in command[command.index("-map"):command.index("pipe:1")]
    assert "join=inputs=3:channel_layout=3.0" in command[command.index("-filter_complex") + 1]


def test_one_input_stays_mono():
    command = audio.build_ffmpeg_command("sink.monitor", None)
    assert audio.output_channels("sink.monitor", None) == 1
    assert command[command.index("-map") + 2:command.index("-map") + 4] == ["-ac", "1"]


def test_capture_streams_are_named_for_the_router():
    command = audio.build_ffmpeg_command("sink.monitor", "mic")
    names = [command[i + 1] for i, arg in enumerate(command) if arg == "-name"]
    assert names == [audio.SYSTEM_STREAM_NAME, audio.MIC_STREAM_NAME]


def test_limiter_keeps_its_headroom():
    """alimiter's auto-level would scale the output back up to full scale."""
    command = audio.build_ffmpeg_command("sink.monitor", "mic")
    graph = command[command.index("-filter_complex") + 1]
    assert "alimiter=limit=0.95:level=disabled" in graph


def test_wav_records_the_mix_only():
    command = audio.build_ffmpeg_command("sink.monitor", "mic", wav_path="x.wav")
    wav = command.index("[wav]")
    assert command[wav + 1:wav + 3] == ["-ac", "1"]


def test_split_channels_deinterleaves_mix_system_mic():
    frames = array.array("h", [10, 20, 30, 11, 21, 31])
    chunk = audio.split_channels(frames.tobytes(), 3)
    assert array.array("h", chunk.pcm).tolist() == [10, 11]
    assert array.array("h", chunk.system).tolist() == [20, 21]
    assert array.array("h", chunk.mic).tolist() == [30, 31]


def test_split_channels_passes_mono_through():
    data = array.array("h", [1, 2, 3]).tobytes()
    chunk = audio.split_channels(data, 1)
    assert chunk.pcm == data and chunk.system is None and chunk.mic is None
