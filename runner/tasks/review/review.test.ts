// Tests the review task through the run script, like run-task.test.ts: a
// target repo in a local bare git repo with a PR branch on it, a fake
// `claude` in the enrolment's image, and what the run leaves in its output
// folder. Needs git and Docker (`npm run test:runner`).

import { existsSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import {
  FACTORY, claudeEnv, fakeClaude as fakeAgent, git, makeTarget, runTask as runScript, success, type Files, type Run,
} from "../../testing";

const BRANCH = "feature/accents";

// The fake `claude` (testing.ts), with the enrolment's check command.
const fakeClaude = (script: string, check = "echo checked"): Files =>
  fakeAgent(script, `setup: "true"\ncheck: ${JSON.stringify(check)}\n`);

const reviewOutput = {
  standards: { verdict: "pass", reasoning: "Fine." },
  spec: { verdict: "pass", reasoning: "Does what #123 asks." },
  comments: [],
  fixed: [],
};
const writeOutput = `cat > ${FACTORY}/output.json <<'JSON'\n${JSON.stringify(reviewOutput)}\nJSON`;

// What the fake does as an agent making one fix: a commit with the finding
// in its message.
const fix = (file: string, content = "fixed") =>
  `echo ${content} > ${file} && git add -A && git commit -q -m "review: standards: ${file}"`;

// main with the enrolment, then a PR branch with `pr` on top. Returns both
// ends of the PR.
const makePR = (agent: string, pr: Files = { "search.py": "def search(): pass\n" }, base: Files = {}, check?: string) => {
  const target = makeTarget();
  const prBase = target.commit({ ...fakeClaude(`${agent}\n${writeOutput}\n${success}`, check), ...base });
  target.onBranch(BRANCH);
  const head = target.commit(pr);
  return { target, prBase, head };
};

const reviewDispatch = (pr: { prBase: string; head: string }, extra: object = {}) => ({
  contract_version: 1,
  run_id: 426,
  repo: "octo/widgets",
  task: "review",
  ticket: "130",
  ticket_markdown: "# PR #130: Fold accents when searching\n",
  pr_number: 130,
  pr_base_sha: pr.prBase,
  findings_only: false,
  base_sha: pr.head,
  branch: BRANCH,
  triggered_by: { login: "octocat", id: 583231 },
  enrolment_sha: pr.prBase,
  model: "claude-test-model",
  timeout_minutes: 45,
  ...extra,
});

const runTask = (dispatch: object, targetUrl: string): Run => runScript(dispatch, targetUrl, claudeEnv);

// The files each commit in the run's bundle touches, oldest first.
const bundled = (target: { url: string }, run: Run): string[][] => {
  const bundle = join(run.out, "branch.bundle");
  if (!existsSync(bundle)) return [];
  const dir = mkdtempSync(join(tmpdir(), "factory-unbundle-"));
  git(dir, "clone", "-q", "--no-checkout", target.url, "clone");
  const clone = join(dir, "clone");
  git(clone, "fetch", "-q", bundle, `refs/heads/${BRANCH}:refs/heads/fetched`);
  const base = git(clone, "rev-parse", `origin/${BRANCH}`);
  return git(clone, "rev-list", "--reverse", `${base}..fetched`)
    .split("\n")
    .map((c) => git(clone, "show", "--format=", "--name-only", c).split("\n"));
};

describe("a review run", () => {
  it("has the PR's diff on disk and records the check before and after the agent", () => {
    const pr = makePR(`git diff --stat refs/factory/pr-base...HEAD | sed 's/^/diff: /'`);

    const run = runTask(reviewDispatch(pr), pr.target.url);

    expect(run.result, run.output).toEqual({
      contract_version: 1,
      run_id: 426,
      task: "review",
      outcome: "succeeded",
      check_before: { passed: true, output_tail: "checked\n" },
      check_after: { passed: true, output_tail: "checked\n" },
      output: reviewOutput,
    });
    expect(run.output).toMatch(/^diff: +search\.py \| 1 \+$/m);
    expect(run.status).toBe(0);
  });

  it("asks the agent for the two-axis code-review skill and one commit per fix", () => {
    const pr = makePR(`sed 's/^/prompt: /'`);

    const run = runTask(reviewDispatch(pr), pr.target.url);

    expect(run.result?.outcome, run.output).toBe("succeeded");
    expect(run.output).toMatch(/^prompt: .*`factory:code-review`/m);
    expect(run.output).toMatch(/^prompt: .*one commit per fix/im);
    expect(run.output).toMatch(/^prompt: .*\/home\/agent\/factory\/review\.md/m);
  });
});

describe("agent instructions", () => {
  it("come from the PR's base, the agent's commits leave the PR's own alone, and the result says the PR changed them", () => {
    const show = (path: string) => `{ cat ${path} 2>/dev/null || echo missing; } | sed 's#^#${path}: #'`;
    const pr = makePR(
      [show("CLAUDE.md"), show("AGENTS.md"), show(".claude/settings.json"), show("app/CLAUDE.md"), fix("search.py")].join("\n"),
      {
        "CLAUDE.md": "PR rules\n",
        "AGENTS.md": "PR agents\n",
        ".claude/settings.json": null,
        "app/CLAUDE.md": "PR app rules\n",
        "search.py": "def search(): pass\n",
      },
      { "CLAUDE.md": "base rules\n", ".claude/settings.json": "{}\n" },
    );

    const run = runTask(reviewDispatch(pr), pr.target.url);

    expect(run.result, run.output).toMatchObject({ outcome: "succeeded", instructions_changed: true });
    expect(run.output).toMatch(/^CLAUDE\.md: base rules$/m);
    expect(run.output).toMatch(/^AGENTS\.md: missing$/m);
    expect(run.output).toMatch(/^\.claude\/settings\.json: \{\}$/m);
    expect(run.output).toMatch(/^app\/CLAUDE\.md: missing$/m);
    expect(bundled(pr.target, run)).toEqual([["search.py"]]);
  });
});

describe("a findings-only review", () => {
  it("tells the agent, and uploads no commits and runs no check after it", () => {
    const pr = makePR(`sed 's/^/brief: /' ${FACTORY}/review.md\n${fix("search.py")}`);

    const run = runTask(reviewDispatch(pr, { findings_only: true }), pr.target.url);

    expect(run.result, run.output).toEqual({
      contract_version: 1,
      run_id: 426,
      task: "review",
      outcome: "succeeded",
      check_before: { passed: true, output_tail: "checked\n" },
      output: reviewOutput,
    });
    expect(run.output).toMatch(/^brief: .*findings only.*commit nothing/im);
    expect(existsSync(join(run.out, "branch.bundle"))).toBe(false);
  });
});

describe("the check", () => {
  const check = "cat status.txt; grep -qx fixed status.txt";

  it("tells the agent the PR failed it, and passes after the agent's fix", () => {
    const pr = makePR(
      `sed 's/^/brief: /' ${FACTORY}/review.md\n${fix("status.txt")}`,
      { "status.txt": "broken\n" },
      {},
      check,
    );

    const run = runTask(reviewDispatch(pr), pr.target.url);

    expect(run.result, run.output).toMatchObject({
      outcome: "succeeded",
      check_before: { passed: false, output_tail: "broken\n" },
      check_after: { passed: true, output_tail: "fixed\n" },
    });
    expect(run.output).toMatch(/^brief: .*check failed/im);
    expect(run.output).toMatch(/^brief: broken$/m);
    expect(run.output).toContain("brief: - The check is `cat status.txt; grep -qx fixed status.txt`.");
    expect(run.output).toContain("brief: - Fixes may touch only the files the PR changes.");
    expect(bundled(pr.target, run)).toEqual([["status.txt"]]);
  });

  it("runs after the agent on its commits only, and fails the run with the agent's output kept", () => {
    const pr = makePR(`echo fixed > status.txt`, { "status.txt": "broken\n" }, {}, check);

    const run = runTask(reviewDispatch(pr), pr.target.url);

    expect(run.result, run.output).toEqual({
      contract_version: 1,
      run_id: 426,
      task: "review",
      outcome: "failed",
      failure: "check_failed",
      check_before: { passed: false, output_tail: "broken\n" },
      check_after: { passed: false, output_tail: "broken\n" },
      output: reviewOutput,
    });
  });
});
