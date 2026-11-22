# Reading Claude subscription usage limits from a script

Research date: 2026-11-21. Claude Code version checked locally: 2.1.328.

Question: can an unattended script (Mac mini, no browser) read how much of a
Claude Pro/Max subscription's 5-hour and weekly limits remain, so an
orchestrator can pause dispatching Claude Code runs below a threshold?

## Short answer

**Yes, through documented Claude Code surfaces. The data has some caveats.**
Anthropic has no standalone "usage API" for subscriptions. Claude Code itself
reports plan utilization in machine-readable form in three documented places:

1. **`claude -p "/usage" --output-format stream-json --verbose`**: returns a
   structured `usage_report` with a `percent` and `resets_at` for the session
   (5-hour) and weekly meters. It makes no model call and reported
   `total_cost_usd: 0` when tested locally. The type is marked experimental.
   It needs an OAuth login with the `user:profile` scope. A `claude setup-token`
   token probably lacks that scope (see uncertainties).
2. **`rate_limit_event` messages in `--output-format stream-json`** (and the
   Agent SDK): emitted during any normal run, with `utilization` and `resetsAt`.
   This is documented. It comes for free with every run you already dispatch,
   so the GitHub Actions runs can report it back.
3. **Status line JSON `rate_limits.five_hour` / `rate_limits.seven_day`**:
   documented, but status lines run only in the interactive TUI. They are no
   use to a headless script.

**Recommended approach:**

- **Primary:** on the Mac mini, run `claude -p "/usage"` with stream-json as a
  zero-token probe before each dispatch, or every few minutes. Parse
  `usage_report.rate_limits.limits[]`, using `kind` (`session`, `weekly_all`,
  `weekly_scoped`) and `percent`. Pause when any row crosses the threshold.
  Authenticate the Mac mini with a full `claude auth login`, not only
  `CLAUDE_CODE_OAUTH_TOKEN`, so the `user:profile` scope is present.
- **Secondary, also free:** make each GitHub Actions run save the
  `rate_limit_event` lines from its stream-json output (for example as a job
  artifact or summary) and feed them back to the orchestrator. That catches
  `allowed_warning` and `rejected` states as they happen.
- **Fallback:** if the probe returns `rate_limits: null`, send a minimal model
  request (Haiku, "reply ok") and read its `rate_limit_event`. This costs a
  small amount of usage per probe.

Both the 5-hour and weekly limits are shared with claude.ai and Claude Desktop
use on the same account. A percentage the orchestrator reads can therefore move
because of activity outside the factory.

## Findings per mechanism

### 1. `/usage` run non-interactively (documented, experimental type)

- `/usage` "Show[s] session cost, plan usage limits, and activity stats". `/cost`
  and `/stats` are aliases.
  Source: https://code.claude.com/docs/en/commands
- In `-p` mode, user-invoked skills and some built-in commands work in the
  prompt string. Terminal-only commands such as `/login` don't.
  Source: https://code.claude.com/docs/en/headless (section "Create a commit", note on command support)
- The Agent SDK reference documents `SDKUsageReport`: "Structured form of the
  `/usage` report, carried as `usage_report` on the `SDKAssistantMessage` that
  delivers a `/usage` result ... The type is experimental: its shape may
  change." Each row of `rate_limits.limits` has `kind` (`session`,
  `weekly_all`, `weekly_scoped`), `group`, `percent` (0-100), `resets_at`
  (ISO 8601), `scope`, `severity` (`normal`/`warning`/`critical`) and
  `is_active`. `rate_limits` "is `null` when Claude Code couldn't get the plan's
  usage, for example when the session's OAuth token lacks the `user:profile`
  scope."
  Source: https://code.claude.com/docs/en/agent-sdk/typescript (section `SDKUsageReport`)
- The usage data comes from a separate request that can itself be rate limited.
  `/usage` then shows "last-known usage" up to 60 minutes old, or a "rate
  limited until ..." message. Poll sparingly.
  Source: https://code.claude.com/docs/en/costs (section "When the usage request fails")
- **Tested locally** (Pro account, interactive OAuth login, Claude Code 2.1.328):
  `claude -p "/usage" --output-format stream-json --verbose` emitted an
  assistant message carrying `usage_report`, with
  `{"kind":"session","percent":17,"resets_at":"2026-11-22T00:20:00Z",...}` and
  `{"kind":"weekly_all","percent":3,"resets_at":"2026-11-24T18:00:00Z",...}`.
  The result had `num_turns: 0` and `total_cost_usd: 0`, so no model call was
  made. The plain-text `result` field also carries the human-readable report.

### 2. `rate_limit_event` in stream-json / Agent SDK (documented)

- TypeScript `SDKRateLimitEvent`: `{ type: "rate_limit_event", rate_limit_info:
  { status: "allowed" | "allowed_warning" | "rejected", resetsAt?, utilization?,
  ... } }`, "Emitted when the session encounters a rate limit."
  Source: https://code.claude.com/docs/en/agent-sdk/typescript (section `SDKRateLimitEvent`)
- Python `RateLimitInfo` documents more fields: `rate_limit_type` (one of
  `five_hour`, `seven_day`, `seven_day_opus`, `seven_day_sonnet`, `overage`),
  `utilization` ("Fraction of the rate limit consumed (0.0 to 1.0)"),
  `resets_at` (Unix timestamp) and `raw` (the full dict from the CLI). It is
  described as "Emitted when rate limit status changes (for example, from
  `"allowed"` to `"allowed_warning"`)."
  Source: https://code.claude.com/docs/en/agent-sdk/python (sections `RateLimitEvent`, `RateLimitInfo`)
- Changelog: "Added `SDKRateLimitInfo` and `SDKRateLimitEvent` types to the SDK,
  enabling consumers to receive rate limit status updates including
  utilization, reset times, and overage information."
  Source: https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md
- The CLI emits the same event in `claude -p --output-format stream-json
  --verbose`, because the SDK is built on the CLI's stream-json. The headless
  page doesn't list it explicitly, but the changelog refers to "headless
  `rate_limit_event` usage-limit warnings".
  Source: https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md
- **Tested locally:** one tiny Haiku request produced one event with status
  `allowed`, even though no limit was near:
  `{"status":"allowed","resetsAt":1795306800,"rateLimitType":"five_hour","utilization"...,"unifiedWindows":{"five_hour":{"utilization":0.17,...},"seven_day":{"utilization":0.03,...}}}`.
  **The `unifiedWindows` object, which gives both windows at once, is
  undocumented.** The documented fields only cover the one "representative"
  window named by `rateLimitType`. These numbers matched the `/usage` probe
  (17% and 3%).
- A `StopFailure` hook and the `system/api_retry` event report `error:
  "rate_limit"` when a run is actually blocked. These signals fire too late to
  serve as a threshold, but they work as a backstop.
  Sources: https://code.claude.com/docs/en/hooks, https://code.claude.com/docs/en/headless (section "Handle API retries")

### 3. Status line JSON (documented, interactive only)

- Status line scripts receive `rate_limits.five_hour.used_percentage`,
  `rate_limits.seven_day.used_percentage` (0-100) and `.resets_at` (Unix epoch
  seconds). These are present "only for claude.ai Pro, Max, and Ultra
  subscribers ... only after the first API response in the session."
  Source: https://code.claude.com/docs/en/statusline (sections "Available data", "Rate limit usage")
- The status line is part of the interactive terminal UI. It runs on events
  such as new assistant messages or a `refreshInterval` timer, so a headless
  orchestrator can't use it directly. A long-lived interactive session could
  write its status line input to a file, but that is a workaround, not an
  interface.
  Source: https://code.claude.com/docs/en/statusline (section "How status lines work")

### 4. `--output-format json` result fields (documented, no limit data)

- The `result` message carries `usage` (token counts), `total_cost_usd` and
  per-model `modelUsage`. Cost is a "client-side estimate". For subscribers it
  "isn't relevant for billing purposes."
  Sources: https://code.claude.com/docs/en/headless (section "Pipe data through Claude"), https://code.claude.com/docs/en/costs
- The result has no remaining-limit field (checked locally: no such key in the
  `result` message).

### 5. OpenTelemetry (documented, no limit data)

- Metrics: `claude_code.session.count`, `lines_of_code.count`,
  `pull_request.count`, `commit.count`, `cost.usage`, `token.usage`,
  `code_edit_tool.decision`, `active_time.total`. Events include `api_request`
  (with `cost_usd`) and `api_error`. None reports plan utilization.
  Source: https://code.claude.com/docs/en/monitoring-usage
  (Only the first ~100k characters of this long page were read. The metric list is near the top.)

### 6. Raw API response headers (undocumented)

- The public API docs list only `anthropic-ratelimit-requests-*`,
  `-tokens-*`, `-input-tokens-*` and `-output-tokens-*` headers, which cover
  API-key organisation rate limits. No `unified` header is documented.
  Source: https://platform.claude.com/docs/en/api/rate-limits (section "Response headers")
- **Undocumented:** the Claude Code 2.1.328 binary contains header names such
  as `anthropic-ratelimit-unified-5h-utilization`, `-5h-reset`, `-5h-status`,
  `-7d-utilization`, `-7d-reset`, `-representative-claim` and
  `-overage-status` (found with `grep -a` on the installed binary). This
  matches the `rate_limit_event` / `unifiedWindows` data above. Calling the
  API directly with the subscription OAuth token to read these headers is
  reverse-engineered behaviour. It can break without notice and may conflict
  with the token's intended use. Use the Claude Code surfaces instead.
- **Undocumented:** the binary also references an `api/oauth/usage` path,
  probably the "usage endpoint" that `/usage` calls. Its contract isn't
  published. Third-party tools reportedly call it directly. Treat it as
  unofficial.

### 7. Admin / Usage & Cost APIs (not applicable to Pro/Max)

- "The Admin API isn't available to organizations on the claude.ai Team plan
  or to individual Claude Console accounts." The APIs serve Claude Console
  organisations (Admin API key) and Claude Enterprise (Analytics API key).
  Source: https://platform.claude.com/docs/en/build-with-claude/usage-cost-api
- The Usage endpoint doesn't count "Claude Code in the terminal, IDEs, and the
  desktop app". The Cost endpoint "doesn't include seat fees or usage that a
  seat covers."
  Source: same page
- No official API exists for individual Pro/Max plan utilization outside
  Claude Code itself.
  Source: https://code.claude.com/docs/en/costs (table "Manage costs for your organization": individual plans "have no organization to manage")

### 8. How the limits are officially described

- Pro and Max have "a five-hour session limit and a weekly limit". All activity
  in Claude and Claude Code "counts against the same limits".
  Source: https://support.claude.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan
- 5-hour window: "The period starts when you send your first message, and your
  limit resets in full five hours later." Weekly: "reset at a fixed time each
  week that is assigned to your account". Usage across claude.ai, Claude Code
  and Claude Desktop "counts towards the same usage limit".
  Source: https://support.claude.com/en/articles/11647753-how-do-usage-and-length-limits-work
- What is measured: there is no published unit. Usage depends on "the length
  and complexity of your conversations, the features you use, which Claude
  model you're chatting with, and the effort level". Utilization is therefore
  only available as a percentage. There is no token budget to count against.
  Source: same page
- Error messages: "You've hit your session limit · resets 3:45pm", "You've hit
  your weekly limit", plus per-family Opus and Sonnet limits. "Usage counts
  against the session and weekly allowances at the same time."
  Source: https://code.claude.com/docs/en/errors (section "You've hit your session limit")
- Built-in waiting: `CLAUDE_CODE_RETRY_WATCHDOG=1` makes unattended runs wait
  out a usage limit instead of failing. `CLAUDE_CODE_RETRY_WATCHDOG_MAX_USAGE_LIMIT_WAIT_MS`
  caps that wait. This is an alternative or complement to pausing dispatch.
  Source: https://code.claude.com/docs/en/env-vars

### 9. `claude setup-token` and scopes

- `setup-token` produces a one-year OAuth token for `CLAUDE_CODE_OAUTH_TOKEN`.
  "It can only make model requests, so it can't establish Remote Control
  sessions or fetch claude.ai connectors."
  Source: https://code.claude.com/docs/en/authentication (section "Generate a long-lived token")
- `/usage` plan data needs the `user:profile` scope (see section 1). Example
  full scope set: `"user:profile user:inference user:sessions:claude_code"`.
  Source: https://code.claude.com/docs/en/env-vars (`CLAUDE_CODE_OAUTH_SCOPES`)

### 10. Fallback: estimating from per-run token usage (your own estimate)

If none of the above were available, the orchestrator could record `usage` and
`modelUsage` from each run's JSON result and compare the totals against
observed limit hits. This is weak. Anthropic publishes no token budget for the
plans, the budget depends on model and effort, and claude.ai use on the same
account is invisible to it. Treat this only as a last resort. Community tools
such as ccusage do this from local transcripts. They are unofficial and only
see this machine.

## Open uncertainties

- **`setup-token` and `/usage`:** not tested. The documentation strongly
  suggests (inference-only scope, plus "`null` when ... lacks `user:profile`")
  that a probe authenticated only with `CLAUDE_CODE_OAUTH_TOKEN` returns
  `rate_limits: null`. Check this once on the Mac mini. If it does, log the
  Mac mini in with `claude auth login` for the probe.
- **`rate_limit_event` frequency:** the Python docs say it fires on a status
  change, but locally it fired on a run with status `allowed`. It is unclear
  whether every run emits at least one, or how often it repeats within a long
  run.
- **`unifiedWindows`** is not in any documented type. Without it, the
  documented event only gives the utilization of the window named by
  `rateLimitType`, which may not be the one closest to its limit.
- **`SDKUsageReport` is experimental.** Its shape may change. Classify rows by
  `kind`, as the docs advise, and handle unknown kinds.
- **Polling limits on the usage endpoint** are not published. `/usage` docs
  only say it can be rate limited. A probe every few minutes is a guess, not a
  documented safe rate.
- **Shared pool:** claude.ai and Claude Desktop use on the same account moves
  the same percentages. The orchestrator can see this but can't attribute it.
- **GitHub Actions:** not checked whether `anthropics/claude-code-action`
  exposes the stream-json lines (and so `rate_limit_event`) as an output or
  log artifact by default.
- **Plan-specific scoped limits** (Opus/Sonnet weekly, Fable on Max/Ultra)
  appear as `weekly_scoped` rows or `seven_day_opus`/`seven_day_sonnet` types.
  Not observed on the Pro account tested.
