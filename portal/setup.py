# path: portal/setup.py
"""First-start setup in the browser.

Until Plexbie has a Discord bot token and a Plex token it can't start, so it
serves this instead, on the website's port: a step-by-step page that explains
where each key comes from, tests it, and writes it into config/.env. Discord
servers, channels and roles are picked from lists (no Developer Mode IDs), and
Plex is a "Sign in with Plex" button. Pressing Finish closes this server and the
bot carries on starting in the same process, so the real site takes the port.

Nobody is signed in yet, so the page asks for a setup code first: a new one is
printed to the container's log every time setup opens, so only someone who can
see that log can use it. Wrong codes are slowed down and, past a limit, refused
until a restart. Saved secrets are never sent back to the browser (the page sees
that a key is set, not what it is), never sent to an address other than the one
they were saved with, and the keys that sign logins and webhooks are made here,
never taken from the page.
"""
import asyncio
import io
import json
import os
import re
import secrets
import socket
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import aiohttp
from aiohttp import web
from dotenv import dotenv_values, load_dotenv

from core.blocking import run_blocking
from core.clients import ServiceError

PAGE = Path(__file__).with_name("setup.html")
DISCORD_API = "https://discord.com/api/v10"
PLEX_PRODUCT = "Plexbie"
#: The setup page's own scripts and styles are inline; nothing else may load, and
#: no other site may frame it.
PAGE_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

#: Asked for on the guided steps, and whether each is secret (never sent back).
FIELDS = {
    "DISCORD_BOT_TOKEN": True, "GUILD_ID": False, "BOT_OWNER_ID": False, "ADMIN_ROLE_ID": False,
    "ADMIN_CHANNEL_ID": False, "UPDATES_CHANNEL_ID": False,
    "PLEX_MEMBER_ROLE_ID": False, "ARRIVALS_ROLE_ID": False,
    "WATCH_PARTY_CHANNEL_ID": False, "STATS_CHANNEL_ID": False, "NOW_WATCHING_MESSAGE_ID": False,
    "LEADERBOARD_MESSAGE_ID": False, "WATCH_STREAK_MESSAGE_ID": False,
    "PLEX_URL": False, "PLEX_TOKEN": True, "PLEX_USERNAME": False, "PLEX_PASSWORD": True,
    "SEERR_URL": False, "SEERR_TOKEN": True,
    "SONARR_URL": False, "SONARR_TOKEN": True,
    "RADARR_URL": False, "RADARR_TOKEN": True,
    "SABNZBD_URL": False, "SABNZBD_API_KEY": True,
    "TAUTULLI_URL": False, "TAUTULLI_TOKEN": True,
    "TMDB_API_KEY": True,
    "WEB_PUBLIC_URL": False, "SITE_OPERATOR": False, "SITE_CONTACT": False,
    "DISCORD_CLIENT_ID": False, "DISCORD_CLIENT_SECRET": True,
    "DISCORD_CALLBACK_URL": False,
}
#: Where this install runs rather than what it connects to: not on the page and
#: not taken from an imported file (a port or path from another machine would
#: point at the wrong place here). Edited in config/.env when needed.
LOCAL = {"WEB_PORT", "WEB_BIND", "WEBHOOK_PORT", "WEBHOOK_BIND", "DB_URL", "WEB_DIST", "WEB_IMAGE_CACHE", "WEB_IMAGE_CACHE_MB", "TRUSTED_PROXIES",
         "BOOKSHELF_CACHE_DIR"}
#: Imported, but not worth a box on the page.
HIDDEN = {"WEB_SESSION_SECRET", "DISCORD_CALLBACK_URL", "WEB_ALIASES", "EXPO_ACCESS_TOKEN"}
#: Made by Plexbie, never set from the page: whoever chose these could sign in as
#: anyone (the session key) or forge webhooks. Webhook secrets may come from an
#: imported .env, so an old install's webhooks keep working.
SERVER_MADE = {"WEB_SESSION_SECRET", "SONARR_WEBHOOK_SECRET", "RADARR_WEBHOOK_SECRET", "TAUTULLI_WEBHOOK_SECRET",
               "SEERR_WEBHOOK_SECRET", "PLEX_WEBHOOK_SECRET"}
IMPORTABLE = SERVER_MADE - {"WEB_SESSION_SECRET"}

#: The setup code: unambiguous characters (no 0/O, 1/I), 16 of them = 80 bits.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 16
#: Wrong codes: after FREE_TRIES from one address, each try waits twice as long as
#: the last (up to MAX_WAIT seconds); after GLOBAL_TRIES in all, no code is
#: accepted for GLOBAL_PAUSE seconds (a pause, not until a restart, so nobody can
#: keep the owner out for good).
FREE_TRIES, MAX_WAIT, GLOBAL_TRIES, GLOBAL_PAUSE = 5, 900, 200, 900
#: Characters a .env value can't hold: a carriage return or other control
#: character would start a new line (python-dotenv splits on \r too), so a value
#: could add settings of its own.
_CONTROL = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]")


def new_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return "-".join(raw[i:i + 4] for i in range(0, CODE_LENGTH, 4))


def _plain_code(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def check_value(key: str, value: str) -> None:
    """Refuse a value that can't be written to .env as one line."""
    if _CONTROL.search(value or ""):
        raise ValueError(f"{key} has a line break or control character in it. Paste it again.")
#: Older names for the same setting, as found in long-lived .env files.
ALIASES = {"DISCORD_GUILD_ID": "GUILD_ID", "DISCORD_ADMIN_ROLE_ID": "ADMIN_ROLE_ID",
           "OVERSEERR_URL": "SEERR_URL", "OVERSEERR_TOKEN": "SEERR_TOKEN", "OVERSEERR_WEBHOOK_SECRET": "SEERR_WEBHOOK_SECRET"}
# The source tree's copy first (tests, running from a checkout); in the container the
# config folder is mounted over it, so the copy baked into the image is used.
TEMPLATES = (Path(__file__).resolve().parent.parent / "config" / ".env.example", Path("/app/defaults/.env.example"))


#: Plain-English labels on the "Everything else" step (the setting's name shows beside it).
LABELS = {
    "NZBHYDRA_URL": "NZBHydra address", "NZBHYDRA_API_KEY": "NZBHydra API key",
    "SMTP_HOST": "Mail server", "SMTP_PORT": "Port (587)",
    "SMTP_USERNAME": "Username (usually your email address)",
    "SMTP_PASSWORD": "Password or SMTP token (Proton: SMTP token; Gmail: app password; Brevo: SMTP key)",
    "SMTP_FROM": "Send as (optional, e.g. Plexbie <you@yourdomain.com>)",
    "BOOKSHELF_AUDIOBOOK_WATCH": "Finished audiobook downloads", "BOOKSHELF_EBOOK_WATCH": "Finished ebook downloads",
    "BOOKSHELF_AUDIOBOOK_LIBRARY": "Audiobook library", "BOOKSHELF_EBOOK_LIBRARY": "Ebook library",
    "BOOKSHELF_SETTLE_SECONDS": "Seconds a download must sit still before it's filed",
    "INACTIVITY_WARNING_DAYS": "Warn after this many days without watching",
    "INACTIVITY_REMOVAL_DAYS": "Remove after this many days without watching",
    "WEB_PUBLIC_URL": "Public address", "SITE_OPERATOR": "Who runs this Plexbie",
    "SITE_CONTACT": "Contact email for the Privacy page", "LOG_LEVEL": "Log detail (INFO or DEBUG)",
}
#: Folders the book settings can point at, if mounted (the Unraid template mounts /data).
FOLDER_ROOTS = ("/data", "/watch", "/library", "/media", "/mnt")


def _secret(key: str) -> bool:
    return any(w in key for w in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")) or key.endswith("_KEY")


def template_sections(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """The settings template as titled sections of documented keys.

    A section starts at a "# ---- TITLE" heading; comment lines right after a key
    explain it, comment lines before the first key explain the section.
    """
    path = path or next((p for p in TEMPLATES if p.is_file()), None)
    if not path:
        return []
    sections: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    last: Optional[Dict[str, Any]] = None
    opened = titled = False             # inside a ─── box / its title just read
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line.startswith("# ─"):
            opened, titled = not titled, False
            continue
        heading = re.match(r"#\s*-{3,}\s*(.+?)\s*-{3,}\s*$", line)
        if heading or (opened and line.startswith("#") and line[1:].strip()):
            title = re.sub(r"\s*\(.*\)\s*$", "", (heading.group(1) if heading else line[1:]).strip())
            cur = {"title": title[:1] + title[1:].lower() if title.isupper() else title, "note": [], "keys": []}
            sections.append(cur)
            opened, titled, last = False, not heading, None
            continue
        opened = titled = False
        m = re.match(r"([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m and cur is not None:
            last = {"key": m.group(1), "default": m.group(2).strip(), "help": []}
            cur["keys"].append(last)
        elif line.startswith("#") and cur is not None:
            (last["help"] if last else cur["note"]).append(line[1:].strip())
        elif not line:
            last = None
    for sec in sections:
        sec["note"] = " ".join(sec["note"]).strip()
        for k in sec["keys"]:
            k["help"] = " ".join(k["help"]).strip()
            k["secret"] = _secret(k["key"])
    return [s for s in sections if s["keys"]]


def allowed() -> Dict[str, bool]:
    """Every setting the page may write, and whether it's secret."""
    out = dict(FIELDS)
    for sec in template_sections():
        for k in sec["keys"]:
            if k["key"] not in LOCAL:
                out.setdefault(k["key"], k["secret"])
    return out


#: "Everything else" settings that a Check button can try, by the key that turns them on.
CHECKABLE = ("NZBHYDRA_URL", "SMTP_HOST",
             "BOOKSHELF_AUDIOBOOK_WATCH", "BOOKSHELF_EBOOK_WATCH", "BOOKSHELF_AUDIOBOOK_LIBRARY", "BOOKSHELF_EBOOK_LIBRARY")


def _smtp_login(host: str, port: int, user: str, password: str) -> None:
    import smtplib
    import ssl
    with smtplib.SMTP(host, port, timeout=15) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(user, password)


_BOOK_DIR = re.compile(r"^(audio[ _-]?books?|e[ _-]?books?|books?)$", re.I)
_DOWNLOADS = ("complete", "download", "usenet", "torrent", "sabnzbd", "nzbget", "incoming")


def _book_folders(limit: int = 40, depth: int = 5) -> List[str]:
    """Blocking: folders under the mounted roots named like a book category
    (audiobooks, ebooks, books), without looking inside them."""
    out: List[str] = []
    for root in FOLDER_ROOTS:
        base = Path(root)
        if not base.is_dir():
            continue
        stack = [(base, 0)]
        while stack and len(out) < limit:
            here, level = stack.pop()
            try:
                children = [c for c in here.iterdir() if c.is_dir() and not c.name.startswith(".")]
            except OSError:
                continue
            for child in children:
                if _BOOK_DIR.match(child.name):
                    out.append(str(child))
                elif level + 1 < depth:
                    stack.append((child, level + 1))
    return sorted(set(out))[:limit]


def suggest_book_paths(folders: List[str]) -> Dict[str, str]:
    """Which found folder is which: download folders by their path, libraries otherwise."""
    out: Dict[str, str] = {}
    for f in folders:
        low = f.lower()
        kind = "AUDIOBOOK" if "audio" in Path(f).name.lower() else "EBOOK" if "e" in Path(f).name.lower()[:2] else None
        if not kind:
            continue
        where = "WATCH" if any(w in low for w in _DOWNLOADS) else "LIBRARY"
        out.setdefault(f"BOOKSHELF_{kind}_{where}", f)
    return out


def _folder(path: str, write: bool) -> str:
    p = Path(path)
    if not p.is_dir():
        raise LookupError(f"{path} isn't there inside the container. Map that folder in the container's settings.")
    if write and not os.access(p, os.W_OK):
        raise LookupError(f"Plexbie can't write to {path}")
    return f"{path} is there" + (" and writable." if write else ".")


#: Each saved secret and the address it belongs with (see Setup._get).
SECRET_ADDRESS = {
    "PLEX_TOKEN": "PLEX_URL", "SEERR_TOKEN": "SEERR_URL", "SONARR_TOKEN": "SONARR_URL",
    "RADARR_TOKEN": "RADARR_URL", "SABNZBD_API_KEY": "SABNZBD_URL", "TAUTULLI_TOKEN": "TAUTULLI_URL",
    "NZBHYDRA_API_KEY": "NZBHYDRA_URL", "SMTP_PASSWORD": "SMTP_HOST", "SMTP_USERNAME": "SMTP_HOST",
}

#: Without these Plexbie can't start, so start-up opens the setup page. GUILD_ID isn't
#: here on purpose: a running install with it blank would stop at the setup page on
#: upgrade, bot down. Start-up picks its server instead, when that can be proved.
REQUIRED = ("DISCORD_BOT_TOKEN", "PLEX_URL", "PLEX_TOKEN")
#: What Finish asks for: the household's server too, since commands and admin
#: buttons only work there.
FINISH_REQUIRED = ("DISCORD_BOT_TOKEN", "GUILD_ID", "PLEX_URL", "PLEX_TOKEN")

#: Exactly what Plexbie uses in Discord (the invite link asks for these, so the
#: bot doesn't need Administrator). README "Discord permissions" says why for each.
PERMISSIONS = {
    "Create Invite": 1 << 0,          # invite links for people approved to join
    "Manage Channels": 1 << 4,        # "Set up this server for me" makes its channels
    "Manage Server": 1 << 5,          # reading invites, to see who brought whom
    "View Channels": 1 << 10,
    "Send Messages": 1 << 11,
    "Manage Messages": 1 << 13,       # tidying its own admin cards
    "Embed Links": 1 << 14,           # every card and board is an embed
    "Attach Files": 1 << 15,          # book covers
    "Read Message History": 1 << 16,  # finding its stats boards again
    "Manage Roles": 1 << 28,          # Plex Member, New on Plex, and making its roles
    "Create Public Threads": 1 << 35,  # a thread per person who DMs Plexbie, in the admin channel
    "Send Messages in Threads": 1 << 38,
}
BOT_PERMISSIONS = sum(PERMISSIONS.values())
VIEW, SEND, EMBED, ATTACH, HISTORY = 1 << 10, 1 << 11, 1 << 14, 1 << 15, 1 << 16
#: Application flags: either the full or the "limited" (under 100 servers) intent.
MEMBERS_INTENT = (1 << 14) | (1 << 15)


#: Present from the first thing the setup page saves until Finish: a restart in
#: between (an update, a reboot) returns to the setup page instead of starting the
#: bot half set up just because the required settings happen to be filled in.
IN_PROGRESS = ".setup-in-progress"        # next to config/.env


def needs_setup(config_dir: Path = Path("config")) -> bool:
    return (config_dir / IN_PROGRESS).exists() or not all(os.getenv(k) for k in REQUIRED)


def missing_to_finish() -> List[str]:
    """What Finish still needs. A server ID counts only as a number."""
    def given(key: str) -> bool:
        value = (os.getenv(key) or "").strip()
        return value.isdigit() if key == "GUILD_ID" else bool(value)
    return [k for k in FINISH_REQUIRED if not given(k)]


def write_env(path: Path, values: Dict[str, str]) -> None:
    """Set keys in a .env file in place, keeping its comments and order."""
    lines = path.read_text().splitlines() if path.exists() else []
    left = dict(values)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in left:
            lines[i] = f"{key}={_quote(left.pop(key))}"
    if left:
        lines += ["", "# Added by setup"] + [f"{k}={_quote(v)}" for k, v in left.items()]
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _quote(value: str) -> str:
    """A value as .env text. Single quotes keep it literal (no ${...} expansion);
    a quote or backslash inside is escaped rather than dropped."""
    value = value.strip()
    if _CONTROL.search(value):
        raise ValueError("A setting can't contain a line break or control character")
    if value and any(c in value for c in " #'\"\\$"):
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return value


def _lan_address() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "<this server>"


async def _plain_server_header(request, response) -> None:
    response.headers["Server"] = "Plexbie"


def _why(e: BaseException) -> str:
    """What went wrong reaching a service, without the address: aiohttp's errors
    include the full URL, and some services take their key in the query string."""
    if isinstance(e, aiohttp.ClientResponseError):
        return f"it answered {e.status}"
    if isinstance(e, (aiohttp.ClientConnectorError, aiohttp.ServerDisconnectedError, ConnectionError)):
        return "nothing answered at that address"
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return "it didn't answer in time"
    if isinstance(e, (aiohttp.InvalidURL, ValueError)):
        return "that address isn't valid"
    if isinstance(e, json.JSONDecodeError):
        return "it didn't answer like that app does"
    return type(e).__name__


class Setup:
    def __init__(self, env_file: Path, problem: Optional[str] = None):
        self.env_file = env_file
        self.problem = problem
        self.done = asyncio.Event()
        self.http: Optional[aiohttp.ClientSession] = None
        self.client_id = str(uuid.uuid4())
        self.pins: Dict[str, int] = {}
        self.sections = template_sections()
        self._saving = asyncio.Lock()
        # Always: on a first install whoever reaches the port first would set it up,
        # and a half-finished or reopened setup already holds saved secrets.
        self.code = new_code()
        self._fails: Dict[str, tuple] = {}       # address -> (failures, not before)
        self._fail_total = 0
        self._paused_until = 0.0
        self._tickets: Dict[str, float] = {}     # one-use Plex window tickets -> expiry
        self._probes: Dict[str, float] = {}      # address checks in flight -> when they started
        #: Plex addresses plex.tv listed for the signed-in account: the Plex token may
        #: go to any of them without being typed again.
        self.plex_urls: set = set()
        self.allowed = allowed()

    # ---------------------------------------------------------------- helpers
    def _get(self, key: str, given: Optional[Dict[str, Any]] = None) -> str:
        """A value from the request if typed there, else what's saved.

        A saved secret is never sent to an address other than the saved one: with a
        different address typed in, the secret has to be typed too. Otherwise
        "test Sonarr at http://someone-else" would hand them the saved key.
        """
        given = given or {}
        v = str(given.get(key) or "").strip()
        if v:
            return v
        partner = SECRET_ADDRESS.get(key)
        typed = str(given.get(partner) or "").strip().rstrip("/") if partner else ""
        # The Plex token may go to any address plex.tv listed for the signed-in
        # account (the server picked right after "Sign in with Plex").
        if partner and typed not in ("", os.getenv(partner, "").strip().rstrip("/")) \
                and not (key == "PLEX_TOKEN" and typed in self.plex_urls):
            return ""
        return os.getenv(key, "").strip()

    async def _save(self, values: Dict[str, str], imported: bool = False) -> None:
        ok = self.allowed
        writable = (lambda k: k in ok and (k not in SERVER_MADE or (imported and k in IMPORTABLE)))
        clean = {k: str(v).strip() for k, v in values.items() if writable(k) and v is not None}
        for k, v in clean.items():
            check_value(k, v)
        # A blank secret means "keep the saved one", not "clear it".
        clean = {k: v for k, v in clean.items() if v or not ok[k]}
        callback = clean.get("DISCORD_CALLBACK_URL")
        if callback and not re.fullmatch(r"https?://[^/\s]+/auth/discord/callback", callback):
            raise ValueError("DISCORD_CALLBACK_URL must be this site's address followed by /auth/discord/callback.")
        # A saved key never follows its address somewhere new: moving an address
        # clears the key that went with it unless a key is given with it (or, for
        # Plex, the new address is one plex.tv listed for the signed-in account).
        for secret, address in SECRET_ADDRESS.items():
            if address not in clean or secret in clean or not ok.get(secret):
                continue
            before = os.getenv(address, "").strip().rstrip("/")
            after = clean[address].strip().rstrip("/")
            if before and after != before and not (secret == "PLEX_TOKEN" and after in self.plex_urls):
                clean[secret] = ""
        if clean.get("WEB_PUBLIC_URL"):
            from core.config import public_url
            clean["WEB_PUBLIC_URL"] = public_url(clean["WEB_PUBLIC_URL"])
        if not clean:
            return
        async with self._saving:
            self.env_file.parent.mkdir(parents=True, exist_ok=True)
            (self.env_file.parent / IN_PROGRESS).touch()
            await run_blocking(write_env, self.env_file, clean)
        for k, v in clean.items():
            os.environ[k] = v

    async def _json(self, method: str, url: str, **kw) -> Any:
        async with self.http.request(method, url, timeout=aiohttp.ClientTimeout(total=12), **kw) as r:
            if r.status in (401, 403):
                raise PermissionError("That key was turned down")
            r.raise_for_status()
            return await r.json(content_type=None)

    def _discord(self, token: str) -> Dict[str, str]:
        return {"Authorization": f"Bot {token}"}

    # ----------------------------------------------------------------- routes
    async def page(self, request: web.Request) -> web.Response:
        return web.FileResponse(PAGE, headers={"Cache-Control": "no-store", "Content-Security-Policy": PAGE_CSP,
                                               "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})

    async def state(self, request: web.Request) -> web.Response:
        fields = {k: {"set": bool(os.getenv(k)), "value": None if secret else os.getenv(k, "")}
                  for k, secret in self.allowed.items()}
        return web.json_response({"fields": fields, "problem": self.problem,
                                  "missing": missing_to_finish()})

    async def save(self, request: web.Request) -> web.Response:
        body = await request.json()
        await self._save(body.get("values") or {})
        return await self.state(request)

    @web.middleware
    async def _refuse_bad_values(self, request: web.Request, handler):
        try:
            return await handler(request)
        except ValueError as e:
            if request.path.startswith("/setup/api/"):
                return web.json_response({"ok": False, "error": str(e)}, status=400)
            raise

    async def more(self, request: web.Request) -> web.Response:
        """Everything the guided steps don't ask about, by template section."""
        out = []
        for sec in self.sections:
            keys = [{"key": k["key"], "label": LABELS.get(k["key"]), "help": k["help"], "secret": k["secret"],
                     "default": "" if k["secret"] else k["default"]}
                    for k in sec["keys"] if k["key"] not in FIELDS and k["key"] not in LOCAL and k["key"] not in HIDDEN]
            if keys:
                out.append({"title": sec["title"], "note": sec["note"], "keys": keys,
                            "checkable": any(k["key"] in CHECKABLE for k in keys)})
        return web.json_response({"sections": out})

    async def check(self, request: web.Request) -> web.Response:
        """Try every checkable setting among `keys`, with typed or saved values."""
        body = await request.json()
        g = lambda k: self._get(k, body.get("values"))
        results = []
        for key in [k for k in body.get("keys") or [] if k in CHECKABLE]:
            name = (key[10:] if key.startswith("BOOKSHELF_") else key.rsplit("_", 1)[0]).replace("_", " ").capitalize()
            if not g(key):
                continue
            try:
                if key == "NZBHYDRA_URL":
                    async with self.http.get(f"{g(key).rstrip('/')}/api", params={"t": "caps", "apikey": g("NZBHYDRA_API_KEY")},
                                             timeout=aiohttp.ClientTimeout(total=12)) as r:
                        text = await r.text()
                        if r.status in (401, 403) or "<error" in text:
                            raise PermissionError("That key was turned down")
                        r.raise_for_status()
                    msg = "Connected to NZBHydra."
                elif key == "SMTP_HOST":
                    port = int(g("SMTP_PORT") or 587)
                    await run_blocking(_smtp_login, g(key), port, g("SMTP_USERNAME"), g("SMTP_PASSWORD"))
                    name, msg = "Email", f"Signed in to {g(key)}. Nothing was sent."
                else:
                    msg = await run_blocking(_folder, g(key), "LIBRARY" in key)
                results.append({"name": name, "ok": True, "message": msg})
            except Exception as e:
                if isinstance(e, PermissionError):
                    text = f"{e}. Copy the key again."
                elif isinstance(e, LookupError):
                    text = str(e)
                elif isinstance(e, (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError)):
                    text = f"Couldn't reach it ({_why(e)}). Check the address (http://, IP and port)."
                else:
                    text = type(e).__name__
                results.append({"name": name, "ok": False, "message": text})
        return web.json_response({"results": results})

    async def connect(self, request: web.Request) -> web.Response:
        """Turn on Seerr's or Tautulli's webhook, pointed at Plexbie (core/webhook_connect)."""
        from types import SimpleNamespace
        from core.clients import Seerr, Tautulli
        from core.webhook_connect import connect_seerr, connect_tautulli, new_secrets
        body = await request.json()
        what = body.get("service")
        if what not in ("seerr", "tautulli"):
            return web.json_response({"ok": False, "error": "Unknown service"}, status=400)
        await self._save(body.get("values") or {})
        ip = _lan_address()
        if ip.startswith("<"):
            return web.json_response({"ok": False, "error": "Plexbie couldn't work out this server's address on your network."})
        # Seerr and Tautulli must reach the listener: open it to the network
        # (every route has a secret by now), unless the operator already chose
        # addresses beyond loopback, which are kept as they are.
        binds = [a.strip() for a in (os.getenv("WEBHOOK_BIND") or "127.0.0.1").split(",") if a.strip()]
        widened = all(a in ("127.0.0.1", "::1", "localhost") for a in binds)
        local = {**new_secrets(os.environ), **({"WEBHOOK_BIND": "0.0.0.0"} if widened else {})}
        await run_blocking(write_env, self.env_file, local)
        os.environ.update(local)
        base = f"http://{ip}:{os.getenv('WEBHOOK_PORT') or 7980}"
        from core.config import env
        cfg = SimpleNamespace(seerr_url=env("SEERR_URL", ""), seerr_token=env("SEERR_TOKEN", ""),
                              tautulli_url=os.getenv("TAUTULLI_URL", ""), tautulli_token=os.getenv("TAUTULLI_TOKEN", ""))
        shim = SimpleNamespace(config=cfg, http_session=self.http)
        shim.seerr, shim.tautulli = Seerr(shim), Tautulli(shim)
        try:
            if what == "seerr":
                msg = await connect_seerr(shim, base, os.environ["SEERR_WEBHOOK_SECRET"])
            else:
                msg = await connect_tautulli(shim, base, os.environ["TAUTULLI_WEBHOOK_SECRET"])
        except ServiceError as e:
            return web.json_response({"ok": False, "error": f"{e}. Test the address and key above first."})
        return web.json_response({"ok": True, "message": f"{msg} Events go to {base}."})

    async def folders(self, request: web.Request) -> web.Response:
        """Book-looking folders inside the container, to pick instead of typing paths."""
        found = await run_blocking(_book_folders)
        return web.json_response({"roots": [r for r in FOLDER_ROOTS if Path(r).is_dir()], "folders": found,
                                  "suggested": suggest_book_paths(found)})

    async def import_env(self, request: web.Request) -> web.Response:
        """Take the settings from an existing .env (pasted or picked as a file)."""
        text = (await request.json()).get("text") or ""
        found = {k: (v or "").strip() for k, v in dotenv_values(stream=io.StringIO(text), interpolate=False).items()}
        for old, new in ALIASES.items():
            if found.get(old) and not found.get(new):
                found[new] = found[old]
        took = {k: v for k, v in found.items()
                if v and k in self.allowed and (k not in SERVER_MADE or k in IMPORTABLE)}
        public = took.get("WEB_PUBLIC_URL", "")
        if public and not public.startswith("https://"):
            took.pop("WEB_PUBLIC_URL")      # a LAN address of the old install, not a public name
        for k, v in took.items():
            check_value(k, v)
        await self._save(took, imported=True)
        skipped = sorted(k for k, v in found.items() if v and k not in took and k not in ALIASES)
        return web.json_response({"ok": True, "imported": len(took), "skipped": skipped})

    async def discord(self, request: web.Request) -> web.Response:
        """Check the bot token and list the servers it's in, with an invite link."""
        body = await request.json()
        token = self._get("DISCORD_BOT_TOKEN", body)
        if not token:
            return web.json_response({"ok": False, "error": "Paste the bot token first."})
        try:
            app = await self._json("GET", f"{DISCORD_API}/oauth2/applications/@me", headers=self._discord(token))
            guilds = await self._json("GET", f"{DISCORD_API}/users/@me/guilds", headers=self._discord(token))
        except PermissionError:
            return web.json_response({"ok": False, "error": "Discord didn't accept that token. Copy it again (Reset Token gives a fresh one)."})
        except Exception as e:
            return web.json_response({"ok": False, "error": f"Couldn't reach Discord: {e}"})
        await self._save({"DISCORD_BOT_TOKEN": token})
        flags = int(app.get("flags") or 0)
        missing = [name for name, bits in (("Server Members", MEMBERS_INTENT),)
                   if not flags & bits]
        owner = (app.get("team") or {}).get("owner_user_id") or (app.get("owner") or {}).get("id")
        if owner and not os.getenv("BOT_OWNER_ID"):
            await self._save({"BOT_OWNER_ID": str(owner)})
        if not os.getenv("DISCORD_CLIENT_ID"):
            await self._save({"DISCORD_CLIENT_ID": str(app["id"])})
        invite = "https://discord.com/oauth2/authorize?" + urlencode(
            {"client_id": app["id"], "scope": "bot applications.commands", "permissions": BOT_PERMISSIONS})
        return web.json_response({
            "ok": True, "name": app.get("name"), "invite": invite, "missingIntents": missing,
            "guilds": [{"id": g["id"], "name": g["name"]} for g in guilds],
            # On (Discord's default), anyone with the bot's ID can add it to their own server.
            "publicBot": bool(app.get("bot_public")),
        })

    async def discord_guild(self, request: web.Request) -> web.Response:
        """Channels and roles of the chosen server."""
        body = await request.json()
        token, gid = self._get("DISCORD_BOT_TOKEN"), str(body.get("guild") or "")
        try:
            chans = await self._json("GET", f"{DISCORD_API}/guilds/{int(gid)}/channels", headers=self._discord(token))
            roles = await self._json("GET", f"{DISCORD_API}/guilds/{int(gid)}/roles", headers=self._discord(token))
        except Exception as e:
            return web.json_response({"ok": False, "error": f"Couldn't read that server: {e}"})
        await self._save({"GUILD_ID": gid})
        ordered = sorted(chans, key=lambda c: c.get("position", 0))
        return web.json_response({
            "ok": True,
            "channels": [{"id": c["id"], "name": c["name"]} for c in ordered if c.get("type") in (0, 5)],
            "voice": [{"id": c["id"], "name": c["name"]} for c in ordered if c.get("type") in (2, 13)],
            "roles": [{"id": r["id"], "name": r["name"]} for r in sorted(roles, key=lambda r: -r.get("position", 0))
                      if r["name"] != "@everyone" and not r.get("managed")],
        })

    async def discord_messages(self, request: web.Request) -> web.Response:
        """The bot's own recent messages in a channel: the stats displays it may edit.

        Discord only lets a bot edit what it posted, so only its messages are
        offered. `create` posts three placeholders and saves their ids instead.
        """
        body = await request.json()
        token = self._get("DISCORD_BOT_TOKEN")
        try:
            cid = int(body.get("channel"))
            me = await self._json("GET", f"{DISCORD_API}/users/@me", headers=self._discord(token))
            if body.get("create"):
                made = {}
                for key, label in (("NOW_WATCHING_MESSAGE_ID", "Now watching"), ("LEADERBOARD_MESSAGE_ID", "Leaderboard"),
                                   ("WATCH_STREAK_MESSAGE_ID", "Watch streaks")):
                    msg = await self._json("POST", f"{DISCORD_API}/channels/{cid}/messages", headers=self._discord(token),
                                           json={"content": f"📊 {label} - Plexbie fills this in once it starts."})
                    made[key] = msg["id"]
                await self._save({"STATS_CHANNEL_ID": str(cid), **made})
            msgs = await self._json("GET", f"{DISCORD_API}/channels/{cid}/messages?limit=50", headers=self._discord(token))
        except PermissionError:
            return web.json_response({"ok": False, "error": "Plexbie can't read or post in that channel. Give its role View Channel, Send Messages and Read Message History there."})
        except Exception as e:
            return web.json_response({"ok": False, "error": f"Couldn't read that channel: {e}"})
        mine = []
        for m in msgs:
            if (m.get("author") or {}).get("id") != me["id"]:
                continue
            text = (m.get("content") or "").strip() or ((m.get("embeds") or [{}])[0].get("title") or "an embed")
            mine.append({"id": m["id"], "name": f"{text[:60]} ({m.get('timestamp', '')[:10]})"})
        return web.json_response({"ok": True, "messages": mine})

    async def discord_build(self, request: web.Request) -> web.Response:
        """Set the chosen server up for Plexbie: roles, a category with its channels, the
        stats boards and the arrivals button. Reuses anything already there by name,
        so pressing it twice changes nothing."""
        body = await request.json()
        token, h = self._get("DISCORD_BOT_TOKEN"), None
        try:
            gid = int(body.get("guild") or os.getenv("GUILD_ID") or 0)
        except ValueError:
            gid = 0
        if not (token and gid):
            return web.json_response({"ok": False, "error": "Pick your server first."})
        h = self._discord(token)
        api = DISCORD_API
        made, kept = [], []
        try:
            me = await self._json("GET", f"{api}/users/@me", headers=h)
            roles = {r["name"]: r for r in await self._json("GET", f"{api}/guilds/{gid}/roles", headers=h)}
            channels = await self._json("GET", f"{api}/guilds/{gid}/channels", headers=h)

            async def role(name: str, **extra) -> str:
                if name in roles:
                    kept.append(f"@{name}")
                    return roles[name]["id"]
                r = await self._json("POST", f"{api}/guilds/{gid}/roles", headers=h,
                                     json={"name": name, "permissions": "0", **extra})
                made.append(f"@{name}")
                return r["id"]

            admin_role = await role("Plexbie Admin", hoist=True, color=0xFF5C93)
            member_role = await role("Plex Member", color=0xFFD1E4)
            ping_role = await role("New on Plex", mentionable=True)

            def find(name: str, kind: int):
                return next((c for c in channels if c.get("name") == name and c.get("type") == kind), None)

            async def channel(name: str, kind: int, parent: Optional[str] = None, **extra) -> str:
                hit = find(name, kind)
                if hit:
                    kept.append(f"#{name}" if kind != 4 else name)
                    return hit["id"]
                c = await self._json("POST", f"{api}/guilds/{gid}/channels", headers=h,
                                     json={"name": name, "type": kind, **({"parent_id": parent} if parent else {}), **extra})
                made.append(f"#{name}" if kind != 4 else f"the {name} category")
                return c["id"]

            bot_can = {"id": me["id"], "type": 1, "allow": str(VIEW | SEND | EMBED | ATTACH | HISTORY), "deny": "0"}
            read_only = [{"id": str(gid), "type": 0, "allow": str(VIEW | HISTORY), "deny": str(SEND)}, bot_can]
            private = [{"id": str(gid), "type": 0, "allow": "0", "deny": str(VIEW)},
                       {"id": admin_role, "type": 0, "allow": str(VIEW | SEND | HISTORY), "deny": "0"}, bot_can]
            category = await channel("Plexbie", 4)
            arrivals = await channel("new-on-plex", 0, category, permission_overwrites=read_only,
                                     topic="New movies and shows on Plex. One post per arrival; later episodes update it.")
            stats = await channel("plex-stats", 0, category, permission_overwrites=read_only,
                                  topic="Who's watching, the leaderboard and watch streaks. Plexbie keeps these updated.")
            admin = await channel("plexbie-admin", 0, category, permission_overwrites=private,
                                  topic="Join requests, request approvals, help requests and health alerts.")
            party = await channel("Watch Party", 2, category)

            async def mine(cid: str) -> list:
                return [m for m in await self._json("GET", f"{api}/channels/{cid}/messages?limit=50", headers=h)
                        if (m.get("author") or {}).get("id") == me["id"]]

            # The three stats boards (Plexbie can only edit its own messages).
            boards = {}
            existing = await mine(stats)
            for key, label in (("NOW_WATCHING_MESSAGE_ID", "Now watching"), ("LEADERBOARD_MESSAGE_ID", "Leaderboard"),
                               ("WATCH_STREAK_MESSAGE_ID", "Watch streaks")):
                saved = os.getenv(key)
                if saved and any(m["id"] == saved for m in existing):
                    boards[key] = saved
                    continue
                msg = await self._json("POST", f"{api}/channels/{stats}/messages", headers=h,
                                       json={"content": f"📊 {label} - Plexbie fills this in once it starts."})
                boards[key] = msg["id"]
                made.append(f"the {label.lower()} board")

            # The opt-in arrivals button, once.
            button = "plexbie:arrivals_role"
            if not any(button in json.dumps(m.get("components") or []) for m in await mine(arrivals)):
                await self._json("POST", f"{api}/channels/{arrivals}/messages", headers=h, json={
                    "content": ("**New on Plex** lands here: one post per movie, and one per batch of a show's "
                                "episodes (later episodes update that post instead of posting again).\n"
                                "Want a ping when something new arrives? Press the button. Press it again to stop."),
                    "components": [{"type": 1, "components": [{"type": 2, "style": 2, "label": "Ping me for new arrivals",
                                                              "emoji": {"name": "🔔"}, "custom_id": button}]}]})
                made.append("the 🔔 ping button")
        except PermissionError:
            return web.json_response({"ok": False, "error": (
                "Discord said no. Plexbie needs Manage Channels and Manage Roles here: press "
                "\"Add Plexbie to my server\" again (the link asks for exactly those) and pick this server.")})
        except Exception as e:
            return web.json_response({"ok": False, "error": f"Setting up the server stopped partway: {e}. Press it again to finish."})

        await self._save({"GUILD_ID": str(gid), "UPDATES_CHANNEL_ID": arrivals, "STATS_CHANNEL_ID": stats,
                          "ADMIN_CHANNEL_ID": admin, "WATCH_PARTY_CHANNEL_ID": party, "ADMIN_ROLE_ID": admin_role,
                          "PLEX_MEMBER_ROLE_ID": member_role, "ARRIVALS_ROLE_ID": ping_role, **boards})
        # New roles land beside Plexbie's own at the bottom, where it can't give them out.
        reorder = None
        try:
            from core.role_order import fix_text, out_of_reach
            now = {r["id"]: r for r in await self._json("GET", f"{api}/guilds/{gid}/roles", headers=h)}
            mine = await self._json("GET", f"{api}/guilds/{gid}/members/{me['id']}", headers=h)
            stuck = out_of_reach([now[r]["position"] for r in mine.get("roles", []) if r in now], now,
                                 [member_role, ping_role, admin_role])
            if stuck:
                reorder = "One more step: " + fix_text(stuck, me.get("username") or "Plexbie")
        except Exception as e:
            reorder = None
            print(f"   Couldn't check the role order: {_why(e)}", flush=True)
        return web.json_response({"ok": True, "made": made, "kept": kept, "reorder": reorder})

    def _plex_headers(self, token: Optional[str] = None) -> Dict[str, str]:
        h = {"Accept": "application/json", "X-Plex-Product": PLEX_PRODUCT, "X-Plex-Client-Identifier": self.client_id}
        if token:
            h["X-Plex-Token"] = token
        return h

    async def plex_ticket(self, request: web.Request) -> web.Response:
        """A one-use pass for the Plex sign-in window, which can't send the setup code."""
        now = time.monotonic()
        self._tickets = {t: exp for t, exp in self._tickets.items() if exp > now}
        ticket = secrets.token_urlsafe(24)
        self._tickets[ticket] = now + 600
        return web.json_response({"ticket": ticket})

    async def plex_go(self, request: web.Request) -> web.Response:
        """The sign-in window: the browser makes the plex.tv PIN (Plex refuses one
        made from a different internet address than the one approving it), hands
        it to plex_set, and moves on to plex.tv by itself, which keeps phones in
        the browser instead of the Plex app. See portal.auth.Auth.plex_go."""
        from portal.auth import WINDOW_CSP, pin_page
        return web.Response(content_type="text/html", headers={"Cache-Control": "no-store", "Content-Security-Policy": WINDOW_CSP},
                            text=pin_page({"client": self.client_id, "product": PLEX_PRODUCT, "next": "", "invite": False,
                                           "post": "/setup/api/plex/pin",
                                           "headers": {"X-Setup-Ticket": request.query.get("t", "")[:64]}}))

    async def plex_set(self, request: web.Request) -> web.Response:
        ticket = request.headers.get("X-Setup-Ticket", "")
        if self._tickets.pop(ticket, 0) < time.monotonic():
            return web.json_response({"error": "That sign-in window expired. Press Sign in with Plex again."}, status=403)
        body = await request.json()
        try:
            pin_id, code = int(body["id"]), str(body["code"])
        except (KeyError, TypeError, ValueError):
            return web.json_response({"error": "Bad PIN"}, status=400)
        if not re.fullmatch(r"[A-Za-z0-9]{4,64}", code):
            return web.json_response({"error": "Bad PIN"}, status=400)
        self.pins["current"] = pin_id
        return web.json_response({"url": "https://app.plex.tv/auth/#!?" + urlencode({
            "clientID": self.client_id, "code": code,
            "forwardUrl": f"{request.scheme}://{request.host}/setup/plex/done",
            "context[device][product]": PLEX_PRODUCT})})

    async def plex_done(self, request: web.Request) -> web.Response:
        return web.Response(content_type="text/html", text=(
            "<!doctype html><meta name=viewport content='width=device-width'><body style='background:#10172b;color:#f7f1f6;"
            "font:17px system-ui;text-align:center;padding:48px 20px'><p>Signed in to Plex. You can close this window.</p>"
            "<script>setTimeout(function(){window.close()},300)</script>"))

    async def plex_poll(self, request: web.Request) -> web.Response:
        pid = self.pins.get(request.match_info["ref"])
        if not pid:
            return web.json_response({"ok": False, "error": "Start the sign-in again."})
        pin = await self._json("GET", f"https://plex.tv/api/v2/pins/{pid}", headers=self._plex_headers())
        token = pin.get("authToken")
        if not token:
            return web.json_response({"ok": True, "waiting": True})
        self.pins.pop(request.match_info["ref"], None)
        await self._save({"PLEX_TOKEN": token})
        return await self.plex_servers(request)

    async def plex_servers(self, request: web.Request) -> web.Response:
        """The Plex servers this account owns, with the addresses to reach each one."""
        token = self._get("PLEX_TOKEN")
        try:
            res = await self._json("GET", "https://plex.tv/api/v2/resources?includeHttps=1&includeRelay=0",
                                   headers=self._plex_headers(token))
            user = await self._json("GET", "https://plex.tv/api/v2/user", headers=self._plex_headers(token))
        except Exception as e:
            return web.json_response({"ok": False, "error": f"Couldn't list your Plex servers: {_why(e)}"})
        if user.get("username") and not os.getenv("PLEX_USERNAME"):
            await self._save({"PLEX_USERNAME": user["username"]})
        servers = []
        for r in res:
            if "server" not in (r.get("provides") or "") or not r.get("owned"):
                continue
            conns = sorted(r.get("connections") or [], key=lambda c: (not c.get("local"), c.get("relay", False)))
            urls = [f"http://{c['address']}:{c['port']}" for c in conns if c.get("local") and c.get("address")]
            urls += [c["uri"] for c in conns if c.get("uri")]
            servers.append({"name": r.get("name"), "urls": list(dict.fromkeys(urls))})
            self.plex_urls.update(u.rstrip("/") for u in urls)
        return web.json_response({"ok": True, "waiting": False, "account": user.get("username"), "servers": servers})

    async def address(self, request: web.Request) -> web.Response:
        """Does a public address (WEB_PUBLIC_URL) reach this Plexbie? Plexbie asks it for a
        one-time path only this server knows, the way a visitor would: out through the
        internet and back in through the tunnel or proxy."""
        from urllib.parse import urlparse
        from core.config import public_url
        body = await request.json()
        url = public_url(str(body.get("url") or ""))[:300]
        host = urlparse(url).hostname if url else None
        if not host or "." not in host:
            return web.json_response({"ok": False, "message": "That isn't an address. Type something like plexbie.example.com."})
        nonce = secrets.token_urlsafe(18)
        self._probes[nonce] = time.monotonic()
        try:
            async with self.http.get(f"{url}/setup/probe/{nonce}", timeout=aiohttp.ClientTimeout(total=15), allow_redirects=False) as r:
                answer = (await r.text())[:200].strip() if r.status == 200 else ""
        except aiohttp.ClientConnectorCertificateError:
            return web.json_response({"ok": False, "message": f"{host} answers, but its HTTPS certificate isn't valid."})
        except aiohttp.ClientConnectorError as e:
            dns = isinstance(getattr(e, "os_error", None), socket.gaierror)
            return web.json_response({"ok": False, "message": (
                f"{host} doesn't exist yet. Add it in your DNS (or your Cloudflare tunnel), then check again." if dns
                else f"Nothing answered at {host}. Check your tunnel or proxy sends it to this server, port "
                     f"{os.getenv('WEB_PORT', '7979')}.")})
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            return web.json_response({"ok": False, "message": f"Couldn't check {host}: {_why(e)}."})
        finally:
            self._probes.pop(nonce, None)
        if answer != nonce:
            return web.json_response({"ok": False, "message": (
                f"{host} answers, but it isn't this Plexbie. Point it at this server, port {os.getenv('WEB_PORT', '7979')}.")})
        if not url.startswith("https://"):
            return web.json_response({"ok": True, "warn": True, "message": (
                f"{host} reaches this Plexbie, but over plain http. Signing in with Discord or Plex and phone alerts "
                "need https: put it behind a tunnel or proxy with a certificate.")})
        return web.json_response({"ok": True, "message": f"{host} reaches this Plexbie over https."})

    async def probe(self, request: web.Request) -> web.Response:
        """The other end of address(): answers only for a check this server started."""
        nonce = request.match_info["nonce"]
        started = self._probes.get(nonce)
        if started is None or time.monotonic() - started > 30:
            return web.Response(status=404, text="Not found.")
        return web.Response(text=nonce, headers={"Cache-Control": "no-store"})

    async def test(self, request: web.Request) -> web.Response:
        """Try one service with the typed (or saved) address and key."""
        body = await request.json()
        what = body.get("service")
        g = lambda k: self._get(k, body.get("values"))
        try:
            if what == "plex":
                url, token = g("PLEX_URL").rstrip("/"), g("PLEX_TOKEN")
                data = await self._json("GET", f"{url}/", headers=self._plex_headers(token))
                name = (data.get("MediaContainer") or {}).get("friendlyName") or "your server"
                msg = f"Connected to {name}."
            elif what in ("sonarr", "radarr"):
                key = what.upper()
                data = await self._json("GET", f"{g(key + '_URL').rstrip('/')}/api/v3/system/status",
                                        headers={"X-Api-Key": g(key + "_TOKEN")})
                msg = f"Connected to {data.get('appName', what.title())} {data.get('version', '')}."
            elif what == "seerr":
                data = await self._json("GET", f"{g('SEERR_URL').rstrip('/')}/api/v1/settings/main",
                                        headers={"X-Api-Key": g("SEERR_TOKEN")})
                msg = f"Connected to {data.get('applicationTitle') or 'Seerr'}."
            elif what == "sabnzbd":
                data = await self._json("GET", f"{g('SABNZBD_URL').rstrip('/')}/api",
                                        params={"mode": "queue", "output": "json", "limit": 1, "apikey": g("SABNZBD_API_KEY")})
                if data.get("error") or data.get("status") is False:
                    raise PermissionError(data.get("error") or "That key was turned down")
                msg = "Connected to SABnzbd."
            elif what == "tautulli":
                data = await self._json("GET", f"{g('TAUTULLI_URL').rstrip('/')}/api/v2",
                                        params={"apikey": g("TAUTULLI_TOKEN"), "cmd": "get_server_friendly_name"})
                if (data.get("response") or {}).get("result") != "success":
                    raise PermissionError("That key was turned down")
                msg = "Connected to Tautulli."
            elif what == "tmdb":
                await self._json("GET", "https://api.themoviedb.org/3/configuration", params={"api_key": g("TMDB_API_KEY")})
                msg = "TMDB accepted the key."
            else:
                return web.json_response({"ok": False, "error": "Unknown service"}, status=400)
        except PermissionError as e:
            return web.json_response({"ok": False, "error": f"{e}. Copy the key again."})
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, json.JSONDecodeError) as e:
            return web.json_response({"ok": False, "error": f"Couldn't reach it: {_why(e)}. Check the address (http://, IP and port)."})
        return web.json_response({"ok": True, "message": msg})

    async def finish(self, request: web.Request) -> web.Response:
        body = await request.json()
        await self._save(body.get("values") or {})
        missing = missing_to_finish()
        if missing:
            return web.json_response({"ok": False, "missing": missing})
        load_dotenv(self.env_file, override=True)
        (self.env_file.parent / IN_PROGRESS).unlink(missing_ok=True)
        asyncio.get_running_loop().call_later(0.5, self.done.set)
        return web.json_response({"ok": True})

    async def brand(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        for root in (Path(os.getenv("WEB_DIST", "web/dist")), Path("web/public")):
            f = root / "brand" / name
            if name in {p.name for p in (root / "brand").glob("*")} and f.is_file():
                return web.FileResponse(f)
        raise web.HTTPNotFound()

    #: Reached by the Plex sign-in window, which has a ticket instead of the code.
    UNGATED = ("/setup/api/plex/pin",)

    @web.middleware
    async def _home_only(self, request: web.Request, handler):
        """Setup is for this network only (or a VPN into it): never through Cloudflare
        or another public route, even when it reopens on a live install. The one
        exception is the address check's probe, which comes in by the public route on
        purpose and tells a visitor nothing (a random one-time path, or 404)."""
        if request.path.startswith("/setup/probe/"):
            return await handler(request)
        import ipaddress
        from portal.ratelimit import client_ip, trusted
        try:
            ip = client_ip(request)
            home = not ipaddress.ip_address(ip).is_global
            # A proxy (cloudflared, NPM) always says who it's forwarding. One that
            # doesn't could be passing on anyone, so its own private address proves
            # nothing. Loopback itself (an SSH tunnel, a browser on the server) is fine.
            if ip == request.remote and trusted(request.remote) and not ipaddress.ip_address(ip).is_loopback:
                home = False
        except ValueError:
            home = False
        if home:
            return await handler(request)
        text = ("Plexbie's setup only opens on your own network. Open it from a device at home, "
                "at this server's local address (port {port}).").format(port=os.getenv("WEB_PORT", "7979"))
        if request.path.startswith("/setup/api/"):
            return web.json_response({"error": text}, status=403)
        return web.Response(status=403, content_type="text/plain", text=text)

    @web.middleware
    async def _gate(self, request: web.Request, handler):
        """Every setup API call needs the code from the log. It's sent in a header,
        which also keeps other websites out (they can't add one without asking first)."""
        if not request.path.startswith("/setup/api/") or request.path in self.UNGATED:
            return await handler(request)
        from portal.ratelimit import _visitor, client_ip
        who = _visitor(client_ip(request))
        given = request.headers.get("X-Setup-Code", "")
        refused = {"needsCode": True, "problem": self.problem}
        if not given:
            return web.json_response({**refused, "error": "Enter the setup code from Plexbie's log first."}, status=403)
        # Limits before the code is even looked at: a guess made while waiting is
        # refused unseen, so waiting really slows guessing down.
        count, not_before = self._fails.get(who, (0, 0.0))
        now = time.monotonic()
        if self._fail_total >= GLOBAL_TRIES:
            self._paused_until, self._fail_total = now + GLOBAL_PAUSE, 0
        if now < self._paused_until:
            return web.json_response({**refused, "error": "Too many wrong codes. Try again in "
                                      f"{int((self._paused_until - now) / 60) + 1} minutes."}, status=429)
        if now < not_before:
            return web.json_response({**refused, "error": f"Too many wrong codes. Try again in {int(not_before - now) + 1} seconds."},
                                     status=429)
        if secrets.compare_digest(_plain_code(given), _plain_code(self.code)):
            self._fails.pop(who, None)
            return await handler(request)
        count += 1
        self._fail_total += 1
        wait = 0.0 if count < FREE_TRIES else min(MAX_WAIT, 2.0 ** (count - FREE_TRIES + 1))
        self._fails[who] = (count, now + wait)
        if len(self._fails) > 1000:
            self._fails.pop(next(iter(self._fails)))
        print(f"   A wrong setup code was tried from {who}.", flush=True)
        return web.json_response({**refused, "error": "That code isn't right. It's in Plexbie's log."}, status=403)

    def app(self) -> web.Application:
        app = web.Application(client_max_size=256 * 1024, middlewares=[self._home_only, self._gate, self._refuse_bad_values])
        app.on_response_prepare.append(_plain_server_header)
        r = app.router
        r.add_get("/setup/api/state", self.state)
        r.add_post("/setup/api/save", self.save)
        r.add_get("/setup/api/more", self.more)
        r.add_post("/setup/api/check", self.check)
        r.add_get("/setup/api/folders", self.folders)
        r.add_post("/setup/api/connect", self.connect)
        r.add_post("/setup/api/import", self.import_env)
        r.add_post("/setup/api/test", self.test)
        r.add_post("/setup/api/address", self.address)
        r.add_get("/setup/probe/{nonce}", self.probe)
        r.add_post("/setup/api/discord", self.discord)
        r.add_post("/setup/api/discord/guild", self.discord_guild)
        r.add_post("/setup/api/discord/messages", self.discord_messages)
        r.add_post("/setup/api/discord/build", self.discord_build)
        r.add_get("/setup/api/plex/pin/{ref}", self.plex_poll)
        r.add_get("/setup/api/plex/servers", self.plex_servers)
        r.add_get("/setup/plex/go", self.plex_go)
        r.add_post("/setup/api/plex/pin", self.plex_set)
        r.add_post("/setup/api/plex/ticket", self.plex_ticket)
        r.add_get("/setup/plex/done", self.plex_done)
        r.add_post("/setup/api/finish", self.finish)
        r.add_get("/brand/{name}", self.brand)
        r.add_get("/{tail:.*}", self.page)
        return app


async def run(env_file: Path, port: int, bind: str = "0.0.0.0", problem: Optional[str] = None) -> None:
    """Serve the setup page until it's finished."""
    setup = Setup(env_file, problem)
    setup.http = aiohttp.ClientSession()
    runner = web.AppRunner(setup.app(), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, bind, port).start()
        print("=" * 60, flush=True)
        print("👋 Plexbie needs a few settings before it can start.", flush=True)
        if problem:
            print(f"   {problem}", flush=True)
        print(f"   Open http://{_lan_address()}:{port} in a browser to set it up.", flush=True)
        print(f"   Setup code: {setup.code}", flush=True)
        print("   (The page asks for it, so only someone who can see this log can set Plexbie up.", flush=True)
        print("    A new code is made every time Plexbie starts.)", flush=True)
        print("=" * 60, flush=True)
        await setup.done.wait()
        print("✅ Setup finished, starting Plexbie.", flush=True)
    finally:
        await setup.http.close()
        await runner.cleanup()
