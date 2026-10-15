import {
  Fragment, createContext, useCallback, useContext, useEffect, useId, useLayoutEffect, useRef, useState,
  type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type ReactNode,
} from "react";
import {
  AnimatePresence, LayoutGroup, animate, motion, useMotionValue, useMotionValueEvent, useReducedMotion, useTransform,
} from "motion/react";
import {
  Archive, ArrowLeft, Bell, Check, ChevronDown, ChevronUp, Download, LifeBuoy, CircleAlert, Copy, Hourglass, Link2, List, ListOrdered, Lock, LogOut, Mail, Megaphone, Menu, MessageCircle, MessageSquare, MonitorPlay, Radio, Plus, RefreshCw, Search, Server, Settings, Share2, ShieldCheck, Ticket, Trash2, UserPlus, Users, X,
} from "../components/icons";
import type { Icon } from "../components/icons";
import { createPortal } from "react-dom";
import { Link, useSearchParams } from "react-router-dom";
import { api, mayHaveWorked, type Ack } from "../api/client";
import type { ArrEpisode, ArrItem, BlockedChoice, BlockedPreview, BlockedRef, BlockedRow, AdminAllRequests, AdminRequestDetail, AdminRequestRow, AdminTicket, AdminTicketDetail, AdminTicketRow, AdminTickets, TicketEntry, AdminCleanup, AdminCleanupRow, CleanupMatch, AdminInvite, PlexInvite, CleanupSettings, DiscordOverview, LoggedMessage, MessageChannel, MessagePerson, AdminJoin, AdminPerson, AdminRequest, AdminRequests, HealthCheck, NewInvite } from "../api/types";
import { sendAgain } from "../api/alerts";
import { useSession } from "../components/Layout";
import { Mascot } from "../components/Mascot";
import { EASE_OUT, Journey, LiveProgress } from "../components/motion";
import { Art, KIND_LABEL, formatSlot, stageLabel, inDays, scrollBehavior, shortDate, since, useCopied, useLoad, useTitle } from "../components/ui";
import { afterOverlay, useBackToClose, useSheet } from "../components/overlay";
import { menuGroups, type MenuGroup, type MenuRow } from "./manageMenu";
import { AREAS, DESK_FEEDS, deriveDesk, listFor, seasonsWords, type Area, type Desk, type DeskData, type Feed, type Group, type Item } from "./manageQueue";
import { BottomSheet } from "../components/BottomSheet";

/* ================================================================== store */

type Section = "requests" | "all" | "tickets" | "joins" | "invites" | "plexinvites" | "people" | "cleanup" | "discord" | "messages" | "health" | "security";
type Store = {
  requests?: AdminRequests; all?: AdminAllRequests; tickets?: AdminTickets; joins?: AdminJoin[]; invites?: AdminInvite[]; plexinvites?: PlexInvite[]; people?: AdminPerson[]; cleanup?: AdminCleanup;
  discord?: DiscordOverview; messages?: MessagePerson[]; health?: HealthCheck[];
};
type ToastIn = {
  tone?: "ok" | "error"; text: string; detail?: string; undo?: () => void;
  /** Runs when the toast goes without its Undo pressed (timed out, swiped away, the tab hidden,
   *  the page left): a decision held back for Undo is sent then. */
  onGone?: () => void;
};
type Toast = ToastIn & { id: number };

const SECTIONS: Section[] = ["requests", "all", "tickets", "joins", "invites", "plexinvites", "people", "cleanup", "discord", "messages", "health"];
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
  { id: "security", label: "Sign-ins" },
];

interface ManageState {
  data: Store;
  refresh: (s: Section) => Promise<void>;
  patch: <K extends keyof Store>(s: K, fn: (d: NonNullable<Store[K]>) => NonNullable<Store[K]>) => void;
  toast: (t: ToastIn) => void;
  readOnly: boolean;
  failed: Partial<Record<Section, boolean>>;
  /** The invite link just made, shown on Invites until "Make another". */
  freshInvite: NewInvite | null;
  setFreshInvite: (made: NewInvite | null) => void;
  /** A decision from the queue, held back for Undo: its row leaves at once, and `run` sends it
   *  once the toast goes (Undo puts the row back and sends nothing). */
  defer: (key: string, text: string, run: () => Promise<unknown>) => void;
  /** The blocked imports the page loads with everything else (undefined until answered). */
  blocked: { rows?: BlockedRow[]; failed: boolean };
}
const ManageCtx = createContext<ManageState | null>(null);
/** What each tab shows (most show only their own section). */
const SHOWS: Partial<Record<Section, Section[]>> = { invites: ["invites", "plexinvites", "discord"], security: [] };
const shows = (t: Section) => SHOWS[t] ?? [t];
/** What the queue, the monitors and the menu count, kept fresh whichever view is open. */
const COUNTED: Section[] = ["requests", "all", "tickets", "joins", "invites", "people", "cleanup", "discord", "messages", "health"];
const useManage = () => useContext(ManageCtx)!;
/** The part of a full view the menu asked for (?part=keep): it's scrolled to, or opened. */
const usePart = () => useSearchParams()[0].get("part");

/** A tiny tap on phones that support it; silence everywhere else. */
function buzz(ms = 8) {
  try { navigator.vibrate?.(ms); } catch { /* not supported */ }
}

/** Runs an admin action and turns any failure into a Ack, so callers only branch once.
 *  `unsure`: no answer came back, so it may still have gone through. */
async function attempt(act: () => Promise<Ack>): Promise<Ack & { unsure?: boolean }> {
  try {
    return await act();
  } catch (e) {
    return { ok: false, message: e instanceof Error ? e.message : "That didn’t work.", unsure: mayHaveWorked(e) };
  }
}

/**
 * Admin actions, the same way everywhere: mark a row busy while it runs (`busy`
 * holds every key still running, so one finishing never frees another), and on
 * failure show the error (with `failText` as the headline when given, and to
 * `onFail` for a caller that also says it in place) and return null, so a caller
 * only handles success. With `unsure`, a failure that may still have gone through
 * (no answer came back) says that instead, under "No answer yet". `later`
 * refreshes a section after the bot has had a moment to act.
 */
function useAct() {
  const { toast, refresh } = useManage();
  const [busy, setBusy] = useState<ReadonlySet<string>>(() => new Set());
  const mark = (key: string, on: boolean) => setBusy((was) => {
    const now = new Set(was);
    if (on) now.add(key); else now.delete(key);
    return now;
  });
  const act = async (key: string | null, call: () => Promise<Ack>,
    opts: { failText?: string; unsure?: string; buzzOnFail?: boolean; onFail?: (message: string, unsure: boolean) => void } = {}) => {
    if (key !== null) mark(key, true);
    const out = await attempt(call);
    if (key !== null) mark(key, false);
    if (out.ok) return out;
    if (opts.buzzOnFail) buzz(30);
    if (out.unsure && opts.unsure) {
      opts.onFail?.(opts.unsure, true);
      toast({ tone: "error", text: "No answer yet", detail: opts.unsure });
      return null;
    }
    opts.onFail?.(out.message, false);
    toast(opts.failText ? { tone: "error", text: opts.failText, detail: out.message } : { tone: "error", text: out.message });
    return null;
  };
  const later = (section: Section, ms: number) => window.setTimeout(() => void refresh(section), ms);
  return { busy, act, later };
}

/**
 * Search as you type: asks once typing pauses for `ms`, and keeps only the
 * answer for the words in the box now, so a slow reply to "star" never
 * replaces the one for "star wars". Empty words clear the results; `found`
 * keeps the last answer on screen while `searching` says a newer one is on
 * its way. When `fetcher` fails, `failed` says so (never an empty answer,
 * which would read as "nothing found") and `retry` asks again.
 */
function useDebouncedSearch<T>(words: string, fetcher: (words: string) => Promise<T>, ms: number) {
  const [got, setGot] = useState<{ words: string; found: T | null } | null>(null);
  const [attempt, setAttempt] = useState(0);
  const fetchLatest = useRef(fetcher);
  useEffect(() => { fetchLatest.current = fetcher; });
  if (!words && got) setGot(null);
  useEffect(() => {
    if (!words) return;
    let current = true;
    const t = window.setTimeout(() => {
      void fetchLatest.current(words).then(
        (found) => { if (current) setGot({ words, found }); },
        () => { if (current) setGot({ words, found: null }); });
    }, ms);
    return () => { current = false; window.clearTimeout(t); };
  }, [words, ms, attempt]);
  const answered = !!words && got?.words === words;
  const retry = () => { setGot(null); setAttempt((a) => a + 1); };
  return { found: words && got ? got.found : null, searching: !!words && !answered, failed: answered && got.found === null, retry };
}

/**
 * Loads one thing and keeps what it last got: a reload that fails leaves that on
 * screen, with `failed` and when it was loaded (`at`), rather than swapping it for
 * an error, and an answer to an older ask is dropped. Loads again when `deps`
 * change, and on `reload`. For one thing per component: key it by what it loads.
 */
function useKept<T>(load: () => Promise<T>, deps: unknown[]) {
  const [state, setState] = useState<{ data?: T; at?: number; failed: boolean }>({ failed: false });
  const asks = useRef(0);
  const loadLatest = useRef(load);
  useEffect(() => { loadLatest.current = load; });
  const reload = useCallback(() => {
    const n = ++asks.current;
    return loadLatest.current().then(
      (data) => { if (n === asks.current) setState({ data, at: Date.now(), failed: false }); },
      () => { if (n === asks.current) setState((s) => ({ ...s, failed: true })); });
  }, []);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { void reload(); }, deps);
  return { ...state, reload };
}

/* ============================================================ primitives */

/**
 * A destructive action you have to mean: press and hold until the fill reaches
 * the end. Letting go early cancels. Keyboard users hold Space or Enter;
 * assistive tech that "clicks" without a press gets a two-step confirm instead,
 * said out loud when armed and disarmed again after a few seconds.
 */
function HoldButton({ label, icon, onConfirm, disabled, ms = 1000, stages, bail, quiet = false }: {
  label: string; icon?: ReactNode; onConfirm: () => void; disabled?: boolean; ms?: number;
  /** A hollow pill (Manage's queue rows), not a key. */
  quiet?: boolean;
  /** What the button says through the hold, evenly spaced (instead of "Keep holding…"). */
  stages?: string[];
  /** Shown for a moment after letting go early. */
  bail?: string;
}) {
  const [holding, setHolding] = useState(false);
  const [armed, setArmed] = useState(false);
  const [stage, setStage] = useState(0);
  const [bailed, setBailed] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  const ticker = useRef<number | undefined>(undefined);
  const bailTimer = useRef<number | undefined>(undefined);
  const armTimer = useRef<number | undefined>(undefined);
  const pressed = useRef(false);
  const self = useRef<HTMLButtonElement>(null);
  // The props as they are now: a hold's timer fires when the hold is done, and they may have changed by then.
  const latest = useRef({ onConfirm, disabled });
  useEffect(() => { latest.current = { onConfirm, disabled }; });

  // Armed by a "click" with no press: a stray one much later shouldn't still confirm.
  const arm = (on: boolean) => {
    window.clearTimeout(armTimer.current);
    setArmed(on);
    if (on) armTimer.current = window.setTimeout(() => setArmed(false), 4000);
  };

  // Confirming usually removes this card, and focus would drop to the page:
  // keep it nearby instead, on the next card or the section's heading.
  const confirm = () => {
    const card = self.current?.closest("li, article");
    const next = card?.nextElementSibling?.querySelector<HTMLElement>("button, a")
      ?? card?.previousElementSibling?.querySelector<HTMLElement>("button, a");
    const heading = self.current?.closest("section")?.querySelector<HTMLElement>("h2, h3");
    latest.current.onConfirm();
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
    setBailed(false);
    setStage(0);
    buzz(4);
    if (stages?.length) {
      let at = 0;
      ticker.current = window.setInterval(() => { at = Math.min(at + 1, stages.length - 1); setStage(at); buzz(3); }, ms / stages.length);
    }
    timer.current = window.setTimeout(() => {
      window.clearInterval(ticker.current);
      pressed.current = false;
      setHolding(false);
      if (latest.current.disabled) return;
      buzz(16);
      confirm();
    }, ms);
  };
  const cancel = () => {
    window.clearTimeout(timer.current);
    window.clearInterval(ticker.current);
    if (pressed.current && bail) {
      setBailed(true);
      window.clearTimeout(bailTimer.current);
      bailTimer.current = window.setTimeout(() => setBailed(false), 2200);
    }
    pressed.current = false;
    setHolding(false);
  };
  useEffect(() => () => {
    window.clearTimeout(timer.current); window.clearInterval(ticker.current); window.clearTimeout(bailTimer.current); window.clearTimeout(armTimer.current);
  }, []);
  const isKey = (e: ReactKeyboardEvent) => e.key === " " || e.key === "Enter";
  const text = armed ? "Again to confirm" : bailed && bail ? bail : `Hold to ${label.toLowerCase()}`;
  const hint = useId();

  return (
    <>
      <button
        ref={self}
        type="button"
        className={`btn${quiet ? " btn--quiet" : ""} m-btn m-hold${holding ? " is-holding" : ""}${armed ? " is-armed" : ""}`}
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
          if (armed) { arm(false); confirm(); } else arm(true);
        }}
        onBlur={() => { cancel(); arm(false); }}
        onContextMenu={(e) => e.preventDefault()}
      >
        <span className="m-hold__label">{icon}{holding && stages?.length ? stages[stage] : text}</span>
        <span id={hint} hidden>Press and hold, or press twice, to confirm.</span>
        <span className="m-hold__fill" aria-hidden>
          <span className="m-hold__label">{icon}{holding ? (stages?.[stage] ?? "Keep holding…") : text}</span>
        </span>
      </button>
      {/* Said out loud: a new name on the focused button often isn't. */}
      <span className="visually-hidden" role="status">{armed ? `Press again to confirm: ${label}` : ""}</span>
    </>
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

/** Try again, spinning (and not pressable twice) until the ask it starts has settled. */
function RetryButton({ onRetry, quiet = false }: { onRetry: () => unknown; quiet?: boolean }) {
  const [retrying, setRetrying] = useState(false);
  const retry = () => {
    setRetrying(true);
    void Promise.resolve(onRetry()).catch(() => undefined).finally(() => setRetrying(false));
  };
  return (
    <button type="button" className={`btn${quiet ? " btn--quiet" : ""} m-btn`} onClick={retry} disabled={retrying}>
      <RefreshCw size={16} aria-hidden className={retrying ? "m-spin" : undefined} /> Try again
    </button>
  );
}

/** A load failed with nothing to show: say so (never an empty list, which would be a false answer) and offer another go. */
function LoadFailed({ text, onRetry }: { text: string; onRetry: () => unknown }) {
  return (
    <div className="m-failed" role="alert">
      <p className="field__error">{text}</p>
      <RetryButton onRetry={onRetry} />
    </div>
  );
}

/** A refresh failed over what's already on screen: keep it, say how old it is, and offer another go. */
function Stale({ at, onRetry }: { at?: number; onRetry: () => unknown }) {
  const when = at ? new Date(at).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }) : null;
  return (
    <div className="m-stale" role="status">
      <CircleAlert size={16} aria-hidden />
      <span>Couldn’t refresh{when ? `, last updated ${when}` : ""}.</span>
      <RetryButton onRetry={onRetry} quiet />
    </div>
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
        // Only past the line, never on a quick flick: a hasty flick would decline
        // or deny without the hold their buttons ask for, and there's no undo.
        onPanEnd={enabled ? () => {
          const v = x.get();
          if (Math.abs(v) > line()) void commit(v > 0 ? 1 : -1);
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

/** Approve or decline a request: here, and from Manage's queue. */
function useDecideRequest() {
  const { patch, toast } = useManage();
  const { session } = useSession();
  const { busy, act, later } = useAct();
  const decide = async (r: AdminRequest, approve: boolean, quiet = false) => {
    const out = await act(r.id, () => api.decide("requests", r.id, approve), { buzzOnFail: true });
    if (!out) return false;
    buzz(12);
    if (!quiet) toast({ text: approve ? `Approved ${r.title}` : `Declined ${r.title}`, detail: out.message });
    patch("requests", (d) => ({
      ...d,
      pending: d.pending.filter((p) => p.id !== r.id),
      recent: [{ ...r, status: approve ? "approved" : "declined", resolvedBy: session?.user.name ?? "you", resolvedAt: new Date().toISOString() }, ...d.recent],
    }));
    later("requests", 2500);
    later("all", 2500);
    return true;
  };
  return { busy, decide };
}

function RequestsTab() {
  const { data, readOnly } = useManage();
  const part = usePart();
  const { busy, decide } = useDecideRequest();
  const res = data.requests;
  if (!res) {
    return (
      <div className="m-stack" aria-busy="true" aria-label="Loading requests">
        {[0, 1].map((i) => <div key={i} className="skeleton" style={{ height: 150, borderRadius: 16 }} />)}
      </div>
    );
  }
  const { pending, older, recent } = res;

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
                  enabled={!readOnly && !busy.has(r.id)}
                  yes={() => decide(r, true)} no={() => decide(r, false)} yesLabel="Approve" noLabel="Decline"
                >
                  <article className={`m-card${busy.has(r.id) ? " is-busy" : ""}`} aria-busy={busy.has(r.id)}>
                    <span className="m-card__poster"><Art title={{ poster: r.poster, title: r.title, kind: r.kind as never }} size="w185" /></span>
                    <div className="m-card__body">
                      <span className="m-card__eyebrow">No. {formatSlot(r.slot)} · {KIND_LABEL[r.kind as keyof typeof KIND_LABEL] ?? r.kind}</span>
                      <h3 className="m-card__title">{r.title}</h3>
                      {seasonsWords(r.seasons) ? <span className="m-chip">{seasonsWords(r.seasons)}</span> : null}
                      <span className="m-card__who"><Initial name={r.requester} /><span>{r.requester} asked {since(r.requestedAt)}</span></span>
                    </div>
                    <div className="m-card__actions">
                      <button type="button" className="btn m-btn m-btn--go" disabled={readOnly || busy.has(r.id)} onClick={() => void decide(r, true)}>
                        <Check size={18} aria-hidden /> {busy.has(r.id) ? "Working…" : "Approve"}
                      </button>
                      <HoldButton label="Decline" icon={<X size={16} aria-hidden />} disabled={readOnly || busy.has(r.id)} onConfirm={() => void decide(r, false)} />
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
        <details className="m-older" id="m-part-older" open={part === "older" || undefined}>
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

/** Invite someone who asked to join, or deny them: here, and from Manage's queue. */
function useDecideJoin() {
  const { patch, toast } = useManage();
  const { busy, act, later } = useAct();
  const decide = async (j: AdminJoin, approve: boolean, quiet = false) => {
    const out = await act(j.key, () => api.decide("joins", j.messageId, approve), { buzzOnFail: true });
    if (!out) return false;
    buzz(12);
    if (!quiet) toast({ text: approve ? `Invited ${j.name}` : `Denied ${j.name}`, detail: out.message });
    patch("joins", (d) => d.map((x) => (x.key === j.key ? { ...x, status: approve ? "approved" : "denied" } : x)));
    later("joins", 2500);
    return true;
  };
  return { busy, decide };
}

function JoinsTab() {
  const { data, readOnly } = useManage();
  const { busy, decide } = useDecideJoin();
  const rows = data.joins;
  if (!rows) return <div className="skeleton" style={{ height: 240 }} />;
  const pending = rows.filter((j) => j.status === "pending");
  const earlier = rows.filter((j) => j.status !== "pending");

  return (
    <div className="m-stack">
      <section className="section">
        <h2>Waiting to join <span className="m-count"><Tick value={pending.length} /></span></h2>
        {pending.some((j) => j.messageId) && !readOnly ? <SwipeHint /> : null}
        <ul className="m-cards">
          <AnimatePresence initial={false}>
            {pending.map((j) => {
              const can = !readOnly && !!j.messageId && !busy.has(j.key);
              return (
                <motion.li key={j.key} {...fold}>
                  <SwipeCard enabled={can} yes={() => decide(j, true)} no={() => decide(j, false)} yesLabel="Invite" noLabel="Deny">
                    <article className={`m-card m-card--person${busy.has(j.key) ? " is-busy" : ""}`} aria-busy={busy.has(j.key)}>
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
                              <UserPlus size={18} aria-hidden /> {busy.has(j.key) ? "Inviting…" : "Invite to Plex"}
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
                <span className={`m-pill m-pill--${j.status === "denied" ? "declined" : "approved"}`}>{j.status === "denied" ? "Denied" : "Invited"}</span>
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

/** "Never remove" on someone, with Undo: on People, and from Manage's queue. */
function useKeepPerson() {
  const { patch, toast } = useManage();
  const { act, later } = useAct();
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
  return keep;
}

function PeopleTab() {
  const { data, patch, toast, readOnly } = useManage();
  const { busy, act, later } = useAct();
  const keep = useKeepPerson();
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
                <article className={`m-person${busy.has(p.plexName) ? " is-busy" : ""}`}>
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
                          <button type="submit" className="btn btn--primary m-btn" disabled={readOnly || busy.has(p.plexName) || !newName.trim()}>Save</button>
                        </div>
                      </form>
                    ) : null}
                    {p.hasAccess === false && p.candidates?.length ? (
                      <MatchPicker person={p} disabled={readOnly || busy.has(p.plexName)} onMatch={(a) => void match(p, a)} />
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
                      <HoldButton label={p.hasAccess === false ? "Forget" : "Remove"} icon={<Trash2 size={16} aria-hidden />} ms={1400} disabled={readOnly || busy.has(p.plexName)} onConfirm={() => void remove(p)} />
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
  const res = useLoad(() => api.linkCandidates().then((r) => r.discord), []);
  const members = res.data ?? null;
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  // Straight into the search box with a mouse; on a phone that would open the
  // keyboard over the list of people you're meant to tap.
  const finePointer = typeof matchMedia === "function" && matchMedia("(pointer: fine)").matches;

  const q = query.trim().toLowerCase();
  const shown = (members ?? []).filter((m) => !q || `${m.name} ${m.username}`.toLowerCase().includes(q));

  return (
    <BottomSheet titleId="m-sheet-h" title={`Link ${person.plexName} to Discord`} onClose={onClose} className="m-sheet__panel--list">
      <p className="muted">Pick who this Plex account belongs to.{person.tracked === false ? " Linking also starts tracking them for inactivity." : ""}</p>
      <SearchField label="Find a Discord member" value={query} onChange={setQuery} className="m-sheet__search" autofocus={finePointer} />
      <ul className="m-sheet__list">
        {res.error ? <li><LoadFailed text="Couldn’t load the server’s members." onRetry={res.reload} /></li>
          : members === null ? <li className="muted">Loading members…</li> : null}
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

/** A number with big − and + buttons: easy on a phone, and typing still works.
 *  A typed number counts once you leave the field or press Enter, not mid-word. */
function Stepper({ id, label, value, min, max, step = 1, suffix, onChange, disabled }: {
  id: string; label: string; value: number; min: number; max: number; step?: number; suffix: string;
  onChange: (n: number) => void; disabled?: boolean;
}) {
  const clamp = (n: number) => Math.max(min, Math.min(max, Math.round(n)));
  const [draft, setDraft] = useState(String(value));
  // A new value from outside (a tap, a save, a refresh) replaces the draft.
  const [shown, setShown] = useState(value);
  if (value !== shown) { setShown(value); setDraft(String(value)); }
  const commit = () => {
    const n = Number(draft);
    const next = draft.trim() === "" || !Number.isFinite(n) ? value : clamp(n);
    setDraft(String(next));
    if (next !== value) onChange(next);
  };
  return (
    <div className="m-step">
      <label htmlFor={id} className="m-field-label">{label}</label>
      <div className="m-step__row">
        <button type="button" className="m-step__btn" aria-label={`${label}: fewer`} disabled={disabled || value <= min}
          onClick={() => { buzz(5); onChange(clamp(value - step)); }}>−</button>
        <input id={id} className="m-step__input" type="number" inputMode="numeric" min={min} max={max} value={draft} disabled={disabled}
          onChange={(e) => setDraft(e.target.value)} onBlur={commit}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); commit(); } }} />
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
 * (files really get deleted) is hold-to-confirm, and so are turning cleanup
 * on live while practice mode is off (a plain button turns it on in practice
 * instead) and a live scan.
 */
function CleanupSettingsCard({ settings, libraries, channels }: {
  settings: CleanupSettings; libraries: string[]; channels: { id: string; name: string }[];
}) {
  const { patch, refresh, toast, readOnly } = useManage();
  const { act, later } = useAct();
  const part = usePart();
  const [open, setOpen] = useState(part === "settings");
  const [days, setDays] = useState(settings.inactivityDays);
  const [warn, setWarn] = useState(settings.warnDaysBefore);
  const [scanning, setScanning] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  const reduced = useReducedMotion();
  const live = settings.enabled && !settings.practice;

  useEffect(() => { setDays(settings.inactivityDays); setWarn(settings.warnDaysBefore); }, [settings.inactivityDays, settings.warnDaysBefore]);

  const save = async (change: Partial<CleanupSettings>, quiet = false) => {
    // Shown straight away; a failure puts the old values back.
    const before = Object.fromEntries(Object.keys(change).map((k) => [k, settings[k as keyof CleanupSettings]])) as Partial<CleanupSettings>;
    patch("cleanup", (d) => ({ ...d, settings: { ...d.settings, ...change } }));
    const out = await act(null, () => api.cleanupSettings(change));
    if (!out) patch("cleanup", (d) => ({ ...d, settings: { ...d.settings, ...before } }));
    else if (!quiet) toast({ text: "Cleanup settings saved", detail: out.message });
    later("cleanup", 600);
  };
  // Leaving the tab before the numbers' pause is up still saves them.
  const pending = useRef<Partial<CleanupSettings> | null>(null);
  const saveLatest = useRef(save);
  useEffect(() => { saveLatest.current = save; });
  const saveNumbers = (nextDays: number, nextWarn: number) => {
    setDays(nextDays);
    setWarn(nextWarn);
    window.clearTimeout(timer.current);
    const change = { inactivityDays: nextDays, warnDaysBefore: Math.min(nextWarn, nextDays - 1) };
    pending.current = change;
    timer.current = window.setTimeout(() => { pending.current = null; void save(change); }, 900);
  };
  useEffect(() => () => {
    window.clearTimeout(timer.current);
    if (pending.current) void saveLatest.current(pending.current);
    pending.current = null;
  }, []);
  // A hold is swapped for another control in its row: focus moves there, not to the page.
  const enableRow = useRef<HTMLDivElement>(null);
  const modeRow = useRef<HTMLDivElement>(null);
  const focusRow = useRef<HTMLDivElement | null>(null);
  useEffect(() => { focusRow.current?.querySelector<HTMLElement>("button")?.focus(); focusRow.current = null; }, [settings.enabled, settings.practice]);

  const scan = async () => {
    setScanning(true);
    const out = await attempt(() => api.cleanupScan());
    setScanning(false);
    toast({ tone: out.ok ? "ok" : "error", text: out.ok ? "Cleanup scan finished" : "Scan didn’t run", detail: out.message });
    void refresh("cleanup");
  };

  return (
    <section className={`m-mode m-mode--card${live ? " is-live" : ""}`} id="m-part-settings">
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
              <div className="m-set-row" ref={enableRow}>
                <span><b>Cleanup</b><span className="muted">Check every day for titles nobody watches.{!settings.enabled && !settings.practice ? " Practice mode is off: turn it on in practice, or hold to go live." : ""}</span></span>
                {/* With practice off, turning it on is going live: the same hold as Go live, next to a plain way in through practice. */}
                {!settings.enabled && !settings.practice
                  ? <span className="m-set-row__actions">
                      <button type="button" className="btn m-btn" disabled={readOnly} onClick={() => { focusRow.current = enableRow.current; void save({ enabled: true, practice: true }); }}>Turn on in practice</button>
                      <HoldButton label="Turn on, live" ms={1400} disabled={readOnly} onConfirm={() => { focusRow.current = enableRow.current; void save({ enabled: true }); }} />
                    </span>
                  // Sends the mode on screen too, so a practice save that failed meanwhile can't make this go live.
                  : <Switch on={settings.enabled} label="Cleanup on" disabled={readOnly} onChange={(on) => void save(on ? { enabled: true, practice: settings.practice } : { enabled: false })} />}
              </div>

              <div className="m-set-row" ref={modeRow}>
                <span>
                  <b>{settings.practice ? "Practice mode" : "Live mode"}</b>
                  <span className="muted">{settings.practice ? "Reports what it would delete, deletes nothing." : "Really deletes the files from the server."}</span>
                </span>
                {settings.practice
                  ? <HoldButton label="Go live" ms={1400} disabled={readOnly || !settings.enabled} onConfirm={() => { focusRow.current = modeRow.current; void save({ practice: false }); }} />
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
                    {/* A skipped library Plex no longer has stays listed, so it can be let go: until then cleanup removes nothing. */}
                    {[...libraries, ...settings.excludedLibraries.filter((l) => !libraries.includes(l))].map((lib) => {
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
                <span><b>Run a scan now</b><span className="muted">{live ? "Live: anything whose warning has run out is deleted right away, with no daily limit." : "Same check the daily run does, without its daily limit."}</span></span>
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

/**
 * Keep any film or show, not only the ones on the clock: the same Plex title search
 * as /cleanup exempt add in Discord, without the libraries cleanup skips.
 */
function KeepSearch({ kept, onKeep }: { kept: Set<string>; onKeep: (m: CleanupMatch, on: boolean) => void }) {
  const { readOnly } = useManage();
  const [q, setQ] = useState("");
  // The read-only preview's server has no Plex search behind it.
  const words = !readOnly && q.trim().length >= 2 ? q.trim() : "";
  const { found, searching, failed, retry } = useDebouncedSearch(words, (w) => api.cleanupSearch(w), 300);
  return (
    <section className="section" id="m-part-keep">
      <h2>Keep a title forever</h2>
      <p className="muted">Search Plex for any film or show, even one nowhere near the clock, and cleanup will never touch it.</p>
      {readOnly
        ? <p className="muted m-actions__note">Switched off in the read-only preview.</p>
        : <SearchField label="Search Plex by title" value={q} onChange={setQ} className="m-keepsearch" />}
      {words && failed ? <LoadFailed text="Couldn’t search Plex just now." onRetry={retry} /> : words ? (
        <p className="muted m-all__count" role="status">
          {searching || !found ? "Searching…" : found.length ? `${found.length === 1 ? "1 title" : `${found.length} titles`} found` : "Nothing on Plex by that title."}
        </p>
      ) : null}
      {words && found?.length ? (
        <ul className="m-rows">
          {found.map((m) => {
            const on = kept.has(m.ratingKey);
            return (
              <li key={m.ratingKey}>
                <article className="m-leaving">
                  <span className={`m-ring ${on ? "is-kept" : "is-found"}`} aria-hidden>{on ? <ShieldCheck size={20} /> : <Hourglass size={20} />}</span>
                  <div className="m-leaving__body">
                    <h3 className="m-card__title">{m.title}</h3>
                    <span className="muted m-person__meta">{m.type === "show" ? "TV" : "Film"}{m.year ? ` · ${m.year}` : ""} · {on ? "never removed" : "cleanup can remove it"}</span>
                  </div>
                  <span className="m-keep">
                    <span aria-hidden>Keep</span>
                    <Switch on={on} label={`Keep ${m.title} forever`} disabled={readOnly} onChange={() => onKeep(m, !on)} />
                  </span>
                </article>
              </li>
            );
          })}
        </ul>
      ) : null}
    </section>
  );
}

/** Keep a title forever (or put it back on the clock), with Undo: in Cleanup, and from Manage's queue. */
function useKeepTitle() {
  const { patch, toast } = useManage();
  const { act, later } = useAct();
  const keep = async (row: { ratingKey: string; title: string; type?: string | null; year?: number | null }, on: boolean, quiet = false) => {
    // Move it straight away; the server confirms, and a failure puts it back.
    patch("cleanup", (d) => on
      ? {
        ...d,
        warning: d.warning.filter((c) => c.ratingKey !== row.ratingKey),
        upcoming: d.upcoming.filter((c) => c.ratingKey !== row.ratingKey),
        exempt: [{ ratingKey: row.ratingKey, title: row.title, type: row.type ?? null, year: row.year ?? null }, ...d.exempt.filter((e) => e.ratingKey !== row.ratingKey)],
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
  return keep;
}

function CleanupTab() {
  const { data, readOnly } = useManage();
  const keep = useKeepTitle();
  const [view, setView] = useState<CleanupView>("soon");
  const res = data.cleanup;
  if (!res) return <div className="skeleton" style={{ height: 300 }} />;
  const { settings, warning, upcoming, exempt } = res;

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
                        <span className="muted m-person__meta">{e.type === "show" ? "TV" : e.type === "movie" ? "Film" : e.type ?? "Title"}{e.year ? ` · ${e.year}` : ""} · never removed</span>
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
            {!exempt.length ? <AllClear title="Nothing kept forever yet">Flip “Keep” on any title, or search for one below, and cleanup will never touch it.</AllClear> : null}
          </>
        )}
      </section>

      <KeepSearch kept={new Set(exempt.map((e) => e.ratingKey))} onKeep={(m, on) => void keep(m, on)} />
    </div>
  );
}

/** What the import button says while it's held: a little ceremony, so nobody imports blind. */
const IMPORT_STAGES = ["Did you look at the files? 👀", "Episode numbers? Sizes? No .exe? 🧐", "Okay, okay. Going in 3…", "2…", "1…"];

/**
 * A download Sonarr or Radarr won't import by themselves (core/blocked_imports in the
 * bot): why, word for word, what's in it, what looks off, and the choices their own
 * Manual Import gives (which episode or film each file is, quality, language, release
 * group, or skip it). Then a long hold to import it. Nothing goes in blind.
 */
function BlockedImport({ target, onDone }: { target: BlockedRef; onDone?: () => void }) {
  const { toast, readOnly } = useManage();
  const { busy, act } = useAct();
  const [p, setP] = useState<BlockedPreview | null>(null);
  const [failed, setFailed] = useState("");
  const [done, setDone] = useState("");
  const [notDone, setNotDone] = useState("");
  /** The last import got no answer, so it may still be going in. */
  const [unsure, setUnsure] = useState(false);
  /** Bumped to look inside again, after an import that didn't go in (or may not have). */
  const [looks, setLooks] = useState(0);
  /** The admin's changes, by file name. */
  const [picks, setPicks] = useState<Record<string, BlockedChoice>>({});
  /** "Wrong show?": another series for every file, and its episodes. */
  const [series, setSeries] = useState<ArrItem | null>(null);
  const [eps, setEps] = useState<ArrEpisode[] | null>(null);
  const [epsFailed, setEpsFailed] = useState(false);
  /** Which "Wrong show?" pick the episodes being asked for belong to: a slower answer for an earlier one is dropped. */
  const epsAsked = useRef(0);
  const [finding, setFinding] = useState<string | null>(null);   // "series", or a file name for "Wrong film?"
  useEffect(() => {
    let live = true;
    api.blockedPreview(target.app, target.downloadId).then((x) => { if (live) setP(x); })
      .catch((e: unknown) => { if (live) setFailed(e instanceof Error ? e.message : "Couldn’t look inside it."); });
    return () => { live = false; };
  }, [target.app, target.downloadId, looks]);
  const app = target.app === "sonarr" ? "Sonarr" : "Radarr";
  const tv = target.app === "sonarr";
  /** Another show was picked and its episodes aren't here yet (or couldn't be had). */
  const epsWaiting = !!series && !eps;
  const episodes = series ? eps ?? [] : p?.options.episodes ?? [];
  const known = new Set(episodes.map((e) => e.id));
  const pick = (name: string, change: Partial<BlockedChoice>) => setPicks((x) => ({ ...x, [name]: { ...x[name], name, ...change } }));

  /** Each file as it stands with the admin's changes. */
  const files = (p?.files ?? []).map((f) => {
    const c = picks[f.name] ?? { name: f.name };
    const episodeIds = c.episodeIds ?? (series ? [] : f.episodes.map((e) => e.id));
    const movie = c.movieId ? { id: c.movieId, title: (c as BlockedChoice & { movieLabel?: string }).movieLabel } : f.movie;
    // With another show picked, only its own episodes count (Sonarr refuses any other).
    return { f, c, skip: !!c.skip, episodeIds, movie,
      placed: tv ? episodeIds.length > 0 && (!series || episodeIds.every((id) => known.has(id))) : !!movie?.id };
  });
  // Two files as one episode, or as one film (Radarr keeps one file per film: CD1 and CD2, or a sample).
  const used = new Map<number, number>();
  files.filter((x) => !x.skip).forEach((x) => (tv ? x.episodeIds : x.movie?.id ? [x.movie.id] : [])
    .forEach((id) => used.set(id, (used.get(id) ?? 0) + 1)));
  const doubled = [...used].filter(([, n]) => n > 1).map(([id]) => tv ? episodes.find((e) => e.id === id)?.label ?? "an episode"
    : files.find((x) => x.movie?.id === id)?.movie?.title ?? "the same film");
  const going = files.filter((x) => !x.skip);
  const unplaced = going.filter((x) => !x.placed);
  const blocker = !going.length ? "Every file is skipped." : epsWaiting ? `Pick the episodes once ${series.title}’s have loaded.` : unplaced.length ? `Pick which ${tv ? "episode" : "film"} ${unplaced[0].f.name} is, or skip it.`
    : doubled.length ? `Two files are set as ${doubled[0]}.` : "";

  const go = async () => {
    const choices: BlockedChoice[] = files.map(({ f, c, skip, episodeIds }) => ({
      ...c, name: f.name, ...(skip ? { skip: true } : {}),
      ...(tv ? { episodeIds, ...(series ? { seriesId: series.id } : {}) } : {}),
    }));
    setNotDone("");
    setUnsure(false);
    // Shown here too, not only in a toast: it stays beside the button while the admin decides
    // what next. The toast is the one that's announced. Either way, look inside again: what
    // was seen before may be gone or half imported, so the same import isn't offered twice.
    const out = await act("import", () => api.blockedImport(target.app, target.downloadId, choices), {
      failText: "Not imported",
      unsure: `${app} may still be importing it. Look in ${app} before trying again.`,
      onFail: (message, maybe) => { setNotDone(message); setUnsure(maybe); setP(null); setFailed(""); setLooks((n) => n + 1); },
    });
    if (!out) return;
    setDone(out.message);
    toast({ text: "Imported", detail: out.message });
    onDone?.();
  };
  const pickSeries = async (s: ArrItem) => {
    const asked = ++epsAsked.current;
    setFinding(null);
    setSeries(s);
    setPicks({});
    setEps(null);
    setEpsFailed(false);
    try {
      const { rows } = await api.arrEpisodes(s.id);
      if (asked === epsAsked.current) setEps(rows);
    } catch {
      if (asked === epsAsked.current) setEpsFailed(true);
    }
  };

  const notDoneLine = <p className="m-blocked__nope">{unsure ? "No answer yet" : "Not imported"}: {notDone}</p>;

  return (
    <section className="m-blocked" aria-label={`${app} won’t import this by itself`}>
      <p className="m-blocked__head"><CircleAlert size={16} aria-hidden /> {app} won’t import this by itself</p>
      {/* Until the look inside after a failed import is back (or if it fails), say what became of the import here. */}
      {notDone && !p ? notDoneLine : null}
      {failed ? <LoadFailed text={failed} onRetry={() => { setFailed(""); setLooks((n) => n + 1); }} />
        : !p ? <p className="muted">Looking inside…</p> : (
        <>
          {p.messages.map((m) => <p key={m} className="m-blocked__why">{app} says: “{m}”</p>)}
          <p className="m-blocked__advice">Look before you import. Usually it’s fine (a name {app} couldn’t match), but a blocked import can be the wrong episode, the wrong film, or something that shouldn’t be there. Check each file below is what {app} thinks it is.</p>
          {p.warnings.length ? (
            <ul className="m-blocked__warnings">{p.warnings.map((w) => <li key={w}><CircleAlert size={14} aria-hidden /> {w}</li>)}</ul>
          ) : null}

          {tv ? (
            <div className="m-blocked__owner">
              <span>Show: <b>{(series ?? p.series)?.title ?? "none"}</b>{(series ?? p.series)?.year ? ` (${(series ?? p.series)!.year})` : ""}</span>
              <button type="button" className="btn btn--quiet m-btn" onClick={() => setFinding(finding === "series" ? null : "series")}>Wrong show?</button>
              {finding === "series" ? <ArrFinder app="sonarr" onPick={(s) => void pickSeries(s)} /> : null}
              {epsWaiting ? (epsFailed
                ? <LoadFailed text={`Couldn’t get ${series.title}’s episodes from Sonarr.`} onRetry={() => void pickSeries(series)} />
                : <p className="muted">Loading episodes…</p>) : null}
            </div>
          ) : null}

          <ul className="m-blocked__files" aria-label="What’s in it">
            {files.map(({ f, c, skip, episodeIds, placed }) => (
              <li key={f.name} className={skip ? "is-other" : !placed || f.notes.length ? "is-odd" : undefined}>
                <span className="m-blocked__name">{f.name}</span>
                <span className="muted">{bytes(f.size)}</span>
                {f.rejections.map((r) => <span key={r} className="m-blocked__note">{app}: {r}</span>)}
                {f.notes.filter((n) => !f.rejections.includes(n) && !(placed && n.includes("can’t tell which") || placed && n.includes("can't tell which")))
                  .map((n) => <span key={n} className="m-blocked__note">{n}</span>)}
                {!skip ? (
                  <div className="m-blocked__edit">
                    {tv ? (
                      <>
                        {(episodeIds.length ? episodeIds : [0]).map((id, i) => (
                          <label key={i} className="m-blocked__field">
                            <span>{i ? "and" : "Episode"}</span>
                            <select className="m-select" value={id || ""} disabled={epsWaiting} onChange={(e) => {
                              const next = [...episodeIds]; const v = Number(e.target.value);
                              if (v) next[i] = v; else next.splice(i, 1);
                              pick(f.name, { episodeIds: next.filter(Boolean) });
                            }}>
                              <option value="">Pick one…</option>
                              {episodes.map((e) => <option key={e.id} value={e.id}>{e.label} · {e.title || "TBA"}{e.hasFile ? " (has a file)" : ""}</option>)}
                            </select>
                          </label>
                        ))}
                        {episodeIds.length ? (
                          <button type="button" className="btn btn--quiet m-btn m-blocked__more" disabled={epsWaiting} onClick={() => pick(f.name, { episodeIds: [...episodeIds, 0] })}>
                            + Another episode (a double)
                          </button>
                        ) : null}
                      </>
                    ) : (
                      <span className="m-blocked__field">
                        <span>Film</span>
                        <b>{c.movieId ? (c as BlockedChoice & { movieLabel?: string }).movieLabel : f.movie ? `${f.movie.title}${f.movie.year ? ` (${f.movie.year})` : ""}` : "none"}</b>
                        <button type="button" className="btn btn--quiet m-btn" onClick={() => setFinding(finding === f.name ? null : f.name)}>Wrong film?</button>
                      </span>
                    )}
                    {!tv && finding === f.name ? (
                      <ArrFinder app="radarr" onPick={(m) => { setFinding(null); pick(f.name, { movieId: m.id, movieLabel: `${m.title}${m.year ? ` (${m.year})` : ""}` } as Partial<BlockedChoice>); }} />
                    ) : null}
                    <details className="m-blocked__details">
                      <summary>Quality, language, release group</summary>
                      <label className="m-blocked__field"><span>Quality</span>
                        <select className="m-select" value={c.qualityId ?? f.qualityId ?? ""} onChange={(e) => pick(f.name, { qualityId: Number(e.target.value) })}>
                          {p.options.qualities.map((q) => <option key={q.id} value={q.id}>{q.name}</option>)}
                        </select>
                      </label>
                      <label className="m-blocked__field"><span>Language</span>
                        <select className="m-select" value={(c.languageIds ?? f.languages.map((l) => l.id))[0] ?? ""} onChange={(e) => pick(f.name, { languageIds: [Number(e.target.value)] })}>
                          {p.options.languages.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
                        </select>
                      </label>
                      <label className="m-blocked__field"><span>Release group</span>
                        <input className="m-select" value={c.releaseGroup ?? f.releaseGroup} maxLength={60} onChange={(e) => pick(f.name, { releaseGroup: e.target.value })} />
                      </label>
                    </details>
                  </div>
                ) : null}
                <label className="m-blocked__skip">
                  <input type="checkbox" checked={skip} onChange={(e) => pick(f.name, { skip: e.target.checked })} /> Don’t import this file
                </label>
              </li>
            ))}
            {p.others.map((o) => (
              <li key={o.name} className={o.danger ? "is-danger" : "is-other"}>
                <span className="m-blocked__name">{o.name}</span>
                <span className="muted">{bytes(o.size)} · {o.danger ? "a program, not a video" : "not imported"}</span>
              </li>
            ))}
          </ul>
          <p className="muted m-blocked__folder">In {p.folder}</p>
          {done ? <p className="m-blocked__done"><Check size={16} aria-hidden /> {done}</p> : !p.ok ? (
            <p className="m-blocked__nope">Not importable from here: sort it out in {app}{unsure ? "" : ", or delete the download"}.</p>
          ) : (
            <>
              {blocker ? <p className="m-blocked__nope">{blocker}</p> : null}
              {notDone ? notDoneLine : null}
              <HoldButton label="Import it" ms={3200} stages={IMPORT_STAGES} bail="Chickened out. Fair. 🐔"
                disabled={readOnly || busy.size > 0 || !!blocker} onConfirm={() => void go()} />
            </>
          )}
        </>
      )}
    </section>
  );
}

/** Search Sonarr's shows or Radarr's films by title, to say which one it really is. */
function ArrFinder({ app, onPick }: { app: "sonarr" | "radarr"; onPick: (item: ArrItem) => void }) {
  const [q, setQ] = useState("");
  const words = q.trim().length < 2 ? "" : q.trim();
  const { found: rows, searching, failed, retry } = useDebouncedSearch(words, (w) => api.arrLibrary(app, w).then((x) => x.rows), 250);
  return (
    <div className="m-blocked__finder">
      <label className="m-blocked__field m-blocked__findlabel">
        <span>{app === "sonarr" ? "Find the show in Sonarr" : "Find the film in Radarr"}</span>
        <span className="m-search m-search--field"><Search size={16} aria-hidden />
          <input type="search" className="m-select" autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="A title" /></span>
      </label>
      {searching ? <p className="muted">Searching…</p> : failed ? (
        <LoadFailed text={`Couldn’t search ${app === "sonarr" ? "Sonarr" : "Radarr"} just now.`} onRetry={retry} />
      ) : rows ? (rows.length ? (
        <ul>{rows.map((r) => <li key={r.id}><button type="button" className="btn btn--quiet m-btn" onClick={() => onPick(r)}>{r.title}{r.year ? ` (${r.year})` : ""}</button></li>)}</ul>
      ) : <p className="muted">Nothing in {app === "sonarr" ? "Sonarr" : "Radarr"} by that name.</p>) : null}
      <p className="visually-hidden" role="status">{!searching && !failed && rows ? (rows.length ? `${rows.length} ${rows.length === 1 ? "match" : "matches"}` : "No matches") : ""}</p>
    </div>
  );
}

function bytes(n: number): string {
  if (n >= 2 ** 30) return `${(n / 2 ** 30).toFixed(1)} GB`;
  if (n >= 2 ** 20) return `${Math.round(n / 2 ** 20)} MB`;
  return `${Math.max(1, Math.round(n / 1024))} KB`;
}

/** Manage → Health: every download waiting for an admin to look at it. */
function BlockedList({ checks }: { checks: number }) {
  const { data, blocked } = useManage();
  const [open, setOpen] = useState<string | null>(null);
  // Asked on opening Health and on "Check again" (`checks`), and with the services at most
  // every five minutes while the page stays open: each ask reads Sonarr's and Radarr's queues.
  const asked = useRef(0);
  const res = useKept(() => { asked.current = Date.now(); return api.adminBlocked(); }, [checks]);
  const load = res.reload;
  useEffect(() => { if (Date.now() - asked.current > 300_000) void load(); }, [data.health, load]);
  // What the page already has shows at once (so Services doesn't jump when this lands), then this view's own answer.
  const got = res.data ?? (blocked.rows ? { rows: blocked.rows, errors: {} as Partial<Record<BlockedRow["app"], string>> } : undefined);
  if (!got) return res.failed ? <section className="section"><LoadFailed text="Couldn’t check for blocked downloads." onRetry={load} /></section> : null;
  const rows: BlockedRow[] = got.rows;
  // Sonarr or Radarr not answering: a download it won't import by itself couldn't show here.
  const unreachable = (["sonarr", "radarr"] as const).filter((app) => got.errors?.[app]);
  if (!rows.length && !unreachable.length && !res.failed) return null;
  return (
    <section className="section" id="m-part-blocked">
      <div className="m-head"><h3>Waiting for you to look</h3></div>
      {res.failed ? <Stale at={res.at} onRetry={load} /> : null}
      {unreachable.map((app) => (
        <p key={app} className="field__error">Couldn’t reach {app === "sonarr" ? "Sonarr" : "Radarr"} just now, so a download it won’t import by itself can’t show here.</p>
      ))}
      {!rows.length ? null : <ul className="m-cards">
        {rows.map((r) => {
          const key = `${r.app}:${r.downloadId}`;
          return (
            <li key={key} className="m-card m-card--stack">
              <p className="m-card__eyebrow">{r.app === "sonarr" ? "Sonarr" : "Radarr"} · import blocked{r.episodes.length ? ` · ${r.episodes.join(", ")}` : ""}</p>
              <h3 className="m-card__title">{r.title}{r.year ? <span className="muted"> ({r.year})</span> : null}</h3>
              {open === key ? <BlockedImport target={r} onDone={() => window.setTimeout(() => void load(), 1500)} /> : (
                <button type="button" className="btn m-btn" onClick={() => setOpen(key)}>Look inside</button>
              )}
            </li>
          );
        })}
      </ul>}
    </section>
  );
}

/** Manage → Health: for a sign-in that wasn't an admin's own (the sign-in alerts point
 *  here), every website and app sign-in but this one ends. Disruptive, so held. */
function SignOutOthers() {
  const { toast, readOnly } = useManage();
  const { session } = useSession();
  const { busy, act } = useAct();
  const go = async () => {
    let ended = 0;
    const out = await act("sign-out", async () => {
      const done = await api.signOutOthers();
      ended = done.ended;
      return done;
    }, { failText: "Sign-out didn’t finish" });
    if (!out) return;
    // Alerts were turned off everywhere; this browser's own come back now.
    if (session) void sendAgain(session.user.id).catch(() => undefined);
    toast({ text: "Signed out everywhere else",
      detail: `${ended === 1 ? "1 app sign-in" : `${ended} app sign-ins`} ended, and every other website sign-in, with the alerts they had on. You’re still signed in here.` });
  };
  return (
    <section className="section" id="m-part-security">
      <div className="m-head"><h3>Sign out everywhere else</h3></div>
      <div className="m-set-row">
        <span><b>Sign out every other session</b><span className="muted">For a sign-in that wasn’t you: every website and app sign-in ends except this one, alerts turned on elsewhere stop, and the rest of the household signs in again.</span></span>
        <HoldButton label="Sign out others" icon={<LogOut size={16} aria-hidden />} ms={1400} disabled={readOnly || busy.has("sign-out")} onConfirm={() => void go()} />
      </div>
    </section>
  );
}

function HealthTab() {
  const { data, refresh } = useManage();
  const [spinning, setSpinning] = useState(false);
  const [checks, setChecks] = useState(0);
  const rows = data.health;
  if (!rows) return <div className="skeleton" style={{ height: 200 }} />;
  const again = async () => {
    setSpinning(true);
    setChecks((c) => c + 1);
    await refresh("health");
    setSpinning(false);
  };
  return (
    <>
    <BlockedList checks={checks} />
    <section className="section m-services">
      <div className="m-head">
        <h3>Services</h3>
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
              <b>{h.name} <span className="m-health__state">{h.ok ? "Up" : "Check failing"}</span></b>
              <span className="muted">{h.ok ? `Answering in ${h.ms} ms` : h.detail ?? "Not answering"}</span>
              {h.ok ? <span className="m-health__bar" aria-hidden><span style={{ transform: `scaleX(${Math.min(1, Math.max(0.04, h.ms / 600))})` }} /></span> : null}
            </div>
          </li>
        ))}
      </ul>
    </section>
    </>
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
      initial={{ opacity: 0, transform: "translateY(8px)" }}
      animate={{ opacity: 1, transform: "translateY(0px)" }}
      transition={{ duration: 0.24, ease: EASE_OUT }}
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

/** Take back a Plex invite nobody accepted: under Waiting on Plex, and from Manage's queue. */
function useCancelPlexInvite() {
  const { patch, toast } = useManage();
  const { busy, act } = useAct();
  const cancel = async (i: PlexInvite, quiet = false) => {
    const out = await act(i.email, () => api.plexInviteCancel(i.email));
    if (!out) return false;
    if (!quiet) toast({ text: out.message });
    patch("plexinvites", (d) => d.filter((x) => x.email !== i.email));
    return true;
  };
  return { busy, cancel };
}

/** Cancel an open invite link: under Invites, and from Manage's queue. */
function useRevokeInvite() {
  const { patch, toast } = useManage();
  const { busy, act, later } = useAct();
  const revoke = async (i: AdminInvite, quiet = false) => {
    const out = await act(i.id, () => api.revokeInvite(i.id));
    if (!out) return false;
    if (!quiet) toast({ text: `Cancelled ${i.label}’s invite`, detail: out.message });
    patch("invites", (d) => d.map((x) => (x.id === i.id ? { ...x, status: "revoked" } : x)));
    later("invites", 1500);
    return true;
  };
  return { busy, revoke };
}

/** Invites sent on plex.tv that nobody has accepted: fix a wrong address, or take one back. */
function PlexInvites() {
  const { data, patch, toast, readOnly } = useManage();
  const rows = data.plexinvites;
  const [editing, setEditing] = useState<string | null>(null);
  const [next, setNext] = useState("");
  const { busy, act, later } = useAct();
  const { busy: cancelling, cancel } = useCancelPlexInvite();
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

  return (
    <section className="section" id="m-part-plex">
      <h2>Waiting on Plex <span className="m-count"><Tick value={rows.length} /></span></h2>
      <p className="muted">Plex invites nobody has accepted yet. Sent to the wrong address? Change it and the invite goes to the right one.</p>
      <ul className="m-rows">
        <AnimatePresence initial={false}>
          {rows.map((i) => (
            // An invite to a Plex username has no email, so the name and time tell it apart.
            <motion.li key={i.email || `${i.name}|${i.sentAt}`} {...fold}>
              <article className="m-person">
                <Initial name={i.who || i.email || i.name} big />
                <div className="m-person__body">
                  <h3 className="m-card__title">{i.who || i.email || i.name || "A Plex invite"}</h3>
                  <span className="muted m-person__meta">{i.who && i.email ? `${i.email} · ` : ""}sent {since(i.sentAt)}</span>
                  {i.email ? null : <span className="m-person__note">Sent to a Plex username, so change or cancel it on plex.tv.</span>}
                </div>
                {!i.email || editing === i.email ? null : (
                  <div className="m-person__actions m-person__actions--two">
                    <button type="button" className="btn m-btn" disabled={readOnly} onClick={() => { setEditing(i.email); setNext(""); }}>
                      <Mail size={16} aria-hidden /> Change email
                    </button>
                    <HoldButton label="Cancel" icon={<X size={16} aria-hidden />} ms={800} disabled={readOnly || busy.has(i.email) || cancelling.has(i.email)} onConfirm={() => void cancel(i)} />
                  </div>
                )}
                  {i.email && editing === i.email ? (
                    <form className="m-plex-invite__edit" onSubmit={(e) => { e.preventDefault(); void change(i); }}>
                      <div className="field">
                      <label htmlFor={`pi-${i.email}`}>The right email</label>
                      <input id={`pi-${i.email}`} type="email" inputMode="email" autoComplete="off" autoFocus
                        value={next} onChange={(e) => setNext(e.target.value)} placeholder="their Plex email" />
                      </div>
                      <div className="m-person__actions m-person__actions--two">
                        <button type="button" className="btn m-btn" onClick={() => { setEditing(null); setNext(""); }}>Back</button>
                        <button type="submit" className="btn btn--primary m-btn" disabled={readOnly || busy.has(i.email) || !next.trim()}>
                          <Mail size={16} aria-hidden /> {busy.has(i.email) ? "Sending…" : "Send invite"}
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
  const { data, patch, toast, readOnly, freshInvite: made, setFreshInvite: setMade } = useManage();
  const { busy: acting, act, later } = useAct();
  const [label, setLabel] = useState("");
  const [email, setEmail] = useState("");
  const [days, setDays] = useState(7);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
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

  const { busy: revoking, revoke } = useRevokeInvite();

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
    const out = await act(i.id, () => api.deleteInvite(i.id));
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

      <section className="section" id="m-part-links">
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
                    <HoldButton label="Cancel" icon={<X size={16} aria-hidden />} ms={800} disabled={readOnly || acting.has(i.id) || revoking.has(i.id)} onConfirm={() => void revoke(i)} />
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
                        <span className={`m-pill m-pill--${i.status === "used" ? "approved" : "declined"}`}>{i.status === "revoked" ? "Cancelled" : i.status === "used" ? "Used" : "Expired"}</span>
                      </h3>
                      <span className="muted m-person__meta">
                        {i.status === "used" && i.usedBy ? `Joined as ${i.usedBy} ${i.usedAt ? since(i.usedAt) : ""}` : `Made by ${i.createdBy ?? "an admin"} ${since(i.createdAt)}`}
                        {i.email ? ` · for ${i.email}` : ""}
                      </span>
                    </div>
                    <div className={`m-person__actions${i.status === "used" ? "" : " m-person__actions--two"}`}>
                      {renewButton(i, i.status === "used" ? "Invite again" : "New link")}
                      {i.status === "used" ? null : (
                        <HoldButton label="Delete" icon={<Trash2 size={16} aria-hidden />} ms={700} disabled={readOnly || acting.has(i.id)} onConfirm={() => void remove(i)} />
                      )}
                    </div>
                  </article>
                </motion.li>
              ))}
            </AnimatePresence>
          </ul>
        </section>
      ) : null}

      <WhoBroughtWhom />
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

  return (
    <div className="m-stack">
      <section className="section" id="m-part-party">
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

      <section className="section" id="m-part-dms">
        <h2>DMs to Plexbie</h2>
        <p className="muted">When someone DMs Plexbie, admins get an alert and it lands in Messages, and in their own thread under the admin channel. Reply from Messages, or in the thread with Reply or <code>/reply</code>.</p>
        {d.inbox ? (
          <>
            <div className="m-set-row">
              <span><b>Answer DMs automatically</b><span className="muted">On someone’s first DM in 12 hours: “Thanks! The admins have your message and will reply here.”</span></span>
              <Switch on={d.inbox.autoreply} label="Answer DMs automatically" disabled={readOnly}
                onChange={async (on) => {
                  const out = await act(null, () => api.inboxSettings(on), { failText: "Not saved" });
                  if (out) { toast({ text: out.message }); void refresh("discord"); }
                }} />
            </div>
            {d.inbox.threadsMissing ? (
              <p className="m-stuck"><CircleAlert size={15} aria-hidden /><span>Plexbie can’t make threads in the admin channel, so DMs are posted in the channel itself. Give Plexbie’s role {d.inbox.threadsMissing} there.</span></p>
            ) : null}
          </>
        ) : null}
      </section>

      <section className="section" id="m-part-say">
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

    </div>
  );
}


/** Who brought whom: the Discord invites and Plexbie invite links people joined with. */
function WhoBroughtWhom() {
  const { data } = useManage();
  const d = data.discord;
  const [query, setQuery] = useState("");
  if (!d) return null;
  const q = query.trim().toLowerCase();
  const joins = q ? d.joins.filter((j) => `${j.who} ${j.by} ${j.code ?? ""}`.toLowerCase().includes(q)) : d.joins;
  return (
      <section className="section" id="m-part-who">
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
function Conversation({ person, onBack, inPane = false, reply = 0 }: {
  person: MessagePerson; onBack: () => void;
  /** In Manage's detail pane: no Back of its own, and the page isn't scrolled to its end. */
  inPane?: boolean;
  /** A new value puts the cursor in the reply box (Reply on a queue row). */
  reply?: number;
}) {
  const { refresh, toast, readOnly } = useManage();
  const { session } = useSession();
  const { busy, act } = useAct();
  // The send's own flag; `sendingNow` also stops a second send from the shortcut before a re-render.
  const [sending, setSending] = useState(false);
  const sendingNow = useRef(false);
  const [text, setText] = useState("");
  const [empty, setEmpty] = useState(false);
  // Loaded again when something new arrives (the page refreshes the people every minute).
  const res = useKept<LoggedMessage[]>(() => api.conversation(person.id), [person.last.at]);
  const reload = () => { void res.reload(); void refresh("messages"); };
  const me = session?.user.name ?? "you";
  const discord = person.id.startsWith("d");
  const send = async (e: React.FormEvent) => {
    e.preventDefault();
    // One send at a time: the shortcut skips the button's guards, so check here too.
    const body = text.trim();
    if (!body) { setEmpty(true); box.current?.focus(); return; }
    if (readOnly || sendingNow.current) return;
    sendingNow.current = true;
    setSending(true);
    const out = await act(null, () => api.messageReply(person.id, body), { failText: "Not sent" });
    sendingNow.current = false;
    setSending(false);
    if (!out) return;
    buzz(12);
    toast({ text: "Sent as Plexbie", detail: out.message });
    setText("");
    reload();
  };
  const done = async (yes: boolean) => {
    const out = await act("done", () => api.messageDone(person.id, yes));
    if (out) { toast({ text: yes ? "Marked done" : "Marked unread" }); reload(); }
  };
  const toTicket = async (key: string) => {
    const out = await act(key, () => api.messageToTicket(key), { failText: "Not added" });
    if (out) { toast({ text: "Added to their ticket", detail: out.message }); reload(); void refresh("tickets"); }
  };
  // On a phone the conversation covers the list: Back returns to the list, not off the page.
  useBackToClose(!inPane, onBack);
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => { if (!inPane) end.current?.scrollIntoView({ block: "end" }); }, [res.data, inPane]);
  const box = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { if (reply) box.current?.focus(); }, [reply]);
  let lastDay = "";
  return (
    <section className="m-chat" aria-label={`Messages with ${person.name}`}>
      <header className="m-chat__head">
        {inPane ? null : <button type="button" className="m-chat__back" onClick={onBack} aria-label="Back to everyone"><ArrowLeft size={20} aria-hidden /></button>}
        <Initial name={person.name} big />
        <div><h3 className="m-card__title">{person.name}</h3>
          <span className="muted m-person__meta">{person.count} message{person.count === 1 ? "" : "s"} from Plexbie{person.received ? ` · ${person.received} from ${person.name}` : ""} · by {person.via.map((v) => VIA[v].label.toLowerCase()).join(", ")}</span>
          {person.done ? <span className="muted m-person__meta">Marked done{person.done.by ? ` by ${person.done.by}` : ""} {since(person.done.at)}</span> : null}</div>
        <button type="button" className="btn btn--quiet m-btn m-chat__done" disabled={readOnly || busy.has("done")}
          onClick={() => void done(!!person.unread || !person.done)}>
          {person.unread || !person.done ? <><Check size={16} aria-hidden /> Done</> : "Mark unread"}
        </button>
      </header>
      <div className="m-chat__body">
        {res.data ? (res.failed ? <Stale at={res.at} onRetry={res.reload} /> : null)
          : res.failed ? <LoadFailed text={`Couldn’t load the messages with ${person.name}.`} onRetry={res.reload} />
          : <div className="skeleton" style={{ height: 160 }} />}
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
                  {m.by ? <span className="muted m-msg__by">Sent by {m.by}</span> : null}
                  {m.direction === "in" && m.ticket ? <span className="muted m-msg__by"><LifeBuoy size={13} aria-hidden /> On their ticket</span> : null}
                  {m.direction === "in" && !m.ticket && person.ticket && m.context === "Discord DM" ? (
                    <button type="button" className="btn btn--quiet m-btn m-msg__action" disabled={readOnly || busy.has(m.id)} onClick={() => void toTicket(m.id)}>
                      <LifeBuoy size={14} aria-hidden /> Add to their {person.ticket.subject === "general" ? "general ticket" : `ticket on ${person.ticket.title}`}
                    </button>
                  ) : null}
                </div>
              </article>
            </div>
          );
        })}
        {res.data && !res.data.length ? <p className="muted">No messages kept for {person.name}.</p> : null}
        <div ref={end} />
      </div>
      <form className="m-chat__composer" onSubmit={send}>
        <label className="field__label" htmlFor={`m-reply-${person.id}`}>Message to {person.name}</label>
        <textarea ref={box} id={`m-reply-${person.id}`} className="m-textarea m-textarea--reply" rows={2} maxLength={1500} value={text}
          readOnly={sending} onChange={(e) => { setText(e.target.value); setEmpty(false); }} placeholder={`Message ${person.name} as Plexbie`}
          onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !e.repeat) void send(e); }} />
        <div className="m-chat__send">
          <span className="muted m-person__meta">{discord ? "A Discord DM from Plexbie" : "A phone alert, else an email"}, signed “— {me} (admin)”</span>
          <button type="submit" className="btn btn--primary m-btn" disabled={readOnly || sending}>
            {sending ? "Sending…" : "Send as Plexbie"}
          </button>
        </div>
        {empty ? <p className="field__error" role="alert">Write the message first.</p> : null}
      </form>
    </section>
  );
}

/** Everything Plexbie has said to people, by Discord DM, website alert or email. */
function MessagesTab() {
  const { data } = useManage();
  const [params, setParams] = useSearchParams();
  // An alert about a DM opens straight on that conversation (?who=d123).
  const [open, setOpen] = useState<string | null>(params.get("who"));
  // Taken off the address once read, before the conversation adds its Back entry
  // (a later replace would drop that entry's marker and leave an extra Back press).
  const whoDropped = useRef(false);
  useLayoutEffect(() => {
    if (whoDropped.current || !params.get("who")) return;
    whoDropped.current = true;
    setParams((p) => { p.delete("who"); return p; }, { replace: true });
  }, [params, setParams]);
  const [query, setQuery] = useState("");
  const people = data.messages;
  if (!people) return <div className="skeleton" style={{ height: 300 }} />;
  const q = query.trim().toLowerCase();
  const shown = q ? people.filter((p) => p.name.toLowerCase().includes(q)) : people;
  const current = people.find((p) => p.id === open) ?? null;
  return (
    <section className="section">
      <div className="m-head m-head--end">
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
                  <span className="m-inbox__top"><b>{p.name}</b>{p.unread ? <span className="m-pill m-pill--hot">{p.unread} new</span> : null}<time dateTime={p.last.at}>{since(p.last.at)}</time></span>
                  <span className="m-inbox__last">{p.last.direction === "in" ? <b>{p.name.split(" ")[0]}: </b> : null}{p.last.text}</span>
                  <span className="m-inbox__via">
                    {p.via.map((v) => { const Icon = VIA[v].icon; return <Icon key={v} size={13} role="img" aria-label={VIA[v].label} />; })}
                    {p.failed ? <span className="m-inbox__failed">{p.failed} not delivered</span> : null}
                  </span>
                </span>
              </button>
            </li>
          ))}
          {!shown.length ? <li className="muted" style={{ padding: 12 }}>{q ? "Nobody matches." : "No messages yet."}</li> : null}
        </ul>
        {/* Keyed by person, so a draft or a send in progress never carries over to the next one. */}
        {current ? <Conversation key={current.id} person={current} onBack={() => setOpen(null)} /> : (
          <div className="m-inbox__empty muted">Pick someone to see their messages with Plexbie.</div>
        )}
      </div>
    </section>
  );
}


/* ========================================================== needs help */

/* ========================================================= all requests */

type Show = "progress" | "stuck" | "waiting" | "finished" | "declined" | "everything";
const SHOW: { id: Show; label: string }[] = [
  { id: "everything", label: "Every request" }, { id: "progress", label: "On its way" }, { id: "stuck", label: "Looks stuck" },
  { id: "waiting", label: "Waiting for a decision" }, { id: "finished", label: "On Plex" }, { id: "declined", label: "Declined" },
];
const ENDED = ["declined", "closed"];
const showing = (show: Show, r: AdminRequestRow) =>
  show === "progress" ? !["available", "requested", ...ENDED].includes(r.stage) : show === "stuck" ? r.stuck.length > 0
    : show === "waiting" ? r.stage === "requested" : show === "finished" ? r.stage === "available" : show === "declined" ? ENDED.includes(r.stage) : true;

/** How many rows All requests puts on screen at a time. */
const PAGE = 50;

/**
 * Every request from everyone (waiting, approved, declined, on Plex), where it is now, and
 * which look stuck. It opens on the last 30 days (and anything still on its way); "Every
 * request since No. 0001" loads the lot, and a search reaches any request ever. A row
 * opens the request in full, with the fixes and "Open a ticket".
 */
function AllRequestsTab() {
  const { data, failed } = useManage();
  const [show, setShow] = useState<Show>(() => ((data.all?.counts?.stuck ?? 0) > 0 ? "stuck" : "everything"));
  // Every request since No. 0001, loaded when asked for (the list opens on the last 30 days),
  // and again each time the page refreshes the last 30 days, so it's never older than them.
  const [everything, setEverything] = useState(false);
  const [history, setHistory] = useState<AdminAllRequests | null>(null);
  const [historyFailed, setHistoryFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!everything) return;
    let current = true;
    api.adminAll("", true).then(
      (h) => { if (current) { setHistory(h); setHistoryFailed(false); } },
      () => { if (current) setHistoryFailed(true); });
    return () => { current = false; };
  }, [everything, data.all, attempt]);
  const loadAll = () => { setEverything(true); setShow("everything"); };
  const loadAllAgain = () => { setHistoryFailed(false); setAttempt((a) => a + 1); };
  const backToRecent = () => { setEverything(false); setHistory(null); setHistoryFailed(false); };
  const loadingAll = everything && !history && !historyFailed;
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<string | null>(null);

  // Searching asks the bot (it reaches every request ever, not only the ones listed).
  const words = q.trim();
  const { found, searching, failed: searchFailed, retry } = useDebouncedSearch(words, (w) => api.adminAll(w), 300);
  // Rows go on screen a page at a time (every request since No. 0001 can be thousands),
  // starting again from the first page whenever the list asked for changes.
  const view = `${show}|${words}|${everything}`;
  const [paged, setPaged] = useState({ view, limit: PAGE });
  if (paged.view !== view) setPaged({ view, limit: PAGE });
  const limit = paged.view === view ? paged.limit : PAGE;
  const list = useRef<HTMLUListElement>(null);
  // Focus goes to the first row added, so the keyboard carries on where the list did.
  const more = () => {
    const from = limit;
    setPaged({ view, limit: from + PAGE });
    requestAnimationFrame(() => list.current?.children[from]?.querySelector("button")?.focus());
  };

  const recent = data.all;
  if (!recent) return failed.all ? null : <p className="muted">Loading…</p>;
  const all = history ?? recent;
  const counts = all.counts ?? { active: 0, stuck: 0, finished: 0 };
  const count = (id: Show) => id === "progress" ? counts.active : id === "stuck" ? counts.stuck : id === "waiting" ? counts.waiting ?? 0
    : id === "finished" ? counts.finished : id === "declined" ? counts.declined ?? 0 : all.rows.length;
  const shown = found ? found.rows : words ? [] : all.rows.filter((r) => showing(show, r));

  return (
    <section className="section m-all">
      <p className="muted m-all__lede">
        Every request from everyone, waiting, approved or declined, and where each one is now. {history ? "Showing every request since No. 0001." : "Showing the last 30 days, and anything still on its way."}
        {" "}Open one to see it in full, search again, or open a ticket.
      </p>
      <div className="request-bar m-all__bar">
        <label className="finder__field m-all__search">
          <Search size={18} aria-hidden />
          <span className="visually-hidden">Search every request</span>
          <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search every request: a title, a name or a number"
            autoComplete="off" enterKeyHint="search" />
        </label>
        {!words ? (
          <select className="select" value={show} aria-label="Show" onChange={(e) => setShow(e.target.value as Show)}>
            {SHOW.map((s) => <option key={s.id} value={s.id}>{`${s.label} · ${count(s.id)}`}</option>)}
          </select>
        ) : null}
      </div>
      {words && searchFailed ? <LoadFailed text="Couldn’t search just now." onRetry={retry} /> : words ? (
        <p className="muted m-all__count" role="status">
          {searching || !found ? "Searching…" : `${found.rows.length === 0 ? "Nothing" : found.rows.length === 1 ? "1 request" : `${found.rows.length} requests`} found`}
        </p>
      ) : null}
      {shown.length ? (
        <ul className="m-rows m-all__rows" ref={list}>
          {shown.slice(0, limit).map((r) => <li key={r.id}><AllRequestRow r={r} onOpen={() => setOpen(r.id!)} /></li>)}
        </ul>
      ) : !words ? (
        <p className="muted">{show === "stuck" ? "Nothing looks stuck right now." : show === "finished" ? `Nothing reached Plex ${history ? "yet" : "in the last 30 days"}.`
          : show === "waiting" ? "Nothing is waiting for a decision." : show === "declined" ? `Nothing declined${history ? "" : " in the last 30 days"}.`
          : show === "progress" ? "Nothing on its way right now." : "No requests yet."}</p>
      ) : null}
      {shown.length > limit ? (
        <div className="m-all__more">
          <button type="button" className="btn m-btn" onClick={more}>
            Show {Math.min(PAGE, shown.length - limit)} more · {shown.length - limit} not shown
          </button>
        </div>
      ) : null}
      {!words ? (
        <div className="m-all__more">
          {history && historyFailed ? <Stale onRetry={loadAllAgain} /> : null}
          {history ? (
            <button type="button" className="btn btn--quiet m-btn" onClick={backToRecent}>Back to the last 30 days</button>
          ) : historyFailed ? (
            <LoadFailed text="Couldn’t load every request." onRetry={loadAllAgain} />
          ) : (
            <button type="button" className="btn m-btn" disabled={loadingAll} onClick={loadAll}>
              {loadingAll ? "Loading every request…" : `Every request since No. 0001${recent.total ? ` · ${recent.total}` : ""}`}
            </button>
          )}
        </div>
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
        <span className="slot__title">{r.title.title}{seasonsWords(r.seasons) ? <span className="muted"> · {seasonsWords(r.seasons)}</span> : null}</span>
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

/** The ways a request's search can be sent off again, and what the toast says for each. */
type SearchHow = "again" | "episodes" | "name";
const SEARCHING: Record<SearchHow, string> = { again: "Searching again", episodes: "Searching episode by episode", name: "Searching by name" };

/** Ways to send a film or show's search off again: on a request, and on its ticket. `byName` makes "Search by name" the one to try. */
function RequestFixes({ kind, busy, disabled, byName = false, onSearch }: {
  kind: string; busy: ReadonlySet<string>; disabled: boolean; byName?: boolean; onSearch: (how: SearchHow) => void;
}) {
  return (
    <div className="m-reqsheet__fixes">
      <button type="button" className="btn m-btn" disabled={disabled || busy.size > 0} onClick={() => onSearch("again")}>
        <RefreshCw size={16} aria-hidden className={busy.has("again") ? "m-spin" : undefined} /> Search again
      </button>
      {kind === "tv" ? (
        <button type="button" className="btn btn--quiet m-btn" disabled={disabled || busy.size > 0} onClick={() => onSearch("episodes")}>
          <ListOrdered size={16} aria-hidden /> Episode by episode
        </button>
      ) : null}
      <button type="button" className={`btn m-btn${byName ? "" : " btn--quiet"}`} disabled={disabled || busy.size > 0} onClick={() => onSearch("name")}>
        <Search size={16} aria-hidden /> Search by name
      </button>
    </div>
  );
}

/** One request in full, for an admin: where it is, who asked, its tickets, and the fixes.
 *  In a sheet (All requests) or in Manage's detail pane; `head` says the line about who asked. */
function RequestBody({ id, res, head = true, onLeave, quiet = false }: {
  id: string; res: ReturnType<typeof useKept<AdminRequestDetail>>; head?: boolean;
  /** "Open a ticket" isn't the main thing here (the pane, where Approve is). */
  quiet?: boolean;
  /** Called before going to the request's open ticket (a sheet closes first). */
  onLeave?: () => void;
}) {
  const { refresh, toast, readOnly } = useManage();
  const { busy, act } = useAct();
  const r = res.data ?? null;
  const [writing, setWriting] = useState(false);
  const [note, setNote] = useState("");
  const [tell, setTell] = useState(false);
  const [message, setMessage] = useState("");
  const [empty, setEmpty] = useState(false);
  const [, setParams] = useSearchParams();
  const titleId = useId();
  const load = res.reload;

  const after = () => { window.setTimeout(() => { void load(); void refresh("all"); void refresh("tickets"); }, 900); };
  const search = async (how: SearchHow) => {
    const out = await act(how, () => api.requestSearch(id, how), { failText: "Couldn’t search" });
    if (!out) return;
    toast({ text: SEARCHING[how], detail: out.message });
    after();
  };
  const ticket = async () => {
    if (!note.trim()) { setEmpty(true); document.getElementById(`${titleId}-note`)?.focus(); return; }
    const out = await act("ticket", () => api.requestTicket(id, note.trim(), tell, tell ? message.trim() : ""), { failText: "Couldn’t open the ticket" });
    if (!out) return;
    buzz(12);
    toast(out.told === false ? { tone: "error", text: "Not delivered", detail: out.message } : { text: "Ticket opened", detail: out.message });
    setWriting(false);
    setNote("");
    after();
  };

  if (!r) return res.failed ? <LoadFailed text="Couldn’t load this request." onRetry={load} /> : <p className="muted">Loading…</p>;
  const openTicket = r.tickets.find((t) => t.status === "open");
  // Only an approved request can be searched for (not one waiting for a decision, or declined).
  const video = ["tv", "movie"].includes(r.title.kind) && !["requested", ...ENDED].includes(r.stage);
  return (
    <div className="m-reqsheet__body">
      {res.failed ? <Stale at={res.at} onRetry={load} /> : null}
      {head ? <p className="m-ticket__line"><span className="caps">No. {formatSlot(r.slot)}</span> · {KIND_LABEL[r.title.kind] ?? r.title.kind} · asked {since(r.requestedAt)} by {r.requester} · via {r.via}</p> : null}
      <Journey stage={r.stage} kind={r.title.kind} />
      <LiveProgress stage={r.stage} progress={r.progress} />
      {r.stuck.length ? (
        <ul className="m-stuck m-stuck--list">{r.stuck.map((s) => <li key={s}><CircleAlert size={15} aria-hidden /> {s}</li>)}</ul>
      ) : null}
      <dl className="m-facts">
        {!head ? <><dt>Asked</dt><dd>{since(r.requestedAt)} by {r.requester}, via {r.via}</dd></> : null}
        {r.approvedAt ? <><dt>Approved</dt><dd>{since(r.approvedAt)}{r.approvedBy ? ` by ${r.approvedBy}` : ""}</dd></> : null}
        {r.stageSince && r.stage !== "available" ? <><dt>{stageLabel(r.stage, r.title.kind)}</dt><dd>since {since(r.stageSince)}</dd></> : null}
        {r.finishedAt ? <><dt>On Plex</dt><dd>since {since(r.finishedAt)}</dd></> : null}
        {r.seerrId ? <><dt>Seerr</dt><dd>request #{r.seerrId}</dd></> : null}
      </dl>

      {video ? <RequestFixes kind={r.title.kind} busy={busy} disabled={readOnly} onSearch={(how) => void search(how)} /> : null}

      {openTicket ? (
        <div className="m-reqsheet__ticket">
          <LifeBuoy size={16} aria-hidden />
          <span>There’s an open ticket on this: <b>{openTicket.reason}</b>.</span>
          <button type="button" className="btn m-btn" onClick={() => {
            onLeave?.();
            afterOverlay(() => setParams((p) => { p.set("tab", "tickets"); p.set("ticket", openTicket.id); return p; }, { replace: true }));
          }}>Open the ticket</button>
        </div>
      ) : writing ? (
        <div className="m-help__reply">
          <label className="field__label" htmlFor={`${titleId}-note`}>What’s wrong, or what you’ve found</label>
          <textarea id={`${titleId}-note`} className="m-textarea" rows={3} maxLength={600} value={note} onChange={(e) => { setNote(e.target.value); setEmpty(false); }}
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
            <button type="button" className="btn btn--primary m-btn" disabled={readOnly || busy.has("ticket")} onClick={() => void ticket()}>
              <LifeBuoy size={16} aria-hidden /> {busy.has("ticket") ? "Opening…" : "Open the ticket"}
            </button>
          </div>
          {empty ? <p className="field__error" role="alert">Say what’s wrong first: it’s the ticket’s first note.</p> : null}
        </div>
      ) : (
        <button type="button" className={`btn${quiet ? " btn--quiet" : " btn--primary"} m-btn m-reqsheet__open`} disabled={readOnly} onClick={() => setWriting(true)}>
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
  );
}

function RequestSheet({ id, onClose }: { id: string; onClose: () => void }) {
  const res = useKept<AdminRequestDetail>(() => api.adminRequest(id), [id]);
  const r = res.data ?? null;
  const titleId = useId();
  return (
    <BottomSheet titleId={titleId} onClose={onClose} className="m-reqsheet"
      title={r ? <>{r.title.title}{seasonsWords(r.seasons) ? <span className="muted"> · {seasonsWords(r.seasons)}</span> : null}</> : "Request"}>
      <RequestBody id={id} res={res} onLeave={onClose} />
    </BottomSheet>
  );
}

/* ============================================================== tickets */

type TicketShow = "action" | "waiting" | "mine" | "solved" | "everything";

function ticketState(t: Pick<AdminTicketRow, "status" | "waiting" | "who">) {
  return t.status !== "open" ? { label: "Solved", tone: "done" } : t.waiting ? { label: `Waiting on ${t.who}`, tone: "wait" } : { label: "Needs an admin", tone: "hot" };
}

/** What a ticket is about, as its eyebrow puts it: its request's number, "Title" or "General". */
function ticketKind(t: Pick<AdminTicketRow, "subject" | "slot">) {
  return t.subject === "general" ? "General" : t.subject === "title" ? "Title" : `No. ${formatSlot(t.slot)}`;
}

/** Its heading: the film or show (a title ticket's with its year), or "General ticket". */
function ticketTitle(t: Pick<AdminTicketRow, "subject" | "title" | "year">) {
  if (t.subject === "general" || !t.title) return "General ticket";
  return t.subject === "title" && t.year ? `${t.title} (${t.year})` : t.title;
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
    // Once the sheet's own history entry is off, or replacing would overwrite it and leave an extra Back.
    if (params.get("ticket")) afterOverlay(() => setParams((p) => { p.delete("ticket"); return p; }, { replace: true }));
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
      <p className="muted m-all__lede">“Something wrong?” from members, on a request, a film or show, or anything else, and tickets admins open from a request. Open one to talk it through, fix it and solve it.</p>
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
      <span className="m-ticket__line"><span className="caps">{ticketKind(t)}</span> · {t.openedBy ? `opened by ${t.openedBy}` : `asked by ${t.who}`} · {since(t.updatedAt)}</span>
      <span className="m-ticket__title">{ticketTitle(t)}{seasonsWords(t.seasons) ? <span className="muted"> · {seasonsWords(t.seasons)}</span> : null}</span>
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

/** One ticket, worked on in one place: in a sheet (Tickets), or in Manage's detail pane,
 *  where the blocked import waits behind "Look inside" (`blockedRow`) and `head` is left to
 *  the pane. A new `solve` asks for Solve… (the E key). */
function TicketBody({ id, res, head = true, blockedRow, solve = 0, lead }: {
  id: string; res: ReturnType<typeof useKept<AdminTicketDetail>>; head?: boolean; blockedRow?: BlockedRow; solve?: number;
  /** In Manage's detail pane: the pill beside the main key (Open the full ticket), the pair right under the header. */
  lead?: ReactNode;
}) {
  const { refresh, toast, readOnly } = useManage();
  const { session } = useSession();
  const { busy, act } = useAct();
  const t = res.data ?? null;
  const [kind, setKind] = useState<"note" | "reply">("note");
  const [text, setText] = useState("");
  const [solving, setSolving] = useState(false);
  const [last, setLast] = useState("");
  const [looking, setLooking] = useState(false);
  const [empty, setEmpty] = useState(false);
  const box = useRef<HTMLTextAreaElement>(null);
  const titleId = useId();
  const load = res.reload;
  const after = () => { void load(); window.setTimeout(() => { void refresh("tickets"); void refresh("all"); }, 600); };
  // E asks for Solve… on the ticket shown.
  const [asked, setAsked] = useState(solve);
  if (solve !== asked) { setAsked(solve); if (solve && t?.status === "open") setSolving(true); }

  const run = async (key: string, call: () => Promise<Ack>, done: string) => {
    const out = await act(key, call);
    if (!out) return false;
    toast(out.told === false ? { tone: "error", text: "Not delivered", detail: out.message } : { text: done, detail: out.message });
    after();
    return true;
  };
  const send = async () => {
    // An empty box says so, rather than a key that does nothing.
    if (!text.trim()) { setEmpty(true); box.current?.focus(); return; }
    if (await run("send", () => api.ticketComment(id, kind, text.trim()), kind === "reply" ? "Sent" : "Note added")) setText("");
  };
  const search = (how: SearchHow) =>
    run(how, () => ({ again: api.helpSearch, episodes: api.helpEpisodes, name: api.helpByName }[how])(id), SEARCHING[how]);

  if (!t) return res.failed ? <LoadFailed text="Couldn’t load this ticket." onRetry={load} /> : <p className="muted">Loading…</p>;
  const state = ticketState(t);
  // A title ticket is searched for by its own TMDB id; a general one has nothing to search for.
  const video = ["tv", "movie"].includes(t.kind) && (t.subject !== "title" || !!t.tmdb);
  const mine = t.owner && t.owner === session?.user.name;
  // Leading the pane, Take it is the one pink key; beside Look inside (or in a sheet) it's a quiet pill.
  const lookLeads = !!(lead && blockedRow && !looking && t.blocked && t.status === "open");
  const take = (
    <button type="button" className={`btn${lead && !lookLeads ? " btn--primary" : " btn--quiet"} m-btn m-ticket__take`} disabled={readOnly || busy.size > 0}
      onClick={() => void run("take", () => api.ticketTake(id), mine ? "Let go" : "It’s yours")}>
      {mine ? "Let it go" : t.owner ? "Take it over" : "Take it"}
    </button>
  );
  const reopen = (
    <button type="button" className="btn m-btn" disabled={readOnly || busy.size > 0} onClick={() => void run("reopen", () => api.ticketStatus(id, "open"), "Reopened")}>
      <RefreshCw size={16} aria-hidden /> Reopen
    </button>
  );
  const blockedPart = t.blocked && t.status === "open" ? (
    blockedRow && !looking
      ? <BlockedSummary row={blockedRow} onLook={() => setLooking(true)} keyed={!lead} />
      : <BlockedImport target={t.blocked} onDone={after} />
  ) : null;
  // In the pane a blocked import's Look inside is the main key, and Take it joins the chips.
  const lookKey = lookLeads
    ? <button type="button" className="btn btn--primary m-btn" onClick={() => setLooking(true)}><Search size={18} aria-hidden /> Look inside</button> : null;
  return (
    <div className="m-reqsheet__body">
      {lead ? <div className="m-pane__acts">{lookKey ?? (t.status === "open" ? take : reopen)}{lead}</div> : null}
      {res.failed ? <Stale at={res.at} onRetry={load} /> : null}
      {head ? <p className="m-ticket__line"><span className="caps">{ticketKind(t)}</span> · {t.reason} · {t.openedBy ? `opened by ${t.openedBy}` : `asked by ${t.who}`} {since(t.createdAt)}</p> : null}
      <div className="m-ticket__tags">
        <span className={`m-pill m-pill--${state.tone}`}>{state.label}</span>
        <span className="m-pill">{t.owner ? `${t.owner} has it` : "Nobody has it yet"}</span>
        {t.status === "open" && (!lead || lookKey) ? take : null}
      </div>

      {/* What can't wait comes first: a blocked import's Look inside, right under the header. */}
      {blockedPart}

      {t.request ? (
        <div className="m-ticket__request">
          <Journey stage={t.request.stage} kind={t.request.title.kind} compact={!lead} />
          <span className="muted">{stageLabel(t.request.stage, t.request.title.kind)}{rowLine(t.request) ? ` · ${rowLine(t.request)}` : ""}</span>
          {t.request.stuck.length ? (
            <span className="m-stuck"><CircleAlert size={15} aria-hidden /><span>{t.request.stuck.map((s) => <span key={s} className="m-stuck__item">{s}</span>)}</span></span>
          ) : null}
        </div>
      ) : null}

      {t.subject === "title" && ((t.tmdb && ["tv", "movie"].includes(t.kind)) || t.plexUrl) ? (
        <div className="m-ticket__links">
          {t.tmdb && ["tv", "movie"].includes(t.kind) ? <Link to={`/title/${t.kind}/${t.tmdb}`}>Open its page</Link> : null}
          {t.plexUrl ? <a href={t.plexUrl} target="_blank" rel="noopener noreferrer">Open in Plex</a> : null}
        </div>
      ) : null}

      {t.statusThen ? (
        <div className="m-ticket__sees">
          <span className="m-card__eyebrow">When it was opened, Plexbie saw</span>
          {t.statusThen.split("\n").map((line, i) => <span key={i}>{line}</span>)}
        </div>
      ) : null}

      {video && t.status === "open" ? (
        <RequestFixes kind={t.kind} busy={busy} disabled={readOnly} byName={t.offer === "name"} onSearch={(how) => void search(how)} />
      ) : null}

      <ol className="m-thread" aria-label="Timeline">
        {t.thread.map((e) => (
          <li key={e.id} className={`m-thread__entry is-${e.kind}`}>
            {e.kind === "status" ? (
              <span className="m-thread__status">{e.by}: {e.text} · {since(e.at)}</span>
            ) : (
              <>
                <span className="m-thread__head">
                  <b>{e.by}</b>{ENTRY_LABEL[e.kind] ? <span className={`m-thread__tag${e.missed ? " is-missed" : ""}`}>{e.kind === "reply" ? (e.missed ? "Not delivered" : `Sent to ${t.who}`) : ENTRY_LABEL[e.kind]}</span> : null}
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
          <div className="m-seg" role="group" aria-label="Who sees it">
            <button type="button" aria-pressed={kind === "note"} className="m-seg__item" onClick={() => setKind("note")}>Note for admins</button>
            <button type="button" aria-pressed={kind === "reply"} className="m-seg__item" onClick={() => setKind("reply")}>Reply to {t.who}</button>
          </div>
          <label className="visually-hidden" htmlFor={`${titleId}-say`}>{kind === "note" ? "Note for admins" : `Reply to ${t.who}`}</label>
          <textarea ref={box} id={`${titleId}-say`} className={`m-textarea${kind === "reply" ? " m-textarea--reply" : " m-textarea--note"}`} rows={3} maxLength={1200}
            value={text} onChange={(e) => { setText(e.target.value); setEmpty(false); }} aria-describedby={empty ? `${titleId}-empty` : undefined}
            placeholder={kind === "note" ? "Only admins see this" : `${t.who} gets this as a Discord DM or an alert, and can answer`} />
          <div className="m-person__actions m-person__actions--two">
            <button type="button" className="btn m-btn" disabled={readOnly || busy.size > 0}
              onClick={() => void run("status", () => api.ticketStatus(id, t.waiting ? "open" : "waiting"), t.waiting ? "Back with the admins" : `Waiting on ${t.who}`)}>
              {t.waiting ? "Stop waiting" : `Wait on ${t.who}`}
            </button>
            <button type="button" className="btn btn--primary m-btn" disabled={readOnly || busy.has("send")} onClick={() => void send()}>
              {busy.has("send") ? "Sending…" : kind === "note" ? "Add note" : "Send reply"}
            </button>
          </div>
          {empty ? <p className="field__error" id={`${titleId}-empty`} role="alert">Write the {kind === "note" ? "note" : "reply"} first.</p> : null}
          {solving ? (
            <div className="m-help__reply">
              <label className="field__label" htmlFor={`${titleId}-last`}>Last word to {t.who} <span className="muted">(optional)</span></label>
              <textarea id={`${titleId}-last`} className="m-textarea m-textarea--reply" rows={2} maxLength={600} value={last} autoFocus={solve > 0}
                onChange={(e) => setLast(e.target.value)} placeholder="Grabbed the 4K release, it’ll be on Plex tonight" />
              <div className="m-person__actions m-person__actions--two">
                <button type="button" className="btn m-btn" onClick={() => { setSolving(false); setLast(""); }}>Back</button>
                <button type="button" className="btn btn--primary m-btn" disabled={readOnly || busy.has("solve")}
                  onClick={async () => { if (await run("solve", () => api.ticketStatus(id, "resolved", last.trim()), "Solved")) { setSolving(false); setLast(""); } }}>
                  <Check size={16} aria-hidden /> {busy.has("solve") ? "Solving…" : "Solve it"}
                </button>
              </div>
            </div>
          ) : (
            <button type="button" className="btn btn--quiet m-btn m-ticket__solve" disabled={readOnly} onClick={() => setSolving(true)}>
              <Check size={16} aria-hidden /> Solve…
            </button>
          )}
        </div>
      ) : lead ? null : reopen}
    </div>
  );
}

function TicketSheet({ id, onClose }: { id: string; onClose: () => void }) {
  const res = useKept<AdminTicketDetail>(() => api.adminTicket(id), [id]);
  const t = res.data ?? null;
  const titleId = useId();
  return (
    <BottomSheet titleId={titleId} onClose={onClose} className="m-reqsheet m-ticketsheet"
      title={t ? <>{ticketTitle(t)}{seasonsWords(t.seasons) ? <span className="muted"> · {seasonsWords(t.seasons)}</span> : null}</> : "Ticket"}>
      <TicketBody id={id} res={res} />
    </BottomSheet>
  );
}

/** A blocked import before looking inside: what Sonarr or Radarr said, and the way in. */
function BlockedSummary({ row, onLook, keyed = true }: { row: BlockedRow; onLook: () => void;
  /** Its own Look inside key (not when the pane's main key is Look inside already). */
  keyed?: boolean;
}) {
  const app = row.app === "sonarr" ? "Sonarr" : "Radarr";
  const n = row.episodes.length;
  return (
    <section className="m-blocked m-blocked--summary" aria-label={`${app} won’t import this by itself`}>
      <h3>{app} can’t import {n ? `${n === 1 ? "1 episode" : `${n} episodes`}` : "it"}{n ? <span className="muted"> ({n > 1 ? `${row.episodes[0]} to ${row.episodes[n - 1].replace(/^S\d+/, "")}` : row.episodes[0]})</span> : null}</h3>
      <p className="m-blocked__release">{row.release}</p>
      {row.messages.map((m) => <p key={m} className="m-blocked__msg">{m}</p>)}
      {keyed ? (
        <div className="m-blocked__acts">
          <button type="button" className="btn btn--primary m-btn" onClick={onLook}><Search size={18} aria-hidden /> Look inside</button>
          <span className="muted">See the files, pick episodes, then Hold to import</span>
        </div>
      ) : <p className="muted m-blocked__how">Look inside: see the files, pick episodes, then Hold to import.</p>}
    </section>
  );
}

/** Each menu row's icon. */
const MENU_ICON: Record<string, Icon> = {
  all: List, older: Archive, tickets: LifeBuoy, blocked: Download, messages: MessageCircle,
  joins: UserPlus, people: Users, plex: Mail, links: Link2, who: Share2,
  leaving: Hourglass, keep: ShieldCheck, settings: Settings,
  services: Server, security: Lock, party: MonitorPlay, say: Megaphone, dms: MessageSquare,
};

/** A group's sum, like its monitor's: "3 waiting", "6 · 5 tickets · 1 chat". */
function GroupSum({ g }: { g: MenuGroup }) {
  if (g.count === undefined || !g.makeup) return null;
  const n = `${g.count}`;
  const single = !g.makeup.includes(" · ") && g.makeup.startsWith(`${n}\u00a0`);
  return <span className="m-mgroup__sum">{g.count ? <b>{n}</b> : null}{single ? g.makeup.slice(n.length + 1) : g.makeup}</span>;
}

/**
 * "Everything else": every full view, grouped by job like the monitors and with the same
 * counts, in a drawer from the right (two columns on a wide screen). Escape, Back, the
 * close button or a tap outside closes it, and focus goes back to the button.
 */
function MenuDrawer({ groups, facts, onClose, onPick }: {
  groups: MenuGroup[]; facts: string; onClose: () => void; onPick: (row: MenuRow) => void;
}) {
  const panel = useRef<HTMLDivElement>(null);
  useSheet(panel, onClose);
  useTabWrap(panel);
  const titleId = useId();
  const group = (g: MenuGroup) => (
    <section key={g.id} className="m-mgroup" aria-labelledby={`${titleId}-${g.id}`}>
      <h3 className="m-mgroup__head" id={`${titleId}-${g.id}`}><span className="caps">{g.label}</span><GroupSum g={g} /></h3>
      <ul className="m-mlist">
        {g.rows.map((r) => {
          const I = MENU_ICON[r.id] ?? Menu;
          return (
            <li key={r.id}>
              <button type="button" className="m-mitem" onClick={() => onPick(r)}>
                <I size={20} aria-hidden />
                <span className="m-mitem__t">{r.label}<span className="m-mitem__d">{r.desc}</span></span>
                <span className="m-mends">
                  {r.urgent ? <span className="m-mtag">{`${r.urgent}\u00a0urgent`}</span> : r.hot ? <span className="m-mtag">Urgent</span> : null}
                  {r.tally === "down" ? <span className="m-mcount m-mcount--hot">{`${r.count}\u00a0down`}</span>
                    : r.tally === "live" ? <span className="m-mcount m-mcount--live">Live</span>
                      : r.count ? <span className="m-mcount">{r.count}</span> : null}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
  const col = (ids: MenuGroup["id"][]) => groups.filter((g) => ids.includes(g.id)).map(group);
  return (
    <div className="m-drawer-wrap">
      <div className="m-drawer__scrim" aria-hidden onClick={onClose} />
      <div ref={panel} className="m-drawer" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <div className="m-drawer__top">
          <div><h2 id={titleId} tabIndex={-1}>Everything else</h2><p>Grouped like the monitors, with the same counts</p></div>
          <button type="button" className="btn btn--quiet m-iconbtn" aria-label="Close the menu" onClick={onClose} data-autofocus><X size={20} aria-hidden /></button>
        </div>
        <div className="m-drawer__cols">
          <div>{col(["requests", "help", "people"])}</div>
          <div>{col(["library", "services", "discord"])}</div>
        </div>
        {facts ? <p className="m-drawer__foot">{facts}</p> : null}
      </div>
    </div>
  );
}

/** The menu's button, with a dot when anything needs you (pink when something is urgent). */
function EverythingElse({ groups, dot, hot, facts, onPick, open, setOpen }: {
  groups: MenuGroup[]; dot: boolean; hot: boolean; facts: string; onPick: (row: MenuRow) => void;
  open: boolean; setOpen: (open: boolean) => void;
}) {
  return (
    <>
      <button type="button" className="btn m-menu-btn" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(true)}
        aria-label={`Everything else${dot ? (hot ? ", something urgent" : ", things waiting") : ""}`}>
        <Menu size={20} aria-hidden /><span className="m-menu-btn__label">Everything else</span>
        {dot ? <span className={`m-menu-btn__dot${hot ? " is-hot" : ""}`} aria-hidden /> : null}
      </button>
      {open ? createPortal(
        <MenuDrawer groups={groups} facts={facts} onClose={() => setOpen(false)}
          onPick={(r) => { setOpen(false); afterOverlay(() => onPick(r)); }} />,
        document.body,
      ) : null}
    </>
  );
}

/* ================================================================ toasts */

/** What Undo's shortcut is called on this keyboard. */
const UNDO_KEYS = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform) ? "Option+Z" : "Alt+Z";

function ToastItem({ t, dismiss, hold }: { t: Toast; dismiss: (id: number, undone?: boolean) => void; hold: boolean }) {
  // The clock pauses while the tab is hidden, or while the pointer or keyboard
  // focus is on the toast (reaching Undo takes a moment), so nothing vanishes unread.
  // With `hold` (an Undo while the keyboard is in use) it waits until it's let go.
  const clock = useRef<{ pause: () => void; resume: () => void } | null>(null);
  useEffect(() => {
    let left = t.undo ? 6500 : 4200;
    let started = Date.now();
    let timer: number | undefined = window.setTimeout(() => dismiss(t.id), left);
    let holds = 0;
    let gone = false;
    const pause = () => {
      if (gone || holds++ > 0) return;
      window.clearTimeout(timer);
      left -= Date.now() - started;
    };
    const resume = () => {
      if (gone || --holds > 0) return;
      holds = 0;
      started = Date.now();
      timer = window.setTimeout(() => dismiss(t.id), Math.max(800, left));
    };
    clock.current = { pause, resume };
    const onVis = () => (document.hidden ? pause() : resume());
    document.addEventListener("visibilitychange", onVis);
    // Once torn down the clock never starts again, even if a late resume() reaches it.
    return () => { gone = true; window.clearTimeout(timer); document.removeEventListener("visibilitychange", onVis); };
  }, [t, dismiss]);
  useEffect(() => {
    if (!hold) return;
    const c = clock.current;
    c?.pause();
    return () => c?.resume();
  }, [hold]);
  return (
    <motion.div
      layout
      className={`m-toast${t.tone === "error" ? " is-error" : ""}`}
      role={t.tone === "error" ? "alert" : undefined}
      onPointerEnter={() => clock.current?.pause()} onPointerLeave={() => clock.current?.resume()}
      onFocus={() => clock.current?.pause()} onBlur={() => clock.current?.resume()}
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: 8, transition: { duration: 0.16, ease: EASE_OUT } }}
      transition={{ duration: 0.24, ease: EASE_OUT }}
      drag="x"
      dragSnapToOrigin
      onDragEnd={(_, i) => { if (Math.abs(i.offset.x) > 70 || Math.abs(i.velocity.x) > 500) dismiss(t.id); }}
    >
      {t.tone === "error" ? <CircleAlert size={18} aria-hidden /> : <Check size={18} aria-hidden />}
      <span className="m-toast__text">
        <b>{t.text}</b>{t.detail && t.detail !== t.text ? <small>{t.detail}</small> : null}
        {t.undo ? <span className="visually-hidden"> {UNDO_KEYS} undoes it.</span> : null}
      </span>
      {t.undo ? (
        <button type="button" className="btn btn--quiet m-btn m-toast__undo" aria-keyshortcuts="Alt+Z" onClick={() => { t.undo!(); dismiss(t.id, true); }}>Undo</button>
      ) : null}
    </motion.div>
  );
}

/**
 * The toasts, kept apart from the page so a new one re-renders only this. `register`
 * gets the function that adds one. While the keyboard is in use, Undo toasts wait to be
 * acted on (Escape lets them go), and Alt+Z (Option+Z on a Mac) runs the newest Undo.
 */
function ToastHost({ register }: { register: (push: (t: ToastIn) => void) => void }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);
  const [keyboard, setKeyboard] = useState(false);
  const [letGo, setLetGo] = useState<Set<number>>(() => new Set());
  // The toasts as they are now, kept in step on every change (a push and a dismiss in the
  // same moment both see the other), and each held-back decision sent once only.
  const latest = useRef<Toast[]>([]);
  const set = useCallback((next: Toast[]) => { latest.current = next; setToasts(next); }, []);
  const gone = useCallback((t: Toast) => { const run = t.onGone; t.onGone = undefined; run?.(); }, []);
  const dismiss = useCallback((id: number, undone = false) => {
    const t = latest.current.find((x) => x.id === id);
    if (t && !undone) gone(t);
    set(latest.current.filter((x) => x.id !== id));
  }, [gone, set]);
  useLayoutEffect(() => register((t) => {
    const all = latest.current;
    const rest = all.length && all[all.length - 1].text === t.text ? all.slice(0, -1) : all;
    const kept = rest.slice(-2);
    // Pushed off the stack is gone too: what it held back is sent.
    all.filter((x) => !kept.includes(x)).forEach(gone);
    set([...kept, { ...t, id: nextId.current++ }]);
  }), [register, gone, set]);
  // Leaving the tab (a phone app switch, closing it) or the page sends what's held back now.
  useEffect(() => {
    const flush = () => {
      if (!document.hidden) return;
      const held = latest.current.filter((t) => t.onGone);
      if (!held.length) return;
      held.forEach(gone);
      set(latest.current.filter((t) => !held.includes(t)));
    };
    document.addEventListener("visibilitychange", flush);
    window.addEventListener("pagehide", flush);
    return () => {
      document.removeEventListener("visibilitychange", flush);
      window.removeEventListener("pagehide", flush);
      latest.current.forEach(gone);
    };
  }, [gone, set]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setLetGo(new Set(latest.current.map((t) => t.id)));
        return;
      }
      // A shortcut such as copy, or a modifier alone, doesn't count as using the keyboard.
      if (!e.ctrlKey && !e.metaKey && !["Alt", "Control", "Meta", "Shift"].includes(e.key)) setKeyboard(true);
      // The key marked Z on any layout; Option+Z on a Mac types Ω, so there it's the key's place.
      const z = /^[a-z]$/i.test(e.key) ? e.key.toLowerCase() === "z" : e.code === "KeyZ";
      if (e.altKey && !e.ctrlKey && !e.metaKey && z) {
        const t = [...latest.current].reverse().find((x) => x.undo);
        if (!t) return;
        e.preventDefault();
        // Holding the keys down undoes one thing, not one after another.
        if (e.repeat) return;
        t.undo!();
        dismiss(t.id, true);
      }
    };
    const onPointer = () => setKeyboard(false);
    window.addEventListener("keydown", onKey);
    window.addEventListener("pointerdown", onPointer);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("pointerdown", onPointer);
    };
  }, [dismiss]);
  // Always present, so screen readers hear each toast as it's added. On body beside the
  // sheets, not in #root: an open sheet makes the page inert and covers it, and a result
  // from inside the sheet must still be seen and heard.
  return createPortal(
    <div className="m-toasts" role="status" aria-live="polite">
      <AnimatePresence initial={false}>
        {/* A decision held back for Undo isn't held forever: only an Undo of something already done waits for the keyboard. */}
        {toasts.map((t) => <ToastItem key={t.id} t={t} dismiss={dismiss} hold={keyboard && !!t.undo && !t.onGone && !letGo.has(t.id)} />)}
      </AnimatePresence>
    </div>,
    document.body,
  );
}

/* ================================================================== page */

/* ================================================================== desk */

type DeskView = "needs" | "waiting" | "done";
const VIEW_LABEL: Record<DeskView, string> = { needs: "Needs you", waiting: "Waiting on them", done: "Done" };
const GROUP_HEAD: Record<Group, { label: string; rule: string }> = {
  urgent: { label: "Urgent", rule: "Can’t wait" },
  today: { label: "Today", rule: "Longest wait first" },
  soon: { label: "Coming up", rule: "Soonest deadline first" },
  waiting: { label: "Waiting on them", rule: "They owe you an answer" },
  done: { label: "Done · last 2 days", rule: "Newest first" },
};
/** Wide enough for the queue and its detail side by side (styles.css). */
const WIDE = "(min-width: 1000px)";

function useMedia(query: string) {
  const [on, setOn] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const q = window.matchMedia(query);
    const change = () => setOn(q.matches);
    change();
    q.addEventListener("change", change);
    return () => q.removeEventListener("change", change);
  }, [query]);
  return on;
}

/** Typing somewhere, or a sheet or the menu is up: the page's keys stay out of the way. */
const keysOff = (e: KeyboardEvent, within: string) => {
  const t = e.target as HTMLElement | null;
  return e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented || document.documentElement.classList.contains("has-sheet")
    || !!t?.closest("input, textarea, select, [contenteditable='true']")
    // Single-key shortcuts only where they're meant (WCAG 2.1.4): never from a stray key elsewhere.
    || !t?.closest(within);
};
/** Where the queue's keys work: the strip, the queue and the detail. */
const DESK_KEYS = ".m-strip, .m-queue-col, .m-pane";

/** A row's picture: its poster (or a drawn cover), an icon, or the person's initial. */
function ItemArt({ item, big = false }: { item: Item; big?: boolean }) {
  if (item.art) return <span className={big ? "m-hero__art" : "m-thumb"}><Art title={{ ...item.art, kind: item.art.kind as never }} size={big ? "w342" : "w185"} /></span>;
  if (item.initial) return <span className={big ? "m-hero__who" : "m-who"} aria-hidden>{item.initial.trim().charAt(0).toUpperCase()}</span>;
  const I = item.kind === "down" ? Server : item.kind === "join" ? UserPlus : item.kind === "link" ? Link2 : item.kind === "invite" ? Mail
    : item.kind === "import" ? Download : LifeBuoy;
  return <span className={big ? "m-hero__tile" : "m-tile2"} aria-hidden><I size={20} /></span>;
}

/** Every action the queue offers, the same ones the full views use. */
function useDeskActions() {
  const { refresh, toast, readOnly, defer } = useManage();
  const { busy, act } = useAct();
  const requests = useDecideRequest();
  const joins = useDecideJoin();
  const keepPerson = useKeepPerson();
  const keepTitle = useKeepTitle();
  const plex = useCancelPlexInvite();
  const links = useRevokeInvite();
  const after = (...sections: Section[]) => window.setTimeout(() => sections.forEach((s) => void refresh(s)), 600);
  const say = async (key: string, call: () => Promise<Ack>, done: string, ...sections: Section[]) => {
    const out = await act(key, call);
    if (!out) return false;
    toast(out.told === false ? { tone: "error", text: "Not delivered", detail: out.message } : { text: done, detail: out.message });
    after(...sections);
    return true;
  };
  return {
    readOnly,
    busy: (key: string) => busy.has(key) || requests.busy.has(key.replace(/^request:/, "")) || joins.busy.has(key.replace(/^join:/, ""))
      || plex.busy.size > 0 && key.startsWith("plex:") || links.busy.size > 0 && key.startsWith("link:"),
    // Decisions wait out an Undo before they're sent (the full views send theirs at once).
    approve: (r: AdminRequest, yes: boolean) =>
      defer(`request:${r.id}`, `${yes ? "Approved" : "Declined"} ${r.title}.`, () => requests.decide(r, yes, true)),
    invite: (j: AdminJoin, yes: boolean) =>
      defer(`join:${j.key}`, yes ? `Invited ${j.name.replace(/\s*\(Plex sign-in\)$/i, "")} to Plex.` : `Denied ${j.name.replace(/\s*\(Plex sign-in\)$/i, "")}.`, () => joins.decide(j, yes, true)),
    take: (t: AdminTicketRow, mine: boolean) => say(`ticket:${t.id}`, () => api.ticketTake(t.id), mine ? "Let go" : "It’s yours", "tickets"),
    searchTicket: (t: AdminTicketRow) => say(`ticket:${t.id}`, () => api.helpSearch(t.id), SEARCHING.again, "tickets", "all"),
    searchRequest: (r: AdminRequestRow) => say(`stuck:${r.id}`, () => api.requestSearch(r.id!, "again"), SEARCHING.again, "all"),
    reopen: (t: AdminTicketRow) => say(`ticket:${t.id}`, () => api.ticketStatus(t.id, "open"), t.status === "open" ? "Back with the admins" : "Reopened", "tickets"),
    chatDone: (p: MessagePerson, done: boolean) => say(`chat:${p.id}`, () => api.messageDone(p.id, done), done ? "Marked done" : "Marked unread", "messages"),
    keepPerson: (p: AdminPerson) => keepPerson(p, true),
    keepTitle: (c: AdminCleanupRow) => keepTitle(c, true),
    cancelPlex: (i: PlexInvite) => defer(`plex:${i.email || `${i.name}|${i.sentAt}`}`, `Cancelled the Plex invite to ${i.email || i.name}.`, () => plex.cancel(i, true)),
    cancelLink: (i: AdminInvite) => defer(`link:${i.id}`, `Cancelled ${i.label}’s invite link.`, () => links.revoke(i, true)),
    checkAgain: async () => {
      await refresh("health");
      toast({ text: "Checked again" });
    },
  };
}
type DeskActions = ReturnType<typeof useDeskActions>;

/** Opens a full view (a new page of history, so Back returns to the queue). */
function useOpenFull() {
  const [, setParams] = useSearchParams();
  return (tab: Section, extra: Record<string, string> = {}) => afterOverlay(() =>
    setParams(() => new URLSearchParams({ tab, ...extra })));
}

/** A row's two keys: what you'd most likely do, and the next thing. */
function RowActions({ item, a, open, look, reply, selected = false }: {
  item: Item; a: DeskActions; open: () => void; look: () => void; reply: () => void;
  /** Selected beside the detail, which leads with the same key: this one goes quiet. */
  selected?: boolean;
}) {
  const { session } = useSession();
  const full = useOpenFull();
  const off = a.readOnly || a.busy(item.key);
  const key = (label: ReactNode, onClick: () => void, quiet = false, disabled = off) => (
    <button type="button" className={`btn${quiet ? " btn--quiet" : ""} m-btn m-rowbtn`} disabled={disabled}
      onClick={(e) => { e.stopPropagation(); onClick(); }}>{label}</button>
  );
  const openKey = key("Open", open, true, false);
  const hold = (label: string, run: () => void) => <HoldButton quiet label={label} disabled={off} onConfirm={run} />;
  const src = item.src;
  switch (src.type) {
    case "ticket": {
      const t = src.t;
      if (item.group === "done") return <>{key("Open", open, false, false)}{key("Reopen", () => void a.reopen(t), true)}</>;
      if (item.group === "waiting") return <>{key("Open", open, false, false)}{key("Back to open", () => void a.reopen(t), true)}</>;
      const video = ["tv", "movie"].includes(t.kind) && (t.subject !== "title" || !!t.tmdb);
      if (src.blocked) return <>{key("Look inside", look, selected, false)}{key("Search again", () => void a.searchTicket(t), true)}</>;
      if (t.owner && t.owner === session?.user.name) return <>{video ? key("Search again", () => void a.searchTicket(t)) : null}{openKey}</>;
      return <>{key(t.owner ? "Take it over" : "Take it", () => void a.take(t, false))}{openKey}</>;
    }
    case "import": return <>{key("Look inside", look, false, false)}{key("Open", () => full("health", { part: "blocked" }), true, false)}</>;
    case "stuck": return <>{["tv", "movie"].includes(src.r.title.kind) ? key("Search again", () => void a.searchRequest(src.r)) : null}{openKey}</>;
    case "request":
      if (item.group === "done") return <>{key("Open", open, false, false)}{key("All requests", () => full("all"), true, false)}</>;
      return <>{key(<><Check size={18} aria-hidden /> Approve</>, () => void a.approve(src.r, true))}{hold("Decline", () => void a.approve(src.r, false))}</>;
    case "chat":
      if (item.group === "done") return <>{key("Open", open, false, false)}{key("Mark unread", () => void a.chatDone(src.p, false), true)}</>;
      return <>{key("Reply", reply, false, false)}{openKey}</>;
    case "join":
      if (!src.j.messageId) return openKey;
      return <>{key(<><Check size={18} aria-hidden /> Invite to Plex</>, () => void a.invite(src.j, true))}{hold("Deny", () => void a.invite(src.j, false))}</>;
    case "person": return <>{src.chat ? key("Reply", reply, false, false) : key("Open", open, false, false)}{key("Never remove", () => void a.keepPerson(src.p), true)}</>;
    case "leaving": return <>{key("Keep", () => void a.keepTitle(src.c))}{openKey}</>;
    case "link": return <>{key("Open", () => full("invites", { part: "links" }), false, false)}{hold("Cancel", () => void a.cancelLink(src.i))}</>;
    case "plexinvite":
      if (!src.i.email) return openKey;
      return <>{key("Change email", () => full("invites", { part: "plex" }), false, false)}{hold("Cancel", () => void a.cancelPlex(src.i))}</>;
    case "down": return <>{key("Check again", () => void a.checkAgain())}{openKey}</>;
  }
}

/** One queue row: its picture, three lines, and two keys. */
function QueueRow({ item, selected, onSelect, actions }: {
  item: Item; selected: boolean; onSelect: () => void; actions: ReactNode;
}) {
  return (
    <li className={`m-row${selected ? " is-selected" : ""}`} data-key={item.key} onClick={onSelect}>
      <button type="button" className="m-row__main" aria-current={selected ? "true" : undefined} onClick={(e) => { e.stopPropagation(); onSelect(); }}>
        <ItemArt item={item} />
        <span className="m-row__text">
          <span className="m-row__kind"><span className="caps">{item.eyebrow}</span>{item.age ? <span className="m-row__age">{item.age}</span> : null}</span>
          <span className="m-row__title">{item.title}{item.sub ? <span className={`muted${item.subIsSeason ? " m-row__season" : ""}`}> · {item.sub}</span> : null}</span>
          <span className="m-row__meta">{item.meta}</span>
        </span>
      </button>
      <div className="m-row__acts">{actions}</div>
    </li>
  );
}

/** A ticket in the detail: the ticket's own insides, as Tickets shows it, its main key and
 *  "Open the full ticket" as the pair under the header. */
function TicketPane({ item, look, solve, lead }: { item: Item & { src: { type: "ticket" } }; look: boolean; solve: number; lead: ReactNode }) {
  const res = useKept<AdminTicketDetail>(() => api.adminTicket(item.src.t.id), [item.src.t.id]);
  return <TicketBody key={String(look)} id={item.src.t.id} res={res} head={false} blockedRow={look ? undefined : item.src.blocked} solve={solve} lead={lead} />;
}

/** A request in the detail: RequestSheet's insides (journey, fixes, Open a ticket, history). */
function RequestPane({ id }: { id: string }) {
  const res = useKept<AdminRequestDetail>(() => api.adminRequest(id), [id]);
  return <RequestBody id={id} res={res} head={false} quiet />;
}

/** What the detail says about an item, top to bottom. Every kind opens the same way: its
 *  main key and a pill, left-aligned right under the header, then what there is to know. */
function DetailBody({ item, a, look, onLook, solve, reply }: {
  item: Item; a: DeskActions; look: boolean; onLook: () => void; solve: number; reply: number;
}) {
  const full = useOpenFull();
  const src = item.src;
  const off = a.readOnly || a.busy(item.key);
  const facts = (rows: [string, ReactNode][]) => (
    <dl className="m-facts">{rows.filter(([, v]) => v !== null && v !== "").map(([k, v]) => <Fragment key={k}><dt>{k}</dt><dd>{v}</dd></Fragment>)}</dl>
  );
  const acts = (main: ReactNode, pill: ReactNode) => <div className="m-pane__acts">{main}{pill}</div>;
  const hold = (label: string, run: () => void) => <HoldButton quiet label={label} disabled={off} onConfirm={run} />;
  // The pane's main key is its one pink key, above the fold.
  const key = (label: ReactNode, onClick: () => void, disabled = off) =>
    <button type="button" className="btn btn--primary m-btn" disabled={disabled} onClick={onClick}>{label}</button>;
  const pill = (label: ReactNode, onClick: () => void) =>
    <button type="button" className="btn btn--quiet m-btn" onClick={onClick}>{label}</button>;
  switch (src.type) {
    case "ticket":
      return <TicketPane item={item as Item & { src: { type: "ticket" } }} look={look} solve={solve}
        lead={pill("Open the full ticket", () => full("tickets", { ticket: src.t.id }))} />;
    case "import":
      return (
        <>
          {acts(key(<><Search size={18} aria-hidden /> Look inside</>, onLook, false), pill("All blocked imports", () => full("health", { part: "blocked" })))}
          {look ? <BlockedImport target={src.b} /> : <BlockedSummary row={src.b} onLook={onLook} />}
        </>
      );
    case "request":
      if (item.group === "done") {
        return (
          <>
            {acts(key("All requests", () => full("all"), false), null)}
            {facts([["Asked", `${since(src.r.requestedAt)} by ${src.r.requester}`], [src.r.status === "approved" ? "Approved" : "Declined", src.r.resolvedAt ? `${since(src.r.resolvedAt)}${src.r.resolvedBy ? ` by ${src.r.resolvedBy}` : ""}` : null]])}
          </>
        );
      }
      return (
        <>
          {acts(key(<><Check size={18} aria-hidden /> Approve</>, () => void a.approve(src.r, true)), hold("Decline", () => void a.approve(src.r, false)))}
          <RequestPane id={src.r.id} />
        </>
      );
    case "stuck":
      return (
        <>
          {acts(["tv", "movie"].includes(src.r.title.kind) ? key(<><RefreshCw size={16} aria-hidden /> Search again</>, () => void a.searchRequest(src.r)) : null,
            pill("All requests", () => full("all")))}
          <RequestPane id={src.r.id!} />
        </>
      );
    case "chat":
      return (
        <>
          {acts(item.group === "done" ? key("Mark unread", () => void a.chatDone(src.p, false)) : key(<><Check size={16} aria-hidden /> Done</>, () => void a.chatDone(src.p, true)),
            pill("All messages", () => full("messages")))}
          <Conversation key={src.p.id} person={src.p} onBack={() => undefined} inPane reply={reply} />
        </>
      );
    case "join":
      return (
        <>
          {src.j.messageId
            ? acts(key(<><Check size={18} aria-hidden /> Invite to Plex</>, () => void a.invite(src.j, true)), hold("Deny", () => void a.invite(src.j, false)))
            : acts(pill("Join requests", () => full("joins")), null)}
          {src.j.messageId ? null : <p className="muted">Answer this one in Discord.</p>}
          {facts([["Asked", since(src.j.askedAt)], ["How", src.j.via === "plex" ? "Signed in with Plex" : "On Discord"], ["Plex invite to", src.j.email || (src.j.via === "plex" ? "Their Plex account" : null)]])}
        </>
      );
    case "person": {
      const p = src.p;
      const total = p.removalIn === null ? null : p.daysIdle + p.removalIn;
      return (
        <>
          {acts(key(<><ShieldCheck size={16} aria-hidden /> Never remove</>, () => void a.keepPerson(p)), pill("People", () => full("people")))}
          <div className="m-meter m-meter--hot">
            <span className="m-meter__track"><span className="m-meter__fill" style={{ transform: `scaleX(${total ? Math.min(1, p.daysIdle / total) : 1})` }} /></span>
            <span className="m-meter__label">{p.removalIn ? `${p.removalIn} days until removal · idle ${p.daysIdle}` : "Due for removal"}</span>
          </div>
          {facts([["Last watched", p.lastWatched ? since(p.lastWatched) : "No watch recorded"], ["Discord", p.discordName], ["Warned", p.warned ? "Yes" : null]])}
          {src.chat ? <Conversation key={src.chat.id} person={src.chat} onBack={() => undefined} inPane reply={reply} /> : null}
        </>
      );
    }
    case "leaving":
      return (
        <>
          {acts(key(<><ShieldCheck size={16} aria-hidden /> Keep it</>, () => void a.keepTitle(src.c)), pill("Leaving soon", () => full("cleanup")))}
          {facts([["Leaves in", `${src.c.daysLeft === 1 ? "1 day" : `${src.c.daysLeft} days`}`], ["Why", clockText(src.c.reason, src.c.lastActivity)], ["Type", src.c.type === "show" ? "TV" : "Film"]])}
        </>
      );
    case "link":
      return (
        <>
          {acts(key("Invite links", () => full("invites", { part: "links" }), false), hold("Cancel", () => void a.cancelLink(src.i)))}
          {facts([["Made", `${since(src.i.createdAt)} by ${src.i.createdBy ?? "an admin"}`], ["Ends", daysUntil(src.i.expiresAt)], ["Locked to", src.i.email]])}
          <p className="muted">The link itself is only shown once, when it’s made. To send it again, make a new one under Invites.</p>
        </>
      );
    case "plexinvite":
      return (
        <>
          {src.i.email
            ? acts(key("Change email", () => full("invites", { part: "plex" }), false), hold("Cancel", () => void a.cancelPlex(src.i)))
            : acts(pill("Waiting on Plex", () => full("invites", { part: "plex" })), null)}
          {src.i.email ? null : <p className="muted">Sent to a Plex username, so change or cancel it on plex.tv.</p>}
          {facts([["Sent", since(src.i.sentAt)], ["To", src.i.email || src.i.name]])}
        </>
      );
    case "down":
      return (
        <>
          {acts(key(<><RefreshCw size={16} aria-hidden /> Check again</>, () => void a.checkAgain()), pill("Services", () => full("health")))}
          <p className="m-pane__text">{src.h.detail ?? "Not answering."}</p>
        </>
      );
  }
}

/** The selected item in full: beside the queue on a wide screen, a full-height sheet on a phone. */
function Detail({ item, where, a, sheet, look, onLook, solve, reply, focus, state, onStep, onClose }: {
  item: Item | null; where: { group: string; place: string; first: boolean; last: boolean }; a: DeskActions; sheet: boolean;
  look: boolean; onLook: () => void; solve: number; reply: number;
  /** A new value puts focus on the title (Enter or Open on a row). */
  focus: number;
  /** With nothing selected: still loading, something couldn't load, or truly nothing. */
  state: "loading" | "missing" | "clear";
  onStep: (by: 1 | -1) => void; onClose: () => void;
}) {
  const box = useRef<HTMLElement>(null);
  const title = useRef<HTMLHeadingElement>(null);
  const [more, setMore] = useState({ has: false, end: true });
  // "More below" and the fade only when the pane really runs past the screen.
  useEffect(() => {
    const el = box.current;
    if (!el || sheet) return;
    const check = () => setMore({ has: el.scrollHeight > el.clientHeight + 1, end: el.scrollTop + el.clientHeight >= el.scrollHeight - 4 });
    check();
    const seen = new ResizeObserver(check);
    seen.observe(el);
    if (el.firstElementChild) [...el.children].forEach((c) => seen.observe(c));
    el.addEventListener("scroll", check, { passive: true });
    return () => { seen.disconnect(); el.removeEventListener("scroll", check); };
  }, [sheet, item?.key]);
  useEffect(() => { box.current?.scrollTo({ top: 0 }); }, [item?.key]);
  useEffect(() => { if (focus) title.current?.focus({ preventScroll: true }); }, [focus]);
  const done = item && (item.kind === "ticket" || item.kind === "chat") && item.group !== "done";
  return (
    <aside ref={box} className={`m-pane${sheet ? " is-sheet" : ""}${more.has ? " has-more" : ""}${more.end ? " at-end" : ""}`}
      aria-label={item ? `Selected: ${item.title}` : "Nothing selected"} aria-busy={!item && state === "loading"}>
      <div className="m-pane__top">
        <span className="m-pane__where"><b>{where.group}</b> · {where.place}</span>
        <div className="m-pane__nav">
          {sheet ? null : <>
            <button type="button" className="btn btn--quiet m-iconbtn" aria-label="Previous (K)" disabled={!item || where.first} onClick={() => onStep(-1)}><ChevronUp size={20} aria-hidden /></button>
            <button type="button" className="btn btn--quiet m-iconbtn" aria-label="Next (J)" disabled={!item || where.last} onClick={() => onStep(1)}><ChevronDown size={20} aria-hidden /></button>
          </>}
          {sheet ? <button type="button" className="btn btn--quiet m-iconbtn" aria-label="Back to the queue" onClick={onClose} data-autofocus><X size={20} aria-hidden /></button> : null}
        </div>
      </div>
      {item ? (
        <div className="m-pane__body" key={item.key}>
          <div className={`m-hero${item.art ? "" : " m-hero--plain"}`}>
            <ItemArt item={item} big />
            <div>
              <p className="caps m-hero__eyebrow">{item.eyebrow}</p>
              <h2 className="m-hero__title" ref={title} tabIndex={-1}>{item.title}</h2>
              {item.sub ? <p className="m-hero__sub">{item.sub}</p> : null}
              <p className="m-hero__meta">{[item.meta, item.age && item.group !== "soon" ? `${item.age}${/^\d/.test(item.age) ? " ago" : ""}` : ""].filter(Boolean).join(" · ")}</p>
            </div>
          </div>
          <DetailBody item={item} a={a} look={look} onLook={onLook} solve={solve} reply={reply} />
        </div>
      ) : state === "loading" ? (
        <div className="m-pane__body" aria-hidden>
          <div className="m-hero"><span className="skeleton m-hero__art" /><span className="m-skel-lines"><span className="skeleton" /><span className="skeleton" /><span className="skeleton" /></span></div>
          <div className="skeleton m-skel-block" />
        </div>
      ) : state === "missing" ? (
        <div className="m-pane__body"><p className="muted">Nothing to show here: some of the queue couldn’t be loaded. Try again above.</p></div>
      ) : (
        <div className="m-pane__body">
          <AllClear title="All clear">Nothing here right now.</AllClear>
        </div>
      )}
      {sheet ? null : (
        <div className="m-keys m-pane__keys" aria-hidden>
          <span><kbd>J</kbd> next</span><span><kbd>K</kbd> previous</span>{done ? <span><kbd>E</kbd> done</span> : null}<span><kbd>Esc</kbd> back</span>
          <span className="m-pane__more">More below <ChevronDown size={16} /></span>
        </div>
      )}
    </aside>
  );
}

/** Tab and Shift+Tab go round inside a sheet: from the last control to the first, never out to the page. */
function useTabWrap(panel: { current: HTMLElement | null }) {
  useEffect(() => {
    const el = panel.current;
    if (!el) return;
    const on = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const all = [...el.querySelectorAll<HTMLElement>("a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex='-1'])")]
        .filter((x) => x.offsetParent !== null);
      if (!all.length) return;
      const first = all[0], last = all[all.length - 1];
      const at = document.activeElement;
      if (e.shiftKey && (at === first || !el.contains(at))) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && (at === last || !el.contains(at))) { e.preventDefault(); first.focus(); }
    };
    el.addEventListener("keydown", on);
    return () => el.removeEventListener("keydown", on);
  }, [panel]);
}

/** The phone's detail: a full-height sheet over the queue (Back, Escape or × closes it). */
function DetailSheet(props: Parameters<typeof Detail>[0]) {
  const panel = useRef<HTMLDivElement>(null);
  useSheet(panel, props.onClose);
  useTabWrap(panel);
  return <div ref={panel} className="m-pane-sheet" role="dialog" aria-modal="true" aria-label={props.item ? props.item.title : "Detail"}><Detail {...props} /></div>;
}

/**
 * Manage's front page: the lit monitor strip (tap one, or 1 to 5, to show only its part
 * of the queue), what needs you (Urgent, Today, Coming up), Waiting and Done, and the
 * selected item in full beside it (on a phone, in a sheet).
 */
/** What each feed is called when it couldn't be loaded. */
const FEED_NAME: Record<Feed, string> = {
  requests: "requests", all: "all requests", tickets: "tickets", joins: "join requests", invites: "invite links",
  plexinvites: "Plex invites", people: "people", cleanup: "cleanup", messages: "messages", health: "services", blocked: "blocked imports",
};
const listed = (names: string[]) => (names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}` : names[0] ?? "");

function ManageDesk({ desk, checked, menu, stale, onRetry, openMenu }: {
  desk: Desk; checked: string | null; menu: ReactNode; openMenu: () => void;
  /** A refresh failed over what's on screen: when it was last good. */
  stale: { at?: number } | null;
  onRetry: (feeds: Feed[] | "stale") => Promise<unknown>;
}) {
  const { data } = useManage();
  const facts = desk.facts;
  const [params] = useSearchParams();
  const first = params.get("view");
  const [view, setView] = useState<DeskView>(first === "waiting" || first === "done" ? first : "needs");
  const shown = params.get("show");
  const [filter, setFilter] = useState<Area | null>(AREAS.some((x) => x.id === shown) ? shown as Area : null);
  const [sel, setSel] = useState<{ key: string | null; at: number }>({ key: params.get("item"), at: 0 });
  const wide = useMedia(WIDE);
  const [sheet, setSheet] = useState(() => !!params.get("item") && !window.matchMedia(WIDE).matches);
  // Look inside, Solve… (E) and Reply, each asked for one item.
  const [look, setLook] = useState<string | null>(null);
  const [solve, setSolve] = useState({ key: "", n: 0 });
  const [reply, setReply] = useState({ key: "", n: 0 });
  const [focus, setFocus] = useState(0);
  const loading = desk.loading;
  const missing = desk.missing;
  // A monitor narrows Needs you only.
  const { list, filtered } = listFor(desk, view, filter);
  const found = list.findIndex((i) => i.key === sel.key);
  // An item acted on leaves the list: the next one takes its place.
  const index = found >= 0 ? found : list.length ? Math.min(sel.at, list.length - 1) : -1;
  const current = index >= 0 ? list[index] : null;
  const select = (item: Item, open = false) => {
    setSel({ key: item.key, at: list.indexOf(item) });
    if (open && !wide) setSheet(true);
    // Opened on a wide screen: focus goes to the item's title beside the queue.
    if (open && wide) setFocus((n) => n + 1);
  };
  const a = useDeskActions();
  const step = (by: 1 | -1) => {
    if (!list.length) return;
    const next = list[Math.max(0, Math.min(list.length - 1, index + by))];
    select(next);
    requestAnimationFrame(() => document.querySelector<HTMLElement>(`.m-row[data-key="${CSS.escape(next.key)}"] .m-row__main`)?.focus({ preventScroll: !wide }));
  };
  const toggle = (area: Area | null) => { setView("needs"); setFilter((f) => (f === area ? null : area)); };
  const showView = (v: DeskView) => { setView(v); setSel({ key: null, at: 0 }); if (v !== "needs") setFilter(null); };
  const groups = view === "needs" ? (["urgent", "today", "soon"] as Group[]) : [view as Group];
  const label = AREAS.find((x) => x.id === filter)?.label;
  const where = current ? { group: GROUP_HEAD[current.group].label.replace(" · last 2 days", ""), place: `${index + 1}\u00a0of ${list.length}`, first: index === 0, last: index === list.length - 1 }
    : { group: VIEW_LABEL[view], place: loading ? "loading" : "nothing here", first: true, last: true };
  if (loading) Object.assign(where, { group: VIEW_LABEL[view], place: "loading", first: true, last: true });
  const up = (data.health ?? []).filter((h) => h.ok).map((h) => h.name);
  // Toasts float over the bottom of the queue column on a wide screen (styles.css).
  const column = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    const el = column.current;
    const html = document.documentElement;
    if (!el) return;
    const place = () => {
      const r = el.getBoundingClientRect();
      html.style.setProperty("--m-qcol-left", `${r.left}px`);
      html.style.setProperty("--m-qcol-width", `${r.width}px`);
    };
    place();
    html.classList.add("has-desk");
    const seen = new ResizeObserver(place);
    seen.observe(el);
    window.addEventListener("resize", place);
    return () => { seen.disconnect(); window.removeEventListener("resize", place); html.classList.remove("has-desk"); };
  }, []);
  // The queue's keys: J and K (or the arrows, in the queue) step, E is done (Solve… on a
  // ticket, Done on a conversation), A approves (or invites), 1 to 5 filter, Escape shows all.
  // Enter opens the row that has focus, as its button does.
  const keys = useRef<(e: KeyboardEvent) => void>(() => undefined);
  const onKey = (e: KeyboardEvent) => {
    if (keysOff(e, DESK_KEYS)) return;
    const k = e.key.toLowerCase();
    if (e.key === "/") { e.preventDefault(); openMenu(); return; }
    const inQueue = !!(e.target as HTMLElement | null)?.closest(".m-queue-col");
    if (/^[1-5]$/.test(e.key)) { e.preventDefault(); toggle(AREAS[Number(e.key) - 1].id); return; }
    if (k === "j" || (inQueue && e.key === "ArrowDown")) { e.preventDefault(); step(1); return; }
    if (k === "k" || (inQueue && e.key === "ArrowUp")) { e.preventDefault(); step(-1); return; }
    if (e.key === "Escape" && filtered) { toggle(null); return; }
    if (!current || a.readOnly) return;
    const src = current.src;
    if (k === "e" && current.group !== "done") {
      if (src.type === "ticket") { e.preventDefault(); setSolve((x) => ({ key: current.key, n: x.n + 1 })); if (!wide) setSheet(true); }
      else if (src.type === "chat") { e.preventDefault(); void a.chatDone(src.p, true); }
    }
    if (k === "a" && current.group !== "done") {
      if (src.type === "request") { e.preventDefault(); a.approve(src.r, true); }
      else if (src.type === "join" && src.j.messageId) { e.preventDefault(); a.invite(src.j, true); }
    }
  };
  useEffect(() => { keys.current = onKey; });
  // Focus stays in the queue after an action takes a row away: on the next row (the one
  // before, at the end), or on the queue's heading when nothing's left; in the detail, on the
  // next item's title.
  const keep = useRef<{ at: number; pane: boolean } | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const keys2 = list.map((i) => i.key).join("|");
  useEffect(() => {
    const k = keep.current;
    if (!k) return;
    const lost = !document.activeElement || document.activeElement === document.body || !document.activeElement.isConnected;
    const t = window.setTimeout(() => {
      keep.current = null;
      const still = document.activeElement && document.activeElement !== document.body && document.activeElement.isConnected;
      if (!lost && still) return;
      const rows = [...document.querySelectorAll<HTMLElement>(".m-queue .m-row__main")];
      if (k.pane && wide && rows.length) { setFocus((n) => n + 1); return; }
      const next = rows[Math.min(k.at, rows.length - 1)];
      if (next) next.focus({ preventScroll: true });
      else heading.current?.focus({ preventScroll: true });
    }, 0);
    return () => window.clearTimeout(t);
  }, [keys2, wide]);
  const remember = (e: React.MouseEvent | React.KeyboardEvent) => {
    const row = (e.target as HTMLElement).closest<HTMLElement>(".m-row");
    const pane = !!(e.target as HTMLElement).closest(".m-pane");
    const at = row ? [...document.querySelectorAll(".m-queue .m-row")].indexOf(row) : index;
    keep.current = { at: Math.max(0, at), pane };
  };
  useEffect(() => {
    const on = (e: KeyboardEvent) => keys.current(e);
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);
  const detail = {
    item: loading ? null : current, where, a, look: !!current && look === current.key, onLook: () => { if (current) setLook(current.key); },
    solve: current && solve.key === current.key ? solve.n : 0, reply: current && reply.key === current.key ? reply.n : 0,
    focus, state: (loading ? "loading" : missing.length ? "missing" : "clear") as "loading" | "missing" | "clear",
    onStep: step, onClose: () => setSheet(false),
  };

  return (
    <>
      <div className="m-dhead">
        <div className="m-dhead__text">
          <h1 className="m-h1">Manage</h1>
          <p className="m-headline" aria-live="polite">
            {loading ? <span><b>Checking what needs you…</b></span> : <>
              <span className="m-headline__long"><b>{missing.length ? "At least " : ""}{!desk.needs.length && !missing.length ? "Nothing needs you right now." : desk.needs.length === 1 ? "1\u00a0thing needs you." : `${desk.needs.length}\u00a0things need you.`}</b>{desk.urgent ? <> <b>{`${desk.urgent}\u00a0${desk.urgent === 1 ? "is" : "are"} urgent.`}</b></> : null}</span>
              <span className="m-headline__short"><b>{!desk.needs.length && !missing.length ? "Nothing needs you" : `${desk.needs.length}\u00a0need you`}</b>{desk.urgent ? <> · <b>{`${desk.urgent}\u00a0urgent`}</b></> : null}</span>
            </>}
            {" "}<span className="m-headline__more"><span className="m-headline__wide">The rest is under Everything else.</span><span className="m-headline__phone">The rest is in the menu.</span></span>
          </p>
        </div>
        {menu}
      </div>

      <section className="m-strip" aria-label="At a glance. Press a monitor to show only that part of the queue">
        <div className={`m-monitors${filtered ? " is-filtered" : ""}`} role="group" aria-label="Filter the queue">
          {desk.monitors.map((m, n) => (
            <button key={m.area} type="button" className={`m-mon${m.hot && m.known ? " is-urgent" : ""}`} aria-pressed={filtered && filter === m.area} onClick={() => toggle(m.area)}
              aria-keyshortcuts={String(n + 1)}
              aria-label={`${m.label}: ${m.failed ? "couldn’t load" : m.known ? `${m.count}, ${m.makeup}` : "loading"}${m.hot ? ", urgent" : ""}${m.first ? `. First: ${m.first.title}, ${m.first.top}` : ""}`}>
              <span className="m-mon__label"><span className="m-lamp" aria-hidden /><span className="caps">{m.label}</span></span>
              <span className="m-mon__screen">
                <span className="m-mon__num">{m.failed ? "?" : m.known ? m.count : "–"}</span>
                <span className="m-mon__top">
                  <b>{m.failed ? "Couldn’t load" : !m.known ? "Checking…" : m.first?.title ?? (m.area === "services" ? "All up" : "Nothing waiting")}</b>
                  <span>{m.failed ? "Try again below" : !m.known ? "\u00a0" : m.first?.top ?? "All clear"}</span>
                </span>
                <span className="m-mon__make">{m.failed ? "Count unknown" : m.known ? m.makeup : "Loading…"}</span>
              </span>
            </button>
          ))}
        </div>
      </section>
      <p className="m-facts-line">
        {facts.party ? <span>Watch party:<b>{facts.party}</b></span> : null}
        {facts.cleanup ? <span>{facts.cleanup}</span> : null}
        {checked ? <span className="m-facts-line__checked">Checked {checked}</span> : null}
      </p>

      <div className="m-panes">
        <section ref={column} className="m-queue-col" aria-labelledby="m-queue-h">
          <h2 id="m-queue-h" className="visually-hidden" ref={heading} tabIndex={-1}>{VIEW_LABEL[view]}</h2>
          <div className="m-seg3" role="group" aria-label="Show">
            {(["needs", "waiting", "done"] as DeskView[]).map((v) => (
              <button key={v} type="button" aria-pressed={view === v} onClick={() => showView(v)}>
                {v === "needs" ? "Needs you" : v === "waiting" ? "Waiting" : "Done"}
                {v === "done" ? null : <span className="m-seg3__n">{loading ? "–" : v === "needs" ? desk.counts.needs : desk.counts.waiting}</span>}
              </button>
            ))}
          </div>
          <p className="visually-hidden" aria-live="polite">{filtered ? `Showing ${label} only, ${list.length} items.` : ""}</p>
          {filtered ? (
            <div className="m-filterline">
              <span>Showing <b>{label}</b> only</span>
              <button type="button" className="btn btn--quiet m-btn m-rowbtn" onClick={() => toggle(null)}>Show all</button>
            </div>
          ) : null}
          <div className="m-queue" aria-busy={loading} onClickCapture={remember} onKeyDownCapture={remember}>
            {/* A notice takes the slot the loading queue kept for it, so it never pushes the rows down. */}
            {loading ? null : missing.length ? (
              <div className="m-queue__notice"><LoadFailed text={`Couldn’t load ${listed(missing.map((f) => FEED_NAME[f]))}, so the queue may be missing some of what needs you.`}
                onRetry={() => onRetry(missing)} /></div>
            ) : stale ? <div className="m-queue__notice"><Stale at={stale.at} onRetry={() => onRetry("stale")} /></div> : null}
            {/* Until everything has answered: rows the size of the real ones, swapped in once. */}
            {loading ? (
              <div className="m-group" aria-hidden>
                {/* The slot a "couldn't load" notice would take, so it never pushes the queue down. */}
                <div className="m-skel-notice" />
                <div className="m-group__head"><span className="skeleton m-skel-caps" /></div>
                <ul className="m-rows2">{[0, 1, 2, 3].map((n) => <li key={n} className="skeleton m-skel-row" />)}</ul>
              </div>
            ) : groups.map((g) => {
              const rows = list.filter((i) => i.group === g);
              if (!rows.length) return null;
              return (
                <section key={g} className={`m-group m-group--${g}`} aria-labelledby={`m-group-${g}`}>
                  <div className="m-group__head"><h3 id={`m-group-${g}`}>{GROUP_HEAD[g].label}</h3><span className="m-group__count">{rows.length}</span><span className="m-group__rule">{GROUP_HEAD[g].rule}</span></div>
                  <ul className="m-rows2">
                    {rows.map((item) => (
                      <QueueRow key={item.key} item={item} selected={wide && current?.key === item.key} onSelect={() => select(item, true)}
                        actions={<RowActions item={item} a={a} selected={wide && current?.key === item.key} open={() => select(item, true)}
                          look={() => { select(item, true); setLook(item.key); }}
                          reply={() => { select(item, true); setReply((r) => ({ key: item.key, n: r.n + 1 })); }} />} />
                    ))}
                  </ul>
                </section>
              );
            })}
            {/* "All clear" only when everything answered and there's truly nothing. */}
            {!loading && !missing.length && !list.length ? (
              <AllClear title="All clear">
                {view === "waiting" ? "Nobody owes you an answer." : view === "done" ? "Nothing decided, solved or marked done in the last two days."
                  : filtered ? `Nothing in ${label} needs you.` : "Nothing needs you right now."}
              </AllClear>
            ) : null}
          </div>
          {view === "needs" && list.length && !loading ? (
            <p className="m-queue__end">
              <Check size={18} aria-hidden />
              {filtered ? <span>That’s all for {label}. {desk.needs.length - list.length}{" "}more in the full queue.</span>
                : up.length ? <span>That’s all. {up.length > 1 ? `${up.slice(0, -1).join(", ")} and ${up[up.length - 1]}` : up[0]} {up.length > 1 ? "are" : "is"} up.</span> : <span>That’s all.</span>}
            </p>
          ) : null}
          {loading ? null : <div className="m-keys m-queue__keys" aria-hidden>
            <span><kbd>J</kbd><kbd>K</kbd> next, previous</span><span><kbd>Enter</kbd> open</span><span><kbd>E</kbd> done (tickets, chats)</span>
            <span><kbd>A</kbd> approve</span><span><kbd>1</kbd>–<kbd>5</kbd> filter</span><span><kbd>/</kbd> menu</span>
            <span>(in the queue, the monitors or the detail)</span>
          </div>}
        </section>
        {wide ? <div className="m-pane-wrap" onClickCapture={remember}><Detail {...detail} sheet={false} /></div> : null}
      </div>
      {!wide && sheet && current ? createPortal(<DetailSheet {...detail} sheet />, document.body) : null}
    </>
  );
}

/** The clock the queue's ages are told by, a little behind at most. */
function useNow(ms = 30_000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(t);
  }, [ms]);
  return now;
}

/** "19:53", as the studio clock in the masthead says it. */
const clock = (at: number) => new Date(at).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });

export function Manage() {
  const { session } = useSession();
  const [params, setParams] = useSearchParams();
  const asked = params.get("tab") ?? "";
  const part = params.get("part");
  // Only the tabs: a section loaded behind one (plexinvites) isn't a page of its own. With
  // none asked for, the front page: what needs you.
  const full = TABS.find((t) => t.id === asked)?.id ?? null;
  const tab: Section = full ?? "requests";
  useTitle(full ? `Manage: ${TABS.find((t) => t.id === tab)?.label}` : "Manage");
  const panel = useRef<HTMLDivElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const reduced = useReducedMotion();
  const now = useNow();
  const [menuOpen, setMenuOpen] = useState(false);
  // The strip's ambient light holds still while the tab is out of sight.
  useEffect(() => {
    const on = () => document.documentElement.classList.toggle("is-hidden", document.hidden);
    document.addEventListener("visibilitychange", on);
    return () => { document.removeEventListener("visibilitychange", on); document.documentElement.classList.remove("is-hidden"); };
  }, []);
  // "/" opens Everything else from a full view (the queue has its own, beside its other keys).
  useEffect(() => {
    const on = (e: KeyboardEvent) => { if (e.key === "/" && !keysOff(e, "#m-panel, .page-head")) { e.preventDefault(); setMenuOpen(true); } };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);
  // A view picked from the menu, landed once it's rendered: focus on its heading, the
  // page at the top of its content (or at the part asked for).
  const landing = useRef<Section | null>(null);
  const land = useCallback(() => {
    landing.current = null;
    heading.current?.focus({ preventScroll: true });
    panel.current?.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "start" });
  }, [reduced]);
  useEffect(() => { if (landing.current === tab) land(); }, [tab, land]);

  const [data, setData] = useState<Store>({});
  const [failed, setFailed] = useState<Partial<Record<Section, boolean>>>({});
  // When each section last loaded, for "Couldn't refresh, last updated 14:32".
  const [loadedAt, setLoadedAt] = useState<Partial<Record<Section, number>>>({});
  // Toasts live in ToastHost; this hands its add function to every tab, unchanged across renders.
  const pushToast = useRef<(t: ToastIn) => void>(() => {});
  const registerToasts = useCallback((push: (t: ToastIn) => void) => { pushToast.current = push; }, []);
  const toast = useCallback((t: ToastIn) => pushToast.current(t), []);
  // A new invite link, held here so it's still shown after switching sections, and lands
  // even when it's made after switching away.
  const [freshInvite, setFreshInvite] = useState<NewInvite | null>(null);
  // Queue rows whose decision is waiting out its Undo: out of the queue and every count.
  const [hidden, setHidden] = useState<ReadonlySet<string>>(() => new Set());
  const show = useCallback((key: string) => setHidden((h) => { const n = new Set(h); n.delete(key); return n; }), []);
  const defer = useCallback((key: string, text: string, run: () => Promise<unknown>) => {
    setHidden((h) => new Set(h).add(key));
    buzz(12);
    // Whatever happens, the row comes back off hold: gone from the data once it's done,
    // back in the queue if it didn't go through (the error says so).
    toast({ text, undo: () => show(key), onGone: () => { void run().finally(() => show(key)); } });
  }, [toast, show]);

  // Each section's newest ask (`n`) and when it went. An answer to an older ask, or to one
  // sent before a change made here, is dropped, so a slow reply never puts back what's
  // already been decided.
  const asks = useRef<Partial<Record<Section, { n: number; at: number }>>>({});
  const refresh = useCallback(async (s: Section) => {
    const n = (asks.current[s]?.n ?? 0) + 1;
    asks.current[s] = { n, at: Date.now() };
    const latest = () => asks.current[s]?.n === n;
    try {
      const d = await api.admin<unknown>(s as Exclude<Section, "security">);
      if (!latest()) return;
      setData((prev) => ({ ...prev, [s]: d }));
      setFailed((f) => ({ ...f, [s]: false }));
      setLoadedAt((at) => ({ ...at, [s]: Date.now() }));
    } catch {
      if (latest()) setFailed((f) => ({ ...f, [s]: true }));
    }
  }, []);
  const patch = useCallback(<K extends keyof Store>(s: K, fn: (d: NonNullable<Store[K]>) => NonNullable<Store[K]>) => {
    const ask = asks.current[s];
    if (ask) ask.n += 1;
    setData((prev) => (prev[s] ? { ...prev, [s]: fn(prev[s] as NonNullable<Store[K]>) } : prev));
  }, []);

  // Blocked imports: on opening, and with the services at most every five minutes while
  // the page stays open (each ask reads Sonarr's and Radarr's queues).
  const [blocked, setBlocked] = useState<BlockedRow[] | undefined>(undefined);
  const [blockedFailed, setBlockedFailed] = useState(false);
  const [blockedLoadedAt, setBlockedLoadedAt] = useState<number | undefined>(undefined);
  const blockedAt = useRef(0);
  const loadBlocked = useCallback(() => {
    blockedAt.current = Date.now();
    // A failure keeps the last answer and says so.
    return api.adminBlocked().then((b) => { setBlocked(b.rows); setBlockedFailed(false); setBlockedLoadedAt(Date.now()); }, () => setBlockedFailed(true));
  }, []);

  const admin = !!session?.admin;
  /** Asks again for what wasn't asked for in the last few seconds (coming back fires both focus and visibilitychange). */
  const freshen = useCallback((sections: Section[]) => {
    sections.forEach((s) => { if (Date.now() - (asks.current[s]?.at ?? 0) > 10_000) void refresh(s); });
  }, [refresh]);
  const shown = useRef(tab);
  useEffect(() => {
    if (!admin) return;
    SECTIONS.forEach((s) => void refresh(s));
    void loadBlocked();
    // Kept current while it's open: what's on screen and what the queue, monitors and menu
    // count, every minute while the page is visible, and again on coming back to it.
    // Pending Plex invites mean signing in to plex.tv: asked on opening Invites and on
    // coming back, not every minute.
    const due = (poll: boolean) => [...new Set([...shows(shown.current), ...COUNTED])].filter((s) => !poll || s !== "plexinvites");
    const tick = () => { if (!document.hidden) freshen(due(false)); };
    const t = window.setInterval(() => { if (!document.hidden) freshen(due(true)); }, 60_000);
    document.addEventListener("visibilitychange", tick);
    window.addEventListener("focus", tick);
    return () => {
      window.clearInterval(t);
      document.removeEventListener("visibilitychange", tick);
      window.removeEventListener("focus", tick);
    };
  }, [admin, refresh, freshen, loadBlocked]);
  useEffect(() => { if (admin && data.health && Date.now() - blockedAt.current > 300_000) void loadBlocked(); }, [admin, data.health, loadBlocked]);
  // A tab opened later shows what's true now, not what was true when the page loaded.
  useEffect(() => {
    shown.current = tab;
    if (admin) freshen(shows(tab));
  }, [admin, tab, freshen]);
  // The part of a view the menu asked for, once it's on the page.
  const parted = useRef("");
  // Once everything the view shows has loaded, so nothing above it still grows.
  const ready = shows(tab).every((s) => data[s as keyof Store] !== undefined || failed[s]);
  useEffect(() => {
    const want = `${tab}|${part}`;
    if (!part || !ready || parted.current === want) return;
    const t = window.setTimeout(() => {
      const el = document.getElementById(`m-part-${part}`);
      if (!el) return;
      parted.current = want;
      el.scrollIntoView({ block: "start", behavior: reduced ? "auto" : "smooth" });
    }, 250);
    return () => window.clearTimeout(t);
  }, [tab, part, ready, data, reduced]);

  if (!admin) {
    return <div className="shell page"><p className="muted">This page is for admins.</p></div>;
  }

  // From the menu: a new page of history (Back returns), at the top of the view, or its part.
  const pick = (row: MenuRow) => {
    const t = row.tab as Section;
    if (full && t === tab && (row.part ?? null) === part) { requestAnimationFrame(land); return; }
    parted.current = "";
    if (!row.part) landing.current = t;
    setParams(() => { const p = new URLSearchParams(); p.set("tab", t); if (row.part) p.set("part", row.part); return p; });
  };
  const deskData: DeskData = { ...data, blocked };
  const feedFailed = { ...failed, blocked: blockedFailed } as Partial<Record<Feed, boolean>>;
  const desk = deriveDesk(deskData, now, { me: session?.user.name ?? null, hidden, failed: feedFailed });
  // Refreshes that failed over what's already here: the desk keeps it and says how old it is.
  const staleFeeds = DESK_FEEDS.filter((f) => feedFailed[f] && deskData[f] !== undefined);
  const retry = (feeds: Feed[] | "stale") => Promise.all((feeds === "stale" ? staleFeeds : feeds)
    .map((f) => (f === "blocked" ? loadBlocked() : refresh(f as Section))));
  const groups = menuGroups(desk, deskData);
  const facts = [desk.facts.party ? `Watch party: ${desk.facts.party}.` : "",
    desk.facts.cleanup ? `${desk.facts.cleanup}.` : "", loadedAt.health ? `Checked ${clock(loadedAt.health)}, again every minute.` : ""].filter(Boolean).join(" ");
  const label = TABS.find((t) => t.id === tab)?.label ?? "";
  const ctx: ManageState = { data, refresh, patch, toast, readOnly: !!session?.preview, failed, freshInvite, setFreshInvite, defer,
    blocked: { rows: blocked, failed: blockedFailed } };
  // A refresh of this tab failed over what's already on screen: say so, and how old it is.
  const stale = shows(tab).filter((s) => failed[s] && data[s as keyof Store]);

  const menu = <EverythingElse groups={groups} dot={!desk.loading && desk.needs.length > 0} hot={!desk.loading && desk.urgent > 0} facts={facts} onPick={pick}
    open={menuOpen} setOpen={setMenuOpen} />;

  if (!full) {
    return (
      <ManageCtx.Provider value={ctx}>
        <div className="shell page m-page m-page--desk">
          <ManageDesk desk={desk} checked={loadedAt.health ? clock(loadedAt.health) : null} menu={menu} openMenu={() => setMenuOpen(true)}
            stale={staleFeeds.length ? { at: Math.min(...staleFeeds.map((f) => (f === "blocked" ? blockedLoadedAt ?? 0 : loadedAt[f as Section] ?? 0))) || undefined } : null}
            onRetry={retry} />
        </div>
        <ToastHost register={registerToasts} />
      </ManageCtx.Provider>
    );
  }

  return (
    <ManageCtx.Provider value={ctx}>
      <div className="shell page m-page">
        <div className="page-head">
          <div className="m-titlebar">
            <h1 className="m-h1">Manage</h1>
            {menu}
          </div>
          <Link to="/manage" className="m-back"><ArrowLeft size={16} aria-hidden /> What needs you</Link>
          <h2 id="m-section" ref={heading} tabIndex={-1} className="m-section">{label}</h2>
        </div>

        <div role="region" id="m-panel" ref={panel} aria-labelledby="m-section">
          {failed[tab] && !data[tab as keyof Store] ? (
            <LoadFailed text="Couldn’t load this. Plexbie may be restarting." onRetry={() => refresh(tab)} />
          ) : (
            <>
            {stale.length ? <Stale at={Math.min(...stale.map((s) => loadedAt[s] ?? 0)) || undefined} onRetry={() => Promise.all(stale.map((s) => refresh(s)))} /> : null}
            <motion.div
              key={tab}
              initial={reduced ? { opacity: 0 } : { opacity: 0, transform: "translateY(6px)" }}
              animate={reduced ? { opacity: 1 } : { opacity: 1, transform: "translateY(0px)" }}
              transition={{ duration: 0.2, ease: EASE_OUT }}
            >
              <FullView tab={tab} />
            </motion.div>
            </>
          )}
        </div>
      </div>
      <ToastHost register={registerToasts} />
    </ManageCtx.Provider>
  );
}

/** One section in full, as the menu (and a push alert's ?tab=) opens it. */
function FullView({ tab }: { tab: Section }) {
  const { data, failed, blocked } = useManage();
  // Until its first answer: cards at their final height, so nothing below jumps when it lands.
  const first = shows(tab)[0] as keyof Store | undefined;
  const waitBlocked = tab === "health" && blocked.rows === undefined && !blocked.failed;
  if ((first && data[first] === undefined && !failed[first as Section]) || waitBlocked) {
    return <div className="m-stack" aria-busy="true" aria-label="Loading">{[0, 1, 2].map((n) => <div key={n} className="skeleton m-skel-card" />)}</div>;
  }
  return tab === "requests" ? <RequestsTab /> : tab === "all" ? <AllRequestsTab /> : tab === "tickets" ? <TicketsTab />
    : tab === "joins" ? <JoinsTab /> : tab === "invites" ? <InvitesTab /> : tab === "people" ? <PeopleTab />
      : tab === "cleanup" ? <CleanupTab /> : tab === "discord" ? <DiscordTab /> : tab === "messages" ? <MessagesTab />
        : tab === "security" ? <SignOutOthers /> : <HealthTab />;
}
