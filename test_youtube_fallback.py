"""
Tests for the YouTube/librosa analysis fallback.

Two kinds of tests:

  UNIT  — mock everything, test fallback logic in main._run_analysis
  INTEGRATION — real yt-dlp download + librosa analysis (requires internet + ffmpeg)

Run all:
    .venv/bin/pytest test_youtube_fallback.py -v -s

Run only unit tests (no internet required):
    .venv/bin/pytest test_youtube_fallback.py -v -m "not integration"

Run only integration tests:
    .venv/bin/pytest test_youtube_fallback.py -v -s -m integration
"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio  # noqa: F401  — ensures plugin is loaded

pytestmark = pytest.mark.asyncio  # all async tests use asyncio mode


# ── Fixture: reset module-level state between tests ──────────────────────────

@pytest.fixture(autouse=True)
def reset_analysis_state():
    """Clear main._analysis_state before and after every test."""
    import main as m
    blank = {"running": False, "backfilling": False, "total": 0, "done": 0, "results": {}}
    m._analysis_state.update(blank)
    yield
    m._analysis_state.update(blank)


# ── UNIT: fallback logic in main._run_analysis ────────────────────────────────

async def test_youtube_fallback_called_on_not_found():
    """
    When GetSongBPM returns 'not found', _yt_analyze should be called
    and its result should end up in the cache tagged source='youtube'.
    """
    import main as m

    track = {
        "id": "tid_001",
        "title": "Some Obscure Track",
        "artists": "Unknown Artist",
        "album": "",
    }
    getsongbpm_miss = {"error": "not found: 'Some Obscure Track'"}
    youtube_hit = {"bpm": 128, "key": "A min", "camelot": "8A", "energy": 60}

    with (
        patch("main.lookup_track", new=AsyncMock(return_value=("tid_001", getsongbpm_miss))),
        patch("main._lastfm_tags", new=AsyncMock(return_value=[])),
        patch("main._yt_analyze",  new=AsyncMock(return_value=("tid_001", youtube_hit))) as mock_yt,
    ):
        cache = {}
        await m._run_analysis([track], [], cache)

    mock_yt.assert_called_once()
    assert cache["tid_001"]["bpm"] == 128
    assert cache["tid_001"]["source"] == "youtube"
    assert "error" not in cache["tid_001"]


async def test_youtube_not_called_on_transient_quota_error():
    """
    quota exceeded is transient — YouTube fallback must NOT be triggered,
    and the track must NOT be written to cache.
    """
    import main as m

    track = {
        "id": "tid_002",
        "title": "Some Track",
        "artists": "Some Artist",
        "album": "",
    }
    quota_error = {"error": "quota exceeded"}

    with (
        patch("main.lookup_track", new=AsyncMock(return_value=("tid_002", quota_error))),
        patch("main._lastfm_tags", new=AsyncMock(return_value=[])),
        patch("main._yt_analyze",  new=AsyncMock()) as mock_yt,
    ):
        cache = {}
        await m._run_analysis([track], [], cache)

    mock_yt.assert_not_called()
    assert "tid_002" not in cache


async def test_audio_download_failure_caches_as_error():
    """
    When both GetSongBPM AND audio download (SoundCloud/YouTube) fail,
    the track is cached as an error so it is not retried on the next session.
    """
    import main as m

    track = {
        "id": "tid_003",
        "title": "Ghost Track",
        "artists": "No One",
        "album": "",
    }
    getsongbpm_miss = {"error": "not found: 'Ghost Track'"}
    audio_miss      = {"error": "audio download failed for 'Ghost Track'"}

    with (
        patch("main.lookup_track", new=AsyncMock(return_value=("tid_003", getsongbpm_miss))),
        patch("main._lastfm_tags", new=AsyncMock(return_value=[])),
        patch("main._yt_analyze",  new=AsyncMock(return_value=("tid_003", audio_miss))),
    ):
        cache = {}
        await m._run_analysis([track], [], cache)

    assert "tid_003" in cache
    assert "error" in cache["tid_003"]


async def test_getsongbpm_hit_skips_youtube():
    """
    When GetSongBPM succeeds, _yt_analyze should never be called.
    """
    import main as m

    track = {
        "id": "tid_004",
        "title": "Popular Track",
        "artists": "Famous Artist",
        "album": "",
    }
    getsongbpm_hit = {"bpm": 140, "key": "G maj", "camelot": "9B", "energy": 80}

    with (
        patch("main.lookup_track", new=AsyncMock(return_value=("tid_004", getsongbpm_hit))),
        patch("main._lastfm_tags", new=AsyncMock(return_value=[])),
        patch("main._yt_analyze",  new=AsyncMock()) as mock_yt,
    ):
        cache = {}
        await m._run_analysis([track], [], cache)

    mock_yt.assert_not_called()
    assert cache["tid_004"]["bpm"] == 140
    assert "source" not in cache["tid_004"]


# ── INTEGRATION: real YouTube download + librosa ──────────────────────────────
# These tests hit the network; skip them with: pytest -m "not integration"

@pytest.mark.integration
def test_audio_download_daft_punk():
    """Downloads audio of a well-known track (SoundCloud first) and verifies the file is valid."""
    from analysis import _download_audio
    path = _download_audio("Around the World", "Daft Punk")
    try:
        assert path is not None, "yt-dlp returned None — check ffmpeg / network"
        p = Path(path)
        assert p.exists(), f"Expected file at {path}"
        assert p.stat().st_size > 10_000, f"File too small ({p.stat().st_size} bytes) — probably corrupt"
    finally:
        if path:
            Path(path).unlink(missing_ok=True)


@pytest.mark.integration
async def test_analyze_track_youtube_daft_punk():
    """
    Full pipeline: download from YouTube → librosa → BPM / key / Camelot.
    'Around the World' by Daft Punk is ~135 BPM.
    """
    from analysis import analyze_track
    sem = asyncio.Semaphore(1)
    track_id, result = await analyze_track(
        track_id="daft_punk_atw",
        title="Around the World",
        artists="Daft Punk",
        preview_url="",  # empty forces YouTube path
        semaphore=sem,
    )
    assert track_id == "daft_punk_atw"
    assert "error" not in result, f"Analysis failed: {result.get('error')}"
    for field in ("bpm", "key", "camelot", "energy"):
        assert field in result, f"Missing field '{field}' in result"
    assert 120 <= result["bpm"] <= 150, f"Expected ~135 BPM for Daft Punk ATW, got {result['bpm']}"
    assert result["camelot"] != "?", "Camelot should be resolved"


@pytest.mark.integration
async def test_analyze_track_youtube_chemical_brothers():
    """
    Second track with a different style: 'Block Rockin Beats' is ~107 BPM.
    Validates the fallback works across genres.
    """
    from analysis import analyze_track
    sem = asyncio.Semaphore(1)
    track_id, result = await analyze_track(
        track_id="chem_bros_brb",
        title="Block Rockin Beats",
        artists="The Chemical Brothers",
        preview_url="",
        semaphore=sem,
    )
    assert "error" not in result, f"Analysis failed: {result.get('error')}"
    # librosa can detect at half/double time; accept a wide range for complex beats
    assert 85 <= result["bpm"] <= 140, f"Expected 85–140 BPM for Block Rockin Beats, got {result['bpm']}"
    assert result.get("camelot", "?") != "?"
