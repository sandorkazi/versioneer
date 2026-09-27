"""{{HOME}}/{{HOST}} templating + hardcoded-path lint helper (Phase 9)."""

from __future__ import annotations

import os
import socket
from pathlib import Path


def render_bytes(data: bytes, home: str | None = None, host: str | None = None) -> bytes:
    """Substitute {{HOME}} / {{HOST}} in artifact bytes (deploy side)."""
    if home is None:
        from versioneer.core import elevate as _elev

        home = str(_elev.effective_home())
    host = host if host is not None else socket.gethostname()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data  # binary: no templating
    text = text.replace("{{HOME}}", home).replace("{{HOST}}", host)
    # also support $HOME-style leftovers? No — only the two tokens in v1.
    return text.encode("utf-8")


def render_file(src: Path, dest_text: str) -> str:
    """Render text with HOME/HOST substitution (str variant)."""
    from versioneer.core import elevate as _elev

    return (dest_text.replace("{{HOME}}", str(_elev.effective_home()))
            .replace("{{HOST}}", socket.gethostname()))


def state_home() -> str:
    from versioneer.core import elevate as _elev

    return os.environ.get("HOME", str(_elev.effective_home()))
