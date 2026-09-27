"""Nonversioned remote backend for binaries / limited-revision targets.

Git stores only a small pointer file per target; content revisions live on
the remote with keep-last-N rotation enforced at commit time. v1 supports
the ``file`` backend (a local path, e.g. a mounted drive). Any other
backend name is rejected with a "planned" error.

Layout (file backend)::

    <root>/<artifact-rel>/rev-<UTC-ts>-<sha256hex>.blob

Blob names sort chronologically (timestamp prefix). Rotation keeps the
N newest blobs; anything older is deleted, so the remote holds exactly
the configured revision count — unlike git history, which keeps everything.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path


class RemoteError(RuntimeError):
    """Remote unreachable, misconfigured, or out of revisions."""


#: Backends implemented in v1. Anything else is rejected as "planned".
BACKENDS = ("file",)

#: Default keep-last-N when a remote has no explicit retention.
DEFAULT_RETENTION = 3

#: Suffix for the pointer file committed to git next to the artifact rel.
POINTER_SUFFIX = ".remote.json"


def remote_retention(remote: dict) -> int:
    """Keep-last-N for a remote dict (default 3)."""
    try:
        n = int((remote or {}).get("retention", DEFAULT_RETENTION))
    except (TypeError, ValueError):
        return DEFAULT_RETENTION
    return n if n >= 1 else DEFAULT_RETENTION


def validate_remote_dict(remote: object) -> list[str]:
    """Validate a ``[targets.remote]`` dict. Returns error strings."""
    if not isinstance(remote, dict):
        return ["invalid remote: must be a table {backend, root, retention}"]
    errors: list[str] = []
    backend = remote.get("backend", "file")
    if backend not in BACKENDS:
        errors.append(
            f"unsupported remote backend {backend!r} (v1 supports 'file' only; "
            "ftp/drive are planned)"
        )
    root = remote.get("root", "")
    if not root or not str(root).strip():
        errors.append("remote.root is required (e.g. /mnt/drive/vers-remote)")
    elif not Path(os.path.expandvars(os.path.expanduser(str(root)))).is_absolute():
        errors.append(f"remote.root must be an absolute path, got {root!r}")
    if "retention" in remote:
        try:
            n = int(remote["retention"])
        except (TypeError, ValueError):
            n = 0
        if n < 1:
            errors.append(
                f"invalid remote.retention {remote['retention']!r}: must be int >= 1"
            )
    return errors


def _require_file(backend: str) -> None:
    if backend != "file":
        raise RemoteError(
            f"unsupported remote backend {backend!r} (v1 supports 'file' only)"
        )


def expand_root(root: str) -> Path:
    """Expand + absolutize a file-backend root. Raises RemoteError."""
    path = Path(os.path.expandvars(os.path.expanduser(str(root or ""))))
    if not str(path).strip() or not path.is_absolute():
        raise RemoteError(f"remote root must be an absolute path, got {root!r}")
    return path


def artifact_dir(root: str, rel_posix: str) -> Path:
    """Directory holding an artifact's blobs (created on push)."""
    return expand_root(root) / rel_posix


def blob_name(content_hash: str, when: datetime | None = None) -> str:
    """Timestamped blob name embedding the full content hash.

    Microsecond resolution: savegame-style auto-commit can fire several
    commits per second, and second-resolution ties made rotation order
    arbitrary (it could prune the just-pushed blob).
    """
    now = when or datetime.now(UTC)
    ts = now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond:06d}"
    hexpart = content_hash.split(":", 1)[-1] if ":" in content_hash else content_hash
    return f"rev-{ts}Z-{hexpart}.blob"


def _blob_sort_key(name: str) -> str:
    return name


def list_blobs(backend: str, root: str, rel_posix: str) -> list[str]:
    """Blob names, newest first. [] when the artifact has no revisions yet."""
    _require_file(backend)
    top = artifact_dir(root, rel_posix)
    try:
        names = [p.name for p in top.iterdir() if p.is_file() and p.name.endswith(".blob")]
    except FileNotFoundError:
        return []
    except OSError as e:
        raise RemoteError(f"cannot list remote {top}: {e}")
    return sorted(names, key=_blob_sort_key, reverse=True)


def push_blob(
    backend: str, root: str, rel_posix: str, data: bytes, content_hash: str,
    when: datetime | None = None,
) -> str:
    """Store a new revision blob. Returns the blob name."""
    _require_file(backend)
    top = artifact_dir(root, rel_posix)
    try:
        top.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RemoteError(f"cannot create remote dir {top}: {e}")
    name = blob_name(content_hash, when)
    dest = top / name
    if not dest.exists():
        try:
            dest.write_bytes(data)
        except OSError as e:
            raise RemoteError(f"cannot write remote blob {dest}: {e}")
    return name


def fetch_blob(backend: str, root: str, rel_posix: str, name: str) -> bytes:
    """Read one blob. Raises RemoteError when missing/unreadable."""
    _require_file(backend)
    path = artifact_dir(root, rel_posix) / name
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise RemoteError(f"remote blob missing: {path} (run commit first)")
    except OSError as e:
        raise RemoteError(f"cannot read remote blob {path}: {e}")


def delete_blob(backend: str, root: str, rel_posix: str, name: str) -> None:
    """Delete one blob. Raises RemoteError on failure."""
    _require_file(backend)
    path = artifact_dir(root, rel_posix) / name
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        raise RemoteError(f"cannot delete remote blob {path}: {e}")


def prune_blobs(
    backend: str, root: str, rel_posix: str, keep: int, exclude: str | None = None
) -> list[str]:
    """Delete all but the newest ``keep`` blobs. Returns deleted names.

    ``exclude`` (the just-pushed blob) is never deleted: with coarse
    clock ties the newest-first order is arbitrary, so retention must
    not eat the revision it just stored.
    """
    names = list_blobs(backend, root, rel_posix)
    keep_n = max(1, int(keep))
    if exclude is not None:
        doomed = [n for n in names if n != exclude][keep_n - 1 :]
    else:
        doomed = names[keep_n:]
    for name in doomed:
        delete_blob(backend, root, rel_posix, name)
    return doomed


def pointer_rel_for(store_rel: Path) -> Path:
    """Git pointer path for an artifact rel (sibling with suffix)."""
    return store_rel.parent / (store_rel.name + POINTER_SUFFIX)


def write_pointer(store: Path, pointer_rel: Path, payload: dict) -> None:
    """Write a git pointer file (JSON). Creates parent dirs."""
    import json as _json

    dest = store / pointer_rel
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(_json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except OSError as e:
        raise RemoteError(f"cannot write pointer {dest}: {e}")


def read_pointer(store: Path, pointer_rel: Path) -> dict | None:
    """Read a git pointer file, or None when absent/corrupt."""
    import json as _json

    try:
        return _json.loads((store / pointer_rel).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def pointer_payload(
    backend: str, root: str, rel_posix: str, blob: str, content_hash: str
) -> dict:
    """Pointer dict committed to git for one remote revision."""
    return {
        "version": 1,
        "backend": backend,
        "root": root,
        "rel": rel_posix,
        "blob": blob,
        "hash": content_hash,
    }


def cache_path(cache_base: Path, backend: str, root: str, rel_posix: str) -> Path:
    """Deterministic materialization path for the latest blob."""
    digest = hashlib.sha1(f"{backend}\0{root}\0{rel_posix}".encode()).hexdigest()[:16]
    safe = Path(rel_posix).name or "artifact"
    return cache_base / f"{digest}-{safe}"


def materialize_latest(
    backend: str,
    root: str,
    rel_posix: str,
    cache_base: Path,
    blob: str | None = None,
) -> Path:
    """Fetch the newest blob (or ``blob``) into the cache. Returns the path.

    Raises RemoteError when the remote has no revisions or is unreachable.
    The cache persists (no cleanup needed); it is overwritten per fetch.
    """
    names = list_blobs(backend, root, rel_posix)
    if not names:
        raise RemoteError(
            f"no revisions on remote {root}/{rel_posix} (run commit first)"
        )
    want = blob or names[0]
    if want not in names:
        raise RemoteError(f"remote blob not found: {want} under {root}/{rel_posix}")
    data = fetch_blob(backend, root, rel_posix, want)
    dest = cache_path(cache_base, backend, root, rel_posix)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    except OSError as e:
        raise RemoteError(f"cannot write remote cache {dest}: {e}")
    return dest
