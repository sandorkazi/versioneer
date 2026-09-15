#!/usr/bin/env bash
# Versioneer uninstaller — removes venv + units, keeps stores unless --purge-stores.
# Usage: ./installer/uninstall.sh [--purge-stores] [--venv DIR]
set -euo pipefail

PURGE=0
VENV_DIR="${VERSIONEER_VENV_DIR:-$HOME/.local/share/versioneer/venv}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge-stores) PURGE=1; shift ;;
    --venv=*) VENV_DIR="${1#--venv=}"; shift ;;
    --venv) VENV_DIR="${2:?--venv needs a DIR}"; shift 2 ;;
    -h|--help) echo "Usage: uninstall.sh [--purge-stores] [--venv DIR]"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> stopping/disabling units (if present)"
systemctl --user disable --now versioneer-user.service 2>/dev/null || true
sudo systemctl disable --now versioneer-system.service 2>/dev/null || true

echo "==> removing venv at $VENV_DIR"
rm -rf "$VENV_DIR"

echo "==> removing completions"
rm -f "$HOME/.local/share/bash-completion/completions/versioneer" \
      "$HOME/.config/fish/completions/versioneer.fish" \
      "$HOME/.zfunc/_versioneer" 2>/dev/null || true

read -r -p "Remove ~/.config/versioneer TOMLs? [y/N] " ans
if [[ "$ans" =~ ^[Yy]$ ]]; then
  rm -rf "$HOME/.config/versioneer"
  echo "removed ~/.config/versioneer"
else
  echo "kept ~/.config/versioneer"
fi

if [[ "$PURGE" -eq 1 ]]; then
  read -r -p "Delete ~/versioneer-store/* too? [y/N] " ans2
  if [[ "$ans2" =~ ^[Yy]$ ]]; then
    rm -rf "$HOME"/versioneer-store
    echo "purged ~/versioneer-store"
  fi
else
  echo "kept ~/versioneer-store/<name> (pass --purge-stores to delete)"
fi

echo "done."
