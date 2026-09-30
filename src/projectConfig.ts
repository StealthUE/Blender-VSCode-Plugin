import * as fs from "fs";
import * as path from "path";
import { clampPort, normalizeClients, ProjectConfig } from "./types";

export function configPath(workspace: string): string {
  return path.join(workspace, ".blender-ai", "config.json");
}

export function readConfig(workspace: string): ProjectConfig | undefined {
  const file = configPath(workspace);
  if (!fs.existsSync(file)) return undefined;
  try {
    const raw = JSON.parse(fs.readFileSync(file, "utf8")) as Partial<ProjectConfig>;
    if (raw.version !== 1) return undefined;
    const blender = typeof raw.blender === "string" && raw.blender.trim() ? raw.blender.trim() : undefined;
    return {
      version: 1,
      port: clampPort(raw.port),
      ...(blender ? { blender } : {}),
      clients: normalizeClients(raw.clients),
      replaceLegacy: raw.replaceLegacy !== false,
    };
  } catch {
    return undefined;
  }
}

export function writeConfig(workspace: string, config: ProjectConfig): string {
  const file = configPath(workspace);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const body = JSON.stringify(config, null, 2) + "\n";
  const existing = fs.existsSync(file) ? fs.readFileSync(file, "utf8") : undefined;
  if (existing !== body) fs.writeFileSync(file, body, "utf8");
  return file;
}
