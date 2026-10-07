import * as net from "net";
import { snapshotFromPing, WindowSnapshot } from "./windows";

/** What run_script changed, by category. modified[category][name] lists the aspects that changed. */
export interface ChangeReport {
  added: string[];
  removed: string[];
  recreated: string[];
  renamed: { from: string; to: string }[];
  modified: Record<string, Record<string, string[]>>;
  /** Removed or rebuilt datablocks that had animation (keys, drivers, NLA), and removed actions. */
  lost_animation?: string[];
  /** Built and removed again within the run (an import that was measured and deleted). Not a change. */
  temporary?: string[];
}

export interface BridgeResponse {
  id?: number;
  ok: boolean;
  result?: Record<string, unknown>;
  error?: string;
  warnings?: string[];
  changed?: string[];
  changes?: ChangeReport;
  ms?: number;
}

export function callBridge(
  port: number,
  method: string,
  params: Record<string, unknown>,
  timeoutMs: number
): Promise<BridgeResponse> {
  return new Promise((resolve, reject) => {
    const socket = net.connect({ host: "127.0.0.1", port });
    // Decodes across chunk boundaries, so a multi-byte character split between two reads survives.
    socket.setEncoding("utf8");
    let buffer = "";
    let settled = false;
    const finish = (fn: () => void): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      socket.destroy();
      fn();
    };
    const timer = setTimeout(() => {
      finish(() => reject(new Error(`bridge timed out after ${timeoutMs}ms`)));
    }, timeoutMs);
    socket.on("error", (error) => finish(() => reject(error)));
    socket.on("close", () => finish(() => reject(new Error("the bridge closed the connection without an answer"))));
    socket.on("data", (chunk: string) => {
      buffer += chunk;
      const newline = buffer.indexOf("\n");
      if (newline < 0) return;
      const line = buffer.slice(0, newline);
      try {
        const parsed = JSON.parse(line) as BridgeResponse;
        finish(() => resolve(parsed));
      } catch (error) {
        finish(() => reject(new Error(`the bridge sent a reply that is not JSON: ${error instanceof Error ? error.message : String(error)}`)));
      }
    });
    socket.on("connect", () => {
      socket.write(`${JSON.stringify({ id: 1, method, params })}\n`);
    });
  });
}

/**
 * absent: nothing accepted the connection, so it is safe to start Blender.
 * unresponsive: something accepted but did not answer. Never start a second Blender on it.
 * foreign: another program owns the port.
 */
export type ProbeState = "listening" | "absent" | "unresponsive" | "foreign" | "error";

export interface ScriptProgress {
  label?: string;
  fraction?: number | null;
  message?: string;
  seconds?: number;
}

export interface Probe {
  ok: boolean;
  state: ProbeState;
  detail: string;
  version?: string;
  busy?: string;
  progress?: ScriptProgress;
  /** The file that window has open, when the add-on reported it. */
  window?: WindowSnapshot;
}

export async function probeBridge(port: number): Promise<Probe> {
  try {
    const response = await callBridge(port, "ping", {}, 1500);
    if (response.ok && response.result?.["product"] === "vsblender") {
      const version = String(response.result["version"] ?? "");
      const busy = String(response.result["busy"] ?? "");
      const progress = response.result["progress"] as ScriptProgress | undefined;
      const hasProgress = Boolean(progress && Object.keys(progress).length);
      const share = hasProgress && typeof progress?.fraction === "number" ? ` ${Math.round(progress.fraction * 100)}%` : "";
      const window = snapshotFromPing(response.result) ?? { unknown: true };
      return {
        ok: true,
        state: "listening",
        detail: `listening on ${port}${busy ? ` (busy: ${busy}${share}${hasProgress && progress?.message ? `, ${progress.message}` : ""})` : ""}`,
        version,
        window,
        ...(busy ? { busy } : {}),
        ...(hasProgress && progress ? { progress } : {}),
      };
    }
    if (response.ok) return { ok: false, state: "foreign", detail: `port ${port} answered, but it is not the VSBlender bridge` };
    return { ok: false, state: "error", detail: response.error || `port ${port} refused the ping` };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (/ECONNREFUSED/i.test(message)) return { ok: false, state: "absent", detail: `nothing is listening on ${port}` };
    if (/timed out|without an answer/i.test(message)) {
      return { ok: false, state: "unresponsive", detail: `something on port ${port} accepted the connection but did not answer the ping` };
    }
    return { ok: false, state: "error", detail: message };
  }
}

/** Ask a running script to stop at its next vsblender.progress() call. Answered while Blender is busy. */
export async function cancelScript(port: number): Promise<boolean> {
  try {
    const response = await callBridge(port, "cancel", {}, 1500);
    return response.ok && response.result?.["cancelled"] === true;
  } catch {
    return false;
  }
}

/** One line per category, for tool replies and the journal. */
export function formatChanges(report: ChangeReport | undefined, limit = 12): string[] {
  if (!report) return [];
  const lines: string[] = [];
  const list = (items: string[]): string =>
    items.slice(0, limit).join(", ") + (items.length > limit ? ` (+${items.length - limit} more)` : "");
  if (report.added?.length) lines.push(`added: ${list(report.added)}`);
  if (report.removed?.length) lines.push(`removed: ${list(report.removed)}`);
  if (report.recreated?.length) lines.push(`recreated (deleted and built again under the same name): ${list(report.recreated)}`);
  if (report.renamed?.length) lines.push(`renamed: ${list(report.renamed.map((r) => `${r.from} -> ${r.to}`))}`);
  if (report.lost_animation?.length) lines.push(`WARNING lost animation (removed or rebuilt without its keys): ${list(report.lost_animation)}`);
  for (const [category, items] of Object.entries(report.modified ?? {})) {
    const names = Object.keys(items);
    if (category === "scenes") {
      for (const name of names) {
        const paths = items[name] ?? [];
        lines.push(`scene ${name}: ${paths.slice(0, 20).join("; ")}${paths.length > 20 ? ` (+${paths.length - 20} more)` : ""}`);
      }
      continue;
    }
    const shown = names.slice(0, limit).map((name) => `${name} (${(items[name] ?? []).slice(0, 4).join(", ")})`);
    lines.push(`modified ${category}: ${shown.join(", ")}${names.length > limit ? ` (+${names.length - limit} more)` : ""}`);
  }
  if (report.temporary?.length) lines.push(`temporary (added and removed again, not a change): ${list(report.temporary)}`);
  return lines;
}
