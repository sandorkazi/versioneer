"""Watch-to-add workflow (Phase 4): snapshot dir, wait, diff, add.

Uses polling (portable, no watchdog dependency) with a short interval.
Ctrl-C terminates the watch and reports changed files.
"""

from __future__ import annotations

from pathlib import Path

from versioneer.core import monitor as _mon


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
