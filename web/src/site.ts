// Who runs this Plexbie, from the server (portal/pages.py fills the plexbie-site tag
// from SITE_OPERATOR and SITE_CONTACT). Every install is one household's own site, so
// its Privacy and Terms name whoever runs it, never the Plexbie project.

interface SiteInfo {
  /** Who runs this server: a name, a household, or "" when it wasn't set. */
  operator: string;
  /** Where to write about it (an email address), or "". */
  contact: string;
}

function read(): SiteInfo {
  try {
    const raw = document.querySelector('meta[name="plexbie-site"]')?.getAttribute("content");
    const got = raw ? (JSON.parse(raw) as Partial<SiteInfo>) : {};
    return { operator: String(got.operator ?? "").trim(), contact: String(got.contact ?? "").trim() };
  } catch {
    return { operator: "", contact: "" };
  }
}

export const SITE: SiteInfo = typeof document === "undefined" ? { operator: "", contact: "" } : read();

/** The Plexbie project's own site, which every install credits. */
export const PROJECT_SITE = "https://plexbie.com";
