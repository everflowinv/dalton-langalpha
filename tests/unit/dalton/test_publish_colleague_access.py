"""Colleague access to the publish pipeline and owner-only auto-tracking.

The PM's workspace is the unit of publication governance: a teammate with rw
on the workspace may run ``check_draft`` / ``check_language`` / ``publish_report``
(whose request and publication rows then belong to the workspace owner), while
an ro colleague, an outsider, or anyone but the owner on auto-tracking stays
refused — the tracking refusal says why instead of the generic 403.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi import HTTPException

from src.config import settings as app_settings
from src.dalton import sharing as dalton_sharing
from src.dalton.sharing import get_workspace_sharing as _REAL_GET_SHARING
from src.server.database import user as user_db
from src.server.database import workspace as ws_db
from src.server.database.pool import get_db_connection as _REAL_GET_DB_CONN
from src.server.database.user import get_user as _REAL_GET_USER
from src.server.database.workspace import get_workspace as _REAL_GET_WORKSPACE
from src.dalton.database.knowledge import (
    list_all_latest_for_workspace as _REAL_LIST_LATEST,
)
from src.server.database.conversation import get_thread_by_id as _REAL_GET_THREAD_BY_ID

PM_USER_ID = "11111111-1111-4111-8111-111111111111"
COLLEAGUE_USER_ID = "22222222-2222-4222-8222-222222222222"
ANOTHER_PM_USER_ID = "44444444-4444-4444-8444-444444444444"
OTHER_COLLEAGUE_USER_ID = "55555555-5555-4555-8555-555555555555"
OUTSIDER_USER_ID = "outsider-user"
WS_ID = "33333333-3333-4333-8333-333333333333"

TEAM_EMAILS = (
    "pm@dalton.test,colleague@dalton.test,"
    "another-pm@dalton.test,other@dalton.test"
)

USERS_BY_ID: dict[str, dict[str, Any]] = {
    PM_USER_ID: {
        "user_id": PM_USER_ID,
        "email": "pm@dalton.test",
        "name": "PM Owner",
    },
    COLLEAGUE_USER_ID: {
        "user_id": COLLEAGUE_USER_ID,
        "email": "colleague@dalton.test",
        "name": "RW Colleague",
    },
    ANOTHER_PM_USER_ID: {
        "user_id": ANOTHER_PM_USER_ID,
        "email": "another-pm@dalton.test",
        "name": "Another PM",
    },
    OTHER_COLLEAGUE_USER_ID: {
        "user_id": OTHER_COLLEAGUE_USER_ID,
        "email": "other@dalton.test",
        "name": "Other Colleague",
    },
    OUTSIDER_USER_ID: {
        "user_id": OUTSIDER_USER_ID,
        "email": "outsider@other.com",
        "name": "Outsider",
    },
}


def _pm_workspace() -> dict[str, Any]:
    return {
        "workspace_id": WS_ID,
        "user_id": PM_USER_ID,
        "name": "META",
        "status": "running",
        "is_home": False,
    }


def _install_real_access_chain(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cos_access: str = "rw",
) -> dict[str, Any]:
    """Drive the REAL ``verify_workspace_access`` -> ``require_workspace_access``
    chain against a PM-owned workspace row and a mutable sharing row.

    Dispatch is off so the workspace sharing row (not the owner's user-level
    cos switch) decides colleague access; the returned holder lets one test
    flip ``cos_access`` between scenarios.
    """
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setenv("DALTON_ALLOWED_EMAILS", TEAM_EMAILS)
    monkeypatch.setenv("DALTON_COLLEAGUE_DISPATCH_ENABLED", "0")
    monkeypatch.setenv("DALTON_PM_USER_IDS", PM_USER_ID)

    async def _get_user(uid: str) -> dict[str, Any] | None:
        return USERS_BY_ID.get(uid)

    monkeypatch.setattr(
        user_db,
        "get_user",
        create_autospec(_REAL_GET_USER, side_effect=_get_user),
    )
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(_REAL_GET_WORKSPACE, return_value=_pm_workspace()),
    )

    holder: dict[str, Any] = {
        "workspace_id": WS_ID,
        "cos_access": cos_access,
        "web_access": "hidden",
        "updated_by": None,
        "updated_at": None,
    }

    async def _sharing(workspace_id: str, *, conn: Any = None) -> dict[str, Any]:
        return dict(holder)

    monkeypatch.setattr(
        dalton_sharing,
        "get_workspace_sharing",
        create_autospec(_REAL_GET_SHARING, side_effect=_sharing),
    )
    return holder


class _BackendProto:
    async def aread_text(self, path: str) -> str | None: ...
    async def awrite_text(self, path: str, content: str) -> None: ...


def _memory_backend(files: dict[str, str] | None = None) -> Any:
    store = dict(files or {})

    def _read(path: str) -> str | None:
        return store.get(path)

    def _write(path: str, content: str) -> None:
        store[path] = content

    backend = create_autospec(_BackendProto, instance=True)
    backend.aread_text.side_effect = _read
    backend.awrite_text.side_effect = _write
    backend.files_store = store
    return backend


DRAFT = "## META 定性纪要\n\n本文为定性讨论，不引用任何数字。"


def _install_check_draft_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.publish import service as publish_svc

    monkeypatch.setattr(
        publish_svc,
        "resolve_audit_config_fingerprint",
        create_autospec(
            publish_svc.resolve_audit_config_fingerprint,
            return_value=("audit-model", ["fallback-model"], "300", "cfg-sha"),
        ),
    )
    monkeypatch.setattr(
        db_knowledge,
        "latest_valid_version",
        create_autospec(db_knowledge.latest_valid_version, return_value=None),
    )
    monkeypatch.setattr(
        publish_svc,
        "_load_workspace_provenance",
        create_autospec(publish_svc._load_workspace_provenance, return_value={}),
    )


@pytest.mark.asyncio
async def test_check_draft_access_matrix_owner_rw_ro_outsider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """check_draft admits the owner and an rw colleague; an ro colleague and an
    outsider are refused by the real sharing chain (403)."""
    from src.dalton.publish import service as publish_svc

    holder = _install_real_access_chain(monkeypatch, cos_access="rw")
    _install_check_draft_stubs(monkeypatch)
    backend = _memory_backend({"drafts/meta.md": DRAFT})

    owner_res = await publish_svc.run_check_draft(
        workspace_id=WS_ID,
        draft_path="drafts/meta.md",
        backend=backend,
        use_llm_judge=False,
        user_id=PM_USER_ID,
    )
    assert owner_res["ok"] is True, owner_res

    rw_res = await publish_svc.run_check_draft(
        workspace_id=WS_ID,
        draft_path="drafts/meta.md",
        backend=backend,
        use_llm_judge=False,
        user_id=COLLEAGUE_USER_ID,
    )
    assert rw_res["ok"] is True, rw_res

    holder["cos_access"] = "ro"
    with pytest.raises(HTTPException) as exc_ro:
        await publish_svc.run_check_draft(
            workspace_id=WS_ID,
            draft_path="drafts/meta.md",
            backend=backend,
            use_llm_judge=False,
            user_id=COLLEAGUE_USER_ID,
        )
    assert exc_ro.value.status_code == 403

    with pytest.raises(HTTPException) as exc_outsider:
        await publish_svc.run_check_draft(
            workspace_id=WS_ID,
            draft_path="drafts/meta.md",
            backend=backend,
            use_llm_judge=False,
            user_id=OUTSIDER_USER_ID,
        )
    assert exc_outsider.value.status_code == 403


@pytest.mark.asyncio
async def test_check_language_access_matrix_owner_rw_ro_outsider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """check_language admits the owner and an rw colleague; an ro colleague and
    an outsider are refused by the real sharing chain (403)."""
    from src.dalton.publish import language as language_mod

    holder = _install_real_access_chain(monkeypatch, cos_access="rw")
    sentinel = {"ok": True, "message": "locked-body-ran"}
    monkeypatch.setattr(
        language_mod,
        "_run_check_language_locked",
        create_autospec(
            language_mod._run_check_language_locked, return_value=sentinel
        ),
    )
    backend = _memory_backend({"drafts/meta.md": DRAFT})

    assert (
        await language_mod.run_check_language(
            workspace_id=WS_ID,
            draft_path="drafts/meta.md",
            round=1,
            backend=backend,
            user_id=PM_USER_ID,
        )
        == sentinel
    )
    assert (
        await language_mod.run_check_language(
            workspace_id=WS_ID,
            draft_path="drafts/meta.md",
            round=1,
            backend=backend,
            user_id=COLLEAGUE_USER_ID,
        )
        == sentinel
    )

    holder["cos_access"] = "ro"
    with pytest.raises(HTTPException) as exc_ro:
        await language_mod.run_check_language(
            workspace_id=WS_ID,
            draft_path="drafts/meta.md",
            round=1,
            backend=backend,
            user_id=COLLEAGUE_USER_ID,
        )
    assert exc_ro.value.status_code == 403

    with pytest.raises(HTTPException) as exc_outsider:
        await language_mod.run_check_language(
            workspace_id=WS_ID,
            draft_path="drafts/meta.md",
            round=1,
            backend=backend,
            user_id=OUTSIDER_USER_ID,
        )
    assert exc_outsider.value.status_code == 403


@pytest.mark.asyncio
async def test_run_publish_access_matrix_owner_rw_ro_outsider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_publish admits the owner and an rw colleague (proved by reaching the
    missing-draft exit past the access check); an ro colleague and an outsider
    are refused by the real sharing chain (403)."""
    from src.dalton.publish import service as publish_svc

    holder = _install_real_access_chain(monkeypatch, cos_access="rw")
    backend = _memory_backend()  # no draft file on purpose

    for uid in (PM_USER_ID, COLLEAGUE_USER_ID):
        res = await publish_svc.run_publish(
            user_id=uid,
            workspace_id=WS_ID,
            thread_id="t-1",
            tool_call_id="tc-1",
            draft_path="drafts/meta.md",
            title="META",
            kind="initial_coverage",
            slug="meta",
            backend=backend,
            author_model="test-model",
        )
        assert res["ok"] is False
        assert "Draft file not found" in res["message"]

    holder["cos_access"] = "ro"
    with pytest.raises(HTTPException) as exc_ro:
        await publish_svc.run_publish(
            user_id=COLLEAGUE_USER_ID,
            workspace_id=WS_ID,
            thread_id="t-1",
            tool_call_id="tc-1",
            draft_path="drafts/meta.md",
            title="META",
            kind="initial_coverage",
            slug="meta",
            backend=backend,
            author_model="test-model",
        )
    assert exc_ro.value.status_code == 403

    with pytest.raises(HTTPException) as exc_outsider:
        await publish_svc.run_publish(
            user_id=OUTSIDER_USER_ID,
            workspace_id=WS_ID,
            thread_id="t-1",
            tool_call_id="tc-1",
            draft_path="drafts/meta.md",
            title="META",
            kind="initial_coverage",
            slug="meta",
            backend=backend,
            author_model="test-model",
        )
    assert exc_outsider.value.status_code == 403


async def _install_publish_state_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    row: dict[str, Any],
    created: bool = True,
) -> dict[str, Any]:
    """Stub the publish state machine's collaborators; returns a call log.

    Round-3 decision 5: access goes through the REAL chain
    (``verify_workspace_access`` -> ``require_workspace_access``) with a real
    sharing row — only the database and external services are doubled. The
    caller identity is admitted by that chain (owner passes directly; an rw
    colleague passes ``need='write'`` on 'cos').
    The request row carries the real columns the round-3 checks read
    (``conversation_thread_id`` for initiator resolution, ``title``/``kind``/
    ``slug``/``draft_path``/``updates_thesis``/``thesis_version_no``/
    ``bundle_sha256`` for the request-row-is-authoritative design); the
    initiator thread lookup defaults to "no thread row" (initiator unknown)
    — individual tests override it to name a colleague thread."""
    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.database import publications as db_publications
    from src.dalton.publish import service as publish_svc
    from src.dalton.publish.audit import aaudit_draft
    from src.dalton.publish.files import compute_bundle_sha256
    from src.dalton.publish.review import ReviewResult, ReviewVerdict
    from src.server.database import conversation as conv_db

    _install_real_access_chain(monkeypatch, cos_access="rw")

    calls: dict[str, Any] = {
        "get_or_create": {},
        "handoff": {},
        "insert_pub": {},
        "write_bundle": {},
        "transitions": [],
        "transition_cols": [],
    }

    if not row.get("bundle_sha256"):
        row["bundle_sha256"] = compute_bundle_sha256(DRAFT.encode("utf-8"), [])
    row.setdefault("title", "META")
    row.setdefault("kind", "initial_coverage")
    row.setdefault("draft_path", "drafts/meta.md")

    passing_audit = await aaudit_draft(
        DRAFT,
        use_llm_judge=False,
        user_id=PM_USER_ID,
    )
    assert passing_audit.passed

    monkeypatch.setattr(
        conv_db,
        "get_thread_by_id",
        create_autospec(_REAL_GET_THREAD_BY_ID, return_value=None),
    )
    monkeypatch.setattr(
        publish_svc,
        "resolve_audit_config_fingerprint",
        create_autospec(
            publish_svc.resolve_audit_config_fingerprint,
            return_value=("audit-model", ["fallback-model"], "300", "cfg-sha"),
        ),
    )
    monkeypatch.setattr(
        publish_svc,
        "aaudit_draft",
        create_autospec(publish_svc.aaudit_draft, return_value=passing_audit),
    )
    monkeypatch.setattr(
        publish_svc,
        "_load_workspace_provenance",
        create_autospec(publish_svc._load_workspace_provenance, return_value={}),
    )
    monkeypatch.setattr(
        db_knowledge,
        "list_all_latest_for_workspace",
        create_autospec(db_knowledge.list_all_latest_for_workspace, return_value={}),
    )

    async def _get_or_create(**kwargs: Any) -> tuple[dict[str, Any], bool]:
        calls["get_or_create"] = kwargs
        # Like the real DB: the row carries the (thread, tool_call) identity
        # it was filed from (round-4 decision 4).
        row.setdefault("conversation_thread_id", kwargs.get("thread_id"))
        return dict(row), created

    monkeypatch.setattr(
        db_publications,
        "get_or_create_request",
        create_autospec(
            db_publications.get_or_create_request, side_effect=_get_or_create
        ),
    )

    async def _transition(
        publish_request_id: str,
        from_statuses: Any,
        to: str,
        **cols: Any,
    ) -> dict[str, Any]:
        calls["transitions"].append((publish_request_id, tuple(from_statuses), to))
        calls["transition_cols"].append(dict(cols))
        row["status"] = to
        row.update(cols)
        return dict(row)

    monkeypatch.setattr(
        db_publications,
        "transition",
        create_autospec(db_publications.transition, side_effect=_transition),
    )
    monkeypatch.setattr(
        db_publications,
        "get_request",
        create_autospec(
            db_publications.get_request, side_effect=lambda _rid: dict(row)
        ),
    )
    monkeypatch.setattr(
        db_publications,
        "resolve_thread_author_model",
        create_autospec(
            db_publications.resolve_thread_author_model,
            return_value="claude-opus-5-5-oauth",
        ),
    )
    monkeypatch.setattr(
        publish_svc,
        "run_review",
        create_autospec(
            publish_svc.run_review,
            return_value=ReviewResult(
                status="done",
                model="gpt-6.1-sol-oauth",
                family="openai-gpt",
                verdict=ReviewVerdict(
                    verdict="pass", summary="ok", items=[], must_fix=[], nice_to_have=[]
                ),
            ),
        ),
    )

    async def _handoff(**kwargs: Any) -> None:
        calls["handoff"] = kwargs

    monkeypatch.setattr(
        publish_svc,
        "_append_agenda_pm_handoff",
        create_autospec(publish_svc._append_agenda_pm_handoff, side_effect=_handoff),
    )

    async def _insert_pub(**kwargs: Any) -> dict[str, Any]:
        calls["insert_pub"] = kwargs
        return {"publication_id": "pub-1", "files": kwargs.get("files") or []}

    monkeypatch.setattr(
        db_publications,
        "insert_publication",
        create_autospec(db_publications.insert_publication, side_effect=_insert_pub),
    )
    monkeypatch.setattr(
        db_publications,
        "consume_approval",
        create_autospec(
            db_publications.consume_approval,
            side_effect=lambda _rid, _sha: {**row, "status": "publishing"},
        ),
    )
    monkeypatch.setattr(
        db_publications,
        "list_publications",
        create_autospec(db_publications.list_publications, return_value=[]),
    )
    async def _write_bundle(**kwargs: Any) -> tuple[list, list, list]:
        calls["write_bundle"] = kwargs
        return (
            [
                {
                    "path": f"published/2026-11-02-{kwargs.get('slug')}.md",
                    "sha256": "x",
                    "role": "md",
                }
            ],
            [],
            [],
        )

    monkeypatch.setattr(
        publish_svc,
        "write_published_bundle",
        create_autospec(
            publish_svc.write_published_bundle, side_effect=_write_bundle
        ),
    )
    return calls


@pytest.mark.asyncio
async def test_colleague_publish_automation_path_attributes_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A colleague-initiated publish on the automation path creates the request
    under the workspace owner and hands the approval off to that owner, naming
    the initiator resolved from the request row's thread (decision C)."""
    from src.dalton.publish import service as publish_svc
    from src.server.database import conversation as conv_db

    row: dict[str, Any] = {
        "publish_request_id": "req-1",
        "workspace_id": WS_ID,
        "user_id": PM_USER_ID,
        "conversation_thread_id": "thread-colleague-1",
        "status": "auditing",
        "audit": None,
    }
    calls = await _install_publish_state_stubs(monkeypatch, row=row)
    # Decision C: the initiator is the request row's thread owner, resolved
    # fresh on every read — override the default "no thread row" stub.
    monkeypatch.setattr(
        conv_db,
        "get_thread_by_id",
        create_autospec(
            _REAL_GET_THREAD_BY_ID,
            return_value={
                "conversation_thread_id": "thread-colleague-1",
                "user_id": COLLEAGUE_USER_ID,
            },
        ),
    )
    backend = _memory_backend({"drafts/meta.md": DRAFT})

    res = await publish_svc.run_publish(
        user_id=COLLEAGUE_USER_ID,
        workspace_id=WS_ID,
        thread_id="t-1",
        tool_call_id="tc-1",
        draft_path="drafts/meta.md",
        title="META",
        kind="initial_coverage",
        slug="meta",
        backend=backend,
        author_model="test-model",
        origin_type="automation",
    )
    assert res["status"] == "awaiting_pm", res
    assert calls["get_or_create"]["user_id"] == PM_USER_ID
    assert calls["handoff"]["user_id"] == PM_USER_ID
    assert f"由 {COLLEAGUE_USER_ID} 发起" in calls["handoff"]["what"]


@pytest.mark.asyncio
async def test_colleague_initiated_publish_takes_unattended_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision A: a colleague-initiated interactive publish never emits an
    approval card in the colleague's thread — it stops at awaiting_pm, hands
    off to the owner's agenda (naming the initiator, decision 5), and tells
    the colleague the request_id the owner can pick up in their own thread."""
    from src.dalton.publish import service as publish_svc
    from src.server.database import conversation as conv_db

    row: dict[str, Any] = {
        "publish_request_id": "req-1",
        "workspace_id": WS_ID,
        "user_id": PM_USER_ID,
        "conversation_thread_id": "thread-colleague-1",
        "status": "auditing",
        "audit": None,
    }
    calls = await _install_publish_state_stubs(monkeypatch, row=row)
    monkeypatch.setattr(
        conv_db,
        "get_thread_by_id",
        create_autospec(
            _REAL_GET_THREAD_BY_ID,
            return_value={
                "conversation_thread_id": "thread-colleague-1",
                "user_id": COLLEAGUE_USER_ID,
            },
        ),
    )
    backend = _memory_backend({"drafts/meta.md": DRAFT})

    def _must_not_interrupt(payload: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("colleague-initiated publish must not emit an approval card")

    res = await publish_svc.run_publish(
        user_id=COLLEAGUE_USER_ID,
        workspace_id=WS_ID,
        thread_id="t-1",
        tool_call_id="tc-1",
        draft_path="drafts/meta.md",
        title="META",
        kind="initial_coverage",
        slug="meta",
        backend=backend,
        author_model="test-model",
        origin_type="user",
        interrupt_fn=_must_not_interrupt,
    )
    assert res["status"] == "awaiting_pm", res
    assert res["request_id"] == "req-1"
    assert "request_id=req-1" in res["message"]
    assert "所有者在自己的线程里用这个 request_id" in res["message"]
    assert calls["handoff"]["user_id"] == PM_USER_ID
    assert f"由 {COLLEAGUE_USER_ID} 发起" in calls["handoff"]["what"]


@pytest.mark.asyncio
async def test_owner_takeover_card_names_thread_initiator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decisions C/D: the owner picks a colleague-filed request up by
    request_id in their own thread; the approval card names the ORIGINAL
    initiator (the request row's thread owner), and the owner may approve it."""
    from src.dalton.publish import service as publish_svc
    from src.server.database import conversation as conv_db

    row: dict[str, Any] = {
        "publish_request_id": "req-1",
        "workspace_id": WS_ID,
        "user_id": PM_USER_ID,
        "conversation_thread_id": "thread-colleague-1",
        "status": "awaiting_pm",
        "audit": None,
    }
    calls = await _install_publish_state_stubs(monkeypatch, row=row)
    monkeypatch.setattr(
        conv_db,
        "get_thread_by_id",
        create_autospec(
            _REAL_GET_THREAD_BY_ID,
            return_value={
                "conversation_thread_id": "thread-colleague-1",
                "user_id": COLLEAGUE_USER_ID,
            },
        ),
    )
    backend = _memory_backend({"drafts/meta.md": DRAFT})

    captured: list[dict[str, Any]] = []

    def _answer(payload: dict[str, Any]) -> dict[str, Any]:
        captured.append(payload)
        return {"order_decisions": {"req-1": {"type": "approve", "message": "ok"}}}

    res = await publish_svc.run_publish(
        user_id=PM_USER_ID,
        workspace_id=WS_ID,
        thread_id="t-pm",
        tool_call_id="tc-pm",
        draft_path="drafts/meta.md",
        title="META",
        kind="initial_coverage",
        slug="meta",
        request_id="req-1",
        backend=backend,
        author_model="test-model",
        origin_type="user",
        interrupt_fn=_answer,
    )
    assert res["status"] == "published", res
    args = captured[0]["action_requests"][0]["args"]
    assert args["initiated_by"] == COLLEAGUE_USER_ID
    assert args["发起人"] == COLLEAGUE_USER_ID
    assert calls["insert_pub"]["user_id"] == PM_USER_ID
    assert calls["insert_pub"]["approver_user_id"] == PM_USER_ID


@pytest.mark.asyncio
async def test_another_pm_cannot_approve_foreign_workspace_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision B: another PM-listed colleague has no approve/reject path into
    someone else's workspace — the unattended split (decision A) stops them at
    awaiting_pm before any card (the in-card owner check is the second gate),
    and request_id re-entry is refused outright (decision D)."""
    from src.dalton.publish import service as publish_svc
    from src.server.database import conversation as conv_db

    another_pm = ANOTHER_PM_USER_ID
    row: dict[str, Any] = {
        "publish_request_id": "req-1",
        "workspace_id": WS_ID,
        "user_id": PM_USER_ID,
        "conversation_thread_id": "thread-colleague-1",
        "status": "auditing",
        "audit": None,
    }
    calls = await _install_publish_state_stubs(monkeypatch, row=row)
    # The real-access chain installs a single-PM roster; this test needs both
    # PMs listed, so it overrides AFTER the stubs are in place.
    monkeypatch.setenv("DALTON_PM_USER_IDS", f"{PM_USER_ID},{another_pm}")
    monkeypatch.setattr(
        conv_db,
        "get_thread_by_id",
        create_autospec(
            _REAL_GET_THREAD_BY_ID,
            return_value={
                "conversation_thread_id": "thread-colleague-1",
                "user_id": COLLEAGUE_USER_ID,
            },
        ),
    )
    backend = _memory_backend({"drafts/meta.md": DRAFT})

    def _must_not_interrupt(payload: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("a non-owner must never be handed an approval card")

    # Interactive call by the other PM (no request_id, get_or_create path):
    # stopped at awaiting_pm and pointed at the workspace owner.
    res = await publish_svc.run_publish(
        user_id=another_pm,
        workspace_id=WS_ID,
        thread_id="t-pm2",
        tool_call_id="tc-pm2",
        draft_path="drafts/meta.md",
        title="META",
        kind="initial_coverage",
        slug="meta",
        backend=backend,
        author_model="test-model",
        origin_type="user",
        interrupt_fn=_must_not_interrupt,
    )
    assert res["status"] == "awaiting_pm", res
    assert "已提交给工作区所有者审批" in res["message"]
    assert all(t[2] not in ("approved", "rejected") for t in calls["transitions"])

    # Re-entry by request_id is refused outright: the other PM is neither the
    # workspace owner nor the row-thread's initiator.
    with pytest.raises(HTTPException) as exc_reentry:
        await publish_svc.run_publish(
            user_id=another_pm,
            workspace_id=WS_ID,
            thread_id="t-pm2",
            tool_call_id="tc-pm2b",
            draft_path="drafts/meta.md",
            title="META",
            kind="initial_coverage",
            slug="meta",
            request_id="req-1",
            backend=backend,
            author_model="test-model",
        )
    assert exc_reentry.value.status_code == 403


@pytest.mark.asyncio
async def test_request_id_reentry_refuses_other_colleague(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision D: request_id re-entry is the owner's or the original
    initiator's (resolved from the row's thread); any other colleague gets 403."""
    from src.dalton.publish import service as publish_svc
    from src.server.database import conversation as conv_db

    other_colleague = OTHER_COLLEAGUE_USER_ID
    row: dict[str, Any] = {
        "publish_request_id": "req-1",
        "workspace_id": WS_ID,
        "user_id": PM_USER_ID,
        "conversation_thread_id": "thread-colleague-1",
        "status": "awaiting_pm",
        "audit": None,
    }
    await _install_publish_state_stubs(monkeypatch, row=row)
    monkeypatch.setattr(
        conv_db,
        "get_thread_by_id",
        create_autospec(
            _REAL_GET_THREAD_BY_ID,
            return_value={
                "conversation_thread_id": "thread-colleague-1",
                "user_id": COLLEAGUE_USER_ID,
            },
        ),
    )
    backend = _memory_backend({"drafts/meta.md": DRAFT})

    with pytest.raises(HTTPException) as exc_reentry:
        await publish_svc.run_publish(
            user_id=other_colleague,
            workspace_id=WS_ID,
            thread_id="t-other",
            tool_call_id="tc-other",
            draft_path="drafts/meta.md",
            title="META",
            kind="initial_coverage",
            slug="meta",
            request_id="req-1",
            backend=backend,
            author_model="test-model",
        )
    assert exc_reentry.value.status_code == 403
    assert (
        exc_reentry.value.detail
        == "Publication request does not belong to current user"
    )

    # The original initiator may still re-enter their own request.
    initiator_res = await publish_svc.run_publish(
        user_id=COLLEAGUE_USER_ID,
        workspace_id=WS_ID,
        thread_id="thread-colleague-1",
        tool_call_id="tc-colleague",
        draft_path="drafts/meta.md",
        title="META",
        kind="initial_coverage",
        slug="meta",
        request_id="req-1",
        backend=backend,
        author_model="test-model",
        origin_type="user",
        interrupt_fn=lambda _p: {
            "order_decisions": {"req-1": {"type": "approve", "message": ""}}
        },
    )
    # The colleague passes re-entry but decision A routes them to the
    # unattended path before any card; decision B keeps the request waiting.
    assert initiator_res["status"] == "awaiting_pm", initiator_res
    assert "request_id=req-1" in initiator_res["message"]


@pytest.mark.asyncio
async def test_publishing_resume_rejects_changed_draft_not_changed_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-3 decision 1: on the publishing resume path a CHANGED DRAFT
    (bundle mismatch) voids the approval (superseded); a merely different
    title/slug in the call is ignored in favour of the row's params and the
    publish completes with them."""
    from src.dalton.publish import service as publish_svc

    row: dict[str, Any] = {
        "publish_request_id": "req-1",
        "workspace_id": WS_ID,
        "user_id": PM_USER_ID,
        "conversation_thread_id": "t-pm",
        "status": "publishing",
        "bundle_sha256": "0" * 64,
        "title": "META",
        "kind": "initial_coverage",
        "slug": "meta",
        "draft_path": "drafts/meta.md",
        "audit": None,
    }
    calls = await _install_publish_state_stubs(monkeypatch, row=row)
    backend = _memory_backend({"drafts/meta.md": DRAFT})

    res_bundle = await publish_svc.run_publish(
        user_id=PM_USER_ID,
        workspace_id=WS_ID,
        thread_id="t-pm",
        tool_call_id="tc-pm",
        draft_path="drafts/meta.md",
        title="META",
        kind="initial_coverage",
        slug="meta",
        request_id="req-1",
        backend=backend,
        author_model="test-model",
    )
    assert res_bundle["status"] == "superseded", res_bundle
    assert "重新调用 publish_report" in res_bundle["message"]
    assert ("req-1", ("publishing",), "superseded") in calls["transitions"]
    # No publication row was ever inserted off the stale approval.
    assert calls["insert_pub"] == {}

    # Same-content resume with DIFFERENT caller params: the row wins, the
    # differences are only reported, and the publish completes.
    from src.dalton.publish.files import compute_bundle_sha256

    row["status"] = "publishing"
    row["bundle_sha256"] = compute_bundle_sha256(DRAFT.encode("utf-8"), [])
    res_ignored = await publish_svc.run_publish(
        user_id=PM_USER_ID,
        workspace_id=WS_ID,
        thread_id="t-pm",
        tool_call_id="tc-pm",
        draft_path="drafts/meta.md",
        title="META 改名",
        kind="initial_coverage",
        slug="meta-renamed",
        request_id="req-1",
        backend=backend,
        author_model="test-model",
    )
    assert res_ignored["status"] == "published", res_ignored
    assert "忽略本次传入的 title、slug" in res_ignored["message"]
    assert res_ignored.get("ignored_params") == ["title", "slug"]
    # The publication row carries the row's params, not the caller's.
    assert calls["insert_pub"]["title"] == "META"
    assert calls["write_bundle"]["slug"] == "meta"


@pytest.mark.asyncio
async def test_owner_publish_records_owner_rows_and_approver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owner's own publish keeps owner rows end to end: publication row,
    approver, and no initiator annotation on the approval card."""
    from src.dalton.publish import service as publish_svc

    row: dict[str, Any] = {
        "publish_request_id": "req-2",
        "workspace_id": WS_ID,
        "user_id": PM_USER_ID,
        "status": "auditing",
        "bundle_sha256": None,
        "audit": None,
    }
    calls = await _install_publish_state_stubs(monkeypatch, row=row)
    backend = _memory_backend({"drafts/meta.md": DRAFT})

    captured: list[dict[str, Any]] = []

    def _answer(payload: dict[str, Any]) -> dict[str, Any]:
        captured.append(payload)
        return {"order_decisions": {"req-2": {"type": "approve", "message": "ok"}}}

    res = await publish_svc.run_publish(
        user_id=PM_USER_ID,
        workspace_id=WS_ID,
        thread_id="t-2",
        tool_call_id="tc-2",
        draft_path="drafts/meta.md",
        title="META",
        kind="initial_coverage",
        slug="meta",
        backend=backend,
        author_model="test-model",
        origin_type="user",
        interrupt_fn=_answer,
    )
    assert res["status"] == "published", res
    assert calls["get_or_create"]["user_id"] == PM_USER_ID
    assert calls["insert_pub"]["user_id"] == PM_USER_ID
    assert calls["insert_pub"]["approver_user_id"] == PM_USER_ID
    args = captured[0]["action_requests"][0]["args"]
    assert "initiated_by" not in args
    assert "发起人" not in args


@pytest.mark.asyncio
async def test_auto_tracking_refusal_names_owner_only_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-tracking stays owner-only: the Chief-of-Staff branch's real owner
    check and the scheduling-layer 403 both surface the reason, not the generic
    'Workspace does not belong to current user'."""
    import src.dalton.autonomy.tracking as tracking_mod
    import src.dalton.tools.director_tools as dt_mod
    from src.dalton.tools.director_tools import (
        TRACKING_OWNER_ONLY_DETAIL,
        build_director_tools,
    )

    holder = _install_real_access_chain(monkeypatch, cos_access="rw")
    assert holder  # rw colleague is still not enough: tracking is owner-only

    cos_tool = {
        t.name: t for t in build_director_tools(role="chief_of_staff")
    }["dalton_coverage_tracking"]
    analyst_tool = {
        t.name: t for t in build_director_tools(role="analyst")
    }["dalton_coverage_tracking"]

    # Chief of Staff naming the PM's workspace: the tool's own owner check 403s.
    with pytest.raises(HTTPException) as exc_cos:
        await cos_tool.ainvoke(
            {"action": "enable", "workspace_id": WS_ID},
            config={"configurable": {"user_id": COLLEAGUE_USER_ID, "workspace_id": "home"}},
        )
    assert exc_cos.value.status_code == 403
    assert exc_cos.value.detail == TRACKING_OWNER_ONLY_DETAIL

    # Analyst in the PM's workspace: the scheduling layer's generic 403 is
    # re-raised with the same reason. The ticker resolver is imported lazily
    # from tracking; enable_tracking is bound in director_tools at import.
    real_enable_tracking = dt_mod.enable_tracking
    monkeypatch.setattr(
        tracking_mod,
        "_resolve_workspace_ticker",
        create_autospec(tracking_mod._resolve_workspace_ticker, return_value="META"),
    )
    monkeypatch.setattr(
        dt_mod,
        "enable_tracking",
        create_autospec(
            real_enable_tracking,
            side_effect=HTTPException(
                status_code=403, detail="Workspace does not belong to current user"
            ),
        ),
    )
    with pytest.raises(HTTPException) as exc_analyst:
        await analyst_tool.ainvoke(
            {"action": "enable"},
            config={
                "configurable": {"user_id": COLLEAGUE_USER_ID, "workspace_id": WS_ID}
            },
        )
    assert exc_analyst.value.status_code == 403
    assert exc_analyst.value.detail == TRACKING_OWNER_ONLY_DETAIL

    # The owner is unaffected: enable runs through and returns its result.
    monkeypatch.setattr(
        dt_mod,
        "enable_tracking",
        create_autospec(
            real_enable_tracking,
            return_value={"ok": True, "status": "enabled", "paused_by_user": False},
        ),
    )
    owner_res = await analyst_tool.ainvoke(
        {"action": "enable"},
        config={"configurable": {"user_id": PM_USER_ID, "workspace_id": WS_ID}},
    )
    assert "enabled" in owner_res


@pytest.mark.asyncio
async def test_tracking_status_open_to_read_colleague_reported_for_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision F: status is open to a colleague with read access, and the
    state is reported from the WORKSPACE OWNER's automation rows (a colleague
    has none — reporting by caller would show enabled tracking as disabled)."""
    import src.dalton.tools.director_tools as dt_mod
    from src.dalton.tools.director_tools import build_director_tools

    holder = _install_real_access_chain(monkeypatch, cos_access="ro")
    assert holder

    analyst_tool = {
        t.name: t for t in build_director_tools(role="analyst")
    }["dalton_coverage_tracking"]
    seen: list[tuple[str, str]] = []
    real_get_status = dt_mod.get_tracking_status

    async def _get_status(user_id: str, workspace_id: str) -> dict[str, Any]:
        seen.append((user_id, workspace_id))
        return {"ok": True, "status": "enabled", "workspace_id": workspace_id}

    monkeypatch.setattr(
        dt_mod,
        "get_tracking_status",
        create_autospec(real_get_status, side_effect=_get_status),
    )

    colleague_res = await analyst_tool.ainvoke(
        {"action": "status"},
        config={"configurable": {"user_id": COLLEAGUE_USER_ID, "workspace_id": WS_ID}},
    )
    assert "enabled" in colleague_res
    assert seen == [(PM_USER_ID, WS_ID)]

    seen.clear()
    owner_res = await analyst_tool.ainvoke(
        {"action": "status"},
        config={"configurable": {"user_id": PM_USER_ID, "workspace_id": WS_ID}},
    )
    assert "enabled" in owner_res
    assert seen == [(PM_USER_ID, WS_ID)]


@pytest.mark.asyncio
async def test_tracking_by_ticker_refuses_colleague_with_owner_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision F: a colleague naming the PM's workspace by ticker gets the
    owner-only reason — not "workspace not found" — on enable, and a readable
    status on status."""
    import src.dalton.tools.director_tools as dt_mod
    from src.dalton.tools.director_tools import (
        TRACKING_OWNER_ONLY_DETAIL,
        build_director_tools,
    )

    holder = _install_real_access_chain(monkeypatch, cos_access="ro")
    assert holder

    # The caller-scoped resolver sees nothing (PM's workspace); the any-owner
    # twin finds it — that is exactly the "exists but not yours" case. The
    # tool resolves both through its director_tools module bindings.
    monkeypatch.setattr(
        dt_mod,
        "resolve_workspace_id_by_ticker",
        create_autospec(
            dt_mod.resolve_workspace_id_by_ticker, return_value=None
        ),
    )
    from src.dalton.autonomy.tracking import (
        resolve_workspace_id_by_ticker_any_owner as _real_any_owner,
    )

    monkeypatch.setattr(
        dt_mod,
        "resolve_workspace_id_by_ticker_any_owner",
        create_autospec(_real_any_owner, return_value=WS_ID),
    )

    cos_enable = {
        t.name: t for t in build_director_tools(role="chief_of_staff")
    }["dalton_coverage_tracking"]
    with pytest.raises(HTTPException) as exc_enable:
        await cos_enable.ainvoke(
            {"action": "enable", "ticker": "META"},
            config={"configurable": {"user_id": COLLEAGUE_USER_ID, "workspace_id": "home"}},
        )
    assert exc_enable.value.status_code == 403
    assert exc_enable.value.detail == TRACKING_OWNER_ONLY_DETAIL

    seen: list[tuple[str, str]] = []
    real_get_status = dt_mod.get_tracking_status

    async def _get_status(user_id: str, workspace_id: str) -> dict[str, Any]:
        seen.append((user_id, workspace_id))
        return {"ok": True, "status": "enabled", "workspace_id": workspace_id}

    monkeypatch.setattr(
        dt_mod,
        "get_tracking_status",
        create_autospec(real_get_status, side_effect=_get_status),
    )
    status_res = await cos_enable.ainvoke(
        {"action": "status", "ticker": "META"},
        config={"configurable": {"user_id": COLLEAGUE_USER_ID, "workspace_id": "home"}},
    )
    assert "enabled" in status_res
    assert seen == [(PM_USER_ID, WS_ID)]


@pytest.mark.asyncio
async def test_system_jobs_owner_only_wording_and_toggle_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision F: dalton_system_jobs enable/disable surfaces the owner-only
    reason at the analyst gates, at the company-key precheck, and around a
    403 raised deep inside toggle_system_job."""
    import src.dalton.autonomy.system_jobs as system_jobs_mod
    import src.dalton.autonomy.tracking as tracking_mod
    import src.dalton.tools.director_tools as dt_mod
    from src.dalton.tools.director_tools import (
        TRACKING_OWNER_ONLY_DETAIL,
        build_director_tools,
    )

    holder = _install_real_access_chain(monkeypatch, cos_access="rw")
    assert holder

    tools = {t.name: t for t in build_director_tools(role="analyst")}
    analyst_jobs = tools["dalton_system_jobs"]
    cos_jobs = {
        t.name: t for t in build_director_tools(role="chief_of_staff")
    }["dalton_system_jobs"]

    # The analyst branch resolves the current workspace's ticker through the
    # tracking module (lazy import at call time).
    monkeypatch.setattr(
        tracking_mod,
        "_resolve_workspace_ticker",
        create_autospec(tracking_mod._resolve_workspace_ticker, return_value="META"),
    )

    # Analyst enable inside the PM's workspace: the analyst gate (company
    # branch) refuses with the owner-only reason.
    with pytest.raises(HTTPException) as exc_analyst:
        await analyst_jobs.ainvoke(
            {"action": "enable", "key": "company:META"},
            config={"configurable": {"user_id": COLLEAGUE_USER_ID, "workspace_id": WS_ID}},
        )
    assert exc_analyst.value.status_code == 403
    assert exc_analyst.value.detail == TRACKING_OWNER_ONLY_DETAIL

    # CoS enable by company key where the workspace belongs to someone else:
    # the any-owner precheck answers the owner-only reason as JSON.
    monkeypatch.setattr(
        dt_mod,
        "resolve_workspace_id_by_ticker",
        create_autospec(
            dt_mod.resolve_workspace_id_by_ticker, return_value=None
        ),
    )
    from src.dalton.autonomy.tracking import (
        resolve_workspace_id_by_ticker_any_owner as _real_any_owner,
    )

    monkeypatch.setattr(
        dt_mod,
        "resolve_workspace_id_by_ticker_any_owner",
        create_autospec(_real_any_owner, return_value=WS_ID),
    )
    precheck_raw = await cos_jobs.ainvoke(
        {"action": "enable", "key": "company:META"},
        config={"configurable": {"user_id": COLLEAGUE_USER_ID, "workspace_id": "home"}},
    )
    precheck = json.loads(precheck_raw)
    assert precheck["ok"] is False
    assert precheck["error"] == "tracking_owner_only"
    assert precheck["message"] == TRACKING_OWNER_ONLY_DETAIL

    # A 403 raised deep inside toggle_system_job (e.g. by the scheduling
    # layer) is converted to the same wording instead of a raw system error.
    async def _toggle_raises(user_id: str, key: str, enabled: bool) -> dict[str, Any]:
        raise HTTPException(
            status_code=403, detail="Workspace does not belong to current user"
        )

    monkeypatch.setattr(
        system_jobs_mod,
        "toggle_system_job",
        create_autospec(system_jobs_mod.toggle_system_job, side_effect=_toggle_raises),
    )
    monkeypatch.setattr(
        dt_mod,
        "resolve_workspace_id_by_ticker_any_owner",
        create_autospec(_real_any_owner, return_value=None),
    )
    converted_raw = await cos_jobs.ainvoke(
        {"action": "disable", "key": "sector:us-semiconductor"},
        config={"configurable": {"user_id": COLLEAGUE_USER_ID, "workspace_id": "home"}},
    )
    converted = json.loads(converted_raw)
    assert converted["ok"] is False
    assert converted["error"] == "tracking_owner_only"
    assert converted["message"] == TRACKING_OWNER_ONLY_DETAIL


@pytest.mark.asyncio
async def test_owner_takeover_with_wrong_params_still_publishes_row_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-3 decision 1: an owner taking a request over without knowing the
    original slug/title still gets the card with the ROW's params, and the
    publish completes on them — nothing is voided by the differing params."""
    from src.dalton.publish import service as publish_svc
    from src.server.database import conversation as conv_db

    row: dict[str, Any] = {
        "publish_request_id": "req-1",
        "workspace_id": WS_ID,
        "user_id": PM_USER_ID,
        "conversation_thread_id": "thread-colleague-1",
        "status": "awaiting_pm",
        "slug": "meta-original",
        "audit": None,
    }
    calls = await _install_publish_state_stubs(monkeypatch, row=row)
    monkeypatch.setattr(
        conv_db,
        "get_thread_by_id",
        create_autospec(
            _REAL_GET_THREAD_BY_ID,
            return_value={
                "conversation_thread_id": "thread-colleague-1",
                "user_id": COLLEAGUE_USER_ID,
            },
        ),
    )
    backend = _memory_backend({"drafts/meta.md": DRAFT})

    captured: list[dict[str, Any]] = []

    def _answer(payload: dict[str, Any]) -> dict[str, Any]:
        captured.append(payload)
        return {"order_decisions": {"req-1": {"type": "approve", "message": "ok"}}}

    res = await publish_svc.run_publish(
        user_id=PM_USER_ID,
        workspace_id=WS_ID,
        thread_id="t-pm",
        tool_call_id="tc-pm",
        draft_path="drafts/meta.md",
        title="错误标题",
        kind="initial_coverage",
        slug="wrong-slug",
        request_id="req-1",
        backend=backend,
        author_model="test-model",
        origin_type="user",
        interrupt_fn=_answer,
    )
    assert res["status"] == "published", res
    assert res.get("ignored_params") == ["title", "slug"]
    args = captured[0]["action_requests"][0]["args"]
    assert args["title"] == "META"
    assert "meta-original" in args["published_path"]
    assert calls["insert_pub"]["title"] == "META"
    assert calls["write_bundle"]["slug"] == "meta-original"


@pytest.mark.asyncio
async def test_initiator_approved_reentry_cannot_flip_updates_thesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-3 decision 1: the initiator re-entering an approved request with
    ``updates_thesis=True`` does not flip it — the row's saved value rules, so
    thesis.official is never touched by a post-approval parameter change."""
    from src.dalton.publish import service as publish_svc
    from src.server.database import conversation as conv_db

    row: dict[str, Any] = {
        "publish_request_id": "req-1",
        "workspace_id": WS_ID,
        "user_id": PM_USER_ID,
        "conversation_thread_id": "thread-colleague-1",
        "status": "approved",
        "updates_thesis": False,
        "thesis_version_no": None,
        "decided_by": PM_USER_ID,
        "audit": None,
    }
    calls = await _install_publish_state_stubs(monkeypatch, row=row)
    monkeypatch.setattr(
        conv_db,
        "get_thread_by_id",
        create_autospec(
            _REAL_GET_THREAD_BY_ID,
            return_value={
                "conversation_thread_id": "thread-colleague-1",
                "user_id": COLLEAGUE_USER_ID,
            },
        ),
    )
    backend = _memory_backend({"drafts/meta.md": DRAFT})

    applied_updates: list[dict[str, Any]] = []

    async def _apply_update(**kwargs: Any) -> dict[str, Any]:
        applied_updates.append(kwargs)
        return {"ok": True, "version_no": 9}

    monkeypatch.setattr(
        publish_svc,
        "apply_update",
        create_autospec(publish_svc.apply_update, side_effect=_apply_update),
    )

    res = await publish_svc.run_publish(
        user_id=COLLEAGUE_USER_ID,
        workspace_id=WS_ID,
        thread_id="thread-colleague-1",
        tool_call_id="tc-colleague",
        draft_path="drafts/meta.md",
        title="META",
        kind="initial_coverage",
        slug="meta",
        updates_thesis=True,
        request_id="req-1",
        backend=backend,
        author_model="test-model",
    )
    assert res["status"] == "published", res
    assert res.get("ignored_params") == ["updates_thesis"]
    # The row's updates_thesis=False rules: thesis.official is never written.
    assert applied_updates == []
    assert calls["insert_pub"].get("thesis_version_no") is None


@pytest.mark.asyncio
async def test_awaiting_pm_takeover_after_draft_change_returns_error_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-3 decision 1: re-entering an awaiting_pm request whose draft has
    changed returns an error BEFORE any card or review, and leaves the request
    status untouched."""
    from src.dalton.publish import service as publish_svc
    from src.server.database import conversation as conv_db

    row: dict[str, Any] = {
        "publish_request_id": "req-1",
        "workspace_id": WS_ID,
        "user_id": PM_USER_ID,
        "conversation_thread_id": "thread-colleague-1",
        "status": "awaiting_pm",
        "bundle_sha256": "0" * 64,
        "audit": None,
    }
    calls = await _install_publish_state_stubs(monkeypatch, row=row)
    monkeypatch.setattr(
        conv_db,
        "get_thread_by_id",
        create_autospec(
            _REAL_GET_THREAD_BY_ID,
            return_value={
                "conversation_thread_id": "thread-colleague-1",
                "user_id": COLLEAGUE_USER_ID,
            },
        ),
    )
    backend = _memory_backend({"drafts/meta.md": DRAFT + "\n改动后的草稿。"})

    def _must_not_interrupt(payload: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("a changed draft must never reach the approval card")

    res = await publish_svc.run_publish(
        user_id=PM_USER_ID,
        workspace_id=WS_ID,
        thread_id="t-pm",
        tool_call_id="tc-pm",
        draft_path="drafts/meta.md",
        title="META",
        kind="initial_coverage",
        slug="meta",
        request_id="req-1",
        backend=backend,
        author_model="test-model",
        origin_type="user",
        interrupt_fn=_must_not_interrupt,
    )
    assert res["ok"] is False
    assert res["status"] == "awaiting_pm", res
    assert "正文已改动" in res["message"]
    assert "不带 request_id" in res["message"]
    # The request was left exactly as it was.
    assert calls["transitions"] == []


@pytest.mark.asyncio
async def test_published_fast_path_rejects_changed_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-3 decision 1: the published fast path re-checks the content too —
    a draft changed after publication is refused with a pointer to a fresh
    request, not reported as a no-op success."""
    from src.dalton.publish import service as publish_svc

    row: dict[str, Any] = {
        "publish_request_id": "req-1",
        "workspace_id": WS_ID,
        "user_id": PM_USER_ID,
        "status": "published",
        "publication_id": "pub-1",
        "bundle_sha256": "0" * 64,
        "audit": None,
    }
    await _install_publish_state_stubs(monkeypatch, row=row)
    backend = _memory_backend({"drafts/meta.md": DRAFT})

    res = await publish_svc.run_publish(
        user_id=PM_USER_ID,
        workspace_id=WS_ID,
        thread_id="t-pm",
        tool_call_id="tc-pm",
        draft_path="drafts/meta.md",
        title="META",
        kind="initial_coverage",
        slug="meta",
        request_id="req-1",
        backend=backend,
        author_model="test-model",
    )
    assert res["ok"] is False
    assert res["status"] == "published", res
    assert "已改动" in res["message"]
    assert "不带 request_id" in res["message"]


@pytest.mark.asyncio
async def test_approved_resume_rejects_thesis_version_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-3 decision 1: an approval that pinned thesis v3 is not executable
    when the workspace's thesis has moved to v4 — superseded, back to approval."""
    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.publish import service as publish_svc

    row: dict[str, Any] = {
        "publish_request_id": "req-1",
        "workspace_id": WS_ID,
        "user_id": PM_USER_ID,
        "conversation_thread_id": "t-pm",
        "status": "approved",
        "updates_thesis": True,
        "thesis_version_no": 3,
        "decided_by": PM_USER_ID,
        "audit": None,
    }
    calls = await _install_publish_state_stubs(monkeypatch, row=row)
    monkeypatch.setattr(
        db_knowledge,
        "list_all_latest_for_workspace",
        create_autospec(
            _REAL_LIST_LATEST,
            return_value={"thesis": {"content": {}, "version_no": 4}},
        ),
    )
    backend = _memory_backend({"drafts/meta.md": DRAFT})

    res = await publish_svc.run_publish(
        user_id=PM_USER_ID,
        workspace_id=WS_ID,
        thread_id="t-pm",
        tool_call_id="tc-pm",
        draft_path="drafts/meta.md",
        title="META",
        kind="initial_coverage",
        slug="meta",
        request_id="req-1",
        backend=backend,
        author_model="test-model",
    )
    assert res["status"] == "superseded", res
    assert "thesis" in res["message"]
    assert ("req-1", ("approved",), "superseded") in calls["transitions"]
    assert calls["insert_pub"] == {}


@pytest.mark.asyncio
async def test_another_pm_cannot_decide_owner_filed_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-3 decision 5 (decision B's reject branch): another PM-listed
    colleague cannot approve or reject a request the OWNER filed — re-entry by
    request_id is 403 (initiator is the owner, so only the owner may re-enter),
    and their interactive attempt never reaches a card."""
    from src.dalton.publish import service as publish_svc
    from src.server.database import conversation as conv_db

    another_pm = ANOTHER_PM_USER_ID
    row: dict[str, Any] = {
        "publish_request_id": "req-1",
        "workspace_id": WS_ID,
        "user_id": PM_USER_ID,
        # The owner filed this one themselves: the row thread is the owner's.
        "conversation_thread_id": "thread-owner-1",
        "status": "awaiting_pm",
        "audit": None,
    }
    calls = await _install_publish_state_stubs(monkeypatch, row=row)
    monkeypatch.setenv("DALTON_PM_USER_IDS", f"{PM_USER_ID},{another_pm}")
    monkeypatch.setattr(
        conv_db,
        "get_thread_by_id",
        create_autospec(
            _REAL_GET_THREAD_BY_ID,
            return_value={
                "conversation_thread_id": "thread-owner-1",
                "user_id": PM_USER_ID,
            },
        ),
    )
    backend = _memory_backend({"drafts/meta.md": DRAFT})

    for decision in ("approve", "reject"):
        with pytest.raises(HTTPException) as exc_decide:
            await publish_svc.run_publish(
                user_id=another_pm,
                workspace_id=WS_ID,
                thread_id="t-pm2",
                tool_call_id=f"tc-pm2-{decision}",
                draft_path="drafts/meta.md",
                title="META",
                kind="initial_coverage",
                slug="meta",
                request_id="req-1",
                backend=backend,
                author_model="test-model",
                origin_type="user",
                interrupt_fn=lambda _p, d=decision: {
                    "order_decisions": {"req-1": {"type": d, "message": ""}}
                },
            )
        assert exc_decide.value.status_code == 403
    assert all(t[2] not in ("approved", "rejected") for t in calls["transitions"])


def _fake_ticker_candidate_conn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Feed ``resolve_workspace_id_by_ticker_any_owner`` one PM-owned candidate
    row (name match) and an empty second (company-doc) result, via a spec'd
    connection double; the per-candidate READ check then runs for real."""
    from contextlib import asynccontextmanager

    from psycopg import AsyncConnection, AsyncCursor

    import src.dalton.autonomy.tracking as tracking_mod

    cur = create_autospec(AsyncCursor, instance=True)
    calls = {"n": 0}

    async def _fetchall() -> list[dict[str, Any]]:
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"workspace_id": WS_ID}]
        return []

    cur.fetchall.side_effect = _fetchall
    conn = create_autospec(AsyncConnection, instance=True)

    @asynccontextmanager
    async def _cursor(*args: Any, **kwargs: Any):
        yield cur

    conn.cursor.side_effect = _cursor

    @asynccontextmanager
    async def _fake_get_db(*args: Any, **kwargs: Any):
        yield conn

    monkeypatch.setattr(
        tracking_mod,
        "get_db_connection",
        create_autospec(
            _REAL_GET_DB_CONN, side_effect=lambda *a, **k: _fake_get_db()
        ),
    )


@pytest.mark.asyncio
async def test_any_owner_ticker_resolution_requires_readable_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-3 decision 2: the any-owner resolver keeps only workspaces the
    CALLER can read — external members, hidden sharing, and unreadable
    workspaces all resolve to None, so the tool answers "not found", never the
    owner-only wording; an ro colleague still resolves it."""
    from src.dalton.tools.director_tools import (
        TRACKING_OWNER_ONLY_DETAIL,
        build_director_tools,
    )

    cos_enable = {
        t.name: t for t in build_director_tools(role="chief_of_staff")
    }["dalton_coverage_tracking"]

    def _invoke_ticker_enable(caller: str) -> Any:
        return cos_enable.ainvoke(
            {"action": "enable", "ticker": "META"},
            config={"configurable": {"user_id": caller, "workspace_id": "home"}},
        )

    # External member: candidate exists but the caller is off-roster.
    _install_real_access_chain(monkeypatch, cos_access="rw")
    _fake_ticker_candidate_conn(monkeypatch)
    import src.dalton.tools.director_tools as dt_mod

    monkeypatch.setattr(
        dt_mod,
        "resolve_workspace_id_by_ticker",
        create_autospec(dt_mod.resolve_workspace_id_by_ticker, return_value=None),
    )
    outsider_raw = await _invoke_ticker_enable(OUTSIDER_USER_ID)
    outsider = json.loads(outsider_raw)
    assert outsider["ok"] is False
    assert outsider["error"] == "workspace_not_found"

    # Readable-in-principle colleague with the workspace hidden on cos.
    holder = _install_real_access_chain(monkeypatch, cos_access="hidden")
    _fake_ticker_candidate_conn(monkeypatch)
    hidden_raw = await _invoke_ticker_enable(COLLEAGUE_USER_ID)
    hidden = json.loads(hidden_raw)
    assert hidden["ok"] is False
    assert hidden["error"] == "workspace_not_found"

    # ro colleague: the candidate IS readable, so it resolves — and the
    # owner-only wording is then the (correct) refusal for enable.
    holder["cos_access"] = "ro"
    _fake_ticker_candidate_conn(monkeypatch)
    with pytest.raises(HTTPException) as exc_ro:
        await _invoke_ticker_enable(COLLEAGUE_USER_ID)
    assert exc_ro.value.status_code == 403
    assert exc_ro.value.detail == TRACKING_OWNER_ONLY_DETAIL


@pytest.mark.asyncio
async def test_same_tool_call_retry_on_changed_draft_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-4 decision 1: a retry on the SAME (thread, tool_call) that fetched
    an existing row gets the same unified gate — a changed draft voids
    approved/publishing requests (superseded) instead of publishing new
    content; the caller passed NO request_id."""
    from src.dalton.publish import service as publish_svc

    row: dict[str, Any] = {
        "publish_request_id": "req-1",
        "workspace_id": WS_ID,
        "user_id": PM_USER_ID,
        "conversation_thread_id": "t-pm",
        "status": "publishing",
        "bundle_sha256": "0" * 64,
        "title": "META",
        "kind": "initial_coverage",
        "slug": "meta",
        "draft_path": "drafts/meta.md",
        "audit": None,
    }
    calls = await _install_publish_state_stubs(
        monkeypatch, row=row, created=False
    )
    backend = _memory_backend({"drafts/meta.md": DRAFT + "\n改动后的正文。"})

    res_publishing = await publish_svc.run_publish(
        user_id=PM_USER_ID,
        workspace_id=WS_ID,
        thread_id="t-pm",
        tool_call_id="tc-pm",
        draft_path="drafts/meta.md",
        title="META",
        kind="initial_coverage",
        slug="meta",
        backend=backend,
        author_model="test-model",
    )
    assert res_publishing["status"] == "superseded", res_publishing
    assert ("req-1", ("publishing",), "superseded") in calls["transitions"]
    assert calls["insert_pub"] == {}

    row["status"] = "approved"
    row["decided_by"] = PM_USER_ID
    res_approved = await publish_svc.run_publish(
        user_id=PM_USER_ID,
        workspace_id=WS_ID,
        thread_id="t-pm",
        tool_call_id="tc-pm",
        draft_path="drafts/meta.md",
        title="META",
        kind="initial_coverage",
        slug="meta",
        backend=backend,
        author_model="test-model",
    )
    assert res_approved["status"] == "superseded", res_approved
    assert ("req-1", ("approved",), "superseded") in calls["transitions"]
    assert calls["insert_pub"] == {}


@pytest.mark.asyncio
async def test_updates_thesis_without_pinned_version_drifts_once_thesis_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-4 decision 2: ``updates_thesis=True`` with an EMPTY pinned
    thesis_version_no (no thesis at filing time) drifts as soon as the
    workspace has one — caught before the card, without touching the row."""
    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.publish import service as publish_svc

    row: dict[str, Any] = {
        "publish_request_id": "req-1",
        "workspace_id": WS_ID,
        "user_id": PM_USER_ID,
        "conversation_thread_id": "thread-colleague-1",
        "status": "awaiting_pm",
        "updates_thesis": True,
        "thesis_version_no": None,
        "audit": None,
    }
    calls = await _install_publish_state_stubs(monkeypatch, row=row)
    monkeypatch.setattr(
        db_knowledge,
        "list_all_latest_for_workspace",
        create_autospec(
            _REAL_LIST_LATEST,
            return_value={"thesis": {"content": {}, "version_no": 1}},
        ),
    )
    backend = _memory_backend({"drafts/meta.md": DRAFT})

    def _must_not_interrupt(payload: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("a drifted thesis must never reach the approval card")

    res = await publish_svc.run_publish(
        user_id=PM_USER_ID,
        workspace_id=WS_ID,
        thread_id="t-pm",
        tool_call_id="tc-pm",
        draft_path="drafts/meta.md",
        title="META",
        kind="initial_coverage",
        slug="meta",
        request_id="req-1",
        backend=backend,
        author_model="test-model",
        origin_type="user",
        interrupt_fn=_must_not_interrupt,
    )
    assert res["ok"] is False
    assert res["status"] == "awaiting_pm", res
    assert "尚无 thesis" in res["message"]
    assert "重新调用 publish_report" in res["message"]
    assert calls["transitions"] == []


@pytest.mark.asyncio
async def test_missing_initiator_thread_shows_unknown_and_owner_still_approves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-4 decisions 4/5: a cross-thread request_id re-entry whose
    original thread is gone names the initiator as '发起人未知（原线程已删除）'
    on the card, and the owner approves normally."""
    from src.dalton.publish import service as publish_svc
    from src.dalton.publish.service import PUBLISH_INITIATOR_UNKNOWN
    from src.server.database import conversation as conv_db

    row: dict[str, Any] = {
        "publish_request_id": "req-1",
        "workspace_id": WS_ID,
        "user_id": PM_USER_ID,
        "conversation_thread_id": "thread-gone",
        "status": "awaiting_pm",
        "audit": None,
    }
    calls = await _install_publish_state_stubs(monkeypatch, row=row)
    monkeypatch.setattr(
        conv_db,
        "get_thread_by_id",
        create_autospec(_REAL_GET_THREAD_BY_ID, return_value=None),
    )
    backend = _memory_backend({"drafts/meta.md": DRAFT})

    captured: list[dict[str, Any]] = []

    def _answer(payload: dict[str, Any]) -> dict[str, Any]:
        captured.append(payload)
        return {"order_decisions": {"req-1": {"type": "approve", "message": "ok"}}}

    res = await publish_svc.run_publish(
        user_id=PM_USER_ID,
        workspace_id=WS_ID,
        thread_id="t-pm",
        tool_call_id="tc-pm",
        draft_path="drafts/meta.md",
        title="META",
        kind="initial_coverage",
        slug="meta",
        request_id="req-1",
        backend=backend,
        author_model="test-model",
        origin_type="user",
        interrupt_fn=_answer,
    )
    assert res["status"] == "published", res
    args = captured[0]["action_requests"][0]["args"]
    assert args["initiated_by"] == PUBLISH_INITIATOR_UNKNOWN
    assert args["发起人"] == PUBLISH_INITIATOR_UNKNOWN
    assert calls["insert_pub"]["user_id"] == PM_USER_ID


WS_RW_ID = "66666666-6666-4666-8666-666666666666"
WS_HIDDEN_ID = "77777777-7777-4777-8777-777777777777"


def _two_same_ticker_workspaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """One owner (PM), two workspaces both named META: one readable (cos ro),
    one hidden — for the system-jobs resolver's read filtering."""
    async def _get_ws(ws_id: str, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        rows = {
            WS_ID: _pm_workspace(),
            WS_RW_ID: {
                "workspace_id": WS_RW_ID,
                "user_id": PM_USER_ID,
                "name": "META",
                "status": "running",
                "is_home": False,
            },
            WS_HIDDEN_ID: {
                "workspace_id": WS_HIDDEN_ID,
                "user_id": PM_USER_ID,
                "name": "META",
                "status": "running",
                "is_home": False,
            },
        }
        return rows.get(str(ws_id))

    monkeypatch.setattr(
        ws_db, "get_workspace", create_autospec(_REAL_GET_WORKSPACE, side_effect=_get_ws)
    )

    async def _sharing(workspace_id: str, *, conn: Any = None) -> dict[str, Any]:
        level = "hidden" if str(workspace_id) == WS_HIDDEN_ID else "ro"
        return {
            "workspace_id": workspace_id,
            "cos_access": level,
            "web_access": level,
            "updated_by": None,
            "updated_at": None,
        }

    monkeypatch.setattr(
        dalton_sharing,
        "get_workspace_sharing",
        create_autospec(_REAL_GET_SHARING, side_effect=_sharing),
    )


@pytest.mark.asyncio
async def test_system_jobs_status_reported_for_owner_across_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-4 decision 5: system-jobs list/status report from the workspace
    owner's rows — analyst company branch, analyst sector branch, and the CoS
    by-key branch (which passes the VERIFIED workspace UUID, not the ticker)."""
    import src.dalton.autonomy.system_jobs as system_jobs_mod
    import src.dalton.autonomy.tracking as tracking_mod
    import src.dalton.tools.director_tools as dt_mod
    from src.dalton.tools.director_tools import build_director_tools

    _install_real_access_chain(monkeypatch, cos_access="ro")
    _two_same_ticker_workspaces(monkeypatch)

    tools = {t.name: t for t in build_director_tools(role="analyst")}
    analyst_jobs = tools["dalton_system_jobs"]
    cos_jobs = {
        t.name: t for t in build_director_tools(role="chief_of_staff")
    }["dalton_system_jobs"]

    seen: list[tuple[str, str]] = []
    real_status = system_jobs_mod.get_system_job_status

    async def _status(user_id: str, key: str) -> dict[str, Any]:
        seen.append((user_id, str(key)))
        return {"ok": True, "job": {"key": str(key), "on": "on"}}

    monkeypatch.setattr(
        system_jobs_mod,
        "get_system_job_status",
        create_autospec(real_status, side_effect=_status),
    )

    # Analyst company branch (current workspace = the PM's).
    company_raw = await analyst_jobs.ainvoke(
        {"action": "status", "key": "company:META"},
        config={"configurable": {"user_id": COLLEAGUE_USER_ID, "workspace_id": WS_ID}},
    )
    assert json.loads(company_raw)["ok"] is True
    assert seen[-1] == (PM_USER_ID, WS_ID)

    # Analyst sector branch (coverage units on; the workspace is a sector unit).
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "1")
    monkeypatch.setattr(
        tracking_mod,
        "_resolve_workspace_sector",
        create_autospec(
            tracking_mod._resolve_workspace_sector,
            return_value={"unit": {"slug": "us-semiconductor", "unit_id": "u-1"}},
        ),
    )
    sector_raw = await analyst_jobs.ainvoke(
        {"action": "status", "key": "sector:us-semiconductor"},
        config={"configurable": {"user_id": COLLEAGUE_USER_ID, "workspace_id": WS_ID}},
    )
    assert json.loads(sector_raw)["ok"] is True
    assert seen[-1] == (PM_USER_ID, "sector:us-semiconductor")

    # CoS by-key branch: ticker resolves to the READABLE same-name workspace
    # and the status query runs under the owner with the verified UUID key.
    monkeypatch.setattr(
        dt_mod,
        "resolve_workspace_id_by_ticker",
        create_autospec(dt_mod.resolve_workspace_id_by_ticker, return_value=None),
    )
    monkeypatch.setattr(
        dt_mod,
        "resolve_workspace_id_by_ticker_any_owner",
        create_autospec(
            dt_mod.resolve_workspace_id_by_ticker_any_owner, return_value=WS_RW_ID
        ),
    )
    cos_raw = await cos_jobs.ainvoke(
        {"action": "status", "key": "company:META"},
        config={"configurable": {"user_id": COLLEAGUE_USER_ID, "workspace_id": "home"}},
    )
    assert json.loads(cos_raw)["ok"] is True
    assert seen[-1] == (PM_USER_ID, f"company:{WS_RW_ID}")


@pytest.mark.asyncio
async def test_system_jobs_owner_ws_resolution_never_leaks_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-5 item 1: an unreadable (hidden) workspace — by ticker, by UUID,
    or a sector unit — answers EXACTLY the not-found shape a nonexistent key
    produces: same fields, no ticker, no job state, and never an exception.
    The real ``get_system_job_status`` runs for the nonexistent-key control;
    only its database dependencies are doubled."""
    import src.dalton.autonomy.system_jobs as system_jobs_mod
    import src.dalton.tools.director_tools as dt_mod
    from src.dalton.tools.director_tools import build_director_tools

    _install_real_access_chain(monkeypatch, cos_access="ro")
    _two_same_ticker_workspaces(monkeypatch)

    cos_jobs = {
        t.name: t for t in build_director_tools(role="chief_of_staff")
    }["dalton_system_jobs"]

    # Database doubles for the REAL status lookup (nonexistent-key control):
    # no automations rows, no company entry, no discovered sector units.
    monkeypatch.setattr(
        system_jobs_mod,
        "list_all_user_automations",
        create_autospec(system_jobs_mod.list_all_user_automations, return_value=[]),
    )
    monkeypatch.setattr(
        system_jobs_mod,
        "_build_company_job_entry",
        create_autospec(system_jobs_mod._build_company_job_entry, return_value=None),
    )
    monkeypatch.setattr(
        system_jobs_mod,
        "_discover_sector_units",
        create_autospec(system_jobs_mod._discover_sector_units, return_value=[]),
    )

    MISSING_WS_ID = "88888888-8888-4888-8888-888888888888"

    # Control: a UUID no workspace has -> the real layer's not-found answer.
    missing_raw = await cos_jobs.ainvoke(
        {"action": "status", "key": f"company:{MISSING_WS_ID}"},
        config={"configurable": {"user_id": COLLEAGUE_USER_ID, "workspace_id": "home"}},
    )
    missing = json.loads(missing_raw)
    assert missing == {
        "ok": False,
        "error": "system_job_error",
        "message": f"未找到公司自动跟踪任务: company:{MISSING_WS_ID}",
    }

    # The HIDDEN workspace by UUID: byte-for-byte the same not-found shape
    # (same fields, no ticker, no job) — not the caller's fallback view.
    hidden_raw = await cos_jobs.ainvoke(
        {"action": "status", "key": f"company:{WS_HIDDEN_ID}"},
        config={"configurable": {"user_id": COLLEAGUE_USER_ID, "workspace_id": "home"}},
    )
    hidden = json.loads(hidden_raw)
    assert hidden == {
        "ok": False,
        "error": "system_job_error",
        "message": f"未找到公司自动跟踪任务: company:{WS_HIDDEN_ID}",
    }
    assert "job" not in hidden
    assert "META" not in hidden_raw

    # Ticker whose only cross-owner candidate is the hidden workspace.
    monkeypatch.setattr(
        dt_mod,
        "resolve_workspace_id_by_ticker",
        create_autospec(dt_mod.resolve_workspace_id_by_ticker, return_value=None),
    )
    monkeypatch.setattr(
        dt_mod,
        "resolve_workspace_id_by_ticker_any_owner",
        create_autospec(
            dt_mod.resolve_workspace_id_by_ticker_any_owner, return_value=WS_HIDDEN_ID
        ),
    )
    hidden_ticker = json.loads(
        await cos_jobs.ainvoke(
            {"action": "status", "key": "company:META"},
            config={"configurable": {"user_id": COLLEAGUE_USER_ID, "workspace_id": "home"}},
        )
    )
    assert hidden_ticker == {
        "ok": False,
        "error": "system_job_error",
        "message": "未找到公司自动跟踪任务: company:META",
    }

    # A sector unit bound to the hidden workspace: same not-found shape.
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "1")
    monkeypatch.setattr(
        dt_mod,
        "get_registered_units",
        create_autospec(
            dt_mod.get_registered_units,
            return_value=[
                {
                    "kind": "sector",
                    "slug": "us-semiconductor",
                    "unit_id": "u-1",
                    "workspace_id": WS_HIDDEN_ID,
                }
            ],
        ),
    )
    sector = json.loads(
        await cos_jobs.ainvoke(
            {"action": "status", "key": "sector:us-semiconductor"},
            config={"configurable": {"user_id": COLLEAGUE_USER_ID, "workspace_id": "home"}},
        )
    )
    assert sector == {
        "ok": False,
        "error": "system_job_error",
        "message": "未找到行业自动跟踪任务: sector:us-semiconductor",
    }


@pytest.mark.asyncio
async def test_sector_resolution_skips_unreadable_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-5 item 3: same-name sector candidates are scanned past unreadable
    ones — a hidden candidate listed FIRST does not resolve; the readable one
    behind it does."""
    import src.dalton.tools.director_tools as dt_mod
    from src.dalton.tools.director_tools import _resolve_system_job_owner_ws

    _install_real_access_chain(monkeypatch, cos_access="ro")
    _two_same_ticker_workspaces(monkeypatch)
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "1")
    monkeypatch.setattr(
        dt_mod,
        "get_registered_units",
        create_autospec(
            dt_mod.get_registered_units,
            return_value=[
                {
                    "kind": "sector",
                    "slug": "us-semiconductor",
                    "unit_id": "u-hidden",
                    "workspace_id": WS_HIDDEN_ID,
                },
                {
                    "kind": "sector",
                    "slug": "us-semiconductor",
                    "unit_id": "u-rw",
                    "workspace_id": WS_RW_ID,
                },
            ],
        ),
    )

    resolved = await _resolve_system_job_owner_ws(
        "sector", "us-semiconductor", COLLEAGUE_USER_ID
    )
    assert resolved == (WS_RW_ID, PM_USER_ID)


@pytest.mark.asyncio
async def test_explicit_reentry_keeps_row_version_and_refuses_thesis_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-5 item 2: an EXPLICIT request_id re-entry never rewrites the row's
    params or pinned thesis version. With the row pinned at thesis v3 and the
    workspace now at v4, the re-entry returns the drift error and the row
    stays at v3 (no transition columns touched); a no-drift explicit re-entry
    re-audits with the status alone — no param/version write-back."""
    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.publish import service as publish_svc

    row: dict[str, Any] = {
        "publish_request_id": "req-1",
        "workspace_id": WS_ID,
        "user_id": PM_USER_ID,
        "conversation_thread_id": "thread-colleague-1",
        "status": "audit_failed",
        "updates_thesis": True,
        "thesis_version_no": 3,
        "audit": {},
    }
    calls = await _install_publish_state_stubs(monkeypatch, row=row)
    monkeypatch.setattr(
        db_knowledge,
        "list_all_latest_for_workspace",
        create_autospec(
            _REAL_LIST_LATEST,
            return_value={"thesis": {"content": {}, "version_no": 4}},
        ),
    )
    backend = _memory_backend({"drafts/meta.md": DRAFT})

    res = await publish_svc.run_publish(
        user_id=PM_USER_ID,
        workspace_id=WS_ID,
        thread_id="t-pm",
        tool_call_id="tc-pm",
        draft_path="drafts/meta.md",
        title="META",
        kind="initial_coverage",
        slug="meta",
        request_id="req-1",
        backend=backend,
        author_model="test-model",
    )
    assert res["ok"] is False
    assert res["status"] == "audit_failed", res
    assert "thesis 版本与送审时不一致" in res["message"]
    assert "重新调用 publish_report" in res["message"]
    # The row was left untouched: no transitions at all.
    assert calls["transitions"] == []
    assert row["thesis_version_no"] == 3

    # No drift (workspace thesis back at v3): an explicit re-entry re-audits
    # moving ONLY the status — the param/version write-back belongs to the
    # same-tool-call retry, not to request_id re-entry.
    monkeypatch.setattr(
        db_knowledge,
        "list_all_latest_for_workspace",
        create_autospec(
            _REAL_LIST_LATEST,
            return_value={
                "thesis": {
                    "content": {
                        "house_view": {},
                        "aggregate_status": "open",
                        "pillars": [],
                    },
                    "version_no": 3,
                }
            },
        ),
    )
    monkeypatch.setattr(
        publish_svc,
        "apply_update",
        create_autospec(
            publish_svc.apply_update,
            return_value={"ok": True, "version_no": 4, "message": "ok"},
        ),
    )
    res_redo = await publish_svc.run_publish(
        user_id=PM_USER_ID,
        workspace_id=WS_ID,
        thread_id="t-pm",
        tool_call_id="tc-pm",
        draft_path="drafts/meta.md",
        title="META",
        kind="initial_coverage",
        slug="meta",
        request_id="req-1",
        backend=backend,
        author_model="test-model",
        origin_type="user",
        interrupt_fn=lambda _p: {
            "order_decisions": {"req-1": {"type": "approve", "message": "ok"}}
        },
    )
    # The stubbed audit dict has no inputs hash, so the reuse gate re-audits;
    # the run proceeds on the row's params to a full approval.
    assert res_redo["status"] == "published", res_redo
    assert ("req-1", ("audit_failed",), "auditing") in calls["transitions"]
    reaudit_cols = calls["transition_cols"][
        calls["transitions"].index(("req-1", ("audit_failed",), "auditing"))
    ]
    assert "title" not in reaudit_cols
    assert "thesis_version_no" not in reaudit_cols
    assert row["thesis_version_no"] == 3


@pytest.mark.asyncio
async def test_blank_request_id_is_treated_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool may pass "" for request_id; the state machine mixes truthiness
    and `is None` checks, so the wrapper normalizes blank to None once."""
    from src.dalton.publish import service as publish_service

    machine = create_autospec(
        publish_service._run_publish_state_machine, return_value={"ok": True, "message": "m"}
    )
    monkeypatch.setattr(publish_service, "_run_publish_state_machine", machine)
    for blank in ("", "   "):
        await publish_service.run_publish(
            user_id="u", workspace_id="ws", thread_id="t", tool_call_id="c",
            draft_path="drafts/x.md", title="t", kind="call", slug="s",
            request_id=blank, backend=None,
        )
        assert machine.await_args.kwargs["request_id"] is None
    await publish_service.run_publish(
        user_id="u", workspace_id="ws", thread_id="t", tool_call_id="c",
        draft_path="drafts/x.md", title="t", kind="call", slug="s",
        request_id=" req-1 ", backend=None,
    )
    assert machine.await_args.kwargs["request_id"] == "req-1"
