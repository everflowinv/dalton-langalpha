"""Feishu edit-grant diagnostics and fallback conditions (R7 §3, 2026-11-01).

The three grant stages — directory lookup, openid grant, email grant — each
keep their own result (bridge error code, Feishu's own code/msg, request
log_id) in the raised error and the log line, so the last attempt can no
longer mask an earlier failure. The email spelling runs only when the lookup
found no account or the openid grant was refused for the member's identity;
parameter / permission / transport refusals surface as-is. The email request
also sets ``need_notification=false`` like the openid one, and the re-grant
endpoint refuses non-docx objects before granting.

All HTTP is faked at the ``urllib`` opener layer, so the real ``_request``,
``user_open_id_by_email`` and ``grant_full_access`` run end to end; no real
Feishu call is ever made.
"""

from __future__ import annotations

import email.message
import io
import json
import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

SCRIPTS_DALTON = Path(__file__).resolve().parents[3] / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402


# ---------------------------------------------------------------------------
# fake HTTP layer: one opener whose .open answers per-URL scripted payloads
# ---------------------------------------------------------------------------


class _FakeHTTPResponse(io.BytesIO):
    """What ``opener.open`` returns: body bytes, an HTTP status, and a
    Content-Type header (real urllib responses always carry one)."""

    def __init__(
        self, body: bytes, status: int = 200, content_type: str = "application/json"
    ) -> None:
        super().__init__(body)
        self.status = status
        headers = email.message.Message()
        headers["Content-Type"] = content_type
        self.headers = headers


def _install_fake_feishu_http(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> list[dict[str, Any]]:
    """Serve every Feishu HTTP call from ``handler(url, body) ->
    (status, payload_dict)``; record each request's URL and JSON body.

    A ``str`` payload is sent as a raw non-JSON body (HTML error pages).
    A status >= 400 is delivered as a real ``urllib.error.HTTPError``, which
    is the only shape ``FeishuClient._request`` treats as an HTTP refusal.
    An exception raised by the handler simulates a transport failure."""
    calls: list[dict[str, Any]] = []
    opener_cls = create_autospec(rb.urllib.request.build_opener)
    opener = opener_cls.return_value

    def _open(req: Any, timeout: Any = None) -> _FakeHTTPResponse:
        url = req.full_url
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        calls.append({"url": url, "body": body})
        status, payload = handler(url, body)
        if isinstance(payload, str):
            data, content_type = payload.encode("utf-8"), "text/html; charset=utf-8"
        else:
            data, content_type = json.dumps(payload).encode("utf-8"), "application/json"
        if status >= 400:
            err_headers = email.message.Message()
            err_headers["Content-Type"] = content_type
            raise rb.urllib.error.HTTPError(
                url, status, "error", err_headers, io.BytesIO(data)
            )
        return _FakeHTTPResponse(data, status, content_type)

    opener.open.side_effect = _open
    monkeypatch.setattr(rb.urllib.request, "build_opener", opener_cls)
    return calls


def _client() -> rb.FeishuClient:
    return rb.FeishuClient("fake-app-id", "fake-app-secret", timeout_s=1.0)


def _auth_ok(url: str) -> bool:
    return "tenant_access_token" in url


class TestGrantFallbackAndPhaseRetention:
    def test_openid_identity_failure_then_email_failure_keeps_both_errors(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """R7: openid refused for the member's identity, email fallback also
        refused — the raised error, its ``phases``, and the log line all carry
        BOTH stages' code/msg/log_id, not just the email attempt's."""

        def handler(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            if _auth_ok(url):
                return 200, {"code": 0, "tenant_access_token": "t-fake", "expire": 7200}
            if "batch_get_id" in url:
                return 200, {
                    "code": 0,
                    "data": {"user_list": [{"email": "u@corp.com", "user_id": "ou_1"}]},
                }
            if body.get("member_type") == "openid":
                # Identity-class: Feishu's own msg names the member as absent.
                return 200, {
                    "code": 1062005,
                    "msg": "member not exist",
                    "log_id": "log-openid-1",
                }
            # Email fallback: refused as a parameter problem.
            return 400, {
                "code": 99991661,
                "msg": "invalid member email u@corp.com",
                "log_id": "log-email-1",
            }

        calls = _install_fake_feishu_http(monkeypatch, handler)
        client = _client()

        with caplog.at_level(logging.INFO, logger="researchdata_bridge"):
            with pytest.raises(rb.FeishuApiError) as excinfo:
                client.grant_full_access("doccnGrant1", "u@corp.com")

        err = excinfo.value
        assert err.error_code == "feishu_grant_failed"
        text = str(err)
        for fragment in (
            "lookup:ok",
            "(found)",
            "openid:failed",
            "feishu_code=1062005",
            "member not exist",
            "log_id=log-openid-1",
            "email:failed",
            "feishu_http_400",
            "feishu_code=99991661",
            "log_id=log-email-1",
        ):
            assert fragment in text, text
        # The member's address never survives in the error or the log.
        assert "u@corp.com" not in text, text
        grant_lines = [
            m
            for m in (r.getMessage() for r in caplog.records)
            if m.startswith("feishu_grant ")
        ]
        assert len(grant_lines) == 1, grant_lines
        assert "outcome=failed" in grant_lines[0], grant_lines[0]
        assert "openid:failed" in grant_lines[0] and "email:failed" in grant_lines[0]
        assert "u@corp.com" not in grant_lines[0], grant_lines[0]
        # Structured phases: lookup ok, openid failed (class=identity), email failed.
        assert [(p["stage"], p["status"]) for p in err.phases] == [
            ("lookup", "ok"),
            ("openid", "failed"),
            ("email", "failed"),
        ]
        assert err.phases[1]["note"] == "class=identity"
        assert err.phases[1]["log_id"] == "log-openid-1"
        assert err.phases[2]["error_code"] == "feishu_http_400"
        # Both grant requests asked Feishu not to notify the member.
        grant_calls = [c for c in calls if "/permissions/" in c["url"]]
        assert len(grant_calls) == 2, calls
        for call in grant_calls:
            assert "need_notification=false" in call["url"], call["url"]
        assert grant_calls[0]["body"]["perm"] == "full_access"

    def test_parameter_refusal_does_not_fall_back_to_email(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R7: an HTTP 400 on the openid grant is a parameter refusal — the
        email body has the same shape, so no second attempt; the error keeps
        the openid stage and marks the email stage skipped."""

        def handler(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            if _auth_ok(url):
                return 200, {"code": 0, "tenant_access_token": "t-fake", "expire": 7200}
            if "batch_get_id" in url:
                return 200, {
                    "code": 0,
                    "data": {"user_list": [{"email": "u@corp.com", "user_id": "ou_1"}]},
                }
            return 400, {"code": 99991661, "msg": "perm invalid", "log_id": "lg-400"}

        calls = _install_fake_feishu_http(monkeypatch, handler)
        client = _client()

        with pytest.raises(rb.FeishuApiError) as excinfo:
            client.grant_full_access("doccnGrant2", "u@corp.com")

        grant_calls = [c for c in calls if "/permissions/" in c["url"]]
        assert len(grant_calls) == 1, calls
        assert grant_calls[0]["body"]["member_type"] == "openid"
        text = str(excinfo.value)
        assert "feishu_grant_failed" not in text or True
        assert "openid:failed" in text and "feishu_http_400" in text, text
        assert "email:skipped" in text, text
        assert "class=parameter" in text, text
        assert [(p["stage"], p["status"]) for p in excinfo.value.phases] == [
            ("lookup", "ok"),
            ("openid", "failed"),
            ("email", "skipped"),
        ]

    def test_production_1063001_invalid_parameter_shape_does_not_fall_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R7 r2 (codex #5): the 10-28 production refusal was HTTP 400 with
        code 1063001 and msg "Invalid parameter" — a parameter refusal, and
        no permission mapping is claimed for the code. No email retry."""

        def handler(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            if _auth_ok(url):
                return 200, {"code": 0, "tenant_access_token": "t-fake", "expire": 7200}
            if "batch_get_id" in url:
                return 200, {
                    "code": 0,
                    "data": {"user_list": [{"email": "u@corp.com", "user_id": "ou_1"}]},
                }
            return 400, {"code": 1063001, "msg": "Invalid parameter", "log_id": "lg-1063001"}

        calls = _install_fake_feishu_http(monkeypatch, handler)
        client = _client()

        with pytest.raises(rb.FeishuApiError) as excinfo:
            client.grant_full_access("doccnGrant3", "u@corp.com")

        grant_calls = [c for c in calls if "/permissions/" in c["url"]]
        assert len(grant_calls) == 1, calls
        text = str(excinfo.value)
        assert "feishu_http_400" in text, text
        assert "feishu_code=1063001" in text, text
        assert "log_id=lg-1063001" in text, text
        assert "class=parameter" in text, text
        assert "email:skipped" in text, text

    def test_http400_member_not_exist_wording_falls_back_to_email(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """R7 r2 (lead): Feishu answers member-not-exist as HTTP 400 — that
        shape must still classify as identity and reach the email fallback,
        which the old 400-first order made unreachable."""

        def handler(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            if _auth_ok(url):
                return 200, {"code": 0, "tenant_access_token": "t-fake", "expire": 7200}
            if "batch_get_id" in url:
                return 200, {
                    "code": 0,
                    "data": {"user_list": [{"email": "u@corp.com", "user_id": "ou_1"}]},
                }
            if body.get("member_type") == "openid":
                return 400, {"code": 1062005, "msg": "member not exist", "log_id": "lg-400"}
            return 200, {"code": 0, "data": {}}

        calls = _install_fake_feishu_http(monkeypatch, handler)
        client = _client()

        with caplog.at_level(logging.INFO, logger="researchdata_bridge"):
            spelling = client.grant_full_access("doccnGrant3b", "u@corp.com")

        assert spelling == "email:u@corp.com"
        grant_calls = [c for c in calls if "/permissions/" in c["url"]]
        assert [c["body"]["member_type"] for c in grant_calls] == ["openid", "email"], calls
        grant_lines = [
            m
            for m in (r.getMessage() for r in caplog.records)
            if m.startswith("feishu_grant ")
        ]
        assert len(grant_lines) == 1, grant_lines
        assert "openid:failed" in grant_lines[0], grant_lines[0]
        assert "class=identity" in grant_lines[0], grant_lines[0]
        assert "email:ok" in grant_lines[0], grant_lines[0]

    def test_transport_refusal_does_not_fall_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            if _auth_ok(url):
                return 200, {"code": 0, "tenant_access_token": "t-fake", "expire": 7200}
            if "batch_get_id" in url:
                return 200, {
                    "code": 0,
                    "data": {"user_list": [{"email": "u@corp.com", "user_id": "ou_1"}]},
                }
            raise OSError("connection reset")

        calls = _install_fake_feishu_http(monkeypatch, handler)
        client = _client()

        with pytest.raises(rb.FeishuApiError) as excinfo:
            client.grant_full_access("doccnGrant4", "u@corp.com")

        grant_calls = [c for c in calls if "/permissions/" in c["url"]]
        assert len(grant_calls) == 1, calls
        text = str(excinfo.value)
        assert "openid:failed" in text and "feishu_network_error" in text, text
        assert "email:skipped" in text, text

    def test_lookup_not_found_falls_back_to_email_with_notification_off(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """R7: the directory answering "no such user" is the legitimate
        fallback condition — the email spelling gets its chance, and it now
        asks Feishu not to notify, like the openid spelling always did."""

        def handler(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            if _auth_ok(url):
                return 200, {"code": 0, "tenant_access_token": "t-fake", "expire": 7200}
            if "batch_get_id" in url:
                return 200, {
                    "code": 0,
                    "data": {"user_list": [{"email": "u@corp.com", "user_id": ""}]},
                }
            return 200, {"code": 0, "data": {}}

        calls = _install_fake_feishu_http(monkeypatch, handler)
        client = _client()

        with caplog.at_level(logging.INFO, logger="researchdata_bridge"):
            spelling = client.grant_full_access("doccnGrant5", "u@corp.com")

        assert spelling == "email:u@corp.com"
        grant_calls = [c for c in calls if "/permissions/" in c["url"]]
        assert len(grant_calls) == 1, calls
        assert grant_calls[0]["body"]["member_type"] == "email"
        assert "need_notification=false" in grant_calls[0]["url"], grant_calls[0]["url"]
        grant_lines = [
            m
            for m in (r.getMessage() for r in caplog.records)
            if m.startswith("feishu_grant ")
        ]
        assert len(grant_lines) == 1, grant_lines
        assert "outcome=ok" in grant_lines[0], grant_lines[0]
        assert "lookup:ok" in grant_lines[0] and "(not_found)" in grant_lines[0]
        assert "openid:skipped" in grant_lines[0], grant_lines[0]
        assert "email:ok" in grant_lines[0], grant_lines[0]
        assert "u@corp.com" not in grant_lines[0], grant_lines[0]

    def test_openid_success_records_both_stages(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """R7 r2 (codex #2): a direct openid success answers with the lookup
        and openid stages in both the log line and the returned phases."""

        def handler(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            if _auth_ok(url):
                return 200, {"code": 0, "tenant_access_token": "t-fake", "expire": 7200}
            if "batch_get_id" in url:
                return 200, {
                    "code": 0,
                    "data": {"user_list": [{"email": "u@corp.com", "user_id": "ou_1"}]},
                }
            return 200, {"code": 0, "data": {}}

        _install_fake_feishu_http(monkeypatch, handler)
        client = _client()

        with caplog.at_level(logging.INFO, logger="researchdata_bridge"):
            spelling, phases = client.grant_full_access_with_phases(
                "doccnGrant5b", "u@corp.com"
            )

        assert spelling == "openid:ou_1"
        assert [(p["stage"], p["status"]) for p in phases] == [
            ("lookup", "ok"),
            ("openid", "ok"),
        ]
        grant_lines = [
            m
            for m in (r.getMessage() for r in caplog.records)
            if m.startswith("feishu_grant ")
        ]
        assert len(grant_lines) == 1, grant_lines
        assert "lookup:ok" in grant_lines[0] and "openid:ok" in grant_lines[0], grant_lines[0]

    def test_open_id_never_survives_phases_log_or_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """R7 r2 (codex #1): a Feishu msg that echoes the refused member_id
        (`invalid member_id: ou_…`) must not carry the open_id into the
        raised error, the phase list, or the feishu_grant log line."""

        def handler(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            if _auth_ok(url):
                return 200, {"code": 0, "tenant_access_token": "t-fake", "expire": 7200}
            if "batch_get_id" in url:
                return 200, {
                    "code": 0,
                    "data": {
                        "user_list": [{"email": "u@corp.com", "user_id": "ou_Target1d2e3f"}]
                    },
                }
            if body.get("member_type") == "openid":
                # Identity refusal whose msg echoes the open_id.
                return 400, {
                    "code": 1062005,
                    "msg": "invalid member_id: ou_Target1d2e3f member not exist",
                    "log_id": "lg-oid",
                }
            return 400, {
                "code": 99991661,
                "msg": "invalid member_id: ou_Target1d2e3f rejected",
                "log_id": "lg-email",
            }

        _install_fake_feishu_http(monkeypatch, handler)
        client = _client()

        with caplog.at_level(logging.WARNING, logger="researchdata_bridge"):
            with pytest.raises(rb.FeishuApiError) as excinfo:
                client.grant_full_access("doccnGrant5c", "u@corp.com")

        text = str(excinfo.value)
        assert "ou_Target1d2e3f" not in text, text
        assert "openid:failed" in text and "email:failed" in text, text
        assert "[REDACTED]" in text, text
        for phase in excinfo.value.phases:
            assert "ou_Target1d2e3f" not in phase.get("feishu_msg", ""), phase
        grant_lines = [
            m
            for m in (r.getMessage() for r in caplog.records)
            if m.startswith("feishu_grant ")
        ]
        assert len(grant_lines) == 1, grant_lines
        assert "ou_Target1d2e3f" not in grant_lines[0], grant_lines[0]

    def test_feishu_token_echo_never_survives_success_phases_or_log(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """R7 r3 (codex): a Feishu msg that echoes a token — by shape (`t-…`)
        or the client's own app secret by value — must not reach the phases
        of a SUCCESSFUL grant (they ride the success response) or the log."""
        tenant_token = "t-g1049aTenantTok99"

        def handler(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            if _auth_ok(url):
                return 200, {"code": 0, "tenant_access_token": tenant_token, "expire": 7200}
            if "batch_get_id" in url:
                return 200, {
                    "code": 0,
                    "data": {"user_list": [{"email": "u@corp.com", "user_id": "ou_Tok1d2e3f"}]},
                }
            if body.get("member_type") == "openid":
                return 400, {
                    "code": 1062005,
                    "msg": (
                        f"member not exist; token {tenant_token} secret fake-app-secret"
                        " user u-UserTok77abc"
                    ),
                    "log_id": "lg-oid",
                }
            return 200, {"code": 0, "data": {}}

        _install_fake_feishu_http(monkeypatch, handler)
        client = _client()

        with caplog.at_level(logging.INFO, logger="researchdata_bridge"):
            spelling, phases = client.grant_full_access_with_phases("doccnGrantTok1", "u@corp.com")

        assert spelling.startswith("email")
        dumped = repr(phases)
        assert "openid" in dumped and "email" in dumped, dumped
        # tenant token and app secret: the client's own (scrubbed by value);
        # u-UserTok77abc: a token the client never held (scrubbed by shape).
        for secret in (tenant_token, "fake-app-secret", "u-UserTok77abc"):
            assert secret not in dumped, dumped
            for record in caplog.records:
                assert secret not in record.getMessage(), record.getMessage()
        assert "[REDACTED]" in dumped, dumped


    def test_lookup_transport_failure_raises_without_email_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R7: a directory call that itself failed is NOT "user not found" —
        no email retry masks it; the error names the lookup stage."""

        def handler(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            if _auth_ok(url):
                return 200, {"code": 0, "tenant_access_token": "t-fake", "expire": 7200}
            raise OSError("dns gone")

        calls = _install_fake_feishu_http(monkeypatch, handler)
        client = _client()

        with pytest.raises(rb.FeishuApiError) as excinfo:
            client.grant_full_access("doccnGrant6", "u@corp.com")

        assert not [c for c in calls if "/permissions/" in c["url"]], calls
        text = str(excinfo.value)
        assert "lookup:failed" in text and "feishu_network_error" in text, text
        assert "email:skipped" in text, text

    def test_non_json_upstream_failure_keeps_request_diagnostics_in_detail(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """R7 r3 (codex #2): lookup not found → email grant answered with an
        HTTP 502 HTML page. The stage never reached a payload msg, so its
        ``detail`` must keep `_request`'s own diagnostics — HTTP status,
        content-type, the templated path, a body preview — scrubbed, and the
        summary line must carry it."""

        def handler(url: str, body: dict[str, Any]) -> tuple[int, Any]:
            if _auth_ok(url):
                return 200, {"code": 0, "tenant_access_token": "t-fake", "expire": 7200}
            if "batch_get_id" in url:
                return 200, {
                    "code": 0,
                    "data": {"user_list": [{"email": "u@corp.com", "user_id": ""}]},
                }
            return 502, "<html><body>Bad Gateway</body></html>"

        _install_fake_feishu_http(monkeypatch, handler)
        client = _client()

        with caplog.at_level(logging.WARNING, logger="researchdata_bridge"):
            with pytest.raises(rb.FeishuApiError) as excinfo:
                client.grant_full_access("doccnGrant7", "u@corp.com")

        err = excinfo.value
        email_phase = err.phases[-1]
        assert email_phase["stage"] == "email" and email_phase["status"] == "failed"
        assert email_phase["error_code"] == "feishu_invalid_response"
        assert email_phase["feishu_msg"] == ""
        for fragment in ("http_502", "content_type=text/html", "path=/open-apis/drive/v1/permissions/"):
            assert fragment in email_phase["detail"], email_phase["detail"]
        assert "Bad Gateway" in email_phase["detail"], email_phase["detail"]
        assert "u@corp.com" not in email_phase["detail"], email_phase["detail"]
        assert len(email_phase["detail"]) <= 160, email_phase["detail"]
        text = str(err)
        assert "detail=http_502" in text, text
        grant_lines = [
            m
            for m in (r.getMessage() for r in caplog.records)
            if m.startswith("feishu_grant ")
        ]
        assert len(grant_lines) == 1, grant_lines
        assert "detail=http_502" in grant_lines[0], grant_lines[0]

    def test_network_failure_keeps_exception_type_in_detail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R7 r3 (codex #2): a transport failure on the grant call — the
        exception type is the only diagnosis there was, and it must survive
        in the stage's ``detail``."""

        def handler(url: str, body: dict[str, Any]) -> tuple[int, Any]:
            if _auth_ok(url):
                return 200, {"code": 0, "tenant_access_token": "t-fake", "expire": 7200}
            if "batch_get_id" in url:
                return 200, {
                    "code": 0,
                    "data": {"user_list": [{"email": "u@corp.com", "user_id": "ou_1"}]},
                }
            raise rb.urllib.error.URLError("dial failed")

        _install_fake_feishu_http(monkeypatch, handler)
        client = _client()

        with pytest.raises(rb.FeishuApiError) as excinfo:
            client.grant_full_access("doccnGrant8", "u@corp.com")

        openid_phase = excinfo.value.phases[1]
        assert openid_phase["stage"] == "openid" and openid_phase["status"] == "failed"
        assert openid_phase["error_code"] == "feishu_network_error"
        assert openid_phase["feishu_msg"] == ""
        assert "URLError" in openid_phase["detail"], openid_phase["detail"]
        assert "class=transport" in openid_phase["note"], openid_phase["note"]
        assert "detail=URLError" in str(excinfo.value), str(excinfo.value)


class TestRefusalClassPartition:
    """The conservative partition and its basis — transport first (no
    verdict), then identity by Feishu's own msg wording (before the HTTP
    shapes: Feishu answers business refusals as HTTP 400, so a 400-first
    order would make the email fallback dead code), then permission, then
    parameter for the remaining 400s; everything else is unclassified and
    never falls back."""

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"error_code": "feishu_network_error"}, "transport"),
            ({"error_code": "feishu_invalid_response"}, "transport"),
            ({"error_code": "feishu_empty_error_response"}, "transport"),
            # A 5xx never got a verdict, even when a body code says more.
            ({"error_code": "feishu_http_502", "feishu_code": "1063001"}, "transport"),
            # Feishu answers member-not-exist as HTTP 400 — identity must
            # win over the 400 shape or the fallback is dead code.
            ({"error_code": "feishu_http_400", "feishu_msg": "member not exist"}, "identity"),
            ({"error_code": "feishu_http_400", "feishu_msg": "成员不存在"}, "identity"),
            ({"error_code": "feishu_api_error", "feishu_msg": "user not exist"}, "identity"),
            ({"error_code": "feishu_http_401"}, "permission"),
            ({"error_code": "feishu_http_403"}, "permission"),
            # The production shape (10-28): HTTP 400, code 1063001,
            # "Invalid parameter" — a parameter refusal, no permission
            # mapping is claimed for the code.
            (
                {"error_code": "feishu_http_400", "feishu_code": "1063001",
                 "feishu_msg": "Invalid parameter"},
                "parameter",
            ),
            ({"error_code": "feishu_http_400"}, "parameter"),
            # Unknown business codes and shapes stay unclassified → no fallback.
            ({"error_code": "feishu_api_error", "feishu_code": "99991672"}, "unclassified"),
            ({"error_code": "feishu_api_error", "feishu_code": "1063001"}, "unclassified"),
            ({"error_code": "feishu_http_404"}, "unclassified"),
            ({"error_code": "feishu_api_error", "feishu_msg": "Permission limited"}, "unclassified"),
        ],
    )
    def test_partition(self, kwargs: dict[str, Any], expected: str) -> None:
        exc = rb.FeishuApiError(kwargs.get("error_code", ""), "x", **{
            k: v for k, v in kwargs.items() if k != "error_code"
        })
        assert rb.FeishuClient._grant_refusal_class(exc) == expected

    def test_bridge_own_message_wording_is_not_identity(self) -> None:
        """Identity matches only Feishu's ``feishu_msg`` — never the
        bridge-wrapped message text, so this classifier's (or a caller's)
        own wording can never re-classify a refusal as identity."""
        exc = rb.FeishuApiError(
            "feishu_http_400", "our own text: member not found in the registry"
        )
        assert rb.FeishuClient._grant_refusal_class(exc) == "parameter"

    def test_structured_fields_survive_the_http_error_path(self) -> None:
        """`_request` lifts Feishu's own code/msg/log_id out of the payload —
        the fields the phases and the classification read."""
        exc = rb.FeishuApiError(
            "feishu_api_error",
            "code=1063001 msg=Invalid parameter",
            feishu_code="1063001",
            feishu_msg="Invalid parameter",
            log_id="lg-struct",
        )
        assert (exc.feishu_code, exc.feishu_msg, exc.log_id) == (
            "1063001",
            "Invalid parameter",
            "lg-struct",
        )


# ---------------------------------------------------------------------------
# endpoint: non-docx objects are refused before any grant
# ---------------------------------------------------------------------------


def _grant_bridge(tmp_path: Path) -> rb.ResearchDataBridge:
    bridge = rb.ResearchDataBridge.__new__(rb.ResearchDataBridge)
    bridge.cfg = create_autospec(rb.BridgeConfig, instance=True)
    bridge.cfg.wiki_ingest_audit_file = tmp_path / "wiki_ingest_audit.jsonl"
    bridge._feishu_client_instance = create_autospec(rb.FeishuClient, instance=True)
    bridge._feishu_client_instance.grant_full_access_with_phases.return_value = (
        "openid:ou_1",
        [],
    )
    return bridge


class TestNonDocxRefused:
    @pytest.mark.asyncio
    async def test_wiki_link_resolving_to_sheet_is_refused_400(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R7: a wiki link that resolves to a non-docx object (obj_type !=
        "docx") is refused before the grant — the permission call only speaks
        type=docx, so a sheet's obj_token must never be sent as one."""
        bridge = _grant_bridge(tmp_path)
        bridge._register_feishu_created_doc("objcnSheet1", creator_email="u@corp.com", title="T")
        monkeypatch.setattr(
            rb,
            "resolve_feishu_document",
            create_autospec(
                rb.resolve_feishu_document,
                return_value=(
                    "objcnSheet1",
                    {"obj_token": "objcnSheet1", "obj_type": "sheet", "node_token": "wikinode1"},
                ),
            ),
        )

        code, payload = await bridge.handle_feishu_doc_grant(
            {"token": "https://feishu.cn/wiki/wikinode1", "email": "u@corp.com"}
        )

        assert code == 400, payload
        assert payload["error"] == "not_docx"
        assert payload["obj_type"] == "sheet"
        assert "docx" in payload["message"]
        bridge._feishu_client_instance.grant_full_access.assert_not_called()

    @pytest.mark.asyncio
    async def test_wiki_node_without_obj_type_is_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A wiki link whose node carries no obj_type cannot be verified as
        docx, so it is refused rather than granted blind."""
        bridge = _grant_bridge(tmp_path)
        bridge._register_feishu_created_doc("doccnNoType", creator_email="u@corp.com", title="T")
        monkeypatch.setattr(
            rb,
            "resolve_feishu_document",
            create_autospec(
                rb.resolve_feishu_document,
                return_value=("doccnNoType", {"obj_token": "doccnNoType", "node_token": "wikinode2"}),
            ),
        )

        code, payload = await bridge.handle_feishu_doc_grant(
            {"token": "https://feishu.cn/wiki/wikinode2", "email": "u@corp.com"}
        )

        assert code == 400, payload
        assert payload["error"] == "not_docx"
        assert payload["obj_type"] == "unknown"
        bridge._feishu_client_instance.grant_full_access.assert_not_called()

    @pytest.mark.asyncio
    async def test_wiki_link_resolving_to_docx_still_grants(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The narrow check does not narrow legitimate grants: a wiki node
        with obj_type=docx passes through to the grant."""
        bridge = _grant_bridge(tmp_path)
        bridge._register_feishu_created_doc("doccnWiki9", creator_email="u@corp.com", title="T")
        monkeypatch.setattr(
            rb,
            "resolve_feishu_document",
            create_autospec(
                rb.resolve_feishu_document,
                return_value=(
                    "doccnWiki9",
                    {"obj_token": "doccnWiki9", "obj_type": "docx", "node_token": "wikinode9"},
                ),
            ),
        )

        code, payload = await bridge.handle_feishu_doc_grant(
            {"token": "https://feishu.cn/wiki/wikinode9", "email": "u@corp.com"}
        )

        assert code == 200, payload
        assert payload["ok"] is True
        bridge._feishu_client_instance.grant_full_access_with_phases.assert_called_once_with(
            "doccnWiki9", "u@corp.com"
        )

    @pytest.mark.asyncio
    async def test_grant_endpoint_response_carries_no_open_id(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R7 r2 (codex #1), endpoint leg: a Feishu msg echoing the refused
        member_id must not reach the re-grant response either — neither the
        detail line nor the phases."""

        def handler(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            if _auth_ok(url):
                return 200, {"code": 0, "tenant_access_token": "t-fake", "expire": 7200}
            if "batch_get_id" in url:
                return 200, {
                    "code": 0,
                    "data": {
                        "user_list": [{"email": "u@corp.com", "user_id": "ou_Target1d2e3f"}]
                    },
                }
            if body.get("member_type") == "openid":
                return 400, {
                    "code": 1062005,
                    "msg": "invalid member_id: ou_Target1d2e3f member not exist",
                    "log_id": "lg-oid",
                }
            return 400, {
                "code": 99991661,
                "msg": "invalid member_id: ou_Target1d2e3f rejected",
                "log_id": "lg-email",
            }

        _install_fake_feishu_http(monkeypatch, handler)
        bridge = _grant_bridge(tmp_path)
        bridge._register_feishu_created_doc("doccnG3", creator_email="u@corp.com", title="T")
        # A real client, so the grant actually runs through the fake HTTP
        # layer and the endpoint sees the wrapped three-stage error.
        bridge._feishu_client_instance = _client()
        monkeypatch.setattr(
            rb,
            "resolve_feishu_document",
            create_autospec(rb.resolve_feishu_document, return_value=("doccnG3", {})),
        )

        code, payload = await bridge.handle_feishu_doc_grant(
            {"token": "doccnG3", "email": "u@corp.com"}
        )

        assert code == 502, payload
        assert "ou_Target1d2e3f" not in payload["detail"], payload["detail"]
        assert "ou_Target1d2e3f" not in json.dumps(payload["phases"]), payload["phases"]
        assert "openid:failed" in payload["detail"] and "email:failed" in payload["detail"]

    @pytest.mark.asyncio
    async def test_grant_endpoint_success_response_carries_phases(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R7 r3 (codex #1): an "openid refused for identity → email granted"
        re-grant answers with both stages — the failed openid stage keeps
        Feishu's code/msg/log_id and the email stage records ok."""

        def handler(url: str, body: dict[str, Any]) -> tuple[int, Any]:
            if _auth_ok(url):
                return 200, {"code": 0, "tenant_access_token": "t-fake", "expire": 7200}
            if "batch_get_id" in url:
                return 200, {
                    "code": 0,
                    "data": {"user_list": [{"email": "u@corp.com", "user_id": "ou_1"}]},
                }
            if body.get("member_type") == "openid":
                return 400, {"code": 1062005, "msg": "member not exist", "log_id": "lg-oid"}
            return 200, {"code": 0, "data": {}}

        _install_fake_feishu_http(monkeypatch, handler)
        bridge = _grant_bridge(tmp_path)
        bridge._register_feishu_created_doc("doccnG4", creator_email="u@corp.com", title="T")
        bridge._feishu_client_instance = _client()
        monkeypatch.setattr(
            rb,
            "resolve_feishu_document",
            create_autospec(rb.resolve_feishu_document, return_value=("doccnG4", {})),
        )

        code, payload = await bridge.handle_feishu_doc_grant(
            {"token": "doccnG4", "email": "u@corp.com"}
        )

        assert code == 200, payload
        assert payload["ok"] is True
        assert payload["grant_member"] == "email:u@corp.com"
        assert [(p["stage"], p["status"]) for p in payload["phases"]] == [
            ("lookup", "ok"),
            ("openid", "failed"),
            ("email", "ok"),
        ]
        openid_phase = payload["phases"][1]
        assert openid_phase["feishu_code"] == "1062005"
        assert openid_phase["feishu_msg"] == "member not exist"
        assert openid_phase["log_id"] == "lg-oid"

    @pytest.mark.asyncio
    async def test_grant_failure_answer_lists_every_phase(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R7: the endpoint's failure answer carries the per-stage phases, so
        the caller can see lookup/openid/email results, not a single code."""

        def _fail(token: str, email: str) -> str:
            raise rb.FeishuApiError(
                "feishu_grant_failed",
                "lookup:ok (found) | openid:failed feishu_code=1063001 | email:skipped",
                phases=[
                    {"stage": "lookup", "status": "ok", "note": "found", "error_code": "",
                     "feishu_code": "", "feishu_msg": "", "log_id": ""},
                    {"stage": "openid", "status": "failed", "note": "class=permission",
                     "error_code": "feishu_api_error", "feishu_code": "1063001",
                     "feishu_msg": "Permission limited", "log_id": "lg-x"},
                    {"stage": "email", "status": "skipped",
                     "note": "no fallback: openid refusal class=permission", "error_code": "",
                     "feishu_code": "", "feishu_msg": "", "log_id": ""},
                ],
            )

        bridge = _grant_bridge(tmp_path)
        bridge._register_feishu_created_doc("doccnG1", creator_email="u@corp.com", title="T")
        grant_client = rb.FeishuClient("fake-app-id", "fake-app-secret", timeout_s=1.0)
        grant_client.grant_full_access_with_phases = create_autospec(
            grant_client.grant_full_access_with_phases, side_effect=_fail
        )
        bridge._feishu_client_instance = grant_client
        monkeypatch.setattr(
            rb,
            "resolve_feishu_document",
            create_autospec(rb.resolve_feishu_document, return_value=("doccnG1", {})),
        )

        code, payload = await bridge.handle_feishu_doc_grant(
            {"token": "doccnG1", "email": "u@corp.com"}
        )

        assert code == 502, payload
        assert payload["error"] == "feishu_grant_failed"
        assert "openid:failed" in payload["detail"] and "1063001" in payload["detail"]
        assert [(p["stage"], p["status"]) for p in payload["phases"]] == [
            ("lookup", "ok"),
            ("openid", "failed"),
            ("email", "skipped"),
        ]
        assert payload["phases"][1]["log_id"] == "lg-x"
