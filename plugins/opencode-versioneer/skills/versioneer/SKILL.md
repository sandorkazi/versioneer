---
name: Versioneer
description: Track, review, save, and restore versioned Linux configs with versioneer (status/diff/commit/deploy). Use for drift review, safe deploys, and new-machine bootstraps.
---

# Versioneer workflow

Versioneer is plain git with safe rollout for Linux configs. Every command is scoped
with `-C/--config <name>` (e.g. `hypr`, `etc`, `savegames`).

## Lifecycle (always in this order)

1. **Review first (read-only):** `versioneer_status`, then `versioneer_diff` for what changed.
   Drift states: `modified` | `perm-drift` | `missing` | `untracked` | `read-error` | `clean`.
2. **Save explicitly:** `versioneer_commit` with a message (`--all` for savegame-style bulk),
   then `versioneer_push`. Status/commit work offline; push/pull defer with a message.
3. **Restore safely:** ALWAYS run `versioneer_deploy_preview` (`--dry-run`) before
   `versioneer_deploy_apply`. Deploy does backup-before-overwrite (.bak), atomic
   tmp+rename, and permission restore per target (ok|skipped|error, never whole-run abort).
4. **Health:** `versioneer_doctor` when something looks wrong; `versioneer_log` for history.

## Rules

- Never commit or deploy without showing the user the status/diff first.
- Never run live deploy on `/etc/fstab` or system paths without an explicit user go-ahead
  after a dry-run preview. Remind them to keep the .bak and a live USB for fstab.
- `flexi` targets need `--to <path>`; `machines`-restricted targets may report
  `skipped (wrong host)` — that is expected, not an error.
- Manifests (`packages`/`wine`/`systemd`/`env`) only print replay instructions unless
  the user explicitly asks for `--apply`.
- Secret-scan and encrypt warnings are warn-only in v1: surface them, don't block on them.
- For large binaries/savegames prefer keep-last-N remotes or bounded retention; flag
  frequent auto-commit space warnings from daemon output.

## Tool map

| Goal | Tool |
| ---- | ---- |
| drift overview | `versioneer_status` |
| what changed | `versioneer_diff` |
| history | `versioneer_log` |
| configs / targets | `versioneer_config_list`, `versioneer_config_show`, `versioneer_target_list` |
| health / monitor pass | `versioneer_doctor`, `versioneer_service_check` |
| save | `versioneer_commit`, `versioneer_push`, `versioneer_pull` |
| restore | `versioneer_deploy_preview` → `versioneer_deploy_apply(confirm=true)` |
| track / untrack | `versioneer_target_add`, `versioneer_target_remove` |
| new machine (preview) | `versioneer_bootstrap_preview` |
