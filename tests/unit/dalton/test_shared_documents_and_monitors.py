"""Unit tests for non-HTML shared documents and Monitors panel HTML-only filtering."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from src.dalton.monitors import (
    db as monitor_db,
    grants as monitor_grants,
    permissions as monitor_perms,
    service as monitor_service,
    shared_documents as shared_doc_service,
)
from src.dalton.monitors.models import (
    MonitorRecord,
    PublishMonitorRequest,
    SharedDocumentRecord,
    ShareDocumentRequest,
    is_html_entry_path,
    is_monitor_panel_entry,
)
from src.dalton.monitors.router import router as monitors_router
from src.server.database.artifacts import Artifact, Version, VersionFile
from src.server.database.artifacts.versions import PublishResult, StoredVersionFile
from src.server.services.artifacts.manifest import ManifestEntry
from src.server.services.artifacts.snapshot import Snapshot
from src.server.utils.api import get_current_user_id

NOW = datetime(2026, 11, 27, 10, 0, 0, tzinfo=UTC)
WS_ID = "62000000-0000-4000-8000-000000000099"
TEST_SIGNING_KEY = b"dalton-shared-doc-test-hmac-key!"


@pytest.fixture(autouse=True)
def _stub_grant_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(monitor_grants._MONITOR_CODEC, "_key", TEST_SIGNING_KEY)
    monkeypatch.setattr(monitor_grants._SHARED_DOC_CODEC, "_key", TEST_SIGNING_KEY)


def _build_app(user_id: str) -> FastAPI:
    app = FastAPI()
    app.include_router(monitors_router)
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    return app


def _make_monitor(**overrides: Any) -> MonitorRecord:
    defaults: dict[str, Any] = dict(
        monitor_id="60000000-0000-4000-8000-000000000101",
        slug="alpha-dashboard",
        artifact_id="61000000-0000-4000-8000-000000000101",
        owner_user_id="owner-1",
        title="Alpha Dashboard",
        category="overview",
        sort_order=0,
        visibility="company",
        published_seq=1,
        keep_updated=False,
        source_kind="workspace",
        source_ref=f"{WS_ID}:report.html",
        acl_revision=1,
        created_at=NOW,
        updated_at=NOW,
        artifact_code="htmlcode1234",
        workspace_id=WS_ID,
        entry_path="report.html",
        owner_name="Alice",
        owner_email="alice@company.com",
        data_as_of=None,
        member_user_ids=(),
    )
    defaults.update(overrides)
    return MonitorRecord(**defaults)


def _make_doc(**overrides: Any) -> SharedDocumentRecord:
    defaults: dict[str, Any] = dict(
        doc_id="70000000-0000-4000-8000-000000000101",
        artifact_id="71000000-0000-4000-8000-000000000101",
        owner_user_id="owner-1",
        title="Q4 Memo",
        visibility="company",
        published_seq=1,
        keep_updated=False,
        acl_revision=1,
        created_at=NOW,
        updated_at=NOW,
        artifact_code="mdcode123456",
        workspace_id=WS_ID,
        entry_path="notes/q4-memo.md",
        owner_name="Alice",
        owner_email="alice@company.com",
        member_user_ids=(),
    )
    defaults.update(overrides)
    return SharedDocumentRecord(**defaults)


def _make_artifact(**overrides: Any) -> Artifact:
    defaults: dict[str, Any] = dict(
        artifact_id="71000000-0000-4000-8000-000000000101",
        kind="file",
        code="mdcode123456",
        workspace_id=WS_ID,
        path="notes/q4-memo.md",
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


def test_html_entry_path_classification() -> None:
    assert is_html_entry_path("report.html") is True
    assert is_html_entry_path("nested/INDEX.HTM") is True
    assert is_html_entry_path("notes/memo.md") is False
    assert is_html_entry_path("deck.pdf") is False
    assert is_html_entry_path("model.xlsx") is False
    assert is_html_entry_path(None) is False

    html_mon = _make_monitor(entry_path="dashboard.html", source_kind="workspace")
    md_mon = _make_monitor(entry_path="notes/memo.md", source_kind="workspace")
    job_mon = _make_monitor(entry_path="job/index.html", source_kind="data_job")
    assert is_monitor_panel_entry(html_mon) is True
    assert is_monitor_panel_entry(md_mon) is False
    assert is_monitor_panel_entry(job_mon) is True


@pytest.mark.asyncio
async def test_non_html_files_rejected_by_monitor_publish_and_hidden_from_monitor_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    monkeypatch.setattr(
        monitor_perms,
        "get_user_tier",
        create_autospec(monitor_perms.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        monitor_service,
        "get_user_tier",
        create_autospec(monitor_service.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        monitor_service,
        "owned_workspace_on_computer",
        create_autospec(
            monitor_service.owned_workspace_on_computer,
            return_value={"workspace_id": WS_ID, "user_id": "owner-1", "status": "running"},
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
    commit_spy = create_autospec(monitor_db.commit_monitor_publish_tx)
    monkeypatch.setattr(monitor_db, "commit_monitor_publish_tx", commit_spy)

    # Sharing md / pdf / xlsx via monitor publish endpoint is refused and creates no monitor row.
    for path in ("notes/memo.md", "deck.pdf", "model.xlsx"):
        with pytest.raises(HTTPException) as exc:
            await monitor_service.publish_workspace_monitor(
                PublishMonitorRequest(
                    workspace_id=WS_ID,
                    path=path,
                    visibility="company",
                ),
                caller_id="owner-1",
            )
        assert exc.value.status_code == 400
    commit_spy.assert_not_called()

    # Existing legacy non-HTML rows in dalton_monitors stay out of GET /api/v1/dalton/monitors.
    html_mon = _make_monitor(
        monitor_id="60000000-0000-4000-8000-000000000201",
        slug="html-panel",
        entry_path="report.html",
    )
    legacy_md_mon = _make_monitor(
        monitor_id="60000000-0000-4000-8000-000000000202",
        slug="legacy-md",
        artifact_id="61000000-0000-4000-8000-000000000202",
        entry_path="notes/legacy.md",
        title="Legacy MD",
    )
    data_job_mon = _make_monitor(
        monitor_id="60000000-0000-4000-8000-000000000203",
        slug="us-market",
        artifact_id="61000000-0000-4000-8000-000000000203",
        source_kind="data_job",
        entry_path="us-market/index.html",
        title="US Market",
    )
    monkeypatch.setattr(
        monitor_db,
        "list_all_monitors",
        create_autospec(
            monitor_db.list_all_monitors,
            return_value=[html_mon, legacy_md_mon, data_job_mon],
        ),
    )

    app = _build_app("colleague-1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.get("/api/v1/dalton/monitors")
        assert resp.status_code == 200
        slugs = [m["slug"] for m in resp.json()["monitors"]]
        assert "html-panel" in slugs
        assert "us-market" in slugs
        assert "legacy-md" not in slugs


@pytest.mark.asyncio
async def test_share_md_document_creates_no_monitor_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    tiers = {"owner-1": "internal", "colleague-a": "internal"}

    async def _tier(uid: str | None) -> str:
        return tiers.get(str(uid or ""), "external")

    monkeypatch.setattr(
        monitor_perms,
        "get_user_tier",
        create_autospec(monitor_perms.get_user_tier, side_effect=_tier),
    )
    monkeypatch.setattr(
        shared_doc_service,
        "get_user_tier",
        create_autospec(shared_doc_service.get_user_tier, side_effect=_tier),
    )
    monkeypatch.setattr(
        shared_doc_service,
        "owned_workspace_on_computer",
        create_autospec(
            shared_doc_service.owned_workspace_on_computer,
            return_value={"workspace_id": WS_ID, "user_id": "owner-1", "status": "running"},
        ),
    )
    monkeypatch.setattr(
        shared_doc_service,
        "owner_work_dir",
        create_autospec(shared_doc_service.owner_work_dir, return_value="/home/workspace"),
    )
    monkeypatch.setattr(
        shared_doc_service,
        "previous_dir_names_of",
        create_autospec(shared_doc_service.previous_dir_names_of, return_value=()),
    )
    art = _make_artifact()
    monkeypatch.setattr(
        shared_doc_service.artifacts_db,
        "ensure_artifact",
        create_autospec(shared_doc_service.artifacts_db.ensure_artifact, return_value=art),
    )
    monkeypatch.setattr(
        shared_doc_service,
        "get_shared_document_by_artifact_id",
        create_autospec(shared_doc_service.get_shared_document_by_artifact_id, return_value=None),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_artifact_id",
        create_autospec(monitor_db.get_monitor_by_artifact_id, return_value=None),
    )
    snap = Snapshot(
        entries=[ManifestEntry("notes/q4-memo.md", 16, "entry")],
        left_out=[],
        files=[
            VersionFile(
                path="notes/q4-memo.md",
                sha256="a" * 64,
                content_type="text/markdown",
                byte_len=16,
                content=b"# Q4 Memo\nhello\n",
            )
        ],
        blobs={},
        title="Q4 Memo",
    )
    monkeypatch.setattr(
        shared_doc_service.artifact_snapshot,
        "capture_workspace_monitor_snapshot",
        create_autospec(
            shared_doc_service.artifact_snapshot.capture_workspace_monitor_snapshot,
            return_value=snap,
        ),
    )
    saved_doc = _make_doc(visibility="members", member_user_ids=("colleague-a",))
    doc_commit_spy = create_autospec(
        shared_doc_service.commit_shared_document_publish_tx,
        return_value=(saved_doc, PublishResult(artifact=art, seq=1, cut=True, shared_now=False)),
    )
    monitor_commit_spy = create_autospec(monitor_db.commit_monitor_publish_tx)
    monkeypatch.setattr(
        shared_doc_service,
        "commit_shared_document_publish_tx",
        doc_commit_spy,
    )
    monkeypatch.setattr(monitor_db, "commit_monitor_publish_tx", monitor_commit_spy)

    result = await shared_doc_service.share_workspace_document(
        ShareDocumentRequest(
            workspace_id=WS_ID,
            path="notes/q4-memo.md",
            visibility="members",
            member_user_ids=["colleague-a"],
        ),
        caller_id="owner-1",
    )
    assert result["document"]["file_name"] == "q4-memo.md"
    assert result["document"]["visibility"] == "members"
    assert result["document"]["url"] == "/a/mdcode123456"
    doc_commit_spy.assert_awaited_once()
    monitor_commit_spy.assert_not_called()


@pytest.mark.asyncio
async def test_shared_documents_endpoint_lists_only_authorized_non_html_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tiers = {
        "owner-1": "internal",
        "colleague-a": "internal",
        "colleague-b": "internal",
        "ext-1": "external",
    }

    async def _tier(uid: str | None) -> str:
        return tiers.get(str(uid or ""), "external")

    monkeypatch.setattr(
        shared_doc_service,
        "get_user_tier",
        create_autospec(shared_doc_service.get_user_tier, side_effect=_tier),
    )
    monkeypatch.setattr(
        monitor_perms,
        "get_user_tier",
        create_autospec(monitor_perms.get_user_tier, side_effect=_tier),
    )

    own_doc = _make_doc(
        doc_id="70000000-0000-4000-8000-000000000301",
        artifact_id="71000000-0000-4000-8000-000000000301",
        artifact_code="owncode12345",
        entry_path="private.md",
        visibility="own",
        updated_at=NOW + timedelta(minutes=5),
    )
    members_doc = _make_doc(
        doc_id="70000000-0000-4000-8000-000000000302",
        artifact_id="71000000-0000-4000-8000-000000000302",
        artifact_code="memcode12345",
        entry_path="notes/for-a.md",
        visibility="members",
        member_user_ids=("colleague-a",),
        updated_at=NOW + timedelta(minutes=4),
    )
    company_doc = _make_doc(
        doc_id="70000000-0000-4000-8000-000000000303",
        artifact_id="71000000-0000-4000-8000-000000000303",
        artifact_code="comcode12345",
        entry_path="models/valuation.xlsx",
        visibility="company",
        updated_at=NOW + timedelta(minutes=3),
    )
    public_doc = _make_doc(
        doc_id="70000000-0000-4000-8000-000000000304",
        artifact_id="71000000-0000-4000-8000-000000000304",
        artifact_code="pubcode12345",
        entry_path="briefs/public.pdf",
        visibility="public",
        updated_at=NOW + timedelta(minutes=2),
    )
    legacy_pdf_monitor = _make_monitor(
        monitor_id="60000000-0000-4000-8000-000000000305",
        slug="legacy-deck",
        artifact_id="61000000-0000-4000-8000-000000000305",
        artifact_code="legcode12345",
        entry_path="slides/deck.pdf",
        visibility="company",
        updated_at=NOW + timedelta(minutes=1),
    )
    html_monitor = _make_monitor(
        monitor_id="60000000-0000-4000-8000-000000000306",
        slug="html-page",
        artifact_id="61000000-0000-4000-8000-000000000306",
        artifact_code="htmcode12345",
        entry_path="dashboard.html",
        visibility="company",
    )

    monkeypatch.setattr(
        shared_doc_service,
        "list_all_shared_documents",
        create_autospec(
            shared_doc_service.list_all_shared_documents,
            return_value=[own_doc, members_doc, company_doc, public_doc],
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "list_all_monitors",
        create_autospec(
            monitor_db.list_all_monitors,
            return_value=[legacy_pdf_monitor, html_monitor],
        ),
    )

    # Colleague A (listed in members_doc) sees members_doc, company_doc, public_doc, legacy_pdf_monitor (never own_doc or html_monitor)
    async with AsyncClient(transport=ASGITransport(app=_build_app("colleague-a")), base_url="http://t") as client:
        resp_a = await client.get("/api/v1/dalton/monitors/shared-documents")
        assert resp_a.status_code == 200
        body_a = resp_a.json()
        names_a = [d["file_name"] for d in body_a["documents"]]
        assert names_a == ["for-a.md", "valuation.xlsx", "public.pdf", "deck.pdf"]
        assert body_a["documents"][0]["url"] == "/a/memcode12345"
        assert body_a["shared_by_me"] == []
        assert [d["file_name"] for d in body_a["shared_with_me"]] == [
            "for-a.md",
            "valuation.xlsx",
            "public.pdf",
            "deck.pdf",
        ]
        assert all(d["is_owner"] is False for d in body_a["shared_with_me"])

    # Owner-1 sees their own shared non-HTML docs in shared_by_me (never own_doc or html_monitor)
    async with AsyncClient(transport=ASGITransport(app=_build_app("owner-1")), base_url="http://t") as client:
        resp_owner = await client.get("/api/v1/dalton/monitors/shared-documents")
        assert resp_owner.status_code == 200
        body_owner = resp_owner.json()
        assert [d["file_name"] for d in body_owner["shared_by_me"]] == [
            "for-a.md",
            "valuation.xlsx",
            "public.pdf",
            "deck.pdf",
        ]
        assert all(d["is_owner"] is True for d in body_owner["shared_by_me"])
        assert body_owner["shared_with_me"] == []

    # Colleague B (NOT listed in members_doc) does NOT see for-a.md
    async with AsyncClient(transport=ASGITransport(app=_build_app("colleague-b")), base_url="http://t") as client:
        resp_b = await client.get("/api/v1/dalton/monitors/shared-documents")
        assert resp_b.status_code == 200
        names_b = [d["file_name"] for d in resp_b.json()["documents"]]
        assert names_b == ["valuation.xlsx", "public.pdf", "deck.pdf"]

    # External member only sees public.pdf in shared_with_me (never members/company/own)
    async with AsyncClient(transport=ASGITransport(app=_build_app("ext-1")), base_url="http://t") as client:
        resp_ext = await client.get("/api/v1/dalton/monitors/shared-documents")
        assert resp_ext.status_code == 200
        body_ext = resp_ext.json()
        names_ext = [d["file_name"] for d in body_ext["documents"]]
        assert names_ext == ["public.pdf"]
        assert body_ext["shared_by_me"] == []
        assert [d["file_name"] for d in body_ext["shared_with_me"]] == ["public.pdf"]


@pytest.mark.asyncio
async def test_shared_document_grant_serves_authorized_colleague_and_rejects_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tiers = {"colleague-a": "internal", "colleague-b": "internal"}

    async def _tier(uid: str | None) -> str:
        return tiers.get(str(uid or ""), "external")

    doc = _make_doc(visibility="members", member_user_ids=("colleague-a",), published_seq=2, acl_revision=3)
    monkeypatch.setattr(
        shared_doc_service,
        "get_user_tier",
        create_autospec(shared_doc_service.get_user_tier, side_effect=_tier),
    )
    monkeypatch.setattr(
        shared_doc_service,
        "get_shared_document_by_artifact_id",
        create_autospec(shared_doc_service.get_shared_document_by_artifact_id, return_value=doc),
    )
    monkeypatch.setattr(
        shared_doc_service.artifacts_db,
        "get_version",
        create_autospec(
            shared_doc_service.artifacts_db.get_version,
            return_value=Version(
                seq=2,
                entry_path="notes/q4-memo.md",
                origin="owner",
                title="Q4 Memo",
                label=None,
                byte_len=16,
                published_at=NOW,
            ),
        ),
    )
    monkeypatch.setattr(
        shared_doc_service.artifacts_db,
        "get_version_file",
        create_autospec(
            shared_doc_service.artifacts_db.get_version_file,
            return_value=StoredVersionFile(
                user_id="owner-1",
                sha256="a" * 64,
                content_type="text/markdown",
                byte_len=16,
                content=b"# Q4 Memo\nhello\n",
            ),
        ),
    )

    from src.server.services import account as accounts_svc

    monkeypatch.setattr(
        accounts_svc,
        "read_standing",
        create_autospec(
            accounts_svc.read_standing,
            return_value=SimpleNamespace(suspended=False),
        ),
    )

    meta = await shared_doc_service.build_shared_document_file_metadata(
        doc.artifact_id, "colleague-a"
    )
    assert meta["seq"] == 2
    assert meta["frame_base"].startswith("/api/v1/dalton/monitors/g/d1.")
    token = meta["frame_base"].removeprefix("/api/v1/dalton/monitors/g/").rstrip("/")

    served = await monitor_service.serve_monitor_file(token, "2", "notes/q4-memo.md")
    assert served.status == 200
    assert served.body == b"# Q4 Memo\nhello\n"
    headers_map = {k.lower(): v for k, v in served.headers}
    assert headers_map["content-type"] == "text/markdown; charset=utf-8"

    # Unauthorized colleague gets 404 from metadata and 403 from forged/revoked grant
    with pytest.raises(HTTPException) as exc_meta:
        await shared_doc_service.build_shared_document_file_metadata(
            doc.artifact_id, "colleague-b"
        )
    assert exc_meta.value.status_code == 404

    stale_grant = await monitor_grants.mint_shared_document_grant(
        doc.artifact_id, "colleague-a", acl_revision=2
    )
    with pytest.raises(HTTPException) as exc_stale:
        await monitor_service.serve_monitor_file(stale_grant.token, "2", "notes/q4-memo.md")
    assert exc_stale.value.status_code == 403


@pytest.mark.asyncio
async def test_shared_documents_two_groups_mixed_ownership_and_unshare_revoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from psycopg import AsyncCursor

    tiers = {"me-1": "internal", "colleague-x": "internal", "ext-owner": "external"}

    async def _tier(uid: str | None) -> str:
        return tiers.get(str(uid or ""), "external")

    monkeypatch.setattr(
        shared_doc_service,
        "get_user_tier",
        create_autospec(shared_doc_service.get_user_tier, side_effect=_tier),
    )
    monkeypatch.setattr(
        monitor_perms,
        "get_user_tier",
        create_autospec(monitor_perms.get_user_tier, side_effect=_tier),
    )

    my_members_doc = _make_doc(
        doc_id="70000000-0000-4000-8000-000000000401",
        artifact_id="71000000-0000-4000-8000-000000000401",
        artifact_code="mymem1234567",
        owner_user_id="me-1",
        owner_name="Me",
        entry_path="notes/my-plan.md",
        visibility="members",
        member_user_ids=("colleague-x",),
        updated_at=NOW + timedelta(minutes=5),
    )
    other_company_doc = _make_doc(
        doc_id="70000000-0000-4000-8000-000000000402",
        artifact_id="71000000-0000-4000-8000-000000000402",
        artifact_code="othcom123456",
        owner_user_id="colleague-x",
        owner_name="Colleague X",
        entry_path="notes/team-brief.md",
        visibility="company",
        updated_at=NOW + timedelta(minutes=4),
    )
    other_members_hidden = _make_doc(
        doc_id="70000000-0000-4000-8000-000000000403",
        artifact_id="71000000-0000-4000-8000-000000000403",
        artifact_code="othhid123456",
        owner_user_id="colleague-x",
        owner_name="Colleague X",
        entry_path="notes/secret.md",
        visibility="members",
        member_user_ids=("someone-else",),
        updated_at=NOW + timedelta(minutes=3),
    )
    ext_own_shared_doc = _make_doc(
        doc_id="70000000-0000-4000-8000-000000000404",
        artifact_id="71000000-0000-4000-8000-000000000404",
        artifact_code="extown123456",
        owner_user_id="ext-owner",
        owner_name="Ext Owner",
        entry_path="notes/old-company-share.md",
        visibility="company",
        updated_at=NOW + timedelta(minutes=2),
    )

    monkeypatch.setattr(
        shared_doc_service,
        "list_all_shared_documents",
        create_autospec(
            shared_doc_service.list_all_shared_documents,
            return_value=[my_members_doc, other_company_doc, other_members_hidden, ext_own_shared_doc],
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "list_all_monitors",
        create_autospec(monitor_db.list_all_monitors, return_value=[]),
    )

    async with AsyncClient(transport=ASGITransport(app=_build_app("me-1")), base_url="http://t") as client:
        resp = await client.get("/api/v1/dalton/monitors/shared-documents")
        assert resp.status_code == 200
        payload = resp.json()
        assert [d["file_name"] for d in payload["shared_by_me"]] == ["my-plan.md"]
        assert [d["file_name"] for d in payload["shared_with_me"]] == [
            "team-brief.md",
            "old-company-share.md",
        ]

    # Even if an owner was downgraded to external, their own previously shared doc stays in shared_by_me for revocation.
    async with AsyncClient(transport=ASGITransport(app=_build_app("ext-owner")), base_url="http://t") as client:
        resp_ext = await client.get("/api/v1/dalton/monitors/shared-documents")
        assert resp_ext.status_code == 200
        payload_ext = resp_ext.json()
        assert [d["file_name"] for d in payload_ext["shared_by_me"]] == ["old-company-share.md"]
        assert payload_ext["shared_with_me"] == []

    # Unsharing a members/company shared document downgrades it to 'own' and deletes member rows.
    executed: list[str] = []
    cur = create_autospec(AsyncCursor, instance=True)

    async def _exec_doc(sql: str, _params: Any = None) -> None:
        executed.append(" ".join(sql.split()))
        if "FROM dalton_monitors" in sql:
            cur._row = None
        elif "FROM dalton_shared_documents" in sql:
            cur._row = {"doc_id": my_members_doc.doc_id, "visibility": "members"}
        else:
            cur._row = None

    cur.execute.side_effect = _exec_doc
    cur.fetchone.side_effect = lambda: getattr(cur, "_row", None)

    await shared_doc_service.downgrade_dalton_share_on_unshare_in_cur(
        cur, my_members_doc.artifact_id, entry_path="notes/my-plan.md"
    )
    assert any("UPDATE dalton_shared_documents SET visibility = 'own'" in s for s in executed)
    assert any("DELETE FROM dalton_shared_document_members" in s for s in executed)

    # Unsharing a legacy non-HTML monitor with visibility='members' also downgrades it to 'own' and clears members,
    # while an HTML monitor with visibility='company' is left untouched.
    executed_legacy: list[str] = []
    cur_legacy = create_autospec(AsyncCursor, instance=True)

    async def _exec_legacy(sql: str, _params: Any = None) -> None:
        executed_legacy.append(" ".join(sql.split()))
        if "FROM dalton_monitors" in sql:
            cur_legacy._row = {"monitor_id": "m-legacy-1", "visibility": "members"}
        else:
            cur_legacy._row = None

    cur_legacy.execute.side_effect = _exec_legacy
    cur_legacy.fetchone.side_effect = lambda: getattr(cur_legacy, "_row", None)

    await shared_doc_service.downgrade_dalton_share_on_unshare_in_cur(
        cur_legacy, "71000000-0000-4000-8000-000000000499", entry_path="notes/legacy.md"
    )
    assert any("UPDATE dalton_monitors SET visibility = 'own'" in s for s in executed_legacy)
    assert any("DELETE FROM dalton_monitor_members" in s for s in executed_legacy)

    executed_html: list[str] = []
    cur_html = create_autospec(AsyncCursor, instance=True)

    async def _exec_html(sql: str, _params: Any = None) -> None:
        executed_html.append(" ".join(sql.split()))
        if "FROM dalton_monitors" in sql:
            cur_html._row = {"monitor_id": "m-html-1", "visibility": "company"}
        else:
            cur_html._row = None

    cur_html.execute.side_effect = _exec_html
    cur_html.fetchone.side_effect = lambda: getattr(cur_html, "_row", None)

    await shared_doc_service.downgrade_dalton_share_on_unshare_in_cur(
        cur_html, "71000000-0000-4000-8000-000000000498", entry_path="dashboard.html"
    )
    assert not any("UPDATE dalton_monitors" in s for s in executed_html)


@pytest.mark.asyncio
async def test_republish_legacy_non_html_deactivates_old_monitor_and_unshare_revokes_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager
    from psycopg import AsyncCursor

    from src.server.services import account as accounts_svc

    tiers = {
        "owner-1": "internal",
        "colleague-a": "internal",
        "colleague-b": "internal",
    }

    async def _tier(uid: str | None) -> str:
        return tiers.get(str(uid or ""), "external")

    monkeypatch.setattr(
        shared_doc_service,
        "get_user_tier",
        create_autospec(shared_doc_service.get_user_tier, side_effect=_tier),
    )
    monkeypatch.setattr(
        monitor_perms,
        "get_user_tier",
        create_autospec(monitor_perms.get_user_tier, side_effect=_tier),
    )
    monkeypatch.setattr(
        accounts_svc,
        "read_standing",
        create_autospec(
            accounts_svc.read_standing,
            return_value=SimpleNamespace(suspended=False),
        ),
    )

    # 1) Re-publishing a legacy non-HTML monitor inside commit_shared_document_publish_tx
    #    atomically deactivates the legacy dalton_monitors row while preserving it.
    art = _make_artifact(
        artifact_id="71000000-0000-4000-8000-000000000501",
        path="notes/legacy-memo.md",
    )
    executed_tx: list[str] = []
    cur = create_autospec(AsyncCursor, instance=True)
    cur._row = None

    async def _exec_tx(sql: str, _params: Any = None) -> None:
        norm = " ".join(sql.split())
        executed_tx.append(norm)
        if "FROM dalton_monitors" in norm:
            cur._row = {
                "monitor_id": "60000000-0000-4000-8000-000000000501",
                "visibility": "company",
                "keep_updated": True,
            }
        elif "INSERT INTO dalton_shared_documents" in norm:
            cur._row = {"doc_id": "70000000-0000-4000-8000-000000000501"}
        elif "WHERE d.doc_id = %s::uuid" in norm:
            cur._row = {
                "doc_id": "70000000-0000-4000-8000-000000000501",
                "artifact_id": art.artifact_id,
                "owner_user_id": "owner-1",
                "title": "Legacy Memo",
                "visibility": "members",
                "published_seq": 2,
                "keep_updated": False,
                "acl_revision": 1,
                "created_at": NOW,
                "updated_at": NOW,
                "artifact_code": art.code,
                "workspace_id": WS_ID,
                "entry_path": art.path,
                "owner_name": "Alice",
                "owner_email": "alice@company.com",
                "member_user_ids": ["colleague-a"],
            }
        else:
            cur._row = None

    cur.execute.side_effect = _exec_tx
    cur.fetchone.side_effect = lambda: cur._row
    cur.fetchall.return_value = []
    cur.executemany.return_value = None
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = False

    class _FakeTx:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args: Any) -> bool:
            return False

    class _FakeConn:
        def transaction(self) -> _FakeTx:
            return _FakeTx()

        def cursor(self, **_kwargs: Any) -> Any:
            return cur

    @asynccontextmanager
    async def _fake_conn():
        yield _FakeConn()

    monkeypatch.setattr(shared_doc_service, "get_db_connection", _fake_conn)
    monkeypatch.setattr(
        shared_doc_service.artifacts_records_db,
        "lock_owned",
        create_autospec(
            shared_doc_service.artifacts_records_db.lock_owned, return_value=art
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "_resolve_live_tier_in_cur",
        create_autospec(monitor_db._resolve_live_tier_in_cur, return_value="internal"),
    )
    monkeypatch.setattr(
        shared_doc_service.artifacts_versions_db,
        "cut_monitor_in_cur",
        create_autospec(
            shared_doc_service.artifacts_versions_db.cut_monitor_in_cur,
            return_value=PublishResult(artifact=art, seq=2, cut=True, shared_now=False),
        ),
    )

    saved_doc, _res = await shared_doc_service.commit_shared_document_publish_tx(
        artifact_id=art.artifact_id,
        workspace_owner_id="owner-1",
        doc_owner_id="owner-1",
        title="Legacy Memo",
        visibility="members",
        keep_updated=False,
        member_user_ids=["colleague-a"],
        files=[
            VersionFile(
                path=art.path,
                sha256="b" * 64,
                content_type="text/markdown",
                byte_len=10,
                content=b"# updated\n",
            )
        ],
    )
    assert saved_doc.visibility == "members"
    assert any(
        "UPDATE dalton_monitors SET visibility = 'own', keep_updated = FALSE, acl_revision = acl_revision + 1"
        in s
        for s in executed_tx
    )
    assert any("DELETE FROM dalton_monitor_members" in s for s in executed_tx)
    assert not any("DELETE FROM dalton_monitors" in s for s in executed_tx)

    # 2) After narrowing scope to colleague-a, old audience (colleague-b) cannot read
    #    via /monitors/{slug}, /m/{slug}, old m1. grant, or /a/{code}, and does NOT see it in list.
    deactivated_mon = _make_monitor(
        monitor_id="60000000-0000-4000-8000-000000000501",
        slug="legacy-memo",
        artifact_id=art.artifact_id,
        artifact_code=art.code,
        entry_path=art.path,
        visibility="own",
        keep_updated=False,
        acl_revision=2,
        published_seq=1,
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(monitor_db.get_monitor_by_slug, return_value=deactivated_mon),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_id",
        create_autospec(monitor_db.get_monitor_by_id, return_value=deactivated_mon),
    )
    monkeypatch.setattr(
        shared_doc_service,
        "get_shared_document_by_artifact_id",
        create_autospec(
            shared_doc_service.get_shared_document_by_artifact_id,
            return_value=saved_doc,
        ),
    )

    with pytest.raises(HTTPException) as exc_pub:
        await monitor_service.get_public_monitor_detail("legacy-memo")
    assert exc_pub.value.status_code == 404

    with pytest.raises(HTTPException) as exc_mon:
        await monitor_service.get_monitor_detail("legacy-memo", viewer_id="colleague-b")
    assert exc_mon.value.status_code == 403

    old_grant = await monitor_grants.mint_monitor_grant(
        deactivated_mon.monitor_id, "colleague-b", acl_revision=1
    )
    with pytest.raises(HTTPException) as exc_grant:
        await monitor_service.serve_monitor_file(old_grant.token, "1", art.path)
    assert exc_grant.value.status_code == 403

    assert (
        await shared_doc_service.resolve_shared_document_for_link(
            art.artifact_id, "colleague-b"
        )
        == "denied"
    )
    assert (
        await shared_doc_service.resolve_shared_document_for_link(
            art.artifact_id, "colleague-a"
        )
        == "member"
    )

    # Even if a legacy monitor row with broad visibility were still returned,
    # the active dalton_shared_documents row governs the list exclusively.
    stale_broad_legacy = _make_monitor(
        monitor_id="60000000-0000-4000-8000-000000000501",
        slug="legacy-memo",
        artifact_id=art.artifact_id,
        artifact_code=art.code,
        entry_path=art.path,
        visibility="company",
        published_seq=1,
    )
    monkeypatch.setattr(
        shared_doc_service,
        "list_all_shared_documents",
        create_autospec(
            shared_doc_service.list_all_shared_documents, return_value=[saved_doc]
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "list_all_monitors",
        create_autospec(monitor_db.list_all_monitors, return_value=[stale_broad_legacy]),
    )
    items_b = await shared_doc_service.list_shared_documents_for_user("colleague-b")
    assert items_b == []
    items_a = await shared_doc_service.list_shared_documents_for_user("colleague-a")
    assert [i["artifact_id"] for i in items_a] == [art.artifact_id]

    # 3) Unsharing when BOTH dalton_monitors and dalton_shared_documents exist
    #    downgrades BOTH tables atomically instead of skipping dalton_shared_documents.
    executed_both: list[str] = []
    cur_both = create_autospec(AsyncCursor, instance=True)

    async def _exec_both(sql: str, _params: Any = None) -> None:
        norm = " ".join(sql.split())
        executed_both.append(norm)
        if "FROM dalton_monitors" in norm:
            cur_both._row = {
                "monitor_id": deactivated_mon.monitor_id,
                "visibility": "own",
                "keep_updated": False,
            }
        elif "FROM dalton_shared_documents" in norm:
            cur_both._row = {"doc_id": saved_doc.doc_id, "visibility": "members"}
        else:
            cur_both._row = None

    cur_both.execute.side_effect = _exec_both
    cur_both.fetchone.side_effect = lambda: getattr(cur_both, "_row", None)

    await shared_doc_service.downgrade_dalton_share_on_unshare_in_cur(
        cur_both, art.artifact_id, entry_path=art.path
    )
    assert any(
        "UPDATE dalton_shared_documents SET visibility = 'own'" in s
        for s in executed_both
    )
    assert any("DELETE FROM dalton_shared_document_members" in s for s in executed_both)


def test_shared_documents_and_monitors_queries_exclude_soft_deleted_workspaces() -> None:
    doc_sql = " ".join(shared_doc_service._SHARED_DOC_SELECT.split())
    mon_sql = " ".join(monitor_db._MONITOR_SELECT.split())
    assert "JOIN workspaces w ON w.workspace_id = l.workspace_id AND w.status <> 'deleted'" in doc_sql
    assert "JOIN workspaces w ON w.workspace_id = l.workspace_id AND w.status <> 'deleted'" in mon_sql


@pytest.mark.asyncio
async def test_public_monitor_detail_exposes_safe_entry_file_type_and_blocks_root_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    md_mon = _make_monitor(
        slug="legacy-public-md",
        entry_path="reports/q4/memo.md",
        visibility="public",
        published_seq=2,
    )
    html_mon = _make_monitor(
        slug="public-html",
        entry_path="nested/index.html",
        visibility="public",
        published_seq=1,
    )
    pdf_mon = _make_monitor(
        slug="public-pdf",
        entry_path="slides/deck.pdf",
        visibility="public",
        published_seq=1,
    )
    by_slug = {
        md_mon.slug: md_mon,
        html_mon.slug: html_mon,
        pdf_mon.slug: pdf_mon,
    }
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(
            monitor_db.get_monitor_by_slug, side_effect=lambda slug: by_slug.get(slug)
        ),
    )
    monkeypatch.setattr(
        shared_doc_service,
        "get_shared_document_by_artifact_id",
        create_autospec(
            shared_doc_service.get_shared_document_by_artifact_id,
            return_value=None,
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "is_monitor_version_published",
        create_autospec(monitor_db.is_monitor_version_published, return_value=True),
    )

    files_in_version = {
        "reports/q4/memo.md": StoredVersionFile(
            user_id="owner-1",
            sha256="a" * 64,
            content_type="text/markdown",
            byte_len=12,
            content=b"# Q4 Report\n",
        ),
        "reports/q4/charts/rev.png": StoredVersionFile(
            user_id="owner-1",
            sha256="b" * 64,
            content_type="image/png",
            byte_len=7,
            content=b"nested!",
        ),
        "reports/shared/logo.png": StoredVersionFile(
            user_id="owner-1",
            sha256="d" * 64,
            content_type="image/png",
            byte_len=11,
            content=b"parent-logo",
        ),
        # Same-named files at workspace root must NEVER be returned as fallbacks for relative paths.
        "shared/logo.png": StoredVersionFile(
            user_id="owner-1",
            sha256="e" * 64,
            content_type="image/png",
            byte_len=10,
            content=b"root-decoy",
        ),
        "charts/other.png": StoredVersionFile(
            user_id="owner-1",
            sha256="c" * 64,
            content_type="image/png",
            byte_len=5,
            content=b"root!",
        ),
    }

    async def _get_vfile(_aid: str, _seq: int, path: str) -> StoredVersionFile | None:
        return files_in_version.get(path)

    monkeypatch.setattr(
        monitor_service.artifacts_db,
        "get_version_file",
        create_autospec(
            monitor_service.artifacts_db.get_version_file, side_effect=_get_vfile
        ),
    )

    app = FastAPI()
    app.include_router(monitors_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        r_md = await client.get("/api/v1/dalton/monitors/public/legacy-public-md")
        assert r_md.status_code == 200
        body_md = r_md.json()
        assert "entry_path" not in body_md["monitor"]
        assert "workspace_id" not in body_md["monitor"]
        assert body_md["versions"] == []
        assert body_md["monitor"]["entry_file_type"] == "markdown"

        r_html = await client.get("/api/v1/dalton/monitors/public/public-html")
        assert r_html.status_code == 200
        assert r_html.json()["monitor"]["entry_file_type"] == "html"

        r_pdf = await client.get("/api/v1/dalton/monitors/public/public-pdf")
        assert r_pdf.status_code == 200
        assert r_pdf.json()["monitor"]["entry_file_type"] == "other"

        # Entry file and nested relative image resolve under reports/q4/
        r_entry = await client.get("/api/v1/dalton/monitors/public/legacy-public-md/v/2/")
        assert r_entry.status_code == 200
        assert r_entry.content == b"# Q4 Report\n"

        r_img = await client.get(
            "/api/v1/dalton/monitors/public/legacy-public-md/v/2/charts/rev.png"
        )
        assert r_img.status_code == 200
        assert r_img.content == b"nested!"

        # Parent-relative ../shared/logo.png from reports/q4/memo.md resolves to reports/shared/logo.png
        r_parent_head = await client.head(
            "/api/v1/dalton/monitors/public/legacy-public-md/v/2/",
            params={"rel": "../shared/logo.png"},
        )
        assert r_parent_head.status_code == 200
        r_parent_img = await client.get(
            "/api/v1/dalton/monitors/public/legacy-public-md/v/2/",
            params={"rel": "../shared/logo.png"},
        )
        assert r_parent_img.status_code == 200
        assert r_parent_img.content == b"parent-logo"

        # Same-named file at workspace root (shared/logo.png or charts/other.png) is NEVER returned for entry-relative paths.
        r_root_decoy_rel = await client.get(
            "/api/v1/dalton/monitors/public/legacy-public-md/v/2/",
            params={"rel": "shared/logo.png"},
        )
        assert r_root_decoy_rel.status_code == 404

        r_escape_rel = await client.get(
            "/api/v1/dalton/monitors/public/legacy-public-md/v/2/",
            params={"rel": "../../../etc/passwd.png"},
        )
        assert r_escape_rel.status_code == 404

        # Missing nested file does NOT fall back to workspace root charts/other.png
        r_root_fallback = await client.get(
            "/api/v1/dalton/monitors/public/legacy-public-md/v/2/charts/other.png"
        )
        assert r_root_fallback.status_code == 404


@pytest.mark.asyncio
async def test_shared_document_auto_update_after_turn_reuses_permission_and_pause_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton import settings as dalton_settings
    from src.server.services.artifacts import snapshot as snapshot_svc

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    monkeypatch.setattr(
        dalton_settings,
        "is_dalton_sharing_auth_enabled",
        create_autospec(dalton_settings.is_dalton_sharing_auth_enabled, return_value=True),
    )

    doc_ok = _make_doc(
        doc_id="70000000-0000-4000-8000-000000000601",
        artifact_id="71000000-0000-4000-8000-000000000601",
        owner_user_id="owner-internal",
        visibility="company",
        keep_updated=True,
        published_seq=1,
    )
    doc_lost_admin = _make_doc(
        doc_id="70000000-0000-4000-8000-000000000602",
        artifact_id="71000000-0000-4000-8000-000000000602",
        owner_user_id="demoted-user",
        visibility="public",
        keep_updated=True,
        published_seq=1,
    )
    art_ok = _make_artifact(artifact_id=doc_ok.artifact_id, latest_seq=1)
    snap_ok = Snapshot(
        entries=[ManifestEntry("notes/q4-memo.md", 14, "entry")],
        left_out=[],
        files=[
            VersionFile(
                path="notes/q4-memo.md",
                sha256="f" * 64,
                content_type="text/markdown",
                byte_len=14,
                content=b"# Updated v2!\n",
            )
        ],
        blobs={},
        title="Q4 Memo",
    )

    tiers = {"owner-internal": "internal", "demoted-user": "internal"}

    async def _tier(uid: str | None) -> str:
        return tiers.get(str(uid or ""), "external")

    monkeypatch.setattr(
        shared_doc_service,
        "get_user_tier",
        create_autospec(shared_doc_service.get_user_tier, side_effect=_tier),
    )
    monkeypatch.setattr(
        shared_doc_service,
        "list_workspace_auto_update_shared_documents",
        create_autospec(
            shared_doc_service.list_workspace_auto_update_shared_documents,
            return_value=[doc_ok, doc_lost_admin],
        ),
    )
    ws_running = {"workspace_id": WS_ID, "user_id": "owner-internal", "status": "running", "config": {}}
    ws_paused = {
        "workspace_id": WS_ID,
        "user_id": "owner-internal",
        "status": "running",
        "config": {"pending_outbound_confirm": {"token": "tok-1", "reason": "colleague_outbound_blocked"}},
    }
    ws_spy = create_autospec(shared_doc_service.db_get_workspace, return_value=ws_running)
    monkeypatch.setattr(shared_doc_service, "db_get_workspace", ws_spy)
    monkeypatch.setattr(
        shared_doc_service.artifacts_db,
        "get_artifact",
        create_autospec(shared_doc_service.artifacts_db.get_artifact, return_value=art_ok),
    )
    monkeypatch.setattr(
        shared_doc_service.artifact_snapshot,
        "capture_workspace_monitor_snapshot",
        create_autospec(
            shared_doc_service.artifact_snapshot.capture_workspace_monitor_snapshot,
            return_value=snap_ok,
        ),
    )
    monkeypatch.setattr(
        monitor_service,
        "_abort_if_absent_dependencies",
        create_autospec(monitor_service._abort_if_absent_dependencies, return_value=None),
    )
    commit_spy = create_autospec(
        shared_doc_service.commit_shared_document_version_tx,
        return_value=(doc_ok, PublishResult(artifact=art_ok, seq=2, cut=True, shared_now=False)),
    )
    monkeypatch.setattr(
        shared_doc_service,
        "commit_shared_document_version_tx",
        commit_spy,
    )

    # 1) Turn-end hook runs shared documents auto-update: doc_ok cuts v2, doc_lost_admin is blocked.
    monkeypatch.setattr(
        monitor_service,
        "republish_workspace_monitors_after_turn",
        create_autospec(monitor_service.republish_workspace_monitors_after_turn, return_value=None),
    )
    monkeypatch.setattr(
        snapshot_svc.artifacts_db,
        "list_kept_updated",
        create_autospec(snapshot_svc.artifacts_db.list_kept_updated, return_value=[]),
    )

    await snapshot_svc.keep_updated_after_turn(workspace_id=WS_ID, user_id="owner-internal")
    assert commit_spy.await_count == 1
    assert commit_spy.await_args_list[0].kwargs["doc_id"] == doc_ok.doc_id
    assert commit_spy.await_args_list[0].kwargs["check_pending_outbound_confirm"] is True

    # 2) When workspace is paused (pending_outbound_confirm), shared documents auto-update skips cleanly.
    commit_spy.reset_mock()
    ws_spy.return_value = ws_paused
    await shared_doc_service.republish_workspace_shared_documents_after_turn(workspace_id=WS_ID)
    commit_spy.assert_not_called()


@pytest.mark.asyncio
async def test_superseded_legacy_monitor_blocks_excluded_admin_read_republish_and_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager
    from psycopg import AsyncCursor

    from src.dalton.monitors.models import RepublishMonitorRequest, UpdateMonitorRequest
    from src.server.services import account as accounts_svc

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    tiers = {"owner-a": "internal", "admin-b": "admin", "colleague-c": "internal"}

    async def _tier(uid: str | None) -> str:
        return tiers.get(str(uid or ""), "external")

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
        shared_doc_service,
        "get_user_tier",
        create_autospec(shared_doc_service.get_user_tier, side_effect=_tier),
    )
    monkeypatch.setattr(
        accounts_svc,
        "read_standing",
        create_autospec(
            accounts_svc.read_standing,
            return_value=SimpleNamespace(suspended=False),
        ),
    )

    art = _make_artifact(
        artifact_id="71000000-0000-4000-8000-000000000701",
        path="notes/migrated.md",
        shared_at=NOW,
        shared_seq=2,
        latest_seq=2,
    )
    # Legacy monitor previously taken over by admin-b, now downgraded to visibility='own'
    # while admin-b is EXCLUDED from the new shared document owned by owner-a.
    legacy_mon = _make_monitor(
        monitor_id="60000000-0000-4000-8000-000000000701",
        slug="legacy-migrated-md",
        artifact_id=art.artifact_id,
        artifact_code=art.code,
        entry_path=art.path,
        owner_user_id="admin-b",
        visibility="own",
        keep_updated=False,
        published_seq=1,
        acl_revision=2,
    )
    new_doc = _make_doc(
        doc_id="70000000-0000-4000-8000-000000000701",
        artifact_id=art.artifact_id,
        artifact_code=art.code,
        entry_path=art.path,
        owner_user_id="owner-a",
        visibility="members",
        member_user_ids=("colleague-c",),
        published_seq=2,
        keep_updated=False,
    )

    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(monitor_db.get_monitor_by_slug, return_value=legacy_mon),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_id",
        create_autospec(monitor_db.get_monitor_by_id, return_value=legacy_mon),
    )
    monkeypatch.setattr(
        monitor_db,
        "list_monitor_versions",
        create_autospec(monitor_db.list_monitor_versions, return_value=[]),
    )
    monkeypatch.setattr(
        shared_doc_service,
        "get_shared_document_by_artifact_id",
        create_autospec(
            shared_doc_service.get_shared_document_by_artifact_id,
            return_value=new_doc,
        ),
    )
    update_tx_spy = create_autospec(monitor_db.update_monitor_settings_tx)
    delete_tx_spy = create_autospec(monitor_db.delete_monitor_tx)
    commit_ver_spy = create_autospec(monitor_db.commit_monitor_version_tx)
    monkeypatch.setattr(monitor_db, "update_monitor_settings_tx", update_tx_spy)
    monkeypatch.setattr(monitor_db, "delete_monitor_tx", delete_tx_spy)
    monkeypatch.setattr(monitor_db, "commit_monitor_version_tx", commit_ver_spy)

    # 1) Excluded admin-b cannot read detail, mint a read grant, use a minted grant,
    #    re-publicize via PATCH, republish, or delete via old monitor endpoints.
    for coro in (
        monitor_service.get_monitor_detail("legacy-migrated-md", viewer_id="admin-b"),
        monitor_service.mint_grant_for_monitor("legacy-migrated-md", viewer_id="admin-b"),
        monitor_service.update_monitor_settings(
            "legacy-migrated-md",
            UpdateMonitorRequest(visibility="public"),
            caller_id="admin-b",
        ),
        monitor_service.republish_workspace_monitor(
            "legacy-migrated-md",
            RepublishMonitorRequest(),
            caller_id="admin-b",
        ),
        monitor_service.delete_monitor_by_slug("legacy-migrated-md", caller_id="admin-b"),
    ):
        with pytest.raises(HTTPException) as exc:
            await coro
        assert exc.value.status_code == 403

    matching_grant = await monitor_grants.mint_monitor_grant(
        legacy_mon.monitor_id, "admin-b", acl_revision=legacy_mon.acl_revision
    )
    with pytest.raises(HTTPException) as exc_serve:
        await monitor_service.serve_monitor_file(matching_grant.token, "1", art.path)
    assert exc_serve.value.status_code == 403

    update_tx_spy.assert_not_called()
    delete_tx_spy.assert_not_called()
    commit_ver_spy.assert_not_called()

    # 2) New doc owner (owner-a) can still inspect legacy history read-only (can_manage=False),
    #    while old monitor write/delete endpoints remain blocked.
    history_detail = await monitor_service.get_monitor_detail(
        "legacy-migrated-md", viewer_id="owner-a"
    )
    assert history_detail["monitor"]["can_manage"] is False
    with pytest.raises(HTTPException) as exc_owner_del:
        await monitor_service.delete_monitor_by_slug("legacy-migrated-md", caller_id="owner-a")
    assert exc_owner_del.value.status_code == 403

    # 3) Same-transaction re-verification in db.py rejects PATCH / republish / DELETE under lock
    #    without mutating dalton_monitors or unsharing/re-sharing share_links.
    monkeypatch.undo()
    executed_tx: list[str] = []
    cur = create_autospec(AsyncCursor, instance=True)
    cur._row = None
    cur.rowcount = 0

    async def _exec_tx(sql: str, _params: Any = None) -> None:
        norm = " ".join(sql.split())
        executed_tx.append(norm)
        if "FROM dalton_monitors" in norm:
            cur._row = {
                "monitor_id": legacy_mon.monitor_id,
                "title": legacy_mon.title,
                "category": legacy_mon.category,
                "sort_order": 0,
                "visibility": "own",
                "keep_updated": False,
                "published_seq": 1,
                "owner_user_id": "admin-b",
                "source_kind": "workspace",
                "source_ref": legacy_mon.source_ref,
                "slug": legacy_mon.slug,
            }
        elif "FROM dalton_shared_documents" in norm:
            cur._row = {"?column?": 1}
        else:
            cur._row = None

    cur.execute.side_effect = _exec_tx
    cur.fetchone.side_effect = lambda: cur._row
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = False

    class _FakeTx:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args: Any) -> bool:
            return False

    class _FakeConn:
        def transaction(self) -> _FakeTx:
            return _FakeTx()

        def cursor(self, **_kwargs: Any) -> Any:
            return cur

    @asynccontextmanager
    async def _fake_conn():
        yield _FakeConn()

    monkeypatch.setattr(monitor_db, "get_db_connection", _fake_conn)
    monkeypatch.setattr(
        monitor_db.artifacts_records_db,
        "lock_owned",
        create_autospec(monitor_db.artifacts_records_db.lock_owned, return_value=art),
    )
    unshare_spy = create_autospec(monitor_db.artifacts_records_db.unshare_in_cur)
    monkeypatch.setattr(monitor_db.artifacts_records_db, "unshare_in_cur", unshare_spy)
    share_pub_spy = create_autospec(monitor_db._share_existing_version_publicly_in_cur)
    monkeypatch.setattr(
        monitor_db, "_share_existing_version_publicly_in_cur", share_pub_spy
    )

    with pytest.raises(HTTPException) as exc_upd_tx:
        await monitor_db.update_monitor_settings_tx(
            legacy_mon.monitor_id,
            artifact_id=art.artifact_id,
            owner_user_id="owner-a",
            caller_id="admin-b",
            visibility="public",
        )
    assert exc_upd_tx.value.status_code == 403
    share_pub_spy.assert_not_called()

    with pytest.raises(HTTPException) as exc_del_tx:
        await monitor_db.delete_monitor_tx(
            legacy_mon.monitor_id,
            artifact_id=art.artifact_id,
            workspace_owner_id="owner-a",
            caller_id="admin-b",
        )
    assert exc_del_tx.value.status_code == 403
    unshare_spy.assert_not_called()
    assert not any("DELETE FROM dalton_monitors" in s for s in executed_tx)
    assert not any("UPDATE dalton_monitors" in s for s in executed_tx)

    with pytest.raises(HTTPException) as exc_ver_tx:
        await monitor_db.commit_monitor_version_tx(
            monitor_id=legacy_mon.monitor_id,
            artifact_id=art.artifact_id,
            workspace_owner_id="owner-a",
            files=[],
            title=legacy_mon.title,
            caller_user_id="admin-b",
        )
    assert exc_ver_tx.value.status_code == 403


@pytest.mark.asyncio
async def test_legacy_public_link_migrated_with_keep_updated_false_stops_turn_end_advances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager
    from dataclasses import replace
    from psycopg import AsyncCursor

    from src.server.services.artifacts import snapshot as snapshot_svc

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    legacy_public_art = _make_artifact(
        artifact_id="71000000-0000-4000-8000-000000000801",
        path="reports/public-note.md",
        shared_at=NOW,
        shared_seq=1,
        latest_seq=1,
        keep_updated=True,
    )
    assert legacy_public_art.follows_turns is True

    state: dict[str, Any] = {"artifact": legacy_public_art}
    cur = create_autospec(AsyncCursor, instance=True)
    cur._row = None

    async def _exec(sql: str, _params: Any = None) -> None:
        norm = " ".join(sql.split())
        if "FROM dalton_monitors" in norm:
            cur._row = None
        elif "INSERT INTO dalton_shared_documents" in norm:
            cur._row = {"doc_id": "70000000-0000-4000-8000-000000000801"}
        elif "WHERE d.doc_id = %s::uuid" in norm:
            cur._row = {
                "doc_id": "70000000-0000-4000-8000-000000000801",
                "artifact_id": legacy_public_art.artifact_id,
                "owner_user_id": "admin-1",
                "title": "Public Note",
                "visibility": "public",
                "published_seq": 2,
                "keep_updated": False,
                "acl_revision": 1,
                "created_at": NOW,
                "updated_at": NOW,
                "artifact_code": legacy_public_art.code,
                "workspace_id": WS_ID,
                "entry_path": legacy_public_art.path,
                "owner_name": "Admin",
                "owner_email": "admin@company.com",
                "member_user_ids": [],
            }
        else:
            cur._row = None

    cur.execute.side_effect = _exec
    cur.fetchone.side_effect = lambda: cur._row
    cur.fetchall.return_value = []
    cur.executemany.return_value = None
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = False

    class _FakeTx:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args: Any) -> bool:
            return False

    class _FakeConn:
        def transaction(self) -> _FakeTx:
            return _FakeTx()

        def cursor(self, **_kwargs: Any) -> Any:
            return cur

    @asynccontextmanager
    async def _fake_conn():
        yield _FakeConn()

    async def _fake_set_record_in_cur(
        _cur: Any,
        *,
        user_id: str,
        artifact_id: str,
        assignments: str,
        params: dict[str, Any],
    ) -> Artifact:
        assert user_id == "admin-1"
        assert artifact_id == legacy_public_art.artifact_id
        if "keep_updated = FALSE" in assignments:
            state["artifact"] = replace(state["artifact"], keep_updated=False)
        return state["artifact"]

    async def _fake_cut_monitor_in_cur(
        _cur: Any, locked: Artifact, **_kwargs: Any
    ) -> PublishResult:
        # Must already have share_links.keep_updated=False inside the same transaction.
        assert locked.keep_updated is False
        updated = replace(locked, latest_seq=2, shared_seq=2)
        state["artifact"] = updated
        return PublishResult(artifact=updated, seq=2, cut=True, shared_now=False)

    monkeypatch.setattr(shared_doc_service, "get_db_connection", _fake_conn)
    monkeypatch.setattr(
        shared_doc_service.artifacts_records_db,
        "lock_owned",
        create_autospec(
            shared_doc_service.artifacts_records_db.lock_owned,
            side_effect=lambda *_a, **_kw: state["artifact"],
        ),
    )
    set_record_spy = create_autospec(
        shared_doc_service.artifacts_records_db.set_record_in_cur,
        side_effect=_fake_set_record_in_cur,
    )
    monkeypatch.setattr(
        shared_doc_service.artifacts_records_db, "set_record_in_cur", set_record_spy
    )
    monkeypatch.setattr(
        monitor_db,
        "_resolve_live_tier_in_cur",
        create_autospec(monitor_db._resolve_live_tier_in_cur, return_value="admin"),
    )
    monkeypatch.setattr(
        shared_doc_service.artifacts_versions_db,
        "cut_monitor_in_cur",
        create_autospec(
            shared_doc_service.artifacts_versions_db.cut_monitor_in_cur,
            side_effect=_fake_cut_monitor_in_cur,
        ),
    )

    doc, res = await shared_doc_service.commit_shared_document_publish_tx(
        artifact_id=legacy_public_art.artifact_id,
        workspace_owner_id="admin-1",
        doc_owner_id="admin-1",
        title="Public Note",
        visibility="public",
        keep_updated=False,
        member_user_ids=[],
        files=[
            VersionFile(
                path=legacy_public_art.path,
                sha256="1" * 64,
                content_type="text/markdown",
                byte_len=12,
                content=b"# v2 public\n",
            )
        ],
    )
    assert doc.keep_updated is False
    assert res.seq == 2
    set_record_spy.assert_awaited_once()
    assert state["artifact"].keep_updated is False
    assert state["artifact"].follows_turns is False

    # Turn-end refresh now skips both the shared-documents path and legacy share_links path.
    monkeypatch.setattr(
        monitor_service,
        "republish_workspace_monitors_after_turn",
        create_autospec(monitor_service.republish_workspace_monitors_after_turn, return_value=None),
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
        snapshot_svc.artifacts_db,
        "list_kept_updated",
        create_autospec(
            snapshot_svc.artifacts_db.list_kept_updated,
            side_effect=lambda _ws: [state["artifact"]] if state["artifact"].follows_turns else [],
        ),
    )
    keep_one_spy = create_autospec(snapshot_svc._keep_one)
    monkeypatch.setattr(snapshot_svc, "_keep_one", keep_one_spy)
    commit_doc_spy = create_autospec(shared_doc_service.commit_shared_document_version_tx)
    monkeypatch.setattr(
        shared_doc_service, "commit_shared_document_version_tx", commit_doc_spy
    )

    await snapshot_svc.keep_updated_after_turn(workspace_id=WS_ID, user_id="admin-1")
    keep_one_spy.assert_not_called()
    commit_doc_spy.assert_not_called()


@pytest.mark.asyncio
async def test_managed_shared_document_rejects_generic_keep_updated_and_blocks_turn_version_public_advance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager
    from psycopg import AsyncCursor

    from src.server.app.artifacts import records as artifact_records_app
    from src.server.app.artifacts.models import KeepUpdatedRequest
    from src.server.database.artifacts import versions as artifact_versions_db
    from src.server.services.shared_workspaces.access import Access

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    managed_art = _make_artifact(
        artifact_id="71000000-0000-4000-8000-000000000811",
        path="reports/managed.md",
        shared_at=NOW,
        shared_seq=2,
        latest_seq=2,
        keep_updated=False,
        files=("reports/managed.md",),
    )
    managed_doc = _make_doc(
        doc_id="70000000-0000-4000-8000-000000000811",
        artifact_id=managed_art.artifact_id,
        owner_user_id="admin-1",
        entry_path=managed_art.path,
        visibility="public",
        published_seq=2,
        keep_updated=False,
    )

    monkeypatch.setattr(
        artifact_records_app,
        "reached",
        create_autospec(
            artifact_records_app.reached,
            return_value=(managed_art, Access("admin-1")),
        ),
    )
    monkeypatch.setattr(
        artifact_records_app,
        "_require_non_external_for_publish",
        create_autospec(
            artifact_records_app._require_non_external_for_publish, return_value=None
        ),
    )
    monkeypatch.setattr(
        artifact_records_app,
        "_require_admin_for_public_publish",
        create_autospec(
            artifact_records_app._require_admin_for_public_publish, return_value=None
        ),
    )
    monkeypatch.setattr(
        artifact_records_app,
        "refuse_link_change",
        create_autospec(artifact_records_app.refuse_link_change, return_value=None),
    )
    monkeypatch.setattr(
        shared_doc_service,
        "get_shared_document_by_artifact_id",
        create_autospec(
            shared_doc_service.get_shared_document_by_artifact_id,
            return_value=managed_doc,
        ),
    )
    set_keep_spy = create_autospec(
        artifact_records_app.artifacts_db.set_keep_updated,
        return_value=managed_art,
    )
    monkeypatch.setattr(
        artifact_records_app.artifacts_db, "set_keep_updated", set_keep_spy
    )
    monkeypatch.setattr(
        artifact_records_app,
        "_changed",
        create_autospec(
            artifact_records_app._changed,
            return_value=SimpleNamespace(keep_updated=False),
        ),
    )

    # 1) Generic PATCH /artifacts/{id} refuses keep_updated=True with 409 once taken over.
    with pytest.raises(HTTPException) as exc_patch:
        await artifact_records_app.update_artifact(
            managed_art.artifact_id,
            KeepUpdatedRequest(keep_updated=True),
            x_user_id="admin-1",
        )
    assert exc_patch.value.status_code == 409
    assert exc_patch.value.detail == shared_doc_service.SHARED_DOCUMENT_TAKEOVER_KEEP_UPDATED_DETAIL
    set_keep_spy.assert_not_called()

    # Turning keep_updated OFF stays open for cleanup.
    await artifact_records_app.update_artifact(
        managed_art.artifact_id,
        KeepUpdatedRequest(keep_updated=False),
        x_user_id="admin-1",
    )
    set_keep_spy.assert_awaited_once()

    # 2) Even if share_links.keep_updated is True, turn_version checks dalton_shared_documents
    #    under FOR SHARE in the same commit transaction and never advances /a/ public shared_seq.
    from dataclasses import replace

    cur = create_autospec(AsyncCursor, instance=True)
    executed_sql: list[str] = []

    async def _exec(sql: str, _params: Any = None) -> None:
        norm = " ".join(sql.split())
        executed_sql.append(norm)
        if "FROM dalton_shared_documents" in norm:
            cur._row = {"?column?": 1}
        else:
            cur._row = None

    cur.execute.side_effect = _exec
    cur.fetchone.side_effect = lambda: getattr(cur, "_row", None)

    re_enabled_art = replace(managed_art, keep_updated=True, kept_at=None)
    state: dict[str, Artifact] = {"locked": re_enabled_art}

    @asynccontextmanager
    async def _fake_locked(_user_id: str, _artifact_id: str):
        yield cur, state["locked"]

    monkeypatch.setattr(artifact_versions_db, "_locked", _fake_locked)
    cut_spy = create_autospec(
        artifact_versions_db._cut,
        return_value=PublishResult(artifact=re_enabled_art, seq=3, cut=True, shared_now=False),
    )
    monkeypatch.setattr(artifact_versions_db, "_cut", cut_spy)

    vfile = VersionFile(
        path=managed_art.path,
        sha256="9" * 64,
        content_type="text/markdown",
        byte_len=10,
        content=b"# v3 edit\n",
    )
    res_unkept = await artifact_versions_db.turn_version(
        user_id="admin-1",
        artifact_id=managed_art.artifact_id,
        read=artifact_versions_db.ReadBasis.of(re_enabled_art),
        files=[vfile],
        title="Managed",
    )
    assert res_unkept.cut is False
    assert res_unkept.seq == 2
    cut_spy.assert_not_called()
    assert any("FROM dalton_shared_documents" in s and "FOR SHARE" in s for s in executed_sql)

    # 3) If the artifact is also privately kept (kept_at set), turn_version cuts private only (to_visitors=False).
    kept_art = replace(re_enabled_art, kept_at=NOW)
    state["locked"] = kept_art
    res_kept = await artifact_versions_db.turn_version(
        user_id="admin-1",
        artifact_id=managed_art.artifact_id,
        read=artifact_versions_db.ReadBasis.of(kept_art),
        files=[vfile],
        title="Managed",
    )
    assert res_kept.cut is True
    cut_spy.assert_awaited_once()
    assert cut_spy.await_args.kwargs["to_visitors"] is False


@pytest.mark.asyncio
async def test_superseded_monitor_turn_refresh_skips_without_recording_publish_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager
    from psycopg import AsyncCursor

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    legacy_mon = _make_monitor(
        monitor_id="60000000-0000-4000-8000-000000000821",
        slug="legacy-racing-md",
        artifact_id="71000000-0000-4000-8000-000000000821",
        entry_path="notes/racing.md",
        owner_user_id="owner-1",
        visibility="company",
        keep_updated=True,
        published_seq=1,
    )
    art = _make_artifact(
        artifact_id=legacy_mon.artifact_id,
        path=legacy_mon.entry_path,
        latest_seq=1,
    )
    snap = Snapshot(
        entries=[ManifestEntry("notes/racing.md", 12, "entry")],
        left_out=[],
        files=[
            VersionFile(
                path="notes/racing.md",
                sha256="8" * 64,
                content_type="text/markdown",
                byte_len=12,
                content=b"# updated\n",
            )
        ],
        blobs={},
        title="Racing",
    )

    monkeypatch.setattr(
        monitor_db,
        "list_workspace_auto_update_monitors",
        create_autospec(
            monitor_db.list_workspace_auto_update_monitors,
            return_value=[legacy_mon],
        ),
    )
    monkeypatch.setattr(
        monitor_service,
        "db_get_workspace",
        create_autospec(
            monitor_service.db_get_workspace,
            return_value={"workspace_id": WS_ID, "user_id": "owner-1", "status": "running", "config": {}},
        ),
    )
    monkeypatch.setattr(
        monitor_service,
        "_get_superseded_shared_document",
        create_autospec(
            monitor_service._get_superseded_shared_document, return_value=None
        ),
    )
    monkeypatch.setattr(
        monitor_service,
        "get_user_tier",
        create_autospec(monitor_service.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        monitor_service.artifacts_db,
        "get_artifact",
        create_autospec(monitor_service.artifacts_db.get_artifact, return_value=art),
    )
    monkeypatch.setattr(
        monitor_service.artifact_snapshot,
        "capture_workspace_monitor_snapshot",
        create_autospec(
            monitor_service.artifact_snapshot.capture_workspace_monitor_snapshot,
            return_value=snap,
        ),
    )
    monkeypatch.setattr(
        monitor_service,
        "_abort_if_absent_dependencies",
        create_autospec(monitor_service._abort_if_absent_dependencies, return_value=None),
    )
    # Takeover happens after snapshot read starts: commit raises MonitorSupersededBySharedDocumentError.
    monkeypatch.setattr(
        monitor_service,
        "cut_monitor_version",
        create_autospec(
            monitor_service.cut_monitor_version,
            side_effect=monitor_db.MonitorSupersededBySharedDocumentError(),
        ),
    )
    record_fail_spy = create_autospec(monitor_db.record_monitor_publish_failure)
    monkeypatch.setattr(monitor_db, "record_monitor_publish_failure", record_fail_spy)

    await monitor_service.republish_workspace_monitors_after_turn(workspace_id=WS_ID)
    record_fail_spy.assert_not_called()

    # record_monitor_publish_failure itself re-checks dalton_shared_documents in its UPDATE guard.
    monkeypatch.undo()
    executed_sql: list[str] = []
    cur = create_autospec(AsyncCursor, instance=True)

    async def _exec_fail(sql: str, _params: Any = None) -> None:
        executed_sql.append(" ".join(sql.split()))

    cur.execute.side_effect = _exec_fail
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = False

    class _FakeConn:
        def cursor(self, **_kwargs: Any) -> Any:
            return cur

    @asynccontextmanager
    async def _fake_conn():
        yield _FakeConn()

    monkeypatch.setattr(monitor_db, "get_db_connection", _fake_conn)
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_id",
        create_autospec(monitor_db.get_monitor_by_id, return_value=legacy_mon),
    )

    await monitor_db.record_monitor_publish_failure(
        legacy_mon.monitor_id, error_message="some transient error"
    )
    assert any(
        "UPDATE dalton_monitors SET last_publish_error" in s
        and "FROM dalton_shared_documents d" in s
        and "WHERE d.artifact_id = dalton_monitors.artifact_id" in s
        for s in executed_sql
    )
