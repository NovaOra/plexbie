import type { MemberTicket, MyTicket } from "../api/types";
import { formatSlot } from "./ui";

/** "Your ticket on No. 0214", "Your ticket on Sintel" or (a general one) "Your ticket". */
export function ticketHeading(subject: MemberTicket["subject"], title?: string | null, slot?: number | null) {
  if (subject === "general" || !title) return "Your ticket";
  return subject === "request" && slot ? `Your ticket on No. ${formatSlot(slot)}` : `Your ticket on ${title}`;
}

/** A reason inside a sentence ("Your ticket: won’t play on Plex"): lower-cased, names kept. */
export const inLine = (reason: string) =>
  reason.toLowerCase().replace(/\b(plex|plexbie|sonarr|radarr|audiobookshelf)\b/g, (w) => w[0].toUpperCase() + w.slice(1));

/** One of the member's own tickets (their list of them) as the ticket sheet shows it. */
export const asMemberTicket = (t: MyTicket): MemberTicket =>
  ({ id: t.id, reason: t.reason, subject: t.subject, status: t.status, waiting: t.waiting, thread: t.thread });
