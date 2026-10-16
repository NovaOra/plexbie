import { useEffect, useState } from "react";

/** Whether a media query matches now, kept up to date. */
export function useMedia(query: string) {
  const [on, setOn] = useState(() => typeof window !== "undefined" && window.matchMedia(query).matches);
  useEffect(() => {
    const q = window.matchMedia(query);
    const change = () => setOn(q.matches);
    change();
    q.addEventListener("change", change);
    return () => q.removeEventListener("change", change);
  }, [query]);
  return on;
}
