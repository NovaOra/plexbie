# path: core/config.py
"""Configuration management for Plexbie"""

import os
from typing import Any, List, Optional

from pydantic import BaseModel, Field

from core.logging import get_logger

logger = get_logger(__name__)


def _int_or_none(value: Any, source: Optional[str] = None, fallback: Optional[int] = None) -> Optional[int]:
    """Safely coerce environment/YAML values to int, else `fallback`.

    Warns when a non-empty value has to be discarded. Silently returning None
    here meant a typo'd setting (e.g. BOT_OWNER_ID=novaora instead of a Discord
    snowflake) produced a bot that started perfectly and simply never applied
    that setting, with nothing in the logs to explain why.
    """
    if value is None or value == "":
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning(
            f"Ignoring invalid integer for {source or 'config value'}: {value!r} - "
            + ("expected a numeric ID. This setting is now INACTIVE." if fallback is None else f"falling back to {fallback}")
        )
        return fallback


#: Settings that were renamed: the old name still works (a container's own environment,
#: an .env nobody has opened since), and rename_env_keys() updates config/.env on start.
RENAMED = {"OVERSEERR_URL": "SEERR_URL", "OVERSEERR_TOKEN": "SEERR_TOKEN", "OVERSEERR_WEBHOOK_SECRET": "SEERR_WEBHOOK_SECRET"}


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    """A setting by its name, or by the name it had before (RENAMED)."""
    value = os.getenv(name)
    if value is None:
        old = next((o for o, n in RENAMED.items() if n == name), None)
        value = os.getenv(old) if old else None
    return default if value is None else value


def rename_env_keys(path) -> list:
    """config/.env: give renamed settings their new names, values untouched, comments and
    order kept. Only where the new name isn't there already. Returns what was renamed."""
    from pathlib import Path
    path = Path(path)
    if not path.is_file():
        return []
    lines = path.read_text().splitlines()
    present = {line.split("=", 1)[0].strip() for line in lines if "=" in line and not line.lstrip().startswith("#")}
    done = []
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in RENAMED and RENAMED[key] not in present:
            lines[i] = RENAMED[key] + line[line.index("="):]
            present.add(RENAMED[key])
            done.append(key)
    if done:
        tmp = path.with_suffix(".renaming")
        tmp.write_text("\n".join(lines) + "\n")
        os.chmod(tmp, path.stat().st_mode & 0o777)
        tmp.replace(path)
    return done


def _env_int(name: str) -> Optional[int]:
    """Read an optional integer env var, warning if it is set but unusable."""
    return _int_or_none(os.getenv(name), source=name)


def _env_int_default(name: str, default: int) -> int:
    """Read an integer env var with a fallback, warning if set but unusable.

    Previously these used a bare int(os.getenv(...)), which raised ValueError and
    took the whole bot down on a single typo'd interval - and surfaced only as an
    opaque "Fatal error" with no mention of which variable was at fault.
    """
    return _int_or_none(os.getenv(name), source=name, fallback=default)


_TRUE, _FALSE = ("1", "true", "yes", "on"), ("0", "false", "no", "off")


def _env_bool(name: str, default: bool) -> bool:
    """An on/off env var; anything it doesn't recognise means the default."""
    raw = os.getenv(name, "").strip().lower()
    return True if raw in _TRUE else False if raw in _FALSE else default



def public_url(value: str) -> str:
    """WEB_PUBLIC_URL as a full address: "plexbie.example.com" means https://."""
    value = (value or "").strip().rstrip("/")
    if value and "://" not in value:
        value = "https://" + value
    return value

CALLBACK_PATH = "/auth/discord/callback"


def discord_callback() -> Optional[str]:
    """The Discord sign-in return address: it follows WEB_PUBLIC_URL, so moving the site
    can't leave Discord sending people to the old address. Without a public address,
    DISCORD_CALLBACK_URL as written."""
    public = public_url(os.getenv("WEB_PUBLIC_URL", ""))
    written = (os.getenv("DISCORD_CALLBACK_URL") or "").strip() or None
    if not public:
        return written
    derived = public + CALLBACK_PATH
    if written and written.rstrip("/") != derived:
        logger.warning(f"DISCORD_CALLBACK_URL ({written}) doesn't match WEB_PUBLIC_URL; using {derived}. "
                       "Make sure that exact address is under OAuth2 > Redirects in the Discord Developer Portal.")
    return derived


def _aliases(value: str) -> List[str]:
    """WEB_ALIASES: other addresses, each as a full https address, no duplicates."""
    out: List[str] = []
    for part in (value or "").split(","):
        url = public_url(part)
        if url.startswith("https://") and url not in out:
            out.append(url)
    return out


#: Days between an inactivity warning and removal, at least.
MIN_NOTICE_DAYS = 3


class Config(BaseModel):
    """Bot configuration, from environment variables (config/.env)."""

    # Discord
    discord_bot_token: str = Field(default_factory=lambda: os.getenv("DISCORD_BOT_TOKEN", ""))
    guild_id: Optional[int] = Field(default_factory=lambda: _env_int("GUILD_ID"))
    bot_owner_id: Optional[int] = Field(default_factory=lambda: _env_int("BOT_OWNER_ID"))

    # Plex
    plex_url: str = Field(default_factory=lambda: os.getenv("PLEX_URL", "http://localhost:32400"))
    plex_token: str = Field(default_factory=lambda: os.getenv("PLEX_TOKEN", ""))
    plex_username: Optional[str] = Field(default_factory=lambda: os.getenv("PLEX_USERNAME"))
    plex_password: Optional[str] = Field(default_factory=lambda: os.getenv("PLEX_PASSWORD"))

    # Seerr
    seerr_url: str = Field(default_factory=lambda: env("SEERR_URL", ""))
    seerr_token: str = Field(default_factory=lambda: env("SEERR_TOKEN", ""))

    # Tautulli
    tautulli_url: str = Field(default_factory=lambda: os.getenv("TAUTULLI_URL", ""))
    tautulli_token: str = Field(default_factory=lambda: os.getenv("TAUTULLI_TOKEN", ""))

    # Sonarr/Radarr
    sonarr_url: str = Field(default_factory=lambda: os.getenv("SONARR_URL", ""))
    sonarr_token: str = Field(default_factory=lambda: os.getenv("SONARR_TOKEN", ""))
    radarr_url: str = Field(default_factory=lambda: os.getenv("RADARR_URL", ""))
    radarr_token: str = Field(default_factory=lambda: os.getenv("RADARR_TOKEN", ""))

    # NZBHydra
    nzbhydra_url: str = Field(default_factory=lambda: os.getenv("NZBHYDRA_URL", ""))
    nzbhydra_api_key: str = Field(default_factory=lambda: os.getenv("NZBHYDRA_API_KEY", ""))

    # SABnzbd
    sabnzbd_url: str = Field(default_factory=lambda: os.getenv("SABNZBD_URL", ""))
    sabnzbd_api_key: str = Field(default_factory=lambda: os.getenv("SABNZBD_API_KEY", ""))

    # Bookshelf Processor
    bookshelf_audiobook_watch: str = Field(default_factory=lambda: os.getenv("BOOKSHELF_AUDIOBOOK_WATCH", "/watch/audiobooks"))
    bookshelf_ebook_watch: str = Field(default_factory=lambda: os.getenv("BOOKSHELF_EBOOK_WATCH", "/watch/ebooks"))
    bookshelf_audiobook_library: str = Field(default_factory=lambda: os.getenv("BOOKSHELF_AUDIOBOOK_LIBRARY", "/library/audiobooks"))
    bookshelf_ebook_library: str = Field(default_factory=lambda: os.getenv("BOOKSHELF_EBOOK_LIBRARY", "/library/ebooks"))
    bookshelf_settle_seconds: int = Field(default_factory=lambda: _env_int_default("BOOKSHELF_SETTLE_SECONDS", 120))
    bookshelf_cache_dir: str = Field(default_factory=lambda: os.getenv("BOOKSHELF_CACHE_DIR", "/app/cache/bookshelf"))


    # Database
    db_url: str = Field(default_factory=lambda: os.getenv("DB_URL", "sqlite:///config/plexbie.db"))

    # Logging
    log_level: str = Field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    # Webhook server (for inbound events)
    webhook_port: int = Field(default_factory=lambda: _env_int_default("WEBHOOK_PORT", 7980))
    # Addresses to bind the webhook server to, comma-separated.
    #
    # Defaults to loopback only. The server previously bound 0.0.0.0, which with
    # network_mode: host exposed every /webhook/* route to the whole LAN. A route
    # whose secret is unset refuses every request, loopback included (start-up
    # makes any missing secret: bot.ensure_webhook_secrets).
    #
    # Senders on the host network (Plex) reach the bot on 127.0.0.1. A sender in a
    # bridge-networked container (Sonarr, Radarr, Tautulli, Seerr) cannot, and
    # needs the bridge gateway added here (e.g. "127.0.0.1,172.17.0.1") plus its
    # notification URL pointed at that address.
    webhook_bind: str = Field(default_factory=lambda: os.getenv("WEBHOOK_BIND", "127.0.0.1"))
    sonarr_webhook_secret: Optional[str] = Field(default_factory=lambda: os.getenv("SONARR_WEBHOOK_SECRET"))
    radarr_webhook_secret: Optional[str] = Field(default_factory=lambda: os.getenv("RADARR_WEBHOOK_SECRET"))
    tautulli_webhook_secret: Optional[str] = Field(default_factory=lambda: os.getenv("TAUTULLI_WEBHOOK_SECRET"))
    seerr_webhook_secret: Optional[str] = Field(default_factory=lambda: env("SEERR_WEBHOOK_SECRET"))
    # Plex sends no auth of its own; append ?token=<value> to the webhook URL.
    plex_webhook_secret: Optional[str] = Field(default_factory=lambda: os.getenv("PLEX_WEBHOOK_SECRET"))

    # ---- Additional config matching original bot features ----
    # Discord resource IDs (stored as int for discord.py)
    admin_channel_id: Optional[int] = Field(default_factory=lambda: _env_int("ADMIN_CHANNEL_ID"))
    stats_channel_id: Optional[int] = Field(default_factory=lambda: _env_int("STATS_CHANNEL_ID"))
    updates_channel_id: Optional[int] = Field(default_factory=lambda: _env_int("UPDATES_CHANNEL_ID"))
    # Mentioned on each new arrivals post (never on edits); members opt in with a button.
    arrivals_role_id: Optional[int] = Field(default_factory=lambda: _env_int("ARRIVALS_ROLE_ID"))
    plex_member_role_id: Optional[int] = Field(default_factory=lambda: _env_int("PLEX_MEMBER_ROLE_ID"))
    admin_role_id: Optional[int] = Field(default_factory=lambda: _env_int("ADMIN_ROLE_ID"))

    # Watch tracking persistent message IDs
    now_watching_message_id: Optional[int] = Field(default_factory=lambda: _env_int("NOW_WATCHING_MESSAGE_ID"))
    watch_streak_message_id: Optional[int] = Field(default_factory=lambda: _env_int("WATCH_STREAK_MESSAGE_ID"))
    leaderboard_message_id: Optional[int] = Field(default_factory=lambda: _env_int("LEADERBOARD_MESSAGE_ID"))

    # External APIs
    tmdb_api_key: Optional[str] = Field(default_factory=lambda: os.getenv("TMDB_API_KEY"))

    # Plugin-specific config
    watch_party_channel_id: Optional[int] = Field(default_factory=lambda: _env_int("WATCH_PARTY_CHANNEL_ID"))
    watch_party_credit_interval: int = Field(default_factory=lambda: _env_int_default("WATCH_PARTY_CREDIT_INTERVAL", 300))
    health_check_interval: int = Field(default_factory=lambda: _env_int_default("HEALTH_CHECK_INTERVAL", 300))
    # Consecutive failed checks before the first outage alert is sent. Referenced
    # by service_health but previously never declared, so the failure path raised
    # AttributeError and killed the monitor at the moment of an outage.
    health_max_failures: int = Field(default_factory=lambda: _env_int_default("HEALTH_MAX_FAILURES", 3))
    # Minimum seconds between repeat alerts for the same still-down service.
    health_alert_cooldown: int = Field(default_factory=lambda: _env_int_default("HEALTH_ALERT_COOLDOWN", 1800))
    inactivity_warning_days: int = Field(default_factory=lambda: _env_int_default("INACTIVITY_WARNING_DAYS", 25))
    inactivity_removal_days: int = Field(default_factory=lambda: _env_int_default("INACTIVITY_REMOVAL_DAYS", 30))

    # Web portal (plexbie.com). Off unless WEB_PORT is set.
    # The website is on unless WEB_PORT=0; 7979 for the site, 7980 for webhooks
    # (8080 is SABnzbd's and many other apps' default, so Plexbie stays off it).
    web_port: int = Field(default_factory=lambda: _env_int_default("WEB_PORT", 7979))
    web_bind: str = Field(default_factory=lambda: os.getenv("WEB_BIND", "0.0.0.0"))
    # Empty means "work it out from each request" (X-Forwarded-Proto/Host behind a proxy).
    web_public_url: str = Field(default_factory=lambda: public_url(os.getenv("WEB_PUBLIC_URL", "")))
    web_dist: str = Field(default_factory=lambda: os.getenv("WEB_DIST", "web/dist"))
    # In config/ (a mounted folder), not the container's own disk (docker.img on Unraid).
    web_image_cache: str = Field(default_factory=lambda: os.getenv("WEB_IMAGE_CACHE", "config/cache/web-images"))
    web_image_cache_mb: int = Field(default_factory=lambda: _env_int_default("WEB_IMAGE_CACHE_MB", 500))
    # Who runs this Plexbie, for the website's Privacy and Terms (and its footer):
    # a name or household, and an email address to write to. Both optional.
    site_operator: str = Field(default_factory=lambda: os.getenv("SITE_OPERATOR", "").strip()[:80])
    site_contact: str = Field(default_factory=lambda: os.getenv("SITE_CONTACT", "").strip()[:120])
    # Addresses this Plexbie used to answer at (comma-separated, like
    # https://old.example.com), after moving to WEB_PUBLIC_URL. The phone app may still
    # sign in through them for a while; it moves itself to the new address.
    web_aliases: List[str] = Field(default_factory=lambda: _aliases(os.getenv("WEB_ALIASES", "")))
    web_session_secret: Optional[str] = Field(default_factory=lambda: os.getenv("WEB_SESSION_SECRET"))
    discord_client_id: Optional[str] = Field(default_factory=lambda: os.getenv("DISCORD_CLIENT_ID"))
    discord_client_secret: Optional[str] = Field(default_factory=lambda: os.getenv("DISCORD_CLIENT_SECRET"))
    # Where Discord sends people back after "Log in with Discord": <WEB_PUBLIC_URL>/auth/discord/callback,
    # always, when the public address is set (an older DISCORD_CALLBACK_URL is ignored, with a
    # warning). It must be listed, exactly, under OAuth2 > Redirects in the Discord Developer Portal.
    discord_callback_url: Optional[str] = Field(default_factory=lambda: discord_callback())

    # Email for members without Discord (core/notify.py). Off unless host, username and password are set.
    smtp_host: Optional[str] = Field(default_factory=lambda: os.getenv("SMTP_HOST"))
    smtp_port: int = Field(default_factory=lambda: _env_int_default("SMTP_PORT", 587))
    smtp_username: Optional[str] = Field(default_factory=lambda: os.getenv("SMTP_USERNAME"))
    smtp_password: Optional[str] = Field(default_factory=lambda: os.getenv("SMTP_PASSWORD"))
    smtp_from: Optional[str] = Field(default_factory=lambda: os.getenv("SMTP_FROM"))

    def forget_everyone_roles(self) -> None:
        """The @everyone role's id is the server's id, and every member has it:
        as the admin or member role it would make everyone an admin or member.
        Run again when start-up fills in a blank GUILD_ID."""
        for field, name in (("admin_role_id", "ADMIN_ROLE_ID"), ("plex_member_role_id", "PLEX_MEMBER_ROLE_ID"),
                            ("arrivals_role_id", "ARRIVALS_ROLE_ID")):
            if self.guild_id and getattr(self, field) == self.guild_id:
                logger.error(f"{name} is the server's own id (that's @everyone, which every member has). "
                             f"Ignoring it - pick a real role.")
                setattr(self, field, None)

    def model_post_init(self, __context) -> None:
        """Settings that would quietly do something drastic are corrected (and
        logged) instead: each falls back to the safe reading."""
        self.forget_everyone_roles()
        # Removal must come after a warning, and some days after it.
        if self.inactivity_warning_days < 1:
            logger.error(f"INACTIVITY_WARNING_DAYS={self.inactivity_warning_days} is too low; using 25.")
            self.inactivity_warning_days = 25
        if self.inactivity_removal_days < self.inactivity_warning_days + MIN_NOTICE_DAYS:
            fixed = self.inactivity_warning_days + MIN_NOTICE_DAYS
            logger.error(f"INACTIVITY_REMOVAL_DAYS={self.inactivity_removal_days} doesn't leave time after the warning "
                         f"(INACTIVITY_WARNING_DAYS={self.inactivity_warning_days}); using {fixed}.")
            self.inactivity_removal_days = fixed
        # Intervals: 0 would spin against every service.
        for field, name, floor in (("health_check_interval", "HEALTH_CHECK_INTERVAL", 30),
                                   ("watch_party_credit_interval", "WATCH_PARTY_CREDIT_INTERVAL", 30),
                                   ("health_alert_cooldown", "HEALTH_ALERT_COOLDOWN", 60),
                                   ("health_max_failures", "HEALTH_MAX_FAILURES", 1)):
            if getattr(self, field) < floor:
                logger.error(f"{name}={getattr(self, field)} is too low; using {floor}.")
                setattr(self, field, floor)
