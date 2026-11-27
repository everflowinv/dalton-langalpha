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

    # 6. --warm-shared-slices opens & closes BOTH app pool and checkpointer pool and distinguishes warmed/cache_hit/stub
    from contextlib import asynccontextmanager
    import psycopg
    from src.server.database import pool as db_pool
    from src.server.database.conversation import replay_rows as replay_rows_mod
    from src.server.services.history import replay as replay_mod

    shared_tid_cold = str(uuid.uuid4())
    shared_tid_warm = str(uuid.uuid4())
    shared_tid_stub = str(uuid.uuid4())
    lifecycle: list[str] = []

    async def _fake_open_pools():
        lifecycle.append("open_pools")
        return ("fake-pool", "fake-cp", True, True)

    async def _fake_close_pools(conv_pool, cp, opened_app, opened_cp):
        assert opened_app is True and opened_cp is True
        lifecycle.append("close_pools")

    warm_cur = create_autospec(psycopg.AsyncCursor, instance=True)
    warm_cur.__aenter__.return_value = warm_cur
    warm_cur.__aexit__.return_value = False
    warm_cur.execute.return_value = None
    warm_cur.fetchall.return_value = [
        {"thread_id": shared_tid_cold},
        {"thread_id": shared_tid_warm},
        {"thread_id": shared_tid_stub},
    ]
    warm_conn = create_autospec(psycopg.AsyncConnection, instance=True)
    warm_conn.cursor.return_value = warm_cur

    @asynccontextmanager
    async def _fake_warm_conn():
        yield warm_conn

    async def _fake_read_replay(rows, **kwargs):
        if rows.thread_id == shared_tid_cold:
            if kwargs.get("persist", True):
                share_redaction.mark_cold_projection_miss()
            return ReplayPage(lines=[], first_turn_index=0, has_more=False), "checkpoint"
        if rows.thread_id == shared_tid_warm:
            return ReplayPage(lines=[], first_turn_index=0, has_more=False), "checkpoint"
        return ReplayPage(lines=[], first_turn_index=0, has_more=False), "stub"

    read_warm_spy = create_autospec(
        replay_mod.read_replay_page,
        side_effect=_fake_read_replay,
    )
    monkeypatch.setattr(
        ops_script,
        "_ensure_cli_pools_open",
        create_autospec(ops_script._ensure_cli_pools_open, side_effect=_fake_open_pools),
    )
    monkeypatch.setattr(
        ops_script,
        "_close_cli_pools",
        create_autospec(ops_script._close_cli_pools, side_effect=_fake_close_pools),
    )
    monkeypatch.setattr(db_pool, "get_db_connection", _fake_warm_conn)
    monkeypatch.setattr(
        replay_rows_mod,
        "get_replay_thread_data",
        create_autospec(
            replay_rows_mod.get_replay_thread_data,
            side_effect=lambda tid: SimpleNamespace(thread_id=tid),
        ),
    )
    monkeypatch.setattr(replay_mod, "read_replay_page", read_warm_spy)
    monkeypatch.setattr(
        replay_mod, "lines_epoch", create_autospec(replay_mod.lines_epoch, return_value="warm-epoch-1")
    )

    warm_res = await ops_script.warm_shared_slices(limit_threads=10, concurrency=2)
    assert lifecycle == ["open_pools", "close_pools"]
    assert warm_res["warmed"] == 1
    assert warm_res["persist_failed"] == 0
    assert warm_res["cache_hit"] == 1
    assert warm_res["stub"] == 1
    assert warm_res["failed"] == 0
    assert read_warm_spy.await_count == 4
    assert sum(1 for call in read_warm_spy.await_args_list if call.kwargs["persist"] is True) == 3
    assert sum(1 for call in read_warm_spy.await_args_list if call.kwargs["persist"] is False) == 1


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

    import psycopg

    call_idx = [0]
    sum_cur = create_autospec(psycopg.AsyncCursor, instance=True)
    sum_cur.__aenter__.return_value = sum_cur
    sum_cur.__aexit__.return_value = False

    async def _exec(sql: Any, params: Any = None) -> None:
        call_idx[0] += 1

    async def _fetchall() -> list[dict[str, Any]]:
        return query_rows if call_idx[0] == 1 else resp_rows

    sum_cur.execute.side_effect = _exec
    sum_cur.fetchall.side_effect = _fetchall

    sum_conn = create_autospec(psycopg.AsyncConnection, instance=True)
    sum_conn.cursor.return_value = sum_cur

    @asynccontextmanager
    async def _fake_get_db_connection():
        yield sum_conn

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
    """High #2 & Codex Med #5: Only sign & serve media in the authoritative ThreadMediaRegistry."""
    tid = str(uuid.uuid4())
    other_tid = str(uuid.uuid4())
    ws_id = str(uuid.uuid4())
    own_att_url = f"https://cdn.example/private-media/attachments/{tid}/batch1/ok.png"
    own_img_url = f"https://cdn.example/private-media/response-images/{tid}/abc/chart.png"
    unregistered_img_url = f"https://cdn.example/private-media/response-images/{tid}/abc/unregistered.png"
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
    responses_by_turn = {
        0: {"replay_facts": {"images": {"/workspace/chart.png": own_img_url}}},
    }
    items = [
        # Attacker pastes victim URLs in user_message free text
        {
            "event": "user_message",
            "data": {
                "content": f"{victim_att_url} {victim_img_url} {victim_widget_url} https://evil.other/response-images/{tid}/x.png",
            },
        },
        # Assistant embeds legitimate captured image + unregistered same-thread capture + foreign URL
        {
            "event": "message_chunk",
            "data": {
                "role": "assistant",
                "content": f"![ok]({own_img_url}) ![unreg]({unregistered_img_url}) {victim_img_url} {victim_att_url}",
            },
        },
    ]

    orig_load_reg = share_media._load_thread_media_registry_from_db
    reg_for_sign = share_media._collect_thread_media_registry(
        tid,
        owner_id="user-owner",
        queries=queries,
        responses_by_turn=responses_by_turn,
        items=items,
    )
    monkeypatch.setattr(
        share_media,
        "_load_thread_media_registry_from_db",
        create_autospec(
            orig_load_reg,
            return_value=reg_for_sign,
        ),
    )

    bindings = await share_media.resolve_thread_media_bindings(
        thread, queries, items, responses_by_turn=responses_by_turn
    )
    assert bindings is not None
    assert own_att_url in bindings
    assert own_img_url in bindings
    assert unregistered_img_url not in bindings
    assert victim_att_url not in bindings
    assert victim_img_url not in bindings
    assert victim_widget_url not in bindings
    assert f"https://evil.other/response-images/{tid}/x.png" not in bindings

    # Read side: even with a valid HMAC token, unbound attachments/, widgets/, response-images/
    # (including unregistered same-thread response-images!), and unregistered chat_upload MUST return 404 + no-store.
    trusted_registry = share_media.ThreadMediaRegistry(
        captured_image_keys={f"response-images/{tid}/abc/chart.png"}
    )
    monkeypatch.setattr(
        share_media,
        "_load_thread_media_registry_from_db",
        create_autospec(
            orig_load_reg,
            return_value=trusted_registry,
        ),
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
        ("upstream", f"private-media/response-images/{tid}/abc/unregistered.png"),
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

    # Registered captured image succeeds
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
    """Medium: OSS upload retries without ACL on ACL-not-supported error; S3 presigned PUT probes ACL mode."""
    import alibabacloud_oss_v2 as oss
    from src.utils.storage import oss_uploader, s3_compatible

    share_media.reset_bucket_acl_cache_for_test()

    # 1. OSS upload_bytes & upload_file ACL fallback (using create_autospec on real oss.Client)
    put_calls: list[Any] = []

    class _FakeOssAclError(Exception):
        code = "AccessControlListNotSupported"

    def _oss_put(req: Any) -> Any:
        put_calls.append(req)
        if getattr(req, "acl", None) == "private":
            raise _FakeOssAclError("Bucket does not allow ACLs")
        return SimpleNamespace(status_code=200)

    oss_client = create_autospec(oss.Client, instance=True)
    oss_client.put_object.side_effect = _oss_put
    monkeypatch.setattr(oss_uploader, "get_oss_client", lambda: oss_client)
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

    # 2. S3 presigned single PUT probes ownership controls before presigning (using create_autospec)
    class _S3PresignClientSpec:
        def get_bucket_ownership_controls(self, *, Bucket: str) -> dict[str, Any]: ...
        def generate_presigned_url(
            self, ClientMethod: str, *, Params: dict[str, Any], ExpiresIn: int
        ) -> str: ...

    presign_calls: list[dict[str, Any]] = []
    s3_client = create_autospec(_S3PresignClientSpec, instance=True)
    s3_client.get_bucket_ownership_controls.return_value = {
        "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerPreferred"}]}
    }
    s3_client.generate_presigned_url.side_effect = (
        lambda op, *, Params, ExpiresIn: (presign_calls.append(dict(Params)), "https://store.example/presigned")[1]
    )
    monkeypatch.setattr(s3_compatible, "_get_client", lambda: s3_client)
    monkeypatch.setattr(s3_compatible.StorageConfig, "BUCKET_NAME", "acl-pref-bucket")
    monkeypatch.setattr(s3_compatible.StorageConfig, "ENDPOINT_URL", "https://s3.custom.example")
    monkeypatch.delenv("DALTON_STORAGE_DISABLE_OBJECT_ACL", raising=False)
    monkeypatch.delenv("STORAGE_DISABLE_OBJECT_ACL", raising=False)

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

    # BucketOwnerEnforced bucket MUST omit ACL and x-amz-acl BEFORE presigning
    share_media.reset_bucket_acl_cache_for_test()
    s3_client.get_bucket_ownership_controls.return_value = {
        "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}
    }
    monkeypatch.setattr(s3_compatible.StorageConfig, "BUCKET_NAME", "enforced-bucket")
    out_enforced = s3_compatible.get_signed_upload_url(
        f"blobs/u1/{sha}", sha256_hex=sha, content_length=10, content_type="application/octet-stream"
    )
    assert out_enforced is not None
    assert "ACL" not in presign_calls[-1]
    assert "x-amz-acl" not in out_enforced[1]

    # Explicit disable env skips probing and omits ACL
    share_media.reset_bucket_acl_cache_for_test()
    s3_client.get_bucket_ownership_controls.reset_mock()
    monkeypatch.setenv("DALTON_STORAGE_DISABLE_OBJECT_ACL", "1")
    out_disabled = s3_compatible.get_signed_upload_url(
        f"blobs/u1/{sha}", sha256_hex=sha, content_length=10, content_type="application/octet-stream"
    )
    assert out_disabled is not None
    assert "ACL" not in presign_calls[-1]
    assert "x-amz-acl" not in out_disabled[1]
    s3_client.get_bucket_ownership_controls.assert_not_called()


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

    # 422 validation errors on public routes MUST also carry Cache-Control: no-store
    r_422 = client.get("/api/v1/public/shared/invalid-tok/replay?before_turn=not-an-int")
    assert r_422.status_code == 422
    assert r_422.headers.get("cache-control") == "no-store"

    # Non-ASCII media tokens MUST return 401/404 instead of 500
    with pytest.raises(HTTPException) as exc_owner:
        share_media.verify_owner_media_token("中文.令牌", expected_thread_id=str(uuid.uuid4()))
    assert exc_owner.value.status_code == 401
    with pytest.raises(HTTPException) as exc_pub:
        share_media.verify_public_media_ref("中文.令牌", expected_thread_id=str(uuid.uuid4()))
    assert exc_pub.value.status_code == 404


@pytest.mark.asyncio
async def test_nested_thinking_real_cold_projection_then_cached_public_sse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex High #1: Real cold projection writes depth-stripped turn_slices; subsequent public SSE hits cache cleanly."""
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from src.server.database.conversation import replay_rows as replay_rows_mod
    from src.server.database.conversation import turn_slices as slices_db
    from src.server.services.history import replay as replay_mod
    from src.server.services.history import slices as slices_mod
    from src.server.services.history.reader import CheckpointHistoryReader, TurnAnchor

    tid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    share_token = "tok-nested-cold-warm"
    fake_thread = {
        "conversation_thread_id": tid,
        "workspace_id": str(uuid.uuid4()),
        "user_id": "u1",
        "owner_id": "u1",
        "title": "Nested test",
        "share_token": share_token,
        "is_shared": True,
        "share_permissions": {"allow_files": False, "allow_download": False},
        "latest_checkpoint_id": "cp-tail-0",
    }
    rows = replay_rows_mod.ThreadRows(
        thread_id=tid,
        owner_id="u1",
        thread=fake_thread,
        queries=[
            {
                "conversation_query_id": str(uuid.uuid4()),
                "turn_index": 0,
                "type": "initial",
                "content": "请分析财报",
                "metadata": {},
            }
        ],
        responses_by_turn={
            0: {
                "conversation_response_id": rid,
                "turn_index": 0,
                "status": "completed",
                "replay_facts": {},
            }
        },
        usages=[],
        provenance=[],
        run_facts=[],
        open_runs={},
        failed_steers=[],
    )

    anchor = TurnAnchor(
        turn_ordinal=0,
        input_checkpoint_id="cp-in-0",
        tail_checkpoint_id="cp-tail-0",
        turn_index=0,
        end_checkpoint_id="cp-tail-0",
    )
    nested_ai = AIMessage(
        id="ai-nested",
        content=(
            "<thinking>outer_start <thinking>inner_block</thinking> "
            "NESTED_OUTER_LEAK_MUST_NEVER_APPEAR</thinking>"
            "干净的公开结论。"
        ),
    )
    fake_slice = slices_mod.TurnSlice(
        anchor=anchor,
        messages=[HumanMessage(id="h-0", content="请分析财报"), nested_ai],
    )

    reader_stub = create_autospec(CheckpointHistoryReader, instance=True)
    reader_stub.serde = JsonPlusSerializer()
    reader_stub.aget_turn_anchors.return_value = ([anchor], "cp-tail-0")
    reader_stub.aget_turn_slices.return_value = [fake_slice]
    reader_stub.aget_tip_interrupts.return_value = []
    monkeypatch.setattr(
        CheckpointHistoryReader,
        "get_instance",
        classmethod(lambda cls: reader_stub),
    )

    # Real in-memory turn_slices store backing slices_db calls
    stored_slices: dict[str, slices_db.StoredSlice] = {}

    async def _get_turn_lines(rids: list[str]) -> dict[str, slices_db.StoredSlice]:
        return {r: stored_slices[r] for r in rids if r in stored_slices}

    async def _get_turn_slices(rids: list[str]) -> dict[str, slices_db.StoredSlice]:
        return {r: stored_slices[r] for r in rids if r in stored_slices}

    async def _upsert_turn_slices(turn_rows: list[slices_db.TurnRow]) -> None:
        for tr in turn_rows:
            stored_slices[tr.response_id] = tr.slice

    async def _update_turns(updates: list[slices_db.TurnUpdate]) -> None:
        for u in updates:
            cur = stored_slices.get(u.response_id)
            if cur is not None:
                stored_slices[u.response_id] = cur._replace(
                    lines_key=u.lines_key or cur.lines_key,
                    lines=u.lines or cur.lines,
                )

    monkeypatch.setattr(
        slices_db, "get_turn_lines", create_autospec(slices_db.get_turn_lines, side_effect=_get_turn_lines)
    )
    monkeypatch.setattr(
        slices_db, "get_turn_slices", create_autospec(slices_db.get_turn_slices, side_effect=_get_turn_slices)
    )
    monkeypatch.setattr(
        slices_db, "upsert_turn_slices", create_autospec(slices_db.upsert_turn_slices, side_effect=_upsert_turn_slices)
    )
    monkeypatch.setattr(
        slices_db, "update_turns", create_autospec(slices_db.update_turns, side_effect=_update_turns)
    )
    monkeypatch.setattr(
        public_app, "get_shared_thread", create_autospec(public_app.get_shared_thread, return_value=fake_thread)
    )
    monkeypatch.setattr(
        share_access, "get_shared_thread", create_autospec(share_access.get_shared_thread, return_value=fake_thread)
    )
    monkeypatch.setattr(
        public_app, "get_replay_thread_data", create_autospec(public_app.get_replay_thread_data, return_value=rows)
    )

    # 1. Cold projection with persist=True writes stripped lines into turn_slices
    page_cold, src_cold = await replay_mod.read_replay_page(rows, persist=True)
    assert src_cold == "checkpoint"
    assert rid in stored_slices
    assert stored_slices[rid].lines is not None
    assert "NESTED_OUTER_LEAK_MUST_NEVER_APPEAR" not in stored_slices[rid].lines
    assert "干净的公开结论。" in stored_slices[rid].lines
    assert reader_stub.aget_turn_slices.await_count == 1

    # 2. Subsequent public SSE replay hits cached turn_slices (zero cold projection) and leaks nothing
    app = FastAPI()
    app.include_router(public_app.router)
    client = TestClient(app)
    with share_redaction.track_cold_projection_miss() as cold_hit:
        resp = client.get(f"/api/v1/public/shared/{share_token}/replay")
    assert resp.status_code == 200
    assert cold_hit[0] is False  # served directly from warm turn_slices
    assert reader_stub.aget_turn_slices.await_count == 1
    assert "NESTED_OUTER_LEAK_MUST_NEVER_APPEAR" not in resp.text
    assert "干净的公开结论。" in resp.text


@pytest.mark.asyncio
async def test_concurrent_share_permissions_partial_update_and_download_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex High #2 & Med #4: Concurrent partial POSTs merge atomically without re-opening allow_files; download-only normalizes after merge."""
    from contextlib import asynccontextmanager
    import httpx
    import psycopg
    from src.server.app.threads import sharing as threads_sharing
    from src.server.database import pool as db_pool
    from src.server.services.shared_workspaces.access import Access

    tid = str(uuid.uuid4())
    ws_id = str(uuid.uuid4())
    db_row: dict[str, Any] = {
        "conversation_thread_id": tid,
        "workspace_id": ws_id,
        "user_id": "admin-1",
        "owner_id": "admin-1",
        "is_shared": True,
        "share_token": "tok-concurrent",
        "share_permissions": {"allow_files": True, "allow_download": False, "audience": "public"},
    }

    # Barrier ensures both POSTs read the stale initial state (allow_files=True) BEFORE either writes
    both_read = asyncio.Event()
    readers = [0]
    write_lock = asyncio.Lock()

    async def _stale_get_thread(_tid: str) -> dict[str, Any]:
        snap = {**db_row, "share_permissions": dict(db_row["share_permissions"])}
        readers[0] += 1
        if readers[0] >= 2:
            both_read.set()
        await asyncio.wait_for(both_read.wait(), timeout=2.0)
        return snap

    # Simulate PostgreSQL atomic JSONB CASE merge (identical semantics to append_atomic_share_permissions_set)
    async def _atomic_update_thread(_tid: str, **kwargs: Any) -> dict[str, Any]:
        patch = kwargs.get("share_permissions_patch")
        # Force audience-only writer to commit AFTER allow_files=False writer
        if isinstance(patch, dict) and "allow_files" not in patch:
            await asyncio.sleep(0.03)
        async with write_lock:
            if "is_shared" in kwargs:
                db_row["is_shared"] = kwargs["is_shared"]
            if patch is not None:
                merged = {**db_row["share_permissions"], **patch}
                if not merged.get("allow_files"):
                    merged["allow_files"] = False
                    merged["allow_download"] = False
                db_row["share_permissions"] = merged
            elif "share_permissions" in kwargs and kwargs["share_permissions"] is not None:
                db_row["share_permissions"] = dict(kwargs["share_permissions"])
            return {**db_row, "share_permissions": dict(db_row["share_permissions"])}

    orig_atomic_update = share_redaction.atomic_update_thread_sharing
    upd_mock = create_autospec(
        orig_atomic_update, side_effect=_atomic_update_thread
    )
    orig_get_thread = threads_sharing.get_thread_by_id
    monkeypatch.setattr(
        threads_sharing,
        "_authorize_thread_share",
        create_autospec(threads_sharing._authorize_thread_share, return_value=Access("admin-1")),
    )
    monkeypatch.setattr(
        threads_sharing,
        "get_thread_by_id",
        create_autospec(orig_get_thread, side_effect=_stale_get_thread),
    )
    monkeypatch.setattr(share_redaction, "atomic_update_thread_sharing", upd_mock)
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
        create_autospec(share_redaction.post_update_thread_share_hook, return_value=None),
    )

    app = FastAPI()
    app.include_router(threads_sharing.router)
    app.dependency_overrides[api_utils.get_current_user_id] = lambda: "admin-1"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r_aud, r_close = await asyncio.gather(
            client.post(
                f"/api/v1/threads/{tid}/share",
                json={"is_shared": True, "permissions": {"audience": "organization"}},
            ),
            client.post(
                f"/api/v1/threads/{tid}/share",
                json={"is_shared": True, "permissions": {"allow_files": False}},
            ),
        )
    assert r_aud.status_code == 200
    assert r_close.status_code == 200
    # Final DB state MUST keep allow_files=False while applying audience="organization"
    assert db_row["share_permissions"]["allow_files"] is False
    assert db_row["share_permissions"]["allow_download"] is False
    assert db_row["share_permissions"]["audience"] == "organization"
    assert r_aud.json()["permissions"]["allow_files"] is False

    # Partial update submitting ONLY allow_download=True:
    # - Under allow_files=False: collapses to allow_download=False
    # - Under allow_files=True: preserves allow_download=True!
    async def _direct_get_thread(_tid: str) -> dict[str, Any]:
        return {**db_row, "share_permissions": dict(db_row["share_permissions"])}

    monkeypatch.setattr(
        threads_sharing,
        "get_thread_by_id",
        create_autospec(orig_get_thread, side_effect=_direct_get_thread),
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # allow_files=False -> download stays False
        r_dl_false = await client.post(
            f"/api/v1/threads/{tid}/share",
            json={"is_shared": True, "permissions": {"allow_download": True}},
        )
        assert r_dl_false.status_code == 200
        assert db_row["share_permissions"]["allow_files"] is False
        assert db_row["share_permissions"]["allow_download"] is False

        # Enable allow_files=True, then submit ONLY allow_download=True
        db_row["share_permissions"] = {"allow_files": True, "allow_download": False, "audience": "public"}
        r_dl_true = await client.post(
            f"/api/v1/threads/{tid}/share",
            json={"is_shared": True, "permissions": {"allow_download": True}},
        )
        assert r_dl_true.status_code == 200
        assert db_row["share_permissions"]["allow_files"] is True
        assert db_row["share_permissions"]["allow_download"] is True
        assert r_dl_true.json()["permissions"]["allow_download"] is True

    # Verify Dalton's atomic_update_thread_sharing actually issues the atomic JSONB CASE SQL
    executed_sql: list[str] = []
    sql_cur = create_autospec(psycopg.AsyncCursor, instance=True)
    sql_cur.__aenter__.return_value = sql_cur
    sql_cur.__aexit__.return_value = False
    sql_cur.execute.side_effect = lambda sql, params=None: executed_sql.append(str(sql))
    sql_cur.fetchone.return_value = db_row
    sql_conn = create_autospec(psycopg.AsyncConnection, instance=True)
    sql_conn.cursor.return_value = sql_cur

    @asynccontextmanager
    async def _fake_conn():
        yield sql_conn

    monkeypatch.setattr(db_pool, "get_db_connection", _fake_conn)
    await orig_atomic_update(
        tid, is_shared=True, share_permissions_patch={"audience": "organization"}
    )
    assert executed_sql
    assert "COALESCE(share_permissions, '{}'::jsonb) || %s::jsonb" in executed_sql[0]
    assert '{"allow_files": false, "allow_download": false}' in executed_sql[0]


@pytest.mark.asyncio
async def test_cross_user_upload_id_rejected_at_ingest_issuance_and_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grok High #4: Foreign user's upload_id in the same workspace is rejected at ingest, signing, and serving."""
    from src.dalton.services import chat_uploads as chat_uploads_svc
    from src.dalton.database import chat_uploads as chat_uploads_db

    ws_id = str(uuid.uuid4())
    tid = str(uuid.uuid4())
    victim_uid = str(uuid.uuid4())
    own_uid = str(uuid.uuid4())

    victim_row = {
        "upload_id": victim_uid,
        "user_id": "user-victim",
        "workspace_id": ws_id,
        "object_key": f"private-media/chat-uploads/{ws_id}/{victim_uid}/secret.pdf",
        "filename": "secret.pdf",
        "safe_filename": "secret.pdf",
        "mime_type": "application/pdf",
        "size_bytes": 1024,
        "state": "ready",
        "cleaned_at": None,
    }
    own_row = {
        "upload_id": own_uid,
        "user_id": "user-attacker",
        "workspace_id": ws_id,
        "object_key": f"private-media/chat-uploads/{ws_id}/{own_uid}/mine.pdf",
        "filename": "mine.pdf",
        "safe_filename": "mine.pdf",
        "mime_type": "application/pdf",
        "size_bytes": 512,
        "state": "ready",
        "cleaned_at": None,
    }

    # 1. Ingest: build_chat_upload_history_metadata MUST NOT persist victim's upload_id
    monkeypatch.setattr(
        chat_uploads_db,
        "get_uploads_by_ids",
        create_autospec(
            chat_uploads_db.get_uploads_by_ids,
            return_value={victim_uid.lower(): victim_row, own_uid.lower(): own_row},
        ),
    )
    hist_meta = await chat_uploads_svc.build_chat_upload_history_metadata(
        [
            {"type": "chat_upload", "upload_id": victim_uid, "name": "secret.pdf"},
            {"type": "chat_upload", "upload_id": own_uid, "name": "mine.pdf"},
        ],
        user_id="user-attacker",
        workspace_id=ws_id,
    )
    assert len(hist_meta) == 1
    assert hist_meta[0]["upload_id"] == own_uid
    assert all(m.get("upload_id") != victim_uid for m in hist_meta)

    # 2. Issuance & Serving: even if victim_uid existed in legacy query metadata of attacker's thread,
    #    resolve_thread_media_bindings & _serve_media_payload enforce user_id ownership.
    async def _fake_fetch_ready(
        workspace_id: str,
        upload_ids: Any,
        *,
        allowed_user_ids: Any = None,
    ) -> dict[str, dict[str, Any]]:
        allowed = set(allowed_user_ids or ())
        out: dict[str, dict[str, Any]] = {}
        for r in (victim_row, own_row):
            if r["upload_id"] in set(upload_ids) and r["workspace_id"] == workspace_id:
                if allowed_user_ids is None or r["user_id"] in allowed:
                    out[r["upload_id"]] = r
        return out

    monkeypatch.setattr(
        share_media,
        "_fetch_ready_chat_uploads",
        create_autospec(share_media._fetch_ready_chat_uploads, side_effect=_fake_fetch_ready),
    )

    thread = {"conversation_thread_id": tid, "workspace_id": ws_id, "user_id": "user-attacker"}
    queries = [
        {"metadata": {"attachments": [{"upload_id": victim_uid}, {"upload_id": own_uid}]}},
    ]
    reg = share_media._collect_thread_media_registry(tid, owner_id="user-attacker", queries=queries)
    monkeypatch.setattr(
        share_media,
        "_load_thread_media_registry_from_db",
        create_autospec(share_media._load_thread_media_registry_from_db, return_value=reg),
    )
    bindings = await share_media.resolve_thread_media_bindings(thread, queries, [])
    assert bindings is not None
    assert f"upload_id:{victim_uid}" not in bindings
    assert f"upload_id:{own_uid}" in bindings

    # Read side refuses victim_uid with 404
    with pytest.raises(HTTPException) as exc:
        await share_media._serve_media_payload(
            thread_id=tid,
            workspace_id=ws_id,
            thread_owner_id="user-attacker",
            backend="chat_upload",
            ref_id=victim_uid,
            cache_control="no-store",
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_media_registry_fail_closed_steering_and_subagent_protected_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex Med #5: Empty/error origins & closed staging pool fail closed; steering messages[] rewritten; subagent_runs.replay_facts included."""
    from contextlib import asynccontextmanager
    import psycopg
    from src.server.database import chat_attachments as att_db
    from src.server.database import pool as db_pool

    tid = str(uuid.uuid4())
    ws_id = str(uuid.uuid4())
    steer_att_url = f"https://cdn.example/private-media/attachments/{tid}/s1/steer.png"
    staged_att_url = "https://cdn.example/private-media/attachments/att-staged-1/doc.png"
    own_steer_uid = str(uuid.uuid4())
    foreign_steer_uid = str(uuid.uuid4())

    # 1. Empty or failed storage origin resolution fails closed
    orig_resolve_targets = share_media.resolve_active_storage_targets
    orig_get_att = att_db.get_attachment
    monkeypatch.setattr(
        share_media,
        "resolve_active_storage_targets",
        create_autospec(orig_resolve_targets, return_value=[]),
    )
    assert share_media._is_url_from_configured_storage(steer_att_url) is False

    monkeypatch.setattr(
        share_media,
        "resolve_active_storage_targets",
        create_autospec(orig_resolve_targets, side_effect=RuntimeError("env broken")),
    )
    assert share_media._is_url_from_configured_storage(steer_att_url) is False

    # 2. Closed attachment staging pool fails closed
    monkeypatch.setattr(
        att_db,
        "get_attachment",
        create_autospec(orig_get_att, side_effect=RuntimeError("connection pool is not open")),
    )
    assert (
        await share_media._verify_staged_attachment_owner(
            "att-staged-1", "attachments/att-staged-1/doc.png", owner_id="u1"
        )
        is False
    )

    # 3. steering_delivered messages[] attachments use own registration & same rewrite logic
    monkeypatch.setattr(
        share_media,
        "_allowed_storage_origins",
        create_autospec(share_media._allowed_storage_origins, return_value={"https://cdn.example"}),
    )
    monkeypatch.setattr(
        att_db,
        "get_attachment",
        create_autospec(
            orig_get_att,
            return_value={"object_key": "attachments/att-staged-1/doc.png", "user_id": "u1"},
        ),
    )

    async def _fake_ready(workspace_id: str, upload_ids: Any, *, allowed_user_ids: Any = None):
        allowed = set(allowed_user_ids or ())
        rows = {
            own_steer_uid: {"upload_id": own_steer_uid, "user_id": "u1", "workspace_id": ws_id},
            foreign_steer_uid: {"upload_id": foreign_steer_uid, "user_id": "u-other", "workspace_id": ws_id},
        }
        return {k: v for k, v in rows.items() if k in set(upload_ids) and v["user_id"] in allowed}

    monkeypatch.setattr(
        share_media,
        "_fetch_ready_chat_uploads",
        create_autospec(share_media._fetch_ready_chat_uploads, side_effect=_fake_ready),
    )

    thread = {"conversation_thread_id": tid, "workspace_id": ws_id, "user_id": "u1"}
    items = [
        {
            "event": "steering_delivered",
            "data": {
                "messages": [
                    {
                        "content": f"See {steer_att_url}",
                        "attachments": [
                            {"url": steer_att_url},
                            {"upload_id": own_steer_uid},
                            {"upload_id": foreign_steer_uid},
                        ],
                        "metadata": {"attachments": [{"url": staged_att_url}]},
                    }
                ]
            },
        }
    ]
    reg_steer = share_media._collect_thread_media_registry(tid, owner_id="u1", items=items)
    monkeypatch.setattr(
        share_media,
        "_load_thread_media_registry_from_db",
        create_autospec(share_media._load_thread_media_registry_from_db, return_value=reg_steer),
    )
    bindings = await share_media.resolve_thread_media_bindings(thread, [], items)
    assert bindings is not None
    assert steer_att_url in bindings
    assert staged_att_url in bindings
    assert f"upload_id:{own_steer_uid}" in bindings
    assert f"upload_id:{foreign_steer_uid}" not in bindings

    rewritten = share_media.rewrite_items_for_public_media(
        items, thread_id=tid, share_token="tok-steer", bindings=bindings
    )
    msg0 = rewritten[0]["data"]["messages"][0]
    assert "/api/v1/public/shared/tok-steer/media/" in msg0["content"]
    assert msg0["attachments"][0]["url"].startswith("/api/v1/public/shared/tok-steer/media/")
    assert msg0["attachments"][1]["url"].startswith("/api/v1/public/shared/tok-steer/media/")
    assert "url" not in msg0["attachments"][2]
    assert msg0["metadata"]["attachments"][0]["url"].startswith("/api/v1/public/shared/tok-steer/media/")

    # 4. _load_thread_protected_urls_map includes subagent_runs.replay_facts
    sub_img = f"https://cdn.example/private-media/response-images/{tid}/sub/chart.png"
    q_idx = [0]
    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = False

    async def _exec(sql: Any, params: Any = None) -> None:
        q_idx[0] += 1

    async def _fetchall() -> list[dict[str, Any]]:
        if q_idx[0] == 3:
            return [{"thread_id": tid, "replay_facts": {"images": {"/w/sub.png": sub_img}}}]
        return []

    cur.execute.side_effect = _exec
    cur.fetchall.side_effect = _fetchall
    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    conn.cursor.return_value = cur

    @asynccontextmanager
    async def _fake_conn():
        yield conn

    monkeypatch.setattr(db_pool, "get_db_connection", _fake_conn)
    protected_map = await share_redaction._load_thread_protected_urls_map([tid])
    assert tid in protected_map
    assert sub_img in protected_map[tid]


@pytest.mark.asyncio
async def test_verify_hardening_no_acl_list_fail_multi_existing_and_signing_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex High #3 & Grok Med #2: --verify fails on list error, checks multi existing, probes no-ACL & signing/CDN/custom S3 URLs."""
    import alibabacloud_oss_v2 as oss

    target_s3 = share_media.ActiveStorageTarget(
        provider="s3",
        bucket_name="custom-bucket",
        endpoint_url="https://s3.internal.example",
        signing_endpoint_url="https://browser-s3.public.example",
        region="us-east-1",
        addressing_style="path",
        access_key_id="ak",
        secret_access_key="sk",
        roles=("upstream",),
        key_prefixes=("",),
        sensitive_prefixes=("provenance/",),
        public_url_bases=("https://cdn.public.example",),
    )
    target_oss = share_media.ActiveStorageTarget(
        provider="oss",
        bucket_name="oss-bkt",
        endpoint_url="https://oss-cn-hangzhou.aliyuncs.com",
        signing_endpoint_url="https://oss-cn-hangzhou.aliyuncs.com",
        region="cn-hangzhou",
        addressing_style="virtual",
        access_key_id="ak",
        secret_access_key="sk",
        roles=("upstream",),
        key_prefixes=("",),
        sensitive_prefixes=("provenance/",),
        public_url_bases=("https://cdn-oss.example",),
    )

    # 1. Real _put_temp_probe writes WITHOUT object-level private ACL on BOTH S3 and OSS
    class _S3ProbeSpec:
        def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> dict[str, Any]: ...
        def delete_object(self, *, Bucket: str, Key: str) -> dict[str, Any]: ...
        def list_objects_v2(self, *, Bucket: str, Prefix: str, MaxKeys: int) -> dict[str, Any]: ...

    s3_mock = create_autospec(_S3ProbeSpec, instance=True)
    oss_mock = create_autospec(oss.Client, instance=True)
    monkeypatch.setattr(ops_script, "_build_s3_client_for_target", lambda t: s3_mock)
    monkeypatch.setattr(ops_script, "_build_oss_client_for_target", lambda t: oss_mock)

    cleanup_s3 = ops_script._put_temp_probe(target_s3, "provenance/_probe.txt")
    s3_put_kwargs = s3_mock.put_object.call_args.kwargs
    assert "ACL" not in s3_put_kwargs
    cleanup_s3()
    s3_mock.delete_object.assert_called_once()

    cleanup_oss = ops_script._put_temp_probe(target_oss, "provenance/_probe.txt")
    oss_put_req = oss_mock.put_object.call_args.args[0]
    assert getattr(oss_put_req, "acl", None) is None
    cleanup_oss()
    oss_mock.delete_object.assert_called_once()

    # 2. Listing failure MUST fail verification (never treated as empty prefix!)
    s3_mock.list_objects_v2.side_effect = RuntimeError("AccessDenied on ListBucket")
    ok_list_fail, checks_list_fail = ops_script.verify_storage_targets(
        targets=[target_s3],
        probe_write=True,
        http_get_fn=lambda url, timeout_s=10.0: (403, "Forbidden"),
    )
    assert ok_list_fail is False
    assert any("AccessDenied" in c.detail for c in checks_list_fail)

    # 3. Multiple existing objects sampled per prefix: 1st is 403, 2nd is 200 -> MUST FAIL!
    s3_mock.list_objects_v2.side_effect = None
    s3_mock.list_objects_v2.return_value = {
        "Contents": [
            {"Key": "provenance/obj1.json"},
            {"Key": "provenance/obj2_public.json"},
        ]
    }
    probed_urls: list[str] = []

    def _get_second_200(url: str, *, timeout_s: float = 10.0) -> tuple[int | None, str]:
        probed_urls.append(url)
        if "obj2_public.json" in url:
            return 200, "OK"
        return 403, "Forbidden"

    ok_multi, checks_multi = ops_script.verify_storage_targets(
        targets=[target_s3],
        probe_write=True,
        http_get_fn=_get_second_200,
    )
    assert ok_multi is False
    # Verify origin, independent signing endpoint, AND CDN were all probed
    assert any("https://s3.internal.example/custom-bucket/provenance/obj1.json" == u for u in probed_urls)
    assert any("https://browser-s3.public.example/custom-bucket/provenance/obj1.json" == u for u in probed_urls)
    assert any("https://cdn.public.example/provenance/obj1.json" == u for u in probed_urls)
    assert any(not c.ok and c.status_code == 200 and "obj2_public.json" in c.url for c in checks_multi)

    # 4. Known historical URL returning 200 fails verification
    ok_hist, checks_hist = ops_script.verify_storage_targets(
        targets=[],
        known_historical_urls=["https://cdn.public.example/blobs/u1/legacy.pdf"],
        http_get_fn=lambda url, timeout_s=10.0: (200, "OK"),
    )
    assert ok_hist is False
    assert checks_hist[0].status_code == 200


@pytest.mark.asyncio
async def test_warm_shared_slices_independent_pool_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex Med #8: App pool & checkpointer lifecycles are independent on both open failure and close failure."""
    from psycopg_pool import AsyncConnectionPool
    from src.server.app import setup
    from src.server.database import pool as db_pool
    from src.server.utils import checkpointer as cp_mod

    conv_pool = create_autospec(AsyncConnectionPool, instance=True)
    conv_pool.closed = True
    monkeypatch.setattr(db_pool, "get_or_create_pool", lambda: conv_pool)
    monkeypatch.setattr(setup, "checkpointer", None)

    # 1. Checkpointer open failure MUST close the already-opened app pool
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    fake_cp = create_autospec(AsyncPostgresSaver, instance=True)
    monkeypatch.setattr(cp_mod, "get_checkpointer", lambda **kw: fake_cp)
    monkeypatch.setattr(
        cp_mod,
        "open_checkpointer_pool",
        create_autospec(cp_mod.open_checkpointer_pool, side_effect=RuntimeError("cp open boom")),
    )
    with pytest.raises(RuntimeError, match="cp open boom"):
        await ops_script._ensure_cli_pools_open()
    conv_pool.open.assert_awaited_once()
    conv_pool.close.assert_awaited_once()

    # 2. Checkpointer close failure MUST NOT skip closing app pool
    conv_pool.close.reset_mock()
    monkeypatch.setattr(setup, "checkpointer", fake_cp)
    monkeypatch.setattr(
        cp_mod,
        "close_checkpointer_pool",
        create_autospec(cp_mod.close_checkpointer_pool, side_effect=RuntimeError("cp close boom")),
    )
    with pytest.raises(RuntimeError, match="cp close boom"):
        await ops_script._close_cli_pools(conv_pool, fake_cp, True, True)
    conv_pool.close.assert_awaited_once()
    assert setup.checkpointer is None


# =============================================================================
# AU1 r4 REGRESSION TESTS (Items 1-5, 8-10)
# =============================================================================


@pytest.mark.asyncio
async def test_r4_thinking_spans_mismatched_code_cross_phase_and_unclosed_cold_to_cached_sse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AU1 r4 #1: Cold projection -> turn_slices -> cache-hit public SSE for (a) mismatched nested, (b) backtick/fence closer, (c) cross-phase, and unclosed message."""
    import httpx
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from src.server.database.conversation import replay_rows as replay_rows_mod
    from src.server.database.conversation import turn_slices as slices_db
    from src.server.services.history import replay as replay_mod
    from src.server.services.history import slices as slices_mod
    from src.server.services.history.reader import CheckpointHistoryReader, TurnAnchor

    cases = [
        (
            "a_mismatched_balanced",
            "<thinking>A<thinking_mode>B</thinking> LEAK</thinking_mode></thinking>答案A",
            "答案A",
            ("LEAK", "B"),
            False,
        ),
        (
            "a_mismatched_unclosed_outer",
            "<thinking>A<thinking_mode>B</thinking> LEAK</thinking_mode>",
            None,
            ("LEAK", "B"),
            True,
        ),
        (
            "b_backtick_and_fence_closer",
            "<thinking>用 `</thinking>` 和\n```\n</thinking>\n```\n结束 SECRET</thinking>答案B",
            "答案B",
            ("SECRET",),
            False,
        ),
        (
            "c_cross_phase",
            [
                {"type": "text", "text": "<thinking>SECRET跨阶段 ", "phase": "commentary"},
                {"type": "text", "text": "B</thinking>答案C", "phase": "final_answer"},
            ],
            "答案C",
            ("SECRET", "B"),
            False,
        ),
        (
            "d_unclosed_cross_phase",
            [
                {"type": "text", "text": "<thinking>SECRET_UNCLOSED_1 ", "phase": "commentary"},
                {"type": "text", "text": "SECRET_UNCLOSED_2", "phase": "final_answer"},
            ],
            None,
            ("SECRET_UNCLOSED_1", "SECRET_UNCLOSED_2"),
            True,
        ),
    ]

    app = FastAPI()
    app.include_router(public_app.router)
    transport = httpx.ASGITransport(app=app)

    orig_get_turn_lines = slices_db.get_turn_lines
    orig_get_turn_slices = slices_db.get_turn_slices
    orig_upsert_turn_slices = slices_db.upsert_turn_slices
    orig_update_turns = slices_db.update_turns
    orig_pub_get_shared = public_app.get_shared_thread
    orig_access_get_shared = share_access.get_shared_thread
    orig_get_replay_data = public_app.get_replay_thread_data

    for case_name, ai_content, expected_answer, forbidden_tokens, expect_unclosed in cases:
        tid = str(uuid.uuid4())
        rid = str(uuid.uuid4())
        share_token = f"tok-r4-{case_name}"
        fake_thread = {
            "conversation_thread_id": tid,
            "workspace_id": str(uuid.uuid4()),
            "user_id": "u1",
            "owner_id": "u1",
            "title": f"Case {case_name}",
            "share_token": share_token,
            "is_shared": True,
            "share_permissions": {"allow_files": False, "allow_download": False},
            "latest_checkpoint_id": "cp-tail-0",
        }
        rows = replay_rows_mod.ThreadRows(
            thread_id=tid,
            owner_id="u1",
            thread=fake_thread,
            queries=[
                {
                    "conversation_query_id": str(uuid.uuid4()),
                    "turn_index": 0,
                    "type": "initial",
                    "content": "请总结要点",
                    "metadata": {},
                }
            ],
            responses_by_turn={
                0: {
                    "conversation_response_id": rid,
                    "turn_index": 0,
                    "status": "completed",
                    "replay_facts": {},
                }
            },
            usages=[],
            provenance=[],
            run_facts=[],
            open_runs={},
            failed_steers=[],
        )
        anchor = TurnAnchor(
            turn_ordinal=0,
            input_checkpoint_id="cp-in-0",
            tail_checkpoint_id="cp-tail-0",
            turn_index=0,
            end_checkpoint_id="cp-tail-0",
        )
        fake_slice = slices_mod.TurnSlice(
            anchor=anchor,
            messages=[
                HumanMessage(id="h-0", content="请总结要点"),
                AIMessage(id=f"ai-{case_name}", content=ai_content),
            ],
        )

        reader_stub = create_autospec(CheckpointHistoryReader, instance=True)
        reader_stub.serde = JsonPlusSerializer()
        reader_stub.aget_turn_anchors.return_value = ([anchor], "cp-tail-0")
        reader_stub.aget_turn_slices.return_value = [fake_slice]
        reader_stub.aget_tip_interrupts.return_value = []
        monkeypatch.setattr(
            CheckpointHistoryReader,
            "get_instance",
            classmethod(lambda cls, _r=reader_stub: _r),
        )

        stored_slices: dict[str, slices_db.StoredSlice] = {}

        async def _get_turn_lines(rids: list[str], _store=stored_slices) -> dict[str, slices_db.StoredSlice]:
            return {r: _store[r] for r in rids if r in _store}

        async def _get_turn_slices(rids: list[str], _store=stored_slices) -> dict[str, slices_db.StoredSlice]:
            return {r: _store[r] for r in rids if r in _store}

        async def _upsert_turn_slices(turn_rows: list[slices_db.TurnRow], _store=stored_slices) -> None:
            for tr in turn_rows:
                _store[tr.response_id] = tr.slice

        async def _update_turns(updates: list[slices_db.TurnUpdate], _store=stored_slices) -> None:
            for u in updates:
                cur = _store.get(u.response_id)
                if cur is not None:
                    _store[u.response_id] = cur._replace(
                        lines_key=u.lines_key or cur.lines_key,
                        lines=u.lines or cur.lines,
                    )

        monkeypatch.setattr(
            slices_db, "get_turn_lines", create_autospec(orig_get_turn_lines, side_effect=_get_turn_lines)
        )
        monkeypatch.setattr(
            slices_db, "get_turn_slices", create_autospec(orig_get_turn_slices, side_effect=_get_turn_slices)
        )
        monkeypatch.setattr(
            slices_db, "upsert_turn_slices", create_autospec(orig_upsert_turn_slices, side_effect=_upsert_turn_slices)
        )
        monkeypatch.setattr(
            slices_db, "update_turns", create_autospec(orig_update_turns, side_effect=_update_turns)
        )
        monkeypatch.setattr(
            public_app, "get_shared_thread", create_autospec(orig_pub_get_shared, return_value=fake_thread)
        )
        monkeypatch.setattr(
            share_access, "get_shared_thread", create_autospec(orig_access_get_shared, return_value=fake_thread)
        )
        monkeypatch.setattr(
            public_app, "get_replay_thread_data", create_autospec(orig_get_replay_data, return_value=rows)
        )

        # 1. Real cold projection persists turn_slices row
        page_cold, src_cold = await replay_mod.read_replay_page(rows, persist=True)
        assert src_cold == "checkpoint"
        assert rid in stored_slices and stored_slices[rid].lines
        stored_lines_str = stored_slices[rid].lines
        if not expect_unclosed:
            for tok in forbidden_tokens:
                assert tok not in stored_lines_str, f"{case_name}: {tok} leaked into turn_slices"
        else:
            # Unclosed messages remain untouched in turn_slices so strict SSE can recognize & drop them
            assert "<thinking>" in stored_lines_str

        # 2. Public SSE hits warm turn_slices cache (ZERO additional checkpoint slice reads)
        fetches_before_sse = reader_stub.aget_turn_slices.call_count
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/public/shared/{share_token}/replay")
        assert resp.status_code == 200
        assert reader_stub.aget_turn_slices.call_count == fetches_before_sse
        sse_body = resp.text

        for tok in forbidden_tokens:
            assert tok not in sse_body, f"{case_name}: {tok} leaked into public SSE"
        if expected_answer is not None:
            assert expected_answer in sse_body, f"{case_name}: missing {expected_answer} in SSE"
        else:
            # Unclosed message emits no assistant answer text from the AI message itself
            assert f"ai-{case_name}" not in sse_body


def test_r4_verify_checks_runtime_default_s3_public_base_and_empty_enabled_storage_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AU1 r4 #2: --verify probes runtime StorageConfig.get_public_url_base() even with custom S3 endpoint, and fails when storage is enabled but targets empty."""
    import src.utils.storage as storage_mod

    for var in (
        "STORAGE_PROVIDER",
        "STORAGE_BUCKET_NAME",
        "STORAGE_ENDPOINT_URL",
        "STORAGE_BROWSER_ENDPOINT_URL",
        "STORAGE_PUBLIC_URL_BASE",
        "STORAGE_REGION",
        "STORAGE_ADDRESSING_STYLE",
        "STORAGE_KEY_PREFIX",
        "S3_BUCKET_NAME",
        "S3_ENDPOINT_URL",
        "S3_PUBLIC_URL_BASE",
        "S3_REGION",
        "OSS_BUCKET_NAME",
        "OSS_ENDPOINT",
        "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME",
        "DALTON_ASR_STORAGE_BUCKET_NAME",
    ):
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setenv("STORAGE_PROVIDER", "s3")
    monkeypatch.setenv("STORAGE_BUCKET_NAME", "custom-ep-bucket")
    monkeypatch.setenv("STORAGE_REGION", "ap-southeast-1")
    monkeypatch.setenv("STORAGE_ENDPOINT_URL", "https://custom-s3.internal.example")

    targets = share_media.resolve_active_storage_targets()
    assert len(targets) == 1
    aws_default_base = "https://custom-ep-bucket.s3.ap-southeast-1.amazonaws.com"
    assert aws_default_base in targets[0].public_url_bases

    # Only the AWS default URL returns 200 while the custom endpoint returns 403 -> MUST FAIL
    def _only_aws_default_200(url: str, timeout_s: float = 10.0) -> tuple[int, str]:
        if url.startswith(aws_default_base + "/"):
            return 200, "OK"
        return 403, "Forbidden"

    ok, checks = ops_script.verify_storage_targets(
        targets=targets,
        probe_write=False,
        http_get_fn=_only_aws_default_200,
        find_existing_fn=lambda t, p: [f"{p}sample.bin"] if p == "private-media/" else [f"{p}x.bin"],
    )
    assert ok is False
    assert any(not c.ok and c.url.startswith(aws_default_base + "/") and c.status_code == 200 for c in checks)

    # Runtime storage enabled (per src.utils.storage) but resolve_active_storage_targets returns [] -> MUST FAIL
    orig_is_enabled = storage_mod.is_storage_enabled
    monkeypatch.setattr(
        ops_script,
        "resolve_active_storage_targets",
        create_autospec(ops_script.resolve_active_storage_targets, return_value=[]),
    )
    monkeypatch.setattr(
        storage_mod,
        "is_storage_enabled",
        create_autospec(orig_is_enabled, return_value=True),
    )
    ok_empty_enabled, checks_empty_enabled = ops_script.verify_storage_targets()
    assert ok_empty_enabled is False
    assert checks_empty_enabled and checks_empty_enabled[0].ok is False

    # Runtime storage disabled + empty targets -> still passes
    monkeypatch.setattr(
        storage_mod,
        "is_storage_enabled",
        create_autospec(orig_is_enabled, return_value=False),
    )
    ok_empty_disabled, checks_empty_disabled = ops_script.verify_storage_targets()
    assert ok_empty_disabled is True
    assert checks_empty_disabled == []


@pytest.mark.asyncio
async def test_r4_replay_media_prod_sql_row_shapes_owner_rich_colleague_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AU1 r4 #3: Prod SQL row shapes (no replay_facts on responses_by_turn, no user_id on queries/rows.thread) sign & serve 200 for captured img, upload_id, and colleague attachments, while rejecting stranger upload_id and failing closed on DB error."""
    from contextlib import asynccontextmanager
    import re
    import httpx
    import psycopg
    from src.dalton import chat_upload_storage as chat_store
    from src.server.database import chat_attachments as att_db
    from src.server.database import pool as db_pool
    from src.server.database.conversation import replay_rows as replay_rows_mod
    from src.server.services.history import replay as replay_mod
    from src.server.services.history.replay.lines import line_of

    tid = str(uuid.uuid4())
    ws_id = str(uuid.uuid4())
    owner_uid = "user-owner-1"
    colleague_uid = "user-colleague-2"
    stranger_uid = "user-stranger-9"

    cap_img_url = f"https://cdn.prod.example/private-media/response-images/{tid}/t0/chart.png"
    colleague_att_url = "https://cdn.prod.example/private-media/attachments/att-colleague-1/deck.png"
    owner_upload_id = str(uuid.uuid4())
    colleague_upload_id = str(uuid.uuid4())
    stranger_upload_id = str(uuid.uuid4())

    # Production SQL row shapes:
    # - rows.thread has NO user_id (owner_id is separate on ThreadRows)
    # - rows.queries has NO user_id column
    # - rows.responses_by_turn uses _LIGHT_RESPONSE_COLUMNS (NO replay_facts)
    prod_thread_summary = {
        "conversation_thread_id": tid,
        "workspace_id": ws_id,
        "current_status": "completed",
        "thread_index": 1,
        "latest_checkpoint_id": "cp-1",
    }
    prod_queries = [
        {
            "conversation_query_id": str(uuid.uuid4()),
            "turn_index": 0,
            "type": "initial",
            "content": "owner query",
            "metadata": {
                "attachments": [
                    {"upload_id": owner_upload_id, "name": "owner_big.pdf"},
                    {"upload_id": stranger_upload_id, "name": "stranger_big.pdf"},
                ]
            },
        },
        {
            "conversation_query_id": str(uuid.uuid4()),
            "turn_index": 1,
            "type": "follow_up",
            "content": "colleague query",
            "metadata": {
                "attachments": [
                    {"url": colleague_att_url, "name": "deck.png"},
                    {"upload_id": colleague_upload_id, "name": "colleague_big.pdf"},
                ]
            },
        },
    ]
    prod_responses_by_turn = {
        0: {"conversation_response_id": str(uuid.uuid4()), "turn_index": 0, "status": "completed"},
        1: {"conversation_response_id": str(uuid.uuid4()), "turn_index": 1, "status": "completed"},
    }
    prod_rows = replay_rows_mod.ThreadRows(
        thread_id=tid,
        owner_id=owner_uid,
        thread=prod_thread_summary,
        queries=prod_queries,
        responses_by_turn=prod_responses_by_turn,
        usages=[],
        provenance=[],
        run_facts=[],
        open_runs={},
    )

    page_lines = [
        line_of(
            {
                "event": "user_message",
                "data": {
                    "turn_index": 0,
                    "content": "owner query",
                    "metadata": prod_queries[0]["metadata"],
                },
            }
        ),
        line_of(
            {
                "event": "message_chunk",
                "data": {
                    "turn_index": 0,
                    "role": "assistant",
                    "content": f"Chart: ![c]({cap_img_url})",
                },
            }
        ),
        line_of(
            {
                "event": "user_message",
                "data": {
                    "turn_index": 1,
                    "content": "colleague query",
                    "metadata": prod_queries[1]["metadata"],
                },
            }
        ),
    ]

    executed_sqls: list[str] = []

    def _make_db_conn():
        state = {"step": 0}
        cur = create_autospec(psycopg.AsyncCursor, instance=True)
        cur.__aenter__.return_value = cur
        cur.__aexit__.return_value = False

        async def _exec(sql: Any, params: Any = None) -> None:
            sql_s = str(sql)
            executed_sqls.append(sql_s)
            if "FROM conversation_queries" in sql_s:
                state["step"] = 1
            elif "FROM conversation_responses" in sql_s:
                state["step"] = 2
            elif "FROM subagent_runs" in sql_s:
                state["step"] = 3
            elif "FROM turn_slices" in sql_s:
                state["step"] = 4

        async def _fetchall() -> list[dict[str, Any]]:
            if state["step"] == 1:
                return [
                    {"turn_index": 0, "metadata": prod_queries[0]["metadata"]},
                    {"turn_index": 1, "metadata": prod_queries[1]["metadata"]},
                ]
            if state["step"] == 2:
                return [
                    {
                        "turn_index": 0,
                        "replay_facts": {"images": {"/w/chart.png": cap_img_url}},
                        "sender_user_id": owner_uid,
                    },
                    {
                        "turn_index": 1,
                        "replay_facts": None,
                        "sender_user_id": colleague_uid,
                    },
                ]
            return []

        cur.execute.side_effect = _exec
        cur.fetchall.side_effect = _fetchall
        conn = create_autospec(psycopg.AsyncConnection, instance=True)
        conn.cursor.return_value = cur
        return conn

    @asynccontextmanager
    async def _fake_conn():
        yield _make_db_conn()

    monkeypatch.setattr(db_pool, "get_db_connection", _fake_conn)
    monkeypatch.setattr(
        share_media,
        "_allowed_storage_origins",
        create_autospec(share_media._allowed_storage_origins, return_value={"https://cdn.prod.example"}),
    )
    monkeypatch.setattr(
        att_db,
        "get_attachment",
        create_autospec(
            att_db.get_attachment,
            return_value={
                "object_key": "attachments/att-colleague-1/deck.png",
                "user_id": colleague_uid,
            },
        ),
    )

    upload_rows = {
        owner_upload_id: {
            "upload_id": owner_upload_id,
            "user_id": owner_uid,
            "workspace_id": ws_id,
            "object_key": f"private-media/chat-uploads/{ws_id}/{owner_upload_id}/owner_big.pdf",
            "safe_filename": "owner_big.pdf",
            "mime_type": "application/pdf",
        },
        colleague_upload_id: {
            "upload_id": colleague_upload_id,
            "user_id": colleague_uid,
            "workspace_id": ws_id,
            "object_key": f"private-media/chat-uploads/{ws_id}/{colleague_upload_id}/colleague_big.pdf",
            "safe_filename": "colleague_big.pdf",
            "mime_type": "application/pdf",
        },
        stranger_upload_id: {
            "upload_id": stranger_upload_id,
            "user_id": stranger_uid,
            "workspace_id": ws_id,
            "object_key": f"private-media/chat-uploads/{ws_id}/{stranger_upload_id}/stranger_big.pdf",
            "safe_filename": "stranger_big.pdf",
            "mime_type": "application/pdf",
        },
    }

    async def _fake_fetch_ready(
        workspace_id: str, upload_ids: Any, *, allowed_user_ids: Any = None
    ) -> dict[str, dict[str, Any]]:
        allowed = set(allowed_user_ids or ())
        return {
            uid: row
            for uid, row in upload_rows.items()
            if uid in set(upload_ids)
            and row["workspace_id"] == workspace_id
            and (allowed_user_ids is None or row["user_id"] in allowed)
        }

    monkeypatch.setattr(
        share_media,
        "_fetch_ready_chat_uploads",
        create_autospec(share_media._fetch_ready_chat_uploads, side_effect=_fake_fetch_ready),
    )
    monkeypatch.setattr(
        share_media,
        "read_controlled_object_bytes",
        create_autospec(share_media.read_controlled_object_bytes, return_value=b"PNG_BYTES"),
    )
    monkeypatch.setattr(
        chat_store,
        "resolve_existing_chat_upload_key",
        create_autospec(chat_store.resolve_existing_chat_upload_key, side_effect=lambda k: k),
    )
    monkeypatch.setattr(
        chat_store,
        "iter_stored_object_chunks",
        create_autospec(chat_store.iter_stored_object_chunks, side_effect=lambda k: iter([b"PDF_BYTES"])),
    )

    # 1. Owner replay path: signs captured image, owner upload_id, colleague staged att & upload_id; leaves ZERO raw URLs
    rewritten_owner_lines, _cookies = await share_media.prepare_owner_replay_media(
        None, prod_rows, page_lines, thread_id=tid, viewer_id=owner_uid
    )
    assert any(
        "position('steering_delivered' in ts.lines) > 0" in s
        and "JOIN conversation_responses" in s
        for s in executed_sqls
    )
    owner_joined = "\n".join(ln.data_json for ln in rewritten_owner_lines)
    assert "cdn.prod.example" not in owner_joined
    owner_items = [ln.item() for ln in rewritten_owner_lines]
    # Turn 0: owner upload signed, stranger upload NOT signed
    t0_atts = owner_items[0]["data"]["metadata"]["attachments"]
    assert t0_atts[0]["url"].startswith(f"/api/v1/threads/{tid}/media/")
    assert "url" not in t0_atts[1]
    # Turn 0 assistant: captured image signed
    assert f"/api/v1/threads/{tid}/media/" in owner_items[1]["data"]["content"]
    # Turn 1: colleague staged att and colleague upload_id both signed
    t1_atts = owner_items[2]["data"]["metadata"]["attachments"]
    assert t1_atts[0]["url"].startswith(f"/api/v1/threads/{tid}/media/")
    assert t1_atts[1]["url"].startswith(f"/api/v1/threads/{tid}/media/")

    # Serve EVERY signed owner token -> 200 OK
    for signed_url in (
        t0_atts[0]["url"],
        t1_atts[0]["url"],
        t1_atts[1]["url"],
    ):
        tok = signed_url.rsplit("/", 1)[-1]
        claims = share_media.verify_owner_media_token(tok, expected_thread_id=tid)
        assert claims["vid"] == owner_uid
        resp = await share_media._serve_media_payload(
            thread_id=tid,
            workspace_id=ws_id,
            thread_owner_id=owner_uid,
            backend=claims["b"],
            ref_id=claims["ref"],
            cache_control="private, no-store",
        )
        assert resp.status_code == 200

    # Captured image token also serves 200 OK
    m_cap = re.search(rf"/api/v1/threads/{re.escape(tid)}/media/([^)\s\"']+)", owner_items[1]["data"]["content"])
    assert m_cap is not None
    cap_claims = share_media.verify_owner_media_token(
        m_cap.group(1), expected_thread_id=tid
    )
    assert cap_claims["vid"] == owner_uid
    cap_resp = await share_media._serve_media_payload(
        thread_id=tid,
        workspace_id=ws_id,
        thread_owner_id=owner_uid,
        backend=cap_claims["b"],
        ref_id=cap_claims["ref"],
        cache_control="private, no-store",
    )
    assert cap_resp.status_code == 200

    # Stranger upload_id rejected on read side with 404
    with pytest.raises(HTTPException) as exc_stranger:
        await share_media._serve_media_payload(
            thread_id=tid,
            workspace_id=ws_id,
            thread_owner_id=owner_uid,
            backend="chat_upload",
            ref_id=stranger_upload_id,
            cache_control="no-store",
        )
    assert exc_stranger.value.status_code == 404

    # 2. Rich share replay path (allow_files=True): also leaves ZERO raw object URLs
    rich_thread = {
        **prod_thread_summary,
        "share_token": "tok-rich-prod",
        "is_shared": True,
        "share_permissions": {"allow_files": True, "allow_download": False},
    }
    monkeypatch.setattr(
        public_app, "get_shared_thread", create_autospec(public_app.get_shared_thread, return_value=rich_thread)
    )
    monkeypatch.setattr(
        public_app, "get_replay_thread_data", create_autospec(public_app.get_replay_thread_data, return_value=prod_rows)
    )
    monkeypatch.setattr(
        share_redaction,
        "resolve_effective_share_permissions",
        create_autospec(
            share_redaction.resolve_effective_share_permissions,
            return_value=share_redaction.EffectiveSharePermissions(True, False),
        ),
    )
    page_obj = replay_mod.ReplayPage(lines=page_lines, first_turn_index=0, has_more=False)
    monkeypatch.setattr(
        replay_mod,
        "read_replay_page",
        create_autospec(replay_mod.read_replay_page, return_value=(page_obj, "cache")),
    )
    monkeypatch.setattr(
        replay_mod,
        "finish_lines",
        create_autospec(replay_mod.finish_lines, return_value=None),
    )

    app = FastAPI()
    app.include_router(public_app.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        rich_resp = await client.get("/api/v1/public/shared/tok-rich-prod/replay")
    assert rich_resp.status_code == 200
    assert "cdn.prod.example" not in rich_resp.text
    assert "/api/v1/public/shared/tok-rich-prod/media/" in rich_resp.text

    # 3. DB registry load failure -> signs nothing AND strips raw object URLs (never leaks raw URL)
    monkeypatch.setattr(
        share_media,
        "_load_thread_media_registry_from_db",
        create_autospec(share_media._load_thread_media_registry_from_db, return_value=None),
    )
    fail_lines, _ = await share_media.prepare_owner_replay_media(
        None, prod_rows, page_lines, thread_id=tid, viewer_id=owner_uid
    )
    fail_joined = "\n".join(ln.data_json for ln in fail_lines)
    assert "cdn.prod.example" not in fail_joined
    assert f"/api/v1/threads/{tid}/media/" not in fail_joined


@pytest.mark.asyncio
async def test_r4_multi_attempt_union_and_protected_urls_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AU1 r4 #4: Multiple attempts on the same turn are unioned across media registry & protected_urls; DB error on protected_urls fails closed (no summary)."""
    from contextlib import asynccontextmanager
    import psycopg
    from src.server.database import pool as db_pool

    tid = str(uuid.uuid4())
    img1 = f"https://cdn.retry.example/private-media/response-images/{tid}/a1/img1.png"
    img2 = f"https://cdn.retry.example/private-media/response-images/{tid}/a2/img2.png"

    def _make_multi_attempt_conn():
        state = {"table": ""}
        cur = create_autospec(psycopg.AsyncCursor, instance=True)
        cur.__aenter__.return_value = cur
        cur.__aexit__.return_value = False

        async def _exec(sql: Any, params: Any = None) -> None:
            s = str(sql)
            if "FROM conversation_responses" in s:
                state["table"] = "responses"
            else:
                state["table"] = "other"

        async def _fetchall() -> list[dict[str, Any]]:
            if state["table"] == "responses":
                return [
                    {
                        "thread_id": tid,
                        "turn_index": 0,
                        "replay_facts": {"images": {"/w/1.png": img1}},
                        "sender_user_id": "u1",
                    },
                    {
                        "thread_id": tid,
                        "turn_index": 0,
                        "replay_facts": {"images": {"/w/2.png": img2}},
                        "sender_user_id": "u1",
                    },
                ]
            return []

        cur.execute.side_effect = _exec
        cur.fetchall.side_effect = _fetchall
        conn = create_autospec(psycopg.AsyncConnection, instance=True)
        conn.cursor.return_value = cur
        return conn

    @asynccontextmanager
    async def _fake_conn():
        yield _make_multi_attempt_conn()

    monkeypatch.setattr(db_pool, "get_db_connection", _fake_conn)
    monkeypatch.setattr(
        share_media,
        "_allowed_storage_origins",
        create_autospec(share_media._allowed_storage_origins, return_value={"https://cdn.retry.example"}),
    )

    # 1. Media registry unions BOTH attempts on turn 0 (issuance + serving)
    reg = await share_media._load_thread_media_registry_from_db(tid, owner_id="u1")
    assert reg is not None
    assert f"response-images/{tid}/a1/img1.png" in reg.captured_image_keys
    assert f"response-images/{tid}/a2/img2.png" in reg.captured_image_keys

    bindings = await share_media.resolve_thread_media_bindings(
        {"conversation_thread_id": tid, "user_id": "u1"}, [], []
    )
    assert bindings is not None
    assert img1 in bindings
    assert img2 in bindings

    # 2. _load_thread_protected_urls_map unions BOTH attempts on turn 0
    p_map = await share_redaction._load_thread_protected_urls_map([tid])
    assert p_map is not None
    assert img1 in p_map[tid]
    assert img2 in p_map[tid]

    # 3. When _load_thread_protected_urls_map fails with a real DB error -> fail closed (safe title only, NO summary)
    @asynccontextmanager
    async def _boom_conn():
        raise RuntimeError("database connection reset")
        yield  # pragma: no cover

    monkeypatch.setattr(db_pool, "get_db_connection", _boom_conn)
    assert await share_redaction._load_thread_protected_urls_map([tid]) is None

    monkeypatch.setattr(
        share_redaction,
        "get_org_library_title_meta_map",
        create_autospec(share_redaction.get_org_library_title_meta_map, return_value={"item-1": True}),
    )
    monkeypatch.setattr(
        share_redaction,
        "_load_threads_for_redaction",
        create_autospec(
            share_redaction._load_threads_for_redaction,
            return_value={
                tid: {
                    "conversation_thread_id": tid,
                    "share_permissions": {"allow_files": False, "allow_download": False},
                }
            },
        ),
    )
    redacted_lib = await share_redaction.redact_org_library_rows(
        [
            {
                "item_id": "item-1",
                "kind": "thread",
                "thread_id": tid,
                "title": "Secret Custom Title",
                "summary": f"Contains sensitive URL {img1}",
            }
        ]
    )
    assert redacted_lib[0]["title"] == share_redaction.DEFAULT_SHARED_TITLE
    assert redacted_lib[0]["summary"] == ""


def test_r4_bucket_acl_unknown_access_denied_and_explicit_on_off_modes(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AU1 r4 #5: AccessDenied on GetBucketOwnershipControls omits ACL on presigned PUT (no cache + warning); DALTON_STORAGE_OBJECT_ACL=on/off overrides."""
    from botocore.exceptions import ClientError

    class _S3PresignProbeSpec:
        def generate_presigned_url(
            self, ClientMethod: str, Params: dict[str, Any] | None = None, ExpiresIn: int = 3600
        ) -> str:
            raise NotImplementedError

        def get_bucket_ownership_controls(self, *, Bucket: str) -> dict[str, Any]:
            raise NotImplementedError

    monkeypatch.delenv("DALTON_STORAGE_DISABLE_OBJECT_ACL", raising=False)
    monkeypatch.delenv("STORAGE_DISABLE_OBJECT_ACL", raising=False)
    share_media.reset_bucket_acl_cache_for_test()

    access_denied = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
        "GetBucketOwnershipControls",
    )

    try:
        # 1. auto + AccessDenied -> omits ACL & x-amz-acl, does NOT cache, emits warning
        monkeypatch.setenv("DALTON_STORAGE_OBJECT_ACL", "auto")
        client = create_autospec(_S3PresignProbeSpec, instance=True)
        client.get_bucket_ownership_controls.side_effect = access_denied
        client.generate_presigned_url.return_value = "https://s3.example/presigned"
        with caplog.at_level("WARNING", logger="src.dalton.share_media"):
            url, headers = share_media.s3_presign_put_with_private_acl(
                client,
                bucket="min-perm-bucket",
                key="blobs/abc",
                checksum_b64="AAAA",
                content_length=10,
                content_type="application/octet-stream",
                expires_in=300,
                endpoint_url="https://s3.example",
            )
        assert url == "https://s3.example/presigned"
        params = client.generate_presigned_url.call_args.kwargs["Params"]
        assert "ACL" not in params
        assert "x-amz-acl" not in headers
        assert any("unknown" in rec.message.lower() for rec in caplog.records)
        # Unknown MUST NOT be cached: a second call probes again
        assert client.get_bucket_ownership_controls.call_count == 1
        share_media.s3_presign_put_with_private_acl(
            client,
            bucket="min-perm-bucket",
            key="blobs/abc",
            checksum_b64="AAAA",
            content_length=10,
            content_type="application/octet-stream",
            expires_in=300,
            endpoint_url="https://s3.example",
        )
        assert client.get_bucket_ownership_controls.call_count == 2

        # 2. DALTON_STORAGE_OBJECT_ACL=on -> ALWAYS attaches ACL & x-amz-acl
        monkeypatch.setenv("DALTON_STORAGE_OBJECT_ACL", "on")
        client_on = create_autospec(_S3PresignProbeSpec, instance=True)
        client_on.get_bucket_ownership_controls.side_effect = access_denied
        client_on.generate_presigned_url.return_value = "https://s3.example/presigned-on"
        _, headers_on = share_media.s3_presign_put_with_private_acl(
            client_on,
            bucket="min-perm-bucket",
            key="blobs/abc",
            checksum_b64="AAAA",
            content_length=10,
            content_type="application/octet-stream",
            expires_in=300,
            endpoint_url="https://s3.example",
        )
        assert client_on.generate_presigned_url.call_args.kwargs["Params"]["ACL"] == "private"
        assert headers_on["x-amz-acl"] == "private"

        # 3. DALTON_STORAGE_OBJECT_ACL=off -> NEVER attaches ACL or x-amz-acl
        monkeypatch.setenv("DALTON_STORAGE_OBJECT_ACL", "off")
        client_off = create_autospec(_S3PresignProbeSpec, instance=True)
        client_off.generate_presigned_url.return_value = "https://s3.example/presigned-off"
        _, headers_off = share_media.s3_presign_put_with_private_acl(
            client_off,
            bucket="min-perm-bucket",
            key="blobs/abc",
            checksum_b64="AAAA",
            content_length=10,
            content_type="application/octet-stream",
            expires_in=300,
            endpoint_url="https://s3.example",
        )
        assert "ACL" not in client_off.generate_presigned_url.call_args.kwargs["Params"]
        assert "x-amz-acl" not in headers_off
    finally:
        share_media.reset_bucket_acl_cache_for_test()


def test_r4_historical_url_strips_credentials_and_rejects_grant_paths(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AU1 r4 #9: --historical-url strips userinfo/query/fragment before anonymous GET, redacts output (text & JSON), and rejects grant-path URLs."""
    probed_urls: list[str] = []

    def _fake_get(url: str, timeout_s: float = 10.0) -> tuple[int, str]:
        probed_urls.append(url)
        return 403, "Forbidden"

    s3_signed = "https://alice:topsecret@bucket.s3.us-east-1.amazonaws.com/blobs/u1/doc.pdf?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=SECRET_AWS_SIG_999#frag"
    oss_signed = "https://bucket.oss-cn-hangzhou.aliyuncs.com/memo/u1/a.pdf?OSSAccessKeyId=SECRET_OSS_AK_888&Signature=SECRET_OSS_SIG_777"
    grant_url = "https://app.example/api/v1/artifacts/g/v1.SECRET_GRANT_TICKET_666/v/1/index.html"

    ok, checks = ops_script.verify_storage_targets(
        targets=[],
        known_historical_urls=[s3_signed, oss_signed, grant_url],
        http_get_fn=_fake_get,
    )
    assert ok is False  # grant_url rejected as invalid input
    assert probed_urls == [
        "https://bucket.s3.us-east-1.amazonaws.com/blobs/u1/doc.pdf",
        "https://bucket.oss-cn-hangzhou.aliyuncs.com/memo/u1/a.pdf",
    ]
    assert checks[0].ok is True
    assert checks[0].url == "https://bucket.s3.us-east-1.amazonaws.com/blobs/u1/doc.pdf?<redacted>"
    assert checks[1].ok is True
    assert checks[1].url == "https://bucket.oss-cn-hangzhou.aliyuncs.com/memo/u1/a.pdf?<redacted>"
    assert checks[2].ok is False
    assert checks[2].status_code is None
    assert "SECRET_GRANT_TICKET_666" not in checks[2].url

    # Verify CLI --json and text outputs never contain any signature/key/userinfo
    monkeypatch.setattr(
        ops_script,
        "resolve_active_storage_targets",
        create_autospec(ops_script.resolve_active_storage_targets, return_value=[]),
    )
    monkeypatch.setattr(
        ops_script,
        "_is_runtime_storage_enabled",
        create_autospec(ops_script._is_runtime_storage_enabled, return_value=False),
    )
    monkeypatch.setattr(
        ops_script,
        "_http_anonymous_get_status",
        create_autospec(ops_script._http_anonymous_get_status, side_effect=_fake_get),
    )
    rc_json = ops_script.main(
        ["--verify", "--no-probe-write", "--json", "--historical-url", s3_signed, "--historical-url", oss_signed]
    )
    assert rc_json == 0
    json_out = capsys.readouterr().out
    for secret in ("SECRET_AWS_SIG_999", "SECRET_OSS_AK_888", "SECRET_OSS_SIG_777", "alice:topsecret"):
        assert secret not in json_out
    assert "?<redacted>" in json_out

    rc_text = ops_script.main(
        ["--verify", "--no-probe-write", "--historical-url", s3_signed, "--historical-url", oss_signed]
    )
    assert rc_text == 0
    text_out = capsys.readouterr().out
    for secret in ("SECRET_AWS_SIG_999", "SECRET_OSS_AK_888", "SECRET_OSS_SIG_777", "alice:topsecret"):
        assert secret not in text_out
    assert "?<redacted>" in text_out


@pytest.mark.asyncio
async def test_r4_warm_shared_slices_persist_failed_and_cancel_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AU1 r4 #10: Swallowed cache write is counted as persist_failed (not warmed); CancelledError during checkpointer open closes app pool."""
    from contextlib import asynccontextmanager
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    import psycopg
    from psycopg_pool import AsyncConnectionPool
    from src.server.app import setup
    from src.server.database import pool as db_pool
    from src.server.database.conversation import replay_rows as replay_rows_mod
    from src.server.services.history import replay as replay_mod
    from src.server.utils import checkpointer as cp_mod

    # 1. CancelledError while opening checkpointer MUST close already-opened app pool
    conv_pool = create_autospec(AsyncConnectionPool, instance=True)
    conv_pool.closed = True
    monkeypatch.setattr(
        db_pool,
        "get_or_create_pool",
        create_autospec(db_pool.get_or_create_pool, return_value=conv_pool),
    )
    monkeypatch.setattr(setup, "checkpointer", None)
    fake_cp = create_autospec(AsyncPostgresSaver, instance=True)
    monkeypatch.setattr(
        cp_mod,
        "get_checkpointer",
        create_autospec(cp_mod.get_checkpointer, return_value=fake_cp),
    )
    monkeypatch.setattr(
        cp_mod,
        "open_checkpointer_pool",
        create_autospec(cp_mod.open_checkpointer_pool, side_effect=asyncio.CancelledError()),
    )
    with pytest.raises(asyncio.CancelledError):
        await ops_script._ensure_cli_pools_open()
    conv_pool.open.assert_awaited_once()
    conv_pool.close.assert_awaited_once()

    # 1b. CancelledError while CLOSING checkpointer pool MUST still close app pool
    conv_pool_close = create_autospec(AsyncConnectionPool, instance=True)
    monkeypatch.setattr(
        cp_mod,
        "close_checkpointer_pool",
        create_autospec(cp_mod.close_checkpointer_pool, side_effect=asyncio.CancelledError()),
    )
    with pytest.raises(asyncio.CancelledError):
        await ops_script._close_cli_pools(conv_pool_close, fake_cp, True, True)
    conv_pool_close.close.assert_awaited_once()

    # 2. Swallowed turn_slices persist -> follow-up persist=False still hits cold projection -> persist_failed=1, warmed=0
    tid = str(uuid.uuid4())
    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = False
    cur.fetchall.return_value = [{"thread_id": tid}]
    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    conn.cursor.return_value = cur

    @asynccontextmanager
    async def _fake_conn():
        yield conn

    monkeypatch.setattr(db_pool, "get_db_connection", _fake_conn)
    fake_rows = replay_rows_mod.ThreadRows(
        thread_id=tid,
        owner_id="u1",
        thread={"conversation_thread_id": tid},
        queries=[],
        responses_by_turn={},
        usages=[],
        provenance=[],
        run_facts=[],
    )
    monkeypatch.setattr(
        replay_rows_mod,
        "get_replay_thread_data",
        create_autospec(replay_rows_mod.get_replay_thread_data, return_value=fake_rows),
    )

    async def _always_cold_read(
        rows: Any, *, before_turn: Any = None, limit: int = 200, budget: Any = None, persist: bool = True
    ):
        share_redaction.mark_cold_projection_miss()
        return replay_mod.ReplayPage(lines=[], first_turn_index=0, has_more=False), "checkpoint"

    monkeypatch.setattr(
        replay_mod,
        "read_replay_page",
        create_autospec(replay_mod.read_replay_page, side_effect=_always_cold_read),
    )
    res = await ops_script.warm_shared_slices(manage_pools=False)
    assert res["persist_failed"] == 1
    assert res["warmed"] == 0
    assert res["cache_hit"] == 0


# =============================================================================
# AU1 r5 REGRESSION TESTS (Items 1, 2, 3, 4, 7, 8)
# =============================================================================


@pytest.mark.asyncio
async def test_r5_cross_phase_prefix_and_blockquote_fence_thinking_absent_across_cold_slice_and_sse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AU1 r5 #1: Same inputs (prefix + cross-phase + blockquote fenced literal closer) have zero thinking body in cold projection, stored turn_slices row, and public SSE."""
    import httpx
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from src.server.database.conversation import replay_rows as replay_rows_mod
    from src.server.database.conversation import turn_slices as slices_db
    from src.server.services.history import replay as replay_mod
    from src.server.services.history import slices as slices_mod
    from src.server.services.history.reader import CheckpointHistoryReader, TurnAnchor
    from src.server.services.history.replay import lines as replay_lines

    cases = [
        (
            "prefix_cross_phase",
            [
                {
                    "type": "text",
                    "text": "Working on it.\n<thinking>SECRET_PREFIX_CROSS_PART1 ",
                    "phase": "commentary",
                },
                {
                    "type": "text",
                    "text": "SECRET_PREFIX_CROSS_PART2</thinking>Public Answer One.",
                    "phase": "final_answer",
                },
            ],
            ("SECRET_PREFIX_CROSS_PART1", "SECRET_PREFIX_CROSS_PART2"),
            "Public Answer One.",
        ),
        (
            "blockquote_fence_cross_phase",
            [
                {
                    "type": "text",
                    "text": (
                        "Checking details.\n"
                        "<thinking>SECRET_BQ_TOP\n"
                        "> ```xml\n"
                        "> SECRET_BQ_INSIDE_FENCE </thinking>\n"
                        "> ```\n"
                        "SECRET_BQ_AFTER_FENCE "
                    ),
                    "phase": "commentary",
                },
                {
                    "type": "text",
                    "text": "SECRET_BQ_IN_FINAL</thinking>Public Answer Two.",
                    "phase": "final_answer",
                },
            ],
            (
                "SECRET_BQ_TOP",
                "SECRET_BQ_INSIDE_FENCE",
                "SECRET_BQ_AFTER_FENCE",
                "SECRET_BQ_IN_FINAL",
            ),
            "Public Answer Two.",
        ),
        (
            "blockquote_fence_opening_single_phase",
            (
                "<thinking>SECRET_SINGLE_TOP\n"
                "> ```\n"
                "> SECRET_SINGLE_BQ </thinking>\n"
                "> ```\n"
                "SECRET_SINGLE_TAIL</thinking>Public Answer Three."
            ),
            ("SECRET_SINGLE_TOP", "SECRET_SINGLE_BQ", "SECRET_SINGLE_TAIL"),
            "Public Answer Three.",
        ),
        (
            "blockquote_tab_tilde_fence_cross_phase",
            [
                {
                    "type": "text",
                    "text": (
                        "Checking tab fence.\n"
                        "<thinking>SECRET_BQTAB_TOP\n"
                        ">\t~~~\n"
                        ">\tSECRET_BQTAB_INSIDE_FENCE </thinking>\n"
                        ">\t~~~\n"
                        "SECRET_BQTAB_AFTER_FENCE "
                    ),
                    "phase": "commentary",
                },
                {
                    "type": "text",
                    "text": "SECRET_BQTAB_IN_FINAL</thinking>Public Answer Four.",
                    "phase": "final_answer",
                },
            ],
            (
                "SECRET_BQTAB_TOP",
                "SECRET_BQTAB_INSIDE_FENCE",
                "SECRET_BQTAB_AFTER_FENCE",
                "SECRET_BQTAB_IN_FINAL",
            ),
            "Public Answer Four.",
        ),
        (
            "blockquote_tab_tilde_fence_opening_single_phase",
            (
                "<thinking>SECRET_SINGLE_TAB_TOP\n"
                ">\t~~~\n"
                ">\tSECRET_SINGLE_TAB_BQ </thinking>\n"
                ">\t~~~\n"
                "SECRET_SINGLE_TAB_TAIL</thinking>Public Answer Five."
            ),
            ("SECRET_SINGLE_TAB_TOP", "SECRET_SINGLE_TAB_BQ", "SECRET_SINGLE_TAB_TAIL"),
            "Public Answer Five.",
        ),
    ]

    app = FastAPI()
    app.include_router(public_app.router)
    transport = httpx.ASGITransport(app=app)

    orig_get_turn_lines = slices_db.get_turn_lines
    orig_get_turn_slices = slices_db.get_turn_slices
    orig_upsert_turn_slices = slices_db.upsert_turn_slices
    orig_update_turns = slices_db.update_turns
    orig_pub_get_shared = public_app.get_shared_thread
    orig_access_get_shared = share_access.get_shared_thread
    orig_get_replay_data = public_app.get_replay_thread_data
    orig_get_instance = CheckpointHistoryReader.get_instance
    orig_resolve_perms = share_redaction.resolve_effective_share_permissions

    for case_name, content_blocks, forbidden_secrets, expected_answer in cases:
        tid = str(uuid.uuid4())
        rid = str(uuid.uuid4())
        share_token = f"tok-r5-{case_name}"
        fake_thread = {
            "conversation_thread_id": tid,
            "workspace_id": str(uuid.uuid4()),
            "user_id": "u1",
            "owner_id": "u1",
            "title": f"Case {case_name}",
            "share_token": share_token,
            "is_shared": True,
            "share_permissions": {"allow_files": False, "allow_download": False},
            "latest_checkpoint_id": "cp-tail-0",
        }
        rows = replay_rows_mod.ThreadRows(
            thread_id=tid,
            owner_id="u1",
            thread=fake_thread,
            queries=[
                {
                    "conversation_query_id": str(uuid.uuid4()),
                    "turn_index": 0,
                    "type": "initial",
                    "content": "Summarize",
                    "metadata": {},
                }
            ],
            responses_by_turn={
                0: {
                    "conversation_response_id": rid,
                    "turn_index": 0,
                    "status": "completed",
                    "replay_facts": {},
                }
            },
            usages=[],
            provenance=[],
            run_facts=[],
            open_runs={},
            failed_steers=[],
        )
        anchor = TurnAnchor(
            turn_ordinal=0,
            input_checkpoint_id="cp-in-0",
            tail_checkpoint_id="cp-tail-0",
            turn_index=0,
            end_checkpoint_id="cp-tail-0",
        )
        fake_slice = slices_mod.TurnSlice(
            anchor=anchor,
            messages=[
                HumanMessage(id="h-0", content="Summarize"),
                AIMessage(id=f"ai-{case_name}", content=content_blocks),
            ],
        )

        reader_stub = create_autospec(CheckpointHistoryReader, instance=True)
        reader_stub.serde = JsonPlusSerializer()
        reader_stub.aget_turn_anchors.return_value = ([anchor], "cp-tail-0")
        reader_stub.aget_turn_slices.return_value = [fake_slice]
        reader_stub.aget_tip_interrupts.return_value = []
        monkeypatch.setattr(
            CheckpointHistoryReader,
            "get_instance",
            create_autospec(orig_get_instance, return_value=reader_stub),
        )

        stored_slices: dict[str, slices_db.StoredSlice] = {}

        async def _get_turn_lines(rids: list[str], _store=stored_slices) -> dict[str, slices_db.StoredSlice]:
            return {r: _store[r] for r in rids if r in _store}

        async def _get_turn_slices(rids: list[str], _store=stored_slices) -> dict[str, slices_db.StoredSlice]:
            return {r: _store[r] for r in rids if r in _store}

        async def _upsert_turn_slices(turn_rows: list[slices_db.TurnRow], _store=stored_slices) -> None:
            for tr in turn_rows:
                _store[tr.response_id] = tr.slice

        async def _update_turns(updates: list[slices_db.TurnUpdate], _store=stored_slices) -> None:
            for u in updates:
                cur = _store.get(u.response_id)
                if cur is not None:
                    _store[u.response_id] = cur._replace(
                        lines_key=u.lines_key or cur.lines_key,
                        lines=u.lines or cur.lines,
                    )

        monkeypatch.setattr(slices_db, "get_turn_lines", create_autospec(orig_get_turn_lines, side_effect=_get_turn_lines))
        monkeypatch.setattr(slices_db, "get_turn_slices", create_autospec(orig_get_turn_slices, side_effect=_get_turn_slices))
        monkeypatch.setattr(slices_db, "upsert_turn_slices", create_autospec(orig_upsert_turn_slices, side_effect=_upsert_turn_slices))
        monkeypatch.setattr(slices_db, "update_turns", create_autospec(orig_update_turns, side_effect=_update_turns))
        monkeypatch.setattr(public_app, "get_shared_thread", create_autospec(orig_pub_get_shared, return_value=fake_thread))
        monkeypatch.setattr(share_access, "get_shared_thread", create_autospec(orig_access_get_shared, return_value=fake_thread))
        monkeypatch.setattr(public_app, "get_replay_thread_data", create_autospec(orig_get_replay_data, return_value=rows))
        monkeypatch.setattr(
            share_redaction,
            "resolve_effective_share_permissions",
            create_autospec(
                orig_resolve_perms,
                return_value=share_redaction.EffectiveSharePermissions(
                    allow_files=False, allow_download=False
                ),
            ),
        )

        # 1. Cold projection
        cold_page, source = await replay_mod.read_replay_page(rows, persist=True)
        assert source == "checkpoint"
        cold_blob = "\n".join(ln.data_json for ln in cold_page.lines)
        for secret in forbidden_secrets:
            assert secret not in cold_blob, f"{case_name}: secret leaked in cold projection"
        assert expected_answer in cold_blob

        # 2. Stored turn_slices row
        assert rid in stored_slices
        stored_lines_str = stored_slices[rid].lines or ""
        assert stored_lines_str
        for secret in forbidden_secrets:
            assert secret not in stored_lines_str, f"{case_name}: secret leaked in stored turn_slices row"
        assert expected_answer in stored_lines_str

        # 3. Public SSE (cache-hit from stored turn_slices)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            resp = await client.get(f"/api/v1/public/shared/{share_token}/replay")
        assert resp.status_code == 200
        for secret in forbidden_secrets:
            assert secret not in resp.text, f"{case_name}: secret leaked in public SSE"
        assert expected_answer in resp.text

        # 4. Also verify direct strict SSE redaction on raw unprojected cross-phase chunks
        if isinstance(content_blocks, list):
            redactor = share_redaction.StrictNoFilesShareRedactor(
                thread_id=tid,
                responses_by_turn={0: {"status": "completed"}},
            )
            raw_lines = [
                replay_lines.line_of(
                    {
                        "event": "user_message",
                        "data": {"thread_id": tid, "turn_index": 0, "content": "Summarize"},
                    }
                ),
                *(
                    replay_lines.line_of(
                        {
                            "event": "message_chunk",
                            "data": {
                                "thread_id": tid,
                                "turn_index": 0,
                                "agent": "main",
                                "id": f"ai-{case_name}",
                                "content": blk["text"],
                                "content_type": "text",
                                "phase": blk["phase"],
                                "finish_reason": "stop" if blk["phase"] == "final_answer" else None,
                            },
                        }
                    )
                    for blk in content_blocks
                ),
            ]
            sse_from_raw = list(redactor.iter_redacted_events(raw_lines))
            raw_sse_blob = json.dumps(sse_from_raw, ensure_ascii=False)
            for secret in forbidden_secrets:
                assert secret not in raw_sse_blob, f"{case_name}: secret leaked in strict SSE on raw chunks"
            assert expected_answer in raw_sse_blob


@pytest.mark.asyncio
async def test_r5_omit_permissions_patch_does_not_write_share_permissions_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AU1 r5 #2: When permissions field is omitted (patch is None), atomic SQL MUST NOT touch share_permissions."""
    from contextlib import asynccontextmanager
    import psycopg
    from src.server.database import pool as db_pool
    from src.server.database.conversation import threads_write

    executed_sqls: list[tuple[str, tuple[Any, ...]]] = []
    # Simulated DB state after concurrent revocation set allow_files=False
    db_state = {"allow_files": False, "allow_download": False, "audience": "public"}

    async def _fake_execute(sql: str, params: tuple[Any, ...] = ()) -> None:
        executed_sqls.append((sql, tuple(params)))
        if "share_permissions =" in sql:
            # Would clobber DB state if buggy code wrote share_permissions
            db_state["allow_files"] = True

    async def _fake_fetchone() -> dict[str, Any]:
        return {
            "conversation_thread_id": "t-race",
            "workspace_id": "ws-1",
            "share_token": "tok-1",
            "is_shared": True,
            "share_permissions": dict(db_state),
            "shared_at": None,
            "current_status": "completed",
            "msg_type": "ptc",
            "title": "t",
            "created_at": None,
            "updated_at": None,
        }

    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = False
    cur.execute.side_effect = _fake_execute
    cur.fetchone.side_effect = _fake_fetchone
    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    conn.cursor.return_value = cur

    @asynccontextmanager
    async def _fake_conn():
        yield conn

    monkeypatch.setattr(db_pool, "get_db_connection", _fake_conn)

    # Stale read had allow_files=True; request omits permissions -> patch is None
    stale_eff, patch = share_redaction.prepare_share_permissions_update(
        {"allow_files": True, "allow_download": True, "audience": "public"}, None
    )
    assert patch is None
    updated = await share_redaction.apply_thread_share_update(
        threads_write.update_thread_sharing,
        "t-race",
        is_shared=True,
        shared_at=None,
        effective_perms=stale_eff,
        patch=patch,
    )
    assert updated is not None
    assert len(executed_sqls) == 1
    sql_text = executed_sqls[0][0]
    assert "share_permissions =" not in sql_text
    assert db_state["allow_files"] is False
    assert updated["share_permissions"]["allow_files"] is False


@pytest.mark.asyncio
async def test_r5_paginated_media_registry_sql_joins_conversation_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AU1 r5 #3: Paginated _load_thread_media_registry_from_db joins turn_slices with conversation_responses on conversation_response_id."""
    from contextlib import asynccontextmanager
    import psycopg
    from src.server.database import pool as db_pool

    sql_calls: list[str] = []

    async def _record_exec(sql: str, params: tuple[Any, ...] = ()) -> None:
        sql_calls.append(sql)

    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = False
    cur.execute.side_effect = _record_exec
    cur.fetchall.return_value = []
    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    conn.cursor.return_value = cur

    @asynccontextmanager
    async def _fake_conn():
        yield conn

    monkeypatch.setattr(db_pool, "get_db_connection", _fake_conn)

    reg = await share_media._load_thread_media_registry_from_db(
        "11111111-1111-4111-8111-111111111111",
        owner_id="u1",
        turn_indices=[1, 2],
    )
    assert reg is not None
    slice_queries = [s for s in sql_calls if "FROM turn_slices" in s]
    assert len(slice_queries) == 1
    q = slice_queries[0]
    assert "JOIN conversation_responses" in q
    assert "conversation_response_id" in q
    assert "r.turn_index = ANY" in q
    assert "turn_slices.turn_index" not in q
    assert "position('steering_delivered' in ts.lines) > 0" in q
    assert '"steering_delivered"' not in q

    sql_calls.clear()
    reg_full = await share_media._load_thread_media_registry_from_db(
        "11111111-1111-4111-8111-111111111111",
        owner_id="u1",
        turn_indices=None,
    )
    assert reg_full is not None
    full_slice_queries = [s for s in sql_calls if "FROM turn_slices" in s]
    assert len(full_slice_queries) == 1
    q_full = full_slice_queries[0]
    assert "position('steering_delivered' in lines) > 0" in q_full
    assert '"steering_delivered"' not in q_full


@pytest.mark.asyncio
async def test_r5_attachment_keys_with_spaces_chinese_percent_encoding_and_unsigned_url_erasure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AU1 r5 #4: Spaces, parentheses, Chinese, and percent-encoded filenames extract full object keys; unsigned configured-storage URLs are erased."""
    from src.server.services.history.replay import lines as replay_lines
    from src.server.utils.multimodal_context import _object_url

    monkeypatch.setenv("DALTON_MEDIA_SIGNING_SECRET", "r5-url-key-test-secret-32-bytes!!")
    monkeypatch.setenv("STORAGE_PUBLIC_URL_BASE", "https://cdn.files.example")
    monkeypatch.setenv("STORAGE_PROVIDER", "s3")
    monkeypatch.setenv("S3_BUCKET_NAME", "files-bucket")
    monkeypatch.setenv("S3_ENDPOINT_URL", "https://s3.files.example")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "sk")

    from src.server.utils import multimodal_context as mm_ctx

    tid = "t-r5-names"

    def _fake_public_url(key: str) -> str:
        return f"https://cdn.files.example/{key.lstrip('/')}"

    monkeypatch.setattr(
        mm_ctx,
        "get_public_url",
        create_autospec(mm_ctx.get_public_url, side_effect=_fake_public_url),
    )

    test_keys = [
        f"private-media/attachments/{tid}/b1/f1/Q1 Report (Final).pdf",
        f"private-media/attachments/{tid}/b1/f2/2026 季度财报 摘要.pdf",
        f"private-media/attachments/{tid}/b1/f3/report %20 v2 #1?x=1.pdf",
    ]
    urls = [_object_url(k) for k in test_keys]
    for orig_key, url in zip(test_keys, urls):
        assert share_media.extract_upstream_key_from_url(url) == orig_key

    # Encoded traversal %2F must be rejected
    assert (
        share_media.extract_upstream_key_from_url(
            f"https://cdn.files.example/private-media/attachments/{tid}/..%2Fsecret.pdf"
        )
        is None
    )

    # Unsigned configured-storage URLs MUST be erased (not left verbatim) even when bindings is empty or partial
    unsigned_url = f"https://cdn.files.example/private-media/response-images/{tid}/unregistered%20img.png"
    external_url = "https://public.external.org/readme.pdf"
    items = [
        {
            "event": "user_message",
            "data": {
                "thread_id": tid,
                "turn_index": 1,
                "content": f"See {unsigned_url} and {external_url}",
                "metadata": {
                    "attachments": [
                        {"name": "Q1 Report (Final).pdf", "url": urls[0]},
                        {"name": "unregistered.pdf", "url": unsigned_url},
                        {"name": "ext.pdf", "url": external_url},
                    ]
                },
            },
        }
    ]

    # Case A: partial bindings (only urls[0] signed; unsigned_url NOT in bindings)
    partial_out = share_media.rewrite_items_for_public_media(
        items,
        thread_id=tid,
        share_token="tok-r5",
        bindings={urls[0]: ("upstream", test_keys[0])},
    )
    atts_a = partial_out[0]["data"]["metadata"]["attachments"]
    assert atts_a[0]["url"].startswith("/api/v1/public/shared/tok-r5/media/")
    assert "url" not in atts_a[1]
    assert atts_a[2]["url"] == external_url
    assert unsigned_url not in partial_out[0]["data"]["content"]
    assert "[media-unavailable]" in partial_out[0]["data"]["content"]
    assert external_url in partial_out[0]["data"]["content"]

    # Case B: empty bindings ({}) MUST still erase configured-storage URLs
    empty_out = share_media.rewrite_items_for_public_media(
        items, thread_id=tid, share_token="tok-r5", bindings={}
    )
    assert unsigned_url not in json.dumps(empty_out)
    assert urls[0] not in json.dumps(empty_out)
    assert external_url in json.dumps(empty_out)

    # Case C: owner replay lines with empty bindings ({}) MUST also erase unsigned configured-storage URLs
    owner_lines = [replay_lines.line_of(items[0])]
    rewritten_owner = share_media.rewrite_lines_for_owner_media(
        owner_lines, thread_id=tid, viewer_id="u1", bindings={}
    )
    assert unsigned_url not in rewritten_owner[0].data_json
    assert urls[0] not in rewritten_owner[0].data_json
    assert external_url in rewritten_owner[0].data_json


def test_r5_acl_probe_method_not_allowed_and_not_implemented_stay_unknown_uncached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AU1 r5 #7: MethodNotAllowed / NotImplemented on GetBucketOwnershipControls map to unknown (uncached); explicit bucket modes & write rejection cache."""
    from botocore.exceptions import ClientError

    class _S3ProbeAndPutSpec:
        def get_bucket_ownership_controls(self, *, Bucket: str) -> dict[str, Any]:
            raise NotImplementedError

        def generate_presigned_url(
            self, ClientMethod: str, Params: dict[str, Any] | None = None, ExpiresIn: int = 3600
        ) -> str:
            raise NotImplementedError

        def put_object(self, **kwargs: Any) -> dict[str, Any]:
            raise NotImplementedError

    monkeypatch.delenv("DALTON_STORAGE_DISABLE_OBJECT_ACL", raising=False)
    monkeypatch.delenv("STORAGE_DISABLE_OBJECT_ACL", raising=False)
    monkeypatch.setenv("DALTON_STORAGE_OBJECT_ACL", "auto")
    share_media.reset_bucket_acl_cache_for_test()

    try:
        for err_code in ("MethodNotAllowed", "NotImplemented"):
            client = create_autospec(_S3ProbeAndPutSpec, instance=True)
            client.get_bucket_ownership_controls.side_effect = ClientError(
                {"Error": {"Code": err_code, "Message": "not supported on probe"}},
                "GetBucketOwnershipControls",
            )
            client.generate_presigned_url.return_value = "https://s3.compat.example/put"
            mode1 = share_media.probe_bucket_object_acl_mode(
                client, f"bucket-{err_code}", endpoint_url="https://s3.compat.example"
            )
            mode2 = share_media.probe_bucket_object_acl_mode(
                client, f"bucket-{err_code}", endpoint_url="https://s3.compat.example"
            )
            assert mode1 == "unknown"
            assert mode2 == "unknown"
            # Must NOT be cached: probed twice
            assert client.get_bucket_ownership_controls.call_count == 2
            _, hdrs = share_media.s3_presign_put_with_private_acl(
                client,
                bucket=f"bucket-{err_code}",
                key="blobs/k1",
                checksum_b64="AAAA",
                content_length=5,
                content_type="application/octet-stream",
                expires_in=60,
                endpoint_url="https://s3.compat.example",
            )
            assert "x-amz-acl" not in hdrs
            assert "ACL" not in client.generate_presigned_url.call_args.kwargs["Params"]

        # Actual write rejection DOES cache disabled
        write_client = create_autospec(_S3ProbeAndPutSpec, instance=True)
        write_client.get_bucket_ownership_controls.side_effect = ClientError(
            {"Error": {"Code": "MethodNotAllowed", "Message": "no ownership API"}},
            "GetBucketOwnershipControls",
        )
        write_client.put_object.side_effect = [
            ClientError(
                {"Error": {"Code": "AccessControlListNotSupported", "Message": "ACLs disabled"}},
                "PutObject",
            ),
            {"ETag": "ok"},
        ]
        share_media.s3_put_object_with_private_acl(
            write_client,
            bucket="write-reject-bucket",
            key="blobs/k2",
            body=b"hi",
            content_type="text/plain",
            endpoint_url="https://s3.compat.example",
        )
        assert (
            share_media.probe_bucket_object_acl_mode(
                write_client, "write-reject-bucket", endpoint_url="https://s3.compat.example"
            )
            == "disabled"
        )
    finally:
        share_media.reset_bucket_acl_cache_for_test()


def test_r5_historical_url_decoded_grant_path_and_userinfo_only_redaction(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AU1 r5 #8: Percent-encoded grant path is rejected without GET; userinfo-only historical URL marks ?<redacted>."""
    probed: list[str] = []

    def _fake_get(url: str, timeout_s: float = 10.0) -> tuple[int, str]:
        probed.append(url)
        return 403, "Forbidden"

    encoded_grant = "https://app.example/api/v1/artifacts/g/v1.ENC_GRANT_SECRET%2Fv%2F1/index.html"
    double_encoded_grant = "https://app.example/api/v1/wsfiles/g/v1.DOUBLE_SECRET%252Freport.pdf"
    userinfo_only = "https://bob:s3cr3tpass@bucket.s3.us-east-1.amazonaws.com/blobs/u1/doc.pdf"

    ok, checks = ops_script.verify_storage_targets(
        targets=[],
        known_historical_urls=[encoded_grant, double_encoded_grant, userinfo_only],
        probe_write=False,
        http_get_fn=_fake_get,
    )
    assert ok is False
    assert probed == ["https://bucket.s3.us-east-1.amazonaws.com/blobs/u1/doc.pdf"]
    assert checks[0].ok is False and checks[0].status_code is None
    assert "ENC_GRANT_SECRET" not in checks[0].url
    assert checks[1].ok is False and checks[1].status_code is None
    assert "DOUBLE_SECRET" not in checks[1].url
    assert checks[2].ok is True
    assert checks[2].url == "https://bucket.s3.us-east-1.amazonaws.com/blobs/u1/doc.pdf?<redacted>"
    assert "bob" not in checks[2].url and "s3cr3tpass" not in checks[2].url

    monkeypatch.setattr(
        ops_script,
        "resolve_active_storage_targets",
        create_autospec(ops_script.resolve_active_storage_targets, return_value=[]),
    )
    monkeypatch.setattr(
        ops_script,
        "_is_runtime_storage_enabled",
        create_autospec(ops_script._is_runtime_storage_enabled, return_value=False),
    )
    monkeypatch.setattr(
        ops_script,
        "_http_anonymous_get_status",
        create_autospec(ops_script._http_anonymous_get_status, side_effect=_fake_get),
    )
    rc = ops_script.main(
        ["--verify", "--no-probe-write", "--json", "--historical-url", userinfo_only]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "s3cr3tpass" not in out
    assert "bob" not in out
    assert "https://bucket.s3.us-east-1.amazonaws.com/blobs/u1/doc.pdf?<redacted>" in out
