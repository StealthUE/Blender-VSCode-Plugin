import * as fs from "fs";
import * as path from "path";
import { execFileText } from "./exec";

export interface IngestRequest {
  blender: string;
  blendFile: string;
  script: string;
  force?: boolean;
  previews?: boolean;
  actor?: string;
  reason?: string;
  timeoutMs?: number;
}

export interface IngestResult {
  status: string;
  out?: string;
  objects?: number;
  changes?: number;
  issues?: number;
  previews?: number;
  seconds?: number;
  error?: string;
  log: string;
}

/**
 * Always the copy shipped with the extension. Auto-ingest runs when a folder opens, so a
 * blender_ingest.py taken from the workspace would let any cloned repo run its own Python.
 * It lives inside the add-on package, so the add-on can also ingest the live session.
 */
export function ingestScript(extensionRoot: string): string {
  return path.join(extensionRoot, "resources", "addon", "vsblender_bridge", "blender_ingest.py");
}

/** Write the manifest of a .blend (a checkpoint, say) without touching its sidecar. */
export async function manifestHeadless(blender: string, script: string, blendFile: string, out: string): Promise<string> {
  const result = await execFileText(blender, [
    "-b", "--factory-startup", "--disable-autoexec", blendFile, "--python-exit-code", "1", "--python", script,
    "--", "--manifest-to", out,
  ], { timeout: 300000 });
  const parsed = parseIngestResult(`${result.stdout}\n${result.stderr}`);
  if (!parsed || parsed["status"] !== "manifest") {
    throw new Error(`could not build the manifest of ${blendFile}: ${String(parsed?.["error"] ?? `${result.stdout}${result.stderr}`.slice(-800))}`);
  }
  return out;
}

/** Compare two manifest files with the ingester's diff, in a Blender that opens no file. */
export async function diffHeadless(blender: string, script: string, a: string, b: string, out: string): Promise<unknown[]> {
  const result = await execFileText(blender, [
    "-b", "--factory-startup", "--python-exit-code", "1", "--python", script, "--", "--diff", a, b, "--diff-to", out,
  ], { timeout: 120000 });
  const parsed = parseIngestResult(`${result.stdout}\n${result.stderr}`);
  if (!parsed || parsed["status"] !== "diff") {
    throw new Error(`diff failed: ${String(parsed?.["error"] ?? `${result.stdout}${result.stderr}`.slice(-800))}`);
  }
  const data: unknown = JSON.parse(fs.readFileSync(out, "utf8"));
  return Array.isArray(data) ? data : [];
}

export function parseIngestResult(output: string): Record<string, unknown> | undefined {
  const lines = output.split(/\r?\n/).map((line) => line.trim()).filter((line) => line.startsWith("INGEST_RESULT "));
  const last = lines[lines.length - 1];
  if (!last) return undefined;
  try {
    const parsed: unknown = JSON.parse(last.slice("INGEST_RESULT ".length));
    return parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : undefined;
  } catch {
    return undefined;
  }
}

export async function runIngest(request: IngestRequest): Promise<IngestResult> {
  const args = [
    "-b",
    "--factory-startup",
    "--disable-autoexec",
    request.blendFile,
    "--python-exit-code",
    "1",
    "--python",
    request.script,
    "--",
  ];
  if (request.force) args.push("--force");
  if (request.previews === false) args.push("--no-previews");
  if (request.actor) args.push("--actor", request.actor);
  if (request.reason) args.push("--reason", request.reason);
  const result = await execFileText(request.blender, args, { timeout: request.timeoutMs ?? 600000 });
  const log = `${result.stdout}\n${result.stderr}`.trim();
  const parsed = parseIngestResult(`${result.stdout}\n${result.stderr}`);
  if (!parsed) {
    return {
      status: "error",
      error: result.code === 124 ? "ingest timed out" : "Blender did not print INGEST_RESULT",
      log: log.slice(-4000),
    };
  }
  return {
    status: String(parsed["status"] ?? "error"),
    out: typeof parsed["out"] === "string" ? parsed["out"] : undefined,
    objects: typeof parsed["objects"] === "number" ? parsed["objects"] : undefined,
    changes: typeof parsed["changes"] === "number" ? parsed["changes"] : undefined,
    issues: typeof parsed["issues"] === "number" ? parsed["issues"] : undefined,
    previews: typeof parsed["previews"] === "number" ? parsed["previews"] : undefined,
    seconds: typeof parsed["seconds"] === "number" ? parsed["seconds"] : undefined,
    error: typeof parsed["error"] === "string" ? parsed["error"] : undefined,
    log: log.slice(-2000),
  };
}
