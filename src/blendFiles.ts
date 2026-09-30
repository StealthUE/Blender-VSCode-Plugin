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

export type IngestStatus = "new" | "current" | "stale";

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
    const state = JSON.parse(fs.readFileSync(statePath, "utf8")) as { sha256?: string };
    const sha = fileSha256(blendFile);
    if (sha && state.sha256 === sha) return "current";
    return "stale";
  } catch {
    return "stale";
  }
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
