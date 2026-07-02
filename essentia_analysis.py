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

# Max seconds to wait for one track download. Real-time pacing means a track
# takes roughly its own duration to download, so allow generous headroom.
ZOTIFY_TIMEOUT = int(os.getenv("ZOTIFY_TIMEOUT", "900"))

# Random extra wait (seconds, up to this value) before each download, so
# sequential audio-key requests don't fire in a perfectly regular rhythm.
ZOTIFY_PACING_JITTER = float(os.getenv("ZOTIFY_PACING_JITTER", "5"))

# Directory where downloaded audio files are stored permanently.
# Override with env var SPOTO_AUDIO_DIR.
AUDIO_DIR = Path(os.getenv("SPOTO_AUDIO_DIR", ".audio_files"))

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
        "--download-rate-limiter", ZOTIFY_RATE_LIMITER,
        "--retry-attempts", "3",
        "--print-splash", "False",
        "--print-progress-info", "False",
        "--print-download-progress", "False",
        spotify_url,
    ]


def download_track(spotify_url: str, track_id: str) -> Optional[Path]:
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
    log.info("Downloading %s via zotify …", track_id)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=ZOTIFY_TIMEOUT,
            stdin=subprocess.DEVNULL,  # never let it block on interactive input
        )
    except subprocess.TimeoutExpired:
        log.warning("zotify timed out (>%ss) for %s", ZOTIFY_TIMEOUT, track_id)
        return None
    except OSError as e:
        log.error("could not run zotify (%s): %s", ZOTIFY_BIN, e)
        return None

    # Ground truth is the output file — zotify's exit code can mask errors
    # (Googolplexed0/zotify issue #222).
    path = _find_downloaded(track_id)
    if path:
        log.info("Downloaded: %s → %s", track_id, path)
        return path

    combined = (proc.stdout or "") + (proc.stderr or "")
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
            parents.append(parent.strip())

    ordered, seen = [], set()
    for tag in subs + parents:
        key = tag.lower()
        if tag and key not in seen:
            seen.add(key)
            ordered.append(tag)
    return ordered


def classify_genre(audio_path: Path, top_n: int = 4) -> list:
    """
    Classify genre using essentia's Discogs-400 model.

    Returns a de-duplicated list of clean genre tags (e.g. ["Glitch",
    "Vaporwave", "Electronic"]) derived from the top_n raw predictions.
    Returns [] if models are not downloaded or essentia-tensorflow is not installed.
    """
    effnet_path = MODELS_DIR / _EFFNET_MODEL
    genre_path  = MODELS_DIR / _GENRE_MODEL

    if not effnet_path.exists() or not genre_path.exists():
        log.warning(
            "Essentia models not found in %s — run download_models() first", MODELS_DIR
        )
        return []

    try:
        import numpy as np
        import essentia.standard as es  # type: ignore

        # Step 1: extract embeddings at 16 kHz (required by EffNet)
        audio16k = es.MonoLoader(filename=str(audio_path), sampleRate=16000)()
        embedding_model = es.TensorflowPredictEffnetDiscogs(
            graphFilename=str(effnet_path),
            output="PartitionedCall:1",
        )
        embeddings = embedding_model(audio16k)

        # Step 2: predict genre from embeddings
        genre_model = es.TensorflowPredict2D(
            graphFilename=str(genre_path),
            input="serving_default_model_Placeholder",
            output="PartitionedCall:0",
        )
        predictions = genre_model(embeddings)
        avg = np.mean(predictions, axis=0)

        labels = _load_genre_labels()
        if len(labels) != len(avg):
            log.error(
                "Genre label count (%d) != model outputs (%d); skipping genres",
                len(labels), len(avg),
            )
            return []

        top_indices = np.argsort(avg)[::-1][:top_n]
        raw = [labels[i] for i in top_indices]
        return _clean_genre_labels(raw)

    except Exception as e:
        log.warning("Genre classification failed for %s: %s", audio_path, e)
        return []


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
) -> tuple:
    """
    Download + analyze a track. Returns (track_id, result_dict).

    result_dict contains: bpm, key, camelot, energy, track_genres, source="local"
    or {"error": "..."} on failure.

    Downloads must run strictly sequentially (semaphore of 1) with jittered
    pacing — that is the recommended anti-rate-limit pattern for zotify.
    """
    async with semaphore:
        loop = asyncio.get_event_loop()

        # Jittered pause so sequential downloads don't fire in a perfectly
        # regular rhythm (skipped when the file is already cached).
        if ZOTIFY_PACING_JITTER > 0 and not _find_downloaded(track_id):
            await asyncio.sleep(random.uniform(0, ZOTIFY_PACING_JITTER))

        # Download
        audio_path = await loop.run_in_executor(
            None, download_track, spotify_url, track_id,
        )
        if not audio_path:
            return track_id, {"error": f"audio download failed for '{title}'"}

        # Analyze BPM / key / energy
        try:
            result = await loop.run_in_executor(None, analyze_audio, audio_path)
        except Exception as e:
            return track_id, {"error": f"essentia analysis failed: {e}"}

        result["source"] = "local"

        # Genre classification (optional, skip if models not present)
        if not skip_genre:
            try:
                genres = await loop.run_in_executor(None, classify_genre, audio_path)
                result["track_genres"] = genres
            except Exception as e:
                log.warning("Genre classification error for '%s': %s", title, e)
                result["track_genres"] = []

        log.info(
            "Local analysis OK for '%s': bpm=%.1f key=%s camelot=%s genres=%s",
            title, result.get("bpm", 0), result.get("key"), result.get("camelot"),
            result.get("track_genres", [])[:2],
        )
        return track_id, result
