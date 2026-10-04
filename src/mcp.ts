import * as fs from "fs";
import { callTool, loadWorkspaceContext } from "./tools";
import { TOOLS } from "./toolDefs";
import { ADDON_VERSION } from "./types";

interface RpcMessage {
  jsonrpc?: string;
  id?: number | string | null;
  method?: string;
  params?: unknown;
}

/** Calls that are still running, so notifications/cancelled can stop them. */
const inFlight = new Map<string, AbortController>();

/** MCP stdio is one JSON-RPC message per line. JSON.stringify never emits a raw newline. */
function writeMessage(payload: unknown): void {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

function resultMessage(id: number | string | null, result: unknown): void {
  writeMessage({ jsonrpc: "2.0", id, result });
}

function errorMessage(id: number | string | null, code: number, message: string): void {
  writeMessage({ jsonrpc: "2.0", id, error: { code, message } });
}

async function handle(message: RpcMessage): Promise<void> {
  const { id, method } = message;
  if (!method) return;
  if (method === "notifications/cancelled") {
    const params = message.params as { requestId?: number | string } | undefined;
    if (params?.requestId !== undefined) inFlight.get(String(params.requestId))?.abort();
    return;
  }
  if (method === "notifications/initialized") return;
  if (id === undefined || id === null) return;
  if (method === "initialize") {
    const params = message.params && typeof message.params === "object" ? (message.params as { protocolVersion?: string }) : {};
    resultMessage(id, {
      protocolVersion: params.protocolVersion || "2024-11-05",
      capabilities: { tools: {} },
      serverInfo: { name: "vsblender", version: ADDON_VERSION },
      instructions:
        "Drive Blender through these tools. Do not read or edit .blend binaries. Call doctor for each file's status, then ingest or context_pack before changing an existing file. Write <blend folder>/scripts/*.py and run_script with a reason; build geometry with vsblender.geo (solids, booleans, threads, holes) and vsblender.material. new_blend starts a project from a template (render, game, print_mm). check_model checks a model for its purpose (general, render, game, print) and export_model writes 3MF/STL for slicers or GLB/FBX/USD/OBJ. preview and render never touch the user's viewport or render settings; lengths are scene units (session_info.units).",
    });
    return;
  }
  if (method === "ping") {
    resultMessage(id, {});
    return;
  }
  if (method === "tools/list") {
    resultMessage(id, { tools: TOOLS });
    return;
  }
  if (method === "tools/call") {
    const params = message.params && typeof message.params === "object"
      ? (message.params as { name?: string; arguments?: Record<string, unknown>; _meta?: { progressToken?: string | number } })
      : {};
    const name = params.name ?? "";
    const args = params.arguments ?? {};
    const token = params._meta?.progressToken;
    const controller = new AbortController();
    inFlight.set(String(id), controller);
    let last = 0;
    const progress = token === undefined ? undefined : (fraction: number | undefined, text: string): void => {
      // MCP wants progress to increase with every notification, also when only the message changed.
      const wanted = fraction === undefined ? last + 0.1 : Math.round(fraction * 1000) / 10;
      last = Math.min(100, Math.max(wanted, last + 0.01));
      writeMessage({
        jsonrpc: "2.0",
        method: "notifications/progress",
        params: { progressToken: token, progress: Math.round(last * 100) / 100, total: 100, message: text },
      });
    };
    try {
      const outcome = await callTool(loadWorkspaceContext(), name, args, {
        signal: controller.signal,
        ...(progress ? { progress } : {}),
      });
      // A cancelled request gets no response.
      if (controller.signal.aborted) return;
      const content: { type: string; text?: string; data?: string; mimeType?: string }[] = [{ type: "text", text: outcome.text }];
      for (const image of outcome.images ?? []) {
        content.push({ type: "image", data: image.data, mimeType: image.mimeType });
      }
      resultMessage(id, { content, isError: !outcome.ok });
    } finally {
      inFlight.delete(String(id));
    }
    return;
  }
  errorMessage(id, -32601, `method not found: ${method}`);
}

function takeMessage<T extends ArrayBufferLike>(buffer: Buffer<T>): { message: RpcMessage | undefined; rest: Buffer<T> } | undefined {
  if (buffer.length === 0) return undefined;
  const first = buffer[0];
  if (first === 0x0a || first === 0x0d || first === 0x20 || first === 0x09) {
    return { message: undefined, rest: buffer.subarray(1) };
  }
  if (first === 0x7b) {
    const newline = buffer.indexOf(0x0a);
    if (newline < 0) return undefined;
    const line = buffer.subarray(0, newline).toString("utf8").replace(/\r$/, "");
    const rest = buffer.subarray(newline + 1);
    try {
      return { message: JSON.parse(line) as RpcMessage, rest };
    } catch (error) {
      process.stderr.write(`vsblender mcp: skipped a line that is not JSON (${error instanceof Error ? error.message : String(error)})\n`);
      return { message: undefined, rest };
    }
  }
  const separator = buffer.indexOf("\r\n\r\n");
  if (separator < 0) return undefined;
  const header = buffer.subarray(0, separator).toString("utf8");
  const match = /Content-Length:\s*(\d+)/i.exec(header);
  const rawLength = match?.[1];
  if (!rawLength) return { message: undefined, rest: buffer.subarray(separator + 4) };
  const length = Number(rawLength);
  const start = separator + 4;
  if (buffer.length < start + length) return undefined;
  const body = buffer.subarray(start, start + length).toString("utf8");
  return { message: JSON.parse(body) as RpcMessage, rest: buffer.subarray(start + length) };
}

export function runServer(): void {
  let buffer: Buffer<ArrayBuffer> = Buffer.alloc(0);
  process.stdin.on("data", (chunk: Buffer) => {
    buffer = Buffer.concat([buffer, chunk]);
    for (;;) {
      let parsed: { message: RpcMessage | undefined; rest: Buffer<ArrayBuffer> } | undefined;
      try {
        parsed = takeMessage(buffer);
      } catch (error) {
        process.stderr.write(`vsblender mcp: ${error instanceof Error ? error.message : String(error)}\n`);
        buffer = Buffer.alloc(0);
        break;
      }
      if (!parsed) break;
      buffer = parsed.rest;
      if (!parsed.message) continue;
      const method = parsed.message.method ?? "";
      if (method && method !== "tools/call") process.stderr.write(`vsblender mcp: ${method}\n`);
      if (method === "tools/call") {
        const params = parsed.message.params as { name?: string } | undefined;
        process.stderr.write(`vsblender mcp: tools/call ${params?.name ?? ""}\n`);
      }
      void handle(parsed.message).catch((error) => {
        const id = parsed?.message?.id;
        if (id !== undefined && id !== null) errorMessage(id, -32603, error instanceof Error ? error.message : String(error));
      });
    }
  });
  process.stdin.on("end", () => process.exit(0));
  process.stdin.resume();
}

function startedAsScript(): boolean {
  try {
    const argv = process.argv[1];
    if (!argv) return false;
    return fs.realpathSync(argv) === fs.realpathSync(__filename);
  } catch {
    return false;
  }
}

if (startedAsScript()) runServer();
