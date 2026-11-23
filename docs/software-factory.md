# Software factory: services

Status: agreed design, not built. This document covers the services, the runners, the runner contract, the tasks' outputs and enrolling a repo. The orchestrator, including the workflows it chains, its limits and its observability, is specified in [orchestrator.md](orchestrator.md). The tasks' prompts come later.

Two terms are used throughout:

- A **task** is one run on a runner, with one output: explore, implement, review or auto-tickets.
- A **workflow** is a sequence of tasks the orchestrator chains together: implement-ticket or implement-spec (see [Workflows](orchestrator.md#workflows)).

This doc lives in this repo until the factory repo exists, then moves there. The **factory repo** is a public [home repo](https://www.aihero.dev/the-home-repo-pattern): it holds the factory's code and the shared skills. Each enrolled repo keeps its own enrolment (see [Enrolling a repo](#enrolling-a-repo)).

## Goal

Move the software factory off GitHub Actions as its **orchestrator**. GitHub stays the **ticket tracker** and **git host**, and stays the source of truth for work. Today's factory chains GitHub Actions workflows together with labels set by a PAT. The new factory replaces that with an orchestrator of our own. It is built for more than one repo, and its first enrolled repo is a Python project, which stays private.

The point is portability. GitHub keeps the repos and the issues, but it stops deciding what runs next.

Out of scope for v1:

- new tasks (v1 starts from today's Sandcastle tasks; adapting them to the Python project is task design)
- migrating the cohort repo (it keeps its Actions factory until the course ends, then is retired)
- webhooks
- running agents on the Mac mini
- a web runs page

## Services

| Slot | Product |
|---|---|
| Ticket tracker | GitHub Issues: each enrolled repo's own issues |
| Git host | GitHub: each enrolled repo's own GitHub repo |
| Orchestrator | Our own Python process on the family Mac mini (see [orchestrator.md](orchestrator.md)) |
| Runner | GitHub Actions, behind a Runner interface |
| Observability platform | None. Status comments, notices, factory problem issues, Actions logs and a CLI are enough (see [Observability](orchestrator.md#observability)) |

## Orchestrator

The orchestrator is our own Python process on the family Mac mini: **one process and one SQLite file**. It polls each enrolled repo for trigger labels, queues runs, dispatches them to runners, collects and validates their results, and chains tasks into workflows. It acts as a GitHub App and is **the only writer**: only the orchestrator pushes branches, opens PRs, posts comments and sets labels. Runners never write to GitHub.

Its full specification is in [orchestrator.md](orchestrator.md).

## Runners

**Product.** GitHub Actions. The orchestrator talks to runners through a Runner interface, so it doesn't change if Actions is later replaced by a VM, Fly Machines or a self-hosted Mac mini. Agents do not run on the family Mac mini in v1: an unsandboxed agent next to family data is too risky.

**Runs happen in the target repo.** The factory repo holds one **reusable workflow**, `run.yml`. Each enrolled repo holds a thin caller workflow of about 10 lines (`workflow_dispatch` → `uses: <owner>/<factory>/.github/workflows/run.yml@main`, with `permissions: contents: read`), and the orchestrator dispatches that caller. So each job runs in the target repo:

- Its logs take the **target's visibility**, so the factory repo, which holds only code and no runs, can be public whatever its targets are.
- It checks out the target with the job's built-in `GITHUB_TOKEN`, limited to `contents: read`. **No token travels in the dispatch.**
- Its minutes are billed to the target repo's owner: free on a public repo, and from the account's allowance on a private one (see Visibility and cost).

Each run goes through these steps:

1. Validate the dispatch, and stop at once if its `contract_version` is unknown (see [The runner contract](#the-runner-contract)).
2. Check out the target repo at `base_sha`, and the repo's `.factory/` enrolment from the default-branch commit `enrolment_sha` (see [Enrolling a repo](#enrolling-a-repo)). Both come from the target repo, so the job's `GITHUB_TOKEN` is enough. The ticket isn't fetched: it arrives already rendered in the dispatch (see below). For review, the checkout fetches both the PR head and its base, so the diff is on disk: the agent can't fetch anything itself (see Secrets on the runner).
3. Build the enrolment's Dockerfile, with Docker's GitHub Actions layer cache (`cache-from: type=gha`, kept in the target repo's Actions cache), so an unchanged image is mostly cache hits. Prebuilding each enrolment's image to GHCR is a later optimisation, if `factory stats` shows build time matters.
4. Run the setup command (for explore, implement and review; auto-tickets only reads and writes text, so it skips setup), then the task. The agent writes only its task's output, to `output.json`.
   - The agent step has its own timeout: the task's timeout minus 5 minutes. When it fires, the steps below still run (they're `if: always()`), so the run records `failure: agent_timeout` and uploads any partial bundle instead of vanishing. The job's `timeout-minutes` stays as the backstop.
   - If `output.json` fails its task's sub-schema, the runner gives the validation errors back to the agent **once**, in the same session, and asks for a corrected file. If that fails too, the run fails with `failure: invalid_output`.
5. Steps 5–7 run `if: always()`, whatever happened in step 4. For implement, run the check command after the task (see the automated checks under [Limits and brakes](orchestrator.md#limits-and-brakes)). For review, run it **before and after**: before, so a PR that already fails reaches the agent as something to fix; after, on review's fixes. `result.json` records both. Explore and auto-tickets make no commits, so they run no check.
6. Assemble `result.json` around `output.json`: run id, outcome, failure reason, and the check result this step recorded itself, out of the agent's reach.
7. Upload a **git bundle plus `result.json`** as an artifact, together with any screenshots the agent took (`evidence/*.png`). The orchestrator doesn't post the screenshots yet (see [Open questions](#open-questions)), but they can be downloaded from the Actions run that the PR links to. `run.yml` uploads with `retention-days: 7`. The upload is the one step that isn't in the run script: it needs `actions/upload-artifact`, so it's a separate `run.yml` step after the script's, with `if: always()` so it runs whatever the script did.

**Tickets reach the runner as text.** The orchestrator fetches the ticket, renders it to markdown (title, body, and the comments the task should see) and passes that markdown in the dispatch. The task reads it as `ticket.md`, as today's tasks already do. The runner never calls the Issues API, doesn't fall back to fetching the issue itself, and doesn't know which tracker is in use. This is what lets the tracker change later without touching the runner contract. How the orchestrator filters comments and trims a ticket to fit the dispatch is in [Rendering tickets](orchestrator.md#rendering-tickets).

The orchestrator then downloads the artifact, validates it, and does all the writing to GitHub (see [Dispatching and collecting](orchestrator.md#dispatching-and-collecting)).

**Secrets on the runner.** A runner holds only the Claude token, which is a secret in each enrolled repo (a personal account has no account-wide secrets), and the read-only `GITHUB_TOKEN`. Nothing on the runner can write to GitHub.

- The Claude token reaches the **agent only**, not setup or the check. Actions can't scope a secret to part of a step, so it's in the env of `run.yml`'s one "Run the task" step; the run script deletes it from its own environment before it runs anything, and passes it only to the agent's `docker exec` (by name, in that one command's environment). git, the image build, setup and the check never see it. What remains: `npx` and `tsx`, which start the script, inherit the step's env, and so does the script itself until its first line runs.
- Checkout uses `persist-credentials: false`, and the agent step doesn't get `GITHUB_TOKEN`, so the agent holds no GitHub credential, not even a read-only one. It doesn't need one: the steps before it put the code, its history, review's base and `ticket.md` on disk, and it works with local git only. The bundle and `result.json` are uploaded by later steps through the Actions artifact channel.
- Commands the agent runs itself (tests, scripts) inherit the Claude token; that can't be avoided while it holds it. Network egress is an [open question](#open-questions).

The dispatch is visible to anyone who can read the target repo's runs, so it carries no secrets. This closes the gap in today's setup, where the PAT is kept away from the agent's step but is, in the workflow's own words, "not a wall".

**Base image.** The factory publishes a base image containing Node, the Claude Code CLI, Sandcastle, agent-browser and git. Every enrolled repo builds its image `FROM` it and adds its own toolchain (Python, SQLite, …). The base image is needed because the agent harness runs on Node even when the target repo doesn't. It's a public package on GHCR, like the factory repo, so any run can pull it without a token. It's `ghcr.io/<owner>/factory-base` (`:latest`, or `:<commit sha>` to pin), built from `base-image/Dockerfile` and published by the factory repo's `base-image.yml` workflow. It runs as the `agent` user, so an enrolment's Dockerfile switches to `USER root` to install its toolchain and back to `USER agent` after.

**Visibility and cost.** Actions logs (agent transcripts, diffs, test output) take the visibility of the repo the job runs in, which is the target repo. **v1 starts with a private repo**, so its transcripts stay private.

- **Minutes:** a private repo's runs use the account's included Actions minutes (2,000 a month on GitHub Free, 3,000 on Pro), then about $0.006 a minute on Linux. A public repo's runs are free, and its transcripts are public; both kinds can be enrolled.
- **Storage:** a private repo's artifacts and caches count against the account's Actions storage allowance. Bundles kept for 7 days should be small, but the cache can grow.
- **Spending limit:** GitHub's Actions spending limit is set to **$20 a month**. Once it's reached, runs are refused rather than billed. The Actions runner must report a refused run as [runner unavailable](orchestrator.md#dispatching-and-collecting), not as a missing run, so tickets aren't retried into `agent:blocked` while the limit holds. A refused run still exists on Actions, and its job ends without running a step, with "The job was not started because recent account payments have failed or your spending limit needs to be increased." (from user reports; see [Dispatching and collecting](orchestrator.md#dispatching-and-collecting)).
- **The first month is a cost experiment.** `factory stats` shows minutes per task, and GitHub's billing page shows the spend. If Actions costs more than the limit allows, the alternative is a runner on our own hardware (see [Open questions](#open-questions)).

The factory repo stays public without revealing private repos: enrolments live in each repo's `.factory/`, and [factory problems](orchestrator.md#observability) about one repo are opened on that repo, not on the factory repo.

**A possible future runner: Sandcastle cloud sandboxes.** Sandcastle isn't an orchestrator: it runs one agent per `sandcastle.run()` call, and it has no queue, polling, persistence or GitHub identity. But it can run that agent in a cloud sandbox (Daytona, E2B, Fly Sprites, Modal or Vercel), which overlaps with the Runner slot. The orchestrator could call `sandcastle.run()` directly instead of dispatching `run.yml`.

- **It removes:** `run.yml` and the caller workflows, `workflow_dispatch` and its size cap, uploading and downloading artifacts, and most of the runner contract. The ticket becomes a function argument, and Sandcastle validates structured output itself.
- **It costs:**
  - Sandcastle is a TypeScript library, so a small TypeScript script has to sit between it and the Python orchestrator. The orchestrator runs the script as a subprocess, and they exchange the dispatch and the result as JSON files, as they do with `run.yml` today. Writing the orchestrator in TypeScript would remove the script, but isn't required.
  - You pay a sandbox provider instead of using the Actions allowance.
  - There are no Actions logs, so the factory has to store and link transcripts itself.
  - The Claude token lives on the Mac mini and is passed into the sandbox for each run. The agent still doesn't run on the Mac mini.

v1 keeps Actions, because it needs no hardware, the logs come free, and the spending limit caps what it can cost.

**Keeping the runner swappable.** A later runner on our own hardware, such as a dedicated Linux PC, should need a new Runner implementation and nothing else. Three rules keep it that way:

- **The run steps live in a script, not in `run.yml`.** `run.yml` only prepares the job and calls one script with the dispatch (e.g. `run-task.sh dispatch.json`). That script runs the steps above, including the `if: always()` behaviour, so another runner can call the same script.
- **Nothing outside the Runner knows it's Actions.** The orchestrator asks the Runner for a run's log link and its files (`result.json`, the bundle, screenshots). It never builds an Actions URL, calls the artifacts API or relies on artifact retention itself.
- **A runner that can't be reached is one general failure.** Whatever the cause (an Actions outage, a refused dispatch, a PC that's switched off), the Runner reports it the same way, and it isn't an attempt.

## The runner contract

The orchestrator and the tasks are written in different languages and share no code. They agree on two documents, each defined as a **JSON Schema** in `contracts/` in the factory repo:

- `dispatch.schema.json` covers the dispatch: `contract_version`, run id, repo, task, ticket id, the rendered ticket markdown, the list of older open issues (for explore, see [Explore](#explore)), the PR number and the findings-only flag (for review), `base_sha` (the exact commit to start from), the branch name the orchestrator chose, `triggered_by` (for implement and review: the login and user id of the human who triggered the work, for the commits' `Co-Authored-By` trailer, see [Implement](#implement)), `enrolment_sha` (the target's default-branch commit whose `.factory/` the orchestrator read, so the runner and the orchestrator judge the run against the same config), the task's `model` and timeout, and an optional `previous_attempt` (see Attempts under [Limits and brakes](orchestrator.md#limits-and-brakes)). It travels as **one JSON input**, so adding a field changes the schema only, not the caller workflows. It holds no secrets.
- `result.schema.json` covers the `result.json` a run uploads: `contract_version`, run id, outcome (`succeeded` or `failed`), a `failure` reason when it failed (`setup_failed`, `agent_error`, `agent_timeout`, `no_output`, `invalid_output`, `check_failed`, `contract_mismatch`, `claude_auth` or `usage_limit`), whether the check passed and the last 50 lines of its output (for review, before and after the agent), and a task-specific `output` object. `output` is a **tagged union on `task`**, one sub-schema per task (see [Tasks](#tasks)), so a result for the wrong task fails validation.

**Outcomes and verdicts are different things.** "The agent says stop" is a verdict in `output` (e.g. `needs-a-human`). "The machinery broke" is a `failed` outcome. The agent writes only `output.json`; the run's own steps write everything else in `result.json`, including the check result, so the agent can't claim a check passed. A `failed` result's `output` is ignored, with one exception: a review whose check failed (see [Review](orchestrator.md#review)).

**Pinned commits.** The runner checks out exactly `base_sha`, and the orchestrator checks that the bundle builds on it. Implement-spec's fast-forward merges and review's "branch moved" rule both rely on knowing which commit a run started from.

**Bundle checks.** Before pushing anything, the orchestrator checks the bundle's shape. Any violation makes the result `invalid`:

- exactly one ref, whose prerequisite is `base_sha`
- linear history, with no merge commits
- at least one commit when implement says `built`; review's bundle may be empty
- no bundle at all from explore or auto-tickets
- at most 50 MB. GitHub refuses files over 100 MB anyway, and a giant bundle means the agent committed something it shouldn't have.

**Who writes what.** The agent supplies prose only, and only through `output`. The orchestrator owns everything else it posts: every title, footer, link, closing keyword, status comment and notice. It needs no model to do this. It also chooses every branch name and passes it in the dispatch; `result.json` doesn't name a branch. (For how this works for a PR, see [Implement-ticket](orchestrator.md#implement-ticket).)

Agent prose is posted as-is under the App's identity. The schemas cap its length (`maxLength`), but the orchestrator doesn't filter it, so a stray `@mention` or `Closes #M` gets through. That risk is accepted, because a human reviews and merges every PR.

**Secrets in prose.** An issue's body comes from its author, who may be an outsider on a public repo, and a prompt injection in it could get the agent to copy the Claude token into `output.json`. Before posting any agent prose, the orchestrator scans it for the token's prefix (`sk-ant-`) and other token-like patterns. A match makes the result `invalid`, and nothing is posted.

**Protected paths.** A bundle whose commits touch `.github/workflows/` or `.factory/` is an invalid result, whatever the task: a failed attempt, and nothing is pushed. `.github/workflows/` is where the caller workflow lives, and the App has no `workflows` permission. `.factory/` holds the check command and Dockerfile, and the orchestrator reads it only from the default branch (see [Enrolling a repo](#enrolling-a-repo)), so neither an agent's commit nor a PR can change its own brake.

**Review's reach.** A review bundle that touches a file outside the PR's own files and the repo's `test_paths` is also invalid. Anything else review wants to change becomes a finding, so a review can't wander into a refactor.

Both sides validate against the schemas, and the schemas are the only source of truth:

- **Runner side:** a run checks its own `result.json` before uploading it. If the result is invalid, the run fails rather than handing the orchestrator something it can't read.
- **Orchestrator side:** the orchestrator validates every `result.json` it downloads, plus the checks a schema can't express (e.g. explore's `duplicate_of` must be one of the issues it sent). An invalid result counts as a failed attempt, and nothing is written to GitHub.
- **Strict:** every object in both schemas has `additionalProperties: false`, so a typo like `verdcit` fails validation instead of being silently dropped.
- **One version:** `contract_version` is a single integer shared by both schemas. Changing either schema means bumping it and updating both sides in the same commit. Before the bump, copy the old `dispatch`, `result` and `enrolment` schemas into `contracts/previous/`: that's how the run script accepts the version before the current one (below).
- **Fixtures:** `contracts/examples/` holds a valid and an invalid sample of every dispatch and every task's result. The orchestrator's tests and the task side's tests both validate them, so both sides run the same concrete test.
- `run.yml` and the orchestrator deploy separately, so they can be out of step. Callers use `run.yml@main`, so a merged schema change reaches `run.yml` at once, while the orchestrator changes only when it's redeployed. So `run.yml` accepts the current version **and the one before it**, and writes its result in the version it was sent, and the rule is: merge, then redeploy the orchestrator. If `run.yml` gets any other version (an orchestrator ahead of `run.yml`, or two versions behind), it fails before the agent starts, with `failure: contract_mismatch`. That costs seconds rather than Claude usage, and doesn't count as an attempt, because it isn't the ticket's fault. A `result.json` with an unknown version is `invalid`.
- If Claude rejects the token (expired, revoked, or the secret is missing), `run.yml` fails the run with `failure: claude_auth`. It isn't an attempt either.
- If a run hits the Claude usage limit partway through (Claude Code reports a `rate_limit` error), it fails with `failure: usage_limit`. That doesn't count as an attempt either. Runs don't wait out the limit (`CLAUDE_CODE_RETRY_WATCHDOG` stays off), because waiting would burn Actions minutes up to the timeout.

How the orchestrator responds to `contract_mismatch`, `claude_auth` and `usage_limit` is in [Failures that aren't attempts](orchestrator.md#dispatching-and-collecting).

Language types are generated from the schemas or tested against them, never written independently. On the orchestrator side they're pydantic models, tested against the schemas' examples. On the task side they're TypeScript types in `runner/contract-types.ts`, whose keys, required keys and literal values are tied to constants by the compiler, and those constants to the schemas by a test.

## Tasks

Each task's verdicts are a closed enum in its `output` sub-schema, and the orchestrator branches on them in code (see [Handling verdicts](orchestrator.md#handling-verdicts)). A run's `outcome` and a task's verdict are different things: the outcome says whether the run worked, and the verdict is what the agent decided.

**Prompts** live in the factory repo next to each task's script, and change only through a factory-repo PR. Enrolments have no per-repo prompt override in v1: a repo shapes the agent through its own `CLAUDE.md`, `AGENTS.md` and docs, which the agent reads from the checkout. There's no eval suite in v1; a prompt change is judged by `factory stats` and the PRs it produces.

**Skills.** The base image installs the factory's shared skills as a **plugin**, so they're namespaced (`factory:code-review`, `factory:to-tickets`) and never shadow, or get shadowed by, a target repo's own skills of the same name. Task prompts call the namespaced names.

**Permission mode.** Agents run with every tool allowed and no permission prompts. The container on a throwaway runner is the sandbox, nobody is there to answer a prompt, and each task's limits are enforced from outside the agent: [protected paths, review's reach, bundle checks](#the-runner-contract) and the timeout.

**`smoke`** is a task with no agent, used by [`factory doctor`](#enrolling-a-repo): it builds the image, runs setup, runs the check on the default branch's head, and uploads a `result.json` with an empty `output`. It proves the caller workflow, `run.yml`, the image and the repo's commands end to end without using Claude usage. It's never triggered by a label and isn't counted against the daily cap.

**Adding a task** means four pieces, all in the factory repo: an `output` sub-schema (with a `contract_version` bump), a task script and prompt, the verdict handling in the orchestrator, and a trigger label or a workflow that chains it.

### Explore

**One ticket per run.** Today's explore runs every labelled issue as one batch so it can spot duplicates among them. In the new factory, each explore run takes one ticket, which fits the `runs` table, the attempts limit and the dispatch. To keep duplicate detection, the dispatch also carries the **number and title of the 200 most recent open issues older than the ticket**.

- Titles only: the job's token can't read issues, and the prompt already says to confirm a duplicate from the code, not from the wording.
- Older only: the oldest issue survives, as today. Two duplicates explored at the same time can't close each other, because the newer one sees the older one but not the other way round.
- The cost: related tickets are explored separately, so the same code gets read twice. The gain: a new ticket is checked against every older open issue, not only the ones labelled in the same batch.

**Explore may run things.** It runs setup, so it can run the tests, reproduce a bug or try a query, and check the issue's claims rather than guess. Anything it changes is thrown away: it uploads no bundle.

**Output:** `verdict`; the findings as fields, `summary` (what the ticket really asks for), `findings` (what the code does today, and whether the claims held up), `plan` (steps), `files` (paths) and `questions` (required with `needs-a-human` and `waiting`); and `duplicate_of` (only with `duplicate`). The orchestrator writes the headings, so every explore comment reads the same way and the implementer always finds the plan in the same place.

| Verdict | Meaning |
|---|---|
| `easy-call` | The change is clear, and nobody would disagree with any decision in it |
| `needs-a-human` | Someone has to decide something first, or the issue's claims didn't hold up |
| `waiting` | Waiting on something outside the repo, or on information only the reporter has |
| `duplicate` | Same bug, root cause or request as the older issue in `duplicate_of` |
| `too-big` | Clear, but too big for one implement run |

What the orchestrator does with each verdict is in [Handling verdicts: Explore](orchestrator.md#explore).

- `waiting` was called `blocked`. It was renamed so it can't be confused with the `agent:blocked` label, which means the factory gave up after its attempts.
- `duplicate_of` must be one of the issues the orchestrator sent.
- The findings are required: they're the only thing explore produces, and the implement run reads them in `ticket.md`.
- **A reopened duplicate.** If a human reopens a ticket the factory closed as a duplicate, `ticket.md` on the next explore carries that history (see [Rendering tickets](orchestrator.md#rendering-tickets)). A human comment explaining why the ticket isn't a duplicate of #X settles it for #X. Without one, closing it again is fine, and it may still turn out to duplicate another issue. Rebuttals add up across rounds. Judging whether a comment is a rebuttal is the agent's job.

### Implement

Implement builds one ticket and commits on the branch the orchestrator named, starting from the base the orchestrator chose. It reads the ticket, including any explore findings, in `ticket.md`.

**Output:**

- `verdict`: `built` or `needs-a-human`.
- `comment`: required with `needs-a-human`. It says why the ticket can't be built as written.
- `pr_body` (optional): what the ticket asked for, what was built, and what was left out.

What the orchestrator does with each verdict is in [Handling verdicts: Implement](orchestrator.md#implement).

**Rules:**

- `built` with no commits on top of the base is an invalid result.
- `needs-a-human` gives the agent a way to say the ticket can't be built as written, for example when explore was wrong or a decision is needed. Without it, the only signal is a failed run, retried for another 45 minutes and then `agent:blocked` with no explanation.
- **It runs the check itself.** The prompt tells implement to run the repo's `check` command and keep working until it passes, and to end with `needs-a-human` if it can't get it green. The check `run.yml` runs afterwards stays the authority, but a slip no longer costs a whole retry.
- **It may touch any file** except the protected paths, with no cap on the diff's size in v1. Tickets vary too much for a rule; review and the human catch a wandering diff.
- **Commits** are authored by the App's bot identity (`<app-name>[bot]` and its `noreply` address), set by `run.yml`, with a `Co-Authored-By:` trailer naming `triggered_by` at their GitHub `noreply` address (`<id>+<login>@users.noreply.github.com`), so no private email is exposed. The run script rewrites every commit on the branch with that identity and trailer after the agent finishes, so neither depends on the agent. Commits aren't signed (see [Enrolling a repo](#enrolling-a-repo)).

### Review

Review reads a PR's diff, the repo's coding standards and the originating ticket or spec. It fixes every finding it can, as commits on the PR branch, and returns the rest. It shares nothing with the implement runs that built the PR.

It uses the `code-review` skill, which runs two reviewers as **sub-agents inside the one run**, each with a fresh context: **Standards** (the repo's documented standards, plus the skill's baseline list of code smells) and **Spec** (what's missing, what wasn't asked for, and what looks implemented but wrong). The main agent combines their reports and makes the fixes in one pass. They aren't separate tasks: two runs would fix the same branch at the same time and count twice against attempts and the daily cap.

**Instructions come from the base.** The PR head is checked out, so a PR that edits `CLAUDE.md`, `AGENTS.md` or `.claude/` would change the instructions its own review runs under. Before the agent starts, the run script restores those paths from the PR's base (and hides the restored files from git, so the agent can't commit them over the PR's), `result.json` says so with `instructions_changed: true`, and the verdict gets a line: "this PR changes agent instructions; it was reviewed against the base's".

**Output:**

- `standards`: `pass` or `changes-requested`, with a short `reasoning`. Both axes describe the PR **after** review's own fixes: a review that fixed every Standards finding says `pass`.
- `spec`: `pass`, `changes-requested` or `no-ticket`, with a short `reasoning`.
- `comments`: the findings it left unfixed, each with its axis (`standards` or `spec`), title, path, line and body, most important first.
- `fixed`: a short line per fix it made.
- `missing_rules` (optional): rules the review wanted to fail the change for that no standard covers. They stay in `runs.result`, and the retro reads them with `factory rules` (see [Observability](orchestrator.md#observability)).

**Fixes** are one commit per fix, with the finding in the message (e.g. `review: standards: extract the retry loop into a helper`), so a human can revert one without losing the rest. They may touch only the PR's own files and the repo's `test_paths` (see [Review's reach](#the-runner-contract)).

**Findings-only.** When the dispatch's findings-only flag is set (for PRs the factory didn't open), review pushes nothing: changes it would have made become findings.

Which PRs get reviewed, which spec a review checks against, and what the orchestrator does with the result are in [Handling verdicts: Review](orchestrator.md#review).

### Auto-tickets

Auto-tickets turns a spec issue into tickets. It follows spec explore in the [implement-spec](orchestrator.md#implement-spec) workflow and has no trigger label of its own. Its input includes the spec exploration notes. To make tickets without building them, use the `/to-tickets` skill, which also lets you check the breakdown.

Its prompt reuses the `/to-tickets` skill's slicing rules and issue template, minus the skill's "quiz the user" step, because nobody is there to answer. The quiz asks about the breakdown (granularity, blocking edges, merges and splits). Auto-tickets decides those itself, and its `needs-a-human` verdict is about something else: whether the spec can be broken down within 12 tickets. Judging the spec itself is spec explore's job.

**Output:**

- `verdict`: `decomposed` or `needs-a-human`.
- `comment`: required with `needs-a-human`.
- `tickets`: 1 to 12, blockers first. Each has a title, `## What to build`, `## Acceptance criteria` (as `- [ ]` checkboxes), `## Blocked by` (other tickets in the list, by list index, or none), and optionally `Related: #M`.

**Rules for the breakdown:**

- **Each ticket fits one implement run**, with its tests, inside implement's timeout, on top of `/to-tickets`' vertical slices. A spec that can't be cut that small within 12 tickets is the "more than 12" case.
- **Overlap with existing issues:** auto-tickets gets the same list of 200 older open issues as explore. A ticket that overlaps one gets `Related: #M`; auto-tickets never adopts or skips because of it, since pulling someone else's issue into a spec is a human's call.
- `needs-a-human` is only for a spec that needs more than 12 tickets (the comment then suggests how to split it). Open decisions, contradictions and claims that don't hold up are caught earlier, by spec explore. Granularity and blocking edges are never a reason to stop. If you disagree with a breakdown, fix the tickets by hand.

What the orchestrator does with each verdict, and how it creates the tickets, is in [Handling verdicts: Auto-tickets](orchestrator.md#auto-tickets).

## Enrolling a repo

A repo is enrolled with three changes, all on the repo itself: a `.factory/` folder, a caller workflow, and the GitHub App plus a Claude token secret. The factory repo names no enrolled repo, and the factory's code knows nothing about any particular repo.

```
factory repo:
  factory-config.yml  # owner: <github login>, see Observability in orchestrator.md
target repo:
  .factory/
    Dockerfile    # FROM the factory base image, plus the repo's toolchain
    config.yml
  .github/workflows/factory.yml   # the caller workflow
```

The enrolment is `.factory/`: a recipe for building and checking the repo. Each run checks out the target's code fresh from the target repo.

A repo is polled only when the App is installed on it **and** `.factory/config.yml` exists on its default branch (see [Enrolments](orchestrator.md#enrolments)).

**A PR can't change the check it's judged by.** The orchestrator always reads `.factory/` from the repo's **default branch**, never from a PR head or a factory branch, and passes that commit in the dispatch as `enrolment_sha`. `.factory/` is also a [protected path](#the-runner-contract), so no agent's commit can change it. Changing a project's recipe, such as its *toolchain* (e.g. a new Python version) or its check, takes a human merge to the default branch. Dependencies still come from the project's own lockfile through `setup`, so day-to-day changes don't touch `.factory/`.

`config.yml` holds:

- `setup`: e.g. `npm ci && npm run db:migrate && npm run db:seed`, or `uv sync`
- `check`: e.g. `npm run check`, or `pytest`
- `test_paths` (optional): globs for test files, e.g. `tests/**` or `**/*.test.ts`. Review's fixes may touch them as well as the PR's own files. Without it, fixes stay inside the PR's files.
- `tasks`: which tasks are enabled for this repo. Left out, every task is enabled. A trigger for a disabled task is rejected, and a workflow that needs one refuses to start (see [Enrolments](orchestrator.md#enrolments)).
- `enabled`: `true` or `false`. A disabled repo is still polled and its triggers are queued, but nothing is dispatched for it until it's enabled again. It stops one project without touching the others, and as a commit to the default branch it has a history.
- optional overrides of per-task **timeouts** (still capped at 60 by `run.yml`) and **models**. Concurrency limits, the daily cap and the usage thresholds stay global: they protect the one shared Claude subscription, and a repo shouldn't be able to raise its own share.

**Validation.** `config.yml` is defined by `contracts/enrolment.schema.json` in the factory repo. There's no central CI over every enrolment, so `factory doctor` validates it and builds the Dockerfile, and a project's own CI can do the same. At runtime, an enrolment that fails the schema isn't polled and opens a [factory problem](orchestrator.md#observability) on the repo.

**Target repo settings:**

- Actions enabled
- default workflow permissions read-only
- workflows from fork PRs need approval
- no required signed commits on `factory/*` branches: the factory's commits aren't signed, so its pushes would fail
- `factory/*` branches not blocked by rulesets

**The Claude token** is one secret, copied to every enrolled repo (a personal account has no account-wide secrets). A `setup-token` token lasts about a year. Rotating it is a short documented loop of `gh secret set` over the enrolled repos, run by hand: the App gets no `secrets` permission, because that would let the orchestrator write runners' secrets. An expired or missing token fails runs with `claude_auth` (see [The runner contract](#the-runner-contract)).

**`factory doctor <repo>`** checks an enrolment before a real ticket does, then dispatches a [`smoke`](#tasks) run (see [Enrolments](orchestrator.md#enrolments)).

**Unenrolling** is deleting `.factory/` from the default branch or uninstalling the App; what the orchestrator does then is in [Enrolments](orchestrator.md#enrolments). `enabled: false` stays the way to pause a repo.

## Code

- The orchestrator is written in Python. Its language is independent of the tasks, which run on the runner in the base image. The only coupling is the runner contract.
- It lives in a new **public factory repo**, a home repo, together with `run.yml`, the base image, `contracts/`, the shared skills the tasks use (`/to-tickets`, `code-review`, …), and today's `.sandcastle/` task scripts (TypeScript), which move there and are reused.
- The Python project is the first enrolled repo. The cohort repo, where this doc started, is not enrolled.
- How the orchestrator is deployed is in [Deploying](orchestrator.md#deploying).

## Rollout

The Python project has no factory today, so there is nothing to cut over from. It is a fresh enrolment.

1. Build the orchestrator, `run.yml`, the base image and the contracts.
2. Set the Actions spending limit to $20 a month on the account that owns the Python project (see [Visibility and cost](#runners)). The repo stays private.
3. Enrol the Python project:
   - add `.factory/Dockerfile` to the project (`FROM` the base image, plus Python and `uv`)
   - add `.factory/config.yml` to the project (setup `uv sync`, check e.g. `pytest`)
   - add the caller workflow to the project
   - install the GitHub App and add the Claude token secret
   - run `factory doctor` on it
4. Take one issue through the implement-ticket workflow (explore → implement → review) on the new factory.
5. After a month of normal use, compare `factory stats` with the Actions bill, and decide whether to stay on hosted minutes or move to a runner on our own hardware.

The cohort repo is left alone. Its Actions factory keeps running until the course ends, and it is never enrolled, so the two factories never watch the same labels.

## Open questions

Open questions about the orchestrator itself are in [orchestrator.md](orchestrator.md#open-questions).

- **Screenshots as PR evidence.** Repos with a UI (the Python project is a web app) should show before and after screenshots on the PR, as today's factory does. In v1 the screenshots travel in the artifact and nothing posts them. Still to decide: how the orchestrator hosts them so they show inline (GitHub has no official API for attaching images to issues or PRs), and what field `result.schema.json` gets for them, which will be added with a version bump.
- **Network egress from the agent.** The agent has open internet access, so a prompt injection in an outsider's issue could get it to `curl` the Claude token somewhere; the prose scan only catches a token that lands in `output.json`. v1 accepts this: only someone with triage access can add a trigger label, so a human has seen the text first, and the token can be revoked and reissued. The first thing to try later is running setup with network access and the agent with none except Anthropic's API; an egress allowlist through a proxy is the alternative.
- **Recording each run's usage.** An optional `usage` field in `result.json` (tokens, plus the run's `rate_limit_event` readings) would show what each task typically costs, and could tune the thresholds. Deferred until the usage pause has run for a while.
- **Hosted minutes or our own hardware.** v1 runs a private repo on paid hosted minutes, capped at $20 a month (see [Visibility and cost](#runners)). At about $0.006 a minute on Linux, after the 2,000–3,000 included minutes, the factory flat out at 40 runs a day would cost roughly $170 a month, and about $33 at 10. If the first month shows the limit is too tight, the alternative is a runner on our own hardware:
  - A Mac mini costs about $800–1,300 at 2026 prices. But `run.yml` builds Linux images, which need a VM on macOS, and Actions' container features are Linux-only, so a dedicated small Linux box would be the better buy.
  - The box would run each target's code with the Claude token, so it would need its own network, away from the family's, and **ephemeral** runners that re-register after every job. GitHub advises against self-hosted runners on public repos.
  - It needs upkeep: updates, disk cleanup, staying awake. Jobs wait in GitHub's queue while it sleeps, and fail after about 24 hours.
  - Self-hosted minutes are free today. GitHub announced a $0.002-a-minute fee for them on private repos from March 2026, then postponed it with no new date. If GitHub starts charging in a way that matters, revisit the factory's setup rather than pay.
