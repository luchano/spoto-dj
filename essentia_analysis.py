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

# Discogs 400 genre labels in model output order.
# Source: https://essentia.upf.edu/models/classification-heads/genre_discogs400/
DISCOGS400_LABELS = [
    "Blues---Boogie Woogie", "Blues---Chicago Blues", "Blues---Country Blues",
    "Blues---Delta Blues", "Blues---Electric Blues", "Blues---Harmonica Blues",
    "Blues---Jump Blues", "Blues---Louisiana Blues", "Blues---Modern Electric Blues",
    "Blues---Piano Blues", "Blues---Rhythm & Blues", "Blues---Texas Blues",
    "Brass & Military---Brass Band", "Brass & Military---Marches",
    "Brass & Military---Military", "Children's---Children's",
    "Classical---Baroque", "Classical---Choral", "Classical---Classical",
    "Classical---Contemporary", "Classical---Impressionist", "Classical---Medieval",
    "Classical---Modern", "Classical---Neo-Classical", "Classical---Neo-Romantic",
    "Classical---Opera", "Classical---Post-Modern", "Classical---Renaissance",
    "Classical---Romantic", "Electronic---Abstract", "Electronic---Ambient",
    "Electronic---Bass Music", "Electronic---Bleep", "Electronic---Breakbeat",
    "Electronic---Broken Beat", "Electronic---Dancehall", "Electronic---Dark Ambient",
    "Electronic---Downtempo", "Electronic---Dub", "Electronic---Dub Techno",
    "Electronic---Dubstep", "Electronic---Electro", "Electronic---Electroacoustic",
    "Electronic---Electronic Rock", "Electronic---Footwork", "Electronic---Funk",
    "Electronic---Funky Breaks", "Electronic---Future Jazz", "Electronic---Gabber",
    "Electronic---Ghettotech", "Electronic---Glitch", "Electronic---Glitch Hop",
    "Electronic---Grime", "Electronic---Halftime", "Electronic---Hardcore",
    "Electronic---Hardstyle", "Electronic---Hi NRG", "Electronic---Hip Hop",
    "Electronic---House", "Electronic---IDM", "Electronic---Illbient",
    "Electronic---Industrial", "Electronic---Italo-Disco", "Electronic---Juke",
    "Electronic---Jungle", "Electronic---Latin", "Electronic---Leftfield",
    "Electronic---Minimal", "Electronic---Modern Classical", "Electronic---Musique Concrète",
    "Electronic---Neo Trance", "Electronic---Noise", "Electronic---Nu-Disco",
    "Electronic---Power Electronics", "Electronic---Progressive House",
    "Electronic---Progressive Trance", "Electronic---Psy-Trance", "Electronic---Rhythmic Noise",
    "Electronic---Screw", "Electronic---Soca", "Electronic---Soul",
    "Electronic---Soundtrack", "Electronic---Synth-pop", "Electronic---Tech House",
    "Electronic---Tech Trance", "Electronic---Techno", "Electronic---Trance",
    "Electronic---Tribal", "Electronic---Tribal House", "Electronic---Trip Hop",
    "Electronic---UK Garage", "Electronic---Vaporwave",
    "Folk, World, & Country---African", "Folk, World, & Country---Bluegrass",
    "Folk, World, & Country---Cajun", "Folk, World, & Country---Canzone Napoletana",
    "Folk, World, & Country---Catalan Music", "Folk, World, & Country---Celtic",
    "Folk, World, & Country---Country", "Folk, World, & Country---Fado",
    "Folk, World, & Country---Flamenco", "Folk, World, & Country---Folk",
    "Folk, World, & Country---Gospel", "Folk, World, & Country---Highlife",
    "Folk, World, & Country---Hillbilly", "Folk, World, & Country---Hindustani",
    "Folk, World, & Country---Honky Tonk", "Folk, World, & Country---Indian Classical",
    "Folk, World, & Country---Isicathamiya", "Folk, World, & Country---Jive",
    "Folk, World, & Country---Klezmer", "Folk, World, & Country---Laïkó",
    "Folk, World, & Country---Latin", "Folk, World, & Country---Lutenist",
    "Folk, World, & Country---Mbalax", "Folk, World, & Country---Merengue",
    "Folk, World, & Country---Merseybeat", "Folk, World, & Country---Middle Eastern",
    "Folk, World, & Country---Norteño", "Folk, World, & Country---Polka",
    "Folk, World, & Country---Raï", "Folk, World, & Country---Reggae",
    "Folk, World, & Country---Romani", "Folk, World, & Country---Salsa",
    "Folk, World, & Country---Samba", "Folk, World, & Country---Séga",
    "Folk, World, & Country---Soukous", "Folk, World, & Country---Tango",
    "Folk, World, & Country---Volksmusik", "Folk, World, & Country---Zouk",
    "Folk, World, & Country---Zydeco",
    "Funk / Soul---Afrobeat", "Funk / Soul---Boogie", "Funk / Soul---Contemporary R&B",
    "Funk / Soul---Disco", "Funk / Soul---Free Funk", "Funk / Soul---Funk",
    "Funk / Soul---Gospel", "Funk / Soul---Neo Soul", "Funk / Soul---New Jack Swing",
    "Funk / Soul---P.Funk", "Funk / Soul---Quiet Storm", "Funk / Soul---Soul",
    "Hip Hop---Bass Music", "Hip Hop---Bounce", "Hip Hop---Conscious",
    "Hip Hop---Crunk", "Hip Hop---Dirty South", "Hip Hop---East Coast Hip Hop",
    "Hip Hop---Gangsta", "Hip Hop---Grime", "Hip Hop---Hardcore Hip-Hop",
    "Hip Hop---Horrorcore", "Hip Hop---Hyphy", "Hip Hop---Instrumental",
    "Hip Hop---Latin", "Hip Hop---Nerdcore", "Hip Hop---Old School Hip-Hop",
    "Hip Hop---Political", "Hip Hop---Pop Rap", "Hip Hop---Ragga HipHop",
    "Hip Hop---Southern Hip-Hop", "Hip Hop---Thug Rap", "Hip Hop---Trap",
    "Hip Hop---Turntablism", "Hip Hop---Underground Hip-Hop", "Hip Hop---West Coast Hip-Hop",
    "Jazz---Afro-Cuban Jazz", "Jazz---Avant-garde Jazz", "Jazz---Big Band",
    "Jazz---Bop", "Jazz---Bossa Nova", "Jazz---Contemporary Jazz",
    "Jazz---Cool Jazz", "Jazz---Dixieland", "Jazz---Ethio-jazz",
    "Jazz---European Free Jazz", "Jazz---Free Jazz", "Jazz---Fusion",
    "Jazz---Gypsy Jazz", "Jazz---Hard Bop", "Jazz---Jazz-Funk",
    "Jazz---Jazz-Rock", "Jazz---Latin Jazz", "Jazz---Modal",
    "Jazz---Post Bop", "Jazz---Ragtime", "Jazz---Smooth Jazz",
    "Jazz---Soul-Jazz", "Jazz---Swing", "Jazz---Vocal",
    "Latin---Afrobeat", "Latin---Bachata", "Latin---Batucada",
    "Latin---Beguine", "Latin---Bolero", "Latin---Boogaloo",
    "Latin---Bossanova", "Latin---Cha-Cha", "Latin---Charanga",
    "Latin---Chicha", "Latin---Cumbia", "Latin---Forró",
    "Latin---Guaracha", "Latin---Joropo", "Latin---Mambo",
    "Latin---Merengue", "Latin---Norteño", "Latin---Nueva Canción",
    "Latin---Nueva Trova", "Latin---Pachanga", "Latin---Porro",
    "Latin---Ranchera", "Latin---Reggaeton", "Latin---Rumba",
    "Latin---Salsa", "Latin---Samba", "Latin---Son",
    "Latin---Son Cubano", "Latin---Tango", "Latin---Tejano",
    "Latin---Timba", "Latin---Trova", "Latin---Vallenato",
    "Non-Music---Audiobook", "Non-Music---Interview",
    "Non-Music---Monolog", "Non-Music---Poetry", "Non-Music---Spoken Word",
    "Pop---Ballad", "Pop---Bubblegum", "Pop---City Pop",
    "Pop---Dance-pop", "Pop---Europop", "Pop---Folk Rock",
    "Pop---Funk", "Pop---Indie Pop", "Pop---J-pop",
    "Pop---K-pop", "Pop---Kayōkyoku", "Pop---Pop Rock",
    "Pop---Power Pop", "Pop---Psychedelic", "Pop---Soft Rock",
    "Pop---Synth-pop", "Pop---Teen Pop",
    "Reggae---Calypso", "Reggae---Dancehall", "Reggae---Dub",
    "Reggae---Lovers Rock", "Reggae---Ragga", "Reggae---Reggae",
    "Reggae---Reggae-Pop", "Reggae---Rocksteady", "Reggae---Roots Reggae",
    "Reggae---Ska", "Reggae---Soca",
    "Rock---AOR", "Rock---Acid Rock", "Rock---Acoustic",
    "Rock---Alternative Rock", "Rock---Arena Rock", "Rock---Art Rock",
    "Rock---Blues Rock", "Rock---Classic Rock", "Rock---Country Rock",
    "Rock---Disco", "Rock---Dream Pop", "Rock---Emo",
    "Rock---Experimental", "Rock---Folk Rock", "Rock---Funk Rock",
    "Rock---Garage Rock", "Rock---Glam", "Rock---Goth Rock",
    "Rock---Grunge", "Rock---Hard Rock", "Rock---Hardcore",
    "Rock---Heavy Metal", "Rock---Indie Rock", "Rock---Industrial",
    "Rock---Krautrock", "Rock---Lo-fi", "Rock---Math Rock",
    "Rock---Metal", "Rock---Mod", "Rock---Noise",
    "Rock---Oldies", "Rock---Pop Rock", "Rock---Post Rock",
    "Rock---Power Pop", "Rock---Progressive Rock", "Rock---Psychedelic Rock",
    "Rock---Punk", "Rock---Rockabilly", "Rock---Shoegaze",
    "Rock---Soft Rock", "Rock---Soul", "Rock---Southern Rock",
    "Rock---Spoken Word", "Rock---Surf", "Rock---Swamp Pop",
    "Rock---Technical Death Metal", "Rock---Thrash",
    "Stage & Screen---Musical", "Stage & Screen---Score", "Stage & Screen---Soundtrack",
    "Stage & Screen---Theme",
]


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

def analyze_audio(audio_path: Path) -> dict:
    """
    Analyze audio with essentia. Returns {bpm, key, camelot, energy}.
    Uses RhythmExtractor2013 for BPM (more accurate than librosa on electronic music)
    and HPCP-based KeyExtractor for key detection.
    """
    try:
        import essentia.standard as es  # type: ignore
    except ImportError:
        raise RuntimeError("essentia not installed — run: pip install essentia-tensorflow")

    audio = es.MonoLoader(filename=str(audio_path), sampleRate=44100)()

    # BPM — multifeature method handles tempo changes and syncopation better
    bpm, _, _, _, _ = es.RhythmExtractor2013(method="multifeature")(audio)
    bpm = round(float(bpm), 1)

    # Key
    key, scale, _ = es.KeyExtractor()(audio)
    camelot = _key_to_camelot(key, scale)

    # Energy — RMS normalized to 0-100 (same scale as existing cache)
    rms = float(es.RMS()(audio))
    energy = min(100, round(rms * 450))

    return {
        "bpm": bpm,
        "key": f"{key} {'maj' if scale == 'major' else 'min'}",
        "camelot": camelot,
        "energy": energy,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Genre classification
# ─────────────────────────────────────────────────────────────────────────────

_EFFNET_MODEL  = "discogs-effnet-bs64-1.pb"
_GENRE_MODEL   = "genre_discogs400-discogs-effnet-1.pb"
_MODEL_BASE_URL = "https://essentia.upf.edu/models"


def download_models():
    """
    Download essentia pre-trained model files if not already present.
    Call once during setup: python -c "from essentia_analysis import download_models; download_models()"
    """
    import urllib.request

    _ensure_dir(MODELS_DIR)

    models = {
        _EFFNET_MODEL: f"{_MODEL_BASE_URL}/feature-extractors/discogs-effnet/{_EFFNET_MODEL}",
        _GENRE_MODEL:  f"{_MODEL_BASE_URL}/classification-heads/genre_discogs400/{_GENRE_MODEL}",
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


def classify_genre(audio_path: Path, top_n: int = 5) -> list:
    """
    Classify genre using essentia's Discogs-400 model.
    Returns a list of up to top_n genre strings (e.g. ["Electronic---Techno", …]).
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

        top_indices = np.argsort(avg)[::-1][:top_n]
        return [DISCOGS400_LABELS[i] for i in top_indices if i < len(DISCOGS400_LABELS)]

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
