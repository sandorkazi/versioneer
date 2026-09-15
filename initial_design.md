# Business Goal

Have a service based file and config monitoring application, mainly for CachyOS and arch based linux distributions.

Versioneer is **git + git-lfs with a brain for Linux configs**: it watches files you declare
(`hyprland.conf`, `fstab`, savegames, wine recipes, `~/bin` snippets), tells you when they drift,
and redeploys them safely (backup → atomic write → permission restore → validation hook).

It is **not** a blind backup tool (like `rsync`/`restic`) and **not** a dotfile templater
(like `chezmoi`/`stow`) — it sits in between: versioned state + safe rollout + background monitoring.

# Core Lifecycle (authoritative)

Three planes:

1. **Declared state** — `~/.config/versioneer/<name>.toml` lists targets + last-known `hash/owner/mode`.
2. **Versioned store** — one plain git (+ LFS) repo per config, e.g. `~/versioneer-store/hypr/`.
   Text = normal git, binary/savegames = LFS, manifests = generated recipes.
3. **Live filesystem** — what is actually on disk right now.

Rules:

1. **`target add` = start tracking from now.** Records `hash/owner/mode`, commits that baseline.
   No history backfill, no watcher installed.
2. **`target remove` = stop tracking.** Deletes TOML entry + `git rm`s artifact, commits removal.
   Working file untouched, git history kept (full purge via `git filter-repo` only as escape hatch).
3. **Daemon is read-only by default.** `hash + stat` only, never `commit/push`.
   Exception: per-config `auto_commit=true` (savegames/snippets) allows silent commit;
   `auto_push=true` (off by default, requires `auto_commit`) additionally pushes.
4. **Save and rollout are always explicit and selective:**
   `commit [--all|<target>...]`, `push/pull`, `deploy [<target>...] [--dry-run]`.
   `status/commit` work offline; `push/pull` defer with a message when upstream is unreachable.
5. **Deploy is safe by construction:** backup-before-overwrite (`.bak` + timestamped copy),
   atomic tmp+rename, permission restore (`chown/chmod` incl. `+x`), `sudo` re-exec when needed,
   per-target `ok|skipped|error` (never whole-run abort).

# Functional Requirements

## Installer

- one can install a service, a CLI app and helps the user to create the storage (git repository with LFS support)
- same request for uninstall with the addition of removing its config files from .config
- prerequisites: Arch/CachyOS (or any systemd Linux), Python 3.12+, `git`, `git-lfs`,
  private git host with SSH auth, `notify-send`/D-Bus for notifications (fallback: stdout/journal),
  optional `sops`+`age` for encrypted targets
- installer is venv-only — never uses system python for packages, never `sudo pip install` /
  `pip install --break-system-packages`. Creates `~/.local/share/versioneer/venv` or uses `pipx`.
  Installs user + system units and shell completions (bash/fish/zsh). Checks `git lfs install`.
- `versioneer doctor` re-validates install (LFS present, upstream reachable/auth, disk quota,
  read-errors, large files, dangling symlinks, missing validators, retention status).
- uninstall: stop/disable units, remove CLI + `~/.config/versioneer` (after confirm);
  stores in `~/versioneer-store/<name>` are kept unless `--purge-stores` is passed.

## Service Application

- regularly checks and monitors targets on a filesystem
- can manage elevation (sudo) with warning about the files not being pushed to a version control
- lets the user know via notification (on system start and every 3 hours) if there are drifted file states
- may manage multiple groups of targets (configs), for which notifications may or may not apply
- split into two units: `versioneer-user.service` (`$HOME` targets, `notify-send`) +
  `versioneer-system.service` (`/etc` targets, read-only, runs as root). System unit only reads;
  unreadable files are a distinct `read-error` state ("run `versioneer status` as root"), never a push warning.
- checks on system start + every interval. Global default `3h`, overridable per config/target
  (`check_interval = "5m"`, `"10s"` for tests, `"inotify"` for immediate savegame/snippet mode via `watchdog`).
  `VERSIONEER_INTERVAL` env override for tests.
- per-config `notify = true|false` — noisy configs (savegames) may commit silently and notify on errors only.
  Damping: max one notification per interval. Zero git writes by default (asserted in tests);
  writes happen only with `auto_commit=true`.
- game-exit hook pattern (shipped as unit template):
  `versioneer commit -C savegames --all -m "post-session" && versioneer push -C savegames`
- logs: `journalctl` (service) + `~/.local/state/versioneer/<config>-deploy-*.json` (deploy).

## CLI

- user may specify the config (-C) for any command
- user may create a config by providing a path, a short name and an upstream location (git)
  - per-config policy: `notify` / `auto_commit` / `auto_push` / `check_interval`.
    `--auto-push` requires `--auto-commit`. Typical split: `hypr`/`etc` notify yes + no auto-commit;
    `savegames`/`snippets` auto-commit yes. Upstream should be private.
- user may add targets to a config, specifying their details (see: Data Model)
- user can smart deploy files onto a machine from the version control (see: Smart Deploy)
- the monitoring target's path needs to be saved
  - optional root dir may be specified so that is omitted from the path (the actual path is interpreted relative to that)
    - user targets may not have such root dir, as that defeats their purpose (`root` with `flex=user` is rejected)
- user may add file paths via waiting for folder changes:
  1. start monitoring the target dir (root dir may be provided)
  2. make some changes
  3. terminate the monitorig: list the changed files within the interval and add only those files (with same root) instead of the folder
  - `versioneer -C <cfg> watch <dir> [--auto-add]`: snapshot hashes, wait until Ctrl-C,
    diff → interactive multi-select → `target add` (or auto-add all). Uses `inotify` (`watchdog`)
    with polling fallback. Respects `ignore`, applies the same `root`.
- CLI should be colored, should work the same way from bash, fish and zsh
- review (read-only): `status` (drift table), `diff` (unified diff for text, stat summary for
  binary/dir, recipe preview for manifest), `log` (store git log). Save (explicit):
  `commit [-m MSG] [--all|<target>...]`, `push/pull`.
- `add`/`commit` run two warn-only lints: **secret scan** (possible token/key → suggest
  `encrypt=true` or `ignore`) and **hardcoded-path lint** (absolute `/home/<other>/` in a `text`
  file → suggest `--template`).
- locked files (SQLite WAL, game still running) are copied-then-hashed with a warning, never hashed in place.
- `service install/enable/disable` glue; inspect via `systemctl --user status` / `sudo systemctl status`.
- `bootstrap <upstream-url> [--to DIR] [--host HOST]` and `bootstrap --all`: clone store(s) +
  recreate TOML(s) + `deploy`. New-machine flow is `clone → deploy --dry-run → deploy`.
- `manifest [--packages] [--wine] [--systemd] [--env]` (at least one flag required).
  Regenerated on commit, replayed on deploy.
- `doctor [-C config] [--secrets]`: health + secret audit, exit != 0 on errors.

## Data Model

- each config's content (-C) is managed separately
- owner (by user name), group (by name) and chmod flags are to be kept and monitored
  - captured on `add` as baseline, refreshed on `commit`. Drift (even if content same) is a
    distinct `perm-drift` state. Deploy MUST restore `owner/group/mode` (incl. `+x` for snippets).
- drift states (what `status` reports; there is intentionally no `unversioned` catch-all —
  every state has a distinct next action):
  - `tracked`: in TOML + baseline committed
  - `modified`: content hash differs from baseline
  - `perm-drift`: `owner/group/mode` differs (even if content same)
  - `missing`: tracked path no longer exists (incl. dangling links)
  - `untracked`: new file inside a `dir`/`glob` target, not yet added (never auto-added
    unless `watch --auto-add` for snippets)
  - `read-error`: cannot be read (permission denied) — needs elevation
  - `clean`: matches baseline
- has multiple target category distinctions
  1. location types
    - text file targets - normal versioning
      - normal git + unified diff. Supports `template = true` for `{{HOME}}`/`{{HOST}}`
        substitution on deploy.
    - binary file targets - few versions are kept (last 3 by default)
      - with alerts on large files (`large_file_warn_mb`, default ~10 MB)
      - git-LFS. Default retention `count=3`; savegames override to e.g. `{count=30, age="30d"}`.
        Retention is configurable, not silent squash. Savegames use bulk `commit --all`.
      - with alerts on large files
    - dir targets are monitored for any changes (even new files) based on file type
      - snapshot of file list + per-file handling. New files = `untracked` candidates.
        Supports `glob` (e.g. `~/.local/bin/*`) and gitignore-style `ignore`
        (e.g. `Cache/`, `*.log`, wine `Temp/`). `symlink = preserve|follow` (default `preserve`;
        `follow` dereferences on commit, `preserve` recreates link on deploy).
    - manifest targets - generated recipe, not a copy (NEW vs original design)
      - types: `packages` (`pacman -Qqe`, AUR `yay/paru -Qqm`, `flatpak list --app` →
        `packages.list` + `setup-packages.sh`), `wine` (`wine --version`, `WINEARCH`,
        `WINEPREFIX`, `winetricks list-installed`, exe inventory → `wine-manifest.json` +
        `setup-wine.sh`), `systemd` (enabled user + system units → `units.list`),
        `env` (`$PATH`, shell, hotkey env → `env.json`).
      - behave like `text` for `status/diff/commit` (regenerated on `commit`), replay on `deploy`
        (prints replay; `--apply` where safe, default prints only).
  2. flexibility
    - fixed targets are for the same location
      - absolute path, e.g. `/etc/fstab`. Deploy writes to the same path (needs `sudo`).
    - user targets are for the same location within the current user's directory
      - eg: /home/a/x.txt backed up and deployed elsewhere by user b will end up in /home/b/x.txt by default
      - identified automatically based on path (/home/someuser/...)
      - auto-detected from `/home/*/`; on deploy the prefix is rewritten to current `$HOME`.
        A `root` may never be set for these.
    - flexi tagets need to have a target location to get deployed (during smart deploy)
      - no fixed destination. `--to <path>` required at deploy (or interactive prompt /
        `deploy_path` in TOML); missing `--to` fails that entry only, never the whole run.
  3. whether the state or the diff is of interest
    - state: the whole is the artifact
    - diff: the file is kept, but when deploying, what has happened to the file is more important
    - v1: `diff` previews a diff in `status`/`dry-run` but deploys state; true patch-apply is v2.
- practical per-target fields beyond kind/flex/interest: `glob`, `ignore`, `symlink`, `machines`,
  `retention`, `template`, `on_deploy`, `deploy_path`, `check_interval` override.
  `machines = []` means all hosts, else e.g. `["laptop"]` deploys as `skipped (wrong host)`
  elsewhere (`--force-host` overrides for testing). Alternative per-machine branches documented
  as escape hatch (merge cost documented).
- **Wine rule: manifest-only by default.** Never `target add ~/prefix` whole — prefixes are GBs
  of churn (`Temp/`, `*.log`, `dosdevices/` symlinks). Instead: one `manifest(type=wine)` for the
  recipe + selective `text` targets for `*.reg`/`*.cfg` with default ignores
  (`drive_c/users/*/Temp/**`, `*.log`, `*.tmp`, `**/Cache/**`, `dosdevices/**`) + savegames inside
  a prefix as a separate `binary` target. Full-prefix copy requires explicit `--force` +
  docs acknowledgment.
- **Secrets:** `add`/`commit` scans for tokens/private keys (warn-only in v1, never blocks).
  Real secrets use `encrypt = true` per target via `sops + age` (encrypted in store, decrypted
  on deploy). Keep upstreams private; `doctor --secrets` audits. If a secret was pushed, rotate
  it — git history keeps it.
- **Templates:** `template = true` substitutes `{{HOME}}`/`{{HOST}}` on deploy.
- **Hooks:** `on_deploy = "findmnt --verify /etc/fstab"` or `"hyprctl reload"` runs after copy;
  failure = per-target `error` with output captured in the deploy-status file.

## Smart Deploy (CLI)

- user may dry run (see the list of affected files) before a rollout
  - plan file can be created so one can make sure through hashes that there were no interim edits between the plan and the deploy (keep track of hash deviations, list them as deployment errors)
  - `deploy --dry-run` previews actions + diffs, no writes. `deploy --plan-out plan.json` writes
    `{target, src_hash, dst_hash, action}`; `deploy --plan plan.json` re-hashes and flags interim
    edits as errors (re-run `--plan-out`).
- rollout is different per file category
  - binary files: copy to target, ask for overwrite (N by default) if file exists and different
    - text/binary: backup-before-overwrite (`.bak` + timestamped copy), atomic write (tmp + rename),
      perm restore, `sudo` re-exec if unwritable. Exists + differs → prompt overwrite
      (default N; `--yes`/`--no-interaction` for scripts).
  - if the dir exists: ask whether to skip (default), overwrite or merge (somehow)
    - `skip` (default) | `overwrite` | `merge` (rsync-like, no deletes unless `--prune`).
  - if it is a flexi target: need to arrange a location on the deploy system
  - if the destination location does not exist: don't deploy
    - recorded as per-target `error`, never whole-run failure.
  - symlink: `preserve` recreates link, `follow` writes content.
  - template: substitutes `{{HOME}}`/`{{HOST}}` when `template=true`.
  - manifest: prints replay (`pacman -S ...`, `setup-wine.sh`); `--apply` where safe.
  - machines allowlist: non-matching hosts deploy as `skipped (wrong host)`.
  - validation + hooks: `on_deploy` runs after copy (e.g. `findmnt --verify`, `hyprctl reload`);
    hook failure = per-target `error`, not whole-run abort.
- smart deploy needs to produce a deployment status file
- smart deploy needs to pick up the current deployment status file and create a new one after the run
  - `~/.local/state/versioneer/<config>-deploy-<timestamp>.json` + `latest.json` symlink.
    Next deploy reads the previous status as baseline. Hook output captured there.
    Backups live in `backups/<timestamp>/...`.
- smart deploy should not fail on errors, but indicate which monitored targets were or were not deployed
  - per-target `ok|skipped|error + reason`. Missing `--to` (flexi), missing destination, and
    interim plan edits are per-entry errors.
- safety notes: deploying `/etc/fstab` always `--dry-run` first, keep the `.bak`, keep a live USB handy.
  Versioneer helps you not shoot your foot — it can't remove the gun.

## Storage Layout

```text
~/.config/versioneer/<name>.toml        # declared state + baselines
~/versioneer-store/<name>/              # git + LFS repo (one per config)
~/.local/state/versioneer/
  <config>-deploy-<timestamp>.json      # per-target ok|skipped|error + hook output
  <config>-latest.json -> <config>-deploy-<timestamp>.json  # symlink to newest
  backups/<timestamp>/...               # backup-before-overwrite copies (.bak)
```

- `text` → normal git objects, word diff.
- `binary` → LFS (`*.bin` + explicit binary targets), `large_file_warn_mb` alert,
  `retention = {count, age}` enforced on commit.

# Non-Goals / Design Choices Worth Knowing

- `diff`-interest deploys state in v1 (true patch-apply is v2).
- dir `merge` never deletes extras unless `--prune`.
- daemon never auto-pushes unless you opt in (`auto_commit` + `auto_push`).
- full wineprefix tracking discouraged (manifest-only default); full-prefix copy is an explicit escape hatch.
- per-machine branches are an escape hatch; default is single branch + `machines` allowlist + templating.
- secret encryption in v1 is warn + `sops/age` opt-in; no silent encryption.
