import { ChildProcess, spawn } from "child_process";
import * as fs from "fs";
import * as path from "path";
import { WorkspaceContext } from "./types";

/**
 * Background Blender jobs: final renders, and previews or sheets made outside the user's Blender.
 * Each job is a headless Blender running resources/job.py on a copy of the scene, so the user's
 * Blender stays usable and render overrides never reach their file.
 */
export type JobState = "running" | "done" | "failed" | "cancelled";

export interface JobProgress {
  done: number;
  total: number;
  frame?: number;
  samples?: [number, number];
}

export interface Job {
  id: string;
  kind: string;
  state: JobState;
  started: number;
  finished?: number;
  dir: string;
  progress: JobProgress;
  result?: Record<string, unknown>;
  error?: string;
  log: string[];
  child?: ChildProcess;
  /** Deleted when the job ends: the scene copy made for it. */
  scratch?: string;
  label: string;
}

const jobs = new Map<string, Job>();

export function jobScript(extensionRoot: string): string {
  return path.join(extensionRoot, "resources", "job.py");
}

export function addonSource(extensionRoot: string): string {
  return path.join(extensionRoot, "resources", "addon", "vsblender_bridge");
}

export function newJobId(prefix: string): string {
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace("T", "-").slice(0, 15);
  let id = `${prefix}-${stamp}`;
  for (let n = 2; jobs.has(id); n += 1) id = `${prefix}-${stamp}-${n}`;
  return id;
}

export function jobsDir(workspace: string): string {
  return path.join(workspace, ".blender-ai", "jobs");
}

/** Read Blender's own render lines for progress inside a frame: "Rendering 12 / 64 samples", "Sample 12/128". */
export function parseSamples(line: string): [number, number] | undefined {
  const match = /(?:Rendering|Sample)\s+(\d+)\s*\/\s*(\d+)/i.exec(line);
  if (!match) return undefined;
  return [Number(match[1]), Number(match[2])];
}

export function parseJobLine(job: Job, line: string): void {
  if (line.startsWith("VSB_PROGRESS ")) {
    try {
      const data = JSON.parse(line.slice("VSB_PROGRESS ".length)) as Partial<JobProgress>;
      job.progress = { ...job.progress, done: Number(data.done ?? 0), total: Number(data.total ?? 1) };
      if (data.frame !== undefined) job.progress.frame = Number(data.frame);
      delete job.progress.samples;
    } catch {
      // A malformed progress line only costs a progress update.
    }
    return;
  }
  if (line.startsWith("VSB_RESULT ")) {
    try {
      job.result = JSON.parse(line.slice("VSB_RESULT ".length)) as Record<string, unknown>;
    } catch (error) {
      job.error = `unreadable result: ${error instanceof Error ? error.message : String(error)}`;
    }
    return;
  }
  const samples = parseSamples(line);
  if (samples) job.progress.samples = samples;
}

export function fraction(job: Job): number {
  const { done, total, samples } = job.progress;
  if (job.state === "done") return 1;
  const within = samples && samples[1] ? samples[0] / samples[1] : 0;
  return total ? Math.min(1, (done + within) / total) : 0;
}

export interface StartOptions {
  blend?: string;
  spec: Record<string, unknown>;
  kind: string;
  label: string;
  scratch?: string;
  autoexec?: boolean;
  factoryStartup?: boolean;
}

export function startJob(ctx: WorkspaceContext, id: string, options: StartOptions): Job {
  if (!ctx.blender || !fs.existsSync(ctx.blender)) throw new Error("No Blender executable is configured. Run VSBlender: Setup.");
  const dir = path.join(jobsDir(ctx.workspace), id);
  fs.mkdirSync(dir, { recursive: true });
  const specPath = path.join(dir, "spec.json");
  fs.writeFileSync(specPath, JSON.stringify(options.spec, null, 2), "utf8");
  const args = ["-b"];
  // Render jobs keep the user's preferences: --factory-startup would drop the Cycles GPU setting.
  if (options.factoryStartup) args.push("--factory-startup");
  if (!options.autoexec) args.push("--disable-autoexec");
  if (options.blend) args.push(options.blend);
  args.push("--python", jobScript(ctx.extensionRoot), "--", specPath);
  const job: Job = {
    id,
    kind: options.kind,
    state: "running",
    started: Date.now(),
    dir,
    progress: { done: 0, total: 1 },
    log: [],
    label: options.label,
    ...(options.scratch ? { scratch: options.scratch } : {}),
  };
  const child = spawn(ctx.blender, args, {
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, VSBLENDER_ADDON_SRC: addonSource(ctx.extensionRoot), VSBLENDER_WORKSPACE: ctx.workspace },
  });
  job.child = child;
  jobs.set(id, job);
  let pending = "";
  const onData = (chunk: Buffer): void => {
    pending += chunk.toString("utf8");
    let newline = pending.indexOf("\n");
    while (newline >= 0) {
      const line = pending.slice(0, newline).replace(/\r$/, "");
      pending = pending.slice(newline + 1);
      parseJobLine(job, line);
      job.log.push(line);
      if (job.log.length > 400) job.log.splice(0, job.log.length - 400);
      newline = pending.indexOf("\n");
    }
  };
  child.stdout?.on("data", onData);
  child.stderr?.on("data", onData);
  child.on("error", (error) => {
    job.state = "failed";
    job.error = error.message;
    finish(job);
  });
  child.on("exit", (code) => {
    if (pending.trim()) parseJobLine(job, pending.trim());
    if (job.state === "running") {
      const status = job.result?.["status"];
      job.state = status === "done" && code === 0 ? "done" : "failed";
      if (job.state === "failed" && !job.error) {
        job.error = String(job.result?.["error"] ?? `Blender exited with code ${code}`);
      }
    }
    finish(job);
  });
  return job;
}

function finish(job: Job): void {
  job.finished = job.finished ?? Date.now();
  delete job.child;
  if (job.scratch) {
    try {
      fs.rmSync(job.scratch, { force: true });
    } catch {
      // A copy left behind only costs disk space; the jobs folder is git-ignored.
    }
  }
  try {
    const { child: _child, ...rest } = job;
    fs.writeFileSync(path.join(job.dir, "job.json"), JSON.stringify({ ...rest, log: job.log.slice(-60) }, null, 2), "utf8");
  } catch {
    // The status is still in memory.
  }
}

export function getJob(id: string): Job | undefined {
  return jobs.get(id);
}

export function allJobs(): Job[] {
  return [...jobs.values()].sort((a, b) => a.started - b.started);
}

export function cancel(id: string): boolean {
  const job = jobs.get(id);
  if (!job || job.state !== "running") return false;
  job.state = "cancelled";
  job.child?.kill();
  return true;
}

/** Resolves when the job ends, or after timeoutMs with the job still running. */
export function waitFor(job: Job, timeoutMs: number, signal?: AbortSignal): Promise<Job> {
  return new Promise((resolve) => {
    if (job.state !== "running" || timeoutMs <= 0) {
      resolve(job);
      return;
    }
    const started = Date.now();
    const timer = setInterval(() => {
      if (job.state !== "running" || Date.now() - started >= timeoutMs || signal?.aborted) {
        clearInterval(timer);
        resolve(job);
      }
    }, 250);
  });
}

/** Run a short job to completion and return its result, for previews of checkpoints and compositing. */
export async function runToEnd(ctx: WorkspaceContext, options: StartOptions, timeoutMs: number, signal?: AbortSignal): Promise<Record<string, unknown>> {
  const id = newJobId(options.kind);
  const job = startJob(ctx, id, options);
  await waitFor(job, timeoutMs, signal);
  if (job.state === "running") {
    cancel(id);
    throw new Error(`${options.label} did not finish within ${Math.round(timeoutMs / 1000)}s`);
  }
  if (job.state !== "done" || !job.result) {
    const tail = job.log.slice(-12).join("\n");
    throw new Error(`${options.label} failed: ${job.error ?? "no result"}\n${tail}`.trim());
  }
  jobs.delete(id);
  try {
    fs.rmSync(job.dir, { recursive: true, force: true });
  } catch {
    // Left in the git-ignored jobs folder.
  }
  return job.result;
}

export function describeJob(job: Job, workspace: string): string {
  const rel = (file: unknown): string => path.relative(workspace, String(file)).split(path.sep).join("/");
  const seconds = Math.round(((job.finished ?? Date.now()) - job.started) / 1000);
  const lines = [`${job.id}: ${job.state} (${job.label}, ${seconds}s)`];
  if (job.state === "running") {
    const { done, total, frame, samples } = job.progress;
    lines.push(`progress: ${Math.round(fraction(job) * 100)}% - frame ${Math.min(done + 1, total)} of ${total}`
      + (frame !== undefined ? ` (frame ${frame})` : "") + (samples ? `, sample ${samples[0]}/${samples[1]}` : ""));
  }
  const files = job.result?.["files"];
  if (Array.isArray(files) && files.length) {
    lines.push(`output: ${files.slice(0, 6).map(rel).join(", ")}${files.length > 6 ? ` (+${files.length - 6} more)` : ""}`);
  }
  if (job.result?.["resolution"]) lines.push(`resolution: ${JSON.stringify(job.result["resolution"])}, engine ${String(job.result["engine"] ?? "")}`);
  const warnings = job.result?.["warnings"];
  if (Array.isArray(warnings) && warnings.length) lines.push(`warnings: ${warnings.join("; ")}`);
  if (job.error) lines.push(`error: ${job.error}`);
  if (job.state === "failed") {
    const trace = String(job.result?.["trace"] ?? "");
    lines.push((trace || job.log.slice(-15).join("\n")).trim());
  }
  return lines.join("\n");
}
