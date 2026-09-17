"""Elevation helpers + target-add denied/missing distinction."""

from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from versioneer.cli import cli
from versioneer.core import elevate as elev
from versioneer.core import monitor as mon


def _make_config(runner, tmp_path, name="e"):
    store = tmp_path / f"store-{name}"
    r = runner.invoke(
        cli,
        ["config", "create", "--name", name, "--path", str(store), "--upstream", ""],
    )
    assert r.exit_code == 0, r.output
    return store


def test_classify_missing_denied_exists(tmp_path, monkeypatch):
    missing = tmp_path / "nope.txt"
    assert elev.classify_path(missing) == "missing"
    real = tmp_path / "real.txt"
    real.write_text("x")
    assert elev.classify_path(real) == "exists"
    with mock.patch("os.lstat", side_effect=OSError(13, "Permission denied")):
        assert elev.classify_path(real) == "denied"


def test_suggest_similar(tmp_path):
    (tmp_path / "faillock").write_text("x")
    got = elev.suggest_similar(tmp_path / "faillock.conf")
    assert "faillock" in got


def test_target_add_missing_suggests_similar(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make_config(runner, tmp_path, "sg")
    d = tmp_path / "sec"
    d.mkdir()
    (d / "faillock").write_text("x")
    r = runner.invoke(cli, ["-C", "sg", "target", "add", str(d / "faillock.conf")])
    assert r.exit_code != 0
    assert "does not exist" in r.output
    assert "did you mean" in r.output
    assert "faillock" in r.output


def test_target_add_denied_without_sudo_reports_permission(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    _make_config(runner, tmp_path, "dn")
    f = tmp_path / "root.txt"
    f.write_text("secret")
    with mock.patch.object(
        Path, "is_symlink", lambda self: False
    ), mock.patch(
        "versioneer.core.elevate.classify_path", return_value="denied"
    ), mock.patch(
        "versioneer.core.elevate.sudo_cmd", return_value=None
    ):
        r = runner.invoke(cli, ["-C", "dn", "target", "add", str(f)])
    assert r.exit_code != 0
    assert "permission denied" in r.output.lower()
    assert "does not exist" not in r.output


def test_target_add_denied_elevates_via_sudo(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    store = _make_config(runner, tmp_path, "el")
    f = tmp_path / "shadow-fake"
    f.write_text("old")
    with mock.patch(
        "versioneer.core.elevate.classify_path", return_value="exists"
    ), mock.patch(
        "versioneer.core.permissions.capture",
        side_effect=OSError(13, "Permission denied"),
    ), mock.patch(
        "versioneer.core.monitor.hash_target", return_value="read-error"
    ), mock.patch(
        "versioneer.core.elevate.stat_via_sudo",
        return_value=("root", "root", "0600"),
    ), mock.patch(
        "versioneer.core.elevate.hash_file_elevated",
        return_value="sha256:deadbeef",
    ), mock.patch(
        "versioneer.core.elevate.read_bytes_via_sudo",
        return_value=b"sudo-content\n",
    ), mock.patch("shutil.copy2", side_effect=OSError(13, "Permission denied")):
        r = runner.invoke(cli, ["-C", "el", "target", "add", str(f)])
    assert r.exit_code == 0, r.output
    assert "elevated read via sudo" in r.output
    staged = store / mon.artifact_rel("", f.resolve(strict=False), "fixed", "")
    # artifact_rel for fixed strips leading /; check any staged copy exists
    assert staged.exists()
    assert staged.read_bytes() == b"sudo-content\n"


def test_resolve_input_denied_keeps_abs_path(tmp_path):
    p = tmp_path / "r.txt"
    p.write_text("x")
    with mock.patch("os.lstat", side_effect=OSError(13, "denied")), mock.patch.object(
        Path, "is_symlink", lambda self: False
    ):
        _stored, abs_p = mon.resolve_input(str(p), "")
    assert abs_p == p.absolute() or str(abs_p) == str(p.absolute())


def test_system_shim_missing_shape():
    missing = elev.system_shim_missing()
    assert isinstance(missing, list)
    assert all(m.startswith("/usr/local/bin/") for m in missing)


def test_sudo_vers_hint_mentions_secure_path():
    hint = elev.sudo_vers_hint("-C etc status")
    assert "secure_path" in hint or "preserve-env" in hint.lower() or "HOME" in hint


def test_invoking_user_only_when_root_via_sudo(monkeypatch):
    import os as _os

    with mock.patch.object(_os, "geteuid", return_value=1000):
        monkeypatch.setenv("SUDO_USER", "alice")
        assert elev.invoking_user() is None
    with mock.patch.object(_os, "geteuid", return_value=0):
        monkeypatch.setenv("SUDO_USER", "")
        assert elev.invoking_user() is None
        monkeypatch.setenv("SUDO_USER", "root")
        assert elev.invoking_user() is None
        monkeypatch.setenv("SUDO_USER", "alice")
        assert elev.invoking_user() == "alice"


def test_effective_home_follows_sudo_user(tmp_path, monkeypatch):
    import os as _os

    fake_home = tmp_path / "alice-home"
    fake_home.mkdir()
    with mock.patch.object(_os, "geteuid", return_value=0):
        monkeypatch.setenv("SUDO_USER", "alice")
        with mock.patch("pwd.getpwnam") as m:
            m.return_value.pw_dir = str(fake_home)
            assert elev.effective_home() == fake_home
            assert elev.expand_user("~/versioneer-store/x") == fake_home / "versioneer-store/x"
            assert elev.expand_user("~") == fake_home
            # absolute paths untouched
            assert elev.expand_user("/etc/fstab") == Path("/etc/fstab")
    with mock.patch.object(_os, "geteuid", return_value=1000):
        monkeypatch.delenv("SUDO_USER", raising=False)
        assert elev.effective_home() == Path.home()


def test_config_and_store_dirs_follow_sudo_user(tmp_path, monkeypatch):
    import os as _os

    from versioneer.core import config as _cfg
    from versioneer.core import deploy as _dep

    fake_home = tmp_path / "sudo-alice"
    fake_home.mkdir()
    monkeypatch.delenv("VERSIONEER_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("VERSIONEER_STATE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    with mock.patch.object(_os, "geteuid", return_value=0):
        monkeypatch.setenv("SUDO_USER", "alice")
        with mock.patch("pwd.getpwnam") as m:
            m.return_value.pw_dir = str(fake_home)
            assert _cfg.config_dir() == fake_home / ".config" / "versioneer"
            assert _dep.state_dir() == fake_home / ".local" / "state" / "versioneer"
            conf = _cfg.Config(
                meta=_cfg.Meta(name="n", upstream="", storage="~/versioneer-store/n")
            )
            assert _cfg.store_dir(conf) == fake_home / "versioneer-store" / "n"
            # user-flex resolution follows the invoking user, not /root
            assert _dep.live_dest(
                _cfg.Target(path="x", abs_path=str(fake_home / "x"), flex="user"),
                "",
                "",
                False,
            ) == fake_home / "x"


def test_installer_scripts_are_sudo_aware():
    from pathlib import Path as _P

    repo = _P(__file__).resolve().parents[1]
    install = (repo / "installer" / "install.sh").read_text(encoding="utf-8")
    uninstall = (repo / "installer" / "uninstall.sh").read_text(encoding="utf-8")
    for text in (install, uninstall):
        assert "SUDO_USER" in text
        assert "TARGET_HOME" in text
    # shims are installed by default so `sudo vers` resolves; the
    # whole-installer-with-sudo path still targets the invoking user
    assert "SYSTEM_SHIM=1" in install
    assert "--no-system-shim" in install
    assert "getent passwd" in install
    # installer verifies itself (built-in `ls -l`): venv bins, user
    # links, system shims and PATH resolvability are printed
    assert "==> verify:" in install
    assert "system shim:" in install


def test_fix_ownership_noop_without_sudo(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x")
    assert elev.fix_ownership(f) is False
    assert elev.sudo_transparency_warning() is None


def test_sudo_warning_prefers_user_run(monkeypatch):
    import os as _os

    with mock.patch.object(_os, "geteuid", return_value=0):
        monkeypatch.setenv("SUDO_USER", "alice")
        msg = elev.sudo_transparency_warning()
        assert msg is not None
        assert "prefer plain" in msg
        assert "alice" in msg


def test_fix_store_after_write_chowns_no_follow_symlink(tmp_path, monkeypatch):
    import os as _os

    base = tmp_path / "store"
    (base / ".config").mkdir(parents=True)
    (base / ".config" / "f").write_text("x")
    (base / ".git").mkdir()
    (base / ".git" / "index").write_text("i")
    link = base / "link"
    try:
        link.symlink_to("/etc/hosts")
    except OSError:
        pass
    calls: list = []

    def _fake_chown(p, uid, gid, follow_symlinks=True):
        calls.append((str(p), uid, gid, follow_symlinks))

    fake_pw = type("P", (), {"pw_uid": 1000, "pw_gid": 1000})()
    with mock.patch.object(_os, "geteuid", return_value=0):
        monkeypatch.setenv("SUDO_USER", "alice")
        with (
            mock.patch("pwd.getpwnam", return_value=fake_pw),
            mock.patch("os.chown", side_effect=_fake_chown),
        ):
            elev.fix_store_after_write(base, [".config/f"])
    assert calls, "expected chown calls under sudo"
    assert all(c[3] is False for c in calls)
    assert any(str(base / ".config") == c[0] for c in calls)  # parent chain
    assert any(".git" in c[0] for c in calls)
    assert not any(c[0] == "/etc/hosts" for c in calls)
