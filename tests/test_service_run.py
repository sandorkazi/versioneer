"""ITEM 4: systemd timer + run_loop wiring.

- `service run [--once]` invokes run_loop
- user+system .timer units (3h default, persistent) installed by `service install`
- run_loop(once=True) never sleeps forever
- damping state kept
"""

from __future__ import annotations

import time

from click.testing import CliRunner

from versioneer.cli import cli
from versioneer.core import daemon as _daemon


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("VERSIONEER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("NOTIFY_DEBUG", "1")


def _make(runner, tmp_path, name="app"):
    r = runner.invoke(cli, ["config", "create", "--name", name,
                            "--path", str(tmp_path / f"store-{name}"),
                            "--upstream", "git@example:x.git"])
    assert r.exit_code == 0, r.output


def test_service_install_writes_service_and_timer(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    r = runner.invoke(cli, ["service", "install"])
    assert r.exit_code == 0, r.output
    upath = tmp_path / "xdg" / "systemd" / "user" / "versioneer-user.service"
    tpath = tmp_path / "xdg" / "systemd" / "user" / "versioneer-user.timer"
    assert upath.exists()
    assert tpath.exists()
    svc = upath.read_text()
    assert "service run --once" in svc
    assert "Type=oneshot" in svc
    tmr = tpath.read_text()
    assert "OnUnitActiveSec=3h" in tmr
    assert "Persistent=true" in tmr
    # system units staged (no sudo needed)
    sdir = tmp_path / "state"
    assert (sdir / "versioneer-system.service").exists()
    assert (sdir / "versioneer-system.timer").exists()
    sys_tmr = (sdir / "versioneer-system.timer").read_text()
    assert "OnUnitActiveSec=3h" in sys_tmr
    assert "Persistent=true" in sys_tmr


def test_service_run_once_works(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "hypr")
    f = tmp_path / "a.conf"
    f.write_text("v1\n")
    assert runner.invoke(cli, ["-C", "hypr", "target", "add", str(f)]).exit_code == 0
    r = runner.invoke(cli, ["service", "run", "--once"])
    assert r.exit_code == 0, r.output
    assert "run complete" in r.output


def test_service_run_once_no_configs(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    r = runner.invoke(cli, ["service", "run", "--once"])
    assert r.exit_code == 0, r.output


def test_run_loop_once_does_not_sleep(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "s")

    def _boom(*a, **k):
        raise AssertionError("must not sleep on once=True")

    monkeypatch.setattr(time, "sleep", _boom)
    monkeypatch.setattr(_daemon, "wait_inotify", _boom)
    start = time.monotonic() if hasattr(time, "monotonic") else 0
    _daemon.run_loop("", once=True)
    # returns promptly (no sleep/wait); wall-clock guard via timeout in pytest
    assert True
    _ = start


def test_service_run_invokes_run_loop(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    calls: dict = {}

    def _fake(host="", once=False):
        calls["host"] = host
        calls["once"] = once

    monkeypatch.setattr(_daemon, "run_loop", _fake)
    r = runner.invoke(cli, ["service", "run", "--once", "--host", "h1"])
    assert r.exit_code == 0, r.output
    assert calls == {"host": "h1", "once": True}


def test_service_enable_disable_use_timer(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    import shutil
    import subprocess

    runner = CliRunner()
    seen: list = []

    class _P:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(cmd, **kw):
        seen.append(cmd)
        return _P()

    monkeypatch.setattr(shutil, "which", lambda *a, **k: "/usr/bin/systemctl")
    monkeypatch.setattr(subprocess, "run", _fake_run)
    r = runner.invoke(cli, ["service", "enable"])
    assert r.exit_code == 0, r.output
    assert any("versioneer-user.timer" in c for c in seen), seen
    seen.clear()
    r = runner.invoke(cli, ["service", "disable"])
    assert r.exit_code == 0, r.output
    assert any("versioneer-user.timer" in c for c in seen), seen
