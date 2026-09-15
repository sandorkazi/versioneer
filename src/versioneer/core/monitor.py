"""Path resolution, hashing, kind/flex detection, ignore matching (Phase 1)."""

from __future__ import annotations

import fnmatch
import glob as globmod
import hashlib
import os
import shutil
import tempfile
from pathlib import Path

WINE_DEFAULT_IGNORES = [
    "drive_c/users/*/Temp/**",
    "*.log",
    "*.tmp",
    "**/Cache/**",
    "dosdevices/**",
]


def expand_path(raw: str) -> Path:
    return Path(os.path.expandvars(raw)).expanduser()


def _abs_preserve(p: Path) -> Path:
    """Absolute path without dereferencing a trailing symlink."""
    if p.is_symlink():
        parent = p.parent.resolve(strict=False) if str(p.parent) else Path.cwd()
        return parent / p.name
    return p.resolve(strict=False)


def resolve_input(raw: str, root: str = "") -> tuple[str, Path]:
    """Return (stored_path, abs_path).

    - root set: stored_path is relative to root; input may be absolute (must be
      under root) or relative (interpreted under root).
    - no root: stored_path is the absolute string, abs_path resolved.
    Trailing symlinks are preserved (not dereferenced) so `symlink=preserve`
    records the link itself.
    """
    p = expand_path(raw)
    if root:
        r = expand_path(root)
        r_resolved = r.resolve(strict=False)
        if not p.is_absolute():
            abs_path = _abs_preserve(r_resolved / p)
            try:
                stored = str(abs_path.relative_to(r_resolved))
            except ValueError:
                abs_path = r_resolved / p
                stored = str(p)
            return stored, abs_path
        abs_path = _abs_preserve(p)
        try:
            abs_path.relative_to(r_resolved)
        except ValueError as e:
            raise ValueError(f"path {raw!r} is not under root {root!r}") from e
        stored = str(abs_path.relative_to(r_resolved))
        return stored, abs_path
    if not p.is_absolute():
        base = Path.cwd() / p
        abs_path = _abs_preserve(base)
    else:
        abs_path = _abs_preserve(p) if (p.exists() or p.is_symlink()) else p.absolute()
    return str(abs_path), abs_path


def detect_flex(abs_path: Path) -> str:
    home = Path.home()
    try:
        abs_path.relative_to(home)
        return "user"
    except ValueError:
        pass
    parts = abs_path.parts
    if len(parts) > 2 and parts[1] == "home":
        return "user"
    return "fixed"


def looks_binary_file(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            chunk = f.read(8192)
    except OSError:
        return False
    if b"\x00" in chunk:
        return True
    suffix = path.suffix.lower()
    return suffix in {".png", ".jpg", ".jpeg", ".gif", ".zip", ".gz", ".xz", ".bin",
                      ".dat", ".sav", ".sqlite", ".db", ".o", ".so", ".exe"}


def detect_kind(abs_path: Path, symlink_mode: str = "preserve") -> str:
    if abs_path.is_symlink() and symlink_mode == "preserve":
        # link itself is stored; treat as text artifact (link target string)
        return "text"
    if abs_path.is_dir() and not (abs_path.is_symlink() and symlink_mode == "follow"):
        return "dir"
    if abs_path.is_file() or abs_path.is_symlink():
        target = abs_path.resolve(strict=False) if symlink_mode == "follow" else abs_path
        if target.is_dir():
            return "dir"
        if target.is_file() and looks_binary_file(target):
            return "binary"
        return "text"
    # missing path: default text (add will record missing baseline as error)
    return "text"


def expand_glob(pattern: str) -> list[Path]:
    pat = str(expand_path(pattern))
    return sorted(Path(p) for p in globmod.glob(pat, recursive=True))


def matches_ignore(relpath: str, patterns: list[str]) -> bool:
    """Minimal gitignore-style match on posix relpath."""
    rel = relpath.replace(os.sep, "/")
    name = rel.split("/")[-1]
    for pat in patterns:
        p = pat.strip()
        if not p:
            continue
        if p.endswith("/"):
            if rel == p.rstrip("/") or rel.startswith(p):
                return True
            continue
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p):
            return True
        # support ** prefix/suffix loosely
        stripped = p.replace("**/", "").replace("**", "")
        if stripped and (fnmatch.fnmatch(rel, stripped) or fnmatch.fnmatch(name, stripped)):
            return True
    return False


def iter_dir_files(top: Path, ignore: list[str]) -> list[Path]:
    out: list[Path] = []
    if not top.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        base = Path(dirpath)
        try:
            rel_dir = base.relative_to(top).as_posix()
        except ValueError:
            continue
        # prune ignored dirs in-place
        kept: list[str] = []
        for d in sorted(dirnames):
            rel = f"{rel_dir}/{d}" if rel_dir != "." else d
            if matches_ignore(rel + "/", ignore) or matches_ignore(rel, ignore):
                continue
            kept.append(d)
        dirnames[:] = kept
        for fn in sorted(filenames):
            rel = f"{rel_dir}/{fn}" if rel_dir != "." else fn
            if matches_ignore(rel, ignore):
                continue
            out.append(base / fn)
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def safe_sha256_file(path: Path) -> tuple[str, bool]:
    """Copy-then-hash. Returns (hash, copied). Warn caller if copy fallback used."""
    try:
        return sha256_file(path), False
    except OSError:
        pass
    # locked-file fallback: copy then hash
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        shutil.copyfile(path, tmp_path)
        digest = sha256_file(tmp_path)
        return digest, True
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def hash_target(abs_path: Path, kind: str, symlink_mode: str,
                ignore: list[str] | None = None) -> str:
    ignore = ignore or []
    if abs_path.is_symlink() and symlink_mode == "preserve":
        try:
            target = os.readlink(abs_path)
        except OSError:
            target = ""
        digest = hashlib.sha256(target.encode("utf-8", errors="replace")).hexdigest()
        return f"symlink:{digest}"
    if kind == "dir":
        top = abs_path.resolve(strict=False) if symlink_mode == "follow" else abs_path
        files = iter_dir_files(top, ignore)
        h = hashlib.sha256()
        for f in files:
            try:
                rel = f.relative_to(top).as_posix()
            except ValueError:
                rel = f.name
            try:
                fh = sha256_file(f) if not f.is_symlink() else f"symlink:{os.readlink(f)}"
            except OSError:
                fh = "read-error"
            h.update(f"{rel}:{fh}\n".encode())
        return f"dir-sha256:{h.hexdigest()}"
    try:
        digest, _ = safe_sha256_file(abs_path)
        return digest
    except OSError:
        return "missing"


def artifact_rel(store_path: str, abs_path: Path, flex: str, root: str = "") -> Path:
    """Store-relative artifact path for a target."""
    if root:
        return Path(store_path)
    home = Path.home()
    if flex == "user":
        try:
            return abs_path.relative_to(home)
        except ValueError:
            pass
        parts = abs_path.parts
        if len(parts) > 3 and parts[1] == "home":
            return Path(*parts[3:])  # strip /home/<user>/
    s = str(abs_path).lstrip("/")
    return Path(s) if s else Path("_root")


def wine_preset_ignores(abs_path: Path, ignore: list[str]) -> list[str]:
    out = list(ignore)
    probe = abs_path if abs_path.is_dir() else abs_path.parent
    looks_wine = ("drive_c" in {p.name for p in probe.iterdir()} if probe.is_dir() else False) \
        or "wine" in abs_path.as_posix().lower() or "pfx" in abs_path.as_posix().lower()
    if looks_wine:
        for pat in WINE_DEFAULT_IGNORES:
            if pat not in out:
                out.append(pat)
    return out


# ---------- Phase 2: drift scan (backs `status`, daemon reuses) ----------

def live_abs_path(stored_path: str, abs_path_str: str, meta_root: str = "") -> Path:
    """Live filesystem path for a target (root-relative or absolute)."""
    if meta_root:
        return expand_path(meta_root).resolve(strict=False) / stored_path
    # abs_path cache may be stale across machines; prefer stored absolute string
    raw = abs_path_str or stored_path
    return expand_path(raw)


def store_rel_for(stored_path: str, abs_path: Path, flex: str, meta_root: str = "") -> Path:
    """Store-relative artifact path (single place for CLI + scan)."""
    if meta_root:
        return Path(stored_path)
    return artifact_rel("", abs_path, flex, "")


def scan_one(target, meta_root: str = "", store_dir: Path | None = None,
             host: str = "") -> dict:
    """Compare live filesystem vs TOML baseline. Read-only, offline-safe.

    Returns dict: state, current_hash, owner/group/mode, detail, untracked,
    host_skipped, abs_path, rel.
    States: clean|modified|perm-drift|missing|untracked|read-error.
    """
    import errno
    import socket

    from versioneer.core import permissions as _perm

    host = host or socket.gethostname()
    machines = list(getattr(target, "machines", []) or [])
    host_skipped = bool(machines) and host not in machines

    abs_path = live_abs_path(target.path, target.abs_path, meta_root)
    follow = (getattr(target, "symlink", "preserve") == "follow")
    kind = getattr(target, "kind", "text")
    ignore = list(getattr(target, "ignore", []) or [])
    rel = store_rel_for(target.path, abs_path, getattr(target, "flex", "fixed"), meta_root)

    # dangling link => missing (spec), regardless of preserve/follow
    try:
        is_link = abs_path.is_symlink()
    except OSError:
        is_link = False
    try:
        exists = abs_path.exists()
    except OSError:
        exists = False
    if is_link and not exists:
        return {"target": target, "state": "missing", "detail": "dangling symlink",
                "current_hash": "missing", "owner": "", "group": "", "mode": "",
                "untracked": [], "host_skipped": host_skipped,
                "abs_path": abs_path, "rel": rel}
    if not exists and not is_link:
        return {"target": target, "state": "missing", "detail": "path does not exist",
                "current_hash": "missing", "owner": "", "group": "", "mode": "",
                "untracked": [], "host_skipped": host_skipped,
                "abs_path": abs_path, "rel": rel}

    # stat baseline (perm part)
    try:
        owner, group, mode = _perm.capture(abs_path, follow=follow)
    except OSError as e:
        state = "read-error"
        detail = f"cannot stat: {e.strerror or e}"
        # ENOENT raced -> missing
        if e.errno == errno.ENOENT:
            state, detail = "missing", "path does not exist"
        return {"target": target, "state": state, "detail": detail,
                "current_hash": "read-error", "owner": "", "group": "", "mode": "",
                "untracked": [], "host_skipped": host_skipped,
                "abs_path": abs_path, "rel": rel}

    # quick readability gate for regular files (root-owned fixed targets)
    if kind in ("text", "binary") and not is_link:
        try:
            if not abs_path.is_dir() and not _perm.is_readable(abs_path, follow=follow):
                return {"target": target, "state": "read-error",
                        "detail": "cannot read — elevation required",
                        "current_hash": "read-error", "owner": owner, "group": group,
                        "mode": mode, "untracked": [], "host_skipped": host_skipped,
                        "abs_path": abs_path, "rel": rel}
        except OSError:
            pass

    current_hash = hash_target(abs_path, kind, getattr(target, "symlink", "preserve"), ignore)
    if current_hash in ("missing", "read-error"):
        state = "missing" if current_hash == "missing" else "read-error"
        return {"target": target, "state": state,
                "detail": "path does not exist" if state == "missing" else "cannot read file",
                "current_hash": current_hash, "owner": owner, "group": group, "mode": mode,
                "untracked": [], "host_skipped": host_skipped,
                "abs_path": abs_path, "rel": rel}

    # untracked extras inside dir targets (live set minus store snapshot set)
    untracked: list[str] = []
    if kind == "dir" and store_dir is not None:
        try:
            live_files = {p.relative_to(abs_path.resolve(strict=False) if follow else abs_path).as_posix()
                          if not p.is_symlink() else p.name
                          for p in iter_dir_files(
                              abs_path.resolve(strict=False) if follow else abs_path, ignore)}
        except OSError:
            live_files = set()
        stored_top = store_dir / rel
        try:
            stored_files = {p.relative_to(stored_top).as_posix()
                            for p in iter_dir_files(stored_top, [])} if stored_top.is_dir() else set()
        except OSError:
            stored_files = set()
        untracked = sorted(live_files - stored_files)

    baseline_hash = getattr(target, "hash", "")
    perm_same = (owner == getattr(target, "owner", "") and group == getattr(target, "group", "")
                 and mode == getattr(target, "mode", ""))
    if untracked:
        return {"target": target, "state": "untracked",
                "detail": f"{len(untracked)} new file(s): {', '.join(untracked[:5])}"
                          + ("…" if len(untracked) > 5 else ""),
                "current_hash": current_hash, "owner": owner, "group": group, "mode": mode,
                "untracked": untracked, "host_skipped": host_skipped,
                "abs_path": abs_path, "rel": rel}
    if current_hash != baseline_hash:
        return {"target": target, "state": "modified", "detail": "content differs from baseline",
                "current_hash": current_hash, "owner": owner, "group": group, "mode": mode,
                "untracked": [], "host_skipped": host_skipped,
                "abs_path": abs_path, "rel": rel}
    if not perm_same:
        return {"target": target, "state": "perm-drift",
                "detail": (f"perms {getattr(target, 'owner', '')}/{getattr(target, 'group', '')}/"
                           f"{getattr(target, 'mode', '')} → {owner}/{group}/{mode}"),
                "current_hash": current_hash, "owner": owner, "group": group, "mode": mode,
                "untracked": [], "host_skipped": host_skipped,
                "abs_path": abs_path, "rel": rel}
    return {"target": target, "state": "clean", "detail": "matches baseline",
            "current_hash": current_hash, "owner": owner, "group": group, "mode": mode,
            "untracked": [], "host_skipped": host_skipped,
            "abs_path": abs_path, "rel": rel}


def scan_all(config, store_dir: Path | None = None, host: str = "") -> list[dict]:
    """Scan every target in a config (read-only)."""
    return [scan_one(t, config.meta.root or "", store_dir, host) for t in config.targets]
