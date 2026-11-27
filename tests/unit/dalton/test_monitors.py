"""Unit tests for Dalton Monitors (Round 1: Platform — Checklist 1–7)."""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
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
        auth_api,
        "require_thread_owner",
        create_autospec(auth_api.require_thread_owner, return_value=None),
    )
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
    monkeypatch.setattr(
        public_app,
        "get_thread_by_share_token",
        create_autospec(
            public_app.get_thread_by_share_token,
            return_value={**threads["th-int"], "owner_id": "int-u"},
        ),
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

    from src.server.services.shared_workspaces.access import Access

    shared_art = _make_artifact(shared_at=NOW, latest_seq=1, shared_seq=1)
    monkeypatch.setattr(
        artifact_records,
        "reached",
        create_autospec(
            artifact_records.reached, return_value=(shared_art, Access("adm-1"))
        ),
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
    from src.dalton.monitors import shared_documents as shared_doc_service

    monkeypatch.setattr(
        monitor_db,
        "list_workspace_auto_update_monitors",
        create_autospec(orig_list_auto, return_value=[]),
    )
    monkeypatch.setattr(
        shared_doc_service,
        "list_workspace_auto_update_shared_documents",
        create_autospec(
            shared_doc_service.list_workspace_auto_update_shared_documents,
            return_value=[],
        ),
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
                        "entry_path": "live-demo/index.html",
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
            return_value=_make_artifact(path="live-demo/index.html", latest_seq=0),
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
            return_value=_make_artifact(path="live-demo/index.html", latest_seq=0),
        ),
    )

    async def _cut_seq(_cur: Any, _locked: Artifact, **kwargs: Any) -> PublishResult:
        seq_counter["seq"] += 1
        s = seq_counter["seq"]
        return PublishResult(
            artifact=_make_artifact(path="live-demo/index.html", latest_seq=s),
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


def test_sentiment_dashboard_refresh_registers_six_company_panels() -> None:
    from src.dalton.monitors import data_job_registry

    bindings = data_job_registry.get_registered_monitor_bindings(
        data_job_registry.SENTIMENT_DASHBOARD_JOB_KEY
    )
    assert [b.slug for b in bindings] == [
        "market-overview",
        "us-market",
        "cn-market",
        "hk-market",
        "asia-leverage",
        "stocks",
    ]
    assert [b.category for b in bindings] == [
        "overview",
        "us",
        "cn",
        "hk",
        "asia",
        "stocks",
    ]
    for binding in bindings:
        assert binding.visibility == "company"
        assert binding.rel_dir == f"research-outputs/sentiment-dashboard/panels/{binding.slug}"
        assert binding.shared_rel_dir == "research-outputs/sentiment-dashboard/panels/_shared"
        assert binding.entry_file == "index.html"
        assert (
            data_job_registry.get_registered_monitor_binding_by_slug(
                data_job_registry.SENTIMENT_DASHBOARD_JOB_KEY, binding.slug
            )
            == binding
        )


@pytest.mark.asyncio
async def test_republish_sentiment_dashboard_publishes_six_isolated_panels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.dalton.monitors import data_job_registry
    from src.server.database import artifacts as artifacts_db

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    data_root = tmp_path / "openclaw-data"
    panels_dir = data_root / "research-outputs" / "sentiment-dashboard" / "panels"
    shared_dir = panels_dir / "_shared"
    shared_dir.mkdir(parents=True)
    (shared_dir / "panel.css").write_text(":root{--dir-up:#ff6b6b}", encoding="utf-8")
    (shared_dir / "panel.js").write_text("console.log('ok');", encoding="utf-8")

    expected_slugs = [
        b.slug
        for b in data_job_registry.get_registered_monitor_bindings(
            data_job_registry.SENTIMENT_DASHBOARD_JOB_KEY
        )
    ]
    for slug in expected_slugs:
        pdir = panels_dir / slug
        pdir.mkdir(parents=True)
        (pdir / "index.html").write_text(
            (
                '<link rel="stylesheet" href="../_shared/panel.css">'
                f'<h1>{slug}</h1>'
                '<script src="../_shared/panel.js"></script>'
            ),
            encoding="utf-8",
        )

    created_monitors: dict[str, MonitorRecord] = {}
    ensured_entry_paths: list[str] = []
    cut_bundles: dict[str, set[str]] = {}
    recorded_failures: list[str] = []

    monkeypatch.setattr(
        monitor_db,
        "ensure_company_monitor_workspace",
        create_autospec(monitor_db.ensure_company_monitor_workspace, return_value=WS_ID),
    )
    monkeypatch.setattr(
        monitor_db,
        "list_monitors_by_data_job",
        create_autospec(
            monitor_db.list_monitors_by_data_job,
            side_effect=lambda _job_key: list(created_monitors.values()),
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "has_processed_monitor_run",
        create_autospec(monitor_db.has_processed_monitor_run, return_value=False),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(
            monitor_db.get_monitor_by_slug,
            side_effect=lambda slug: created_monitors.get(slug),
        ),
    )

    def _ensure_artifact(_ws_id: str, path: str) -> Artifact:
        ensured_entry_paths.append(path)
        return _make_artifact(
            artifact_id=f"art-{len(ensured_entry_paths)}",
            path=path,
            latest_seq=0,
        )

    monkeypatch.setattr(
        artifacts_db,
        "ensure_artifact",
        create_autospec(artifacts_db.ensure_artifact, side_effect=_ensure_artifact),
    )

    def _upsert_monitor(
        *,
        slug: str,
        artifact_id: str,
        owner_user_id: str,
        title: str,
        category: str,
        sort_order: int,
        visibility: str,
        keep_updated: bool,
        source_kind: str,
        source_ref: str | None,
        member_user_ids: Any,
        bump_acl_revision: bool = False,
    ) -> MonitorRecord:
        assert owner_user_id == COMPANY_SYSTEM_USER_ID
        rec = _make_monitor(
            monitor_id=f"mon-{slug}",
            slug=slug,
            artifact_id=artifact_id,
            owner_user_id=owner_user_id,
            title=title,
            category=category,
            visibility=visibility,
            keep_updated=keep_updated,
            published_seq=0,
        )
        created_monitors[slug] = rec
        return rec

    monkeypatch.setattr(
        monitor_db,
        "upsert_monitor",
        create_autospec(monitor_db.upsert_monitor, side_effect=_upsert_monitor),
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

    async def _cut_version(
        *,
        monitor: MonitorRecord,
        owner_user_id: str,
        files: Any,
        title: str | None,
        read_basis: Any,
        origin: str,
        loaded_files: list[str] | None = None,
        artifact: Artifact | None = None,
        verify_public_authority: Any = None,
        run_id: str | None = None,
    ) -> tuple[MonitorRecord, PublishResult, bool]:
        del owner_user_id, title, read_basis, origin, loaded_files, verify_public_authority, run_id
        cut_bundles[monitor.slug] = {f.path for f in files}
        next_seq = monitor.published_seq + 1
        updated = _make_monitor(
            monitor_id=monitor.monitor_id,
            slug=monitor.slug,
            artifact_id=monitor.artifact_id,
            owner_user_id=monitor.owner_user_id,
            title=monitor.title,
            category=monitor.category,
            visibility=monitor.visibility,
            published_seq=next_seq,
        )
        created_monitors[monitor.slug] = updated
        pub = PublishResult(
            artifact=_make_artifact(
                artifact_id=monitor.artifact_id,
                path=artifact.path if artifact else f"{monitor.slug}/index.html",
                latest_seq=next_seq,
            ),
            seq=next_seq,
            cut=True,
            shared_now=False,
            changed=True,
        )
        return updated, pub, False

    monkeypatch.setattr(
        monitor_service,
        "cut_monitor_version",
        create_autospec(monitor_service.cut_monitor_version, side_effect=_cut_version),
    )
    monkeypatch.setattr(
        monitor_db,
        "record_monitor_publish_failure",
        create_autospec(
            monitor_db.record_monitor_publish_failure,
            side_effect=lambda monitor_id, **_k: recorded_failures.append(monitor_id),
        ),
    )

    res = await monitor_service.republish_data_job_monitor(
        job_key=data_job_registry.SENTIMENT_DASHBOARD_JOB_KEY,
        run_id="run-1",
        data_root=data_root,
    )
    assert len(res["monitors"]) == 6
    assert [m["slug"] for m in res["monitors"]] == expected_slugs
    assert ensured_entry_paths == [f"{slug}/index.html" for slug in expected_slugs]

    for slug in expected_slugs:
        bundle_paths = cut_bundles[slug]
        assert bundle_paths == {
            f"{slug}/index.html",
            "_shared/panel.css",
            "_shared/panel.js",
        }

    # Failure run records failure on existing monitors without cutting a new version
    await monitor_service.maybe_publish_data_job_monitor(
        job_key=data_job_registry.SENTIMENT_DASHBOARD_JOB_KEY,
        run_id="run-fail",
        terminal_status="failed",
        failure_reason="upstream failed",
        data_root=data_root,
    )
    assert len(recorded_failures) == 6
    assert all(created_monitors[s].published_seq == 1 for s in expected_slugs)


@pytest.mark.asyncio
async def test_republish_data_job_slug_conflict_fails_without_renaming_and_logs_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from src.dalton.monitors import data_job_registry
    from src.server.database import artifacts as artifacts_db

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    data_root = tmp_path / "openclaw-data"
    panels_dir = data_root / "research-outputs" / "sentiment-dashboard" / "panels"
    shared_dir = panels_dir / "_shared"
    shared_dir.mkdir(parents=True)
    (shared_dir / "panel.css").write_text(":root{}", encoding="utf-8")
    (shared_dir / "panel.js").write_text("// js", encoding="utf-8")
    for b in data_job_registry.get_registered_monitor_bindings(
        data_job_registry.SENTIMENT_DASHBOARD_JOB_KEY
    ):
        pdir = panels_dir / b.slug
        pdir.mkdir(parents=True)
        (pdir / "index.html").write_text(f"<h1>{b.slug}</h1>", encoding="utf-8")

    # Existing unrelated workspace monitor already owns 'us-market'
    foreign_owner_monitor = _make_monitor(
        monitor_id="mon-foreign-us",
        slug="us-market",
        source_kind="workspace",
        source_ref=f"{WS_ID}:other.html",
    )
    monitors_by_slug: dict[str, MonitorRecord] = {"us-market": foreign_owner_monitor}
    upserted_slugs: list[str] = []

    monkeypatch.setattr(
        monitor_db,
        "ensure_company_monitor_workspace",
        create_autospec(monitor_db.ensure_company_monitor_workspace, return_value=WS_ID),
    )
    monkeypatch.setattr(
        monitor_db,
        "list_monitors_by_data_job",
        create_autospec(monitor_db.list_monitors_by_data_job, return_value=[]),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(
            monitor_db.get_monitor_by_slug,
            side_effect=lambda s: monitors_by_slug.get(s),
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "pick_available_slug",
        create_autospec(
            monitor_db.pick_available_slug,
            side_effect=AssertionError("must not pick suffixed slug on conflict"),
        ),
    )
    monkeypatch.setattr(
        artifacts_db,
        "ensure_artifact",
        create_autospec(
            artifacts_db.ensure_artifact,
            side_effect=lambda _ws, p: _make_artifact(artifact_id=f"art-{p}", path=p, latest_seq=0),
        ),
    )

    def _upsert_monitor(**kwargs: Any) -> MonitorRecord:
        slug = kwargs["slug"]
        upserted_slugs.append(slug)
        rec = _make_monitor(
            monitor_id=f"mon-{slug}",
            slug=slug,
            artifact_id=kwargs["artifact_id"],
            owner_user_id=kwargs["owner_user_id"],
            title=kwargs["title"],
            category=kwargs["category"],
            visibility=kwargs["visibility"],
            published_seq=0,
        )
        monitors_by_slug[slug] = rec
        return rec

    monkeypatch.setattr(
        monitor_db,
        "upsert_monitor",
        create_autospec(monitor_db.upsert_monitor, side_effect=_upsert_monitor),
    )
    monkeypatch.setattr(
        monitor_service.artifact_snapshot,
        "get_vault_secrets_for_redaction",
        create_autospec(monitor_service.artifact_snapshot.get_vault_secrets_for_redaction, return_value={}),
    )
    monkeypatch.setattr(
        monitor_service.artifact_snapshot,
        "store_unregistered_blobs",
        create_autospec(monitor_service.artifact_snapshot.store_unregistered_blobs),
    )

    async def _cut(**kwargs: Any) -> tuple[MonitorRecord, PublishResult, bool]:
        m = kwargs["monitor"]
        updated = _make_monitor(monitor_id=m.monitor_id, slug=m.slug, artifact_id=m.artifact_id, published_seq=1)
        return updated, PublishResult(artifact=_make_artifact(latest_seq=1), seq=1, cut=True, shared_now=False, changed=True), False

    monkeypatch.setattr(
        monitor_service,
        "cut_monitor_version",
        create_autospec(monitor_service.cut_monitor_version, side_effect=_cut),
    )

    with caplog.at_level("ERROR"), pytest.raises(HTTPException) as exc_info:
        await monitor_service.republish_data_job_monitor(
            job_key=data_job_registry.SENTIMENT_DASHBOARD_JOB_KEY,
            run_id="run-conflict",
            data_root=data_root,
        )

    assert exc_info.value.status_code == 409
    assert "禁止自动改名" in exc_info.value.detail
    assert any("slug collision" in r.message for r in caplog.records)
    assert "us-market" not in upserted_slugs
    assert all(not s.startswith("us-market-") for s in upserted_slugs)
    # Non-conflicting siblings still published normally
    assert upserted_slugs == ["market-overview", "cn-market", "hk-market", "asia-leverage", "stocks"]


@pytest.mark.asyncio
async def test_republish_data_job_first_publish_failure_leaves_no_orphan_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.dalton.monitors import data_job_registry
    from src.server.database import artifacts as artifacts_db

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    data_root = tmp_path / "openclaw-data"
    # Note: market-overview/index.html is intentionally absent on first publish!
    single_binding = data_job_registry.get_registered_monitor_binding_by_slug(
        data_job_registry.SENTIMENT_DASHBOARD_JOB_KEY, "market-overview"
    )
    assert single_binding is not None

    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(monitor_db.get_monitor_by_slug, return_value=None),
    )
    ensure_mock = create_autospec(
        artifacts_db.ensure_artifact,
        side_effect=AssertionError("must not create artifact when snapshot fails"),
    )
    upsert_mock = create_autospec(
        monitor_db.upsert_monitor,
        side_effect=AssertionError("must not create orphan monitor row when snapshot fails"),
    )
    monkeypatch.setattr(artifacts_db, "ensure_artifact", ensure_mock)
    monkeypatch.setattr(monitor_db, "upsert_monitor", upsert_mock)
    monkeypatch.setattr(
        monitor_service.artifact_snapshot,
        "get_vault_secrets_for_redaction",
        create_autospec(monitor_service.artifact_snapshot.get_vault_secrets_for_redaction, return_value={}),
    )

    with pytest.raises(HTTPException):
        await monitor_service._republish_single_binding(
            single_binding,
            clean_run_id="run-missing",
            data_root=data_root,
            existing=None,
            is_multi=True,
        )

    assert ensure_mock.call_count == 0
    assert upsert_mock.call_count == 0


@pytest.mark.asyncio
async def test_write_monitor_data_internal_requires_exact_slug_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.monitors import data_job_registry

    mismatched_slug_monitor = _make_monitor(
        slug="us-market-2",
        source_kind="data_job",
        source_ref=data_job_registry.SENTIMENT_DASHBOARD_JOB_KEY,
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(monitor_db.get_monitor_by_slug, return_value=mismatched_slug_monitor),
    )
    # Give binding[0] an explicit data_keys allowlist to prove there is NO fallback to bindings[0]
    first_binding = data_job_registry.get_registered_monitor_bindings(
        data_job_registry.SENTIMENT_DASHBOARD_JOB_KEY
    )[0]
    def _lookup_binding(job_key: str, slug: str) -> MonitorDataJobBinding | None:
        del job_key
        return first_binding if slug == first_binding.slug else None

    monkeypatch.setattr(
        monitor_service,
        "get_registered_monitor_binding_by_slug",
        create_autospec(
            data_job_registry.get_registered_monitor_binding_by_slug,
            side_effect=_lookup_binding,
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await monitor_service.write_monitor_data_internal(
            "us-market-2",
            "live",
            {"price": 1},
        )
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_all_registered_data_job_monitors_coexist_without_overwriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.dalton.monitors import data_job_registry
    from src.server.database import artifacts as artifacts_db

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    data_root = tmp_path / "openclaw-data"

    # Populate disk outputs for every registered data-job monitor binding.
    all_jobs = (
        "monitor-live-demo",
        "hk-momentum",
        "cn-intraday",
        "pm-market-state",
        "richard-relative-dashboard-refresh",
        data_job_registry.SENTIMENT_DASHBOARD_JOB_KEY,
    )
    shared_dir = data_root / data_job_registry.SENTIMENT_SHARED_REL_DIR
    shared_dir.mkdir(parents=True)
    (shared_dir / "panel.css").write_text(":root{}", encoding="utf-8")

    expected_bindings: list[MonitorDataJobBinding] = []
    for jk in all_jobs:
        for b in data_job_registry.get_registered_monitor_bindings(jk):
            expected_bindings.append(b)
            pdir = data_root / b.rel_dir
            pdir.mkdir(parents=True, exist_ok=True)
            link_tag = '<link rel="stylesheet" href="../_shared/panel.css">' if b.shared_rel_dir else ""
            (pdir / b.entry_file).write_text(
                f"<!doctype html><html><head><title>{b.title}</title>{link_tag}</head><body>{b.slug}</body></html>",
                encoding="utf-8",
            )

    # In-memory DB double enforcing unique (workspace_id, path) on share_links
    # and unique artifact_id / slug on dalton_monitors (mirrors real Postgres constraints).
    artifacts_by_key: dict[tuple[str, str], Artifact] = {}
    monitors_by_id: dict[str, MonitorRecord] = {}
    monitors_by_artifact: dict[str, MonitorRecord] = {}
    monitors_by_slug: dict[str, MonitorRecord] = {}
    processed_runs: set[tuple[str, str]] = set()
    cut_files: dict[str, set[str]] = {}

    monkeypatch.setattr(
        monitor_db,
        "ensure_company_monitor_workspace",
        create_autospec(monitor_db.ensure_company_monitor_workspace, return_value=WS_ID),
    )

    def _ensure_artifact(ws_id: str, path: str) -> Artifact:
        key = (ws_id, path)
        if key not in artifacts_by_key:
            artifacts_by_key[key] = _make_artifact(
                artifact_id=f"art-{len(artifacts_by_key) + 1}",
                workspace_id=ws_id,
                path=path,
                latest_seq=0,
            )
        return artifacts_by_key[key]

    monkeypatch.setattr(
        artifacts_db,
        "ensure_artifact",
        create_autospec(artifacts_db.ensure_artifact, side_effect=_ensure_artifact),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_data_job",
        create_autospec(
            monitor_db.get_monitor_by_data_job,
            side_effect=lambda jk: next(
                (m for m in monitors_by_id.values() if m.source_kind == "data_job" and m.source_ref == jk),
                None,
            ),
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "list_monitors_by_data_job",
        create_autospec(
            monitor_db.list_monitors_by_data_job,
            side_effect=lambda jk: [
                m for m in monitors_by_id.values() if m.source_kind == "data_job" and m.source_ref == jk
            ],
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(
            monitor_db.get_monitor_by_slug,
            side_effect=lambda slug: monitors_by_slug.get(slug),
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "has_processed_monitor_run",
        create_autospec(
            monitor_db.has_processed_monitor_run,
            side_effect=lambda mid, rid: (mid, rid) in processed_runs,
        ),
    )

    def _upsert_monitor(
        *,
        slug: str,
        artifact_id: str,
        owner_user_id: str,
        title: str,
        category: str,
        sort_order: int,
        visibility: str,
        keep_updated: bool,
        source_kind: str,
        source_ref: str | None,
        member_user_ids: Any,
        bump_acl_revision: bool = False,
    ) -> MonitorRecord:
        del member_user_ids, bump_acl_revision
        existing = monitors_by_artifact.get(artifact_id)
        if existing is not None:
            raise AssertionError(
                f"Artifact {artifact_id} collided: existing={existing.slug}/{existing.source_ref}, new={slug}/{source_ref}"
            )
        entry_path = next(a.path for a in artifacts_by_key.values() if a.artifact_id == artifact_id)
        rec = _make_monitor(
            monitor_id=f"mon-{slug}",
            slug=slug,
            artifact_id=artifact_id,
            owner_user_id=owner_user_id,
            title=title,
            category=category,
            sort_order=sort_order,
            visibility=visibility,
            keep_updated=keep_updated,
            source_kind=source_kind,
            source_ref=source_ref,
            entry_path=entry_path,
            published_seq=0,
        )
        monitors_by_id[rec.monitor_id] = rec
        monitors_by_artifact[artifact_id] = rec
        monitors_by_slug[slug] = rec
        return rec

    monkeypatch.setattr(
        monitor_db,
        "upsert_monitor",
        create_autospec(monitor_db.upsert_monitor, side_effect=_upsert_monitor),
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

    async def _cut_version(
        *,
        monitor: MonitorRecord,
        owner_user_id: str,
        files: Any,
        title: str | None,
        read_basis: Any,
        origin: str,
        loaded_files: list[str] | None = None,
        artifact: Artifact | None = None,
        verify_public_authority: Any = None,
        run_id: str | None = None,
    ) -> tuple[MonitorRecord, PublishResult, bool]:
        del owner_user_id, title, read_basis, origin, loaded_files, verify_public_authority
        assert artifact is not None
        assert monitor.artifact_id == artifact.artifact_id
        if run_id:
            processed_runs.add((monitor.monitor_id, run_id))
        cut_files[monitor.slug] = {f.path for f in files}
        next_seq = monitor.published_seq + 1
        updated = _make_monitor(
            **{
                **monitor.__dict__,
                "published_seq": next_seq,
            }
        )
        monitors_by_id[updated.monitor_id] = updated
        monitors_by_artifact[updated.artifact_id] = updated
        monitors_by_slug[updated.slug] = updated
        return (
            updated,
            PublishResult(
                artifact=_make_artifact(
                    artifact_id=updated.artifact_id,
                    path=artifact.path,
                    latest_seq=next_seq,
                ),
                seq=next_seq,
                cut=True,
                shared_now=False,
                changed=True,
            ),
            False,
        )

    monkeypatch.setattr(
        monitor_service,
        "cut_monitor_version",
        create_autospec(monitor_service.cut_monitor_version, side_effect=_cut_version),
    )

    for jk in all_jobs:
        await monitor_service.republish_data_job_monitor(
            job_key=jk, run_id=f"run-1-{jk}", data_root=data_root
        )

    assert len(monitors_by_slug) == len(expected_bindings) == 12
    assert len(artifacts_by_key) == 12
    for b in expected_bindings:
        rec = monitors_by_slug[b.slug]
        assert rec.source_ref == b.job_key
        assert rec.entry_path == f"{b.slug}/{b.entry_file}"
        assert rec.published_seq == 1
        assert f"{b.slug}/{b.entry_file}" in cut_files[b.slug]

    # Re-running single-binding jobs updates each monitor independently with zero cross-overwrite.
    for jk in ("hk-momentum", "cn-intraday", "richard-relative-dashboard-refresh"):
        await monitor_service.republish_data_job_monitor(
            job_key=jk, run_id=f"run-2-{jk}", data_root=data_root
        )
    assert monitors_by_slug["hk-momentum"].published_seq == 2
    assert monitors_by_slug["cn-intraday"].published_seq == 2
    assert monitors_by_slug["relative-strength"].published_seq == 2
    assert monitors_by_slug["live-demo"].published_seq == 1
    assert monitors_by_slug["pm-market-state"].published_seq == 1
    assert monitors_by_slug["cn-sector-rotation"].published_seq == 1


@pytest.mark.asyncio
async def test_relative_strength_publishes_only_page_and_dependencies_with_220_portfolio_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.server.database import artifacts as artifacts_db

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    data_root = tmp_path / "openclaw-data"
    kweb_dir = data_root / "research-outputs" / "kweb-dashboard"
    assets_dir = kweb_dir / "assets"
    portfolio_dir = kweb_dir / "portfolio"
    assets_dir.mkdir(parents=True)
    portfolio_dir.mkdir(parents=True)

    (assets_dir / "theme.css").write_text("body{color:#fff}", encoding="utf-8")
    (kweb_dir / "index.html").write_text(
        '<!doctype html><html><head><link rel="stylesheet" href="assets/theme.css"></head>'
        "<body><h1>Relative Strength</h1></body></html>",
        encoding="utf-8",
    )
    # Create 220 daily markdown reports (> MAX_FILES=200) inside portfolio/
    for idx in range(220):
        (portfolio_dir / f"2026-01-{idx:03d}.md").write_text(
            f"# Portfolio Report {idx}\n", encoding="utf-8"
        )

    captured_paths: list[str] = []
    artifact = _make_artifact(
        artifact_id="art-rel",
        path="relative-strength/index.html",
        latest_seq=0,
    )
    monkeypatch.setattr(
        monitor_db,
        "ensure_company_monitor_workspace",
        create_autospec(monitor_db.ensure_company_monitor_workspace, return_value=WS_ID),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_data_job",
        create_autospec(monitor_db.get_monitor_by_data_job, return_value=None),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(monitor_db.get_monitor_by_slug, return_value=None),
    )
    monkeypatch.setattr(
        artifacts_db,
        "ensure_artifact",
        create_autospec(artifacts_db.ensure_artifact, return_value=artifact),
    )
    monkeypatch.setattr(
        monitor_db,
        "upsert_monitor",
        create_autospec(
            monitor_db.upsert_monitor,
            return_value=_make_monitor(
                monitor_id="mon-rel",
                slug="relative-strength",
                artifact_id="art-rel",
                source_kind="data_job",
                source_ref="richard-relative-dashboard-refresh",
                entry_path="relative-strength/index.html",
                published_seq=0,
            ),
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

    async def _cut_version(**kwargs: Any) -> tuple[MonitorRecord, PublishResult, bool]:
        captured_paths.extend(f.path for f in kwargs["files"])
        mon = kwargs["monitor"]
        updated = _make_monitor(**{**mon.__dict__, "published_seq": 1})
        return (
            updated,
            PublishResult(
                artifact=_make_artifact(
                    artifact_id="art-rel",
                    path="relative-strength/index.html",
                    latest_seq=1,
                ),
                seq=1,
                cut=True,
                shared_now=False,
                changed=True,
            ),
            False,
        )

    monkeypatch.setattr(
        monitor_service,
        "cut_monitor_version",
        create_autospec(monitor_service.cut_monitor_version, side_effect=_cut_version),
    )

    res = await monitor_service.republish_data_job_monitor(
        job_key="richard-relative-dashboard-refresh",
        run_id="run-kweb-220",
        data_root=data_root,
    )
    assert res["slug"] == "relative-strength"
    assert res["published_seq"] == 1
    assert set(captured_paths) == {
        "relative-strength/index.html",
        "relative-strength/assets/theme.css",
    }
    assert len(list(portfolio_dir.glob("*.md"))) == 220


@pytest.mark.asyncio
async def test_check_monitor_conflicts_read_only_inspector() -> None:
    from scripts.dalton import check_monitor_conflicts as cmc
    from src.dalton.monitors import data_job_registry

    executed_sql: list[str] = []

    def _make_fake_conn(
        monitor_rows: list[dict[str, Any]],
        workspace_links: list[dict[str, Any]],
    ) -> Any:
        executed_sql.clear()
        cur = create_autospec(AsyncCursor, instance=True)
        cur.__aenter__.return_value = cur
        cur.__aexit__.return_value = False
        state = {"rows": []}

        async def _exec(sql: str, params: Any = None) -> None:
            del params
            executed_sql.append(sql.strip())
            if "FROM dalton_monitors m" in sql:
                state["rows"] = list(monitor_rows)
            elif "FROM share_links l" in sql:
                state["rows"] = list(workspace_links)
            else:
                state["rows"] = []

        cur.execute.side_effect = _exec
        cur.fetchall.side_effect = lambda: list(state["rows"])
        conn = create_autospec(AsyncConnection, instance=True)
        conn.cursor.return_value = cur

        @asynccontextmanager
        async def _tx():
            yield

        conn.transaction.side_effect = _tx
        return conn

    # 1) Clean state: every registered binding has matching source_ref & <slug>/<entry_file>
    clean_monitors = []
    clean_links = []
    for idx, jk in enumerate(data_job_registry.MONITOR_DATA_JOB_REGISTRY):
        for b in data_job_registry.get_registered_monitor_bindings(jk):
            aid = f"art-{idx}-{b.slug}"
            clean_monitors.append(
                {
                    "monitor_id": f"mon-{b.slug}",
                    "slug": b.slug,
                    "source_kind": "data_job",
                    "source_ref": b.job_key,
                    "title": b.title,
                    "category": b.category,
                    "published_seq": 1,
                    "artifact_id": aid,
                    "entry_path": f"{b.slug}/{b.entry_file}",
                }
            )
            clean_links.append(
                {"artifact_id": aid, "path": f"{b.slug}/{b.entry_file}", "latest_seq": 1}
            )

    clean_report = await cmc.inspect_monitor_conflicts(
        conn=_make_fake_conn(clean_monitors, clean_links)
    )
    assert clean_report["has_conflicts"] is False
    assert clean_report["ok"] is True
    assert executed_sql[0] == "SET TRANSACTION READ ONLY"
    assert all(
        not s.upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in executed_sql
    )

    # 2) Polluted legacy state: hk-momentum overwritten by cn-intraday on index.html
    dirty_report = await cmc.inspect_monitor_conflicts(
        conn=_make_fake_conn(
            [
                {
                    "monitor_id": "mon-hk",
                    "slug": "hk-momentum",
                    "source_kind": "data_job",
                    "source_ref": "cn-intraday",
                    "title": "A股盘中监测",
                    "category": "cn",
                    "published_seq": 4,
                    "artifact_id": "art-shared",
                    "entry_path": "index.html",
                }
            ],
            [
                {
                    "artifact_id": "art-shared",
                    "path": "index.html",
                    "latest_seq": 4,
                    "is_referenced": True,
                }
            ],
        )
    )
    assert dirty_report["has_conflicts"] is True
    assert len(dirty_report["source_ref_mismatches"]) == 1
    assert dirty_report["source_ref_mismatches"][0]["expected_source_ref"] == "hk-momentum"
    assert len(dirty_report["legacy_entry_paths"]) == 1
    assert len(dirty_report["legacy_shared_links"]) == 1
    assert dirty_report["historical_shared_links"] == []
    assert any("UPDATE dalton_monitors" in s for s in dirty_report["remediation_sql"])
    assert any("重跑该数据任务即可自动迁移" in s for s in dirty_report["remediation_sql"])
    assert all("DELETE FROM" not in s for s in dirty_report["remediation_sql"])

    # 3) Migrated state: all monitors point to <slug>/index.html; unreferenced index.html is info-only
    migrated_links = list(clean_links) + [
        {
            "artifact_id": "art-legacy-unreferenced",
            "path": "index.html",
            "latest_seq": 4,
            "is_referenced": False,
        }
    ]
    migrated_report = await cmc.inspect_monitor_conflicts(
        conn=_make_fake_conn(clean_monitors, migrated_links)
    )
    assert migrated_report["has_conflicts"] is False
    assert migrated_report["ok"] is True
    assert migrated_report["legacy_shared_links"] == []
    assert len(migrated_report["historical_shared_links"]) == 1
    assert migrated_report["historical_shared_links"][0]["artifact_id"] == "art-legacy-unreferenced"
    assert migrated_report["remediation_sql"] == []


def _setup_legacy_migration_db(
    monkeypatch: pytest.MonkeyPatch,
    *,
    share_links: dict[str, dict[str, Any]],
    artifact_versions: dict[tuple[str, int], dict[str, Any]],
    artifact_version_files: dict[tuple[str, int, str], dict[str, Any]],
    monitors_table: dict[str, dict[str, Any]],
    monitor_members: dict[str, set[str]],
    monitor_versions: dict[str, set[int]],
    monitor_data: dict[tuple[str, str], dict[str, Any]],
    publish_runs: set[tuple[str, str]],
) -> list[str]:
    import copy
    from psycopg.errors import UniqueViolation
    from src.server.database.artifacts import records as artifacts_records_db
    from src.server.database.artifacts import versions as artifacts_versions_db

    executed_sql: list[str] = []

    def _artifact_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "artifact_id": row["artifact_id"],
            "code": row["code"],
            "workspace_id": row["workspace_id"],
            "path": row["path"],
            "created_thread_id": None,
            "bound_thread_id": None,
            "keep_updated": False,
            "latest_seq": row["latest_seq"],
            "shared_at": row["shared_at"],
            "files": row["files"],
            "revision": row["revision"],
            "created_at": NOW,
            "legacy_refused": None,
            "loaded_files": [],
            "left_out_files": [],
            "kept_at": None,
            "shared_seq": row.get("shared_seq", 0),
        }

    def _monitor_join_row(row: dict[str, Any]) -> dict[str, Any]:
        link = share_links[row["artifact_id"]]
        data_as_of = max(
            (v["as_of"] for (mid, _k), v in monitor_data.items() if mid == row["monitor_id"] and "as_of" in v),
            default=None,
        )
        return {
            **row,
            "artifact_code": link["code"],
            "workspace_id": link["workspace_id"],
            "entry_path": link["path"],
            "owner_name": None,
            "owner_email": None,
            "data_as_of": data_as_of,
            "member_user_ids": sorted(monitor_members.get(row["monitor_id"], set())),
        }

    def _find_link_by_path(ws_id: str, path: str) -> dict[str, Any] | None:
        return next(
            (
                r
                for r in share_links.values()
                if r["workspace_id"] == ws_id and r["path"] == path
            ),
            None,
        )

    @asynccontextmanager
    async def _fake_db_conn():
        cur = create_autospec(AsyncCursor, instance=True)
        cur.__aenter__.return_value = cur
        cur.__aexit__.return_value = False
        cur._rows = []

        async def _exec(sql: str, params: Any = None) -> None:
            executed_sql.append(" ".join(sql.split()))
            upper = " ".join(sql.upper().split())
            cur.rowcount = 0

            if upper.startswith("DELETE FROM ARTIFACT_VERSIONS"):
                cur._rows = []
                return
            if upper.startswith("DELETE FROM DALTON_MONITOR_MEMBERS"):
                monitor_members.pop(params[0], None)
                cur._rows = []
                return
            if upper.startswith("DELETE "):
                raise AssertionError(f"Unexpected DELETE during migration: {sql}")

            # 1) _MONITOR_SELECT queries
            if "FROM DALTON_MONITORS M JOIN SHARE_LINKS L" in upper:
                if "WHERE M.SLUG = %S" in upper:
                    row = next((r for r in monitors_table.values() if r["slug"] == params[0]), None)
                elif "WHERE M.MONITOR_ID = %S::UUID" in upper:
                    row = monitors_table.get(params[0])
                elif "WHERE M.SOURCE_KIND = 'DATA_JOB' AND M.SOURCE_REF = %S" in upper:
                    row = next(
                        (
                            r
                            for r in monitors_table.values()
                            if r["source_kind"] == "data_job" and r["source_ref"] == params[0]
                        ),
                        None,
                    )
                else:
                    row = None
                cur._rows = [_monitor_join_row(row)] if row else []
                return

            # 2) share_links operations
            if "FROM SHARE_LINKS L" in upper and "FOR UPDATE OF L" in upper:
                aid = params["artifact_id"]
                link = share_links.get(aid)
                cur._rows = [_artifact_row(link)] if link else []
                return
            if "FROM SHARE_LINKS L WHERE L.WORKSPACE_ID = %S AND L.KIND = 'FILE' AND L.PATH = %S" in upper:
                link = _find_link_by_path(params[0], params[1])
                cur._rows = [_artifact_row(link)] if link else []
                return
            if "INSERT INTO SHARE_LINKS AS L (CODE, WORKSPACE_ID, KIND, PATH)" in upper:
                code, ws_id, path = params
                existing = _find_link_by_path(ws_id, path)
                if existing is not None:
                    cur._rows = []
                else:
                    new_aid = f"61000000-0000-4000-8000-{len(share_links) + 1:012d}"
                    share_links[new_aid] = {
                        "artifact_id": new_aid,
                        "code": code,
                        "workspace_id": ws_id,
                        "path": path,
                        "latest_seq": 0,
                        "shared_at": None,
                        "files": None,
                        "revision": 1,
                        "shared_seq": 0,
                    }
                    cur._rows = [_artifact_row(share_links[new_aid])]
                return
            if "UPDATE SHARE_LINKS L SET PATH = %(NEW_PATH)S" in upper:
                aid = params["artifact_id"]
                new_path = params["new_path"]
                link = share_links.get(aid)
                if link is None:
                    cur._rows = []
                    return
                conflict = _find_link_by_path(link["workspace_id"], new_path)
                if conflict is not None and conflict["artifact_id"] != aid:
                    raise UniqueViolation("duplicate key value violates unique constraint")
                link["path"] = new_path
                cur._rows = [_artifact_row(link)]
                return
            if "UPDATE SHARE_LINKS L SET LATEST_SEQ = %(SEQ)S" in upper:
                aid = params["artifact_id"]
                link = share_links[aid]
                link["latest_seq"] = params["seq"]
                link["shared_seq"] = params["shared_seq"]
                if "FILES = %(FILES)S::TEXT[]" in upper:
                    link["files"] = list(params["files"])
                if "SHARED_AT = COALESCE(L.SHARED_AT, NOW())" in upper and link["shared_at"] is None:
                    link["shared_at"] = NOW
                    link["revision"] += 1
                cur._rows = [_artifact_row(link)]
                return

            # 3) dalton_monitors locks & mutations
            if "FROM DALTON_MONITORS WHERE MONITOR_ID = %S::UUID FOR UPDATE" in upper:
                row = monitors_table.get(params[0])
                cur._rows = [dict(row)] if row else []
                return
            if "FROM DALTON_MONITORS WHERE ARTIFACT_ID = %S::UUID FOR UPDATE" in upper:
                row = next((r for r in monitors_table.values() if r["artifact_id"] == params[0]), None)
                cur._rows = [dict(row)] if row else []
                return
            if "INSERT INTO DALTON_MONITORS (" in upper:
                slug, aid, owner_uid, title, cat, sort_order, vis, keep_upd, src_kind, src_ref = params
                mid = f"60000000-0000-4000-8000-{len(monitors_table) + 1:012d}"
                monitors_table[mid] = {
                    "monitor_id": mid,
                    "slug": slug,
                    "artifact_id": aid,
                    "owner_user_id": owner_uid,
                    "title": title,
                    "category": cat,
                    "sort_order": sort_order,
                    "visibility": vis,
                    "published_seq": 0,
                    "keep_updated": keep_upd,
                    "source_kind": src_kind,
                    "source_ref": src_ref,
                    "acl_revision": 1,
                    "last_publish_run_id": None,
                    "last_publish_error": None,
                    "last_publish_failed_at": None,
                    "created_at": NOW,
                    "updated_at": NOW,
                }
                cur._rows = [{"monitor_id": mid}]
                return
            if "UPDATE DALTON_MONITORS SET SOURCE_REF = %S" in upper:
                src_ref, mid = params
                monitors_table[mid]["source_ref"] = src_ref
                cur._rows = []
                return
            if "UPDATE DALTON_MONITORS SET PUBLISHED_SEQ = %S" in upper:
                seq, owner_uid, run_id, mid = params
                monitors_table[mid]["published_seq"] = seq
                monitors_table[mid]["owner_user_id"] = owner_uid
                if run_id is not None:
                    monitors_table[mid]["last_publish_run_id"] = run_id
                monitors_table[mid]["last_publish_error"] = None
                monitors_table[mid]["last_publish_failed_at"] = None
                cur._rows = []
                return
            if "UPDATE DALTON_MONITORS SET LAST_PUBLISH_ERROR = %S" in upper:
                err_msg, mid = params
                monitors_table[mid]["last_publish_error"] = err_msg
                monitors_table[mid]["last_publish_failed_at"] = NOW
                cur._rows = []
                return

            # 4) publish runs & versions
            if "INSERT INTO DALTON_MONITOR_PUBLISH_RUNS" in upper:
                key = (params[0], params[1])
                if key in publish_runs:
                    cur._rows = []
                else:
                    publish_runs.add(key)
                    cur._rows = [{"monitor_id": params[0]}]
                return
            if "SELECT 1 FROM DALTON_MONITOR_PUBLISH_RUNS" in upper:
                cur._rows = [(1,)] if (params[0], params[1]) in publish_runs else []
                return
            if "UPDATE DALTON_MONITOR_PUBLISH_RUNS" in upper:
                cur._rows = []
                return
            if "SELECT PATH, SHA256 FROM ARTIFACT_VERSION_FILES" in upper:
                aid, seq = params
                cur._rows = [
                    {"path": fpath, "sha256": frow["sha256"]}
                    for (faid, fseq, fpath), frow in artifact_version_files.items()
                    if faid == aid and fseq == seq
                ]
                return
            if "INSERT INTO ARTIFACT_VERSIONS" in upper:
                aid, seq, entry_path, origin, title, label, byte_len = params
                artifact_versions[(aid, seq)] = {
                    "seq": seq,
                    "entry_path": entry_path,
                    "origin": origin,
                    "title": title,
                    "label": label,
                    "byte_len": byte_len,
                    "published_at": NOW,
                }
                cur._rows = []
                return
            if "INSERT INTO DALTON_MONITOR_VERSIONS" in upper:
                monitor_versions.setdefault(params[0], set()).add(int(params[1]))
                cur._rows = []
                return
            if "JOIN DALTON_MONITOR_VERSIONS MV" in upper:
                mid, aid = params
                seqs = monitor_versions.get(mid, set())
                rows = [
                    dict(artifact_versions[(aid, s)])
                    for s in sorted(seqs, reverse=True)
                    if (aid, s) in artifact_versions
                ]
                cur._rows = rows
                return
            if "SELECT 1 FROM DALTON_MONITOR_VERSIONS" in upper:
                mid, req_seq = params
                cur._rows = [(1,)] if req_seq in monitor_versions.get(mid, set()) else []
                return
            if "SELECT SEQ, PATH FROM ARTIFACT_VERSION_FILES" in upper:
                aid, missing_seqs = params
                seq_set = set(missing_seqs)
                cur._rows = sorted(
                    [
                        {"seq": fseq, "path": fpath}
                        for (faid, fseq, fpath) in artifact_version_files
                        if faid == aid and fseq in seq_set
                    ],
                    key=lambda r: (r["seq"], r["path"]),
                )
                return
            if "FROM ARTIFACT_VERSION_FILES F JOIN SHARE_LINKS L" in upper:
                aid, seq, path = params
                frow = artifact_version_files.get((aid, seq, path))
                cur._rows = [dict(frow)] if frow and aid in share_links else []
                return

            cur._rows = []

        async def _exec_many(sql: str, seq_params: list[tuple[Any, ...]]) -> None:
            upper = " ".join(sql.upper().split())
            if "INSERT INTO ARTIFACT_VERSION_FILES" in upper:
                for aid, seq, path, uid, sha, byte_len, ctype, content in seq_params:
                    artifact_version_files[(aid, seq, path)] = {
                        "user_id": uid,
                        "sha256": sha,
                        "byte_len": byte_len,
                        "content_type": ctype,
                        "content": content,
                    }
            elif "INSERT INTO DALTON_MONITOR_MEMBERS" in upper:
                for mid, uid in seq_params:
                    monitor_members.setdefault(mid, set()).add(uid)

        cur.execute.side_effect = _exec
        cur.executemany.side_effect = _exec_many
        cur.fetchone.side_effect = lambda: cur._rows[0] if cur._rows else None
        cur.fetchall.side_effect = lambda: list(cur._rows)
        conn = create_autospec(AsyncConnection, instance=True)
        conn.cursor.return_value = cur

        @asynccontextmanager
        async def _tx():
            snap = (
                copy.deepcopy(share_links),
                copy.deepcopy(artifact_versions),
                copy.deepcopy(artifact_version_files),
                copy.deepcopy(monitors_table),
                copy.deepcopy(monitor_members),
                copy.deepcopy(monitor_versions),
                copy.deepcopy(publish_runs),
            )
            try:
                yield
            except BaseException:
                for target, saved in zip(
                    (
                        share_links,
                        artifact_versions,
                        artifact_version_files,
                        monitors_table,
                        monitor_members,
                        monitor_versions,
                        publish_runs,
                    ),
                    snap,
                    strict=True,
                ):
                    target.clear()
                    if isinstance(target, dict):
                        target.update(saved)
                    else:
                        target.update(saved)
                raise

        conn.transaction.side_effect = _tx
        yield conn

    monkeypatch.setattr(monitor_db, "get_db_connection", _fake_db_conn)
    monkeypatch.setattr(artifacts_records_db, "get_db_connection", _fake_db_conn)
    monkeypatch.setattr(artifacts_versions_db, "get_db_connection", _fake_db_conn)
    monkeypatch.setattr(
        monitor_db,
        "ensure_company_monitor_workspace",
        create_autospec(monitor_db.ensure_company_monitor_workspace, return_value=WS_ID),
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
        monitor_service,
        "get_user_tier",
        create_autospec(monitor_service.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        monitor_perms,
        "get_user_tier",
        create_autospec(monitor_perms.get_user_tier, return_value="internal"),
    )
    return executed_sql


@pytest.mark.asyncio
async def test_legacy_data_job_monitor_auto_migrates_in_single_tx_preserving_history_and_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.dalton import check_monitor_conflicts as cmc

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    data_root = tmp_path / "openclaw-data"
    hk_dir = data_root / "research-outputs" / "hk-momentum"
    cn_dir = data_root / "research-outputs" / "cn-intraday"
    hk_dir.mkdir(parents=True)
    cn_dir.mkdir(parents=True)
    (hk_dir / "index.html").write_text("<html><body>HK v5</body></html>", encoding="utf-8")
    (cn_dir / "index.html").write_text("<html><body>CN v1</body></html>", encoding="utf-8")

    # Pre-upgrade legacy DB state: hk-momentum has 4 published versions on shared index.html,
    # and its source_ref was overwritten by cn-intraday in the old collision.
    legacy_aid = "61000000-0000-4000-8000-000000009999"
    hk_mid = "60000000-0000-4000-8000-000000000101"
    share_links: dict[str, dict[str, Any]] = {
        legacy_aid: {
            "artifact_id": legacy_aid,
            "code": "legacycode",
            "workspace_id": WS_ID,
            "path": "index.html",
            "latest_seq": 4,
            "shared_at": None,
            "files": None,
            "revision": 1,
            "shared_seq": 0,
        }
    }
    artifact_versions: dict[tuple[str, int], dict[str, Any]] = {}
    artifact_version_files: dict[tuple[str, int, str], dict[str, Any]] = {}
    for seq in range(1, 5):
        body = f"<html><body>HK v{seq}</body></html>".encode("utf-8")
        artifact_versions[(legacy_aid, seq)] = {
            "seq": seq,
            "entry_path": "index.html",
            "origin": "owner",
            "title": "港股动量指数",
            "label": None,
            "byte_len": len(body),
            "published_at": NOW,
        }
        artifact_version_files[(legacy_aid, seq, "index.html")] = {
            "user_id": COMPANY_SYSTEM_USER_ID,
            "sha256": hashlib.sha256(body).hexdigest(),
            "byte_len": len(body),
            "content_type": "text/html",
            "content": body,
        }

    monitors_table: dict[str, dict[str, Any]] = {
        hk_mid: {
            "monitor_id": hk_mid,
            "slug": "hk-momentum",
            "artifact_id": legacy_aid,
            "owner_user_id": COMPANY_SYSTEM_USER_ID,
            "title": "港股动量指数",
            "category": "hk",
            "sort_order": 45,
            "visibility": "members",
            "published_seq": 4,
            "keep_updated": True,
            "source_kind": "data_job",
            "source_ref": "cn-intraday",  # overwritten by old shared-artifact collision
            "acl_revision": 2,
            "last_publish_run_id": "old-run-4",
            "last_publish_error": None,
            "last_publish_failed_at": None,
            "created_at": NOW,
            "updated_at": NOW,
        }
    }
    monitor_members: dict[str, set[str]] = {hk_mid: {"alice", "bob"}}
    monitor_versions: dict[str, set[int]] = {hk_mid: {1, 2, 3, 4}}
    monitor_data: dict[tuple[str, str], dict[str, Any]] = {
        (hk_mid, "intraday"): {"score": 88.5, "as_of": NOW}
    }
    publish_runs: set[tuple[str, str]] = {(hk_mid, "old-run-4")}

    executed_sql = _setup_legacy_migration_db(
        monkeypatch,
        share_links=share_links,
        artifact_versions=artifact_versions,
        artifact_version_files=artifact_version_files,
        monitors_table=monitors_table,
        monitor_members=monitor_members,
        monitor_versions=monitor_versions,
        monitor_data=monitor_data,
        publish_runs=publish_runs,
    )

    # 0) Pre-migration: detail lists versions 1-4 and version 1 is readable at index.html
    pre_detail = await monitor_service.get_monitor_detail("hk-momentum", viewer_id="alice")
    assert [v["seq"] for v in pre_detail["versions"]] == [4, 3, 2, 1]
    pre_v1 = await monitor_service.serve_monitor_file(
        pre_detail["monitor_grant"], "1", pre_detail["versions"][-1]["entry_path"]
    )
    assert b"HK v1" in pre_v1.body

    # 1) cn-intraday runs first: leaves mon-hk-legacy untouched and creates its own cn-intraday/index.html panel
    cn_res = await monitor_service.republish_data_job_monitor(
        job_key="cn-intraday", run_id="run-cn-1", data_root=data_root
    )
    assert cn_res["slug"] == "cn-intraday"
    assert cn_res["monitor_id"] != hk_mid
    assert cn_res["published_seq"] == 1
    assert monitors_table[hk_mid]["artifact_id"] == legacy_aid
    assert share_links[legacy_aid]["path"] == "index.html"

    # 2) hk-momentum runs: renames share_links.path in place (keeps legacy_aid!) and cuts v5
    hk_res = await monitor_service.republish_data_job_monitor(
        job_key="hk-momentum", run_id="run-hk-1", data_root=data_root
    )
    assert hk_res["monitor_id"] == hk_mid
    assert hk_res["slug"] == "hk-momentum"
    assert hk_res["published_seq"] == 5
    assert monitors_table[hk_mid]["artifact_id"] == legacy_aid
    assert monitors_table[hk_mid]["source_ref"] == "hk-momentum"
    assert share_links[legacy_aid]["path"] == "hk-momentum/index.html"
    assert share_links[legacy_aid]["latest_seq"] == 5
    # Preserved: monitor_id, artifact_id, members, historical versions 1-4 + new 5, and realtime data
    assert monitor_members[hk_mid] == {"alice", "bob"}
    assert monitor_versions[hk_mid] == {1, 2, 3, 4, 5}
    assert monitor_data[(hk_mid, "intraday")]["score"] == 88.5
    assert all(row["path"] != "index.html" for row in share_links.values())
    assert all(
        not sql.upper().startswith("DELETE ")
        or sql.upper().startswith(("DELETE FROM ARTIFACT_VERSIONS ", "DELETE FROM DALTON_MONITOR_MEMBERS "))
        for sql in executed_sql
    )

    # 2b) Post-migration: detail lists 1-5 and reading version 1 AND version 5 returns actual file bytes
    post_detail = await monitor_service.get_monitor_detail("hk-momentum", viewer_id="alice")
    assert post_detail["monitor"]["monitor_id"] == hk_mid
    assert post_detail["monitor"]["artifact_id"] == legacy_aid
    assert post_detail["monitor"]["entry_path"] == "hk-momentum/index.html"
    assert [v["seq"] for v in post_detail["versions"]] == [5, 4, 3, 2, 1]
    assert post_detail["versions"][0]["entry_path"] == "hk-momentum/index.html"
    assert post_detail["versions"][-1]["entry_path"] == "index.html"

    served_v1 = await monitor_service.serve_monitor_file(
        post_detail["monitor_grant"], "1", post_detail["versions"][-1]["entry_path"]
    )
    assert b"HK v1" in served_v1.body
    served_v5 = await monitor_service.serve_monitor_file(
        post_detail["monitor_grant"], "5", post_detail["versions"][0]["entry_path"]
    )
    assert b"HK v5" in served_v5.body

    # 3) Subsequent runs on both tasks update their own artifacts independently without overwriting
    (hk_dir / "index.html").write_text("<html><body>HK v6</body></html>", encoding="utf-8")
    (cn_dir / "index.html").write_text("<html><body>CN v2</body></html>", encoding="utf-8")
    hk_res2 = await monitor_service.republish_data_job_monitor(
        job_key="hk-momentum", run_id="run-hk-2", data_root=data_root
    )
    cn_res2 = await monitor_service.republish_data_job_monitor(
        job_key="cn-intraday", run_id="run-cn-2", data_root=data_root
    )
    assert hk_res2["monitor_id"] == hk_mid and hk_res2["published_seq"] == 6
    assert cn_res2["monitor_id"] == cn_res["monitor_id"] and cn_res2["published_seq"] == 2
    assert monitors_table[hk_mid]["artifact_id"] == legacy_aid
    assert monitors_table[hk_mid]["artifact_id"] != monitors_table[cn_res["monitor_id"]]["artifact_id"]

    # 4) Conflict checker sees no conflicts and no leftover index.html
    report = cmc.analyze_monitor_conflicts(
        monitor_rows=[
            {
                "monitor_id": row["monitor_id"],
                "slug": row["slug"],
                "source_kind": row["source_kind"],
                "source_ref": row["source_ref"],
                "artifact_id": row["artifact_id"],
                "entry_path": share_links[row["artifact_id"]]["path"],
                "published_seq": row["published_seq"],
            }
            for row in monitors_table.values()
        ],
        workspace_links=[
            {
                "artifact_id": aid,
                "path": row["path"],
                "latest_seq": row["latest_seq"],
                "is_referenced": any(m["artifact_id"] == aid for m in monitors_table.values()),
            }
            for aid, row in share_links.items()
        ],
    )
    assert report["has_conflicts"] is False
    assert report["legacy_shared_links"] == []
    assert report["historical_shared_links"] == []
    assert report["remediation_sql"] == []


@pytest.mark.asyncio
async def test_legacy_data_job_monitor_migration_aborts_when_target_path_occupied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    data_root = tmp_path / "openclaw-data"
    hk_dir = data_root / "research-outputs" / "hk-momentum"
    hk_dir.mkdir(parents=True)
    (hk_dir / "index.html").write_text("<html><body>HK v5</body></html>", encoding="utf-8")

    legacy_aid = "61000000-0000-4000-8000-000000009999"
    occupant_aid = "61000000-0000-4000-8000-000000008888"
    hk_mid = "60000000-0000-4000-8000-000000000101"
    share_links: dict[str, dict[str, Any]] = {
        legacy_aid: {
            "artifact_id": legacy_aid,
            "code": "legacycode",
            "workspace_id": WS_ID,
            "path": "index.html",
            "latest_seq": 4,
            "shared_at": None,
            "files": None,
            "revision": 1,
            "shared_seq": 0,
        },
        occupant_aid: {
            "artifact_id": occupant_aid,
            "code": "occupantcode",
            "workspace_id": WS_ID,
            "path": "hk-momentum/index.html",
            "latest_seq": 1,
            "shared_at": None,
            "files": None,
            "revision": 1,
            "shared_seq": 0,
        },
    }
    monitors_table: dict[str, dict[str, Any]] = {
        hk_mid: {
            "monitor_id": hk_mid,
            "slug": "hk-momentum",
            "artifact_id": legacy_aid,
            "owner_user_id": COMPANY_SYSTEM_USER_ID,
            "title": "港股动量指数",
            "category": "hk",
            "sort_order": 45,
            "visibility": "company",
            "published_seq": 4,
            "keep_updated": True,
            "source_kind": "data_job",
            "source_ref": "hk-momentum",
            "acl_revision": 1,
            "last_publish_run_id": "old-run-4",
            "last_publish_error": None,
            "last_publish_failed_at": None,
            "created_at": NOW,
            "updated_at": NOW,
        }
    }
    monitor_versions: dict[str, set[int]] = {hk_mid: {1, 2, 3, 4}}
    publish_runs: set[tuple[str, str]] = {(hk_mid, "old-run-4")}

    _setup_legacy_migration_db(
        monkeypatch,
        share_links=share_links,
        artifact_versions={},
        artifact_version_files={},
        monitors_table=monitors_table,
        monitor_members={},
        monitor_versions=monitor_versions,
        monitor_data={},
        publish_runs=publish_runs,
    )

    with pytest.raises(HTTPException) as exc_info:
        await monitor_service.republish_data_job_monitor(
            job_key="hk-momentum", run_id="run-hk-occupied", data_root=data_root
        )

    assert exc_info.value.status_code == 409
    assert "已被另一资源占用" in exc_info.value.detail
    assert occupant_aid in exc_info.value.detail
    # Migration rolled back cleanly: path, artifact_id, published_seq, versions, occupant all unchanged
    assert share_links[legacy_aid]["path"] == "index.html"
    assert share_links[legacy_aid]["latest_seq"] == 4
    assert share_links[occupant_aid]["path"] == "hk-momentum/index.html"
    assert monitors_table[hk_mid]["artifact_id"] == legacy_aid
    assert monitors_table[hk_mid]["published_seq"] == 4
    assert monitor_versions[hk_mid] == {1, 2, 3, 4}
    assert (hk_mid, "run-hk-occupied") not in publish_runs
    assert monitors_table[hk_mid]["last_publish_error"] is not None


@pytest.mark.asyncio
async def test_non_legacy_or_workspace_slug_collision_still_raises_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    data_root = tmp_path / "openclaw-data"
    hk_dir = data_root / "research-outputs" / "hk-momentum"
    hk_dir.mkdir(parents=True)
    (hk_dir / "index.html").write_text("<html><body>HK</body></html>", encoding="utf-8")

    # A workspace monitor owned by a human user occupying slug="hk-momentum" must NOT be auto-migrated.
    workspace_conflict = _make_monitor(
        monitor_id="60000000-0000-4000-8000-000000000202",
        slug="hk-momentum",
        source_kind="workspace",
        owner_user_id="human-analyst-1",
        entry_path="index.html",
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_data_job",
        create_autospec(monitor_db.get_monitor_by_data_job, return_value=None),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(monitor_db.get_monitor_by_slug, return_value=workspace_conflict),
    )

    with pytest.raises(HTTPException) as exc_info:
        await monitor_service.republish_data_job_monitor(
            job_key="hk-momentum", run_id="run-hk-conflict", data_root=data_root
        )
    assert exc_info.value.status_code == 409
    assert "禁止自动改名" in exc_info.value.detail


@pytest.mark.asyncio
async def test_monitor_detail_derives_version_entry_path_from_files_and_loads_migrated_v1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_aid = "61000000-0000-4000-8000-000000009998"
    hk_mid = "60000000-0000-4000-8000-000000000203"
    v1_body = b"<html><body>HK v1 legacy</body></html>"
    v2_body = b"<html><body>HK v2 migrated</body></html>"

    share_links: dict[str, dict[str, Any]] = {
        legacy_aid: {
            "artifact_id": legacy_aid,
            "code": "migratedcode",
            "workspace_id": WS_ID,
            "path": "hk-momentum/index.html",
            "latest_seq": 2,
            "shared_at": None,
            "files": None,
            "revision": 1,
            "shared_seq": 0,
        }
    }
    artifact_versions: dict[tuple[str, int], dict[str, Any]] = {
        (legacy_aid, 1): {
            "seq": 1,
            "entry_path": None,
            "origin": "owner",
            "title": "港股动量指数",
            "label": None,
            "byte_len": len(v1_body),
            "published_at": NOW,
        },
        (legacy_aid, 2): {
            "seq": 2,
            "entry_path": "",
            "origin": "owner",
            "title": "港股动量指数",
            "label": None,
            "byte_len": len(v2_body),
            "published_at": NOW,
        },
    }
    artifact_version_files: dict[tuple[str, int, str], dict[str, Any]] = {
        (legacy_aid, 1, "assets/app.js"): {
            "user_id": COMPANY_SYSTEM_USER_ID,
            "sha256": hashlib.sha256(b"console.log(1);").hexdigest(),
            "byte_len": 15,
            "content_type": "application/javascript",
            "content": b"console.log(1);",
        },
        (legacy_aid, 1, "index.html"): {
            "user_id": COMPANY_SYSTEM_USER_ID,
            "sha256": hashlib.sha256(v1_body).hexdigest(),
            "byte_len": len(v1_body),
            "content_type": "text/html",
            "content": v1_body,
        },
        (legacy_aid, 2, "hk-momentum/assets/app.js"): {
            "user_id": COMPANY_SYSTEM_USER_ID,
            "sha256": hashlib.sha256(b"console.log(2);").hexdigest(),
            "byte_len": 15,
            "content_type": "application/javascript",
            "content": b"console.log(2);",
        },
        (legacy_aid, 2, "hk-momentum/index.html"): {
            "user_id": COMPANY_SYSTEM_USER_ID,
            "sha256": hashlib.sha256(v2_body).hexdigest(),
            "byte_len": len(v2_body),
            "content_type": "text/html",
            "content": v2_body,
        },
    }
    monitors_table: dict[str, dict[str, Any]] = {
        hk_mid: {
            "monitor_id": hk_mid,
            "slug": "hk-momentum",
            "artifact_id": legacy_aid,
            "owner_user_id": COMPANY_SYSTEM_USER_ID,
            "title": "港股动量指数",
            "category": "hk",
            "sort_order": 45,
            "visibility": "company",
            "published_seq": 2,
            "keep_updated": True,
            "source_kind": "data_job",
            "source_ref": "hk-momentum",
            "acl_revision": 1,
            "last_publish_run_id": "run-hk-2",
            "last_publish_error": None,
            "last_publish_failed_at": None,
            "created_at": NOW,
            "updated_at": NOW,
        }
    }

    _setup_legacy_migration_db(
        monkeypatch,
        share_links=share_links,
        artifact_versions=artifact_versions,
        artifact_version_files=artifact_version_files,
        monitors_table=monitors_table,
        monitor_members={},
        monitor_versions={hk_mid: {1, 2}},
        monitor_data={},
        publish_runs=set(),
    )

    detail = await monitor_service.get_monitor_detail("hk-momentum", viewer_id="alice")
    assert detail["monitor"]["entry_path"] == "hk-momentum/index.html"
    assert [v["seq"] for v in detail["versions"]] == [2, 1]
    assert detail["versions"][0]["entry_path"] == "hk-momentum/index.html"
    assert detail["versions"][1]["entry_path"] == "index.html"

    served_v1 = await monitor_service.serve_monitor_file(
        detail["monitor_grant"], "1", detail["versions"][1]["entry_path"]
    )
    assert b"HK v1 legacy" in served_v1.body
    served_v2 = await monitor_service.serve_monitor_file(
        detail["monitor_grant"], "2", detail["versions"][0]["entry_path"]
    )
    assert b"HK v2 migrated" in served_v2.body

