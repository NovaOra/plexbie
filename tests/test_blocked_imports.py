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
                     "rejections": []} for name, size, n in self.files]
        if path.startswith("command/"):
            return {"status": "completed", "message": "Manually imported 2 files"}
        return {}

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
    assert not p["ok"], "a file it can't place blocks importing from Plexbie"
    assert any("S01E02" in w for w in p["warnings"]), "grabbed for E02, but nothing in it is E02"


def test_a_blocked_download_is_a_problem_not_adding_to_plex():
    from portal.progress import Progress
    live = Progress._downloading([{"downloadId": "d", "size": 100, "sizeleft": 0, "trackedDownloadState": "importBlocked",
                                   "statusMessages": [{"messages": ["Matched to series by ID."]}]}], {"d": {"_done": "Completed"}})
    assert live["stage"] == "importing" and live["problem"] == "Import blocked: Matched to series by ID."
