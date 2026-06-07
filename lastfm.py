"""
Last.fm API client for genre tags.
Docs: https://www.last.fm/api/show/track.getTopTags

Track-level tags are the most specific (a producer who releases both minimal
techno and deep house gets the right genre per track), but Last.fm's per-track
tag data is sparse — many popular tracks return an empty tag list from the API.
So we fall back through progressively broader scopes until we find tags:

    track  →  cleaned-title track  →  album  →  artist

Artist-level tags are cached in-process: a library has far fewer distinct
artists than tracks, so this collapses most lookups to a single request.
"""
import asyncio
import logging
import re
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
    "female vocalists", "male vocalists", "singer-songwriter", "favorite songs",
}

# Geographic / nationality tags Last.fm crowds love but that aren't genres.
# Not exhaustive (city-level tags still leak) but catches the common ones.
_LOCATIONS = {
    "argentina", "argentinian", "argentine", "swiss", "switzerland",
    "american", "america", "usa", "us", "uk", "british", "england", "english",
    "spanish", "spain", "mexican", "mexico", "colombia", "colombian", "chile",
    "chilean", "french", "france", "german", "germany", "italian", "italy",
    "japanese", "japan", "korean", "korea", "brazilian", "brazil", "brasil",
    "canadian", "canada", "australian", "australia", "irish", "ireland",
    "scottish", "scotland", "dutch", "netherlands", "swedish", "sweden",
    "norwegian", "norway", "finnish", "finland", "danish", "denmark",
    "portuguese", "portugal", "polish", "poland", "russian", "russia",
    "icelandic", "iceland", "uruguay", "uruguayan", "peru", "peruvian",
    "venezuela", "venezuelan", "cuban", "cuba", "puerto rico",
}

_YEAR_RE = re.compile(r"^(19|20)\d{2}s?$")

# Minimum tag count (Last.fm normalises counts to 0-100).
# Tags below this threshold are typically applied by a single user.
_MIN_COUNT = 5

# artist (lowercased) -> filtered genre list. Populated lazily during a run.
_artist_tag_cache: dict = {}


def _clean_title(title: str) -> str:
    """Strip parentheticals and dash-suffixes (feat./live/remaster/remix etc.)."""
    t = re.sub(r"\s*\(.*?\)\s*", " ", title)   # "(feat. Romy)", "(Live)"
    t = re.sub(r"\s*-\s.*$", "", t)            # " - En Vivo", " - Radio Edit"
    return t.strip()


def _filter_tags(raw: list) -> List[str]:
    """Keep only genre-like tags above the count threshold, max 5."""
    out: List[str] = []
    for t in raw:
        name = (t.get("name") or "").strip().lower()
        count = t.get("count")
        if not name:
            continue
        if not isinstance(count, int) or count < _MIN_COUNT:
            continue
        if name in _NON_GENRE or name in _LOCATIONS:
            continue
        if _YEAR_RE.match(name) or "best of" in name or name.startswith("top "):
            continue
        if name not in out:
            out.append(name)
    return out[:5]


async def _get_tags(
    api_key: str,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    method: str,
    **params,
) -> Optional[list]:
    """Call a *.getTopTags method.

    Returns the raw tag list, or None on a network/HTTP error (caller should
    treat None as "retry later", not "no tags").
    """
    query = {
        "method": method,
        "api_key": api_key,
        "format": "json",
        "autocorrect": "1",
        **params,
    }
    async with semaphore:
        try:
            resp = await client.get(_BASE, params=query, timeout=8.0)
        except Exception as exc:
            log.warning("Last.fm network error (%s %s): %s", method, params, exc)
            return None

    if resp.status_code != 200:
        log.debug("Last.fm HTTP %d (%s %s)", resp.status_code, method, params)
        return None

    data = resp.json()
    if "error" in data:
        # e.g. {"error": 6, "message": "Track not found"} — a real "no data"
        # answer, not a transient failure.
        return []

    raw = data.get("toptags", {}).get("tag", [])
    if isinstance(raw, dict):   # Last.fm returns a bare dict when there's one tag
        raw = [raw]
    return raw


async def _artist_tags(
    api_key: str,
    artist: str,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
) -> Optional[List[str]]:
    """Filtered artist-level genre tags, memoised per run. None on network error."""
    key = artist.lower()
    if key in _artist_tag_cache:
        return _artist_tag_cache[key]
    raw = await _get_tags(api_key, client, semaphore, "artist.getTopTags", artist=artist)
    if raw is None:
        return None
    genres = _filter_tags(raw)
    _artist_tag_cache[key] = genres
    return genres


async def lookup_track_tags(
    api_key: str,
    title: str,
    artist: str,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    album: str = "",
) -> Optional[List[str]]:
    """Fetch genre tags for a track, falling back to album then artist scope.

    Returns:
        list[str]  up to 5 genre strings (track-, album-, or artist-level)
        []         nothing found at any scope
        None       a network error occurred — caller should not cache the result
    """
    if not api_key or not title or not artist:
        return []

    network_err = False

    async def _attempt(method: str, **params) -> Optional[List[str]]:
        """Returns genres if found, [] if scope had none, None on network error."""
        raw = await _get_tags(api_key, client, semaphore, method, **params)
        if raw is None:
            return None
        return _filter_tags(raw)

    # 1. Track-level — most specific.
    genres = await _attempt("track.getTopTags", artist=artist, track=title)
    if genres is None:
        network_err = True
    elif genres:
        return genres

    # 2. Cleaned title (drop "(feat. …)" / "- Live" suffixes) if it differs.
    cleaned = _clean_title(title)
    if cleaned and cleaned != title:
        genres = await _attempt("track.getTopTags", artist=artist, track=cleaned)
        if genres is None:
            network_err = True
        elif genres:
            return genres

    # 3. Album-level.
    if album:
        genres = await _attempt("album.getTopTags", artist=artist, album=album)
        if genres is None:
            network_err = True
        elif genres:
            return genres

    # 4. Artist-level — broadest, cached across the run.
    genres = await _artist_tags(api_key, artist, client, semaphore)
    if genres is None:
        network_err = True
    elif genres:
        return genres

    # Nothing anywhere. Return None on a transient failure so we retry later.
    return None if network_err else []
