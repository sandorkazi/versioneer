#!/usr/bin/env bash
# Versioneer uninstaller — removes venv + units, keeps stores unless --purge-stores.
# Usage: ./installer/uninstall.sh [--purge-stores] [--venv DIR]
set -euo pipefail

PURGE=0
VENV_DIR_ARG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge-stores) PURGE=1; shift ;;
    --venv=*) VENV_DIR_ARG="${1#--venv=}"; shift ;;
    --venv) VENV_DIR_ARG="${2:?--venv needs a DIR}"; shift 2 ;;
    -h|--help) echo "Usage: uninstall.sh [--purge-stores] [--venv DIR]"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Sudo-aware: `sudo ./uninstall.sh` must clean the invoking user's files,
# not /root's (mirrors installer/install.sh).
EUID_NOW="${EUID:-$(id -u)}"
TARGET_HOME="${HOME:-/root}"
if [[ "$EUID_NOW" -eq 0 && -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
  CANDIDATE="$(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6 || true)"
  if [[ -n "$CANDIDATE" && -d "$CANDIDATE" ]]; then
    TARGET_HOME="$CANDIDATE"
  elif [[ -d "/home/$SUDO_USER" ]]; then
    TARGET_HOME="/home/$SUDO_USER"
  fi
  echo "==> sudo detected: uninstalling for $SUDO_USER ($TARGET_HOME)"
fi

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

echo "==> stopping/disabling units (if present)"
if [[ "$EUID_NOW" -eq 0 && -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
  sudo -u "$SUDO_USER" systemctl --user disable --now versioneer-user.service 2>/dev/null || true
else
  systemctl --user disable --now versioneer-user.service 2>/dev/null || true
fi
# shellcheck disable=SC2086
$SUDO_BIN systemctl disable --now versioneer-system.service 2>/dev/null || true

echo "==> removing venv at $VENV_DIR"
rm -rf "$VENV_DIR"

echo "==> removing ~/.local/bin symlinks (only if they point at this venv)"
for bin in versioneer vers; do
  link="$TARGET_HOME/.local/bin/$bin"
  if [[ -L "$link" ]]; then
    target="$(readlink "$link" || true)"
    case "$target" in
      "$VENV_DIR/bin/"*) rm -f "$link" && echo "removed $link" ;;
      *) echo "kept $link (points elsewhere: $target)" ;;
    esac
  fi
done

echo "==> removing /usr/local/bin shims (needs sudo, only versioneer wrappers)"
for bin in versioneer vers; do
  if [[ -f "/usr/local/bin/$bin" ]] && grep -q "versioneer" "/usr/local/bin/$bin" 2>/dev/null; then
    # shellcheck disable=SC2086
    $SUDO_BIN rm -f "/usr/local/bin/$bin" && echo "removed /usr/local/bin/$bin" || echo "kept /usr/local/bin/$bin (sudo failed)"
  fi
done

echo "==> removing completions"
rm -f "$TARGET_HOME/.local/share/bash-completion/completions/versioneer" \
      "$TARGET_HOME/.local/share/bash-completion/completions/vers" \
      "$TARGET_HOME/.config/fish/completions/versioneer.fish" \
      "$TARGET_HOME/.config/fish/completions/vers.fish" \
      "$TARGET_HOME/.zfunc/_versioneer" "$TARGET_HOME/.zfunc/_vers" 2>/dev/null || true

read -r -p "Remove ~/.config/versioneer TOMLs? [y/N] " ans
if [[ "$ans" =~ ^[Yy]$ ]]; then
  rm -rf "$TARGET_HOME/.config/versioneer"
  echo "removed $TARGET_HOME/.config/versioneer"
else
  echo "kept $TARGET_HOME/.config/versioneer"
fi

if [[ "$PURGE" -eq 1 ]]; then
  read -r -p "Delete ~/versioneer-store/* too? [y/N] " ans2
  if [[ "$ans2" =~ ^[Yy]$ ]]; then
    rm -rf "$TARGET_HOME"/versioneer-store
    echo "purged $TARGET_HOME/versioneer-store"
  fi
else
  echo "kept ~/versioneer-store/<name> (pass --purge-stores to delete)"
fi

echo "done."
