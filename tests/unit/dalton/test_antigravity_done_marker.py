"""The Antigravity "login finished" marker only ever reports a committed save
of the flow the poll is waiting on."""

from __future__ import annotations

from typing import Any
from unittest.mock import create_autospec

import pytest

from src.dalton.llm.providers.antigravity import oauth as ag
from src.utils.cache.redis_cache import RedisCacheClient
from tests.unit.dalton.test_team_credentials import _FakeRedisStore


def _cache(store: _FakeRedisStore) -> Any:
    cache = create_autospec(RedisCacheClient, instance=True)
    cache.enabled = True
    cache.client = store
    return cache


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _FakeRedisStore:
    s = _FakeRedisStore()
    monkeypatch.setattr("src.utils.cache.redis_cache.get_cache_client", lambda: _cache(s))
    monkeypatch.setattr(ag, "_flow_keys", lambda uid: (f"flow:{uid}", f"done:{uid}"))
    return s


_REAL_GET_TOKENS = ag.get_oauth_tokens


def _tokens(monkeypatch: pytest.MonkeyPatch, row: dict | None) -> None:
    monkeypatch.setattr(
        ag, "get_oauth_tokens", create_autospec(_REAL_GET_TOKENS, return_value=row)
    )


@pytest.mark.asyncio
async def test_marker_ignored_while_a_newer_flow_is_pending(
    store: _FakeRedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tokens(monkeypatch, {"access_token": "a"})
    store.store["done:u1"] = "1"  # an older login's marker
    store.store["flow:u1"] = "newer-flow"  # a newer login still waiting
    assert await ag.check_login_complete("u1") is False


@pytest.mark.asyncio
async def test_marker_needs_the_connection_row(
    store: _FakeRedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.store["done:u1"] = "1"
    _tokens(monkeypatch, None)  # disconnected after the save
    assert await ag.check_login_complete("u1") is False
    _tokens(monkeypatch, {"access_token": "a"})
    assert await ag.check_login_complete("u1") is True


@pytest.mark.asyncio
async def test_a_failed_save_publishes_no_marker(
    store: _FakeRedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.store["flow:u1"] = "this-flow"
    monkeypatch.setattr(
        ag,
        "upsert_oauth_tokens",
        create_autospec(ag.upsert_oauth_tokens, side_effect=RuntimeError("commit failed")),
    )

    class _Lock:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(
        "src.server.database.oauth_tokens.connection_change", lambda uid, provider: _Lock()
    )
    with pytest.raises(RuntimeError):
        await ag._finalize_token_save(
            "u1",
            {"access_token": "a", "refresh_token": "r", "expires_in": 3600, "id_token": ""},
            expected_flow_raw="this-flow",
        )
    assert "done:u1" not in store.store
