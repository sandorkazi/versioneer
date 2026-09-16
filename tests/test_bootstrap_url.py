"""ITEM 1: bootstrap <url> new-machine restore (Phase 6 spec).

Spec: `bootstrap <upstream-url> [--to DIR]` = clone store(s) + recreate
TOML(s) + deploy --all, so a fresh machine restores from upstream alone.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from click.testing import CliRunner

from versioneer.cli import cli
from versioneer.core import config as cfg


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("VERSIONEER_STATE_DIR", str(tmp_path / "state"))


def _init_bare(path: Path) -> None:
    subprocess.run(["git", "init", "--bare", str(path)],
                   check=True, capture_output=True)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args],
                   check=True, capture_output=True)


def _make_pushed_config(runner, tmp_path, name, bare, live_text="hello-bootstrap\n"):
    """Create config + add a fixed file under tmp + push to bare."""
    r = runner.invoke(cli, ["config", "create", "--name", name,
                            "--path", str(tmp_path / f"store-{name}"),
                            "--upstream", str(bare)])
    assert r.exit_code == 0, r.output
    live = tmp_path / "live" / f"{name}.conf"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(live_text)
    r = runner.invoke(cli, ["-C", name, "target", "add", str(live)])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli, ["-C", name, "push"])
    assert r.exit_code == 0, r.output
    return live


def test_bootstrap_url_restores_file_after_wipe(tmp_path, monkeypatch):
    """(a) add + push to bare, wipe config+store+live, bootstrap restores file."""
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    bare = tmp_path / "versioneer-demo.git"  # -> cname "demo"
    _init_bare(bare)
    live = _make_pushed_config(runner, tmp_path, "demo", bare)
    # wipe everything except upstream (fresh-VM simulation)
    shutil.rmtree(tmp_path / "store-demo")
    cfg.config_path("demo").unlink()
    live.unlink()
    assert not live.exists()
    r = runner.invoke(cli, ["bootstrap", str(bare),
                            "--to", str(tmp_path / "restored-store"), "--yes"])
    assert r.exit_code == 0, r.output
    assert live.read_text() == "hello-bootstrap\n"
    # TOML recreated with the tracked target, deploy-status written
    conf = cfg.load("demo")
    assert len(conf.targets) == 1
    assert list((tmp_path / "state").glob("demo-deploy-*.json")), r.output


def test_bootstrap_existing_config_pulls_and_deploys(tmp_path, monkeypatch):
    """(b) second bootstrap on existing config pulls upstream advance + deploys."""
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    bare = tmp_path / "versioneer-pull.git"  # -> cname "pull"
    _init_bare(bare)
    live = _make_pushed_config(runner, tmp_path, "pull", bare,
                               live_text="v1\n")
    # advance upstream from a "second machine" clone
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(bare), str(other)],
                   check=True, capture_output=True)
    _git(other, "config", "user.email", "t@t")
    _git(other, "config", "user.name", "t")
    # find the artifact (store rel mirrors the fixed absolute path)
    artifacts = [p for p in other.rglob("pull.conf")
                 if ".git" not in p.parts]
    assert artifacts, [str(p) for p in other.rglob("*")]
    artifacts[0].write_text("v2-from-upstream\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "upstream advance")
    branch = subprocess.run(["git", "-C", str(other),
                             "rev-parse", "--abbrev-ref", "HEAD"],
                            check=True, capture_output=True,
                            text=True).stdout.strip()
    _git(other, "push", "origin", branch)
    # lose the live file, then bootstrap again (config already exists)
    live.unlink()
    r = runner.invoke(cli, ["bootstrap", str(bare), "--yes"])
    assert r.exit_code == 0, r.output
    assert "already exists" in r.output  # existing-config path taken
    assert live.read_text() == "v2-from-upstream\n"


def test_bootstrap_invalid_url_defers_clearly(tmp_path, monkeypatch):
    """(c) invalid upstream defers with a clear offline message, no traceback."""
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    r = runner.invoke(cli, ["bootstrap", "/nonexistent/versioneer-xyz.git"])
    assert r.exit_code != 0
    low = r.output.lower()
    assert any(w in low for w in ("offline", "deferred", "unreachable",
                                  "does not exist", "failed")), r.output
    assert "Traceback" not in r.output


def test_bootstrap_infers_user_targets_without_snapshot(tmp_path, monkeypatch):
    """Fallback: snapshot-less store adopts files as text/user targets under HOME."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    # hand-built upstream with no .versioneer.toml snapshot
    bare = tmp_path / "versioneer-fallback.git"  # -> cname "fallback"
    _init_bare(bare)
    src = tmp_path / "src"
    (src / "notes").mkdir(parents=True)
    (src / "notes" / "hello.txt").write_text("fallback-data\n")
    subprocess.run(["git", "init", str(src)], check=True, capture_output=True)
    _git(src, "config", "user.email", "t@t")
    _git(src, "config", "user.name", "t")
    _git(src, "add", "-A")
    _git(src, "commit", "-m", "seed")
    branch = subprocess.run(["git", "-C", str(src),
                             "rev-parse", "--abbrev-ref", "HEAD"],
                            check=True, capture_output=True,
                            text=True).stdout.strip()
    _git(src, "remote", "add", "origin", str(bare))
    _git(src, "push", "-u", "origin", branch)
    r = runner.invoke(cli, ["bootstrap", str(bare),
                            "--to", str(tmp_path / "fb-store"), "--yes"])
    assert r.exit_code == 0, r.output
    dest = fake_home / "notes" / "hello.txt"
    assert dest.read_text() == "fallback-data\n"
    conf = cfg.load("fallback")
    assert len(conf.targets) == 1 and conf.targets[0].flex == "user"
