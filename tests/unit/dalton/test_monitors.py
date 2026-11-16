"""Unit tests for Dalton Monitors (Round 1: Platform — Checklist 1–7)."""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec, patch

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from psycopg import AsyncConnection, AsyncCursor

from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.dalton.monitors import (
    data_job_registry,
    db as monitor_db,
    grants as monitor_grants,
    permissions as monitor_perms,
    service as monitor_service,
)
from src.dalton.monitors.data_job_registry import (
    HostDirFileSource,
    MonitorDataJobBinding,
    build_live_demo_html,
    resolve_registered_job_dir,
)
from src.dalton.monitors.models import (
    RESERVED_MONITOR_SLUGS,
    MonitorDataEntry,
    MonitorRecord,
    PublishMonitorRequest,
    RepublishMonitorRequest,
    UpdateMonitorRequest,
    is_valid_slug,
    slugify_candidate,
)
from src.dalton.monitors.router import router as monitors_router
from src.server.database.artifacts import Artifact, Version, VersionFile
from src.server.database.artifacts.versions import PublishResult, StoredVersionFile
from src.server.services.artifacts.manifest import ManifestEntry
from src.server.services.artifacts.snapshot import Snapshot
from src.server.utils.api import get_current_user_id

NOW = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)
MONITOR_ID = "60000000-0000-4000-8000-000000000001"
ARTIFACT_ID = "61000000-0000-4000-8000-000000000001"
WS_ID = "62000000-0000-4000-8000-000000000001"
TEST_SIGNING_KEY = b"dalton-monitor-test-hmac-key-32b"


def _make_monitor(**overrides: Any) -> MonitorRecord:
    defaults: dict[str, Any] = dict(
        monitor_id=MONITOR_ID,
        slug="portfolio-beta",
        artifact_id=ARTIFACT_ID,
        owner_user_id="internal-owner",
        title="Portfolio Beta Monitor",
        category="portfolio",
        sort_order=0,
        visibility="company",
        published_seq=1,
        keep_updated=False,
        source_kind="workspace",
        source_ref=f"{WS_ID}:report.html",
        acl_revision=1,
        created_at=NOW,
        updated_at=NOW,
        artifact_code="moncode12345",
        workspace_id=WS_ID,
        entry_path="report.html",
        owner_name="Alice",
        owner_email="alice@company.com",
        data_as_of=NOW,
        member_user_ids=(),
    )
    defaults.update(overrides)
    return MonitorRecord(**defaults)


def _make_artifact(**overrides: Any) -> Artifact:
    defaults: dict[str, Any] = dict(
        artifact_id=ARTIFACT_ID,
        code="moncode12345",
        workspace_id=WS_ID,
        path="report.html",
        created_thread_id=None,
        bound_thread_id=None,
        keep_updated=False,
        latest_seq=1,
        shared_at=None,
        files=None,
        revision=0,
        created_at=NOW,
        shared_seq=0,
    )
    defaults.update(overrides)
    return Artifact(**defaults)


@pytest.fixture(autouse=True)
def _stub_monitor_grant_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(monitor_grants._MONITOR_CODEC, "_key", TEST_SIGNING_KEY)


def _build_monitor_app(user_id: str) -> FastAPI:
    app = FastAPI()
    app.include_router(monitors_router)
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    return app


# ---------------------------------------------------------------------------
# Checklist 1: External member (including invited dalton_members) gets 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checklist_1_external_member_including_invited_gets_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton import invites

    company_monitor = _make_monitor(visibility="company")
    external_uid = "ext-invited-user"

    # Even if invites.is_team_member returns True (dalton_members invited account),
    # monitor permissions MUST use access_tiers.get_user_tier -> "external".
    monkeypatch.setattr(
        invites,
        "is_team_member",
        create_autospec(invites.is_team_member, return_value=True),
    )
    monkeypatch.setattr(
        monitor_perms,
        "get_user_tier",
        create_autospec(monitor_perms.get_user_tier, return_value="external"),
    )
    monkeypatch.setattr(
        monitor_service,
        "get_user_tier",
        create_autospec(monitor_service.get_user_tier, return_value="external"),
    )
    monkeypatch.setattr(
        monitor_db,
        "list_all_monitors",
        create_autospec(monitor_db.list_all_monitors, return_value=[company_monitor]),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(monitor_db.get_monitor_by_slug, return_value=company_monitor),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_id",
        create_autospec(monitor_db.get_monitor_by_id, return_value=company_monitor),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_data",
        create_autospec(
            monitor_db.get_monitor_data,
            return_value=MonitorDataEntry(
                monitor_id=MONITOR_ID,
                data_key="metrics",
                payload={"v": 1},
                as_of=NOW,
                sha256="a" * 64,
                updated_at=NOW,
            ),
        ),
    )

    grant = await monitor_grants.mint_monitor_grant(
        MONITOR_ID, external_uid, company_monitor.acl_revision
    )

    app = _build_monitor_app(external_uid)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        list_resp = await client.get("/api/v1/dalton/monitors")
        assert list_resp.status_code == 200
        assert list_resp.json()["monitors"] == []

        detail_resp = await client.get(f"/api/v1/dalton/monitors/{company_monitor.slug}")
        assert detail_resp.status_code == 403

        data_resp = await client.get(f"/api/v1/dalton/monitors/g/{grant.token}/data/metrics")
        assert data_resp.status_code == 403

        file_resp = await client.get(f"/api/v1/dalton/monitors/g/{grant.token}/v/1/report.html")
        assert file_resp.status_code == 403


# ---------------------------------------------------------------------------
# Checklist 2: Non-admin public selection & bare /artifacts/{id}/publish rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checklist_2_non_admin_public_and_bare_artifact_publish_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton import access_tiers
    from src.server.app.artifacts import records as artifact_records
    from src.server.app.artifacts.models import PublishRequest
    from src.server.database import artifacts as artifacts_db

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    tiers = {
        "ext-1": "external",
        "int-1": "internal",
        "adm-1": "admin",
    }

    async def _tier(uid: str | None) -> str:
        return tiers.get(str(uid or ""), "external")

    monkeypatch.setattr(
        monitor_perms,
        "get_user_tier",
        create_autospec(monitor_perms.get_user_tier, side_effect=_tier),
    )
    monkeypatch.setattr(
        access_tiers,
        "get_user_tier",
        create_autospec(access_tiers.get_user_tier, side_effect=_tier),
    )
    monkeypatch.setattr(
        monitor_service,
        "owned_workspace_on_computer",
        create_autospec(
            monitor_service.owned_workspace_on_computer,
            return_value={"workspace_id": WS_ID, "user_id": "int-1", "status": "running"},
        ),
    )

    # External user cannot publish company/members/public
    for vis in ("members", "company", "public"):
        with pytest.raises(HTTPException) as exc:
            await monitor_service.publish_workspace_monitor(
                PublishMonitorRequest(
                    workspace_id=WS_ID,
                    path="report.html",
                    visibility=vis,  # type: ignore[arg-type]
                    member_user_ids=["int-1"] if vis == "members" else [],
                ),
                caller_id="ext-1",
            )
        assert exc.value.status_code == 403

    # Internal user cannot publish public
    with pytest.raises(HTTPException) as exc_int_pub:
        await monitor_service.publish_workspace_monitor(
            PublishMonitorRequest(
                workspace_id=WS_ID,
                path="report.html",
                visibility="public",
            ),
            caller_id="int-1",
        )
    assert exc_int_pub.value.status_code == 403

    # Internal user cannot update an existing monitor to public
    owned_monitor = _make_monitor(owner_user_id="int-1", visibility="company")
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(monitor_db.get_monitor_by_slug, return_value=owned_monitor),
    )
    monkeypatch.setattr(
        monitor_service,
        "get_user_tier",
        create_autospec(monitor_service.get_user_tier, side_effect=_tier),
    )
    monkeypatch.setattr(
        monitor_service,
        "db_get_workspace",
        create_autospec(
            monitor_service.db_get_workspace,
            return_value={"workspace_id": WS_ID, "user_id": "int-1", "status": "running"},
        ),
    )
    with pytest.raises(HTTPException) as exc_int_patch:
        await monitor_service.update_monitor_settings(
            owned_monitor.slug,
            UpdateMonitorRequest(visibility="public"),
            caller_id="int-1",
        )
    assert exc_int_patch.value.status_code == 403

    # Direct call to upstream POST /api/v1/artifacts/{id}/publish is refused for non-admins
    snapshot_publish = create_autospec(artifact_records.snapshot.publish)
    monkeypatch.setattr(artifact_records.snapshot, "publish", snapshot_publish)
    monkeypatch.setattr(
        artifacts_db,
        "get_artifact",
        create_autospec(artifacts_db.get_artifact, return_value=_make_artifact()),
    )

    for caller in ("ext-1", "int-1"):
        with pytest.raises(HTTPException) as exc_bare:
            await artifact_records.publish_artifact(
                ARTIFACT_ID,
                PublishRequest(files=["report.html"]),
                x_user_id=caller,
            )
        assert exc_bare.value.status_code == 403
    snapshot_publish.assert_not_called()


# ---------------------------------------------------------------------------
# Checklist 3: Public -> Company calls unshare (/a/{code} 404) & invalidates old grants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checklist_3_public_downgrade_unshares_and_invalidates_old_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.server.app import public as public_app
    from src.server.database import artifacts as artifacts_db
    from src.server.database.share_links import ShareLink

    public_monitor = _make_monitor(
        owner_user_id="adm-1",
        visibility="public",
        acl_revision=1,
    )
    downgraded_monitor = _make_monitor(
        owner_user_id="adm-1",
        visibility="company",
        acl_revision=2,
    )
    shared_state = {"shared_at": NOW}

    async def _fake_unshare(*, user_id: str, artifact_id: str) -> Artifact:
        shared_state["shared_at"] = None
        return _make_artifact(shared_at=None, revision=2)

    monkeypatch.setattr(
        monitor_service,
        "get_user_tier",
        create_autospec(monitor_service.get_user_tier, return_value="admin"),
    )
    monkeypatch.setattr(
        monitor_perms,
        "get_user_tier",
        create_autospec(monitor_perms.get_user_tier, return_value="admin"),
    )
    monkeypatch.setattr(
        monitor_service,
        "db_get_workspace",
        create_autospec(
            monitor_service.db_get_workspace,
            return_value={"workspace_id": WS_ID, "user_id": "adm-1", "status": "running"},
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(monitor_db.get_monitor_by_slug, return_value=public_monitor),
    )

    async def _fake_update_settings_tx(
        monitor_id: str, **kwargs: Any
    ) -> MonitorRecord:
        assert kwargs["visibility"] == "company"
        await _fake_unshare(user_id=kwargs["owner_user_id"], artifact_id=kwargs["artifact_id"])
        return downgraded_monitor

    tx_spy = create_autospec(
        monitor_db.update_monitor_settings_tx, side_effect=_fake_update_settings_tx
    )
    monkeypatch.setattr(monitor_db, "update_monitor_settings_tx", tx_spy)
    monkeypatch.setattr(
        monitor_db,
        "list_monitor_versions",
        create_autospec(monitor_db.list_monitor_versions, return_value=[]),
    )

    old_grant = await monitor_grants.mint_monitor_grant(MONITOR_ID, "adm-1", 1)

    updated = await monitor_service.update_monitor_settings(
        public_monitor.slug,
        UpdateMonitorRequest(visibility="company"),
        caller_id="adm-1",
    )
    assert updated["monitor"]["acl_revision"] == 2
    tx_spy.assert_awaited_once()
    assert shared_state["shared_at"] is None

    # /a/{code} now resolves to unshared -> 404 immediately
    unshared_link = ShareLink(
        code="moncode12345",
        workspace_id=WS_ID,
        kind="file",
        path="report.html",
        port=None,
        title=None,
        files=None,
        shared_at=shared_state["shared_at"],
        created_at=NOW,
        revision=2,
        artifact=_make_artifact(shared_at=shared_state["shared_at"], revision=2),
    )
    with pytest.raises(HTTPException) as exc_public:
        await public_app._latest_shared_version(unshared_link.artifact)
    assert exc_public.value.status_code == 404

    # Old grant (acl_revision=1) is immediately rejected (403) against downgraded monitor (acl_revision=2)
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_id",
        create_autospec(monitor_db.get_monitor_by_id, return_value=downgraded_monitor),
    )
    with pytest.raises(HTTPException) as exc_old_grant:
        await monitor_service.read_monitor_data_by_grant(old_grant.token, "metrics")
    assert exc_old_grant.value.status_code == 403


# ---------------------------------------------------------------------------
# Checklist 4: Members visibility — listed allowed, non-listed 403, demoted 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checklist_4_members_visibility_and_tier_demotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    members_monitor = _make_monitor(
        owner_user_id="owner-1",
        visibility="members",
        member_user_ids=("colleague-1",),
    )
    tiers = {
        "owner-1": "internal",
        "colleague-1": "internal",
        "colleague-2": "internal",
    }

    async def _tier(uid: str | None) -> str:
        return tiers.get(str(uid or ""), "external")

    monkeypatch.setattr(
        monitor_perms,
        "get_user_tier",
        create_autospec(monitor_perms.get_user_tier, side_effect=_tier),
    )
    monkeypatch.setattr(
        monitor_service,
        "get_user_tier",
        create_autospec(monitor_service.get_user_tier, side_effect=_tier),
    )
    monkeypatch.setattr(
        monitor_service,
        "db_get_workspace",
        create_autospec(
            monitor_service.db_get_workspace,
            return_value={"workspace_id": WS_ID, "user_id": "int-1", "status": "running"},
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(monitor_db.get_monitor_by_slug, return_value=members_monitor),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_id",
        create_autospec(monitor_db.get_monitor_by_id, return_value=members_monitor),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_data",
        create_autospec(
            monitor_db.get_monitor_data,
            return_value=MonitorDataEntry(
                monitor_id=MONITOR_ID,
                data_key="metrics",
                payload={"ok": True},
                as_of=NOW,
                sha256="b" * 64,
                updated_at=NOW,
            ),
        ),
    )

    grant_c1 = await monitor_grants.mint_monitor_grant(MONITOR_ID, "colleague-1", 1)

    # Listed internal member can view & read data
    assert await monitor_perms.can_view_monitor(members_monitor, "colleague-1") is True
    status, body, _ = await monitor_service.read_monitor_data_by_grant(
        grant_c1.token, "metrics"
    )
    assert status == 200
    assert body is not None and body["data"] == {"ok": True}

    # Unlisted internal colleague gets 403
    assert await monitor_perms.can_view_monitor(members_monitor, "colleague-2") is False
    with pytest.raises(HTTPException) as exc_unlisted:
        await monitor_service.get_monitor_detail(
            members_monitor.slug, viewer_id="colleague-2"
        )
    assert exc_unlisted.value.status_code == 403

    # Once colleague-1 ceases to be a company member (demoted to external), access is 403
    tiers["colleague-1"] = "external"
    assert await monitor_perms.can_view_monitor(members_monitor, "colleague-1") is False
    with pytest.raises(HTTPException) as exc_demoted:
        await monitor_service.read_monitor_data_by_grant(grant_c1.token, "metrics")
    assert exc_demoted.value.status_code == 403

    # Non-company member cannot be added to member_user_ids
    with pytest.raises(HTTPException) as exc_add_ext:
        await monitor_perms.validate_publish_permissions(
            caller_id="owner-1",
            visibility="members",
            member_user_ids=["colleague-1"],
        )
    assert exc_add_ext.value.status_code == 422


# ---------------------------------------------------------------------------
# Checklist 5: Workspace after-turn auto-update, hash dedup, and 100-version prune
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checklist_5_workspace_after_turn_update_dedup_and_prune(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.server.database import artifacts as artifacts_db

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    ws_monitor = _make_monitor(
        owner_user_id="int-owner",
        visibility="company",
        keep_updated=True,
        published_seq=1,
    )
    artifact = _make_artifact(shared_at=None, latest_seq=1)

    monkeypatch.setattr(
        monitor_db,
        "list_workspace_auto_update_monitors",
        create_autospec(
            monitor_db.list_workspace_auto_update_monitors, return_value=[ws_monitor]
        ),
    )
    monkeypatch.setattr(
        monitor_service,
        "db_get_workspace",
        create_autospec(
            monitor_service.db_get_workspace,
            return_value={"workspace_id": WS_ID, "user_id": "int-owner", "status": "running"},
        ),
    )
    monkeypatch.setattr(
        artifacts_db,
        "get_artifact",
        create_autospec(artifacts_db.get_artifact, return_value=artifact),
    )

    v1_bytes = b"<html><body>v1</body></html>"
    v2_bytes = b"<html><body>v2</body></html>"
    snap_v2 = Snapshot(
        entries=[ManifestEntry("report.html", len(v2_bytes), "entry")],
        left_out=[],
        files=[
            VersionFile(
                path="report.html",
                sha256=hashlib.sha256(v2_bytes).hexdigest(),
                byte_len=len(v2_bytes),
                content_type="text/html",
                content=v2_bytes,
            )
        ],
        blobs={},
        title="Monitor v2",
    )
    monkeypatch.setattr(
        monitor_service.artifact_snapshot,
        "capture_workspace_monitor_snapshot",
        create_autospec(
            monitor_service.artifact_snapshot.capture_workspace_monitor_snapshot,
            return_value=snap_v2,
        ),
    )

    cut_calls: list[dict[str, Any]] = []

    async def _fake_commit_version_tx(**kwargs: Any) -> tuple[MonitorRecord, PublishResult, bool]:
        cut_calls.append(kwargs)
        # First call changes -> seq 2; second call identical -> cut=False, seq 2
        if len(cut_calls) == 1:
            res = PublishResult(
                artifact=_make_artifact(latest_seq=2, shared_at=None),
                seq=2,
                cut=True,
                shared_now=False,
                changed=True,
            )
            return _make_monitor(published_seq=2, visibility="company"), res, False
        res = PublishResult(
            artifact=_make_artifact(latest_seq=2, shared_at=None),
            seq=2,
            cut=False,
            shared_now=False,
            changed=False,
        )
        return _make_monitor(published_seq=2, visibility="company"), res, False

    tx_spy = create_autospec(
        monitor_db.commit_monitor_version_tx, side_effect=_fake_commit_version_tx
    )
    monkeypatch.setattr(monitor_db, "commit_monitor_version_tx", tx_spy)
    real_prune = monitor_db._prune_monitor_versions_in_cur

    # Turn 1: file changed -> cuts seq=2 without sharing publicly
    await monitor_service.republish_workspace_monitors_after_turn(workspace_id=WS_ID)
    assert tx_spy.await_count == 1
    assert cut_calls[0]["monitor_id"] == MONITOR_ID

    # Turn 2: identical content -> cut=False
    await monitor_service.republish_workspace_monitors_after_turn(workspace_id=WS_ID)
    assert tx_spy.await_count == 2

    # Verify SQL emitted by prune_monitor_versions protects 100 latest + published_seq + comments
    executed_sql: list[tuple[str, Any]] = []
    cur = create_autospec(AsyncCursor, instance=True)
    cur.rowcount = 5
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = False

    async def _capture_exec(sql: str, params: Any = None) -> None:
        executed_sql.append((sql, params))

    cur.execute.side_effect = _capture_exec
    conn = create_autospec(AsyncConnection, instance=True)
    deleted_count = await real_prune(
        cur, ARTIFACT_ID, published_seq=105, keep_count=100
    )
    assert deleted_count == 5
    sql_text, sql_params = executed_sql[0]
    assert "DELETE FROM artifact_versions" in sql_text
    assert "artifact_comments" in sql_text
    assert "LIMIT %(keep_count)s" in sql_text
    assert sql_params["keep_count"] == 100
    assert sql_params["published_seq"] == 105


# ---------------------------------------------------------------------------
# Checklist 6: Data-job mapping, path confinement, run_id dedup, failure retention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checklist_6_data_job_publish_dedup_unregistered_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.server.database import artifacts as artifacts_db

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    data_root = tmp_path / "openclaw-data"
    demo_dir = data_root / "research-outputs" / "monitor-demo"
    demo_dir.mkdir(parents=True)
    (demo_dir / "index.html").write_text(build_live_demo_html(), encoding="utf-8")
    (demo_dir / "styles.css").write_text("body { color: #222; }", encoding="utf-8")

    # Unregistered job_key is rejected
    with pytest.raises(HTTPException) as exc_unreg:
        await monitor_service.republish_data_job_monitor(
            job_key="unregistered-arbitrary-job",
            run_id="run-1",
            data_root=data_root,
        )
    assert exc_unreg.value.status_code == 403

    # HostDirFileSource blocks path escape / symlinks
    source = HostDirFileSource(demo_dir)
    assert await source.stat(["../secret.txt", "index.html"]) == {
        "index.html": ("index.html", (demo_dir / "index.html").stat().st_size)
    }
    assert await source.read("../secret.txt") is None

    # Registered job publishes cleanly under COMPANY_SYSTEM_USER_ID
    stored_monitor: dict[str, MonitorRecord] = {}
    artifact = _make_artifact(path="index.html", latest_seq=0)

    monkeypatch.setattr(
        monitor_db,
        "ensure_company_monitor_workspace",
        create_autospec(
            monitor_db.ensure_company_monitor_workspace, return_value=WS_ID
        ),
    )
    monkeypatch.setattr(
        artifacts_db,
        "ensure_artifact",
        create_autospec(artifacts_db.ensure_artifact, return_value=artifact),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_data_job",
        create_autospec(
            monitor_db.get_monitor_by_data_job,
            side_effect=lambda jk: stored_monitor.get(jk),
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(monitor_db.get_monitor_by_slug, return_value=None),
    )

    def _upsert(**kwargs: Any) -> MonitorRecord:
        rec = _make_monitor(
            slug=kwargs["slug"],
            owner_user_id=kwargs["owner_user_id"],
            title=kwargs["title"],
            category=kwargs["category"],
            visibility=kwargs["visibility"],
            source_kind="data_job",
            source_ref=kwargs["source_ref"],
            published_seq=0,
            entry_path="index.html",
        )
        stored_monitor[kwargs["source_ref"]] = rec
        return rec

    monkeypatch.setattr(
        monitor_db,
        "upsert_monitor",
        create_autospec(monitor_db.upsert_monitor, side_effect=_upsert),
    )
    monkeypatch.setattr(
        monitor_service.artifact_snapshot,
        "get_vault_secrets_for_redaction",
        create_autospec(
            monitor_service.artifact_snapshot.get_vault_secrets_for_redaction,
            return_value={},
        ),
    )
    monkeypatch.setattr(
        monitor_service.artifact_snapshot,
        "store_unregistered_blobs",
        create_autospec(
            monitor_service.artifact_snapshot.store_unregistered_blobs,
            return_value=None,
        ),
    )
    processed_runs: set[tuple[str, str]] = set()

    async def _has_run(mid: str, rid: str) -> bool:
        return (mid, rid) in processed_runs

    monkeypatch.setattr(
        monitor_db,
        "has_processed_monitor_run",
        create_autospec(monitor_db.has_processed_monitor_run, side_effect=_has_run),
    )

    async def _commit_ver_tx(**kwargs: Any) -> tuple[MonitorRecord, PublishResult | None, bool]:
        cur = stored_monitor["monitor-live-demo"]
        rid = kwargs.get("run_id")
        if rid and (cur.monitor_id, rid) in processed_runs:
            return cur, None, True
        if rid:
            processed_runs.add((cur.monitor_id, rid))
        updated = _make_monitor(
            **{
                **cur.__dict__,
                "published_seq": 1,
                "last_publish_run_id": rid,
                "last_publish_error": None,
                "last_publish_failed_at": None,
            }
        )
        stored_monitor["monitor-live-demo"] = updated
        res = PublishResult(
            artifact=_make_artifact(path="index.html", latest_seq=1),
            seq=1,
            cut=True,
            shared_now=False,
            changed=True,
        )
        return updated, res, False

    monkeypatch.setattr(
        monitor_db,
        "commit_monitor_version_tx",
        create_autospec(monitor_db.commit_monitor_version_tx, side_effect=_commit_ver_tx),
    )

    def _rec_fail(mid: str, *, error_message: str, run_id: str | None = None) -> MonitorRecord:
        cur = stored_monitor["monitor-live-demo"]
        updated = _make_monitor(
            **{
                **cur.__dict__,
                "last_publish_error": error_message,
                "last_publish_failed_at": NOW,
            }
        )
        stored_monitor["monitor-live-demo"] = updated
        return updated

    monkeypatch.setattr(
        monitor_db,
        "record_monitor_publish_failure",
        create_autospec(monitor_db.record_monitor_publish_failure, side_effect=_rec_fail),
    )

    # First run succeeds
    res1 = await monitor_service.republish_data_job_monitor(
        job_key="monitor-live-demo",
        run_id="run-100",
        data_root=data_root,
    )
    assert res1["published_seq"] == 1
    assert res1["cut"] is True
    assert stored_monitor["monitor-live-demo"].owner_user_id == COMPANY_SYSTEM_USER_ID

    # Duplicate run_id is deduplicated (no new version)
    res_dup = await monitor_service.republish_data_job_monitor(
        job_key="monitor-live-demo",
        run_id="run-100",
        data_root=data_root,
    )
    assert res_dup["deduplicated"] is True
    assert res_dup["published_seq"] == 1

    # Failed job preserves published_seq=1 and records last_publish_error
    await monitor_service.maybe_publish_data_job_monitor(
        job_key="monitor-live-demo",
        run_id="run-101",
        terminal_status="failed",
        failure_reason="upstream timeout",
        data_root=data_root,
    )
    after_fail = stored_monitor["monitor-live-demo"]
    assert after_fail.published_seq == 1
    assert after_fail.last_publish_error == "upstream timeout"
    assert after_fail.last_publish_failed_at is not None

    # Internal HTTP route rejects arbitrary path fields (extra="forbid" -> 422)
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "svc-token-" + "x" * 32)
    app = _build_monitor_app("svc")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        bad_payload = await client.post(
            "/api/v1/dalton/monitors/internal/data-jobs/monitor-live-demo/publish",
            json={"run_id": "run-102", "path": "/etc/passwd"},
            headers={"X-Service-Token": "svc-token-" + "x" * 32},
        )
        assert bad_payload.status_code == 422


# ---------------------------------------------------------------------------
# Checklist 7: Origin: null + valid grant -> 200/304; expired/revoked -> 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checklist_7_realtime_data_origin_null_etag_304_and_sandbox_csp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.server.app.setup import _CORSMiddleware
    from src.server.database import artifacts as artifacts_db

    monitor = _make_monitor(owner_user_id="int-1", visibility="company", acl_revision=1)
    payload = {"price": 123.45, "beta": 1.12}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    expected_etag = hashlib.sha256(
        json.dumps(
            {
                "monitor_id": MONITOR_ID,
                "slug": monitor.slug,
                "key": "metrics",
                "as_of": NOW.isoformat(),
                "sha256": sha,
                "updated_at": NOW.isoformat(),
                "data": payload,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    html_bytes = build_live_demo_html().encode("utf-8")
    html_sha = hashlib.sha256(html_bytes).hexdigest()

    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_id",
        create_autospec(monitor_db.get_monitor_by_id, return_value=monitor),
    )
    tier_state = {"tier": "internal"}
    monkeypatch.setattr(
        monitor_perms,
        "get_user_tier",
        create_autospec(
            monitor_perms.get_user_tier,
            side_effect=lambda uid: tier_state["tier"],
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_data",
        create_autospec(
            monitor_db.get_monitor_data,
            return_value=MonitorDataEntry(
                monitor_id=MONITOR_ID,
                data_key="metrics",
                payload=payload,
                as_of=NOW,
                sha256=sha,
                updated_at=NOW,
            ),
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "is_monitor_version_published",
        create_autospec(monitor_db.is_monitor_version_published, return_value=True),
    )
    monkeypatch.setattr(
        artifacts_db,
        "get_version_file",
        create_autospec(
            artifacts_db.get_version_file,
            return_value=StoredVersionFile(
                user_id="int-1",
                sha256=html_sha,
                byte_len=len(html_bytes),
                content_type="text/html",
                content=html_bytes,
            ),
        ),
    )

    app = FastAPI()
    app.add_middleware(
        _CORSMiddleware,
        allow_origins=["http://app.local"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    app.include_router(monitors_router)

    live_grant = await monitor_grants.mint_monitor_grant(
        MONITOR_ID, "int-1", 1, ttl=3600, now=1_800_000_000
    )
    # Expired grant (issued in the past)
    expired_grant = await monitor_grants.mint_monitor_grant(
        MONITOR_ID, "int-1", 1, ttl=10, now=1_000_000_000
    )

    with patch("src.server.services.signed_grants.time.time", return_value=1_800_000_100):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            # 1. Origin: null + valid grant -> 200 with ETag & as_of & CORS *
            resp_200 = await client.get(
                f"/api/v1/dalton/monitors/g/{live_grant.token}/data/metrics",
                headers={"Origin": "null"},
            )
            assert resp_200.status_code == 200
            assert resp_200.headers.get("access-control-allow-origin") == "*"
            assert "access-control-allow-credentials" not in resp_200.headers
            assert resp_200.headers.get("etag") == f'"{expected_etag}"'
            body = resp_200.json()
            assert body["data"] == payload
            assert body["as_of"].startswith("2026-11-16T12:00:00")

            # 2. If-None-Match -> 304 Not Modified
            resp_304 = await client.get(
                f"/api/v1/dalton/monitors/g/{live_grant.token}/data/metrics",
                headers={"Origin": "null", "If-None-Match": f'"{expected_etag}"'},
            )
            assert resp_304.status_code == 304

            # 3. Sandboxed HTML serving has CSP sandbox without allow-same-origin
            resp_html = await client.get(
                f"/api/v1/dalton/monitors/g/{live_grant.token}/v/1/report.html",
                headers={"Origin": "null"},
            )
            assert resp_html.status_code == 200
            csp = resp_html.headers.get("content-security-policy", "")
            assert "sandbox allow-scripts" in csp
            assert "allow-same-origin" not in csp
            assert "page:monitor-grant" in resp_html.text

            # 4. Expired grant -> 403
            resp_exp = await client.get(
                f"/api/v1/dalton/monitors/g/{expired_grant.token}/data/metrics",
                headers={"Origin": "null"},
            )
            assert resp_exp.status_code == 403

            # 5. Revoked permission (tier becomes external) -> 403
            tier_state["tier"] = "external"
            resp_revoked = await client.get(
                f"/api/v1/dalton/monitors/g/{live_grant.token}/data/metrics",
                headers={"Origin": "null"},
            )
            assert resp_revoked.status_code == 403


# ---------------------------------------------------------------------------
# Round 2 regression tests (Items 1–7, 9–11, 15)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r2_1_thread_share_allow_files_requires_admin_including_historical_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import Response
    from src.dalton import access_tiers
    from src.server.app import public as public_app
    from src.server.app import share_access
    from src.server.app.threads import sharing as threads_sharing
    from src.server.models.conversation import SharePermissions, ThreadShareRequest
    from src.server.utils import api as auth_api

    tiers = {"int-u": "internal", "adm-u": "admin"}
    monkeypatch.setattr(
        threads_sharing, "is_dalton_supabase_auth_enabled", lambda: True
    )
    monkeypatch.setattr(
        share_access, "is_dalton_supabase_auth_enabled", lambda: True
    )
    monkeypatch.setattr(
        access_tiers,
        "get_user_tier",
        create_autospec(access_tiers.get_user_tier, side_effect=lambda uid: tiers[uid]),
    )
    monkeypatch.setattr(
        auth_api,
        "require_thread_owner",
        create_autospec(auth_api.require_thread_owner, return_value=None),
    )

    threads = {
        "th-int": {
            "conversation_thread_id": "th-int",
            "workspace_id": WS_ID,
            "user_id": "int-u",
            "workspace_owner_id": "int-u",
            "is_shared": True,
            "share_token": "tok-int",
            "share_permissions": {"allow_files": True, "allow_download": False},
        },
        "th-adm": {
            "conversation_thread_id": "th-adm",
            "workspace_id": WS_ID,
            "user_id": "adm-u",
            "workspace_owner_id": "adm-u",
            "is_shared": True,
            "share_token": "tok-adm",
            "share_permissions": {"allow_files": True, "allow_download": False},
        },
    }
    monkeypatch.setattr(
        threads_sharing,
        "get_thread_by_id",
        create_autospec(
            threads_sharing.get_thread_by_id, side_effect=lambda tid: threads.get(tid)
        ),
    )
    monkeypatch.setattr(
        threads_sharing,
        "update_thread_sharing",
        create_autospec(
            threads_sharing.update_thread_sharing,
            side_effect=lambda tid, **kw: {**threads[tid], **kw},
        ),
    )

    # 1a. Internal non-admin cannot enable allow_files=True or allow_download=True
    with pytest.raises(HTTPException) as exc_files:
        await threads_sharing.update_thread_share(
            "th-int",
            ThreadShareRequest(
                is_shared=True, permissions=SharePermissions(allow_files=True)
            ),
            x_user_id="int-u",
        )
    assert exc_files.value.status_code == 403

    # 1b. Non-admin CAN still share conversation content without files
    conv_only = await threads_sharing.update_thread_share(
        "th-int",
        ThreadShareRequest(
            is_shared=True, permissions=SharePermissions(allow_files=False)
        ),
        x_user_id="int-u",
    )
    assert conv_only.is_shared is True

    # 1c. Admin CAN enable allow_files=True
    adm_files = await threads_sharing.update_thread_share(
        "th-adm",
        ThreadShareRequest(
            is_shared=True, permissions=SharePermissions(allow_files=True)
        ),
        x_user_id="adm-u",
    )
    assert adm_files.permissions.allow_files is True

    # 1d. Legacy non-admin share with allow_files=True is blocked at read time (403)
    # while conversation metadata/replay continues to work with allow_files masked False
    orig_get_shared_thread = share_access.get_shared_thread
    orig_serving_ws = share_access._serving_workspace
    monkeypatch.setattr(
        share_access,
        "get_shared_thread",
        create_autospec(orig_get_shared_thread, return_value=threads["th-int"]),
    )
    monkeypatch.setattr(
        share_access,
        "_serving_workspace",
        create_autospec(
            orig_serving_ws,
            return_value=({"workspace_id": WS_ID, "user_id": "int-u"}, "/workspace"),
        ),
    )
    with pytest.raises(HTTPException) as exc_legacy_read:
        await share_access.resolve_shared_files("tok-int", require_files=True)
    assert exc_legacy_read.value.status_code == 403

    monkeypatch.setattr(
        public_app,
        "resolve_link",
        create_autospec(public_app.resolve_link, return_value=None),
    )
    monkeypatch.setattr(
        public_app,
        "get_shared_thread",
        create_autospec(public_app.get_shared_thread, return_value=threads["th-int"]),
    )
    meta = await public_app.get_shared_thread_metadata(
        "tok-int",
        page_viewer=public_app.Viewer(None),
        response=Response(),
        view_as=None,
        path=None,
    )
    assert meta["kind"] == "thread"
    assert meta["permissions"] == {"allow_files": False, "allow_download": False}

    # Admin historical/active file share still resolves
    monkeypatch.setattr(
        share_access,
        "get_shared_thread",
        create_autospec(orig_get_shared_thread, return_value=threads["th-adm"]),
    )
    monkeypatch.setattr(
        share_access,
        "_serving_workspace",
        create_autospec(
            orig_serving_ws,
            return_value=({"workspace_id": WS_ID, "user_id": "adm-u"}, "/workspace"),
        ),
    )
    target = await share_access.resolve_shared_files("tok-adm", require_files=True)
    assert target.workspace_id == WS_ID


@pytest.mark.asyncio
async def test_r2_2_atomic_visibility_unshare_rollback_and_concurrent_lock_reread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.server.database.artifacts import records as artifacts_records_db
    from src.server.database.artifacts import versions as artifacts_versions_db

    # Part A: unshare failure rolls back the entire transaction (visibility stays 'public')
    state = {
        "visibility": "public",
        "shared_at": NOW,
        "acl_revision": 1,
        "published_seq": 2,
        "owner_user_id": "adm-1",
    }

    @asynccontextmanager
    async def _tx_conn():
        snapshot = dict(state)
        cur = create_autospec(AsyncCursor, instance=True)
        cur.__aenter__.return_value = cur
        cur.__aexit__.return_value = False
        cur._rows: list[Any] = []

        async def _exec(sql: str, params: Any = None) -> None:
            if "FROM dalton_monitors" in sql and "FOR UPDATE" in sql:
                cur._rows = [
                    {
                        "title": "Monitor",
                        "category": "portfolio",
                        "sort_order": 0,
                        "visibility": state["visibility"],
                        "keep_updated": False,
                        "published_seq": state["published_seq"],
                        "owner_user_id": state["owner_user_id"],
                        "source_kind": "workspace",
                    }
                ]
            elif "FROM dalton_monitor_members" in sql:
                cur._rows = []
            elif "UPDATE dalton_monitors" in sql:
                state["visibility"] = params[3]
                state["acl_revision"] += int(params[6])
                cur._rows = []
            else:
                cur._rows = []

        async def _fetchone() -> Any:
            return cur._rows[0] if cur._rows else None

        async def _fetchall() -> list[Any]:
            return list(cur._rows)

        async def _noop(*_args: Any, **_kwargs: Any) -> None:
            return None

        cur.execute.side_effect = _exec
        cur.executemany.side_effect = _noop
        cur.fetchone.side_effect = _fetchone
        cur.fetchall.side_effect = _fetchall

        conn = create_autospec(AsyncConnection, instance=True)
        conn.cursor.return_value = cur

        @asynccontextmanager
        async def _tx():
            try:
                yield
            except Exception:
                state.clear()
                state.update(snapshot)
                raise

        conn.transaction.side_effect = _tx
        yield conn

    monkeypatch.setattr(monitor_db, "get_db_connection", _tx_conn)
    orig_lock_owned = artifacts_records_db.lock_owned
    orig_unshare_cur = artifacts_records_db.unshare_in_cur
    orig_prune_cur = monitor_db._prune_monitor_versions_in_cur
    monkeypatch.setattr(
        artifacts_records_db,
        "lock_owned",
        create_autospec(
            orig_lock_owned,
            return_value=_make_artifact(shared_at=NOW, latest_seq=2, shared_seq=2),
        ),
    )
    monkeypatch.setattr(
        artifacts_records_db,
        "unshare_in_cur",
        create_autospec(
            orig_unshare_cur,
            side_effect=RuntimeError("unshare DB failure"),
        ),
    )

    monkeypatch.setattr(
        monitor_db,
        "get_user_tier",
        create_autospec(monitor_db.get_user_tier, return_value="admin"),
    )
    with pytest.raises(RuntimeError, match="unshare DB failure"):
        await monitor_db.update_monitor_settings_tx(
            MONITOR_ID,
            artifact_id=ARTIFACT_ID,
            owner_user_id="adm-1",
            caller_id="adm-1",
            title="Monitor",
            category="portfolio",
            sort_order=0,
            visibility="company",
            keep_updated=False,
            member_user_ids=(),
        )
    # Entire transaction rolled back: visibility never left as 'company' while share open
    assert state["visibility"] == "public"
    assert state["acl_revision"] == 1
    assert state["shared_at"] == NOW

    # Part B: republish commit_monitor_version_tx re-reads visibility under FOR UPDATE lock
    # Simulates a concurrent downgrade having already flipped DB visibility to 'company'
    locked_visibility = {"value": "company"}
    cut_share_flags: list[bool] = []
    unshare_calls: list[str] = []

    @asynccontextmanager
    async def _republish_tx_conn():
        cur = create_autospec(AsyncCursor, instance=True)
        cur.__aenter__.return_value = cur
        cur.__aexit__.return_value = False
        cur._rows: list[Any] = []

        async def _exec(sql: str, params: Any = None) -> None:
            if "FROM dalton_monitors" in sql and "FOR UPDATE" in sql:
                cur._rows = [
                    {
                        "monitor_id": MONITOR_ID,
                        "visibility": locked_visibility["value"],
                        "owner_user_id": "adm-1",
                        "title": "Monitor",
                        "published_seq": 2,
                    }
                ]
            elif "FROM dalton_monitors m" in sql:
                cur._rows = [
                    {
                        "monitor_id": MONITOR_ID,
                        "slug": "portfolio-beta",
                        "artifact_id": ARTIFACT_ID,
                        "owner_user_id": "adm-1",
                        "title": "Monitor",
                        "category": "portfolio",
                        "sort_order": 0,
                        "visibility": locked_visibility["value"],
                        "published_seq": 3,
                        "keep_updated": False,
                        "source_kind": "workspace",
                        "source_ref": f"{WS_ID}:report.html",
                        "acl_revision": 2,
                        "last_publish_run_id": None,
                        "last_publish_error": None,
                        "last_publish_failed_at": None,
                        "created_at": NOW,
                        "updated_at": NOW,
                        "artifact_code": "moncode12345",
                        "workspace_id": WS_ID,
                        "entry_path": "report.html",
                        "owner_name": "Alice",
                        "owner_email": "alice@company.com",
                        "data_as_of": None,
                        "member_user_ids": (),
                    }
                ]
            else:
                cur._rows = []

        cur.execute.side_effect = _exec
        cur.fetchone.side_effect = lambda: cur._rows[0] if cur._rows else None

        conn = create_autospec(AsyncConnection, instance=True)
        conn.cursor.return_value = cur

        @asynccontextmanager
        async def _tx():
            yield

        conn.transaction.side_effect = _tx
        yield conn

    monkeypatch.setattr(monitor_db, "get_db_connection", _republish_tx_conn)
    # Upstream artifact still appears shared when lock starts
    monkeypatch.setattr(
        artifacts_records_db,
        "lock_owned",
        create_autospec(
            orig_lock_owned,
            return_value=_make_artifact(shared_at=NOW, latest_seq=2, shared_seq=2),
        ),
    )

    async def _unshare_cur(_cur: Any, *, user_id: str, artifact_id: str) -> Artifact:
        unshare_calls.append(artifact_id)
        return _make_artifact(shared_at=None, latest_seq=2, shared_seq=0, revision=2)

    monkeypatch.setattr(
        artifacts_records_db,
        "unshare_in_cur",
        create_autospec(orig_unshare_cur, side_effect=_unshare_cur),
    )

    async def _cut_in_cur(_cur: Any, locked: Artifact, **kwargs: Any) -> PublishResult:
        cut_share_flags.append(bool(kwargs["share_public"]))
        return PublishResult(
            artifact=_make_artifact(shared_at=None, latest_seq=3, shared_seq=0),
            seq=3,
            cut=True,
            shared_now=False,
            changed=True,
        )

    monkeypatch.setattr(
        artifacts_versions_db,
        "cut_monitor_in_cur",
        create_autospec(artifacts_versions_db.cut_monitor_in_cur, side_effect=_cut_in_cur),
    )
    monkeypatch.setattr(
        monitor_db,
        "_prune_monitor_versions_in_cur",
        create_autospec(orig_prune_cur, return_value=0),
    )

    updated_mon, _res, deduped = await monitor_db.commit_monitor_version_tx(
        monitor_id=MONITOR_ID,
        artifact_id=ARTIFACT_ID,
        workspace_owner_id="adm-1",
        files=[
            VersionFile(
                path="report.html",
                sha256="b" * 64,
                byte_len=10,
                content_type="text/html",
                content=b"<html>v3</html>",
            )
        ],
        title="Monitor",
    )
    assert deduped is False
    # Re-read under FOR UPDATE detected downgrade -> share_public=False AND ensured unshare
    assert cut_share_flags == [False]
    assert unshare_calls == [ARTIFACT_ID]
    assert updated_mon.visibility == "company"


@pytest.mark.asyncio
async def test_r2_3_public_write_paths_check_current_admin_and_data_job_approver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.dalton import access_tiers
    from src.server.app.artifacts import records as artifact_records
    from src.server.app.artifacts.models import KeepUpdatedRequest, KeptRequest
    from src.server.database import artifacts as artifacts_db
    from src.server.services.artifacts import snapshot as artifact_snapshot

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    tiers = {"former-adm": "internal", "active-adm": "admin"}
    monkeypatch.setattr(
        monitor_service,
        "get_user_tier",
        create_autospec(monitor_service.get_user_tier, side_effect=lambda u: tiers[u]),
    )
    monkeypatch.setattr(
        monitor_perms,
        "get_user_tier",
        create_autospec(monitor_perms.get_user_tier, side_effect=lambda u: tiers[u]),
    )
    monkeypatch.setattr(
        access_tiers,
        "get_user_tier",
        create_autospec(access_tiers.get_user_tier, side_effect=lambda u: tiers[u]),
    )

    public_mon = _make_monitor(
        owner_user_id="former-adm",
        visibility="public",
        keep_updated=True,
        published_seq=2,
    )
    failures: list[str] = []

    async def _rec_fail(mid: str, *, error_message: str, run_id: str | None = None) -> MonitorRecord:
        failures.append(error_message)
        return _make_monitor(**{**public_mon.__dict__, "last_publish_error": error_message})

    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(monitor_db.get_monitor_by_slug, return_value=public_mon),
    )
    monkeypatch.setattr(
        monitor_db,
        "record_monitor_publish_failure",
        create_autospec(monitor_db.record_monitor_publish_failure, side_effect=_rec_fail),
    )

    # 3a. Manual /republish refused for demoted admin, recording exact failure message
    with pytest.raises(HTTPException) as exc_repub:
        await monitor_service.republish_workspace_monitor(
            public_mon.slug, RepublishMonitorRequest(), caller_id="former-adm"
        )
    assert exc_repub.value.status_code == 403
    assert "发布人已无公开权限" in str(exc_repub.value.detail)
    assert failures[-1] == "发布人已无公开权限"

    # 3b. Auto-update after turn skips cutting public monitor owned by demoted admin
    orig_list_auto = monitor_db.list_workspace_auto_update_monitors
    monkeypatch.setattr(
        monitor_db,
        "list_workspace_auto_update_monitors",
        create_autospec(orig_list_auto, return_value=[public_mon]),
    )
    monkeypatch.setattr(
        monitor_service,
        "db_get_workspace",
        create_autospec(
            monitor_service.db_get_workspace,
            return_value={
                "workspace_id": WS_ID,
                "user_id": "former-adm",
                "status": "running",
            },
        ),
    )
    cut_spy = create_autospec(monitor_db.commit_monitor_version_tx)
    monkeypatch.setattr(monitor_db, "commit_monitor_version_tx", cut_spy)
    await monitor_service.republish_workspace_monitors_after_turn(workspace_id=WS_ID)
    cut_spy.assert_not_awaited()
    assert failures[-1] == "发布人已无公开权限"

    # 3c. Admin can downgrade/take over demoted user's public monitor
    monkeypatch.setattr(
        monitor_db,
        "update_monitor_settings_tx",
        create_autospec(
            monitor_db.update_monitor_settings_tx,
            return_value=_make_monitor(
                owner_user_id="active-adm", visibility="company", acl_revision=2
            ),
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "list_monitor_versions",
        create_autospec(monitor_db.list_monitor_versions, return_value=[]),
    )
    adopted = await monitor_service.update_monitor_settings(
        public_mon.slug, UpdateMonitorRequest(visibility="company"), caller_id="active-adm"
    )
    assert adopted["monitor"]["visibility"] == "company"

    # 3d. Data-job public binding requires approved_by_admin_user_id AND current admin tier
    unapproved_pub = MonitorDataJobBinding(
        job_key="job-unapproved-pub",
        slug="job-unapproved",
        title="Unapproved Public",
        category="overview",
        visibility="public",
        rel_dir="research-outputs/unapproved",
        approved_by_admin_user_id=None,
    )
    demoted_approver_pub = MonitorDataJobBinding(
        job_key="job-demoted-pub",
        slug="job-demoted",
        title="Demoted Public",
        category="overview",
        visibility="public",
        rel_dir="research-outputs/demoted",
        approved_by_admin_user_id="former-adm",
    )
    monkeypatch.setitem(
        data_job_registry.MONITOR_DATA_JOB_REGISTRY, unapproved_pub.job_key, unapproved_pub
    )
    monkeypatch.setitem(
        data_job_registry.MONITOR_DATA_JOB_REGISTRY,
        demoted_approver_pub.job_key,
        demoted_approver_pub,
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_data_job",
        create_autospec(monitor_db.get_monitor_by_data_job, return_value=None),
    )

    with pytest.raises(HTTPException) as exc_unapp:
        await monitor_service.republish_data_job_monitor(
            job_key=unapproved_pub.job_key, run_id="r-1", data_root=tmp_path
        )
    assert exc_unapp.value.status_code == 403

    with pytest.raises(HTTPException) as exc_dem:
        await monitor_service.republish_data_job_monitor(
            job_key=demoted_approver_pub.job_key, run_id="r-2", data_root=tmp_path
        )
    assert exc_dem.value.status_code == 403
    assert "发布人已无公开权限" in str(exc_dem.value.detail)

    # 3e. Upstream PATCH /artifacts/{id} keep_updated=True & PUT /kept=True on shared artifact require admin
    from src.server.app import share_links as _share_links

    shared_art = _make_artifact(shared_at=NOW, latest_seq=1, shared_seq=1)
    monkeypatch.setattr(
        artifact_records,
        "owned",
        create_autospec(artifact_records.owned, return_value=shared_art),
    )
    monkeypatch.setattr(
        _share_links,
        "get_user_tier",
        create_autospec(_share_links.get_user_tier, side_effect=lambda u: tiers[u]),
    )
    with pytest.raises(HTTPException) as exc_patch_keep:
        await artifact_records.update_artifact(
            ARTIFACT_ID, KeepUpdatedRequest(keep_updated=True), x_user_id="former-adm"
        )
    assert exc_patch_keep.value.status_code == 403

    with pytest.raises(HTTPException) as exc_put_kept:
        await artifact_records.set_artifact_kept(
            ARTIFACT_ID, KeptRequest(kept=True), x_user_id="former-adm"
        )
    assert exc_put_kept.value.status_code == 403

    # Upstream keep_updated_after_turn also skips shared keep_updated artifact for demoted admin
    monkeypatch.setattr(
        "src.dalton.settings.is_dalton_supabase_auth_enabled", lambda: True
    )
    monkeypatch.setattr(
        monitor_db,
        "list_workspace_auto_update_monitors",
        create_autospec(orig_list_auto, return_value=[]),
    )
    monkeypatch.setattr(
        artifacts_db,
        "list_kept_updated",
        create_autospec(
            artifacts_db.list_kept_updated,
            return_value=[_make_artifact(shared_at=NOW, keep_updated=True, latest_seq=1)],
        ),
    )
    monkeypatch.setattr(
        artifact_snapshot,
        "_release_unlisted",
        create_autospec(
            artifact_snapshot._release_unlisted,
            side_effect=lambda kept, **_kw: kept,
        ),
    )
    monkeypatch.setattr(
        artifact_snapshot,
        "db_get_workspace",
        create_autospec(
            artifact_snapshot.db_get_workspace,
            return_value={
                "workspace_id": WS_ID,
                "user_id": "former-adm",
                "status": "running",
            },
        ),
    )
    keep_one_spy = create_autospec(artifact_snapshot._keep_one)
    monkeypatch.setattr(artifact_snapshot, "_keep_one", keep_one_spy)
    await artifact_snapshot.keep_updated_after_turn(
        workspace_id=WS_ID, user_id="former-adm"
    )
    keep_one_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_r2_4_data_job_registry_rejects_symlinks_and_traversals_without_resolve(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "openclaw-data"
    (data_root / "research-outputs").mkdir(parents=True)
    in_root_other = data_root / "other-secret"
    in_root_other.mkdir()
    (in_root_other / "index.html").write_text("<h1>secret</h1>", encoding="utf-8")

    # Even if symlink target lies INSIDE data_root, resolve_registered_job_dir must refuse it
    symlink_demo = data_root / "research-outputs" / "monitor-demo"
    symlink_demo.symlink_to(in_root_other)

    binding = data_job_registry.get_registered_monitor_job("monitor-live-demo")
    assert binding is not None
    with pytest.raises(ValueError, match="Symlink not allowed"):
        resolve_registered_job_dir(data_root, binding)

    # Path traversal in binding rel_dir is refused
    traversal_binding = MonitorDataJobBinding(
        job_key="bad-rel",
        slug="bad-rel",
        title="Bad",
        category="other",
        visibility="company",
        rel_dir="research-outputs/../../etc",
    )
    with pytest.raises(ValueError, match="Invalid registered"):
        resolve_registered_job_dir(data_root, traversal_binding)

    # HostDirFileSource refuses symlinked subdirs & symlinked files via dir_fd + O_NOFOLLOW
    real_demo = data_root / "research-outputs" / "real-demo"
    real_demo.mkdir()
    (real_demo / "index.html").write_text("<h1>ok</h1>", encoding="utf-8")
    (real_demo / "sub").mkdir()
    secret_file = tmp_path / "outside.json"
    secret_file.write_text('{"secret": 1}', encoding="utf-8")
    (real_demo / "sub" / "leak.json").symlink_to(secret_file)
    (real_demo / "linked-dir").symlink_to(in_root_other)

    source = HostDirFileSource(real_demo)
    assert await source.read("index.html") == b"<h1>ok</h1>"
    assert await source.read("sub/leak.json") is None
    assert await source.read("linked-dir/index.html") is None
    assert await source.read("/index.html") is None
    assert await source.read("../outside.json") is None
    assert await source.stat(["sub/leak.json", "linked-dir/index.html", "index.html"]) == {
        "index.html": ("index.html", len(b"<h1>ok</h1>"))
    }


@pytest.mark.asyncio
async def test_r2_5_run_id_db_dedup_persists_across_later_runs_and_skips_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.server.database import artifacts as artifacts_db
    from src.server.database.artifacts import records as artifacts_records_db
    from src.server.database.artifacts import versions as artifacts_versions_db

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    data_root = tmp_path / "openclaw-data"
    demo_dir = data_root / "research-outputs" / "monitor-demo"
    demo_dir.mkdir(parents=True)
    (demo_dir / "index.html").write_text("<h1>v1</h1>", encoding="utf-8")

    # Simulate PostgreSQL dalton_monitor_publish_runs table with PK (monitor_id, run_id)
    pk_ledger: dict[tuple[str, str], int] = {}
    monitor_row: dict[str, Any] = {
        "monitor_id": MONITOR_ID,
        "slug": "live-demo",
        "artifact_id": ARTIFACT_ID,
        "owner_user_id": COMPANY_SYSTEM_USER_ID,
        "title": "Demo",
        "category": "overview",
        "visibility": "company",
        "published_seq": 0,
        "last_publish_run_id": None,
        "last_publish_error": None,
        "last_publish_failed_at": None,
    }
    seq_counter = {"seq": 0}

    @asynccontextmanager
    async def _ledger_conn():
        snapshot_ledger = dict(pk_ledger)
        cur = create_autospec(AsyncCursor, instance=True)
        cur.__aenter__.return_value = cur
        cur.__aexit__.return_value = False
        cur._rows: list[Any] = []

        async def _exec(sql: str, params: Any = None) -> None:
            if "FROM dalton_monitors" in sql and "FOR UPDATE" in sql:
                cur._rows = [dict(monitor_row)]
            elif "FROM dalton_monitor_publish_runs" in sql and sql.lstrip().startswith(
                "SELECT 1"
            ):
                mid, rid = params
                cur._rows = [(1,)] if (mid, rid) in pk_ledger else []
            elif "INSERT INTO dalton_monitor_publish_runs" in sql:
                mid, rid = params[0], params[1]
                if (mid, rid) in pk_ledger:
                    cur._rows = []
                else:
                    pk_ledger[(mid, rid)] = params[2] if len(params) > 2 else 0
                    cur._rows = [{"monitor_id": mid}]
            elif "UPDATE dalton_monitor_publish_runs" in sql:
                pseq, mid, rid = params
                pk_ledger[(mid, rid)] = pseq
                cur._rows = []
            elif "UPDATE dalton_monitors" in sql and "last_publish_error = NULL" in sql:
                monitor_row["published_seq"] = params[0]
                monitor_row["last_publish_run_id"] = params[2]
                monitor_row["last_publish_error"] = None
                cur._rows = []
            elif "UPDATE dalton_monitors" in sql and "last_publish_error = %s" in sql:
                monitor_row["last_publish_error"] = params[0]
                monitor_row["last_publish_failed_at"] = NOW
                cur._rows = []
            elif "FROM dalton_monitors m" in sql:
                cur._rows = [
                    {
                        **monitor_row,
                        "sort_order": 0,
                        "keep_updated": True,
                        "source_kind": "data_job",
                        "source_ref": "monitor-live-demo",
                        "acl_revision": 1,
                        "created_at": NOW,
                        "updated_at": NOW,
                        "artifact_code": "demo1234",
                        "workspace_id": WS_ID,
                        "entry_path": "index.html",
                        "owner_name": "Company",
                        "owner_email": None,
                        "data_as_of": None,
                        "member_user_ids": (),
                    }
                ]
            else:
                cur._rows = []

        cur.execute.side_effect = _exec
        cur.fetchone.side_effect = lambda: cur._rows[0] if cur._rows else None

        conn = create_autospec(AsyncConnection, instance=True)
        conn.cursor.return_value = cur

        @asynccontextmanager
        async def _tx():
            try:
                yield
            except Exception:
                pk_ledger.clear()
                pk_ledger.update(snapshot_ledger)
                raise

        conn.transaction.side_effect = _tx
        yield conn

    monkeypatch.setattr(monitor_db, "get_db_connection", _ledger_conn)
    monkeypatch.setattr(
        monitor_db,
        "ensure_company_monitor_workspace",
        create_autospec(monitor_db.ensure_company_monitor_workspace, return_value=WS_ID),
    )
    monkeypatch.setattr(
        artifacts_db,
        "ensure_artifact",
        create_autospec(
            artifacts_db.ensure_artifact,
            return_value=_make_artifact(path="index.html", latest_seq=0),
        ),
    )
    monkeypatch.setattr(
        monitor_service.artifact_snapshot,
        "get_vault_secrets_for_redaction",
        create_autospec(
            monitor_service.artifact_snapshot.get_vault_secrets_for_redaction,
            return_value={},
        ),
    )
    monkeypatch.setattr(
        monitor_service.artifact_snapshot,
        "store_unregistered_blobs",
        create_autospec(monitor_service.artifact_snapshot.store_unregistered_blobs),
    )
    monkeypatch.setattr(
        artifacts_records_db,
        "lock_owned",
        create_autospec(
            artifacts_records_db.lock_owned,
            return_value=_make_artifact(path="index.html", latest_seq=0),
        ),
    )

    async def _cut_seq(_cur: Any, _locked: Artifact, **kwargs: Any) -> PublishResult:
        seq_counter["seq"] += 1
        s = seq_counter["seq"]
        return PublishResult(
            artifact=_make_artifact(path="index.html", latest_seq=s),
            seq=s,
            cut=True,
            shared_now=False,
            changed=True,
        )

    monkeypatch.setattr(
        artifacts_versions_db,
        "cut_monitor_in_cur",
        create_autospec(artifacts_versions_db.cut_monitor_in_cur, side_effect=_cut_seq),
    )
    monkeypatch.setattr(
        monitor_db,
        "_prune_monitor_versions_in_cur",
        create_autospec(monitor_db._prune_monitor_versions_in_cur, return_value=0),
    )

    # 1) run-A succeeds -> seq 1; run-B succeeds -> seq 2; replaying run-A is STILL deduplicated!
    r_a1 = await monitor_service.republish_data_job_monitor(
        job_key="monitor-live-demo", run_id="run-A", data_root=data_root
    )
    assert r_a1["published_seq"] == 1
    r_b = await monitor_service.republish_data_job_monitor(
        job_key="monitor-live-demo", run_id="run-B", data_root=data_root
    )
    assert r_b["published_seq"] == 2
    assert monitor_row["last_publish_run_id"] == "run-B"

    # Re-delivering run-A AFTER run-B still deduplicates cleanly!
    r_a2 = await monitor_service.republish_data_job_monitor(
        job_key="monitor-live-demo", run_id="run-A", data_root=data_root
    )
    assert r_a2["deduplicated"] is True
    assert r_a2["published_seq"] == 2

    # 2) Failed run-C does NOT get marked processed -> subsequent retry of run-C executes!
    await monitor_service.maybe_publish_data_job_monitor(
        job_key="monitor-live-demo",
        run_id="run-C",
        terminal_status="failed",
        failure_reason="boom",
        data_root=data_root,
    )
    assert (MONITOR_ID, "run-C") not in pk_ledger
    r_c = await monitor_service.republish_data_job_monitor(
        job_key="monitor-live-demo", run_id="run-C", data_root=data_root
    )
    assert r_c["deduplicated"] is False
    assert r_c["published_seq"] == 3


@pytest.mark.asyncio
async def test_r2_6_auto_update_aborts_on_missing_previous_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.server.database import artifacts as artifacts_db

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    ws_mon = _make_monitor(
        owner_user_id="int-owner",
        visibility="company",
        keep_updated=True,
        published_seq=4,
    )
    artifact = _make_artifact(latest_seq=4)
    html = b"<html><script src='chart.js'></script></html>"
    snap_missing_dep = Snapshot(
        entries=[ManifestEntry("report.html", len(html), "entry")],
        left_out=[],
        files=[
            VersionFile(
                path="report.html",
                sha256=hashlib.sha256(html).hexdigest(),
                byte_len=len(html),
                content_type="text/html",
                content=html,
            )
        ],
        blobs={},
        title="Monitor",
        absent=("chart.js",),
    )

    monkeypatch.setattr(
        monitor_db,
        "list_workspace_auto_update_monitors",
        create_autospec(
            monitor_db.list_workspace_auto_update_monitors, return_value=[ws_mon]
        ),
    )
    monkeypatch.setattr(
        monitor_service,
        "db_get_workspace",
        create_autospec(
            monitor_service.db_get_workspace,
            return_value={
                "workspace_id": WS_ID,
                "user_id": "int-owner",
                "status": "running",
            },
        ),
    )
    monkeypatch.setattr(
        artifacts_db,
        "get_artifact",
        create_autospec(artifacts_db.get_artifact, return_value=artifact),
    )
    monkeypatch.setattr(
        monitor_service.artifact_snapshot,
        "capture_workspace_monitor_snapshot",
        create_autospec(
            monitor_service.artifact_snapshot.capture_workspace_monitor_snapshot,
            return_value=snap_missing_dep,
        ),
    )
    monkeypatch.setattr(
        artifacts_db,
        "get_version_hashes",
        create_autospec(
            artifacts_db.get_version_hashes,
            return_value={"report.html": "1" * 64, "chart.js": "2" * 64},
        ),
    )
    cut_spy = create_autospec(monitor_db.commit_monitor_version_tx)
    monkeypatch.setattr(monitor_db, "commit_monitor_version_tx", cut_spy)
    fail_spy = create_autospec(
        monitor_db.record_monitor_publish_failure, return_value=ws_mon
    )
    monkeypatch.setattr(monitor_db, "record_monitor_publish_failure", fail_spy)

    await monitor_service.republish_workspace_monitors_after_turn(workspace_id=WS_ID)
    cut_spy.assert_not_awaited()
    fail_spy.assert_awaited_once()
    assert "chart.js" in fail_spy.await_args.kwargs["error_message"]


@pytest.mark.asyncio
async def test_r2_7_master_switch_blocks_new_public_data_job_but_allows_downgrade_and_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.server.database import artifacts as artifacts_db
    from src.server.services.workspace_files import owned_workspace_on_computer

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "0")
    monkeypatch.setattr(
        monitor_perms,
        "get_user_tier",
        create_autospec(monitor_perms.get_user_tier, return_value="admin"),
    )
    monkeypatch.setattr(
        monitor_service,
        "get_user_tier",
        create_autospec(monitor_service.get_user_tier, return_value="admin"),
    )
    monkeypatch.setattr(
        monitor_service,
        "owned_workspace_on_computer",
        create_autospec(
            owned_workspace_on_computer,
            return_value={"workspace_id": WS_ID, "user_id": "adm-1", "status": "running"},
        ),
    )
    monkeypatch.setattr(
        monitor_service,
        "owner_work_dir",
        create_autospec(monitor_service.owner_work_dir, return_value="/home/workspace"),
    )
    monkeypatch.setattr(
        monitor_service,
        "previous_dir_names_of",
        create_autospec(monitor_service.previous_dir_names_of, return_value=()),
    )
    monkeypatch.setattr(
        artifacts_db,
        "ensure_artifact",
        create_autospec(artifacts_db.ensure_artifact, return_value=_make_artifact()),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_artifact_id",
        create_autospec(monitor_db.get_monitor_by_artifact_id, return_value=None),
    )

    # New publish blocked when switch off
    with pytest.raises(HTTPException) as exc_pub:
        await monitor_service.publish_workspace_monitor(
            PublishMonitorRequest(workspace_id=WS_ID, path="report.html"),
            caller_id="adm-1",
        )
    assert exc_pub.value.status_code == 403
    assert exc_pub.value.detail == {"code": "artifact_publishing_disabled"}

    # Data-job republish blocked when switch off
    with pytest.raises(HTTPException) as exc_job:
        await monitor_service.republish_data_job_monitor(
            job_key="monitor-live-demo", run_id="r-off", data_root=tmp_path
        )
    assert exc_job.value.status_code == 403
    assert exc_job.value.detail == {"code": "artifact_publishing_disabled"}

    # Upgrading existing monitor to public blocked when switch off
    orig_get_by_slug = monitor_db.get_monitor_by_slug
    priv_mon = _make_monitor(owner_user_id="adm-1", visibility="company")
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(orig_get_by_slug, return_value=priv_mon),
    )
    with pytest.raises(HTTPException) as exc_up:
        await monitor_service.update_monitor_settings(
            priv_mon.slug, UpdateMonitorRequest(visibility="public"), caller_id="adm-1"
        )
    assert exc_up.value.status_code == 403
    assert exc_up.value.detail == {"code": "artifact_publishing_disabled"}

    # Downgrade (public -> company) and deletion STILL allowed when switch off
    pub_mon = _make_monitor(owner_user_id="adm-1", visibility="public")
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(orig_get_by_slug, return_value=pub_mon),
    )
    monkeypatch.setattr(
        monitor_service,
        "db_get_workspace",
        create_autospec(
            monitor_service.db_get_workspace,
            return_value={"workspace_id": WS_ID, "user_id": "adm-1", "status": "running"},
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "update_monitor_settings_tx",
        create_autospec(
            monitor_db.update_monitor_settings_tx,
            return_value=_make_monitor(owner_user_id="adm-1", visibility="company", acl_revision=2),
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "list_monitor_versions",
        create_autospec(monitor_db.list_monitor_versions, return_value=[]),
    )
    downgraded = await monitor_service.update_monitor_settings(
        pub_mon.slug, UpdateMonitorRequest(visibility="company"), caller_id="adm-1"
    )
    assert downgraded["monitor"]["visibility"] == "company"

    del_spy = create_autospec(monitor_db.delete_monitor_tx, return_value=True)
    monkeypatch.setattr(monitor_db, "delete_monitor_tx", del_spy)
    deleted = await monitor_service.delete_monitor_by_slug(pub_mon.slug, caller_id="adm-1")
    assert deleted == {"deleted": True, "slug": pub_mon.slug}


@pytest.mark.asyncio
async def test_r2_9_etag_changes_when_as_of_changes_with_identical_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor = _make_monitor(owner_user_id="int-1", visibility="company", acl_revision=1)
    payload = {"price": 100.0}
    sha = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    later = datetime(2026, 11, 16, 12, 5, 0, tzinfo=UTC)
    current_entry = {
        "val": MonitorDataEntry(
            monitor_id=MONITOR_ID,
            data_key="metrics",
            payload=payload,
            as_of=NOW,
            sha256=sha,
            updated_at=NOW,
        )
    }

    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_id",
        create_autospec(monitor_db.get_monitor_by_id, return_value=monitor),
    )
    monkeypatch.setattr(
        monitor_perms,
        "get_user_tier",
        create_autospec(monitor_perms.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_data",
        create_autospec(
            monitor_db.get_monitor_data, side_effect=lambda _mid, _k: current_entry["val"]
        ),
    )

    grant = await monitor_grants.mint_monitor_grant(MONITOR_ID, "int-1", 1)
    s1, _b1, h1 = await monitor_service.read_monitor_data_by_grant(grant.token, "metrics")
    assert s1 == 200

    # Identical payload, newer as_of -> MUST NOT return 304 for old ETag
    current_entry["val"] = MonitorDataEntry(
        monitor_id=MONITOR_ID,
        data_key="metrics",
        payload=payload,
        as_of=later,
        sha256=sha,
        updated_at=later,
    )
    s2, b2, h2 = await monitor_service.read_monitor_data_by_grant(
        grant.token, "metrics", if_none_match=h1["ETag"]
    )
    assert s2 == 200
    assert h2["ETag"] != h1["ETag"]
    assert b2 is not None and b2["as_of"] == later.isoformat()


@pytest.mark.asyncio
async def test_r2_10_reserved_monitor_slugs_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.server.database import artifacts as artifacts_db
    from src.server.services.workspace_files import owned_workspace_on_computer

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    for reserved in ("company-members", "g", "by-artifact", "internal", "new"):
        assert reserved in RESERVED_MONITOR_SLUGS
        assert is_valid_slug(reserved) is False
        assert slugify_candidate(reserved) not in RESERVED_MONITOR_SLUGS

    monkeypatch.setattr(
        monitor_perms,
        "get_user_tier",
        create_autospec(monitor_perms.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        monitor_service,
        "owned_workspace_on_computer",
        create_autospec(
            owned_workspace_on_computer,
            return_value={"workspace_id": WS_ID, "user_id": "int-1", "status": "running"},
        ),
    )
    monkeypatch.setattr(
        monitor_service,
        "owner_work_dir",
        create_autospec(monitor_service.owner_work_dir, return_value="/home/workspace"),
    )
    monkeypatch.setattr(
        monitor_service,
        "previous_dir_names_of",
        create_autospec(monitor_service.previous_dir_names_of, return_value=()),
    )
    monkeypatch.setattr(
        artifacts_db,
        "ensure_artifact",
        create_autospec(artifacts_db.ensure_artifact, return_value=_make_artifact()),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_artifact_id",
        create_autospec(monitor_db.get_monitor_by_artifact_id, return_value=None),
    )

    for bad_slug in ("company-members", "g", "by-artifact"):
        with pytest.raises(HTTPException) as exc:
            await monitor_service.publish_workspace_monitor(
                PublishMonitorRequest(
                    workspace_id=WS_ID,
                    path="report.html",
                    slug=bad_slug,
                ),
                caller_id="int-1",
            )
        assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_r2_11_internal_data_write_enforces_data_job_and_data_keys_whitelist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_mon = _make_monitor(slug="ws-mon", source_kind="workspace", source_ref=f"{WS_ID}:report.html")
    job_mon = _make_monitor(slug="live-demo", source_kind="data_job", source_ref="monitor-live-demo")

    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(
            monitor_db.get_monitor_by_slug,
            side_effect=lambda s: ws_mon if s == "ws-mon" else job_mon,
        ),
    )
    upsert_spy = create_autospec(
        monitor_db.upsert_monitor_data,
        return_value=MonitorDataEntry(
            monitor_id=MONITOR_ID,
            data_key="metrics",
            payload={"ok": True},
            as_of=NOW,
            sha256="a" * 64,
            updated_at=NOW,
        ),
    )
    monkeypatch.setattr(monitor_db, "upsert_monitor_data", upsert_spy)

    # Workspace monitor cannot receive internal data writes
    with pytest.raises(HTTPException) as exc_ws:
        await monitor_service.write_monitor_data_internal(
            "ws-mon", "metrics", {"ok": True}
        )
    assert exc_ws.value.status_code == 403

    # Non-whitelisted data_key on data_job monitor is rejected
    with pytest.raises(HTTPException) as exc_key:
        await monitor_service.write_monitor_data_internal(
            "live-demo", "unregistered-key", {"ok": True}
        )
    assert exc_key.value.status_code == 403

    # Registered data_key succeeds
    res = await monitor_service.write_monitor_data_internal(
        "live-demo", "metrics", {"ok": True}
    )
    assert res["key"] == "metrics"
    upsert_spy.assert_awaited_once()


def test_r2_15_live_demo_html_uses_builtin_copy_and_css_variables_without_hex_fallbacks() -> None:
    import re

    zh_html = build_live_demo_html(locale="zh-CN")
    en_html = build_live_demo_html(locale="en-US")

    # Built-in bilingual copy without reading frontend source tree at runtime
    assert "实时数据监控示例" in zh_html
    assert "Realtime Monitor Demo" in en_html
    assert "Data as of: " in en_html

    # Zero hardcoded hex or rgba color literals inside generated demo template
    assert re.search(r"#[0-9a-fA-F]{3,8}", zh_html) is None
    assert "rgba(" not in zh_html
    assert "var(--color-bg-page)" in zh_html
    assert "var(--color-text-primary)" in zh_html


# ---------------------------------------------------------------------------
# Round 3 fixes (Items 1-8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r3_1_patch_concurrencies_only_update_explicit_fields_inside_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from src.server.database.artifacts import records as artifacts_records_db

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    db_lock = asyncio.Lock()
    state: dict[str, Any] = {
        "monitor_id": MONITOR_ID,
        "slug": "portfolio-beta",
        "artifact_id": ARTIFACT_ID,
        "owner_user_id": "int-1",
        "title": "Initial Title",
        "category": "portfolio",
        "sort_order": 0,
        "visibility": "company",
        "keep_updated": False,
        "published_seq": 1,
        "acl_revision": 1,
        "shared_at": None,
        "members": set(),
    }

    async def _tier(uid: str | None) -> str:
        return "internal"

    monkeypatch.setattr(
        monitor_service,
        "get_user_tier",
        create_autospec(monitor_service.get_user_tier, side_effect=_tier),
    )
    monkeypatch.setattr(
        monitor_service,
        "db_get_workspace",
        create_autospec(
            monitor_service.db_get_workspace,
            return_value={"workspace_id": WS_ID, "user_id": "int-1", "status": "running"},
        ),
    )
    def _read_state_monitor(_arg: Any = None) -> MonitorRecord:
        return _make_monitor(
            owner_user_id="int-1",
            title=state["title"],
            category=state["category"],
            sort_order=state["sort_order"],
            visibility=state["visibility"],
            keep_updated=state["keep_updated"],
            acl_revision=state["acl_revision"],
            member_user_ids=tuple(sorted(state["members"])),
        )

    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(monitor_db.get_monitor_by_slug, side_effect=_read_state_monitor),
    )
    monkeypatch.setattr(
        monitor_db,
        "_select_monitor_in_cur",
        create_autospec(
            monitor_db._select_monitor_in_cur,
            side_effect=lambda _cur, _mid: _read_state_monitor(),
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "list_monitor_versions",
        create_autospec(monitor_db.list_monitor_versions, return_value=[]),
    )
    monkeypatch.setattr(
        artifacts_records_db,
        "lock_owned",
        create_autospec(
            artifacts_records_db.lock_owned,
            side_effect=lambda *_args, **_kwargs: _make_artifact(
                shared_at=state["shared_at"]
            ),
        ),
    )

    @asynccontextmanager
    async def _locked_conn():
        async with db_lock:
            cur = create_autospec(AsyncCursor, instance=True)
            cur.__aenter__.return_value = cur
            cur.__aexit__.return_value = False
            cur._rows: list[Any] = []

            async def _exec(sql: str, params: Any = None) -> None:
                if "FROM dalton_monitors" in sql and "FOR UPDATE" in sql:
                    cur._rows = [
                        {
                            "title": state["title"],
                            "category": state["category"],
                            "sort_order": state["sort_order"],
                            "visibility": state["visibility"],
                            "keep_updated": state["keep_updated"],
                            "published_seq": state["published_seq"],
                            "owner_user_id": state["owner_user_id"],
                            "source_kind": "workspace",
                        }
                    ]
                elif "FROM dalton_monitor_members" in sql and sql.lstrip().startswith(
                    "SELECT"
                ):
                    cur._rows = [(m,) for m in sorted(state["members"])]
                elif "UPDATE dalton_monitors" in sql:
                    (
                        state["title"],
                        state["category"],
                        state["sort_order"],
                        state["visibility"],
                        state["keep_updated"],
                        state["owner_user_id"],
                        rev_delta,
                        _mid,
                    ) = (
                        params[0],
                        params[1],
                        params[2],
                        params[3],
                        params[4],
                        params[5],
                        params[6],
                        params[7],
                    )
                    state["acl_revision"] += int(rev_delta)
                    cur._rows = []
                elif "DELETE FROM dalton_monitor_members" in sql:
                    state["members"].clear()
                    cur._rows = []
                else:
                    cur._rows = []

            async def _execmany(_sql: str, rows: list[tuple[str, str]]) -> None:
                for _mid, uid in rows:
                    state["members"].add(uid)

            cur.execute.side_effect = _exec
            cur.executemany.side_effect = _execmany
            cur.fetchone.side_effect = lambda: cur._rows[0] if cur._rows else None
            cur.fetchall.side_effect = lambda: list(cur._rows)

            conn = create_autospec(AsyncConnection, instance=True)
            conn.cursor.return_value = cur

            @asynccontextmanager
            async def _tx():
                yield

            conn.transaction.side_effect = _tx
            yield conn

    monkeypatch.setattr(monitor_db, "get_db_connection", _locked_conn)

    # Interleave: Req A checks visibility outside lock while state is 'company', then waits;
    # Req B updates visibility='own'; Req A then enters lock with only title='Renamed'.
    req_a_ready = asyncio.Event()
    req_b_done = asyncio.Event()
    orig_update_tx = monitor_db.update_monitor_settings_tx
    call_idx = 0

    async def _interleaved_tx(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_idx
        call_idx += 1
        my_idx = call_idx
        if my_idx == 1:
            req_a_ready.set()
            await req_b_done.wait()
        return await orig_update_tx(*args, **kwargs)

    monkeypatch.setattr(
        monitor_db,
        "update_monitor_settings_tx",
        create_autospec(orig_update_tx, side_effect=_interleaved_tx),
    )

    async def _req_a() -> Any:
        return await monitor_service.update_monitor_settings(
            "portfolio-beta",
            UpdateMonitorRequest(title="Renamed Title"),
            caller_id="int-1",
        )

    async def _req_b() -> Any:
        await req_a_ready.wait()
        try:
            return await monitor_service.update_monitor_settings(
                "portfolio-beta",
                UpdateMonitorRequest(visibility="own"),
                caller_id="int-1",
            )
        finally:
            req_b_done.set()

    await asyncio.gather(_req_a(), _req_b())
    # Req A must preserve Req B's locked visibility='own' while applying title='Renamed Title'
    assert state["visibility"] == "own"
    assert state["title"] == "Renamed Title"


@pytest.mark.asyncio
async def test_r3_2_delete_locks_before_inspecting_public_state_against_concurrent_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.server.database.artifacts import records as artifacts_records_db

    # Pre-lock read observes visibility='company', then concurrent tx upgrades to 'public'
    pre_lock_monitor = _make_monitor(owner_user_id="adm-1", visibility="company")
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(monitor_db.get_monitor_by_slug, return_value=pre_lock_monitor),
    )
    monkeypatch.setattr(
        monitor_service,
        "get_user_tier",
        create_autospec(monitor_service.get_user_tier, return_value="admin"),
    )
    monkeypatch.setattr(
        monitor_service,
        "db_get_workspace",
        create_autospec(
            monitor_service.db_get_workspace,
            return_value={"workspace_id": WS_ID, "user_id": "adm-1", "status": "running"},
        ),
    )

    locked_state = {"visibility": "public", "shared_at": NOW, "deleted": False}
    lock_events: list[str] = []

    async def _lock_owned(*_args: Any, **_kwargs: Any) -> Artifact:
        lock_events.append("lock_artifact")
        return _make_artifact(shared_at=locked_state["shared_at"])

    async def _unshare(*_args: Any, **_kwargs: Any) -> Artifact:
        lock_events.append("unshare_artifact")
        locked_state["shared_at"] = None
        return _make_artifact(shared_at=None)

    monkeypatch.setattr(
        artifacts_records_db,
        "lock_owned",
        create_autospec(artifacts_records_db.lock_owned, side_effect=_lock_owned),
    )
    monkeypatch.setattr(
        artifacts_records_db,
        "unshare_in_cur",
        create_autospec(artifacts_records_db.unshare_in_cur, side_effect=_unshare),
    )

    @asynccontextmanager
    async def _delete_conn():
        cur = create_autospec(AsyncCursor, instance=True)
        cur.__aenter__.return_value = cur
        cur.__aexit__.return_value = False
        cur._rows: list[Any] = []
        cur.rowcount = 1

        async def _exec(sql: str, params: Any = None) -> None:
            if "FROM dalton_monitors" in sql and "FOR UPDATE" in sql:
                lock_events.append("lock_monitor")
                cur._rows = [
                    {"visibility": locked_state["visibility"], "owner_user_id": "adm-1"}
                ]
            elif "DELETE FROM dalton_monitors" in sql:
                lock_events.append("delete_monitor")
                locked_state["deleted"] = True
                cur._rows = []
            else:
                cur._rows = []

        cur.execute.side_effect = _exec
        cur.fetchone.side_effect = lambda: cur._rows[0] if cur._rows else None
        conn = create_autospec(AsyncConnection, instance=True)
        conn.cursor.return_value = cur

        @asynccontextmanager
        async def _tx():
            yield

        conn.transaction.side_effect = _tx
        yield conn

    monkeypatch.setattr(monitor_db, "get_db_connection", _delete_conn)

    res = await monitor_service.delete_monitor_by_slug("portfolio-beta", caller_id="adm-1")
    assert res == {"deleted": True, "slug": "portfolio-beta"}
    assert locked_state["deleted"] is True
    assert locked_state["shared_at"] is None
    assert lock_events == [
        "lock_artifact",
        "lock_monitor",
        "unshare_artifact",
        "delete_monitor",
    ]


@pytest.mark.asyncio
async def test_r3_3_data_job_registry_dir_fd_immune_to_parent_symlink_replacement(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "openclaw-data"
    parent_dir = data_root / "research-outputs"
    demo_dir = parent_dir / "monitor-demo"
    demo_dir.mkdir(parents=True)
    (demo_dir / "index.html").write_text("<h1>legit</h1>", encoding="utf-8")

    outside_dir = tmp_path / "outside-attack"
    outside_sub = outside_dir / "monitor-demo"
    outside_sub.mkdir(parents=True)
    (outside_sub / "index.html").write_text("<h1>pwned</h1>", encoding="utf-8")

    binding = data_job_registry.get_registered_monitor_job("monitor-live-demo")
    assert binding is not None

    # Open via pinned dir_fd chain while parent is clean
    with HostDirFileSource.open_registered(data_root, binding) as source:
        # Attacker replaces intermediate parent dir 'research-outputs' with a symlink
        renamed_parent = data_root / "research-outputs-old"
        parent_dir.rename(renamed_parent)
        parent_dir.symlink_to(outside_dir)

        # Already-opened HostDirFileSource stays pinned to the original directory inode
        assert await source.read("index.html") == b"<h1>legit</h1>"
        files = source.list_artifact_files()
        assert files == ("index.html",)

    # A new lookup now rejects the symlinked intermediate parent directory via O_NOFOLLOW
    with pytest.raises(ValueError, match="Symlink not allowed"):
        HostDirFileSource.open_registered(data_root, binding)


@pytest.mark.asyncio
async def test_r3_4_kept_unpublished_versions_hidden_from_monitor_list_and_file_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.server.database import artifacts as artifacts_db
    from src.server.database.artifacts import versions as artifacts_versions_db

    # Artifact has seq 1 & 3 published to monitor, seq 2 kept by owner only (not in monitor)
    v1 = Version(seq=1, entry_path="report.html", origin="owner", title="V1", label=None, byte_len=10, published_at=NOW)
    v2_kept = Version(seq=2, entry_path="report.html", origin="keep_updated", title="V2", label=None, byte_len=10, published_at=NOW)
    v3 = Version(seq=3, entry_path="report.html", origin="owner", title="V3", label=None, byte_len=10, published_at=NOW)

    monitor = _make_monitor(owner_user_id="int-1", visibility="company", published_seq=3)

    @asynccontextmanager
    async def _versions_conn():
        cur = create_autospec(AsyncCursor, instance=True)
        cur.__aenter__.return_value = cur
        cur.__aexit__.return_value = False
        cur._rows: list[Any] = []

        async def _exec(sql: str, params: Any = None) -> None:
            if "JOIN dalton_monitor_versions" in sql:
                cur._rows = [
                    {
                        "seq": 3,
                        "entry_path": "report.html",
                        "origin": "owner",
                        "title": "V3",
                        "label": None,
                        "byte_len": 10,
                        "published_at": NOW,
                    },
                    {
                        "seq": 1,
                        "entry_path": "report.html",
                        "origin": "owner",
                        "title": "V1",
                        "label": None,
                        "byte_len": 10,
                        "published_at": NOW,
                    },
                ]
            elif "FROM dalton_monitor_versions" in sql:
                _mid, req_seq = params
                cur._rows = [(1,)] if req_seq in (1, 3) else []
            else:
                cur._rows = []

        cur.execute.side_effect = _exec
        cur.fetchone.side_effect = lambda: cur._rows[0] if cur._rows else None
        cur.fetchall.side_effect = lambda: list(cur._rows)
        conn = create_autospec(AsyncConnection, instance=True)
        conn.cursor.return_value = cur
        yield conn

    monkeypatch.setattr(monitor_db, "get_db_connection", _versions_conn)
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(monitor_db.get_monitor_by_slug, return_value=monitor),
    )
    monkeypatch.setattr(
        monitor_perms,
        "get_user_tier",
        create_autospec(monitor_perms.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        artifacts_db,
        "get_version_file",
        create_autospec(
            artifacts_db.get_version_file,
            return_value=StoredVersionFile(
                user_id="int-1",
                sha256="a" * 64,
                byte_len=9,
                content_type="text/html",
                content=b"<p>ok</p>",
            ),
        ),
    )
    monkeypatch.setattr(
        artifacts_versions_db,
        "list_versions",
        create_autospec(artifacts_versions_db.list_versions, return_value=[v3, v2_kept, v1]),
    )

    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_id",
        create_autospec(monitor_db.get_monitor_by_id, return_value=monitor),
    )
    detail = await monitor_service.get_monitor_detail("portfolio-beta", viewer_id="int-2")
    assert [v["seq"] for v in detail["versions"]] == [3, 1]

    grant = await monitor_grants.mint_monitor_grant(
        MONITOR_ID, "int-2", monitor.acl_revision
    )
    # Kept-only seq=2 returns 404 even when artifacts_db has the version file
    with pytest.raises(HTTPException) as exc_kept:
        await monitor_service.serve_monitor_file(grant.token, "2", "report.html")
    assert exc_kept.value.status_code == 404

    # Published seq=3 succeeds
    served = await monitor_service.serve_monitor_file(grant.token, "3", "report.html")
    assert b"<p>ok</p>" in served.body


@pytest.mark.asyncio
async def test_r3_5_public_anonymous_monitor_routes_serve_public_and_404_when_non_public(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.server.database import artifacts as artifacts_db

    monkeypatch.setenv("DALTON_MONITOR_GRANT_SECRET", TEST_SIGNING_KEY.decode())
    public_monitor = _make_monitor(
        owner_user_id="adm-1", visibility="public", published_seq=2
    )
    company_monitor = _make_monitor(
        owner_user_id="adm-1", visibility="company", published_seq=2
    )
    current_monitor = {"value": public_monitor}

    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(
            monitor_db.get_monitor_by_slug,
            side_effect=lambda _s: current_monitor["value"],
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "list_monitor_versions",
        create_autospec(
            monitor_db.list_monitor_versions,
            return_value=[
                Version(
                    seq=2,
                    entry_path="report.html",
                    origin="owner",
                    title="V2",
                    label=None,
                    byte_len=12,
                    published_at=NOW,
                ),
                Version(
                    seq=1,
                    entry_path="report.html",
                    origin="owner",
                    title="V1",
                    label=None,
                    byte_len=12,
                    published_at=NOW,
                ),
            ],
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "is_monitor_version_published",
        create_autospec(monitor_db.is_monitor_version_published, return_value=True),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_data",
        create_autospec(
            monitor_db.get_monitor_data,
            return_value=MonitorDataEntry(
                monitor_id=MONITOR_ID,
                data_key="beta",
                payload={"beta": 1.15},
                as_of=NOW,
                sha256="b" * 64,
                updated_at=NOW,
            ),
        ),
    )
    monkeypatch.setattr(
        artifacts_db,
        "get_version_file",
        create_autospec(
            artifacts_db.get_version_file,
            return_value=StoredVersionFile(
                user_id="adm-1",
                sha256="c" * 64,
                byte_len=14,
                content_type="text/html",
                content=b"<h1>public</h1>",
            ),
        ),
    )

    app = FastAPI()
    app.include_router(monitors_router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Anonymous requests work for public monitors (detail, current file, data)
        r_detail = await client.get("/api/v1/dalton/monitors/public/portfolio-beta")
        assert r_detail.status_code == 200
        assert r_detail.json()["monitor"]["visibility"] == "public"

        r_file = await client.get(
            "/api/v1/dalton/monitors/public/portfolio-beta/v/2/report.html"
        )
        assert r_file.status_code == 200
        assert b"<h1>public</h1>" in r_file.content

        # Old version not served on anonymous current-version public endpoint
        r_old = await client.get(
            "/api/v1/dalton/monitors/public/portfolio-beta/v/1/report.html"
        )
        assert r_old.status_code == 404

        r_data = await client.get(
            "/api/v1/dalton/monitors/public/portfolio-beta/data/beta"
        )
        assert r_data.status_code == 200
        assert r_data.json()["data"] == {"beta": 1.15}

        # Once downgraded to non-public, all anonymous /public/* routes return 404
        current_monitor["value"] = company_monitor
        assert (
            await client.get("/api/v1/dalton/monitors/public/portfolio-beta")
        ).status_code == 404
        assert (
            await client.get(
                "/api/v1/dalton/monitors/public/portfolio-beta/v/2/report.html"
            )
        ).status_code == 404
        assert (
            await client.get("/api/v1/dalton/monitors/public/portfolio-beta/data/beta")
        ).status_code == 404


@pytest.mark.asyncio
async def test_r3_6_master_switch_off_downgrade_or_delete_skips_file_capture_and_cuts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.server.database import artifacts as artifacts_db
    from src.server.services.artifacts import snapshot as artifact_snapshot

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "0")
    pub_monitor = _make_monitor(owner_user_id="adm-1", visibility="public", published_seq=3)

    monkeypatch.setattr(
        monitor_service,
        "get_user_tier",
        create_autospec(monitor_service.get_user_tier, return_value="admin"),
    )
    monkeypatch.setattr(
        monitor_perms,
        "get_user_tier",
        create_autospec(monitor_perms.get_user_tier, return_value="admin"),
    )
    monkeypatch.setattr(
        monitor_service,
        "owned_workspace_on_computer",
        create_autospec(
            monitor_service.owned_workspace_on_computer,
            return_value={
                "workspace_id": WS_ID,
                "user_id": "adm-1",
                "status": "running",
                "work_dir": "/workspace",
            },
        ),
    )
    monkeypatch.setattr(
        monitor_service,
        "owner_work_dir",
        create_autospec(monitor_service.owner_work_dir, return_value="/workspace"),
    )
    monkeypatch.setattr(
        monitor_service,
        "previous_dir_names_of",
        create_autospec(monitor_service.previous_dir_names_of, return_value=()),
    )
    monkeypatch.setattr(
        artifacts_db,
        "ensure_artifact",
        create_autospec(artifacts_db.ensure_artifact, return_value=_make_artifact()),
    )
    orig_get_by_art = monitor_db.get_monitor_by_artifact_id
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_artifact_id",
        create_autospec(orig_get_by_art, return_value=pub_monitor),
    )
    monkeypatch.setattr(
        monitor_db,
        "list_monitor_versions",
        create_autospec(monitor_db.list_monitor_versions, return_value=[]),
    )
    capture_spy = create_autospec(artifact_snapshot.capture_workspace_monitor_snapshot)
    monkeypatch.setattr(artifact_snapshot, "capture_workspace_monitor_snapshot", capture_spy)

    downgraded = _make_monitor(
        owner_user_id="adm-1", visibility="company", published_seq=3, acl_revision=2
    )
    update_tx_spy = create_autospec(
        monitor_db.update_monitor_settings_tx, return_value=downgraded
    )
    monkeypatch.setattr(monitor_db, "update_monitor_settings_tx", update_tx_spy)

    res = await monitor_service.publish_workspace_monitor(
        PublishMonitorRequest(
            workspace_id=WS_ID,
            path="report.html",
            visibility="company",
        ),
        caller_id="adm-1",
    )
    assert res["cut"] is False
    assert res["monitor"]["visibility"] == "company"
    capture_spy.assert_not_awaited()
    update_tx_spy.assert_awaited_once()

    # Creating a brand-new monitor while switch is off still fails closed (403)
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_artifact_id",
        create_autospec(orig_get_by_art, return_value=None),
    )
    with pytest.raises(HTTPException) as exc_off:
        await monitor_service.publish_workspace_monitor(
            PublishMonitorRequest(
                workspace_id=WS_ID,
                path="new.html",
                visibility="company",
            ),
            caller_id="adm-1",
        )
    assert exc_off.value.status_code == 403


@pytest.mark.asyncio
async def test_r3_7_first_public_publish_rechecks_admin_inside_commit_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.server.database import artifacts as artifacts_db
    from src.server.database.artifacts import records as artifacts_records_db
    from src.server.database.artifacts import versions as artifacts_versions_db
    from src.server.services.artifacts import snapshot as artifact_snapshot

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    tier_state = {"adm-1": "admin"}

    async def _tier(uid: str | None) -> str:
        return tier_state.get(str(uid or ""), "external")

    monkeypatch.setattr(
        monitor_service,
        "get_user_tier",
        create_autospec(monitor_service.get_user_tier, side_effect=_tier),
    )
    monkeypatch.setattr(
        monitor_perms,
        "get_user_tier",
        create_autospec(monitor_perms.get_user_tier, side_effect=_tier),
    )
    monkeypatch.setattr(
        monitor_service,
        "owned_workspace_on_computer",
        create_autospec(
            monitor_service.owned_workspace_on_computer,
            return_value={
                "workspace_id": WS_ID,
                "user_id": "adm-1",
                "status": "running",
                "work_dir": "/workspace",
            },
        ),
    )
    monkeypatch.setattr(
        monitor_service,
        "owner_work_dir",
        create_autospec(monitor_service.owner_work_dir, return_value="/workspace"),
    )
    monkeypatch.setattr(
        monitor_service,
        "previous_dir_names_of",
        create_autospec(monitor_service.previous_dir_names_of, return_value=()),
    )
    monkeypatch.setattr(
        artifacts_db,
        "ensure_artifact",
        create_autospec(artifacts_db.ensure_artifact, return_value=_make_artifact()),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_artifact_id",
        create_autospec(monitor_db.get_monitor_by_artifact_id, return_value=None),
    )
    monkeypatch.setattr(
        monitor_db,
        "pick_available_slug",
        create_autospec(monitor_db.pick_available_slug, return_value="report"),
    )

    async def _capture_and_demote(*_args: Any, **_kwargs: Any) -> Any:
        # User is demoted from admin after pre-snapshot check, before DB lock commits
        tier_state["adm-1"] = "internal"
        return Snapshot(
            files=[
                VersionFile(
                    path="report.html",
                    sha256="a" * 64,
                    byte_len=9,
                    content_type="text/html",
                    content=b"<p>ok</p>",
                ),
            ],
            entries=[
                ManifestEntry(
                    path="report.html",
                    size=9,
                    reason="loaded",
                ),
            ],
            left_out=[],
            blobs={},
            title="Report",
        )

    monkeypatch.setattr(
        artifact_snapshot,
        "capture_workspace_monitor_snapshot",
        create_autospec(
            artifact_snapshot.capture_workspace_monitor_snapshot,
            side_effect=_capture_and_demote,
        ),
    )

    @asynccontextmanager
    async def _pub_conn():
        cur = create_autospec(AsyncCursor, instance=True)
        cur.__aenter__.return_value = cur
        cur.__aexit__.return_value = False
        cur.execute.return_value = None
        cur.fetchone.return_value = None
        conn = create_autospec(AsyncConnection, instance=True)
        conn.cursor.return_value = cur

        @asynccontextmanager
        async def _tx():
            yield

        conn.transaction.side_effect = _tx
        yield conn

    monkeypatch.setattr(monitor_db, "get_db_connection", _pub_conn)
    monkeypatch.setattr(
        artifacts_records_db,
        "lock_owned",
        create_autospec(artifacts_records_db.lock_owned, return_value=_make_artifact()),
    )
    cut_spy = create_autospec(artifacts_versions_db.cut_monitor_in_cur)
    monkeypatch.setattr(artifacts_versions_db, "cut_monitor_in_cur", cut_spy)

    with pytest.raises(HTTPException) as exc:
        await monitor_service.publish_workspace_monitor(
            PublishMonitorRequest(
                workspace_id=WS_ID,
                path="report.html",
                visibility="public",
            ),
            caller_id="adm-1",
        )
    assert exc.value.status_code == 403
    cut_spy.assert_not_awaited()


def test_r3_8_demo_copy_built_in_without_reading_web_locales(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    orig_read_text = Path.read_text
    orig_open = builtins.open

    def _guarded_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if "locales" in self.parts:
            raise FileNotFoundError(f"web/src/locales absent in container: {self}")
        return orig_read_text(self, *args, **kwargs)

    def _guarded_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if "locales" in str(file):
            raise FileNotFoundError(f"web/src/locales absent in container: {file}")
        return orig_open(file, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _guarded_read_text)
    monkeypatch.setattr(builtins, "open", _guarded_open)

    zh_html = build_live_demo_html(locale="zh-CN")
    en_html = build_live_demo_html(locale="en-US")
    assert "实时数据监控示例" in zh_html
    assert "Realtime Monitor Demo" in en_html
    assert "var(--color-bg-page)" in zh_html


# ---------------------------------------------------------------------------
# Round 4 fixes (Items 1-4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r4_1a_concurrent_owner_downgrade_to_own_blocks_pending_admin_title_patch_and_takeover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from src.server.database.artifacts import records as artifacts_records_db

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    db_lock = asyncio.Lock()
    state: dict[str, Any] = {
        "monitor_id": MONITOR_ID,
        "slug": "portfolio-beta",
        "artifact_id": ARTIFACT_ID,
        "owner_user_id": "int-owner",
        "title": "Original Title",
        "category": "portfolio",
        "sort_order": 0,
        "visibility": "public",
        "keep_updated": False,
        "published_seq": 2,
        "acl_revision": 1,
        "shared_at": NOW,
        "members": set(),
    }
    tiers = {"int-owner": "internal", "adm-b": "admin"}

    async def _tier(uid: str | None) -> str:
        return tiers.get(str(uid or ""), "external")

    monkeypatch.setattr(
        monitor_service,
        "get_user_tier",
        create_autospec(monitor_service.get_user_tier, side_effect=_tier),
    )
    monkeypatch.setattr(
        monitor_service,
        "db_get_workspace",
        create_autospec(
            monitor_service.db_get_workspace,
            return_value={"workspace_id": WS_ID, "user_id": "int-owner", "status": "running"},
        ),
    )

    def _read_state_monitor(_arg: Any = None) -> MonitorRecord:
        return _make_monitor(
            owner_user_id=state["owner_user_id"],
            title=state["title"],
            category=state["category"],
            sort_order=state["sort_order"],
            visibility=state["visibility"],
            keep_updated=state["keep_updated"],
            published_seq=state["published_seq"],
            acl_revision=state["acl_revision"],
            member_user_ids=tuple(sorted(state["members"])),
        )

    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(monitor_db.get_monitor_by_slug, side_effect=_read_state_monitor),
    )
    monkeypatch.setattr(
        monitor_db,
        "_select_monitor_in_cur",
        create_autospec(
            monitor_db._select_monitor_in_cur,
            side_effect=lambda _cur, _mid: _read_state_monitor(),
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "list_monitor_versions",
        create_autospec(monitor_db.list_monitor_versions, return_value=[]),
    )
    monkeypatch.setattr(
        artifacts_records_db,
        "lock_owned",
        create_autospec(
            artifacts_records_db.lock_owned,
            side_effect=lambda *_args, **_kwargs: _make_artifact(
                shared_at=state["shared_at"]
            ),
        ),
    )

    async def _unshare(*_args: Any, **_kwargs: Any) -> Artifact:
        state["shared_at"] = None
        return _make_artifact(shared_at=None)

    monkeypatch.setattr(
        artifacts_records_db,
        "unshare_in_cur",
        create_autospec(artifacts_records_db.unshare_in_cur, side_effect=_unshare),
    )

    @asynccontextmanager
    async def _locked_conn():
        async with db_lock:
            cur = create_autospec(AsyncCursor, instance=True)
            cur.__aenter__.return_value = cur
            cur.__aexit__.return_value = False
            cur._rows: list[Any] = []

            async def _exec(sql: str, params: Any = None) -> None:
                if "FROM dalton_monitors" in sql and "FOR UPDATE" in sql:
                    cur._rows = [
                        {
                            "title": state["title"],
                            "category": state["category"],
                            "sort_order": state["sort_order"],
                            "visibility": state["visibility"],
                            "keep_updated": state["keep_updated"],
                            "published_seq": state["published_seq"],
                            "owner_user_id": state["owner_user_id"],
                            "source_kind": "workspace",
                        }
                    ]
                elif "FROM dalton_monitor_members" in sql and sql.lstrip().startswith(
                    "SELECT"
                ):
                    cur._rows = [(m,) for m in sorted(state["members"])]
                elif "UPDATE dalton_monitors" in sql:
                    (
                        state["title"],
                        state["category"],
                        state["sort_order"],
                        state["visibility"],
                        state["keep_updated"],
                        state["owner_user_id"],
                        rev_delta,
                        _mid,
                    ) = params[:8]
                    state["acl_revision"] += int(rev_delta)
                    cur._rows = []
                elif "DELETE FROM dalton_monitor_members" in sql:
                    state["members"].clear()
                    cur._rows = []
                else:
                    cur._rows = []

            async def _execmany(_sql: str, rows: list[tuple[str, str]]) -> None:
                for _mid, uid in rows:
                    state["members"].add(uid)

            cur.execute.side_effect = _exec
            cur.executemany.side_effect = _execmany
            cur.fetchone.side_effect = lambda: cur._rows[0] if cur._rows else None
            cur.fetchall.side_effect = lambda: list(cur._rows)
            conn = create_autospec(AsyncConnection, instance=True)
            conn.cursor.return_value = cur

            @asynccontextmanager
            async def _tx():
                yield

            conn.transaction.side_effect = _tx
            yield conn

    monkeypatch.setattr(monitor_db, "get_db_connection", _locked_conn)

    admin_passed_prelock = asyncio.Event()
    owner_downgraded = asyncio.Event()
    orig_update_tx = monitor_db.update_monitor_settings_tx

    async def _interleaved_tx(*args: Any, **kwargs: Any) -> MonitorRecord:
        if kwargs.get("caller_id") == "adm-b":
            admin_passed_prelock.set()
            await owner_downgraded.wait()
        return await orig_update_tx(*args, **kwargs)

    monkeypatch.setattr(
        monitor_db,
        "update_monitor_settings_tx",
        create_autospec(orig_update_tx, side_effect=_interleaved_tx),
    )

    async def _admin_patch() -> HTTPException | None:
        try:
            await monitor_service.update_monitor_settings(
                "portfolio-beta",
                UpdateMonitorRequest(title="Admin Hijack Title"),
                caller_id="adm-b",
            )
            return None
        except HTTPException as exc:
            return exc

    async def _owner_downgrade() -> Any:
        await admin_passed_prelock.wait()
        try:
            return await monitor_service.update_monitor_settings(
                "portfolio-beta",
                UpdateMonitorRequest(visibility="own"),
                caller_id="int-owner",
            )
        finally:
            owner_downgraded.set()

    admin_exc, _ = await asyncio.gather(_admin_patch(), _owner_downgrade())
    assert admin_exc is not None
    assert admin_exc.status_code == 403
    assert state["visibility"] == "own"
    assert state["owner_user_id"] == "int-owner"
    assert state["title"] == "Original Title"


@pytest.mark.asyncio
async def test_r4_1b_concurrent_owner_downgrade_to_own_blocks_pending_admin_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from src.server.database.artifacts import records as artifacts_records_db

    db_lock = asyncio.Lock()
    state: dict[str, Any] = {
        "visibility": "public",
        "owner_user_id": "int-owner",
        "shared_at": NOW,
        "deleted": False,
        "acl_revision": 1,
    }
    tiers = {"int-owner": "internal", "adm-b": "admin"}

    async def _tier(uid: str | None) -> str:
        return tiers.get(str(uid or ""), "external")

    monkeypatch.setattr(
        monitor_service,
        "get_user_tier",
        create_autospec(monitor_service.get_user_tier, side_effect=_tier),
    )
    monkeypatch.setattr(
        monitor_service,
        "db_get_workspace",
        create_autospec(
            monitor_service.db_get_workspace,
            return_value={"workspace_id": WS_ID, "user_id": "int-owner", "status": "running"},
        ),
    )

    def _read_state_monitor(_arg: Any = None) -> MonitorRecord:
        return _make_monitor(
            owner_user_id=state["owner_user_id"],
            visibility=state["visibility"],
            acl_revision=state["acl_revision"],
        )

    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(monitor_db.get_monitor_by_slug, side_effect=_read_state_monitor),
    )
    monkeypatch.setattr(
        monitor_db,
        "_select_monitor_in_cur",
        create_autospec(
            monitor_db._select_monitor_in_cur,
            side_effect=lambda _cur, _mid: _read_state_monitor(),
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "list_monitor_versions",
        create_autospec(monitor_db.list_monitor_versions, return_value=[]),
    )
    monkeypatch.setattr(
        artifacts_records_db,
        "lock_owned",
        create_autospec(
            artifacts_records_db.lock_owned,
            side_effect=lambda *_args, **_kwargs: _make_artifact(
                shared_at=state["shared_at"]
            ),
        ),
    )

    unshare_spy = create_autospec(
        artifacts_records_db.unshare_in_cur,
        side_effect=lambda *_a, **_kw: state.update({"shared_at": None})
        or _make_artifact(shared_at=None),
    )
    monkeypatch.setattr(artifacts_records_db, "unshare_in_cur", unshare_spy)

    @asynccontextmanager
    async def _locked_conn():
        async with db_lock:
            cur = create_autospec(AsyncCursor, instance=True)
            cur.__aenter__.return_value = cur
            cur.__aexit__.return_value = False
            cur._rows: list[Any] = []
            cur.rowcount = 1

            async def _exec(sql: str, params: Any = None) -> None:
                if "FROM dalton_monitors" in sql and "FOR UPDATE" in sql:
                    cur._rows = [
                        {
                            "title": "Monitor",
                            "category": "portfolio",
                            "sort_order": 0,
                            "visibility": state["visibility"],
                            "keep_updated": False,
                            "published_seq": 2,
                            "owner_user_id": state["owner_user_id"],
                            "source_kind": "workspace",
                        }
                    ]
                elif "FROM dalton_monitor_members" in sql:
                    cur._rows = []
                elif "UPDATE dalton_monitors" in sql:
                    state["visibility"] = params[3]
                    state["acl_revision"] += int(params[6])
                    cur._rows = []
                elif "DELETE FROM dalton_monitors" in sql:
                    state["deleted"] = True
                    cur._rows = []
                else:
                    cur._rows = []

            async def _noop(*_a: Any, **_kw: Any) -> None:
                return None

            cur.execute.side_effect = _exec
            cur.executemany.side_effect = _noop
            cur.fetchone.side_effect = lambda: cur._rows[0] if cur._rows else None
            cur.fetchall.side_effect = lambda: list(cur._rows)
            conn = create_autospec(AsyncConnection, instance=True)
            conn.cursor.return_value = cur

            @asynccontextmanager
            async def _tx():
                yield

            conn.transaction.side_effect = _tx
            yield conn

    monkeypatch.setattr(monitor_db, "get_db_connection", _locked_conn)

    admin_passed_prelock = asyncio.Event()
    owner_downgraded = asyncio.Event()
    orig_delete_tx = monitor_db.delete_monitor_tx

    async def _interleaved_delete_tx(*args: Any, **kwargs: Any) -> bool:
        admin_passed_prelock.set()
        await owner_downgraded.wait()
        return await orig_delete_tx(*args, **kwargs)

    monkeypatch.setattr(
        monitor_db,
        "delete_monitor_tx",
        create_autospec(orig_delete_tx, side_effect=_interleaved_delete_tx),
    )

    async def _admin_delete() -> HTTPException | None:
        try:
            await monitor_service.delete_monitor_by_slug(
                "portfolio-beta", caller_id="adm-b"
            )
            return None
        except HTTPException as exc:
            return exc

    async def _owner_downgrade() -> Any:
        await admin_passed_prelock.wait()
        try:
            return await monitor_service.update_monitor_settings(
                "portfolio-beta",
                UpdateMonitorRequest(visibility="own"),
                caller_id="int-owner",
            )
        finally:
            owner_downgraded.set()

    admin_exc, _ = await asyncio.gather(_admin_delete(), _owner_downgrade())
    assert admin_exc is not None
    assert admin_exc.status_code == 403
    assert state["deleted"] is False
    assert state["visibility"] == "own"
    # unshare called once by owner's downgrade, NOT by rejected admin delete
    assert unshare_spy.await_count == 1


@pytest.mark.asyncio
async def test_r4_2_prune_monitor_versions_also_cleans_orphan_monitor_version_rows() -> None:
    executed_sql: list[tuple[str, Any]] = []
    cur = create_autospec(AsyncCursor, instance=True)
    cur.rowcount = 3
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = False

    async def _exec(sql: str, params: Any = None) -> None:
        executed_sql.append((sql, params))

    cur.execute.side_effect = _exec

    deleted = await monitor_db._prune_monitor_versions_in_cur(
        cur, ARTIFACT_ID, published_seq=110, keep_count=100
    )
    assert deleted == 3
    assert len(executed_sql) == 2
    assert "DELETE FROM artifact_versions" in executed_sql[0][0]
    assert "DELETE FROM dalton_monitor_versions" in executed_sql[1][0]
    assert "NOT EXISTS" in executed_sql[1][0]
    assert executed_sql[1][1] == (ARTIFACT_ID,)


@pytest.mark.asyncio
async def test_r4_3_host_dir_file_source_enforces_max_bytes_on_actual_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os as os_mod

    data_root = tmp_path / "openclaw-data"
    demo_dir = data_root / "research-outputs" / "monitor-demo"
    demo_dir.mkdir(parents=True)
    (demo_dir / "small.html").write_bytes(b"ok")
    # 15 bytes while MAX_HOST_FILE_BYTES is patched to 10; fstat spoofs 5 bytes
    (demo_dir / "growing.html").write_bytes(b"0123456789abcde")

    monkeypatch.setattr(data_job_registry, "MAX_HOST_FILE_BYTES", 10)
    real_fstat = os_mod.fstat

    class _FakeStat:
        def __init__(self, real_st: os_mod.stat_result) -> None:
            self.st_mode = real_st.st_mode
            self.st_size = 5

    def _spoofed_fstat(fd: int) -> Any:
        return _FakeStat(real_fstat(fd))

    monkeypatch.setattr(data_job_registry.os, "fstat", _spoofed_fstat)

    binding = data_job_registry.get_registered_monitor_job("monitor-live-demo")
    assert binding is not None
    with HostDirFileSource.open_registered(data_root, binding) as source:
        assert await source.read("small.html") == b"ok"
        assert await source.read("growing.html") is None


@pytest.mark.asyncio
async def test_r4_4_public_monitor_detail_strips_internal_fields_and_serves_entry_by_empty_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.server.database import artifacts as artifacts_db

    public_monitor = _make_monitor(
        owner_user_id="adm-1",
        owner_name="Chief Strategist",
        visibility="public",
        published_seq=4,
        entry_path="nested/index.html",
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(monitor_db.get_monitor_by_slug, return_value=public_monitor),
    )
    monkeypatch.setattr(
        monitor_db,
        "is_monitor_version_published",
        create_autospec(monitor_db.is_monitor_version_published, return_value=True),
    )

    async def _get_file(_aid: str, _seq: int, path: str) -> StoredVersionFile | None:
        if path == "nested/index.html":
            return StoredVersionFile(
                user_id="adm-1",
                sha256="d" * 64,
                byte_len=16,
                content_type="text/html",
                content=b"<h1>trimmed</h1>",
            )
        if path == "nested/styles.css":
            return StoredVersionFile(
                user_id="adm-1",
                sha256="e" * 64,
                byte_len=15,
                content_type="text/css",
                content=b"body{margin:0}",
            )
        return None

    monkeypatch.setattr(
        artifacts_db,
        "get_version_file",
        create_autospec(artifacts_db.get_version_file, side_effect=_get_file),
    )

    app = FastAPI()
    app.include_router(monitors_router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        detail_resp = await client.get("/api/v1/dalton/monitors/public/portfolio-beta")
        assert detail_resp.status_code == 200
        mon = detail_resp.json()["monitor"]
        for stripped_key in ("workspace_id", "artifact_id", "owner_user_id", "entry_path"):
            assert stripped_key not in mon
        assert mon["owner_name"] == "Chief Strategist"
        assert mon["title"] == "Portfolio Beta Monitor"
        assert mon["category"] == "portfolio"
        assert mon["updated_at"]

        entry_resp = await client.get("/api/v1/dalton/monitors/public/portfolio-beta/v/4/")
        assert entry_resp.status_code == 200
        assert b"<h1>trimmed</h1>" in entry_resp.content

        css_resp = await client.get(
            "/api/v1/dalton/monitors/public/portfolio-beta/v/4/styles.css"
        )
        assert css_resp.status_code == 200
        assert css_resp.content == b"body{margin:0}"



