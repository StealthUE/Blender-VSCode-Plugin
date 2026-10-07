import * as fs from "fs";
import * as path from "path";
import { listBlendFiles, relativeTo, resolveInside, sidecarDir } from "./blendFiles";
import { callBridge } from "./bridge";
import * as jobs from "./jobs";
import { printerForCall } from "./printerLookup";
import { describePrinter, printerParams } from "./printers";
import { readConfig, writeConfig } from "./projectConfig";
import {
  bridgeError,
  bridgeFailure,
  callWithProgress,
  checkpointLine,
  checkpointParams,
  ensureSceneWindow,
  errorText,
  fail,
  imageReply,
  importPaths,
  liveBlend,
  ok,
  PREVIEW_BYTES,
  referenceRoots,
  routeCall,
  runScript,
  saveLines,
  saveRefusal,
  unsavedBanner,
  sourceNote,
  tempPng,
} from "./tools";
import { CallExtras, ToolOutcome, WorkspaceContext } from "./types";

const PURPOSES = ["general", "render", "game", "print"];
const TEMPLATES = ["empty", "render", "game", "print_mm"];
const EXPORT_FORMATS = ["3mf", "stl", "obj", "glb", "gltf", "fbx", "usd", "usda", "usdc", "usdz", "ply"];

function str(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function copyKeys(args: Record<string, unknown>, keys: string[]): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const key of keys) {
    const value = args[key];
    if (value !== undefined && value !== null && value !== "") out[key] = value;
  }
  return out;
}

function failed(ctx: WorkspaceContext, error: unknown, timeout: number): ToolOutcome {
  const text = errorText(error);
  if (/did not finish|failed:|not running|refusing|outside|must be/i.test(text)) return fail(text);
  return fail(bridgeFailure(ctx.port, error, timeout));
}

// ----------------------------------------------------------------------------- check_model
export async function checkModel(ctx: WorkspaceContext, args: Record<string, unknown>, extras: CallExtras = {}): Promise<ToolOutcome> {
  const purpose = str(args["purpose"]);
  if (purpose && !PURPOSES.includes(purpose)) return fail(`purpose must be one of ${PURPOSES.join(", ")}`);
  const printer = (await printerForCall(ctx, args["printer"])).params;
  const params: Record<string, unknown> = {
    ...copyKeys(args, ["targets", "purpose", "deep", "max_tris", "mm_per_unit", "suggest_orientation", "place_on_bed", "views", "size", "frame"]),
    printer,
    image: args["image"] !== false,
    out: tempPng("check"),
    max_bytes: PREVIEW_BYTES,
  };
  const timeout = 300000;
  try {
    const routed = await routeCall(ctx, "check_model", params, timeout, args["path"], extras);
    const response = routed.response;
    if (!response.ok) return fail(bridgeError(response, "check_model"));
    const result = response.result ?? {};
    const notes = (printer["warnings"] as string[] | undefined) ?? [];
    const lines = [
      sourceNote(ctx, routed).trim(),
      String(result["text"] ?? ""),
      notes.length ? `printer: ${notes.join("; ")}` : "",
      typeof result["legend"] === "string" && result["legend"] ? `image: ${String(result["legend"])}` : "",
      response.warnings?.length ? `warnings: ${response.warnings.join("; ")}` : "",
    ].filter(Boolean);
    const file = typeof result["file"] === "string" ? result["file"] : "";
    if (file && fs.existsSync(file)) {
      return { ...imageReply(file, ctx.workspace, lines, args["save"] === true), details: result };
    }
    return ok(lines.join("\n"), { details: result });
  } catch (error) {
    return failed(ctx, error, timeout);
  }
}

// ----------------------------------------------------------------------------- export_model
function appendJournal(blend: string | undefined, workspace: string, entry: Record<string, unknown>): void {
  const dir = blend ? sidecarDir(blend) : path.join(workspace, ".blender-ai", "untitled");
  try {
    fs.mkdirSync(dir, { recursive: true });
    fs.appendFileSync(path.join(dir, "journal.jsonl"), JSON.stringify(entry) + "\n", "utf8");
  } catch {
    // The export itself succeeded; the journal is a convenience.
  }
}

export async function exportModel(ctx: WorkspaceContext, args: Record<string, unknown>, extras: CallExtras = {}): Promise<ToolOutcome> {
  const format = str(args["format"])?.toLowerCase().replace(/^\./, "");
  if (format && !EXPORT_FORMATS.includes(format)) return fail(`format must be one of ${EXPORT_FORMATS.join(", ")}`);
  const purpose = str(args["purpose"]);
  if (purpose && !PURPOSES.includes(purpose)) return fail(`purpose must be one of ${PURPOSES.join(", ")}`);
  const output = str(args["output"]) ?? str(args["path_out"]);
  if (output) {
    try {
      resolveInside(ctx.workspace, output);
    } catch (error) {
      return fail(errorText(error));
    }
  }
  const printer = (await printerForCall(ctx, args["printer"])).params;
  const params: Record<string, unknown> = {
    ...copyKeys(args, ["targets", "purpose", "split", "overwrite", "place_on_bed", "center", "assembly", "ascii",
      "apply_modifiers", "mm_per_unit", "reason"]),
    ...(format ? { format } : {}),
    ...(output ? { path: output } : {}),
    printer,
    actor: "ai",
  };
  const timeout = 300000;
  let routed;
  try {
    routed = await routeCall(ctx, "export_model", params, timeout, args["path"], extras);
  } catch (error) {
    return failed(ctx, error, timeout);
  }
  const response = routed.response;
  if (!response.ok) return fail(bridgeError(response, "export_model"));
  const result = response.result ?? {};
  const job = result["job"] as Record<string, unknown> | undefined;
  if (!job) {
    const lines = [sourceNote(ctx, routed).trim(), String(result["text"] ?? ""),
      response.warnings?.length ? `warnings:\n${response.warnings.join("\n")}` : ""].filter(Boolean);
    return ok(lines.join("\n"), { details: result });
  }
  // glTF, FBX, USD, OBJ, PLY: Blender's exporter needs selection, so it runs on a copy in a background Blender.
  const id = jobs.newJobId("export");
  const dir = path.join(jobs.jobsDir(ctx.workspace), id);
  fs.mkdirSync(dir, { recursive: true });
  const copy = path.join(dir, "scene.blend");
  let blendForExport = copy;
  if (routed.source === "live") {
    try {
      const saved = await callBridge(ctx.port, "save_copy", { path: copy }, 300000);
      if (!saved.ok) return fail(bridgeError(saved, "save_copy"));
    } catch (error) {
      return failed(ctx, error, 300000);
    }
  } else {
    blendForExport = routed.blend ?? copy;
  }
  let done: Record<string, unknown>;
  try {
    done = await jobs.runToEnd(ctx, {
      blend: blendForExport,
      spec: { kind: "export", export: job },
      kind: "export",
      label: `${String(job["format"])} export`,
      ...(routed.source === "live" ? { scratch: copy } : {}),
    }, 600000, extras.signal);
  } catch (error) {
    return fail(errorText(error));
  }
  const files = (done["files"] as Record<string, unknown>[] | undefined) ?? [];
  const blend = await liveBlend(ctx);
  const rows: Record<string, unknown>[] = files.map((file) => ({ ...file, file: relativeTo(ctx.workspace, String(file["file"])) }));
  appendJournal(blend, ctx.workspace, {
    time: new Date().toISOString(), event: "export", actor: "ai", format: job["format"], purpose: job["purpose"],
    files: rows, reason: args["reason"] ?? null,
  });
  const lines = [`exported ${rows.length} file(s) as ${String(job["format"])} with Blender's exporter (from a copy of the session; your selection is unchanged)`];
  for (const row of rows) {
    lines.push(`- ${String(row["file"])}: ${Math.round(Number(row["bytes"]) / 1024)} KB (${(row["objects"] as string[] | undefined ?? []).join(", ")})`);
  }
  if (Number(job["bu_to_m"]) !== 1 && ["glb", "gltf", "usd", "usda", "usdc", "usdz", "fbx"].includes(String(job["format"]))) {
    lines.push(`scaled from ${String(job["units"])} to metres, as ${String(job["format"]).toUpperCase()} expects`);
  }
  return ok(lines.join("\n"), { details: { files: rows, format: job["format"] } });
}

/** Trust <blend folder>/scripts, including before that folder exists. Returns the prefix, or undefined when the workspace has no config. */
export function trustScriptsFolder(workspace: string, blendFile: string): string | undefined {
  const prefix = relativeTo(workspace, path.join(path.dirname(blendFile), "scripts"));
  const config = readConfig(workspace);
  if (!config) return undefined;
  const have = (config.trustedScripts ?? []).map((item) => item.replace(/\\/g, "/").replace(/\/+$/, ""));
  if (!have.includes(prefix)) {
    writeConfig(workspace, { ...config, trustedScripts: [...(config.trustedScripts ?? []), prefix] });
  }
  return prefix;
}

// ----------------------------------------------------------------------------- new_blend
export async function newBlend(ctx: WorkspaceContext, args: Record<string, unknown>, _extras: CallExtras = {}): Promise<ToolOutcome> {
  if (!ctx.blender || !fs.existsSync(ctx.blender)) return fail("No Blender executable is configured. Run VSBlender: Setup.");
  const requested = str(args["path"]);
  if (!requested) return fail("path is required: the new .blend, e.g. Models/lamp.blend");
  let abs: string;
  try {
    abs = resolveInside(ctx.workspace, requested);
  } catch (error) {
    return fail(errorText(error));
  }
  if (!abs.toLowerCase().endsWith(".blend")) return fail("path must end in .blend");
  if (fs.existsSync(abs)) return fail(`${relativeTo(ctx.workspace, abs)} exists. new_blend never overwrites a file; pick another path.`);
  const template = str(args["template"]) ?? "empty";
  if (!TEMPLATES.includes(template)) return fail(`template must be one of ${TEMPLATES.join(", ")}`);
  const { profile, warnings } = await printerForCall(ctx, args["printer"]);
  let result: Record<string, unknown>;
  try {
    result = await jobs.runToEnd(ctx, {
      spec: { kind: "new_blend", path: abs, template, printer: printerParams(profile) },
      kind: "new_blend",
      label: `new ${template} file`,
      factoryStartup: true,
    }, 180000);
  } catch (error) {
    return fail(errorText(error));
  }
  const rel = relativeTo(ctx.workspace, abs);
  const lines = [`created ${rel} from the ${template} template (${String(result["units"])})`];
  const trusted = trustScriptsFolder(ctx.workspace, abs);
  for (const item of (result["applied"] as string[] | undefined) ?? []) lines.push(`- ${item}`);
  if (template === "print_mm") lines.push(`printer: ${describePrinter(profile)}${warnings.length ? `; ${warnings.join("; ")}` : ""}`);
  if (args["open"] !== false) {
    const ensured = await ensureSceneWindow(ctx, abs);
    if (ensured.error) lines.push(`not opened: ${ensured.error}`);
    else lines.push(ensured.note ?? "opened it in Blender");
  } else {
    lines.push(`open it with launch_blender {"file": "${rel}"}`);
  }
  if (trusted) {
    lines.push(`Put its scripts in ${trusted}/ (trusted, including before that folder exists) and shared code in scripts/lib/.`);
  } else {
    const prefix = relativeTo(ctx.workspace, path.join(path.dirname(abs), "scripts"));
    lines.push(`Put its scripts in ${prefix}/ and shared code in scripts/lib/.`);
  }
  return ok(lines.join("\n"), { details: result });
}

// ----------------------------------------------------------------------------- measure
export async function measure(ctx: WorkspaceContext, args: Record<string, unknown>, extras: CallExtras = {}): Promise<ToolOutcome> {
  const op = str(args["op"]) ?? "section";
  const params: Record<string, unknown> = { ...args, op, reference_roots: referenceRoots(ctx) };
  delete params["path"];
  const requestedOut = str(args["out"]);
  const jsonOut = op === "section" && !!requestedOut?.toLowerCase().endsWith(".json");
  if (jsonOut && requestedOut) {
    try {
      params["out"] = resolveInside(ctx.workspace, requestedOut);
    } catch (error) {
      return fail(errorText(error));
    }
  } else if (op === "depthmap" && args["image"] !== false) {
    params["out"] = tempPng("depthmap");
  }
  // A section or profile drawn on a grid (both sources, with compare_to) is often worth more than the numbers.
  if (!jsonOut && (op === "section" || op === "profile") && args["image"] === true) params["out"] = tempPng(`measure-${op}`);
  const timeout = 180000;
  try {
    const routed = await routeCall(ctx, "measure", params, timeout, args["path"], extras);
    const response = routed.response;
    if (!response.ok) return fail(bridgeError(response, "measure"));
    const result = response.result ?? {};
    const text = sourceNote(ctx, routed) + String(result["text"] ?? JSON.stringify(result, null, 1))
      + (response.warnings?.length ? `\nwarnings: ${response.warnings.join("; ")}` : "");
    const file = typeof result["file"] === "string" ? result["file"] : "";
    if (file && fs.existsSync(file) && !file.toLowerCase().endsWith(".json")) {
      return { ...imageReply(file, ctx.workspace, [text], args["save"] === true), details: result };
    }
    return ok(text, { details: result });
  } catch (error) {
    return failed(ctx, error, timeout);
  }
}

// ----------------------------------------------------------------------------- pipelines
function findPipelines(workspace: string): string[] {
  const found: string[] = [];
  const skip = new Set(["node_modules", ".git", "out", ".blender-ai", "dist"]);
  const walk = (dir: string, depth: number): void => {
    if (depth > 5) return;
    let entries: fs.Dirent[];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory() && !skip.has(entry.name) && !entry.name.startsWith(".")) walk(full, depth + 1);
      else if (entry.isFile() && entry.name === "pipeline.json") found.push(full);
    }
  };
  walk(workspace, 0);
  return found;
}

/** The pipeline.json a call is about. within: only look in these workspace folders (the trusted ones). */
function pipelineFor(ctx: WorkspaceContext, args: Record<string, unknown>, within?: string[]): string {
  const requested = str(args["pipeline"]);
  if (requested) {
    const abs = resolveInside(ctx.workspace, requested);
    if (fs.existsSync(abs) && fs.statSync(abs).isDirectory()) {
      const inside = path.join(abs, "pipeline.json");
      if (!fs.existsSync(inside)) throw new Error(`no pipeline.json in ${requested}`);
      return inside;
    }
    if (!fs.existsSync(abs)) throw new Error(`file not found: ${requested}`);
    return abs;
  }
  const step = str(args["from"]);
  const roots = within?.map((folder) => path.resolve(ctx.workspace, folder));
  const all = findPipelines(ctx.workspace).filter((file) => {
    if (roots && !roots.some((root) => !path.relative(root, file).startsWith(".."))) return false;
    if (!step) return true;
    try {
      const steps = (JSON.parse(fs.readFileSync(file, "utf8")) as { steps?: string[] }).steps ?? [];
      return steps.some((s) => path.basename(s, ".py") === path.basename(step, ".py"));
    } catch {
      return false;
    }
  });
  if (all.length === 1) return all[0]!;
  if (!all.length) throw new Error("no pipeline.json found. Write <scripts folder>/pipeline.json: {\"steps\": [\"01_base\", \"02_detail\"], \"after\": {\"01_base\": [\"02_detail\"]}}");
  throw new Error(`more than one pipeline.json; pass pipeline:\n${all.map((f) => relativeTo(ctx.workspace, f)).join("\n")}`);
}

export async function runPipeline(ctx: WorkspaceContext, args: Record<string, unknown>, extras: CallExtras = {},
  resolved?: string): Promise<ToolOutcome> {
  let file: string;
  try {
    file = resolved ?? pipelineFor(ctx, args);
  } catch (error) {
    return fail(errorText(error));
  }
  const refused = saveRefusal(ctx, args);
  if (refused) return fail(refused);
  const ensured = await ensureSceneWindow(ctx);
  if (ensured.error) return fail(ensured.error);
  const params: Record<string, unknown> = {
    pipeline: file,
    ...copyKeys(args, ["from", "to", "mode", "reason"]),
    actor: "ai",
    atomic: args["atomic"] !== false,
    checkpoint: checkpointParams(ctx, args),
    lib_paths: importPaths(ctx),
    reference_roots: referenceRoots(ctx),
    printer: (await printerForCall(ctx)).params,
  };
  if (args["save"] === true) params["save"] = true;
  if (args["allow_references"] === true) params["allow_references"] = true;
  const timeout = Math.min(600000, Math.max(1000, Number(args["timeout_ms"]) || 300000));
  const rel = relativeTo(ctx.workspace, file);
  try {
    const response = await callWithProgress(ctx, "run_pipeline", params, timeout, extras, `running ${rel}`);
    if (!response.ok) return fail(`pipeline: ${rel}\n${bridgeError(response, "run_pipeline")}`, { changed: response.changed ?? [] });
    const result = response.result ?? {};
    const steps = (result["steps"] as Record<string, unknown>[] | undefined) ?? [];
    const lines = [`pipeline: ${rel}`, `ran ${steps.length} step(s) as one undo step`, checkpointLine(result, "all of them")];
    for (const step of steps) lines.push(`- ${String(step["step"])}: ${String(step["changes"] ?? "")}`);
    if (response.warnings?.length) lines.push(`warnings:\n${response.warnings.join("\n")}`);
    lines.push(...saveLines(result));
    if (ensured.note) lines.push(ensured.note);
    const banner = unsavedBanner(result);
    if (banner) lines.unshift(banner);
    return ok(lines.join("\n"), { details: result });
  } catch (error) {
    return failed(ctx, error, timeout);
  }
}

/** The real path of a file when it is inside one of the trusted folders, else why not. */
function trustedPath(ctx: WorkspaceContext, requested: string, what: string): { real: string } | { problem: string } {
  const trusted = ctx.trustedScripts ?? [];
  const underPrefix = (relFile: string, folder: string): boolean => {
    const root = folder.replace(/\\/g, "/").replace(/^\.\//, "").replace(/\/+$/, "");
    if (!root || root.split("/").includes("..")) return false;
    return relFile === root || relFile.startsWith(`${root}/`);
  };
  if (!trusted.length) {
    return { problem: "no trusted script folders. Add \"trustedScripts\": [\"Models/scripts\"] to .blender-ai/config.json, "
      + `or use ${what === "pipeline" ? "run_pipeline" : "run_script"}.` };
  }
  let real: string;
  try {
    real = fs.realpathSync(resolveInside(ctx.workspace, requested));
  } catch (error) {
    return { problem: errorText(error) };
  }
  const rel = relativeTo(ctx.workspace, real);
  if (rel.split("/").includes(".blender-ai")) return { problem: `${what}s under .blender-ai/ are never trusted` };
  const inside = trusted.some((folder) => {
    let root: string;
    try {
      root = fs.realpathSync(resolveInside(ctx.workspace, folder));
    } catch {
      // new_blend trusts <blend folder>/scripts before that folder exists.
      return underPrefix(rel, folder);
    }
    const relative = path.relative(root, real);
    return !!relative && !relative.startsWith("..") && !path.isAbsolute(relative);
  });
  if (!inside) {
    return { problem: `${rel} is not inside a trusted folder (${trusted.join(", ")}). Use ${what === "pipeline" ? "run_pipeline" : "run_script"} for other ${what}s.` };
  }
  return { real };
}

/** run_script for files in the workspace's trusted folders only. */
export async function runProjectScript(ctx: WorkspaceContext, args: Record<string, unknown>, extras: CallExtras = {}): Promise<ToolOutcome> {
  const requested = str(args["path"]);
  if ((ctx.trustedScripts ?? []).length && !requested) return fail("path is required");
  const found = trustedPath(ctx, requested ?? ".", "script");
  if ("problem" in found) return fail(found.problem);
  return runScript(ctx, { ...args, path: found.real }, extras);
}

/** run_pipeline for a pipeline.json inside a trusted folder: its steps are trusted scripts too. */
export async function runProjectPipeline(ctx: WorkspaceContext, args: Record<string, unknown>, extras: CallExtras = {}): Promise<ToolOutcome> {
  let file: string;
  try {
    file = pipelineFor(ctx, args, ctx.trustedScripts ?? []);
  } catch (error) {
    return fail(errorText(error));
  }
  const found = trustedPath(ctx, relativeTo(ctx.workspace, file), "pipeline");
  if ("problem" in found) return fail(found.problem);
  return runPipeline(ctx, args, extras, found.real);
}

export function blendCount(ctx: WorkspaceContext): number {
  return listBlendFiles(ctx.workspace).length;
}
