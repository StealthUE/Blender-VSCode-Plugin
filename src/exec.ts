import { execFile } from "child_process";

export interface ExecResult {
  stdout: string;
  stderr: string;
  code: number;
}

export function execFileText(
  command: string,
  args: string[],
  options: { timeout: number; cwd?: string; env?: NodeJS.ProcessEnv; maxBuffer?: number }
): Promise<ExecResult> {
  return new Promise((resolve) => {
    execFile(
      command,
      args,
      {
        timeout: options.timeout,
        cwd: options.cwd,
        env: options.env,
        encoding: "utf8",
        windowsHide: true,
        maxBuffer: options.maxBuffer ?? 20 * 1024 * 1024,
      },
      (error, stdout, stderr) => {
        const stdoutText = typeof stdout === "string" ? stdout : "";
        const stderrText = typeof stderr === "string" ? stderr : "";
        if (!error) {
          resolve({ stdout: stdoutText, stderr: stderrText, code: 0 });
          return;
        }
        const coded = error as NodeJS.ErrnoException & { code?: number | string; killed?: boolean };
        const code = typeof coded.code === "number" ? coded.code : coded.killed ? 124 : 1;
        const extra = coded.message && !stderrText.includes(coded.message) ? `\n${coded.message}` : "";
        resolve({ stdout: stdoutText, stderr: stderrText + extra, code });
      }
    );
  });
}
