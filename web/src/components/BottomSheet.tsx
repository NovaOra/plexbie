import { useRef, type ReactNode } from "react";
import { motion, useDragControls, useReducedMotion } from "motion/react";
import { EASE_OUT } from "./motion";
import { useSheet } from "./overlay";

/**
 * A sheet that rises from the bottom over a dimmed page. Tap the scrim, press
 * Escape or Back, or drag it down by its handle or heading to close it. Only the
 * handle and heading drag, so a list inside it still scrolls.
 */
export function BottomSheet({ titleId, title, onClose, className, children }: {
  titleId: string; title: ReactNode; onClose: () => void; className?: string; children: ReactNode;
}) {
  const reduced = useReducedMotion();
  const panel = useRef<HTMLDivElement>(null);
  const drag = useDragControls();
  useSheet(panel, onClose);
  return (
    // Clicks stop here: a sheet opened from inside a link card mustn't follow the link.
    <motion.div className="m-sheet" role="dialog" aria-modal="true" aria-labelledby={titleId}
      initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0, transition: { duration: 0.15 } }}
      onClick={(e) => e.stopPropagation()}>
      <button type="button" className="m-sheet__scrim" aria-label="Close" onClick={onClose} />
      <motion.div ref={panel} className={className ? `m-sheet__panel ${className}` : "m-sheet__panel"}
        initial={reduced ? { opacity: 0 } : { transform: "translateY(100%)" }}
        animate={reduced ? { opacity: 1 } : { transform: "translateY(0%)" }}
        exit={reduced ? { opacity: 0 } : { transform: "translateY(100%)", transition: { duration: 0.2, ease: EASE_OUT } }}
        transition={{ type: "spring", duration: 0.45, bounce: 0.12 }}
        drag="y" dragListener={false} dragControls={drag} dragConstraints={{ top: 0, bottom: 0 }} dragElastic={{ top: 0, bottom: 0.6 }}
        onDragEnd={(_, i) => { if (i.offset.y > 120 || i.velocity.y > 600) onClose(); }}>
        <span className="m-sheet__grab" aria-hidden onPointerDown={(e) => drag.start(e)} />
        <h2 id={titleId} className="h3" tabIndex={-1} onPointerDown={(e) => drag.start(e)}>{title}</h2>
        {children}
      </motion.div>
    </motion.div>
  );
}
