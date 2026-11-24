// The run script: runs one dispatch from start to finish, on any runner.
//
//   npx tsx runner/run-task.ts <dispatch.json> <out-dir> [<work-dir>]
//
// It validates the dispatch, checks out the target at base_sha and its
// .factory/ enrolment at enrolment_sha, builds the enrolment's image, runs
// setup, the agent (agent.ts) and the check in it, and writes
// out-dir/result.json, validated against contracts/result.schema.json, plus
// out-dir/branch.bundle when the agent committed on the dispatch's branch
// (commits.ts). Whatever happens after the dispatch is accepted, it still
// bundles, runs the check and writes a result: the "if: always()" steps live
// here, not in the workflow, so another runner can call this same script.
// See "Runners" in docs/software-factory.md.
//
// The runner (run.yml on Actions) only prepares the job, calls this script
// and uploads out-dir. Environment:
//
//   GITHUB_TOKEN        read-only token used to fetch the target, passed to
//                       git per command and never written to .git/config
//   CLAUDE_CODE_OAUTH_TOKEN  the Claude token: taken out of this process's
//                       environment at the start and passed to the agent's
//                       `docker exec` only
//   FACTORY_BOT_NAME, FACTORY_BOT_EMAIL  the App's bot identity, which
//                       authors the agent's commits (implement and review)
//   FACTORY_TARGET_URL  where to fetch the target from (default
//                       https://github.com/<repo>.git; tests use a local path)
//   FACTORY_BUILD_CACHE "gha" to build with the GitHub Actions layer cache
//   FACTORY_IMAGE_PREFIX the image's name before -<run_id> (default
//                       factory-run; tests make it unique per process)
//
// Exit code: 0 when the run succeeded, 1 when it failed (result.json says
// why), 2 when it couldn't write a valid result.json at all.

import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { parse as parseYaml } from "yaml";
import {
  AGENT_DIR, OLDER_ISSUES, OUTPUT, TICKET, putFile, runAgent, takeClaudeToken, type AgentOutcome,
} from "./agent";
import { botIdentity, bundleBranch, openBranch, type Identity } from "./commits";
import type { Check, Dispatch, Failure, Output, Result, TriggeredBy } from "./contract-types";
import { loadContracts, type Contract, type Contracts } from "./contract";
import { withPreviousAttempt } from "./previous-attempt";
import { expandIncludes } from "./prompt-includes";
import { CommandFailed, mustRun as must, run, type RunOptions } from "./spawn";
import { blockersFirst } from "./tasks/auto-tickets/output";
import * as review from "./tasks/review/review";

const RUNNER = dirname(fileURLToPath(import.meta.url));

const WORKSPACE = "/home/agent/workspace";

// Raised when a step fails in a way that has a `failure` reason.
class RunFailure extends Error {
  constructor(readonly failure: Failure, message: string) {
    super(message);
  }
}

const log = (message: string) => console.log(`[factory] ${message}`);

// Runs a command that must succeed; if it doesn't, the run fails with `failure`.
const mustRun = (failure: Failure, command: string, args: string[], options: RunOptions = {}) => {
  try {
    return must(command, args, options).stdout;
  } catch (error) {
    if (error instanceof CommandFailed) throw new RunFailure(failure, error.message);
    throw error;
  }
};

// The last 50 lines of a command's output, trimmed to fit the schema.
const outputTail = (output: string, lines = 50, maxLength = 20_000): string => {
  const parts = output.split("\n");
  const trailingNewline = parts.at(-1) === "";
  if (trailingNewline) parts.pop();
  let tail = parts.slice(-lines).join("\n") + (trailingNewline ? "\n" : "");
  if (tail.length > maxLength) tail = tail.slice(-maxLength);
  return tail;
};

// Step 2: the target at base_sha in <work>/target, and its .factory/ at
// enrolment_sha in <work>/enrolment. The token goes to git on the command
// line for each fetch, so it's never stored in the clone (the equivalent of
// actions/checkout's persist-credentials: false).
const checkout = (dispatch: Dispatch, work: string) => {
  const target = join(work, "target");
  const enrolment = join(work, "enrolment");
  mkdirSync(target, { recursive: true });
  mkdirSync(enrolment, { recursive: true });

  const url = process.env.FACTORY_TARGET_URL ?? `https://github.com/${dispatch.repo}.git`;
  const auth: string[] = [];
  const token = process.env.GITHUB_TOKEN;
  if (token) {
    const basic = Buffer.from(`x-access-token:${token}`).toString("base64");
    auth.push("-c", `http.extraheader=AUTHORIZATION: basic ${basic}`);
  }

  mustRun("setup_failed", "git", ["init", "-q", target]);
  mustRun("setup_failed", "git", [
    "-C", target, ...auth, "fetch", "-q", "--no-tags", url,
    dispatch.base_sha, dispatch.enrolment_sha, ...(dispatch.pr_base_sha ? [dispatch.pr_base_sha] : []),
  ]);
  mustRun("setup_failed", "git", ["-C", target, "checkout", "-q", "--detach", dispatch.base_sha]);
  const archive = run("git", ["-C", target, "archive", dispatch.enrolment_sha, ".factory"]);
  if (archive.status !== 0) {
    throw new RunFailure("setup_failed", `no .factory/ at ${dispatch.enrolment_sha}:\n${archive.stderr}`);
  }
  mustRun("setup_failed", "tar", ["-x", "-C", enrolment], { input: archive.bytes });
  return { target, factoryDir: join(enrolment, ".factory") };
};

type Enrolment = { setup: string; check: string; test_paths?: string[] };

const readEnrolment = (contract: Contract, factoryDir: string): Enrolment => {
  let config: unknown;
  try {
    config = parseYaml(readFileSync(join(factoryDir, "config.yml"), "utf8"));
  } catch (error) {
    throw new RunFailure("setup_failed", `can't read .factory/config.yml: ${error}`);
  }
  const errors = contract.validateEnrolment(config);
  if (errors) throw new RunFailure("setup_failed", `.factory/config.yml is invalid: ${errors}`);
  return config as Enrolment;
};

// Step 3: build the enrolment's Dockerfile, with .factory/ as the context.
const buildImage = (dispatch: Dispatch, factoryDir: string): string => {
  const tag = `${process.env.FACTORY_IMAGE_PREFIX ?? "factory-run"}-${dispatch.run_id}`;
  const cache =
    process.env.FACTORY_BUILD_CACHE === "gha"
      ? ["--cache-from", "type=gha,scope=factory-enrolment", "--cache-to", "type=gha,mode=max,scope=factory-enrolment"]
      : [];
  log(`building .factory/Dockerfile as ${tag}`);
  const build = run(
    "docker",
    ["buildx", "build", "--load", ...cache, "-t", tag, "-f", join(factoryDir, "Dockerfile"), factoryDir],
    { stdio: ["ignore", "inherit", "inherit"] },
  );
  if (build.status !== 0) throw new RunFailure("setup_failed", "the image didn't build");
  return tag;
};

// A container from the image with the target copied in at WORKSPACE and
// owned by the image's user. Copied, not mounted, so the image's user can
// write to it whatever user the runner is.
const startContainer = (image: string, target: string) => {
  const id = mustRun("setup_failed", "docker", [
    "run", "-d", "--entrypoint", "sleep", image, "infinity",
  ]).trim();
  const exec = (...args: string[]) => run("docker", ["exec", ...args]);
  const stop = () => run("docker", ["rm", "-f", id]);
  try {
    const owner = mustRun("setup_failed", "docker", ["exec", id, "sh", "-c", 'echo "$(id -u):$(id -g)"']).trim();
    mustRun("setup_failed", "docker", ["exec", "-u", "root", id, "mkdir", "-p", WORKSPACE]);
    mustRun("setup_failed", "docker", ["cp", `${target}/.`, `${id}:${WORKSPACE}`]);
    mustRun("setup_failed", "docker", ["exec", "-u", "root", id, "chown", "-R", owner, WORKSPACE]);
    // The agent's own folder, outside the repo so nothing in it is committed.
    mustRun("setup_failed", "docker", ["exec", "-u", "root", id, "install", "-d", "-o", owner.split(":")[0]!, AGENT_DIR]);
  } catch (error) {
    stop();
    throw error;
  }
  // Runs a shell command in the workspace with stderr folded into stdout.
  const sh = (name: string, command: string) => {
    log(`${name}: ${command}`);
    const result = exec("-w", WORKSPACE, id, "bash", "-c", `exec 2>&1\n${command}`);
    process.stdout.write(result.output);
    log(`${name} exited ${result.status}`);
    return result;
  };
  return { id, sh, stop };
};

const runCheck = (container: ReturnType<typeof startContainer>, enrolment: Enrolment): Check => {
  const result = container.sh("check", enrolment.check);
  return { passed: result.status === 0, output_tail: outputTail(result.output) };
};

const AGENT_TASKS = ["explore", "implement", "review", "auto-tickets"];
// Tasks that run the check after the agent. Review's result calls it
// check_after, and review also runs it before (tasks/review/review.ts).
const CHECKED_TASKS = ["implement", "review", "smoke"];
// Auto-tickets only reads the repo and writes text, so it needs no setup.
const NO_SETUP_TASKS = ["auto-tickets"];
// Rules a task's output must follow that its JSON Schema can't express.
const OUTPUT_RULES: Record<string, (output: unknown) => string | null> = {
  "auto-tickets": blockersFirst,
};

// The task's prompt (runner/tasks/<task>/prompt.md, from the task tickets)
// followed by what every agent task is told: where the ticket is, and the
// one file it writes. In the task's prompt, {{check}} is the enrolment's
// check command.
const taskPrompt = (contract: Contract, task: string, enrolment: Enrolment): string => {
  const file = join(RUNNER, "tasks", task, "prompt.md");
  const own = existsSync(file)
    ? expandIncludes(readFileSync(file, "utf8"), join(RUNNER, ".."))
        .trimEnd()
        .replaceAll("{{check}}", () => enrolment.check) + "\n\n"
    : "";
  return [
    `${own}## Your output`,
    "",
    `The ticket is in ${TICKET}. The repo is checked out in ${WORKSPACE}; use local git only.`,
    "",
    `When you're done, write your output to ${OUTPUT}: one JSON object matching this JSON Schema. Write nothing else there.`,
    "",
    "```json",
    JSON.stringify(contract.outputSchema(task), null, 2),
    "```",
    "",
  ].join("\n");
};

// Step 4: the agent. Its timeout is the job's (capped at 60 like run.yml)
// minus 5 minutes, so the steps after it still have time to run.
const agentStep = (
  contract: Contract, dispatch: Dispatch, enrolment: Enrolment, container: string, token: string | undefined,
): AgentOutcome => {
  if (dispatch.ticket_markdown !== undefined) {
    putFile(container, TICKET, withPreviousAttempt(dispatch.ticket_markdown, dispatch.previous_attempt));
  }
  if (dispatch.older_issues !== undefined) {
    const list = dispatch.older_issues.map((i) => `- #${i.ticket} ${i.title}\n`).join("");
    putFile(container, OLDER_ISSUES, list || "None.\n");
  }
  const minutes = Math.max(1, Math.min(dispatch.timeout_minutes, 60) - 5);
  return runAgent({
    container,
    token,
    workspace: WORKSPACE,
    model: dispatch.model!,
    prompt: taskPrompt(contract, dispatch.task, enrolment),
    timeoutSeconds: minutes * 60,
    validate: (output) =>
      contract.validateOutput(dispatch.task, output) ?? OUTPUT_RULES[dispatch.task]?.(output) ?? null,
  });
};

// The steps after the dispatch is accepted. It always returns a result.
const runTask = (
  contract: Contract, dispatch: Dispatch, work: string, outDir: string, token: string | undefined,
): Result => {
  const base = {
    contract_version: dispatch.contract_version,
    run_id: dispatch.run_id,
    task: dispatch.task,
  };

  let failure: Failure | undefined;
  let check: Check | undefined;
  let checkBefore: Check | undefined;
  let instructionsChanged = false;
  let output: Output | undefined;
  let container: ReturnType<typeof startContainer> | undefined;
  let enrolment: Enrolment | undefined;
  let branch: { name: string; identity: Identity; triggeredBy: TriggeredBy } | undefined;
  try {
    const { target, factoryDir } = checkout(dispatch, work);
    enrolment = readEnrolment(contract, factoryDir);
    if (dispatch.task === "review") {
      try {
        instructionsChanged = review.prepareCheckout(target, dispatch.pr_base_sha!);
      } catch (error) {
        throw new RunFailure("setup_failed", String(error));
      }
    }
    // Tasks that commit need the bot identity before anything runs.
    const identity = dispatch.branch === undefined ? undefined : botIdentity();
    if (typeof identity === "string") throw new RunFailure("setup_failed", identity);
    container = startContainer(buildImage(dispatch, factoryDir), target);
    if (!NO_SETUP_TASKS.includes(dispatch.task) && container.sh("setup", enrolment.setup).status !== 0) {
      throw new RunFailure("setup_failed", "setup failed");
    }
    // Review checks the PR as it came, so a failing PR reaches the agent as
    // something to fix.
    if (dispatch.task === "review") {
      checkBefore = runCheck(container, enrolment);
      putFile(container.id, review.BRIEF, review.brief(dispatch, enrolment, checkBefore));
    }
    if (dispatch.branch !== undefined && identity && dispatch.triggered_by) {
      openBranch(container.id, WORKSPACE, dispatch.branch, identity);
      branch = { name: dispatch.branch, identity, triggeredBy: dispatch.triggered_by };
    }
    if (AGENT_TASKS.includes(dispatch.task)) {
      const agent = agentStep(contract, dispatch, enrolment, container.id, token);
      if ("failure" in agent) throw new RunFailure(agent.failure, agent.reason);
      output = agent.output;
    }
  } catch (error) {
    if (!(error instanceof RunFailure)) throw error;
    log(error.message);
    failure = error.failure;
  }

  // Steps 5-7 run whatever happened above, as long as there's somewhere to
  // run them. A findings-only review uploads no commits, so there's nothing
  // to check after it either.
  const findingsOnly = dispatch.task === "review" && dispatch.findings_only === true;
  const checkAfter = CHECKED_TASKS.includes(dispatch.task) && !findingsOnly;
  try {
    if (container && branch && !findingsOnly) {
      // Even after a timeout: a partial bundle is still worth uploading.
      try {
        bundleBranch(container.id, WORKSPACE, {
          branch: branch.name,
          baseSha: dispatch.base_sha,
          identity: branch.identity,
          triggeredBy: branch.triggeredBy,
          outDir,
        });
      } catch (error) {
        log(String(error));
        failure ??= "agent_error";
      }
    }
    if (container && enrolment && checkAfter) {
      if (dispatch.task === "review") review.cleanWorkspace(container.id, WORKSPACE);
      check = runCheck(container, enrolment);
      if (!check.passed) failure ??= "check_failed";
    }
  } finally {
    container?.stop();
  }

  const checks =
    dispatch.task === "review"
      ? {
          ...(checkBefore && { check_before: checkBefore }),
          ...(check && { check_after: check }),
          ...(instructionsChanged && { instructions_changed: true as const }),
        }
      : { ...(check && { check }) };
  // A failed result keeps the agent's output if it wrote a valid one (a
  // failed check); the orchestrator decides whether to use it.
  return failure
    ? { ...base, outcome: "failed", failure, ...checks, ...(output && { output }) }
    : { ...base, outcome: "succeeded", ...checks, output: output ?? {} };
};

class Rejected extends Error {}

// Step 1: the dispatch, checked before anything else runs. A contract_version
// this runner doesn't accept fails at once with contract_mismatch, written in
// the current version (if the dispatch says which run it is). A dispatch that
// isn't JSON, or fails its version's schema, gets no result at all: the
// orchestrator sees a run that finished without files.
const accept = (
  contracts: Contracts,
  text: string,
): { dispatch: Dispatch; contract: Contract } | Result => {
  let data: any;
  try {
    data = JSON.parse(text);
  } catch (error) {
    throw new Rejected(`the dispatch isn't JSON: ${error}`);
  }
  const contract = contracts.get(data?.contract_version);
  if (!contract) {
    log(`contract_version ${data?.contract_version} isn't one of ${contracts.versions.join(", ")}`);
    const mismatch: Result = {
      contract_version: contracts.current.version,
      run_id: data?.run_id,
      task: data?.task,
      outcome: "failed",
      failure: "contract_mismatch",
    };
    if (contracts.current.validateResult(mismatch)) {
      throw new Rejected("the dispatch has an unknown contract_version and no usable run_id or task");
    }
    return mismatch;
  }
  const errors = contract.validateDispatch(data);
  if (errors) throw new Rejected(`the dispatch is invalid: ${errors}`);
  return { dispatch: data as Dispatch, contract };
};

const main = (argv: string[]): number => {
  // Before anything else runs, so no command inherits it.
  const token = takeClaudeToken();
  const [dispatchPath, outDir, workArg] = argv;
  if (!dispatchPath || !outDir) {
    console.error("usage: run-task.ts <dispatch.json> <out-dir> [<work-dir>]");
    return 2;
  }
  const work = workArg ?? mkdtempSync(join(tmpdir(), "factory-work-"));
  mkdirSync(outDir, { recursive: true });
  mkdirSync(work, { recursive: true });

  const contracts = loadContracts(process.env.FACTORY_CONTRACTS);
  let contract = contracts.current;
  let result: Result;
  try {
    const accepted = accept(contracts, readFileSync(dispatchPath, "utf8"));
    if ("dispatch" in accepted) {
      contract = accepted.contract;
      result = runTask(contract, accepted.dispatch, work, outDir, token);
    } else {
      result = accepted;
    }
  } catch (error) {
    if (!(error instanceof Rejected)) throw error;
    console.error(`[factory] ${error.message}`);
    return 2;
  }

  // The result is checked against the schema of the version it was sent in.
  const errors = contract.validateResult(result);
  if (errors) {
    console.error(`[factory] refusing to upload an invalid result.json: ${errors}`);
    console.error(JSON.stringify(result, null, 2));
    return 2;
  }
  writeFileSync(join(outDir, "result.json"), JSON.stringify(result, null, 2) + "\n");
  log(`result: ${result.outcome}${result.failure ? ` (${result.failure})` : ""}`);
  return result.outcome === "succeeded" ? 0 : 1;
};

process.exitCode = main(process.argv.slice(2));
