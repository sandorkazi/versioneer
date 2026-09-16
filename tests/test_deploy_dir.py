"""ITEM 9: deploy dir handling — ignore, template, recursive perms, prune."""

from __future__ import annotations

import stat
from pathlib import Path

from click.testing import CliRunner

from versioneer.cli import cli
from versioneer.core import config as cfg


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("VERSIONEER_STATE_DIR", str(tmp_path / "state"))


def _make(runner, tmp_path, name="app"):
    r = runner.invoke(cli, ["config", "create", "--name", name,
                            "--path", str(tmp_path / f"store-{name}"),
                            "--upstream", "git@example:x.git"])
    assert r.exit_code == 0, r.output
    return r


def _mode(p: Path) -> str:
    return format(stat.S_IMODE(p.stat().st_mode), "04o")


def test_dir_merge_respects_ignore(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "d1")
    live = tmp_path / "live1"
    live.mkdir()
    (live / "keep.txt").write_text("keep-v1\n")
    (live / "skip.log").write_text("log-v1\n")
    r = runner.invoke(cli, ["-C", "d1", "target", "add", str(live),
                            "--kind", "dir", "--ignore", "*.log"])
    assert r.exit_code == 0, r.output
    # store must not contain the ignored file
    c = cfg.load("d1")
    store = cfg.store_dir(c)
    # fixed target without root: store rel derived from live abs path
    from versioneer.core import monitor as _mon
    srel = _mon.store_rel_for(c.targets[0].path,
                              _mon.live_abs_path(c.targets[0].path,
                                                 c.targets[0].abs_path, ""),
                              c.targets[0].flex, "")
    assert not (store / srel / "skip.log").exists()
    # inject an ignored file into the store: merge must NOT copy it
    (store / srel / "skip.log").write_text("store-ignored\n")
    # ignored dest extra must be left untouched
    (live / "skip.log").write_text("dest-orig\n")
    (live / "keep.txt").write_text("drift\n")
    r = runner.invoke(cli, ["-C", "d1", "deploy", str(live), "--yes"])
    assert r.exit_code == 0, r.output
    assert (live / "keep.txt").read_text() == "keep-v1\n"
    assert (live / "skip.log").read_text() == "dest-orig\n"
    # ignored file absent in dest stays absent
    (live / "skip.log").unlink()
    r = runner.invoke(cli, ["-C", "d1", "deploy", str(live), "--yes"])
    assert r.exit_code == 0, r.output
    assert not (live / "skip.log").exists()


def test_dir_template_rendered(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "d2")
    live = tmp_path / "live2"
    live.mkdir()
    (live / "cfg.txt").write_text("home={{HOME}} host={{HOST}}\n")
    (live / "bin.dat").write_bytes(b"\x00\x01\x02binary\n")
    r = runner.invoke(cli, ["-C", "d2", "target", "add", str(live),
                            "--kind", "dir", "--template"])
    assert r.exit_code == 0, r.output
    (live / "cfg.txt").write_text("drift\n")
    r = runner.invoke(cli, ["-C", "d2", "deploy", str(live), "--yes"])
    assert r.exit_code == 0, r.output
    text = (live / "cfg.txt").read_text()
    assert "{{HOME}}" not in text and "{{HOST}}" not in text
    assert str(Path.home()) in text
    # binary file passes through unchanged
    assert (live / "bin.dat").read_bytes() == b"\x00\x01\x02binary\n"


def test_dir_perms_recursive(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "d3")
    live = tmp_path / "live3"
    (live / "sub").mkdir(parents=True)
    (live / "top.txt").write_text("top\n")
    (live / "sub" / "inner.txt").write_text("inner\n")
    r = runner.invoke(cli, ["-C", "d3", "target", "add", str(live),
                            "--kind", "dir"])
    assert r.exit_code == 0, r.output
    c = cfg.load("d3")
    c.targets[0].mode = "0600"
    cfg.save(c)
    (live / "top.txt").chmod(0o777)
    (live / "sub" / "inner.txt").chmod(0o777)
    (live / "top.txt").write_text("drift-top\n")
    r = runner.invoke(cli, ["-C", "d3", "deploy", str(live), "--yes"])
    assert r.exit_code == 0, r.output
    assert _mode(live / "top.txt") == "0600"
    assert _mode(live / "sub" / "inner.txt") == "0600"


def test_dir_prune_only_with_flag(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "d4")
    live = tmp_path / "live4"
    live.mkdir()
    (live / "a.txt").write_text("a\n")
    r = runner.invoke(cli, ["-C", "d4", "target", "add", str(live),
                            "--kind", "dir"])
    assert r.exit_code == 0, r.output
    (live / "extra.txt").write_text("extra\n")
    # without --prune the extra file is kept
    r = runner.invoke(cli, ["-C", "d4", "deploy", str(live), "--yes"])
    assert r.exit_code == 0, r.output
    assert (live / "extra.txt").exists()
    # with --prune the extra file is deleted
    r = runner.invoke(cli, ["-C", "d4", "deploy", str(live), "--yes", "--prune"])
    assert r.exit_code == 0, r.output
    assert not (live / "extra.txt").exists()
    assert (live / "a.txt").read_text() == "a\n"
