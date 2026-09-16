"""ITEM 10: manifest unified diff, dir per-file diff, root read-error, wine --force."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from versioneer.cli import cli
from versioneer.core import config as cfg
from versioneer.core import monitor as _mon
from versioneer.core import permissions as _perm


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))


def _make(tmp_path, name="w10"):
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "create", "--name", name,
                            "--path", str(tmp_path / f"store-{name}"),
                            "--upstream", "git@example:x.git"])
    assert r.exit_code == 0, r.output
    return runner


def _add_manifest_raw(name, fname, content, stale_baseline=True):
    c = cfg.load(name)
    store = cfg.store_dir(c)
    dest = store / fname
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    digest = _mon.hash_target(dest, "text", "preserve", [])
    if stale_baseline:
        digest = "sha256:stale-baseline-for-diff-test"
    c.targets.append(cfg.Target(
        path=fname, abs_path=str(dest), kind="manifest",
        flex="fixed", interest="state", owner="", group="",
        mode="0644", hash=digest))
    cfg.save(c)
    return cfg.load(name).targets[-1], store


def test_manifest_diff_unified(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = _make(tmp_path)
    _add_manifest_raw("w10", "packages.list", "vim\nold-pkg\n")
    regen = "# versioneer packages manifest\n\n[pacman -Qqe]\nvim\nnew-pkg\n"
    with mock.patch.dict(
        "versioneer.core.manifest.GENERATORS",
        {"packages": lambda: ("packages.list", regen, "replay-hint")},
    ):
        r = runner.invoke(cli, ["-C", "w10", "diff"])
    assert r.exit_code == 0, r.output
    assert "-old-pkg" in r.output, r.output
    assert "+new-pkg" in r.output, r.output
    assert "manifest" in r.output.lower()


def test_dir_diff_per_file(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = _make(tmp_path, "d10")
    top = tmp_path / "data10"
    top.mkdir()
    (top / "keep.txt").write_text("v1\n")
    (top / "other.txt").write_text("same\n")
    r = runner.invoke(cli, ["-C", "d10", "target", "add", str(top), "--kind", "dir"])
    assert r.exit_code == 0, r.output
    # modify tracked file + add untracked file
    (top / "keep.txt").write_text("v2\n")
    (top / "brand-new.txt").write_text("n\n")
    r = runner.invoke(cli, ["-C", "d10", "diff"])
    assert r.exit_code == 0, r.output
    assert "keep.txt" in r.output, r.output  # changed file listed
    assert "brand-new.txt" in r.output, r.output  # untracked listed
    assert "changed" in r.output.lower()


def test_root_read_error_mode_000(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = _make(tmp_path, "r10")
    f = tmp_path / "secret10.conf"
    f.write_text("x\n")
    r = runner.invoke(cli, ["-C", "r10", "target", "add", str(f)])
    assert r.exit_code == 0, r.output

    real_stat = Path.stat
    real_lstat = Path.lstat

    def fake_stat(self, *a, **k):
        if str(self).endswith("secret10.conf"):
            m = mock.Mock()
            m.st_mode = 0o100000  # S_IFREG with no read bits (000)
            m.st_uid = 0
            m.st_gid = 0
            return m
        return real_stat(self, *a, **k)

    def fake_lstat(self, *a, **k):
        if str(self).endswith("secret10.conf"):
            m = mock.Mock()
            m.st_mode = 0o100000
            m.st_uid = 0
            m.st_gid = 0
            return m
        return real_lstat(self, *a, **k)

    # root simulation: os.access says readable, but mode 000 must still be read-error
    with mock.patch.object(Path, "stat", fake_stat), mock.patch.object(
        Path, "lstat", fake_lstat
    ):
        with mock.patch.object(os, "access", return_value=True):
            assert _perm.is_readable(f) is False
        out = runner.invoke(cli, ["-C", "r10", "status"]).output
        assert "read-error" in out, out


def test_wine_prefix_requires_force(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = _make(tmp_path, "wine10")
    prefix = tmp_path / "game-wine-pfx"
    (prefix / "drive_c" / "users").mkdir(parents=True)
    (prefix / "drive_c" / "game.cfg").write_text("x\n")
    assert _mon.is_wine_prefix(prefix) is True
    r = runner.invoke(cli, ["-C", "wine10", "target", "add", str(prefix), "--kind", "dir"])
    assert r.exit_code != 0, r.output
    assert "--force" in r.output, r.output
    r2 = runner.invoke(
        cli, ["-C", "wine10", "target", "add", str(prefix), "--kind", "dir", "--force"])
    assert r2.exit_code == 0, r2.output
    c = cfg.load("wine10")
    assert len(c.targets) == 1
    for pat in _mon.WINE_DEFAULT_IGNORES:
        assert pat in c.targets[0].ignore
