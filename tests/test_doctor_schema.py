"""ITEM 11: doctor + schema + small fixes.

- unreachable upstream warns (ls-remote, timeout, warn-only) not crash
- [targets.manifest] subtable read/write compat
- auto_add_glob persists (+ interest=diff v1 state-deploy documented)
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from versioneer.cli import cli
from versioneer.core import config as cfg


def _make_config(runner: CliRunner, tmp_path: Path, name: str, upstream: str) -> Path:
    store = tmp_path / f"store-{name}"
    r = runner.invoke(
        cli,
        [
            "config",
            "create",
            "--name",
            name,
            "--path",
            str(store),
            "--upstream",
            upstream,
        ],
    )
    assert r.exit_code == 0, r.output
    return store


def test_doctor_unreachable_upstream_warns_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    # Short probe timeout so the test stays fast offline.
    monkeypatch.setenv("VERSIONEER_DOCTOR_TIMEOUT", "3")
    runner = CliRunner()
    _make_config(runner, tmp_path, "off", "git@example.invalid:foo/bar.git")
    r = runner.invoke(cli, ["-C", "off", "doctor"])
    # Warn-only: offline/unreachable must not crash and must not be an error.
    assert r.exit_code == 0, r.output
    assert "doctor:" in r.output
    low = r.output.lower()
    assert "unreachable" in low or "upstream" in low
    assert "traceback" not in low


def test_doctor_unreachable_upstream_mocked_warns(tmp_path, monkeypatch):
    """Mocked ls-remote failure still warns (no crash, no error exit)."""
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make_config(runner, tmp_path, "mockoff", "git@example.invalid:foo/bar.git")
    with mock.patch(
        "versioneer.core.store.upstream_reachable",
        return_value=(False, "upstream unreachable: mocked (warn-only, offline?)"),
    ):
        r = runner.invoke(cli, ["-C", "mockoff", "doctor"])
    assert r.exit_code == 0, r.output
    assert "unreachable" in r.output.lower()


def test_manifest_subtable_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    d = tmp_path / "cfg"
    d.mkdir(parents=True, exist_ok=True)
    (d / "wine.toml").write_text(
        """[meta]
name = "wine"
storage = "/tmp/versioneer-store-wine"

[[targets]]
path = "wine-manifest.json"
kind = "manifest"

[targets.manifest]
type = "wine"
source = "builtin:wine"
output = "wine-manifest.json"
""",
        encoding="utf-8",
    )
    c = cfg.load("wine")
    assert len(c.targets) == 1
    t = c.targets[0]
    assert t.kind == "manifest"
    assert isinstance(t.manifest, dict)
    assert t.manifest.get("type") == "wine"
    assert t.manifest.get("output") == "wine-manifest.json"
    # Write compat: round-trip keeps the subtable, omits empties elsewhere.
    saved = cfg.save(c)
    text = saved.read_text(encoding="utf-8")
    assert "[targets.manifest]" in text
    assert "type" in text
    c2 = cfg.load("wine")
    assert c2.targets[0].manifest.get("type") == "wine"


def test_manifest_flat_compat(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    # from_dict folds flat keys into the manifest subtable
    t2 = cfg.Target.from_dict(
        {"path": "packages.list", "kind": "manifest", "manifest_type": "packages"}
    )
    assert t2.manifest.get("type") == "packages"


def test_no_lfs_attributes_created_by_tracking(tmp_path, monkeypatch):
    # LFS was removed: tracking a binary must not create .gitattributes rules.
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    store = _make_config(runner, tmp_path, "plain", "git@example:plain.git")
    assert not (store / ".gitattributes").exists()
    b = tmp_path / "b.bin"
    b.write_bytes(b"\x00" * 64)
    r = runner.invoke(cli, ["-C", "plain", "target", "add", str(b), "--kind", "binary"])
    assert r.exit_code == 0, r.output
    assert "git lfs" not in r.output.lower()
    assert "filter=lfs" not in r.output.lower()
    assert not (store / ".gitattributes").exists()


def test_auto_add_glob_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make_config(runner, tmp_path, "snip", "git@example:x.git")
    f = tmp_path / "note.txt"
    f.write_text("hello\n")
    r = runner.invoke(
        cli,
        ["-C", "snip", "target", "add", str(f), "--auto-add-glob", "*.txt"],
    )
    assert r.exit_code == 0, r.output
    c = cfg.load("snip")
    assert c.targets[0].auto_add_glob == "*.txt"
    # Save/load round-trip (TOML) keeps the field.
    cfg.save(c)
    c2 = cfg.load("snip")
    assert c2.targets[0].auto_add_glob == "*.txt"
    # interest=diff is accepted and v1 deploys state (preview only).
    assert c2.targets[0].interest in ("state", "diff")
