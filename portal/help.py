# path: portal/help.py
"""Help requests: "something's wrong with my request" from the person who made it.

Downloads get stuck in Sonarr or Radarr, the wrong version arrives, a season
is missing. Asking for help on the request itself gives the admins everything
at once: the request number and title, who asked, what they say is wrong, and
what Plexbie can see right now. It goes to the Discord admin channel and, as a
phone/browser alert, to admins who turned alerts on. Admins can search again
(Sonarr/Radarr, by the title's IDs), search by name (NZBHydra; see
core/verified_search) or mark it resolved with a reply, which reaches the person
the usual way (Discord DM, else alert or email).

Each one is a ticket (Manage → Tickets): a timeline ("thread") of what the member
said, admins' notes (admins only), replies sent to the member, what Plexbie did and
status changes; an owner (an admin who took it; any admin can still help); and
"waiting": an admin asked the member something and the ticket waits on their answer.
`status` stays "open" or "resolved", as older app versions read it.

A ticket's `subject` is what it's about: a request (every ticket from before there
were others has no subject, and is one), a title on Plex the member didn't request
("title": its TMDB id, kind and Plex ratingKey instead of a request), or nothing in
particular ("general").
"""
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core.logging import get_logger
from database.kv_store import kv_get, kv_get_all, kv_set
from database.request_store import get_request

logger = get_logger(__name__)

NAMESPACE = "help_requests"

#: What can be wrong, as the person picks it.
REASONS = {
    "stuck": "Stuck downloading",
    "notfound": "Can’t be found",
    "notonplex": "Downloaded, but not on Plex",
    "wrongfilm": "Plex says it’s a different film",
    "quality": "Wrong version or quality",
    "episodes": "Wrong or missing episodes",
    "playback": "Won’t play on Plex",
    "other": "Something else",
}
#: Reasons only Plexbie opens tickets for (members don't pick these).
PLEXBIE_REASONS = {
    "blocked": "Downloaded, but won’t import",
    "notreached": "Approved, but it never reached Sonarr or Radarr",
}
#: What can be wrong with a film or show on Plex that the member didn't request.
TITLE_REASONS = {
    "playback": "Won’t play on Plex",
    "picture": "Picture looks wrong",
    "audio": "Sound or subtitles",
    "wrongfilm": "Wrong film or episode",
    "episodes": "Missing episodes",
    "other": "Something else",
}
#: ...and with nothing in particular.
GENERAL_REASONS = {
    "access": "Can’t get in, or nothing plays",
    "other": "Something else",
}
#: A line under each of those, where there's room for one (Discord's picker).
REASON_HINTS = {
    "playback": "It won’t start, stops, or keeps buffering",
    "picture": "Washed out, wrong colours, or blurry",
    "audio": "No sound, out of sync, wrong language, or subtitles",
    "wrongfilm": "It’s a different film, or the wrong episode",
    "episodes": "Episodes are missing from a season",
    "access": "You can’t get into Plex, or nothing will play at all",
}
SUBJECTS = ("request", "title", "general")


def reason_code(h: dict) -> str:
    """A ticket's reason as a fixed code ("access", "blocked", "playback"…), for code to read
    while the words stay free to change: the code it was opened with, or, for a ticket from
    before codes were kept, the code its words belong to ("" for words no code has, such as
    an admin's own ticket)."""
    if h.get("reason_code"):
        return str(h["reason_code"])
    # Compared with plain apostrophes: tickets from before the words used curly ones match too.
    words = (h.get("reason") or "").replace("’", "'")
    for table in (GENERAL_REASONS, PLEXBIE_REASONS, TITLE_REASONS, REASONS):
        for code, label in table.items():
            if label.replace("’", "'") == words:
                return code
    return ""
#: Open general tickets one member may have at once.
MAX_OPEN_GENERAL = 3
#: How long a solved ticket stays on the member's own list.
SOLVED_SHOWN_DAYS = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def subject_of(h: dict) -> str:
    return h.get("subject") if h.get("subject") in SUBJECTS else "request"


def reasons_for(subject: str, kind: Optional[str] = None) -> Dict[str, str]:
    """What a member may pick for a ticket about this: a request, a title (missing
    episodes only for a show) or nothing in particular."""
    if subject == "general":
        return GENERAL_REASONS
    if subject == "title":
        return {code: label for code, label in TITLE_REASONS.items() if code != "episodes" or kind == "tv"}
    return REASONS


def named(h: dict) -> str:
    """The title a ticket is about, with its year when it has one: "Sintel (2010)"."""
    return f"{h.get('title') or 'Untitled'}" + (f" ({h['year']})" if h.get("year") else "")


def owns(rec: dict, user: dict) -> bool:
    """Whether this signed-in person made this request."""
    if user.get("discordId") and str(rec.get("user_id") or "") == str(user["discordId"]):
        return True
    return bool(user.get("plexAccountId")) and str(rec.get("plex_account_id") or "") == str(user["plexAccountId"])


def owns_ticket(h: dict, user: dict) -> bool:
    """Whether a ticket is this signed-in person's: opened by them, or about their request."""
    if user.get("discordId") and str(h.get("discord_id") or "") == str(user["discordId"]):
        return True
    return bool(user.get("plexAccountId")) and str(h.get("plex_account_id") or "") == str(user["plexAccountId"])


async def open_for(request_keys: Optional[set] = None) -> Dict[str, dict]:
    """Open help requests by request key (tickets about a request only)."""
    out = {}
    for hid, h in (await kv_get_all(NAMESPACE)).items():
        if isinstance(h, dict) and h.get("status") == "open" and subject_of(h) == "request" and h.get("request") \
                and (request_keys is None or h.get("request") in request_keys):
            out[h["request"]] = {**h, "id": hid}
    return out


async def open_on_title(user: dict, kind: str, tmdb: Any = None, rating_key: Any = None) -> Optional[dict]:
    """This member's open ticket on a film or show (by its TMDB id, or its Plex
    ratingKey when Plex has no TMDB match for it): one about the title itself, or
    one on their request for it. Not one an admin opened without telling them: they
    can't see it (tickets_of), so it's neither theirs to open nor in their way."""
    for hid, h in (await kv_get_all(NAMESPACE)).items():
        if not isinstance(h, dict) or h.get("status") != "open" or h.get("quiet") or not owns_ticket(h, user):
            continue
        subject = subject_of(h)
        if subject == "title" and h.get("kind") == kind and (
                (tmdb and str(h.get("tmdb") or "") == str(tmdb))
                or (rating_key and str(h.get("rating_key") or "") == str(rating_key))):
            return {**h, "id": hid}
        if subject == "request" and tmdb and str(h.get("request") or "").isdigit():
            media = (await get_request(int(h["request"])) or {}).get("media") or {}
            if str(media.get("id")) == str(tmdb) and media.get("media_type") == kind:
                return {**h, "id": hid}
    return None


async def open_general(user: dict) -> int:
    """How many general tickets this member has open."""
    return sum(1 for h in (await kv_get_all(NAMESPACE)).values()
               if isinstance(h, dict) and h.get("status") == "open" and subject_of(h) == "general" and owns_ticket(h, user))


async def all_help() -> List[dict]:
    rows = [{**h, "id": hid} for hid, h in (await kv_get_all(NAMESPACE)).items() if isinstance(h, dict)]
    rows.sort(key=lambda h: (h.get("status") != "open", h.get("created_at") or ""), reverse=False)
    opened = sorted((h for h in rows if h["status"] == "open"), key=lambda h: h["created_at"], reverse=True)
    closed = sorted((h for h in rows if h["status"] != "open"), key=lambda h: h.get("resolved_at") or "", reverse=True)[:20]
    return opened + closed


async def create(*, request_key: Optional[str] = None, slot: Optional[int] = None, title: Optional[str] = None,
                 kind: Optional[str] = None, seasons: Any = None, user: dict, reason: str, note: str, status_now: str,
                 offer: Optional[str] = None, opened_by: Optional[str] = None, quiet: bool = False,
                 subject: str = "request", tmdb: Optional[int] = None, year: Optional[str] = None,
                 rating_key: Optional[str] = None) -> dict:
    """A new ticket. On a request: its key, number, title and seasons. About a title:
    its TMDB id (when Plex has one for it), Plex ratingKey, title and year. General:
    none of those."""
    hid = secrets.token_hex(6)
    rec = {
        "subject": subject, "request": request_key, "slot": slot, "title": title, "kind": kind, "seasons": seasons,
        "who": user["user"].get("name") or "Someone",
        "discord_id": user.get("discordId"), "plex_account_id": user.get("plexAccountId"), "plex_name": user.get("plexName"),
        "reason": reason, "reason_code": reason_code({"reason": reason}), "note": note[:600], "status_then": status_now,
        "status": "open", "created_at": _now(),
    }
    if subject == "title":
        rec.update(tmdb=tmdb, year=year or None, rating_key=rating_key)
    first = (f"{reason}. {note[:600]}" if note else reason) if not opened_by else (note[:600] or reason)
    rec["thread"] = [_entry("note" if opened_by else "member", opened_by or rec["who"], first, rec["created_at"])]
    if offer:
        rec["offer"] = offer          # "name": Plexbie asks whether to search by name
    if opened_by:
        rec["opened_by"] = opened_by  # an admin opened it from Manage → All requests
    if quiet:
        rec["quiet"] = True           # ...without telling the person who asked
    await kv_set(NAMESPACE, hid, rec)
    about = {"request": f"on No. {slot} ({title})", "title": f"on {named(rec)}", "general": "(general)"}[subject]
    logger.info(f"Help request {hid} {about} from {rec['who']}: {reason}")
    return {**rec, "id": hid}


async def resolve(hid: str, actor: str, reply: str) -> Optional[dict]:
    rec = await kv_get(NAMESPACE, hid)
    if not isinstance(rec, dict):
        return None
    if rec.get("status") != "open":
        return {**rec, "id": hid, "already": True}
    thread = thread_of(rec)
    if reply:
        thread.append(_entry("reply", actor, reply[:600]))
    thread.append(_entry("status", actor, "Solved"))
    rec.update(status="resolved", resolved_by=actor, resolved_at=_now(), reply=(reply or "")[:600] or None,
               thread=thread, waiting=False)
    await kv_set(NAMESPACE, hid, rec)
    return {**rec, "id": hid}


# ---------------------------------------------------------------- the thread
#: Kinds of thread entry: what the member wrote; an admin's note (admins only); a reply
#: sent to the member ("missed": it reached nobody); what someone did (a search and its
#: result); a status change.
KINDS = ("member", "note", "reply", "action", "status")
#: What the member sees of their own ticket: what they said, what was said to them, and
#: whether it's solved (not who took it, or that it's "waiting on them").
MEMBER_SEES = ("member", "reply")
MEMBER_STATUSES = ("Solved", "Reopened")


def _entry(kind: str, by: str, text: str, at: Optional[str] = None) -> dict:
    return {"id": secrets.token_hex(4), "at": at or _now(), "by": by or "Someone", "kind": kind, "text": str(text)[:1200]}


def thread_of(rec: dict) -> List[dict]:
    """The ticket's timeline. Tickets from before there was one get it built from what
    they kept: the first message, the searches (actions) and how it was resolved."""
    if isinstance(rec.get("thread"), list):
        return list(rec["thread"])
    made = rec.get("created_at") or _now()
    if rec.get("opened_by"):
        out = [_entry("note", rec["opened_by"], rec.get("note") or rec.get("reason") or "", made)]
    else:
        out = [_entry("member", rec.get("who") or "Someone",
                      f"{rec.get('reason')}. {rec['note']}" if rec.get("note") else (rec.get("reason") or ""), made)]
    out += [_entry("action", a.get("by") or "Someone", a.get("did") or "", a.get("at")) for a in rec.get("actions") or []
            if isinstance(a, dict)]
    if rec.get("status") == "resolved":
        if rec.get("reply"):
            out.append(_entry("reply", rec.get("resolved_by") or "An admin", rec["reply"], rec.get("resolved_at")))
        out.append(_entry("status", rec.get("resolved_by") or "An admin", "Solved", rec.get("resolved_at")))
    return out


async def add(hid: str, kind: str, by: str, text: str, **changes) -> Optional[dict]:
    """Adds an entry to a ticket's timeline (and any field changes). None: no such ticket."""
    rec = await kv_get(NAMESPACE, hid)
    if not isinstance(rec, dict):
        return None
    thread = thread_of(rec)
    if text:
        thread.append(_entry(kind, by, text))
    rec.update(thread=thread, **changes)
    await kv_set(NAMESPACE, hid, rec)
    return {**rec, "id": hid}


def last_reply(rec: dict) -> Optional[str]:
    """The id of the ticket's newest reply to the member, if it has one."""
    return next((e.get("id") for e in reversed(thread_of(rec)) if e.get("kind") == "reply"), None)


async def not_reached(hid: str, entry: Optional[str], why: str) -> None:
    """A message to the member reached nobody: Plexbie says so on the thread, and the reply
    it was (`entry`, if it was one) is marked "missed", in the same write."""
    rec = await kv_get(NAMESPACE, hid)
    if not isinstance(rec, dict):
        return
    thread = [{**e, "missed": True} if entry and e.get("id") == entry and e.get("kind") == "reply" else e
              for e in thread_of(rec)]
    rec["thread"] = thread + [_entry("action", "Plexbie", why)]
    await kv_set(NAMESPACE, hid, rec)


async def reopen(hid: str, actor: str) -> Optional[dict]:
    return await add(hid, "status", actor, "Reopened", status="open", waiting=False,
                     resolved_by=None, resolved_at=None)


async def take(hid: str, actor: str, discord_id: Optional[str], plex_account_id: Optional[str],
               intent: Optional[str] = None) -> Optional[dict]:
    """The admin owns it now (alerted when the member answers). With `intent` ("take" or
    "release") it does what was asked and nothing when the ticket is already that way, so a
    second press on a page that hadn't caught up can't undo the first. Without one (older
    apps), taking your own releases it."""
    rec = await kv_get(NAMESPACE, hid)
    if not isinstance(rec, dict):
        return None
    mine = rec.get("owner") == actor
    if (intent == "take" and mine) or (intent == "release" and not mine):
        return rec
    if mine:
        return await add(hid, "status", actor, "Let it go", owner=None, owner_discord_id=None, owner_plex_id=None)
    return await add(hid, "status", actor, "Took it", owner=actor, owner_discord_id=discord_id, owner_plex_id=plex_account_id)


def member_view(rec: dict) -> dict:
    """A ticket as the member who asked sees it: no admins' notes or searches."""
    return {"status": rec.get("status"), "waiting": bool(rec.get("waiting")),
            "thread": [{k: e.get(k) for k in ("id", "at", "kind", "text")} | {"by": e.get("by") if e.get("kind") != "member" else "You"}
                       for e in thread_of(rec)
                       if e.get("kind") in MEMBER_SEES or (e.get("kind") == "status" and e.get("text") in MEMBER_STATUSES)]}


def member_row(hid: str, rec: dict) -> dict:
    """One of the member's own tickets, on their list of them: what it's about, and
    the ticket as they see it."""
    view = member_view(rec)
    seen = view["thread"]      # its time is when something they can see changed, not an admin's note
    return {
        "id": hid, "subject": subject_of(rec), "title": rec.get("title") or None, "kind": rec.get("kind") or None,
        "tmdb": str(rec["tmdb"]) if rec.get("tmdb") else None, "year": rec.get("year") or None,
        "requestKey": rec.get("request") or None, "slot": rec.get("slot") or None,
        "seasons": rec.get("seasons"), "reason": rec.get("reason") or "",
        "status": view["status"] or "open", "waiting": view["waiting"],
        "createdAt": rec.get("created_at"), "updatedAt": (seen[-1].get("at") if seen else None) or rec.get("created_at"),
        "thread": view["thread"],
    }


async def tickets_of(user: dict) -> List[dict]:
    """The member's own tickets, about anything: the open ones (waiting on their answer
    first), then those solved in the last SOLVED_SHOWN_DAYS days. Not one an admin
    opened on their request without telling them."""
    since = (datetime.now(timezone.utc) - timedelta(days=SOLVED_SHOWN_DAYS)).isoformat()
    rows = [member_row(hid, h) for hid, h in (await kv_get_all(NAMESPACE)).items()
            if isinstance(h, dict) and owns_ticket(h, user) and not h.get("quiet")
            and (h.get("status") == "open" or (h.get("resolved_at") or "") >= since)]
    rows.sort(key=lambda r: r["updatedAt"] or "", reverse=True)
    rows.sort(key=lambda r: (r["status"] != "open", not r["waiting"]))
    return rows


def needs_action(rec: dict) -> bool:
    """Open, and not waiting on the member: an admin's move."""
    return rec.get("status") == "open" and not rec.get("waiting")


async def note_search(hid: str, actor: str, what: str) -> None:
    rec = await kv_get(NAMESPACE, hid)
    if isinstance(rec, dict):
        rec["thread"] = thread_of(rec) + [_entry("action", actor, what)]
        rec.setdefault("actions", []).append({"at": _now(), "by": actor, "did": what})
        await kv_set(NAMESPACE, hid, rec)


async def _series_for(services, rec: dict) -> dict:
    media = (rec or {}).get("media") or {}
    if not services.sonarr.configured:
        raise LookupError("Sonarr isn't set up")
    series = next((s for s in await services.sonarr.series() if s.get("tmdbId") == media.get("id")), None)
    if not series:
        raise LookupError("It isn't in Sonarr yet")
    return series


async def _seasons_for(services, rec: dict, series: dict) -> list:
    from core.season_search import seasons_with_missing
    seasons = rec.get("seasons")
    if isinstance(seasons, list) and seasons:
        return [int(n) for n in seasons]
    return await seasons_with_missing(services.sonarr, series["id"])


async def _movie_for(services, rec: dict) -> dict:
    from core.verified_search import find_movie
    if not services.radarr.configured:
        raise LookupError("Radarr isn't set up")
    movie = await find_movie(services.radarr, ((rec or {}).get("media") or {}).get("id"))
    if not movie:
        raise LookupError("It isn't in Radarr yet")
    return movie


def search_target(h: dict) -> Any:
    """What a ticket's search buttons look for: its request (by key), or the film or
    show it's about, shaped as a request's record. A general ticket has none."""
    subject = subject_of(h)
    if subject == "request" and h.get("request"):
        return h["request"]
    if subject == "title" and str(h.get("tmdb") or "").isdigit() and h.get("kind") in ("movie", "tv"):
        return {"media": {"id": int(h["tmdb"]), "media_type": h["kind"],
                          "title" if h["kind"] == "movie" else "name": h.get("title") or ""}, "seasons": None}
    raise LookupError("This ticket isn't about one film or show Plexbie can look up, so there's nothing to search for")


async def _record(target: Any) -> dict:
    """A request's record by its key, or one search_target built."""
    return target if isinstance(target, dict) else (await get_request(int(target)) or {})


async def search_again(services, target: Any, on_nothing=None, on_movie=None) -> str:
    """Search again for a request (by its key) or a ticket's title (search_target), by
    its IDs. Returns what was started.

    Films: Radarr's search, keeping only releases that really are the film
    (core/verified_search); on_movie hears what happened, as a sentence and
    whether something was grabbed. Shows search season by season, falling back
    to single episodes when no whole-season release exists (core/season_search);
    on_nothing hears which seasons turned up nothing at all.
    """
    from core import season_search, verified_search
    rec = await _record(target)
    media = (rec or {}).get("media") or {}
    if media.get("media_type") == "movie":
        movie = await _movie_for(services, rec)
        title = movie.get("title") or media.get("title") or "It"

        async def run():
            try:
                result = await verified_search.verify_movie(services.radarr, movie)
                text, found = verified_search.describe_by_id(title, result), result["state"] != "searched" or bool(result.get("grabbed"))
            except Exception as e:
                logger.warning(f"Searching for {title} by its IDs failed: {e}")
                text, found = f"Searching by its IDs failed: {e}", False
            if on_movie:
                await on_movie(text, found)
        verified_search.background(run())
        return "Radarr is searching for it again by its IDs. Plexbie checks what it finds really is this film."
    if media.get("media_type") == "tv":
        series = await _series_for(services, rec)
        seasons = await _seasons_for(services, rec, series)
        if not seasons:
            return "Sonarr already has every aired episode of what was asked for."
        season_search.start(services, series["id"], seasons, title=media.get("name") or "", on_nothing=on_nothing)
        return (f"Sonarr is searching season {', '.join(map(str, seasons))} again. If no whole-season "
                "release turns up, Plexbie tries it episode by episode.")
    raise LookupError("Books are searched by an admin in SABnzbd")


async def search_episodes(services, target: Any, on_nothing=None) -> str:
    """Skip the season search: look for every missing episode on its own."""
    from core import season_search
    rec = await _record(target)
    media = (rec or {}).get("media") or {}
    if media.get("media_type") != "tv":
        raise LookupError("That's for shows only")
    series = await _series_for(services, rec)
    seasons = await _seasons_for(services, rec, series)
    if not seasons:
        return "Sonarr already has every aired episode of what was asked for."
    season_search.start(services, series["id"], seasons, title=media.get("name") or "",
                        on_nothing=on_nothing, season_first=False)
    return f"Sonarr is searching season {', '.join(map(str, seasons))} one episode at a time."


async def search_by_name(services, target: Any, on_done) -> str:
    """Search NZBHydra by the title's name instead of its IDs, in the background.
    Returns what was started; on_done hears what happened, as a sentence and
    whether something was grabbed."""
    from core import verified_search
    rec = await _record(target)
    media = (rec or {}).get("media") or {}
    if not services.hydra.configured:
        raise LookupError("NZBHydra isn't set up, so Plexbie can't search by name")
    if media.get("media_type") == "movie":
        movie = await _movie_for(services, rec)
        title = movie.get("title") or "It"
        query = f'"{title} {movie.get("year")}"' if movie.get("year") else f'"{title}"'

        async def search():
            return await verified_search.movie_by_name(services.radarr, services.hydra, movie)
    elif media.get("media_type") == "tv":
        series = await _series_for(services, rec)
        seasons = await _seasons_for(services, rec, series)
        if not seasons:
            return "Sonarr already has every aired episode of what was asked for."
        title = series.get("title") or "It"
        query = " and ".join(f'"{title} S{n:02d}"' for n in seasons)

        async def search():
            return await verified_search.show_by_name(services.sonarr, services.hydra, series, seasons)
    else:
        raise LookupError("Books are searched by an admin in SABnzbd")

    async def run():
        try:
            result = await search()
            text, found = verified_search.describe_by_name(title, result), bool(result.get("grabbed"))
        except Exception as e:
            logger.warning(f"Searching for {title} by name failed: {e}")
            text, found = f"Searching by name failed: {e}", False
        await on_done(text, found)
    verified_search.background(run())
    return f"Plexbie is searching NZBHydra for {query}. It reports back here, and closes this if it finds it."


async def name_search_for_help(services, hid: str, actor: str, tell=None) -> str:
    """An admin said yes to searching by name: start it, note it on the help request,
    and when it's done note what happened there (and tell(text) the admins). Found
    and grabbed: the help request is resolved."""
    h = await kv_get(NAMESPACE, hid)
    if not isinstance(h, dict) or h.get("status") != "open":
        raise LookupError("That help request isn't open any more")

    async def done(text: str, found: bool) -> None:
        await note_search(hid, "Plexbie", text)
        if found:
            await resolve(hid, "Plexbie", text)
        if tell:
            try:
                await tell(f"{'✅' if found else '🔎'} **{h.get('title')}**: {text}")
            except Exception as e:
                logger.warning(f"Could not report the name search for {h.get('title')}: {e}")
    message = await search_by_name(services, search_target(h), done)
    await note_search(hid, actor, message)
    return message
