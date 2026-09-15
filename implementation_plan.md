# Versioneer — Implementation Plan
Derived from `initial_design.md`. Business goal: service-based file and config monitoring app, primarily for CachyOS / Arch-based Linux.

## 0. Core lifecycle (authoritative)

- `target add <path>` only starts tracking from now on: records path + current `hash/owner/mode` baseline in TOML and commits that baseline to the store. It does not backfill history and does not install any watcher beyond the regular check. Supports single paths, `--glob` patterns (e.g. `~/.local/bin/*`), and `dir` targets with `ignore` lists; symlinks handled per `symlink` policy (`preserve|follow`, default `preserve`).
- `target remove <path>` only stops tracking: removes the TOML entry and `git rm`s the artifact from the store (working file on disk is left untouched, git history is kept), then commits that removal.
- Content updates never happen via `add` or via the daemon by default. Update flow is always: daemon detects drift (read-only) → `notify` → user reviews via `status/diff` → user saves via `commit + push`. Exception: per-config/target opt-in `auto_commit = true` (intended for savegames/snippets) allows the daemon/service or a game-exit hook to `commit` (optionally `push`) without manual review; this must be explicit and is off by default.
- Daemon/service is strictly read-only unless `auto_commit` opt-in is set for that config: `hash + stat` compare only. It never `push`es unless `auto_push = true` is also set. Read failures (e.g. missing permission) are a distinct `read-error` state, not a push warning.
- Deploy is always explicit, selective, and safe: `deploy [<target>...]` (default all), with `--dry-run` preview, `--plan` hash-guard, per-target `ok|skipped|error`, backup-before-overwrite (`.bak` + timestamped copy), atomic write (tmp + rename), permission restore (`chown/chmod` from baseline), and elevation via `sudo` when destination requires it. Deploy never deletes extras on merge unless `--prune` is given.

## 1. Guiding decisions (to confirm before Phase 1)

1. **Language: Python 3.12+ (recommended).** Rationale: fast to build, good `systemd` / `inotify` / `git` libs, single binary-ish distribution via `pipx`/`uv` + Arch PKGBUILD. Alternative: Go (better single static binary, but slower to iterate on git-LFS + diff logic).
2. **Config format: TOML** (`~/.config/versioneer/<name>.toml`). Human-editable, preserves permissions model well. JSON avoided (no comments).
3. **Storage: plain git repo + git-lfs per config**, as per design. One repo per config (`-C`), upstream set at `versioneer config create`.
4. **Service: systemd units** — one `--user` unit for user targets + one system unit for fixed/flexi targets requiring elevation. Timer or in-process scheduler for 3h checks.
5. **Notifications: `notify-send` / D-Bus (`org.freedesktop.Notifications`)**, fallback to stdout/journal.
6. **CLI UX: `click` + `rich`** for color. Shell-agnostic (bash/fish/zsh work the same since it's a binary, plus shipped completions for each shell).

Open questions:
- Q1: system-wide vs. user-only install default?
- Q2: default check interval + startup delay? Note: now per-config/target overridable (`check_interval`, see §3); global default remains 3h, savegame/snippet configs may use shorter intervals or `inotify` immediate mode.
- Q3: binary retention = 3 versions — git history squash or LFS pruning? DECISION REQUIRED before M1: squash breaks `plan.json` hashes + `target remove` history; `lfs prune` is local-only. Candidates: (a) keep full history + document LFS quota, (b) time-based retention (e.g. keep 30d of savegames) via separate `savegame` branch + orphan compaction, (c) configurable `retention = {count, age}` per target. Default proposal: (c) with `count=3` for `binary`, `count=30`/`age=30d` opt-in for savegames.
- Q4: secret encryption backend: `git-crypt` vs `sops/age` vs `transcrypt`? Proposal: `sops + age` for per-file encryption, plus secret-scan warning on `add` (no silent encryption in v1).
- Q5: multi-machine strategy: single branch + `machines` allowlist vs per-machine branches (`host-<hostname>`)? Proposal: single branch + optional `machines = [...]` / `host` conditioning + `{{HOME}}`/`{{HOST}}` templating (see §3).
- Q6: manifest scope for v1: `pacman/Qqe + flatpak + systemctl enable` and `wine-manifest` only, or also `cargo/pip/npm`? Proposal: pacman/AUR-helper + flatpak + systemd first; language package managers in v2.

Additional guiding decisions (from gap review):
7. **Glob/ignore/symlink:** `target add --glob PATTERN`, `dir` targets support `ignore = [...]` (gitignore-style), `symlink = "preserve"|"follow"` (default `preserve`; `follow` dereferences on commit, restores as symlink on deploy).
8. **Wineprefixes are manifest-only by default:** never `target add ~/prefix` whole; use `wine-manifest` recipe + selective `*.reg`/`*.cfg` tracking with default excludes (`Temp/`, `*.log`, `*.tmp`, `Cache/`). Document escape hatch for small prefixes.
9. **Bootstrap flow:** new-machine setup is `versioneer bootstrap <upstream-url-or-config> [--to DIR]` = clone store(s) + recreate TOML(s) + `deploy --all`. No manual `config create` on the new machine.
10. **Deploy safety invariants:** backup-before-overwrite, atomic tmp+rename, permission restore, `sudo` re-exec when needed, destination validation hooks (e.g. `findmnt --verify` for fstab, `hyprctl reload` check).

## 2. Proposed repo layout

```
versioneer/
  initial_design.md
  implementation_plan.md
  pyproject.toml
  src/versioneer/
    cli.py            # entrypoint, -C handling
    commands/
      config_cmd.py   # create/list/show
      target_cmd.py   # add/remove/list (tracking-set only, see §0)
      status_cmd.py   # status/diff/log (review, read-only)
      sync_cmd.py     # commit/push/pull (save, explicit only)
      watch_cmd.py    # folder-change capture workflow
      deploy_cmd.py   # dry-run, plan, rollout, deploy-status
      service_cmd.py  # install/uninstall helpers hook
    core/
      config.py       # Config + Target dataclasses, load/save TOML
      store.py        # git + LFS wrapper (init/clone/commit/push/pull)
      monitor.py      # hashing, stat compare, inotify/scan
      permissions.py  # owner/group/chmod capture & apply (deploy restores)
      deploy.py       # planner + rollout engine + deploy-status file (backup, atomic, sudo, validation)
      notify.py       # desktop notifications
      manifest.py     # NEW: package/wine/systemd/env exporters (pacman, flatpak, winetricks, wine --version)
      secrets.py      # NEW: secret scan on add/commit + sops/age encrypt helpers
      template.py     # NEW: {{HOME}}/{{HOST}} templating + hardcoded-path lint
      hooks.py        # NEW: pre/post-deploy validation + restart hooks (e.g. hyprctl reload)
    service/
      daemon.py       # check loop, multi-config, elevation handling, auto_commit opt-in path
      versioneer-user.service
      versioneer-system.service
    commands/
      bootstrap_cmd.py # NEW: clone + deploy --all for new machines
      manifest_cmd.py  # NEW: `manifest --packages --wine --systemd`
      doctor_cmd.py    # NEW: `doctor` health checks (LFS, auth, disk, read-errors, large files)
  installer/
    install.sh
    uninstall.sh
    PKGBUILD (cachyos/arch)
  tests/
```

## 3. Data model (detailed spec)

`~/.config/versioneer/<config>.toml`:

```toml
[meta]
name = "short-name"
upstream = "git@host:org/repo.git"
root = "/etc"              # optional; omitted for user targets
storage = "~/versioneer-store/<name>"
notify = true              # per-config notification toggle
auto_commit = false        # NEW opt-in: daemon/hook may commit without review (savegames)
auto_push = false          # NEW opt-in: requires auto_commit; daemon may also push
check_interval = "3h"      # NEW per-config override; supports "10s" (tests), "5m", "inotify"
encrypt = false            # NEW: sops/age per-file encryption flag (v2 full, v1 warning only)

[[targets]]
path = "nginx/nginx.conf"  # relative to root if root set, else absolute
abs_path = "/etc/nginx/nginx.conf"  # resolved, cached
kind = "text" | "binary" | "dir" | "manifest"  # NEW: manifest = generated recipe, not copied verbatim
glob = ""                  # NEW: optional glob pattern (e.g. "~/.local/bin/*"); expands to tracked set
ignore = []                # NEW: gitignore-style excludes for dir/glob (e.g. ["*.log", "Cache/", "Temp/"])
symlink = "preserve"       # NEW: "preserve" (recreate link) | "follow" (dereference)
flex = "fixed" | "user" | "flexi"
interest = "state" | "diff"
deploy_path = ""           # required only for flexi, prompted at deploy
owner = "root"
group = "root"
mode = "0644"
hash = "sha256:..."
machines = []              # NEW: empty = all hosts; else ["laptop", "desktop"] hostname allowlist
check_interval = ""        # NEW: per-target override (savegames shorter, fstab longer)
retention = { count = 3 }  # NEW: per-target retention override, e.g. {count=30, age="30d"}
template = false           # NEW: if true, apply {{HOME}}/{{HOST}} substitution on deploy
on_deploy = ""             # NEW: post-deploy hook, e.g. "hyprctl reload" or "findmnt --verify /etc/fstab"

[manifest]                 # NEW: only for kind="manifest" targets
type = "packages" | "wine" | "systemd" | "env"
source = "pacman -Qqe"     # generator command or builtin id
output = "packages.list"   # artifact filename in store
```

Rules to implement:
- **Terminology (to avoid `unversioned` overload):**
  - `tracked`: in TOML + baseline committed.
  - `modified`: tracked file whose current `hash` differs from baseline.
  - `perm-drift`: tracked file whose `owner/group/mode` differs (even if content same).
  - `missing`: tracked path no longer exists on disk.
  - `untracked`: new file inside a `dir` target, not yet added.
  - `read-error`: target cannot be read (e.g. permission denied) — surfaced distinctly.
  - Notifications and `status` report `modified|perm-drift|missing|untracked|read-error`, never auto-save.
- **Permissions:** capture `uid→username`, `gid→groupname`, `stat.st_mode` on add as baseline, plus on every `commit`/`status` check. Drift = `perm-drift` state, saved only via explicit `commit` (or `auto_commit` opt-in). Deploy MUST restore `owner/group/mode` (incl. `+x` for snippets) after copy; failures recorded per-target as `error`.
- **Location types:**
  - `text`: normal git versioning + word diff on deploy/status. Supports `template = true` for `{{HOME}}`/`{{HOST}}` substitution on deploy + `hardcoded-path` lint on `add` (warn if absolute `/home/<other>/` inside file).
  - `binary`: git-lfs tracked, default `retention.count = 3` (implement via `git log` cap + `git lfs prune` policy — see Q3), warn if > e.g. 10 MB (configurable `large_file_warn_mb`). Savegames use `retention = {count=30, age="30d"}` opt-in; must handle locked files (copy-then-hash, warn on SQLite WAL).
  - `dir`: snapshot file list + per-file type handling; new files detected as `untracked` (candidate for `target add`), not auto-added unless `auto_add_glob = true` for snippet-style configs. `ignore = [...]` always respected (savegame caches, wine `Temp/`, `*.log`).
  - `manifest` (NEW): artifact is a generated recipe, not a verbatim copy. Types: `packages` (`pacman -Qqe` + AUR helper + `flatpak list`), `wine` (see below), `systemd` (`systemctl list-unit-files --state=enabled`), `env` (`$PATH`, hotkey env). Regenerated on `commit`, replayed as `setup.sh` hint on `deploy`.
  - `symlink`: default `preserve` — store link target string, recreate link on deploy. `follow` dereferences on `commit`. `status` shows dangling links as `missing`.
- **Wine guidance (NEW):** do NOT track whole `WINEPREFIX` as `dir`. Default pattern: one `manifest(type=wine)` capturing `wine --version`, `WINEARCH`, `winetricks list`, installed programs, env vars + selective `text` targets for `*.reg`/`*.cfg` with default `ignore = ["drive_c/users/*/Temp/**", "*.log", "*.tmp", "**/Cache/**", "dosdevices/**"]`. Full-prefix copy is an explicit escape hatch with large-file warning.
- **Machine conditioning (NEW):** `machines = []` means all hosts. If set, `status` still shows drift but `deploy` skips non-matching hosts as `skipped (wrong host)`. Alternative per-machine branches (`host-<hostname>`) documented as escape hatch.
- **Flexibility:**
  - `fixed`: absolute path deploy.
  - `user`: auto-detect if path under `/home/<user>/`; on deploy rewrite prefix to current `$HOME`. No `root` allowed (validate + error).
  - `flexi`: no fixed destination; `deploy_path` required interactively or via `--to` flag during smart deploy.
- **Interest:**
  - `state`: artifact = whole file/dir snapshot.
  - `diff`: artifact = file + patch history emphasis; deploy shows/uses what changed (unified diff preview, apply semantics TBD — at minimum show diff in dry-run/status).

## 4. Phased implementation

### Phase 0 — Scaffolding (0.5–1d)
- [ ] `pyproject.toml`, `src/versioneer` skeleton, `rich/click` CLI stub with global `-C/--config` option.
- [ ] TOML config load/save + validation.
- [ ] Unit tests harness (`pytest`), `ruff`, shell completion stubs (bash/fish/zsh).
- Exit: `versioneer -C foo --help` works.

### Phase 1 — Config & Target Management (core, 2–3d)
- [ ] `versioneer config create --name <short> --path <store-path> --upstream <git-url> [--auto-commit] [--check-interval]` → `git init + lfs install + .gitattributes`, initial commit/push, write TOML in `~/.config/versioneer/`.
- [ ] `versioneer config list/show/remove`.
- [ ] `versioneer target add <path> [--root DIR] [--kind text|binary|dir|manifest|auto] [--flex fixed|user|flexi|auto] [--interest state|diff] [--glob PATTERN] [--ignore PATTERN...] [--symlink preserve|follow] [--machines h1,h2] [--retention COUNT] [--template]` (tracking-start only, per §0):
  - resolve root-relative vs absolute, auto-detect `user` (`/home/*`), reject `root` with user targets.
  - glob expansion for snippet dirs (`~/.local/bin/*`); `dir` targets store `ignore` list; wine/savegame presets apply default ignores.
  - symlink handling: `preserve` stores link target, `follow` dereferences; warn on dangling links.
  - secret scan: warn (never block) if content looks like token/private key; suggest `encrypt=true` or `ignore` (full encryption in Phase 9).
  - hardcoded-path lint: warn if `text` file contains absolute `/home/<other>/` and suggest `--template`.
  - capture owner/group/mode + sha256 as baseline, append to TOML, `git add` + commit that baseline only. Later content changes are NOT committed here.
- [ ] `versioneer target list/remove <path>`: `remove` = stop tracking only — delete TOML entry + `git rm` artifact from store + commit removal. Leave working file untouched, keep git history. No `--purge-history` in v1 (document `git filter-repo` escape hatch, interacts with binary retention Q3).
- [ ] permission re-scan (re-scan updates baseline only as part of a `commit`, otherwise reports `perm-drift` via `status`).
- Exit: tracking-set management works without daemon; no auto-update semantics.

### Phase 2 — Storage layer + Review & Save (git+LFS) (2–3d, parallelizable with P1)
- [ ] `store.py`: init/clone/pull/commit/push, LFS tracking rules (`*.bin`, explicit binary targets), large-file alert. Wrapper only — called by CLI and (only when `auto_commit` opt-in) by daemon/hook; never auto-pushes unless `auto_push=true`.
- [ ] Review (read-only): `versioneer status` (lists `modified|perm-drift|missing|untracked|read-error` + `clean`, honors `machines` filter + `--host` override), `versioneer diff [<target>]` (unified diff for `text`, `stat` summary for `binary/dir`, recipe preview for `manifest`), `versioneer log [<target>]`.
- [ ] Save (explicit by default): `versioneer commit [-m MSG] [--all|<target>...]` (re-hash + update TOML baselines + `git commit`; `--all` for savegame-style bulk saves; locked-file safe copy-then-hash with warning), `versioneer push/pull`. Auto-commit only when `auto_commit=true` (daemon or game-exit hook path).
- [ ] Binary retention policy: per-target `retention = {count, age}` (default `count=3` for `binary`); enforce on commit via history truncation flag or documented `git rebase`/orphan strategy + test. Resolve Q3 before M1. Note: interacts with `plan.json` hash checks + `target remove` history.
- [ ] Corrupt/missing upstream handling + offline mode (`status/commit` work offline, `push/pull` defer with clear message).
- Exit: `add` commits baseline, `status/diff` reviews drift, `commit/push/pull` saves; `push/pull` commands work.

### Phase 3 — Monitor Engine + Service App (3–4d, read-only by default)
- [ ] `monitor.py`: full scan — hash + stat compare vs stored baseline snapshot → `modified|perm-drift|missing|untracked|read-error: [targets]`, respecting `ignore`, `symlink`, `machines`, per-target `check_interval`, and size caps for large save dirs. Same logic backs `status` and daemon (share code, daemon adds notify/auto-commit branch).
- [ ] `daemon.py`: loop over all configs (multiple groups), per-config `notify = true|false`, `check_interval` (`3h` default; `VERSIONEER_INTERVAL` override for tests; `inotify` immediate mode opt-in for savegame/snippet configs via `watchdog`).
  - notify on system start + every interval only if drift set non-empty. Message points to `versioneer status` / `commit`; daemon itself never commits/pushes unless `auto_commit=true` (then commits silently, notifies on `error` only; pushes only if `auto_push=true`). Damping: rapidly churning savegames notify at most once per interval.
  - `sudo` elevation: system unit runs as root only to *read* fixed targets; unreadable files → `read-error` state + log warning + include in notification ("cannot read — elevation required, run `versioneer status` as root"), not a push warning.
  - game-exit hook example: `versioneer commit -C savegames --all -m "post-session"` shipped as systemd/user unit template + docs.
- [ ] systemd units (user + system) + `versioneer service install/enable/disable` glue.
- [ ] `notify.py` via `notify-send`, journal logging.
- Exit: daemon detects drift and notifies correctly; multi-config filtering works; verified daemon performs zero git writes unless `auto_commit` opt-in is set.

### Phase 4 — CLI Watch-to-Add workflow (1–2d)
- [ ] `versioneer watch <dir> [--root DIR] [--glob PATTERN] [--auto-add]`:
  1. snapshot dir (hashes, respecting `ignore`),
  2. wait until Ctrl-C,
  3. diff → list changed files → interactive multi-select (rich prompt) → `target add` each with same root (or auto-add all if `--auto-add`, intended for `~/bin` snippet configs).
- [ ] Implement with `inotify` (`watchdog` lib) + fallback polling for portability.
- Exit: folder-change capture works per design §CLI.

### Phase 5 — Smart Deploy (3–5d, highest complexity)
- [ ] `versioneer deploy [<target>...] [--dry-run]`: selective by default (single savefile/snippet restore supported); list affected files + per-category actions + diffs preview. No writes on dry-run. Honors `machines` allowlist + `--force-host` override.
- [ ] `versioneer deploy --plan-out plan.json`: write `{target, src_hash, dst_hash, action}`. On `deploy --plan plan.json`: re-hash, abort/flag entries with interim edits as deployment errors (hash deviation list).
- [ ] Rollout engine per category:
  - text/binary: backup-before-overwrite (`.bak` + timestamped copy in `~/.local/state/versioneer/backups/`), atomic write (tmp + rename), restore `owner/group/mode` (incl. `+x`), re-exec via `sudo` when destination not writable. If exists+differs prompt overwrite (default N, `--yes/--no-interaction` flags).
  - dir exists: prompt skip (default) | overwrite | merge (merge = rsync-like copy without deleting extras unless `--prune`; document limitation).
  - manifest: regenerate hint — `packages` prints `pacman -S` replay + optional `--apply`; `wine` prints bottle recipe; never blind-copies.
  - symlink: recreate link for `preserve`, copy content for `follow`.
  - template: substitute `{{HOME}}`/`{{HOST}}` when `template=true`.
  - flexi: require `--to <path>` or interactive prompt; fail entry (don't skip whole run) if missing.
  - destination missing → don't deploy that target, record as error.
  - validation + hooks: `on_deploy` runs after copy (e.g. `findmnt --verify`, `hyprctl reload`); hook failure = per-target `error`, not whole-run abort.
- [ ] Deploy-status file: `~/.local/state/versioneer/<config>-deploy-<timestamp>.json` + `latest.json` symlink; deploy reads previous deploy-status as baseline. Never hard-fail: per-target `ok|skipped|error + reason`.
- Exit: dry-run → plan → deploy → deploy-status round-trip works, error-tolerant.

### Phase 6 — Installer / Uninstaller + Bootstrap (1–2d)
- [ ] `install.sh` (+ `PKGBUILD`): install service units, CLI (pipx/uv or system package), shell completions (bash/fish/zsh), help user create storage (prompt upstream, `git lfs install` check, `sops/age` optional check).
- [ ] `versioneer bootstrap <upstream-url-or-config> [--to DIR] [--host HOST]`: clone store(s) + recreate TOML(s) + `deploy --all` (or selective). This is the new-machine path for hypr/firejail/fstab/snippets/savegames/wine-manifests. Document `clone → deploy --dry-run → deploy` flow.
- [ ] `uninstall.sh` / `versioneer uninstall`: stop/disable units, remove CLI, remove `~/.config/versioneer` + `~/.config/versioneer/*` (confirm prompt), optionally keep stores.
- [ ] Colored CLI verified identically under bash/fish/zsh (CI matrix or manual checklist).
- Exit: clean install → use → uninstall leaves no config residue; fresh VM can bootstrap from upstream alone.

### Phase 7 — Hardening & Release (2d)
- [ ] Integration tests: fake `$HOME`, temp git remote, root/user/flexi fixtures, daemon notify mocked, permission-restore + symlink + backup-before-overwrite assertions.
- [ ] Docs: `README` (quickstart per use-case: hypr/firejail, fstab, savegames, wine-manifest, snippets), `man` page, `--help` polish.
- [ ] Arch/CachyOS smoke test (VM or container), LFS round-trip with large binary.

### Phase 8 — Manifests: packages + wine + systemd + env (2–3d) [NEW — unblocks new-machine + wine use-cases]
- [ ] `versioneer manifest --packages --wine --systemd --env -C <config>`: generators:
  - `packages`: `pacman -Qqe` + AUR helper (`yay/paru -Qqm`) + `flatpak list --app` → `packages.list` + replay `setup-packages.sh`.
  - `wine`: `wine --version`, `WINEARCH`, `WINEPREFIX`, `winetricks list-installed`, installed `*.exe` inventory, registry mtime snapshot → `wine-manifest.json` + `setup-wine.sh` recipe skeleton.
  - `systemd`: enabled user+system units list.
  - `env`: `$PATH`, hotkey-relevant env, shell.
- [ ] `manifest` targets behave like `text` for `status/diff/commit` (regenerate on `commit`, diff as unified diff) and print replay instructions on `deploy` (with `--apply` for packages where safe).
- [ ] Wine default excludes + docs: why full-prefix tracking is discouraged, escape hatch for small prefixes, savegame-in-prefix guidance (`prefix/drive_c/.../saves` as separate `binary` target).
- Exit: `commit` regenerates manifests, `deploy` on new machine reproduces package/wine setup without manual cooking.

### Phase 9 — Secrets, Doctor, Templates & Hooks (2–3d) [NEW — hardening]
- [ ] `secrets.py`: secret scan on `add`/`commit` (token/private-key entropy + denylist, warn-only in v1); `encrypt=true` per-target via `sops + age` (encrypt in store, decrypt on deploy); `doctor --secrets` audit.
- [ ] `versioneer doctor [-C config]`: health checks — LFS installed, upstream reachable/auth, disk quota, `read-error` list, large-file warnings vs `large_file_warn_mb`, dangling symlinks, missing `on_deploy` validators, retention/Q3 status. Exit non-zero on errors, used by installer + CI.
- [ ] `template.py` + `hooks.py`: `{{HOME}}`/`{{HOST}}` substitution on deploy when `template=true`, hardcoded-path lint, `on_deploy` validation/restart hooks (`findmnt --verify`, `hyprctl reload`, `firejail --check`); hook output captured in deploy-status file.
- [ ] Snippet ergonomics: `target add --glob` + `watch --auto-add` + `commit --all` shortcut docs; `+x` restore test.
- Exit: `doctor` green on reference VM; secrets warning verified; template + hook round-trip tested.

## 5. Testing strategy
- Unit: config parse/validate, path resolution (root vs user vs flexi), hash/compare, `add` baseline vs `commit` update semantics, `remove` keeps working file + history, plan-file deviation detection, glob/ignore matching, symlink preserve/follow, `{{HOME}}` templating, `machines` filtering, retention config parse.
- Integration: temp git server (local bare repo), add → modify → `status` shows `modified` → `diff` previews → `commit + push` clears drift → `pull` on second clone; remove → `git rm` + history kept → working file intact; deploy restores `owner/mode/+x`, creates `.bak`, atomic tmp+rename, `sudo` re-exec mocked, `on_deploy` hook captured; `bootstrap` from bare upstream reproduces files on fake `$HOME`.
- Service: mocked notifier + accelerated timer (e.g. 10s instead of 3h via env `VERSIONEER_INTERVAL`); assert daemon performs zero git writes by default and emits `read-error` for unreadable fixed targets; with `auto_commit=true` assert single commit per interval (damping) and no push unless `auto_push=true`.
- Manifests: fixture `pacman -Qqe`/`flatpak`/`wine --version` outputs → manifest regenerates + diffs; wine default excludes hide `Temp/*.log`.
- Manual checklist: sudo-owned file read-error, binary > threshold alert, flexi without `--to`, missing destination, `commit --all` for bulk savegame-style changes, locked savegame copy-then-hash warning, `doctor` green, secret-scan warning on fake token, `deploy --dry-run` before fstab overwrite + `findmnt --verify` hook.

## 6. Risks / mitigations
- LFS UX on Arch (user may lack `git-lfs`) → installer checks + auto-suggest `pacman -S git-lfs`; `doctor` re-checks; document quota for savegames/wine.
- `sudo`/ownership edge cases → daemon never pushes (commits only with `auto_commit` opt-in); unreadable files surface as explicit `read-error` state prompting `versioneer status` with elevation; deploy re-execs via `sudo` only for writes + restores perms + keeps `.bak`.
- fstab/hypr deploy danger → mandatory `--dry-run` preview in docs, backup-before-overwrite, validation hooks; never auto-deploy fixed targets.
- Wineprefix size explosion → manifest-only default + default ignores + large-file warning; full-prefix tracking requires explicit `--force` + docs acknowledgment.
- Savegame churn/notify fatigue → per-target `check_interval`, `inotify` opt-in, damping, `auto_commit` opt-in + game-exit hook; `ignore` for caches/logs.
- Dir merge semantics vague ("somehow") → define as non-destructive copy; full 3-way merge out of scope for v1 (`--prune` required for deletes).
- `diff`-interest deploy semantics → v1 = preview diff + deploy state; true patch-apply in v2.
- Secrets in store → scan warns on `add/commit`, `sops/age` for `encrypt=true`, private-repo default in `config create` docs.
- Terminology drift (`unversioned` vs deploy `status` file) → `status/diff/log` = versioning review, `deploy --dry-run/--plan/--status` = rollout; keep file names distinct (`<config>-deploy-<ts>.json`).
- Multi-machine divergence → `machines` allowlist + `{{HOST}}` templating; per-machine branches as escape hatch (document merge cost).

## 7. Suggested milestones (v1)
- M1 (end P1+P2): tracking-set + review & save via CLI (`add/remove` with glob/ignore/symlink, `status/diff/log`, `commit/push/pull`); Q3 retention decided.
- M2 (end P3): background monitoring + notifications pointing to `status`; `auto_commit` opt-in + game-exit hook for savegames.
- M3 (end P5): safe smart deploy (selective, backup, atomic, perm-restore, sudo, hooks) with plan/deploy-status.
- M4 (end P6+P7): installable Arch package + `bootstrap` new-machine flow + docs per use-case.
- M5 (end P8): manifests (packages/wine/systemd/env) — wine without re-cooking, new machine without reinventing hypr/firejail.
- M6 (end P9): `doctor` green, secrets scan + `sops/age`, templates + hooks, snippet ergonomics.

## 8. Immediate next steps
1. Confirm §0 lifecycle + §1 decisions (language, TOML, systemd split, Q3–Q6: retention, encryption backend, multi-machine, manifest scope).
2. Scaffold Phase 0.
3. Implement Phase 1 `config create / target add/remove` (tracking-only, with glob/ignore/symlink lint) + Phase 2 `status/diff/commit/push` + tests.
4. Then Phase 3 daemon intervals/auto-commit opt-in, Phase 5 deploy safety, Phase 8 manifests, Phase 9 doctor/secrets.
