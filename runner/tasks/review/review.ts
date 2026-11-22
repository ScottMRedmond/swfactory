// The review task's own steps, around the agent step in run-task.ts. See
// "Review" in docs/software-factory.md.
//
// The checkout is the PR's head (base_sha) with its base (pr_base_sha)
// fetched too and named PR_BASE_REF, so the diff is on disk: the agent has
// no credential to fetch anything itself.

import { spawnSync } from "node:child_process";
import { appendFileSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { AGENT_DIR } from "../../agent";

export const PR_BASE_REF = "refs/factory/pr-base";
export const BRIEF = `${AGENT_DIR}/review.md`;

type Check = { passed: boolean; output_tail: string };

const git = (cwd: string, args: string[], input?: string | Buffer) => {
  const proc = spawnSync("git", ["-C", cwd, ...args], { input, maxBuffer: 256 * 1024 * 1024 });
  if (proc.error) throw proc.error;
  return { status: proc.status ?? 1, stdout: proc.stdout, stderr: String(proc.stderr ?? "") };
};

const mustGit = (cwd: string, args: string[], input?: string | Buffer) => {
  const result = git(cwd, args, input);
  if (result.status !== 0) throw new Error(`git ${args.join(" ")} failed: ${result.stderr}`);
  return result.stdout;
};

// The files that tell an agent how to work, wherever they are in the repo.
const INSTRUCTIONS = [":(glob)**/CLAUDE.md", ":(glob)**/AGENTS.md", ":(glob)**/.claude/**"];

// On the host, in the fresh checkout of the PR's head, before it's copied
// into the container: names the PR's base, and puts back the base's agent
// instructions wherever the PR changed them, so a PR can't change the
// instructions its own review runs under. Returns whether it changed any.
//
// The restored files are hidden from git (skip-worktree for files the head
// has, info/exclude for files only the base has), so the agent's
// `git add -A` doesn't commit them back over the PR's.
export const prepareCheckout = (target: string, prBaseSha: string): boolean => {
  mustGit(target, ["update-ref", PR_BASE_REF, prBaseSha]);
  const changed = String(
    mustGit(target, ["diff", "--name-only", "-z", "--no-renames", PR_BASE_REF, "HEAD", "--", ...INSTRUCTIONS]),
  )
    .split("\0")
    .filter(Boolean);
  const exclude: string[] = [];
  for (const path of changed) {
    const file = join(target, path);
    if (git(target, ["cat-file", "-e", `${prBaseSha}:${path}`]).status === 0) {
      mkdirSync(dirname(file), { recursive: true });
      writeFileSync(file, mustGit(target, ["show", `${prBaseSha}:${path}`]));
    } else {
      rmSync(file, { force: true });
    }
    if (git(target, ["cat-file", "-e", `HEAD:${path}`]).status === 0) {
      mustGit(target, ["update-index", "--skip-worktree", "--", path]);
    } else {
      exclude.push(`/${path.replace(/([[\]*?!#\\ ])/g, "\\$1")}`);
    }
  }
  if (exclude.length) {
    const info = join(target, ".git", "info");
    mkdirSync(info, { recursive: true });
    appendFileSync(join(info, "exclude"), exclude.join("\n") + "\n");
  }
  if (changed.length) console.log(`[factory] restored the PR's base version of ${changed.join(", ")}`);
  return changed.length > 0;
};

// What the agent is told about this review, next to the ticket.
export const brief = (
  dispatch: { pr_number?: number; pr_base_sha?: string; branch?: string; findings_only?: boolean },
  enrolment: { check: string; test_paths?: string[] },
  checkBefore: Check,
): string => {
  const tests = enrolment.test_paths?.length
    ? ` and files matching the repo's test paths: ${enrolment.test_paths.map((p) => `\`${p}\``).join(", ")}`
    : "";
  const ticks = (checkBefore.output_tail.match(/`+/g) ?? []).map((m) => m.length + 1);
  const fence = "`".repeat(Math.max(3, ...ticks));
  return [
    `# Review of PR #${dispatch.pr_number}`,
    "",
    `- The PR's head is checked out on branch \`${dispatch.branch}\`. Its base is \`${PR_BASE_REF}\` ` +
      `(${dispatch.pr_base_sha}), so the PR is \`git diff ${PR_BASE_REF}...HEAD\`.`,
    dispatch.findings_only
      ? "- **Findings only:** commit nothing. Every change you would have made is a finding instead."
      : "- Fix what you can, one commit per fix, with the finding in the message.",
    `- Fixes may touch only the files the PR changes${tests}.`,
    `- The check is \`${enrolment.check.trim()}\`.`,
    "",
    checkBefore.passed
      ? "The check passed before you started."
      : "**The check failed** before you started, on the PR as it is. The last 50 lines of its output:",
    "",
    ...(checkBefore.passed ? [] : [fence, checkBefore.output_tail.trimEnd(), fence, ""]),
  ].join("\n");
};

// After the agent: throw away whatever it left uncommitted, so the check
// runs on its commits only, which are all that gets pushed.
export const cleanWorkspace = (container: string, workspace: string) => {
  const proc = spawnSync(
    "docker",
    ["exec", "-w", workspace, container, "sh", "-c", "git reset -q --hard && git clean -qfd"],
    { encoding: "utf8" },
  );
  // The check still runs, and says what it found, if this fails.
  if (proc.status !== 0) console.log(`[factory] can't clean the workspace: ${proc.stdout}${proc.stderr}`);
};
