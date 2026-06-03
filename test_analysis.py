import tempfile
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from analysis import CAMELOT, KEY_NAMES, _analyze_file, _detect_key


# ── Camelot mapping ──────────────────────────────────────────────────────────

def test_camelot_covers_all_24_keys():
    for pitch in range(12):
        for mode in [0, 1]:
            assert (pitch, mode) in CAMELOT, f"Missing Camelot entry for ({pitch}, {mode})"


@pytest.mark.parametrize("pitch,mode,expected", [
    (0,  1, "8B"),   # C major
    (0,  0, "5A"),   # C minor
    (1,  1, "3B"),   # C# major
    (1,  0, "12A"),  # C# minor — was buggy "10A"
    (9,  0, "8A"),   # A minor
    (11, 1, "1B"),   # B major
    (11, 0, "10A"),  # B minor
    (6,  0, "11A"),  # F# minor
])
def test_camelot_values(pitch, mode, expected):
    assert CAMELOT[(pitch, mode)] == expected


# ── Key detection (unit, no audio) ───────────────────────────────────────────

def test_detect_key_c_major():
    chroma = np.zeros(12)
    chroma[0] = 1.0   # C
    chroma[4] = 0.7   # E
    chroma[7] = 0.5   # G
    pitch, mode = _detect_key(chroma)
    assert pitch == 0 and mode == 1, f"Expected C major, got ({KEY_NAMES[pitch]}, {'maj' if mode else 'min'})"


def test_detect_key_a_minor():
    chroma = np.zeros(12)
    chroma[9] = 1.0   # A
    chroma[0] = 0.6   # C
    chroma[4] = 0.4   # E
    pitch, mode = _detect_key(chroma)
    assert pitch == 9 and mode == 0, f"Expected A minor, got ({KEY_NAMES[pitch]}, {'maj' if mode else 'min'})"


# ── Full audio analysis (integration) ────────────────────────────────────────

SR = 22050


def _write_wav(y: np.ndarray, path: str):
    sf.write(path, y, SR, subtype="PCM_16")


def _click_track(bpm: float, duration: float = 8.0) -> np.ndarray:
    """Generates a click track at the given BPM using a tonal click (440 Hz + exponential decay)."""
    samples = int(SR * duration)
    y = np.zeros(samples, dtype=np.float32)
    beat_samples = int(SR * 60.0 / bpm)
    click_len = int(SR * 0.06)  # 60 ms per click
    t_click = np.linspace(0, 1, click_len)
    click = (np.sin(2 * np.pi * 440 * t_click) * np.exp(-8 * t_click)).astype(np.float32)
    for start in range(0, samples - click_len, beat_samples):
        y[start:start + click_len] += click
    peak = np.abs(y).max()
    if peak > 0:
        y /= peak
    return y * 0.9


def _chord(freqs: list[float], duration: float = 6.0) -> np.ndarray:
    """Generates a sustained chord from a list of frequencies (multi-octave)."""
    t = np.linspace(0, duration, int(SR * duration), endpoint=False)
    y = np.zeros_like(t, dtype=np.float32)
    for f in freqs:
        for octave in [0.5, 1.0, 2.0]:
            y += np.sin(2 * np.pi * f * octave * t).astype(np.float32)
    y /= np.abs(y).max() + 1e-9
    return y * 0.8


def test_analyze_file_bpm_120():
    y = _click_track(120.0)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        _write_wav(y, f.name)
        path = f.name
    try:
        result = _analyze_file(path)
        assert "bpm" in result
        assert 110 <= result["bpm"] <= 130, f"Expected ~120 BPM, got {result['bpm']}"
    finally:
        Path(path).unlink(missing_ok=True)


def test_analyze_file_bpm_140():
    y = _click_track(140.0)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        _write_wav(y, f.name)
        path = f.name
    try:
        result = _analyze_file(path)
        assert 128 <= result["bpm"] <= 152, f"Expected ~140 BPM, got {result['bpm']}"
    finally:
        Path(path).unlink(missing_ok=True)


def test_analyze_file_key_c_major():
    # C major chord: C(261.63), E(329.63), G(392.00) Hz
    y = _chord([261.63, 329.63, 392.00])
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        _write_wav(y, f.name)
        path = f.name
    try:
        result = _analyze_file(path)
        assert result["key"] == "C maj", f"Expected 'C maj', got '{result['key']}'"
        assert result["camelot"] == "8B", f"Expected '8B', got '{result['camelot']}'"
    finally:
        Path(path).unlink(missing_ok=True)


def test_analyze_file_returns_all_fields():
    y = _click_track(128.0)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        _write_wav(y, f.name)
        path = f.name
    try:
        result = _analyze_file(path)
        for field in ("bpm", "key", "camelot", "energy"):
            assert field in result, f"Missing field: {field}"
        assert isinstance(result["bpm"], int)
        assert 0 <= result["energy"] <= 100
    finally:
        Path(path).unlink(missing_ok=True)
