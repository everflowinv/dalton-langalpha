"""Auth-mechanism and key-leak guards for the FMP client.

The API key travels ONLY in the ``apikey`` request header — never the URL.
Query-string auth is forbidden because Daytona substitutes sandbox secret
placeholders exclusively in HTTPS request headers, and because httpx bakes
request URLs into exception messages. ``FMPClient._make_request``
additionally never stringifies the underlying httpx error into
``FMPRequestError`` (defense in depth): it surfaces only the status code or
a static message.
"""

from unittest.mock import AsyncMock

import httpx
import pytest

from src.data_client.fmp.fmp_client import FMPClient, FMPRequestError

_SECRET = "TESTSECRET"  # neutral placeholder — stands in for a real apikey


def _client_with_mocked_http(get_mock: AsyncMock) -> FMPClient:
    client = FMPClient(api_key=_SECRET)
    mock_http = AsyncMock()
    mock_http.is_closed = False  # else _get_client rebuilds a real client
    mock_http.get = get_mock
    client._client = mock_http
    return client


class TestFmpErrorKeyLeakGuard:
    @pytest.mark.asyncio
    async def test_http_status_error_does_not_leak_apikey(self):
        leaky_url = (
            f"https://financialmodelingprep.com/stable/profile?symbol=AAPL&apikey={_SECRET}"
        )
        request = httpx.Request("GET", leaky_url)
        response = httpx.Response(403, request=request, text="Forbidden")

        # Sanity: the RAW httpx error genuinely embeds the key via the URL, so
        # the guard below is load-bearing rather than testing a no-op.
        with pytest.raises(httpx.HTTPStatusError) as raw:
            response.raise_for_status()
        assert _SECRET in str(raw.value)

        client = _client_with_mocked_http(AsyncMock(return_value=response))
        try:
            with pytest.raises(FMPRequestError) as exc:
                await client.get_profile("AAPL")
        finally:
            await client.close()

        assert _SECRET not in str(exc.value)
        assert exc.value.status_code == 403
        assert str(exc.value) == "FMP API request failed (403)"

    @pytest.mark.asyncio
    async def test_request_error_does_not_leak_apikey(self):
        # A transport-level error (ConnectError) whose message embeds the key.
        leaky = f"cannot connect to https://financialmodelingprep.com/stable/quote?apikey={_SECRET}"
        client = _client_with_mocked_http(AsyncMock(side_effect=httpx.ConnectError(leaky)))
        try:
            with pytest.raises(FMPRequestError) as exc:
                await client.get_quote("AAPL")
        finally:
            await client.close()

        assert _SECRET not in str(exc.value)
        assert str(exc.value) == "FMP API request failed"
        assert exc.value.status_code is None


class TestFmpHeaderAuth:
    """The key must travel ONLY in the `apikey` request header.

    Query-string auth is forbidden: Daytona sandbox placeholders are
    substituted exclusively in HTTPS request headers, so a query-param key
    breaks hosted sandboxes (placeholder egresses verbatim -> FMP 401).
    """

    @pytest.mark.asyncio
    async def test_key_sent_in_header_not_query_or_cache(self):
        request = httpx.Request(
            "GET", "https://financialmodelingprep.com/stable/profile?symbol=AAPL"
        )
        response = httpx.Response(200, request=request, json=[{"symbol": "AAPL"}])
        get_mock = AsyncMock(return_value=response)
        client = _client_with_mocked_http(get_mock)
        try:
            await client.get_profile("AAPL")
        finally:
            await client.close()

        assert get_mock.await_count == 1
        kwargs = get_mock.await_args.kwargs
        assert kwargs["headers"] == {"apikey": _SECRET}
        assert "apikey" not in kwargs["params"]
        assert client._cache
        assert all(_SECRET not in key for key in client._cache)


class TestFmpVendorSpelling:
    """FMP resolves Shanghai only as ``.SS``; a ``.SH`` caller still lands."""

    @pytest.mark.asyncio
    async def test_display_spelling_reaches_fmp_in_its_own_form(self):
        request = httpx.Request("GET", "https://financialmodelingprep.com/stable/quote")
        response = httpx.Response(200, request=request, json=[{"symbol": "600519.SS"}])
        get_mock = AsyncMock(return_value=response)
        client = _client_with_mocked_http(get_mock)
        try:
            await client._make_request("quote", {"symbol": "600519.SH"})
            await client._make_request("batch-quote", {"symbols": "600519.SH,AAPL"})
        finally:
            await client.close()

        sent = [call.kwargs["params"] for call in get_mock.await_args_list]
        assert sent[0]["symbol"] == "600519.SS"
        assert sent[1]["symbols"] == "600519.SS,AAPL"

    @pytest.mark.asyncio
    async def test_rows_come_back_in_our_spelling(self):
        request = httpx.Request("GET", "https://financialmodelingprep.com/stable/batch-quote")
        response = httpx.Response(
            200, request=request, json=[{"symbol": "600519.SS"}, {"symbol": "^GSPC"}]
        )
        get_mock = AsyncMock(return_value=response)
        client = _client_with_mocked_http(get_mock)
        try:
            fresh = await client._make_request("batch-quote", {"symbols": "600519.SH,^GSPC"})
            cached = await client._make_request("batch-quote", {"symbols": "600519.SH,^GSPC"})
        finally:
            await client.close()

        assert get_mock.await_count == 1
        assert [r["symbol"] for r in fresh] == ["600519.SH", "^GSPC"]
        assert [r["symbol"] for r in cached] == ["600519.SH", "^GSPC"]

    @pytest.mark.asyncio
    async def test_blank_entries_drop_and_an_unspellable_symbol_never_reaches_fmp(self):
        request = httpx.Request("GET", "https://financialmodelingprep.com/stable/batch-quote")
        get_mock = AsyncMock(return_value=httpx.Response(200, request=request, json=[]))
        client = _client_with_mocked_http(get_mock)
        try:
            await client._make_request("batch-quote", {"symbols": "600519.SH,, AAPL,"})
            with pytest.raises(FMPRequestError) as exc:
                await client._make_request("batch-quote", {"symbols": "AAPL,EUR/USD"})
        finally:
            await client.close()

        assert get_mock.await_count == 1
        assert get_mock.await_args.kwargs["params"]["symbols"] == "600519.SS,AAPL"
        # A status, so the sandbox tools report it as a bad argument, with the reason.
        assert exc.value.status_code == 400
        assert "EUR/USD" in str(exc.value)

    @pytest.mark.asyncio
    async def test_a_snapshot_batch_skips_only_the_symbol_fmp_cannot_be_asked_for(self):
        from unittest.mock import patch

        from src.data_client.fmp.data_source import FMPDataSource

        asked: list[list[str]] = []

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get_batch_quotes(self, symbols):
                asked.append(symbols)
                return [{"symbol": s, "price": 1.0} for s in symbols]

        with patch("src.data_client.fmp.data_source.FMPClient", return_value=_FakeClient()):
            snaps = await FMPDataSource().get_snapshots(["AAPL", "EUR/USD", "MSFT"])
            # Nothing left to ask: the refusal itself is the answer.
            with pytest.raises(ValueError, match="EUR/USD"):
                await FMPDataSource().get_snapshots(["EUR/USD"])

        assert [s["symbol"] for s in snaps] == ["AAPL", "MSFT"]
        assert asked == [["AAPL", "MSFT"]]


# ---------------------------------------------------------------------------
# Shared quota breaker (DALTON-HOOK(market-data-fallback))
# ---------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import create_autospec  # noqa: E402

from src.data_client.fmp import fmp_client as fmp_mod  # noqa: E402
from src.data_client.fmp.fmp_client import (  # noqa: E402
    FmpQuotaExhausted,
    next_quota_reset,
    quota_marker_reset,
    trip_quota_marker,
)


class _MarkerRedis:
    """The breaker's Redis surface: a dict holding (value, ttl) per key."""

    def __init__(self, monkeypatch):
        from src.utils.cache import redis_cache
        from src.utils.cache.redis_cache import RedisCacheClient

        self.store: dict[str, tuple[Any, Any]] = {}
        cache = create_autospec(RedisCacheClient, instance=True)
        cache.get.side_effect = self._get
        cache.set.side_effect = self._set
        monkeypatch.setattr(
            redis_cache,
            "get_cache_client",
            create_autospec(redis_cache.get_cache_client, return_value=cache),
        )

    async def _get(self, key):
        item = self.store.get(key)
        return item[0] if item else None

    async def _set(self, key, value, ttl=None):
        self.store[key] = (value, ttl)
        return True


class _FrozenDatetime(fmp_mod.datetime):
    """datetime subclass whose now() is pinned, for deterministic TTLs."""

    frozen = fmp_mod.datetime(2026, 10, 24, 12, 0, tzinfo=fmp_mod.timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.frozen if tz is not None else cls.frozen.replace(tzinfo=None)


@pytest.fixture
def marker_redis(monkeypatch):
    return _MarkerRedis(monkeypatch)


@pytest.fixture
def frozen_now(monkeypatch):
    monkeypatch.setattr(fmp_mod, "datetime", _FrozenDatetime)
    return _FrozenDatetime.frozen


def _http_mock(response: httpx.Response) -> Any:
    http = create_autospec(httpx.AsyncClient, instance=True)
    http.is_closed = False  # else _get_client rebuilds a real client
    http.get.return_value = response
    return http


def _response(status: int, *, headers: dict[str, str] | None = None, json=None):
    return httpx.Response(
        status,
        headers=headers or {},
        json=json,
        request=httpx.Request("GET", "https://financialmodelingprep.com/stable/quote"),
    )


class TestQuotaBreaker:
    @pytest.mark.asyncio
    async def test_marker_present_fails_fast_without_a_request(self, marker_redis):
        marker_redis.store["fmp:quota_exhausted"] = (
            {"status": 429, "reset_at": "2026-10-24T19:00:00+00:00"},
            3600,
        )
        client = FMPClient(api_key=_SECRET)
        client._client = _http_mock(_response(200, json=[{"symbol": "AAPL"}]))
        try:
            with pytest.raises(FmpQuotaExhausted) as exc:
                await client.get_quote("AAPL")
        finally:
            await client.close()

        client._client.get.assert_not_awaited()
        assert exc.value.status_code == 429
        assert "resets at 2026-10-24 15:00 US/Eastern" in str(exc.value)

    @pytest.mark.asyncio
    async def test_http_429_trips_the_shared_marker(self, marker_redis, frozen_now):
        reset_epoch = (_FrozenDatetime.frozen + timedelta(hours=3)).timestamp()
        client = FMPClient(api_key=_SECRET)
        client._client = _http_mock(
            _response(429, headers={"X-RateLimit-Reset": str(reset_epoch)})
        )
        try:
            with pytest.raises(FMPRequestError) as exc:
                await client.get_quote("AAPL")
        finally:
            await client.close()

        assert exc.value.status_code == 429
        value, ttl = marker_redis.store["fmp:quota_exhausted"]
        assert value["status"] == 429
        assert ttl == 3 * 3600

    @pytest.mark.asyncio
    async def test_a_header_reset_under_two_hours_is_still_honored(
        self, marker_redis, frozen_now
    ):
        # A 90-minute window is a valid reset: only past or >48h values are not.
        reset_epoch = (_FrozenDatetime.frozen + timedelta(minutes=90)).timestamp()
        await trip_quota_marker(429, reset_epoch)
        _, ttl = marker_redis.store["fmp:quota_exhausted"]
        assert ttl == 90 * 60

    @pytest.mark.asyncio
    async def test_http_402_fails_the_call_without_tripping(self, marker_redis):
        client = FMPClient(api_key=_SECRET)
        client._client = _http_mock(_response(402))
        try:
            with pytest.raises(FMPRequestError) as exc:
                await client.get_quote("AAPL")
        finally:
            await client.close()

        assert exc.value.status_code == 402
        assert marker_redis.store == {}

    @pytest.mark.asyncio
    async def test_limit_reach_body_trips_the_marker(self, marker_redis):
        client = FMPClient(api_key=_SECRET)
        client._client = _http_mock(
            _response(
                200,
                json={"Error Message": "Limit Reach. Please upgrade your plan."},
            )
        )
        try:
            with pytest.raises(FMPRequestError) as exc:
                await client.get_quote("AAPL")
        finally:
            await client.close()

        assert exc.value.status_code == 429
        assert marker_redis.store["fmp:quota_exhausted"][0]["status"] == 429

    @pytest.mark.asyncio
    async def test_default_ttl_runs_to_the_documented_reset(
        self, marker_redis, frozen_now
    ):
        await trip_quota_marker(429)
        value, ttl = marker_redis.store["fmp:quota_exhausted"]
        # 12:00 UTC → 19:00 UTC (15:00 ET) the same day.
        assert ttl == 7 * 3600
        assert await quota_marker_reset() == next_quota_reset(frozen_now)

        _FrozenDatetime.frozen = frozen_now + timedelta(hours=8)  # 16:00 ET
        try:
            await trip_quota_marker(429)
            _, ttl = marker_redis.store["fmp:quota_exhausted"]
            # The 3 PM slot has passed; the marker runs to tomorrow's.
            assert ttl == 23 * 3600
        finally:
            _FrozenDatetime.frozen = frozen_now  # the class is shared; unpollute

    def test_next_quota_reset_crosses_the_3pm_slot(self):
        from datetime import datetime, timezone

        reset = datetime(2026, 10, 24, 19, 0, tzinfo=timezone.utc)
        before = datetime(2026, 10, 24, 18, 59, tzinfo=timezone.utc)  # 14:59 ET
        assert next_quota_reset(before) == reset
        after = datetime(2026, 10, 24, 19, 1, tzinfo=timezone.utc)  # 15:01 ET
        assert next_quota_reset(after) == reset + timedelta(days=1)

    @pytest.mark.asyncio
    async def test_marker_reset_at_defaults_a_malformed_marker(
        self, marker_redis, frozen_now
    ):
        assert await quota_marker_reset() is None  # no marker yet
        marker_redis.store["fmp:quota_exhausted"] = ("garbage", 60)
        assert await quota_marker_reset() == next_quota_reset(frozen_now)

    @pytest.mark.asyncio
    async def test_no_redis_fails_open(self, monkeypatch):
        """Sandbox: no Redis import, no marker — the request proceeds as before."""
        monkeypatch.setattr(
            fmp_mod,
            "_quota_store",
            create_autospec(fmp_mod._quota_store, return_value=None),
        )
        client = FMPClient(api_key=_SECRET)
        client._client = _http_mock(_response(200, json=[{"symbol": "AAPL"}]))
        try:
            assert await client.get_quote("AAPL") == [{"symbol": "AAPL"}]
        finally:
            await client.close()
        client._client.get.assert_awaited_once()

        # A 429 with nowhere to write the marker still fails its own call.
        client2 = FMPClient(api_key=_SECRET)
        client2._client = _http_mock(_response(429))
        try:
            with pytest.raises(FMPRequestError) as exc:
                await client2.get_quote("AAPL")
        finally:
            await client2.close()
        assert exc.value.status_code == 429

    @pytest.mark.asyncio
    async def test_limit_reach_in_a_403_body_trips_the_marker(self, marker_redis):
        client = FMPClient(api_key=_SECRET)
        client._client = _http_mock(
            _response(
                403,
                json={"Error Message": "Limit Reach. Please upgrade your plan."},
            )
        )
        try:
            with pytest.raises(FMPRequestError) as exc:
                await client.get_quote("AAPL")
        finally:
            await client.close()

        assert exc.value.status_code == 429
        assert marker_redis.store["fmp:quota_exhausted"][0]["status"] == 429

    @pytest.mark.asyncio
    async def test_limit_reach_in_a_402_body_does_not_trip(self, marker_redis):
        client = FMPClient(api_key=_SECRET)
        client._client = _http_mock(
            _response(
                402,
                json={"Error Message": "Limit Reach. Please upgrade your plan."},
            )
        )
        try:
            with pytest.raises(FMPRequestError):
                await client.get_quote("AAPL")
        finally:
            await client.close()

        assert marker_redis.store == {}

    @pytest.mark.asyncio
    async def test_a_30_second_reset_header_sets_a_30_second_ttl(
        self, marker_redis, frozen_now
    ):
        reset_epoch = (_FrozenDatetime.frozen + timedelta(seconds=30)).timestamp()
        await trip_quota_marker(429, reset_epoch)
        _, ttl = marker_redis.store["fmp:quota_exhausted"]
        assert ttl == 30

    @pytest.mark.asyncio
    async def test_limit_reach_carries_the_header_reset(self, marker_redis, frozen_now):
        reset_epoch = (_FrozenDatetime.frozen + timedelta(minutes=90)).timestamp()
        client = FMPClient(api_key=_SECRET)
        client._client = _http_mock(
            _response(
                200,
                headers={"X-RateLimit-Reset": str(reset_epoch)},
                json={"Error Message": "Limit Reach. Please upgrade your plan."},
            )
        )
        try:
            with pytest.raises(FMPRequestError):
                await client.get_quote("AAPL")
        finally:
            await client.close()

        _, ttl = marker_redis.store["fmp:quota_exhausted"]
        assert ttl == 90 * 60


class TestEconomicIndicatorWindowWalk:
    """Stable ignores ``limit`` and answers at most 91 days ending at ``to``."""

    @pytest.mark.asyncio
    async def test_the_newest_limit_rows_come_from_windows_walked_back(self, monkeypatch):
        from datetime import date, timedelta

        import src.data_client.fmp.fmp_client as fmp_client

        monkeypatch.setattr(fmp_client, "_today_utc", lambda: date(2026, 10, 19))
        # Quarterly, dated at the quarter's start: nothing in the newest 91 days.
        quarters = [date(y, m, 1) for y in range(2026, 2019, -1) for m in (10, 7, 4, 1)]
        quarters = [q for q in quarters if q <= date(2026, 4, 1)]

        async def fmp_get(url, params, headers):
            hi = date.fromisoformat(params["to"])
            lo = max(date.fromisoformat(params["from"]), hi - timedelta(days=90))
            rows = [
                {"name": "GDP", "date": q.isoformat(), "value": q.year + q.month / 100}
                for q in quarters
                if lo <= q <= hi
            ]
            return httpx.Response(200, request=httpx.Request("GET", url), json=rows)

        get_mock = AsyncMock(side_effect=fmp_get)
        client = _client_with_mocked_http(get_mock)
        try:
            rows = await client.get_economic_indicators("GDP", limit=3)
        finally:
            await client.close()

        assert [r["date"] for r in rows] == ["2026-04-01", "2026-01-01", "2025-10-01"]
        sent = [call.kwargs["params"] for call in get_mock.await_args_list]
        assert all("limit" not in p for p in sent)
        # The first window alone, then batches of two and four, newest first.
        assert len(sent) == 7
        windows = sorted(
            ((p["from"], p["to"]) for p in sent), key=lambda w: w[1], reverse=True
        )
        assert windows[0] == ("2026-07-21", "2026-10-19")
        for (lo, hi), (_, older_hi) in zip(windows, windows[1:]):
            assert (date.fromisoformat(hi) - date.fromisoformat(lo)).days == 90
            assert date.fromisoformat(older_hi) == date.fromisoformat(lo) - timedelta(days=1)

    @pytest.mark.parametrize(
        ("failing_to", "fails"),
        [
            # The two oldest windows of the last batch: three rows were in hand.
            ("2025-07-21", False),
            ("2025-04-21", False),
            # A window the three rows still needed.
            ("2026-01-19", True),
        ],
    )
    @pytest.mark.asyncio
    async def test_only_a_needed_window_s_failure_fails_the_read(
        self, monkeypatch, failing_to, fails
    ):
        from datetime import date, timedelta

        import src.data_client.fmp.fmp_client as fmp_client

        monkeypatch.setattr(fmp_client, "_today_utc", lambda: date(2026, 10, 19))
        quarters = [date(y, m, 1) for y in range(2026, 2019, -1) for m in (10, 7, 4, 1)]
        quarters = [q for q in quarters if q <= date(2026, 4, 1)]

        async def fmp_get(url, params, headers):
            request = httpx.Request("GET", url)
            if params["to"] == failing_to:
                return httpx.Response(429, request=request, json={})
            hi = date.fromisoformat(params["to"])
            lo = max(date.fromisoformat(params["from"]), hi - timedelta(days=90))
            rows = [
                {"name": "GDP", "date": q.isoformat(), "value": q.year + q.month / 100}
                for q in quarters
                if lo <= q <= hi
            ]
            return httpx.Response(200, request=request, json=rows)

        client = _client_with_mocked_http(AsyncMock(side_effect=fmp_get))
        try:
            if fails:
                with pytest.raises(fmp_client.FMPRequestError):
                    await client.get_economic_indicators("GDP", limit=3)
            else:
                rows = await client.get_economic_indicators("GDP", limit=3)
                assert [r["date"] for r in rows] == ["2026-04-01", "2026-01-01", "2025-10-01"]
        finally:
            await client.close()
