#!/usr/bin/env bash
# Versioneer installer — venv-only, never touches system python.
# Usage:
#   ./installer/install.sh [--dev] [--venv DIR] [--no-completions]
#   VERSIONEER_VENV_DIR=/custom/path ./installer/install.sh
set -euo pipefail

DEV=0
VENV_DIR="${VERSIONEER_VENV_DIR:-$HOME/.local/share/versioneer/venv}"
INSTALL_COMPLETIONS=1
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev) DEV=1; shift ;;
    --venv=*) VENV_DIR="${1#--venv=}"; shift ;;
    --venv) VENV_DIR="${2:?--venv needs a DIR}"; shift 2 ;;
    --no-completions) INSTALL_COMPLETIONS=0; shift ;;
    -h|--help)
      echo "Usage: install.sh [--dev] [--venv DIR] [--no-completions]"
      exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

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
command -v git >/dev/null || { echo "error: git not found — sudo pacman -S git git-lfs" >&2; exit 1; }
if ! command -v git-lfs >/dev/null; then
  echo "warn: git-lfs not found — install with: sudo pacman -S git-lfs && git lfs install" >&2
fi

# 3. Create venv if needed
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  python3 -m venv "$VENV_DIR"
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

# 5. Completions (user-level, shell-agnostic)
if [[ "$INSTALL_COMPLETIONS" -eq 1 ]]; then
  mkdir -p "$HOME/.local/share/bash-completion/completions" \
           "$HOME/.config/fish/completions" "$HOME/.zfunc"
  cp "$REPO_ROOT/installer/completions/versioneer.bash" \
     "$HOME/.local/share/bash-completion/completions/versioneer" 2>/dev/null || true
  cp "$REPO_ROOT/installer/completions/versioneer.fish" \
     "$HOME/.config/fish/completions/versioneer.fish" 2>/dev/null || true
  cp "$REPO_ROOT/installer/completions/versioneer.zsh" \
     "$HOME/.zfunc/_versioneer" 2>/dev/null || true
  echo "==> completions installed (restart shell or source them)"
fi

echo "==> done. Activate with: source \"$VENV_DIR/bin/activate\""
echo "==> Run: versioneer --help  (ensure $VENV_DIR/bin is on PATH)"
echo "==> Never run 'sudo pip install' — system python stays untouched."
