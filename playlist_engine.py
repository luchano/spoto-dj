"""
DJ Playlist Generator — narrative arc edition.

Each set tells a story with 7 acts:

  peak_time:   Intro → Build → First Peak → Journey (valley) → Rise → Climax → Outro
  warmup:      Opening → Build → Lift → Groove → Rise → Peak → Outro
  afterhours:  In the Zone → Peak → Descent → Deep → Drift → Closing → Fade

Professional DJs structure sets this way to create tension & release, emotional
contrast, and memorable moments. The "valley" (mid-journey) is deliberate —
dropping energy before the climax makes the climax hit much harder.

Data used per track:
  bpm        → BPM arc (absolute targets per section)
  energy     → energy arc (section energy ranges)
  camelot    → harmonic transitions (Camelot wheel)
  popularity → hit placement (required/avoided per section)
  duration_ms→ set duration calculation
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Dict, List, Optional, Tuple

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


# Each profile defines 7 sections that sum weights to 1.0
SECTION_PROFILES: Dict[str, List[SectionDef]] = {
    # ── Peak-time: full narrative arc, most popular profile ──────────────────
    "peak_time": [
        SectionDef("intro",       "Intro",       "🎵", 0.12, 25, 50, 0.92,  0,  70),
        SectionDef("build",       "Build",       "📈", 0.18, 45, 70, 0.97,  0,  85),
        SectionDef("peak_a",      "First Peak",  "🔥", 0.12, 70, 90, 1.02, 55, 100),
        SectionDef("mid_journey", "Journey",     "🌊", 0.18, 50, 68, 0.95,  0,  75),
        SectionDef("escalation",  "Rise",        "⬆",  0.18, 65, 88, 1.02,  0,  90),
        SectionDef("climax",      "Climax",      "💥", 0.12, 82, 100, 1.07, 60, 100),
        SectionDef("outro",       "Outro",       "🌅", 0.10, 35, 62, 0.93,  0,  70),
    ],
    # ── Warm-up: starts gentle, peaks in the latter half ─────────────────────
    "warmup": [
        SectionDef("intro",       "Opening",     "🎵", 0.15, 20, 42, 0.88,  0,  65),
        SectionDef("build",       "Build",       "📈", 0.22, 35, 60, 0.94,  0,  80),
        SectionDef("peak_a",      "Lift",        "🔥", 0.15, 55, 75, 1.00, 45, 100),
        SectionDef("mid_journey", "Groove",      "🌊", 0.18, 45, 65, 0.96,  0,  72),
        SectionDef("escalation",  "Rise",        "⬆",  0.15, 60, 80, 1.00,  0,  88),
        SectionDef("climax",      "Peak",        "💥", 0.10, 70, 92, 1.04, 55, 100),
        SectionDef("outro",       "Outro",       "🌅", 0.05, 30, 55, 0.92,  0,  65),
    ],
    # ── After-hours: high energy opening, descends into a deep journey ────────
    "afterhours": [
        SectionDef("intro",       "In the Zone", "🌙", 0.12, 65, 88, 1.02, 50, 100),
        SectionDef("build",       "Peak",        "💫", 0.15, 80, 100, 1.06, 60, 100),
        SectionDef("peak_a",      "Descent",     "🌊", 0.18, 58, 80, 0.99,  0,  90),
        SectionDef("mid_journey", "Deep",        "🔮", 0.20, 42, 65, 0.95,  0,  72),
        SectionDef("escalation",  "Drift",       "✨",  0.18, 32, 58, 0.93,  0,  78),
        SectionDef("climax",      "Closing",     "🌅", 0.10, 22, 50, 0.90,  0,  65),
        SectionDef("outro",       "Fade",        "🌃", 0.07, 15, 40, 0.87,  0,  55),
    ],
}


def compute_base_bpm(pool: list) -> int:
    """Return the median BPM of the pool, used as the reference for section targets."""
    bpms = [t["bpm"] for t in pool if t["bpm"] > 0]
    if not bpms:
        return 120
    return round(median(bpms))


def distribute_sections(n_tracks: int, profile: str, base_bpm: int) -> List[SectionSlot]:
    """Assign *n_tracks* to sections and return ordered SectionSlot list.

    Each section gets at least 0 tracks; sections with weight>0 are preferred.
    Hit slots are placed at the climactic positions of each "peak" section.
    """
    defs = SECTION_PROFILES.get(profile, SECTION_PROFILES["peak_time"])

    # Proportional allocation with floor + fractional-remainder distribution
    weights     = [d.weight for d in defs]
    total_w     = sum(weights)
    raw_counts  = [w / total_w * n_tracks for w in weights]
    counts      = [int(c) for c in raw_counts]
    remainder   = n_tracks - sum(counts)

    fracs = sorted(enumerate(raw_counts), key=lambda x: x[1] - int(x[1]), reverse=True)
    for i, _ in fracs[:remainder]:
        counts[i] += 1

    # Build slot list
    slots: List[SectionSlot] = []
    pos = 0
    for sec_def, count in zip(defs, counts):
        if count == 0:
            continue
        bpm_t    = max(60, round(base_bpm * sec_def.bpm_factor))
        target_e = (sec_def.energy_min + sec_def.energy_max) / 200.0  # normalised

        for j in range(count):
            # Hit slots: last position in first-peak, first 2 positions in climax
            is_hit = (
                (sec_def.name == "peak_a"  and j == count - 1) or
                (sec_def.name == "climax"  and j <= 1) or
                (sec_def.name == "build"   and sec_def.pop_min >= 40 and j == count - 1)
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

_GENRE_KEYWORDS = {
    "electronic": {
        "electronic", "house", "techno", "edm", "dance", "electronica",
        "tech house", "deep house", "progressive house", "minimal", "trance",
        "drum and bass", "dnb", "dubstep", "electro", "synth", "disco",
        "microhouse", "ambient", "downtempo",
    },
    "hip_hop": {"hip hop", "hip-hop", "rap", "trap", "r&b", "rnb", "urban", "drill", "grime"},
    "latin":   {"latin", "reggaeton", "salsa", "cumbia", "bachata", "latin pop", "dembow", "perreo", "tropical"},
    "rock":    {"rock", "indie", "alternative", "punk", "metal", "grunge", "shoegaze", "post-rock"},
    "pop":     {"pop", "dance pop", "electropop", "synth-pop", "k-pop", "teen pop", "pop rock"},
    "jazz":    {"jazz", "blues", "soul", "funk", "bossa nova", "bebop", "neo soul"},
}


def classify_genre(genres: list) -> Optional[str]:
    if not genres:
        return None
    text = " ".join(g.lower() for g in genres)
    scores = {
        cluster: sum(1 for kw in keywords if kw in text)
        for cluster, keywords in _GENRE_KEYWORDS.items()
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else None


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

        pool.append({
            "id":            tid,
            "title":         t.get("title", ""),
            "artists":       artists_str,
            "artists_list":  artists_list,
            "bpm":           bpm,
            "camelot":       analysis.get("camelot") or t.get("camelot") or "?",
            "energy":        analysis.get("energy")  or t.get("energy")  or 0,
            "duration_ms":   t.get("duration_ms", 240_000),
            "popularity":    t.get("popularity", 0),
            "genres":        t.get("genres", []),
            "genre_cluster": classify_genre(t.get("genres", [])),
            "spotify_url":   t.get("spotify_url", ""),
            "image_url":     t.get("image_url", ""),
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
    energy_profile: str                 = "peak_time",
    genre_filter: Optional[str]         = None,
    hit_ratio: float                    = 0.25,   # kept for API compatibility
    bpm_range: Optional[Tuple[int,int]] = None,
) -> dict:
    """Generate a narrative-arc DJ set.

    Returns a dict with keys:
        tracks            – ordered list of enriched track dicts (with section info)
        total_duration_ms – effective duration (accounting for 15 s overlaps)
        track_count
        warnings          – human-readable warning strings
        sections          – ordered list of section summary dicts for the UI
    """
    warnings: List[str] = []

    # ── Build and filter pool ────────────────────────────────────────────────
    full_pool = build_pool(library, cache)
    if not full_pool:
        return {
            "error":   "No analysed tracks available. Run analysis first.",
            "tracks":  [], "total_duration_ms": 0, "track_count": 0,
            "warnings": [], "sections": [],
        }

    pool = full_pool

    if genre_filter:
        genre_pool = [t for t in full_pool if t["genre_cluster"] == genre_filter]
        needed = round(duration_min / 3.5) * 2
        if len(genre_pool) >= needed:
            pool = genre_pool
        else:
            warnings.append(
                f"Not enough '{genre_filter}' tracks ({len(genre_pool)}, need ~{needed}). "
                "Using full library."
            )

    if bpm_range:
        lo, hi    = bpm_range
        bpm_pool  = [t for t in pool if lo <= t["bpm"] <= hi]
        if len(bpm_pool) >= 8:
            pool = bpm_pool
        else:
            warnings.append(
                f"Only {len(bpm_pool)} tracks in BPM {bpm_range}. Ignoring filter."
            )

    # ── Set-wide parameters ──────────────────────────────────────────────────
    n_tracks = max(5, round(duration_min / 3.75))
    if len(pool) < n_tracks:
        n_tracks = len(pool)
        warnings.append(
            f"Only {len(pool)} eligible tracks; set will be shorter than requested."
        )

    base_bpm = compute_base_bpm(pool)
    slots    = distribute_sections(n_tracks, energy_profile, base_bpm)

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


def create_playlist(result: dict, params: dict, name: Optional[str] = None) -> dict:
    """Persist a generate() result and return the stored playlist object."""
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

    profile_label = {
        "warmup": "Warm-Up", "peak_time": "Peak Time", "afterhours": "After Hours",
    }.get(params.get("energy_profile", "peak_time"), "DJ Set")

    duration_min = round(result["total_duration_ms"] / 60_000)
    auto_name    = name or (
        f"{profile_label} {duration_min}min — {datetime.now(timezone.utc).strftime('%b %d')}"
    )

    playlist = {
        "id":                   pid,
        "name":                 auto_name,
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
