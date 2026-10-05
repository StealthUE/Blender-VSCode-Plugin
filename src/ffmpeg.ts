import * as fs from "fs";
import * as path from "path";

/**
 * ffmpeg encodes render videos (H.264, yuv420p, +faststart: plays everywhere) and reads frames from
 * reference videos. Blender ships its own FFmpeg libraries but no ffmpeg program, so it is found
 * here: "ffmpeg" in .blender-ai/config.json, VSBLENDER_FFMPEG, PATH, then the usual install folders.
 */
export function findFfmpeg(configured?: string, env: NodeJS.ProcessEnv = process.env): string | undefined {
  const exe = process.platform === "win32" ? "ffmpeg.exe" : "ffmpeg";
  const usable = (file: string | undefined): file is string => {
    if (!file) return false;
    try {
      return fs.statSync(file).isFile();
    } catch {
      return false;
    }
  };
  for (const candidate of [configured, env["VSBLENDER_FFMPEG"]]) {
    if (!candidate) continue;
    const file = fs.existsSync(candidate) && fs.statSync(candidate).isDirectory() ? path.join(candidate, exe) : candidate;
    if (usable(file)) return file;
    const inBin = path.join(candidate, "bin", exe);
    if (usable(inBin)) return inBin;
  }
  for (const dir of (env["PATH"] ?? env["Path"] ?? "").split(path.delimiter)) {
    const file = dir ? path.join(dir.replace(/^"|"$/g, ""), exe) : "";
    if (usable(file)) return file;
  }
  const roots: string[] = [];
  if (process.platform === "win32") {
    roots.push("C:\\", env["ProgramFiles"] ?? "C:\\Program Files", path.join(env["LOCALAPPDATA"] ?? "", "Programs"));
    const links = path.join(env["LOCALAPPDATA"] ?? "", "Microsoft", "WinGet", "Links", exe);
    if (usable(links)) return links;
  } else {
    for (const file of ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"]) {
      if (usable(file)) return file;
    }
  }
  for (const root of roots) {
    let entries: fs.Dirent[] = [];
    try {
      entries = fs.readdirSync(root, { withFileTypes: true });
    } catch {
      continue;
    }
    // C:\ffmpeg, C:\ffmpeg-8.0-full_build, C:\Program Files\ffmpeg: newest name first.
    const dirs = entries.filter((entry) => entry.isDirectory() && /^ffmpeg/i.test(entry.name)).map((entry) => entry.name).sort().reverse();
    for (const name of dirs) {
      for (const file of [path.join(root, name, "bin", exe), path.join(root, name, exe)]) {
        if (usable(file)) return file;
      }
    }
  }
  return undefined;
}
