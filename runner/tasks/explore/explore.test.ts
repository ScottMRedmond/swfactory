// The explore task, tested from the outside like run-task.test.ts: a target
// in a local bare repo, a fake `claude` in its enrolment, and the run script
// as a subprocess. Needs git, Docker and factory-base:test
// (`npm run build:image`); run with `npm run test:runner`.

import { execFileSync, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const root = join(dirname(fileURLToPath(import.meta.url)), "../../..");
const baseImage = process.env.FACTORY_BASE_IMAGE ?? "factory-base:test";
const FACTORY = "/home/agent/factory";

const git = (cwd: string, ...args: string[]): string =>
  execFileSync("git", args, { cwd, encoding: "utf8" }).trim();

// A target repo whose one commit holds the enrolment with a fake `claude`.
const makeTarget = (fakeClaude: string, config: string) => {
  const dir = mkdtempSync(join(tmpdir(), "factory-target-"));
  const bare = join(dir, "target.git");
  const work = join(dir, "work");
  git(dir, "init", "-q", "--bare", "-b", "main", bare);
  git(dir, "init", "-q", "-b", "main", work);
  const files: Record<string, string> = {
    ".factory/Dockerfile": `FROM ${baseImage}\nCOPY --chmod=755 fake-claude /usr/local/bin/claude\n`,
    ".factory/config.yml": config,
    ".factory/fake-claude": `#!/bin/bash\nset -u\n${fakeClaude}\n`,
    "app/search.py": "def search(q, names):\n    return [n for n in names if q in n]\n",
  };
  for (const [path, content] of Object.entries(files)) {
    mkdirSync(dirname(join(work, path)), { recursive: true });
    writeFileSync(join(work, path), content);
  }
  git(work, "add", "-A");
  git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init");
  git(work, "push", "-q", bare, "HEAD:main");
  return { url: bare, sha: git(work, "rev-parse", "HEAD") };
};

// The valid explore dispatch from contracts/examples, pointed at the target.
const exploreDispatch = (sha: string) => ({
  ...JSON.parse(readFileSync(join(root, "contracts/examples/dispatch/explore.valid.json"), "utf8")),
  base_sha: sha,
  enrolment_sha: sha,
});

const runExplore = (fakeClaude: string, config = 'setup: "true"\ncheck: echo checked\n') => {
  const target = makeTarget(fakeClaude, config);
  const dir = mkdtempSync(join(tmpdir(), "factory-run-"));
  writeFileSync(join(dir, "dispatch.json"), JSON.stringify(exploreDispatch(target.sha)));
  const out = join(dir, "out");
  const proc = spawnSync(
    "npx",
    ["tsx", join(root, "runner/run-task.ts"), join(dir, "dispatch.json"), out, join(dir, "work")],
    {
      cwd: root,
      encoding: "utf8",
      env: { ...process.env, FACTORY_TARGET_URL: target.url, CLAUDE_CODE_OAUTH_TOKEN: "sk-ant-oat01-test" },
    },
  );
  const resultPath = join(out, "result.json");
  return {
    status: proc.status,
    output: proc.stdout + proc.stderr,
    out,
    result: existsSync(resultPath) ? JSON.parse(readFileSync(resultPath, "utf8")) : undefined,
  };
};

const event = (data: object) => `echo '${JSON.stringify({ session_id: "s-1", ...data })}'`;
const success = event({ type: "result", subtype: "success", is_error: false, result: "done" });
const writeOutput = (output: object) =>
  `cat > ${FACTORY}/output.json <<'JSON'\n${JSON.stringify(output)}\nJSON`;

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
      run_id: 412,
      task: "explore",
      outcome: "succeeded",
      output: easyCall,
    });
    expect(run.output).toContain("ticket: # Search ignores accents");
    expect(run.output).toContain("older: - 118: Search is slow on large lists");
    expect(run.output).toContain("older: - 97: Unicode names sort wrongly");
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
      run_id: 412,
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
