import * as fs from "fs";
import * as path from "path";
import { relativeTo, sidecarDir } from "./blendFiles";

export interface ContextPack {
  text: string;
  tokens: number;
  truncated: boolean;
  notesPath?: string;
}

interface Section {
  heading: string;
  body: string;
}

const PRIORITY = [
  "before you modify",
  "intent & constraints",
  "object tree",
  "materials",
  "animation",
  "previews",
  "text blocks inside the .blend",
  "files next to this one",
  "change log",
];

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

function focusLines(body: string, focus: string | undefined): string {
  if (!focus) return body;
  const needle = focus.toLowerCase();
  const lines = body.split(/\r?\n/);
  const kept = lines.filter((line) => line.toLowerCase().includes(needle) || line.startsWith("#"));
  if (!kept.length) return "";
  return kept.join("\n");
}

/**
 * Build a NOTES-style pack that fits a token budget.
 * "Before you modify" and "Intent & constraints" are kept even when that exceeds the budget,
 * because dropping those warnings is worse than being a bit over.
 */
export function packNotes(markdown: string, budgetTokens: number, focus?: string): { text: string; truncated: boolean } {
  const budget = Math.max(64, budgetTokens);
  const required = new Set(["before you modify", "intent & constraints"]);
  const lineFiltered = new Set(["object tree", "materials", "animation"]);
  const blocks: { text: string; force: boolean; rank: number }[] = [];

  for (const section of splitSections(markdown)) {
    const key = section.heading.toLowerCase();
    const force = key === "" || required.has(key);
    let body = section.body;
    if (focus && lineFiltered.has(key)) {
      const filtered = focusLines(section.body, focus);
      if (!filtered.trim()) continue;
      body = filtered;
    } else if (focus && !force && !`${section.heading}\n${body}`.toLowerCase().includes(focus.toLowerCase())) {
      continue;
    }
    const text = `${section.heading ? `## ${section.heading}\n` : ""}${body}`.trim();
    if (!text) continue;
    blocks.push({ text, force, rank: key ? priorityOf(section.heading) : -1 });
  }
  blocks.sort((a, b) => a.rank - b.rank);

  const parts: string[] = [];
  let used = 0;
  let truncated = false;
  for (const block of blocks) {
    const cost = estimateTokens(block.text);
    if (!block.force && used + cost > budget) {
      truncated = true;
      continue;
    }
    parts.push(block.text);
    used += cost;
    if (used > budget) truncated = true;
  }
  if (truncated) parts.push("… truncated to the token budget. Read NOTES.md and manifest.json for the rest.");
  return { text: parts.join("\n\n").trim(), truncated };
}

export function buildContextPack(
  workspace: string,
  blendFile: string,
  budgetTokens: number,
  focus?: string
): ContextPack {
  const notesPath = path.join(sidecarDir(blendFile), "NOTES.md");
  const rel = relativeTo(workspace, blendFile);
  const header = [
    `blend: ${rel}`,
    `sidecar: ${relativeTo(workspace, sidecarDir(blendFile))}/`,
    "Read NOTES.md before changing anything. Do not open the .blend binary.",
  ].join("\n");
  if (!fs.existsSync(notesPath)) {
    const text = `${header}\n\nNo ingest yet. Call ingest for this file, then context_pack again.`;
    return { text, tokens: estimateTokens(text), truncated: false };
  }
  const notes = fs.readFileSync(notesPath, "utf8");
  const packed = packNotes(notes, budgetTokens, focus);
  const text = `${header}\n\n${packed.text}`;
  return {
    text,
    tokens: estimateTokens(text),
    truncated: packed.truncated,
    notesPath,
  };
}
