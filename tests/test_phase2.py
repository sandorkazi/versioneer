"""Phase 2: storage (git+LFS) + review (status/diff/log) + save (commit/push/pull)."""

from __future__ import annotations

import subprocess
from unittest import mock

from click.testing import CliRunner

from versioneer.cli import cli
from versioneer.core import config as cfg
from versioneer.core import store as store_mod


def _make(runner, tmp_path, name="p2", upstream="git@example:x.git"):
    r = runner.invoke(cli, ["config", "create", "--name", name,
                            "--path", str(tmp_path / f"store-{name}"),
                            "--upstream", upstream])
    assert r.exit_code == 0, r.output
    return r


def test_config_create_sets_upstream_and_init_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    store = tmp_path / "store-x"
    _make(runner, tmp_path, "x")
    assert (store.parent / "store-x" / ".git").exists() or (tmp_path / "store-x" / ".git").exists()
    c = cfg.load("x")
    store_dir = cfg.store_dir(c)
    assert store_mod.get_upstream(store_dir) == "git@example:x.git"
    assert store_mod.log_lines(store_dir, 1)  # init commit exists


def test_roundtrip_add_modify_status_diff_commit_log(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "rt")
    f = tmp_path / "app.conf"
    f.write_text("v1\n")
    assert runner.invoke(cli, ["-C", "rt", "target", "add", str(f)]).exit_code == 0
    assert "clean" in runner.invoke(cli, ["-C", "rt", "status"]).output
    f.write_text("v2\n")
    out = runner.invoke(cli, ["-C", "rt", "status"]).output
    assert "modified" in out
    dout = runner.invoke(cli, ["-C", "rt", "diff"]).output
    assert "-v1" in dout and "+v2" in dout
    r = runner.invoke(cli, ["-C", "rt", "commit", "--all", "-m", "bump"])
    assert r.exit_code == 0, r.output
    assert "committed" in r.output
    assert "clean" in runner.invoke(cli, ["-C", "rt", "status"]).output
    assert "bump" in runner.invoke(cli, ["-C", "rt", "log"]).output
    assert "bump" in runner.invoke(cli, ["-C", "rt", "log", str(f)]).output


def test_commit_selection_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "sel")
    r = runner.invoke(cli, ["-C", "sel", "commit", "-m", "x"])
    assert r.exit_code == 0 and "no targets" in r.output  # empty config: no-op
    f = tmp_path / "a.txt"
    f.write_text("x\n")
    runner.invoke(cli, ["-C", "sel", "target", "add", str(f)])
    r = runner.invoke(cli, ["-C", "sel", "commit", "-m", "x"])
    assert r.exit_code != 0 and "nothing selected" in r.output
    r = runner.invoke(cli, ["-C", "sel", "commit", "/not/tracked", "-m", "x"])
    assert r.exit_code != 0 and "not tracked" in r.output
    # clean --all is a no-op success
    r = runner.invoke(cli, ["-C", "sel", "commit", "--all", "-m", "x"])
    assert r.exit_code == 0 and "nothing to commit" in r.output


def test_perm_drift_then_baseline_update(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "perm")
    f = tmp_path / "p.conf"
    f.write_text("x\n")
    runner.invoke(cli, ["-C", "perm", "target", "add", str(f)])
    f.chmod(0o600)
    assert "perm-drift" in runner.invoke(cli, ["-C", "perm", "status"]).output
    r = runner.invoke(cli, ["-C", "perm", "commit", str(f), "-m", "perms"])
    assert r.exit_code == 0, r.output
    assert "updated baselines" in r.output or "committed" in r.output
    assert "clean" in runner.invoke(cli, ["-C", "perm", "status"]).output


def test_missing_and_read_error(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "mr")
    f = tmp_path / "gone.conf"
    f.write_text("x\n")
    runner.invoke(cli, ["-C", "mr", "target", "add", str(f)])
    f.unlink()
    assert "missing" in runner.invoke(cli, ["-C", "mr", "status"]).output
    r = runner.invoke(cli, ["-C", "mr", "commit", str(f), "-m", "x"])
    assert r.exit_code == 0 and "missing" in r.output
    # read-error via mocked unreadable
    f.write_text("x\n")
    runner.invoke(cli, ["-C", "mr", "target", "add", str(tmp_path / "other.conf")] if False else ["-C", "mr", "status"])
    g = tmp_path / "r.conf"
    g.write_text("y\n")
    runner.invoke(cli, ["-C", "mr", "target", "add", str(g)])
    with mock.patch("versioneer.core.permissions.is_readable", return_value=False):
        assert "read-error" in runner.invoke(cli, ["-C", "mr", "status"]).output


def test_dir_untracked_and_diff_stat(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "dir")
    top = tmp_path / "d"
    top.mkdir()
    (top / "keep.txt").write_text("k\n")
    runner.invoke(cli, ["-C", "dir", "target", "add", str(top), "--kind", "dir"])
    (top / "new.txt").write_text("n\n")
    assert "untracked" in runner.invoke(cli, ["-C", "dir", "status"]).output
    assert "untracked" in runner.invoke(cli, ["-C", "dir", "diff"]).output
    assert runner.invoke(cli, ["-C", "dir", "commit", "--all", "-m", "add"]).exit_code == 0


def test_binary_diff_and_retention_warn(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "bin")
    b = tmp_path / "b.bin"
    b.write_bytes(b"\x00" * 64)
    r = runner.invoke(cli, ["-C", "bin", "target", "add", str(b), "--kind", "binary",
                            "--retention-count", "1"])
    assert r.exit_code == 0 and "git-lfs" in r.output  # warn-only without LFS binary
    b.write_bytes(b"\x00" * 128)
    assert "binary" in runner.invoke(cli, ["-C", "bin", "diff"]).output
    r = runner.invoke(cli, ["-C", "bin", "commit", str(b), "-m", "b2"])
    assert r.exit_code == 0 and "retention" in r.output


def test_status_host_filter(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "host")
    f = tmp_path / "m.conf"
    f.write_text("m\n")
    runner.invoke(cli, ["-C", "host", "target", "add", str(f), "--machines", "otherhost"])
    assert "wrong host" in runner.invoke(cli, ["-C", "host", "status", "--host", "myhost"]).output


def test_push_pull_offline_defer_and_local_bare_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "off")
    r = runner.invoke(cli, ["-C", "off", "push"])
    assert r.exit_code != 0 and "offline" in r.output
    r = runner.invoke(cli, ["-C", "off", "pull"])
    assert r.exit_code != 0 and "offline" in r.output
    # local bare remote succeeds
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    _make(runner, tmp_path, "live", upstream=str(bare))
    f = tmp_path / "a.conf"
    f.write_text("v1\n")
    runner.invoke(cli, ["-C", "live", "target", "add", str(f)])
    assert runner.invoke(cli, ["-C", "live", "push"]).exit_code == 0
    assert runner.invoke(cli, ["-C", "live", "pull"]).exit_code == 0


def test_store_clone_and_upstream_helpers(tmp_path):
    bare = tmp_path / "src.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    dest = tmp_path / "clone"
    store_mod.clone(str(bare), dest)
    assert (dest / ".git").exists()
    store_mod.set_upstream(dest, str(bare))
    assert store_mod.get_upstream(dest) == str(bare)
    with mock.patch("versioneer.core.store._run_git", side_effect=store_mod.GitError("boom")):
        assert store_mod.get_upstream(dest) == ""
        assert store_mod.log_lines(dest) == []
        assert store_mod.count_artifact_commits(dest, "x") == 0
    # clone into non-empty dest fails
    try:
        store_mod.clone(str(bare), dest)
        assert False, "should raise"
    except store_mod.GitError as e:
        assert "not empty" in str(e)


def test_lfs_attributes_written_when_present(tmp_path, monkeypatch):
    store = tmp_path / "s"
    store.mkdir()
    with mock.patch("shutil.which", return_value="/usr/bin/git-lfs"), mock.patch(
        "versioneer.core.store._run_git", return_value=""
    ):
        assert store_mod.ensure_lfs(store, ["a.bin"]) is None
        assert "a.bin" in (store / ".gitattributes").read_text()
    assert store_mod.lfs_available() in (True, False)
    assert store_mod.retention_warning(5, {"count": 3}) is not None
    assert store_mod.retention_warning(2, {"count": 3}) is None
