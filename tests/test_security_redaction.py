# path: tests/test_security_redaction.py
"""Secret redaction in log payloads.

Regression coverage for: redact_dict() walked nested dicts but returned lists
untouched, so {"users": [{"token": "..."}]} - the shape *arr and Seerr
webhook payloads use - was logged in full.
"""
import conftest  # noqa: F401

from core.security import redact, redact_dict


# --- the list regression ---

def test_list_of_dicts_is_redacted():
    payload = {"users": [{"name": "a", "token": "supersecret123456"}]}
    result = redact_dict(payload)
    assert result["users"][0]["token"] == "REDACTED"
    assert result["users"][0]["name"] == "a"


def test_deeply_nested_list_is_redacted():
    payload = {"a": [{"b": [{"c": {"password": "hunter2hunter2"}}]}]}
    assert redact_dict(payload)["a"][0]["b"][0]["c"]["password"] == "REDACTED"


def test_tuple_of_dicts_is_redacted():
    payload = {"pair": ({"api_key": "aaaaaaaaaaaaaaaa"}, {"ok": 1})}
    result = redact_dict(payload)
    assert result["pair"][0]["api_key"] == "REDACTED"
    assert isinstance(result["pair"], tuple)


def test_list_of_plain_strings_is_still_walked():
    payload = {"notes": ["token=abcdef1234567890"]}
    assert "abcdef1234567890" not in str(redact_dict(payload))


def test_list_of_scalars_is_preserved():
    payload = {"counts": [1, 2, 3], "flags": [True, None]}
    result = redact_dict(payload)
    assert result["counts"] == [1, 2, 3]
    assert result["flags"] == [True, None]


# --- existing behaviour must be preserved ---

def test_sensitive_key_at_top_level():
    for key in ("token", "api_key", "apikey", "password", "secret", "auth", "X-Api-Key"):
        assert redact_dict({key: "value12345678901234"})[key] == "REDACTED"


def test_nested_dict_still_redacted():
    assert redact_dict({"outer": {"token": "abc123456789"}})["outer"]["token"] == "REDACTED"


def test_non_sensitive_values_survive():
    payload = {"title": "The Matrix", "year": 1999, "watched": False}
    assert redact_dict(payload) == payload


def test_non_dict_input_passes_through():
    assert redact_dict("not a dict") == "not a dict"
    assert redact_dict(None) is None


def test_non_string_keys_do_not_crash():
    """Webhook JSON is string-keyed, but internal dicts are not always."""
    payload = {1: "one", None: "none", ("a",): "tuple"}
    assert redact_dict(payload)[1] == "one"


def test_plex_token_in_url_is_redacted():
    assert "REDACTED" in redact("http://plex:32400/library?X-Plex-Token=abc123")


def test_redact_handles_empty_and_none():
    assert redact("") == ""
    assert redact(None) is None


def test_emails_in_logs_are_masked():
    """Logs travel further than the database; an address shows only enough to tell two apart."""
    assert redact("Successfully sent Plex invite to jane.doe+plex@gmail.com") == "Successfully sent Plex invite to j***@gmail.com"
    assert redact("moved from a@b.co to Zed_99@mail.example.org") == "moved from a***@b.co to Z***@mail.example.org"
    assert redact_dict({"message": "invite to sam@example.com"}) == {"message": "invite to s***@example.com"}
    assert redact("listening on 192.168.1.20:7980") == "listening on 192.168.1.20:7980"
