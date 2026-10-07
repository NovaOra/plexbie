import { useEffect, useRef, type ReactNode } from "react";
import { useReducedMotion } from "motion/react";

/**
 * A marquee you can grab. It drifts on its own, follows the pointer while
 * dragged, keeps the fling's momentum when released, then eases back to its
 * drift. A press that barely moves is a click and goes to onPick.
 *
 * It holds still while `paused`, while the mouse is over it, a finger is on it,
 * or keyboard focus is in it (each item is a button), and its frame loop stops
 * altogether while it's off screen. Under reduced motion it never drifts, but
 * dragging still works.
 */
export function DragCrawl<T>({
  items, render, onPick, speed = 38, reverse = false, label, paused = false,
}: {
  items: T[];
  render: (item: T, active: boolean) => ReactNode;
  onPick?: (item: T) => void;
  speed?: number;
  reverse?: boolean;
  label: string;
  paused?: boolean;
}) {
  const reduced = useReducedMotion();
  const root = useRef<HTMLDivElement>(null);
  const track = useRef<HTMLDivElement>(null);
  const copy = useRef<HTMLDivElement>(null);
  const st = useRef({ x: 0, vel: 0, drag: false, hold: false, focus: false, startX: 0, startPointer: 0, lastPointer: 0, lastT: 0, moved: 0, picked: false });
  const drift = (reverse ? 1 : -1) * (reduced || paused ? 0 : speed);

  useEffect(() => {
    let raf = 0;
    let prev = performance.now();
    let visible = false;
    const s = st.current;
    const tick = (now: number) => {
      const dt = Math.min(0.05, (now - prev) / 1000);
      prev = now;
      const width = copy.current?.offsetWidth ?? 0;
      if (!s.drag) {
        const target = s.hold || s.focus ? 0 : drift;
        s.vel += (target - s.vel) * Math.min(1, dt * 2.2);   // momentum decays toward the drift
        s.x += s.vel * dt;
      }
      if (width) {
        while (s.x <= -width) s.x += width;
        while (s.x > 0) s.x -= width;
      }
      if (track.current) track.current.style.transform = `translate3d(${s.x}px,0,0)`;
      // Settled and not drifting: stop asking for frames until something changes.
      raf = visible && (s.drag || Math.abs(s.vel) > 0.5 || drift !== 0) ? requestAnimationFrame(tick) : 0;
    };
    const start = () => { if (!raf && visible) { prev = performance.now(); raf = requestAnimationFrame(tick); } };
    const io = new IntersectionObserver(([e]) => { visible = e.isIntersecting; if (visible) start(); else { cancelAnimationFrame(raf); raf = 0; } });
    if (root.current) io.observe(root.current);
    (s as typeof s & { wake?: () => void }).wake = start;
    return () => { io.disconnect(); cancelAnimationFrame(raf); };
  }, [drift]);

  const wake = () => (st.current as typeof st.current & { wake?: () => void }).wake?.();

  return (
    <div
      ref={root}
      className="dcrawl"
      role="group"
      aria-label={label}
      onPointerEnter={(e) => { if (e.pointerType === "mouse") st.current.hold = true; }}
      onPointerLeave={() => { st.current.hold = false; wake(); }}
      onFocus={(e) => {
        // Keyboard focus only: a tapped or clicked item takes focus too, and it should drift on.
        st.current.focus = (e.target as HTMLElement).matches(":focus-visible");
        // Bring the focused item into view: the row may have drifted it off screen.
        const cell = (e.target as HTMLElement).closest<HTMLElement>(".dcrawl__cell");
        const box = root.current?.getBoundingClientRect();
        if (cell && box) {
          const r = cell.getBoundingClientRect();
          if (r.left < box.left + 16 || r.right > box.right - 16) st.current.x -= r.left - box.left - 24;
        }
        wake();
      }}
      onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node)) { st.current.focus = false; wake(); } }}
      onPointerDown={(e) => {
        const s = st.current;
        s.drag = true; s.hold = true; s.startX = s.x; s.startPointer = e.clientX; s.lastPointer = e.clientX; s.lastT = performance.now(); s.moved = 0;
        e.currentTarget.setPointerCapture(e.pointerId);
        wake();
      }}
      onPointerMove={(e) => {
        const s = st.current;
        if (!s.drag) return;
        const now = performance.now();
        s.x = s.startX + (e.clientX - s.startPointer);
        s.moved = Math.max(s.moved, Math.abs(e.clientX - s.startPointer));
        const dt = Math.max(1, now - s.lastT) / 1000;
        s.vel = (e.clientX - s.lastPointer) / dt;
        s.lastPointer = e.clientX; s.lastT = now;
      }}
      onPointerUp={(e) => {
        const s = st.current;
        s.drag = false;
        if (e.pointerType !== "mouse") s.hold = false;      // a finger lifted: drift again
        if (performance.now() - s.lastT > 80) s.vel = 0;     // held still before letting go: no fling
        s.vel = Math.max(-2400, Math.min(2400, s.vel));
        s.picked = false;
        if (s.moved < 6 && onPick) {
          const el = (document.elementFromPoint(e.clientX, e.clientY) as HTMLElement | null)?.closest<HTMLElement>("[data-i]");
          if (el) { onPick(items[Number(el.dataset.i)]); s.picked = true; }
        }
        wake();
      }}
      onPointerCancel={() => { st.current.drag = false; st.current.hold = false; wake(); }}
    >
      <div className="dcrawl__track" ref={track}>
        {[0, 1, 2].map((k) => (
          <div className="dcrawl__copy" key={k} ref={k === 0 ? copy : undefined} aria-hidden={k > 0 || undefined}>
            {items.map((item, n) => (
              <button key={n} type="button" data-i={n} className="dcrawl__cell" tabIndex={k === 0 ? 0 : -1}
                onClick={() => {
                  // A pointer press already picked it in onPointerUp; this is Enter or Space.
                  if (st.current.picked) { st.current.picked = false; return; }
                  onPick?.(item);
                }}>
                {render(item, false)}
              </button>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
