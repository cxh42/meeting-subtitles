"""Meeting mode: live bilingual subtitles + transcript recording for Zoom/Meet/Teams.

Built on top of WhisperLiveKit's WebSocket server. The pieces are:

- ``audio``    -- capture what the machine is playing (the other participants)
                  and the microphone through PulseAudio/PipeWire, mixed by
                  ffmpeg into one 16 kHz mono PCM stream.
- ``client``   -- stream that PCM to ``ws://.../asr`` and yield the server's
                  ``FrontData`` updates.
- ``recorder`` -- persist every update as Markdown / SRT / JSON.
- ``overlay``  -- always-on-top subtitle window (English above, Chinese below).
- ``serve``    -- start the transcription engine with the right environment.
- ``launcher`` -- the GUI that ties the above together.
"""

__all__ = ["audio", "client", "recorder", "overlay", "serve", "launcher"]
