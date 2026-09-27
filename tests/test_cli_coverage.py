"""CLI error-path + functional coverage: Phase 0/1 exit criteria."""

from __future__ import annotations

from click.testing import CliRunner

from versioneer.cli import cli
from versioneer.core import config as cfg


def _make(runner, tmp_path, name="hypr"):
    r = runner.invoke(
        cli, ["config", "create", "--name", name,
              "--path", str(tmp_path / f"store-{name}"),
              "--upstream", "git@example:x.git"])
    assert r.exit_code == 0, r.output
    return r


def test_config_create_duplicate_and_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "hypr")
    r = runner.invoke(cli, ["config", "create", "--name", "hypr",
                            "--path", str(tmp_path / "other"),
                            "--upstream", "git@example:x.git"])
    assert r.exit_code != 0 and "already exists" in r.output
    r = runner.invoke(cli, ["config", "create", "--name", "s",
                            "--path", str(tmp_path / "s"),
                            "--upstream", "u", "--auto-push"])
    assert r.exit_code != 0 and "auto_commit" in r.output


def test_config_show_remove_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    # missing -C
    r = runner.invoke(cli, ["config", "show"])
    assert r.exit_code != 0
    r = runner.invoke(cli, ["config", "remove"])
    assert r.exit_code != 0
    # not found
    r = runner.invoke(cli, ["-C", "nope", "config", "show"])
    assert r.exit_code != 0 and "not found" in r.output
    r = runner.invoke(cli, ["-C", "nope", "config", "remove"])
    assert r.exit_code != 0


def test_config_list_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg-empty"))
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "list"])
    assert r.exit_code == 0 and "no configs" in r.output


def test_target_add_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "e")
    # missing config
    r = runner.invoke(cli, ["-C", "nope", "target", "add", "/tmp/x"])
    assert r.exit_code != 0 and "not found" in r.output
    # missing path
    r = runner.invoke(cli, ["-C", "e", "target", "add", str(tmp_path / "gone.txt")])
    assert r.exit_code != 0 and "does not exist" in r.output
    # glob matched nothing
    r = runner.invoke(cli, ["-C", "e", "target", "add", str(tmp_path),
                            "--glob", str(tmp_path / "*.nomatch_xyz")])
    assert r.exit_code != 0 and "matched nothing" in r.output
    # kind=manifest rejected
    f = tmp_path / "f.txt"
    f.write_text("x")
    r = runner.invoke(cli, ["-C", "e", "target", "add", str(f), "--kind", "manifest"])
    assert r.exit_code != 0 and "manifest" in r.output
    # multi-root mismatch
    r1 = tmp_path / "root1"
    r1.mkdir()
    (r1 / "a.txt").write_text("a")
    r = runner.invoke(cli, ["-C", "e", "target", "add", str(r1 / "a.txt"),
                            "--root", str(r1)])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli, ["-C", "e", "target", "add", str(f),
                            "--root", str(tmp_path)])
    assert r.exit_code != 0 and "multi-root" in r.output


def test_target_add_binary_large_and_dangling_warn(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "b")
    big = tmp_path / "big.bin"
    big.write_bytes(b"\x00" * 1024)  # binary detect via NUL
    r = runner.invoke(cli, ["-C", "b", "target", "add", str(big), "--kind", "binary"])
    assert r.exit_code == 0, r.output  # tracks without warnings at this size
    c = cfg.load("b")
    assert c.targets[0].kind == "binary"

    # dangling symlink preserve warns but tracks
    link = tmp_path / "dangle"
    link.symlink_to(tmp_path / "target-gone-xyz")
    r = runner.invoke(cli, ["-C", "b", "target", "add", str(link)])
    assert r.exit_code == 0, r.output
    assert "dangling" in r.output


def test_target_list_empty_and_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "l")
    r = runner.invoke(cli, ["-C", "l", "target", "list"])
    assert r.exit_code == 0 and "no targets" in r.output
    r = runner.invoke(cli, ["-C", "nope", "target", "list"])
    assert r.exit_code != 0


def test_target_remove_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "r")
    r = runner.invoke(cli, ["-C", "r", "target", "remove", "/tmp/not-tracked-xyz"])
    assert r.exit_code != 0 and "not tracked" in r.output
    r = runner.invoke(cli, ["-C", "nope", "target", "remove", "/tmp/x"])
    assert r.exit_code != 0


def test_implemented_commands_no_stub(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("VERSIONEER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("VERSIONEER_WATCH_TIMEOUT", "1")
    runner = CliRunner()
    _make(runner, tmp_path, "hypr")
    # deploy (empty config) succeeds without stub text
    r = runner.invoke(cli, ["-C", "hypr", "deploy", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "not yet implemented" not in r.output
    # service install writes the user unit (real implementation)
    r = runner.invoke(cli, ["service", "install"])
    assert r.exit_code == 0, r.output
    assert "not yet implemented" not in r.output
    # watch with timeout detects no changes and exits 0
    r = runner.invoke(cli, ["-C", "hypr", "watch", str(tmp_path),
                            "--timeout", "1"])
    assert r.exit_code == 0, r.output
    assert "not yet implemented" not in r.output
    # manifest requires a flag (usage error, not stub)
    r = runner.invoke(cli, ["-C", "hypr", "manifest"])
    assert r.exit_code != 0
    assert "not yet implemented" not in r.output
    # doctor on empty config succeeds
    r = runner.invoke(cli, ["-C", "hypr", "doctor"])
    assert r.exit_code == 0, r.output
    assert "not yet implemented" not in r.output
    # bootstrap --all deploys empty config successfully
    r = runner.invoke(cli, ["bootstrap", "--all"])
    assert r.exit_code == 0, r.output
    assert "not yet implemented" not in r.output
    # service enable/disable need systemctl: either succeed or clean error,
    # but never the old stub
    for args in (["service", "enable"], ["service", "disable"]):
        r = runner.invoke(cli, args)
        assert "not yet implemented" not in r.output, args
