"""Unit tests for Antigravity native model list reuse and failure caching in provider_models."""

from __future__ import annotations

import json
import time
from unittest.mock import create_autospec

import httpx
import pytest

from src.dalton.llm import native_catalog
from src.llms.llm import ModelConfig
from src.server.services.llm import egress
from src.server.services.llm import provider_models as pm
from src.server.services.llm.provider_models import service as pm_service


@pytest.fixture(scope="module")
def mc() -> ModelConfig:
    return ModelConfig()


class _FakeRedis:
    """Minimal in-memory Redis hash double for provider_models cache tests."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.expirations: dict[str, int] = {}

    async def hget(self, key: str, field: str) -> str | None:
        return self.hashes.get(key, {}).get(field)

    async def hset(self, key: str, field: str, value: str) -> int:
        self.hashes.setdefault(key, {})[field] = value
        return 1

    async def expire(self, key: str, ttl: int) -> bool:
        self.expirations[key] = ttl
        return True

    async def delete(self, key: str) -> int:
        return 1 if self.hashes.pop(key, None) is not None else 0


class TestAntigravityNativeModelListing:
    """wire.py dispatches sdk=='antigravity' to native POST v1internal:fetchAvailableModels."""

    def test_antigravity_list_shape_and_url(self, mc: ModelConfig) -> None:
        info = mc.get_provider_info("antigravity-oauth")
        assert info.get("sdk") == "antigravity"
        shape = pm.list_shape(info)
        assert shape == pm.ANTIGRAVITY
        url, resolved_shape = pm.list_url(info, shape, None)
        assert resolved_shape == pm.ANTIGRAVITY
        assert url.endswith("/v1internal:fetchAvailableModels")
        assert not url.endswith("/models")

    @pytest.mark.asyncio
    async def test_antigravity_fetch_reuses_native_catalog_post_endpoint(
        self, mc: ModelConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        info = mc.get_provider_info("antigravity-oauth")
        seen_requests: list[tuple[str, str, dict[str, str], bytes]] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            seen_requests.append(
                (request.method, str(request.url), dict(request.headers), request.content)
            )
            return httpx.Response(
                200,
                json={
                    "models": {
                        "gemini-4-argon-high": {
                            "displayName": "Gemini 4 Argon High",
                            "maxTokens": 1048576,
                            "maxOutputTokens": 65536,
                        },
                        "tab_internal_helper": {
                            "maxTokens": 32768,
                        },
                    }
                },
            )

        spy_fetch = create_autospec(
            native_catalog.fetch_antigravity_models,
            side_effect=native_catalog.fetch_antigravity_models,
        )
        monkeypatch.setattr(native_catalog, "fetch_antigravity_models", spy_fetch)

        req = pm.build_list_request(info, api_key="ag-token-123")
        payload = await pm.fetch_list(req, transport=httpx.MockTransport(_handler))
        models = pm.normalize(req.shape, payload)

        assert spy_fetch.await_count == 1
        assert len(seen_requests) == 1
        method, url, headers, body = seen_requests[0]
        assert method == "POST"
        assert url == "https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels"
        assert headers.get("authorization") == "Bearer ag-token-123"
        assert json.loads(body.decode("utf-8")) == {}
        assert [m["id"] for m in models] == ["gemini-4-argon-high"]
        assert models[0]["display_name"] == "Gemini 4 Argon High"
        assert models[0]["context_window"] == 1048576

    @pytest.mark.asyncio
    async def test_antigravity_available_models_caches_failure_short_term_and_preserves_last_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_redis = _FakeRedis()
        now_box = {"t": 10_000.0}

        monkeypatch.setattr(pm_service, "_redis", lambda: fake_redis)
        monkeypatch.setattr(
            pm_service.time,
            "time",
            create_autospec(time.time, side_effect=lambda: now_box["t"]),
        )
        monkeypatch.setattr(
            pm_service,
            "_refresh_allowed",
            create_autospec(pm_service._refresh_allowed, return_value=True),
        )
        cred = pm_service.Credential(
            api_key="ag-tok",
            holder="antigravity-oauth",
            account_id="acct-1",
            plan_type="pro",
        )
        monkeypatch.setattr(
            pm_service,
            "_resolve_credential",
            create_autospec(pm_service._resolve_credential, return_value=cred),
        )

        call_count = {"n": 0}
        mode = {"status": 200}

        def _handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if mode["status"] == 200:
                return httpx.Response(
                    200,
                    json={
                        "models": {
                            "gemini-4-argon-high": {
                                "displayName": "Gemini 4 Argon High",
                                "maxTokens": 1048576,
                            }
                        }
                    },
                )
            return httpx.Response(503, json={"error": {"message": "upstream overloaded"}})

        transport = httpx.MockTransport(_handler)
        monkeypatch.setattr(
            egress,
            "user_endpoint_transport",
            create_autospec(egress.user_endpoint_transport, return_value=transport),
        )

        # 1. Initial success populates cache
        res_ok = await pm.available_models("u-ag", "antigravity-oauth")
        assert res_ok["status"] == "ok"
        assert "gemini-4-argon-high-oauth" in res_ok["ready"]
        first_fetched_at = res_ok["fetched_at"]
        assert first_fetched_at is not None
        assert call_count["n"] == 1

        # 2. Cache expires past 6h; refresh fails -> preserves last successful list & fetched_at, caches failure short-term
        now_box["t"] += pm_service.CACHE_TTL_S + 10.0
        mode["status"] = 503
        res_stale = await pm.available_models("u-ag", "antigravity-oauth")
        assert call_count["n"] == 2
        assert "gemini-4-argon-high-oauth" in res_stale["ready"]
        assert res_stale["fetched_at"] == first_fetched_at

        # 3. Repeated request within short failure cache window does NOT hit provider again
        now_box["t"] += 15.0
        res_cached_fail = await pm.available_models("u-ag", "antigravity-oauth")
        assert call_count["n"] == 2
        assert "gemini-4-argon-high-oauth" in res_cached_fail["ready"]
        assert res_cached_fail["fetched_at"] == first_fetched_at

        # 4. After short failure TTL expires, retries and recovers when provider is healthy
        now_box["t"] += pm_service._FAILURE_CACHE_TTL_S + 5.0
        mode["status"] = 200
        res_recovered = await pm.available_models("u-ag", "antigravity-oauth")
        assert call_count["n"] == 3
        assert res_recovered["status"] == "ok"
        assert "gemini-4-argon-high-oauth" in res_recovered["ready"]

    @pytest.mark.asyncio
    async def test_cold_failure_is_cached_short_term_without_hammering_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_redis = _FakeRedis()
        now_box = {"t": 20_000.0}

        monkeypatch.setattr(pm_service, "_redis", lambda: fake_redis)
        monkeypatch.setattr(
            pm_service.time,
            "time",
            create_autospec(time.time, side_effect=lambda: now_box["t"]),
        )
        cred = pm_service.Credential(
            api_key="ag-tok-cold",
            holder="antigravity-oauth",
            account_id="acct-cold",
            plan_type="pro",
        )
        monkeypatch.setattr(
            pm_service,
            "_resolve_credential",
            create_autospec(pm_service._resolve_credential, return_value=cred),
        )

        call_count = {"n": 0}

        def _handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(502, json={"error": "bad gateway"})

        transport = httpx.MockTransport(_handler)
        monkeypatch.setattr(
            egress,
            "user_endpoint_transport",
            create_autospec(egress.user_endpoint_transport, return_value=transport),
        )

        r1 = await pm.available_models("u-cold", "antigravity-oauth")
        assert r1["status"] == "fallback"
        assert r1["reason"] == pm.BAD_RESPONSE
        assert call_count["n"] == 1

        # Second call inside failure TTL hits short-term failure cache -> no second upstream request
        now_box["t"] += 10.0
        r2 = await pm.available_models("u-cold", "antigravity-oauth")
        assert r2["status"] == "fallback"
        assert r2["reason"] == pm.BAD_RESPONSE
        assert call_count["n"] == 1

        # After failure TTL expires -> retries upstream
        now_box["t"] += pm_service._FAILURE_CACHE_TTL_S + 5.0
        r3 = await pm.available_models("u-cold", "antigravity-oauth")
        assert r3["status"] == "fallback"
        assert call_count["n"] == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_payload",
        [
            {},
            {"models": "not-a-dict"},
            {"models": {}},
            {"models": {"tab_only": {"maxTokens": 32768}}},
            {
                "models": {
                    "gemini-4-argon-high": {
                        "displayName": "Gemini 4 Argon High",
                        "maxTokens": 1048576,
                    },
                    "broken_non_dict": "bad-entry",
                }
            },
            {
                "models": {
                    "gemini-4-argon-high": {
                        "displayName": "Gemini 4 Argon High",
                        "maxTokens": 1048576,
                    },
                    "bad_tokens_type": {
                        "displayName": "Bad Tokens",
                        "maxTokens": "not-an-int",
                    },
                }
            },
        ],
    )
    async def test_antigravity_malformed_or_empty_200_preserves_last_success(
        self, bad_payload: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_redis = _FakeRedis()
        now_box = {"t": 30_000.0}

        monkeypatch.setattr(pm_service, "_redis", lambda: fake_redis)
        monkeypatch.setattr(
            pm_service.time,
            "time",
            create_autospec(time.time, side_effect=lambda: now_box["t"]),
        )
        cred = pm_service.Credential(
            api_key="ag-tok-bad200",
            holder="antigravity-oauth",
            account_id="acct-bad200",
            plan_type="pro",
        )
        monkeypatch.setattr(
            pm_service,
            "_resolve_credential",
            create_autospec(pm_service._resolve_credential, return_value=cred),
        )

        phase = {"ok": True}
        calls = {"n": 0}

        def _handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if phase["ok"]:
                return httpx.Response(
                    200,
                    json={
                        "models": {
                            "gemini-4-argon-high": {
                                "displayName": "Gemini 4 Argon High",
                                "maxTokens": 1048576,
                            }
                        }
                    },
                )
            return httpx.Response(200, json=bad_payload)

        transport = httpx.MockTransport(_handler)
        monkeypatch.setattr(
            egress,
            "user_endpoint_transport",
            create_autospec(egress.user_endpoint_transport, return_value=transport),
        )

        first = await pm.available_models("u-bad200", "antigravity-oauth")
        assert first["status"] == "ok"
        assert "gemini-4-argon-high-oauth" in first["ready"]
        first_fetched_at = first["fetched_at"]
        assert first_fetched_at is not None

        # After 6h cache expiry, Antigravity returns malformed/empty 200 -> must NOT overwrite with [] for 6h!
        now_box["t"] += pm_service.CACHE_TTL_S + 10.0
        phase["ok"] = False
        second = await pm.available_models("u-bad200", "antigravity-oauth")
        assert "gemini-4-argon-high-oauth" in second["ready"]
        assert second["fetched_at"] == first_fetched_at
        assert calls["n"] == 2

        # Within 60s short failure cache window: serves preserved list without hammering upstream
        now_box["t"] += 15.0
        third = await pm.available_models("u-bad200", "antigravity-oauth")
        assert third["fetched_at"] == first_fetched_at
        assert calls["n"] == 2

        # After 60s short failure cache window expires (NOT 6h!): retries and recovers
        now_box["t"] += pm_service._FAILURE_CACHE_TTL_S + 5.0
        phase["ok"] = True
        fourth = await pm.available_models("u-bad200", "antigravity-oauth")
        assert fourth["status"] == "ok"
        assert calls["n"] == 3

    @pytest.mark.asyncio
    async def test_concurrent_refresh_failure_does_not_overwrite_newer_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        fake_redis = _FakeRedis()
        now_box = {"t": 40_000.0}

        monkeypatch.setattr(pm_service, "_redis", lambda: fake_redis)
        monkeypatch.setattr(
            pm_service.time,
            "time",
            create_autospec(time.time, side_effect=lambda: now_box["t"]),
        )
        monkeypatch.setattr(
            pm_service,
            "_refresh_allowed",
            create_autospec(pm_service._refresh_allowed, return_value=True),
        )
        cred = pm_service.Credential(
            api_key="ag-tok-race",
            holder="antigravity-oauth",
            account_id="acct-race",
            plan_type="pro",
        )
        monkeypatch.setattr(
            pm_service,
            "_resolve_credential",
            create_autospec(pm_service._resolve_credential, return_value=cred),
        )
        monkeypatch.setattr(
            egress,
            "user_endpoint_transport",
            create_autospec(egress.user_endpoint_transport, return_value=None),
        )

        # Seed initial gen=1 list with gemini-4-argon-low
        v1_models = [{"id": "gemini-4-argon-low", "display_name": "Argon Low", "context_window": 1048576}]
        v2_models = [{"id": "gemini-4-argon-high", "display_name": "Argon High", "context_window": 1048576}]
        fail_started = asyncio.Event()
        allow_fail_to_finish = asyncio.Event()
        call_seq = {"n": 0}

        async def _scripted_fetch(req, *, transport=None, timeout=8.0, max_bytes=8 * 1024 * 1024):
            call_seq["n"] += 1
            idx = call_seq["n"]
            if idx == 1:
                return {"models": {"gemini-4-argon-low": {"displayName": "Argon Low", "maxTokens": 1048576}}}
            if idx == 2:
                # Slow failing refresh A (started at gen=1)
                fail_started.set()
                await allow_fail_to_finish.wait()
                raise pm.ListFetchError(pm.BAD_RESPONSE, 503, "boom")
            # Fast succeeding refresh B (also started at gen=1, finishes first -> writes gen=2)
            return {"models": {"gemini-4-argon-high": {"displayName": "Argon High", "maxTokens": 1048576}}}

        monkeypatch.setattr(
            pm_service,
            "fetch_list",
            create_autospec(pm_service.fetch_list, side_effect=_scripted_fetch),
        )

        r_seed = await pm.available_models("u-race", "antigravity-oauth")
        assert "gemini-4-argon-low-oauth" in r_seed["ready"]
        assert "gemini-4-argon-high-oauth" not in r_seed["ready"]

        # Expire cache so both A and B fetch concurrently from gen=1
        now_box["t"] += pm_service.CACHE_TTL_S + 10.0
        task_a = asyncio.create_task(pm.available_models("u-race", "antigravity-oauth"))
        await asyncio.wait_for(fail_started.wait(), timeout=1.0)

        # Refresh B completes and writes gen=2 (v2_models)
        r_b = await asyncio.wait_for(pm.available_models("u-race", "antigravity-oauth"), timeout=1.0)
        assert "gemini-4-argon-high-oauth" in r_b["ready"]
        v2_fetched_at = r_b["fetched_at"]

        # Now let slow refresh A fail -> MUST NOT overwrite gen=2 (v2_models) with v1_models!
        allow_fail_to_finish.set()
        r_a = await asyncio.wait_for(task_a, timeout=1.0)
        assert "gemini-4-argon-high-oauth" in r_a["ready"]
        assert r_a["fetched_at"] == v2_fetched_at

        # Subsequent cached read still sees v2_models (gen=2) and its fetched_at!
        r_after = await pm.available_models("u-race", "antigravity-oauth")
        assert "gemini-4-argon-high-oauth" in r_after["ready"]
        assert r_after["fetched_at"] == v2_fetched_at
        assert v1_models != v2_models

    @pytest.mark.asyncio
    async def test_cross_worker_concurrent_refresh_uses_atomic_redis_cas(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """两个独立锁域（模拟双 worker）共享同一 Redis 缓存时，通过原子 Lua CAS 按 gen 比较后再写。"""
        import asyncio

        class _AtomicFakeRedis(_FakeRedis):
            def __init__(self) -> None:
                super().__init__()
                self.eval_calls = 0

            async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> str:
                assert numkeys == 1
                self.eval_calls += 1
                key = str(keys_and_args[0])
                args = keys_and_args[1:]
                field = str(args[0])
                schema = int(args[1])  # type: ignore[arg-type]
                fp = str(args[2])
                base_gen = int(args[3])  # type: ignore[arg-type]
                raw = self.hashes.get(key, {}).get(field)
                cur = None
                cur_gen = 0
                if raw is not None:
                    decoded = json.loads(raw)
                    if (
                        isinstance(decoded, dict)
                        and decoded.get("schema") == schema
                        and decoded.get("fingerprint") == fp
                    ):
                        cur = decoded
                        if isinstance(cur.get("gen"), int) and cur["gen"] >= 0:
                            cur_gen = cur["gen"]

                if script == pm_service._CAS_SUCCESS_LUA:
                    shape = str(args[4])
                    models = json.loads(str(args[5]))
                    fetched_at = str(args[6])
                    now_ts = float(args[7])  # type: ignore[arg-type]
                    ttl_s = int(args[8])  # type: ignore[arg-type]
                    if (
                        cur is not None
                        and cur_gen > base_gen
                        and isinstance(cur.get("models"), list)
                        and str(cur.get("fetched_at") or "") > fetched_at
                    ):
                        return raw  # type: ignore[return-value]
                    out = json.dumps(
                        {
                            "schema": schema,
                            "fingerprint": fp,
                            "shape": shape,
                            "models": models,
                            "fetched_at": fetched_at,
                            "stored": now_ts,
                            "gen": max(cur_gen, base_gen) + 1,
                        }
                    )
                    self.hashes.setdefault(key, {})[field] = out
                    self.expirations[key] = ttl_s
                    return out

                assert script == pm_service._CAS_FAILURE_LUA
                req_shape = str(args[4])
                reason = str(args[5])
                now_ts = float(args[6])  # type: ignore[arg-type]
                ttl_s = int(args[7])  # type: ignore[arg-type]
                fb_raw = str(args[8])
                if cur is not None and cur_gen > base_gen and isinstance(cur.get("models"), list):
                    return raw  # type: ignore[return-value]
                preserved = (
                    cur
                    if cur is not None and isinstance(cur.get("models"), list)
                    else (json.loads(fb_raw) if fb_raw else None)
                )
                if isinstance(preserved, dict) and isinstance(preserved.get("models"), list):
                    out_obj = {
                        "schema": schema,
                        "fingerprint": fp,
                        "shape": preserved.get("shape") or req_shape,
                        "models": preserved["models"],
                        "fetched_at": preserved.get("fetched_at"),
                        "stored": preserved.get("stored", 0),
                        "gen": max(cur_gen, int(preserved.get("gen") or 0)),
                        "failed_at": now_ts,
                        "failure_reason": reason,
                    }
                else:
                    out_obj = {
                        "schema": schema,
                        "fingerprint": fp,
                        "shape": req_shape,
                        "models": None,
                        "fetched_at": None,
                        "stored": 0,
                        "gen": cur_gen,
                        "failed_at": now_ts,
                        "failure_reason": reason,
                    }
                out = json.dumps(out_obj)
                self.hashes.setdefault(key, {})[field] = out
                self.expirations[key] = ttl_s
                return out

        shared_redis = _AtomicFakeRedis()
        now_box = {"t": 50_000.0}
        monkeypatch.setattr(pm_service, "_redis", lambda: shared_redis)
        monkeypatch.setattr(
            pm_service.time,
            "time",
            create_autospec(time.time, side_effect=lambda: now_box["t"]),
        )
        monkeypatch.setattr(
            pm_service,
            "_refresh_allowed",
            create_autospec(pm_service._refresh_allowed, return_value=True),
        )
        cred = pm_service.Credential(
            api_key="ag-tok-xworker",
            holder="antigravity-oauth",
            account_id="acct-xworker",
            plan_type="pro",
        )
        monkeypatch.setattr(
            pm_service,
            "_resolve_credential",
            create_autospec(pm_service._resolve_credential, return_value=cred),
        )
        monkeypatch.setattr(
            egress,
            "user_endpoint_transport",
            create_autospec(egress.user_endpoint_transport, return_value=None),
        )

        worker_a_started = asyncio.Event()
        worker_a_release = asyncio.Event()
        seq = {"n": 0}

        async def _scripted(req, *, transport=None, timeout=8.0, max_bytes=8 * 1024 * 1024):
            seq["n"] += 1
            if seq["n"] == 1:
                return {"models": {"gemini-4-argon-low": {"displayName": "Argon Low", "maxTokens": 1048576}}}
            if seq["n"] == 2:
                worker_a_started.set()
                await worker_a_release.wait()
                raise pm.ListFetchError(pm.BAD_RESPONSE, 503, "worker A failed")
            return {"models": {"gemini-4-argon-high": {"displayName": "Argon High", "maxTokens": 1048576}}}

        monkeypatch.setattr(
            pm_service,
            "fetch_list",
            create_autospec(pm_service.fetch_list, side_effect=_scripted),
        )

        # Seed gen=1
        r0 = await pm.available_models("u-xworker", "antigravity-oauth")
        assert "gemini-4-argon-low-oauth" in r0["ready"]

        # Expire cache; Worker A starts with base_gen=1
        now_box["t"] += pm_service.CACHE_TTL_S + 10.0
        task_worker_a = asyncio.create_task(pm.available_models("u-xworker", "antigravity-oauth"))
        await asyncio.wait_for(worker_a_started.wait(), timeout=1.0)

        # Simulate a separate worker process (completely independent in-process lock dict)
        monkeypatch.setattr(pm_service, "_cache_write_locks", {})
        r_worker_b = await asyncio.wait_for(
            pm.available_models("u-xworker", "antigravity-oauth"), timeout=1.0
        )
        assert "gemini-4-argon-high-oauth" in r_worker_b["ready"]
        v2_fetched_at = r_worker_b["fetched_at"]

        # Worker A finishes later with failure -> Redis Lua CAS keeps Worker B's gen=2 list intact
        worker_a_release.set()
        r_worker_a = await asyncio.wait_for(task_worker_a, timeout=1.0)
        assert "gemini-4-argon-high-oauth" in r_worker_a["ready"]
        assert r_worker_a["fetched_at"] == v2_fetched_at
        assert shared_redis.eval_calls == 3

        stored_entry = json.loads(
            shared_redis.hashes[pm_service.available_models_key("u-xworker")]["antigravity-oauth"]
        )
        assert stored_entry["gen"] == 2
        assert stored_entry["models"][0]["id"] == "gemini-4-argon-high"
        assert stored_entry["fetched_at"] == v2_fetched_at
        assert "failed_at" not in stored_entry
