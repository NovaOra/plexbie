# path: core/clients.py
"""One small client per outside service, shared by every plugin and the website.

Each knows its own address and key (from config), applies a timeout, and turns
every failure - not set up, unreachable, timed out, an HTTP error, or the
service saying no - into one ServiceError with a readable message and, when
there was one, the HTTP status. Callers ask for what they want ("the series",
"run this search") instead of building URLs and headers each time.

ServiceError subclasses aiohttp.ClientError, so code that already catches
aiohttp's errors keeps catching these.
"""
import asyncio
import json
from typing import Any, Dict, Iterable, List, Optional

import aiohttp

from core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT = 15
#: Tautulli's users table is read this many at a time, up to this many pages.
USERS_TABLE_PAGE = 100
USERS_TABLE_PAGES = 50


class ServiceError(aiohttp.ClientError):
    """A service is not set up, could not be reached, or answered with an error."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


async def _reason(response) -> str:
    """The service's own explanation of an error, when it gives one."""
    try:
        text = (await response.text())[:300]
    except Exception:
        return ""
    try:
        body = json.loads(text)
        text = str(body.get("message") or body.get("error") or "") if isinstance(body, dict) else ""
    except ValueError:
        text = "" if "<html" in text.lower() else text
    text = " ".join(text.split())[:200]
    return f" ({text})" if text else ""


class _Client:
    name = "service"

    def __init__(self, services):
        self._services = services

    @property
    def config(self):
        return self._services.config

    @property
    def configured(self) -> bool:
        raise NotImplementedError

    async def _request(self, method: str, url: str, *, timeout: float = DEFAULT_TIMEOUT, text: bool = False,
                       raw: bool = False, **kw) -> Any:
        """The response's JSON (or text). raw=True: (status, text) for any status below 400."""
        if not self.configured:
            raise ServiceError(f"{self.name} isn't set up")
        http = getattr(self._services, "http_session", None)
        if http is None:
            raise ServiceError(f"{self.name}: not ready yet")
        try:
            async with http.request(method, url, timeout=aiohttp.ClientTimeout(total=timeout), **kw) as r:
                if r.status >= 400:
                    raise ServiceError(f"{self.name} answered HTTP {r.status}{await _reason(r)}", r.status)
                if raw:
                    return r.status, await r.text()
                if text:
                    return await r.text()
                if r.status == 204 or r.content_length == 0:
                    return None
                return await r.json(content_type=None)
        except ServiceError:
            raise
        except asyncio.TimeoutError:
            raise ServiceError(f"{self.name} timed out") from None
        except aiohttp.ClientError as e:
            raise ServiceError(f"{self.name} couldn't be reached: {e}") from e
        except ValueError as e:                     # not JSON
            raise ServiceError(f"{self.name} sent something unreadable: {e}") from e


class Tautulli(_Client):
    name = "Tautulli"

    @property
    def configured(self) -> bool:
        return bool(self.config.tautulli_url and self.config.tautulli_token)

    #: The address a POST has been seen to work at (see call).
    _post_url: Optional[str] = None

    async def call(self, cmd: str, *, timeout: float = DEFAULT_TIMEOUT, post: bool = False, **params) -> Any:
        """Run one API command and return its `data`.

        post=True sends the parameters as a form body, so a secret among them stays
        out of the query string that Tautulli and any proxy in front of it log. A
        harmless read is posted first to see that this Tautulli takes them; if it
        doesn't, they go in the query string as before."""
        url = f"{self.config.tautulli_url.rstrip('/')}/api/v2"
        fields = {"apikey": self.config.tautulli_token, "cmd": cmd, **params}
        if post and await self._takes_post(url, timeout):
            body = await self._request("POST", url, timeout=timeout, data={k: str(v) for k, v in fields.items()})
        else:
            body = await self._request("GET", url, timeout=timeout, params=fields)
        return self._data(body)

    @staticmethod
    def _data(body) -> Any:
        response = (body or {}).get("response") or {}
        if response.get("result") != "success":
            raise ServiceError(f"Tautulli said: {response.get('message') or 'unknown error'}")
        return response.get("data")

    async def _takes_post(self, url: str, timeout: float) -> bool:
        if self._post_url != url:
            try:
                self._data(await self._request("POST", url, timeout=timeout, data={
                    "apikey": self.config.tautulli_token, "cmd": "get_notifiers"}))
            except ServiceError:
                return False
            self._post_url = url
        return True

    async def ping(self, timeout: float = 6) -> None:
        await self.call("status", timeout=timeout)

    async def users(self) -> List[Dict[str, Any]]:
        """Every Plex user Tautulli knows, with emails (get_users)."""
        return await self.call("get_users") or []

    async def users_table(self, **params) -> List[Dict[str, Any]]:
        """Users with play counts and last-seen times (get_users_table). Every user,
        a page at a time, unless a length is given (Tautulli's own default is 25)."""
        if "length" in params:
            return ((await self.call("get_users_table", **params)) or {}).get("data") or []
        rows: List[Dict[str, Any]] = []
        for _ in range(USERS_TABLE_PAGES):
            page = (await self.call("get_users_table", **{**params, "start": len(rows),
                                                           "length": USERS_TABLE_PAGE})) or {}
            batch = page.get("data") or []
            rows.extend(batch)
            total = page.get("recordsFiltered", page.get("recordsTotal"))
            if len(batch) < USERS_TABLE_PAGE or (total is not None and len(rows) >= int(total)):
                break
        else:
            logger.warning("Tautulli: read the first %d users and stopped; it reports %s",
                           len(rows), total if total is not None else "more")
        return rows


class Arr(_Client):
    """Sonarr or Radarr (API v3)."""

    def __init__(self, services, kind: str):
        super().__init__(services)
        self.kind = kind                      # "sonarr" or "radarr"
        self.name = kind.capitalize()

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    @property
    def url(self) -> str:
        return (getattr(self.config, f"{self.kind}_url", "") or "").rstrip("/")

    @property
    def token(self) -> str:
        return getattr(self.config, f"{self.kind}_token", "") or ""

    async def _api(self, method: str, path: str, **kw) -> Any:
        return await self._request(method, f"{self.url}/api/v3/{path}", headers={"X-Api-Key": self.token}, **kw)

    async def get(self, path: str, **params) -> Any:
        return await self._api("GET", path, params=params or None)

    async def put(self, path: str, body: Any) -> Any:
        return await self._api("PUT", path, json=body)

    async def post(self, path: str, body: Any) -> Any:
        return await self._api("POST", path, json=body)

    async def delete(self, path: str, **params) -> Any:
        return await self._api("DELETE", path, params=params or None)

    async def command(self, name: str, **body) -> Any:
        """Start a command: SeriesSearch, SeasonSearch, MoviesSearch, ..."""
        return await self.post("command", {"name": name, **body})

    async def status(self) -> Dict[str, Any]:
        return await self.get("system/status")

    async def ping(self, timeout: float = 6) -> None:
        await self._api("GET", "system/status", timeout=timeout)

    async def queue(self, **params) -> List[Dict[str, Any]]:
        return ((await self.get("queue", pageSize=500, **params)) or {}).get("records") or []

    async def releases(self, timeout: float = 180, **params) -> List[Dict[str, Any]]:
        """Search the indexers now (movieId=, or seriesId= and seasonNumber=) and return
        every release with Sonarr/Radarr's verdict, its favourite first. Grabs nothing."""
        return await self._api("GET", "release", params=params, timeout=timeout) or []

    async def push(self, release: Dict[str, Any]) -> Dict[str, Any]:
        """Offer one release found elsewhere; Sonarr/Radarr grab it if they'd take it.
        Returns their verdict (approved, rejections)."""
        out = await self._api("POST", "release/push", json=release, timeout=60)
        return (out[0] if out else {}) if isinstance(out, list) else (out or {})

    # ---- Sonarr
    async def series(self) -> List[Dict[str, Any]]:
        return await self.get("series") or []

    async def episodes(self, series_id: int) -> List[Dict[str, Any]]:
        return await self.get("episode", seriesId=series_id) or []

    async def set_series_monitored(self, series_id: int, monitored: bool) -> Any:
        return await self.put("series/editor", {"seriesIds": [series_id], "monitored": monitored})

    async def set_episodes_monitored(self, series_id: int, seasons: Optional[Iterable[int]], monitored: bool,
                                     episodes: Optional[List[Dict[str, Any]]] = None) -> Any:
        """Monitor (or not) every episode of the show, or only those in `seasons`."""
        episodes = episodes if episodes is not None else await self.episodes(series_id)
        wanted = None if seasons is None else {int(s) for s in seasons}
        ids = [e["id"] for e in episodes if wanted is None or int(e.get("seasonNumber", 0)) in wanted]
        if not ids:
            return []
        return await self.put("episode/monitor", {"episodeIds": ids, "monitored": monitored})

    # ---- Radarr
    async def movies(self) -> List[Dict[str, Any]]:
        return await self.get("movie") or []

    async def set_movie_monitored(self, movie_id: int, monitored: bool) -> Any:
        return await self.put("movie/editor", {"movieIds": [movie_id], "monitored": monitored})


class Hydra(_Client):
    """NZBHydra's newznab API: Plexbie's own plain-text ("by name") searches."""
    name = "NZBHydra"

    @property
    def configured(self) -> bool:
        return bool(self.config.nzbhydra_url and self.config.nzbhydra_api_key)

    async def search(self, query: str, categories: Iterable[str], *, limit: int = 100,
                     timeout: float = 90) -> List[Dict[str, Any]]:
        """Releases for a text search: title, link (a Hydra download link), size, pubDate."""
        body = await self._request("GET", f"{self.config.nzbhydra_url.rstrip('/')}/api", timeout=timeout, params={
            "t": "search", "q": query, "cat": ",".join(categories), "limit": str(limit), "o": "json",
            "apikey": self.config.nzbhydra_api_key})
        if isinstance(body, dict) and body.get("error"):
            raise ServiceError(f"NZBHydra said: {(body['error'].get('@attributes') or {}).get('description') or 'search refused'}")
        items = ((body or {}).get("channel") or {}).get("item") or []
        out = []
        for item in [items] if isinstance(items, dict) else items:
            enclosure = (item.get("enclosure") or {}).get("@attributes") or {}
            link = enclosure.get("url") or item.get("link")
            if item.get("title") and link:
                out.append({"title": item["title"], "link": link, "pubDate": item.get("pubDate"),
                            "size": int(enclosure.get("length") or 0)})
        return out


class Tmdb(_Client):
    name = "TMDB"

    @property
    def configured(self) -> bool:
        return bool(self.config.tmdb_api_key)

    async def get(self, path: str, **params) -> Any:
        return await self._request("GET", f"https://api.themoviedb.org/3/{path.lstrip('/')}",
                                   params={"api_key": self.config.tmdb_api_key, **params})


class Sabnzbd(_Client):
    name = "SABnzbd"

    @property
    def configured(self) -> bool:
        return bool(self.config.sabnzbd_url and self.config.sabnzbd_api_key)

    async def call(self, mode: str, *, timeout: float = DEFAULT_TIMEOUT, **params) -> Dict[str, Any]:
        data = await self._request("GET", f"{self.config.sabnzbd_url.rstrip('/')}/api", timeout=timeout,
                                   params={"mode": mode, "output": "json", "apikey": self.config.sabnzbd_api_key, **params})
        if isinstance(data, dict) and (data.get("error") or data.get("status") is False):
            raise ServiceError(f"SABnzbd said: {data.get('error') or 'request refused'}")
        return data or {}

    async def ping(self, timeout: float = 6) -> None:
        await self.call("version", timeout=timeout)


class Seerr(_Client):
    name = "Seerr"

    @property
    def configured(self) -> bool:
        return bool(self.config.seerr_url and self.config.seerr_token)

    async def _api(self, method: str, path: str, **kw) -> Any:
        return await self._request(method, f"{self.config.seerr_url.rstrip('/')}/api/v1/{path.lstrip('/')}",
                                   headers={"X-Api-Key": self.config.seerr_token}, **kw)

    async def get(self, path: str, **params) -> Any:
        return await self._api("GET", path, params=params or None)

    async def ping(self, timeout: float = 6) -> None:
        await self._api("GET", "status", timeout=timeout)

    async def delete(self, path: str) -> Any:
        return await self._api("DELETE", path)

    async def post(self, path: str, body: Any, raw: bool = False) -> Any:
        return await self._api("POST", path, json=body, raw=raw)


class OpenLibrary(_Client):
    """Book search and details. Free and keyless, so always available."""
    name = "Open Library"
    configured = True

    async def get(self, path: str, **params) -> Any:
        return await self._request("GET", f"https://openlibrary.org/{path.lstrip('/')}", params=params or None)
