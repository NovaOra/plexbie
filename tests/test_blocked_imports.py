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
    opened = []

    async def open_help(bot, key, *, seasons, reason, note, status_now, offer=None):
        assert reason == "blocked" and "won't import it by itself" in note and "Look at the files" in note
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
    assert live["stage"] == "importing" and live["problem"] == "Import blocked: Matched to series by ID."


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
    message, commands = _import(files, [{"name": "Extras.mkv", "episodeIds": [903]}])
    assert [f["episodeIds"] for f in commands[0][1]["files"]] == [[901], [902], [903]]
    message, commands = _import(files, [{"name": "Extras.mkv", "skip": True}])
    assert len(commands[0][1]["files"]) == 2, "a skipped file stays where it is"


def test_choices_are_checked_against_sonarr():
    two, none = _import(GOOD, [{"name": "East.Of.Eden.S01E02.mkv", "episodeIds": [901]}])
    assert "both set as the same episode" in two and not none
    other, none = _import(GOOD, [{"name": "East.Of.Eden.S01E01.mkv", "episodeIds": [12345]}])
    assert "aren't in that show" in other and not none
    stale, none = _import(GOOD, [{"name": "Something.Else.mkv", "skip": True}])
    assert "changed since you looked" in stale and not none
    quality, none = _import(GOOD, [{"name": "East.Of.Eden.S01E01.mkv", "qualityId": 99}])
    assert "doesn't have that quality" in quality and not none


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
