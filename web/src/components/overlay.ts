import { useEffect, useRef, type RefObject } from "react";

let backTokens = 0;
/** An entry a closing overlay is about to take back off: reused if the same
 *  overlay mounts again at once (React's development double-mount does that). */
let leaving: { token: number; timer: number } | null = null;

/**
 * Android's Back button (and the browser's) closes an open overlay - a sheet, the
 * account menu, an open conversation - instead of leaving the page and losing
 * what was typed in it. Opening adds a history entry for the overlay; Back pops
 * it. Closing it any other way takes that entry back off.
 */
export function useBackToClose(open: boolean, onClose: () => void) {
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    if (!open) return;
    let token: number;
    if (leaving && window.history.state?.plexbieOverlay === leaving.token) {
      window.clearTimeout(leaving.timer);
      token = leaving.token;
    } else {
      token = ++backTokens;
      window.history.pushState({ ...(window.history.state ?? {}), plexbieOverlay: token }, "");
    }
    leaving = null;
    let popped = false;
    const onPop = () => { popped = true; close.current(); };
    window.addEventListener("popstate", onPop);
    return () => {
      window.removeEventListener("popstate", onPop);
      if (popped) return;
      // Still on this overlay's entry (closed by a tap, not Back, and no link was
      // followed): take the entry off so Back doesn't need pressing twice.
      const timer = window.setTimeout(() => {
        if (leaving?.token === token && window.history.state?.plexbieOverlay === token) window.history.back();
        if (leaving?.token === token) leaving = null;
      }, 0);
      leaving = { token, timer };
    };
  }, [open]);
}

/**
 * Runs `then` once a just-closed overlay's history entry is off again, so a
 * link it followed with `replace` replaces the page's own entry, not the
 * overlay's (which would leave an extra Back step behind). At once when no
 * overlay entry is open.
 */
export function afterOverlay(then: () => void) {
  if (!window.history.state?.plexbieOverlay) { then(); return; }
  let done = false;
  const run = () => {
    if (done) return;
    done = true;
    window.removeEventListener("popstate", run);
    window.clearTimeout(late);
    then();
  };
  window.addEventListener("popstate", run);
  // Should Back never come, the link still goes.
  const late = window.setTimeout(run, 500);
}

/**
 * A modal sheet, done properly: Back and Escape close it, focus moves into it on
 * open and back to whatever opened it on close, the page behind can't be
 * reached with Tab or scrolled while it's up.
 */
export function useSheet(panel: RefObject<HTMLElement | null>, onClose: () => void) {
  const close = useRef(onClose);
  close.current = onClose;
  useBackToClose(true, onClose);
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const root = document.getElementById("root");
    root?.setAttribute("inert", "");
    document.documentElement.classList.add("has-sheet");
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") close.current(); };
    window.addEventListener("keydown", onKey);
    const first = panel.current?.querySelector<HTMLElement>("[data-autofocus]") ?? panel.current?.querySelector<HTMLElement>("h2");
    first?.focus({ preventScroll: true });
    return () => {
      window.removeEventListener("keydown", onKey);
      root?.removeAttribute("inert");
      document.documentElement.classList.remove("has-sheet");
      if (opener?.isConnected) opener.focus({ preventScroll: true });
    };
  }, [panel]);
}
