# path: webhooks/sonarr_handler.py
"""Sonarr webhook: notes when episodes someone requested start downloading (the
rest is logged; Plex's own webhook announces arrivals). Shared plumbing:
arr_handler."""
from typing import Any, Dict

from core.logging import get_logger
from core.media_tracking import EpisodeProgress, get_media_tracker
from webhooks.arr_handler import register_arr_webhook

logger = get_logger(__name__)


async def _handle_grab(payload: Dict[str, Any]) -> None:
    """A download started. Sonarr's payload has no TMDB id to match on, so a
    requested season is found by its title and season number."""
    series = payload.get("series", {})
    episodes = payload.get("episodes", [])
    if not episodes:
        logger.warning("Grab event with no episodes")
        return
    title = series.get("title", "Unknown")
    season_number = episodes[0].get("seasonNumber", 0)
    logger.info(
        f"Sonarr Grab: {title} - {len(episodes)} episode(s)"
        f" (TVDB: {series.get('tvdbId')}, IMDB: {series.get('imdbId')})"
    )
    tracker = get_media_tracker()
    for tracked in tracker.tracked_media.values():
        if (tracked.media_type == "tv" and tracked.title.lower() == title.lower()
                and tracked.season_number == season_number):
            tracked.download_id = payload.get("downloadId", "")
            tracked.expected_episode_count = len(episodes)
            tracked.download_status = "downloading"
            if not tracked.episodes:
                tracked.episodes = [
                    EpisodeProgress(episode_number=ep.get("episodeNumber", 0), title=ep.get("title"))
                    for ep in episodes
                ]
            tracker.save_tracking_data()
            logger.info(f"Updated existing tracking for {title} S{season_number}")
            break
    logger.info(
        f"Grab event processed: {title} S{season_number} "
        f"({'season pack' if len(episodes) > 1 else 'single episode'})"
    )


async def _handle_download(payload: Dict[str, Any]) -> None:
    """Imported into the library; Plex's webhook does the announcing."""
    episodes = payload.get("episodes", [])
    title = payload.get("series", {}).get("title", "Unknown")
    logger.info(
        f"Sonarr Download: {title} - {len(episodes)} episode(s) imported"
        f"{' (upgrade)' if payload.get('isUpgrade') else ''}"
    )
    for ep in episodes:
        logger.info(
            f"  S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d} - {ep.get('title', 'Unknown')}"
        )


async def _handle_series_add(payload: Dict[str, Any]) -> None:
    logger.info(f"Sonarr Series Add: {payload.get('series', {}).get('title', 'Unknown')}")


def register_sonarr_webhook(webhook_server, bot) -> None:
    register_arr_webhook(webhook_server, "Sonarr", {
        "Grab": _handle_grab, "Download": _handle_download, "SeriesAdd": _handle_series_add,
    })
