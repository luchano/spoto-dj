"""
Tests for Spotify token refresh — the fix for "export failed: 401" after the
1-hour access-token lifetime.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

import main as m

pytestmark = pytest.mark.asyncio


def _resp(status, json_body=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_body or {}
    r.text = ""
    return r


class TestRefreshAccessToken:
    async def test_updates_session_in_place(self):
        session = {"access_token": "old", "refresh_token": "r1"}
        client = MagicMock()
        client.post = AsyncMock(return_value=_resp(200, {
            "access_token": "new", "expires_in": 3600,
        }))
        token = await m._refresh_access_token(session, client)
        assert token == "new"
        assert session["access_token"] == "new"
        assert session["expires_at"] > 0

    async def test_keeps_old_refresh_token_when_not_rotated(self):
        session = {"access_token": "old", "refresh_token": "r1"}
        client = MagicMock()
        client.post = AsyncMock(return_value=_resp(200, {"access_token": "new"}))
        await m._refresh_access_token(session, client)
        assert session["refresh_token"] == "r1"

    async def test_rotates_refresh_token_when_returned(self):
        session = {"access_token": "old", "refresh_token": "r1"}
        client = MagicMock()
        client.post = AsyncMock(return_value=_resp(200, {
            "access_token": "new", "refresh_token": "r2",
        }))
        await m._refresh_access_token(session, client)
        assert session["refresh_token"] == "r2"

    async def test_no_refresh_token_returns_none(self):
        client = MagicMock()
        client.post = AsyncMock()
        assert await m._refresh_access_token({"access_token": "x"}, client) is None
        client.post.assert_not_called()

    async def test_refresh_http_error_returns_none(self):
        session = {"refresh_token": "r1", "access_token": "old"}
        client = MagicMock()
        client.post = AsyncMock(return_value=_resp(400, {"error": "invalid_grant"}))
        assert await m._refresh_access_token(session, client) is None
        assert session["access_token"] == "old"   # untouched


class TestSpotifyRequestRetry:
    async def test_success_no_refresh(self):
        session = {"access_token": "good", "refresh_token": "r1"}
        client = MagicMock()
        client.request = AsyncMock(return_value=_resp(201, {"id": "pl"}))
        r = await m._spotify_request(client, session, "POST", "http://x")
        assert r.status_code == 201
        assert client.request.call_count == 1

    async def test_401_triggers_refresh_and_retry(self):
        session = {"access_token": "stale", "refresh_token": "r1"}
        client = MagicMock()
        # first call 401, second (after refresh) 201
        client.request = AsyncMock(side_effect=[_resp(401), _resp(201, {"id": "pl"})])
        client.post = AsyncMock(return_value=_resp(200, {"access_token": "fresh"}))
        r = await m._spotify_request(client, session, "POST", "http://x")
        assert r.status_code == 201
        assert client.request.call_count == 2
        # retry used the refreshed token
        _, kwargs = client.request.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer fresh"

    async def test_401_with_failed_refresh_returns_401(self):
        session = {"access_token": "stale", "refresh_token": "r1"}
        client = MagicMock()
        client.request = AsyncMock(return_value=_resp(401))
        client.post = AsyncMock(return_value=_resp(400))   # refresh fails
        r = await m._spotify_request(client, session, "POST", "http://x")
        assert r.status_code == 401
        assert client.request.call_count == 1   # not retried
