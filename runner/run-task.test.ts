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
