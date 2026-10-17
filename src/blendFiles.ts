import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";

const SKIP_DIRS = new Set([
  "node_modules",
  ".git",
  "out",
  "OldVersions",
  "dist",
  ".blender-ai",
]);

/**
 * From the files alone: new (no sidecar), current (it matches the .blend), stale (the .blend changed
 * since), live (it was built from a Blender session with unsaved changes, so it does not match the
 * file on disk). diverged needs the running Blender and comes from session_info.
 */
export type IngestStatus = "new" | "current" | "stale" | "live" | "diverged";

export function sidecarDir(blendFile: string): string {
  const dir = path.dirname(blendFile);
  const name = path.basename(blendFile, path.extname(blendFile));
  return path.join(dir, ".blender-ai", name);
}

export function listBlendFiles(root: string, maxDepth = 6): string[] {
  const found: string[] = [];
  const walk = (dir: string, depth: number): void => {
    if (depth > maxDepth) return;
    let entries: fs.Dirent[];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      if (entry.name.startsWith(".")) continue;
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (SKIP_DIRS.has(entry.name)) continue;
        walk(full, depth + 1);
        continue;
      }
      if (entry.isFile() && entry.name.toLowerCase().endsWith(".blend")) found.push(full);
    }
  };
  walk(root, 0);
  found.sort((a, b) => a.localeCompare(b));
  return found;
}

/**
 * Whether opening a folder should show the setup panel: only a folder that has a .blend and is not
 * set up yet, with the setting on, once per window. VS Code can activate the extension in folders
 * without one (a workspaceContains search that times out on a big drive activates it anyway).
 */
export function shouldOfferSetup(state: { configured: boolean; hasBlend: boolean; setting: boolean; offered?: boolean }): boolean {
  return !state.configured && state.hasBlend && state.setting && !state.offered;
}

export function fileSha256(file: string): string | undefined {
  try {
    const hash = crypto.createHash("sha256");
    hash.update(fs.readFileSync(file));
    return hash.digest("hex");
  } catch {
    return undefined;
  }
}

export function ingestStatus(blendFile: string): IngestStatus {
  const statePath = path.join(sidecarDir(blendFile), "state.json");
  if (!fs.existsSync(statePath)) return "new";
  try {
    const state = JSON.parse(fs.readFileSync(statePath, "utf8")) as { sha256?: string; source?: string; dirty?: boolean };
    const sha = fileSha256(blendFile);
    if (!sha || state.sha256 !== sha) return "stale";
    return state.source === "live" && state.dirty ? "live" : "current";
  } catch {
    return "stale";
  }
}

/**
 * Whether the sidecar's preview images show this version of the file. current: rendered from it.
 * older: rendered from an earlier version (an ingest that skipped previews kept them). unrecorded:
 * images from before 0.8.0, with no record of what they show. none: no images.
 */
export type PreviewsStatus = "current" | "older" | "unrecorded" | "none";

export function previewsStatus(blendFile: string, sha?: string): { status: PreviewsStatus; renderedAt?: string } {
  const dir = path.join(sidecarDir(blendFile), "previews");
  let index: { sha256?: string; rendered_at?: string; source?: string } | undefined;
  try {
    index = JSON.parse(fs.readFileSync(path.join(dir, "previews.json"), "utf8"));
  } catch {
    index = undefined;
  }
  if (!index) {
    let images = false;
    try {
      images = fs.readdirSync(dir).some((name) => name.toLowerCase().endsWith(".png") && name !== "click.png");
    } catch {
      images = false;
    }
    return { status: images ? "unrecorded" : "none" };
  }
  const current = sha ?? fileSha256(blendFile);
  const renderedAt = typeof index.rendered_at === "string" ? index.rendered_at : undefined;
  // A live ingest renders the session, which can be ahead of the file: compare only disk renders.
  const same = index.source === "live" ? true : Boolean(current) && index.sha256 === current;
  return { status: same ? "current" : "older", ...(renderedAt ? { renderedAt } : {}) };
}

export interface CheckpointEntry {
  id: string;
  file: string;
  label?: string;
  auto?: boolean;
  time?: string;
  scene?: string;
  script?: string;
  reason?: string;
}

/** Checkpoints written by the add-on, oldest first. `file` is relative to the workspace. */
export function readCheckpoints(blendFile: string): CheckpointEntry[] {
  try {
    const data: unknown = JSON.parse(fs.readFileSync(path.join(sidecarDir(blendFile), "checkpoints", "index.json"), "utf8"));
    return Array.isArray(data) ? (data as CheckpointEntry[]).filter((e) => e && typeof e.id === "string") : [];
  } catch {
    return [];
  }
}

export function findCheckpoint(blendFile: string, id: string): CheckpointEntry | undefined {
  const entries = readCheckpoints(blendFile);
  if ((id === "last" || id === "latest") && entries.length) return entries[entries.length - 1];
  const exact = entries.find((entry) => entry.id === id);
  if (exact) return exact;
  const prefixed = entries.filter((entry) => entry.id.startsWith(id));
  return prefixed.length === 1 ? prefixed[0] : undefined;
}

/** Resolve a user path and reject anything outside the workspace. */
export function resolveInside(workspace: string, file: string): string {
  const abs = path.resolve(workspace, file);
  const rel = path.relative(workspace, abs);
  if (rel.startsWith("..") || path.isAbsolute(rel)) {
    throw new Error(`path is outside the workspace: ${file}`);
  }
  return abs;
}

export function relativeTo(workspace: string, file: string): string {
  return path.relative(workspace, file).split(path.sep).join("/");
}
