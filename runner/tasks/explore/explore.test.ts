// The explore task, tested from the outside with the helpers in
// runner/testing.ts: a fake `claude` in the target's enrolment, and the run
// script as a subprocess. Run with `npm run test:runner`.

import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { FACTORY, claudeEnv, fakeClaude, makeTarget, root, runTask, success, writeOutput } from "../../testing";

// The valid explore dispatch from contracts/examples, pointed at the target.
// Its run_id names the run's Docker image, so it's unique among the runner's
// test files, which vitest runs in parallel.
const exploreDispatch = (sha: string) => ({
  ...JSON.parse(readFileSync(join(root, "contracts/examples/dispatch/explore.valid.json"), "utf8")),
  run_id: 424,
  base_sha: sha,
  enrolment_sha: sha,
});

const runExplore = (script: string, config?: string) => {
  const target = makeTarget();
  const sha = target.commit({
    ...fakeClaude(script, config),
    "app/search.py": "def search(q, names):\n    return [n for n in names if q in n]\n",
  });
  return runTask(exploreDispatch(sha), target.url, claudeEnv);
};

const easyCall = {
  verdict: "easy-call",
  summary: "Make search match accented names.",
  findings: "search() compares raw strings, so 'cafe' misses 'café'.",
  plan: ["Fold accents in search()"],
  files: ["app/search.py"],
};

describe("an explore run", () => {
  it("gives the agent the ticket and the older open issues, and its prompt says where they are", () => {
    const run = runExplore(
      `sed 's/^/prompt: /'\n` +
        `sed 's/^/ticket: /' ${FACTORY}/ticket.md\n` +
        `sed 's/^/older: /' ${FACTORY}/older-issues.md\n` +
        `${writeOutput(easyCall)}\n${success}`,
    );

    expect(run.result, run.output).toEqual({
      contract_version: 1,
      run_id: 424,
      task: "explore",
      outcome: "succeeded",
      output: easyCall,
    });
    expect(run.output).toContain("ticket: # Search ignores accents");
    expect(run.output).toContain("older: - #118 Search is slow on large lists");
    expect(run.output).toContain("older: - #97 Unicode names sort wrongly");
    expect(run.output).toMatch(new RegExp(`^prompt: .*${FACTORY}/older-issues\\.md`, "m"));
    expect(run.output).toMatch(/^prompt: .*duplicate_of/m);
  });

  it("runs setup before the agent, and uploads only result.json, with no check, whatever the agent changed", () => {
    const run = runExplore(
      `echo "agent-sees: $(cat built.txt)"\n` +
        `echo change >> app/search.py && git -c user.name=a -c user.email=a@a commit -qam change\n` +
        `${writeOutput(easyCall)}\n${success}`,
      "setup: echo built > built.txt\ncheck: echo checked\n",
    );

    expect(run.result, run.output).toEqual({
      contract_version: 1,
      run_id: 424,
      task: "explore",
      outcome: "succeeded",
      output: easyCall,
    });
    expect(run.output).toContain("agent-sees: built");
    expect(run.output).not.toContain("check: echo checked");
    expect(readdirSync(run.out)).toEqual(["result.json"]);
  });

  it.each([
    ["duplicate", { ...easyCall, verdict: "duplicate", duplicate_of: "97" }],
    ["needs-a-human", { ...easyCall, verdict: "needs-a-human", questions: ["Fold accents or transliterate?"] }],
    ["waiting", { ...easyCall, verdict: "waiting", questions: ["Which locale does the reporter use?"] }],
    ["too-big", { ...easyCall, verdict: "too-big" }],
  ])("accepts a %s verdict with its fields", (_, output) => {
    const run = runExplore(`${writeOutput(output)}\n${success}`);

    expect(run.result, run.output).toMatchObject({ outcome: "succeeded", output });
  });

  // The fake writes one output, then the corrected one when resumed.
  const fixedOnResume = (first: object, fixed: object) => `
if [[ " $* " == *" --resume s-1 "* ]]; then
  sed 's/^/resumed-with: /'
  ${writeOutput(fixed)}
else
  cat > /dev/null
  ${writeOutput(first)}
fi
${success}`;

  it.each([
    ["needs-a-human without questions", { ...easyCall, verdict: "needs-a-human" }],
    ["waiting with no questions", { ...easyCall, verdict: "waiting", questions: [] }],
    ["duplicate_of without duplicate", { ...easyCall, duplicate_of: "97" }],
    ["duplicate without duplicate_of", { ...easyCall, verdict: "duplicate" }],
    ["no findings", { ...easyCall, findings: undefined }],
  ])("gives %s back to the agent to fix", (_, first) => {
    const run = runExplore(fixedOnResume(first, easyCall));

    expect(run.result, run.output).toMatchObject({ outcome: "succeeded", output: easyCall });
    expect(run.output).toContain("resumed-with: ");
  });
});
