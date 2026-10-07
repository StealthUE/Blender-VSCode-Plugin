/**
 * Machines that are not in the built-in list. A name is searched on the web, the build volume is
 * read from the page, and the profile is saved under .blender-ai/ so the next call does not search again.
 * A slicer 3MF's nozzle, layer height, material and bed override the page.
 */
import * as fs from "fs";
import * as path from "path";
import {
  canonicalPreset,
  describePrinter,
  MATERIAL_DENSITY,
  PrinterCatalog,
  PrinterProfile,
  printerParams,
  printerSlug,
  PRINTER_PRESETS,
  resolvePrinter,
} from "./printers";
import { readConfig, writeConfig } from "./projectConfig";
import { WorkspaceContext } from "./types";

export function catalogPath(workspace: string): string {
  return path.join(workspace, ".blender-ai", "printers.json");
}

interface CatalogEntry {
  profile: PrinterProfile;
  source: string;
  fetchedAt: string;
  keys: string[];
}

interface CatalogFile {
  printers: Record<string, CatalogEntry>;
}

export interface FetchLike {
  (input: string, init?: { signal?: AbortSignal; headers?: Record<string, string> }): Promise<{ ok: boolean; status?: number; text: () => Promise<string> }>;
}

export interface LookupResult {
  profile?: PrinterProfile;
  source?: string;
  warning?: string;
}

const CUES = /build(?:ing)? volume|print(?:ing)? volume|build size|printable (?:area|volume)|maximum print size|build area/i;

export function cleanQuery(name: string): string {
  const cut = (name.split("&")[0] ?? name).replace(/0\.\d+\s*mm(\s+nozzle)?/ig, " ");
  return cut.replace(/\s+/g, " ").trim();
}

/** The first "build volume 250 x 210 x 210 mm" (or cm) near those words. Nozzle-sized numbers are ignored. */
export function parseBuildVolume(text: string): [number, number, number] | undefined {
  const plain = text
    .replace(/<[^>]+>/g, " ")
    .replace(/&times;|&#215;|&#xd7;|×/gi, "x")
    .replace(/&nbsp;/gi, " ");
  const re = /(\d+(?:\.\d+)?)\s*(?:mm)?\s*[x*]\s*(\d+(?:\.\d+)?)\s*(?:mm)?\s*[x*]\s*(\d+(?:\.\d+)?)\s*(mm|cm)?/gi;
  let match: RegExpExecArray | null;
  while ((match = re.exec(plain))) {
    let a = Number(match[1]);
    let b = Number(match[2]);
    let c = Number(match[3]);
    const unit = (match[4] || "").toLowerCase();
    const around = plain.slice(Math.max(0, match.index - 100), match.index + match[0].length + 60);
    if (!CUES.test(around)) continue;
    const saysCm = unit === "cm" || (/\bcm\b/i.test(around) && !/\bmm\b/i.test(around));
    if (saysCm && a < 80 && b < 80 && c < 80) {
      a *= 10;
      b *= 10;
      c *= 10;
    }
    if ([a, b, c].every((n) => n >= 50 && n <= 1500)) return [a, b, c];
  }
  return undefined;
}

function parseNozzle(text: string): number | undefined {
  const match = /(\d(?:\.\d+)?)\s*mm\s+nozzle/i.exec(text.replace(/<[^>]+>/g, " "));
  if (!match?.[1]) return undefined;
  const n = Number(match[1]);
  return n >= 0.1 && n <= 1.2 ? n : undefined;
}

function displayName(query: string, text: string): string {
  const q = query.replace(/\s+/g, " ").trim();
  if (/prusa|bambu|creality/i.test(q)) return q;
  if (/prusa/i.test(text) && /\bmk\d/i.test(q)) return `Prusa ${q}`;
  if (/bambu/i.test(text)) return `Bambu Lab ${q}`;
  if (/creality|ender/i.test(text) && /ender/i.test(q)) return `Creality ${q}`;
  return q;
}

function finishProfile(name: string, volume: [number, number, number], opts: { nozzle?: number; layerHeight?: number; material?: string; density?: number } = {}): PrinterProfile {
  const nozzle = opts.nozzle && opts.nozzle > 0 ? opts.nozzle : 0.4;
  const material = (opts.material || "PLA").toUpperCase();
  const layer = opts.layerHeight && opts.layerHeight > 0 ? opts.layerHeight : 0.2;
  return {
    preset: printerSlug(name),
    name,
    buildVolume: volume,
    nozzle,
    layerHeight: layer,
    minWall: Math.max(0.8, Math.round(nozzle * 2 * 100) / 100),
    maxOverhangDeg: 45,
    material,
    density: MATERIAL_DENSITY[material] ?? opts.density ?? 1.24,
    filamentDiameter: 1.75,
    holeCompensation: Math.round((0.15 * nozzle) / 0.4 * 100) / 100,
  };
}

function readCatalogFile(workspace: string): CatalogFile {
  try {
    const raw = JSON.parse(fs.readFileSync(catalogPath(workspace), "utf8")) as CatalogFile;
    if (raw?.printers && typeof raw.printers === "object") return raw;
  } catch {
    // Missing or unreadable: nothing has been looked up yet.
  }
  return { printers: {} };
}

/** Preset-shaped entries for resolvePrinter. A catalog key never replaces a built-in preset. */
export function loadCatalog(workspace: string): PrinterCatalog {
  const out: PrinterCatalog = {};
  for (const entry of Object.values(readCatalogFile(workspace).printers)) {
    const profile = entry?.profile;
    if (!profile?.buildVolume || profile.buildVolume.length !== 3) continue;
    const { preset: _preset, density: _density, ...fields } = profile;
    const keys = new Set([profile.preset, ...(entry.keys ?? [])]);
    for (const key of keys) {
      if (key && !PRINTER_PRESETS[key]) out[key] = fields;
    }
  }
  return out;
}

export function rememberPrinter(workspace: string, profile: PrinterProfile, source: string, keys: string[]): void {
  const file = readCatalogFile(workspace);
  const aliases = [...new Set([profile.preset, ...keys.map((key) => printerSlug(key)).filter(Boolean)])];
  file.printers[profile.preset] = { profile, source, fetchedAt: new Date().toISOString(), keys: aliases };
  const dest = catalogPath(workspace);
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.writeFileSync(dest, JSON.stringify(file, null, 2) + "\n", "utf8");
}

function settingName(setting: unknown): string | undefined {
  if (typeof setting === "string" && setting.trim()) return setting.trim();
  if (setting && typeof setting === "object" && !Array.isArray(setting)) {
    const preset = (setting as { preset?: unknown }).preset;
    if (typeof preset === "string" && preset.trim()) return preset.trim();
  }
  return undefined;
}

function hasVolume(setting: unknown): boolean {
  if (!setting || typeof setting !== "object" || Array.isArray(setting)) return false;
  const volume = (setting as { buildVolume?: unknown }).buildVolume;
  return Array.isArray(volume) && volume.length === 3 && volume.every((n) => typeof n === "number" && n > 0);
}

/**
 * Write the profile into config when the workspace has no printer, or its printer is this same
 * unknown name. A preset or a profile that already has a build volume is left alone.
 */
export function installPrinter(workspace: string, profile: PrinterProfile, asked: string, also: string[] = []): boolean {
  const config = readConfig(workspace);
  if (!config) return false;
  if (config.printer === undefined) {
    writeConfig(workspace, { ...config, printer: profile });
    return true;
  }
  const name = settingName(config.printer);
  if (!name || canonicalPreset(name) || hasVolume(config.printer)) return false;
  const keys = new Set([printerSlug(profile.preset), printerSlug(profile.name), printerSlug(asked), ...also.map((key) => printerSlug(key))]);
  if (!keys.has(printerSlug(name))) return false;
  writeConfig(workspace, { ...config, printer: profile });
  return true;
}

async function getText(url: string, fetchImpl: FetchLike): Promise<string> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 6000);
  try {
    const res = await fetchImpl(url, {
      signal: ctrl.signal,
      headers: { "user-agent": "VSBlender printer lookup", accept: "application/json,text/html;q=0.9,*/*;q=0.8" },
    });
    if (!res.ok) throw new Error(String(res.status || "HTTP error"));
    return await res.text();
  } finally {
    clearTimeout(timer);
  }
}

function allowedHost(url: string): boolean {
  try {
    const host = new URL(url).hostname.toLowerCase();
    return host.endsWith("wikipedia.org") || host.endsWith("prusa3d.com") || host.endsWith("bambulab.com") || host.endsWith("creality.com");
  } catch {
    return false;
  }
}

function firstAllowedLink(html: string): string | undefined {
  const re = /href="([^"]+)"/gi;
  let match: RegExpExecArray | null;
  while ((match = re.exec(html))) {
    let href = match[1] ?? "";
    const uddg = /uddg=([^&"]+)/.exec(href);
    if (uddg?.[1]) {
      try {
        href = decodeURIComponent(uddg[1]);
      } catch {
        // Keep the raw href and test it below.
      }
    }
    if (href.startsWith("//")) href = `https:${href}`;
    if (allowedHost(href)) return href;
  }
  return undefined;
}

async function searchPages(query: string, fetchImpl: FetchLike): Promise<{ url: string; text: string }[]> {
  const pages: { url: string; text: string }[] = [];
  const searchUrl = `https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch=${encodeURIComponent(`${query} 3D printer`)}&srlimit=3&format=json&utf8=1`;
  try {
    const raw = JSON.parse(await getText(searchUrl, fetchImpl)) as { query?: { search?: { title?: string }[] } };
    const titles = (raw.query?.search ?? []).map((row) => row.title).filter((title): title is string => Boolean(title)).slice(0, 2);
    if (titles.length) {
      const extractUrl = "https://en.wikipedia.org/w/api.php?action=query&prop=extracts&explaintext=1&exchars=5000&redirects=1&format=json&titles="
        + titles.map((title) => encodeURIComponent(title)).join("|");
      const body = JSON.parse(await getText(extractUrl, fetchImpl)) as { query?: { pages?: Record<string, { title?: string; extract?: string }> } };
      for (const page of Object.values(body.query?.pages ?? {})) {
        if (page.extract) pages.push({ url: `https://en.wikipedia.org/wiki/${encodeURIComponent(page.title || titles[0] || query)}`, text: page.extract });
      }
    }
  } catch {
    // Wikipedia missed. The DuckDuckGo page is the other place the volume can be.
  }
  if (pages.some((page) => parseBuildVolume(page.text))) return pages;
  try {
    const html = await getText(`https://lite.duckduckgo.com/lite/?q=${encodeURIComponent(`${query} 3D printer build volume mm`)}`, fetchImpl);
    pages.push({ url: "https://lite.duckduckgo.com/lite/", text: html });
    const link = firstAllowedLink(html);
    if (link) pages.push({ url: link, text: await getText(link, fetchImpl) });
  } catch {
    // The caller says the volume was not found.
  }
  return pages;
}

/** A built-in preset, or a build volume read from a web page. Does not write config. */
export async function lookupPrinter(name: string, options: { fetch?: FetchLike } = {}): Promise<LookupResult> {
  const known = canonicalPreset(name);
  if (known && PRINTER_PRESETS[known]) {
    const base = PRINTER_PRESETS[known];
    return { profile: { ...base, preset: known, density: MATERIAL_DENSITY[base.material] ?? 1.24 }, source: "preset" };
  }
  const fetchImpl: FetchLike | undefined = options.fetch ?? (typeof globalThis.fetch === "function" ? globalThis.fetch.bind(globalThis) : undefined);
  if (!fetchImpl) return { warning: `cannot look up "${name}": this runtime has no HTTP fetch` };
  const query = cleanQuery(name);
  if (!query) return { warning: "printer name is empty" };
  try {
    for (const page of await searchPages(query, fetchImpl)) {
      const volume = parseBuildVolume(page.text);
      if (!volume) continue;
      return { profile: finishProfile(displayName(query, page.text), volume, { nozzle: parseNozzle(page.text) }), source: page.url };
    }
    return { warning: `looked up "${query}" and no page stated a build volume` };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return { warning: `looked up "${query}" and the search failed (${message})` };
  }
}

/** Look up each unknown name, cache it, and save it into config when the workspace printer is that name or unset. */
export async function ensurePrinters(workspace: string, settings: unknown[], fetch?: FetchLike): Promise<string[]> {
  const notes: string[] = [];
  let extras = loadCatalog(workspace);
  const seen = new Set<string>();
  for (const setting of settings) {
    const name = settingName(setting);
    if (!name || seen.has(name.toLowerCase())) continue;
    seen.add(name.toLowerCase());
    if (canonicalPreset(name, extras) || hasVolume(setting)) continue;
    const found = await lookupPrinter(name, fetch ? { fetch } : {});
    if (!found.profile || found.source === "preset") {
      if (found.warning) notes.push(found.warning);
      continue;
    }
    rememberPrinter(workspace, found.profile, found.source ?? "web", [name, found.profile.name]);
    extras = loadCatalog(workspace);
    const saved = installPrinter(workspace, found.profile, name, [name, found.profile.name]);
    notes.push(saved
      ? `saved printer profile ${describePrinter(found.profile)} from ${found.source}`
      : `looked up ${describePrinter(found.profile)} from ${found.source}`);
  }
  return notes;
}

export interface PreparedPrinter {
  params: Record<string, unknown>;
  profile: PrinterProfile;
  warnings: string[];
}

/** The profile a tool call should use, after an unknown name has been looked up. */
export async function printerForCall(ctx: WorkspaceContext, override?: unknown): Promise<PreparedPrinter> {
  const notes = await ensurePrinters(ctx.workspace, [ctx.printer, override]);
  const { profile, warnings } = resolvePrinter(ctx.printer, override, loadCatalog(ctx.workspace));
  const all = [...warnings, ...notes];
  return {
    profile,
    warnings: all,
    params: { ...printerParams(profile), configured: ctx.printer !== undefined || override !== undefined, warnings: all },
  };
}

export interface SlicerPrinter {
  model: string;
  name: string;
  nozzle?: number;
  layerHeight?: number;
  material?: string;
  density?: number;
  bed?: [number, number];
  buildVolume?: [number, number, number];
}

function positive(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : undefined;
}

function triple(value: unknown): [number, number, number] | undefined {
  if (!Array.isArray(value) || value.length !== 3 || !value.every((n) => typeof n === "number" && n > 0)) return undefined;
  return [value[0] as number, value[1] as number, value[2] as number];
}

/** The printer block read_3mf puts on a slicer project. */
export function normalizeSlicerPrinter(raw: unknown): SlicerPrinter | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const row = raw as Record<string, unknown>;
  const model = typeof row["model"] === "string" ? row["model"].trim() : "";
  const name = typeof row["name"] === "string" ? row["name"].trim() : "";
  const bedRaw = row["bed"];
  const bed = Array.isArray(bedRaw) && bedRaw.length >= 2 && typeof bedRaw[0] === "number" && typeof bedRaw[1] === "number" && bedRaw[0] > 0 && bedRaw[1] > 0
    ? [bedRaw[0], bedRaw[1]] as [number, number]
    : undefined;
  const buildVolume = triple(row["build_volume"]);
  if (!model && !name && !bed && !buildVolume) return undefined;
  const material = typeof row["material"] === "string" ? row["material"].trim().toUpperCase() : "";
  return {
    model, name, bed, buildVolume,
    nozzle: positive(row["nozzle"]),
    layerHeight: positive(row["layer_height"]),
    ...(material ? { material } : {}),
    density: positive(row["density"]),
  };
}

/**
 * Turn a slicer project's printer into a saved profile. The file's nozzle, layers, material and bed
 * win. The web is used only for a build volume the file does not state. An existing different printer
 * in config is not replaced.
 */
export async function adoptSlicerPrinter(workspace: string, raw: unknown, existing: unknown, fetch?: FetchLike): Promise<string> {
  const file = normalizeSlicerPrinter(raw);
  if (!file) return "";
  const label = file.name || file.model || "the slicer printer";
  let volume = file.buildVolume;
  let source = "the 3MF";
  let looked: LookupResult | undefined;
  let bedNote = "";
  if (!volume) {
    looked = await lookupPrinter(label, fetch ? { fetch } : {});
    if (looked.profile) {
      const web = looked.profile.buildVolume;
      source = looked.source ?? "the web";
      if (file.bed) {
        volume = [file.bed[0], file.bed[1], web[2]];
        if (Math.abs(web[0] - file.bed[0]) > 2 || Math.abs(web[1] - file.bed[1]) > 2) {
          bedNote = ` The page's bed is ${web[0]}x${web[1]} mm; the 3MF bed ${file.bed[0]}x${file.bed[1]} mm was kept.`;
        }
      } else volume = web;
    }
  }
  if (!volume) {
    const why = looked?.warning ? ` ${looked.warning}.` : "";
    return `The 3MF names ${label} and the build volume is still unknown.${why}`;
  }
  const profile = finishProfile(label, volume, {
    nozzle: file.nozzle, layerHeight: file.layerHeight, material: file.material, density: file.density,
  });
  rememberPrinter(workspace, profile, source, [label, file.model, file.name]);
  const saved = installPrinter(workspace, profile, label, [file.model, file.name, label]);
  const where = source === "the 3MF"
    ? "from the 3MF"
    : `build volume from ${source}; nozzle, layers and material from the 3MF`;
  const line = `${describePrinter(profile)} ${where}.${bedNote}`;
  if (saved) return `printer: saved ${line}`;
  if (existing !== undefined) return `printer: the 3MF was sliced for ${line} The workspace printer was left as it is.`;
  return `printer: ${line} No .blender-ai/config.json to save it into.`;
}
