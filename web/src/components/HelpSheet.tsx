import { useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { AnimatePresence } from "motion/react";
import { LifeBuoy } from "./icons";
import { api } from "../api/client";
import type { HelpReason, MediaRequest, MemberTicket, MyTicket, Title, TicketReason } from "../api/types";
import { BottomSheet } from "./BottomSheet";
import { asMemberTicket, inLine, ticketHeading } from "./tickets";
import { formatSlot, seasonsLabel, since } from "./ui";

const REASONS: [HelpReason, string][] = [
  ["stuck", "Stuck downloading"],
  ["notfound", "Can’t be found (no download turns up)"],
  ["quality", "Wrong version or quality"],
  ["episodes", "Wrong or missing episodes"],
  ["playback", "Won’t play on Plex"],
  ["other", "Something else"],
];
/** What can be wrong with a film or show on Plex ("episodes" for a show only), with a line under each. */
const TITLE_REASONS: [TicketReason, string, string?][] = [
  ["playback", "Won’t play on Plex", "It won’t start, stops, or keeps buffering"],
  ["picture", "Picture looks wrong", "Washed out, wrong colours, or blurry"],
  ["audio", "Sound or subtitles", "No sound, out of sync, wrong language, or subtitles"],
  ["wrongfilm", "Wrong film or episode", "It’s a different film, or the wrong episode"],
  ["episodes", "Missing episodes", "Episodes are missing from a season"],
  ["other", "Something else"],
];
/** ...and with nothing in particular. */
const GENERAL_REASONS: [TicketReason, string, string?][] = [
  ["access", "Can’t get in, or nothing plays", "You can’t get into Plex, or nothing will play at all"],
  ["other", "Something else"],
];

/** A film or show a new ticket is about (none: a general ticket). */
export type TicketAbout = { kind: "movie" | "tv"; id: string; title: string };

/** The button that opens a member's ticket: what it's waiting on, or what it's about. */
function TicketOpener({ ticket, onOpen }: { ticket: MemberTicket; onOpen: () => void }) {
  return (
    <button type="button" className={`help-open help-ticket${ticket.waiting ? " is-waiting" : ""}`}
      onClick={(e) => { e.preventDefault(); e.stopPropagation(); onOpen(); }}>
      <LifeBuoy size={15} aria-hidden /> {ticket.waiting ? "An admin asked you something" : `Your ticket: ${inLine(ticket.reason)}`}
    </button>
  );
}

/** A sheet rendered on <body>: inside an animated card, position: fixed is relative to the card, not the screen. */
const onBody = (sheet: ReactNode) => createPortal(<AnimatePresence>{sheet}</AnimatePresence>, document.body);

/** "Something wrong?" on a request: pick what, add a note, and the admins hear about it at once. */
export function HelpButton({ request }: { request: MediaRequest }) {
  const [open, setOpen] = useState(false);
  const [asked, setAsked] = useState<MemberTicket | null>(request.help ?? null);
  if (!request.id) return null;
  if (asked) {
    return (
      <>
        <TicketOpener ticket={asked} onOpen={() => setOpen(true)} />
        {onBody(open ? <TicketSheet key="ticket" ticket={asked} heading={`Your ticket on No. ${formatSlot(request.slot)}`}
          about={request.title.title} onClose={() => setOpen(false)} onAnswered={(t) => setAsked(t)} /> : null)}
      </>
    );
  }
  return (
    <>
      <button type="button" className="help-open" onClick={(e) => { e.preventDefault(); e.stopPropagation(); setOpen(true); }}>
        <LifeBuoy size={15} aria-hidden /> Something wrong? Ask for help
      </button>
      {onBody(open ? <HelpSheet key="sheet" request={request} onClose={() => setOpen(false)}
        onSent={(h) => { setAsked(h); setOpen(false); }} /> : null)}
    </>
  );
}

/** "Something wrong? Open a ticket" on a film or show that's on Plex, or the member's open ticket on it. */
export function TitleTicket({ title }: { title: Title }) {
  const [open, setOpen] = useState(false);
  const [ticket, setTicket] = useState<MemberTicket | null>(title.yourTicket ?? null);
  if (title.kind !== "movie" && title.kind !== "tv") return null;
  const about: TicketAbout = { kind: title.kind, id: title.id, title: title.title };
  return (
    <>
      {ticket ? <TicketOpener ticket={ticket} onOpen={() => setOpen(true)} /> : (
        <button type="button" className="help-open" onClick={() => setOpen(true)}>
          <LifeBuoy size={15} aria-hidden /> Something wrong? Open a ticket
        </button>
      )}
      {onBody(!open ? null : ticket ? (
        <TicketSheet key="ticket" ticket={ticket} heading={ticketHeading(ticket.subject, title.title)}
          onClose={() => setOpen(false)} onAnswered={setTicket} />
      ) : (
        <NewTicketSheet key="new" about={about} onClose={() => setOpen(false)}
          onSent={(t) => { setTicket(asMemberTicket(t)); setOpen(false); }} />
      ))}
    </>
  );
}

function HelpSheet({ request, onClose, onSent }: { request: MediaRequest; onClose: () => void; onSent: (h: { id: string; reason: string }) => void }) {
  const [reason, setReason] = useState<HelpReason | null>(null);
  const [note, setNote] = useState("");
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState("");

  const send = async () => {
    if (!reason || !request.id) return;
    if (reason === "other" && !note.trim()) { setProblem("Say a little about what’s wrong."); return; }
    setSending(true);
    setProblem("");
    try {
      const out = await api.askHelp(request.id, reason, note.trim());
      onSent(out.help);
    } catch (e) {
      setProblem(e instanceof Error ? e.message : "That didn’t send. Try again in a minute.");
    } finally {
      setSending(false);
    }
  };

  const seasons = seasonsLabel(request.seasons) ? ` (${seasonsLabel(request.seasons)})` : "";
  return (
    <BottomSheet titleId="help-h" title={`What’s wrong with No. ${formatSlot(request.slot)}?`} onClose={onClose} className="help-sheet">
      <p className="muted">{request.title.title}{seasons}. The admins get this straight away, with what Plexbie can see right now.</p>
      <div className="choices" role="group" aria-label="What's wrong">
        {REASONS.map(([id, label]) => (
          <button key={id} type="button" className="choice" aria-pressed={reason === id} onClick={() => setReason(id)}>{label}</button>
        ))}
      </div>
      <div className="field">
        <label htmlFor="help-note">Anything else? <span className="muted">{reason === "other" ? "(needed)" : "(optional)"}</span></label>
        <textarea id="help-note" className="m-textarea" rows={3} maxLength={600} value={note} onChange={(e) => setNote(e.target.value)}
          onFocus={(e) => e.currentTarget.scrollIntoView({ block: "nearest" })}
          placeholder="It’s been at 0% since this morning" />
      </div>
      {problem ? <p className="field__error" role="alert">{problem}</p> : null}
      <div className="m-sheet__foot">
        <button type="button" className="btn btn--primary btn--block" disabled={!reason || sending} onClick={send}>
          {sending ? "Sending…" : reason ? "Send to the admins" : "Pick what’s wrong"}
        </button>
      </div>
    </BottomSheet>
  );
}

/** A new ticket that isn't on a request: about a film or show on Plex, or (`about` null) general. */
export function NewTicketSheet({ about, onClose, onSent }: { about: TicketAbout | null; onClose: () => void; onSent: (t: MyTicket) => void }) {
  const [reason, setReason] = useState<TicketReason | null>(null);
  const [note, setNote] = useState("");
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState("");
  const reasons = about ? TITLE_REASONS.filter(([id]) => id !== "episodes" || about.kind === "tv") : GENERAL_REASONS;

  const send = async () => {
    if (!reason) return;
    if (reason === "other" && !note.trim()) { setProblem("Say a little about what’s wrong."); return; }
    setSending(true);
    setProblem("");
    try {
      const out = await api.openTicket(about ? { kind: about.kind, id: about.id, reason, note: note.trim() } : { reason, note: note.trim() });
      onSent(out.ticket);
    } catch (e) {
      setProblem(e instanceof Error ? e.message : "That didn’t send. Try again in a minute.");
    } finally {
      setSending(false);
    }
  };

  return (
    <BottomSheet titleId="ticket-new-h" title={about ? `What’s wrong with ${about.title}?` : "What do you need help with?"} onClose={onClose} className="help-sheet">
      <p className="muted">
        {about ? "The admins get this straight away, with what Plexbie can see of the file and of how you last played it."
          : "For anything that isn’t about one film or show: Plex itself, your account, or something you can’t find. The admins get it straight away."}
      </p>
      <div className="choices choices--stacked" role="group" aria-label="What's wrong">
        {reasons.map(([id, label, hint]) => (
          <button key={id} type="button" className="choice" aria-pressed={reason === id} onClick={() => setReason(id)}>
            {label}{hint ? <small>{hint}</small> : null}
          </button>
        ))}
      </div>
      <div className="field">
        <label htmlFor="ticket-note">Anything else? <span className="muted">{reason === "other" ? "(needed)" : "(optional)"}</span></label>
        <textarea id="ticket-note" className="m-textarea" rows={3} maxLength={600} value={note} onChange={(e) => setNote(e.target.value)}
          onFocus={(e) => e.currentTarget.scrollIntoView({ block: "nearest" })}
          placeholder={about ? "It looks grey and washed out on my TV" : "Plex says I don’t have access any more"} />
      </div>
      {problem ? <p className="field__error" role="alert">{problem}</p> : null}
      <div className="m-sheet__foot">
        <button type="button" className="btn btn--primary btn--block" disabled={!reason || sending} onClick={send}>
          {sending ? "Sending…" : reason ? "Send to the admins" : "Pick what’s wrong"}
        </button>
      </div>
    </BottomSheet>
  );
}

/** The member's own ticket: what they said, what the admins said to them, and a box to add
 *  to it while it's open (their answer, when an admin asked something). Admins' notes to each
 *  other aren't here. */
export function TicketSheet({ ticket, heading, about, onClose, onAnswered }: {
  ticket: MemberTicket; heading: string; about?: string | null; onClose: () => void; onAnswered: (t: MemberTicket) => void;
}) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState("");
  const thread = ticket.thread ?? [];
  const open = ticket.status !== "resolved";
  const send = async () => {
    if (!text.trim()) return;
    setSending(true);
    setProblem("");
    try {
      await api.replyTicket(ticket.id, text.trim());
      onAnswered({ ...ticket, waiting: false, thread: [...thread, { id: `me${Date.now()}`, at: new Date().toISOString(), by: "You", kind: "member", text: text.trim() }] });
      setText("");
    } catch (e) {
      setProblem(e instanceof Error ? e.message : "That didn’t send. Try again in a minute.");
    } finally {
      setSending(false);
    }
  };
  return (
    <BottomSheet titleId="ticket-h" title={heading} onClose={onClose} className="help-sheet">
      <p className="muted">{about ? `${about} · ` : ""}{ticket.reason}</p>
      <ol className="m-thread" aria-label="Your ticket">
        {thread.map((e) => (
          <li key={e.id} className={`m-thread__entry is-${e.kind === "member" ? "member" : e.kind}`}>
            {e.kind === "status" ? <span className="m-thread__status">{e.text} · {since(e.at)}</span> : (
              <>
                <span className="m-thread__head"><b>{e.by}</b><span className="muted">{since(e.at)}</span></span>
                <span className="m-thread__text">{e.text}</span>
              </>
            )}
          </li>
        ))}
      </ol>
      {open ? (
        <div className="field">
          {ticket.waiting ? null : <p className="muted">The admins have it. You’ll hear back here, and by Discord or an alert.</p>}
          <label htmlFor="ticket-answer">{ticket.waiting ? "Your answer" : "Add to your ticket"}</label>
          <textarea id="ticket-answer" className="m-textarea m-textarea--reply" rows={3} maxLength={1200} value={text}
            onChange={(e) => setText(e.target.value)} onFocus={(e) => e.currentTarget.scrollIntoView({ block: "nearest" })} />
          {problem ? <p className="field__error" role="alert">{problem}</p> : null}
          <button type="button" className="btn btn--primary" disabled={sending || !text.trim()} onClick={() => void send()}>
            {sending ? "Sending…" : "Send to the admins"}
          </button>
        </div>
      ) : <p className="muted">Solved. If it’s still wrong, open a new ticket.</p>}
    </BottomSheet>
  );
}
