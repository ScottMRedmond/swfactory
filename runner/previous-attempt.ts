// A retry's ticket.md: the ticket, then a "Previous attempt" section saying
// how the last attempt ended, so the agent starts from what broke. See
// Attempts under "Limits and brakes" in docs/orchestrator.md.

import type { PreviousAttempt } from "./contract-types";

// A code block that the text can't close early, whatever backticks it holds.
const block = (text: string): string => {
  const longest = Math.max(0, ...(text.match(/`+/g) ?? []).map((run) => run.length));
  const fence = "`".repeat(Math.max(3, longest + 1));
  return `${fence}\n${text.replace(/\n$/, "")}\n${fence}`;
};

export const withPreviousAttempt = (ticket: string, previous?: PreviousAttempt): string => {
  if (!previous) return ticket;
  const parts = ["## Previous attempt"];
  if (previous.outcome === "invalid") {
    parts.push("The previous attempt at this ticket produced an invalid result:");
    parts.push(block(previous.validation_error ?? ""));
  } else {
    parts.push(`The previous attempt at this ticket failed: ${previous.failure}.`);
  }
  if (previous.check_output) {
    parts.push("The last lines of its check output:", block(previous.check_output));
  }
  return `${ticket.trimEnd()}\n\n${parts.join("\n\n")}\n`;
};
