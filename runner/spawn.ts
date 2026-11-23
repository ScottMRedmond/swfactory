// How the run script runs a command (git, docker, tar) and waits for it:
// the one place that calls spawnSync. A command that can't start at all
// (e.g. it isn't installed) throws; one that exits non-zero doesn't, unless
// it's run with `mustRun`.

import { spawnSync, type StdioOptions } from "node:child_process";

export type RunOptions = {
  input?: string | Buffer;
  // The command's whole environment (default: this process's).
  env?: NodeJS.ProcessEnv;
  stdio?: StdioOptions;
};

export type Ran = {
  status: number;
  stdout: string;
  // stdout as bytes, for a command whose output isn't text (e.g. `git show`).
  bytes: Buffer;
  stderr: string;
  // stdout then stderr.
  output: string;
};

export const run = (command: string, args: string[], options: RunOptions = {}): Ran => {
  const proc = spawnSync(command, args, { maxBuffer: 256 * 1024 * 1024, ...options });
  if (proc.error) throw proc.error;
  const bytes = proc.stdout ?? Buffer.alloc(0);
  const stdout = bytes.toString("utf8");
  const stderr = (proc.stderr ?? Buffer.alloc(0)).toString("utf8");
  return { status: proc.status ?? 1, stdout, bytes, stderr, output: stdout + stderr };
};

export class CommandFailed extends Error {}

// Like `run`, but a non-zero exit throws `CommandFailed` with the output.
export const mustRun = (command: string, args: string[], options: RunOptions = {}): Ran => {
  const ran = run(command, args, options);
  if (ran.status !== 0) throw new CommandFailed(`${command} ${args.join(" ")} failed:\n${ran.output}`);
  return ran;
};
