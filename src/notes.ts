import * as fs from "fs";
import * as path from "path";
import { relativeTo, sidecarDir } from "./blendFiles";

const HEADING = "## Intent & constraints";
const PLACEHOLDER = /<!--\s*Hand-written by people[\s\S]*?-->\s*/;

/**
 * Write the hand-written "Intent & constraints" section of NOTES.md, which re-ingests keep: where sizes
 * live, conventions (angles, axes), what to re-run after what, what is keyed. append adds to it,
 * replace rewrites it. Returns the new section body.
 */
export function writeIntent(notesPath: string, text: string, mode: "append" | "replace"): string {
  const notes = fs.readFileSync(notesPath, "utf8");
  const body = text.trim();
  const start = notes.indexOf(HEADING);
  if (start < 0) {
    // Older notes without the section: it goes before the change log, which must stay last.
    const log = notes.indexOf("## Change log");
    const section = `${HEADING}\n\n${body}\n\n`;
    const next = log >= 0 ? notes.slice(0, log) + section + notes.slice(log) : `${notes.trimEnd()}\n\n${section}`;
    fs.writeFileSync(notesPath, next, "utf8");
    return body;
  }
  const after = start + HEADING.length;
  const nextHeading = notes.indexOf("\n## ", after);
  const end = nextHeading < 0 ? notes.length : nextHeading + 1;
  const current = notes.slice(after, end).replace(PLACEHOLDER, "").trim();
  const merged = mode === "replace" || !current ? body : `${current}\n\n${body}`;
  const next = `${notes.slice(0, after)}\n\n${merged}\n\n${notes.slice(end).replace(/^\n+/, "")}`;
  fs.writeFileSync(notesPath, next, "utf8");
  return merged;
}

export function notesPathFor(blend: string): string {
  return path.join(sidecarDir(blend), "NOTES.md");
}

export function journalIntent(blend: string, workspace: string, mode: string, text: string): void {
  try {
    fs.appendFileSync(path.join(sidecarDir(blend), "journal.jsonl"), JSON.stringify({
      time: new Date().toISOString(), event: "intent", actor: "ai", mode, blend: relativeTo(workspace, blend),
      text: text.slice(0, 2000),
    }) + "\n", "utf8");
  } catch {
    // The notes were written; the journal is a convenience.
  }
}
