import * as fs from "fs";
import * as path from "path";

/**
 * Claude Code, Grok and the other clients start the MCP server from the path written into their
 * config, even when VS Code is closed. The installed extension folder has the version in its name
 * and is deleted on update, so the server runs from a copy in global storage whose path is stable.
 * Returns the directory to use as the extension root for the server and Blender scripts.
 */
export function syncRuntime(extensionRoot: string, storageDir: string, version: string): string {
  const target = path.join(storageDir, "runtime");
  const stamp = path.join(target, "version.txt");
  const server = path.join(extensionRoot, "out", "mcp.js");
  const guide = path.join(extensionRoot, "resources", "blender-guide.md");
  // The build time is in the stamp too, so a recompiled server or an edited guide is copied again.
  const guideMtime = fs.existsSync(guide) ? Math.round(fs.statSync(guide).mtimeMs) : 0;
  const wanted = `${version} ${Math.round(fs.statSync(server).mtimeMs)} ${guideMtime}`;
  const current = fs.existsSync(stamp) ? fs.readFileSync(stamp, "utf8").trim() : "";
  if (current === wanted) return target;
  for (const dir of ["out", "resources"]) {
    const dest = path.join(target, dir);
    // Node has already read the files of a running server, so replacing them is safe on Windows.
    fs.rmSync(dest, { recursive: true, force: true });
    fs.cpSync(path.join(extensionRoot, dir), dest, {
      recursive: true,
      filter: (source) => !/[\\/]__pycache__([\\/]|$)|\.map$/.test(source),
    });
  }
  fs.writeFileSync(stamp, `${wanted}\n`, "utf8");
  return target;
}
