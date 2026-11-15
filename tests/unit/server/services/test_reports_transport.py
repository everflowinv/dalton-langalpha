"""A 404 from the platform means "no such report" only when the platform says
so; any other 404 is a platform without the route, which a withdrawal must
not read as a report already gone."""

from __future__ import annotations

from unittest.mock import AsyncMock, create_autospec, patch  # DALTON-HOOK(problem-reports-off): autospec doubles

import httpx
import pytest

from src.server.services import reports


def _client(status: int, body: dict) -> AsyncMock:
    client = AsyncMock()
    client.post.return_value = httpx.Response(status, json=body)
    return client


async def _withdraw_against(status: int, body: dict) -> reports.PlatformResult:
    with patch("src.server.utils.http_client.get_http_client", AsyncMock(return_value=_client(status, body))), \
         patch("src.server.dependencies.usage_limits.service_headers", return_value={}):
        return await reports._withdraw("rep1", "u1")


@pytest.mark.asyncio
async def test_a_report_the_platform_never_got_is_already_withdrawn():
    result = await _withdraw_against(404, {"detail": {"error": "report_not_found"}})
    assert result.status == "accepted"


@pytest.mark.asyncio
async def test_a_platform_without_the_route_leaves_the_withdrawal_undone():
    result = await _withdraw_against(404, {"detail": "Not Found"})
    assert result.status == "unavailable"


@pytest.mark.asyncio
async def test_a_call_that_outlives_its_deadline_is_unavailable(monkeypatch):
    # httpx bounds each phase, not the call; the whole call is bounded too.
    import asyncio

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)

    client = AsyncMock()
    client.post.side_effect = slow
    monkeypatch.setattr(reports, "_FORWARD_TIMEOUT_S", 0.05)
    with patch("src.server.utils.http_client.get_http_client", AsyncMock(return_value=client)), \
         patch("src.server.dependencies.usage_limits.service_headers", return_value={}):
        assert (await reports._withdraw("rep1", "u1")).status == "unavailable"


def test_a_capability_in_a_failed_turns_error_does_not_leave():
    from src.server.utils.secret_redactor import redact_capability_paths

    text = "fetch https://api.x/api/v1/wsfiles/g/GRANT1/a.png failed; see https://app.x/s/SHARE9"
    assert "GRANT1" not in redact_capability_paths(text) and "SHARE9" not in redact_capability_paths(text)
    assert redact_capability_paths("/chat/t/abc/assets/s/x") == "/chat/t/abc/assets/s/x"
    assert redact_capability_paths("(see https://app.x/s/SHARE9)") == "(see https://app.x/s/[redacted])"


@pytest.mark.asyncio
async def test_problem_reports_default_off_even_with_platform_and_auth_url(monkeypatch):
    """DALTON-HOOK(problem-reports-off): reports stay off by default unless DALTON_PROBLEM_REPORTS_ENABLED is set."""
    from uuid import uuid4

    from fastapi import BackgroundTasks, HTTPException

    from src.dalton import settings as dalton_settings
    from src.server.app import reports as reports_app
    from src.server.models.report import ReportClient, ReportCreate, ReportError
    from src.server.services import feedback as feedback_svc

    monkeypatch.delenv("DALTON_PROBLEM_REPORTS_ENABLED", raising=False)
    monkeypatch.setattr(reports, "HOST_MODE", "platform")
    monkeypatch.setattr(reports, "AUTH_SERVICE_URL", "https://auth.example.test")
    dalton_settings.get_dalton_config.cache_clear()

    assert reports.reports_enabled() is False
    cfg = await reports_app.get_reports_config("u1")
    assert cfg.enabled is False

    body = ReportCreate(
        report_id=uuid4(),
        event="error_report",
        source="error_banner",
        error=ReportError(type="internal_error", message="boom"),
        client=ReportClient(surface="web"),
    )
    with pytest.raises(HTTPException) as exc:
        await reports_app.create_report(body, "u1", BackgroundTasks())
    assert exc.value.status_code == 404

    from src.server.database.conversation import upsert_feedback as real_upsert_feedback
    from src.server.database.conversation.feedback import get_feedback_for_turn as real_get_feedback_for_turn

    report_id = uuid4()
    fb_row = {
        "conversation_feedback_id": "fb-1",
        "conversation_response_id": "resp-1",
        "user_id": "u1",
        "rating": "thumbs_down",
        "issue_categories": ["inaccurate"],
        "comment": "bad",
        "consent_human_review": False,
        "reported_at": None,
        "report_id": str(report_id),
    }
    mock_get_fb = create_autospec(real_get_feedback_for_turn, return_value=None)
    mock_upsert_fb = create_autospec(real_upsert_feedback, return_value=fb_row)
    mock_send_off = create_autospec(feedback_svc._send_report)
    mock_withdraw = create_autospec(feedback_svc._withdraw_report)
    with patch.object(feedback_svc, "get_feedback_for_turn", mock_get_fb), \
         patch.object(feedback_svc, "upsert_feedback", mock_upsert_fb), \
         patch.object(feedback_svc, "_send_report", mock_send_off), \
         patch.object(feedback_svc, "_withdraw_report", mock_withdraw):
        row, report_status = await feedback_svc.submit(
            thread_id="t-1",
            turn_index=0,
            user_id="u1",
            rating="thumbs_down",
            issue_categories=["inaccurate"],
            comment="bad",
            consent_human_review=False,
            client=None,
        )
    assert row["rating"] == "thumbs_down"
    assert report_status is None
    mock_send_off.assert_not_called()

    monkeypatch.setenv("DALTON_PROBLEM_REPORTS_ENABLED", "1")
    dalton_settings.get_dalton_config.cache_clear()
    assert reports.reports_enabled() is True
    assert (await reports_app.get_reports_config("u1")).enabled is True

    mock_send_on = create_autospec(
        feedback_svc._send_report,
        return_value=(reports.PlatformResult("accepted"), fb_row),
    )
    with patch.object(feedback_svc, "get_feedback_for_turn", mock_get_fb), \
         patch.object(feedback_svc, "upsert_feedback", mock_upsert_fb), \
         patch.object(feedback_svc, "_send_report", mock_send_on), \
         patch.object(feedback_svc, "_withdraw_report", mock_withdraw):
        row_on, report_status_on = await feedback_svc.submit(
            thread_id="t-1",
            turn_index=0,
            user_id="u1",
            rating="thumbs_down",
            issue_categories=["inaccurate"],
            comment="bad",
            consent_human_review=False,
            client=None,
        )
    assert row_on["report_id"] == str(report_id)
    assert report_status_on == "accepted"
    mock_send_on.assert_awaited_once()
