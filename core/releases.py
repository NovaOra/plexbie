# path: core/releases.py
"""When a requested title comes out, for the "Upcoming" stage.

A film in cinemas has no copy to find: anything posted as a WEB release before
its digital date is fake (Verity and Primetime, 2026: an .exe each). So until a
film is out to stream or buy, Plexbie says when that is instead of
"Searching", and doesn't search for it either (core/verified_search).

Radarr's "Released" availability is the same rule: the earlier of the digital
and disc dates, else 90 days after cinemas when neither is announced.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional

from utils.formatting import parse_utc

#: Radarr's guess for a film with a cinema date but no digital or disc date.
CINEMA_TO_HOME = timedelta(days=90)


def day(d: datetime, now: datetime) -> str:
    """"Oct 27", with the year when it isn't this one."""
    text = f"{d:%b} {d.day}"
    return text if d.year == now.year else f"{text}, {d.year}"


def movie_upcoming(movie: Dict[str, Any], now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """A film (Radarr record) not out to stream or buy yet: the "upcoming" stage,
    with when it is (releaseDate, releaseKind) and a sentence saying so. None
    once it's out (or nothing says it isn't)."""
    now = now or datetime.now(timezone.utc)
    cinemas = parse_utc(movie.get("inCinemas"))
    digital, disc = parse_utc(movie.get("digitalRelease")), parse_utc(movie.get("physicalRelease"))
    home = min((d for d in (digital, disc) if d), default=None)
    if home:
        if home <= now:
            return None
        kind = "digital" if home == digital else "disc"
        where = "to stream" if kind == "digital" else "on disc"
        also = (f" It's in cinemas from {day(cinemas, now)}." if cinemas and cinemas > now
                else f" In cinemas since {day(cinemas, now)}." if cinemas else "")
        return {"stage": "upcoming", "releaseDate": home.date().isoformat(), "releaseKind": kind,
                "detail": f"Out {where} {day(home, now)}. Plexbie gets it then.{also}"}
    if cinemas:
        expected = cinemas + CINEMA_TO_HOME
        if expected <= now:
            return None
        lead = (f"In cinemas {day(cinemas, now)}" if cinemas > now else f"In cinemas since {day(cinemas, now)}")
        return {"stage": "upcoming", "releaseDate": cinemas.date().isoformat(), "releaseKind": "cinemas",
                "expected": expected.date().isoformat(),
                "detail": f"{lead}. Its streaming date isn't announced yet; Plexbie starts looking around "
                          f"{day(expected, now)}, when films usually come out to stream."}
    if (movie.get("status") or "") in ("announced", "tba"):
        return {"stage": "upcoming", "releaseKind": "unannounced",
                "detail": "Announced, with no release date yet. Plexbie gets it once it's out."}
    return None


def show_upcoming(seasons: Iterable[Dict[str, Any]], now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Requested seasons (Sonarr season records) with no episode out yet: the
    "upcoming" stage with the first air date. None if any has aired."""
    now = now or datetime.now(timezone.utc)
    seasons = list(seasons)
    if not seasons or any(not s.get("statistics") or s["statistics"].get("episodeCount") for s in seasons):
        return None                 # something has aired (or Sonarr didn't say): it's searchable
    starts = [(d, s.get("seasonNumber")) for s in seasons
              for d in [parse_utc((s.get("statistics") or {}).get("nextAiring"))] if d]
    if not starts:
        return {"stage": "upcoming", "releaseKind": "unannounced",
                "detail": "Not aired yet, and no date is announced. Plexbie gets each episode as it airs."}
    first, number = min(starts, key=lambda x: x[0])
    return {"stage": "upcoming", "releaseDate": first.date().isoformat(), "releaseKind": "premiere",
            "detail": f"Season {number} starts {day(first, now)}. Plexbie gets each episode as it airs."}


def next_episode(seasons: Iterable[Dict[str, Any]], now: Optional[datetime] = None) -> Optional[str]:
    """", next episode Oct 7" for a season still airing, or None."""
    now = now or datetime.now(timezone.utc)
    upcoming = [d for s in seasons for d in [parse_utc((s.get("statistics") or {}).get("nextAiring"))] if d and d > now]
    return f"next episode {day(min(upcoming), now)}" if upcoming else None
