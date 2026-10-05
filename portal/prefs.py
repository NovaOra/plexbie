# path: portal/prefs.py
"""What each member chose for themselves, kept with their account so it follows
them between the website and the app. For now: which languages the Request page's
shelves show. None chosen (the default) is everything, the global top; some
like soaps from Hong Kong, everyone's anime is Japanese, so nothing is hidden
unless they say so."""
from typing import Dict, List, Optional, Set

from database.kv_store import kv_get, kv_set

NAMESPACE = "member_prefs"

#: The choices, by original language as TMDB labels it: (code, name, TMDB codes).
LANGUAGES = (
    ("en", "English", {"en"}), ("ja", "Japanese", {"ja"}), ("ko", "Korean", {"ko"}),
    ("zh", "Chinese", {"zh", "cn"}), ("es", "Spanish", {"es"}), ("hi", "Hindi", {"hi"}),
    ("fr", "French", {"fr"}), ("de", "German", {"de"}), ("it", "Italian", {"it"}),
    ("pt", "Portuguese", {"pt"}), ("th", "Thai", {"th"}), ("tr", "Turkish", {"tr"}),
)
CODES = {code for code, _, _ in LANGUAGES}


def _key(user: dict) -> Optional[str]:
    if user.get("discordId"):
        return f"discord:{user['discordId']}"
    if user.get("plexAccountId"):
        return f"plex:{user['plexAccountId']}"
    return None


def options() -> List[Dict[str, str]]:
    return [{"code": code, "name": name} for code, name, _ in LANGUAGES]


async def get(user: dict) -> Dict[str, List[str]]:
    key = _key(user)
    saved = await kv_get(NAMESPACE, key) if key else None
    langs = [c for c in (saved or {}).get("languages") or [] if c in CODES]
    return {"languages": langs}


async def save(user: dict, body: dict) -> Dict[str, List[str]]:
    """Keep this member's choice. An unknown language is left out; none is everything."""
    key = _key(user)
    if key is None:
        raise LookupError("no account to keep it with")
    raw = body.get("languages")
    langs = sorted({c for c in raw if isinstance(c, str) and c in CODES}) if isinstance(raw, list) else []
    saved = await kv_get(NAMESPACE, key) or {}
    await kv_set(NAMESPACE, key, {**saved, "languages": langs})
    return {"languages": langs}


def tmdb_codes(languages: List[str]) -> Optional[Set[str]]:
    """The TMDB language codes to keep, or None for everything."""
    keep = {t for code, _, tmdb in LANGUAGES if code in languages for t in tmdb}
    return keep or None
