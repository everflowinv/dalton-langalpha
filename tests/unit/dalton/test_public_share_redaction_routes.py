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
                },
                # tid_deleted intentionally absent (physically deleted thread - B6)
            },
        ),
    )

    raw_lib_rows = [
        {
            "item_id": item_legacy,
            "kind": "thread",
            "thread_id": tid_alive,
            "title": "SECRET_AUTO_LIB_01",
            "summary": "Check /api/v1/artifacts/g/v1.grant_lib_summary_01/v/1/a.html and /Users/alice/ws/secret.py",
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
    assert redacted_lib[1]["title"] == "公开策略周报"
    assert "grant_lib_summary_01" not in redacted_lib[1]["summary"]
    # B6: physically deleted thread row stays stable and sanitized
    assert redacted_lib[2]["title"] == DEFAULT_SHARED_TITLE
    assert "grant_deleted" not in redacted_lib[2]["summary"]

    # Activity log: early auto-title row stays Shared Conversation even after item got custom title later,
    # new custom_title=True row keeps explicit title, and all other Action rows have detail scrubbed.
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
            "detail": {"path": "/Users/alice/ws/SECRET_ACT_PATH_01.html", "note": "/api/v1/artifacts/g/v1.grant_act/v/1/x.html"},
            "created_at": now,
        },
        {
            "event_id": str(uuid.uuid4()),
            "action": activity_svc.Action.LIBRARY_PUBLISHED.value,
            "thread_id": tid_deleted,
            "detail": {"kind": "thread", "title": "SECRET_DELETED_ACT"},
            "created_at": now,
        },
    ]
    redacted_act = await share_redaction.redact_activity_rows(raw_activity)
    assert len(redacted_act) == 4
    assert redacted_act[0]["detail"]["title"] == DEFAULT_SHARED_TITLE
    assert "custom_title" not in redacted_act[0]["detail"]
    assert redacted_act[1]["detail"]["title"] == "公开策略周报"
    assert "custom_title" not in redacted_act[1]["detail"]
    assert redacted_act[2]["detail"]["path"] == "[redacted-path]"
    assert "SECRET_ACT_PATH_01" not in json.dumps(redacted_act[2]["detail"])
    assert "grant_act" not in json.dumps(redacted_act[2]["detail"])
    assert redacted_act[3]["detail"]["title"] == DEFAULT_SHARED_TITLE


@pytest.mark.asyncio
async def test_singleflight_coalesces_concurrent_cold_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    gate = asyncio.Event()

    async def _slow_read(rows, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs.get("persist") is False
        await gate.wait()
        return ReplayPage(lines=[], first_turn_index=0, has_more=False), "checkpoint"

    from src.server.services.history import replay as replay_mod

    monkeypatch.setattr(replay_mod, "read_replay_page", _slow_read)
    monkeypatch.setattr(replay_mod, "lines_epoch", lambda: "test-epoch-1")

    rows = SimpleNamespace(thread_id=str(uuid.uuid4()))

    tasks = [
        asyncio.create_task(
            share_redaction.read_strict_public_replay_page(
                rows, trigger_background_warm=False
            )
        )
        for _ in range(10)
    ]
    await asyncio.sleep(0.02)
    gate.set()
    results = await asyncio.gather(*tasks)
    assert len(results) == 10
    assert calls == 1


@pytest.mark.asyncio
async def test_ops_script_verify_and_warm(monkeypatch: pytest.MonkeyPatch) -> None:
    target_a = share_media.ActiveStorageTarget(
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

    seen_urls: list[str] = []

    def _fake_get_403(url: str, *, timeout_s: float = 10.0) -> tuple[int | None, str]:
        seen_urls.append(url)
        return 403, "Forbidden"

    ok, checks = ops_script.verify_storage_targets(
        targets=[target_a], probe_write=False, http_get_fn=_fake_get_403
    )
    assert ok is True
    assert len(checks) == 8  # 4 prefixes x (origin + CDN)
    assert any("cdn.example.com/provenance/" in u for u in seen_urls)
    assert any("127.0.0.1:9000/primary-bucket/memo/" in u for u in seen_urls)

    # Any 200 immediately fails verification
    def _fake_get_leak(url: str, *, timeout_s: float = 10.0) -> tuple[int | None, str]:
        return (200, "OK") if "/provenance/" in url else (403, "Forbidden")

    ok2, checks2 = ops_script.verify_storage_targets(
        targets=[target_a], probe_write=False, http_get_fn=_fake_get_leak
    )
    assert ok2 is False
    assert any(not c.ok and c.status_code == 200 for c in checks2)


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
