"""Unit tests for the three-tier user identity (`src/dalton/access_tiers.py`).

All test doubles use `unittest.mock.create_autospec` and make no real network calls.
"""

from __future__ import annotations

from unittest.mock import create_autospec

import pytest

import src.dalton.settings as settings_mod
from src.dalton.access_tiers import get_user_tier
from src.server.database import user as user_db

PM_USER_ID = "11111111-1111-4111-8111-111111111111"
INTERNAL_USER_ID = "22222222-2222-4222-8222-222222222222"
EXTERNAL_USER_ID = "33333333-3333-4333-8333-333333333333"


@pytest.fixture(autouse=True)
def _roster(monkeypatch: pytest.MonkeyPatch):
    settings_mod.get_dalton_config.cache_clear()
    monkeypatch.setenv("DALTON_PM_USER_IDS", PM_USER_ID)
    monkeypatch.setenv(
        "DALTON_ALLOWED_EMAILS", "internal@example.com,pm@example.com"
    )
    yield
    settings_mod.get_dalton_config.cache_clear()


@pytest.mark.asyncio
async def test_admin_tier_from_pm_list_without_user_row(monkeypatch: pytest.MonkeyPatch) -> None:
    # No users-table row at all: PM-list membership alone decides admin.
    monkeypatch.setattr(
        user_db,
        "get_user",
        create_autospec(user_db.get_user, return_value=None),
    )
    assert await get_user_tier(PM_USER_ID) == "admin"


@pytest.mark.asyncio
async def test_internal_tier_for_allowed_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        user_db,
        "get_user",
        create_autospec(
            user_db.get_user,
            return_value={"user_id": INTERNAL_USER_ID, "email": "Internal@Example.com"},
        ),
    )
    assert await get_user_tier(INTERNAL_USER_ID) == "internal"


@pytest.mark.asyncio
async def test_external_tier_for_everyone_else(monkeypatch: pytest.MonkeyPatch) -> None:
    # Invite-signup synthetic domain is not in DALTON_ALLOWED_EMAILS -> external.
    monkeypatch.setattr(
        user_db,
        "get_user",
        create_autospec(
            user_db.get_user,
            return_value={"user_id": EXTERNAL_USER_ID, "email": "alice01@team.dalton.local"},
        ),
    )
    assert await get_user_tier(EXTERNAL_USER_ID) == "external"
    assert await get_user_tier("") == "external"
    assert await get_user_tier(None) == "external"


@pytest.mark.asyncio
async def test_lookup_failure_fails_closed_to_external(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        user_db,
        "get_user",
        create_autospec(user_db.get_user, side_effect=RuntimeError("db down")),
    )
    assert await get_user_tier(EXTERNAL_USER_ID) == "external"


@pytest.mark.asyncio
async def test_admin_roster_failure_falls_through_not_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising PM check must not grant admin; the email check still runs (here: internal)."""
    import src.dalton.access_tiers as access_tiers_mod

    monkeypatch.setattr(
        access_tiers_mod,
        "is_invite_pm",
        create_autospec(access_tiers_mod.is_invite_pm, side_effect=RuntimeError("roster read failed")),
    )
    monkeypatch.setattr(
        user_db,
        "get_user",
        create_autospec(
            user_db.get_user,
            return_value={"user_id": PM_USER_ID, "email": "internal@example.com"},
        ),
    )
    assert await get_user_tier(PM_USER_ID) == "internal"


@pytest.mark.asyncio
async def test_allowed_emails_failure_fails_closed_to_external(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.dalton.access_tiers as access_tiers_mod

    monkeypatch.setattr(
        access_tiers_mod,
        "get_dalton_allowed_emails",
        create_autospec(
            access_tiers_mod.get_dalton_allowed_emails, side_effect=RuntimeError("env read failed")
        ),
    )
    monkeypatch.setattr(
        user_db,
        "get_user",
        create_autospec(
            user_db.get_user,
            return_value={"user_id": INTERNAL_USER_ID, "email": "internal@example.com"},
        ),
    )
    assert await get_user_tier(INTERNAL_USER_ID) == "external"
