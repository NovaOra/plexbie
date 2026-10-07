# path: tests/test_cleanup_brakes.py
"""Media cleanup: brakes on what one run removes.

A title was removed as soon as it had been idle long enough, whether or not it had
ever been warned about. Lowering the inactivity days, adding a library or un-skipping
one, Plexbie being down for longer than the warning window, or going live for the
first time each removed everything already past the threshold in one run, with no
warning for any of it. Nothing capped a run, and a watch history that Plex answered
but left empty made every title on the server look untouched.
"""
import asyncio
import copy
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import conftest  # noqa: F401

from plugins.media_cleanup import cog as cleanup
from portal import cleanup as countdown
from utils.formatting import parse_utc
from test_cleanup_whole_removal import Movie, _cog, _libraries, _watched


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _idle(movie, days):
    movie.addedAt = datetime.now() - timedelta(days=days)
    return movie


class _Store:
    """The cleanup namespace, in memory instead of the database; last checked an hour
    ago unless a test says otherwise."""

    def __init__(self, **rows):
        self.rows = copy.deepcopy({"warned_checked": _ago(1 / 24), **rows})

    async def kv_get(self, namespace, key, default=None):
        return copy.deepcopy(self.rows.get(key, default))

    async def kv_set(self, namespace, key, value):
        self.rows[key] = copy.deepcopy(value)


def _wired(cog):
    """Stand in for Sonarr/Radarr and Discord; returns (removed titles, notices sent)."""
    removed, notices = [], []

    async def ready():
        return True

    async def monitor():
        return {}

    async def nothing():
        return 0

    async def delete_media_items(items):
        removed.extend(item["title"] for item in items)
        return items

    async def notify(items, kind):
        notices.append((kind, [(item["title"], item.get("days_until_deletion")) for item in items]))
    cog.load_data, cog.enforce_request_monitor_cleanup, cog.reconcile_seerr = ready, monitor, nothing
    cog.delete_media_items, cog.send_cleanup_notification = delete_media_items, notify
    cog._scan_lock = asyncio.Lock()
    return removed, notices


def _scan(cog, store, daily=False):
    """One scan against `store`: the daily check's, or an admin's from the panel or website."""
    saved = cleanup.kv_get, cleanup.kv_set
    cleanup.kv_get, cleanup.kv_set = store.kv_get, store.kv_set
    try:
        return asyncio.run(cog._daily_scan() if daily else cog.scan_now())
    finally:
        cleanup.kv_get, cleanup.kv_set = saved


def test_a_title_is_removed_only_once_it_was_warned_for_the_whole_warning_period():
    """A library added (or un-skipped) while live: its film had been idle for 400 days."""
    cog = _cog()
    _libraries(cog, ("Movies", "movie", [Movie(1, "Dune", 2021, ["tmdb://438631"])]))
    removed, notices = _wired(cog)
    store = _Store()

    _scan(cog, store)
    assert removed == [], "removed without ever being warned"
    assert notices == [("warning", [("Dune", 7)])], notices
    assert set(store.rows["warned"]) == {"1"}

    store.rows["warned"]["1"] = _ago(3)
    notices.clear()
    _scan(cog, store)
    assert removed == [] and notices == [("warning", [("Dune", 4)])], notices

    store.rows["warned"]["1"] = _ago(7)
    _scan(cog, store)
    assert removed == ["Dune"]


def test_lowering_the_inactivity_days_warns_before_anything_goes():
    cog = _cog()
    cog.config["inactivity_days"] = 30
    _libraries(cog, ("Movies", "movie", [_idle(Movie(n, f"Film {n}", 2000 + n), 60) for n in range(1, 4)]))
    removed, notices = _wired(cog)
    _scan(cog, _Store(warned={}))
    assert removed == [] and [days for _, days in notices[0][1]] == [7, 7, 7], notices


def test_a_title_no_longer_due_forgets_its_warning():
    """Watched since, or gone from Plex: a later idle spell starts a new warning."""
    cog = _cog()
    _libraries(cog, ("Movies", "movie", [Movie(1, "Dune", 2021, ["tmdb://438631"]),
                                         _watched(Movie(2, "Heat", 1995, ["tmdb://949"]), 5)]))
    _wired(cog)
    store = _Store(warned={"1": _ago(30), "2": _ago(30), "9": _ago(30)})
    _scan(cog, store)
    assert set(store.rows["warned"]) == {"1"}


def test_a_copy_in_use_forgets_the_warning_of_every_copy():
    cog = _cog()
    idle = Movie(1, "Dune", 2021, ["tmdb://438631"])
    _libraries(cog, ("Movies 4K", "movie", [idle]),
               ("Movies", "movie", [_watched(Movie(2, "Dune", 2021, ["tmdb://438631"]), 5)]))
    _wired(cog)
    store = _Store(warned={"1": _ago(30)})
    _scan(cog, store)
    assert store.rows["warned"] == {}


def _many(due, of):
    """`of` films with distinct titles; the first `due` idle and warned long ago, the rest watched."""
    films = [Movie(n, f"Film {n}", 1950 + n) for n in range(1, of + 1)]
    for film in films[due:]:
        _watched(film, 5)
    return films, {str(n): _ago(30) for n in range(1, due + 1)}


def test_the_daily_check_removes_nothing_when_too_many_are_due_and_tells_the_admins():
    cog = _cog()
    films, warned = _many(20, 20)
    _libraries(cog, ("Movies", "movie", films))
    removed, notices = _wired(cog)
    _scan(cog, _Store(warned=warned), daily=True)
    assert removed == [], "the daily check removed every title on the server in one go"
    assert [(kind, len(items)) for kind, items in notices] == [("held", 20)], notices


def test_the_brake_allows_five_titles_or_a_tenth_of_the_library():
    for due, of, removes in ((5, 20, True), (6, 20, False), (6, 60, True), (7, 60, False)):
        films, warned = _many(due, of)
        cog = _cog(history=[(str(n), 5) for n in range(due + 1, of + 1)])   # the watched ones, in the history
        _libraries(cog, ("Movies", "movie", films))
        removed, notices = _wired(cog)
        _scan(cog, _Store(warned=warned), daily=True)
        assert (len(removed) == due) is removes and (removed == []) is not removes, (due, of, removed)
        assert any(kind == "held" for kind, _ in notices) is not removes, (due, of, notices)


def test_an_admins_own_scan_is_the_go_ahead():
    cog = _cog()
    films, warned = _many(20, 20)
    _libraries(cog, ("Movies", "movie", films))
    removed, _ = _wired(cog)
    _scan(cog, _Store(warned=warned))
    assert len(removed) == 20


def test_an_empty_watch_history_on_a_big_library_is_taken_as_unreadable():
    films, warned = _many(60, 60)
    cog = _cog(history=[])
    _libraries(cog, ("Movies", "movie", films))
    removed, notices = _wired(cog)
    store = _Store(warned=warned)
    _scan(cog, store)
    assert removed == [], "an empty history made every title look unwatched"
    assert notices == [("paused", [])], "the admins are told cleanup has paused"
    assert store.rows["warned"] == warned, "nothing was judged, so no warning is forgotten"
    assert countdown.compute(_Plex([(str(n), f"Film {n}", 400) for n in range(1, 61)]),
                             {"inactivity_days": 90}, [], warned) == {}, "the website counts down nothing either"

    cog = _cog(history=[("999", 3)])                  # someone watched something
    _libraries(cog, ("Movies", "movie", films))
    removed, _ = _wired(cog)
    _scan(cog, _Store(warned=warned))
    assert len(removed) == 60

    small, small_warned = _many(3, 3)
    cog = _cog(history=[])                            # a small library can really be idle
    _libraries(cog, ("Movies", "movie", small))
    removed, _ = _wired(cog)
    _scan(cog, _Store(warned=small_warned))
    assert len(removed) == 3


def test_warnings_start_again_after_days_without_a_check():
    """Plexbie down, or cleanup switched off, for longer than the warning: the one
    warning sent before that gap isn't enough."""
    cog = _cog()
    films, warned = _many(3, 3)
    _libraries(cog, ("Movies", "movie", films))
    removed, notices = _wired(cog)
    store = _Store(warned=warned, warned_checked=_ago(40))
    _scan(cog, store)
    assert removed == [] and [days for _, days in notices[0][1]] == [7, 7, 7], notices
    assert all(parse_utc(when) > datetime.now(timezone.utc) - timedelta(minutes=5)
               for when in store.rows["warned"].values()), store.rows["warned"]
    assert parse_utc(store.rows["warned_checked"]) > datetime.now(timezone.utc) - timedelta(minutes=5)

    now = datetime.now(timezone.utc)
    assert countdown.current_warnings(warned, _ago(2), now) == warned
    assert countdown.current_warnings(warned, None, now) == {}
    assert countdown.current_warnings(["not", "a", "record"], _ago(0), now) == {}


def test_a_damaged_warning_date_starts_again_instead_of_warning_forever():
    cog = _cog()
    _libraries(cog, ("Movies", "movie", [Movie(1, "Dune", 2021, ["tmdb://438631"])]))
    removed, notices = _wired(cog)
    store = _Store(warned={"1": "last tuesday"})
    _scan(cog, store)
    assert removed == [] and notices == [("warning", [("Dune", 7)])], notices
    assert parse_utc(store.rows["warned"]["1"]), store.rows["warned"]


def test_practice_mode_warnings_count_once_live():
    """Practice mode warns (in its reports) just as live does, so going live doesn't
    restart the warning; the daily limit still holds back a large first removal."""
    cog = _cog()
    cog.config["dry_run"] = True
    _libraries(cog, ("Movies", "movie", [Movie(1, "Dune", 2021, ["tmdb://438631"])]))
    removed, notices = _wired(cog)
    store = _Store()
    _scan(cog, store, daily=True)
    assert notices == [("warning", [("Dune", 7)])] and set(store.rows["warned"]) == {"1"}

    store.rows["warned"]["1"] = _ago(8)
    cog.config["dry_run"] = False
    _scan(cog, store, daily=True)
    assert removed == ["Dune"]


def test_when_titles_were_warned_unreadable_means_warnings_only():
    async def broken(namespace, key, default=None):
        raise RuntimeError("database is locked")
    cog = _cog()
    films, warned = _many(3, 3)
    _libraries(cog, ("Movies", "movie", films))
    removed, notices = _wired(cog)
    store = _Store(warned=warned)
    store.kv_get = broken
    _scan(cog, store)
    assert removed == [] and [kind for kind, _ in notices] == ["warning"]
    assert store.rows["warned"] == warned, "nothing written over the records it couldn't read"


def test_the_held_notice_goes_to_the_admins():
    sent = []

    class Channel:
        async def send(self, embed=None):
            sent.append(embed)

    class Bot:
        def get_channel(self, channel_id):
            return Channel() if channel_id == 42 else None

    class Config:
        admin_channel_id = 42
    cog = _cog()
    cog.bot = Bot()
    cog.services.config = Config()
    cog.config["notification_channel_id"] = 7                 # the household's warnings channel
    held = [{"title": "Dune", "type": "movie", "days_inactive": 400}] * 8
    asyncio.run(cog.send_cleanup_notification(held, "held"))
    assert len(sent) == 1 and "8" in sent[0].description and "Run Scan Now" in sent[0].description
    assert "Scan now under Manage" in sent[0].description and "to remove them" in sent[0].description

    cog.config["dry_run"] = True
    asyncio.run(cog.send_cleanup_notification(held, "held"))
    assert "DRY RUN" in sent[1].title and "would-remove" in sent[1].description
    assert "to remove them" not in sent[1].description, "practice mode's scan removes nothing"

    asyncio.run(cog.send_cleanup_notification([], "paused"))
    assert len(sent) == 3 and "unreadable" in sent[2].description


class _Plex:
    """A Plex server as the website's countdown reads it: films (rating key, title, days idle)."""

    def __init__(self, films):
        self.films = films

    def history(self, mindate=None):
        return []

    def query(self, path):
        if path == "/library/sections":
            return ET.fromstring('<MediaContainer><Directory key="1" type="movie" title="Movies"/></MediaContainer>')
        now = datetime.now(timezone.utc)
        return ET.fromstring("<MediaContainer>" + "".join(
            f'<Video ratingKey="{rk}" title="{title}" addedAt="{int((now - timedelta(days=idle)).timestamp())}"/>'
            for rk, title, idle in self.films) + "</MediaContainer>")


def test_the_website_countdown_waits_for_the_warning_too():
    films = [("1", "Never warned", 200), ("2", "Warned 3 days ago", 200), ("3", "Warned long ago", 200),
             ("4", "In the window, warned", 85), ("5", "In the window, not yet warned", 85), ("6", "Fresh", 10)]
    warned = {"2": _ago(3), "3": _ago(30), "4": _ago(2)}
    config = {"inactivity_days": 90, "notify_days_before": 7}
    out = countdown.compute(_Plex(films), config, [], warned)
    assert {rk: c["daysLeft"] for rk, c in out.items()} == {"1": 7, "2": 4, "3": 0, "4": 5, "5": 7, "6": 80}
    assert [rk for rk, c in out.items() if c["warning"]] == ["1", "2", "3", "4", "5"]

    # The bot's own schedule for the same titles: removed exactly when the countdown says 0.
    cog = _cog()
    _libraries(cog, ("Movies", "movie", [_idle(Movie(int(rk), title), idle) for rk, title, idle in films]))
    notify, delete = cog._scan_libraries_for_cleanup(dict(warned))
    bot = {d["rating_key"]: 0 for d in delete}
    bot.update({n["rating_key"]: n["days_until_deletion"] for n in notify})
    assert bot == {rk: c["daysLeft"] for rk, c in out.items() if c["warning"]}


def test_the_website_countdown_carries_each_titles_year():
    """Keeping a title from the countdown stores its year, as /cleanup exempt add does."""
    class Dated(_Plex):
        def query(self, path):
            root = super().query(path)
            for el in root.findall("Video"):
                el.set("year", "2010" if el.get("ratingKey") == "1" else "")
            return root

    out = countdown.compute(Dated([("1", "Sintel", 10), ("2", "No year", 10)]), {"inactivity_days": 90}, [], {})
    assert (out["1"]["year"], out["2"]["year"]) == (2010, None)
