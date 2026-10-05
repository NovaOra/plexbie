import { Link } from "react-router-dom";
import { useTitle, Moment } from "../components/ui";

/** An address that isn't a page (the server answers 404 for it too). */
export function NotFound() {
  useTitle("Not found");
  return (
    <div className="shell page">
      <Moment>
        <h1 className="h3">This channel is off the air</h1>
        <p className="muted">There’s nothing at this address. It may have moved, or the link was mistyped.</p>
        <Link className="btn btn--primary" to="/">Back to Plexbie</Link>
      </Moment>
    </div>
  );
}
