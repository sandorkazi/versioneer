from click.testing import CliRunner

from versioneer.cli import cli


def test_help_lists_commands():
    r = CliRunner().invoke(cli, ["--help"])
    assert r.exit_code == 0
    for cmd in ("config", "target", "status", "deploy", "doctor"):
        assert cmd in r.output


def test_config_flag_help():
    r = CliRunner().invoke(cli, ["-C", "foo", "--help"])
    assert r.exit_code == 0


def test_deploy_no_longer_stub(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("VERSIONEER_STATE_DIR", str(tmp_path / "state"))
    from click.testing import CliRunner
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "create", "--name", "hypr",
                            "--path", str(tmp_path / "store-hypr"),
                            "--upstream", "git@example:x.git"])
    assert r.exit_code == 0, r.output
    # empty config: deploy is a no-op success (not a Phase 5 stub)
    r = runner.invoke(cli, ["-C", "hypr", "deploy", "--dry-run"])
    assert r.exit_code == 0
    assert "Phase 5" not in r.output
    assert "no targets" in r.output


def test_config_create_list_show_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    store = str(tmp_path / "store")
    r = runner.invoke(cli, ["config", "create", "--name", "hypr",
                            "--path", store,
                            "--upstream", "git@example:x.git"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli, ["config", "list"])
    assert "hypr" in r.output
    r = runner.invoke(cli, ["-C", "hypr", "config", "show"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli, ["-C", "hypr", "config", "remove"])
    assert r.exit_code == 0, r.output
