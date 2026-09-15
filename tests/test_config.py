import pytest

from versioneer.core import config as cfg


@pytest.fixture
def isolated_config_dir(tmp_path, monkeypatch):
    d = tmp_path / "versioneer"
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(d))
    return d


def test_save_load_roundtrip(isolated_config_dir):
    c = cfg.Config(meta=cfg.Meta(name="hypr", upstream="git@example:x.git",
                                 storage="~/store/hypr"))
    path = cfg.save(c)
    assert path.exists()
    loaded = cfg.load("hypr")
    assert loaded.meta.name == "hypr"
    assert loaded.meta.check_interval == "3h"
    assert loaded.meta.large_file_warn_mb == 10


def test_auto_push_requires_auto_commit(isolated_config_dir):
    c = cfg.Config(meta=cfg.Meta(name="x", auto_push=True, auto_commit=False))
    with pytest.raises(ValueError, match="auto_commit"):
        cfg.save(c)


def test_list_configs(isolated_config_dir):
    assert cfg.list_configs() == []
    cfg.save(cfg.Config(meta=cfg.Meta(name="b")))
    cfg.save(cfg.Config(meta=cfg.Meta(name="a")))
    assert cfg.list_configs() == ["a", "b"]
