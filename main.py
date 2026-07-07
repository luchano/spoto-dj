import asyncio
import logging
import os
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from analysis import load_cache, save_cache, analyze_track as _yt_analyze
from essentia_analysis import analyze_track_full as _local_analyze, rate_limiter_info
from getsongbpm import lookup_track, QUOTA_EXCEEDED as _GETSONGBPM_QUOTA
from lastfm import lookup_track_tags as _lastfm_tags
from playlist_engine import (
    create_playlist, delete_playlist, generate as generate_playlist,
    genre_options, load_playlists, plan_library_tour, save_playlists,
)
from set_namer import generate_set_name
from spotify import build_track_library

log = logging.getLogger("spoto")

# ── logging: console + rotating file ─────────────────────────────────────────
_LOG_FMT = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
_LOG_DATE = "%Y-%m-%d %H:%M:%S"

from logging.handlers import RotatingFileHandler as _RFH

_root = logging.getLogger()
_root.setLevel(logging.INFO)

_console = logging.StreamHandler()
_console.setFormatter(logging.Formatter(_LOG_FMT, datefmt=_LOG_DATE))
_root.addHandler(_console)

_file_handler = _RFH(
    Path(__file__).parent / "server.log",
    maxBytes=10 * 1024 * 1024,  # 10 MB per file
    backupCount=3,
    encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter(_LOG_FMT, datefmt=_LOG_DATE))
_root.addHandler(_file_handler)

load_dotenv()

CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "http://localhost:8000/callback")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
GETSONGBPM_API_KEY = os.getenv("GETSONGBPM_API_KEY", "")
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "")

# Local audio pipeline (zotify + essentia).
# Set USE_LOCAL_ANALYSIS=true to use this instead of GetSongBPM + Last.fm.
# zotify setup (one-time) is documented in essentia_analysis.py.
USE_LOCAL_ANALYSIS = os.getenv("USE_LOCAL_ANALYSIS", "false").lower() == "true"

SCOPES = "user-library-read playlist-modify-public playlist-modify-private"
SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"

app = FastAPI(title="Spoto DJ")

_sessions: dict[str, dict] = {}
_track_cache: dict[str, list[dict]] = {}
_pending_states: set[str] = set()
_analysis_state: dict = {
    "running": False,     # BPM lookups in progress
    "backfilling": False, # Last.fm genre backfill in progress (silent, after BPM done)
    "total": 0,
    "done": 0,
    "results": {},
    "current": None,      # {title, artists, stage: downloading|analyzing, started: epoch}
    "last_done": None,    # {title, bpm, finished: epoch}
}

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Error prefixes that are permanent (not transient) — safe to cache so we don't
# waste API calls retrying them. Defined at module level so _lookup() can use it.
# "essentia analysis failed" is deterministic (corrupt/edge-case audio fails the
# same way every run) — without caching it, the track re-downloads/re-analyzes
# on every page refresh forever.
_PERMANENT_ERRORS = ("not found", "incomplete data", "essentia analysis failed")


def _set_session(response, session_id: str, data: dict):
    _sessions[session_id] = data
    response.set_cookie("sid", session_id, httponly=True, samesite="lax")


def _get_session(request: Request) -> dict:
    sid = request.cookies.get("sid")
    return _sessions.get(sid, {}) if sid else {}


async def _refresh_access_token(session: dict, client: httpx.AsyncClient) -> Optional[str]:
    """Exchange the stored refresh token for a fresh access token.

    Spotify access tokens expire after 1 hour; the refresh token (saved at
    login) is long-lived. Updates the session dict in place — since
    _get_session returns the live _sessions entry, the new token persists.
    Returns the new access token, or None if refresh isn't possible.
    """
    refresh = session.get("refresh_token")
    if not refresh:
        return None
    try:
        resp = await client.post(SPOTIFY_TOKEN_URL, data={
            "grant_type":    "refresh_token",
            "refresh_token": refresh,
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        })
    except Exception as e:
        log.warning("Token refresh request failed: %s", e)
        return None
    if resp.status_code != 200:
        log.warning("Token refresh failed: %d %s", resp.status_code, resp.text[:200])
        return None
    data = resp.json()
    session["access_token"] = data["access_token"]
    # Spotify only sometimes rotates the refresh token — keep the old one if not.
    if data.get("refresh_token"):
        session["refresh_token"] = data["refresh_token"]
    if data.get("expires_in"):
        session["expires_at"] = time.time() + data["expires_in"]
    log.info("Refreshed Spotify access token")
    return data["access_token"]


async def _spotify_request(client, session, method, url, **kwargs):
    """Make a Spotify API call, transparently refreshing the token once on 401."""
    headers = dict(kwargs.pop("headers", {}))
    headers["Authorization"] = f"Bearer {session.get('access_token')}"
    resp = await client.request(method, url, headers=headers, **kwargs)
    if resp.status_code == 401:
        new_token = await _refresh_access_token(session, client)
        if new_token:
            headers["Authorization"] = f"Bearer {new_token}"
            resp = await client.request(method, url, headers=headers, **kwargs)
    return resp


def _primary_artist(track: dict) -> str:
    """Extract the first artist name from a track dict (handles str or list)."""
    raw = track.get("artists", "")
    if isinstance(raw, list):
        return raw[0].strip() if raw else ""
    return raw.split(",")[0].strip() if raw else ""


async def _run_analysis(tracks: list[dict], to_genre_backfill: list[dict], cache: dict):
    """
    tracks            — tracks NOT in cache; get BPM + genres
    to_genre_backfill — tracks already in cache but missing track_genres; genre only
    cache             — full audio cache dict (mutated in place)

    When USE_LOCAL_ANALYSIS=true: downloads audio via zotify and analyzes with essentia.
    Otherwise: uses GetSongBPM API + Last.fm (original flow).
    """
    log.info(
        "Analysis started [%s]: %d new tracks, %d genre backfill",
        "local/essentia" if USE_LOCAL_ANALYSIS else "GetSongBPM+LastFm",
        len(tracks), len(to_genre_backfill),
    )
    bpm_sem = asyncio.Semaphore(2)   # conservative for GetSongBPM free tier
    lfm_sem = asyncio.Semaphore(5)   # Last.fm allows 5 req/s on free tier
    yt_sem  = asyncio.Semaphore(3)   # YouTube downloads: 3 concurrent max
    # Local pipeline: downloads MUST be strictly sequential (one zotify
    # subprocess at a time) — the recommended anti-rate-limit pattern.
    local_sem = asyncio.Semaphore(1)

    async def _local_lookup(track: dict):
        """Download + analyze locally with zotify + essentia."""
        def _on_stage(stage: str):
            # Fires when this track actually acquires the download slot
            # (sequential), so "current" reflects the live pipeline state.
            _analysis_state["current"] = {
                "title": track.get("title", ""),
                "artists": track.get("artists", ""),
                "stage": stage,
                "started": time.time(),
            }
        return await _local_analyze(
            track_id=track["id"],
            spotify_url=track.get("spotify_url", ""),
            title=track.get("title", ""),
            artists=track.get("artists", ""),
            semaphore=local_sem,
            on_stage=_on_stage,
            duration_ms=track.get("duration_ms", 0),
        )

    async def _lookup(track: dict):
        """Full lookup: BPM via GetSongBPM (+ YouTube fallback) + genres via Last.fm."""
        title   = track.get("title", "")
        artists = track.get("artists", [])
        primary = _primary_artist(track)
        album   = track.get("album", "")

        async with httpx.AsyncClient() as client:
            (track_id, bpm_result), lfm_tags = await asyncio.gather(
                lookup_track(GETSONGBPM_API_KEY, track["id"], title, artists, client, bpm_sem),
                _lastfm_tags(LASTFM_API_KEY, title, primary, client, lfm_sem, album),
            )

        # If GetSongBPM couldn't find the track, fall back to YouTube + librosa analysis
        if "error" in bpm_result and any(bpm_result["error"].startswith(p) for p in _PERMANENT_ERRORS):
            log.info("GetSongBPM miss for '%s' — trying YouTube/librosa fallback", title)
            _, yt_result = await _yt_analyze(track["id"], title, artists, "", yt_sem)
            if "error" not in yt_result:
                yt_result["source"] = "youtube"
                bpm_result = yt_result
                log.info("YouTube analysis OK for '%s': bpm=%s key=%s camelot=%s",
                         title, yt_result.get("bpm"), yt_result.get("key"), yt_result.get("camelot"))
            else:
                log.warning("YouTube fallback also failed for '%s': %s", title, yt_result.get("error"))

        # None = network error → don't cache (retry next session)
        # []   = not found   → cache as empty so we don't re-query next session
        if lfm_tags is not None:
            bpm_result["track_genres"] = lfm_tags
        return track_id, bpm_result

    async def _genre_only(track: dict):
        """Last.fm-only lookup for tracks already in cache but missing track_genres."""
        tid    = track["id"]
        title  = track.get("title", "")
        primary = _primary_artist(track)
        album   = track.get("album", "")

        async with httpx.AsyncClient() as client:
            lfm_tags = await _lastfm_tags(LASTFM_API_KEY, title, primary, client, lfm_sem, album)

        # None = network error → don't cache; [] = not found → cache to skip next session
        if lfm_tags is not None:
            cache[tid]["track_genres"] = lfm_tags
            # Propagate non-empty results into live state so the frontend picks them up
            if lfm_tags and tid in _analysis_state["results"]:
                _analysis_state["results"][tid]["track_genres"] = lfm_tags

    lookup_fn = _local_lookup if USE_LOCAL_ANALYSIS else _lookup
    titles = {t["id"]: t.get("title", "") for t in tracks}

    tasks          = [asyncio.create_task(lookup_fn(t)) for t in tracks]
    # Genre backfill: use local pipeline if enabled (re-classify existing audio files),
    # otherwise fall back to Last.fm
    backfill_tasks = [asyncio.create_task(_genre_only(t)) for t in to_genre_backfill]

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
            if any(msg.startswith(p) for p in _PERMANENT_ERRORS):
                cache[track_id] = result   # cache so we skip next time
        else:
            cache[track_id] = result
            _analysis_state["results"][track_id] = result
            _analysis_state["last_done"] = {
                "title": titles.get(track_id, track_id),
                "bpm": result.get("bpm"),
                "finished": time.time(),
            }
        _analysis_state["done"] += 1
        # Persist after every track: the local pipeline is slow and often
        # interrupted (server restart), so a coarse checkpoint would lose all
        # in-flight work. The cache write is atomic and cheap (ms) next to the
        # seconds-per-track download+analysis.
        save_cache(cache)
        if _analysis_state["done"] % 25 == 0:
            log.info("Progress: %d/%d (errors: %d)", _analysis_state["done"], len(tracks), errors)

    # BPM analysis done — mark complete so the frontend stops the progress bar
    save_cache(cache)
    _analysis_state["running"] = False
    _analysis_state["current"] = None
    log.info("BPM done: %d/%d found, %d errors", len(tracks) - errors, len(tracks), errors)

    # Genre backfill — already marked backfilling=True in start_analyze before response was sent
    if backfill_tasks:
        await asyncio.gather(*backfill_tasks, return_exceptions=True)
        save_cache(cache)
        _analysis_state["backfilling"] = False
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

    cached = access_token in _track_cache
    if cached:
        library = _track_cache[access_token]
    else:
        library = await build_track_library(access_token)
        _track_cache[access_token] = library

    # Merge per-track Last.fm genre tags from the audio cache as a SEPARATE field
    # (`track_genres`) so the frontend can show Spotify artist genres and Last.fm
    # track genres side by side. The Spotify `genres` field is left untouched.
    audio_cache = load_cache()
    if any(audio_cache.get(t["id"], {}).get("track_genres") for t in library):
        library = [
            {**t, "track_genres": audio_cache[t["id"]]["track_genres"]}
            if audio_cache.get(t["id"], {}).get("track_genres")
            else t
            for t in library
        ]

    return JSONResponse({"tracks": library, "cached": cached})


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

    if not USE_LOCAL_ANALYSIS and not GETSONGBPM_API_KEY:
        raise HTTPException(503, "GETSONGBPM_API_KEY not configured")

    cache = load_cache()
    _analysis_state["results"] = {k: v for k, v in cache.items() if "error" not in v}

    def _needs_analysis(t: dict) -> bool:
        entry = cache.get(t["id"])
        if entry is None:
            return True
        # Local mode: re-analyze entries cached before the loudness/danceability
        # calibration (they lack 'loudness' and carry the old saturated energy).
        # Re-analysis is cheap — the audio file is already downloaded.
        if USE_LOCAL_ANALYSIS and "error" not in entry and "loudness" not in entry:
            return True
        return False

    to_analyze = [t for t in library if _needs_analysis(t)]

    # Tracks in cache but with no Last.fm genre tags → backfill silently alongside
    # BPM analysis. We re-query empty results too (not just missing ones): the
    # fallback chain (track → album → artist) now resolves nearly everything, so
    # previously-empty entries are worth another look.
    to_genre_backfill: list[dict] = []
    if LASTFM_API_KEY:
        to_genre_backfill = [
            t for t in library
            if t["id"] in cache
            and "error" not in cache[t["id"]]
            and not cache[t["id"]].get("track_genres")
        ]

    cached_ok = sum(1 for v in cache.values() if "error" not in v)
    cached_err = len(cache) - cached_ok
    log.info(
        "Analyze request: %d total, %d cached ok, %d cached not-found, "
        "%d to look up, %d genre backfill",
        len(library), cached_ok, cached_err, len(to_analyze), len(to_genre_backfill),
    )

    # Set backfilling=True BEFORE returning the response so the first poll
    # doesn't see running=False/backfilling=False and close the poll prematurely.
    _analysis_state.update({
        "running":    bool(to_analyze),
        "backfilling": bool(to_genre_backfill),
        "total": len(to_analyze),
        "done": 0,
        "current": None,
        "last_done": None,
    })

    if to_analyze or to_genre_backfill:
        background_tasks.add_task(_run_analysis, to_analyze, to_genre_backfill, cache)

    return JSONResponse({
        "status": "started",
        "total": len(to_analyze),
        "cached": len(cache),
        "backfill_count": len(to_genre_backfill),
    })


@app.get("/api/analyze/status")
async def analyze_status():
    current = _analysis_state.get("current")
    if current:
        current = {**current, "elapsed": round(time.time() - current["started"])}
    return JSONResponse({
        "running":    _analysis_state["running"],
        "backfilling": _analysis_state["backfilling"],
        "done":       _analysis_state["done"],
        "total":      _analysis_state["total"],
        "results":    _analysis_state["results"],
        "current":    current,
        "last_done":  _analysis_state.get("last_done"),
        "rate":       rate_limiter_info() if USE_LOCAL_ANALYSIS else None,
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


@app.get("/api/playlists/genres")
async def api_genre_options(request: Request):
    """Genre dropdown options derived from the user's own analysed library."""
    session = _get_session(request)
    access_token = session.get("access_token")
    if not access_token:
        raise HTTPException(401, "Not authenticated")

    library = _track_cache.get(access_token, [])
    options = genre_options(library, load_cache()) if library else []
    return JSONResponse({"genres": options})


async def _ai_rename_playlist(pid: str, provisional: dict):
    """Background task: prepend the AI-generated creative name to a stored
    playlist's auto-name once GLM answers. No-op on any failure."""
    ai_name = await generate_set_name(provisional)
    if not ai_name:
        return
    playlists = load_playlists()
    pl = playlists.get(pid)
    if not pl:
        return   # deleted meanwhile
    pl["name"] = f"{ai_name} · {pl['name']}"
    playlists[pid] = pl
    save_playlists(playlists)
    log.info("Playlist %s renamed by AI: %r", pid, pl["name"])


@app.post("/api/playlists/generate")
async def api_generate_playlist(request: Request, background_tasks: BackgroundTasks):
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

    # Creative set name via z.ai (GLM + thinking) when the user didn't type
    # one. GLM-5's reasoning takes 45-90 s, so the set is returned immediately
    # with the standard auto-name and renamed IN THE BACKGROUND when the model
    # answers. Fail-safe: any error/timeout/missing key keeps the auto-name.
    if not name:
        background_tasks.add_task(
            _ai_rename_playlist,
            playlist["id"],
            {"tracks": result["tracks"],
             "total_duration_ms": result["total_duration_ms"],
             "params": params},
        )

    return JSONResponse(playlist)


@app.post("/api/playlists/tour")
async def api_library_tour(request: Request, background_tasks: BackgroundTasks):
    """Partition the WHOLE analyzed library into cohesive, disjoint DJ sets.

    Body: {"dry_run": true} returns the proposed plan without creating
    anything; {"dry_run": false} creates every set (AI names arrive in the
    background, one by one)."""
    session = _get_session(request)
    access_token = session.get("access_token")
    if not access_token:
        raise HTTPException(401, "Not authenticated")
    library = _track_cache.get(access_token, [])
    if not library:
        raise HTTPException(400, "Load tracks first via /api/tracks")

    body = await request.json()
    dry_run = bool(body.get("dry_run", True))

    plan = plan_library_tour(library, load_cache())
    if not plan["sets"]:
        raise HTTPException(400, "No analysed tracks to build a tour from.")

    summary = [
        {
            "label":        s["label"],
            "count":        s["count"],
            "duration_ms":  s["duration_ms"],
            "bpm_lo":       s["bpm_lo"],
            "bpm_hi":       s["bpm_hi"],
            "short":        s["short"],
            "sample":       [f'{t["title"]} — {t["artists"]}'
                             for t in s["result"]["tracks"][:3]],
        }
        for s in plan["sets"]
    ]
    if dry_run:
        return JSONResponse({"dry_run": True, "sets": summary, "stats": plan["stats"],
                             "unplaced": plan["unplaced"]})

    created = []
    for s in plan["sets"]:
        params = {"tour": True, "duration_min": round(s["duration_ms"] / 60000),
                  "genre_filter": None, "bpm_range": None}
        pl = create_playlist(s["result"], params, name=s["label"])
        created.append({"id": pl["id"], "name": pl["name"]})
        # AI flavor name lands later, one set at a time (sequential background)
        background_tasks.add_task(
            _ai_rename_playlist, pl["id"],
            {"tracks": s["result"]["tracks"],
             "total_duration_ms": s["duration_ms"], "params": params},
        )
    log.info("Library tour created: %d sets, %d tracks placed",
             len(created), plan["stats"]["placed"])
    return JSONResponse({"dry_run": False, "created": created, "stats": plan["stats"]})


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


async def _export_one(client: httpx.AsyncClient, session: dict, playlist: dict) -> dict:
    """Create the Spotify playlist + add its tracks. Returns {id, url}.
    Raises HTTPException on failure (401 expired / 502 API error)."""
    user_id = session.get("spotify_user_id")
    json_headers = {"Content-Type": "application/json"}

    create_resp = await _spotify_request(
        client, session, "POST",
        f"https://api.spotify.com/v1/users/{user_id}/playlists",
        headers=json_headers,
        json={
            "name":        playlist["name"],
            "description": f"Generated by Spoto DJ — {playlist['track_count']} tracks",
            "public":      False,
        },
    )
    if create_resp.status_code == 401:
        raise HTTPException(401, "Spotify session expired — please log in again")
    if create_resp.status_code not in (200, 201):
        raise HTTPException(502, f"Spotify create playlist failed: {create_resp.status_code}")
    spotify_pid = create_resp.json()["id"]
    spotify_url = create_resp.json()["external_urls"]["spotify"]

    uris = [f"spotify:track:{t['spotify_id']}" for t in playlist["tracks"]]
    for i in range(0, len(uris), 100):
        add_resp = await _spotify_request(
            client, session, "POST",
            f"https://api.spotify.com/v1/playlists/{spotify_pid}/tracks",
            headers=json_headers,
            json={"uris": uris[i:i + 100]},
        )
        if add_resp.status_code not in (200, 201):
            log.warning("Add tracks batch failed: %d", add_resp.status_code)

    return {"id": spotify_pid, "url": spotify_url}


@app.post("/api/playlists/export-all")
async def export_all_playlists(request: Request):
    """Export every saved set that isn't on Spotify yet. Sequential, gentle."""
    session = _get_session(request)
    if not session.get("access_token"):
        raise HTTPException(401, "Not authenticated")
    if not session.get("spotify_user_id"):
        raise HTTPException(403, "Re-login required for playlist export (missing Spotify user ID)")

    playlists = load_playlists()
    pending = {pid: pl for pid, pl in playlists.items()
               if not pl.get("spotify_playlist_id")}
    exported, failed = [], []

    async with httpx.AsyncClient(timeout=30) as client:
        for pid, pl in pending.items():
            try:
                res = await _export_one(client, session, pl)
            except HTTPException as e:
                if e.status_code == 401:
                    raise   # token beyond refresh — surface to the user
                failed.append({"id": pid, "name": pl["name"], "detail": e.detail})
                continue
            pl["spotify_playlist_id"]  = res["id"]
            pl["spotify_playlist_url"] = res["url"]
            playlists[pid] = pl
            save_playlists(playlists)          # persist progress per playlist
            exported.append({"id": pid, "name": pl["name"], "url": res["url"]})
            log.info("Exported %s → %s", pl["name"][:50], res["url"])
            await asyncio.sleep(0.4)           # gentle on the API rate limit

    skipped = len(playlists) - len(pending)
    log.info("Export-all done: %d exported, %d failed, %d already on Spotify",
             len(exported), len(failed), skipped)
    return JSONResponse({"exported": exported, "failed": failed, "skipped": skipped})


@app.post("/api/playlists/{pid}/export")
async def export_playlist(request: Request, pid: str):
    session = _get_session(request)
    if not session.get("access_token"):
        raise HTTPException(401, "Not authenticated")
    if not session.get("spotify_user_id"):
        raise HTTPException(403, "Re-login required for playlist export (missing Spotify user ID)")

    playlists = load_playlists()
    if pid not in playlists:
        raise HTTPException(404, "Playlist not found")
    playlist = playlists[pid]

    async with httpx.AsyncClient(timeout=30) as client:
        res = await _export_one(client, session, playlist)

    playlist["spotify_playlist_id"]  = res["id"]
    playlist["spotify_playlist_url"] = res["url"]
    playlists[pid] = playlist
    save_playlists(playlists)

    log.info("Exported playlist %s → %s", pid, res["url"])
    return JSONResponse({"spotify_playlist_url": res["url"], "spotify_playlist_id": res["id"]})


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
