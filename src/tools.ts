import { spawn } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { listBlendFiles, relativeTo, resolveInside, sidecarDir } from "./blendFiles";
import { callBridge, probeBridge } from "./bridge";
import { buildContextPack } from "./contextPack";
import { runDoctor } from "./doctor";
import { gotchasFor } from "./gotchas";
import { ingestScript, runIngest } from "./ingest";
import { readConfig } from "./projectConfig";
import { clampPort, DEFAULT_CLIENTS, DEFAULT_PORT, ToolOutcome, WorkspaceContext } from "./types";

const PREVIEW_BYTES = 1_500_000;
const LIVE_KEEP = 20;

export function loadWorkspaceContext(env: NodeJS.ProcessEnv = process.env, cwd = process.cwd()): WorkspaceContext {
  const fromEnv = env["VSBLENDER_WORKSPACE"];
  const workspace = fromEnv && fs.existsSync(fromEnv) ? fromEnv : cwd;
  const rootEnv = env["VSBLENDER_EXTENSION_ROOT"];
  const extensionRoot = rootEnv && fs.existsSync(rootEnv) ? rootEnv : path.resolve(__dirname, "..");
  const config = readConfig(workspace);
  const port = env["VSBLENDER_PORT"]
    ? clampPort(Number(env["VSBLENDER_PORT"]), config?.port ?? DEFAULT_PORT)
    : (config?.port ?? DEFAULT_PORT);
  const blender = env["VSBLENDER_BLENDER"] || config?.blender;
  return {
    workspace,
    extensionRoot,
    port,
    ...(blender ? { blender } : {}),
    clients: config?.clients ?? DEFAULT_CLIENTS,
    replaceLegacy: config?.replaceLegacy ?? true,
  };
}

function ok(text: string, extra?: Partial<ToolOutcome>): ToolOutcome {
  return { ok: true, text, ...extra };
}

function fail(text: string, details?: unknown): ToolOutcome {
  return { ok: false, text, ...(details !== undefined ? { details } : {}) };
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function pickBlend(ctx: WorkspaceContext, requested: unknown): string {
  if (typeof requested === "string" && requested.trim()) {
    const abs = resolveInside(ctx.workspace, requested);
    if (!abs.toLowerCase().endsWith(".blend")) throw new Error("path must be a .blend file");
    if (!fs.existsSync(abs)) throw new Error(`file not found: ${requested}`);
    return abs;
  }
  const files = listBlendFiles(ctx.workspace);
  const only = files.length === 1 ? files[0] : undefined;
  if (only) return only;
  if (!files.length) throw new Error("no .blend files in the workspace");
  const names = files.map((file) => relativeTo(ctx.workspace, file)).join("\n");
  throw new Error(`more than one .blend file. Pass path.\n${names}`);
}

async function waitForBridge(port: number, timeoutMs: number): Promise<boolean> {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const probe = await probeBridge(port);
    if (probe.ok) return true;
    if (probe.state !== "absent" && probe.state !== "unresponsive") return false;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return false;
}

/** Say what actually went wrong: a slow Blender is not an unreachable one. */
function bridgeFailure(port: number, error: unknown, timeoutMs: number): string {
  const message = errorText(error);
  if (/ECONNREFUSED/i.test(message)) {
    return `Nothing is listening on 127.0.0.1:${port}. Call launch_blender, or start the bridge in Blender (sidebar > VSBlender).`;
  }
  if (/timed out/i.test(message)) {
    return `Blender did not answer within ${Math.round(timeoutMs / 1000)}s. It may still be working on this request; call session_info before retrying.`;
  }
  return `Blender bridge on port ${port}: ${message}`;
}

export async function doctor(ctx: WorkspaceContext, args: Record<string, unknown>): Promise<ToolOutcome> {
  const report = await runDoctor(ctx, { fix: args["fix"] === true, electronPath: process.execPath });
  return report.ok ? ok(report.text, { details: report.checks }) : fail(report.text, report.checks);
}

export async function sessionInfo(ctx: WorkspaceContext): Promise<ToolOutcome> {
  try {
    const response = await callBridge(ctx.port, "session_info", {}, 8000);
    if (!response.ok) return fail(response.error || "session_info failed");
    const version = String(response.result?.["blender_version"] ?? "");
    const info = {
      ...response.result,
      workspace: ctx.workspace,
      port: ctx.port,
      gotchas: gotchasFor(version),
    };
    return ok(JSON.stringify(info, null, 2), { details: info });
  } catch (error) {
    return fail(bridgeFailure(ctx.port, error, 8000));
  }
}

export async function launchBlender(ctx: WorkspaceContext, args: Record<string, unknown>): Promise<ToolOutcome> {
  if (!ctx.blender || !fs.existsSync(ctx.blender)) {
    return fail("No Blender executable is configured. Run VSBlender: Setup.");
  }
  let file: string | undefined;
  try {
    if (typeof args["file"] === "string" && args["file"].trim()) file = pickBlend(ctx, args["file"]);
  } catch (error) {
    return fail(errorText(error));
  }
  const background = args["background"] === true;
  const probe = await probeBridge(ctx.port);
  if (probe.ok) {
    if (!file) return ok(`Blender bridge already listening on ${ctx.port}.`);
    try {
      const opened = await callBridge(ctx.port, "open_file", { path: file }, 90000);
      if (opened.ok && opened.result?.["opened"] === true) {
        const already = opened.result?.["already"] === true;
        return ok(already ? `Already open: ${file}` : `Opened ${file}`);
      }
      const reason = String(opened.result?.["reason"] ?? opened.error ?? "could not open the file");
      return fail(`${reason}. The bridge on port ${ctx.port} was left on the file it already had open.`);
    } catch (error) {
      const back = await waitForBridge(ctx.port, 20000);
      if (back) return ok(`Asked Blender to open ${file}. The bridge is listening again. ${errorText(error)}`);
      return fail(`Opening the file dropped the bridge and it did not come back. ${errorText(error)}`);
    }
  }
  // Only start Blender when nothing holds the port. A Blender that is busy (rendering, running a
  // long script) still owns it, and a second one would either fail to bind or fight over requests.
  if (probe.state !== "absent") return fail(`${probe.detail}. Not starting another Blender.`);
  // A new Blender opens the workspace's .blend when there is only one. A running one is never
  // switched to another file unless a file was named.
  if (!file) {
    const files = listBlendFiles(ctx.workspace);
    if (files.length === 1) file = files[0];
  }

  const starter = path.join(ctx.extensionRoot, "resources", "start_bridge.py");
  const addon = path.join(ctx.extensionRoot, "resources", "addon", "vsblender_bridge");
  if (!fs.existsSync(starter)) return fail(`missing ${starter}`);
  const launchArgs = [...(background ? ["-b"] : []), ...(file ? [file] : []), "--python", starter];
  const logPath = path.join(ctx.workspace, ".blender-ai", "launch.log");
  fs.mkdirSync(path.dirname(logPath), { recursive: true });
  const logFd = fs.openSync(logPath, "a");
  const child = spawn(ctx.blender, launchArgs, {
    detached: true,
    windowsHide: background,
    stdio: ["ignore", logFd, logFd],
    env: {
      ...process.env,
      VSBLENDER_ADDON_SRC: addon,
      VSBLENDER_PORT: String(ctx.port),
      VSBLENDER_WORKSPACE: ctx.workspace,
    },
  });
  child.unref();
  fs.closeSync(logFd);
  const up = await waitForBridge(ctx.port, 90000);
  if (!up) {
    const tail = fs.existsSync(logPath) ? fs.readFileSync(logPath, "utf8").slice(-1500) : "";
    return fail(`Blender did not open the bridge on ${ctx.port}.\n${tail}`.trim());
  }
  return ok(`Blender is listening on ${ctx.port}${file ? ` with ${relativeTo(ctx.workspace, file)}` : ""}. pid ${child.pid ?? "?"}`);
}

export async function runScript(ctx: WorkspaceContext, args: Record<string, unknown>): Promise<ToolOutcome> {
  if (typeof args["path"] !== "string" || !args["path"].trim()) return fail("path is required");
  let script: string;
  try {
    script = resolveInside(ctx.workspace, args["path"]);
  } catch (error) {
    return fail(errorText(error));
  }
  if (!script.toLowerCase().endsWith(".py")) return fail("path must be a .py file");
  if (!fs.existsSync(script)) return fail(`file not found: ${args["path"]}`);
  const params: Record<string, unknown> = { path: script };
  if (typeof args["function"] === "string" && args["function"]) params["function"] = args["function"];
  if (args["args"] && typeof args["args"] === "object") params["args"] = args["args"];
  const timeout = Math.min(300000, Math.max(1000, Number(args["timeout_ms"]) || 60000));
  try {
    const response = await callBridge(ctx.port, "run_script", params, timeout);
    if (!response.ok) {
      // The add-on already puts the traceback from the user's file:line, stdout and partial changes in error.
      return fail(`file: ${relativeTo(ctx.workspace, script)}\n${response.error || "run_script failed"}`, { changed: response.changed ?? [] });
    }
    const result = response.result ?? {};
    const lines = [
      `file: ${relativeTo(ctx.workspace, script)}`,
      `result: ${JSON.stringify(result["result"])}`,
      `changed: ${JSON.stringify(response.changed ?? [])}`,
    ];
    if (response.warnings?.length) lines.push(`warnings:\n${response.warnings.join("\n")}`);
    const stdout = typeof result["stdout"] === "string" ? result["stdout"] : "";
    const stderr = typeof result["stderr"] === "string" ? result["stderr"] : "";
    if (stdout.trim()) lines.push(`stdout:\n${stdout.trimEnd()}`);
    if (stderr.trim()) lines.push(`stderr:\n${stderr.trimEnd()}`);
    return ok(lines.join("\n"), { details: result });
  } catch (error) {
    return fail(bridgeFailure(ctx.port, error, timeout));
  }
}

export async function ingest(ctx: WorkspaceContext, args: Record<string, unknown>): Promise<ToolOutcome> {
  if (!ctx.blender || !fs.existsSync(ctx.blender)) return fail("No Blender executable is configured. Run VSBlender: Setup.");
  let blend: string;
  try {
    blend = pickBlend(ctx, args["path"]);
  } catch (error) {
    return fail(errorText(error));
  }
  const script = ingestScript(ctx.extensionRoot);
  if (!fs.existsSync(script)) return fail(`blender_ingest.py was not found at ${script}`);
  const result = await runIngest({
    blender: ctx.blender,
    blendFile: blend,
    script,
    force: args["force"] === true,
    previews: args["previews"] !== false,
    actor: typeof args["actor"] === "string" ? args["actor"] : "ai",
    reason: typeof args["reason"] === "string" ? args["reason"] : "",
  });
  const where = result.out ? relativeTo(ctx.workspace, result.out) : relativeTo(ctx.workspace, sidecarDir(blend));
  if (result.status === "error") return fail(`${result.error ?? "ingest failed"}\n${result.log}`.trim());
  const summary = [
    `status: ${result.status}`,
    `out: ${where}`,
    result.objects !== undefined ? `objects: ${result.objects}` : "",
    result.changes !== undefined ? `changes: ${result.changes}` : "",
    result.issues !== undefined ? `issues: ${result.issues}` : "",
    result.previews !== undefined ? `previews: ${result.previews}` : "",
    result.seconds !== undefined ? `seconds: ${result.seconds}` : "",
  ].filter(Boolean).join("\n");
  return ok(summary, { details: result });
}

export async function contextPack(ctx: WorkspaceContext, args: Record<string, unknown>): Promise<ToolOutcome> {
  let blend: string;
  try {
    blend = pickBlend(ctx, args["path"]);
  } catch (error) {
    return fail(errorText(error));
  }
  const budget = Math.max(64, Math.min(20000, Number(args["budget_tokens"]) || 2000));
  const focus = typeof args["focus"] === "string" ? args["focus"] : undefined;
  const packed = buildContextPack(ctx.workspace, blend, budget, focus);
  const header = packed.truncated ? "truncated: true\n" : "";
  return ok(`${header}tokens: ${packed.tokens}\n\n${packed.text}`, { details: { tokens: packed.tokens, truncated: packed.truncated } });
}

export async function preview(ctx: WorkspaceContext, args: Record<string, unknown>): Promise<ToolOutcome> {
  const view = typeof args["view"] === "string" ? args["view"] : "iso";
  const shading = typeof args["shading"] === "string" ? args["shading"] : "solid";
  const size = Math.max(64, Math.min(2048, Number(args["size"]) || 512));
  const outDir = path.join(os.tmpdir(), "vsblender");
  fs.mkdirSync(outDir, { recursive: true });
  const out = path.join(outDir, `preview-${view}-${Date.now()}.png`);
  const params: Record<string, unknown> = { view, shading, size, out };
  if (typeof args["target"] === "string" && args["target"]) params["target"] = args["target"];
  if (args["frame"] !== undefined && args["frame"] !== null && args["frame"] !== "") params["frame"] = Number(args["frame"]);
  try {
    const response = await callBridge(ctx.port, "preview", params, 120000);
    if (!response.ok) return fail(response.error || "preview failed");
    const file = String(response.result?.["file"] ?? out);
    if (!fs.existsSync(file)) return fail(`Blender reported ${file}, but the file is not there`);
    const liveDir = path.join(ctx.workspace, ".blender-ai", "live");
    const kept = path.join(liveDir, path.basename(file));
    fs.mkdirSync(liveDir, { recursive: true });
    fs.copyFileSync(file, kept);
    const size = fs.statSync(kept).size;
    const data = size <= PREVIEW_BYTES ? fs.readFileSync(kept).toString("base64") : undefined;
    if (path.resolve(file) !== path.resolve(kept)) fs.rmSync(file, { force: true });
    pruneLive(liveDir, LIVE_KEEP);
    const warnings = response.warnings?.length ? `warnings: ${response.warnings.join("; ")}` : "";
    const text = [
      `view: ${response.result?.["view"] ?? view}`,
      `shading: ${response.result?.["shading"] ?? shading}`,
      `engine: ${response.result?.["engine"] ?? ""}`,
      `file: ${relativeTo(ctx.workspace, kept)}`,
      warnings,
      data ? "" : `image omitted from the tool result (${size} bytes). Open the file path.`,
    ].filter(Boolean).join("\n");
    return ok(text, { ...(data ? { images: [{ mimeType: "image/png", data }] } : {}), details: response.result });
  } catch (error) {
    return fail(bridgeFailure(ctx.port, error, 120000));
  }
}

/** Keep the newest previews only; this folder is a scratch view of what the AI saw, not history. */
function pruneLive(dir: string, keep: number): void {
  let files: { full: string; mtime: number }[];
  try {
    files = fs.readdirSync(dir)
      .filter((name) => name.toLowerCase().endsWith(".png"))
      .map((name) => {
        const full = path.join(dir, name);
        return { full, mtime: fs.statSync(full).mtimeMs };
      });
  } catch {
    return;
  }
  files.sort((a, b) => b.mtime - a.mtime);
  for (const item of files.slice(keep)) fs.rmSync(item.full, { force: true });
}

export async function callTool(ctx: WorkspaceContext, name: string, args: Record<string, unknown>): Promise<ToolOutcome> {
  switch (name) {
    case "doctor":
      return doctor(ctx, args);
    case "session_info":
      return sessionInfo(ctx);
    case "launch_blender":
      return launchBlender(ctx, args);
    case "run_script":
      return runScript(ctx, args);
    case "ingest":
      return ingest(ctx, args);
    case "context_pack":
      return contextPack(ctx, args);
    case "preview":
      return preview(ctx, args);
    default:
      return fail(`unknown tool ${name}`);
  }
}
