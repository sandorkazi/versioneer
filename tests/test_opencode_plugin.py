"""OpenCode plugin scaffold integrity (plugins/opencode-versioneer)."""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "opencode-versioneer"

EXPECTED_FILES = (
    "package.json",
    "index.ts",
    "versioneer.ts",
    "README.md",
    "opencode.example.jsonc",
    "skills/versioneer/SKILL.md",
    "commands/versioneer-status.md",
    "commands/versioneer-commit.md",
    "commands/versioneer-deploy.md",
)

READONLY_TOOLS = (
    "status",
    "diff",
    "log",
    "config_list",
    "config_show",
    "target_list",
    "doctor",
    "service_check",
    "deploy_preview",
    "bootstrap_preview",
)

MUTATING_TOOLS = (
    "commit",
    "push",
    "pull",
    "deploy_apply",
    "target_add",
    "target_remove",
)


def test_scaffold_files_exist():
    for rel in EXPECTED_FILES:
        assert (PLUGIN / rel).is_file(), f"missing plugins/opencode-versioneer/{rel}"


def test_package_json_valid():
    data = json.loads((PLUGIN / "package.json").read_text(encoding="utf-8"))
    assert data["name"] == "opencode-versioneer-plugin"
    assert data["main"] == "./index.ts"


def test_index_registers_tools_skill_commands():
    src = (PLUGIN / "index.ts").read_text(encoding="utf-8")
    assert "Plugin.define" in src
    assert "ctx.tool.transform" in src
    assert "ctx.skill.transform" in src
    assert "ctx.command.transform" in src
    for tool in READONLY_TOOLS + MUTATING_TOOLS:
        assert f'"{tool}"' in src, f"tool not registered: {tool}"
    # deploy safety: preview is always dry-run, apply requires explicit confirm
    assert '"deploy", "--dry-run"' in src
    assert "confirm" in src and "deploy_preview" in src


def test_runner_helper_guards():
    src = (PLUGIN / "versioneer.ts").read_text(encoding="utf-8")
    assert "runVersioneer" in src
    assert "mutationsAllowed" in src
    assert "allowMutations" in src
    assert "VERSIONEER_BIN" in src


def test_skill_and_example_config():
    skill = (PLUGIN / "skills" / "versioneer" / "SKILL.md").read_text(encoding="utf-8")
    assert "description:" in skill
    assert "deploy_preview" in skill or "--dry-run" in skill
    example = (PLUGIN / "opencode.example.jsonc").read_text(encoding="utf-8")
    assert '"plugins"' in example
    assert "opencode-versioneer" in example
    assert "versioneer_deploy_apply" in example
