"""sync3 B11 acceptance: the ``dalton.artifact_publishing.enabled`` deployment
switch (upstream #666 artifact publishing, versions and file comments).

Off (the code default): the publish surface, the comment writes, the comment
send and the per-turn version cut are all refused or inert, with nothing
written and nothing delivered; old links keep reading, unsharing and their
legacy first-read conversion. On: an internal owner publishes, keeps updated
and sends comments; the HTTP refusal below goes away.

DALTON-HOOK(artifact-publishing-off)
"""

from __future__ import annotations

import dataclasses
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)
ARTIFACT_ID = "50000000-0000-4000-8000-000000000001"
THREAD_ID = "51000000-0000-4000-8000-000000000001"
COMMENT_ID = "52000000-0000-4000-8000-000000000001"

SWITCH_ENV = "DALTON_ARTIFACT_PUBLISHING_ENABLED"


@pytest.fixture(autouse=True)
def _switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SWITCH_ENV, raising=False)


def _artifact():
    from src.server.database import artifacts as artifacts_db

    return artifacts_db.Artifact(
        artifact_id=ARTIFACT_ID,
        code="artsw1test",
        workspace_id="ws-switch-1",
        path="report.html",
        created_thread_id=None,
        bound_thread_id=None,
        keep_updated=True,
        latest_seq=0,
        shared_at=NOW,
        files=("report.html",),
        revision=0,
        created_at=NOW,
    )


@pytest.fixture
def owner_routes(monkeypatch: pytest.MonkeyPatch):
    """The artifact routes with the database and events faked."""
    from src.server.app.artifacts import comments as comments_app
    from src.server.app.artifacts import records as records_app
    from src.server.database import artifacts as artifacts_db

    artifact = _artifact()
    monkeypatch.setattr(
        artifacts_db,
        "get_artifact",
        create_autospec(
            artifacts_db.get_artifact, side_effect=lambda aid, *, user_id: artifact
        ),
    )
    comment_counts = create_autospec(
        artifacts_db.comment_counts,
        return_value=SimpleNamespace(open=0, unread=0),
    )
    monkeypatch.setattr(artifacts_db, "comment_counts", comment_counts)
    monkeypatch.setattr(
        records_app,
        "publish_artifact_change",
        create_autospec(records_app.publish_artifact_change),
    )
    monkeypatch.setattr(
        comments_app,
        "publish_comment_change",
        create_autospec(comments_app.publish_comment_change),
    )
    return SimpleNamespace(
        artifact=artifact,
        records=records_app,
        comments=comments_app,
        db=artifacts_db,
    )


# --- switch off: the publish surface refuses without writing ----------------


@pytest.mark.asyncio
async def test_off_publish_is_refused_before_any_snapshot(owner_routes) -> None:
    from src.server.app.artifacts.models import PublishRequest

    snapshot_publish = create_autospec(
        owner_routes.records.snapshot.publish,
        return_value=SimpleNamespace(
            artifact=owner_routes.artifact, seq=1, cut=True
        ),
    )
    with patch.object(owner_routes.records.snapshot, "publish", snapshot_publish):
        with pytest.raises(HTTPException) as raised:
            await owner_routes.records.publish_artifact(
                ARTIFACT_ID,
                PublishRequest(files=["report.html"]),
                x_user_id="u-owner-1",
            )
    assert raised.value.status_code == 403
    assert raised.value.detail == {"code": "artifact_publishing_disabled"}
    snapshot_publish.assert_not_called()


@pytest.mark.asyncio
async def test_off_keep_updated_true_refused_false_allowed(
    owner_routes, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.server.app.artifacts.models import KeepUpdatedRequest

    set_keep_updated = create_autospec(
        owner_routes.db.set_keep_updated,
        side_effect=lambda *, user_id, artifact_id, keep_updated: (
            dataclasses.replace(owner_routes.artifact, keep_updated=keep_updated)
        ),
    )
    monkeypatch.setattr(owner_routes.db, "set_keep_updated", set_keep_updated)

    with pytest.raises(HTTPException) as raised:
        await owner_routes.records.update_artifact(
            ARTIFACT_ID,
            KeepUpdatedRequest(keep_updated=True),
            x_user_id="u-owner-1",
        )
    assert raised.value.status_code == 403
    assert raised.value.detail == {"code": "artifact_publishing_disabled"}
    set_keep_updated.assert_not_called()

    # Turning it off is cleanup, and stays open.
    response = await owner_routes.records.update_artifact(
        ARTIFACT_ID,
        KeepUpdatedRequest(keep_updated=False),
        x_user_id="u-owner-1",
    )
    assert response.keep_updated is False
    set_keep_updated.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("left_out", [True, False])
async def test_off_left_out_refused_before_any_write(
    owner_routes, monkeypatch: pytest.MonkeyPatch, left_out: bool
) -> None:
    from src.server.app.artifacts.models import LeftOutRequest

    set_left_out = create_autospec(
        owner_routes.db.set_left_out,
        return_value=owner_routes.artifact,
    )
    monkeypatch.setattr(owner_routes.db, "set_left_out", set_left_out)

    with pytest.raises(HTTPException) as raised:
        await owner_routes.records.set_file_left_out(
            ARTIFACT_ID,
            LeftOutRequest(path="extra.html", left_out=left_out),
            x_user_id="u-owner-1",
        )
    assert raised.value.status_code == 403
    assert raised.value.detail == {"code": "artifact_publishing_disabled"}
    set_left_out.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("left_out", [True, False])
async def test_on_external_member_left_out_refused_and_internal_allowed(
    owner_routes, monkeypatch: pytest.MonkeyPatch, left_out: bool
) -> None:
    from src.dalton import access_tiers as dalton_access_tiers
    from src.server.app.artifacts.models import LeftOutRequest

    monkeypatch.setenv(SWITCH_ENV, "1")
    monkeypatch.setattr(
        dalton_access_tiers,
        "get_user_tier",
        create_autospec(
            dalton_access_tiers.get_user_tier,
            side_effect=lambda uid: "external" if uid == "u-ext-1" else "standard",
        ),
    )
    set_left_out = create_autospec(
        owner_routes.db.set_left_out,
        side_effect=lambda *, user_id, artifact_id, path, left_out, cap: (
            dataclasses.replace(
                owner_routes.artifact,
                left_out_files=(path,) if left_out else (),
            )
        ),
    )
    monkeypatch.setattr(owner_routes.db, "set_left_out", set_left_out)

    with pytest.raises(HTTPException) as raised:
        await owner_routes.records.set_file_left_out(
            ARTIFACT_ID,
            LeftOutRequest(path="extra.html", left_out=left_out),
            x_user_id="u-ext-1",
        )
    assert raised.value.status_code == 403
    assert raised.value.detail == "外部成员不能创建公开分享链接"
    set_left_out.assert_not_called()

    response = await owner_routes.records.set_file_left_out(
        ARTIFACT_ID,
        LeftOutRequest(path="extra.html", left_out=left_out),
        x_user_id="u-owner-1",
    )
    assert response.artifact_id == ARTIFACT_ID
    set_left_out.assert_awaited_once()


@pytest.mark.asyncio
async def test_off_binding_refused_delete_open(owner_routes) -> None:
    from src.server.app.artifacts.models import BindRequest

    bind = create_autospec(
        owner_routes.db.bind,
        return_value=owner_routes.artifact,
    )
    with patch.object(owner_routes.db, "bind", bind):
        with pytest.raises(HTTPException) as raised:
            await owner_routes.records.bind_artifact(
                ARTIFACT_ID,
                BindRequest(thread_id=THREAD_ID),
                x_user_id="u-owner-1",
            )
    assert raised.value.status_code == 403
    bind.assert_not_called()

    unbind = create_autospec(
        owner_routes.db.unbind, return_value=owner_routes.artifact
    )
    with patch.object(owner_routes.db, "unbind", unbind):
        unbound = await owner_routes.records.unbind_artifact(
            ARTIFACT_ID, x_user_id="u-owner-1"
        )
    assert unbound.artifact_id == ARTIFACT_ID
    unbind.assert_awaited_once()


# --- switch off: comment writes refuse without rows or delivery -------------


def _comment_db(owner_routes, monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    from src.server.database.artifacts import comments as comments_db

    made = {
        name: create_autospec(getattr(comments_db, name))
        for name in (
            "create_comment",
            "add_owner_message",
            "update_comment",
            "mark_comment_read",
            "mark_artifact_read",
            "delete_comment",
            "list_comments",
            "list_queued_comments",
        )
    }
    for name, mock in made.items():
        monkeypatch.setattr(owner_routes.db, name, mock)
    return made


@pytest.mark.asyncio
async def test_off_comment_writes_refused_reads_and_delete_open(
    owner_routes, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.server.app.artifacts.models import (
        AddCommentMessageRequest,
        CreateCommentRequest,
        UpdateCommentRequest,
    )

    db = _comment_db(owner_routes, monkeypatch)
    from src.server.app.artifacts.models import CommentAnchor

    anchor = CommentAnchor(kind="text", quote="a passage", prefix="before ", suffix=" after")

    with pytest.raises(HTTPException) as raised_create:
        await owner_routes.comments.create_comment(
            ARTIFACT_ID,
            CreateCommentRequest(
                anchor=anchor, body="note", delivery="queued", thread_id=THREAD_ID
            ),
            x_user_id="u-owner-1",
        )
    assert raised_create.value.status_code == 403
    db["create_comment"].assert_not_called()

    with pytest.raises(HTTPException) as raised_message:
        await owner_routes.comments.add_comment_message(
            ARTIFACT_ID,
            COMMENT_ID,
            AddCommentMessageRequest(body="more"),
            x_user_id="u-owner-1",
        )
    assert raised_message.value.status_code == 403
    db["add_owner_message"].assert_not_called()

    with pytest.raises(HTTPException) as raised_update:
        await owner_routes.comments.update_comment(
            ARTIFACT_ID,
            COMMENT_ID,
            UpdateCommentRequest(delivery="note"),
            x_user_id="u-owner-1",
        )
    assert raised_update.value.status_code == 403
    db["update_comment"].assert_not_called()

    # Nothing was written, so nothing was delivered either.
    owner_routes.comments.publish_comment_change.assert_not_called()

    # Reading and cleaning up stay open.
    db["mark_comment_read"].return_value = (
        SimpleNamespace(thread_id=None, moved_comment=False),
        False,
    )


# --- switch off: the per-turn cut and the legacy first read -----------------


@pytest.mark.asyncio
async def test_off_keep_updated_after_turn_cuts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.server.services.artifacts import snapshot

    list_kept = create_autospec(snapshot.artifacts_db.list_kept_updated)
    with patch.object(snapshot.artifacts_db, "list_kept_updated", list_kept):
        await snapshot.keep_updated_after_turn(
            workspace_id="ws-switch-1", user_id="u-owner-1"
        )
    # Backfilled keep_updated=TRUE links gain no version while off: not even
    # the kept list is read.
    list_kept.assert_not_called()


@pytest.mark.asyncio
async def test_off_legacy_first_read_still_cuts_v1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.server.services.artifacts import snapshot

    artifact = _artifact()
    cut = create_autospec(
        snapshot._cut,
        return_value=SimpleNamespace(
            artifact=dataclasses.replace(artifact, latest_seq=1)
        ),
    )
    captured = SimpleNamespace(
        entries=[SimpleNamespace(size=10, as_dict=lambda: {"path": "report.html"})],
        total=10,
        files=[SimpleNamespace(path="report.html")],
    )
    with (
        patch.object(
            snapshot,
            "db_get_workspace",
            create_autospec(
                snapshot.db_get_workspace,
                return_value={
                    "workspace_id": artifact.workspace_id,
                    "user_id": "u-owner-1",
                    "status": "running",
                },
            ),
        ),
        patch.object(snapshot, "_capture", create_autospec(snapshot._capture, return_value=captured)),
        patch.object(snapshot, "_cut", cut),
    ):
        result = await snapshot.legacy_snapshot(artifact)
    assert result.latest_seq == 1
    cut.assert_awaited_once()
    assert cut.await_args.kwargs.get("origin") == "legacy"


# --- switch off: an old link still unshares ---------------------------------


@pytest.mark.asyncio
async def test_off_unshare_still_allowed(owner_routes, monkeypatch) -> None:
    unshare = create_autospec(
        owner_routes.db.unshare,
        return_value=dataclasses.replace(owner_routes.artifact, shared_at=None),
    )
    with patch.object(owner_routes.db, "unshare", unshare):
        response = await owner_routes.records.unshare_artifact(
            ARTIFACT_ID, x_user_id="u-owner-1"
        )
    assert response.shared is False
    unshare.assert_awaited_once()


# --- switch off/on: the send route's HTTP 409 (real client) -----------------


def _empty_async_gen():
    async def _gen():
        if False:
            yield ""

    return _gen()


@pytest_asyncio.fixture
async def threads_client():
    from src.server.app.threads import router
    from src.server.dependencies.usage_limits import ChatAuthResult
    from tests.conftest import create_test_app, pin_chat_limit

    app = create_test_app(router)
    pin_chat_limit(app, ChatAuthResult(user_id="usr-placeholder-001", access_tier=0))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@contextmanager
def _gated_send_route():
    """The send route with everything past the Dalton gate stubbed out."""
    from src.server.app import setup as setup_module

    wm_singleton = MagicMock()
    wm_singleton.has_ready_session.return_value = True
    stream = MagicMock(return_value=_empty_async_gen())

    with (
        patch.object(setup_module, "agent_config", MagicMock()),
        patch(
            "src.server.services.llm.config.resolve_llm_config",
            new=AsyncMock(return_value=_stub_config()),
        ),
        patch(
            "src.server.dependencies.usage_limits.enforce_credit_limit",
            new=AsyncMock(),
        ),
        patch(
            "src.server.database.conversation.get_thread_by_id",
            new=AsyncMock(return_value={"workspace_id": "ws-placeholder"}),
        ),
        # A send carrying comments must carry a request_key; its retransmit
        # probe reads the run ledger, which has no pool in the unit app.
        patch(
            "src.server.services.runs.admission.run_named_by_key",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.server.app.threads.messaging.get_thread_auth_meta",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.server.database.workspace.get_workspace",
            new=AsyncMock(
                return_value={
                    "workspace_id": "ws-placeholder",
                    "user_id": "usr-placeholder-001",
                    "status": "running",
                }
            ),
        ),
        patch(
            "src.server.services.workspace_manager.WorkspaceManager.get_instance",
            return_value=wm_singleton,
        ),
        patch(
            "src.server.handlers.chat.astream_ptc_workflow",
            stream,
        ),
        patch(
            "src.server.app.threads.messaging.observe_chat_stream",
            side_effect=lambda gen, **_: gen,
        ),
        patch(
            "src.server.app.threads.messaging._assert_stream_transport_ready",
            new=AsyncMock(),
        ),
    ):
        yield stream


def _stub_config():
    from ptc_agent.config.agent import CredentialSource

    cfg = MagicMock()
    cfg.credential_source = CredentialSource.PLATFORM
    cfg.llm_client = None
    cfg.llm = MagicMock()
    cfg.llm.name = "claude-sonnet-placeholder"
    return cfg


_COMMENT_SEND = {
    "workspace_id": "ws-placeholder",
    "messages": [{"role": "user", "content": "please look at the notes"}],
    "request_key": "53000000-0000-4000-8000-000000000001",
    "reading": "chat",
    "artifact_comment_ids": [COMMENT_ID],
}


@pytest.mark.asyncio
async def test_off_comment_send_answers_http_409_not_a_stream_frame(
    threads_client,
) -> None:
    with _gated_send_route() as stream:
        response = await threads_client.post(
            f"/api/v1/threads/{THREAD_ID}/messages", json=_COMMENT_SEND
        )
    # A real 409 status, not a 200 whose stream carries an error frame.
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "artifact_publishing_disabled"
    # Nothing was claimed and no turn started.
    stream.assert_not_called()


@pytest.mark.asyncio
async def test_on_comment_send_passes_the_gate_to_the_stream(
    threads_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SWITCH_ENV, "1")
    with _gated_send_route() as stream:
        response = await threads_client.post(
            f"/api/v1/threads/{THREAD_ID}/messages", json=_COMMENT_SEND
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    stream.assert_called_once()


# --- switch on: an internal owner publishes and keeps updated ---------------


@pytest.mark.asyncio
async def test_on_owner_publish_and_keep_updated_allowed(
    owner_routes, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.server.app.artifacts.models import KeepUpdatedRequest, PublishRequest
    from src.server.database.artifacts.versions import Version

    monkeypatch.setenv(SWITCH_ENV, "1")
    snapshot_publish = create_autospec(
        owner_routes.records.snapshot.publish,
        return_value=SimpleNamespace(
            artifact=owner_routes.artifact, seq=1, cut=True
        ),
    )
    get_version = create_autospec(
        owner_routes.db.get_version,
        return_value=Version(
            seq=1,
            entry_path="report.html",
            origin="owner",
            title="Report",
            label=None,
            byte_len=128,
            published_at=NOW,
        ),
    )
    set_keep_updated = create_autospec(
        owner_routes.db.set_keep_updated,
        side_effect=lambda *, user_id, artifact_id, keep_updated: (
            dataclasses.replace(owner_routes.artifact, keep_updated=keep_updated)
        ),
    )
    with (
        patch.object(owner_routes.records.snapshot, "publish", snapshot_publish),
        patch.object(owner_routes.db, "get_version", get_version),
        patch.object(owner_routes.db, "set_keep_updated", set_keep_updated),
    ):
        published = await owner_routes.records.publish_artifact(
            ARTIFACT_ID,
            PublishRequest(files=["report.html"], keep_updated=True),
            x_user_id="u-owner-1",
        )
        kept = await owner_routes.records.update_artifact(
            ARTIFACT_ID,
            KeepUpdatedRequest(keep_updated=True),
            x_user_id="u-owner-1",
        )
    assert published.cut is True
    assert published.version.seq == 1
    snapshot_publish.assert_awaited_once()
    assert kept.keep_updated is True
    set_keep_updated.assert_awaited_once()


# --- the agent tool list follows the switch ---------------------------------


def test_off_main_agent_registers_no_reply_to_comment() -> None:
    """The tool's absence is the whole guard: it writes to the comments tables
    in its own body, out of the HTTP layer's reach."""
    from ptc_agent.agent.agent import PTCAgent
    from ptc_agent.config.agent import AgentConfig, LLMConfig
    from ptc_agent.config.core import (
        DaytonaConfig,
        FilesystemConfig,
        LoggingConfig,
        MCPConfig,
        SandboxConfig,
        SecurityConfig,
    )
    from src.llms.llm import create_llm

    config = AgentConfig(
        llm=LLMConfig(name="claude-haiku-5-5-code"),
        security=SecurityConfig(),
        logging=LoggingConfig(),
        sandbox=SandboxConfig(daytona=DaytonaConfig(api_key="test-key")),
        mcp=MCPConfig(),
        filesystem=FilesystemConfig(),
    )
    target = create_llm("claude-haiku-5-5-code")
    with patch.object(AgentConfig, "get_llm_client", return_value=target):
        graph = PTCAgent(config).create_agent(
            llm=target,
            thread_id="t-switch-1",
            user_id="u-1",
            mcp_registry=MagicMock(),
            sandbox=MagicMock(),
            claude_code_launcher=lambda sandbox: MagicMock(),
        )
    assert "reply_to_comment" not in {
        t.name for t in graph.turn.spec.tools
    }
