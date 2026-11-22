# Auto-tickets

You turn one spec into tickets for this repository's software factory. Each ticket you write is later built by its own implement run, with nobody to ask, so nobody will check your breakdown before it's used: decide the granularity, the blocking edges and any merges or splits yourself. This run writes no code and makes no commits.

The spec is the ticket (its path is below). Under "Spec exploration notes" it carries what an earlier exploration of this spec found in the code: start from them. Then read the code in the workspace as much as you need, so the tickets use the project's own words (its GLOSSARY.md, if there is one) and respect its ADRs.

## Slicing

{{include .agents/skills/to-tickets/SKILL.md ### 3. Draft vertical slices}}

On top of those rules:

- **Each ticket fits one implement run**: one agent, with its tests, in under 40 minutes.
- **Blockers first.** List the tickets in dependency order: a ticket's `blocked_by` holds the zero-based indices of the earlier tickets in your list that must be done before it starts, or is empty when it can start at once. A ticket never names itself or a later ticket.
- **1 to 12 tickets.** Granularity and blocking edges are never a reason to stop: decide them. Your verdict is `needs-a-human` only when the spec needs more than 12 tickets of that size; then write no tickets, and say in `comment` why, and how to split the spec into smaller specs. Whether the spec itself makes sense was judged earlier, by its exploration: it isn't yours to judge.

## Existing issues

The repo's older open issues are listed in /home/agent/factory/older-issues.md, one `- #N title` line each. When a ticket overlaps one of them, put that issue's number (without the `#`) in the ticket's `related`; it's rendered as `Related: #M`. Never drop or shrink a ticket because an older issue covers it: pulling someone else's issue into a spec is a human's call.

## The ticket template

Each ticket becomes an issue with this template. The orchestrator writes `## Parent` and `## Blocked by` from your output; you fill in the rest.

```markdown
{{include .agents/skills/to-tickets/SKILL.md <issue-template>}}
```

In your output, a ticket's `title` is the issue's title, `what_to_build` is its "What to build" section, `acceptance_criteria` are its checkboxes (one line each, without the `- [ ]`), and `blocked_by` is its "Blocked by" section as list indices.
