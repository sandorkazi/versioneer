"""Monitor engine + service app (Phase 3, read-only by default).

Daemon is strictly read-only unless auto_commit opt-in is set for that
config: hash + stat compare only. It never pushes unless auto_push=true.
"""

from __future__ import annotations

import os
import socket
import time
from contextlib import suppress
from pathlib import Path

from versioneer.core import config as _cfg
from versioneer.core import monitor as _mon
from versioneer.core import notify as _notify
from versioneer.core import store as _store

#: Tight-poll fallback when check_interval="inotify" but watchdog is
#: not installed. Overridable in tests via VERSIONEER_INTERVAL.
INOTIFY_FALLBACK_POLL_S = 5


def watchdog_available() -> bool:
    """True when the optional ``watchdog`` dependency is importable."""
    try:
        from watchdog.events import FileSystemEventHandler  # noqa: F401
        from watchdog.observers import Observer  # noqa: F401
    except ImportError:
        return False
    return True


def inotify_poll_interval() -> int:
    """Poll seconds for inotify fallback (honors VERSIONEER_INTERVAL)."""
    env = os.environ.get("VERSIONEER_INTERVAL", "").strip()
    if env and env != "inotify":
        val = parse_interval(env)
        return val if val > 0 else INOTIFY_FALLBACK_POLL_S
    return INOTIFY_FALLBACK_POLL_S


def wait_inotify(top_paths: list[Path], timeout_s: float) -> None:
    """Wait for filesystem events (watchdog) or fall back to sleep.

    Snapshot/diff stays authoritative in the caller; this is only an
    efficient wait. Missing/non-dir paths are skipped; with nothing to
    watch it degrades to ``time.sleep``.
    """
    timeout_s = max(0, timeout_s)
    if watchdog_available():
        with suppress(ImportError, OSError):
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            class _QuietHandler(FileSystemEventHandler):
                def on_any_event(self, event):
                    return None

            observer = Observer()
            scheduled = 0
            handler = _QuietHandler()
            for p in top_paths:
                with suppress(OSError):
                    if p.is_dir():
                        observer.schedule(handler, str(p), recursive=True)
                        scheduled += 1
            if scheduled:
                observer.start()
                try:
                    if timeout_s <= 0:
                        while True:
                            time.sleep(1)
                    else:
                        time.sleep(timeout_s)
                finally:
                    with suppress(OSError):
                        observer.stop()
                    with suppress(OSError):
                        observer.join(timeout=5)
                return
    if timeout_s <= 0:
        while True:
            time.sleep(1)
    else:
        time.sleep(timeout_s)


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
    out: dict = {
        "config": config_name,
        "drift": [
            {"target": r["target"].path, "state": r["state"], "detail": r["detail"]} for r in drift
        ],
        "notified": False,
        "committed": [],
        "pushed": False,
        "errors": [],
    }
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
                owner, group, mode = _perm.capture(abs_path, follow=(t.symlink == "follow"))
            except OSError as e:
                out["errors"].append(f"{t.path}: stat failed: {e}")
                continue
            t.owner, t.group, t.mode = owner, group, mode
            t.hash = _mon.hash_target(abs_path, t.kind, t.symlink, list(t.ignore or []))
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
                _store.add_and_commit(store, rels, f"auto-commit {len(out['committed'])} targets")
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
            _notify.send(
                f"versioneer {config_name}: auto-commit errors", "; ".join(out["errors"][:3])
            )
            out["notified"] = True
        return out
    # default: notify only
    if config.meta.notify:
        summary = ", ".join(f"{d['target']} ({d['state']})" for d in out["drift"][:5])
        _notify.send(
            f"versioneer {config_name}: drift detected",
            f"{len(drift)} target(s): {summary} — run `versioneer -C {config_name} status`",
        )
        out["notified"] = True
    return out


def check_all(host: str = "") -> list[dict]:
    return [check_once(n, host) for n in _cfg.list_configs()]


def run_loop(host: str = "", once: bool = False) -> None:
    """Daemon loop over all configs (used by systemd units)."""
    notified_at: dict[str, float] = {}
    while True:
        inotify_tops: list[Path] = []
        for name in _cfg.list_configs():
            try:
                config = _cfg.load(name)
            except (FileNotFoundError, ValueError):
                continue
            interval = parse_interval(config.meta.check_interval or "3h")
            if interval <= 0:
                # inotify immediate mode: collect live dirs to watch;
                # the check below still runs immediately (damped).
                for t in config.targets:
                    try:
                        live = _mon.live_abs_path(t.path, t.abs_path, config.meta.root or "")
                    except (ValueError, OSError):
                        continue
                    top = live if live.is_dir() else live.parent
                    if top not in inotify_tops:
                        inotify_tops.append(top)
                interval = inotify_poll_interval()
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
        # sleep until the shortest configured interval (min 10s for timed
        # configs; inotify configs use the tight watchdog/tight-poll wait).
        waits = []
        inotify_pending = False
        for n in _cfg.list_configs():
            try:
                c = _cfg.load(n)
                iv = parse_interval(c.meta.check_interval or "3h")
                waits.append(iv if iv > 0 else inotify_poll_interval())
                if iv <= 0:
                    inotify_pending = True
            except (FileNotFoundError, ValueError):
                continue
        if inotify_pending:
            wait_inotify(inotify_tops, min(waits) if waits else INOTIFY_FALLBACK_POLL_S)
        else:
            time.sleep(max(10, min(waits) if waits else 10800))


USER_UNIT = """[Unit]
Description=Versioneer user monitor (read-only unless auto_commit opt-in)
After=graphical-session.target network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=%h/.local/bin/versioneer service run --once
Environment=NOTIFY_DEBUG=0

[Install]
WantedBy=default.target
"""

SYSTEM_UNIT = """[Unit]
Description=Versioneer system monitor (read-only, runs as root)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/versioneer service run --once

[Install]
WantedBy=multi-user.target
"""

#: 3h default check interval, persistent across reboots/suspends.
#: Triggered units above are oneshot `service run --once` passes.
USER_TIMER = """[Unit]
Description=Versioneer user monitor timer (3h, persistent)

[Timer]
OnBootSec=15min
OnUnitActiveSec=3h
Persistent=true

[Install]
WantedBy=timers.target
"""

SYSTEM_TIMER = """[Unit]
Description=Versioneer system monitor timer (3h, persistent)

[Timer]
OnBootSec=15min
OnUnitActiveSec=3h
Persistent=true

[Install]
WantedBy=timers.target
"""

USER_SERVICE_NAME = "versioneer-user.service"
USER_TIMER_NAME = "versioneer-user.timer"
SYSTEM_SERVICE_NAME = "versioneer-system.service"
SYSTEM_TIMER_NAME = "versioneer-system.timer"


def completion_paths(home: Path | None = None) -> list[Path]:
    """User-level shell completion paths (mirrors installer/install.sh)."""
    base = home or Path.home()
    return [
        base / ".local/share/bash-completion/completions/versioneer",
        base / ".config/fish/completions/versioneer.fish",
        base / ".zfunc/_versioneer",
    ]


def missing_completions(home: Path | None = None) -> list[Path]:
    """Completion files that are not yet installed (warn-only callers)."""
    missing: list[Path] = []
    for p in completion_paths(home):
        try:
            if not (p.is_file() or p.is_symlink()):
                missing.append(p)
        except OSError:
            missing.append(p)
    return missing


def linger_hint() -> str:
    """Shell hint to keep user timers running after logout."""
    import getpass as _getpass

    try:
        user = _getpass.getuser()
    except (OSError, KeyError):
        user = "$USER"
    return f"loginctl enable-linger {user}  # keep user timers alive after logout"


def is_linger_enabled() -> bool | None:
    """True/False when `loginctl show-user` works, else None (unknown)."""
    import shutil as _shutil
    import subprocess as _sp

    exe = _shutil.which("loginctl")
    if not exe:
        return None
    try:
        proc = _sp.run(
            [exe, "show-user", os.environ.get("USER", ""), "-p", "Linger", "--value"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, _sp.TimeoutExpired):
        return None
    out = (proc.stdout or "").strip().lower()
    if out == "yes":
        return True
    if out == "no":
        return False
    return None


def prereq_warnings() -> list[str]:
    """Warn-only preflight: git-lfs (required) + sops/age (optional)."""
    warnings: list[str] = []
    if not _store.lfs_available():
        warnings.append(
            "git-lfs not found — install with: sudo pacman -S git-lfs "
            "&& git lfs install (binary targets fall back to plain git)"
        )
    try:
        from versioneer.core import secrets as _sec

        st = _sec.toolchain_status()
        if not st.get("sops") or not st.get("age"):
            warnings.append(
                "sops/age not found (optional) — install with: "
                "sudo pacman -S sops age (encrypt=true stays plaintext until then)"
            )
    except ImportError:
        pass
    return warnings


def write_user_units() -> tuple[Path, Path]:
    """Idempotently write user service+timer. Overwrite is safe (same content)."""
    upath = user_unit_path()
    tpath = user_timer_path()
    upath.parent.mkdir(parents=True, exist_ok=True)
    upath.write_text(USER_UNIT, encoding="utf-8")
    tpath.write_text(USER_TIMER, encoding="utf-8")
    return upath, tpath


def stage_system_units(state_dir: Path) -> tuple[Path, Path]:
    """Stage system service+timer under state dir (needs sudo to activate)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    spat = state_dir / SYSTEM_SERVICE_NAME
    tpat = state_dir / SYSTEM_TIMER_NAME
    spat.write_text(SYSTEM_UNIT, encoding="utf-8")
    tpat.write_text(SYSTEM_TIMER, encoding="utf-8")
    return spat, tpat


def try_reload_and_enable() -> tuple[bool, str]:
    """Best-effort `daemon-reload + enable --now` for the user timer.

    Never raises: returns (ok, detail). Missing systemctl or a failing
    daemon-reload/enable yields (False, reason) so `service install`
    stays warn-only and idempotent on non-systemd machines/CI.
    """
    import shutil as _shutil
    import subprocess as _sp

    exe = _shutil.which("systemctl")
    if not exe:
        return False, "systemctl not found (non-systemd machine?)"
    try:
        _sp.run([exe, "--user", "daemon-reload"], check=False, capture_output=True, timeout=30)
        proc = _sp.run(
            [exe, "--user", "enable", "--now", USER_TIMER_NAME],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, _sp.TimeoutExpired) as e:
        return False, f"systemctl failed: {e}"
    if proc.returncode != 0:
        detail = ((proc.stderr or proc.stdout) or "").strip()
        return False, f"systemctl enable failed: {detail or 'unknown error'}"
    return True, f"enabled {USER_TIMER_NAME}"


def user_unit_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "systemd" / "user" / USER_SERVICE_NAME


def user_timer_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "systemd" / "user" / USER_TIMER_NAME
