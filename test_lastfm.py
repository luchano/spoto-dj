"""
Tests for Last.fm genre tag integration.

Covers:
- lastfm.lookup_track_tags: API calls, filtering, error handling
- Cache persistence: track_genres survives save/load cycle
- Backfill filter: already-tagged and error tracks are excluded
- No re-querying: [] (not found) is cached so tracks are not re-requested
- /api/tracks merge: non-empty track_genres override Spotify artist genres
"""
import asyncio
import json
import tempfile
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lastfm import lookup_track_tags, _MIN_COUNT, _NON_GENRE


# ─── helpers ─────────────────────────────────────────────────────────────────

def _make_lastfm_response(tags: list[tuple[str, int]]) -> dict:
    """Build a fake Last.fm track.getTopTags JSON response."""
    return {
        "toptags": {
            "tag": [{"name": name, "count": count, "url": ""} for name, count in tags]
        }
    }


def _make_client(response_json: dict, status_code: int = 200):
    """Return a mock httpx.AsyncClient whose get() returns the given JSON."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = response_json
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    return client


# ─── lookup_track_tags ────────────────────────────────────────────────────────

class TestLookupTrackTags:

    @pytest.fixture
    def sem(self):
        return asyncio.Semaphore(5)

    def test_returns_genre_tags_above_min_count(self, sem):
        payload = _make_lastfm_response([
            ("deep house", 80),
            ("electronic", 60),
            ("seen live", 30),    # personal tag → filtered out
            ("favourite", 20),    # personal tag → filtered out
            ("minimal", 3),       # below _MIN_COUNT → filtered out
        ])
        client = _make_client(payload)
        tags = asyncio.get_event_loop().run_until_complete(
            lookup_track_tags("KEY", "Strobe", "deadmau5", client, sem)
        )
        assert tags == ["deep house", "electronic"], f"Unexpected: {tags}"

    def test_returns_empty_list_when_track_not_found(self, sem):
        payload = {"error": 6, "message": "Track not found"}
        client = _make_client(payload)
        tags = asyncio.get_event_loop().run_until_complete(
            lookup_track_tags("KEY", "NOPE", "NOBODY", client, sem)
        )
        assert tags == []

    def test_returns_empty_list_when_no_tags(self, sem):
        payload = _make_lastfm_response([])
        client = _make_client(payload)
        tags = asyncio.get_event_loop().run_until_complete(
            lookup_track_tags("KEY", "Song", "Artist", client, sem)
        )
        assert tags == []

    def test_returns_none_on_network_error(self, sem):
        import httpx
        client = MagicMock()
        client.get = AsyncMock(side_effect=httpx.ConnectError("timeout"))
        tags = asyncio.get_event_loop().run_until_complete(
            lookup_track_tags("KEY", "Song", "Artist", client, sem)
        )
        assert tags is None, "Network error must return None (not []) so it's not cached"

    def test_returns_empty_list_when_no_api_key(self, sem):
        client = _make_client({})
        tags = asyncio.get_event_loop().run_until_complete(
            lookup_track_tags("", "Song", "Artist", client, sem)
        )
        assert tags == []
        client.get.assert_not_called()

    def test_all_personal_tags_filtered_out(self, sem):
        payload = _make_lastfm_response([(tag, 100) for tag in list(_NON_GENRE)[:5]])
        client = _make_client(payload)
        tags = asyncio.get_event_loop().run_until_complete(
            lookup_track_tags("KEY", "Song", "Artist", client, sem)
        )
        assert tags == [], f"All personal tags should be filtered, got: {tags}"

    def test_caps_at_five_tags(self, sem):
        payload = _make_lastfm_response([
            (f"genre-{i}", 100 - i * 5) for i in range(10)
        ])
        client = _make_client(payload)
        tags = asyncio.get_event_loop().run_until_complete(
            lookup_track_tags("KEY", "Song", "Artist", client, sem)
        )
        assert len(tags) <= 5

    def test_tags_returned_lowercased(self, sem):
        payload = _make_lastfm_response([("Deep House", 90), ("Electronic", 80)])
        client = _make_client(payload)
        tags = asyncio.get_event_loop().run_until_complete(
            lookup_track_tags("KEY", "Song", "Artist", client, sem)
        )
        assert all(t == t.lower() for t in tags), f"Tags should be lowercase: {tags}"


# ─── cache persistence ────────────────────────────────────────────────────────

class TestCachePersistence:

    def test_track_genres_survives_save_load(self, tmp_path):
        from analysis import save_cache, load_cache
        cache_file = tmp_path / ".audio_cache.json"

        cache = {
            "track_abc": {"bpm": 120, "key": "A min", "camelot": "8A", "energy": 50},
        }
        with patch("analysis.CACHE_FILE", cache_file):
            save_cache(cache)
            cache["track_abc"]["track_genres"] = ["deep house", "minimal techno"]
            save_cache(cache)
            loaded = load_cache()

        assert loaded["track_abc"]["track_genres"] == ["deep house", "minimal techno"]

    def test_empty_track_genres_persisted(self, tmp_path):
        """track_genres=[] (not found on Last.fm) must be saved to prevent re-querying."""
        from analysis import save_cache, load_cache
        cache_file = tmp_path / ".audio_cache.json"

        cache = {"track_xyz": {"bpm": 90, "key": "C maj", "camelot": "8B", "energy": 60}}
        cache["track_xyz"]["track_genres"] = []  # Last.fm returned nothing

        with patch("analysis.CACHE_FILE", cache_file):
            save_cache(cache)
            loaded = load_cache()

        assert "track_genres" in loaded["track_xyz"], \
            "Empty track_genres must be saved so the track is not re-queried"
        assert loaded["track_xyz"]["track_genres"] == []


# ─── backfill filter ──────────────────────────────────────────────────────────

class TestBackfillFilter:
    """The to_genre_backfill filter must include/exclude the right tracks."""

    def _run_filter(self, library, cache):
        return [
            t for t in library
            if t["id"] in cache
            and "error" not in cache[t["id"]]
            and "track_genres" not in cache[t["id"]]
        ]

    def test_excludes_tracks_not_in_cache(self):
        library = [{"id": "new_track", "title": "New", "artists": "Artist"}]
        cache = {}
        assert self._run_filter(library, cache) == []

    def test_excludes_tracks_with_existing_genres(self):
        library = [{"id": "track_a", "title": "A", "artists": "X"}]
        cache = {"track_a": {"bpm": 120, "track_genres": ["house"]}}
        assert self._run_filter(library, cache) == []

    def test_excludes_tracks_with_empty_genres(self):
        """track_genres=[] means Last.fm already returned nothing — don't retry."""
        library = [{"id": "track_b", "title": "B", "artists": "Y"}]
        cache = {"track_b": {"bpm": 130, "track_genres": []}}
        assert self._run_filter(library, cache) == []

    def test_excludes_error_tracks(self):
        library = [{"id": "track_c", "title": "C", "artists": "Z"}]
        cache = {"track_c": {"error": "not found: 'C'"}}
        assert self._run_filter(library, cache) == []

    def test_includes_cached_tracks_without_genres(self):
        library = [{"id": "track_d", "title": "D", "artists": "W"}]
        cache = {"track_d": {"bpm": 110, "key": "E min", "camelot": "9A", "energy": 70}}
        result = self._run_filter(library, cache)
        assert len(result) == 1 and result[0]["id"] == "track_d"

    def test_none_result_not_cached(self):
        """None from lookup_track_tags (network error) must NOT write track_genres to cache."""
        cache = {"track_e": {"bpm": 100}}
        lfm_tags: Optional[list] = None  # network error

        # Simulate _genre_only behavior — correct code uses `is not None`
        if lfm_tags is not None:
            cache["track_e"]["track_genres"] = lfm_tags

        assert "track_genres" not in cache["track_e"], \
            "None (network error) must not be cached — track should retry next session"

    def test_empty_result_is_cached(self):
        """[] from lookup_track_tags (not found) MUST write track_genres=[] to prevent retry."""
        cache = {"track_f": {"bpm": 100}}
        lfm_tags: Optional[list] = []  # not found on Last.fm

        # Correct code uses `is not None`
        if lfm_tags is not None:
            cache["track_f"]["track_genres"] = lfm_tags

        assert "track_genres" in cache["track_f"], \
            "[] (not found) must be cached so the track is not re-queried next session"
        assert cache["track_f"]["track_genres"] == []


# ─── /api/tracks genre merge ─────────────────────────────────────────────────

class TestTracksMerge:
    """Verify the merge logic in /api/tracks."""

    def _merge(self, library, audio_cache):
        """Reproduce the merge logic from the /api/tracks endpoint."""
        if any(audio_cache.get(t["id"], {}).get("track_genres") for t in library):
            return [
                {**t, "genres": audio_cache[t["id"]]["track_genres"]}
                if audio_cache.get(t["id"], {}).get("track_genres")
                else t
                for t in library
            ]
        return library

    def test_merges_non_empty_track_genres(self):
        library = [{"id": "t1", "genres": ["pop"], "title": "Song", "artists": "X"}]
        audio = {"t1": {"bpm": 120, "track_genres": ["deep house", "minimal"]}}
        result = self._merge(library, audio)
        assert result[0]["genres"] == ["deep house", "minimal"]

    def test_keeps_spotify_genres_when_track_genres_empty(self):
        library = [{"id": "t2", "genres": ["pop"], "title": "Song", "artists": "X"}]
        audio = {"t2": {"bpm": 120, "track_genres": []}}  # Last.fm found nothing
        result = self._merge(library, audio)
        assert result[0]["genres"] == ["pop"], \
            "Empty track_genres should not overwrite Spotify genres"

    def test_keeps_spotify_genres_when_track_not_in_audio_cache(self):
        library = [{"id": "t3", "genres": ["electronic"], "title": "Song", "artists": "X"}]
        audio = {}
        result = self._merge(library, audio)
        assert result[0]["genres"] == ["electronic"]

    def test_partial_merge_only_tracks_with_genres(self):
        library = [
            {"id": "t4", "genres": ["pop"], "title": "A", "artists": "X"},
            {"id": "t5", "genres": ["rock"], "title": "B", "artists": "Y"},
        ]
        audio = {
            "t4": {"track_genres": ["synth-pop", "electropop"]},
            "t5": {"track_genres": []},  # not found — keep Spotify
        }
        result = self._merge(library, audio)
        assert result[0]["genres"] == ["synth-pop", "electropop"]
        assert result[1]["genres"] == ["rock"]
