import { spawn } from "child_process";
import * as crypto from "crypto";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { listBlendFiles, relativeTo, resolveInside, sidecarDir } from "./blendFiles";
import { BridgeResponse, callBridge, cancelScript, ChangeReport, formatChanges, probeBridge } from "./bridge";
import { buildContextPack } from "./contextPack";
import { journalIntent, notesPathFor, writeIntent } from "./notes";
import { runDoctor } from "./doctor";
import { gotchasFor } from "./gotchas";
import { ingestScript, runIngest } from "./ingest";
import * as jobs from "./jobs";
import * as modeling from "./modeling";
import { findFfmpeg } from "./ffmpeg";
import { describePrinter, printerParams, resolvePrinter } from "./printers";
import { readConfig } from "./projectConfig";
import { importReference } from "./references";
import { compare, diff, reference, setRole } from "./review";
import {
  CallExtras,
  clampPort,
  DEFAULT_CHECKPOINTS,
  DEFAULT_CLIENTS,
  DEFAULT_LIB_PATHS,
  DEFAULT_PORT,
  ToolOutcome,
  WorkspaceContext,
} from "./types";

export const PREVIEW_BYTES = 1_500_000;
const LIVE_KEEP = 20;

/**
 * The workspace is VSBLENDER_WORKSPACE when the client config sets it. Otherwise the server walks up
 * from its working directory to the folder with .blender-ai/config.json, so a config without
 * machine-specific paths still finds the project.
 */
export function findWorkspace(env: NodeJS.ProcessEnv = process.env, cwd = process.cwd()): string {
  const fromEnv = env["VSBLENDER_WORKSPACE"];
  if (fromEnv && fs.existsSync(fromEnv)) return fromEnv;
  let current = path.resolve(cwd);
  for (;;) {
    if (fs.existsSync(path.join(current, ".blender-ai", "config.json"))) return current;
    const parent = path.dirname(current);
    if (parent === current) return cwd;
    current = parent;
  }
}

export function loadWorkspaceContext(env: NodeJS.ProcessEnv = process.env, cwd = process.cwd()): WorkspaceContext {
  const workspace = findWorkspace(env, cwd);
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
    checkpoints: config?.checkpoints ?? DEFAULT_CHECKPOINTS,
    libPaths: config?.libPaths ?? DEFAULT_LIB_PATHS,
    ...(config?.printer !== undefined ? { printer: config.printer } : {}),
    ...(config?.trustedScripts ? { trustedScripts: config.trustedScripts } : {}),
    ...(config?.allowSave !== undefined ? { allowSave: config.allowSave } : {}),
    ...(config?.referenceRoots ? { referenceRoots: config.referenceRoots } : {}),
    ...(config?.sharedLibs ? { sharedLibs: config.sharedLibs } : {}),
    ...(config?.ffmpeg ? { ffmpeg: config.ffmpeg } : {}),
  };
}

/** Folders scripts import from: libPaths, then another project's shared folders (sharedLibs). */
export function importPaths(ctx: WorkspaceContext): string[] {
  return [...ctx.libPaths, ...(ctx.sharedLibs ?? []).filter((entry) => !ctx.libPaths.includes(entry))];
}

/** Absolute folders outside the workspace that read-only tools may read reference files from. */
export function referenceRoots(ctx: WorkspaceContext): string[] {
  return (ctx.referenceRoots ?? []).map((entry) => path.resolve(ctx.workspace, entry));
}

export function ok(text: string, extra?: Partial<ToolOutcome>): ToolOutcome {
  return { ok: true, text, ...extra };
}

export function fail(text: string, details?: unknown): ToolOutcome {
  return { ok: false, text, ...(details !== undefined ? { details } : {}) };
}

export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function pickBlend(ctx: WorkspaceContext, requested: unknown): string {
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
export function bridgeFailure(port: number, error: unknown, timeoutMs: number): string {
  const message = errorText(error);
  if (/ECONNREFUSED/i.test(message)) {
    return `Nothing is listening on 127.0.0.1:${port}. Call launch_blender, or start the bridge in Blender (sidebar > VSBlender).`;
  }
  if (/timed out/i.test(message)) {
    return `Blender did not answer within ${Math.round(timeoutMs / 1000)}s. It may still be working on this request; call session_info before retrying.`;
  }
  return `Blender bridge on port ${port}: ${message}`;
}

/** An add-on older than this server answers new methods with "unknown method". */
export function bridgeError(response: BridgeResponse, method: string): string {
  const error = response.error || `${method} failed`;
  if (/^unknown method/i.test(error)) {
    return `The add-on running in Blender is older than this server and has no ${method}. Run doctor_fix and restart Blender, or close Blender and call launch_blender (it loads the add-on shipped with the extension).`;
  }
  return error;
}

/** Read-only bridge methods that also work on the saved file, in a background Blender, when Blender is not running. */
export const HEADLESS_OK = new Set(["describe", "find", "spatial", "timeline", "measure", "check_model", "export_model", "preview"]);

export interface Routed {
  response: BridgeResponse;
  /** live: the open session. disk: the saved file, read by a background Blender. */
  source: "live" | "disk";
  blend?: string;
}

/**
 * Call a bridge method on the open session; when nothing is listening and the method only reads,
 * run it on the saved .blend in a one-shot background Blender instead (a few seconds; about 20 s
 * for the first start on Windows). A persistent background Blender would hold the port.
 */
export async function routeCall(ctx: WorkspaceContext, method: string, params: Record<string, unknown>, timeoutMs: number,
  blendArg: unknown, extras: CallExtras = {}): Promise<Routed> {
  const probe = await probeBridge(ctx.port);
  if (probe.ok || probe.state !== "absent" || !HEADLESS_OK.has(method)) {
    return { response: await callBridge(ctx.port, method, params, timeoutMs), source: "live" };
  }
  if (!ctx.blender || !fs.existsSync(ctx.blender)) {
    throw new Error(`Blender is not running, and no Blender executable is configured to read the saved file. Run VSBlender: Setup, or launch_blender.`);
  }
  let blend: string;
  try {
    blend = pickBlend(ctx, blendArg);
  } catch (error) {
    throw new Error(`Blender is not running, so this reads a saved file, and ${errorText(error)}`);
  }
  const result = await jobs.runToEnd(ctx, {
    blend,
    spec: { kind: "call", method, params },
    kind: "call",
    label: `${method} on ${relativeTo(ctx.workspace, blend)}`,
    factoryStartup: true,
  }, Math.max(timeoutMs, 120000), extras.signal);
  const warnings = Array.isArray(result["warnings"]) ? (result["warnings"] as string[]) : [];
  return { response: { ok: true, result: (result["result"] as Record<string, unknown>) ?? {}, warnings }, source: "disk", blend };
}

export function sourceNote(ctx: WorkspaceContext, routed: Routed): string {
  return routed.source === "disk" && routed.blend
    ? `source: saved file ${relativeTo(ctx.workspace, routed.blend)} (Blender is not running; unsaved work is not included)\n`
    : "";
}

/** Call a bridge method and turn its reply into text. Most read-only tools are just this. */
export async function bridgeTool(ctx: WorkspaceContext, method: string, params: Record<string, unknown>, timeoutMs: number,
  format?: (result: Record<string, unknown>) => string, blendArg?: unknown, extras: CallExtras = {}): Promise<ToolOutcome> {
  try {
    const routed = await routeCall(ctx, method, params, timeoutMs, blendArg, extras);
    const response = routed.response;
    if (!response.ok) return fail(bridgeError(response, method));
    const result = response.result ?? {};
    const text = format ? format(result) : typeof result["text"] === "string" ? result["text"] : JSON.stringify(result, null, 1);
    const warnings = response.warnings?.length ? `\nwarnings: ${response.warnings.join("; ")}` : "";
    return ok(sourceNote(ctx, routed) + text + warnings, { details: { ...result, source: routed.source } });
  } catch (error) {
    if (/did not finish|failed:|not running/i.test(errorText(error))) return fail(errorText(error));
    return fail(bridgeFailure(ctx.port, error, timeoutMs));
  }
}

/** The .blend the running Blender has open, when it is in this workspace. */
export async function liveBlend(ctx: WorkspaceContext): Promise<string | undefined> {
  try {
    const response = await callBridge(ctx.port, "session_info", {}, 8000);
    const file = String(response.result?.["file"] ?? "");
    if (!response.ok || !file) return undefined;
    const rel = path.relative(ctx.workspace, file);
    return rel.startsWith("..") || path.isAbsolute(rel) ? undefined : file;
  } catch {
    return undefined;
  }
}

/** The file a tool is about: the one named, else the one open in Blender, else the only one. */
export async function currentBlend(ctx: WorkspaceContext, requested: unknown): Promise<string> {
  if (typeof requested === "string" && requested.trim()) return pickBlend(ctx, requested);
  return (await liveBlend(ctx)) ?? pickBlend(ctx, undefined);
}

// ----------------------------------------------------------------------------- connection
export async function doctor(ctx: WorkspaceContext, fix: boolean, archive = false): Promise<ToolOutcome> {
  const report = await runDoctor(ctx, { fix, archive, electronPath: process.execPath });
  return report.ok ? ok(report.text, { details: report.checks }) : fail(report.text, report.checks);
}

/** Blender versions whose gotcha sheet this server process has already sent. */
const gotchasSent = new Set<string>();

export async function sessionInfo(ctx: WorkspaceContext, args: Record<string, unknown> = {}): Promise<ToolOutcome> {
  try {
    const response = await callBridge(ctx.port, "session_info", {}, 15000);
    if (!response.ok) return fail(response.error || "session_info failed");
    const version = String(response.result?.["blender_version"] ?? "");
    const { profile, warnings } = resolvePrinter(ctx.printer);
    // The sheet is about 1.5k tokens: sent the first time per Blender version, or when asked for.
    const mode = typeof args["gotchas"] === "string" ? args["gotchas"] : "new";
    let gotchas: string[] | string;
    if (mode === "none") gotchas = "left out (gotchas: \"all\" lists them)";
    else if (mode === "all" || !gotchasSent.has(version)) {
      gotchas = gotchasFor(version);
      gotchasSent.add(version);
    } else gotchas = `sent earlier in this session for Blender ${version} (gotchas: "all" repeats them)`;
    const info = {
      ...response.result,
      workspace: ctx.workspace,
      port: ctx.port,
      printer: ctx.printer !== undefined
        ? `${describePrinter(profile)}${warnings.length ? ` (${warnings.join("; ")})` : ""}`
        : `none set (purpose print uses ${describePrinter(profile)}); set "printer" in .blender-ai/config.json`,
      save: ctx.allowSave ? "the user lets the AI save (save tool, save: true on runs)" : "the save tool asks the user; save: true on runs is off",
      gotchas,
    };
    return ok(JSON.stringify(info, null, 2), { details: info });
  } catch (error) {
    return fail(bridgeFailure(ctx.port, error, 15000));
  }
}

export async function launchBlender(ctx: WorkspaceContext, args: Record<string, unknown>, background = false): Promise<ToolOutcome> {
  if (!ctx.blender || !fs.existsSync(ctx.blender)) {
    return fail("No Blender executable is configured. Run VSBlender: Setup.");
  }
  let file: string | undefined;
  try {
    if (typeof args["file"] === "string" && args["file"].trim()) file = pickBlend(ctx, args["file"]);
  } catch (error) {
    return fail(errorText(error));
  }
  const probe = await probeBridge(ctx.port);
  if (probe.ok) {
    if (!file) return ok(`Blender bridge already listening on ${ctx.port}.`);
    try {
      const opened = await callBridge(ctx.port, "open_blend", { path: file, reason: "launch_blender", actor: "user" }, 90000);
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
  const addon = jobs.addonSource(ctx.extensionRoot);
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
  const mode = background ? "Headless Blender" : "Blender";
  return ok(`${mode} is listening on ${ctx.port}${file ? ` with ${relativeTo(ctx.workspace, file)}` : ""}. pid ${child.pid ?? "?"}`);
}

// ----------------------------------------------------------------------------- run_script
/** The printer profile scripts see through vsblender.printer(). configured: false when the workspace set none. */
export function printerContext(ctx: WorkspaceContext, override?: unknown): Record<string, unknown> {
  const { profile, warnings } = resolvePrinter(ctx.printer, override);
  return { ...printerParams(profile), configured: ctx.printer !== undefined || override !== undefined, warnings };
}

function changesText(report: ChangeReport | undefined): string {
  const lines = formatChanges(report);
  return lines.length ? `changes:\n  ${lines.join("\n  ")}` : "changes: none";
}

/** save: true on a script tool saves the user's file, which only the user can allow (allowSave in setup). */
export function saveRefusal(ctx: WorkspaceContext, args: Record<string, unknown>): string | undefined {
  if (args["save"] !== true || ctx.allowSave === true) return undefined;
  return "save: true needs the user's permission: tick \"Let the AI save the .blend\" in VSBlender: Setup "
    + "(\"allowSave\": true in .blender-ai/config.json). Run without save, then call the save tool, which asks the user.";
}

export function checkpointParams(ctx: WorkspaceContext, args: Record<string, unknown>): Record<string, unknown> {
  return {
    auto: ctx.checkpoints.auto && args["checkpoint"] !== false,
    keep: ctx.checkpoints.keep,
    max_mb: ctx.checkpoints.maxMb,
    budget_mb: ctx.checkpoints.budgetMb,
  };
}

function mb(bytes: unknown): string {
  return `${(Number(bytes) / 1e6).toFixed(1)} MB`;
}

/** The checkpoint a run took, with its size and what all checkpoints of the file take, or why there is none. */
export function checkpointLine(result: Record<string, unknown>, what: string): string {
  const id = typeof result["checkpoint"] === "string" ? result["checkpoint"] : "";
  if (!id) return `checkpoint: ${String(result["checkpoint_note"] ?? "none")}`;
  let line = `checkpoint: ${id} (restore_checkpoint ${id} undoes ${what})`;
  if (result["checkpoint_bytes"] !== undefined) {
    line += `; ${mb(result["checkpoint_bytes"])}, ${String(result["checkpoints_count"])} checkpoints take ${mb(result["checkpoints_total_bytes"])}`;
  }
  if (Array.isArray(result["checkpoints_dropped"]) && result["checkpoints_dropped"].length) {
    line += `; dropped the oldest to stay within the budget: ${(result["checkpoints_dropped"] as string[]).join(", ")}`;
  }
  return line;
}

/** The first line of a run that left the file on disk behind: the count, and which file. */
export function unsavedBanner(result: Record<string, unknown>): string | undefined {
  if (result["saved"]) return undefined;
  const unsaved = result["unsaved"] as { runs?: number; file?: string } | undefined;
  const runs = Number(unsaved?.runs ?? 0);
  if (!(runs > 0)) return undefined;
  const file = unsaved?.file ? String(unsaved.file) : "the open file";
  return `${runs} runs, the file on disk does not have them (${file})`;
}

/** The last lines of a run: what was saved, and what the file on disk does not have yet. */
export function saveLines(result: Record<string, unknown>): string[] {
  const lines: string[] = [];
  const saved = result["saved"] as Record<string, unknown> | undefined;
  if (saved && typeof saved === "object") lines.push(`saved: ${String(saved["saved"])} (${mb(saved["bytes"])})`);
  const unsaved = result["unsaved"] as Record<string, unknown> | undefined;
  if (unsaved && typeof unsaved === "object" && !saved) lines.push(`unsaved changes: ${String(unsaved["text"] ?? "")}`);
  return lines;
}

/** A script on a throwaway copy. The open file, its journal and its checkpoints are not touched. */
async function runScriptInBackground(ctx: WorkspaceContext, args: Record<string, unknown>): Promise<ToolOutcome> {
  if (!ctx.blender || !fs.existsSync(ctx.blender)) return fail("No Blender executable is configured. Run VSBlender: Setup.");
  if (typeof args["path"] !== "string" || !args["path"].trim()) return fail("path is required");
  let script: string;
  try {
    script = resolveInside(ctx.workspace, args["path"]);
  } catch (error) {
    return fail(errorText(error));
  }
  if (!script.toLowerCase().endsWith(".py")) return fail("path must be a .py file");
  if (!fs.existsSync(script)) return fail(`file not found: ${args["path"]}`);
  const rel = relativeTo(ctx.workspace, script);
  const probe = await probeBridge(ctx.port);
  const id = jobs.newJobId("script");
  const dir = path.join(jobs.jobsDir(ctx.workspace), id);
  fs.mkdirSync(dir, { recursive: true });
  let blend: string;
  let source: string;
  let scratch: string | undefined;
  if (probe.ok) {
    const copy = path.join(dir, "scene.blend");
    try {
      const response = await callBridge(ctx.port, "save_copy", { path: copy }, 300000);
      if (!response.ok) return fail(bridgeError(response, "save_copy"));
      blend = copy;
      scratch = copy;
      source = "a copy of the open session (thrown away; the open file is unchanged)";
    } catch (error) {
      return fail(bridgeFailure(ctx.port, error, 300000));
    }
  } else if (probe.state !== "absent") {
    return fail(`${probe.detail}. Not running the script.`);
  } else {
    try {
      blend = pickBlend(ctx, args["blend"] ?? args["file"]);
    } catch (error) {
      return fail(`Blender is not running, so the saved file is used, and ${errorText(error)}`);
    }
    source = `the saved file ${relativeTo(ctx.workspace, blend)} (Blender is not running; the file is not written)`;
  }
  const spec: Record<string, unknown> = { kind: "script", script };
  if (typeof args["function"] === "string" && args["function"]) spec["function"] = args["function"];
  if (args["args"] && typeof args["args"] === "object") spec["args"] = args["args"];
  const timeout = Math.min(600000, Math.max(1000, Number(args["timeout_ms"]) || 60000));
  try {
    const result = await jobs.runToEnd(ctx, {
      blend, spec, kind: "script", label: `background ${rel}`, ...(scratch ? { scratch } : {}),
    }, timeout);
    const lines = [`background: ${rel}`, source, `result: ${JSON.stringify(result["result"])}`];
    const stdout = typeof result["stdout"] === "string" ? result["stdout"] : "";
    const stderr = typeof result["stderr"] === "string" ? result["stderr"] : "";
    if (Array.isArray(result["warnings"]) && result["warnings"].length) lines.push(`warnings:\n${(result["warnings"] as string[]).join("\n")}`);
    if (stdout.trim()) lines.push(`stdout:\n${stdout.trimEnd()}`);
    if (stderr.trim()) lines.push(`stderr:\n${stderr.trimEnd()}`);
    lines.push("The copy was thrown away. Nothing was checkpointed or journaled, and the open file is unchanged.");
    return ok(lines.join("\n"), { details: result });
  } catch (error) {
    return fail(errorText(error));
  }
}

export async function runScript(ctx: WorkspaceContext, args: Record<string, unknown>, extras: CallExtras = {}): Promise<ToolOutcome> {
  if (args["background"] === true) return runScriptInBackground(ctx, args);
  if (typeof args["path"] !== "string" || !args["path"].trim()) return fail("path is required");
  let script: string;
  try {
    script = resolveInside(ctx.workspace, args["path"]);
  } catch (error) {
    return fail(errorText(error));
  }
  if (!script.toLowerCase().endsWith(".py")) return fail("path must be a .py file");
  if (!fs.existsSync(script)) return fail(`file not found: ${args["path"]}`);
  const reason = typeof args["reason"] === "string" ? args["reason"].trim() : "";
  const refused = saveRefusal(ctx, args);
  if (refused) return fail(refused);
  const params: Record<string, unknown> = {
    path: script,
    reason,
    actor: "ai",
    checkpoint: checkpointParams(ctx, args),
    lib_paths: importPaths(ctx),
    reference_roots: referenceRoots(ctx),
    printer: printerContext(ctx),
  };
  if (args["save"] === true) params["save"] = true;
  if (args["allow_references"] === true) params["allow_references"] = true;
  if (typeof args["atomic"] === "boolean") params["atomic"] = args["atomic"];
  if (typeof args["function"] === "string" && args["function"]) params["function"] = args["function"];
  if (args["args"] && typeof args["args"] === "object") params["args"] = args["args"];
  const timeout = Math.min(600000, Math.max(1000, Number(args["timeout_ms"]) || 60000));
  const rel = relativeTo(ctx.workspace, script);
  try {
    const response = await callWithProgress(ctx, "run_script", params, timeout, extras, `running ${rel}`);
    if (!response.ok) {
      // The add-on already puts the traceback from the user's file:line, stdout and partial changes in error.
      const error = bridgeError(response, "run_script");
      return fail(`file: ${rel}\n${error}`, { changed: response.changed ?? [], changes: response.changes });
    }
    const result = response.result ?? {};
    const report = result["changes"] as ChangeReport | undefined;
    const lines = [
      `file: ${rel}`,
      `result: ${JSON.stringify(result["result"])}`,
      checkpointLine(result, "this script"),
      changesText(report),
    ];
    if (typeof result["provenance"] === "string") lines.push(`provenance: ${result["provenance"]}`);
    if (Array.isArray(result["rerun_after"]) && result["rerun_after"].length) {
      lines.push(`pipeline: this script repairs what ${(result["rerun_after"] as string[]).join(", ")} reset; run it again after those`);
    }
    if (Array.isArray(result["out_of_date"]) && result["out_of_date"].length) {
      const why = Array.isArray(result["out_of_date_why"]) ? result["out_of_date_why"] as { script?: string; names?: string[] }[] : [];
      const named = why.filter((item) => Array.isArray(item.names) && item.names.length)
        .map((item) => `${item.script} (${item.names!.join(", ")})`);
      const detail = named.length ? ` Changed: ${named.join("; ")}.` : "";
      lines.push(`now out of date: ${(result["out_of_date"] as string[]).join(", ")} (pipeline.json or their rerun-after header).${detail} `
        + "Run them again, or run_pipeline from this step.");
    }
    if (Array.isArray(result["not_run_yet"]) && result["not_run_yet"].length) {
      lines.push(`later steps that have not run yet: ${(result["not_run_yet"] as string[]).join(", ")}`);
    }
    if (response.warnings?.length) lines.push(`warnings:\n${response.warnings.join("\n")}`);
    const stdout = typeof result["stdout"] === "string" ? result["stdout"] : "";
    const stderr = typeof result["stderr"] === "string" ? result["stderr"] : "";
    if (stdout.trim()) lines.push(`stdout:\n${stdout.trimEnd()}`);
    if (stderr.trim()) lines.push(`stderr:\n${stderr.trimEnd()}`);
    lines.push(...saveLines(result));
    const banner = unsavedBanner(result);
    if (banner) lines.unshift(banner);
    return ok(lines.join("\n"), { details: { ...result, changed: response.changed ?? [] } });
  } catch (error) {
    if (/timed out/i.test(errorText(error))) {
      const asked = await cancelScript(ctx.port);
      return fail(`${bridgeFailure(ctx.port, error, timeout)}${asked ? " The script was asked to stop at its next vsblender.progress() call; a script that never calls it runs to the end." : ""}`);
    }
    return fail(bridgeFailure(ctx.port, error, timeout));
  }
}

/**
 * A long bridge call that runs a script: progress comes from ping (answered on the bridge's socket
 * thread while the script holds Blender's main thread), and an abort asks the script to stop.
 */
export async function callWithProgress(ctx: WorkspaceContext, method: string, params: Record<string, unknown>, timeout: number,
  extras: CallExtras, label: string): Promise<BridgeResponse> {
  let last = "";
  const poll = setInterval(() => {
    void probeBridge(ctx.port).then((probe) => {
      const progress = probe.progress;
      if (!progress || !extras.progress) return;
      const key = `${progress.fraction ?? ""}|${progress.message ?? ""}|${progress.label ?? ""}`;
      if (key === last) return;
      last = key;
      extras.progress(typeof progress.fraction === "number" ? progress.fraction : undefined,
        progress.message || (progress.label ? `running ${progress.label}` : label));
    });
  }, 1000);
  const onAbort = (): void => {
    void cancelScript(ctx.port);
  };
  extras.signal?.addEventListener("abort", onAbort);
  try {
    return await callBridge(ctx.port, method, params, timeout);
  } finally {
    clearInterval(poll);
    extras.signal?.removeEventListener("abort", onAbort);
  }
}

// ----------------------------------------------------------------------------- understanding
export async function ingest(ctx: WorkspaceContext, args: Record<string, unknown>): Promise<ToolOutcome> {
  const reason = typeof args["reason"] === "string" ? args["reason"] : "";
  if (args["live"] === true) {
    return bridgeTool(ctx, "ingest_live", { actor: "ai", reason, force: args["force"] === true, previews: args["previews"] !== false }, 600000,
      (result) => [
        `status: ${String(result["status"])}`,
        `source: live session`,
        `out: ${String(result["out"] ?? "")}`,
        result["objects"] !== undefined ? `objects: ${String(result["objects"])}` : "",
        result["changes"] !== undefined ? `changes since the last ingest: ${String(result["changes"])}` : "",
        result["issues"] !== undefined ? `issues: ${String(result["issues"])}` : "",
        result["previews"] !== undefined ? `previews: ${String(result["previews"])}` : "",
      ].filter(Boolean).join("\n"));
  }
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
    reason,
  });
  const where = result.out ? relativeTo(ctx.workspace, result.out) : relativeTo(ctx.workspace, sidecarDir(blend));
  if (result.status === "error") return fail(`${result.error ?? "ingest failed"}\n${result.log}`.trim());
  const live = await liveBlend(ctx);
  const note = live && path.resolve(live) === path.resolve(blend)
    ? "note: this read the saved file. Blender has it open; if it has unsaved changes, ingest with live=true describes those."
    : "";
  const summary = [
    `status: ${result.status}`,
    `out: ${where}`,
    result.objects !== undefined ? `objects: ${result.objects}` : "",
    result.changes !== undefined ? `changes: ${result.changes}` : "",
    result.issues !== undefined ? `issues: ${result.issues}` : "",
    result.previews !== undefined ? `previews: ${result.previews}` : "",
    result.seconds !== undefined ? `seconds: ${result.seconds}` : "",
    note,
  ].filter(Boolean).join("\n");
  return ok(summary, { details: result });
}

export async function notes(ctx: WorkspaceContext, args: Record<string, unknown>): Promise<ToolOutcome> {
  const text = typeof args["intent"] === "string" ? args["intent"].trim() : "";
  if (!text) return fail("intent is required: what the next session must know (where sizes live, conventions, what to re-run, what is keyed)");
  const mode = args["mode"] === "replace" ? "replace" : "append";
  let blend: string;
  try {
    blend = await currentBlend(ctx, args["path"]);
  } catch (error) {
    return fail(errorText(error));
  }
  const file = notesPathFor(blend);
  if (!fs.existsSync(file)) return fail(`${relativeTo(ctx.workspace, blend)} has no NOTES.md yet. Call ingest first.`);
  const section = writeIntent(file, text, mode);
  journalIntent(blend, ctx.workspace, mode, text);
  return ok(`${mode === "replace" ? "rewrote" : "added to"} Intent & constraints in ${relativeTo(ctx.workspace, file)} `
    + `(${section.split(/\r?\n/).length} line(s)); re-ingests keep it, and context_pack always includes it.`);
}

export async function contextPack(ctx: WorkspaceContext, args: Record<string, unknown>): Promise<ToolOutcome> {
  let blend: string;
  try {
    // The file named, else the one open in Blender, else the only one.
    blend = await currentBlend(ctx, args["path"]);
  } catch (error) {
    return fail(errorText(error));
  }
  const budget = Math.max(64, Math.min(20000, Number(args["budget_tokens"]) || 2000));
  const focus = typeof args["focus"] === "string" ? args["focus"] : undefined;
  const packed = buildContextPack(ctx.workspace, blend, budget, focus);
  const header = [`tokens: ${packed.tokens} of ${budget}`];
  if (packed.dropped.length) header.push(`cut to fit: ${packed.dropped.join("; ")}`);
  if (packed.tokens > budget) {
    header.push(packed.required !== undefined && packed.required > budget
      ? `over budget: the sections that are always kept (header, Before you modify, Intent & constraints) are ${packed.required} tokens on their own`
      : "over budget: the warnings sections are always kept in full");
  }
  return ok(`${header.join("\n")}\n\n${packed.text}`, { details: { tokens: packed.tokens, truncated: packed.truncated, dropped: packed.dropped } });
}

// ----------------------------------------------------------------------------- seeing
export function mimeFor(file: string): string {
  return /\.jpe?g$/i.test(file) ? "image/jpeg" : "image/png";
}

export function imageReply(file: string, workspace: string, lines: string[], keep: boolean, liveBase?: string,
  livePrefix?: string): ToolOutcome {
  const size = fs.statSync(file).size;
  const data = size <= PREVIEW_BYTES ? fs.readFileSync(file).toString("base64") : undefined;
  let shown = file;
  if (keep || !data) {
    const liveDir = path.join(workspace, ".blender-ai", "live");
    fs.mkdirSync(liveDir, { recursive: true });
    shown = path.join(liveDir, liveBase || path.basename(file));
    if (path.resolve(shown) !== path.resolve(file)) fs.copyFileSync(file, shown);
    pruneLive(liveDir, LIVE_KEEP, livePrefix);
  }
  if (path.resolve(shown) !== path.resolve(file)) fs.rmSync(file, { force: true });
  const text = [
    ...lines,
    keep || !data ? `file: ${relativeTo(workspace, shown)}` : "not saved (pass save: true to keep a copy in .blender-ai/live/)",
    data ? "" : `image omitted from the tool result (${size} bytes). Open the file path, or pass a smaller size or a crop.`,
  ].filter(Boolean).join("\n");
  return ok(text, data ? { images: [{ mimeType: mimeFor(shown), data }] } : {});
}

export function tempPng(prefix: string): string {
  const dir = path.join(os.tmpdir(), "vsblender");
  fs.mkdirSync(dir, { recursive: true });
  return path.join(dir, `${prefix}-${Date.now()}-${crypto.randomBytes(3).toString("hex")}.png`);
}

const PREVIEW_KEYS = ["view", "shading", "size", "width", "height", "aspect", "target", "isolate", "projection", "samples",
  "compositor", "dof", "motion_blur", "views", "framing", "crop", "region", "overlay", "cavity", "outline", "shadow", "matcap",
  "xray", "color_type", "material", "camera", "ref", "with_scene", "name"];

export function previewParams(args: Record<string, unknown>): Record<string, unknown> {
  const params: Record<string, unknown> = {};
  for (const key of PREVIEW_KEYS) {
    const value = args[key];
    if (value !== undefined && value !== null && value !== "") params[key] = value;
  }
  if (args["frame"] !== undefined && args["frame"] !== null && args["frame"] !== "") params["frame"] = Number(args["frame"]);
  // The add-on re-encodes or shrinks an image that would not fit in a tool reply, instead of omitting it.
  params["max_bytes"] = PREVIEW_BYTES;
  return params;
}

export async function preview(ctx: WorkspaceContext, args: Record<string, unknown>, extras: CallExtras = {}): Promise<ToolOutcome> {
  const out = tempPng(`preview-${safeToken(String(args["camera"] ?? args["view"] ?? "iso"))}`);
  const params = { ...previewParams(args), out, reference_roots: referenceRoots(ctx), ffmpeg: findFfmpeg(ctx.ffmpeg) };
  const timeout = 240000;
  try {
    const routed = await routeCall(ctx, "preview", params, timeout, args["path"], extras);
    const response = routed.response;
    if (!response.ok) return fail(bridgeError(response, "preview"));
    const result = response.result ?? {};
    const file = String(result["file"] ?? out);
    if (!fs.existsSync(file)) return fail(`Blender reported ${file}, but the file is not there`);
    const applied = Array.isArray(result["applied"]) && result["applied"].length ? (result["applied"] as string[]).join(", ") : "none";
    const summary = typeof result["summary"] === "string" ? result["summary"] : "";
    const read = Array.isArray(result["read"]) ? (result["read"] as string[]).join("\n") : "";
    const lines = [
      summary,
      read,
      sourceNote(ctx, routed).trim(),
      result["material"] ? `material ball: ${String(result["material"])}` : "",
      Array.isArray(result["reference"]) ? `reference (from references.json, not in the file): ${(result["reference"] as string[]).join(", ")}` : "",
      result["camera"] && !summary ? `camera: ${String(result["camera"])}` : "",
      result["views"] ? `views: ${(result["views"] as string[]).join(", ")}` : `view: ${String(result["view"] ?? "")}`,
      `shading: ${String(result["shading"] ?? "")} (${String(result["engine"] ?? "")})`,
      `size: ${String(result["width"])}x${String(result["height"])}`,
      `projection: ${typeof result["projection"] === "object" ? JSON.stringify(result["projection"]) : String(result["projection"] ?? "")}`,
      result["isolate"] ? "isolated: only the target and lights" : "",
      typeof result["framing"] === "string" && result["framing"] ? String(result["framing"]) : "",
      `applied: ${applied}`,
      Array.isArray(result["overlays"]) && result["overlays"].length ? `overlays: ${(result["overlays"] as string[]).join("; ")}` : "",
      typeof result["note"] === "string" ? String(result["note"]) : "",
      response.warnings?.length ? `warnings: ${response.warnings.join("; ")}` : "",
    ];
    const given = typeof args["name"] === "string" ? args["name"].trim() : "";
    const camera = safeToken(String(args["camera"] ?? result["camera"] ?? args["view"] ?? "view"));
    const liveBase = given ? `${camera}-${safeToken(given)}.png` : undefined;
    const keep = args["save"] === true || given.length > 0;
    return { ...imageReply(file, ctx.workspace, lines, keep, liveBase, liveBase ? `${camera}-` : undefined), details: result };
  } catch (error) {
    if (/did not finish|failed:|not running/i.test(errorText(error))) return fail(errorText(error));
    return fail(bridgeFailure(ctx.port, error, timeout));
  }
}

export function safeToken(value: string): string {
  const cleaned = value.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
  return cleaned || "preview";
}

function pruneLive(dir: string, keep: number, prefix?: string, prefixKeep = 4): void {
  let files: { full: string; name: string; mtime: number }[];
  try {
    files = fs.readdirSync(dir)
      .filter((name) => /\.(png|jpe?g)$/i.test(name))
      .map((name) => {
        const full = path.join(dir, name);
        return { full, name, mtime: fs.statSync(full).mtimeMs };
      });
  } catch {
    return;
  }
  files.sort((a, b) => b.mtime - a.mtime);
  if (prefix) {
    const matched = files.filter((item) => item.name.startsWith(prefix));
    for (const item of matched.slice(prefixKeep)) fs.rmSync(item.full, { force: true });
    const dropped = new Set(matched.slice(prefixKeep).map((item) => item.full));
    files = files.filter((item) => !dropped.has(item.full));
  }
  for (const item of files.slice(keep)) fs.rmSync(item.full, { force: true });
}

// ----------------------------------------------------------------------------- render jobs
const IMAGE_EXT = /\.png$/i;

function renderOutput(ctx: WorkspaceContext, id: string, mode: string, requested: unknown): { output: string; frameDir: string } {
  const base = path.join(ctx.workspace, ".blender-ai", "renders");
  if (typeof requested !== "string" || !requested.trim()) {
    if (mode === "mp4") return { output: path.join(base, `${id}.mp4`), frameDir: base };
    if (mode === "frames") return { output: path.join(base, id), frameDir: path.join(base, id) };
    return { output: path.join(base, mode === "sheet" ? `${id}-sheet.png` : `${id}.png`), frameDir: path.join(base, id) };
  }
  const abs = resolveInside(ctx.workspace, requested);
  if (mode === "mp4" && !/\.mp4$/i.test(abs)) throw new Error("output for as=mp4 must end in .mp4");
  if ((mode === "still" || mode === "sheet") && !IMAGE_EXT.test(abs)) throw new Error("output must end in .png");
  if (mode === "frames") {
    if (fs.existsSync(abs) && !fs.statSync(abs).isDirectory()) throw new Error("output for as=frames must be a folder");
    return { output: abs, frameDir: abs };
  }
  return { output: abs, frameDir: path.join(path.dirname(abs), `${path.basename(abs, path.extname(abs))}_frames`) };
}

export async function render(ctx: WorkspaceContext, args: Record<string, unknown>, extras: CallExtras = {}): Promise<ToolOutcome> {
  if (!ctx.blender || !fs.existsSync(ctx.blender)) return fail("No Blender executable is configured. Run VSBlender: Setup.");
  let frames = args["frames"] && typeof args["frames"] === "object" ? args["frames"] as Record<string, unknown> | number[] : undefined;
  const mode = typeof args["as"] === "string" && args["as"] ? args["as"] : frames ? "sheet" : "still";
  if (!["still", "frames", "sheet", "mp4"].includes(mode)) return fail("as must be still, frames, sheet or mp4");
  if (mode === "still" && frames) return fail("as=still renders one frame; pass frame, or as=frames/sheet/mp4 with frames");
  if (mode === "sheet" && !frames) return fail("as=sheet needs frames: a list such as [1, 36, 216, 260], or {start, end, step}");
  // A video or a frame sequence of one frame is never what was asked for: without frames they cover
  // the scene's frame range, and the reply says so.
  let defaulted = "";
  if (!frames && (mode === "mp4" || mode === "frames")) {
    if (args["frame"] !== undefined) return fail(`as=${mode} renders a range: pass frames {start, end, step}, not frame`);
    frames = {};
    defaulted = "the scene's frame range (no frames given)";
  }
  if (frames) {
    const problem = await checkFrames(ctx, frames, mode, args["max_tiles"]);
    if (problem) return fail(problem);
  }
  const id = jobs.newJobId("render");
  let output: string;
  let frameDir: string;
  try {
    ({ output, frameDir } = renderOutput(ctx, id, mode, args["output"]));
  } catch (error) {
    return fail(errorText(error));
  }
  const dir = path.join(jobs.jobsDir(ctx.workspace), id);
  fs.mkdirSync(dir, { recursive: true });
  let blend: string;
  let scratch: string | undefined;
  let autoexec = false;
  let scene: string | undefined;
  let source: string;
  const probe = await probeBridge(ctx.port);
  if (probe.ok) {
    // Render what is open, unsaved changes included, from a copy.
    const copy = path.join(dir, "scene.blend");
    try {
      const response = await callBridge(ctx.port, "save_copy", { path: copy }, 300000);
      if (!response.ok) return fail(bridgeError(response, "save_copy"));
      autoexec = response.result?.["autoexec"] === true;
      scene = typeof response.result?.["scene"] === "string" ? response.result["scene"] : undefined;
      source = response.result?.["dirty"] ? "the open session, including unsaved changes" : "the open session";
    } catch (error) {
      return fail(bridgeFailure(ctx.port, error, 300000));
    }
    blend = copy;
    scratch = copy;
  } else {
    try {
      blend = pickBlend(ctx, args["path"]);
    } catch (error) {
      return fail(`Blender is not running, so the saved file is rendered, and ${errorText(error)}`);
    }
    source = `the saved file ${relativeTo(ctx.workspace, blend)} (Blender is not running)`;
  }
  const spec: Record<string, unknown> = {
    kind: "render",
    as: mode,
    output,
    frame_dir: frameDir,
    preview: path.join(dir, "preview.png"),
    preview_max: 1024,
    max_bytes: PREVIEW_BYTES,
    overrides: args["overrides"] && typeof args["overrides"] === "object" ? args["overrides"] : {},
  };
  if (frames) spec["frames"] = frames;
  else if (args["frame"] !== undefined) spec["frame"] = Number(args["frame"]);
  if (typeof args["camera"] === "string" && args["camera"]) spec["camera"] = args["camera"];
  if (scene) spec["scene"] = scene;
  if (mode === "mp4") {
    const ffmpeg = findFfmpeg(ctx.ffmpeg);
    if (ffmpeg) spec["ffmpeg"] = ffmpeg;
    if (args["keep_frames"] === true) spec["keep_frames"] = true;
  }
  let label = `${mode} render of ${source}`;
  if (defaulted) {
    const range = frames && !Array.isArray(frames) && frames["start"] !== undefined ? ` ${String(frames["start"])}-${String(frames["end"])}` : "";
    label += `; frames: ${defaulted}${range}`;
  }
  let job: jobs.Job;
  try {
    job = jobs.startJob(ctx, id, { blend, spec, kind: "render", label, ...(scratch ? { scratch } : {}), autoexec });
  } catch (error) {
    return fail(errorText(error));
  }
  const wait = Math.max(0, Math.min(600, args["wait_seconds"] === undefined ? 90 : Number(args["wait_seconds"]) || 0)) * 1000;
  const poll = extras.progress ? setInterval(() => extras.progress?.(jobs.fraction(job), job.progress.stage === "encoding"
    ? `encoding ${job.progress.total} frames`
    : `rendering ${Math.min(job.progress.done + 1, job.progress.total)}/${job.progress.total}`), 1000) : undefined;
  await jobs.waitFor(job, wait, extras.signal);
  if (poll) clearInterval(poll);
  return jobReply(ctx, job);
}

/**
 * Frame lists and ranges: not empty, end after start, mp4 needs a range of 2 frames or more, and a
 * sheet has at most max_tiles tiles. A range missing start or end is completed from the open scene
 * (in place); with Blender closed the job uses the saved file's range.
 */
async function checkFrames(ctx: WorkspaceContext, frames: Record<string, unknown> | number[], mode: string, maxTiles: unknown): Promise<string | undefined> {
  let count: number;
  if (Array.isArray(frames)) {
    if (!frames.length) return "frames is an empty list";
    if (!frames.every((f) => Number.isFinite(Number(f)))) return "frames must be numbers, e.g. [1, 36, 216, 260]";
    if (mode === "mp4") return "as=mp4 needs a frame range {start, end, step}, not a list of frames";
    count = frames.length;
  } else {
    let start = Number(frames["start"]);
    let end = Number(frames["end"]);
    if (!Number.isFinite(start) || !Number.isFinite(end)) {
      try {
        const info = await callBridge(ctx.port, "session_info", {}, 8000);
        if (!Number.isFinite(start)) start = Number(info.result?.["frame_start"] ?? 1);
        if (!Number.isFinite(end)) end = Number(info.result?.["frame_end"] ?? start);
        frames["start"] = start;
        frames["end"] = end;
      } catch {
        // Blender is closed: the job reads the range from the saved file and checks it there.
        if (mode === "mp4" || mode === "frames") return undefined;
        return "frames needs start and end when Blender is not running";
      }
    }
    if (end < start) return `frames end ${end} is before start ${start}`;
    const step = Math.max(1, Number(frames["step"]) || 1);
    count = Math.floor((end - start) / step) + 1;
  }
  if (mode === "mp4" && count < 2) {
    return `as=mp4 of ${count} frame is not a video. Pass frames {start, end} covering 2 frames or more, or use as=still.`;
  }
  const cap = Math.max(1, Math.min(36, Number(maxTiles) || 16));
  if (mode === "sheet" && count > cap) {
    return `a sheet of ${count} frames is too many to read (limit ${cap}). Pass a step, a list such as [1, 36, 216, 260], `
      + `as=frames for every frame, or max_tiles (up to 36).`;
  }
  return undefined;
}

function jobReply(ctx: WorkspaceContext, job: jobs.Job): ToolOutcome {
  const text = jobs.describeJob(job, ctx.workspace);
  if (job.state === "running") return ok(`${text}\nStill rendering. Call job_status with id ${job.id}, or cancel_job.`);
  if (job.state !== "done") return fail(text);
  const preview = job.result?.["preview"];
  if (typeof preview === "string" && fs.existsSync(preview) && fs.statSync(preview).size <= PREVIEW_BYTES) {
    return ok(text, { images: [{ mimeType: mimeFor(preview), data: fs.readFileSync(preview).toString("base64") }], details: job.result });
  }
  return ok(text, { details: job.result });
}

export async function jobStatus(ctx: WorkspaceContext, args: Record<string, unknown>): Promise<ToolOutcome> {
  const id = typeof args["id"] === "string" ? args["id"] : "";
  if (id) {
    const job = jobs.getJob(id);
    if (!job) return fail(`no job ${id} in this session. Jobs: ${jobs.allJobs().map((j) => j.id).join(", ") || "none"}`);
    return jobReply(ctx, job);
  }
  const all = jobs.allJobs();
  if (!all.length) return ok("no jobs in this session");
  return ok(all.map((job) => jobs.describeJob(job, ctx.workspace).split("\n").slice(0, 3).join("\n")).join("\n\n"));
}

export async function cancelJob(_ctx: WorkspaceContext, args: Record<string, unknown>): Promise<ToolOutcome> {
  const id = typeof args["id"] === "string" ? args["id"] : "";
  if (!id) return fail("id is required");
  return jobs.cancel(id) ? ok(`cancelled ${id}`) : fail(`${id} is not a running job`);
}

// ----------------------------------------------------------------------------- saving
export async function save(ctx: WorkspaceContext, args: Record<string, unknown>): Promise<ToolOutcome> {
  const probe = await probeBridge(ctx.port);
  if (!probe.ok) {
    return fail(probe.state === "absent"
      ? "Blender is not running, so there is nothing unsaved: the .blend on disk is the latest version."
      : `${probe.detail}. Not saving.`);
  }
  const params: Record<string, unknown> = { reason: args["reason"] ?? null, actor: "ai", compress: args["compress"] !== false };
  for (const key of ["allow_references", "overwrite"]) if (args[key] === true) params[key] = true;
  if (typeof args["path"] === "string" && args["path"].trim()) {
    try {
      const abs = resolveInside(ctx.workspace, args["path"]);
      if (!abs.toLowerCase().endsWith(".blend")) return fail("path must end in .blend");
      params["path"] = abs;
    } catch (error) {
      return fail(errorText(error));
    }
  }
  try {
    const response = await callBridge(ctx.port, "save_file", params, 300000);
    if (!response.ok) return fail(bridgeError(response, "save_file"));
    const result = response.result ?? {};
    const runs = Number(result["runs"] ?? 0);
    const lines = [
      `saved: ${String(result["saved"])} (${mb(result["bytes"])}${result["compress"] ? ", compressed" : ""})`,
      runs ? `${runs} run(s) since the last save are in the file now.` : "There were no runs through the bridge since the last save.",
      "Journaled with the reason. The watcher re-ingests the saved file; its change log says the changes came from the AI's runs.",
    ];
    if (Array.isArray(result["references_kept"])) lines.push(`kept reference objects in the file (allow_references): ${(result["references_kept"] as string[]).join(", ")}`);
    return ok(lines.join("\n"), { details: result });
  } catch (error) {
    return fail(bridgeFailure(ctx.port, error, 300000));
  }
}

// ----------------------------------------------------------------------------- append
export async function append(ctx: WorkspaceContext, args: Record<string, unknown>): Promise<ToolOutcome> {
  const from = typeof args["from"] === "string" ? args["from"].trim() : "";
  if (!from) return fail("from is required: the workspace .blend to copy objects from");
  let source: string;
  try {
    source = resolveInside(ctx.workspace, from);
  } catch (error) {
    return fail(errorText(error));
  }
  if (!source.toLowerCase().endsWith(".blend") || !fs.existsSync(source)) return fail(`not a .blend in the workspace: ${from}`);
  const objects = typeof args["objects"] === "string" ? [args["objects"]] : Array.isArray(args["objects"]) ? args["objects"] : [];
  if (!objects.length) return fail("objects is required: names or globs, e.g. [\"SG DHD*\"]");
  const params: Record<string, unknown> = {
    from: source, objects, reason: args["reason"] ?? null, actor: "ai", checkpoint: checkpointParams(ctx, args),
    with_children: args["with_children"] !== false,
  };
  if (typeof args["collection"] === "string" && args["collection"].trim()) params["collection"] = args["collection"].trim();
  try {
    const response = await callBridge(ctx.port, "append_objects", params, 300000);
    if (!response.ok) return fail(bridgeError(response, "append_objects"));
    const result = response.result ?? {};
    const appended = (result["appended"] as string[] | undefined) ?? [];
    const lines = [
      `appended ${appended.length} object(s) from ${relativeTo(ctx.workspace, source)} into collection ${String(result["collection"])}: `
        + `${appended.slice(0, 20).join(", ")}${appended.length > 20 ? ` (+${appended.length - 20} more)` : ""}`,
      "Each records where it came from (ai_appended_from, ai_appended_object): describe shows it, find from:<file> selects them.",
      checkpointLine(result, "the append"),
      changesText(result["changes"] as ChangeReport | undefined),
    ];
    if (response.warnings?.length) lines.push(`warnings:\n${response.warnings.join("\n")}`);
    lines.push(...saveLines(result));
    return ok(lines.join("\n"), { details: result });
  } catch (error) {
    return fail(bridgeFailure(ctx.port, error, 300000));
  }
}

export async function openBlend(ctx: WorkspaceContext, args: Record<string, unknown>): Promise<ToolOutcome> {
  const requested = typeof args["path"] === "string" && args["path"].trim()
    ? args["path"].trim()
    : typeof args["file"] === "string" ? args["file"].trim() : "";
  if (!requested) return fail("path is required: a workspace .blend");
  let abs: string;
  try {
    abs = resolveInside(ctx.workspace, requested);
  } catch (error) {
    return fail(errorText(error));
  }
  if (!abs.toLowerCase().endsWith(".blend")) return fail("path must end in .blend");
  if (!fs.existsSync(abs)) return fail(`file not found: ${requested}`);
  const probe = await probeBridge(ctx.port);
  if (!probe.ok) {
    return fail(probe.state === "absent"
      ? "Blender is not running. Use launch_blender to open the file."
      : `${probe.detail}. Not opening.`);
  }
  try {
    const response = await callBridge(ctx.port, "open_blend", {
      path: abs, reason: args["reason"] ?? null, actor: "ai",
    }, 90000);
    if (!response.ok) return fail(bridgeError(response, "open_blend"));
    const result = response.result ?? {};
    if (result["opened"] !== true) {
      return fail(`${String(result["reason"] ?? "the open file could not be replaced")}. The bridge was left on the file it already had open.`);
    }
    const rel = relativeTo(ctx.workspace, abs);
    const text = result["already"] === true ? `Already open: ${rel}` : `Opened ${rel}`;
    return ok(text, { details: result });
  } catch (error) {
    return fail(bridgeFailure(ctx.port, error, 90000));
  }
}

/** Journaled script runs since the last save. run: true executes them; a sha mismatch is skipped unless force. */
export async function replay(ctx: WorkspaceContext, args: Record<string, unknown>): Promise<ToolOutcome> {
  const probe = await probeBridge(ctx.port);
  if (!probe.ok) {
    return fail(probe.state === "absent"
      ? "Blender is not running, so there is no open session to replay into."
      : `${probe.detail}. Not replaying.`);
  }
  const run = args["run"] === true;
  const timeout = run ? 600000 : 30000;
  try {
    const response = await callBridge(ctx.port, "replay", {
      run, force: args["force"] === true, actor: "ai",
    }, timeout);
    if (!response.ok) return fail(bridgeError(response, "replay"));
    const result = response.result ?? {};
    const text = typeof result["text"] === "string" ? result["text"] : JSON.stringify(result, null, 1);
    return ok(text, { details: result });
  } catch (error) {
    return fail(bridgeFailure(ctx.port, error, timeout));
  }
}

// ----------------------------------------------------------------------------- history
export async function checkpoint(ctx: WorkspaceContext, args: Record<string, unknown>): Promise<ToolOutcome> {
  const label = typeof args["label"] === "string" && args["label"].trim() ? args["label"].trim() : "manual";
  return bridgeTool(ctx, "checkpoint", { label, reason: args["reason"] ?? null, actor: "ai" }, 300000,
    (result) => `checkpoint: ${String(result["id"])}\nfile: ${String(result["file"])}\nlabel: ${String(result["label"])}\nThe open file and its dirty flag are unchanged.`);
}

export async function restoreCheckpoint(ctx: WorkspaceContext, args: Record<string, unknown>): Promise<ToolOutcome> {
  const id = typeof args["id"] === "string" ? args["id"].trim() : "";
  if (!id) return fail("id is required: a checkpoint id from session_info, or last");
  return bridgeTool(ctx, "restore_checkpoint", { id, reason: args["reason"] ?? null, actor: "ai" }, 600000,
    (result) => [
      `restored: ${String(result["restored"])}`,
      `the state before the restore is checkpoint ${String(result["safety_checkpoint"])}`,
      `file: ${String(result["file"])} (path unchanged, not saved)`,
      "Ctrl+Z in Blender also undoes the restore.",
    ].join("\n"));
}

// ----------------------------------------------------------------------------- dispatch
export async function callTool(ctx: WorkspaceContext, name: string, args: Record<string, unknown>, extras: CallExtras = {}): Promise<ToolOutcome> {
  switch (name) {
    case "doctor":
      return doctor(ctx, false);
    case "doctor_fix":
      return doctor(ctx, true, args["archive"] === true);
    case "open_blend":
      return openBlend(ctx, args);
    case "replay":
      return replay(ctx, args);
    case "session_info":
      return sessionInfo(ctx, args);
    case "launch_blender":
      return launchBlender(ctx, args, false);
    case "launch_blender_background":
      return launchBlender(ctx, args, true);
    case "run_script":
      return runScript(ctx, args, extras);
    case "ingest":
      return ingest(ctx, args);
    case "context_pack":
      return contextPack(ctx, args);
    case "notes":
      return notes(ctx, args);
    case "preview":
      return preview(ctx, args, extras);
    case "render":
      return render(ctx, args, extras);
    case "job_status":
      return jobStatus(ctx, args);
    case "cancel_job":
      return cancelJob(ctx, args);
    case "checkpoint":
      return checkpoint(ctx, args);
    case "restore_checkpoint":
      return restoreCheckpoint(ctx, args);
    case "save":
      return save(ctx, args);
    case "append":
      return append(ctx, args);
    case "import_reference":
      return importReference(ctx, args, extras);
    case "api":
      return bridgeTool(ctx, "api", { query: args["query"], inherited: args["inherited"] === true }, 30000);
    case "node_schema":
      return bridgeTool(ctx, "node_schema", { bl_idname: args["bl_idname"], props: args["props"] ?? null }, 30000);
    case "describe":
      return bridgeTool(ctx, "describe", { target: args["target"], ...(args["frame"] !== undefined ? { frame: Number(args["frame"]) } : {}) },
        60000, undefined, args["path"], extras);
    case "find":
      return bridgeTool(ctx, "find", { selector: args["selector"], limit: args["limit"] ?? 100 }, 60000, undefined, args["path"], extras);
    case "spatial":
      return bridgeTool(ctx, "spatial", args, 120000, (result) => JSON.stringify(result, null, 1), args["path"], extras);
    case "timeline":
      return bridgeTool(ctx, "timeline", args, 120000, undefined, args["path"], extras);
    case "measure":
      return modeling.measure(ctx, args, extras);
    case "check_model":
      return modeling.checkModel(ctx, args, extras);
    case "export_model":
      return modeling.exportModel(ctx, args, extras);
    case "new_blend":
      return modeling.newBlend(ctx, args, extras);
    case "run_pipeline":
      return modeling.runPipeline(ctx, args, extras);
    case "run_project_script":
      return modeling.runProjectScript(ctx, args, extras);
    case "run_project_pipeline":
      return modeling.runProjectPipeline(ctx, args, extras);
    case "set_role":
      return setRole(ctx, args);
    case "diff":
      return diff(ctx, args, extras);
    case "compare":
      return compare(ctx, args, extras);
    case "reference":
      return reference(ctx, args, extras);
    default:
      return fail(`unknown tool ${name}`);
  }
}
