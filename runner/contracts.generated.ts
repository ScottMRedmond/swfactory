// Generated from contracts/dispatch.schema.json and contracts/result.schema.json
// by runner/gen-contracts.ts. Do not edit: change the schemas and run
// `npm run gen:contracts`.

/**
 * What the orchestrator sends a runner for one run. One JSON object, passed as a single workflow_dispatch input. It holds no secrets: anyone who can read the target repo's runs can read it.
 */
export type Dispatch = Explore | Implement | Review | AutoTickets | Smoke;
/**
 * The most recent open issues older than the ticket, newest first, for spotting duplicates and overlaps.
 *
 * @maxItems 200
 */
export type OlderIssues = {
  /**
   * An opaque ticket id (an issue number today), so another tracker's ids fit.
   */
  ticket: string;
  title: string;
}[];
/**
 * On a retry: how the previous attempt ended, so the task can render a Previous attempt section in ticket.md.
 */
export type PreviousAttempt = {
  outcome: "failed" | "invalid";
  /**
   * Why a failed run failed. contract_mismatch, claude_auth and usage_limit aren't attempts.
   */
  failure?:
    | "setup_failed"
    | "agent_error"
    | "agent_timeout"
    | "no_output"
    | "invalid_output"
    | "check_failed"
    | "contract_mismatch"
    | "claude_auth"
    | "usage_limit";
  /**
   * The last 50 lines of the previous attempt's check output. May be trimmed to fit the dispatch.
   */
  check_output?: string;
  /**
   * Why the previous attempt's result was invalid.
   */
  validation_error?: string;
};
/**
 * The result.json a run uploads. The agent writes only output (as output.json); the run's own steps write everything else, including the check results, out of the agent's reach. output is a tagged union on task.
 */
export type Result = {
  /**
   * Shared by dispatch.schema.json and result.schema.json. Bump it whenever either schema changes.
   */
  contract_version: 1;
  /**
   * The orchestrator's runs.id for this dispatch.
   */
  run_id: number;
  task: "explore" | "implement" | "review" | "auto-tickets" | "smoke";
  outcome: "succeeded" | "failed";
  /**
   * Why a failed run failed. contract_mismatch, claude_auth and usage_limit aren't attempts.
   */
  failure?:
    | "setup_failed"
    | "agent_error"
    | "agent_timeout"
    | "no_output"
    | "invalid_output"
    | "check_failed"
    | "contract_mismatch"
    | "claude_auth"
    | "usage_limit";
  check?: Check;
  check_before?: Check1;
  check_after?: Check2;
  /**
   * Review: the PR changes CLAUDE.md, AGENTS.md or .claude/, so it was reviewed against the base's instead. Absent when it doesn't.
   */
  instructions_changed?: true;
  /**
   * The task's output; its sub-schema depends on task.
   */
  output?: {
    [k: string]: unknown;
  };
};

export interface Contracts {
  dispatch: Dispatch;
  result: Result;
}
export interface Explore {
  /**
   * Shared by dispatch.schema.json and result.schema.json. Bump it whenever either schema changes.
   */
  contract_version: 1;
  /**
   * The orchestrator's runs.id for this dispatch.
   */
  run_id: number;
  /**
   * The target repo, as owner/name.
   */
  repo: string;
  task: "explore";
  /**
   * An opaque ticket id (an issue number today), so another tracker's ids fit.
   */
  ticket: string;
  /**
   * The ticket rendered to markdown by the orchestrator; the task reads it as ticket.md.
   */
  ticket_markdown: string;
  older_issues: OlderIssues;
  base_sha: string;
  enrolment_sha: string;
  /**
   * The Claude model the runner passes to Claude Code.
   */
  model: string;
  /**
   * The whole job's timeout. run.yml caps it at 60.
   */
  timeout_minutes: number;
  previous_attempt?: PreviousAttempt;
}
export interface Implement {
  /**
   * Shared by dispatch.schema.json and result.schema.json. Bump it whenever either schema changes.
   */
  contract_version: 1;
  /**
   * The orchestrator's runs.id for this dispatch.
   */
  run_id: number;
  /**
   * The target repo, as owner/name.
   */
  repo: string;
  task: "implement";
  /**
   * An opaque ticket id (an issue number today), so another tracker's ids fit.
   */
  ticket: string;
  /**
   * The ticket rendered to markdown by the orchestrator; the task reads it as ticket.md.
   */
  ticket_markdown: string;
  base_sha: string;
  /**
   * The branch name the orchestrator chose.
   */
  branch: string;
  triggered_by: TriggeredBy;
  enrolment_sha: string;
  /**
   * The Claude model the runner passes to Claude Code.
   */
  model: string;
  /**
   * The whole job's timeout. run.yml caps it at 60.
   */
  timeout_minutes: number;
  previous_attempt?: PreviousAttempt;
}
/**
 * The GitHub user who triggered the workflow. Commits get a Co-Authored-By trailer naming them at <id>+<login>@users.noreply.github.com.
 */
export interface TriggeredBy {
  login: string;
  id: number;
}
export interface Review {
  /**
   * Shared by dispatch.schema.json and result.schema.json. Bump it whenever either schema changes.
   */
  contract_version: 1;
  /**
   * The orchestrator's runs.id for this dispatch.
   */
  run_id: number;
  /**
   * The target repo, as owner/name.
   */
  repo: string;
  task: "review";
  /**
   * An opaque ticket id (an issue number today), so another tracker's ids fit.
   */
  ticket: string;
  /**
   * The ticket rendered to markdown by the orchestrator; the task reads it as ticket.md.
   */
  ticket_markdown: string;
  pr_number: number;
  /**
   * The PR's base commit, so the runner can fetch the diff and restore agent instructions from the base.
   */
  pr_base_sha: string;
  /**
   * Push nothing: changes review would have made become findings (PRs the factory didn't open).
   */
  findings_only: boolean;
  /**
   * The PR head the review starts from.
   */
  base_sha: string;
  /**
   * The branch name the orchestrator chose.
   */
  branch: string;
  triggered_by: TriggeredBy;
  enrolment_sha: string;
  /**
   * The Claude model the runner passes to Claude Code.
   */
  model: string;
  /**
   * The whole job's timeout. run.yml caps it at 60.
   */
  timeout_minutes: number;
  previous_attempt?: PreviousAttempt;
}
export interface AutoTickets {
  /**
   * Shared by dispatch.schema.json and result.schema.json. Bump it whenever either schema changes.
   */
  contract_version: 1;
  /**
   * The orchestrator's runs.id for this dispatch.
   */
  run_id: number;
  /**
   * The target repo, as owner/name.
   */
  repo: string;
  task: "auto-tickets";
  /**
   * An opaque ticket id (an issue number today), so another tracker's ids fit.
   */
  ticket: string;
  /**
   * The ticket rendered to markdown by the orchestrator; the task reads it as ticket.md.
   */
  ticket_markdown: string;
  older_issues: OlderIssues;
  base_sha: string;
  enrolment_sha: string;
  /**
   * The Claude model the runner passes to Claude Code.
   */
  model: string;
  /**
   * The whole job's timeout. run.yml caps it at 60.
   */
  timeout_minutes: number;
  previous_attempt?: PreviousAttempt;
}
/**
 * No agent: build the image, run setup, run the check on base_sha (the default branch's head).
 */
export interface Smoke {
  /**
   * Shared by dispatch.schema.json and result.schema.json. Bump it whenever either schema changes.
   */
  contract_version: 1;
  /**
   * The orchestrator's runs.id for this dispatch.
   */
  run_id: number;
  /**
   * The target repo, as owner/name.
   */
  repo: string;
  task: "smoke";
  base_sha: string;
  enrolment_sha: string;
  /**
   * The whole job's timeout. run.yml caps it at 60.
   */
  timeout_minutes: number;
}
/**
 * Implement and smoke: the check after the task.
 */
export interface Check {
  passed: boolean;
  /**
   * The last 50 lines of the check command's output.
   */
  output_tail: string;
}
/**
 * Review: the check before the agent ran.
 */
export interface Check1 {
  passed: boolean;
  /**
   * The last 50 lines of the check command's output.
   */
  output_tail: string;
}
/**
 * Review: the check on review's fixes.
 */
export interface Check2 {
  passed: boolean;
  /**
   * The last 50 lines of the check command's output.
   */
  output_tail: string;
}
