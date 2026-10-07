import * as fs from "fs";
import * as path from "path";
import { AUTO_APPROVE, claudeRule, PIPELINE_TOOL, SAVE_TOOL, SCRIPT_TOOL, TOOL_NAMES, TRUSTED_PIPELINE_TOOL, TRUSTED_SCRIPT_TOOL } from "./toolDefs";
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

/** Cline reads alwaysAllow from the server entry: the shared allow list, plus save when that tick is on. */
export function clineServerEntry(launch: ServerLaunch, allowScripts: boolean, allowTrusted = false, allowSave = false): JsonRecord {
  const extra = [
    ...(allowScripts ? [SCRIPT_TOOL, PIPELINE_TOOL] : []),
    ...(allowScripts || allowTrusted ? [TRUSTED_SCRIPT_TOOL, TRUSTED_PIPELINE_TOOL] : []),
    ...(allowSave ? [SAVE_TOOL] : []),
  ];
  const alwaysAllow = [...new Set([...AUTO_APPROVE, ...extra])];
  return { ...claudeServerEntry(launch), alwaysAllow, disabled: false };
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

const GITIGNORE_BASE = [
  ".blender-ai/config.json",
  ".blender-ai/launch.log",
  ".blender-ai/live/",
  ".blender-ai/jobs/",
  ".blender-ai/renders/",
  ".blender-ai/references/",
  // Sidecars sit next to each .blend, in any folder: unanchored patterns.
  "**/.blender-ai/_archive/",
  "**/.blender-ai/**/state.json",
  "**/.blender-ai/**/previews/",
  "**/.blender-ai/**/checkpoints/",
  "**/.blender-ai/**/texts/",
  "**/.blender-ai/**/refs/",
  ".claude/settings.local.json",
];

/** The MCP files setup writes hold this machine's paths (node, the extension's storage folder). */
export const CLIENT_CONFIG_FILES = [".mcp.json", ".vscode/mcp.json", ".grok/config.toml", ".cursor/mcp.json", ".cline/mcp.json"];

export function mergeGitignore(existing: string | undefined, ignoreClientConfig = false): string {
  const source = existing ?? "";
  const without = source.replace(/# vsblender-begin[\s\S]*?# vsblender-end\r?\n?/g, "").replace(/\s+$/, "");
  const lines = ["# vsblender-begin", ...GITIGNORE_BASE];
  if (ignoreClientConfig) {
    lines.push("# MCP client config written by VSBlender setup: paths for this machine. Each person runs setup.");
    lines.push(...CLIENT_CONFIG_FILES);
  }
  lines.push("# vsblender-end");
  return (without ? `${without}\n\n` : "") + lines.join("\n") + "\n";
}

const BLEND_DENY = [
  "Read(**/*.blend)",
  "Read(**/*.blend1)",
  "Edit(**/*.blend)",
  "Edit(**/*.blend1)",
];

function parseSettings(existing: string | undefined): JsonRecord | undefined {
  if (existing === undefined) return {};
  try {
    const parsed: unknown = JSON.parse(existing);
    return isRecord(parsed) ? parsed : undefined;
  } catch {
    return undefined;
  }
}

function ruleList(permissions: JsonRecord, key: string): string[] {
  return Array.isArray(permissions[key]) ? (permissions[key] as unknown[]).map((item) => String(item)) : [];
}

/**
 * .claude/settings.json: deny reading or editing .blend files, and allow the shared VSBlender list
 * (read-only tools and the build loop) without asking. Rules for tools that no longer exist are
 * dropped. Other rules are left alone.
 */
export function mergeClaudeSettings(existing: string | undefined): string | undefined {
  const settings = parseSettings(existing);
  if (!settings) return undefined;
  const permissions = isRecord(settings["permissions"]) ? { ...settings["permissions"] } : {};
  const deny = ruleList(permissions, "deny");
  const allow = ruleList(permissions, "allow");
  const ours = new Set(TOOL_NAMES.map(claudeRule));
  // Old builds allowed tool names that were renamed or split; keep only rules for tools that exist.
  const keptAllow = allow.filter((rule) => !rule.startsWith("mcp__vsblender__") || ours.has(rule));
  const wantAllow = AUTO_APPROVE.map(claudeRule);
  const nextAllow = [...keptAllow, ...wantAllow.filter((rule) => !keptAllow.includes(rule))];
  const nextDeny = [...deny, ...BLEND_DENY.filter((rule) => !deny.includes(rule))];
  if (existing !== undefined && JSON.stringify(nextAllow) === JSON.stringify(allow) && JSON.stringify(nextDeny) === JSON.stringify(deny)) {
    return existing;
  }
  permissions["allow"] = nextAllow;
  permissions["deny"] = nextDeny;
  settings["permissions"] = permissions;
  return JSON.stringify(settings, null, 2) + "\n";
}

/** Kept for callers of the first version: deny rules plus the read-only allow rules. */
export const mergeClaudeDeny = mergeClaudeSettings;

/**
 * .claude/settings.local.json is personal and not committed. save lives here when its tick is on.
 * The script tools are already on the shared allow list; a true here writes a personal copy, and a
 * false removes only that copy. allow undefined leaves the file as it is.
 */
export function mergeClaudeLocal(existing: string | undefined, allowScripts: boolean | undefined,
  allowTrusted?: boolean, allowSave?: boolean): string | undefined {
  if (allowScripts === undefined && allowTrusted === undefined && allowSave === undefined) return existing;
  const settings = parseSettings(existing);
  if (!settings) return undefined;
  const permissions = isRecord(settings["permissions"]) ? { ...settings["permissions"] } : {};
  let allow = ruleList(permissions, "allow");
  const want = new Map<string, boolean>();
  // run_script and run_pipeline run any workspace script; run_project_script and run_project_pipeline
  // only trusted folders.
  if (allowScripts !== undefined) {
    want.set(claudeRule(SCRIPT_TOOL), allowScripts);
    want.set(claudeRule(PIPELINE_TOOL), allowScripts);
  }
  const trusted = allowTrusted ?? (allowScripts === true ? true : undefined);
  if (trusted !== undefined) {
    want.set(claudeRule(TRUSTED_SCRIPT_TOOL), trusted || allowScripts === true);
    want.set(claudeRule(TRUSTED_PIPELINE_TOOL), trusted || allowScripts === true);
  }
  // Saving the user's file is its own decision.
  if (allowSave !== undefined) want.set(claudeRule(SAVE_TOOL), allowSave);
  let changed = false;
  for (const [rule, on] of want) {
    const has = allow.includes(rule);
    if (has === on) continue;
    allow = on ? [...allow, rule] : allow.filter((item) => item !== rule);
    changed = true;
  }
  if (!changed) return existing;
  if (existing === undefined && !allow.length) return undefined;
  permissions["allow"] = allow;
  settings["permissions"] = permissions;
  return JSON.stringify(settings, null, 2) + "\n";
}

export function hasScriptRule(existing: string | undefined): boolean {
  const settings = parseSettings(existing);
  const permissions = settings && isRecord(settings["permissions"]) ? settings["permissions"] : {};
  return ruleList(permissions, "allow").includes(claudeRule(SCRIPT_TOOL));
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
