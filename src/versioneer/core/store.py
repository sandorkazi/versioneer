"""Git + LFS wrapper (Phase 2: init/clone/commit/push/pull).

Wrapper only — called by CLI and (only with auto_commit opt-in) by daemon/hook.
Never auto-pushes unless auto_push=true (enforced by callers).

Offline behavior: status/commit work offline; push/pull raise GitError with
an "offline — ..." / "upstream ..." prefix so CLI can defer with a clear message.
Missing git-lfs degrades to warn-only (plain git still works in tests).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _run_git(args: list[str], cwd: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, check=True,
            capture_output=True, text=True,
        )
    except FileNotFoundError as e:
        raise GitError("git not found on PATH") from e
    except subprocess.CalledProcessError as e:
        raise GitError((e.stderr or e.stdout or "").strip() or f"git {' '.join(args)} failed")
    return proc.stdout.strip()


def lfs_available() -> bool:
    """True when git-lfs binary is on PATH."""
    return shutil.which("git-lfs") is not None


def is_repo(store: Path) -> bool:
    return (store / ".git").is_dir() or (store / ".git").is_file()


def ensure_repo(store: Path) -> None:
    store.mkdir(parents=True, exist_ok=True)
    if not is_repo(store):
        _run_git(["init"], store)
    # local identity fallback so commits work in tests/fresh machines
    try:
        email = _run_git(["config", "user.email"], store)
    except GitError:
        email = ""
    if not email:
        _run_git(["config", "user.email", "versioneer@localhost"], store)
        _run_git(["config", "user.name", "versioneer"], store)


def ensure_lfs(store: Path, rel_paths: list[str]) -> str | None:
    """Ensure git-lfs tracks binary artifacts. Returns warning or None.

    Warn-only when git-lfs is missing or `lfs install` fails — plain git
    still tracks the file (tests + machines without LFS keep working).
    """
    if not lfs_available():
        return "git-lfs not found — binary targets need 'pacman -S git-lfs' (warn-only)"
    try:
        _run_git(["lfs", "install", "--local"], store)
    except GitError as e:
        return f"git lfs install failed: {e} (warn-only)"
    ga = store / ".gitattributes"
    existing = ga.read_text(encoding="utf-8", errors="replace") if ga.exists() else ""
    lines = existing.splitlines()
    changed = False
    for rel in rel_paths:
        # track exact artifact path + a generic *.bin rule
        for pattern in (rel, "*.bin"):
            entry = f"{pattern} filter=lfs diff=lfs merge=lfs -text"
            if pattern not in existing:
                lines.append(entry)
                changed = True
    if changed:
        ga.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return None


def add_and_commit(store: Path, rel_paths: list[str], message: str) -> str | None:
    ensure_repo(store)
    if rel_paths:
        _run_git(["add", "--", *rel_paths], store)
    else:
        _run_git(["add", "-A"], store)
    status = _run_git(["status", "--porcelain"], store)
    if not status:
        return None
    _run_git(["commit", "-m", message], store)
    return _run_git(["rev-parse", "HEAD"], store)


def rm_and_commit(store: Path, rel_paths: list[str], message: str) -> str | None:
    ensure_repo(store)
    if rel_paths:
        # --cached-safe: artifact lives only in store, so plain rm is right
        _run_git(["rm", "-r", "--", *rel_paths], store)
    status = _run_git(["status", "--porcelain"], store)
    if not status:
        return None
    _run_git(["commit", "-m", message], store)
    return _run_git(["rev-parse", "HEAD"], store)


def log_lines(store: Path, n: int = 10, rel_path: str = "") -> list[str]:
    """Recent oneline log; optionally filtered to one artifact path."""
    try:
        if rel_path:
            out = _run_git(["log", f"-n{n}", "--oneline", "--", rel_path], store)
        else:
            out = _run_git(["log", f"-n{n}", "--oneline"], store)
    except GitError:
        return []
    return out.splitlines() if out else []


def status_porcelain(store: Path) -> str:
    """porcelain status of the store (empty when clean); [] on non-repo."""
    if not is_repo(store):
        return ""
    try:
        return _run_git(["status", "--porcelain"], store)
    except GitError:
        return ""


def get_upstream(store: Path) -> str:
    """Configured origin URL or '' when none/corrupt."""
    if not is_repo(store):
        return ""
    try:
        return _run_git(["remote", "get-url", "origin"], store)
    except GitError:
        return ""


def set_upstream(store: Path, url: str) -> None:
    """Add or update origin without network access (offline-safe)."""
    ensure_repo(store)
    if not url:
        return
    try:
        current = get_upstream(store)
    except GitError:
        current = ""
    if current == url:
        return
    if current:
        _run_git(["remote", "set-url", "origin", url], store)
    else:
        _run_git(["remote", "add", "origin", url], store)


def _offline_wrap(action: str, err: GitError) -> GitError:
    msg = str(err).strip()
    low = msg.lower()
    offline_hints = ("could not resolve", "connection", "network is unreachable",
                     "no route to host", "timed out", "unable to connect",
                     "authentication failed", "permission denied (publickey)",
                     "repository not found", "name or service not known")
    if any(h in low for h in offline_hints) or not msg:
        return GitError(f"offline — {action} deferred (upstream unreachable): {msg or 'no upstream response'}")
    return GitError(f"offline — {action} deferred: {msg}")


def _current_branch(store: Path) -> str:
    try:
        return _run_git(["rev-parse", "--abbrev-ref", "HEAD"], store)
    except GitError:
        return ""


def push(store: Path) -> str:
    """Push to origin. Raises GitError with offline-defer message on failure."""
    if not is_repo(store):
        raise GitError("store is not a git repo (run `config create` or `bootstrap`)")
    if not get_upstream(store):
        raise GitError("offline — push deferred: no upstream configured (set meta.upstream)")
    branch = _current_branch(store)
    args = ["push", "-u", "origin", branch] if branch and branch != "HEAD" else ["push", "-u", "origin", "HEAD"]
    try:
        return _run_git(args, store)
    except GitError as e:
        raise _offline_wrap("push", e) from e


def pull(store: Path) -> str:
    """Pull from origin (ff-only safe default). Raises GitError on failure."""
    if not is_repo(store):
        raise GitError("store is not a git repo (run `config create` or `bootstrap`)")
    if not get_upstream(store):
        raise GitError("offline — pull deferred: no upstream configured (set meta.upstream)")
    try:
        # ff-only avoids surprise merges on savegame churn
        return _run_git(["pull", "--ff-only"], store)
    except GitError as e:
        raise _offline_wrap("pull", e) from e


def clone(url: str, dest: Path) -> None:
    """Clone upstream into dest. Raises GitError (caller adds offline prefix)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if is_repo(dest) or (dest.exists() and any(dest.iterdir())):
        raise GitError(f"clone destination not empty: {dest}")
    try:
        subprocess.run(
            ["git", "clone", url, str(dest)], check=True,
            capture_output=True, text=True,
        )
    except FileNotFoundError as e:
        raise GitError("git not found on PATH") from e
    except subprocess.CalledProcessError as e:
        detail = ((e.stderr or e.stdout or "").strip() or "git clone failed")
        raise GitError(f"offline — clone deferred (upstream unreachable): {detail}") from e
    ensure_repo(dest)


def show_head_file(store: Path, rel_path: str) -> bytes | None:
    """Artifact bytes at HEAD, or None when missing/unborn."""
    try:
        proc = subprocess.run(
            ["git", "show", f"HEAD:{rel_path}"], cwd=store, check=True,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return proc.stdout


def count_artifact_commits(store: Path, rel_path: str) -> int:
    """Number of commits touching an artifact (for retention warnings)."""
    try:
        out = _run_git(["rev-list", "--count", "HEAD", "--", rel_path], store)
    except GitError:
        return 0
    try:
        return int(out.strip())
    except ValueError:
        return 0


def retention_warning(count: int, retention: dict) -> str | None:
    """Non-destructive Q3 policy (c): warn when history exceeds retention.

    Never squashes (that would break plan.json hashes + remove history).
    Returns warning string or None.
    """
    want = retention.get("count") if isinstance(retention, dict) else None
    if not isinstance(want, int) or want < 1:
        return None
    if count > want:
        return (f"retention: {count} commits exceed retention.count={want} "
                "(history kept; prune with `git filter-repo` or LFS prune — see docs)")
    return None
