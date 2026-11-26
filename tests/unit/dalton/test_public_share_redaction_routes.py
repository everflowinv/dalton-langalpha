"""Route-level & security-boundary unit tests for AU1 public share redaction (§6.2.2 + S19 + B5/B6).

All test doubles use ``unittest.mock.create_autospec``.
Covers:
- Unkeyed single-user ``oss`` unified identity loopback gate across ``jwt_bearer``, ``api``,
  ``ws_auth`` (HTTP 401 denial extension & 1008 fallback), ``messaging._is_service_request``,
  and ``deployment_setup._owner_only``, while ``get_optional_user_id`` stays anonymous for public links.
- S19 dev-fallback transcription ticket loopback gate BOTH when ``LANGALPHA_OWNER_SECRET`` is
  unset AND when ``LANGALPHA_OWNER_SECRET`` is set, plus dedicated secret remote pass & forgery refusal.
- ``dalton_oss_media_session`` 3-factor Minting Trust Gate & cross-port origin isolation (including B5).
- Conflicting permissions ``{"allow_files": false, "allow_download": true}`` -> 403 + ``no-store`` +
  ``Clear-Site-Data: "cache"`` on all 5 ``/files*`` routes and ``/media/*``.
- Org library fixed generic title, ``summary`` scrubbing on all read/write routes, and ``GET /api/v1/activity``
  per-row ``custom_title`` + all-action ``detail`` scrubbing + B6 deleted-thread cursor stability.
- Singleflight coalescing for concurrent cold ``allow_files=false`` replay + zero ``finish_lines``.
- DB-only ``summarize_thread_share_redaction`` making zero calls to ``finish_lines``, storage,
  ``stamp_replay_task_status``, or ``CheckpointHistoryReader``.
- Ops script ``privatize_shared_thread_objects.py`` ``--verify`` & ``--warm-shared-slices``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec
import uuid

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketException
from fastapi.testclient import TestClient
import pytest
from starlette.datastructures import Headers

from scripts.dalton.ops import privatize_shared_thread_objects as ops_script
from src.dalton import share_media, share_redaction
from src.dalton.share_redaction import DEFAULT_SHARED_TITLE
from src.dalton.app import transcriptions as asr_router_mod
from src.dalton.services import transcriptions as asr_svc
from src.server.app import deployment_setup
from src.server.app import public as public_app
from src.server.app import share_access
from src.server.app.threads import messaging as messaging_app
from src.server.auth import jwt_bearer, owner_requests, ws_auth
from src.server.services import activity as activity_svc
from src.server.services.history.replay import ReplayPage
from src.server.utils import api as api_utils


def _make_scope(
    *,
    client: tuple[str, int] | None = ("127.0.0.1", 51234),
    host: str = "127.0.0.1:8000",
    headers: dict[str, str] | None = None,
    scheme: str = "http",
    scope_type: str = "http",
    extensions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    hdrs: list[tuple[bytes, bytes]] = []
    all_hdrs = {"host": host, **(headers or {})}
    for k, v in all_hdrs.items():
        hdrs.append((k.lower().encode("latin-1"), v.encode("latin-1")))
    scope: dict[str, Any] = {
        "type": scope_type,
        "scheme": scheme,
        "headers": hdrs,
        "state": {},
    }
    if client is not None:
        scope["client"] = client
    if extensions is not None:
        scope["extensions"] = extensions
    return scope


def _make_request(scope: dict[str, Any], cookies: dict[str, str] | None = None) -> Request:
    req = create_autospec(Request, instance=True)
    req.scope = scope
    hdr_dict = {
        k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers") or ()
    }
    req.headers = Headers(hdr_dict)
    req.cookies = dict(cookies or {})
    return req


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_utils, "HOST_MODE", "oss")
    monkeypatch.setattr(jwt_bearer, "HOST_MODE", "oss")
    monkeypatch.setattr(ws_auth, "HOST_MODE", "oss")
    monkeypatch.delenv("DALTON_PROFILE", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
    monkeypatch.delenv("LANGALPHA_OWNER_SECRET", raising=False)
    monkeypatch.delenv("LANGALPHA_DESKTOP_APP", raising=False)
    monkeypatch.delenv("DALTON_TRANSCRIPTION_TICKET_SECRET", raising=False)
    monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("DALTON_BRIDGE_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("DALTON_TRUSTED_APP_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")


@pytest.mark.asyncio
async def test_unkeyed_oss_unified_identity_loopback_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_utils, "admit_caller", create_autospec(api_utils.admit_caller, return_value=None))

    bad_scopes = [
        _make_scope(client=("203.0.113.9", 50000)),
        _make_scope(client=("172.31.250.2", 50000)),
        _make_scope(client=("127.0.0.1", 50000), headers={"X-Forwarded-For": "203.0.113.9"}),
        _make_scope(client=("127.0.0.1", 50000), headers={"Forwarded": "for=203.0.113.9"}),
        _make_scope(client=("127.0.0.1", 50000), headers={"X-Real-IP": "203.0.113.9"}),
        _make_scope(client=("127.0.0.1", 50000), headers={"X-Forwarded-Host": "public.example.com"}),
        _make_scope(client=("127.0.0.1", 50000), headers={"X-Forwarded-Proto": "https"}),
        _make_scope(client=("127.0.0.1", 50000), host="public.example.com"),
        _make_scope(client=("127.0.0.1", 50000), headers={"Origin": "http://127.0.0.1:3999"}),
        _make_scope(client=("127.0.0.1", 50000), headers={"Origin": "null"}),
    ]

    for scope in bad_scopes:
        req = _make_request(scope)
        with pytest.raises(HTTPException) as exc1:
            await jwt_bearer.verify_jwt_token(request=req, credentials=None)
        assert exc1.value.status_code == 401

        with pytest.raises(HTTPException) as exc2:
            await jwt_bearer.get_current_auth_info(request=req, credentials=None)
        assert exc2.value.status_code == 401

        with pytest.raises(HTTPException) as exc3:
            await api_utils.get_user_id_any_state(request=req, credentials=None)
        assert exc3.value.status_code == 401

        # Optional auth MUST return None (anonymous) so public share pages still work behind Caddy
        assert await api_utils.get_optional_user_id(request=req, credentials=None) is None

        # Self-dispatch must refuse remote/proxied unkeyed requests
        assert messaging_app._is_service_request(req) is False

        # WebSocket handshake: with websocket.http.response extension sends 401 denial response
        ws_denial = create_autospec(WebSocket, instance=True)
        ws_denial.scope = {**scope, "type": "websocket", "extensions": {"websocket.http.response": {}}}
        with pytest.raises(WebSocketException):
            await ws_auth.authenticate_websocket(ws_denial)
        ws_denial.send_denial_response.assert_awaited_once()
        denial_resp = ws_denial.send_denial_response.call_args.args[0]
        assert denial_resp.status_code == 401

        # WebSocket handshake without extension falls back to close(1008)
        ws_plain = create_autospec(WebSocket, instance=True)
        ws_plain.scope = {**scope, "type": "websocket"}
        with pytest.raises(WebSocketException):
            await ws_auth.authenticate_websocket(ws_plain)
        ws_plain.close.assert_awaited_once()

    # Direct loopback without forwarding headers passes
    good_req = _make_request(_make_scope(client=("127.0.0.1", 51234), host="127.0.0.1:8000"))
    assert await api_utils.get_user_id_any_state(request=good_req, credentials=None) == api_utils.LOCAL_DEV_USER_ID
    assert await jwt_bearer.verify_jwt_token(request=good_req, credentials=None) == jwt_bearer.LOCAL_DEV_USER_ID
    assert messaging_app._is_service_request(good_req) is True

    # deployment_setup._owner_only also rejects remote when Dalton disabled
    monkeypatch.setattr("src.dalton.settings.is_dalton_enabled", lambda: False)
    with pytest.raises(HTTPException) as exc_setup:
        deployment_setup._owner_only(_make_request(bad_scopes[0]))
    assert exc_setup.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("with_owner_secret", [False, True])
async def test_s19_dev_fallback_transcription_ticket_loopback_gate(
    monkeypatch: pytest.MonkeyPatch,
    with_owner_secret: bool,
) -> None:
    """S19: dev-fallback transcription tickets refuse remote/proxied requests regardless of LANGALPHA_OWNER_SECRET."""
    if with_owner_secret:
        monkeypatch.setenv(owner_requests.OWNER_SECRET_ENV, "configured-owner-secret-value")
    else:
        monkeypatch.delenv(owner_requests.OWNER_SECRET_ENV, raising=False)

    monkeypatch.setattr(asr_router_mod, "is_transcription_enabled", lambda: True)
    monkeypatch.setattr(asr_router_mod, "admit_caller", create_autospec(asr_router_mod.admit_caller, return_value=None))
    monkeypatch.setattr(
        asr_router_mod,
        "get_dalton_settings",
        lambda: SimpleNamespace(enabled=True),
    )

    job_id = str(uuid.uuid4())
    ticket, _exp = asr_svc.issue_transcription_ticket(
        job_id=job_id,
        user_id="local-dev-user",
        workspace_id=str(uuid.uuid4()),
        thread_id=str(uuid.uuid4()),
        source_object_key="transcriptions/ws/job/source/a.wav",
        source_size_bytes=1024,
        source_sha256="a" * 64,
    )

    # 1. Remote or reverse-proxied request with valid dev-fallback ticket -> 401 (even if LANGALPHA_OWNER_SECRET is set!)
    for bad_scope in (
        _make_scope(client=("203.0.113.9", 55555), headers={"X-Dalton-Transcription-Ticket": ticket}),
        _make_scope(
            client=("127.0.0.1", 55555),
            headers={"X-Forwarded-For": "203.0.113.9", "X-Dalton-Transcription-Ticket": ticket},
        ),
    ):
        req = _make_request(bad_scope)
        req.url = SimpleNamespace(path=f"/api/v1/dalton/transcriptions/{job_id}/result")
        with pytest.raises(HTTPException) as exc:
            await asr_router_mod._transcription_caller_gate(req, credentials=None)
        assert exc.value.status_code == 401

        with pytest.raises(HTTPException) as exc_raw:
            await asr_router_mod._resolve_caller_and_ticket(
                req, None, required_op="result", expected_job_id=job_id
            )
        assert exc_raw.value.status_code == 401

    # 2. Direct loopback non-proxied request with dev-fallback ticket -> succeeds
    local_req = _make_request(
        _make_scope(client=("127.0.0.1", 55555), headers={"X-Dalton-Transcription-Ticket": ticket})
    )
    local_req.url = SimpleNamespace(path=f"/api/v1/dalton/transcriptions/{job_id}/result")
    uid, claims = await asr_router_mod._resolve_caller_and_ticket(
        local_req, None, required_op="result", expected_job_id=job_id
    )
    assert uid == "local-dev-user"
    assert claims is not None and claims["job_id"] == job_id

    # 3. Dedicated DALTON_TRANSCRIPTION_TICKET_SECRET: dedicated ticket works remotely, forged dev-seed ticket returns 401
    monkeypatch.setenv("DALTON_TRANSCRIPTION_TICKET_SECRET", "dedicated-random-ticket-secret-32-bytes!!")
    real_ticket, _ = asr_svc.issue_transcription_ticket(
        job_id=job_id,
        user_id="local-dev-user",
        workspace_id=str(uuid.uuid4()),
        thread_id=str(uuid.uuid4()),
        source_object_key="transcriptions/ws/job/source/a.wav",
        source_size_bytes=1024,
        source_sha256="a" * 64,
    )
    remote_real = _make_request(
        _make_scope(client=("203.0.113.9", 55555), headers={"X-Dalton-Transcription-Ticket": real_ticket})
    )
    remote_real.url = SimpleNamespace(path=f"/api/v1/dalton/transcriptions/{job_id}/result")
    uid2, _ = await asr_router_mod._resolve_caller_and_ticket(
        remote_real, None, required_op="result", expected_job_id=job_id
    )
    assert uid2 == "local-dev-user"

    remote_forged = _make_request(
        _make_scope(client=("203.0.113.9", 55555), headers={"X-Dalton-Transcription-Ticket": ticket})
    )
    remote_forged.url = SimpleNamespace(path=f"/api/v1/dalton/transcriptions/{job_id}/result")
    with pytest.raises(HTTPException) as exc_forged:
        await asr_router_mod._resolve_caller_and_ticket(
            remote_forged, None, required_op="result", expected_job_id=job_id
        )
    assert exc_forged.value.status_code == 401


@pytest.mark.asyncio
async def test_oss_media_cookie_minting_and_cross_port_isolation_with_b5() -> None:
    tid = str(uuid.uuid4())
    # 1. Trusted cross-port primary app origin (http://localhost:5173) mints cookie
    mint_scope = _make_scope(
        client=("127.0.0.1", 51234),
        host="127.0.0.1:8000",
        headers={
            "Origin": "http://localhost:5173",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
        },
    )
    resp = Response()
    minted = share_media.mint_oss_media_session_cookie(_make_request(mint_scope), resp)
    assert minted is not None
    sid_hash, origin_hash = minted
    set_cookie = resp.headers.get("set-cookie") or ""
    assert share_media.OSS_MEDIA_SESSION_COOKIE in set_cookie
    cookie_val = set_cookie.split(f"{share_media.OSS_MEDIA_SESSION_COOKIE}=", 1)[1].split(";", 1)[0]

    # Untrusted preview port or remote request gets ZERO Set-Cookie
    resp_bad = Response()
    bad_mint_scope = _make_scope(
        client=("127.0.0.1", 51234),
        host="127.0.0.1:8000",
        headers={"Origin": "http://127.0.0.1:3999"},
    )
    assert share_media.mint_oss_media_session_cookie(_make_request(bad_mint_scope), resp_bad) is None
    assert "set-cookie" not in resp_bad.headers

    tok = share_media.mint_owner_media_token(
        thread_id=tid,
        viewer_id="local-dev-user",
        backend="upstream",
        ref_id=f"private-media/response-images/{tid}/abcdef1234567890/chart.png",
        sid_hash=sid_hash,
        origin_hash=origin_hash,
    )
    claims = share_media.verify_owner_media_token(tok, expected_thread_id=tid)

    # 2. Same trusted origin + CORS + valid Cookie -> 200
    good_read = _make_request(mint_scope, cookies={share_media.OSS_MEDIA_SESSION_COOKIE: cookie_val})
    assert await share_media.require_media_viewer(good_read, tid, claims, None) == "local-dev-user"

    # 3. Same host other port (preview 127.0.0.1:3999) with same valid Cookie -> 401
    preview_req = _make_request(
        _make_scope(
            client=("127.0.0.1", 51234),
            host="127.0.0.1:8000",
            headers={
                "Origin": "http://127.0.0.1:3999",
                "Sec-Fetch-Site": "same-site",
                "Sec-Fetch-Mode": "cors",
            },
        ),
        cookies={share_media.OSS_MEDIA_SESSION_COOKIE: cookie_val},
    )
    with pytest.raises(HTTPException) as exc_preview:
        await share_media.require_media_viewer(preview_req, tid, claims, None)
    assert exc_preview.value.status_code == 401

    # 4. no-cors <img> tag with same-site + valid Cookie -> 401
    nocors_req = _make_request(
        _make_scope(
            client=("127.0.0.1", 51234),
            host="127.0.0.1:8000",
            headers={
                "Sec-Fetch-Site": "same-site",
                "Sec-Fetch-Mode": "no-cors",
            },
        ),
        cookies={share_media.OSS_MEDIA_SESSION_COOKIE: cookie_val},
    )
    with pytest.raises(HTTPException) as exc_nocors:
        await share_media.require_media_viewer(nocors_req, tid, claims, None)
    assert exc_nocors.value.status_code == 401

    # 5. B5: client stripping Sec-Fetch-* with trusted Origin -> 200; bare Cookie without Origin/Referer -> 401
    b5_ok = _make_request(
        _make_scope(
            client=("127.0.0.1", 51234),
            host="127.0.0.1:8000",
            headers={"Origin": "http://localhost:5173"},
        ),
        cookies={share_media.OSS_MEDIA_SESSION_COOKIE: cookie_val},
    )
    assert await share_media.require_media_viewer(b5_ok, tid, claims, None) == "local-dev-user"

    b5_bare = _make_request(
        _make_scope(client=("127.0.0.1", 51234), host="127.0.0.1:8000"),
        cookies={share_media.OSS_MEDIA_SESSION_COOKIE: cookie_val},
    )
    with pytest.raises(HTTPException) as exc_bare:
        await share_media.require_media_viewer(b5_bare, tid, claims, None)
    assert exc_bare.value.status_code == 401


def test_conflicting_allow_files_false_allow_download_true_blocks_all_file_and_media_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid.uuid4())
    ws_id = str(uuid.uuid4())
    conflicting_thread = {
        "conversation_thread_id": tid,
        "workspace_id": ws_id,
        "user_id": "owner-1",
        "share_permissions": {"allow_files": False, "allow_download": True},
        "title": "SECRET_THREAD_TITLE",
    }
    monkeypatch.setattr(
        share_access,
        "get_shared_thread",
        create_autospec(share_access.get_shared_thread, return_value=conflicting_thread),
    )

    app = FastAPI()
    app.include_router(public_app.router)
    client = TestClient(app, raise_server_exceptions=False)

    ref = share_media.mint_public_media_ref(
        thread_id=tid, backend="upstream", ref_id=f"private-media/response-images/{tid}/0123456789abcdef/a.png"
    )

    routes = [
        ("GET", "/api/v1/public/shared/tok/files"),
        ("POST", "/api/v1/public/shared/tok/files/resolve"),
        ("GET", "/api/v1/public/shared/tok/files/read?path=a.txt"),
        ("GET", "/api/v1/public/shared/tok/files/download?path=a.txt"),
        ("GET", "/api/v1/public/shared/tok/files/serve/report.html"),
        ("GET", "/api/v1/public/shared/tok/files/serve/report.html?format=pdf"),
        ("GET", f"/api/v1/public/shared/tok/media/{ref}"),
    ]

    for method, path in routes:
        if method == "POST":
            r = client.post(path, json={"candidates": ["a.txt"]})
        else:
            r = client.get(path)
        assert r.status_code == 403, f"{method} {path} returned {r.status_code}"
        assert r.headers.get("cache-control") == "no-store"
        assert r.headers.get("clear-site-data") == '"cache"'


@pytest.mark.asyncio
async def test_org_library_and_activity_per_row_title_summary_and_b6_deleted_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid_alive = str(uuid.uuid4())
    tid_deleted = str(uuid.uuid4())
    item_legacy = str(uuid.uuid4())
    item_custom = str(uuid.uuid4())
    item_deleted = str(uuid.uuid4())

    monkeypatch.setattr(
        share_redaction,
        "get_org_library_title_meta_map",
        create_autospec(
            share_redaction.get_org_library_title_meta_map,
            return_value={item_custom: True},
        ),
    )
    monkeypatch.setattr(
        share_redaction,
        "_load_threads_for_redaction",
        create_autospec(
            share_redaction._load_threads_for_redaction,
            return_value={
                tid_alive: {
                    "conversation_thread_id": tid_alive,
                    "share_permissions": {"allow_files": False},
                    "owner_user_id": "u1",
                    "computer_root_dir": "/Users/alice/ws",
                    "previous_root_dirs": ["/srv/legacy_custom_root"],
                },
                # tid_deleted intentionally absent (physically deleted thread - B6)
            },
        ),
    )
    monkeypatch.setattr(
        share_redaction,
        "_load_thread_protected_urls_map",
        create_autospec(
            share_redaction._load_thread_protected_urls_map,
            return_value={
                tid_alive: {"https://custom-media.internal/doc/SECRET_THREAD_URL_01"}
            },
        ),
    )
    monkeypatch.setattr(
        share_redaction,
        "_load_workspace_roots_map",
        create_autospec(
            share_redaction._load_workspace_roots_map,
            return_value={"ws-only-1": ("/srv/ws_only_root",)},
        ),
    )

    raw_lib_rows = [
        {
            "item_id": item_legacy,
            "kind": "thread",
            "thread_id": tid_alive,
            "title": "SECRET_AUTO_LIB_01",
            "summary": (
                "Check /api/v1/artifacts/g/v1.grant_lib_summary_01/v/1/a.html, "
                "/Users/alice/ws/secret.py, /srv/legacy_custom_root/old.csv, "
                "reports/q3.pdf, and https://custom-media.internal/doc/SECRET_THREAD_URL_01"
            ),
        },
        {
            "item_id": item_custom,
            "kind": "thread",
            "thread_id": tid_alive,
            "title": "公开策略周报",
            "summary": "Summary with /api/v1/artifacts/g/v1.grant_lib_summary_01/v/1/b.html",
        },
        {
            "item_id": item_deleted,
            "kind": "thread",
            "thread_id": tid_deleted,
            "title": "SECRET_DELETED_THREAD_TITLE",
            "summary": "Deleted thread /api/v1/artifacts/g/v1.grant_deleted/v/1/c.html",
        },
    ]
    redacted_lib = await share_redaction.redact_org_library_rows(raw_lib_rows)
    assert len(redacted_lib) == 3
    assert redacted_lib[0]["title"] == DEFAULT_SHARED_TITLE
    assert "grant_lib_summary_01" not in redacted_lib[0]["summary"]
    assert "/Users/alice/ws" not in redacted_lib[0]["summary"]
    assert "/srv/legacy_custom_root" not in redacted_lib[0]["summary"]
    assert "reports/q3.pdf" not in redacted_lib[0]["summary"]
    assert "SECRET_THREAD_URL_01" not in redacted_lib[0]["summary"]
    assert redacted_lib[1]["title"] == "公开策略周报"
    assert "grant_lib_summary_01" not in redacted_lib[1]["summary"]
    # B6: physically deleted thread row stays stable and sanitized
    assert redacted_lib[2]["title"] == DEFAULT_SHARED_TITLE
    assert "grant_deleted" not in redacted_lib[2]["summary"]

    # Activity log: early auto-title row stays Shared Conversation even after item got custom title later,
    # new custom_title=True row keeps explicit title, and all other Action rows (including workspace-only
    # FILE_PUBLISHED with no thread_id) have detail & path scrubbed.
    now = datetime.now(timezone.utc)
    raw_activity = [
        {
            "event_id": str(uuid.uuid4()),
            "action": activity_svc.Action.LIBRARY_PUBLISHED.value,
            "thread_id": tid_alive,
            "detail": {"kind": "thread", "title": "SECRET_AUTO_LIB_01", "custom_title": False},
            "created_at": now,
        },
        {
            "event_id": str(uuid.uuid4()),
            "action": activity_svc.Action.LIBRARY_UPDATED.value,
            "thread_id": tid_alive,
            "detail": {"kind": "thread", "title": "公开策略周报", "custom_title": True},
            "created_at": now,
        },
        {
            "event_id": str(uuid.uuid4()),
            "action": activity_svc.Action.FILE_PUBLISHED.value,
            "thread_id": tid_alive,
            "detail": {
                "path": "/Users/alice/ws/SECRET_ACT_PATH_01.html",
                "note": (
                    "/api/v1/artifacts/g/v1.grant_act/v/1/x.html "
                    "https://custom-media.internal/doc/SECRET_THREAD_URL_01 "
                    "/srv/legacy_custom_root/old.md"
                ),
            },
            "created_at": now,
        },
        {
            "event_id": str(uuid.uuid4()),
            "action": activity_svc.Action.LIBRARY_PUBLISHED.value,
            "thread_id": tid_deleted,
            "detail": {"kind": "thread", "title": "SECRET_DELETED_ACT"},
            "created_at": now,
        },
        {
            "event_id": str(uuid.uuid4()),
            "action": activity_svc.Action.FILE_PUBLISHED.value,
            "workspace_id": "ws-only-1",
            "thread_id": None,
            "detail": {"path": "reports/q3_confidential.pdf", "note": "/srv/ws_only_root/draft.md"},
            "created_at": now,
        },
    ]
    redacted_act = await share_redaction.redact_activity_rows(raw_activity)
    assert len(redacted_act) == 5
    assert redacted_act[0]["detail"]["title"] == DEFAULT_SHARED_TITLE
    assert "custom_title" not in redacted_act[0]["detail"]
    assert redacted_act[1]["detail"]["title"] == "公开策略周报"
    assert "custom_title" not in redacted_act[1]["detail"]
    assert redacted_act[2]["detail"]["path"] == "[redacted-path]"
    assert "SECRET_ACT_PATH_01" not in json.dumps(redacted_act[2]["detail"])
    assert "grant_act" not in json.dumps(redacted_act[2]["detail"])
    assert "SECRET_THREAD_URL_01" not in json.dumps(redacted_act[2]["detail"])
    assert "/srv/legacy_custom_root" not in json.dumps(redacted_act[2]["detail"])
    assert redacted_act[3]["detail"]["title"] == DEFAULT_SHARED_TITLE
    assert redacted_act[4]["detail"]["path"] == "[redacted-path]"
    assert "q3_confidential.pdf" not in json.dumps(redacted_act[4]["detail"])
    assert "/srv/ws_only_root" not in json.dumps(redacted_act[4]["detail"])


@pytest.mark.asyncio
async def test_singleflight_coalesces_concurrent_cold_reads_and_isolates_waiter_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    gate = asyncio.Event()
    mark_miss = False

    async def _slow_read(rows, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs.get("persist") is False
        if mark_miss:
            share_redaction.mark_cold_projection_miss()
        await gate.wait()
        return ReplayPage(lines=[], first_turn_index=0, has_more=False), "checkpoint"

    from src.server.services.history import replay as replay_mod

    read_spy = create_autospec(replay_mod.read_replay_page, side_effect=_slow_read)
    warm_spy = create_autospec(
        share_redaction.schedule_warm_shared_thread_slices, return_value=None
    )
    monkeypatch.setattr(replay_mod, "read_replay_page", read_spy)
    monkeypatch.setattr(
        replay_mod, "lines_epoch", create_autospec(replay_mod.lines_epoch, return_value="test-epoch-1")
    )
    monkeypatch.setattr(share_redaction, "schedule_warm_shared_thread_slices", warm_spy)

    rows = SimpleNamespace(thread_id=str(uuid.uuid4()))

    # 1. Warm hit (no cold projection miss) + cancelling a waiter must not break leader or other waiters
    tasks = [
        asyncio.create_task(
            share_redaction.read_strict_public_replay_page(
                rows, trigger_background_warm=True
            )
        )
        for _ in range(10)
    ]
    await asyncio.sleep(0.02)
    # Cancel a waiter (index 3) and even the first caller (index 0)
    tasks[3].cancel()
    tasks[0].cancel()
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert isinstance(results[3], asyncio.CancelledError)
    succeeded = [r for idx, r in enumerate(results) if idx not in (0, 3)]
    assert len(succeeded) == 8
    assert all(not isinstance(r, BaseException) for r in succeeded)
    assert calls == 1
    # Hot cache hit returning "checkpoint" must NOT schedule background warm
    warm_spy.assert_not_called()

    # 2. Actual cold cache miss (cold.project_stale -> mark_cold_projection_miss) schedules warm once
    gate.clear()
    mark_miss = True
    t_cold = asyncio.create_task(
        share_redaction.read_strict_public_replay_page(rows, trigger_background_warm=True)
    )
    await asyncio.sleep(0.01)
    gate.set()
    await t_cold
    warm_spy.assert_called_once()


@pytest.mark.asyncio
async def test_ops_script_verify_and_warm(monkeypatch: pytest.MonkeyPatch) -> None:
    target_s3 = share_media.ActiveStorageTarget(
        provider="s3",
        bucket_name="primary-bucket",
        endpoint_url="http://127.0.0.1:9000",
        signing_endpoint_url="http://127.0.0.1:9000",
        region="us-east-1",
        addressing_style="path",
        access_key_id="ak",
        secret_access_key="sk",
        roles=("upstream",),
        key_prefixes=("",),
        sensitive_prefixes=("private-media/", "blobs/", "provenance/", "memo/"),
        public_url_bases=("https://cdn.example.com",),
    )
    target_oss = share_media.ActiveStorageTarget(
        provider="oss",
        bucket_name="oss-bucket",
        endpoint_url="https://oss-cn-hangzhou.aliyuncs.com",
        signing_endpoint_url="https://oss-cn-hangzhou.aliyuncs.com",
        region="cn-hangzhou",
        addressing_style="virtual",
        access_key_id="ak",
        secret_access_key="sk",
        roles=("upstream",),
        key_prefixes=("",),
        sensitive_prefixes=("private-media/", "provenance/"),
        public_url_bases=("https://oss-bucket.oss-cn-hangzhou.aliyuncs.com",),
    )

    written_probes: list[tuple[str, str]] = []
    cleaned_probes: list[tuple[str, str]] = []
    seen_urls: list[str] = []

    def _fake_put_probe(target, key):
        written_probes.append((target.provider, key))

        def _cleanup():
            cleaned_probes.append((target.provider, key))

        return _cleanup

    def _fake_get_403(url: str, *, timeout_s: float = 10.0) -> tuple[int | None, str]:
        seen_urls.append(url)
        return 403, "Forbidden"

    # 1. Default probe_write=True writes & deletes probes on BOTH S3 and OSS and requires strict 403
    ok, checks = ops_script.verify_storage_targets(
        targets=[target_s3, target_oss],
        http_get_fn=_fake_get_403,
        put_probe_fn=_fake_put_probe,
        find_existing_fn=lambda t, p: None,
    )
    assert ok is True
    assert len(written_probes) == 6  # 4 S3 + 2 OSS
    assert len(cleaned_probes) == 6
    assert any(p == "oss" for p, _ in written_probes)
    assert any("cdn.example.com/provenance/" in u for u in seen_urls)
    assert any("127.0.0.1:9000/primary-bucket/memo/" in u for u in seen_urls)
    assert any("oss-bucket.oss-cn-hangzhou.aliyuncs.com/provenance/" in u for u in seen_urls)

    # 2. HTTP 404 MUST FAIL verification (never false-pass on missing key)
    ok_404, checks_404 = ops_script.verify_storage_targets(
        targets=[target_s3],
        http_get_fn=lambda url, timeout_s=10.0: (404, "Not Found"),
        put_probe_fn=_fake_put_probe,
        find_existing_fn=lambda t, p: None,
    )
    assert ok_404 is False
    assert all(not c.ok and c.status_code == 404 for c in checks_404)

    # 3. Any 200 immediately fails verification
    ok_200, checks_200 = ops_script.verify_storage_targets(
        targets=[target_s3],
        http_get_fn=lambda url, timeout_s=10.0: (200, "OK") if "/provenance/" in url else (403, "Forbidden"),
        put_probe_fn=_fake_put_probe,
        find_existing_fn=lambda t, p: None,
    )
    assert ok_200 is False
    assert any(not c.ok and c.status_code == 200 for c in checks_200)

    # 4. Probe cleanup failure MUST fail verification
    def _failing_cleanup_put(target, key):
        def _boom():
            raise RuntimeError("delete denied")
        return _boom

    ok_clean, checks_clean = ops_script.verify_storage_targets(
        targets=[target_oss],
        http_get_fn=_fake_get_403,
        put_probe_fn=_failing_cleanup_put,
        find_existing_fn=lambda t, p: None,
    )
    assert ok_clean is False
    assert any("cleanup failed" in c.detail for c in checks_clean)

    # 5. --no-probe-write with no existing object MUST fail verification
    ok_noprobe, checks_noprobe = ops_script.verify_storage_targets(
        targets=[target_s3],
        probe_write=False,
        http_get_fn=_fake_get_403,
        find_existing_fn=lambda t, p: None,
    )
    assert ok_noprobe is False
    assert all("no existing object" in c.detail for c in checks_noprobe)

    # 6. --warm-shared-slices opens & closes BOTH app pool and checkpointer pool and warms persist=True
    from contextlib import asynccontextmanager
    from src.server.database import pool as db_pool
    from src.server.database.conversation import replay_rows as replay_rows_mod
    from src.server.services.history import replay as replay_mod

    shared_tid = str(uuid.uuid4())
    lifecycle: list[str] = []

    async def _fake_open_pools():
        lifecycle.append("open_pools")
        return ("fake-pool", "fake-cp", True, True)

    async def _fake_close_pools(conv_pool, cp, opened_app, opened_cp):
        assert opened_app is True and opened_cp is True
        lifecycle.append("close_pools")

    class _FakeWarmCur:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql, params=None):
            return None

        async def fetchall(self):
            return [{"thread_id": shared_tid}]

    class _FakeWarmConn:
        def cursor(self, row_factory=None):
            return _FakeWarmCur()

    @asynccontextmanager
    async def _fake_warm_conn():
        yield _FakeWarmConn()

    fake_rows = SimpleNamespace(thread_id=shared_tid)
    read_warm_spy = create_autospec(
        replay_mod.read_replay_page,
        return_value=(ReplayPage(lines=[], first_turn_index=0, has_more=False), "checkpoint"),
    )
    monkeypatch.setattr(ops_script, "_ensure_cli_pools_open", _fake_open_pools)
    monkeypatch.setattr(ops_script, "_close_cli_pools", _fake_close_pools)
    monkeypatch.setattr(db_pool, "get_db_connection", _fake_warm_conn)
    monkeypatch.setattr(
        replay_rows_mod,
        "get_replay_thread_data",
        create_autospec(replay_rows_mod.get_replay_thread_data, return_value=fake_rows),
    )
    monkeypatch.setattr(replay_mod, "read_replay_page", read_warm_spy)
    monkeypatch.setattr(
        replay_mod, "lines_epoch", create_autospec(replay_mod.lines_epoch, return_value="warm-epoch-1")
    )

    warm_res = await ops_script.warm_shared_slices(limit_threads=10, concurrency=2)
    assert lifecycle == ["open_pools", "close_pools"]
    assert warm_res["warmed"] == 1
    assert warm_res["failed"] == 0
    read_warm_spy.assert_awaited_once()
    assert read_warm_spy.await_args.kwargs["persist"] is True


@pytest.mark.asyncio
async def test_summarize_thread_share_redaction_db_only_zero_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """summarize_thread_share_redaction reads only DB rows with 0 calls to finish_lines / storage / checkpoint."""
    from contextlib import asynccontextmanager
    from src.server.database import pool as db_pool
    from src.server.services.history import reader as reader_mod
    from src.server.services.history import replay as replay_mod
    from src.utils import storage as storage_mod

    query_rows = [
        {
            "turn_index": 0,
            "type": "initial",
            "content": "Check ![a](https://img.example/a.png)\n<file-comments>secret</file-comments>",
            "metadata": {
                "attachments": [{"filename": "deck.pdf"}],
                "artifact_comments": {"comments": [{"body": "note"}]},
            },
        },
        {
            "turn_index": 1,
            "type": "wakeup",
            "content": "![ignored](https://img.example/wake.png)",
            "metadata": {"attachments": [{"filename": "ignored.pdf"}]},
        },
    ]
    from src.server.services.history.replay.lines import line_of

    def _tsv(ev: str, data: dict[str, Any]) -> str:
        ln = line_of({"event": ev, "data": data})
        return f"{ln.event}\t\t{ln.data_json}"

    stored_lines = "\n".join(
        [
            _tsv("tool_calls", {"agent": "main", "tool_calls": [{"name": "Read"}, {"name": "Bash"}]}),
            _tsv("artifact", {"agent": "main", "artifact_type": "html_widget"}),
            _tsv(
                "message_chunk",
                {
                    "agent": "main",
                    "role": "assistant",
                    "content_type": "text",
                    "phase": "final_answer",
                    "content": "Here is ![chart](https://img.example/c.png)",
                },
            ),
        ]
    )
    resp_rows = [
        {
            "turn_index": 0,
            "status": "completed",
            "replay_facts": {},
            "lines": stored_lines,
        },
        {
            "turn_index": 1,
            "status": "completed",
            "replay_facts": {
                "images": {"/work/a.png": "https://cdn/a.png"},
                "legacy": {
                    "widgets": [{"id": "w1"}],
                    "verbatim": [
                        {"event": "tool_calls", "data": {"agent": "main", "tool_calls": [{"name": "Search"}]}},
                    ],
                },
            },
            "lines": None,
        },
    ]

    class _FakeCursor:
        def __init__(self) -> None:
            self._call = 0

        async def __aenter__(self) -> _FakeCursor:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def execute(self, sql: str, params: Any = None) -> None:
            self._call += 1

        async def fetchall(self) -> list[dict[str, Any]]:
            return query_rows if self._call == 1 else resp_rows

    class _FakeConn:
        def cursor(self, **kwargs: Any) -> _FakeCursor:
            return _FakeCursor()

    @asynccontextmanager
    async def _fake_get_db_connection():
        yield _FakeConn()

    def _forbid(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Forbidden I/O or replay call during DB-only share redaction summary")

    monkeypatch.setattr(db_pool, "get_db_connection", _fake_get_db_connection)
    monkeypatch.setattr(replay_mod, "finish_lines", _forbid)
    monkeypatch.setattr(storage_mod, "get_bytes", _forbid)
    monkeypatch.setattr(reader_mod, "CheckpointHistoryReader", _forbid)

    summary = await share_redaction.summarize_thread_share_redaction(str(uuid.uuid4()))
    assert summary == {
        "widgets": 2,
        "tool_calls": 3,
        "images": 3,
        "attachments": 2,
    }


@pytest.mark.asyncio
async def test_public_share_metadata_and_replay_headers_and_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/v1/public/shared/{token} and /replay emit Cache-Control: no-store + Clear-Site-Data and redact under allow_files=False."""
    from src.server.services.history.replay.lines import line_of

    tid = str(uuid.uuid4())
    share_token = "tok-pub-test-01"
    thread = {
        "conversation_thread_id": tid,
        "workspace_id": str(uuid.uuid4()),
        "user_id": "u1",
        "owner_id": "u1",
        "title": "SECRET_RAW_THREAD_TITLE_01",
        "share_token": share_token,
        "is_shared": True,
        "share_permissions": {"allow_files": False, "allow_download": True},
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }

    monkeypatch.setattr(
        public_app,
        "get_shared_thread",
        create_autospec(public_app.get_shared_thread, return_value=thread),
    )
    monkeypatch.setattr(
        share_redaction,
        "resolve_effective_share_permissions",
        create_autospec(
            share_redaction.resolve_effective_share_permissions,
            return_value=share_redaction.EffectiveSharePermissions(
                allow_files=False, allow_download=False
            ),
        ),
    )
    monkeypatch.setattr(
        share_redaction,
        "resolve_public_metadata_fields",
        create_autospec(
            share_redaction.resolve_public_metadata_fields,
            return_value={
                "title": "请总结核心结论",
                "workspace_name": None,
                "root_dir": None,
                "previous_root_dirs": [],
                "dir_name": None,
            },
        ),
    )
    monkeypatch.setattr(
        share_redaction,
        "read_strict_public_replay_page",
        create_autospec(
            share_redaction.read_strict_public_replay_page,
            return_value=(
                ReplayPage(
                    lines=[
                        line_of({"event": "replay_turn", "data": {"turn_index": 0, "status": "completed", "response_id": "r0"}}),
                        line_of({"event": "user_message", "data": {"turn_index": 0, "content": "请总结核心结论"}}),
                        line_of({"event": "tool_calls", "data": {"turn_index": 0, "agent": "main", "tool_calls": [{"name": "Read"}]}}),
                        line_of(
                            {
                                "event": "message_chunk",
                                "data": {
                                    "turn_index": 0,
                                    "agent": "main",
                                    "role": "assistant",
                                    "content_type": "text",
                                    "phase": "final_answer",
                                    "content": "最终观点",
                                },
                            }
                        ),
                    ],
                    first_turn_index=0,
                    has_more=True,
                ),
                "slices",
            ),
        ),
    )

    monkeypatch.setattr(
        public_app,
        "get_replay_thread_data",
        create_autospec(
            public_app.get_replay_thread_data,
            return_value=SimpleNamespace(
                thread_id=tid,
                queries=[{"turn_index": 0, "type": "initial", "content": "请总结核心结论", "metadata": {}}],
                responses_by_turn={0: {"turn_index": 0, "status": "completed", "conversation_response_id": "r0"}},
                open_runs=[],
                run_facts={},
            ),
        ),
    )
    monkeypatch.setattr(
        public_app,
        "get_thread_by_share_token",
        create_autospec(public_app.get_thread_by_share_token, return_value=thread),
    )
    monkeypatch.setattr(
        public_app,
        "resolve_link",
        create_autospec(public_app.resolve_link, return_value=None),
    )
    monkeypatch.setattr(
        public_app,
        "_shared_thread_for",
        create_autospec(public_app._shared_thread_for, return_value=(thread, None)),
    )

    app = FastAPI()
    app.include_router(public_app.router)
    client = TestClient(app)

    meta_resp = client.get(f"/api/v1/public/shared/{share_token}")
    assert meta_resp.status_code == 200
    assert meta_resp.headers.get("cache-control") == "no-store"
    assert meta_resp.headers.get("clear-site-data") == '"cache"'
    meta_body = meta_resp.json()
    assert meta_body["title"] == "请总结核心结论"
    assert "SECRET_RAW_THREAD_TITLE_01" not in meta_resp.text
    assert meta_body["permissions"]["allow_files"] is False
    assert meta_body["permissions"]["allow_download"] is False

    replay_resp = client.get(f"/api/v1/public/shared/{share_token}/replay")
    assert replay_resp.status_code == 200
    assert replay_resp.headers.get("cache-control") == "no-store"
    assert replay_resp.headers.get("clear-site-data") == '"cache"'
    assert "event: history_page" in replay_resp.text
    assert "最终观点" in replay_resp.text
    assert "tool_calls" not in replay_resp.text


def test_share_permissions_post_normalization_and_conflict_combinations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Severe #1: Real POST /threads/{id}/share & GET /share never elevate allow_files from False to True."""
    from src.server.app.threads import sharing as threads_sharing
    from src.server.services.shared_workspaces.access import Access

    tid = str(uuid.uuid4())
    ws_id = str(uuid.uuid4())
    share_token = "tok-norm-test"
    db_state: dict[str, Any] = {
        "conversation_thread_id": tid,
        "workspace_id": ws_id,
        "user_id": "admin-1",
        "owner_id": "admin-1",
        "is_shared": True,
        "share_token": share_token,
        "share_permissions": {"allow_files": False, "allow_download": True, "audience": "public"},
    }

    async def _get_thread(_tid: str):
        return dict(db_state)

    async def _upd_thread(_tid: str, **kwargs):
        if "is_shared" in kwargs:
            db_state["is_shared"] = kwargs["is_shared"]
        if "share_permissions" in kwargs:
            db_state["share_permissions"] = dict(kwargs["share_permissions"])
        return dict(db_state)

    monkeypatch.setattr(
        threads_sharing,
        "_authorize_thread_share",
        create_autospec(threads_sharing._authorize_thread_share, return_value=Access("admin-1")),
    )
    monkeypatch.setattr(
        threads_sharing,
        "get_thread_by_id",
        create_autospec(threads_sharing.get_thread_by_id, side_effect=_get_thread),
    )
    monkeypatch.setattr(
        threads_sharing,
        "update_thread_sharing",
        create_autospec(threads_sharing.update_thread_sharing, side_effect=_upd_thread),
    )
    monkeypatch.setattr(
        threads_sharing,
        "_require_admin_for_share_files",
        create_autospec(threads_sharing._require_admin_for_share_files, return_value=None),
    )
    monkeypatch.setattr(
        threads_sharing,
        "require_members_audience",
        create_autospec(threads_sharing.require_members_audience, return_value=None),
    )
    monkeypatch.setattr(
        threads_sharing,
        "refuse_public_link_of",
        create_autospec(threads_sharing.refuse_public_link_of, return_value=None),
    )
    monkeypatch.setattr(
        threads_sharing,
        "offered",
        create_autospec(threads_sharing.offered, return_value=["public", "organization"]),
    )
    monkeypatch.setattr(
        threads_sharing,
        "_record_share_change",
        create_autospec(threads_sharing._record_share_change, return_value=None),
    )
    monkeypatch.setattr(
        share_redaction,
        "post_update_thread_share_hook",
        create_autospec(
            share_redaction.post_update_thread_share_hook,
            return_value={"widgets": 0, "tool_calls": 0, "images": 0, "attachments": 0},
        ),
    )
    monkeypatch.setattr(
        share_access,
        "get_shared_thread",
        create_autospec(share_access.get_shared_thread, side_effect=lambda _tok: dict(db_state)),
    )

    app = FastAPI()
    app.include_router(threads_sharing.router)
    app.include_router(public_app.router)
    app.dependency_overrides[api_utils.get_current_user_id] = lambda: "admin-1"
    client = TestClient(app)

    # 1. Reading legacy conflict row collapses allow_download -> False
    r_get = client.get(f"/api/v1/threads/{tid}/share")
    assert r_get.status_code == 200
    assert r_get.json()["permissions"]["allow_files"] is False
    assert r_get.json()["permissions"]["allow_download"] is False

    # 2. Updating ONLY audience on legacy conflict row MUST NOT elevate allow_files to True
    r_aud = client.post(
        f"/api/v1/threads/{tid}/share",
        json={"is_shared": True, "permissions": {"audience": "organization"}},
    )
    assert r_aud.status_code == 200
    assert db_state["share_permissions"]["allow_files"] is False
    assert db_state["share_permissions"]["allow_download"] is False
    assert db_state["share_permissions"]["audience"] == "organization"

    # 3. Explicitly posting allow_files=False, allow_download=True MUST save allow_files=False, allow_download=False
    r_conflict = client.post(
        f"/api/v1/threads/{tid}/share",
        json={"is_shared": True, "permissions": {"allow_files": False, "allow_download": True}},
    )
    assert r_conflict.status_code == 200
    assert db_state["share_permissions"]["allow_files"] is False
    assert db_state["share_permissions"]["allow_download"] is False
    assert r_conflict.json()["permissions"]["allow_files"] is False
    assert r_conflict.json()["permissions"]["allow_download"] is False

    # Visitor file & media access stays 403 after POST
    r_files = client.get(f"/api/v1/public/shared/{share_token}/files")
    assert r_files.status_code == 403
    assert r_files.headers.get("cache-control") == "no-store"

    # 4. Posting ONLY allow_download=True (without allow_files) on allow_files=False thread MUST stay False
    r_dl_only = client.post(
        f"/api/v1/threads/{tid}/share",
        json={"is_shared": True, "permissions": {"allow_download": True}},
    )
    assert r_dl_only.status_code == 200
    assert db_state["share_permissions"]["allow_files"] is False
    assert db_state["share_permissions"]["allow_download"] is False

    # 5. Explicitly posting allow_files=True, allow_download=True succeeds
    r_both = client.post(
        f"/api/v1/threads/{tid}/share",
        json={"is_shared": True, "permissions": {"allow_files": True, "allow_download": True}},
    )
    assert r_both.status_code == 200
    assert db_state["share_permissions"]["allow_files"] is True
    assert db_state["share_permissions"]["allow_download"] is True

    # 6. Turning allow_files=False alone collapses allow_download=False
    r_off = client.post(
        f"/api/v1/threads/{tid}/share",
        json={"is_shared": True, "permissions": {"allow_files": False}},
    )
    assert r_off.status_code == 200
    assert db_state["share_permissions"]["allow_files"] is False
    assert db_state["share_permissions"]["allow_download"] is False


@pytest.mark.asyncio
async def test_controlled_media_thread_binding_blocks_unbound_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """High #2: Only sign & serve media demonstrably bound to the target thread."""
    tid = str(uuid.uuid4())
    other_tid = str(uuid.uuid4())
    ws_id = str(uuid.uuid4())
    own_att_url = f"https://cdn.example/private-media/attachments/{tid}/batch1/ok.png"
    own_img_url = f"https://cdn.example/private-media/response-images/{tid}/abc/chart.png"
    victim_att_url = "https://cdn.example/attachments/victim-att-id/secret.pdf"
    victim_img_url = f"https://cdn.example/private-media/response-images/{other_tid}/abc/leak.png"
    victim_widget_url = f"https://cdn.example/private-media/widgets/{other_tid}/w.json"

    monkeypatch.setattr(
        share_media,
        "_allowed_storage_origins",
        create_autospec(
            share_media._allowed_storage_origins,
            return_value={"https://cdn.example"},
        ),
    )

    thread = {"conversation_thread_id": tid, "workspace_id": ws_id, "user_id": "u1"}
    queries = [
        {"metadata": {"attachments": [{"url": own_att_url}]}},
    ]
    items = [
        # Attacker pastes victim URLs in user_message free text
        {
            "event": "user_message",
            "data": {
                "content": f"{victim_att_url} {victim_img_url} {victim_widget_url} https://evil.other/response-images/{tid}/x.png",
            },
        },
        # Assistant embeds legitimate captured image + foreign URL
        {
            "event": "message_chunk",
            "data": {
                "role": "assistant",
                "content": f"![ok]({own_img_url}) {victim_img_url} {victim_att_url}",
            },
        },
    ]

    bindings = await share_media.resolve_thread_media_bindings(thread, queries, items)
    assert own_att_url in bindings
    assert own_img_url in bindings
    assert victim_att_url not in bindings
    assert victim_img_url not in bindings
    assert victim_widget_url not in bindings
    assert f"https://evil.other/response-images/{tid}/x.png" not in bindings

    # Read side: even with a valid HMAC token, unbound attachments/, widgets/, response-images/,
    # and chat_upload upload_id not registered on thread_id MUST return 404 with Cache-Control: no-store.
    monkeypatch.setattr(
        share_media,
        "_load_thread_query_metadata_rows",
        create_autospec(share_media._load_thread_query_metadata_rows, return_value=[]),
    )
    monkeypatch.setattr(
        share_media,
        "read_controlled_object_bytes",
        create_autospec(share_media.read_controlled_object_bytes, return_value=b"SECRET_BYTES"),
    )

    for backend, bad_ref in (
        ("upstream", "attachments/victim-att-id/secret.pdf"),
        ("upstream", f"widgets/{other_tid}/w.json"),
        ("upstream", "widgets/legacy_unbound.json"),
        ("upstream", f"response-images/{other_tid}/abc/leak.png"),
        ("chat_upload", str(uuid.uuid4())),
    ):
        with pytest.raises(HTTPException) as exc:
            await share_media._serve_media_payload(
                thread_id=tid,
                workspace_id=ws_id,
                thread_owner_id="u1",
                backend=backend,
                ref_id=bad_ref,
                cache_control="no-store",
            )
        assert exc.value.status_code == 404
        assert (exc.value.headers or {}).get("Cache-Control") == "no-store"

    # Own thread-scoped media succeeds
    ok_resp = await share_media._serve_media_payload(
        thread_id=tid,
        workspace_id=ws_id,
        thread_owner_id="u1",
        backend="upstream",
        ref_id=f"private-media/response-images/{tid}/abc/chart.png",
        cache_control="no-store",
    )
    assert ok_resp.status_code == 200
    assert ok_resp.headers.get("cache-control") == "no-store"


def test_oss_upload_acl_fallback_and_s3_presigned_put_acl(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Medium: OSS upload retries without ACL on ACL-not-supported error; S3 presigned PUT signs ACL."""
    from src.utils.storage import oss_uploader, s3_compatible

    # 1. OSS upload_bytes & upload_file ACL fallback
    put_calls: list[Any] = []

    class _FakeOssAclError(Exception):
        code = "AccessControlListNotSupported"

    class _FakeOssClient:
        def put_object(self, req):
            put_calls.append(req)
            if getattr(req, "acl", None) == "private":
                raise _FakeOssAclError("Bucket does not allow ACLs")
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(oss_uploader, "get_oss_client", lambda: _FakeOssClient())
    monkeypatch.setattr(oss_uploader.OSSConfig, "BUCKET_NAME", "oss-test-bucket")

    assert oss_uploader.upload_bytes("private-media/t1/a.png", b"hello") is True
    assert len(put_calls) == 2
    assert getattr(put_calls[0], "acl", None) == "private"
    assert getattr(put_calls[1], "acl", None) is None

    put_calls.clear()
    sample = tmp_path / "sample.txt"
    sample.write_bytes(b"file-body")
    assert oss_uploader.upload_file("private-media/t1/sample.txt", str(sample)) is True
    assert len(put_calls) == 2
    assert getattr(put_calls[0], "acl", None) == "private"
    assert getattr(put_calls[1], "acl", None) is None

    # 2. S3 presigned single PUT attaches private ACL for non-avatar keys and skips avatars/
    presign_calls: list[dict[str, Any]] = []

    class _FakeS3Client:
        def generate_presigned_url(self, op, *, Params, ExpiresIn):
            presign_calls.append(dict(Params))
            return "https://store.example/presigned"

    monkeypatch.setattr(s3_compatible, "_get_client", lambda: _FakeS3Client())
    sha = "a" * 64
    out_priv = s3_compatible.get_signed_upload_url(
        f"blobs/u1/{sha}", sha256_hex=sha, content_length=10, content_type="application/octet-stream"
    )
    assert out_priv is not None
    assert presign_calls[-1].get("ACL") == "private"
    assert out_priv[1].get("x-amz-acl") == "private"

    out_avatar = s3_compatible.get_signed_upload_url(
        "avatars/u1.png", sha256_hex=sha, content_length=10, content_type="image/png"
    )
    assert out_avatar is not None
    assert "ACL" not in presign_calls[-1]
    assert "x-amz-acl" not in out_avatar[1]


def test_public_error_responses_carry_no_store_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Low: 404/error responses on public share, file, and media routes carry Cache-Control: no-store."""
    monkeypatch.setattr(
        public_app,
        "resolve_link",
        create_autospec(public_app.resolve_link, return_value=None),
    )
    monkeypatch.setattr(
        public_app,
        "_shared_thread_for",
        create_autospec(
            public_app._shared_thread_for,
            side_effect=HTTPException(status_code=404, detail="Shared link not found"),
        ),
    )
    not_found_shared = create_autospec(
        share_access.get_shared_thread,
        side_effect=HTTPException(status_code=404, detail="Shared thread not found"),
    )
    monkeypatch.setattr(share_access, "get_shared_thread", not_found_shared)
    monkeypatch.setattr(public_app, "get_shared_thread", not_found_shared)

    app = FastAPI()
    app.include_router(public_app.router)
    client = TestClient(app)

    for url in (
        "/api/v1/public/shared/invalid-tok",
        "/api/v1/public/shared/invalid-tok/replay",
        "/api/v1/public/shared/invalid-tok/files?path=../etc/passwd",
        "/api/v1/public/shared/invalid-tok/media/bad-ref",
    ):
        r = client.get(url)
        assert r.status_code == 404, f"Expected 404 for {url}, got {r.status_code}"
        assert r.headers.get("cache-control") == "no-store", f"Missing no-store on {url}"
