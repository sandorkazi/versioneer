"""Unit tests for store.py + config validation edge cases."""

from __future__ import annotations

import pytest

from versioneer.core import config as cfg
from versioneer.core import store as store_mod


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    return tmp_path


def test_store_ensure_repo_and_commit(tmp_path):
    store = tmp_path / "store"
    store_mod.ensure_repo(store)
    assert (store / ".git").exists()
    # empty commit -> None
    assert store_mod.add_and_commit(store, [], "empty") is None
    # real commit
    (store / "a.txt").write_text("hello")
    sha = store_mod.add_and_commit(store, ["a.txt"], "add a")
    assert sha and len(sha) == 40
    assert "add a" in " ".join(store_mod.log_lines(store))


def test_store_rm_and_commit(tmp_path):
    store = tmp_path / "store"
    store_mod.ensure_repo(store)
    (store / "a.txt").write_text("hello")
    store_mod.add_and_commit(store, ["a.txt"], "add a")
    sha = store_mod.rm_and_commit(store, ["a.txt"], "rm a")
    assert sha
    assert not (store / "a.txt").exists()


def test_store_log_empty_and_missing(tmp_path):
    assert store_mod.log_lines(tmp_path / "nope-store") == []


def test_config_dir_xdg(tmp_path, monkeypatch):
    monkeypatch.delenv("VERSIONEER_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert str(cfg.config_dir()).endswith("versioneer")


def test_meta_validation_errors():
    m = cfg.Meta(name="bad name!")
    assert any("invalid name" in e for e in m.validate())
    m = cfg.Meta(name="ok", check_interval="bogus")
    assert any("check_interval" in e for e in m.validate())
    m = cfg.Meta(name="ok", check_interval="inotify")
    assert m.validate() == []
    m = cfg.Meta(name="ok", check_interval="10s")
    assert m.validate() == []


def test_target_validation_errors():
    t = cfg.Target(path="", kind="bogus-kind", flex="bogus", symlink="bogus", interest="bogus")
    errs = t.validate()
    assert any("path" in e for e in errs)
    assert any("kind" in e for e in errs)
    assert any("flex" in e for e in errs)
    assert any("symlink" in e for e in errs)
    assert any("interest" in e for e in errs)
    t2 = cfg.Target(path="x", retention={"count": 0})
    assert any("retention.count" in e for e in t2.validate())
    t3 = cfg.Target(path="x", retention={"age": "bogus"})
    assert any("retention.age" in e for e in t3.validate())
    t4 = cfg.Target(path="x", retention={"count": 3, "age": "30d"})
    assert t4.validate() == []


def test_config_duplicate_detected(isolated):
    c = cfg.Config(meta=cfg.Meta(name="dup"))
    c.targets.append(cfg.Target(path="a", abs_path="/a"))
    c.targets.append(cfg.Target(path="a", abs_path="/a2"))
    with pytest.raises(ValueError, match="duplicate"):
        cfg.save(c)


def test_config_load_invalid_meta(isolated):
    p = cfg.config_path("bad")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('[meta]\nname="bad"\nauto_commit=false\nauto_push=true\n')
    with pytest.raises(ValueError, match="auto_commit"):
        cfg.load("bad")


def test_find_target_and_store_dir(isolated):
    c = cfg.Config(meta=cfg.Meta(name="n", storage="~/mystore"))
    c.targets.append(cfg.Target(path="a", abs_path="/abs/a"))
    assert cfg.find_target(c, "a") is not None
    assert cfg.find_target(c, "/abs/a") is not None
    assert cfg.find_target(c, "missing") is None
    assert str(cfg.store_dir(c)).endswith("mystore")
