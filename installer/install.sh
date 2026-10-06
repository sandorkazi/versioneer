#!/usr/bin/env bash
# Versioneer installer — venv-only, never touches system python.
# Usage:
#   ./installer/install.sh [--dev] [--venv DIR] [--no-completions]
#     [--no-system-shim] [--opencode-plugin] [--no-opencode-plugin]
#   VERSIONEER_VENV_DIR=/custom/path ./installer/install.sh
set -euo pipefail

DEV=0
INSTALL_COMPLETIONS=1
SYSTEM_SHIM=1
OPENCODE_PLUGIN="auto" # auto | yes (--opencode-plugin) | no (--no-opencode-plugin)
VENV_DIR_ARG=""
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev) DEV=1; shift ;;
    --system-shim) SYSTEM_SHIM=1; shift ;;
    --no-system-shim) SYSTEM_SHIM=0; shift ;;
    --venv=*) VENV_DIR_ARG="${1#--venv=}"; shift ;;
    --venv) VENV_DIR_ARG="${2:?--venv needs a DIR}"; shift 2 ;;
    --no-completions) INSTALL_COMPLETIONS=0; shift ;;
    --opencode-plugin) OPENCODE_PLUGIN="yes"; shift ;;
    --no-opencode-plugin) OPENCODE_PLUGIN="no"; shift ;;
    -h|--help)
      cat <<'EOF'
Usage: install.sh [--dev] [--venv DIR] [--no-completions] [--no-system-shim]
                  [--opencode-plugin] [--no-opencode-plugin]

Venv-only installer: creates/uses a venv (default
~/.local/share/versioneer/venv, override with $VERSIONEER_VENV_DIR
or --venv DIR) and installs versioneer into it (--dev = editable
install). Never touches system python (no sudo pip, no
--break-system-packages). Preflight requires python3 >= 3.12 and
git. Installs bash/fish/zsh
completions unless --no-completions. Always installs
/usr/local/bin/versioneer + /usr/local/bin/vers shims (via sudo,
warn-only if sudo fails — pass --no-system-shim to skip)
so `sudo vers` works (sudo secure_path excludes ~/.local/bin) and
the system unit ExecStart=/usr/local/bin/versioneer resolves.
(--system-shim is accepted for backwards compatibility and means
the same as the default.)
When run with sudo (EUID 0 + $SUDO_USER), the venv, completions and
~/.local/bin symlinks target the invoking user (not /root) and the
system shims are installed automatically. The CLI itself is also
sudo-aware: `sudo vers ...` reuses the invoking user's configs and
stores instead of /root's. Do NOT run the whole installer with sudo
in the normal case — run it as your user; it only uses sudo for the
two /usr/local/bin shims.
Upstreams are set later via
`versioneer config create --upstream <url>`; systemd units via
`versioneer service install`.
OpenCode plugin (plugins/opencode-versioneer): with neither
--opencode-plugin nor --no-opencode-plugin, the installer checks for
the `opencode` binary — absent means a normal install with no prompt;
present means it asks (default N) whether to register the plugin in
the global ~/.config/opencode config. --opencode-plugin registers
without asking; --no-opencode-plugin skips silently.
EOF
      exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# 0. Sudo-aware target user: `sudo ./install.sh` must install for the
# invoking user, not for /root. Resolve TARGET_USER/TARGET_HOME first,
# then derive VENV_DIR (explicit --venv / $VERSIONEER_VENV_DIR wins).
EUID_NOW="${EUID:-$(id -u)}"
TARGET_USER=""
TARGET_HOME=""
IS_ROOT_SUDO=0
if [[ "$EUID_NOW" -eq 0 && -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
  TARGET_USER="$SUDO_USER"
  TARGET_HOME="$(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6 || true)"
  if [[ -z "$TARGET_HOME" || ! -d "$TARGET_HOME" ]]; then
    TARGET_HOME="/home/$SUDO_USER"
  fi
  if [[ ! -d "$TARGET_HOME" ]]; then
    EVAL_HOME="$(eval echo "~$SUDO_USER" 2>/dev/null || true)"
    [[ -n "$EVAL_HOME" && -d "$EVAL_HOME" ]] && TARGET_HOME="$EVAL_HOME"
  fi
  IS_ROOT_SUDO=1
  # Running as root via sudo: shims are on by default anyway; nothing
  # extra to force here (kept for clarity).
  SYSTEM_SHIM=1
else
  TARGET_USER="$(id -un 2>/dev/null || echo "${SUDO_USER:-root}")"
  TARGET_HOME="${HOME:-/root}"
  # Direct root login (`su -`, root shell): shims are on by default so
  # the system unit + `sudo vers` resolve.
fi
TARGET_GROUP="$(id -gn "$TARGET_USER" 2>/dev/null || echo "$TARGET_USER")"

if [[ -n "$VENV_DIR_ARG" ]]; then
  VENV_DIR="$VENV_DIR_ARG"
elif [[ -n "${VERSIONEER_VENV_DIR:-}" ]]; then
  VENV_DIR="$VERSIONEER_VENV_DIR"
else
  VENV_DIR="$TARGET_HOME/.local/share/versioneer/venv"
fi

SUDO_BIN="sudo"
if [[ "$EUID_NOW" -eq 0 ]]; then
  SUDO_BIN=""
fi

chown_target() {
  # Best-effort: files created as root must stay usable by the user.
  if [[ "$EUID_NOW" -eq 0 && -n "$TARGET_USER" && "$TARGET_USER" != "root" ]]; then
    chown -R "$TARGET_USER:$TARGET_GROUP" "$1" 2>/dev/null || true
  fi
}

if [[ "$IS_ROOT_SUDO" -eq 1 ]]; then
  echo "==> sudo detected: installing for user $TARGET_USER ($TARGET_HOME), shims enabled"
fi

# 1. Refuse system-python installs: this script always creates/uses a venv.
if [[ -z "${VIRTUAL_ENV:-}" ]]; then
  echo "==> creating venv at $VENV_DIR (system python is never used for packages)"
fi

# 2. Preflight
command -v python3 >/dev/null || { echo "error: python3 not found" >&2; exit 1; }
PYVER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
echo "==> python3 $PYVER detected"
python3 -c 'import sys; assert sys.version_info >= (3,12), "need >=3.12"' \
  || { echo "error: Python 3.12+ required (found $PYVER)" >&2; exit 1; }
command -v git >/dev/null || { echo "error: git not found — sudo pacman -S git" >&2; exit 1; }

# 3. Create venv if needed
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  python3 -m venv "$VENV_DIR"
  chown_target "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip -q

# 4. Install versioneer into the venv only
if [[ "$DEV" -eq 1 ]]; then
  python -m pip install -e "$REPO_ROOT"
else
  python -m pip install "$REPO_ROOT"
fi
chown_target "$VENV_DIR"

# 5. Completions (user-level, shell-agnostic)
# NOTE: bash/fish lazy-load completions by command name, so each name
# needs its own file even though the content covers both.
if [[ "$INSTALL_COMPLETIONS" -eq 1 ]]; then
  mkdir -p "$TARGET_HOME/.local/share/bash-completion/completions" \
           "$TARGET_HOME/.config/fish/completions" "$TARGET_HOME/.zfunc"
  cp "$REPO_ROOT/installer/completions/versioneer.bash" \
     "$TARGET_HOME/.local/share/bash-completion/completions/versioneer" 2>/dev/null || true
  cp "$TARGET_HOME/.local/share/bash-completion/completions/versioneer" \
     "$TARGET_HOME/.local/share/bash-completion/completions/vers" 2>/dev/null || true
  cp "$REPO_ROOT/installer/completions/versioneer.fish" \
     "$TARGET_HOME/.config/fish/completions/versioneer.fish" 2>/dev/null || true
  cp "$TARGET_HOME/.config/fish/completions/versioneer.fish" \
     "$TARGET_HOME/.config/fish/completions/vers.fish" 2>/dev/null || true
  cp "$REPO_ROOT/installer/completions/versioneer.zsh" \
     "$TARGET_HOME/.zfunc/_versioneer" 2>/dev/null || true
  cp "$TARGET_HOME/.zfunc/_versioneer" \
     "$TARGET_HOME/.zfunc/_vers" 2>/dev/null || true
  chown_target "$TARGET_HOME/.local/share/bash-completion"
  chown_target "$TARGET_HOME/.config/fish"
  chown_target "$TARGET_HOME/.zfunc"
  echo "==> completions installed for versioneer + vers (restart shell or source them)"
fi

# 6. Symlink venv binaries into ~/.local/bin (already on PATH for
# bash/fish/zsh). The venv itself stays the install target; symlinks
# just make both entry points resolvable without manual PATH edits.
# NOTE: ~/.local/bin is NOT in sudo's secure_path, so `sudo vers`
# only resolves via the /usr/local/bin shims installed below (on by
# default; --no-system-shim to skip). Two supported paths:
#   a) preferred for tracking root-owned files: just run `vers target add`
#      as your user — it elevates only the read via `sudo cat/stat`;
#   b) full-root runs (`sudo vers status`, system unit): use the
#      /usr/local/bin shims (installed by default).
mkdir -p "$TARGET_HOME/.local/bin"
for bin in versioneer vers; do
  if [[ -x "$VENV_DIR/bin/$bin" ]]; then
    ln -sf "$VENV_DIR/bin/$bin" "$TARGET_HOME/.local/bin/$bin"
  else
    echo "warn: $VENV_DIR/bin/$bin not found after install — reinstall may have failed" >&2
  fi
done
chown_target "$TARGET_HOME/.local/bin"

# 7. Optional system shims for sudo / system unit (requires sudo once).
# The system unit ExecStart=/usr/local/bin/versioneer needs these to exist.
if [[ "$SYSTEM_SHIM" -eq 1 ]]; then
  for bin in versioneer vers; do
    src="$VENV_DIR/bin/$bin"
    if [[ -x "$src" ]]; then
      # Wrapper (not a symlink into $HOME) so root can exec it even when
      # $HOME differs under sudo. Re-run the installer after --venv moves
      # (shims embed the venv path).
      wrapper="#!/bin/sh
exec \"$src\" \"\$@\"
"
      # shellcheck disable=SC2086
      if [[ -n "$SUDO_BIN" ]]; then
        echo "$wrapper" | $SUDO_BIN tee "/usr/local/bin/$bin" >/dev/null \
          && $SUDO_BIN chmod 755 "/usr/local/bin/$bin" \
          && echo "==> system shim: /usr/local/bin/$bin -> $src" \
          || echo "warn: could not install /usr/local/bin/$bin (sudo failed — \`sudo vers\` will not resolve; re-run installer once sudo works)" >&2
      else
        echo "$wrapper" > "/usr/local/bin/$bin" \
          && chmod 755 "/usr/local/bin/$bin" \
          && echo "==> system shim: /usr/local/bin/$bin -> $src" \
          || echo "warn: could not install /usr/local/bin/$bin" >&2
      fi
    else
      echo "warn: $src not executable — skipping /usr/local/bin/$bin" >&2
    fi
  done
  if [[ ! -x /usr/local/bin/versioneer || ! -x /usr/local/bin/vers ]]; then
    echo "==> note: system shim(s) missing — \`sudo vers\` will fail with 'command not found'"
    echo "    (expected when sudo was unavailable: ~/.local/bin is not in sudo secure_path)."
    echo "    Tracking root-owned files does NOT need \`sudo vers\`: just run"
    echo "    \`vers target add /etc/...\` as your user (sudo read is automatic)."
    echo "    To fix sudo runs, re-run: ./installer/install.sh (shims are default)"
  fi
else
  echo "==> note: --no-system-shim: skipping /usr/local/bin shims — \`sudo vers\` will fail"
  echo "    (expected: ~/.local/bin is not in sudo secure_path)."
  echo "    Tracking root-owned files does NOT need \`sudo vers\`: just run"
  echo "    \`vers target add /etc/...\` as your user (sudo read is automatic)."
fi

# 8. Verify: show exactly what landed where (built-in `ls -l`,
# so a missing `sudo vers` is visible immediately, not discovered later).
echo "==> verify:"
for bin in versioneer vers; do
  if [[ -x "$VENV_DIR/bin/$bin" ]]; then
    echo "    venv:            $VENV_DIR/bin/$bin (executable)"
  else
    echo "    venv:            $VENV_DIR/bin/$bin (MISSING — install failed?)" >&2
  fi
  link="$TARGET_HOME/.local/bin/$bin"
  if [[ -L "$link" ]]; then
    echo "    user link:       $link -> $(readlink "$link" || echo '?')"
  elif [[ -e "$link" ]]; then
    echo "    user link:       $link (exists, not a symlink)"
  else
    echo "    user link:       $link (MISSING)" >&2
  fi
  shim="/usr/local/bin/$bin"
  if [[ -x "$shim" ]]; then
    dest="$(grep -m1 '^exec ' "$shim" 2>/dev/null | sed 's/^exec //; s/ "\$@"$//' || true)"
    echo "    system shim:     $shim -> ${dest:-?} (executable: \`sudo $bin\` should resolve)"
  else
    echo "    system shim:     $shim (MISSING — \`sudo $bin\` will fail with 'command not found')"
  fi
done
if command -v vers >/dev/null 2>&1; then
  echo "    PATH:            vers resolves as $(command -v vers)"
else
  echo "    PATH:            vers NOT on PATH (add $TARGET_HOME/.local/bin to PATH or re-login)"
fi

# 9. OpenCode plugin (optional global registration).
# auto: no `opencode` binary -> normal install, no prompt; binary present
# -> ask (default N). Explicit --opencode-plugin / --no-opencode-plugin
# skips the detection/prompt entirely.
opencode_register() {
  PLUGIN_SRC="$REPO_ROOT/plugins/opencode-versioneer"
  if [[ ! -f "$PLUGIN_SRC/index.ts" ]]; then
    echo "warn: OpenCode plugin source missing ($PLUGIN_SRC) — skipping" >&2
    return 0
  fi
  if ! command -v opencode >/dev/null 2>&1; then
    echo "warn: opencode not found — registering the plugin config anyway" >&2
    echo "      (install opencode later; entry points at $PLUGIN_SRC)" >&2
  fi
  if python3 "$REPO_ROOT/installer/opencode-plugin.py" \
      "$TARGET_HOME/.config/opencode" "$PLUGIN_SRC"; then
    chown_target "$TARGET_HOME/.config/opencode"
    echo "==> OpenCode plugin registered (restart opencode service to load it)"
  else
    echo "warn: automatic OpenCode plugin registration needs a manual edit (see above)" >&2
  fi
  # Local file:// plugins do not auto-install deps (only npm packages do),
  # so install @opencode/plugin here when a JS package manager exists.
  if [[ -f "$PLUGIN_SRC/package.json" ]]; then
    if command -v bun >/dev/null 2>&1; then
      (cd "$PLUGIN_SRC" && bun install) && echo "==> OpenCode plugin dependencies installed (bun)" \
        || echo "warn: 'bun install' failed in $PLUGIN_SRC — run it manually" >&2
    elif command -v npm >/dev/null 2>&1; then
      (cd "$PLUGIN_SRC" && npm install) && echo "==> OpenCode plugin dependencies installed (npm)" \
        || echo "warn: 'npm install' failed in $PLUGIN_SRC — run it manually" >&2
    else
      echo "warn: bun/npm not found — install one and run '(cd $PLUGIN_SRC && bun install)' so @opencode/plugin resolves" >&2
    fi
    chown_target "$PLUGIN_SRC/node_modules" 2>/dev/null || true
    chown_target "$PLUGIN_SRC/bun.lock" 2>/dev/null || true
    chown_target "$PLUGIN_SRC/bun.lockb" 2>/dev/null || true
    chown_target "$PLUGIN_SRC/package-lock.json" 2>/dev/null || true
  fi
}
if [[ "$OPENCODE_PLUGIN" == "no" ]]; then
  echo "==> note: --no-opencode-plugin: skipping OpenCode plugin registration"
elif [[ "$OPENCODE_PLUGIN" == "yes" ]]; then
  opencode_register
elif command -v opencode >/dev/null 2>&1; then
  REPLY=""
  if [[ -t 0 ]]; then
    read -r -p "Install the versioneer OpenCode plugin (global ~/.config/opencode)? [y/N] " REPLY || REPLY=""
  fi
  case "$REPLY" in
    [yY]*) opencode_register ;;
    *) echo "==> note: skipping OpenCode plugin (see plugins/opencode-versioneer/README.md to add it later)" ;;
  esac
else
  echo "==> note: opencode not detected — skipping plugin registration (see plugins/opencode-versioneer/README.md)"
fi

echo "==> done. Activate with: source \"$VENV_DIR/bin/activate\""
echo "==> Run: versioneer --help (shorthand: vers --help; via ~/.local/bin symlinks)"
