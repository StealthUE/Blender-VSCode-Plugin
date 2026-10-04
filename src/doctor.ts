import * as fs from "fs";
import * as path from "path";
import { execFileText } from "./exec";
import { compareVersions, findNode } from "./findBlender";
import { callBridge, probeBridge } from "./bridge";
import { ingestStatus, listBlendFiles, relativeTo } from "./blendFiles";
import { describePrinter, resolvePrinter } from "./printers";
import { readConfig } from "./projectConfig";
import { applyWorkspace } from "./workspaceSetup";
import { hasScriptRule, readText, serverLaunch } from "./configWrite";
import { AUTO_APPROVE, claudeRule } from "./toolDefs";
import { ADDON_VERSION, ClientFlags, WorkspaceContext } from "./types";

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
    checks.push(check("add-on install", installed.ok, installed.message + (installed.ok ? ". Restart Blender to load it." : "")));
    const saved = readConfig(ctx.workspace);
    const applied = applyWorkspace({
      workspace: ctx.workspace,
      extensionRoot: ctx.extensionRoot,
      port: ctx.port,
      ...(ctx.blender ? { blender: ctx.blender } : {}),
      clients: ctx.clients,
      replaceLegacy: ctx.replaceLegacy,
      launch,
      ...(saved?.allowScripts !== undefined ? { allowScripts: saved.allowScripts } : {}),
      ...(saved?.allowTrustedScripts !== undefined ? { allowTrustedScripts: saved.allowTrustedScripts } : {}),
      ...(saved?.ignoreClientConfig !== undefined ? { ignoreClientConfig: saved.ignoreClientConfig } : {}),
      ...(saved?.checkpoints ? { checkpoints: saved.checkpoints } : {}),
      ...(saved?.libPaths ? { libPaths: saved.libPaths } : {}),
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
  if (isExtensionSource(ctx.workspace)) {
    checks.push(check("workspace", false, "this folder is the VSBlender extension source, and its scripts/ folder holds the extension's tests. "
      + "Keep model scripts next to their .blend, in <blend folder>/scripts/ (shared code in <blend folder>/scripts/lib/).", true));
  }
  if (config?.printer !== undefined) {
    const { profile, warnings } = resolvePrinter(config.printer);
    checks.push(check("printer", !warnings.length, warnings.length ? warnings.join("; ") : describePrinter(profile), true));
  }

  for (const file of clientFiles(ctx.clients, ctx.workspace)) {
    const exists = fs.existsSync(file.path);
    const text = exists ? fs.readFileSync(file.path, "utf8") : "";
    const mentions = text.includes("vsblender");
    checks.push(check(file.label, exists && mentions, exists && mentions ? file.path : `missing vsblender entry in ${file.path}`));
  }

  if (ctx.clients.claude || ctx.clients.grok) {
    const settings = readText(path.join(ctx.workspace, ".claude", "settings.json")) ?? "";
    const missing = AUTO_APPROVE.map(claudeRule).filter((rule) => !settings.includes(`"${rule}"`));
    const scripts = hasScriptRule(readText(path.join(ctx.workspace, ".claude", "settings.local.json")))
      ? "run_script runs without asking (settings.local.json)"
      : "run_script asks each time";
    checks.push(check("claude permissions", !missing.length,
      missing.length ? `${missing.length} read-only tool(s) still ask for approval (${missing.slice(0, 3).join(", ")}...). Run doctor_fix.`
        : `read-only tools run without asking; ${scripts}`, true));
  }

  const bridge = await probeBridge(ctx.port);
  let live: { file?: string; dirty?: boolean; status?: string; detail?: string } = {};
  if (bridge.ok && bridge.version && bridge.version !== ADDON_VERSION) {
    checks.push(check("bridge", false, `${bridge.detail}, but the add-on in Blender is ${bridge.version} and this extension ships ${ADDON_VERSION}. Run doctor_fix, then restart Blender (or close Blender and use launch_blender, which loads the shipped add-on).`, true));
  } else if (bridge.state === "absent") {
    checks.push(check("bridge", false, `${bridge.detail}. Blender is not open with the add-on yet: use Launch Blender.`, true));
  } else {
    checks.push(check("bridge", bridge.ok, bridge.detail));
    if (bridge.ok && !bridge.busy) {
      try {
        const info = await callBridge(ctx.port, "session_info", {}, 15000);
        const sidecar = info.result?.["sidecar"] as { status?: string; detail?: string } | undefined;
        live = {
          file: String(info.result?.["file"] ?? ""),
          dirty: info.result?.["dirty"] === true,
          ...(sidecar?.status ? { status: sidecar.status } : {}),
          ...(sidecar?.detail ? { detail: sidecar.detail } : {}),
        };
      } catch {
        live = {};
      }
    }
  }

  const blends = listBlendFiles(ctx.workspace);
  if (!blends.length) {
    checks.push(check("blend files", true, "no .blend files in the workspace"));
  } else {
    const summary = blends.map((file) => {
      const rel = relativeTo(ctx.workspace, file);
      const fold = (p: string): string => (process.platform === "win32" ? path.resolve(p).toLowerCase() : path.resolve(p));
      const open = live.file && fold(live.file) === fold(file);
      if (open && live.status) {
        return `${rel} (${live.status}, open in Blender${live.dirty ? " with unsaved changes" : ""}${live.status === "current" ? "" : `: ${live.detail ?? ""}`})`;
      }
      return `${rel} (${ingestStatus(file)})`;
    }).join(", ");
    checks.push(check("blend files", true, summary));
  }

  const ok = checks.every((item) => item.ok || item.optional);
  return { ok, checks, text: formatReport(checks) };
}

/** The workspace is this extension's own repository (its package.json names vsblender, or it holds the add-on source). */
export function isExtensionSource(workspace: string): boolean {
  if (fs.existsSync(path.join(workspace, "resources", "addon", "vsblender_bridge", "__init__.py"))) return true;
  try {
    const pkg = JSON.parse(fs.readFileSync(path.join(workspace, "package.json"), "utf8")) as { name?: unknown };
    return pkg.name === "vsblender";
  } catch {
    return false;
  }
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
