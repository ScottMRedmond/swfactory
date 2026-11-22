# The task side is TypeScript, the orchestrator is Python

The run script and task code in `runner/` (everything that executes on a runner) are written in TypeScript and run with Node, while the orchestrator is written in Python. Node is already in the base image because Claude Code runs on it, so a TypeScript run script needs no extra toolchain in any enrolled repo's image. The existing Sandcastle task scripts were TypeScript and are reused. Sandcastle and the Claude Agent SDK are TypeScript-first, too. The two sides share no code and meet only at the JSON Schemas in `contracts/`, so neither language constrains the other.

## Considered options

- **Python, matching the orchestrator.** Rejected: enrolled repos bring their own toolchains, so Python isn't guaranteed in the run image, and the existing task scripts would have to be rewritten.
- **A shell script** (the design doc's example name was `run-task.sh`). Rejected: the run script validates JSON Schemas, retries the agent once on invalid output and always writes a valid `result.json`. That logic is hard to write and test in shell.
- **TypeScript for the orchestrator too, so both sides share one language.** Not required: the runner contract already decouples them. Revisit if the orchestrator calls Sandcastle directly (see "A possible future runner" in `docs/software-factory.md`).

## Consequences

- Types on each side (zod here, Python models in the orchestrator) are generated from the schemas in `contracts/` or tested against them, never written independently.
- Any future runner must provide Node, which the base image already does.
