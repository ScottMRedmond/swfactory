// Helpers for tests that drive `runner/run-task.ts` from the outside: a
// target repo in a local bare git repo, an enrolment whose image swaps
// Claude Code for a fake `claude` script, and a run of the script as a
// subprocess. They need git, Docker and factory-base:test (`npm run
// build:image`); run them with `npm run test:runner`.

import { execFileSync, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export const root = join(dirname(fileURLToPath(import.meta.url)), "..");
export const baseImage = process.env.FACTORY_BASE_IMAGE ?? "factory-base:test";

export const git = (cwd: string, ...args: string[]): string =>
  execFileSync("git", args, { cwd, encoding: "utf8" }).trim();

// Path -> content; null deletes the file.
export type Files = Record<string, string | null>;

// A bare repo standing in for the target on GitHub. Each call to commit()
// adds a commit with the given files on the current branch (main at first)
// and returns its sha; onBranch() switches to a new branch from the current
// commit, e.g. a PR's.
export const makeTarget = () => {
  const dir = mkdtempSync(join(tmpdir(), "factory-target-"));
  const bare = join(dir, "target.git");
  const work = join(dir, "work");
  git(dir, "init", "-q", "--bare", "-b", "main", bare);
  git(dir, "init", "-q", "-b", "main", work);
  git(work, "remote", "add", "origin", bare);
  git(work, "config", "user.email", "test@example.com");
  git(work, "config", "user.name", "test");
  const commit = (files: Files): string => {
    for (const [path, content] of Object.entries(files)) {
      if (content === null) {
        rmSync(join(work, path));
        continue;
      }
      mkdirSync(dirname(join(work, path)), { recursive: true });
      writeFileSync(join(work, path), content);
    }
    git(work, "add", "-A");
    git(work, "commit", "-q", "-m", "change");
    git(work, "push", "-q", "origin", "HEAD");
    return git(work, "rev-parse", "HEAD");
  };
  const onBranch = (name: string) => git(work, "checkout", "-q", "-b", name);
  return { url: bare, commit, onBranch };
};

export type Run = { status: number; output: string; out: string; result?: any };

// Image names unique to this test process, so test files running at once
// don't build over each other's `factory-run-<run_id>`.
const IMAGE_PREFIX = `factory-test-${process.pid}`;

export const runTask = (dispatch: unknown, targetUrl: string, env: NodeJS.ProcessEnv = {}): Run => {
  const dir = mkdtempSync(join(tmpdir(), "factory-run-"));
  const dispatchPath = join(dir, "dispatch.json");
  writeFileSync(dispatchPath, typeof dispatch === "string" ? dispatch : JSON.stringify(dispatch));
  const out = join(dir, "out");
  const work = join(dir, "work");
  const proc = spawnSync("npx", ["tsx", join(root, "runner/run-task.ts"), dispatchPath, out, work], {
    cwd: root,
    encoding: "utf8",
    env: { ...process.env, FACTORY_TARGET_URL: targetUrl, FACTORY_IMAGE_PREFIX: IMAGE_PREFIX, ...env },
  });
  const runId = (dispatch as { run_id?: unknown })?.run_id;
  if (typeof runId === "number") spawnSync("docker", ["rmi", "-f", `${IMAGE_PREFIX}-${runId}`]);
  const resultPath = join(out, "result.json");
  return {
    status: proc.status ?? -1,
    output: proc.stdout + proc.stderr,
    out,
    result: existsSync(resultPath) ? JSON.parse(readFileSync(resultPath, "utf8")) : undefined,
  };
};

// The agent's folder, where the prompt tells it to find the ticket and
// write output.json.
export const FACTORY = "/home/agent/factory";

// An enrolment whose image has a fake `claude` (copied over the real one, so
// no Claude usage is spent): a bash script that gets the prompt on stdin and
// runs in the workspace.
export const fakeClaude = (script: string, config = "setup: \"true\"\ncheck: echo checked\n"): Files => ({
  ".factory/Dockerfile": `FROM ${baseImage}\nCOPY --chmod=755 fake-claude /usr/local/bin/claude\n`,
  ".factory/config.yml": config,
  ".factory/fake-claude": `#!/bin/bash\nset -u\n${script}\n`,
});

// One stream-json line as Claude Code prints it.
export const event = (data: object) => `echo '${JSON.stringify({ session_id: "s-1", ...data })}'`;
export const success = event({ type: "result", subtype: "success", is_error: false, result: "done" });

export const writeOutput = (output: unknown) =>
  `cat > ${FACTORY}/output.json <<'JSON'\n${typeof output === "string" ? output : JSON.stringify(output)}\nJSON`;

// What run.yml gives the script: the App's bot identity, and with it the
// Claude token.
export const bot = {
  FACTORY_BOT_NAME: "swfactory[bot]",
  FACTORY_BOT_EMAIL: "1234+swfactory[bot]@users.noreply.github.com",
};
export const claudeEnv = { CLAUDE_CODE_OAUTH_TOKEN: "sk-ant-oat01-test-token", ...bot };
