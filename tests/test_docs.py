"""ITEM 12: docs refresh — README matches implemented behavior (ITEMS 1-11)."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def test_readme_mentions_implemented_surface():
    text = _readme()
    for needle in (
        "service run",
        "service check",
        "uninstall",
        "--apply",
        "--force",
        "--dry-run",
        "--timeout",
        "--prune",
        ".versioneer.toml",
        "[targets.manifest]",
        "watchdog",
        "timer",
    ):
        assert needle in text, f"README missing {needle!r}"


def test_readme_documents_undocumented_flags():
    text = _readme()
    # service check/run, watch flags, deploy --host, bootstrap flags
    assert "service check" in text
    assert "--timeout" in text
    assert "--glob" in text
    assert "--ignore" in text
    assert "--host" in text
    assert "bootstrap" in text and "--yes" in text


def test_readme_no_stale_phase_rows():
    text = _readme()
    for stale in (
        "expected until Phase 0",
        "Phase 0 works",
        "Phase 0–1",
        "Phase 0-1",
        "not yet packaged",
        "Not yet implemented",
        "track Phase 6",
    ):
        assert stale not in text, f"stale docs row still present: {stale!r}"


def test_installer_and_completions_exist_and_full():
    assert (REPO / "installer" / "install.sh").is_file()
    assert (REPO / "installer" / "uninstall.sh").is_file()
    for fname in ("versioneer.bash", "versioneer.fish", "versioneer.zsh"):
        p = REPO / "installer" / "completions" / fname
        assert p.is_file(), f"missing {p}"
        content = p.read_text(encoding="utf-8")
        # full completions cover subcommands/flags, not Phase-0 stubs
        assert "service" in content
        assert "uninstall" in content
        assert "bootstrap" in content
        assert "Phase 0" not in content
