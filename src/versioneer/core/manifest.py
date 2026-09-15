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
