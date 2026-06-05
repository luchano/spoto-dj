import asyncio
import logging
import os
import secrets
import urllib.parse
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from analysis import load_cache, save_cache
from getsongbpm import lookup_track, QUOTA_EXCEEDED as _GETSONGBPM_QUOTA
from lastfm import lookup_track_tags as _lastfm_tags
from playlist_engine import (
    create_playlist, delete_playlist, generate as generate_playlist,
    load_playlists, save_playlists,
)
from spotify import build_track_library

log = logging.getLogger("spoto")
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")

load_dotenv()

CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "http://localhost:8000/callback")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
GETSONGBPM_API_KEY = os.getenv("GETSONGBPM_API_KEY", "")
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "")

SCOPES = "user-library-read playlist-modify-public playlist-modify-private"
SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"

app = FastAPI(title="Spoto DJ")

_sessions: dict[str, dict] = {}
_track_cache: dict[str, list[dict]] = {}
_pending_states: set[str] = set()
_analysis_state: dict = {"running": False, "total": 0, "done": 0, "results": {}}

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


def _set_session(response, session_id: str, data: dict):
    _sessions[session_id] = data
    response.set_cookie("sid", session_id, httponly=True, samesite="lax")


def _get_session(request: Request) -> dict:
    sid = request.cookies.get("sid")
    return _sessions.get(sid, {}) if sid else {}


def _primary_artist(track: dict) -> str:
    """Extract the first artist name from a track dict (handles str or list)."""
    raw = track.get("artists", "")
    if isinstance(raw, list):
        return raw[0].strip() if raw else ""
    return raw.split(",")[0].strip() if raw else ""


async def _run_analysis(tracks: list[dict], to_genre_backfill: list[dict], cache: dict):
    """
    tracks            — tracks NOT in cache; get BPM (GetSongBPM) + genres (Last.fm)
    to_genre_backfill — tracks already in cache but missing track_genres; Last.fm only
    cache             — full audio cache dict (mutated in place)
    """
    log.info(
        "Analysis started: %d new tracks (BPM + genre), %d cached tracks (genre backfill)",
        len(tracks), len(to_genre_backfill),
    )
    bpm_sem = asyncio.Semaphore(2)   # conservative for GetSongBPM free tier
    lfm_sem = asyncio.Semaphore(5)   # Last.fm allows 5 req/s on free tier

    async def _lookup(track: dict):
        """Full lookup: BPM via GetSongBPM + genres via Last.fm, concurrently."""
        title  = track.get("title", "")
        artists = track.get("artists", [])
        primary = _primary_artist(track)

        async with httpx.AsyncClient() as client:
            (track_id, bpm_result), lfm_tags = await asyncio.gather(
                lookup_track(GETSONGBPM_API_KEY, track["id"], title, artists, client, bpm_sem),
                _lastfm_tags(LASTFM_API_KEY, title, primary, client, lfm_sem),
            )

        # lfm_tags is None on network error (don't cache), [] if not found (also skip)
        if lfm_tags:
            bpm_result["track_genres"] = lfm_tags
        return track_id, bpm_result

    async def _genre_only(track: dict):
        """Last.fm-only lookup for tracks already in cache but missing track_genres."""
        tid    = track["id"]
        title  = track.get("title", "")
        primary = _primary_artist(track)

        async with httpx.AsyncClient() as client:
            lfm_tags = await _lastfm_tags(LASTFM_API_KEY, title, primary, client, lfm_sem)

        if lfm_tags:
            cache[tid]["track_genres"] = lfm_tags
            # Propagate into live results so the frontend picks them up
            if tid in _analysis_state["results"]:
                _analysis_state["results"][tid]["track_genres"] = lfm_tags

    tasks          = [asyncio.create_task(_lookup(t)) for t in tracks]
    backfill_tasks = [asyncio.create_task(_genre_only(t)) for t in to_genre_backfill]

    # Permanent errors worth caching so we don't waste API calls on retries.
    # Transient errors (quota, network, HTTP 5xx) are NOT cached → retried next session.
    _PERMANENT = ("not found", "incomplete data")

    errors = 0
    for coro in asyncio.as_completed(tasks):
        track_id, result = await coro
        if result.get("error") == "quota exceeded":
            log.error(
                "API quota exceeded after %d/%d tracks — stopping. "
                "Remaining tracks will be retried next session.",
                _analysis_state["done"], len(tracks),
            )
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            break
        elif "error" in result:
            errors += 1
            msg = result["error"]
            log.warning("Lookup failed %s: %s", track_id, msg)
            if any(msg.startswith(p) for p in _PERMANENT):
                cache[track_id] = result   # cache so we skip next time
        else:
            cache[track_id] = result
            _analysis_state["results"][track_id] = result
        _analysis_state["done"] += 1
        if _analysis_state["done"] % 50 == 0:
            save_cache(cache)
            log.info("Progress: %d/%d (errors: %d)", _analysis_state["done"], len(tracks), errors)

    # BPM analysis done — mark complete so the frontend stops polling
    save_cache(cache)
    _analysis_state["running"] = False
    log.info("BPM done: %d/%d found, %d errors", len(tracks) - errors, len(tracks), errors)

    # Genre backfill continues silently after BPM is marked done
    if backfill_tasks:
        await asyncio.gather(*backfill_tasks, return_exceptions=True)
        save_cache(cache)
        log.info("Genre backfill complete: %d cached tracks updated", len(to_genre_backfill))


@app.get("/", response_class=HTMLResponse)
async def index():
    return (static_dir / "index.html").read_text()


@app.get("/login")
async def login():
    state = secrets.token_urlsafe(16)
    _pending_states.add(state)
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
    }
    url = f"{SPOTIFY_AUTH_URL}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(url)


@app.get("/callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        raise HTTPException(400, f"Spotify auth error: {error}")

    if state not in _pending_states:
        raise HTTPException(400, "State mismatch — possible CSRF")
    _pending_states.discard(state)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            SPOTIFY_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
            auth=(CLIENT_ID, CLIENT_SECRET),
        )
        resp.raise_for_status()
        token_data = resp.json()

        # Fetch Spotify user ID for playlist export (optional — don't crash auth if it fails)
        try:
            me_resp = await client.get(
                "https://api.spotify.com/v1/me",
                headers={"Authorization": f"Bearer {token_data['access_token']}"},
            )
            if me_resp.status_code == 200:
                token_data["spotify_user_id"] = me_resp.json().get("id", "")
        except Exception as e:
            log.warning("Could not fetch Spotify user ID (export will require re-login): %s", e)

    session_id = secrets.token_urlsafe(32)
    response = RedirectResponse("/")
    _set_session(response, session_id, token_data)
    return response


@app.get("/api/me")
async def me(request: Request):
    session = _get_session(request)
    if not session.get("access_token"):
        return JSONResponse({"authenticated": False})
    return JSONResponse({
        "authenticated": True,
        "can_export": bool(session.get("spotify_user_id")),
    })


@app.get("/api/tracks")
async def tracks(request: Request):
    session = _get_session(request)
    access_token = session.get("access_token")
    if not access_token:
        raise HTTPException(401, "Not authenticated")

    if access_token in _track_cache:
        return JSONResponse({"tracks": _track_cache[access_token], "cached": True})

    library = await build_track_library(access_token)
    _track_cache[access_token] = library
    return JSONResponse({"tracks": library, "cached": False})


@app.post("/api/analyze")
async def start_analyze(request: Request, background_tasks: BackgroundTasks):
    session = _get_session(request)
    access_token = session.get("access_token")
    if not access_token:
        raise HTTPException(401, "Not authenticated")

    if _analysis_state["running"]:
        return JSONResponse({"status": "already_running"})

    library = _track_cache.get(access_token, [])
    if not library:
        raise HTTPException(400, "Load tracks first via /api/tracks")

    if not GETSONGBPM_API_KEY:
        raise HTTPException(503, "GETSONGBPM_API_KEY not configured")

    cache = load_cache()
    _analysis_state["results"] = {k: v for k, v in cache.items() if "error" not in v}
    to_analyze = [t for t in library if t["id"] not in cache]

    # Tracks in cache but missing Last.fm genre tags → backfill silently alongside BPM analysis
    to_genre_backfill: list[dict] = []
    if LASTFM_API_KEY:
        to_genre_backfill = [
            t for t in library
            if t["id"] in cache
            and "error" not in cache[t["id"]]
            and "track_genres" not in cache[t["id"]]
        ]

    cached_ok = sum(1 for v in cache.values() if "error" not in v)
    cached_err = len(cache) - cached_ok
    log.info(
        "Analyze request: %d total, %d cached ok, %d cached not-found, "
        "%d to look up, %d genre backfill",
        len(library), cached_ok, cached_err, len(to_analyze), len(to_genre_backfill),
    )

    _analysis_state.update({
        "running": bool(to_analyze),
        "total": len(to_analyze),
        "done": 0,
    })

    if to_analyze or to_genre_backfill:
        background_tasks.add_task(_run_analysis, to_analyze, to_genre_backfill, cache)

    return JSONResponse({
        "status": "started",
        "total": len(to_analyze),
        "cached": len(cache),
    })


@app.get("/api/analyze/status")
async def analyze_status():
    return JSONResponse({
        "running": _analysis_state["running"],
        "done": _analysis_state["done"],
        "total": _analysis_state["total"],
        "results": _analysis_state["results"],
    })


@app.get("/api/playlists")
async def list_playlists(request: Request):
    session = _get_session(request)
    if not session.get("access_token"):
        raise HTTPException(401, "Not authenticated")
    playlists = load_playlists()
    summaries = [
        {
            "id":               p["id"],
            "name":             p["name"],
            "created_at":       p["created_at"],
            "track_count":      p["track_count"],
            "total_duration_ms":p["total_duration_ms"],
            "warnings":             p.get("warnings", []),
            "sections":             p.get("sections", []),
            "spotify_playlist_url": p.get("spotify_playlist_url"),
        }
        for p in playlists.values()
    ]
    summaries.sort(key=lambda x: x["created_at"], reverse=True)
    return JSONResponse(summaries)


@app.post("/api/playlists/generate")
async def api_generate_playlist(request: Request):
    session = _get_session(request)
    access_token = session.get("access_token")
    if not access_token:
        raise HTTPException(401, "Not authenticated")

    library = _track_cache.get(access_token, [])
    if not library:
        raise HTTPException(400, "Load tracks first via /api/tracks")

    body = await request.json()
    duration_min = int(body.get("duration_min", 60))
    genre_filter = body.get("genre_filter") or None
    bpm_range    = body.get("bpm_range")    # [min, max] or null
    name         = body.get("name") or None

    if not 40 <= duration_min <= 180:
        raise HTTPException(400, "duration_min must be between 40 and 180")
    if bpm_range:
        bpm_range = tuple(int(b) for b in bpm_range)
        if bpm_range[0] >= bpm_range[1]:
            raise HTTPException(400, "bpm_range min must be less than max")

    cache = load_cache()
    params = {
        "duration_min": duration_min,
        "genre_filter": genre_filter,
        "bpm_range":    list(bpm_range) if bpm_range else None,
    }

    result = generate_playlist(
        library=library,
        cache=cache,
        duration_min=duration_min,
        genre_filter=genre_filter,
        bpm_range=bpm_range,
    )

    if "error" in result:
        raise HTTPException(400, result["error"])

    playlist = create_playlist(result, params, name=name)
    log.info("Playlist generated: %s (%d tracks)", playlist["name"], playlist["track_count"])
    return JSONResponse(playlist)


@app.get("/api/playlists/{pid}")
async def get_playlist(request: Request, pid: str):
    session = _get_session(request)
    if not session.get("access_token"):
        raise HTTPException(401, "Not authenticated")
    playlists = load_playlists()
    if pid not in playlists:
        raise HTTPException(404, "Playlist not found")
    return JSONResponse(playlists[pid])


@app.delete("/api/playlists/{pid}")
async def api_delete_playlist(request: Request, pid: str):
    session = _get_session(request)
    if not session.get("access_token"):
        raise HTTPException(401, "Not authenticated")
    if not delete_playlist(pid):
        raise HTTPException(404, "Playlist not found")
    return JSONResponse({"ok": True})


@app.post("/api/playlists/{pid}/export")
async def export_playlist(request: Request, pid: str):
    session = _get_session(request)
    access_token = session.get("access_token")
    user_id = session.get("spotify_user_id")
    if not access_token:
        raise HTTPException(401, "Not authenticated")
    if not user_id:
        raise HTTPException(403, "Re-login required for playlist export (missing Spotify user ID)")

    playlists = load_playlists()
    if pid not in playlists:
        raise HTTPException(404, "Playlist not found")
    playlist = playlists[pid]

    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        # 1. Create empty Spotify playlist
        create_resp = await client.post(
            f"https://api.spotify.com/v1/users/{user_id}/playlists",
            headers=headers,
            json={
                "name":        playlist["name"],
                "description": f"Generated by Spoto DJ — {playlist['track_count']} tracks",
                "public":      False,
            },
        )
        if create_resp.status_code not in (200, 201):
            raise HTTPException(502, f"Spotify create playlist failed: {create_resp.status_code}")
        spotify_pid = create_resp.json()["id"]
        spotify_url = create_resp.json()["external_urls"]["spotify"]

        # 2. Add tracks in batches of 100
        uris = [f"spotify:track:{t['spotify_id']}" for t in playlist["tracks"]]
        for i in range(0, len(uris), 100):
            batch = uris[i:i + 100]
            add_resp = await client.post(
                f"https://api.spotify.com/v1/playlists/{spotify_pid}/tracks",
                headers=headers,
                json={"uris": batch},
            )
            if add_resp.status_code not in (200, 201):
                log.warning("Add tracks batch failed: %d", add_resp.status_code)

    # 3. Persist Spotify IDs
    playlist["spotify_playlist_id"]  = spotify_pid
    playlist["spotify_playlist_url"] = spotify_url
    playlists[pid] = playlist
    save_playlists(playlists)

    log.info("Exported playlist %s → %s", pid, spotify_url)
    return JSONResponse({"spotify_playlist_url": spotify_url, "spotify_playlist_id": spotify_pid})


@app.get("/logout")
async def logout(request: Request):
    sid = request.cookies.get("sid")
    if sid and sid in _sessions:
        token = _sessions[sid].get("access_token")
        if token and token in _track_cache:
            del _track_cache[token]
        del _sessions[sid]
    response = RedirectResponse("/")
    response.delete_cookie("sid")
    return response
