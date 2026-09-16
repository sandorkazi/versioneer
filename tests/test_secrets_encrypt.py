"""ITEM 2: sops/age encrypt=true — scan warn, round-trip, degrade, deploy decrypt."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from click.testing import CliRunner

from versioneer.cli import cli
from versioneer.core import config as cfg

MARK = b"SOPS-ENC:"


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("VERSIONEER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("VERSIONEER_STATE_DIR", str(tmp_path / "state"))


def _make(runner, tmp_path, name="sec"):
    r = runner.invoke(cli, ["config", "create", "--name", name,
                            "--path", str(tmp_path / f"store-{name}"),
                            "--upstream", "git@example:x.git"])
    assert r.exit_code == 0, r.output
    return r


def _patch_sops_available(monkeypatch, available: bool):
    orig_which = shutil.which

    def _which(cmd, *a, **k):
        if cmd in ("sops", "age"):
            return "/fake/sops" if available else None
        return orig_which(cmd, *a, **k)

    monkeypatch.setattr(shutil, "which", _which)
    # secrets imports shutil module object, so patching shutil.which is enough


def _patch_sops_transform(monkeypatch):
    """Fake sops that prepends/strips MARK; delegates non-sops to real run."""
    import versioneer.core.secrets as sec

    assert sec is not None  # imported after implementation exists
    real_run = subprocess.run

    def _fake_run(cmd, *args, **kwargs):
        prog = cmd[0] if isinstance(cmd, (list, tuple)) else str(cmd).split()[0]
        base = prog.split("/")[-1]
        if base != "sops":
            return real_run(cmd, *args, **kwargs)
        # --encrypt --in-place FILE
        if "--encrypt" in cmd:
            target = Path(cmd[-1])
            raw = target.read_bytes()
            if MARK not in raw:
                target.write_bytes(MARK + raw)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        # --decrypt FILE (stdout) or --decrypt --in-place FILE
        if "--decrypt" in cmd:
            if "--in-place" in cmd:
                target = Path(cmd[-1])
                raw = target.read_bytes()
                if raw.startswith(MARK):
                    target.write_bytes(raw[len(MARK):])
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            target = Path(cmd[-1])
            raw = target.read_bytes()
            out = raw.removeprefix(MARK)
            # honor text=True callers
            if kwargs.get("text"):
                return subprocess.CompletedProcess(cmd, 0, stdout=out.decode(), stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr=b"")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="usage")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    _patch_sops_available(monkeypatch, True)


def test_scan_warn_reports_secret(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from versioneer.core import lint as _lint

    f = tmp_path / "tok.conf"
    f.write_text('api_key = "supersecretpassword12345"\n-----BEGIN RSA PRIVATE KEY-----\n')
    warnings = _lint.scan_file(f)
    assert warnings, "secret scan should warn on token/key material"
    assert any("secret scan" in w for w in warnings)


def test_encrypt_roundtrip_with_mocked_sops(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from versioneer.core import secrets as sec

    _patch_sops_transform(monkeypatch)
    assert sec.sops_available() is True
    f = tmp_path / "secret.txt"
    f.write_bytes(b"hello-secret\n")
    warn = sec.encrypt_file_in_place(f)
    assert warn is None, f"encrypt should succeed with fake sops, got: {warn}"
    assert sec.is_encrypted_file(f), "store artifact should carry sops marker after encrypt"
    data, dwarn = sec.decrypt_bytes(f)
    assert dwarn is None, f"decrypt should succeed, got: {dwarn}"
    assert data == b"hello-secret\n"


def test_missing_binary_degrades_warn_only(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from versioneer.core import secrets as sec

    _patch_sops_available(monkeypatch, False)
    assert sec.sops_available() is False
    f = tmp_path / "plain.txt"
    f.write_bytes(b"plain-data\n")
    warn = sec.encrypt_file_in_place(f)
    assert isinstance(warn, str) and "sops" in warn.lower(), "missing sops must warn"
    assert f.read_bytes() == b"plain-data\n", "file must stay plaintext on degrade"
    # decrypt path also degrades without raising
    data, dwarn = sec.decrypt_bytes(f)
    assert data == b"plain-data\n"
    # plaintext file -> no decrypt warning needed (or warn-only); never raises
    assert dwarn is None or isinstance(dwarn, str)
    # encrypted-looking file without binary -> warn + raw bytes, never block
    f.write_bytes(MARK + b"cipher\n")
    data2, dwarn2 = sec.decrypt_bytes(f)
    assert isinstance(dwarn2, str) and "sops" in dwarn2.lower()
    assert data2 == MARK + b"cipher\n"


def test_deploy_decrypt_path(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from versioneer.core import secrets as sec

    _patch_sops_transform(monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "enc")
    live = tmp_path / "secret.conf"
    live.write_text("token=abc123\n")
    r = runner.invoke(cli, ["-C", "enc", "target", "add", str(live), "--encrypt"])
    assert r.exit_code == 0, r.output
    c = cfg.load("enc")
    assert len(c.targets) == 1
    assert bool(getattr(c.targets[0], "encrypt", False)) is True
    store = cfg.store_dir(c)
    from versioneer.core import monitor as _mon

    rel = _mon.store_rel_for(c.targets[0].path,
                             _mon.live_abs_path(c.targets[0].path, c.targets[0].abs_path, ""),
                             c.targets[0].flex, "")
    stored = store / rel
    assert stored.exists()
    assert sec.is_encrypted_file(stored), f"store artifact should be encrypted:\n{r.output}"
    # drift live, then deploy must decrypt back to plaintext
    live.write_text("drifted\n")
    r = runner.invoke(cli, ["-C", "enc", "deploy", "--yes", str(live)])
    assert r.exit_code == 0, r.output
    assert live.read_bytes() == b"token=abc123\n", f"deploy must decrypt:\n{r.output}"


def test_doctor_secrets_flags_plaintext_and_toolchain(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    _make(runner, tmp_path, "audit")
    f = tmp_path / "tok2.conf"
    f.write_text('password = "supersecretvalue123"\n')
    r = runner.invoke(cli, ["-C", "audit", "target", "add", str(f)])
    assert r.exit_code == 0, r.output
    assert "secret scan" in r.output
    # secrets audit flags the plaintext secret
    r = runner.invoke(cli, ["-C", "audit", "doctor", "--secrets"])
    assert r.exit_code == 0, r.output
    assert "secret" in r.output.lower() or "warning" in r.output.lower()
    # encrypt=true without sops binary -> toolchain warning, never blocks
    _patch_sops_available(monkeypatch, False)
    c = cfg.load("audit")
    c.targets[0].encrypt = True
    cfg.save(c)
    r = runner.invoke(cli, ["-C", "audit", "doctor", "--secrets"])
    assert r.exit_code == 0, r.output
    assert "sops" in r.output.lower()
