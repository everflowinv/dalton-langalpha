"""Notify group push via the OpenClaw channel, per-task routes, labels, discovery (2026-11, round 3).

Covers the group-push design:
- bridge: both feishu target shapes ride the OpenClaw send-card script (only
  the ``--receive-id-type`` namespace differs); chat-only text rides the
  OpenClaw ``message`` tool with ``chat:oc_…``; a chat_id never reaches a log
  line raw; ``list_bot_chats`` paginates and exposes only chat_id + name;
- preference layer: personal target lists accept personal DMs only; routes may
  name personal or group targets, groups are admin-only and must be discovered;
  cleanup after deleting a personal target keeps group entries;
- dispatch: without a route a task reaches personal targets only; a route's
  personal side intersects the configured list and its group side rides along
  only for an admin owner — first settlement and retry share the rule;
- endpoints: group discovery is PM-only and cached; the test push lets an
  admin test a discovered group and everyone else only their own targets.

All Feishu HTTP is faked at the ``urllib`` opener layer (the R7 pattern), so the
real ``FeishuClient._request`` runs end to end and no real Feishu call is made.
"""

from __future__ import annotations

import email.message
import io
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
import subprocess
import sys
from typing import Any
from unittest.mock import create_autospec

import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402
import src.dalton.invites as invites_mod  # noqa: E402
import src.dalton.llm.model_prefs as model_prefs_mod  # noqa: E402
from src.dalton.llm.model_prefs import (  # noqa: E402
    DALTON_NOTIFY_ROUTES_KEY,
    DALTON_NOTIFY_TARGET_LABELS_KEY,
    DALTON_NOTIFY_TARGETS_KEY,
    is_dalton_chat_target,
    is_valid_dalton_personal_target,
    normalize_dalton_notify_label,
    resolve_dalton_notify_target_labels,
    resolve_dalton_notify_routes,
    validate_dalton_model_preferences,
    validate_dalton_notify_routes_for_user,
)
import src.dalton.notify.dispatch as dispatch_mod  # noqa: E402
import src.dalton.notify.groups as groups_mod  # noqa: E402
from src.dalton.database.verdicts import get_verdict_for_execution  # noqa: E402
from src.dalton.notify.dispatch import (  # noqa: E402
    claim_target_delivery,
    evaluate_user_notify_eligibility,
    mark_target_delivery,
    on_automation_settled,
    post_bridge_notify,
    resolve_dalton_notify_enabled,
    resolve_dalton_notify_overrides,
    resolve_dalton_notify_targets,
    resolve_notify_route_targets,
    retry_failed_notify_deliveries,
)
from src.dalton.settings import get_dalton_frontend_url  # noqa: E402
from src.llms.preferences import TuningError  # noqa: E402


# ---------------------------------------------------------------------------
# preference layer
# ---------------------------------------------------------------------------


def test_personal_target_list_rejects_chat_targets() -> None:
    assert is_valid_dalton_personal_target("feishu:user:ou_me_1") is True
    assert is_valid_dalton_personal_target("discord:user:987654") is True
    assert is_valid_dalton_personal_target("feishu:chat:oc_group_1") is False
    assert is_dalton_chat_target("feishu:chat:oc_group_1") is True
    assert is_dalton_chat_target("feishu:user:ou_me_1") is False

    bag = {
        DALTON_NOTIFY_TARGETS_KEY: ["feishu:user:ou_me_1", "discord:user:987654"]
    }
    validate_dalton_model_preferences(bag)
    assert bag[DALTON_NOTIFY_TARGETS_KEY] == ["feishu:user:ou_me_1", "discord:user:987654"]

    with pytest.raises(TuningError):
        validate_dalton_model_preferences(
            {DALTON_NOTIFY_TARGETS_KEY: ["feishu:chat:oc_group_1"]}
        )


def test_label_normalization_strips_controls_and_clamps_to_40() -> None:
    assert normalize_dalton_notify_label("投研群") == "投研群"
    assert normalize_dalton_notify_label("我\u200b本人\x07") == "我本人"
    assert len(normalize_dalton_notify_label("长" * 80)) == 40
    assert normalize_dalton_notify_label("  \x1b \t ") == ""

    bag = {
        DALTON_NOTIFY_TARGETS_KEY: ["feishu:user:ou_me_1"],
        DALTON_NOTIFY_TARGET_LABELS_KEY: {
            "feishu:user:ou_me_1": " 我本人\x08 ",
            "discord:user:987654": None,
        },
    }
    validate_dalton_model_preferences(bag)
    assert bag[DALTON_NOTIFY_TARGET_LABELS_KEY] == {
        "feishu:user:ou_me_1": "我本人",
        "discord:user:987654": None,
    }
    with pytest.raises(TuningError):
        validate_dalton_model_preferences(
            {
                DALTON_NOTIFY_TARGETS_KEY: ["feishu:user:ou_me_1"],
                DALTON_NOTIFY_TARGET_LABELS_KEY: {"feishu:user:ou_me_1": 42},
            }
        )


def test_route_validation_accepts_personal_and_chat_shapes() -> None:
    # Both target shapes are legal inside a route; whether a chat entry may be
    # written is the user-scoped check, not this format check.
    bag = {
        DALTON_NOTIFY_TARGETS_KEY: ["feishu:user:ou_me_1"],
        DALTON_NOTIFY_ROUTES_KEY: {
            "dalton-googl-earn-2026q1-t0": ["feishu:user:ou_me_1", "feishu:chat:oc_group_1"],
            "category:price_move": None,
        },
    }
    validate_dalton_model_preferences(bag)
    assert bag[DALTON_NOTIFY_ROUTES_KEY] == {
        "dalton-googl-earn-2026q1-t0": ["feishu:user:ou_me_1", "feishu:chat:oc_group_1"],
        "category:price_move": None,
    }

    # A personal target outside the configured list is a 400; an unknown-format
    # entry too. A chat target is none of this checker's business.
    with pytest.raises(TuningError):
        validate_dalton_model_preferences(
            {
                DALTON_NOTIFY_TARGETS_KEY: ["feishu:user:ou_me_1"],
                DALTON_NOTIFY_ROUTES_KEY: {"any-task": ["feishu:user:ou_never_added"]},
            }
        )
    with pytest.raises(TuningError):
        validate_dalton_model_preferences(
            {
                DALTON_NOTIFY_TARGETS_KEY: ["feishu:user:ou_me_1"],
                DALTON_NOTIFY_ROUTES_KEY: {"any-task": ["telegram:user:123"]},
            }
        )

    # An empty route list is stored as-is: an explicit "deliver nowhere"
    # route. Only null means "no route" (personal targets / category fallback).
    bag_empty_route = {
        DALTON_NOTIFY_TARGETS_KEY: ["feishu:user:ou_me_1"],
        DALTON_NOTIFY_ROUTES_KEY: {"any-task": []},
    }
    validate_dalton_model_preferences(bag_empty_route)
    assert bag_empty_route[DALTON_NOTIFY_ROUTES_KEY] == {"any-task": []}


def test_removing_a_personal_target_cleans_routes_and_labels_but_keeps_chats() -> None:
    effective = {
        DALTON_NOTIFY_TARGETS_KEY: ["feishu:user:ou_me_1"],
        DALTON_NOTIFY_ROUTES_KEY: {
            # task-a loses the removed personal target but keeps its group;
            # task-b was only about the removed target; task-c untouched.
            "task-a": ["feishu:user:ou_removed", "feishu:chat:oc_group_1"],
            "task-b": ["feishu:user:ou_removed"],
            "task-c": ["feishu:user:ou_me_1"],
        },
        DALTON_NOTIFY_TARGET_LABELS_KEY: {
            "feishu:user:ou_removed": "旧号",
            "feishu:user:ou_me_1": "我本人",
        },
    }
    patch = {DALTON_NOTIFY_TARGETS_KEY: ["feishu:user:ou_me_1"]}
    validate_dalton_model_preferences(patch, effective_model_preference=effective)
    cleanup_routes = patch[DALTON_NOTIFY_ROUTES_KEY]
    assert cleanup_routes["task-a"] == ["feishu:chat:oc_group_1"]
    # task-b was only about the removed target: it becomes an explicit empty
    # route (deliver nowhere), never null — null would widen delivery back to
    # every personal target.
    assert cleanup_routes["task-b"] == []
    assert "task-c" not in cleanup_routes
    assert patch[DALTON_NOTIFY_TARGET_LABELS_KEY] == {"feishu:user:ou_removed": None}


@pytest.mark.asyncio
async def test_route_and_label_preferences_read_back_through_resolvers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_get_prefs = create_autospec(
        model_prefs_mod.get_user_preferences,
        return_value={
            "model_preference": {
                DALTON_NOTIFY_ROUTES_KEY: {
                    "task-a": ["feishu:chat:oc_group_1", "feishu:chat:oc_group_1"],
                    "task-b": [],
                    "task-c": ["telegram:user:oops"],
                    "task-d": None,
                },
                DALTON_NOTIFY_TARGET_LABELS_KEY: {"feishu:user:ou_me_1": "我本人"},
            },
            "other_preference": {},
        },
    )
    monkeypatch.setattr(model_prefs_mod, "get_user_preferences", mock_get_prefs)

    assert await resolve_dalton_notify_routes("u-1") == {
        "task-a": ["feishu:chat:oc_group_1"],
        # [] is an explicit deliver-nowhere route and survives normalization.
        "task-b": [],
    }
    assert await resolve_dalton_notify_target_labels("u-1") == {
        "feishu:user:ou_me_1": "我本人"
    }


# ---------------------------------------------------------------------------
# group-target write rules (user-scoped)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_write_without_chat_targets_needs_no_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(invites_mod, "is_invite_pm", lambda uid: False)
    # No discovery call: a personal-only route (or no route key at all) passes
    # even for a non-admin and with the bridge unreachable.
    async def _boom() -> list[dict[str, str]]:
        raise AssertionError("discovery must not be called")

    monkeypatch.setattr(groups_mod, "discover_feishu_notify_groups", _boom)
    await validate_dalton_notify_routes_for_user(
        "u-1", {"task": ["feishu:user:ou_me_1"]}
    )
    await validate_dalton_notify_routes_for_user("u-1", None)


@pytest.mark.asyncio
async def test_route_write_chat_targets_admin_only_and_discovery_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    async def _discovered() -> list[dict[str, str]]:
        return [{"chat_id": "oc_group_1", "name": "投研群"}]

    monkeypatch.setattr(groups_mod, "discover_feishu_notify_groups", _discovered)
    submitted = {"task": ["feishu:chat:oc_group_1"]}

    monkeypatch.setattr(invites_mod, "is_invite_pm", lambda uid: False)
    with pytest.raises(HTTPException) as non_pm:
        await validate_dalton_notify_routes_for_user("u-member", submitted)
    assert non_pm.value.status_code == 403

    monkeypatch.setattr(invites_mod, "is_invite_pm", lambda uid: True)
    with pytest.raises(HTTPException) as unknown:
        await validate_dalton_notify_routes_for_user(
            "u-pm", {"task": ["feishu:chat:oc_not_joined"]}
        )
    assert unknown.value.status_code == 400

    await validate_dalton_notify_routes_for_user("u-pm", submitted)

    async def _unavailable() -> list[dict[str, str]]:
        raise groups_mod.FeishuGroupsUnavailable("bridge down")

    monkeypatch.setattr(groups_mod, "discover_feishu_notify_groups", _unavailable)
    with pytest.raises(HTTPException) as unavailable:
        await validate_dalton_notify_routes_for_user("u-pm", submitted)
    assert unavailable.value.status_code == 503


@pytest.mark.asyncio
async def test_route_write_gates_only_groups_added_to_the_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two saved groups the bot has since left: removing one submits the other,
    which must not 400 (it was already in the route), while a group newly
    added — even beside it — is still checked against discovery."""
    from fastapi import HTTPException

    async def _discovered() -> list[dict[str, str]]:
        return [{"chat_id": "oc_live", "name": "在群"}]

    monkeypatch.setattr(groups_mod, "discover_feishu_notify_groups", _discovered)
    monkeypatch.setattr(invites_mod, "is_invite_pm", lambda uid: True)
    stored = {"task": ["feishu:chat:oc_left_1", "feishu:chat:oc_left_2"]}

    await validate_dalton_notify_routes_for_user(
        "u-pm", {"task": ["feishu:chat:oc_left_2"]}, stored
    )

    with pytest.raises(HTTPException) as added:
        await validate_dalton_notify_routes_for_user(
            "u-pm", {"task": ["feishu:chat:oc_left_2", "feishu:chat:oc_unknown"]}, stored
        )
    assert added.value.status_code == 400

    # A stored group under one task is still new under another.
    with pytest.raises(HTTPException) as moved:
        await validate_dalton_notify_routes_for_user(
            "u-pm", {"other": ["feishu:chat:oc_left_1"]}, stored
        )
    assert moved.value.status_code == 400

    # A non-admin can never add a group, stored elsewhere or not.
    monkeypatch.setattr(invites_mod, "is_invite_pm", lambda uid: False)
    with pytest.raises(HTTPException) as non_pm:
        await validate_dalton_notify_routes_for_user(
            "u-member", {"other": ["feishu:chat:oc_live"]}, stored
        )
    assert non_pm.value.status_code == 403


# ---------------------------------------------------------------------------
# dispatch: route resolution shared by settlement and retry
# ---------------------------------------------------------------------------


def test_resolve_notify_route_targets_personal_and_chat_split() -> None:
    automation = {"name": "dalton-googl-earn-2026q1-t0"}
    personal = ["feishu:user:ou_a", "feishu:user:ou_b"]

    # No routes -> None (personal targets only, groups never default).
    assert resolve_notify_route_targets(automation, None, personal) is None
    assert resolve_notify_route_targets(automation, {}, personal) is None

    routes = {"dalton-googl-earn-2026q1-t0": ["feishu:chat:oc_g", "feishu:user:ou_b"]}
    # Personal side intersects configured order; the group rides only for an admin.
    assert resolve_notify_route_targets(automation, routes, personal) == [
        "feishu:user:ou_b"
    ]
    assert (
        resolve_notify_route_targets(
            automation, routes, personal, include_chat_targets=True
        )
        == ["feishu:user:ou_b", "feishu:chat:oc_g"]
    )
    # A route holding only groups: personal intersection empty, admin still gets the group.
    only_group = {"category:earnings_t0": ["feishu:chat:oc_g"]}
    assert (
        resolve_notify_route_targets(
            automation, only_group, personal, include_chat_targets=True
        )
        == ["feishu:chat:oc_g"]
    )
    # Deleted personal targets drop out on their own.
    assert resolve_notify_route_targets(
        automation,
        {"dalton-googl-earn-2026q1-t0": ["feishu:user:ou_gone", "feishu:user:ou_a"]},
        personal,
    ) == ["feishu:user:ou_a"]


def _route_test_fns(
    *,
    targets: list[str],
    routes: dict[str, list[str]] | None,
) -> tuple[Any, Any, Any, Any]:
    return (
        create_autospec(resolve_dalton_notify_enabled, return_value=True),
        create_autospec(resolve_dalton_notify_overrides, return_value={}),
        create_autospec(resolve_dalton_notify_targets, return_value=targets),
        create_autospec(resolve_dalton_notify_routes, return_value=routes or {}),
    )


@pytest.mark.asyncio
async def test_eligibility_group_rules(
    caplog: pytest.LogCaptureFixture,
) -> None:
    automation = {"name": "dalton-googl-earn-2026q1-t0", "user_id": "u-1"}
    personal = ["feishu:user:ou_a"]
    routes = {
        "dalton-googl-earn-2026q1-t0": ["feishu:chat:oc_g", "feishu:user:ou_a"]
    }
    admin_fn = create_autospec(resolve_dalton_notify_enabled, return_value=True)
    member_fn = create_autospec(resolve_dalton_notify_enabled, return_value=False)

    # No route -> personal targets only, whatever the owner's role.
    fns = _route_test_fns(targets=personal, routes=None)
    allowed, reason, effective = await evaluate_user_notify_eligibility(
        "u-1",
        automation,
        resolve_enabled_fn=fns[0],
        resolve_overrides_fn=fns[1],
        resolve_targets_fn=fns[2],
        resolve_routes_fn=fns[3],
        is_admin_fn=member_fn,
    )
    assert (allowed, reason, effective) == (True, None, personal)

    # Admin owner: the route's group rides along.
    fns = _route_test_fns(targets=personal, routes=routes)
    allowed, reason, effective = await evaluate_user_notify_eligibility(
        "u-1",
        automation,
        resolve_enabled_fn=fns[0],
        resolve_overrides_fn=fns[1],
        resolve_targets_fn=fns[2],
        resolve_routes_fn=fns[3],
        is_admin_fn=admin_fn,
    )
    assert (allowed, reason, effective) == (
        True,
        None,
        ["feishu:user:ou_a", "feishu:chat:oc_g"],
    )

    # Non-admin owner: the group is dropped; the personal side still applies.
    allowed, reason, effective = await evaluate_user_notify_eligibility(
        "u-1",
        automation,
        resolve_enabled_fn=fns[0],
        resolve_overrides_fn=fns[1],
        resolve_targets_fn=fns[2],
        resolve_routes_fn=fns[3],
        is_admin_fn=member_fn,
    )
    assert (allowed, reason, effective) == (True, None, ["feishu:user:ou_a"])

    # Route holding only groups, non-admin owner: empty result skips.
    group_only = _route_test_fns(
        targets=personal, routes={"dalton-googl-earn-2026q1-t0": ["feishu:chat:oc_g"]}
    )
    with caplog.at_level(logging.INFO, logger="src.dalton.notify.dispatch"):
        allowed, reason, effective = await evaluate_user_notify_eligibility(
            "u-1",
            automation,
            resolve_enabled_fn=group_only[0],
            resolve_overrides_fn=group_only[1],
            resolve_targets_fn=group_only[2],
            resolve_routes_fn=group_only[3],
            is_admin_fn=member_fn,
        )
    assert (allowed, reason, effective) == (False, "target_removed", [])
    assert "routed task" in caplog.text


@pytest.mark.asyncio
async def test_on_automation_settled_delivers_route_personal_plus_group() -> None:
    personal = ["feishu:user:ou_a", "feishu:user:ou_b"]
    routes = {
        "dalton-googl-earn-2026q1-t0": ["feishu:chat:oc_g", "feishu:user:ou_b"]
    }
    fns = _route_test_fns(targets=personal, routes=routes)
    verdict_fn = create_autospec(
        get_verdict_for_execution, return_value={"word": "THESIS_STRENGTHENED"}
    )
    url_fn = create_autospec(get_dalton_frontend_url, return_value="https://dalton.example.com")
    claim_fn = create_autospec(claim_target_delivery, return_value=True)
    mark_fn = create_autospec(mark_target_delivery, return_value=None)
    admin_fn = create_autospec(resolve_dalton_notify_enabled, return_value=True)

    posted: list[list[str]] = []

    async def _fake_post(
        send_targets: list[str], text: str, card: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        _ = (text, card)
        posted.append(list(send_targets))
        return {"ok": True, "results": [{"target": t, "ok": True} for t in send_targets]}

    post_fn = create_autospec(post_bridge_notify, side_effect=_fake_post)

    pushed = await on_automation_settled(
        automation={"name": "dalton-googl-earn-2026q1-t0", "user_id": "u-pm"},
        execution_id="exec-routed-1",
        thread_id="th-routed",
        final_text="谷歌 Q1 营收超预期。",
        resolve_enabled_fn=fns[0],
        resolve_overrides_fn=fns[1],
        resolve_targets_fn=fns[2],
        resolve_routes_fn=fns[3],
        is_admin_fn=admin_fn,
        get_verdict_fn=verdict_fn,
        claim_target_fn=claim_fn,
        mark_target_fn=mark_fn,
        post_notify_fn=post_fn,
        frontend_url_fn=url_fn,
    )
    assert pushed is True
    assert posted == [["feishu:user:ou_b"], ["feishu:chat:oc_g"]]
    claimed = {call.args[2] for call in claim_fn.await_args_list}
    assert claimed == {"feishu:user:ou_b", "feishu:chat:oc_g"}


@pytest.mark.asyncio
async def test_retry_path_applies_the_same_route_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed group row for a task whose owner is not an admin is skipped; an
    out-of-route personal row is skipped the same way."""

    fake_cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    fake_cursor.rowcount = 1

    candidate_rows: list[dict[str, Any]] = [
        {
            "execution_id": "exec-routed-retry",
            "automation_id": "auto-1",
            "user_id": "u-member",
            "channel": "feishu",
            "target": "feishu:chat:oc_g",
            "payload_json": {
                "text": "GOOGL · 财报 T0 快讯",
                "automation_name": "dalton-googl-earn-2026q1-t0",
            },
            "automation_name": "dalton-googl-earn-2026q1-t0",
            "automation_metadata": {"ticker": "GOOGL"},
        }
    ]

    async def _cursor_aenter() -> Any:
        return fake_cursor

    async def _cursor_aexit(*args: object) -> None:
        _ = args

    async def _cursor_execute(sql: Any, params: Any = None, **kwargs: object) -> Any:
        _ = (params, kwargs)

    async def _cursor_fetchall() -> list[dict[str, Any]]:
        return list(candidate_rows)

    async def _cursor_fetchone() -> dict[str, Any] | None:
        return None

    fake_cursor.__aenter__.side_effect = _cursor_aenter
    fake_cursor.__aexit__.side_effect = _cursor_aexit
    fake_cursor.execute.side_effect = _cursor_execute
    fake_cursor.fetchall.side_effect = _cursor_fetchall
    fake_cursor.fetchone.side_effect = _cursor_fetchone

    fake_conn = create_autospec(psycopg.AsyncConnection, instance=True)
    fake_conn.cursor.return_value = fake_cursor

    @asynccontextmanager
    async def _fake_get_db_connection(conn: Any = None):
        _ = conn
        yield fake_conn

    monkeypatch.setattr(dispatch_mod, "get_db_connection", _fake_get_db_connection)

    marks: list[tuple[str, str, str | None]] = []

    async def _fake_mark(
        execution_id: str,
        channel: str,
        target: str,
        **kwargs: Any,
    ) -> bool:
        _ = (execution_id, channel)
        marks.append((str(kwargs.get("status")), target, kwargs.get("reason")))
        return True

    mark_fn = create_autospec(mark_target_delivery, side_effect=_fake_mark)
    post_fn = create_autospec(post_bridge_notify, return_value={"ok": True})

    fns = _route_test_fns(
        targets=["feishu:user:ou_a"],
        routes={"dalton-googl-earn-2026q1-t0": ["feishu:chat:oc_g", "feishu:user:ou_a"]},
    )
    member_fn = create_autospec(resolve_dalton_notify_enabled, return_value=False)
    sent = await retry_failed_notify_deliveries(
        post_notify_fn=post_fn,
        mark_delivery_fn=mark_fn,
        resolve_enabled_fn=fns[0],
        resolve_overrides_fn=fns[1],
        resolve_targets_fn=fns[2],
        resolve_routes_fn=fns[3],
        is_admin_fn=member_fn,
    )
    assert sent == 0
    post_fn.assert_not_called()
    # The group row is skipped for a non-admin owner.
    assert marks == [("skipped", "feishu:chat:oc_g", "target_removed")]


# ---------------------------------------------------------------------------
# fake Feishu HTTP (R7 pattern): all Feishu HTTP answered at the opener layer
# ---------------------------------------------------------------------------


class _FakeHTTPResponse(io.BytesIO):
    def __init__(
        self, body: bytes, status: int = 200, content_type: str = "application/json"
    ) -> None:
        super().__init__(body)
        self.status = status
        headers = email.message.Message()
        headers["Content-Type"] = content_type
        self.headers = headers


class _FakeOpener:
    """Stand-in for urllib's opener: every call answered by ``open_fn``."""

    def __init__(self, open_fn: Any) -> None:
        self._open_fn = open_fn

    def open(self, req: Any, timeout: Any = None) -> Any:
        return self._open_fn(req, timeout)


def _install_fake_feishu_http(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _open(req: Any, timeout: Any = None) -> _FakeHTTPResponse:
        url = req.full_url
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        calls.append({"url": url, "body": body, "method": req.get_method()})
        status, payload = handler(url, body)
        data = json.dumps(payload).encode("utf-8")
        if status >= 400:
            err_headers = email.message.Message()
            err_headers["Content-Type"] = "application/json"
            raise rb.urllib.error.HTTPError(
                url, status, "error", err_headers, io.BytesIO(data)
            )
        return _FakeHTTPResponse(data, status, "application/json")

    monkeypatch.setattr(
        rb.urllib.request, "build_opener", lambda *args: _FakeOpener(_open)
    )
    return calls


def _auth_ok(url: str) -> bool:
    return "tenant_access_token" in url


def _make_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, feishu_creds: bool = True
) -> rb.ResearchDataBridge:
    monkeypatch.setenv("DALTON_BRIDGE_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("DALTON_BRIDGE_TEST_MODE", "0")
    empty_env = tmp_path / "bridge.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(empty_env))
    monkeypatch.setenv("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "secret-gateway-token-xyz987")
    if feishu_creds:
        monkeypatch.setenv("FEISHU_APP_ID", "fake-app-id")
        monkeypatch.setenv("FEISHU_APP_SECRET", "fake-app-secret-never-log")
    else:
        monkeypatch.delenv("FEISHU_APP_ID", raising=False)
        monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    # BridgeConfig falls back to ~/.openclaw/openclaw.json when the env vars
    # are unset; a machine with real credentials there must not leak into the
    # "not configured" case, so the fallback is pinned to empty here.
    monkeypatch.setattr(rb, "_read_openclaw_feishu_credentials", lambda: ("", ""))
    return rb.ResearchDataBridge(rb.BridgeConfig())


# ---------------------------------------------------------------------------
# bridge: send-card argv + handle_notify channel split + discovery
# ---------------------------------------------------------------------------


def test_parse_notify_target_accepts_feishu_chat() -> None:
    assert rb.parse_notify_target("feishu:chat:oc_group_1") == ("feishu", "chat:oc_group_1")
    assert rb.parse_notify_target("feishu:user:ou_me_1") == ("feishu", "user:ou_me_1")
    assert rb.parse_notify_target("discord:user:123") == ("discord", "user:123")
    with pytest.raises(ValueError):
        rb.parse_notify_target("feishu:group:oc_group_1")
    with pytest.raises(ValueError):
        rb.parse_notify_target("feishu:chat:ou_wrong_prefix")


def test_build_feishu_send_card_cmd_receive_id_type() -> None:
    user_cmd = rb.build_feishu_send_card_cmd(
        script_path="/opt/send_card.py",
        receive_id="ou_me_1",
        card_json_file="/tmp/card.json",
    )
    assert user_cmd[user_cmd.index("--receive-id-type") + 1] == "open_id"
    assert user_cmd[user_cmd.index("--chat-id") + 1] == "ou_me_1"

    chat_cmd = rb.build_feishu_send_card_cmd(
        script_path="/opt/send_card.py",
        receive_id="oc_group_1",
        card_json_file="/tmp/card.json",
        receive_id_type="chat_id",
    )
    assert chat_cmd[chat_cmd.index("--receive-id-type") + 1] == "chat_id"
    assert chat_cmd[chat_cmd.index("--chat-id") + 1] == "oc_group_1"


@pytest.mark.asyncio
async def test_handle_notify_chat_card_via_script_and_text_via_openclaw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    script_cmds: list[list[str]] = []

    def _fake_subprocess_run(
        cmd: list[str],
        *,
        env: Any = None,
        capture_output: bool = True,
        text: bool = True,
        timeout: float = 15.0,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        _ = (env, capture_output, text, timeout, check)
        script_cmds.append(list(cmd))
        receive_id = cmd[cmd.index("--chat-id") + 1]
        if receive_id == "oc_fail_group":
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout='{"status": "ok"}', stderr="")

    run_spy = create_autospec(subprocess.run, side_effect=_fake_subprocess_run)
    monkeypatch.setattr(rb.subprocess, "run", run_spy)

    invoke_payloads: list[dict[str, Any]] = []

    def _fake_openclaw_post(
        *,
        gateway_url: str,
        gateway_token: str,
        payload: dict,
        timeout_s: float = 15.0,
    ) -> dict:
        _ = (gateway_url, gateway_token, timeout_s)
        invoke_payloads.append(payload)
        return {"ok": True, "result": {"via": "direct"}}

    monkeypatch.setattr(
        rb, "_post_openclaw_tools_invoke", _fake_openclaw_post
    )

    card = {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": "GOOGL · 财报 T0 快讯"}},
        "body": {"elements": [{"tag": "markdown", "content": "**结论**\n超预期。"}]},
    }
    with caplog.at_level(logging.INFO, logger="researchdata_bridge"):
        # Card push: user + chat both through the script; text-only chat push
        # rides the OpenClaw message tool with the chat:… target spelling.
        code, resp = await bridge.handle_notify(
            {
                "targets": [
                    "feishu:chat:oc_ok_group",
                    "feishu:chat:oc_fail_group",
                    "feishu:user:ou_script_path",
                ],
                "text": "正文",
                "card": card,
            }
        )
        code_text, resp_text = await bridge.handle_notify(
            {"targets": ["feishu:chat:oc_text_group"], "text": "纯文本"}
        )

    assert code == 200
    results = {item["target"]: item for item in resp["results"]}
    assert results["feishu:chat:oc_ok_group"]["ok"] is True
    assert results["feishu:user:ou_script_path"]["ok"] is True
    failed = results["feishu:chat:oc_fail_group"]
    assert failed["ok"] is False
    assert failed["error"] == "exit_code=1"

    # Both card sends went through the script, with the right id namespaces.
    assert len(script_cmds) == 3
    ok_cmd = script_cmds[0]
    assert ok_cmd[ok_cmd.index("--receive-id-type") + 1] == "chat_id"
    assert ok_cmd[ok_cmd.index("--chat-id") + 1] == "oc_ok_group"
    user_cmd = script_cmds[2]
    assert user_cmd[user_cmd.index("--receive-id-type") + 1] == "open_id"
    assert user_cmd[user_cmd.index("--chat-id") + 1] == "ou_script_path"

    # Text-only chat push went through the OpenClaw message tool.
    assert code_text == 200
    assert resp_text["results"][0]["ok"] is True
    assert invoke_payloads[0]["args"]["target"] == "chat:oc_text_group"
    assert invoke_payloads[0]["args"]["message"] == "纯文本"

    # No Feishu id reaches a log line in raw form — user or chat.
    assert "oc_ok_group" not in caplog.text
    assert "oc_fail_group" not in caplog.text
    assert "oc_text_group" not in caplog.text
    assert "ou_script_path" not in caplog.text
    assert "feishu:chat:<id>" in caplog.text
    assert "feishu:user:<id>" in caplog.text


def test_list_bot_chats_paginates_and_returns_only_id_and_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if _auth_ok(url):
            return 200, {"code": 0, "tenant_access_token": "t-fake", "expire": 7200}
        assert url.startswith("https://open.feishu.cn/open-apis/im/v1/chats")
        assert "page_size=100" in url
        if "page_token=p2" in url:
            return 200, {
                "code": 0,
                "data": {
                    "items": [
                        {
                            "chat_id": "oc_g3",
                            "name": "群三",
                            "owner_id": "ou_owner_secret",
                            "member_count": 42,
                        }
                    ],
                    "has_more": False,
                },
            }
        return 200, {
            "code": 0,
            "data": {
                "items": [{"chat_id": "oc_g1", "name": "群一", "owner_id": "ou_x"}],
                "has_more": True,
                "page_token": "p2",
            },
        }

    calls = _install_fake_feishu_http(monkeypatch, handler)
    client = rb.FeishuClient("fake-app-id", "fake-app-secret", timeout_s=1.0)
    chats = client.list_bot_chats()
    assert chats == [
        {"chat_id": "oc_g1", "name": "群一"},
        {"chat_id": "oc_g3", "name": "群三"},
    ]
    assert len([c for c in calls if "im/v1/chats" in c["url"]]) == 2


@pytest.mark.asyncio
async def test_handle_feishu_chats_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if _auth_ok(url):
            return 200, {"code": 0, "tenant_access_token": "t-fake", "expire": 7200}
        return 200, {
            "code": 0,
            "data": {
                "items": [
                    {"chat_id": "oc_g1", "name": "投研群", "owner_id": "ou_owner"}
                ]
            },
        }

    _install_fake_feishu_http(monkeypatch, handler)
    bridge = _make_bridge(tmp_path, monkeypatch)
    code, resp = await bridge.handle_feishu_chats({})
    assert code == 200
    # Only chat_id and name leave the bridge — no owner ids or counts.
    assert resp == {"ok": True, "chats": [{"chat_id": "oc_g1", "name": "投研群"}]}

    # No credentials -> 503.
    bridge_off = _make_bridge(tmp_path, monkeypatch, feishu_creds=False)
    code_off, resp_off = await bridge_off.handle_feishu_chats({})
    assert (code_off, resp_off) == (503, {"ok": False, "error": "feishu_not_configured"})

    # A Feishu refusal -> 502 with the bridge error code.
    def handler_fail(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if _auth_ok(url):
            return 200, {"code": 0, "tenant_access_token": "t-fake", "expire": 7200}
        return 400, {"code": 99991672, "msg": "no im scope", "log_id": "log-chats-1"}

    _install_fake_feishu_http(monkeypatch, handler_fail)
    bridge_fail = _make_bridge(tmp_path, monkeypatch)
    code_fail, resp_fail = await bridge_fail.handle_feishu_chats({})
    assert code_fail == 502
    assert resp_fail["ok"] is False
    assert resp_fail["error"].startswith("feishu_http_4")
    assert "ou_" not in str(resp_fail)


# ---------------------------------------------------------------------------
# bot info: which bot the user must invite to a group
# ---------------------------------------------------------------------------


def test_bot_info_request_shape_returns_app_name(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if _auth_ok(url):
            return 200, {"code": 0, "tenant_access_token": "t-fake", "expire": 7200}
        assert url == "https://open.feishu.cn/open-apis/bot/v3/info"
        # The real shape: bot/v3/info nests its payload under a top-level
        # `bot` key, not the usual `data` — no `data` exists to return.
        return 200, {
            "code": 0,
            "bot": {"app_name": "Everflow 投研", "open_id": "ou_bot_secret"},
        }

    calls = _install_fake_feishu_http(monkeypatch, handler)
    client = rb.FeishuClient("fake-app-id", "fake-app-secret", timeout_s=1.0)
    # Only the display name comes back — the open_id in the payload does not.
    assert client.bot_info() == "Everflow 投研"
    assert len([c for c in calls if "bot/v3/info" in c["url"]]) == 1


@pytest.mark.asyncio
async def test_handle_feishu_bot_info_returns_only_app_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if _auth_ok(url):
            return 200, {"code": 0, "tenant_access_token": "t-fake", "expire": 7200}
        return 200, {
            "code": 0,
            "bot": {"app_name": "Everflow 投研", "open_id": "ou_bot_secret"},
        }

    _install_fake_feishu_http(monkeypatch, handler)
    bridge = _make_bridge(tmp_path, monkeypatch)
    code, resp = await bridge.handle_feishu_bot_info({})
    assert code == 200
    # The bridge's answer carries the name only, straight from the real shape.
    assert resp == {"ok": True, "app_name": "Everflow 投研"}


# ---------------------------------------------------------------------------
# group discovery service + endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_feishu_notify_groups_caches_success_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    async def _fake_bridge(route: str, body: dict[str, Any]) -> dict[str, Any]:
        _ = body
        assert route == "chats"
        calls["n"] += 1
        return {"ok": True, "chats": [{"chat_id": "oc_g1", "name": "投研群", "extra": "x"}]}

    monkeypatch.setattr(groups_mod, "post_bridge_feishu", _fake_bridge)
    monkeypatch.setattr(groups_mod, "_groups_cache", None)

    g1 = await groups_mod.discover_feishu_notify_groups()
    g2 = await groups_mod.discover_feishu_notify_groups()
    assert g1 == [{"chat_id": "oc_g1", "name": "投研群"}]
    assert g2 == g1
    assert calls["n"] == 1

    # A failure raises, is not cached, and the next call retries.
    async def _failing_bridge(route: str, body: dict[str, Any]) -> dict[str, Any]:
        _ = (route, body)
        raise groups_mod.FeishuBridgeError("bridge_unreachable")

    monkeypatch.setattr(groups_mod, "_groups_cache", None)
    monkeypatch.setattr(groups_mod, "post_bridge_feishu", _failing_bridge)
    with pytest.raises(groups_mod.FeishuGroupsUnavailable):
        await groups_mod.discover_feishu_notify_groups()
    assert groups_mod._groups_cache is None


@pytest.mark.asyncio
async def test_feishu_groups_endpoint_pm_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    from src.dalton.app.router import dalton_notify_feishu_groups_endpoint

    monkeypatch.setattr(invites_mod, "is_invite_pm", lambda uid: False)
    with pytest.raises(HTTPException) as non_pm:
        await dalton_notify_feishu_groups_endpoint(user_id="u-member")
    assert non_pm.value.status_code == 403

    monkeypatch.setattr(invites_mod, "is_invite_pm", lambda uid: True)
    monkeypatch.setattr(groups_mod, "_groups_cache", None)

    async def _fake_bridge(route: str, body: dict[str, Any]) -> dict[str, Any]:
        _ = (route, body)
        return {"ok": True, "chats": [{"chat_id": "oc_g1", "name": "投研群"}]}

    monkeypatch.setattr(groups_mod, "post_bridge_feishu", _fake_bridge)
    res = await dalton_notify_feishu_groups_endpoint(user_id="u-pm")
    assert res == {"ok": True, "groups": [{"chat_id": "oc_g1", "name": "投研群"}]}


# ---------------------------------------------------------------------------
# POST /api/v1/dalton/notify/test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_test_endpoint_refuses_unconfigured_personal_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    from src.dalton.app.router import DaltonNotifyTestRequest, dalton_notify_test_endpoint

    async def _targets(user_id: str | None = None) -> list[str]:
        _ = user_id
        return ["feishu:user:ou_me_1"]

    monkeypatch.setattr(model_prefs_mod, "resolve_dalton_notify_targets", _targets)
    with pytest.raises(HTTPException) as excinfo:
        await dalton_notify_test_endpoint(
            DaltonNotifyTestRequest(target="feishu:user:ou_someone_else"),
            user_id="u-1",
        )
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_notify_test_endpoint_group_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    from src.dalton.app.router import DaltonNotifyTestRequest, dalton_notify_test_endpoint

    async def _discovered() -> list[dict[str, str]]:
        return [{"chat_id": "oc_group_1", "name": "投研群"}]

    monkeypatch.setattr(groups_mod, "discover_feishu_notify_groups", _discovered)

    # Non-admin testing a group -> 403 (no discovery needed past the gate).
    monkeypatch.setattr(invites_mod, "is_invite_pm", lambda uid: False)
    with pytest.raises(HTTPException) as non_pm:
        await dalton_notify_test_endpoint(
            DaltonNotifyTestRequest(target="feishu:chat:oc_group_1"),
            user_id="u-member",
        )
    assert non_pm.value.status_code == 403

    # Admin testing a group the bot is not in -> 404.
    monkeypatch.setattr(invites_mod, "is_invite_pm", lambda uid: True)
    with pytest.raises(HTTPException) as unknown:
        await dalton_notify_test_endpoint(
            DaltonNotifyTestRequest(target="feishu:chat:oc_not_joined"),
            user_id="u-pm",
        )
    assert unknown.value.status_code == 404

    # Admin testing a discovered group sends the card; the group's own name
    # is what the body shows.
    async def _ok_post(
        send_targets: list[str], text: str, card: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        assert send_targets == ["feishu:chat:oc_group_1"]
        assert card is not None
        assert card["header"]["title"]["content"] == "Dalton 推送测试"
        assert "投研群" in card["body"]["elements"][0]["content"]
        assert "投研群" in text
        return {"ok": True, "results": [{"target": send_targets[0], "ok": True}]}

    monkeypatch.setattr(dispatch_mod, "post_bridge_notify", _ok_post)
    res = await dalton_notify_test_endpoint(
        DaltonNotifyTestRequest(target="feishu:chat:oc_group_1"), user_id="u-pm"
    )
    assert res == {"ok": True, "detail": None, "error_code": None}


@pytest.mark.asyncio
async def test_notify_test_endpoint_personal_target_and_bridge_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.app.router import DaltonNotifyTestRequest, dalton_notify_test_endpoint

    async def _targets(user_id: str | None = None) -> list[str]:
        _ = user_id
        return ["feishu:user:ou_me_1"]

    async def _labels(user_id: str | None = None) -> dict[str, str]:
        _ = user_id
        return {"feishu:user:ou_me_1": "我本人"}

    async def _fail_post(
        send_targets: list[str], text: str, card: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        _ = (text, card)
        return {
            "ok": False,
            "results": [
                {
                    "target": send_targets[0],
                    "ok": False,
                    "error": "exit_code=1",
                }
            ],
        }

    monkeypatch.setattr(model_prefs_mod, "resolve_dalton_notify_targets", _targets)
    monkeypatch.setattr(model_prefs_mod, "resolve_dalton_notify_target_labels", _labels)
    monkeypatch.setattr(dispatch_mod, "post_bridge_notify", _fail_post)

    res = await dalton_notify_test_endpoint(
        DaltonNotifyTestRequest(target="feishu:user:ou_me_1"), user_id="u-1"
    )
    assert res["ok"] is False
    assert "exit_code=1" in res["detail"]
    assert res["error_code"] == "exit_code=1"

    async def _ok_post(
        send_targets: list[str], text: str, card: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        assert send_targets == ["feishu:user:ou_me_1"]
        assert "我本人" in text
        assert card is not None and card["schema"] == "2.0"
        return {"ok": True, "results": [{"target": send_targets[0], "ok": True}]}

    monkeypatch.setattr(dispatch_mod, "post_bridge_notify", _ok_post)
    res_ok = await dalton_notify_test_endpoint(
        DaltonNotifyTestRequest(target="feishu:user:ou_me_1"), user_id="u-1"
    )
    assert res_ok == {"ok": True, "detail": None, "error_code": None}


# ---------------------------------------------------------------------------
# r6 review fixes: group-only routes, explicit empty routes, write gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_with_no_personal_targets_still_delivers_routed_groups() -> None:
    """r6 #1: an admin who configured only groups (no personal targets) still
    gets a group-routed task delivered; a non-admin in the same shape skips."""
    automation = {"name": "dalton-googl-earn-2026q1-t0", "user_id": "u-owner"}
    routes = {"dalton-googl-earn-2026q1-t0": ["feishu:chat:oc_g"]}
    admin_fn = create_autospec(resolve_dalton_notify_enabled, return_value=True)
    member_fn = create_autospec(resolve_dalton_notify_enabled, return_value=False)

    fns = _route_test_fns(targets=[], routes=routes)
    allowed, reason, effective = await evaluate_user_notify_eligibility(
        "u-owner",
        automation,
        resolve_enabled_fn=fns[0],
        resolve_overrides_fn=fns[1],
        resolve_targets_fn=fns[2],
        resolve_routes_fn=fns[3],
        is_admin_fn=admin_fn,
    )
    assert (allowed, reason, effective) == (True, None, ["feishu:chat:oc_g"])

    allowed_member, reason_member, effective_member = await evaluate_user_notify_eligibility(
        "u-owner",
        automation,
        resolve_enabled_fn=fns[0],
        resolve_overrides_fn=fns[1],
        resolve_targets_fn=fns[2],
        resolve_routes_fn=fns[3],
        is_admin_fn=member_fn,
    )
    assert (allowed_member, reason_member, effective_member) == (
        False,
        "target_removed",
        [],
    )

    # Without any route, an empty personal list skips — a group is never a default.
    fns_no_route = _route_test_fns(targets=[], routes=None)
    allowed_none, reason_none, _ = await evaluate_user_notify_eligibility(
        "u-owner",
        automation,
        resolve_enabled_fn=fns_no_route[0],
        resolve_overrides_fn=fns_no_route[1],
        resolve_targets_fn=fns_no_route[2],
        resolve_routes_fn=fns_no_route[3],
        is_admin_fn=admin_fn,
    )
    assert (allowed_none, reason_none) == (False, "target_removed")


@pytest.mark.asyncio
async def test_deleting_the_only_routed_personal_target_stops_delivery() -> None:
    """r6 #2 end to end: A and B configured, the task routed to A only; the
    user deletes A -> the cleanup writes an explicit [] route, and the task
    delivers nowhere (B does not suddenly receive it)."""
    # A pushable task name, or classify_automation_for_notify treats it as
    # unroutable and the route never applies.
    task_name = "dalton-googl-earn-2026q1-t0"
    automation = {"name": task_name, "user_id": "u-1"}
    effective = {
        DALTON_NOTIFY_TARGETS_KEY: ["feishu:user:ou_b"],
        DALTON_NOTIFY_ROUTES_KEY: {
            task_name: ["feishu:user:ou_a"],
            "task-other": ["feishu:user:ou_a", "feishu:user:ou_b"],
        },
    }
    patch = {DALTON_NOTIFY_TARGETS_KEY: ["feishu:user:ou_b"]}
    validate_dalton_model_preferences(patch, effective_model_preference=effective)
    assert patch[DALTON_NOTIFY_ROUTES_KEY] == {
        task_name: [],
        "task-other": ["feishu:user:ou_b"],
    }

    # A stored [] resolves to deliver-nowhere, whatever the role.
    member_fn = create_autospec(resolve_dalton_notify_enabled, return_value=False)
    fns = _route_test_fns(
        targets=["feishu:user:ou_b"], routes={task_name: []}
    )
    allowed, reason, effective_targets = await evaluate_user_notify_eligibility(
        "u-1",
        automation,
        resolve_enabled_fn=fns[0],
        resolve_overrides_fn=fns[1],
        resolve_targets_fn=fns[2],
        resolve_routes_fn=fns[3],
        is_admin_fn=member_fn,
    )
    assert (allowed, reason, effective_targets) == (False, "target_removed", [])


@pytest.mark.asyncio
async def test_notify_keys_under_other_preference_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r6 #3: the three notify keys under other_preference bypass every
    model_preference validator, so the write is refused outright."""
    from fastapi import HTTPException

    from src.server.app import users as users_mod
    from src.server.models.user import UserPreferencesUpdate

    fake_user = {"user_id": "u-1", "email": "u@x.com"}
    monkeypatch.setattr(
        users_mod, "db_get_user", create_autospec(users_mod.db_get_user, return_value=fake_user)
    )

    for key in ("dalton_notify_routes", "dalton_notify_targets", "dalton_notify_target_labels"):
        body = UserPreferencesUpdate.model_validate(
            {"other_preference": {key: {"any-task": []}}}
        )
        with pytest.raises(HTTPException) as excinfo:
            await users_mod.update_preferences(body, user_id="u-1")
        assert excinfo.value.status_code == 400
        assert "model_preference" in str(excinfo.value.detail)


def test_partial_route_patch_cleans_stored_entries_too() -> None:
    """r6 #8: a patch that rewrites one task's route and drops a personal
    target must also clean the same target out of stored routes the patch
    never named (the one-level effective bag cannot show them)."""
    stored = {
        DALTON_NOTIFY_TARGETS_KEY: ["feishu:user:ou_a", "feishu:user:ou_b"],
        DALTON_NOTIFY_ROUTES_KEY: {
            "task-named": ["feishu:user:ou_a"],
            "task-stored": ["feishu:user:ou_a"],
        },
    }
    patch = {
        # The user removed ou_a AND rewrote task-named in one request.
        DALTON_NOTIFY_TARGETS_KEY: ["feishu:user:ou_b"],
        DALTON_NOTIFY_ROUTES_KEY: {"task-named": ["feishu:user:ou_b"]},
    }
    # The effective bag users.py builds is a one-level merge: the patch's own
    # targets and routes replace the stored ones whole, so `task-stored` is
    # invisible there — exactly the gap the stored bag closes.
    validate_dalton_model_preferences(
        patch,
        effective_model_preference={
            DALTON_NOTIFY_TARGETS_KEY: ["feishu:user:ou_b"],
            DALTON_NOTIFY_ROUTES_KEY: {"task-named": ["feishu:user:ou_b"]},
        },
        stored_model_preference=stored,
    )
    # task-stored was not in the patch, yet its deleted target is cleaned out
    # — as an explicit empty route, not a widening null.
    assert patch[DALTON_NOTIFY_ROUTES_KEY]["task-stored"] == []


@pytest.mark.asyncio
async def test_feishu_bot_endpoint_refuses_non_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r6 #9: the bot name endpoint serves the admin group card only."""
    from fastapi import HTTPException

    from src.dalton.app.router import dalton_notify_feishu_bot_endpoint

    monkeypatch.setattr(invites_mod, "is_invite_pm", lambda uid: False)
    with pytest.raises(HTTPException) as excinfo:
        await dalton_notify_feishu_bot_endpoint(user_id="u-member")
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_deleting_a_personal_target_survives_group_discovery_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r7 #1: the task route is [A, G]; the user deletes A. The cleanup keeps
    [G] in the patch, but the group gate sees only what the caller submitted —
    so a down discovery API, a group the bot left, or lost admin rights must
    not block deleting one's own personal target."""
    # The full users.py sequence: the user submits only the target-list change
    # (deleting A, their sole personal target); the cleanup writes the kept
    # group into the patch, then the gate sees the original submission.
    patch = {DALTON_NOTIFY_TARGETS_KEY: []}
    stored = {
        DALTON_NOTIFY_TARGETS_KEY: ["feishu:user:ou_a"],
        DALTON_NOTIFY_ROUTES_KEY: {
            "task-keep": ["feishu:user:ou_a", "feishu:chat:oc_gone_group"],
            "task-only-b": ["feishu:user:ou_a"],
        },
    }
    submitted_routes = patch.get(DALTON_NOTIFY_ROUTES_KEY)

    validate_dalton_model_preferences(
        patch,
        effective_model_preference={
            # users.py's one-level effective bag: the patch's targets, and the
            # stored routes (the patch names none).
            DALTON_NOTIFY_TARGETS_KEY: [],
            DALTON_NOTIFY_ROUTES_KEY: stored[DALTON_NOTIFY_ROUTES_KEY],
        },
        stored_model_preference=stored,
    )
    # The cleanup kept the stored group and emptied the A-only route…
    assert patch[DALTON_NOTIFY_ROUTES_KEY]["task-keep"] == ["feishu:chat:oc_gone_group"]
    assert patch[DALTON_NOTIFY_ROUTES_KEY]["task-only-b"] == []

    # …and the group gate on the *submitted* entries passes even when
    # discovery is down (or would not know the kept group), for anyone.
    monkeypatch.setattr(invites_mod, "is_invite_pm", lambda uid: False)

    async def _unavailable() -> list[dict[str, str]]:
        raise groups_mod.FeishuGroupsUnavailable("bridge down")

    monkeypatch.setattr(groups_mod, "discover_feishu_notify_groups", _unavailable)
    await validate_dalton_notify_routes_for_user("u-member", submitted_routes)

    # Contrast: a group the caller actually submitted is still gated — the
    # discovery outage is a 503 for a new group submission.
    from fastapi import HTTPException

    monkeypatch.setattr(invites_mod, "is_invite_pm", lambda uid: True)
    with pytest.raises(HTTPException) as submitted_gate:
        await validate_dalton_notify_routes_for_user(
            "u-pm", {"task-new": ["feishu:chat:oc_gone_group"]}
        )
    assert submitted_gate.value.status_code == 503


def test_cleanup_merges_on_normalized_route_keys() -> None:
    """r7 #3: a patch key with surrounding spaces (`" T "`) must override the
    stored `T` after normalization — not coexist with it and let the stale
    stored value survive the cleanup."""
    stored = {
        DALTON_NOTIFY_TARGETS_KEY: ["feishu:user:ou_a", "feishu:user:ou_b"],
        DALTON_NOTIFY_ROUTES_KEY: {"T": ["feishu:user:ou_a"]},
        # Label keys are target-shaped; the submitted copy pads with spaces.
        DALTON_NOTIFY_TARGET_LABELS_KEY: {"feishu:user:ou_a": "旧别名"},
    }
    # The user deletes ou_a, rewrites task T (spelled " T ") to ou_b, and
    # clears ou_a's label (its key spelled with padding spaces).
    patch = {
        DALTON_NOTIFY_TARGETS_KEY: ["feishu:user:ou_b"],
        DALTON_NOTIFY_ROUTES_KEY: {" T ": ["feishu:user:ou_b"]},
        DALTON_NOTIFY_TARGET_LABELS_KEY: {" feishu:user:ou_a ": None},
    }
    validate_dalton_model_preferences(
        patch,
        effective_model_preference={
            DALTON_NOTIFY_TARGETS_KEY: ["feishu:user:ou_b"],
            DALTON_NOTIFY_ROUTES_KEY: {" T ": ["feishu:user:ou_b"]},
            DALTON_NOTIFY_TARGET_LABELS_KEY: {" feishu:user:ou_a ": None},
        },
        stored_model_preference=stored,
    )
    # One T entry, the submitted one — the stored T (naming the deleted
    # ou_a) did not come back through an unnormalized key.
    routes = patch[DALTON_NOTIFY_ROUTES_KEY]
    assert routes["T"] == ["feishu:user:ou_b"]
    assert " T " not in routes
    # Labels merge on the normalized key too: the stored alias is deleted,
    # not duplicated under both spellings.
    labels = patch[DALTON_NOTIFY_TARGET_LABELS_KEY]
    assert labels == {"feishu:user:ou_a": None}
