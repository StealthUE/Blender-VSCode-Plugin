import * as fs from "fs";
import * as path from "path";
import { normalizePrinterSetting } from "./printers";
import { clampPort, normalizeCheckpoints, normalizeClients, ProjectConfig } from "./types";

export function configPath(workspace: string): string {
  return path.join(workspace, ".blender-ai", "config.json");
}

function stringList(value: unknown): string[] | undefined {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : undefined;
}

export function readConfig(workspace: string): ProjectConfig | undefined {
  const file = configPath(workspace);
  if (!fs.existsSync(file)) return undefined;
  try {
    const raw = JSON.parse(fs.readFileSync(file, "utf8")) as Partial<ProjectConfig>;
    if (raw.version !== 1) return undefined;
    const blender = typeof raw.blender === "string" && raw.blender.trim() ? raw.blender.trim() : undefined;
    const libPaths = stringList(raw.libPaths);
    const trustedScripts = stringList(raw.trustedScripts);
    const referenceRoots = stringList(raw.referenceRoots);
    const sharedLibs = stringList(raw.sharedLibs);
    const ffmpeg = typeof raw.ffmpeg === "string" && raw.ffmpeg.trim() ? raw.ffmpeg.trim() : undefined;
    const printer = normalizePrinterSetting(raw.printer);
    return {
      version: 1,
      port: clampPort(raw.port),
      ...(blender ? { blender } : {}),
      clients: normalizeClients(raw.clients),
      replaceLegacy: raw.replaceLegacy !== false,
      ...(typeof raw.allowScripts === "boolean" ? { allowScripts: raw.allowScripts } : {}),
      ...(typeof raw.ignoreClientConfig === "boolean" ? { ignoreClientConfig: raw.ignoreClientConfig } : {}),
      ...(raw.checkpoints !== undefined ? { checkpoints: normalizeCheckpoints(raw.checkpoints) } : {}),
      ...(libPaths ? { libPaths } : {}),
      ...(printer !== undefined ? { printer } : {}),
      ...(trustedScripts ? { trustedScripts } : {}),
      ...(typeof raw.allowTrustedScripts === "boolean" ? { allowTrustedScripts: raw.allowTrustedScripts } : {}),
      ...(typeof raw.allowSave === "boolean" ? { allowSave: raw.allowSave } : {}),
      ...(referenceRoots ? { referenceRoots } : {}),
      ...(sharedLibs ? { sharedLibs } : {}),
      ...(ffmpeg ? { ffmpeg } : {}),
    };
  } catch {
    return undefined;
  }
}

/**
 * Write the config, keeping keys this writer does not set. Setup and the activation refresh only
 * know the keys they manage; a printer profile or trusted folders added by hand (or by a newer
 * extension) must survive them.
 */
export function writeConfig(workspace: string, config: ProjectConfig): string {
  const file = configPath(workspace);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const existing = fs.existsSync(file) ? fs.readFileSync(file, "utf8") : undefined;
  let kept: Record<string, unknown> = {};
  if (existing) {
    try {
      const parsed = JSON.parse(existing) as unknown;
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) kept = parsed as Record<string, unknown>;
    } catch {
      kept = {};
    }
  }
  const body = JSON.stringify({ ...kept, ...config }, null, 2) + "\n";
  if (existing !== body) fs.writeFileSync(file, body, "utf8");
  return file;
}
