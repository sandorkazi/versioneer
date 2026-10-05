/** Pure helpers for the versioneer OpenCode plugin: arg building, validation, execution.
 *
 * Kept free of OpenCode imports so the logic stays testable outside the runtime.
 */

import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

export interface RunnerOptions {
  /** Versioneer binary. Defaults to $VERSIONEER_BIN or "versioneer". */
  binary?: string;
  /** Per-call timeout in ms. Defaults to 120_000. */
  timeoutMs?: number;
  /** Truncate combined output to this many chars. Defaults to 30_000. */
  maxChars?: number;
}

export interface RunResult {
  exitCode: number;
  stdout: string;
  stderr: string;
  truncated: boolean;
}

const CONFIG_RE = /^[A-Za-z0-9._-]+$/;

export function binaryPath(options?: RunnerOptions): string {
  const raw = (options?.binary ?? process.env.VERSIONEER_BIN ?? "versioneer").trim();
  if (!raw) throw new Error("empty versioneer binary path");
  return raw;
}

/** Resolve a `-C <config>` name or throw a user-facing error. */
export function requireConfig(config: unknown, fallback?: string): string {
  const name = ((typeof config === "string" && config.trim()) || fallback || "").trim();
  if (!name) throw new Error("config is required — pass e.g. { config: \"hypr\" } (versioneer -C hypr …)");
  if (!CONFIG_RE.test(name)) throw new Error(`invalid config name: ${JSON.stringify(name)}`);
  return name;
}

/** Guard for every mutating tool. Opt out via plugin options `{ allowMutations: false }`. */
export function mutationsAllowed(pluginOptions: Record<string, unknown>): boolean {
  return pluginOptions["allowMutations"] !== false;
}

export function refusal(tool: string): string {
  return (
    `refused: ${tool} is a mutating versioneer operation and this plugin instance ` +
    `has allowMutations=false. Run it directly in a terminal instead, e.g. ` +
    "`versioneer -C <config> status` first, then commit/deploy explicitly."
  );
}

export function truncate(text: string, limit: number): { text: string; truncated: boolean } {
  if (text.length <= limit) return { text, truncated: false };
  const head = Math.floor(limit * 0.7);
  const tail = limit - head;
  return {
    text: text.slice(0, head) + `\n…[truncated ${text.length - limit} chars]…\n` + text.slice(text.length - tail),
    truncated: true,
  };
}

export async function runVersioneer(args: string[], options?: RunnerOptions): Promise<RunResult> {
  const bin = binaryPath(options);
  const timeout = options?.timeoutMs ?? 120_000;
  const maxChars = options?.maxChars ?? 30_000;
  try {
    const { stdout, stderr } = await execFileAsync(bin, args, { timeout, maxBuffer: 10 * 1024 * 1024 });
    const t = truncate(`${stdout}${stderr ? `\n--- stderr ---\n${stderr}` : ""}`, maxChars);
    return { exitCode: 0, stdout: String(stdout), stderr: String(stderr), truncated: t.truncated };
  } catch (err: unknown) {
    const e = err as { code?: number; stdout?: unknown; stderr?: unknown; message?: string; killed?: boolean };
    if (typeof e?.code === "number" || typeof e?.stdout !== "undefined") {
      const out = String(e.stdout ?? "");
      const serr = String(e.stderr ?? "");
      const t = truncate(`${out}${serr ? `\n--- stderr ---\n${serr}` : ""}`, maxChars);
      void t;
      return { exitCode: typeof e.code === "number" ? e.code : 1, stdout: out, stderr: serr, truncated: t.truncated };
    }
    if ((e as { code?: string })?.code === "ENOENT") {
      throw new Error(
        `versioneer binary not found: ${JSON.stringify(bin)} — install it (./installer/install.sh --dev) ` +
          `or set plugin option { "binary": "/path/to/versioneer" } / $VERSIONEER_BIN`,
      );
    }
    throw new Error(`failed to run versioneer: ${(e as Error)?.message ?? String(err)}`);
  }
}

export function formatResult(argv: string[], r: RunResult): string {
  const header = `$ versioneer ${argv.join(" ")}\n(exit ${r.exitCode})`;
  const body = `${r.stdout}${r.stderr ? `\n--- stderr ---\n${r.stderr}` : ""}`.trim();
  return `${header}\n${body || "(no output)"}${r.truncated ? "\n[output truncated]" : ""}`;
}
