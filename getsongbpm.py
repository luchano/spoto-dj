"""
GetSongBPM API client.
Docs: https://getsongbpm.com/api
Lookup a Spotify track ID → returns BPM, musical key, and Camelot notation.
"""
import asyncio
import logging

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


def _parse_key(key_of: str):
    """Parse GetSongBPM key_of ('Am', 'C#', 'Bb', …) into (pitch, mode).
    Returns (-1, 1) on failure.
    """
    if not key_of:
        return -1, 1
    is_minor = key_of.endswith("m") and len(key_of) > 1
    note = key_of[:-1] if is_minor else key_of
    pitch = _NOTE.get(note, -1)
    mode = 0 if is_minor else 1
    return pitch, mode


async def lookup_by_spotify_id(
    api_key: str,
    track_id: str,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
) -> dict:
    """Return {bpm, key, camelot, energy} or {"error": reason}."""
    async with semaphore:
        url = f"{_BASE}/song/"
        params = {"api_key": api_key, "type": "spotify", "lookup": track_id}

        for attempt in range(3):
            try:
                resp = await client.get(url, params=params, timeout=15)

                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 60))
                    log.warning("Rate limited — waiting %ds", wait)
                    await asyncio.sleep(wait)
                    continue

                if resp.status_code == 402:
                    return {"error": "quota exceeded"}

                if resp.status_code != 200:
                    return {"error": f"HTTP {resp.status_code}"}

                data = resp.json()
                song = data.get("song")
                if not song:
                    return {"error": "not found"}

                bpm = round(float(song.get("tempo") or 0))
                key_of = song.get("key_of") or ""
                pitch, mode = _parse_key(key_of)

                if pitch == -1 or bpm == 0:
                    return {"error": f"incomplete data (bpm={bpm}, key={key_of!r})"}

                return {
                    "bpm": bpm,
                    "key": f"{KEY_NAMES[pitch]} {'maj' if mode == 1 else 'min'}",
                    "camelot": CAMELOT.get((pitch, mode), "?"),
                    "energy": 0,  # not available from GetSongBPM
                }

            except Exception as exc:
                if attempt == 2:
                    return {"error": str(exc)}
                await asyncio.sleep(1 << attempt)

        return {"error": "max retries exceeded"}


