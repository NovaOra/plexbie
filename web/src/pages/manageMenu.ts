import { days, type Area, type Desk, type DeskData, type Kind } from "./manageQueue";

/**
 * Manage's "Everything else" menu: every full view, grouped by job like the monitors,
 * with the same counts. A row's count is the queue rows it covers, so the menu, the
 * monitors and the queue always agree. Its line under the name starts with what makes
 * up that count, from the queue too; anything it doesn't count is said as "more" or
 * "under" the row that does, never as a number that looks like part of it.
 */

/** Where a menu row goes: `?tab=` (the full view) and, within it, `part`. */
export type MenuRow = {
  id: string; label: string; desc: string; tab: string; part?: string;
  /** How many queue rows it covers (none shown when 0 or not counted). */
  count?: number;
  /** "2 urgent" beside the count. */
  urgent?: number;
  /** Urgent, for a row whose urgent rows count elsewhere (a blocked import in its ticket). */
  hot?: boolean;
  /** A filled "1 down" tally in place of the count; "live" for a watch party on now. */
  tally?: "down" | "live";
};
export type MenuGroup = { id: Area | "discord"; label: string; count?: number; makeup?: string; rows: MenuRow[] };

const NB = " ";

export function menuGroups(desk: Desk, d: DeskData): MenuGroup[] {
  const of = (...kinds: Kind[]) => desk.needs.filter((i) => kinds.includes(i.kind));
  const urgentIn = (...kinds: Kind[]) => of(...kinds).filter((i) => i.group === "urgent").length;
  const mon = (a: Area) => desk.monitors.find((m) => m.area === a)!;
  // A group whose feeds couldn't load says so, never a count that looks final.
  const group = (id: Area, rows: MenuRow[]): MenuGroup => mon(id).failed
    ? { id, label: mon(id).label, makeup: "Count unknown", rows }
    : { id, label: mon(id).label, count: mon(id).count, makeup: mon(id).makeup, rows };

  const pending = of("request").length;
  const stuck = of("stuck").length;
  // Stuck requests whose open ticket is counted under Help instead.
  const openTickets = (d.tickets?.rows ?? []).filter((t) => t.status === "open");
  const ticketed = new Set<string | null | undefined>(openTickets.filter((t) => t.requestKey).map((t) => t.requestKey));
  const stuckTicketed = (d.all?.rows ?? []).filter((r) => r.stuck.length && r.status !== "pending" && ticketed.has(r.id)).length;
  const older = d.requests?.older.length ?? 0;
  const tickets = of("ticket").length;
  const waitingOn = desk.waiting.length;
  const blocked = d.blocked ?? [];
  const imports = of("import");
  const inTickets = blocked.filter((b) => b.ticket && openTickets.some((t) => t.id === b.ticket));
  const chats = of("chat").map((i) => i.title);
  const folded = of("warned").filter((i) => i.src.type === "person" && i.src.chat?.unread).map((i) => i.title);
  const warned = of("warned").map((i) => i.title);
  const stale = of("invite");
  const notAccepted = (d.plexinvites?.length ?? 0) - stale.length;
  const links = of("link");
  const active = (d.invites ?? []).filter((i) => i.status === "active").length;
  const cleanup = d.cleanup?.settings;
  const down = (d.health ?? []).filter((h) => !h.ok);
  const party = d.discord?.party;
  const list = (names: string[]) => (names.length > 2 ? `${names.slice(0, 2).join(", ")} and ${names.length - 2} more` : names.join(" and "));

  return [
    group("requests", [
      { id: "all", label: "All requests", tab: "all", count: pending + stuck,
        desc: [pending ? `${pending}${NB}waiting` : "", stuck ? `${stuck}${NB}stuck` : "",
          stuckTicketed ? `${stuckTicketed}${NB}${stuck ? "more " : ""}stuck under Help` : ""].filter(Boolean).join(" · ") || "The last 30 days, and every request ever" },
      { id: "older", label: "Older requests", tab: "requests", part: "older", desc: older ? `${older} from before outcomes were recorded` : "None" },
    ]),
    group("help", [
      { id: "tickets", label: "All tickets", tab: "tickets", count: tickets, urgent: urgentIn("ticket"),
        desc: d.tickets ? [tickets ? `${tickets}${NB}need you` : "None need you", urgentIn("ticket") ? `${urgentIn("ticket")}${NB}urgent` : "",
          waitingOn ? `${waitingOn}${NB}more waiting on a reply` : ""].filter(Boolean).join(" · ") : "Loading…" },
      // One without a ticket is counted here; one in a ticket counts under All tickets.
      { id: "blocked", label: "Blocked imports", tab: "health", part: "blocked", count: imports.length, urgent: urgentIn("import"),
        hot: desk.needs.some((i) => i.group === "urgent" && i.src.type === "ticket" && !!i.src.blocked),
        desc: [imports.length ? list(imports.map((i) => i.title)) : "", inTickets.length ? `${list(inTickets.map((b) => b.title))} under All tickets` : ""]
          .filter(Boolean).join(" · ") || "None" },
      { id: "messages", label: "Messages", tab: "messages", count: chats.length,
        desc: [chats.length ? `${list(chats)} unread` : "", folded.length ? `${list(folded)} under People` : ""].filter(Boolean).join(" · ") || "Nothing unread" },
    ]),
    group("people", [
      { id: "joins", label: "Join requests", tab: "joins", count: of("join").length, desc: "Waiting and earlier" },
      { id: "people", label: "People", tab: "people", count: warned.length,
        desc: [warned.length ? `${list(warned)} warned` : "", d.people ? `${d.people.length}${NB}on Plex` : ""].filter(Boolean).join(" · ") || "Everyone on Plex" },
      { id: "plex", label: "Waiting on Plex", tab: "invites", part: "plex", count: stale.length,
        desc: [stale.length ? `${stale.length} from ${stale[0].age} ago` : "", notAccepted > 0 ? `${notAccepted}${NB}${stale.length ? "more " : ""}not accepted` : ""].filter(Boolean).join(" · ") || "None waiting" },
      { id: "links", label: "Invite links", tab: "invites", count: links.length,
        desc: links.length && links[0].src.type === "link" ? `${links[0].src.i.label}’s ends ${links[0].due ? `in ${days(links[0].due)}` : "today"}` : active ? `${active}${NB}open` : "Make one for someone without Discord" },
      { id: "who", label: "Who brought whom", tab: "invites", part: "who", desc: "Invite links and Discord codes used" },
    ]),
    group("library", [
      { id: "leaving", label: "Leaving soon", tab: "cleanup", count: of("leaving").length, desc: `Your ${cleanup?.warnDaysBefore ?? 7}-day warning window` },
      { id: "keep", label: "Keep forever", tab: "cleanup", part: "keep", desc: `${d.cleanup ? `${d.cleanup.exempt.length}${NB}kept · ` : ""}search Plex to add` },
      { id: "settings", label: "Cleanup settings", tab: "cleanup", part: "settings",
        desc: cleanup ? `${cleanup.enabled ? (cleanup.practice ? "Practice" : "Live") : "Off"} · ${cleanup.warnDaysBefore}-day warning · Scan now` : "Mode, days, libraries, Scan now" },
    ]),
    group("services", [
      { id: "services", label: "Services", tab: "health", count: down.length, tally: down.length ? "down" : undefined,
        desc: d.health ? (down.length ? `${list(down.map((h) => h.name))} down · ${d.health.length}${NB}checks` : `${d.health.length}${NB}checks · all up`) : "Plex, Seerr, Sonarr, Radarr…" },
      { id: "security", label: "Sign-ins", tab: "security", desc: "Sign out every other session" },
    ]),
    {
      id: "discord", label: "Discord", rows: [
        { id: "party", label: "Watch party", tab: "discord", part: "party", tally: party ? "live" : undefined, desc: desk.facts.party ?? "None right now" },
        { id: "say", label: "Say something as Plexbie", tab: "discord", part: "say", desc: "Post in a Discord channel" },
        { id: "dms", label: "DMs to Plexbie", tab: "discord", part: "dms", desc: d.discord?.inbox ? `Auto-reply ${d.discord.inbox.autoreply ? "on" : "off"}` : "Plexbie’s inbox" },
      ],
    },
  ];
}
