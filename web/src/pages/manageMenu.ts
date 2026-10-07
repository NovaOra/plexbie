/** One line of Manage's sections menu (☰): "Tickets  2 open". */
export type MenuItem<S extends string = string> = { id: S; label: string; note: string; hot: boolean; current: boolean };

/** "All requests · 2 stuck", "Invites · 1 ready" (a new link to send); everything else counts what's waiting. */
const word = (s: string) => (s === "all" ? "stuck" : s === "tickets" ? "open" : s === "messages" ? "new" : s === "invites" ? "ready" : s === "health" ? "down" : "waiting");

/**
 * Every section, in order, with what it counts ("3 waiting", "1 down", or
 * "couldn’t refresh" when its count is in doubt) and which one is open. Health
 * and Cleanup with something in them are hot: a service down, someone about to
 * lose access.
 */
export function menuItems<S extends string>(tabs: readonly { id: S; label: string }[], current: S,
  counts: Partial<Record<S, number>>, failed: Partial<Record<S, boolean>>): MenuItem<S>[] {
  return tabs.map(({ id, label }) => {
    const n = counts[id];
    // The ready link is held on this page, so a failed invites refresh doesn't put it in doubt.
    const note = !n ? "" : failed[id] && id !== "invites" ? "couldn’t refresh" : `${n} ${word(id)}`;
    return { id, label, note, hot: !!n && (id === "health" || id === "cleanup"), current: id === current };
  });
}

/** What's waiting outside the open section (whose own count is in its heading). */
export const waitingElsewhere = <S extends string>(items: MenuItem<S>[]) => items.filter((i) => i.note && !i.current);

/** The ☰ button's name, which says what its dot means. */
export function menuLabel(items: MenuItem[]) {
  const n = waitingElsewhere(items).length;
  return n ? `Sections, ${n} ${n === 1 ? "needs" : "need"} you` : "Sections";
}
