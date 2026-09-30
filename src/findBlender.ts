import * as fs from "fs";
import * as path from "path";
import { execFileText } from "./exec";

export interface BlenderInstall {
  path: string;
  version: string;
}

export function defaultSearchRoots(): string[] {
  const roots: string[] = [];
  const programFiles = process.env["ProgramFiles"];
  const programFilesX86 = process.env["ProgramFiles(x86)"];
  const localAppData = process.env["LOCALAPPDATA"];
  if (programFiles) roots.push(path.join(programFiles, "Blender Foundation"));
  if (programFilesX86) roots.push(path.join(programFilesX86, "Blender Foundation"));
  if (localAppData) roots.push(path.join(localAppData, "Programs"));
  if (programFiles) roots.push(path.join(programFiles, "Steam", "steamapps", "common"));
  if (programFilesX86) roots.push(path.join(programFilesX86, "Steam", "steamapps", "common"));
  return roots;
}

export function blenderExesUnder(root: string): string[] {
  if (!root || !fs.existsSync(root)) return [];
  const found: string[] = [];
  const consider = (file: string): void => {
    if (fs.existsSync(file)) found.push(file);
  };
  consider(path.join(root, "blender.exe"));
  consider(path.join(root, "blender"));
  let entries: fs.Dirent[] = [];
  try {
    entries = fs.readdirSync(root, { withFileTypes: true });
  } catch {
    return found;
  }
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const child = path.join(root, entry.name);
    consider(path.join(child, "blender.exe"));
    consider(path.join(child, "blender"));
    let nested: fs.Dirent[] = [];
    try {
      nested = fs.readdirSync(child, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const inner of nested) {
      if (!inner.isDirectory()) continue;
      consider(path.join(child, inner.name, "blender.exe"));
      consider(path.join(child, inner.name, "blender"));
    }
  }
  return found;
}

export function parseBlenderVersion(text: string): string | undefined {
  const match = /Blender\s+(\d+\.\d+(?:\.\d+)?)/i.exec(text);
  return match?.[1];
}

export function compareVersions(a: string, b: string): number {
  const left = a.split(".").map((part) => Number(part) || 0);
  const right = b.split(".").map((part) => Number(part) || 0);
  const length = Math.max(left.length, right.length);
  for (let i = 0; i < length; i += 1) {
    const diff = (left[i] ?? 0) - (right[i] ?? 0);
    if (diff !== 0) return diff;
  }
  return 0;
}

async function whereExecutable(name: string): Promise<string[]> {
  const command = process.platform === "win32" ? "where.exe" : "which";
  const result = await execFileText(command, [name], { timeout: 5000 });
  if (result.code !== 0) return [];
  return result.stdout
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.toLowerCase().startsWith("info:") && fs.existsSync(line));
}

export async function defaultVersionOf(exe: string): Promise<string | undefined> {
  const result = await execFileText(exe, ["--version"], { timeout: 30000 });
  return parseBlenderVersion(`${result.stdout}\n${result.stderr}`);
}

export async function findBlenders(options?: {
  extraDirs?: string[];
  /** When set, these directories are scanned instead of the default install locations. */
  roots?: string[];
  versionOf?: (exe: string) => Promise<string | undefined>;
  includePath?: boolean;
}): Promise<BlenderInstall[]> {
  const versionOf = options?.versionOf ?? defaultVersionOf;
  const candidates = new Set<string>();
  const roots = options?.roots ?? [...defaultSearchRoots(), ...(options?.extraDirs ?? [])];
  for (const root of roots) {
    for (const exe of blenderExesUnder(root)) candidates.add(path.resolve(exe));
  }
  if (options?.includePath !== false) {
    for (const exe of await whereExecutable("blender")) candidates.add(path.resolve(exe));
  }
  const installs: BlenderInstall[] = [];
  for (const exe of candidates) {
    const version = (await versionOf(exe)) ?? "0.0.0";
    installs.push({ path: exe, version });
  }
  installs.sort((a, b) => compareVersions(b.version, a.version));
  return installs;
}

export async function findNode(): Promise<string | undefined> {
  const matches = await whereExecutable("node");
  return matches.find((file) => /node(\.exe)?$/i.test(file));
}
