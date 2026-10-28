"""A4: direct calls on subsidiary OAuth clients refresh once on a mid-turn 401.

``call_with_oauth_refresh`` is the shared helper for the call sites that
bypass the resilience middleware: WebFetch's extraction call, compaction's
fallback summary, and server-side ``LLMService.complete``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import create_autospec

import pytest

from src.dalton.llm import oauth_refresh
from src.dalton.llm.oauth_refresh import (
    OAuthRebuildSpec,
    call_with_oauth_refresh,
    tag_oauth_client,
    token_digest,
)


class _FakeModel:
    def __init__(self, name: str, token: str = "t0"):
        self.model = name
        self.token = token


class _FakeAuthError(Exception):
    def __init__(self) -> None:
        super().__init__("Error code: 401 - token expired")
        self.status_code = 401


def _client(token: str = "t0") -> Any:
    return tag_oauth_client(
        _FakeModel("m", token),
        OAuthRebuildSpec(
            user_id="u1",
            model_name="m",
            provider="claude-oauth",
            token_digest=token_digest(token),
        ),
    )


def _rebuild_double(fresh: Any | None):
    return create_autospec(oauth_refresh.rebuild_oauth_client, return_value=fresh)


@pytest.mark.asyncio
async def test_success_first_try_never_rebuilds(monkeypatch: pytest.MonkeyPatch) -> None:
    rebuild = _rebuild_double(None)
    monkeypatch.setattr(oauth_refresh, "rebuild_oauth_client", rebuild)
    seen: list[Any] = []

    async def _fn(client: Any) -> str:
        seen.append(client)
        return "ok"

    assert await call_with_oauth_refresh(_client(), _fn) == "ok"
    rebuild.assert_not_awaited()
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_401_rebuilds_and_retries_once_with_fresh_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale, fresh = _client("t0"), _client("t1")
    rebuild = _rebuild_double(fresh)
    monkeypatch.setattr(oauth_refresh, "rebuild_oauth_client", rebuild)
    seen: list[Any] = []

    async def _fn(client: Any) -> str:
        seen.append(client)
        if client is stale:
            raise _FakeAuthError()
        return f"ok:{client.token}"

    assert await call_with_oauth_refresh(stale, _fn) == "ok:t1"
    rebuild.assert_awaited_once_with(stale)
    assert seen == [stale, fresh]


@pytest.mark.asyncio
async def test_second_401_raises_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    fresh = _client("t1")
    monkeypatch.setattr(oauth_refresh, "rebuild_oauth_client", _rebuild_double(fresh))
    calls: list[Any] = []

    async def _fn(client: Any) -> str:
        calls.append(client)
        raise _FakeAuthError()

    with pytest.raises(Exception) as exc_info:
        await call_with_oauth_refresh(_client("t0"), _fn)
    assert getattr(exc_info.value, "status_code", None) == 401
    assert len(calls) == 2  # one rebuild, one retry, then give up


@pytest.mark.asyncio
async def test_no_fresh_client_reraises_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oauth_refresh, "rebuild_oauth_client", _rebuild_double(None))

    async def _fn(client: Any) -> str:
        raise _FakeAuthError()

    with pytest.raises(_FakeAuthError):
        await call_with_oauth_refresh(_client(), _fn)


@pytest.mark.asyncio
async def test_non_401_propagates_without_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rebuild = _rebuild_double(_client("t1"))
    monkeypatch.setattr(oauth_refresh, "rebuild_oauth_client", rebuild)

    async def _fn(client: Any) -> str:
        raise TimeoutError("upstream slow")

    with pytest.raises(TimeoutError):
        await call_with_oauth_refresh(_client(), _fn)
    rebuild.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepare_mutates_the_fresh_client_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _client("t1")
    monkeypatch.setattr(oauth_refresh, "rebuild_oauth_client", _rebuild_double(fresh))
    prepared: list[Any] = []

    def _prepare(client: Any) -> None:
        prepared.append(client)
        client.streaming = False  # what fetch.py re-applies on a rebuild

    async def _fn(client: Any) -> str:
        if getattr(client, "streaming", True) is False:
            return "ok"
        raise _FakeAuthError()

    assert await call_with_oauth_refresh(_client(), _fn, prepare=_prepare) == "ok"
    assert prepared == [fresh]
