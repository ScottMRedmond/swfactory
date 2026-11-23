// The runner contract's types (contracts/*.schema.json), in one place.
//
// ADR 0001: types on the task side are generated from the schemas or tested
// against them, never written independently. These are written by hand and
// pinned to the schemas in two steps: below, each type's keys (and which of
// them are required) and its literal values are listed in constants the
// compiler holds to the type, exactly; contract-types.test.ts holds those
// constants to the schemas. A schema change that isn't made here fails the
// test, and a change here that isn't made in the constants fails typecheck.

export type Task = "explore" | "implement" | "review" | "auto-tickets" | "smoke";

// result.schema.json $defs/failure.
export type Failure =
  | "setup_failed"
  | "agent_error"
  | "agent_timeout"
  | "no_output"
  | "invalid_output"
  | "check_failed"
  | "contract_mismatch"
  | "claude_auth"
  | "usage_limit";

// The failures the agent step reports (agent.ts).
export type AgentFailure = Extract<
  Failure,
  "agent_error" | "agent_timeout" | "no_output" | "invalid_output" | "claude_auth" | "usage_limit"
>;

// dispatch.schema.json $defs/triggered_by.
export type TriggeredBy = { login: string; id: number };

// dispatch.schema.json $defs/previous_attempt.
export type PreviousAttempt = {
  outcome: "failed" | "invalid";
  failure?: Failure;
  check_output?: string;
  validation_error?: string;
};

// dispatch.schema.json $defs/older_issues items.
export type OlderIssue = { ticket: string; title: string };

// A dispatch of any task: the union of the per-task $defs in
// dispatch.schema.json, flattened, so a field one task lacks is optional.
// The schema says which task has which; the run script reads them by task.
export type Dispatch = {
  contract_version: number;
  run_id: number;
  repo: string;
  task: Task;
  base_sha: string;
  enrolment_sha: string;
  timeout_minutes: number;
  // Agent tasks only.
  ticket?: string;
  ticket_markdown?: string;
  previous_attempt?: PreviousAttempt;
  model?: string;
  branch?: string;
  triggered_by?: TriggeredBy;
  older_issues?: OlderIssue[];
  // Review only.
  pr_number?: number;
  pr_base_sha?: string;
  findings_only?: boolean;
};

// result.schema.json $defs/check.
export type Check = { passed: boolean; output_tail: string };

// result.schema.json. `output` is the task's own sub-schema (contract.ts
// validates it against the task's).
export type Result = {
  contract_version: number;
  run_id: number;
  task: Task;
  outcome: "succeeded" | "failed";
  failure?: Failure;
  check?: Check;
  check_before?: Check;
  check_after?: Check;
  instructions_changed?: true;
  output?: object;
};

// --- the constants the test holds to the schemas ---

type RequiredKeys<T> = { [K in keyof T]-?: {} extends Pick<T, K> ? never : K }[keyof T];

// `values` lists every member of T and nothing else, or it doesn't compile.
const exactly =
  <T>() =>
  <const V extends readonly T[]>(values: V & (Exclude<T, V[number]> extends never ? unknown : never)) =>
    values;

export const TASKS = exactly<Task>()(["explore", "implement", "review", "auto-tickets", "smoke"]);
export const FAILURES = exactly<Failure>()([
  "setup_failed", "agent_error", "agent_timeout", "no_output", "invalid_output", "check_failed",
  "contract_mismatch", "claude_auth", "usage_limit",
]);
export const OUTCOMES = exactly<Result["outcome"]>()(["succeeded", "failed"]);
export const PREVIOUS_OUTCOMES = exactly<PreviousAttempt["outcome"]>()(["failed", "invalid"]);

export const KEYS = {
  dispatch: exactly<keyof Dispatch>()([
    "contract_version", "run_id", "repo", "task", "base_sha", "enrolment_sha", "timeout_minutes",
    "ticket", "ticket_markdown", "previous_attempt", "model", "branch", "triggered_by",
    "older_issues", "pr_number", "pr_base_sha", "findings_only",
  ]),
  result: exactly<keyof Result>()([
    "contract_version", "run_id", "task", "outcome", "failure", "check", "check_before",
    "check_after", "instructions_changed", "output",
  ]),
  check: exactly<keyof Check>()(["passed", "output_tail"]),
  previous_attempt: exactly<keyof PreviousAttempt>()(["outcome", "failure", "check_output", "validation_error"]),
  triggered_by: exactly<keyof TriggeredBy>()(["login", "id"]),
  older_issue: exactly<keyof OlderIssue>()(["ticket", "title"]),
};

export const REQUIRED = {
  // The fields every task's dispatch requires.
  dispatch: exactly<RequiredKeys<Dispatch>>()([
    "contract_version", "run_id", "repo", "task", "base_sha", "enrolment_sha", "timeout_minutes",
  ]),
  result: exactly<RequiredKeys<Result>>()(["contract_version", "run_id", "task", "outcome"]),
  check: exactly<RequiredKeys<Check>>()(["passed", "output_tail"]),
  previous_attempt: exactly<RequiredKeys<PreviousAttempt>>()(["outcome"]),
  triggered_by: exactly<RequiredKeys<TriggeredBy>>()(["login", "id"]),
  older_issue: exactly<RequiredKeys<OlderIssue>>()(["ticket", "title"]),
};
