"""Warn-only lints for target add (Phase 1): secrets + hardcoded paths."""

from __future__ import annotations

import re
from pathlib import Path

SECRET_PATTERNS: list[tuple[str, str]] = [
    (r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----", "private key header"),
    (r"AKIA[0-9A-Z]{16}", "possible AWS access key"),
    (r"ghp_[A-Za-z0-9]{20,}", "possible GitHub token"),
    (r"gho_[A-Za-z0-9]{20,}", "possible GitHub OAuth token"),
    (r"xox[bap]-", "possible Slack token"),
    (r"(?i)\b(api[_-]?key|secret|password)\b\s*[:=]\s*['\"]?[\w\-./+]{12,}", "possible secret assignment"),
]

_HARDCODED_HOME = re.compile(r"/home/([^/\s:'\"]+)")


def scan_bytes(data: bytes, *, max_bytes: int = 1_000_000) -> list[str]:
    """Scan up to max_bytes of content, return warning strings (never blocks)."""
    try:
        text = data[:max_bytes].decode("utf-8", errors="replace")
    except (UnicodeError, ValueError):
        return []
    warnings: list[str] = []
    for pattern, label in SECRET_PATTERNS:
        if re.search(pattern, text):
            warnings.append(
                f"secret scan: {label} — consider encrypt=true or ignore (warn-only in v1)"
            )
            break  # one warning is enough; avoid spam
    return warnings


def scan_file(path: Path) -> list[str]:
    try:
        if path.is_symlink() or not path.is_file():
            return []
        if path.stat().st_size > 5_000_000:
            return []
        return scan_bytes(path.read_bytes())
    except OSError:
        return []


def hardcoded_path_warnings(path: Path) -> list[str]:
    """Warn if a text file contains /home/<other>/ different from current $HOME."""
    try:
        if not path.is_file() or path.is_symlink():
            return []
        if path.stat().st_size > 1_000_000:
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return []
    homes = set(_HARDCODED_HOME.findall(text))
    if not homes:
        return []
    current = Path.home().name
    others = sorted(h for h in homes if h != current)
    if not others:
        return []
    return [
        (f"hardcoded path: /home/{others[0]}/... found — consider --template "
         "(warn-only in v1)")
    ]
