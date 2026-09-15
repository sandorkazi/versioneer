"""Final push: large-file warn, validation, save/commit failures, remove fallback."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from versioneer.cli import cli
from versioneer.core import config as cfg
from versioneer.core import store as store_mod


def _make(runner, tmp_path, name):
    r = runner.invoke(cli, ["config", "create", "--name", name,
                            "--path", str(tmp_path / f"store-{name}"),
                            "--upstream", "git@example:x.git"])
    assert r.exit_code == 0, r.output


def test_large_file_warn_triggered(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "lg")
    c = cfg.load("lg")
    c.meta.large_file_warn_mb = 1  # 1 MB threshold (0 is falsy: `or 10` fallback)
    cfg.save(c)
    big = tmp_path / "s.bin"
    big.write_bytes(b"\x00" * (2 * 1024 * 1024))  # 2 MB binary
    r = runner.invoke(cli, ["-C", "lg", "target", "add", str(big), "--kind", "binary"])
    assert r.exit_code == 0, r.output
    assert "large file" in r.output


def test_binary_stat_oserror_branch(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "bstat")
    f = tmp_path / "b2.bin"
    f.write_bytes(b"\x00abc")
    f.stat()
    calls = {"n": 0}
    orig_stat = Path.stat

    def flaky_stat(self, *a, **k):
        # permissions.capture succeeds on first stat; size check raises OSError
        if self == f:
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("no stat")
        return orig_stat(self, *a, **k)

    with mock.patch.object(Path, "stat", flaky_stat):
        r = runner.invoke(cli, ["-C", "bstat", "target", "add", str(f), "--kind", "binary"])
    # size check falls back to 0 -> no warn, but tracking still succeeds
    assert r.exit_code == 0, r.output


def test_target_add_validation_error_retention(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "val")
    f = tmp_path / "v.txt"
    f.write_text("x")
    r = runner.invoke(cli, ["-C", "val", "target", "add", str(f),
                            "--retention-count", "0"])
    assert r.exit_code != 0 and "retention" in r.output.lower()


def test_target_add_save_and_commit_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "fail")
    f = tmp_path / "a.txt"
    f.write_text("x")
    with mock.patch("versioneer.core.config.save", side_effect=ValueError("bad toml")):
        r = runner.invoke(cli, ["-C", "fail", "target", "add", str(f)])
    assert r.exit_code != 0 and "bad toml" in r.output

    with mock.patch("versioneer.core.store.add_and_commit",
                    side_effect=store_mod.GitError("commit boom")):
        r = runner.invoke(cli, ["-C", "fail", "target", "add", str(f)])
    assert r.exit_code != 0 and "baseline commit failed" in r.output


def test_target_remove_resolve_fallback_and_root_rel(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "rmx")
    root = tmp_path / "rr"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "k.conf").write_text("k")
    r = runner.invoke(cli, ["-C", "rmx", "target", "add", str(root / "sub" / "k.conf"),
                            "--root", str(root)])
    assert r.exit_code == 0, r.output
    # remove via relative path (exercises resolve_input fallback + root rel branch)
    r = runner.invoke(cli, ["-C", "rmx", "target", "remove", "sub/k.conf"])
    assert r.exit_code == 0, r.output
    # invalid root-relative path -> ValueError inside fallback -> not tracked
    r = runner.invoke(cli, ["-C", "rmx", "target", "remove", "nope/k.conf"])
    assert r.exit_code != 0


def test_store_branches(tmp_path):
    # rm with nothing staged -> returns None (covers status-empty branch)
    store = tmp_path / "store"
    store_mod.ensure_repo(store)
    (store / "z.txt").write_text("z")
    store_mod.add_and_commit(store, ["z.txt"], "add z")
    # second rm of same path would fail; instead call add with no changes
    assert store_mod.add_and_commit(store, [], "empty2") is None
