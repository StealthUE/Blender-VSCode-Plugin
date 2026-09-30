import * as fs from "fs";
import * as path from "path";
import { execFileText } from "./exec";
import { compareVersions, findNode } from "./findBlender";
import { probeBridge } from "./bridge";
import { ingestStatus, listBlendFiles, relativeTo } from "./blendFiles";
import { readConfig } from "./projectConfig";
import { applyWorkspace } from "./workspaceSetup";
import { serverLaunch } from "./configWrite";
import { ADDON_VERSION, ClientFlags, DEFAULT_CLIENTS, WorkspaceContext } from "./types";

export interface Check {
  id: string;
  ok: boolean;
  message: string;
  /** A warning: reported, but it does not fail the doctor (for example, Blender simply is not open yet). */
  optional?: boolean;
}

export interface DoctorReport {
  ok: boolean;
  checks: Check[];
  text: string;
}

function check(id: string, ok: boolean, message: string, optional = false): Check {
  return { id, ok, message, ...(optional ? { optional } : {}) };
}

export function addonSource(extensionRoot: string): string {
  return path.join(extensionRoot, "resources", "addon", "vsblender_bridge");
}

export function formatReport(checks: Check[]): string {
  return checks.map((item) => `${item.ok ? "ok" : item.optional ? "warn" : "FAIL"}  ${item.id}: ${item.message}`).join("\n");
}

/** Offscreen preview uses context.temp_override, which Blender added in 3.2. */
const MIN_BLENDER = "3.2";

async function blenderVersion(exe: string): Promise<string | undefined> {
  const result = await execFileText(exe, ["--version"], { timeout: 30000 });
  const match = /Blender\s+(\d+\.\d+(?:\.\d+)?)/i.exec(`${result.stdout}\n${result.stderr}`);
  return match?.[1];
}

export async function installAddon(blender: string, extensionRoot: string, port: number): Promise<{ ok: boolean; message: string }> {
  const src = addonSource(extensionRoot);
  const script = path.join(extensionRoot, "resources", "enable_addon.py");
  if (!fs.existsSync(path.join(src, "__init__.py")) || !fs.existsSync(script)) {
    return { ok: false, message: "the extension is missing resources/addon or enable_addon.py" };
  }
  const result = await execFileText(blender, ["-b", "--python", script], {
    timeout: 120000,
    env: { ...process.env, VSBLENDER_ADDON_SRC: src, VSBLENDER_PORT: String(port) },
  });
  const output = `${result.stdout}\n${result.stderr}`;
  const okLine = output.split(/\r?\n/).find((line) => line.startsWith("VSBLENDER_INSTALL_OK"));
  if (okLine) return { ok: true, message: okLine.trim() };
  const tail = output.trim().split(/\r?\n/).slice(-12).join("\n");
  return { ok: false, message: tail || "Blender did not confirm the add-on install" };
}

export async function runDoctor(
  ctx: WorkspaceContext,
  options?: { fix?: boolean; electronPath?: string }
): Promise<DoctorReport> {
  const checks: Check[] = [];
  const script = path.join(ctx.extensionRoot, "out", "mcp.js");
  checks.push(check("mcp script", fs.existsSync(script), fs.existsSync(script) ? script : `missing ${script}; compile the extension`));

  const node = await findNode();
  let launch;
  try {
    launch = serverLaunch({
      ...(node ? { nodePath: node } : {}),
      ...(options?.electronPath ? { electronPath: options.electronPath } : {}),
      extensionRoot: ctx.extensionRoot,
      workspace: ctx.workspace,
      port: ctx.port,
      ...(ctx.blender ? { blender: ctx.blender } : {}),
    });
    checks.push(check("node", true, launch.via === "node" ? launch.command : `using VS Code as Node (${launch.command})`));
  } catch (error) {
    checks.push(check("node", false, error instanceof Error ? error.message : String(error)));
  }

  if (options?.fix && launch && ctx.blender) {
    const installed = await installAddon(ctx.blender, ctx.extensionRoot, ctx.port);
    checks.push(check("add-on install", installed.ok, installed.message));
    const applied = applyWorkspace({
      workspace: ctx.workspace,
      extensionRoot: ctx.extensionRoot,
      port: ctx.port,
      ...(ctx.blender ? { blender: ctx.blender } : {}),
      clients: ctx.clients,
      replaceLegacy: ctx.replaceLegacy,
      launch,
    });
    checks.push(check("config", true, `rewrote ${applied.written.length} file(s); skipped ${applied.skipped.length}`));
  }

  if (!ctx.blender) {
    checks.push(check("blender", false, "no Blender path yet. Run VSBlender: Setup."));
  } else if (!fs.existsSync(ctx.blender)) {
    checks.push(check("blender", false, `not found: ${ctx.blender}`));
  } else {
    const version = await blenderVersion(ctx.blender);
    if (!version) {
      checks.push(check("blender", false, `could not run ${ctx.blender} --version`));
    } else if (compareVersions(version, MIN_BLENDER) < 0) {
      checks.push(check("blender", false, `${version} at ${ctx.blender}. VSBlender needs ${MIN_BLENDER} or newer (4.2+ recommended). Pick another install in Setup.`));
    } else {
      checks.push(check("blender", true, `${version} at ${ctx.blender}`));
    }
  }

  const config = readConfig(ctx.workspace);
  checks.push(check("workspace config", Boolean(config), config ? `port ${config.port}` : "missing .blender-ai/config.json"));

  for (const file of clientFiles(ctx.clients, ctx.workspace)) {
    const exists = fs.existsSync(file.path);
    const text = exists ? fs.readFileSync(file.path, "utf8") : "";
    const mentions = text.includes("vsblender");
    checks.push(check(file.label, exists && mentions, exists && mentions ? file.path : `missing vsblender entry in ${file.path}`));
  }

  const bridge = await probeBridge(ctx.port);
  if (bridge.ok && bridge.version && bridge.version !== ADDON_VERSION) {
    checks.push(check("bridge", false, `${bridge.detail}, but the add-on in Blender is ${bridge.version} and this extension ships ${ADDON_VERSION}. Run doctor with fix, then restart Blender.`, true));
  } else if (bridge.state === "absent") {
    checks.push(check("bridge", false, `${bridge.detail}. Blender is not open with the add-on yet: use Launch Blender.`, true));
  } else {
    checks.push(check("bridge", bridge.ok, bridge.detail));
  }

  const blends = listBlendFiles(ctx.workspace);
  if (!blends.length) {
    checks.push(check("blend files", true, "no .blend files in the workspace"));
  } else {
    const summary = blends.map((file) => `${relativeTo(ctx.workspace, file)} (${ingestStatus(file)})`).join(", ");
    checks.push(check("blend files", true, summary));
  }

  const ok = checks.every((item) => item.ok || item.optional);
  return { ok, checks, text: formatReport(checks) };
}

function clientFiles(clients: ClientFlags, workspace: string): { label: string; path: string }[] {
  const files: { label: string; path: string; on: boolean }[] = [
    { label: "claude mcp", path: path.join(workspace, ".mcp.json"), on: clients.claude },
    { label: "vscode mcp", path: path.join(workspace, ".vscode", "mcp.json"), on: clients.vscode },
    { label: "grok mcp", path: path.join(workspace, ".grok", "config.toml"), on: clients.grok },
    { label: "cursor mcp", path: path.join(workspace, ".cursor", "mcp.json"), on: clients.cursor },
    { label: "cline mcp", path: path.join(workspace, ".cline", "mcp.json"), on: clients.cline },
  ];
  return files.filter((file) => file.on).map(({ label, path: file }) => ({ label, path: file }));
}

export function contextFromConfig(
  workspace: string,
  extensionRoot: string,
  overrides?: { blender?: string; port?: number; clients?: ClientFlags }
): WorkspaceContext {
  const config = readConfig(workspace);
  const blender = overrides?.blender || config?.blender;
  return {
    workspace,
    extensionRoot,
    port: overrides?.port || config?.port || 47876,
    ...(blender ? { blender } : {}),
    clients: overrides?.clients ?? config?.clients ?? DEFAULT_CLIENTS,
    replaceLegacy: config?.replaceLegacy ?? true,
  };
}
