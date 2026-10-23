"""Unit tests for native-subscription model check/sync (native_catalog, native_admin, wiring).

All provider traffic goes through httpx.MockTransport or create_autospec test
doubles — no real network, no real credentials.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import create_autospec

import httpx
import pytest
from psycopg import AsyncConnection, AsyncCursor

import src.dalton.llm.native_catalog as nc
import src.dalton.services.native_admin as na
from src.dalton.llm.native_catalog import (
    NATIVE_LISTING_PROVIDERS,
    ProviderListing,
    UpstreamModel,
    build_native_diff,
    build_overlay_entries,
    fetch_antigravity_models,
    fetch_claude_models,
    fetch_codex_models,
    suggest_native_model_key,
    write_native_sync_overlay,
)
from src.dalton.llm.overlay import load_dalton_meta


# ---------------------------------------------------------------------------
# Overlay merge position + stamp coverage
# ---------------------------------------------------------------------------


def test_overlay_sources_merge_order_places_native_sync_between_native_and_openclaw():
    from src.dalton.llm.overlay import OVERLAY_SOURCES

    names = [name for name, _path in OVERLAY_SOURCES]
    assert names == ["native", "native_sync", "openclaw"]


def test_native_sync_overlay_merges_models_and_dalton_meta(tmp_path, monkeypatch):
    import src.dalton.llm.overlay as overlay_mod

    sync_overlay_path = tmp_path / "native_sync_overlay.json"
    sync_overlay_path.write_text(
        json.dumps(
            {
                "schema": "dalton-native-sync-overlay/1",
                "models": {
                    "claude-bravo-6-0-oauth": {
                        "model_id": "claude-bravo-6-0",
                        "provider": "claude-oauth",
                        "visible": True,
                    }
                },
                "dalton_meta": {
                    "claude-bravo-6-0-oauth": {
                        "family": "anthropic-claude",
                        "billing": "subscription",
                        "tool_calling": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(overlay_mod, "NATIVE_SYNC_OVERLAY_PATH", sync_overlay_path)

    llm_cfg: dict[str, Any] = {"claude-opus-5-5": {"model_id": "original", "provider": "anthropic"}}
    manifest: dict[str, Any] = {"provider_config": {}, "models": {}}
    overlay_data = overlay_mod.load_overlay_data(sync_overlay_path)
    assert overlay_data is not None
    overlay_mod._merge_single_overlay(overlay_data, "native_sync", llm_cfg, manifest)
    assert llm_cfg["claude-bravo-6-0-oauth"]["model_id"] == "claude-bravo-6-0"

    meta = load_dalton_meta()
    assert meta["claude-bravo-6-0-oauth"]["family"] == "anthropic-claude"


def test_native_sync_overlay_collision_is_skipped():
    from src.dalton.llm.overlay import _merge_single_overlay

    llm_cfg = {"grok-4.7": {"model_id": "original", "provider": "xai"}}
    fake_overlay = {"models": {"grok-4.7": {"model_id": "hacked", "provider": "xai"}}}
    _merge_single_overlay(fake_overlay, "native_sync", llm_cfg, {"provider_config": {}})
    assert llm_cfg["grok-4.7"]["model_id"] == "original"


# ---------------------------------------------------------------------------
# Provider fetchers via httpx.MockTransport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_native_model_fetch_claude_version_header_and_pagination():
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        after_id = request.url.params.get("after_id")
        if after_id is None:
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5"}],
                    "has_more": True,
                    "last_id": "claude-opus-5-5",
                },
            )
        return httpx.Response(
            200,
            json={"data": [{"id": "claude-haiku-5-5", "display_name": "Claude Haiku 5.5"}], "has_more": False},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        models = await fetch_claude_models(
            "secret-claude-token",
            default_headers={
                "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
                "user-agent": "claude-cli/2.1.77",
                "x-app": "cli",
            },
            client=client,
        )

    assert [m.id for m in models] == ["claude-opus-5-5", "claude-haiku-5-5"]
    assert models[0].display_name == "Claude Opus 5.5"
    assert str(seen[0].url).startswith("https://api.anthropic.com/v1/models")
    assert seen[0].url.params["limit"] == "1000"
    assert seen[0].headers["authorization"] == "Bearer secret-claude-token"
    # Required on this route (401/400 without it), same value the BYOK probe sends.
    assert seen[0].headers["anthropic-version"] == "2023-06-01"
    assert seen[0].headers["anthropic-beta"] == "claude-code-20250219,oauth-2025-04-20"
    assert seen[0].headers["x-app"] == "cli"
    assert seen[1].url.params["after_id"] == "claude-opus-5-5"


@pytest.mark.asyncio
async def test_native_model_fetch_codex_client_version_and_fields():
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "slug": "gpt-6.2-sol",
                        "display_name": "GPT-6.2 Sol",
                        "visibility": "list",
                        "context_window": 1050000,
                        # Codex 0.164.0 protocol (openai_models.rs): each level is
                        # an object {effort, description}, not a bare string.
                        "supported_reasoning_levels": [
                            {"effort": "low", "description": "Low effort"},
                            {"effort": "medium", "description": "Medium effort"},
                            {"effort": "high", "description": "High effort"},
                            {"effort": "xhigh", "description": "Extra high effort"},
                            {"effort": "max", "description": "Maximum effort"},
                        ],
                        "default_reasoning_level": "xhigh",
                    },
                    {"slug": "gpt-reserve", "display_name": "GPT Reserve", "visibility": "hide"},
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        models = await fetch_codex_models(
            "secret-codex-token", account_id="acct-123", client=client
        )

    assert [m.id for m in models] == ["gpt-6.2-sol", "gpt-reserve"]
    assert models[0].display_name == "GPT-6.2 Sol"
    assert models[0].context_window == 1050000
    assert models[0].reasoning_efforts == ["low", "medium", "high", "xhigh", "max"]
    assert models[0].reasoning_default == "xhigh"
    assert models[0].visibility == "list"
    req = seen[0]
    assert str(req.url).startswith("https://chatgpt.com/backend-api/codex/models")
    # client_version >= 0.155.0 or the backend filters newer models out.
    assert req.url.params["client_version"] == nc.CODEX_LIST_CLIENT_VERSION
    assert nc.CODEX_LIST_CLIENT_VERSION >= "0.155.0"
    assert req.headers["authorization"] == "Bearer secret-codex-token"
    assert req.headers["originator"] == "codex_cli_rs"
    assert req.headers["user-agent"] == f"codex_cli_rs/{nc.CODEX_LIST_CLIENT_VERSION}"
    assert req.headers["chatgpt-account-id"] == "acct-123"


@pytest.mark.asyncio
async def test_native_model_fetch_antigravity_dict_response():
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "models": {
                    # Wire shape: dict keyed by full id; internal entries carry
                    # no displayName and come back flagged internal (they still
                    # count for deprecation matching, never as additions).
                    "gemini-4-argon-high": {
                        "displayName": "Gemini 4 Argon (high)",
                        "maxTokens": 1048576,
                        "maxOutputTokens": 65536,
                    },
                    "claude-opus-5-5-low": {"displayName": "Claude Opus 5.5 (low)"},
                    "tab_completion": {"maxTokens": 8192},
                    "chat_router-tiered": {"maxOutputTokens": 4096},
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        models = await fetch_antigravity_models("secret-ag-token", client=client)

    by_id = {m.id: m for m in models}
    assert set(by_id) == {
        "gemini-4-argon-high",
        "claude-opus-5-5-low",
        "tab_completion",
        "chat_router-tiered",
    }
    gemini = by_id["gemini-4-argon-high"]
    assert gemini.display_name == "Gemini 4 Argon (high)"
    assert gemini.context_window == 1048576
    assert gemini.max_output == 65536
    assert gemini.internal is False
    for internal_id in ("tab_completion", "chat_router-tiered"):
        assert by_id[internal_id].internal is True
        assert by_id[internal_id].display_name is None
    req = seen[0]
    assert str(req.url) == "https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels"
    assert req.method == "POST"
    assert json.loads(req.content) == {}
    assert req.headers["authorization"] == "Bearer secret-ag-token"
    assert req.headers["user-agent"].startswith("antigravity/cli/")


def test_native_catalog_diff_internal_entry_prevents_deprecation():
    # A catalog id present upstream but without displayName (internal row):
    # not deprecated, and never offered as an addition.
    llm_config = {
        "gemini-2.5-flash-oauth": {
            "model_id": "gemini-2.5-flash",
            "provider": "antigravity-oauth",
        },
        "gemini-old-9-oauth": {
            "model_id": "gemini-old-9",
            "provider": "antigravity-oauth",
        },
    }
    listing = ProviderListing(
        provider="antigravity-oauth",
        display_name="Google Antigravity",
        status="ok",
        models=[
            UpstreamModel(id="gemini-2.5-flash", internal=True),
            UpstreamModel(id="gemini-4-argon-high", display_name="Gemini 4 Argon (high)"),
        ],
    )

    diff = build_native_diff({"antigravity-oauth": listing}, llm_config)

    deprecated_keys = {d["key"] for d in diff["deprecated"]}
    assert deprecated_keys == {"gemini-old-9-oauth"}
    added_keys = {a["suggested_key"] for a in diff["added"]}
    assert added_keys == {"gemini-4-argon-high-oauth"}
    assert diff["unchanged"] == 1


@pytest.mark.asyncio
async def test_native_catalog_collect_isolates_failures_and_marks_unsupported(monkeypatch):
    async def _token(provider: str, user_id: str) -> dict[str, Any] | None:
        if provider == "claude-oauth":
            return {"access_token": "tok", "account_id": "", "plan_type": "pro"}
        return None

    monkeypatch.setattr(nc, "_resolve_provider_token", create_autospec(nc._resolve_provider_token, side_effect=_token))

    async def _claude(access_token: str, **kwargs: Any) -> list[UpstreamModel]:
        return [UpstreamModel(id="claude-opus-5-5")]

    monkeypatch.setattr(nc, "fetch_claude_models", create_autospec(nc.fetch_claude_models, side_effect=_claude))

    listings = await nc.collect_native_listings("user-1")

    assert listings["claude-oauth"].status == "ok"
    assert [m.id for m in listings["claude-oauth"].models] == ["claude-opus-5-5"]
    assert listings["muse-oauth"].status == "unsupported"
    assert listings["muse-oauth"].message == "该供应商不提供模型列表接口"
    assert listings["z-ai-cn-coding"].status == "unsupported"
    for provider in ("codex-oauth", "antigravity-oauth", "xai-oauth"):
        assert listings[provider].status == "error"
        assert listings[provider].error_kind == "no_credentials"


# ---------------------------------------------------------------------------
# Diff + entry construction
# ---------------------------------------------------------------------------


def test_native_catalog_key_rules_and_exact_match():
    assert suggest_native_model_key("claude-oauth", "claude-bravo-6-0") == "claude-bravo-6-0-oauth"
    assert suggest_native_model_key("xai-oauth", "grok-4.9") == "grok-4.9-oauth"
    # Full ids match exactly; no effort-suffix stripping.
    assert nc._entry_matches_upstream({"model_id": "gemini-4-argon-low"}, "gemini-4-argon") is False
    assert nc._entry_matches_upstream({"model_id": "gemini-4-argon-low"}, "gemini-4-argon-low") is True


def test_native_catalog_diff_added_deprecated_unchanged_and_hidden():
    llm_config = {
        "claude-opus-5-5-oauth": {"model_id": "claude-opus-5-5", "provider": "claude-oauth"},
        "claude-old-4-0-oauth": {"model_id": "claude-old-4-0", "provider": "claude-oauth"},
        "gemini-4-argon-low-oauth": {"model_id": "gemini-4-argon-low", "provider": "antigravity-oauth"},
        "grok-4.7": {"model_id": "grok-4.7", "provider": "xai"},
    }
    listings = {
        "claude-oauth": ProviderListing(
            provider="claude-oauth",
            display_name="Claude 订阅",
            status="ok",
            models=[
                UpstreamModel(id="claude-opus-5-5"),
                UpstreamModel(id="claude-bravo-6-0", display_name="Claude Bravo 6.0"),
                UpstreamModel(id="gpt-reserve", display_name="GPT Reserve", visibility="hide"),
            ],
        ),
        "antigravity-oauth": ProviderListing(
            provider="antigravity-oauth",
            display_name="Google Antigravity",
            status="ok",
            models=[UpstreamModel(id="gemini-4-argon-low")],
        ),
    }

    diff = build_native_diff(listings, llm_config)

    added_keys = {item["suggested_key"] for item in diff["added"]}
    assert added_keys == {"claude-bravo-6-0-oauth"}
    added_item = diff["added"][0]
    assert added_item["upstream_id"] == "claude-bravo-6-0"
    assert added_item["conflict"] is False
    # Hidden upstream-side entries are not offered.
    assert all(item["upstream_id"] != "gpt-reserve" for item in diff["added"])
    deprecated_keys = {item["key"] for item in diff["deprecated"]}
    assert deprecated_keys == {"claude-old-4-0-oauth"}
    assert diff["unchanged"] == 2


def test_native_catalog_diff_empty_listing_downgrades_to_error_no_deprecation():
    llm_config = {
        "claude-opus-5-5-oauth": {"model_id": "claude-opus-5-5", "provider": "claude-oauth"},
    }
    listing = ProviderListing(
        provider="claude-oauth",
        display_name="Claude 订阅",
        status="ok",
        models=[],
    )

    diff = build_native_diff({"claude-oauth": listing}, llm_config)

    assert listing.status == "error"
    assert listing.error_kind == "empty_listing"
    assert listing.message == "返回了空列表或无法解析"
    assert diff["added"] == []
    assert diff["deprecated"] == []
    assert diff["unchanged"] == 0


def test_native_catalog_diff_marks_cross_provider_key_conflicts():
    llm_config = {
        "claude-opus-5-5-oauth": {"model_id": "claude-opus-5-5", "provider": "claude-oauth"},
        "claude-renamed-oauth": {"model_id": "claude-old-name", "provider": "claude-oauth"},
    }
    listings = {
        "claude-oauth": ProviderListing(
            provider="claude-oauth",
            display_name="Claude 订阅",
            status="ok",
            models=[
                UpstreamModel(id="claude-opus-5-5"),
                # Same id under two providers → identical suggested keys.
                UpstreamModel(id="claude-shared-6-0"),
                # Suggested key exists in the catalog under a different model_id.
                UpstreamModel(id="claude-renamed"),
            ],
        ),
        "codex-oauth": ProviderListing(
            provider="codex-oauth",
            display_name="ChatGPT/Codex 订阅",
            status="ok",
            models=[UpstreamModel(id="claude-opus-5-5"), UpstreamModel(id="claude-shared-6-0")],
        ),
    }

    diff = build_native_diff(listings, llm_config)

    by_key = {item["suggested_key"]: item for item in diff["added"]}
    shared = by_key["claude-shared-6-0-oauth"]
    assert shared["conflict"] is True
    assert "claude-oauth" in str(shared["conflict_reason"])
    assert "codex-oauth" in str(shared["conflict_reason"])
    # A key that collides with an existing catalog model is flagged too.
    renamed = by_key["claude-renamed-oauth"]
    assert renamed["conflict"] is True
    assert renamed["conflict_reason"]


def test_native_catalog_build_entries_inherit_protocol_fields_only(monkeypatch):
    from src.llms.llm import LLM

    LLM._model_config = None
    llm_config = LLM.get_model_config().llm_config or {}
    meta_map = load_dalton_meta()

    added = [
        {
            "provider": "codex-oauth",
            "upstream_id": "gpt-6.2-sol",
            "display_name": "GPT-6.2 Sol",
            "suggested_key": "gpt-6.2-sol-oauth",
            "conflict": False,
            "context": 1050000,
            "max_output": None,
            "supports_reasoning_effort": None,
            # As parsed from the object-array supported_reasoning_levels.
            "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
            "reasoning_default": "xhigh",
        }
    ]
    models_out, meta_out = build_overlay_entries(added, llm_config, meta_map)

    entry = models_out["gpt-6.2-sol-oauth"]
    assert entry["model_id"] == "gpt-6.2-sol"
    assert entry["provider"] == "codex-oauth"
    assert entry["display_name"] == "GPT-6.2 Sol"
    assert entry["visible"] is True
    assert entry["context"] == 1050000
    # Provider-uniform parameters ride along; model-specific ones don't.
    assert entry["parameters"]["store"] is False
    assert "oauth_plans" not in entry
    assert "speed" not in entry and "intelligence" not in entry
    # Reasoning block from the upstream levels, write path from the provider.
    assert entry["reasoning"] == {
        "efforts": ["low", "medium", "high", "xhigh", "max"],
        "default": "xhigh",
        "write": "parameters.reasoning.effort",
    }

    meta = meta_out["gpt-6.2-sol-oauth"]
    assert meta["family"] == "openai-gpt"
    assert meta["billing"] == "subscription"
    assert "notional_ref" not in meta  # gpt-6.2-sol has no cash card


def test_native_catalog_build_entries_antigravity_family_templates(monkeypatch):
    from src.llms.llm import LLM

    LLM._model_config = None
    llm_config = LLM.get_model_config().llm_config or {}
    meta_map = load_dalton_meta()

    added = [
        {
            "provider": "antigravity-oauth",
            "upstream_id": "gemini-4-corrinth-low",
            "display_name": "Gemini 4 Corrinth (low)",
            "suggested_key": "gemini-4-corrinth-low-oauth",
            "conflict": False,
            "context": 1048576,
            "max_output": 65536,
            "supports_reasoning_effort": None,
            "reasoning_efforts": None,
        },
        {
            "provider": "antigravity-oauth",
            "upstream_id": "gpt-oss-140b-medium",
            "display_name": "GPT-OSS 140B (medium)",
            "suggested_key": "gpt-oss-140b-medium-oauth",
            "conflict": False,
            "context": 131072,
            "max_output": 32768,
            "supports_reasoning_effort": None,
            "reasoning_efforts": None,
        },
    ]
    models_out, meta_out = build_overlay_entries(added, llm_config, meta_map)

    gemini = models_out["gemini-4-corrinth-low-oauth"]
    assert gemini["prompt_guidance"] == "lean"  # same-family (Gemini) convention
    assert gemini["parameters"]["include_thoughts"] is True
    assert gemini["parameters"]["max_output_tokens"] == 65536
    assert meta_out["gemini-4-corrinth-low-oauth"]["family"] == "google-gemini"

    gptoss = models_out["gpt-oss-140b-medium-oauth"]
    # GPT-OSS family inside antigravity: no lean guidance, 32k output convention.
    assert "prompt_guidance" not in gptoss
    assert gptoss["parameters"]["max_output_tokens"] == 32768
    assert meta_out["gpt-oss-140b-medium-oauth"]["family"] == "openai-gpt"


def test_native_catalog_build_entries_write_xai_capability_fields(monkeypatch):
    from src.llms.llm import LLM

    LLM._model_config = None
    llm_config = LLM.get_model_config().llm_config or {}
    meta_map = load_dalton_meta()

    added = [
        {
            "provider": "xai-oauth",
            "upstream_id": "grok-4.9",
            "display_name": "Grok 4.9",
            "suggested_key": "grok-4.9-oauth",
            "conflict": False,
            "context": 500000,
            "max_output": 64000,
            "supports_reasoning_effort": True,
            "reasoning_efforts": ["low", "high"],
        },
        {
            "provider": "xai-oauth",
            "upstream_id": "grok-5.0-build-fast",
            "display_name": None,
            "suggested_key": "grok-5.0-build-fast-oauth",
            "conflict": False,
            "context": 256000,
            "max_output": 64000,
            "supports_reasoning_effort": False,
            "reasoning_efforts": None,
        },
    ]
    models_out, meta_out = build_overlay_entries(added, llm_config, meta_map)

    main = models_out["grok-4.9-oauth"]
    assert main["supports_reasoning_effort"] is True
    assert main["parameters"]["max_tokens"] == 64000
    assert main["reasoning"]["efforts"] == ["low", "high"]
    assert main["reasoning"]["write"] == "parameters.reasoning.effort"
    # grok-4.9 has no cash price card → no invented notional_ref.
    assert "notional_ref" not in meta_out["grok-4.9-oauth"]

    fast = models_out["grok-5.0-build-fast-oauth"]
    assert fast["supports_reasoning_effort"] is False
    assert "reasoning" not in fast
    # Display name falls back to the raw upstream id.
    assert fast["display_name"] == "grok-5.0-build-fast"


def test_native_catalog_write_overlay_is_add_only_and_skips_existing(tmp_path):
    target = tmp_path / "native_sync_overlay.json"
    first_models = {
        "claude-bravo-6-0-oauth": {"model_id": "claude-bravo-6-0", "provider": "claude-oauth"}
    }
    first_meta = {"claude-bravo-6-0-oauth": {"family": "anthropic-claude", "billing": "subscription"}}

    stamp1 = write_native_sync_overlay(first_models, first_meta, added_keys=["claude-bravo-6-0-oauth"], path=target)
    assert ":" in stamp1
    assert not (tmp_path / "native_sync_overlay.json.tmp").exists()

    # Re-syncing the same key with different content must not overwrite it.
    write_native_sync_overlay(
        {"claude-bravo-6-0-oauth": {"model_id": "tampered", "provider": "claude-oauth"}},
        {"claude-bravo-6-0-oauth": {"family": "wrong", "billing": "cash"}},
        added_keys=["claude-bravo-6-0-oauth"],
        path=target,
    )
    write_native_sync_overlay(
        {"grok-4.9-oauth": {"model_id": "grok-4.9", "provider": "xai-oauth"}},
        {},
        added_keys=["grok-4.9-oauth"],
        path=target,
    )

    data = json.loads(target.read_text(encoding="utf-8"))
    assert set(data["models"]) == {"claude-bravo-6-0-oauth", "grok-4.9-oauth"}
    assert data["models"]["claude-bravo-6-0-oauth"]["model_id"] == "claude-bravo-6-0"
    assert data["dalton_meta"]["claude-bravo-6-0-oauth"]["family"] == "anthropic-claude"
    assert data["schema"] == "dalton-native-sync-overlay/1"
    assert [h["added_keys"] for h in data["meta"]["history"]] == [
        ["claude-bravo-6-0-oauth"],
        ["claude-bravo-6-0-oauth"],
        ["grok-4.9-oauth"],
    ]

    with pytest.raises(ValueError):
        write_native_sync_overlay(
            {"bad-oauth": {"model_id": "bad", "provider": "x", "token": "leak"}},
            {},
            added_keys=["bad-oauth"],
            path=target,
        )


# ---------------------------------------------------------------------------
# Service layer: lock discipline, failure recording, add-only apply
# ---------------------------------------------------------------------------


def _make_fake_db(monkeypatch: pytest.MonkeyPatch, *, lock_acquired: bool = True):
    """Patch `module.get_db_connection` with a create_autospec-backed fake pool."""
    executed: list[tuple[str, Any]] = []

    cur = create_autospec(AsyncCursor, instance=True)

    async def _execute(query: Any, params: Any = None, **kwargs: Any) -> Any:
        executed.append((str(query), params))
        return cur

    cur.execute.side_effect = _execute

    async def _fetchone() -> dict[str, Any] | None:
        sql = executed[-1][0] if executed else ""
        if "pg_try_advisory_lock" in sql:
            return {"locked": lock_acquired}
        if "RETURNING run_id" in sql:
            return {"run_id": uuid.uuid4()}
        return None

    cur.fetchone.side_effect = _fetchone

    conn = create_autospec(AsyncConnection, instance=True)

    @asynccontextmanager
    async def _cursor_cm(*args: Any, **kwargs: Any):
        yield cur

    conn.cursor.side_effect = _cursor_cm

    @asynccontextmanager
    async def _get_db(_conn=None):
        yield conn

    return executed, _get_db


def _recorded_statuses(executed: list[tuple[str, Any]]) -> list[str]:
    """Status values written by `UPDATE dalton_openclaw_sync_runs` (params[0])."""
    return [
        str(params[0])
        for sql, params in executed
        if "UPDATE dalton_openclaw_sync_runs" in sql and params
    ]


def _recorded_diffs(executed: list[tuple[str, Any]]) -> list[dict[str, Any] | None]:
    """Diff JSONB values written by `UPDATE dalton_openclaw_sync_runs` (params[2])."""
    out: list[dict[str, Any] | None] = []
    for sql, params in executed:
        if "UPDATE dalton_openclaw_sync_runs" not in sql or not params:
            continue
        out.append(json.loads(params[2]) if params[2] is not None else None)
    return out


def _error_only_listings() -> dict[str, ProviderListing]:
    return {
        provider: ProviderListing(
            provider=provider,
            display_name=NATIVE_LISTING_PROVIDERS[provider],
            status="unsupported" if provider in nc.UNSUPPORTED_PROVIDERS else "error",
            message="该供应商不提供模型列表接口"
            if provider in nc.UNSUPPORTED_PROVIDERS
            else "该供应商未连接，无法检查",
            error_kind=None if provider in nc.UNSUPPORTED_PROVIDERS else "no_credentials",
        )
        for provider in NATIVE_LISTING_PROVIDERS
    }


@pytest.mark.asyncio
async def test_native_model_check_all_failed_records_failed_run(monkeypatch):
    from fastapi import HTTPException

    executed, fake_db = _make_fake_db(monkeypatch)
    monkeypatch.setattr(na, "get_db_connection", fake_db)
    monkeypatch.setattr(na, "_collect_reference_map", create_autospec(na._collect_reference_map, return_value={}))
    write_spy = create_autospec(write_native_sync_overlay, return_value="0:0")
    monkeypatch.setattr(na, "write_native_sync_overlay", write_spy)
    monkeypatch.setattr(
        na,
        "collect_native_listings",
        create_autospec(na.collect_native_listings, return_value=_error_only_listings()),
    )

    with pytest.raises(HTTPException) as exc_info:
        await na.run_native_check(user_id="pm-user")
    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["code"] == "no_provider_ok"

    assert _recorded_statuses(executed) == ["failed"]
    # The failed run keeps its own provider errors in its diff …
    diffs = _recorded_diffs(executed)
    assert len(diffs) == 1
    assert diffs[0] is not None
    failed_rows = diffs[0].get("providers") or []
    assert {row["status"] for row in failed_rows} == {"error", "unsupported"}
    assert "added" not in diffs[0]
    write_spy.assert_not_called()


@pytest.mark.asyncio
async def test_native_model_check_ok_records_run_and_in_use_refs(monkeypatch):
    executed, fake_db = _make_fake_db(monkeypatch)
    monkeypatch.setattr(na, "get_db_connection", fake_db)
    write_spy = create_autospec(write_native_sync_overlay, return_value="0:0")
    monkeypatch.setattr(na, "write_native_sync_overlay", write_spy)

    listings = _error_only_listings()
    listings["xai-oauth"] = ProviderListing(
        provider="xai-oauth",
        display_name="xAI SuperGrok",
        status="ok",
        models=[UpstreamModel(id="grok-4.7"), UpstreamModel(id="grok-4.9", display_name="Grok 4.9")],
    )
    monkeypatch.setattr(
        na,
        "collect_native_listings",
        create_autospec(na.collect_native_listings, return_value=listings),
    )
    monkeypatch.setattr(
        na,
        "_collect_reference_map",
        create_autospec(
            na._collect_reference_map,
            return_value={"grok-4.3-oauth": ["agent_config.llm.name", "user:u1:preferred_model"]},
        ),
    )

    result = await na.run_native_check(user_id="pm-user")

    assert result["counts"]["providers_ok"] == 1
    assert [i["suggested_key"] for i in result["diff"]["added"]] == ["grok-4.9-oauth"]
    deprecated_by_key = {d["key"]: d for d in result["diff"]["deprecated"]}
    assert deprecated_by_key["grok-4.3-oauth"]["in_use_by"] == [
        "agent_config.llm.name",
        "user:u1:preferred_model",
    ]
    # Matched entries stay in the catalog without a usage note.
    assert "grok-4.7-oauth" not in deprecated_by_key
    write_spy.assert_not_called()
    assert _recorded_statuses(executed) == ["succeeded"]


@pytest.mark.asyncio
async def test_native_model_check_lock_busy_returns_409(monkeypatch):
    from fastapi import HTTPException

    _executed, fake_db = _make_fake_db(monkeypatch, lock_acquired=False)
    monkeypatch.setattr(na, "get_db_connection", fake_db)

    with pytest.raises(HTTPException) as exc_info:
        await na.run_native_check(user_id="pm-user")
    assert exc_info.value.status_code == 409


def test_native_model_reference_map_includes_dalton_settings_items(monkeypatch):
    """`_collect_reference_map` covers assist/extract/reviewer settings (async wrapper)."""
    import asyncio

    settings = na.get_dalton_settings()

    async def _base_refs() -> dict[str, list[str]]:
        return {"claude-opus-5-5-oauth": ["agent_config.llm.name"]}

    monkeypatch.setattr(
        na, "_collect_model_references", create_autospec(na._collect_model_references, side_effect=_base_refs)
    )

    refs = asyncio.run(na._collect_reference_map())

    assert "agent_config.llm.name" in refs["claude-opus-5-5-oauth"]
    assert "dalton.models.assist_model" in refs.get(settings.models.assist_model, [])
    for fallback in na.get_assist_fallback_models():
        assert "dalton.models.assist_fallback_model" in refs.get(fallback, [])
    for reviewer in settings.publish.reviewer_models:
        assert "dalton.publish.reviewer_models" in refs.get(reviewer, [])
    assert "dalton.conversations.extractor_model" in refs.get(
        settings.conversations.extractor_model, []
    )
    if settings.publish.extract_model:
        assert "dalton.publish.extract_model" in refs.get(settings.publish.extract_model, [])


@pytest.mark.asyncio
async def test_native_model_sync_applies_selected_keys_add_only(monkeypatch, tmp_path):
    executed, fake_db = _make_fake_db(monkeypatch)
    monkeypatch.setattr(na, "get_db_connection", fake_db)
    invalidate_spy = create_autospec(na._invalidate_local_worker_model_caches, return_value=None)
    monkeypatch.setattr(na, "_invalidate_local_worker_model_caches", invalidate_spy)

    overlay_path = tmp_path / "native_sync_overlay.json"
    monkeypatch.setattr(nc, "NATIVE_SYNC_OVERLAY_PATH", overlay_path)

    last_check = {
        "run_id": "00000000-0000-0000-0000-0000000000aa",
        "diff": {
            "added": [
                {
                    "provider": "claude-oauth",
                    "upstream_id": "claude-bravo-6-0",
                    "display_name": "Claude Bravo 6.0",
                    "suggested_key": "claude-bravo-6-0-oauth",
                    "conflict": False,
                },
                {
                    "provider": "xai-oauth",
                    "upstream_id": "grok-4.9",
                    "display_name": None,
                    "suggested_key": "grok-4.9-oauth",
                    "conflict": True,
                    "conflict_reason": "key 与其他供应商的新增重复：xai-oauth",
                },
            ]
        },
    }
    monkeypatch.setattr(
        na,
        "fetch_last_succeeded_native_check",
        create_autospec(na.fetch_last_succeeded_native_check, return_value=last_check),
    )

    from fastapi import HTTPException

    # Conflicting key is rejected before any write.
    with pytest.raises(HTTPException) as exc_conflict:
        await na.run_native_sync(user_id="pm-user", model_keys=["grok-4.9-oauth"])
    assert exc_conflict.value.status_code == 409

    # Unknown key is rejected.
    with pytest.raises(HTTPException) as exc_unknown:
        await na.run_native_sync(user_id="pm-user", model_keys=["not-in-check-oauth"])
    assert exc_unknown.value.status_code == 422

    result = await na.run_native_sync(user_id="pm-user", model_keys=["claude-bravo-6-0-oauth"])
    assert result["applied"] is True
    assert result["added_keys"] == ["claude-bravo-6-0-oauth"]
    invalidate_spy.assert_called_once()

    data = json.loads(overlay_path.read_text(encoding="utf-8"))
    assert "claude-bravo-6-0-oauth" in data["models"]
    assert data["models"]["claude-bravo-6-0-oauth"]["model_id"] == "claude-bravo-6-0"


@pytest.mark.asyncio
async def test_native_catalog_collect_maps_xai_name_and_levels(monkeypatch):
    import src.dalton.llm.providers.xai_grok.oauth as xai_oauth_mod

    async def _token(provider: str, user_id: str) -> dict[str, Any] | None:
        return {"access_token": "tok", "account_id": "", "plan_type": "supergrok"}

    async def _fake_fetch_xai(access_token: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "id": "grok-4.9",
                "name": "Grok 4.9",
                "context_window": 500000,
                "supports_reasoning_effort": True,
                "reasoning_efforts": ["low", "high"],
            }
        ]

    monkeypatch.setattr(nc, "_resolve_provider_token", create_autospec(nc._resolve_provider_token, side_effect=_token))
    monkeypatch.setattr(
        xai_oauth_mod, "fetch_xai_models", create_autospec(xai_oauth_mod.fetch_xai_models, side_effect=_fake_fetch_xai)
    )

    listings = await nc.collect_native_listings("user-1")
    xai = listings["xai-oauth"]
    assert xai.status == "ok"
    assert len(xai.models) == 1
    mapped = xai.models[0]
    # Display name comes from the upstream `name`; no invented max output.
    assert mapped.display_name == "Grok 4.9"
    assert mapped.reasoning_efforts == ["low", "high"]
    assert mapped.max_output is None


def test_native_catalog_diff_flags_duplicate_display_names():
    llm_config = {
        "gemini-4-argon-low-oauth": {
            "model_id": "gemini-4-argon-low",
            "provider": "antigravity-oauth",
            "display_name": "Gemini 4 Argion Low",
        },
    }
    listing = ProviderListing(
        provider="antigravity-oauth",
        display_name="Google Antigravity",
        status="ok",
        models=[
            UpstreamModel(id="gemini-4-argon-low"),
            # Two new ids sharing one display name (old upstream aliases)…
            UpstreamModel(id="gemini-2.5-flash", display_name="Gemini 3.5 Flash Lite"),
            UpstreamModel(id="gemini-2.5-flash-lite", display_name="Gemini 3.5 Flash Lite"),
            # …a new id reusing an existing catalog display name…
            UpstreamModel(id="gemini-2.5-flash-thinking", display_name="Gemini 4 Argion Low"),
            # …and a clean one.
            UpstreamModel(id="gemini-4-corrinth-low", display_name="Gemini 4 Corrinth (low)"),
        ],
    )

    diff = build_native_diff({"antigravity-oauth": listing}, llm_config)

    by_key = {item["suggested_key"]: item for item in diff["added"]}
    assert by_key["gemini-2.5-flash-oauth"]["duplicate_display_name"] is True
    assert by_key["gemini-2.5-flash-lite-oauth"]["duplicate_display_name"] is True
    assert by_key["gemini-2.5-flash-thinking-oauth"]["duplicate_display_name"] is True
    assert by_key["gemini-4-corrinth-low-oauth"]["duplicate_display_name"] is False


@pytest.mark.asyncio
async def test_native_model_status_keeps_succeeded_diff_after_failed_check(monkeypatch):
    """Regression: a later all-failed check must not hide the last good diff.

    Success check first, then a fully failed one; status must still surface the
    succeeded check's diff/provider rows, with the failed run only as
    "上次检查".
    """
    succeeded_run = {
        "run_id": "00000000-0000-0000-0000-0000000000d1",
        "status": "succeeded",
        "started_at": "2026-10-23T10:00:00+00:00",
        "diff": {
            "added": [
                {
                    "provider": "xai-oauth",
                    "upstream_id": "grok-4.9",
                    "display_name": "Grok 4.9",
                    "suggested_key": "grok-4.9-oauth",
                    "conflict": False,
                }
            ],
            "deprecated": [
                {"provider": "xai-oauth", "key": "grok-4.3-oauth", "model_id": "grok-4.3"}
            ],
            "unchanged": 3,
            "providers": [
                {"provider": "xai-oauth", "display_name": "xAI SuperGrok", "status": "ok", "model_count": 4, "message": None}
            ],
        },
    }
    failed_run = {
        "run_id": "00000000-0000-0000-0000-0000000000d2",
        "status": "failed",
        "started_at": "2026-10-23T11:00:00+00:00",
        "error": "no_provider_ok",
        "diff": {
            "providers": [
                {"provider": "xai-oauth", "display_name": "xAI SuperGrok", "status": "error", "model_count": 0, "message": "该供应商未连接，无法检查"}
            ]
        },
    }

    monkeypatch.setattr(
        na,
        "fetch_recent_native_runs",
        create_autospec(na.fetch_recent_native_runs, return_value=(failed_run, None)),
    )
    monkeypatch.setattr(
        na,
        "fetch_last_succeeded_native_check",
        create_autospec(na.fetch_last_succeeded_native_check, return_value=succeeded_run),
    )
    # Patch the names the service actually resolves, and pin the catalog so a model
    # already present locally cannot hide the expected "added" entry.
    monkeypatch.setattr(
        na, "load_native_sync_overlay", create_autospec(nc.load_native_sync_overlay, return_value=None)
    )
    from src.llms.llm import LLM

    fixed_cfg = create_autospec(type(LLM.get_model_config()), instance=True)
    fixed_cfg.llm_config = {}
    monkeypatch.setattr(LLM, "get_model_config", classmethod(lambda cls: fixed_cfg))

    status = await na.get_native_status()

    # Last-run line reflects the failed attempt…
    assert status["last_check"]["status"] == "failed"
    # …but the diff and provider rows are the succeeded check's.
    assert status["last_succeeded_check"]["run_id"] == succeeded_run["run_id"]
    assert [i["suggested_key"] for i in status["diff"]["added"]] == ["grok-4.9-oauth"]
    assert [d["key"] for d in status["diff"]["deprecated"]] == ["grok-4.3-oauth"]
    assert status["providers"][0]["status"] == "ok"
    assert status["providers"][0]["provider"] == "xai-oauth"


def test_openclaw_sync_run_queries_filter_by_source():
    """OpenClaw status must not surface native runs (d012 `source` column)."""
    import inspect

    import src.dalton.database.openclaw_sync as oc_sync_db
    import src.dalton.database.native_sync as native_sync_db

    src = inspect.getsource(oc_sync_db.fetch_recent_sync_runs)
    assert "source = 'openclaw'" in src
    src_native = inspect.getsource(native_sync_db.fetch_recent_native_runs)
    assert "source = 'native'" in src_native


# ---------------------------------------------------------------------------
# Router wiring + PM gate
# ---------------------------------------------------------------------------


@pytest.fixture()
def native_client(monkeypatch: pytest.MonkeyPatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import src.dalton.app.router as dalton_router_mod
    from src.dalton.app import dalton_router
    from src.server.utils.api import get_current_user_id

    # The router imports the service functions by name, so the stubs go on the
    # router's own namespace (the wiring test pins the layers underneath).
    monkeypatch.setattr(
        dalton_router_mod,
        "get_native_status",
        create_autospec(
            na.get_native_status,
            return_value={
                "providers": [
                    {"provider": "claude-oauth", "display_name": "Claude 订阅", "status": "never", "model_count": 0, "message": "尚未检查"}
                ],
                "last_check": None,
                "last_sync": None,
                "diff": {"added": [], "deprecated": [], "unchanged": 0},
                "overlay": {"exists": False, "generated_at": None, "model_count": 0, "path": "var/dalton/catalog/native_sync_overlay.json"},
            },
        ),
    )
    monkeypatch.setattr(
        dalton_router_mod,
        "run_native_check",
        create_autospec(
            na.run_native_check,
            return_value={
                "run_id": "00000000-0000-0000-0000-0000000000bb",
                "checked_at": "2026-10-23T00:00:00Z",
                "providers": [],
                "diff": {"added": [], "deprecated": [], "unchanged": 5},
                "counts": {},
                "warnings": [],
            },
        ),
    )
    sync_spy = create_autospec(
        na.run_native_sync,
        return_value={
            "run_id": "00000000-0000-0000-0000-0000000000cc",
            "applied": True,
            "added_keys": ["claude-bravo-6-0-oauth"],
            "skipped_keys": [],
            "synced_at": "2026-10-23T00:00:00Z",
        },
    )
    monkeypatch.setattr(dalton_router_mod, "run_native_sync", sync_spy)

    app = FastAPI()
    app.dependency_overrides[get_current_user_id] = lambda: "local-dev-user"
    app.include_router(dalton_router)
    client = TestClient(app, raise_server_exceptions=False)
    return client, sync_spy


def test_native_models_endpoints_pm_gate_and_wiring(native_client):
    client, sync_spy = native_client

    resp = client.get("/api/v1/dalton/native-models/status")
    assert resp.status_code == 200
    assert resp.json()["providers"][0]["provider"] == "claude-oauth"

    # local-dev-user is the default PM.
    resp_check = client.post("/api/v1/dalton/native-models/check")
    assert resp_check.status_code == 200

    resp_sync = client.post(
        "/api/v1/dalton/native-models/sync", json={"models": ["claude-bravo-6-0-oauth"]}
    )
    assert resp_sync.status_code == 200
    sync_spy.assert_called_once_with(user_id="local-dev-user", model_keys=["claude-bravo-6-0-oauth"])


def test_native_models_sync_rejects_empty_and_non_pm(native_client, monkeypatch):
    client, sync_spy = native_client

    resp_empty = client.post("/api/v1/dalton/native-models/sync", json={"models": []})
    assert resp_empty.status_code == 422

    monkeypatch.setenv("DALTON_PM_USER_IDS", "other-pm@example.com")
    resp_forbidden = client.post(
        "/api/v1/dalton/native-models/sync", json={"models": ["claude-bravo-6-0-oauth"]}
    )
    assert resp_forbidden.status_code == 403
    resp_check_forbidden = client.post("/api/v1/dalton/native-models/check")
    assert resp_check_forbidden.status_code == 403
    # Status stays readable for non-PM viewers.
    resp_status = client.get("/api/v1/dalton/native-models/status")
    assert resp_status.status_code == 200
    sync_spy.assert_not_called()
