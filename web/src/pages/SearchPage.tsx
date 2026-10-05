import { useSearchParams } from "react-router-dom";
import { RequestView, TYPES, type RequestType } from "../components/Discover";
import { useTitle } from "../components/ui";
import { Finder } from "./Home";

/** Request: one search box for everything, and Type, Language and Genre as small dropdowns. */
export function SearchPage() {
  useTitle("Request");
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  // ?kind= is how links said it before there was one search box.
  const legacy = params.get("kind");
  const wanted = params.get("type") ?? (legacy === "audiobook" || legacy === "ebook" ? "book" : legacy);
  const type: RequestType = TYPES.some(([t]) => t === wanted) ? (wanted as RequestType) : "all";
  const setType = (t: RequestType) => {
    const next = new URLSearchParams(params);
    next.delete("kind");
    if (t === "all") next.delete("type"); else next.set("type", t);
    setParams(next, { replace: true });
  };
  return (
    <div className="shell page">
      {q ? <h1 className="visually-hidden">Request: results for {q}</h1> : null}
      <Finder initialQuery={q} heading={!q} live type={type} />
      <RequestView q={q} type={type} setType={setType} />
    </div>
  );
}
