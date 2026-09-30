import * as fs from "fs";
import * as path from "path";
import { blendTable, renderGuide, writeGuides } from "./aiFiles";
import { ingestStatus, listBlendFiles, relativeTo } from "./blendFiles";
import {
  claudeServerEntry,
  fileIsEmptyJson,
  mergeClaudeDeny,
  mergeGitignore,
  mergeGrokToml,
  mergeMcpJson,
  readText,
  removeGrokServer,
  removeMcpServer,
  renderGrokToml,
  vscodeServerEntry,
  writeIfChanged,
} from "./configWrite";
import { writeConfig } from "./projectConfig";
import { ClientFlags, ProjectConfig, ServerLaunch } from "./types";

export interface ApplyInput {
  workspace: string;
  extensionRoot: string;
  port: number;
  blender?: string;
  clients: ClientFlags;
  replaceLegacy: boolean;
  launch: ServerLaunch;
}

export interface ApplyResult {
  written: string[];
  unchanged: string[];
  skipped: string[];
  removed: string[];
}

interface JsonTarget {
  rel: string;
  style: "servers" | "mcpServers";
  enabled: boolean;
  entry: ReturnType<typeof vscodeServerEntry>;
}

function record(result: ApplyResult, file: string, state: "written" | "unchanged" | "skipped"): void {
  if (state === "written") result.written.push(file);
  else if (state === "unchanged") result.unchanged.push(file);
  else result.skipped.push(file);
}

export function applyWorkspace(input: ApplyInput): ApplyResult {
  const result: ApplyResult = { written: [], unchanged: [], skipped: [], removed: [] };
  const config: ProjectConfig = {
    version: 1,
    port: input.port,
    ...(input.blender ? { blender: input.blender } : {}),
    clients: input.clients,
    replaceLegacy: input.replaceLegacy,
  };
  const configFile = writeConfig(input.workspace, config);
  result.written.push(configFile);

  const targets: JsonTarget[] = [
    { rel: ".mcp.json", style: "mcpServers", enabled: input.clients.claude, entry: claudeServerEntry(input.launch) },
    { rel: path.join(".vscode", "mcp.json"), style: "servers", enabled: input.clients.vscode, entry: vscodeServerEntry(input.launch) },
    { rel: path.join(".cursor", "mcp.json"), style: "mcpServers", enabled: input.clients.cursor, entry: claudeServerEntry(input.launch) },
    { rel: path.join(".cline", "mcp.json"), style: "mcpServers", enabled: input.clients.cline, entry: claudeServerEntry(input.launch) },
  ];

  for (const target of targets) {
    const file = path.join(input.workspace, target.rel);
    const existing = readText(file);
    if (!target.enabled) {
      if (existing === undefined) continue;
      const next = removeMcpServer(existing, target.style);
      if (next === undefined || next === existing) continue;
      if (fileIsEmptyJson(next, target.style)) {
        fs.rmSync(file);
        result.removed.push(file);
      } else {
        record(result, file, writeIfChanged(file, next));
      }
      continue;
    }
    const next = mergeMcpJson(existing, target.style, target.entry, input.replaceLegacy);
    if (next === undefined) {
      result.skipped.push(file);
      continue;
    }
    record(result, file, writeIfChanged(file, next));
  }

  const grokFile = path.join(input.workspace, ".grok", "config.toml");
  if (input.clients.grok) {
    const next = mergeGrokToml(readText(grokFile), renderGrokToml(input.launch));
    record(result, grokFile, writeIfChanged(grokFile, next));
  } else if (fs.existsSync(grokFile)) {
    const next = removeGrokServer(fs.readFileSync(grokFile, "utf8"));
    if (!next.trim()) {
      fs.rmSync(grokFile);
      result.removed.push(grokFile);
    } else {
      record(result, grokFile, writeIfChanged(grokFile, next));
    }
  }

  const templatePath = path.join(input.extensionRoot, "resources", "blender-guide.md");
  const template = fs.existsSync(templatePath)
    ? fs.readFileSync(templatePath, "utf8")
    : "# VSBlender\n\n{{BLEND_TABLE}}\n\nPort {{PORT}}\n";
  const rows = listBlendFiles(input.workspace).map((file) => ({
    rel: relativeTo(input.workspace, file),
    status: ingestStatus(file),
  }));
  const guides = writeGuides(input.workspace, input.clients, renderGuide(template, blendTable(rows), input.port));
  result.written.push(...guides.written);
  result.skipped.push(...guides.skipped);
  result.removed.push(...guides.removed);

  if (input.clients.claude || input.clients.grok) {
    const denyFile = path.join(input.workspace, ".claude", "settings.json");
    const next = mergeClaudeDeny(readText(denyFile));
    record(result, denyFile, writeIfChanged(denyFile, next));
  }

  const ignoreFile = path.join(input.workspace, ".gitignore");
  record(result, ignoreFile, writeIfChanged(ignoreFile, mergeGitignore(readText(ignoreFile))));
  return result;
}
