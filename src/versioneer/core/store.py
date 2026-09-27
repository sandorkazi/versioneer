"""Git wrapper (Phase 2: init/clone/commit/push/pull).

Wrapper only — called by CLI and (only with auto_commit opt-in) by daemon/hook.
Never auto-pushes unless auto_push=true (enforced by callers).

LFS was removed (see README §17 Roadmap): all artifacts are plain git
objects. Binaries therefore accumulate full history in the store; the
nonversioned remote backend (planned separately) is the intended home for
binaries and limited-revision targets.

Q3 decision (c) — per-target ``retention = {count, age}``:
warn by default. History is always kept; this module never
rewrites/squashes history (no ``filter-repo``/``rebase``/squash) so
``plan.json`` hashes and ``target remove`` history stay intact.
``commit --prune-retention`` is accepted but deprecated: automatic pruning
was removed with LFS, so it only prints manual-prune guidance.

Offline behavior: status/commit work offline; push/pull raise GitError with
an "offline — ..." / "upstream ..." prefix so CLI can defer with a clear message.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path


class GitError(RuntimeError):
    pass


def _run_git(args: list[str], cwd: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        raise GitError("git not found on PATH") from e
    except subprocess.CalledProcessError as e:
        raise GitError((e.stderr or e.stdout or "").strip() or f"git {' '.join(args)} failed")
    return proc.stdout.strip()


def is_repo(store: Path) -> bool:
    return (store / ".git").is_dir() or (store / ".git").is_file()


def ensure_repo(store: Path) -> None:
    store.mkdir(parents=True, exist_ok=True)
    try:
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
    finally:
        # Transparent sudo: a root-via-sudo run must leave the user's
        # store usable by the user (no root-owned .git/index).
        try:
            from versioneer.core import elevate as _elev

            _elev.fix_store_after_write(store)
        except (OSError, ImportError):
            pass


def upstream_reachable(store: Path, url: str = "", timeout: int = 10) -> tuple[bool | None, str]:
    """Probe upstream reachability via `git ls-remote` (warn-only helper).

    Returns (True, detail) when reachable, (False, detail) when unreachable/
    auth-failed, (None, detail) when skipped (not a repo / no upstream).
    Never raises: timeouts, missing git, and auth failures all yield
    (False, ...) so `doctor` can warn without crashing offline.
    """
    if not is_repo(store):
        return None, "not a git repo"
    target = (url or "").strip() or get_upstream(store)
    if not target:
        return None, "no upstream configured"
    git = shutil.which("git")
    if not git:
        return False, "git not found on PATH (offline?)"
    try:
        proc = subprocess.run(
            [git, "ls-remote", "--heads", target, "HEAD"],
            cwd=store,
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"upstream unreachable (ls-remote timed out after {timeout}s, offline?)"
    except OSError as e:
        return False, f"upstream unreachable: {e} (offline?)"
    if proc.returncode == 0:
        return True, "upstream reachable"
    detail = ((proc.stderr or proc.stdout) or "").strip().splitlines()
    first = detail[0].strip()[:300] if detail else "ls-remote failed"
    low = first.lower()
    if any(
        h in low
        for h in (
            "authentication failed",
            "permission denied",
            "repository not found",
            "could not read",
            "askpass",
        )
    ):
        return False, f"upstream auth/permission failed: {first} (warn-only)"
    return False, f"upstream unreachable: {first} (warn-only, offline?)"


def _has_staged_changes(store: Path) -> bool:
    """True when the index has staged changes vs HEAD.

    Uses ``git diff --cached --quiet`` (staged-only) so unrelated worktree
    dirt — e.g. a dirty gitlink (``modified content``) from an embedded
    ``.git`` inside a tracked dir, or an untracked ``.gitattributes`` — never
    triggers an empty commit attempt. ``git status --porcelain`` is global
    and must not be used for this decision.
    """
    try:
        proc = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=store,
            capture_output=True,
        )
    except FileNotFoundError:
        return True  # let the caller surface "git not found"
    except OSError:
        return True
    return proc.returncode != 0


def staged_files(store: Path) -> list[str]:
    """Staged paths (vs HEAD) as posix rels; [] on non-repo/error.

    Used by the per-target auto-commit guard: a silent auto-commit must
    never sweep in pre-existing staged changes belonging to
    non-autocommittable targets (messed-up state: files staged but never
    committed explicitly). Only staged changes matter — ``git commit``
    without ``-a`` commits the index, so unstaged worktree dirt and
    untracked files are safe and ignored here.
    """
    if not is_repo(store):
        return []
    try:
        out = _run_git(["diff", "--cached", "--name-only"], store)
    except GitError:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def add_and_commit(store: Path, rel_paths: list[str], message: str) -> str | None:
    ensure_repo(store)
    try:
        if rel_paths:
            _run_git(["add", "--", *rel_paths], store)
        else:
            _run_git(["add", "-A"], store)
        if not _has_staged_changes(store):
            return None
        try:
            _run_git(["commit", "-m", message], store)
        except GitError as e:
            # Defensive: a concurrent change can still leave nothing staged.
            low = str(e).lower()
            if "nothing to commit" in low or "no changes added to commit" in low:
                return None
            raise
        return _run_git(["rev-parse", "HEAD"], store)
    finally:
        try:
            from versioneer.core import elevate as _elev

            _elev.fix_store_after_write(store, rel_paths)
        except (OSError, ImportError):
            pass


def rm_and_commit(store: Path, rel_paths: list[str], message: str) -> str | None:
    ensure_repo(store)
    try:
        if rel_paths:
            # --cached-safe: artifact lives only in store, so plain rm is right
            _run_git(["rm", "-r", "--", *rel_paths], store)
        status = _run_git(["status", "--porcelain"], store)
        if not status:
            return None
        _run_git(["commit", "-m", message], store)
        return _run_git(["rev-parse", "HEAD"], store)
    finally:
        try:
            from versioneer.core import elevate as _elev

            _elev.fix_store_after_write(store, rel_paths)
        except (OSError, ImportError):
            pass


def stage_rm(store: Path, rel_paths: list[str]) -> None:
    """Stage deletions (``git rm``) without committing. Raises GitError.

    Used when a target's store artifact changes kind (e.g. git content <->
    remote pointer migrations): the caller batches the staged deletion with
    new content into a single ``add_and_commit``.
    """
    ensure_repo(store)
    try:
        if rel_paths:
            # --cached-safe: artifact lives only in store, so plain rm is right
            _run_git(["rm", "-r", "--", *rel_paths], store)
    finally:
        try:
            from versioneer.core import elevate as _elev

            _elev.fix_store_after_write(store, rel_paths)
        except (OSError, ImportError):
            pass


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
    try:
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
    finally:
        try:
            from versioneer.core import elevate as _elev

            _elev.fix_store_after_write(store)
        except (OSError, ImportError):
            pass


def remove_upstream(store: Path) -> bool:
    """Remove the origin remote (go local-only). Returns True when removed."""
    if not is_repo(store):
        return False
    if not get_upstream(store):
        return False
    try:
        _run_git(["remote", "remove", "origin"], store)
    finally:
        try:
            from versioneer.core import elevate as _elev

            _elev.fix_store_after_write(store)
        except (OSError, ImportError):
            pass
    return True


def _offline_wrap(action: str, err: GitError) -> GitError:
    msg = str(err).strip()
    low = msg.lower()
    offline_hints = (
        "could not resolve",
        "connection",
        "network is unreachable",
        "no route to host",
        "timed out",
        "unable to connect",
        "authentication failed",
        "permission denied (publickey)",
        "repository not found",
        "name or service not known",
    )
    if any(h in low for h in offline_hints) or not msg:
        return GitError(
            f"offline — {action} deferred (upstream unreachable): {msg or 'no upstream response'}"
        )
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
    args = (
        ["push", "-u", "origin", branch]
        if branch and branch != "HEAD"
        else ["push", "-u", "origin", "HEAD"]
    )
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
            ["git", "clone", url, str(dest)],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        raise GitError("git not found on PATH") from e
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip() or "git clone failed"
        raise GitError(f"offline — clone deferred (upstream unreachable): {detail}") from e
    try:
        ensure_repo(dest)
    finally:
        try:
            from versioneer.core import elevate as _elev

            _elev.fix_ownership(dest, recursive=True)
        except (OSError, ImportError):
            pass


def show_head_file(store: Path, rel_path: str) -> bytes | None:
    """Artifact bytes at HEAD, or None when missing/unborn."""
    try:
        proc = subprocess.run(
            ["git", "show", f"HEAD:{rel_path}"],
            cwd=store,
            check=True,
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


_AGE_RE = re.compile(r"^\s*(\d+)\s*([smhdwSMHDW])?\s*$")
_AGE_MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_retention_age(age: object) -> int | None:
    """Parse a retention age like ``30d`` into seconds.

    Accepts ``30d``/``7d``/``12h``/``5m``/``10s``/``2w`` (case-insensitive,
    optional whitespace) plus bare numbers (seconds) and ints. Returns
    seconds as int, or None when missing/invalid. ``30d`` -> 2592000.
    """
    if age is None:
        return None
    if isinstance(age, bool):
        return None
    if isinstance(age, (int, float)):
        try:
            secs = int(age)
        except (ValueError, OverflowError):
            return None
        return secs if secs > 0 else None
    text = str(age).strip()
    if not text:
        return None
    m = _AGE_RE.match(text)
    if not m:
        return None
    value = int(m.group(1))
    suffix = (m.group(2) or "s").lower()
    mult = _AGE_MULT.get(suffix)
    if mult is None or value < 1:
        return None
    return value * mult


def artifact_commit_times(store: Path, rel_path: str, limit: int = 0) -> list[int]:
    """Unix timestamps (newest first) for commits touching an artifact.

    Uses ``git log --format=%ct``; returns [] on error/non-repo (warn-only
    callers treat this as "unknown age").
    """
    try:
        args = ["log", "--format=%ct", "--", rel_path]
        if limit and limit > 0:
            args = ["log", f"-n{limit}", "--format=%ct", "--", rel_path]
        out = _run_git(args, store)
    except GitError:
        return []
    stamps: list[int] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            stamps.append(int(line))
        except ValueError:
            continue
    return stamps


def oldest_artifact_commit_time(store: Path, rel_path: str) -> int | None:
    """Oldest commit timestamp for an artifact, or None when unknown."""
    stamps = artifact_commit_times(store, rel_path)
    return min(stamps) if stamps else None


def retention_age_warning(
    oldest_ts: int | None, retention: dict, now: float | None = None
) -> str | None:
    """Warn when the oldest history is older than retention.age.

    Never prunes — warn-only. Returns warning string or None.
    """
    if not isinstance(retention, dict):
        return None
    max_age = parse_retention_age(retention.get("age"))
    if max_age is None or oldest_ts is None:
        return None
    now_s = time.time() if now is None else float(now)
    age_s = int(now_s - int(oldest_ts))
    if age_s > max_age:
        return (
            f"retention: oldest history {age_s // 86400}d old exceeds "
            f"retention.age={retention.get('age')} "
            "(history kept; inspect with `git log` — see docs)"
        )
    return None


def prune_guidance(rel_path: str, retention: dict) -> str:
    """Manual prune guidance (history kept, never silent squash)."""
    return (
        f"retention prune (manual-only, history kept by default) for {rel_path} "
        f"{retention}: inspect with `git log --oneline -- {rel_path}`; "
        "automatic pruning was removed with LFS (see README §14 Storage layout); "
        "deep history edits via `git filter-repo` are manual-only "
        "(they change hashes validated by plan.json)"
    )


def check_retention(
    store: Path, rel_path: str, retention: dict, now: float | None = None
) -> list[str]:
    """Auto-check helper: count + age warnings via ``git log``.

    Combines :func:`count_artifact_commits` and commit timestamps into
    warn-only messages (history kept by default).
    """
    warnings: list[str] = []
    if not isinstance(retention, dict) or not retention:
        return warnings
    count = count_artifact_commits(store, rel_path)
    oldest: int | None = None
    if parse_retention_age(retention.get("age")) is not None:
        oldest = oldest_artifact_commit_time(store, rel_path)
    warn = retention_warning(count, retention, oldest, now)
    if warn:
        warnings.append(warn)
    return warnings


def retention_warning(
    count: int, retention: dict, oldest_ts: int | None = None, now: float | None = None
) -> str | None:
    """Non-destructive Q3 policy (c): warn when history exceeds retention.

    Checks ``retention.count`` (commit count) and, when ``oldest_ts`` is
    given, ``retention.age`` (e.g. ``30d`` parsed via :func:`parse_retention_age`).
    Never squashes (that would break plan.json hashes + remove history).
    Returns combined warning string or None. History kept; automatic
    pruning was removed with LFS (``commit --prune-retention`` is
    deprecated and warn-only).
    """
    if not isinstance(retention, dict):
        return None
    reasons: list[str] = []
    want = retention.get("count")
    if isinstance(want, int) and want >= 1 and count > want:
        reasons.append(
            f"{count} commits exceed retention.count={want} "
            "(history kept; no automatic pruning since LFS removal — "
            "manual `git filter-repo` only, see docs)"
        )
    age_warn = retention_age_warning(oldest_ts, retention, now)
    if age_warn:
        # retention_age_warning already carries the "retention: " prefix
        reasons.append(age_warn.removeprefix("retention: "))
    if not reasons:
        return None
    return "retention: " + "; ".join(reasons)
