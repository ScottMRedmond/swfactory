"""The orchestrator's side of the runner contract.

The JSON Schemas in contracts/ at the repo root are the only source of truth.
These models mirror them by hand and are tested against every example in
contracts/examples/ (see tests/test_contract.py), so a schema change that
isn't made here too fails the tests.
"""

from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

CONTRACT_VERSION = 1

Task = Literal["explore", "implement", "review", "auto-tickets", "smoke"]
TASKS: tuple[Task, ...] = Task.__args__


class ContractError(ValueError):
    """A dispatch, result or enrolment doesn't satisfy its schema."""


class _Strict(BaseModel):
    # additionalProperties: false, and no coercion ("1" is not 1), like the schemas.
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
Ticket = Annotated[str, Field(min_length=1, max_length=100)]
TicketMarkdown = Annotated[str, Field(min_length=1, max_length=65535)]
Repo = Annotated[str, Field(pattern=r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")]
Model = Annotated[str, Field(min_length=1, max_length=100)]
Branch = Annotated[str, Field(min_length=1, max_length=255)]
Failure = Literal[
    "setup_failed",
    "agent_error",
    "agent_timeout",
    "no_output",
    "invalid_output",
    "check_failed",
    "contract_mismatch",
    "claude_auth",
    "usage_limit",
]
FAILURES: tuple[Failure, ...] = Failure.__args__


class OlderIssue(_Strict):
    ticket: Ticket
    title: Annotated[str, Field(max_length=1024)]


class PreviousAttempt(_Strict):
    outcome: Literal["failed", "invalid"]
    failure: Failure | None = None
    check_output: Annotated[str, Field(max_length=20000)] | None = None
    validation_error: Annotated[str, Field(max_length=20000)] | None = None

    @model_validator(mode="after")
    def _reason_matches_outcome(self):
        if self.outcome == "failed" and (self.failure is None or self.validation_error is not None):
            raise ValueError("a failed previous attempt has a failure and no validation_error")
        if self.outcome == "invalid" and (self.validation_error is None or self.failure is not None):
            raise ValueError("an invalid previous attempt has a validation_error and no failure")
        return self


class _DispatchBase(_Strict):
    contract_version: Literal[CONTRACT_VERSION]
    run_id: Annotated[int, Field(ge=1)]
    repo: Repo
    base_sha: Sha
    enrolment_sha: Sha
    timeout_minutes: Annotated[int, Field(ge=1)]


class _AgentDispatch(_DispatchBase):
    ticket: Ticket
    ticket_markdown: TicketMarkdown
    model: Model
    previous_attempt: PreviousAttempt | None = None


OlderIssues = Annotated[list[OlderIssue], Field(max_length=200)]


class ExploreDispatch(_AgentDispatch):
    task: Literal["explore"]
    older_issues: OlderIssues


class ImplementDispatch(_AgentDispatch):
    task: Literal["implement"]
    branch: Branch


class ReviewDispatch(_AgentDispatch):
    task: Literal["review"]
    branch: Branch
    pr_number: Annotated[int, Field(ge=1)]
    pr_base_sha: Sha
    findings_only: bool


class AutoTicketsDispatch(_AgentDispatch):
    task: Literal["auto-tickets"]
    older_issues: OlderIssues


class SmokeDispatch(_DispatchBase):
    task: Literal["smoke"]


Dispatch = Annotated[
    ExploreDispatch | ImplementDispatch | ReviewDispatch | AutoTicketsDispatch | SmokeDispatch,
    Field(discriminator="task"),
]

_dispatch = TypeAdapter(Dispatch)


def _validate(adapter: TypeAdapter, data: Any):
    try:
        return adapter.validate_python(data)
    except ValidationError as e:
        raise ContractError(str(e)) from e


def parse_dispatch(data: Any) -> Dispatch:
    """Validate a dispatch (already decoded from JSON)."""
    return _validate(_dispatch, data)


# --- result.json -----------------------------------------------------------

Prose = Annotated[str, Field(min_length=1, max_length=20000)]
Line = Annotated[str, Field(min_length=1, max_length=2000)]
RepoPath = Annotated[str, Field(min_length=1, max_length=500)]
Reasoning = Annotated[str, Field(min_length=1, max_length=5000)]


class Check(_Strict):
    passed: bool
    output_tail: Annotated[str, Field(max_length=20000)]


class ExploreOutput(_Strict):
    verdict: Literal["easy-call", "needs-a-human", "waiting", "duplicate", "too-big"]
    summary: Prose
    findings: Prose
    plan: Annotated[list[Line], Field(max_length=50)]
    files: Annotated[list[RepoPath], Field(max_length=200)]
    questions: Annotated[list[Line], Field(max_length=20)] | None = None
    duplicate_of: Ticket | None = None

    @model_validator(mode="after")
    def _verdict_rules(self):
        if self.verdict in ("needs-a-human", "waiting") and not self.questions:
            raise ValueError(f"{self.verdict} needs questions")
        if (self.verdict == "duplicate") != (self.duplicate_of is not None):
            raise ValueError("duplicate_of goes with the duplicate verdict, and only with it")
        return self


class ImplementOutput(_Strict):
    verdict: Literal["built", "needs-a-human"]
    comment: Prose | None = None
    pr_body: Prose | None = None

    @model_validator(mode="after")
    def _verdict_rules(self):
        if self.verdict == "needs-a-human" and self.comment is None:
            raise ValueError("needs-a-human needs a comment")
        return self


class StandardsAxis(_Strict):
    verdict: Literal["pass", "changes-requested"]
    reasoning: Reasoning


class SpecAxis(_Strict):
    verdict: Literal["pass", "changes-requested", "no-ticket"]
    reasoning: Reasoning


class ReviewComment(_Strict):
    axis: Literal["standards", "spec"]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    path: RepoPath
    line: Annotated[int, Field(ge=1)]
    body: Reasoning


class ReviewOutput(_Strict):
    standards: StandardsAxis
    spec: SpecAxis
    comments: Annotated[list[ReviewComment], Field(max_length=25)]
    fixed: Annotated[list[Line], Field(max_length=50)]
    missing_rules: Annotated[list[Line], Field(max_length=20)] | None = None


class NewTicket(_Strict):
    title: Annotated[str, Field(min_length=1, max_length=256)]
    what_to_build: Prose
    acceptance_criteria: Annotated[list[Line], Field(min_length=1, max_length=30)]
    blocked_by: Annotated[list[Annotated[int, Field(ge=0, le=10)]], Field(max_length=11)]
    related: Annotated[list[Ticket], Field(max_length=10)] | None = None

    @model_validator(mode="after")
    def _unique_blockers(self):
        if len(set(self.blocked_by)) != len(self.blocked_by):
            raise ValueError("blocked_by repeats an index")
        return self


class AutoTicketsOutput(_Strict):
    verdict: Literal["decomposed", "needs-a-human"]
    comment: Prose | None = None
    tickets: Annotated[list[NewTicket], Field(min_length=1, max_length=12)] | None = None

    @model_validator(mode="after")
    def _verdict_rules(self):
        if self.verdict == "decomposed" and self.tickets is None:
            raise ValueError("decomposed needs tickets")
        if self.verdict == "needs-a-human" and (self.comment is None or self.tickets is not None):
            raise ValueError("needs-a-human needs a comment and no tickets")
        return self


class SmokeOutput(_Strict):
    pass


class _ResultBase(_Strict):
    contract_version: Literal[CONTRACT_VERSION]
    run_id: Annotated[int, Field(ge=1)]
    outcome: Literal["succeeded", "failed"]
    failure: Failure | None = None

    @model_validator(mode="after")
    def _outcome_rules(self):
        if self.outcome == "succeeded" and (self.failure is not None or self.output is None):
            raise ValueError("a succeeded result has an output and no failure")
        if self.outcome == "failed" and self.failure is None:
            raise ValueError("a failed result needs a failure reason")
        return self


class ExploreResult(_ResultBase):
    task: Literal["explore"]
    output: ExploreOutput | None = None


class ImplementResult(_ResultBase):
    task: Literal["implement"]
    check: Check | None = None
    output: ImplementOutput | None = None


class ReviewResult(_ResultBase):
    task: Literal["review"]
    check_before: Check | None = None
    check_after: Check | None = None
    output: ReviewOutput | None = None


class AutoTicketsResult(_ResultBase):
    task: Literal["auto-tickets"]
    output: AutoTicketsOutput | None = None


class SmokeResult(_ResultBase):
    task: Literal["smoke"]
    check: Check | None = None
    output: SmokeOutput | None = None


Result = Annotated[
    ExploreResult | ImplementResult | ReviewResult | AutoTicketsResult | SmokeResult,
    Field(discriminator="task"),
]

_result = TypeAdapter(Result)


def parse_result(data: Any, task: Task) -> Result:
    """Validate a result.json (already decoded from JSON) for a run of `task`.

    A result for any other task than the one dispatched is a contract error.
    """
    result = _validate(_result, data)
    if result.task != task:
        raise ContractError(f"expected a result for {task}, got one for {result.task}")
    return result


# --- .factory/config.yml ---------------------------------------------------

# smoke has no agent and is always enabled, so an enrolment can't list,
# time or pick a model for it.
AgentTask = Literal["explore", "implement", "review", "auto-tickets"]
Minutes = Annotated[int, Field(ge=1)]


class Enrolment(_Strict):
    setup: Annotated[str, Field(min_length=1)]
    check: Annotated[str, Field(min_length=1)]
    test_paths: list[Annotated[str, Field(min_length=1)]] | None = None
    tasks: list[AgentTask] | None = None
    enabled: bool = True
    timeouts: dict[AgentTask, Minutes] = {}
    models: dict[AgentTask, Model] = {}

    @model_validator(mode="after")
    def _unique_tasks(self):
        if self.tasks is not None and len(set(self.tasks)) != len(self.tasks):
            raise ValueError("tasks repeats a task")
        return self

    def enabled_tasks(self) -> set[Task]:
        listed = set(AgentTask.__args__) if self.tasks is None else set(self.tasks)
        return listed | {"smoke"}


_enrolment = TypeAdapter(Enrolment)


def parse_enrolment(text: str) -> Enrolment:
    """Validate the text of a repo's .factory/config.yml."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ContractError(f"config.yml isn't YAML: {e}") from e
    return _validate(_enrolment, data)
