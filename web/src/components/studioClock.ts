import { createContext, useContext, useEffect } from "react";

/** Set by Layout: whether the masthead shows the studio clock. */
export const StudioClockContext = createContext<(on: boolean) => void>(() => {});

/** The sign-in page's TV set: while it's on screen, the masthead shows a studio clock. */
export function useStudioClock() {
  const show = useContext(StudioClockContext);
  useEffect(() => {
    show(true);
    return () => show(false);
  }, [show]);
}
