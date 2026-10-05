import {
  createContext, useCallback, useContext, useEffect, useId, useRef, useState,
  type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type ReactNode,
} from "react";
import {
  AnimatePresence, LayoutGroup, animate, motion, useMotionValue, useMotionValueEvent, useReducedMotion, useTransform,
} from "motion/react";
import {
  ArrowLeft, Bell, Check, CircleDot, LifeBuoy, CircleAlert, CircleCheck, Copy, Hourglass, Inbox, Link2, ListOrdered, Lock, Mail, MessageCircle, MessageSquare, Radio, Plus, RefreshCw, Search, Share2, ShieldCheck, Ticket, Trash2, UserPlus, X,
} from "../components/icons";
import type { Icon } from "../components/icons";
import { createPortal } from "react-dom";
import { useSearchParams } from "react-router-dom";
import { api, type Ack } from "../api/client";
import type { AdminAllRequests, AdminRequestDetail, AdminRequestRow, AdminTicket, AdminTicketDetail, AdminTicketRow, AdminTickets, TicketEntry, AdminCleanup, AdminCleanupRow, AdminHelp, AdminInvite, PlexInvite, CleanupSettings, DiscordOverview, LoggedMessage, MessageChannel, MessagePerson, AdminJoin, AdminPerson, AdminRequest, AdminRequests, HealthCheck, NewInvite } from "../api/types";
import { useSession } from "../components/Layout";
import { Mascot } from "../components/Mascot";
import { Journey, LiveProgress } from "../components/motion";
import { Art, KIND_LABEL, formatSlot, stageLabel, inDays, scrollBehavior, seasonsLabel, shortDate, since, useCopied, useLoad, useTitle, PageHead } from "../components/ui";
import { useBackToClose } from "../components/overlay";
import { BottomSheet } from "../components/BottomSheet";
import { EASE_OUT } from "../components/motion";

/* ================================================================== store */

type Section = "requests" | "all" | "tickets" | "joins" | "invites" | "plexinvites" | "people" | "cleanup" | "discord" | "messages" | "health" | "help";
type Store = {
  requests?: AdminRequests; all?: AdminAllRequests; tickets?: AdminTickets; joins?: AdminJoin[]; invites?: AdminInvite[]; plexinvites?: PlexInvite[]; people?: AdminPerson[]; cleanup?: AdminCleanup;
  discord?: DiscordOverview; messages?: MessagePerson[]; health?: HealthCheck[]; help?: AdminHelp[];
};
type ToastIn = { tone?: "ok" | "error"; text: string; detail?: string; undo?: () => void };
type Toast = ToastIn & { id: number };

const SECTIONS: Section[] = ["requests", "all", "tickets", "joins", "invites", "plexinvites", "people", "cleanup", "discord", "messages", "health", "help"];
const TABS: { id: Section; label: string }[] = [
  { id: "requests", label: "Requests" },
  { id: "all", label: "All requests" },
  { id: "tickets", label: "Tickets" },
  { id: "joins", label: "Join requests" },
  { id: "invites", label: "Invites" },
  { id: "people", label: "People" },
  { id: "cleanup", label: "Cleanup" },
  { id: "discord", label: "Discord" },
  { id: "messages", label: "Messages" },
  { id: "health", label: "Health" },
];

interface ManageState {
  data: Store;
  refresh: (s: Section) => Promise<void>;
  patch: <K extends Section>(s: K, fn: (d: NonNullable<Store[K]>) => NonNullable<Store[K]>) => void;
  toast: (t: ToastIn) => void;
  readOnly: boolean;
  failed: Partial<Record<Section, boolean>>;
}
const ManageCtx = createContext<ManageState | null>(null);
const useManage = () => useContext(ManageCtx)!;

/** A tiny tap on phones that support it; silence everywhere else. */
function buzz(ms = 8) {
  try { navigator.vibrate?.(ms); } catch { /* not supported */ }
}

/** Runs an admin action and turns any failure into a Ack, so callers only branch once. */
async function attempt(act: () => Promise<Ack>): Promise<Ack> {
  try {
    return await act();
  } catch (e) {
    return { ok: false, message: e instanceof Error ? e.message : "That didn’t work." };
  }
}

/**
 * Admin actions, the same way everywhere: mark a row busy while it runs, and on
 * failure show the error (with `failText` as the headline when given) and
 * return null, so a caller only handles success. `later` refreshes a section
 * after the bot has had a moment to act.
 */
function useAct() {
  const { toast, refresh } = useManage();
  const [busy, setBusy] = useState<string | null>(null);
  const act = async (key: string | null, call: () => Promise<Ack>, opts: { failText?: string; buzzOnFail?: boolean } = {}) => {
    if (key !== null) setBusy(key);
    const out = await attempt(call);
    if (key !== null) setBusy(null);
    if (out.ok) return out;
    if (opts.buzzOnFail) buzz(30);
    toast(opts.failText ? { tone: "error", text: opts.failText, detail: out.message } : { tone: "error", text: out.message });
    return null;
  };
  const later = (section: Section, ms: number) => window.setTimeout(() => void refresh(section), ms);
  return { busy, act, later };
}

/* ============================================================ primitives */

/**
 * A destructive action you have to mean: press and hold until the fill reaches
 * the end. Letting go early cancels. Keyboard users hold Space or Enter;
 * assistive tech that "clicks" without a press gets a two-step confirm instead.
 */
function HoldButton({ label, icon, onConfirm, disabled, ms = 1000 }: {
  label: string; icon?: ReactNode; onConfirm: () => void; disabled?: boolean; ms?: number;
}) {
  const [holding, setHolding] = useState(false);
  const [armed, setArmed] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  const pressed = useRef(false);
  const self = useRef<HTMLButtonElement>(null);

  // Confirming usually removes this card, and focus would drop to the page:
  // keep it nearby instead, on the next card or the section's heading.
  const confirm = () => {
    const card = self.current?.closest("li, article");
    const next = card?.nextElementSibling?.querySelector<HTMLElement>("button, a")
      ?? card?.previousElementSibling?.querySelector<HTMLElement>("button, a");
    const heading = self.current?.closest("section")?.querySelector<HTMLElement>("h2, h3");
    onConfirm();
    window.setTimeout(() => {
      const at = document.activeElement;
      if (at && at !== document.body && at.isConnected) return;
      const target = next?.isConnected ? next : heading?.isConnected ? heading : null;
      if (!target) return;
      if (target.matches("h2, h3") && !target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
      target.focus();
    }, 400);
  };

  const start = () => {
    if (disabled || pressed.current) return;
    pressed.current = true;
    setHolding(true);
    buzz(4);
    timer.current = window.setTimeout(() => {
      pressed.current = false;
      setHolding(false);
      buzz(16);
      confirm();
    }, ms);
  };
  const cancel = () => {
    window.clearTimeout(timer.current);
    pressed.current = false;
    setHolding(false);
  };
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const isKey = (e: ReactKeyboardEvent) => e.key === " " || e.key === "Enter";
  const text = armed ? "Again to confirm" : `Hold to ${label.toLowerCase()}`;
  const hint = useId();

  return (
    <button
      ref={self}
      type="button"
      className={`btn m-btn m-hold${holding ? " is-holding" : ""}${armed ? " is-armed" : ""}`}
      style={{ "--hold": `${ms}ms` } as CSSProperties}
      disabled={disabled}
      // The name starts with the words on the button, so "click Hold to remove" works
      // for voice control; how it works is the description.
      aria-label={armed ? `Again to confirm: ${label}` : text}
      aria-describedby={hint}
      onPointerDown={(e) => {
        if (e.button !== 0) return;
        e.stopPropagation();
        e.currentTarget.setPointerCapture(e.pointerId);
        start();
      }}
      onPointerUp={cancel}
      onPointerCancel={cancel}
      onKeyDown={(e) => { if (isKey(e)) { e.preventDefault(); if (!e.repeat) start(); } }}
      onKeyUp={(e) => { if (isKey(e)) { e.preventDefault(); cancel(); } }}
      onClick={(e) => {
        if (e.detail !== 0 || pressed.current) return;
        if (armed) { setArmed(false); confirm(); } else setArmed(true);
      }}
      onBlur={() => { cancel(); setArmed(false); }}
      onContextMenu={(e) => e.preventDefault()}
    >
      <span className="m-hold__label">{icon}{text}</span>
      <span id={hint} hidden>Press and hold, or press twice, to confirm.</span>
      <span className="m-hold__fill" aria-hidden>
        <span className="m-hold__label">{icon}{holding ? "Keep holding…" : text}</span>
      </span>
    </button>
  );
}

/** An on/off switch whose knob stretches while pressed and springs across. */
function Switch({ on, label, onChange, disabled }: { on: boolean; label: string; onChange: (on: boolean) => void; disabled?: boolean }) {
  return (
    <button
      type="button" role="switch" aria-checked={on} aria-label={label} disabled={disabled}
      className={`m-switch${on ? " is-on" : ""}`}
      onClick={() => { buzz(6); onChange(!on); }}
    >
      <motion.span className="m-switch__knob" layout transition={{ type: "spring", duration: 0.32, bounce: 0.25 }}>
        {on ? <ShieldCheck size={12} aria-hidden /> : null}
      </motion.span>
    </button>
  );
}

/** A number that ticks into place when it changes, so a decision visibly lands. */
function Tick({ value }: { value: number | string }) {
  return (
    <span className="m-tick">
      <AnimatePresence mode="popLayout" initial={false}>
        <motion.span
          key={String(value)}
          initial={{ opacity: 0, transform: "translateY(-60%)" }}
          animate={{ opacity: 1, transform: "translateY(0%)" }}
          exit={{ opacity: 0, transform: "translateY(60%)" }}
          transition={{ duration: 0.22, ease: EASE_OUT }}
        >
          {value}
        </motion.span>
      </AnimatePresence>
    </span>
  );
}

function Initial({ name, big = false }: { name: string; big?: boolean }) {
  return <span className={`m-avatar${big ? " m-avatar--big" : ""}`} aria-hidden>{name.trim().charAt(0).toUpperCase() || "?"}</span>;
}

function AllClear({ title, children }: { title: string; children: ReactNode }) {
  return (
    <motion.div
      className="m-clear" role="status"
      initial={{ opacity: 0, transform: "translateY(6px)" }} animate={{ opacity: 1, transform: "translateY(0px)" }}
      transition={{ duration: 0.3, ease: EASE_OUT }}
    >
      <Mascot />
      <div><b>{title}</b><p className="muted">{children}</p></div>
    </motion.div>
  );
}

/* ======================================================== swipe-to-decide */

const SWIPE_MAX = 260;
/** Rubber band: easy at first, heavier the further you pull. */
const damp = (o: number) => Math.sign(o) * SWIPE_MAX * (1 - Math.exp(-Math.abs(o) / SWIPE_MAX));

/**
 * A card you can swipe: right says yes, left says no. Past the line it buzzes
 * and the label pops; let go and it flies off while the action runs, or springs
 * home if you change your mind (or the action fails).
 */
function SwipeCard({ yes, no, yesLabel, noLabel, enabled, children }: {
  yes: () => Promise<boolean>; no: () => Promise<boolean>; yesLabel: string; noLabel: string; enabled: boolean; children: ReactNode;
}) {
  const reduced = useReducedMotion();
  const ref = useRef<HTMLDivElement>(null);
  const x = useMotionValue(0);
  const yesOpacity = useTransform(x, [0, 24, 90], [0, 0.5, 1]);
  const noOpacity = useTransform(x, [-90, -24, 0], [1, 0.5, 0]);
  const [past, setPast] = useState<-1 | 0 | 1>(0);
  const pastRef = useRef<-1 | 0 | 1>(0);
  const line = () => Math.min(110, (ref.current?.offsetWidth ?? 360) * 0.3);

  useMotionValueEvent(x, "change", (v) => {
    const p = v > line() ? 1 : v < -line() ? -1 : 0;
    if (p !== pastRef.current) {
      pastRef.current = p;
      setPast(p);
      if (p) buzz(7);
    }
  });

  const home = () => animate(x, 0, { type: "spring", duration: 0.45, bounce: 0.3 });

  const commit = async (dir: 1 | -1) => {
    const width = ref.current?.offsetWidth ?? 400;
    if (!reduced) animate(x, dir * (width + 40), { duration: 0.22, ease: EASE_OUT });
    const ok = await (dir === 1 ? yes() : no());
    if (!ok) home();
  };

  return (
    <div className="m-swipe" ref={ref}>
      {enabled ? (
        <div className="m-swipe__under" aria-hidden>
          <motion.span className={`m-swipe__yes${past === 1 ? " is-past" : ""}`} style={{ opacity: yesOpacity }}>
            <Check size={20} /> {yesLabel}
          </motion.span>
          <motion.span className={`m-swipe__no${past === -1 ? " is-past" : ""}`} style={{ opacity: noOpacity }}>
            {noLabel} <X size={20} />
          </motion.span>
        </div>
      ) : null}
      <motion.div
        className={`m-swipe__card${enabled ? " is-live" : ""}`}
        style={{ x }}
        onPan={enabled ? (_, info) => x.set(damp(info.offset.x)) : undefined}
        onPanEnd={enabled ? (_, info) => {
          const v = x.get();
          const flick = Math.abs(info.velocity.x) > 700 && Math.abs(v) > 40 && Math.sign(info.velocity.x) === Math.sign(v);
          if (Math.abs(v) > line() || flick) void commit(v > 0 ? 1 : -1);
          else home();
        } : undefined}
      >
        {children}
      </motion.div>
    </div>
  );
}

/** List items leave by folding away, so the next card slides up into place. */
const fold = {
  initial: { opacity: 0, height: 0 },
  animate: { opacity: 1, height: "auto", transition: { duration: 0.25, ease: EASE_OUT } },
  exit: { opacity: 0, height: 0, transition: { duration: 0.22, ease: EASE_OUT } },
} as const;

/* ================================================================= tabs */

/** A row of pills, one chosen; the highlight slides to the new one. */
function Segmented<T extends string | number>({ id, label, labelledBy, options, value, onChange }: {
  id: string; label?: string; labelledBy?: string; options: { id: T; label: ReactNode }[]; value: T; onChange: (v: T) => void;
}) {
  return (
    <LayoutGroup id={id}>
      <div className="m-seg" role="group" aria-label={label} aria-labelledby={labelledBy}>
        {options.map((o) => (
          <button key={o.id} type="button" aria-pressed={value === o.id} className="m-seg__item" onClick={() => onChange(o.id)}>
            {value === o.id ? <motion.span layoutId={`${id}-pill`} className="m-seg__pill" transition={{ type: "spring", duration: 0.35, bounce: 0.15 }} /> : null}
            {o.label}
          </button>
        ))}
      </div>
    </LayoutGroup>
  );
}

/** The filter box on the People, Discord and Messages lists. */
function SearchField({ label = "Find someone", value, onChange, className, autofocus = false }: {
  label?: string; value: string; onChange: (v: string) => void; className?: string; autofocus?: boolean;
}) {
  return (
    <label className={className ? `m-search ${className}` : "m-search"}>
      <Search size={16} aria-hidden />
      <span className="visually-hidden">{label}</span>
      <input type="search" placeholder={label} value={value} onChange={(e) => onChange(e.target.value)}
        data-autofocus={autofocus ? "" : undefined} />
    </label>
  );
}

function SwipeHint() {
  return <p className="m-hint muted"><span aria-hidden>←</span> Swipe a card: right for yes, left for no <span aria-hidden>→</span></p>;
}

function RequestsTab() {
  const { data, patch, toast, readOnly, failed } = useManage();
  const { busy, act, later } = useAct();
  const res = data.requests;
  // Waits for the help requests too: they go above the queue, and arriving later
  // they'd push it down under the reader's thumb.
  if (!res || (data.help === undefined && !failed.help)) {
    return (
      <div className="m-stack" aria-busy="true" aria-label="Loading requests">
        {[0, 1].map((i) => <div key={i} className="skeleton" style={{ height: 150, borderRadius: 16 }} />)}
      </div>
    );
  }
  const { pending, older, recent } = res;

  const decide = async (r: AdminRequest, approve: boolean) => {
    const out = await act(r.id, () => api.decide("requests", r.id, approve), { buzzOnFail: true });
    if (!out) return false;
    buzz(12);
    toast({ text: approve ? `Approved ${r.title}` : `Declined ${r.title}`, detail: out.message });
    patch("requests", (d) => ({
      ...d,
      pending: d.pending.filter((p) => p.id !== r.id),
      recent: [{ ...r, status: approve ? "approved" : "declined", resolvedBy: "you", resolvedAt: new Date().toISOString() }, ...d.recent],
    }));
    later("requests", 2500);
    return true;
  };

  return (
    <div className="m-stack">
      <section className="section">
        <h2>Waiting for a decision <span className="m-count"><Tick value={pending.length} /></span></h2>
        {pending.length && !readOnly ? <SwipeHint /> : null}
        <ul className="m-cards">
          <AnimatePresence initial={false}>
            {pending.map((r) => (
              <motion.li key={r.id} {...fold}>
                <SwipeCard
                  enabled={!readOnly && busy !== r.id}
                  yes={() => decide(r, true)} no={() => decide(r, false)} yesLabel="Approve" noLabel="Decline"
                >
                  <article className={`m-card${busy === r.id ? " is-busy" : ""}`} aria-busy={busy === r.id}>
                    <span className="m-card__poster"><Art title={{ poster: r.poster, title: r.title, kind: r.kind as never }} size="w185" /></span>
                    <div className="m-card__body">
                      <span className="m-card__eyebrow">No. {formatSlot(r.slot)} · {KIND_LABEL[r.kind as keyof typeof KIND_LABEL] ?? r.kind}</span>
                      <h3 className="m-card__title">{r.title}</h3>
                      {seasonsLabel(r.seasons) ? <span className="m-chip">{seasonsLabel(r.seasons)}</span> : null}
                      <span className="m-card__who"><Initial name={r.requester} /><span>{r.requester} asked {since(r.requestedAt)}</span></span>
                    </div>
                    <div className="m-card__actions">
                      <button type="button" className="btn m-btn m-btn--go" disabled={readOnly || busy === r.id} onClick={() => void decide(r, true)}>
                        <Check size={18} aria-hidden /> {busy === r.id ? "Working…" : "Approve"}
                      </button>
                      <HoldButton label="Decline" icon={<X size={16} aria-hidden />} disabled={readOnly || busy === r.id} onConfirm={() => void decide(r, false)} />
                    </div>
                  </article>
                </SwipeCard>
              </motion.li>
            ))}
          </AnimatePresence>
        </ul>
        {!pending.length ? <AllClear title="All caught up">Every request has an answer. New ones show up here and in Discord.</AllClear> : null}
      </section>

      {recent.length ? (
        <section className="section">
          <h2>Recently decided</h2>
          <ul className="m-log">
            <AnimatePresence initial={false}>
              {recent.slice(0, 12).map((r) => (
                <motion.li key={r.id} {...fold}>
                  <div className="m-log__row">
                    <span className={`m-pill m-pill--${r.status}`}>{r.status === "approved" ? "Approved" : "Declined"}</span>
                    <span className="m-log__main">{r.title}</span>
                    <span className="muted m-log__meta">{r.requester} · {r.resolvedBy ? `by ${r.resolvedBy} ` : ""}{r.resolvedAt ? since(r.resolvedAt) : ""}</span>
                  </div>
                </motion.li>
              ))}
            </AnimatePresence>
          </ul>
        </section>
      ) : null}

      {older.length ? (
        <details className="m-older">
          <summary>{older.length} older requests from before outcomes were recorded (30 September)</summary>
          <p className="muted">
            These can’t tell “approved months ago” from “never answered”. Open one in Discord: if it still has
            Approve and Decline buttons, it’s genuinely waiting.
          </p>
          <ul className="m-log">
            {older.map((r) => (
              <li key={r.id} className="m-log__row">
                <span className="muted">No. {formatSlot(r.slot)}</span>
                <span className="m-log__main">{r.title}</span>
                <span className="muted m-log__meta">
                  {r.requester}, {since(r.requestedAt)}
                  {r.discordUrl ? <> · <a href={r.discordUrl} rel="noopener">Open in Discord</a></> : null}
                </span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}

function JoinsTab() {
  const { data, patch, toast, readOnly } = useManage();
  const { busy, act, later } = useAct();
  const rows = data.joins;
  if (!rows) return <div className="skeleton" style={{ height: 240 }} />;
  const pending = rows.filter((j) => j.status === "pending");
  const earlier = rows.filter((j) => j.status !== "pending");

  const decide = async (j: AdminJoin, approve: boolean) => {
    const out = await act(j.key, () => api.decide("joins", j.messageId, approve), { buzzOnFail: true });
    if (!out) return false;
    buzz(12);
    toast({ text: approve ? `Invited ${j.name}` : `Denied ${j.name}`, detail: out.message });
    patch("joins", (d) => d.map((x) => (x.key === j.key ? { ...x, status: approve ? "approved" : "denied" } : x)));
    later("joins", 2500);
    return true;
  };

  return (
    <div className="m-stack">
      <section className="section">
        <h2>Waiting to join <span className="m-count"><Tick value={pending.length} /></span></h2>
        {pending.some((j) => j.messageId) && !readOnly ? <SwipeHint /> : null}
        <ul className="m-cards">
          <AnimatePresence initial={false}>
            {pending.map((j) => {
              const can = !readOnly && !!j.messageId && busy !== j.key;
              return (
                <motion.li key={j.key} {...fold}>
                  <SwipeCard enabled={can} yes={() => decide(j, true)} no={() => decide(j, false)} yesLabel="Invite" noLabel="Deny">
                    <article className={`m-card m-card--person${busy === j.key ? " is-busy" : ""}`} aria-busy={busy === j.key}>
                      <Initial name={j.name} big />
                      <div className="m-card__body">
                        <span className="m-card__eyebrow">{j.via === "plex" ? "Signed in with Plex" : "From Discord"} · asked {since(j.askedAt)}</span>
                        <h3 className="m-card__title">{j.name}</h3>
                        <span className="m-card__who">{j.email || "Email on their Plex account"}</span>
                      </div>
                      <div className="m-card__actions">
                        {j.messageId ? (
                          <>
                            <button type="button" className="btn m-btn m-btn--go" disabled={!can} onClick={() => void decide(j, true)}>
                              <UserPlus size={18} aria-hidden /> {busy === j.key ? "Inviting…" : "Invite to Plex"}
                            </button>
                            <HoldButton label="Deny" icon={<X size={16} aria-hidden />} disabled={!can} onConfirm={() => void decide(j, false)} />
                          </>
                        ) : <span className="muted m-card__note">Answer this one in Discord.</span>}
                      </div>
                    </article>
                  </SwipeCard>
                </motion.li>
              );
            })}
          </AnimatePresence>
        </ul>
        {!pending.length ? <AllClear title="Nobody waiting">When someone asks to join, from Discord or by signing in with Plex, they land here.</AllClear> : null}
      </section>
      {earlier.length ? (
        <section className="section">
          <h2>Earlier</h2>
          <ul className="m-log">
            {earlier.map((j) => (
              <li className="m-log__row" key={j.key}>
                <span className={`m-pill m-pill--${j.status === "denied" ? "declined" : "approved"}`}>{j.status}</span>
                <span className="m-log__main">{j.name}</span>
                <span className="muted m-log__meta">{j.email} · {since(j.askedAt)}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

function PeopleTab() {
  const { data, patch, toast, readOnly } = useManage();
  const { busy, act, later } = useAct();
  const [query, setQuery] = useState("");
  const [linking, setLinking] = useState<AdminPerson | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const rows = data.people;
  if (!rows) return <div className="skeleton" style={{ height: 300 }} />;
  const q = query.trim().toLowerCase();
  const shown = q ? rows.filter((p) => `${p.plexName} ${p.discordName ?? ""}`.toLowerCase().includes(q)) : rows;

  const link = async (p: AdminPerson, member: { id: string; name: string }, quiet = false) => {
    const out = await act(null, () => api.linkPerson(p.plexName, member.id));
    if (!out) return false;
    if (!quiet) toast({ text: `Linked ${p.plexName}`, detail: p.tracked === false ? `${out.message} They’re tracked for inactivity from now on.` : out.message });
    patch("people", (d) => d.map((x) => (x.plexName === p.plexName ? { ...x, linked: true, discordName: member.name, discordId: member.id } : x)));
    later("people", 1200);
    return true;
  };

  const unlink = async (p: AdminPerson) => {
    const out = await act(null, () => api.unlinkPerson(p.plexName));
    if (!out) return;
    const was = p.discordId ? { id: p.discordId, name: p.discordName ?? "" } : null;
    toast({ text: `Unlinked ${p.plexName}`, detail: out.message, undo: was ? () => void link(p, was, true) : undefined });
    patch("people", (d) => d.map((x) => (x.plexName === p.plexName ? { ...x, linked: false, discordName: null, discordId: null } : x)));
  };

  const rename = async (p: AdminPerson) => {
    const name = newName.trim();
    const out = await act(p.plexName, () => api.renamePerson(p.plexName, name));
    if (!out) return;
    buzz(12);
    toast({ text: out.message });
    patch("people", (d) => d.map((x) => (x.plexName === p.plexName ? { ...x, displayName: name === p.plexName ? null : name } : x)));
    setRenaming(null);
    later("people", 1200);
  };

  const keep = async (p: AdminPerson, on: boolean) => {
    patch("people", (d) => d.map((x) => (x.plexName === p.plexName ? { ...x, neverRemove: on, warned: on ? false : x.warned } : x)));
    const out = await act(null, () => api.keepPerson(p.plexName, on));
    if (!out) {
      patch("people", (d) => d.map((x) => (x.plexName === p.plexName ? { ...x, neverRemove: !on } : x)));
      return;
    }
    toast({ text: out.message, undo: () => void keep(p, !on) });
    later("people", 1200);
  };

  const match = async (p: AdminPerson, account: string) => {
    const out = await act(p.plexName, () => api.matchPerson(p.plexName, account));
    if (!out) return;
    buzz(12);
    toast({ text: `${p.plexName} → ${account}`, detail: out.message });
    patch("people", (d) => d.map((x) => (x.plexName === p.plexName ? { ...x, plexName: account, hasAccess: true, candidates: undefined } : x)));
    later("people", 1200);
  };

  const remove = async (p: AdminPerson) => {
    const out = await act(p.plexName, () => api.removePerson(p.plexName), { buzzOnFail: true });
    if (!out) return;
    toast({ text: p.hasAccess === false ? `Forgot ${p.plexName}` : `Removed ${p.plexName}`, detail: out.message });
    patch("people", (d) => d.filter((x) => x.plexName !== p.plexName));
  };

  return (
    <section className="section">
      <div className="m-head">
        <h2>Who’s on Plex <span className="m-count"><Tick value={rows.length} /></span></h2>
        {rows.length ? (
          <SearchField value={query} onChange={setQuery} />
        ) : null}
      </div>
      <p className="muted">Everyone your server is shared with. Closest to removal first; top-three watchers are always safe, and watching anything resets the clock.</p>
      {!rows.length ? (
        <AllClear title="Nobody else yet">When you share the server with someone (an invite link, or in Plex), they show up here.</AllClear>
      ) : null}
      <ul className="m-rows">
        <AnimatePresence initial={false}>
          {shown.map((p) => {
            const total = p.removalIn === null ? null : p.daysIdle + p.removalIn;
            const fill = total ? Math.min(1, p.daysIdle / total) : 0;
            const tone = p.removalIn === null ? "safe" : p.removalIn <= 7 ? "hot" : p.daysIdle >= p.warnAfter ? "warm" : "calm";
            return (
              <motion.li key={p.plexName} {...fold}>
                <article className={`m-person${busy === p.plexName ? " is-busy" : ""}`}>
                  <Initial name={p.displayName || p.plexName} big />
                  <div className="m-person__body">
                    {/* The heading is just the person; Rename and the status pills sit beside it. */}
                    <div className="m-card__title">
                      <h3 className="m-person__name">
                        {p.displayName || p.plexName}
                        {p.displayName ? <span className="muted m-person__plexname">{p.plexName} on Plex</span> : null}
                      </h3>
                      {p.tracked === false || renaming === p.plexName ? null : (
                        <button type="button" className="m-linkbtn" disabled={readOnly}
                          onClick={() => { setRenaming(p.plexName); setNewName(p.displayName || p.plexName); }}>Rename</button>
                      )}
                      {p.owner ? <span className="m-pill m-pill--safe">Owner</span> : null}
                      {p.topThree ? <span className="m-pill m-pill--safe"><ShieldCheck size={12} aria-hidden /> Top three</span> : null}
                      {p.neverRemove ? <span className="m-pill m-pill--safe"><ShieldCheck size={12} aria-hidden /> Never removed</span> : null}
                      {p.warned ? <span className="m-pill m-pill--declined">Warned</span> : null}
                      {p.hasAccess === false ? <span className="m-pill m-pill--declined">No longer on Plex</span> : null}
                      {p.tracked === false ? <span className="m-pill">Not tracked</span> : null}
                    </div>
                    <span className="m-link-row">
                      {p.linked ? (
                        <>
                          <span className="muted">Discord: <b className="m-link-row__name">{p.discordName ?? "linked"}</b></span>
                          <button type="button" className="m-linkbtn" disabled={readOnly} onClick={() => void unlink(p)}>Unlink</button>
                        </>
                      ) : (
                        <button type="button" className="m-linkbtn m-linkbtn--add" disabled={readOnly} onClick={() => setLinking(p)}>
                          <Link2 size={14} aria-hidden /> Link Discord
                        </button>
                      )}
                    </span>
                    {p.tracked === false ? null : (
                      <span className="muted m-person__meta">{p.lastWatched ? `Watched ${since(p.lastWatched)}` : "No watch recorded"}</span>
                    )}
                    {renaming === p.plexName ? (
                      <form className="m-match" onSubmit={(e) => { e.preventDefault(); void rename(p); }}>
                        <label htmlFor={`rn-${p.plexName}`} className="m-person__note"><b>Display name</b></label>
                        <p id={`rn-${p.plexName}-hint`} className="m-person__note">
                          Shown everywhere in Plexbie and in Tautulli. Their Plex username stays {p.plexName}.
                        </p>
                        <div className="m-match__row">
                          <input id={`rn-${p.plexName}`} className="m-select" value={newName} maxLength={40} autoFocus
                            aria-describedby={`rn-${p.plexName}-hint`}
                            onChange={(e) => setNewName(e.target.value)} />
                          <button type="button" className="btn m-btn" onClick={() => setRenaming(null)}>Back</button>
                          <button type="submit" className="btn btn--primary m-btn" disabled={readOnly || busy === p.plexName || !newName.trim()}>Save</button>
                        </div>
                      </form>
                    ) : null}
                    {p.hasAccess === false && p.candidates?.length ? (
                      <MatchPicker person={p} disabled={readOnly || busy === p.plexName} onMatch={(a) => void match(p, a)} />
                    ) : null}
                    {p.owner ? (
                      <span className="m-person__note">Owns the server.</span>
                    ) : p.tracked === false ? (
                      <span className="m-person__note">Not tracked, so the inactivity check never removes them. To remove them, use Plex’s own sharing settings.</span>
                    ) : (
                      <div className={`m-meter m-meter--${tone}`}>
                        <span className="m-meter__track"><span className="m-meter__fill" style={{ transform: `scaleX(${p.removalIn === null ? 1 : fill})` }} /></span>
                        <span className="m-meter__label">
                          {p.neverRemove ? "Never removed for not watching" : p.removalIn === null ? "Safe while in the top three" : p.removalIn === 0 ? "Due for removal" : `${p.removalIn} days until removal · idle ${p.daysIdle}`}
                        </span>
                      </div>
                    )}
                  </div>
                  {p.owner || p.tracked === false ? null : (
                    <div className="m-person__actions m-person__actions--keep">
                      <label className="m-keep">
                        <Switch on={!!p.neverRemove} label={`Never remove ${p.plexName}`} disabled={readOnly} onChange={(on) => void keep(p, on)} />
                        <span>Never remove</span>
                      </label>
                      <HoldButton label={p.hasAccess === false ? "Forget" : "Remove"} icon={<Trash2 size={16} aria-hidden />} ms={1400} disabled={readOnly || busy === p.plexName} onConfirm={() => void remove(p)} />
                    </div>
                  )}
                </article>
              </motion.li>
            );
          })}
        </AnimatePresence>
      </ul>
      {q && !shown.length ? <p className="muted">Nobody matches “{query}”.</p> : null}
      {createPortal(
        <AnimatePresence>
          {linking ? (
            <LinkSheet key="sheet" person={linking} onClose={() => setLinking(null)}
              onPick={async (m) => { if (await link(linking, m)) setLinking(null); }} />
          ) : null}
        </AnimatePresence>,
        document.body,
      )}
    </section>
  );
}

/** "No longer on Plex" can just mean Plexbie has the wrong account name (an invite
 * that went to the wrong email, say). Say which shared account they really are. */
function MatchPicker({ person, disabled, onMatch }: { person: AdminPerson; disabled: boolean; onMatch: (account: string) => void }) {
  const [pick, setPick] = useState("");
  const id = `match-${person.plexName}`;
  return (
    <div className="m-match">
      <label htmlFor={id} className="m-person__note">Still watching under another Plex account? Which one is {person.discordName || person.plexName}?</label>
      <div className="m-match__row">
        <select id={id} className="m-select" value={pick} onChange={(e) => setPick(e.target.value)} disabled={disabled}>
          <option value="">Choose a Plex account…</option>
          {person.candidates!.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <button type="button" className="btn btn--primary m-btn" disabled={disabled || !pick} onClick={() => onMatch(pick)}>
          <Link2 size={16} aria-hidden /> That’s them
        </button>
      </div>
    </div>
  );
}

/** A sheet from the bottom of the screen: pick which Discord member a Plex account belongs to. */
function LinkSheet({ person, onClose, onPick }: {
  person: AdminPerson; onClose: () => void; onPick: (m: { id: string; name: string }) => Promise<void>;
}) {
  const [members, setMembers] = useState<{ id: string; name: string; username: string }[] | null>(null);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  // Straight into the search box with a mouse; on a phone that would open the
  // keyboard over the list of people you're meant to tap.
  const finePointer = typeof matchMedia === "function" && matchMedia("(pointer: fine)").matches;

  useEffect(() => {
    api.linkCandidates().then((r) => setMembers(r.discord), () => setMembers([]));
  }, []);

  const q = query.trim().toLowerCase();
  const shown = (members ?? []).filter((m) => !q || `${m.name} ${m.username}`.toLowerCase().includes(q));

  return (
    <BottomSheet titleId="m-sheet-h" title={`Link ${person.plexName} to Discord`} onClose={onClose} className="m-sheet__panel--list">
      <p className="muted">Pick who this Plex account belongs to.{person.tracked === false ? " Linking also starts tracking them for inactivity." : ""}</p>
      <SearchField label="Find a Discord member" value={query} onChange={setQuery} className="m-sheet__search" autofocus={finePointer} />
      <ul className="m-sheet__list">
        {members === null ? <li className="muted">Loading members…</li> : null}
        {members && !shown.length ? <li className="muted">{members.length ? "Nobody matches." : "Everyone in the server is already linked."}</li> : null}
        {shown.map((m) => (
          <li key={m.id}>
            <button type="button" className="m-sheet__pick" disabled={!!busy}
              onClick={async () => { buzz(8); setBusy(m.id); await onPick(m); setBusy(null); }}>
              <Initial name={m.name} />
              <span><b>{m.name}</b><span className="muted">@{m.username}</span></span>
              <span className="m-sheet__go">{busy === m.id ? "Linking…" : "Link"}</span>
            </button>
          </li>
        ))}
      </ul>
    </BottomSheet>
  );
}

function DaysRing({ days, of }: { days: number; of: number }) {
  const r = 20;
  const c = 2 * Math.PI * r;
  const left = Math.max(0, Math.min(1, days / Math.max(1, of)));
  return (
    <span className={`m-ring${days <= 7 ? " is-hot" : ""}`} aria-hidden>
      <svg viewBox="0 0 48 48">
        <circle cx="24" cy="24" r={r} className="m-ring__track" />
        <circle cx="24" cy="24" r={r} className="m-ring__fill" strokeDasharray={c} strokeDashoffset={c * (1 - left)} />
      </svg>
      <b>{days}</b>
    </span>
  );
}

/** "Added Jul 8, not watched since" — when and why the 90-day clock started. */
function clockText(reason: string, when?: string) {
  const date = when ? shortDate(when) : "";
  if (reason === "added") return `Added ${date}, not watched since`.replace("Added ,", "Added,");
  if (reason === "requested") return `Requested ${date}`;
  return `Last watched ${date}`;
}

type CleanupView = "soon" | "next" | "kept";

/** A number with big − and + buttons: easy on a phone, and typing still works. */
function Stepper({ id, label, value, min, max, step = 1, suffix, onChange, disabled }: {
  id: string; label: string; value: number; min: number; max: number; step?: number; suffix: string;
  onChange: (n: number) => void; disabled?: boolean;
}) {
  const clamp = (n: number) => Math.max(min, Math.min(max, Math.round(n)));
  return (
    <div className="m-step">
      <label htmlFor={id} className="m-field-label">{label}</label>
      <div className="m-step__row">
        <button type="button" className="m-step__btn" aria-label={`${label}: fewer`} disabled={disabled || value <= min}
          onClick={() => { buzz(5); onChange(clamp(value - step)); }}>−</button>
        <input id={id} className="m-step__input" type="number" inputMode="numeric" min={min} max={max} value={value} disabled={disabled}
          onChange={(e) => { const n = Number(e.target.value); if (Number.isFinite(n)) onChange(clamp(n)); }} />
        <button type="button" className="m-step__btn" aria-label={`${label}: more`} disabled={disabled || value >= max}
          onClick={() => { buzz(5); onChange(clamp(value + step)); }}>+</button>
        <span className="m-step__suffix" aria-hidden>{suffix}</span>
      </div>
    </div>
  );
}

/**
 * Everything the Discord cleanup panel and /cleanup config can change. Each
 * control saves on its own; numbers wait until you stop tapping. Going live
 * (files really get deleted) is hold-to-confirm, and so is a live scan.
 */
function CleanupSettingsCard({ settings, libraries, channels }: {
  settings: CleanupSettings; libraries: string[]; channels: { id: string; name: string }[];
}) {
  const { patch, refresh, toast, readOnly } = useManage();
  const { act, later } = useAct();
  const [open, setOpen] = useState(false);
  const [days, setDays] = useState(settings.inactivityDays);
  const [warn, setWarn] = useState(settings.warnDaysBefore);
  const [scanning, setScanning] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  const reduced = useReducedMotion();
  const live = settings.enabled && !settings.practice;

  useEffect(() => { setDays(settings.inactivityDays); setWarn(settings.warnDaysBefore); }, [settings.inactivityDays, settings.warnDaysBefore]);

  const save = async (change: Partial<CleanupSettings>, quiet = false) => {
    patch("cleanup", (d) => ({ ...d, settings: { ...d.settings, ...change } }));
    const out = await act(null, () => api.cleanupSettings(change));
    if (out && !quiet) toast({ text: "Cleanup settings saved", detail: out.message });
    later("cleanup", 600);
  };
  const saveNumbers = (nextDays: number, nextWarn: number) => {
    setDays(nextDays);
    setWarn(nextWarn);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => void save({ inactivityDays: nextDays, warnDaysBefore: Math.min(nextWarn, nextDays - 1) }), 900);
  };
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const scan = async () => {
    setScanning(true);
    const out = await attempt(() => api.cleanupScan());
    setScanning(false);
    toast({ tone: out.ok ? "ok" : "error", text: out.ok ? "Cleanup scan finished" : "Scan didn’t run", detail: out.message });
    void refresh("cleanup");
  };

  return (
    <section className={`m-mode m-mode--card${live ? " is-live" : ""}`}>
      <button type="button" className="m-mode__head" aria-expanded={open} aria-controls="m-cleanup-settings" onClick={() => setOpen((o) => !o)}>
        <span className="m-mode__dot" aria-hidden />
        <span className="m-mode__text">
          <b>{settings.enabled ? (settings.practice ? "Practice mode" : "Live") : "Cleanup is off"}</b>
          <span className="muted">
            {settings.enabled
              ? settings.practice
                ? "Nothing is deleted; Plexbie only reports what it would remove."
                : `Titles nobody has watched or asked for in ${settings.inactivityDays} days are deleted. A warning goes out ${settings.warnDaysBefore} days before.`
              : "Nothing is being removed."}
            {settings.excludedLibraries.length ? ` Skips ${settings.excludedLibraries.join(", ")}.` : ""}
          </span>
        </span>
        <span className="m-mode__more">{open ? "Done" : "Settings"}</span>
      </button>
      <AnimatePresence initial={false}>
        {open ? (
          <motion.div
            id="m-cleanup-settings" className="m-settings2"
            initial={reduced ? { opacity: 0 } : { opacity: 0, height: 0 }}
            animate={reduced ? { opacity: 1 } : { opacity: 1, height: "auto" }}
            exit={reduced ? { opacity: 0 } : { opacity: 0, height: 0 }}
            transition={{ duration: 0.25, ease: EASE_OUT }}
          >
            <div className="m-settings2__inner">
              <div className="m-set-row">
                <span><b>Cleanup</b><span className="muted">Check every day for titles nobody watches.</span></span>
                <Switch on={settings.enabled} label="Cleanup on" disabled={readOnly} onChange={(on) => void save({ enabled: on })} />
              </div>

              <div className="m-set-row">
                <span>
                  <b>{settings.practice ? "Practice mode" : "Live mode"}</b>
                  <span className="muted">{settings.practice ? "Reports what it would delete, deletes nothing." : "Really deletes the files from the server."}</span>
                </span>
                {settings.practice
                  ? <HoldButton label="Go live" ms={1400} disabled={readOnly || !settings.enabled} onConfirm={() => void save({ practice: false })} />
                  : <button type="button" className="btn m-btn" disabled={readOnly} onClick={() => void save({ practice: true })}>Switch to practice</button>}
              </div>

              <div className="m-set-grid">
                <Stepper id="m-days" label="Remove after" suffix="days" value={days} min={30} max={3650} step={5} disabled={readOnly}
                  onChange={(n) => saveNumbers(n, Math.min(warn, n - 1))} />
                <Stepper id="m-warn" label="Warn" suffix="days before" value={warn} min={1} max={days - 1} disabled={readOnly}
                  onChange={(n) => saveNumbers(days, n)} />
              </div>

              {libraries.length ? (
                <div className="m-set-block">
                  <span className="m-field-label" id="m-libs">Libraries cleanup skips</span>
                  <div className="m-chips" role="group" aria-labelledby="m-libs">
                    {libraries.map((lib) => {
                      const skipped = settings.excludedLibraries.includes(lib);
                      return (
                        <button key={lib} type="button" aria-pressed={skipped} className={`m-chipbtn${skipped ? " is-on" : ""}`} disabled={readOnly}
                          onClick={() => { buzz(5); void save({ excludedLibraries: skipped ? settings.excludedLibraries.filter((l) => l !== lib) : [...settings.excludedLibraries, lib] }); }}>
                          {skipped ? <ShieldCheck size={14} aria-hidden /> : null}{lib}
                        </button>
                      );
                    })}
                  </div>
                </div>
              ) : null}

              {channels.length ? (
                <div className="m-set-block field">
                  <label htmlFor="m-channel" className="m-field-label">Post cleanup notices in</label>
                  <select id="m-channel" className="m-select" value={settings.channelId ?? ""} disabled={readOnly}
                    onChange={(e) => void save({ channelId: e.target.value || null })}>
                    <option value="">Nowhere</option>
                    {channels.map((c) => <option key={c.id} value={c.id}>#{c.name}</option>)}
                  </select>
                </div>
              ) : null}

              <div className="m-set-row">
                <span><b>Run a scan now</b><span className="muted">{live ? "Live: anything past its time is deleted right away." : "Same check the daily run does."}</span></span>
                {live
                  ? <HoldButton label="Scan now" ms={1400} disabled={readOnly || scanning} onConfirm={() => void scan()} />
                  : <button type="button" className="btn m-btn" disabled={readOnly || scanning || !settings.enabled} onClick={() => void scan()}>
                    <RefreshCw size={16} aria-hidden className={scanning ? "m-spin" : undefined} /> {scanning ? "Scanning…" : "Scan now"}
                  </button>}
              </div>
            </div>
          </motion.div>
        ) : null}
      </AnimatePresence>
    </section>
  );
}

function CleanupTab() {
  const { data, patch, toast, readOnly } = useManage();
  const { act, later } = useAct();
  const [view, setView] = useState<CleanupView>("soon");
  const res = data.cleanup;
  if (!res) return <div className="skeleton" style={{ height: 300 }} />;
  const { settings, warning, upcoming, exempt } = res;

  const keep = async (row: { ratingKey: string; title: string; type?: string | null }, on: boolean, quiet = false) => {
    // Move it straight away; the server confirms, and a failure puts it back.
    patch("cleanup", (d) => on
      ? {
        ...d,
        warning: d.warning.filter((c) => c.ratingKey !== row.ratingKey),
        upcoming: d.upcoming.filter((c) => c.ratingKey !== row.ratingKey),
        exempt: [{ ratingKey: row.ratingKey, title: row.title, type: row.type ?? null }, ...d.exempt.filter((e) => e.ratingKey !== row.ratingKey)],
      }
      : { ...d, exempt: d.exempt.filter((e) => e.ratingKey !== row.ratingKey) });
    const out = await act(null, () => api.exempt(row.ratingKey, on), { buzzOnFail: true });
    if (out && !quiet) {
      toast({
        text: on ? `${row.title} is kept forever.` : `${row.title} is back on the clock.`,
        undo: () => void keep(row, !on, true),
      });
    }
    later("cleanup", 900);
  };

  const row = (c: AdminCleanupRow) => (
    <motion.li key={c.ratingKey} {...fold}>
      <article className="m-leaving">
        <DaysRing days={c.daysLeft} of={settings.inactivityDays} />
        <div className="m-leaving__body">
          <h3 className="m-card__title">{c.title}</h3>
          <span className="m-person__meta">
            <span className={`m-left${c.daysLeft <= 7 ? " is-hot" : ""}`}>{c.daysLeft === 1 ? "1 day left" : `${c.daysLeft} days left`}</span>
            <span className="muted"> · {c.type === "show" ? "TV" : "Film"} · {clockText(c.reason, c.lastActivity)}</span>
          </span>
        </div>
        <span className="m-keep">
          <span aria-hidden>Keep</span>
          <Switch on={false} label={`Keep ${c.title} forever`} disabled={readOnly} onChange={() => void keep(c, true)} />
        </span>
      </article>
    </motion.li>
  );

  const views: { id: CleanupView; label: string; n: number }[] = [
    { id: "soon", label: "Leaving soon", n: warning.length },
    { id: "next", label: "Next up", n: upcoming.length },
    { id: "kept", label: "Kept", n: exempt.length },
  ];

  return (
    <div className="m-stack">
      <CleanupSettingsCard settings={settings} libraries={res.libraries ?? []} channels={res.channels ?? []} />

      <section className="section">
        <h2>On the clock</h2>
        <Segmented id="cleanup-views" label="Which titles to show" value={view} onChange={setView}
          options={views.map((v) => ({ id: v.id, label: <>{v.label} <span className="m-seg__n"><Tick value={v.n} /></span></> }))} />

        {view === "soon" ? (
          <>
            <ul className="m-rows"><AnimatePresence initial={false}>{warning.map(row)}</AnimatePresence></ul>
            {!warning.length ? <AllClear title="Nothing leaving this week">No title is inside the {settings.warnDaysBefore}-day warning window.</AllClear> : null}
          </>
        ) : view === "next" ? (
          <ul className="m-rows"><AnimatePresence initial={false}>{upcoming.map(row)}</AnimatePresence></ul>
        ) : (
          <>
            <ul className="m-rows">
              <AnimatePresence initial={false}>
                {exempt.map((e) => (
                  <motion.li key={e.ratingKey} {...fold}>
                    <article className="m-leaving">
                      <span className="m-ring is-kept" aria-hidden><ShieldCheck size={20} /></span>
                      <div className="m-leaving__body">
                        <h3 className="m-card__title">{e.title}</h3>
                        <span className="muted m-person__meta">{e.type === "show" ? "TV" : e.type === "movie" ? "Film" : e.type ?? "Title"} · never removed</span>
                      </div>
                      <span className="m-keep">
                        <span aria-hidden>Keep</span>
                        <Switch on label={`Keep ${e.title} forever`} disabled={readOnly} onChange={() => void keep(e, false)} />
                      </span>
                    </article>
                  </motion.li>
                ))}
              </AnimatePresence>
            </ul>
            {!exempt.length ? <AllClear title="Nothing kept forever yet">Flip “Keep” on any title and cleanup will never touch it.</AllClear> : null}
          </>
        )}
      </section>
    </div>
  );
}

function HealthTab() {
  const { data, refresh } = useManage();
  const [spinning, setSpinning] = useState(false);
  const rows = data.health;
  if (!rows) return <div className="skeleton" style={{ height: 200 }} />;
  const again = async () => {
    setSpinning(true);
    await refresh("health");
    setSpinning(false);
  };
  return (
    <section className="section">
      <div className="m-head">
        <h2>Services</h2>
        <button type="button" className="btn m-btn" onClick={again} disabled={spinning}>
          <RefreshCw size={16} aria-hidden className={spinning ? "m-spin" : undefined} /> Check again
        </button>
      </div>
      {!rows.length ? (
        <AllClear title="Nothing to check yet">Connect Sonarr, Radarr, Seerr, Tautulli or SABnzbd on the setup page and they’re watched here.</AllClear>
      ) : null}
      <ul className="m-health">
        {rows.map((h) => (
          <li className={`m-health__item${h.ok ? "" : " is-down"}`} key={h.name}>
            <span className="m-health__dot" aria-hidden />
            <div>
              <b>{h.name}</b>
              <span className="muted">{h.ok ? `Answering in ${h.ms} ms` : h.detail ?? "Not answering"}</span>
              {h.ok ? <span className="m-health__bar" aria-hidden><span style={{ transform: `scaleX(${Math.min(1, Math.max(0.04, h.ms / 600))})` }} /></span> : null}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}


/* ============================================================== invites */

const LASTS = [1, 3, 7, 14];

function daysUntil(iso: string) {
  return inDays(Math.ceil((new Date(iso).getTime() - Date.now()) / 864e5));
}

/** The new link, shown once: share it straight from the phone, or copy it. */
function FreshLink({ made, onAnother }: { made: NewInvite; onAnother: () => void }) {
  const [copied, flashCopied] = useCopied(1800);
  const canShare = typeof navigator !== "undefined" && "share" in navigator;
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(made.url);
    } catch {
      const field = document.getElementById("m-invite-url") as HTMLInputElement | null;
      field?.select();
      document.execCommand?.("copy");
    }
    buzz(10);
    flashCopied();
  };
  const share = async () => {
    try {
      await navigator.share({
        title: "Your invite to the household Plex",
        text: `Here’s your invite to our Plex, ${made.invite.label}. Open it and sign in with Plex (it’s free if you don’t have an account yet).`,
        url: made.url,
      });
    } catch { /* closed the share sheet */ }
  };
  return (
    <motion.div
      className="m-fresh" role="status"
      initial={{ opacity: 0, transform: "translateY(8px) scale(0.98)" }}
      animate={{ opacity: 1, transform: "translateY(0px) scale(1)" }}
      transition={{ type: "spring", duration: 0.45, bounce: 0.2 }}
    >
      <span className="m-fresh__eyebrow"><Ticket size={14} aria-hidden /> Invite ready</span>
      <h3 className="m-card__title">For {made.invite.label}</h3>
      <label className="visually-hidden" htmlFor="m-invite-url">Invite link</label>
      <input id="m-invite-url" className="m-fresh__url" readOnly value={made.url} onFocus={(e) => e.currentTarget.select()} />
      <div className="m-fresh__actions">
        {canShare ? (
          <button type="button" className="btn m-btn m-btn--go" onClick={share}><Share2 size={18} aria-hidden /> Send it</button>
        ) : null}
        <button type="button" className={`btn m-btn${canShare ? "" : " m-btn--go"}`} onClick={copy}>
          {copied ? <Check size={18} aria-hidden /> : <Copy size={18} aria-hidden />} {copied ? "Copied" : "Copy link"}
        </button>
      </div>
      <p className="muted m-fresh__note">
        Works once, until {shortDate(made.invite.expiresAt)}{made.invite.email ? `, and only for the Plex account with ${made.invite.email}` : ""}.
        {" "}This is the only time you’ll see the link, so send it now.
      </p>
      <button type="button" className="btn btn--quiet m-fresh__again" onClick={onAnother}><Plus size={16} aria-hidden /> Make another</button>
    </motion.div>
  );
}

/** Invites sent on plex.tv that nobody has accepted: fix a wrong address, or take one back. */
function PlexInvites() {
  const { data, patch, toast, readOnly } = useManage();
  const rows = data.plexinvites;
  const [editing, setEditing] = useState<string | null>(null);
  const [next, setNext] = useState("");
  const { busy, act, later } = useAct();
  if (!rows || !rows.length) return null;

  const change = async (i: PlexInvite) => {
    const out = await act(i.email, () => api.plexInviteChange(i.email, next.trim()), { failText: "Not sent" });
    if (!out) return;
    buzz(14);
    toast({ text: out.message });
    setEditing(null);
    setNext("");
    patch("plexinvites", (d) => d.map((x) => (x.email === i.email ? { ...x, email: next.trim(), sentAt: new Date().toISOString() } : x)));
    later("plexinvites", 1500);
  };
  const cancel = async (i: PlexInvite) => {
    const out = await act(null, () => api.plexInviteCancel(i.email));
    if (!out) return;
    toast({ text: out.message });
    patch("plexinvites", (d) => d.filter((x) => x.email !== i.email));
  };

  return (
    <section className="section">
      <h2>Waiting on Plex <span className="m-count"><Tick value={rows.length} /></span></h2>
      <p className="muted">Plex invites nobody has accepted yet. Sent to the wrong address? Change it and the invite goes to the right one.</p>
      <ul className="m-rows">
        <AnimatePresence initial={false}>
          {rows.map((i) => (
            <motion.li key={i.email} {...fold}>
              <article className="m-person">
                <Initial name={i.who || i.email} big />
                <div className="m-person__body">
                  <h3 className="m-card__title">{i.who || i.email}</h3>
                  <span className="muted m-person__meta">{i.who ? `${i.email} · ` : ""}sent {since(i.sentAt)}</span>
                </div>
                {editing === i.email ? null : (
                  <div className="m-person__actions m-person__actions--two">
                    <button type="button" className="btn m-btn" disabled={readOnly} onClick={() => { setEditing(i.email); setNext(""); }}>
                      <Mail size={16} aria-hidden /> Change email
                    </button>
                    <HoldButton label="Cancel" icon={<X size={16} aria-hidden />} ms={800} disabled={readOnly} onConfirm={() => void cancel(i)} />
                  </div>
                )}
                  {editing === i.email ? (
                    <form className="m-plex-invite__edit" onSubmit={(e) => { e.preventDefault(); void change(i); }}>
                      <div className="field">
                      <label htmlFor={`pi-${i.email}`}>The right email</label>
                      <input id={`pi-${i.email}`} type="email" inputMode="email" autoComplete="off" autoFocus
                        value={next} onChange={(e) => setNext(e.target.value)} placeholder="their Plex email" />
                      </div>
                      <div className="m-person__actions m-person__actions--two">
                        <button type="button" className="btn m-btn" onClick={() => { setEditing(null); setNext(""); }}>Back</button>
                        <button type="submit" className="btn btn--primary m-btn" disabled={readOnly || busy === i.email || !next.trim()}>
                          <Mail size={16} aria-hidden /> {busy === i.email ? "Sending…" : "Send invite"}
                        </button>
                      </div>
                    </form>
                  ) : null}
              </article>
            </motion.li>
          ))}
        </AnimatePresence>
      </ul>
    </section>
  );
}

function InvitesTab() {
  const { data, patch, toast, readOnly } = useManage();
  const { act, later } = useAct();
  const [label, setLabel] = useState("");
  const [email, setEmail] = useState("");
  const [days, setDays] = useState(7);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [made, setMade] = useState<NewInvite | null>(null);
  const rows = data.invites;

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!label.trim()) { setProblem("Give it a name, so you know who it’s for."); return; }
    setBusy(true);
    setProblem("");
    try {
      const out = await api.createInvite({ label: label.trim(), email: email.trim() || undefined, days });
      buzz(14);
      setMade(out);
      setLabel("");
      setEmail("");
      patch("invites", (d) => [out.invite, ...d]);
    } catch (err) {
      setProblem(err instanceof Error ? err.message : "That didn’t work.");
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (i: AdminInvite) => {
    const out = await act(null, () => api.revokeInvite(i.id));
    if (!out) return;
    toast({ text: `Cancelled ${i.label}’s invite`, detail: out.message });
    patch("invites", (d) => d.map((x) => (x.id === i.id ? { ...x, status: "revoked" } : x)));
    later("invites", 1500);
  };

  const top = useRef<HTMLElement>(null);
  const [renewing, setRenewing] = useState<string | null>(null);

  // A fresh link for the same person, shown in the "Invite ready" card at the top.
  const renew = async (i: AdminInvite) => {
    setRenewing(i.id);
    try {
      const out = await api.renewInvite(i.id);
      buzz(14);
      setMade(out);
      patch("invites", (d) => [out.invite, ...(i.status === "used" ? d : d.filter((x) => x.id !== i.id))]);
      top.current?.scrollIntoView({ behavior: scrollBehavior(), block: "start" });
      later("invites", 1500);
    } catch (err) {
      toast({ tone: "error", text: "No new link", detail: err instanceof Error ? err.message : undefined });
    } finally {
      setRenewing(null);
    }
  };

  const remove = async (i: AdminInvite) => {
    const out = await act(null, () => api.deleteInvite(i.id));
    if (!out) return;
    toast({ text: `Deleted ${i.label}’s invite` });
    patch("invites", (d) => d.filter((x) => x.id !== i.id));
  };

  const active = (rows ?? []).filter((i) => i.status === "active");
  const past = (rows ?? []).filter((i) => i.status !== "active");
  const renewButton = (i: AdminInvite, label = "New link") => (
    <button type="button" className="btn m-btn" disabled={readOnly || renewing === i.id} onClick={() => void renew(i)}>
      <RefreshCw size={16} aria-hidden className={renewing === i.id ? "m-spin" : undefined} /> {renewing === i.id ? "Making it…" : label}
    </button>
  );

  return (
    <div className="m-stack">
      <section className="section" ref={top}>
        <h2>Invite someone</h2>
        <p className="muted">
          For people who don’t use Discord. They open the link, sign in with Plex, and they’re in. No approval needed:
          making the link is your yes.
        </p>
        <AnimatePresence mode="wait" initial={false}>
          {made ? (
            <FreshLink key="made" made={made} onAnother={() => setMade(null)} />
          ) : (
            <motion.form
              key="form" className="m-invite-form" onSubmit={create} noValidate
              initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0, transition: { duration: 0.12 } }}
            >
              <div className="field">
                <label htmlFor="m-invite-name">Who’s it for?</label>
                <input id="m-invite-name" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Mom" maxLength={60} autoComplete="off" />
              </div>
              <div className="field">
                <label htmlFor="m-invite-email">Their Plex email <span className="muted">(optional)</span></label>
                <input id="m-invite-email" type="email" inputMode="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="name@example.com" autoComplete="off" aria-describedby="m-invite-email-hint" />
                <span className="field__hint" id="m-invite-email-hint"><Lock size={12} aria-hidden /> Locks the link to that Plex account, so a forwarded link won’t work for anyone else.</span>
              </div>
              <div className="field">
                <span className="m-field-label" id="m-invite-days">Link works for</span>
                <Segmented id="invite-days" labelledBy="m-invite-days" value={days} onChange={setDays}
                  options={LASTS.map((d) => ({ id: d, label: d === 1 ? "1 day" : `${d} days` }))} />
              </div>
              {problem ? <p className="field__error" role="alert">{problem}</p> : null}
              <button type="submit" className="btn btn--primary m-btn m-invite-form__go" disabled={busy || readOnly}>
                <Ticket size={18} aria-hidden /> {busy ? "Making it…" : "Make invite link"}
              </button>
              {readOnly ? <p className="muted m-actions__note">Switched off in the read-only preview.</p> : null}
            </motion.form>
          )}
        </AnimatePresence>
      </section>

      <PlexInvites />

      <section className="section">
        <h2>Open invites <span className="m-count"><Tick value={active.length} /></span></h2>
        {!rows ? <div className="skeleton" style={{ height: 120 }} /> : null}
        <ul className="m-rows">
          <AnimatePresence initial={false}>
            {active.map((i) => (
              <motion.li key={i.id} {...fold}>
                <article className="m-person">
                  <Initial name={i.label} big />
                  <div className="m-person__body">
                    <h3 className="m-card__title">
                      {i.label}
                      {i.email ? <span className="m-pill"><Lock size={11} aria-hidden /> Locked</span> : null}
                    </h3>
                    <span className="muted m-person__meta">
                      Expires {daysUntil(i.expiresAt)} · made by {i.createdBy ?? "an admin"} {since(i.createdAt)}
                      {i.email ? ` · for ${i.email}` : ""}
                    </span>
                  </div>
                  <div className="m-person__actions m-person__actions--two">
                    {renewButton(i)}
                    <HoldButton label="Cancel" icon={<X size={16} aria-hidden />} ms={800} disabled={readOnly} onConfirm={() => void revoke(i)} />
                  </div>
                </article>
              </motion.li>
            ))}
          </AnimatePresence>
        </ul>
        {rows && !active.length ? <AllClear title="No open invites">Make one above when someone needs to get in without Discord.</AllClear> : null}
      </section>

      {past.length ? (
        <section className="section">
          <h2>Earlier</h2>
          <p className="muted">Lost a link, or it ran out? Make a new one for the same person. Cancelled and expired invites can be deleted; used ones stay as a record of who joined.</p>
          <ul className="m-rows">
            <AnimatePresence initial={false}>
              {past.map((i) => (
                <motion.li key={i.id} {...fold}>
                  <article className="m-person m-person--past">
                    <Initial name={i.label} big />
                    <div className="m-person__body">
                      <h3 className="m-card__title">
                        {i.label}
                        <span className={`m-pill m-pill--${i.status === "used" ? "approved" : "declined"}`}>{i.status === "revoked" ? "Cancelled" : i.status}</span>
                      </h3>
                      <span className="muted m-person__meta">
                        {i.status === "used" && i.usedBy ? `Joined as ${i.usedBy} ${i.usedAt ? since(i.usedAt) : ""}` : `Made by ${i.createdBy ?? "an admin"} ${since(i.createdAt)}`}
                        {i.email ? ` · for ${i.email}` : ""}
                      </span>
                    </div>
                    <div className={`m-person__actions${i.status === "used" ? "" : " m-person__actions--two"}`}>
                      {renewButton(i, i.status === "used" ? "Invite again" : "New link")}
                      {i.status === "used" ? null : (
                        <HoldButton label="Delete" icon={<Trash2 size={16} aria-hidden />} ms={700} disabled={readOnly} onConfirm={() => void remove(i)} />
                      )}
                    </div>
                  </article>
                </motion.li>
              ))}
            </AnimatePresence>
          </ul>
        </section>
      ) : null}
    </div>
  );
}


/* ============================================================== discord */

/** What /say, /who-invited and /watchparty-active do, from the website. */
function DiscordTab() {
  const { data, refresh, toast, readOnly } = useManage();
  const { act } = useAct();
  const d = data.discord;
  const [channel, setChannel] = useState("");
  const [text, setText] = useState("");
  const [pings, setPings] = useState(false);
  const [sending, setSending] = useState(false);
  const [query, setQuery] = useState("");
  if (!d) return <div className="skeleton" style={{ height: 300 }} />;
  const chosen = channel || d.channels[0]?.id || "";

  const send = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!text.trim() || !chosen) return;
    setSending(true);
    const out = await act(null, () => api.say(chosen, text.trim(), pings), { failText: "Not posted" });
    setSending(false);
    if (!out) return;
    buzz(12);
    toast({ text: "Posted as Plexbie", detail: out.message });
    setText("");
    setPings(false);
  };

  const q = query.trim().toLowerCase();
  const joins = q ? d.joins.filter((j) => `${j.who} ${j.by} ${j.code ?? ""}`.toLowerCase().includes(q)) : d.joins;

  return (
    <div className="m-stack">
      <section className="section">
        <h2>Watch party</h2>
        {d.party ? (
          <div className="m-party">
            <span className="m-party__live"><Radio size={16} aria-hidden /> Live{d.party.startedAt ? ` · started ${since(d.party.startedAt)}` : ""}</span>
            <h3 className="m-card__title">{d.party.title ?? "Something on Plex"}</h3>
            <p className="muted">{d.party.streamer} is streaming{d.party.channel ? ` in ${d.party.channel}` : ""}.</p>
            <ul className="m-party__people" aria-label="Watching together">
              {d.party.people.map((n) => <li key={n}><Initial name={n} />{n}</li>)}
            </ul>
          </div>
        ) : (
          <p className="muted">No watch party right now. One starts on its own when someone streams Plex in the Watch Party voice channel.</p>
        )}
        <button type="button" className="btn btn--quiet m-btn" style={{ justifySelf: "start" }} onClick={() => void refresh("discord")}><RefreshCw size={16} aria-hidden /> Check again</button>
      </section>

      <section className="section">
        <h2>Say something as Plexbie</h2>
        <form className="m-invite-form" onSubmit={send}>
          <div className="field">
            <label htmlFor="m-say-channel">Channel</label>
            <select id="m-say-channel" className="m-select" value={chosen} onChange={(e) => setChannel(e.target.value)} disabled={readOnly}>
              {d.channels.map((c) => <option key={c.id} value={c.id}>#{c.name}</option>)}
            </select>
          </div>
          <div className="field">
            <label htmlFor="m-say-text">Message</label>
            <textarea id="m-say-text" className="m-textarea" rows={4} maxLength={2000} value={text} placeholder="Movie night Friday at 8!"
              onChange={(e) => setText(e.target.value)} disabled={readOnly} aria-describedby="m-say-count" />
            <span className="field__hint" id="m-say-count">{text.length}/2000</span>
          </div>
          <div className="m-set-row">
            <span><b>Allow @everyone and role pings</b><span className="muted">Off: those show as plain text and notify nobody.</span></span>
            <Switch on={pings} label="Allow @everyone and role pings" disabled={readOnly} onChange={setPings} />
          </div>
          <button type="submit" className="btn btn--primary m-btn m-invite-form__go" disabled={readOnly || sending || !text.trim()}>
            <MessageSquare size={18} aria-hidden /> {sending ? "Posting…" : "Post it"}
          </button>
        </form>
      </section>

      <section className="section">
        <div className="m-head">
          <h2>Who brought whom</h2>
          <SearchField value={query} onChange={setQuery} />
        </div>
        <p className="muted">Discord invites people used to join the server, and Plexbie invite links people used to get on Plex.</p>
        <ul className="m-log">
          {joins.map((j, i) => (
            <li className="m-log__row" key={`${j.who}-${j.at}-${i}`}>
              <span className={`m-pill${j.via === "plexbie" ? " m-pill--safe" : ""}`}>{j.via === "plexbie" ? "Link" : "Discord"}</span>
              <span className="m-log__main">{j.who} <span className="muted">← {j.by}</span></span>
              <span className="muted m-log__meta">
                {j.via === "plexbie" ? `invite “${j.code}”` : `code ${j.code}`}{j.role ? ` · got ${j.role}` : ""}{j.at ? ` · ${since(j.at)}` : ""}
              </span>
            </li>
          ))}
        </ul>
        {!joins.length ? <p className="muted">{q ? "Nobody matches." : "No invites recorded yet."}</p> : null}
      </section>
    </div>
  );
}


/* ============================================================= messages */

const VIA: Record<MessageChannel, { label: string; icon: Icon }> = {
  discord: { label: "Discord DM", icon: MessageCircle },
  web: { label: "Website alert", icon: Bell },
  email: { label: "Email", icon: Mail },
  none: { label: "Not delivered", icon: CircleAlert },
};

/** Where something a person sent Plexbie came from. */
const FROM: Partial<Record<MessageChannel, { label: string; icon: Icon }>> = {
  discord: { label: "Sent on Discord", icon: MessageCircle },
  web: { label: "Sent on the website or app", icon: Bell },
};

function ViaChip({ channel, delivered, error, incoming = false }: { channel: MessageChannel; delivered: boolean; error?: string | null; incoming?: boolean }) {
  const v = incoming ? FROM[channel] ?? VIA[channel] : delivered ? VIA[channel] : { label: channel === "discord" ? "Discord DM didn't arrive" : "Not delivered", icon: CircleAlert };
  const Icon = v.icon;
  return (
    <span className={`m-via${delivered ? "" : " is-failed"}`} title={error ?? undefined}>
      <Icon size={13} aria-hidden /> {v.label}{!delivered && error ? `: ${error}` : ""}
    </span>
  );
}

function dayLabel(iso: string) {
  const d = new Date(iso), today = new Date();
  const days = Math.round((new Date(today.toDateString()).getTime() - new Date(d.toDateString()).getTime()) / 864e5);
  return days === 0 ? "Today" : days === 1 ? "Yesterday" : d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

/** One person's messages with Plexbie, Discord style: newest at the bottom, a day line
 *  between days, and what they sent Plexbie (tinted) between Plexbie's messages. */
function Conversation({ person, onBack }: { person: MessagePerson; onBack: () => void }) {
  const res = useLoad<LoggedMessage[]>(() => api.conversation(person.id), [person.id]);
  // On a phone the conversation covers the list: Back returns to the list, not off the page.
  useBackToClose(true, onBack);
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [res.data]);
  let lastDay = "";
  return (
    <section className="m-chat" aria-label={`Messages with ${person.name}`}>
      <header className="m-chat__head">
        <button type="button" className="m-chat__back" onClick={onBack} aria-label="Back to everyone"><ArrowLeft size={20} aria-hidden /></button>
        <Initial name={person.name} big />
        <div><h3 className="m-card__title">{person.name}</h3>
          <span className="muted m-person__meta">{person.count} message{person.count === 1 ? "" : "s"} from Plexbie{person.received ? ` · ${person.received} from ${person.name}` : ""} · by {person.via.map((v) => VIA[v].label.toLowerCase()).join(", ")}</span></div>
      </header>
      <div className="m-chat__body">
        {res.loading ? <div className="skeleton" style={{ height: 160 }} /> : null}
        {(res.data ?? []).map((m) => {
          const day = dayLabel(m.at);
          const showDay = day !== lastDay;
          lastDay = day;
          return (
            <div key={m.id}>
              {showDay ? <div className="m-chat__day"><span>{day}</span></div> : null}
              <article className={`m-msg${m.direction === "in" ? " is-in" : ""}${m.delivered ? "" : " is-failed"}`}>
                {m.direction === "in" ? <span className="m-msg__avatar"><Initial name={person.name} /></span>
                  : <img className="m-msg__avatar" src="/brand/plexbie-64.png" alt="" width={36} height={36} />}
                <div className="m-msg__main">
                  <div className="m-msg__meta"><b>{m.direction === "in" ? person.name : "Plexbie"}</b> <time dateTime={m.at}>{new Date(m.at).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}</time></div>
                  <div className="m-msg__bubble">
                    {m.title ? <b className="m-msg__title">{m.title}</b> : null}
                    <p>{m.text}</p>
                  </div>
                  <ViaChip channel={m.channel} delivered={m.delivered} error={m.error} incoming={m.direction === "in"} />
                </div>
              </article>
            </div>
          );
        })}
        {res.data && !res.data.length ? <p className="muted">No messages kept for {person.name}.</p> : null}
        <div ref={end} />
      </div>
    </section>
  );
}

/** Everything Plexbie has said to people, by Discord DM, website alert or email. */
function MessagesTab() {
  const { data } = useManage();
  const [open, setOpen] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const people = data.messages;
  if (!people) return <div className="skeleton" style={{ height: 300 }} />;
  const q = query.trim().toLowerCase();
  const shown = q ? people.filter((p) => p.name.toLowerCase().includes(q)) : people;
  const current = people.find((p) => p.id === open) ?? null;
  return (
    <section className="section">
      <div className="m-head">
        <h2>Messages</h2>
        <SearchField value={query} onChange={setQuery} />
      </div>
      <p className="muted">Every message Plexbie sends someone and how it got there, and what they send Plexbie: DMs, “Something wrong?” and answers on their tickets. Kept for 90 days.</p>
      <div className={`m-inbox${current ? " has-open" : ""}`}>
        <ul className="m-inbox__list" aria-label="People">
          {shown.map((p) => (
            <li key={p.id}>
              <button type="button" className={`m-inbox__person${p.id === open ? " is-open" : ""}`} aria-current={p.id === open ? "true" : undefined} onClick={() => { buzz(5); setOpen(p.id); }}>
                <Initial name={p.name} big />
                <span className="m-inbox__text">
                  <span className="m-inbox__top"><b>{p.name}</b><time dateTime={p.last.at}>{since(p.last.at)}</time></span>
                  <span className="m-inbox__last">{p.last.direction === "in" ? <b>{p.name.split(" ")[0]}: </b> : null}{p.last.text}</span>
                  <span className="m-inbox__via">
                    {p.via.map((v) => { const Icon = VIA[v].icon; return <Icon key={v} size={13} aria-label={VIA[v].label} />; })}
                    {p.failed ? <span className="m-inbox__failed">{p.failed} not delivered</span> : null}
                  </span>
                </span>
              </button>
            </li>
          ))}
          {!shown.length ? <li className="muted" style={{ padding: 12 }}>{q ? "Nobody matches." : "No messages yet."}</li> : null}
        </ul>
        {current ? <Conversation person={current} onBack={() => setOpen(null)} /> : (
          <div className="m-inbox__empty muted">Pick someone to see their messages with Plexbie.</div>
        )}
      </div>
    </section>
  );
}


/* ========================================================== needs help */

/* ========================================================= all requests */

type Show = "progress" | "stuck" | "finished" | "everything";
const SHOW: { id: Show; label: string }[] = [
  { id: "progress", label: "In progress" }, { id: "stuck", label: "Looks stuck" },
  { id: "finished", label: "Finished (30 days)" }, { id: "everything", label: "Everything" },
];

/**
 * Every approved request from everyone, where it is now, and which look stuck. Without a
 * search it's what's on its way plus the last 30 days of finished; a search reaches any
 * request ever. A row opens the request in full, with the fixes and "Open a ticket".
 */
function AllRequestsTab() {
  const { data, failed } = useManage();
  const [show, setShow] = useState<Show>(() => ((data.all?.counts?.stuck ?? 0) > 0 ? "stuck" : "progress"));
  const [q, setQ] = useState("");
  const [found, setFound] = useState<AdminAllRequests | null>(null);
  const [searching, setSearching] = useState(false);
  const [open, setOpen] = useState<string | null>(null);

  // Searching asks the bot (it reaches every request ever, not only the ones listed).
  const words = q.trim();
  useEffect(() => {
    if (!words) return;
    const t = window.setTimeout(() => {
      void api.adminAll(words).then(setFound).catch(() => setFound({ rows: [], counts: null, query: words })).finally(() => setSearching(false));
    }, 300);
    return () => window.clearTimeout(t);
  }, [words]);
  const typed = (value: string) => { setQ(value); setSearching(!!value.trim()); };

  const all = data.all;
  if (!all) return failed.all ? null : <p className="muted">Loading…</p>;
  const counts = all.counts ?? { active: 0, stuck: 0, finished: 0 };
  const results = words ? found : null;
  const shown = results ? results.rows : words ? [] : all.rows.filter((r) =>
    show === "progress" ? r.stage !== "available" : show === "stuck" ? r.stuck.length > 0 : show === "finished" ? r.stage === "available" : true);

  return (
    <section className="section m-all">
      <h2>All requests</h2>
      <p className="muted m-all__lede">Everyone’s approved requests and where each one is now. Open one to see it in full, search again, or open a ticket.</p>
      <div className="request-bar m-all__bar">
        <label className="finder__field m-all__search">
          <Search size={18} aria-hidden />
          <span className="visually-hidden">Search every request</span>
          <input type="search" value={q} onChange={(e) => typed(e.target.value)} placeholder="Search every request: a title, a name or a number"
            autoComplete="off" enterKeyHint="search" />
        </label>
        {!words ? (
          <select className="select" value={show} aria-label="Show" onChange={(e) => setShow(e.target.value as Show)}>
            {SHOW.map((s) => {
              const n = s.id === "progress" ? counts.active : s.id === "stuck" ? counts.stuck : s.id === "finished" ? counts.finished : all.rows.length;
              return <option key={s.id} value={s.id}>{`${s.label} · ${n}`}</option>;
            })}
          </select>
        ) : null}
      </div>
      {words ? (
        <p className="muted m-all__count" role="status">
          {searching || !results ? "Searching…" : `${results.rows.length === 0 ? "Nothing" : results.rows.length === 1 ? "1 request" : `${results.rows.length} requests`} found`}
        </p>
      ) : null}
      {shown.length ? (
        <ul className="m-rows m-all__rows">
          {shown.map((r) => <li key={r.id}><AllRequestRow r={r} onOpen={() => setOpen(r.id!)} /></li>)}
        </ul>
      ) : !words ? (
        <p className="muted">{show === "stuck" ? "Nothing looks stuck right now." : show === "finished" ? "Nothing finished in the last 30 days." : "Nothing on its way right now."}</p>
      ) : null}
      {/* Outside the page, like every sheet: the page goes inert while it's open. */}
      {createPortal(<AnimatePresence>{open ? <RequestSheet key={open} id={open} onClose={() => setOpen(null)} /> : null}</AnimatePresence>, document.body)}
    </section>
  );
}

function rowLine(r: AdminRequestRow) {
  const p = r.progress;
  if (r.stage === "downloading" && typeof p?.percent === "number") return `${p.percent}%${p.detail ? `, ${p.detail}` : ""}`;
  if (r.stage === "available") return r.finishedAt ? `On Plex since ${since(r.finishedAt)}` : "On Plex";
  return p?.detail ?? null;
}

function AllRequestRow({ r, onOpen }: { r: AdminRequestRow; onOpen: () => void }) {
  const line = rowLine(r);
  return (
    <button type="button" className={`slot slot--journey slot--button${r.stuck.length ? " is-stuck" : ""}`} onClick={onOpen}>
      <span className="slot__no">No.<b>{formatSlot(r.slot)}</b></span>
      <span className="slot__poster"><Art title={r.title} size="w185" /></span>
      <span className="slot__body">
        <span className="slot__title">{r.title.title}{seasonsLabel(r.seasons) ? <span className="muted"> · {seasonsLabel(r.seasons)}</span> : null}</span>
        <span className="m-card__who"><Initial name={r.requester} /><span>{r.requester} · <b>{stageLabel(r.stage, r.title.kind)}</b></span></span>
        <Journey stage={r.stage} kind={r.title.kind} compact />
        {r.stuck.length ? (
          <span className="m-stuck"><CircleAlert size={15} aria-hidden /><span>{r.stuck.map((s) => <span key={s} className="m-stuck__item">{s}</span>)}</span></span>
        ) : line ? <span className="muted m-all__line">{line}</span> : null}
      </span>
    </button>
  );
}

/** A ticket in a request's history, in a sentence. */
function ticketLine(t: AdminTicket) {
  const head = t.opened_by ? `${t.opened_by} opened a ticket` : `${t.who ?? "They"} asked for help: ${t.reason}`;
  const note = t.note ? ` (“${t.note}”)` : "";
  const end = t.status === "resolved" ? `. Resolved by ${t.resolved_by ?? "an admin"}${t.reply ? `: ${t.reply}` : ""}` : ". Still open";
  return head + note + end;
}

/** One request in full, for an admin: where it is, who asked, its tickets, and the fixes. */
function RequestSheet({ id, onClose }: { id: string; onClose: () => void }) {
  const { refresh, toast, readOnly } = useManage();
  const { busy, act } = useAct();
  const [r, setR] = useState<AdminRequestDetail | null>(null);
  const [gone, setGone] = useState(false);
  const [writing, setWriting] = useState(false);
  const [note, setNote] = useState("");
  const [tell, setTell] = useState(false);
  const [message, setMessage] = useState("");
  const [, setParams] = useSearchParams();
  const titleId = useId();
  const load = useCallback(() => api.adminRequest(id).then(setR).catch(() => setGone(true)), [id]);
  useEffect(() => { void load(); }, [load]);

  const after = () => { window.setTimeout(() => { void load(); void refresh("all"); void refresh("help"); }, 900); };
  const search = async (how: "again" | "episodes" | "name") => {
    const out = await act(how, () => api.requestSearch(id, how), { failText: "Couldn’t search" });
    if (!out) return;
    toast({ text: { again: "Searching again", episodes: "Searching episode by episode", name: "Searching by name" }[how], detail: out.message });
    after();
  };
  const ticket = async () => {
    const out = await act("ticket", () => api.requestTicket(id, note.trim(), tell, tell ? message.trim() : ""), { failText: "Couldn’t open the ticket" });
    if (!out) return;
    buzz(12);
    toast({ text: "Ticket opened", detail: out.message });
    setWriting(false);
    setNote("");
    after();
  };

  const openTicket = r?.tickets.find((t) => t.status === "open");
  const video = r && ["tv", "movie"].includes(r.title.kind);
  return (
    <BottomSheet titleId={titleId} onClose={onClose} className="m-reqsheet"
      title={r ? <>{r.title.title}{seasonsLabel(r.seasons) ? <span className="muted"> · {seasonsLabel(r.seasons)}</span> : null}</> : "Request"}>
      {gone ? <p className="field__error">Couldn’t load this request.</p> : !r ? <p className="muted">Loading…</p> : (
        <div className="m-reqsheet__body">
          <p className="m-card__eyebrow">No. {formatSlot(r.slot)} · {KIND_LABEL[r.title.kind] ?? r.title.kind} · asked {since(r.requestedAt)} by {r.requester} · via {r.via}</p>
          <Journey stage={r.stage} kind={r.title.kind} />
          <LiveProgress stage={r.stage} progress={r.progress} />
          {r.stuck.length ? (
            <ul className="m-stuck m-stuck--list">{r.stuck.map((s) => <li key={s}><CircleAlert size={15} aria-hidden /> {s}</li>)}</ul>
          ) : null}
          <dl className="m-facts">
            {r.approvedAt ? <><dt>Approved</dt><dd>{since(r.approvedAt)}{r.approvedBy ? ` by ${r.approvedBy}` : ""}</dd></> : null}
            {r.stageSince && r.stage !== "available" ? <><dt>{stageLabel(r.stage, r.title.kind)}</dt><dd>since {since(r.stageSince)}</dd></> : null}
            {r.finishedAt ? <><dt>On Plex</dt><dd>since {since(r.finishedAt)}</dd></> : null}
            {r.seerrId ? <><dt>Seerr</dt><dd>request #{r.seerrId}</dd></> : null}
          </dl>

          {video ? (
            <div className="m-reqsheet__fixes">
              <button type="button" className="btn m-btn" disabled={readOnly || !!busy} onClick={() => void search("again")}>
                <RefreshCw size={16} aria-hidden className={busy === "again" ? "m-spin" : undefined} /> Search again
              </button>
              {r.title.kind === "tv" ? (
                <button type="button" className="btn btn--quiet m-btn" disabled={readOnly || !!busy} onClick={() => void search("episodes")}>
                  <ListOrdered size={16} aria-hidden /> Episode by episode
                </button>
              ) : null}
              <button type="button" className="btn btn--quiet m-btn" disabled={readOnly || !!busy} onClick={() => void search("name")}>
                <Search size={16} aria-hidden /> Search by name
              </button>
            </div>
          ) : null}

          {openTicket ? (
            <div className="m-reqsheet__ticket">
              <LifeBuoy size={16} aria-hidden />
              <span>There’s an open ticket on this: <b>{openTicket.reason}</b>.</span>
              <button type="button" className="btn m-btn" onClick={() => {
                onClose();
                setParams((p) => { p.set("tab", "tickets"); p.set("ticket", openTicket.id); return p; }, { replace: true });
              }}>Open the ticket</button>
            </div>
          ) : writing ? (
            <div className="m-help__reply">
              <label className="field__label" htmlFor={`${titleId}-note`}>What’s wrong, or what you’ve found</label>
              <textarea id={`${titleId}-note`} className="m-textarea" rows={3} maxLength={600} value={note} onChange={(e) => setNote(e.target.value)}
                placeholder="For example: the indexer had nothing, trying another release" />
              <label className="m-check">
                <input type="checkbox" checked={tell} onChange={(e) => {
                  setTell(e.target.checked);
                  if (e.target.checked && !message) setMessage(`An admin is looking into your request for ${r.title.title}. You’ll hear back when it’s sorted.`);
                }} />
                <span>Let {r.requester} know</span>
              </label>
              {tell ? (
                <>
                  <label className="field__label" htmlFor={`${titleId}-msg`}>Message to {r.requester}</label>
                  <textarea id={`${titleId}-msg`} className="m-textarea m-textarea--reply" rows={3} maxLength={600} value={message}
                    onChange={(e) => setMessage(e.target.value)} />
                  <span className="muted m-person__meta">Sent the usual way (a Discord DM, or an alert). They can answer it.</span>
                </>
              ) : null}
              <div className="m-person__actions m-person__actions--two">
                <button type="button" className="btn m-btn" onClick={() => { setWriting(false); setNote(""); }}>Back</button>
                <button type="button" className="btn btn--primary m-btn" disabled={readOnly || busy === "ticket" || !note.trim()} onClick={() => void ticket()}>
                  <LifeBuoy size={16} aria-hidden /> {busy === "ticket" ? "Opening…" : "Open the ticket"}
                </button>
              </div>
            </div>
          ) : (
            <button type="button" className="btn btn--primary m-btn m-reqsheet__open" disabled={readOnly} onClick={() => setWriting(true)}>
              <LifeBuoy size={16} aria-hidden /> Open a ticket
            </button>
          )}

          {r.tickets.length || r.activity.length ? (
            <div className="m-reqsheet__history">
              <h3>History</h3>
              <ul>
                {[...r.tickets.map((t) => ({ at: t.created_at ?? "", text: ticketLine(t) })),
                  ...r.activity.map((a) => ({ at: a.at, text: `${a.by}: ${a.did}` }))]
                  .sort((a, b) => b.at.localeCompare(a.at))
                  .map((e, n) => <li key={n}><span className="muted">{since(e.at)}</span> {e.text}</li>)}
              </ul>
            </div>
          ) : null}
        </div>
      )}
    </BottomSheet>
  );
}

/* ============================================================== tickets */

type TicketShow = "action" | "waiting" | "mine" | "solved" | "everything";

function ticketState(t: Pick<AdminTicketRow, "status" | "waiting" | "who">) {
  return t.status !== "open" ? { label: "Solved", tone: "done" } : t.waiting ? { label: `Waiting on ${t.who}`, tone: "wait" } : { label: "Needs an admin", tone: "hot" };
}

/**
 * Every ticket: what members asked for help with ("Something wrong?") and what admins
 * opened from a request. A ticket is worked on in one place: its timeline, notes only
 * admins see, replies to the member, the fixes, and its status and owner.
 */
function TicketsTab() {
  const { data, failed } = useManage();
  const { session } = useSession();
  const [params, setParams] = useSearchParams();
  const asked = params.get("ticket");
  const [open, setOpen] = useState<string | null>(asked);
  const [show, setShow] = useState<TicketShow>("action");
  const me = session?.user.name;
  const list = data.tickets;
  if (!list) return failed.tickets ? null : <p className="muted">Loading…</p>;
  const close = () => {
    setOpen(null);
    if (params.get("ticket")) setParams((p) => { p.delete("ticket"); return p; }, { replace: true });
  };
  const rows = list.rows.filter((t) =>
    show === "action" ? t.status === "open" && !t.waiting : show === "waiting" ? t.status === "open" && t.waiting
      : show === "mine" ? t.status === "open" && t.owner === me : show === "solved" ? t.status !== "open" : true);
  const mine = list.rows.filter((t) => t.status === "open" && t.owner === me).length;
  const options: { id: TicketShow; label: string }[] = [
    { id: "action", label: `Needs an admin · ${list.counts.action}` },
    { id: "waiting", label: `Waiting on them · ${list.counts.waiting}` },
    { id: "mine", label: `Mine · ${mine}` },
    { id: "solved", label: `Solved · ${list.counts.solved}` },
    { id: "everything", label: `Everything · ${list.rows.length}` },
  ];
  return (
    <section className="section m-tickets">
      <h2>Tickets</h2>
      <p className="muted m-all__lede">“Something wrong?” from members, and tickets admins open from a request. Open one to talk it through, fix it and solve it.</p>
      <div className="request-bar m-all__bar">
        <select className="select" value={show} aria-label="Show" onChange={(e) => setShow(e.target.value as TicketShow)}>
          {options.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
        </select>
      </div>
      {rows.length ? (
        <ul className="m-rows m-all__rows">
          {rows.map((t) => <li key={t.id}><TicketRow t={t} onOpen={() => setOpen(t.id)} /></li>)}
        </ul>
      ) : (
        <p className="muted">{show === "action" ? "Nothing needs an admin right now." : show === "waiting" ? "No ticket is waiting on a member."
          : show === "mine" ? "You haven’t taken any open tickets." : show === "solved" ? "Nothing solved yet." : "No tickets yet."}</p>
      )}
      {createPortal(<AnimatePresence>{open ? <TicketSheet key={open} id={open} onClose={close} /> : null}</AnimatePresence>, document.body)}
    </section>
  );
}

function TicketRow({ t, onOpen }: { t: AdminTicketRow; onOpen: () => void }) {
  const state = ticketState(t);
  return (
    <button type="button" className={`m-ticket is-${state.tone}`} onClick={onOpen}>
      <span className="m-card__eyebrow">No. {formatSlot(t.slot)} · {t.openedBy ? `opened by ${t.openedBy}` : `asked by ${t.who}`} · {since(t.updatedAt)}</span>
      <span className="m-ticket__title">{t.title}{seasonsLabel(t.seasons) ? <span className="muted"> · {seasonsLabel(t.seasons)}</span> : null}</span>
      <span className="m-ticket__reason">{t.reason}</span>
      {t.last ? <span className="m-ticket__last"><b>{t.last.by}:</b> {t.last.text}</span> : null}
      <span className="m-ticket__tags">
        <span className={`m-pill m-pill--${state.tone}`}>{state.label}</span>
        <span className="m-pill">{t.owner ? `${t.owner} has it` : "Nobody has it yet"}</span>
      </span>
    </button>
  );
}

const ENTRY_LABEL: Record<TicketEntry["kind"], string> = { member: "", note: "Admins only", reply: "Sent to them", action: "", status: "" };

/** One ticket, worked on in one place. */
function TicketSheet({ id, onClose }: { id: string; onClose: () => void }) {
  const { refresh, toast, readOnly } = useManage();
  const { session } = useSession();
  const { busy, act } = useAct();
  const [t, setT] = useState<AdminTicketDetail | null>(null);
  const [gone, setGone] = useState(false);
  const [kind, setKind] = useState<"note" | "reply">("note");
  const [text, setText] = useState("");
  const [solving, setSolving] = useState(false);
  const [last, setLast] = useState("");
  const titleId = useId();
  const load = useCallback(() => api.adminTicket(id).then(setT).catch(() => setGone(true)), [id]);
  useEffect(() => { void load(); }, [load]);
  const after = () => { void load(); window.setTimeout(() => { void refresh("tickets"); void refresh("all"); void refresh("help"); }, 600); };

  const run = async (key: string, call: () => Promise<Ack>, done: string) => {
    const out = await act(key, call);
    if (!out) return false;
    toast({ text: done, detail: out.message });
    after();
    return true;
  };
  const send = async () => {
    if (!text.trim()) return;
    if (await run("send", () => api.ticketComment(id, kind, text.trim()), kind === "reply" ? "Sent" : "Note added")) setText("");
  };
  const search = (how: "again" | "episodes" | "name") =>
    run(how, () => ({ again: api.helpSearch, episodes: api.helpEpisodes, name: api.helpByName }[how])(id),
      { again: "Searching again", episodes: "Searching episode by episode", name: "Searching by name" }[how]);

  if (gone) return <BottomSheet titleId={titleId} onClose={onClose} title="Ticket"><p className="field__error">Couldn’t load this ticket.</p></BottomSheet>;
  const state = t ? ticketState(t) : null;
  const video = t && ["tv", "movie"].includes(t.kind);
  const mine = t?.owner && t.owner === session?.user.name;
  return (
    <BottomSheet titleId={titleId} onClose={onClose} className="m-reqsheet m-ticketsheet"
      title={t ? <>{t.title}{seasonsLabel(t.seasons) ? <span className="muted"> · {seasonsLabel(t.seasons)}</span> : null}</> : "Ticket"}>
      {!t ? <p className="muted">Loading…</p> : (
        <div className="m-reqsheet__body">
          <p className="m-card__eyebrow">No. {formatSlot(t.slot)} · {t.reason} · {t.openedBy ? `opened by ${t.openedBy}` : `asked by ${t.who}`} {since(t.createdAt)}</p>
          <div className="m-ticket__tags">
            <span className={`m-pill m-pill--${state!.tone}`}>{state!.label}</span>
            <span className="m-pill">{t.owner ? `${t.owner} has it` : "Nobody has it yet"}</span>
            {t.status === "open" ? (
              <button type="button" className="btn btn--quiet m-btn m-ticket__take" disabled={readOnly || !!busy}
                onClick={() => void run("take", () => api.ticketTake(id), mine ? "Let go" : "It’s yours")}>
                {mine ? "Let it go" : t.owner ? "Take it over" : "Take it"}
              </button>
            ) : null}
          </div>

          {t.request ? (
            <div className="m-ticket__request">
              <Journey stage={t.request.stage} kind={t.request.title.kind} compact />
              <span className="muted">{stageLabel(t.request.stage, t.request.title.kind)}{rowLine(t.request) ? ` · ${rowLine(t.request)}` : ""}</span>
              {t.request.stuck.length ? (
                <span className="m-stuck"><CircleAlert size={15} aria-hidden /><span>{t.request.stuck.map((s) => <span key={s} className="m-stuck__item">{s}</span>)}</span></span>
              ) : null}
            </div>
          ) : null}

          {video && t.status === "open" ? (
            <div className="m-reqsheet__fixes">
              <button type="button" className="btn m-btn" disabled={readOnly || !!busy} onClick={() => void search("again")}>
                <RefreshCw size={16} aria-hidden className={busy === "again" ? "m-spin" : undefined} /> Search again
              </button>
              {t.kind === "tv" ? (
                <button type="button" className="btn btn--quiet m-btn" disabled={readOnly || !!busy} onClick={() => void search("episodes")}>
                  <ListOrdered size={16} aria-hidden /> Episode by episode
                </button>
              ) : null}
              <button type="button" className={`btn m-btn${t.offer === "name" ? "" : " btn--quiet"}`} disabled={readOnly || !!busy} onClick={() => void search("name")}>
                <Search size={16} aria-hidden /> Search by name
              </button>
            </div>
          ) : null}

          <ol className="m-thread" aria-label="Timeline">
            {t.thread.map((e) => (
              <li key={e.id} className={`m-thread__entry is-${e.kind}`}>
                {e.kind === "status" ? (
                  <span className="m-thread__status">{e.by}: {e.text} · {since(e.at)}</span>
                ) : (
                  <>
                    <span className="m-thread__head">
                      <b>{e.by}</b>{ENTRY_LABEL[e.kind] ? <span className="m-thread__tag">{e.kind === "reply" ? `Sent to ${t.who}` : ENTRY_LABEL[e.kind]}</span> : null}
                      <span className="muted">{since(e.at)}</span>
                    </span>
                    <span className="m-thread__text">{e.text}</span>
                  </>
                )}
              </li>
            ))}
          </ol>

          {t.status === "open" ? (
            <div className={`m-composer is-${kind}`}>
              <div className="m-seg" role="radiogroup" aria-label="Who sees it">
                <button type="button" role="radio" aria-checked={kind === "note"} className="m-seg__item" onClick={() => setKind("note")}>Note for admins</button>
                <button type="button" role="radio" aria-checked={kind === "reply"} className="m-seg__item" onClick={() => setKind("reply")}>Reply to {t.who}</button>
              </div>
              <label className="visually-hidden" htmlFor={`${titleId}-say`}>{kind === "note" ? "Note for admins" : `Reply to ${t.who}`}</label>
              <textarea id={`${titleId}-say`} className={`m-textarea${kind === "reply" ? " m-textarea--reply" : " m-textarea--note"}`} rows={3} maxLength={1200}
                value={text} onChange={(e) => setText(e.target.value)}
                placeholder={kind === "note" ? "Only admins see this" : `${t.who} gets this as a Discord DM or an alert, and can answer`} />
              <div className="m-person__actions m-person__actions--two">
                <button type="button" className="btn m-btn" disabled={readOnly || !!busy}
                  onClick={() => void run("status", () => api.ticketStatus(id, t.waiting ? "open" : "waiting"), t.waiting ? "Back with the admins" : `Waiting on ${t.who}`)}>
                  {t.waiting ? "Back to open" : `Wait on ${t.who}`}
                </button>
                <button type="button" className="btn btn--primary m-btn" disabled={readOnly || busy === "send" || !text.trim()} onClick={() => void send()}>
                  {busy === "send" ? "Sending…" : kind === "note" ? "Add note" : "Send reply"}
                </button>
              </div>
              {solving ? (
                <div className="m-help__reply">
                  <label className="field__label" htmlFor={`${titleId}-last`}>Last word to {t.who} <span className="muted">(optional)</span></label>
                  <textarea id={`${titleId}-last`} className="m-textarea m-textarea--reply" rows={2} maxLength={600} value={last}
                    onChange={(e) => setLast(e.target.value)} placeholder="Grabbed the 4K release, it’ll be on Plex tonight" />
                  <div className="m-person__actions m-person__actions--two">
                    <button type="button" className="btn m-btn" onClick={() => { setSolving(false); setLast(""); }}>Back</button>
                    <button type="button" className="btn btn--primary m-btn" disabled={readOnly || busy === "solve"}
                      onClick={async () => { if (await run("solve", () => api.ticketStatus(id, "resolved", last.trim()), "Solved")) { setSolving(false); setLast(""); } }}>
                      <Check size={16} aria-hidden /> {busy === "solve" ? "Solving…" : "Solve it"}
                    </button>
                  </div>
                </div>
              ) : (
                <button type="button" className="btn btn--quiet m-btn m-ticket__solve" disabled={readOnly} onClick={() => setSolving(true)}>
                  <Check size={16} aria-hidden /> Solve…
                </button>
              )}
            </div>
          ) : (
            <button type="button" className="btn m-btn" disabled={readOnly || !!busy} onClick={() => void run("reopen", () => api.ticketStatus(id, "open"), "Reopened")}>
              <RefreshCw size={16} aria-hidden /> Reopen
            </button>
          )}
        </div>
      )}
    </BottomSheet>
  );
}

/* ============================================================== overview */

/** What's waiting on an admin: the overview tiles and the tab badges both count from this. */
function waiting(data: Store) {
  return {
    requests: data.requests?.pending.length,
    tickets: data.tickets?.counts.action ?? 0,
    joins: data.joins?.filter((j) => j.status === "pending").length,
    leaving: data.cleanup?.warning.length,
    down: data.health?.filter((h) => !h.ok).length,
  };
}

function Overview({ go, failed }: { go: (t: Section) => void; failed: Partial<Record<Section, boolean>> }) {
  const { data } = useManage();
  const services = data.health;
  const { down = 0, joins, tickets } = waiting(data);
  // Pink and pulsing ("hot") only for what's wrong right now: a service down, or
  // someone asking for help. Waiting work gets a quiet dot, so the tiles still rank.
  const health = services === undefined
    ? failed.health
      ? { label: "Couldn’t check services", value: "?", hot: true, icon: <CircleAlert size={18} aria-hidden /> }
      : { label: "Services", value: undefined, hot: false, icon: <CircleDot size={18} aria-hidden /> }
    : !services.length
      ? { label: "No services set up", value: "–", hot: false, icon: <CircleDot size={18} aria-hidden /> }
      : down
        ? { label: "Services down", value: down, hot: true, icon: <CircleAlert size={18} aria-hidden /> }
        : { label: "Services up", value: `${services.length}/${services.length}`, hot: false, icon: <CircleCheck size={18} aria-hidden /> };
  const tiles: { id: Section; label: string; value: number | string | undefined; hot: boolean; waiting?: boolean; icon: ReactNode }[] = [
    { id: "requests", label: "Requests waiting", value: data.requests?.pending.length, hot: false, waiting: !!data.requests?.pending.length,
      icon: <Inbox size={18} aria-hidden /> },
    { id: "tickets", label: "Open tickets", value: data.tickets ? tickets : undefined, hot: !!tickets, icon: <LifeBuoy size={18} aria-hidden /> },
    { id: "joins", label: "Want to join", value: joins, hot: false, waiting: !!joins, icon: <UserPlus size={18} aria-hidden /> },
    { id: "cleanup", label: "Leaving this week", value: data.cleanup?.warning.length, hot: false, waiting: !!data.cleanup?.warning.length, icon: <Hourglass size={18} aria-hidden /> },
    { id: "health", ...health },
  ];
  return (
    <div className="m-overview">
      {tiles.map((t) => (
        <button key={t.id} type="button" className={`m-tile${t.hot ? " is-hot" : ""}`} onClick={() => go(t.id)}>
          <span className="m-tile__top">{t.icon}{t.hot || t.waiting ? <span className={`m-tile__dot${t.hot ? "" : " m-tile__dot--quiet"}`} aria-hidden /> : null}</span>
          <b className="m-tile__n">{t.value === undefined ? "–" : <Tick value={t.value} />}</b>
          <span className="m-tile__label">{t.label}</span>
        </button>
      ))}
    </div>
  );
}

/* ================================================================ toasts */

function ToastItem({ t, dismiss }: { t: Toast; dismiss: (id: number) => void }) {
  // The clock pauses while the tab is hidden, or while the pointer or keyboard
  // focus is on the toast (reaching Undo takes a moment), so nothing vanishes unread.
  const clock = useRef<{ pause: () => void; resume: () => void } | null>(null);
  useEffect(() => {
    let left = t.undo ? 6500 : 4200;
    let started = Date.now();
    let timer: number | undefined = window.setTimeout(() => dismiss(t.id), left);
    let holds = 0;
    const pause = () => {
      if (holds++ > 0) return;
      window.clearTimeout(timer);
      left -= Date.now() - started;
    };
    const resume = () => {
      if (--holds > 0) return;
      holds = 0;
      started = Date.now();
      timer = window.setTimeout(() => dismiss(t.id), Math.max(800, left));
    };
    clock.current = { pause, resume };
    const onVis = () => (document.hidden ? pause() : resume());
    document.addEventListener("visibilitychange", onVis);
    return () => { window.clearTimeout(timer); document.removeEventListener("visibilitychange", onVis); };
  }, [t, dismiss]);
  return (
    <motion.div
      layout
      className={`m-toast${t.tone === "error" ? " is-error" : ""}`}
      role={t.tone === "error" ? "alert" : undefined}
      onPointerEnter={() => clock.current?.pause()} onPointerLeave={() => clock.current?.resume()}
      onFocus={() => clock.current?.pause()} onBlur={() => clock.current?.resume()}
      initial={{ opacity: 0, y: 24, scale: 0.96 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: 12, scale: 0.97, transition: { duration: 0.16 } }}
      transition={{ type: "spring", duration: 0.4, bounce: 0.18 }}
      drag="x"
      dragSnapToOrigin
      onDragEnd={(_, i) => { if (Math.abs(i.offset.x) > 70 || Math.abs(i.velocity.x) > 500) dismiss(t.id); }}
    >
      {t.tone === "error" ? <CircleAlert size={18} aria-hidden /> : <Check size={18} aria-hidden />}
      <span className="m-toast__text"><b>{t.text}</b>{t.detail && t.detail !== t.text ? <small>{t.detail}</small> : null}</span>
      {t.undo ? (
        <button type="button" className="m-toast__undo" onClick={() => { t.undo!(); dismiss(t.id); }}>Undo</button>
      ) : null}
    </motion.div>
  );
}

/* ================================================================== page */

export function Manage() {
  const { session } = useSession();
  const [params, setParams] = useSearchParams();
  const asked = params.get("tab") ?? "";
  const tab: Section = (SECTIONS as string[]).includes(asked) ? (asked as Section) : "requests";
  useTitle(`Manage: ${TABS.find((t) => t.id === tab)?.label ?? "Overview"}`);
  const panel = useRef<HTMLDivElement>(null);
  const reduced = useReducedMotion();

  const [data, setData] = useState<Store>({});
  const [failed, setFailed] = useState<Partial<Record<Section, boolean>>>({});
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);

  const refresh = useCallback(async (s: Section) => {
    try {
      const d = await api.admin<unknown>(s);
      setData((prev) => ({ ...prev, [s]: d }));
      setFailed((f) => ({ ...f, [s]: false }));
    } catch {
      setFailed((f) => ({ ...f, [s]: true }));
    }
  }, []);
  const patch = useCallback(<K extends Section>(s: K, fn: (d: NonNullable<Store[K]>) => NonNullable<Store[K]>) => {
    setData((prev) => (prev[s] ? { ...prev, [s]: fn(prev[s] as NonNullable<Store[K]>) } : prev));
  }, []);
  const dismiss = useCallback((id: number) => setToasts((all) => all.filter((t) => t.id !== id)), []);
  const toast = useCallback((t: ToastIn) => setToasts((all) => {
    const rest = all.length && all[all.length - 1].text === t.text ? all.slice(0, -1) : all;
    return [...rest.slice(-2), { ...t, id: nextId.current++ }];
  }), []);

  const admin = !!session?.admin;
  useEffect(() => {
    if (!admin) return;
    SECTIONS.forEach((s) => void refresh(s));
    const t = window.setInterval(() => { if (!document.hidden) void refresh("health"); }, 60_000);
    return () => window.clearInterval(t);
  }, [admin, refresh]);

  if (!admin) {
    return <div className="shell page"><p className="muted">This page is for admins.</p></div>;
  }

  const go = (t: Section, scroll = false) => {
    setParams((p) => { p.set("tab", t); return p; }, { replace: true });
    if (scroll) panel.current?.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "start" });
  };
  const w = waiting(data);
  const counts: Partial<Record<Section, number>> = {
    requests: w.requests || undefined, all: data.all?.counts?.stuck || undefined, tickets: w.tickets || undefined,
    joins: w.joins, cleanup: w.leaving, health: w.down,
  };
  /** "All requests · 2 stuck"; everything else counts what's waiting. */
  const word = (t: Section) => (t === "all" ? "stuck" : t === "tickets" ? "open" : "waiting");
  const ctx: ManageState = { data, refresh, patch, toast, readOnly: !!session?.preview, failed };

  return (
    <ManageCtx.Provider value={ctx}>
      <div className="shell page m-page">
        <PageHead title="Manage" lede="Everything the admin commands do in Discord, in one place. Discord keeps working as before." />

        <Overview go={(t) => go(t, true)} failed={failed} />

        {/* The sections as one dropdown, like Request and Library; what's waiting elsewhere
            stays in sight beside it. */}
        <div className="m-tabbar request-bar" ref={panel} role="group" aria-label="Manage">
          <select className="select" value={tab} aria-label="Section" onChange={(e) => go(e.target.value as Section)}>
            {TABS.map((t) => (
              <option key={t.id} value={t.id}>{counts[t.id] ? `${t.label} · ${counts[t.id]} ${word(t.id)}` : t.label}</option>
            ))}
          </select>
          {TABS.filter((t) => t.id !== tab && counts[t.id]).map((t) => (
            <button key={t.id} type="button" className={`m-waiting${t.id === "health" || t.id === "cleanup" ? " is-hot" : ""}`}
              onClick={() => go(t.id)}>
              {t.label} <span className="m-badge"><Tick value={counts[t.id]!} /></span>
            </button>
          ))}
        </div>

        <div role="region" id="m-panel" aria-label={TABS.find((t) => t.id === tab)?.label ?? "Manage"}>
          {failed[tab] && !data[tab] ? (
            <div className="m-failed">
              <p className="field__error">Couldn’t load this. Plexbie may be restarting.</p>
              <button type="button" className="btn m-btn" onClick={() => void refresh(tab)}><RefreshCw size={16} aria-hidden /> Try again</button>
            </div>
          ) : (
            <motion.div
              key={tab}
              initial={reduced ? { opacity: 0 } : { opacity: 0, transform: "translateY(6px)" }}
              animate={reduced ? { opacity: 1 } : { opacity: 1, transform: "translateY(0px)" }}
              transition={{ duration: 0.2, ease: EASE_OUT }}
            >
              {tab === "requests" ? <RequestsTab /> : tab === "all" ? <AllRequestsTab /> : tab === "tickets" ? <TicketsTab /> : tab === "joins" ? <JoinsTab /> : tab === "invites" ? <InvitesTab /> : tab === "people" ? <PeopleTab /> : tab === "cleanup" ? <CleanupTab /> : tab === "discord" ? <DiscordTab /> : tab === "messages" ? <MessagesTab /> : <HealthTab />}
            </motion.div>
          )}
        </div>
      </div>
      {/* Always present, so screen readers hear each toast as it's added. */}
      <div className="m-toasts" role="status" aria-live="polite">
        <AnimatePresence initial={false}>
          {toasts.map((t) => <ToastItem key={t.id} t={t} dismiss={dismiss} />)}
        </AnimatePresence>
      </div>
    </ManageCtx.Provider>
  );
}
