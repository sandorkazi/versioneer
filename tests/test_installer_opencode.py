"""Installer OpenCode-plugin step (install.sh flags + opencode-plugin.py merge)."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO / "installer" / "install.sh"
HELPER = REPO / "installer" / "opencode-plugin.py"
PLUGIN_DIR = REPO / "plugins" / "opencode-versioneer"


def _load_helper():
    spec = importlib.util.spec_from_file_location("opencode_plugin_helper", HELPER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_install_sh_has_opencode_flags_and_prompt():
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "--opencode-plugin" in text
    assert "--no-opencode-plugin" in text
    assert "OPENCODE_PLUGIN" in text
    assert "[y/N]" in text  # prompt defaults to N
    assert "opencode-plugin.py" in text
    assert "command -v opencode" in text


def test_helper_missing_file_creates_config(tmp_path):
    helper = _load_helper()
    code, msg = helper.register(tmp_path / "opencode", PLUGIN_DIR)
    assert code == 0, msg
    target = tmp_path / "opencode" / "opencode.jsonc"
    assert target.is_file()
    data = json.loads(target.read_text(encoding="utf-8"))  # fresh file is strict JSON
    assert data["plugins"] == [f"file://{PLUGIN_DIR.resolve()}"]
    assert "added" in msg


def test_helper_existing_without_plugins_key(tmp_path):
    helper = _load_helper()
    cfg = tmp_path / "opencode.jsonc"
    cfg.write_text(
        '{\n  // user comment stays\n  "$schema": "https://opencode.ai/config.json",\n'
        '  "model": "x/y",\n}\n',
        encoding="utf-8",
    )
    code, msg = helper.register(tmp_path, PLUGIN_DIR)
    assert code == 0, msg
    text = cfg.read_text(encoding="utf-8")
    assert "// user comment stays" in text  # comments preserved
    assert f"file://{PLUGIN_DIR.resolve()}" in text
    assert (tmp_path / "opencode.jsonc.bak").is_file()


def test_helper_empty_and_nonempty_arrays(tmp_path):
    helper = _load_helper()
    for initial in ('{\n  "plugins": [],\n}\n', '{\n  "plugins": [\n  ],\n}\n'):
        cfg = tmp_path / "opencode.jsonc"
        cfg.write_text(initial, encoding="utf-8")
        code, _ = helper.register(tmp_path, PLUGIN_DIR)
        assert code == 0
        text = cfg.read_text(encoding="utf-8")
        assert text.count(f"file://{PLUGIN_DIR.resolve()}") == 1

    cfg = tmp_path / "opencode.jsonc"
    cfg.write_text('{\n  "plugins": [\n    "some-other-plugin",\n  ],\n}\n', encoding="utf-8")
    code, _ = helper.register(tmp_path, PLUGIN_DIR)
    assert code == 0
    text = cfg.read_text(encoding="utf-8")
    assert '"some-other-plugin",' in text  # comma kept valid
    assert f'"{f"file://{PLUGIN_DIR.resolve()}"}",' in text


def test_helper_idempotent(tmp_path):
    helper = _load_helper()
    cfg = tmp_path / "opencode.jsonc"
    cfg.write_text('{\n  "plugins": [],\n}\n', encoding="utf-8")
    assert helper.register(tmp_path, PLUGIN_DIR)[0] == 0
    before = cfg.read_bytes()
    code, msg = helper.register(tmp_path, PLUGIN_DIR)
    assert code == 0 and "already present" in msg
    assert cfg.read_bytes() == before


def test_helper_block_comments_need_manual(tmp_path):
    helper = _load_helper()
    cfg = tmp_path / "opencode.jsonc"
    cfg.write_text('/* notes */\n{\n  "plugins": [],\n}\n', encoding="utf-8")
    code, msg = helper.register(tmp_path, PLUGIN_DIR)
    assert code == 2
    assert "MANUAL" in msg
    assert f"file://{PLUGIN_DIR.resolve()}" in msg  # entry echoed for copy-paste


def test_helper_main_exit_codes(tmp_path, capsys):
    helper = _load_helper()
    assert helper.main(["opencode-plugin.py", str(tmp_path / "c"), str(PLUGIN_DIR)]) == 0
    out = capsys.readouterr().out
    assert "added" in out
    assert helper.main(["opencode-plugin.py"]) == 1  # usage error
