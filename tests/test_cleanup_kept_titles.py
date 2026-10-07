# path: tests/test_cleanup_kept_titles.py
"""Media cleanup: kept titles and skipped libraries outlast a change in Plex.

A title kept forever was matched by its Plex key alone, and a skipped library by its
name alone. Renaming a skipped library made every title in it past the threshold
removable by the next daily check, and a title Plex listed again under a new key
(removed and added back, a rebuilt library) quietly lost its "keep forever".
"""
import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import conftest  # noqa: F401

from plugins.media_cleanup.cog import MediaCleanupCog
from portal import cleanup as countdown
from test_cleanup_brakes import _scan, _Store, _wired
from test_cleanup_settings_load import (ADMIN, STORED, _CaptureLog, _Interaction, _keep_website, _Plex, _run,
                                        _website)
from test_cleanup_whole_removal import Guid, Movie, Show, _cog, _libraries, _warned

DUNE = {"title": "Dune", "type": "movie", "ids": {"tmdb": "438631"}}


def _idle(item, days=500):
    item.addedAt = datetime.now(timezone.utc) - timedelta(days=days)
    return item


# --- keep forever ---

def test_a_kept_title_stays_kept_under_a_new_plex_key():
    cog = _cog()
    cog.config["exempt_items"] = {"1": DUNE}
    assert cog.check_item_for_cleanup(_idle(Movie(7, "Dune", 2021, ["tmdb://438631"])), {}) is None, \
        "added back to Plex under a new key, it lost its keep-forever"
    show = _idle(Show(8, []))
    show.guids = [Guid("tmdb://438631")]
    assert cog.check_item_for_cleanup(show, {})["action"] == "delete", "a show with the film's TMDB number is another title"
    assert cog.check_item_for_cleanup(_idle(Movie(9, "Heat", 1995, ["tmdb://949"])), {})["action"] == "delete"


def test_keeping_a_title_from_discord_stores_its_ids():
    async def body(cog):
        cog.services.plex_server = _Plex()
        await MediaCleanupCog.cleanup_exempt_add.callback(cog, _Interaction(), "Caminandes")

    _, stored = _run(body)
    assert stored["exempt_items"]["301"]["ids"] == {"tvdb": "276587", "tmdb": "46316"}, stored["exempt_items"]


def test_keeping_a_title_from_the_website_stores_its_ids():
    async def from_plex(cog):
        cog.services.plex_server = _Plex()
        return await _keep_website(cog).exempt(ADMIN, {"ratingKey": "301", "keep": True})

    (result, stored) = _run(from_plex)
    assert result["ok"] and stored["exempt_items"]["301"]["ids"] == {"tvdb": "276587", "tmdb": "46316"}

    async def listed():
        return {"5": {"ratingKey": "5", "title": "Old Film", "type": "movie", "year": 1999, "ids": {"tmdb": "62"}}}

    async def from_countdown(cog):
        return await _keep_website(cog, listed).exempt(ADMIN, {"ratingKey": "5", "keep": True})

    (result, stored) = _run(from_countdown)
    assert result["ok"] and stored["exempt_items"]["5"]["ids"] == {"tmdb": "62"}


def test_a_kept_title_plex_no_longer_has_is_logged():
    cog = _cog()
    cog.config["exempt_items"] = {"5": {"title": "Old Film", "type": "movie"}, "1": DUNE}
    _libraries(cog, ("Movies", "movie", [Movie(7, "Dune", 2021, ["tmdb://438631"])]))
    with _CaptureLog() as log:
        cog._scan_libraries_for_cleanup()
    lost = [m for m in log.messages if "kept forever" in m]
    assert len(lost) == 1 and "Old Film" in lost[0], log.messages


# --- skipped libraries ---

def test_a_renamed_skipped_library_stays_skipped():
    cog = _cog()
    _libraries(cog, ("Movies", "movie", [_idle(Movie(1, "Dune", 2021, ["tmdb://438631"]))], "1"),
               ("Children", "movie", [_idle(Movie(2, "Heat", 1995, ["tmdb://949"]))], "3"), excluded=["Kids"])
    cog.config["exclude_library_keys"] = {"Kids": "3"}
    notify, delete = cog._scan_libraries_for_cleanup(_warned(1, 2))
    assert [d["rating_key"] for d in delete] == ["1"], "a skipped library renamed in Plex was cleaned up"


def test_a_skipped_library_plex_no_longer_has_holds_every_removal():
    for daily in (True, False):
        cog = _cog()
        _libraries(cog, ("Movies", "movie", [_idle(Movie(1, "Dune", 2021, ["tmdb://438631"]))], "1"), excluded=["Kids"])
        removed, notices = _wired(cog)
        _scan(cog, _Store(warned=_warned(1)), daily=daily)
        assert removed == [], "a skipped library that matches nothing in Plex may be renamed; nothing should go"
        assert notices == [("missing", [("Kids", None)])], notices


def test_the_admins_hear_about_a_missing_skipped_library_once_even_with_nothing_due():
    cog = _cog()
    _libraries(cog, ("Movies", "movie", [Movie(1, "Dune", 2021, ["tmdb://438631"])], "1"), excluded=["Kids"])
    removed, notices = _wired(cog)
    store = _Store()

    def missing():
        return [titles for kind, titles in notices if kind == "missing"]

    _scan(cog, store)
    assert removed == [] and missing() == [[("Kids", None)]], notices
    _scan(cog, store)
    assert missing() == [[("Kids", None)]], "the admins were told again at the next check"
    cog.config["exclude_libraries"] = ["Kids", "Gone"]
    _scan(cog, store)
    assert missing()[1:] == [[("Kids", None), ("Gone", None)]], notices

    cog.config["exclude_libraries"] = []
    _scan(cog, store)
    assert store.rows["skipped_missing"] == [] and len(missing()) == 2, notices


def test_a_skipped_librarys_key_is_recorded_and_keeps_it_skipped_once_renamed():
    cog = _cog()
    cog._data_loaded, cog._stored = True, {}
    dune, heat = _idle(Movie(1, "Dune", 2021, ["tmdb://438631"])), _idle(Movie(2, "Heat", 1995, ["tmdb://949"]))
    _libraries(cog, ("Movies", "movie", [dune], "1"), ("Kids", "movie", [heat], "3"), excluded=["Kids"])
    removed, notices = _wired(cog)
    store = _Store(warned=_warned(1, 2))
    _scan(cog, store)
    assert removed == ["Dune"] and store.rows["config"]["exclude_library_keys"] == {"Kids": "3"}

    _libraries(cog, ("Movies", "movie", [dune], "1"), ("Children", "movie", [heat], "3"), excluded=["Kids"])
    _scan(cog, store)
    assert removed == ["Dune", "Dune"] and notices == [("deleted", [("Dune", None)])] * 2, (removed, notices)


def test_the_missing_library_notice_goes_to_the_admins():
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
    cog.bot, cog.services.config = Bot(), Config()
    cog.config["notification_channel_id"] = 7
    asyncio.run(cog.send_cleanup_notification([{"title": "Kids"}], "missing"))
    assert len(sent) == 1 and "Kids" in sent[0].description and "Manage → Cleanup" in sent[0].description
    assert "a library by that name" in sent[0].description
    asyncio.run(cog.send_cleanup_notification([{"title": "Kids"}, {"title": "Gone"}, {"title": "Old"}], "missing"))
    assert "Kids, Gone and Old" in sent[1].description and "libraries by those names" in sent[1].description


def test_skipping_a_library_on_the_website_records_its_key():
    async def body(cog):
        cog.services.plex_server = _Plex()
        assert await cog.load_data()
        return await _website(cog).cleanup_settings(ADMIN, {"excludedLibraries": ["Home Videos", "Gone"]})

    result, stored = _run(body)
    assert result["ok"] and stored["exclude_libraries"] == ["Gone", "Home Videos"]
    assert stored["exclude_library_keys"] == {"Home Videos": "3"}


def test_the_title_search_leaves_out_a_renamed_skipped_library():
    plex = _Plex()
    plex.shelves[2].title = "Family"

    async def body(cog):
        cog.services.plex_server = plex
        return await _keep_website(cog).cleanup_search(ADMIN, "sin")

    found, _ = _run(body, stored={**STORED, "exclude_library_keys": {"Home Videos": "3"}})
    assert [f["ratingKey"] for f in found] == ["201"] and plex.shelves[2].searched == 0, found


def test_the_website_shows_a_renamed_skipped_library_by_its_new_name():
    config = {"exclude_libraries": ["Kids", "Gone"], "exclude_library_keys": {"Kids": "3"}}
    assert countdown.skipped_names(config, {"Movies": "1", "Children": "3"}) == ["Children", "Gone"]
    # A new library given the old name is skipped by it as well, so the website shows both.
    assert countdown.skipped_names(config, {"Kids": "7", "Children": "3"}) == ["Kids", "Children", "Gone"]


def test_the_title_search_shows_a_title_kept_under_its_old_key_as_kept():
    async def body(cog):
        cog.services.plex_server = _Plex()
        return await _keep_website(cog).cleanup_search(ADMIN, "caminandes")

    found, _ = _run(body, stored={**STORED, "exempt_items": {
        "1": {"title": "Caminandes", "type": "show", "ids": {"tvdb": "276587"}}}})
    assert [(f["ratingKey"], f["kept"]) for f in found] == [("301", True)], found


# --- the website's countdown and request expiry ---

class _Listing:
    """Plex as the countdown and request expiry read it: (key, title, films) sections,
    each film (rating key, title, TMDB number) and idle for 500 days."""

    def __init__(self, *sections):
        self.sections = sections

    def history(self, mindate=None):
        return []

    def query(self, path):
        if path == "/library/sections":
            return ET.fromstring("<MediaContainer>" + "".join(
                f'<Directory key="{key}" type="movie" title="{title}"/>' for key, title, _ in self.sections)
                + "</MediaContainer>")
        added = int((datetime.now(timezone.utc) - timedelta(days=500)).timestamp())
        for key, _, films in self.sections:
            if path == f"/library/sections/{key}/all?includeGuids=1":
                return ET.fromstring("<MediaContainer>" + "".join(
                    f'<Video ratingKey="{rk}" title="{title}" addedAt="{added}"><Guid id="tmdb://{tmdb}"/></Video>'
                    for rk, title, tmdb in films) + "</MediaContainer>")
        raise AssertionError(f"unexpected Plex query {path}")


def _renamed_and_re_added():
    plex = _Listing(("1", "Movies", [("7", "Dune", 438631), ("8", "Heat", 949)]),
                    ("3", "Children", [("9", "Coco", 354912)]))
    config = {"inactivity_days": 90, "notify_days_before": 7, "exempt_items": {"1": DUNE},
              "exclude_libraries": ["Kids"], "exclude_library_keys": {"Kids": "3"}}
    return plex, config


def test_the_countdown_follows_kept_titles_and_skipped_libraries_too():
    plex, config = _renamed_and_re_added()
    out = countdown.compute(plex, config, [], {})
    assert out["7"]["exempt"] is True, "the countdown lost a kept title Plex re-added"
    assert out["8"]["exempt"] is False and out["8"]["ids"] == {"tmdb": "949"}
    assert "9" not in out, "the countdown listed a renamed skipped library"


def test_request_expiry_follows_kept_titles_and_skipped_libraries_too():
    plex, config = _renamed_and_re_added()
    cog = _cog()
    cog.config, cog.services.plex_server = config, plex
    kept = cog._kept_from_expiry(datetime.now(timezone.utc) - timedelta(days=90))
    assert ("movie", "tmdb", "438631") in kept and ("movie", "tmdb", "354912") in kept, kept
    assert ("movie", "tmdb", "949") not in kept
