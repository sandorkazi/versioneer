"""Secrets: secret-scan + sops/age per-file encryption (Phase 9, ITEM 2).

v1 contract (warn-only, never blocks):
- `encrypt=true` per-target (or per-config `meta.encrypt`) means the store
  artifact is sops-encrypted, decrypted on deploy.
- When `sops`/`age` binaries are absent, every encrypt/decrypt helper
  degrades to a warning string and leaves bytes untouched. Callers print
  the warning and continue; nothing raises for a missing toolchain.
- Detection is marker-based (`sops:`, `SOPS-ENC:`, `ENC[`); the fake-sops
  used in tests prepends `SOPS-ENC:` which matches this heuristic.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

MARKERS: tuple[bytes, ...] = (b"sops:", b"SOPS-ENC:", b"ENC[")

ENCRYPT_WARN_MISSING = (
    "sops not found (sudo pacman -S sops age) — "
    "storing plaintext for now (warn-only in v1, set encrypt=true data stays unencrypted)"
)


def sops_available() -> bool:
    return shutil.which("sops") is not None


def age_available() -> bool:
    return shutil.which("age") is not None


def toolchain_status() -> dict:
    return {"sops": sops_available(), "age": age_available()}


def is_encrypted_bytes(data: bytes, *, limit: int = 1_000_000) -> bool:
    sample = data[:limit]
    return any(m in sample for m in MARKERS)


def is_encrypted_file(path: Path) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
        if path.stat().st_size > 20_000_000:
            return False
        return is_encrypted_bytes(path.read_bytes())
    except OSError:
        return False


def _get(obj, key: str, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def should_encrypt(target=None, meta=None) -> bool:
    """True when per-target `encrypt` or (fallback) `meta.encrypt` is set."""
    if target is not None and bool(_get(target, "encrypt", False)):
        return True
    return meta is not None and bool(_get(meta, "encrypt", False))


def _age_recipient() -> str | None:
    for env in ("SOPS_AGE_RECIPIENT", "SOPS_AGE_RECIPIENTS", "AGE_RECIPIENT"):
        val = os.environ.get(env, "").strip()
        if val:
            # comma/space separated -> first recipient
            return val.replace(",", " ").split()[0]
    return None


def _encrypt_cmd(path: Path) -> list[str]:
    cmd = ["sops", "--encrypt", "--in-place"]
    recipient = _age_recipient()
    if recipient:
        cmd += ["--age", recipient]
    cmd.append(str(path))
    return cmd


def encrypt_file_in_place(path: Path) -> str | None:
    """Encrypt one store artifact in place. Returns None or warning (never raises).

    Already-encrypted files are a no-op (None). Missing sops -> warn-only.
    sops failures -> warn-only (plaintext kept).
    """
    try:
        if path.is_symlink() or not path.is_file():
            return None
    except OSError as e:
        return f"encrypt skipped for {path}: {e} (warn-only)"
    if is_encrypted_file(path):
        return None
    if not sops_available():
        return ENCRYPT_WARN_MISSING
    try:
        proc = subprocess.run(
            _encrypt_cmd(path), capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"sops encrypt failed for {path}: {e} (warn-only, plaintext kept)"
    if proc.returncode != 0:
        detail = ((proc.stderr or proc.stdout or "").strip() or "unknown error")[:500]
        return f"sops encrypt failed for {path}: {detail} (warn-only, plaintext kept)"
    return None


def encrypt_store_artifact(store_path: Path, kind: str = "text") -> list[str]:
    """Encrypt a staged store artifact (file or dir tree). Returns warnings."""
    warnings: list[str] = []
    try:
        if store_path.is_symlink():
            return warnings  # link targets are not encrypted in v1
        if kind == "dir" or (store_path.is_dir() and not store_path.is_symlink()):
            try:
                files = sorted(p for p in store_path.rglob("*") if p.is_file() and not p.is_symlink())
            except OSError as e:
                return [f"encrypt scan failed for {store_path}: {e} (warn-only)"]
            for f in files:
                w = encrypt_file_in_place(f)
                if w:
                    warnings.append(f"{f.name}: {w}")
            return warnings
        w = encrypt_file_in_place(store_path)
        if w:
            warnings.append(w)
        return warnings
    except OSError as e:
        return [f"encrypt failed for {store_path}: {e} (warn-only)"]


def decrypt_bytes(path: Path) -> tuple[bytes, str | None]:
    """Read a store artifact, decrypting when it looks sops-encrypted.

    Returns (bytes, warning|None). Never raises for missing toolchain:
    encrypted-but-no-sops yields (raw, warning). Unreadable files yield
    (b"", warning).
    """
    try:
        raw = path.read_bytes()
    except OSError as e:
        return b"", f"cannot read store artifact {path}: {e} (warn-only)"
    if not is_encrypted_bytes(raw):
        return raw, None
    if not sops_available():
        return raw, ENCRYPT_WARN_MISSING
    try:
        proc = subprocess.run(
            ["sops", "--decrypt", str(path)],
            capture_output=True, text=False, timeout=120, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return raw, f"sops decrypt failed for {path}: {e} (warn-only)"
    if proc.returncode != 0:
        detail = ""
        try:
            detail = ((proc.stderr or b"").decode(errors="replace").strip() or "unknown error")[:500]
        except (UnicodeError, ValueError):
            detail = "unknown error"
        return raw, f"sops decrypt failed for {path}: {detail} (warn-only)"
    out = proc.stdout if isinstance(proc.stdout, (bytes, bytearray)) else b""
    return bytes(out), None


def decrypt_file_in_place(path: Path) -> str | None:
    """Decrypt one file in place (used for dir-deploy destinations)."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
    except OSError as e:
        return f"decrypt skipped for {path}: {e} (warn-only)"
    if not is_encrypted_file(path):
        return None
    if not sops_available():
        return ENCRYPT_WARN_MISSING
    # Prefer stdout-mode decrypt (works with fake sops), write back ourselves.
    data, warn = decrypt_bytes(path)
    if warn is not None and data and is_encrypted_bytes(data):
        # real decrypt failed; keep ciphertext, surface warning
        return warn
    if warn is not None:
        return warn
    try:
        path.write_bytes(data)
    except OSError as e:
        return f"cannot write decrypted {path}: {e} (warn-only)"
    return None


def decrypt_tree_in_place(root: Path) -> list[str]:
    """Decrypt every sops-encrypted file under root (dir-deploy dest)."""
    warnings: list[str] = []
    try:
        if not root.is_dir() or root.is_symlink():
            return warnings
        files = sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())
    except OSError as e:
        return [f"decrypt scan failed for {root}: {e} (warn-only)"]
    for f in files:
        try:
            if not is_encrypted_file(f):
                continue
        except OSError:
            continue
        w = decrypt_file_in_place(f)
        if w:
            warnings.append(f"{f.name}: {w}")
    return warnings


def hash_store_artifact(src: Path, kind: str = "text", symlink: str = "preserve",
                        ignore: list[str] | None = None) -> str:
    """Hash a store artifact for deploy/plan comparison, decrypting first.

    Falls back to raw hash_target when plaintext or toolchain missing.
    """
    from versioneer.core import monitor as _mon

    ignore = ignore or []
    try:
        if src.is_symlink() and symlink == "preserve":
            return _mon.hash_target(src, kind, symlink, ignore)
        if kind == "dir":
            return _mon.hash_target(src, kind, symlink, ignore)
        if is_encrypted_file(src) and sops_available():
            data, warn = decrypt_bytes(src)
            if warn is None:
                import hashlib

                return f"sha256:{hashlib.sha256(data).hexdigest()}"
        return _mon.hash_target(src, kind, symlink, ignore)
    except OSError:
        return "read-error"
