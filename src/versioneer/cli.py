"""Versioneer CLI entrypoint.

Implements: global -C/--config, config create/list/show/remove,
target add/list/remove, status/diff/log, commit/push/pull,
deploy, service, bootstrap, watch, manifest, doctor.
"""

from __future__ import annotations

import os
from pathlib import Path

import click
from rich.console import Console

from versioneer import __version__
from versioneer.core import config as cfg

console = Console()


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "-C", "--config", "config_name", default=None,
    help="Config name to operate on (e.g. -C hypr).",
)
@click.version_option(__version__, prog_name="versioneer")
@click.pass_context
def cli(ctx: click.Context, config_name: str | None) -> None:
    """Service-based file and config monitoring for Linux."""
    ctx.ensure_object(dict)
    ctx.obj["config_name"] = config_name


# ---------- config management (implemented) ----------

@cli.group("config")
def config_grp() -> None:
    """Create/list/show/remove configs."""


@config_grp.command("create")
@click.option("--name", required=True, help="Short name, e.g. hypr.")
@click.option("--path", "store_path", required=True, help="Store repo path.")
@click.option("--upstream", required=True, help="Upstream git URL.")
@click.option("--auto-commit", is_flag=True, default=False)
@click.option("--auto-push", is_flag=True, default=False)
@click.option("--check-interval", default="3h", show_default=True)
@click.option("--notify/--no-notify", default=True, show_default=True)
def config_create(name, store_path, upstream, auto_commit, auto_push, check_interval, notify):
    """Create a config: write TOML + git init store."""
    from versioneer.core import store as _store

    meta = cfg.Meta(
        name=name,
        upstream=upstream,
        storage=store_path,
        notify=notify,
        auto_commit=auto_commit,
        auto_push=auto_push,
        check_interval=check_interval,
    )
    errors = meta.validate()
    if errors:
        raise click.ClickException("; ".join(errors))
    if cfg.config_path(name).exists():
        raise click.ClickException(f"config {name!r} already exists: {cfg.config_path(name)}")

    store = Path(os.path.expandvars(store_path)).expanduser()
    try:
        _store.ensure_repo(store)
        _store.set_upstream(store, upstream)
        # LFS install is warn-only: plain git keeps working without it.
        lfs_warn = _store.ensure_lfs(store, ["*.bin"])
        # initial commit so push/pull/log have a HEAD (offline-safe, no push here).
        if not _store.log_lines(store, 1):
            readme = store / ".versioneer-keep"
            if not readme.exists():
                readme.write_text(f"versioneer store for {name}\n", encoding="utf-8")
            try:
                _store.add_and_commit(store, [readme.name], f"init {name}")
            except _store.GitError:
                pass
    except _store.GitError as e:
        raise click.ClickException(str(e))

    saved = cfg.save(cfg.Config(meta=meta))
    msg = f"[green]created[/green] {saved} + store {store}"
    if lfs_warn:
        console.print(f"[yellow]warn[/yellow]: {lfs_warn}")
    console.print(msg)


@config_grp.command("list")
def config_list():
    """List configs."""
    names = cfg.list_configs()
    if not names:
        console.print(f"no configs in {cfg.config_dir()} (use: versioneer config create --help)")
        return
    for n in names:
        console.print(n)


@config_grp.command("show")
@click.pass_context
def config_show(ctx):
    """Show current config TOML path + meta."""
    name = _require_config(ctx)
    try:
        c = cfg.load(name)
    except FileNotFoundError:
        raise click.ClickException(f"config {name!r} not found in {cfg.config_dir()}")
    console.print(f"[bold]{name}[/bold]  {cfg.config_path(name)}")
    console.print(f"upstream={c.meta.upstream or '(none)'} storage={c.meta.storage or '(none)'} "
                  f"notify={c.meta.notify} auto_commit={c.meta.auto_commit} "
                  f"auto_push={c.meta.auto_push} check_interval={c.meta.check_interval}")
    console.print(f"targets: {len(c.targets)}")


@config_grp.command("remove")
@click.pass_context
def config_remove(ctx):
    """Remove a config TOML (store repo on disk is kept)."""
    name = _require_config(ctx)
    path = cfg.config_path(name)
    if not path.exists():
        raise click.ClickException(f"config {name!r} not found")
    path.unlink()
    console.print(f"[yellow]removed[/yellow] {path} (store repo kept)")


def _require_config(ctx: click.Context) -> str:
    name = ctx.obj.get("config_name") if ctx.obj else None
    # also allow `versioneer config show -C hypr` via parent params
    if name is None and ctx.parent:
        name = (ctx.parent.params or {}).get("config_name")
    # click group nesting: cli -> config -> show, so check grandparent too
    if name is None and ctx.parent and ctx.parent.parent:
        name = (ctx.parent.parent.params or {}).get("config_name")
    if not name:
        raise click.ClickException("missing -C/--config <name> (e.g. versioneer -C hypr config show)")
    return name


def _select_targets(config, key: str | None) -> list:
    """Match targets by stored path, abs path, or live-resolved path."""
    if not key:
        return []
    from versioneer.core import monitor as _mon

    hit = cfg.find_target(config, key)
    if hit is not None:
        return [hit]
    try:
        stored, abs_p = _mon.resolve_input(key, config.meta.root or "")
    except ValueError:
        return []
    out = []
    for t in config.targets:
        assert isinstance(t, cfg.Target)
        if t.path == stored or t.abs_path == str(abs_p) or t.path == key:
            out.append(t)
    return out


def _stage_artifact(abs_path: Path, kind: str, symlink: str, ignore: list[str], dest: Path) -> None:
    """Copy live file/dir/link into the store (same semantics as target add)."""
    import shutil as _shutil

    from versioneer.core import monitor as _mon

    follow = (symlink == "follow")
    if abs_path.is_symlink() and not follow:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink() or dest.exists():
            if dest.is_dir() and not dest.is_symlink():
                _shutil.rmtree(dest)
            else:
                dest.unlink()
        dest.symlink_to(os.readlink(abs_path))
    elif kind == "dir":
        src_top = abs_path.resolve(strict=False) if follow else abs_path
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        if dest.is_dir():
            _shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        _shutil.copytree(
            src_top, dest, symlinks=(not follow),
            ignore=lambda d, names, _top=src_top, _ig=tuple(ignore): [
                n for n in names
                if _mon.matches_ignore(
                    ((Path(d).relative_to(_top).as_posix() + "/" + n)
                     if Path(d) != _top else n),
                    list(_ig))
            ],
        )
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _shutil.copy2(abs_path, dest)


# ---------- stubs (explicit, non-zero exit) ----------

def _stub(phase: str, what: str):
    console.print(f"[yellow]not yet implemented[/yellow]: {what} (planned: {phase}). "
                  "See README status banner.")
    raise SystemExit(2)


@cli.group("target")
@click.pass_context
def target_grp(ctx):
    """Track/untrack targets (Phase 1)."""


@target_grp.command("add")
@click.argument("path")
@click.option("--root", "root_opt", default=None, help="Root dir; stored paths are relative to it.")
@click.option("--kind", "kind_opt",
              type=click.Choice(["text", "binary", "dir", "manifest", "auto"]),
              default="auto", show_default=True)
@click.option("--flex", "flex_opt",
              type=click.Choice(["fixed", "user", "flexi", "auto"]),
              default="auto", show_default=True)
@click.option("--interest", default="state", type=click.Choice(["state", "diff"]), show_default=True)
@click.option("--glob", "glob_pat", default="", help="Glob pattern expanding to tracked set.")
@click.option("--ignore", "ignore_opts", multiple=True, help="Ignore pattern (repeatable).")
@click.option("--symlink", default="preserve", type=click.Choice(["preserve", "follow"]),
              show_default=True)
@click.option("--machines", default="", help="Comma-separated hostname allowlist (empty = all).")
@click.option("--retention", "retention_shorthand", type=int, default=None,
              help="Shorthand for --retention-count.")
@click.option("--retention-count", type=int, default=None)
@click.option("--retention-age", default=None, help="E.g. 30d.")
@click.option("--template/--no-template", default=False, show_default=True)
@click.option("--on-deploy", default="", help="Post-deploy hook command.")
@click.option("--deploy-path", default="", help="Destination for flexi targets.")
@click.option("--check-interval", default="", help="Per-target interval override.")
@click.pass_context
def target_add(ctx, path, root_opt, kind_opt, flex_opt, interest, glob_pat,
               ignore_opts, symlink, machines, retention_shorthand,
               retention_count, retention_age, template, on_deploy,
               deploy_path, check_interval):
    """Start tracking PATH from now (baseline + commit, no backfill)."""
    import shutil as _shutil

    from versioneer.core import lint as _lint
    from versioneer.core import monitor as _mon
    from versioneer.core import permissions as _perm
    from versioneer.core import store as _store

    name = _require_config(ctx)
    try:
        config = cfg.load(name)
    except FileNotFoundError:
        raise click.ClickException(f"config {name!r} not found in {cfg.config_dir()}")
    except ValueError as e:
        raise click.ClickException(str(e))

    # --root handling: single-root configs in v1
    if root_opt:
        root_norm = str(_mon.expand_path(root_opt))
        if config.meta.root and root_norm != str(_mon.expand_path(config.meta.root)):
            raise click.ClickException(
                f"--root {root_opt!r} differs from config root {config.meta.root!r} "
                "(multi-root configs not supported in v1)")
        if not config.meta.root:
            config.meta.root = root_opt
    effective_root = config.meta.root or ""

    # candidate set: glob expansion or single path
    candidates: list[str] = []
    glob_stored = glob_pat or ""
    if glob_stored:
        matches = _mon.expand_glob(glob_stored)
        if not matches:
            raise click.ClickException(f"--glob {glob_stored!r} matched nothing")
        candidates = [str(m) for m in matches]
    else:
        candidates = [path]

    machines_list = [m.strip() for m in machines.split(",") if m.strip()] if machines else []
    ignore_list = list(ignore_opts)
    count = retention_count if retention_count is not None else retention_shorthand
    retention: dict = {}
    if count is not None:
        retention["count"] = count
    if retention_age:
        retention["age"] = retention_age

    store = cfg.store_dir(config)
    try:
        _store.ensure_repo(store)
    except _store.GitError as e:
        raise click.ClickException(str(e))

    added = 0
    for cand in candidates:
        try:
            stored_path, abs_path = _mon.resolve_input(cand, effective_root)
        except ValueError as e:
            raise click.ClickException(str(e))
        if cfg.find_target(config, stored_path) or cfg.find_target(config, str(abs_path)):
            raise click.ClickException(f"already tracked: {cand!r} (use target list)")

        flex = flex_opt
        if flex == "auto":
            flex = _mon.detect_flex(abs_path)
        kind = kind_opt
        if kind == "auto":
            kind = _mon.detect_kind(abs_path, symlink)
        if kind == "manifest":
            raise click.ClickException("kind=manifest is added via `manifest` (Phase 8), not target add")
        if flex == "user" and effective_root:
            raise click.ClickException("root must not be set for user targets (flex=user)")

        # stat / symlink state
        dangling = abs_path.is_symlink() and not abs_path.exists()
        missing = not abs_path.exists() and not abs_path.is_symlink()
        if missing:
            raise click.ClickException(f"path does not exist: {abs_path}")
        if dangling and symlink == "follow":
            console.print(f"[yellow]warn[/yellow]: dangling symlink {abs_path} (follow mode)")
        elif dangling:
            console.print(f"[yellow]warn[/yellow]: dangling symlink {abs_path}")

        eff_ignore = _mon.wine_preset_ignores(abs_path, ignore_list) \
            if kind == "dir" else list(ignore_list)
        follow = (symlink == "follow")
        try:
            owner, group, mode = _perm.capture(abs_path, follow=follow)
        except OSError as e:
            raise click.ClickException(f"cannot stat {abs_path}: {e}")
        digest = _mon.hash_target(abs_path, kind, symlink, eff_ignore)

        # warn-only lints
        if kind == "text" and not dangling and abs_path.is_file():
            for w in _lint.scan_file(abs_path):
                console.print(f"[yellow]warn[/yellow]: {w}")
            for w in _lint.hardcoded_path_warnings(abs_path):
                console.print(f"[yellow]warn[/yellow]: {w}")
        if kind == "binary" and abs_path.is_file():
            try:
                size_mb = abs_path.stat().st_size / (1024 * 1024)
            except OSError:
                size_mb = 0
            warn_mb = config.meta.large_file_warn_mb or 10
            if size_mb > warn_mb:
                console.print(
                    f"[yellow]warn[/yellow]: large file {size_mb:.1f} MB > {warn_mb} MB — "
                    "uses git-lfs (warn-only)")

        target = cfg.Target(
            path=stored_path, abs_path=str(abs_path), kind=kind, glob=glob_stored,
            ignore=eff_ignore, symlink=symlink, flex=flex, interest=interest,
            deploy_path=deploy_path, owner=owner, group=group, mode=mode,
            hash=digest, machines=machines_list, check_interval=check_interval,
            retention=retention, template=template, on_deploy=on_deploy,
        )
        errors = target.validate(config.meta.root)
        if errors:
            raise click.ClickException("; ".join(errors))

        # stage artifact copy inside the store repo
        if effective_root:
            rel = Path(stored_path)
        else:
            rel = _mon.artifact_rel("", abs_path, flex, "")
        dest = store / rel
        try:
            if abs_path.is_symlink() and not follow:
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.is_symlink() or dest.exists():
                    if dest.is_dir() and not dest.is_symlink():
                        _shutil.rmtree(dest)
                    else:
                        dest.unlink()
                dest.symlink_to(os.readlink(abs_path))
            elif kind == "dir":
                src_top = abs_path.resolve(strict=False) if follow else abs_path
                if dest.is_symlink() or dest.is_file():
                    dest.unlink()
                if dest.is_dir():
                    _shutil.rmtree(dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                _shutil.copytree(
                    src_top, dest, symlinks=(not follow),
                    ignore=lambda d, names, _top=src_top, _ig=tuple(eff_ignore): [
                        n for n in names
                        if _mon.matches_ignore(
                            ((Path(d).relative_to(_top).as_posix() + "/" + n)
                             if Path(d) != _top else n),
                            list(_ig))
                    ],
                )
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                _shutil.copy2(abs_path, dest)
        except OSError as e:
            raise click.ClickException(f"cannot stage artifact for {abs_path}: {e}")

        if kind == "binary":
            warn = _store.ensure_lfs(store, [rel.as_posix()])
            if warn:
                console.print(f"[yellow]warn[/yellow]: {warn}")

        config.targets.append(target)
        added += 1
        console.print(f"[green]tracking[/green] {stored_path} "
                      f"(kind={kind} flex={flex} hash={digest[:19]}…)")

    try:
        cfg.save(config)
    except ValueError as e:
        raise click.ClickException(str(e))
    rels: list[str] = []
    for t in config.targets[-added:]:
        assert isinstance(t, cfg.Target)
        if effective_root:
            rels.append(Path(t.path).as_posix())
        else:
            rels.append(_mon.artifact_rel("", Path(t.abs_path), t.flex, "").as_posix())
    if kind_opt == "binary" or any(
            isinstance(t, cfg.Target) and t.kind == "binary" for t in config.targets[-added:]):
        ga_warn = _store.ensure_lfs(store, rels)
        if ga_warn:
            console.print(f"[yellow]warn[/yellow]: {ga_warn}")
        rels = rels + [".gitattributes"] if (store / ".gitattributes").exists() else rels
    try:
        label = candidates[0] if added == 1 else f"{added} targets"
        _store.add_and_commit(store, rels, f"track {label}")
    except _store.GitError as e:
        raise click.ClickException(f"baseline commit failed: {e}")
    console.print(f"[green]committed[/green] baseline for {added} target(s) in {store}")


@target_grp.command("list")
@click.pass_context
def target_list(ctx):
    """List tracked targets."""
    from rich.table import Table

    name = _require_config(ctx)
    try:
        config = cfg.load(name)
    except FileNotFoundError:
        raise click.ClickException(f"config {name!r} not found in {cfg.config_dir()}")
    if not config.targets:
        console.print(f"no targets in config {name!r} (use: target add --help)")
        return
    table = Table(title=f"targets in {name}")
    table.add_column("path")
    table.add_column("kind")
    table.add_column("flex")
    table.add_column("hash")
    table.add_column("owner/mode")
    for t in config.targets:
        assert isinstance(t, cfg.Target)
        short = (t.hash[:19] + "…") if len(t.hash) > 19 else t.hash
        table.add_row(t.path, t.kind, t.flex, short, f"{t.owner}/{t.mode}")
    console.print(table)


@target_grp.command("remove")
@click.argument("path")
@click.pass_context
def target_remove(ctx, path):
    """Stop tracking PATH (TOML entry + git rm; working file untouched)."""
    from versioneer.core import monitor as _mon
    from versioneer.core import store as _store

    name = _require_config(ctx)
    try:
        config = cfg.load(name)
    except FileNotFoundError:
        raise click.ClickException(f"config {name!r} not found in {cfg.config_dir()}")
    # match by stored path or abs path (resolve against config root)
    target = cfg.find_target(config, path)
    if target is None:
        try:
            stored, abs_p = _mon.resolve_input(path, config.meta.root or "")
            target = cfg.find_target(config, stored) or cfg.find_target(config, str(abs_p))
        except ValueError:
            target = None
    if target is None:
        raise click.ClickException(f"not tracked: {path!r}")
    assert isinstance(target, cfg.Target)
    if config.meta.root:
        rel = Path(target.path).as_posix()
    else:
        rel = _mon.artifact_rel("", Path(target.abs_path), target.flex, "").as_posix()
    config.targets = [t for t in config.targets if t is not target]
    try:
        cfg.save(config)
    except ValueError as e:
        raise click.ClickException(str(e))
    store = cfg.store_dir(config)
    try:
        _store.rm_and_commit(store, [rel], f"untrack {target.path}")
    except _store.GitError as e:
        raise click.ClickException(f"untrack commit failed: {e} "
                                   "(TOML entry already removed)")
    console.print(f"[yellow]untracked[/yellow] {target.path} "
                  "(working file untouched, git history kept)")


@cli.command("status")
@click.option("--host", default=None, help="Simulate a different hostname (machines filter).")
@click.pass_context
def status(ctx, host):
    """Show drift (read-only, offline-safe)."""
    from rich.table import Table

    from versioneer.core import monitor as _mon

    name = _require_config(ctx)
    try:
        config = cfg.load(name)
    except FileNotFoundError:
        raise click.ClickException(f"config {name!r} not found in {cfg.config_dir()}")
    except ValueError as e:
        raise click.ClickException(str(e))
    store = cfg.store_dir(config)
    results = _mon.scan_all(config, store, host or "")
    if not results:
        console.print(f"no targets in config {name!r} (use: target add --help)")
        return
    table = Table(title=f"status {name}")
    table.add_column("path")
    table.add_column("state")
    table.add_column("detail")
    colors = {"clean": "green", "modified": "yellow", "perm-drift": "magenta",
              "missing": "red", "untracked": "cyan", "read-error": "red"}
    from rich.markup import escape as _escape
    for r in results:
        t = r["target"]
        state = r["state"]
        detail = r["detail"]
        if r["host_skipped"]:
            detail = (detail + " [wrong host]" if detail else "[wrong host]")
        table.add_row(_escape(t.path),
                      f"[{colors.get(state, '')}]{state}[/]" if state in colors else _escape(state),
                      _escape(detail))
    console.print(table)


@cli.command("diff")
@click.argument("target", required=False)
@click.pass_context
def diff(ctx, target):
    """Show diff (read-only): unified diff for text, stat summary for binary/dir."""
    import difflib

    from versioneer.core import monitor as _mon
    from versioneer.core import store as _store

    name = _require_config(ctx)
    try:
        config = cfg.load(name)
    except FileNotFoundError:
        raise click.ClickException(f"config {name!r} not found in {cfg.config_dir()}")
    except ValueError as e:
        raise click.ClickException(str(e))
    store = cfg.store_dir(config)
    selected = _select_targets(config, target)
    if target and not selected:
        raise click.ClickException(f"not tracked: {target!r}")
    if not selected:
        selected = list(config.targets)
    if not selected:
        console.print(f"no targets in config {name!r}")
        return
    for t in selected:
        assert isinstance(t, cfg.Target)
        r = _mon.scan_one(t, config.meta.root or "", store)
        rel = r["rel"].as_posix()
        from rich.markup import escape as _escape
        console.print(f"[bold]{_escape(t.path)}[/bold]  {_escape(r['state'])}  {_escape(r['detail'])}")
        if r["state"] in ("clean",):
            continue
        if r["state"] in ("missing", "read-error"):
            console.print(f"  [red]{_escape(r['state'])}[/red]: {_escape(r['detail'])} "
                          "(no diff available)")
            continue
        if t.kind == "text":
            head = _store.show_head_file(store, rel)
            live = r["abs_path"]
            try:
                live_text = live.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError as e:
                console.print(f"  [red]cannot read live file: {e}[/red]")
                continue
            head_text = head.decode("utf-8", errors="replace").splitlines() if head else []
            if head is None:
                console.print("  (no committed artifact yet — new file)")
                continue
            for line in difflib.unified_diff(head_text, live_text, f"store/{rel}",
                                             f"live/{t.path}", lineterm=""):
                console.print(f"  {line}", markup=False, highlight=False)
        elif t.kind == "binary":
            try:
                size = r["abs_path"].stat().st_size if r["abs_path"].exists() else -1
            except OSError:
                size = -1
            console.print(f"  binary: {r['current_hash'][:19]}… size={size} bytes "
                          f"(baseline {t.hash[:19]}…)")
        elif t.kind == "dir":
            if r["untracked"]:
                console.print(f"  untracked ({len(r['untracked'])}): "
                              f"{', '.join(r['untracked'][:10])}")
            live_files = _mon.iter_dir_files(r["abs_path"], list(t.ignore or []))
            console.print(f"  dir: {len(live_files)} file(s) live, hash {r['current_hash'][:23]}… "
                          f"(baseline {t.hash[:23]}…)")
        else:  # manifest (Phase 8): recipe preview
            console.print("  manifest: regenerate on commit, replay on deploy (see Phase 8)")


@cli.command("log")
@click.argument("target", required=False)
@click.option("-n", "--number", default=10, show_default=True, help="Max commits to show.")
@click.pass_context
def log(ctx, target, number):
    """Show store log (read-only, offline-safe)."""
    from versioneer.core import monitor as _mon
    from versioneer.core import store as _store

    name = _require_config(ctx)
    try:
        config = cfg.load(name)
    except FileNotFoundError:
        raise click.ClickException(f"config {name!r} not found in {cfg.config_dir()}")
    except ValueError as e:
        raise click.ClickException(str(e))
    store = cfg.store_dir(config)
    rel = ""
    if target:
        selected = _select_targets(config, target)
        if not selected:
            raise click.ClickException(f"not tracked: {target!r}")
        t = selected[0]
        assert isinstance(t, cfg.Target)
        abs_p = _mon.live_abs_path(t.path, t.abs_path, config.meta.root or "")
        rel = _mon.store_rel_for(t.path, abs_p, t.flex, config.meta.root or "").as_posix()
    lines = _store.log_lines(store, number, rel)
    if not lines:
        console.print("(no commits yet)" if not rel else f"(no commits yet for {target})")
        return
    for line in lines:
        console.print(line)


@cli.command("commit")
@click.option("-m", "--message", default="", help="Commit message.")
@click.option("--all", "all_targets", is_flag=True, help="Commit all drifted targets.")
@click.argument("targets", nargs=-1)
@click.pass_context
def commit(ctx, message, all_targets, targets):
    """Save drift: re-hash + update TOML baselines + git commit (offline-safe)."""
    from versioneer.core import lint as _lint
    from versioneer.core import monitor as _mon
    from versioneer.core import permissions as _perm
    from versioneer.core import store as _store

    name = _require_config(ctx)
    try:
        config = cfg.load(name)
    except FileNotFoundError:
        raise click.ClickException(f"config {name!r} not found in {cfg.config_dir()}")
    except ValueError as e:
        raise click.ClickException(str(e))
    if not config.targets:
        console.print(f"no targets in config {name!r} (use: target add --help)")
        return
    if not all_targets and not targets:
        raise click.ClickException("nothing selected: pass <target>... or --all")
    if all_targets:
        selected = list(config.targets)
    else:
        selected = []
        for key in targets:
            hits = _select_targets(config, key)
            if not hits:
                raise click.ClickException(f"not tracked: {key!r}")
            selected.extend(h for h in hits if h not in selected)

    store = cfg.store_dir(config)
    try:
        _store.ensure_repo(store)
    except _store.GitError as e:
        raise click.ClickException(str(e))

    rels: list[str] = []
    committed: list[str] = []
    skipped: list[str] = []
    for t in selected:
        assert isinstance(t, cfg.Target)
        r = _mon.scan_one(t, config.meta.root or "", store)
        if r["state"] == "clean":
            skipped.append(f"{t.path} clean")
            continue
        if r["state"] == "missing":
            console.print(f"[red]error[/red]: {t.path} missing — cannot commit a deleted file "
                          "(restore it or `target remove` it)")
            skipped.append(f"{t.path} missing")
            continue
        if r["state"] == "read-error":
            console.print(f"[red]error[/red]: {t.path} read-error — {r['detail']} "
                          "(run with elevation)")
            skipped.append(f"{t.path} read-error")
            continue
        abs_path = r["abs_path"]
        rel = r["rel"]
        dest = store / rel
        # warn-only lints (same as add)
        if t.kind == "text" and abs_path.is_file() and not abs_path.is_symlink():
            for w in _lint.scan_file(abs_path):
                console.print(f"[yellow]warn[/yellow]: {t.path}: {w}")
        if t.kind == "binary" and abs_path.is_file() and not abs_path.is_symlink():
            try:
                size_mb = abs_path.stat().st_size / (1024 * 1024)
            except OSError:
                size_mb = 0
            warn_mb = config.meta.large_file_warn_mb or 10
            if size_mb > warn_mb:
                console.print(f"[yellow]warn[/yellow]: {t.path}: large file {size_mb:.1f} MB > "
                              f"{warn_mb} MB — uses git-lfs (warn-only)")
            # locked-file safe copy-then-hash notice
            try:
                _digest, copied = _mon.safe_sha256_file(abs_path.resolve(strict=False))
                if copied:
                    console.print(f"[yellow]warn[/yellow]: {t.path}: locked file? "
                                  "copied-then-hashed")
            except OSError:
                pass
        try:
            _stage_artifact(abs_path, t.kind, t.symlink, list(t.ignore or []), dest)
        except OSError as e:
            console.print(f"[red]error[/red]: {t.path}: cannot stage artifact: {e}")
            skipped.append(f"{t.path} stage-error")
            continue
        if t.kind == "binary":
            warn = _store.ensure_lfs(store, [rel.as_posix()])
            if warn:
                console.print(f"[yellow]warn[/yellow]: {warn}")
        # refresh baseline from live state
        follow = (t.symlink == "follow")
        try:
            owner, group, mode = _perm.capture(abs_path, follow=follow)
        except OSError as e:
            console.print(f"[red]error[/red]: {t.path}: cannot stat after stage: {e}")
            skipped.append(f"{t.path} stat-error")
            continue
        t.owner, t.group, t.mode = owner, group, mode
        t.hash = _mon.hash_target(abs_path, t.kind, t.symlink, list(t.ignore or []))
        rels.append(rel.as_posix())
        committed.append(t.path)
        # retention advisory (non-destructive, Q3 decision c)
        if t.kind == "binary" and t.retention:
            n = _store.count_artifact_commits(store, rel.as_posix())
            warn_r = _store.retention_warning(n + 1, t.retention)
            if warn_r:
                console.print(f"[yellow]warn[/yellow]: {t.path}: {warn_r}")

    if not committed:
        console.print("clean — nothing to commit" if all("clean" in s for s in skipped)
                      else "nothing committed (see errors above)")
        return
    if any(isinstance(t, cfg.Target) and t.kind == "binary" for t in selected
           if t.path in committed):
        ga_warn = _store.ensure_lfs(store, rels)
        if ga_warn:
            console.print(f"[yellow]warn[/yellow]: {ga_warn}")
        if (store / ".gitattributes").exists() and ".gitattributes" not in rels:
            rels = rels + [".gitattributes"]
    try:
        cfg.save(config)
    except ValueError as e:
        raise click.ClickException(str(e))
    msg = message or f"update {committed[0] if len(committed) == 1 else f'{len(committed)} targets'}"
    try:
        sha = _store.add_and_commit(store, rels, msg)
    except _store.GitError as e:
        raise click.ClickException(f"commit failed: {e}")
    if sha:
        console.print(f"[green]committed[/green] {len(committed)} target(s) ({sha[:7]}): "
                      f"{', '.join(committed)}")
    else:
        # Store content already in sync (e.g. perm-only change is invisible
        # to git, which tracks only the exec bit): TOML baselines above were
        # still updated via cfg.save, so report success, not "clean".
        console.print(f"[green]updated baselines[/green] for {len(committed)} target(s) "
                      f"(store content unchanged): {', '.join(committed)}")
    for s in skipped:
        console.print(f"  skipped: {s}")


@cli.command("push")
@click.pass_context
def push(ctx):
    """Push store (defers with a message when offline)."""
    from versioneer.core import store as _store

    name = _require_config(ctx)
    try:
        config = cfg.load(name)
    except FileNotFoundError:
        raise click.ClickException(f"config {name!r} not found in {cfg.config_dir()}")
    except ValueError as e:
        raise click.ClickException(str(e))
    store = cfg.store_dir(config)
    try:
        out = _store.push(store)
    except _store.GitError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]pushed[/green] {name}" + (f": {out}" if out else ""))


@cli.command("pull")
@click.pass_context
def pull(ctx):
    """Pull store (defers with a message when offline)."""
    from versioneer.core import store as _store

    name = _require_config(ctx)
    try:
        config = cfg.load(name)
    except FileNotFoundError:
        raise click.ClickException(f"config {name!r} not found in {cfg.config_dir()}")
    except ValueError as e:
        raise click.ClickException(str(e))
    store = cfg.store_dir(config)
    try:
        out = _store.pull(store)
    except _store.GitError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]pulled[/green] {name}" + (f": {out}" if out else ""))


@cli.command("deploy")
@click.argument("targets", nargs=-1)
@click.option("--dry-run", is_flag=True)
@click.option("--plan-out", default=None)
@click.option("--plan", "plan_in", default=None)
@click.option("--to", "to_path", default=None)
@click.option("--yes", is_flag=True)
@click.option("--no-interaction", is_flag=True)
@click.option("--force-host", is_flag=True)
@click.option("--host", "host_opt", default=None,
              help="Simulate a different hostname (machines filter).")
@click.option("--prune", is_flag=True)
@click.option("--apply", is_flag=True)
@click.pass_context
def deploy(ctx, targets, dry_run, plan_out, plan_in, to_path, yes, no_interaction,
           force_host, host_opt, prune, apply):
    """Restore from store (selective, safe; never whole-run abort)."""
    import json as _json

    from rich.table import Table

    from versioneer.core import deploy as _dep

    name = _require_config(ctx)
    try:
        config = cfg.load(name)
    except FileNotFoundError:
        raise click.ClickException(f"config {name!r} not found in {cfg.config_dir()}")
    except ValueError as e:
        raise click.ClickException(str(e))
    if not config.targets:
        console.print(f"no targets in config {name!r} (use: target add --help)")
        return
    if targets:
        selected = []
        for key in targets:
            hits = _select_targets(config, key)
            if not hits:
                raise click.ClickException(f"not tracked: {key!r}")
            selected.extend(h for h in hits if h not in selected)
    else:
        selected = list(config.targets)
    store = cfg.store_dir(config)
    assume_yes = bool(yes or no_interaction)
    multi = len(selected) > 1

    plan_map = None
    if plan_in:
        try:
            plan_map = _dep.load_plan(plan_in)
        except (OSError, ValueError) as e:
            raise click.ClickException(f"cannot load --plan {plan_in!r}: {e}")

    if plan_out:
        entries = []
        for t in selected:
            assert isinstance(t, cfg.Target)
            dest = _dep.live_dest(t, config.meta.root or "", to_path or "",
                                  multi)
            entries.append(_dep.plan_entry(t, config.meta.root or "", store, dest))
        try:
            with open(plan_out, "w", encoding="utf-8") as f:
                _json.dump(entries, f, indent=2)
        except OSError as e:
            raise click.ClickException(f"cannot write --plan-out {plan_out!r}: {e}")
        console.print(f"[green]wrote plan[/green] {plan_out} ({len(entries)} entries)")
        for e in entries:
            console.print(f"  {e['target']}: {e['action']} "
                          f"(src={str(e['src_hash'])[:19]}… dst={str(e['dst_hash'])[:19]}…)")
        return

    results = []
    for t in selected:
        assert isinstance(t, cfg.Target)
        r = _dep.deploy_one(t, config, store, to_path or "", multi,
                            assume_yes, prune, bool(apply), bool(force_host),
                            host_opt or "", bool(dry_run), plan_map)
        results.append(r)

    table = Table(title=f"deploy {name}{' (dry-run)' if dry_run else ''}")
    table.add_column("target")
    table.add_column("status")
    table.add_column("detail")
    colors = {"ok": "green", "skipped": "yellow", "error": "red"}
    from rich.markup import escape as _escape
    for r in results:
        st = r.get("status", "")
        reason = r.get("reason", "")
        if r.get("backups"):
            reason = reason + f" [backups: {len(r['backups'])}]"
        table.add_row(_escape(str(r.get("target", ""))),
                      f"[{colors.get(st, '')}]{_escape(st)}[/]" if st in colors
                      else _escape(st),
                      _escape(str(reason)))
        if r.get("replay"):
            console.print(f"  replay preview for {r['target']}:")
            console.print(str(r["replay"])[:2000], markup=False, highlight=False)
    console.print(table)
    if dry_run:
        console.print("dry-run: no writes performed")
        return
    try:
        spath = _dep.write_status_file(name, results)
    except OSError as e:
        raise click.ClickException(f"deploy ran, but status file failed: {e}")
    console.print(f"deploy-status: {spath}")
    if any(r.get("status") == "error" for r in results):
        raise SystemExit(1)


@cli.group("service")
def service_grp():
    """Manage systemd units (Phase 3)."""


@service_grp.command("install")
def service_install():
    """Install user + system units (writes unit files, no sudo needed)."""
    from versioneer.core import daemon as _daemon

    upath = _daemon.user_unit_path()
    try:
        upath.parent.mkdir(parents=True, exist_ok=True)
        upath.write_text(_daemon.USER_UNIT, encoding="utf-8")
    except OSError as e:
        raise click.ClickException(f"cannot write {upath}: {e}")
    # system unit cannot be installed without sudo: stage it under state dir
    from versioneer.core import deploy as _dep

    sdir = _dep.state_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    sys_staged = sdir / "versioneer-system.service"
    try:
        sys_staged.write_text(_daemon.SYSTEM_UNIT, encoding="utf-8")
    except OSError as e:
        raise click.ClickException(f"cannot stage system unit: {e}")
    console.print(f"[green]installed[/green] {upath}")
    console.print(f"system unit staged at {sys_staged} — install with: "
                  "sudo cp versioneer-system.service "
                  "/etc/systemd/system/ && sudo systemctl daemon-reload")


@service_grp.command("enable")
def service_enable():
    """Enable/start user unit (and print system-unit hint)."""
    import shutil as _shutil
    import subprocess as _sp

    sys = _shutil.which("systemctl")
    if not sys:
        raise click.ClickException("systemctl not found (non-systemd machine?)")
    try:
        _sp.run([sys, "--user", "daemon-reload"], check=False,
                capture_output=True, timeout=30)
        p = _sp.run([sys, "--user", "enable", "--now", "versioneer-user.service"],
                    capture_output=True, text=True, timeout=60, check=False)
    except (OSError, _sp.TimeoutExpired) as e:
        raise click.ClickException(f"systemctl failed: {e}")
    if p.returncode != 0:
        raise click.ClickException(
            f"systemctl enable failed: {(p.stderr or p.stdout).strip()}")
    console.print("[green]enabled[/green] versioneer-user.service "
                  "(system unit: sudo systemctl enable --now versioneer-system)")


@service_grp.command("disable")
def service_disable():
    """Disable/stop user unit."""
    import shutil as _shutil
    import subprocess as _sp

    sys = _shutil.which("systemctl")
    if not sys:
        raise click.ClickException("systemctl not found (non-systemd machine?)")
    try:
        p = _sp.run([sys, "--user", "disable", "--now", "versioneer-user.service"],
                    capture_output=True, text=True, timeout=60, check=False)
    except (OSError, _sp.TimeoutExpired) as e:
        raise click.ClickException(f"systemctl failed: {e}")
    if p.returncode != 0:
        raise click.ClickException(
            f"systemctl disable failed: {(p.stderr or p.stdout).strip()}")
    console.print("[yellow]disabled[/yellow] versioneer-user.service")


@service_grp.command("check")
@click.option("--all", "all_configs", is_flag=True,
              help="Check all configs (default: -C config or all).")
@click.option("--host", default=None, help="Simulate a different hostname.")
@click.pass_context
def service_check(ctx, all_configs, host):
    """Run one monitor pass (used by systemd units; read-only by default)."""
    from versioneer.core import daemon as _daemon

    name = None
    try:
        name = _require_config(ctx)
    except click.ClickException:
        name = None
    if all_configs or not name:
        results = _daemon.check_all(host or "")
        if not results:
            console.print("no configs to check")
            return
        for r in results:
            n = len(r["drift"])
            console.print(f"{r['config']}: {n} drifted "
                          f"{'(notified)' if r['notified'] else ''}"
                          f"{'(auto-committed)' if r['committed'] else ''}")
            for d in r["drift"][:10]:
                console.print(f"  {d['target']} {d['state']}")
        return
    r = _daemon.check_once(name, host or "")
    console.print(f"{name}: {len(r['drift'])} drifted")
    for d in r["drift"][:20]:
        console.print(f"  {d['target']} {d['state']}: {d['detail']}")


@cli.command("bootstrap")
@click.argument("upstream", required=False)
@click.option("--all", "all_configs", is_flag=True)
@click.option("--to", "to_dir", default=None)
@click.option("--host", default=None)
@click.option("--dry-run", is_flag=True, help="Preview: clone + deploy --dry-run only.")
@click.option("--yes", is_flag=True, help="Assume yes for deploy overwrites.")
def bootstrap(upstream, all_configs, to_dir, host, dry_run, yes):
    """Clone store(s) + recreate TOML(s) + deploy (new-machine flow)."""
    import re as _re

    from versioneer.core import deploy as _dep
    from versioneer.core import store as _store

    if not upstream and not all_configs:
        raise click.ClickException("usage: versioneer bootstrap <upstream-url> "
                                   "[--to DIR] | versioneer bootstrap --all")
    if upstream and all_configs:
        raise click.ClickException("pass either <upstream-url> or --all, not both")

    def _name_from_url(url: str) -> str:
        base = url.rstrip("/").split("/")[-1]
        base = _re.sub(r"\.git$", "", base)
        base = _re.sub(r"^versioneer-", "", base)
        return _re.sub(r"[^A-Za-z0-9_-]", "-", base).strip("-") or "bootstrap"

    def _deploy_config(cname: str) -> None:
        try:
            conf = cfg.load(cname)
        except (FileNotFoundError, ValueError) as e:
            console.print(f"[red]error[/red]: cannot load {cname}: {e}")
            raise SystemExit(1)
        store = cfg.store_dir(conf)
        results = []
        for t in conf.targets:
            assert isinstance(t, cfg.Target)
            r = _dep.deploy_one(t, conf, store, "", len(conf.targets) > 1,
                                bool(yes), False, False, False,
                                host or "", bool(dry_run), None)
            results.append(r)
            console.print(f"  {t.path}: {r['status']} — {r['reason']}")
        if dry_run:
            console.print(f"dry-run for {cname}: no writes, no status file")
            return
        try:
            spath = _dep.write_status_file(cname, results)
        except OSError as e:
            raise click.ClickException(f"deploy ran, status file failed: {e}")
        console.print(f"deploy-status for {cname}: {spath}")
        if any(r.get("status") == "error" for r in results):
            raise SystemExit(1)

    if all_configs:
        names = cfg.list_configs()
        if not names:
            console.print(f"no configs in {cfg.config_dir()} (nothing to bootstrap)")
            return
        for n in names:
            try:
                conf = cfg.load(n)
            except (FileNotFoundError, ValueError) as e:
                console.print(f"[red]error[/red]: {n}: {e}")
                continue
            store = cfg.store_dir(conf)
            if not _store.is_repo(store) and conf.meta.upstream:
                console.print(f"cloning {conf.meta.upstream} -> {store}")
                try:
                    _store.clone(conf.meta.upstream, store)
                except _store.GitError as e:
                    console.print(f"[red]error[/red]: clone failed for {n}: {e}")
                    continue
            console.print(f"[bold]{n}[/bold]: deploying {len(conf.targets)} target(s)")
            _deploy_config(n)
        return

    assert upstream
    cname = _name_from_url(upstream)
    import os as _os
    from pathlib import Path as _Path

    raw_store = to_dir or f"~/versioneer-store/{cname}"
    store_path = _Path(_os.path.expandvars(raw_store)).expanduser()
    if cfg.config_path(cname).exists():
        console.print(f"config {cname!r} already exists — pulling + deploying")
        try:
            conf = cfg.load(cname)
        except ValueError as e:
            raise click.ClickException(str(e))
        store_path = cfg.store_dir(conf)
        try:
            out = _store.pull(store_path)
            console.print(f"pulled {cname}" + (f": {out}" if out else ""))
        except _store.GitError as e:
            console.print(f"[yellow]warn[/yellow]: {e}")
        _deploy_config(cname)
        return
    console.print(f"cloning {upstream} -> {store_path}")
    try:
        _store.clone(upstream, store_path)
    except _store.GitError as e:
        raise click.ClickException(str(e))
    meta = cfg.Meta(name=cname, upstream=upstream, storage=str(store_path))
    errs = meta.validate()
    if errs:
        raise click.ClickException("; ".join(errs))
    # Recreate minimal TOML: adopt store-relative files as text targets?
    # Full target inference needs live paths which don't exist yet on a new
    # machine, so bootstrap writes the config shell; `deploy --all` (below)
    # restores whatever the store already contains via per-target deploy.
    # When the store has no TOML-adopted targets yet, list store files as hint.
    try:
        saved = cfg.save(cfg.Config(meta=meta))
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]created[/green] {saved} + store {store_path}")
    console.print("hint: on the source machine run `push`; here `deploy` "
                  "restores tracked targets once TOML entries exist.")
    # If TOML has no targets (fresh clone with no exported config), there is
    # nothing to deploy yet — exit success, not error.
    conf = cfg.load(cname)
    if not conf.targets:
        console.print("no targets declared yet — nothing to deploy")
        return
    _deploy_config(cname)


@cli.command("watch")
@click.argument("directory")
@click.option("--auto-add", is_flag=True)
@click.option("--root", "root_opt", default=None,
              help="Root dir; added targets are stored relative to it.")
@click.option("--ignore", "ignore_opts", multiple=True,
              help="Ignore pattern (repeatable).")
@click.option("--glob", "glob_pat", default="", help="Glob stored on added targets.")
@click.option("--timeout", "timeout_s", type=int, default=None,
              help="Stop watching after N seconds (tests/CI).")
@click.pass_context
def watch(ctx, directory, auto_add, root_opt, ignore_opts, glob_pat, timeout_s):
    """Capture changed files (snapshot, wait Ctrl-C, add)."""
    import time as _time

    from versioneer.core import monitor as _mon
    from versioneer.core import watch as _watch

    name = _require_config(ctx)
    try:
        config = cfg.load(name)
    except FileNotFoundError:
        raise click.ClickException(f"config {name!r} not found in {cfg.config_dir()}")
    except ValueError as e:
        raise click.ClickException(str(e))
    top = _mon.expand_path(directory)
    if not top.is_dir():
        raise click.ClickException(f"not a directory: {directory!r}")
    ignore = list(ignore_opts)
    before = _watch.snapshot(top, ignore)
    if timeout_s is None:
        import os as _os

        try:
            timeout_s = int(_os.environ.get("VERSIONEER_WATCH_TIMEOUT", "0") or 0)
        except ValueError:
            timeout_s = 0
    timeout_s = timeout_s or 0
    console.print(f"watching {top} ({len(before)} files) — make changes, then Ctrl-C")
    try:
        if timeout_s > 0:
            _time.sleep(timeout_s)
        else:
            while True:
                _time.sleep(1)
    except KeyboardInterrupt:
        pass
    after = _watch.snapshot(top, ignore)
    diff = _watch.diff_snapshots(before, after)
    changed = diff["changed"] + diff["added"]
    if not changed:
        console.print("no changes detected")
        if diff["removed"]:
            console.print(f"removed ({len(diff['removed'])}): "
                          f"{', '.join(diff['removed'][:10])}")
        return
    console.print(f"changed ({len(changed)}): {', '.join(changed[:20])}")
    if diff["removed"]:
        console.print(f"removed ({len(diff['removed'])}): "
                      f"{', '.join(diff['removed'][:10])}")
    if auto_add:
        picked = changed
    else:
        picked = []
        for rel in changed:
            if click.confirm(f"track {rel}?", default=True):
                picked.append(rel)
    if not picked:
        console.print("nothing selected")
        return
    # delegate to target add logic per file with same root
    root = root_opt or config.meta.root or ""
    added = 0
    for rel in picked:
        full = (top / rel).as_posix()
        args = ["-C", name, "target", "add", full]
        if root:
            args += ["--root", root]
        for ig in ignore:
            args += ["--ignore", ig]
        # invoke in-process via Click runner to reuse validation/commit path
        from click.testing import CliRunner as _CR

        r = _CR().invoke(cli, args)
        console.print(r.output, markup=False, highlight=False)
        if r.exit_code == 0:
            added += 1
            if glob_pat:
                try:
                    _conf = cfg.load(name)
                    _hit = cfg.find_target(_conf, full)
                    if _hit is None:
                        from versioneer.core import monitor as _mm

                        _stored, _abs = _mm.resolve_input(
                            full, _conf.meta.root or "")
                        _hit = cfg.find_target(_conf, _stored)
                    if _hit is not None:
                        _hit.glob = glob_pat
                        cfg.save(_conf)
                except (ValueError, OSError):
                    pass
    console.print(f"added {added}/{len(picked)} target(s)")


@cli.command("manifest")
@click.option("--packages", is_flag=True)
@click.option("--wine", "wine_", is_flag=True)
@click.option("--systemd", "systemd_", is_flag=True)
@click.option("--env", "env_", is_flag=True)
@click.pass_context
def manifest(ctx, packages, wine_, systemd_, env_):
    """Generate manifests (regenerated on commit, replayed on deploy)."""
    from versioneer.core import manifest as _mg
    from versioneer.core import monitor as _mon
    from versioneer.core import permissions as _perm
    from versioneer.core import store as _store

    name = _require_config(ctx)
    try:
        config = cfg.load(name)
    except FileNotFoundError:
        raise click.ClickException(f"config {name!r} not found in {cfg.config_dir()}")
    except ValueError as e:
        raise click.ClickException(str(e))
    wanted: list[str] = []
    if packages:
        wanted.append("packages")
    if wine_:
        wanted.append("wine")
    if systemd_:
        wanted.append("systemd")
    if env_:
        wanted.append("env")
    if not wanted:
        raise click.ClickException("nothing selected: pass at least one of "
                                   "--packages --wine --systemd --env")
    store = cfg.store_dir(config)
    try:
        _store.ensure_repo(store)
    except _store.GitError as e:
        raise click.ClickException(str(e))
    rels: list[str] = []
    for kind in wanted:
        fname, content, replay = _mg.GENERATORS[kind]()
        dest = store / fname
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        except OSError as e:
            raise click.ClickException(f"cannot write manifest {fname}: {e}")
        rels.append(fname)
        # ensure a manifest-kind target entry exists for status/diff/deploy
        existing = None
        for t in config.targets:
            assert isinstance(t, cfg.Target)
            if t.kind == "manifest" and t.path == fname:
                existing = t
                break
        if existing is None:
            try:
                owner, group, mode = _perm.capture(dest)
            except OSError:
                owner, group, mode = "", "", "0644"
            digest = _mon.hash_target(dest, "text", "preserve", [])
            config.targets.append(cfg.Target(
                path=fname, abs_path=str(dest), kind="manifest",
                flex="fixed", interest="state", owner=owner, group=group,
                mode=mode, hash=digest, on_deploy=""))
        else:
            existing.hash = _mon.hash_target(dest, "text", "preserve", [])
            try:
                o, g, m = _perm.capture(dest)
                existing.owner, existing.group, existing.mode = o, g, m
            except OSError:
                pass
        console.print(f"[green]manifest[/green] {kind} -> {fname} ({replay})")
    try:
        cfg.save(config)
    except ValueError as e:
        raise click.ClickException(str(e))
    try:
        sha = _store.add_and_commit(store, rels,
                                    f"manifest {'+'.join(wanted)}")
    except _store.GitError as e:
        raise click.ClickException(f"manifest commit failed: {e}")
    if sha:
        console.print(f"[green]committed[/green] manifests ({sha[:7]})")
    else:
        console.print("manifests unchanged")


@cli.command("doctor")
@click.option("--secrets", "secrets_only", is_flag=True)
@click.pass_context
def doctor(ctx, secrets_only):
    """Health checks (exit != 0 on errors)."""
    import shutil as _shutil

    from versioneer.core import lint as _lint
    from versioneer.core import monitor as _mon
    from versioneer.core import store as _store

    name = None
    try:
        name = _require_config(ctx)
    except click.ClickException:
        name = None
    names = [name] if name else cfg.list_configs()
    if name and name not in cfg.list_configs():
        raise click.ClickException(f"config {name!r} not found in {cfg.config_dir()}")
    if not names:
        console.print(f"no configs in {cfg.config_dir()} (use: versioneer config create --help)")
        if not _store.lfs_available():
            console.print("[yellow]warn[/yellow]: git-lfs not found "
                          "(sudo pacman -S git-lfs)")
        return

    errors = 0
    warnings = 0

    def _check_config(cname: str) -> None:
        nonlocal errors, warnings
        try:
            conf = cfg.load(cname)
        except (FileNotFoundError, ValueError) as e:
            console.print(f"[red]error[/red] {cname}: {e}")
            errors += 1
            return
        store = cfg.store_dir(conf)
        console.print(f"[bold]{cname}[/bold] storage={store} upstream={conf.meta.upstream or '(none)'}")
        if not _store.lfs_available():
            console.print("  [yellow]warn[/yellow]: git-lfs not found "
                          "(binary targets need it)")
            warnings += 1
        if not _store.is_repo(store):
            console.print(f"  [red]error[/red]: store is not a git repo: {store}")
            errors += 1
        elif conf.meta.upstream and not _store.get_upstream(store):
            console.print("  [yellow]warn[/yellow]: upstream not configured in store")
            warnings += 1
        # disk quota (statvfs, warn-only)
        try:
            st = _shutil.disk_usage(store if store.exists() else store.parent)
            if st.free < 500 * 1024 * 1024:
                console.print(f"  [yellow]warn[/yellow]: low disk free "
                              f"{st.free // (1024 * 1024)} MB on store fs")
                warnings += 1
        except OSError:
            pass
        for t in conf.targets:
            assert isinstance(t, cfg.Target)
            r = _mon.scan_one(t, conf.meta.root or "", store)
            if r["state"] == "read-error":
                console.print(f"  [red]error[/red] {t.path}: read-error — {r['detail']}")
                errors += 1
            elif r["state"] == "missing":
                if t.abs_path and _mon.expand_path(t.abs_path).is_symlink():
                    console.print(f"  [red]error[/red] {t.path}: dangling symlink")
                    errors += 1
                else:
                    console.print(f"  [yellow]warn[/yellow] {t.path}: missing")
                    warnings += 1
            if t.kind == "binary" and t.abs_path:
                try:
                    size = _mon.expand_path(t.abs_path).stat().st_size
                    warn_mb = conf.meta.large_file_warn_mb or 10
                    if size > warn_mb * 1024 * 1024:
                        console.print(f"  [yellow]warn[/yellow] {t.path}: large file "
                                      f"{size / (1024 * 1024):.1f} MB > {warn_mb} MB")
                        warnings += 1
                except OSError:
                    pass
            if t.on_deploy and not _shutil.which(t.on_deploy.split()[0]):
                console.print(f"  [yellow]warn[/yellow] {t.path}: on_deploy validator "
                              f"not on PATH: {t.on_deploy.split()[0]}")
                warnings += 1
            if t.kind == "binary" and t.retention:
                n = _store.count_artifact_commits(
                    store, r["rel"].as_posix())
                w = _store.retention_warning(n, t.retention)
                if w:
                    console.print(f"  [yellow]warn[/yellow] {t.path}: {w}")
                    warnings += 1
            # secrets audit (live file scan)
            live = r["abs_path"]
            try:
                if live.is_file() and not live.is_symlink() and \
                        live.stat().st_size < 5_000_000:
                    for wmsg in _lint.scan_file(live):
                        console.print(f"  [yellow]warn[/yellow] {t.path}: {wmsg}")
                        warnings += 1
            except OSError:
                pass

    if secrets_only:
        # secret audit only across selected configs
        for n in names:
            try:
                conf = cfg.load(n)
            except (FileNotFoundError, ValueError):
                continue
            store = cfg.store_dir(conf)
            for t in conf.targets:
                assert isinstance(t, cfg.Target)
                r = _mon.scan_one(t, conf.meta.root or "", store)
                live = r["abs_path"]
                try:
                    if live.is_file() and not live.is_symlink():
                        for wmsg in _lint.scan_file(live):
                            console.print(f"{n}/{t.path}: {wmsg}")
                            warnings += 1
                except OSError:
                    pass
        console.print(f"secrets audit: {warnings} warning(s)")
        return

    for n in names:
        _check_config(n)
    console.print(f"doctor: {errors} error(s), {warnings} warning(s)")
    if errors:
        raise SystemExit(1)
