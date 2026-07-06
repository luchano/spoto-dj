"""
Local audio pipeline: zotify download + essentia analysis + genre classification.

Replaces GetSongBPM API and Last.fm for BPM, key, energy and genre detection.

Flow per track:
  1. download_track()    — zotify (Googolplexed0 fork) downloads the native
                           96 kbps Ogg Vorbis stream directly from Spotify
  2. analyze_audio()     — essentia extracts BPM, key, camelot, energy
  3. classify_genre()    — essentia-tensorflow Discogs model predicts genre tags
  4. analyze_track_full()— combines all three; call this from main.py

zotify lives in its own venv (.venv-dl, Python 3.10+) and is driven as a
subprocess — the app venv stays on Python 3.9. One-time setup:

  /opt/homebrew/bin/python3.12 -m venv .venv-dl
  .venv-dl/bin/pip install git+https://github.com/Googolplexed0/zotify.git
  # First run opens a Spotify OAuth login in the browser (interactive, once);
  # the refresh token is saved and later runs are fully non-interactive.

96 kbps is plenty for analysis: the essentia genre model resamples to 16 kHz
mono anyway, and BPM/key features live in low-mid frequencies that lossy
codecs preserve well (the legacy pipeline analyzed 64 kbps MP3s fine).
"""
import asyncio
import logging
import os
import random
import subprocess
import sys
from pathlib import Path
from typing import Optional

# Load .env BEFORE the module-level os.getenv() reads below. main.py imports
# this module before it calls load_dotenv(), so without this the ZOTIFY_*
# overrides in .env would silently fall back to their defaults (e.g. the
# download rate limiter staying at 1.0 despite .env saying otherwise).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover — always present in the app venv
    pass

log = logging.getLogger(__name__)

# zotify CLI in its dedicated venv. Override with ZOTIFY_BIN.
ZOTIFY_BIN = os.getenv(
    "ZOTIFY_BIN", str(Path(__file__).parent / ".venv-dl" / "bin" / "zotify")
)

# Where zotify stores its saved OAuth credentials. Mirrors zotify's own
# per-platform default (Config.get_credentials_location). Override with
# ZOTIFY_CREDENTIALS if you passed --creds to the login step.
_ZOTIFY_CRED_DEFAULTS = {
    "darwin": Path.home() / "Library/Application Support/Zotify/credentials.json",
    "linux": Path.home() / ".local/share/zotify/credentials.json",
    "win32": Path.home() / "AppData/Roaming/Zotify/credentials.json",
}
ZOTIFY_CREDENTIALS = Path(
    os.getenv("ZOTIFY_CREDENTIALS",
              str(_ZOTIFY_CRED_DEFAULTS.get(sys.platform, Path.cwd() / ".zotify/credentials.json")))
)

# Real-time pacing multiplier passed to zotify's --download-rate-limiter.
# 1.0 = download at playback speed (strongest anti-flag mitigation, the
# recommended setting for bulk library runs). 0 disables pacing.
ZOTIFY_RATE_LIMITER = os.getenv("ZOTIFY_RATE_LIMITER", "1.0")

# ── Ban-risk watchdog ────────────────────────────────────────────────────────
# Spotify's practical rate limit surfaces as audio-key request denials in
# zotify's output ("Failed fetching audio key", API_ERROR 429). When we see
# one, we slow down automatically: base rate → 0.35 on the first signal, and
# → 1.0 (full real-time pacing) if signals keep appearing at 0.35. Escalations
# stick for the server's lifetime (a restart resets to the .env base) and are
# logged loudly. The effective rate never goes FASTER than the .env base.
_ESCALATION_RATES = (0.35, 1.0)
_rate_state = {"level": 0, "signals": 0}

_BAN_RISK_RE = None  # compiled lazily


def current_rate_limiter() -> str:
    """Effective --download-rate-limiter value, including watchdog escalation."""
    try:
        base = float(ZOTIFY_RATE_LIMITER)
    except ValueError:
        base = 1.0
    level = _rate_state["level"]
    if level <= 0:
        return ZOTIFY_RATE_LIMITER
    effective = max(base, _ESCALATION_RATES[min(level, len(_ESCALATION_RATES)) - 1])
    return f"{effective:g}"


def rate_limiter_info() -> dict:
    """Status snapshot for the UI/status endpoint."""
    return {
        "base": ZOTIFY_RATE_LIMITER,
        "effective": current_rate_limiter(),
        "escalation_level": _rate_state["level"],
        "signals": _rate_state["signals"],
    }


def _check_ban_signals(output: str, track_id: str) -> None:
    """Scan zotify's output for rate-limit / ban-risk markers and escalate."""
    global _BAN_RISK_RE
    if not output:
        return
    if _BAN_RISK_RE is None:
        import re
        _BAN_RISK_RE = re.compile(
            r"(?i)(audio[ _-]?key|rate ?limit|too many requests|\b429\b)"
        )
    match = _BAN_RISK_RE.search(output)
    if not match:
        return
    _rate_state["signals"] += 1
    old = current_rate_limiter()
    if _rate_state["level"] < len(_ESCALATION_RATES):
        _rate_state["level"] += 1
        log.warning(
            "BAN-RISK signal in zotify output for %s (marker %r, signal #%d) — "
            "rate limiter escalated %s → %s",
            track_id, match.group(0), _rate_state["signals"], old, current_rate_limiter(),
        )
    else:
        log.warning(
            "BAN-RISK signal for %s (marker %r, signal #%d) — already at max pacing %s",
            track_id, match.group(0), _rate_state["signals"], old,
        )

# Max seconds to wait for one track download. Real-time pacing means a track
# takes roughly its own duration to download, so allow generous headroom.
ZOTIFY_TIMEOUT = int(os.getenv("ZOTIFY_TIMEOUT", "900"))

# Random extra wait (seconds, up to this value) before each download, so
# sequential audio-key requests don't fire in a perfectly regular rhythm.
ZOTIFY_PACING_JITTER = float(os.getenv("ZOTIFY_PACING_JITTER", "5"))

# Directory where downloaded audio files are stored permanently.
# Override with env var SPOTO_AUDIO_DIR.
AUDIO_DIR = Path(os.getenv("SPOTO_AUDIO_DIR", ".audio_files"))

# NOTE on timbre embeddings: we evaluated cosine similarity over mean-pooled
# EffNet embeddings (raw, z-scored, and over the genre-head outputs at 11 and
# 400 dims) as a style-cohesion signal for the sequencer, and NONE ordered
# same-style pairs above different-style pairs on the real library (e.g. two
# house tracks scored below house·neoclassical-piano). Classifier features
# mean-pooled over time are not a perceptual similarity space, so no embedding
# store is kept — style cohesion uses vocalness + era instead, which separated
# the observed mismatches cleanly (vocalness 91-92 vs 2-22).

# Directory where essentia model files are stored.
MODELS_DIR = Path(os.getenv("SPOTO_MODELS_DIR", ".essentia_models"))

# Mapping from essentia key string → pitch index (matches analysis.py KEY_NAMES)
_KEY_TO_PITCH = {
    "C": 0, "C#": 1, "D": 2, "D#": 3, "E": 4, "F": 5,
    "F#": 6, "G": 7, "G#": 8, "A": 9, "A#": 10, "B": 11,
    "Db": 1, "Eb": 3, "Gb": 6, "Ab": 8, "Bb": 10,
}

# Discogs-400 genre labels are loaded at runtime from the model's metadata JSON
# (the authoritative 400-class list in exact output order). Hand-transcribing
# them is error-prone — a mismatched/short list silently maps every prediction
# to the wrong genre.
_GENRE_LABELS_CACHE = None


def _load_genre_labels() -> list:
    """Return the 400 Discogs genre labels in model output order, or [] if the
    metadata JSON is missing. Cached after first load."""
    global _GENRE_LABELS_CACHE
    if _GENRE_LABELS_CACHE is not None:
        return _GENRE_LABELS_CACHE
    import json
    path = MODELS_DIR / _GENRE_LABELS_JSON
    try:
        classes = json.loads(path.read_text())["classes"]
        if not isinstance(classes, list) or len(classes) < 100:
            raise ValueError(f"unexpected classes payload ({type(classes)}, len={len(classes)})")
        _GENRE_LABELS_CACHE = classes
    except Exception as e:
        log.error("Could not load genre labels from %s: %s — run download_models()", path, e)
        _GENRE_LABELS_CACHE = []
    return _GENRE_LABELS_CACHE


def _key_to_camelot(key: str, scale: str) -> str:
    """Convert essentia key name + scale to Camelot notation."""
    from analysis import CAMELOT
    pitch = _KEY_TO_PITCH.get(key)
    if pitch is None:
        return "?"
    mode = 1 if scale == "major" else 0
    return CAMELOT.get((pitch, mode), "?")


def _ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Download
# ─────────────────────────────────────────────────────────────────────────────

def _find_downloaded(track_id: str) -> Optional[Path]:
    """Return the cached audio file for a track, if any."""
    for ext in (".ogg", ".m4a", ".mp3", ".opus", ".flac", ".wav"):
        cached = AUDIO_DIR / f"{track_id}{ext}"
        if cached.exists():
            return cached
    return None


def zotify_logged_in() -> bool:
    """
    True if zotify has saved OAuth credentials.

    Critical guard: if this is False, zotify would fall into its interactive
    browser-login flow, whose OAuth callback server binds all interfaces
    (0.0.0.0) and triggers the macOS "find devices on your local network"
    prompt. That login must be done ONCE, by hand, via setup_zotify.py — never
    unattended from the web server. We check this before ever spawning zotify.
    """
    return ZOTIFY_CREDENTIALS.exists()


def build_zotify_command(spotify_url: str) -> list:
    """
    Build the zotify CLI invocation for a single track at the lowest quality.

    Output lands at AUDIO_DIR/{spotify track id}.ogg (96 kbps Vorbis, no
    transcode). Every metadata extra is disabled: fewer per-track API calls
    means less rate-limit exposure (see Googolplexed0/zotify issue #209), and
    tags are irrelevant for analysis.
    """
    return [
        ZOTIFY_BIN,
        "--download-quality", "normal",          # 96 kbps Ogg Vorbis (lowest tier)
        "--codec", "copy",                        # keep native stream, no ffmpeg
        "--root-path", str(AUDIO_DIR),
        "--output-single", "{id}",                # → AUDIO_DIR/<track_id>.ogg
        "--lyrics-to-file", "False",
        "--lyrics-to-metadata", "False",
        "--md-save-lyrics", "False",
        "--album-art-jpg-file", "False",
        "--md-save-genres", "False",              # extra API call per track — skip
        "--md-disc-track-totals", "False",        # extra API call per track — skip
        "--disable-song-archive", "True",
        "--disable-directory-archives", "True",
        "--download-rate-limiter", current_rate_limiter(),
        "--retry-attempts", "3",
        "--print-splash", "False",
        "--print-progress-info", "False",
        "--print-download-progress", "False",
        spotify_url,
    ]


def _download_timeout(duration_ms: int) -> int:
    """
    Per-track download timeout, aware of real-time pacing.

    With --download-rate-limiter R, a track takes ≈ duration × R to download,
    so a fixed timeout kills long tracks forever: LCD Soundsystem's "45:33"
    (45.5 min) at R=0.35 needs ~16 min but the old fixed 900 s cap killed it
    at 15 — on every single run. Scale the cap with the expected download
    time (+50% headroom +3 min slack), floored at ZOTIFY_TIMEOUT.
    """
    if duration_ms <= 0:
        return ZOTIFY_TIMEOUT
    try:
        rate = float(current_rate_limiter())
    except ValueError:
        rate = 1.0
    expected = (duration_ms / 1000.0) * max(rate, 0.1)
    return max(ZOTIFY_TIMEOUT, int(expected * 1.5 + 180))


def download_track(spotify_url: str, track_id: str, duration_ms: int = 0) -> Optional[Path]:
    """
    Download a track's audio directly from Spotify via zotify (subprocess).

    Returns the path to the downloaded .ogg, or None on failure. Files are
    stored permanently in AUDIO_DIR so they are never re-downloaded; zotify
    itself also skips existing files (--skip-existing defaults to True).

    Requires one-time interactive OAuth setup (see module docstring). If the
    saved credentials are missing, zotify would block waiting for a browser
    login — we detect that case and fail fast with a clear message instead.
    """
    _ensure_dir(AUDIO_DIR)

    cached = _find_downloaded(track_id)
    if cached:
        log.info("Audio already downloaded: %s", cached)
        return cached

    if not Path(ZOTIFY_BIN).exists():
        log.error(
            "zotify not found at %s — create .venv-dl and pip install "
            "git+https://github.com/Googolplexed0/zotify.git (see essentia_analysis.py)",
            ZOTIFY_BIN,
        )
        return None

    # Never let the server trigger zotify's interactive login (it binds 0.0.0.0
    # and pops the macOS local-network prompt). Require the one-time manual
    # login via setup_zotify.py first.
    if not zotify_logged_in():
        log.error(
            "zotify has no saved credentials at %s — run the one-time login "
            "first:  .venv-dl/bin/python setup_zotify.py",
            ZOTIFY_CREDENTIALS,
        )
        return None

    cmd = build_zotify_command(spotify_url)
    timeout = _download_timeout(duration_ms)
    log.info("Downloading %s via zotify (timeout %ss)…", track_id, timeout)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,  # never let it block on interactive input
        )
    except subprocess.TimeoutExpired:
        log.warning("zotify timed out (>%ss) for %s", timeout, track_id)
        return None
    except OSError as e:
        log.error("could not run zotify (%s): %s", ZOTIFY_BIN, e)
        return None

    # Ground truth is the output file — zotify's exit code can mask errors
    # (Googolplexed0/zotify issue #222).
    combined = (proc.stdout or "") + (proc.stderr or "")
    # Watchdog: scan EVERY download's output (success included — zotify may
    # retry through audio-key denials and still produce the file).
    _check_ban_signals(combined, track_id)

    path = _find_downloaded(track_id)
    if path:
        log.info("Downloaded: %s → %s", track_id, path)
        return path

    if "login" in combined.lower() and "http" in combined.lower():
        log.error(
            "zotify needs its one-time OAuth login. Run this in a terminal and "
            "complete the browser login:  %s <any spotify track url>",
            ZOTIFY_BIN,
        )
    else:
        tail = combined.strip().splitlines()[-8:]
        log.warning(
            "zotify produced no file for %s (exit %s): %s",
            track_id, proc.returncode, " | ".join(tail),
        )
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Audio analysis
# ─────────────────────────────────────────────────────────────────────────────

_LUFS_FLOOR = -70.0  # EBUR128's own silence floor; also our NaN/Inf fallback


def _integrated_lufs(mono, es) -> float:
    """
    Integrated loudness (LUFS) via EBUR128, the broadcast-standard perceptual
    loudness measure — the closest match to Spotify's `loudness` dB field.
    EBUR128 wants a stereo signal; we duplicate the mono channel. Falls back to
    DynamicComplexity's dB estimate, then to an RMS-derived dB, so it never
    throws. Always returns a finite value (NaN/Inf → the -70 floor), because
    downstream round()/int() would otherwise raise on a non-finite input.
    """
    import math
    import numpy as np

    def _finite(x):
        x = float(x)
        return x if math.isfinite(x) else _LUFS_FLOOR

    try:
        stereo = np.stack([mono, mono], axis=1)
        return _finite(es.LoudnessEBUR128()(stereo)[2])   # index 2 = integratedLoudness
    except Exception:
        pass
    try:
        _, loud_db = es.DynamicComplexity()(mono)
        return _finite(loud_db)
    except Exception:
        pass
    # Last resort: RMS → dBFS
    rms = float(es.RMS()(mono))
    return 20.0 * math.log10(rms) if rms > 1e-9 else _LUFS_FLOOR


def _energy_from_lufs(lufs: float) -> int:
    """
    Map perceptual loudness (LUFS) to a 0–100 energy score.

    Replaces the old `RMS * 450` which saturated at 100 for almost any loud,
    modern master. Real tracks measure roughly -18…-8 LUFS; we map the wider
    [-30, -6] window to [0, 100] so nothing pins to 100 short of a brickwalled
    master, and quiet material spreads across the low end.
    """
    score = (lufs + 30.0) / 24.0 * 100.0
    return max(0, min(100, round(score)))


def _danceability_score(raw: float) -> int:
    """
    Map essentia's DFA danceability (theoretical 0–~3, but real music clusters
    ~0.8–2.3) onto 0–100. We scale against that empirical window rather than the
    theoretical ceiling of 3.0 — dividing by 3.0 compresses everything into a
    dull ~40–60 band with no discriminating power.
    """
    lo, hi = 0.8, 2.3
    score = (raw - lo) / (hi - lo) * 100.0
    return max(0, min(100, round(score)))


def _resolve_tempo_octave(rhythm_bpm: float, percival_bpm: float) -> float:
    """
    Correct octave (half/double-tempo) errors in beat tracking.

    RhythmExtractor2013 gives an accurate beat grid but on slow, sparse,
    reverb-heavy material (e.g. instrumental guitar) it often locks onto the
    subdivision and reports double the real tempo. We cross-check against a
    second, independent estimator (PercivalBpmEstimator): when the two disagree
    by ~2x it's an octave ambiguity, and we fold the primary onto the shared
    octave. When they agree (the overwhelming common case) the primary is kept
    untouched.
    """
    if percival_bpm <= 0:
        return rhythm_bpm
    if 1.85 <= rhythm_bpm / percival_bpm <= 2.15:      # primary doubled the tempo
        return rhythm_bpm / 2.0
    if 1.85 <= percival_bpm / rhythm_bpm <= 2.15:      # primary halved the tempo
        return rhythm_bpm * 2.0
    return rhythm_bpm


def analyze_audio(audio_path: Path) -> dict:
    """
    Analyze audio with essentia. Returns
    {bpm, key, camelot, energy, danceability, loudness}.

    - bpm     — RhythmExtractor2013 (multifeature), octave-corrected against
                PercivalBpmEstimator to fix half/double-tempo errors
    - key     — HPCP-based KeyExtractor → musical key + Camelot
    - loudness— integrated LUFS (EBUR128); matches Spotify's `loudness` dB field
    - energy  — 0–100 derived from loudness (recalibrated, non-saturating)
    - danceability — essentia Danceability (0–~3) rescaled to 0–100
    """
    try:
        import essentia.standard as es  # type: ignore
    except ImportError:
        raise RuntimeError("essentia not installed — run: pip install essentia-tensorflow")

    audio = es.MonoLoader(filename=str(audio_path), sampleRate=44100)()

    # BPM — multifeature method handles tempo changes and syncopation better,
    # then cross-check a second estimator to catch octave (half/double) errors.
    rhythm_bpm, _, _, _, _ = es.RhythmExtractor2013(method="multifeature")(audio)
    rhythm_bpm = float(rhythm_bpm)
    try:
        percival_bpm = float(es.PercivalBpmEstimator()(audio))
    except Exception as e:
        log.warning("PercivalBpmEstimator failed for %s: %s", audio_path, e)
        percival_bpm = 0.0
    corrected = _resolve_tempo_octave(rhythm_bpm, percival_bpm)
    if abs(corrected - rhythm_bpm) > 0.1:
        log.info("Octave-corrected BPM for %s: %.1f → %.1f (percival=%.1f)",
                 audio_path.name, rhythm_bpm, corrected, percival_bpm)
    bpm = round(corrected, 1)

    # Key
    key, scale, _ = es.KeyExtractor()(audio)
    camelot = _key_to_camelot(key, scale)

    # Loudness (LUFS) → energy score
    lufs = _integrated_lufs(audio, es)
    loudness = round(max(-60.0, min(0.0, lufs)), 1)
    energy = _energy_from_lufs(lufs)

    # Danceability — essentia returns ~0..3 (higher = more danceable)
    try:
        raw_dance, _ = es.Danceability()(audio)
        danceability = _danceability_score(float(raw_dance))
    except Exception as e:
        log.warning("Danceability failed for %s: %s", audio_path, e)
        danceability = 0

    return {
        "bpm": bpm,
        "key": f"{key} {'maj' if scale == 'major' else 'min'}",
        "camelot": camelot,
        "energy": energy,
        "danceability": danceability,
        "loudness": loudness,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Genre classification
# ─────────────────────────────────────────────────────────────────────────────

_EFFNET_MODEL     = "discogs-effnet-bs64-1.pb"
_GENRE_MODEL      = "genre_discogs400-discogs-effnet-1.pb"
_GENRE_LABELS_JSON = "genre_discogs400-discogs-effnet-1.json"  # authoritative class list
_VOICE_MODEL      = "voice_instrumental-discogs-effnet-1.pb"   # classes: [instrumental, voice]
_MODEL_BASE_URL   = "https://essentia.upf.edu/models"


def download_models():
    """
    Download essentia pre-trained model files if not already present.
    Call once during setup: python -c "from essentia_analysis import download_models; download_models()"
    """
    import urllib.request

    _ensure_dir(MODELS_DIR)

    _genre_base = f"{_MODEL_BASE_URL}/classification-heads/genre_discogs400"
    models = {
        _EFFNET_MODEL: f"{_MODEL_BASE_URL}/feature-extractors/discogs-effnet/{_EFFNET_MODEL}",
        _GENRE_MODEL:  f"{_genre_base}/{_GENRE_MODEL}",
        _GENRE_LABELS_JSON: f"{_genre_base}/{_GENRE_LABELS_JSON}",  # 400-class label map
        _VOICE_MODEL:  f"{_MODEL_BASE_URL}/classification-heads/voice_instrumental/{_VOICE_MODEL}",
    }

    for filename, url in models.items():
        dest = MODELS_DIR / filename
        if dest.exists():
            log.info("Model already present: %s", filename)
            continue
        log.info("Downloading model %s …", filename)
        print(f"Downloading {filename} …")
        urllib.request.urlretrieve(url, dest)
        print(f"  → saved to {dest}")


# Discogs "Parent" genres too broad to be useful as DJ tags on their own; we
# still emit them (they help playlist genre-clustering match) but after the
# specific subgenres.
_GENRE_NOISE_PARENTS = {"Non-Music", "Stage & Screen", "Brass & Military", "Children's"}


def _clean_genre_labels(raw_labels: list) -> list:
    """
    Turn raw Discogs-400 labels ("Electronic---Glitch") into clean, readable and
    cluster-matchable tags.

    "Electronic---Glitch" → parent "Electronic" + subgenre "Glitch". We return
    subgenres first (most useful for a DJ), then parents, de-duplicated in order.
    The split also lets playlist_engine.tags_to_clusters() match them — the raw
    "Parent---Child" string matches no cluster, but "Electronic"/"Glitch" do.
    Drops non-music / soundtrack noise.
    """
    subs, parents = [], []
    for label in raw_labels:
        parent, _, child = label.partition("---")
        if parent in _GENRE_NOISE_PARENTS:
            continue
        if child:
            subs.append(child.strip())
        if parent:
            # Discogs compound parents ("Folk, World, & Country", "Funk / Soul")
            # match no genre cluster as-is — split them into their component
            # genres so playlist filtering can cluster these tracks.
            import re
            parts = [p.strip() for p in re.split(r"[,/&]", parent) if p.strip()]
            parents.extend(parts if len(parts) > 1 else [parent.strip()])

    ordered, seen = [], set()
    for tag in subs + parents:
        key = tag.lower()
        if tag and key not in seen:
            seen.add(key)
            ordered.append(tag)
    return ordered


# Version of the style-inference outputs (genre_affinity attribution rules,
# vocalness). Bump on logic changes so migrate_style.py recomputes stale rows.
STYLE_VERSION = 2

# Cached TF models — loading the graphs takes seconds; reuse across tracks.
_TF_MODELS: dict = {}


def _get_tf_models():
    """Lazily load and cache the EffNet extractor + classification heads."""
    if _TF_MODELS:
        return _TF_MODELS
    import essentia
    # Reusing cached TF predictors makes essentia print a harmless "No network
    # created…" warning on every inference — silence it or it floods the log.
    essentia.log.warningActive = False
    import essentia.standard as es  # type: ignore
    _TF_MODELS["effnet"] = es.TensorflowPredictEffnetDiscogs(
        graphFilename=str(MODELS_DIR / _EFFNET_MODEL),
        output="PartitionedCall:1",
    )
    _TF_MODELS["genre"] = es.TensorflowPredict2D(
        graphFilename=str(MODELS_DIR / _GENRE_MODEL),
        input="serving_default_model_Placeholder",
        output="PartitionedCall:0",
    )
    voice_path = MODELS_DIR / _VOICE_MODEL
    if voice_path.exists():
        _TF_MODELS["voice"] = es.TensorflowPredict2D(
            graphFilename=str(voice_path),
            input="model/Placeholder",
            output="model/Softmax",
        )
    return _TF_MODELS


_CLUSTER_MAP_CACHE = None


def _label_cluster_map() -> list:
    """Clusters (frozenset) credited by each of the 400 Discogs labels, by index.

    SUBGENRE-FIRST attribution: when the child part of "Parent---Child" maps to
    a cluster on its own, ONLY those clusters receive the label's probability
    mass. The parent is a fallback for unmapped children. Otherwise every
    "Electronic---X" label (ambient, downtempo, synth-pop…) would credit the
    'electronic' cluster too, structurally inflating it and letting ambient
    tracks pass an electronic genre filter — the exact intruder class the
    affinity system exists to stop."""
    global _CLUSTER_MAP_CACHE
    if _CLUSTER_MAP_CACHE is None:
        from playlist_engine import tags_to_clusters   # lazy — no import cycle
        mapping = []
        for label in _load_genre_labels():
            _, _, child = label.partition("---")
            clusters = tags_to_clusters([child.strip()]) if child else set()
            if not clusters:
                clusters = tags_to_clusters(_clean_genre_labels([label]))
            mapping.append(frozenset(clusters))
        _CLUSTER_MAP_CACHE = mapping
    return _CLUSTER_MAP_CACHE


def analyze_style(audio_path: Path, top_n: int = 4) -> dict:
    """
    Full style inference from one embedding pass:

      track_genres   — clean top-N genre tags (as before)
      genre_affinity — per-cluster probability mass {cluster: 0..1}. The sum of
                       the model's 400 label probabilities grouped by playlist
                       genre cluster: the honest "how much of this cluster is
                       in this track", replacing binary tag membership.
      vocalness      — 0-100 P(voice) from the voice/instrumental head

    Returns {} if models are missing or inference fails.
    """
    if not (MODELS_DIR / _EFFNET_MODEL).exists() or not (MODELS_DIR / _GENRE_MODEL).exists():
        log.warning("Essentia models not found in %s — run download_models() first", MODELS_DIR)
        return {}

    try:
        import numpy as np
        import essentia.standard as es  # type: ignore

        models = _get_tf_models()
        audio16k = es.MonoLoader(filename=str(audio_path), sampleRate=16000)()
        embeddings = models["effnet"](audio16k)          # (frames, 1280)

        # Genre head
        avg = np.mean(models["genre"](embeddings), axis=0)
        labels = _load_genre_labels()
        if len(labels) != len(avg):
            log.error("Genre label count (%d) != model outputs (%d); skipping genres",
                      len(labels), len(avg))
            return {}

        top_indices = np.argsort(avg)[::-1][:top_n]
        track_genres = _clean_genre_labels([labels[i] for i in top_indices])

        # Per-cluster affinity: sum probability mass by cluster.
        cluster_map = _label_cluster_map()
        affinity: dict = {}
        for prob, clusters in zip(avg, cluster_map):
            for c in clusters:
                affinity[c] = affinity.get(c, 0.0) + float(prob)
        affinity = {c: round(v, 4) for c, v in affinity.items() if v >= 0.01}

        # Voice/instrumental head (optional — model may not be downloaded yet)
        vocalness = None
        if "voice" in models:
            voice_pred = np.mean(models["voice"](embeddings), axis=0)
            vocalness = int(round(float(voice_pred[1]) * 100))   # [instrumental, voice]

        return {
            "track_genres":   track_genres,
            "genre_affinity": affinity,
            "vocalness":      vocalness,
            # Bump when the affinity attribution logic changes — the migration
            # script re-processes entries with an older/missing version.
            "style_version":  STYLE_VERSION,
        }
    except Exception as e:
        log.warning("Style inference failed for %s: %s", audio_path, e)
        return {}


def classify_genre(audio_path: Path, top_n: int = 4) -> list:
    """Clean top-N genre tags (thin wrapper kept for backward compatibility)."""
    return analyze_style(audio_path, top_n).get("track_genres", [])


# ─────────────────────────────────────────────────────────────────────────────
# Combined entry point
# ─────────────────────────────────────────────────────────────────────────────

async def analyze_track_full(
    track_id: str,
    spotify_url: str,
    title: str,
    artists: str,
    semaphore: asyncio.Semaphore,
    skip_genre: bool = False,
    on_stage=None,
    duration_ms: int = 0,
) -> tuple:
    """
    Download + analyze a track. Returns (track_id, result_dict).

    result_dict contains: bpm, key, camelot, energy, track_genres, source="local"
    or {"error": "..."} on failure.

    Downloads must run strictly sequentially (semaphore of 1) with jittered
    pacing — that is the recommended anti-rate-limit pattern for zotify.

    on_stage: optional callable(stage: str) fired when the track actually
    starts each phase ("downloading", "analyzing") — i.e. after acquiring the
    sequential slot, so the caller can surface live progress.
    """
    def _stage(name: str):
        if on_stage:
            try:
                on_stage(name)
            except Exception:  # progress reporting must never break analysis
                pass

    async with semaphore:
        loop = asyncio.get_event_loop()

        # Jittered pause so sequential downloads don't fire in a perfectly
        # regular rhythm (skipped when the file is already cached).
        if ZOTIFY_PACING_JITTER > 0 and not _find_downloaded(track_id):
            await asyncio.sleep(random.uniform(0, ZOTIFY_PACING_JITTER))

        # Download
        _stage("downloading")
        audio_path = await loop.run_in_executor(
            None, download_track, spotify_url, track_id, duration_ms,
        )
        if not audio_path:
            return track_id, {"error": f"audio download failed for '{title}'"}

        # Analyze BPM / key / energy
        _stage("analyzing")
        try:
            result = await loop.run_in_executor(None, analyze_audio, audio_path)
        except Exception as e:
            return track_id, {"error": f"essentia analysis failed: {e}"}

        result["source"] = "local"

        # Style inference: genres + per-cluster affinity + vocalness,
        # all from one EffNet pass (optional, skip if models absent)
        if not skip_genre:
            try:
                style = await loop.run_in_executor(None, analyze_style, audio_path)
            except Exception as e:
                log.warning("Style inference error for '%s': %s", title, e)
                style = {}
            result["track_genres"]   = style.get("track_genres", [])
            result["genre_affinity"] = style.get("genre_affinity", {})
            result["style_version"]  = style.get("style_version", 0)
            if style.get("vocalness") is not None:
                result["vocalness"] = style["vocalness"]

        log.info(
            "Local analysis OK for '%s': bpm=%.1f key=%s camelot=%s genres=%s vocal=%s",
            title, result.get("bpm", 0), result.get("key"), result.get("camelot"),
            result.get("track_genres", [])[:2], result.get("vocalness"),
        )
        return track_id, result
