"""The Antigravity "login finished" marker names the flow it completes and only
counts while that flow is the one the poll waits on, after a committed save."""

from __future__ import annotations

from typing import Any
from unittest.mock import create_autospec

import pytest

from src.dalton.llm.providers.antigravity import oauth as ag
from src.utils.cache.redis_cache import RedisCacheClient
from tests.unit.dalton.test_team_credentials import _FakeRedisStore

_REAL_GET_TOKENS = ag.get_oauth_tokens
_REAL_UPSERT = ag.upsert_oauth_tokens


def _cache(store: _FakeRedisStore) -> Any:
    cache = create_autospec(RedisCacheClient, instance=True)
    cache.enabled = True
    cache.client = store
    cache.claim.side_effect = store_claim(store)
    return cache


def store_claim(store: _FakeRedisStore):
    async def _claim(key: str, expected: str) -> bool:
        if store.store.get(key) == expected:
            del store.store[key]
            return True
        return False

    return _claim


class _Lock:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc: object) -> bool:
        return False


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _FakeRedisStore:
    s = _FakeRedisStore()
    monkeypatch.setattr("src.utils.cache.redis_cache.get_cache_client", lambda: _cache(s))
    monkeypatch.setattr(ag, "_flow_keys", lambda uid: (f"flow:{uid}", f"done:{uid}"))
    monkeypatch.setattr(ag, "_current_flow_key", lambda uid: f"current:{uid}")
    monkeypatch.setattr(
        "src.server.database.oauth_tokens.connection_change", lambda uid, provider: _Lock()
    )
    return s


def _tokens(monkeypatch: pytest.MonkeyPatch, row: dict | None) -> None:
    monkeypatch.setattr(ag, "get_oauth_tokens", create_autospec(_REAL_GET_TOKENS, return_value=row))


def _upsert_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ag, "upsert_oauth_tokens", create_autospec(_REAL_UPSERT, return_value=None))


_TOKENS = {"access_token": "a", "refresh_token": "r", "expires_in": 3600, "id_token": ""}


def _start(store: _FakeRedisStore, raw: str) -> None:
    """What start_login writes inside the connection lock."""
    store.store.pop("done:u1", None)
    store.store["flow:u1"] = raw
    store.store["current:u1"] = ag._flow_id(raw)


@pytest.mark.asyncio
async def test_completed_flow_reports_done(store: _FakeRedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    _upsert_ok(monkeypatch)
    _tokens(monkeypatch, {"access_token": "a"})
    _start(store, "flow-A")
    await ag._finalize_token_save("u1", dict(_TOKENS), expected_flow_raw="flow-A")
    assert await ag.check_login_complete("u1") is True


@pytest.mark.asyncio
async def test_older_flow_finishing_late_never_completes_a_newer_login(
    store: _FakeRedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commits; B starts (no disconnect) and claims but has not committed;
    A's marker lands late: B's poll must stay pending (codex B9 r5)."""
    _upsert_ok(monkeypatch)
    _tokens(monkeypatch, {"access_token": "token-from-A"})
    _start(store, "flow-A")
    # A's save commits, then B starts before A publishes its marker.
    flow_a_id = ag._flow_id("flow-A")
    del store.store["flow:u1"]  # A claimed its flow
    _start(store, "flow-B")
    store.store["flow:u1"] = "flow-B"
    store.store["done:u1"] = flow_a_id  # A's late marker
    del store.store["flow:u1"]  # B claimed, not yet committed
    assert await ag.check_login_complete("u1") is False
    # B commits and publishes its own marker: now done.
    store.store["done:u1"] = ag._flow_id("flow-B")
    assert await ag.check_login_complete("u1") is True


@pytest.mark.asyncio
async def test_late_older_marker_cannot_overwrite_a_newer_success(
    store: _FakeRedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commits and pauses; B starts, commits and publishes; A resumes: B
    stays done (codex B9 r6)."""
    _upsert_ok(monkeypatch)
    _tokens(monkeypatch, {"access_token": "token-from-B"})
    _start(store, "flow-B")
    await ag._finalize_token_save("u1", dict(_TOKENS), expected_flow_raw="flow-B")
    assert await ag.check_login_complete("u1") is True
    # A's late publish goes through the same atomic step and is refused.
    applied = await store.eval(ag._PUBLISH_DONE_LUA, 2, "current:u1", "done:u1", ag._flow_id("flow-A"), 600)
    assert applied == 0
    assert store.store["done:u1"] == ag._flow_id("flow-B")
    assert await ag.check_login_complete("u1") is True


@pytest.mark.asyncio
async def test_marker_needs_the_connection_row(store: _FakeRedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    _start(store, "flow-A")
    store.store["done:u1"] = ag._flow_id("flow-A")
    _tokens(monkeypatch, None)
    assert await ag.check_login_complete("u1") is False


@pytest.mark.asyncio
async def test_a_failed_commit_publishes_no_marker(store: _FakeRedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    _upsert_ok(monkeypatch)
    _start(store, "flow-A")

    class _FailingCommit(_Lock):
        async def __aexit__(self, *exc: object) -> bool:
            raise RuntimeError("commit failed")

    monkeypatch.setattr(
        "src.server.database.oauth_tokens.connection_change", lambda uid, provider: _FailingCommit()
    )
    with pytest.raises(RuntimeError):
        await ag._finalize_token_save("u1", dict(_TOKENS), expected_flow_raw="flow-A")
    assert "done:u1" not in store.store
