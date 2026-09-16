"""Manifest generators: packages + wine + systemd + env (Phase 8).

Each generator returns (artifact_filename, content_text, replay_hint).
Artifacts behave like text targets: regenerated on commit, replayed on deploy.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
from pathlib import Path


def _run(cmd: list[str], timeout: int = 20) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return (proc.stdout or "").strip()
    return (proc.stdout or "").strip()


def gen_packages() -> tuple[str, str, str]:
    lines: list[str] = []
    pacman = _run(["pacman", "-Qqe"]) if shutil.which("pacman") else ""
    aur = ""
    for helper in ("yay", "paru"):
        if shutil.which(helper):
            aur = _run([helper, "-Qqm"])
            if aur:
                break
    flatpak = _run(["flatpak", "list", "--app", "--columns=application"]) \
        if shutil.which("flatpak") else ""
    content = "# versioneer packages manifest\n"
    if pacman:
        content += "\n[pacman -Qqe]\n" + pacman + "\n"
        lines += [p for p in pacman.splitlines() if p.strip()]
    if aur:
        content += "\n[AUR]\n" + aur + "\n"
    if flatpak:
        content += "\n[flatpak]\n" + flatpak + "\n"
    if not (pacman or aur or flatpak):
        content += "(no supported package manager found on this machine)\n"
    replay = ("setup-packages.sh: "
              + (f"sudo pacman -S --needed {len(lines)} pkgs" if lines
                 else "no pacman data"))
    setup = "#!/usr/bin/env bash\n# replay: sudo pacman -S --needed $(cat packages.list)\n"
    full = content + "\n---\n" + setup
    return "packages.list", full, replay


def gen_wine() -> tuple[str, str, str]:
    wine_version = _run(["wine", "--version"]) if shutil.which("wine") else "(wine not found)"
    prefix = os.environ.get("WINEPREFIX", str(Path.home() / ".wine"))
    arch = os.environ.get("WINEARCH", "")
    tricks = _run(["winetricks", "list-installed"]) if shutil.which("winetricks") else ""
    exes: list[str] = []
    try:
        p = Path(prefix)
        if p.is_dir():
            for f in p.rglob("*.exe"):
                try:
                    exes.append(str(f.relative_to(p)))
                except ValueError:
                    exes.append(f.name)
                if len(exes) >= 200:
                    break
    except OSError:
        pass
    manifest = {
        "wine_version": wine_version,
        "WINEARCH": arch,
        "WINEPREFIX": prefix,
        "winetricks_installed": tricks.splitlines() if tricks else [],
        "exe_inventory": sorted(exes)[:200],
        "host": socket.gethostname(),
    }
    content = json.dumps(manifest, indent=2)
    replay = (f"setup-wine.sh: WINEPREFIX={prefix} "
              f"wine {wine_version}; winetricks verbs: {len(manifest['winetricks_installed'])}")
    return "wine-manifest.json", content, replay


def gen_systemd() -> tuple[str, str, str]:
    user_units = _run(["systemctl", "--user", "list-unit-files",
                       "--state=enabled", "--no-legend"])
    sys_units = _run(["systemctl", "list-unit-files",
                      "--state=enabled", "--no-legend"])
    content = ("# enabled user units\n" + (user_units or "(none)") +
               "\n\n# enabled system units\n" + (sys_units or "(none)") + "\n")
    return "units.list", content, "replay: systemctl enable <units from units.list>"


def gen_env() -> tuple[str, str, str]:
    data = {
        "PATH": os.environ.get("PATH", ""),
        "SHELL": os.environ.get("SHELL", ""),
        "HOST": socket.gethostname(),
        "ARCH": platform.machine(),
        "HOTKEY_ENV": {k: os.environ.get(k, "") for k in
                       ("XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP",
                        "WAYLAND_DISPLAY", "DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE")
                       if k in os.environ},
    }
    return "env.json", json.dumps(data, indent=2), "replay: compare env.json, export as needed"


GENERATORS = {
    "packages": gen_packages,
    "wine": gen_wine,
    "systemd": gen_systemd,
    "env": gen_env,
}


# ---------- ITEM 6: safe --apply helpers ----------
# Deploy stays print-only by default; --apply opts into the safe subset:
# packages -> `sudo pacman -S --needed ...`, systemd -> `systemctl enable`,
# env -> print-only always, wine -> generate setup-wine.sh, never auto-run
# winetricks. All helpers are pure (no subprocess) so deploy.py owns the
# only side effects and dry-run can guard them in one place.

def manifest_kind_for_target(target) -> str:
    """Infer manifest subtype (packages|wine|systemd|env) from a target.

    Manifest targets are stored with path == artifact filename
    (packages.list, wine-manifest.json, units.list, env.json). An explicit
    `manifest_type`/`manifest` attribute wins when present; otherwise fall
    back to filename heuristics so older TOMLs keep working.
    """
    for attr in ("manifest_type", "manifest", "manifest_kind"):
        val = getattr(target, attr, "")
        if isinstance(val, dict):
            val = val.get("type", "")
        if isinstance(val, str) and val.strip() in (
            "packages", "wine", "systemd", "env",
        ):
            return val.strip()
    path = str(getattr(target, "path", "") or "")
    base = path.rsplit("/", 1)[-1].lower()
    if base in ("packages.list", "setup-packages.sh"):
        return "packages"
    if base in ("wine-manifest.json", "setup-wine.sh"):
        return "wine"
    if base in ("units.list",):
        return "systemd"
    if base in ("env.json",):
        return "env"
    # loose fallback for renamed artifacts
    if "package" in base:
        return "packages"
    if "wine" in base:
        return "wine"
    if "unit" in base or "systemd" in base:
        return "systemd"
    if base == "env" or "env." in base:
        return "env"
    return "unknown"


def parse_packages_manifest(text: str) -> dict[str, list[str]]:
    """Parse a packages.list artifact into {pacman, aur, flatpak} name lists."""
    out: dict[str, list[str]] = {"pacman": [], "aur": [], "flatpak": []}
    section: str | None = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line == "---":
            break  # replay setup snippet appended after --- is not data
        if line.startswith("[") and line.endswith("]"):
            head = line[1:-1].strip().lower()
            if "pacman" in head:
                section = "pacman"
            elif "aur" in head:
                section = "aur"
            elif "flatpak" in head:
                section = "flatpak"
            else:
                section = None
            continue
        if line.startswith(("#", "//")):
            continue
        if line.startswith(("setup-packages", "sudo pacman")):
            continue
        if line.startswith(("#!", "replay:")):
            continue
        if section in out:
            # package names are single tokens; strip inline comments/version pins
            token = line.split("#", 1)[0].strip().split()
            if token:
                out[section].append(token[0])
        elif section is None:
            # header-less legacy artifact: treat bare tokens as pacman names
            if line and not line.startswith("("):
                token = line.split()[0]
                if token and "/" not in token and "=" not in token:
                    pass  # keep strict: unknown section lines are ignored
    # de-dup, preserve order
    for key, values in out.items():
        seen: set[str] = set()
        uniq: list[str] = []
        for pkg in values:
            if pkg not in seen:
                seen.add(pkg)
                uniq.append(pkg)
        out[key] = uniq
    return out


def packages_apply_command(pacman_pkgs: list[str]) -> list[str]:
    """Build the safe pacman replay/apply command (no --noconfirm)."""
    return ["sudo", "pacman", "-S", "--needed", *pacman_pkgs]


def replay_packages_text(parsed: dict[str, list[str]], limit: int = 20) -> str:
    pkgs = list(parsed.get("pacman", []))
    if not pkgs:
        return "(no pacman packages recorded)"
    shown = " ".join(pkgs[:limit])
    extra = f" ... (+{len(pkgs) - limit} more)" if len(pkgs) > limit else ""
    return f"sudo pacman -S --needed {shown}{extra}"


def parse_systemd_manifest(text: str) -> dict[str, list[str]]:
    """Parse a units.list artifact into {user, system} unit-name lists."""
    user: list[str] = []
    system: list[str] = []
    current = "user"
    for raw in (text or "").splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        low = stripped.lower()
        if "system units" in low and stripped.startswith("#"):
            current = "system"
            continue
        if "user units" in low and stripped.startswith("#"):
            current = "user"
            continue
        if stripped.startswith(("#", "(")):
            continue
        token = stripped.split()[0]
        # keep plausible unit names only (foo.service, foo.timer, ...)
        if "." in token and token[0].isalnum():
            (user if current == "user" else system).append(token)
    # de-dup, preserve order
    for lst in (user, system):
        seen: set[str] = set()
        uniq = [u for u in lst if not (u in seen or seen.add(u))]  # type: ignore[func-returns-value]
        lst[:] = uniq
    return {"user": user, "system": system}


def wine_setup_script_text(manifest_text: str) -> str:
    """Render a setup-wine.sh recipe skeleton from wine-manifest.json text.

    Never executed by versioneer itself (deploy --apply only writes the
    file); winetricks verbs are listed for the user to run manually.
    """
    try:
        data = json.loads(manifest_text or "{}")
    except (ValueError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    prefix = str(data.get("WINEPREFIX") or "~/.wine")
    arch = str(data.get("WINEARCH") or "")
    version = str(data.get("wine_version") or "")
    verbs = data.get("winetricks_installed") or []
    if not isinstance(verbs, list):
        verbs = []
    verbs = [str(v).strip() for v in verbs if str(v).strip()]
    exes = data.get("exe_inventory") or []
    if not isinstance(exes, list):
        exes = []
    lines = [
        "#!/usr/bin/env bash",
        "# versioneer wine recipe — REVIEW before running.",
        "# Generated by `versioneer deploy --apply` from wine-manifest.json.",
        "# versioneer never auto-runs winetricks; run the commands below manually.",
        "set -u",
        "",
    ]
    if version and version != "(wine not found)":
        lines.append(f"# recorded wine version: {version}")
    lines.append(f'export WINEPREFIX="{prefix}"')
    if arch:
        lines.append(f'export WINEARCH="{arch}"')
    lines += [
        "",
        "# 1. create the prefix (manual):",
        "#   winecfg",
        "",
        "# 2. install winetricks verbs recorded in the manifest (manual, one by one):",
    ]
    if verbs:
        for v in verbs:
            lines.append(f"#   winetricks {v}")
    else:
        lines.append("#   (no winetricks verbs recorded)")
    lines += [
        "",
        "# 3. reinstall your programs into $WINEPREFIX (manual):",
    ]
    if exes:
        for e in list(exes)[:20]:
            lines.append(f"#   - {e}")
        if len(exes) > 20:
            lines.append(f"#   ... (+{len(exes) - 20} more, see wine-manifest.json)")
    else:
        lines.append("#   (no exe inventory recorded)")
    lines.append("")
    return "\n".join(lines) + "\n"


def replay_wine_text(manifest_text: str) -> str:
    try:
        data = json.loads(manifest_text or "{}")
    except (ValueError, TypeError):
        data = {}
    prefix = data.get("WINEPREFIX", "~/.wine") if isinstance(data, dict) else "~/.wine"
    verbs = data.get("winetricks_installed", []) if isinstance(data, dict) else []
    n = len(verbs) if isinstance(verbs, list) else 0
    version = data.get("wine_version", "") if isinstance(data, dict) else ""
    return (f"setup-wine.sh: WINEPREFIX={prefix} wine {version}; "
            f"winetricks verbs: {n} (manual, never auto-run)")
