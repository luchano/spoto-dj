"""
Last.fm API client for per-track genre tags.
Docs: https://www.last.fm/api/show/track.getTopTags

Crowd-sourced tags are specific to the recording, not the artist —
so a producer who releases both minimal techno and deep house gets
the right genre per track instead of a generic mix of their overall style.
"""
import asyncio
import logging
from typing import List, Optional

import httpx

log = logging.getLogger("spoto.lastfm")

_BASE = "https://ws.audioscrobbler.com/2.0/"

# Personal / meta tags that are not genre descriptors — filter them out.
_NON_GENRE = {
    "seen live", "favourite", "favorites", "love", "loved", "awesome",
    "great", "good", "amazing", "best", "perfect", "my music", "beautiful",
    "cool", "epic", "classic", "recommended", "spotify", "youtube",
    "soundcloud", "all", "under 2000 listeners", "check", "albums i own",
    "female vocalists", "male vocalists", "singer-songwriter",
}

# Minimum tag count (Last.fm normalises counts to 0-100).
# Tags below this threshold are typically applied by a single user.
_MIN_COUNT = 5


async def lookup_track_tags(
    api_key: str,
    title: str,
    artist: str,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
) -> Optional[List[str]]:
    """Fetch the top genre tags for a specific track from Last.fm.

    Returns:
        list[str]  up to 5 genre strings on success
        []         track not found, or no qualifying tags
        None       network error — caller should not cache this result
    """
    if not api_key or not title or not artist:
        return []

    params = {
        "method": "track.getTopTags",
        "api_key": api_key,
        "artist": artist,
        "track": title,
        "format": "json",
        "autocorrect": "1",
    }

    async with semaphore:
        try:
            resp = await client.get(_BASE, params=params, timeout=8.0)
        except Exception as exc:
            log.warning("Last.fm network error for '%s' by '%s': %s", title, artist, exc)
            return None

    if resp.status_code != 200:
        log.debug("Last.fm HTTP %d for '%s' by '%s'", resp.status_code, title, artist)
        return []

    data = resp.json()

    # {"error": 6, "message": "Track not found"}
    if "error" in data:
        log.debug("Last.fm %s ('%s' by '%s')", data.get("message", "error"), title, artist)
        return []

    tags = data.get("toptags", {}).get("tag", [])
    if not tags:
        return []

    genre_tags = [
        t["name"].lower()
        for t in tags
        if (
            isinstance(t.get("count"), int)
            and t["count"] >= _MIN_COUNT
            and t["name"].lower() not in _NON_GENRE
        )
    ]

    return genre_tags[:5]
