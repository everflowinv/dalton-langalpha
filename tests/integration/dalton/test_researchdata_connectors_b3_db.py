"""Postgres integration tests for Research Data Bridge Split — Batch 3 (`tests/integration/dalton/`).

Runs against the isolated `_test` Postgres database (migrated to Dalton Alembic head by
`tests/integration/dalton/conftest.py`). Exercises:
1. End-to-end `dalton_wiki_ingests` ledger idempotency paired with in-process `research-host`
   `/ingest/wiki` (via `httpx.ASGITransport`, zero external network calls).
2. Capability-scoped `dalton_data_map_scoped_cache` isolation in Postgres across Core default,
   Core + `company_wiki` (without `researchdata_bridge`), and Everflow profiles.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import date
from pathlib import Path

import httpx
import pytest

from src.dalton.capabilities import (
    CapabilityId,
    capabilities_fingerprint,
    effective_capabilities,
)
from src.dalton.data_map import builder as dm_builder
from src.dalton.database.data_map import get_cached_data_map, upsert_cached_data_map
from src.dalton.database.wiki_ingests import (
    create_pending_wiki_ingest,
    list_wiki_ingests_by_user,
    update_wiki_ingest_status,
)
from src.dalton.profile import DeploymentProfile
from src.dalton.research_host.app import ResearchHostConfig, create_app

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_wiki_ingest_ledger_idempotency_with_research_host_db(
    patched_get_db_connection,
    cleanup_tables,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Postgres `dalton_wiki_ingests` + in-process `research-host` `/ingest/wiki` idempotent replay."""
    user_id = f"b3-user-{uuid.uuid4().hex[:8]}"
    request_id = str(uuid.uuid4())
    content = "TSMC CoWoS-L allocation expanded for 2026H2."
    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()

    monkeypatch.setenv("APP_ENV", "core")
    monkeypatch.setenv("DALTON_PROFILE", "core")
    monkeypatch.setenv("DALTON_ENABLED", "true")
    monkeypatch.setenv("DALTON_CAP_COMPANY_WIKI", "true")

    def _core_wiki_caps() -> frozenset[CapabilityId]:
        return frozenset({CapabilityId.COMPANY_WIKI})

    app = create_app(
        ResearchHostConfig(
            data_root=tmp_path / "wiki",
            mcp_token="b3-int-host-token",
            admin_token="b3-int-admin-token",
            profile=DeploymentProfile.CORE,
        ),
        capabilities_provider=_core_wiki_caps,
    )
    transport = httpx.ASGITransport(app=app)

    # 1. Insert pending audit row in Postgres
    pending = await create_pending_wiki_ingest(
        request_id=request_id,
        user_id=user_id,
        workspace_id="ws-b3-1",
        thread_id="th-b3-1",
        target_type="company",
        target="TSM",
        content_type="研究笔记",
        doc_date="2026-08-18",
        sha256=sha,
    )
    assert pending["status"] == "pending"

    async with httpx.AsyncClient(transport=transport, base_url="http://research-host:8951") as client:
        ingest_payload = {
            "request_id": request_id,
            "target_type": "company",
            "target": "TSM",
            "content_type": "研究笔记",
            "date": "2026-08-18",
            "content": content,
        }
        first = await client.post(
            "/ingest/wiki",
            headers={"Authorization": "Bearer b3-int-admin-token"},
            json=ingest_payload,
        )
        assert first.status_code == 200
        first_body = first.json()
        assert first_body["ok"] is True
        doc_id = first_body["doc_id"]

        # 2. Mark done in Postgres
        done_row = await update_wiki_ingest_status(
            request_id,
            status="done",
            doc_id=doc_id,
            filepath=first_body.get("filepath"),
        )
        assert done_row["status"] == "done"
        assert done_row["doc_id"] == doc_id

        # 3. Replay identical request_id against both Postgres and research-host
        replayed_pg = await create_pending_wiki_ingest(
            request_id=request_id,
            user_id=user_id,
            workspace_id="ws-b3-1",
            thread_id="th-b3-1",
            target_type="company",
            target="TSM",
            content_type="研究笔记",
            doc_date="2026-08-18",
            sha256=sha,
        )
        assert replayed_pg["status"] == "done"

        second = await client.post(
            "/ingest/wiki",
            headers={"Authorization": "Bearer b3-int-admin-token"},
            json=ingest_payload,
        )
        assert second.status_code == 200
        second_body = second.json()
        assert second_body["idempotent_replay"] is True
        assert second_body["doc_id"] == doc_id

    rows = await list_wiki_ingests_by_user(user_id)
    assert len(rows) == 1
    assert rows[0]["ingest_id"] == request_id
    assert rows[0]["status"] == "done"


async def test_scoped_data_map_cache_isolates_core_wiki_from_everflow_db(
    patched_get_db_connection,
    cleanup_tables,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Postgres `dalton_data_map_scoped_cache` isolates Core default, Core+company_wiki, and Everflow."""
    cache_day = date(2026, 8, 20)

    for key in list(os.environ):
        if key.startswith("DALTON_CAP_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DALTON_ENABLED", "true")

    # 1. Core default (includes open-source company_wiki; researchdata_bridge remains OFF)
    monkeypatch.setenv("APP_ENV", "core")
    monkeypatch.setenv("DALTON_PROFILE", "core")
    monkeypatch.setenv("DALTON_HOST_PLATFORM", "linux")
    caps_core_default = effective_capabilities()
    assert CapabilityId.COMPANY_WIKI in caps_core_default
    assert CapabilityId.RESEARCHDATA_BRIDGE not in caps_core_default
    fp_core_default = capabilities_fingerprint(caps_core_default)
    md_core_default = dm_builder.render_static_data_map_markdown(team_memory_enabled=False)
    assert "company-wiki" in md_core_default

    # 2. Core + default-off capability (sales_notes) enabled (researchdata_bridge remains OFF)
    monkeypatch.setenv("DALTON_CAP_SALES_NOTES", "true")
    caps_core_sales = effective_capabilities()
    assert CapabilityId.SALES_NOTES in caps_core_sales
    assert CapabilityId.RESEARCHDATA_BRIDGE not in caps_core_sales
    fp_core_sales = capabilities_fingerprint(caps_core_sales)
    md_core_sales = dm_builder.render_static_data_map_markdown(team_memory_enabled=False)

    # 3. Everflow default
    monkeypatch.delenv("DALTON_CAP_SALES_NOTES", raising=False)
    monkeypatch.setenv("APP_ENV", "dalton")
    monkeypatch.setenv("DALTON_PROFILE", "everflow")
    monkeypatch.setenv("DALTON_HOST_PLATFORM", "macos")
    caps_ef = effective_capabilities()
    fp_ef = capabilities_fingerprint(caps_ef)
    md_ef = dm_builder.render_static_data_map_markdown(team_memory_enabled=True)

    assert len({fp_core_default, fp_core_sales, fp_ef}) == 3

    await upsert_cached_data_map(
        cache_day,
        md_core_default or "core-default-empty",
        {"profile": "core", "mode": "default"},
        capabilities_fingerprint=fp_core_default,
    )
    await upsert_cached_data_map(
        cache_day,
        md_core_sales,
        {"profile": "core", "mode": "sales_notes"},
        capabilities_fingerprint=fp_core_sales,
    )
    await upsert_cached_data_map(
        cache_day,
        md_ef,
        {"profile": "everflow", "mode": "full"},
        capabilities_fingerprint=fp_ef,
    )

    got_default = await get_cached_data_map(cache_day, capabilities_fingerprint=fp_core_default)
    got_sales = await get_cached_data_map(cache_day, capabilities_fingerprint=fp_core_sales)
    got_ef = await get_cached_data_map(cache_day, capabilities_fingerprint=fp_ef)

    assert got_default is not None and got_default["payload"]["mode"] == "default"
    assert "company-wiki" in got_default["rendered_markdown"]
    assert got_sales is not None and got_sales["payload"]["mode"] == "sales_notes"
    assert got_ef is not None and got_ef["payload"]["mode"] == "full"
