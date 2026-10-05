import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { lazy, Suspense, type ReactNode } from "react";
import { Layout, useSession } from "./components/Layout";
import { InvitePage } from "./pages/InvitePage";
import { Join } from "./pages/Join";
import { Landing } from "./pages/Landing";
import { NotFound } from "./pages/NotFound";
import { Privacy, Terms } from "./pages/Legal";

// One household's Plexbie, at the root of its own address: signed out it's the
// sign-in page, signed in it's Home. (The Plexbie project's own site, plexbie.com,
// is a separate build: src/project.) The pages load when first opened: people not
// on Plex yet never download them, admin pages included.
const AlertsPage = lazy(() => import("./components/Alerts").then((m) => ({ default: m.AlertsPage })));
const Channel = lazy(() => import("./pages/Channel").then((m) => ({ default: m.Channel })));
const Home = lazy(() => import("./pages/Home").then((m) => ({ default: m.Home })));
const Library = lazy(() => import("./pages/Library").then((m) => ({ default: m.Library })));
const Manage = lazy(() => import("./pages/Manage").then((m) => ({ default: m.Manage })));
const Schedule = lazy(() => import("./pages/Schedule").then((m) => ({ default: m.Schedule })));
const SearchPage = lazy(() => import("./pages/SearchPage").then((m) => ({ default: m.SearchPage })));
const TitlePage = lazy(() => import("./pages/TitlePage").then((m) => ({ default: m.TitlePage })));

/** Logged out: landing. Logged in without the role: join. Member: the page. */
function Gate({ children }: { children: ReactNode }) {
  const { session } = useSession();
  if (!session) return <Landing />;
  if (!session.member) return <Join />;
  return <Suspense fallback={<div className="shell page" aria-busy="true"><div className="skeleton" style={{ height: 48, width: "40%" }} /></div>}>{children}</Suspense>;
}

/** Old addresses (/app/...) from before the household moved to the root: links in
 *  Discord, emails and bookmarks. The server forwards them too; this covers the rest. */
function FromApp() {
  const { pathname, search, hash } = useLocation();
  return <Navigate to={`${pathname.replace(/^\/app(?=\/|$)/, "") || "/"}${search}${hash}`} replace />;
}

function Routed() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Gate><Home /></Gate>} />
        <Route path="library" element={<Gate><Library /></Gate>} />
        <Route path="search" element={<Gate><SearchPage /></Gate>} />
        <Route path="title/:kind/:id" element={<Gate><TitlePage /></Gate>} />
        <Route path="schedule" element={<Gate><Schedule /></Gate>} />
        <Route path="channel" element={<Gate><Channel /></Gate>} />
        <Route path="manage" element={<Gate><Manage /></Gate>} />
        <Route path="alerts" element={<Gate><AlertsPage /></Gate>} />
        <Route path="privacy" element={<Privacy />} />
        <Route path="terms" element={<Terms />} />
        <Route path="invite" element={<InvitePage />} />
        <Route path="app/*" element={<FromApp />} />
        <Route path="app" element={<FromApp />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}

/** demo.plexbie.com only: its visits are counted like plexbie.com's. Never in a household build. */
// Checked against the build setting itself, so the household build drops it entirely.
const DemoCounted = import.meta.env.VITE_DEMO === "1" ? lazy(() => import("./DemoCounted")) : null;

export default function App() {
  return (
    <BrowserRouter>
      {DemoCounted && <Suspense fallback={null}><DemoCounted /></Suspense>}
      <Routed />
    </BrowserRouter>
  );
}
