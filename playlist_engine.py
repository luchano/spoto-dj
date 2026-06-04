"""
DJ Playlist Generator.

Builds sets following professional DJ principles:
  - Harmonic mixing via the Camelot wheel (key compatibility)
  - Smooth BPM transitions (±8 BPM per step)
  - Energy arc narrative: warmup / peak_time / afterhours profiles
  - Hit distribution: popular tracks placed at arc peaks (~25%)
  - Genre coherence with graceful fallback to full library
  - No same-artist back-to-back
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

PLAYLISTS_FILE = Path(__file__).parent / ".playlists.json"

# ---------------------------------------------------------------------------
# Camelot wheel
# ---------------------------------------------------------------------------

def camelot_neighbors(key: str) -> frozenset:
    """Return the set of Camelot keys that mix harmonically with *key*.

    Rules (standard harmonic mixing):
    - Same key      (perfect loop)
    - Same number, other letter  (relative major/minor switch)
    - Number ±1, same letter     (energy shift up/down the circle)
    Numbers wrap: 12 → 1 and 1 → 12.
    """
    if not key or key == "?":
        return frozenset()
    try:
        num = int(key[:-1])
        letter = key[-1].upper()
        if letter not in ("A", "B") or not 1 <= num <= 12:
            return frozenset()
    except (ValueError, IndexError):
        return frozenset()

    other = "B" if letter == "A" else "A"
    up   = (num % 12) + 1           # 12 → 1
    down = ((num - 2) % 12) + 1     # 1 → 12

    return frozenset({
        key,                 # same
        f"{num}{other}",     # relative major/minor
        f"{up}{letter}",     # +1 on wheel
        f"{down}{letter}",   # -1 on wheel
    })


def camelot_score(key_from: str, key_to: str) -> float:
    """
    Returns compatibility score:
      1.0  — same key or adjacent on wheel
      0.5  — same number, different letter (energy shift, can sound ok)
      0.3  — unknown key (neutral, not penalised heavily)
      0.0  — incompatible
    """
    if not key_from or not key_to or "?" in (key_from, key_to):
        return 0.3
    if key_to == key_from:
        return 1.0
    if key_to in camelot_neighbors(key_from):
        return 1.0
    # Same number, different letter but not already in neighbors means
    # it was filtered as non-standard; give partial credit
    try:
        if int(key_from[:-1]) == int(key_to[:-1]):
            return 0.5
    except (ValueError, IndexError):
        pass
    return 0.0


# ---------------------------------------------------------------------------
# Energy arc
# ---------------------------------------------------------------------------

# Each profile is a list of (position 0-1, energy 0-1) control points.
_ARC_PROFILES = {
    "warmup": [
        (0.00, 0.30),
        (0.35, 0.58),
        (0.65, 0.80),
        (0.85, 0.90),
        (1.00, 0.82),
    ],
    "peak_time": [
        (0.00, 0.52),
        (0.18, 0.78),
        (0.45, 1.00),
        (0.60, 0.82),   # brief breakdown
        (0.78, 0.96),   # second peak
        (1.00, 0.70),
    ],
    "afterhours": [
        (0.00, 0.82),
        (0.22, 1.00),
        (0.50, 0.68),
        (0.75, 0.52),
        (1.00, 0.36),
    ],
}


def build_energy_arc(n: int, profile: str = "peak_time") -> List[float]:
    """Return list of *n* energy targets in [0, 1] by linearly interpolating
    the named profile's control points."""
    points = _ARC_PROFILES.get(profile, _ARC_PROFILES["peak_time"])
    arc = []
    for i in range(n):
        pos = i / max(n - 1, 1)
        for j in range(len(points) - 1):
            p0, e0 = points[j]
            p1, e1 = points[j + 1]
            if p0 <= pos <= p1:
                t = (pos - p0) / (p1 - p0) if p1 > p0 else 0.0
                arc.append(round(e0 + t * (e1 - e0), 4))
                break
        else:
            arc.append(points[-1][1])
    return arc


# ---------------------------------------------------------------------------
# Genre clustering
# ---------------------------------------------------------------------------

_GENRE_KEYWORDS = {
    "electronic": {
        "electronic", "house", "techno", "edm", "dance", "electronica",
        "tech house", "deep house", "progressive house", "minimal", "trance",
        "drum and bass", "dnb", "dubstep", "electro", "synth", "disco",
        "microhouse", "ambient", "downtempo",
    },
    "hip_hop": {
        "hip hop", "hip-hop", "rap", "trap", "r&b", "rnb", "urban",
        "drill", "grime", "uk rap",
    },
    "latin": {
        "latin", "reggaeton", "salsa", "cumbia", "bachata", "latin pop",
        "dembow", "perreo", "tropical", "merengue",
    },
    "rock": {
        "rock", "indie", "alternative", "punk", "metal", "grunge",
        "shoegaze", "post-rock", "psychedelic",
    },
    "pop": {
        "pop", "dance pop", "electropop", "synth-pop", "k-pop",
        "teen pop", "pop rock",
    },
    "jazz": {
        "jazz", "blues", "soul", "funk", "bossa nova", "bebop",
        "neo soul", "r&b",
    },
}


def classify_genre(genres: list) -> Optional[str]:
    """Return the best genre cluster label, or None if not identifiable."""
    if not genres:
        return None
    text = " ".join(g.lower() for g in genres)
    scores = {}
    for cluster, keywords in _GENRE_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score:
            scores[cluster] = score
    return max(scores, key=scores.get) if scores else None


# ---------------------------------------------------------------------------
# Pool building
# ---------------------------------------------------------------------------

def build_pool(library: list, cache: dict) -> list:
    """Merge library tracks with analysed cache data.

    Only includes tracks that:
    - Have a successful cache entry (no error key)
    - Have a non-zero BPM (required for placement in the arc)
    """
    pool = []
    for t in library:
        tid = t["id"]
        analysis = cache.get(tid, {})
        if not analysis or "error" in analysis:
            continue
        # Cache is the source of truth for BPM; don't fall back to library value
        bpm = analysis.get("bpm") or 0
        if bpm == 0:
            continue

        artists_raw = t.get("artists", "")
        if isinstance(artists_raw, list):
            artists_list = artists_raw
            artists_str  = ", ".join(artists_raw)
        else:
            artists_str  = artists_raw
            artists_list = [a.strip() for a in artists_raw.split(",") if a.strip()]

        pool.append({
            "id":           tid,
            "title":        t.get("title", ""),
            "artists":      artists_str,
            "artists_list": artists_list,
            "bpm":          bpm,
            "camelot":      analysis.get("camelot") or t.get("camelot") or "?",
            "energy":       analysis.get("energy") or t.get("energy") or 0,
            "duration_ms":  t.get("duration_ms", 240_000),
            "popularity":   t.get("popularity", 0),
            "genres":       t.get("genres", []),
            "genre_cluster":classify_genre(t.get("genres", [])),
            "spotify_url":  t.get("spotify_url", ""),
            "image_url":    t.get("image_url", ""),
        })
    return pool


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_candidate(
    candidate: dict,
    prev: Optional[dict],
    target_energy: float,       # 0-1
    is_hit_slot: bool,
    recent_artists: set,        # lowercase artist names from last 2 tracks
) -> float:
    """Score a candidate track for the current arc position."""
    # --- Energy match ---
    e_norm = candidate["energy"] / 100.0
    energy_score = max(0.0, 1.0 - abs(e_norm - target_energy) / 0.4)

    # --- BPM smoothness ---
    if prev and prev["bpm"] > 0 and candidate["bpm"] > 0:
        bpm_diff = abs(prev["bpm"] - candidate["bpm"])
        bpm_score = max(0.0, 1.0 - bpm_diff / 10.0)
    else:
        bpm_score = 0.5

    # --- Camelot compatibility ---
    key_s = camelot_score(prev["camelot"] if prev else "?", candidate["camelot"])

    # --- Hit factor ---
    hit_s = (candidate["popularity"] / 100.0) if is_hit_slot else 0.0

    # --- Same-artist penalty ---
    artist_penalty = 0.0
    for a in candidate["artists_list"]:
        if a.lower() in recent_artists:
            artist_penalty = 0.5
            break

    raw = (
        key_s        * 0.35 +
        energy_score * 0.30 +
        bpm_score    * 0.20 +
        hit_s        * 0.15
    ) - artist_penalty

    return max(0.0, raw)


# ---------------------------------------------------------------------------
# Core generator
# ---------------------------------------------------------------------------

def generate(
    library: list,
    cache: dict,
    duration_min: int = 60,
    energy_profile: str = "peak_time",
    genre_filter: Optional[str] = None,
    hit_ratio: float = 0.25,
    bpm_range: Optional[Tuple[int, int]] = None,
) -> dict:
    """Generate a DJ set.

    Parameters
    ----------
    library       : track dicts from build_track_library()
    cache         : analysis cache dict from load_cache()
    duration_min  : target set length in minutes (40-180)
    energy_profile: "warmup" | "peak_time" | "afterhours"
    genre_filter  : optional genre cluster name to restrict pool
    hit_ratio     : fraction of slots reserved for popular tracks (popularity>65)
    bpm_range     : optional (min_bpm, max_bpm) tuple

    Returns a dict with keys:
        tracks            – ordered list of track dicts
        total_duration_ms – effective duration accounting for 15 s overlaps
        track_count       – number of tracks
        warnings          – list of human-readable warning strings
    """
    warnings: List[str] = []

    # --- Build and filter pool ---
    full_pool = build_pool(library, cache)
    if not full_pool:
        return {
            "error": "No analysed tracks available. Run analysis first.",
            "tracks": [], "total_duration_ms": 0, "track_count": 0, "warnings": [],
        }

    pool = full_pool

    if genre_filter:
        genre_pool = [t for t in full_pool if t["genre_cluster"] == genre_filter]
        needed = round(duration_min / 3.5) * 2
        if len(genre_pool) >= needed:
            pool = genre_pool
        else:
            warnings.append(
                f"Not enough '{genre_filter}' tracks ({len(genre_pool)} found, "
                f"need ~{needed}). Using full library."
            )

    if bpm_range:
        lo, hi = bpm_range
        bpm_pool = [t for t in pool if lo <= t["bpm"] <= hi]
        if len(bpm_pool) >= 8:
            pool = bpm_pool
        else:
            warnings.append(
                f"Only {len(bpm_pool)} tracks in BPM range {bpm_range}. Ignoring filter."
            )

    # --- Estimate track count ---
    # Effective track runtime ≈ 3.75 min (4 min avg - 15 s overlap)
    n_tracks = max(5, round(duration_min / 3.75))
    if len(pool) < n_tracks:
        n_tracks = len(pool)
        warnings.append(
            f"Only {len(pool)} eligible tracks; set will be shorter than requested."
        )

    # --- Energy arc and hit slots ---
    arc = build_energy_arc(n_tracks, energy_profile)
    hit_count = max(1, round(n_tracks * hit_ratio))
    # Hit slots = positions with highest target energy
    hit_slots = set(
        sorted(range(n_tracks), key=lambda i: arc[i], reverse=True)[:hit_count]
    )

    # --- Greedy track selection ---
    selected: List[dict] = []
    used_ids: set = set()
    recent_artists: set = set()

    # Opener: pick closest to arc[0] energy, bias towards popular tracks
    opener_pool = pool
    opener = min(
        opener_pool,
        key=lambda t: abs(t["energy"] / 100.0 - arc[0]) - t["popularity"] / 500.0,
    )
    selected.append(opener)
    used_ids.add(opener["id"])
    recent_artists = {a.lower() for a in opener["artists_list"]}

    for i in range(1, n_tracks):
        target   = arc[i]
        is_hit   = i in hit_slots
        prev     = selected[-1]
        candidates = [t for t in pool if t["id"] not in used_ids]
        if not candidates:
            break

        # Pass 1: BPM ±8 AND Camelot compatible
        strict = [
            t for t in candidates
            if abs(t["bpm"] - prev["bpm"]) <= 8
            and camelot_score(prev["camelot"], t["camelot"]) > 0
        ]
        # Pass 2: relax BPM to ±14
        if not strict:
            strict = [t for t in candidates if abs(t["bpm"] - prev["bpm"]) <= 14]
        # Pass 3: anything
        if not strict:
            strict = candidates

        best = max(
            strict,
            key=lambda t: score_candidate(t, prev, target, is_hit, recent_artists),
        )
        selected.append(best)
        used_ids.add(best["id"])
        # Rolling window of last 2 tracks' artists
        recent_artists = {
            a.lower()
            for track in selected[-2:]
            for a in track["artists_list"]
        }

    # --- Duration accounting ---
    overlap_ms  = max(0, len(selected) - 1) * 15_000
    total_ms    = max(0, sum(t["duration_ms"] for t in selected) - overlap_ms)

    return {
        "tracks":            selected,
        "total_duration_ms": total_ms,
        "track_count":       len(selected),
        "warnings":          warnings,
    }


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def load_playlists() -> dict:
    if PLAYLISTS_FILE.exists():
        try:
            return json.loads(PLAYLISTS_FILE.read_text())
        except Exception:
            pass
    return {}


def save_playlists(playlists: dict) -> None:
    PLAYLISTS_FILE.write_text(json.dumps(playlists, indent=2))


def create_playlist(result: dict, params: dict, name: Optional[str] = None) -> dict:
    """Persist a generate() result and return the stored playlist object."""
    pid = f"pl_{uuid.uuid4().hex[:8]}"

    tracks_out = []
    for i, t in enumerate(result["tracks"], 1):
        tracks_out.append({
            "position":   i,
            "spotify_id": t["id"],
            "title":      t["title"],
            "artists":    t["artists"],
            "bpm":        t["bpm"],
            "camelot":    t["camelot"],
            "energy":     t["energy"],
            "duration_ms":t["duration_ms"],
            "popularity": t["popularity"],
            "spotify_url":t.get("spotify_url", ""),
            "image_url":  t.get("image_url", ""),
        })

    duration_min = round(result["total_duration_ms"] / 60_000)
    profile_label = {
        "warmup": "Warm-Up",
        "peak_time": "Peak Time",
        "afterhours": "After Hours",
    }.get(params.get("energy_profile", "peak_time"), "DJ Set")

    auto_name = name or (
        f"{profile_label} {duration_min}min "
        f"— {datetime.now(timezone.utc).strftime('%b %d')}"
    )

    playlist = {
        "id":                   pid,
        "name":                 auto_name,
        "created_at":           datetime.now(timezone.utc).isoformat(),
        "params":               params,
        "tracks":               tracks_out,
        "total_duration_ms":    result["total_duration_ms"],
        "track_count":          result["track_count"],
        "warnings":             result.get("warnings", []),
        "spotify_playlist_id":  None,
        "spotify_playlist_url": None,
    }

    playlists = load_playlists()
    playlists[pid] = playlist
    save_playlists(playlists)
    return playlist


def delete_playlist(pid: str) -> bool:
    """Remove a playlist by id. Returns True if it existed."""
    playlists = load_playlists()
    if pid not in playlists:
        return False
    del playlists[pid]
    save_playlists(playlists)
    return True
