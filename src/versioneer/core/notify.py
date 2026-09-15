"""Desktop notifications: notify-send / D-Bus with stdout/journal fallback."""

from __future__ import annotations

import os
import shutil
import subprocess


def send(title: str, body: str = "") -> str:
    """Send a desktop notification. Returns method used: notify-send|stdout.

    Never raises — daemon must not crash when D-Bus is missing.
    Set NOTIFY_DEBUG=1 to force stdout (used in tests/docs).
    """
    if os.environ.get("NOTIFY_DEBUG") == "1":
        print(f"[notify] {title}: {body}")
        return "stdout"
    exe = shutil.which("notify-send")
    if exe:
        try:
            subprocess.run([exe, title, body] if body else [exe, title],
                           check=False, capture_output=True, timeout=10)
            return "notify-send"
        except (OSError, subprocess.TimeoutExpired):
            pass
    print(f"[notify] {title}: {body}" if body else f"[notify] {title}")
    return "stdout"
