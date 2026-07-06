"""
Tests for set_namer.py — creative names via z.ai, always fail-safe.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import set_namer as sn

pytestmark = pytest.mark.asyncio


PLAYLIST = {
    "tracks": [
        {"title": "Gratitude", "artists": "Sébastien Léger", "bpm": 122.0,
         "energy": 83, "section_label": "Climax"},
        {"title": "Calle Sur", "artists": "Uji", "bpm": 125.0,
         "energy": 70, "section_label": "Build"},
    ],
    "total_duration_ms": 5_280_000,
    "params": {"genre_filter": "electronic"},
}


def _resp(status=200, content="Neon Caravan"):
    r = MagicMock()
    r.status_code = status
    r.text = ""
    r.json.return_value = {"choices": [{"message": {"content": content}}]}
    return r


def _client_returning(resp):
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, client


class TestGenerateSetName:
    async def test_no_api_key_returns_none_without_calling(self, monkeypatch):
        monkeypatch.setattr(sn, "ZAI_API_KEY", "")
        with patch.object(sn.httpx, "AsyncClient") as ac:
            assert await sn.generate_set_name(PLAYLIST) is None
            ac.assert_not_called()

    async def test_success_returns_clean_name(self, monkeypatch):
        monkeypatch.setattr(sn, "ZAI_API_KEY", "k")
        ctx, client = _client_returning(_resp(content='"Neon Caravan"\n'))
        with patch.object(sn.httpx, "AsyncClient", return_value=ctx):
            assert await sn.generate_set_name(PLAYLIST) == "Neon Caravan"

    async def test_request_uses_thinking_and_model(self, monkeypatch):
        monkeypatch.setattr(sn, "ZAI_API_KEY", "k")
        ctx, client = _client_returning(_resp())
        with patch.object(sn.httpx, "AsyncClient", return_value=ctx):
            await sn.generate_set_name(PLAYLIST)
        _, kwargs = client.post.call_args
        body = kwargs["json"]
        assert body["thinking"] == {"type": "enabled"}
        assert body["model"] == sn.ZAI_MODEL
        # the prompt actually carries the set's tracks
        user_msg = body["messages"][1]["content"]
        assert "Gratitude" in user_msg and "BPM" in user_msg

    async def test_http_error_returns_none(self, monkeypatch):
        monkeypatch.setattr(sn, "ZAI_API_KEY", "k")
        ctx, _ = _client_returning(_resp(status=429, content=""))
        with patch.object(sn.httpx, "AsyncClient", return_value=ctx):
            assert await sn.generate_set_name(PLAYLIST) is None

    async def test_network_exception_returns_none(self, monkeypatch):
        monkeypatch.setattr(sn, "ZAI_API_KEY", "k")
        client = MagicMock()
        client.post = AsyncMock(side_effect=sn.httpx.ConnectError("boom"))
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=client)
        ctx.__aexit__ = AsyncMock(return_value=False)
        with patch.object(sn.httpx, "AsyncClient", return_value=ctx):
            assert await sn.generate_set_name(PLAYLIST) is None

    async def test_empty_playlist_returns_none(self, monkeypatch):
        monkeypatch.setattr(sn, "ZAI_API_KEY", "k")
        assert await sn.generate_set_name({"tracks": []}) is None


class TestCleanName:
    def test_strips_quotes_and_period(self):
        assert sn._clean_name('"Medianoche Andina".') == "Medianoche Andina"

    def test_takes_last_line_of_rambling(self):
        assert sn._clean_name("Pensando...\n\nArena y Neón") == "Arena y Neón"

    def test_rejects_long_ramble(self):
        assert sn._clean_name(
            "Creo que un buen nombre para este set sería algo como esto"
        ) is None

    def test_rejects_empty(self):
        assert sn._clean_name("") is None
        assert sn._clean_name('""') is None

    def test_collapses_whitespace(self):
        assert sn._clean_name("  Selva   Eléctrica  ") == "Selva Eléctrica"
