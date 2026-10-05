# path: webhooks/radarr_handler.py
"""Radarr webhook: notes when a requested film starts downloading (the rest is
logged; Plex's own webhook announces arrivals). Shared plumbing: arr_handler."""
from typing import Any, Dict

from core.logging import get_logger
from core.media_tracking import get_media_tracker
from webhooks.arr_handler import register_arr_webhook

logger = get_logger(__name__)


async def _handle_grab(payload: Dict[str, Any]) -> None:
    """A download started: a film someone requested is now on its way."""
    movie = payload.get("movie", {})
    title = movie.get("title", "Unknown")
    tmdb_id = movie.get("tmdbId")
    logger.info(f"Radarr Grab: {title} ({movie.get('year')}) - TMDB: {tmdb_id}")
    if tmdb_id:
        tracker = get_media_tracker()
        tracked = tracker.get_tracked_media(tmdb_id)
        if tracked:
            tracked.download_id = payload.get("downloadId", "")
            tracked.download_status = "downloading"
            tracker.save_tracking_data()
            logger.info(f"Updated tracking for requested movie: {title}")


async def _handle_download(payload: Dict[str, Any]) -> None:
    """Imported into the library; Plex's webhook does the announcing."""
    title = payload.get("movie", {}).get("title", "Unknown")
    logger.info(f"Radarr Download: {title} imported{' (upgrade)' if payload.get('isUpgrade') else ''}")


async def _handle_movie_add(payload: Dict[str, Any]) -> None:
    logger.info(f"Radarr Movie Add: {payload.get('movie', {}).get('title', 'Unknown')}")


def register_radarr_webhook(webhook_server, bot) -> None:
    register_arr_webhook(webhook_server, "Radarr", {
        "Grab": _handle_grab, "Download": _handle_download, "MovieAdded": _handle_movie_add,
    })
