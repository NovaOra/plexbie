# path: tests/test_cleanup_compute.py
"""Media cleanup: the website's countdown is the bot's own schedule.

The countdown on the website and in the app (portal/cleanup.compute) reads Plex's raw
XML, while the cleanup task (MediaCleanupCog) walks plexapi objects; each has its own
copy of the rule. Nothing tied the two together, so a change to one (a new source of
activity, how a show's episodes count, what keeps a title) would leave the other
counting down to a removal that never comes, or not warning about one that does.

Both are run here over one Plex server, from the same items, requests and warnings.
"""
import json
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import quoteattr

import conftest  # noqa: F401

from core import media_tracking
from core.media_tracking import TrackedMedia
from portal import cleanup as countdown
from utils.formatting import parse_utc
from test_cleanup_whole_removal import _cog

NOW = datetime.now(timezone.utc)


def _at(days):
    """`days` days ago, and an hour more, so a whole day is never lost to the seconds
    between the two runs."""
    return NOW - timedelta(days=days, hours=1)


def _local(days):
    """As plexapi gives it: the server's local time, without a zone."""
    return _at(days).astimezone().replace(tzinfo=None)


class _Guid:
    def __init__(self, gid):
        self.id = gid


class _Tag:
    """A guid plexapi gives by its tag rather than an id."""

    def __init__(self, gid):
        self.tag = gid


class _Episode:
    def __init__(self, added, seen=None):
        self.addedAt = _local(added)
        self.lastViewedAt = _local(seen) if seen is not None else None


class _Item:
    """A film or show as library.all() lists it. `seen` is the owner's own lastViewedAt
    (a film's), `episodes` (added, seen) pairs (a show's)."""

    def __init__(self, kind, key, title, added, tmdb=None, year=None, seen=None, episodes=()):
        self.type, self.ratingKey, self.title, self.year = kind, key, title, year
        self.guids = [_Guid(f"tmdb://{tmdb}")] if tmdb else []
        self.addedAt = _local(added)
        self.lastViewedAt = _local(seen) if seen is not None else None
        self._episodes = [_Episode(*ep) for ep in episodes]

    def episodes(self):
        return self._episodes


def _film(key, title, added, **kw):
    return _Item("movie", key, title, added, **kw)


def _show(key, title, added, episodes, **kw):
    return _Item("show", key, title, added, episodes=episodes, **kw)


class _View:
    """One play in Plex's history: a film, or an episode of a show."""

    def __init__(self, key, days, show=None):
        self.ratingKey, self.grandparentRatingKey, self.viewedAt = key, show, _local(days)


class _Plex:
    """One Plex server, read both ways: plexapi objects for the bot (library.sections()),
    raw XML for the website (query()). Sections are (key, title, type, items); every
    query is kept in `asked`."""

    def __init__(self, sections, history=()):
        self.sections, self.views, self.asked = sections, list(history), []
        plex = self

        class Lib:
            def __init__(self, key, title, kind, items):
                self.key, self.title, self.type, self._items = key, title, kind, items

            def all(self):
                return self._items

        class Library:
            def sections(self):
                return [Lib(*s) for s in plex.sections]
        self.library = Library()

    def history(self, mindate=None):
        return self.views

    def query(self, path):
        self.asked.append(path)
        if path == "/library/sections":
            return self._xml("".join(f'<Directory key="{key}" type="{kind}" title={quoteattr(title)}/>'
                                     for key, title, kind, _ in self.sections))
        for key, _, kind, items in self.sections:
            if path == f"/library/sections/{key}/all?includeGuids=1":
                tag = "Video" if kind == "movie" else "Directory"
                return self._xml("".join(self._element(tag, item) for item in items))
            for item in items:
                if path == f"/library/metadata/{item.ratingKey}/allLeaves":
                    return self._xml("".join(self._element("Video", ep) for ep in item.episodes()))
        raise AssertionError(f"unexpected Plex query {path}")

    @staticmethod
    def _xml(body):
        return ET.fromstring(f"<MediaContainer>{body}</MediaContainer>")

    @staticmethod
    def _element(tag, obj):
        attrs = {"addedAt": obj.addedAt, "lastViewedAt": obj.lastViewedAt}
        out = "".join(f' {name}="{int(when.timestamp())}"' for name, when in attrs.items() if when)
        if hasattr(obj, "ratingKey"):
            out += f' ratingKey="{obj.ratingKey}" title={quoteattr(obj.title)} year="{obj.year or ""}"'
        if getattr(obj, "guid", None):
            out += f" guid={quoteattr(obj.guid)}"
        guids = "".join(f'<Guid id="{getattr(g, "id", None) or g.tag}"/>' for g in getattr(obj, "guids", []))
        return f"<{tag}{out}>{guids}</{tag}>"


class _Requests:
    """Media tracking's requests, on a scratch file that both sides read: the bot through
    the tracker, the website through the file."""

    def __init__(self, *media):
        self.path = Path(tempfile.mkdtemp()) / "media_tracking.json"
        self.path.write_text(json.dumps({f"{m.media_type}:{m.tmdb_id}": m.to_dict() for m in media}))

    def __enter__(self):
        self._saved = media_tracking.TRACKING_FILE, media_tracking._tracking_manager, countdown.TRACKING_FILE
        media_tracking.TRACKING_FILE = countdown.TRACKING_FILE = self.path
        media_tracking._tracking_manager = media_tracking.MediaTrackingManager()
        return self

    def __exit__(self, *exc):
        media_tracking.TRACKING_FILE, media_tracking._tracking_manager, countdown.TRACKING_FILE = self._saved


def _requested(tmdb, kind, title, days):
    return TrackedMedia(tmdb_id=tmdb, media_type=kind, title=title, request_timestamp=_at(days).isoformat())


def _household():
    """A server with something on each path of the rule (90 inactive days, 7 of warning)."""
    films = [
        _film(1, "Dune", 400, tmdb=438631, year=2021),            # idle, never warned
        _film(2, "Heat", 400, tmdb=949, year=1995),               # idle, warned 3 days ago
        _film(3, "Solaris", 400, tmdb=2103, year=2002),           # idle, warned long ago: due
        _film(4, "Arrival", 86, tmdb=329865, year=2016),          # in its last week, warned long ago
        _film(5, "Tenet", 400, tmdb=577922, seen=30),             # the owner watched it
        _film(6, "Sintel", 400),                                  # a member watched it, no ids
        _film(7, "Moon", 400, tmdb=17431),                        # requested again
        _film(8, "Fresh", 10, tmdb=1),
        _film(9, "Kept", 500, tmdb=2),                            # kept forever
    ]
    shows = [
        _show(20, "The Boys", 500, [(400, None), (400, None)], tmdb=76479),
        _show(21, "Bluey", 500, [(400, None), (5, None)], tmdb=82728),           # a new episode
        _show(22, "Severance", 500, [(400, None)], tmdb=95396),                  # a member watched one
        _show(23, "Home Movies", 500, [(400, None)]),                            # requested, by title
        _show(24, "Andor", 500, [(400, 20), (400, None)], tmdb=83867),           # the owner watched one
        _show(25, "Shogun", 500, [(400, None), (87, None)], tmdb=126308),        # newest episode 87 days
    ]
    kids = [_film(30, "Coco", 500, tmdb=354912)]
    music = [_film(40, "An album", 500)]
    plex = _Plex([("1", "Movies", "movie", films), ("2", "TV Shows", "show", shows),
                  ("3", "Kids", "movie", kids), ("4", "Music", "artist", music)],
                 history=[_View(6, 3), _View(221, 4, show=22)])
    requests = _Requests(_requested(17431, "movie", "Moon", 2), _requested(99, "tv", "Home Movies", 1),
                         _requested(438631, "movie", "Dune", 200))
    warned = {"2": _at(3).isoformat(), "3": _at(30).isoformat(), "4": _at(30).isoformat(),
              "20": _at(30).isoformat(), "25": _at(30).isoformat()}
    config = {"enabled": True, "inactivity_days": 90, "notify_days_before": 7, "dry_run": False,
              "exempt_items": {"9": {"title": "Kept", "type": "movie"}}, "exclude_libraries": ["Kids"]}
    return plex, requests, warned, config


def _site(plex, requests, warned, config):
    with requests:
        return countdown.compute(plex, config, countdown.request_times(), dict(warned))


def _bot(plex, requests, warned, config):
    """{rating key: days until removal} for every title the bot warns about or removes,
    and each one's last activity."""
    cog = _cog()
    del cog._get_recent_request_timestamp            # the real lookup, against the same requests
    cog.config = config
    cog.services.plex_server = plex
    with requests:
        notify, delete = cog._scan_libraries_for_cleanup(dict(warned))
    left = {d["rating_key"]: 0 for d in delete}
    left.update({n["rating_key"]: n["days_until_deletion"] for n in notify})
    return left, {r["rating_key"]: r["last_viewed"] for r in notify + delete}


def test_the_website_and_the_bot_agree_on_every_warning_and_removal():
    plex, requests, warned, config = _household()
    site = _site(plex, requests, warned, config)
    bot, last = _bot(plex, requests, warned, config)

    due = {rk: c["daysLeft"] for rk, c in site.items() if c.get("warning")}
    assert due == {"1": 7, "2": 4, "3": 0, "4": 4, "20": 0, "25": 3}, due
    assert bot == due, f"the bot would act on {bot}, the countdown says {due}"
    for rk, when in last.items():
        apart = abs(parse_utc(when) - parse_utc(site[rk]["lastActivity"]))
        assert apart < timedelta(seconds=2), (rk, when, site[rk]["lastActivity"])

    kept = {rk: c["reason"] for rk, c in site.items() if not c["exempt"] and not c["warning"]}
    assert kept == {"5": "watched", "6": "watched", "7": "requested", "8": "added", "21": "added",
                    "22": "watched", "23": "requested", "24": "watched"}, kept


def test_whatever_brings_a_title_back_brings_it_back_on_both():
    """Watched, a new episode, a member's play, kept forever, longer to go: each title
    the bot stops warning about leaves the countdown too."""
    plex, requests, warned, config = _household()
    films, shows = plex.sections[0][3], plex.sections[1][3]
    films[0].lastViewedAt = _local(1)                              # Dune watched by the owner
    shows[0]._episodes.append(_Episode(2))                         # a new episode of The Boys
    plex.views.append(_View(3, 6))                                 # a member watched Solaris
    config["exempt_items"]["2"] = {"title": "Heat", "type": "movie"}
    config["inactivity_days"] = 120                                # Arrival and Shogun have longer

    site = _site(plex, requests, warned, config)
    bot, _ = _bot(plex, requests, warned, config)
    due = {rk for rk, c in site.items() if c.get("warning")}
    assert due == set() and bot == {}, f"the countdown still warns about {due}, the bot about {bot}"


def test_a_title_kept_forever_is_listed_as_kept_and_never_counted_down():
    plex, requests, warned, config = _household()
    config["exempt_items"]["20"] = {"title": "The Boys", "type": "show"}
    site = _site(plex, requests, {**warned, "9": _at(30).isoformat()}, config)

    for rk, title in (("9", "Kept"), ("20", "The Boys")):
        assert site[rk]["exempt"] is True and site[rk]["title"] == title, site[rk]
        assert not {"daysLeft", "warning", "lastActivity"} & set(site[rk]), site[rk]
    assert "/library/metadata/20/allLeaves" not in plex.asked, "an exempt show's episodes were read"
    assert site["9"]["tmdb"] == 2 and site["20"]["type"] == "show"

    config["exempt_items"] = {}
    site = _site(plex, requests, warned, config)
    assert site["9"]["exempt"] is False and site["9"]["daysLeft"] == 7
    assert site["20"]["exempt"] is False and site["20"]["daysLeft"] == 0


def test_a_skipped_library_is_never_read_or_listed():
    plex, requests, warned, config = _household()
    site = _site(plex, requests, warned, config)
    assert "30" not in site and "40" not in site, "a skipped or music library's titles were listed"
    assert "/library/sections/3/all?includeGuids=1" not in plex.asked, "the skipped library was read"
    assert "/library/sections/4/all?includeGuids=1" not in plex.asked, "a music library was read"

    config["exclude_libraries"] = []
    site = _site(plex, requests, warned, config)
    assert site["30"]["warning"] is True and site["30"]["daysLeft"] == 7, "once not skipped, warned in full"
    assert "40" not in site


def test_both_read_a_titles_ids_the_same_way():
    """A title's TMDB number on its own guid, or a guid plexapi gives by its tag: the
    bot and the countdown match its request and its "keep forever" by the same ids."""
    plex, requests, warned, config = _household()
    films = plex.sections[0][3]
    matrix = _film(50, "Matrix", 400)
    matrix.guid = "tmdb://603"                                      # requested as "The Matrix"
    ronin = _film(51, "Ronin", 400)
    ronin.guids = [_Tag("tmdb://8195")]                             # kept, under the key it had before
    films += [matrix, ronin]
    requests = _Requests(_requested(17431, "movie", "Moon", 2), _requested(99, "tv", "Home Movies", 1),
                         _requested(438631, "movie", "Dune", 200), _requested(603, "movie", "The Matrix", 2))
    config["exempt_items"]["999"] = {"title": "Ronin", "type": "movie", "ids": {"tmdb": "8195"}}

    site = _site(plex, requests, warned, config)
    bot, _ = _bot(plex, requests, warned, config)
    assert site["50"]["reason"] == "requested" and not site["50"]["warning"], site["50"]
    assert site["51"]["exempt"] is True, site["51"]
    assert "50" not in bot and "51" not in bot, f"the bot would act on {bot}"
    due = {rk: c["daysLeft"] for rk, c in site.items() if c.get("warning")}
    assert bot == due, f"the bot would act on {bot}, the countdown says {due}"


def test_one_rule_dates_and_judges_every_title():
    """The rule both share: the latest activity, and what that means at 90 days with 7
    of warning (or whatever an admin set)."""
    config = {"inactivity_days": 90, "notify_days_before": 7}

    def judged(watched=None, added=None, requested=None, settings=config):
        days = [None if d is None else NOW - timedelta(days=d) for d in (watched, added, requested)]
        return countdown.judge(*days, settings, NOW)

    assert judged() is None, "nothing dates it"
    assert judged(watched=10, added=400) == {"last": NOW - timedelta(days=10), "reason": "watched",
                                             "inactive": 10, "action": None}
    assert judged(watched=400, added=5)["reason"] == "added"
    assert judged(watched=40, added=400, requested=3)["reason"] == "requested"
    assert judged(watched=None, added=None, requested=95)["reason"] == "requested"
    assert judged(watched=20, added=20)["reason"] == "watched", "a tie stays watched"

    assert [judged(added=d)["action"] for d in (82, 83, 89, 90, 500)] == [None, "notify", "notify", "delete", "delete"]
    shorter = {"inactivity_days": "30", "notify_days_before": "10"}
    assert [judged(added=d, settings=shorter)["action"] for d in (19, 20, 30)] == [None, "notify", "delete"]
