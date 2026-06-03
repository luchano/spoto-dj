import httpx
import asyncio
from typing import Optional

SPOTIFY_API = "https://api.spotify.com/v1"

# Camelot wheel: (pitch_class, mode) -> camelot notation
# mode: 1=major, 0=minor
CAMELOT = {
    (0, 1): "8B",  (0, 0): "5A",
    (1, 1): "3B",  (1, 0): "10A",
    (2, 1): "10B", (2, 0): "7A",
    (3, 1): "5B",  (3, 0): "2A",
    (4, 1): "12B", (4, 0): "9A",
    (5, 1): "7B",  (5, 0): "4A",
    (6, 1): "2B",  (6, 0): "11A",
    (7, 1): "9B",  (7, 0): "6A",
    (8, 1): "4B",  (8, 0): "1A",
    (9, 1): "11B", (9, 0): "8A",
    (10, 1): "6B", (10, 0): "3A",
    (11, 1): "1B", (11, 0): "10A",  # B major / Bb minor
}

KEY_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def camelot(pitch: int, mode: int) -> str:
    return CAMELOT.get((pitch, mode), "?")


def key_name(pitch: int, mode: int) -> str:
    if pitch == -1:
        return "?"
    name = KEY_NAMES[pitch]
    return f"{name} {'maj' if mode == 1 else 'min'}"


def ms_to_min(ms: int) -> str:
    total_sec = ms // 1000
    return f"{total_sec // 60}:{total_sec % 60:02d}"


async def get_liked_tracks(access_token: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {access_token}"}
    tracks = []

    async with httpx.AsyncClient(timeout=30) as client:
        url = f"{SPOTIFY_API}/me/tracks?limit=50&offset=0"
        while url:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            tracks.extend(data["items"])
            url = data.get("next")

    return tracks


async def get_audio_features_batch(access_token: str, track_ids: list[str]) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}
    result = {}

    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(track_ids), 100):
            batch = track_ids[i:i + 100]
            ids_param = ",".join(batch)
            resp = await client.get(
                f"{SPOTIFY_API}/audio-features?ids={ids_param}",
                headers=headers,
            )
            resp.raise_for_status()
            for feat in resp.json().get("audio_features") or []:
                if feat:
                    result[feat["id"]] = feat

    return result


async def get_artist_genres_batch(access_token: str, artist_ids: list[str]) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}
    result = {}
    unique_ids = list(set(artist_ids))

    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(unique_ids), 50):
            batch = unique_ids[i:i + 50]
            ids_param = ",".join(batch)
            resp = await client.get(
                f"{SPOTIFY_API}/artists?ids={ids_param}",
                headers=headers,
            )
            resp.raise_for_status()
            for artist in resp.json().get("artists") or []:
                if artist:
                    result[artist["id"]] = artist.get("genres", [])

    return result


async def build_track_library(access_token: str) -> list[dict]:
    raw_tracks = await get_liked_tracks(access_token)

    track_ids = [item["track"]["id"] for item in raw_tracks if item.get("track")]
    primary_artist_ids = [
        item["track"]["artists"][0]["id"]
        for item in raw_tracks
        if item.get("track") and item["track"].get("artists")
    ]

    audio_features, artist_genres = await asyncio.gather(
        get_audio_features_batch(access_token, track_ids),
        get_artist_genres_batch(access_token, primary_artist_ids),
    )

    library = []
    for item in raw_tracks:
        track = item.get("track")
        if not track or track.get("is_local"):
            continue

        tid = track["id"]
        feat = audio_features.get(tid, {})
        artists = track.get("artists", [])
        primary_artist_id = artists[0]["id"] if artists else None
        genres = artist_genres.get(primary_artist_id, []) if primary_artist_id else []

        pitch = feat.get("key", -1)
        mode = feat.get("mode", 1)
        release_date = track.get("album", {}).get("release_date", "")
        year = release_date[:4] if release_date else "?"

        library.append({
            "id": tid,
            "title": track.get("name", ""),
            "artists": ", ".join(a["name"] for a in artists),
            "album": track.get("album", {}).get("name", ""),
            "year": year,
            "genres": genres[:3],  # top 3 genres
            "bpm": round(feat.get("tempo", 0)),
            "key": key_name(pitch, mode),
            "camelot": camelot(pitch, mode),
            "energy": round(feat.get("energy", 0) * 100),
            "danceability": round(feat.get("danceability", 0) * 100),
            "valence": round(feat.get("valence", 0) * 100),
            "loudness": round(feat.get("loudness", 0), 1),
            "duration": ms_to_min(track.get("duration_ms", 0)),
            "duration_ms": track.get("duration_ms", 0),
            "popularity": track.get("popularity", 0),
            "time_signature": feat.get("time_signature", 4),
            "acousticness": round(feat.get("acousticness", 0) * 100),
            "instrumentalness": round(feat.get("instrumentalness", 0) * 100),
            "added_at": item.get("added_at", "")[:10],
            "spotify_url": track.get("external_urls", {}).get("spotify", ""),
            "image_url": (
                track.get("album", {}).get("images", [{}])[0].get("url", "")
                if track.get("album", {}).get("images") else ""
            ),
        })

    return library
