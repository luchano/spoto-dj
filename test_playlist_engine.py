"""
Tests for playlist_engine.py — narrative arc edition.

Coverage:
  - Camelot wheel correctness
  - Energy arc profiles
  - Genre classification
  - Pool building
  - Section definitions (weights, BPM factors)
  - Section distribution
  - Track role classification
  - Scoring function
  - Core generator (narrative, sections, BPM arc, no duplicates)
  - Persistence
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from playlist_engine import (
    PLAYLISTS_FILE,
    UNIVERSAL_SECTIONS,
    SectionDef,
    SectionSlot,
    build_energy_arc,
    build_pool,
    camelot_neighbors,
    camelot_score,
    classify_genre,
    classify_track_role,
    compute_base_bpm,
    create_playlist,
    delete_playlist,
    distribute_sections,
    generate,
    genre_options,
    load_playlists,
    save_playlists,
    score_candidate,
    tags_to_clusters,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

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


def _make_library(n=30, base_bpm=112):
    """Return *n* tracks with varied BPMs, keys, energies and popularities."""
    keys = ["8A","9A","8B","7A","10A","5A","3B","12A","6A","11A",
            "1B","2A","4A","9B","7B","6B","5B","4B","3A","2B"]
    tracks = []
    for i in range(n):
        bpm    = base_bpm + (i % 20) - 10     # base ± 10
        energy = 20 + (i * 4) % 80            # 20–99
        pop    = 10 + (i * 7) % 91            # 10–100
        tracks.append(_track(
            tid=f"t{i}", title=f"Track {i}", artists=f"Artist {i % 6}",
            bpm=bpm, camelot=keys[i % len(keys)],
            energy=energy, popularity=pop,
        ))
    return tracks


def _make_cache(tracks):
    return {t["id"]: _cache_ok(t["bpm"], t["camelot"], t["energy"]) for t in tracks}


# ─────────────────────────────────────────────────────────────────────────────
# Camelot wheel
# ─────────────────────────────────────────────────────────────────────────────

class TestCamelotNeighbors:
    def test_returns_frozenset(self):
        assert isinstance(camelot_neighbors("8A"), frozenset)

    def test_includes_self(self):
        assert "8A" in camelot_neighbors("8A")

    def test_relative_major_minor(self):
        assert "8B" in camelot_neighbors("8A")
        assert "8A" in camelot_neighbors("8B")

    def test_wheel_plus_one(self):
        assert "9A" in camelot_neighbors("8A")

    def test_wheel_minus_one(self):
        assert "7A" in camelot_neighbors("8A")

    def test_wrap_12_to_1(self):
        assert "1A" in camelot_neighbors("12A")

    def test_wrap_1_to_12(self):
        assert "12A" in camelot_neighbors("1A")

    def test_unknown_key_returns_empty(self):
        assert camelot_neighbors("?") == frozenset()
        assert camelot_neighbors("") == frozenset()

    def test_invalid_key_returns_empty(self):
        assert camelot_neighbors("13A") == frozenset()
        assert camelot_neighbors("0B") == frozenset()

    @pytest.mark.parametrize("key", ["1A","1B","6A","6B","12A","12B"])
    def test_always_4_neighbors(self, key):
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
        assert camelot_score("8A", "9A") == camelot_score("9A", "8A")


# ─────────────────────────────────────────────────────────────────────────────
# Energy arc
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildEnergyArc:
    def test_correct_length(self):
        for n in (1, 10, 40):
            assert len(build_energy_arc(n)) == n

    def test_values_between_0_and_1(self):
        for profile in ("warmup", "peak_time", "afterhours"):
            arc = build_energy_arc(20, profile)
            assert all(0.0 <= v <= 1.0 for v in arc)

    def test_peak_time_peaks_in_first_60_pct(self):
        arc = build_energy_arc(20, "peak_time")
        peak_idx = arc.index(max(arc))
        assert peak_idx < 13

    def test_warmup_starts_low_ends_high(self):
        arc = build_energy_arc(20, "warmup")
        assert arc[0] < arc[-1]

    def test_afterhours_starts_high_ends_low(self):
        arc = build_energy_arc(20, "afterhours")
        assert arc[0] > arc[-1]

    def test_unknown_profile_falls_back_to_peak_time(self):
        assert build_energy_arc(10, "xyzzy") == build_energy_arc(10, "peak_time")

    def test_single_track_arc(self):
        arc = build_energy_arc(1)
        assert len(arc) == 1 and 0.0 <= arc[0] <= 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Universal sections
# ─────────────────────────────────────────────────────────────────────────────

class TestUniversalSections:
    def test_weights_sum_to_one(self):
        total = sum(s.weight for s in UNIVERSAL_SECTIONS)
        assert abs(total - 1.0) < 1e-6, f"weights sum to {total}"

    def test_exactly_7_sections(self):
        assert len(UNIVERSAL_SECTIONS) == 7

    def test_energy_ranges_valid(self):
        for s in UNIVERSAL_SECTIONS:
            assert 0 <= s.energy_min < s.energy_max <= 100, \
                f"{s.name}: bad energy range [{s.energy_min},{s.energy_max}]"

    def test_bpm_factors_reasonable(self):
        for s in UNIVERSAL_SECTIONS:
            assert 0.80 <= s.bpm_factor <= 1.20, \
                f"{s.name}: bpm_factor {s.bpm_factor} out of range"

    def test_climax_higher_energy_than_journey(self):
        secs = {s.name: s for s in UNIVERSAL_SECTIONS}
        assert secs["climax"].energy_min > secs["mid_journey"].energy_min

    def test_intro_lower_energy_than_climax(self):
        secs = {s.name: s for s in UNIVERSAL_SECTIONS}
        intro_mid  = (secs["intro"].energy_min  + secs["intro"].energy_max)  / 2
        climax_mid = (secs["climax"].energy_min + secs["climax"].energy_max) / 2
        assert intro_mid < climax_mid

    def test_outro_lower_energy_than_climax(self):
        secs = {s.name: s for s in UNIVERSAL_SECTIONS}
        outro_mid  = (secs["outro"].energy_min  + secs["outro"].energy_max)  / 2
        climax_mid = (secs["climax"].energy_min + secs["climax"].energy_max) / 2
        assert outro_mid < climax_mid

    def test_section_names_include_narrative_acts(self):
        names = {s.name for s in UNIVERSAL_SECTIONS}
        for required in ("intro", "mid_journey", "climax", "outro"):
            assert required in names


# ─────────────────────────────────────────────────────────────────────────────
# Section distribution
# ─────────────────────────────────────────────────────────────────────────────

class TestDistributeSections:
    def test_total_slots_equals_n(self):
        for n in (5, 10, 16, 24, 40):
            slots = distribute_sections(n, 120)
            assert len(slots) == n, f"n={n}: got {len(slots)} slots"

    def test_section_order_preserved(self):
        """Intro must come before climax, outro must be last."""
        slots = distribute_sections(16, 120)
        names = [s.section_name for s in slots]
        assert names.index("intro") < names.index("climax")
        assert names[-1] == "outro"

    def test_bpm_targets_scaled_to_base(self):
        slots_80  = distribute_sections(16, 80)
        slots_140 = distribute_sections(16, 140)
        avg_80  = sum(s.bpm_target for s in slots_80)  / len(slots_80)
        avg_140 = sum(s.bpm_target for s in slots_140) / len(slots_140)
        assert avg_140 > avg_80

    def test_bpm_range_clamps_targets(self):
        """All BPM targets must fall within the requested range."""
        slots = distribute_sections(16, 128, bpm_range=(120, 140))
        for s in slots:
            assert 120 <= s.bpm_target <= 140, \
                f"section {s.section_name}: bpm_target {s.bpm_target} outside [120,140]"

    def test_climax_slots_are_hit_slots(self):
        slots = distribute_sections(16, 120)
        climax_slots = [s for s in slots if s.section_name == "climax"]
        assert any(s.is_hit_slot for s in climax_slots)

    def test_intro_slots_not_hit_slots(self):
        slots = distribute_sections(16, 120)
        intro_slots = [s for s in slots if s.section_name == "intro"]
        assert not any(s.is_hit_slot for s in intro_slots)

    def test_positions_are_contiguous(self):
        slots = distribute_sections(16, 120)
        assert [s.position for s in slots] == list(range(16))

    def test_large_set(self):
        assert len(distribute_sections(47, 120)) == 47

    def test_energy_ranges_are_reasonable(self):
        for s in distribute_sections(20, 120):
            assert s.energy_min < s.energy_max

    def test_no_bpm_range_uses_full_pool(self):
        """Without bpm_range, targets should span wide (not artificially clamped)."""
        slots = distribute_sections(16, 120)
        targets = [s.bpm_target for s in slots]
        # intro should be below base, climax above
        intro_bpm  = next(s.bpm_target for s in slots if s.section_name == "intro")
        climax_bpm = next(s.bpm_target for s in slots if s.section_name == "climax")
        assert intro_bpm < climax_bpm


# ─────────────────────────────────────────────────────────────────────────────
# Track role classification
# ─────────────────────────────────────────────────────────────────────────────

class TestClassifyTrackRole:
    def test_opener(self):
        t = _track(bpm=100, energy=45)   # low BPM, low energy
        pool = build_pool([t], {t["id"]: _cache_ok(100, "8A", 45)})
        assert classify_track_role(pool[0], 120) == "opener"

    def test_climax(self):
        t = _track(bpm=135, energy=90, popularity=75)
        pool = build_pool([t], {t["id"]: _cache_ok(135, "8A", 90)})
        assert classify_track_role(pool[0], 120) == "climax"

    def test_anthem(self):
        t = _track(bpm=125, energy=75, popularity=60)
        pool = build_pool([t], {t["id"]: _cache_ok(125, "8A", 75)})
        assert classify_track_role(pool[0], 120) == "anthem"

    def test_emotional(self):
        t = _track(bpm=108, energy=55)
        pool = build_pool([t], {t["id"]: _cache_ok(108, "8A", 55)})
        role = classify_track_role(pool[0], 120)
        assert role == "emotional"

    def test_closer(self):
        # ratio = 111/120 = 0.925 — moderately below base, low energy → closer
        t = _track(bpm=111, energy=50)
        pool = build_pool([t], {t["id"]: _cache_ok(111, "8A", 50)})
        role = classify_track_role(pool[0], 120)
        assert role in ("closer", "emotional")

    def test_returns_string(self):
        t = _track()
        pool = build_pool([t], {t["id"]: _cache_ok()})
        role = classify_track_role(pool[0], 120)
        assert isinstance(role, str) and len(role) > 0


# ─────────────────────────────────────────────────────────────────────────────
# Compute base BPM
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeBaseBpm:
    def test_returns_median(self):
        tracks = _make_library(10, base_bpm=120)
        pool   = build_pool(tracks, _make_cache(tracks))
        bpm    = compute_base_bpm(pool)
        assert 110 <= bpm <= 130

    def test_empty_pool_returns_120(self):
        assert compute_base_bpm([]) == 120

    def test_single_track(self):
        t    = _track(bpm=100)
        pool = build_pool([t], {t["id"]: _cache_ok(100)})
        assert compute_base_bpm(pool) == 100


# ─────────────────────────────────────────────────────────────────────────────
# Genre classification
# ─────────────────────────────────────────────────────────────────────────────

class TestClassifyGenre:
    def test_electronic(self):
        assert classify_genre(["electronic", "deep house"]) == "electronic"

    def test_hip_hop(self):
        assert classify_genre(["rap", "trap"]) == "hip_hop"

    def test_latin(self):
        assert classify_genre(["latin", "reggaeton"]) == "latin"

    def test_empty_returns_none(self):
        assert classify_genre([]) is None

    def test_unknown_returns_none(self):
        assert classify_genre(["xyzzy"]) is None

    def test_mixed_picks_best(self):
        assert classify_genre(["electronic", "deep house", "rap"]) == "electronic"


class TestTagsToClusters:
    def test_separator_variants_normalise(self):
        # "deep house" / "deep-house" / "deephouse" all collapse to electronic
        assert tags_to_clusters(["deep house"]) == {"electronic"}
        assert tags_to_clusters(["deep-house"]) == {"electronic"}
        assert tags_to_clusters(["deephouse"]) == {"electronic"}

    def test_multi_membership(self):
        assert tags_to_clusters(["indie pop"]) == {"indie", "pop"}
        assert tags_to_clusters(["neo-soul"]) == {"hip_hop", "jazz"}

    def test_combines_multiple_tags(self):
        assert tags_to_clusters(["reggaeton", "techno"]) == {"latin", "electronic"}

    def test_no_substring_false_positives(self):
        # exact-normalised matching: "dub" must not match "dubstep" and vice-versa
        assert tags_to_clusters(["dub"]) == {"world"}
        assert tags_to_clusters(["dubstep"]) == {"electronic"}
        # "trap" must not be matched by a "rap" rule (both happen to be hip_hop,
        # but each is matched exactly, not by substring)
        assert tags_to_clusters(["trap"]) == {"hip_hop"}

    def test_unknown_and_noise(self):
        assert tags_to_clusters(["xyzzy"]) == set()
        assert tags_to_clusters(["80s", "shake that thing"]) == set()

    def test_empty(self):
        assert tags_to_clusters([]) == set()


class TestGenreOptions:
    def test_counts_and_min_threshold(self):
        lib = [_track(f"t{i}", genres=["house"]) for i in range(10)]
        lib += [_track("r1", genres=["reggae"])]   # below threshold → hidden
        cache = {t["id"]: _cache_ok() for t in lib}
        opts = genre_options(lib, cache, min_tracks=8)
        values = {o["value"] for o in opts}
        assert "electronic" in values
        assert "world" not in values
        elec = next(o for o in opts if o["value"] == "electronic")
        assert elec["count"] == 10 and elec["label"]

    def test_combines_spotify_and_lastfm_tags(self):
        lib = [_track("t1", genres=["pop"])]
        cache = {"t1": {**_cache_ok(), "track_genres": ["reggaeton"]}}
        clusters = {o["value"] for o in genre_options(lib, cache, min_tracks=1)}
        assert "pop" in clusters and "latin" in clusters

    def test_sorted_by_count_desc(self):
        lib = [_track(f"e{i}", genres=["techno"]) for i in range(5)]
        lib += [_track(f"l{i}", genres=["cumbia"]) for i in range(9)]
        cache = {t["id"]: _cache_ok() for t in lib}
        opts = genre_options(lib, cache, min_tracks=1)
        counts = [o["count"] for o in opts]
        assert counts == sorted(counts, reverse=True)


# ─────────────────────────────────────────────────────────────────────────────
# Pool building
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildPool:
    def test_excludes_error_entries(self):
        lib = [_track("t1"), _track("t2")]
        cache = {"t1": _cache_ok(), "t2": {"error": "not found: 'x'"}}
        assert len(build_pool(lib, cache)) == 1

    def test_excludes_missing_from_cache(self):
        lib = [_track("t1"), _track("t2")]
        assert len(build_pool(lib, {"t1": _cache_ok()})) == 1

    def test_excludes_zero_bpm(self):
        lib = [_track("t1")]
        assert build_pool(lib, {"t1": _cache_ok(bpm=0)}) == []

    def test_uses_cache_bpm_not_library(self):
        lib = [_track("t1", bpm=100)]
        pool = build_pool(lib, {"t1": _cache_ok(bpm=130)})
        assert pool[0]["bpm"] == 130

    def test_artists_always_a_list(self):
        lib = [_track("t1", artists="A, B")]
        pool = build_pool(lib, {"t1": _cache_ok()})
        assert isinstance(pool[0]["artists_list"], list)
        assert len(pool[0]["artists_list"]) == 2

    def test_genre_cluster_assigned(self):
        lib = [_track("t1", genres=["electronic"])]
        pool = build_pool(lib, {"t1": _cache_ok()})
        assert pool[0]["genre_cluster"] == "electronic"

    def test_empty_inputs(self):
        assert build_pool([], {}) == []
        assert build_pool([_track()], {}) == []


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────

class TestScoreCandidate:
    def _pool_track(self, bpm=128, camelot="8A", energy=70, pop=50):
        t = _track(bpm=bpm, camelot=camelot, energy=energy, popularity=pop)
        return build_pool([t], {t["id"]: _cache_ok(bpm, camelot, energy)})[0]

    def test_same_key_beats_incompatible(self):
        prev = self._pool_track(camelot="8A")
        s_same = score_candidate(self._pool_track(camelot="8A"), prev, 0.7, 128, False, set())
        s_bad  = score_candidate(self._pool_track(camelot="4B"), prev, 0.7, 128, False, set())
        assert s_same > s_bad

    def test_close_bpm_beats_far(self):
        prev  = self._pool_track(bpm=128)
        s_close = score_candidate(self._pool_track(bpm=130, camelot="8A"), prev, 0.7, 128, False, set())
        s_far   = score_candidate(self._pool_track(bpm=160, camelot="8A"), prev, 0.7, 128, False, set())
        assert s_close > s_far

    def test_same_artist_penalty(self):
        t = build_pool([_track("tx", artists="DJ X", popularity=80)],
                       {"tx": _cache_ok()})[0]
        s_no  = score_candidate(t, None, 0.7, 128, False, set())
        s_yes = score_candidate(t, None, 0.7, 128, False, {"dj x"})
        assert s_no > s_yes

    def test_hit_slot_favours_popular(self):
        popular = self._pool_track(pop=90)
        obscure = self._pool_track(pop=10)
        assert score_candidate(popular, None, 0.7, 128, True, set()) > \
               score_candidate(obscure, None, 0.7, 128, True, set())

    def test_score_non_negative(self):
        prev = self._pool_track()
        cand = self._pool_track(bpm=200, camelot="4B", energy=5)
        assert score_candidate(cand, prev, 1.0, 150, False, {"artist"}) >= 0.0

    def test_bpm_on_target_beats_off_target(self):
        prev     = self._pool_track(bpm=120)
        on_t     = self._pool_track(bpm=120)
        off_t    = self._pool_track(bpm=160)
        assert score_candidate(on_t, prev, 0.7, 120, False, set()) > \
               score_candidate(off_t, prev, 0.7, 120, False, set())


# ─────────────────────────────────────────────────────────────────────────────
# Core generator
# ─────────────────────────────────────────────────────────────────────────────

class TestGenerate:
    def setup_method(self):
        self.library = _make_library(40)
        self.cache   = _make_cache(self.library)

    def test_returns_tracks(self):
        r = generate(self.library, self.cache, duration_min=40)
        assert r["track_count"] > 0

    def test_section_info_on_every_track(self):
        r = generate(self.library, self.cache, duration_min=60)
        for t in r["tracks"]:
            assert "section"       in t and t["section"]
            assert "section_label" in t and t["section_label"]

    def test_sections_summary_in_result(self):
        r = generate(self.library, self.cache, duration_min=60)
        assert "sections" in r
        assert len(r["sections"]) >= 5

    def test_sections_summary_has_required_fields(self):
        r = generate(self.library, self.cache, duration_min=60)
        for s in r["sections"]:
            assert "name" in s and "label" in s and "emoji" in s
            assert "start_position" in s and "count" in s

    def test_narrative_order(self):
        r = generate(self.library, self.cache, duration_min=60)
        section_names = [t["section"] for t in r["tracks"]]
        intro_pos  = next((i for i, s in enumerate(section_names) if s == "intro"),  None)
        climax_pos = next((i for i, s in enumerate(section_names) if s == "climax"), None)
        outro_pos  = next((i for i, s in enumerate(section_names) if s == "outro"),  None)
        if intro_pos is not None and climax_pos is not None:
            assert intro_pos < climax_pos
        if climax_pos is not None and outro_pos is not None:
            assert climax_pos < outro_pos

    def test_has_valley(self):
        """mid_journey energy should be lower than climax energy on average."""
        r = generate(self.library, self.cache, duration_min=90)
        by_section = {}
        for t in r["tracks"]:
            by_section.setdefault(t["section"], []).append(t["energy"])
        if "mid_journey" in by_section and "climax" in by_section:
            avg_valley = sum(by_section["mid_journey"]) / len(by_section["mid_journey"])
            avg_climax = sum(by_section["climax"])      / len(by_section["climax"])
            assert avg_valley < avg_climax

    def test_no_duplicate_tracks(self):
        r = generate(self.library, self.cache, duration_min=60)
        ids = [t["id"] for t in r["tracks"]]
        assert len(ids) == len(set(ids))

    def test_approximate_duration(self):
        r = generate(self.library, self.cache, duration_min=60)
        assert 25 <= r["total_duration_ms"] / 60_000 <= 90

    def test_bpm_range_filter_respected(self):
        """Tracks should come from the requested BPM range when enough are available."""
        r = generate(self.library, self.cache, duration_min=30, bpm_range=(108, 120))
        # BPM range is applied so base_bpm should reflect it
        if not r.get("warnings"):
            for t in r["tracks"]:
                assert 95 <= t["bpm"] <= 135, f"BPM {t['bpm']} far outside requested range"

    def test_bpm_range_warning_when_insufficient(self):
        r = generate(self.library, self.cache, duration_min=60, bpm_range=(1, 2))
        assert any("BPM" in w for w in r.get("warnings", []))

    def test_empty_cache_returns_error(self):
        r = generate(self.library, {})
        assert "error" in r

    def test_genre_filter_warning_when_insufficient(self):
        r = generate(self.library, self.cache, genre_filter="latin")
        if r.get("warnings"):
            assert any("latin" in w for w in r["warnings"])

    def test_track_count_matches_tracks_list(self):
        r = generate(self.library, self.cache, duration_min=60)
        assert r["track_count"] == len(r["tracks"])

    def test_result_has_required_fields(self):
        r = generate(self.library, self.cache, duration_min=40)
        for key in ("tracks", "total_duration_ms", "track_count", "warnings", "sections"):
            assert key in r

    def test_bpm_transitions_mostly_smooth(self):
        r = generate(self.library, self.cache, duration_min=60)
        tracks = r["tracks"]
        if len(tracks) < 2:
            return
        smooth = sum(1 for i in range(1, len(tracks))
                     if abs(tracks[i]["bpm"] - tracks[i-1]["bpm"]) <= 18)
        assert smooth / (len(tracks) - 1) >= 0.65

    def test_no_bpm_range_uses_full_library(self):
        """Without bpm_range, all analyzed tracks are candidates."""
        r = generate(self.library, self.cache, duration_min=60)
        assert r["track_count"] > 0
        assert not any("BPM" in w for w in r.get("warnings", []))

    def test_bpm_min_only_always_enforced(self):
        """bpm_range with only a minimum (max=9999) must exclude sub-120 tracks
        even when fewer than 8 tracks qualify — the 'too few tracks' fallback
        must not silently drop the filter and return out-of-range tracks."""
        # Build a small library where only 3 tracks are >= 120 BPM.
        low_tracks  = [_track(tid=f"lo{i}", title=f"Low {i}", bpm=90 + i,
                               duration_ms=240_000) for i in range(10)]
        high_tracks = [_track(tid=f"hi{i}", title=f"High {i}", bpm=125 + i,
                               duration_ms=240_000) for i in range(3)]
        library = low_tracks + high_tracks
        cache   = {t["id"]: _cache_ok(t["bpm"], t["camelot"], t["energy"]) for t in library}

        r = generate(library, cache, duration_min=20, bpm_range=(120, 9999))

        out_of_range = [t for t in r["tracks"] if t["bpm"] < 120]
        assert out_of_range == [], (
            f"BPM filter (min=120) was ignored: got tracks with BPM "
            f"{[t['bpm'] for t in out_of_range]}"
        )

    def test_bpm_min_only_with_genre_always_enforced(self):
        """Reproduces the reported bug: genre=electronica + bpm_min=120 returned
        sub-120 tracks. The BPM floor must hold even when the genre-filtered pool
        has fewer than 8 qualifying tracks."""
        low  = [_track(tid=f"elo{i}", bpm=90 + i, duration_ms=240_000,
                        genres=["electronica"]) for i in range(8)]
        high = [_track(tid=f"ehi{i}", bpm=122 + i, duration_ms=240_000,
                        genres=["electronica"]) for i in range(4)]
        library = low + high
        cache   = {t["id"]: _cache_ok(t["bpm"], t["camelot"], t["energy"]) for t in library}

        r = generate(library, cache, duration_min=20,
                     genre_filter="electronica", bpm_range=(120, 9999))

        out_of_range = [t for t in r["tracks"] if t["bpm"] < 120]
        assert out_of_range == [], (
            f"BPM filter ignored with genre filter active: got BPMs "
            f"{[t['bpm'] for t in out_of_range]}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────────

class TestPersistence:
    def setup_method(self, _):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        self._tmp_path = Path(self._tmp.name)
        self._patcher = patch("playlist_engine.PLAYLISTS_FILE", self._tmp_path)
        self._patcher.start()

    def teardown_method(self, _):
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
        lib    = _make_library(25)
        cache  = _make_cache(lib)
        result = generate(lib, cache, duration_min=30)
        pl = create_playlist(result, {"energy_profile": "peak_time"}, name="My Set")
        assert pl["id"] in load_playlists()
        assert load_playlists()[pl["id"]]["name"] == "My Set"

    def test_create_playlist_includes_section_info(self):
        lib    = _make_library(25)
        cache  = _make_cache(lib)
        result = generate(lib, cache, duration_min=30)
        pl = create_playlist(result, {})
        for t in pl["tracks"]:
            assert "section" in t

    def test_create_playlist_includes_sections_summary(self):
        lib    = _make_library(25)
        cache  = _make_cache(lib)
        result = generate(lib, cache, duration_min=30)
        pl = create_playlist(result, {})
        assert "sections" in pl
        assert len(pl["sections"]) >= 3

    def test_create_playlist_auto_name_with_bpm_range(self):
        lib = _make_library(25); cache = _make_cache(lib)
        pl  = create_playlist(
            generate(lib, cache, duration_min=30, bpm_range=(120, 135)),
            {"bpm_range": [120, 135]},
        )
        assert "120" in pl["name"] and "135" in pl["name"]

    def test_create_playlist_auto_name_without_bpm_range(self):
        lib = _make_library(25); cache = _make_cache(lib)
        pl  = create_playlist(generate(lib, cache, duration_min=30), {})
        assert "DJ Set" in pl["name"]

    def test_delete_playlist(self):
        lib = _make_library(25); cache = _make_cache(lib)
        pl  = create_playlist(generate(lib, cache, duration_min=30), {})
        pid = pl["id"]
        assert delete_playlist(pid) is True
        assert pid not in load_playlists()

    def test_delete_nonexistent_returns_false(self):
        assert delete_playlist("pl_doesnotexist") is False

    def test_multiple_playlists_coexist(self):
        lib = _make_library(25); cache = _make_cache(lib)
        result = generate(lib, cache, duration_min=30)
        p1 = create_playlist(result, {}, name="Set 1")
        p2 = create_playlist(result, {}, name="Set 2")
        stored = load_playlists()
        assert p1["id"] in stored and p2["id"] in stored
        assert len(stored) == 2
