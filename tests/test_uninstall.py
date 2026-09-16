"""ITEM 5: `versioneer uninstall` CLI (mirrors installer/uninstall.sh).

- --help lists --purge-stores/--yes
- confirm N keeps configs, --yes removes
- --purge-stores handling (kept by default, purged with --yes)
- units disabled (systemctl mocked), running exe never deleted
"""

from __future__ import annotations

import sys
from pathlib import Path

from click.testing import CliRunner

from versioneer.cli import cli
from versioneer.core import config as cfg


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("VERSIONEER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VERSIONEER_VENV_DIR", str(tmp_path / "venv"))
    for d in ("cfg", "state", "xdg", "home", "venv"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)


def _make_config(runner, tmp_path, name="hypr"):
    r = runner.invoke(cli, ["config", "create", "--name", name,
                            "--path", str(tmp_path / f"store-{name}"),
                            "--upstream", "git@example:x.git"])
    assert r.exit_code == 0, r.output
    assert cfg.config_path(name).exists()
    assert (tmp_path / f"store-{name}").is_dir()


def _mock_systemctl(monkeypatch):
    import shutil
    import subprocess

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
    return seen


def test_uninstall_help(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["uninstall", "--help"])
    assert r.exit_code == 0, r.output
    assert "--purge-stores" in r.output
    assert "--yes" in r.output


def test_uninstall_decline_keeps_configs(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make_config(runner, tmp_path, "hypr")
    seen = _mock_systemctl(monkeypatch)
    r = runner.invoke(cli, ["uninstall"], input="n\n")
    assert r.exit_code == 0, r.output
    assert cfg.config_path("hypr").exists()
    assert "kept" in r.output
    # units disabled via systemctl (user + system)
    flat = [" ".join(c) for c in seen]
    assert any("versioneer-user" in c for c in flat), flat
    assert any("versioneer-system" in c for c in flat), flat
    # stores kept by default
    assert (tmp_path / "store-hypr").is_dir()
    assert "kept ~/versioneer-store" in r.output


def test_uninstall_yes_removes_configs_keeps_stores(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make_config(runner, tmp_path, "hypr")
    venv = tmp_path / "venv"
    assert venv.is_dir()
    _mock_systemctl(monkeypatch)
    # sys.executable is the test env python, not inside tmp venv -> venv removed
    assert Path(sys.executable).resolve() != venv.resolve()
    r = runner.invoke(cli, ["uninstall", "--yes"])
    assert r.exit_code == 0, r.output
    assert not (tmp_path / "cfg").exists()
    assert (tmp_path / "store-hypr").is_dir()
    assert not venv.exists()
    assert "done." in r.output


def test_uninstall_purge_stores(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make_config(runner, tmp_path, "hypr")
    _mock_systemctl(monkeypatch)
    r = runner.invoke(cli, ["uninstall", "--purge-stores", "--yes"])
    assert r.exit_code == 0, r.output
    assert not (tmp_path / "cfg").exists()
    assert not (tmp_path / "store-hypr").exists()
    assert "purged" in r.output


def test_uninstall_purge_decline_keeps_stores(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make_config(runner, tmp_path, "hypr")
    _mock_systemctl(monkeypatch)
    # first confirm (configs) yes, second (purge) no
    r = runner.invoke(cli, ["uninstall", "--purge-stores"], input="y\nn\n")
    assert r.exit_code == 0, r.output
    assert not (tmp_path / "cfg").exists()
    assert (tmp_path / "store-hypr").is_dir()
    assert "kept ~/versioneer-store" in r.output


def test_uninstall_never_deletes_running_exe(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    import sys as _sys

    runner = CliRunner()
    _make_config(runner, tmp_path, "hypr")
    _mock_systemctl(monkeypatch)
    # pretend the venv IS the running interpreter's prefix
    fake_venv = tmp_path / "venv"
    monkeypatch.setattr(_sys, "executable", str(fake_venv / "bin" / "python"))
    (fake_venv / "bin").mkdir(parents=True, exist_ok=True)
    (fake_venv / "bin" / "python").write_text("fake")
    r = runner.invoke(cli, ["uninstall", "--yes"])
    assert r.exit_code == 0, r.output
    assert fake_venv.exists()
    assert "skipping venv removal" in r.output
    # configs still removed
    assert not (tmp_path / "cfg").exists()


def test_uninstall_removes_user_units(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make_config(runner, tmp_path, "hypr")
    _mock_systemctl(monkeypatch)
    r = runner.invoke(cli, ["service", "install"])
    assert r.exit_code == 0, r.output
    upath = tmp_path / "xdg" / "systemd" / "user" / "versioneer-user.service"
    tpath = tmp_path / "xdg" / "systemd" / "user" / "versioneer-user.timer"
    assert upath.exists() and tpath.exists()
    r = runner.invoke(cli, ["uninstall", "--yes"])
    assert r.exit_code == 0, r.output
    assert not upath.exists()
    assert not tpath.exists()
