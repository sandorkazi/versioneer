"""ITEM 3: inotify/watchdog immediate mode (watch + daemon).

Covers: snapshot/diff still works, watchdog path (mocked Observer),
polling fallback, daemon parse_interval("inotify") + VERSIONEER_INTERVAL
override + zero-git-writes default for inotify configs.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import types

from click.testing import CliRunner

from versioneer.cli import cli
from versioneer.core import config as cfg


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("VERSIONEER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("NOTIFY_DEBUG", "1")
    # never let the real env override interval parsing unexpectedly
    monkeypatch.delenv("VERSIONEER_INTERVAL", raising=False)


def _make(runner, tmp_path, name="app"):
    r = runner.invoke(
        cli,
        [
            "config",
            "create",
            "--name",
            name,
            "--path",
            str(tmp_path / f"store-{name}"),
            "--upstream",
            "git@example:x.git",
        ],
    )
    assert r.exit_code == 0, r.output
    return r


def test_snapshot_diff_still_works(tmp_path):
    from versioneer.core import watch as _watch

    top = tmp_path / "proj"
    top.mkdir()
    (top / "a.txt").write_text("v1\n")
    (top / "b.txt").write_text("b\n")
    before = _watch.snapshot(top, [])
    assert set(before) == {"a.txt", "b.txt"}

    (top / "a.txt").write_text("v2\n")  # changed
    (top / "c.txt").write_text("new\n")  # added
    (top / "b.txt").unlink()  # removed
    after = _watch.snapshot(top, [])
    diff = _watch.diff_snapshots(before, after)
    assert diff["changed"] == ["a.txt"]
    assert diff["added"] == ["c.txt"]
    assert diff["removed"] == ["b.txt"]


def test_watch_dir_uses_watchdog_when_available(tmp_path, monkeypatch):
    """Mocked watchdog Observer path: change during watch is detected."""
    from versioneer.core import watch as _watch

    top = tmp_path / "wd"
    top.mkdir()
    (top / "a.txt").write_text("v1\n")

    calls = {"scheduled": 0, "started": 0, "stopped": 0}

    class FakeObserver:
        def schedule(self, *a, **k):
            calls["scheduled"] += 1

        def start(self):
            calls["started"] += 1

        def stop(self):
            calls["stopped"] += 1

        def join(self, timeout=None):
            pass

    class FakeEventHandler:
        pass

    fake_observers = types.ModuleType("watchdog.observers")
    fake_observers.Observer = FakeObserver
    fake_events = types.ModuleType("watchdog.events")
    fake_events.FileSystemEventHandler = FakeEventHandler
    fake_top = types.ModuleType("watchdog")
    monkeypatch.setitem(sys.modules, "watchdog", fake_top)
    monkeypatch.setitem(sys.modules, "watchdog.observers", fake_observers)
    monkeypatch.setitem(sys.modules, "watchdog.events", fake_events)
    monkeypatch.setattr(_watch, "watchdog_available", lambda: True)

    # mutate the tree shortly after the watch starts (in watchdog mode
    # the waiter still snapshots before/after, so this must be picked up)
    def _mutate():
        time.sleep(0.05)
        (top / "a.txt").write_text("v2\n")
        (top / "new.txt").write_text("hello\n")

    t = threading.Thread(target=_mutate)
    t.start()
    before, after = _watch.watch_dir(top, [], timeout_s=0.4)
    t.join()

    assert calls["scheduled"] >= 1
    assert calls["started"] >= 1
    assert calls["stopped"] >= 1
    diff = _watch.diff_snapshots(before, after)
    assert "a.txt" in diff["changed"]
    assert "new.txt" in diff["added"]


def test_watch_dir_fallback_polling_without_watchdog(tmp_path, monkeypatch):
    from versioneer.core import watch as _watch

    monkeypatch.setattr(_watch, "watchdog_available", lambda: False)
    # ensure no real watchdog import can sneak in
    monkeypatch.delitem(sys.modules, "watchdog", raising=False)
    monkeypatch.delitem(sys.modules, "watchdog.observers", raising=False)
    monkeypatch.delitem(sys.modules, "watchdog.events", raising=False)

    top = tmp_path / "poll"
    top.mkdir()
    (top / "a.txt").write_text("v1\n")

    def _mutate():
        time.sleep(0.05)
        (top / "a.txt").write_text("v2\n")

    t = threading.Thread(target=_mutate)
    t.start()
    t0 = time.monotonic()
    before, after = _watch.watch_dir(top, [], timeout_s=0.4)
    dt = time.monotonic() - t0
    t.join()

    assert dt >= 0.3  # actually waited (polling fallback), not instant
    diff = _watch.diff_snapshots(before, after)
    assert diff["changed"] == ["a.txt"]
    assert diff["added"] == []


def test_daemon_parse_interval_inotify_and_override(monkeypatch):
    from versioneer.core import daemon as _daemon

    monkeypatch.delenv("VERSIONEER_INTERVAL", raising=False)
    assert _daemon.parse_interval("inotify") == 0
    # plain specs still parse
    assert _daemon.parse_interval("10s") == 10
    assert _daemon.parse_interval("5m") == 300
    assert _daemon.parse_interval("3h") == 10800
    # env override wins over spec (tests accelerate 3h -> 10s)
    monkeypatch.setenv("VERSIONEER_INTERVAL", "10s")
    assert _daemon.parse_interval("inotify") == 10
    assert _daemon.parse_interval("3h") == 10
    # inotify tight-poll fallback honors the same override
    assert _daemon.inotify_poll_interval() == 10
    monkeypatch.delenv("VERSIONEER_INTERVAL", raising=False)
    assert _daemon.inotify_poll_interval() == _daemon.INOTIFY_FALLBACK_POLL_S


def test_daemon_inotify_config_stays_read_only(tmp_path, monkeypatch):
    """inotify check_interval must keep the zero-git-writes default."""
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "ino")
    f = tmp_path / "x.conf"
    f.write_text("v1\n")
    assert runner.invoke(cli, ["-C", "ino", "target", "add", str(f)]).exit_code == 0

    c = cfg.load("ino")
    c.meta.check_interval = "inotify"
    c.meta.notify = True
    c.meta.auto_commit = False
    cfg.save(c)

    from versioneer.core import daemon as _daemon
    from versioneer.core import monitor as _mon
    from versioneer.core import store as _store

    f.write_text("v2-drift\n")
    rel = _mon.store_rel_for(
        c.targets[0].path,
        _mon.live_abs_path(c.targets[0].path, c.targets[0].abs_path, ""),
        c.targets[0].flex,
        "",
    ).as_posix()
    n_before = _store.count_artifact_commits(cfg.store_dir(c), rel)
    res = _daemon.check_once("ino")
    assert res["drift"]  # drift detected immediately
    assert res["committed"] == []  # but nothing committed (read-only default)
    assert os.environ.get("NOTIFY_DEBUG") == "1"
    assert res["notified"] is True
    n_after = _store.count_artifact_commits(cfg.store_dir(c), rel)
    assert n_after == n_before  # zero git writes
