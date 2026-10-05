# path: tests/test_webhook_security.py
"""Webhook signature validation: constant-time compare and fail-closed paths."""
import asyncio

import conftest  # noqa: F401
from helpers import EmptySecretsConfig, FakeRequest, SetSecretsConfig

from core.webhook_security import WebhookValidator, secure_equals


# --- secure_equals: hmac.compare_digest raises TypeError on non-ASCII str ---

def test_equal_secrets_match():
    assert secure_equals("secret", "secret") is True


def test_unequal_secrets_do_not_match():
    assert secure_equals("secret", "other") is False


def test_non_ascii_does_not_raise():
    """An unauthenticated request with one non-ASCII auth byte must not 500.

    hmac.compare_digest("sécret", "secret") raises
    TypeError: comparing strings with non-ASCII characters is not supported.
    """
    assert secure_equals("sécret", "secret") is False


def test_non_ascii_can_still_match_itself():
    assert secure_equals("sécret", "sécret") is True


def test_none_never_matches():
    assert secure_equals(None, "secret") is False
    assert secure_equals("secret", None) is False
    assert secure_equals(None, None) is False


def test_empty_string_does_not_match_real_secret():
    assert secure_equals("", "secret") is False


# --- Plex: must fail closed when a secret is set but no token supplied ---

def _plex(request, secret="plex-token"):
    validator = WebhookValidator(SetSecretsConfig)
    return validator._validate_plex_signature(request, None, secret)


def test_plex_rejects_missing_token_when_secret_configured():
    """Previously returned True here: omitting the token bypassed validation."""
    ok, error, _ = _plex(FakeRequest())
    assert ok is False and error


def test_plex_accepts_correct_token_in_query():
    ok, _, _ = _plex(FakeRequest(query={"token": "plex-token"}))
    assert ok is True


def test_plex_accepts_correct_token_in_header():
    ok, _, _ = _plex(FakeRequest(headers={"X-Plex-Token": "plex-token"}))
    assert ok is True


def test_plex_rejects_wrong_token():
    ok, _, _ = _plex(FakeRequest(query={"token": "wrong"}))
    assert ok is False


# --- unconfigured secret means passthrough, and must not consume the body ---

class _ReadableRequest(FakeRequest):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.read_calls = 0

    async def read(self):
        self.read_calls += 1
        return b'{"eventType": "Test"}'


def test_no_secret_refuses_even_on_loopback():
    """A tunnel or proxy on this machine forwards strangers from 127.0.0.1, so
    "only this machine can reach it" doesn't stand in for a secret."""
    validator = WebhookValidator(EmptySecretsConfig)
    ok, error, _ = asyncio.run(validator.validate_request(_ReadableRequest(), "sonarr"))
    assert ok is False and error


def test_missing_webhook_secrets_are_made_and_saved_on_start():
    import os
    import tempfile
    import pathlib
    import bot
    from core.webhook_connect import SECRET_KEYS
    env = pathlib.Path(tempfile.mkdtemp()) / ".env"
    env.write_text("# settings\nSONARR_WEBHOOK_SECRET=keep-me-as-is-please-0123\nRADARR_WEBHOOK_SECRET=\n")
    saved = {k: os.environ.pop(k, None) for k in SECRET_KEYS}
    old_env, bot.ENV_FILE = bot.ENV_FILE, env
    try:
        os.environ["SONARR_WEBHOOK_SECRET"] = "keep-me-as-is-please-0123"
        asyncio.run(bot.ensure_webhook_secrets())
        text = env.read_text()
        made = {k: os.environ.get(k) for k in SECRET_KEYS}
    finally:
        bot.ENV_FILE = old_env
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
    assert made["SONARR_WEBHOOK_SECRET"] == "keep-me-as-is-please-0123"
    assert all(len(made[k] or "") >= 24 for k in SECRET_KEYS)
    assert all(f"{k}={made[k]}" in text for k in SECRET_KEYS if k != "SONARR_WEBHOOK_SECRET")


def test_no_secret_on_the_network_refuses():
    """With WEBHOOK_BIND=0.0.0.0 (or a LAN address) and no secret, anyone on the
    network could post fake arrivals, approvals and "Plex is down" alerts."""
    validator = WebhookValidator(EmptySecretsConfig)
    ok, error, _ = asyncio.run(validator.validate_request(_ReadableRequest(), "sonarr"))
    assert ok is False and error


def test_a_mistyped_service_cant_be_registered():
    from core.webhooks import WebhookServer
    from core.config import Config
    from helpers import FakeServices
    server = WebhookServer(FakeServices(Config()))
    for bad in ("Seerr", "lidarr"):
        try:
            server.add_validated_post("/webhook/x", lambda r: None, bad)
            raise AssertionError(f"{bad} was accepted")
        except ValueError:
            pass


def test_unknown_service_with_secret_fails_closed():
    """A secret we cannot validate must reject, not silently accept."""
    validator = WebhookValidator(SetSecretsConfig)
    validator._get_secret = lambda service: "some-secret"
    ok, error, _ = asyncio.run(validator.validate_request(_ReadableRequest(), "mystery"))
    assert ok is False and error


# --- Sonarr/Radarr ---

def _arr(request, service="sonarr"):
    validator = WebhookValidator(SetSecretsConfig)
    return validator._validate_arr_signature(request, b"", f"{service}-key", service)


def test_arr_accepts_correct_api_key():
    ok, _, _ = _arr(FakeRequest(headers={"X-Api-Key": "sonarr-key"}))
    assert ok is True


def test_arr_rejects_wrong_api_key():
    ok, _, _ = _arr(FakeRequest(headers={"X-Api-Key": "nope"}))
    assert ok is False


def test_arr_rejects_missing_auth():
    ok, error, _ = _arr(FakeRequest())
    assert ok is False and error


def test_arr_non_ascii_api_key_is_rejected_not_crashed():
    ok, _, _ = _arr(FakeRequest(headers={"X-Api-Key": "sécret"}))
    assert ok is False


# --- Tautulli / Seerr / shared-secret services ---

def test_tautulli_accepts_bearer_token():
    validator = WebhookValidator(SetSecretsConfig)
    request = FakeRequest(headers={"Authorization": "Bearer taut-secret"})
    ok, _, _ = validator._validate_tautulli_signature(request, None, "taut-secret")
    assert ok is True


def test_tautulli_accepts_query_secret():
    validator = WebhookValidator(SetSecretsConfig)
    ok, _, _ = validator._validate_tautulli_signature(
        FakeRequest(query={"secret": "taut-secret"}), None, "taut-secret")
    assert ok is True


def test_tautulli_rejects_missing_auth():
    validator = WebhookValidator(SetSecretsConfig)
    ok, error, _ = validator._validate_tautulli_signature(FakeRequest(), None, "taut-secret")
    assert ok is False and error


def test_seerr_rejects_missing_authorization():
    validator = WebhookValidator(SetSecretsConfig)
    ok, error, _ = validator._validate_seerr_signature(FakeRequest(), None, "over-secret")
    assert ok is False and error


# --- every service the validator knows must be reachable from config ---

def test_all_five_services_resolve_a_secret():
    validator = WebhookValidator(SetSecretsConfig)
    for service in ("sonarr", "radarr", "tautulli", "seerr", "plex"):
        assert validator._get_secret(service), f"{service} secret not wired to Config"


def test_config_defines_every_secret_the_validator_reads():
    """_get_secret used getattr, so a field missing from Config read as None
    forever - which is how plex_webhook_secret was silently always unset.
    """
    from core.config import Config

    for service in ("sonarr", "radarr", "tautulli", "seerr", "plex"):
        field = f"{service}_webhook_secret"
        assert field in Config.model_fields, f"Config has no {field}"
