"""Watch-to-add workflow (Phase 4): snapshot dir, wait, diff, add.

Uses watchdog/inotify when available (``pip install versioneer[inotify]``),
with a portable polling fallback. Snapshot/diff stays authoritative in
both modes; Ctrl-C terminates the watch and reports changed files.
"""

from __future__ import annotations

import time
from contextlib import suppress
from pathlib import Path

from versioneer.core import monitor as _mon


def watchdog_available() -> bool:
    """True when the optional ``watchdog`` dependency is importable."""
    try:
        from watchdog.events import FileSystemEventHandler  # noqa: F401
        from watchdog.observers import Observer  # noqa: F401
    except ImportError:
        return False
    return True


def _wait_polling(timeout_s: float, poll_interval: float = 1.0) -> None:
    """Portable fallback wait, chunked so Ctrl-C stays responsive."""
    if timeout_s <= 0:
        while True:
            time.sleep(poll_interval)
    else:
        end = time.monotonic() + timeout_s
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(poll_interval, remaining))


def _wait_with_watchdog(top: Path, timeout_s: float) -> None:
    """Efficient wait via watchdog Observer (no-op handler).

    The handler intentionally does nothing: snapshot/diff at the end
    stays authoritative. The Observer just keeps the wait efficient
    (inotify) instead of busy-polling.
    """
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    class _QuietHandler(FileSystemEventHandler):
        def on_any_event(self, event):
            return None

    with suppress(OSError):
        observer = Observer()
        observer.schedule(_QuietHandler(), str(top), recursive=True)
        observer.start()
        try:
            _wait_polling(timeout_s)
        finally:
            with suppress(OSError):
                observer.stop()
            with suppress(OSError):
                observer.join(timeout=5)
        return
    # path vanished mid-watch (or observer unavailable): plain wait
    _wait_polling(timeout_s)


def watch_dir(
    top: Path,
    ignore: list[str],
    timeout_s: float = 0,
    poll_interval: float = 1.0,
) -> tuple[dict[str, str], dict[str, str]]:
    """Snapshot ``top``, wait, snapshot again; return ``(before, after)``.

    Uses the watchdog Observer when available, else polling fallback.
    ``timeout_s <= 0`` waits until Ctrl-C (KeyboardInterrupt propagates).
    """
    before = snapshot(top, ignore)
    try:
        if watchdog_available():
            _wait_with_watchdog(top, timeout_s)
        else:
            _wait_polling(timeout_s, poll_interval)
    except KeyboardInterrupt:
        pass
    after = snapshot(top, ignore)
    return before, after


def snapshot(top: Path, ignore: list[str]) -> dict[str, str]:
    """Map relpath -> hash for files under top (respecting ignore)."""
    out: dict[str, str] = {}
    try:
        top_resolved = top.resolve(strict=False)
    except OSError:
        return out
    for f in _mon.iter_dir_files(top_resolved, ignore):
        try:
            rel = f.relative_to(top_resolved).as_posix()
        except ValueError:
            continue
        try:
            out[rel] = _mon.sha256_file(f)
        except OSError:
            out[rel] = "read-error"
    return out


def diff_snapshots(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    changed = sorted(r for r in after if r in before and after[r] != before[r])
    added = sorted(r for r in after if r not in before)
    removed = sorted(r for r in before if r not in after)
    return {"changed": changed, "added": added, "removed": removed}
