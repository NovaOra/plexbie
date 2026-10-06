# path: tests/test_arrival_announcements.py
"""New-episode announcements: one post per season run, then silent edits."""
import asyncio
from datetime import timedelta

import conftest  # noqa: F401

from core.config import Config
from helpers import FakeServices


class Channel:
    id = 55

    def __init__(self):
        self.posts, self.edits, self.contents = [], [], []

    async def send(self, embed=None, content=None, allowed_mentions=None):
        self.posts.append(embed)
        self.contents.append(content)

        class M:
            id = 1000 + len(self.posts)
        return M()

    def get_partial_message(self, message_id):
        channel = self

        class P:
            async def edit(self, embed=None):
                channel.edits.append((message_id, embed))
        return P()


def _cog(season_size, ping_role=None):
    from plugins.new_media_added.cog import NewMediaAddedCog
    channel = Channel()
    cfg = Config()
    cfg.updates_channel_id = 55
    cfg.arrivals_role_id = ping_role

    class Bot:
        def get_channel(self, _):
            return channel

    cog = object.__new__(NewMediaAddedCog)
    cog.bot, cog.services = Bot(), FakeServices(cfg)
    cog.active_batches, cog._data_loaded = {}, True
    from collections import defaultdict
    cog._batch_locks, cog._announce_lock, cog._show_ids_cache = defaultdict(asyncio.Lock), asyncio.Lock(), {}
    cog._sweep_lock = asyncio.Lock()

    async def nothing(*a, **k):
        return None

    async def not_monitored(*a, **k):
        return False

    async def size(*a, **k):
        return season_size
    cog.load_tracking_data = nothing
    cog.save_tracking_data = nothing
    cog._check_if_monitored = not_monitored
    cog._fetch_tmdb_data = nothing
    cog._season_size = size
    return cog, channel


def _arrive(cog, episode, title="Ep"):
    meta = {"grandparentTitle": "The Simpsons", "parentIndex": 2, "index": episode, "title": title, "Guid": []}
    asyncio.run(cog._handle_new_episode(meta, {}))


def _age(cog, hours):
    batch = cog.active_batches["the simpsons:s2"]
    batch.last_update -= timedelta(hours=hours)


def test_a_season_arriving_over_hours_is_one_post_then_edits():
    cog, channel = _cog(season_size=3)
    _arrive(cog, 1, "Bart the Genius")
    _age(cog, 2)                      # downloads trickle in: 2 h apart
    _arrive(cog, 2)
    _age(cog, 5)
    _arrive(cog, 3)
    assert len(channel.posts) == 1, "only the first episode notifies the channel"
    assert len(channel.edits) == 2 and {m for m, _ in channel.edits} == {1001}
    assert "Bart the Genius" in channel.posts[0].description
    final = channel.edits[-1][1]
    assert final.title.startswith("✅") and "All 3 episodes" in final.description


def test_partway_through_it_shows_progress():
    cog, channel = _cog(season_size=22)
    for n in range(1, 10):
        _arrive(cog, n)
    progress = channel.edits[-1][1].description
    assert "Episodes 1-9" in progress and "9 of 22" in progress and "more on the way" in progress


def test_a_weekly_episode_gets_its_own_post():
    cog, channel = _cog(season_size=None)
    _arrive(cog, 1)
    _age(cog, 24 * 7)
    _arrive(cog, 2)
    assert len(channel.posts) == 2 and not channel.edits
    assert "S02E02" in channel.posts[1].description or "Episode 2" in channel.posts[1].description


def test_several_episodes_at_once_are_one_post_not_a_generic_show_message():
    """Plex reports 2+ episodes from one scan as a single show-level event with no
    episode numbers. Plexbie looks them up instead of posting "The Simpsons has been added!"."""
    from plugins.new_media_added import cog as module
    cog, channel = _cog(season_size=22)
    cog.services.plex_server = object()
    original = module._recently_added_episodes
    module._recently_added_episodes = lambda server, key, within: [(2, n, f"Ep {n}") for n in range(1, 6)]
    try:
        asyncio.run(cog.handle_plex_webhook({"event": "library.new", "Metadata": {
            "type": "show", "title": "The Simpsons", "ratingKey": "77", "Guid": []}}))
    finally:
        module._recently_added_episodes = original
    assert len(channel.posts) == 1 and not channel.edits
    assert "Episodes 1-5" in channel.posts[0].description and "5 of 22" in channel.posts[0].description


def test_only_new_posts_ping_the_opt_in_role():
    cog, channel = _cog(season_size=3, ping_role=777)
    _arrive(cog, 1)
    _arrive(cog, 2)
    assert channel.contents == ["<@&777>"], "one ping for the post, none for the edit"
    assert len(channel.edits) == 1


def test_the_same_episode_reported_twice_changes_nothing():
    """Plex's webhook and Tautulli's recently-added can both report one arrival."""
    cog, channel = _cog(season_size=3)
    _arrive(cog, 1)
    _arrive(cog, 1)
    assert len(channel.posts) == 1 and not channel.edits


def test_a_movie_reported_twice_is_announced_once():
    from plugins.new_media_added import cog as module
    cog, channel = _cog(season_size=None)
    store = {}

    async def kv_get(ns, key, default=None):
        return store.get(key, default)

    async def kv_set(ns, key, value):
        store[key] = value
    original = module.kv_get, module.kv_set
    module.kv_get, module.kv_set = kv_get, kv_set
    try:
        for _ in range(2):
            asyncio.run(cog.handle_plex_webhook({"event": "library.new", "Metadata": {
                "type": "movie", "title": "Film", "Guid": [{"id": "tmdb://42"}]}}))
    finally:
        module.kv_get, module.kv_set = original
    assert len(channel.posts) == 1


def test_episodes_reported_at_the_same_moment_make_one_post():
    """Plex sends several library.new events within seconds of a season import."""
    cog, channel = _cog(season_size=4)
    slow_send = channel.send

    async def send(*a, **k):
        await asyncio.sleep(0.05)          # Discord takes a moment
        return await slow_send(*a, **k)
    channel.send = send

    async def go():
        meta = lambda n: {"grandparentTitle": "The Simpsons", "parentIndex": 2, "index": n, "title": "Ep", "Guid": []}
        await asyncio.gather(*[cog._handle_new_episode(meta(n), {}) for n in (1, 2, 3, 4)])
    asyncio.run(go())
    assert len(channel.posts) == 1, f"{len(channel.posts)} posts (and pings) for one season import"


def test_plex_recently_added_is_announced_even_without_any_webhook():
    """Coven Academy reached Plex but no Plex webhook (no Plex Pass) or Tautulli event came."""
    from datetime import datetime
    from plugins.new_media_added import cog as module
    cog, channel = _cog(season_size=None)
    cog.services.plex_server = object()
    store, announced = {}, []

    async def kv_get(ns, key, default=None):
        return store.get(key, default)

    async def kv_set(ns, key, value):
        store[key] = value

    async def announce(key):
        announced.append(key)
    cog.announce_rating_key = announce
    from datetime import timedelta
    from datetime import timezone
    now = lambda: datetime.now(timezone.utc)
    items = [("old", now() - timedelta(days=3)), ("during-setup", now() - timedelta(minutes=30))]
    saved = module.kv_get, module.kv_set, module._recent_items
    module.kv_get, module.kv_set = kv_get, kv_set
    module._recent_items = lambda server: list(items)
    try:
        asyncio.run(module.NewMediaAddedCog.sweep_recently_added.coro(cog))     # first run: last 2 hours only
        assert announced == ["during-setup"], "the existing library isn't announced again"
        items.append(("coven-s1", now()))
        asyncio.run(module.NewMediaAddedCog.sweep_recently_added.coro(cog))
        asyncio.run(module.NewMediaAddedCog.sweep_recently_added.coro(cog))     # nothing new
    finally:
        module.kv_get, module.kv_set, module._recent_items = saved
    assert announced == ["during-setup", "coven-s1"]


def test_downloaded_but_never_on_plex_tells_the_admins_once_and_clears_when_it_arrives():
    from datetime import datetime, timedelta, timezone
    from plugins.new_media_added import cog as module
    from core import season_search
    from portal import help as helpdesk
    import database.request_store as request_store
    cog, channel = _cog(season_size=None)
    store, opened, resolved = {}, [], []
    state = {"waiting": True}
    now = datetime.now(timezone.utc).isoformat()

    async def all_requests():
        return {"12": {"status": "approved", "timestamp": now, "media_type": "tv", "seasons": [1],
                       "media": {"id": 298505, "media_type": "tv", "name": "Coven Academy"}},
                "13": {"status": "approved", "timestamp": now, "media_type": "audiobook",
                       "media": {"title": "A Book", "open_library_key": "x"}}}

    class Prog:
        async def video(self, media, seasons):
            return {"stage": "importing", "waitingForPlex": True} if state["waiting"] else {"stage": "available"}

    async def kv_get_all(ns):
        return dict(store)

    async def kv_set(ns, key, value):
        store[key] = value

    async def kv_set_many(ns, items):
        store.update(items)

    async def kv_delete_many(ns, keys):
        for k in keys:
            store.pop(k, None)

    async def open_help(bot, key, **kw):
        opened.append((key, kw["reason"]))
        return {"id": "h1"}

    async def resolve(hid, actor, reply):
        resolved.append(hid)
    cog._progress = Prog()
    saved = (module.kv_get_all, module.kv_set_many, module.kv_delete_many, season_search.open_help,
             helpdesk.resolve, request_store.all_requests)
    module.kv_get_all, module.kv_set_many, module.kv_delete_many = kv_get_all, kv_set_many, kv_delete_many
    season_search.open_help, helpdesk.resolve, request_store.all_requests = open_help, resolve, all_requests
    try:
        run = lambda: asyncio.run(cog.check_waiting_for_plex())
        run()
        assert list(store) == ["tv:298505:1"] and opened == [], "the clock starts; nobody is told yet"
        store["tv:298505:1"]["since"] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        run(); run()
        assert opened == [("12", "notonplex")], "the admins are told once"
        state["waiting"] = False
        run()
        assert store == {} and resolved == ["h1"], "Plex has it: the help request closes itself"
    finally:
        (module.kv_get_all, module.kv_set_many, module.kv_delete_many, season_search.open_help,
         helpdesk.resolve, request_store.all_requests) = saved



def test_a_download_plex_calls_another_film_is_flagged_once_and_cleared_when_put_right():
    """Obsession (2026): Radarr grabbed a namesake's release, Plex filed it as that film.
    The "not on Plex" alert closes, one "different film" one opens, and closes again
    once Plex has the requested film."""
    from datetime import datetime, timedelta, timezone
    from plugins.new_media_added import cog as module
    from core import season_search
    from portal import help as helpdesk
    import database.request_store as request_store
    cog, channel = _cog(season_size=None)
    then = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    store = {"movie:1339713": {"since": then, "alerted": then, "help": "old"}}
    opened, resolved = [], []
    state = {"ids": ["imdb://tt39365308"], "stage": "available"}
    now = datetime.now(timezone.utc).isoformat()

    async def all_requests():
        return {"20": {"status": "approved", "timestamp": now, "media_type": "movie",
                       "media": {"id": 1339713, "media_type": "movie", "title": "Obsession"}}}

    class Prog:
        async def video(self, media, seasons):
            if state["ids"]:
                return {"stage": "available", "plexIds": state["ids"], "wantedIds": ["imdb://tt37287335", "tmdb://1339713"]}
            return {"stage": state["stage"]}

    async def kv_get_all(ns):
        return dict(store)

    async def kv_set_many(ns, items):
        store.update(items)

    async def kv_delete_many(ns, keys):
        for k in keys:
            store.pop(k, None)

    async def open_help(bot, key, **kw):
        opened.append((key, kw["reason"], kw["note"]))
        return {"id": f"h{len(opened)}"}

    async def resolve(hid, actor, reply):
        resolved.append(hid)
    cog._progress = Prog()
    saved = (module.kv_get_all, module.kv_set_many, module.kv_delete_many, season_search.open_help,
             helpdesk.resolve, request_store.all_requests)
    module.kv_get_all, module.kv_set_many, module.kv_delete_many = kv_get_all, kv_set_many, kv_delete_many
    season_search.open_help, helpdesk.resolve, request_store.all_requests = open_help, resolve, all_requests
    try:
        run = lambda: asyncio.run(cog.check_waiting_for_plex())
        run(); run()
        assert resolved == ["old"], "the not-on-Plex alert closes: Plex does have the file"
        assert [o[:2] for o in opened] == [("20", "wrongfilm")], "one different-film alert"
        assert "IMDb tt39365308" in opened[0][2] and "IMDb tt37287335" in opened[0][2], opened[0][2]
        assert store["movie:1339713"]["wrong"] == ["imdb://tt39365308"], "the record survives the same pass"
        state["ids"], state["stage"] = [], "downloading"     # wrong file deleted, the right one on its way
        run()
        assert "movie:1339713" in store and resolved == ["old"], "not put right until Plex has the right film"
        state["stage"] = "available"                         # replaced, or rematched in Plex
        run()
        assert store == {} and resolved == ["old", "h1"], "put right: the alert closes itself"
    finally:
        (module.kv_get_all, module.kv_set_many, module.kv_delete_many, season_search.open_help,
         helpdesk.resolve, request_store.all_requests) = saved


def test_sweep_times_are_utc_so_a_new_tz_setting_doesnt_shift_them():
    from datetime import datetime, timezone
    from plugins.new_media_added.cog import _utc
    local = datetime(2026, 10, 3, 0, 45, 59)             # what plexapi and older marks look like
    assert _utc(local) == local.astimezone(timezone.utc) and _utc(local).tzinfo is timezone.utc
    aware = datetime(2026, 10, 3, 5, 0, tzinfo=timezone.utc)
    assert _utc(aware) == aware


def test_episode_posts_read_like_every_other_arrival():
    """Movies post "🎬 Film has been added!"; episodes say it the same way at every
    stage, with no stray flavour text ("acquired from across the infinite cosmos")."""
    cog, channel = _cog(season_size=3)
    _arrive(cog, 1)
    _arrive(cog, 2)
    _arrive(cog, 3)
    titles = [channel.posts[0].title] + [embed.title for _, embed in channel.edits]
    assert titles == [
        "📺 A new episode of The Simpsons has been added!",
        "📺 New episodes of The Simpsons have been added!",
        "✅ Season 2 of The Simpsons has been added!",
    ], titles


def test_a_download_of_another_film_for_a_request_is_stopped_and_replaced_once():
    """The sweep checks Radarr's downloads for requested films: Obsession's German
    namesake is removed and blocklisted, the real film grabbed, the admins told."""
    from datetime import datetime, timezone
    from plugins.new_media_added import cog as module
    from core import season_search
    import database.request_store as request_store
    cog, channel = _cog(season_size=None)
    cog.services.config.admin_channel_id = 77
    cog._grabs_checked = set()
    german = "Obsession.Du.sollst.mich.lieben.2025.German.DL.2160p.UHD.BluRay.HEVC-UNTHEVC-FTP"
    movie = {"id": 231, "tmdbId": 1339713, "title": "Obsession", "alternateTitles": []}
    other = {"id": 5, "tmdbId": 99, "title": "Not Requested", "alternateTitles": []}
    opened = []

    class Radarr:
        configured = True

        def __init__(self):
            self.removed, self.grabbed, self.parsed = [], [], 0

        async def queue(self, **params):
            items = [{"id": 9, "movieId": 231, "title": german, "downloadId": "SAB_1"},
                     {"id": 10, "movieId": 5, "title": "Something.Else.2026.1080p", "downloadId": "SAB_2"}]
            return [i for i in items if f"queue/{i['id']}" not in self.removed]

        async def get(self, path, **params):
            if path == "parse":
                self.parsed += 1
                return {"parsedMovieInfo": {"movieTitles": ["Obsession Du sollst mich lieben"]}}
            return movie if path == "movie/231" else other

        async def delete(self, path, **params):
            self.removed.append(path)

        async def releases(self, **params):
            return [{"title": "Obsession.2026.1080p.BluRay.x264-LuCY", "movieTitles": ["Obsession"], "approved": True,
                     "guid": "g1", "indexerId": 43}]

        async def post(self, path, body):
            self.grabbed.append(body["guid"])
    radarr = Radarr()
    cog.services.radarr = radarr
    now = datetime.now(timezone.utc).isoformat()

    async def all_requests():
        return {"20": {"status": "approved", "timestamp": now, "media": {"id": 1339713, "media_type": "movie"}}}

    async def open_help(bot, key, **kw):
        opened.append(kw)
    saved = request_store.all_requests, season_search.open_help
    request_store.all_requests, season_search.open_help = all_requests, open_help
    try:
        asyncio.run(cog.check_movie_grabs())
        asyncio.run(cog.check_movie_grabs())
    finally:
        request_store.all_requests, season_search.open_help = saved
    assert radarr.removed == ["queue/9"] and radarr.grabbed == ["g1"] and opened == []
    assert radarr.parsed == 1, "the unrequested film is left alone, and each download is checked once"
    assert channel.posts == [f"🛡️ **Obsession**: Stopped {german}: it isn't Obsession. "
                             "Found Obsession by its IDs and grabbed Obsession.2026.1080p.BluRay.x264-LuCY."]
    assert module.WATCH_DAYS > 0


def test_plex_matching_the_right_file_to_another_film_is_fixed_not_flagged():
    from datetime import datetime, timezone
    from plugins.new_media_added import cog as module
    from core import season_search
    import database.request_store as request_store
    cog, channel = _cog(season_size=None)
    store, opened, tried = {}, [], []
    now = datetime.now(timezone.utc).isoformat()

    async def all_requests():
        return {"20": {"status": "approved", "timestamp": now, "media_type": "movie",
                       "media": {"id": 1339713, "media_type": "movie", "title": "Obsession"}}}

    class Prog:
        async def video(self, media, seasons):
            return {"stage": "available", "plexIds": ["imdb://tt39365308"], "wantedIds": ["tmdb://1339713"], "plexKey": "11509"}

    async def kv_get_all(ns):
        return dict(store)

    async def kv_set_many(ns, items):
        store.update(items)

    async def kv_delete_many(ns, keys):
        for k in keys:
            store.pop(k, None)

    async def open_help(bot, key, **kw):
        opened.append(kw["reason"])
        return {"id": "h1"}

    async def fix(media, rating_key):
        tried.append(rating_key)
        return "Plex had matched Obsession (2026) to another film; fixed."
    cog._progress, cog._fix_plex_match = Prog(), fix
    saved = (module.kv_get_all, module.kv_set_many, module.kv_delete_many, season_search.open_help, request_store.all_requests)
    module.kv_get_all, module.kv_set_many, module.kv_delete_many = kv_get_all, kv_set_many, kv_delete_many
    season_search.open_help, request_store.all_requests = open_help, all_requests
    try:
        asyncio.run(cog.check_waiting_for_plex())
    finally:
        (module.kv_get_all, module.kv_set_many, module.kv_delete_many, season_search.open_help,
         request_store.all_requests) = saved
    assert tried == ["11509"] and opened == [] and store == {}, "fixed: no help request, nothing kept"


def _sweep_with(cog, module, plex_episodes):
    """One Recently Added check where Plex lists nothing new, and the show's episodes
    are plex_episodes: (season, number, title, added) with added a UTC datetime."""
    from datetime import datetime, timezone
    store = {"mark": datetime.now(timezone.utc).isoformat()}

    async def kv_get(ns, key, default=None):
        return store.get(key, default)

    async def kv_set(ns, key, value):
        store[key] = value

    def episodes(server, key, within):
        cutoff = datetime.now(timezone.utc) - within
        return [(s, n, t) for s, n, t, added in plex_episodes if added >= cutoff]
    saved = module.kv_get, module.kv_set, module._recent_items, module._recently_added_episodes
    module.kv_get, module.kv_set = kv_get, kv_set
    module._recent_items = lambda server: []
    module._recently_added_episodes = episodes
    try:
        asyncio.run(module.NewMediaAddedCog.sweep_recently_added.coro(cog))
    finally:
        module.kv_get, module.kv_set, module._recent_items, module._recently_added_episodes = saved


def test_the_rest_of_a_season_arriving_later_edits_the_announcement():
    """Bob's Burgers S2: episodes 1-3 were announced as "3 of 9 · more on the way", then
    4-9 reached Plex. Recently Added still listed the season with its first time, and no
    webhook came, so the post stayed at 3 of 9 while Plex had all nine."""
    from datetime import datetime, timezone
    from plugins.new_media_added import cog as module
    cog, channel = _cog(season_size=9)
    cog.services.plex_server = object()
    now = datetime.now(timezone.utc)
    original = module._recently_added_episodes
    module._recently_added_episodes = lambda server, key, within: [(2, n, f"Ep {n}") for n in range(1, 4)]
    try:
        asyncio.run(cog.handle_plex_webhook({"event": "library.new", "Metadata": {
            "type": "show", "title": "Bob's Burgers", "ratingKey": "88", "Guid": []}}))
    finally:
        module._recently_added_episodes = original
    assert "3 of 9" in channel.posts[0].description
    _sweep_with(cog, module, [(2, n, f"Ep {n}", now) for n in range(1, 10)])
    assert len(channel.posts) == 1, "the rest is an edit, not a second post"
    final = channel.edits[-1][1]
    assert final.title.startswith("✅") and "All 9 episodes" in final.description
    _sweep_with(cog, module, [(2, n, f"Ep {n}", now) for n in range(1, 10)])
    assert len(channel.edits) == 1, "a complete season isn't checked again"


def test_following_a_weekly_episode_doesnt_pull_in_earlier_weeks():
    from datetime import datetime, timezone
    from plugins.new_media_added import cog as module
    cog, channel = _cog(season_size=10)
    cog.services.plex_server = object()
    meta = {"grandparentTitle": "The Simpsons", "grandparentRatingKey": "77", "parentIndex": 2,
            "index": 5, "title": "Ep 5", "Guid": []}
    asyncio.run(cog._handle_new_episode(meta, {}))
    now = datetime.now(timezone.utc)
    weeks_ago = [(2, n, f"Ep {n}", now - timedelta(days=7 * (5 - n))) for n in range(1, 5)]
    _sweep_with(cog, module, weeks_ago + [(2, 5, "Ep 5", now)])
    assert not channel.edits, "episodes 1-4 came in earlier weeks; they aren't new"
    _sweep_with(cog, module, weeks_ago + [(2, 5, "Ep 5", now), (2, 6, "Ep 6", now)])
    assert "Episodes 5-6" in channel.edits[-1][1].description


def test_an_announcement_saved_before_show_keys_is_followed_by_title():
    from datetime import datetime, timezone
    from plugins.new_media_added import cog as module
    cog, channel = _cog(season_size=9)
    cog.services.plex_server = object()
    for n in (1, 2, 3):
        _arrive(cog, n)
    cog.active_batches["the simpsons:s2"].show_key = None
    saved = module._show_key_by_title
    module._show_key_by_title = lambda server, title: "77" if title == "The Simpsons" else None
    try:
        _sweep_with(cog, module, [(2, n, f"Ep {n}", datetime.now(timezone.utc)) for n in range(1, 10)])
    finally:
        module._show_key_by_title = saved
    assert "All 9 episodes" in channel.edits[-1][1].description
