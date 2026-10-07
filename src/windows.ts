import * as net from "net";
import * as path from "path";

/** What a listening Blender last reported about the file it has open. */
export interface WindowSnapshot {
  /** The add-on answered ping and did not say which file it has. */
  unknown?: boolean;
  workspace?: string;
  file?: string;
  dirty?: boolean;
  runs?: number;
  /** The listener is headless: there is no window to watch. */
  background?: boolean;
}

export type WindowChoice = "close" | "new";

export interface WindowDecision {
  /** This workspace may use the window that is listening. */
  use: boolean;
  /** Nothing is listening: start Blender on the usual port. */
  start: boolean;
  /** The other window is saved: quit it, then start this folder's file. */
  closeFirst: boolean;
  /** Start a second Blender on a free port and leave the other window as it is. */
  separate: boolean;
  message?: string;
}

const leases = new Map<string, number>();

function clean(value: string): string {
  return path.normalize(value).replace(/[\\/]+$/, "").toLowerCase();
}

export function pathsEqual(a: string, b: string): boolean {
  return clean(a) === clean(b);
}

/** True when `file` is inside `workspace`. */
export function fileInside(workspace: string, file: string): boolean {
  if (!workspace || !file) return false;
  const rel = path.relative(workspace, file);
  return rel !== "" && !rel.startsWith("..") && !path.isAbsolute(rel);
}

/** The port a second window claimed for this workspace, if this process opened one. */
export function leasedPort(workspace: string): number | undefined {
  return leases.get(clean(workspace));
}

export function leasePort(workspace: string, port: number): void {
  leases.set(clean(workspace), port);
}

export function clearLease(workspace: string): void {
  leases.delete(clean(workspace));
}

const idle: WindowDecision = { use: false, start: false, closeFirst: false, separate: false };

/**
 * What to do with the Blender that is already listening.
 * `snap` omitted means nothing is listening.
 * A window whose file is outside this workspace is never switched.
 */
export function decideWindow(ourWorkspace: string, snap: WindowSnapshot | undefined, choice?: WindowChoice): WindowDecision {
  if (!snap) return { ...idle, start: true };
  if (snap.unknown) {
    if (choice === "new") return { ...idle, separate: true };
    return {
      ...idle,
      message: "Blender is listening and did not say which file it has open. The add-on in that window is older than this extension. "
        + "Run doctor_fix and restart it, or launch_blender with window \"new\" to open a second window. This chat will not switch the file that window has open.",
    };
  }
  const file = (snap.file || "").trim();
  const theirs = (snap.workspace || "").trim();
  if (!file && !theirs) {
    if (choice === "new") return { ...idle, separate: true };
    return {
      ...idle,
      message: "Blender is listening and has not reported its file yet. "
        + "launch_blender with window \"new\" opens a second window. This chat will not switch the file that window has open.",
    };
  }
  const ours = file ? fileInside(ourWorkspace, file) : pathsEqual(theirs, ourWorkspace);
  if (ours) return { use: true, start: false, closeFirst: false, separate: false };
  const where = file || theirs;
  const unsaved = Boolean(snap.dirty) || Number(snap.runs || 0) > 0 || !file;
  if (choice === "new") return { ...idle, separate: true };
  if (unsaved) {
    return {
      ...idle,
      message: `Blender already has ${where} open, and that file still has unsaved work. The chat that has it open has to call finish, which saves it. `
        + "This chat can open its own window with launch_blender window \"new\". It will not switch the file that window has open.",
    };
  }
  if (choice === "close") return { ...idle, closeFirst: true };
  return {
    ...idle,
    message: `Blender already has ${where} open, and that file is saved. launch_blender with window \"close\" closes that window and opens this folder. `
      + "window \"new\" opens a second window and leaves the other one as it is.",
  };
}

/** Fields ping carries once the add-on has seen the open file. Missing fields mean an older add-on. */
export function snapshotFromPing(result: Record<string, unknown> | undefined): WindowSnapshot | undefined {
  if (!result || result["product"] !== "vsblender") return undefined;
  if (!("file" in result) && !("workspace" in result)) return { unknown: true };
  const runs = Number(result["unsaved_runs"] ?? 0);
  return {
    workspace: typeof result["workspace"] === "string" ? result["workspace"] : "",
    file: typeof result["file"] === "string" ? result["file"] : "",
    dirty: result["dirty"] === true,
    runs: Number.isFinite(runs) ? runs : 0,
    background: result["background"] === true,
  };
}

export interface SceneWindowPlan {
  /** This workspace already has the Blender the edit should use. */
  ready: boolean;
  /** Nothing is listening: start Blender with a window on the usual port. */
  launch: boolean;
  /** A saved headless Blender is listening: quit it and start a window on the same port. */
  replace: boolean;
  /** Another folder has the port: open a second window and leave that one as it is. */
  separate: boolean;
  /** This workspace's listener is headless and has never been saved: start a second window. */
  beside: boolean;
  /** Open this file in the window that is already ours. */
  switchTo?: string;
  /** The edit cannot run. For a busy saved file this starts with "unsaved changes". */
  message?: string;
}

const noPlan: SceneWindowPlan = { ready: false, launch: false, replace: false, separate: false, beside: false };

/**
 * How to get a Blender window before objects are made or changed.
 * `snap` omitted means nothing is listening.
 * A headless session that still has unsaved work is kept: quitting it would drop those edits.
 * A headless session that has never been saved cannot be quit, so a second window is started beside it.
 */
export function sceneWindowPlan(ourWorkspace: string, snap: WindowSnapshot | undefined, file?: string): SceneWindowPlan {
  if (!snap) return { ...noPlan, launch: true };
  const decision = decideWindow(ourWorkspace, snap);
  if (!decision.use) return { ...noPlan, separate: true };
  const unsaved = Boolean(snap.dirty) || Number(snap.runs || 0) > 0;
  const openFile = (snap.file || "").trim();
  const different = Boolean(file) && (!openFile || !pathsEqual(openFile, String(file)));
  if (different) {
    if (unsaved) {
      return {
        ...noPlan,
        message: `unsaved changes. Blender keeps ${openFile || "the open session"}. Call finish, then open this file.`,
      };
    }
    if (snap.background) return openFile ? { ...noPlan, replace: true } : { ...noPlan, beside: true };
    return { ...noPlan, ready: true, switchTo: file };
  }
  if (snap.background) {
    if (unsaved) return { ...noPlan, ready: true };
    if (!openFile) return { ...noPlan, beside: true };
    return { ...noPlan, replace: true };
  }
  return { ...noPlan, ready: true };
}

/** The first port at or after `start` that this process can bind on 127.0.0.1. */
export function findFreePort(start: number): Promise<number> {
  const tryOne = (port: number): Promise<boolean> => new Promise((resolve) => {
    const server = net.createServer();
    server.once("error", () => resolve(false));
    server.listen(port, "127.0.0.1", () => {
      server.close(() => resolve(true));
    });
  });
  return (async () => {
    const first = Math.max(1024, Math.min(65535, start));
    for (let port = first; port <= Math.min(65535, first + 40); port += 1) {
      if (await tryOne(port)) return port;
    }
    throw new Error(`no free port from ${first} to ${Math.min(65535, first + 40)}`);
  })();
}

/** Tools that change the Blender window, or read it as this workspace's session. */
export function needsOurWindow(name: string, args: Record<string, unknown>): boolean {
  if (name === "run_script" && args["background"] === true) return false;
  return name === "run_script"
    || name === "run_pipeline"
    || name === "run_project_script"
    || name === "run_project_pipeline"
    || name === "open_blend"
    || name === "replay"
    || name === "save"
    || name === "finish"
    || name === "restore_checkpoint"
    || name === "append"
    || name === "checkpoint"
    || name === "set_role";
}
