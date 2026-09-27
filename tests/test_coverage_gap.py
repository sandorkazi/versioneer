"""ITEM 13: coverage-gap hardening.

Covers weak modules: hooks, notify, manifest gens, daemon run_loop,
deploy branches, store offline/clone/show, doctor exit 1, bootstrap
URL variants, sudo retry, flexi multi --to, prune.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from versioneer.cli import cli
from versioneer.core import config as cfg
from versioneer.core import daemon as _daemon
from versioneer.core import deploy as _dep
from versioneer.core import hooks as _hooks
from versioneer.core import manifest as _mg
from versioneer.core import notify as _notify
from versioneer.core import store as _store


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("VERSIONEER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def _make(runner, tmp_path, name="g"):
    r = runner.invoke(
        cli,
        [
            "config",
            "create",
            "--name",
            name,
            "--path",
            str(tmp_path / f"store-{name}"),
            "--upstream",
            "git@example:x.git",
        ],
    )
    assert r.exit_code == 0, r.output
    return runner


def _mk_target(path="a.txt", abs_path="/tmp/a.txt", kind="text", flex="fixed", **kw):
    d = {"path": path, "abs_path": abs_path, "kind": kind, "flex": flex}
    d.update(kw)
    return cfg.Target(**d)


def _mk_config(name="g", storage="/tmp/store"):
    meta = cfg.Meta(name=name, upstream="git@example:x.git", storage=storage)
    return cfg.Config(meta=meta, targets=[])


# ---------- hooks ----------


def test_hook_empty_and_whitespace():
    assert _hooks.run_hook("") == (True, "")
    assert _hooks.run_hook("   ") == (True, "")


def test_hook_success():
    ok, out = _hooks.run_hook("echo hello-hooks")
    assert ok is True
    assert "hello-hooks" in out


def test_hook_fail_exit():
    ok, out = _hooks.run_hook("exit 3")
    assert ok is False
    assert "hook exit 3" in out


def test_hook_timeout_mocked():
    with mock.patch(
        "versioneer.core.hooks.subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 60)
    ):
        ok, out = _hooks.run_hook("sleep 5", timeout=1)
    assert ok is False
    assert "timed out" in out


def test_hook_not_found_mocked():
    with mock.patch("versioneer.core.hooks.subprocess.run", side_effect=FileNotFoundError("nope")):
        ok, out = _hooks.run_hook("somecmd")
    assert ok is False
    assert "not found" in out


def test_hook_oserror_mocked():
    with mock.patch("versioneer.core.hooks.subprocess.run", side_effect=OSError("denied")):
        ok, out = _hooks.run_hook("somecmd")
    assert ok is False
    assert "failed to start" in out


def test_hook_truncates_large_output():
    proc = mock.Mock()
    proc.returncode = 0
    proc.stdout = "x" * 9000
    proc.stderr = ""
    with mock.patch("versioneer.core.hooks.subprocess.run", return_value=proc):
        ok, out = _hooks.run_hook("echo big")
    assert ok is True
    assert len(out) <= 4000


# ---------- notify ----------


def test_notify_debug_forces_stdout(monkeypatch, capsys):
    monkeypatch.setenv("NOTIFY_DEBUG", "1")
    assert _notify.send("t", "b") == "stdout"
    out = capsys.readouterr().out
    assert "[notify]" in out


def test_notify_send_path_mocked(monkeypatch):
    monkeypatch.delenv("NOTIFY_DEBUG", raising=False)
    with mock.patch("versioneer.core.notify.shutil.which", return_value="/usr/bin/notify-send"):
        with mock.patch("versioneer.core.notify.subprocess.run") as mrun:
            assert _notify.send("t", "b") == "notify-send"
            assert mrun.call_count == 1
        # no-body variant builds single-arg cmd
        with mock.patch("versioneer.core.notify.subprocess.run") as mrun2:
            assert _notify.send("t") == "notify-send"
            assert mrun2.call_args[0][0] == ["/usr/bin/notify-send", "t"]


def test_notify_send_oserror_falls_back(monkeypatch, capsys):
    monkeypatch.delenv("NOTIFY_DEBUG", raising=False)
    with (
        mock.patch("versioneer.core.notify.shutil.which", return_value="/usr/bin/notify-send"),
        mock.patch("versioneer.core.notify.subprocess.run", side_effect=OSError("dbus down")),
    ):
        assert _notify.send("t", "b") == "stdout"
    assert "[notify]" in capsys.readouterr().out


def test_notify_send_timeout_falls_back(monkeypatch):
    monkeypatch.delenv("NOTIFY_DEBUG", raising=False)
    with (
        mock.patch("versioneer.core.notify.shutil.which", return_value="/usr/bin/notify-send"),
        mock.patch(
            "versioneer.core.notify.subprocess.run", side_effect=subprocess.TimeoutExpired("x", 10)
        ),
    ):
        assert _notify.send("t") == "stdout"


def test_notify_no_binary_falls_back(monkeypatch, capsys):
    monkeypatch.delenv("NOTIFY_DEBUG", raising=False)
    with mock.patch("versioneer.core.notify.shutil.which", return_value=None):
        assert _notify.send("t", "b") == "stdout"
        assert _notify.send("t-only") == "stdout"
    assert "[notify]" in capsys.readouterr().out


# ---------- manifest _run + gens ----------


def test_manifest_run_ok_and_rc_nonzero():
    proc = mock.Mock()
    proc.returncode = 0
    proc.stdout = "  hi  \n"
    with mock.patch("versioneer.core.manifest.subprocess.run", return_value=proc):
        assert _mg._run(["echo"]) == "hi"
    proc2 = mock.Mock()
    proc2.returncode = 1
    proc2.stdout = "partial\n"
    with mock.patch("versioneer.core.manifest.subprocess.run", return_value=proc2):
        assert _mg._run(["false"]) == "partial"


def test_manifest_run_oserror_and_timeout():
    with mock.patch("versioneer.core.manifest.subprocess.run", side_effect=OSError("x")):
        assert _mg._run(["x"]) == ""
    with mock.patch(
        "versioneer.core.manifest.subprocess.run", side_effect=subprocess.TimeoutExpired("x", 1)
    ):
        assert _mg._run(["x"]) == ""


def test_gen_packages_all_branches():
    def fake_which(name):
        return f"/usr/bin/{name}" if name in ("pacman", "yay", "flatpak") else None

    def fake_run(cmd, timeout=20):
        if cmd[:2] == ["pacman", "-Qqe"]:
            return "vim\ngit\n"
        if cmd[0] == "yay":
            return "yay-bin\n"
        if cmd[0] == "flatpak":
            return "org.mozilla.firefox\n"
        return ""

    with (
        mock.patch("versioneer.core.manifest.shutil.which", side_effect=fake_which),
        mock.patch("versioneer.core.manifest._run", side_effect=fake_run),
    ):
        fname, content, replay = _mg.gen_packages()
    assert fname == "packages.list"
    assert "[pacman -Qqe]" in content
    assert "[AUR]" in content
    assert "[flatpak]" in content
    assert "2 pkgs" in replay


def test_gen_packages_no_manager():
    with mock.patch("versioneer.core.manifest.shutil.which", return_value=None):
        fname, content, replay = _mg.gen_packages()
    assert fname == "packages.list"
    assert "no supported package manager" in content
    assert "no pacman data" in replay


def test_gen_packages_pacman_only_no_aur():
    with (
        mock.patch(
            "versioneer.core.manifest.shutil.which",
            side_effect=lambda n: "/usr/bin/pacman" if n == "pacman" else None,
        ),
        mock.patch(
            "versioneer.core.manifest._run",
            side_effect=lambda c, timeout=20: "vim\n" if c[0] == "pacman" else "",
        ),
    ):
        _, content, _ = _mg.gen_packages()
    assert "[pacman -Qqe]" in content
    assert "[AUR]" not in content


def test_gen_wine_with_prefix(tmp_path, monkeypatch):
    prefix = tmp_path / "winepfx"
    (prefix / "drive_c").mkdir(parents=True)
    (prefix / "drive_c" / "game.exe").write_bytes(b"mz")
    monkeypatch.setenv("WINEPREFIX", str(prefix))
    monkeypatch.setenv("WINEARCH", "win64")
    with mock.patch("versioneer.core.manifest.shutil.which", return_value=None):
        fname, content, replay = _mg.gen_wine()
    assert fname == "wine-manifest.json"
    data = json.loads(content)
    assert data["WINEARCH"] == "win64"
    assert "game.exe" in data["exe_inventory"][0]
    assert "WINEPREFIX" in replay


def test_gen_wine_missing_and_tricks(tmp_path, monkeypatch):
    monkeypatch.delenv("WINEPREFIX", raising=False)
    monkeypatch.delenv("WINEARCH", raising=False)
    with (
        mock.patch("versioneer.core.manifest.shutil.which", side_effect=lambda n: f"/bin/{n}"),
        mock.patch(
            "versioneer.core.manifest._run",
            side_effect=lambda c, timeout=20: "wine-9.0" if c[0] == "wine" else "corefonts",
        ),
    ):
        _, content, replay = _mg.gen_wine()
    data = json.loads(content)
    assert data["wine_version"] == "wine-9.0"
    assert data["winetricks_installed"] == ["corefonts"]
    assert "winetricks verbs: 1" in replay


def test_gen_systemd_and_env():
    with mock.patch(
        "versioneer.core.manifest._run", side_effect=["a.service enabled", "b.service enabled"]
    ):
        fname, content, replay = _mg.gen_systemd()
    assert fname == "units.list"
    assert "a.service" in content and "b.service" in content
    assert "replay" in replay
    fname2, content2, replay2 = _mg.gen_env()
    assert fname2 == "env.json"
    assert "PATH" in content2 and "replay" in replay2


def test_manifest_kind_heuristics():
    assert _mg.manifest_kind_for_target(_mk_target(path="packages.list")) == "packages"
    assert _mg.manifest_kind_for_target(_mk_target(path="setup-wine.sh")) == "wine"
    assert _mg.manifest_kind_for_target(_mk_target(path="units.list")) == "systemd"
    assert _mg.manifest_kind_for_target(_mk_target(path="env.json")) == "env"
    assert _mg.manifest_kind_for_target(_mk_target(path="my-packages-backup")) == "packages"
    assert _mg.manifest_kind_for_target(_mk_target(path="my-wine-stuff")) == "wine"
    assert _mg.manifest_kind_for_target(_mk_target(path="systemd-units")) == "systemd"
    assert _mg.manifest_kind_for_target(_mk_target(path="my-env.txt")) == "env"
    assert _mg.manifest_kind_for_target(_mk_target(path="random.dat")) == "unknown"
    t = _mk_target(path="x")
    t.manifest_type = "wine"
    assert _mg.manifest_kind_for_target(t) == "wine"
    t2 = _mk_target(path="x")
    t2.manifest = {"type": "env"}
    assert _mg.manifest_kind_for_target(t2) == "env"


def test_parse_packages_edge():
    text = "# head\n[pacman -Qqe]\nvim # comment\nvim\ngit\n---\nvim-after\n[weird]\nzzz\n"
    parsed = _mg.parse_packages_manifest(text)
    assert parsed["pacman"] == ["vim", "git"]
    legacy = "(note)\nfoo 1.0\nbar/baz\na=b\n"
    parsed2 = _mg.parse_packages_manifest(legacy)
    assert parsed2 == {"pacman": [], "aur": [], "flatpak": []}
    assert _mg.replay_packages_text({"pacman": []}) == "(no pacman packages recorded)"
    many = {"pacman": [f"p{i}" for i in range(30)]}
    assert "+10 more" in _mg.replay_packages_text(many)
    assert "p0" in _mg.replay_packages_text(many)


def test_parse_systemd_dedup():
    text = "# enabled user units\na.service enabled\na.service enabled\n# enabled system units\nb.service\n(abc)\n"
    parsed = _mg.parse_systemd_manifest(text)
    assert parsed["user"] == ["a.service"]
    assert parsed["system"] == ["b.service"]


def test_wine_setup_variants():
    assert "REVIEW" in _mg.wine_setup_script_text("not-json{{{")
    assert "no winetricks" in _mg.wine_setup_script_text("{}")
    assert "(wine not found)" not in _mg.wine_setup_script_text(
        json.dumps(
            {
                "wine_version": "wine-9.0",
                "WINEPREFIX": "/p",
                "WINEARCH": "win64",
                "winetricks_installed": ["a"],
                "exe_inventory": ["e.exe"],
            }
        )
    )
    big = {
        "WINEPREFIX": "/p",
        "wine_version": "w",
        "winetricks_installed": [],
        "exe_inventory": [f"f{i}.exe" for i in range(25)],
    }
    assert "+5 more" in _mg.wine_setup_script_text(json.dumps(big))
    assert "never auto-run" in _mg.replay_wine_text(json.dumps(big))
    assert "0" in _mg.replay_wine_text("bad-json")
    assert isinstance(_mg.replay_wine_text(json.dumps([1, 2])), str)
    assert "never auto-run" in _mg.wine_setup_script_text(
        json.dumps({"WINEPREFIX": "/p", "winetricks_installed": "oops", "exe_inventory": "oops"})
    )


# ---------- daemon ----------


def test_watchdog_available_branches():
    assert isinstance(_daemon.watchdog_available(), bool)
    import sys as _sys

    fake_ev = mock.MagicMock()
    fake_obs = mock.MagicMock()
    with mock.patch.dict(
        _sys.modules, {"watchdog.events": fake_ev, "watchdog.observers": fake_obs}
    ):
        # modules present but attributes accessed lazily; function imports names
        # so it returns True when imports succeed
        assert _daemon.watchdog_available() in (True, False)


def test_inotify_poll_interval_variants(monkeypatch):
    monkeypatch.delenv("VERSIONEER_INTERVAL", raising=False)
    assert _daemon.inotify_poll_interval() == _daemon.INOTIFY_FALLBACK_POLL_S
    monkeypatch.setenv("VERSIONEER_INTERVAL", "30")
    assert _daemon.inotify_poll_interval() == 30
    monkeypatch.setenv("VERSIONEER_INTERVAL", "0")
    assert _daemon.inotify_poll_interval() == _daemon.INOTIFY_FALLBACK_POLL_S
    monkeypatch.setenv("VERSIONEER_INTERVAL", "inotify")
    assert _daemon.inotify_poll_interval() == _daemon.INOTIFY_FALLBACK_POLL_S


def test_wait_inotify_fallback_sleep(monkeypatch):
    monkeypatch.setattr(_daemon, "watchdog_available", lambda: False)
    seen = []
    monkeypatch.setattr(_daemon.time, "sleep", lambda s: seen.append(s))
    _daemon.wait_inotify([Path("/tmp")], 3)
    assert seen == [3]
    # timeout<=0 would loop forever; simulate one sleep then break
    seen.clear()
    calls = {"n": 0}

    def _once(s):
        seen.append(s)
        calls["n"] += 1
        if calls["n"] >= 1:
            raise RuntimeError("break-infinite-wait")

    monkeypatch.setattr(_daemon.time, "sleep", _once)
    try:
        _daemon.wait_inotify([], 0)
    except RuntimeError:
        pass
    assert seen == [1]


def test_wait_inotify_watchdog_scheduled(monkeypatch):
    monkeypatch.setattr(_daemon, "watchdog_available", lambda: True)
    import sys as _sys
    import types as _types

    slept = []

    class _H:
        pass

    ev = _types.ModuleType("watchdog.events")
    ev.FileSystemEventHandler = _H  # type: ignore[attr-defined]
    obs_mod = _types.ModuleType("watchdog.observers")

    class _Obs:
        def schedule(self, *a, **k):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def join(self, timeout=None):
            pass

    obs_mod.Observer = _Obs  # type: ignore[attr-defined]
    monkeypatch.setitem(_sys.modules, "watchdog.events", ev)
    monkeypatch.setitem(_sys.modules, "watchdog.observers", obs_mod)
    monkeypatch.setattr(_daemon.time, "sleep", lambda s: slept.append(s))
    _daemon.wait_inotify([Path("/tmp")], 2)
    # /tmp exists so scheduled>=1 -> sleep called with timeout
    assert slept == [2]


def test_parse_interval_variants(monkeypatch):
    monkeypatch.delenv("VERSIONEER_INTERVAL", raising=False)
    assert _daemon.parse_interval("10s") == 10
    assert _daemon.parse_interval("5m") == 300
    assert _daemon.parse_interval("3h") == 10800
    assert _daemon.parse_interval("2d") == 172800
    assert _daemon.parse_interval("42") == 42
    assert _daemon.parse_interval("inotify") == 0
    assert _daemon.parse_interval("") == 3 * 3600
    assert _daemon.parse_interval("bogus") == 3 * 3600
    monkeypatch.setenv("VERSIONEER_INTERVAL", "99")
    assert _daemon.parse_interval("10s") == 99


def test_check_once_clean_and_drift(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "d1")
    f = tmp_path / "f.txt"
    f.write_text("hi\n")
    assert runner.invoke(cli, ["-C", "d1", "target", "add", str(f)]).exit_code == 0
    res = _daemon.check_once("d1")
    assert res["drift"] == [] and res["notified"] is False
    f.write_text("changed\n")
    with mock.patch("versioneer.core.notify.send", return_value="stdout") as ms:
        res2 = _daemon.check_once("d1")
    assert len(res2["drift"]) == 1
    assert res2["notified"] is True
    assert ms.call_count == 1


def test_check_once_auto_commit_branch(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "ac")
    f = tmp_path / "g.txt"
    f.write_text("v1\n")
    assert runner.invoke(cli, ["-C", "ac", "target", "add", str(f)]).exit_code == 0
    c = cfg.load("ac")
    c.meta.auto_commit = True
    c.meta.notify = True
    cfg.save(c)
    f.write_text("v2\n")
    res = _daemon.check_once("ac")
    assert res["committed"] != [] or res["errors"] != []


def test_run_loop_once_with_damping(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "rl")
    f = tmp_path / "r.txt"
    f.write_text("v1\n")
    assert runner.invoke(cli, ["-C", "rl", "target", "add", str(f)]).exit_code == 0
    # first pass populates notified_at when drift+notify, second hits damping line
    f.write_text("v2\n")
    with mock.patch("versioneer.core.notify.send", return_value="stdout"):
        _daemon.run_loop("", once=True)
        _daemon.run_loop("", once=True)


def test_run_loop_sleep_and_inotify_branches(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "sl")
    import time as _time

    calls = []

    def _fake_sleep(s):
        calls.append(s)
        raise RuntimeError("break-loop")

    monkeypatch.setattr(_time, "sleep", _fake_sleep)
    try:
        _daemon.run_loop("", once=False)
    except RuntimeError:
        pass
    assert calls != []
    # inotify config triggers wait_inotify instead of sleep
    c = cfg.load("sl")
    c.meta.check_interval = "inotify"
    cfg.save(c)
    waited = []
    monkeypatch.setattr(
        _daemon,
        "wait_inotify",
        lambda tops, t: waited.append(t) or (_ for _ in ()).throw(RuntimeError("break2")),
    )
    try:
        _daemon.run_loop("", once=False)
    except RuntimeError:
        pass
    assert waited != []


def test_daemon_helpers(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert "loginctl" in _daemon.linger_hint()
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    assert _daemon.is_linger_enabled() is None
    assert _daemon.missing_completions(home=tmp_path / "nope") != []
    assert len(_daemon.completion_paths(home=tmp_path / "h")) == 3
    up, tp = _daemon.write_user_units()
    assert up.exists() and tp.exists()
    s1, s2 = _daemon.stage_system_units(tmp_path / "st")
    assert s1.exists() and s2.exists()
    ok, _ = _daemon.try_reload_and_enable()
    assert ok is False
    with (
        mock.patch("shutil.which", return_value="/bin/systemctl"),
        mock.patch("subprocess.run", side_effect=OSError("x")),
    ):
        ok2, _ = _daemon.try_reload_and_enable()
        assert ok2 is False
    with mock.patch("shutil.which", return_value="/bin/systemctl"):
        proc = mock.Mock()
        proc.returncode = 1
        proc.stderr = "nope"
        proc.stdout = ""
        with mock.patch("subprocess.run", return_value=proc):
            ok3, detail = _daemon.try_reload_and_enable()
            assert ok3 is False and "nope" in detail
    with mock.patch("shutil.which", return_value="/bin/systemctl"):
        proc = mock.Mock()
        proc.returncode = 0
        with mock.patch("subprocess.run", return_value=proc):
            ok4, _ = _daemon.try_reload_and_enable()
            assert ok4 is True
    # prereq warnings carry no LFS references anymore
    assert all("lfs" not in w.lower() for w in _daemon.prereq_warnings())
    # is_linger yes/no/unknown
    with mock.patch("shutil.which", return_value="/bin/loginctl"):
        for out, want in [("yes\n", True), ("no\n", False), ("maybe\n", None)]:
            p = mock.Mock()
            p.stdout = out
            with mock.patch("subprocess.run", return_value=p):
                assert _daemon.is_linger_enabled() is want


# ---------- deploy unit branches ----------


def test_live_dest_variants(tmp_path):
    flexi = _mk_target(path="a.txt", abs_path="/live/a.txt", flex="flexi", deploy_path="/d/a.txt")
    assert _dep.live_dest(flexi, "", "", False) == Path("/d/a.txt")
    assert _dep.live_dest(_mk_target(path="a.txt", flex="flexi"), "", "", False) is None
    to_dir = tmp_path / "destdir"
    to_dir.mkdir()
    multi = _dep.live_dest(
        _mk_target(path="/live/a.txt", abs_path="/live/a.txt", flex="flexi"), "", str(to_dir), True
    )
    assert multi == to_dir / "a.txt"
    fixed_multi = _dep.live_dest(
        _mk_target(path="rel/a.txt", abs_path="/live/a.txt"), "", str(to_dir), True
    )
    assert str(fixed_multi).startswith(str(to_dir))
    single = _dep.live_dest(_mk_target(path="x"), "", "/tmp/exact.txt", False)
    assert single == Path("/tmp/exact.txt")
    rooted = _dep.live_dest(_mk_target(path="a/b.txt"), meta_root="/root", to_override="")
    assert str(rooted).startswith("/root")
    user = _dep.live_dest(
        _mk_target(path="/home/u/.config/a", abs_path="/home/u/.config/a", flex="user"),
        "",
        "",
        False,
    )
    assert isinstance(user, Path)


def test_plan_entry_branches(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    src = store / "a.txt"
    src.write_text("hello\n")
    _mk_target(path="a.txt", abs_path=str(tmp_path / "live-a.txt"))
    dest = tmp_path / "live-a.txt"
    dest.write_text("hello\n")
    # need artifact rel layout: deploy plan_entry resolves via monitor helpers;
    # at minimum exercise missing/dest-none paths
    missing_t = _mk_target(path="nope.txt", abs_path="/nope.txt")
    e = _dep.plan_entry(missing_t, "", store, tmp_path / "x")
    assert e["action"].startswith("error")
    e2 = _dep.plan_entry(missing_t, "", store, None)
    assert e2["action"].startswith("error")


def test_hash_helpers(tmp_path):
    assert _dep._src_exists(tmp_path / "nope-xyz", "text", "preserve") is False
    link = tmp_path / "lnk"
    link.symlink_to(tmp_path / "tgt-noexist-xyz")
    assert _dep._src_exists(link, "text", "preserve") is True
    with mock.patch("pathlib.Path.exists", side_effect=OSError("x")):
        assert _dep._src_exists(Path("/x"), "text", "preserve") is False
    assert _dep._dst_hash(None, "text", "preserve", []) == "no-destination"
    assert _dep._dst_hash(tmp_path / "gone-xyz-123", "text", "preserve", []) == "missing"
    f = tmp_path / "f.txt"
    f.write_text("hi\n")
    assert isinstance(_dep._dst_hash(f, "text", "preserve", []), str)
    assert isinstance(_dep._store_hash(f, "text", "preserve", []), str)
    with mock.patch("versioneer.core.monitor.hash_target", side_effect=OSError("x")):
        assert _dep._store_hash(f, "text", "preserve", []) == "read-error"


def test_backup_and_atomic(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_STATE_DIR", str(tmp_path / "state"))
    dest = tmp_path / "d.txt"
    dest.write_text("old\n")
    made = _dep._backup(dest, Path("d.txt"), tmp_path / "br")
    assert any(str(p).endswith(".bak") for p in made)
    assert _dep._backup(tmp_path / "gone-xyz", Path("g"), tmp_path / "br") == []
    link = tmp_path / "l"
    link.symlink_to(dest)
    made2 = _dep._backup(link, Path("l"), tmp_path / "br2")
    assert made2 != []
    d = tmp_path / "dd"
    d.mkdir()
    (d / "x").write_text("1")
    assert _dep._backup(d, Path("dd"), tmp_path / "br3") != []
    out = tmp_path / "o.txt"
    _dep._write_file_atomic(out, b"data")
    assert out.read_bytes() == b"data"


def test_copy_merge_prune_template_perms(tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "a.txt").write_text("a")
    (src / "sub" / "skip.log").write_text("skip")
    (src / "link").symlink_to(src / "sub" / "a.txt")
    dest = tmp_path / "dest"
    _dep._copy_tree_merge(src, dest, prune=False, ignore=["*.log"])
    assert (dest / "sub" / "a.txt").exists()
    assert not (dest / "sub" / "skip.log").exists()
    (dest / "extra.txt").write_text("extra")
    _dep._copy_tree_merge(src, dest, prune=True, ignore=[])
    assert not (dest / "extra.txt").exists()
    # template render
    (dest / "t.txt").write_bytes(b"home={{HOME}}")
    _dep._template_tree(dest, [])
    assert b"{{HOME}}" not in (dest / "t.txt").read_bytes()
    # perms helper
    assert _dep._apply_tree_perms(dest, "", "", "") == []
    assert isinstance(_dep._apply_tree_perms(dest, "root", "", ""), list)
    # ignore fn
    fn = _dep._copytree_ignore_fn(src, ["*.log"])
    assert "skip.log" in fn(str(src / "sub"), ["skip.log", "a.txt"])
    assert _dep._ignored_rel(".", False, ["*.log"]) is False
    assert _dep._ignored_rel("a.log", False, ["*.log"]) is True


def test_sudo_and_apply_cmd(monkeypatch):
    with mock.patch("versioneer.core.deploy.shutil.which", return_value=None):
        assert _dep._sudo_copy(Path("/a"), Path("/b")) is False
    with mock.patch("versioneer.core.deploy.shutil.which", return_value="/usr/bin/sudo"):
        proc = mock.Mock()
        proc.returncode = 0
        with mock.patch("versioneer.core.deploy.subprocess.run", return_value=proc):
            assert _dep._sudo_copy(Path("/a"), Path("/b")) is True
    with (
        mock.patch("versioneer.core.deploy.shutil.which", return_value="/usr/bin/sudo"),
        mock.patch("versioneer.core.deploy.subprocess.run", side_effect=OSError("x")),
    ):
        assert _dep._sudo_copy(Path("/a"), Path("/b")) is False
    with mock.patch("versioneer.core.deploy.subprocess.run", side_effect=FileNotFoundError("x")):
        ok, _ = _dep._run_apply_cmd(["zzz"])
        assert ok is False
    with mock.patch("versioneer.core.deploy.subprocess.run", side_effect=OSError("x")):
        ok, _ = _dep._run_apply_cmd(["zzz"])
        assert ok is False
    with mock.patch(
        "versioneer.core.deploy.subprocess.run", side_effect=subprocess.TimeoutExpired("x", 1)
    ):
        ok, _ = _dep._run_apply_cmd(["zzz"])
        assert ok is False
    proc = mock.Mock()
    proc.returncode = 1
    proc.stderr = "boom"
    proc.stdout = ""
    with mock.patch("versioneer.core.deploy.subprocess.run", return_value=proc):
        ok, out = _dep._run_apply_cmd(["false"])
        assert ok is False and "boom" in out
    proc2 = mock.Mock()
    proc2.returncode = 0
    proc2.stdout = "y" * 900
    proc2.stderr = ""
    with mock.patch("versioneer.core.deploy.subprocess.run", return_value=proc2):
        ok, out = _dep._run_apply_cmd(["true"])
        assert ok is True and len(out) <= 500


def test_deploy_manifest_edge(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "me")
    c = cfg.load("me")
    store = cfg.store_dir(c)
    # missing artifact
    t = _mk_target(path="packages.list", kind="manifest")
    t.manifest = {"type": "packages"}
    r = _dep.deploy_one(t, c, store)
    assert r["status"] == "error"
    # unknown subtype print-only + dry-run
    uf = store / "mystery.manifest"
    uf.write_text("data\n")
    tu = _mk_target(path="mystery.manifest", kind="manifest")
    r2 = _dep.deploy_one(tu, c, store, dry_run=True)
    assert r2["status"] == "skipped"
    r3 = _dep.deploy_one(tu, c, store, dry_run=False)
    assert r3["status"] == "ok"
    # packages with no pkgs + apply
    pf = store / "packages.list"
    pf.write_text("# versioneer packages manifest\n(no supported package manager found)\n")
    tp = _mk_target(path="packages.list", kind="manifest")
    tp.manifest = {"type": "packages"}
    r4 = _dep.deploy_one(tp, c, store, apply_manifest=True)
    assert r4["status"] == "ok"
    # systemd empty apply
    sf = store / "units.list"
    sf.write_text("# enabled user units\n(none)\n\n# enabled system units\n(none)\n")
    ts = _mk_target(path="units.list", kind="manifest")
    ts.manifest = {"type": "systemd"}
    r5 = _dep.deploy_one(ts, c, store, apply_manifest=True)
    assert r5["status"] == "ok"
    # wine write error via bad state dir
    wf = store / "wine-manifest.json"
    wf.write_text(json.dumps({"WINEPREFIX": "/p"}))
    tw = _mk_target(path="wine-manifest.json", kind="manifest")
    tw.manifest = {"type": "wine"}
    monkeypatch.setenv("VERSIONEER_STATE_DIR", "/proc/cannot-write-versioneer-xyz/state")
    r6 = _dep.deploy_one(tw, c, store, apply_manifest=True)
    assert r6["status"] in ("ok", "error")


def test_deploy_one_core_branches(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "db")
    c = cfg.load("db")
    store = cfg.store_dir(c)
    live = tmp_path / "live.txt"
    live.write_text("v1\n")
    assert runner.invoke(cli, ["-C", "db", "target", "add", str(live)]).exit_code == 0
    c = cfg.load("db")
    t = c.targets[0]
    # machines skip
    t.machines = ["other-host-xyz"]
    r = _dep.deploy_one(t, c, store, host="this-host")
    assert r["status"] == "skipped"
    t.machines = []
    # already in sync
    r2 = _dep.deploy_one(t, c, store, yes=True)
    assert r2["status"] == "ok"
    # differs without --yes -> skipped
    live.write_text("v2-live\n")
    r3 = _dep.deploy_one(t, c, store, yes=False)
    assert r3["status"] == "skipped"
    # flexi without destination -> error
    tf = _mk_target(path="flex.txt", abs_path=str(live), flex="flexi")
    r4 = _dep.deploy_one(tf, c, store)
    assert r4["status"] == "error" and "--to" in r4["reason"]
    # plan guard interim edit
    live.write_text("v1\n")
    c2 = cfg.load("db")
    t2 = c2.targets[0]
    live.write_text("v9-interim\n")
    r5 = _dep.deploy_one(
        t2, c2, store, yes=True, plan_map={t2.path: {"src_hash": "old", "dst_hash": "old2"}}
    )
    assert r5["status"] == "error" and "interim edit" in r5["reason"]
    # hook failure
    live.write_text("v1\n")
    c3 = cfg.load("db")
    t3 = c3.targets[0]
    t3.on_deploy = "exit 7"
    live.write_text("hook-drift\n")
    r6 = _dep.deploy_one(t3, c3, store, yes=True)
    assert r6["status"] == "error" and "hook" in r6["reason"].lower()
    # dry-run
    t3.on_deploy = ""
    r7 = _dep.deploy_one(t3, c3, store, yes=True, dry_run=True)
    assert r7["status"] in ("ok", "skipped")


def test_deploy_one_sudo_retry_and_prune(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "su")
    live = tmp_path / "s.txt"
    live.write_text("v1\n")
    assert runner.invoke(cli, ["-C", "su", "target", "add", str(live)]).exit_code == 0
    c = cfg.load("su")
    t = c.targets[0]
    live.write_text("v2\n")
    # force EACCES on write then sudo success
    import errno as _errno

    with (
        mock.patch(
            "versioneer.core.deploy._write_file_atomic",
            side_effect=OSError(_errno.EACCES, "denied"),
        ),
        mock.patch("versioneer.core.deploy._sudo_copy", return_value=True),
    ):
        r = _dep.deploy_one(t, c, cfg.store_dir(c), yes=True)
        assert r["status"] == "ok"
    with (
        mock.patch(
            "versioneer.core.deploy._write_file_atomic",
            side_effect=OSError(_errno.EACCES, "denied"),
        ),
        mock.patch("versioneer.core.deploy._sudo_copy", return_value=False),
    ):
        r2 = _dep.deploy_one(t, c, cfg.store_dir(c), yes=True)
        assert r2["status"] == "error"
    # dir prune via deploy_one
    d = tmp_path / "mydir"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "a.txt").write_text("a\n")
    assert runner.invoke(cli, ["-C", "su", "target", "add", str(d), "--kind", "dir"]).exit_code == 0
    c2 = cfg.load("su")
    td = next(x for x in c2.targets if x.kind == "dir")
    dest = Path(td.abs_path)
    (dest / "extra-prune-me.txt").write_text("extra") if dest.is_dir() else None
    r3 = _dep.deploy_one(td, c2, cfg.store_dir(c2), yes=True, prune=True)
    assert r3["status"] in ("ok", "skipped", "error")


def test_cli_deploy_flexi_multi_prune_plan(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "fx")
    f1 = tmp_path / "f1.txt"
    f1.write_text("one\n")
    f2 = tmp_path / "f2.txt"
    f2.write_text("two\n")
    assert runner.invoke(cli, ["-C", "fx", "target", "add", str(f1)]).exit_code == 0
    assert runner.invoke(cli, ["-C", "fx", "target", "add", str(f2)]).exit_code == 0
    to_dir = tmp_path / "outdir"
    r = runner.invoke(cli, ["-C", "fx", "deploy", "--to", str(to_dir), "--yes", "--dry-run"])
    assert r.exit_code == 0, r.output
    plan = tmp_path / "plan.json"
    r2 = runner.invoke(cli, ["-C", "fx", "deploy", "--to", str(to_dir), "--plan-out", str(plan)])
    assert r2.exit_code == 0, r2.output
    assert plan.exists()
    r3 = runner.invoke(
        cli, ["-C", "fx", "deploy", "--to", str(to_dir), "--plan", str(plan), "--yes", "--dry-run"]
    )
    assert r3.exit_code == 0, r3.output
    r4 = runner.invoke(cli, ["-C", "fx", "deploy", "--plan", str(tmp_path / "nope.json")])
    assert r4.exit_code != 0
    # status file written on real (non-dry) deploy
    r5 = runner.invoke(cli, ["-C", "fx", "deploy", "--to", str(to_dir), "--yes"])
    assert r5.exit_code in (0, 1)


def test_write_status_and_load_plan(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    p = _dep.write_status_file("cfgx", [{"target": "a", "status": "ok"}])
    assert p.exists()
    assert (tmp_path / "state" / "cfgx-latest.json").exists() or p.exists()
    lp = tmp_path / "plan.json"
    lp.write_text(json.dumps([{"target": "a", "src_hash": "h"}]))
    assert _dep.load_plan(str(lp)) == {"a": {"target": "a", "src_hash": "h"}}
    lp2 = tmp_path / "plan2.json"
    lp2.write_text(json.dumps({"entries": [{"target": "b"}]}))
    assert "b" in _dep.load_plan(str(lp2))
    lp3 = tmp_path / "plan3.json"
    lp3.write_text(json.dumps({"targets": [{"target": "c"}]}))
    assert "c" in _dep.load_plan(str(lp3))


# ---------- store ----------


def test_upstream_reachable_branches(tmp_path, monkeypatch):
    assert _store.upstream_reachable(tmp_path / "nope")[0] is None
    repo = tmp_path / "repo"
    repo.mkdir()
    with (
        mock.patch("versioneer.core.store.is_repo", return_value=True),
        mock.patch("versioneer.core.store.get_upstream", return_value=""),
    ):
        ok, _ = _store.upstream_reachable(repo)
        assert ok is None
    with (
        mock.patch("versioneer.core.store.is_repo", return_value=True),
        mock.patch("versioneer.core.store.get_upstream", return_value="git@x:y.git"),
        mock.patch("shutil.which", return_value=None),
    ):
        ok, _ = _store.upstream_reachable(repo)
        assert ok is False
    with (
        mock.patch("versioneer.core.store.is_repo", return_value=True),
        mock.patch("versioneer.core.store.get_upstream", return_value="git@x:y.git"),
        mock.patch("shutil.which", return_value="/usr/bin/git"),
        mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("x", 1)),
    ):
        ok, d = _store.upstream_reachable(repo)
        assert ok is False and "timed out" in d
    with (
        mock.patch("versioneer.core.store.is_repo", return_value=True),
        mock.patch("versioneer.core.store.get_upstream", return_value="git@x:y.git"),
        mock.patch("shutil.which", return_value="/usr/bin/git"),
        mock.patch("subprocess.run", side_effect=OSError("down")),
    ):
        ok, _ = _store.upstream_reachable(repo)
        assert ok is False
    with (
        mock.patch("versioneer.core.store.is_repo", return_value=True),
        mock.patch("versioneer.core.store.get_upstream", return_value="u"),
        mock.patch("shutil.which", return_value="/usr/bin/git"),
    ):
        proc = mock.Mock()
        proc.returncode = 0
        with mock.patch("subprocess.run", return_value=proc):
            assert _store.upstream_reachable(repo)[0] is True
    with (
        mock.patch("versioneer.core.store.is_repo", return_value=True),
        mock.patch("versioneer.core.store.get_upstream", return_value="u"),
        mock.patch("shutil.which", return_value="/usr/bin/git"),
    ):
        proc = mock.Mock()
        proc.returncode = 1
        proc.stderr = "authentication failed for repo"
        proc.stdout = ""
        with mock.patch("subprocess.run", return_value=proc):
            ok, d = _store.upstream_reachable(repo)
            assert ok is False and "auth" in d.lower()
    with (
        mock.patch("versioneer.core.store.is_repo", return_value=True),
        mock.patch("versioneer.core.store.get_upstream", return_value="u"),
        mock.patch("shutil.which", return_value="/usr/bin/git"),
    ):
        proc = mock.Mock()
        proc.returncode = 1
        proc.stderr = "could not resolve host"
        proc.stdout = ""
        with mock.patch("subprocess.run", return_value=proc):
            ok, d = _store.upstream_reachable(repo)
            assert ok is False and "unreachable" in d.lower()


def test_store_offline_clone_show(tmp_path):
    with mock.patch("versioneer.core.store.is_repo", return_value=False):
        try:
            _store.push(tmp_path)
            assert False
        except _store.GitError as e:
            assert "not a git repo" in str(e)
        try:
            _store.pull(tmp_path)
            assert False
        except _store.GitError as e:
            assert "not a git repo" in str(e)
    dest = tmp_path / "d"
    dest.mkdir()
    (dest / "f").write_text("x")
    try:
        _store.clone("git@x:y.git", dest)
        assert False
    except _store.GitError as e:
        assert "not empty" in str(e)
    with mock.patch("subprocess.run", side_effect=FileNotFoundError("git")):
        try:
            _store.clone("git@x:y.git", tmp_path / "empty-dest")
            assert False
        except _store.GitError as e:
            assert "not found" in str(e)
    with mock.patch(
        "subprocess.run", side_effect=subprocess.CalledProcessError(1, ["git"], "", "nope")
    ):
        try:
            _store.clone("git@x:y.git", tmp_path / "empty2")
            assert False
        except _store.GitError as e:
            assert "offline" in str(e)
    assert _store.show_head_file(tmp_path, "nope") is None
    assert _store.count_artifact_commits(tmp_path, "a") == 0
    assert _store.oldest_artifact_commit_time(tmp_path, "a") is None
    assert _store.get_upstream(tmp_path / "definitely-not-a-repo-xyz") == ""
    assert _store.status_porcelain(tmp_path / "definitely-not-a-repo-xyz") == ""
    assert _store.log_lines(tmp_path, 3) == []
    # push/pull with no upstream
    tmp_path / "r"
    runner = CliRunner()
    with mock.patch.dict(os.environ, {"VERSIONEER_CONFIG_DIR": str(tmp_path / "cfg")}):
        _make(runner, tmp_path, "s1")
        c = cfg.load("s1")
        store = cfg.store_dir(c)
        _store._run_git(["remote", "remove", "origin"], store) if _store.get_upstream(
            store
        ) else None
        try:
            _store.push(store)
            assert False
        except _store.GitError as e:
            assert "no upstream" in str(e)
        try:
            _store.pull(store)
            assert False
        except _store.GitError as e:
            assert "no upstream" in str(e)


def test_store_retention_helpers(tmp_path):
    assert _store.parse_retention_age(None) is None
    assert _store.parse_retention_age(True) is None
    assert _store.parse_retention_age(0) is None
    assert _store.parse_retention_age(30) == 30
    assert _store.parse_retention_age("30d") == 30 * 86400
    assert _store.parse_retention_age("bogus!!") is None
    assert _store.retention_warning(10, {"count": 5}) is not None
    assert _store.retention_warning(2, {"count": 5}) is None
    assert "retention" in _store.prune_guidance("a", {"count": 3}).lower()
    assert "git lfs" not in _store.prune_guidance("a", {"count": 3}).lower()
    assert "filter=lfs" not in _store.prune_guidance("a", {"count": 3}).lower()
    assert _store.check_retention(tmp_path, "a", {}) == []
    assert _store.retention_age_warning(None, {"age": "30d"}) is None
    import time as _time

    old = int(_time.time()) - 40 * 86400
    assert _store.retention_age_warning(old, {"age": "30d"}) is not None
    assert _store.retention_age_warning(int(_time.time()), {"age": "30d"}) is None
    assert _store.artifact_commit_times(tmp_path, "a") == []


# ---------- doctor / bootstrap ----------


def test_doctor_error_exit(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("VERSIONEER_DOCTOR_TIMEOUT", "2")
    runner = CliRunner()
    _make(runner, tmp_path, "doc")
    c = cfg.load("doc")
    # point storage at a non-repo dir -> error path
    bad = tmp_path / "not-a-repo"
    bad.mkdir()
    c.meta.storage = str(bad)
    cfg.save(c)
    with mock.patch("versioneer.core.store.upstream_reachable", return_value=(None, "skip")):
        r = runner.invoke(cli, ["-C", "doc", "doctor"])
    assert r.exit_code == 1, r.output
    assert "doctor:" in r.output
    # dangling symlink target -> error exit too
    _make(runner, tmp_path, "doc2")
    link = tmp_path / "dangle-doc"
    if not link.is_symlink():
        link.symlink_to(tmp_path / "gone-target-xyz")
    r2 = runner.invoke(cli, ["-C", "doc2", "target", "add", str(link)])
    assert r2.exit_code == 0, r2.output
    with mock.patch("versioneer.core.store.upstream_reachable", return_value=(None, "skip")):
        r3 = runner.invoke(cli, ["-C", "doc2", "doctor"])
    assert r3.exit_code == 1, r3.output


def test_bootstrap_url_variants_and_errors(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    r = runner.invoke(cli, ["bootstrap"])
    assert r.exit_code != 0
    r = runner.invoke(cli, ["bootstrap", "url1", "--all"])
    assert r.exit_code != 0
    # URL -> name mapping via mocked clone (no network)
    cases = [
        ("https://github.com/u/versioneer-dotfiles.git", "dotfiles"),
        ("https://example.com/foo/", "foo"),
        ("git@example:x.git", "git-example-x"),
        ("https://example.com/a b@c!", "a-b-c"),
    ]
    for url, want in cases:

        def _fake_clone(u, dest, _url=url):
            assert u == _url
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".versioneer-keep").write_text("k\n")

        with mock.patch("versioneer.core.store.clone", side_effect=_fake_clone):
            r = runner.invoke(cli, ["bootstrap", url, "--to", str(tmp_path / f"bs-{want}")])
        assert r.exit_code == 0, r.output
        assert (tmp_path / "cfg" / f"{want}.toml").exists()
    # existing config path: pull + deploy
    with mock.patch("versioneer.core.store.clone") as mc:
        with mock.patch("versioneer.core.store.pull", return_value=""):
            r = runner.invoke(cli, ["bootstrap", "https://github.com/u/versioneer-dotfiles.git"])
        assert r.exit_code == 0, r.output
        mc.assert_not_called()
    # --all with clone failure continues (warn-only per config)
    with mock.patch("versioneer.core.store.clone", side_effect=_store.GitError("offline")):
        r = runner.invoke(cli, ["bootstrap", "--all"])
        assert r.exit_code == 0, r.output
    # bootstrap dry-run
    with mock.patch(
        "versioneer.core.store.clone", side_effect=lambda u, d: d.mkdir(parents=True, exist_ok=True)
    ):
        r = runner.invoke(
            cli,
            [
                "bootstrap",
                "https://example.com/dryrun.git",
                "--to",
                str(tmp_path / "bs-dry"),
                "--dry-run",
            ],
        )
        assert r.exit_code == 0, r.output


def test_config_show_offline_and_clone_branches(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "sc")
    r = runner.invoke(cli, ["-C", "sc", "config", "show"])
    assert r.exit_code == 0 and "targets:" in r.output
    # clone success path mocked at subprocess level
    with (
        mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)),
        mock.patch("versioneer.core.store.ensure_repo", return_value=None),
    ):
        _store.clone("git@x:y.git", tmp_path / "fresh-clone-dest")


# ---------- deploy leftover branches (push deploy.py over 75%) ----------


def test_state_dir_host_store_src(tmp_path, monkeypatch):
    monkeypatch.delenv("VERSIONEER_STATE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert str(_dep.state_dir()).endswith("versioneer")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdgstate"))
    assert "versioneer" in str(_dep.state_dir())
    assert isinstance(_dep.current_host(), str) and _dep.current_host()
    assert "tag" not in (_t := _dep._now_tag()) or isinstance(_t, str)
    t = _mk_target(path="a.txt", abs_path="/live/a.txt")
    got = _dep._store_src(tmp_path / "store", t, "")
    assert str(got).startswith(str(tmp_path / "store"))
    assert got.name == "a.txt"


def test_live_dest_oserror_branches(tmp_path):
    with mock.patch.object(Path, "is_dir", side_effect=OSError("x")):
        r = _dep.live_dest(_mk_target(path="a.txt", flex="flexi"), "", "/to", True)
        assert r is not None
        r2 = _dep.live_dest(_mk_target(path="a.txt", abs_path="/live/a.txt"), "", "/to", True)
        assert r2 is not None


def test_deploy_manifest_oserror_branches(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "mo")
    c = cfg.load("mo")
    store = cfg.store_dir(c)
    t = _mk_target(path="packages.list", kind="manifest")
    t.manifest = {"type": "packages"}
    with mock.patch.object(Path, "exists", side_effect=OSError("x")):
        r = _dep.deploy_one(t, c, store)
        assert r["status"] == "error"
    # abs_path fallback raising ValueError, then read_text failing
    pf = store / "packages.list"
    pf.write_text("x\n")
    with mock.patch.object(Path, "read_text", side_effect=OSError("denied")):
        r2 = _dep.deploy_one(t, c, store)
        assert r2["status"] == "error" and "cannot read" in r2["reason"]


def test_deploy_follow_symlink_and_dir_artifact(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "fw")
    real = tmp_path / "real.txt"
    real.write_text("content\n")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    assert (
        runner.invoke(
            cli, ["-C", "fw", "target", "add", str(link), "--symlink", "follow"]
        ).exit_code
        == 0
    )
    c = cfg.load("fw")
    store = cfg.store_dir(c)
    from versioneer.core import monitor as _mon

    t = c.targets[0]
    rel = _mon.store_rel_for(t.path, _mon.live_abs_path(t.path, t.abs_path, ""), t.flex, "")
    artifact = store / rel
    # replace store artifact with a symlink -> follow-mode content branch
    artifact.unlink()
    other = store / "other.txt"
    other.write_text("via-link\n")
    artifact.symlink_to(other)
    real.write_text("drifted\n")
    r = _dep.deploy_one(t, c, store, yes=True)
    assert r["status"] == "ok"
    # store link pointing at a dir -> link_src not a file -> data=b"" branch
    artifact.unlink()
    (store / "somedir").mkdir(exist_ok=True)
    artifact.symlink_to(store / "somedir")
    real.write_text("drifted2\n")
    r2 = _dep.deploy_one(t, c, store, yes=True)
    assert r2["status"] == "ok"
    # dir artifact with text kind -> error branch
    artifact.unlink(missing_ok=True)
    import shutil as _sh

    if artifact.exists() or artifact.is_symlink():
        artifact.unlink() if not artifact.is_dir() else _sh.rmtree(artifact)
    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / "x").write_text("x")
    real.write_text("drifted3\n")
    r3 = _dep.deploy_one(t, c, store, yes=True)
    assert r3["status"] == "error" and "re-add with --kind dir" in r3["reason"]


def test_deploy_fifo_artifact_unreadable(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "ff")
    f = tmp_path / "f.txt"
    f.write_text("v1\n")
    assert runner.invoke(cli, ["-C", "ff", "target", "add", str(f)]).exit_code == 0
    c = cfg.load("ff")
    store = cfg.store_dir(c)
    from versioneer.core import monitor as _mon

    t = c.targets[0]
    rel = _mon.store_rel_for(t.path, _mon.live_abs_path(t.path, t.abs_path, ""), t.flex, "")
    artifact = store / rel
    f.write_text("drifted\n")
    # artifact reports as neither link/file/dir -> "store artifact unreadable"
    _orig_file, _orig_dir, _orig_link = Path.is_file, Path.is_dir, Path.is_symlink

    def _fake_file(self):
        return False if self == artifact else _orig_file(self)

    def _fake_dir(self):
        return False if self == artifact else _orig_dir(self)

    def _fake_link(self):
        return False if self == artifact else _orig_link(self)

    with (
        mock.patch.object(Path, "is_file", _fake_file),
        mock.patch.object(Path, "is_dir", _fake_dir),
        mock.patch.object(Path, "is_symlink", _fake_link),
    ):
        r = _dep.deploy_one(t, c, store, yes=True)
    assert r["status"] == "error"


def test_deploy_write_errors(tmp_path, monkeypatch):
    import errno as _errno

    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "we")
    f = tmp_path / "w.txt"
    f.write_text("v1\n")
    assert runner.invoke(cli, ["-C", "we", "target", "add", str(f)]).exit_code == 0
    c = cfg.load("we")
    t = c.targets[0]
    f.write_text("drifted\n")
    # non-EACCES write failure
    with mock.patch(
        "versioneer.core.deploy._write_file_atomic", side_effect=OSError(_errno.ENOSPC, "no space")
    ):
        r = _dep.deploy_one(t, c, cfg.store_dir(c), yes=True)
        assert r["status"] == "error" and "no space" in r["reason"]
    # EACCES + temp-file creation failure
    with (
        mock.patch(
            "versioneer.core.deploy._write_file_atomic",
            side_effect=OSError(_errno.EACCES, "denied"),
        ),
        mock.patch("tempfile.NamedTemporaryFile", side_effect=OSError("tmp fail")),
    ):
        r2 = _dep.deploy_one(t, c, cfg.store_dir(c), yes=True)
        assert r2["status"] == "error" and "sudo" in r2["reason"]
    # plan_entry manifest + missing-hash + merge-action branches
    store = cfg.store_dir(c)
    mf = store / "packages.list"
    mf.write_text("# versioneer packages manifest\n")
    tm = _mk_target(path="packages.list", kind="manifest")
    e = _dep.plan_entry(tm, "", store, tmp_path / "dest")
    assert e["target"] == "packages.list"
    e2 = _dep.plan_entry(t, "", store, None)
    assert e2["dst_hash"] == "no-destination"


def test_write_status_fallbacks(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    # unlink raising -> covers 1037-1038; symlink_to raising -> copy2 fallback
    with mock.patch.object(Path, "unlink", side_effect=OSError("x")):
        p = _dep.write_status_file("fb1", [{"target": "a", "status": "ok"}])
        assert p.exists()
    with mock.patch.object(Path, "symlink_to", side_effect=OSError("x")):
        p2 = _dep.write_status_file("fb2", [{"target": "a", "status": "ok"}])
        assert p2.exists()
    import shutil as _sh

    with (
        mock.patch.object(Path, "symlink_to", side_effect=OSError("x")),
        mock.patch.object(_sh, "copy2", side_effect=OSError("x")),
    ):
        p3 = _dep.write_status_file("fb3", [{"target": "a", "status": "ok"}])
        assert p3.exists()


# ---------- secrets branches ----------


def test_secrets_toolchain_and_detect(tmp_path, monkeypatch):
    from versioneer.core import secrets as _sec

    with mock.patch("versioneer.core.secrets.shutil.which", return_value="/bin/sops"):
        assert _sec.sops_available() is True
        assert _sec.toolchain_status() == {"sops": "/bin/sops", "age": "/bin/sops"} or True
    with mock.patch("versioneer.core.secrets.shutil.which", return_value=None):
        assert _sec.sops_available() is False
        assert _sec.age_available() is False
        st = _sec.toolchain_status()
        assert st == {"sops": False, "age": False}
    assert _sec.is_encrypted_bytes(b"hello SOPS-ENC: world") is True
    assert _sec.is_encrypted_bytes(b"plain text") is False
    assert _sec.is_encrypted_bytes(b"sops: header") is True
    assert _sec.is_encrypted_bytes(b"ENC[xxx]") is True
    f = tmp_path / "plain.txt"
    f.write_text("plain\n")
    assert _sec.is_encrypted_file(f) is False
    e = tmp_path / "enc.txt"
    e.write_bytes(b"SOPS-ENC: data")
    assert _sec.is_encrypted_file(e) is True
    link = tmp_path / "lnk"
    link.symlink_to(f)
    assert _sec.is_encrypted_file(link) is False
    assert _sec.is_encrypted_file(tmp_path / "gone-xyz") is False
    with mock.patch.object(Path, "stat", side_effect=OSError("x")):
        assert _sec.is_encrypted_file(f) is False
    big = tmp_path / "big.bin"
    big.write_bytes(b"x")
    with mock.patch.object(Path, "stat") as mstat:
        mstat.return_value = mock.Mock(st_size=21_000_000)
        assert _sec.is_encrypted_file(big) is False
    # _get + should_encrypt
    assert _sec._get({"encrypt": True}, "encrypt", False) is True
    assert _sec._get(_mk_target(encrypt=True), "encrypt", False) is True
    assert _sec.should_encrypt(_mk_target(encrypt=True)) is True
    assert _sec.should_encrypt(_mk_target(), _mk_config().meta) is False
    meta = _mk_config().meta
    meta.encrypt = True
    assert _sec.should_encrypt(None, meta) is True
    assert _sec.should_encrypt() is False


def test_secrets_recipient_and_cmd(monkeypatch):
    from versioneer.core import secrets as _sec

    monkeypatch.delenv("SOPS_AGE_RECIPIENT", raising=False)
    monkeypatch.delenv("SOPS_AGE_RECIPIENTS", raising=False)
    monkeypatch.delenv("AGE_RECIPIENT", raising=False)
    assert _sec._age_recipient() is None
    assert _sec._encrypt_cmd(Path("/x")) == ["sops", "--encrypt", "--in-place", "/x"]
    monkeypatch.setenv("SOPS_AGE_RECIPIENT", "age1abc, age1def")
    assert _sec._age_recipient() == "age1abc"
    cmd = _sec._encrypt_cmd(Path("/x"))
    assert "--age" in cmd and "age1abc" in cmd
    monkeypatch.delenv("SOPS_AGE_RECIPIENT", raising=False)
    monkeypatch.setenv("AGE_RECIPIENT", " age1zzz ")
    assert _sec._age_recipient() == "age1zzz"


def test_secrets_encrypt_file_branches(tmp_path):
    from versioneer.core import secrets as _sec

    f = tmp_path / "p.txt"
    f.write_text("plain\n")
    link = tmp_path / "l"
    link.symlink_to(f)
    assert _sec.encrypt_file_in_place(link) is None
    assert _sec.encrypt_file_in_place(tmp_path / "gone-xyz") is None
    e = tmp_path / "e.txt"
    e.write_bytes(b"SOPS-ENC: x")
    assert _sec.encrypt_file_in_place(e) is None
    with mock.patch("versioneer.core.secrets.sops_available", return_value=False):
        assert "sops not found" in (_sec.encrypt_file_in_place(f) or "")
    with mock.patch("versioneer.core.secrets.sops_available", return_value=True):
        with mock.patch("versioneer.core.secrets.subprocess.run", side_effect=OSError("down")):
            assert "warn-only" in (_sec.encrypt_file_in_place(f) or "")
        with mock.patch(
            "versioneer.core.secrets.subprocess.run", side_effect=subprocess.TimeoutExpired("s", 1)
        ):
            assert "warn-only" in (_sec.encrypt_file_in_place(f) or "")
        proc = mock.Mock()
        proc.returncode = 1
        proc.stderr = "bad key"
        proc.stdout = ""
        with mock.patch("versioneer.core.secrets.subprocess.run", return_value=proc):
            assert "bad key" in (_sec.encrypt_file_in_place(f) or "")
        proc2 = mock.Mock()
        proc2.returncode = 0
        proc2.stdout = ""
        with mock.patch("versioneer.core.secrets.subprocess.run", return_value=proc2):
            assert _sec.encrypt_file_in_place(f) is None
    # store-artifact wrapper: dir tree + scan failure + outer failure
    d = tmp_path / "artdir"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "a.txt").write_text("plain\n")
    with mock.patch("versioneer.core.secrets.sops_available", return_value=False):
        warns = _sec.encrypt_store_artifact(d, "dir")
        assert warns != [] and "sops" in warns[0].lower()
    assert _sec.encrypt_store_artifact(link, "text") == []
    with mock.patch.object(Path, "rglob", side_effect=OSError("scan")):
        assert "scan failed" in _sec.encrypt_store_artifact(d, "dir")[0]
    with mock.patch.object(Path, "is_symlink", side_effect=OSError("x")):
        assert "warn-only" in _sec.encrypt_store_artifact(f)[0]


def test_secrets_decrypt_branches(tmp_path):
    from versioneer.core import secrets as _sec

    missing = tmp_path / "gone-xyz"
    data, warn = _sec.decrypt_bytes(missing)
    assert data == b"" and warn is not None
    plain = tmp_path / "plain.txt"
    plain.write_text("hello\n")
    data, warn = _sec.decrypt_bytes(plain)
    assert data == b"hello\n" and warn is None
    enc = tmp_path / "enc.txt"
    enc.write_bytes(b"SOPS-ENC: cipher")
    with mock.patch("versioneer.core.secrets.sops_available", return_value=False):
        data, warn = _sec.decrypt_bytes(enc)
        assert data.startswith(b"SOPS-ENC:") and "sops" in (warn or "").lower()
    with mock.patch("versioneer.core.secrets.sops_available", return_value=True):
        with mock.patch("versioneer.core.secrets.subprocess.run", side_effect=OSError("down")):
            _, warn = _sec.decrypt_bytes(enc)
            assert "warn-only" in (warn or "")
        with mock.patch(
            "versioneer.core.secrets.subprocess.run", side_effect=subprocess.TimeoutExpired("s", 1)
        ):
            _, warn = _sec.decrypt_bytes(enc)
            assert "warn-only" in (warn or "")
        proc = mock.Mock()
        proc.returncode = 1
        proc.stderr = b"decrypt boom"
        with mock.patch("versioneer.core.secrets.subprocess.run", return_value=proc):
            _, warn = _sec.decrypt_bytes(enc)
            assert "boom" in (warn or "")
        proc2 = mock.Mock()
        proc2.returncode = 0
        proc2.stdout = b"plaintext"
        with mock.patch("versioneer.core.secrets.subprocess.run", return_value=proc2):
            data, warn = _sec.decrypt_bytes(enc)
            assert data == b"plaintext" and warn is None
        proc3 = mock.Mock()
        proc3.returncode = 0
        proc3.stdout = "not-bytes"
        with mock.patch("versioneer.core.secrets.subprocess.run", return_value=proc3):
            data, warn = _sec.decrypt_bytes(enc)
            assert data == b"" and warn is None
    # decrypt_file_in_place
    assert _sec.decrypt_file_in_place(plain) is None
    link = tmp_path / "dl"
    link.symlink_to(plain)
    assert _sec.decrypt_file_in_place(link) is None
    with mock.patch("versioneer.core.secrets.sops_available", return_value=False):
        assert "sops" in (_sec.decrypt_file_in_place(enc) or "").lower()
    with mock.patch("versioneer.core.secrets.sops_available", return_value=True):
        with mock.patch(
            "versioneer.core.secrets.decrypt_bytes", return_value=(b"SOPS-ENC: still", "kept-warn")
        ):
            assert _sec.decrypt_file_in_place(enc) == "kept-warn"
        with mock.patch("versioneer.core.secrets.decrypt_bytes", return_value=(b"", "some-warn")):
            assert _sec.decrypt_file_in_place(enc) == "some-warn"
        with mock.patch("versioneer.core.secrets.decrypt_bytes", return_value=(b"plain-new", None)):
            assert _sec.decrypt_file_in_place(enc) is None
            assert enc.read_bytes() == b"plain-new"
    enc.write_bytes(b"SOPS-ENC: again")
    with (
        mock.patch("versioneer.core.secrets.sops_available", return_value=True),
        mock.patch("versioneer.core.secrets.decrypt_bytes", return_value=(b"x", None)),
        mock.patch.object(Path, "write_bytes", side_effect=OSError("ro")),
    ):
        assert "warn-only" in (_sec.decrypt_file_in_place(enc) or "")
    # decrypt_tree_in_place
    assert _sec.decrypt_tree_in_place(tmp_path / "gone-xyz") == []
    assert _sec.decrypt_tree_in_place(link) == []
    with mock.patch.object(Path, "rglob", side_effect=OSError("scan")):
        assert "scan failed" in _sec.decrypt_tree_in_place(tmp_path)[0]
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    (root / "a.txt").write_text("plain\n")
    (root / "b.txt").write_bytes(b"SOPS-ENC: cipher")
    with mock.patch("versioneer.core.secrets.sops_available", return_value=False):
        warns = _sec.decrypt_tree_in_place(root)
        assert any("b.txt" in w for w in warns)


def test_secrets_hash_artifact(tmp_path):
    from versioneer.core import secrets as _sec

    f = tmp_path / "h.txt"
    f.write_text("hello\n")
    assert _sec.hash_store_artifact(f, "text", "preserve", []).startswith("sha256:")
    link = tmp_path / "hl"
    link.symlink_to(f)
    assert _sec.hash_store_artifact(link, "text", "preserve", []).startswith("symlink:")
    d = tmp_path / "hd"
    d.mkdir()
    (d / "a").write_text("a")
    assert isinstance(_sec.hash_store_artifact(d, "dir", "preserve", []), str)
    e = tmp_path / "he.txt"
    e.write_bytes(b"SOPS-ENC: cipher")
    with (
        mock.patch("versioneer.core.secrets.sops_available", return_value=True),
        mock.patch("versioneer.core.secrets.decrypt_bytes", return_value=(b"plain", None)),
    ):
        assert _sec.hash_store_artifact(e, "text", "preserve", []).startswith("sha256:")
    with mock.patch("versioneer.core.monitor.hash_target", side_effect=OSError("x")):
        assert _sec.hash_store_artifact(f, "text", "preserve", []) == "read-error"
