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
# BPM factors follow DJ practice: the tempo arc climbs gently (~10-15 BPM net
# per hour, "BPM creep") and the mid-set valley is expressed through ENERGY,
# not through a tempo dive — "a BPM jump is a genre change, not an energy
# change". Energy ranges carve the narrative; tempo stays mixable throughout.
UNIVERSAL_SECTIONS: List[SectionDef] = [
    #              name           label          emoji  wt    emin emax bfactor pmin pmax
    SectionDef("intro",       "Intro",       "🎵", 0.12, 25,  50, 0.94,  0,  70),
    SectionDef("build",       "Build",       "📈", 0.18, 45,  70, 0.97,  0,  85),
    SectionDef("peak_a",      "First Peak",  "🔥", 0.12, 70,  90, 1.00, 55, 100),
    SectionDef("mid_journey", "Journey",     "🌊", 0.18, 50,  68, 0.98,  0,  75),
    SectionDef("escalation",  "Rise",        "⬆",  0.18, 65,  88, 1.02,  0,  90),
    SectionDef("climax",      "Climax",      "💥", 0.12, 82, 100, 1.05, 60, 100),
    SectionDef("outro",       "Outro",       "🌅", 0.10, 35,  62, 0.97,  0,  70),
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

    # ── Piecewise-linear BPM/energy trajectory across ALL slots ─────────────
    # Flat per-section targets produce cliff-edge jumps at section boundaries
    # (the "127→108" problem). Instead, each section anchors its target at its
    # midpoint slot and every slot interpolates between neighboring anchors,
    # so consecutive slot targets never differ by more than a couple of BPM —
    # the "BPM creep" DJs actually use.
    anchors = []   # (midpoint_position, anchor_bpm, anchor_energy)
    pos_cursor = 0
    for sec_def, count in zip(defs, counts):
        if count == 0:
            continue
        raw_bpm  = base_bpm * sec_def.bpm_factor
        anchor_b = max(bpm_lo, min(bpm_hi, max(60.0, raw_bpm)))
        anchor_e = (sec_def.energy_min + sec_def.energy_max) / 200.0
        anchors.append((pos_cursor + (count - 1) / 2.0, anchor_b, anchor_e))
        pos_cursor += count

    def _interp(p: float) -> Tuple[float, float]:
        if p <= anchors[0][0]:
            return anchors[0][1], anchors[0][2]
        if p >= anchors[-1][0]:
            return anchors[-1][1], anchors[-1][2]
        for (p0, b0, e0), (p1, b1, e1) in zip(anchors, anchors[1:]):
            if p0 <= p <= p1:
                t = (p - p0) / (p1 - p0) if p1 > p0 else 0.0
                return b0 + t * (b1 - b0), e0 + t * (e1 - e0)
        return anchors[-1][1], anchors[-1][2]

    slots: List[SectionSlot] = []
    pos = 0
    for sec_def, count in zip(defs, counts):
        if count == 0:
            continue

        for j in range(count):
            is_hit = (
                (sec_def.name == "peak_a" and j == count - 1) or
                (sec_def.name == "climax" and j <= 1)
            )
            bpm_t, target_e = _interp(float(pos))
            slots.append(SectionSlot(
                section_name  = sec_def.name,
                label         = sec_def.label,
                emoji         = sec_def.emoji,
                position      = pos,
                bpm_target    = round(bpm_t),
                energy_min    = sec_def.energy_min,
                energy_max    = sec_def.energy_max,
                target_energy = round(target_e, 4),
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


def _expand_tag(tag: str) -> list:
    """Split compound genre tags into their component genres.

    Discogs parent genres arrive as compounds ('Folk, World, & Country',
    'Funk / Soul') that match no cluster as one token but whose components do.
    Tags without separators pass through untouched (so 'r&b' stays intact —
    we only split on ',' and '/', never on '&')."""
    if "," in tag or "/" in tag:
        return [p.strip(" &") for p in re.split(r"[,/]", tag) if p.strip(" &")]
    return [tag]


def tags_to_clusters(genres: list) -> set:
    """Every cluster key that any of the given tags belongs to (may be empty)."""
    norm = {_normalize_tag(p) for g in genres if g for p in _expand_tag(g)}
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
    norm = [_normalize_tag(p) for g in genres if g for p in _expand_tag(g)]
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


# ── Affinity-based genre membership ──────────────────────────────────────────
# When the local pipeline stored per-cluster probability mass (genre_affinity),
# membership means "a meaningful share of what the model heard", not "one weak
# tag matched". Thresholds calibrated on the real library: the chosen cluster
# must hold at least AFFINITY_MIN_ABS of probability mass AND be at least
# AFFINITY_MIN_REL of the track's dominant cluster.
AFFINITY_MIN_ABS = 0.10
AFFINITY_MIN_REL = 0.40


def cluster_membership(analysis: dict, tags: list) -> set:
    """Clusters a track genuinely belongs to.

    Prefers audio-derived probability mass (genre_affinity) when the local
    pipeline provides it; falls back to binary tag matching for legacy entries.
    """
    affinity = analysis.get("genre_affinity")
    if affinity:
        top = max(affinity.values())
        if top > 0:
            passed = {
                c for c, v in affinity.items()
                if v >= AFFINITY_MIN_ABS and v >= AFFINITY_MIN_REL * top
            }
            if passed:
                return passed
        # No cluster cleared the thresholds → the model's read is too diffuse
        # to trust; fall back to tags exactly like entries with no affinity at
        # all (otherwise slightly-more-signal would mean LESS membership).
    return tags_to_clusters(tags)


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
        for cluster in cluster_membership(analysis, _track_tags(t, analysis)):
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

        tags = _track_tags(t, analysis)   # Spotify artist genres + analyzed track tags
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
            # spotify.py stores year as a STRING ("2019", or "?" when unknown)
            # — coerce here or the era-cohesion comparison raises TypeError.
            "year":           int(t["year"]) if str(t.get("year", "")).strip().isdigit() else 0,
            "vocalness":      analysis.get("vocalness"),
            "genres":         t.get("genres", []),
            "genre_cluster":  classify_genre(tags),      # primary, for display
            "genre_clusters": cluster_membership(analysis, tags),  # affinity-aware
            "spotify_url":    t.get("spotify_url", ""),
            "image_url":      t.get("image_url", ""),
        })
    return pool


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────

# ── Tempo / transition primitives ────────────────────────────────────────────
# Grounded in DJ practice research: adjacent beatmatched tracks should differ
# by ~1-3 BPM ideally, ≤6% hard cap (pitch faders default ±6-10%, keylock
# artifacts appear past ~5-6%). Rules are PERCENT-based, never absolute BPM.

TRANSITION_HARD_PCT  = 0.06   # normal adjacency ceiling (~7.5 BPM at 125)
TRANSITION_RELAX_PCT = 0.08   # emergency ceiling before flagging a reset
MAX_RESETS_PER_SET   = 1      # big-jump "events" allowed per set (labeled)


def tempo_distance_pct(bpm_a: float, bpm_b: float) -> float:
    """Relative tempo distance between two tracks, octave-normalized.

    Half/double-time mixing keeps beats aligned (128→64 beatmatches cleanly),
    so 2:1 ratios count as close. Returns a fraction (0.05 == 5%)."""
    if bpm_a <= 0 or bpm_b <= 0:
        return 9.99
    best = 9.99
    for mult in (0.5, 1.0, 2.0):
        hi = max(bpm_a, bpm_b * mult)
        lo = min(bpm_a, bpm_b * mult)
        best = min(best, hi / lo - 1.0)
    return best


def transition_smoothness(prev_bpm: float, next_bpm: float) -> float:
    """0-1 score for how beatmatchable a transition is.

    Uses Ishizaki et al.'s asymmetric discomfort weighting: listeners tolerate
    speeding up slightly more than slowing down (a=0.765 up, b=1.0 down)."""
    if prev_bpm <= 0 or next_bpm <= 0:
        return 0.7   # unknown — neutral-ish
    # Fold octaves first so 128→64 counts as a perfect ratio.
    f_candidates = (next_bpm / prev_bpm, next_bpm * 2 / prev_bpm, next_bpm / 2 / prev_bpm)
    f = min(f_candidates, key=lambda r: abs(r - 1.0))
    discomfort = (f - 1.0) * 0.765 if f >= 1.0 else (1.0 / f - 1.0) * 1.0
    return max(0.0, 1.0 - discomfort / TRANSITION_HARD_PCT)


def harmonic_score(key_from: str, key_to: str) -> float:
    """Camelot compatibility per DJ practice (Mixed In Key tiers).

    1.0 same key · 0.8 the "four great options" (±1 same letter, letter swap)
    · 0.5 +2 steps (whole-tone energy boost, use sparingly) · 0.4 unknown
    · 0.0 incompatible."""
    if not key_from or not key_to or "?" in (key_from, key_to):
        return 0.4
    if key_from == key_to:
        return 1.0
    try:
        num_f, let_f = int(key_from[:-1]), key_from[-1].upper()
        num_t, let_t = int(key_to[:-1]), key_to[-1].upper()
    except (ValueError, IndexError):
        return 0.4
    if let_f == let_t and (num_t - num_f) % 12 in (1, 11):
        return 0.8
    if num_f == num_t and let_f != let_t:
        return 0.8
    if let_f == let_t and (num_t - num_f) % 12 == 2:
        return 0.5
    return 0.0


def score_candidate(
    candidate: dict,
    prev: Optional[dict],
    target_energy: float,   # normalised 0-1 (interpolated slot target)
    bpm_target: int,        # absolute BPM target for this slot (interpolated)
    is_hit_slot: bool,
    recent_artists: set,    # lowercase artist names from last 2 tracks
) -> float:
    """Score a candidate for a slot. Higher = better fit.

    Weighting follows the playlist-sequencing literature (Bittner/Spotify,
    Pauws, hpDJ): transition smoothness DOMINATES, the arc (slot target) comes
    second, harmony third — adjacency is additionally enforced as a hard gate
    upstream in the beam search, so scoring only ranks feasible options.

    When style data is available (vocalness from the local pipeline, release
    year), a cohesion block keeps consecutive tracks "rhyming" in vocal
    character and era — e.g. a 1990 vocal euro-house track next to a modern
    instrumental prog-house run pays a real penalty. (Embedding-cosine timbre
    similarity was evaluated and rejected: it did not order same-style pairs
    correctly on real data — see essentia_analysis.py.)"""

    bpm_c = candidate["bpm"]

    # Transition smoothness vs the actual previous track (chains across
    # section boundaries — a boundary transition is still a transition).
    smooth = transition_smoothness(prev["bpm"], bpm_c) if prev else 0.7

    # Arc fit: proximity to this slot's interpolated trajectory target.
    arc = max(0.0, 1.0 - tempo_distance_pct(bpm_c, bpm_target) / TRANSITION_RELAX_PCT)

    # Harmonic compatibility with the previous track.
    key_s = harmonic_score(prev["camelot"] if prev else "?", candidate["camelot"])

    # Energy proximity to the interpolated slot energy target.
    e_norm       = candidate["energy"] / 100.0
    energy_score = max(0.0, 1.0 - abs(e_norm - target_energy) / 0.35)

    # Hit factor (popularity bonus at designated hit slots)
    hit_s = (candidate["popularity"] / 100.0) if is_hit_slot else 0.0

    # Same-artist penalty
    artist_penalty = 0.5 if any(
        a.lower() in recent_artists for a in candidate["artists_list"]
    ) else 0.0

    # ── Style cohesion vs the previous track (vocalness / era) ──────────────
    # Missing data scores the NEUTRAL 0.5 — never a different weight scale.
    # (A dual-scale version systematically favored unanalyzed tracks over
    # analyzed ones with ordinary, imperfect cohesion.)
    vocal_c = 0.5
    if prev is not None and prev.get("vocalness") is not None \
            and candidate.get("vocalness") is not None:
        vocal_c = 1.0 - abs(candidate["vocalness"] - prev["vocalness"]) / 100.0

    era_c = 0.5
    if prev is not None and (prev.get("year") or 0) > 0 and (candidate.get("year") or 0) > 0:
        era_c = 1.0 - min(abs(candidate["year"] - prev["year"]), 25) / 25.0

    raw = (
        smooth       * 0.30 +
        arc          * 0.22 +
        key_s        * 0.15 +
        energy_score * 0.12 +
        vocal_c      * 0.10 +
        era_c        * 0.06 +
        hit_s        * 0.05
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
) -> list:
    """Hard-filter candidates to a slot's arc window (domain reduction).

    BPM proximity to the slot's trajectory target is percent-based and widens
    with the energy/popularity tolerances (8% → 12% → 20%). Adjacency to the
    previous track is enforced separately in the beam search."""
    bpm_pct = 0.08 + (0.12 if energy_tol >= 20 else 0.04 if energy_tol >= 10 else 0.0)
    result = []
    for t in candidates:
        if not (slot.energy_min - energy_tol <= t["energy"] <= slot.energy_max + energy_tol):
            continue
        if not (slot.pop_min - pop_tol <= t["popularity"] <= slot.pop_max + pop_tol):
            continue
        if tempo_distance_pct(t["bpm"], slot.bpm_target) > bpm_pct:
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
        if len(bpm_pool) >= 8:
            pool = bpm_pool
        else:
            warnings.append(
                f"Only {len(bpm_pool)} tracks in BPM {bpm_range[0]}–{bpm_range[1]}. "
                "Ignoring BPM filter."
            )
            bpm_range = None   # don't clamp section BPM targets either

    return _sequence_pool(pool, duration_min, bpm_range, warnings)


def _sequence_pool(
    pool: list,
    duration_min: int,
    bpm_range: Optional[Tuple[int, int]] = None,
    warnings: Optional[List[str]] = None,
    max_resets: int = MAX_RESETS_PER_SET,
) -> dict:
    """Sequence a prepared pool into a narrative-arc set (beam search core).

    Shared by generate() (which filters the pool by genre/BPM first) and the
    library-tour planner (which passes pre-partitioned cohesive chunks)."""
    warnings = warnings if warnings is not None else []

    # ── Set-wide parameters ──────────────────────────────────────────────────
    n_tracks = max(5, round(duration_min / 3.75))
    if len(pool) < n_tracks:
        n_tracks = len(pool)
        warnings.append(
            f"Only {len(pool)} eligible tracks; set will be shorter than requested."
        )

    base_bpm = compute_base_bpm(pool, bpm_range)
    slots    = distribute_sections(n_tracks, base_bpm, bpm_range)

    # ── Beam-search selection over the slot trajectory ───────────────────────
    # Greedy pickers dead-end (they burn scarce "bridge" tracks and leave slots
    # with nothing beatmatchable) and oscillate around per-slot targets. Beam
    # search keeps BEAM_WIDTH partial sets alive so a locally-tempting pick
    # that wrecks later transitions gets outcompeted.
    #
    # Adjacency is a HARD GATE, not a score: candidates further than
    # TRANSITION_HARD_PCT (6%) from the previous track are rejected, relaxing
    # to 8%; beyond that the transition is only allowed as a labeled "reset"
    # event (budget: MAX_RESETS_PER_SET), matching how real DJs treat big
    # tempo jumps (breakdown swaps / dead stops — deliberate, rare, marked).
    BEAM_WIDTH   = 12
    BRANCH_LIMIT = 6    # top-scored expansions kept per beam state

    def _recent_artists(tracks: list) -> set:
        return {a.lower() for tr in tracks[-2:] for a in tr["artists_list"]}

    beams = [{"tracks": [], "used": frozenset(), "score": 0.0, "resets": 0}]

    for slot in slots:
        expansions = []
        for state in beams:
            prev  = state["tracks"][-1] if state["tracks"] else None
            avail = [t for t in pool if t["id"] not in state["used"]]
            if not avail:
                continue

            # Candidate selection: ADJACENCY OUTRANKS THE NARRATIVE WINDOWS.
            # A beatmatchable track outside the section's energy window always
            # beats an unmixable track inside it — the energy misfit is only a
            # score penalty, never a reason to break the tempo chain. So the
            # adjacency gate is applied at every window-relaxation level,
            # including a final no-windows level, before we even consider
            # stretching (≤8%) and only then a labeled reset.
            _ladders = (
                _filter_for_slot(avail, slot, prev, 0,  0),
                _filter_for_slot(avail, slot, prev, 10, 15),
                _filter_for_slot(avail, slot, prev, 20, 30),
                avail,
            )
            transition, cands = "beatmatch", []
            if prev is None:
                cands = next((l for l in _ladders if l), avail)
                transition = "open"
            else:
                for pct_gate, label in ((TRANSITION_HARD_PCT, "beatmatch"),
                                        (TRANSITION_RELAX_PCT, "stretch")):
                    for level in _ladders:
                        gated = [t for t in level
                                 if tempo_distance_pct(prev["bpm"], t["bpm"]) <= pct_gate]
                        if gated:
                            cands, transition = gated, label
                            break
                    if cands:
                        break
                if not cands:
                    if state["resets"] >= max_resets:
                        continue   # this beam can't afford another reset — dies
                    cands = next((l for l in _ladders if l), avail)
                    transition = "reset"

            recent = _recent_artists(state["tracks"])
            scored = sorted(
                cands,
                key=lambda t: score_candidate(
                    t, prev, slot.target_energy, slot.bpm_target,
                    slot.is_hit_slot, recent,
                ),
                reverse=True,
            )[:BRANCH_LIMIT]

            for t in scored:
                s = score_candidate(t, prev, slot.target_energy, slot.bpm_target,
                                    slot.is_hit_slot, recent)
                # Resets are a last resort: make them expensive so a beam only
                # keeps one when every alternative path truly dead-ends.
                if transition == "reset":
                    s -= 0.6
                elif transition == "stretch":
                    s -= 0.15
                expansions.append({
                    "tracks": state["tracks"] + [{
                        **t,
                        "section":       slot.section_name,
                        "section_label": slot.label,
                        "section_emoji": slot.emoji,
                        "position":      slot.position + 1,   # 1-indexed
                        "transition":    transition if prev is not None else "open",
                    }],
                    "used":   state["used"] | {t["id"]},
                    "score":  state["score"] + s,
                    "resets": state["resets"] + (1 if transition == "reset" else 0),
                })

        if not expansions:
            break   # pool exhausted for every beam — set ends shorter

        # Keep the best beams, de-duplicated by (used set, last track).
        expansions.sort(key=lambda st: st["score"], reverse=True)
        seen_keys, beams = set(), []
        for st in expansions:
            key = (st["used"], st["tracks"][-1]["id"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            beams.append(st)
            if len(beams) >= BEAM_WIDTH:
                break

    selected = beams[0]["tracks"] if beams else []

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
# Library tour — partition the WHOLE library into cohesive, disjoint sets
# ─────────────────────────────────────────────────────────────────────────────

# Small genre clusters merge into these macro-buckets so their leftovers can
# still form cohesive sets instead of orphan mini-playlists.
_MACRO_BUCKETS = {
    "electronic": "electronic", "downtempo": "downtempo", "classical": "downtempo",
    "rock": "banda", "indie": "banda", "pop": "banda",
    "hip_hop": "groove", "jazz": "groove",
    "latin": "raices", "folk": "raices", "world": "raices",
}
_BUCKET_LABELS = {
    "electronic": "Electrónica",
    "downtempo":  "Calma",
    "banda":      "Bandas & Pop",
    "groove":     "Groove",
    "raices":     "Raíces",
}


def _dominant_cluster(track: dict, cache: dict) -> str:
    """Single best cluster for partitioning: affinity argmax, tag fallback."""
    analysis = cache.get(track["id"], {})
    affinity = analysis.get("genre_affinity") or {}
    if affinity:
        return max(affinity, key=affinity.get)
    clusters = track.get("genre_clusters") or set()
    return next(iter(clusters), "") or "otros"


def _slice_by_duration(tracks: list, min_ms: int, target_ms: int, max_ms: int) -> list:
    """Split BPM-sorted tracks into chunks of cumulative duration ≈ target.

    The final short tail merges into the previous chunk when it still fits
    under max_ms; otherwise it stays as a (possibly short) chunk."""
    chunks, current, cur_ms = [], [], 0
    for t in tracks:
        current.append(t)
        cur_ms += t["duration_ms"]
        if cur_ms >= target_ms:
            chunks.append(current)
            current, cur_ms = [], 0
    if current:
        tail_ms = sum(t["duration_ms"] for t in current)
        if chunks and tail_ms < min_ms:
            prev_ms = sum(t["duration_ms"] for t in chunks[-1])
            if prev_ms + tail_ms <= max_ms:
                chunks[-1].extend(current)
            else:
                chunks.append(current)   # short set — flagged by caller
        else:
            chunks.append(current)
    return chunks


def plan_library_tour(
    library: list,
    cache: dict,
    min_minutes: int    = 60,
    target_minutes: int = 110,
    max_minutes: int    = 180,
) -> dict:
    """Partition the entire analyzed library into cohesive, disjoint DJ sets.

    Strategy:
      1. Group tracks by DOMINANT genre cluster (affinity argmax).
      2. Groups big enough on their own are split by vocal character when
         very large (instrumental vs vocal — a validated cohesion axis), then
         BPM-sorted and sliced into chunks of ~target duration. BPM sorting
         means each chunk covers a tight tempo band → smooth sequencing.
      3. Groups too small for one set pool into macro-buckets (Bandas & Pop,
         Groove, Raíces, Calma) and get the same treatment.
      4. Every chunk is sequenced with the narrative beam search, using all
         of its tracks. Sets are mutually disjoint by construction.

    Returns {"sets": [...], "unplaced": [...], "stats": {...}} where each set
    has {label, cluster, tracks(sequenced result), duration_ms, bpm_lo/hi,
    count, short(bool)}.
    """
    min_ms, target_ms, max_ms = (m * 60_000 for m in (min_minutes, target_minutes, max_minutes))
    pool = build_pool(library, cache)
    if not pool:
        return {"sets": [], "unplaced": [], "stats": {"pool": 0}}

    groups: dict = {}
    for t in pool:
        groups.setdefault(_dominant_cluster(t, cache), []).append(t)

    chunk_specs = []   # (label_base, tracks)
    leftovers: dict = {}
    for cluster, tracks in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        total_ms = sum(t["duration_ms"] for t in tracks)
        if total_ms < min_ms:
            bucket = _MACRO_BUCKETS.get(cluster, "raices")
            leftovers.setdefault(bucket, []).extend(tracks)
            continue

        label_base = GENRE_LABELS.get(cluster, cluster.title())
        # Very large groups: split along the validated vocal-cohesion axis
        # first so vocal and instrumental sets don't interleave.
        if total_ms > 2.2 * target_ms:
            instrumental = [t for t in tracks if (t.get("vocalness") or 50) < 45]
            vocal        = [t for t in tracks if (t.get("vocalness") or 50) >= 45]
            bands = []
            if sum(t["duration_ms"] for t in instrumental) >= min_ms and \
               sum(t["duration_ms"] for t in vocal) >= min_ms:
                bands = [(f"{label_base} instrumental", instrumental),
                         (f"{label_base} vocal", vocal)]
            else:
                bands = [(label_base, tracks)]
        else:
            bands = [(label_base, tracks)]

        for band_label, band_tracks in bands:
            band_tracks.sort(key=lambda t: t["bpm"])
            for chunk in _slice_by_duration(band_tracks, min_ms, target_ms, max_ms):
                chunk_specs.append((band_label, chunk))

    # Macro-bucket leftovers get the same slicing.
    for bucket, tracks in leftovers.items():
        total_ms = sum(t["duration_ms"] for t in tracks)
        label = _BUCKET_LABELS.get(bucket, bucket.title())
        if total_ms < min_ms and chunk_specs:
            # Too small even pooled — ride along with the closest-BPM chunk
            # that still has room, else stand alone as a short set.
            for t in sorted(tracks, key=lambda x: x["bpm"]):
                host = min(
                    (spec for spec in chunk_specs
                     if sum(x["duration_ms"] for x in spec[1]) + t["duration_ms"] <= max_ms),
                    key=lambda spec: abs(compute_base_bpm(spec[1]) - t["bpm"]),
                    default=None,
                )
                if host:
                    host[1].append(t)
                else:
                    chunk_specs.append((label, [t]))
            continue
        tracks.sort(key=lambda t: t["bpm"])
        for chunk in _slice_by_duration(tracks, min_ms, target_ms, max_ms):
            chunk_specs.append((label, chunk))

    # Sequence every chunk with the narrative beam search, using ALL tracks.
    sets, used_ids = [], set()
    label_counts: Counter = Counter(lbl for lbl, _ in chunk_specs)
    label_seen: Counter = Counter()
    for label_base, chunk in chunk_specs:
        # duration_min chosen so n_tracks == len(chunk) → every track is used.
        dur_min = max(5, round(len(chunk) * 3.75))
        result = _sequence_pool(list(chunk), dur_min, warnings=[], max_resets=2)
        tracks = result["tracks"]
        used_ids.update(t["id"] for t in tracks)

        label_seen[label_base] += 1
        label = (f"{label_base} {label_seen[label_base]}"
                 if label_counts[label_base] > 1 else label_base)
        bpms = [t["bpm"] for t in tracks] or [0]
        sets.append({
            "label":       label,
            "result":      result,
            "count":       len(tracks),
            "duration_ms": result["total_duration_ms"],
            "bpm_lo":      round(min(bpms)),
            "bpm_hi":      round(max(bpms)),
            "short":       result["total_duration_ms"] < min_ms,
        })

    unplaced = [t for t in pool if t["id"] not in used_ids]
    return {
        "sets": sets,
        "unplaced": [{"id": t["id"], "title": t["title"], "artists": t["artists"]}
                     for t in unplaced],
        "stats": {
            "pool":     len(pool),
            "placed":   len(pool) - len(unplaced),
            "sets":     len(sets),
            "total_ms": sum(s["duration_ms"] for s in sets),
        },
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
