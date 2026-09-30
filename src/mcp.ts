import * as fs from "fs";
import { callTool, loadWorkspaceContext } from "./tools";
import { ADDON_VERSION } from "./types";

interface RpcMessage {
  jsonrpc?: string;
  id?: number | string | null;
  method?: string;
  params?: unknown;
}

const TOOLS = [
  {
    name: "doctor",
    description: "Check Blender, the add-on, client MCP config, and whether the bridge is listening. fix=true rewrites config and reinstalls the add-on.",
    inputSchema: {
      type: "object",
      properties: { fix: { type: "boolean", description: "Repair config and reinstall the add-on." } },
    },
  },
  {
    name: "session_info",
    description: "Blender version, open file, dirty flag, scene, frame, units, engine, and version gotchas. The bridge must be running.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "launch_blender",
    description: "Start Blender with the VSBlender bridge, or open a file in the Blender that is already listening. Does not discard unsaved changes.",
    inputSchema: {
      type: "object",
      properties: {
        file: { type: "string", description: "Workspace .blend to open. Optional when only one .blend exists." },
        background: { type: "boolean", description: "Keep a headless Blender running instead of opening the GUI." },
      },
    },
  },
  {
    name: "ingest",
    description: "Read a .blend headlessly and write .blender-ai/<name>/ notes, manifest, and roles. Does not save the .blend.",
    inputSchema: {
      type: "object",
      properties: {
        path: { type: "string", description: "Workspace .blend. Optional when only one exists." },
        force: { type: "boolean" },
        previews: { type: "boolean", description: "Render overview previews. Default true." },
        reason: { type: "string" },
      },
    },
  },
  {
    name: "context_pack",
    description: "A NOTES.md summary sized to a token budget. Includes Before you modify even when that exceeds the budget. Call ingest first if the file is new.",
    inputSchema: {
      type: "object",
      properties: {
        path: { type: "string" },
        budget_tokens: { type: "number", description: "Approximate token budget. Default 2000." },
        focus: { type: "string", description: "Object or material name to prefer." },
      },
    },
  },
  {
    name: "run_script",
    description: "Run a workspace .py inside the open Blender. Tracebacks use that file and line. The namespace is fresh each call. Set result, or pass function and args.",
    inputSchema: {
      type: "object",
      required: ["path"],
      properties: {
        path: { type: "string" },
        function: { type: "string" },
        args: { type: "object" },
        timeout_ms: { type: "number" },
      },
    },
  },
  {
    name: "preview",
    description: "Offscreen PNG. Does not move the user's viewport, camera, or render settings. view: camera, front, back, left, right, top, bottom, iso. shading: solid, material, rendered.",
    inputSchema: {
      type: "object",
      properties: {
        view: { type: "string" },
        shading: { type: "string" },
        size: { type: "number", description: "Square pixels, 64 to 2048. Default 512." },
        target: { type: "string", description: "Object to frame." },
        frame: { type: "number" },
      },
    },
  },
];

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
  if (method === "notifications/initialized" || method === "notifications/cancelled") return;
  if (id === undefined || id === null) return;
  if (method === "initialize") {
    const params = message.params && typeof message.params === "object" ? (message.params as { protocolVersion?: string }) : {};
    resultMessage(id, {
      protocolVersion: params.protocolVersion || "2024-11-05",
      capabilities: { tools: {} },
      serverInfo: { name: "vsblender", version: ADDON_VERSION },
      instructions:
        "Drive Blender through these tools. Do not read or edit .blend binaries. Ingest or context_pack before changing an existing file, write scripts/*.py, and run_script. preview is offscreen and does not touch the user's viewport.",
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
      ? (message.params as { name?: string; arguments?: Record<string, unknown> })
      : {};
    const name = params.name ?? "";
    const args = params.arguments ?? {};
    const outcome = await callTool(loadWorkspaceContext(), name, args);
    const content: { type: string; text?: string; data?: string; mimeType?: string }[] = [{ type: "text", text: outcome.text }];
    for (const image of outcome.images ?? []) {
      content.push({ type: "image", data: image.data, mimeType: image.mimeType });
    }
    resultMessage(id, { content, isError: !outcome.ok });
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
