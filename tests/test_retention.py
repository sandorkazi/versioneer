"""ITEM 7 — per-target retention={count,age} (Q3 decision c).

Policy: warn by default + opt-in prune. History is always kept by
default; this suite asserts we never silently squash (no filter-repo /
rebase) and that plan.json hashes stay valid (history intact).
"""

from __future__ import annotations

import time

from click.testing import CliRunner

from versioneer import cli as cli_mod
from versioneer.core import config as cfg
from versioneer.core import store as store_mod

cli = cli_mod.cli


def _make(runner, tmp_path, name="ret", upstream="https://example.com/x.git"):
    store = tmp_path / f"{name}-store"
    r = runner.invoke(cli, ["config", "create", "--name", name,
                            "--path", str(store), "--upstream", upstream])
    assert r.exit_code == 0, r.output
    return store


# --- age parsing: valid ---

def test_parse_age_valid():
    assert store_mod.parse_retention_age("30d") == 30 * 86400
    assert store_mod.parse_retention_age("5m") == 5 * 60
    assert store_mod.parse_retention_age("3h") == 3 * 3600
    assert store_mod.parse_retention_age("10s") == 10
    assert store_mod.parse_retention_age("2w") == 2 * 604800
    assert store_mod.parse_retention_age("7D") == 7 * 86400  # case-insensitive
    assert store_mod.parse_retention_age("  12h  ") == 12 * 3600
    assert store_mod.parse_retention_age(60) == 60
    assert store_mod.parse_retention_age("3600") == 3600  # bare = seconds


def test_parse_age_invalid():
    assert store_mod.parse_retention_age(None) is None
    assert store_mod.parse_retention_age("") is None
    assert store_mod.parse_retention_age("bogus") is None
    assert store_mod.parse_retention_age("inotify") is None
    assert store_mod.parse_retention_age("0d") is None
    assert store_mod.parse_retention_age(0) is None
    assert store_mod.parse_retention_age(-5) is None
    assert store_mod.parse_retention_age(True) is None
    assert store_mod.parse_retention_age("30x") is None
    assert store_mod.parse_retention_age("d") is None


# --- count exceed warns ---

def test_retention_warning_count_exceed():
    w = store_mod.retention_warning(5, {"count": 3})
    assert w is not None and "retention.count=3" in w
    assert "history kept" in w
    assert store_mod.retention_warning(2, {"count": 3}) is None
    assert store_mod.retention_warning(3, {"count": 3}) is None  # at limit: quiet
    assert store_mod.retention_warning(5, {}) is None
    assert store_mod.retention_warning(5, {"count": 0}) is None  # invalid config ignored


def test_retention_warning_age():
    now = time.time()
    old = int(now - 40 * 86400)  # 40d old
    w = store_mod.retention_warning(1, {"age": "30d"}, old, now)
    assert w is not None and "retention.age=30d" in w
    fresh = int(now - 5 * 86400)
    assert store_mod.retention_warning(1, {"age": "30d"}, fresh, now) is None
    # no timestamp -> no age warn (unknown age, warn-only callers skip)
    assert store_mod.retention_warning(1, {"age": "30d"}, None, now) is None
    # combined count + age
    w2 = store_mod.retention_warning(9, {"count": 3, "age": "30d"}, old, now)
    assert "retention.count=3" in w2 and "retention.age=30d" in w2


# --- prune guidance: never silent squash, protect plan.json ---

def test_prune_guidance_mentions_manual_only():
    g = store_mod.prune_guidance("a.bin", {"count": 3, "age": "30d"})
    assert "git log" in g
    assert "git lfs prune" in g
    assert "--prune-retention" in g
    assert "filter-repo" in g and "manual-only" in g
    assert "plan.json" in g
    for forbidden in ("squash", "rebase", "history rewrite"):
        assert forbidden not in g.lower()


def test_prune_retention_warn_only_without_lfs(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    out = store_mod.prune_retention(tmp_path, "a.bin", {"count": 3})
    assert "history kept" in out


def test_check_retention_real_repo(tmp_path):
    store = tmp_path / "store"
    store_mod.ensure_repo(store)
    (store / "a.bin").write_text("v1")
    store_mod.add_and_commit(store, ["a.bin"], "c1")
    (store / "a.bin").write_text("v2")
    store_mod.add_and_commit(store, ["a.bin"], "c2")
    warns = store_mod.check_retention(store, "a.bin", {"count": 1})
    assert warns and "retention.count=1" in warns[0]
    assert store_mod.check_retention(store, "a.bin", {"count": 99}) == []
    assert store_mod.check_retention(store, "a.bin", {}) == []


# --- config validation ---

def test_config_retention_age_validation():
    assert cfg.Target(path="x", retention={"count": 3, "age": "30d"}).validate() == []
    assert cfg.Target(path="x", retention={"age": "5m"}).validate() == []
    assert cfg.Target(path="x", retention={"age": "3h"}).validate() == []
    assert cfg.Target(path="x", retention={"age": "2w"}).validate() == []
    bad = cfg.Target(path="x", retention={"age": "bogus"}).validate()
    assert any("retention.age" in e for e in bad)


# --- CLI wiring: warn + opt-in prune, history kept ---

def test_commit_warns_and_keeps_history(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "rh")
    f = tmp_path / "save.bin"
    f.write_bytes(b"\x00" * 64)
    r = runner.invoke(cli, ["-C", "rh", "target", "add", str(f),
                            "--kind", "binary", "--retention-count", "1"])
    assert r.exit_code == 0, r.output
    f.write_bytes(b"\x01" * 64)
    r = runner.invoke(cli, ["-C", "rh", "commit", str(f), "-m", "v2"])
    assert r.exit_code == 0, r.output
    assert "retention" in r.output  # warn by default
    assert "lfs prune" in r.output  # documented hint
    # history kept: both commits visible (no squash)
    log = runner.invoke(cli, ["-C", "rh", "log", str(f)]).output
    assert "track" in log and "v2" in log


def test_commit_prune_retention_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "rp")
    f = tmp_path / "p.bin"
    f.write_bytes(b"\x00" * 32)
    runner.invoke(cli, ["-C", "rp", "target", "add", str(f),
                        "--kind", "binary", "--retention-count", "1"])
    f.write_bytes(b"\x01" * 32)
    r = runner.invoke(cli, ["-C", "rp", "commit", str(f), "-m", "v2",
                            "--prune-retention"])
    assert r.exit_code == 0, r.output
    assert "retention" in r.output
    # opt-in prune ran (or warn-only without LFS) but history still intact
    log = runner.invoke(cli, ["-C", "rp", "log", str(f)]).output
    assert "track" in log and "v2" in log
