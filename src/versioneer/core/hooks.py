"""Pre/post-deploy validation + restart hooks (Phase 9)."""

from __future__ import annotations

import subprocess


def run_hook(command: str, timeout: int = 60) -> tuple[bool, str]:
    """Run on_deploy hook via shell. Returns (ok, output).

    Empty command is a no-op success. Output is combined stdout+stderr,
    truncated to a sane size for the deploy-status file.
    """
    if not command or not command.strip():
        return True, ""
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout,
            check=False,
        )
    except FileNotFoundError as e:
        return False, f"hook not found: {e}"
    except subprocess.TimeoutExpired:
        return False, f"hook timed out after {timeout}s: {command}"
    except OSError as e:
        return False, f"hook failed to start: {e}"
    out = (proc.stdout or "") + (proc.stderr or "")
    out = out.strip()[-4000:]
    if proc.returncode != 0:
        return False, f"hook exit {proc.returncode}: {out}"
    return True, out
