# opencode-versioneer-plugin

OpenCode plugin that drives the [versioneer](../../README.md) CLI from OpenCode with safe defaults:
read-only drift review freely, mutating operations gated, deploy dry-run-first.

## Install

Option A — local path (this repo):

```jsonc
// opencode.jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "plugins": ["file:///home/masu/versioneer/plugins/opencode-versioneer"],
}
```

Option B — copy `opencode.example.jsonc` from this directory into your project
and adjust paths/options.

Requires: `versioneer` on `PATH` (see repo `installer/install.sh --dev`),
or set the binary explicitly via options / `$VERSIONEER_BIN`.

## Options

```jsonc
{
  "package": "file:///path/to/plugins/opencode-versioneer",
  "options": {
    "binary": "versioneer",   // or absolute path; $VERSIONEER_BIN also works
    "defaultConfig": "hypr",  // fallback when the agent omits `config`
    "allowMutations": true,   // set false to make commit/deploy/target tools refuse
    "timeoutMs": 120000
  },
}
```

Even with `allowMutations: true`, gate mutating tools with OpenCode permissions
(see `opencode.example.jsonc`): read-only tools `allow`, mutating tools `ask`.

## Tools

Read-only: `versioneer_status`, `versioneer_diff`, `versioneer_log`,
`versioneer_config_list`, `versioneer_config_show`, `versioneer_target_list`,
`versioneer_doctor`, `versioneer_service_check`.

Preview (no writes): `versioneer_deploy_preview` (always `--dry-run`),
`versioneer_bootstrap_preview` (always `--dry-run`).

Mutating (need approval + `allowMutations`): `versioneer_commit`,
`versioneer_push`, `versioneer_pull`, `versioneer_deploy_apply` (requires
`confirm: true` after a shown preview), `versioneer_target_add`,
`versioneer_target_remove`.

Also registers the `versioneer` skill and three commands:
`/versioneer-status`, `/versioneer-commit`, `/versioneer-deploy`.

## Safety model

- Review (`status`/`diff`) always precedes save or restore.
- Live deploy requires a shown `--dry-run` preview + `confirm: true`.
- `/etc/fstab` and system paths: explicit user go-ahead after preview; keep `.bak`.
- Manifest replay stays print-only unless the user asks for `apply: true`.
- Status/commit work offline; push/pull/deploys against remotes fail safe with a message.
