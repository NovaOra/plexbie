# path: portal/progress.py
"""Where an approved request really is, from Radarr, Sonarr, SABnzbd and Plex.

The request store only knows "approved". These services know the rest, and
each step is reported by the one that does it:

  upcoming                  not out yet: a film before its streaming or disc date,
                            a season before its first episode (core/releases)
  searching / downloading   Sonarr or Radarr, and SABnzbd (live bytes)
  unpacking                 SABnzbd's post-processing
  importing                 "Adding to Plex": Sonarr or Radarr has the file,
                            Plex hasn't listed it yet
  available                 "On Plex": Plex itself lists it (episode counts too)

So "On Plex" is never shown for something that can't be played yet. If Plex
can't be asked, Sonarr's and Radarr's word is used, as before.
Every viewer shares the same short-lived snapshots through the portal cache, so
a busy page costs one call per service every few seconds, not one per visitor.
"""
import asyncio
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from core.logging import get_logger
from core.blocking import run_blocking
from core.releases import movie_upcoming, next_episode, show_upcoming
from database.kv_store import kv_get
from portal.cache import TTLCache

logger = get_logger(__name__)

LIST_TTL = 60      # the full movie / series lists change rarely
QUEUE_TTL = 4      # download queues change by the second
PLEX_TTL = 60          # Plex's list of what it has, while something is waiting to appear
PLEX_IDLE_TTL = 600    # ...and when nothing is (a big library takes a few seconds to list)
#: Titles waiting for Plex (see plugins/new_media_added: the bot times them and tells the admins).
WAIT_NAMESPACE = "plex_wait"
NUDGE_SECONDS = 60     # at most this often, ask the bot to announce what Plex just listed

#: SABnzbd history statuses between "downloaded" and "Completed", in plain words.
POST_PROCESSING = {
    "Queued": "Waiting to unpack",
    "QuickCheck": "Checking the files",
    "Verifying": "Checking the files",
    "Repairing": "Repairing the files",
    "Fetching": "Fetching blocks to repair it",
    "Extracting": "Unpacking",
    "Moving": "Moving the files",
    "Running": "Finishing up",
}


def unpacking(slot: dict) -> Optional[dict]:
    """A SABnzbd history slot that is still being post-processed, as an "unpacking" stage.

    SABnzbd says how far it has got in action_line ("Unpacking: 03/12"); when
    it does, that becomes the bar, otherwise the bar runs without a number.
    """
    status = slot.get("status") or slot.get("_done")
    words = POST_PROCESSING.get(status)
    if not words:
        return None
    m = re.search(r"(\d+)\s*/\s*(\d+)", slot.get("action_line") or "")
    pct, detail = None, words
    if m and int(m.group(2)):
        done, total = int(m.group(1)), int(m.group(2))
        pct = max(0, min(100, int(100 * done / total)))
        detail = f"{words}, {done} of {total}"
    return {"stage": "unpacking", "percent": pct, "detail": f"{detail} in SABnzbd"}



#: Libraries on Plex's older agents keep one id in the item's guid instead.
_LEGACY_GUID = re.compile(r"agents\.(themoviedb|thetvdb|imdb)://([^?/]+)")
_LEGACY_NAME = {"themoviedb": "tmdb", "thetvdb": "tvdb", "imdb": "imdb"}


def file_key(path: Optional[str]) -> Optional[str]:
    """A file by its folder and name ("file://obsession (2026)/obsession (2026).mkv"), the
    same whichever machine's mount it's seen through (Radarr's /data/..., Plex's /movies/...)."""
    if not path:
        return None
    parts = [p for p in str(path).replace("\\", "/").split("/") if p]
    return "file://" + "/".join(parts[-2:]).casefold() if len(parts) >= 2 else None


def _plex_index(server, kind: str) -> Dict[str, str]:
    """{"tmdb://1399": ratingKey, "tvdb://121361": ratingKey, ...} for one kind ("movie"/"show").

    Films are also listed by their file (file_key): Plex sometimes matches a new film to
    other ids than Radarr's (another IMDb entry, no TMDB id yet), and the file Radarr
    imported is the same one Plex plays, whatever either thinks it's called."""
    index: Dict[str, str] = {}
    for section in server.library.sections():
        if section.type != kind:
            continue
        for item in section.all():
            key = str(item.ratingKey)
            for guid in getattr(item, "guids", None) or []:
                index[guid.id] = key
            m = _LEGACY_GUID.search(getattr(item, "guid", "") or "")
            if m:
                index[f"{_LEGACY_NAME[m.group(1)]}://{m.group(2)}"] = key
            if kind == "movie":
                for media in getattr(item, "media", None) or []:
                    for part in getattr(media, "parts", None) or []:
                        fk = file_key(getattr(part, "file", None))
                        if fk:
                            index.setdefault(fk, key)
    return index


def _plex_seasons(server, rating_key: str) -> Dict[int, int]:
    """{season number: episodes Plex has} for one show."""
    return {int(s.index): int(s.leafCount or 0) for s in server.fetchItem(int(rating_key)).seasons()}


class Progress:
    def __init__(self, services, cache: TTLCache):
        self.services = services
        self.cache = cache
        self.radarr, self.sonarr, self.sab = services.radarr, services.sonarr, services.sab
        #: Called when Plex lists something the site had shown as "Adding to Plex",
        #: so the Discord post and the requester's notice follow within a minute.
        self.on_plex: Optional[Callable[[], Awaitable[None]]] = None
        self._waiting: set = set()      # titles seen waiting, so their arrival nudges the bot
        # None until the first nudge: time.monotonic() counts from boot, so a
        # starting value of 0 would skip the first nudge on a just-booted machine.
        self._nudged: Optional[float] = None

    # ------------------------------------------------------------ snapshots
    async def _radarr_movies(self) -> Dict[int, dict]:
        if not self.radarr.configured:
            return {}

        async def load():
            movies = await self.radarr.movies()
            return {m["tmdbId"]: m for m in movies if m.get("tmdbId")}
        return await self.cache.get("radarr:movies", LIST_TTL, load)

    async def _radarr_queue(self) -> Dict[int, List[dict]]:
        if not self.radarr.configured:
            return {}

        async def load():
            out: Dict[int, List[dict]] = {}
            for rec in await self.radarr.queue(includeMovie="false"):
                if rec.get("movieId"):
                    out.setdefault(rec["movieId"], []).append(rec)
            return out
        return await self.cache.get("radarr:queue", QUEUE_TTL, load)

    async def _sonarr_series(self) -> Dict[int, dict]:
        if not self.sonarr.configured:
            return {}

        async def load():
            series = await self.sonarr.series()
            return {s["tmdbId"]: s for s in series if s.get("tmdbId")}
        return await self.cache.get("sonarr:series", LIST_TTL, load)

    async def _sonarr_queue(self) -> Dict[int, List[dict]]:
        if not self.sonarr.configured:
            return {}

        async def load():
            out: Dict[int, List[dict]] = {}
            for rec in await self.sonarr.queue(includeEpisode="true"):
                if rec.get("seriesId"):
                    out.setdefault(rec["seriesId"], []).append(rec)
            return out
        return await self.cache.get("sonarr:queue", QUEUE_TTL, load)

    async def _sab(self) -> Dict[str, List[dict]]:
        if not self.sab.configured:
            return {"queue": [], "history": []}

        async def load():
            q = await self.sab.call("queue")
            h = await self.sab.call("history", limit=60)
            return {"queue": q.get("queue", {}).get("slots", []), "history": h.get("history", {}).get("slots", [])}
        return await self.cache.get("sab", QUEUE_TTL, load)

    # -------------------------------------------------------------- helpers
    async def _sab_slots(self) -> Dict[str, dict]:
        """SABnzbd by nzo_id (the downloadId Sonarr and Radarr report): live queue
        slots, plus finished ones from history marked with "_done"."""
        try:
            sab = await self._sab()
        except Exception as e:
            logger.info(f"portal progress: SABnzbd unavailable ({type(e).__name__})")
            return {}
        out = {slot["nzo_id"]: {**slot, "_done": slot.get("status")} for slot in sab["history"] if slot.get("nzo_id")}
        out.update({slot["nzo_id"]: slot for slot in sab["queue"] if slot.get("nzo_id")})
        return out

    @staticmethod
    def _time_left(text: Optional[str]) -> Optional[str]:
        """SABnzbd's "0:04:12" (or "1:02:03:04" with days) as "about 4 min left"."""
        try:
            parts = [int(p) for p in str(text).split(":")]
        except (TypeError, ValueError):
            return None
        total = sum(v * m for v, m in zip(reversed(parts), (1, 60, 3600, 86400)))
        if total <= 0:
            return None
        if total < 90:
            return "under a minute left"
        if total < 3600:
            return f"about {round(total / 60)} min left"
        hours = round(total / 3600, 1)
        return f"about {hours:g} h left"

    @staticmethod
    def _downloading(records: List[dict], sab: Optional[Dict[str, dict]] = None) -> Optional[dict]:
        """Live progress for one title's queue records.

        Sonarr lists a season pack once per episode, each with the whole pack's
        size, so records are grouped by download first: summing them counted a
        13-episode pack 13 times. Each download's numbers then come from
        SABnzbd when it has them (it is live; Sonarr only asks it about once a
        minute), falling back to what Sonarr or Radarr last saw.
        """
        if not records:
            return None
        sab = sab or {}
        groups: Dict[str, List[dict]] = {}
        for r in records:
            groups.setdefault(str(r.get("downloadId") or r.get("id")), []).append(r)

        size = left = 0.0
        paused = queued = finished = failed = 0
        eta_text = None
        post = None
        for key, recs in groups.items():
            slot = sab.get(key)
            if slot and slot.get("_done") and unpacking(slot):
                post = post or unpacking(slot)
                size += max((r.get("size") or 0) for r in recs) / 1048576
            elif slot and slot.get("_done"):
                # Finished in SABnzbd; Sonarr/Radarr just haven't noticed yet.
                failed += slot["_done"] == "Failed"
                finished += slot["_done"] != "Failed"
                mb = max((r.get("size") or 0) for r in recs) / 1048576
                size += mb
            elif slot:
                mb, mbleft = float(slot.get("mb") or 0), float(slot.get("mbleft") or 0)
                size += mb
                left += mbleft
                status = (slot.get("status") or "").lower()
                paused += status == "paused"
                queued += status in ("queued", "grabbing", "fetching")
                if status == "downloading":
                    eta_text = eta_text or slot.get("timeleft")
            else:
                size += max((r.get("size") or 0) for r in recs) / 1048576
                left += min((r.get("sizeleft") or 0) for r in recs) / 1048576

        states = {r.get("trackedDownloadState") for r in records}
        problem = next((r.get("errorMessage") or r.get("trackedDownloadStatus") for r in records
                        if r.get("trackedDownloadStatus") in ("warning", "error") or r.get("status") == "failed"), None)
        eta = min((r.get("estimatedCompletionTime") for r in records if r.get("estimatedCompletionTime")), default=None)
        if failed:
            problem = "SABnzbd couldn't finish a download; Sonarr or Radarr will look for another copy"
        elif post and left <= 0:
            return post
        elif finished == len(groups) or (states & {"importPending", "importing", "imported"} and left <= 0):
            return {"stage": "importing", "percent": 100, "detail": "Downloaded, moving it onto Plex"}

        pct = max(0, min(100, int(100 * (1 - left / size)))) if size else 0
        episodes, downloads = len(records), len(groups)
        what = None
        if episodes > 1:
            what = (f"Season pack, {episodes} episodes" if downloads == 1
                    else f"{episodes} episodes in {downloads} downloads")
        if paused and paused == downloads:
            when = "paused in SABnzbd"
        elif queued and queued == downloads:
            when = "waiting its turn in the download queue"
        else:
            when = Progress._time_left(eta_text)
        detail = ", ".join(x for x in (what, when) if x) or None
        return {
            "stage": "downloading", "percent": pct, "eta": eta,
            "detail": detail[0].upper() + detail[1:] if detail else None,
            "problem": problem if problem not in (None, "ok") else None,
        }

    # ----------------------------------------------------------------- plex
    def _plex_ttl(self) -> float:
        return PLEX_TTL if self._waiting else PLEX_IDLE_TTL

    async def _plex_key(self, kind: str, *ids) -> Optional[str]:
        """Plex's ratingKey for a title, by its TMDB/TVDB ids; None when Plex doesn't have it.
        Raises when Plex can't be asked (the caller falls back to Sonarr/Radarr)."""
        server = self.services.plex_server
        if server is None:
            raise RuntimeError("Plex is not connected")
        index = await self.cache.get(f"plex:index:{kind}", self._plex_ttl(), lambda: run_blocking(_plex_index, server, kind))
        return next((index[i] for i in ids if i in index), None)

    async def _plex_ids(self, kind: str, rating_key: str) -> List[str]:
        """The TMDB/IMDb ids Plex matched one item to (none when Plex didn't match it)."""
        server = self.services.plex_server
        if server is None:
            raise RuntimeError("Plex is not connected")
        index = await self.cache.get(f"plex:index:{kind}", self._plex_ttl(), lambda: run_blocking(_plex_index, server, kind))
        return sorted(k for k, v in index.items() if v == rating_key and k.startswith(("tmdb://", "imdb://")))

    async def _plex_seasons(self, rating_key: str) -> Dict[int, int]:
        server = self.services.plex_server
        if server is None:
            raise RuntimeError("Plex is not connected")
        return await self.cache.get(f"plex:seasons:{rating_key}", self._plex_ttl(),
                                    lambda: run_blocking(_plex_seasons, server, rating_key))

    def _arrived(self, key: str, on_plex: bool) -> None:
        """Track titles seen waiting for Plex; when one shows up, nudge the bot."""
        if not on_plex:
            self._waiting.add(key)
            return
        if key not in self._waiting:
            return
        self._waiting.discard(key)
        if self.on_plex and (self._nudged is None or time.monotonic() - self._nudged > NUDGE_SECONDS):
            self._nudged = time.monotonic()
            task = asyncio.create_task(self.on_plex())
            task.add_done_callback(lambda t: t.cancelled() or t.exception() and
                                   logger.info(f"Announcing what Plex added failed: {t.exception()}"))

    @staticmethod
    def wait_key(media: dict, seasons: Any) -> str:
        """One name per request target, shared with the bot's waiting-for-Plex check."""
        if media.get("media_type") == "movie":
            return f"movie:{media.get('id')}"
        return f"tv:{media.get('id')}:{','.join(map(str, sorted(seasons))) if isinstance(seasons, list) else seasons}"

    async def _waiting_stage(self, key: str) -> dict:
        stage = {"stage": "importing", "percent": 100, "detail": "Plex usually picks it up within a few minutes",
                 "waitingForPlex": True}
        try:
            wait = await kv_get(WAIT_NAMESPACE, key)
        except Exception:
            wait = None
        if isinstance(wait, dict) and wait.get("alerted"):
            stage["problem"] = "Plex still hasn't listed it after a couple of hours. The admins have been told."
        return stage

    # --------------------------------------------------------------- public
    async def video(self, media: dict, seasons: Any) -> dict:
        """Live stage of an approved film or show."""
        tmdb = media.get("id")
        try:
            if media.get("media_type") == "movie":
                movie = (await self._radarr_movies()).get(tmdb)
                if movie is None:
                    return {"stage": "approved", "detail": "Approved and passed to Seerr"}
                if movie.get("hasFile"):
                    wanted = {f"tmdb://{tmdb}", f"imdb://{movie.get('imdbId')}"}
                    try:
                        by_id = await self._plex_key("movie", *wanted)
                        by_file = None if by_id else await self._plex_key(
                            "movie", file_key((movie.get("movieFile") or {}).get("path")))
                        other = await self._plex_ids("movie", by_file) if by_file else []
                    except Exception as e:
                        logger.info(f"Plex not asked about {movie.get('title')}: {e}")
                        return {"stage": "available"}
                    on_plex = (by_id or by_file) is not None
                    key = self.wait_key(media, seasons)
                    self._arrived(key, on_plex)
                    if not on_plex:
                        return await self._waiting_stage(key)
                    # Plex has the file, but says it's another film (a release of a namesake,
                    # grabbed for this one): on Plex, and flagged for the admins.
                    if not other:
                        return {"stage": "available"}
                    return {"stage": "available", "plexIds": other, "wantedIds": sorted(wanted), "plexKey": by_file,
                            "problem": "Plex lists it under another film's name and poster for now. The admins have been told."}
                live = self._downloading((await self._radarr_queue()).get(movie["id"], []), await self._sab_slots())
                return live or movie_upcoming(movie) or {"stage": "searching", "detail": "Looking for a copy"}

            series = (await self._sonarr_series()).get(tmdb)
            if series is None:
                return await self._not_in_sonarr(tmdb, seasons)
            wanted = None
            if isinstance(seasons, list) and seasons:
                wanted = {int(s) for s in seasons}
            asked = [s for s in series.get("seasons", [])
                     if s.get("seasonNumber", 0) and (wanted is None or s["seasonNumber"] in wanted)]
            more = next_episode(asked)            # a season still airing: when the next one is
            rows = []
            for s in series.get("seasons", []):
                n = s.get("seasonNumber", 0)
                if n == 0 or (wanted is not None and n not in wanted):
                    continue
                st = s.get("statistics") or {}
                total = st.get("episodeCount") or 0          # aired and monitored
                have = st.get("episodeFileCount") or 0
                if total or have:
                    rows.append({"n": n, "have": min(have, total) if total else have, "total": total})
            # What Sonarr has on disk vs what Plex lists: rows count what Plex has
            # (what can actually be played); the difference is "Adding to Plex".
            on_disk = sum(r["have"] for r in rows)
            plex_known = False
            if on_disk:
                try:
                    rk = await self._plex_key("show", f"tmdb://{tmdb}", f"tvdb://{series.get('tvdbId')}")
                    counts = await self._plex_seasons(rk) if rk else {}
                    for r in rows:
                        r["have"] = min(r["have"], counts.get(r["n"], 0))
                    plex_known = True
                except Exception as e:
                    logger.info(f"Plex not asked about {series.get('title')}: {e}")
            waiting = plex_known and sum(r["have"] for r in rows) < on_disk
            if on_disk:
                self._arrived(self.wait_key(media, seasons), not waiting)
            done = rows and all(r["total"] and r["have"] >= r["total"] for r in rows)
            if done:
                return {"stage": "available", "seasons": rows, **({"detail": more.capitalize()} if more else {})}
            # Only this request's seasons: a show with three requests (season 1, 2 and the
            # latest) showed the latest season's download on all three cards.
            queue = (await self._sonarr_queue()).get(series["id"], [])
            if wanted is not None:
                queue = [r for r in queue
                         if (r.get("seasonNumber") if r.get("seasonNumber") is not None
                             else (r.get("episode") or {}).get("seasonNumber")) in wanted]
            live = self._downloading(queue, await self._sab_slots())
            if live and not (waiting and live.get("stage") == "importing"):
                return {**live, "seasons": rows}
            if waiting:
                return {**await self._waiting_stage(self.wait_key(media, seasons)), "seasons": rows}
            if any(r["have"] for r in rows):
                have = sum(r["have"] for r in rows)
                total = sum(r["total"] for r in rows)
                return {"stage": "available", "partial": True, "seasons": rows,
                        "detail": f"{have} of {total} episodes on Plex so far" + (f", {more}" if more else "")}
            return {**(show_upcoming(asked) or {"stage": "searching", "detail": "Looking for episodes"}), "seasons": rows}
        except Exception as e:
            logger.warning(f"portal progress for {media.get('title') or media.get('name')}: {e}")
            return {"stage": "approved", "detail": "Approved. Live progress isn't available right now"}

    async def _not_in_sonarr(self, tmdb: Any, seasons: Any) -> dict:
        """A show Sonarr doesn't have. Usually it's on its way through Seerr; but one added
        by hand (a show Sonarr can't take) is on Plex, and then it's simply there. The
        "notInSonarr" mark lets Data say what went wrong when it isn't (Data.request_row)."""
        try:
            rk = await self._plex_key("show", f"tmdb://{tmdb}")
            counts = await self._plex_seasons(rk) if rk else {}
        except Exception as e:
            logger.info(f"Plex not asked about TMDB {tmdb}: {e}")
            counts = {}
        wanted = [int(s) for s in seasons] if isinstance(seasons, list) and seasons else sorted(n for n in counts if n)
        rows = [{"n": n, "have": counts.get(n, 0), "total": counts.get(n, 0)} for n in wanted]
        if rows and all(r["have"] for r in rows):
            return {"stage": "available", "seasons": rows}
        return {"stage": "approved", "detail": "Approved and passed to Seerr", "notInSonarr": True}

    async def book(self, book: dict, shelf_titles: List[str]) -> dict:
        """Live stage of an approved book: SABnzbd while downloading, then the shelf."""
        title = (book.get("title") or "").casefold()
        if title and any(title in t or t in title for t in shelf_titles):
            return {"stage": "available"}
        try:
            sab = await self._sab()
            words = [w for w in title.replace(":", " ").split() if len(w) > 2][:4]

            def matches(name: str) -> bool:
                n = (name or "").casefold()
                return bool(words) and all(w in n for w in words)

            for slot in sab["queue"]:
                if matches(slot.get("filename")):
                    return {"stage": "downloading", "percent": int(float(slot.get("percentage") or 0)),
                            "detail": f"{slot.get('timeleft')} left" if slot.get("timeleft") else None}
            for slot in sab["history"]:
                if matches(slot.get("name")):
                    if slot.get("status") == "Failed":
                        return {"stage": "approved", "problem": "The download failed. Ask an admin in Discord."}
                    if slot.get("status") == "Completed":
                        return {"stage": "importing", "percent": 100, "detail": "Downloaded, being filed into the library"}
                    return unpacking(slot) or {"stage": "downloading", "percent": 100, "detail": (slot.get("status") or "").capitalize()}
        except Exception as e:
            logger.warning(f"portal progress for book {book.get('title')}: {e}")
        return {"stage": "searching", "detail": "Looking for a copy"}
