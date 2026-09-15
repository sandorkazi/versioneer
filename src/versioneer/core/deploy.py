"""Smart deploy: planner + rollout engine + deploy-status file (Phase 5).

Safety invariants (from implementation_plan.md):
- backup-before-overwrite (.bak next to dest + timestamped copy under state dir)
- atomic write (tmp + rename)
- permission restore (owner/group/mode via permissions.apply)
- sudo re-exec: CLI attempts the write; on EACCES it retries via
  `sudo` only when available, otherwise records per-target error
  suggesting elevation. Never aborts the whole run.
- per-target ok|skipped|error, never whole-run abort.
- machines allowlist, template substitution, symlink preserve/follow,
  dir skip|overwrite|merge (+ --prune), manifest replay, on_deploy hooks,
  plan-file hash guard, flexi --to handling.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

from versioneer.core import hooks as _hooks
from versioneer.core import monitor as _mon
from versioneer.core import permissions as _perm
from versioneer.core import template as _tmpl


def state_dir() -> Path:
    override = os.environ.get("VERSIONEER_STATE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "versioneer"


def _now_tag() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y%m%d-%H%M%S")


def current_host() -> str:
    return socket.gethostname()


def live_dest(target, meta_root: str = "", to_override: str = "",
              multi: bool = False) -> Path | None:
    """Resolve deploy destination for a target.

    Returns None when a flexi target has no destination (caller records error).
    """
    flex = getattr(target, "flex", "fixed")
    if flex == "flexi":
        if to_override:
            p = _mon.expand_path(to_override)
            if multi:
                # --to is a directory when several flexi targets deploy at once
                try:
                    if p.is_dir():
                        return p / Path(target.path).name
                except OSError:
                    pass
                return p / Path(target.path).name if not p.suffix else p
            return p
        dp = getattr(target, "deploy_path", "") or ""
        if dp:
            return _mon.expand_path(dp)
        return None
    if to_override:
        p = _mon.expand_path(to_override)
        if multi:
            rel = _mon.store_rel_for(target.path,
                                     _mon.live_abs_path(target.path, target.abs_path,
                                                        meta_root),
                                     flex, meta_root)
            try:
                if p.is_dir() or not p.suffix:
                    return p / rel
            except OSError:
                return p / rel
            return p / rel
        # single-target preview: --to is the exact destination file/dir
        return p
    if meta_root:
        return _mon.expand_path(meta_root).resolve(strict=False) / target.path
    if flex == "user":
        abs_p = _mon.live_abs_path(target.path, target.abs_path, "")
        rel = _mon.store_rel_for(target.path, abs_p, flex, "")
        return Path.home() / rel
    return _mon.live_abs_path(target.path, target.abs_path, "")


def _store_src(store: Path, target, meta_root: str = "") -> Path:
    abs_p = _mon.live_abs_path(target.path, target.abs_path, meta_root)
    rel = _mon.store_rel_for(target.path, abs_p, getattr(target, "flex", "fixed"),
                             meta_root)
    return store / rel


def plan_entry(target, meta_root: str, store: Path, dest: Path | None) -> dict:
    """Build {target, src_hash, dst_hash, action} for plan-out."""
    rel = _mon.store_rel_for(
        target.path, _mon.live_abs_path(target.path, target.abs_path, meta_root),
        getattr(target, "flex", "fixed"), meta_root)
    src = store / rel
    kind = getattr(target, "kind", "text")
    sym = getattr(target, "symlink", "preserve")
    ign = list(getattr(target, "ignore", []) or [])
    if not _src_exists(src, kind, sym):
        return {"target": target.path, "src_hash": "missing",
                "dst_hash": _dst_hash(dest, kind, sym, ign),
                "action": "error: no committed artifact"}
    src_hash = _mon.hash_target(src, kind, sym, ign)
    dst_hash = _dst_hash(dest, kind, sym, ign)
    if dest is None:
        action = "error: flexi target needs --to"
    elif dst_hash == "missing":
        action = "create"
    elif src_hash == dst_hash:
        action = "skip: already in sync"
    else:
        action = "overwrite" if kind in ("text", "binary") else "merge"
    return {"target": target.path, "src_hash": src_hash,
            "dst_hash": dst_hash, "action": action}


def _src_exists(src: Path, kind: str, sym: str) -> bool:
    try:
        if src.is_symlink() and sym == "preserve":
            return True
        return src.exists()
    except OSError:
        return False


def _dst_hash(dest: Path | None, kind: str, sym: str, ignore: list[str]) -> str:
    if dest is None:
        return "no-destination"
    try:
        exists = dest.exists() or dest.is_symlink()
    except OSError:
        return "read-error"
    if not exists:
        return "missing"
    try:
        return _mon.hash_target(dest, kind, sym, ignore)
    except OSError:
        return "read-error"


def _backup(dest: Path, rel: Path, backup_root: Path) -> list[str]:
    """Backup-before-overwrite. Returns list of backup paths created."""
    made: list[str] = []
    try:
        if not dest.exists() and not dest.is_symlink():
            return made
    except OSError:
        return made
    # .bak next to destination
    try:
        bak = dest.parent / (dest.name + ".bak")
        if dest.is_symlink() and not dest.exists():
            pass
        elif dest.is_symlink():
            bak.unlink(missing_ok=True)
            bak.symlink_to(os.readlink(dest))
            made.append(str(bak))
        elif dest.is_dir():
            pass  # dirs: only timestamped copy, no .bak (avoid huge dups)
        elif dest.is_file():
            shutil.copy2(dest, bak)
            made.append(str(bak))
    except OSError:
        pass
    # timestamped copy under state dir
    try:
        stamp = backup_root / rel
        stamp.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink():
            link_target = os.readlink(dest)
            stale = stamp.is_symlink() or stamp.exists()
            if stale:
                if stamp.is_dir() and not stamp.is_symlink():
                    shutil.rmtree(stamp)
                else:
                    stamp.unlink()
            stamp.symlink_to(link_target)
            made.append(str(stamp))
        elif dest.is_file():
            shutil.copy2(dest, stamp)
            made.append(str(stamp))
        elif dest.is_dir():
            if stamp.is_symlink() or stamp.is_file():
                stamp.unlink()
            if stamp.is_dir():
                shutil.rmtree(stamp)
            shutil.copytree(dest, stamp, symlinks=True)
            made.append(str(stamp))
    except OSError:
        pass
    return made


def _write_file_atomic(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".versioneer-", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _copy_tree_merge(src: Path, dest: Path, prune: bool = False) -> None:
    """rsync-like merge: copy new/changed files, never delete unless prune."""
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        base = Path(dirpath)
        try:
            rel = base.relative_to(src)
        except ValueError:
            continue
        target_dir = dest / rel if str(rel) != "." else dest
        target_dir.mkdir(parents=True, exist_ok=True)
        for fn in filenames:
            s = base / fn
            d = target_dir / fn
            try:
                if s.is_symlink():
                    if d.is_symlink() or d.exists():
                        if d.is_dir() and not d.is_symlink():
                            shutil.rmtree(d)
                        else:
                            d.unlink()
                    d.symlink_to(os.readlink(s))
                    continue
                if d.is_symlink():
                    d.unlink()
                if d.is_file():
                    try:
                        if s.stat().st_size == d.stat().st_size and \
                                _mon.sha256_file(s) == _mon.sha256_file(d):
                            continue
                    except OSError:
                        pass
                shutil.copy2(s, d)
            except OSError:
                continue
    if prune:
        # delete extras in dest not present in src (files only; keep dirs)
        src_files: set[str] = set()
        for dirpath, _dn, filenames in os.walk(src, followlinks=False):
            for fn in filenames:
                try:
                    rel = (Path(dirpath) / fn).relative_to(src).as_posix()
                    src_files.add(rel)
                except ValueError:
                    continue
        for dirpath, _dn, filenames in os.walk(dest, followlinks=False):
            for fn in filenames:
                p = Path(dirpath) / fn
                try:
                    rel = p.relative_to(dest).as_posix()
                except ValueError:
                    continue
                if rel not in src_files:
                    try:
                        p.unlink()
                    except OSError:
                        pass


def _sudo_copy(src: Path, dest: Path) -> bool:
    """Best-effort sudo copy for unwritable destinations. Returns success."""
    sudo = shutil.which("sudo")
    if not sudo:
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [sudo, "cp", "-a", str(src), str(dest)],
            capture_output=True, text=True, timeout=60, check=False)
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def deploy_one(target, config, store: Path, to_override: str = "",
               multi: bool = False, yes: bool = False,
               prune: bool = False, apply_manifest: bool = False,
               force_host: bool = False, host: str = "",
               dry_run: bool = False,
               plan_map: dict | None = None) -> dict:
    """Deploy a single target. Never raises for per-target failures."""
    from versioneer.core import monitor as _m

    host = host or current_host()
    machines = list(getattr(target, "machines", []) or [])
    if machines and host not in machines and not force_host:
        return {"target": target.path, "status": "skipped",
                "reason": "skipped (wrong host)", "action": "skip",
                "src_hash": "", "dst_hash": ""}
    meta_root = config.meta.root or ""
    kind = getattr(target, "kind", "text")
    sym = getattr(target, "symlink", "preserve")
    ign = list(getattr(target, "ignore", []) or [])
    rel = _m.store_rel_for(
        target.path, _m.live_abs_path(target.path, target.abs_path, meta_root),
        getattr(target, "flex", "fixed"), meta_root)
    src = store / rel
    dest = live_dest(target, meta_root, to_override, multi)

    if dest is None:
        return {"target": target.path, "status": "error",
                "reason": "flexi target needs --to <path> (or set deploy_path)",
                "action": "error", "src_hash": "", "dst_hash": ""}

    # manifest: replay only
    if kind == "manifest":
        try:
            exists = src.exists()
        except OSError:
            exists = False
        if not exists:
            return {"target": target.path, "status": "error",
                    "reason": "no committed manifest artifact",
                    "action": "error", "src_hash": "missing", "dst_hash": ""}
        try:
            preview = src.read_text(encoding="utf-8", errors="replace")[:2000]
        except OSError as e:
            return {"target": target.path, "status": "error",
                    "reason": f"cannot read manifest artifact: {e}",
                    "action": "error", "src_hash": "", "dst_hash": ""}
        hint = (f"replay printed ({len(preview)} chars)"
                + (" --apply: no-op, replay only" if not apply_manifest
                   else " --apply: printed (no destructive apply in v1)"))
        if dry_run:
            return {"target": target.path, "status": "skipped",
                    "reason": f"dry-run: would replay manifest: {hint}",
                    "action": "replay", "src_hash": "", "dst_hash": "",
                    "replay": preview}
        return {"target": target.path, "status": "ok", "reason": hint,
                "action": "replay", "src_hash": "", "dst_hash": "",
                "replay": preview}

    if not _src_exists(src, kind, sym):
        # destination missing is a per-target error only when the *source*
        # artifact exists but dest parent is absent? Spec: "if the destination
        # location does not exist: don't deploy" -> error. Here src missing
        # is also an error (nothing to deploy).
        return {"target": target.path, "status": "error",
                "reason": "no committed artifact in store (run commit first)",
                "action": "error", "src_hash": "missing",
                "dst_hash": _dst_hash(dest, kind, sym, ign)}

    src_hash = _mon.hash_target(src, kind, sym, ign)
    dst_hash = _dst_hash(dest, kind, sym, ign)

    # plan hash-guard
    if plan_map is not None and target.path in plan_map:
        entry = plan_map[target.path]
        for key in ("src_hash", "dst_hash"):
            if key in entry and entry[key] not in ("", None) and \
                    entry[key] != (src_hash if key == "src_hash" else dst_hash):
                return {"target": target.path, "status": "error",
                        "reason": (f"interim edit detected: {key} changed since "
                                   f"plan-out (re-run --plan-out)"),
                        "action": "error", "src_hash": src_hash,
                        "dst_hash": dst_hash}

    if dst_hash not in ("missing", "no-destination") and src_hash == dst_hash:
        return {"target": target.path, "status": "ok",
                "reason": "already in sync", "action": "skip",
                "src_hash": src_hash, "dst_hash": dst_hash}

    # destination parent: create when missing (new-machine flow); only error
    # when the parent cannot be created (permission, missing mount, ...).
    try:
        parent_ok = dest.parent.exists()
    except OSError:
        parent_ok = False
    if not parent_ok:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            parent_ok = True
        except OSError as e:
            return {"target": target.path, "status": "error",
                    "reason": f"destination parent missing: {dest.parent} ({e})",
                    "action": "error", "src_hash": src_hash, "dst_hash": dst_hash}

    action = "create" if dst_hash == "missing" else "overwrite"
    if dry_run:
        return {"target": target.path, "status": "skipped",
                "reason": f"dry-run: would {action} {dest}",
                "action": action, "src_hash": src_hash, "dst_hash": dst_hash}

    # dir handling
    if kind == "dir":
        try:
            dest_exists = dest.exists() or dest.is_symlink()
        except OSError:
            dest_exists = False
        if dest_exists and not yes:
            return {"target": target.path, "status": "skipped",
                    "reason": "dir exists (skip default; use --yes to merge)",
                    "action": "skip", "src_hash": src_hash, "dst_hash": dst_hash}
        stamp_root = state_dir() / "backups" / _now_tag()
        backups = _backup(dest, rel, stamp_root) if dest_exists else []
        try:
            if dest_exists:
                _copy_tree_merge(src, dest, prune=prune)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src, dest, symlinks=True)
        except OSError as e:
            # sudo retry for unwritable destinations
            if _sudo_copy(src, dest):
                pass
            else:
                return {"target": target.path, "status": "error",
                        "reason": f"dir deploy failed: {e}", "action": "error",
                        "src_hash": src_hash, "dst_hash": dst_hash,
                        "backups": backups}
        perms_err = _perm.apply(dest, getattr(target, "owner", ""),
                                getattr(target, "group", ""),
                                getattr(target, "mode", ""))
        hook_out = ""
        if getattr(target, "on_deploy", ""):
            ok_h, hook_out = _hooks.run_hook(getattr(target, "on_deploy", ""))
            if not ok_h:
                return {"target": target.path, "status": "error",
                        "reason": f"on_deploy hook failed: {hook_out}",
                        "action": "merge", "src_hash": src_hash,
                        "dst_hash": dst_hash, "backups": backups,
                        "hook_output": hook_out}
        if perms_err:
            return {"target": target.path, "status": "error",
                    "reason": "; ".join(perms_err), "action": "merge",
                    "src_hash": src_hash, "dst_hash": dst_hash,
                    "backups": backups, "hook_output": hook_out}
        return {"target": target.path, "status": "ok",
                "reason": f"merged {'(pruned)' if prune else ''}".strip(),
                "action": "merge", "src_hash": src_hash, "dst_hash": dst_hash,
                "backups": backups, "hook_output": hook_out}

    # symlink preserve: recreate link (store artifact is the link itself)
    if src.is_symlink() and sym == "preserve":
        try:
            link_target = os.readlink(src)
        except OSError as e:
            return {"target": target.path, "status": "error",
                    "reason": f"cannot read store link: {e}",
                    "action": "error", "src_hash": src_hash,
                    "dst_hash": dst_hash}
        stamp_root = state_dir() / "backups" / _now_tag()
        backups = _backup(dest, rel, stamp_root)
        # file prompt semantics: default N. Without --yes, an existing
        # differing link is skipped (spec: exists+differs prompts,
        # default N).
        if dst_hash != "missing" and src_hash != dst_hash and not yes:
            return {"target": target.path, "status": "skipped",
                    "reason": "link differs (skip default; use --yes to overwrite)",
                    "action": "skip", "src_hash": src_hash,
                    "dst_hash": dst_hash, "backups": backups}
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.is_symlink() or dest.exists():
                if dest.is_dir() and not dest.is_symlink():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            dest.symlink_to(link_target)
        except OSError as e:
            return {"target": target.path, "status": "error",
                    "reason": f"link deploy failed: {e} "
                              "(re-run with sudo if needed)",
                    "action": "error", "src_hash": src_hash,
                    "dst_hash": dst_hash, "backups": backups}
        hook_out = ""
        if getattr(target, "on_deploy", ""):
            ok_h, hook_out = _hooks.run_hook(getattr(target, "on_deploy", ""))
            if not ok_h:
                return {"target": target.path, "status": "error",
                        "reason": f"on_deploy hook failed: {hook_out}",
                        "action": "overwrite", "src_hash": src_hash,
                        "dst_hash": dst_hash, "backups": backups,
                        "hook_output": hook_out}
        return {"target": target.path, "status": "ok",
                "reason": f"link -> {link_target}", "action": "overwrite",
                "src_hash": src_hash, "dst_hash": dst_hash,
                "backups": backups, "hook_output": hook_out}
    # fall through for follow-mode links (content copy below)

    # text/binary (or follow-mode symlink content)
    if dst_hash != "missing" and src_hash != dst_hash and not yes:
        # interactive prompt would ask; non-interactive default N -> skip
        # unless --yes. But an explicit single-target deploy without --yes
        # in scripts often expects the write; spec says default N, so skip.
        return {"target": target.path, "status": "skipped",
                "reason": "exists and differs (skip default; use --yes to overwrite)",
                "action": "skip", "src_hash": src_hash, "dst_hash": dst_hash}
    stamp_root = state_dir() / "backups" / _now_tag()
    backups = _backup(dest, rel, stamp_root) if dst_hash != "missing" else []
    try:
        if src.is_symlink() and sym == "follow":
            data = (src.resolve(strict=False).read_bytes()
                    if src.resolve(strict=False).is_file() else b"")
        elif src.is_file():
            data = src.read_bytes()
        elif src.is_dir():
            return {"target": target.path, "status": "error",
                    "reason": "store artifact is a dir but target kind is "
                              f"{kind} (re-add with --kind dir)",
                    "action": "error", "src_hash": src_hash,
                    "dst_hash": dst_hash, "backups": backups}
        else:
            return {"target": target.path, "status": "error",
                    "reason": "store artifact unreadable",
                    "action": "error", "src_hash": src_hash,
                    "dst_hash": dst_hash, "backups": backups}
    except OSError as e:
        return {"target": target.path, "status": "error",
                "reason": f"cannot read store artifact: {e}",
                "action": "error", "src_hash": src_hash,
                "dst_hash": dst_hash, "backups": backups}
    if getattr(target, "template", False):
        data = _tmpl.render_bytes(data)
    try:
        _write_file_atomic(dest, data)
    except OSError as e:
        import errno as _errno

        if e.errno in (_errno.EACCES, _errno.EPERM):
            # best-effort sudo re-exec for unwritable destinations
            try:
                with tempfile.NamedTemporaryFile(delete=False) as tmp:
                    tmp.write(data)
                    tmp_path = Path(tmp.name)
            except OSError:
                return {"target": target.path, "status": "error",
                        "reason": f"cannot write {dest}: {e} "
                                  "(re-run with sudo if needed)",
                        "action": "error", "src_hash": src_hash,
                        "dst_hash": dst_hash, "backups": backups}
            try:
                if _sudo_copy(tmp_path, dest):
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                else:
                    return {"target": target.path, "status": "error",
                            "reason": f"cannot write {dest}: {e} "
                                      "(re-run with sudo if needed)",
                            "action": "error", "src_hash": src_hash,
                            "dst_hash": dst_hash, "backups": backups}
            finally:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
        else:
            return {"target": target.path, "status": "error",
                    "reason": f"cannot write {dest}: {e}",
                    "action": "error", "src_hash": src_hash,
                    "dst_hash": dst_hash, "backups": backups}
    perms_err = _perm.apply(dest, getattr(target, "owner", ""),
                            getattr(target, "group", ""),
                            getattr(target, "mode", ""))
    hook_out = ""
    if getattr(target, "on_deploy", ""):
        ok_h, hook_out = _hooks.run_hook(getattr(target, "on_deploy", ""))
        if not ok_h:
            details = "; ".join(perms_err) if perms_err else ""
            reason = f"on_deploy hook failed: {hook_out}"
            if details:
                reason += f" ({details})"
            return {"target": target.path, "status": "error",
                    "reason": reason, "action": action,
                    "src_hash": src_hash, "dst_hash": dst_hash,
                    "backups": backups, "hook_output": hook_out}
    if perms_err:
        return {"target": target.path, "status": "error",
                "reason": "; ".join(perms_err), "action": action,
                "src_hash": src_hash, "dst_hash": dst_hash,
                "backups": backups, "hook_output": hook_out}
    return {"target": target.path, "status": "ok",
            "reason": f"{action}d {dest}", "action": action,
            "src_hash": src_hash, "dst_hash": dst_hash,
            "backups": backups, "hook_output": hook_out}


def write_status_file(config_name: str, results: list[dict]) -> Path:
    """Write <config>-deploy-<timestamp>.json + update <config>-latest.json."""
    sdir = state_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    tag = _now_tag()
    path = sdir / f"{config_name}-deploy-{tag}.json"
    payload = {"config": config_name, "timestamp": tag, "results": results}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    latest = sdir / f"{config_name}-latest.json"
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
    except OSError:
        pass
    try:
        latest.symlink_to(path.name)
    except OSError:
        try:
            shutil.copy2(path, latest)
        except OSError:
            pass
    return path


def load_plan(plan_path: str) -> dict:
    """Load plan.json into {target_path: entry}."""
    data = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    entries = data if isinstance(data, list) else data.get("entries", data)
    if isinstance(entries, dict) and "targets" in entries:
        entries = entries["targets"]
    out: dict = {}
    if isinstance(entries, list):
        for e in entries:
            if isinstance(e, dict) and "target" in e:
                out[e["target"]] = e
    elif isinstance(entries, dict):
        out = dict(entries)
    return out
