"""
Tests for essentia_analysis.py — zotify download orchestration.

Network, zotify and essentia are never touched: subprocess is mocked and the
audio-dir is a tmp_path. Covers:
  - zotify command construction (flags that keep rate-limit exposure low)
  - cache short-circuit (existing file → no subprocess)
  - ground-truth-by-file success/failure detection
  - timeout and missing-binary handling
  - analyze_track_full error propagation
  - Camelot conversion from essentia key names
"""
import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import essentia_analysis as ea


@pytest.fixture
def audio_dir(tmp_path, monkeypatch):
    d = tmp_path / "audio"
    monkeypatch.setattr(ea, "AUDIO_DIR", d)
    return d


@pytest.fixture
def fake_zotify(tmp_path, monkeypatch):
    """A zotify binary path that exists (contents irrelevant — it's mocked)."""
    binpath = tmp_path / "zotify"
    binpath.write_text("#!/bin/sh\n")
    monkeypatch.setattr(ea, "ZOTIFY_BIN", str(binpath))
    return binpath


@pytest.fixture
def logged_in(tmp_path, monkeypatch):
    """Pretend zotify has saved credentials so download_track proceeds."""
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    monkeypatch.setattr(ea, "ZOTIFY_CREDENTIALS", creds)
    return creds


TRACK_ID = "77aKtsd1ZO94xmQmX671db"
TRACK_URL = f"https://open.spotify.com/track/{TRACK_ID}"


# ─────────────────────────────────────────────────────────────────────────────
# Command construction
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildZotifyCommand:
    def test_lowest_quality_and_copy_codec(self):
        cmd = ea.build_zotify_command(TRACK_URL)
        assert cmd[cmd.index("--download-quality") + 1] == "normal"
        assert cmd[cmd.index("--codec") + 1] == "copy"

    def test_output_is_track_id_template(self):
        cmd = ea.build_zotify_command(TRACK_URL)
        assert cmd[cmd.index("--output-single") + 1] == "{id}"
        assert cmd[cmd.index("--root-path") + 1] == str(ea.AUDIO_DIR)

    def test_url_is_last_argument(self):
        cmd = ea.build_zotify_command(TRACK_URL)
        assert cmd[-1] == TRACK_URL

    def test_extra_api_calls_disabled(self):
        """Genre/disc-totals metadata and lyrics all add per-track API calls
        (rate-limit exposure, zotify issue #209) — they must be off."""
        cmd = ea.build_zotify_command(TRACK_URL)
        for flag in ("--md-save-genres", "--md-disc-track-totals",
                     "--download-lyrics", "--lyrics-to-file",
                     "--lyrics-to-metadata", "--album-art-jpg-file"):
            assert cmd[cmd.index(flag) + 1] == "False", flag

    def test_rate_limiter_passed_through(self):
        cmd = ea.build_zotify_command(TRACK_URL)
        assert cmd[cmd.index("--download-rate-limiter") + 1] == ea.ZOTIFY_RATE_LIMITER


# ─────────────────────────────────────────────────────────────────────────────
# download_track
# ─────────────────────────────────────────────────────────────────────────────

class TestDownloadTrack:
    def test_cached_file_short_circuits(self, audio_dir, fake_zotify):
        audio_dir.mkdir(parents=True)
        cached = audio_dir / f"{TRACK_ID}.ogg"
        cached.write_bytes(b"fake-ogg")
        with patch.object(ea.subprocess, "run") as run:
            assert ea.download_track(TRACK_URL, TRACK_ID) == cached
            run.assert_not_called()

    def test_missing_binary_returns_none(self, audio_dir, monkeypatch):
        monkeypatch.setattr(ea, "ZOTIFY_BIN", "/nonexistent/zotify")
        with patch.object(ea.subprocess, "run") as run:
            assert ea.download_track(TRACK_URL, TRACK_ID) is None
            run.assert_not_called()

    def test_no_credentials_never_spawns_zotify(self, audio_dir, fake_zotify, tmp_path, monkeypatch):
        """The critical guard: without saved credentials, zotify must NOT be
        spawned (its interactive login binds 0.0.0.0 → macOS network prompt)."""
        monkeypatch.setattr(ea, "ZOTIFY_CREDENTIALS", tmp_path / "does-not-exist.json")
        with patch.object(ea.subprocess, "run") as run:
            assert ea.download_track(TRACK_URL, TRACK_ID) is None
            run.assert_not_called()

    def test_success_detected_by_output_file(self, audio_dir, fake_zotify, logged_in):
        def fake_run(cmd, **kwargs):
            (audio_dir / f"{TRACK_ID}.ogg").write_bytes(b"fake-ogg")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        with patch.object(ea.subprocess, "run", side_effect=fake_run):
            path = ea.download_track(TRACK_URL, TRACK_ID)
        assert path == audio_dir / f"{TRACK_ID}.ogg"

    def test_zero_exit_without_file_is_failure(self, audio_dir, fake_zotify, logged_in):
        """zotify exit codes can mask errors (issue #222) — the file is the
        ground truth, not the exit code."""
        ok = subprocess.CompletedProcess([], 0, stdout="done", stderr="")
        with patch.object(ea.subprocess, "run", return_value=ok):
            assert ea.download_track(TRACK_URL, TRACK_ID) is None

    def test_timeout_returns_none(self, audio_dir, fake_zotify, logged_in):
        with patch.object(
            ea.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(cmd="zotify", timeout=1),
        ):
            assert ea.download_track(TRACK_URL, TRACK_ID) is None

    def test_stdin_devnull(self, audio_dir, fake_zotify, logged_in):
        """zotify must never be able to block waiting for interactive input."""
        captured = {}
        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        with patch.object(ea.subprocess, "run", side_effect=fake_run):
            ea.download_track(TRACK_URL, TRACK_ID)
        assert captured.get("stdin") is subprocess.DEVNULL


# ─────────────────────────────────────────────────────────────────────────────
# analyze_track_full
# ─────────────────────────────────────────────────────────────────────────────

class TestAnalyzeTrackFull:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_download_failure_returns_error(self, audio_dir, monkeypatch):
        monkeypatch.setattr(ea, "ZOTIFY_PACING_JITTER", 0.0)
        with patch.object(ea, "download_track", return_value=None):
            tid, result = self._run(ea.analyze_track_full(
                TRACK_ID, TRACK_URL, "Song", "Artist", asyncio.Semaphore(1),
            ))
        assert tid == TRACK_ID
        assert "error" in result and "download failed" in result["error"]

    def test_success_merges_analysis_and_genres(self, audio_dir, monkeypatch):
        monkeypatch.setattr(ea, "ZOTIFY_PACING_JITTER", 0.0)
        fake_path = audio_dir / f"{TRACK_ID}.ogg"
        analysis = {"bpm": 122.4, "key": "Bb maj", "camelot": "6B", "energy": 80}
        with patch.object(ea, "download_track", return_value=fake_path), \
             patch.object(ea, "analyze_audio", return_value=dict(analysis)), \
             patch.object(ea, "classify_genre", return_value=["Electronic---House"]):
            tid, result = self._run(ea.analyze_track_full(
                TRACK_ID, TRACK_URL, "Song", "Artist", asyncio.Semaphore(1),
            ))
        assert result["bpm"] == 122.4
        assert result["camelot"] == "6B"
        assert result["source"] == "local"
        assert result["track_genres"] == ["Electronic---House"]


# ─────────────────────────────────────────────────────────────────────────────
# Camelot conversion
# ─────────────────────────────────────────────────────────────────────────────

class TestKeyToCamelot:
    @pytest.mark.parametrize("key,scale,expected", [
        ("C", "major", "8B"),
        ("A", "minor", "8A"),
        ("Bb", "major", "6B"),   # flat alias maps to A# pitch class
        ("F#", "minor", "11A"),
    ])
    def test_known_keys(self, key, scale, expected):
        assert ea._key_to_camelot(key, scale) == expected

    def test_unknown_key_returns_question_mark(self):
        assert ea._key_to_camelot("H", "major") == "?"
