// The run script: runs one dispatch from start to finish, on any runner.
//
//   npx tsx runner/run-task.ts <dispatch.json> <out-dir> [<work-dir>]
//
// It validates the dispatch, checks out the target at base_sha and its
// .factory/ enrolment at enrolment_sha, builds the enrolment's image, runs
// setup and the check in it, and writes out-dir/result.json, validated
// against contracts/result.schema.json. Whatever happens after the dispatch
// is accepted, it still runs the check and writes a result: the "if:
// always()" steps live here, not in the workflow, so another runner can call
// this same script. See "Runners" in docs/software-factory.md.
//
// The runner (run.yml on Actions) only prepares the job, calls this script
// and uploads out-dir. Environment:
//
//   GITHUB_TOKEN        read-only token used to fetch the target, passed to
//                       git per command and never written to .git/config
//   FACTORY_TARGET_URL  where to fetch the target from (default
//                       https://github.com/<repo>.git; tests use a local path)
//   FACTORY_BUILD_CACHE "gha" to build with the GitHub Actions layer cache
//
// Exit code: 0 when the run succeeded, 1 when it failed (result.json says
// why), 2 when it couldn't write a valid result.json at all.

import { spawnSync, type SpawnSyncOptions } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { parse as parseYaml } from "yaml";
import { loadContracts, type Contract, type Contracts } from "./contract";

type Dispatch = {
  contract_version: number;
  run_id: number;
  repo: string;
  task: string;
  base_sha: string;
  enrolment_sha: string;
  timeout_minutes: number;
};

type Check = { passed: boolean; output_tail: string };

type Result = {
  contract_version: number;
  run_id: number;
  task: string;
  outcome: "succeeded" | "failed";
  failure?: string;
  check?: Check;
  output?: object;
};

const WORKSPACE = "/home/agent/workspace";

// Raised when a step fails in a way that has a `failure` reason.
class RunFailure extends Error {
  constructor(readonly failure: string, message: string) {
    super(message);
  }
}

const log = (message: string) => console.log(`[factory] ${message}`);

const run = (
  command: string,
  args: string[],
  options: SpawnSyncOptions = {},
): { status: number; output: string } => {
  const proc = spawnSync(command, args, {
    encoding: "utf8",
    maxBuffer: 256 * 1024 * 1024,
    ...options,
  });
  if (proc.error) throw proc.error;
  return {
    status: proc.status ?? 1,
    output: String(proc.stdout ?? "") + String(proc.stderr ?? ""),
  };
};

const mustRun = (failure: string, command: string, args: string[], options: SpawnSyncOptions = {}) => {
  const result = run(command, args, options);
  if (result.status !== 0) {
    throw new RunFailure(failure, `${command} ${args.join(" ")} failed:\n${result.output}`);
  }
  return result.output;
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
    dispatch.base_sha, dispatch.enrolment_sha,
  ]);
  mustRun("setup_failed", "git", ["-C", target, "checkout", "-q", "--detach", dispatch.base_sha]);
  const archive = spawnSync("git", ["-C", target, "archive", dispatch.enrolment_sha, ".factory"]);
  if (archive.status !== 0) {
    throw new RunFailure("setup_failed", `no .factory/ at ${dispatch.enrolment_sha}:\n${archive.stderr}`);
  }
  mustRun("setup_failed", "tar", ["-x", "-C", enrolment], { input: archive.stdout });
  return { target, factoryDir: join(enrolment, ".factory") };
};

type Enrolment = { setup: string; check: string };

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
  const tag = `factory-run-${dispatch.run_id}`;
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
  return { sh, stop };
};

// The steps after the dispatch is accepted. It always returns a result.
const runTask = (contract: Contract, dispatch: Dispatch, work: string): Result => {
  const base = {
    contract_version: dispatch.contract_version,
    run_id: dispatch.run_id,
    task: dispatch.task,
  };

  let failure: string | undefined;
  let check: Check | undefined;
  let container: ReturnType<typeof startContainer> | undefined;
  let enrolment: Enrolment | undefined;
  try {
    const { target, factoryDir } = checkout(dispatch, work);
    enrolment = readEnrolment(contract, factoryDir);
    container = startContainer(buildImage(dispatch, factoryDir), target);
    if (container.sh("setup", enrolment.setup).status !== 0) {
      failure = "setup_failed";
    }
  } catch (error) {
    if (!(error instanceof RunFailure)) throw error;
    log(error.message);
    failure = error.failure;
  }

  // Runs whatever happened above, as long as there's somewhere to run it.
  try {
    if (container && enrolment) {
      const result = container.sh("check", enrolment.check);
      check = { passed: result.status === 0, output_tail: outputTail(result.output) };
      if (!check.passed) failure ??= "check_failed";
    }
  } finally {
    container?.stop();
  }

  return failure
    ? { ...base, outcome: "failed", failure, ...(check && { check }) }
    : { ...base, outcome: "succeeded", check, output: {} };
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
      result = runTask(contract, accepted.dispatch, work);
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
