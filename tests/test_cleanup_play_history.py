# path: tests/test_cleanup_play_history.py
"""Who watched what, from Plex's history as Plex sends it now.

The history stopped saying which show an episode is from: no grandparentRatingKey on
any of 748 plays (November 2026). Plays were filed under each episode, so every show
looked unwatched: request expiry switched VisionQuest off in Sonarr a week before its
finale, and cleanup warned about Hacks although a member watched it in September."""
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import conftest  # noqa: F401
from plexapi.exceptions import NotFound

from plugins.media_cleanup.cog import MediaCleanupCog
from portal.cleanup import everyones_views

VISIONQUEST, HACKS, BOYS = "11758", "10876", "76"
EPISODES = {"12196": VISIONQUEST, "900": BOYS}       # episodes still in Plex, and their shows


class Play:
    """A history entry with only what this Plex sends; `attrs` adds the rest."""

    def __init__(self, days, type="episode", **attrs):
        self.viewedAt = datetime.now() - timedelta(days=days)
        self.type = type
        for name, value in attrs.items():
            setattr(self, name, value)


def _xml(body):
    return ET.fromstring(f"<MediaContainer>{body}</MediaContainer>")


class Plex:
    """A film library and a TV library; a remake and a show title each listed twice."""

    def __init__(self, plays):
        self.plays, self.queries = plays, []

    def history(self, mindate=None):
        return list(self.plays)

    def query(self, path):
        self.queries.append(path)
        if path == "/library/sections":
            return _xml('<Directory key="1" type="movie" title="Films"/><Directory key="2" type="show" title="TV"/>'
                        '<Directory key="3" type="artist" title="Music"/>')
        if path.startswith("/library/sections/1/all"):
            return _xml('<Video ratingKey="500" title="The Matrix"><Guid id="tmdb://603"/></Video>'
                        '<Video ratingKey="501" title="Open Season"/><Video ratingKey="502" title="Open Season"/>')
        if path.startswith("/library/sections/2/all"):
            return _xml(f'<Directory ratingKey="{VISIONQUEST}" title="VisionQuest">'
                        '<Guid id="imdb://tt23112594"/><Guid id="tmdb://213375"/><Guid id="tvdb://426606"/></Directory>'
                        f'<Directory ratingKey="{HACKS}" title="Hacks"><Guid id="tmdb://94954"/></Directory>'
                        f'<Directory ratingKey="{BOYS}" title="The Boys"/>'
                        '<Directory ratingKey="80" title="The Office"/><Directory ratingKey="81" title="The Office"/>')
        if path.startswith("/library/metadata/"):
            found = [k for k in path.rsplit("/", 1)[1].split(",") if k in EPISODES]
            if not found:
                raise NotFound("(404) not_found")       # Plex: none of them is still there
            return _xml("".join(f'<Video ratingKey="{k}" type="episode" grandparentRatingKey="{EPISODES[k]}"/>'
                                for k in found))
        raise AssertionError(f"unexpected Plex request {path}")


def _days_ago(views, key):
    return round((datetime.now(timezone.utc) - views[key]).total_seconds() / 86400)


def test_an_episode_play_counts_for_its_show_though_plex_leaves_the_show_out():
    plex = Plex([Play(1, ratingKey=12196, grandparentTitle="VisionQuest", librarySectionID=2)])
    views = everyones_views(plex, 90)
    assert _days_ago(views, VISIONQUEST) == 1
    assert "12196" not in views, "the play is the show's, not one episode's"
    assert plex.queries == ["/library/metadata/12196"], "found through its episode, no title lookup needed"


def test_a_play_with_only_the_show_link_or_only_its_title_still_counts():
    plex = Plex([Play(3, grandparentKey=f"/library/metadata/{BOYS}", grandparentTitle="The Boys", librarySectionID=2),
                 Play(52, grandparentTitle="Hacks", librarySectionID=2, accountID=31699736)])   # an episode since replaced
    views = everyones_views(plex, 90)
    assert _days_ago(views, BOYS) == 3
    assert _days_ago(views, HACKS) == 52


def test_a_title_listed_twice_keeps_both_rather_than_guessing():
    plex = Plex([Play(5, grandparentTitle="The Office", librarySectionID=2),
                 Play(7, type="movie", title="Open Season", librarySectionID=1)])
    views = everyones_views(plex, 90)
    assert {k for k in views} == {"80", "81", "501", "502"}


def test_plays_that_still_name_their_show_or_film_need_no_lookups():
    plex = Plex([Play(2, grandparentRatingKey=76, ratingKey=900), Play(4, type="movie", ratingKey=500)])
    views = everyones_views(plex, 90)
    assert _days_ago(views, BOYS) == 2 and _days_ago(views, "500") == 4
    assert plex.queries == []


def test_gone_episodes_and_unknown_titles_count_for_nothing():
    plex = Plex([Play(1, ratingKey=4444, grandparentTitle="Cancelled Show", librarySectionID=2),
                 Play(1, type="track", title="A Song", librarySectionID=3)])
    assert everyones_views(plex, 90) == {}


def test_the_newest_play_of_any_episode_wins():
    plex = Plex([Play(40, ratingKey=12196), Play(2, grandparentTitle="VisionQuest", librarySectionID=2),
                 Play(9, grandparentKey=f"/library/metadata/{VISIONQUEST}")])
    assert _days_ago(everyones_views(plex, 90), VISIONQUEST) == 2


def _cog(plex):
    cog = object.__new__(MediaCleanupCog)
    cog.config = {"enabled": True, "inactivity_days": 90, "notify_days_before": 7, "exempt_items": {},
                  "dry_run": True, "exclude_libraries": []}
    cog._get_recent_request_timestamp = lambda item: None

    class Services:
        plex_server = plex
    cog.services = Services()
    return cog


def test_request_expiry_keeps_a_show_someone_is_watching_monitored():
    plex = Plex([Play(1, ratingKey=12196, grandparentTitle="VisionQuest", librarySectionID=2)])
    kept = _cog(plex)._kept_from_expiry(datetime.now(timezone.utc) - timedelta(days=90))
    assert ("tv", "tmdb", "213375") in kept and ("tv", "tvdb", "426606") in kept
    assert not any(k[2] == "94954" for k in kept), "nobody watched Hacks here"


class _Episode:
    def __init__(self):
        self.lastViewedAt = None                    # the owner never watched it
        self.addedAt = datetime.now() - timedelta(days=200)


class _Show:
    type = "show"

    def __init__(self, key, title):
        self.ratingKey, self.title = key, title
        self.addedAt = datetime.now() - timedelta(days=200)

    def episodes(self):
        return [_Episode(), _Episode()]


def test_cleanup_keeps_a_show_only_a_member_watched():
    plex = Plex([Play(52, grandparentTitle="Hacks", librarySectionID=2, accountID=31699736)])
    cog = _cog(plex)
    views = cog._everyones_views()
    assert cog.check_item_for_cleanup(_Show(int(HACKS), "Hacks"), views) is None
    assert cog.check_item_for_cleanup(_Show(int(BOYS), "The Boys"), views)["action"] == "delete"
