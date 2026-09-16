"""ITEM 6: manifest --apply (safe subset).

Default is print-only replay; --apply opts into:
- packages: `sudo pacman -S --needed ...` (parsed from packages.list)
- systemd: `systemctl enable` (user + system units from units.list)
- env: print-only always (even with --apply)
- wine: generate setup-wine.sh, never auto-run winetricks

dry-run never runs subprocesses nor writes files, even with --apply.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from versioneer.cli import cli
from versioneer.core import config as cfg
from versioneer.core import deploy as _dep
from versioneer.core import manifest as _mg


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("VERSIONEER_STATE_DIR", str(tmp_path / "state"))


def _make(tmp_path, name="m"):
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "create", "--name", name,
                            "--path", str(tmp_path / f"store-{name}"),
                            "--upstream", "git@example:x.git"])
    assert r.exit_code == 0, r.output
    return runner


def _add_manifest(name, fname, content):
    from versioneer.core import monitor as _mon

    c = cfg.load(name)
    store = cfg.store_dir(c)
    dest = store / fname
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    digest = _mon.hash_target(dest, "text", "preserve", [])
    c.targets.append(cfg.Target(
        path=fname, abs_path=str(dest), kind="manifest",
        flex="fixed", interest="state", owner="", group="",
        mode="0644", hash=digest))
    cfg.save(c)
    return cfg.load(name).targets[-1], store


PACKAGES = """# versioneer packages manifest

[pacman -Qqe]
vim
git
htop

[AUR]
yay-bin

[flatpak]
org.mozilla.firefox
"""

UNITS = """# enabled user units
pipewire.service enabled
pipewire-pulse.service enabled

# enabled system units
sshd.service enabled
NetworkManager.service enabled
"""

WINE = json.dumps({
    "wine_version": "wine-9.0",
    "WINEARCH": "win64",
    "WINEPREFIX": "/home/u/.wine",
    "winetricks_installed": ["corefonts", "vcrun2019"],
    "exe_inventory": ["drive_c/Program Files/game/game.exe"],
    "host": "h",
}, indent=2)

ENV = json.dumps({"PATH": "/usr/bin", "SHELL": "/bin/bash"})


def _ok_proc(stdout="done"):
    p = mock.Mock()
    p.returncode = 0
    p.stdout = stdout
    p.stderr = ""
    return p


def _fail_proc():
    p = mock.Mock()
    p.returncode = 1
    p.stdout = ""
    p.stderr = "boom"
    return p


# ---------- parsers ----------

def test_parse_packages_and_systemd():
    parsed = _mg.parse_packages_manifest(PACKAGES)
    assert parsed["pacman"] == ["vim", "git", "htop"]
    assert parsed["aur"] == ["yay-bin"]
    assert parsed["flatpak"] == ["org.mozilla.firefox"]
    cmd = _mg.packages_apply_command(parsed["pacman"])
    assert cmd[:4] == ["sudo", "pacman", "-S", "--needed"]
    assert cmd[4:] == ["vim", "git", "htop"]

    units = _mg.parse_systemd_manifest(UNITS)
    assert "pipewire.service" in units["user"]
    assert "sshd.service" in units["system"]

    script = _mg.wine_setup_script_text(WINE)
    assert "WINEPREFIX" in script and "corefonts" in script
    assert "winetricks" in script


# ---------- packages ----------

def test_packages_default_is_print_only(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _make(tmp_path)
    t, store = _add_manifest("m", "packages.list", PACKAGES)
    c = cfg.load("m")
    with mock.patch("versioneer.core.deploy.subprocess.run") as mrun:
        mrun.side_effect = AssertionError("must not call subprocess")
        r = _dep.deploy_one(t, c, store, dry_run=False, apply_manifest=False)
    assert r["status"] == "ok"
    assert "sudo pacman -S --needed" in r["reason"]
    assert "vim" in (r.get("replay") or "")


def test_packages_dry_run_noop_even_with_apply(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _make(tmp_path)
    t, store = _add_manifest("m", "packages.list", PACKAGES)
    c = cfg.load("m")
    with mock.patch("versioneer.core.deploy.subprocess.run") as mrun:
        r = _dep.deploy_one(t, c, store, dry_run=True, apply_manifest=True)
        mrun.assert_not_called()
    assert r["status"] == "skipped"
    assert "dry-run" in r["reason"]
    # dry-run writes no status side effects here (deploy_one is pure);
    # wine script must not appear either
    assert not (tmp_path / "state" / "setup-wine.sh").exists()


def test_packages_apply_calls_pacman(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _make(tmp_path)
    t, store = _add_manifest("m", "packages.list", PACKAGES)
    c = cfg.load("m")
    with mock.patch("versioneer.core.deploy.subprocess.run",
                     return_value=_ok_proc()) as mrun:
        r = _dep.deploy_one(t, c, store, dry_run=False, apply_manifest=True)
    assert r["status"] == "ok", r
    assert "applied" in r["reason"]
    assert mrun.call_count == 1
    cmd = mrun.call_args[0][0]
    assert cmd[:4] == ["sudo", "pacman", "-S", "--needed"]
    assert "vim" in cmd and "git" in cmd
    # AUR/flatpak are hints only, never passed to pacman
    assert "yay-bin" not in cmd
    assert "org.mozilla.firefox" not in cmd


def test_packages_apply_failure_is_error(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _make(tmp_path)
    t, store = _add_manifest("m", "packages.list", PACKAGES)
    c = cfg.load("m")
    with mock.patch("versioneer.core.deploy.subprocess.run",
                     return_value=_fail_proc()):
        r = _dep.deploy_one(t, c, store, dry_run=False, apply_manifest=True)
    assert r["status"] == "error"
    assert "pacman" in r["reason"]


# ---------- systemd ----------

def test_systemd_default_print_only(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _make(tmp_path)
    t, store = _add_manifest("m", "units.list", UNITS)
    c = cfg.load("m")
    with mock.patch("versioneer.core.deploy.subprocess.run") as mrun:
        mrun.side_effect = AssertionError("must not call subprocess")
        r = _dep.deploy_one(t, c, store, dry_run=False, apply_manifest=False)
    assert r["status"] == "ok"
    assert "systemctl" in r["reason"]


def test_systemd_dry_run_noop(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _make(tmp_path)
    t, store = _add_manifest("m", "units.list", UNITS)
    c = cfg.load("m")
    with mock.patch("versioneer.core.deploy.subprocess.run") as mrun:
        r = _dep.deploy_one(t, c, store, dry_run=True, apply_manifest=True)
        mrun.assert_not_called()
    assert r["status"] == "skipped"


def test_systemd_apply_calls_systemctl(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _make(tmp_path)
    t, store = _add_manifest("m", "units.list", UNITS)
    c = cfg.load("m")
    with mock.patch("versioneer.core.deploy.subprocess.run",
                     return_value=_ok_proc()) as mrun:
        r = _dep.deploy_one(t, c, store, dry_run=False, apply_manifest=True)
    assert r["status"] == "ok", r
    assert "enabled" in r["reason"]
    # user units via `systemctl --user enable`, system via sudo
    cmds = [call[0][0] for call in mrun.call_args_list]
    assert any(cmd[:3] == ["systemctl", "--user", "enable"] for cmd in cmds)
    assert any("systemctl" in cmd and "sshd.service" in cmd for cmd in cmds)
    assert mrun.call_count == 2


def test_systemd_apply_failure_is_error(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _make(tmp_path)
    t, store = _add_manifest("m", "units.list", UNITS)
    c = cfg.load("m")
    with mock.patch("versioneer.core.deploy.subprocess.run",
                     return_value=_fail_proc()):
        r = _dep.deploy_one(t, c, store, dry_run=False, apply_manifest=True)
    assert r["status"] == "error"


# ---------- env (print-only always) ----------

def test_env_apply_is_print_only(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _make(tmp_path)
    t, store = _add_manifest("m", "env.json", ENV)
    c = cfg.load("m")
    with mock.patch("versioneer.core.deploy.subprocess.run") as mrun:
        r = _dep.deploy_one(t, c, store, dry_run=False, apply_manifest=True)
        mrun.assert_not_called()
    assert r["status"] == "ok"
    assert "print-only" in r["reason"]


# ---------- wine (script generation, never exec) ----------

def test_wine_default_no_script_no_exec(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _make(tmp_path)
    t, store = _add_manifest("m", "wine-manifest.json", WINE)
    c = cfg.load("m")
    with mock.patch("versioneer.core.deploy.subprocess.run") as mrun:
        mrun.side_effect = AssertionError("wine must never call subprocess")
        r = _dep.deploy_one(t, c, store, dry_run=False, apply_manifest=False)
        mrun.assert_not_called()
    assert r["status"] == "ok"
    assert not (tmp_path / "state" / "setup-wine.sh").exists()


def test_wine_apply_generates_script_never_runs(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _make(tmp_path)
    t, store = _add_manifest("m", "wine-manifest.json", WINE)
    c = cfg.load("m")
    with mock.patch("versioneer.core.deploy.subprocess.run") as mrun:
        r = _dep.deploy_one(t, c, store, dry_run=False, apply_manifest=True)
        mrun.assert_not_called()
    assert r["status"] == "ok", r
    script = Path(r.get("script") or str(tmp_path / "state" / "setup-wine.sh"))
    assert script.exists()
    text = script.read_text(encoding="utf-8")
    assert 'WINEPREFIX="/home/u/.wine"' in text
    assert "corefonts" in text
    assert "winetricks" in text
    assert "never auto-run" in r["reason"]


def test_wine_dry_run_writes_nothing(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _make(tmp_path)
    t, store = _add_manifest("m", "wine-manifest.json", WINE)
    c = cfg.load("m")
    with mock.patch("versioneer.core.deploy.subprocess.run") as mrun:
        r = _dep.deploy_one(t, c, store, dry_run=True, apply_manifest=True)
        mrun.assert_not_called()
    assert r["status"] == "skipped"
    assert not (tmp_path / "state" / "setup-wine.sh").exists()


# ---------- CLI end-to-end ----------

def test_cli_deploy_apply_packages(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = _make(tmp_path)
    _add_manifest("m", "packages.list", PACKAGES)
    with mock.patch("versioneer.core.deploy.subprocess.run",
                     return_value=_ok_proc()):
        r = runner.invoke(cli, ["-C", "m", "deploy", "--yes", "--apply"])
    assert r.exit_code == 0, r.output
    assert "applied" in r.output or "pacman" in r.output


def test_cli_deploy_dry_run_apply_noop(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = _make(tmp_path)
    _add_manifest("m", "packages.list", PACKAGES)
    with mock.patch("versioneer.core.deploy.subprocess.run") as mrun:
        r = runner.invoke(cli, ["-C", "m", "deploy", "--dry-run", "--apply"])
        mrun.assert_not_called()
    assert r.exit_code == 0, r.output
    assert "dry-run" in r.output
