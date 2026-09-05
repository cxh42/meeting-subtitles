"""Repair proxy environment variables before anything builds an HTTP client.

GNOME's network settings export ``all_proxy=socks://host:port``. httpx accepts
only ``socks5://`` and raises ``ValueError: Unknown scheme for proxy URL`` from
the *constructor*, so the failure lands wherever a client is first created --
inside huggingface_hub, inside transformers -- rather than at any point that
looks like it is about to use a proxy. One bad variable is enough to kill an
otherwise fully offline model load.
"""

import logging
import os

logger = logging.getLogger(__name__)

PROXY_VARS = (
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
)


def normalize_proxy_env() -> list[str]:
    """Rewrite ``socks://`` to ``socks5://``. Returns the names it changed."""
    changed = []
    for name in PROXY_VARS:
        value = os.environ.get(name)
        if value and value.startswith("socks://"):
            os.environ[name] = "socks5://" + value[len("socks://"):]
            changed.append(name)
    if changed:
        logger.debug("已修正代理变量 scheme: %s", ", ".join(changed))
    return changed


def clear_proxy_env() -> None:
    """Drop every proxy variable.

    For strictly offline work: a proxy cannot help, and a malformed one can
    still abort the run while a client that is never used is constructed.
    """
    for name in PROXY_VARS:
        if name.lower() != "no_proxy":
            os.environ.pop(name, None)
