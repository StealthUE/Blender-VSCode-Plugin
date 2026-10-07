import * as crypto from "crypto";
import * as fs from "fs";
import * as https from "https";
import * as path from "path";
import { findCheckpoint, readCheckpoints, relativeTo, resolveInside, sidecarDir } from "./blendFiles";
import { callBridge, probeBridge } from "./bridge";
import { decideWindow } from "./windows";
import { diffHeadless, ingestScript, manifestHeadless } from "./ingest";
import * as jobs from "./jobs";
import { isVideoArg, videoReference } from "./references";
import { bridgeFailure, currentBlend, errorText, fail, imageReply, ok, PREVIEW_BYTES, previewParams, tempPng } from "./tools";
import { CallExtras, ToolOutcome, WorkspaceContext } from "./types";

const IMAGE_FILE = /\.(png|jpe?g|webp|bmp|tiff?)$/i;
const MAX_DOWNLOAD = 15 * 1024 * 1024;

// ----------------------------------------------------------------------------- set_role
export async function setRole(ctx: WorkspaceContext, args: Record<string, unknown>): Promise<ToolOutcome> {
  const target = typeof args["target"] === "string" ? args["target"].trim() : "";
  const role = typeof args["role"] === "string" ? args["role"].trim() : "";
  if (!target || !role) return fail("target and role are required");
  const source = args["source"] === "human" ? "human" : "ai";
  let blend: string;
  try {
    blend = await currentBlend(ctx, args["path"]);
  } catch (error) {
    return fail(errorText(error));
  }
  const folder = sidecarDir(blend);
  const rolesPath = path.join(folder, "roles.json");
  let data: { about?: string; objects: Record<string, Record<string, unknown>> } = { objects: {} };
  try {
    const parsed = JSON.parse(fs.readFileSync(rolesPath, "utf8")) as typeof data;
    if (parsed && typeof parsed.objects === "object") data = parsed;
  } catch {
    data = {
      about: "What each object is for. source: annotation (from the file), inferred (guess, see evidence), ai / human (reviewed; kept across re-ingests).",
      objects: {},
    };
  }
  const existing = data.objects[target];
  let facts: Record<string, unknown> | undefined;
  try {
    const response = await callBridge(ctx.port, "object_facts", { names: [target] }, 8000);
    facts = response.ok ? (response.result?.[target] as Record<string, unknown> | undefined) : undefined;
  } catch {
    facts = undefined;
  }
  if (!existing && !facts) {
    return fail(`${target} is not in roles.json, and the open Blender has no object with that name. Check the name with find.`);
  }
  const evidence = Array.isArray(existing?.["evidence"]) ? (existing["evidence"] as unknown[]).map(String) : [];
  const entry: Record<string, unknown> = {
    ...(existing ?? { type: facts?.["type"], parent: facts?.["parent"] ?? null, dimensions: facts?.["dimensions"], materials: facts?.["materials"] ?? [] }),
    role,
    source,
    confidence: 1,
    needs_review: false,
    evidence: [...evidence.filter((line) => !line.startsWith("set with set_role")), `set with set_role by ${source} on ${new Date().toISOString().slice(0, 10)}`],
  };
  delete entry["changed_since_review"];
  data.objects[target] = entry;
  fs.mkdirSync(folder, { recursive: true });
  fs.writeFileSync(rolesPath, JSON.stringify(data, null, 2) + "\n", "utf8");
  fs.appendFileSync(path.join(folder, "journal.jsonl"),
    JSON.stringify({ time: new Date().toISOString(), event: "role", actor: source, target, role, new_object: !existing }) + "\n", "utf8");
  const lines = [`${target}: ${role} (source ${source})`, `written to ${relativeTo(ctx.workspace, rolesPath)}; kept on re-ingest.`];
  if (!existing) lines.push("This object was not in the sidecar yet; the next ingest adds its other facts.");
  if (args["write_to_file"] === true) {
    try {
      const response = await callBridge(ctx.port, "write_role_property", { target, role }, 15000);
      lines.push(response.ok ? "ai_role was stamped on the object in Blender (one undo step; not saved)." : `ai_role not written: ${response.error ?? "failed"}`);
    } catch (error) {
      lines.push(`ai_role not written: ${bridgeFailure(ctx.port, error, 15000)}`);
    }
  }
  return ok(lines.join("\n"));
}

// ----------------------------------------------------------------------------- diff
async function manifestFor(ctx: WorkspaceContext, side: string, blend: string, temps: string[]): Promise<string> {
  if (side === "sidecar") {
    const file = path.join(sidecarDir(blend), "manifest.json");
    if (!fs.existsSync(file)) throw new Error("there is no ingest yet: call ingest first");
    return file;
  }
  if (side === "live") {
    const file = path.join(ctx.workspace, ".blender-ai", "jobs", `live-${Date.now()}.manifest.json`);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const response = await callBridge(ctx.port, "manifest", { path: file }, 300000);
    if (!response.ok) throw new Error(response.error || "could not read the live session");
    temps.push(file);
    return file;
  }
  const entry = findCheckpoint(blend, side);
  if (!entry) {
    const known = readCheckpoints(blend).slice(-8).map((e) => e.id).join(", ") || "none";
    throw new Error(`${side} is not sidecar, live, or a checkpoint id. Recent checkpoints: ${known}`);
  }
  const file = path.resolve(ctx.workspace, entry.file);
  if (!fs.existsSync(file)) throw new Error(`checkpoint file missing: ${entry.file}`);
  const cached = file.replace(/\.blend$/i, ".manifest.json");
  if (!fs.existsSync(cached)) {
    if (!ctx.blender) throw new Error("No Blender executable is configured. Run VSBlender: Setup.");
    await manifestHeadless(ctx.blender, ingestScript(ctx.extensionRoot), file, cached);
  }
  return cached;
}

export async function diff(ctx: WorkspaceContext, args: Record<string, unknown>, _extras: CallExtras = {}): Promise<ToolOutcome> {
  const a = typeof args["a"] === "string" ? args["a"].trim() : "";
  const b = typeof args["b"] === "string" ? args["b"].trim() : "";
  if (!a || !b) return fail("a and b are required: sidecar, live, or a checkpoint id");
  const temps: string[] = [];
  try {
    const blend = await currentBlend(ctx, args["path"]);
    const fileA = await manifestFor(ctx, a, blend, temps);
    const fileB = await manifestFor(ctx, b, blend, temps);
    let changes: unknown[];
    const probe = await probeBridge(ctx.port);
    const ours = probe.ok && decideWindow(ctx.workspace, probe.window ?? { unknown: true }).use;
    if (ours) {
      const response = await callBridge(ctx.port, "diff_manifests", { a: fileA, b: fileB }, 60000);
      if (!response.ok) throw new Error(response.error || "diff failed");
      changes = Array.isArray(response.result?.["changes"]) ? response.result["changes"] as unknown[] : [];
    } else {
      if (!ctx.blender) throw new Error("No Blender executable is configured. Run VSBlender: Setup.");
      const out = path.join(ctx.workspace, ".blender-ai", "jobs", `diff-${Date.now()}.json`);
      temps.push(out);
      changes = await diffHeadless(ctx.blender, ingestScript(ctx.extensionRoot), fileA, fileB, out);
    }
    const lines = changes.map((item) => {
      const change = item as { subject?: string; change?: string };
      return `- ${change.subject ?? "?"}: ${change.change ?? ""}`;
    });
    const head = `${changes.length} change(s) from ${a} to ${b}`;
    return ok(lines.length ? `${head}\n${lines.slice(0, 300).join("\n")}${lines.length > 300 ? `\n... +${lines.length - 300} more` : ""}` : `${head}: none`);
  } catch (error) {
    return fail(errorText(error));
  } finally {
    for (const file of temps) fs.rmSync(file, { force: true });
  }
}

// ----------------------------------------------------------------------------- compare / reference
interface Source {
  file: string;
  label: string;
  temp: boolean;
}

async function livePreview(ctx: WorkspaceContext, params: Record<string, unknown>): Promise<string> {
  const out = tempPng("compare-live");
  const response = await callBridge(ctx.port, "preview", { ...params, out }, 240000);
  if (!response.ok) throw new Error(response.error || "preview failed");
  return String(response.result?.["file"] ?? out);
}

async function sourceImage(ctx: WorkspaceContext, source: string, params: Record<string, unknown>, blend: string | undefined, extras: CallExtras): Promise<Source> {
  if (source === "live") {
    const probe = await probeBridge(ctx.port);
    if (probe.ok && !decideWindow(ctx.workspace, probe.window ?? { unknown: true }).use) {
      throw new Error("the open Blender belongs to another folder, so live is that window. Compare a checkpoint or an image, or finish that chat first.");
    }
    return { file: await livePreview(ctx, params), label: "live", temp: true };
  }
  if (IMAGE_FILE.test(source)) {
    const file = resolveInside(ctx.workspace, source);
    if (!fs.existsSync(file)) throw new Error(`image not found: ${source}`);
    return { file, label: path.basename(file), temp: false };
  }
  const entry = blend ? findCheckpoint(blend, source) : undefined;
  if (!entry) throw new Error(`${source} is not live, a workspace image, or a checkpoint id`);
  const checkpointFile = path.resolve(ctx.workspace, entry.file);
  const out = tempPng(`compare-${entry.id}`);
  await jobs.runToEnd(ctx, {
    blend: checkpointFile,
    spec: { kind: "preview", params, out },
    kind: "preview",
    label: `preview of checkpoint ${entry.id}`,
    factoryStartup: true,
  }, 300000, extras.signal);
  return { file: out, label: `${entry.id}${entry.label ? ` (${entry.label})` : ""}`, temp: true };
}

async function composeImages(ctx: WorkspaceContext, paths: string[], labels: string[], diffPanel: boolean, cell: number, signal?: AbortSignal): Promise<{ file: string; stats?: Record<string, unknown> }> {
  const out = tempPng("compare");
  // The sheet, not the panels, is what has to fit in the tool reply.
  const spec = { paths, out, labels, diff: diffPanel, cell, max_bytes: PREVIEW_BYTES };
  const probe = await probeBridge(ctx.port);
  let result: Record<string, unknown>;
  if (probe.ok && decideWindow(ctx.workspace, probe.window ?? { unknown: true }).use) {
    const response = await callBridge(ctx.port, "compose", spec, 120000);
    if (!response.ok) throw new Error(response.error || "compose failed");
    result = response.result ?? {};
  } else {
    result = await jobs.runToEnd(ctx, { spec: { kind: "compose", ...spec }, kind: "compose", label: "image sheet", factoryStartup: true }, 120000, signal);
  }
  const stats = result["difference"] as Record<string, unknown> | undefined;
  return { file: String(result["file"] ?? out), ...(stats ? { stats } : {}) };
}

export async function compare(ctx: WorkspaceContext, args: Record<string, unknown>, extras: CallExtras = {}): Promise<ToolOutcome> {
  const a = typeof args["a"] === "string" ? args["a"].trim() : "";
  const b = typeof args["b"] === "string" && args["b"].trim() ? args["b"].trim() : "live";
  if (!a) return fail("a is required: a checkpoint id, live, or a workspace image");
  const params = previewParams({ size: 512, ...args });
  delete params["max_bytes"];
  delete params["views"];
  const sources: Source[] = [];
  try {
    const needsBlend = ![a, b].every((s) => s === "live" || IMAGE_FILE.test(s));
    const blend = needsBlend ? await currentBlend(ctx, args["path"]) : undefined;
    for (const source of [a, b]) sources.push(await sourceImage(ctx, source, params, blend, extras));
    const heat = args["heatmap"] !== false;
    const composed = await composeImages(ctx, sources.map((s) => s.file), sources.map((s) => s.label), heat, Number(params["size"]) || 512, extras.signal);
    const lines = [`${sources[0]?.label} | ${sources[1]?.label}${heat ? " | difference" : ""}`];
    if (composed.stats) {
      lines.push(`difference: ${String(composed.stats["changed_percent"])}% of pixels changed, mean ${String(composed.stats["mean_difference"])}, max ${String(composed.stats["max_difference"])}`);
    }
    lines.push(`view: ${String(params["view"] ?? "iso")}, shading ${String(params["shading"] ?? "solid")}${params["target"] ? `, target ${String(params["target"])}` : ""}`);
    return imageReply(composed.file, ctx.workspace, lines, args["save"] === true);
  } catch (error) {
    return fail(errorText(error));
  } finally {
    for (const source of sources) if (source.temp) fs.rmSync(source.file, { force: true });
  }
}

function download(url: string, redirects = 3): Promise<{ data: Buffer; type: string }> {
  return new Promise((resolve, reject) => {
    const request = https.get(url, { headers: { "User-Agent": "VSBlender reference fetch" }, timeout: 30000 }, (response) => {
      const status = response.statusCode ?? 0;
      if (status >= 300 && status < 400 && response.headers.location && redirects > 0) {
        response.resume();
        const next = new URL(response.headers.location, url).toString();
        if (!next.startsWith("https://")) {
          reject(new Error(`refusing a redirect to ${next}: only https`));
          return;
        }
        download(next, redirects - 1).then(resolve, reject);
        return;
      }
      if (status !== 200) {
        response.resume();
        reject(new Error(`${url} answered ${status}`));
        return;
      }
      const type = String(response.headers["content-type"] ?? "").split(";")[0]?.trim().toLowerCase() ?? "";
      if (!/^image\/(png|jpeg|webp|bmp|tiff)$/.test(type)) {
        response.resume();
        reject(new Error(`${url} is ${type || "not an image"}; only png, jpeg, webp, bmp and tiff are accepted`));
        return;
      }
      const chunks: Buffer[] = [];
      let size = 0;
      response.on("data", (chunk: Buffer) => {
        size += chunk.length;
        if (size > MAX_DOWNLOAD) {
          request.destroy(new Error(`${url} is over 15 MB`));
          return;
        }
        chunks.push(chunk);
      });
      response.on("end", () => resolve({ data: Buffer.concat(chunks), type }));
      response.on("error", reject);
    });
    request.on("timeout", () => request.destroy(new Error(`${url} timed out`)));
    request.on("error", reject);
  });
}

export async function reference(ctx: WorkspaceContext, args: Record<string, unknown>, extras: CallExtras = {}): Promise<ToolOutcome> {
  if (isVideoArg(args)) return videoReference(ctx, args, extras);
  const images = Array.isArray(args["images"]) ? args["images"].filter((item): item is string => typeof item === "string" && Boolean(item.trim())) : [];
  if (!images.length) return fail("images is required (workspace image paths or https URLs), or video with times or fps");
  if (images.length > 6) return fail("at most 6 reference images");
  const files: string[] = [];
  const labels: string[] = [];
  let live: string | undefined;
  try {
    const refDir = path.join(ctx.workspace, ".blender-ai", "references");
    for (const image of images) {
      if (/^https:\/\//i.test(image)) {
        const { data, type } = await download(image);
        const ext = type === "image/jpeg" ? ".jpg" : `.${type.split("/")[1]}`;
        fs.mkdirSync(refDir, { recursive: true });
        const file = path.join(refDir, crypto.createHash("sha1").update(image).digest("hex").slice(0, 16) + ext);
        fs.writeFileSync(file, data);
        files.push(file);
        labels.push(`reference: ${new URL(image).hostname}`);
      } else if (/^[a-z]+:\/\//i.test(image)) {
        return fail(`${image}: only https URLs are fetched`);
      } else {
        const file = resolveInside(ctx.workspace, image);
        if (!fs.existsSync(file) || !IMAGE_FILE.test(file)) return fail(`not an image in the workspace: ${image}`);
        files.push(file);
        labels.push(`reference: ${path.basename(file)}`);
      }
    }
    const params = previewParams({ size: 512, ...args });
    delete params["max_bytes"];
    delete params["views"];
    live = await livePreview(ctx, params);
    files.push(live);
    labels.push(`live: ${String(params["view"] ?? "iso")}${params["target"] ? ` - ${String(params["target"])}` : ""}`);
    const composed = await composeImages(ctx, files, labels, false, Number(params["size"]) || 512);
    const lines = [`${images.length} reference image(s) | live preview`, "Downloaded references are kept in .blender-ai/references/."];
    return imageReply(composed.file, ctx.workspace, lines, args["save"] === true);
  } catch (error) {
    return fail(/ECONNREFUSED/.test(errorText(error)) ? bridgeFailure(ctx.port, error, 0) : errorText(error));
  } finally {
    if (live) fs.rmSync(live, { force: true });
  }
}
