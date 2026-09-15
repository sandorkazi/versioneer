import subprocess
from pathlib import Path

from click.testing import CliRunner

from versioneer.cli import cli
from versioneer.core import config as cfg


def _make_config(runner: CliRunner, tmp_path: Path, name="hypr"):
    store = tmp_path / "store"
    r = runner.invoke(
        cli, ["config", "create", "--name", name,
              "--path", str(store), "--upstream", "git@example:x.git"],
    )
    assert r.exit_code == 0, r.output
    return store


def test_target_add_list_remove_file(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    store = _make_config(runner, tmp_path)
    src = tmp_path / "hyprland.conf"
    src.write_text("gaps_in=5\n")
    r = runner.invoke(cli, ["-C", "hypr", "target", "add", str(src)])
    assert r.exit_code == 0, r.output
    assert "tracking" in r.output
    assert "committed" in r.output

    c = cfg.load("hypr")
    assert len(c.targets) == 1
    t = c.targets[0]
    assert t.hash.startswith("sha256:")
    assert t.owner and t.mode

    # artifact staged + committed in store
    log = subprocess.run(["git", "log", "--oneline"], cwd=store,
                         capture_output=True, text=True, check=False)
    assert "track" in log.stdout

    r = runner.invoke(cli, ["-C", "hypr", "target", "list"])
    assert r.exit_code == 0, r.output
    assert "text" in r.output  # kind column (rich table may truncate long paths)

    # duplicate add fails
    r = runner.invoke(cli, ["-C", "hypr", "target", "add", str(src)])
    assert r.exit_code != 0

    # remove keeps working file, git history kept
    r = runner.invoke(cli, ["-C", "hypr", "target", "remove", str(src)])
    assert r.exit_code == 0, r.output
    assert src.exists()
    c = cfg.load("hypr")
    assert c.targets == []
    log = subprocess.run(["git", "log", "--oneline"], cwd=store,
                         capture_output=True, text=True, check=False)
    assert "untrack" in log.stdout
    assert "track" in log.stdout  # history kept


def test_target_add_with_root(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    store = _make_config(runner, tmp_path, name="etc")
    root = tmp_path / "root"
    (root / "nginx").mkdir(parents=True)
    f = root / "nginx" / "nginx.conf"
    f.write_text("user nginx;\n")
    r = runner.invoke(cli, ["-C", "etc", "target", "add", str(f),
                            "--root", str(root), "--kind", "text", "--flex", "fixed"])
    assert r.exit_code == 0, r.output
    c = cfg.load("etc")
    assert c.meta.root == str(root)
    assert c.targets[0].path == "nginx/nginx.conf"
    assert (store / "nginx" / "nginx.conf").exists()


def test_target_add_glob(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make_config(runner, tmp_path, name="snip")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "a.sh").write_text("#!/bin/sh\necho a\n")
    (bindir / "b.sh").write_text("#!/bin/sh\necho b\n")
    (bindir / "skip.log").write_text("noise\n")
    r = runner.invoke(cli, ["-C", "snip", "target", "add", str(bindir),
                            "--glob", str(bindir / "*.sh")])
    assert r.exit_code == 0, r.output
    c = cfg.load("snip")
    assert len(c.targets) == 2
    assert all(t.glob == str(bindir / "*.sh") for t in c.targets)


def test_target_add_dir_with_ignore(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    store = _make_config(runner, tmp_path, name="d")
    top = tmp_path / "mydir"
    (top / "sub").mkdir(parents=True)
    (top / "keep.txt").write_text("keep\n")
    (top / "skip.log").write_text("skip\n")
    (top / "sub" / "inner.txt").write_text("inner\n")
    r = runner.invoke(cli, ["-C", "d", "target", "add", str(top),
                            "--kind", "dir", "--ignore", "*.log"])
    assert r.exit_code == 0, r.output
    c = cfg.load("d")
    assert c.targets[0].kind == "dir"
    assert "*.log" in c.targets[0].ignore
    # find store artifact: flex auto for tmp -> fixed, stripped absolute
    from versioneer.core import monitor as mon
    rel = mon.artifact_rel("", top.resolve(), c.targets[0].flex, "")
    assert (store / rel / "keep.txt").exists()
    assert not (store / rel / "skip.log").exists()


def test_target_add_symlink_preserve(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make_config(runner, tmp_path, name="s")
    real = tmp_path / "real.txt"
    real.write_text("data\n")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    r = runner.invoke(cli, ["-C", "s", "target", "add", str(link),
                            "--symlink", "preserve"])
    assert r.exit_code == 0, r.output
    c = cfg.load("s")
    assert c.targets[0].hash.startswith("symlink:")


def test_target_add_secret_warn_only(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make_config(runner, tmp_path, name="sec")
    f = tmp_path / "tok.conf"
    f.write_text('api_key = "AKIAIOSFODNN7EXAMPLE"\n')
    r = runner.invoke(cli, ["-C", "sec", "target", "add", str(f)])
    assert r.exit_code == 0, r.output
    assert "secret scan" in r.output


def test_target_validation_user_with_root_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make_config(runner, tmp_path, name="v")
    f = tmp_path / "x.conf"
    f.write_text("x\n")
    r = runner.invoke(cli, ["-C", "v", "target", "add", str(f),
                            "--root", "/etc", "--flex", "user"])
    assert r.exit_code != 0
    assert "user" in r.output.lower() or "root" in r.output.lower()
