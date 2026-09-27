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
    "-C",
    "--config",
    "config_name",
    default=None,
    help="Config name to operate on (e.g. -C hypr). Omit to use 'default' (from init) or the sole config.",
)
@click.version_option(__version__)
@click.pass_context
def cli(ctx: click.Context, config_name: str | None) -> None:
    """Service-based file and config monitoring for Linux."""
    ctx.ensure_object(dict)
    ctx.obj["config_name"] = config_name
    # Transparent sudo: root-via-sudo reuses the invoking user's config/store
    # (see elevate.effective_home) and store/config writes chown back to the
    # user — but prefer a plain user run (sudo read is automatic).
    try:
        from versioneer.core import elevate as _elev_cli

        _warn = _elev_cli.sudo_transparency_warning()
    except ImportError:
        _warn = None
    if _warn:
        console.print(f"[yellow]warn[/yellow]: {_warn}")


# ---------- init (default config quickstart) ----------

DEFAULT_CONFIG_NAME = "default"
DEFAULT_STORE_PATH = "~/versioneer-store/default"


def _create_config_files(meta, store_path: str):
    """Shared create logic for `config create` and `init`."""
    from versioneer.core import elevate as _elev
    from versioneer.core import store as _store

    name = meta.name
    upstream = meta.upstream
    errors = meta.validate()
    if errors:
        raise click.ClickException("; ".join(errors))
    if cfg.config_path(name).exists():
        raise click.ClickException(f"config {name!r} already exists: {cfg.config_path(name)}")

    store = _elev.expand_user(os.path.expandvars(store_path))
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
    return saved, store, lfs_warn


@cli.command("init")
@click.argument("upstream", required=False, default="")
def init_cmd(upstream: str | None) -> None:
    """Create default config + store (quickstart).

    UPSTREAM is an optional upstream git URL (empty = local only).
    Creates ~/.config/versioneer/default.toml + ~/versioneer-store/default/.
    """
    upstream = (upstream or "").strip()
    meta = cfg.Meta(
        name=DEFAULT_CONFIG_NAME,
        upstream=upstream,
        storage=DEFAULT_STORE_PATH,
    )
    saved, store, lfs_warn = _create_config_files(meta, DEFAULT_STORE_PATH)
    msg = f"[green]created[/green] {saved} + store {store}"
    if lfs_warn:
        console.print(f"[yellow]warn[/yellow]: {lfs_warn}")
    console.print(msg)


# ---------- config management (implemented) ----------


@cli.group("config")
def config_grp() -> None:
    """Create/list/show/set-upstream/remove configs."""


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
    meta = cfg.Meta(
        name=name,
        upstream=upstream,
        storage=store_path,
        notify=notify,
        auto_commit=auto_commit,
        auto_push=auto_push,
        check_interval=check_interval,
    )
    saved, store, lfs_warn = _create_config_files(meta, store_path)
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
    console.print(
        f"upstream={c.meta.upstream or '(none)'} storage={c.meta.storage or '(none)'} "
        f"notify={c.meta.notify} auto_commit={c.meta.auto_commit} "
        f"auto_push={c.meta.auto_push} check_interval={c.meta.check_interval}"
    )
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


@config_grp.command("set-upstream")
@click.argument("url", required=False)
@click.option("--remove", is_flag=True, default=False, help="Clear upstream (local-only).")
@click.pass_context
def config_set_upstream(ctx, url, remove):
    """Set or clear the upstream git URL for a config.

    Updates the TOML (meta.upstream) and the store's git origin, then
    refreshes the store-side snapshot. Offline-safe (no network access).

    \b
    versioneer -C hypr config set-upstream git@github.com:me/new.git
    versioneer -C hypr config set-upstream --remove
    """
    from versioneer.core import store as _store

    name = _require_config(ctx)
    try:
        config = cfg.load(name)
    except FileNotFoundError:
        raise click.ClickException(f"config {name!r} not found in {cfg.config_dir()}")
    except ValueError as e:
        raise click.ClickException(str(e))
    if remove and url:
        raise click.ClickException("pass either URL or --remove, not both")
    if remove:
        new_upstream = ""
    else:
        new_upstream = (url or "").strip()
        if not new_upstream:
            raise click.ClickException(
                "usage: versioneer -C <name> config set-upstream <git-url> | --remove"
            )
    old_upstream = config.meta.upstream or ""
    store = cfg.store_dir(config)
    try:
        _store.ensure_repo(store)
        if new_upstream:
            _store.set_upstream(store, new_upstream)
        else:
            _store.remove_upstream(store)
    except _store.GitError as e:
        raise click.ClickException(str(e))
    config.meta.upstream = new_upstream
    try:
        saved = cfg.save(config)
    except ValueError as e:
        raise click.ClickException(str(e))
    try:
        cfg.export_snapshot(config, store)
        try:
            _store.add_and_commit(store, [cfg.SNAPSHOT_NAME], "set upstream")
        except _store.GitError:
            pass
    except (ValueError, OSError) as e:
        console.print(f"[yellow]warn[/yellow]: snapshot refresh skipped: {e}")
    if not new_upstream:
        console.print(f"[green]cleared[/green] upstream for {name} ({saved}, local-only)")
    elif old_upstream == new_upstream:
        console.print(f"upstream unchanged for {name}: {new_upstream or '(none)'}")
    else:
        console.print(
            f"[green]updated[/green] upstream for {name}: "
            f"{old_upstream or '(none)'} -> {new_upstream}"
        )


def _require_config(ctx: click.Context) -> str:
    name = ctx.obj.get("config_name") if ctx.obj else None
    # also allow `versioneer config show -C hypr` via parent params
    if name is None and ctx.parent:
        name = (ctx.parent.params or {}).get("config_name")
    # click group nesting: cli -> config -> show, so check grandparent too
    if name is None and ctx.parent and ctx.parent.parent:
        name = (ctx.parent.parent.params or {}).get("config_name")
    if name:
        return name
    # No -C given: fall back to the `init` default, else the sole config.
    try:
        names = cfg.list_configs()
    except OSError:
        names = []
    if DEFAULT_CONFIG_NAME in names:
        return DEFAULT_CONFIG_NAME
    if len(names) == 1:
        return names[0]
    hint = f" (available: {', '.join(names)})" if names else ""
    raise click.ClickException(
        f"missing -C/--config <name> (e.g. versioneer -C hypr config show){hint} "
        f"— omit -C only when '{DEFAULT_CONFIG_NAME}' exists or a single config exists"
    )


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

    follow = symlink == "follow"
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
            src_top,
            dest,
            symlinks=(not follow),
            ignore=lambda d, names, _top=src_top, _ig=tuple(ignore): [
                n
                for n in names
                if n == ".git"
                or _mon.matches_ignore(
                    ((Path(d).relative_to(_top).as_posix() + "/" + n) if Path(d) != _top else n),
                    list(_ig),
                )
            ],
        )
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _shutil.copy2(abs_path, dest)


# ---------- stubs (explicit, non-zero exit) ----------


def _stub(phase: str, what: str):
    console.print(
        f"[yellow]not yet implemented[/yellow]: {what} (planned: {phase}). "
        "See README status banner."
    )
    raise SystemExit(2)


@cli.group("target")
@click.pass_context
def target_grp(ctx):
    """Track/untrack targets (Phase 1)."""


@target_grp.command("add")
@click.argument("path")
@click.option("--root", "root_opt", default=None, help="Root dir; stored paths are relative to it.")
@click.option(
    "--kind",
    "kind_opt",
    type=click.Choice(["text", "binary", "dir", "manifest", "auto"]),
    default="auto",
    show_default=True,
)
@click.option(
    "--flex",
    "flex_opt",
    type=click.Choice(["fixed", "user", "flexi", "auto"]),
    default="auto",
    show_default=True,
)
@click.option(
    "--interest",
    default="state",
    type=click.Choice(["state", "diff"]),
    show_default=True,
    help="state = whole snapshot; diff = what changed (v1: diff previews "
    "diff, deploys state; true patch-apply is v2).",
)
@click.option("--glob", "glob_pat", default="", help="Glob pattern expanding to tracked set.")
@click.option(
    "--auto-add-glob",
    default="",
    help="Dir auto-add glob (e.g. '*.txt'); '' = disabled. "
    "Untracked matches are candidates for watch --auto-add.",
)
@click.option("--ignore", "ignore_opts", multiple=True, help="Ignore pattern (repeatable).")
@click.option(
    "--symlink", default="preserve", type=click.Choice(["preserve", "follow"]), show_default=True
)
@click.option("--machines", default="", help="Comma-separated hostname allowlist (empty = all).")
@click.option(
    "--retention",
    "retention_shorthand",
    type=int,
    default=None,
    help="Shorthand for --retention-count.",
)
@click.option("--retention-count", type=int, default=None)
@click.option("--retention-age", default=None, help="E.g. 30d.")
@click.option("--template/--no-template", default=False, show_default=True)
@click.option("--on-deploy", default="", help="Post-deploy hook command.")
@click.option("--deploy-path", default="", help="Destination for flexi targets.")
@click.option("--check-interval", default="", help="Per-target interval override.")
@click.option(
    "--encrypt/--no-encrypt",
    default=False,
    show_default=True,
    help="Encrypt store artifact via sops/age (warn-only when sops absent).",
)
@click.option(
    "--auto-commit/--no-auto-commit",
    "target_auto_commit",
    default=False,
    show_default=True,
    help="Per-target auto-commit opt-in (daemon commits drift silently). "
    "Requires --retention-count N (limited revisions kept).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Allow tracking a whole wine prefix dir (escape hatch; manifest-only is default).",
)
@click.pass_context
def target_add(
    ctx,
    path,
    root_opt,
    kind_opt,
    flex_opt,
    interest,
    glob_pat,
    auto_add_glob,
    ignore_opts,
    symlink,
    machines,
    retention_shorthand,
    retention_count,
    retention_age,
    template,
    on_deploy,
    deploy_path,
    check_interval,
    encrypt,
    target_auto_commit,
    force,
):
    """Start tracking PATH from now (baseline + commit, no backfill)."""
    import shutil as _shutil

    from versioneer.core import elevate as _elev
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
                "(multi-root configs not supported in v1)"
            )
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
    if target_auto_commit and not cfg.has_limited_retention(retention):
        raise click.ClickException(
            "--auto-commit requires limited retention: "
            "pass --retention-count N (e.g. --retention-count 30) "
            "so auto-committed revisions stay bounded"
        )

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
            raise click.ClickException(
                "kind=manifest is added via `manifest` (Phase 8), not target add"
            )
        if flex == "user" and effective_root:
            raise click.ClickException("root must not be set for user targets (flex=user)")

        # stat / symlink state.
        # NOTE: Path.exists() returns False for BOTH missing and
        # permission-denied paths, which misled users into thinking a
        # root-owned file "does not exist". Use lstat classification so
        # EACCES/EPERM reports elevation instead of a bogus missing error.
        # Sudo note: `sudo vers` fails ( ~/.local/bin not in secure_path ),
        # so this command elevates only the *read* via `sudo cat/stat`
        # and keeps TOML + store owned by the user.
        try:
            is_link = abs_path.is_symlink()
        except OSError:
            is_link = False
        if is_link:
            try:
                dangling = not abs_path.exists()
            except OSError:
                dangling = True
            missing = False
        else:
            dangling = False
            state = _elev.classify_path(abs_path)
            if state == "missing":
                hint = ""
                try:
                    similar = _elev.suggest_similar(abs_path)
                    if similar:
                        hint = f" (did you mean: {', '.join(str(abs_path.parent / s) for s in similar)}?)"
                except (OSError, ValueError):
                    pass
                raise click.ClickException(f"path does not exist: {abs_path}{hint}")
            if state == "denied" and _elev.sudo_cmd() is None:
                raise click.ClickException(
                    f"cannot read {abs_path}: permission denied (sudo not found)"
                )
            # else: fall through — capture/hash/stage below retry via sudo
            # (password prompt once; TOML + store stay owned by you).
            missing = False
        if missing:
            raise click.ClickException(f"path does not exist: {abs_path}")
        if dangling and symlink == "follow":
            console.print(f"[yellow]warn[/yellow]: dangling symlink {abs_path} (follow mode)")
        elif dangling:
            console.print(f"[yellow]warn[/yellow]: dangling symlink {abs_path}")

        # Wine full-prefix escape hatch: manifest-only is the default.
        # A dir that looks like a wine prefix requires --force; the preset
        # ignores apply in either case (also for wine-adjacent files).
        looks_wine = _mon.is_wine_prefix(abs_path)
        if kind == "dir" and looks_wine and not force:
            raise click.ClickException(
                f"refusing to track wine prefix {abs_path} as a full dir "
                "(manifest-only by default): use `versioneer -C "
                f"{name} manifest --wine` + selective *.reg/*.cfg targets "
                "instead, or re-run with --force to acknowledge the "
                "size/quota cost (see docs: wine-manifest)"
            )
        if kind == "dir" and looks_wine and force:
            console.print(
                "[yellow]warn[/yellow]: tracking whole wine prefix "
                f"{abs_path} (--force): prefer `manifest --wine` + "
                "selective *.reg/*.cfg; default excludes applied, "
                "large-file warning applies"
            )

        eff_ignore = _mon.wine_preset_ignores(abs_path, ignore_list)
        follow = symlink == "follow"
        elevated = False
        try:
            owner, group, mode = _perm.capture(abs_path, follow=follow)
        except OSError:
            # Root-owned file: retry metadata via sudo stat (keeps TOML
            # owned by the user; no `sudo vers` needed).
            try:
                owner, group, mode = _elev.stat_via_sudo(abs_path, follow=follow)
                elevated = True
                console.print(f"[yellow]warn[/yellow]: elevated read via sudo: {abs_path}")
            except OSError as e2:
                hint = ""
                if _elev.sudo_cmd() is None:
                    hint = " (sudo not found)"
                else:
                    hint = (
                        " — if this is a root-owned file, ensure your user "
                        "has sudo access (you will be prompted)"
                    )
                raise click.ClickException(f"cannot stat {abs_path}: {e2}{hint}")
        try:
            digest = _mon.hash_target(abs_path, kind, symlink, eff_ignore)
        except OSError:
            digest = "read-error"
        if digest in ("missing", "read-error") and kind in ("text", "binary"):
            # Retry content hash via `sudo cat` for root-owned files.
            try:
                digest = _elev.hash_file_elevated(abs_path, kind)
                if not elevated:
                    console.print(f"[yellow]warn[/yellow]: elevated read via sudo: {abs_path}")
                elevated = True
            except OSError as e:
                if kind == "dir":
                    raise click.ClickException(
                        f"cannot read dir {abs_path}: {e} — run with a user "
                        "that can read it (sudo access)"
                    )
                raise click.ClickException(
                    f"cannot read {abs_path}: {e} — ensure sudo access "
                    "(you will be prompted for your password)"
                )

        # warn-only lints (use elevated bytes when direct read was denied)
        lint_data: bytes | None = None
        if elevated and kind == "text" and not dangling:
            try:
                lint_data = _elev.read_bytes_via_sudo(abs_path)
            except OSError:
                lint_data = None
        if kind == "text" and not dangling and abs_path.is_file():
            shown = False
            for w in _lint.scan_file(abs_path):
                console.print(f"[yellow]warn[/yellow]: {w}")
                shown = True
            for w in _lint.hardcoded_path_warnings(abs_path):
                console.print(f"[yellow]warn[/yellow]: {w}")
                shown = True
            if not shown and lint_data is not None:
                for w in _lint.scan_bytes(lint_data):
                    console.print(f"[yellow]warn[/yellow]: {w}")
                import re as _re

                if _re.search(rb"/home/[^/\s:'\"]+", lint_data):
                    console.print(
                        "[yellow]warn[/yellow]: hardcoded path: /home/... found "
                        "— consider --template (warn-only in v1)"
                    )
        if kind == "binary" and abs_path.is_file():
            try:
                size_mb = abs_path.stat().st_size / (1024 * 1024)
            except OSError:
                size_mb = 0
            warn_mb = config.meta.large_file_warn_mb or 10
            if size_mb > warn_mb:
                console.print(
                    f"[yellow]warn[/yellow]: large file {size_mb:.1f} MB > {warn_mb} MB — "
                    "uses git-lfs (warn-only)"
                )

        target = cfg.Target(
            path=stored_path,
            abs_path=str(abs_path),
            kind=kind,
            glob=glob_stored,
            ignore=eff_ignore,
            symlink=symlink,
            flex=flex,
            interest=interest,
            deploy_path=deploy_path,
            owner=owner,
            group=group,
            mode=mode,
            hash=digest,
            machines=machines_list,
            check_interval=check_interval,
            retention=retention,
            template=template,
            on_deploy=on_deploy,
            encrypt=bool(encrypt or config.meta.encrypt),
            auto_add_glob=auto_add_glob or "",
            auto_commit=bool(target_auto_commit),
        )
        errors = target.validate(config.meta.root)
        if errors:
            raise click.ClickException("; ".join(errors))
        if bool(target_auto_commit):
            console.print(
                f"[yellow]warn[/yellow]: {stored_path}: {cfg.AUTO_COMMIT_SPACE_WARNING}"
            )

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
                    src_top,
                    dest,
                    symlinks=(not follow),
                    ignore=lambda d, names, _top=src_top, _ig=tuple(eff_ignore): [
                        n
                        for n in names
                        if n == ".git"
                        or _mon.matches_ignore(
                            (
                                (Path(d).relative_to(_top).as_posix() + "/" + n)
                                if Path(d) != _top
                                else n
                            ),
                            list(_ig),
                        )
                    ],
                )
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    _shutil.copy2(abs_path, dest)
                except OSError as e:
                    import errno as _errno

                    if e.errno in (_errno.EACCES, _errno.EPERM) and kind in (
                        "text",
                        "binary",
                    ):
                        # Root-owned file: stage via `sudo cat` so the store
                        # copy stays owned by the user. No `sudo vers` needed
                        # (~/.local/bin is not in sudo secure_path).
                        try:
                            data = _elev.read_bytes_via_sudo(abs_path)
                        except OSError as e2:
                            raise click.ClickException(
                                f"cannot stage artifact for {abs_path}: {e2} — "
                                "ensure sudo access (you will be prompted)"
                            )
                        try:
                            dest.write_bytes(data)
                        except OSError as e3:
                            raise click.ClickException(
                                f"cannot stage artifact for {abs_path}: {e3}"
                            )
                        try:
                            _shutil.copystat(abs_path, dest, follow_symlinks=False)
                        except OSError:
                            pass  # perms come from TOML baseline
                        if not elevated:
                            console.print(
                                f"[yellow]warn[/yellow]: elevated read via sudo: {abs_path}"
                            )
                    else:
                        raise
        except OSError as e:
            msg = f"cannot stage artifact for {abs_path}: {e}"
            if "Permission denied" in str(e) or "Operation not permitted" in str(e):
                if _elev.sudo_cmd() is None:
                    msg += " (sudo not found)"
                else:
                    missing_shims = _elev.system_shim_missing()
                    msg += (
                        " — root-owned file? Just re-run the same command "
                        "as your user (you will be prompted for your sudo "
                        "password; no `sudo vers` needed)"
                    )
                    if missing_shims:
                        msg += (
                            f". Note: `sudo vers` fails (command not found) "
                            f"because ~/.local/bin is not in sudo secure_path; "
                            f"re-run ./installer/install.sh to create "
                            f"{' and '.join(missing_shims)} for full-root runs "
                            f"(shims are installed by default)"
                        )
            raise click.ClickException(msg)

        if kind == "binary":
            warn = _store.ensure_lfs(store, [rel.as_posix()])
            if warn:
                console.print(f"[yellow]warn[/yellow]: {warn}")

        # ITEM 2: encrypt in store when per-target/config encrypt flag set.
        # Warn-only when sops/age absent — never blocks tracking.
        if bool(getattr(target, "encrypt", False)):
            from versioneer.core import secrets as _sec

            for w in _sec.encrypt_store_artifact(dest, kind):
                console.print(f"[yellow]warn[/yellow]: {stored_path}: {w}")

        config.targets.append(target)
        added += 1
        console.print(
            f"[green]tracking[/green] {stored_path} (kind={kind} flex={flex} hash={digest[:19]}…)"
        )

    try:
        cfg.save(config)
    except ValueError as e:
        raise click.ClickException(str(e))
    # Store-side TOML snapshot so `bootstrap <url>` recreates exact targets
    # on a fresh machine (warn-only: tracking must succeed regardless).
    try:
        cfg.export_snapshot(config, store)
    except (ValueError, OSError) as e:
        console.print(f"[yellow]warn[/yellow]: snapshot export skipped: {e}")
    rels: list[str] = []
    for t in config.targets[-added:]:
        assert isinstance(t, cfg.Target)
        if effective_root:
            rels.append(Path(t.path).as_posix())
        else:
            rels.append(_mon.artifact_rel("", Path(t.abs_path), t.flex, "").as_posix())
    if kind_opt == "binary" or any(
        isinstance(t, cfg.Target) and t.kind == "binary" for t in config.targets[-added:]
    ):
        ga_warn = _store.ensure_lfs(store, rels)
        if ga_warn:
            console.print(f"[yellow]warn[/yellow]: {ga_warn}")
    # .gitattributes is created by ensure_lfs at config create time (and by
    # ensure_lfs above for binaries). Stage it whenever present so it never
    # lingers as untracked dirt that pollutes future commits.
    if (store / ".gitattributes").exists() and ".gitattributes" not in rels:
        rels = rels + [".gitattributes"]
    if (store / cfg.SNAPSHOT_NAME).exists() and cfg.SNAPSHOT_NAME not in rels:
        rels = rels + [cfg.SNAPSHOT_NAME]
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
    table.add_column("auto-commit")
    for t in config.targets:
        assert isinstance(t, cfg.Target)
        short = (t.hash[:19] + "…") if len(t.hash) > 19 else t.hash
        table.add_row(
            t.path,
            t.kind,
            t.flex,
            short,
            f"{t.owner}/{t.mode}",
            "yes" if bool(getattr(t, "auto_commit", False)) else "no",
        )
    console.print(table)


@target_grp.command("set")
@click.argument("path")
@click.option(
    "--auto-commit/--no-auto-commit",
    "target_auto_commit",
    default=None,
    help="Toggle per-target auto-commit (daemon commits drift silently). "
    "Enabling requires limited retention (--retention-count N).",
)
@click.option(
    "--retention",
    "retention_shorthand",
    type=int,
    default=None,
    help="Shorthand for --retention-count.",
)
@click.option("--retention-count", type=int, default=None)
@click.option("--retention-age", default=None, help="E.g. 30d.")
@click.option(
    "--clear-retention",
    is_flag=True,
    default=False,
    help="Clear the retention policy (refused while auto-commit is enabled).",
)
@click.pass_context
def target_set(
    ctx, path, target_auto_commit, retention_shorthand, retention_count, retention_age,
    clear_retention,
):
    """Toggle auto-commit / retention on an already tracked target.

    \b
    versioneer -C saves target set saves/slot1.sav --auto-commit --retention-count 30
    versioneer -C saves target set saves/slot1.sav --no-auto-commit
    """
    from versioneer.core import monitor as _mon
    from versioneer.core import store as _store

    name = _require_config(ctx)
    try:
        config = cfg.load(name)
    except FileNotFoundError:
        raise click.ClickException(f"config {name!r} not found in {cfg.config_dir()}")
    except ValueError as e:
        raise click.ClickException(str(e))
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

    if (
        target_auto_commit is None
        and retention_shorthand is None
        and retention_count is None
        and retention_age is None
        and not clear_retention
    ):
        raise click.ClickException(
            "nothing to change: pass --auto-commit/--no-auto-commit "
            "and/or --retention-count N [--retention-age AGE]"
        )
    if clear_retention and (
        retention_shorthand is not None or retention_count is not None or retention_age
    ):
        raise click.ClickException("pass either --clear-retention or retention values, not both")

    new_retention = dict(target.retention or {})
    if clear_retention:
        new_retention = {}
    else:
        count = retention_count if retention_count is not None else retention_shorthand
        if count is not None:
            new_retention["count"] = count
        if retention_age:
            new_retention["age"] = retention_age
    new_auto = bool(getattr(target, "auto_commit", False))
    if target_auto_commit is not None:
        new_auto = bool(target_auto_commit)

    if new_auto and not cfg.has_limited_retention(new_retention):
        raise click.ClickException(
            "--auto-commit requires limited retention: "
            "pass --retention-count N (e.g. --retention-count 30) "
            "so auto-committed revisions stay bounded"
        )
    if clear_retention and bool(getattr(target, "auto_commit", False)) and new_auto:
        raise click.ClickException(
            "cannot clear retention while auto-commit is enabled "
            "(pass --no-auto-commit together with --clear-retention)"
        )

    target.retention = new_retention
    target.auto_commit = new_auto
    errors = target.validate(config.meta.root)
    if errors:
        raise click.ClickException("; ".join(errors))
    try:
        cfg.save(config)
    except ValueError as e:
        raise click.ClickException(str(e))
    store = cfg.store_dir(config)
    try:
        cfg.export_snapshot(config, store)
        try:
            _store.add_and_commit(
                store, [cfg.SNAPSHOT_NAME], f"target set {target.path}"
            )
        except _store.GitError:
            pass
    except (ValueError, OSError) as e:
        console.print(f"[yellow]warn[/yellow]: snapshot refresh skipped: {e}")
    if target_auto_commit is True:
        console.print(
            f"[yellow]warn[/yellow]: {target.path}: {cfg.AUTO_COMMIT_SPACE_WARNING}"
        )
    console.print(
        f"[green]updated[/green] {target.path} "
        f"(auto_commit={'yes' if new_auto else 'no'}, retention={new_retention or '{}'})"
    )


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
        raise click.ClickException(f"untrack commit failed: {e} (TOML entry already removed)")
    # Keep the store-side snapshot in sync (warn-only).
    try:
        cfg.export_snapshot(config, store)
        _store.add_and_commit(store, [cfg.SNAPSHOT_NAME], f"untrack {target.path} (snapshot)")
    except (ValueError, OSError, _store.GitError) as e:
        console.print(f"[yellow]warn[/yellow]: snapshot refresh skipped: {e}")
    console.print(
        f"[yellow]untracked[/yellow] {target.path} (working file untouched, git history kept)"
    )


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
    colors = {
        "clean": "green",
        "modified": "yellow",
        "perm-drift": "magenta",
        "missing": "red",
        "untracked": "cyan",
        "read-error": "red",
    }
    from rich.markup import escape as _escape

    for r in results:
        t = r["target"]
        state = r["state"]
        detail = r["detail"]
        if r["host_skipped"]:
            detail = detail + " [wrong host]" if detail else "[wrong host]"
        table.add_row(
            _escape(t.path),
            f"[{colors.get(state, '')}]{state}[/]" if state in colors else _escape(state),
            _escape(detail),
        )
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

        console.print(
            f"[bold]{_escape(t.path)}[/bold]  {_escape(r['state'])}  {_escape(r['detail'])}"
        )
        if r["state"] in ("clean",):
            continue
        if r["state"] in ("missing", "read-error"):
            console.print(
                f"  [red]{_escape(r['state'])}[/red]: {_escape(r['detail'])} (no diff available)"
            )
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
            for line in difflib.unified_diff(
                head_text, live_text, f"store/{rel}", f"live/{t.path}", lineterm=""
            ):
                console.print(f"  {line}", markup=False, highlight=False)
        elif t.kind == "binary":
            try:
                size = r["abs_path"].stat().st_size if r["abs_path"].exists() else -1
            except OSError:
                size = -1
            console.print(
                f"  binary: {r['current_hash'][:19]}… size={size} bytes (baseline {t.hash[:19]}…)"
            )
        elif t.kind == "dir":
            if r["untracked"]:
                console.print(
                    f"  untracked ({len(r['untracked'])}): {', '.join(r['untracked'][:10])}"
                )
            live_top = r["abs_path"]
            try:
                follow_dir = t.symlink == "follow"
                live_resolved = live_top.resolve(strict=False) if follow_dir else live_top
            except OSError:
                live_resolved = live_top
            stored_top = store / rel
            try:
                cmp = _mon.compare_dir_trees(
                    live_resolved, stored_top, list(t.ignore or []), follow_dir
                )
            except OSError:
                cmp = {"changed": [], "untracked": list(r["untracked"] or []), "missing": []}
            # per-file lists (changed excludes pure untracked/missing sets)
            if cmp["changed"]:
                console.print(
                    f"  changed ({len(cmp['changed'])}): "
                    f"{', '.join(cmp['changed'][:20])}" + ("…" if len(cmp["changed"]) > 20 else "")
                )
            # untracked already printed above from scan; show extras beyond scan cap
            extra_untracked = [u for u in cmp["untracked"] if u not in (r["untracked"] or [])]
            if extra_untracked:
                console.print(
                    f"  untracked (more {len(extra_untracked)}): {', '.join(extra_untracked[:20])}"
                )
            if cmp["missing"]:
                console.print(
                    f"  missing ({len(cmp['missing'])}): {', '.join(cmp['missing'][:20])}"
                )
            try:
                n_live = len(_mon.iter_dir_files(live_resolved, list(t.ignore or [])))
            except OSError:
                n_live = 0
            console.print(
                f"  dir: {n_live} file(s) live, hash {r['current_hash'][:23]}… "
                f"(baseline {t.hash[:23]}…)"
            )
        else:  # manifest: unified diff of store artifact vs regenerated
            from versioneer.core import manifest as _mg

            mtype = _mg.manifest_kind_for_target(t)
            # locate store artifact bytes (HEAD first, then working-tree fallbacks)
            head = _store.show_head_file(store, rel)
            candidates: list = []
            try:
                from pathlib import Path as _P

                candidates = [
                    store / str(t.path or ""),
                    store / _P(str(t.path or "")).name,
                    store / rel,
                    r["abs_path"],
                ]
            except (OSError, ValueError):
                candidates = [r["abs_path"]]
            store_bytes = head
            if store_bytes is None:
                for cand in candidates:
                    try:
                        if cand and cand.is_file():
                            store_bytes = cand.read_bytes()
                            break
                    except OSError:
                        continue
            if store_bytes is None:
                console.print("  (no committed artifact yet — new file)")
                continue
            store_text = store_bytes.decode("utf-8", errors="replace").splitlines()
            if mtype not in _mg.GENERATORS:
                console.print(
                    "  manifest: regenerate on commit, replay on deploy "
                    f"(unknown subtype {mtype!r}; showing store preview)"
                )
                for line in store_text[:20]:
                    console.print(f"  {line}", markup=False, highlight=False)
                continue
            try:
                _fname, regen_text, replay = _mg.GENERATORS[mtype]()
            except (OSError, ValueError, RuntimeError) as e:  # best-effort, never crash diff
                console.print(f"  [red]cannot regenerate manifest: {e}[/red]")
                continue
            regen_lines = regen_text.splitlines()
            dlines = list(
                difflib.unified_diff(
                    store_text,
                    regen_lines,
                    f"store/{t.path}",
                    "regenerated",
                    lineterm="",
                )
            )
            if not dlines:
                console.print(f"  manifest {mtype}: in sync ({replay})")
                continue
            console.print(f"  manifest {mtype} diff ({replay}):")
            for line in dlines:
                console.print(f"  {line}", markup=False, highlight=False)


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
@click.option(
    "--prune-retention",
    is_flag=True,
    default=False,
    help="Opt-in: run local `git lfs prune` when retention "
    "exceeds (history kept by default, never squashes).",
)
@click.argument("targets", nargs=-1)
@click.pass_context
def commit(ctx, message, all_targets, targets, prune_retention):
    """Save drift: re-hash + update TOML baselines + git commit (offline-safe)."""
    from versioneer.core import elevate as _elev_c
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
            console.print(
                f"[red]error[/red]: {t.path} missing — cannot commit a deleted file "
                "(restore it or `target remove` it)"
            )
            skipped.append(f"{t.path} missing")
            continue
        if r["state"] == "read-error":
            # Root-owned target scanned as user: scan_one is read-only and
            # never prompts, so suggest the two supported elevation paths.
            # (commit below retries the read via sudo when possible.)
            detail = r["detail"]
            if _elev_c.sudo_cmd() is not None:
                # Retry the hash via sudo: a password prompt here is
                # acceptable (interactive commit), unlike daemon scans.
                try:
                    _elev_c.hash_file_elevated(r["abs_path"], t.kind)
                    console.print(
                        f"[yellow]warn[/yellow]: {t.path}: elevated read via sudo "
                        "(continuing commit)"
                    )
                except OSError:
                    console.print(
                        f"[red]error[/red]: {t.path} read-error — {detail} "
                        "(root-owned file? ensure sudo access, or run status "
                        "via the system unit / full-root CLI, see README §15)"
                    )
                    skipped.append(f"{t.path} read-error")
                    continue
            else:
                console.print(
                    f"[red]error[/red]: {t.path} read-error — {detail} "
                    "(sudo not found; run with a user that can read it)"
                )
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
                console.print(
                    f"[yellow]warn[/yellow]: {t.path}: large file {size_mb:.1f} MB > "
                    f"{warn_mb} MB — uses git-lfs (warn-only)"
                )
            # locked-file safe copy-then-hash notice
            try:
                _digest, copied = _mon.safe_sha256_file(abs_path.resolve(strict=False))
                if copied:
                    console.print(
                        f"[yellow]warn[/yellow]: {t.path}: locked file? copied-then-hashed"
                    )
            except OSError:
                pass
        try:
            _stage_artifact(abs_path, t.kind, t.symlink, list(t.ignore or []), dest)
        except OSError as e:
            import errno as _errno_c

            if (
                e.errno in (_errno_c.EACCES, _errno_c.EPERM)
                and t.kind in ("text", "binary")
                and _elev_c.sudo_cmd() is not None
            ):
                try:
                    data = _elev_c.read_bytes_via_sudo(abs_path)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)
                    console.print(f"[yellow]warn[/yellow]: {t.path}: elevated read via sudo")
                except OSError as e2:
                    console.print(f"[red]error[/red]: {t.path}: cannot stage artifact: {e2}")
                    skipped.append(f"{t.path} stage-error")
                    continue
            else:
                console.print(f"[red]error[/red]: {t.path}: cannot stage artifact: {e}")
                skipped.append(f"{t.path} stage-error")
                continue
        if t.kind == "binary":
            warn = _store.ensure_lfs(store, [rel.as_posix()])
            if warn:
                console.print(f"[yellow]warn[/yellow]: {warn}")
        # ITEM 2: encrypt staged artifact when per-target/config encrypt set.
        # Warn-only when sops/age absent — never blocks commit.
        if bool(getattr(t, "encrypt", False) or config.meta.encrypt):
            from versioneer.core import secrets as _sec

            for w in _sec.encrypt_store_artifact(dest, t.kind):
                console.print(f"[yellow]warn[/yellow]: {t.path}: {w}")
        # refresh baseline from live state (sudo retry for root-owned files)
        follow = t.symlink == "follow"
        try:
            owner, group, mode = _perm.capture(abs_path, follow=follow)
        except OSError:
            try:
                owner, group, mode = _elev_c.stat_via_sudo(abs_path, follow=follow)
            except OSError as e:
                console.print(f"[red]error[/red]: {t.path}: cannot stat after stage: {e}")
                skipped.append(f"{t.path} stat-error")
                continue
        t.owner, t.group, t.mode = owner, group, mode
        try:
            t.hash = _mon.hash_target(abs_path, t.kind, t.symlink, list(t.ignore or []))
        except OSError:
            t.hash = "read-error"
        if t.hash in ("missing", "read-error") and t.kind in ("text", "binary"):
            try:
                t.hash = _elev_c.hash_file_elevated(abs_path, t.kind)
            except OSError as e:
                console.print(f"[red]error[/red]: {t.path}: cannot hash after stage: {e}")
                skipped.append(f"{t.path} hash-error")
                continue
        rels.append(rel.as_posix())
        committed.append(t.path)
        # retention advisory (non-destructive, Q3 decision c: warn by default,
        # opt-in prune via --prune-retention; never silent squash so
        # plan.json hashes + remove history stay intact).
        if t.retention:
            rel_posix = rel.as_posix()
            n = _store.count_artifact_commits(store, rel_posix)
            oldest: int | None = None
            if _store.parse_retention_age(t.retention.get("age")) is not None:
                oldest = _store.oldest_artifact_commit_time(store, rel_posix)
            warn_r = _store.retention_warning(n + 1, t.retention, oldest)
            if warn_r:
                console.print(f"[yellow]warn[/yellow]: {t.path}: {warn_r}")
                console.print(
                    f"  {_store.prune_guidance(rel_posix, t.retention)}"
                    + (
                        ""
                        if prune_retention
                        else " (pass --prune-retention for local `git lfs prune`)"
                    )
                )

    if not committed:
        console.print(
            "clean — nothing to commit"
            if all("clean" in s for s in skipped)
            else "nothing committed (see errors above)"
        )
        return
    if any(
        isinstance(t, cfg.Target) and t.kind == "binary" for t in selected if t.path in committed
    ):
        ga_warn = _store.ensure_lfs(store, rels)
        if ga_warn:
            console.print(f"[yellow]warn[/yellow]: {ga_warn}")
    if (store / ".gitattributes").exists() and ".gitattributes" not in rels:
        rels = rels + [".gitattributes"]
    try:
        cfg.save(config)
    except ValueError as e:
        raise click.ClickException(str(e))
    try:
        cfg.export_snapshot(config, store)
    except (ValueError, OSError) as e:
        console.print(f"[yellow]warn[/yellow]: snapshot export skipped: {e}")
    if (store / cfg.SNAPSHOT_NAME).exists() and cfg.SNAPSHOT_NAME not in rels:
        rels = rels + [cfg.SNAPSHOT_NAME]
    msg = (
        message or f"update {committed[0] if len(committed) == 1 else f'{len(committed)} targets'}"
    )
    try:
        sha = _store.add_and_commit(store, rels, msg)
    except _store.GitError as e:
        raise click.ClickException(f"commit failed: {e}")
    if sha:
        console.print(
            f"[green]committed[/green] {len(committed)} target(s) ({sha[:7]}): "
            f"{', '.join(committed)}"
        )
    else:
        # Store content already in sync (e.g. perm-only change is invisible
        # to git, which tracks only the exec bit): TOML baselines above were
        # still updated via cfg.save, so report success, not "clean".
        console.print(
            f"[green]updated baselines[/green] for {len(committed)} target(s) "
            f"(store content unchanged): {', '.join(committed)}"
        )
    if prune_retention:
        # Opt-in prune only: local `git lfs prune`, never history rewrite,
        # so plan.json hashes stay valid. Warn-only per target.
        for t in selected:
            assert isinstance(t, cfg.Target)
            if t.path not in committed or not t.retention:
                continue
            from versioneer.core import monitor as _mon2

            abs_p = _mon2.live_abs_path(t.path, t.abs_path, config.meta.root or "")
            rel_p = _mon2.store_rel_for(t.path, abs_p, t.flex, config.meta.root or "").as_posix()
            if _store.check_retention(store, rel_p, t.retention):
                result = _store.prune_retention(store, rel_p, t.retention)
                console.print(f"[yellow]prune[/yellow]: {t.path}: {result}")
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
@click.option(
    "--host", "host_opt", default=None, help="Simulate a different hostname (machines filter)."
)
@click.option("--prune", is_flag=True)
@click.option(
    "--apply",
    is_flag=True,
    help="Apply manifests where safe (packages: sudo pacman -S "
    "--needed; systemd: systemctl enable; wine: write "
    "setup-wine.sh, never auto-runs winetricks; env: print-only). "
    "Default is print-only replay.",
)
@click.pass_context
def deploy(
    ctx,
    targets,
    dry_run,
    plan_out,
    plan_in,
    to_path,
    yes,
    no_interaction,
    force_host,
    host_opt,
    prune,
    apply,
):
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
            dest = _dep.live_dest(t, config.meta.root or "", to_path or "", multi)
            entries.append(_dep.plan_entry(t, config.meta.root or "", store, dest))
        try:
            with open(plan_out, "w", encoding="utf-8") as f:
                _json.dump(entries, f, indent=2)
        except OSError as e:
            raise click.ClickException(f"cannot write --plan-out {plan_out!r}: {e}")
        console.print(f"[green]wrote plan[/green] {plan_out} ({len(entries)} entries)")
        for e in entries:
            console.print(
                f"  {e['target']}: {e['action']} "
                f"(src={str(e['src_hash'])[:19]}… dst={str(e['dst_hash'])[:19]}…)"
            )
        return

    results = []
    for t in selected:
        assert isinstance(t, cfg.Target)
        r = _dep.deploy_one(
            t,
            config,
            store,
            to_path or "",
            multi,
            assume_yes,
            prune,
            bool(apply),
            bool(force_host),
            host_opt or "",
            bool(dry_run),
            plan_map,
        )
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
        table.add_row(
            _escape(str(r.get("target", ""))),
            f"[{colors.get(st, '')}]{_escape(st)}[/]" if st in colors else _escape(st),
            _escape(str(reason)),
        )
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
@click.option(
    "--enable/--no-enable",
    "do_enable",
    default=True,
    show_default=True,
    help="Also daemon-reload + enable --now the user timer (warn-only).",
)
def service_install(do_enable):
    """Install user + system units and timers (no sudo needed).

    Warn-only preflight: git-lfs check, linger hint, completions check,
    sops/age optional check. Writes are idempotent; timer enable never fails
    the install (use `service enable` to retry verbosely).
    """
    from versioneer.core import daemon as _daemon

    # 1. Preflight (warn-only, never blocks the install).
    for w in _daemon.prereq_warnings():
        console.print(f"[yellow]warn[/yellow]: {w}")
    linger = _daemon.is_linger_enabled()
    if linger is False:
        console.print(f"[yellow]warn[/yellow]: linger not enabled — run: {_daemon.linger_hint()}")
    else:
        console.print(f"linger: {_daemon.linger_hint()}")
    missing = _daemon.missing_completions()
    if missing:
        console.print(
            "[yellow]warn[/yellow]: shell completions missing: "
            + ", ".join(str(p) for p in missing)
            + " (run ./installer/install.sh or copy installer/completions/*)"
        )

    # 2. Write user units + stage system units (idempotent overwrite).
    try:
        upath, tpath = _daemon.write_user_units()
    except OSError as e:
        raise click.ClickException(f"cannot write {_daemon.user_unit_path()}: {e}")
    from versioneer.core import deploy as _dep

    try:
        sys_staged, sys_timer_staged = _daemon.stage_system_units(_dep.state_dir())
    except OSError as e:
        raise click.ClickException(f"cannot stage system unit: {e}")
    console.print(f"[green]installed[/green] {upath}")
    console.print(f"[green]installed[/green] {tpath}")
    console.print(
        f"system units staged at {sys_staged} + {sys_timer_staged.name} — "
        "install with: "
        f"sudo cp {sys_staged.name} {sys_timer_staged.name} "
        "/etc/systemd/system/ && sudo systemctl daemon-reload"
    )
    try:
        from versioneer.core import elevate as _elev_svc

        _missing_shims = _elev_svc.system_shim_missing()
    except ImportError:
        _missing_shims = []
    if _missing_shims:
        console.print(
            "[yellow]warn[/yellow]: system unit ExecStart=/usr/local/bin/versioneer "
            f"is missing ({', '.join(_missing_shims)}). "
            "`sudo vers` will also fail (command not found: ~/.local/bin "
            "is not in sudo secure_path). Fix: "
            "re-run ./installer/install.sh (shims are installed by default). "
            "Tracking root-owned files does NOT need `sudo vers`: "
            "run `vers target add /etc/...` as your user (sudo read is automatic)."
        )

    # 3. Write+enable timers: daemon-reload + enable --now (warn-only).
    console.print(
        f"next: systemctl --user enable --now {_daemon.USER_TIMER_NAME} "
        f"(system: sudo systemctl enable --now {_daemon.SYSTEM_TIMER_NAME})"
    )
    if do_enable:
        ok, detail = _daemon.try_reload_and_enable()
        if ok:
            console.print(f"[green]enabled[/green] {_daemon.USER_TIMER_NAME}")
        else:
            console.print(
                f"[yellow]warn[/yellow]: timer enable skipped: {detail} "
                f"(run `versioneer service enable` to retry)"
            )


@service_grp.command("enable")
def service_enable():
    """Enable/start user timer (and print system-unit hint)."""
    import shutil as _shutil
    import subprocess as _sp

    from versioneer.core import daemon as _daemon

    sys = _shutil.which("systemctl")
    if not sys:
        raise click.ClickException("systemctl not found (non-systemd machine?)")
    try:
        _sp.run([sys, "--user", "daemon-reload"], check=False, capture_output=True, timeout=30)
        p = _sp.run(
            [sys, "--user", "enable", "--now", _daemon.USER_TIMER_NAME],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, _sp.TimeoutExpired) as e:
        raise click.ClickException(f"systemctl failed: {e}")
    if p.returncode != 0:
        raise click.ClickException(f"systemctl enable failed: {(p.stderr or p.stdout).strip()}")
    console.print(
        f"[green]enabled[/green] {_daemon.USER_TIMER_NAME} "
        f"(system: sudo systemctl enable --now {_daemon.SYSTEM_TIMER_NAME})"
    )


@service_grp.command("disable")
def service_disable():
    """Disable/stop user timer and service."""
    import shutil as _shutil
    import subprocess as _sp

    from versioneer.core import daemon as _daemon

    sys = _shutil.which("systemctl")
    if not sys:
        raise click.ClickException("systemctl not found (non-systemd machine?)")
    try:
        p = _sp.run(
            [sys, "--user", "disable", "--now", _daemon.USER_TIMER_NAME, _daemon.USER_SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, _sp.TimeoutExpired) as e:
        raise click.ClickException(f"systemctl failed: {e}")
    if p.returncode != 0:
        raise click.ClickException(f"systemctl disable failed: {(p.stderr or p.stdout).strip()}")
    console.print(
        f"[yellow]disabled[/yellow] {_daemon.USER_TIMER_NAME} + {_daemon.USER_SERVICE_NAME}"
    )


@service_grp.command("run")
@click.option("--once", is_flag=True, help="Single pass over all configs, then exit.")
@click.option(
    "--host", "host_opt", default=None, help="Simulate a different hostname (machines filter)."
)
def service_run(once, host_opt):
    """Run the daemon loop (systemd ExecStart target; --once for timers)."""
    from versioneer.core import daemon as _daemon

    _daemon.run_loop(host_opt or "", once=bool(once))
    if once:
        console.print("run complete (once)")


@service_grp.command("check")
@click.option(
    "--all", "all_configs", is_flag=True, help="Check all configs (default: -C config or all)."
)
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
            console.print(
                f"{r['config']}: {n} drifted "
                f"{'(notified)' if r['notified'] else ''}"
                f"{'(auto-committed)' if r['committed'] else ''}"
            )
            for d in r["drift"][:10]:
                console.print(f"  {d['target']} {d['state']}")
            for w in r.get("warnings", []):
                console.print(f"[yellow]warn[/yellow]: {w}")
            for e in r.get("errors", []):
                console.print(f"[red]error[/red]: {e}")
        return
    r = _daemon.check_once(name, host or "")
    console.print(f"{name}: {len(r['drift'])} drifted")
    for d in r["drift"][:20]:
        console.print(f"  {d['target']} {d['state']}: {d['detail']}")
    for w in r.get("warnings", []):
        console.print(f"[yellow]warn[/yellow]: {w}")
    for e in r.get("errors", []):
        console.print(f"[red]error[/red]: {e}")


def _infer_targets_from_store(store_path: Path) -> list:
    """Best-effort TOML reconstruction for snapshot-less stores.

    Adopts each store-relative file as a text/binary target (kinds
    collapsed, root unknown -> no root, flex user by default). Symlinks
    are preserved. Directories become their files (no dir-target merge).
    """
    from versioneer.core import monitor as _mon
    from versioneer.core import permissions as _perm

    skip_top = {".git", ".gitattributes", ".gitignore", cfg.SNAPSHOT_NAME, ".versioneer-keep"}
    targets: list = []
    try:
        entries = sorted(store_path.rglob("*"))
    except OSError:
        return targets
    for p in entries:
        try:
            rel = p.relative_to(store_path)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] in (".git",):
            continue
        if rel.name in skip_top:
            continue  # versioneer-internal files, at any depth
        is_link = p.is_symlink()
        try:
            if p.is_dir() and not is_link:
                continue
            if not (p.is_file() or is_link):
                continue
        except OSError:
            continue
        rel_posix = rel.as_posix()
        # Best-effort flex: keep an existing absolute destination fixed,
        # else fall back to user (rewritten to current $HOME on deploy).
        if (Path("/" + rel_posix)).exists():
            flex, live = "fixed", Path("/" + rel_posix)
        else:
            from versioneer.core import elevate as _elev_i

            flex, live = "user", _elev_i.effective_home() / rel_posix
        kind, sym = "text", "preserve"
        if not is_link:
            try:
                if _mon.looks_binary_file(p):
                    kind = "binary"
            except OSError:
                pass
        try:
            owner, group, mode = _perm.capture(p, follow=False)
        except OSError:
            owner, group, mode = "", "", "0644"
        try:
            digest = _mon.hash_target(p, kind, sym, [])
        except OSError:
            digest = ""
        targets.append(
            cfg.Target(
                path=str(live),
                abs_path=str(live),
                kind=kind,
                flex=flex,
                interest="state",
                symlink=sym,
                owner=owner,
                group=group,
                mode=mode,
                hash=digest,
            )
        )
    return targets


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
        raise click.ClickException(
            "usage: versioneer bootstrap <upstream-url> [--to DIR] | versioneer bootstrap --all"
        )
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
            r = _dep.deploy_one(
                t,
                conf,
                store,
                "",
                len(conf.targets) > 1,
                bool(yes),
                False,
                False,
                False,
                host or "",
                bool(dry_run),
                None,
            )
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

    from versioneer.core import elevate as _elev_b

    raw_store = to_dir or f"~/versioneer-store/{cname}"
    store_path = _elev_b.expand_user(_os.path.expandvars(raw_store))
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
    # New-machine restore: recreate TOML(s) then deploy --all.
    # 1) Exact path: store-side snapshot written by target add/commit.
    snap = cfg.load_snapshot(store_path, cname)
    if snap is not None:
        meta = cfg.Meta(
            name=cname,
            upstream=upstream,
            storage=str(store_path),
            root=snap.meta.root,
            notify=snap.meta.notify,
            auto_commit=snap.meta.auto_commit,
            auto_push=snap.meta.auto_push,
            check_interval=snap.meta.check_interval or "3h",
            encrypt=snap.meta.encrypt,
            large_file_warn_mb=snap.meta.large_file_warn_mb,
        )
        rebuilt = cfg.Config(meta=meta, targets=snap.targets)
        try:
            saved = cfg.save(rebuilt)
        except ValueError as e:
            raise click.ClickException(str(e))
        console.print(
            f"[green]recreated[/green] {saved} from store snapshot "
            f"({len(rebuilt.targets)} target(s))"
        )
    else:
        # 2) Best-effort fallback for snapshot-less stores: adopt
        # store-relative files as text/user targets.
        targets = _infer_targets_from_store(store_path)
        meta = cfg.Meta(name=cname, upstream=upstream, storage=str(store_path))
        errs = meta.validate()
        if errs:
            raise click.ClickException("; ".join(errs))
        rebuilt = cfg.Config(meta=meta, targets=targets)
        try:
            saved = cfg.save(rebuilt)
        except ValueError as e:
            raise click.ClickException(str(e))
        if targets:
            console.print(
                f"[green]created[/green] {saved} + store {store_path} "
                f"(adopted {len(targets)} file(s) as text/user "
                "targets, best-effort: kinds collapsed, "
                "root/flexi destinations need review)"
            )
        else:
            console.print(f"[green]created[/green] {saved} + store {store_path}")
    # Deploy whatever the TOML now declares (per-target ok|skipped|error;
    # status file written unless --dry-run). Empty stores exit success.
    conf = cfg.load(cname)
    if not conf.targets:
        console.print("no targets declared yet — nothing to deploy")
        return
    _deploy_config(cname)


@cli.command("watch")
@click.argument("directory")
@click.option("--auto-add", is_flag=True)
@click.option(
    "--root", "root_opt", default=None, help="Root dir; added targets are stored relative to it."
)
@click.option("--ignore", "ignore_opts", multiple=True, help="Ignore pattern (repeatable).")
@click.option("--glob", "glob_pat", default="", help="Glob stored on added targets.")
@click.option(
    "--timeout",
    "timeout_s",
    type=int,
    default=None,
    help="Stop watching after N seconds (tests/CI).",
)
@click.pass_context
def watch(ctx, directory, auto_add, root_opt, ignore_opts, glob_pat, timeout_s):
    """Capture changed files (snapshot, wait Ctrl-C, add)."""
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
    if timeout_s is None:
        import os as _os

        try:
            timeout_s = int(_os.environ.get("VERSIONEER_WATCH_TIMEOUT", "0") or 0)
        except ValueError:
            timeout_s = 0
    timeout_s = timeout_s or 0
    backend = "watchdog/inotify" if _watch.watchdog_available() else "polling"
    console.print(
        f"watching {top} ({backend}, Ctrl-C to finish"
        f"{f', timeout {timeout_s}s' if timeout_s else ''}) — make changes"
    )
    before, after = _watch.watch_dir(top, ignore, timeout_s=timeout_s)
    diff = _watch.diff_snapshots(before, after)
    changed = diff["changed"] + diff["added"]
    if not changed:
        console.print("no changes detected")
        if diff["removed"]:
            console.print(f"removed ({len(diff['removed'])}): {', '.join(diff['removed'][:10])}")
        return
    console.print(f"changed ({len(changed)}): {', '.join(changed[:20])}")
    if diff["removed"]:
        console.print(f"removed ({len(diff['removed'])}): {', '.join(diff['removed'][:10])}")
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

                        _stored, _abs = _mm.resolve_input(full, _conf.meta.root or "")
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
        raise click.ClickException(
            "nothing selected: pass at least one of --packages --wine --systemd --env"
        )
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
            config.targets.append(
                cfg.Target(
                    path=fname,
                    abs_path=str(dest),
                    kind="manifest",
                    flex="fixed",
                    interest="state",
                    owner=owner,
                    group=group,
                    mode=mode,
                    hash=digest,
                    on_deploy="",
                    manifest={
                        "type": kind,
                        "source": f"builtin:{kind}",
                        "output": fname,
                    },
                )
            )
        else:
            existing.hash = _mon.hash_target(dest, "text", "preserve", [])
            existing.manifest = {
                "type": kind,
                "source": f"builtin:{kind}",
                "output": fname,
            }
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
        cfg.export_snapshot(config, store)
    except (ValueError, OSError) as e:
        console.print(f"[yellow]warn[/yellow]: snapshot export skipped: {e}")
    if (store / cfg.SNAPSHOT_NAME).exists() and cfg.SNAPSHOT_NAME not in rels:
        rels = rels + [cfg.SNAPSHOT_NAME]
    try:
        sha = _store.add_and_commit(store, rels, f"manifest {'+'.join(wanted)}")
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
            console.print("[yellow]warn[/yellow]: git-lfs not found (sudo pacman -S git-lfs)")
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
        console.print(
            f"[bold]{cname}[/bold] storage={store} upstream={conf.meta.upstream or '(none)'}"
        )
        if not _store.lfs_available():
            console.print("  [yellow]warn[/yellow]: git-lfs not found (binary targets need it)")
            warnings += 1
        if not _store.is_repo(store):
            console.print(f"  [red]error[/red]: store is not a git repo: {store}")
            errors += 1
        elif conf.meta.upstream and not _store.get_upstream(store):
            console.print("  [yellow]warn[/yellow]: upstream not configured in store")
            warnings += 1
        # Upstream reachability/auth via ls-remote (warn-only, never crashes
        # offline). Timeouts are short so doctor stays fast on bad networks.
        if _store.is_repo(store) and (conf.meta.upstream or _store.get_upstream(store)):
            try:
                import os as _os

                try:
                    _timeout = int(_os.environ.get("VERSIONEER_DOCTOR_TIMEOUT", "10") or 10)
                except ValueError:
                    _timeout = 10
                ok, detail = _store.upstream_reachable(store, "", timeout=_timeout)
                if ok is True:
                    console.print(f"  upstream reachable: {detail}")
                elif ok is False:
                    console.print(f"  [yellow]warn[/yellow]: {detail}")
                    warnings += 1
            except (OSError, ValueError) as e:  # never crash doctor on probe errors
                console.print(f"  [yellow]warn[/yellow]: upstream probe skipped: {e}")
                warnings += 1
        # disk quota (statvfs, warn-only)
        try:
            st = _shutil.disk_usage(store if store.exists() else store.parent)
            if st.free < 500 * 1024 * 1024:
                console.print(
                    f"  [yellow]warn[/yellow]: low disk free "
                    f"{st.free // (1024 * 1024)} MB on store fs"
                )
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
                        console.print(
                            f"  [yellow]warn[/yellow] {t.path}: large file "
                            f"{size / (1024 * 1024):.1f} MB > {warn_mb} MB"
                        )
                        warnings += 1
                except OSError:
                    pass
            if t.on_deploy and not _shutil.which(t.on_deploy.split()[0]):
                console.print(
                    f"  [yellow]warn[/yellow] {t.path}: on_deploy validator "
                    f"not on PATH: {t.on_deploy.split()[0]}"
                )
                warnings += 1
            if t.kind == "binary" and t.retention:
                n = _store.count_artifact_commits(store, r["rel"].as_posix())
                w = _store.retention_warning(n, t.retention)
                if w:
                    console.print(f"  [yellow]warn[/yellow] {t.path}: {w}")
                    warnings += 1
            if bool(getattr(t, "auto_commit", False)):
                if not cfg.has_limited_retention(getattr(t, "retention", {})):
                    console.print(
                        f"  [red]error[/red] {t.path}: auto_commit=true "
                        "requires limited retention (set retention.count >= 1)"
                    )
                    errors += 1
                else:
                    console.print(
                        f"  [yellow]warn[/yellow] {t.path}: {cfg.AUTO_COMMIT_SPACE_WARNING}"
                    )
                    warnings += 1
            # secrets audit (live file scan)
            live = r["abs_path"]
            try:
                if live.is_file() and not live.is_symlink() and live.stat().st_size < 5_000_000:
                    for wmsg in _lint.scan_file(live):
                        console.print(f"  [yellow]warn[/yellow] {t.path}: {wmsg}")
                        warnings += 1
            except OSError:
                pass
            # ITEM 2: encrypt flag vs toolchain/store state (warn-only, never blocks)
            try:
                from versioneer.core import secrets as _sec

                needs_enc = _sec.should_encrypt(t, conf.meta)
                if needs_enc and not _sec.sops_available():
                    console.print(
                        f"  [yellow]warn[/yellow] {t.path}: encrypt=true "
                        f"but sops not found (sudo pacman -S sops age) — "
                        f"plaintext for now (warn-only)"
                    )
                    warnings += 1
                elif needs_enc:
                    src_artifact = store / r["rel"]
                    if src_artifact.is_file() and not _sec.is_encrypted_file(src_artifact):
                        console.print(
                            f"  [yellow]warn[/yellow] {t.path}: encrypt=true "
                            f"but store artifact is plaintext (run commit to encrypt)"
                        )
                        warnings += 1
            except ImportError:
                pass

    if secrets_only:
        # ITEM 2: secrets audit + sops/age toolchain + per-target encrypt state
        from versioneer.core import secrets as _sec

        _st = _sec.toolchain_status()
        console.print(
            f"sops={'found' if _st['sops'] else 'not found'} "
            f"age={'found' if _st['age'] else 'not found'} "
            f"(sudo pacman -S sops age)"
        )
        if not _st["sops"]:
            console.print(
                "[yellow]warn[/yellow]: sops not found — "
                "encrypt=true targets stay plaintext (warn-only in v1)"
            )
            warnings += 1
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
                            if not _sec.should_encrypt(t, conf.meta):
                                console.print(
                                    f"  [yellow]warn[/yellow]: {n}/{t.path}: "
                                    f"secret found but encrypt=false — "
                                    f"consider `target add --encrypt` (warn-only)"
                                )
                                warnings += 1
                except OSError:
                    pass
                # per-target encrypt flag vs store state
                try:
                    if _sec.should_encrypt(t, conf.meta):
                        src_artifact = store / r["rel"]
                        if not _st["sops"]:
                            console.print(
                                f"  [yellow]warn[/yellow]: {n}/{t.path}: "
                                f"encrypt=true but sops not found (warn-only)"
                            )
                            warnings += 1
                        elif src_artifact.is_file():
                            if _sec.is_encrypted_file(src_artifact):
                                console.print(f"  encrypted (sops): {n}/{t.path}")
                            else:
                                console.print(
                                    f"  [yellow]warn[/yellow]: {n}/{t.path}: "
                                    f"encrypt=true but store artifact plaintext"
                                )
                                warnings += 1
                except OSError:
                    pass
        console.print(f"secrets audit: {warnings} warning(s)")
        return

    for n in names:
        _check_config(n)
    # Host-level toolchain checks (warn-only, once per run): sops/age
    # presence, systemd units installed, shell completions present.
    try:
        from versioneer.core import secrets as _sec_h

        _st = _sec_h.toolchain_status()
        if not _st.get("sops") or not _st.get("age"):
            missing_tools = "/".join(k for k in ("sops", "age") if not _st.get(k))
            console.print(
                f"[yellow]warn[/yellow]: {missing_tools} not found "
                "(sudo pacman -S sops age; encrypt=true stays plaintext until then)"
            )
            warnings += 1
    except ImportError:
        pass
    try:
        from versioneer.core import daemon as _daemon_h

        for _upath in (_daemon_h.user_unit_path(), _daemon_h.user_timer_path()):
            try:
                if not (_upath.is_file() or _upath.is_symlink()):
                    console.print(
                        f"[yellow]warn[/yellow]: systemd unit not installed: {_upath} "
                        "(run `versioneer service install`)"
                    )
                    warnings += 1
            except OSError:
                pass
        try:
            _missing = _daemon_h.missing_completions()
        except (OSError, RuntimeError):
            _missing = []
        if _missing:
            console.print(
                "[yellow]warn[/yellow]: shell completions missing: "
                + ", ".join(str(p) for p in _missing)
                + " (run ./installer/install.sh or copy installer/completions/*)"
            )
            warnings += 1
    except ImportError:
        pass
    console.print(f"doctor: {errors} error(s), {warnings} warning(s)")
    if errors:
        raise SystemExit(1)


def _uninstall_venv_dir(venv_opt: str | None) -> Path:
    """Resolve the venv dir (CLI flag > env > default, sudo-aware)."""
    import os as _os

    from versioneer.core import elevate as _elev

    raw = venv_opt or _os.environ.get("VERSIONEER_VENV_DIR")
    if raw:
        return _elev.expand_user(_os.path.expandvars(raw))
    return _elev.effective_home() / ".local" / "share" / "versioneer" / "venv"


def _uninstall_default_store_root() -> Path:
    from versioneer.core import elevate as _elev

    return _elev.effective_home() / "versioneer-store"


def _uninstall_disable_units() -> list[str]:
    """Disable user+system timers/units (warn-only). Returns actions taken."""
    import os as _os
    import shutil as _shutil
    import subprocess as _sp

    from versioneer.core import daemon as _daemon
    from versioneer.core import elevate as _elev

    done: list[str] = []
    sys = _shutil.which("systemctl")
    if not sys:
        console.print("[yellow]warn[/yellow]: systemctl not found — skipping unit disable")
        return done
    user_cmd = [
        sys,
        "--user",
        "disable",
        "--now",
        _daemon.USER_TIMER_NAME,
        _daemon.USER_SERVICE_NAME,
    ]
    # Under `sudo versioneer uninstall`, the --user manager must be the
    # invoking user's, not root's.
    inv = _elev.invoking_user()
    if inv is not None:
        sudo = _shutil.which("sudo")
        if sudo:
            user_cmd = [
                sudo,
                "-u",
                inv,
                sys,
                "--user",
                "disable",
                "--now",
                _daemon.USER_TIMER_NAME,
                _daemon.USER_SERVICE_NAME,
            ]
    cmds = [
        user_cmd,
        [sys, "disable", "--now", _daemon.SYSTEM_TIMER_NAME, _daemon.SYSTEM_SERVICE_NAME],
    ]
    # Silence unused-import lint for _os (kept for symmetry with shell scripts).
    _ = _os.environ.get("USER", "")
    for cmd in cmds:
        try:
            _sp.run(cmd, check=False, capture_output=True, timeout=60)
            done.append(" ".join(cmd))
        except (OSError, _sp.TimeoutExpired) as e:
            console.print(f"[yellow]warn[/yellow]: {' '.join(cmd)} failed: {e}")
    return done


@cli.command("uninstall")
@click.option(
    "--purge-stores",
    is_flag=True,
    show_default=True,
    help="Also delete ~/versioneer-store/<name> repos (kept by default).",
)
@click.option(
    "--yes", is_flag=True, show_default=True, help="Assume yes for confirmations (non-interactive)."
)
@click.option(
    "--venv",
    "venv_opt",
    default=None,
    help="Venv dir to remove (default $VERSIONEER_VENV_DIR or ~/.local/share/versioneer/venv).",
)
def uninstall(purge_stores: bool, yes: bool, venv_opt: str | None) -> None:
    """Stop units, remove venv/completions, remove configs (confirm), keep stores.

    Mirrors installer/uninstall.sh in-process (testable via CliRunner).
    Never deletes the running executable: when sys.executable lives inside
    the venv dir, venv removal is skipped with a warning.
    """
    import shutil as _shutil
    import sys as _sys

    # 1. Disable timers/units (warn-only; never fails the run).
    _uninstall_disable_units()

    # Remove user unit/timer files installed by `service install` (best-effort).
    try:
        from versioneer.core import daemon as _daemon

        for p in (_daemon.user_unit_path(), _daemon.user_timer_path()):
            try:
                if p.is_file() or p.is_symlink():
                    p.unlink()
                    console.print(f"[yellow]removed[/yellow] {p}")
            except OSError as e:
                console.print(f"[yellow]warn[/yellow]: cannot remove {p}: {e}")
    except ImportError:
        pass

    # 2. Remove venv dir — never delete the running interpreter.
    venv_dir = _uninstall_venv_dir(venv_opt)
    try:
        exe = Path(_sys.executable).resolve(strict=False)
        venv_resolved = venv_dir.resolve(strict=False)
        running_inside = exe.is_relative_to(venv_resolved)
    except (OSError, ValueError):
        running_inside = False
    if running_inside:
        console.print(
            f"[yellow]warn[/yellow]: skipping venv removal {venv_dir} "
            "(running executable lives inside it)"
        )
    elif venv_dir.is_dir() or venv_dir.is_symlink():
        # A bare symlink (e.g. ~/.local/share/versioneer/venv -> elsewhere)
        # is removed as a link; real dirs are removed as trees.
        try:
            if venv_dir.is_symlink() and not venv_dir.is_dir():
                venv_dir.unlink()
            else:
                _shutil.rmtree(venv_dir, ignore_errors=False)
            console.print(f"[yellow]removed[/yellow] venv {venv_dir}")
        except OSError as e:
            console.print(f"[yellow]warn[/yellow]: cannot remove venv {venv_dir}: {e}")
    else:
        console.print(f"no venv at {venv_dir} (nothing to remove)")

    # Remove ~/.local/bin/versioneer + vers shims only when they point into the venv.
    try:
        from versioneer.core import elevate as _elev_u

        _bindir = _elev_u.effective_home() / ".local" / "bin"
        for _name in ("versioneer", "vers"):
            shim = _bindir / _name
            if shim.is_symlink():
                try:
                    target = shim.resolve(strict=False)
                    if target.is_relative_to(venv_dir.resolve(strict=False)):
                        shim.unlink()
                        console.print(f"[yellow]removed[/yellow] shim {shim}")
                except OSError as e:
                    console.print(f"[yellow]warn[/yellow]: cannot remove shim {shim}: {e}")
    except (OSError, ValueError):
        pass

    # Remove /usr/local/bin shims installed by the installer (best-effort;
    # needs root, so warn instead of failing when sudo is unavailable).
    try:
        for _name in ("versioneer", "vers"):
            _sys_shim = Path(f"/usr/local/bin/{_name}")
            try:
                if _sys_shim.is_file() and not _sys_shim.is_symlink():
                    try:
                        _text = _sys_shim.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        _text = ""
                    if "versioneer" in _text or venv_dir.as_posix() in _text:
                        try:
                            _sys_shim.unlink()
                            console.print(f"[yellow]removed[/yellow] shim {_sys_shim}")
                        except OSError:
                            console.print(
                                f"[yellow]warn[/yellow]: cannot remove {_sys_shim} "
                                "(needs sudo: sudo rm "
                                f"{_sys_shim})"
                            )
            except OSError:
                pass
    except (OSError, ValueError):
        pass

    # 3. Remove shell completions (best-effort, mirrors uninstall.sh).
    try:
        from versioneer.core import elevate as _elev_c

        home = _elev_c.effective_home()
        for c in (
            home / ".local/share/bash-completion/completions/versioneer",
            home / ".config/fish/completions/versioneer.fish",
            home / ".zfunc/_versioneer",
        ):
            try:
                if c.is_file() or c.is_symlink():
                    c.unlink()
                    console.print(f"[yellow]removed[/yellow] completion {c}")
            except OSError:
                pass
    except (OSError, RuntimeError):
        pass

    # Snapshot store dirs BEFORE configs are removed (purge needs them).
    known_stores: list[Path] = []
    try:
        for n in cfg.list_configs():
            try:
                conf = cfg.load(n)
            except (FileNotFoundError, ValueError, OSError):
                continue
            try:
                known_stores.append(cfg.store_dir(conf))
            except (ValueError, OSError):
                continue
    except OSError:
        pass

    # 4. Configs: confirm unless --yes.
    cdir = cfg.config_dir()
    if cdir.is_dir():
        confirmed = bool(yes) or click.confirm(f"Remove {cdir} TOMLs?", default=False)
        if confirmed:
            try:
                _shutil.rmtree(cdir)
                console.print(f"[yellow]removed[/yellow] {cdir}")
            except OSError as e:
                raise click.ClickException(f"cannot remove {cdir}: {e}")
        else:
            console.print(f"kept {cdir}")
    else:
        console.print(f"no configs at {cdir} (nothing to remove)")

    # 5. Stores: kept unless --purge-stores (second confirm unless --yes).
    if purge_stores:
        do_purge = bool(yes) or click.confirm("Delete ~/versioneer-store/* too?", default=False)
        if do_purge:
            targets: list[Path] = list(known_stores)
            # Configs may already be removed above: fall back to the
            # conventional root so `uninstall --purge-stores --yes` still purges.
            default_root = _uninstall_default_store_root()
            if default_root not in targets:
                targets.append(default_root)
            for s in targets:
                try:
                    if s.is_dir() or s.is_symlink():
                        if s.is_symlink() and not s.is_dir():
                            s.unlink()
                        else:
                            _shutil.rmtree(s)
                        console.print(f"[yellow]purged[/yellow] {s}")
                    else:
                        console.print(f"no store at {s} (nothing to purge)")
                except OSError as e:
                    console.print(f"[yellow]warn[/yellow]: cannot purge {s}: {e}")
        else:
            console.print("kept ~/versioneer-store/<name> (purge declined)")
    else:
        console.print("kept ~/versioneer-store/<name> (pass --purge-stores to delete)")

    console.print("done.")
