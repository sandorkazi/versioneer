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
        return Path(os.path.expandvars(override)).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(os.path.expandvars(xdg)).expanduser() / "versioneer"
    from versioneer.core import elevate as _elev

    return _elev.effective_home() / ".local" / "state" / "versioneer"


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
        from versioneer.core import elevate as _elev

        return _elev.effective_home() / rel
    return _mon.live_abs_path(target.path, target.abs_path, "")


def _store_src(store: Path, target, meta_root: str = "") -> Path:
    abs_p = _mon.live_abs_path(target.path, target.abs_path, meta_root)
    rel = _mon.store_rel_for(target.path, abs_p, getattr(target, "flex", "fixed"),
                             meta_root)
    return store / rel


def plan_entry(target, meta_root: str, store: Path, dest: Path | None) -> dict:
    """Build {target, src_hash, dst_hash, action} for plan-out."""
    kind = getattr(target, "kind", "text")
    if kind == "manifest":
        # artifacts live at store/<fname>; see _deploy_manifest.
        cand = store / str(getattr(target, "path", "") or "")
        if cand.exists():
            src = cand
        else:
            cand2 = store / Path(str(getattr(target, "path", "") or "")).name
            src = cand2 if cand2.exists() else cand
    else:
        rel = _mon.store_rel_for(
            target.path, _mon.live_abs_path(target.path, target.abs_path, meta_root),
            getattr(target, "flex", "fixed"), meta_root)
        src = store / rel
        if getattr(target, "remote", None):
            # Remote target: fetch the latest blob into the cache and deploy
            # from there (plan hash-guard semantics unchanged: a rotation
            # between plan-out and deploy surfaces as an interim edit).
            from versioneer.core import remote as _rem_p

            rem = target.remote
            try:
                src = _rem_p.materialize_latest(
                    rem.get("backend", "file"), rem.get("root", ""),
                    rel.as_posix(), state_dir() / "remote-cache",
                )
            except _rem_p.RemoteError as e:
                return {"target": target.path, "src_hash": "missing",
                        "dst_hash": "", "action": f"error: remote unreachable ({e})"}
    sym = getattr(target, "symlink", "preserve")
    ign = list(getattr(target, "ignore", []) or [])
    if not _src_exists(src, kind, sym):
        return {"target": target.path, "src_hash": "missing",
                "dst_hash": _dst_hash(dest, kind, sym, ign),
                "action": "error: no committed artifact"}
    src_hash = _store_hash(src, kind, sym, ign)
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


def _store_hash(src: Path, kind: str, sym: str, ignore: list[str]) -> str:
    """Hash a store artifact, decrypting sops envelopes first (warn-only fallback)."""
    try:
        from versioneer.core import secrets as _sec

        return _sec.hash_store_artifact(src, kind, sym, ignore)
    except (ImportError, OSError):
        pass
    try:
        return _mon.hash_target(src, kind, sym, ignore)
    except OSError:
        return "read-error"


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


def _copytree_ignore_fn(top: Path, ignore: list[str] | None):
    """shutil.copytree ignore-callable honoring target ignore patterns."""
    ig = list(ignore or [])

    def _fn(dirpath: str, names: list[str]) -> list[str]:
        out: list[str] = []
        base = Path(dirpath)
        for n in names:
            try:
                rel = (base / n).relative_to(top).as_posix()
            except ValueError:
                continue
            try:
                is_dir = (base / n).is_dir() and not (base / n).is_symlink()
            except OSError:
                is_dir = False
            cand = rel + "/" if is_dir else rel
            if _mon.matches_ignore(cand, ig) or _mon.matches_ignore(rel, ig):
                out.append(n)
        return out

    return _fn


def _ignored_rel(rel_posix: str, is_dir: bool, ignore: list[str]) -> bool:
    if not ignore or not rel_posix or rel_posix == ".":
        return False
    if is_dir:
        return bool(_mon.matches_ignore(rel_posix + "/", ignore)
                    or _mon.matches_ignore(rel_posix, ignore))
    return bool(_mon.matches_ignore(rel_posix, ignore))


def _copy_tree_merge(src: Path, dest: Path, prune: bool = False,
                     ignore: list[str] | None = None) -> None:
    """rsync-like merge: copy new/changed files, never delete unless prune.

    Respects target ignore patterns (skips ignored src entries; prune never
    deletes ignored dest extras). Best-effort per file: one bad entry never
    aborts the merge.
    """
    ig = list(ignore or [])
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        base = Path(dirpath)
        try:
            rel = base.relative_to(src)
        except ValueError:
            continue
        rel_posix = "" if str(rel) == "." else rel.as_posix()
        # replicate symlink-to-dir entries as links; don't descend into them.
        kept_dirs: list[str] = []
        for d in list(dirnames):
            d_rel = f"{rel_posix}/{d}" if rel_posix else d
            if _ignored_rel(d_rel, True, ig):
                continue
            full = base / d
            try:
                if full.is_symlink():
                    link_dest = (dest / rel / d) if str(rel) != "." else (dest / d)
                    try:
                        link_dest.parent.mkdir(parents=True, exist_ok=True)
                        if link_dest.is_symlink() or link_dest.exists():
                            if link_dest.is_dir() and not link_dest.is_symlink():
                                shutil.rmtree(link_dest)
                            else:
                                link_dest.unlink()
                        link_dest.symlink_to(os.readlink(full))
                    except OSError:
                        pass
                    continue  # don't descend into linked dirs
            except OSError:
                continue
            kept_dirs.append(d)
        dirnames[:] = sorted(kept_dirs)
        try:
            target_dir = dest / rel if str(rel) != "." else dest
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        for fn in filenames:
            f_rel = f"{rel_posix}/{fn}" if rel_posix else fn
            if _ignored_rel(f_rel, False, ig):
                continue
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
        # delete extras in dest not present in src (files only; keep dirs).
        # Ignored dest extras are always kept, even with --prune.
        src_files: set[str] = set()
        for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
            base = Path(dirpath)
            try:
                rel_dir = base.relative_to(src).as_posix()
            except ValueError:
                continue
            prefix = "" if rel_dir == "." else rel_dir + "/"
            dirnames[:] = sorted(
                d for d in dirnames
                if not _ignored_rel(f"{prefix}{d}", True, ig))
            for fn in filenames:
                f_rel = f"{prefix}{fn}"
                if _ignored_rel(f_rel, False, ig):
                    continue
                try:
                    src_files.add((base / fn).relative_to(src).as_posix())
                except ValueError:
                    continue
        for dirpath, _dn, filenames in os.walk(dest, followlinks=False):
            for fn in filenames:
                p = Path(dirpath) / fn
                try:
                    rel = p.relative_to(dest).as_posix()
                except ValueError:
                    continue
                if rel not in src_files and not _ignored_rel(rel, False, ig):
                    try:
                        if not p.is_symlink() and p.is_dir():
                            continue
                        p.unlink()
                    except OSError:
                        pass


def _template_tree(dest: Path, ignore: list[str] | None = None) -> None:
    """Render {{HOME}}/{{HOST}} in deployed dir text files (best-effort).

    Binary files pass through unchanged (render_bytes returns them as-is).
    Symlinks and ignored paths are left untouched. Never raises.
    """
    ig = list(ignore or [])
    try:
        walker = os.walk(dest, followlinks=False)
    except OSError:
        return
    for dirpath, dirnames, filenames in walker:
        base = Path(dirpath)
        try:
            rel_dir = base.relative_to(dest).as_posix()
        except ValueError:
            continue
        prefix = "" if rel_dir == "." else rel_dir + "/"
        kept: list[str] = []
        for d in dirnames:
            if _ignored_rel(f"{prefix}{d}", True, ig):
                continue
            try:
                if (base / d).is_symlink():
                    continue
            except OSError:
                continue
            kept.append(d)
        dirnames[:] = kept
        for fn in filenames:
            f_rel = f"{prefix}{fn}"
            if _ignored_rel(f_rel, False, ig):
                continue
            p = base / fn
            try:
                if p.is_symlink() or not p.is_file():
                    continue
                data = p.read_bytes()
            except OSError:
                continue
            rendered = _tmpl.render_bytes(data)
            if rendered != data:
                try:
                    _write_file_atomic(p, rendered)
                except OSError:
                    continue


def _apply_tree_perms(dest: Path, owner: str = "", group: str = "",
                      mode: str = "") -> list[str]:
    """Recursive owner/mode restore for dir deploys (best-effort).

    Owner/group apply to dirs and files; mode applies to files only (dirs
    keep their execute/search bits). Symlinks are skipped. Never raises;
    returns collected error strings (possibly empty).
    """
    if not owner and not group and not mode:
        return []
    errors: list[str] = list(_perm.apply(dest, owner, group, ""))
    try:
        walker = os.walk(dest, followlinks=False)
    except OSError as e:
        errors.append(f"perms walk failed: {e}")
        return errors
    for dirpath, dirnames, filenames in walker:
        base = Path(dirpath)
        for d in dirnames:
            p = base / d
            try:
                if p.is_symlink():
                    continue
            except OSError:
                continue
            # _perm.apply never raises (returns error strings).
            errors.extend(_perm.apply(p, owner, group, ""))
        for fn in filenames:
            p = base / fn
            try:
                if p.is_symlink():
                    continue
            except OSError:
                continue
            errors.extend(_perm.apply(p, owner, group, mode))
        if len(errors) > 50:
            errors = errors[:50] + [f"... ({len(errors) - 50} more perms errors)"]
            break
    return errors


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


def _run_apply_cmd(cmd: list[str], timeout: int = 300) -> tuple[bool, str]:
    """Run a manifest --apply command. Returns (ok, output-snippet)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except FileNotFoundError as e:
        return False, f"{cmd[0]} not found: {e}"
    except OSError as e:
        return False, f"cannot exec {' '.join(cmd[:3])}: {e}"
    except subprocess.TimeoutExpired:
        return False, f"timed out: {' '.join(cmd[:3])}..."
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return False, f"{' '.join(cmd[:4])}... failed (rc={proc.returncode}): {err[:500]}"
    out = (proc.stdout or proc.stderr or "").strip()
    return True, out[-500:] if len(out) > 500 else out


def _deploy_manifest(target, config, store: Path, src: Path,
                     dry_run: bool = False,
                     apply_manifest: bool = False) -> dict:
    """Manifest rollout: print-only default, safe --apply on opt-in.

    Never raises; per-target ok|skipped|error. dry-run performs no
    subprocess calls and writes no files, even with apply_manifest=True.
    """
    from versioneer.core import manifest as _mg

    # Manifest artifacts live at store/<fname> (e.g. store/packages.list);
    # the generic fixed-target rel would nest the absolute abs_path, so
    # resolve via manifest-friendly candidates first.
    for cand in (src,
                 store / str(getattr(target, "path", "") or ""),
                 store / Path(getattr(target, "path", "") or "").name):
        try:
            if cand and cand.exists():
                src = cand
                break
        except OSError:
            continue
    else:
        # last resort: abs_path already points inside the store (as written
        # by `manifest`), use it directly when it exists.
        try:
            abs_cand = Path(str(getattr(target, "abs_path", "") or ""))
            if abs_cand.is_file():
                src = abs_cand
        except (OSError, ValueError):
            pass
    try:
        exists = src.exists()
    except OSError:
        exists = False
    if not exists:
        return {"target": target.path, "status": "error",
                "reason": "no committed manifest artifact",
                "action": "error", "src_hash": "missing", "dst_hash": ""}
    try:
        full = src.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"target": target.path, "status": "error",
                "reason": f"cannot read manifest artifact: {e}",
                "action": "error", "src_hash": "", "dst_hash": ""}
    preview = full[:2000]
    mtype = _mg.manifest_kind_for_target(target)

    def _skipped(would: str) -> dict:
        return {"target": target.path, "status": "skipped",
                "reason": f"dry-run: would {would}",
                "action": "replay", "src_hash": "", "dst_hash": "",
                "replay": preview}

    def _ok(reason: str, extra: dict | None = None) -> dict:
        d: dict = {"target": target.path, "status": "ok",
                   "reason": reason, "action": "replay",
                   "src_hash": "", "dst_hash": "", "replay": preview}
        if extra:
            d.update(extra)
        return d

    def _err(reason: str) -> dict:
        return {"target": target.path, "status": "error",
                "reason": reason, "action": "error",
                "src_hash": "", "dst_hash": "", "replay": preview}

    # ---- packages: pacman -Qqe replay + optional sudo apply ----
    if mtype == "packages":
        parsed = _mg.parse_packages_manifest(full)
        pkgs = parsed.get("pacman", [])
        replay_cmd = _mg.replay_packages_text(parsed)
        aur_n = len(parsed.get("aur", []))
        flat_n = len(parsed.get("flatpak", []))
        extras = ""
        if aur_n:
            extras += f" (+{aur_n} AUR, manual: yay -S)"
        if flat_n:
            extras += f" (+{flat_n} flatpak, manual: flatpak install)"
        if dry_run:
            if apply_manifest and pkgs:
                return _skipped(f"run {replay_cmd}{extras}")
            return _skipped(f"replay manifest: {replay_cmd}{extras}")
        if not apply_manifest:
            return _ok(f"replay printed: {replay_cmd}{extras} "
                       "(use --apply to run sudo pacman -S --needed)")
        if not pkgs:
            return _ok("nothing to install (no pacman packages recorded)")
        cmd = _mg.packages_apply_command(pkgs)
        ok, out = _run_apply_cmd(cmd)
        if not ok:
            return _err(f"pacman apply failed: {out}")
        detail = f"applied: sudo pacman -S --needed {len(pkgs)} pkgs{extras}"
        if out:
            detail += f" [{out[:200]}]"
        return _ok(detail, {"applied": cmd})

    # ---- systemd: systemctl enable replay + optional apply ----
    if mtype == "systemd":
        parsed = _mg.parse_systemd_manifest(full)
        user_units = parsed.get("user", [])
        sys_units = parsed.get("system", [])
        total = len(user_units) + len(sys_units)
        replay = ("replay: "
                  + (f"systemctl --user enable {' '.join(user_units[:10])}"
                     if user_units else "")
                  + ("; " if user_units and sys_units else "")
                  + (f"sudo systemctl enable {' '.join(sys_units[:10])}"
                     if sys_units else "")
                  ).strip() or "replay: no enabled units recorded"
        if dry_run:
            if apply_manifest and total:
                return _skipped(f"enable {total} unit(s): {replay}")
            return _skipped(f"replay manifest: {replay}")
        if not apply_manifest:
            return _ok(f"{replay} (use --apply to run systemctl enable)")
        if not total:
            return _ok("nothing to enable (no units recorded)")
        failures: list[str] = []
        applied: list[list[str]] = []
        if user_units:
            cmd = ["systemctl", "--user", "enable", *user_units]
            ok, out = _run_apply_cmd(cmd)
            if not ok:
                failures.append(f"user enable failed: {out}")
            else:
                applied.append(cmd)
        if sys_units:
            cmd = ["sudo", "systemctl", "enable", *sys_units]
            ok, out = _run_apply_cmd(cmd)
            if not ok:
                failures.append(f"system enable failed: {out}")
            else:
                applied.append(cmd)
        if failures:
            return _err("; ".join(failures))
        return _ok(f"applied: enabled {total} unit(s) "
                   f"({len(user_units)} user, {len(sys_units)} system)",
                   {"applied": applied})

    # ---- env: print-only always ----
    if mtype == "env":
        if dry_run:
            return _skipped("replay manifest: compare env.json, export as needed")
        return _ok("replay printed: compare env.json, export as needed "
                   "(env is print-only, --apply is a no-op)")

    # ---- wine: recipe preview + setup-wine.sh generation, never exec ----
    if mtype == "wine":
        replay = _mg.replay_wine_text(full)
        if dry_run:
            if apply_manifest:
                return _skipped(f"write setup-wine.sh ({replay}; "
                                "winetricks never auto-run)")
            return _skipped(f"replay manifest: {replay}")
        if not apply_manifest:
            return _ok(f"replay printed: {replay} "
                       "(use --apply to generate setup-wine.sh; "
                       "winetricks never auto-run)")
        script = _mg.wine_setup_script_text(full)
        try:
            sdir = state_dir()
            sdir.mkdir(parents=True, exist_ok=True)
            script_path = sdir / "setup-wine.sh"
            script_path.write_text(script, encoding="utf-8")
            try:
                import stat as _stat

                mode = script_path.stat().st_mode
                script_path.chmod(mode | _stat.S_IXUSR | _stat.S_IXGRP)
            except OSError:
                pass
        except OSError as e:
            return _err(f"cannot write setup-wine.sh: {e}")
        return _ok(f"wrote {script_path} ({replay}; "
                   "review + run manually, winetricks never auto-run)",
                   {"script": str(script_path),
                    "replay": preview + "\n---\n" + script[:2000]})

    # ---- unknown manifest subtype: safe print-only fallback ----
    if dry_run:
        return _skipped("replay manifest (unknown subtype, print-only)")
    return _ok("replay printed (unknown manifest subtype, print-only; "
               "--apply is a no-op)")


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

    # manifest: print-only by default, safe --apply on explicit opt-in.
    # packages -> `sudo pacman -S --needed ...`, systemd -> `systemctl enable`,
    # env -> print-only always, wine -> write setup-wine.sh (never auto-run
    # winetricks). dry-run never runs subprocesses nor writes files.
    if kind == "manifest":
        return _deploy_manifest(target, config, store, src,
                                dry_run=dry_run, apply_manifest=apply_manifest)

    if getattr(target, "remote", None):
        from versioneer.core import remote as _rem_o

        rem = target.remote
        try:
            src = _rem_o.materialize_latest(
                rem.get("backend", "file"), rem.get("root", ""), rel.as_posix(),
                state_dir() / "remote-cache")
        except _rem_o.RemoteError as e:
            return {"target": target.path, "status": "error",
                    "reason": f"remote unreachable: {e}",
                    "action": "error", "src_hash": "missing",
                    "dst_hash": _dst_hash(dest, kind, sym, ign)}

    if not _src_exists(src, kind, sym):
        # destination missing is a per-target error only when the *source*
        # artifact exists but dest parent is absent? Spec: "if the destination
        # location does not exist: don't deploy" -> error. Here src missing
        # is also an error (nothing to deploy).
        return {"target": target.path, "status": "error",
                "reason": "no committed artifact in store (run commit first)",
                "action": "error", "src_hash": "missing",
                "dst_hash": _dst_hash(dest, kind, sym, ign)}

    src_hash = _store_hash(src, kind, sym, ign)
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
                _copy_tree_merge(src, dest, prune=prune, ignore=ign)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src, dest, symlinks=True,
                                ignore=_copytree_ignore_fn(src, ign))
        except OSError as e:
            # sudo retry for unwritable destinations
            if _sudo_copy(src, dest):
                pass
            else:
                return {"target": target.path, "status": "error",
                        "reason": f"dir deploy failed: {e}", "action": "error",
                        "src_hash": src_hash, "dst_hash": dst_hash,
                        "backups": backups}
        # ITEM 2: decrypt dir destination when store holds sops envelopes.
        # Warn-only when sops/age absent — never blocks deploy.
        _sec_warns: list[str] = []
        try:
            from versioneer.core import secrets as _sec

            _sec_warns = _sec.decrypt_tree_in_place(dest)
        except (ImportError, OSError):
            _sec_warns = []
        # ITEM 9: render {{HOME}}/{{HOST}} in dir text files when
        # template=true (binary-safe, best-effort, never aborts).
        if getattr(target, "template", False):
            _template_tree(dest, ign)
        perms_err = _apply_tree_perms(dest, getattr(target, "owner", ""),
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
        _merge_reason = f"merged {'(pruned)' if prune else ''}".strip()
        if _sec_warns:
            _merge_reason = (_merge_reason + " [secrets: " + "; ".join(_sec_warns)[:500] + "]").strip()
        return {"target": target.path, "status": "ok",
                "reason": _merge_reason,
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
    secrets_warn: str | None = None
    try:
        if src.is_symlink() and sym == "follow":
            link_src = src.resolve(strict=False)
            if link_src.is_file():
                try:
                    from versioneer.core import secrets as _secf

                    if _secf.is_encrypted_file(link_src):
                        _data, secrets_warn = _secf.decrypt_bytes(link_src)
                        data = _data
                    else:
                        data = link_src.read_bytes()
                except (ImportError, OSError):
                    data = link_src.read_bytes()
            else:
                data = b""
        elif src.is_file():
            try:
                from versioneer.core import secrets as _secf

                if _secf.is_encrypted_file(src):
                    _data, secrets_warn = _secf.decrypt_bytes(src)
                    data = _data
                else:
                    data = src.read_bytes()
            except (ImportError, OSError):
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
    _ok_reason = f"{action}d {dest}"
    if secrets_warn:
        _ok_reason = f"{_ok_reason} [secrets: {secrets_warn[:500]}]"
    return {"target": target.path, "status": "ok",
            "reason": _ok_reason, "action": action,
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
