"""
DJ Playlist Generator — narrative arc edition.

Each set tells a story with 7 universal acts:

  Intro → Build → First Peak → Journey (valley) → Rise → Climax → Outro

The BPM range (min/max) provided by the user defines the intensity envelope
of the set. Section BPM targets are derived by scaling the factors against
the midpoint of that range:
  - Intro / Outro  →  near bpm_min  (calm opening and closing)
  - Climax         →  near bpm_max  (peak intensity)

Professional DJs structure sets this way to create tension & release, emotional
contrast, and memorable moments. The "valley" (mid-journey) is deliberate —
dropping energy before the climax makes the climax hit much harder.
"""

import json
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import List, Optional, Tuple

PLAYLISTS_FILE = Path(__file__).parent / ".playlists.json"


# ─────────────────────────────────────────────────────────────────────────────
# Camelot wheel
# ─────────────────────────────────────────────────────────────────────────────

def camelot_neighbors(key: str) -> frozenset:
    """Compatible keys for harmonic mixing.

    Returns keys that sound good when mixed together:
    - Same key (perfect loop)
    - Same number, other letter (relative major/minor)
    - Number ±1, same letter (energy shift along the circle)
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
    up   = (num % 12) + 1
    down = ((num - 2) % 12) + 1

    return frozenset({key, f"{num}{other}", f"{up}{letter}", f"{down}{letter}"})


def camelot_score(key_from: str, key_to: str) -> float:
    """Harmonic compatibility 0.0–1.0.

    1.0 — same key or adjacent on wheel
    0.5 — same number, different letter (acceptable energy shift)
    0.3 — unknown key (neutral, not penalised heavily)
    0.0 — incompatible
    """
    if not key_from or not key_to or "?" in (key_from, key_to):
        return 0.3
    if key_to == key_from:
        return 1.0
    if key_to in camelot_neighbors(key_from):
        return 1.0
    try:
        if int(key_from[:-1]) == int(key_to[:-1]):
            return 0.5
    except (ValueError, IndexError):
        pass
    return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Energy arc (kept for backward-compatibility / tests)
# ─────────────────────────────────────────────────────────────────────────────

_ARC_PROFILES = {
    "warmup": [
        (0.00, 0.30), (0.35, 0.58), (0.65, 0.80), (0.85, 0.90), (1.00, 0.82),
    ],
    "peak_time": [
        (0.00, 0.52), (0.18, 0.78), (0.45, 1.00),
        (0.60, 0.82), (0.78, 0.96), (1.00, 0.70),
    ],
    "afterhours": [
        (0.00, 0.82), (0.22, 1.00), (0.50, 0.68), (0.75, 0.52), (1.00, 0.36),
    ],
}


def build_energy_arc(n: int, profile: str = "peak_time") -> List[float]:
    """Return *n* energy targets in [0, 1] by interpolating profile control points."""
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


# ─────────────────────────────────────────────────────────────────────────────
# 7-Section narrative architecture
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SectionDef:
    name: str           # internal identifier
    label: str          # human-readable display name
    emoji: str
    weight: float       # fraction of total tracks (sums to 1.0 per profile)
    energy_min: int     # hard floor  0-100
    energy_max: int     # hard ceiling 0-100
    bpm_factor: float   # multiplier on the pool's base BPM
    pop_min: int        # popularity floor  (0 = unconstrained)
    pop_max: int        # popularity ceiling (100 = unconstrained)


@dataclass
class SectionSlot:
    section_name: str
    label: str
    emoji: str
    position: int       # 0-indexed in the full set
    bpm_target: int     # absolute BPM for this slot
    energy_min: int
    energy_max: int
    target_energy: float   # normalised midpoint (energy_min+max) / 200
    pop_min: int
    pop_max: int
    is_hit_slot: bool


# Universal 7-section narrative arc (weights sum to 1.0).
# BPM targets are derived at runtime from the user's bpm_range,
# not hardcoded per profile.
UNIVERSAL_SECTIONS: List[SectionDef] = [
    #              name           label          emoji  wt    emin emax bfactor pmin pmax
    SectionDef("intro",       "Intro",       "🎵", 0.12, 25,  50, 0.92,  0,  70),
    SectionDef("build",       "Build",       "📈", 0.18, 45,  70, 0.97,  0,  85),
    SectionDef("peak_a",      "First Peak",  "🔥", 0.12, 70,  90, 1.02, 55, 100),
    SectionDef("mid_journey", "Journey",     "🌊", 0.18, 50,  68, 0.95,  0,  75),
    SectionDef("escalation",  "Rise",        "⬆",  0.18, 65,  88, 1.02,  0,  90),
    SectionDef("climax",      "Climax",      "💥", 0.12, 82, 100, 1.07, 60, 100),
    SectionDef("outro",       "Outro",       "🌅", 0.10, 35,  62, 0.93,  0,  70),
]


def compute_base_bpm(pool: list, bpm_range: Optional[Tuple[int, int]] = None) -> int:
    """Return the median BPM of the pool (optionally filtered to bpm_range)."""
    bpms = [t["bpm"] for t in pool if t["bpm"] > 0]
    if bpm_range:
        lo, hi   = bpm_range
        filtered = [b for b in bpms if lo <= b <= hi]
        if filtered:
            bpms = filtered
    return round(median(bpms)) if bpms else 120


def distribute_sections(
    n_tracks: int,
    base_bpm: int,
    bpm_range: Optional[Tuple[int, int]] = None,
) -> List[SectionSlot]:
    """Assign *n_tracks* to the universal 7-section narrative arc.

    BPM targets are scaled from *base_bpm* using each section's factor,
    then clamped to *bpm_range* when provided so every slot stays within
    the requested BPM envelope.
    """
    defs      = UNIVERSAL_SECTIONS
    bpm_lo    = bpm_range[0] if bpm_range else 0
    bpm_hi    = bpm_range[1] if bpm_range else 9999

    # Proportional allocation, floor + fractional-remainder
    weights    = [d.weight for d in defs]
    total_w    = sum(weights)
    raw_counts = [w / total_w * n_tracks for w in weights]
    counts     = [int(c) for c in raw_counts]
    remainder  = n_tracks - sum(counts)
    fracs      = sorted(enumerate(raw_counts), key=lambda x: x[1] - int(x[1]), reverse=True)
    for i, _ in fracs[:remainder]:
        counts[i] += 1

    slots: List[SectionSlot] = []
    pos = 0
    for sec_def, count in zip(defs, counts):
        if count == 0:
            continue
        raw_bpm = round(base_bpm * sec_def.bpm_factor)
        bpm_t   = max(bpm_lo, min(bpm_hi, max(60, raw_bpm)))
        target_e = (sec_def.energy_min + sec_def.energy_max) / 200.0

        for j in range(count):
            is_hit = (
                (sec_def.name == "peak_a" and j == count - 1) or
                (sec_def.name == "climax" and j <= 1)
            )
            slots.append(SectionSlot(
                section_name  = sec_def.name,
                label         = sec_def.label,
                emoji         = sec_def.emoji,
                position      = pos,
                bpm_target    = bpm_t,
                energy_min    = sec_def.energy_min,
                energy_max    = sec_def.energy_max,
                target_energy = target_e,
                pop_min       = sec_def.pop_min,
                pop_max       = sec_def.pop_max,
                is_hit_slot   = is_hit,
            ))
            pos += 1

    return slots


def classify_track_role(track: dict, base_bpm: int) -> str:
    """Classify a track into a narrative role.

    Roles: opener | groove | build | anthem | emotional | climax | closer
    """
    bpm    = track["bpm"]
    energy = track["energy"]
    pop    = track["popularity"]
    ratio  = bpm / max(base_bpm, 1)

    if ratio < 0.92 and energy <= 52:
        return "opener"   # notably below base BPM, low energy — set-opening feel
    if ratio > 1.04 and energy >= 80 and pop >= 60:
        return "climax"
    if ratio >= 0.98 and energy >= 70 and pop >= 55:
        return "anthem"
    if energy <= 65 and ratio < 0.98:
        return "emotional"
    if 0.92 <= ratio < 0.97 and energy <= 60:
        return "closer"   # moderately below base, low-medium energy — wind-down feel
    if energy >= 50 and 0.93 <= ratio <= 1.03:
        return "groove"
    return "build"


# ─────────────────────────────────────────────────────────────────────────────
# Genre clustering
# ─────────────────────────────────────────────────────────────────────────────

import re

# Genre clusters: (key, human label, set of member tags in NORMALISED form).
# Normalised = lowercased with spaces/hyphens stripped, so "deep house",
# "deep-house" and "deephouse" all collapse to "deephouse" and match here.
# A tag may belong to several clusters (e.g. "indie pop" → indie AND pop);
# membership is intentionally overlapping so filtering stays inclusive.
# Order defines display order in the dropdown before the count-based sort.
GENRE_CLUSTERS = [
    ("electronic", "Electrónica", {
        "electronic", "electronica", "house", "deephouse", "techhouse",
        "chillhouse", "microhouse", "minimal", "minimaltechno", "techno",
        "dance", "electro", "edm", "idm", "kompakt", "trance", "drumandbass",
        "dnb", "dubstep", "progressivehouse", "breakbeat", "garage", "ukgarage",
        "futuregarage", "glitch", "electroclash", "electronicdance", "nudisco",
        "acidhouse", "melodichouse", "organichouse", "deeptech",
    }),
    ("downtempo", "Downtempo / Chill", {
        "downtempo", "chillout", "chill", "chillwave", "lounge", "triphop",
        "ambient", "lofi", "downbeat", "balearic", "easylistening",
    }),
    ("indie", "Indie", {
        "indie", "indierock", "indiepop", "dreampop", "indietronica",
        "bedroompop", "janglepop", "indiefolk",
    }),
    ("rock", "Rock", {
        "rock", "alternative", "alternativerock", "rockargentino", "punk",
        "postpunk", "postrock", "metal", "grunge", "shoegaze", "psychedelicrock",
        "psychedelic", "newwave", "garagerock", "hardrock", "classicrock",
        "bluesrock", "indierock", "poprock", "softrock",
    }),
    ("pop", "Pop", {
        "pop", "synthpop", "electropop", "artpop", "dancepop", "kpop",
        "poprock", "powerpop", "hyperpop", "dreampop", "indiepop", "bedroompop",
        "chamberpop",
    }),
    ("hip_hop", "Hip-Hop / R&B", {
        "hiphop", "rap", "trap", "poprap", "rnb", "r&b", "neosoul", "drill",
        "grime", "urban", "boombap", "conscioushiphop",
    }),
    ("jazz", "Jazz / Soul / Funk", {
        "jazz", "soul", "funk", "blues", "disco", "bossanova", "bebop",
        "neosoul", "swing", "bluesrock", "nujazz", "jazzfunk", "acidjazz",
        "motown", "souljazz",
    }),
    ("latin", "Latino", {
        "latin", "latinpop", "latinalternative", "latinelectronic", "cumbia",
        "digitalcumbia", "electrocumbia", "electrocumbe", "reggaeton", "dembow",
        "perreo", "salsa", "bachata", "tropical", "mpb", "latinjazz",
        "merengue", "champeta", "rockargentino", "bossanova",
    }),
    ("folk", "Folk / Acústico", {
        "folk", "acoustic", "indiefolk", "singersongwriter", "americana",
        "country", "countrypop", "folkpop", "folkrock", "altcountry",
        "bluegrass", "neofolk",
    }),
    ("world", "World / Reggae", {
        "world", "worldmusic", "reggae", "dub", "dancehall", "ska", "afrobeat",
        "balkan", "gypsy", "roots", "worldfusion", "afro",
    }),
    ("classical", "Clásica / Instrumental", {
        "classical", "piano", "instrumental", "soundtrack", "score",
        "neoclassical", "modernclassical", "orchestral", "contemporaryclassical",
    }),
]

# Human-readable label per cluster key, for the UI.
GENRE_LABELS = {key: label for key, label, _ in GENRE_CLUSTERS}

_YEAR_RE = re.compile(r"^(19|20)\d{2}s?$")


def _normalize_tag(tag: str) -> str:
    """Lowercase a tag and strip everything but a-z/0-9/&, so separator
    variants collapse ('deep house' / 'deep-house' → 'deephouse')."""
    return re.sub(r"[^a-z0-9&]", "", tag.lower())


def tags_to_clusters(genres: list) -> set:
    """Every cluster key that any of the given tags belongs to (may be empty)."""
    norm = {_normalize_tag(g) for g in genres if g}
    norm.discard("")
    return {
        key for key, _, members in GENRE_CLUSTERS
        if norm & members
    }


def classify_genre(genres: list) -> Optional[str]:
    """Single best-fit cluster (highest number of matching tags), or None.

    Kept for backward compatibility / display; filtering uses the full
    multi-membership set from tags_to_clusters() instead.
    """
    if not genres:
        return None
    norm = [_normalize_tag(g) for g in genres]
    norm = [n for n in norm if n]
    scores = {
        key: sum(1 for n in norm if n in members)
        for key, _, members in GENRE_CLUSTERS
    }
    best = max(scores, key=scores.get) if scores else None
    return best if best and scores[best] > 0 else None


def _track_tags(library_track: dict, analysis: dict) -> list:
    """Combine Spotify artist genres + Last.fm track tags for one track."""
    return list(library_track.get("genres", [])) + list(analysis.get("track_genres", []))


def genre_options(library: list, cache: dict, min_tracks: int = 8) -> list:
    """Dropdown options derived from the user's own library.

    Counts, per cluster, how many analysed tracks match it (using both Spotify
    and Last.fm tags), and returns only clusters with at least `min_tracks`,
    sorted by count descending. Shape: [{"value", "label", "count"}, ...].
    """
    counts: dict = {key: 0 for key, _, _ in GENRE_CLUSTERS}
    for t in library:
        analysis = cache.get(t["id"], {})
        if not analysis or "error" in analysis:
            continue
        if not (analysis.get("bpm") or 0):
            continue
        for cluster in tags_to_clusters(_track_tags(t, analysis)):
            counts[cluster] += 1

    options = [
        {"value": key, "label": GENRE_LABELS[key], "count": counts[key]}
        for key, _, _ in GENRE_CLUSTERS
        if counts[key] >= min_tracks
    ]
    options.sort(key=lambda o: o["count"], reverse=True)
    return options


# ─────────────────────────────────────────────────────────────────────────────
# Pool building
# ─────────────────────────────────────────────────────────────────────────────

def build_pool(library: list, cache: dict) -> list:
    """Merge library + cache. Excludes unanalysed tracks and zero-BPM tracks."""
    pool = []
    for t in library:
        tid      = t["id"]
        analysis = cache.get(tid, {})
        if not analysis or "error" in analysis:
            continue
        bpm = analysis.get("bpm") or 0   # cache is source of truth
        if bpm == 0:
            continue

        artists_raw = t.get("artists", "")
        if isinstance(artists_raw, list):
            artists_str  = ", ".join(artists_raw)
            artists_list = artists_raw
        else:
            artists_str  = artists_raw
            artists_list = [a.strip() for a in artists_raw.split(",") if a.strip()]

        tags = _track_tags(t, analysis)   # Spotify artist genres + Last.fm track tags
        pool.append({
            "id":             tid,
            "title":          t.get("title", ""),
            "artists":        artists_str,
            "artists_list":   artists_list,
            "bpm":            bpm,
            "camelot":        analysis.get("camelot") or t.get("camelot") or "?",
            "energy":         analysis.get("energy")  or t.get("energy")  or 0,
            "duration_ms":    t.get("duration_ms", 240_000),
            "popularity":     t.get("popularity", 0),
            "genres":         t.get("genres", []),
            "genre_cluster":  classify_genre(tags),      # primary, for display
            "genre_clusters": tags_to_clusters(tags),    # all clusters, for filtering
            "spotify_url":    t.get("spotify_url", ""),
            "image_url":      t.get("image_url", ""),
        })
    return pool


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────

def score_candidate(
    candidate: dict,
    prev: Optional[dict],
    target_energy: float,   # normalised 0-1 (section midpoint)
    bpm_target: int,        # absolute BPM target for this section slot
    is_hit_slot: bool,
    recent_artists: set,    # lowercase artist names from last 2 tracks
) -> float:
    """Score a candidate for its section slot. Higher = better fit."""

    # Energy match to section midpoint
    e_norm       = candidate["energy"] / 100.0
    energy_score = max(0.0, 1.0 - abs(e_norm - target_energy) / 0.4)

    # BPM: blend transition smoothness AND on-target accuracy
    bpm_c = candidate["bpm"]
    if prev and prev["bpm"] > 0 and bpm_c > 0:
        smooth = max(0.0, 1.0 - abs(prev["bpm"] - bpm_c) / 10.0)
    else:
        smooth = 0.5
    on_target = max(0.0, 1.0 - abs(bpm_target - bpm_c) / 15.0)
    bpm_score = smooth * 0.5 + on_target * 0.5

    # Harmonic compatibility
    key_s = camelot_score(prev["camelot"] if prev else "?", candidate["camelot"])

    # Hit factor (popularity bonus at designated hit slots)
    hit_s = (candidate["popularity"] / 100.0) if is_hit_slot else 0.0

    # Same-artist penalty
    artist_penalty = 0.5 if any(
        a.lower() in recent_artists for a in candidate["artists_list"]
    ) else 0.0

    raw = (
        key_s        * 0.35 +
        energy_score * 0.25 +
        bpm_score    * 0.25 +
        hit_s        * 0.15
    ) - artist_penalty

    return max(0.0, raw)


# ─────────────────────────────────────────────────────────────────────────────
# Core generator
# ─────────────────────────────────────────────────────────────────────────────

def _filter_for_slot(
    candidates: list,
    slot: SectionSlot,
    prev: Optional[dict],
    energy_tol: int = 0,
    pop_tol: int    = 0,
    bpm_tol: int    = 12,
) -> list:
    """Hard-filter candidates to match a section slot's constraints."""
    result = []
    for t in candidates:
        if not (slot.energy_min - energy_tol <= t["energy"] <= slot.energy_max + energy_tol):
            continue
        if not (slot.pop_min - pop_tol <= t["popularity"] <= slot.pop_max + pop_tol):
            continue
        if abs(t["bpm"] - slot.bpm_target) > bpm_tol:
            continue
        result.append(t)
    return result


def _sections_summary(tracks: list) -> list:
    """Build ordered section summary for the UI."""
    seen: Dict[str, dict] = {}
    for t in tracks:
        name = t.get("section", "")
        if name and name not in seen:
            seen[name] = {
                "name":           name,
                "label":          t.get("section_label", name),
                "emoji":          t.get("section_emoji", ""),
                "start_position": t.get("position", len(seen) + 1),
                "count":          0,
            }
        if name:
            seen[name]["count"] += 1
    return list(seen.values())


def generate(
    library: list,
    cache: dict,
    duration_min: int                   = 60,
    genre_filter: Optional[str]         = None,
    bpm_range: Optional[Tuple[int,int]] = None,
) -> dict:
    """Generate a narrative-arc DJ set.

    Parameters
    ----------
    library      : track dicts from build_track_library()
    cache        : analysis cache from load_cache()
    duration_min : target set length in minutes (40-180)
    genre_filter : optional genre cluster to restrict pool
    bpm_range    : (min_bpm, max_bpm) — defines the intensity envelope.
                   Section BPM targets are scaled within this range.
                   When omitted, the pool's full BPM range is used.

    Returns a dict with keys:
        tracks, total_duration_ms, track_count, warnings, sections
    """
    warnings: List[str] = []

    # ── Build pool ───────────────────────────────────────────────────────────
    full_pool = build_pool(library, cache)
    if not full_pool:
        return {
            "error":    "No analysed tracks available. Run analysis first.",
            "tracks":   [], "total_duration_ms": 0, "track_count": 0,
            "warnings": [], "sections": [],
        }

    pool = full_pool

    if genre_filter:
        # Strict: only tracks belonging to the chosen genre's cluster. If there
        # aren't enough for the requested duration we warn and build a shorter
        # set — we never pull in tracks from outside the cluster.
        genre_pool = [t for t in full_pool if genre_filter in t["genre_clusters"]]
        label = GENRE_LABELS.get(genre_filter, genre_filter)
        if not genre_pool:
            return {
                "error":    f"No hay canciones del género '{label}' en tu librería analizada.",
                "tracks":   [], "total_duration_ms": 0, "track_count": 0,
                "warnings": [], "sections": [],
            }
        needed = round(duration_min / 3.5) * 2
        if len(genre_pool) < needed:
            warnings.append(
                f"Solo {len(genre_pool)} canciones de '{label}' — el set será más corto "
                "que la duración pedida (no se agregan canciones de otros géneros)."
            )
        pool = genre_pool

    if bpm_range:
        lo, hi   = bpm_range
        bpm_pool = [t for t in pool if lo <= t["bpm"] <= hi]
        if len(bpm_pool) == 0:
            warnings.append(
                f"No tracks found in BPM {bpm_range[0]}–{bpm_range[1]}. "
                "Ignoring BPM filter."
            )
            bpm_range = None   # don't clamp section BPM targets either
        else:
            if len(bpm_pool) < 8:
                warnings.append(
                    f"Solo {len(bpm_pool)} canciones en BPM {bpm_range[0]}–{bpm_range[1]}; "
                    "el set será más corto que la duración pedida."
                )
            pool = bpm_pool

    # ── Set-wide parameters ──────────────────────────────────────────────────
    n_tracks = max(5, round(duration_min / 3.75))
    if len(pool) < n_tracks:
        n_tracks = len(pool)
        warnings.append(
            f"Only {len(pool)} eligible tracks; set will be shorter than requested."
        )

    base_bpm = compute_base_bpm(pool, bpm_range)
    slots    = distribute_sections(n_tracks, base_bpm, bpm_range)

    # ── Greedy section-by-section selection ─────────────────────────────────
    selected: List[dict]  = []
    used_ids: set         = set()
    recent_artists: set   = set()

    for slot in slots:
        prev        = selected[-1] if selected else None
        available   = [t for t in pool if t["id"] not in used_ids]
        if not available:
            break

        # Progressive constraint relaxation
        candidates = _filter_for_slot(available, slot, prev, 0,  0, 12)
        if not candidates:
            candidates = _filter_for_slot(available, slot, prev, 10, 15, 20)
        if not candidates:
            candidates = _filter_for_slot(available, slot, prev, 20, 30, 35)
        if not candidates:
            candidates = available  # last resort — no hard constraints

        best = max(candidates, key=lambda t: score_candidate(
            t, prev, slot.target_energy, slot.bpm_target, slot.is_hit_slot, recent_artists,
        ))

        selected.append({
            **best,
            "section":       slot.section_name,
            "section_label": slot.label,
            "section_emoji": slot.emoji,
            "position":      slot.position + 1,   # 1-indexed for display
        })
        used_ids.add(best["id"])
        recent_artists = {
            a.lower()
            for track in selected[-2:]
            for a in track["artists_list"]
        }

    # ── Duration ──────────────────────────────────────────────────────────────
    overlap_ms = max(0, len(selected) - 1) * 15_000
    total_ms   = max(0, sum(t["duration_ms"] for t in selected) - overlap_ms)

    return {
        "tracks":            selected,
        "total_duration_ms": total_ms,
        "track_count":       len(selected),
        "warnings":          warnings,
        "sections":          _sections_summary(selected),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────────

def load_playlists() -> dict:
    if PLAYLISTS_FILE.exists():
        try:
            return json.loads(PLAYLISTS_FILE.read_text())
        except Exception:
            pass
    return {}


def save_playlists(playlists: dict) -> None:
    PLAYLISTS_FILE.write_text(json.dumps(playlists, indent=2))


def _dominant_genre(tracks: list) -> str:
    """Return the most-common genre tag across all tracks, title-cased.

    Prefers the ``genres`` field which, after the /api/tracks merge, already
    holds Last.fm per-track tags when available and Spotify artist genres
    otherwise.  Returns an empty string when no genre data is present.
    """
    counts: Counter = Counter()
    for t in tracks:
        tags = t.get("genres") or []
        if isinstance(tags, str):
            tags = [g.strip() for g in tags.split(",") if g.strip()]
        for tag in tags:
            if tag:
                counts[tag.lower()] += 1
    if not counts:
        return ""
    return counts.most_common(1)[0][0].title()


def create_playlist(result: dict, params: dict, name: Optional[str] = None) -> dict:
    """Persist a generate() result and return the stored playlist object.

    The automatic name follows the pattern:
        [Prefix · ] Genre · Xmin · BPMmin–BPMmax BPM · Mon DD

    ``name`` is treated as an optional **prefix** prepended before the
    auto-generated parts, not as the full name.
    """
    pid = f"pl_{uuid.uuid4().hex[:8]}"

    tracks_out = []
    for i, t in enumerate(result["tracks"], 1):
        tracks_out.append({
            "position":      i,
            "spotify_id":    t["id"],
            "title":         t["title"],
            "artists":       t["artists"],
            "bpm":           t["bpm"],
            "camelot":       t["camelot"],
            "energy":        t["energy"],
            "duration_ms":   t["duration_ms"],
            "popularity":    t["popularity"],
            "section":       t.get("section", ""),
            "section_label": t.get("section_label", ""),
            "section_emoji": t.get("section_emoji", ""),
            "spotify_url":   t.get("spotify_url", ""),
            "image_url":     t.get("image_url", ""),
        })

    # ── Auto-name components ─────────────────────────────────────────────────
    duration_min = round(result["total_duration_ms"] / 60_000)

    bpm_range = params.get("bpm_range")
    if bpm_range:
        bpm_min, bpm_max = int(bpm_range[0]), int(bpm_range[1])
    else:
        bpms = [
            t["bpm"] for t in result["tracks"]
            if isinstance(t.get("bpm"), (int, float)) and t["bpm"] > 0
        ]
        bpm_min = int(min(bpms)) if bpms else 0
        bpm_max = int(max(bpms)) if bpms else 0

    genre    = _dominant_genre(result["tracks"])
    date_str = datetime.now(timezone.utc).strftime("%b %d")

    parts: List[str] = []
    if genre:
        parts.append(genre)
    parts.append(f"{duration_min}min")
    if bpm_min and bpm_max:
        parts.append(f"{bpm_min}–{bpm_max} BPM")
    parts.append(date_str)

    auto_name = " · ".join(parts)
    full_name = f"{name} · {auto_name}" if name else auto_name

    playlist = {
        "id":                   pid,
        "name":                 full_name,
        "created_at":           datetime.now(timezone.utc).isoformat(),
        "params":               params,
        "tracks":               tracks_out,
        "sections":             result.get("sections", []),
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
    playlists = load_playlists()
    if pid not in playlists:
        return False
    del playlists[pid]
    save_playlists(playlists)
    return True
