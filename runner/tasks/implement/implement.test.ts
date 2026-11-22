// The implement task, end to end through run-task.ts with a fake `claude`
// (see runner/testing.ts). No Claude usage is spent.

import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { FACTORY, claudeEnv, fakeClaude, git, makeTarget, runTask, success, writeOutput } from "../../testing";

const built = { verdict: "built", pr_body: "Built it." };

const implementDispatch = (sha: string, extra: object = {}) => ({
  contract_version: 1,
  run_id: 414,
  repo: "octo/widgets",
  task: "implement",
  ticket: "123",
  ticket_markdown: "# Search ignores accents\n\nSearching for cafe doesn't find café.\n",
  base_sha: sha,
  branch: "factory/123-414",
  enrolment_sha: sha,
  model: "claude-test-model",
  timeout_minutes: 45,
  triggered_by: { login: "octocat", id: 583231 },
  ...extra,
});

// A fake that prints its prompt and ticket, then answers with `output`.
const echoing = (output: unknown) =>
  `sed 's/^/prompt: /'\nsed 's/^/ticket: /' ${FACTORY}/ticket.md\n${writeOutput(output)}\n${success}`;

// What the fake printed with a prefix: its prompt or its ticket.md.
const printed = (prefix: string) => (output: string) =>
  output
    .split("\n")
    .filter((line) => line.startsWith(`${prefix}: `))
    .map((line) => line.slice(`${prefix}: `.length))
    .join("\n");
const promptOf = printed("prompt");
const ticketOf = printed("ticket");

describe("implement", () => {
  it("tells the agent to run the repo's check until it passes, or to return needs-a-human", () => {
    const target = makeTarget();
    const sha = target.commit(fakeClaude(echoing(built), "setup: \"true\"\ncheck: make lint test || true\n"));

    const run = runTask(implementDispatch(sha), target.url, claudeEnv);

    expect(run.result?.outcome, run.output).toBe("succeeded");
    // The task's own instructions, before the generic "Your output" section.
    const own = promptOf(run.output).split("## Your output")[0]!;
    expect(own).toContain("`make lint test || true`");
    expect(own).toContain("needs-a-human");
    expect(run.output).toContain("ticket: # Search ignores accents");
  });

  it("renders a failed previous attempt as a Previous attempt section at the end of the ticket", () => {
    const target = makeTarget();
    const sha = target.commit(fakeClaude(echoing(built)));

    const run = runTask(
      implementDispatch(sha, {
        previous_attempt: {
          outcome: "failed",
          failure: "check_failed",
          check_output: "FAILED tests/test_search.py::test_accents\n1 failed, 41 passed\n",
        },
      }),
      target.url,
      claudeEnv,
    );

    expect(run.result?.outcome, run.output).toBe("succeeded");
    expect(ticketOf(run.output)).toBe(
      [
        "# Search ignores accents",
        "",
        "Searching for cafe doesn't find café.",
        "",
        "## Previous attempt",
        "",
        "The previous attempt at this ticket failed: check_failed.",
        "",
        "The last lines of its check output:",
        "",
        "```",
        "FAILED tests/test_search.py::test_accents",
        "1 failed, 41 passed",
        "```",
      ].join("\n"),
    );
  });

  it("renders an invalid previous attempt with its validation error", () => {
    const target = makeTarget();
    const sha = target.commit(fakeClaude(echoing(built)));

    const run = runTask(
      implementDispatch(sha, {
        previous_attempt: { outcome: "invalid", validation_error: "built with no commits on top of the base" },
      }),
      target.url,
      claudeEnv,
    );

    expect(ticketOf(run.output)).toContain(
      [
        "## Previous attempt",
        "",
        "The previous attempt at this ticket produced an invalid result:",
        "",
        "```",
        "built with no commits on top of the base",
        "```",
      ].join("\n"),
    );
  });

  it("starts the agent on the named branch at base_sha, and bundles what it commits there", () => {
    const target = makeTarget();
    const base = target.commit(
      fakeClaude(
        `echo "on: $(git branch --show-current) $(git rev-parse HEAD)"\n` +
          `echo fix > fix.txt && git add fix.txt && git commit -q -m "Fold accents"\n` +
          `${writeOutput(built)}\n${success}`,
      ),
    );

    const run = runTask(implementDispatch(base), target.url, claudeEnv);

    expect(run.result, run.output).toMatchObject({ outcome: "succeeded", output: built });
    expect(run.output).toContain(`on: factory/123-414 ${base}`);
    const bundle = join(run.out, "branch.bundle");
    expect(git(run.out, "bundle", "list-heads", bundle)).toMatch(/ refs\/heads\/factory\/123-414$/);
  });

  it("returns needs-a-human with its comment, and still runs the check", () => {
    const needsAHuman = { verdict: "needs-a-human", comment: "The findings say search() but there is no search()." };
    const target = makeTarget();
    const sha = target.commit(fakeClaude(`${writeOutput(needsAHuman)}\n${success}`));

    const run = runTask(implementDispatch(sha), target.url, claudeEnv);

    expect(run.result, run.output).toEqual({
      contract_version: 1,
      run_id: 414,
      task: "implement",
      outcome: "succeeded",
      check: { passed: true, output_tail: "checked\n" },
      output: needsAHuman,
    });
  });

  it("leaves the ticket as it is without a previous attempt", () => {
    const target = makeTarget();
    const sha = target.commit(fakeClaude(echoing(built)));

    const run = runTask(implementDispatch(sha), target.url, claudeEnv);

    expect(ticketOf(run.output)).toBe("# Search ignores accents\n\nSearching for cafe doesn't find café.");
  });
});
