import * as crypto from "crypto";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { probeBridge } from "./bridge";
import { listBlendFiles, relativeTo, resolveInside } from "./blendFiles";
import { execFileText } from "./exec";
import { CallExtras, ToolOutcome, WorkspaceContext } from "./types";

/**
 * Python and shell runs outside Blender: checks, sizing tables, file conversions. They run without a
 * permission prompt, like run_script, which already runs any Python with the same access. Scripts
 * that build the model live in <blend folder>/scripts/ and run in Blender; a check is passed inline
 * as code and kept in a temp folder outside the project, so the two never mix.
 */

const OUTPUT_LIMIT = 20000;
const KEEP_CHECKS = 50;
const DEFAULT_TIMEOUT = 120000;
const MAX_TIMEOUT = 600000;

export type LocalKind = "python" | "shell";

export interface Interpreter {
  path: string;
  /** Where it was found: config, environment, PATH, Blender (its bundled Python), Git (Git for Windows). */
  source: string;
}

function isFile(file: string | undefined): file is string {
  if (!file) return false;
  try {
    return fs.statSync(file).isFile();
  } catch {
    return false;
  }
}

function isDir(dir: string): boolean {
  try {
    return fs.statSync(dir).isDirectory();
  } catch {
    return false;
  }
}

function pathDirs(env: NodeJS.ProcessEnv): string[] {
  return (env["PATH"] ?? env["Path"] ?? "").split(path.delimiter).map((dir) => dir.replace(/^"|"$/g, "")).filter(Boolean);
}

function onPath(names: string[], env: NodeJS.ProcessEnv, skip?: (dir: string) => boolean): string | undefined {
  for (const dir of pathDirs(env)) {
    if (skip?.(dir)) continue;
    for (const name of names) {
      const file = path.join(dir, name);
      if (isFile(file)) return file;
    }
  }
  return undefined;
}

/** A configured path can name the program or its folder (python/, python/bin/). */
function configuredExe(candidate: string | undefined, names: string[]): string | undefined {
  if (!candidate) return undefined;
  if (isFile(candidate)) return candidate;
  for (const dir of [candidate, path.join(candidate, "bin")]) {
    for (const name of names) {
      if (isFile(path.join(dir, name))) return path.join(dir, name);
    }
  }
  return undefined;
}

/** Blender's own Python: <blender dir>/<x.y>/python/bin (Windows, Linux) or Contents/Resources/<x.y>/python/bin (macOS). */
export function blenderPython(blender: string | undefined): string | undefined {
  if (!blender) return undefined;
  const dir = path.dirname(blender);
  const bases = [dir, path.join(dir, "..", "Resources")];
  for (const base of bases) {
    let versions: string[] = [];
    try {
      versions = fs.readdirSync(base).filter((name) => /^\d+\.\d+$/.test(name) && isDir(path.join(base, name)));
    } catch {
      continue;
    }
    versions.sort((a, b) => Number(b.split(".")[0]) - Number(a.split(".")[0]) || Number(b.split(".")[1]) - Number(a.split(".")[1]));
    for (const version of versions) {
      const bin = path.join(base, version, "python", "bin");
      if (isFile(path.join(bin, "python.exe"))) return path.join(bin, "python.exe");
      let names: string[] = [];
      try {
        names = fs.readdirSync(bin).filter((name) => /^python3(\.\d+)?$/.test(name)).sort().reverse();
      } catch {
        continue;
      }
      // python3.11 before python3: the versioned name is the real program.
      const file = names.map((name) => path.join(bin, name)).find(isFile);
      if (file) return file;
    }
  }
  return undefined;
}

/**
 * "python" in .blender-ai/config.json, VSBLENDER_PYTHON, a real Python on PATH (not the Windows
 * Store stub), then the Python that ships with Blender, which is always there and has numpy.
 */
export function findPython(ctx: Pick<WorkspaceContext, "python" | "blender">, env: NodeJS.ProcessEnv = process.env): Interpreter | undefined {
  const win = process.platform === "win32";
  const names = win ? ["python.exe", "python3.exe"] : ["python3", "python"];
  const configured = configuredExe(ctx.python, names);
  if (configured) return { path: configured, source: "config" };
  const fromEnv = configuredExe(env["VSBLENDER_PYTHON"], names);
  if (fromEnv) return { path: fromEnv, source: "VSBLENDER_PYTHON" };
  const found = onPath(names, env, (dir) => /WindowsApps/i.test(dir));
  if (found) return { path: found, source: "PATH" };
  const bundled = blenderPython(ctx.blender);
  if (bundled) return { path: bundled, source: "Blender" };
  return undefined;
}

/**
 * "bash" in config, VSBLENDER_BASH, then on Windows Git for Windows' bash. Never System32\bash.exe:
 * that is the WSL launcher, which runs in Linux and sees other paths.
 */
export function findBash(ctx: Pick<WorkspaceContext, "bash">, env: NodeJS.ProcessEnv = process.env): Interpreter | undefined {
  const win = process.platform === "win32";
  const names = win ? ["bash.exe"] : ["bash"];
  const configured = configuredExe(ctx.bash, names);
  if (configured) return { path: configured, source: "config" };
  const fromEnv = configuredExe(env["VSBLENDER_BASH"], names);
  if (fromEnv) return { path: fromEnv, source: "VSBLENDER_BASH" };
  if (!win) {
    const found = onPath(names, env) ?? ["/bin/bash", "/usr/bin/bash"].find(isFile);
    return found ? { path: found, source: "PATH" } : undefined;
  }
  const roots = [env["ProgramFiles"], env["ProgramW6432"], env["ProgramFiles(x86)"], env["LOCALAPPDATA"] && path.join(env["LOCALAPPDATA"], "Programs")]
    .filter((root): root is string => Boolean(root));
  for (const root of roots) {
    const file = path.join(root, "Git", "bin", "bash.exe");
    if (isFile(file)) return { path: file, source: "Git" };
  }
  // git.exe is in Git\cmd, Git\bin or Git\mingw64\bin: bash.exe is in Git\bin.
  const git = onPath(["git.exe"], env);
  if (git) {
    for (const up of ["..", path.join("..", "..")]) {
      const file = path.join(path.dirname(git), up, "bin", "bash.exe");
      if (isFile(file)) return { path: path.resolve(file), source: "Git" };
    }
  }
  const found = onPath(names, env, (dir) => /[\\/](System32|SysWOW64|WindowsApps)([\\/]|$)/i.test(dir));
  return found ? { path: found, source: "PATH" } : undefined;
}

export function findPowerShell(env: NodeJS.ProcessEnv = process.env): Interpreter | undefined {
  const win = process.platform === "win32";
  const pwsh = onPath([win ? "pwsh.exe" : "pwsh"], env);
  if (pwsh) return { path: pwsh, source: "PATH" };
  if (!win) return undefined;
  const found = onPath(["powershell.exe"], env)
    ?? [path.join(env["SystemRoot"] ?? "C:\\Windows", "System32", "WindowsPowerShell", "v1.0", "powershell.exe")].find(isFile);
  return found ? { path: found, source: "Windows" } : undefined;
}

/** The temp folder that keeps inline checks, outside the project: <temp>/vsblender/<workspace>-<hash>/. */
export function checksDir(workspace: string): string {
  const resolved = path.resolve(workspace);
  const key = process.platform === "win32" ? resolved.toLowerCase() : resolved;
  const hash = crypto.createHash("sha1").update(key).digest("hex").slice(0, 8);
  const name = path.basename(resolved).replace(/[^A-Za-z0-9._-]+/g, "_") || "workspace";
  return path.join(os.tmpdir(), "vsblender", `${name}-${hash}`);
}

export function isInsideDir(dir: string, file: string): boolean {
  const rel = path.relative(path.resolve(dir), path.resolve(file));
  return rel !== "" && !rel.startsWith("..") && !path.isAbsolute(rel);
}

/** dir is root or inside it (path.relative ignores case on Windows, as the file system does). */
function atOrInside(root: string, dir: string): boolean {
  return path.relative(root, dir) === "" || isInsideDir(root, dir);
}

/** A workspace path, or a check kept in checksDir (run again by path). Anything else is refused. */
export function resolveScriptPath(workspace: string, requested: string): string {
  try {
    return resolveInside(workspace, requested);
  } catch (error) {
    const abs = path.resolve(workspace, requested);
    if (isInsideDir(checksDir(workspace), abs)) return abs;
    throw error;
  }
}

/** How a script is named in a reply: relative to the workspace, or in full when it is a kept check. */
export function scriptLabel(workspace: string, file: string): string {
  return isInsideDir(checksDir(workspace), file) ? file : relativeTo(workspace, file);
}

/**
 * Write an inline check to checksDir and return its path. Only the newest checks are kept.
 * A PowerShell file gets a BOM (Windows PowerShell reads a file without one as ANSI); a bash file
 * gets Unix line endings.
 */
export function writeCheck(workspace: string, code: string, ext: string): string {
  const dir = checksDir(workspace);
  fs.mkdirSync(dir, { recursive: true });
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace("T", "-").slice(0, 15);
  let file = path.join(dir, `check-${stamp}.${ext}`);
  for (let n = 2; fs.existsSync(file); n += 1) file = path.join(dir, `check-${stamp}-${n}.${ext}`);
  let body = code.endsWith("\n") ? code : `${code}\n`;
  if (ext === "sh") body = body.replace(/\r\n/g, "\n");
  fs.writeFileSync(file, ext === "ps1" ? `\ufeff${body}` : body, "utf8");
  pruneChecks(dir);
  return file;
}

function pruneChecks(dir: string): void {
  let files: { file: string; time: number }[] = [];
  try {
    files = fs.readdirSync(dir)
      .filter((name) => name.startsWith("check-"))
      .map((name) => ({ file: path.join(dir, name), time: fs.statSync(path.join(dir, name)).mtimeMs }));
  } catch {
    return;
  }
  files.sort((a, b) => b.time - a.time);
  for (const old of files.slice(KEEP_CHECKS)) {
    try {
      fs.rmSync(old.file, { force: true });
    } catch {
      // A check that is still open elsewhere is removed next time.
    }
  }
}

/** Starting Blender belongs to launch_blender and run_script, not to a check. */
export function startsBlender(source: string): boolean {
  return /blender\.exe\b/i.test(source)
    // blender -b, and the list form subprocess takes: ["blender", "-b", ...].
    || /\bblender["']?\s*,?\s*["']?(?:-b|--background|-P|--python(?:-expr)?)\b/i.test(source);
}

/** The model folder for a script: the nearest folder at or above it that holds a .blend. */
export function modelFolderOf(workspace: string, file: string): string | undefined {
  const root = path.resolve(workspace);
  let current = path.dirname(path.resolve(file));
  while (atOrInside(root, current)) {
    try {
      if (fs.readdirSync(current).some((name) => name.toLowerCase().endsWith(".blend"))) return current;
    } catch {
      // An unreadable folder is skipped.
    }
    if (path.relative(root, current) === "") break;
    current = path.dirname(current);
  }
  return undefined;
}

/**
 * The model folder for an inline check: blend (a .blend or a folder), the file open in this
 * folder's Blender, the only .blend in the workspace, or the workspace itself.
 */
export async function inlineModelFolder(ctx: WorkspaceContext, requested: unknown): Promise<string> {
  if (typeof requested === "string" && requested.trim()) {
    const abs = resolveInside(ctx.workspace, requested);
    if (isDir(abs)) return abs;
    if (abs.toLowerCase().endsWith(".blend")) return path.dirname(abs);
    throw new Error(`blend must be a workspace .blend or folder: ${requested}`);
  }
  const probe = await probeBridge(ctx.port);
  const open = probe.ok ? probe.window?.file : undefined;
  if (open && isInsideDir(ctx.workspace, open)) return path.dirname(open);
  const blends = listBlendFiles(ctx.workspace);
  return blends.length === 1 ? path.dirname(blends[0]!) : ctx.workspace;
}

/** The script's folder, every lib/ from there up to the workspace, a model folder's scripts/ and scripts/lib/, then libPaths and sharedLibs. */
export function pythonPath(workspace: string, start: string, model: string | undefined, imports: string[]): string[] {
  const folders: string[] = [];
  const add = (dir: string): void => {
    const abs = path.resolve(dir);
    const same = (item: string): boolean => process.platform === "win32" ? item.toLowerCase() === abs.toLowerCase() : item === abs;
    if (isDir(abs) && !folders.some(same)) folders.push(abs);
  };
  const root = path.resolve(workspace);
  add(start);
  let current = path.resolve(start);
  while (atOrInside(root, current)) {
    add(path.join(current, "lib"));
    if (path.relative(root, current) === "") break;
    current = path.dirname(current);
  }
  if (model) {
    add(path.join(model, "scripts"));
    add(path.join(model, "scripts", "lib"));
  }
  for (const entry of imports) add(path.isAbsolute(entry) ? entry : path.join(root, entry));
  return folders;
}

function tail(text: string): string {
  const trimmed = text.trimEnd();
  if (trimmed.length <= OUTPUT_LIMIT) return trimmed;
  return `[${trimmed.length - OUTPUT_LIMIT} characters cut]\n${trimmed.slice(-OUTPUT_LIMIT)}`;
}

function failure(text: string): ToolOutcome {
  return { ok: false, text };
}

/** run_python and run_shell. imports: libPaths and sharedLibs, as run_script gets them. */
export async function runLocal(ctx: WorkspaceContext, kind: LocalKind, args: Record<string, unknown>,
  imports: string[], extras: CallExtras = {}): Promise<ToolOutcome> {
  const tool = kind === "python" ? "run_python" : "run_shell";
  const hasPath = typeof args["path"] === "string" && args["path"].trim() !== "";
  const hasCode = typeof args["code"] === "string" && args["code"].trim() !== "";
  if (hasPath === hasCode) return failure(`${tool} takes path (a workspace script) or code (an inline check), one of them.`);

  let ext: string;
  let source: string;
  let script = "";
  let inline = false;
  if (hasCode) {
    source = String(args["code"]);
    const shell = String(args["shell"] ?? "bash").toLowerCase();
    if (kind === "python") ext = "py";
    else if (shell === "bash" || shell === "sh") ext = "sh";
    else if (shell === "powershell" || shell === "pwsh" || shell === "ps1") ext = "ps1";
    else return failure(`shell must be bash or powershell, not ${shell}`);
  } else {
    try {
      script = resolveScriptPath(ctx.workspace, String(args["path"]));
    } catch (error) {
      return failure(`${error instanceof Error ? error.message : String(error)}. ${tool} runs scripts in the workspace, or a check kept from an earlier inline run.`);
    }
    if (!isFile(script)) return failure(`file not found: ${String(args["path"])}`);
    ext = path.extname(script).slice(1).toLowerCase();
    if (kind === "python" && ext !== "py") return failure("run_python runs a .py file");
    if (kind === "shell" && ext === "bash") ext = "sh";
    if (kind === "shell" && ext !== "sh" && ext !== "ps1") return failure("run_shell runs a .sh (bash) or .ps1 (PowerShell) file");
    source = fs.readFileSync(script, "utf8");
  }
  if (startsBlender(source)) {
    return failure(`${tool} does not start Blender. Use run_script for work in Blender (code: for a check on a throwaway copy), and launch_blender to open it.`);
  }

  let interpreter: Interpreter | undefined;
  if (ext === "py") interpreter = findPython(ctx);
  else if (ext === "sh") interpreter = findBash(ctx);
  else interpreter = findPowerShell();
  if (!interpreter) {
    const what = ext === "py" ? "Python" : ext === "sh" ? "bash" : "PowerShell";
    const key = ext === "py" ? "python" : "bash";
    return failure(`${what} was not found. ${ext === "ps1" ? "Install PowerShell" : `Set "${key}" in .blender-ai/config.json to the program`}${ext === "py" ? ", or configure Blender (its Python is used)" : ext === "sh" ? " (Git for Windows' bin\\bash.exe on Windows)" : ""}.`);
  }

  let model: string | undefined;
  let cwd: string;
  try {
    if (hasCode) {
      model = await inlineModelFolder(ctx, args["blend"]);
      script = writeCheck(ctx.workspace, source, ext);
      inline = true;
    } else if (isInsideDir(checksDir(ctx.workspace), script)) {
      model = await inlineModelFolder(ctx, args["blend"]);
    } else {
      model = modelFolderOf(ctx.workspace, script);
    }
    if (typeof args["cwd"] === "string" && args["cwd"].trim()) {
      cwd = resolveInside(ctx.workspace, args["cwd"]);
      if (!isDir(cwd)) return failure(`cwd is not a folder: ${args["cwd"]}`);
    } else {
      cwd = model ?? ctx.workspace;
    }
  } catch (error) {
    return failure(error instanceof Error ? error.message : String(error));
  }
  const file = script;

  const argv = Array.isArray(args["argv"]) ? (args["argv"] as unknown[]).map((item) => String(item)) : [];
  let command: string[];
  if (ext === "py") command = ["-s", file, ...argv];
  else if (ext === "sh") command = [process.platform === "win32" ? file.replace(/\\/g, "/") : file, ...argv];
  else command = ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", file, ...argv];

  const env: NodeJS.ProcessEnv = { ...process.env };
  delete env["PYTHONHOME"];
  env["VSBLENDER_WORKSPACE"] = ctx.workspace;
  env["VSBLENDER_MODEL_DIR"] = model ?? ctx.workspace;
  env["PYTHONUTF8"] = "1";
  env["PYTHONIOENCODING"] = "utf-8";
  if (ext === "py") {
    env["PYTHONPATH"] = pythonPath(ctx.workspace, inline ? cwd : path.dirname(file), model, imports).join(path.delimiter);
  }

  const timeout = Math.min(MAX_TIMEOUT, Math.max(1000, Number(args["timeout_ms"]) || DEFAULT_TIMEOUT));
  extras.progress?.(undefined, `running ${path.basename(file)}`);
  const started = Date.now();
  const result = await execFileText(interpreter.path, command, { timeout, cwd, env, signal: extras.signal });
  const seconds = ((Date.now() - started) / 1000).toFixed(1);

  const what = ext === "py" ? "python" : ext === "sh" ? "bash" : "powershell";
  const lines = [
    `ran: ${what} (${interpreter.source}: ${interpreter.path})`,
    inline ? `script: inline check, kept at ${file} (pass it as path to run it again)` : `script: ${scriptLabel(ctx.workspace, file)}`,
    `cwd: ${relativeTo(ctx.workspace, cwd) || "."}`,
  ];
  const stopped = extras.signal?.aborted ? "cancelled" : result.code === 124 ? `stopped after ${timeout / 1000} s (timeout_ms; up to ${MAX_TIMEOUT})` : "";
  lines.push(stopped ? `exit: ${stopped}` : `exit: ${result.code} (${seconds} s)`);
  if (result.stdout.trim()) lines.push(`stdout:\n${tail(result.stdout)}`);
  if (result.stderr.trim()) lines.push(`stderr:\n${tail(result.stderr)}`);
  if (ext === "sh" && /\$'\\r'/.test(result.stderr)) lines.push("The script has Windows line endings (CRLF); bash needs LF.");
  if (!result.stdout.trim() && !result.stderr.trim()) lines.push("(no output)");
  const details = { interpreter: interpreter.path, script: file, cwd, exit: result.code, inline };
  return { ok: result.code === 0 && !stopped, text: lines.join("\n"), details };
}
