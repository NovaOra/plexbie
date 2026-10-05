import { useEffect, useRef, useState, type ReactNode } from "react";
import { AnimatePresence, motion, useInView, useReducedMotion } from "motion/react";
import { EASE_OUT, usePageHidden } from "./motion";
import { ChevronDown, ChevronUp, Power } from "./icons";

export interface TvChannel {
  name: string;
  render: () => ReactNode;
  /** Stay here: the set's own tour never flips away from this channel (a film). */
  hold?: boolean;
}

/** Live analog static on a small canvas, scaled up. Runs only while visible. */
function Static({ running }: { running: boolean }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    if (!running) return;
    const c = ref.current;
    const ctx = c?.getContext("2d");
    if (!c || !ctx) return;
    const w = (c.width = 160);
    const h = (c.height = 100);
    const img = ctx.createImageData(w, h);
    let raf = 0;
    let roll = 0;
    const draw = () => {
      roll = (roll + 3) % h;
      for (let y = 0; y < h; y++) {
        const band = Math.abs(y - roll) < 6 ? 40 : 0;
        for (let x = 0; x < w; x++) {
          const v = Math.random() * 215 + band;
          const i = (y * w + x) * 4;
          img.data[i] = v; img.data[i + 1] = v * 0.86; img.data[i + 2] = v * 0.93; img.data[i + 3] = 255;
        }
      }
      ctx.putImageData(img, 0, 0);
      raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, [running]);
  return <canvas ref={ref} className="set__static" aria-hidden />;
}

const SWITCH_MS = 420;

/**
 * A retro television drawn in code. Channel dial, CH up/down and power on the
 * side panel; static between channels; an on-screen channel number that fades.
 * A plain channel list underneath does the same job for anyone who'd rather not
 * fiddle with knobs, and everything works from the keyboard.
 */
/** `tuneTo` switches the set to a channel from outside (a button elsewhere on the page);
 *  `ask` changes each time it's asked, so asking twice for the same channel still counts. */
export function RetroTv({ channels, autoMs = 7000, tuneTo, onTune }: {
  channels: TvChannel[]; autoMs?: number; tuneTo?: { index: number; ask: number };
  /** Someone changed the channel (not the set's own tour), and to which. */
  onTune?: (name: string) => void;
}) {
  const reduced = useReducedMotion();
  const box = useRef<HTMLDivElement>(null);
  const inView = useInView(box, { margin: "-15% 0px" });
  const [ch, setCh] = useState(0);
  const [on, setOn] = useState(true);
  const [switching, setSwitching] = useState(false);
  const [osd, setOsd] = useState(0);
  const [touched, setTouched] = useState(false);
  const hidden = usePageHidden();

  const tune = (next: number, byUser = true) => {
    if (byUser) setTouched(true);
    const n = (next + channels.length) % channels.length;
    if (byUser) onTune?.(channels[n].name);
    if (!on) { setOn(true); setCh(n); setOsd((k) => k + 1); return; }
    if (n === ch) return;
    if (reduced) { setCh(n); setOsd((k) => k + 1); return; }
    setSwitching(true);
    window.setTimeout(() => { setCh(n); setSwitching(false); setOsd((k) => k + 1); }, SWITCH_MS);
  };

  useEffect(() => {
    if (tuneTo) tune(tuneTo.index);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tuneTo]);

  // Plays through the channels on its own until someone takes the controls, and rests on
  // a channel that holds (a film) rather than flipping away from it.
  useEffect(() => {
    if (touched || !on || !inView || hidden || reduced || channels[ch]?.hold) return;
    const t = window.setTimeout(() => tune(ch + 1, false), autoMs);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ch, touched, on, inView, hidden, reduced, autoMs, channels]);

  const angle = -135 + (270 / Math.max(1, channels.length - 1)) * ch;
  const current = channels[ch];

  return (
    <div className="set" ref={box}>
      <svg className="set__antenna" viewBox="0 0 300 120" aria-hidden>
        <path d="M150 112 L58 10" />
        <path d="M150 112 L248 22" />
        <circle cx="58" cy="10" r="7" />
        <circle cx="248" cy="22" r="7" />
        <path className="set__antenna-base" d="M112 120 Q150 92 188 120 Z" />
      </svg>

      <div className="set__cabinet">
        <div className="set__bezel">
          <div className={`set__screen${on ? "" : " is-off"}`} role="region" aria-roledescription="television" aria-label={on ? `Channel ${ch + 1}: ${current.name}` : "Television, switched off"} aria-live={touched ? "polite" : "off"}>
            <AnimatePresence mode="wait" initial={false}>
              {on && !switching ? (
                <motion.div
                  key={current.name}
                  className="set__program"
                  initial={reduced ? false : { opacity: 0, scale: 1.04, filter: "brightness(2.2)" }}
                  animate={{ opacity: 1, scale: 1, filter: "brightness(1)" }}
                  exit={reduced ? undefined : { opacity: 0, transition: { duration: 0.08 } }}
                  transition={{ duration: 0.35, ease: EASE_OUT }}
                >
                  {current.render()}
                </motion.div>
              ) : null}
            </AnimatePresence>
            {!reduced && on && switching ? <Static running={inView} /> : null}
            {on && !switching ? (
              <span className="set__osd" key={osd} aria-hidden>
                CH {String(ch + 1).padStart(2, "0")} <b>{current.name}</b>
              </span>
            ) : null}
            {!on ? <span className="set__off-dot" aria-hidden /> : null}
            <span className="set__glass" aria-hidden />
          </div>
        </div>

        <div className="set__panel">
          <svg className="set__grille" viewBox="0 0 100 60" aria-hidden preserveAspectRatio="none">
            {Array.from({ length: 7 }, (_, i) => <rect key={i} x="0" y={i * 9} width="100" height="4" rx="2" />)}
          </svg>
          <button
            type="button"
            className="set__dial"
            aria-label={`Channel dial. Channel ${ch + 1} of ${channels.length}. Press for the next channel.`}
            onClick={() => tune(ch + 1)}
            onKeyDown={(e) => {
              if (e.key === "ArrowUp" || e.key === "ArrowRight") { e.preventDefault(); tune(ch + 1); }
              if (e.key === "ArrowDown" || e.key === "ArrowLeft") { e.preventDefault(); tune(ch - 1); }
            }}
          >
            <svg viewBox="0 0 100 100" aria-hidden className="set__dial-face">
              {channels.map((_, i) => {
                const a = ((-135 + (270 / Math.max(1, channels.length - 1)) * i) - 90) * (Math.PI / 180);
                const x = 50 + Math.cos(a) * 41, y = 50 + Math.sin(a) * 41;
                return (
                  <g key={i}>
                    {/* The current channel: dark on a pink dot, readable on the silver dial. */}
                    {i === ch ? <circle cx={x} cy={y} r={6.5} className="set__dial-dot" /> : null}
                    <text x={x} y={y + 3.5} className={i === ch ? "is-on" : undefined}>{i + 1}</text>
                  </g>
                );
              })}
            </svg>
            <motion.span className="set__knob" animate={{ rotate: angle }} transition={reduced ? { duration: 0 } : { type: "spring", stiffness: 260, damping: 18 }}>
              <i />
            </motion.span>
          </button>
          <div className="set__rocker">
            <button type="button" aria-label="Channel up" onClick={() => tune(ch + 1)}><ChevronUp aria-hidden /></button>
            <button type="button" aria-label="Channel down" onClick={() => tune(ch - 1)}><ChevronDown aria-hidden /></button>
          </div>
          <button
            type="button"
            className="set__power"
            aria-label={on ? "Turn the television off" : "Turn the television on"}
            onClick={() => { setTouched(true); setOn((v) => !v); setSwitching(false); }}
          >
            <span className={`set__led${on ? " is-on" : ""}`} aria-hidden />
            <Power aria-hidden />
          </button>
        </div>
      </div>
      <div className="set__feet" aria-hidden><i /><i /></div>

      <div className="set__guide" role="group" aria-label="Channels">
        {channels.map((c, i) => (
          <button key={c.name} type="button" className="set__chan" aria-pressed={on && i === ch} onClick={() => tune(i)}>
            <span className="set__chan-n">{String(i + 1).padStart(2, "0")}</span>{c.name}
          </button>
        ))}
      </div>
    </div>
  );
}
