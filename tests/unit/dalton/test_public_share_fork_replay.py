"""Unit tests for public share replay of snapshot forks (#875 + d059) and langalpha-doc Dalton pruning.

All test doubles use ``unittest.mock.create_autospec``.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec
import uuid

from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
import pytest

from src.dalton import content_assembly, share_media, share_redaction
from src.dalton.share_redaction import (
    FORK_SOURCE_UNAVAILABLE_NOTICE,
    WAKEUP_FIXED_NOTICE,
)
from src.ptc_agent.agent.prompts import get_loader
from src.server.app import public as public_app
from src.server.app import share_access
from src.server.database.conversation.replay_rows import BorrowedRows, ThreadRows
from src.server.services.forks import segments as fork_segments
from src.server.services.forks.model import BorrowedSegment, Borrowing, ForkLineage
from src.server.services.history import replay as replay_mod
from src.server.services.history.replay import PageBudget, ReplayPage
from src.server.services.history.replay import borrowed as borrowed_mod
from src.server.services.history.replay.lines import Line, line_of
from src.server.services.shared_workspaces import access as shared_ws_access


REPO_ROOT = Path(__file__).resolve().parents[3]
LANGALPHA_DOC_DIR = (
    REPO_ROOT / "plugins" / "langalpha_service" / "skills" / "langalpha-doc"
)


def _parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for block in body.split("\n\n"):
        kind = None
        payload = None
        for line in block.splitlines():
            if line.startswith("event: "):
                kind = line[len("event: ") :]
            elif line.startswith("data: "):
                payload = json.loads(line[len("data: ") :])
        if kind is not None and payload is not None:
            events.append((kind, payload))
    return events


def _make_turn_lines(
    thread_id: str,
    turn_index: int,
    *,
    user_text: str,
    assistant_text: str,
    user_meta: dict[str, Any] | None = None,
    query_type: str | None = None,
    with_tool: bool = False,
    tool_file: str = "reports/tool_file.pdf",
    tool_output: str = "tool output body",
    with_widget: bool = False,
) -> list[Line]:
    user_data: dict[str, Any] = {
        "thread_id": thread_id,
        "turn_index": turn_index,
        "content": user_text,
        "metadata": dict(user_meta or {}),
    }
    if query_type is not None:
        user_data["query_type"] = query_type
    items: list[dict[str, Any]] = [{"event": "user_message", "data": user_data}]
    if with_tool:
        items.append(
            {
                "event": "tool_calls",
                "data": {
                    "thread_id": thread_id,
                    "turn_index": turn_index,
                    "agent": "main",
                    "id": f"tc-msg-{turn_index}",
                    "tool_calls": [
                        {
                            "id": f"call-{turn_index}",
                            "name": "Read",
                            "args": {"file_path": tool_file},
                        }
                    ],
                },
            }
        )
        items.append(
            {
                "event": "tool_call_result",
                "data": {
                    "thread_id": thread_id,
                    "turn_index": turn_index,
                    "agent": "main",
                    "tool_call_id": f"call-{turn_index}",
                    "content": tool_output,
                },
            }
        )
    items.append(
        {
            "event": "message_chunk",
            "data": {
                "thread_id": thread_id,
                "turn_index": turn_index,
                "agent": "main",
                "id": f"asst-{turn_index}",
                "role": "assistant",
                "content": assistant_text,
                "content_type": "text",
                "finish_reason": "stop",
                "phase": "final_answer",
            },
        }
    )
    if with_widget:
        items.append(
            {
                "event": "artifact",
                "data": {
                    "thread_id": thread_id,
                    "turn_index": turn_index,
                    "agent": "main",
                    "artifact_type": "html_widget",
                    "artifact_id": f"w-{turn_index}",
                    "status": "completed",
                    "payload": {"html": "<div>secret chart</div>"},
                },
            }
        )
    return [line_of(it) for it in items]


def _make_lineage(
    *,
    source_thread_id: str,
    turn_base: int,
    by: str = "maker-1",
    snapshot: bool = True,
    first_turn: int = 0,
) -> dict[str, Any]:
    borrow = (
        Borrowing(
            turn_base=turn_base,
            checkpoint_id="cp-cut",
            ancestors=(
                BorrowedSegment(
                    thread_id=source_thread_id,
                    first_turn=first_turn,
                    through_turn=turn_base - 1,
                    response_id=str(uuid.uuid4()),
                    run_horizon=10,
                    runs_through=turn_base,
                ),
            ),
        )
        if snapshot
        else None
    )
    lineage = ForkLineage(
        kind="durable",
        thread_id=source_thread_id,
        cut="through_turn",
        turn_index=max(0, turn_base - 1),
        checkpoint_id="cp-cut",
        copy_scratchpad=False,
        tasks=0,
        title="Original Title",
        at=datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc),
        by=by,
        borrow=borrow,
    )
    return lineage.model_dump(mode="json")


def _make_thread_rows(
    *,
    thread_id: str,
    owner_id: str = "maker-1",
    workspace_id: str = "ws-1",
    turn_indexes: list[int],
    fork_dict: dict[str, Any] | None = None,
    queries_override: list[dict[str, Any]] | None = None,
    responses_override: dict[int, dict[str, Any]] | None = None,
) -> ThreadRows:
    queries = (
        queries_override
        if queries_override is not None
        else [{"turn_index": ti, "content": f"q-{ti}", "metadata": {}} for ti in turn_indexes]
    )
    responses = (
        responses_override
        if responses_override is not None
        else {
            ti: {
                "turn_index": ti,
                "conversation_response_id": f"resp-{thread_id[:4]}-{ti}",
                "status": "completed",
            }
            for ti in turn_indexes
        }
    )
    return ThreadRows(
        thread_id=thread_id,
        owner_id=owner_id,
        thread={
            "conversation_thread_id": thread_id,
            "workspace_id": workspace_id,
            "latest_checkpoint_id": f"cp-{thread_id[:4]}",
            "fork": fork_dict,
        },
        queries=queries,
        responses_by_turn=responses,
        usages=[],
        provenance=[],
        run_facts=[],
        open_runs={},
    )


def _page_from_turn_map(
    turns_map: dict[int, list[Line]],
    *,
    from_turn: int | None = None,
    before_turn: int | None = None,
    limit: int | None = None,
) -> tuple[ReplayPage, str]:
    selected = sorted(
        ti
        for ti in turns_map
        if (from_turn is None or ti >= from_turn)
        and (before_turn is None or ti < before_turn)
    )
    has_more = limit is not None and len(selected) > limit
    chosen = selected[-limit:] if has_more else selected
    lines = [ln for ti in chosen for ln in turns_map[ti]]
    return (
        ReplayPage(
            lines=lines,
            first_turn_index=chosen[0] if chosen else None,
            has_more=has_more,
            turns=len(chosen),
        ),
        "checkpoint",
    )


def _make_client() -> AsyncClient:
    app = FastAPI()
    app.include_router(public_app.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_snapshot_fork_public_replay_shows_inherited_and_own_turns_with_media_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared snapshot fork replays inherited prior turns + own turns in order and rewrites/serves borrowed media."""
    src_tid = str(uuid.uuid4())
    fork_tid = str(uuid.uuid4())
    share_token = "tok_fork_rich_01"
    src_img = f"https://cdn.example.com/private-media/response-images/{src_tid}/abc/src_chart.png"
    fork_img = f"https://cdn.example.com/private-media/response-images/{fork_tid}/def/fork_chart.png"

    fork_dict = _make_lineage(source_thread_id=src_tid, turn_base=2, by="maker-1", snapshot=True)
    fork_thread = {
        "conversation_thread_id": fork_tid,
        "workspace_id": "ws-fork",
        "user_id": "maker-1",
        "owner_id": "maker-1",
        "is_shared": True,
        "share_permissions": {"allow_files": True},
    }
    src_thread_meta = {
        "conversation_thread_id": src_tid,
        "workspace_id": "ws-src",
        "user_id": "maker-1",
        "owner_user_id": "maker-1",
        "is_shared": True,
        "share_permissions": {"allow_files": True},
        "workspace_status": "active",
    }
    src_rows = _make_thread_rows(thread_id=src_tid, workspace_id="ws-src", turn_indexes=[0, 1])
    fork_rows = _make_thread_rows(
        thread_id=fork_tid, workspace_id="ws-fork", turn_indexes=[2], fork_dict=fork_dict
    )

    lines_by_thread = {
        src_tid: {
            0: _make_turn_lines(src_tid, 0, user_text="Inherited Q0", assistant_text="Inherited A0"),
            1: _make_turn_lines(
                src_tid,
                1,
                user_text="Inherited Q1",
                assistant_text=f"Inherited A1 ![chart]({src_img})",
            ),
        },
        fork_tid: {
            2: _make_turn_lines(
                fork_tid,
                2,
                user_text="Fork Q2",
                assistant_text=f"Fork A2 ![chart]({fork_img})",
            ),
        },
    }

    async def _fake_read_replay_page(
        rows: ThreadRows,
        *,
        source: Any = "auto",
        before_turn: Any = None,
        limit: int | None = None,
        budget: PageBudget | None = None,
        persist: bool = True,
        from_turn: Any = None,
    ) -> tuple[ReplayPage, str]:
        return _page_from_turn_map(
            lines_by_thread[rows.thread_id],
            from_turn=from_turn,
            before_turn=before_turn,
            limit=limit,
        )

    async def _fake_resolve_media(
        thread: Any, queries: Any, items: Any, **kwargs: Any
    ) -> dict[str, tuple[str, str]]:
        tid = str(thread.get("conversation_thread_id") or "")
        if tid == src_tid:
            return {src_img: ("upstream", f"private-media/response-images/{src_tid}/abc/src_chart.png")}
        if tid == fork_tid:
            return {fork_img: ("upstream", f"private-media/response-images/{fork_tid}/def/fork_chart.png")}
        return {}

    finish_mock = create_autospec(replay_mod.finish_lines, return_value=None)
    monkeypatch.setattr(share_access, "get_shared_thread", create_autospec(share_access.get_shared_thread, return_value=fork_thread))
    monkeypatch.setattr(public_app, "get_shared_thread", create_autospec(public_app.get_shared_thread, return_value=fork_thread))
    monkeypatch.setattr(public_app, "get_replay_thread_data", create_autospec(public_app.get_replay_thread_data, return_value=fork_rows))
    monkeypatch.setattr(share_access, "shared_thread_allows_files", create_autospec(share_access.shared_thread_allows_files, return_value=True))
    monkeypatch.setattr(borrowed_mod, "thread_access", create_autospec(borrowed_mod.thread_access, return_value=None))
    monkeypatch.setattr(
        fork_segments,
        "get_borrowed_rows",
        create_autospec(
            fork_segments.get_borrowed_rows,
            return_value=BorrowedRows(present=True, kept=True, seen=[], rows=src_rows),
        ),
    )
    monkeypatch.setattr(replay_mod, "read_replay_page", create_autospec(replay_mod.read_replay_page, side_effect=_fake_read_replay_page))
    monkeypatch.setattr(replay_mod, "finish_lines", finish_mock)
    load_threads_mock = create_autospec(
        share_redaction._load_threads_for_redaction,
        return_value={
            src_tid: src_thread_meta,
            fork_tid: {**fork_thread, "fork": fork_dict, "workspace_status": "active"},
        },
    )
    monkeypatch.setattr(share_redaction, "_load_threads_for_redaction", load_threads_mock)
    monkeypatch.setattr(
        share_media,
        "resolve_thread_media_bindings",
        create_autospec(share_media.resolve_thread_media_bindings, side_effect=_fake_resolve_media),
    )
    monkeypatch.setattr(share_media, "_is_url_from_configured_storage", create_autospec(share_media._is_url_from_configured_storage, return_value=True))

    async with _make_client() as client:
        resp = await client.get(f"/api/v1/public/shared/{share_token}/replay")

    assert resp.status_code == 200
    assert src_tid not in resp.text  # Real ancestor thread_id never leaks on the wire
    events = _parse_sse(resp.text)
    assert events[0][0] == "fork_divider"
    assert events[0][1]["source_thread_id"] == "original-1"
    assert events[0][1]["state"] == "shown"
    assert events[0][1]["turn_base"] == 2

    user_turns = [(d["turn_index"], d["content"]) for k, d in events if k == "user_message"]
    assert user_turns == [(0, "Inherited Q0"), (1, "Inherited Q1"), (2, "Fork Q2")]

    asst_by_turn = {d["turn_index"]: d["content"] for k, d in events if k == "message_chunk"}
    assert src_img not in asst_by_turn[1]
    assert f"/api/v1/public/shared/{share_token}/media/" in asst_by_turn[1]
    assert fork_img not in asst_by_turn[2]
    assert f"/api/v1/public/shared/{share_token}/media/" in asst_by_turn[2]

    # All emitted turn events carry the fork's thread_id
    for kind, data in events:
        if kind in ("user_message", "message_chunk", "fork_divider"):
            assert data.get("thread_id") == fork_tid

    # Verify the borrowed media URL carries opaque segment + turn claims (no plaintext source_thread_id)
    borrowed_url = asst_by_turn[1].split("![chart](", 1)[1].rstrip(")")
    borrowed_ref = borrowed_url.rsplit("/", 1)[-1]
    claims = share_media.verify_public_media_ref(borrowed_ref, expected_thread_id=fork_tid)
    assert claims["seg"] == 0
    assert claims["trn"] == 1
    assert "stid" not in claims
    assert src_tid not in json.dumps(claims)

    monkeypatch.setattr(
        shared_ws_access,
        "thread_access",
        create_autospec(shared_ws_access.thread_access, return_value=None),
    )
    serve_mock = create_autospec(
        share_media._serve_media_payload,
        return_value=public_app.Response(content=b"PNGDATA", media_type="image/png"),
    )
    monkeypatch.setattr(share_media, "_serve_media_payload", serve_mock)

    async with _make_client() as client:
        media_ok = await client.get(borrowed_url)
    assert media_ok.status_code == 200
    assert media_ok.content == b"PNGDATA"
    assert serve_mock.await_args.kwargs["thread_id"] == src_tid
    assert serve_mock.await_args.kwargs["turn_indices"] == (1,)

    # Revoking source's allow_files immediately blocks serving the borrowed media ref with 403
    load_threads_mock.return_value = {
        src_tid: {**src_thread_meta, "share_permissions": {"allow_files": False}},
        fork_tid: {**fork_thread, "fork": fork_dict, "workspace_status": "active"},
    }
    async with _make_client() as client:
        media_denied = await client.get(borrowed_url)
    assert media_denied.status_code == 403

    # Deleting source immediately blocks serving the borrowed media ref with 404
    load_threads_mock.return_value = {
        fork_tid: {**fork_thread, "fork": fork_dict, "workspace_status": "active"},
    }
    async with _make_client() as client:
        media_gone = await client.get(borrowed_url)
    assert media_gone.status_code == 404


@pytest.mark.asyncio
async def test_snapshot_fork_source_allow_files_false_strips_files_and_d059_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When source thread is shared with allow_files=False, inherited turns lose files/comments/links/tools while fork's own turns stay rich."""
    src_tid = str(uuid.uuid4())
    fork_tid = str(uuid.uuid4())
    share_token = "tok_fork_mixed_01"
    secret_storage_url = f"https://cdn.example.com/private-media/attachments/{src_tid}/doc.pdf"

    fork_dict = _make_lineage(source_thread_id=src_tid, turn_base=2, by="maker-1", snapshot=True)
    fork_thread = {
        "conversation_thread_id": fork_tid,
        "workspace_id": "ws-fork",
        "user_id": "maker-1",
        "owner_id": "maker-1",
        "is_shared": True,
        "share_permissions": {"allow_files": True},
    }
    src_thread_meta = {
        "conversation_thread_id": src_tid,
        "workspace_id": "ws-src",
        "user_id": "maker-1",
        "owner_user_id": "maker-1",
        "is_shared": True,
        "share_permissions": {"allow_files": False},
        "computer_root_dir": "/Users/alice/src_ws",
        "workspace_status": "active",
    }
    src_rows = _make_thread_rows(
        thread_id=src_tid,
        workspace_id="ws-src",
        turn_indexes=[0, 1],
        queries_override=[
            {
                "turn_index": 0,
                "content": "Check this",
                "metadata": {"attachments": [{"name": "doc.pdf", "url": secret_storage_url}]},
            },
            {"turn_index": 1, "content": "wake prompt secret", "metadata": {}},
        ],
    )
    fork_rows = _make_thread_rows(
        thread_id=fork_tid, workspace_id="ws-fork", turn_indexes=[2], fork_dict=fork_dict
    )

    lines_by_thread = {
        src_tid: {
            0: _make_turn_lines(
                src_tid,
                0,
                user_text="Question <file-comments>private comment</file-comments> see /Users/alice/src_ws/model.xlsx",
                assistant_text=f"Answer with ![img]({secret_storage_url}) and reports/secret.pdf",
                user_meta={
                    "attachments": [{"name": "doc.pdf", "url": secret_storage_url}],
                    "artifact_comments": {"comments": [{"body": "hidden"}]},
                },
                with_tool=True,
                tool_file="reports/secret.pdf",
                tool_output="secret file contents",
                with_widget=True,
            ),
            1: _make_turn_lines(
                src_tid,
                1,
                user_text="Internal wakeup prompt text",
                assistant_text="Wakeup reply",
                query_type="wakeup",
                user_meta={
                    "wakeup": {
                        "v": 1,
                        "notices": [
                            {"kind": "cron", "notice": "Daily", "order": {"id": "ord-secret"}}
                        ],
                    }
                },
            ),
        },
        fork_tid: {
            2: _make_turn_lines(
                fork_tid,
                2,
                user_text="Fork own question",
                assistant_text="Fork own answer",
                with_tool=True,
                tool_file="notes/fork_own.md",
                tool_output="fork own tool output",
            ),
        },
    }

    async def _fake_read_replay_page(
        rows: ThreadRows,
        *,
        source: Any = "auto",
        before_turn: Any = None,
        limit: int | None = None,
        budget: PageBudget | None = None,
        persist: bool = True,
        from_turn: Any = None,
    ) -> tuple[ReplayPage, str]:
        return _page_from_turn_map(
            lines_by_thread[rows.thread_id],
            from_turn=from_turn,
            before_turn=before_turn,
            limit=limit,
        )

    finish_mock = create_autospec(replay_mod.finish_lines, return_value=None)
    monkeypatch.setattr(public_app, "get_shared_thread", create_autospec(public_app.get_shared_thread, return_value=fork_thread))
    monkeypatch.setattr(public_app, "get_replay_thread_data", create_autospec(public_app.get_replay_thread_data, return_value=fork_rows))
    monkeypatch.setattr(share_access, "shared_thread_allows_files", create_autospec(share_access.shared_thread_allows_files, return_value=True))
    monkeypatch.setattr(borrowed_mod, "thread_access", create_autospec(borrowed_mod.thread_access, return_value=None))
    monkeypatch.setattr(
        fork_segments,
        "get_borrowed_rows",
        create_autospec(
            fork_segments.get_borrowed_rows,
            return_value=BorrowedRows(present=True, kept=True, seen=[], rows=src_rows),
        ),
    )
    monkeypatch.setattr(replay_mod, "read_replay_page", create_autospec(replay_mod.read_replay_page, side_effect=_fake_read_replay_page))
    monkeypatch.setattr(replay_mod, "finish_lines", finish_mock)
    monkeypatch.setattr(
        share_redaction,
        "_load_threads_for_redaction",
        create_autospec(share_redaction._load_threads_for_redaction, return_value={src_tid: src_thread_meta}),
    )
    monkeypatch.setattr(
        share_media,
        "resolve_thread_media_bindings",
        create_autospec(share_media.resolve_thread_media_bindings, return_value={}),
    )

    async with _make_client() as client:
        resp = await client.get(f"/api/v1/public/shared/{share_token}/replay")

    assert resp.status_code == 200
    events = _parse_sse(resp.text)

    # Strict no-files invariant: finish_lines was NEVER called for the allow_files=False source
    finish_called_tids = [call.args[0] for call in finish_mock.await_args_list]
    assert src_tid not in finish_called_tids
    assert fork_tid in finish_called_tids

    # Sensitive source files, comments, paths, storage links, and wakeup order secrets are completely gone
    assert secret_storage_url not in resp.text
    assert "private comment" not in resp.text
    assert "/Users/alice/src_ws" not in resp.text
    assert "reports/secret.pdf" not in resp.text
    assert "ord-secret" not in resp.text
    assert "Internal wakeup prompt text" not in resp.text
    assert "secret file contents" not in resp.text
    assert "<div>secret chart</div>" not in resp.text

    # Inherited turn 0 has redacted widget placeholder and zero tool_calls; turn 1 has WAKEUP_FIXED_NOTICE
    turn0_tools = [k for k, d in events if k in ("tool_calls", "tool_call_result") and d.get("turn_index") == 0]
    assert turn0_tools == []
    turn0_widgets = [d for k, d in events if k == "artifact" and d.get("turn_index") == 0]
    assert len(turn0_widgets) == 1
    assert turn0_widgets[0]["payload"] == {"redacted": True}

    turn1_user = [d for k, d in events if k == "user_message" and d.get("turn_index") == 1][0]
    assert turn1_user["content"] == WAKEUP_FIXED_NOTICE
    assert "order" not in json.dumps(turn1_user["metadata"])

    # Fork's own turn 2 still keeps rich tool_calls
    turn2_tools = [k for k, d in events if k == "tool_calls" and d.get("turn_index") == 2]
    assert len(turn2_tools) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected_state"),
    [
        ("deleted_in_segments", "deleted"),
        ("access_denied", "unavailable"),
        ("deleted_mid_request", "deleted"),
    ],
)
async def test_snapshot_fork_deleted_or_unavailable_source_emits_neutral_placeholder(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    expected_state: str,
) -> None:
    """Deleted or inaccessible source hides all inherited turns and emits a neutral placeholder divider."""
    src_tid = str(uuid.uuid4())
    fork_tid = str(uuid.uuid4())
    share_token = f"tok_fork_{scenario}"

    fork_dict = _make_lineage(source_thread_id=src_tid, turn_base=1, by="maker-1", snapshot=True)
    fork_thread = {
        "conversation_thread_id": fork_tid,
        "workspace_id": "ws-fork",
        "user_id": "maker-1",
        "owner_id": "maker-1",
        "is_shared": True,
        "share_permissions": {"allow_files": False},
    }
    src_rows = _make_thread_rows(thread_id=src_tid, workspace_id="ws-src", turn_indexes=[0])
    fork_rows = _make_thread_rows(
        thread_id=fork_tid, workspace_id="ws-fork", turn_indexes=[1], fork_dict=fork_dict
    )

    lines_by_thread = {
        src_tid: {
            0: _make_turn_lines(src_tid, 0, user_text="MUST_NOT_APPEAR", assistant_text="SECRET_ANS"),
        },
        fork_tid: {
            1: _make_turn_lines(fork_tid, 1, user_text="Fork own Q1", assistant_text="Fork own A1"),
        },
    }

    async def _fake_read_replay_page(
        rows: ThreadRows,
        *,
        source: Any = "auto",
        before_turn: Any = None,
        limit: int | None = None,
        budget: PageBudget | None = None,
        persist: bool = True,
        from_turn: Any = None,
    ) -> tuple[ReplayPage, str]:
        return _page_from_turn_map(
            lines_by_thread[rows.thread_id],
            from_turn=from_turn,
            before_turn=before_turn,
            limit=limit,
        )

    monkeypatch.setattr(public_app, "get_shared_thread", create_autospec(public_app.get_shared_thread, return_value=fork_thread))
    monkeypatch.setattr(public_app, "get_replay_thread_data", create_autospec(public_app.get_replay_thread_data, return_value=fork_rows))
    monkeypatch.setattr(replay_mod, "read_replay_page", create_autospec(replay_mod.read_replay_page, side_effect=_fake_read_replay_page))

    if scenario == "deleted_in_segments":
        monkeypatch.setattr(borrowed_mod, "thread_access", create_autospec(borrowed_mod.thread_access, return_value=None))
        monkeypatch.setattr(
            fork_segments,
            "get_borrowed_rows",
            create_autospec(
                fork_segments.get_borrowed_rows,
                return_value=BorrowedRows(present=False, kept=False, seen=[], rows=None),
            ),
        )
        monkeypatch.setattr(share_redaction, "_load_threads_for_redaction", create_autospec(share_redaction._load_threads_for_redaction, return_value={}))
    elif scenario == "access_denied":
        monkeypatch.setattr(
            borrowed_mod,
            "thread_access",
            create_autospec(borrowed_mod.thread_access, side_effect=HTTPException(status_code=403, detail="Forbidden")),
        )
        monkeypatch.setattr(
            fork_segments,
            "get_borrowed_rows",
            create_autospec(
                fork_segments.get_borrowed_rows,
                return_value=BorrowedRows(present=True, kept=True, seen=[], rows=None),
            ),
        )
        monkeypatch.setattr(share_redaction, "_load_threads_for_redaction", create_autospec(share_redaction._load_threads_for_redaction, return_value={}))
    else:
        monkeypatch.setattr(borrowed_mod, "thread_access", create_autospec(borrowed_mod.thread_access, return_value=None))
        monkeypatch.setattr(
            fork_segments,
            "get_borrowed_rows",
            create_autospec(
                fork_segments.get_borrowed_rows,
                return_value=BorrowedRows(present=True, kept=True, seen=[], rows=src_rows),
            ),
        )
        # Source disappeared before metadata read
        monkeypatch.setattr(share_redaction, "_load_threads_for_redaction", create_autospec(share_redaction._load_threads_for_redaction, return_value={}))

    async with _make_client() as client:
        resp = await client.get(f"/api/v1/public/shared/{share_token}/replay")

    assert resp.status_code == 200
    assert "MUST_NOT_APPEAR" not in resp.text
    assert "SECRET_ANS" not in resp.text
    assert src_tid not in resp.text

    events = _parse_sse(resp.text)
    dividers = [d for k, d in events if k == "fork_divider"]
    assert len(dividers) == 1
    assert dividers[0]["state"] == expected_state
    assert dividers[0]["shown_through"] is None
    assert dividers[0]["segments"] == []
    assert dividers[0]["source_thread_id"] == "original-1"
    assert dividers[0]["notice"] == FORK_SOURCE_UNAVAILABLE_NOTICE

    user_turns = [(d["turn_index"], d["content"]) for k, d in events if k == "user_message"]
    assert user_turns == [(1, "Fork own Q1")]


@pytest.mark.asyncio
async def test_snapshot_fork_pagination_across_inherited_and_own_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pagination cleanly crosses the borrowed/own boundary and redacts every borrowed page by source rules."""
    src_tid = str(uuid.uuid4())
    fork_tid = str(uuid.uuid4())
    share_token = "tok_fork_page_01"

    fork_dict = _make_lineage(source_thread_id=src_tid, turn_base=3, by="maker-1", snapshot=True)
    fork_thread = {
        "conversation_thread_id": fork_tid,
        "workspace_id": "ws-fork",
        "user_id": "maker-1",
        "owner_id": "maker-1",
        "is_shared": True,
        "share_permissions": {"allow_files": True},
    }
    src_thread_meta = {
        "conversation_thread_id": src_tid,
        "workspace_id": "ws-src",
        "user_id": "maker-1",
        "owner_user_id": "maker-1",
        "is_shared": True,
        "share_permissions": {"allow_files": False},
        "workspace_status": "active",
    }
    src_rows = _make_thread_rows(thread_id=src_tid, workspace_id="ws-src", turn_indexes=[0, 1, 2])
    fork_rows = _make_thread_rows(
        thread_id=fork_tid, workspace_id="ws-fork", turn_indexes=[3, 4], fork_dict=fork_dict
    )

    lines_by_thread = {
        src_tid: {
            0: _make_turn_lines(src_tid, 0, user_text="Src Q0", assistant_text="Src A0", with_tool=True),
            1: _make_turn_lines(src_tid, 1, user_text="Src Q1", assistant_text="Src A1", with_tool=True),
            2: _make_turn_lines(src_tid, 2, user_text="Src Q2", assistant_text="Src A2", with_tool=True),
        },
        fork_tid: {
            3: _make_turn_lines(fork_tid, 3, user_text="Fork Q3", assistant_text="Fork A3", with_tool=True),
            4: _make_turn_lines(fork_tid, 4, user_text="Fork Q4", assistant_text="Fork A4", with_tool=True),
        },
    }

    async def _fake_read_replay_page(
        rows: ThreadRows,
        *,
        source: Any = "auto",
        before_turn: Any = None,
        limit: int | None = None,
        budget: PageBudget | None = None,
        persist: bool = True,
        from_turn: Any = None,
    ) -> tuple[ReplayPage, str]:
        return _page_from_turn_map(
            lines_by_thread[rows.thread_id],
            from_turn=from_turn,
            before_turn=before_turn,
            limit=limit,
        )

    monkeypatch.setattr(share_redaction, "PUBLIC_SHARE_MAX_TURNS", 3)
    monkeypatch.setattr(public_app, "get_shared_thread", create_autospec(public_app.get_shared_thread, return_value=fork_thread))
    monkeypatch.setattr(public_app, "get_replay_thread_data", create_autospec(public_app.get_replay_thread_data, return_value=fork_rows))
    monkeypatch.setattr(share_access, "shared_thread_allows_files", create_autospec(share_access.shared_thread_allows_files, return_value=True))
    monkeypatch.setattr(borrowed_mod, "thread_access", create_autospec(borrowed_mod.thread_access, return_value=None))
    monkeypatch.setattr(
        fork_segments,
        "get_borrowed_rows",
        create_autospec(
            fork_segments.get_borrowed_rows,
            return_value=BorrowedRows(present=True, kept=True, seen=[], rows=src_rows),
        ),
    )
    monkeypatch.setattr(replay_mod, "read_replay_page", create_autospec(replay_mod.read_replay_page, side_effect=_fake_read_replay_page))
    monkeypatch.setattr(replay_mod, "finish_lines", create_autospec(replay_mod.finish_lines, return_value=None))
    monkeypatch.setattr(
        share_redaction,
        "_load_threads_for_redaction",
        create_autospec(share_redaction._load_threads_for_redaction, return_value={src_tid: src_thread_meta}),
    )
    monkeypatch.setattr(
        share_media,
        "resolve_thread_media_bindings",
        create_autospec(share_media.resolve_thread_media_bindings, return_value={}),
    )

    async with _make_client() as client:
        page1 = await client.get(f"/api/v1/public/shared/{share_token}/replay")
        assert page1.status_code == 200
        ev1 = _parse_sse(page1.text)
        assert ev1[0] == ("history_page", {"first_turn_index": 2, "has_more": True})
        assert ev1[1][0] == "fork_divider"
        assert ev1[1][1]["turn_base"] == 3
        assert [(d["turn_index"], d["content"]) for k, d in ev1 if k == "user_message"] == [
            (2, "Src Q2"),
            (3, "Fork Q3"),
            (4, "Fork Q4"),
        ]
        # Borrowed turn 2 was strictly redacted (no tool_calls), own turns 3 & 4 kept tool_calls
        assert [d["turn_index"] for k, d in ev1 if k == "tool_calls"] == [3, 4]

        page2 = await client.get(
            f"/api/v1/public/shared/{share_token}/replay", params={"before_turn": 2}
        )
        assert page2.status_code == 200
        ev2 = _parse_sse(page2.text)
        assert ev2[0] == ("history_page", {"first_turn_index": 0, "has_more": False})
        assert not any(k == "fork_divider" for k, _ in ev2)
        assert [(d["turn_index"], d["content"]) for k, d in ev2 if k == "user_message"] == [
            (0, "Src Q0"),
            (1, "Src Q1"),
        ]
        assert [k for k, _ in ev2 if k == "tool_calls"] == []


@pytest.mark.asyncio
async def test_non_snapshot_legacy_copied_fork_unaffected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy copied fork (lineage.borrow is None) stays on the single-thread replay path with no fork_divider."""
    src_tid = str(uuid.uuid4())
    fork_tid = str(uuid.uuid4())
    share_token = "tok_legacy_fork_01"

    legacy_fork_dict = _make_lineage(
        source_thread_id=src_tid, turn_base=1, by="maker-1", snapshot=False
    )
    fork_thread = {
        "conversation_thread_id": fork_tid,
        "workspace_id": "ws-fork",
        "user_id": "maker-1",
        "owner_id": "maker-1",
        "is_shared": True,
        "share_permissions": {"allow_files": True},
    }
    fork_rows = _make_thread_rows(
        thread_id=fork_tid,
        workspace_id="ws-fork",
        turn_indexes=[0, 1],
        fork_dict=legacy_fork_dict,
    )
    page = ReplayPage(
        lines=[
            *_make_turn_lines(fork_tid, 0, user_text="Copied Q0", assistant_text="Copied A0"),
            *_make_turn_lines(fork_tid, 1, user_text="Own Q1", assistant_text="Own A1"),
        ],
        first_turn_index=0,
        has_more=False,
        turns=2,
    )

    read_thread_page_mock = create_autospec(
        borrowed_mod.read_thread_page,
        side_effect=AssertionError("read_thread_page must not be called for legacy non-snapshot forks"),
    )
    read_replay_mock = create_autospec(
        replay_mod.read_replay_page, return_value=(page, "checkpoint")
    )
    monkeypatch.setattr(borrowed_mod, "read_thread_page", read_thread_page_mock)
    monkeypatch.setattr(public_app, "get_shared_thread", create_autospec(public_app.get_shared_thread, return_value=fork_thread))
    monkeypatch.setattr(public_app, "get_replay_thread_data", create_autospec(public_app.get_replay_thread_data, return_value=fork_rows))
    monkeypatch.setattr(share_access, "shared_thread_allows_files", create_autospec(share_access.shared_thread_allows_files, return_value=True))
    monkeypatch.setattr(replay_mod, "read_replay_page", read_replay_mock)
    monkeypatch.setattr(replay_mod, "finish_lines", create_autospec(replay_mod.finish_lines, return_value=None))
    monkeypatch.setattr(
        share_media,
        "resolve_thread_media_bindings",
        create_autospec(share_media.resolve_thread_media_bindings, return_value={}),
    )

    async with _make_client() as client:
        resp = await client.get(f"/api/v1/public/shared/{share_token}/replay")

    assert resp.status_code == 200
    read_replay_mock.assert_awaited_once()
    read_thread_page_mock.assert_not_called()
    events = _parse_sse(resp.text)
    assert not any(k == "fork_divider" for k, _ in events)
    assert [(d["turn_index"], d["content"]) for k, d in events if k == "user_message"] == [
        (0, "Copied Q0"),
        (1, "Own Q1"),
    ]


def test_langalpha_doc_prompt_and_skill_pruning_for_dalton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """langalpha_doc.md.j2 and langalpha-doc skill assembly prune features disabled in Dalton."""
    monkeypatch.setenv("APP_ENV", "dalton")
    monkeypatch.delenv("DALTON_PROFILE", raising=False)
    monkeypatch.delenv("DALTON_COMPUTER_CLI_TOKENS_ENABLED", raising=False)
    monkeypatch.delenv("DALTON_ENABLED", raising=False)

    from src.config.core import clear_config_cache

    clear_config_cache()
    try:
        # 1. Prompt component omits `la skills save` and `brokerage connection` by default in Dalton
        prompt = get_loader().get_system_prompt(
            current_time="2026-11-27 12:00 ET",
            langalpha_doc_enabled=True,
        )
        section = prompt[prompt.index("<langalpha_doc>") : prompt.index("</langalpha_doc>")]
        assert "la skills save" not in section
        assert "brokerage connection" not in section
        assert "MCP server or plugin" in section

        # 2. Skill pages pruned via Dalton content assembly
        skill_md = content_assembly.read_assembled_text(LANGALPHA_DOC_DIR / "SKILL.md")
        assert "brokerage" not in skill_md.lower()
        assert "references/drive.md" not in skill_md

        skills_md = content_assembly.read_assembled_text(
            LANGALPHA_DOC_DIR / "references" / "skills.md"
        )
        assert "la skills save" not in skills_md

        plugins_md = content_assembly.read_assembled_text(
            LANGALPHA_DOC_DIR / "references" / "plugins.md"
        )
        assert "## Brokerages" not in plugins_md
        assert "Brokerages" not in plugins_md
        assert "Robinhood" not in plugins_md
        assert "order approvals" not in plugins_md

        drive_md = content_assembly.read_assembled_text(
            LANGALPHA_DOC_DIR / "references" / "drive.md"
        )
        assert "config.dalton.yaml" in drive_md
        assert "la drive" not in drive_md
        assert "## What it is" not in drive_md

        files_md = content_assembly.read_assembled_text(
            LANGALPHA_DOC_DIR / "references" / "files.md"
        )
        assert "scratchpad" not in files_md

        history_md = content_assembly.read_assembled_text(
            LANGALPHA_DOC_DIR / "references" / "history.md"
        )
        assert ".agents/scratchpad/" not in history_md

        computer_md = content_assembly.read_assembled_text(
            LANGALPHA_DOC_DIR / "references" / "computer.md"
        )
        assert computer_md == (LANGALPHA_DOC_DIR / "references" / "computer.md").read_text(
            encoding="utf-8"
        )
    finally:
        clear_config_cache()


@pytest.mark.asyncio
async def test_snapshot_fork_private_source_readable_vs_revoked_or_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision lock: readable private source shows text without files; revoked/deleted source shows placeholder only."""
    from src.dalton import sharing as dalton_sharing
    from src.server.database import conversation as conv_db

    src_tid = str(uuid.uuid4())
    fork_tid = str(uuid.uuid4())
    share_token = "tok_private_src_01"
    secret_url = f"https://cdn.example.com/private-media/attachments/{src_tid}/private.pdf"

    fork_dict = _make_lineage(source_thread_id=src_tid, turn_base=1, by="maker-1", snapshot=True)
    fork_thread = {
        "conversation_thread_id": fork_tid,
        "workspace_id": "ws-fork",
        "user_id": "maker-1",
        "owner_id": "maker-1",
        "is_shared": True,
        "share_permissions": {"allow_files": True},
    }
    private_src_meta = {
        "conversation_thread_id": src_tid,
        "workspace_id": "ws-src",
        "user_id": "maker-1",
        "owner_user_id": "maker-1",
        "is_shared": False,
        "share_permissions": {},
        "workspace_status": "active",
    }
    src_rows = _make_thread_rows(
        thread_id=src_tid,
        workspace_id="ws-src",
        turn_indexes=[0],
        queries_override=[
            {
                "turn_index": 0,
                "content": "Private Q0",
                "metadata": {"attachments": [{"name": "private.pdf", "url": secret_url}]},
            }
        ],
    )
    fork_rows = _make_thread_rows(
        thread_id=fork_tid, workspace_id="ws-fork", turn_indexes=[1], fork_dict=fork_dict
    )
    lines_by_thread = {
        src_tid: {
            0: _make_turn_lines(
                src_tid,
                0,
                user_text="Private Q0",
                assistant_text=f"Private A0 ![doc]({secret_url})",
                user_meta={"attachments": [{"name": "private.pdf", "url": secret_url}]},
                with_tool=True,
            ),
        },
        fork_tid: {
            1: _make_turn_lines(fork_tid, 1, user_text="Fork Q1", assistant_text="Fork A1"),
        },
    }

    async def _fake_read_replay_page(
        rows: ThreadRows,
        *,
        source: Any = "auto",
        before_turn: Any = None,
        limit: int | None = None,
        budget: PageBudget | None = None,
        persist: bool = True,
        from_turn: Any = None,
    ) -> tuple[ReplayPage, str]:
        return _page_from_turn_map(
            lines_by_thread[rows.thread_id],
            from_turn=from_turn,
            before_turn=before_turn,
            limit=limit,
        )

    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setattr(public_app, "get_shared_thread", create_autospec(public_app.get_shared_thread, return_value=fork_thread))
    monkeypatch.setattr(public_app, "get_replay_thread_data", create_autospec(public_app.get_replay_thread_data, return_value=fork_rows))
    monkeypatch.setattr(share_access, "shared_thread_allows_files", create_autospec(share_access.shared_thread_allows_files, return_value=True))
    monkeypatch.setattr(replay_mod, "read_replay_page", create_autospec(replay_mod.read_replay_page, side_effect=_fake_read_replay_page))
    monkeypatch.setattr(replay_mod, "finish_lines", create_autospec(replay_mod.finish_lines, return_value=None))
    monkeypatch.setattr(
        share_media,
        "resolve_thread_media_bindings",
        create_autospec(share_media.resolve_thread_media_bindings, return_value={}),
    )
    monkeypatch.setattr(
        share_redaction,
        "_load_threads_for_redaction",
        create_autospec(share_redaction._load_threads_for_redaction, return_value={src_tid: private_src_meta}),
    )
    monkeypatch.setattr(
        conv_db,
        "get_thread_auth_meta",
        create_autospec(
            conv_db.get_thread_auth_meta,
            return_value={
                "conversation_thread_id": src_tid,
                "user_id": "maker-1",
                "thread_user_id": "maker-1",
                "workspace_owner_id": "ws-owner-2",
                "workspace_id": "ws-src",
            },
        ),
    )
    req_access_mock = create_autospec(dalton_sharing.require_thread_access, return_value="maker-1")
    monkeypatch.setattr(dalton_sharing, "require_thread_access", req_access_mock)
    borrowed_rows_mock = create_autospec(
        fork_segments.get_borrowed_rows,
        return_value=BorrowedRows(present=True, kept=True, seen=[], rows=src_rows),
    )
    monkeypatch.setattr(fork_segments, "get_borrowed_rows", borrowed_rows_mock)

    # 1. Source is private (is_shared=False) but still readable by fork owner -> text shown, files stripped
    async with _make_client() as client:
        resp_readable = await client.get(f"/api/v1/public/shared/{share_token}/replay")
    assert resp_readable.status_code == 200
    ev_readable = _parse_sse(resp_readable.text)
    assert [(d["turn_index"], d["content"]) for k, d in ev_readable if k == "user_message"] == [
        (0, "Private Q0"),
        (1, "Fork Q1"),
    ]
    assert secret_url not in resp_readable.text
    assert [k for k, d in ev_readable if k == "tool_calls" and d.get("turn_index") == 0] == []

    # 2. Source access revoked for fork owner under Dalton sharing -> placeholder only, zero inherited turns
    req_access_mock.side_effect = HTTPException(status_code=403, detail="Forbidden")
    async with _make_client() as client:
        resp_revoked = await client.get(f"/api/v1/public/shared/{share_token}/replay")
    assert resp_revoked.status_code == 200
    assert "Private Q0" not in resp_revoked.text
    assert "Private A0" not in resp_revoked.text
    ev_revoked = _parse_sse(resp_revoked.text)
    dividers = [d for k, d in ev_revoked if k == "fork_divider"]
    assert len(dividers) == 1
    assert dividers[0]["state"] == "unavailable"
    assert dividers[0]["shown_through"] is None
    assert dividers[0]["notice"] == FORK_SOURCE_UNAVAILABLE_NOTICE
    assert [(d["turn_index"], d["content"]) for k, d in ev_revoked if k == "user_message"] == [
        (1, "Fork Q1"),
    ]


@pytest.mark.asyncio
async def test_snapshot_fork_borrowed_media_bound_to_turn_and_opaque_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 1 & 2: borrowed media ref carries opaque seg+trn (never source_thread_id) and returns 404 after source turn regeneration."""
    src_tid = str(uuid.uuid4())
    fork_tid = str(uuid.uuid4())
    share_token = "tok_turn_bound_01"
    img_t0 = f"https://cdn.example.com/private-media/response-images/{src_tid}/t0/chart0.png"
    att_t1 = f"https://cdn.example.com/private-media/attachments/{src_tid}/t1/report1.pdf"

    fork_dict = _make_lineage(source_thread_id=src_tid, turn_base=2, by="maker-1", snapshot=True)
    fork_thread = {
        "conversation_thread_id": fork_tid,
        "workspace_id": "ws-fork",
        "user_id": "maker-1",
        "owner_id": "maker-1",
        "is_shared": True,
        "share_permissions": {"allow_files": True},
        "fork": fork_dict,
    }
    src_thread_meta = {
        "conversation_thread_id": src_tid,
        "workspace_id": "ws-src",
        "user_id": "maker-1",
        "owner_user_id": "maker-1",
        "is_shared": True,
        "share_permissions": {"allow_files": True},
        "workspace_status": "active",
    }
    src_rows = _make_thread_rows(
        thread_id=src_tid,
        workspace_id="ws-src",
        turn_indexes=[0, 1],
        queries_override=[
            {"turn_index": 0, "content": "Q0", "metadata": {}},
            {
                "turn_index": 1,
                "content": "Q1 with attachment",
                "metadata": {"attachments": [{"name": "report1.pdf", "url": att_t1}]},
            },
        ],
    )
    fork_rows = _make_thread_rows(
        thread_id=fork_tid, workspace_id="ws-fork", turn_indexes=[2], fork_dict=fork_dict
    )
    lines_by_thread = {
        src_tid: {
            0: _make_turn_lines(src_tid, 0, user_text="Q0", assistant_text=f"A0 ![c0]({img_t0})"),
            1: _make_turn_lines(
                src_tid,
                1,
                user_text="Q1",
                assistant_text="A1",
                user_meta={"attachments": [{"name": "report1.pdf", "url": att_t1}]},
            ),
        },
        fork_tid: {
            2: _make_turn_lines(fork_tid, 2, user_text="Q2", assistant_text="A2"),
        },
    }

    async def _fake_read_replay_page(
        rows: ThreadRows,
        *,
        source: Any = "auto",
        before_turn: Any = None,
        limit: int | None = None,
        budget: PageBudget | None = None,
        persist: bool = True,
        from_turn: Any = None,
    ) -> tuple[ReplayPage, str]:
        return _page_from_turn_map(
            lines_by_thread[rows.thread_id],
            from_turn=from_turn,
            before_turn=before_turn,
            limit=limit,
        )

    async def _fake_resolve_media(
        thread: Any, queries: Any, items: Any, **kwargs: Any
    ) -> dict[str, tuple[str, str]]:
        if str(thread.get("conversation_thread_id") or "") == src_tid:
            return {
                img_t0: ("upstream", f"private-media/response-images/{src_tid}/t0/chart0.png"),
                att_t1: ("upstream", f"private-media/attachments/{src_tid}/t1/report1.pdf"),
            }
        return {}

    borrowed_rows_mock = create_autospec(
        fork_segments.get_borrowed_rows,
        return_value=BorrowedRows(present=True, kept=True, seen=[], rows=src_rows),
    )
    monkeypatch.setattr(share_access, "get_shared_thread", create_autospec(share_access.get_shared_thread, return_value=fork_thread))
    monkeypatch.setattr(public_app, "get_shared_thread", create_autospec(public_app.get_shared_thread, return_value=fork_thread))
    monkeypatch.setattr(public_app, "get_replay_thread_data", create_autospec(public_app.get_replay_thread_data, return_value=fork_rows))
    monkeypatch.setattr(share_access, "shared_thread_allows_files", create_autospec(share_access.shared_thread_allows_files, return_value=True))
    monkeypatch.setattr(borrowed_mod, "thread_access", create_autospec(borrowed_mod.thread_access, return_value=None))
    monkeypatch.setattr(shared_ws_access, "thread_access", create_autospec(shared_ws_access.thread_access, return_value=None))
    monkeypatch.setattr(fork_segments, "get_borrowed_rows", borrowed_rows_mock)
    monkeypatch.setattr(replay_mod, "read_replay_page", create_autospec(replay_mod.read_replay_page, side_effect=_fake_read_replay_page))
    monkeypatch.setattr(replay_mod, "finish_lines", create_autospec(replay_mod.finish_lines, return_value=None))
    monkeypatch.setattr(
        share_redaction,
        "_load_threads_for_redaction",
        create_autospec(
            share_redaction._load_threads_for_redaction,
            return_value={
                src_tid: src_thread_meta,
                fork_tid: {**fork_thread, "workspace_status": "active"},
            },
        ),
    )
    monkeypatch.setattr(
        share_media,
        "resolve_thread_media_bindings",
        create_autospec(share_media.resolve_thread_media_bindings, side_effect=_fake_resolve_media),
    )
    monkeypatch.setattr(
        share_media,
        "_is_url_from_configured_storage",
        create_autospec(
            share_media._is_url_from_configured_storage,
            side_effect=lambda u: str(u).startswith("https://cdn.example.com/"),
        ),
    )
    serve_mock = create_autospec(
        share_media._serve_media_payload,
        return_value=public_app.Response(content=b"FILEBYTES", media_type="application/octet-stream"),
    )
    monkeypatch.setattr(share_media, "_serve_media_payload", serve_mock)

    async with _make_client() as client:
        replay_resp = await client.get(f"/api/v1/public/shared/{share_token}/replay")
    assert replay_resp.status_code == 200
    events = _parse_sse(replay_resp.text)

    asst0 = [d["content"] for k, d in events if k == "message_chunk" and d.get("turn_index") == 0][0]
    url_t0 = asst0.split("![c0](", 1)[1].rstrip(")")
    user1 = [d for k, d in events if k == "user_message" and d.get("turn_index") == 1][0]
    url_t1 = user1["metadata"]["attachments"][0]["url"]

    # Both refs encode opaque seg + trn and never contain plaintext source_thread_id
    for media_url, expected_turn in ((url_t0, 0), (url_t1, 1)):
        ref_str = media_url.rsplit("/", 1)[-1]
        raw_payload = share_media._b64u_decode(ref_str.split(".", 1)[0]).decode("utf-8")
        assert src_tid not in raw_payload
        parsed = json.loads(raw_payload)
        assert parsed["seg"] == 0
        assert parsed["trn"] == expected_turn
        assert "stid" not in parsed

    # Before regeneration, both turn 0 and turn 1 media links succeed
    async with _make_client() as client:
        assert (await client.get(url_t0)).status_code == 200
        assert (await client.get(url_t1)).status_code == 200

    # Source regenerates turn 1 (earlier turn 0 still shown, turn 1 query+attachment still in DB rows)
    borrowed_rows_mock.return_value = BorrowedRows(
        present=True,
        kept=False,
        seen=[(0, True), (1, False)],
        rows=src_rows,
    )
    async with _make_client() as client:
        r_t1_after = await client.get(url_t1)
        r_t0_after = await client.get(url_t0)
    assert r_t1_after.status_code == 404
    assert r_t0_after.status_code == 200
    assert serve_mock.await_args.kwargs["turn_indices"] == (0,)


@pytest.mark.asyncio
async def test_snapshot_fork_public_replay_never_persists_borrowed_or_fork_slices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 3: public replay always passes persist=False, and borrowed ancestor reads always use persist=False."""
    src_tid = str(uuid.uuid4())
    fork_tid = str(uuid.uuid4())
    share_token = "tok_no_persist_01"

    fork_dict = _make_lineage(source_thread_id=src_tid, turn_base=1, by="maker-1", snapshot=True)
    fork_thread = {
        "conversation_thread_id": fork_tid,
        "workspace_id": "ws-fork",
        "user_id": "maker-1",
        "owner_id": "maker-1",
        "is_shared": True,
        "share_permissions": {"allow_files": True},
    }
    src_thread_meta = {
        "conversation_thread_id": src_tid,
        "workspace_id": "ws-src",
        "user_id": "maker-1",
        "owner_user_id": "maker-1",
        "is_shared": True,
        "share_permissions": {"allow_files": True},
        "workspace_status": "active",
    }
    src_rows = _make_thread_rows(thread_id=src_tid, workspace_id="ws-src", turn_indexes=[0])
    fork_rows = _make_thread_rows(
        thread_id=fork_tid, workspace_id="ws-fork", turn_indexes=[1], fork_dict=fork_dict
    )
    lines_by_thread = {
        src_tid: {0: _make_turn_lines(src_tid, 0, user_text="Src Q0", assistant_text="Src A0")},
        fork_tid: {1: _make_turn_lines(fork_tid, 1, user_text="Fork Q1", assistant_text="Fork A1")},
    }
    recorded_persist: list[tuple[str, bool]] = []

    async def _recording_read_replay_page(
        rows: ThreadRows,
        *,
        source: Any = "auto",
        before_turn: Any = None,
        limit: int | None = None,
        budget: PageBudget | None = None,
        persist: bool = True,
        from_turn: Any = None,
    ) -> tuple[ReplayPage, str]:
        recorded_persist.append((rows.thread_id, persist))
        return _page_from_turn_map(
            lines_by_thread[rows.thread_id],
            from_turn=from_turn,
            before_turn=before_turn,
            limit=limit,
        )

    monkeypatch.setattr(public_app, "get_shared_thread", create_autospec(public_app.get_shared_thread, return_value=fork_thread))
    monkeypatch.setattr(public_app, "get_replay_thread_data", create_autospec(public_app.get_replay_thread_data, return_value=fork_rows))
    monkeypatch.setattr(share_access, "shared_thread_allows_files", create_autospec(share_access.shared_thread_allows_files, return_value=True))
    monkeypatch.setattr(borrowed_mod, "thread_access", create_autospec(borrowed_mod.thread_access, return_value=None))
    monkeypatch.setattr(
        fork_segments,
        "get_borrowed_rows",
        create_autospec(
            fork_segments.get_borrowed_rows,
            return_value=BorrowedRows(present=True, kept=True, seen=[], rows=src_rows),
        ),
    )
    monkeypatch.setattr(
        replay_mod,
        "read_replay_page",
        create_autospec(replay_mod.read_replay_page, side_effect=_recording_read_replay_page),
    )
    monkeypatch.setattr(replay_mod, "finish_lines", create_autospec(replay_mod.finish_lines, return_value=None))
    monkeypatch.setattr(
        share_redaction,
        "_load_threads_for_redaction",
        create_autospec(share_redaction._load_threads_for_redaction, return_value={src_tid: src_thread_meta}),
    )
    monkeypatch.setattr(
        share_media,
        "resolve_thread_media_bindings",
        create_autospec(share_media.resolve_thread_media_bindings, return_value={}),
    )

    # 1. Public share replay with allow_files=True must NEVER persist slices to fork or source
    async with _make_client() as client:
        resp = await client.get(f"/api/v1/public/shared/{share_token}/replay")
    assert resp.status_code == 200
    assert recorded_persist == [(fork_tid, False), (src_tid, False)]

    # 2. Authenticated owner read_thread_page(persist=True) persists own fork only, NEVER borrowed source
    recorded_persist.clear()
    page = await borrowed_mod.read_thread_page(fork_rows, "maker-1", persist=True)
    assert len(page.parts) == 2
    assert recorded_persist == [(fork_tid, True), (src_tid, False)]


@pytest.mark.asyncio
async def test_colleague_fork_owner_replay_sees_inherited_turns_under_dalton_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 4: workspace owner opening a colleague's snapshot fork sees actual inherited turns via Dalton require_thread_access."""
    from src.dalton import sharing as dalton_sharing
    from src.server.database import conversation as conv_db

    ws_id = str(uuid.uuid4())
    owner_id = "ws-owner-b"
    colleague_id = "colleague-c"
    src_tid = str(uuid.uuid4())
    fork_tid = str(uuid.uuid4())

    fork_dict = _make_lineage(source_thread_id=src_tid, turn_base=2, by=colleague_id, snapshot=True)
    src_rows = ThreadRows(
        thread_id=src_tid,
        owner_id=colleague_id,
        workspace_owner_id=owner_id,
        thread={"conversation_thread_id": src_tid, "workspace_id": ws_id, "fork": None},
        queries=[
            {"turn_index": 0, "content": "Colleague Q0", "metadata": {}},
            {"turn_index": 1, "content": "Colleague Q1", "metadata": {}},
        ],
        responses_by_turn={
            0: {"turn_index": 0, "conversation_response_id": "r0", "status": "completed"},
            1: {"turn_index": 1, "conversation_response_id": "r1", "status": "completed"},
        },
        usages=[],
        provenance=[],
        run_facts=[],
        open_runs={},
    )
    fork_rows = ThreadRows(
        thread_id=fork_tid,
        owner_id=colleague_id,
        workspace_owner_id=owner_id,
        thread={"conversation_thread_id": fork_tid, "workspace_id": ws_id, "fork": fork_dict},
        queries=[{"turn_index": 2, "content": "Colleague Fork Q2", "metadata": {}}],
        responses_by_turn={
            2: {"turn_index": 2, "conversation_response_id": "r2", "status": "completed"},
        },
        usages=[],
        provenance=[],
        run_facts=[],
        open_runs={},
    )
    lines_by_thread = {
        src_tid: {
            0: _make_turn_lines(src_tid, 0, user_text="Colleague Q0", assistant_text="Colleague A0"),
            1: _make_turn_lines(src_tid, 1, user_text="Colleague Q1", assistant_text="Colleague A1"),
        },
        fork_tid: {
            2: _make_turn_lines(fork_tid, 2, user_text="Colleague Fork Q2", assistant_text="Colleague Fork A2"),
        },
    }

    async def _fake_read_replay_page(
        rows: ThreadRows,
        *,
        source: Any = "auto",
        before_turn: Any = None,
        limit: int | None = None,
        budget: PageBudget | None = None,
        persist: bool = True,
        from_turn: Any = None,
    ) -> tuple[ReplayPage, str]:
        return _page_from_turn_map(
            lines_by_thread[rows.thread_id],
            from_turn=from_turn,
            before_turn=before_turn,
            limit=limit,
        )

    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    # Upstream thread_access would reject owner B because thread owner is colleague C;
    # Dalton's require_thread_access must be used instead.
    upstream_reject = create_autospec(
        borrowed_mod.thread_access,
        side_effect=HTTPException(status_code=403, detail="Forbidden"),
    )
    monkeypatch.setattr(borrowed_mod, "thread_access", upstream_reject)
    monkeypatch.setattr(
        conv_db,
        "get_thread_auth_meta",
        create_autospec(
            conv_db.get_thread_auth_meta,
            return_value={
                "conversation_thread_id": src_tid,
                "user_id": colleague_id,
                "thread_user_id": colleague_id,
                "workspace_owner_id": owner_id,
                "workspace_id": ws_id,
            },
        ),
    )
    req_access_mock = create_autospec(dalton_sharing.require_thread_access, return_value=colleague_id)
    monkeypatch.setattr(dalton_sharing, "require_thread_access", req_access_mock)
    monkeypatch.setattr(
        fork_segments,
        "get_borrowed_rows",
        create_autospec(
            fork_segments.get_borrowed_rows,
            return_value=BorrowedRows(present=True, kept=True, seen=[], rows=src_rows),
        ),
    )
    monkeypatch.setattr(
        replay_mod,
        "read_replay_page",
        create_autospec(replay_mod.read_replay_page, side_effect=_fake_read_replay_page),
    )

    page = await borrowed_mod.read_thread_page(fork_rows, owner_id, persist=False)

    upstream_reject.assert_not_awaited()
    req_access_mock.assert_awaited_once()
    assert req_access_mock.await_args.args[:2] == (src_tid, owner_id)
    assert req_access_mock.await_args.kwargs["need"] == "read"
    assert req_access_mock.await_args.kwargs["workspace_owner_id"] == owner_id

    all_items = [ln.item() for ln in page.lines()]
    user_turns = [
        (it["data"]["turn_index"], it["data"]["content"])
        for it in all_items
        if it["event"] == "user_message"
    ]
    asst_turns = [
        (it["data"]["turn_index"], it["data"]["content"])
        for it in all_items
        if it["event"] == "message_chunk"
    ]
    assert user_turns == [
        (0, "Colleague Q0"),
        (1, "Colleague Q1"),
        (2, "Colleague Fork Q2"),
    ]
    assert asst_turns == [
        (0, "Colleague A0"),
        (1, "Colleague A1"),
        (2, "Colleague Fork A2"),
    ]


@pytest.mark.asyncio
async def test_authenticated_fork_replay_rewrites_and_serves_borrowed_screenshot_and_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 5: logged-in fork replay rewrites borrowed screenshots/attachments per PagePart and re-verifies read access when serving."""
    from src.server.database import conversation as conv_db

    src_tid = str(uuid.uuid4())
    fork_tid = str(uuid.uuid4())
    viewer_id = "viewer-1"
    src_screenshot = f"https://cdn.example.com/private-media/response-images/{src_tid}/shot1.png"
    src_upload_id = "up-src-123"
    fork_screenshot = f"https://cdn.example.com/private-media/response-images/{fork_tid}/shot2.png"

    fork_dict = _make_lineage(source_thread_id=src_tid, turn_base=1, by=viewer_id, snapshot=True)
    src_rows = _make_thread_rows(
        thread_id=src_tid,
        owner_id=viewer_id,
        workspace_id="ws-src",
        turn_indexes=[0],
        queries_override=[
            {
                "turn_index": 0,
                "content": "See attached file",
                "metadata": {"attachments": [{"name": "deck.pdf", "upload_id": src_upload_id}]},
            }
        ],
    )
    fork_rows = _make_thread_rows(
        thread_id=fork_tid,
        owner_id=viewer_id,
        workspace_id="ws-fork",
        turn_indexes=[1],
        fork_dict=fork_dict,
    )
    src_part_lines = _make_turn_lines(
        src_tid,
        0,
        user_text="See attached file",
        assistant_text=f"Here is screenshot ![s]({src_screenshot})",
        user_meta={"attachments": [{"name": "deck.pdf", "upload_id": src_upload_id}]},
    )
    fork_part_lines = _make_turn_lines(
        fork_tid,
        1,
        user_text="Follow up",
        assistant_text=f"Own screenshot ![own]({fork_screenshot})",
    )
    page = borrowed_mod.ThreadPage(
        parts=[
            borrowed_mod.PagePart(src_tid, viewer_id, src_part_lines, rows=src_rows),
            borrowed_mod.PagePart(fork_tid, viewer_id, fork_part_lines, rows=fork_rows),
        ],
        first_turn_index=0,
        has_more=False,
        source="checkpoint",
        divider=line_of(
            {
                "event": "fork_divider",
                "data": {"source_thread_id": src_tid, "turn_base": 1, "state": "shown"},
            }
        ),
    )

    async def _fake_resolve_media(
        thread: Any, queries: Any, items: Any, **kwargs: Any
    ) -> dict[str, tuple[str, str]]:
        tid = str(thread.get("conversation_thread_id") or "")
        if tid == src_tid:
            return {
                src_screenshot: ("upstream", f"private-media/response-images/{src_tid}/shot1.png"),
                f"upload_id:{src_upload_id}": ("chat_upload", src_upload_id),
            }
        if tid == fork_tid:
            return {
                fork_screenshot: ("upstream", f"private-media/response-images/{fork_tid}/shot2.png"),
            }
        return {}

    monkeypatch.setattr(
        share_media,
        "resolve_thread_media_bindings",
        create_autospec(share_media.resolve_thread_media_bindings, side_effect=_fake_resolve_media),
    )
    monkeypatch.setattr(
        share_media,
        "_is_url_from_configured_storage",
        create_autospec(
            share_media._is_url_from_configured_storage,
            side_effect=lambda u: str(u).startswith("https://cdn.example.com/"),
        ),
    )

    rewritten_lines, _ = await share_media.prepare_owner_replay_media(
        None,
        fork_rows,
        page.lines(),
        thread_id=fork_tid,
        viewer_id=viewer_id,
        page=page,
    )
    rewritten_items = [ln.item() for ln in rewritten_lines]

    user0 = [it["data"] for it in rewritten_items if it["event"] == "user_message" and it["data"]["turn_index"] == 0][0]
    asst0 = [it["data"] for it in rewritten_items if it["event"] == "message_chunk" and it["data"]["turn_index"] == 0][0]
    asst1 = [it["data"] for it in rewritten_items if it["event"] == "message_chunk" and it["data"]["turn_index"] == 1][0]

    att_url = user0["metadata"]["attachments"][0]["url"]
    shot0_url = asst0["content"].split("![s](", 1)[1].rstrip(")")
    shot1_url = asst1["content"].split("![own](", 1)[1].rstrip(")")

    assert att_url.startswith(f"/api/v1/threads/{fork_tid}/media/")
    assert shot0_url.startswith(f"/api/v1/threads/{fork_tid}/media/")
    assert shot1_url.startswith(f"/api/v1/threads/{fork_tid}/media/")
    assert "[media-unavailable]" not in asst0["content"]
    assert src_screenshot not in asst0["content"]

    # Inspect borrowed token claims: opaque seg=0, trn=0, no plaintext source thread ID
    shot0_tok = shot0_url.rsplit("/", 1)[-1]
    shot0_claims = share_media.verify_owner_media_token(shot0_tok, expected_thread_id=fork_tid)
    assert shot0_claims["seg"] == 0
    assert shot0_claims["trn"] == 0
    assert src_tid not in json.dumps(shot0_claims)

    # Serve borrowed screenshot & attachment through serve_owner_thread_media with live read check
    monkeypatch.setattr(
        conv_db,
        "get_thread_by_id",
        create_autospec(
            conv_db.get_thread_by_id,
            return_value={
                "conversation_thread_id": fork_tid,
                "workspace_id": "ws-fork",
                "user_id": viewer_id,
                "fork": fork_dict,
            },
        ),
    )
    access_mock = create_autospec(shared_ws_access.thread_access, return_value=None)
    monkeypatch.setattr(shared_ws_access, "thread_access", access_mock)
    monkeypatch.setattr(
        fork_segments,
        "get_borrowed_rows",
        create_autospec(
            fork_segments.get_borrowed_rows,
            return_value=BorrowedRows(present=True, kept=True, seen=[], rows=src_rows),
        ),
    )
    serve_mock = create_autospec(
        share_media._serve_media_payload,
        return_value=public_app.Response(content=b"OWNERIMG", media_type="image/png"),
    )
    monkeypatch.setattr(share_media, "_serve_media_payload", serve_mock)

    resp_ok = await share_media.serve_owner_thread_media(
        fork_tid, shot0_tok, gate=(shot0_claims, viewer_id)
    )
    assert resp_ok.status_code == 200
    assert serve_mock.await_args.kwargs["thread_id"] == src_tid
    assert serve_mock.await_args.kwargs["turn_indices"] == (0,)
    access_mock.assert_awaited_once_with(src_tid, viewer_id)

    # Revoking source read access immediately refuses serving with 403
    access_mock.side_effect = HTTPException(status_code=403, detail="Forbidden")
    with pytest.raises(HTTPException) as exc_denied:
        await share_media.serve_owner_thread_media(
            fork_tid, shot0_tok, gate=(shot0_claims, viewer_id)
        )
    assert exc_denied.value.status_code == 403


def test_chart_annotation_skill_gates_reply_to_comment_on_artifact_comments_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 7a: chart-annotation SKILL.md omits reply_to_comment guidance when artifact comments are off."""
    skill_fp = (
        REPO_ROOT / "plugins" / "langalpha_deliverables" / "skills" / "chart-annotation" / "SKILL.md"
    )
    monkeypatch.setenv("APP_ENV", "dalton")
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "0")
    off_text = content_assembly.read_assembled_text(skill_fp)
    assert "reply_to_comment" not in off_text
    assert "## A comment on the chart" not in off_text
    assert "DALTON-HOOK" not in off_text
    assert "DALTON_CAP" not in off_text

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    on_text = content_assembly.read_assembled_text(skill_fp)
    assert "## A comment on the chart" in on_text
    assert "reply_to_comment" in on_text
    assert "DALTON-HOOK" not in on_text
    assert "DALTON_CAP" not in on_text


def test_channel_routing_fact_prunes_unsupported_apps_and_follows_channel_caps() -> None:
    """Item 7b: channel-routing.md omits Slack/Telegram/iMessage and renders only enabled Dalton channels."""
    from src.dalton.capabilities import CapabilityId

    routing_fp = LANGALPHA_DOC_DIR / "facts" / "channel-routing.md"

    everflow_caps = frozenset(
        {
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.CHANNEL_FEISHU,
            CapabilityId.CHANNEL_DISCORD,
            CapabilityId.CHANNEL_WEIXIN,
        }
    )
    everflow_text = content_assembly.read_assembled_text(routing_fp, capabilities=everflow_caps)
    for expected in ("## Feishu", "## Discord", "## Personal WeChat"):
        assert expected in everflow_text
    for forbidden in ("Slack", "Telegram", "iMessage", "DALTON-HOOK", "DALTON_CAP"):
        assert forbidden not in everflow_text

    core_text = content_assembly.read_assembled_text(routing_fp, capabilities=frozenset())
    for forbidden in ("Slack", "Telegram", "iMessage", "Feishu", "Discord", "WeChat", "DALTON-HOOK", "DALTON_CAP"):
        assert forbidden not in core_text

    discord_only = content_assembly.read_assembled_text(
        routing_fp,
        capabilities=frozenset({CapabilityId.CHANNEL_GATEWAY, CapabilityId.CHANNEL_DISCORD}),
    )
    assert "## Discord" in discord_only
    for forbidden in ("Slack", "Telegram", "iMessage", "Feishu", "WeChat", "DALTON-HOOK", "DALTON_CAP"):
        assert forbidden not in discord_only
