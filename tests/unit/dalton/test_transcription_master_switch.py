"""Unit tests for the A1 transcription master switch (`DALTON_TRANSCRIPTION_ENABLED`).

Default-off contract: with the switch off the `prepare_transcription` tool is
not registered for either role, the `dalton-transcript-polish` skill is hidden,
background reconciliation refuses to run, and `/api/v1/dalton/transcriptions*`
answers 404 as if the feature did not exist. The tool additionally requires
the transcription store (`DALTON_ASR_STORAGE_*`); the global `STORAGE_*` store
stays out of the decision, and the skill's `hidden` flag looks at the env
switch only.
"""

from __future__ import annotations

from unittest.mock import create_autospec

import httpx
import pytest
from fastapi import FastAPI

from ptc_agent.agent.middleware.skills.dalton_registry import (
    _HIDDEN_DALTON_SKILLS,
    build_dalton_skill_definitions,
)
from src.dalton.app.transcriptions import router as transcriptions_router
from src.dalton.services import transcriptions as svc_transcriptions
from src.dalton.tools import build_dalton_tools

_ROLES = ("analyst", "chief_of_staff")


def _set_storage(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setattr(
        svc_transcriptions,
        "is_transcription_storage_configured",
        create_autospec(
            svc_transcriptions.is_transcription_storage_configured,
            return_value=enabled,
        ),
    )


def _switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DALTON_TRANSCRIPTION_ENABLED", raising=False)


# ---------------------------------------------------------------------------
# 1. `is_transcription_enabled`: env parsing and the storage conjunction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "Yes", "ON"])
def test_switch_truthy_values_enable_transcription_when_storage_on(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("DALTON_TRANSCRIPTION_ENABLED", value)
    _set_storage(monkeypatch, True)
    assert svc_transcriptions.is_transcription_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "2", "yes please", "enabled"])
def test_switch_non_truthy_values_keep_transcription_disabled(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("DALTON_TRANSCRIPTION_ENABLED", value)
    _set_storage(monkeypatch, True)
    assert svc_transcriptions.is_transcription_enabled() is False


def test_unset_switch_disables_transcription_even_with_storage_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_off(monkeypatch)
    _set_storage(monkeypatch, True)
    assert svc_transcriptions.is_transcription_enabled() is False


def test_switch_on_without_storage_disables_transcription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_TRANSCRIPTION_ENABLED", "1")
    _set_storage(monkeypatch, False)
    assert svc_transcriptions.is_transcription_enabled() is False


def test_switch_on_with_transcription_storage_configured_but_global_storage_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate is the transcription store alone: `DALTON_ASR_STORAGE_*` set and
    every global `STORAGE_*` var absent still enables transcription."""
    monkeypatch.setenv("DALTON_TRANSCRIPTION_ENABLED", "1")
    for var in (
        "STORAGE_BUCKET_NAME",
        "STORAGE_ENDPOINT_URL",
        "STORAGE_ACCESS_KEY_ID",
        "STORAGE_SECRET_ACCESS_KEY",
        "S3_BUCKET_NAME",
        "S3_ENDPOINT_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DALTON_ASR_STORAGE_ENDPOINT_URL", "http://host.docker.internal:9000")
    monkeypatch.setenv("DALTON_ASR_STORAGE_BUCKET_NAME", "asr-bucket")
    monkeypatch.setenv("DALTON_ASR_STORAGE_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("DALTON_ASR_STORAGE_SECRET_ACCESS_KEY", "sk")
    assert svc_transcriptions.is_transcription_enabled() is True


def test_switch_on_with_transcription_storage_unconfigured_disables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No stand-in here: with the `DALTON_ASR_STORAGE_*` vars absent the real
    `is_transcription_storage_configured()` must keep the switch off."""
    monkeypatch.setenv("DALTON_TRANSCRIPTION_ENABLED", "1")
    for var in (
        "DALTON_ASR_STORAGE_ENDPOINT_URL",
        "DALTON_ASR_STORAGE_SIGNING_ENDPOINT_URL",
        "DALTON_ASR_STORAGE_BUCKET_NAME",
        "DALTON_ASR_STORAGE_ACCESS_KEY_ID",
        "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    assert svc_transcriptions.is_transcription_enabled() is False


# ---------------------------------------------------------------------------
# 2. Tool registration (`build_dalton_tools`)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", _ROLES)
def test_switch_off_tool_not_registered(role: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _switch_off(monkeypatch)
    _set_storage(monkeypatch, True)
    names = {t.name for t in build_dalton_tools(role)}
    assert "prepare_transcription" not in names


@pytest.mark.parametrize("role", _ROLES)
def test_switch_on_storage_off_tool_not_registered(
    role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DALTON_TRANSCRIPTION_ENABLED", "1")
    _set_storage(monkeypatch, False)
    names = {t.name for t in build_dalton_tools(role)}
    assert "prepare_transcription" not in names


@pytest.mark.parametrize("role", _ROLES)
def test_switch_on_storage_on_tool_registered(
    role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DALTON_TRANSCRIPTION_ENABLED", "1")
    _set_storage(monkeypatch, True)
    names = {t.name for t in build_dalton_tools(role)}
    assert "prepare_transcription" in names


# ---------------------------------------------------------------------------
# 3. Skill visibility (`build_dalton_skill_definitions`) — env switch only
# ---------------------------------------------------------------------------


def test_switch_off_transcript_polish_skill_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch_off(monkeypatch)
    defs = build_dalton_skill_definitions()
    assert defs["dalton-transcript-polish"].hidden is True
    # No other skill changes visibility: only the always-hidden set plus this one.
    hidden_names = {name for name, d in defs.items() if d.hidden}
    assert hidden_names == _HIDDEN_DALTON_SKILLS | {"dalton-transcript-polish"}


def test_switch_on_transcript_polish_skill_visible_without_storage_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The skill gate reads the env switch only: storage off must not hide it
    (the registry builds at import time and must not probe storage config)."""
    monkeypatch.setenv("DALTON_TRANSCRIPTION_ENABLED", "1")
    _set_storage(monkeypatch, False)
    defs = build_dalton_skill_definitions()
    assert defs["dalton-transcript-polish"].hidden is False


# ---------------------------------------------------------------------------
# 4. Background reconciliation (`is_transcription_reconciliation_enabled`)
# ---------------------------------------------------------------------------


def test_switch_off_reconciliation_disabled_even_with_reconcile_env_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_off(monkeypatch)
    _set_storage(monkeypatch, True)
    monkeypatch.setenv("DALTON_TRANSCRIPTION_RECONCILE_ENABLED", "1")
    assert svc_transcriptions.is_transcription_reconciliation_enabled() is False


def test_storage_off_reconciliation_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DALTON_TRANSCRIPTION_ENABLED", "1")
    _set_storage(monkeypatch, False)
    assert svc_transcriptions.is_transcription_reconciliation_enabled() is False


def test_switch_on_reconciliation_enabled_and_independently_killable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_TRANSCRIPTION_ENABLED", "1")
    _set_storage(monkeypatch, True)
    assert svc_transcriptions.is_transcription_reconciliation_enabled() is True

    monkeypatch.setenv("DALTON_TRANSCRIPTION_RECONCILE_ENABLED", "0")
    assert svc_transcriptions.is_transcription_reconciliation_enabled() is False


# ---------------------------------------------------------------------------
# 5. API surface: 404 with the switch off (feature-absent semantics)
# ---------------------------------------------------------------------------

_SWITCH_OFF_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("POST", "/api/v1/dalton/transcriptions"),
    ("GET", "/api/v1/dalton/transcriptions/usage"),
    ("GET", "/api/v1/dalton/transcriptions/job-1"),
    ("GET", "/api/v1/dalton/transcriptions/job-1/result"),
    ("GET", "/api/v1/dalton/transcriptions/job-1/result/raw"),
    ("POST", "/api/v1/dalton/transcriptions/job-1/upload/start"),
    ("POST", "/api/v1/dalton/transcriptions/job-1/upload/part"),
    ("POST", "/api/v1/dalton/transcriptions/job-1/upload/complete"),
    ("POST", "/api/v1/dalton/transcriptions/job-1/upload/abort"),
    ("POST", "/api/v1/dalton/transcriptions/job-1/retry"),
    ("POST", "/api/v1/dalton/transcriptions/job-1/cancel"),
)


@pytest.mark.asyncio
async def test_switch_off_every_transcription_endpoint_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_off(monkeypatch)
    _set_storage(monkeypatch, True)

    app = FastAPI()
    app.include_router(transcriptions_router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for method, path in _SWITCH_OFF_ENDPOINTS:
            resp = await client.request(method, path, json={})
            assert resp.status_code == 404, f"{method} {path} -> {resp.status_code}"
            assert resp.json()["detail"] == "Transcription is not enabled"


@pytest.mark.asyncio
async def test_switch_off_answers_404_before_parameter_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed request to a switched-off deployment is 404, not 422."""
    _switch_off(monkeypatch)
    _set_storage(monkeypatch, True)

    app = FastAPI()
    app.include_router(transcriptions_router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        bad_query = await client.get("/api/v1/dalton/transcriptions/usage", params={"limit": "abc"})
        bad_body = await client.post(
            "/api/v1/dalton/transcriptions", json={"source_size_bytes": "not-a-number"}
        )
    for resp in (bad_query, bad_body):
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Transcription is not enabled"


@pytest.mark.asyncio
async def test_switch_on_api_passes_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With switch and storage on the same POST reaches validation (400 on a
    body with no job fields), proving the 404 gate is the switch's alone."""
    from src.server.utils.api import get_current_user_id

    monkeypatch.setenv("DALTON_TRANSCRIPTION_ENABLED", "1")
    _set_storage(monkeypatch, True)
    monkeypatch.setattr(
        svc_transcriptions,
        "get_user_tier",
        create_autospec(svc_transcriptions.get_user_tier, return_value="internal"),
    )

    app = FastAPI()
    app.include_router(transcriptions_router)
    app.dependency_overrides[get_current_user_id] = lambda: "u-switch-1"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/dalton/transcriptions", json={"submit": True})
        assert resp.status_code == 400
        assert "Either job_id or" in resp.json()["detail"]
