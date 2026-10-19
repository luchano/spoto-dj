"""
Shared pytest fixtures for the whole suite.
"""
import pytest


@pytest.fixture(autouse=True)
def isolate_audio_cache(tmp_path, monkeypatch):
    """
    Point the analysis cache at a throwaway file for EVERY test.

    Several unit tests drive main._run_analysis(), which persists its cache
    dict to analysis.CACHE_FILE after every track. Without this isolation
    those writes land on the developer's real .audio_cache.json and REPLACE
    hours of accumulated analysis results with test fixtures — this actually
    happened (a `tid_004` test entry was found sitting in the real cache,
    with the real entries gone).

    save_cache()/load_cache() read analysis.CACHE_FILE at call time, so
    patching the module global covers every caller, including main.py's
    `from analysis import save_cache` import.
    """
    import analysis
    monkeypatch.setattr(analysis, "CACHE_FILE", tmp_path / "audio_cache_test.json")
