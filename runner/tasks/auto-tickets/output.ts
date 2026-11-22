// What auto-tickets' output must satisfy beyond its JSON Schema: blockers
// come first, so each ticket's blocked_by names only earlier tickets. The
// orchestrator relies on it to rewrite indices to issue numbers as it
// creates the issues in order.

// null when the rule holds (or there are no tickets), otherwise the errors.
export const blockersFirst = (output: unknown): string | null => {
  const tickets = (output as { tickets?: { blocked_by?: unknown }[] })?.tickets;
  if (!Array.isArray(tickets)) return null;
  const errors = tickets.flatMap((ticket, i) => {
    const later = (Array.isArray(ticket?.blocked_by) ? ticket.blocked_by : []).filter(
      (n) => typeof n !== "number" || n >= i,
    );
    return later.length
      ? [`/tickets/${i}/blocked_by must name only an earlier ticket (an index below ${i}), not ${later.join(", ")}`]
      : [];
  });
  return errors.length ? errors.join("; ") : null;
};
