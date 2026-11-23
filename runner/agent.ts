// The agent step: runs Claude Code in the run's container and turns whatever
// it does into either the task's output or a failure reason. See step 4 of
// "Runners" in docs/software-factory.md.
//
// The agent gets the Claude token and nothing else from the runner's
// environment. The run script takes the token out of its own environment
// before anything runs (`takeClaudeToken`) and hands it here; only the
// agent's `docker exec -e CLAUDE_CODE_OAUTH_TOKEN` gets it, by name in that
// one command's environment, so its value isn't on a command line either.
// Setup, the check, git and every other command never get it.
//
// Claude Code runs detached (`docker exec -d`) with its output in a file,
// polled until it exits. Run attached, `docker exec` returns at once with
// status 0 as soon as Claude Code starts (seen with 2.1.330 under Docker
// Desktop), while it carries on in the container.

import { run } from "./spawn";

export const AGENT_DIR = "/home/agent/factory";
export const OUTPUT = `${AGENT_DIR}/output.json`;
export const TICKET = `${AGENT_DIR}/ticket.md`;
// The dispatch's older open issues (explore, auto-tickets), one "- #N title" line each.
export const OLDER_ISSUES = `${AGENT_DIR}/older-issues.md`;

import type { AgentFailure } from "./contract-types";

export type AgentOutcome = { output: object } | { failure: AgentFailure; reason: string };

export type AgentOptions = {
  container: string;
  // The Claude token (`takeClaudeToken`), or undefined if there's none.
  token: string | undefined;
  workspace: string;
  model: string;
  prompt: string;
  timeoutSeconds: number;
  // null when the output is valid, otherwise the errors.
  validate: (output: unknown) => string | null;
};

const log = (message: string) => console.log(`[factory] ${message}`);

const sleep = (ms: number) => Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);

// Removes the Claude token from this process's environment, so no command it
// runs inherits it, and returns it for the agent alone.
export const takeClaudeToken = (): string | undefined => {
  const token = process.env.CLAUDE_CODE_OAUTH_TOKEN || undefined;
  delete process.env.CLAUDE_CODE_OAUTH_TOKEN;
  return token;
};

const docker = (args: string[], input?: string, env?: NodeJS.ProcessEnv) => run("docker", args, { input, env });

const quote = (arg: string) => `'${arg.replace(/'/g, `'\\''`)}'`;

// Writes a file in the container as the image's user.
export const putFile = (container: string, path: string, content: string) => {
  const put = docker(["exec", "-i", container, "sh", "-c", `cat > ${quote(path)}`], content);
  if (put.status !== 0) throw new Error(`can't write ${path} in the container: ${put.stderr}`);
};

type Session = { exit: number; timedOut: boolean; events: any[] };

// One `claude -p` session, until it exits or the deadline passes. The
// transcript (stream-json) goes to this script's output as it arrives.
const runSession = (options: AgentOptions, n: number, args: string[], prompt: string, deadline: number): Session => {
  const { container } = options;
  const promptFile = `${AGENT_DIR}/prompt-${n}.md`;
  const transcript = `${AGENT_DIR}/session-${n}.jsonl`;
  const exitFile = `${AGENT_DIR}/session-${n}.exit`;
  putFile(container, promptFile, prompt);

  const seconds = Math.max(1, Math.floor((deadline - Date.now()) / 1000));
  const claude = ["claude", ...args].map(quote).join(" ");
  const script = `timeout -k 10 ${seconds} ${claude} < ${promptFile} > ${transcript} 2>&1; echo $? > ${exitFile}`;
  log(`agent session ${n}: claude ${args.join(" ")} (${seconds}s left)`);
  const start = docker(
    ["exec", "-d", "-e", "CLAUDE_CODE_OAUTH_TOKEN", "-w", options.workspace, container, "bash", "-c", script],
    undefined,
    { ...process.env, CLAUDE_CODE_OAUTH_TOKEN: options.token },
  );
  if (start.status !== 0) throw new Error(`can't start the agent: ${start.stderr}`);

  let printed = 0;
  let text = "";
  const readNew = () => {
    const chunk = docker(["exec", container, "tail", "-c", `+${printed + 1}`, transcript]).stdout;
    printed += Buffer.byteLength(chunk);
    text += chunk;
    process.stdout.write(chunk);
  };
  let exit: number | undefined;
  for (;;) {
    sleep(1000);
    readNew();
    const done = docker(["exec", container, "cat", exitFile]);
    if (done.status === 0 && done.stdout.trim()) {
      exit = Number(done.stdout.trim());
      readNew();
      break;
    }
    // A backstop in case `timeout` itself can't end it.
    if (Date.now() > deadline + 30_000) break;
  }
  const timedOut = exit === undefined || exit === 124 || exit === 137;
  if (timedOut) {
    // Whatever the agent left running (as the image's user, which can't
    // signal the container's PID 1) goes too, before the commits are read.
    docker(["exec", container, "kill", "-9", "-1"]);
  }
  log(`agent session ${n} ${timedOut ? "timed out" : `exited ${exit}`}`);

  const events = text
    .split("\n")
    .flatMap((line) => {
      try {
        const event = JSON.parse(line);
        return event && typeof event === "object" ? [event] : [];
      } catch {
        return [];
      }
    });
  return { exit: exit ?? 124, timedOut, events };
};

// Why a session failed, or null when it finished cleanly. Claude Code marks
// an API failure on its synthetic assistant message (`error`) and on the
// result (`api_error_status`).
const classify = (session: Session): { failure: AgentFailure; reason: string } | null => {
  const result = session.events.filter((e) => e.type === "result").at(-1);
  const errors = session.events.filter((e) => e.type === "assistant" && e.error).map((e) => e.error);
  const said = typeof result?.result === "string" ? `: ${result.result}` : "";
  if (errors.includes("authentication_failed") || result?.api_error_status === 401) {
    return { failure: "claude_auth", reason: `Claude rejected the token${said}` };
  }
  if (errors.includes("rate_limit") || result?.api_error_status === 429) {
    return { failure: "usage_limit", reason: `hit the Claude usage limit${said}` };
  }
  if (session.timedOut) return { failure: "agent_timeout", reason: "the agent step timed out" };
  if (!result) return { failure: "agent_error", reason: `Claude Code exited ${session.exit} without a result` };
  if (result.is_error || session.exit !== 0) {
    return { failure: "agent_error", reason: `Claude Code exited ${session.exit}${said}` };
  }
  return null;
};

type Read = { output: object } | { missing: true } | { errors: string };

const readOutput = (options: AgentOptions): Read => {
  const file = docker(["exec", options.container, "cat", OUTPUT]);
  if (file.status !== 0) return { missing: true };
  let data: unknown;
  try {
    data = JSON.parse(file.stdout);
  } catch (error) {
    return { errors: `output.json isn't JSON: ${error}` };
  }
  const errors = options.validate(data);
  return errors ? { errors } : { output: data as object };
};

const fixPrompt = (errors: string) =>
  [
    `${OUTPUT} doesn't match its JSON Schema:`,
    "",
    errors,
    "",
    `Correct it and write the whole file again to ${OUTPUT}. Change nothing else.`,
    "",
  ].join("\n");

// Runs the agent with the prompt, and gives it one more turn in the same
// session if its output.json fails the schema.
export const runAgent = (options: AgentOptions): AgentOutcome => {
  if (!options.token) {
    return { failure: "claude_auth", reason: "CLAUDE_CODE_OAUTH_TOKEN isn't set" };
  }
  const deadline = Date.now() + options.timeoutSeconds * 1000;
  const args = [
    "-p", "--output-format", "stream-json", "--verbose",
    "--model", options.model, "--dangerously-skip-permissions",
  ];

  const first = runSession(options, 1, args, options.prompt, deadline);
  const failed = classify(first);
  if (failed) return failed;
  const read = readOutput(options);
  if ("output" in read) return read;
  if ("missing" in read) return { failure: "no_output", reason: `the agent didn't write ${OUTPUT}` };

  log(`output.json is invalid, asking the agent to fix it: ${read.errors}`);
  const sessionId = first.events.find((e) => typeof e.session_id === "string")?.session_id;
  if (!sessionId) return { failure: "invalid_output", reason: `${read.errors} (and no session to resume)` };
  const second = runSession(options, 2, [...args, "--resume", sessionId], fixPrompt(read.errors), deadline);
  const failedAgain = classify(second);
  if (failedAgain) return failedAgain;
  const reread = readOutput(options);
  if ("output" in reread) return reread;
  return {
    failure: "invalid_output",
    reason: "missing" in reread ? `the agent removed ${OUTPUT}` : reread.errors,
  };
};
