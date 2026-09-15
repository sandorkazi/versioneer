"""Unit tests for lint.py + permissions.py."""

from __future__ import annotations

from pathlib import Path

from versioneer.core import lint as lint_mod
from versioneer.core import permissions as perm


def test_scan_bytes_detects_secrets():
    assert lint_mod.scan_bytes(b"nothing here") == []
    warns = lint_mod.scan_bytes(b'api_key = "AKIAIOSFODNN7EXAMPLE"\n')
    assert len(warns) == 1 and "secret scan" in warns[0]
    warns = lint_mod.scan_bytes(b"-----BEGIN PRIVATE KEY-----\n")
    assert warns
    warns = lint_mod.scan_bytes(b"token ghp_abcdefghij1234567890zzz")
    assert warns
    # only one warning even with multiple hits
    warns = lint_mod.scan_bytes(b"AKIAIOSFODNN7EXAMPLE ghp_abcdefghij1234567890zzz")
    assert len(warns) == 1


def test_scan_file(tmp_path):
    f = tmp_path / "ok.conf"
    f.write_text("plain config\n")
    assert lint_mod.scan_file(f) == []
    s = tmp_path / "tok.conf"
    s.write_text('password = "supersecretvalue123"\n')
    assert lint_mod.scan_file(s)
    # symlink skipped
    link = tmp_path / "link.conf"
    link.symlink_to(s)
    assert lint_mod.scan_file(link) == []
    # missing file -> []
    assert lint_mod.scan_file(tmp_path / "nope") == []


def test_hardcoded_path_warnings(tmp_path, monkeypatch):
    current = Path.home().name
    f = tmp_path / "c.conf"
    f.write_text(f"path=/home/{current}/docs\n")
    assert lint_mod.hardcoded_path_warnings(f) == []
    g = tmp_path / "d.conf"
    g.write_text("path=/home/alice_other_xyz/docs\n")
    if current == "alice_other_xyz":
        assert lint_mod.hardcoded_path_warnings(g) == []
    else:
        warns = lint_mod.hardcoded_path_warnings(g)
        assert warns and "--template" in warns[0]
    # symlink / missing -> []
    link = tmp_path / "l.conf"
    link.symlink_to(g)
    assert lint_mod.hardcoded_path_warnings(link) == []
    assert lint_mod.hardcoded_path_warnings(tmp_path / "nope") == []


def test_permissions_capture_file(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("x")
    owner, group, mode = perm.capture(f)
    assert owner and group
    assert len(mode) == 4 and mode.isdigit()
    # lstat mode for symlink
    link = tmp_path / "l.txt"
    link.symlink_to(f)
    o2, _g2, m2 = perm.capture(link, follow=False)
    assert o2 and m2


def test_is_readable(tmp_path):
    f = tmp_path / "r.txt"
    f.write_text("x")
    assert perm.is_readable(f) is True
    link = tmp_path / "l.txt"
    link.symlink_to(f)
    assert perm.is_readable(link, follow=False) is True
    assert perm.is_readable(tmp_path / "missing-xyz") is False
