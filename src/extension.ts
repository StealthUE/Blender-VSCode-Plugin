import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import { ingestStatus, listBlendFiles, relativeTo } from "./blendFiles";
import { callBridge, probeBridge } from "./bridge";
import { serverLaunch } from "./configWrite";
import { findNode } from "./findBlender";
import { runIngest, ingestScript } from "./ingest";
import { readConfig } from "./projectConfig";
import { syncRuntime } from "./runtime";
import { showSetup } from "./setup";
import { installIfStale } from "./doctor";
import { doctor, launchBlender, loadWorkspaceContext } from "./tools";
import { ADDON_VERSION, WorkspaceContext } from "./types";
import { applyWorkspace } from "./workspaceSetup";

let status: vscode.StatusBarItem | undefined;
let timer: NodeJS.Timeout | undefined;
let refreshTimer: NodeJS.Timeout | undefined;
/** undefined = not computed yet. The scan is recursive, so it is not repeated on every status poll. */
let cachedRoot: { folder: string | undefined } | undefined;
let runtime: string | undefined;
const pending = new Map<string, NodeJS.Timeout>();
const ingestQueue: { file: string; actor: string; manual: boolean }[] = [];
/** Journal files already offered a save for this dirty streak. A save event clears the key. */
const dirtyJournals = new Set<string>();
let draining = false;

function workspaceRoot(): string | undefined {
  if (cachedRoot) return cachedRoot.folder;
  const folders = vscode.workspace.workspaceFolders ?? [];
  const withBlend = folders.find((folder) => listBlendFiles(folder.uri.fsPath).length);
  cachedRoot = { folder: (withBlend ?? folders[0])?.uri.fsPath };
  return cachedRoot.folder;
}

/** Where the MCP server and Blender scripts run from. See runtime.ts. */
function runtimeRoot(context: vscode.ExtensionContext, output: vscode.OutputChannel): string {
  if (runtime) return runtime;
  const version = String((context.extension.packageJSON as { version?: string }).version ?? "0");
  try {
    runtime = syncRuntime(context.extensionUri.fsPath, context.globalStorageUri.fsPath, version);
  } catch (error) {
    output.appendLine(`Could not copy the server to global storage, using the extension folder: ${error instanceof Error ? error.message : String(error)}`);
    runtime = context.extensionUri.fsPath;
  }
  return runtime;
}

function toolContext(context: vscode.ExtensionContext, output: vscode.OutputChannel, folder: string): WorkspaceContext {
  return loadWorkspaceContext({
    ...process.env,
    VSBLENDER_WORKSPACE: folder,
    VSBLENDER_EXTENSION_ROOT: runtimeRoot(context, output),
  }, folder);
}

function autoIngestOn(): boolean {
  return vscode.workspace.getConfiguration("vsblender").get<boolean>("autoIngest") !== false;
}

/** The .blend from the explorer, the only one in the workspace, or the one the user picks. */
async function chooseBlend(folder: string, uri: vscode.Uri | undefined, placeHolder: string): Promise<string | undefined> {
  if (uri?.fsPath && uri.fsPath.toLowerCase().endsWith(".blend")) return uri.fsPath;
  const files = listBlendFiles(folder);
  if (files.length <= 1) return files[0];
  const picked = await vscode.window.showQuickPick(
    files.map((file) => ({ label: relativeTo(folder, file), file })),
    { placeHolder }
  );
  return picked?.file;
}

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  const output = vscode.window.createOutputChannel("VSBlender");
  context.subscriptions.push(output);
  status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  status.command = "vsblender.status";
  status.show();
  context.subscriptions.push(status);

  const root = (): string | undefined => workspaceRoot();
  const refreshStatus = async (): Promise<void> => {
    const folder = root();
    if (!status) return;
    if (!folder) {
      status.text = "$(gear) Blender";
      status.tooltip = "Open a folder to set up VSBlender";
      return;
    }
    const config = readConfig(folder);
    if (!config) {
      status.text = "$(gear) Blender: setup";
      status.tooltip = "VSBlender: Setup";
      return;
    }
    const probe = await probeBridge(config.port);
    if (probe.ok) {
      const share = typeof probe.progress?.fraction === "number" ? ` ${Math.round(probe.progress.fraction * 100)}%` : "";
      status.text = `$(debug-start) Blender :${config.port}${probe.busy ? ` (busy${share})` : ""}`;
      status.tooltip = probe.version && probe.version !== ADDON_VERSION
        ? `${probe.detail}. The add-on in Blender is ${probe.version}; this extension ships ${ADDON_VERSION}. Run Doctor.`
        : probe.detail;
    } else if (probe.state === "absent") {
      status.text = "$(debug-disconnect) Blender: offline";
      status.tooltip = `${probe.detail}. Click to launch Blender.`;
    } else {
      status.text = "$(warning) Blender: port busy";
      status.tooltip = `${probe.detail}. Click to run Doctor.`;
    }
  };
  timer = setInterval(() => void refreshStatus(), 4000);
  context.subscriptions.push({ dispose: () => { if (timer) clearInterval(timer); } });
  void refreshStatus();

  const onConfigured = (folder: string): void => {
    void startWorkspace(context, folder, output);
    void refreshStatus();
  };

  context.subscriptions.push(vscode.commands.registerCommand("vsblender.showLog", () => output.show()));
  context.subscriptions.push(vscode.commands.registerCommand("vsblender.status", async () => {
    const folder = root();
    if (!folder) return;
    const config = readConfig(folder);
    if (!config) {
      await showSetup(context, folder, output, runtimeRoot(context, output), onConfigured);
      return;
    }
    const probe = await probeBridge(config.port);
    if (probe.state === "absent") {
      const choice = await vscode.window.showQuickPick(
        [
          { label: "$(play) Launch Blender", command: "vsblender.launch" },
          { label: "$(pulse) Doctor", command: "vsblender.doctor" },
          { label: "$(gear) Setup", command: "vsblender.setup" },
        ],
        { placeHolder: "The VSBlender bridge is not running" }
      );
      if (choice) await vscode.commands.executeCommand(choice.command);
      return;
    }
    await vscode.commands.executeCommand("vsblender.doctor");
  }));
  context.subscriptions.push(vscode.commands.registerCommand("vsblender.setup", async () => {
    const folder = root();
    if (!folder) {
      vscode.window.showWarningMessage("Open a folder before running VSBlender setup.");
      return;
    }
    await showSetup(context, folder, output, runtimeRoot(context, output), onConfigured);
  }));
  context.subscriptions.push(vscode.commands.registerCommand("vsblender.doctor", async () => {
    const folder = root();
    if (!folder) return;
    const outcome = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Window, title: "VSBlender doctor" },
      () => doctor(toolContext(context, output, folder), false)
    );
    output.appendLine(outcome.text);
    output.show(true);
    const first = outcome.text.split(/\r?\n/).find((line) => line.startsWith("FAIL"));
    if (outcome.ok) void vscode.window.showInformationMessage("VSBlender doctor passed.");
    else void vscode.window.showWarningMessage(first ?? "VSBlender doctor found a problem. See the VSBlender log.");
  }));
  context.subscriptions.push(vscode.commands.registerCommand("vsblender.launch", async (uri?: vscode.Uri) => {
    const folder = root();
    if (!folder) return;
    const ctx = toolContext(context, output, folder);
    const file = await chooseBlend(folder, uri, "Blend file to open in Blender");
    const outcome = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: "Launching Blender" },
      () => launchBlender(ctx, file ? { file } : {})
    );
    output.appendLine(outcome.text);
    if (outcome.ok) void vscode.window.showInformationMessage(outcome.text.split("\n")[0] ?? "Blender is up.");
    else void vscode.window.showErrorMessage(outcome.text.split("\n")[0] ?? "Could not launch Blender.");
    void refreshStatus();
  }));
  context.subscriptions.push(vscode.commands.registerCommand("vsblender.ingest", async (uri?: vscode.Uri) => {
    const folder = root();
    if (!folder) return;
    const file = await chooseBlend(folder, uri, "Blend file to ingest");
    if (!file) {
      void vscode.window.showInformationMessage("There is no .blend file in this workspace to ingest.");
      return;
    }
    enqueueIngest(context, folder, output, file, "human", true);
  }));

  context.subscriptions.push(vscode.workspace.onDidChangeWorkspaceFolders(() => {
    cachedRoot = undefined;
    void refreshStatus();
  }));

  // Registered whether or not setup has run, and checked per event, so finishing setup
  // later in this window starts watching without a reload.
  const blendWatcher = vscode.workspace.createFileSystemWatcher("**/*.blend");
  /** A workspace .blend, not a checkpoint or a render job's copy under .blender-ai/. */
  const ownBlend = (folder: string, uri: vscode.Uri): boolean => {
    if (!uri.fsPath.toLowerCase().endsWith(".blend")) return false;
    const rel = path.relative(folder, uri.fsPath);
    return !rel.startsWith("..") && !rel.split(/[\\/]/).includes(".blender-ai");
  };
  const onBlend = (uri: vscode.Uri): void => {
    const folder = root();
    if (!folder || !readConfig(folder) || !autoIngestOn() || !ownBlend(folder, uri)) return;
    scheduleIngest(context, folder, output, uri.fsPath);
  };
  // The guides list the .blend files, so adding or removing one rewrites them.
  const onBlendSet = (uri: vscode.Uri): void => {
    const folder = root();
    if (!folder || !readConfig(folder) || !ownBlend(folder, uri)) return;
    cachedRoot = undefined;
    scheduleRefresh(context, folder, output);
  };
  blendWatcher.onDidCreate((uri) => {
    onBlend(uri);
    onBlendSet(uri);
  });
  blendWatcher.onDidChange(onBlend);
  blendWatcher.onDidDelete(onBlendSet);
  context.subscriptions.push(blendWatcher);

  const journalWatcher = vscode.workspace.createFileSystemWatcher("**/.blender-ai/**/journal.jsonl");
  const onJournal = (uri: vscode.Uri): void => { void offerSave(uri); };
  journalWatcher.onDidCreate(onJournal);
  journalWatcher.onDidChange(onJournal);
  context.subscriptions.push(journalWatcher);

  const folder = root();
  if (!folder) return;
  if (!readConfig(folder)) {
    // Not awaited: activation should not wait on the Blender search behind the setup panel.
    if (vscode.workspace.getConfiguration("vsblender").get<boolean>("showSetupOnStartup") !== false) {
      void showSetup(context, folder, output, runtimeRoot(context, output), onConfigured);
    }
    return;
  }
  void startWorkspace(context, folder, output);
}

async function ensureAddon(context: vscode.ExtensionContext, folder: string, output: vscode.OutputChannel): Promise<void> {
  const config = readConfig(folder);
  if (!config?.blender || !fs.existsSync(config.blender)) return;
  try {
    const outcome = await installIfStale(config.blender, runtimeRoot(context, output), config.port);
    if (outcome.installed) {
      output.appendLine(`Installed the VSBlender add-on (${ADDON_VERSION}). Restart Blender to load it. ${outcome.message}`);
    }
  } catch (error) {
    output.appendLine(`Add-on install skipped: ${error instanceof Error ? error.message : String(error)}`);
  }
}

/** One notification when a build leaves the file on disk behind. Save uses the same save_file path as the save tool. */
async function offerSave(uri: vscode.Uri): Promise<void> {
  const folder = workspaceRoot();
  if (!folder || !readConfig(folder)) return;
  const key = uri.fsPath;
  let last = "";
  try {
    const lines = fs.readFileSync(uri.fsPath, "utf8").split(/\r?\n/).filter((line) => line.trim());
    last = lines[lines.length - 1] ?? "";
  } catch {
    return;
  }
  if (!last) return;
  let event: Record<string, unknown>;
  try {
    event = JSON.parse(last) as Record<string, unknown>;
  } catch {
    return;
  }
  if (event["event"] === "save") {
    dirtyJournals.delete(key);
    return;
  }
  if (event["event"] !== "script" && event["event"] !== "pipeline") return;
  const runs = Number(event["unsaved_runs"] ?? 0);
  if (!(runs > 0) || dirtyJournals.has(key)) return;
  dirtyJournals.add(key);
  const file = typeof event["blend"] === "string" && event["blend"]
    ? event["blend"]
    : typeof event["file"] === "string" && event["file"] ? event["file"] : "the open file";
  const choice = await vscode.window.showInformationMessage(
    `${runs} runs, the file on disk does not have them (${file})`,
    "Save",
    "Not now"
  );
  if (choice !== "Save") return;
  const config = readConfig(folder);
  if (!config) return;
  try {
    const response = await callBridge(config.port, "save_file", { reason: "save from the editor", actor: "user" }, 60000);
    if (!response.ok) {
      void vscode.window.showErrorMessage(response.error || "Could not save.");
      return;
    }
    dirtyJournals.delete(key);
  } catch (error) {
    void vscode.window.showErrorMessage(error instanceof Error ? error.message : String(error));
  }
}

async function startWorkspace(context: vscode.ExtensionContext, folder: string, output: vscode.OutputChannel): Promise<void> {
  void ensureAddon(context, folder, output);
  await refreshClientFiles(context, folder, output);
  if (!autoIngestOn()) return;
  for (const file of listBlendFiles(folder)) {
    // A "live" sidecar was written on purpose from an unsaved session; re-reading the file would lose it.
    const state = ingestStatus(file);
    if (state === "new" || state === "stale") enqueueIngest(context, folder, output, file, "external", false);
  }
}

async function refreshClientFiles(context: vscode.ExtensionContext, folder: string, output: vscode.OutputChannel): Promise<void> {
  const config = readConfig(folder);
  if (!config?.blender) return;
  const node = await findNode();
  const root = runtimeRoot(context, output);
  try {
    const launch = serverLaunch({
      ...(node ? { nodePath: node } : {}),
      electronPath: process.execPath,
      extensionRoot: root,
      workspace: folder,
      port: config.port,
      blender: config.blender,
    });
    const applied = applyWorkspace({
      workspace: folder,
      extensionRoot: root,
      port: config.port,
      blender: config.blender,
      clients: config.clients,
      replaceLegacy: config.replaceLegacy,
      launch,
      ...(config.allowScripts !== undefined ? { allowScripts: config.allowScripts } : {}),
      ...(config.allowTrustedScripts !== undefined ? { allowTrustedScripts: config.allowTrustedScripts } : {}),
      ...(config.allowSave !== undefined ? { allowSave: config.allowSave } : {}),
      ...(config.ignoreClientConfig !== undefined ? { ignoreClientConfig: config.ignoreClientConfig } : {}),
      ...(config.checkpoints ? { checkpoints: config.checkpoints } : {}),
      ...(config.libPaths ? { libPaths: config.libPaths } : {}),
    });
    // config.json is rewritten on every refresh even when nothing changed; only report real writes.
    const written = applied.written.filter((file) => !file.endsWith("config.json"));
    if (written.length) output.appendLine(`Refreshed ${written.length} VSBlender file(s): ${written.map((file) => relativeTo(folder, file)).join(", ")}`);
    for (const file of applied.skipped) output.appendLine(`Left untouched: ${file}`);
  } catch (error) {
    output.appendLine(error instanceof Error ? error.message : String(error));
  }
}

function scheduleRefresh(context: vscode.ExtensionContext, folder: string, output: vscode.OutputChannel): void {
  if (refreshTimer) clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => {
    refreshTimer = undefined;
    void refreshClientFiles(context, folder, output);
  }, 2000);
}

/** Blender writes a .blend in several steps; wait for the save to settle before ingesting. */
function scheduleIngest(context: vscode.ExtensionContext, folder: string, output: vscode.OutputChannel, file: string): void {
  const existing = pending.get(file);
  if (existing) clearTimeout(existing);
  pending.set(file, setTimeout(() => {
    pending.delete(file);
    enqueueIngest(context, folder, output, file, "external", false);
  }, 1500));
}

/**
 * One ingest at a time: each is a background Blender, and a workspace with many stale files
 * would otherwise start them all at once. A file saved again while it is being ingested is
 * queued once more and runs after.
 */
function enqueueIngest(
  context: vscode.ExtensionContext,
  folder: string,
  output: vscode.OutputChannel,
  file: string,
  actor: string,
  manual: boolean
): void {
  const queued = ingestQueue.find((item) => item.file === file);
  if (queued) {
    queued.manual = queued.manual || manual;
  } else {
    ingestQueue.push({ file, actor, manual });
  }
  void drainIngest(context, folder, output);
}

async function drainIngest(context: vscode.ExtensionContext, folder: string, output: vscode.OutputChannel): Promise<void> {
  if (draining) return;
  draining = true;
  try {
    for (let next = ingestQueue.shift(); next; next = ingestQueue.shift()) {
      await runIngestCommand(context, folder, output, next.file, next.actor, next.manual);
    }
  } finally {
    draining = false;
  }
}

async function runIngestCommand(
  context: vscode.ExtensionContext,
  folder: string,
  output: vscode.OutputChannel,
  target: string,
  actor: string,
  manual: boolean
): Promise<void> {
  const ctx = toolContext(context, output, folder);
  if (!ctx.blender || !fs.existsSync(ctx.blender)) {
    output.appendLine("Skipping ingest: Blender is not configured. Run VSBlender: Setup.");
    if (manual) void vscode.window.showWarningMessage("Blender is not configured. Run VSBlender: Setup first.");
    return;
  }
  if (!fs.existsSync(target)) return;
  const blender = ctx.blender;
  const previews = vscode.workspace.getConfiguration("vsblender").get<boolean>("ingestPreviews") !== false;
  const name = path.basename(target);
  output.appendLine(`Ingest ${relativeTo(folder, target)} (${actor})`);
  const result = await vscode.window.withProgress(
    { location: manual ? vscode.ProgressLocation.Notification : vscode.ProgressLocation.Window, title: `VSBlender: ingesting ${name}` },
    () => runIngest({ blender, blendFile: target, script: ingestScript(ctx.extensionRoot), previews, actor, force: false })
  );
  output.appendLine(`${result.status}${result.error ? `: ${result.error}` : ""}`);
  if (result.status === "error") {
    output.appendLine(result.log);
    void vscode.window.showWarningMessage(`VSBlender ingest failed for ${name}. See the VSBlender log.`);
    return;
  }
  if (manual) {
    const detail = [
      result.objects !== undefined ? `${result.objects} objects` : "",
      result.issues !== undefined ? `${result.issues} issues` : "",
    ].filter(Boolean).join(", ");
    void vscode.window.showInformationMessage(`Ingested ${name}: ${result.status}${detail ? ` (${detail})` : ""}.`);
  }
}

export function deactivate(): void {
  if (timer) clearInterval(timer);
  if (refreshTimer) clearTimeout(refreshTimer);
  for (const handle of pending.values()) clearTimeout(handle);
  pending.clear();
  ingestQueue.length = 0;
}
