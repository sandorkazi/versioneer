"""Privilege-elevation helpers for root-owned targets (Phase 1 fix).

Problem: `vers` lives in `~/.local/bin` (venv install), which is NOT in
sudo's `secure_path`, so `sudo vers ...` fails with "command not found".
Running the whole CLI as root is also wrong: it would read /root/.config
and write /root/versioneer-store instead of the user's config/store.

Solution: never require `sudo vers` for tracking. The CLI runs as the
user and elevates only the *read* of an unreadable file via `sudo cat` /
`sudo stat`, keeping TOML + store owned by the user. Full-root runs
(`sudo <full-path> vers ...`, system unit) remain supported via a
/usr/local/bin shim (installed by `installer/install.sh` by default,
automatic when the installer itself runs under sudo). Root-via-sudo runs
are sudo-aware: config/store/state/venv/user-flex paths follow the
invoking user (SUDO_USER), not /root (see effective_home/expand_user),
writes chown back to the invoking user (see fix_store_after_write), and
the CLI warns to prefer a plain user run.
"""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
import sys
from pathlib import Path


def sudo_cmd() -> str | None:
    """Absolute path to sudo, or None when unavailable."""
    return shutil.which("sudo")


def invoking_user() -> str | None:
    """Original user when the process runs as root via sudo, else None.

    Returns SUDO_USER when euid==0 and SUDO_USER is set to a non-root,
    non-empty name. Otherwise None (normal user run, direct root login,
    or `sudo -u` to non-root).
    """
    try:
        if os.geteuid() != 0:
            return None
    except AttributeError:
        return None  # non-POSIX (tests/Windows): no sudo semantics
    sudo_user = os.environ.get("SUDO_USER", "").strip()
    if not sudo_user or sudo_user == "root":
        return None
    return sudo_user


def invoking_home() -> Path | None:
    """Home dir of the sudo-invoking user, or None when unavailable."""
    user = invoking_user()
    if not user:
        return None
    try:
        import pwd

        try:
            return Path(pwd.getpwnam(user).pw_dir)
        except KeyError:
            pass
    except ImportError:
        pass
    # Fallback: /home/<user> when NSS lookup fails (minimal containers).
    fallback = Path(f"/home/{user}")
    try:
        if fallback.is_dir():
            return fallback
    except OSError:
        pass
    return fallback


def effective_home() -> Path:
    """Home dir to use for user-level paths (sudo-aware).

    Normal runs: Path.home(). Root-via-sudo runs (`sudo vers ...`,
    `sudo ./installer/install.sh`): the invoking user's home, so config
    (~/.config/versioneer), stores (~/versioneer-store), state
    (~/.local/state), venv (~/.local/share/versioneer/venv) and
    user-flex target resolution all follow the user, not /root.
    Explicit env overrides (VERSIONEER_CONFIG_DIR, XDG_*, explicit
    --venv DIR) always win — callers must check those first.
    """
    inv = invoking_home()
    if inv is not None:
        return inv
    return Path.home()


def expand_user(raw: str | Path) -> Path:
    """Expand $VARS + leading ~ against effective_home() (sudo-aware).

    Mirrors Path(os.path.expandvars(raw)).expanduser() for normal runs,
    but under `sudo` (euid 0 + SUDO_USER) a bare "~" or "~/..." resolves
    to the invoking user's home instead of /root. "~other/..." keeps
    standard semantics.
    """
    s = os.path.expandvars(str(raw))
    if s == "~":
        return effective_home()
    if s.startswith("~/"):
        return effective_home() / s[2:]
    return Path(s).expanduser()


def invoking_uid_gid() -> tuple[int, int] | None:
    """(uid, gid) of the sudo-invoking user, or None when not root-via-sudo."""
    user = invoking_user()
    if not user:
        return None
    try:
        import pwd

        pw = pwd.getpwnam(user)
        return pw.pw_uid, pw.pw_gid
    except (ImportError, KeyError):
        return None


def fix_ownership(path: str | Path, recursive: bool = False) -> bool:
    """Chown path back to the sudo-invoking user (best-effort, never raises).

    No-op when not running as root via sudo. Never follows symlinks, so
    preserved-link artifacts (store symlinks pointing outside the store)
    only rechown the link itself, never the target.
    Returns True when a chown was attempted.
    """
    ids = invoking_uid_gid()
    if ids is None:
        return False
    uid, gid = ids
    try:
        p = Path(path)
    except (OSError, ValueError):
        return False
    try:
        if not p.exists() and not p.is_symlink():
            return False
    except OSError:
        return False

    def _chown_one(target: Path) -> None:
        try:
            os.chown(target, uid, gid, follow_symlinks=False)
        except OSError:
            pass

    _chown_one(p)
    if recursive:
        try:
            if p.is_symlink() or not p.is_dir():
                return True
        except OSError:
            return True
        # Manual walk: never descend into symlinked dirs.
        stack = [p]
        while stack:
            top = stack.pop()
            try:
                with os.scandir(top) as it:
                    entries = list(it)
            except OSError:
                continue
            for entry in entries:
                try:
                    ep = Path(entry.path)
                except (OSError, ValueError):
                    continue
                _chown_one(ep)
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(ep)
                except OSError:
                    continue
    return True


def fix_store_after_write(store: str | Path, rel_paths: list[str] | tuple[str, ...] = ()) -> None:
    """Restore user ownership on store files touched by a root-via-sudo run.

    Covers: store dir itself (mkdir-as-root case), .git metadata
    (index/objects/refs written by git), staged artifacts, snapshot and
    .gitattributes. Best-effort, never raises. No-op without SUDO_USER.
    """
    if invoking_user() is None:
        return
    base = Path(store)
    fix_ownership(base, recursive=False)
    git_dir = base / ".git"
    try:
        if git_dir.is_dir() and not git_dir.is_symlink():
            fix_ownership(git_dir, recursive=True)
    except OSError:
        pass
    for rel in rel_paths or []:
        try:
            cand = base / str(rel)
        except (OSError, ValueError):
            continue
        fix_ownership(cand, recursive=True)
        # Parent dirs (e.g. store/.config for .config/dolphinrc) may have
        # been mkdir'd as root — chown the chain up to (not incl.) base.
        try:
            parent = cand.parent
            while parent != base and base in parent.parents:
                fix_ownership(parent, recursive=False)
                parent = parent.parent
        except (OSError, ValueError):
            continue
    for extra in (".versioneer.toml", ".gitattributes"):
        try:
            cand = base / extra
            if cand.exists() or cand.is_symlink():
                fix_ownership(cand, recursive=False)
        except OSError:
            continue


def sudo_transparency_warning() -> str | None:
    """Warn-once message for root-via-sudo runs, or None for normal runs."""
    user = invoking_user()
    if user is None:
        return None
    return (
        f"running via sudo as root — prefer plain `vers ...` as {user} "
        "(sudo read is automatic, password prompted once); "
        f"store/config ownership will be fixed to '{user}'"
    )


def vers_executable() -> Path:
    """Resolved path of the running vers/versioneer entry point."""
    argv0 = sys.argv[0]
    if argv0 and ("/" in argv0 or "\\" in argv0):
        return Path(argv0)
    found = shutil.which("versioneer") or shutil.which("vers")
    if found:
        return Path(found)
    return Path(argv0 or "vers")


def sudo_vers_hint(extra_args: str = "") -> str:
    """Hint string for running the same CLI as root when truly needed.

    Uses the absolute executable path because ~/.local/bin is not in
    sudo's secure_path. The CLI is sudo-aware (reuses the invoking user's
    config/store via SUDO_USER), so plain `sudo <exe>` works; preserving
    HOME is an extra belt-and-braces for tools that read $HOME directly.
    """
    exe = vers_executable()
    tail = f" {extra_args}".rstrip()
    return (
        f"sudo --preserve-env=HOME,XDG_CONFIG_HOME,XDG_STATE_HOME "
        f"{exe}{tail}  # ~/.local/bin is not in sudo secure_path, use full path"
    )


def system_shim_missing() -> list[str]:
    """Shims under /usr/local/bin expected by the system unit / sudo runs."""
    missing: list[str] = []
    for name in ("versioneer", "vers"):
        try:
            if not Path(f"/usr/local/bin/{name}").is_file():
                missing.append(f"/usr/local/bin/{name}")
        except OSError:
            missing.append(f"/usr/local/bin/{name}")
    return missing


def classify_path(path: Path) -> str:
    """Return 'exists' | 'missing' | 'denied'.

    Uses os.lstat so EACCES/EPERM on the path (or an unsearchable parent)
    is reported as 'denied' instead of being swallowed as missing
    (Path.exists() returns False for both).
    """
    try:
        os.lstat(path)
        return "exists"
    except FileNotFoundError:
        return "missing"
    except NotADirectoryError:
        return "missing"
    except OSError as e:
        if e.errno in (errno.EACCES, errno.EPERM):
            return "denied"
        # e.g. ELOOP, ENAMETOOLONG: treat as denied-ish only for perms,
        # otherwise surface as denied so callers suggest elevation, not
        # a misleading "does not exist".
        if e.errno in (errno.ENOENT,):
            return "missing"
        return "denied"


def suggest_similar(path: Path, limit: int = 3) -> list[str]:
    """Return close filenames in the parent dir (typo aid, best-effort)."""
    try:
        parent = path.parent
        if not parent.is_dir():
            return []
        names = [p.name for p in parent.iterdir()]
    except OSError:
        return []
    import difflib

    return difflib.get_close_matches(path.name, names, n=limit, cutoff=0.6)


def stat_via_sudo(path: Path, follow: bool = True) -> tuple[str, str, str]:
    """Return (owner, group, mode) using sudo stat. Raises OSError on failure."""
    sudo = sudo_cmd()
    if not sudo:
        raise OSError("sudo not found")
    # %U %G %a: owner, group, octal mode without leading zero.
    # follow=False (symlink preserve) would ideally lstat the link; GNU
    # stat follows by default for -c, but link ownership rarely matters
    # for the baseline (deploy skips chown on preserved links).
    cmd = [sudo, "stat", "-c", "%U %G %a", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise OSError(f"sudo stat failed: {e}") from e
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise OSError(f"sudo stat failed: {err or 'unknown error'}")
    parts = (proc.stdout or "").strip().split()
    if len(parts) != 3:
        raise OSError(f"sudo stat: unexpected output: {proc.stdout.strip()!r}")
    owner, group, raw_mode = parts
    mode = raw_mode.zfill(4)[-4:]
    return owner, group, mode


def read_bytes_via_sudo(path: Path) -> bytes:
    """Read a file via `sudo cat`. Raises OSError on failure."""
    sudo = sudo_cmd()
    if not sudo:
        raise OSError("sudo not found")
    try:
        proc = subprocess.run(
            [sudo, "cat", "--", str(path)],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise OSError(f"sudo cat failed: {e}") from e
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip() if proc.stderr else ""
        raise OSError(f"sudo cat failed: {err or 'unknown error'}")
    return proc.stdout


def hash_bytes(data: bytes) -> str:
    """sha256: hex digest for in-memory bytes (mirrors monitor.sha256_file)."""
    import hashlib

    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def hash_file_elevated(path: Path, kind: str = "text") -> str:
    """Hash a file, falling back to sudo cat when direct read fails.

    Only for regular files (text/binary). Dirs must be readable to walk;
    callers should surface dir EACCES as an error suggesting elevation of
    the whole operation instead. Raises OSError when both fail.
    """
    from versioneer.core import monitor as _mon

    if kind == "dir":
        raise OSError("dir targets need direct read access (cannot sudo-walk)")
    try:
        return _mon.sha256_file(path)
    except OSError:
        pass
    # locked-file copy fallback first (no password prompt), then sudo.
    try:
        digest, _copied = _mon.safe_sha256_file(path)
        # safe_sha256_file returns "missing"-style only via exception;
        # if it returned, the direct read actually worked on retry.
        return digest
    except OSError:
        pass
    data = read_bytes_via_sudo(path)
    return hash_bytes(data)
