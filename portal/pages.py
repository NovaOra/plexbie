# path: portal/pages.py
"""What crawlers and link previews see: each page's <head>, and robots.txt.

The website is one page of JavaScript, so a link preview (Discord, iMessage,
Slack, X), which doesn't run any, only ever sees index.html. The server fills
its <head> per address: a real title and description, and Open Graph and
Twitter tags with an absolute card image.

Every install is one household's own Plexbie, so the whole site stays out of
search (X-Robots-Tag noindex, and robots.txt turns crawlers away) while
link-preview bots are let in, so shared links still show a card. (The Plexbie
project's own site, plexbie.com, is a separate static build: web/project.)
Invite pages never carry anyone's name in what a preview can read.

The head also says who runs this Plexbie (SITE_OPERATOR, SITE_CONTACT), for the
site's Privacy and Terms.
"""
import html
import json
import re
from pathlib import Path
from typing import Optional, Tuple

#: The marker in web/index.html where the per-page tags go.
HEAD_MARK = "<!--plexbie:head-->"
CARD = "/brand/og-card.png"
CARD_ALT = "The Plexbie logo, a small pink television, beside the words: a Discord bot for your household’s Plex server."

APP = ("Plexbie", "The household’s Plex: requests, what’s new, and what’s on.")
#: The household's pages (the website's routes), and the pages anyone may read.
HOUSEHOLD = ("library", "search", "schedule", "channel", "manage", "alerts")
OPEN = {
    "privacy": ("Privacy · Plexbie", "What this Plexbie keeps about the people who use it, and why."),
    "terms": ("Terms · Plexbie", "The terms for using this Plexbie."),
}
#: Fixed words for invites: a preview must never show who is invited, or by whom.
INVITE = ("You’re invited · Plexbie", "An invite to a household’s Plex server.")
NOT_FOUND = ("Not found · Plexbie", "There’s nothing at this address.")

#: Link-preview bots allowed in, though the site stays out of search.
PREVIEW_BOTS = ("Twitterbot", "facebookexternalhit", "Slackbot", "Slackbot-LinkExpanding", "Discordbot",
                "TelegramBot", "WhatsApp", "LinkedInBot")
PRIVATE_PATHS = ("/api/", "/auth/", "/img/", "/setup/")


def route_of(path: str) -> Tuple[str, Optional[Tuple[str, str]]]:
    """("open"|"invite"|"app"|"missing", (title, description)) for a page address."""
    p = path.strip("/")
    if p in OPEN:
        return "open", OPEN[p]
    if p == "invite" or p.startswith("invite/"):
        return "invite", INVITE
    if not p or p in HOUSEHOLD or p.startswith("title/"):
        return "app", APP
    return "missing", NOT_FOUND


def head_tags(path: str, origin: str, *, operator: str = "", contact: str = "") -> Tuple[int, str, str]:
    """(status, <title>, the tags for HEAD_MARK) for one address."""
    kind, (title, description) = route_of(path)
    e = lambda s: html.escape(s, quote=True)
    card = f"{origin}{CARD}"
    site = json.dumps({"operator": operator, "contact": contact}, ensure_ascii=False)
    tags = [
        f'<meta name="description" content="{e(description)}" />',
        '<meta property="og:type" content="website" />',
        '<meta property="og:site_name" content="Plexbie" />',
        f'<meta property="og:title" content="{e(title)}" />',
        f'<meta property="og:description" content="{e(description)}" />',
        f'<meta property="og:image" content="{e(card)}" />',
        '<meta property="og:image:type" content="image/png" />',
        '<meta property="og:image:width" content="1200" />',
        '<meta property="og:image:height" content="630" />',
        f'<meta property="og:image:alt" content="{e(CARD_ALT)}" />',
        '<meta name="twitter:card" content="summary_large_image" />',
        f'<meta name="twitter:title" content="{e(title)}" />',
        f'<meta name="twitter:description" content="{e(description)}" />',
        f'<meta name="twitter:image" content="{e(card)}" />',
        '<meta name="robots" content="noindex, nofollow" />',
        f'<meta name="plexbie-site" content="{e(site)}" />',
    ]
    status = 404 if kind == "missing" else 200
    return status, title, "\n    ".join(tags)


class IndexPage:
    """web/dist/index.html with each address's head filled in (read once, reread when rebuilt)."""

    def __init__(self, file: Path):
        self.file = file
        self._text: Optional[str] = None
        self._mtime = 0.0

    def _template(self) -> str:
        mtime = self.file.stat().st_mtime
        if self._text is None or mtime != self._mtime:
            self._text, self._mtime = self.file.read_text(encoding="utf-8"), mtime
        return self._text

    def render(self, path: str, origin: str, *, operator: str = "", contact: str = "") -> Tuple[int, str]:
        status, title, tags = head_tags(path, origin, operator=operator, contact=contact)
        page = self._template()
        page = re.sub(r"<title>.*?</title>", f"<title>{html.escape(title)}</title>", page, count=1, flags=re.S)
        return status, page.replace(HEAD_MARK, tags, 1)


def robots_txt() -> str:
    private = "".join(f"Disallow: {p}\n" for p in PRIVATE_PATHS)
    bots = "".join(f"User-agent: {b}\n" for b in PREVIEW_BOTS)
    return ("# A household's own server: kept out of search, but link previews\n"
            "# (Discord, Slack, X, iMessage) may read pages so shared links show a card.\n"
            f"{bots}Allow: /\n{private}\nUser-agent: *\nDisallow: /\n")
