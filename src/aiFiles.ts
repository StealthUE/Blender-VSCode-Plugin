import * as fs from "fs";
import * as path from "path";
import { GUIDE_STAMP, ClientFlags } from "./types";

export interface GuideTarget {
  rel: string;
  client: keyof ClientFlags | "grok-fallback";
}

/**
 * Grok reads CLAUDE.md as well as AGENTS.md. Writing both injects the guide twice,
 * so AGENTS.md is only used when Grok is on and Claude Code is off.
 */
export function guideTargets(clients: ClientFlags): GuideTarget[] {
  const targets: GuideTarget[] = [];
  if (clients.claude) targets.push({ rel: "CLAUDE.md", client: "claude" });
  else if (clients.grok) targets.push({ rel: "AGENTS.md", client: "grok-fallback" });
  if (clients.vscode) targets.push({ rel: path.join(".github", "copilot-instructions.md"), client: "vscode" });
  if (clients.cursor) targets.push({ rel: ".cursorrules", client: "cursor" });
  if (clients.cline) targets.push({ rel: ".clinerules", client: "cline" });
  return targets;
}

export const ALL_GUIDE_RELS = [
  "CLAUDE.md",
  "AGENTS.md",
  path.join(".github", "copilot-instructions.md"),
  ".cursorrules",
  ".clinerules",
];

export function renderGuide(template: string, list: string, port: number): string {
  // {{BLEND_TABLE}} is the placeholder of the first guide template.
  const body = template.replace("{{BLEND_LIST}}", list).replace("{{BLEND_TABLE}}", list).replace(/\{\{PORT\}\}/g, String(port));
  return `${GUIDE_STAMP}-v7 -->\n${body}`;
}

/**
 * The .blend files, without an ingest status. A status written into the guide is wrong as soon as
 * a file is saved or edited in Blender, so the guide sends the reader to doctor for it.
 */
export function blendList(rels: string[]): string {
  if (!rels.length) return "*(no .blend files in this workspace yet)*";
  return rels.map((rel) => `- \`${rel}\``).join("\n");
}

/** First-version name, kept for callers that still pass rows with a status. */
export function blendTable(rows: { rel: string; status?: string }[]): string {
  return blendList(rows.map((row) => row.rel));
}

export interface GuideWriteResult {
  written: string[];
  skipped: string[];
  removed: string[];
}

export function writeGuides(workspace: string, clients: ClientFlags, content: string): GuideWriteResult {
  const wanted = new Set(guideTargets(clients).map((target) => target.rel));
  const written: string[] = [];
  const skipped: string[] = [];
  const removed: string[] = [];
  for (const rel of ALL_GUIDE_RELS) {
    const full = path.join(workspace, rel);
    const exists = fs.existsSync(full);
    const current = exists ? fs.readFileSync(full, "utf8") : undefined;
    const ours = current !== undefined && current.trimStart().startsWith(GUIDE_STAMP);
    if (!wanted.has(rel)) {
      if (ours) {
        fs.rmSync(full);
        removed.push(full);
      }
      continue;
    }
    if (current !== undefined && !ours) {
      skipped.push(full);
      continue;
    }
    if (current === content) continue;
    fs.mkdirSync(path.dirname(full), { recursive: true });
    fs.writeFileSync(full, content, "utf8");
    written.push(full);
  }
  return { written, skipped, removed };
}
