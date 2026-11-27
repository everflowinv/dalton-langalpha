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
        names_a = [d["file_name"] for d in resp_a.json()["documents"]]
        assert names_a == ["for-a.md", "valuation.xlsx", "public.pdf", "deck.pdf"]
        assert resp_a.json()["documents"][0]["url"] == "/a/memcode12345"

    # Colleague B (NOT listed in members_doc) does NOT see for-a.md
    async with AsyncClient(transport=ASGITransport(app=_build_app("colleague-b")), base_url="http://t") as client:
        resp_b = await client.get("/api/v1/dalton/monitors/shared-documents")
        assert resp_b.status_code == 200
        names_b = [d["file_name"] for d in resp_b.json()["documents"]]
        assert names_b == ["valuation.xlsx", "public.pdf", "deck.pdf"]

    # External member only sees public.pdf (never members/company/own)
    async with AsyncClient(transport=ASGITransport(app=_build_app("ext-1")), base_url="http://t") as client:
        resp_ext = await client.get("/api/v1/dalton/monitors/shared-documents")
        assert resp_ext.status_code == 200
        names_ext = [d["file_name"] for d in resp_ext.json()["documents"]]
        assert names_ext == ["public.pdf"]


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
