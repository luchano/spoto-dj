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
                     "--lyrics-to-file", "--lyrics-to-metadata",
                     "--md-save-lyrics", "--album-art-jpg-file"):
            assert cmd[cmd.index(flag) + 1] == "False", flag
        # the deprecated --download-lyrics flag must NOT be present
        assert "--download-lyrics" not in cmd

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

    def test_unavailable_track_returns_sentinel(self, audio_dir, fake_zotify, logged_in):
        """zotify exits 0 and prints 'SKIPPING: "…" (TRACK IS UNAVAILABLE)'
        for delisted/region-blocked tracks — that must surface as the
        permanent UNAVAILABLE sentinel, not a retryable failure."""
        out = '套 SKIPPING:  "Acid Pauli - Anan" (TRACK IS UNAVAILABLE)'
        ok = subprocess.CompletedProcess([], 0, stdout=out, stderr="")
        with patch.object(ea.subprocess, "run", return_value=ok):
            assert ea.download_track(TRACK_URL, TRACK_ID) is ea.UNAVAILABLE

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

class TestBanRiskWatchdog:
    @pytest.fixture(autouse=True)
    def reset_state(self, monkeypatch):
        monkeypatch.setattr(ea, "_rate_state", {"level": 0, "signals": 0})
        monkeypatch.setattr(ea, "ZOTIFY_RATE_LIMITER", "0.1")

    def test_no_signal_keeps_base(self):
        ea._check_ban_signals("Downloaded 'Song' in 24s", "t1")
        assert ea.current_rate_limiter() == "0.1"
        assert ea._rate_state["signals"] == 0

    def test_audio_key_escalates_to_035(self):
        ea._check_ban_signals("Failed fetching audio key! MAY BE CAUSED BY RATE LIMITS", "t1")
        assert ea.current_rate_limiter() == "0.35"

    def test_second_signal_escalates_to_1(self):
        ea._check_ban_signals("Failed fetching audio key!", "t1")
        ea._check_ban_signals("API_ERROR 429", "t2")
        assert ea.current_rate_limiter() == "1"
        assert ea._rate_state["signals"] == 2

    def test_third_signal_stays_at_max(self):
        for i in range(3):
            ea._check_ban_signals("too many requests", f"t{i}")
        assert ea.current_rate_limiter() == "1"

    def test_never_faster_than_base(self, monkeypatch):
        """If the .env base is SLOWER than an escalation tier, keep the base."""
        monkeypatch.setattr(ea, "ZOTIFY_RATE_LIMITER", "1.0")
        ea._check_ban_signals("audio key denial", "t1")
        assert float(ea.current_rate_limiter()) >= 1.0

    def test_429_in_file_size_is_not_a_signal(self):
        ea._check_ban_signals("downloaded 14290KB at 4290KB/s", "t1")
        assert ea._rate_state["signals"] == 0

    def test_command_uses_effective_rate(self):
        ea._check_ban_signals("rate limit hit", "t1")
        cmd = ea.build_zotify_command("https://open.spotify.com/track/x")
        assert cmd[cmd.index("--download-rate-limiter") + 1] == "0.35"

    def test_info_snapshot(self):
        ea._check_ban_signals("audio key", "t1")
        info = ea.rate_limiter_info()
        assert info["base"] == "0.1"
        assert info["effective"] == "0.35"
        assert info["escalation_level"] == 1
        assert info["signals"] == 1


class TestDownloadTimeout:
    def test_short_track_uses_floor(self, monkeypatch):
        monkeypatch.setattr(ea, "ZOTIFY_RATE_LIMITER", "0.35")
        assert ea._download_timeout(240_000) == ea.ZOTIFY_TIMEOUT  # 4-min track

    def test_45min_track_gets_enough_time(self, monkeypatch):
        """LCD Soundsystem's '45:33' (45.5 min) at rate 0.35 needs ~16 min of
        download; the old fixed 900 s cap killed it on every run."""
        monkeypatch.setattr(ea, "ZOTIFY_RATE_LIMITER", "0.35")
        t = ea._download_timeout(45 * 60 * 1000 + 33_000)
        assert t > (45 * 60 + 33) * 0.35        # more than the expected time
        assert t >= 1500                          # comfortably above 900

    def test_unknown_duration_uses_floor(self):
        assert ea._download_timeout(0) == ea.ZOTIFY_TIMEOUT

    def test_bad_rate_limiter_string_does_not_crash(self, monkeypatch):
        monkeypatch.setattr(ea, "ZOTIFY_RATE_LIMITER", "not-a-number")
        assert ea._download_timeout(300_000) >= ea.ZOTIFY_TIMEOUT


class TestMaybeExcerpt:
    def test_short_track_uses_original(self, tmp_path):
        f = tmp_path / "song.ogg"; f.write_bytes(b"x")
        path, cleanup = ea._maybe_excerpt(f, 4 * 60 * 1000)   # 4 min
        assert path == f and cleanup is None

    def test_unknown_duration_uses_original(self, tmp_path):
        f = tmp_path / "song.ogg"; f.write_bytes(b"x")
        path, cleanup = ea._maybe_excerpt(f, 0)
        assert path == f and cleanup is None

    def test_long_track_extracts_centered_excerpt(self, tmp_path):
        f = tmp_path / "mix.ogg"; f.write_bytes(b"x")
        captured = {}
        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            Path(cmd[-1]).write_bytes(b"wav-data")   # ffmpeg "wrote" the excerpt
            return subprocess.CompletedProcess(cmd, 0)
        with patch.object(ea.subprocess, "run", side_effect=fake_run):
            path, cleanup = ea._maybe_excerpt(f, 76 * 60 * 1000)   # 76 min
        try:
            assert path != f and cleanup == path
            cmd = captured["cmd"]
            # centered: starts at (76*60/2 - 300) = 1980 s, lasts 600 s
            assert cmd[cmd.index("-ss") + 1] == "1980"
            assert cmd[cmd.index("-t") + 1] == "600"
        finally:
            if cleanup: cleanup.unlink(missing_ok=True)

    def test_ffmpeg_failure_falls_back_to_original(self, tmp_path):
        f = tmp_path / "mix.ogg"; f.write_bytes(b"x")
        fail = subprocess.CompletedProcess([], 1)
        with patch.object(ea.subprocess, "run", return_value=fail):
            path, cleanup = ea._maybe_excerpt(f, 76 * 60 * 1000)
        assert path == f and cleanup is None


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

    def test_long_track_analyzed_via_excerpt(self, audio_dir, monkeypatch, tmp_path):
        """76-min continuous mixes crash essentia's rhythm extractor — they
        must be analyzed through the mid-track excerpt, flagged as such, and
        the temp file cleaned up."""
        monkeypatch.setattr(ea, "ZOTIFY_PACING_JITTER", 0.0)
        real = audio_dir / f"{TRACK_ID}.ogg"
        excerpt = tmp_path / "excerpt.wav"; excerpt.write_bytes(b"wav")
        analyzed_paths = []
        def fake_analyze(path):
            analyzed_paths.append(path)
            return {"bpm": 123.0}
        with patch.object(ea, "download_track", return_value=real), \
             patch.object(ea, "_maybe_excerpt", return_value=(excerpt, excerpt)), \
             patch.object(ea, "analyze_audio", side_effect=fake_analyze), \
             patch.object(ea, "analyze_style", side_effect=lambda p: {"track_genres": [], "genre_affinity": {}, "vocalness": 5}):
            tid, result = self._run(ea.analyze_track_full(
                TRACK_ID, TRACK_URL, "Mix", "DJ", asyncio.Semaphore(1),
                duration_ms=76 * 60 * 1000,
            ))
        assert result["analysis_excerpt"] is True
        assert analyzed_paths == [excerpt]          # BPM ran on the excerpt
        assert not excerpt.exists()                  # temp cleaned up

    def test_unavailable_becomes_permanent_not_found_error(self, audio_dir, monkeypatch):
        """The 30-tracks-reanalyzed-on-every-refresh bug: unavailable tracks
        must produce a 'not found' (PERMANENT → cached) error, not the
        transient 'audio download failed'."""
        monkeypatch.setattr(ea, "ZOTIFY_PACING_JITTER", 0.0)
        with patch.object(ea, "download_track", return_value=ea.UNAVAILABLE):
            tid, result = self._run(ea.analyze_track_full(
                TRACK_ID, TRACK_URL, "Anan", "Acid Pauli", asyncio.Semaphore(1),
            ))
        assert result["error"].startswith("not found")
        assert "unavailable" in result["error"]

    def test_on_stage_fires_downloading_then_analyzing(self, audio_dir, monkeypatch):
        monkeypatch.setattr(ea, "ZOTIFY_PACING_JITTER", 0.0)
        fake_path = audio_dir / f"{TRACK_ID}.ogg"
        stages = []
        with patch.object(ea, "download_track", return_value=fake_path), \
             patch.object(ea, "analyze_audio", return_value={"bpm": 120}), \
             patch.object(ea, "analyze_style", return_value={"track_genres": [], "genre_affinity": {}, "vocalness": 10}):
            self._run(ea.analyze_track_full(
                TRACK_ID, TRACK_URL, "Song", "Artist", asyncio.Semaphore(1),
                on_stage=stages.append,
            ))
        assert stages == ["downloading", "analyzing"]

    def test_on_stage_exception_does_not_break_analysis(self, audio_dir, monkeypatch):
        monkeypatch.setattr(ea, "ZOTIFY_PACING_JITTER", 0.0)
        fake_path = audio_dir / f"{TRACK_ID}.ogg"
        def boom(_stage): raise RuntimeError("ui broke")
        with patch.object(ea, "download_track", return_value=fake_path), \
             patch.object(ea, "analyze_audio", return_value={"bpm": 120}), \
             patch.object(ea, "analyze_style", return_value={"track_genres": [], "genre_affinity": {}, "vocalness": 10}):
            tid, result = self._run(ea.analyze_track_full(
                TRACK_ID, TRACK_URL, "Song", "Artist", asyncio.Semaphore(1),
                on_stage=boom,
            ))
        assert "error" not in result

    def test_success_merges_analysis_and_genres(self, audio_dir, monkeypatch):
        monkeypatch.setattr(ea, "ZOTIFY_PACING_JITTER", 0.0)
        fake_path = audio_dir / f"{TRACK_ID}.ogg"
        analysis = {"bpm": 122.4, "key": "Bb maj", "camelot": "6B", "energy": 80}
        with patch.object(ea, "download_track", return_value=fake_path), \
             patch.object(ea, "analyze_audio", return_value=dict(analysis)), \
             patch.object(ea, "analyze_style", return_value={"track_genres": ["House", "Electronic"], "genre_affinity": {"electronic": 1.5}, "vocalness": 25}):
            tid, result = self._run(ea.analyze_track_full(
                TRACK_ID, TRACK_URL, "Song", "Artist", asyncio.Semaphore(1),
            ))
        assert result["bpm"] == 122.4
        assert result["camelot"] == "6B"
        assert result["source"] == "local"
        assert result["track_genres"] == ["House", "Electronic"]
        assert result["genre_affinity"] == {"electronic": 1.5}
        assert result["vocalness"] == 25


# ─────────────────────────────────────────────────────────────────────────────
# Camelot conversion
# ─────────────────────────────────────────────────────────────────────────────

class TestGenreLabels:
    def test_loader_returns_400_from_json(self, tmp_path, monkeypatch):
        import json
        classes = [f"G{i}" for i in range(400)]
        (tmp_path / ea._GENRE_LABELS_JSON).write_text(json.dumps({"classes": classes}))
        monkeypatch.setattr(ea, "MODELS_DIR", tmp_path)
        monkeypatch.setattr(ea, "_GENRE_LABELS_CACHE", None)
        assert ea._load_genre_labels() == classes

    def test_missing_json_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ea, "MODELS_DIR", tmp_path)  # empty dir
        monkeypatch.setattr(ea, "_GENRE_LABELS_CACHE", None)
        assert ea._load_genre_labels() == []

    def test_real_label_map_is_400_and_ordered(self):
        """Guards the bug we hit: a short/mis-ordered label list silently maps
        every prediction to the wrong genre. The real metadata must be 400
        classes in the canonical Discogs order."""
        from pathlib import Path
        import json
        p = Path(".essentia_models") / ea._GENRE_LABELS_JSON
        if not p.exists():
            pytest.skip("genre labels JSON not downloaded")
        classes = json.loads(p.read_text())["classes"]
        assert len(classes) == 400
        assert classes[0] == "Blues---Boogie Woogie"
        assert classes[-1] == "Stage & Screen---Theme"


class TestCleanGenreLabels:
    def test_splits_parent_and_child(self):
        out = ea._clean_genre_labels(["Electronic---Glitch"])
        assert out == ["Glitch", "Electronic"]

    def test_subgenres_first_then_parents_deduped(self):
        out = ea._clean_genre_labels(["Electronic---Glitch", "Electronic---Vaporwave"])
        assert out == ["Glitch", "Vaporwave", "Electronic"]

    def test_drops_non_music_noise(self):
        out = ea._clean_genre_labels(["Non-Music---Spoken Word", "Electronic---Techno"])
        assert out == ["Techno", "Electronic"]
        assert "Spoken Word" not in out

    def test_case_insensitive_dedup(self):
        out = ea._clean_genre_labels(["Rock---Rock", "Rock---Indie Rock"])
        assert out == ["Rock", "Indie Rock"]

    def test_cluster_matchable(self):
        """The cleaned tags must match playlist_engine genre clusters — the raw
        'Parent---Child' string matches none."""
        from playlist_engine import tags_to_clusters
        assert tags_to_clusters(["Electronic---Glitch"]) == set()
        assert "electronic" in tags_to_clusters(ea._clean_genre_labels(["Electronic---Glitch"]))

    def test_compound_parents_split_into_components(self):
        assert ea._clean_genre_labels(["Folk, World, & Country---African"]) == \
            ["African", "Folk", "World", "Country"]
        assert ea._clean_genre_labels(["Funk / Soul---Boogie"]) == ["Boogie", "Funk", "Soul"]

    def test_every_discogs_label_reaches_a_cluster(self):
        """End-to-end vocabulary audit: every non-noise Discogs-400 label must
        land in at least one playlist genre cluster — otherwise genre-filtered
        DJ sets silently ignore those tracks (the bug behind the empty genre
        dropdown)."""
        import json
        from pathlib import Path
        from playlist_engine import tags_to_clusters
        p = Path(".essentia_models") / ea._GENRE_LABELS_JSON
        if not p.exists():
            pytest.skip("genre labels JSON not downloaded")
        classes = json.loads(p.read_text())["classes"]
        orphans = [
            label for label in classes
            if ea._clean_genre_labels([label])
            and not tags_to_clusters(ea._clean_genre_labels([label]))
        ]
        assert orphans == [], f"{len(orphans)} labels match no cluster: {orphans[:8]}"


class TestEnergyFromLufs:
    def test_monotonic(self):
        vals = [ea._energy_from_lufs(l) for l in (-40, -30, -20, -14, -10, -6, 0)]
        assert vals == sorted(vals), vals

    def test_clamped_0_100(self):
        assert ea._energy_from_lufs(-100) == 0
        assert ea._energy_from_lufs(10) == 100

    def test_realistic_tracks_do_not_saturate(self):
        """The whole point of the recalibration: a normal loud master
        (~-10 LUFS) must land well below 100, unlike the old RMS*450."""
        assert ea._energy_from_lufs(-10) < 95
        assert ea._energy_from_lufs(-14) < ea._energy_from_lufs(-8)

    def test_silence_is_zero(self):
        assert ea._energy_from_lufs(-70) == 0


class TestIntegratedLufsRobustness:
    """A non-finite LUFS would crash the energy path (round(nan) → ValueError),
    so _integrated_lufs must always return a finite number."""

    class _FakeES:
        def __init__(self, ebur_val):
            self._ebur_val = ebur_val
        def LoudnessEBUR128(self):
            v = self._ebur_val
            return lambda stereo: (None, None, v, None)

    def test_nan_from_ebur128_falls_to_floor(self):
        import math
        es = self._FakeES(float("nan"))
        out = ea._integrated_lufs([0.0, 0.1, 0.2], es)
        assert math.isfinite(out)
        assert out == ea._LUFS_FLOOR

    def test_neg_inf_from_ebur128_falls_to_floor(self):
        import math
        es = self._FakeES(float("-inf"))
        out = ea._integrated_lufs([0.0, 0.1], es)
        assert math.isfinite(out)

    def test_finite_value_passes_through(self):
        es = self._FakeES(-12.3)
        assert ea._integrated_lufs([0.1], es) == -12.3


class TestResolveTempoOctave:
    def test_agreement_keeps_primary(self):
        # both estimators agree → no correction
        assert ea._resolve_tempo_octave(128.0, 128.4) == 128.0
        assert ea._resolve_tempo_octave(89.0, 89.1) == 89.0

    def test_primary_doubled_is_halved(self):
        # the Mi Amor case: Rhythm=155.6, Percival=78.9 → 77.8
        assert ea._resolve_tempo_octave(155.6, 78.9) == 155.6 / 2

    def test_primary_halved_is_doubled(self):
        assert ea._resolve_tempo_octave(80.0, 160.0) == 160.0

    def test_no_percival_keeps_primary(self):
        assert ea._resolve_tempo_octave(155.6, 0.0) == 155.6

    def test_non_2x_disagreement_keeps_primary(self):
        # a 1.5x disagreement is not a clean octave → trust primary
        assert ea._resolve_tempo_octave(150.0, 100.0) == 150.0


class TestDanceabilityScore:
    def test_monotonic_and_clamped(self):
        vals = [ea._danceability_score(x) for x in (0.0, 0.8, 1.5, 2.3, 3.0)]
        assert vals[0] == 0 and vals[-1] == 100
        assert vals == sorted(vals)

    def test_spreads_typical_range(self):
        """Typical music raw danceability (~1.2–1.7) must NOT collapse into a
        narrow mid band — the whole reason we dropped the ÷3.0 mapping."""
        low, high = ea._danceability_score(1.2), ea._danceability_score(1.7)
        assert high - low >= 25  # meaningfully separated


class TestAnalyzeAudioShape:
    """analyze_audio must return the new fields with sane types/ranges.
    Uses a real downloaded .ogg if present, else skips (no network)."""

    def _sample(self):
        from pathlib import Path
        files = sorted(Path(".audio_files").glob("*.ogg")) if Path(".audio_files").exists() else []
        return files[0] if files else None

    def test_returns_new_fields_in_range(self):
        sample = self._sample()
        if sample is None:
            pytest.skip("no downloaded .ogg available")
        r = ea.analyze_audio(sample)
        for k in ("bpm", "key", "camelot", "energy", "danceability", "loudness"):
            assert k in r, k
        assert 0 <= r["energy"] <= 100
        assert 0 <= r["danceability"] <= 100
        assert -60.0 <= r["loudness"] <= 0.0
        assert r["bpm"] > 0


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
