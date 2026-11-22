# Porting notes

These task scripts and prompts were copied as-is from the cohort repo, where they ran inside its Actions workflows (kept for reference in `reference/cohort-workflows/`). They still assume that repo. The task tickets port them to the runner contract in `docs/software-factory.md`. This file lists what's cohort-specific, so nothing is missed. Delete it once every task is ported.

## Assumes the cohort app

- Prompts point at the cohort's own files: `CODING_STANDARDS.md`, `GLOSSARY.md`, `app/db/schema.ts`, and deep modules under `app/modules/` (explore, implement, review and both implement-spec prompts). An enrolled repo shapes the agent through its own `CLAUDE.md`, `AGENTS.md` and docs instead.
- Prompts name the cohort's commands: `npm run check`, `npm run build`, `npm run test`, `npm run typecheck`. The factory runs the enrolment's `setup` and `check` instead.
- Implement's prompts forbid editing `.husky/` and `scripts/pre-commit.sh`, and review's prompt mentions `scripts/check-file-tokens.js`. None of these exist in an enrolled repo. The protected paths (`.github/workflows/`, `.factory/`) replace them.
- The workflows ran `npm run db:migrate` and `npm run db:seed` and installed `agent-browser@0.27.0` before the agent ran. That is now the enrolment's `setup` and the base image.

## Assumes today's Actions factory

- Inputs and outputs are files in `/tmp` (`ticket.md`, `spec.md`, `review-spec.md`, `pr-body.md`, `review-verdict.md`), with prose pulled out by an extraction step (`shared/run-with-extraction.ts`). The contract has the agent write one `output.json` validated against its task's sub-schema instead.
- The zod schemas (`explore/plan-schema.ts`, `review/review-schema.ts`, `implement-spec/decomposition-schema.ts`) predate `contracts/`. Generate or test them against the JSON Schemas instead of keeping them separately.
- The model is hard-coded (`claude-opus-5-5` in `shared/common.ts`). The dispatch carries the task's model.
- The review prompt reads the skill from `.agents/skills/code-review/SKILL.md`. Tasks call the namespaced `factory:code-review` from the base image's plugin.
- `implement-spec/decompose.ts` becomes the auto-tickets task. `implement-spec/build.ts` is replaced by implement running on the integration branch.
- `.sandcastle/Dockerfile` installs `gh`. Runners don't call GitHub, so the base image doesn't need it.

## Not part of the factory

`main.ts`, `interactive.ts`, `test.ts` and `prompt.md` at the top of `.sandcastle/` are the cohort's PRD loop (they still use `claude-opus-4-6`). No task uses them. Delete them unless you want the loop.
