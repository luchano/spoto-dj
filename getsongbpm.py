"""
GetSongBPM API client.
Docs: https://getsongbpm.com/api

Strategy: /search/?type=both&lookup=song:<title>+artist:<artist>
Falls back to type=song + artist name matching if needed.
"""
import asyncio
import logging
import re
import urllib.parse
from typing import Optional

import httpx

from analysis import CAMELOT, KEY_NAMES

log = logging.getLogger("spoto.getsongbpm")

_BASE = "https://api.getsong.co"

# Map note names (including enharmonics) to pitch class 0-11
_NOTE = {
    "C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3,
    "E": 4, "F": 5, "F#": 6, "Gb": 6, "G": 7, "G#": 8, "Ab": 8,
    "A": 9, "A#": 10, "Bb": 10, "B": 11,
}

# Suffixes to strip from titles before searching
_STRIP_RE = re.compile(
    r"\s*[\(\[]"
    r"(?:feat\.?|ft\.?|featuring|with)[^\)\]]*"
    r"[\)\]]"
    r"|\s*-\s*(?:radio edit|remaster(?:ed)?|remix|live|acoustic|version|edit)\s*$",
    re.IGNORECASE,
)


def _clean_title(title: str) -> str:
    return _STRIP_RE.sub("", title).strip()


def _parse_key(key_of: str):
    """Parse key_of ('Am', 'C#', 'Bb', …) → (pitch 0-11, mode 0=minor/1=major).
    Returns (-1, 1) on failure."""
    if not key_of:
        return -1, 1
    is_minor = key_of.endswith("m") and len(key_of) > 1
    note = key_of[:-1] if is_minor else key_of
    pitch = _NOTE.get(note, -1)
    mode = 0 if is_minor else 1
    return pitch, mode


def _artist_match(spotify_artists: list[str], getsong_artist: str) -> bool:
    """True if any Spotify artist overlaps with GetSongBPM artist name."""
    gsa = getsong_artist.lower()
    for a in spotify_artists:
        al = a.lower()
        if al in gsa or gsa in al:
            return True
    return False


def _parse_song(song: dict) -> Optional[dict]:
    """Extract {bpm, key, camelot, energy, danceability} from a song dict.
    Returns None if data is incomplete."""
    bpm = round(float(song.get("tempo") or 0))
    key_of = song.get("key_of") or ""
    pitch, mode = _parse_key(key_of)
    if pitch == -1 or bpm == 0:
        return None
    return {
        "bpm": bpm,
        "key": f"{KEY_NAMES[pitch]} {'maj' if mode == 1 else 'min'}",
        "camelot": CAMELOT.get((pitch, mode), "?"),
        "energy": int(song.get("danceability") or 0),
    }


async def _search(
    api_key: str,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    lookup: str,
    search_type: str = "both",
    limit: int = 5,
) -> list[dict]:
    """Call /search/ and return the list of results (empty on error)."""
    async with semaphore:
        for attempt in range(3):
            try:
                resp = await client.get(
                    f"{_BASE}/search/",
                    params={"api_key": api_key, "type": search_type, "lookup": lookup, "limit": limit},
                    timeout=15,
                )
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 60))
                    log.warning("Rate limited — waiting %ds", wait)
                    await asyncio.sleep(wait)
                    continue
                if resp.status_code != 200:
                    return []
                return resp.json().get("search") or []
            except Exception as exc:
                if attempt == 2:
                    log.debug("Search error: %s", exc)
                    return []
                await asyncio.sleep(1 << attempt)
    return []


async def lookup_track(
    api_key: str,
    track_id: str,
    title: str,
    artists: list[str],
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
) -> tuple[str, dict]:
    """Look up a track by title+artist. Returns (track_id, result_dict).

    Strategy:
    1. type=both  →  song:<clean_title> artist:<first_artist>  (most precise)
    2. type=song  →  <clean_title>  then filter by artist match
    3. type=song  →  <clean_title>  take first result (last resort)
    """
    clean = _clean_title(title)
    first_artist = artists[0] if artists else ""

    # --- Pass 1: targeted search ---
    lookup_both = f"song:{clean} artist:{first_artist}"
    results = await _search(api_key, client, semaphore, lookup_both, search_type="both", limit=3)
    if results:
        parsed = _parse_song(results[0])
        if parsed:
            log.debug("Found (both) %s - %s", title, first_artist)
            return track_id, parsed

    # --- Pass 2: title-only search, filter by artist ---
    results = await _search(api_key, client, semaphore, clean, search_type="song", limit=10)
    for song in results:
        gsa = (song.get("artist") or {}).get("name", "")
        if _artist_match(artists, gsa):
            parsed = _parse_song(song)
            if parsed:
                log.debug("Found (artist match) %s - %s", title, gsa)
                return track_id, parsed

    # --- Pass 3: take first result regardless of artist ---
    for song in results:
        parsed = _parse_song(song)
        if parsed:
            log.debug("Found (first result) %s", title)
            return track_id, parsed

    return track_id, {"error": f"not found: {title!r}"}
