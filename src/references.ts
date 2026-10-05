import * as fs from "fs";
import * as path from "path";
import { relativeTo, sidecarDir } from "./blendFiles";
import { findFfmpeg } from "./ffmpeg";
import * as jobs from "./jobs";
import { currentBlend, errorText, fail, imageReply, ok, PREVIEW_BYTES, referenceRoots, tempPng } from "./tools";
import { CallExtras, ToolOutcome, WorkspaceContext } from "./types";

const IMPORTABLE = /\.(obj|fbx|gltf|glb|stl|ply)$/i;
const VIDEO = /\.(mp4|mov|m4v|mkv|webm|avi)$/i;

/** What a file's numbers mean when the call does not say: STL is millimetres, glTF and FBX arrive in metres. */
export function defaultUnits(file: string): { units: string; assumed: boolean } {
  const ext = path.extname(file).toLowerCase();
  if (ext === ".stl") return { units: "mm", assumed: true };
  if ([".gltf", ".glb", ".fbx"].includes(ext)) return { units: "m", assumed: false };
  return { units: "m", assumed: true };
}

export function safeName(name: string): string {
  return name.replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^_+|_+$/g, "") || "reference";
}

interface ReferenceEntry {
  name: string;
  file?: string;
  parts?: Record<string, string>;
  units?: string;
  transform?: Record<string, unknown>;
  [key: string]: unknown;
}

export function readReferences(file: string): ReferenceEntry[] {
  try {
    const data: unknown = JSON.parse(fs.readFileSync(file, "utf8"));
    const items = Array.isArray(data) ? data : (data as { references?: unknown }).references;
    return Array.isArray(items) ? (items as ReferenceEntry[]).filter((item) => item && typeof item.name === "string") : [];
  } catch {
    return [];
  }
}

/** Add or replace one entry of references.json, keeping the others and their order. */
export function writeReference(file: string, entry: ReferenceEntry): void {
  const items = readReferences(file);
  const index = items.findIndex((item) => item.name === entry.name);
  if (index >= 0) items[index] = entry;
  else items.push(entry);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify({ references: items }, null, 2) + "\n", "utf8");
}

function fileArg(ctx: WorkspaceContext, raw: unknown, what: string): string {
  if (typeof raw !== "string" || !raw.trim()) throw new Error(`${what} is required`);
  const value = raw.trim();
  const abs = path.isAbsolute(value) ? path.resolve(value) : path.resolve(ctx.workspace, value);
  if (!fs.existsSync(abs) || !fs.statSync(abs).isFile()) throw new Error(`file not found: ${value}`);
  return abs;
}

function mb(bytes: number): string {
  return `${(bytes / 1e6).toFixed(1)} MB`;
}

/**
 * A big reference (a scan, a show model, a CAD export) imported once with Blender's own importer in a
 * background Blender, split into parts by material or object, and cached as binary STL in the
 * sidecar (refs/<name>/), with a light overlay of the whole. Registered in references.json with its
 * units and transform, so preview overlays, preview ref= and measure ref= use it by name, and nothing
 * of it enters the .blend or its checkpoints. Called again with the same file it reuses the parts
 * and only updates the transform.
 */
export async function importReference(ctx: WorkspaceContext, args: Record<string, unknown>, extras: CallExtras = {}): Promise<ToolOutcome> {
  if (!ctx.blender || !fs.existsSync(ctx.blender)) return fail("No Blender executable is configured. Run VSBlender: Setup.");
  let source: string;
  let blend: string;
  try {
    source = fileArg(ctx, args["file"], "file");
    blend = await currentBlend(ctx, args["path"]);
  } catch (error) {
    return fail(errorText(error));
  }
  if (!IMPORTABLE.test(source)) return fail("import_reference reads OBJ, FBX, glTF/GLB, STL and PLY files");
  const name = safeName(typeof args["name"] === "string" && args["name"].trim() ? args["name"].trim() : path.basename(source, path.extname(source)));
  const split = typeof args["split"] === "string" ? args["split"] : "material";
  if (!["material", "object", "none"].includes(split)) return fail("split must be material, object or none");
  const axes = args["axes"] === "native" ? "native" : "blender";
  const unitGuess = defaultUnits(source);
  const units = typeof args["units"] === "string" && args["units"].trim() ? args["units"].trim() : unitGuess.units;
  const sidecar = sidecarDir(blend);
  const outDir = path.join(sidecar, "refs", name);
  const stamp = path.join(outDir, "_source.json");
  const stat = fs.statSync(source);
  const key = { file: source, size: stat.size, mtimeMs: Math.round(stat.mtimeMs), split, axes };
  let result: Record<string, unknown> | undefined;
  let reused = false;
  try {
    const previous = JSON.parse(fs.readFileSync(stamp, "utf8")) as { key?: unknown; result?: Record<string, unknown> };
    if (args["force"] !== true && JSON.stringify(previous.key) === JSON.stringify(key) && previous.result) {
      const parts = (previous.result["parts"] ?? {}) as Record<string, string>;
      if (Object.values(parts).every((file) => fs.existsSync(file))) {
        result = previous.result;
        reused = true;
      }
    }
  } catch {
    result = undefined;
  }
  if (!result) {
    const spec: Record<string, unknown> = { kind: "import_reference", file: source, out_dir: outDir, split, axes };
    if (Number(args["overlay_cell"]) > 0) spec["overlay_cell"] = Number(args["overlay_cell"]);
    if (args["radial"] && typeof args["radial"] === "object") spec["radial"] = args["radial"];
    try {
      result = await jobs.runToEnd(ctx, { spec, kind: "import_reference", label: `import of ${path.basename(source)}`, factoryStartup: true },
        Math.min(3600000, Math.max(60000, Number(args["timeout_ms"]) || 1800000)), extras.signal);
    } catch (error) {
      return fail(errorText(error));
    }
    fs.mkdirSync(outDir, { recursive: true });
    fs.writeFileSync(stamp, JSON.stringify({ key, result }, null, 2) + "\n", "utf8");
  }
  const parts = (result["parts"] ?? {}) as Record<string, string>;
  const rel = (file: string): string => relativeTo(ctx.workspace, file);
  const transform = args["transform"] && typeof args["transform"] === "object" ? { ...(args["transform"] as Record<string, unknown>) } : {};
  delete transform["units"];
  const entry: ReferenceEntry = {
    name,
    file: rel(String(result["overlay"])),
    parts: Object.fromEntries(Object.entries(parts).map(([part, file]) => [part, rel(file)])),
    units,
    transform: { ...transform, units },
    source,
    axes,
    split,
    tris: result["tris"],
    imported: new Date().toISOString(),
  };
  const refsFile = path.join(sidecar, "references.json");
  writeReference(refsFile, entry);
  try {
    fs.appendFileSync(path.join(sidecar, "journal.jsonl"), JSON.stringify({
      time: new Date().toISOString(), event: "reference_import", actor: "ai", name, source, parts: Object.keys(parts).length,
      reused, transform: entry.transform,
    }) + "\n", "utf8");
  } catch {
    // The reference is registered; the journal is a convenience.
  }
  const rows = ((result["rows"] ?? []) as Record<string, unknown>[]).slice().sort((a, b) => Number(b["tris"]) - Number(a["tris"]));
  const bytes = rows.reduce((sum, row) => sum + Number(row["bytes"] ?? 0), 0);
  const seconds = result["seconds"] as Record<string, unknown> | undefined;
  const lines = [
    `${reused ? "reused the parts of" : "imported"} ${source} as reference "${name}": ${Object.keys(parts).length} part(s), `
      + `${Number(result["tris"]).toLocaleString("en-US")} triangles${seconds && !reused ? ` (import ${String(seconds["import"])} s, all ${String(seconds["total"])} s)` : ""}`,
    `files: ${rel(outDir)}/ (${mb(bytes)} of part STL), light overlay _overlay.stl (${Number(result["overlay_tris"]).toLocaleString("en-US")} triangles)`,
    `units: ${units}${unitGuess.assumed && !(typeof args["units"] === "string" && args["units"]) ? ` (assumed: ${path.extname(source).toUpperCase().slice(1)} carries no units; pass units if that is wrong)` : ""}; `
      + `axes: ${axes === "native" ? "the file's own coordinates" : "as File > Import (Y-up files stand up in Z)"}; transform ${JSON.stringify(transform)}`,
  ];
  if (typeof result["note"] === "string" && result["note"]) lines.push(`note: ${result["note"]}`);
  lines.push(`parts (file units, bounds min .. max${rows.some((row) => row["r"]) ? "; radial bands about the given axis" : ""}):`);
  for (const row of rows.slice(0, 40)) {
    const fmt = (v: unknown): string => (Array.isArray(v) ? `(${v.map((x) => Number(x).toFixed(3)).join(", ")})` : String(v));
    lines.push(`  ${String(row["part"])}: ${Number(row["tris"]).toLocaleString("en-US")} tris, ${fmt(row["min"])} .. ${fmt(row["max"])}`
      + (row["r"] ? `, r ${fmt(row["r"])}, depth ${fmt(row["depth"])}, ${String(row["angles_covered_deg"])}° covered` : ""));
  }
  if (rows.length > 40) lines.push(`  ... ${rows.length - 40} more`);
  lines.push(`registered in ${rel(refsFile)}. Use it by name: preview {"ref": "${name}"} or {"overlay": [{"ref": "${name}", "style": "solid"}]}; `
    + `measure {"ref": "${name}/<part>"}. To align it, call import_reference again with transform (the parts are reused) or edit references.json.`);
  return ok(lines.join("\n"), { details: { entry, rows } });
}

/** Frames of a reference video as one labelled sheet (needs ffmpeg). diff shows what moved. */
export async function videoReference(ctx: WorkspaceContext, args: Record<string, unknown>, extras: CallExtras = {}): Promise<ToolOutcome> {
  if (!ctx.blender || !fs.existsSync(ctx.blender)) return fail("No Blender executable is configured. Run VSBlender: Setup.");
  const ffmpeg = findFfmpeg(ctx.ffmpeg);
  if (!ffmpeg) return fail("reading video frames needs ffmpeg: install it, or set \"ffmpeg\" in .blender-ai/config.json to ffmpeg.exe");
  let video: string;
  try {
    video = fileArg(ctx, args["video"], "video");
  } catch (error) {
    return fail(errorText(error));
  }
  const inside = (root: string): boolean => {
    const relative = path.relative(root, video);
    return !relative.startsWith("..") && !path.isAbsolute(relative);
  };
  if (!inside(ctx.workspace) && !referenceRoots(ctx).some(inside)) {
    return fail(`${video} is outside the workspace. Add its folder to "referenceRoots" in .blender-ai/config.json.`);
  }
  if (!VIDEO.test(video)) return fail("video must be an mp4, mov, m4v, mkv, webm or avi file");
  const out = tempPng("video-frames");
  const spec: Record<string, unknown> = {
    kind: "video_frames", video, ffmpeg, out, work: path.join(jobs.jobsDir(ctx.workspace), `video-${Date.now()}`),
    max_bytes: PREVIEW_BYTES, max_tiles: Math.max(1, Math.min(36, Number(args["max_tiles"]) || 16)),
  };
  for (const key of ["times", "fps", "start", "end", "crop", "diff", "cell"]) {
    if (args[key] !== undefined && args[key] !== null) spec[key] = args[key];
  }
  let result: Record<string, unknown>;
  try {
    result = await jobs.runToEnd(ctx, { spec, kind: "video_frames", label: `frames of ${path.basename(video)}`, factoryStartup: true },
      300000, extras.signal);
  } catch (error) {
    return fail(errorText(error));
  } finally {
    fs.rmSync(String(spec["work"]), { recursive: true, force: true });
  }
  const times = (result["times"] as number[] | undefined) ?? [];
  const lines = [
    `${path.basename(video)}: ${times.length} frame(s) at ${times.map((t) => `${t}s`).join(", ")}${args["diff"] ? " (red: what changed since the frame before)" : ""}`,
    typeof result["note"] === "string" ? String(result["note"]) : "",
  ].filter(Boolean);
  const file = String(result["file"] ?? out);
  return imageReply(file, ctx.workspace, lines, args["save"] === true);
}

export function isVideoArg(args: Record<string, unknown>): boolean {
  return typeof args["video"] === "string" && Boolean(args["video"].trim());
}
