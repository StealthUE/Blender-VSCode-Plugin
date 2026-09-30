import * as fs from "fs";
import * as vscode from "vscode";
import { serverLaunch } from "./configWrite";
import { installAddon, runDoctor } from "./doctor";
import { findBlenders, findNode } from "./findBlender";
import { readConfig } from "./projectConfig";
import { loadWorkspaceContext } from "./tools";
import { applyWorkspace } from "./workspaceSetup";
import { clampPort, ClientFlags, DEFAULT_PORT, normalizeClients } from "./types";

interface PanelState {
  blenders: { path: string; version: string }[];
  blender: string;
  port: number;
  clients: ClientFlags;
  replaceLegacy: boolean;
  node?: string;
}

let panel: vscode.WebviewPanel | undefined;

function configuredClients(workspace: string): ClientFlags {
  const saved = readConfig(workspace);
  if (saved) return saved.clients;
  const settings = vscode.workspace.getConfiguration("vsblender");
  return normalizeClients({
    claude: settings.get<boolean>("clients.claude"),
    vscode: settings.get<boolean>("clients.vscode"),
    grok: settings.get<boolean>("clients.grok"),
    cursor: settings.get<boolean>("clients.cursor"),
    cline: settings.get<boolean>("clients.cline"),
  });
}

async function collectState(workspace: string): Promise<PanelState> {
  const saved = readConfig(workspace);
  const settings = vscode.workspace.getConfiguration("vsblender");
  const settingPath = settings.get<string>("blenderPath")?.trim();
  const installs = await findBlenders();
  const blender = saved?.blender || settingPath || installs[0]?.path || "";
  if (blender && !installs.some((item) => item.path === blender)) {
    installs.unshift({ path: blender, version: "saved path" });
  }
  const node = await findNode();
  return {
    blenders: installs,
    blender,
    port: saved?.port ?? clampPort(settings.get<number>("port"), DEFAULT_PORT),
    clients: configuredClients(workspace),
    replaceLegacy: saved?.replaceLegacy ?? settings.get<boolean>("replaceLegacy") !== false,
    ...(node ? { node } : {}),
  };
}

function html(state: PanelState): string {
  const options = state.blenders.map((item) => {
    const checked = item.path === state.blender ? "checked" : "";
    const label = `${item.version} — ${item.path}`;
    return `<label class="choice"><input type="radio" name="blender" value="${escapeAttr(item.path)}" ${checked}> ${escapeText(label)}</label>`;
  }).join("");
  const box = (id: keyof ClientFlags, label: string): string =>
    `<label><input type="checkbox" id="${id}" ${state.clients[id] ? "checked" : ""}> ${label}</label>`;
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline';">
<style>
  body { font-family: var(--vscode-font-family); color: var(--vscode-foreground); background: var(--vscode-editor-background); padding: 16px 20px 32px; line-height: 1.45; }
  h1 { font-size: 1.3rem; font-weight: 600; margin: 0 0 8px; }
  p { margin: 0 0 12px; }
  fieldset { border: 1px solid var(--vscode-panel-border, rgba(127,127,127,.4)); margin: 0 0 14px; padding: 10px 12px; }
  legend { padding: 0 6px; }
  label { display: block; margin: 4px 0; }
  .choice { font-family: var(--vscode-editor-font-family, monospace); font-size: 12px; }
  input[type="text"], input[type="number"] { width: 100%; box-sizing: border-box; background: var(--vscode-input-background); color: var(--vscode-input-foreground); border: 1px solid var(--vscode-input-border, transparent); padding: 4px 6px; }
  button { background: var(--vscode-button-background); color: var(--vscode-button-foreground); border: 0; padding: 8px 14px; margin-top: 8px; cursor: pointer; }
  button:disabled { opacity: .6; cursor: default; }
  pre { white-space: pre-wrap; background: var(--vscode-textCodeBlock-background, rgba(127,127,127,.15)); padding: 10px; min-height: 4em; }
  .muted { opacity: .8; }
</style>
</head>
<body>
  <h1>Set up VSBlender</h1>
  <p>This installs the VSBlender add-on, writes MCP config for the clients you tick, and writes the guide files. Cursor and Cline stay off unless you tick them. A guide you wrote yourself is left alone.</p>
  <fieldset>
    <legend>Blender</legend>
    ${options || '<p class="muted">No Blender install was found. Paste the path to blender.exe.</p>'}
    <label>Path <input type="text" id="blenderPath" value="${escapeAttr(state.blender)}"></label>
  </fieldset>
  <fieldset>
    <legend>AI clients</legend>
    ${box("claude", "Claude Code — .mcp.json and CLAUDE.md")}
    ${box("vscode", "VS Code / Copilot — .vscode/mcp.json and copilot instructions")}
    ${box("grok", "Grok — .grok/config.toml (reads CLAUDE.md, so the guide is not copied twice)")}
    ${box("cursor", "Cursor — off by default")}
    ${box("cline", "Cline — off by default")}
    <label><input type="checkbox" id="replace" ${state.replaceLegacy ? "checked" : ""}> Remove existing mcp-for-blender entries</label>
  </fieldset>
  <label>Bridge port <input type="number" id="port" min="1024" max="65535" value="${state.port}"></label>
  <p class="muted">Node: ${escapeText(state.node ?? "not on PATH — VS Code will be used to run the MCP server")}</p>
  <button id="go" type="button">Install and configure</button>
  <button id="launch" type="button" hidden>Launch Blender</button>
  <pre id="log"></pre>
  <script>
    const vscode = acquireVsCodeApi();
    const log = document.getElementById("log");
    const button = document.getElementById("go");
    const launch = document.getElementById("launch");
    launch.addEventListener("click", () => vscode.postMessage({ type: "launch" }));
    function clients() {
      const flags = {};
      for (const id of ["claude", "vscode", "grok", "cursor", "cline"]) flags[id] = document.getElementById(id).checked;
      return flags;
    }
    button.addEventListener("click", () => {
      const picked = document.querySelector("input[name=blender]:checked");
      const typed = document.getElementById("blenderPath").value.trim();
      button.disabled = true;
      log.textContent = "";
      vscode.postMessage({
        type: "install",
        blender: typed || (picked ? picked.value : ""),
        port: Number(document.getElementById("port").value),
        clients: clients(),
        replaceLegacy: document.getElementById("replace").checked
      });
    });
    window.addEventListener("message", (event) => {
      const message = event.data || {};
      if (message.type === "log") log.textContent += message.line + "\\n";
      if (message.type === "done") {
        button.disabled = false;
        log.textContent += "\\n" + message.report + "\\n";
        if (message.configured) launch.hidden = false;
      }
    });
  </script>
</body>
</html>`;
}

function escapeText(value: string): string {
  return value.replace(/[&<>]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[char] ?? char));
}

function escapeAttr(value: string): string {
  return escapeText(value).replace(/"/g, "&quot;");
}

/**
 * extensionRoot is the stable runtime copy (see runtime.ts): it is written into every client's
 * MCP config, so it must not be the versioned extension folder.
 */
export async function showSetup(
  context: vscode.ExtensionContext,
  workspace: string,
  output: vscode.OutputChannel,
  extensionRoot: string,
  onConfigured: (workspace: string) => void
): Promise<void> {
  if (panel) {
    panel.reveal();
    return;
  }
  // Retained so the install log survives switching tabs while Blender runs in the background.
  panel = vscode.window.createWebviewPanel("vsblender.setup", "VSBlender Setup", vscode.ViewColumn.One, {
    enableScripts: true,
    retainContextWhenHidden: true,
  });
  const current = panel;
  current.onDidDispose(() => {
    panel = undefined;
  }, null, context.subscriptions);
  current.webview.html = "<html><body style=\"font-family: sans-serif; padding: 16px;\">Looking for Blender…</body></html>";
  const state = await collectState(workspace);
  if (!panel) return;
  current.webview.html = html(state);
  output.appendLine(`Setup opened for ${workspace}`);

  current.webview.onDidReceiveMessage(async (message: { type?: string; blender?: string; port?: number; clients?: unknown; replaceLegacy?: boolean }) => {
    if (message.type === "launch") {
      await vscode.commands.executeCommand("vsblender.launch");
      return;
    }
    if (message.type !== "install") return;
    const post = (line: string): void => {
      output.appendLine(line);
      void current.webview.postMessage({ type: "log", line });
    };
    const blender = (message.blender ?? "").trim();
    const port = clampPort(message.port, state.port);
    const clients = normalizeClients(message.clients);
    const replaceLegacy = message.replaceLegacy !== false;
    if (!blender || !fs.existsSync(blender)) {
      post(blender ? `Blender was not found at ${blender}` : "Choose a Blender install or paste the path to blender.exe.");
      void current.webview.postMessage({ type: "done", report: "Setup did not run." });
      return;
    }
    const node = await findNode();
    let launch;
    try {
      launch = serverLaunch({
        ...(node ? { nodePath: node } : {}),
        electronPath: process.execPath,
        extensionRoot,
        workspace,
        port,
        blender,
      });
    } catch (error) {
      post(error instanceof Error ? error.message : String(error));
      void current.webview.postMessage({ type: "done", report: "Setup did not run." });
      return;
    }
    post(`Installing the add-on with ${blender}`);
    const installed = await installAddon(blender, extensionRoot, port);
    post(installed.ok ? installed.message : `Add-on install failed. Launch Blender from VS Code still loads the bridge from the extension.\n${installed.message}`);
    // The Blender path and port are saved in .blender-ai/config.json (git-ignored), not in
    // .vscode/settings.json, which is often committed and would carry this machine's paths.
    const applied = applyWorkspace({ workspace, extensionRoot, port, blender, clients, replaceLegacy, launch });
    for (const file of applied.written) post(`wrote ${file}`);
    for (const file of applied.removed) post(`removed ${file}`);
    for (const file of applied.skipped) post(`left untouched (not ours, or not valid JSON): ${file}`);
    onConfigured(workspace);
    const ctx = loadWorkspaceContext({
      ...process.env,
      VSBLENDER_WORKSPACE: workspace,
      VSBLENDER_EXTENSION_ROOT: extensionRoot,
      VSBLENDER_PORT: String(port),
      VSBLENDER_BLENDER: blender,
    }, workspace);
    ctx.clients = clients;
    ctx.replaceLegacy = replaceLegacy;
    const report = await runDoctor(ctx, { electronPath: process.execPath });
    post(report.ok ? "Doctor passed." : "Doctor found something still wrong. The report is below.");
    const next: string[] = [];
    if (clients.claude) next.push("Claude Code asks you to approve the vsblender server in .mcp.json the first time it starts in this folder.");
    if (clients.vscode) next.push("VS Code: start the vsblender server from .vscode/mcp.json (or the MCP Servers view) if it does not start by itself.");
    next.push("Launch Blender below, or open Blender yourself: the add-on starts the bridge on its own once it is enabled.");
    void current.webview.postMessage({ type: "done", report: `${report.text}\n\nNext:\n- ${next.join("\n- ")}`, configured: true });
  }, undefined, context.subscriptions);
}
