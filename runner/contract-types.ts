// The runner contract's types, under the names the runner uses. ADR 0001:
// they come from contracts.generated.ts, which `npm run gen:contracts`
// generates from contracts/*.schema.json (contract-types.test.ts fails while
// it's stale); nothing here is written independently of the schemas.

import type * as Generated from "./contracts.generated";

export type { TriggeredBy, PreviousAttempt, Check } from "./contracts.generated";

// The schemas pin contract_version to the current version, but the runner
// also accepts the one before it (contract.ts), so here it's any number.
type AnyVersion<T> = Omit<T, "contract_version"> & { contract_version: number };

// result.schema.json. `output` is the task's own sub-schema (contract.ts
// validates it against the task's).
export type Result = AnyVersion<Generated.Result>;
export type Output = NonNullable<Result["output"]>;

// result.schema.json: task and $defs/failure.
export type Task = Generated.Result["task"];
export type Failure = NonNullable<Generated.Result["failure"]>;

// The failures the agent step reports (agent.ts).
export type AgentFailure = Extract<
  Failure,
  "agent_error" | "agent_timeout" | "no_output" | "invalid_output" | "claude_auth" | "usage_limit"
>;

// dispatch.schema.json $defs/older_issues items.
export type OlderIssue = Generated.OlderIssues[number];

// Every key of any member of union U.
type AllKeys<U> = U extends unknown ? keyof U : never;

// A union of object types as one object type: a key every member has keeps
// its type; a key only some have is optional, typed as those members have it.
type Flatten<U, Common extends keyof U = keyof U> = { [K in Common]: U[K] } & { [K in Exclude<AllKeys<U>, keyof U>]?: ValueOf<U, K> };
type ValueOf<U, K extends PropertyKey> = U extends unknown ? (K extends keyof U ? Exclude<U[K], undefined> : never) : never;

// A dispatch of any task: dispatch.schema.json's per-task union, flattened,
// so a field one task lacks is optional. The schema says which task has
// which; the run script reads them by task.
export type Dispatch = AnyVersion<Flatten<Generated.Dispatch>>;
