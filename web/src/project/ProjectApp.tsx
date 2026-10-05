import { useEffect } from "react";
import { BrowserRouter, Route, Routes, useLocation } from "react-router-dom";
import { NotFound } from "../pages/NotFound";
import { Project } from "./Project";
import { ProjectLayout } from "./ProjectLayout";
import { ProjectPrivacy, ProjectTerms } from "./ProjectLegal";
import { pageview, startTracking } from "./track";

/** Counts each page opened (src/project/track.ts). */
function Counted() {
  const { pathname } = useLocation();
  useEffect(() => { startTracking(); }, []);
  useEffect(() => { pageview(); }, [pathname]);
  return null;
}

/** plexbie.com: the Plexbie project's own site, a static build (npm run build:project)
 *  served by Cloudflare, apart from any household's server. */
export default function ProjectApp() {
  return (
    <BrowserRouter>
      <Counted />
      <Routes>
        <Route element={<ProjectLayout />}>
          <Route index element={<Project />} />
          <Route path="privacy" element={<ProjectPrivacy />} />
          <Route path="terms" element={<ProjectTerms />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
