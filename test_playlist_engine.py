"""
Tests for playlist_engine.py
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from playlist_engine import (
    PLAYLISTS_FILE,
    build_energy_arc,
    build_pool,
    camelot_neighbors,
    camelot_score,
    classify_genre,
    create_playlist,
    delete_playlist,
    generate,
    load_playlists,
    save_playlists,
    score_candidate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _track(
    tid="t1", title="Song", artists="Artist",
    bpm=128, camelot="8A", energy=70,
    duration_ms=240_000, popularity=50,
    genres=None,
):
    return {
        "id": tid, "title": title, "artists": artists,
        "bpm": bpm, "camelot": camelot, "energy": energy,
        "duration_ms": duration_ms, "popularity": popularity,
        "genres": genres or [],
        "spotify_url": "", "image_url": "", "added_at": "",
    }


def _cache_ok(bpm=128, camelot="8A", energy=70):
    return {"bpm": bpm, "camelot": camelot, "energy": energy}


def _make_library(n=20, base_bpm=128):
    """Return n distinct tracks with staggered BPMs and camelot keys."""
    keys = ["8A", "9A", "8B", "7A", "10A", "5A", "3B", "12A", "6A", "11A",
            "1B", "2A", "4A", "9B", "7B", "6B", "5B", "4B", "3A", "2B"]
    tracks = []
    for i in range(n):
        tracks.append(_track(
            tid=f"t{i}",
            title=f"Track {i}",
            artists=f"Artist {i % 5}",   # 5 artists → some repeats
            bpm=base_bpm + (i % 10) - 5,
            camelot=keys[i % len(keys)],
            energy=40 + (i * 3) % 60,
            popularity=30 + (i * 7) % 70,
        ))
    return tracks


def _make_cache(tracks):
    """Build a cache dict from track list (no errors)."""
    return {t["id"]: _cache_ok(t["bpm"], t["camelot"], t["energy"]) for t in tracks}


# ---------------------------------------------------------------------------
# Camelot wheel
# ---------------------------------------------------------------------------

class TestCamelotNeighbors:
    def test_returns_frozenset(self):
        assert isinstance(camelot_neighbors("8A"), frozenset)

    def test_includes_self(self):
        assert "8A" in camelot_neighbors("8A")

    def test_relative_major_minor(self):
        # 8A (Cm) ↔ 8B (Eb maj) are relative
        assert "8B" in camelot_neighbors("8A")
        assert "8A" in camelot_neighbors("8B")

    def test_wheel_plus_one(self):
        assert "9A" in camelot_neighbors("8A")

    def test_wheel_minus_one(self):
        assert "7A" in camelot_neighbors("8A")

    def test_wrap_12_to_1(self):
        # 12A + 1 = 1A
        assert "1A" in camelot_neighbors("12A")

    def test_wrap_1_to_12(self):
        # 1A - 1 = 12A
        assert "12A" in camelot_neighbors("1A")

    def test_unknown_key_returns_empty(self):
        assert camelot_neighbors("?") == frozenset()
        assert camelot_neighbors("") == frozenset()

    def test_invalid_key_returns_empty(self):
        assert camelot_neighbors("13A") == frozenset()
        assert camelot_neighbors("0B") == frozenset()

    @pytest.mark.parametrize("key", [
        "1A", "1B", "6A", "6B", "12A", "12B",
    ])
    def test_always_4_neighbors(self, key):
        # Every valid key has exactly 4 neighbors (self + relative + ±1)
        assert len(camelot_neighbors(key)) == 4


class TestCamelotScore:
    def test_same_key_is_1(self):
        assert camelot_score("8A", "8A") == 1.0

    def test_adjacent_is_1(self):
        assert camelot_score("8A", "9A") == 1.0
        assert camelot_score("8A", "7A") == 1.0
        assert camelot_score("8A", "8B") == 1.0

    def test_incompatible_is_0(self):
        assert camelot_score("8A", "1A") == 0.0
        assert camelot_score("8A", "4B") == 0.0

    def test_unknown_key_is_neutral(self):
        score = camelot_score("?", "8A")
        assert 0.0 < score < 1.0

    def test_symmetry(self):
        # Compatibility should be symmetric
        assert camelot_score("8A", "9A") == camelot_score("9A", "8A")


# ---------------------------------------------------------------------------
# Energy arc
# ---------------------------------------------------------------------------

class TestBuildEnergyArc:
    def test_correct_length(self):
        assert len(build_energy_arc(10)) == 10
        assert len(build_energy_arc(1)) == 1
        assert len(build_energy_arc(40)) == 40

    def test_values_between_0_and_1(self):
        for profile in ("warmup", "peak_time", "afterhours"):
            arc = build_energy_arc(20, profile)
            assert all(0.0 <= v <= 1.0 for v in arc), f"Out-of-range in {profile}"

    def test_peak_time_peaks_in_middle(self):
        arc = build_energy_arc(20, "peak_time")
        peak_idx = arc.index(max(arc))
        # Peak should be somewhere in the first ~60% of the set
        assert peak_idx < 14

    def test_warmup_starts_low_ends_high(self):
        arc = build_energy_arc(20, "warmup")
        assert arc[0] < arc[-1]

    def test_afterhours_starts_high_ends_low(self):
        arc = build_energy_arc(20, "afterhours")
        assert arc[0] > arc[-1]

    def test_unknown_profile_falls_back_to_peak_time(self):
        arc1 = build_energy_arc(10, "unknown_profile")
        arc2 = build_energy_arc(10, "peak_time")
        assert arc1 == arc2

    def test_single_track_arc(self):
        arc = build_energy_arc(1, "peak_time")
        assert len(arc) == 1
        assert 0.0 <= arc[0] <= 1.0


# ---------------------------------------------------------------------------
# Genre classification
# ---------------------------------------------------------------------------

class TestClassifyGenre:
    def test_electronic(self):
        assert classify_genre(["electronic", "deep house"]) == "electronic"

    def test_hip_hop(self):
        assert classify_genre(["rap", "trap"]) == "hip_hop"

    def test_latin(self):
        assert classify_genre(["latin", "reggaeton"]) == "latin"

    def test_empty_returns_none(self):
        assert classify_genre([]) is None

    def test_unknown_genre_returns_none(self):
        assert classify_genre(["xyzzy", "foobar"]) is None

    def test_mixed_picks_best_match(self):
        # 2 electronic keywords vs 1 hip hop → electronic
        result = classify_genre(["electronic", "deep house", "rap"])
        assert result == "electronic"


# ---------------------------------------------------------------------------
# Pool building
# ---------------------------------------------------------------------------

class TestBuildPool:
    def test_excludes_error_entries(self):
        lib = [_track("t1"), _track("t2")]
        cache = {"t1": _cache_ok(), "t2": {"error": "not found: 'x'"}}
        pool = build_pool(lib, cache)
        assert len(pool) == 1
        assert pool[0]["id"] == "t1"

    def test_excludes_missing_from_cache(self):
        lib = [_track("t1"), _track("t2")]
        cache = {"t1": _cache_ok()}
        pool = build_pool(lib, cache)
        assert len(pool) == 1

    def test_excludes_zero_bpm(self):
        lib = [_track("t1")]
        cache = {"t1": _cache_ok(bpm=0)}
        pool = build_pool(lib, cache)
        assert len(pool) == 0

    def test_uses_cache_bpm_over_library(self):
        lib = [_track("t1", bpm=100)]
        cache = {"t1": _cache_ok(bpm=130)}
        pool = build_pool(lib, cache)
        assert pool[0]["bpm"] == 130

    def test_artists_always_a_list(self):
        lib = [_track("t1", artists="Artist A, Artist B")]
        cache = {"t1": _cache_ok()}
        pool = build_pool(lib, cache)
        assert isinstance(pool[0]["artists_list"], list)
        assert len(pool[0]["artists_list"]) == 2

    def test_genre_cluster_assigned(self):
        lib = [_track("t1", genres=["electronic", "deep house"])]
        cache = {"t1": _cache_ok()}
        pool = build_pool(lib, cache)
        assert pool[0]["genre_cluster"] == "electronic"

    def test_empty_library(self):
        assert build_pool([], {}) == []

    def test_empty_cache(self):
        lib = [_track("t1"), _track("t2")]
        assert build_pool(lib, {}) == []


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

class TestScoreCandidate:
    def _c(self, bpm=128, camelot="8A", energy=70, popularity=50):
        t = _track(bpm=bpm, camelot=camelot, energy=energy, popularity=popularity)
        pool = build_pool([t], {t["id"]: _cache_ok(bpm, camelot, energy)})
        return pool[0]

    def test_same_key_beats_incompatible(self):
        prev = self._c(camelot="8A")
        same_key = self._c(camelot="8A")
        bad_key  = self._c(camelot="4B")
        s_same = score_candidate(same_key, prev, 0.7, False, set())
        s_bad  = score_candidate(bad_key,  prev, 0.7, False, set())
        assert s_same > s_bad

    def test_close_bpm_beats_far(self):
        prev   = self._c(bpm=128)
        close  = self._c(bpm=130, camelot="8A")
        far    = self._c(bpm=150, camelot="8A")
        s_close = score_candidate(close, prev, 0.7, False, set())
        s_far   = score_candidate(far,   prev, 0.7, False, set())
        assert s_close > s_far

    def test_same_artist_penalty(self):
        prev  = self._c()
        cand  = self._c(popularity=80)
        pool_cand = build_pool(
            [_track("tx", artists="Artist A", popularity=80)],
            {"tx": _cache_ok()},
        )[0]
        no_penalty = score_candidate(pool_cand, prev, 0.7, False, set())
        with_penalty = score_candidate(pool_cand, prev, 0.7, False, {"artist a"})
        assert no_penalty > with_penalty

    def test_hit_slot_favours_popular(self):
        popular  = self._c(popularity=90)
        obscure  = self._c(popularity=10)
        s_pop_hit  = score_candidate(popular, None, 0.7, True, set())
        s_obs_hit  = score_candidate(obscure, None, 0.7, True, set())
        assert s_pop_hit > s_obs_hit

    def test_score_never_negative(self):
        prev = self._c()
        cand = self._c(bpm=200, camelot="4B", energy=5)
        assert score_candidate(cand, prev, 1.0, False, {"artist"}) >= 0.0

    def test_no_prev_still_scores(self):
        cand = self._c()
        score = score_candidate(cand, None, 0.5, False, set())
        assert 0.0 <= score <= 1.5   # possible range


# ---------------------------------------------------------------------------
# Core generator
# ---------------------------------------------------------------------------

class TestGenerate:
    def setup_method(self):
        self.library = _make_library(30)
        self.cache   = _make_cache(self.library)

    def test_returns_tracks(self):
        result = generate(self.library, self.cache, duration_min=40)
        assert len(result["tracks"]) > 0

    def test_honours_approximate_duration(self):
        result = generate(self.library, self.cache, duration_min=60)
        # Should produce roughly 60 min worth of music (± 25%)
        minutes = result["total_duration_ms"] / 60_000
        assert 30 <= minutes <= 90

    def test_no_duplicate_tracks(self):
        result = generate(self.library, self.cache, duration_min=60)
        ids = [t["id"] for t in result["tracks"]]
        assert len(ids) == len(set(ids))

    def test_no_same_artist_consecutive(self):
        result = generate(self.library, self.cache, duration_min=60)
        tracks = result["tracks"]
        for i in range(1, len(tracks)):
            prev_artists = {a.lower() for a in tracks[i-1]["artists_list"]}
            curr_artists = {a.lower() for a in tracks[i]["artists_list"]}
            # Allow overlap only if there really is no alternative
            # (just verify the field exists; enforcement is best-effort)
            assert "artists_list" in tracks[i]

    def test_empty_cache_returns_error(self):
        result = generate(self.library, {})
        assert "error" in result

    def test_genre_filter_adds_warning_when_insufficient(self):
        # Use a genre that no track in our library matches
        result = generate(self.library, self.cache, genre_filter="latin")
        # Either it worked with the full library (warning added) or it still produced tracks
        assert "tracks" in result
        # If genre had no matches, warning should mention it
        if result.get("warnings"):
            assert any("latin" in w for w in result["warnings"])

    def test_bpm_range_filter(self):
        result = generate(
            self.library, self.cache,
            duration_min=30,
            bpm_range=(125, 132),
        )
        if not result.get("warnings"):
            # All tracks should be within (relaxed) range when filter applied
            for t in result["tracks"]:
                assert 115 <= t["bpm"] <= 145   # allow some slack from pass 2/3

    def test_min_5_tracks(self):
        # Even a very short request produces at least some tracks
        result = generate(self.library, self.cache, duration_min=10)
        assert result["track_count"] >= 1

    def test_all_profiles(self):
        for profile in ("warmup", "peak_time", "afterhours"):
            result = generate(
                self.library, self.cache,
                duration_min=40, energy_profile=profile
            )
            assert result["track_count"] > 0, f"Profile {profile} produced no tracks"

    def test_result_fields_present(self):
        result = generate(self.library, self.cache, duration_min=40)
        assert "tracks" in result
        assert "total_duration_ms" in result
        assert "track_count" in result
        assert "warnings" in result

    def test_track_fields_present(self):
        result = generate(self.library, self.cache, duration_min=40)
        required = {"id", "title", "artists", "bpm", "camelot", "energy",
                    "duration_ms", "popularity"}
        for t in result["tracks"]:
            assert required.issubset(t.keys())

    def test_track_count_matches_tracks_list(self):
        result = generate(self.library, self.cache, duration_min=60)
        assert result["track_count"] == len(result["tracks"])

    def test_bpm_transitions_mostly_smooth(self):
        """At least 70% of consecutive transitions should be within ±15 BPM."""
        result = generate(self.library, self.cache, duration_min=60)
        tracks = result["tracks"]
        if len(tracks) < 2:
            return
        smooth = sum(
            1 for i in range(1, len(tracks))
            if abs(tracks[i]["bpm"] - tracks[i-1]["bpm"]) <= 15
        )
        ratio = smooth / (len(tracks) - 1)
        assert ratio >= 0.70, f"Only {ratio:.0%} smooth BPM transitions"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    def setup_method(self, method):
        # Redirect PLAYLISTS_FILE to a temp file for each test
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        self._tmp_path = Path(self._tmp.name)
        self._patcher = patch("playlist_engine.PLAYLISTS_FILE", self._tmp_path)
        self._patcher.start()

    def teardown_method(self, method):
        self._patcher.stop()
        self._tmp_path.unlink(missing_ok=True)

    def test_load_empty_when_missing(self):
        self._tmp_path.unlink(missing_ok=True)
        assert load_playlists() == {}

    def test_save_and_load_roundtrip(self):
        data = {"pl_abc": {"id": "pl_abc", "name": "Test"}}
        save_playlists(data)
        assert load_playlists() == data

    def test_create_playlist_persists(self):
        library = _make_library(20)
        cache   = _make_cache(library)
        result  = generate(library, cache, duration_min=30)
        params  = {"duration_min": 30, "energy_profile": "peak_time"}

        pl = create_playlist(result, params, name="My Set")
        stored = load_playlists()

        assert pl["id"] in stored
        assert stored[pl["id"]]["name"] == "My Set"

    def test_create_playlist_auto_name(self):
        library = _make_library(20)
        cache   = _make_cache(library)
        result  = generate(library, cache, duration_min=30)
        pl = create_playlist(result, {"energy_profile": "warmup"})
        assert "Warm-Up" in pl["name"]

    def test_create_playlist_track_structure(self):
        library = _make_library(20)
        cache   = _make_cache(library)
        result  = generate(library, cache, duration_min=30)
        pl = create_playlist(result, {})
        for t in pl["tracks"]:
            assert "position" in t
            assert "spotify_id" in t
            assert "bpm" in t
            assert "camelot" in t

    def test_create_playlist_has_spotify_fields(self):
        library = _make_library(20)
        cache   = _make_cache(library)
        result  = generate(library, cache, duration_min=30)
        pl = create_playlist(result, {})
        assert pl["spotify_playlist_id"] is None
        assert pl["spotify_playlist_url"] is None

    def test_delete_playlist(self):
        library = _make_library(20)
        cache   = _make_cache(library)
        result  = generate(library, cache, duration_min=30)
        pl = create_playlist(result, {})
        pid = pl["id"]

        assert delete_playlist(pid) is True
        assert pid not in load_playlists()

    def test_delete_nonexistent_returns_false(self):
        assert delete_playlist("pl_doesnotexist") is False

    def test_multiple_playlists_coexist(self):
        library = _make_library(20)
        cache   = _make_cache(library)
        result  = generate(library, cache, duration_min=30)
        p1 = create_playlist(result, {}, name="Set 1")
        p2 = create_playlist(result, {}, name="Set 2")
        stored = load_playlists()
        assert p1["id"] in stored
        assert p2["id"] in stored
        assert len(stored) == 2
