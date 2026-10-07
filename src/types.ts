import type { PrinterSetting } from "./printers";

export interface ClientFlags {
  claude: boolean;
  vscode: boolean;
  grok: boolean;
  cursor: boolean;
  cline: boolean;
}

/** Cursor and Cline stay off until the user turns them on. */
export const DEFAULT_CLIENTS: ClientFlags = {
  claude: true,
  vscode: true,
  grok: true,
  cursor: false,
  cline: false,
};

export interface CheckpointSettings {
  /** Checkpoint before each run_script; dropped again when the script changed nothing. */
  auto: boolean;
  /** Automatic checkpoints kept per .blend. Labelled ones are kept until removed by hand. */
  keep: number;
  /** Files larger than this on disk get no automatic checkpoint. */
  maxMb: number;
  /** Disk budget for the automatic checkpoints of one .blend: the oldest go first when it is exceeded. */
  budgetMb: number;
}

export const DEFAULT_CHECKPOINTS: CheckpointSettings = { auto: true, keep: 10, maxMb: 300, budgetMb: 500 };

export interface ProjectConfig {
  version: 1;
  port: number;
  blender?: string;
  clients: ClientFlags;
  replaceLegacy: boolean;
  /**
   * Personal copy of the run_script allow rule in .claude/settings.local.json.
   * The shared .claude/settings.json already allows the build tools. false removes only that
   * personal copy. undefined: setup never asked, so an existing rule is left as it is.
   */
  allowScripts?: boolean;
  /** Add the client MCP files to .gitignore: they hold this machine's paths. */
  ignoreClientConfig?: boolean;
  checkpoints?: CheckpointSettings;
  /** Workspace folders on sys.path during run_script. */
  libPaths?: string[];
  /** 3D printer for purpose "print": a preset name or an object of overrides (src/printers.ts). */
  printer?: PrinterSetting;
  /** Workspace folders whose scripts run_project_script accepts. */
  trustedScripts?: string[];
  /** Personal copy of the run_project_script allow rule. The shared list already allows it. */
  allowTrustedScripts?: boolean;
  /**
   * The user lets the AI save the open .blend: the save tool runs without asking, and the script
   * tools accept save: true. Off unless ticked in setup.
   */
  allowSave?: boolean;
  /** Folders outside the workspace that measure, preview overlays and import_reference may read. */
  referenceRoots?: string[];
  /** Another project's script folders that every script can import from (reused builders). */
  sharedLibs?: string[];
  /** ffmpeg for render videos and reference videos. Found on PATH or in the usual places when unset. */
  ffmpeg?: string;
}

export interface WorkspaceContext {
  workspace: string;
  extensionRoot: string;
  port: number;
  blender?: string;
  clients: ClientFlags;
  replaceLegacy: boolean;
  checkpoints: CheckpointSettings;
  libPaths: string[];
  printer?: PrinterSetting;
  trustedScripts?: string[];
  allowSave?: boolean;
  referenceRoots?: string[];
  sharedLibs?: string[];
  ffmpeg?: string;
}

export interface ServerLaunch {
  command: string;
  args: string[];
  env: Record<string, string>;
  via: "node" | "electron";
}

export interface ToolOutcome {
  ok: boolean;
  text: string;
  details?: unknown;
  images?: { mimeType: string; data: string }[];
}

/** What a long tool call can do while it runs: report progress, and notice a cancel. */
export interface CallExtras {
  signal?: AbortSignal;
  progress?: (fraction: number | undefined, message: string) => void;
}

export const DEFAULT_PORT = 47876;
export const SERVER_NAME = "vsblender";
export const ADDON_VERSION = "0.7.0";
export const GUIDE_STAMP = "<!-- vsblender-guide";
export const DEFAULT_LIB_PATHS = ["scripts/lib"];

export function normalizeClients(value: unknown): ClientFlags {
  const record = value && typeof value === "object" ? (value as Partial<ClientFlags>) : {};
  const flag = (name: keyof ClientFlags): boolean =>
    typeof record[name] === "boolean" ? Boolean(record[name]) : DEFAULT_CLIENTS[name];
  return {
    claude: flag("claude"),
    vscode: flag("vscode"),
    grok: flag("grok"),
    cursor: flag("cursor"),
    cline: flag("cline"),
  };
}

export function normalizeCheckpoints(value: unknown): CheckpointSettings {
  const record = value && typeof value === "object" ? (value as Partial<CheckpointSettings>) : {};
  const keep = Number(record.keep);
  const maxMb = Number(record.maxMb);
  const budgetMb = Number(record.budgetMb ?? (record as Record<string, unknown>)["maxTotalMb"]);
  return {
    auto: typeof record.auto === "boolean" ? record.auto : DEFAULT_CHECKPOINTS.auto,
    keep: Number.isInteger(keep) && keep >= 1 && keep <= 1000 ? keep : DEFAULT_CHECKPOINTS.keep,
    maxMb: Number.isFinite(maxMb) && maxMb > 0 ? maxMb : DEFAULT_CHECKPOINTS.maxMb,
    budgetMb: Number.isFinite(budgetMb) && budgetMb > 0 ? budgetMb : DEFAULT_CHECKPOINTS.budgetMb,
  };
}

export function clampPort(value: unknown, fallback = DEFAULT_PORT): number {
  const port = typeof value === "number" ? value : Number(value);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) return fallback;
  return port;
}
