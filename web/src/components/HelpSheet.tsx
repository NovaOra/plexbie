import { useState } from "react";
import { createPortal } from "react-dom";
import { AnimatePresence } from "motion/react";
import { LifeBuoy } from "./icons";
import { api } from "../api/client";
import type { HelpReason, MediaRequest, MemberTicket } from "../api/types";
import { BottomSheet } from "./BottomSheet";
import { formatSlot, seasonsLabel, since } from "./ui";

const REASONS: [HelpReason, string][] = [
  ["stuck", "Stuck downloading"],
  ["notfound", "Can’t be found (no download turns up)"],
  ["quality", "Wrong version or quality"],
  ["episodes", "Wrong or missing episodes"],
  ["playback", "Won’t play on Plex"],
  ["other", "Something else"],
];

/** "Something wrong?" on a request: pick what, add a note, and the admins hear about it at once. */
export function HelpButton({ request }: { request: MediaRequest }) {
  const [open, setOpen] = useState(false);
  const [asked, setAsked] = useState<MemberTicket | null>(request.help ?? null);
  if (!request.id) return null;
  if (asked) {
    return (
      <>
        <button type="button" className={`help-open help-ticket${asked.waiting ? " is-waiting" : ""}`}
          onClick={(e) => { e.preventDefault(); e.stopPropagation(); setOpen(true); }}>
          <LifeBuoy size={15} aria-hidden /> {asked.waiting ? "An admin asked you something" : `Your ticket: ${asked.reason.toLowerCase()}`}
        </button>
        {createPortal(
          <AnimatePresence>
            {open ? <TicketSheet key="ticket" request={request} ticket={asked} onClose={() => setOpen(false)}
              onAnswered={(t) => setAsked(t)} /> : null}
          </AnimatePresence>,
          document.body,
        )}
      </>
    );
  }
  return (
    <>
      <button type="button" className="help-open" onClick={(e) => { e.preventDefault(); e.stopPropagation(); setOpen(true); }}>
        <LifeBuoy size={15} aria-hidden /> Something wrong? Ask for help
      </button>
      {/* Rendered on <body>: inside an animated card, position: fixed is relative to the card, not the screen. */}
      {createPortal(
        <AnimatePresence>
          {open ? (
            <HelpSheet key="sheet" request={request} onClose={() => setOpen(false)}
              onSent={(h) => { setAsked(h); setOpen(false); }} />
          ) : null}
        </AnimatePresence>,
        document.body,
      )}
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

/** The member's own ticket: what they said, what the admins said to them, and (when an
 *  admin asked something) a box to answer. Admins' notes to each other aren't here. */
function TicketSheet({ request, ticket, onClose, onAnswered }: {
  request: MediaRequest; ticket: MemberTicket; onClose: () => void; onAnswered: (t: MemberTicket) => void;
}) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState("");
  const thread = ticket.thread ?? [];
  const send = async () => {
    if (!request.id || !text.trim()) return;
    setSending(true);
    setProblem("");
    try {
      await api.answerTicket(request.id, text.trim());
      onAnswered({ ...ticket, waiting: false, thread: [...thread, { id: `me${Date.now()}`, at: new Date().toISOString(), by: "You", kind: "member", text: text.trim() }] });
      setText("");
    } catch (e) {
      setProblem(e instanceof Error ? e.message : "That didn’t send. Try again in a minute.");
    } finally {
      setSending(false);
    }
  };
  return (
    <BottomSheet titleId="ticket-h" title={`Your ticket on No. ${formatSlot(request.slot)}`} onClose={onClose} className="help-sheet">
      <p className="muted">{request.title.title} · {ticket.reason}</p>
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
      {ticket.waiting ? (
        <div className="field">
          <label htmlFor="ticket-answer">Your answer</label>
          <textarea id="ticket-answer" className="m-textarea m-textarea--reply" rows={3} maxLength={1200} value={text}
            onChange={(e) => setText(e.target.value)} onFocus={(e) => e.currentTarget.scrollIntoView({ block: "nearest" })} />
          {problem ? <p className="field__error" role="alert">{problem}</p> : null}
          <button type="button" className="btn btn--primary" disabled={sending || !text.trim()} onClick={() => void send()}>
            {sending ? "Sending…" : "Send to the admins"}
          </button>
        </div>
      ) : <p className="muted">The admins have it. You’ll hear back here, and by Discord or an alert.</p>}
    </BottomSheet>
  );
}
