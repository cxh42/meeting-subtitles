"""Read the engine's backend failure report (see ``serve.BackendFailures``).

Apart from the WebSocket client on purpose: this needs nothing outside the
stdlib, so the logic that decides "recognition has died" can be tested
without installing the server's dependencies.
"""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, build_opener

from meeting_subtitles.serve import BACKEND_STATUS_PATH


class EngineTooOld(Exception):
    """The engine predates the failure report: it has no status route."""


def engine_status_url(server: str) -> str:
    """The status route on the same server as the ``ws://`` address."""
    parsed = urlparse(server)
    scheme = "https" if parsed.scheme == "wss" else "http"
    return f"{scheme}://{parsed.netloc}{BACKEND_STATUS_PATH}"


def fetch_engine_status(url: str) -> dict | None:
    """The engine's backend failure counters, or None when unreachable.

    Bypasses any configured proxy, as doctor's health check does: urllib would
    otherwise send 127.0.0.1 through the SOCKS proxy the desktop exports.
    """
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(url, timeout=2) as response:
            status = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            raise EngineTooOld from exc
        return None
    except (URLError, OSError, ValueError):
        return None
    return status if isinstance(status, dict) else None
