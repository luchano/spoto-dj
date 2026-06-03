import asyncio
import httpx

SPOTIFY_API = "https://api.spotify.com/v1"

CAMELOT = {
    (0, 1): "8B",  (0, 0): "5A",
    (1, 1): "3B",  (1, 0): "12A",
    (2, 1): "10B", (2, 0): "7A",
    (3, 1): "5B",  (3, 0): "2A",
    (4, 1): "12B", (4, 0): "9A",
    (5, 1): "7B",  (5, 0): "4A",
    (6, 1): "2B",  (6, 0): "11A",
    (7, 1): "9B",  (7, 0): "6A",
    (8, 1): "4B",  (8, 0): "1A",
    (9, 1): "11B", (9, 0): "8A",
    (10, 1): "6B", (10, 0): "3A",
    (11, 1): "1B", (11, 0): "10A",
}

KEY_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def camelot(pitch: int, mode: int) -> str:
    return CAMELOT.get((pitch, mode), "?")


def key_name(pitch: int, mode: int) -> str:
    if pitch == -1:
        return "?"
    return f"{KEY_NAMES[pitch]} {'maj' if mode == 1 else 'min'}"


def ms_to_min(ms: int) -> str:
    total_sec = ms // 1000
    return f"{total_sec // 60}:{total_sec % 60:02d}"


async def _get(client: httpx.AsyncClient, url: str, headers: dict) -> dict:
    """GET with automatic retry on 429 rate-limit responses."""
    for attempt in range(4):
        resp = await client.get(url, headers=headers)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 2 ** attempt))
            await asyncio.sleep(retry_after)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()
    return {}


async def get_liked_tracks(access_token: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {access_token}"}
    tracks = []

    async with httpx.AsyncClient(timeout=30) as client:
        url = f"{SPOTIFY_API}/me/tracks?limit=50&offset=0"
        while url:
            data = await _get(client, url, headers)
            tracks.extend(data["items"])
            url = data.get("next")

    return tracks


async def get_audio_features_batch(access_token: str, track_ids: list[str]) -> dict:
    """Returns empty dict silently if Spotify has revoked access (403) for new apps."""
    headers = {"Authorization": f"Bearer {access_token}"}
    result = {}

    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(track_ids), 100):
            batch = track_ids[i:i + 100]
            url = f"{SPOTIFY_API}/audio-features?ids={','.join(batch)}"

            for attempt in range(4):
                resp = await client.get(url, headers=headers)
                if resp.status_code == 429:
                    await asyncio.sleep(int(resp.headers.get("Retry-After", 2 ** attempt)))
                    continue
                # 403 = audio features deprecated for this app — skip gracefully
                if resp.status_code == 403:
                    return {}
                resp.raise_for_status()
                for feat in resp.json().get("audio_features") or []:
                    if feat:
                        result[feat["id"]] = feat
                break

    return result


async def get_artist_genres_batch(access_token: str, artist_ids: list[str]) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}
    result = {}
    unique_ids = list(set(artist_ids))

    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(unique_ids), 50):
            batch = unique_ids[i:i + 50]
            url = f"{SPOTIFY_API}/artists?ids={','.join(batch)}"
            data = await _get(client, url, headers)
            for artist in data.get("artists") or []:
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
            "genres": genres[:3],
            "bpm": round(feat.get("tempo", 0)) if feat else 0,
            "key": key_name(pitch, mode) if feat else "?",
            "camelot": camelot(pitch, mode) if feat else "?",
            "energy": round(feat.get("energy", 0) * 100) if feat else 0,
            "danceability": round(feat.get("danceability", 0) * 100) if feat else 0,
            "valence": round(feat.get("valence", 0) * 100) if feat else 0,
            "loudness": round(feat.get("loudness", 0), 1) if feat else 0,
            "preview_url": track.get("preview_url") or "",
            "duration": ms_to_min(track.get("duration_ms", 0)),
            "duration_ms": track.get("duration_ms", 0),
            "popularity": track.get("popularity", 0),
            "time_signature": feat.get("time_signature", 4) if feat else 4,
            "acousticness": round(feat.get("acousticness", 0) * 100) if feat else 0,
            "instrumentalness": round(feat.get("instrumentalness", 0) * 100) if feat else 0,
            "added_at": item.get("added_at", "")[:10],
            "spotify_url": track.get("external_urls", {}).get("spotify", ""),
            "image_url": (
                track.get("album", {}).get("images", [{}])[0].get("url", "")
                if track.get("album", {}).get("images") else ""
            ),
        })

    return library
