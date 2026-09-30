import * as net from "net";

export interface BridgeResponse {
  id?: number;
  ok: boolean;
  result?: Record<string, unknown>;
  error?: string;
  warnings?: string[];
  changed?: string[];
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

export interface Probe {
  ok: boolean;
  state: ProbeState;
  detail: string;
  version?: string;
  busy?: string;
}

export async function probeBridge(port: number): Promise<Probe> {
  try {
    const response = await callBridge(port, "ping", {}, 1500);
    if (response.ok && response.result?.["product"] === "vsblender") {
      const version = String(response.result["version"] ?? "");
      const busy = String(response.result["busy"] ?? "");
      return {
        ok: true,
        state: "listening",
        detail: `listening on ${port}${busy ? ` (busy: ${busy})` : ""}`,
        version,
        ...(busy ? { busy } : {}),
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
