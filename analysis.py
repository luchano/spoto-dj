import asyncio
import json
import tempfile
from pathlib import Path
from typing import Optional

import httpx
import numpy as np

CACHE_FILE = Path(__file__).parent / ".audio_cache.json"

KEY_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

CAMELOT = {
    (0, 1): "8B",  (0, 0): "5A",
    (1, 1): "3B",  (1, 0): "12A",
    (2, 1): "10B", (2, 0): "7A",
    (3, 1): "5B",  (3, 0): "2A",
    (4, 1): "12B", (4, 0): "9A",
    (5, 1): "7B",  (5, 0): "4A",
    (6, 1): "2B",  (6, 0): "11A",
    (7, 1): "9B",  (7, 0): "6A",
    (8, 1): "4B",  (8, 0): "1A",
    (9, 1): "11B", (9, 0): "8A",
    (10, 1): "6B", (10, 0): "3A",
    (11, 1): "1B", (11, 0): "10A",
}

# Krumhansl-Schmuckler key profiles
_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.97, 2.69, 4.98, 4.00, 2.79, 3.34, 3.17])

_FFMPEG_DIR = "/opt/homebrew/bin"


def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_cache(cache: dict):
    # Atomic write: dump to a temp file in the same dir, then rename. Prevents a
    # truncated/corrupt cache if the process is killed mid-write — important
    # because the analysis loop saves after every track.
    tmp = CACHE_FILE.with_suffix(CACHE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(cache))
    tmp.replace(CACHE_FILE)


def _detect_key(chroma_mean: np.ndarray) -> tuple[int, int]:
    best_score = -np.inf
    best_key = (0, 1)
    for i in range(12):
        for mode, profile in enumerate([_MINOR, _MAJOR]):
            shifted = np.roll(profile, i)
            score = float(np.corrcoef(chroma_mean, shifted)[0, 1])
            if score > best_score:
                best_score = score
                best_key = (i, mode)
    return best_key


def _analyze_file(path: str) -> dict:
    import librosa

    y, sr = librosa.load(path, sr=22050, mono=True, duration=30.0)

    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    bpm = round(float(np.atleast_1d(tempo)[0]))

    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    pitch, mode = _detect_key(chroma.mean(axis=1))

    rms = librosa.feature.rms(y=y)[0]
    energy = min(100, round(float(rms.mean()) * 450))

    return {
        "bpm": bpm,
        "key": f"{KEY_NAMES[pitch]} {'maj' if mode == 1 else 'min'}",
        "camelot": CAMELOT.get((pitch, mode), "?"),
        "energy": energy,
    }


def _download_audio(title: str, artists: str) -> Optional[str]:
    """
    Search for 'artists - title' audio and download to a temp MP3.

    Strategy (tried in order):
      1. SoundCloud — fast, no SABR/403 issues
      2. YouTube    — fallback (affected by SABR on some videos but still works for many)
    """
    import yt_dlp

    query = f"{artists} - {title}"
    sources = [f"scsearch1:{query}", f"ytsearch1:{query}"]

    for search_url in sources:
        tmp_base = tempfile.mktemp()
        out_path = f"{tmp_base}.mp3"

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": f"{tmp_base}.%(ext)s",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "64",
            }],
            "ffmpeg_location": _FFMPEG_DIR,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 20,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([search_url])
            if Path(out_path).exists():
                return out_path
        except Exception:
            pass

        # Clean up any partial files yt-dlp may have left
        for leftover in Path(tempfile.gettempdir()).glob(f"{Path(tmp_base).name}*"):
            leftover.unlink(missing_ok=True)

    return None


async def analyze_track(
    track_id: str,
    title: str,
    artists: str,
    preview_url: str,
    semaphore: asyncio.Semaphore,
) -> tuple[str, dict]:
    """Analyze a track: use Spotify preview_url if available, else fall back to YouTube."""
    async with semaphore:
        tmp_path = None
        try:
            loop = asyncio.get_event_loop()

            if preview_url:
                async with httpx.AsyncClient(timeout=20) as client:
                    resp = await client.get(preview_url)
                    resp.raise_for_status()
                    audio_bytes = resp.content
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                    f.write(audio_bytes)
                    tmp_path = f.name
            else:
                tmp_path = await loop.run_in_executor(
                    None, _download_audio, title, artists
                )
                if not tmp_path:
                    return track_id, {"error": f"audio download failed for '{title}'"}

            result = await loop.run_in_executor(None, _analyze_file, tmp_path)
            return track_id, result

        except Exception as e:
            return track_id, {"error": str(e)}
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)
