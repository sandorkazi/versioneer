/** opencode-versioneer-plugin: drive the versioneer CLI from OpenCode with safe defaults.
 *
 * Read-only tools (status/diff/log/doctor/…) run freely. Mutating tools
 * (commit/push/deploy/target add/…) are registered too but refuse unless the
 * plugin instance allows mutations (default: allowed — gate with OpenCode
 * permissions `ask`, or set plugin option `{ "allowMutations": false }`).
 * Deploy defaults to `--dry-run`; the live `versioneer_deploy_apply` tool
 * requires an explicit `confirm: true` input plus a plan preview first.
 */

import { Plugin } from "@opencode/plugin";
import {
  formatResult,
  mutationsAllowed,
  refusal,
  requireConfig,
  runVersioneer,
  type RunnerOptions,
} from "./versioneer.js";

type Ctx = any;
type Editor = any;

const READONLY_DESCRIPTION_SUFFIX = "Read-only: makes no changes to configs, stores, or the filesystem.";

const SKILL_ID = "versioneer";
const SKILL_NAME = "Versioneer";
const SKILL_DESCRIPTION =
  "Track, review, save, and restore versioned Linux configs with versioneer (status/diff/commit/deploy). Use for drift review, safe deploys, and new-machine bootstraps.";
const SKILL_BODY = `\
# Versioneer workflow

Versioneer is plain git with safe rollout for Linux configs. Every command is scoped
with \`-C/--config <name>\` (e.g. \`hypr\`, \`etc\`, \`savegames\`).

## Lifecycle (always in this order)

1. **Review first (read-only):** \`versioneer_status\`, then \`versioneer_diff\` for what changed.
   Drift states: \`modified\` | \`perm-drift\` | \`missing\` | \`untracked\` | \`read-error\` | \`clean\`.
2. **Save explicitly:** \`versioneer_commit\` with a message (\`--all\` for savegame-style bulk),
   then \`versioneer_push\`. Status/commit work offline; push/pull defer with a message.
3. **Restore safely:** ALWAYS run \`versioneer_deploy_preview\` (\`--dry-run\`) before
   \`versioneer_deploy_apply\`. Deploy does backup-before-overwrite (.bak), atomic
   tmp+rename, and permission restore per target (ok|skipped|error, never whole-run abort).
4. **Health:** \`versioneer_doctor\` when something looks wrong; \`versioneer_log\` for history.

## Rules

- Never commit or deploy without showing the user the status/diff first.
- Never run live deploy on \`/etc/fstab\` or system paths without an explicit user go-ahead
  after a dry-run preview. Remind them to keep the .bak and a live USB for fstab.
- \`flexi\` targets need \`--to <path>\`; \`machines\`-restricted targets may report
  \`skipped (wrong host)\` — that is expected, not an error.
- Manifests (\`packages\`/\`wine\`/\`systemd\`/\`env\`) only print replay instructions unless
  the user explicitly asks for \`--apply\`.
- Secret-scan and encrypt warnings are warn-only in v1: surface them, don't block on them.
- For large binaries/savegames prefer keep-last-N remotes or bounded retention; flag
  frequent auto-commit space warnings from daemon output.
`;

function runnerOptions(ctx: Ctx): RunnerOptions {
  const o = (ctx.options ?? {}) as Record<string, unknown>;
  return {
    binary: typeof o["binary"] === "string" ? (o["binary"] as string) : undefined,
    timeoutMs: typeof o["timeoutMs"] === "number" ? (o["timeoutMs"] as number) : undefined,
  };
}

function fallbackConfig(ctx: Ctx): string | undefined {
  const o = (ctx.options ?? {}) as Record<string, unknown>;
  return typeof o["defaultConfig"] === "string" ? (o["defaultConfig"] as string) : undefined;
}

async function run(ctx: Ctx, argv: string[]): Promise<string> {
  const r = await runVersioneer(argv, runnerOptions(ctx));
  return formatResult(argv, r);
}

function withConfig(ctx: Ctx, config: unknown): string[] {
  return ["-C", requireConfig(config, fallbackConfig(ctx))];
}

export default Plugin.define({
  id: "versioneer",
  async setup(ctx: Ctx) {
    const opts = ((ctx.options ?? {}) as Record<string, unknown>) ?? {};

    await ctx.tool.transform((editor: Editor) => {
      editor.namespace({
        name: "versioneer",
        description: "Versioned Linux config tracking: drift review, commit/push, safe deploy.",
      });

      const add = (tool: Record<string, unknown>) => editor.add(tool);

      // ---------- read-only ----------
      add({
        name: "status",
        description: `Show versioneer drift for a config (modified|perm-drift|missing|untracked|read-error|clean). ${READONLY_DESCRIPTION_SUFFIX}`,
        input: {
          type: "object",
          properties: {
            config: { type: "string", description: "Config name, e.g. hypr" },
            host: { type: "string", description: "Simulate a different hostname (machines filter)" },
          },
          required: ["config"],
          additionalProperties: false,
        },
        execute: async (input: any) => {
          const argv = [...withConfig(ctx, input.config), "status"];
          if (input.host) argv.push("--host", String(input.host));
          return { content: await run(ctx, argv) };
        },
      });

      add({
        name: "diff",
        description: `Unified diff (text) / stat summary (binary/dir) for a config. ${READONLY_DESCRIPTION_SUFFIX}`,
        input: {
          type: "object",
          properties: {
            config: { type: "string" },
            target: { type: "string", description: "Optional single target path to diff" },
          },
          required: ["config"],
          additionalProperties: false,
        },
        execute: async (input: any) => {
          const argv = [...withConfig(ctx, input.config), "diff"];
          if (input.target) argv.push(String(input.target));
          return { content: await run(ctx, argv) };
        },
      });

      add({
        name: "log",
        description: `Show store git history for a config. ${READONLY_DESCRIPTION_SUFFIX}`,
        input: {
          type: "object",
          properties: {
            config: { type: "string" },
            target: { type: "string" },
            limit: { type: "number", description: "Max entries (maps to -n)" },
          },
          required: ["config"],
          additionalProperties: false,
        },
        execute: async (input: any) => {
          const argv = [...withConfig(ctx, input.config), "log"];
          if (typeof input.limit === "number") argv.push("-n", String(Math.max(1, Math.floor(input.limit))));
          if (input.target) argv.push(String(input.target));
          return { content: await run(ctx, argv) };
        },
      });

      add({
        name: "config_list",
        description: `List versioneer configs. ${READONLY_DESCRIPTION_SUFFIX}`,
        input: { type: "object", properties: {}, additionalProperties: false },
        execute: async () => ({ content: await run(ctx, ["config", "list"]) }),
      });

      add({
        name: "config_show",
        description: `Show a config's meta + target count. ${READONLY_DESCRIPTION_SUFFIX}`,
        input: {
          type: "object",
          properties: { config: { type: "string" } },
          required: ["config"],
          additionalProperties: false,
        },
        execute: async (input: any) => ({
          content: await run(ctx, [...withConfig(ctx, input.config), "config", "show"]),
        }),
      });

      add({
        name: "target_list",
        description: `List tracked targets (kind/flex/hash/owner). ${READONLY_DESCRIPTION_SUFFIX}`,
        input: {
          type: "object",
          properties: { config: { type: "string" } },
          required: ["config"],
          additionalProperties: false,
        },
        execute: async (input: any) => ({
          content: await run(ctx, [...withConfig(ctx, input.config), "target", "list"]),
        }),
      });

      add({
        name: "doctor",
        description: `Run versioneer health checks (upstream, disk, read-errors, symlinks, retention). ${READONLY_DESCRIPTION_SUFFIX}`,
        input: {
          type: "object",
          properties: {
            config: { type: "string", description: "Omit for all configs" },
            secrets: { type: "boolean", description: "Secret audit only (--secrets)" },
          },
          additionalProperties: false,
        },
        execute: async (input: any) => {
          const argv = ["doctor"];
          if (input.config) argv.push(...withConfig(ctx, input.config));
          if (input.secrets) argv.push("--secrets");
          return { content: await run(ctx, argv) };
        },
      });

      add({
        name: "service_check",
        description: `One read-only monitor pass without systemd (service check). ${READONLY_DESCRIPTION_SUFFIX}`,
        input: {
          type: "object",
          properties: {
            config: { type: "string", description: "Omit with all=true for every config" },
            all: { type: "boolean" },
            host: { type: "string" },
          },
          additionalProperties: false,
        },
        execute: async (input: any) => {
          const argv = ["service", "check"];
          if (input.all) argv.push("--all");
          else argv.push(...withConfig(ctx, input.config));
          if (input.host) argv.push("--host", String(input.host));
          return { content: await run(ctx, argv) };
        },
      });

      // ---------- mutating (gated) ----------
      const gated = (name: string, description: string, input: unknown, execute: (i: any) => Promise<string>) =>
        add({
          name,
          description: `${description} MUTATING: changes versioned state or the filesystem. Only run after showing status/diff and with user approval.`,
          input,
          execute: async (i: any) => {
            if (!mutationsAllowed(opts)) return { content: refusal(`versioneer_${name}`) };
            return { content: await execute(i) };
          },
        });

      gated(
        "commit",
        "Commit drift for selected targets (or --all) with a message.",
        {
          type: "object",
          properties: {
            config: { type: "string" },
            message: { type: "string", description: "Commit message (-m)" },
            targets: { type: "array", items: { type: "string" }, description: "Target paths; omit with all=true" },
            all: { type: "boolean", description: "Commit all drifted targets" },
          },
          required: ["config", "message"],
          additionalProperties: false,
        },
        async (input: any) => {
          if (!String(input.message || "").trim()) throw new Error("commit message is required");
          const argv = [...withConfig(ctx, input.config), "commit", "-m", String(input.message)];
          if (input.all) argv.push("--all");
          else if (Array.isArray(input.targets) && input.targets.length) argv.push(...input.targets.map(String));
          else throw new Error("pass targets=[...] or all=true");
          return run(ctx, argv);
        },
      );

      gated(
        "push",
        "Push a config store to its upstream (offline-safe: defers when unreachable).",
        {
          type: "object",
          properties: { config: { type: "string" } },
          required: ["config"],
          additionalProperties: false,
        },
        async (input: any) => run(ctx, [...withConfig(ctx, input.config), "push"]),
      );

      gated(
        "pull",
        "Pull a config store from its upstream.",
        {
          type: "object",
          properties: { config: { type: "string" } },
          required: ["config"],
          additionalProperties: false,
        },
        async (input: any) => run(ctx, [...withConfig(ctx, input.config), "pull"]),
      );

      add({
        name: "deploy_preview",
        description:
          "Preview a deploy with --dry-run: actions + diffs, no writes. Always run this before deploy_apply and show it to the user.",
        input: {
          type: "object",
          properties: {
            config: { type: "string" },
            targets: { type: "array", items: { type: "string" } },
            to: { type: "string", description: "Preview path for flexi/fixed targets (--to)" },
            host: { type: "string", description: "Simulate a hostname for the machines filter" },
          },
          required: ["config"],
          additionalProperties: false,
        },
        execute: async (input: any) => {
          const argv = [...withConfig(ctx, input.config), "deploy", "--dry-run"];
          if (Array.isArray(input.targets)) argv.push(...input.targets.map(String));
          if (input.to) argv.push("--to", String(input.to));
          if (input.host) argv.push("--host", String(input.host));
          return { content: await run(ctx, argv) };
        },
      });

      gated(
        "deploy_apply",
        "Apply a deploy for real (backup → atomic write → perm restore). Requires a prior deploy_preview shown to the user and confirm=true.",
        {
          type: "object",
          properties: {
            config: { type: "string" },
            targets: { type: "array", items: { type: "string" } },
            to: { type: "string" },
            prune: { type: "boolean", description: "Allow dir merge to delete extras" },
            apply: { type: "boolean", description: "Manifest opt-in (packages/systemd/wine replay)" },
            forceHost: { type: "boolean", description: "Override machines allowlist (testing only)" },
            noInteraction: { type: "boolean", description: "Skip overwrite prompts (default answers No)" },
            confirm: { type: "boolean", description: "MUST be true: user saw the dry-run and approved" },
          },
          required: ["config", "confirm"],
          additionalProperties: false,
        },
        async (input: any) => {
          if (input.confirm !== true) throw new Error("deploy_apply requires confirm=true after a shown dry-run preview");
          const argv = [...withConfig(ctx, input.config), "deploy"];
          if (Array.isArray(input.targets)) argv.push(...input.targets.map(String));
          if (input.to) argv.push("--to", String(input.to));
          if (input.prune) argv.push("--prune");
          if (input.apply) argv.push("--apply");
          if (input.forceHost) argv.push("--force-host");
          argv.push(input.noInteraction === false ? "--yes" : "--no-interaction");
          return run(ctx, argv);
        },
      );

      gated(
        "target_add",
        "Start tracking a path (baseline + commit, no backfill). Prefer explicit kind/flex for /etc files.",
        {
          type: "object",
          properties: {
            config: { type: "string" },
            path: { type: "string" },
            kind: { type: "string", enum: ["text", "binary", "dir", "auto"] },
            flex: { type: "string", enum: ["fixed", "user", "flexi", "auto"] },
            ignore: { type: "array", items: { type: "string" } },
          },
          required: ["config", "path"],
          additionalProperties: false,
        },
        async (input: any) => {
          const argv = [...withConfig(ctx, input.config), "target", "add", String(input.path)];
          argv.push("--kind", input.kind || "auto", "--flex", input.flex || "auto");
          for (const ig of input.ignore ?? []) argv.push("--ignore", String(ig));
          return run(ctx, argv);
        },
      );

      gated(
        "target_remove",
        "Stop tracking a path (keeps the file on disk + git history).",
        {
          type: "object",
          properties: {
            config: { type: "string" },
            path: { type: "string" },
          },
          required: ["config", "path"],
          additionalProperties: false,
        },
        async (input: any) => run(ctx, [...withConfig(ctx, input.config), "target", "remove", String(input.path)]),
      );

      gated(
        "bootstrap_preview",
        "Preview cloning a store on a new machine (bootstrap --dry-run, no writes).",
        {
          type: "object",
          properties: {
            url: { type: "string", description: "Upstream git URL" },
            host: { type: "string" },
          },
          required: ["url"],
          additionalProperties: false,
        },
        async (input: any) => {
          const argv = ["bootstrap", String(input.url), "--dry-run"];
          if (input.host) argv.push("--host", String(input.host));
          return run(ctx, argv);
        },
      );
    });

    await ctx.skill.transform((editor: Editor) => {
      editor.add({
        id: SKILL_ID,
        name: SKILL_NAME,
        description: SKILL_DESCRIPTION,
        path: "skills/versioneer/SKILL.md",
        content: SKILL_BODY,
      });
    });

    await ctx.command.transform((editor: Editor) => {
      editor.add({
        name: "versioneer-status",
        description: "Review versioneer drift for a config (status + diff)",
        execute: async ({ sessionID, prompt, delivery }: any) => {
          const arg = (prompt.text ?? "").trim();
          await ctx.session.prompt({
            ...prompt,
            sessionID,
            text: `Use the versioneer_status tool for config ${arg || "<config>"} (ask which config if unclear), then versioneer_diff, and summarize drift states (modified|perm-drift|missing|untracked|read-error|clean) with the next action per target. Do not commit or deploy.`,
            delivery,
          });
        },
      });
      editor.add({
        name: "versioneer-commit",
        description: "Guided versioneer commit: review drift, then save with a message",
        execute: async ({ sessionID, prompt, delivery }: any) => {
          const arg = (prompt.text ?? "").trim();
          await ctx.session.prompt({
            ...prompt,
            sessionID,
            text: `For versioneer config ${arg || "<config>"}: 1) run versioneer_status + versioneer_diff and show them, 2) ask which targets to save and for a commit message, 3) only then run versioneer_commit (targets or all=true) followed by versioneer_push. Never commit blind.`,
            delivery,
          });
        },
      });
      editor.add({
        name: "versioneer-deploy",
        description: "Safe versioneer deploy: dry-run preview first, apply only on approval",
        execute: async ({ sessionID, prompt, delivery }: any) => {
          const arg = (prompt.text ?? "").trim();
          await ctx.session.prompt({
            ...prompt,
            sessionID,
            text: `For versioneer config ${arg || "<config>"}: 1) run versioneer_deploy_preview and show the full preview, 2) wait for explicit approval (especially for /etc paths — remind about .bak + findmnt --verify + live USB for fstab), 3) only then run versioneer_deploy_apply with confirm=true. Manifest replays stay print-only unless the user asks for apply=true.`,
            delivery,
          });
        },
      });
    });
  },
});
