# path: tests/helpers.py
"""Minimal stand-ins for the discord.py and config objects the code touches.

Deliberately hand-rolled rather than mocked: the production code only reads a
couple of attributes off each, and real fakes make the access pattern explicit.
"""
import conftest  # noqa: F401  (sys.path + env setup)


class FakePerms:
    def __init__(self, administrator: bool = False):
        self.administrator = administrator


class FakeRole:
    def __init__(self, role_id: int):
        self.id = role_id


class FakeMember:
    """Stands in for discord.Member (has guild_permissions and roles)."""

    def __init__(self, user_id: int, administrator: bool = False, roles=()):
        self.id = user_id
        self.guild_permissions = FakePerms(administrator)
        self.roles = list(roles)

    def __str__(self):
        return f"FakeMember({self.id})"


class FakeUser:
    """Stands in for discord.User - no guild_permissions, as in a DM."""

    def __init__(self, user_id: int):
        self.id = user_id

    def __str__(self):
        return f"FakeUser({self.id})"


class FakeServices:
    def __init__(self, config):
        from core.clients import Arr, OpenLibrary, Seerr, Sabnzbd, Tautulli, Tmdb

        self.config = config
        self.http_session = None
        # The real clients: with no http_session they raise ServiceError if called.
        self.tautulli = Tautulli(self)
        self.sonarr = Arr(self, "sonarr")
        self.radarr = Arr(self, "radarr")
        self.tmdb = Tmdb(self)
        self.sab = Sabnzbd(self)
        self.seerr = Seerr(self)
        self.openlibrary = OpenLibrary(self)


class FakeClient:
    """Stands in for the bot; permissions.py reads config off client.services."""

    def __init__(self, config):
        self.services = FakeServices(config)


class FakeResponse:
    def __init__(self, done: bool = False):
        self._done = done
        self.sent = []

    def is_done(self):
        return self._done

    async def send_message(self, content, ephemeral=False):
        self.sent.append(content)


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content, ephemeral=False):
        self.sent.append(content)


class FakeInteraction:
    def __init__(self, user, config, response_done: bool = False):
        self.user = user
        self.client = FakeClient(config)
        self.response = FakeResponse(response_done)
        self.followup = FakeFollowup()
        self.command = None

    @property
    def refusals(self):
        """Everything the code tried to tell the user, via either channel."""
        return self.response.sent + self.followup.sent


class PermConfig:
    """Config shape that permissions.py cares about."""
    bot_owner_id = 111
    admin_role_id = 999


class NoIdsConfig:
    bot_owner_id = None
    admin_role_id = None


class EmptySecretsConfig:
    """Mirrors the live deployment: keys present in .env but with no value."""
    sonarr_webhook_secret = ""
    radarr_webhook_secret = ""
    tautulli_webhook_secret = ""
    seerr_webhook_secret = ""
    plex_webhook_secret = ""


class SetSecretsConfig:
    sonarr_webhook_secret = "sonarr-key"
    radarr_webhook_secret = "radarr-key"
    tautulli_webhook_secret = "taut-secret"
    seerr_webhook_secret = "over-secret"
    plex_webhook_secret = "plex-token"


class FakeRequest:
    """Stands in for aiohttp.web.Request for the header/query validators."""

    def __init__(self, headers=None, query=None):
        self.headers = headers or {}
        self.query = query or {}
