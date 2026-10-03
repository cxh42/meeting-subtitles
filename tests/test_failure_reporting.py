"""Tests for the engine telling the meeting that recognition has died.

The server catches every backend exception per chunk, logs it and carries on,
so an out-of-memory ASR used to look like a quiet meeting. The error strings
below are copied from the engine log of the meeting where that happened.
"""

import http.server
import json
import logging
import threading

import pytest

from meeting_subtitles import serve
from meeting_subtitles.client import (
    EngineTooOld,
    engine_status_url,
    fetch_engine_status,
)
from meeting_subtitles.watchdog import BackendWatch

CT2_OOM = "CUDA failed with error out of memory"
CT2_AFTERMATH = "parallel_for failed: cudaErrorInvalidDevice: invalid device ordinal"


def _logger_with(handler: logging.Handler) -> logging.Logger:
    logger = logging.getLogger(f"whisperlivekit.test.{id(handler)}")
    logger.propagate = False
    logger.addHandler(handler)
    return logger


def _fail(logger: logging.Logger, message: str) -> None:
    try:
        raise RuntimeError(message)
    except RuntimeError as exc:
        logger.exception("SimulStreaming processing error: %s", exc)


# ------------------------------------------------------------------- engine

def test_failures_are_counted_from_logged_exceptions():
    streaks = []
    failures = serve.BackendFailures(on_streak=streaks.append)
    logger = _logger_with(failures)
    for _ in range(3):
        _fail(logger, CT2_OOM)
        _fail(logger, CT2_AFTERMATH)
    snapshot = failures.snapshot()
    assert snapshot["failures"] == 6
    assert snapshot["out_of_memory"] == 3
    assert snapshot["last_error"] == f"RuntimeError: {CT2_AFTERMATH}"
    assert streaks == [f"RuntimeError: {CT2_OOM}"], "one report per streak"


def test_errors_without_an_exception_do_not_count():
    """The server's own 'no output after 41 s' alarm fires on long silences."""
    failures = serve.BackendFailures(on_streak=lambda _error: None)
    logger = _logger_with(failures)
    logger.error("ASR backend produced no output after 41 s of audio.")
    logger.warning("Exception in results_formatter")
    assert failures.snapshot()["failures"] == 0


def test_a_new_streak_is_reported_after_a_quiet_gap():
    streaks = []
    failures = serve.BackendFailures(on_streak=streaks.append, streak_gap=0.0)
    logger = _logger_with(failures)
    _fail(logger, CT2_OOM)
    _fail(logger, CT2_OOM)
    assert len(streaks) == 2


@pytest.mark.parametrize("text, expected", [
    (f"RuntimeError: {CT2_OOM}", True),
    ("OutOfMemoryError: CUDA out of memory. Tried to allocate 20.00 MiB", True),
    ("RuntimeError: CUDA error: CUBLAS_STATUS_ALLOC_FAILED when calling cublasCreate", True),
    (f"RuntimeError: {CT2_AFTERMATH}", False),
    ("RuntimeError: Library libcublas.so.12 is not found or cannot be loaded", False),
])
def test_out_of_memory_is_recognised_in_every_library_wording(text, expected):
    assert serve.is_out_of_memory(text) is expected


# ------------------------------------------------------------------ meeting

def test_first_reading_is_only_a_baseline():
    """The counters outlive sessions; yesterday's failures are not today's."""
    watch = BackendWatch()
    assert watch.update(1253, 725) is None
    assert not watch.failing


def test_a_burst_of_failures_is_an_edge_once():
    watch = BackendWatch(burst=3)
    watch.update(0, 0)
    assert watch.update(40, 20, f"RuntimeError: {CT2_OOM}") == "failing"
    assert watch.out_of_memory
    assert watch.update(80, 40) is None, "still failing, already reported"


def test_one_stale_error_is_not_a_failure():
    """CTranslate2 reports one stale error on the first call after an OOM."""
    watch = BackendWatch(burst=3)
    watch.update(1253, 725)
    assert watch.update(1254, 725) is None
    assert not watch.failing


def test_recovery_takes_two_quiet_readings():
    watch = BackendWatch(burst=3, quiet_polls=2)
    watch.update(0, 0)
    watch.update(30, 15)
    assert watch.update(30, 15) is None
    assert watch.update(30, 15) == "recovered"
    assert not watch.failing and not watch.out_of_memory


def test_failures_other_than_memory_are_not_called_memory():
    watch = BackendWatch()
    watch.update(0, 0)
    assert watch.update(10, 0, "RuntimeError: Library libcublas.so.12 is not found") == "failing"
    assert not watch.out_of_memory


def test_a_restarted_engine_resets_the_baseline():
    watch = BackendWatch()
    watch.update(500, 200)
    assert watch.update(0, 0) is None
    assert watch.update(1, 0) is None


def test_status_url_follows_the_websocket_address():
    assert engine_status_url("ws://127.0.0.1:8000") == \
        f"http://127.0.0.1:8000{serve.BACKEND_STATUS_PATH}"
    assert engine_status_url("wss://example.org:9000/") == \
        f"https://example.org:9000{serve.BACKEND_STATUS_PATH}"


# --------------------------------------------------------------- over HTTP

class _Engine(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 -- the stdlib's name
        if self.path != serve.BACKEND_STATUS_PATH:
            self.send_error(404)
            return
        body = json.dumps({"failures": 7, "out_of_memory": 3, "last_error": "x"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def engine():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"ws://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_status_is_read_from_a_live_engine(engine):
    status = fetch_engine_status(engine_status_url(engine))
    assert status == {"failures": 7, "out_of_memory": 3, "last_error": "x"}


def test_an_engine_without_the_route_is_reported_as_old(engine):
    with pytest.raises(EngineTooOld):
        fetch_engine_status(engine.replace("ws://", "http://") + "/elsewhere")


def test_an_unreachable_engine_is_not_an_error():
    # Port 9 (discard) is closed on any sane desktop.
    assert fetch_engine_status("http://127.0.0.1:9/meeting-subtitles/backend") is None
