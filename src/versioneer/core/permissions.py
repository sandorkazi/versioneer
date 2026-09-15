"""Owner/group/mode capture (Phase 1)."""

from __future__ import annotations

import grp
import os
import pwd
import stat
from pathlib import Path


def capture(path: Path, follow: bool = True) -> tuple[str, str, str]:
    """Return (owner, group, mode) for path.

    follow=False uses lstat (for symlink preserve).
    mode is a 4-digit octal string like "0644".
    """
    st = path.stat() if follow else path.lstat()
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    mode = format(stat.S_IMODE(st.st_mode), "04o")
    return owner, group, mode


def is_readable(path: Path, follow: bool = True) -> bool:
    try:
        if path.is_symlink() and not follow:
            path.lstat()
            return True
        return os.access(path, os.R_OK)
    except OSError:
        return False


def apply(path: Path, owner: str = "", group: str = "", mode: str = "",
          follow: bool = True) -> list[str]:
    """Restore owner/group/mode after deploy. Returns list of error strings.

    Never raises for chown/chmod failures — callers record per-target error.
    Symlinks: when follow=False and path is a link, only the link itself is
    considered (lchmod is generally unavailable; mode restore is skipped).
    """
    errors: list[str] = []
    try:
        is_link = path.is_symlink()
    except OSError:
        is_link = False
    if is_link and not follow:
        return errors
    if owner or group:
        try:
            import shutil as _shutil

            _shutil.chown(str(path), user=owner or None, group=group or None)
        except (OSError, LookupError, ValueError) as e:
            errors.append(f"chown failed: {e}")
    if mode:
        try:
            # mode is a 4-digit octal string like "0644"
            path.chmod(int(mode, 8))
        except (OSError, ValueError) as e:
            errors.append(f"chmod failed: {e}")
    return errors
