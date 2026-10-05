import { useEffect } from "react";
import { useLocation } from "react-router-dom";
import { pageview, startTracking } from "./project/track";

/** The public demo's visit counting (src/project/track.ts), the same as plexbie.com's.
 *  Only the demo build loads this: App.tsx imports it behind DEMO, which is false in
 *  every household build, so it never reaches anyone's own Plexbie. */
export default function DemoCounted() {
  const { pathname } = useLocation();
  useEffect(() => { startTracking(); }, []);
  useEffect(() => { pageview(); }, [pathname]);
  return null;
}
