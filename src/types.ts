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

export interface ProjectConfig {
  version: 1;
  port: number;
  blender?: string;
  clients: ClientFlags;
  replaceLegacy: boolean;
}

export interface WorkspaceContext {
  workspace: string;
  extensionRoot: string;
  port: number;
  blender?: string;
  clients: ClientFlags;
  replaceLegacy: boolean;
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

export const DEFAULT_PORT = 47876;
export const SERVER_NAME = "vsblender";
export const ADDON_VERSION = "0.1.0";
export const GUIDE_STAMP = "<!-- vsblender-guide";

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

export function clampPort(value: unknown, fallback = DEFAULT_PORT): number {
  const port = typeof value === "number" ? value : Number(value);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) return fallback;
  return port;
}
