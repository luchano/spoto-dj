#!/usr/bin/env python3
"""
One-shot migration: add genre_affinity, vocalness and timbre embeddings to
already-analyzed tracks, reusing the downloaded .ogg files.

Only the style models run (~4-6 s/track); BPM/key/loudness are untouched.
Resumable: entries already carrying the current STYLE_VERSION are skipped;
the cache is saved atomically after every track.

IMPORTANT: stop the web server first (./dev.sh stop). The analysis loop keeps
its own in-memory cache dict and rewrites the whole file on every completed
track, which would clobber this script's updates.

Run:  .venv/bin/python migrate_style.py
"""
import sys
import time
from pathlib import Path

from analysis import load_cache, save_cache
from essentia_analysis import AUDIO_DIR, STYLE_VERSION, analyze_style, download_models


def main() -> int:
    # Refuse to run while the dev server is up (cache write race).
    import subprocess
    check = subprocess.run(["pgrep", "-f", "uvicorn main:app"], capture_output=True)
    if check.returncode == 0:
        print("ERROR: the web server is running — stop it first (./dev.sh stop).")
        return 1

    download_models()   # ensures the voice model is present (no-op otherwise)

    cache = load_cache()

    todo = []
    for tid, entry in cache.items():
        if not isinstance(entry, dict) or "error" in entry or not entry.get("bpm"):
            continue
        if entry.get("style_version") == STYLE_VERSION:
            continue
        ogg = AUDIO_DIR / f"{tid}.ogg"
        if ogg.exists():
            todo.append((tid, ogg))

    print(f"{len(todo)} tracks to migrate ({len(cache)} in cache)")
    t0 = time.time()
    done = failed = 0
    for i, (tid, ogg) in enumerate(todo, 1):
        style = analyze_style(ogg)
        if not style:
            failed += 1
            print(f"  [{i}/{len(todo)}] {tid} FAILED style inference")
            continue
        entry = cache[tid]
        entry["track_genres"]   = style["track_genres"]
        entry["genre_affinity"] = style["genre_affinity"]
        entry["style_version"]  = style.get("style_version", 0)
        if style.get("vocalness") is not None:
            entry["vocalness"] = style["vocalness"]
        save_cache(cache)   # atomic, per track — safe to interrupt
        done += 1
        if i % 25 == 0 or i == len(todo):
            rate = (time.time() - t0) / i
            eta = rate * (len(todo) - i) / 60
            print(f"  [{i}/{len(todo)}] done={done} failed={failed}  eta {eta:.0f} min")

    print(f"Migration complete: {done} updated, {failed} failed, "
          f"{time.time()-t0:.0f}s total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
