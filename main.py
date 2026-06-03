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
from getsongbpm import lookup_track
from spotify import build_track_library

log = logging.getLogger("spoto")
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")

load_dotenv()

CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "http://localhost:8000/callback")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
GETSONGBPM_API_KEY = os.getenv("GETSONGBPM_API_KEY", "")

SCOPES = "user-library-read"
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


async def _run_analysis(tracks: list[dict], cache: dict):
    log.info("Analysis started: %d tracks via GetSongBPM", len(tracks))
    # 2 concurrent requests — conservative for free-tier rate limits
    semaphore = asyncio.Semaphore(2)

    async def _lookup(track: dict):
        async with httpx.AsyncClient() as client:
            return await lookup_track(
                GETSONGBPM_API_KEY,
                track["id"],
                track.get("title", ""),
                track.get("artists", []),
                client,
                semaphore,
            )

    tasks = [asyncio.create_task(_lookup(t)) for t in tracks]

    errors = 0
    for coro in asyncio.as_completed(tasks):
        track_id, result = await coro
        if "error" in result:
            errors += 1
            log.warning("Lookup failed %s: %s", track_id, result["error"])
        else:
            cache[track_id] = result
            _analysis_state["results"][track_id] = result
        _analysis_state["done"] += 1
        if _analysis_state["done"] % 50 == 0:
            save_cache(cache)
            log.info("Progress: %d/%d (errors: %d)", _analysis_state["done"], len(tracks), errors)

    save_cache(cache)
    _analysis_state["running"] = False
    log.info("Done: %d/%d found, %d errors", len(tracks) - errors, len(tracks), errors)


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

    async with httpx.AsyncClient() as client:
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

    session_id = secrets.token_urlsafe(32)
    response = RedirectResponse("/")
    _set_session(response, session_id, token_data)
    return response


@app.get("/api/me")
async def me(request: Request):
    session = _get_session(request)
    if not session.get("access_token"):
        return JSONResponse({"authenticated": False})
    return JSONResponse({"authenticated": True})


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
    _analysis_state["results"] = dict(cache)
    to_analyze = [t for t in library if t["id"] not in cache]

    log.info(
        "Analyze request: %d total, %d cached, %d to look up via GetSongBPM",
        len(library), len(cache), len(to_analyze),
    )

    _analysis_state.update({
        "running": True,
        "total": len(to_analyze),
        "done": 0,
    })

    if to_analyze:
        background_tasks.add_task(_run_analysis, to_analyze, cache)
    else:
        _analysis_state["running"] = False

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
