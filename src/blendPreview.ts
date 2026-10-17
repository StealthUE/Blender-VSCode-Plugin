import { spawn } from "child_process";
import * as fs from "fs";
import * as path from "path";
import { previewsStatus, sidecarDir } from "./blendFiles";

/** Ingest stills, then the still this editor renders. The first fresh one is shown. */
export const PREVIEW_NAMES = ["camera.png", "iso.png", "front.png", "click.png"];

/**
 * A preview image of this version of the .blend. Ingest stills count when previews.json says they
 * were rendered from it (or, for older sidecars, when they are at least as new as the file); the
 * click still counts when it is at least as new. Opening the file shows this instead of rendering again.
 */
export function existingPreview(blendFile: string): string | undefined {
  let blendTime = 0;
  try {
    const stat = fs.statSync(blendFile);
    if (!stat.isFile() || stat.size <= 0) return undefined;
    blendTime = stat.mtimeMs;
  } catch {
    return undefined;
  }
  const dir = path.join(sidecarDir(blendFile), "previews");
  const recorded = previewsStatus(blendFile);
  for (const name of PREVIEW_NAMES) {
    const file = path.join(dir, name);
    try {
      const stat = fs.statSync(file);
      if (!stat.isFile() || stat.size <= 0) continue;
      const fresh = stat.mtimeMs + 1000 >= blendTime;
      // Without a previews.json each still is judged by its own time.
      if (name === "click.png" || recorded.fromTimes || recorded.status === "none") {
        if (fresh) return file;
      } else if (recorded.status === "current") {
        return file;
      }
    } catch {
      // The next name may exist.
    }
  }
  return undefined;
}

/** Where a click renders its still. */
export function clickPreviewPath(blendFile: string): string {
  return path.join(sidecarDir(blendFile), "previews", "click.png");
}

export function clickPreviewScript(extensionRoot: string): string {
  return path.join(extensionRoot, "resources", "click_preview.py");
}

/** Render one still in a background Blender. That process does not open the bridge. */
export function renderClickPreview(blender: string, blendFile: string, script: string, outFile: string): Promise<string> {
  fs.mkdirSync(path.dirname(outFile), { recursive: true });
  return new Promise((resolve, reject) => {
    const child = spawn(blender, ["-b", blendFile, "--python", script, "--", outFile], { windowsHide: true });
    let log = "";
    const take = (chunk: Buffer): void => {
      log += chunk.toString();
      if (log.length > 8000) log = log.slice(-8000);
    };
    child.stdout.on("data", take);
    child.stderr.on("data", take);
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error("the preview render took too long"));
    }, 120000);
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      if (code === 0 && fs.existsSync(outFile)) resolve(outFile);
      else reject(new Error(log.trim().split(/\r?\n/).slice(-8).join("\n") || `blender exited ${code}`));
    });
  });
}
