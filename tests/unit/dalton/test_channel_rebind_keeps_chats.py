"""A rebind must not drop the user's registered group chats (2026-11-10 incident)."""
from unittest.mock import create_autospec

import psycopg
import pytest

from src.dalton.database.channel_gateway import _ensure_platform_default_settings


def _cursor(row):
    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    cur.fetchone.return_value = row
    return cur


def _update_payload(cur):
    updates = [c for c in cur.execute.call_args_list if "UPDATE dalton_channel_user_settings" in c.args[0]]
    return updates


@pytest.mark.asyncio
async def test_rebind_keeps_registered_chats():
    chats = {"feishu:oc_x": {"name": "Market Alert", "workspace_id": None}}
    row = {"version": "v5", "apps": {"feishu": {"preferred": "feishu:@me", "chats": chats}}}
    cur = _cursor(row)
    await _ensure_platform_default_settings(cur, "u1", "feishu")
    updates = _update_payload(cur)
    assert len(updates) == 1
    version, apps, uid = updates[0].args[1]
    assert version == "v6" and uid == "u1"
    assert apps.obj["feishu"]["chats"] == chats
    assert apps.obj["feishu"]["agent_messages"]["allowed"] == ["feishu:@me"]


@pytest.mark.asyncio
async def test_rebind_with_complete_settings_writes_nothing():
    full = {
        "preferred": "feishu:@me",
        "chats": {"feishu:oc_x": {"name": "A", "workspace_id": None}},
        "automation_output": {},
        "agent_messages": {"enabled": True, "allowed": ["feishu:@me"]},
    }
    cur = _cursor({"version": "v2", "apps": {"feishu": full}})
    await _ensure_platform_default_settings(cur, "u1", "feishu")
    assert _update_payload(cur) == []


@pytest.mark.asyncio
async def test_first_bind_of_platform_gets_defaults():
    cur = _cursor({"version": "v1", "apps": {"discord": {"chats": {}}}})
    await _ensure_platform_default_settings(cur, "u1", "feishu")
    version, apps, _ = _update_payload(cur)[0].args[1]
    assert apps.obj["feishu"]["chats"] == {} and "discord" in apps.obj


@pytest.mark.asyncio
async def test_rebind_to_another_account_keeps_chats_but_resets_personal_routing():
    chats = {"feishu:oc_x": {"name": "Market Alert", "workspace_id": None}}
    old = {
        "preferred": "feishu:user:ou_old",
        "chats": chats,
        "automation_output": {"a1": "feishu:oc_x"},
        "agent_messages": {"enabled": True, "allowed": ["feishu:user:ou_old"]},
    }
    cur = _cursor({"version": "v3", "apps": {"feishu": old}})
    await _ensure_platform_default_settings(cur, "u1", "feishu", same_account=False)
    _, apps, _ = _update_payload(cur)[0].args[1]
    feishu = apps.obj["feishu"]
    assert feishu["chats"] == chats
    assert feishu["automation_output"] == {"a1": "feishu:oc_x"}
    assert feishu["preferred"] == "feishu:@me"
    assert feishu["agent_messages"]["allowed"] == ["feishu:@me"]
