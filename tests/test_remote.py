"""Phase C: nonversioned remote backend for binaries / limited-revision targets.

Git holds only a pointer file; content revisions live on the remote with
keep-last-N rotation enforced at commit time (v1: file backend only).
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime

from click.testing import CliRunner

from versioneer.cli import cli
from versioneer.core import config as cfg
from versioneer.core import daemon as _daemon
from versioneer.core import remote as rem
from versioneer.core import store as store_mod


def _make(runner, tmp_path, name="rem"):
    store = tmp_path / f"store-{name}"
    r = runner.invoke(cli, ["config", "create", "--name", name,
                            "--path", str(store),
                            "--upstream", "git@example:x.git"])
    assert r.exit_code == 0, r.output
    return store


def _add_remote(runner, tmp_path, name, fname="save.bin", keep=2, kind="binary"):
    f = tmp_path / fname
    f.write_bytes(b"v1")
    r = runner.invoke(cli, ["-C", name, "target", "add", str(f),
                            "--kind", kind,
                            "--remote-root", str(tmp_path / "rem"),
                            "--remote-retention", str(keep)])
    assert r.exit_code == 0, r.output
    return f


def _blobs(tmp_path):
    return sorted((tmp_path / "rem").rglob("*.blob"))


# --- backend unit ---

def test_backend_push_list_fetch_prune(tmp_path):
    root = str(tmp_path / "r")
    b1 = rem.push_blob("file", root, "a.bin", b"one", "sha256:111")
    b2 = rem.push_blob("file", root, "a.bin", b"two", "sha256:222")
    names = rem.list_blobs("file", root, "a.bin")
    assert names == sorted([b1, b2], reverse=True)
    assert rem.fetch_blob("file", root, "a.bin", b1) in (b"one", b"two")
    doomed = rem.prune_blobs("file", root, "a.bin", 1)
    assert len(doomed) == 1
    assert len(rem.list_blobs("file", root, "a.bin")) == 1
    assert rem.list_blobs("file", root, "missing.bin") == []


def test_prune_never_deletes_just_pushed_blob(tmp_path):
    # Same-instant collision: tied timestamps must not eat the new revision.
    root = str(tmp_path / "r")
    frozen = datetime(2026, 1, 1, tzinfo=UTC)
    n1 = rem.push_blob("file", root, "a.bin", b"one", "sha256:111", when=frozen)
    n2 = rem.push_blob("file", root, "a.bin", b"two", "sha256:222", when=frozen)
    assert n1 != n2  # hash suffix disambiguates
    doomed = rem.prune_blobs("file", root, "a.bin", 1, exclude=n2)
    assert n2 not in doomed
    assert rem.list_blobs("file", root, "a.bin") == [n2]


def test_backend_errors(tmp_path):
    try:
        rem.list_blobs("ftp", "/x", "a")
        assert False
    except rem.RemoteError as e:
        assert "file" in str(e)
    try:
        rem.fetch_blob("file", str(tmp_path / "r"), "a", "nope.blob")
        assert False
    except rem.RemoteError as e:
        assert "run commit first" in str(e)
    assert rem.validate_remote_dict({"backend": "ftp", "root": "/x"})
    assert rem.validate_remote_dict({"backend": "file", "root": "relative"})
    assert rem.validate_remote_dict({"backend": "file", "root": "/x", "retention": 0})
    assert rem.validate_remote_dict({"backend": "file", "root": "/x"}) == []
    assert rem.remote_retention({}) == 3


def test_pointer_roundtrip(tmp_path):
    store = tmp_path / "s"
    prel = rem.pointer_rel_for(pathlib.Path("a/b.bin"))
    assert prel.as_posix() == "a/b.bin.remote.json"
    rem.write_pointer(store, prel, rem.pointer_payload("file", "/r", "a/b.bin", "n", "h"))
    assert rem.read_pointer(store, prel)["blob"] == "n"
    assert rem.read_pointer(store, prel.parent / "nope") is None


# --- CLI: add ---

def test_add_remote_pointer_in_git_blob_on_remote(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    store = _make(runner, tmp_path, "rm1")
    f = _add_remote(runner, tmp_path, "rm1")
    c = cfg.load("rm1")
    assert c.targets[0].remote["backend"] == "file"
    assert c.targets[0].remote["retention"] == 2
    # git holds the pointer, not the content
    files = store_mod._run_git(["ls-tree", "-r", "--name-only", "HEAD"], store).splitlines()
    assert any(p.endswith(".remote.json") for p in files)
    assert not any(p.endswith("save.bin") for p in files if not p.endswith(".json"))
    assert len(_blobs(tmp_path)) == 1
    assert _blobs(tmp_path)[0].read_bytes() == f.read_bytes()
    # snapshot carries the remote table for bootstrap
    snap = (store / cfg.SNAPSHOT_NAME).read_text()
    assert "[targets.remote]" in snap


def test_add_remote_rejects(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "rmx")
    d = tmp_path / "d"
    d.mkdir()
    (d / "f.txt").write_text("x")
    r = runner.invoke(cli, ["-C", "rmx", "target", "add", str(d),
                            "--kind", "dir", "--remote-root", str(tmp_path / "rem")])
    assert r.exit_code != 0 and "text|binary" in r.output
    f = tmp_path / "f.bin"
    f.write_bytes(b"\x00")
    r = runner.invoke(cli, ["-C", "rmx", "target", "add", str(f),
                            "--kind", "binary", "--remote-root", str(tmp_path / "rem"),
                            "--remote-backend", "ftp"])
    assert r.exit_code != 0 and "file" in r.output
    link = tmp_path / "lnk"
    link.symlink_to(f)
    r = runner.invoke(cli, ["-C", "rmx", "target", "add", str(link),
                            "--remote-root", str(tmp_path / "rem")])
    assert r.exit_code != 0 and "regular files" in r.output
    r = runner.invoke(cli, ["-C", "rmx", "target", "add", str(f),
                            "--kind", "binary", "--remote-root", str(tmp_path / "rem"),
                            "--encrypt"])
    assert r.exit_code != 0 and "encrypt" in r.output
    r = runner.invoke(cli, ["-C", "rmx", "target", "add", str(f),
                            "--kind", "binary", "--remote-root", str(tmp_path / "rem"),
                            "--remote-retention", "0"])
    assert r.exit_code != 0 and "remote-retention" in r.output


# --- CLI: commit rotation / status / log / diff / deploy ---

def test_commit_rotates_keep_last_n(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "rot")
    f = _add_remote(runner, tmp_path, "rot", keep=2)
    for i in (2, 3, 4):
        f.write_bytes(f"v{i}".encode())
        r = runner.invoke(cli, ["-C", "rot", "commit", str(f), "-m", f"v{i}"])
        assert r.exit_code == 0, r.output
    blobs = _blobs(tmp_path)
    assert len(blobs) == 2  # exactly keep-last-2, not 4
    assert sorted(b.read_bytes() for b in blobs) == [b"v3", b"v4"]
    r = runner.invoke(cli, ["-C", "rot", "commit", str(f), "-m", "noop"])
    assert r.exit_code == 0 and "nothing to commit" in r.output  # clean now
    out = runner.invoke(cli, ["-C", "rot", "status"]).output
    assert "clean" in out and "[remote]" in out


def test_log_lists_blob_revisions(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "rlg")
    f = _add_remote(runner, tmp_path, "rlg", keep=5)
    for i in (2, 3):
        f.write_bytes(f"v{i}".encode())
        assert runner.invoke(cli, ["-C", "rlg", "commit", str(f), "-m", f"v{i}"]).exit_code == 0
    out = runner.invoke(cli, ["-C", "rlg", "log", str(f)]).output
    # blob names are long (rich may wrap them); count occurrences instead
    assert out.replace("\n", "").count(".blob") == 3
    out2 = runner.invoke(cli, ["-C", "rlg", "log", "-n", "1", str(f)]).output
    assert out2.replace("\n", "").count(".blob") == 1


def test_diff_against_remote(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("VERSIONEER_STATE_DIR", str(tmp_path / "state"))
    runner = CliRunner()
    _make(runner, tmp_path, "rdf")
    f = tmp_path / "n.txt"
    f.write_text("v1\n")
    r = runner.invoke(cli, ["-C", "rdf", "target", "add", str(f),
                            "--kind", "text",
                            "--remote-root", str(tmp_path / "rem")])
    assert r.exit_code == 0, r.output
    f.write_text("v2\n")
    out = runner.invoke(cli, ["-C", "rdf", "diff", str(f)]).output
    assert "-v1" in out and "+v2" in out


def test_deploy_restores_latest(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("VERSIONEER_STATE_DIR", str(tmp_path / "state"))
    runner = CliRunner()
    _make(runner, tmp_path, "rdp")
    f = _add_remote(runner, tmp_path, "rdp", keep=3)
    f.write_bytes(b"v2")
    assert runner.invoke(cli, ["-C", "rdp", "commit", str(f), "-m", "v2"]).exit_code == 0
    f.write_bytes(b"tampered")
    assert runner.invoke(cli, ["-C", "rdp", "deploy", "--yes"]).exit_code == 0
    assert f.read_bytes() == b"v2"
    out = runner.invoke(cli, ["-C", "rdp", "deploy", "--dry-run"]).output
    assert "dry-run" in out


# --- CLI: set / remove ---

def test_set_retune_enforces_now(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "rtn")
    f = _add_remote(runner, tmp_path, "rtn", keep=5)
    for i in (2, 3):
        f.write_bytes(f"v{i}".encode())
        assert runner.invoke(cli, ["-C", "rtn", "commit", str(f), "-m", f"v{i}"]).exit_code == 0
    assert len(_blobs(tmp_path)) == 3
    r = runner.invoke(cli, ["-C", "rtn", "target", "set", str(f), "--remote-retention", "1"])
    assert r.exit_code == 0, r.output
    assert "pruned 2 old remote revision" in r.output
    assert len(_blobs(tmp_path)) == 1
    assert cfg.load("rtn").targets[0].remote["retention"] == 1


def test_set_attach_and_detach_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "mig")
    f = tmp_path / "g.bin"
    f.write_bytes(b"v1")
    assert runner.invoke(cli, ["-C", "mig", "target", "add", str(f), "--kind", "binary"]).exit_code == 0
    r = runner.invoke(cli, ["-C", "mig", "target", "set", str(f),
                            "--remote-root", str(tmp_path / "rem")])
    assert r.exit_code == 0, r.output
    assert "moved" in r.output and "remote" in r.output
    c = cfg.load("mig")
    assert c.targets[0].remote["root"] == str(tmp_path / "rem")
    assert len(_blobs(tmp_path)) == 1
    r = runner.invoke(cli, ["-C", "mig", "target", "set", str(f), "--clear-remote"])
    assert r.exit_code == 0, r.output
    assert "moved" in r.output and "git" in r.output
    c = cfg.load("mig")
    assert c.targets[0].remote == {}
    assert "clean" in runner.invoke(cli, ["-C", "mig", "status"]).output


def test_set_remote_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "rse")
    f = _add_remote(runner, tmp_path, "rse")
    r = runner.invoke(cli, ["-C", "rse", "target", "set", str(f),
                            "--remote-root", str(tmp_path / "rem2")])
    assert r.exit_code != 0 and "already a remote target" in r.output
    r = runner.invoke(cli, ["-C", "rse", "target", "set", str(f),
                            "--retention-count", "3"])
    assert r.exit_code != 0 and "--remote-retention" in r.output
    r = runner.invoke(cli, ["-C", "rse", "target", "set", str(f),
                            "--clear-remote", "--remote-retention", "2"])
    assert r.exit_code != 0 and "--clear-remote" in r.output
    g = tmp_path / "plain.bin"
    g.write_bytes(b"x")
    assert runner.invoke(cli, ["-C", "rse", "target", "add", str(g), "--kind", "binary"]).exit_code == 0
    r = runner.invoke(cli, ["-C", "rse", "target", "set", str(g), "--remote-retention", "2"])
    assert r.exit_code != 0 and "not a remote target" in r.output
    r = runner.invoke(cli, ["-C", "rse", "target", "set", str(g), "--clear-remote"])
    assert r.exit_code != 0 and "not a remote target" in r.output


def test_remove_keeps_blobs_with_note(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make(runner, tmp_path, "rrm")
    f = _add_remote(runner, tmp_path, "rrm")
    r = runner.invoke(cli, ["-C", "rrm", "target", "remove", str(f)])
    assert r.exit_code == 0, r.output
    assert "remote revisions kept" in r.output
    assert len(_blobs(tmp_path)) == 1  # blobs survive untrack


# --- daemon + doctor ---

def test_daemon_autocommit_pushes_remote(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("VERSIONEER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("NOTIFY_DEBUG", "1")
    runner = CliRunner()
    _make(runner, tmp_path, "rda")
    f = _add_remote(runner, tmp_path, "rda", keep=3)
    c = cfg.load("rda")
    c.targets[0].auto_commit = True
    cfg.save(c)
    f.write_bytes(b"v2")
    out = _daemon.check_once("rda")
    assert not out["errors"], out["errors"]
    assert len(out["committed"]) == 1
    assert len(_blobs(tmp_path)) == 2


def test_doctor_remote_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("VERSIONEER_DOCTOR_TIMEOUT", "2")
    runner = CliRunner()
    _make(runner, tmp_path, "rdc")
    _add_remote(runner, tmp_path, "rdc")
    from unittest import mock as _mock

    with _mock.patch("versioneer.core.store.upstream_reachable", return_value=(None, "skip")):
        r = runner.invoke(cli, ["-C", "rdc", "doctor"])
    assert r.exit_code == 0, r.output
    assert "plain git" not in r.output  # no binary-in-git warning for remote targets
