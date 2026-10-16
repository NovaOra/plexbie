import { useEffect, useState } from "react";
import { Bell, BellOff, BellRing, Copy, Download, Share, SquarePlus } from "./icons";
import { api } from "../api/client";
import type { AppRelease } from "../api/types";
import { alertState, sendTest, turnOff, turnOn, type AlertState } from "../api/alerts";
import { useSession } from "./Layout";
import { useTitle } from "./ui";

type Device = "android" | "ios" | "other";

/** Which phone this is, for which key leads on the Alerts page. */
function deviceKind(): Device {
  const ua = navigator.userAgent;
  if (/android/i.test(ua)) return "android";
  if (/iphone|ipad|ipod/i.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)) return "ios";
  return "other";
}

/** A phone, not a tablet: iPads (Safari on iPad says it's a Mac) and Android tablets are "this device". */
function isPhone(): boolean {
  const ua = navigator.userAgent;
  return /iphone|ipod/i.test(ua) || (/android/i.test(ua) && /mobile/i.test(ua));
}

/** The page's one pink key goes to what this device can use; the rest are 48px outline keys. */
const keyClass = (lead: boolean) => `btn${lead ? " btn--primary" : " btn--quiet alerts__key"} m-btn`;

/**
 * Turn phone alerts on or off. Discord members already get DMs; this is how
 * everyone else hears that a request arrived or that their access needs a watch.
 */
export function AlertsPanel({ compact = false, known, lead = true, device = deviceKind() }: {
  compact?: boolean;
  /** Already looked up (the Alerts page waits for it, so nothing moves when it arrives). */
  known?: AlertState | null;
  /** Its Turn on key is the page's pink one. */
  lead?: boolean;
  device?: Device;
}) {
  const { session } = useSession();
  const [state, setState] = useState<AlertState | null>(known ?? null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const viaDiscord = session?.user.via === "discord";
  const who = session?.user.id ?? "";

  useEffect(() => { if (!known) alertState(who).then(setState, () => setState("unsupported")); }, [who, known]);

  const run = async (fn: () => Promise<AlertState | string>) => {
    setBusy(true);
    setNote("");
    try {
      const out = await fn();
      if (["unsupported", "install-first", "blocked", "off", "on"].includes(out)) setState(out as AlertState);
      else setNote(out);
    } catch (e) {
      setNote(e instanceof Error ? e.message : "That didn’t work.");
    } finally {
      setBusy(false);
    }
  };

  if (!state) return null;
  if (compact && (state === "on" || state === "unsupported")) return null;

  return (
    <section className={`alerts${state === "on" ? " is-on" : ""}`} aria-labelledby="alerts-h">
      <span className="alerts__icon" aria-hidden>{state === "on" ? <BellRing size={22} /> : state === "blocked" ? <BellOff size={22} /> : <Bell size={22} />}</span>
      <div className="alerts__body">
        <h2 id="alerts-h" className="h3">{state === "on" ? "Alerts are on" : device !== "other" && isPhone() ? "Get alerts on this phone" : "Get alerts on this device"}</h2>
        {state === "install-first" ? (
          <p className="muted">
            On iPhone or iPad, alerts work once Plexbie is on your Home Screen: tap <Share size={14} aria-label="Share" /> in Safari,
            then <b>Add to Home Screen</b> <SquarePlus size={14} aria-hidden />, open Plexbie from there and come back to this page.
          </p>
        ) : state === "blocked" ? (
          <p className="muted">Alerts are blocked for this site. Allow notifications for {location.host} in your browser or phone settings, then reload.</p>
        ) : state === "unsupported" ? (
          <p className="muted">This browser can’t show alerts. Try Chrome on Android, or Safari on an iPhone or iPad with Plexbie on the Home Screen.</p>
        ) : (
          <p className="muted">
            {state === "on" ? "You’ll hear when a request is decided or arrives" : "Find out when a request is decided or arrives"}
            {viaDiscord ? " (you also get these as Discord DMs)" : ", and get a heads-up before your access would lapse for not watching"}.
          </p>
        )}
        {state === "off" || state === "on" ? (
          <div className="alerts__actions">
            {state === "off" ? (
              <button type="button" className={keyClass(lead)} disabled={busy || !!session?.preview} onClick={() => run(() => turnOn(who))}>
                <Bell size={18} aria-hidden /> {busy ? "Turning on…" : "Turn on alerts"}
              </button>
            ) : (
              <>
                <button type="button" className={keyClass(false)} disabled={busy} onClick={() => run(sendTest)}>Send a test</button>
                <button type="button" className={keyClass(false)} disabled={busy} onClick={() => run(turnOff)}>Turn off</button>
              </>
            )}
          </div>
        ) : null}
        {note ? <p className="alerts__note" role="status">{note}</p> : null}
      </div>
    </section>
  );
}

/** The full page, reachable from the account menu. */
export function AlertsPage() {
  useTitle("Alerts");
  const { session } = useSession();
  const who = session?.user.id ?? "";
  const [device] = useState(deviceKind);
  // One look at the newest release, for both apps, and at this device's alerts: the page shows
  // once it knows both, so nothing moves in under the reader.
  const [app, setApp] = useState<AppRelease | null | undefined>(undefined);
  const [alerts, setAlerts] = useState<AlertState | null>(null);
  useEffect(() => {
    let live = true;
    api.appLatest().then((a) => { if (live) setApp(a); }, () => { if (live) setApp(null); });
    alertState(who).then((s) => { if (live) setAlerts(s); }, () => { if (live) setAlerts("unsupported"); });
    return () => { live = false; };
  }, [who]);
  const ready = app !== undefined && alerts !== null;
  return (
    <div className="shell page page--narrow">
      <h1 className="display page-title">Alerts</h1>
      {ready ? (
        <>
          <AlertsPanel known={alerts} device={device} lead={device === "other"} />
          {/* This device's app first. */}
          {device === "ios" ? <IphoneApp app={app ?? null} lead /> : <AppDownload app={app ?? null} lead={device === "android"} />}
          {device === "ios" ? <AppDownload app={app ?? null} lead={false} /> : <IphoneApp app={app ?? null} lead={false} />}
          <p className="muted text-small">
            Alerts come from this site itself, with no outside service involved. They’re per device: turn them on in each
            phone or browser you want them in.
          </p>
        </>
      ) : (
        <div className="alerts-wait" aria-busy="true" aria-label="Loading">
          <div className="skeleton alerts-wait__card" />
          <div className="skeleton alerts-wait__card" />
        </div>
      )}
    </div>
  );
}

/** The Android app, for members: its latest version, what's new, and a download
 *  link that's good for ten minutes (the server signs it; the file isn't public). */
export function AppDownload({ app, lead = true }: { app: AppRelease | null; lead?: boolean }) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  if (!app) return null;

  const download = async () => {
    setBusy(true);
    setNote("");
    try {
      const { url } = await api.appDownloadLink();
      window.location.assign(url);
      setNote("Downloading. Open the file when it’s done; your phone may ask to allow installing apps from your browser.");
    } catch {
      setNote("Couldn’t start the download. Try again in a moment.");
    } finally {
      setBusy(false);
    }
  };
  const whatsNew = app.notes.split("\n").map((l) => l.replace(/[*_`#>]/g, "").replace(/^\s*[-•]\s*/, "").trim())
    .filter((l) => l && !/^new in /i.test(l) && !/^(installing|checks)\b/i.test(l)).slice(0, 3);

  return (
    <section className="alerts is-on" aria-labelledby="app-h">
      <span className="alerts__icon" aria-hidden><Download size={22} /></span>
      <div className="alerts__body">
        <h2 id="app-h" className="h3">Plexbie for Android</h2>
        <p className="muted">
          Version {app.version}, {Math.round(app.size / 1_000_000)} MB. Requests, alerts and Manage in an app; it tells you when there’s a newer one.
        </p>
        {whatsNew.length ? <ul className="app-new">{whatsNew.map((l) => <li key={l}>{l}</li>)}</ul> : null}
        <div className="alerts__actions">
          <button type="button" className={keyClass(lead)} disabled={busy} onClick={() => void download()}>
            <Download size={18} aria-hidden /> {busy ? "Getting the link…" : `Download ${app.version}`}
          </button>
        </div>
        {note ? <p className="alerts__note" role="status">{note}</p> : null}
      </div>
    </section>
  );
}

/** iPhones can't install apps from a website, so Plexbie for iPhone comes through
 *  SideStore (or AltStore): it installs the app under the member's own Apple ID and
 *  keeps it signed. Each member gets their own source address, which SideStore checks
 *  for updates; it stops working if they're no longer on the household's Plex, or
 *  once they replace it (a copy got out, say). */
export function IphoneApp({ app, lead = true }: { app: AppRelease | null; lead?: boolean }) {
  const [busy, setBusy] = useState(false);
  const [armed, setArmed] = useState(false);
  const [note, setNote] = useState("");
  // An unanswered "press again" lapses, so a later stray tap can't replace the address.
  useEffect(() => {
    if (!armed) return;
    const t = window.setTimeout(() => { setArmed(false); setNote(""); }, 8000);
    return () => window.clearTimeout(t);
  }, [armed]);
  if (!app?.ios) return null;

  const source = async (then: "sidestore" | "altstore" | "copy") => {
    setArmed(false);
    setBusy(true);
    setNote("");
    try {
      const s = await api.appIosSource();
      if (then === "copy") {
        await navigator.clipboard.writeText(s.url);
        setNote("Copied. In SideStore or AltStore, open Sources, tap +, and paste it. It’s yours alone: don’t share it.");
      } else {
        window.location.assign(s[then]);
        setNote(`Opening ${then === "sidestore" ? "SideStore" : "AltStore"}. If nothing happens, install it first (step 1), or copy the address instead.`);
      }
    } catch {
      setNote("Couldn’t get your address just now. Try again in a moment.");
    } finally {
      setBusy(false);
    }
  };

  // Pressed twice: the first press only asks, since the old address stops at once.
  const replace = async () => {
    if (!armed) {
      setArmed(true);
      setNote("Press again to replace it. Your current address stops working at once, on every iPhone that has it.");
      return;
    }
    setArmed(false);
    setBusy(true);
    setNote("");
    try {
      await api.appIosSource(true);
      setNote("Replaced. Your old address no longer works. In SideStore or AltStore, remove Plexbie’s source, then add it again with the buttons above.");
    } catch {
      setNote("Couldn’t replace your address just now. Try again in a moment.");
    } finally {
      setBusy(false);
    }
  };

  const actions = (
    <div className="alerts__actions">
      <button type="button" className={keyClass(lead)} disabled={busy} onClick={() => void source("sidestore")}>
        <Download size={18} aria-hidden /> Add to SideStore
      </button>
      <button type="button" className={keyClass(false)} disabled={busy} onClick={() => void source("altstore")}>
        Add to AltStore
      </button>
      <button type="button" className={keyClass(false)} disabled={busy} onClick={() => void source("copy")}>
        <Copy size={18} aria-hidden /> Copy the address
      </button>
    </div>
  );
  // Replacing the address is for later, after the steps: it stops the old one on every iPhone at once.
  const replaceKey = (
    <div className="alerts__actions">
      <button type="button" className={keyClass(false)} disabled={busy} onClick={() => void replace()}
        onBlur={() => { if (armed) { setArmed(false); setNote(""); } }}>
        {armed ? "Press again to replace" : "Replace the address"}
      </button>
    </div>
  );
  return (
    <section className="alerts is-on" aria-labelledby="iphone-h">
      <span className="alerts__icon" aria-hidden><Download size={22} /></span>
      <div className="alerts__body">
        <h2 id="iphone-h" className="h3">Plexbie for iPhone</h2>
        {/* On an iPhone the keys come first, right under the heading, so the pink one is on the first screen. */}
        {lead ? (
          <>
            <p className="app-lead">Already have SideStore? Tap <b>Add to SideStore</b>. New to it? Set it up with the steps below first.</p>
            {actions}
          </>
        ) : null}
        <p className="muted">
          Version {app.version}, {Math.round(app.ios.size / 1_000_000)} MB. iPhones only install apps from the App Store, so
          Plexbie comes through SideStore, a free app that installs it under your own Apple ID and keeps it working.
        </p>
        <ol className="app-steps">
          <li>
            Install SideStore on your iPhone: follow the guide at{" "}
            <a href="https://sidestore.io" target="_blank" rel="noopener noreferrer">sidestore.io</a>. It needs a computer
            once, about 15 minutes; after that it runs on the phone alone. Then, in SideStore’s Settings, sign in with
            your Apple ID: SideStore signs Plexbie with it.
          </li>
          <li>Turn on Developer Mode: Settings, Privacy &amp; Security, Developer Mode. Your iPhone restarts.</li>
          <li>
            Open <strong>LocalDevVPN</strong> (SideStore’s setup has you install it) and tap <strong>Connect</strong>.
            SideStore can only install or renew apps while it’s connected.
          </li>
          <li>On your iPhone, tap <strong>Add to SideStore</strong> {lead ? "above" : "below"}, then install Plexbie from it.</li>
          <li>
            SideStore renews Plexbie every week and shows new versions as updates. If it ever says LocalDevVPN isn’t
            connected, open LocalDevVPN, tap Connect, and try again. If it fails with “OperationError error 28” (you
            aren’t signed in), sign in again under SideStore’s Settings.
          </li>
        </ol>
        {lead ? null : actions}
        {replaceKey}
        <p className="muted app-fine">
          With a free Apple ID an iPhone holds three of these apps, SideStore included. The app can’t get alerts on
          iPhone (Apple keeps that for its paid developer program), so for alerts, add this site to your Home Screen
          from Safari (Share, then Add to Home Screen) and turn them on at the top of this page. Links to this site open
          in Safari rather than the app.
        </p>
        {note ? <p className="alerts__note" role="status">{note}</p> : null}
      </div>
    </section>
  );
}
