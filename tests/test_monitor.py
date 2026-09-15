"""Unit tests for monitor.py: resolution, detection, hashing, ignores."""

from __future__ import annotations

from pathlib import Path

import pytest

from versioneer.core import monitor as mon


def test_expand_path_env_and_user(tmp_path, monkeypatch):
    monkeypatch.setenv("MYDIR", str(tmp_path))
    assert mon.expand_path("$MYDIR/x") == tmp_path / "x"
    # ~ expands to home
    assert str(mon.expand_path("~")).startswith("/")


def test_resolve_input_no_root_absolute(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hi")
    stored, abs_p = mon.resolve_input(str(f))
    assert stored == str(abs_p)
    assert abs_p.name == "a.txt"


def test_resolve_input_no_root_relative(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rel.txt").write_text("x")
    _stored, abs_p = mon.resolve_input("rel.txt")
    assert abs_p == tmp_path / "rel.txt"


def test_resolve_input_with_root_absolute_ok(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    f = root / "sub" / "f.conf"
    f.write_text("x")
    stored, _abs_p = mon.resolve_input(str(f), str(root))
    assert stored == "sub/f.conf"


def test_resolve_input_with_root_relative(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    stored, abs_p = mon.resolve_input("a/b.conf", str(root))
    assert stored == "a/b.conf"
    assert str(abs_p).endswith("a/b.conf")


def test_resolve_input_with_root_outside_raises(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ValueError, match="not under root"):
        mon.resolve_input("/etc/passwd", str(root))


def test_resolve_input_preserves_symlink(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("data")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    _stored, abs_p = mon.resolve_input(str(link))
    assert abs_p.is_symlink()


def test_detect_flex_home_file(tmp_path):
    home_file = Path.home() / "some-versioneer-probe.txt"
    assert mon.detect_flex(home_file) == "user"
    assert mon.detect_flex(Path("/etc/fstab")) == "fixed"
    # /home/<other>/... also user even if not current home
    assert mon.detect_flex(Path("/home/otheruser/x.txt")) == "user"


def test_looks_binary_and_detect_kind(tmp_path):
    text = tmp_path / "t.txt"
    text.write_text("hello\n")
    assert not mon.looks_binary_file(text)
    assert mon.detect_kind(text) == "text"

    binary = tmp_path / "b.bin"
    binary.write_bytes(b"\x00\x01\x02binary")
    assert mon.looks_binary_file(binary)
    assert mon.detect_kind(binary) == "binary"

    sub = tmp_path / "d"
    sub.mkdir()
    assert mon.detect_kind(sub) == "dir"

    # symlink preserve -> text (link artifact), follow dir -> dir
    real = tmp_path / "real.txt"
    real.write_text("x")
    link = tmp_path / "lnk"
    link.symlink_to(real)
    assert mon.detect_kind(link, "preserve") == "text"
    assert mon.detect_kind(link, "follow") == "text"  # target is text file

    dirlink = tmp_path / "dirlink"
    dirlink.symlink_to(sub)
    assert mon.detect_kind(dirlink, "preserve") == "text"
    assert mon.detect_kind(dirlink, "follow") == "dir"

    assert mon.detect_kind(tmp_path / "does-not-exist") == "text"
    assert not mon.looks_binary_file(tmp_path / "does-not-exist")


def test_expand_glob(tmp_path):
    (tmp_path / "a.sh").write_text("a")
    (tmp_path / "b.sh").write_text("b")
    matches = mon.expand_glob(str(tmp_path / "*.sh"))
    assert len(matches) == 2
    assert mon.expand_glob(str(tmp_path / "*.nomatch_xyz")) == []


def test_matches_ignore():
    assert mon.matches_ignore("skip.log", ["*.log"])
    assert mon.matches_ignore("sub/skip.log", ["*.log"])
    assert mon.matches_ignore("Cache/foo.txt", ["Cache/"])
    assert mon.matches_ignore("Cache", ["Cache/"])
    assert not mon.matches_ignore("keep.txt", ["*.log"])
    assert not mon.matches_ignore("keep.txt", [""])
    # ** patterns collapse
    assert mon.matches_ignore("a/Cache/b.txt", ["**/Cache/**"])
    assert mon.matches_ignore("dosdevices/c:", ["dosdevices/**"])


def test_iter_dir_files_respects_ignore(tmp_path):
    top = tmp_path / "top"
    (top / "sub").mkdir(parents=True)
    (top / "keep.txt").write_text("k")
    (top / "skip.log").write_text("s")
    (top / "sub" / "inner.txt").write_text("i")
    (top / "Cache").mkdir()
    (top / "Cache" / "c.txt").write_text("c")
    files = mon.iter_dir_files(top, ["*.log", "Cache/"])
    names = sorted(p.name for p in files)
    assert names == ["inner.txt", "keep.txt"]
    assert mon.iter_dir_files(tmp_path / "nope", []) == []


def test_sha256_and_safe_hash(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("hello")
    digest = mon.sha256_file(f)
    assert digest.startswith("sha256:")
    digest2, copied = mon.safe_sha256_file(f)
    assert digest2 == digest
    assert copied is False


def test_hash_target_symlink_preserve(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("data")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    h = mon.hash_target(link, "text", "preserve")
    assert h.startswith("symlink:")
    # follow hashes content
    h2 = mon.hash_target(link, "text", "follow")
    assert h2.startswith("sha256:")


def test_hash_target_dir_and_missing(tmp_path):
    top = tmp_path / "d"
    top.mkdir()
    (top / "a.txt").write_text("a")
    h1 = mon.hash_target(top, "dir", "preserve", [])
    h2 = mon.hash_target(top, "dir", "preserve", ["*.txt"])
    assert h1.startswith("dir-sha256:")
    assert h2.startswith("dir-sha256:")
    assert h1 != h2
    assert mon.hash_target(tmp_path / "gone.txt", "text", "preserve") == "missing"


def test_artifact_rel_user_and_fixed(tmp_path, monkeypatch):
    home = Path.home()
    assert mon.artifact_rel("", home / "a.txt", "user", "") == Path("a.txt")
    # /home/<other> stripped to remainder
    assert mon.artifact_rel("", Path("/home/other/x.txt"), "user", "") == Path("x.txt")
    # root-based: stored path used directly (caller passes it)
    assert mon.artifact_rel("nginx/nginx.conf", Path("/etc/nginx/nginx.conf"), "fixed", "/etc") == Path(
        "nginx/nginx.conf"
    )
    # fixed absolute -> stripped leading slash
    assert mon.artifact_rel("", Path("/etc/fstab"), "fixed", "") == Path("etc/fstab")


def test_wine_preset_ignores(tmp_path):
    # wine-looking dir gets defaults appended
    winedir = tmp_path / "myprefix-wine"
    winedir.mkdir()
    (winedir / "drive_c").mkdir()
    out = mon.wine_preset_ignores(winedir, [])
    for pat in mon.WINE_DEFAULT_IGNORES:
        assert pat in out
    # non-wine dir untouched (use neutral /tmp path: tmp_path itself contains
    # "wine" via the test name, which the heuristic matches on purpose)
    neutral = Path("/tmp/vsr-plain-probe")
    assert mon.wine_preset_ignores(neutral, ["*.log"]) == ["*.log"]
    # no duplication on second call
    out2 = mon.wine_preset_ignores(winedir, out)
    assert len(out2) == len(out)
