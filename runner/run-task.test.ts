// Tests the run script from the outside: each test makes a target repo in a
// local bare git repo, writes a dispatch, runs `runner/run-task.ts` as a
// subprocess and reads what it left in its output folder. They need git and
// Docker, so they aren't part of `npm test`: run `npm run test:runner`.
//
// The enrolment's image builds FROM $FACTORY_BASE_IMAGE (default
// factory-base:test, from `npm run build:image`).

import { execFileSync, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const baseImage = process.env.FACTORY_BASE_IMAGE ?? "factory-base:test";

const git = (cwd: string, ...args: string[]): string =>
  execFileSync("git", args, { cwd, encoding: "utf8" }).trim();

type Files = Record<string, string>;

// A bare repo standing in for the target on GitHub. Each call to commit()
// adds a commit with the given files and returns its sha.
const makeTarget = () => {
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
      mkdirSync(dirname(join(work, path)), { recursive: true });
      writeFileSync(join(work, path), content);
    }
    git(work, "add", "-A");
    git(work, "commit", "-q", "-m", "change");
    git(work, "push", "-q", "origin", "HEAD:main");
    return git(work, "rev-parse", "HEAD");
  };
  return { url: bare, commit };
};

const enrolment = (config: string): Files => ({
  ".factory/Dockerfile": `FROM ${baseImage}\n`,
  ".factory/config.yml": config,
});

const smokeDispatch = (sha: string, extra: object = {}) => ({
  contract_version: 1,
  run_id: 400,
  repo: "octo/widgets",
  task: "smoke",
  base_sha: sha,
  enrolment_sha: sha,
  timeout_minutes: 20,
  ...extra,
});

type Run = { status: number; output: string; out: string; result?: any };

const runTask = (dispatch: unknown, targetUrl: string, env: NodeJS.ProcessEnv = {}): Run => {
  const dir = mkdtempSync(join(tmpdir(), "factory-run-"));
  const dispatchPath = join(dir, "dispatch.json");
  writeFileSync(
    dispatchPath,
    typeof dispatch === "string" ? dispatch : JSON.stringify(dispatch),
  );
  const out = join(dir, "out");
  const work = join(dir, "work");
  const proc = spawnSync(
    "npx",
    ["tsx", join(root, "runner/run-task.ts"), dispatchPath, out, work],
    {
      cwd: root,
      encoding: "utf8",
      env: { ...process.env, FACTORY_TARGET_URL: targetUrl, ...env },
    },
  );
  const resultPath = join(out, "result.json");
  return {
    status: proc.status ?? -1,
    output: proc.stdout + proc.stderr,
    out,
    result: existsSync(resultPath) ? JSON.parse(readFileSync(resultPath, "utf8")) : undefined,
  };
};

describe("a smoke run", () => {
  it("builds the image, runs setup and the check, and writes a succeeded result", () => {
    const target = makeTarget();
    const sha = target.commit({
      ...enrolment("setup: echo set up > setup.txt\ncheck: cat setup.txt && echo 3 passed\n"),
    });

    const run = runTask(smokeDispatch(sha), target.url);

    expect(run.result, run.output).toEqual({
      contract_version: 1,
      run_id: 400,
      task: "smoke",
      outcome: "succeeded",
      check: { passed: true, output_tail: "set up\n3 passed\n" },
      output: {},
    });
    expect(run.status).toBe(0);
  });

  it("checks out the code at base_sha but takes .factory/ from enrolment_sha", () => {
    const target = makeTarget();
    const enrolmentSha = target.commit(enrolment("setup: \"true\"\ncheck: cat feature.txt\n"));
    const baseSha = target.commit({
      "feature.txt": "from base_sha\n",
      ".factory/config.yml": "setup: \"true\"\ncheck: exit 1\n",
    });

    const run = runTask(smokeDispatch(baseSha, { enrolment_sha: enrolmentSha }), target.url);

    expect(run.result?.check, run.output).toEqual({ passed: true, output_tail: "from base_sha\n" });
  });

  it("records a failed check with the last 50 lines of its output", () => {
    const target = makeTarget();
    const sha = target.commit(enrolment("setup: \"true\"\ncheck: seq 1 60; exit 3\n"));

    const run = runTask(smokeDispatch(sha), target.url);

    const lines = Array.from({ length: 50 }, (_, i) => `${i + 11}\n`).join("");
    expect(run.result, run.output).toEqual({
      contract_version: 1,
      run_id: 400,
      task: "smoke",
      outcome: "failed",
      failure: "check_failed",
      check: { passed: false, output_tail: lines },
    });
    expect(run.status).toBe(1);
  });

  it("still runs the check when setup fails, and records setup_failed", () => {
    const target = makeTarget();
    const sha = target.commit(enrolment("setup: exit 1\ncheck: echo checked\n"));

    const run = runTask(smokeDispatch(sha), target.url);

    expect(run.result, run.output).toMatchObject({
      outcome: "failed",
      failure: "setup_failed",
      check: { passed: true, output_tail: "checked\n" },
    });
  });

  it("records setup_failed when the enrolment's image doesn't build", () => {
    const target = makeTarget();
    const sha = target.commit({
      ".factory/Dockerfile": `FROM ${baseImage}\nRUN exit 1\n`,
      ".factory/config.yml": "setup: \"true\"\ncheck: \"true\"\n",
    });

    const run = runTask(smokeDispatch(sha), target.url);

    expect(run.result, run.output).toEqual({
      contract_version: 1,
      run_id: 400,
      task: "smoke",
      outcome: "failed",
      failure: "setup_failed",
    });
  });

  it("keeps the GitHub token out of the clone and out of setup and the check", () => {
    const target = makeTarget();
    const sha = target.commit(enrolment("setup: env\ncheck: env; git config --list\n"));

    const run = runTask(smokeDispatch(sha), target.url, { GITHUB_TOKEN: "ghs_secret_token" });

    expect(run.result?.outcome, run.output).toBe("succeeded");
    expect(run.output).not.toContain("ghs_secret_token");
    expect(run.output).not.toMatch(/extraheader/i);
  });
});

describe("the dispatch", () => {
  const unreachable = "/nonexistent/target.git";
  const sha = "4b825dc642cb6eb9a060e54bf8d69288fbee4904";

  it.each([0, 2, 99])(
    "fails at once with contract_mismatch for contract_version %s",
    (version) => {
      const run = runTask(smokeDispatch(sha, { contract_version: version }), unreachable);

      expect(run.result, run.output).toEqual({
        contract_version: 1,
        run_id: 400,
        task: "smoke",
        outcome: "failed",
        failure: "contract_mismatch",
      });
      expect(run.status).toBe(1);
    },
  );

  describe("after a contract_version bump", () => {
    // Contracts as they'd be after bumping to 2: today's schemas kept in
    // previous/, and the current ones saying 2.
    const bumped = () => {
      const dir = mkdtempSync(join(tmpdir(), "factory-contracts-"));
      mkdirSync(join(dir, "previous"));
      for (const name of ["dispatch", "result", "enrolment"]) {
        const text = readFileSync(join(root, "contracts", `${name}.schema.json`), "utf8");
        writeFileSync(join(dir, "previous", `${name}.schema.json`), text);
        const schema = JSON.parse(text);
        if (name === "dispatch") schema.$defs.contract_version.const = 2;
        writeFileSync(join(dir, `${name}.schema.json`), JSON.stringify(schema));
      }
      return dir;
    };

    it.each([1, 2])("runs a version %s dispatch and answers in that version", (version) => {
      const target = makeTarget();
      const sha = target.commit(enrolment("setup: \"true\"\ncheck: \"true\"\n"));

      const run = runTask(smokeDispatch(sha, { contract_version: version }), target.url, {
        FACTORY_CONTRACTS: bumped(),
      });

      expect(run.result, run.output).toMatchObject({ contract_version: version, outcome: "succeeded" });
    });

    it("still fails a version 3 dispatch with contract_mismatch", () => {
      const run = runTask(smokeDispatch(sha, { contract_version: 3 }), unreachable, {
        FACTORY_CONTRACTS: bumped(),
      });

      expect(run.result, run.output).toMatchObject({ contract_version: 2, failure: "contract_mismatch" });
    });
  });

  it("is rejected without a result when it fails the schema", () => {
    const run = runTask(smokeDispatch(sha, { model: "claude-opus" }), unreachable);

    expect(run.result).toBeUndefined();
    expect(run.output).toMatch(/dispatch is invalid/);
    expect(run.status).toBe(2);
  });

  it("is rejected without a result when it isn't JSON", () => {
    const run = runTask("{not json", unreachable);

    expect(run.result).toBeUndefined();
    expect(run.status).toBe(2);
  });
});

// The agent tasks run Claude Code in the container. These tests swap it for
// a fake `claude` (a bash script in the enrolment, copied over the real one),
// so no Claude usage is spent. The fake finds its files where the prompt
// says: the task's folder is /home/agent/factory, the repo is the workspace.
const FACTORY = "/home/agent/factory";

const fakeClaude = (script: string, config = "setup: \"true\"\ncheck: echo checked\n"): Files => ({
  ".factory/Dockerfile": `FROM ${baseImage}\nCOPY --chmod=755 fake-claude /usr/local/bin/claude\n`,
  ".factory/config.yml": config,
  ".factory/fake-claude": `#!/bin/bash\nset -u\n${script}\n`,
});

// One stream-json line as Claude Code prints it.
const event = (data: object) => `echo '${JSON.stringify({ session_id: "s-1", ...data })}'`;
const success = event({ type: "result", subtype: "success", is_error: false, result: "done" });

const implementOutput = { verdict: "built", pr_body: "Built it." };
const writeOutput = (output: unknown) =>
  `cat > ${FACTORY}/output.json <<'JSON'\n${typeof output === "string" ? output : JSON.stringify(output)}\nJSON`;

const implementDispatch = (sha: string, extra: object = {}) => ({
  contract_version: 1,
  run_id: 414,
  repo: "octo/widgets",
  task: "implement",
  ticket: "123",
  ticket_markdown: "# Search ignores accents\n",
  base_sha: sha,
  branch: "factory/123-414",
  enrolment_sha: sha,
  model: "claude-test-model",
  timeout_minutes: 45,
  triggered_by: { login: "octocat", id: 583231 },
  ...extra,
});

// What run.yml gives the script: the Claude token and the App's bot identity.
const bot = {
  FACTORY_BOT_NAME: "swfactory[bot]",
  FACTORY_BOT_EMAIL: "1234+swfactory[bot]@users.noreply.github.com",
};
const claudeEnv = { CLAUDE_CODE_OAUTH_TOKEN: "sk-ant-oat01-test-token", ...bot };

describe("the agent step", () => {
  it("runs the agent and puts its output.json in a succeeded result", () => {
    const target = makeTarget();
    const sha = target.commit(fakeClaude(`${writeOutput(implementOutput)}\n${success}`));

    const run = runTask(implementDispatch(sha), target.url, claudeEnv);

    expect(run.result, run.output).toEqual({
      contract_version: 1,
      run_id: 414,
      task: "implement",
      outcome: "succeeded",
      check: { passed: true, output_tail: "checked\n" },
      output: implementOutput,
    });
    expect(run.status).toBe(0);
  });

  it("gives the agent the Claude token and no GitHub credential, and setup and the check no Claude token", () => {
    const target = makeTarget();
    const sha = target.commit(
      fakeClaude(
        `env | sed 's/^/agent-env: /'; git config --list --show-origin | sed 's/^/agent-git: /'\n` +
          `${writeOutput(implementOutput)}\n${success}`,
        "setup: |\n  env | sed 's/^/setup-env: /'\ncheck: |\n  env | sed 's/^/check-env: /'\n",
      ),
    );

    const run = runTask(implementDispatch(sha), target.url, {
      ...claudeEnv,
      GITHUB_TOKEN: "ghs_secret_token",
    });

    expect(run.result?.outcome, run.output).toBe("succeeded");
    expect(run.output).toContain("agent-env: CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-test-token");
    expect(run.output).toMatch(/^agent-git: /m);
    expect(run.output).not.toContain("ghs_secret_token");
    expect(run.output).not.toMatch(/extraheader|credential/i);
    expect(run.output).toMatch(/^setup-env: /m);
    expect(run.output).not.toMatch(/^(setup|check)-env: CLAUDE/m);
  });

  it("hands the Claude token to the agent's docker exec and to no other command it runs", () => {
    // git, docker and tar on PATH log each call made with the token in its environment.
    const bin = mkdtempSync(join(tmpdir(), "factory-bin-"));
    const calls = join(bin, "calls.log");
    for (const command of ["git", "docker", "tar"]) {
      const real = execFileSync("sh", ["-c", `command -v ${command}`], { encoding: "utf8" }).trim();
      writeFileSync(
        join(bin, command),
        `#!/bin/sh\n[ -n "\${CLAUDE_CODE_OAUTH_TOKEN-}" ] && echo "${command} $*" >> ${calls}\nexec ${real} "$@"\n`,
        { mode: 0o755 },
      );
    }
    const target = makeTarget();
    const sha = target.commit(fakeClaude(`${writeOutput(implementOutput)}\n${success}`));

    const run = runTask(implementDispatch(sha), target.url, {
      ...claudeEnv,
      PATH: `${bin}:${process.env.PATH}`,
    });

    expect(run.result?.outcome, run.output).toBe("succeeded");
    const withToken = readFileSync(calls, "utf8").trim().split("\n");
    expect(withToken.length).toBeGreaterThan(0);
    for (const call of withToken) expect(call).toMatch(/^docker exec -d -e CLAUDE_CODE_OAUTH_TOKEN /);
  });

  it("gives the agent the ticket and its output's schema, and runs no check or bundle for explore", () => {
    const exploreOutput = {
      verdict: "easy-call",
      summary: "Fold accents.",
      findings: "search() compares raw strings.",
      plan: ["Fold accents"],
      files: ["app/search.py"],
    };
    const target = makeTarget();
    const sha = target.commit(
      fakeClaude(
        `sed 's/^/prompt: /'\nsed 's/^/ticket: /' ${FACTORY}/ticket.md\n` +
          `echo x > x.txt && git add x.txt && git -c user.name=a -c user.email=a@a commit -q -m x\n` +
          `${writeOutput(exploreOutput)}\n${success}`,
      ),
    );

    const run = runTask(
      {
        contract_version: 1,
        run_id: 412,
        repo: "octo/widgets",
        task: "explore",
        ticket: "123",
        ticket_markdown: "# Search ignores accents\n",
        older_issues: [],
        base_sha: sha,
        enrolment_sha: sha,
        model: "claude-test-model",
        timeout_minutes: 20,
      },
      target.url,
      claudeEnv,
    );

    expect(run.result, run.output).toEqual({
      contract_version: 1,
      run_id: 412,
      task: "explore",
      outcome: "succeeded",
      output: exploreOutput,
    });
    expect(run.output).toContain("ticket: # Search ignores accents");
    expect(run.output).toContain(`prompt: When you're done, write your output to ${FACTORY}/output.json`);
    expect(run.output).toMatch(/^prompt: .*"duplicate_of"/m);
    expect(existsSync(join(run.out, "branch.bundle"))).toBe(false);
  });

  it("records claude_auth without starting the agent when there's no Claude token", () => {
    const target = makeTarget();
    const sha = target.commit(fakeClaude(`echo agent-started\n${writeOutput(implementOutput)}\n${success}`));

    const run = runTask(implementDispatch(sha), target.url, { ...claudeEnv, CLAUDE_CODE_OAUTH_TOKEN: "" });

    expect(run.result, run.output).toMatchObject({ outcome: "failed", failure: "claude_auth" });
    expect(run.output).not.toContain("agent-started");
  });

  // What Claude Code 2.1.330 prints (trimmed) when the API refuses it.
  const apiError = (error: string, status: number, text: string) =>
    [
      event({ type: "assistant", error, api_error_status: status, is_api_error_message: true,
              message: { role: "assistant", content: [{ type: "text", text }] } }),
      event({ type: "result", subtype: "success", is_error: true, api_error_status: status, result: text }),
      "exit 1",
    ].join("\n");

  it.each([
    ["claude_auth", apiError("authentication_failed", 401, "Failed to authenticate. API Error: 401 Invalid bearer token")],
    ["claude_auth", [
      event({ type: "assistant", error: "authentication_failed", message: { content: [] } }),
      event({ type: "result", subtype: "success", is_error: true, api_error_status: null, result: "Not logged in" }),
      "exit 1",
    ].join("\n")],
    ["usage_limit", apiError("rate_limit", 429, "Claude AI usage limit reached")],
    ["agent_error", `${writeOutput(implementOutput)}\nexit 3`],
    ["agent_error", `${writeOutput(implementOutput)}\n${event({ type: "result", subtype: "error_during_execution", is_error: true })}`],
    ["agent_error", writeOutput(implementOutput)],
    ["no_output", success],
  ])("records %s", (failure, script) => {
    const target = makeTarget();
    const sha = target.commit(fakeClaude(script));

    const run = runTask(implementDispatch(sha), target.url, claudeEnv);

    expect(run.result, run.output).toEqual({
      contract_version: 1,
      run_id: 414,
      task: "implement",
      outcome: "failed",
      failure,
      check: { passed: true, output_tail: "checked\n" },
    });
  });

  // The fake answers its first session one way and a resumed one another.
  const firstThenResumed = (first: string, resumed: string) => `
if [[ " $* " == *" --resume s-1 "* ]]; then
  sed 's/^/resumed-with: /'
  ${resumed}
else
  cat > /dev/null
  ${first}
fi
${success}`;

  it("gives an invalid output.json back to the agent once, in the same session", () => {
    const target = makeTarget();
    const sha = target.commit(
      fakeClaude(firstThenResumed(writeOutput({ verdcit: "built" }), writeOutput(implementOutput))),
    );

    const run = runTask(implementDispatch(sha), target.url, claudeEnv);

    expect(run.result, run.output).toMatchObject({ outcome: "succeeded", output: implementOutput });
    expect(run.output).toMatch(/^resumed-with: .*verdict/m);
  });

  it.each([
    ["fails the schema twice", writeOutput({ verdcit: "built" })],
    ["isn't JSON, then is deleted", `rm ${FACTORY}/output.json`],
  ])("records invalid_output when output.json %s", (_, resumed) => {
    const target = makeTarget();
    const sha = target.commit(fakeClaude(firstThenResumed(writeOutput("{not json"), resumed)));

    const run = runTask(implementDispatch(sha), target.url, claudeEnv);

    expect(run.result, run.output).toMatchObject({ outcome: "failed", failure: "invalid_output" });
    expect(run.output).toContain("resumed-with: ");
    expect(run.output).not.toContain("agent session 3");
  });
});

describe("the agent's commits", () => {
  // The fake commits like an agent would, with whatever identity it likes.
  const commit = (file: string) =>
    `echo ${file} > ${file} && git add ${file} && git commit -q -m "Add ${file}" -m "Because." ` +
    `--author "Agent Smith <smith@example.com>"`;

  // A clone of the target at base_sha with the run's bundle fetched into it.
  const unbundle = (target: { url: string }, run: Run) => {
    const dir = mkdtempSync(join(tmpdir(), "factory-unbundle-"));
    git(dir, "clone", "-q", target.url, "clone");
    const clone = join(dir, "clone");
    const bundle = join(run.out, "branch.bundle");
    return {
      verify: git(clone, "bundle", "verify", bundle),
      heads: git(clone, "bundle", "list-heads", bundle),
      log: (format: string) => {
        git(clone, "fetch", "-q", bundle, "refs/heads/factory/123-414:refs/heads/fetched");
        return git(clone, "log", `--format=${format}`, "main..fetched");
      },
    };
  };

  it("bundles the commits on the named branch, authored by the App's bot with the triggering human as co-author", () => {
    const target = makeTarget();
    const base = target.commit(
      fakeClaude(`${commit("one.txt")}\n${commit("two.txt")}\necho stray > stray.txt\n${writeOutput(implementOutput)}\n${success}`),
    );

    const run = runTask(
      implementDispatch(base, { triggered_by: { login: "octocat", id: 583231 } }),
      target.url,
      claudeEnv,
    );

    expect(run.result?.outcome, run.output).toBe("succeeded");
    const bundle = unbundle(target, run);
    expect(bundle.heads).toMatch(/^[0-9a-f]{40} refs\/heads\/factory\/123-414$/);
    expect(bundle.verify).toContain(base);
    expect(bundle.log("%an <%ae>|%cn <%ce>|%s").split("\n")).toEqual([
      "swfactory[bot] <1234+swfactory[bot]@users.noreply.github.com>|swfactory[bot] <1234+swfactory[bot]@users.noreply.github.com>|Add two.txt",
      "swfactory[bot] <1234+swfactory[bot]@users.noreply.github.com>|swfactory[bot] <1234+swfactory[bot]@users.noreply.github.com>|Add one.txt",
    ]);
    expect(bundle.log("%B")).toBe(
      ["Add two.txt", "", "Because.", "", "Co-Authored-By: octocat <583231+octocat@users.noreply.github.com>", "",
       "Add one.txt", "", "Because.", "", "Co-Authored-By: octocat <583231+octocat@users.noreply.github.com>"].join("\n"),
    );
  });

  it("uploads no bundle when the agent made no commits", () => {
    const target = makeTarget();
    const base = target.commit(fakeClaude(`${writeOutput(implementOutput)}\n${success}`));

    const run = runTask(implementDispatch(base), target.url, claudeEnv);

    expect(run.result?.outcome, run.output).toBe("succeeded");
    expect(existsSync(join(run.out, "branch.bundle"))).toBe(false);
  });

  it("refuses to run a task that commits without the App's bot identity", () => {
    const target = makeTarget();
    const base = target.commit(fakeClaude(`echo agent-started\n${success}`));

    const run = runTask(implementDispatch(base), target.url, {
      CLAUDE_CODE_OAUTH_TOKEN: "sk-ant-oat01-test-token",
      FACTORY_BOT_NAME: "",
      FACTORY_BOT_EMAIL: "",
    });

    expect(run.result, run.output).toMatchObject({ outcome: "failed", failure: "setup_failed" });
    expect(run.output).not.toContain("agent-started");
  });

  // Slow: the shortest agent timeout is a minute (a 6-minute task).
  it("times the agent out at the task's timeout minus 5 minutes, and still bundles and checks", () => {
    const target = makeTarget();
    const base = target.commit(
      fakeClaude(`${commit("one.txt")}\n${event({ type: "system", subtype: "init" })}\nsleep 600\n${success}`),
    );

    const started = Date.now();
    const run = runTask(implementDispatch(base, { timeout_minutes: 6 }), target.url, claudeEnv);
    const seconds = (Date.now() - started) / 1000;

    expect(run.result, run.output).toEqual({
      contract_version: 1,
      run_id: 414,
      task: "implement",
      outcome: "failed",
      failure: "agent_timeout",
      check: { passed: true, output_tail: "checked\n" },
    });
    expect(seconds).toBeGreaterThan(60);
    expect(seconds).toBeLessThan(120);
    expect(unbundle(target, run).log("%s")).toBe("Add one.txt");
  });
});

describe("an auto-tickets run", () => {
  const ticketsOutput = {
    verdict: "decomposed",
    tickets: [
      {
        title: "Store saved searches",
        what_to_build: "A table and a model to create, list and delete saved searches.",
        acceptance_criteria: ["A user can save the current query under a name"],
        blocked_by: [],
      },
      {
        title: "Show saved searches in the sidebar",
        what_to_build: "List saved searches in the sidebar; clicking one runs it.",
        acceptance_criteria: ["The sidebar lists saved searches by name"],
        blocked_by: [0],
        related: ["123"],
      },
    ],
  };

  const autoTicketsDispatch = (sha: string, extra: object = {}) => ({
    contract_version: 1,
    run_id: 431,
    repo: "octo/widgets",
    task: "auto-tickets",
    ticket: "140",
    ticket_markdown: "# Spec: saved searches\n\n## Spec exploration notes\n\nNotes.\n",
    older_issues: [
      { ticket: "123", title: "Search ignores accents" },
      { ticket: "97", title: "Sidebar is slow" },
    ],
    base_sha: sha,
    enrolment_sha: sha,
    model: "claude-test-model",
    timeout_minutes: 20,
    ...extra,
  });

  it("skips setup, runs no check, bundles nothing and writes the tickets", () => {
    const target = makeTarget();
    const sha = target.commit(
      fakeClaude(
        `${writeOutput(ticketsOutput)}\n${success}`,
        "setup: echo setup-ran; exit 1\ncheck: echo check-ran\n",
      ),
    );

    const run = runTask(autoTicketsDispatch(sha), target.url, claudeEnv);

    expect(run.result, run.output).toEqual({
      contract_version: 1,
      run_id: 431,
      task: "auto-tickets",
      outcome: "succeeded",
      output: ticketsOutput,
    });
    expect(run.status).toBe(0);
    expect(run.output).not.toContain("setup-ran");
    expect(run.output).not.toContain("check-ran");
    expect(existsSync(join(run.out, "branch.bundle"))).toBe(false);
  });

  it("prompts with /to-tickets' slicing rules and template but no quiz, and gives it the older issues", () => {
    const target = makeTarget();
    const sha = target.commit(
      fakeClaude(
        `sed 's/^/prompt: /'\nsed 's/^/older: /' ${FACTORY}/older-issues.md\n` +
          `${writeOutput(ticketsOutput)}\n${success}`,
      ),
    );

    const run = runTask(autoTicketsDispatch(sha), target.url, claudeEnv);

    expect(run.result?.outcome, run.output).toBe("succeeded");
    const prompt = run.output
      .split("\n")
      .filter((line) => line.startsWith("prompt: "))
      .map((line) => line.slice("prompt: ".length))
      .join("\n");
    // From the /to-tickets skill, word for word.
    expect(prompt).toContain("Each slice cuts a narrow but COMPLETE path through every layer");
    expect(prompt).toContain("Wide refactors are the exception to vertical slicing.");
    expect(prompt).toContain("## Acceptance criteria\n\n- [ ] Criterion 1");
    expect(prompt).not.toMatch(/quiz|Iterate until the user approves/i);
    expect(prompt).toContain(`${FACTORY}/older-issues.md`);
    expect(prompt).toContain("Related: #M");
    expect(prompt).toMatch(/more than 12/);
    expect(prompt).toContain('"blocked_by"');
    expect(run.output).toContain("older: - #123 Search ignores accents\nolder: - #97 Sidebar is slow\n");
  });

  it("gives tickets that aren't blockers-first back to the agent as invalid output", () => {
    const [first, second] = ticketsOutput.tickets;
    const backwards = { verdict: "decomposed", tickets: [{ ...first, blocked_by: [1] }, { ...second, blocked_by: [] }] };
    const selfBlocked = { verdict: "decomposed", tickets: [first, { ...second, blocked_by: [1] }] };
    const target = makeTarget();
    const sha = target.commit(
      fakeClaude(`
if [[ " $* " == *" --resume s-1 "* ]]; then
  sed 's/^/resumed-with: /'
  ${writeOutput(selfBlocked)}
else
  cat > /dev/null
  ${writeOutput(backwards)}
fi
${success}`),
    );

    const run = runTask(autoTicketsDispatch(sha), target.url, claudeEnv);

    expect(run.result, run.output).toMatchObject({ outcome: "failed", failure: "invalid_output" });
    expect(run.output).toMatch(/^resumed-with: .*\/tickets\/0\/blocked_by .*earlier ticket/m);
  });
});
