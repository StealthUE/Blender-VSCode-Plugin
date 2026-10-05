import * as fs from "fs";
import * as path from "path";
import { relativeTo, sidecarDir } from "./blendFiles";

export interface ContextPack {
  text: string;
  tokens: number;
  /** True only when something was left out. */
  truncated: boolean;
  /** What was cut, e.g. "Materials: 12 of 22 shown". */
  dropped: string[];
  /** Tokens of the sections that are always kept (preamble, Before you modify, Intent & constraints). */
  required?: number;
  notesPath?: string;
}

interface Section {
  heading: string;
  body: string;
}

/** Previews come late: they are file links, and they used to crowd out the object tree under focus. */
const PRIORITY = [
  "before you modify",
  "intent & constraints",
  "object tree",
  "materials",
  "animation",
  "animation beats",
  "parameters",
  "built by scripts",
  "text blocks inside the .blend",
  "change log",
  "previews",
  "files next to this one",
];
const REQUIRED = new Set(["before you modify", "intent & constraints"]);

export function estimateTokens(text: string): number {
  return Math.max(1, Math.ceil(text.length / 4));
}

export function splitSections(markdown: string): Section[] {
  const lines = markdown.split(/\r?\n/);
  const sections: Section[] = [];
  let heading = "";
  let body: string[] = [];
  const push = (): void => {
    sections.push({ heading, body: body.join("\n").trim() });
  };
  for (const line of lines) {
    if (line.startsWith("## ")) {
      push();
      heading = line.slice(3).trim();
      body = [];
      continue;
    }
    body.push(line);
  }
  push();
  return sections.filter((section) => section.heading || section.body);
}

function priorityOf(heading: string): number {
  const index = PRIORITY.indexOf(heading.toLowerCase());
  return index === -1 ? PRIORITY.length : index;
}

/** The object, material and preview names a focus covers. Built from manifest.json. */
export interface FocusSet {
  roots: string[];
  objects: Set<string>;
  materials: Set<string>;
}

interface ManifestShape {
  objects?: Record<string, { children?: string[]; materials?: (string | null)[] }>;
  materials?: Record<string, unknown>;
}

export function focusSet(manifest: ManifestShape, focus: string): FocusSet | undefined {
  const objects = manifest.objects ?? {};
  const needle = focus.toLowerCase();
  const exact = Object.keys(objects).filter((name) => name.toLowerCase() === needle);
  const roots = exact.length ? exact : Object.keys(objects).filter((name) => name.toLowerCase().includes(needle));
  const materials = new Set(Object.keys(manifest.materials ?? {}).filter((name) => name.toLowerCase().includes(needle)));
  if (!roots.length && !materials.size) return undefined;
  const found = new Set<string>();
  const visit = (name: string): void => {
    if (found.has(name)) return;
    found.add(name);
    for (const child of objects[name]?.children ?? []) visit(child);
  };
  roots.forEach(visit);
  for (const name of found) {
    for (const material of objects[name]?.materials ?? []) if (material) materials.add(material);
  }
  return { roots, objects: found, materials };
}

/** Bold names on a NOTES line: "**SG Dais**", or a collapsed series "**SG Rock 00 ... SG Rock 15**". */
export function namesOn(line: string): string[] {
  const match = /\*\*(.+?)\*\*/.exec(line);
  if (!match?.[1]) return [];
  const text = match[1];
  return text.includes(" ... ") ? text.split(" ... ") : [text];
}

/** The series a name belongs to and its number: "SG Rock 07" -> ["SG Rock", 7] (the ingester's series_base). */
function seriesOf(name: string): [string, number] | undefined {
  const match = /^(.*?)[\s._-]*(\d+)$/.exec(name);
  return match ? [match[1] ?? "", Number(match[2])] : undefined;
}

/** True when name is on the line, also as a member of a collapsed "A ... B" series. */
export function lineCovers(line: string, has: (name: string) => boolean, pool: Iterable<string>): boolean {
  const names = namesOn(line).map((name) => name.replace(/^(material|OB|MA|WO):/, ""));
  if (names.some(has)) return true;
  if (names.length !== 2) return false;
  const lo = seriesOf(names[0] ?? "");
  const hi = seriesOf(names[1] ?? "");
  if (!lo || !hi || lo[0] !== hi[0]) return false;
  for (const name of pool) {
    const member = seriesOf(name);
    if (member && member[0] === lo[0] && member[1] >= lo[1] && member[1] <= hi[1] && has(name)) return true;
  }
  return false;
}

function keepForFocus(heading: string, line: string, set: FocusSet): boolean {
  const object = (name: string): boolean => set.objects.has(name) || set.objects.has(name.replace(/^OB:/, ""));
  const material = (name: string): boolean => set.materials.has(name) || set.materials.has(name.replace(/^(MA|material):/, ""));
  switch (heading) {
    case "object tree":
    case "parameters":
      return lineCovers(line, object, set.objects);
    case "materials":
      return lineCovers(line, material, set.materials);
    case "animation":
    case "animation beats":
      return lineCovers(line, object, set.objects) || lineCovers(line, material, set.materials);
    case "built by scripts":
      return [...set.objects].some((name) => line.includes(name)) || lineCovers(line.replace(/^-\s*`[^`]*`:\s*/, "- **") + "**", object, set.objects);
    case "previews":
      return set.roots.some((root) => line.includes(`'${root}'`) || line.includes(root.replace(/[^A-Za-z0-9._-]+/g, "_")));
    default:
      return true;
  }
}

/** Items of a markdown list: a "- " line with the more-indented lines under it. Other lines stand alone. */
function items(body: string): { lines: string[]; isItem: boolean }[] {
  const out: { lines: string[]; isItem: boolean; indent: number }[] = [];
  for (const line of body.split(/\r?\n/)) {
    const bullet = /^(\s*)- /.exec(line);
    const current = out[out.length - 1];
    if (bullet) {
      const indent = bullet[1]?.length ?? 0;
      if (current?.isItem && indent > current.indent) {
        current.lines.push(line);
        continue;
      }
      out.push({ lines: [line], isItem: true, indent });
      continue;
    }
    if (current?.isItem && /^\s+\S/.test(line)) {
      current.lines.push(line);
      continue;
    }
    out.push({ lines: [line], isItem: false, indent: 0 });
  }
  return out;
}

function focusFilter(heading: string, body: string, focus: string, set: FocusSet | undefined): string {
  const key = heading.toLowerCase();
  if (set) {
    // Line by line: every object in the subtree is named on its own line, so nesting does not matter.
    return body.split(/\r?\n/).filter((line) => !/^\s*- /.test(line) || keepForFocus(key, line, set)).join("\n");
  }
  const needle = focus.toLowerCase();
  const kept = body.split(/\r?\n/).filter((line) => line.toLowerCase().includes(needle) || line.startsWith("#"));
  return kept.join("\n");
}

/**
 * Build a NOTES-style pack that fits a token budget.
 * "Before you modify" and "Intent & constraints" are kept even when that exceeds the budget,
 * because dropping those warnings is worse than being a bit over. A list section that does not fit
 * is cut item by item (the change log keeps its newest lines), and every cut is reported.
 */
export function packNotes(markdown: string, budgetTokens: number, focus?: string, set?: FocusSet):
  { text: string; truncated: boolean; dropped: string[]; required: number } {
  const budget = Math.max(64, budgetTokens);
  const trailer = "Cut to the token budget. Read NOTES.md and manifest.json for the rest.";
  const blocks: { heading: string; body: string; force: boolean; rank: number }[] = [];
  const dropped: string[] = [];

  for (const section of splitSections(markdown)) {
    const key = section.heading.toLowerCase();
    const force = key === "" || REQUIRED.has(key);
    let body = section.body;
    if (focus && !force) {
      if (["object tree", "materials", "animation", "animation beats", "previews", "parameters", "built by scripts"].includes(key)) {
        body = focusFilter(section.heading, section.body, focus, set);
        if (!body.split(/\r?\n/).some((line) => /^\s*- /.test(line))) continue;
      } else if (!`${section.heading}\n${body}`.toLowerCase().includes(focus.toLowerCase())) {
        continue;
      }
    }
    if (!body.trim() && !section.heading) continue;
    blocks.push({ heading: section.heading, body, force, rank: key ? priorityOf(section.heading) : -1 });
  }
  blocks.sort((a, b) => a.rank - b.rank);

  const parts: string[] = [];
  let used = 0;
  let required = 0;
  let reservedTrailer = false;
  for (const block of blocks) {
    const head = block.heading ? `## ${block.heading}\n` : "";
    const full = `${head}${block.body}`.trim();
    // Sections are joined by a blank line: one more token each.
    const cost = estimateTokens(full) + 1;
    if (block.force) required += cost;
    if (block.force || used + cost <= budget) {
      parts.push(full);
      used += cost;
      continue;
    }
    if (!reservedTrailer) {
      used += estimateTokens(trailer) + 1;
      reservedTrailer = true;
    }
    // Cut the list item by item. The change log reads newest last, so it keeps its tail.
    const all = items(block.body);
    const listItems = all.filter((item) => item.isItem);
    const newestFirst = block.heading.toLowerCase() === "change log";
    const order = newestFirst ? [...all].reverse() : all;
    const keptItems = new Set<typeof all[number]>();
    // The "- ... N of M shown" line that replaces the cut items.
    let sectionCost = estimateTokens(head) + estimateTokens("- ... 999 of 999 shown (newest); the rest is in NOTES.md") + 1;
    for (const item of order) {
      const itemCost = estimateTokens(item.lines.join("\n")) + 1;
      if (!item.isItem) {
        if (used + sectionCost + itemCost <= budget) {
          keptItems.add(item);
          sectionCost += itemCost;
        }
        continue;
      }
      if (used + sectionCost + itemCost > budget) break;
      keptItems.add(item);
      sectionCost += itemCost;
    }
    const shownItems = listItems.filter((item) => keptItems.has(item)).length;
    const name = block.heading || "header";
    if (!shownItems) {
      dropped.push(`${name}: left out (${listItems.length} items)`);
      continue;
    }
    const lines = all.filter((item) => keptItems.has(item)).flatMap((item) => item.lines);
    const note = `- ... ${shownItems} of ${listItems.length} shown${newestFirst ? " (newest)" : ""}; the rest is in NOTES.md`;
    parts.push(`${head}${lines.join("\n")}\n${note}`.trim());
    used += sectionCost;
    dropped.push(`${name}: ${shownItems} of ${listItems.length} shown`);
  }
  if (dropped.length) parts.push(trailer);
  return { text: parts.join("\n\n").trim(), truncated: dropped.length > 0, dropped, required };
}

export function buildContextPack(
  workspace: string,
  blendFile: string,
  budgetTokens: number,
  focus?: string
): ContextPack {
  const folder = sidecarDir(blendFile);
  const notesPath = path.join(folder, "NOTES.md");
  const rel = relativeTo(workspace, blendFile);
  const header = [
    `blend: ${rel}`,
    `sidecar: ${relativeTo(workspace, folder)}/`,
    "Read NOTES.md before changing anything. Do not open the .blend binary.",
  ];
  if (!fs.existsSync(notesPath)) {
    const text = `${header.join("\n")}\n\nNo ingest yet. Call ingest for this file, then context_pack again.`;
    return { text, tokens: estimateTokens(text), truncated: false, dropped: [] };
  }
  try {
    const state = JSON.parse(fs.readFileSync(path.join(folder, "state.json"), "utf8")) as { source?: string; dirty?: boolean };
    if (state.source === "live") {
      header.push(`Source: the live Blender session${state.dirty ? ", with changes not saved to the .blend" : ""}.`);
    }
  } catch {
    // No state: the notes are still worth reading.
  }
  let set: FocusSet | undefined;
  if (focus) {
    try {
      set = focusSet(JSON.parse(fs.readFileSync(path.join(folder, "manifest.json"), "utf8")) as ManifestShape, focus);
    } catch {
      set = undefined;
    }
    if (set) {
      header.push(`focus: ${set.roots.join(", ") || focus}, with ${set.objects.size} object(s) in its subtree and ${set.materials.size} material(s).`);
    }
  }
  const notes = fs.readFileSync(notesPath, "utf8");
  // The header is part of the pack: the notes get what is left of the budget.
  const headerCost = estimateTokens(header.join("\n")) + 1;
  const packed = packNotes(notes, Math.max(64, budgetTokens - headerCost), focus, set);
  const text = `${header.join("\n")}\n\n${packed.text}`;
  return {
    text,
    tokens: estimateTokens(text),
    truncated: packed.truncated,
    dropped: packed.dropped,
    required: packed.required + headerCost,
    notesPath,
  };
}
