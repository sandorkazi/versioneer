"""Phases 3/5/6/8/9: deploy, service, bootstrap, manifest, doctor, daemon."""

from __future__ import annotations

import json
import os
from pathlib import Path

from click.testing import CliRunner

from versioneer.cli import cli
from versioneer.core import config as cfg


def _make(runner, tmp_path, name="app"):
    r = runner.invoke(cli, ["config", "create", "--name", name,
                            "--path", str(tmp_path / f"store-{name}"),
                            "--upstream", "git@example:x.git"])
    assert r.exit_code == 0, r.output
    return r


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("VERSIONEER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("NOTIFY_DEBUG", "1")


def test_deploy_roundtrip_with_backup_and_status(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "hypr")
    f = tmp_path / "hyprland.conf"
    f.write_text("v1\n")
    assert runner.invoke(cli, ["-C", "hypr", "target", "add", str(f)]).exit_code == 0
    # dry-run on clean: no writes, no status file
    r = runner.invoke(cli, ["-C", "hypr", "deploy", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "dry-run" in r.output
    assert list((tmp_path / "state").glob("hypr-deploy-*.json")) == []
    # drift, then --yes deploy restores baseline with .bak + status file
    f.write_text("v2-drift\n")
    assert "modified" in runner.invoke(cli, ["-C", "hypr", "status"]).output
    r = runner.invoke(cli, ["-C", "hypr", "deploy", "--yes"])
    assert r.exit_code == 0, r.output
    assert f.read_text() == "v1\n"
    assert (f.parent / (f.name + ".bak")).exists()
    states = list((tmp_path / "state").glob("hypr-deploy-*.json"))
    assert len(states) == 1
    payload = json.loads(states[0].read_text())
    assert payload["results"][0]["status"] == "ok"
    assert (tmp_path / "state" / "hypr-latest.json").exists()
    # second deploy: already in sync
    r = runner.invoke(cli, ["-C", "hypr", "deploy", "--yes"])
    assert r.exit_code == 0 and "already in sync" in r.output


def test_deploy_plan_guard_and_template_and_machines(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "m")
    f = tmp_path / "a.conf"
    f.write_text("hello\n")
    assert runner.invoke(cli, ["-C", "m", "target", "add", str(f),
                               "--machines", "otherhost",
                               "--template"]).exit_code == 0
    # wrong host -> skipped
    r = runner.invoke(cli, ["-C", "m", "deploy", "--dry-run",
                            "--host", "myhost"])
    assert r.exit_code == 0 and "wrong host" in r.output
    # force-host overrides
    r = runner.invoke(cli, ["-C", "m", "deploy", "--dry-run",
                            "--host", "myhost", "--force-host"])
    assert r.exit_code == 0 and "wrong host" not in r.output
    # plan-out then interim edit -> plan error
    plan = str(tmp_path / "plan.json")
    assert runner.invoke(cli, ["-C", "m", "deploy", "--plan-out", plan,
                               "--force-host", "--host", "myhost"]).exit_code == 0
    assert Path(plan).exists()
    f.write_text("interim\n")
    r = runner.invoke(cli, ["-C", "m", "deploy", "--plan", plan, "--yes",
                            "--force-host", "--host", "myhost"])
    assert r.exit_code != 0 and "interim edit" in r.output


def test_deploy_flexi_symlink_dir_template(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "fx")
    # flexi without --to is a per-target error, not whole-run abort
    src = tmp_path / "note.txt"
    src.write_text("data\n")
    assert runner.invoke(cli, ["-C", "fx", "target", "add", str(src),
                               "--flex", "flexi"]).exit_code == 0
    c = cfg.load("fx")
    c.targets[0].flex = "flexi"
    c.targets[0].deploy_path = ""
    cfg.save(c)
    r = runner.invoke(cli, ["-C", "fx", "deploy", "--yes"])
    assert r.exit_code != 0 and "--to" in r.output
    # with --to it deploys
    dest = tmp_path / "out" / "note.txt"
    r = runner.invoke(cli, ["-C", "fx", "deploy", "--yes", "--to", str(dest)])
    assert r.exit_code == 0, r.output
    assert dest.read_text() == "data\n"
    # symlink preserve round-trip
    real = tmp_path / "real.txt"
    real.write_text("x\n")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    assert runner.invoke(cli, ["-C", "fx", "target", "add", str(link),
                               "--symlink", "preserve"]).exit_code == 0
    link.unlink()
    r = runner.invoke(cli, ["-C", "fx", "deploy", str(link), "--yes"])
    assert r.exit_code == 0, r.output
    assert link.is_symlink()
    # template substitution on deploy
    t = tmp_path / "tpl.conf"
    t.write_text("home={{HOME}} host={{HOST}}\n")
    assert runner.invoke(cli, ["-C", "fx", "target", "add", str(t),
                               "--template"]).exit_code == 0
    t.write_text("drift\n")
    r = runner.invoke(cli, ["-C", "fx", "deploy", str(t), "--yes"])
    assert r.exit_code == 0, r.output
    assert "{{HOME}}" not in t.read_text() and str(Path.home()) in t.read_text()


def test_service_install_check_and_manifest_doctor(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    runner = CliRunner()
    _make(runner, tmp_path, "hypr")
    r = runner.invoke(cli, ["service", "install"])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "xdg" / "systemd" / "user" /
            "versioneer-user.service").exists()
    r = runner.invoke(cli, ["service", "check", "--all"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli, ["-C", "hypr", "manifest", "--env"])
    assert r.exit_code == 0, r.output
    assert "env.json" in r.output
    r = runner.invoke(cli, ["-C", "hypr", "doctor"])
    assert r.exit_code == 0, r.output
    assert "doctor:" in r.output
    r = runner.invoke(cli, ["-C", "hypr", "doctor", "--secrets"])
    assert r.exit_code == 0, r.output


def test_bootstrap_all_and_watch_timeout(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "b1")
    r = runner.invoke(cli, ["bootstrap", "--all"])
    assert r.exit_code == 0, r.output
    watchdir = tmp_path / "wd"
    watchdir.mkdir()
    (watchdir / "a.txt").write_text("a")
    monkeypatch.setenv("VERSIONEER_WATCH_TIMEOUT", "1")
    r = runner.invoke(cli, ["-C", "b1", "watch", str(watchdir)])
    assert r.exit_code == 0, r.output
    assert "no changes" in r.output


def test_daemon_zero_writes_by_default(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("NOTIFY_DEBUG", "1")
    runner = CliRunner()
    _make(runner, tmp_path, "d")
    f = tmp_path / "x.conf"
    f.write_text("v1\n")
    assert runner.invoke(cli, ["-C", "d", "target", "add", str(f)]).exit_code == 0
    f.write_text("v2\n")
    from versioneer.core import daemon as _daemon
    from versioneer.core import monitor as _mon
    from versioneer.core import store as _store

    c = cfg.load("d")
    store = cfg.store_dir(c)
    rel = _mon.store_rel_for(c.targets[0].path,
                             _mon.live_abs_path(c.targets[0].path,
                                                c.targets[0].abs_path, ""),
                             c.targets[0].flex, "").as_posix()
    n_before = _store.count_artifact_commits(store, rel)
    res = _daemon.check_once("d")
    assert res["drift"] and res["committed"] == []
    # default daemon never commits; notify attempted (stdout fallback)
    assert res["notified"] is True
    # auto_commit opt-in commits silently
    c.meta.auto_commit = True
    c.meta.notify = False
    cfg.save(c)
    res2 = _daemon.check_once("d")
    assert res2["committed"] != []
    assert os.environ.get("NOTIFY_DEBUG") == "1"
    assert _store.count_artifact_commits(store, rel) >= n_before
