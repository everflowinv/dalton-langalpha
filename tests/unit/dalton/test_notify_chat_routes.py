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
from collections.abc import Mapping
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
import src.dalton.access_tiers as access_tiers_mod  # noqa: E402
from src.dalton.access_tiers import get_user_tier  # noqa: E402
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
import src.server.database.pool as pool_mod  # noqa: E402


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
    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, return_value="external"),
    )
    # No discovery call: a personal-only route (or no route key at all) passes
    # even for an external member and with the bridge unreachable.
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

    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, return_value="external"),
    )
    with pytest.raises(HTTPException) as non_pm:
        await validate_dalton_notify_routes_for_user("u-member", submitted)
    assert non_pm.value.status_code == 403

    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, return_value="internal"),
    )
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
    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, return_value="admin"),
    )
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

    # An external member can never add a group, stored elsewhere or not.
    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, return_value="external"),
    )
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

    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, return_value="external"),
    )
    with pytest.raises(HTTPException) as non_pm:
        await dalton_notify_feishu_groups_endpoint(user_id="u-member")
    assert non_pm.value.status_code == 403

    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, return_value="internal"),
    )
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

    # External member testing a group -> 403 (no discovery needed past the gate).
    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, return_value="external"),
    )
    with pytest.raises(HTTPException) as non_pm:
        await dalton_notify_test_endpoint(
            DaltonNotifyTestRequest(target="feishu:chat:oc_group_1"),
            user_id="u-member",
        )
    assert non_pm.value.status_code == 403

    # Internal/admin testing a group the bot is not in -> 404.
    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, return_value="internal"),
    )
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
    """r6 #9: the bot name endpoint refuses external members."""
    from fastapi import HTTPException

    from src.dalton.app.router import dalton_notify_feishu_bot_endpoint

    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, return_value="external"),
    )
    with pytest.raises(HTTPException) as excinfo:
        await dalton_notify_feishu_bot_endpoint(user_id="u-member")
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_deleting_a_personal_target_survives_group_discovery_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r7 #1: the task route is [A, G]; the user deletes A. The cleanup keeps
    [G] in the patch, but the group gate sees only what the caller submitted —
    so a down discovery API, a group the bot left, or external tier must
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
    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, return_value="external"),
    )

    async def _unavailable() -> list[dict[str, str]]:
        raise groups_mod.FeishuGroupsUnavailable("bridge down")

    monkeypatch.setattr(groups_mod, "discover_feishu_notify_groups", _unavailable)
    await validate_dalton_notify_routes_for_user("u-member", submitted_routes)

    # Contrast: a group the caller actually submitted is still gated — the
    # discovery outage is a 503 for a new group submission.
    from fastapi import HTTPException

    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, return_value="admin"),
    )
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


# ---------------------------------------------------------------------------
# internal/admin tier permissions + conversational tool dalton_notify_settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_notify_permissions_by_user_tier_and_error_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """internal and admin can configure group routes, list groups/bot, and send
    group test cards; external is refused with 403; get_user_tier raising falls
    back to external."""
    from fastapi import HTTPException

    import src.dalton.access_tiers as access_tiers_mod
    from src.dalton.app import router as router_mod
    from src.dalton.app.router import (
        DaltonNotifyTestRequest,
        dalton_notify_feishu_bot_endpoint,
        dalton_notify_feishu_groups_endpoint,
        dalton_notify_test_endpoint,
    )

    async def _discovered() -> list[dict[str, str]]:
        return [{"chat_id": "oc_group_1", "name": "投研群"}]

    async def _bot_name() -> str | None:
        return "Everflow 投研"

    async def _ok_post(
        send_targets: list[str], text: str, card: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        _ = (text, card)
        return {"ok": True, "results": [{"target": send_targets[0], "ok": True}]}

    monkeypatch.setattr(groups_mod, "discover_feishu_notify_groups", _discovered)
    monkeypatch.setattr(router_mod, "_resolve_feishu_bot_name", _bot_name)
    monkeypatch.setattr(dispatch_mod, "post_bridge_notify", _ok_post)

    tier_map: dict[str, str] = {
        "u-admin": "admin",
        "u-internal": "internal",
        "u-external": "external",
    }

    async def _fake_get_user_tier(user_id: str | None) -> str:
        uid = str(user_id or "")
        if uid == "u-boom":
            raise RuntimeError("tier lookup failed")
        return tier_map.get(uid, "external")

    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, side_effect=_fake_get_user_tier),
    )

    for allowed_uid in ("u-admin", "u-internal"):
        assert await access_tiers_mod.is_internal_or_admin(allowed_uid) is True
        await validate_dalton_notify_routes_for_user(
            allowed_uid, {"task": ["feishu:chat:oc_group_1"]}
        )
        bot_res = await dalton_notify_feishu_bot_endpoint(user_id=allowed_uid)
        assert bot_res == {"ok": True, "app_name": "Everflow 投研"}
        groups_res = await dalton_notify_feishu_groups_endpoint(user_id=allowed_uid)
        assert groups_res == {
            "ok": True,
            "groups": [{"chat_id": "oc_group_1", "name": "投研群"}],
        }
        test_res = await dalton_notify_test_endpoint(
            DaltonNotifyTestRequest(target="feishu:chat:oc_group_1"),
            user_id=allowed_uid,
        )
        assert test_res == {"ok": True, "detail": None, "error_code": None}

    for denied_uid in ("u-external", "u-boom"):
        assert await access_tiers_mod.is_internal_or_admin(denied_uid) is False
        with pytest.raises(HTTPException) as route_exc:
            await validate_dalton_notify_routes_for_user(
                denied_uid, {"task": ["feishu:chat:oc_group_1"]}
            )
        assert route_exc.value.status_code == 403
        assert "外部成员不能使用飞书群推送" in str(route_exc.value.detail)

        with pytest.raises(HTTPException) as bot_exc:
            await dalton_notify_feishu_bot_endpoint(user_id=denied_uid)
        assert bot_exc.value.status_code == 403

        with pytest.raises(HTTPException) as groups_exc:
            await dalton_notify_feishu_groups_endpoint(user_id=denied_uid)
        assert groups_exc.value.status_code == 403

        with pytest.raises(HTTPException) as test_exc:
            await dalton_notify_test_endpoint(
                DaltonNotifyTestRequest(target="feishu:chat:oc_group_1"),
                user_id=denied_uid,
            )
        assert test_exc.value.status_code == 403
        assert "外部成员不能使用飞书群推送" in str(test_exc.value.detail)


@pytest.mark.asyncio
async def test_dispatch_group_targets_follow_owner_tier_internal_vs_external(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default dispatch admin check (`_default_notify_admin`) lets group targets
    ride along for internal and admin task owners, and drops them for external
    (including when get_user_tier raises)."""
    import src.dalton.access_tiers as access_tiers_mod

    automation = {"name": "dalton-googl-earn-2026q1-t0", "user_id": "u-internal"}
    personal = ["feishu:user:ou_a"]
    routes = {
        "dalton-googl-earn-2026q1-t0": ["feishu:chat:oc_g", "feishu:user:ou_a"]
    }
    fns = _route_test_fns(targets=personal, routes=routes)

    async def _tier_by_uid(user_id: str | None) -> str:
        uid = str(user_id or "")
        if uid == "u-boom":
            raise RuntimeError("roster error")
        if uid == "u-internal":
            return "internal"
        if uid == "u-admin":
            return "admin"
        return "external"

    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, side_effect=_tier_by_uid),
    )

    for uid in ("u-internal", "u-admin"):
        allowed, reason, effective = await evaluate_user_notify_eligibility(
            uid,
            automation,
            resolve_enabled_fn=fns[0],
            resolve_overrides_fn=fns[1],
            resolve_targets_fn=fns[2],
            resolve_routes_fn=fns[3],
        )
        assert (allowed, reason, effective) == (
            True,
            None,
            ["feishu:user:ou_a", "feishu:chat:oc_g"],
        )

    for uid in ("u-external", "u-boom"):
        allowed, reason, effective = await evaluate_user_notify_eligibility(
            uid,
            automation,
            resolve_enabled_fn=fns[0],
            resolve_overrides_fn=fns[1],
            resolve_targets_fn=fns[2],
            resolve_routes_fn=fns[3],
        )
        assert (allowed, reason, effective) == (
            True,
            None,
            ["feishu:user:ou_a"],
        )


def _notify_tool_call(args: dict[str, Any], call_id: str = "call_notify_1") -> dict[str, Any]:
    return {
        "name": "dalton_notify_settings",
        "args": args,
        "id": call_id,
        "type": "tool_call",
    }


def _approve_hitl_confirm(
    action_type: str, payload: dict[str, Any]
) -> tuple[bool, dict[str, Any]]:
    _ = action_type
    return (
        True,
        {
            "decisions": [
                {
                    "type": "approve",
                    "confirm_token": payload.get("confirm_token"),
                }
            ]
        },
    )


@pytest.mark.asyncio
async def test_dalton_notify_settings_list_returns_targets_groups_routes_and_switches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.dalton.access_tiers as access_tiers_mod
    import src.dalton.tools.notify_tools as notify_tools_mod
    from src.dalton.tools.notify_tools import create_notify_tools
    from src.server.database import automation as auto_db

    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        notify_tools_mod,
        "resolve_dalton_notify_enabled",
        create_autospec(resolve_dalton_notify_enabled, return_value=True),
    )
    monkeypatch.setattr(
        notify_tools_mod,
        "resolve_dalton_notify_targets",
        create_autospec(
            resolve_dalton_notify_targets, return_value=["feishu:user:ou_me_1"]
        ),
    )
    monkeypatch.setattr(
        notify_tools_mod,
        "resolve_dalton_notify_target_labels",
        create_autospec(
            resolve_dalton_notify_target_labels,
            return_value={"feishu:user:ou_me_1": "我本人"},
        ),
    )
    monkeypatch.setattr(
        notify_tools_mod,
        "resolve_dalton_notify_overrides",
        create_autospec(resolve_dalton_notify_overrides, return_value={}),
    )
    monkeypatch.setattr(
        notify_tools_mod,
        "resolve_dalton_notify_routes",
        create_autospec(
            resolve_dalton_notify_routes,
            return_value={"dalton-nvda-earn-2026q1-t0": ["feishu:chat:oc_g1"]},
        ),
    )
    monkeypatch.setattr(
        auto_db,
        "list_all_automations",
        create_autospec(
            auto_db.list_all_automations,
            return_value=[
                {
                    "automation_id": "auto-nvda-1",
                    "name": "dalton-nvda-earn-2026q1-t0",
                    "status": "active",
                    "metadata": {"ticker": "NVDA"},
                }
            ],
        ),
    )
    monkeypatch.setattr(
        notify_tools_mod,
        "discover_feishu_notify_groups",
        create_autospec(
            groups_mod.discover_feishu_notify_groups,
            return_value=[{"chat_id": "oc_g1", "name": "半导体投研群"}],
        ),
    )

    tool = create_notify_tools()[0]
    cmd = await tool.ainvoke(
        _notify_tool_call({"action": "list"}),
        config={"configurable": {"user_id": "u-internal"}},
    )
    payload = json.loads(cmd.update["messages"][0].content)
    assert payload["success"] is True
    assert payload["notify_enabled"] is True
    assert payload["personal_targets"] == [
        {"target": "feishu:user:ou_me_1", "label": "我本人"}
    ]
    assert payload["groups_available"] is True
    assert payload["groups"] == [
        {
            "chat_id": "oc_g1",
            "target": "feishu:chat:oc_g1",
            "name": "半导体投研群",
        }
    ]
    assert len(payload["automations"]) == 1
    auto_item = payload["automations"][0]
    assert auto_item["automation_id"] == "auto-nvda-1"
    assert auto_item["name"] == "dalton-nvda-earn-2026q1-t0"
    assert auto_item["notify_enabled"] is True
    assert auto_item["route_targets"] == ["feishu:chat:oc_g1"]
    assert auto_item["effective_targets"] == [
        {"target": "feishu:chat:oc_g1", "display": "半导体投研群 (feishu:chat:oc_g1)"}
    ]

    # When group discovery is unavailable, list still succeeds and reports groups_available=False.
    monkeypatch.setattr(
        notify_tools_mod,
        "discover_feishu_notify_groups",
        create_autospec(
            groups_mod.discover_feishu_notify_groups,
            side_effect=groups_mod.FeishuGroupsUnavailable("bridge offline"),
        ),
    )
    cmd_down = await tool.ainvoke(
        _notify_tool_call({"action": "list"}),
        config={"configurable": {"user_id": "u-internal"}},
    )
    payload_down = json.loads(cmd_down.update["messages"][0].content)
    assert payload_down["success"] is True
    assert payload_down["groups_available"] is False
    assert "不可用" in payload_down["groups_note"]


@pytest.mark.asyncio
async def test_dalton_notify_settings_set_route_rejects_foreign_task_unconfigured_personal_and_unknown_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.dalton.access_tiers as access_tiers_mod
    import src.dalton.tools.notify_tools as notify_tools_mod
    from src.dalton.tools.notify_tools import create_notify_tools
    from src.server.database import automation as auto_db

    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        auto_db,
        "list_all_automations",
        create_autospec(
            auto_db.list_all_automations,
            return_value=[{"automation_id": "a-1", "name": "dalton-nvda-earn-2026q1-t0"}],
        ),
    )
    monkeypatch.setattr(
        notify_tools_mod,
        "resolve_dalton_notify_targets",
        create_autospec(
            resolve_dalton_notify_targets, return_value=["feishu:user:ou_me_1"]
        ),
    )
    monkeypatch.setattr(
        notify_tools_mod,
        "resolve_dalton_notify_target_labels",
        create_autospec(
            resolve_dalton_notify_target_labels,
            return_value={"feishu:user:ou_me_1": "我本人"},
        ),
    )
    monkeypatch.setattr(
        notify_tools_mod,
        "discover_feishu_notify_groups",
        create_autospec(
            groups_mod.discover_feishu_notify_groups,
            return_value=[{"chat_id": "oc_g1", "name": "半导体投研群"}],
        ),
    )
    confirm_spy = create_autospec(
        notify_tools_mod.hitl_confirm,
        side_effect=AssertionError("hitl_confirm must not be called on invalid input"),
    )
    monkeypatch.setattr(notify_tools_mod, "hitl_confirm", confirm_spy)

    tool = create_notify_tools()[0]
    cfg = {"configurable": {"user_id": "u-internal"}}

    # 1. Foreign / non-existent automation
    cmd_foreign = await tool.ainvoke(
        _notify_tool_call(
            {
                "action": "set_route",
                "automation": "someone-elses-task",
                "targets": ["feishu:chat:oc_g1"],
            }
        ),
        config=cfg,
    )
    res_foreign = json.loads(cmd_foreign.update["messages"][0].content)
    assert res_foreign["success"] is False
    assert "不存在或不属于当前用户" in res_foreign["error"]

    # 2. Personal target not in current user's target list
    cmd_personal = await tool.ainvoke(
        _notify_tool_call(
            {
                "action": "set_route",
                "automation": "dalton-nvda-earn-2026q1-t0",
                "targets": ["feishu:user:ou_other_person"],
            }
        ),
        config=cfg,
    )
    res_personal = json.loads(cmd_personal.update["messages"][0].content)
    assert res_personal["success"] is False
    assert "尚未添加，请先添加个人推送目标" in res_personal["error"]

    # 3. Group not in bot's discovered groups
    cmd_group = await tool.ainvoke(
        _notify_tool_call(
            {
                "action": "set_route",
                "automation": "dalton-nvda-earn-2026q1-t0",
                "targets": ["feishu:chat:oc_not_joined"],
            }
        ),
        config=cfg,
    )
    res_group = json.loads(cmd_group.update["messages"][0].content)
    assert res_group["success"] is False
    assert "不在机器人已加入的群列表中" in res_group["error"]
    confirm_spy.assert_not_called()


@pytest.mark.asyncio
async def test_dalton_notify_settings_set_route_confirm_writes_via_shared_validator_and_decline_skips_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.dalton.access_tiers as access_tiers_mod
    import src.dalton.tools.notify_tools as notify_tools_mod
    from src.dalton.tools.notify_tools import create_notify_tools
    from src.server.database import automation as auto_db
    from src.server.database import user as user_db
    from src.tools.secretary._commands import hitl_confirm as real_hitl_confirm

    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        auto_db,
        "list_all_automations",
        create_autospec(
            auto_db.list_all_automations,
            return_value=[{"automation_id": "a-1", "name": "dalton-nvda-earn-2026q1-t0"}],
        ),
    )
    stored_prefs: dict[str, Any] = {
        "model_preference": {
            "dalton_notify_enabled": False,
            "dalton_notify_targets": ["feishu:user:ou_me_1"],
            "dalton_notify_target_labels": {"feishu:user:ou_me_1": "我本人"},
            "dalton_notify_overrides": {"dalton-nvda-earn-2026q1-t0": False},
            "dalton_notify_routes": {},
        },
        "other_preference": {},
    }
    monkeypatch.setattr(
        model_prefs_mod,
        "get_user_preferences",
        create_autospec(model_prefs_mod.get_user_preferences, return_value=stored_prefs),
    )
    monkeypatch.setattr(
        user_db,
        "get_user_preferences",
        create_autospec(user_db.get_user_preferences, return_value=stored_prefs),
    )
    monkeypatch.setattr(
        user_db,
        "invalidate_user_prefs_cache",
        create_autospec(user_db.invalidate_user_prefs_cache, return_value=None),
    )
    monkeypatch.setattr(
        user_db,
        "invalidate_user_profile_cache",
        create_autospec(user_db.invalidate_user_profile_cache, return_value=None),
    )

    upserted_patches: list[dict[str, Any]] = []

    async def _fake_upsert(
        user_id: str,
        risk_preference: dict[str, Any] | None = None,
        investment_preference: dict[str, Any] | None = None,
        agent_preference: dict[str, Any] | None = None,
        other_preference: dict[str, Any] | None = None,
        model_preference: dict[str, Any] | None = None,
        replace: bool = False,
        conn: Any = None,
    ) -> dict[str, Any]:
        _ = (
            user_id,
            risk_preference,
            investment_preference,
            agent_preference,
            other_preference,
            replace,
            conn,
        )
        upserted_patches.append(dict(model_preference or {}))
        return {"model_preference": model_preference}

    monkeypatch.setattr(
        user_db,
        "upsert_user_preferences",
        create_autospec(user_db.upsert_user_preferences, side_effect=_fake_upsert),
    )

    discovered_groups = [{"chat_id": "oc_g1", "name": "半导体投研群"}]
    monkeypatch.setattr(
        notify_tools_mod,
        "discover_feishu_notify_groups",
        create_autospec(
            groups_mod.discover_feishu_notify_groups,
            return_value=discovered_groups,
        ),
    )
    monkeypatch.setattr(
        groups_mod,
        "discover_feishu_notify_groups",
        create_autospec(
            groups_mod.discover_feishu_notify_groups,
            return_value=discovered_groups,
        ),
    )

    # Wrap the real validators to prove set_route runs through the exact same validators as PUT.
    validate_sync_spy = create_autospec(
        model_prefs_mod.validate_dalton_model_preferences,
        side_effect=model_prefs_mod.validate_dalton_model_preferences,
    )
    validate_user_spy = create_autospec(
        model_prefs_mod.validate_dalton_notify_routes_for_user,
        side_effect=model_prefs_mod.validate_dalton_notify_routes_for_user,
    )
    monkeypatch.setattr(
        model_prefs_mod, "validate_dalton_model_preferences", validate_sync_spy
    )
    monkeypatch.setattr(
        model_prefs_mod, "validate_dalton_notify_routes_for_user", validate_user_spy
    )

    tool = create_notify_tools()[0]
    cfg = {"configurable": {"user_id": "u-internal"}}

    # 1. Decline -> does not write
    decline_confirm = create_autospec(real_hitl_confirm, return_value=(False, None))
    monkeypatch.setattr(notify_tools_mod, "hitl_confirm", decline_confirm)
    cmd_declined = await tool.ainvoke(
        _notify_tool_call(
            {
                "action": "set_route",
                "automation": "dalton-nvda-earn-2026q1-t0",
                "targets": ["我本人", "半导体投研群"],
            }
        ),
        config=cfg,
    )
    declined_text = cmd_declined.update["messages"][0].content
    assert "取消" in declined_text
    assert upserted_patches == []
    decline_confirm.assert_called_once()
    confirm_payload = decline_confirm.call_args.args[1]
    assert confirm_payload["name"] == "设置定时任务推送"
    assert confirm_payload["args"] == {
        "任务": "dalton-nvda-earn-2026q1-t0",
        "推送到": ["我本人（个人，feishu:user:ou_me_1）", "半导体投研群（群，oc_g1）"],
        "提示": "你的推送总开关目前是关的，在设置里打开后才会推送",
    }
    assert "dalton-nvda-earn-2026q1-t0 →" in confirm_payload["summary"]
    assert "我本人" in confirm_payload["summary"]
    assert "半导体投研群" in confirm_payload["summary"]

    # 2. Confirm -> validates via shared validators, writes single-task route/override, does NOT touch master switch
    approve_confirm = create_autospec(
        real_hitl_confirm, side_effect=_approve_hitl_confirm
    )
    monkeypatch.setattr(notify_tools_mod, "hitl_confirm", approve_confirm)
    cmd_approved = await tool.ainvoke(
        _notify_tool_call(
            {
                "action": "set_route",
                "automation": "dalton-nvda-earn-2026q1-t0",
                "targets": ["我本人", "半导体投研群"],
            }
        ),
        config=cfg,
    )
    res_approved = json.loads(cmd_approved.update["messages"][0].content)
    assert res_approved["success"] is True
    assert res_approved["targets"] == ["feishu:user:ou_me_1", "feishu:chat:oc_g1"]
    assert res_approved["notify_enabled"] is False
    assert "你的推送总开关目前是关的，在设置里打开后才会推送" in res_approved["note"]
    assert "你的推送总开关目前是关的，在设置里打开后才会推送" in res_approved["summary"]
    assert validate_sync_spy.call_count == 1
    assert validate_user_spy.await_count == 1
    assert len(upserted_patches) == 1
    written = upserted_patches[0]
    assert written["dalton_notify_routes"] == {
        "dalton-nvda-earn-2026q1-t0": ["feishu:user:ou_me_1", "feishu:chat:oc_g1"]
    }
    assert "dalton_notify_enabled" not in written
    assert written["dalton_notify_overrides"] == {
        "dalton-nvda-earn-2026q1-t0": True
    }

    # 3. Empty targets=[] -> writes explicit [] ("不推送")
    cmd_empty = await tool.ainvoke(
        _notify_tool_call(
            {
                "action": "set_route",
                "automation": "dalton-nvda-earn-2026q1-t0",
                "targets": [],
            }
        ),
        config=cfg,
    )
    res_empty = json.loads(cmd_empty.update["messages"][0].content)
    assert res_empty["success"] is True
    assert res_empty["targets"] == []
    assert upserted_patches[-1]["dalton_notify_routes"] == {
        "dalton-nvda-earn-2026q1-t0": []
    }


def _mock_locked_prefs_db(
    monkeypatch: pytest.MonkeyPatch,
    stored_prefs: dict[str, Any],
    upserted: list[dict[str, Any]],
) -> Any:
    from src.server.database import user as user_db

    fake_tx = create_autospec(psycopg.AsyncTransaction, instance=True)
    fake_tx.__aenter__.return_value = fake_tx
    fake_tx.__aexit__.return_value = False

    fake_conn = create_autospec(psycopg.AsyncConnection, instance=True)
    fake_conn.transaction.return_value = fake_tx

    @asynccontextmanager
    async def _fake_get_db_conn():
        yield fake_conn

    monkeypatch.setattr(
        pool_mod,
        "get_db_connection",
        create_autospec(pool_mod.get_db_connection, side_effect=_fake_get_db_conn),
    )

    async def _fake_lock_prefs(user_id: str, *, conn: Any) -> dict[str, Any]:
        _ = user_id
        assert conn is fake_conn
        return {
            "model_preference": dict(stored_prefs.get("model_preference") or {}),
            "other_preference": dict(stored_prefs.get("other_preference") or {}),
        }

    lock_spy = create_autospec(user_db.lock_user_preferences, side_effect=_fake_lock_prefs)
    monkeypatch.setattr(user_db, "lock_user_preferences", lock_spy)
    monkeypatch.setattr(
        model_prefs_mod,
        "get_user_preferences",
        create_autospec(model_prefs_mod.get_user_preferences, side_effect=lambda _u: stored_prefs),
    )
    monkeypatch.setattr(
        user_db,
        "get_user_preferences",
        create_autospec(user_db.get_user_preferences, side_effect=lambda _u: stored_prefs),
    )
    monkeypatch.setattr(
        user_db,
        "invalidate_user_prefs_cache",
        create_autospec(user_db.invalidate_user_prefs_cache, return_value=None),
    )
    monkeypatch.setattr(
        user_db,
        "invalidate_user_profile_cache",
        create_autospec(user_db.invalidate_user_profile_cache, return_value=None),
    )

    async def _fake_upsert(
        user_id: str,
        risk_preference: dict[str, Any] | None = None,
        investment_preference: dict[str, Any] | None = None,
        agent_preference: dict[str, Any] | None = None,
        other_preference: dict[str, Any] | None = None,
        model_preference: dict[str, Any] | None = None,
        replace: bool = False,
        conn: Any = None,
    ) -> dict[str, Any]:
        _ = (
            user_id,
            risk_preference,
            investment_preference,
            agent_preference,
            other_preference,
            replace,
            conn,
        )
        patch_copy = dict(model_preference or {})
        upserted.append(patch_copy)
        merged_mp = dict(stored_prefs.get("model_preference") or {})
        for k, v in patch_copy.items():
            if isinstance(v, Mapping) and isinstance(merged_mp.get(k), Mapping):
                sub = dict(merged_mp[k])
                for sk, sv in v.items():
                    if sv is None:
                        sub.pop(sk, None)
                    else:
                        sub[sk] = sv
                merged_mp[k] = sub
            elif v is None:
                merged_mp.pop(k, None)
            else:
                merged_mp[k] = v
        stored_prefs["model_preference"] = merged_mp
        return {"model_preference": dict(merged_mp)}

    monkeypatch.setattr(
        user_db,
        "upsert_user_preferences",
        create_autospec(user_db.upsert_user_preferences, side_effect=_fake_upsert),
    )
    return lock_spy


@pytest.mark.asyncio
async def test_dalton_notify_settings_add_personal_target_confirm_and_decline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.dalton.access_tiers as access_tiers_mod
    import src.dalton.tools.notify_tools as notify_tools_mod
    from src.dalton.tools.notify_tools import create_notify_tools

    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, return_value="internal"),
    )
    stored_prefs: dict[str, Any] = {
        "model_preference": {
            "dalton_notify_targets": ["feishu:user:ou_existing"],
            "dalton_notify_target_labels": {"feishu:user:ou_existing": "已有目标"},
        },
        "other_preference": {},
    }
    upserted: list[dict[str, Any]] = []
    lock_spy = _mock_locked_prefs_db(monkeypatch, stored_prefs, upserted)

    monkeypatch.setattr(
        notify_tools_mod,
        "hitl_confirm",
        create_autospec(
            notify_tools_mod.hitl_confirm, side_effect=_approve_hitl_confirm
        ),
    )

    tool = create_notify_tools()[0]
    cfg = {"configurable": {"user_id": "u-internal"}}

    cmd = await tool.ainvoke(
        _notify_tool_call(
            {
                "action": "add_personal_target",
                "target": "feishu:user:ou_new_user",
                "label": "张分析师",
            }
        ),
        config=cfg,
    )
    res = json.loads(cmd.update["messages"][0].content)
    assert res["success"] is True
    assert res["personal_targets"] == [
        "feishu:user:ou_existing",
        "feishu:user:ou_new_user",
    ]
    assert res["target_labels"]["feishu:user:ou_new_user"] == "张分析师"
    assert res["target_labels"]["feishu:user:ou_existing"] == "已有目标"
    assert len(upserted) == 1
    assert upserted[0]["dalton_notify_target_labels"] == {
        "feishu:user:ou_new_user": "张分析师"
    }
    assert lock_spy.await_count == 1


@pytest.mark.asyncio
async def test_dalton_notify_settings_test_sends_only_to_routed_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.dalton.access_tiers as access_tiers_mod
    import src.dalton.tools.notify_tools as notify_tools_mod
    from src.dalton.tools.notify_tools import create_notify_tools
    from src.server.database import automation as auto_db

    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        auto_db,
        "list_all_automations",
        create_autospec(
            auto_db.list_all_automations,
            return_value=[{"automation_id": "a-1", "name": "dalton-nvda-earn-2026q1-t0"}],
        ),
    )
    # User has 2 personal targets and 2 discovered groups, but the automation route
    # only includes ou_a and oc_g1.
    personal_list = ["feishu:user:ou_a", "feishu:user:ou_b"]
    labels_map = {"feishu:user:ou_a": "分析师A", "feishu:user:ou_b": "分析师B"}
    routes_map = {
        "dalton-nvda-earn-2026q1-t0": ["feishu:user:ou_a", "feishu:chat:oc_g1"]
    }
    groups_list = [
        {"chat_id": "oc_g1", "name": "核心群1"},
        {"chat_id": "oc_g2", "name": "其他群2"},
    ]

    monkeypatch.setattr(
        notify_tools_mod,
        "resolve_dalton_notify_targets",
        create_autospec(resolve_dalton_notify_targets, return_value=personal_list),
    )
    monkeypatch.setattr(
        model_prefs_mod,
        "resolve_dalton_notify_targets",
        create_autospec(resolve_dalton_notify_targets, return_value=personal_list),
    )
    monkeypatch.setattr(
        notify_tools_mod,
        "resolve_dalton_notify_target_labels",
        create_autospec(resolve_dalton_notify_target_labels, return_value=labels_map),
    )
    monkeypatch.setattr(
        model_prefs_mod,
        "resolve_dalton_notify_target_labels",
        create_autospec(resolve_dalton_notify_target_labels, return_value=labels_map),
    )
    monkeypatch.setattr(
        notify_tools_mod,
        "resolve_dalton_notify_routes",
        create_autospec(resolve_dalton_notify_routes, return_value=routes_map),
    )
    monkeypatch.setattr(
        notify_tools_mod,
        "discover_feishu_notify_groups",
        create_autospec(
            groups_mod.discover_feishu_notify_groups, return_value=groups_list
        ),
    )
    monkeypatch.setattr(
        groups_mod,
        "discover_feishu_notify_groups",
        create_autospec(
            groups_mod.discover_feishu_notify_groups, return_value=groups_list
        ),
    )
    monkeypatch.setattr(
        notify_tools_mod,
        "hitl_confirm",
        create_autospec(
            notify_tools_mod.hitl_confirm, side_effect=_approve_hitl_confirm
        ),
    )

    sent_targets: list[str] = []

    async def _fake_bridge_post(
        targets: list[str], text: str, card: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        _ = (text, card)
        sent_targets.extend(targets)
        return {"ok": True, "results": [{"target": t, "ok": True} for t in targets]}

    monkeypatch.setattr(
        dispatch_mod,
        "post_bridge_notify",
        create_autospec(post_bridge_notify, side_effect=_fake_bridge_post),
    )

    tool = create_notify_tools()[0]
    cmd = await tool.ainvoke(
        _notify_tool_call(
            {"action": "test", "automation": "dalton-nvda-earn-2026q1-t0"}
        ),
        config={"configurable": {"user_id": "u-internal"}},
    )
    payload = json.loads(cmd.update["messages"][0].content)
    assert payload["success"] is True
    assert sent_targets == ["feishu:user:ou_a", "feishu:chat:oc_g1"]
    assert [r["target"] for r in payload["results"]] == [
        "feishu:user:ou_a",
        "feishu:chat:oc_g1",
    ]
    assert all(r["ok"] is True for r in payload["results"])


@pytest.mark.asyncio
async def test_hitl_confirm_payload_shape_and_decision_approve_reject_for_all_three_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 2 #1: real hitl_confirm emits Chinese name/args for the generic tool
    approval card and handles both approve and reject decisions."""
    import src.dalton.access_tiers as access_tiers_mod
    from src.dalton.tools.notify_tools import create_notify_tools
    from src.server.database import automation as auto_db
    import src.tools.secretary._commands as commands_mod

    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        auto_db,
        "list_all_automations",
        create_autospec(
            auto_db.list_all_automations,
            return_value=[{"automation_id": "a-1", "name": "Morning Macro Digest"}],
        ),
    )
    stored_prefs: dict[str, Any] = {
        "model_preference": {
            "dalton_notify_enabled": True,
            "dalton_notify_targets": ["feishu:user:ou_zhang"],
            "dalton_notify_target_labels": {"feishu:user:ou_zhang": "张三"},
            "dalton_notify_overrides": {},
            "dalton_notify_routes": {
                "automation:a-1": ["feishu:chat:oc_market", "feishu:user:ou_zhang"]
            },
        },
        "other_preference": {},
    }
    upserted: list[dict[str, Any]] = []
    _mock_locked_prefs_db(monkeypatch, stored_prefs, upserted)

    discovered_groups = [{"chat_id": "oc_market", "name": "Market Alert"}]
    import src.dalton.tools.notify_tools as notify_tools_mod

    discover_mock = create_autospec(
        groups_mod.discover_feishu_notify_groups,
        return_value=discovered_groups,
    )
    monkeypatch.setattr(groups_mod, "discover_feishu_notify_groups", discover_mock)
    monkeypatch.setattr(notify_tools_mod, "discover_feishu_notify_groups", discover_mock)
    monkeypatch.setattr(
        dispatch_mod,
        "post_bridge_notify",
        create_autospec(
            post_bridge_notify,
            return_value={
                "ok": True,
                "results": [
                    {"target": "feishu:chat:oc_market", "ok": True},
                    {"target": "feishu:user:ou_zhang", "ok": True},
                ],
            },
        ),
    )

    tool = create_notify_tools()[0]
    cfg = {"configurable": {"user_id": "u-internal"}}

    captured_payloads: list[dict[str, Any]] = []
    real_interrupt = commands_mod.interrupt

    def _interrupt_reject(payload: dict[str, Any]) -> dict[str, Any]:
        captured_payloads.append(payload)
        return {"decisions": [{"type": "reject", "message": "暂不执行"}]}

    monkeypatch.setattr(
        commands_mod,
        "interrupt",
        create_autospec(real_interrupt, side_effect=_interrupt_reject),
    )

    # Reject all 3 actions via the real hitl_confirm
    for call_args in (
        {
            "action": "set_route",
            "automation": "Morning Macro Digest",
            "targets": ["Market Alert", "张三"],
        },
        {
            "action": "add_personal_target",
            "target": "feishu:user:ou_lisi",
            "label": "李四",
        },
        {
            "action": "test",
            "automation": "Morning Macro Digest",
        },
    ):
        cmd_rej = await tool.ainvoke(_notify_tool_call(call_args), config=cfg)
        assert "取消" in cmd_rej.update["messages"][0].content

    assert len(captured_payloads) == 3
    req_set = captured_payloads[0]["action_requests"][0]
    assert req_set["type"] == "dalton_notify_set_route"
    assert req_set["name"] == "设置定时任务推送"
    assert isinstance(req_set.get("confirm_token"), str) and len(req_set["confirm_token"]) == 64
    assert req_set["args"] == {
        "任务": "Morning Macro Digest",
        "推送到": [
            "Market Alert（群，oc_market）",
            "张三（个人，feishu:user:ou_zhang）",
        ],
    }

    req_add = captured_payloads[1]["action_requests"][0]
    assert req_add["type"] == "dalton_notify_add_personal_target"
    assert req_add["name"] == "添加个人推送目标"
    assert isinstance(req_add.get("confirm_token"), str) and len(req_add["confirm_token"]) == 64
    assert req_add["args"] == {"目标": "feishu:user:ou_lisi", "名称": "李四"}

    req_test = captured_payloads[2]["action_requests"][0]
    assert req_test["type"] == "dalton_notify_test"
    assert req_test["name"] == "发送测试推送"
    assert isinstance(req_test.get("confirm_token"), str) and len(req_test["confirm_token"]) == 64
    assert req_test["args"] == {
        "任务": "Morning Macro Digest",
        "推送到": [
            "张三（个人，feishu:user:ou_zhang）",
            "Market Alert（群，oc_market）",
        ],
    }
    assert upserted == []

    # Approve all 3 actions via the real hitl_confirm (echoing back confirm_token)
    def _interrupt_approve(payload: dict[str, Any]) -> dict[str, Any]:
        captured_payloads.append(payload)
        token = payload["action_requests"][0].get("confirm_token")
        return {"decisions": [{"type": "approve", "confirm_token": token}]}

    monkeypatch.setattr(
        commands_mod,
        "interrupt",
        create_autospec(real_interrupt, side_effect=_interrupt_approve),
    )
    for call_args in (
        {
            "action": "set_route",
            "automation": "Morning Macro Digest",
            "targets": ["Market Alert", "张三"],
        },
        {
            "action": "add_personal_target",
            "target": "feishu:user:ou_lisi",
            "label": "李四",
        },
        {
            "action": "test",
            "automation": "Morning Macro Digest",
        },
    ):
        cmd_ok = await tool.ainvoke(_notify_tool_call(call_args), config=cfg)
        res_ok = json.loads(cmd_ok.update["messages"][0].content)
        assert res_ok["success"] is True


@pytest.mark.asyncio
async def test_custom_automation_with_group_route_pushes_on_settled_and_unrouted_does_not_push() -> None:
    """Round 2 #2: a custom automation configured with an explicit route pushes on
    settlement (category='custom', label_zh='定时任务', scope=<name>); an unrouted
    custom automation does not push."""
    custom_auto = {
        "automation_id": "a-custom-1",
        "name": "Morning Macro Digest",
        "user_id": "u-pm",
    }
    personal = ["feishu:user:ou_pm"]
    verdict_fn = create_autospec(get_verdict_for_execution, return_value=None)
    url_fn = create_autospec(
        get_dalton_frontend_url, return_value="https://dalton.example.com"
    )
    claim_fn = create_autospec(claim_target_delivery, return_value=True)
    mark_fn = create_autospec(mark_target_delivery, return_value=None)
    admin_fn = create_autospec(resolve_dalton_notify_enabled, return_value=True)

    posted_batches: list[tuple[list[str], str, dict[str, Any] | None]] = []

    async def _fake_post(
        send_targets: list[str], text: str, card: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        posted_batches.append((list(send_targets), text, card))
        return {"ok": True, "results": [{"target": t, "ok": True} for t in send_targets]}

    post_fn = create_autospec(post_bridge_notify, side_effect=_fake_post)

    # 1. Unrouted custom automation -> does not push even when user has personal targets
    fns_unrouted = _route_test_fns(targets=personal, routes={})
    pushed_unrouted = await on_automation_settled(
        automation=custom_auto,
        execution_id="exec-custom-unrouted",
        thread_id="th-custom-1",
        final_text="今日宏观早报：美债收益率回落，科技股情绪回暖。",
        resolve_enabled_fn=fns_unrouted[0],
        resolve_overrides_fn=fns_unrouted[1],
        resolve_targets_fn=fns_unrouted[2],
        resolve_routes_fn=fns_unrouted[3],
        is_admin_fn=admin_fn,
        get_verdict_fn=verdict_fn,
        claim_target_fn=claim_fn,
        mark_target_fn=mark_fn,
        post_notify_fn=post_fn,
        frontend_url_fn=url_fn,
    )
    assert pushed_unrouted is False
    assert posted_batches == []

    # 2. Custom automation routed to a Feishu group -> pushes on completion
    fns_routed = _route_test_fns(
        targets=personal,
        routes={"automation:a-custom-1": ["feishu:chat:oc_macro_group"]},
    )
    pushed_routed = await on_automation_settled(
        automation=custom_auto,
        execution_id="exec-custom-routed",
        thread_id="th-custom-2",
        final_text="今日宏观早报：美债收益率回落，科技股情绪回暖。",
        resolve_enabled_fn=fns_routed[0],
        resolve_overrides_fn=fns_routed[1],
        resolve_targets_fn=fns_routed[2],
        resolve_routes_fn=fns_routed[3],
        is_admin_fn=admin_fn,
        get_verdict_fn=verdict_fn,
        claim_target_fn=claim_fn,
        mark_target_fn=mark_fn,
        post_notify_fn=post_fn,
        frontend_url_fn=url_fn,
    )
    assert pushed_routed is True
    assert len(posted_batches) == 1
    sent_targets, sent_text, sent_card = posted_batches[0]
    assert sent_targets == ["feishu:chat:oc_macro_group"]
    assert "Morning Macro Digest · 定时任务" in sent_text
    assert sent_card is not None
    assert "Morning Macro Digest · 定时任务" in sent_card["header"]["title"]["content"]
    assert (
        sent_card["header"]["text_tag_list"][0]["text"]["content"] == "定时任务"
    )


@pytest.mark.asyncio
async def test_set_route_rejects_excluded_automations_and_test_requires_explicit_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 2 #2: excluded tasks (watch/delta/director-daily/data-health) are
    rejected by set_route with a reason; test without an explicit route refuses
    to send and asks the user to configure via set_route first."""
    import src.dalton.access_tiers as access_tiers_mod
    import src.dalton.tools.notify_tools as notify_tools_mod
    from src.dalton.tools.notify_tools import create_notify_tools
    from src.server.database import automation as auto_db

    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        auto_db,
        "list_all_automations",
        create_autospec(
            auto_db.list_all_automations,
            return_value=[
                {"automation_id": "a-w", "name": "dalton-nvda-watch"},
                {"automation_id": "a-d", "name": "dalton-nvda-delta"},
                {"automation_id": "a-dd", "name": "dalton-director-daily"},
                {"automation_id": "a-dh", "name": "dalton-data-health"},
                {"automation_id": "a-c", "name": "Morning Macro Digest"},
            ],
        ),
    )
    monkeypatch.setattr(
        notify_tools_mod,
        "resolve_dalton_notify_targets",
        create_autospec(
            resolve_dalton_notify_targets, return_value=["feishu:user:ou_me"]
        ),
    )
    monkeypatch.setattr(
        notify_tools_mod,
        "resolve_dalton_notify_target_labels",
        create_autospec(
            resolve_dalton_notify_target_labels,
            return_value={"feishu:user:ou_me": "我本人"},
        ),
    )
    monkeypatch.setattr(
        notify_tools_mod,
        "resolve_dalton_notify_routes",
        create_autospec(resolve_dalton_notify_routes, return_value={}),
    )
    confirm_spy = create_autospec(
        notify_tools_mod.hitl_confirm,
        side_effect=AssertionError("hitl_confirm must not be called"),
    )
    monkeypatch.setattr(notify_tools_mod, "hitl_confirm", confirm_spy)
    post_spy = create_autospec(
        post_bridge_notify,
        side_effect=AssertionError("post_bridge_notify must not be called"),
    )
    monkeypatch.setattr(dispatch_mod, "post_bridge_notify", post_spy)

    tool = create_notify_tools()[0]
    cfg = {"configurable": {"user_id": "u-internal"}}

    for excluded_name in (
        "dalton-nvda-watch",
        "dalton-nvda-delta",
        "dalton-director-daily",
        "dalton-data-health",
    ):
        cmd_ex = await tool.ainvoke(
            _notify_tool_call(
                {
                    "action": "set_route",
                    "automation": excluded_name,
                    "targets": ["我本人"],
                }
            ),
            config=cfg,
        )
        res_ex = json.loads(cmd_ex.update["messages"][0].content)
        assert res_ex["success"] is False
        assert "是系统后台内部任务，不支持设置推送目标" in res_ex["error"]

    # test action on a task without an explicit route refuses without sending
    cmd_test_unrouted = await tool.ainvoke(
        _notify_tool_call({"action": "test", "automation": "Morning Macro Digest"}),
        config=cfg,
    )
    res_test_unrouted = json.loads(cmd_test_unrouted.update["messages"][0].content)
    assert res_test_unrouted["success"] is False
    assert (
        res_test_unrouted["error"]
        == "该任务尚未单独设置推送目标（或当前已设为不推送），请先设置定时任务推送（action='set_route'）后再发送测试推送"
    )
    confirm_spy.assert_not_called()
    post_spy.assert_not_called()


@pytest.mark.asyncio
async def test_set_route_concurrent_modification_preserves_other_task_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 2 #3: if another task's route is modified while set_route is waiting
    for confirmation, set_route only submits the single modified task key so the
    other task's new route is preserved."""
    import src.dalton.access_tiers as access_tiers_mod
    import src.dalton.tools.notify_tools as notify_tools_mod
    from src.dalton.tools.notify_tools import create_notify_tools
    from src.server.database import automation as auto_db

    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        auto_db,
        "list_all_automations",
        create_autospec(
            auto_db.list_all_automations,
            return_value=[
                {"automation_id": "a-1", "name": "dalton-nvda-earn-2026q1-t0"},
                {"automation_id": "a-2", "name": "Morning Macro Digest"},
            ],
        ),
    )
    stored_prefs: dict[str, Any] = {
        "model_preference": {
            "dalton_notify_enabled": True,
            "dalton_notify_targets": ["feishu:user:ou_me_1"],
            "dalton_notify_target_labels": {"feishu:user:ou_me_1": "我本人"},
            "dalton_notify_overrides": {"Morning Macro Digest": False},
            "dalton_notify_routes": {"Morning Macro Digest": []},
        },
        "other_preference": {},
    }
    upserted_patches: list[dict[str, Any]] = []
    _mock_locked_prefs_db(monkeypatch, stored_prefs, upserted_patches)

    discovered_groups = [{"chat_id": "oc_g1", "name": "半导体投研群"}]
    monkeypatch.setattr(
        notify_tools_mod,
        "discover_feishu_notify_groups",
        create_autospec(
            groups_mod.discover_feishu_notify_groups,
            return_value=discovered_groups,
        ),
    )
    monkeypatch.setattr(
        groups_mod,
        "discover_feishu_notify_groups",
        create_autospec(
            groups_mod.discover_feishu_notify_groups,
            return_value=discovered_groups,
        ),
    )

    def _confirm_with_concurrent_route_edit(
        action_type: str, payload: dict[str, Any]
    ) -> tuple[bool, dict[str, Any]]:
        _ = action_type
        # Another writer updates Morning Macro Digest while this tool call is paused on HITL.
        stored_prefs["model_preference"]["dalton_notify_routes"][
            "Morning Macro Digest"
        ] = ["feishu:chat:oc_g1"]
        stored_prefs["model_preference"]["dalton_notify_overrides"][
            "Morning Macro Digest"
        ] = True
        return (
            True,
            {
                "decisions": [
                    {
                        "type": "approve",
                        "confirm_token": payload.get("confirm_token"),
                    }
                ]
            },
        )

    monkeypatch.setattr(
        notify_tools_mod,
        "hitl_confirm",
        create_autospec(
            notify_tools_mod.hitl_confirm,
            side_effect=_confirm_with_concurrent_route_edit,
        ),
    )

    tool = create_notify_tools()[0]
    cmd = await tool.ainvoke(
        _notify_tool_call(
            {
                "action": "set_route",
                "automation": "dalton-nvda-earn-2026q1-t0",
                "targets": ["我本人"],
            }
        ),
        config={"configurable": {"user_id": "u-internal"}},
    )
    res = json.loads(cmd.update["messages"][0].content)
    assert res["success"] is True

    # Only the single modified task key was submitted in the patch:
    assert len(upserted_patches) == 1
    assert upserted_patches[0] == {
        "dalton_notify_routes": {
            "dalton-nvda-earn-2026q1-t0": ["feishu:user:ou_me_1"]
        },
        "dalton_notify_overrides": {
            "dalton-nvda-earn-2026q1-t0": True
        },
    }
    # Deep merge preserved the concurrent change to Morning Macro Digest:
    assert stored_prefs["model_preference"]["dalton_notify_routes"] == {
        "Morning Macro Digest": ["feishu:chat:oc_g1"],
        "dalton-nvda-earn-2026q1-t0": ["feishu:user:ou_me_1"],
    }
    assert stored_prefs["model_preference"]["dalton_notify_overrides"] == {
        "Morning Macro Digest": True,
        "dalton-nvda-earn-2026q1-t0": True,
    }


@pytest.mark.asyncio
async def test_add_personal_target_consecutive_adds_under_lock_keep_both_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 2 #3: two consecutive/concurrent add_personal_target calls re-read
    under lock_user_preferences in the same transaction so both targets survive."""
    import src.dalton.access_tiers as access_tiers_mod
    import src.dalton.tools.notify_tools as notify_tools_mod
    from src.dalton.tools.notify_tools import create_notify_tools

    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, return_value="internal"),
    )
    stored_prefs: dict[str, Any] = {
        "model_preference": {
            "dalton_notify_targets": ["feishu:user:ou_base"],
            "dalton_notify_target_labels": {"feishu:user:ou_base": "基准用户"},
        },
        "other_preference": {},
    }
    upserted: list[dict[str, Any]] = []
    lock_spy = _mock_locked_prefs_db(monkeypatch, stored_prefs, upserted)

    monkeypatch.setattr(
        notify_tools_mod,
        "hitl_confirm",
        create_autospec(
            notify_tools_mod.hitl_confirm, side_effect=_approve_hitl_confirm
        ),
    )

    tool = create_notify_tools()[0]
    cfg = {"configurable": {"user_id": "u-internal"}}

    cmd1 = await tool.ainvoke(
        _notify_tool_call(
            {
                "action": "add_personal_target",
                "target": "feishu:user:ou_first",
                "label": "第一位",
            }
        ),
        config=cfg,
    )
    res1 = json.loads(cmd1.update["messages"][0].content)
    assert res1["success"] is True

    cmd2 = await tool.ainvoke(
        _notify_tool_call(
            {
                "action": "add_personal_target",
                "target": "feishu:user:ou_second",
                "label": "第二位",
            }
        ),
        config=cfg,
    )
    res2 = json.loads(cmd2.update["messages"][0].content)
    assert res2["success"] is True
    assert lock_spy.await_count == 2
    assert res2["personal_targets"] == [
        "feishu:user:ou_base",
        "feishu:user:ou_first",
        "feishu:user:ou_second",
    ]
    assert stored_prefs["model_preference"]["dalton_notify_targets"] == [
        "feishu:user:ou_base",
        "feishu:user:ou_first",
        "feishu:user:ou_second",
    ]
    assert stored_prefs["model_preference"]["dalton_notify_target_labels"] == {
        "feishu:user:ou_base": "基准用户",
        "feishu:user:ou_first": "第一位",
        "feishu:user:ou_second": "第二位",
    }


@pytest.mark.asyncio
async def test_custom_automation_routes_and_switches_keyed_by_id_same_name_and_case_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 #1: custom automations store route and override keys under
    automation:<automation_id>; ambiguous name on set_route refuses and lists
    candidates (name, automation_id, created_at); two same-named custom tasks
    and two tasks differing only by case do not affect each other."""
    import src.dalton.access_tiers as access_tiers_mod
    import src.dalton.tools.notify_tools as notify_tools_mod
    from src.dalton.tools.notify_tools import create_notify_tools
    from src.server.database import automation as auto_db

    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, return_value="internal"),
    )
    user_automations = [
        {
            "automation_id": "auto-dup-1",
            "name": "Morning Macro Digest",
            "created_at": "2026-11-01T08:00:00Z",
            "user_id": "u-internal",
        },
        {
            "automation_id": "auto-dup-2",
            "name": "Morning Macro Digest",
            "created_at": "2026-11-02T09:30:00Z",
            "user_id": "u-internal",
        },
        {
            "automation_id": "auto-case-upper",
            "name": "Macro Digest",
            "created_at": "2026-11-03T10:00:00Z",
            "user_id": "u-internal",
        },
        {
            "automation_id": "auto-case-lower",
            "name": "macro digest",
            "created_at": "2026-11-04T11:00:00Z",
            "user_id": "u-internal",
        },
    ]
    monkeypatch.setattr(
        auto_db,
        "list_all_automations",
        create_autospec(auto_db.list_all_automations, return_value=user_automations),
    )

    stored_prefs: dict[str, Any] = {
        "model_preference": {
            "dalton_notify_enabled": True,
            "dalton_notify_targets": ["feishu:user:ou_me_1"],
            "dalton_notify_target_labels": {"feishu:user:ou_me_1": "我本人"},
            # Legacy lowercase "False" override and name-keyed route must NOT suppress or cross-pollinate custom tasks.
            "dalton_notify_overrides": {
                "macro digest": False,
                "Morning Macro Digest": False,
            },
            "dalton_notify_routes": {},
        },
        "other_preference": {},
    }
    upserted: list[dict[str, Any]] = []
    _mock_locked_prefs_db(monkeypatch, stored_prefs, upserted)

    discovered_groups = [
        {"chat_id": "oc_g1", "name": "宏观投研群"},
        {"chat_id": "oc_g2", "name": "策略复盘群"},
    ]
    discover_mock = create_autospec(
        groups_mod.discover_feishu_notify_groups,
        return_value=discovered_groups,
    )
    monkeypatch.setattr(groups_mod, "discover_feishu_notify_groups", discover_mock)
    monkeypatch.setattr(notify_tools_mod, "discover_feishu_notify_groups", discover_mock)
    monkeypatch.setattr(
        notify_tools_mod,
        "hitl_confirm",
        create_autospec(
            notify_tools_mod.hitl_confirm, side_effect=_approve_hitl_confirm
        ),
    )

    tool = create_notify_tools()[0]
    cfg = {"configurable": {"user_id": "u-internal"}}

    # 1. Ambiguous name on set_route -> refuses without writing and lists candidate name, automation_id, created_at
    cmd_ambig = await tool.ainvoke(
        _notify_tool_call(
            {
                "action": "set_route",
                "automation": "Morning Macro Digest",
                "targets": ["宏观投研群"],
            },
            call_id="call_ambig_1",
        ),
        config=cfg,
    )
    res_ambig = json.loads(cmd_ambig.update["messages"][0].content)
    assert res_ambig["success"] is False
    assert "匹配到多个同名任务，请指定任务 ID（automation_id）" in res_ambig["error"]
    assert res_ambig["candidates"] == [
        {
            "name": "Morning Macro Digest",
            "automation_id": "auto-dup-1",
            "created_at": "2026-11-01T08:00:00Z",
        },
        {
            "name": "Morning Macro Digest",
            "automation_id": "auto-dup-2",
            "created_at": "2026-11-02T09:30:00Z",
        },
    ]
    assert upserted == []

    # 2. Configure only auto-dup-1 by automation_id -> stores under automation:auto-dup-1
    cmd_dup1 = await tool.ainvoke(
        _notify_tool_call(
            {
                "action": "set_route",
                "automation": "auto-dup-1",
                "targets": ["宏观投研群"],
            },
            call_id="call_dup1_set",
        ),
        config=cfg,
    )
    res_dup1 = json.loads(cmd_dup1.update["messages"][0].content)
    assert res_dup1["success"] is True
    assert res_dup1["automation_id"] == "auto-dup-1"
    assert res_dup1["route_key"] == "automation:auto-dup-1"
    assert stored_prefs["model_preference"]["dalton_notify_routes"] == {
        "automation:auto-dup-1": ["feishu:chat:oc_g1"],
    }
    assert (
        stored_prefs["model_preference"]["dalton_notify_overrides"][
            "automation:auto-dup-1"
        ]
        is True
    )

    # 3. Settlement of auto-dup-1 pushes; settlement of same-named auto-dup-2 does NOT push
    verdict_fn = create_autospec(get_verdict_for_execution, return_value=None)
    url_fn = create_autospec(
        get_dalton_frontend_url, return_value="https://dalton.example.com"
    )
    claim_fn = create_autospec(claim_target_delivery, return_value=True)
    mark_fn = create_autospec(mark_target_delivery, return_value=None)
    admin_fn = create_autospec(resolve_dalton_notify_enabled, return_value=True)
    posted_targets: list[list[str]] = []

    async def _fake_post(
        send_targets: list[str], text: str, card: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        _ = (text, card)
        posted_targets.append(list(send_targets))
        return {"ok": True, "results": [{"target": t, "ok": True} for t in send_targets]}

    post_fn = create_autospec(post_bridge_notify, side_effect=_fake_post)

    routes_now = dict(stored_prefs["model_preference"]["dalton_notify_routes"])
    overrides_now = dict(stored_prefs["model_preference"]["dalton_notify_overrides"])
    enabled_fn = create_autospec(resolve_dalton_notify_enabled, return_value=True)
    overrides_fn = create_autospec(
        resolve_dalton_notify_overrides, return_value=overrides_now
    )
    targets_fn = create_autospec(
        resolve_dalton_notify_targets, return_value=["feishu:user:ou_me_1"]
    )
    routes_fn = create_autospec(resolve_dalton_notify_routes, return_value=routes_now)

    pushed_1 = await on_automation_settled(
        automation=user_automations[0],
        execution_id="exec-dup-1",
        thread_id="th-dup-1",
        final_text="早报任务 1 完成。",
        resolve_enabled_fn=enabled_fn,
        resolve_overrides_fn=overrides_fn,
        resolve_targets_fn=targets_fn,
        resolve_routes_fn=routes_fn,
        is_admin_fn=admin_fn,
        get_verdict_fn=verdict_fn,
        claim_target_fn=claim_fn,
        mark_target_fn=mark_fn,
        post_notify_fn=post_fn,
        frontend_url_fn=url_fn,
    )
    assert pushed_1 is True
    assert posted_targets == [["feishu:chat:oc_g1"]]

    pushed_2 = await on_automation_settled(
        automation=user_automations[1],
        execution_id="exec-dup-2",
        thread_id="th-dup-2",
        final_text="同名早报任务 2 完成。",
        resolve_enabled_fn=enabled_fn,
        resolve_overrides_fn=overrides_fn,
        resolve_targets_fn=targets_fn,
        resolve_routes_fn=routes_fn,
        is_admin_fn=admin_fn,
        get_verdict_fn=verdict_fn,
        claim_target_fn=claim_fn,
        mark_target_fn=mark_fn,
        post_notify_fn=post_fn,
        frontend_url_fn=url_fn,
    )
    assert pushed_2 is False
    assert posted_targets == [["feishu:chat:oc_g1"]]

    # 4. Two tasks differing only by case ("Macro Digest" vs "macro digest"):
    #    Configure "Macro Digest" by exact name -> matches only auto-case-upper,
    #    writes automation:auto-case-upper, and is NOT suppressed by legacy "macro digest": False.
    cmd_upper = await tool.ainvoke(
        _notify_tool_call(
            {
                "action": "set_route",
                "automation": "Macro Digest",
                "targets": ["策略复盘群"],
            },
            call_id="call_upper_set",
        ),
        config=cfg,
    )
    res_upper = json.loads(cmd_upper.update["messages"][0].content)
    assert res_upper["success"] is True
    assert res_upper["automation_id"] == "auto-case-upper"
    assert res_upper["route_key"] == "automation:auto-case-upper"

    routes_case = dict(stored_prefs["model_preference"]["dalton_notify_routes"])
    overrides_case = dict(stored_prefs["model_preference"]["dalton_notify_overrides"])
    routes_case_fn = create_autospec(
        resolve_dalton_notify_routes, return_value=routes_case
    )
    overrides_case_fn = create_autospec(
        resolve_dalton_notify_overrides, return_value=overrides_case
    )

    posted_targets.clear()
    pushed_upper = await on_automation_settled(
        automation=user_automations[2],
        execution_id="exec-case-upper",
        thread_id="th-case-upper",
        final_text="Macro Digest 大写任务完成。",
        resolve_enabled_fn=enabled_fn,
        resolve_overrides_fn=overrides_case_fn,
        resolve_targets_fn=targets_fn,
        resolve_routes_fn=routes_case_fn,
        is_admin_fn=admin_fn,
        get_verdict_fn=verdict_fn,
        claim_target_fn=claim_fn,
        mark_target_fn=mark_fn,
        post_notify_fn=post_fn,
        frontend_url_fn=url_fn,
    )
    assert pushed_upper is True
    assert posted_targets == [["feishu:chat:oc_g2"]]

    pushed_lower = await on_automation_settled(
        automation=user_automations[3],
        execution_id="exec-case-lower",
        thread_id="th-case-lower",
        final_text="macro digest 小写任务完成。",
        resolve_enabled_fn=enabled_fn,
        resolve_overrides_fn=overrides_case_fn,
        resolve_targets_fn=targets_fn,
        resolve_routes_fn=routes_case_fn,
        is_admin_fn=admin_fn,
        get_verdict_fn=verdict_fn,
        claim_target_fn=claim_fn,
        mark_target_fn=mark_fn,
        post_notify_fn=post_fn,
        frontend_url_fn=url_fn,
    )
    assert pushed_lower is False
    assert posted_targets == [["feishu:chat:oc_g2"]]


@pytest.mark.asyncio
async def test_hitl_confirm_token_rejects_label_change_route_change_task_recreate_and_missing_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 #1: confirmation uses a deterministic SHA-256 confirm_token echoed back
    by the frontend decision (no Redis snapshot). Re-computing on resume rejects
    execution if a label target changed, a route changed, the task was renamed and a
    new task with the same name was created, or confirm_token is missing; unchanged
    content executes normally and reject does not execute."""
    import src.dalton.access_tiers as access_tiers_mod
    import src.dalton.tools.notify_tools as notify_tools_mod
    from src.dalton.tools.notify_tools import (
        _compute_confirm_token,
        create_notify_tools,
    )
    from src.server.database import automation as auto_db
    from src.tools.secretary._commands import hitl_confirm as real_hitl_confirm

    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, return_value="internal"),
    )
    automations_state: list[dict[str, Any]] = [
        {"automation_id": "a-snap-1", "name": "Morning Macro Digest"}
    ]
    monkeypatch.setattr(
        auto_db,
        "list_all_automations",
        create_autospec(
            auto_db.list_all_automations,
            side_effect=lambda _uid: list(automations_state),
        ),
    )

    stored_prefs: dict[str, Any] = {
        "model_preference": {
            "dalton_notify_enabled": True,
            "dalton_notify_targets": [
                "feishu:user:ou_zhang",
                "feishu:user:ou_imposter",
            ],
            "dalton_notify_target_labels": {
                "feishu:user:ou_zhang": "张三",
                "feishu:user:ou_imposter": "李四",
            },
            "dalton_notify_overrides": {},
            "dalton_notify_routes": {
                "automation:a-snap-1": ["feishu:chat:oc_g1"],
            },
        },
        "other_preference": {},
    }
    upserted: list[dict[str, Any]] = []
    _mock_locked_prefs_db(monkeypatch, stored_prefs, upserted)

    discovered_groups = [
        {"chat_id": "oc_g1", "name": "核心群1"},
        {"chat_id": "oc_g2", "name": "其他群2"},
    ]
    discover_mock = create_autospec(
        groups_mod.discover_feishu_notify_groups,
        return_value=discovered_groups,
    )
    monkeypatch.setattr(groups_mod, "discover_feishu_notify_groups", discover_mock)
    monkeypatch.setattr(notify_tools_mod, "discover_feishu_notify_groups", discover_mock)

    sent_targets: list[str] = []

    async def _fake_bridge_post(
        targets: list[str], text: str, card: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        _ = (text, card)
        sent_targets.extend(targets)
        return {"ok": True, "results": [{"target": t, "ok": True} for t in targets]}

    monkeypatch.setattr(
        dispatch_mod,
        "post_bridge_notify",
        create_autospec(post_bridge_notify, side_effect=_fake_bridge_post),
    )

    tool = create_notify_tools()[0]
    cfg = {"configurable": {"user_id": "u-internal"}}

    # 1. Waiting for set_route confirmation, label "张三" is reassigned to ou_imposter -> approve refuses write
    def _confirm_with_label_swap(
        action_type: str, payload: dict[str, Any]
    ) -> tuple[bool, dict[str, Any]]:
        _ = action_type
        assert payload["args"]["推送到"] == ["张三（个人，feishu:user:ou_zhang）"]
        expected_tok = _compute_confirm_token(
            action="set_route",
            user_id="u-internal",
            automation_id="a-snap-1",
            route_key="automation:a-snap-1",
            targets=["feishu:user:ou_zhang"],
        )
        assert payload["confirm_token"] == expected_tok
        # Reassign "张三" to ou_imposter while waiting for approval
        stored_prefs["model_preference"]["dalton_notify_target_labels"] = {
            "feishu:user:ou_zhang": "旧张三",
            "feishu:user:ou_imposter": "张三",
        }
        return (
            True,
            {"decisions": [{"type": "approve", "confirm_token": payload["confirm_token"]}]},
        )

    monkeypatch.setattr(
        notify_tools_mod,
        "hitl_confirm",
        create_autospec(real_hitl_confirm, side_effect=_confirm_with_label_swap),
    )
    cmd_label_changed = await tool.ainvoke(
        _notify_tool_call(
            {
                "action": "set_route",
                "automation": "Morning Macro Digest",
                "targets": ["张三"],
            },
            call_id="call_tok_label",
        ),
        config=cfg,
    )
    res_label_changed = json.loads(cmd_label_changed.update["messages"][0].content)
    assert res_label_changed["success"] is False
    assert (
        res_label_changed["error"]
        == "确认期间任务或推送目标发生变化（或确认校验未通过），已取消本次操作。当前状态：任务 Morning Macro Digest（任务 ID：a-snap-1）→ 目标：feishu:user:ou_imposter。如需继续请重新发起。"
    )
    assert "原目标" not in res_label_changed["error"]
    assert "原任务" not in res_label_changed["error"]
    assert res_label_changed["current_targets"] == ["feishu:user:ou_imposter"]
    assert "old_targets" not in res_label_changed
    assert upserted == []

    # 2. Waiting for test confirmation, route is changed from oc_g1 to oc_g2 -> approve refuses send
    def _confirm_with_route_swap(
        action_type: str, payload: dict[str, Any]
    ) -> tuple[bool, dict[str, Any]]:
        _ = action_type
        assert payload["args"]["推送到"] == ["核心群1（群，oc_g1）"]
        stored_prefs["model_preference"]["dalton_notify_routes"][
            "automation:a-snap-1"
        ] = ["feishu:chat:oc_g2"]
        return (
            True,
            {"decisions": [{"type": "approve", "confirm_token": payload["confirm_token"]}]},
        )

    monkeypatch.setattr(
        notify_tools_mod,
        "hitl_confirm",
        create_autospec(real_hitl_confirm, side_effect=_confirm_with_route_swap),
    )
    cmd_test_changed = await tool.ainvoke(
        _notify_tool_call(
            {
                "action": "test",
                "automation": "Morning Macro Digest",
            },
            call_id="call_tok_test",
        ),
        config=cfg,
    )
    res_test_changed = json.loads(cmd_test_changed.update["messages"][0].content)
    assert res_test_changed["success"] is False
    assert (
        res_test_changed["error"]
        == "确认期间任务或推送目标发生变化（或确认校验未通过），已取消本次操作。当前状态：任务 Morning Macro Digest（任务 ID：a-snap-1）→ 目标：feishu:chat:oc_g2。如需继续请重新发起。"
    )
    assert "原目标" not in res_test_changed["error"]
    assert "原任务" not in res_test_changed["error"]
    assert res_test_changed["current_targets"] == ["feishu:chat:oc_g2"]
    assert "old_targets" not in res_test_changed
    assert sent_targets == []

    # 3. Waiting for set_route confirmation, task is renamed and a new task with the same name is created -> approve refuses write
    def _confirm_with_task_rename_and_recreate(
        action_type: str, payload: dict[str, Any]
    ) -> tuple[bool, dict[str, Any]]:
        _ = action_type
        assert payload["automation_id"] == "a-snap-1"
        automations_state[:] = [
            {"automation_id": "a-snap-1", "name": "Archived Macro Digest"},
            {"automation_id": "a-snap-2", "name": "Morning Macro Digest"},
        ]
        return (
            True,
            {"decisions": [{"type": "approve", "confirm_token": payload["confirm_token"]}]},
        )

    monkeypatch.setattr(
        notify_tools_mod,
        "hitl_confirm",
        create_autospec(
            real_hitl_confirm, side_effect=_confirm_with_task_rename_and_recreate
        ),
    )
    cmd_task_recreated = await tool.ainvoke(
        _notify_tool_call(
            {
                "action": "set_route",
                "automation": "Morning Macro Digest",
                "targets": ["核心群1"],
            },
            call_id="call_tok_task_recreate",
        ),
        config=cfg,
    )
    res_task_recreated = json.loads(cmd_task_recreated.update["messages"][0].content)
    assert res_task_recreated["success"] is False
    assert (
        res_task_recreated["error"]
        == "确认期间任务或推送目标发生变化（或确认校验未通过），已取消本次操作。当前状态：任务 Morning Macro Digest（任务 ID：a-snap-2）→ 目标：feishu:chat:oc_g1。如需继续请重新发起。"
    )
    assert "原任务" not in res_task_recreated["error"]
    assert "原目标" not in res_task_recreated["error"]
    assert res_task_recreated["current_automation"]["automation_id"] == "a-snap-2"
    assert "old_automation" not in res_task_recreated
    assert upserted == []

    # Reset automations_state back to a-snap-1
    automations_state[:] = [
        {"automation_id": "a-snap-1", "name": "Morning Macro Digest"}
    ]

    # 4. Missing confirm_token in approve decision -> refuses execution for set_route, add_personal_target, and test
    monkeypatch.setattr(
        notify_tools_mod,
        "hitl_confirm",
        create_autospec(
            real_hitl_confirm,
            return_value=(True, {"decisions": [{"type": "approve"}]}),
        ),
    )
    for missing_tok_args in (
        {
            "action": "set_route",
            "automation": "Morning Macro Digest",
            "targets": ["核心群1"],
        },
        {
            "action": "add_personal_target",
            "target": "feishu:user:ou_new_person",
            "label": "王五",
        },
        {
            "action": "test",
            "automation": "Morning Macro Digest",
        },
    ):
        cmd_missing_tok = await tool.ainvoke(
            _notify_tool_call(missing_tok_args),
            config=cfg,
        )
        res_missing_tok = json.loads(cmd_missing_tok.update["messages"][0].content)
        assert res_missing_tok["success"] is False
        if missing_tok_args["action"] == "add_personal_target":
            assert (
                res_missing_tok["error"]
                == "确认校验未通过，已取消添加个人推送目标，请重新发起。"
            )
        else:
            assert (
                "确认期间任务或推送目标发生变化（或确认校验未通过），已取消本次操作。"
                in res_missing_tok["error"]
            )
    assert upserted == []
    assert sent_targets == []

    # 5. Unchanged content with matching confirm_token -> normal execution succeeds
    monkeypatch.setattr(
        notify_tools_mod,
        "hitl_confirm",
        create_autospec(real_hitl_confirm, side_effect=_approve_hitl_confirm),
    )
    cmd_unchanged = await tool.ainvoke(
        _notify_tool_call(
            {
                "action": "set_route",
                "automation": "Morning Macro Digest",
                "targets": ["张三", "核心群1"],
            },
            call_id="call_tok_ok",
        ),
        config=cfg,
    )
    res_unchanged = json.loads(cmd_unchanged.update["messages"][0].content)
    assert res_unchanged["success"] is True
    assert res_unchanged["targets"] == [
        "feishu:user:ou_imposter",
        "feishu:chat:oc_g1",
    ]
    assert len(upserted) == 1

    # 6. Reject -> does not execute
    upserted.clear()
    monkeypatch.setattr(
        notify_tools_mod,
        "hitl_confirm",
        create_autospec(
            real_hitl_confirm,
            return_value=(False, {"decisions": [{"type": "reject"}]}),
        ),
    )
    cmd_rejected = await tool.ainvoke(
        _notify_tool_call(
            {
                "action": "set_route",
                "automation": "Morning Macro Digest",
                "targets": ["核心群1"],
            },
            call_id="call_tok_reject",
        ),
        config=cfg,
    )
    assert "取消" in cmd_rejected.update["messages"][0].content
    assert upserted == []


@pytest.mark.asyncio
async def test_hitl_confirm_rerun_from_top_with_stale_token_reports_only_current_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 5 #3: test the real LangGraph resume behavior where the 1st invocation
    raises an interrupt at hitl_confirm, state changes while waiting, and the 2nd
    invocation re-runs the tool from the top carrying the 1st invocation's
    confirm_token. Asserts no write/send occurs and the error message contains only
    current content without fabricating 'original' task or targets."""
    import src.dalton.access_tiers as access_tiers_mod
    import src.dalton.tools.notify_tools as notify_tools_mod
    from src.dalton.tools.notify_tools import create_notify_tools
    from src.server.database import automation as auto_db
    from src.tools.secretary._commands import hitl_confirm as real_hitl_confirm

    class _PausedForConfirm(Exception):
        def __init__(self, payload: dict[str, Any]) -> None:
            super().__init__("paused for HITL confirm")
            self.payload = payload

    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(get_user_tier, return_value="internal"),
    )
    automations_state: list[dict[str, Any]] = [
        {"automation_id": "a-rerun-1", "name": "Morning Macro Digest"}
    ]
    monkeypatch.setattr(
        auto_db,
        "list_all_automations",
        create_autospec(
            auto_db.list_all_automations,
            side_effect=lambda _uid: list(automations_state),
        ),
    )

    stored_prefs: dict[str, Any] = {
        "model_preference": {
            "dalton_notify_enabled": True,
            "dalton_notify_targets": [
                "feishu:user:ou_zhang",
                "feishu:user:ou_imposter",
            ],
            "dalton_notify_target_labels": {
                "feishu:user:ou_zhang": "张三",
                "feishu:user:ou_imposter": "李四",
            },
            "dalton_notify_overrides": {},
            "dalton_notify_routes": {
                "automation:a-rerun-1": ["feishu:chat:oc_g1"],
            },
        },
        "other_preference": {},
    }
    upserted: list[dict[str, Any]] = []
    _mock_locked_prefs_db(monkeypatch, stored_prefs, upserted)

    discovered_groups = [
        {"chat_id": "oc_g1", "name": "群A"},
        {"chat_id": "oc_g2", "name": "群B"},
    ]
    discover_mock = create_autospec(
        groups_mod.discover_feishu_notify_groups,
        return_value=discovered_groups,
    )
    monkeypatch.setattr(groups_mod, "discover_feishu_notify_groups", discover_mock)
    monkeypatch.setattr(notify_tools_mod, "discover_feishu_notify_groups", discover_mock)

    sent_targets: list[str] = []

    async def _fake_bridge_post(
        targets: list[str], text: str, card: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        _ = (text, card)
        sent_targets.extend(targets)
        return {"ok": True, "results": [{"target": t, "ok": True} for t in targets]}

    monkeypatch.setattr(
        dispatch_mod,
        "post_bridge_notify",
        create_autospec(post_bridge_notify, side_effect=_fake_bridge_post),
    )

    tool = create_notify_tools()[0]
    cfg = {"configurable": {"user_id": "u-internal"}}

    def _raise_pause(action_type: str, payload: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        _ = action_type
        raise _PausedForConfirm(payload)

    # Case A: test action — 1st call pauses on route [oc_g1]; route changes to [oc_g2];
    # 2nd call re-runs from top carrying 1st call's confirm_token
    monkeypatch.setattr(
        notify_tools_mod,
        "hitl_confirm",
        create_autospec(real_hitl_confirm, side_effect=_raise_pause),
    )
    with pytest.raises(_PausedForConfirm) as paused_test:
        await tool.ainvoke(
            _notify_tool_call(
                {"action": "test", "automation": "Morning Macro Digest"},
                call_id="call_rerun_test",
            ),
            config=cfg,
        )
    old_test_token = paused_test.value.payload["confirm_token"]
    assert paused_test.value.payload["targets"] == ["feishu:chat:oc_g1"]

    # State changes while waiting for confirmation
    stored_prefs["model_preference"]["dalton_notify_routes"][
        "automation:a-rerun-1"
    ] = ["feishu:chat:oc_g2"]

    # 2nd call (resume): re-runs from top and brings back old_test_token
    monkeypatch.setattr(
        notify_tools_mod,
        "hitl_confirm",
        create_autospec(
            real_hitl_confirm,
            return_value=(
                True,
                {"decisions": [{"type": "approve", "confirm_token": old_test_token}]},
            ),
        ),
    )
    cmd_resume_test = await tool.ainvoke(
        _notify_tool_call(
            {"action": "test", "automation": "Morning Macro Digest"},
            call_id="call_rerun_test",
        ),
        config=cfg,
    )
    res_resume_test = json.loads(cmd_resume_test.update["messages"][0].content)
    assert res_resume_test["success"] is False
    assert sent_targets == []
    assert (
        res_resume_test["error"]
        == "确认期间任务或推送目标发生变化（或确认校验未通过），已取消本次操作。当前状态：任务 Morning Macro Digest（任务 ID：a-rerun-1）→ 目标：feishu:chat:oc_g2。如需继续请重新发起。"
    )
    assert "原目标" not in res_resume_test["error"]
    assert "原任务" not in res_resume_test["error"]
    assert "oc_g1" not in res_resume_test["error"]
    assert "old_targets" not in res_resume_test
    assert "old_automation" not in res_resume_test
    assert res_resume_test["current_targets"] == ["feishu:chat:oc_g2"]

    # Case B: set_route action — 1st call pauses on task a-rerun-1 and label "张三" -> ou_zhang;
    # while waiting, task is recreated as a-rerun-2 and label "张三" is reassigned to ou_imposter;
    # 2nd call re-runs from top carrying 1st call's confirm_token
    monkeypatch.setattr(
        notify_tools_mod,
        "hitl_confirm",
        create_autospec(real_hitl_confirm, side_effect=_raise_pause),
    )
    with pytest.raises(_PausedForConfirm) as paused_set:
        await tool.ainvoke(
            _notify_tool_call(
                {
                    "action": "set_route",
                    "automation": "Morning Macro Digest",
                    "targets": ["张三"],
                },
                call_id="call_rerun_set",
            ),
            config=cfg,
        )
    old_set_token = paused_set.value.payload["confirm_token"]
    assert paused_set.value.payload["automation_id"] == "a-rerun-1"
    assert paused_set.value.payload["targets"] == ["feishu:user:ou_zhang"]

    # Mutate task and label while waiting
    automations_state[:] = [
        {"automation_id": "a-rerun-1", "name": "Archived Macro Digest"},
        {"automation_id": "a-rerun-2", "name": "Morning Macro Digest"},
    ]
    stored_prefs["model_preference"]["dalton_notify_target_labels"] = {
        "feishu:user:ou_zhang": "旧张三",
        "feishu:user:ou_imposter": "张三",
    }

    # 2nd call (resume): re-runs from top and brings back old_set_token
    monkeypatch.setattr(
        notify_tools_mod,
        "hitl_confirm",
        create_autospec(
            real_hitl_confirm,
            return_value=(
                True,
                {"decisions": [{"type": "approve", "confirm_token": old_set_token}]},
            ),
        ),
    )
    cmd_resume_set = await tool.ainvoke(
        _notify_tool_call(
            {
                "action": "set_route",
                "automation": "Morning Macro Digest",
                "targets": ["张三"],
            },
            call_id="call_rerun_set",
        ),
        config=cfg,
    )
    res_resume_set = json.loads(cmd_resume_set.update["messages"][0].content)
    assert res_resume_set["success"] is False
    assert upserted == []
    assert (
        res_resume_set["error"]
        == "确认期间任务或推送目标发生变化（或确认校验未通过），已取消本次操作。当前状态：任务 Morning Macro Digest（任务 ID：a-rerun-2）→ 目标：feishu:user:ou_imposter。如需继续请重新发起。"
    )
    assert "原目标" not in res_resume_set["error"]
    assert "原任务" not in res_resume_set["error"]
    assert "a-rerun-1" not in res_resume_set["error"]
    assert "ou_zhang" not in res_resume_set["error"]
    assert "old_targets" not in res_resume_set
    assert "old_automation" not in res_resume_set
    assert res_resume_set["current_automation"] == {
        "name": "Morning Macro Digest",
        "automation_id": "a-rerun-2",
        "route_key": "automation:a-rerun-2",
    }
    assert res_resume_set["current_targets"] == ["feishu:user:ou_imposter"]




