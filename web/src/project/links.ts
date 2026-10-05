// The Plexbie project's own links, for its site (plexbie.com). Household installs
// never show these: they only credit the project with a "Powered by Plexbie" link.

export const GITHUB = "https://github.com/NovaOra/plexbie";
/** The household website in demo mode: invented people and titles, nothing behind it. */
export const DEMO_SITE = "https://demo.plexbie.com";
/** Who runs the project, and where to write. */
export const CONTACT = "support@plexbie.com";
export const OPERATOR = "NovaOra";
/** Whether the GitHub repository can be visited. Set at build time (VITE_REPO_PUBLIC=false
 *  while it's private), so its links are hidden rather than lead to a 404. */
export const REPO_PUBLIC = import.meta.env.VITE_REPO_PUBLIC !== "false";
/** Tips toward Plexbie's development (the software), never for anyone's Plex access. */
export const SUPPORT = {
  kofi: "https://ko-fi.com/novaora",
  coffee: "https://buymeacoffee.com/novaora",
};
