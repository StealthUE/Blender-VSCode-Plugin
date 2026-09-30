import * as fs from "fs";
import * as path from "path";
import { ServerLaunch, SERVER_NAME } from "./types";

export interface LaunchInput {
  nodePath?: string;
  electronPath?: string;
  extensionRoot: string;
  workspace: string;
  port: number;
  blender?: string;
}

export function serverLaunch(input: LaunchInput): ServerLaunch {
  const script = path.join(input.extensionRoot, "out", "mcp.js");
  const env: Record<string, string> = {
    VSBLENDER_WORKSPACE: input.workspace,
    VSBLENDER_EXTENSION_ROOT: input.extensionRoot,
    VSBLENDER_PORT: String(input.port),
  };
  if (input.blender) env["VSBLENDER_BLENDER"] = input.blender;
  if (input.nodePath) {
    return { command: input.nodePath, args: [script], env, via: "node" };
  }
  if (!input.electronPath) {
    throw new Error("Node was not found, and there is no VS Code executable to fall back on.");
  }
  env["ELECTRON_RUN_AS_NODE"] = "1";
  return { command: input.electronPath, args: [script], env, via: "electron" };
}

interface JsonRecord {
  [key: string]: unknown;
}

function isRecord(value: unknown): value is JsonRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export function isLegacyBlenderEntry(entry: unknown): boolean {
  if (!isRecord(entry)) return false;
  const args = Array.isArray(entry["args"]) ? entry["args"].map((part) => String(part)).join(" ") : "";
  const blob = `${String(entry["command"] ?? "")} ${args}`.toLowerCase();
  return blob.includes("mcp-for-blender");
}

export type JsonStyle = "servers" | "mcpServers";

/**
 * Insert the vsblender server. When replaceLegacy is set, drop entries that launch
 * mcp-for-blender and leave every other server untouched. Returns undefined when the
 * file exists but is not JSON we can safely edit.
 */
export function mergeMcpJson(
  existing: string | undefined,
  style: JsonStyle,
  entry: JsonRecord,
  replaceLegacy: boolean
): string | undefined {
  let doc: JsonRecord = {};
  if (existing !== undefined) {
    try {
      const parsed: unknown = JSON.parse(existing);
      if (!isRecord(parsed)) return undefined;
      doc = parsed;
    } catch {
      return undefined;
    }
  }
  const current = isRecord(doc[style]) ? { ...doc[style] } : {};
  if (replaceLegacy) {
    for (const key of Object.keys(current)) {
      if (isLegacyBlenderEntry(current[key])) delete current[key];
    }
  }
  current[SERVER_NAME] = entry;
  doc[style] = current;
  return JSON.stringify(doc, null, 2) + "\n";
}

export function removeMcpServer(existing: string, style: JsonStyle): string | undefined {
  let doc: JsonRecord;
  try {
    const parsed: unknown = JSON.parse(existing);
    if (!isRecord(parsed)) return undefined;
    doc = parsed;
  } catch {
    return undefined;
  }
  if (!isRecord(doc[style])) return existing;
  const servers = { ...doc[style] };
  if (!(SERVER_NAME in servers)) return existing;
  delete servers[SERVER_NAME];
  doc[style] = servers;
  return JSON.stringify(doc, null, 2) + "\n";
}

export function vscodeServerEntry(launch: ServerLaunch): JsonRecord {
  return { type: "stdio", command: launch.command, args: launch.args, env: launch.env };
}

export function claudeServerEntry(launch: ServerLaunch): JsonRecord {
  return { command: launch.command, args: launch.args, env: launch.env };
}

const TOOL_TIMEOUT_SEC = 600;

function tomlString(value: string): string {
  return `"${value.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

export function renderGrokToml(launch: ServerLaunch): string {
  const lines = [
    "[mcp_servers.vsblender]",
    `command = ${tomlString(launch.command)}`,
    "args = [" + launch.args.map(tomlString).join(", ") + "]",
    "enabled = true",
    // Grok's default tool timeout is 60s. Ingest with previews, renders and long scripts take longer.
    `tool_timeout_sec = ${TOOL_TIMEOUT_SEC}`,
    "",
    "[mcp_servers.vsblender.env]",
  ];
  for (const key of Object.keys(launch.env).sort()) {
    lines.push(`${key} = ${tomlString(launch.env[key] ?? "")}`);
  }
  lines.push("");
  return lines.join("\n");
}

function isOurTomlHeader(line: string): boolean {
  return /^\[mcp_servers\.vsblender(\.[^\]]+)?\]\s*(#.*)?$/.test(line.trim());
}

function isOtherTomlHeader(line: string): boolean {
  const trimmed = line.trim();
  return trimmed.startsWith("[") && !isOurTomlHeader(trimmed);
}

/** Replace our tables and keep the rest of the file, including comments. */
export function mergeGrokToml(existing: string | undefined, block: string): string {
  const source = existing ?? "";
  const kept: string[] = [];
  const lines = source.split(/\r?\n/);
  let skipping = false;
  for (const line of lines) {
    if (isOurTomlHeader(line)) {
      skipping = true;
      continue;
    }
    if (skipping && isOtherTomlHeader(line)) skipping = false;
    if (!skipping) kept.push(line);
  }
  let body = kept.join("\n").replace(/\s+$/, "");
  if (body) body += "\n\n";
  return body + block.replace(/\s+$/, "") + "\n";
}

export function removeGrokServer(existing: string): string {
  return mergeGrokToml(existing, "").replace(/\n{3,}/g, "\n\n").replace(/^\n+/, "");
}

const GITIGNORE_BLOCK = [
  "# vsblender-begin",
  ".blender-ai/config.json",
  ".blender-ai/launch.log",
  ".blender-ai/live/",
  ".blender-ai/**/state.json",
  ".blender-ai/**/previews/",
  ".blender-ai/**/checkpoints/",
  ".blender-ai/**/texts/",
  "# vsblender-end",
].join("\n");

export function mergeGitignore(existing: string | undefined): string {
  const source = existing ?? "";
  const without = source.replace(/# vsblender-begin[\s\S]*?# vsblender-end\r?\n?/g, "").replace(/\s+$/, "");
  return (without ? `${without}\n\n` : "") + GITIGNORE_BLOCK + "\n";
}

const BLEND_DENY = [
  "Read(**/*.blend)",
  "Read(**/*.blend1)",
  "Edit(**/*.blend)",
  "Edit(**/*.blend1)",
];

export function mergeClaudeDeny(existing: string | undefined): string | undefined {
  let settings: JsonRecord = {};
  if (existing !== undefined) {
    try {
      const parsed: unknown = JSON.parse(existing);
      if (!isRecord(parsed)) return undefined;
      settings = parsed;
    } catch {
      return undefined;
    }
  }
  const permissions = isRecord(settings["permissions"]) ? { ...settings["permissions"] } : {};
  const deny = Array.isArray(permissions["deny"]) ? permissions["deny"].map((item) => String(item)) : [];
  const missing = BLEND_DENY.filter((rule) => !deny.includes(rule));
  if (!missing.length && existing !== undefined) return existing;
  permissions["deny"] = [...deny, ...missing];
  settings["permissions"] = permissions;
  return JSON.stringify(settings, null, 2) + "\n";
}

export function writeIfChanged(file: string, body: string | undefined): "written" | "unchanged" | "skipped" {
  if (body === undefined) return "skipped";
  const current = fs.existsSync(file) ? fs.readFileSync(file, "utf8") : undefined;
  if (current === body) return "unchanged";
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, body, "utf8");
  return "written";
}

export function readText(file: string): string | undefined {
  try {
    return fs.readFileSync(file, "utf8");
  } catch {
    return undefined;
  }
}

export function fileIsEmptyJson(body: string, style: JsonStyle): boolean {
  try {
    const parsed: unknown = JSON.parse(body);
    if (!isRecord(parsed)) return false;
    const servers = parsed[style];
    const otherKeys = Object.keys(parsed).filter((key) => key !== style);
    return otherKeys.length === 0 && isRecord(servers) && Object.keys(servers).length === 0;
  } catch {
    return false;
  }
}
