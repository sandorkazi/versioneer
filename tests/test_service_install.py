"""ITEM 8: `service install` full preflight (extends partial user-unit write).

- install writes user service+timer and stages system units
- idempotent: second run overwrites with identical content, exit 0
- linger hint, completions check, sops/age optional check, timer enable
"""

from __future__ import annotations

from click.testing import CliRunner

from versioneer.cli import cli
from versioneer.core import daemon as _daemon


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("VERSIONEER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for d in ("cfg", "state", "xdg", "home"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    # Never touch the real user bus in tests: stub the enable step.
    monkeypatch.setattr(
        _daemon, "try_reload_and_enable", lambda: (True, "enabled versioneer-user.timer")
    )


def test_install_writes_service_and_timer(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["service", "install"])
    assert r.exit_code == 0, r.output
    upath = tmp_path / "xdg" / "systemd" / "user" / "versioneer-user.service"
    tpath = tmp_path / "xdg" / "systemd" / "user" / "versioneer-user.timer"
    assert upath.exists() and tpath.exists()
    svc = upath.read_text()
    assert "service run --once" in svc
    assert "Type=oneshot" in svc
    tmr = tpath.read_text()
    assert "OnUnitActiveSec=3h" in tmr
    assert "Persistent=true" in tmr
    sdir = tmp_path / "state"
    assert (sdir / "versioneer-system.service").exists()
    assert (sdir / "versioneer-system.timer").exists()
    # full preflight surface: linger hint + timer enable next-step
    assert "linger" in r.output
    assert "loginctl enable-linger" in r.output
    assert "systemctl --user enable --now versioneer-user.timer" in r.output


def test_install_preflight_has_no_lfs_warning(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["service", "install"])
    assert r.exit_code == 0, r.output
    assert "lfs" not in r.output.lower()
    # units are still written
    assert (tmp_path / "xdg" / "systemd" / "user" / "versioneer-user.service").exists()


def test_install_warns_sops_and_completions(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from versioneer.core import secrets as _sec

    monkeypatch.setattr(_sec, "sops_available", lambda: False)
    monkeypatch.setattr(_sec, "age_available", lambda: False)
    r = CliRunner().invoke(cli, ["service", "install"])
    assert r.exit_code == 0, r.output
    assert "sops" in r.output
    # HOME is an empty fake dir, so completions are missing -> warn
    assert "completions" in r.output


def test_install_idempotent(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    r1 = runner.invoke(cli, ["service", "install"])
    assert r1.exit_code == 0, r1.output
    upath = tmp_path / "xdg" / "systemd" / "user" / "versioneer-user.service"
    tpath = tmp_path / "xdg" / "systemd" / "user" / "versioneer-user.timer"
    first_svc, first_tmr = upath.read_text(), tpath.read_text()
    r2 = runner.invoke(cli, ["service", "install"])
    assert r2.exit_code == 0, r2.output
    assert upath.read_text() == first_svc == _daemon.USER_UNIT
    assert tpath.read_text() == first_tmr == _daemon.USER_TIMER


def test_install_enable_warn_only_when_systemctl_fails(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(
        _daemon,
        "try_reload_and_enable",
        lambda: (False, "systemctl not found (non-systemd machine?)"),
    )
    r = CliRunner().invoke(cli, ["service", "install"])
    assert r.exit_code == 0, r.output
    assert "timer enable skipped" in r.output
    # --no-enable skips the attempt entirely
    r = CliRunner().invoke(cli, ["service", "install", "--no-enable"])
    assert r.exit_code == 0, r.output
    assert "timer enable skipped" not in r.output
