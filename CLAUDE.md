# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Git workflow

Always work on a feature branch — never commit directly to `main`.

```bash
git checkout -b feat/<short-description>   # start work
# ... make changes, commit ...
gh pr create                               # open PR against main
```

Branch naming: `feat/`, `fix/`, `refactor/`, `chore/` prefix followed by a kebab-case description (e.g. `feat/bpm-range-control`).

## Commands

```bash
# Run server (development)
.venv/bin/uvicorn main:app --reload

# Run tests
.venv/bin/pytest test_analysis.py -v

# Run a single test
.venv/bin/pytest test_analysis.py::test_camelot_values -v

# Install dependencies
.venv/bin/pip install -r requirements.txt
```

Python runtime is **3.9.6** — use `Optional[X]` not `X | None`, and `Union[A, B]` not `A | B`.

## Architecture

Single-process FastAPI app with a vanilla JS SPA frontend. No database — all state is either in-memory or in flat JSON files.

### Request flow

```
Browser → GET /           → serves static/index.html
        → GET /api/me     → checks session cookie
        → GET /login      → redirects to Spotify OAuth
        → GET /callback   → exchanges code for token, sets cookie
        → GET /api/tracks → calls build_track_library(), caches in _track_cache[token]
        → POST /api/analyze → starts _run_analysis() as BackgroundTask
        → GET /api/analyze/status → polled every 2s by frontend
```

### Module responsibilities

- **`main.py`** — FastAPI routes, Spotify OAuth, two in-memory dicts (`_sessions`, `_track_cache`), the `_run_analysis()` background task that drives `getsongbpm.py`
- **`spotify.py`** — Spotify API client. Assembles the full track library dict (liked tracks + artist genres + audio features). Audio features return 403 on new Spotify apps and are silently skipped.
- **`getsongbpm.py`** — GetSongBPM API client. `lookup_track()` does a 3-pass search (type=both → type=song with artist match → type=song first result). Returns `QUOTA_EXCEEDED` sentinel on 401/402/403 to abort the whole analysis run.
- **`analysis.py`** — Legacy audio analysis via librosa + yt-dlp (not called in production). Still the canonical source for `CAMELOT`, `KEY_NAMES`, `load_cache()`, and `save_cache()`.

### Persistence

- **`.audio_cache.json`** — Maps Spotify track ID → `{bpm, key, camelot, energy}` or `{error: "not found: 'title'"}`. Only permanent errors are cached; transient errors (network, quota) are not, so they retry next session.
- **`_sessions` / `_track_cache`** — Pure in-memory; lost on server restart. Users must re-authenticate after restart.

### Key data shape

`build_track_library()` returns a list of track dicts. Important fields:

- `artists` — comma-separated **string**, not a list (e.g. `"Travis Scott, Drake"`)
- `bpm`, `key`, `camelot` — initially `0` / `"?"` if Spotify audio features are unavailable; filled in by `_run_analysis()` from the cache or GetSongBPM
- `duration_ms` — integer milliseconds (for timing/playlist calculations)
- `popularity` — Spotify popularity score 0–100

### Camelot / key data

`CAMELOT` dict maps `(pitch: int 0–11, mode: int 0=minor/1=major)` → Camelot string (e.g. `"8A"`, `"3B"`). It is **duplicated** in both `analysis.py` and `spotify.py` — canonical source is `analysis.py`.

GetSongBPM returns `open_key` (Traktor notation, e.g. `"2m"`), which is **not** the same as Camelot. Camelot = `(open_key_number + 7 - 1) % 12 + 1`. The app derives Camelot from `key_of` (e.g. `"Em"`) via `_parse_key()` → `CAMELOT` table instead.

### GetSongBPM API quirks

- Base URL: `https://api.getsong.co` (not `getsongbpm.com`)
- `_search()` returns: `list` (results), `[]` (no match), `None` (network error), `QUOTA_EXCEEDED` sentinel (401/402/403)
- 3000 req/hour limit. With `asyncio.Semaphore(2)`, each `lookup_track()` makes 1–2 requests per track.

### Frontend

No build step. `static/app.js` uses plain `fetch()` + DOM manipulation. `allTracks` holds the full library; `applyFilters()` re-renders the table on every filter change. Analysis results are merged into `allTracks` via `applyAnalysisResults()` as they arrive from the status poll.

## Environment variables

See `.env.example`. `GETSONGBPM_API_KEY` must be added manually (not in the example file).
