"""Batch 1 unit tests for Researchdata Connectors split:

1. Migration ``d052_embedding_metadata_and_dimension`` metadata DDL and downgrade contract.
2. Host platform configuration (``DALTON_HOST_PLATFORM``) across ``capabilities.py``,
   ``scripts/standalone/init.sh``, ``scripts/dalton/bridge_subprocess_env.py``, and
   ``scripts/dalton/ops/prod/deploy_main.sh``.
3. Backend embeddings module (``src/dalton/embeddings/``): dual-profile config,
   ``gemini-embedding-2-preview`` -> ``gemini-embedding-2`` alias mapping, per-text
   ``Content(parts=[Part(text=t)])`` batching, Gemini/OpenAI retry & backoff,
   and 1536/3072-d HNSW search.
4. ``/jobs/ops`` status fileization (``export_ops_status.py``, launchd plist template,
   ``system_jobs.py`` freshness/staleness rejection and ``DALTON_OPS_JOBS_VIA_BRIDGE``
   rollback) plus ``ops_jobs_enabled`` prompt gating and ``DALTON-HOOK`` markers.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import importlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import httpx
import psycopg
import pytest

from ptc_agent.agent.prompts import loader as prompt_loader_mod
from ptc_agent.agent.middleware.runtime_context.profile import answer_preferences
from src.dalton.autonomy import system_jobs as system_jobs_mod
from src.dalton.capabilities import (
    CAPABILITY_SPECS,
    CapabilityId,
    effective_capabilities,
    has_capability,
    resolve_host_platform,
)
from src.dalton.conversations import embedder as conv_embedder
from src.dalton.conversations import search as conv_search
from src.dalton.database import conversations as db_conv
from src.dalton.embeddings import (
    DALTON_EMBEDDING_METADATA_DDL,
    DEFAULT_CORE_DIMENSIONS,
    DEFAULT_EVERFLOW_API_MODEL_NAME,
    DEFAULT_EVERFLOW_DIMENSIONS,
    DEFAULT_EVERFLOW_MODEL_LABEL,
    DEFAULT_MAX_DIRECT_CONCURRENCY,
    EmbeddingConfig,
    EmbeddingConfigError,
    EmbeddingDimensionMismatchError,
    EmbeddingMetadata,
    EmbeddingMetadataMismatchError,
    EmbeddingProviderHTTPError,
    EmbeddingResponseError,
    build_alter_embedding_column_ddl,
    build_halfvec_index_ddl,
    embed_query_direct,
    embed_texts_direct,
    ensure_conversation_embedding_schema,
    resolve_api_model_name,
    resolve_embedding_config,
    upsert_embedding_metadata,
    validate_embedding_metadata_match,
)
from src.dalton.embeddings import client as emb_client
from src.dalton.embeddings import metadata as emb_meta
from src.dalton.profile import DeploymentProfile
from src.dalton.settings import get_dalton_config
from src.dalton.tools import conversation_tools as conv_tools_mod

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import bridge_subprocess_env as bse  # noqa: E402

OPS_EXPORTER_PATH = REPO_ROOT / "scripts" / "dalton" / "ops" / "prod" / "export_ops_status.py"
OPS_PLIST_TEMPLATE = (
    REPO_ROOT / "scripts" / "dalton" / "launchd" / "com.dalton.ops-status-exporter.plist.template"
)
DEPLOY_MAIN_SH = REPO_ROOT / "scripts" / "dalton" / "ops" / "prod" / "deploy_main.sh"
INIT_SH = REPO_ROOT / "scripts" / "standalone" / "init.sh"


def _load_export_ops_status_module() -> Any:
    spec = importlib.util.spec_from_file_location("export_ops_status_test_mod", OPS_EXPORTER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _clear_b1_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "APP_ENV",
        "DALTON_PROFILE",
        "DALTON_ENABLED",
        "DALTON_HOST_PLATFORM",
        "DALTON_EMBEDDING_PROVIDER",
        "DALTON_EMBEDDING_MODEL",
        "DALTON_EMBEDDING_API_MODEL",
        "DALTON_EMBEDDING_DIMENSIONS",
        "DALTON_EMBEDDING_TASK_TYPE_DOC",
        "DALTON_EMBEDDING_TASK_TYPE_QUERY",
        "DALTON_EMBEDDING_BASE_URL",
        "DALTON_EMBEDDING_API_KEY",
        "DALTON_EMBED_MAX_CONCURRENCY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "DALTON_OPS_JOBS_VIA_BRIDGE",
        "DALTON_OPS_STATUS_FILE",
        "DALTON_OPS_STATUS_MAX_AGE_SECONDS",
        "DALTON_BACKEND_VAR_DIR",
        "DALTON_CAP_RESEARCHDATA_BRIDGE",
        "DALTON_CAP_ALPHAENGINE",
        "DALTON_CAP_DATA_JOBS",
    ):
        monkeypatch.delenv(key, raising=False)
    get_dalton_config.cache_clear()


# ---------------------------------------------------------------------------
# 1. Migration d052 & metadata DDL
# ---------------------------------------------------------------------------


def test_d052_migration_revision_and_downgrade_guards() -> None:
    mod = importlib.import_module(
        "src.dalton.migrations.versions.d052_embedding_metadata_and_dimension"
    )
    assert mod.revision == "d052"
    assert mod.down_revision == "d049"

    src_text = Path(mod.__file__).read_text(encoding="utf-8")
    assert "dalton_embedding_metadata" in src_text
    assert "model_label" in src_text
    assert "api_model_name" in src_text
    assert "gemini-embedding-2-preview" in src_text
    assert "gemini-embedding-2" in src_text
    assert "Refusing in-place downgrade of d052" in src_text
    assert "halfvec(3072)" in src_text
    assert "dalton_embedding_metadata" in DALTON_EMBEDDING_METADATA_DDL
    assert "vector(1536)" in build_alter_embedding_column_ddl(1536)
    assert "halfvec(1536)" in build_halfvec_index_ddl(1536)


# ---------------------------------------------------------------------------
# 2. DALTON_HOST_PLATFORM gating & script integration
# ---------------------------------------------------------------------------


def test_resolve_host_platform_and_batch1_depends_on_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_b1_env(monkeypatch)
    # Remaining 2 legacy-bridge sub-capabilities retain depends_on=(RESEARCHDATA_BRIDGE,).
    for cap in (
        CapabilityId.ALPHAENGINE,
        CapabilityId.FEISHU_DOCS,
    ):
        assert CAPABILITY_SPECS[cap].depends_on == (CapabilityId.RESEARCHDATA_BRIDGE,)
    # Batch 2 (docs/dalton/features/researchdata-connectors.md §6.3) decoupled
    # TRANSCRIPTION from RESEARCHDATA_BRIDGE so standalone dalton-transcription runs without the bridge.
    assert CAPABILITY_SPECS[CapabilityId.TRANSCRIPTION].depends_on == ()
    # Batch 3 (docs/dalton/features/researchdata-connectors.md §6.4 #4) decoupled the five
    # research connector capabilities from RESEARCHDATA_BRIDGE so they run independently
    # on dalton-research-host (:8951) in both Standalone Core and Everflow.
    for cap in (
        CapabilityId.GUIDEPOINT,
        CapabilityId.COMPANY_WIKI,
        CapabilityId.SALES_NOTES,
        CapabilityId.TMTB,
        CapabilityId.FUNDA_AI,
    ):
        assert CAPABILITY_SPECS[cap].depends_on == ()

    # Explicit macos keeps alphaengine enabled even inside a Linux container.
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("APP_ENV", "dalton")
    monkeypatch.setenv("DALTON_HOST_PLATFORM", "macos")
    assert resolve_host_platform() == "macos"
    assert has_capability(CapabilityId.ALPHAENGINE) is True

    # Explicit darwin normalizes to macos.
    monkeypatch.setenv("DALTON_HOST_PLATFORM", "Darwin")
    assert resolve_host_platform() == "macos"

    # Explicit linux disables alphaengine even if sys.platform is darwin.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("DALTON_HOST_PLATFORM", "linux")
    assert resolve_host_platform() == "linux"
    assert has_capability(CapabilityId.ALPHAENGINE) is False

    # Unrecognized value fails closed for alphaengine.
    monkeypatch.setenv("DALTON_HOST_PLATFORM", "windows")
    assert resolve_host_platform() == "unknown"
    assert has_capability(CapabilityId.ALPHAENGINE) is False

    # Unset defaults follow deployment profile, never sys.platform.
    monkeypatch.delenv("DALTON_HOST_PLATFORM", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    assert resolve_host_platform(profile=DeploymentProfile.EVERFLOW) == "macos"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert resolve_host_platform(profile=DeploymentProfile.CORE) == "linux"
    caps_core_linux = effective_capabilities(
        profile=DeploymentProfile.CORE,
        environ={
            "DALTON_CAP_RESEARCHDATA_BRIDGE": "true",
            "DALTON_CAP_ALPHAENGINE": "true",
        },
        dalton_enabled=True,
    )
    assert CapabilityId.ALPHAENGINE not in caps_core_linux


def test_deploy_main_ensure_everflow_host_platform_env(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("APP_ENV=dalton\nDALTON_ENABLED=1\n", encoding="utf-8")
    os.chmod(env_file, 0o600)

    res_add = subprocess.run(
        [
            "bash",
            "-c",
            (
                f"set -uo pipefail; DALTON_DEPLOY_LIB_ONLY=1; source '{DEPLOY_MAIN_SH}'; "
                f"RECREATE=0; ensure_everflow_host_platform_env '{env_file}'; "
                'rc=$?; echo "RC=$rc RECREATE=$RECREATE"'
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_add.returncode == 0, res_add.stderr
    assert "RC=0 RECREATE=1" in res_add.stdout
    assert "DALTON_HOST_PLATFORM=macos" in env_file.read_text(encoding="utf-8")

    # Idempotent when already macos (does not force RECREATE).
    res_noop = subprocess.run(
        [
            "bash",
            "-c",
            (
                f"set -uo pipefail; DALTON_DEPLOY_LIB_ONLY=1; source '{DEPLOY_MAIN_SH}'; "
                f"RECREATE=0; ensure_everflow_host_platform_env '{env_file}'; "
                'rc=$?; echo "RC=$rc RECREATE=$RECREATE"'
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_noop.returncode == 0, res_noop.stderr
    assert "RC=0 RECREATE=0" in res_noop.stdout

    # Rejects non-macos value on Everflow production.
    env_file.write_text("APP_ENV=dalton\nDALTON_HOST_PLATFORM=linux\n", encoding="utf-8")
    res_bad = subprocess.run(
        [
            "bash",
            "-c",
            (
                f"set -uo pipefail; DALTON_DEPLOY_LIB_ONLY=1; source '{DEPLOY_MAIN_SH}'; "
                f"ensure_everflow_host_platform_env '{env_file}'"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_bad.returncode != 0
    assert "DALTON_HOST_PLATFORM=macos" in res_bad.stderr


def test_init_sh_detect_and_validate_host_platform(tmp_path: Path) -> None:
    init_text = INIT_SH.read_text(encoding="utf-8")
    assert "detect_host_platform()" in init_text
    assert 'write_env_key "DALTON_HOST_PLATFORM"' in init_text

    # Extract and test detect_host_platform + validate_host_platform against stubbed uname.
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    uname_stub = stub_bin / "uname"
    for fake_os, expected in (("Darwin", "macos"), ("Linux", "linux")):
        uname_stub.write_text(f"#!/usr/bin/env bash\necho '{fake_os}'\n", encoding="utf-8")
        uname_stub.chmod(0o755)
        snippet = (
            "set -euo pipefail\n"
            'warn() { printf "%s\\n" "$1" >&2; }\n'
            'err() { printf "%s\\n" "$1" >&2; }\n'
            + init_text[
                init_text.index("detect_host_platform()") : init_text.index("normalize_domain()")
            ]
            + '\ndetect_host_platform\n'
        )
        res = subprocess.run(
            ["bash", "-c", snippet],
            env={"PATH": f"{stub_bin}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == expected


def test_bridge_subprocess_env_host_platform(tmp_path: Path) -> None:
    # merge_bridge_and_repo_env_files writes DALTON_HOST_PLATFORM=macos by default and rejects bad values.
    bridge_env = tmp_path / "bridge.env"
    repo_env = tmp_path / "repo.env"
    bse.merge_bridge_and_repo_env_files(
        bridge_env_path=bridge_env,
        repo_env_path=repo_env,
        default_python_bin=sys.executable,
    )
    _, repo_vals = bse.parse_env_file_strict(repo_env)
    assert repo_vals["DALTON_HOST_PLATFORM"] == "macos"

    bad_repo_env = tmp_path / "bad_repo.env"
    bad_repo_env.write_text("DALTON_HOST_PLATFORM=freebsd\n", encoding="utf-8")
    with pytest.raises(bse.EnvFileValidationError, match="DALTON_HOST_PLATFORM"):
        bse.merge_bridge_and_repo_env_files(
            bridge_env_path=bridge_env,
            repo_env_path=bad_repo_env,
            default_python_bin=sys.executable,
        )


# ---------------------------------------------------------------------------
# 3. Backend embeddings module: parity, batching, retry/backoff, rollback, 1536/3072-d
# ---------------------------------------------------------------------------


def test_embedding_config_dual_profile_and_alias_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_b1_env(monkeypatch)
    assert resolve_api_model_name("gemini-embedding-2-preview") == "gemini-embedding-2"
    assert resolve_api_model_name("text-embedding-3-small") == "text-embedding-3-small"

    ef_cfg = resolve_embedding_config(profile=DeploymentProfile.EVERFLOW, environ={})
    assert ef_cfg.provider == "gemini"
    assert ef_cfg.model_label == DEFAULT_EVERFLOW_MODEL_LABEL
    assert ef_cfg.api_model_name == DEFAULT_EVERFLOW_API_MODEL_NAME
    assert ef_cfg.dimensions == DEFAULT_EVERFLOW_DIMENSIONS

    core_openai_cfg = resolve_embedding_config(
        profile=DeploymentProfile.CORE,
        environ={"OPENAI_API_KEY": "sk-test"},
    )
    assert core_openai_cfg.provider == "openai"
    assert core_openai_cfg.model_label == "text-embedding-3-small"
    assert core_openai_cfg.api_model_name == "text-embedding-3-small"
    assert core_openai_cfg.dimensions == DEFAULT_CORE_DIMENSIONS

    core_gemini_cfg = resolve_embedding_config(
        profile=DeploymentProfile.CORE,
        environ={"GEMINI_API_KEY": "gk-test"},
    )
    assert core_gemini_cfg.provider == "gemini"
    assert core_gemini_cfg.model_label == "gemini-embedding-2-preview"
    assert core_gemini_cfg.api_model_name == "gemini-embedding-2"
    assert core_gemini_cfg.dimensions == DEFAULT_CORE_DIMENSIONS


@pytest.mark.asyncio
async def test_gemini_batch_uses_per_text_content_and_maps_api_model_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_b1_env(monkeypatch)
    from google.genai import types as genai_types

    captured_calls: list[dict[str, Any]] = []

    class _FakeModels:
        def embed_content(
            self,
            *,
            model: str,
            contents: Any,
            config: dict[str, Any],
        ) -> Any:
            captured_calls.append({"model": model, "contents": contents, "config": config})
            n = len(contents)
            return SimpleNamespace(
                embeddings=[
                    SimpleNamespace(values=[float(i + 1) / 10.0, 0.5, 0.25] + [0.0] * 3069)
                    for i in range(n)
                ]
            )

    fake_client = SimpleNamespace(models=_FakeModels())
    monkeypatch.setattr(
        emb_client,
        "_create_gemini_client",
        create_autospec(emb_client._create_gemini_client, return_value=fake_client),
    )

    cfg = EmbeddingConfig(
        provider="gemini",
        model_label="gemini-embedding-2-preview",
        api_model_name="gemini-embedding-2",
        dimensions=3072,
    )
    vecs, returned_label = await embed_texts_direct(
        ["NVDA Blackwell 毛利率", "TSMC CoWoS capacity"],
        task_type="RETRIEVAL_DOCUMENT",
        config=cfg,
        api_key="gk-test",
    )

    # Stored label stays preview; upstream API call uses GA model name.
    assert returned_label == "gemini-embedding-2-preview"
    assert len(vecs) == 2
    assert len(vecs[0]) == 3072
    assert conv_search.cosine_similarity(vecs[0], vecs[0]) == pytest.approx(1.0)

    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert call["model"] == "gemini-embedding-2"
    assert call["config"] == {"task_type": "RETRIEVAL_DOCUMENT"}
    # Critical §2.5 invariant: every text is wrapped in its own Content(parts=[Part(text=...)])
    assert isinstance(call["contents"], list) and len(call["contents"]) == 2
    for item, expected_text in zip(call["contents"], ["NVDA Blackwell 毛利率", "TSMC CoWoS capacity"]):
        assert isinstance(item, genai_types.Content)
        assert len(item.parts) == 1
        assert item.parts[0].text == expected_text


@pytest.mark.asyncio
async def test_embedding_retry_backoff_gemini_and_openai_and_permanent_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_b1_env(monkeypatch)
    slept: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(emb_client.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(emb_client, "GEMINI_RATE_LIMIT_DELAYS_S", (5.0, 10.0))
    monkeypatch.setattr(emb_client, "OPENAI_RATE_LIMIT_DELAYS_S", (2.0, 5.0))
    monkeypatch.setattr(emb_client, "OPENAI_TRANSIENT_DELAYS_S", (1.0, 2.0))

    # 1) Gemini 429 RESOURCE_EXHAUSTED retries with GEMINI_RATE_LIMIT_DELAYS_S.
    g_cfg = EmbeddingConfig(
        provider="gemini",
        model_label="gemini-embedding-2-preview",
        api_model_name="gemini-embedding-2",
        dimensions=4,
    )
    g_mock = create_autospec(
        emb_client._call_gemini_embed_sync,
        side_effect=[
            RuntimeError("429 RESOURCE_EXHAUSTED"),
            [[1.0, 0.0, 0.0, 0.0]],
        ],
    )
    monkeypatch.setattr(emb_client, "_call_gemini_embed_sync", g_mock)
    vecs, label = await embed_texts_direct(["hello"], config=g_cfg, api_key="gk-test", retry=True)
    assert label == "gemini-embedding-2-preview"
    assert vecs == [[1.0, 0.0, 0.0, 0.0]]
    assert g_mock.call_count == 2
    assert slept == [5.0]

    # 2) OpenAI 429 honors Retry-After capped at MAX_RETRY_AFTER_S, and permanent 401 fails immediately.
    slept.clear()
    orig_openai_embed = emb_client._call_openai_embed_async
    o_cfg = EmbeddingConfig(
        provider="openai",
        model_label="text-embedding-3-small",
        api_model_name="text-embedding-3-small",
        dimensions=4,
    )
    o_mock = create_autospec(
        orig_openai_embed,
        side_effect=[
            EmbeddingProviderHTTPError("openai", 429, "rate_limit_exceeded", retry_after_s=12.0),
            [[0.0, 1.0, 0.0, 0.0]],
        ],
    )
    monkeypatch.setattr(emb_client, "_call_openai_embed_async", o_mock)
    o_vecs, o_label = await embed_texts_direct(["hi"], config=o_cfg, api_key="sk-test", retry=True)
    assert o_label == "text-embedding-3-small"
    assert o_vecs == [[0.0, 1.0, 0.0, 0.0]]
    assert slept == [12.0]

    slept.clear()
    o_401 = create_autospec(
        orig_openai_embed,
        side_effect=[EmbeddingProviderHTTPError("openai", 401, "invalid_api_key")],
    )
    monkeypatch.setattr(emb_client, "_call_openai_embed_async", o_401)
    with pytest.raises(EmbeddingProviderHTTPError):
        await embed_texts_direct(["hi"], config=o_cfg, api_key="sk-bad", retry=True)
    assert o_401.call_count == 1
    assert slept == []

    # 3) Interactive query path (`retry=False`) does not retry transient errors.
    slept.clear()
    o_503 = create_autospec(
        orig_openai_embed,
        side_effect=[EmbeddingProviderHTTPError("openai", 503, "unavailable")],
    )
    monkeypatch.setattr(emb_client, "_call_openai_embed_async", o_503)
    with pytest.raises(EmbeddingProviderHTTPError):
        await embed_query_direct("q", config=o_cfg, api_key="sk-test", retry=False)
    assert o_503.call_count == 1
    assert slept == []

    # 4) Missing API key raises EmbeddingConfigError immediately.
    with pytest.raises(EmbeddingConfigError):
        await embed_texts_direct(["hi"], config=o_cfg, api_key=None)

    # 5) Mismatched vector count raises EmbeddingResponseError without retrying.
    with pytest.raises(EmbeddingResponseError):
        emb_client._validate_batch_vectors(
            [[1.0, 0.0, 0.0, 0.0]],
            expected_count=2,
            expected_dimensions=4,
            provider="gemini",
        )

    # 6) _call_openai_embed_async orders response by index and parses Retry-After on 429.
    monkeypatch.setattr(emb_client, "_call_openai_embed_async", orig_openai_embed)
    req = httpx.Request("POST", "https://api.openai.com/v1/embeddings")
    ok_resp = httpx.Response(
        200,
        json={
            "data": [
                {"index": 1, "embedding": [0.0, 1.0, 0.0, 0.0]},
                {"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]},
            ]
        },
        request=req,
    )
    rl_resp = httpx.Response(
        429,
        headers={"Retry-After": "9"},
        json={"error": {"message": "rate_limit_exceeded"}},
        request=req,
    )
    mock_http_client = create_autospec(httpx.AsyncClient, instance=True)
    mock_http_client.__aenter__.return_value = mock_http_client
    mock_http_client.__aexit__.return_value = None
    mock_http_client.post.side_effect = [ok_resp, rl_resp]
    monkeypatch.setattr(
        emb_client.httpx,
        "AsyncClient",
        create_autospec(emb_client.httpx.AsyncClient, return_value=mock_http_client),
    )

    ordered = await emb_client._call_openai_embed_async(
        ["first", "second"],
        api_model_name="text-embedding-3-small",
        dimensions=4,
        api_key="sk-test",
        base_url="https://api.openai.com/v1",
        timeout_s=10.0,
    )
    assert ordered == [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]

    with pytest.raises(EmbeddingProviderHTTPError) as rl_exc:
        await emb_client._call_openai_embed_async(
            ["first"],
            api_model_name="text-embedding-3-small",
            dimensions=4,
            api_key="sk-test",
            base_url="https://api.openai.com/v1",
            timeout_s=10.0,
        )
    assert rl_exc.value.code == 429
    assert rl_exc.value.retry_after_s == 9.0


@pytest.mark.asyncio
async def test_embedder_uses_direct_backend_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_b1_env(monkeypatch)

    direct_mock = create_autospec(
        conv_embedder.embed_texts_direct,
        return_value=([[0.1, 0.2, 0.3, 0.4]], "gemini-embedding-2-preview"),
    )
    monkeypatch.setattr(conv_embedder, "embed_texts_direct", direct_mock)

    vecs_direct, model_direct = await conv_embedder.embed_texts(["doc"])
    assert vecs_direct == [[0.1, 0.2, 0.3, 0.4]]
    assert model_direct == "gemini-embedding-2-preview"
    assert direct_mock.call_count == 1


@pytest.mark.asyncio
async def test_search_cards_dense_respects_1536_and_3072_locked_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed_sql: list[str] = []

    fake_cur = create_autospec(psycopg.AsyncCursor, instance=True)

    async def _exec(sql: Any, params: Any = None) -> None:
        executed_sql.append(str(sql))

    fake_cur.execute.side_effect = _exec
    fake_cur.fetchall.return_value = [{"card_id": "c1", "dense_score": 0.99}]

    fake_conn = create_autospec(psycopg.AsyncConnection, instance=True)

    @asynccontextmanager
    async def _cur_ctx(*args: Any, **kwargs: Any):
        yield fake_cur

    fake_conn.cursor.side_effect = _cur_ctx

    # 1) Core 1536-d locked metadata emits halfvec(1536) HNSW expression.
    orig_get_meta = emb_meta.get_embedding_metadata
    meta_1536 = EmbeddingMetadata(
        scope="conversation_archive",
        provider="openai",
        model_label="text-embedding-3-small",
        api_model_name="text-embedding-3-small",
        dimensions=1536,
    )
    monkeypatch.setattr(
        emb_meta,
        "get_embedding_metadata",
        create_autospec(orig_get_meta, return_value=meta_1536),
    )
    rows_1536 = await db_conv.search_cards_dense([0.01] * 1536, conn=fake_conn)
    assert len(rows_1536) == 1
    assert "halfvec(1536)" in executed_sql[-1]
    assert "halfvec(3072)" not in executed_sql[-1]

    # 2) Mismatched query vector length fails immediately with EmbeddingDimensionMismatchError.
    with pytest.raises(EmbeddingDimensionMismatchError):
        await db_conv.search_cards_dense([0.01] * 3072, conn=fake_conn)

    # 3) Everflow 3072-d locked metadata emits halfvec(3072).
    meta_3072 = EmbeddingMetadata(
        scope="conversation_archive",
        provider="gemini",
        model_label="gemini-embedding-2-preview",
        api_model_name="gemini-embedding-2",
        dimensions=3072,
    )
    monkeypatch.setattr(
        emb_meta,
        "get_embedding_metadata",
        create_autospec(orig_get_meta, return_value=meta_3072),
    )
    rows_3072 = await db_conv.search_cards_dense([0.01] * 3072, conn=fake_conn)
    assert len(rows_3072) == 1
    assert "halfvec(3072)" in executed_sql[-1]


@pytest.mark.asyncio
async def test_ensure_conversation_embedding_schema_alters_empty_table_and_blocks_mismatch() -> None:
    # Validate metadata mismatch detection directly.
    locked = EmbeddingMetadata(
        scope="conversation_archive",
        provider="gemini",
        model_label="gemini-embedding-2-preview",
        api_model_name="gemini-embedding-2",
        dimensions=3072,
    )
    with pytest.raises(EmbeddingDimensionMismatchError):
        validate_embedding_metadata_match(
            EmbeddingConfig("openai", "text-embedding-3-small", "text-embedding-3-small", 1536),
            locked,
        )
    with pytest.raises(EmbeddingMetadataMismatchError):
        validate_embedding_metadata_match(
            EmbeddingConfig("gemini", "gemini-embedding-001", "gemini-embedding-001", 3072),
            locked,
        )

    # Empty Core table altered from 3072 to 1536 and locked.
    executed: list[str] = []
    fetchone_queue = [
        None,  # SELECT ... FROM dalton_embedding_metadata FOR UPDATE (unlocked)
        {"col_type": "vector(3072)"},  # pg_attribute format_type
        {"cnt": 0},  # count(*) WHERE embedding IS NOT NULL
        {
            "scope": "conversation_archive",
            "provider": "openai",
            "model_label": "text-embedding-3-small",
            "api_model_name": "text-embedding-3-small",
            "dimensions": 1536,
            "task_type_doc": "RETRIEVAL_DOCUMENT",
            "task_type_query": "RETRIEVAL_QUERY",
        },
    ]

    fake_cur = create_autospec(psycopg.AsyncCursor, instance=True)

    async def _exec(sql: Any, params: Any = None) -> None:
        executed.append(" ".join(str(sql).split()))

    async def _fetchone() -> Any:
        return fetchone_queue.pop(0)

    fake_cur.execute.side_effect = _exec
    fake_cur.fetchone.side_effect = _fetchone

    fake_conn = create_autospec(psycopg.AsyncConnection, instance=True)

    @asynccontextmanager
    async def _tx_ctx():
        yield

    @asynccontextmanager
    async def _cur_ctx(*args: Any, **kwargs: Any):
        yield fake_cur

    fake_conn.transaction.side_effect = _tx_ctx
    fake_conn.cursor.side_effect = _cur_ctx

    cfg_1536 = EmbeddingConfig("openai", "text-embedding-3-small", "text-embedding-3-small", 1536)
    res_meta = await ensure_conversation_embedding_schema(cfg_1536, conn=fake_conn)
    assert res_meta.dimensions == 1536
    assert "pg_advisory_xact_lock(hashtext(%s))" in executed[0]
    advisory_idx = next(i for i, q in enumerate(executed) if "pg_advisory_xact_lock" in q)
    for_update_idx = next(i for i, q in enumerate(executed) if "FOR UPDATE" in q)
    assert advisory_idx < for_update_idx
    assert any("ALTER COLUMN embedding TYPE vector(1536)" in q for q in executed)
    assert any("USING hnsw ((embedding::halfvec(1536)) halfvec_cosine_ops)" in q for q in executed)

    # Task type mismatch is also rejected by validate_embedding_metadata_match.
    with pytest.raises(EmbeddingMetadataMismatchError, match="task_type_query"):
        validate_embedding_metadata_match(
            EmbeddingConfig(
                "gemini",
                "gemini-embedding-2-preview",
                "gemini-embedding-2",
                3072,
                task_type_doc="RETRIEVAL_DOCUMENT",
                task_type_query="SEMANTIC_SIMILARITY",
            ),
            locked,
        )


def test_d052_migration_core_empty_aligns_1536_unlocked_and_everflow_seeds_3072(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_b1_env(monkeypatch)
    mod = importlib.import_module(
        "src.dalton.migrations.versions.d052_embedding_metadata_and_dimension"
    )

    executed_core: list[str] = []
    orig_op_execute = mod.op.execute
    exec_spy_core = create_autospec(orig_op_execute, side_effect=lambda sql, *a, **kw: executed_core.append(str(sql)))
    monkeypatch.setattr(mod.op, "execute", exec_spy_core)
    monkeypatch.setenv("APP_ENV", "core")
    mod.upgrade()

    core_do_block = "\n".join(executed_core)
    assert "CREATE TABLE IF NOT EXISTS dalton_embedding_metadata" in core_do_block
    assert "ALTER TABLE dalton_conversation_cards ALTER COLUMN embedding TYPE vector(1536)" in core_do_block
    assert "USING hnsw ((embedding::halfvec(1536)) halfvec_cosine_ops)" in core_do_block
    # Empty Core branch alters column/index without inserting a lock row; only existing embedded rows seed 3072.
    assert "ELSE" in core_do_block
    else_section = core_do_block.split("ELSE", 1)[1]
    assert "INSERT INTO dalton_embedding_metadata" not in else_section
    assert "vector(1536)" in else_section

    # Everflow seeds 3072-d metadata directly and does not alter the column.
    executed_ef: list[str] = []
    exec_spy_ef = create_autospec(orig_op_execute, side_effect=lambda sql, *a, **kw: executed_ef.append(str(sql)))
    monkeypatch.setattr(mod.op, "execute", exec_spy_ef)
    monkeypatch.setenv("APP_ENV", "dalton")
    mod.upgrade()
    ef_sql = "\n".join(executed_ef)
    assert "INSERT INTO dalton_embedding_metadata" in ef_sql
    assert "3072" in ef_sql
    assert "ALTER TABLE dalton_conversation_cards" not in ef_sql


@pytest.mark.asyncio
async def test_embed_texts_and_insert_cards_batch_ensure_schema_on_real_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_b1_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "core")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    meta_1536 = EmbeddingMetadata(
        scope="conversation_archive",
        provider="openai",
        model_label="text-embedding-3-small",
        api_model_name="text-embedding-3-small",
        dimensions=1536,
    )
    ensure_spy = create_autospec(
        conv_embedder.ensure_conversation_embedding_schema,
        return_value=meta_1536,
    )
    monkeypatch.setattr(conv_embedder, "ensure_conversation_embedding_schema", ensure_spy)
    monkeypatch.setattr(
        conv_embedder,
        "get_embedding_metadata",
        create_autospec(conv_embedder.get_embedding_metadata, return_value=None),
    )
    monkeypatch.setattr(
        conv_embedder,
        "embed_texts_direct",
        create_autospec(
            conv_embedder.embed_texts_direct,
            return_value=([[0.1] * 1536], "text-embedding-3-small"),
        ),
    )

    vecs, label = await conv_embedder.embed_texts(["core doc"])
    assert label == "text-embedding-3-small"
    assert len(vecs) == 1 and len(vecs[0]) == 1536
    assert ensure_spy.call_count == 1
    assert ensure_spy.call_args.args[0].dimensions == 1536


@pytest.mark.asyncio
async def test_conversation_search_and_hybrid_search_reject_same_dim_different_model_without_conn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_b1_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "dalton")

    locked_gemini = EmbeddingMetadata(
        scope="conversation_archive",
        provider="gemini",
        model_label="gemini-embedding-2-preview",
        api_model_name="gemini-embedding-2",
        dimensions=3072,
    )
    get_meta_spy = create_autospec(
        conv_embedder.get_embedding_metadata,
        return_value=locked_gemini,
    )
    monkeypatch.setattr(conv_embedder, "get_embedding_metadata", get_meta_spy)

    direct_q_spy = create_autospec(
        conv_embedder.embed_query_direct,
        return_value=[0.01] * 3072,
    )
    monkeypatch.setattr(conv_embedder, "embed_query_direct", direct_q_spy)

    dense_db_spy = create_autospec(db_conv.search_cards_dense, return_value=[])
    kw_db_spy = create_autospec(db_conv.search_cards_keyword, return_value=[])
    monkeypatch.setattr(db_conv, "search_cards_dense", dense_db_spy)
    monkeypatch.setattr(db_conv, "search_cards_keyword", kw_db_spy)
    monkeypatch.setattr(
        conv_tools_mod,
        "get_user_tier",
        create_autospec(conv_tools_mod.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        conv_tools_mod,
        "resolve_turn_team_memory_enabled",
        create_autospec(conv_tools_mod.resolve_turn_team_memory_enabled, return_value=True),
    )

    # 1) Same 3072 dimension but switched Gemini model (`gemini-embedding-001`) -> refused via conversation_search (conn=None).
    monkeypatch.setenv("DALTON_EMBEDDING_PROVIDER", "gemini")
    monkeypatch.setenv("DALTON_EMBEDDING_MODEL", "gemini-embedding-001")
    monkeypatch.setenv("DALTON_EMBEDDING_DIMENSIONS", "3072")

    conv_tool = conv_tools_mod.create_conversation_tools(role="analyst")[0]
    with pytest.raises(EmbeddingMetadataMismatchError, match="gemini-embedding-001"):
        await conv_tool.ainvoke(
            {"query": "NVDA Blackwell margin", "limit": 5},
            config={"configurable": {"user_id": "internal-1", "thread_id": "t-1"}},
        )
    assert get_meta_spy.call_count == 1
    assert direct_q_spy.call_count == 0
    assert dense_db_spy.call_count == 0

    # 2) Same 3072 dimension but switched provider (`openai` / `text-embedding-3-large`) -> refused via hybrid_search_conversations(conn=None).
    monkeypatch.setenv("DALTON_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("DALTON_EMBEDDING_MODEL", "text-embedding-3-large")
    monkeypatch.setenv("DALTON_EMBEDDING_DIMENSIONS", "3072")
    with pytest.raises(EmbeddingMetadataMismatchError, match="openai"):
        await conv_search.hybrid_search_conversations("NVDA Blackwell margin", conn=None)
    assert get_meta_spy.call_count == 2
    assert direct_q_spy.call_count == 0
    assert dense_db_spy.call_count == 0


@pytest.mark.asyncio
async def test_gemini_direct_passes_sdk_http_timeout_and_enforces_bounded_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_b1_env(monkeypatch)
    from google import genai
    from google.genai import types as genai_types

    # 1) Real google.genai.Client receives positive millisecond HttpOptions.timeout and propagates it to httpx.
    client_real = emb_client._create_gemini_client("gk-test", timeout_s=12.5)
    assert isinstance(client_real, genai.Client)
    api_http_opts = getattr(client_real._api_client, "_http_options", None)
    assert api_http_opts is not None
    assert api_http_opts.timeout == 12500

    # Propagate the exact HttpOptions constructed by _create_gemini_client onto an in-memory MockTransport.
    seen_timeouts: list[Any] = []
    seen_http_options: list[Any] = []
    orig_genai_client_cls = genai.Client

    def _mock_handler(request: httpx.Request) -> httpx.Response:
        seen_timeouts.append(request.extensions.get("timeout"))
        return httpx.Response(
            200,
            json={"embeddings": [{"values": [0.1, 0.2, 0.3, 0.4]}]},
            request=request,
        )

    def _genai_client_with_mock_transport(*, api_key: str | None = None, http_options: Any = None, **kwargs: Any) -> Any:
        seen_http_options.append(http_options)
        wrapped_opts = genai_types.HttpOptions(
            timeout=getattr(http_options, "timeout", None),
            client_args={"transport": httpx.MockTransport(_mock_handler)},
        )
        return orig_genai_client_cls(api_key=api_key, http_options=wrapped_opts, **kwargs)

    monkeypatch.setattr(genai, "Client", _genai_client_with_mock_transport)

    cfg = EmbeddingConfig(
        provider="gemini",
        model_label="gemini-embedding-2-preview",
        api_model_name="gemini-embedding-2",
        dimensions=4,
    )
    vecs, _ = await embed_texts_direct(["timeout check"], config=cfg, api_key="gk-test", timeout_s=12.5)
    assert vecs == [[0.1, 0.2, 0.3, 0.4]]
    assert len(seen_http_options) == 1
    assert seen_http_options[0].timeout == 12500
    assert seen_timeouts and seen_timeouts[0] is not None
    assert seen_timeouts[0]["connect"] == 12.5
    assert seen_timeouts[0]["read"] == 12.5

    # 2) Hung Gemini call triggers TimeoutError on embed_query_direct and falls back cleanly in embed_query.
    def _slow_gemini(*args: Any, **kwargs: Any) -> list[list[float]]:
        time.sleep(0.25)
        return [[0.1, 0.2, 0.3, 0.4]]

    orig_call_gemini_sync = emb_client._call_gemini_embed_sync
    monkeypatch.setattr(
        emb_client,
        "_call_gemini_embed_sync",
        create_autospec(orig_call_gemini_sync, side_effect=_slow_gemini),
    )
    with pytest.raises(TimeoutError):
        await embed_query_direct("hang", config=cfg, api_key="gk-test", timeout_s=0.05, retry=False)

    monkeypatch.setenv("APP_ENV", "dalton")
    monkeypatch.setenv("GEMINI_API_KEY", "gk-test")
    monkeypatch.setattr(
        conv_embedder,
        "get_embedding_metadata",
        create_autospec(conv_embedder.get_embedding_metadata, return_value=None),
    )
    assert await conv_embedder.embed_query("hang", timeout_s=0.05) is None

    # 3) Bounded concurrency (default 2) prevents >2 simultaneous Gemini calls in flight.
    in_flight = 0
    max_in_flight = 0
    import threading

    lock = threading.Lock()

    def _tracked_gemini(*args: Any, **kwargs: Any) -> list[list[float]]:
        nonlocal in_flight, max_in_flight
        with lock:
            in_flight += 1
            if in_flight > max_in_flight:
                max_in_flight = in_flight
        try:
            time.sleep(0.08)
            return [[0.1, 0.2, 0.3, 0.4]]
        finally:
            with lock:
                in_flight -= 1

    monkeypatch.setattr(
        emb_client,
        "_call_gemini_embed_sync",
        create_autospec(orig_call_gemini_sync, side_effect=_tracked_gemini),
    )
    assert DEFAULT_MAX_DIRECT_CONCURRENCY == 2
    results = await asyncio.gather(
        *(
            embed_texts_direct([f"item-{i}"], config=cfg, api_key="gk-test", timeout_s=5.0)
            for i in range(4)
        )
    )
    assert len(results) == 4
    assert max_in_flight <= 2
    assert max_in_flight >= 1


# ---------------------------------------------------------------------------
# 4. /jobs/ops status fileization & prompt gate (`ops_jobs_enabled`)
# ---------------------------------------------------------------------------


def test_export_ops_status_and_launchd_plist_template(tmp_path: Path) -> None:
    exporter = _load_export_ops_status_module()

    # Validate plist template parses cleanly after placeholder substitution.
    raw_plist = OPS_PLIST_TEMPLATE.read_text(encoding="utf-8")
    rendered_plist = (
        raw_plist.replace("__PYTHON_BIN__", sys.executable)
        .replace("__REPO__", str(tmp_path))
        .replace("__HOME__", str(tmp_path))
        .replace("__LOG_DIR__", str(tmp_path / "logs"))
    )
    parsed_plist = plistlib.loads(rendered_plist.encode("utf-8"))
    assert parsed_plist["Label"] == "com.dalton.ops-status-exporter"
    assert parsed_plist["StartInterval"] == 60
    assert parsed_plist["RunAtLoad"] is True

    # Build fake launchctl, backup log, backup plist, watchdog plist & failures file.
    stub_launchctl = tmp_path / "launchctl"
    stub_launchctl.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"com.dalton.pg-backup"* ]]; then exit 0; fi\n'
        'if [[ "$*" == *"com.dalton.backend-watchdog"* ]]; then exit 0; fi\n'
        "exit 1\n",
        encoding="utf-8",
    )
    stub_launchctl.chmod(0o755)

    pg_log = tmp_path / "dalton-pg-backup.log"
    pg_log.write_text("2026-11-25 03:00:00 ok  14G /Users/everflow/Backups/pg.dump\n", encoding="utf-8")

    pg_plist = tmp_path / "com.dalton.pg-backup.plist"
    pg_plist.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.dalton.pg-backup",
                "StartCalendarInterval": {"Hour": 3, "Minute": 0},
            }
        )
    )
    wd_plist = tmp_path / "com.dalton.backend-watchdog.plist"
    wd_plist.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.dalton.backend-watchdog",
                "StartInterval": 60,
            }
        )
    )
    wd_failures = tmp_path / ".watchdog_failures"
    wd_failures.write_text("0\n", encoding="utf-8")

    out_path = tmp_path / "var" / "dalton" / "ops_jobs_status.json"
    fixed_now = datetime(2026, 11, 25, 11, 0, 0, tzinfo=UTC)
    payload = exporter.export_once(
        output_path=out_path,
        pg_backup_log_file=pg_log,
        pg_backup_plist_file=pg_plist,
        backend_watchdog_plist_file=wd_plist,
        watchdog_failures_file=wd_failures,
        ttl_seconds=300,
        now=fixed_now,
        launchctl_bin=str(stub_launchctl),
    )

    assert out_path.is_file()
    disk_data = json.loads(out_path.read_text(encoding="utf-8"))
    assert disk_data == payload
    assert disk_data["schema_version"] == 1
    assert disk_data["generated_at"] == fixed_now.isoformat()
    assert disk_data["ttl_seconds"] == 300
    assert disk_data["exporter"] == "com.dalton.ops-status-exporter"
    by_key = {j["key"]: j for j in disk_data["jobs"]}
    assert by_key["ops-db-backup"]["status"] == "on"
    assert by_key["ops-db-backup"]["last_result"] == "成功（14G）"
    assert "/Users/" not in (by_key["ops-db-backup"]["last_result"] or "")
    assert by_key["ops-backend-watchdog"]["status"] == "on"
    assert by_key["ops-backend-watchdog"]["last_result"] == "健康"


@pytest.mark.asyncio
async def test_system_jobs_ops_status_file_fresh_stale_core_and_via_bridge_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_b1_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "dalton")

    status_file = tmp_path / "ops_jobs_status.json"
    monkeypatch.setenv("DALTON_OPS_STATUS_FILE", str(status_file))

    # Block any outbound HTTP call to prove file mode never touches bridge.
    orig_call_bridge = system_jobs_mod._call_host_bridge

    def _forbidden_http(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("File-based ops status reader must not call host bridge")

    monkeypatch.setattr(
        system_jobs_mod,
        "_call_host_bridge",
        create_autospec(orig_call_bridge, side_effect=_forbidden_http),
    )

    # 1) Fresh status file succeeds even when RESEARCHDATA_BRIDGE capability is disabled!
    monkeypatch.setenv("DALTON_CAP_RESEARCHDATA_BRIDGE", "false")
    fresh_payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "host_platform": "macos",
        "exporter": "com.dalton.ops-status-exporter",
        "ttl_seconds": 300,
        "jobs": [
            {
                "key": "ops-db-backup",
                "kind": "ops_db_backup",
                "status": "on",
                "last_run_at": "2026-11-25T03:00:00+08:00",
                "last_result": "成功（14G）",
                "next_run_at": "2026-11-26T03:00:00+08:00",
            },
            {
                "key": "ops-backend-watchdog",
                "kind": "ops_backend_watchdog",
                "status": "on",
                "last_run_at": "2026-11-25T10:59:00+00:00",
                "last_result": "健康",
                "next_run_at": "2026-11-25T11:00:00+00:00",
            },
        ],
    }
    status_file.write_text(json.dumps(fresh_payload, ensure_ascii=False), encoding="utf-8")

    fresh_jobs = await system_jobs_mod._fetch_ops_jobs()
    by_key = {j["key"]: j for j in fresh_jobs}
    assert by_key["ops-db-backup"]["status"] == "on"
    assert by_key["ops-db-backup"]["last_result"] == "成功（14G）"
    assert by_key["ops-backend-watchdog"]["status"] == "on"

    # 2) Stale status file (> 300s) is rejected and returns status='unknown' (never serves stale 'on').
    stale_ts = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
    stale_payload = {**fresh_payload, "generated_at": stale_ts}
    status_file.write_text(json.dumps(stale_payload, ensure_ascii=False), encoding="utf-8")

    stale_jobs = await system_jobs_mod._fetch_ops_jobs()
    for item in stale_jobs:
        assert item["status"] == "unknown"
        assert item["last_result"] == "状态文件已过期"

    # 3) Standalone Core always returns [] without touching file or bridge.
    monkeypatch.setenv("APP_ENV", "core")
    assert await system_jobs_mod._fetch_ops_jobs() == []

    # 4) Emergency rollback (`DALTON_OPS_JOBS_VIA_BRIDGE=true`) routes back to host bridge.
    monkeypatch.setenv("APP_ENV", "dalton")
    monkeypatch.delenv("DALTON_CAP_RESEARCHDATA_BRIDGE", raising=False)
    monkeypatch.setenv("DALTON_OPS_JOBS_VIA_BRIDGE", "true")
    bridge_spy = create_autospec(
        orig_call_bridge,
        return_value=(
            200,
            {
                "ok": True,
                "jobs": [
                    {
                        "key": "ops-db-backup",
                        "kind": "ops_db_backup",
                        "status": "on",
                        "last_result": "bridge-rollback-ok",
                    }
                ],
            },
        ),
    )
    monkeypatch.setattr(system_jobs_mod, "_call_host_bridge", bridge_spy)
    rb_jobs = await system_jobs_mod._fetch_ops_jobs()
    assert bridge_spy.call_count == 1
    assert next(j for j in rb_jobs if j["key"] == "ops-db-backup")["last_result"] == "bridge-rollback-ok"


@pytest.mark.asyncio
async def test_system_jobs_ops_status_rejects_malformed_job_entry_and_overflow_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_b1_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "dalton")
    status_file = tmp_path / "ops_jobs_status.json"
    monkeypatch.setenv("DALTON_OPS_STATUS_FILE", str(status_file))

    # 1) Non-string `last_result` (JSON object) invalidates the entire status file -> default unknown skeleton.
    bad_job_payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "ttl_seconds": 300,
        "jobs": [
            {
                "key": "ops-db-backup",
                "kind": "ops_db_backup",
                "status": "on",
                "last_result": {"unexpected": "dict"},
            },
            {
                "key": "ops-backend-watchdog",
                "kind": "ops_backend_watchdog",
                "status": "on",
                "last_result": "健康",
            },
        ],
    }
    status_file.write_text(json.dumps(bad_job_payload, ensure_ascii=False), encoding="utf-8")
    loaded, reason = system_jobs_mod._load_ops_jobs_from_status_file(status_file)
    assert loaded is None
    assert reason == "invalid_job_entry"
    jobs = await system_jobs_mod._fetch_ops_jobs()
    assert all(j["status"] == "unknown" and j["last_result"] is None for j in jobs)

    # 2) Extreme timestamps whose UTC conversion overflows are rejected safely without raising OverflowError.
    for extreme_ts in ("0001-01-01T00:00:00+10:00", "9999-12-31T23:59:59-10:00"):
        overflow_payload = {
            "schema_version": 1,
            "generated_at": extreme_ts,
            "ttl_seconds": 300,
            "jobs": [
                {
                    "key": "ops-db-backup",
                    "kind": "ops_db_backup",
                    "status": "on",
                    "last_result": "ok",
                }
            ],
        }
        status_file.write_text(json.dumps(overflow_payload, ensure_ascii=False), encoding="utf-8")
        loaded_ov, reason_ov = system_jobs_mod._load_ops_jobs_from_status_file(status_file)
        assert loaded_ov is None
        assert reason_ov == "invalid_generated_at"
        ov_jobs = await system_jobs_mod._fetch_ops_jobs()
        assert all(j["status"] == "unknown" and j["last_result"] is None for j in ov_jobs)


def test_ops_jobs_enabled_prompt_gate_and_dalton_hook_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_b1_env(monkeypatch)
    cos_tpl = (
        REPO_ROOT
        / "src"
        / "ptc_agent"
        / "agent"
        / "prompts"
        / "templates"
        / "components"
        / "chief_of_staff.md.j2"
    ).read_text(encoding="utf-8")
    up_tpl = (
        REPO_ROOT
        / "src"
        / "ptc_agent"
        / "agent"
        / "prompts"
        / "templates"
        / "components"
        / "user_profile.md.j2"
    ).read_text(encoding="utf-8")

    # Both upstream templates carry DALTON-HOOK(standalone-core) and gate on ops_jobs_enabled.
    for text in (cos_tpl, up_tpl):
        assert "DALTON-HOOK(standalone-core)" in text
        assert "ops_jobs_enabled" in text
        assert "researchdata_bridge_enabled | default(false) %}，运维任务只读" not in text

    # Everflow with RESEARCHDATA_BRIDGE disabled still renders 运维任务只读 (decoupled from bridge!).
    monkeypatch.setenv("APP_ENV", "dalton")
    monkeypatch.setenv("DALTON_CAP_RESEARCHDATA_BRIDGE", "false")
    loader = prompt_loader_mod.PromptLoader()
    cos_ef = loader.render("components/chief_of_staff.md.j2")
    assert "运维任务只读" in cos_ef
    up_ef = loader.render(
        "components/user_profile.md.j2",
        user_profile={"name": "Analyst"},
        preferences=answer_preferences(None, steering=True),
        investment_lines=[],
        user_data_counts=None,
        main_agent_steering=True,
        channels_enabled=False,
        wakeups_enabled=True,
    )
    assert "运维任务只读" in up_ef

    # Core omits 运维任务只读.
    monkeypatch.setenv("APP_ENV", "core")
    cos_core = loader.render("components/chief_of_staff.md.j2")
    assert "运维任务只读" not in cos_core
    up_core = loader.render(
        "components/user_profile.md.j2",
        user_profile={"name": "Analyst"},
        preferences=answer_preferences(None, steering=True),
        investment_lines=[],
        user_data_counts=None,
        main_agent_steering=True,
        channels_enabled=False,
        wakeups_enabled=True,
    )
    assert "运维任务只读" not in up_core


@pytest.mark.asyncio
async def test_gemini_timeout_retains_semaphore_until_worker_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    _clear_b1_env(monkeypatch)
    monkeypatch.setenv("DALTON_EMBED_MAX_CONCURRENCY", "2")

    cfg = EmbeddingConfig(
        provider="gemini",
        model_label="gemini-embedding-2-preview",
        api_model_name="gemini-embedding-2",
        dimensions=4,
    )

    gate = threading.Event()
    started = 0
    in_flight = 0
    max_in_flight = 0
    lock = threading.Lock()
    two_running = threading.Event()

    orig_sync = emb_client._call_gemini_embed_sync

    def _blocking_gemini(*args: Any, **kwargs: Any) -> list[list[float]]:
        nonlocal started, in_flight, max_in_flight
        with lock:
            started += 1
            in_flight += 1
            if in_flight > max_in_flight:
                max_in_flight = in_flight
            if in_flight == 2:
                two_running.set()
        gate.wait(timeout=3.0)
        try:
            return [[0.1, 0.2, 0.3, 0.4]]
        finally:
            with lock:
                in_flight -= 1

    monkeypatch.setattr(
        emb_client,
        "_call_gemini_embed_sync",
        create_autospec(orig_sync, side_effect=_blocking_gemini),
    )

    # First two calls fill concurrency=2 and time out while threads are still blocked on gate.
    t1 = asyncio.create_task(
        embed_texts_direct(["a"], config=cfg, api_key="gk-test", timeout_s=0.05, retry=False)
    )
    t2 = asyncio.create_task(
        embed_texts_direct(["b"], config=cfg, api_key="gk-test", timeout_s=0.05, retry=False)
    )
    assert await asyncio.to_thread(two_running.wait, 2.0)

    r1, r2 = await asyncio.gather(t1, t2, return_exceptions=True)
    assert isinstance(r1, TimeoutError)
    assert isinstance(r2, TimeoutError)

    # Launch a 3rd call while the timed-out worker threads are still blocked:
    # it must wait on the semaphore instead of raising in-flight thread count to 3.
    t3 = asyncio.create_task(
        embed_texts_direct(["c"], config=cfg, api_key="gk-test", timeout_s=2.0, retry=False)
    )
    await asyncio.sleep(0.06)
    with lock:
        assert started == 2
        assert max_in_flight == 2
    assert not t3.done()

    # Unblock worker threads: stalled slots release on completion and t3 proceeds cleanly.
    gate.set()
    vecs, label = await t3
    assert label == "gemini-embedding-2-preview"
    assert vecs == [[0.1, 0.2, 0.3, 0.4]]
    with lock:
        assert started == 3
        assert max_in_flight == 2
        assert in_flight == 0


@pytest.mark.asyncio
async def test_configured_and_invalid_gemini_task_types_enforced_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_b1_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "dalton")
    monkeypatch.setenv("GEMINI_API_KEY", "gk-test")

    # 1) Invalid Gemini task type is rejected before config / locking / upstream call.
    monkeypatch.setenv("DALTON_EMBEDDING_TASK_TYPE_QUERY", "INVALID_TASK_TYPE")
    with pytest.raises(EmbeddingConfigError, match="DALTON_EMBEDDING_TASK_TYPE_QUERY"):
        resolve_embedding_config()

    monkeypatch.delenv("DALTON_EMBEDDING_TASK_TYPE_QUERY")
    with pytest.raises(EmbeddingConfigError, match="task_type_doc"):
        await upsert_embedding_metadata(
            EmbeddingMetadata(
                scope="conversation_archive",
                provider="gemini",
                model_label="gemini-embedding-2-preview",
                api_model_name="gemini-embedding-2",
                dimensions=3072,
                task_type_doc="BOGUS_TASK",
            )
        )

    # 2) Valid SEMANTIC_SIMILARITY doc/query config matching locked metadata is sent verbatim
    # (neither coerced to RETRIEVAL_DOCUMENT by client.py nor overwritten by embed_texts default).
    monkeypatch.setenv("DALTON_EMBEDDING_TASK_TYPE_DOC", "SEMANTIC_SIMILARITY")
    monkeypatch.setenv("DALTON_EMBEDDING_TASK_TYPE_QUERY", "SEMANTIC_SIMILARITY")
    cfg = resolve_embedding_config()
    assert cfg.task_type_doc == "SEMANTIC_SIMILARITY"
    assert cfg.task_type_query == "SEMANTIC_SIMILARITY"

    locked_sem = EmbeddingMetadata.from_config(cfg)
    monkeypatch.setattr(
        conv_embedder,
        "get_embedding_metadata",
        create_autospec(conv_embedder.get_embedding_metadata, return_value=locked_sem),
    )
    monkeypatch.setattr(
        conv_embedder,
        "ensure_conversation_embedding_schema",
        create_autospec(conv_embedder.ensure_conversation_embedding_schema, return_value=locked_sem),
    )

    sent_task_types: list[str] = []

    class _FakeModels:
        def embed_content(self, *, model: str, contents: Any, config: dict[str, Any]) -> Any:
            sent_task_types.append(str(config.get("task_type")))
            return SimpleNamespace(embeddings=[SimpleNamespace(values=[0.5] * 3072)])

    fake_client = SimpleNamespace(models=_FakeModels())
    monkeypatch.setattr(
        emb_client,
        "_create_gemini_client",
        create_autospec(emb_client._create_gemini_client, return_value=fake_client),
    )

    vecs, _ = await conv_embedder.embed_texts(["archive note"])
    assert len(vecs) == 1
    q_vec = await conv_embedder.embed_query("search query")
    assert q_vec is not None and len(q_vec) == 3072
    assert sent_task_types == ["SEMANTIC_SIMILARITY", "SEMANTIC_SIMILARITY"]

    # 3) Explicit runtime task_type override diverging from locked task_type_doc is rejected before sending.
    with pytest.raises(EmbeddingMetadataMismatchError, match="task_type_doc"):
        await conv_embedder.embed_texts(["archive note"], task_type="RETRIEVAL_DOCUMENT")
    assert len(sent_task_types) == 2

    # 4) Direct client also rejects unsupported task types instead of silent fallback.
    with pytest.raises(EmbeddingConfigError, match="Unsupported Gemini embedding task_type"):
        await embed_texts_direct(["hello"], task_type="UNSUPPORTED_TYPE", config=cfg, api_key="gk-test")


def test_d052_downgrade_requires_legacy_gemini_model_and_task_types_when_vectors_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_b1_env(monkeypatch)
    mod = importlib.import_module(
        "src.dalton.migrations.versions.d052_embedding_metadata_and_dimension"
    )

    executed_sql: list[str] = []
    exec_spy = create_autospec(
        mod.op.execute,
        side_effect=lambda sql, *a, **kw: executed_sql.append(str(sql)),
    )
    monkeypatch.setattr(mod.op, "execute", exec_spy)
    mod.downgrade()

    full_sql = "\n".join(executed_sql)
    # Downgrade reads provider, model_label, api_model_name, dimensions, and task types.
    for token in (
        "provider",
        "model_label",
        "api_model_name",
        "dimensions",
        "task_type_doc",
        "task_type_query",
        "has_locked_row := FOUND",
        "NOT has_locked_row",
        "locked_dims IS DISTINCT FROM 3072",
        "locked_provider IS DISTINCT FROM 'gemini'",
        "locked_model_label NOT IN ('gemini-embedding-2-preview', 'gemini-embedding-2')",
        "locked_api_model IS DISTINCT FROM 'gemini-embedding-2'",
        "locked_task_doc IS DISTINCT FROM 'RETRIEVAL_DOCUMENT'",
        "locked_task_query IS DISTINCT FROM 'RETRIEVAL_QUERY'",
        "Refusing in-place downgrade of d052",
    ):
        assert token in full_sql, f"missing expected downgrade guard token: {token}"

    # Guard DO block executes before dropping dalton_embedding_metadata.
    guard_idx = next(i for i, s in enumerate(executed_sql) if "RAISE EXCEPTION" in s)
    drop_idx = next(
        i for i, s in enumerate(executed_sql) if "DROP TABLE IF EXISTS dalton_embedding_metadata" in s
    )
    assert guard_idx < drop_idx


@pytest.mark.asyncio
async def test_interactive_query_times_out_waiting_for_occupied_semaphore_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    _clear_b1_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "dalton")
    monkeypatch.setenv("GEMINI_API_KEY", "gk-test")
    monkeypatch.setenv("DALTON_EMBED_MAX_CONCURRENCY", "2")

    cfg = EmbeddingConfig(
        provider="gemini",
        model_label="gemini-embedding-2-preview",
        api_model_name="gemini-embedding-2",
        dimensions=4,
    )

    gate = threading.Event()
    started = 0
    finished = 0
    in_flight = 0
    max_in_flight = 0
    lock = threading.Lock()
    two_running = threading.Event()
    three_finished = threading.Event()

    orig_sync = emb_client._call_gemini_embed_sync

    def _blocking_gemini(*args: Any, **kwargs: Any) -> list[list[float]]:
        nonlocal started, finished, in_flight, max_in_flight
        with lock:
            started += 1
            in_flight += 1
            if in_flight > max_in_flight:
                max_in_flight = in_flight
            if in_flight == 2:
                two_running.set()
        gate.wait(timeout=3.0)
        try:
            return [[0.1, 0.2, 0.3, 0.4]]
        finally:
            with lock:
                in_flight -= 1
                finished += 1
                if finished == 3:
                    three_finished.set()

    monkeypatch.setattr(
        emb_client,
        "_call_gemini_embed_sync",
        create_autospec(orig_sync, side_effect=_blocking_gemini),
    )
    monkeypatch.setattr(
        conv_embedder,
        "get_embedding_metadata",
        create_autospec(conv_embedder.get_embedding_metadata, return_value=None),
    )
    monkeypatch.setattr(
        conv_embedder,
        "resolve_embedding_config",
        create_autospec(conv_embedder.resolve_embedding_config, return_value=cfg),
    )

    # Occupy both concurrency slots with two timed-out calls whose worker threads keep running.
    t1 = asyncio.create_task(
        embed_texts_direct(["a"], config=cfg, api_key="gk-test", timeout_s=0.05, retry=False)
    )
    t2 = asyncio.create_task(
        embed_texts_direct(["b"], config=cfg, api_key="gk-test", timeout_s=0.05, retry=False)
    )
    assert await asyncio.to_thread(two_running.wait, 2.0)
    r1, r2 = await asyncio.gather(t1, t2, return_exceptions=True)
    assert isinstance(r1, TimeoutError)
    assert isinstance(r2, TimeoutError)

    # 3rd interactive query abandons the semaphore queue within timeout_s and falls back to keyword arm (None).
    t0 = time.monotonic()
    fallback_vec = await conv_embedder.embed_query("queued query", timeout_s=0.08)
    elapsed = time.monotonic() - t0
    assert fallback_vec is None
    assert elapsed < 0.5
    with lock:
        assert started == 2
        assert max_in_flight == 2

    # Direct query helper also raises TimeoutError within its queue+execution budget.
    t0_direct = time.monotonic()
    with pytest.raises(TimeoutError):
        await embed_query_direct("queued direct", config=cfg, api_key="gk-test", timeout_s=0.06, retry=False)
    assert time.monotonic() - t0_direct < 0.5
    with lock:
        assert started == 2
        assert max_in_flight == 2

    # Unblocking the timed-out worker threads releases their slots cleanly for subsequent queries.
    gate.set()
    vec = await embed_query_direct("after release", config=cfg, api_key="gk-test", timeout_s=2.0, retry=False)
    assert vec == [0.1, 0.2, 0.3, 0.4]
    assert await asyncio.to_thread(three_finished.wait, 2.0)
    with lock:
        assert started == 3
        assert max_in_flight == 2
        assert in_flight == 0
