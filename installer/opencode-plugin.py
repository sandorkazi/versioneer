#!/usr/bin/env python3
"""Register the versioneer OpenCode plugin in the global OpenCode config.

Usage: opencode-plugin.py CONFIG_DIR PLUGIN_DIR

Surgical text edit (never a JSON round-trip) so user comments and formatting
survive. A `<config>.bak` backup is written before any modification.

Exit codes: 0 = registered or already present, 2 = needs a manual edit
(reason printed), 1 = usage/IO error.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path


def pick_config(config_dir: Path) -> Path:
    """Prefer an existing opencode.jsonc, then opencode.json, else a new .jsonc."""
    for name in ("opencode.jsonc", "opencode.json"):
        if (config_dir / name).is_file():
            return config_dir / name
    return config_dir / "opencode.jsonc"


def strip_strings(line: str) -> str:
    """Remove double-quoted string contents (brackets inside strings don't count)."""
    out: list[str] = []
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c == '"':
            i += 1
            while i < n:
                if line[i] == "\\":
                    i += 2
                    continue
                if line[i] == '"':
                    i += 1
                    break
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def code_part(line: str) -> str:
    """Line with strings and // comments removed (for structural scanning)."""
    s = strip_strings(line)
    idx = s.find("//")
    return s[:idx] if idx != -1 else s


def ensure_trailing_comma(line: str) -> str:
    """Append a comma to the code part of a line, preserving // comments/newline."""
    nl = "\n" if line.endswith("\n") else ""
    body = line[: -len(nl)] if nl else line
    stripped = strip_strings(body)
    idx = stripped.find("//")
    if idx == -1:
        return body.rstrip() + "," + nl
    return body[:idx].rstrip() + "," + (" " + body[idx:] if body[idx:].strip() else "") + nl


def bracket_pos_outside_strings(line: str, char: str, last: bool = False) -> int:
    """Index of [ or ] outside strings, or -1. `last` picks the final occurrence."""
    in_str = False
    esc = False
    found = -1
    for i, c in enumerate(line):
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == char:
            if not last:
                return i
            found = i
    return found


def manual(target: Path, entry: str, reason: str) -> tuple[int, str]:
    return (
        2,
        f"MANUAL: could not safely edit {target} ({reason}).\n"
        f"Add this string to your \"plugins\" array:\n  \"{entry}\"\n"
        "See plugins/opencode-versioneer/README.md for details.",
    )


def register(config_dir: Path, plugin_dir: Path) -> tuple[int, str]:
    entry = f"file://{plugin_dir.resolve()}"
    marker = str(plugin_dir.resolve())
    target = pick_config(config_dir)

    if not target.is_file():
        config_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(
            '{\n  "$schema": "https://opencode.ai/config.json",\n'
            f'  "plugins": [\n    "{entry}"\n  ]\n}}\n',
            encoding="utf-8",
        )
        return 0, f"added: created {target} with plugins=[{entry}]"

    text = target.read_text(encoding="utf-8")
    if marker in text or entry in text:
        return 0, f"already present: {target} already references {entry}"
    if "/*" in text or "*/" in text:
        return manual(target, entry, "block comments present")

    lines = text.splitlines(keepends=True)
    key_idx = next(
        (i for i, ln in enumerate(lines) if re.search(r'"plugins"\s*:', code_part(ln))),
        None,
    )
    if key_idx is None:
        brace = next((i for i, ln in enumerate(lines) if "{" in code_part(ln)), None)
        if brace is None:
            return manual(target, entry, "no JSON object found")
        lines.insert(brace + 1, f'  "plugins": [\n    "{entry}",\n  ],\n')
        shutil.copy2(target, str(target) + ".bak")
        target.write_text("".join(lines), encoding="utf-8")
        return 0, f"added: plugins entry in {target} (backup: {target}.bak)"

    # Locate the [...] span starting at the key line (depth counting, strings ignored).
    depth = 0
    start = end = None
    for i in range(key_idx, len(lines)):
        for ch in code_part(lines[i]):
            if ch == "[":
                depth += 1
                if start is None:
                    start = i
            elif ch == "]":
                depth -= 1
                if start is not None and depth == 0:
                    end = i
                    break
        if end is not None:
            break
    if start is None or end is None:
        return manual(target, entry, "could not locate plugins array")

    inner = "".join(code_part(lines[start : end + 1]))
    inner_text = inner[inner.find("[") + 1 : inner.rfind("]")].strip().strip(",").strip()
    close_indent = lines[end][: len(lines[end]) - len(lines[end].lstrip())]
    item_indent = close_indent + "  "
    item_line = f'{item_indent}"{entry}",\n'

    if not inner_text:
        if start == end:
            # Single line, e.g. "plugins": [] — rebuild the line around the brackets.
            raw = lines[start]
            open_i = bracket_pos_outside_strings(raw, "[")
            close_i = bracket_pos_outside_strings(raw, "]", last=True)
            if open_i == -1 or close_i == -1 or close_i < open_i:
                return manual(target, entry, "could not parse plugins array")
            rest = raw[close_i + 1 :]
            nl = "\n" if rest.endswith("\n") or raw.endswith("\n") else ""
            rest = rest[: -len(nl)] if nl and rest.endswith("\n") else rest
            lines[start] = (
                raw[: open_i + 1] + "\n" + item_line + close_indent + "]" + rest + nl
            )
        else:
            lines.insert(end, item_line)
        shutil.copy2(target, str(target) + ".bak")
        target.write_text("".join(lines), encoding="utf-8")
        return 0, f"added: plugins entry in {target} (backup: {target}.bak)"

    # Non-empty array: closing line must hold only the bracket (plus comma).
    if code_part(lines[end]).strip() not in ("]", "],"):
        return manual(target, entry, "plugins array has an unusual layout")
    prev = end - 1
    while prev > start and not code_part(lines[prev]).strip():
        prev -= 1
    if not code_part(lines[prev]).rstrip().endswith(","):
        lines[prev] = ensure_trailing_comma(lines[prev])
    lines.insert(end, item_line)
    shutil.copy2(target, str(target) + ".bak")
    target.write_text("".join(lines), encoding="utf-8")
    return 0, f"added: plugins entry in {target} (backup: {target}.bak)"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: opencode-plugin.py CONFIG_DIR PLUGIN_DIR", file=sys.stderr)
        return 1
    try:
        code, message = register(Path(argv[1]).expanduser(), Path(argv[2]))
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(message)
    if code == 2:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
