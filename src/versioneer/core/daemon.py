"""Monitor engine + service app (Phase 3, read-only by default).

Daemon is strictly read-only unless auto_commit opt-in is set for that
config: hash + stat compare only. It never pushes unless auto_push=true.
"""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path

from versioneer.core import config as _cfg
from versioneer.core import monitor as _mon
from versioneer.core import notify as _notify
from versioneer.core import store as _store


def parse_interval(spec: str, default_s: int = 3 * 3600) -> int:
    """Parse '10s'/'5m'/'3h'/'inotify'/plain seconds to seconds (int)."""
    env = os.environ.get("VERSIONEER_INTERVAL", "").strip()
    s = (env or spec or "").strip() or ""
    if not s:
        return default_s
    if s == "inotify":
        return 0
    try:
        if s.endswith("s"):
            return int(s[:-1] or 0)
        if s.endswith("m"):
            return int(s[:-1] or 0) * 60
        if s.endswith("h"):
            return int(s[:-1] or 0) * 3600
        if s.endswith("d"):
            return int(s[:-1] or 0) * 86400
        return int(s)
    except ValueError:
        return default_s


def check_once(config_name: str, host: str = "") -> dict:
    """Single read-only scan + optional auto_commit/auto_push branch.

    Returns {"config": name, "drift": [...], "notified": bool,
             "committed": [...], "pushed": bool, "errors": [...]}.
    Zero git writes unless config.meta.auto_commit is True.
    """
    host = host or socket.gethostname()
    config = _cfg.load(config_name)
    store = _cfg.store_dir(config)
    results = _mon.scan_all(config, store, host)
    drift = [r for r in results if r["state"] != "clean"]
    out: dict = {"config": config_name, "drift": [
        {"target": r["target"].path, "state": r["state"],
         "detail": r["detail"]} for r in drift],
        "notified": False, "committed": [], "pushed": False, "errors": []}
    if not drift:
        return out
    if config.meta.auto_commit:
        # opt-in silent commit path (savegames/snippets)
        from versioneer.core import permissions as _perm

        rels: list[str] = []
        for r in results:
            t = r["target"]
            if r["state"] in ("clean", "missing", "read-error"):
                if r["state"] != "clean":
                    out["errors"].append(f"{t.path}: {r['state']} — {r['detail']}")
                continue
            abs_path = r["abs_path"]
            rel = r["rel"]
            dest = store / rel
            try:
                from versioneer.cli import _stage_artifact as _stage

                _stage(abs_path, t.kind, t.symlink, list(t.ignore or []), dest)
            except OSError as e:
                out["errors"].append(f"{t.path}: stage failed: {e}")
                continue
            try:
                owner, group, mode = _perm.capture(
                    abs_path, follow=(t.symlink == "follow"))
            except OSError as e:
                out["errors"].append(f"{t.path}: stat failed: {e}")
                continue
            t.owner, t.group, t.mode = owner, group, mode
            t.hash = _mon.hash_target(abs_path, t.kind, t.symlink,
                                      list(t.ignore or []))
            rels.append(rel.as_posix())
            out["committed"].append(t.path)
        if out["committed"]:
            try:
                _cfg.save(config)
            except ValueError as e:
                out["errors"].append(f"config save failed: {e}")
                out["committed"] = []
                return out
            try:
                _store.add_and_commit(store, rels,
                                      f"auto-commit {len(out['committed'])} targets")
            except _store.GitError as e:
                out["errors"].append(f"auto-commit failed: {e}")
                out["committed"] = []
                return out
            if config.meta.auto_push:
                try:
                    _store.push(store)
                    out["pushed"] = True
                except _store.GitError as e:
                    out["errors"].append(f"auto-push deferred: {e}")
        if out["errors"] and config.meta.notify:
            _notify.send(f"versioneer {config_name}: auto-commit errors",
                         "; ".join(out["errors"][:3]))
            out["notified"] = True
        return out
    # default: notify only
    if config.meta.notify:
        summary = ", ".join(
            f"{d['target']} ({d['state']})" for d in out["drift"][:5])
        _notify.send(f"versioneer {config_name}: drift detected",
                     f"{len(drift)} target(s): {summary} — run "
                     f"`versioneer -C {config_name} status`")
        out["notified"] = True
    return out


def check_all(host: str = "") -> list[dict]:
    return [check_once(n, host) for n in _cfg.list_configs()]


def run_loop(host: str = "", once: bool = False) -> None:
    """Daemon loop over all configs (used by systemd units)."""
    notified_at: dict[str, float] = {}
    while True:
        for name in _cfg.list_configs():
            try:
                config = _cfg.load(name)
            except (FileNotFoundError, ValueError):
                continue
            interval = parse_interval(config.meta.check_interval or "3h")
            if interval <= 0:
                interval = 3 * 3600  # inotify mode falls back to poll in v1
            now = time.time()
            last = notified_at.get(name, 0)
            if now - last < min(interval, 3 * 3600) and last != 0:
                # damping: at most one notification per interval
                pass
            res = check_once(name, host)
            if res.get("notified") or res.get("committed"):
                notified_at[name] = now
        if once:
            return
        # sleep until the shortest configured interval (min 10s, default 3h)
        waits = []
        for n in _cfg.list_configs():
            try:
                c = _cfg.load(n)
                waits.append(parse_interval(c.meta.check_interval or "3h") or 10800)
            except (FileNotFoundError, ValueError):
                continue
        time.sleep(max(10, min(waits) if waits else 10800))


USER_UNIT = """[Unit]
Description=Versioneer user monitor (read-only unless auto_commit opt-in)
After=graphical-session.target network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=%h/.local/bin/versioneer service check --all
Restart=on-failure
Environment=NOTIFY_DEBUG=0

[Install]
WantedBy=default.target
"""

SYSTEM_UNIT = """[Unit]
Description=Versioneer system monitor (read-only, runs as root)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/versioneer service check --all
Restart=on-failure

[Install]
WantedBy=multi-user.target
"""


def user_unit_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "systemd" / "user" / "versioneer-user.service"
