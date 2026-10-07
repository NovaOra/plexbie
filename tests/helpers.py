# path: tests/helpers.py
"""Minimal stand-ins for the discord.py and config objects the code touches.

Deliberately hand-rolled rather than mocked: the production code only reads a
couple of attributes off each, and real fakes make the access pattern explicit.
"""
import conftest  # noqa: F401  (sys.path + env setup)

import discord

#: The household's own Discord server, and somebody else's.
HOME = 4242
OTHER = 9090


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
        self.choices = []

    def is_done(self):
        return self._done

    async def send_message(self, content, ephemeral=False):
        self.sent.append(content)

    async def defer(self, ephemeral=False, thinking=False):
        self._done = True

    async def autocomplete(self, choices):
        self.choices.append(choices)
        self._done = True


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content, ephemeral=False):
        self.sent.append(content)


_SAME_AS_CONFIG = object()


class FakeInteraction:
    """By default the interaction comes from the server the config calls home."""

    def __init__(self, user, config, response_done: bool = False, guild_id=_SAME_AS_CONFIG,
                 type=discord.InteractionType.application_command, data=None):
        self.user = user
        self.client = FakeClient(config)
        self.response = FakeResponse(response_done)
        self.followup = FakeFollowup()
        self.command = None
        self.guild_id = getattr(config, "guild_id", None) if guild_id is _SAME_AS_CONFIG else guild_id
        self.type = type
        self.data = {"name": "test"} if data is None else data
        self.command_failed = False

    @property
    def refusals(self):
        """Everything the code tried to tell the user, via either channel."""
        return self.response.sent + self.followup.sent


class PermConfig:
    """Config shape that permissions.py cares about."""
    guild_id = HOME
    bot_owner_id = 111
    admin_role_id = 999


class NoIdsConfig:
    guild_id = None
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


class _TautulliResponse:
    status, content_length = 200, None

    def __init__(self, body):
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, **kw):
        return self.body

    async def text(self):
        import json
        return json.dumps(self.body)


class FakeTautulliHttp:
    """Answers /api/v2 the way Tautulli does: the command and its parameters from the
    query string or, when takes_post, a form body. `answers` maps a command to its
    data (or to a function of the parameters); `asked` records (method, url, fields)."""

    def __init__(self, answers=None, takes_post=True):
        self.answers = answers or {}
        self.takes_post = takes_post
        self.asked = []

    def request(self, method, url, params=None, data=None, **kw):
        fields = dict(params or {})
        if method == "POST" and self.takes_post:
            fields.update(data or {})
        self.asked.append((method, url, fields))
        cmd = fields.get("cmd")
        if cmd not in self.answers:
            return _TautulliResponse({"response": {"result": "error", "message": "Unknown command"}})
        answer = self.answers[cmd]
        data = answer(fields) if callable(answer) else answer
        return _TautulliResponse({"response": {"result": "success", "data": data}})
