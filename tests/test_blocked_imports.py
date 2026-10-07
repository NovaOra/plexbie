# path: tests/test_blocked_imports.py
"""Downloads Sonarr/Radarr won't import by themselves (core/blocked_imports.py).

East of Eden sat "import blocked" in Sonarr for four days: the release said
"East.Of.Eden", the show is "East of Eden (2026)", and nothing told anyone. Now each one
opens a ticket and alerts the admins at once, the ticket shows what's in it with what
looks off, and an admin imports it through Sonarr's own Manual Import, never blind."""
import asyncio
import os
import pathlib
import tempfile

import conftest  # noqa: F401

from core import blocked_imports
from test_app_push import _db

DID = "SABnzbd_nzo_abc123"


class Arr:
    """A Sonarr: its queue, Manual Import's look inside a folder, and its commands."""
    configured = True
    name = "Sonarr"

    def __init__(self, folder, files):
        self.folder, self.files, self.commands, self.blocked = folder, files, [], True

    async def queue(self, **params):
        if not self.blocked:
            return []
        return [{"downloadId": DID, "trackedDownloadState": "importBlocked", "title": "East.Of.Eden.S01.2160p",
                 "outputPath": self.folder, "seriesId": 139, "series": {"title": "East of Eden (2026)", "year": 2026, "tmdbId": 555},
                 "episode": {"seasonNumber": 1, "episodeNumber": n},
                 "statusMessages": [{"messages": ["Found matching series via grab history, but release was matched to series by ID."]}]}
                for n in (1, 2)]

    async def get(self, path, **params):
        if path == "manualimport":
            return [{"path": f"{self.folder}/{name}", "relativePath": name, "size": size, "quality": {"quality": {"name": "WEBDL-2160p"}},
                     "series": {"id": 139}, "episodes": [{"id": 900 + n, "seasonNumber": 1, "episodeNumber": n}] if n else [],
                     "quality": {"quality": {"id": 18, "name": "WEBDL-2160p"}, "revision": {"version": 1}},
                     "rejections": []} for name, size, n in self.files]
        if path.startswith("command/"):
            return {"status": "completed", "message": "Manually imported 2 files"}
        if path == "qualitydefinition":
            return [{"quality": {"id": 3, "name": "WEBDL-1080p"}}, {"quality": {"id": 18, "name": "WEBDL-2160p"}}]
        if path == "language":
            return [{"id": 1, "name": "English"}, {"id": 2, "name": "French"}]
        return {}

    async def episodes(self, series_id):
        return [{"id": 900 + n, "seasonNumber": 1, "episodeNumber": n, "title": f"Chapter {n}"} for n in (1, 2, 3)] if series_id == 139 else []

    async def command(self, name, **body):
        self.commands.append((name, body))
        return {"id": 7}


class Off:
    configured = False


class Services:
    def __init__(self, arr):
        self.sonarr, self.radarr = arr, Off()

        class config:
            admin_channel_id = None
            bot_owner_id = admin_role_id = guild_id = None
        self.config = config


def _folder(*extra):
    d = tempfile.mkdtemp()
    for name in ("East.Of.Eden.S01E01.mkv", "East.Of.Eden.S01E02.mkv", *extra):
        pathlib.Path(d, name).write_bytes(b"x")
    return d


GOOD = [("East.Of.Eden.S01E01.mkv", 9 * 2**30, 1), ("East.Of.Eden.S01E02.mkv", 9 * 2**30, 2)]


def test_a_blocked_download_opens_a_ticket_once_and_closes_it_when_imported():
    from core import season_search
    from portal import help as helpdesk
    opened, told = [], []

    async def open_help(bot, key, *, seasons, reason, note, status_now, offer=None, admin_note=None):
        assert reason == "blocked" and "won't import it by itself" in note and "Look at the files" not in note
        told.append(admin_note)
        h = await helpdesk.create(request_key=str(key), slot=1, title="East of Eden", kind="tv", seasons=None,
                                  user={"user": {"name": "Sam"}}, reason=helpdesk.PLEXBIE_REASONS[reason],
                                  note=note, status_now=status_now)
        opened.append(h["id"])
        return h

    async def body():
        from database.kv_store import kv_get
        from database.request_store import REQUESTS_NAMESPACE
        from database.kv_store import kv_set
        await kv_set(REQUESTS_NAMESPACE, "1001", {"status": "approved", "media": {"id": 555, "media_type": "tv", "name": "East of Eden"}})
        arr = Arr(_folder(), GOOD)
        saved = season_search.open_help
        season_search.open_help = open_help
        try:
            await blocked_imports.check(None, Services(arr))
            await blocked_imports.check(None, Services(arr))          # still blocked: no second ticket
            h = await kv_get(helpdesk.NAMESPACE, opened[0])
            assert h["blocked"] == {"app": "sonarr", "downloadId": DID} and h["status"] == "open"
            arr.blocked = False                                        # imported (here or in Sonarr)
            await blocked_imports.check(None, Services(arr))
            return await kv_get(helpdesk.NAMESPACE, opened[0])
        finally:
            season_search.open_help = saved
    h = _db(body)
    assert len(opened) == 1 and h["status"] == "resolved"
    said = "release was matched to series by ID"
    assert said not in str(helpdesk.member_view(h)), "Sonarr's own words are for the admins"
    assert "Look at the files" not in str(helpdesk.member_view(h)), "what to check is for the admins"
    assert any(said in e["text"] and "Look at the files" in e["text"] and e["kind"] == "action"
               for e in helpdesk.thread_of(h))
    assert said in told[0] and "Look at the files" in told[0], "the admin channel still hears what Sonarr said"


def test_the_admins_are_told_what_the_member_isnt():
    """The ticket's first entry is the member's; the admin channel hears the admin note,
    with or without the website running."""
    from types import SimpleNamespace

    from core import season_search
    from portal import help as helpdesk
    sent, told = [], []

    class Channel:
        async def send(self, text):
            sent.append(text)

    class Actions:
        async def _tell_admins_about_help(self, h, note=None):
            told.append(note)

    config = SimpleNamespace(admin_channel_id=5)

    async def body():
        from database.kv_store import kv_set
        from database.request_store import REQUESTS_NAMESPACE
        for key in ("1001", "1002"):
            await kv_set(REQUESTS_NAMESPACE, key, {"status": "approved", "media": {"id": 555, "media_type": "tv", "name": "East of Eden"}})
        site = SimpleNamespace(portal_actions=Actions())
        no_site = SimpleNamespace(services=SimpleNamespace(config=config), get_channel=lambda cid: Channel() if cid == 5 else None)
        hs = []
        for bot, key in ((site, 1001), (no_site, 1002)):
            hs.append(await season_search.open_help(bot, key, seasons=None, reason="blocked", note="An admin will check it.",
                                                    status_now="Downloaded, import blocked", admin_note="Sonarr says: matched by ID"))
        return hs
    hs = _db(body)
    assert all("matched by ID" not in str(helpdesk.member_view(h)) for h in hs)
    assert told == ["Sonarr says: matched by ID"]
    assert len(sent) == 1 and "matched by ID" in sent[0] and "An admin will check it" not in sent[0]


def test_the_preview_shows_what_looks_off_and_import_goes_through_sonarr():
    async def body():
        arr = Arr(_folder(), GOOD)
        p = await blocked_imports.preview(Services(arr), "sonarr", DID)
        assert p["ok"] and [f["as"] for f in p["files"]] == [["S01E01"], ["S01E02"]] and not p["warnings"]
        message = await blocked_imports.do_import(Services(arr), "sonarr", DID)
        return message, arr.commands
    message, commands = asyncio.run(body())
    assert message == "Manually imported 2 files."
    name, cmd = commands[0]
    assert name == "ManualImport" and cmd["importMode"] == "auto"
    assert [(f["seriesId"], f["episodeIds"]) for f in cmd["files"]] == [(139, [901]), (139, [902])]


def test_a_program_in_the_download_is_never_imported():
    async def body():
        arr = Arr(_folder("Codec.Setup.exe"), GOOD)
        p = await blocked_imports.preview(Services(arr), "sonarr", DID)
        assert not p["ok"] and any("program" in w for w in p["warnings"])
        assert [o for o in p["others"] if o["danger"]][0]["name"] == "Codec.Setup.exe"
        try:
            await blocked_imports.do_import(Services(arr), "sonarr", DID)
        except ValueError as e:
            return str(e), arr.commands
    refusal, commands = asyncio.run(body())
    assert "Not importing" in refusal and not commands


def test_odd_files_are_pointed_out():
    async def body():
        arr = Arr(_folder(), [("East.Of.Eden.S01E01.sample.mkv", 30 * 2**20, 1), ("Extras.mkv", 2 * 2**30, 0)])
        return await blocked_imports.preview(Services(arr), "sonarr", DID)
    p = asyncio.run(body())
    sample, extras = p["files"]
    assert any("sample" in n for n in sample["notes"]) and any("small" in n for n in sample["notes"])
    assert any("can't tell which episode" in n for n in extras["notes"])
    assert p["ok"] and not extras["ready"], "an unplaced file is for the admin to place, not a dead end"
    assert any("S01E02" in w for w in p["warnings"]), "grabbed for E02, but nothing in it is E02"


def test_a_blocked_download_is_a_problem_not_adding_to_plex():
    from portal.progress import Progress
    live = Progress._downloading([{"downloadId": "d", "size": 100, "sizeleft": 0, "trackedDownloadState": "importBlocked",
                                   "statusMessages": [{"messages": ["Matched to series by ID."]}]}], {"d": {"_done": "Completed"}})
    assert live["stage"] == "importing" and live["adminProblem"] == "Import blocked: Matched to series by ID."
    assert "Matched" not in live["problem"]


def test_members_see_a_plain_sentence_not_what_sonarr_said():
    """Sonarr's and the download client's own words carry release names and server paths.
    The admins see them on All requests; the member who asked sees a plain sentence."""
    from core.config import Config
    from database.request_store import mark_resolved, save_request
    from helpers import FakeServices
    from portal.admin import Admin
    from portal.data import Data
    from portal.progress import Progress
    data = Data(FakeServices(Config()))
    said = {
        "Blocked": Progress._downloading([{"downloadId": "b", "size": 100, "sizeleft": 0, "trackedDownloadState": "importBlocked",
                                           "statusMessages": [{"messages": ["/data/usenet/complete/East.Of.Eden.S01-GRP: matched by ID"]}]}],
                                         {"b": {"_done": "Completed"}}),
        "Warned": Progress._downloading([{"downloadId": "w", "size": 100, "sizeleft": 50, "trackedDownloadStatus": "warning",
                                          "errorMessage": "Unpacking failed in /data/usenet/incomplete/Night.Train-GRP"}], {}),
    }

    async def video(media, seasons):
        return {**said[media["name"]], "plexKey": "11509"}
    data.progress.video = video

    async def body():
        for key, name in ((1, "Blocked"), (2, "Warned")):
            await save_request(key, user_id=7, media={"id": key, "media_type": "tv", "name": name}, seasons=[1])
            await mark_resolved(key, "approved", "Sam")
        return await data.my_requests(7), await Admin(data).all_requests()
    mine, listed = _db(body)
    for r in mine:
        progress = r["progress"]
        assert "/data" not in str(progress) and "GRP" not in str(progress), progress
        assert progress["problem"] and set(progress) <= {"percent", "eta", "detail", "problem", "partial", "seasons",
                                                         "releaseDate", "releaseKind"}
    admin = {r["title"]["title"]: r for r in listed["rows"]}
    assert "East.Of.Eden.S01-GRP" in admin["Blocked"]["progress"]["problem"]
    assert any("Night.Train-GRP" in s for s in admin["Warned"]["stuck"])


def _import(files, choices):
    async def body():
        arr = Arr(_folder(), files)
        try:
            return await blocked_imports.do_import(Services(arr), "sonarr", DID, choices), arr.commands
        except ValueError as e:
            return str(e), arr.commands
    return asyncio.run(body())


def test_an_admin_can_say_which_episode_each_file_is():
    """"Is this really episode 1?": the two files were swapped, and one is set to 1080p."""
    message, commands = _import(GOOD, [{"name": "East.Of.Eden.S01E01.mkv", "episodeIds": [902], "qualityId": 3},
                                       {"name": "East.Of.Eden.S01E02.mkv", "episodeIds": [901], "languageIds": [2]}])
    files = commands[0][1]["files"]
    assert [f["episodeIds"] for f in files] == [[902], [901]]
    assert files[0]["quality"]["quality"] == {"id": 3, "name": "WEBDL-1080p"} and files[1]["languages"] == [{"id": 2, "name": "French"}]


def test_a_file_sonarr_couldnt_place_is_imported_once_an_admin_picks_its_episode():
    files = GOOD + [("Extras.mkv", 2 * 2**30, 0)]
    refused, none = _import(files, None)
    assert "Pick which episode Extras.mkv is" in refused and not none
    as_seen = [{"name": "East.Of.Eden.S01E01.mkv"}, {"name": "East.Of.Eden.S01E02.mkv"}]
    message, commands = _import(files, as_seen + [{"name": "Extras.mkv", "episodeIds": [903]}])
    assert [f["episodeIds"] for f in commands[0][1]["files"]] == [[901], [902], [903]]
    message, commands = _import(files, as_seen + [{"name": "Extras.mkv", "skip": True}])
    assert len(commands[0][1]["files"]) == 2, "a skipped file stays where it is"


def test_choices_are_checked_against_sonarr():
    first, second = {"name": "East.Of.Eden.S01E01.mkv"}, {"name": "East.Of.Eden.S01E02.mkv"}
    two, none = _import(GOOD, [first, {**second, "episodeIds": [901]}])
    assert "both set as the same episode" in two and not none
    other, none = _import(GOOD, [{**first, "episodeIds": [12345]}, second])
    assert "aren't in that show" in other and not none
    stale, none = _import(GOOD, [first, second, {"name": "Something.Else.mkv", "skip": True}])
    assert "changed since you looked" in stale and not none
    quality, none = _import(GOOD, [{**first, "qualityId": 99}, second])
    assert "doesn't have that quality" in quality and not none


def test_a_file_that_turned_up_after_the_look_inside_isnt_imported_blind():
    """The admin looked at two episodes; a third finished unpacking before they pressed
    Import. It would have gone in with Sonarr's guesses, unseen."""
    seen = [{"name": "East.Of.Eden.S01E01.mkv"}, {"name": "East.Of.Eden.S01E02.mkv"}]
    later, none = _import(GOOD + [("East.Of.Eden.S01E03.mkv", 9 * 2**30, 3)], seen)
    assert "changed since you looked" in later and not none
    message, commands = _import(GOOD, seen)
    assert len(commands[0][1]["files"]) == 2


class Films(Arr):
    """A Radarr whose download holds one film in two files (CD1 and CD2)."""
    name = "Radarr"

    async def queue(self, **params):
        return [{"downloadId": DID, "trackedDownloadState": "importBlocked", "title": "Night.Train.2026.CD1.CD2",
                 "outputPath": self.folder, "movieId": 77, "movie": {"title": "Night Train", "year": 2026, "tmdbId": 777}}]

    async def get(self, path, **params):
        if path == "manualimport":
            return [{"path": f"{self.folder}/{name}", "relativePath": name, "size": size,
                     "movie": {"id": 77, "title": "Night Train", "year": 2026},
                     "quality": {"quality": {"id": 18, "name": "WEBDL-2160p"}, "revision": {"version": 1}},
                     "rejections": []} for name, size, _n in self.files]
        return await super().get(path, **params)

    async def movies(self):
        return [{"id": 77, "title": "Night Train"}, {"id": 78, "title": "Night Train (1959)"}]


def test_two_files_set_as_the_same_film_are_refused():
    """Radarr keeps one file per film: importing CD1 and CD2 as one would keep only one of them."""
    parts = [("Night.Train.CD1.mkv", 3 * 2**30, 0), ("Night.Train.CD2.mkv", 3 * 2**30, 0)]

    def run(choices):
        async def body():
            arr = Films(_folder(), parts)
            services = Services(Off())
            services.radarr = arr
            try:
                return await blocked_imports.do_import(services, "radarr", DID, choices), arr.commands
            except ValueError as e:
                return str(e), arr.commands
        return asyncio.run(body())
    cd1, cd2 = {"name": "Night.Train.CD1.mkv"}, {"name": "Night.Train.CD2.mkv"}
    two, none = run([cd1, cd2])
    assert "both set as the same film" in two and not none
    picked, none = run([cd1, {**cd2, "movieId": 77}])
    assert "both set as the same film" in picked and not none
    message, commands = run([cd1, {**cd2, "skip": True}])
    assert [f["movieId"] for f in commands[0][1]["files"]] == [77]
    message, commands = run([cd1, {**cd2, "movieId": 78}])
    assert [f["movieId"] for f in commands[0][1]["files"]] == [77, 78]


def test_the_preview_carries_sonarrs_reasons_and_the_choices():
    async def body():
        arr = Arr(_folder(), GOOD)
        arr_get = arr.get

        async def get(path, **params):
            out = await arr_get(path, **params)
            if path == "manualimport":
                out[0]["rejections"] = [{"reason": "Episode has a TBA title and recently aired"}]
            return out
        arr.get = get
        return blocked_imports.public(await blocked_imports.preview(Services(arr), "sonarr", DID))
    p = asyncio.run(body())
    first = p["files"][0]
    assert first["rejections"] == ["Episode has a TBA title and recently aired"] and first["ready"]
    assert [e["label"] for e in p["options"]["episodes"]] == ["S01E01", "S01E02", "S01E03"]
    assert [q["name"] for q in p["options"]["qualities"]] == ["WEBDL-1080p", "WEBDL-2160p"] and p["series"]["id"] == 139
    assert first["qualityId"] == 18 and "_raw" not in first


def test_manage_says_which_app_it_couldnt_reach():
    """Sonarr down looked the same as nothing blocked: an empty list. Manage → Health
    now hears which app it couldn't ask, so it can say so instead."""
    from portal.actions import Actions

    class Down(Arr):
        async def queue(self, **params):
            raise ConnectionError("http://sonarr:8989/api/v3/queue?apikey=secret refused")

    async def body():
        actions = Actions(bot=None, services=Services(Down(_folder(), GOOD)), data=None, public_url="")
        down = await actions.blocked_list({"user": {"id": "1"}})
        up = await Actions(bot=None, services=Services(Arr(_folder(), GOOD)), data=None, public_url="").blocked_list({"user": {"id": "1"}})
        return down, up
    down, up = _db(body)
    assert down["rows"] == [] and down["errors"] == {"sonarr": "Couldn't reach Sonarr just now."}
    assert "secret" not in str(down), "the error never carries the address or its key"
    assert len(up["rows"]) == 1 and up["errors"] == {}, "Radarr isn't set up, so it isn't an error"


def test_looking_again_never_uses_up_the_admin_actions():
    """Manage asks for this list again while Health is open. Those asks must never use
    up the hourly allowance Approve, Decline and Import share."""
    from portal.actions import LIMITS, Actions

    async def body():
        actions = Actions(bot=None, services=Services(Arr(_folder(), GOOD)), data=None, public_url="")
        for _ in range(LIMITS["admin"][0] + 1):
            await actions.blocked_list({"user": {"id": "1"}})
        actions.limit("1", "admin")  # an admin action still goes through
        return actions
    actions = _db(body)
    assert len(actions._hits["admin:1"]) == 1
