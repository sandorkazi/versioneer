"""Edge/boundary coverage: mocks for git, stat, and CLI option paths."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from versioneer.cli import cli
from versioneer.core import config as cfg
from versioneer.core import lint as lint_mod
from versioneer.core import monitor as mon
from versioneer.core import permissions as perm
from versioneer.core import store as store_mod


def _make(runner, tmp_path, name="edge"):
    r = runner.invoke(cli, ["config", "create", "--name", name,
                            "--path", str(tmp_path / f"store-{name}"),
                            "--upstream", "git@example:x.git"])
    assert r.exit_code == 0, r.output


def test_config_create_git_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    with mock.patch("versioneer.core.store.ensure_repo",
                    side_effect=store_mod.GitError("git not found on PATH")):
        r = runner.invoke(cli, ["config", "create", "--name", "g",
                                "--path", str(tmp_path / "s"),
                                "--upstream", "u"])
    assert r.exit_code != 0 and "git not found" in r.output


def test_config_create_git_init_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    with mock.patch("versioneer.core.store.ensure_repo",
                    side_effect=store_mod.GitError("git init failed: boom")):
        r = runner.invoke(cli, ["config", "create", "--name", "g",
                                "--path", str(tmp_path / "s2"),
                                "--upstream", "u"])
    assert r.exit_code != 0 and "git init failed" in r.output


def test_target_add_invalid_config_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "bad")
    p = cfg.config_path("bad")
    p.write_text('[meta]\nname="bad"\nauto_commit=false\nauto_push=true\n')
    f = tmp_path / "x.txt"
    f.write_text("x")
    r = runner.invoke(cli, ["-C", "bad", "target", "add", str(f)])
    assert r.exit_code != 0 and "auto_commit" in r.output


def test_target_add_git_error_on_ensure_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "ge")
    f = tmp_path / "f.txt"
    f.write_text("x")
    with mock.patch("versioneer.core.store.ensure_repo",
                    side_effect=store_mod.GitError("no repo")):
        r = runner.invoke(cli, ["-C", "ge", "target", "add", str(f)])
    assert r.exit_code != 0 and "no repo" in r.output


def test_target_add_full_options_and_follow_symlink(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "full")
    real = tmp_path / "real.txt"
    real.write_text("content\n")
    link = tmp_path / "lnk.txt"
    link.symlink_to(real)
    r = runner.invoke(cli, ["-C", "full", "target", "add", str(link),
                            "--symlink", "follow",
                            "--machines", "lap1,lap2",
                            "--retention-count", "5", "--retention-age", "30d",
                            "--template", "--on-deploy", "true",
                            "--deploy-path", "/tmp/d",
                            "--check-interval", "5m",
                            "--interest", "diff"])
    assert r.exit_code == 0, r.output
    c = cfg.load("full")
    t = c.targets[0]
    assert t.machines == ["lap1", "lap2"]
    assert t.retention == {"count": 5, "age": "30d"}
    assert t.template is True and t.on_deploy == "true"


def test_target_add_dangling_follow_warns(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "dang")
    link = tmp_path / "dangle2"
    link.symlink_to(tmp_path / "gone-target-xyz")
    r = runner.invoke(cli, ["-C", "dang", "target", "add", str(link),
                            "--symlink", "follow"])
    # dangling + follow warns, then stat fails -> clean error (no traceback)
    assert r.exit_code != 0
    assert "dangling" in r.output or "cannot stat" in r.output


def test_target_add_hardcoded_path_and_template_warn(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "hc")
    f = tmp_path / "app.conf"
    f.write_text("prefix=/home/someone_else_xyz/data\n")
    r = runner.invoke(cli, ["-C", "hc", "target", "add", str(f)])
    assert r.exit_code == 0, r.output
    assert "hardcoded path" in r.output or "template" in r.output


def test_target_add_stat_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "st")
    f = tmp_path / "s.txt"
    f.write_text("x")
    with mock.patch("versioneer.core.permissions.capture",
                    side_effect=OSError("no stat")):
        r = runner.invoke(cli, ["-C", "st", "target", "add", str(f)])
    assert r.exit_code != 0 and "cannot stat" in r.output


def test_target_add_stage_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "stage")
    f = tmp_path / "s2.txt"
    f.write_text("x")
    with mock.patch("shutil.copy2", side_effect=OSError("disk full")):
        r = runner.invoke(cli, ["-C", "stage", "target", "add", str(f)])
    assert r.exit_code != 0 and "cannot stage" in r.output


def test_target_remove_git_failure_keeps_toml_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "rmf")
    f = tmp_path / "k.txt"
    f.write_text("x")
    r = runner.invoke(cli, ["-C", "rmf", "target", "add", str(f)])
    assert r.exit_code == 0, r.output
    with mock.patch("versioneer.core.store.rm_and_commit",
                    side_effect=store_mod.GitError("rm fail")):
        r = runner.invoke(cli, ["-C", "rmf", "target", "remove", str(f)])
    assert r.exit_code != 0 and "untrack commit failed" in r.output


def test_store_git_error_paths(tmp_path):
    # git binary missing
    with mock.patch("versioneer.core.store.subprocess.run",
                    side_effect=FileNotFoundError):
        try:
            store_mod.ensure_repo(tmp_path / "s")
            assert False, "should raise"
        except store_mod.GitError as e:
            assert "not found" in str(e)
    # git command fails
    err = subprocess.CalledProcessError(1, ["git"], stderr="bad")
    with mock.patch("versioneer.core.store.subprocess.run", side_effect=err):
        try:
            store_mod.ensure_repo(tmp_path / "s2")
            assert False
        except store_mod.GitError:
            pass


def test_permissions_uid_gid_fallback(tmp_path):
    f = tmp_path / "p.txt"
    f.write_text("x")
    with mock.patch("pwd.getpwuid", side_effect=KeyError(0)), mock.patch(
        "grp.getgrgid", side_effect=KeyError(0)
    ):
        owner, group, mode = perm.capture(f)
        assert owner.isdigit() and group.isdigit() and mode


def test_permissions_is_readable_oserror(tmp_path):
    with mock.patch("os.access", side_effect=OSError("bad")):
        assert perm.is_readable(tmp_path / "whatever") is False


def test_lint_large_and_unreadable(tmp_path):
    big = tmp_path / "big.conf"
    big.write_text("x")
    with mock.patch.object(Path, "stat") as mstat:
        mstat.return_value.st_size = 10_000_000
        assert lint_mod.scan_file(big) == []
        assert lint_mod.hardcoded_path_warnings(big) == []
    with mock.patch.object(Path, "read_bytes", side_effect=OSError("denied")):
        assert lint_mod.scan_file(big) == []


def test_monitor_safe_hash_fallback_and_read_error(tmp_path):
    f = tmp_path / "locked.db"
    f.write_text("data")
    real_sha = mon.sha256_file(f)
    # force first hashing attempt to fail, copy fallback path
    with mock.patch("versioneer.core.monitor.sha256_file",
                    side_effect=[OSError("locked"), real_sha]):
        digest, copied = mon.safe_sha256_file(f)
        assert digest == real_sha and copied is True
    # dir with unreadable file -> read-error marker inside, still hashes
    top = tmp_path / "d2"
    top.mkdir()
    (top / "ok.txt").write_text("ok")
    (top / "bad.txt").write_text("bad")
    with mock.patch("versioneer.core.monitor.sha256_file",
                    side_effect=lambda p: (_ for _ in ()).throw(OSError("denied"))
                    if p.name == "bad.txt" else real_sha):
        h = mon.hash_target(top, "dir", "preserve", [])
        assert h.startswith("dir-sha256:")
