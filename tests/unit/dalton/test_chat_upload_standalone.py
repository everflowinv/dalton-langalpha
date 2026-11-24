"""Unit tests for Standalone Core chat upload storage decoupling, HMAC sandbox relay, and init.sh."""

from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec, patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from ptc_agent.config.core import DockerConfig
from ptc_agent.config.file_utils import load_yaml_config
from ptc_agent.config.loaders import load_from_dict
from ptc_agent.core.paths import WorkspaceLayout
from ptc_agent.core.sandbox import wsfiles_transfer_runtime as transfer_rt
from ptc_agent.core.sandbox.providers.docker import DockerProvider
from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
from src.dalton import chat_upload_storage as storage
from src.dalton import transcription_storage as asr_storage
from src.dalton.app.chat_uploads import (
    router as chat_uploads_router,
)
from src.dalton.app.chat_uploads import (
    sandbox_relay_router,
)
from src.dalton.auth import sessions as local_auth_sessions
from src.dalton.database import chat_uploads as db_chat_uploads
from src.dalton.services import chat_uploads as svc
from src.dalton.settings import get_dalton_config
from src.server.services.egress import reachability as egress_reachability

REPO_ROOT = Path(__file__).resolve().parents[3]


def _clear_storage_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for prefix in ("DALTON_CHAT_UPLOAD_STORAGE_", "DALTON_ASR_STORAGE_"):
        for suffix in (
            "ENDPOINT_URL",
            "SIGNING_ENDPOINT_URL",
            "BUCKET_NAME",
            "ACCESS_KEY_ID",
            "SECRET_ACCESS_KEY",
            "REGION",
            "ADDRESSING_STYLE",
            "KEY_PREFIX",
        ):
            monkeypatch.delenv(f"{prefix}{suffix}", raising=False)
    storage._reset_clients_for_test()
    asr_storage._reset_clients_for_test()


def _clear_signing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "DALTON_CHAT_UPLOAD_SIGNING_SECRET",
        "INTERNAL_SERVICE_TOKEN",
        "BYOK_ENCRYPTION_KEY",
        "JWT_SIGNING_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)


def _make_ready_row(
    *,
    payload: bytes,
    workspace_id: str | None = None,
    upload_id: str | None = None,
    filename: str = "report.pdf",
) -> dict[str, Any]:
    ws_id = workspace_id or str(uuid.uuid4())
    uid = upload_id or str(uuid.uuid4())
    sha256 = hashlib.sha256(payload).hexdigest()
    object_key = storage.build_chat_upload_key(ws_id, uid, filename)
    return {
        "upload_id": uid,
        "user_id": "local-dev-user",
        "workspace_id": ws_id,
        "filename": filename,
        "safe_filename": filename,
        "mime_type": "application/pdf",
        "size_bytes": len(payload),
        "object_key": object_key,
        "actual_sha256": sha256,
        "expected_sha256": sha256,
        "state": "ready",
        "cleaned_at": None,
    }


def _make_test_app(*, include_relay: bool = True) -> FastAPI:
    app = FastAPI()
    if include_relay:
        app.include_router(sandbox_relay_router)
    app.include_router(chat_uploads_router)
    return app


class TestChatUploadStorageDecoupling:
    """DALTON_CHAT_UPLOAD_STORAGE_* takes precedence over DALTON_ASR_STORAGE_*."""

    def test_prefers_dedicated_chat_upload_env_over_asr_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_storage_env(monkeypatch)
        monkeypatch.setenv("DALTON_ASR_STORAGE_ENDPOINT_URL", "http://asr-minio:9000")
        monkeypatch.setenv("DALTON_ASR_STORAGE_BUCKET_NAME", "asr-bucket")
        monkeypatch.setenv("DALTON_ASR_STORAGE_ACCESS_KEY_ID", "asr-key")
        monkeypatch.setenv("DALTON_ASR_STORAGE_SECRET_ACCESS_KEY", "asr-secret")
        monkeypatch.setenv("DALTON_ASR_STORAGE_KEY_PREFIX", "asr-prefix")

        monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL", "http://minio:9000/")
        monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME", "chat-uploads")
        monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID", "chat-key")
        monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY", "chat-secret")
        monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_REGION", "eu-west-1")
        monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_ADDRESSING_STYLE", "path")

        assert storage.is_chat_upload_storage_configured() is True
        assert storage._endpoint_url() == "http://minio:9000"
        assert storage.signing_endpoint_url() == "http://minio:9000"
        assert storage._bucket_name() == "chat-uploads"
        assert storage._access_key_id() == "chat-key"
        assert storage._secret_access_key() == "chat-secret"
        assert storage._region() == "eu-west-1"
        assert storage._addressing_style() == "path"
        assert storage.resolve_full_key("chat-uploads/ws/u/a.pdf") == "chat-uploads/ws/u/a.pdf"

    def test_falls_back_to_asr_storage_when_dedicated_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_storage_env(monkeypatch)
        assert storage.is_chat_upload_storage_configured() is False

        monkeypatch.setenv("DALTON_ASR_STORAGE_ENDPOINT_URL", "http://127.0.0.1:9000")
        monkeypatch.setenv(
            "DALTON_ASR_STORAGE_SIGNING_ENDPOINT_URL", "http://host.docker.internal:9000"
        )
        monkeypatch.setenv("DALTON_ASR_STORAGE_BUCKET_NAME", "dalton-asr")
        monkeypatch.setenv("DALTON_ASR_STORAGE_ACCESS_KEY_ID", "asr-key")
        monkeypatch.setenv("DALTON_ASR_STORAGE_SECRET_ACCESS_KEY", "asr-secret")
        monkeypatch.setenv("DALTON_ASR_STORAGE_KEY_PREFIX", "everflow")

        assert storage.is_chat_upload_storage_configured() is True
        assert storage._endpoint_url() == "http://127.0.0.1:9000"
        assert storage.signing_endpoint_url() == "http://host.docker.internal:9000"
        assert storage._bucket_name() == "dalton-asr"
        assert storage.resolve_full_key("chat-uploads/ws/u/a.pdf") == "everflow/chat-uploads/ws/u/a.pdf"

    def test_partial_dedicated_env_warns_without_values_and_falls_back_to_asr(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _clear_storage_env(monkeypatch)
        monkeypatch.setenv("DALTON_ASR_STORAGE_ENDPOINT_URL", "http://asr-minio:9000")
        monkeypatch.setenv(
            "DALTON_ASR_STORAGE_SIGNING_ENDPOINT_URL", "http://host.docker.internal:9000"
        )
        monkeypatch.setenv("DALTON_ASR_STORAGE_BUCKET_NAME", "asr-bucket")
        monkeypatch.setenv("DALTON_ASR_STORAGE_ACCESS_KEY_ID", "asr-key")
        monkeypatch.setenv("DALTON_ASR_STORAGE_SECRET_ACCESS_KEY", "asr-secret")
        monkeypatch.setenv("DALTON_ASR_STORAGE_KEY_PREFIX", "everflow")

        # Set only 2 of the 4 required DALTON_CHAT_UPLOAD_STORAGE_* primary vars
        partial_endpoint = "http://partial-chat-minio:9000"
        partial_access = "super-secret-partial-access-key-999"
        monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL", partial_endpoint)
        monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID", partial_access)
        monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_REGION", "eu-west-1")
        monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_KEY_PREFIX", "partial-prefix")

        with caplog.at_level("WARNING", logger=storage.logger.name):
            assert storage.is_chat_upload_storage_configured() is True
            assert storage._endpoint_url() == "http://asr-minio:9000"
            assert storage.signing_endpoint_url() == "http://host.docker.internal:9000"
            assert storage._bucket_name() == "asr-bucket"
            assert storage._access_key_id() == "asr-key"
            assert storage._secret_access_key() == "asr-secret"
            assert storage.resolve_full_key("chat-uploads/ws/u/a.pdf") == "everflow/chat-uploads/ws/u/a.pdf"

        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME" in warnings[0]
        assert "DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY" in warnings[0]
        assert partial_endpoint not in warnings[0]
        assert partial_access not in warnings[0]

        # When ASR fallback is also absent, partial dedicated env is treated as unconfigured
        _clear_storage_env(monkeypatch)
        monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL", partial_endpoint)
        assert storage.is_chat_upload_storage_configured() is False


class TestSandboxObjectRelayTokensAndEndpoint:
    """Short-lived, single-object HMAC header token validation and streaming endpoint tests."""

    def test_missing_or_predictable_signing_secret_fails_closed_with_503(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_signing_env(monkeypatch)
        row = _make_ready_row(payload=b"secret-check")

        # 1. No signing secret at all -> 503 (never falls back to predictable dev seed)
        with pytest.raises(HTTPException) as exc_missing:
            svc.mint_sandbox_object_token(
                upload_id=row["upload_id"],
                workspace_id=row["workspace_id"],
                object_key=row["object_key"],
                size_bytes=row["size_bytes"],
                sha256_hex=row["actual_sha256"],
            )
        assert exc_missing.value.status_code == 503

        # 2. Public/predictable/short secrets rejected -> 503
        for bad_secret in (
            "langalpha-local-dev-encryption-key",
            "dalton-local-dev-chat-upload-relay-seed:local-dev-user",
            "too-short",
        ):
            monkeypatch.setenv("DALTON_CHAT_UPLOAD_SIGNING_SECRET", bad_secret)
            with pytest.raises(HTTPException) as exc_bad:
                svc.mint_sandbox_object_token(
                    upload_id=row["upload_id"],
                    workspace_id=row["workspace_id"],
                    object_key=row["object_key"],
                    size_bytes=row["size_bytes"],
                    sha256_hex=row["actual_sha256"],
                )
            assert exc_bad.value.status_code == 503

        # 3. BYOK_ENCRYPTION_KEY alone is not accepted
        monkeypatch.delenv("DALTON_CHAT_UPLOAD_SIGNING_SECRET", raising=False)
        monkeypatch.setenv("BYOK_ENCRYPTION_KEY", "a" * 64)
        with pytest.raises(HTTPException) as exc_byok:
            svc.mint_sandbox_object_token(
                upload_id=row["upload_id"],
                workspace_id=row["workspace_id"],
                object_key=row["object_key"],
                size_bytes=row["size_bytes"],
                sha256_hex=row["actual_sha256"],
            )
        assert exc_byok.value.status_code == 503

    def test_rejects_expired_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_signing_env(monkeypatch)
        monkeypatch.setenv(
            "DALTON_CHAT_UPLOAD_SIGNING_SECRET", "0123456789abcdef0123456789abcdef"
        )
        payload = b"hello-core-sandbox"
        row = _make_ready_row(payload=payload)
        now = 1_700_000_000
        token, exp = svc.mint_sandbox_object_token(
            upload_id=row["upload_id"],
            workspace_id=row["workspace_id"],
            object_key=row["object_key"],
            size_bytes=row["size_bytes"],
            sha256_hex=row["actual_sha256"],
            ttl_s=60,
            now_ts=now,
        )
        assert exp == now + 60
        assert svc.verify_sandbox_object_token(token, now_ts=now + 59)["uid"] == row["upload_id"]

        with pytest.raises(HTTPException) as exc_info:
            svc.verify_sandbox_object_token(token, now_ts=now + 60)
        assert exc_info.value.status_code == 401
        assert "expired" in exc_info.value.detail.lower()

    def test_rejects_tampered_signature_and_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_signing_env(monkeypatch)
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "test-internal-secret-0123456789abcdef")
        payload = b"sensitive-financial-model"
        row = _make_ready_row(payload=payload)
        now = int(time.time())
        token, _ = svc.mint_sandbox_object_token(
            upload_id=row["upload_id"],
            workspace_id=row["workspace_id"],
            object_key=row["object_key"],
            size_bytes=row["size_bytes"],
            sha256_hex=row["actual_sha256"],
            ttl_s=120,
            now_ts=now,
        )
        payload_b64, sig_b64 = token.split(".")

        flipped_sig = ("A" if sig_b64[0] != "A" else "B") + sig_b64[1:]
        with pytest.raises(HTTPException) as sig_exc:
            svc.verify_sandbox_object_token(f"{payload_b64}.{flipped_sig}", now_ts=now)
        assert sig_exc.value.status_code == 401

        raw_claims = json.loads(
            base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)).decode("utf-8")
        )
        raw_claims["key"] = storage.build_chat_upload_key(
            row["workspace_id"], str(uuid.uuid4()), "other.pdf"
        )
        tampered_b64 = (
            base64.urlsafe_b64encode(
                json.dumps(raw_claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
            )
            .rstrip(b"=")
            .decode("ascii")
        )
        with pytest.raises(HTTPException) as payload_exc:
            svc.verify_sandbox_object_token(f"{tampered_b64}.{sig_b64}", now_ts=now)
        assert payload_exc.value.status_code == 401

        with pytest.raises(HTTPException) as bad_exc:
            svc.verify_sandbox_object_token("not-a-valid-token", now_ts=now)
        assert bad_exc.value.status_code == 401

    def test_rejects_non_ascii_and_non_b64url_token_segments_with_401(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_signing_env(monkeypatch)
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "test-internal-secret-0123456789abcdef")
        now = int(time.time())

        for malformed in (
            "中文载荷.validSig1234",
            "héllo_payload.validSig1234",
            "validPayload.中文签名",
            "bad+char.validSig1234",
            "validPayload.bad/char",
            "validPayload.a",
        ):
            with pytest.raises(HTTPException) as exc_info:
                svc.verify_sandbox_object_token(malformed, now_ts=now)
            assert exc_info.value.status_code == 401, f"Expected 401 for {malformed!r}"

    def test_rejects_cross_object_use_in_verifier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_signing_env(monkeypatch)
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "test-internal-secret-0123456789abcdef")
        row_a = _make_ready_row(payload=b"object-a")
        row_b = _make_ready_row(payload=b"object-b", workspace_id=row_a["workspace_id"])
        now = int(time.time())
        token_a, _ = svc.mint_sandbox_object_token(
            upload_id=row_a["upload_id"],
            workspace_id=row_a["workspace_id"],
            object_key=row_a["object_key"],
            size_bytes=row_a["size_bytes"],
            sha256_hex=row_a["actual_sha256"],
            ttl_s=120,
            now_ts=now,
        )

        with pytest.raises(HTTPException) as exc_uid:
            svc.verify_sandbox_object_token(
                token_a, expected_upload_id=row_b["upload_id"], now_ts=now
            )
        assert exc_uid.value.status_code == 403

        with pytest.raises(HTTPException) as exc_key:
            svc.verify_sandbox_object_token(
                token_a, expected_object_key=row_b["object_key"], now_ts=now
            )
        assert exc_key.value.status_code == 403

    def test_relay_route_registered_only_in_core_profile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.dalton.app as dalton_app_pkg

        try:
            monkeypatch.setenv("APP_ENV", "dalton")
            monkeypatch.delenv("DALTON_PROFILE", raising=False)
            importlib.reload(dalton_app_pkg)
            ef_paths = {getattr(r, "path", "") for r in dalton_app_pkg.dalton_router.routes}
            assert "/api/v1/dalton/chat-uploads/sandbox-object" not in ef_paths

            monkeypatch.setenv("APP_ENV", "core")
            importlib.reload(dalton_app_pkg)
            core_paths = {getattr(r, "path", "") for r in dalton_app_pkg.dalton_router.routes}
            assert "/api/v1/dalton/chat-uploads/sandbox-object" in core_paths
        finally:
            monkeypatch.delenv("APP_ENV", raising=False)
            monkeypatch.delenv("DALTON_PROFILE", raising=False)
            importlib.reload(dalton_app_pkg)

    @pytest.mark.asyncio
    async def test_endpoint_uses_header_token_blocks_everflow_and_rejects_bad_tokens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_signing_env(monkeypatch)
        monkeypatch.setenv("APP_ENV", "core")
        monkeypatch.delenv("DALTON_PROFILE", raising=False)
        monkeypatch.setenv(
            "DALTON_CHAT_UPLOAD_SIGNING_SECRET", "core-signing-secret-0123456789abcdef"
        )
        get_dalton_config.cache_clear()

        payload = b"chunk-1-" + b"x" * 1024 + b"-chunk-2"
        row_a = _make_ready_row(payload=payload, filename="model.xlsx")
        row_b = _make_ready_row(
            payload=b"different-file-bytes",
            workspace_id=row_a["workspace_id"],
            upload_id=row_a["upload_id"],
            filename="other.xlsx",
        )
        now = int(time.time())
        valid_token, _ = svc.mint_sandbox_object_token(
            upload_id=row_a["upload_id"],
            workspace_id=row_a["workspace_id"],
            object_key=row_a["object_key"],
            size_bytes=row_a["size_bytes"],
            sha256_hex=row_a["actual_sha256"],
            ttl_s=300,
            now_ts=now,
        )
        expired_token, _ = svc.mint_sandbox_object_token(
            upload_id=row_a["upload_id"],
            workspace_id=row_a["workspace_id"],
            object_key=row_a["object_key"],
            size_bytes=row_a["size_bytes"],
            sha256_hex=row_a["actual_sha256"],
            ttl_s=10,
            now_ts=now - 100,
        )

        mock_configured = create_autospec(storage.is_chat_upload_storage_configured, return_value=True)
        mock_get_upload = create_autospec(db_chat_uploads.get_upload, return_value=row_a)
        mock_retained = create_autospec(db_chat_uploads.is_upload_retained_for_recovery, return_value=True)
        mock_iter = create_autospec(
            storage.iter_stored_object_chunks,
            side_effect=lambda key, chunk_size=4 * 1024 * 1024: iter([payload[:256], payload[256:]]),
        )

        app = _make_test_app(include_relay=True)
        with (
            patch.object(storage, "is_chat_upload_storage_configured", mock_configured),
            patch.object(db_chat_uploads, "get_upload", mock_get_upload),
            patch.object(db_chat_uploads, "is_upload_retained_for_recovery", mock_retained),
            patch.object(storage, "iter_stored_object_chunks", mock_iter),
        ):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://backend:8000") as client:
                # 1. Valid download via X-Dalton-Relay-Token header succeeds
                resp = await client.get(
                    "/api/v1/dalton/chat-uploads/sandbox-object",
                    headers={svc.SANDBOX_OBJECT_TOKEN_HEADER: valid_token},
                )
                assert resp.status_code == 200
                assert resp.content == payload
                assert resp.headers["x-content-sha256"] == row_a["actual_sha256"]
                assert resp.headers["content-length"] == str(len(payload))
                mock_iter.assert_called_once_with(row_a["object_key"])

                # 2. Missing header -> 401
                resp_missing = await client.get("/api/v1/dalton/chat-uploads/sandbox-object")
                assert resp_missing.status_code == 401

                # 3. Expired token -> 401
                resp_exp = await client.get(
                    "/api/v1/dalton/chat-uploads/sandbox-object",
                    headers={svc.SANDBOX_OBJECT_TOKEN_HEADER: expired_token},
                )
                assert resp_exp.status_code == 401

                # 4. Tampered token -> 401
                p_b64, s_b64 = valid_token.split(".")
                bad_sig = ("Z" if s_b64[0] != "Z" else "Y") + s_b64[1:]
                resp_tampered = await client.get(
                    "/api/v1/dalton/chat-uploads/sandbox-object",
                    headers={svc.SANDBOX_OBJECT_TOKEN_HEADER: f"{p_b64}.{bad_sig}"},
                )
                assert resp_tampered.status_code == 401

                # 5. Cross-object DB mismatch -> 403
                mock_get_upload.return_value = row_b
                resp_cross = await client.get(
                    "/api/v1/dalton/chat-uploads/sandbox-object",
                    headers={svc.SANDBOX_OBJECT_TOKEN_HEADER: valid_token},
                )
                assert resp_cross.status_code == 403

                # 6. Non-core (Everflow) profile returns 404 even if router is mounted
                monkeypatch.setenv("APP_ENV", "dalton")
                resp_ef = await client.get(
                    "/api/v1/dalton/chat-uploads/sandbox-object",
                    headers={svc.SANDBOX_OBJECT_TOKEN_HEADER: valid_token},
                )
                assert resp_ef.status_code == 404

    @pytest.mark.asyncio
    async def test_integrity_check_completes_before_final_bytes_or_200(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_signing_env(monkeypatch)
        monkeypatch.setenv("APP_ENV", "core")
        monkeypatch.delenv("DALTON_PROFILE", raising=False)
        monkeypatch.setenv(
            "DALTON_CHAT_UPLOAD_SIGNING_SECRET", "core-signing-secret-0123456789abcdef"
        )
        get_dalton_config.cache_clear()

        payload = b"expected-bytes-12345"
        corrupted = b"corruptedbytes-12345"  # same length, wrong SHA-256
        row = _make_ready_row(payload=payload)
        token, _ = svc.mint_sandbox_object_token(
            upload_id=row["upload_id"],
            workspace_id=row["workspace_id"],
            object_key=row["object_key"],
            size_bytes=row["size_bytes"],
            sha256_hex=row["actual_sha256"],
        )

        mock_configured = create_autospec(storage.is_chat_upload_storage_configured, return_value=True)
        mock_get_upload = create_autospec(db_chat_uploads.get_upload, return_value=row)
        mock_retained = create_autospec(db_chat_uploads.is_upload_retained_for_recovery, return_value=True)

        # Case A: Single-chunk corrupted object fails with HTTP 502 BEFORE 200 headers are sent
        mock_iter_single = create_autospec(
            storage.iter_stored_object_chunks,
            side_effect=lambda key, chunk_size=4 * 1024 * 1024: iter([corrupted]),
        )
        with (
            patch.object(storage, "is_chat_upload_storage_configured", mock_configured),
            patch.object(db_chat_uploads, "get_upload", mock_get_upload),
            patch.object(db_chat_uploads, "is_upload_retained_for_recovery", mock_retained),
            patch.object(storage, "iter_stored_object_chunks", mock_iter_single),
        ):
            with pytest.raises(HTTPException) as exc_single:
                await svc.prepare_sandbox_object_download(token)
            assert exc_single.value.status_code == 502

        # Case B: Multi-chunk corrupted object aborts BEFORE yielding final chunk (< Content-Length)
        mock_iter_multi = create_autospec(
            storage.iter_stored_object_chunks,
            side_effect=lambda key, chunk_size=4 * 1024 * 1024: iter([corrupted[:10], corrupted[10:]]),
        )
        with (
            patch.object(storage, "is_chat_upload_storage_configured", mock_configured),
            patch.object(db_chat_uploads, "get_upload", mock_get_upload),
            patch.object(db_chat_uploads, "is_upload_retained_for_recovery", mock_retained),
            patch.object(storage, "iter_stored_object_chunks", mock_iter_multi),
        ):
            _, stream = await svc.prepare_sandbox_object_download(token)
            received = bytearray()
            with pytest.raises(RuntimeError, match="integrity mismatch"):
                async for chunk in stream:
                    received.extend(chunk)
            assert len(received) < len(payload)


class TestPlaceRowProfileRoutingAndTransferHeaders:
    """Core profile uses header-authenticated relay; Everflow keeps direct presigned MinIO URL."""

    @pytest.mark.asyncio
    async def test_core_profile_uses_header_relay_and_skips_minio_presign(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_signing_env(monkeypatch)
        monkeypatch.setenv("APP_ENV", "core")
        monkeypatch.delenv("DALTON_PROFILE", raising=False)
        monkeypatch.setenv("EGRESS_RELAY_BASE_URL", "http://backend:8000")
        monkeypatch.setenv(
            "DALTON_CHAT_UPLOAD_SIGNING_SECRET", "core-relay-secret-0123456789abcdef"
        )

        payload = b"core-profile-upload-bytes"
        row = _make_ready_row(payload=payload)
        sandbox = create_autospec(PTCSandbox, instance=True)
        layout = WorkspaceLayout(root="/home/workspace", dir_name="ws-a")
        rel_path = f"work/uploads/{row['safe_filename']}"
        abs_path = f"/home/workspace/ws-a/{rel_path}"

        captured_specs: list[dict[str, Any]] = []

        async def _fake_pull_direct(sb: Any, items: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
            captured_specs.extend(items)
            return {rel_path: {"status": "ok"}}

        mock_pull = create_autospec(svc.pull_direct, side_effect=_fake_pull_direct)
        mock_presign = create_autospec(storage.sign_download_url)

        with (
            patch.object(svc, "pull_direct", mock_pull),
            patch.object(storage, "sign_download_url", mock_presign),
        ):
            ok = await svc._place_row_in_sandbox(
                sandbox,
                row,
                layout=layout,
                abs_path=abs_path,
                rel_path=rel_path,
                can_direct=True,
                verify_retained=False,
            )

        assert ok is True
        mock_presign.assert_not_called()
        assert len(captured_specs) == 1
        spec = captured_specs[0]
        assert spec["sha256"] == row["actual_sha256"]
        assert spec["size"] == row["size_bytes"]
        assert spec["url"] == "http://backend:8000/api/v1/dalton/chat-uploads/sandbox-object"
        assert "host.docker.internal" not in spec["url"]
        token = spec["headers"][svc.SANDBOX_OBJECT_TOKEN_HEADER]
        assert token not in spec["url"]

        claims = svc.verify_sandbox_object_token(
            token,
            expected_upload_id=row["upload_id"],
            expected_object_key=row["object_key"],
        )
        assert claims["sha256"] == row["actual_sha256"]
        assert claims["size"] == row["size_bytes"]

    def test_wsfiles_pull_file_forwards_item_headers_to_download(self, tmp_path: Path) -> None:
        payload = b"pulled-via-header"
        digest = hashlib.sha256(payload).hexdigest()
        captured: dict[str, Any] = {}

        def _fake_download(
            url: str,
            parent: str,
            timeout_s: float,
            headers: dict[str, str] | None = None,
        ) -> tuple[str, str, int, int]:
            captured["url"] = url
            captured["headers"] = headers
            tmp_file = Path(parent) / ".wsfiles-test.tmp"
            tmp_file.write_bytes(payload)
            return str(tmp_file), digest, len(payload), 200

        mock_dl = create_autospec(transfer_rt._download_to_temp, side_effect=_fake_download)
        budget = transfer_rt._ByteBudget(1024 * 1024)
        item = {
            "kind": "file",
            "path": "work/uploads/test.bin",
            "size": len(payload),
            "sha256": digest,
            "mode": 0o644,
            "url": "http://backend:8000/api/v1/dalton/chat-uploads/sandbox-object",
            "headers": {svc.SANDBOX_OBJECT_TOKEN_HEADER: "tok.sig"},
        }
        (tmp_path / "work" / "uploads").mkdir(parents=True)
        with patch.object(transfer_rt, "_download_to_temp", mock_dl):
            res = transfer_rt._pull_file(str(tmp_path), item, 30.0, budget)
        assert res["status"] == "ok"
        assert captured["headers"] == {svc.SANDBOX_OBJECT_TOKEN_HEADER: "tok.sig"}
        assert (tmp_path / "work" / "uploads" / "test.bin").read_bytes() == payload

    @pytest.mark.asyncio
    @pytest.mark.parametrize("app_env", ["dalton", ""])
    async def test_everflow_profile_keeps_presigned_direct_minio_url(
        self, monkeypatch: pytest.MonkeyPatch, app_env: str
    ) -> None:
        monkeypatch.setenv("APP_ENV", app_env)
        monkeypatch.delenv("DALTON_PROFILE", raising=False)

        payload = b"everflow-profile-upload-bytes"
        row = _make_ready_row(payload=payload)
        sandbox = create_autospec(PTCSandbox, instance=True)
        layout = WorkspaceLayout(root="/home/workspace", dir_name="ws-ef")
        rel_path = f"work/uploads/{row['safe_filename']}"
        abs_path = f"/home/workspace/ws-ef/{rel_path}"

        presigned = "http://host.docker.internal:9000/dalton-asr/chat-uploads/presigned?X-Amz-Signature=abc"
        captured_specs: list[dict[str, Any]] = []

        async def _fake_pull_direct(sb: Any, items: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
            captured_specs.extend(items)
            return {rel_path: {"status": "ok"}}

        mock_pull = create_autospec(svc.pull_direct, side_effect=_fake_pull_direct)
        mock_presign = create_autospec(storage.sign_download_url, return_value=presigned)

        with (
            patch.object(svc, "pull_direct", mock_pull),
            patch.object(storage, "sign_download_url", mock_presign),
        ):
            ok = await svc._place_row_in_sandbox(
                sandbox,
                row,
                layout=layout,
                abs_path=abs_path,
                rel_path=rel_path,
                can_direct=True,
                verify_retained=False,
            )

        assert ok is True
        mock_presign.assert_called_once()
        assert len(captured_specs) == 1
        assert captured_specs[0]["url"] == presigned
        assert "headers" not in captured_specs[0]


class TestSandboxNetworkAndInitScript:
    """Verify per-instance sandbox network override, reachability, and init.sh 0600 permissions."""

    @pytest.mark.asyncio
    async def test_docker_provider_honors_sandbox_docker_network_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APP_ENV", "core")
        monkeypatch.delenv("DALTON_PROFILE", raising=False)
        raw = load_yaml_config(str(REPO_ROOT / "agent_config.yaml"), use_cache=False)
        cfg = load_from_dict(raw)
        assert cfg.sandbox.docker.network_mode == "sandbox-net"

        import src.config.env as env_mod

        monkeypatch.setattr(env_mod, "EGRESS_RELAY_BASE_URL", "http://backend:8000")
        assert egress_reachability.effective_relay_base_url("docker") == "http://backend:8000"

        # Verify DockerProvider passes SANDBOX_DOCKER_NETWORK into HostConfig["NetworkMode"]
        monkeypatch.setenv("SANDBOX_DOCKER_NETWORK", "custom-inst_sandbox")
        provider = DockerProvider(DockerConfig(network_mode="sandbox-net"))
        captured_cfg: dict[str, Any] = {}

        async def _fake_start_new(client: Any, container_config: dict[str, Any], name: str, sizing: Any) -> Any:
            captured_cfg.update(container_config)
            raise RuntimeError("stop-after-capture")

        from ptc_agent.core.sandbox.providers import docker as docker_provider_mod

        mock_get_client = create_autospec(provider._get_client, return_value=object())
        mock_ensure_image = create_autospec(docker_provider_mod.ensure_image, return_value=None)
        mock_start_new = create_autospec(provider._start_new, side_effect=_fake_start_new)
        with (
            patch.object(provider, "_get_client", mock_get_client),
            patch.object(docker_provider_mod, "ensure_image", mock_ensure_image),
            patch.object(provider, "_start_new", mock_start_new),
        ):
            with pytest.raises(RuntimeError, match="stop-after-capture"):
                await provider.create()
        assert captured_cfg["HostConfig"]["NetworkMode"] == "custom-inst_sandbox"
        assert "ExtraHosts" not in captured_cfg["HostConfig"]

    @pytest.mark.asyncio
    async def test_docker_provider_omits_host_gateway_only_when_core_and_sandbox_network_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ptc_agent.core.sandbox.providers import docker as docker_provider_mod

        async def _capture_host_config() -> dict[str, Any]:
            provider = DockerProvider(DockerConfig(network_mode="sandbox-net"))
            captured: dict[str, Any] = {}

            async def _fake_start_new(
                client: Any, container_config: dict[str, Any], name: str, sizing: Any
            ) -> Any:
                captured.update(container_config["HostConfig"])
                raise RuntimeError("stop-after-capture")

            mock_get_client = create_autospec(provider._get_client, return_value=object())
            mock_ensure_image = create_autospec(docker_provider_mod.ensure_image, return_value=None)
            mock_start_new = create_autospec(provider._start_new, side_effect=_fake_start_new)
            with (
                patch.object(provider, "_get_client", mock_get_client),
                patch.object(docker_provider_mod, "ensure_image", mock_ensure_image),
                patch.object(provider, "_start_new", mock_start_new),
            ):
                with pytest.raises(RuntimeError, match="stop-after-capture"):
                    await provider.create()
            return captured

        # Core profile + SANDBOX_DOCKER_NETWORK set -> no host.docker.internal alias
        monkeypatch.delenv("APP_ENV", raising=False)
        monkeypatch.setenv("DALTON_PROFILE", "core")
        monkeypatch.setenv("SANDBOX_DOCKER_NETWORK", "dalton-core_sandbox")
        core_with_net = await _capture_host_config()
        assert "ExtraHosts" not in core_with_net

        # Core profile WITHOUT SANDBOX_DOCKER_NETWORK -> host.docker.internal preserved
        monkeypatch.delenv("SANDBOX_DOCKER_NETWORK", raising=False)
        core_without_net = await _capture_host_config()
        assert core_without_net["ExtraHosts"] == ["host.docker.internal:host-gateway"]

    @pytest.mark.asyncio
    async def test_docker_provider_keeps_host_gateway_in_everflow_profile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ptc_agent.core.sandbox.providers import docker as docker_provider_mod

        async def _capture_host_config() -> dict[str, Any]:
            provider = DockerProvider(DockerConfig(network_mode="bridge"))
            captured: dict[str, Any] = {}

            async def _fake_start_new(
                client: Any, container_config: dict[str, Any], name: str, sizing: Any
            ) -> Any:
                captured.update(container_config["HostConfig"])
                raise RuntimeError("stop-after-capture")

            mock_get_client = create_autospec(provider._get_client, return_value=object())
            mock_ensure_image = create_autospec(docker_provider_mod.ensure_image, return_value=None)
            mock_start_new = create_autospec(provider._start_new, side_effect=_fake_start_new)
            with (
                patch.object(provider, "_get_client", mock_get_client),
                patch.object(docker_provider_mod, "ensure_image", mock_ensure_image),
                patch.object(provider, "_start_new", mock_start_new),
            ):
                with pytest.raises(RuntimeError, match="stop-after-capture"):
                    await provider.create()
            return captured

        monkeypatch.setenv("APP_ENV", "dalton")
        monkeypatch.delenv("DALTON_PROFILE", raising=False)
        monkeypatch.delenv("SANDBOX_DOCKER_NETWORK", raising=False)
        ef_default = await _capture_host_config()
        assert ef_default["ExtraHosts"] == ["host.docker.internal:host-gateway"]

        # Even if SANDBOX_DOCKER_NETWORK is set in Everflow, host.docker.internal stays intact
        monkeypatch.setenv("SANDBOX_DOCKER_NETWORK", "ef_sandbox")
        ef_with_net = await _capture_host_config()
        assert ef_with_net["ExtraHosts"] == ["host.docker.internal:host-gateway"]

    @staticmethod
    def _stage_init_install(tmp_path: Path, name: str = "my-core-inst") -> tuple[Path, Path]:
        install_dir = tmp_path / name
        script_dir = install_dir / "scripts" / "standalone"
        script_dir.mkdir(parents=True)
        init_copy = script_dir / "init.sh"
        init_copy.write_text(
            (REPO_ROOT / "scripts/standalone/init.sh").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        init_copy.chmod(0o755)
        (install_dir / ".env.core.example").write_text(
            (REPO_ROOT / ".env.core.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        return install_dir, init_copy

    @staticmethod
    def _make_fake_docker_env(tmp_path: Path, server_version: str | None) -> dict[str, str]:
        fake_bin = tmp_path / f"fake-bin-{uuid.uuid4().hex[:8]}"
        fake_bin.mkdir()
        docker_shim = fake_bin / "docker"
        if server_version is None:
            docker_shim.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        else:
            docker_shim.write_text(
                "#!/usr/bin/env bash\n"
                'if [[ "$1" == "version" ]]; then\n'
                f'  echo "{server_version}"\n'
                "  exit 0\n"
                "fi\n"
                'if [[ "$1" == "compose" && "$2" == "version" ]]; then\n'
                '  echo "2.30.0"\n'
                "  exit 0\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
        docker_shim.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
        env.pop("DALTON_AUTH_MODE", None)
        return env

    def test_init_script_enforces_0600_permissions_on_first_run_and_rerun(
        self, tmp_path: Path
    ) -> None:
        install_dir, init_script = self._stage_init_install(tmp_path)
        env_path = install_dir / ".env"
        env = self._make_fake_docker_env(tmp_path, "28.0.1")
        cmd = [
            "bash",
            str(init_script),
            "--non-interactive",
            "--domain",
            "localhost",
            "--skip-sandbox-build",
        ]

        # Run under permissive caller umask 022 to prove init.sh enforces 0600 itself
        def _set_permissive_umask() -> None:
            os.umask(0o022)

        res1 = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            preexec_fn=_set_permissive_umask,
        )
        assert "docker.sock" in res1.stdout
        assert "28.0" in res1.stdout
        assert env_path.is_file()
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600

        def _parse_env(path: Path) -> dict[str, str]:
            out: dict[str, str] = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
            return out

        first = _parse_env(env_path)
        assert first["APP_ENV"] == "core"
        assert first["DALTON_PROFILE"] == "core"
        assert first["DOMAIN"] == "localhost"
        assert first["COMPOSE_PROJECT_NAME"] == "my-core-inst"
        assert first["SANDBOX_DOCKER_NETWORK"] == "my-core-inst_sandbox"
        assert first["EGRESS_RELAY_BASE_URL"] == "http://backend:8000"
        assert first["DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL"] == "http://minio:9000"
        assert first["DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME"] == "chat-uploads"

        secret_keys = (
            "DB_PASSWORD",
            "REDIS_PASSWORD",
            "MINIO_ROOT_USER",
            "MINIO_ROOT_PASSWORD",
            "DALTON_CHAT_UPLOAD_SIGNING_SECRET",
            "INTERNAL_SERVICE_TOKEN",
            "BYOK_ENCRYPTION_KEY",
            "EGRESS_RELAY_SECRET",
            "JWT_SIGNING_SECRET",
            "BOOTSTRAP_SETUP_TOKEN",
        )
        for key in secret_keys:
            assert len(first[key]) == 64, f"{key} should be 64-hex chars from openssl rand -hex 32"
        assert first["DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID"] == first["MINIO_ROOT_USER"]
        assert first["DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY"] == first["MINIO_ROOT_PASSWORD"]

        # Re-run (rewriting DOMAIN to another loopback host to force write_env_key temp-file replace)
        subprocess.run(
            [
                "bash",
                str(init_script),
                "--non-interactive",
                "--domain",
                "127.0.0.1",
                "--skip-sandbox-build",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            preexec_fn=_set_permissive_umask,
        )
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
        second = _parse_env(env_path)
        assert second["DOMAIN"] == "127.0.0.1"
        for key in secret_keys:
            assert second[key] == first[key], f"{key} must not be overwritten on re-run"

    def test_init_script_docker_server_version_domain_guard_and_removed_env_file_flag(
        self, tmp_path: Path
    ) -> None:
        def _parse_env(path: Path) -> dict[str, str]:
            out: dict[str, str] = {}
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
            return out

        # 1. --env-file flag is removed
        _, init_removed = self._stage_init_install(tmp_path, "inst-removed-flag")
        res_flag = subprocess.run(
            ["bash", str(init_removed), "--env-file", str(tmp_path / "custom.env")],
            env=self._make_fake_docker_env(tmp_path, "28.0.1"),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert res_flag.returncode == 2
        assert "Unknown argument" in res_flag.stderr

        # 2. Public domain automatically configures local auth, strips all assignment variants,
        #    sets VITE_DALTON_AUTH=local, and leaves no DALTON_ALLOW_DEV_OSS_AUTH line.
        inst_domain, init_domain = self._stage_init_install(tmp_path, "inst-domain")
        (inst_domain / ".env").write_text(
            "  export DALTON_AUTH_MODE = oss\n"
            "DALTON_AUTH_MODE: oss\n"
            "DALTON_ALLOW_DEV_OSS_AUTH=1\n"
            "  export   DALTON_ALLOW_DEV_OSS_AUTH : 1\n"
            "  DOMAIN = old.example.com\n"
            "export DOMAIN: localhost\n"
            "  VITE_DALTON_AUTH = oss\n"
            "# Keep this comment DALTON_AUTH_MODE=oss\n",
            encoding="utf-8",
        )
        res_domain = subprocess.run(
            [
                "bash",
                str(init_domain),
                "--non-interactive",
                "--domain",
                "dalton.example.com",
                "--skip-sandbox-build",
            ],
            env=self._make_fake_docker_env(tmp_path, "28.0.1"),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res_domain.returncode == 0, res_domain.stderr
        domain_env_text = (inst_domain / ".env").read_text(encoding="utf-8")
        lines = domain_env_text.splitlines()
        auth_lines = [
            ln for ln in lines
            if not ln.lstrip().startswith("#") and "DALTON_AUTH_MODE" in ln
        ]
        dev_oss_lines = [
            ln for ln in lines
            if not ln.lstrip().startswith("#") and "DALTON_ALLOW_DEV_OSS_AUTH" in ln
        ]
        domain_lines = [
            ln for ln in lines
            if not ln.lstrip().startswith("#")
            and ln.lstrip().removeprefix("export").lstrip().startswith("DOMAIN")
            and ln.lstrip().removeprefix("export").lstrip()[6:7] in ("=", ":", " ", "\t")
        ]
        vite_auth_lines = [
            ln for ln in lines
            if not ln.lstrip().startswith("#") and "VITE_DALTON_AUTH" in ln
        ]
        assert auth_lines == ["DALTON_AUTH_MODE=local"]
        assert dev_oss_lines == []
        assert domain_lines == ["DOMAIN=dalton.example.com"]
        assert vite_auth_lines == ["VITE_DALTON_AUTH=local"]
        assert "# Keep this comment DALTON_AUTH_MODE=oss" in lines
        parsed_pub = _parse_env(inst_domain / ".env")
        assert parsed_pub["PUBLIC_APP_URL"] == "https://dalton.example.com"
        assert parsed_pub["SERVER_BASE_URL"] == "https://dalton.example.com"
        assert parsed_pub["DALTON_INVITE_EMAIL_DOMAIN"] == "team.dalton.example.com"

        # 3. ::1 and localhost keep loopback passwordless trial (DALTON_AUTH_MODE=oss, DALTON_ALLOW_DEV_OSS_AUTH=1)
        inst_v6, init_v6 = self._stage_init_install(tmp_path, "inst-v6")
        res_v6 = subprocess.run(
            [
                "bash",
                str(init_v6),
                "--non-interactive",
                "--domain",
                "::1",
                "--skip-sandbox-build",
            ],
            env=self._make_fake_docker_env(tmp_path, "28.0.1"),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res_v6.returncode == 0
        assert (inst_v6 / ".env").is_file()
        parsed_v6 = _parse_env(inst_v6 / ".env")
        assert parsed_v6["DALTON_AUTH_MODE"] == "oss"
        assert parsed_v6["DALTON_ALLOW_DEV_OSS_AUTH"] == "1"

        # 3b. validate_domain_for_auth refuses a public domain when SCRIPT_WROTE_LOCAL_AUTH != 1,
        #     even if .env already contains DALTON_AUTH_MODE=local.
        inst_guard, init_guard = self._stage_init_install(tmp_path, "inst-guard")
        guard_env_file = inst_guard / ".env"
        guard_env_file.write_text("DALTON_AUTH_MODE=local\nDOMAIN=dalton.example.com\n", encoding="utf-8")
        guard_script = (
            "set -euo pipefail\n"
            f'ENV_FILE="{guard_env_file}"\n'
            "SCRIPT_WROTE_LOCAL_AUTH=0\n"
            'err() { printf "ERR: %s\\n" "$1" >&2; }\n'
            f'eval "$(awk \'/^(get_env_value|count_env_key_assignments|is_loopback_domain|validate_domain_for_auth)\\(\\) \\{{/ {{p=1}} p {{print}} p && /^\\}}/ {{p=0}}\' "{init_guard}")"\n'
            'validate_domain_for_auth "dalton.example.com"\n'
        )
        res_guard_refused = subprocess.run(
            ["bash", "-c", guard_script],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert res_guard_refused.returncode != 0
        assert "DALTON_AUTH_MODE=local" in res_guard_refused.stderr

        # And passes ONLY when SCRIPT_WROTE_LOCAL_AUTH=1 and .env has unique local + no dev oss flag
        res_guard_ok = subprocess.run(
            ["bash", "-c", guard_script.replace("SCRIPT_WROTE_LOCAL_AUTH=0", "SCRIPT_WROTE_LOCAL_AUTH=1")],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert res_guard_ok.returncode == 0, res_guard_ok.stderr

        # 3c. Existing public DOMAIN in .env with no --domain is also normalized cleanly to local auth.
        inst_prev, init_prev = self._stage_init_install(tmp_path, "inst-env-domain")
        (inst_prev / ".env").write_text(
            "export DOMAIN = dalton.example.com\nDALTON_AUTH_MODE=oss\nDALTON_ALLOW_DEV_OSS_AUTH=1\n",
            encoding="utf-8",
        )
        res_prev = subprocess.run(
            ["bash", str(init_prev), "--non-interactive", "--skip-sandbox-build"],
            env=self._make_fake_docker_env(tmp_path, "28.0.1"),
            capture_output=True, text=True, timeout=30,
        )
        assert res_prev.returncode == 0, res_prev.stderr
        parsed_prev = _parse_env(inst_prev / ".env")
        assert parsed_prev["DOMAIN"] == "dalton.example.com"
        assert parsed_prev["DALTON_AUTH_MODE"] == "local"
        assert parsed_prev["VITE_DALTON_AUTH"] == "local"
        assert "DALTON_ALLOW_DEV_OSS_AUTH" not in parsed_prev

        # 4. Docker server < 28.0 or unreachable fails in --non-interactive
        for ver in ("27.5.1", None):
            _, init_old = self._stage_init_install(tmp_path, f"inst-old-{ver or 'none'}")
            res_old = subprocess.run(
                [
                    "bash",
                    str(init_old),
                    "--non-interactive",
                    "--domain",
                    "localhost",
                    "--skip-sandbox-build",
                ],
                env=self._make_fake_docker_env(tmp_path, ver),
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert res_old.returncode != 0

        # 5. Interactive mode with Docker server < 28.0 rejects non-'yes' and accepts 'yes'
        _, init_int_no = self._stage_init_install(tmp_path, "inst-int-no")
        res_int_no = subprocess.run(
            ["bash", str(init_int_no), "--domain", "localhost", "--skip-sandbox-build"],
            env=self._make_fake_docker_env(tmp_path, "27.5.1"),
            input="no\n",
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert res_int_no.returncode != 0

        inst_int_yes, init_int_yes = self._stage_init_install(tmp_path, "inst-int-yes")
        res_int_yes = subprocess.run(
            ["bash", str(init_int_yes), "--domain", "localhost", "--skip-sandbox-build"],
            env=self._make_fake_docker_env(tmp_path, "27.5.1"),
            input="yes\n",
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res_int_yes.returncode == 0
        assert (inst_int_yes / ".env").is_file()

    def test_configure_set_env_preserves_0600_under_permissive_umask(
        self, tmp_path: Path
    ) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text("DB_PASSWORD=topsecret\nEXISTING_KEY=old\n", encoding="utf-8")
        env_path.chmod(0o600)

        configure_sh = REPO_ROOT / "scripts/configure.sh"
        script = (
            "set -euo pipefail\n"
            f'ENV_FILE="{env_path}"\n'
            f'eval "$(awk \'/^set_env\\(\\) \\{{/ {{p=1}} p {{print}} p && /^\\}}/ {{exit}}\' "{configure_sh}")"\n'
            'set_env EXISTING_KEY "updated-value"\n'
            'set_env NEW_SECRET_KEY "new-secret"\n'
        )

        def _set_permissive_umask() -> None:
            os.umask(0o022)

        subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
            preexec_fn=_set_permissive_umask,
        )
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
        content = env_path.read_text(encoding="utf-8")
        assert "EXISTING_KEY=updated-value" in content
        assert "NEW_SECRET_KEY=new-secret" in content

    def test_configure_set_env_writes_through_a_relative_symlink(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "real.env"
        target.write_text("EXISTING_KEY=old\n", encoding="utf-8")
        target.chmod(0o640)
        link = tmp_path / ".env"
        link.symlink_to("real.env")

        configure_sh = REPO_ROOT / "scripts/configure.sh"
        script = (
            "set -euo pipefail\n"
            f'ENV_FILE="{link}"\n'
            f'eval "$(awk \'/^set_env\\(\\) \\{{/ {{p=1}} p {{print}} p && /^\\}}/ {{exit}}\' "{configure_sh}")"\n'
            'set_env EXISTING_KEY "updated"\n'
            'set_env NEW_KEY "added"\n'
        )
        subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
            preexec_fn=lambda: os.umask(0o022),
        )
        assert link.is_symlink()
        assert stat.S_IMODE(target.stat().st_mode) == 0o640
        content = target.read_text(encoding="utf-8")
        assert "EXISTING_KEY=updated" in content
        assert "NEW_KEY=added" in content

    def test_minio_init_fails_on_bucket_policy_error_except_no_such_policy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import boto3
        from botocore.exceptions import ClientError

        spec = importlib.util.spec_from_file_location(
            "minio_init_mod", REPO_ROOT / "scripts/standalone/minio_init.py"
        )
        assert spec and spec.loader
        minio_init = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(minio_init)

        monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL", "http://minio:9000")
        monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME", "chat-uploads")
        monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID", "minio-user")
        monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY", "minio-pass")

        class _S3ClientProto:
            def head_bucket(self, *, Bucket: str) -> dict[str, Any]: ...
            def create_bucket(self, **kwargs: Any) -> dict[str, Any]: ...
            def delete_bucket_policy(self, *, Bucket: str) -> dict[str, Any]: ...

        # 1. NoSuchBucketPolicy -> exit 0
        s3_no_policy = create_autospec(_S3ClientProto, instance=True)
        s3_no_policy.delete_bucket_policy.side_effect = ClientError(
            {"Error": {"Code": "NoSuchBucketPolicy", "Message": "none"}},
            "DeleteBucketPolicy",
        )
        mock_boto_ok = create_autospec(boto3.client, return_value=s3_no_policy)
        with patch.object(boto3, "client", mock_boto_ok):
            assert minio_init.main() == 0

        # 2. Other policy errors (AccessDenied, MethodNotAllowed, 404) -> exit non-zero
        for err_code in ("AccessDenied", "MethodNotAllowed", "NotImplemented", "404"):
            s3_fail = create_autospec(_S3ClientProto, instance=True)
            s3_fail.delete_bucket_policy.side_effect = ClientError(
                {"Error": {"Code": err_code, "Message": "failed"}},
                "DeleteBucketPolicy",
            )
            mock_boto_fail = create_autospec(boto3.client, return_value=s3_fail)
            with patch.object(boto3, "client", mock_boto_fail):
                assert minio_init.main() == 1, f"Expected non-zero exit for {err_code}"

    def test_edge_forwarded_request_extracts_real_client_ip_from_core_internal_subnet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Decision 2 + r3 #6: DALTON_TRUSTED_PROXIES derives from DALTON_CORE_INTERNAL_SUBNET so changing subnet keeps both aligned."""
        import re
        from types import SimpleNamespace
        import yaml

        # 1. Verify .env.core.example, docker-compose.core.yml, and init.sh use a single subnet source
        class _ComposeSafeLoader(yaml.SafeLoader):
            pass

        def _passthrough_tag(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
            if isinstance(node, yaml.MappingNode):
                return loader.construct_mapping(node)
            if isinstance(node, yaml.SequenceNode):
                return loader.construct_sequence(node)
            return loader.construct_scalar(node)

        _ComposeSafeLoader.add_constructor("!reset", _passthrough_tag)
        _ComposeSafeLoader.add_constructor("!override", _passthrough_tag)

        compose_raw = (REPO_ROOT / "docker-compose.core.yml").read_text(encoding="utf-8")
        compose_cfg = yaml.load(compose_raw, Loader=_ComposeSafeLoader)
        net_subnet_expr = compose_cfg["networks"]["core_internal"]["ipam"]["config"][0]["subnet"]
        backend_trusted_expr = compose_cfg["services"]["backend"]["environment"]["DALTON_TRUSTED_PROXIES"]
        assert net_subnet_expr == "${DALTON_CORE_INTERNAL_SUBNET:-172.31.250.0/24}"
        assert backend_trusted_expr == "${DALTON_CORE_INTERNAL_SUBNET:-172.31.250.0/24}"
        assert "DALTON_TRUSTED_PROXY_HOPS" in compose_cfg["services"]["backend"]["environment"]

        env_example_text = (REPO_ROOT / ".env.core.example").read_text(encoding="utf-8")
        assert "DALTON_CORE_INTERNAL_SUBNET=172.31.250.0/24" in env_example_text
        assert "DALTON_TRUSTED_PROXIES=" not in env_example_text
        assert "DALTON_TRUSTED_PROXY_HOPS=1" in env_example_text

        def _parse_env(path: Path) -> dict[str, str]:
            out: dict[str, str] = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
            return out

        def _resolve_compose_expr(expr: str, env_map: dict[str, str]) -> str:
            m = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]*)\}", expr)
            assert m, f"Unexpected compose expression: {expr!r}"
            var_name, default_val = m.group(1), m.group(2)
            return env_map.get(var_name) or default_val

        install_dir, init_script = self._stage_init_install(tmp_path, "inst-trusted-proxy")
        subprocess.run(
            [
                "bash",
                str(init_script),
                "--non-interactive",
                "--domain",
                "localhost",
                "--skip-sandbox-build",
            ],
            env=self._make_fake_docker_env(tmp_path, "28.0.1"),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        env_map = _parse_env(install_dir / ".env")
        assert env_map["DALTON_CORE_INTERNAL_SUBNET"] == "172.31.250.0/24"
        assert "DALTON_TRUSTED_PROXIES" not in env_map
        assert env_map["DALTON_TRUSTED_PROXY_HOPS"] == "1"

        resolved_net = _resolve_compose_expr(net_subnet_expr, env_map)
        resolved_trusted = _resolve_compose_expr(backend_trusted_expr, env_map)
        assert resolved_net == resolved_trusted == "172.31.250.0/24"

        # 2. Verify extract_client_ip extracts real client IP when forwarded from Caddy edge in core_internal
        monkeypatch.setenv("DALTON_TRUSTED_PROXIES", resolved_trusted)
        monkeypatch.setenv("DALTON_TRUSTED_PROXY_HOPS", env_map["DALTON_TRUSTED_PROXY_HOPS"])

        edge_req = SimpleNamespace(
            client=SimpleNamespace(host="172.31.250.10"),
            headers={"x-forwarded-for": "198.51.100.42"},
        )
        assert local_auth_sessions.extract_client_ip(edge_req) == "198.51.100.42"

        # Client-spoofed leftmost IP is ignored; rightmost hop added by Caddy wins
        spoofed_req = SimpleNamespace(
            client=SimpleNamespace(host="172.31.250.10"),
            headers={"x-forwarded-for": "203.0.113.1, 198.51.100.42"},
        )
        assert local_auth_sessions.extract_client_ip(spoofed_req) == "198.51.100.42"

        # Untrusted peer outside core_internal cannot spoof X-Forwarded-For
        untrusted_req = SimpleNamespace(
            client=SimpleNamespace(host="10.99.0.5"),
            headers={"x-forwarded-for": "198.51.100.42"},
        )
        assert local_auth_sessions.extract_client_ip(untrusted_req) == "10.99.0.5"

        # 3. Override DALTON_CORE_INTERNAL_SUBNET and re-run init.sh: both Compose sites stay identical
        custom_subnet = "10.200.50.0/24"
        env_path = install_dir / ".env"
        env_path.write_text(
            env_path.read_text(encoding="utf-8").replace(
                "DALTON_CORE_INTERNAL_SUBNET=172.31.250.0/24",
                f"DALTON_CORE_INTERNAL_SUBNET={custom_subnet}",
            ),
            encoding="utf-8",
        )
        subprocess.run(
            ["bash", str(init_script), "--non-interactive", "--skip-sandbox-build"],
            env=self._make_fake_docker_env(tmp_path, "28.0.1"),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        env_map_2 = _parse_env(env_path)
        assert env_map_2["DALTON_CORE_INTERNAL_SUBNET"] == custom_subnet
        assert "DALTON_TRUSTED_PROXIES" not in env_map_2
        resolved_net_2 = _resolve_compose_expr(net_subnet_expr, env_map_2)
        resolved_trusted_2 = _resolve_compose_expr(backend_trusted_expr, env_map_2)
        assert resolved_net_2 == resolved_trusted_2 == custom_subnet

        monkeypatch.setenv("DALTON_TRUSTED_PROXIES", resolved_trusted_2)
        new_edge_req = SimpleNamespace(
            client=SimpleNamespace(host="10.200.50.10"),
            headers={"x-forwarded-for": "198.51.100.99"},
        )
        assert local_auth_sessions.extract_client_ip(new_edge_req) == "198.51.100.99"
        old_subnet_req = SimpleNamespace(
            client=SimpleNamespace(host="172.31.250.10"),
            headers={"x-forwarded-for": "198.51.100.99"},
        )
        assert local_auth_sessions.extract_client_ip(old_subnet_req) == "172.31.250.10"

    def test_init_b6r2_domain_port_tls_mode_validation_and_mode_preservation(
        self, tmp_path: Path
    ) -> None:
        env = self._make_fake_docker_env(tmp_path, "28.0.1")

        # 1. Domain with port or illegal chars is rejected (Decision 8)
        for bad_domain in (
            "https://localhost:8443/",
            "localhost:8088",
            "[::1]:8443",
            "bad host!",
            "https://dalton.example.com:8443/path",
        ):
            _, init_bad = self._stage_init_install(tmp_path, f"inst-bad-{uuid.uuid4().hex[:6]}")
            res_bad = subprocess.run(
                [
                    "bash",
                    str(init_bad),
                    "--non-interactive",
                    "--domain",
                    bad_domain,
                    "--skip-sandbox-build",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert res_bad.returncode == 2, f"Expected exit 2 for bad domain {bad_domain!r}: {res_bad.stderr}"

        # 2. Loopback + --tls-mode auto stays bound to 127.0.0.1 (Decision 1)
        inst_auto, init_auto = self._stage_init_install(tmp_path, "inst-loopback-auto")
        res_auto = subprocess.run(
            [
                "bash",
                str(init_auto),
                "--non-interactive",
                "--domain",
                "localhost",
                "--tls-mode",
                "auto",
                "--skip-sandbox-build",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert res_auto.returncode == 0
        auto_env = (inst_auto / ".env").read_text(encoding="utf-8")
        assert "DALTON_EDGE_TLS_BIND=127.0.0.1\n" in auto_env
        assert "COMPOSE_FILE=docker-compose.yml:docker-compose.core.yml:deploy/standalone/docker-compose.tls.yml\n" in auto_env

        # 3. Existing .env with invalid DALTON_TLS_MODE is rejected on rerun (Decision 1)
        inst_invalid_mode, init_invalid_mode = self._stage_init_install(tmp_path, "inst-invalid-tls")
        (inst_invalid_mode / ".env").write_text(
            "DOMAIN=localhost\nDALTON_TLS_MODE=bogus\n", encoding="utf-8"
        )
        (inst_invalid_mode / ".env").chmod(0o600)
        res_inv = subprocess.run(
            ["bash", str(init_invalid_mode), "--non-interactive", "--skip-sandbox-build"],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert res_inv.returncode == 2
        assert "bogus" in res_inv.stderr

        # 4. Existing .env permissions (e.g. 0640) are preserved when init.sh updates it
        inst_perm, init_perm = self._stage_init_install(tmp_path, "inst-perm-keep")
        env_perm = inst_perm / ".env"
        env_perm.write_text("DOMAIN=localhost\nDALTON_TLS_MODE=loopback\n", encoding="utf-8")
        env_perm.chmod(0o640)
        subprocess.run(
            [
                "bash",
                str(init_perm),
                "--non-interactive",
                "--domain",
                "127.0.0.1",
                "--skip-sandbox-build",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert stat.S_IMODE(env_perm.stat().st_mode) == 0o640

        # 5. Pre-existing non-loopback bind address is ALWAYS rewritten to 127.0.0.1 even with no flags
        for variant_idx, bind_line in enumerate(
            (
                "DALTON_EDGE_TLS_BIND=0.0.0.0",
                "  export DALTON_EDGE_TLS_BIND = 0.0.0.0",
                "DALTON_EDGE_TLS_BIND: 0.0.0.0",
            )
        ):
            inst_bind, init_bind = self._stage_init_install(tmp_path, f"inst-rewrite-bind-{variant_idx}")
            env_bind = inst_bind / ".env"
            env_bind.write_text(
                f"DOMAIN=localhost\nDALTON_TLS_MODE=auto\n{bind_line}\n",
                encoding="utf-8",
            )
            env_bind.chmod(0o600)
            res_bind = subprocess.run(
                ["bash", str(init_bind), "--non-interactive", "--skip-sandbox-build"],
                env=env,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            env_bind_text = env_bind.read_text(encoding="utf-8")
            assert "DALTON_EDGE_TLS_BIND=127.0.0.1\n" in env_bind_text
            assert "0.0.0.0" not in env_bind_text
            assert "Rewrote DALTON_EDGE_TLS_BIND" in res_bind.stderr

        # 6. IPv6 loopback (--domain ::1 --tls-mode internal) selects docker-compose.tls.ipv6.yml and passes [::1] to Caddy
        inst_v6_tls, init_v6_tls = self._stage_init_install(tmp_path, "inst-v6-internal")
        (inst_v6_tls / ".env").write_text("DALTON_TRUSTED_PROXIES=10.0.0.0/8\n", encoding="utf-8")
        (inst_v6_tls / ".env").chmod(0o600)
        subprocess.run(
            [
                "bash",
                str(init_v6_tls),
                "--non-interactive",
                "--domain",
                "::1",
                "--tls-mode",
                "internal",
                "--skip-sandbox-build",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        v6_env_text = (inst_v6_tls / ".env").read_text(encoding="utf-8")
        assert "DALTON_EDGE_TLS_BIND=[::1]\n" in v6_env_text
        assert "DALTON_CADDY_HOST=[::1]\n" in v6_env_text
        assert "COMPOSE_FILE=docker-compose.yml:docker-compose.core.yml:deploy/standalone/docker-compose.tls.ipv6.yml\n" in v6_env_text
        assert "PUBLIC_APP_URL=https://[::1]:18443\n" in v6_env_text
        assert "DALTON_TRUSTED_PROXIES=" not in v6_env_text

        # 7. Hardcoded loopback binds (no bind env interpolation before Batch 4b) & IPv6 redirect preserves brackets
        auto_caddy = (REPO_ROOT / "deploy/standalone/modes/auto.caddy").read_text(encoding="utf-8")
        assert "import dalton_http_redirect" in auto_caddy
        caddyfile = (REPO_ROOT / "deploy/standalone/Caddyfile").read_text(encoding="utf-8")
        assert "vars_regexp ipv6_host {http.request.host}" in caddyfile
        assert "https://[{re.ipv6_host.1}]" in caddyfile
        tls_compose = (REPO_ROOT / "deploy/standalone/docker-compose.tls.yml").read_text(encoding="utf-8")
        assert '"127.0.0.1:${DALTON_EDGE_HTTP_PORT:-18080}:80"' in tls_compose
        assert '"127.0.0.1:${DALTON_EDGE_HTTPS_PORT:-18443}:443"' in tls_compose
        assert "DALTON_EDGE_TLS_BIND" not in tls_compose
        tls_ipv6_compose = (REPO_ROOT / "deploy/standalone/docker-compose.tls.ipv6.yml").read_text(encoding="utf-8")
        assert '"[::1]:${DALTON_EDGE_HTTP_PORT:-18080}:80"' in tls_ipv6_compose
        assert '"[::1]:${DALTON_EDGE_HTTPS_PORT:-18443}:443"' in tls_ipv6_compose
        assert "DALTON_EDGE_TLS_BIND" not in tls_ipv6_compose

        # 8. Standalone images are pinned by digest
        edge_df = (REPO_ROOT / "deploy/standalone/Dockerfile.edge").read_text(encoding="utf-8")
        for line in edge_df.splitlines():
            if line.strip().startswith("FROM "):
                assert "@sha256:" in line, f"Dockerfile.edge base image not pinned by digest: {line}"
        core_compose = (REPO_ROOT / "docker-compose.core.yml").read_text(encoding="utf-8")
        assert "valkey/valkey:8-alpine@sha256:" in core_compose
        assert "cgr.dev/chainguard/minio@sha256:" in core_compose
        env_example = (REPO_ROOT / ".env.core.example").read_text(encoding="utf-8")
        assert "public.ecr.aws/chainguard/minio:latest" not in env_example

    def test_ops_scripts_external_db_guard_retain_validation_and_replace_flags(
        self, tmp_path: Path
    ) -> None:
        """Offline checks for Decision 4 (external DB guard), Decision 5 (split flags), Decision 10 (--retain)."""
        work = tmp_path / "ops-work"
        work.mkdir()
        (work / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (work / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
        (work / ".env").write_text("DB_HOST=external-pg.corp.internal\n", encoding="utf-8")
        backup_stub = tmp_path / "bak-stub"
        backup_stub.mkdir()
        (backup_stub / "postgres.dump").write_bytes(b"PGDMP")

        # External DB refused by backup.sh, restore.sh, and upgrade.sh
        res_bak = subprocess.run(
            ["bash", str(REPO_ROOT / "scripts/standalone/backup.sh"), "--work-dir", str(work)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert res_bak.returncode != 0
        assert "pg_dump" in res_bak.stderr

        res_res = subprocess.run(
            [
                "bash",
                str(REPO_ROOT / "scripts/standalone/restore.sh"),
                "--work-dir",
                str(work),
                "--backup-dir",
                str(backup_stub),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert res_res.returncode != 0
        assert "pg_restore" in res_res.stderr

        rel_dir = tmp_path / "rel-stub"
        rel_dir.mkdir()
        res_upg = subprocess.run(
            [
                "bash",
                str(REPO_ROOT / "scripts/standalone/upgrade.sh"),
                "--work-dir",
                str(work),
                "--release-dir",
                str(rel_dir),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert res_upg.returncode != 0
        assert "pg_dump" in res_upg.stderr

        # backup.sh accepts leading-zero decimal --retain 08 (fails later at external DB, not octal error)
        res_oct = subprocess.run(
            [
                "bash",
                str(REPO_ROOT / "scripts/standalone/backup.sh"),
                "--work-dir",
                str(work),
                "--retain",
                "08",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert "octal" not in res_oct.stderr.lower()
        assert "pg_dump" in res_oct.stderr

        # backup.sh rejects --retain above upper bound
        res_cap = subprocess.run(
            [
                "bash",
                str(REPO_ROOT / "scripts/standalone/backup.sh"),
                "--work-dir",
                str(work),
                "--retain",
                "99999",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert res_cap.returncode == 2

        # restore.sh rejects removed --force and --replace-data (v1 is empty-target-only)
        res_force = subprocess.run(
            [
                "bash",
                str(REPO_ROOT / "scripts/standalone/restore.sh"),
                "--backup-dir",
                str(backup_stub),
                "--force",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert res_force.returncode == 2
        assert "--replace-data" not in res_force.stderr
        assert "--replace-env" in res_force.stderr
        assert "STANDALONE-DEPLOY.md" in res_force.stderr

        res_replace_data = subprocess.run(
            [
                "bash",
                str(REPO_ROOT / "scripts/standalone/restore.sh"),
                "--backup-dir",
                str(backup_stub),
                "--replace-data",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert res_replace_data.returncode == 2

        # backup.sh rejects multiple distinct object storage services (v1 single object storage only)
        multi_s3_work = tmp_path / "ops-multi-storage"
        multi_s3_work.mkdir()
        (multi_s3_work / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (multi_s3_work / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
        (multi_s3_work / ".env").write_text(
            "DB_HOST=postgres\n"
            "DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL=http://minio-chat:9000\n"
            "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME=shared-bucket\n"
            "DALTON_ASR_STORAGE_ENDPOINT_URL=http://minio-asr:9000\n"
            "DALTON_ASR_STORAGE_BUCKET_NAME=shared-bucket\n",
            encoding="utf-8",
        )
        res_multi_s3 = subprocess.run(
            ["bash", str(REPO_ROOT / "scripts/standalone/backup.sh"), "--work-dir", str(multi_s3_work)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert res_multi_s3.returncode != 0
        assert "Multiple object storage services" in res_multi_s3.stderr
        assert "v1 only supports a single object storage service" in res_multi_s3.stderr

        # Compose .env syntax variants (export, leading space, KEY =, KEY:) are recognized for DB_HOST
        for variant_idx, db_host_line in enumerate(
            (
                "export DB_HOST=external-pg.corp.internal",
                "   DB_HOST = external-pg.corp.internal # prod",
                "DB_HOST: external-pg.corp.internal",
            )
        ):
            vwork = tmp_path / f"ops-env-var-{variant_idx}"
            vwork.mkdir()
            (vwork / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
            (vwork / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
            (vwork / ".env").write_text(f"{db_host_line}\n", encoding="utf-8")
            for script_name, extra in (
                ("backup.sh", []),
                ("restore.sh", ["--backup-dir", str(backup_stub)]),
                ("upgrade.sh", ["--release-dir", str(rel_dir)]),
            ):
                vres = subprocess.run(
                    ["bash", str(REPO_ROOT / "scripts/standalone" / script_name), "--work-dir", str(vwork), *extra],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                assert vres.returncode != 0, f"{script_name} missed external DB in {db_host_line!r}"
                assert "External PostgreSQL detected" in vres.stderr, (
                    f"{script_name} unexpected stderr for {db_host_line!r}: {vres.stderr}"
                )

        # --retain rejects 64-bit integer wraparound overflow (18446744073709551617 -> 1) before arithmetic
        for script_name, extra in (
            ("backup.sh", ["--work-dir", str(work)]),
            ("upgrade.sh", ["--work-dir", str(work), "--release-dir", str(rel_dir)]),
        ):
            res_ovf = subprocess.run(
                [
                    "bash",
                    str(REPO_ROOT / "scripts/standalone" / script_name),
                    *extra,
                    "--retain",
                    "18446744073709551617",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert res_ovf.returncode == 2, f"{script_name} did not reject overflow --retain: {res_ovf.stderr}"

        # restore.sh --replace-env stages env.enc and leaves existing .env untouched on external DB or project mismatch
        pass_file = tmp_path / "restore.pass"
        pass_file.write_text("test-passphrase-0123456789\n", encoding="utf-8")
        pass_file.chmod(0o600)

        def _make_enc_backup(name: str, env_content: str, manifest_proj: str) -> Path:
            bdir = tmp_path / name
            bdir.mkdir()
            (bdir / "postgres.dump").write_bytes(b"PGDMP")
            plain = tmp_path / f"{name}.plain.env"
            plain.write_text(env_content, encoding="utf-8")
            subprocess.run(
                [
                    "openssl",
                    "enc",
                    "-aes-256-cbc",
                    "-pbkdf2",
                    "-md",
                    "sha256",
                    "-iter",
                    "10000",
                    "-salt",
                    "-in",
                    str(plain),
                    "-out",
                    str(bdir / "env.enc"),
                    "-pass",
                    f"file:{pass_file}",
                ],
                check=True,
                capture_output=True,
                timeout=15,
            )
            (bdir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "compose_project": manifest_proj,
                        "env_backup": "encrypted",
                        "env_encryption": {
                            "cipher": "aes-256-cbc",
                            "kdf": "pbkdf2",
                            "digest": "sha256",
                            "iterations": 10000,
                        },
                    }
                ),
                encoding="utf-8",
            )
            return bdir

        rwork = tmp_path / "restore-stage-work"
        rwork.mkdir()
        (rwork / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (rwork / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
        orig_env_text = "COMPOSE_PROJECT_NAME=proj-current\nDB_HOST=postgres\nSENTINEL=keep-me\n"
        (rwork / ".env").write_text(orig_env_text, encoding="utf-8")

        bak_mismatch = _make_enc_backup(
            "bak-proj-mismatch",
            "COMPOSE_PROJECT_NAME=proj-other\nDB_HOST=postgres\nSENTINEL=clobbered\n",
            "proj-other",
        )
        res_mismatch = subprocess.run(
            [
                "bash",
                str(REPO_ROOT / "scripts/standalone/restore.sh"),
                "--work-dir",
                str(rwork),
                "--backup-dir",
                str(bak_mismatch),
                "--env-passphrase-file",
                str(pass_file),
                "--replace-env",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert res_mismatch.returncode != 0
        assert "COMPOSE_PROJECT_NAME" in res_mismatch.stderr
        assert (rwork / ".env").read_text(encoding="utf-8") == orig_env_text

        bak_ext_db = _make_enc_backup(
            "bak-ext-db",
            "COMPOSE_PROJECT_NAME=proj-current\nDB_HOST: external-rds.corp.internal\nSENTINEL=clobbered\n",
            "proj-current",
        )
        res_ext_env = subprocess.run(
            [
                "bash",
                str(REPO_ROOT / "scripts/standalone/restore.sh"),
                "--work-dir",
                str(rwork),
                "--backup-dir",
                str(bak_ext_db),
                "--env-passphrase-file",
                str(pass_file),
                "--replace-env",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert res_ext_env.returncode != 0
        assert "External PostgreSQL" in res_ext_env.stderr
        assert (rwork / ".env").read_text(encoding="utf-8") == orig_env_text

        # Verbatim COMPOSE_PROJECT_NAME check rejects `dalton-` vs `dalton_` (no normalization)
        rwork_dash = tmp_path / "restore-verbatim-proj"
        rwork_dash.mkdir()
        (rwork_dash / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (rwork_dash / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
        dash_env_text = "COMPOSE_PROJECT_NAME=dalton-\nDB_HOST=postgres\nSENTINEL=keep-dash\n"
        (rwork_dash / ".env").write_text(dash_env_text, encoding="utf-8")
        bak_underscore = _make_enc_backup(
            "bak-proj-underscore",
            "COMPOSE_PROJECT_NAME=dalton_\nDB_HOST=postgres\nSENTINEL=clobbered\n",
            "dalton_",
        )
        res_verbatim = subprocess.run(
            [
                "bash",
                str(REPO_ROOT / "scripts/standalone/restore.sh"),
                "--work-dir",
                str(rwork_dash),
                "--backup-dir",
                str(bak_underscore),
                "--env-passphrase-file",
                str(pass_file),
                "--replace-env",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert res_verbatim.returncode != 0
        assert "COMPOSE_PROJECT_NAME mismatch" in res_verbatim.stderr
        assert (rwork_dash / ".env").read_text(encoding="utf-8") == dash_env_text

        # Stale .env.tmp-* left by a killed run is cleaned up after lock acquisition
        stale_tmp_work = tmp_path / "restore-stale-tmp"
        stale_tmp_work.mkdir()
        (stale_tmp_work / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (stale_tmp_work / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
        stale_proj = f"stale-tmp-{os.getpid()}"
        (stale_tmp_work / ".env").write_text(
            f"COMPOSE_PROJECT_NAME={stale_proj}\nDB_HOST=external-pg.corp.internal\n",
            encoding="utf-8",
        )
        leftover_tmp = stale_tmp_work / ".env.tmp-deadbeef"
        leftover_tmp.write_text("LEFTOVER=1\n", encoding="utf-8")
        stale_lock_path = Path(f"/tmp/dalton-{stale_proj}.restore.lock")
        try:
            res_stale_tmp = subprocess.run(
                [
                    "bash",
                    str(REPO_ROOT / "scripts/standalone/restore.sh"),
                    "--work-dir",
                    str(stale_tmp_work),
                    "--backup-dir",
                    str(backup_stub),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert res_stale_tmp.returncode != 0
            assert "External PostgreSQL detected" in res_stale_tmp.stderr
            assert not leftover_tmp.exists(), "restore.sh must clean up leftover .env.tmp-* files"
        finally:
            if stale_lock_path.is_dir():
                stale_lock_path.rmdir()
            elif stale_lock_path.exists():
                stale_lock_path.unlink()

        # backup.sh PID lock automatically cleans up a stale lock left by a dead PID
        lock_parent = tmp_path / "lock-test"
        lock_parent.mkdir()
        stale_lock = lock_parent / ".dalton-backup.lock"
        stale_lock.mkdir()
        (stale_lock / "pid").write_text("999999\n", encoding="utf-8")
        backup_sh = REPO_ROOT / "scripts/standalone/backup.sh"
        lock_driver = (
            "set -euo pipefail\n"
            'warn() { printf "[backup] WARN: %s\\n" "$1" >&2; }\n'
            'err()  { printf "[backup] ERROR: %s\\n" "$1" >&2; }\n'
            f'LOCK_DIR="{stale_lock}"\n'
            "LOCK_HELD=0\n"
            f'eval "$(awk \'/^pid_is_alive\\(\\) \\{{/ {{p=1}} p {{print}} p && /^release_publish_lock\\(\\) \\{{/ {{r=1}} r && /^\\}}/ {{exit}}\' "{backup_sh}")"\n'
            "acquire_publish_lock\n"
            'test "$(cat "$LOCK_DIR/pid")" = "$$"\n'
            "release_publish_lock\n"
            'test ! -d "$LOCK_DIR"\n'
        )
        res_lock = subprocess.run(
            ["bash", "-c", lock_driver],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert res_lock.returncode == 0, f"Stale PID lock cleanup failed: {res_lock.stderr}"
        assert "Removing stale backup lock" in res_lock.stderr

        # upgrade.sh falls back to new release's backup.sh when old WORK_DIR has none (#480 first upgrade)
        # AND pre-migration failure tells operator NOT to wipe volumes or restore data
        old_work = tmp_path / "old-480-work"
        old_work.mkdir()
        (old_work / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (old_work / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
        (old_work / ".env").write_text("DB_HOST=postgres\n", encoding="utf-8")
        new_rel = tmp_path / "new-release"
        (new_rel / "scripts" / "standalone").mkdir(parents=True)
        (new_rel / "deploy" / "standalone").mkdir(parents=True)
        for req in (
            "docker-compose.yml",
            "docker-compose.core.yml",
            "scripts/standalone/migrate.sh",
            "scripts/standalone/restore.sh",
            "deploy/standalone/Caddyfile",
        ):
            (new_rel / req).write_text("stub\n", encoding="utf-8")
        (new_rel / "scripts" / "standalone" / "backup.sh").write_text(
            "#!/usr/bin/env bash\necho 'NEW_RELEASE_BACKUP_INVOKED' >&2\nexit 1\n",
            encoding="utf-8",
        )
        res_first_upg = subprocess.run(
            [
                "bash",
                str(REPO_ROOT / "scripts/standalone/upgrade.sh"),
                "--work-dir",
                str(old_work),
                "--release-dir",
                str(new_rel),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert res_first_upg.returncode != 0
        assert "NEW_RELEASE_BACKUP_INVOKED" in res_first_upg.stderr
        assert "Do NOT wipe volumes or restore data" in res_first_upg.stderr
        assert "--replace-data" not in res_first_upg.stderr

        # restore.sh v1 empty-target-only invariants, fixed /tmp lock, env -u isolation,
        # no in-place replacement logic, and embedded Python compilation.
        import re as _re
        from botocore.exceptions import ClientError

        restore_text = (REPO_ROOT / "scripts/standalone/restore.sh").read_text(encoding="utf-8")
        assert "read_env_var()" not in restore_text, "restore.sh must read .env files strictly via read_env_file_var"
        assert 'if [[ "$NEW_PROJECT" != "$CUR_PROJECT" ]]' in restore_text, "restore.sh must compare COMPOSE_PROJECT_NAME verbatim for --replace-env"
        assert "sanitize_project_name()" in restore_text, "restore.sh must share sanitize_project_name for directory fallback"
        assert "--replace-data" not in restore_text, "restore.sh must remove --replace-data"
        assert "ALTER DATABASE" not in restore_text, "restore.sh must remove DB rename / swap logic"
        assert "_pre_restore_" not in restore_text, "restore.sh must remove preserved DB logic"
        assert "${ENV_FILE}.pre-restore-" in restore_text, "restore.sh must back up existing .env before swapping from env.enc"
        assert "write_compose_env_override" not in restore_text, "restore.sh must not generate temporary compose override files"
        assert "RESTORE_COMPOSE_OVERRIDE" not in restore_text, "restore.sh must remove RESTORE_COMPOSE_OVERRIDE"
        assert "ACTIVE_COMPOSE_ENV" not in restore_text, "restore.sh must remove ACTIVE_COMPOSE_ENV"
        assert "derive_restore_bucket_name" not in restore_text, "restore.sh must restore into original bucket names"
        assert "pid_is_alive" not in restore_text, "restore.sh must not auto-clean locks by PID"
        assert "/tmp/dalton-${LOCK_PROJECT}.restore.lock" in restore_text
        assert "XDG_RUNTIME_DIR" not in restore_text, "restore lock path must be fixed under /tmp"
        assert 'env "${ENV_UNSET_ARGS[@]}" docker compose' in restore_text

        # Fixed /tmp/dalton-<project>.restore.lock ignores XDG_RUNTIME_DIR and NEVER auto-removes stale locks
        xdg_dir = tmp_path / "xdg-runtime"
        xdg_dir.mkdir()
        shared_proj = f"shared-proj-lock-{os.getpid()}"
        fixed_lock = Path(f"/tmp/dalton-{shared_proj}.restore.lock")
        lock_work_b = tmp_path / "work-dir-b"
        lock_work_b.mkdir()
        (lock_work_b / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (lock_work_b / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
        (lock_work_b / ".env").write_text(
            f"COMPOSE_PROJECT_NAME={shared_proj}\nDB_HOST=external-pg.corp.internal\n",
            encoding="utf-8",
        )
        lock_env = os.environ.copy()
        lock_env["XDG_RUNTIME_DIR"] = str(xdg_dir)
        try:
            # Pre-create lock directory: restore.sh must refuse and instruct manual removal (never delete it)
            if fixed_lock.is_dir():
                fixed_lock.rmdir()
            elif fixed_lock.exists():
                fixed_lock.unlink()
            fixed_lock.mkdir()
            res_proj_lock = subprocess.run(
                [
                    "bash",
                    str(REPO_ROOT / "scripts/standalone/restore.sh"),
                    "--work-dir",
                    str(lock_work_b),
                    "--backup-dir",
                    str(backup_stub),
                ],
                env=lock_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert res_proj_lock.returncode != 0
            assert str(fixed_lock) in res_proj_lock.stderr
            assert "manually" in res_proj_lock.stderr.lower()
            assert fixed_lock.is_dir(), "restore.sh must NOT auto-remove an existing lock"
            assert not list(xdg_dir.iterdir()), "restore.sh must ignore XDG_RUNTIME_DIR for lock path"
        finally:
            if fixed_lock.is_dir():
                fixed_lock.rmdir()
            elif fixed_lock.exists():
                fixed_lock.unlink()

        for var_name in ("MINIO_PREFLIGHT_PY", "MINIO_RESTORE_PY", "MISSING_CHECK_PY"):
            m = _re.search(rf"{var_name}='\n(.*?)\n'", restore_text, flags=_re.DOTALL)
            assert m, f"Missing embedded Python block {var_name} in restore.sh"
            compile(m.group(1), f"restore.sh:{var_name}", "exec")

        # Verify `env -u` strips exported shell variables matching keys in `.env` before invoking `docker compose`
        env_u_work = tmp_path / "restore-env-unset"
        env_u_work.mkdir()
        (env_u_work / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (env_u_work / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
        env_u_proj = f"env-unset-{os.getpid()}"
        (env_u_work / ".env").write_text(
            f"COMPOSE_PROJECT_NAME={env_u_proj}\n"
            "DB_HOST=postgres\n"
            "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME=file-bucket\n",
            encoding="utf-8",
        )
        fake_docker_dir = tmp_path / "fake-docker-bin"
        fake_docker_dir.mkdir()
        env_dump_file = tmp_path / "docker-seen-env.txt"
        fake_docker = fake_docker_dir / "docker"
        fake_docker.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "BUCKET=%s\\nPROJ=%s\\n" "${{DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME:-UNSET}}" "${{COMPOSE_PROJECT_NAME:-UNSET}}" > "{env_dump_file}"\n'
            "exit 99\n",
            encoding="utf-8",
        )
        fake_docker.chmod(0o755)
        poison_env = os.environ.copy()
        poison_env["PATH"] = f"{fake_docker_dir}:{poison_env.get('PATH', '')}"
        poison_env["DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME"] = "shell-poison-bucket"
        poison_env["COMPOSE_PROJECT_NAME"] = "shell-poison-project"
        env_u_lock = Path(f"/tmp/dalton-{env_u_proj}.restore.lock")
        try:
            subprocess.run(
                [
                    "bash",
                    str(REPO_ROOT / "scripts/standalone/restore.sh"),
                    "--work-dir",
                    str(env_u_work),
                    "--backup-dir",
                    str(backup_stub),
                ],
                env=poison_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            seen = env_dump_file.read_text(encoding="utf-8")
            assert "BUCKET=UNSET\n" in seen, f"Shell bucket env was not unset via env -u: {seen!r}"
            assert "PROJ=UNSET\n" in seen, f"Shell COMPOSE_PROJECT_NAME was not unset via env -u: {seen!r}"
        finally:
            if env_u_lock.is_dir():
                env_u_lock.rmdir()
            elif env_u_lock.exists():
                env_u_lock.unlink()

        # backup.sh identifies restore-created buckets by tag or .dalton-restore.json (never suffix regex)
        backup_text = (REPO_ROOT / "scripts/standalone/backup.sh").read_text(encoding="utf-8")
        assert "restore_bucket_re" not in backup_text
        is_restore_match = _re.search(
            r"(def is_restore_created_bucket\(.*?\n    return False\n)",
            backup_text,
            flags=_re.DOTALL,
        )
        assert is_restore_match is not None
        b_ns: dict[str, Any] = {
            "ClientError": ClientError,
            "RESTORE_TAG_KEY": "dalton-restore",
            "RESTORE_MARKER_KEY": ".dalton-restore.json",
        }
        exec(is_restore_match.group(1), b_ns)
        is_restore_fn = b_ns["is_restore_created_bucket"]

        class _FakeBackupS3:
            def __init__(self, tags: dict[str, list[dict[str, str]]], markers: set[str]) -> None:
                self._tags = tags
                self._markers = markers

            def get_bucket_tagging(self, *, Bucket: str) -> dict[str, Any]:
                if Bucket not in self._tags:
                    raise ClientError({"Error": {"Code": "NoSuchTagSet"}}, "GetBucketTagging")
                return {"TagSet": self._tags[Bucket]}

            def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
                if (Bucket, Key) not in self._markers:
                    raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
                return {}

        fake_s3 = _FakeBackupS3(
            tags={"chat-uploads-r20261123170000": [{"Key": "dalton-restore", "Value": "true"}]},
            markers={("legacy-marker-bucket", ".dalton-restore.json")},
        )
        assert is_restore_fn(fake_s3, "reports-restore-20260101120000") is False
        assert is_restore_fn(fake_s3, "chat-uploads-r20261123170000") is True
        assert is_restore_fn(fake_s3, "legacy-marker-bucket") is True

        # upgrade.sh rollback hint:
        #   - failure strictly before migrate runs -> restore files & images only (do NOT wipe volumes)
        #   - changed versions                     -> point to manual §6.2 steps (`docker volume ls --filter ...` + `docker compose -p ... down -v`)
        #   - unchanged OR unreadable versions after migrate started -> UNCERTAIN (prompt manual check first)
        for scenario, psql_behavior, migrate_body, build_body, extra_upg_flags, expected_mode in (
            (
                "fail-before-migrate",
                'echo "rev1"; exit 0',
                "exit 0",
                'echo "build failed" >&2; exit 1',
                [],
                "before",
            ),
            (
                "fail-versions-unchanged",
                'echo "rev1"; exit 0',
                'echo "[migrate] Waiting for PostgreSQL..."; echo "connection refused" >&2; exit 1',
                "exit 0",
                ["--skip-build"],
                "unknown",
            ),
            (
                "fail-versions-changed",
                'state_f="${UPG_STATE_FILE}"; if [[ -f "$state_f" ]]; then echo "rev2"; else echo "rev1"; fi; exit 0',
                'touch "${UPG_STATE_FILE}"; echo "[migrate] partial upgrade failed" >&2; exit 1',
                "exit 0",
                ["--skip-build"],
                "migrated",
            ),
            (
                "fail-pre-version-unreadable",
                'state_f="${UPG_STATE_FILE}"; if [[ -f "$state_f" ]]; then echo "rev1"; exit 0; else exit 1; fi',
                'touch "${UPG_STATE_FILE}"; echo "migrate failed" >&2; exit 1',
                "exit 0",
                ["--skip-build"],
                "unknown",
            ),
        ):
            sc_root = tmp_path / f"upg-{scenario}"
            uwork = sc_root / "work"
            urel = sc_root / "rel"
            ubak = sc_root / "bak-out"
            fake_bin = sc_root / "bin"
            for d in (
                uwork / "scripts/standalone",
                uwork / "deploy/standalone",
                urel / "scripts/standalone",
                urel / "deploy/standalone",
                ubak,
                fake_bin,
            ):
                d.mkdir(parents=True, exist_ok=True)
            for target_dir in (uwork, urel):
                for req in (
                    "docker-compose.yml",
                    "docker-compose.core.yml",
                    "scripts/standalone/migrate.sh",
                    "scripts/standalone/restore.sh",
                    "deploy/standalone/Caddyfile",
                ):
                    (target_dir / req).write_text("stub\n", encoding="utf-8")
            (uwork / ".env").write_text(
                "COMPOSE_PROJECT_NAME=upg-proj-from-file\nDB_HOST=postgres\nDB_USER=postgres\nDB_NAME=file_db\n",
                encoding="utf-8",
            )
            fake_bak_dir = ubak / "dalton-backup-20260101-000000Z-abc123"
            fake_bak_dir.mkdir()
            backup_stub_script = (
                "#!/usr/bin/env bash\n"
                f"printf '  Backup directory : %s\\n' '{fake_bak_dir}'\n"
            )
            (uwork / "scripts/standalone/backup.sh").write_text(backup_stub_script, encoding="utf-8")
            (urel / "scripts/standalone/backup.sh").write_text(backup_stub_script, encoding="utf-8")
            seen_migrate_env = sc_root / "seen_migrate.txt"
            docker_stub = fake_bin / "docker"
            docker_stub.write_text(
                "#!/usr/bin/env bash\n"
                'if [[ "$*" == *" images -q "* ]]; then echo "img123"; exit 0; fi\n'
                'if [[ "$1" == "tag" ]]; then exit 0; fi\n'
                f'if [[ "$*" == *" build "* ]]; then {build_body}; fi\n'
                f'if [[ "$*" == *" psql "* ]]; then {psql_behavior}; fi\n'
                f'if [[ "$*" == *" run --rm migrate"* ]]; then printf "DB_NAME=%s\\nARGS=%s\\n" "${{DB_NAME:-UNSET}}" "$*" > "{seen_migrate_env}"; {migrate_body}; fi\n'
                "exit 0\n",
                encoding="utf-8",
            )
            docker_stub.chmod(0o755)
            stub_env = os.environ.copy()
            stub_env["PATH"] = f"{fake_bin}:{stub_env.get('PATH', '')}"
            stub_env["UPG_STATE_FILE"] = str(sc_root / "migrated.flag")
            stub_env["COMPOSE_PROJECT_NAME"] = "shell-poison-proj"
            stub_env["DB_NAME"] = "shell_poison_db"
            res_sc = subprocess.run(
                [
                    "bash",
                    str(REPO_ROOT / "scripts/standalone/upgrade.sh"),
                    "--work-dir",
                    str(uwork),
                    "--release-dir",
                    str(urel),
                    "--backup-dir",
                    str(ubak),
                    *extra_upg_flags,
                ],
                env=stub_env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert res_sc.returncode != 0, f"{scenario}: expected upgrade.sh to fail"
            assert "--replace-data" not in res_sc.stderr, (
                f"{scenario}: upgrade.sh must never reference removed --replace-data:\n{res_sc.stderr}"
            )
            assert "-p upg-proj-from-file" in res_sc.stderr and "--env-file" in res_sc.stderr, (
                f"{scenario}: upgrade.sh rollback commands must specify -p <project> and --env-file from .env:\n{res_sc.stderr}"
            )
            assert "shell-poison-proj" not in res_sc.stderr
            if seen_migrate_env.is_file():
                m_seen = seen_migrate_env.read_text(encoding="utf-8")
                assert "DB_NAME=UNSET\n" in m_seen, f"{scenario}: shell DB_NAME was not unset before migrate: {m_seen!r}"
                assert "-p upg-proj-from-file" in m_seen and "--env-file" in m_seen
            if expected_mode == "migrated":
                assert "STANDALONE-DEPLOY.md" in res_sc.stderr and "down -v" in res_sc.stderr, (
                    f"{scenario}: expected manual §6.2 reset-and-restore guide, got:\n{res_sc.stderr}"
                )
                assert "docker volume ls --filter label=com.docker.compose.project=upg-proj-from-file" in res_sc.stderr, (
                    f"{scenario}: expected full volume list command before down -v, got:\n{res_sc.stderr}"
                )
            elif expected_mode == "unknown":
                assert "UNCERTAIN" in res_sc.stderr and "Do NOT assume migrations already ran" in res_sc.stderr, (
                    f"{scenario}: expected uncertain-state manual version check guide, got:\n{res_sc.stderr}"
                )
            else:
                assert "Do NOT wipe volumes or restore data" in res_sc.stderr, (
                    f"{scenario}: pre-migration failure must not suggest wiping data, got:\n{res_sc.stderr}"
                )

    def test_b6_r6_restore_guards_storage_config_and_flock_stderr(self, tmp_path: Path) -> None:
        """Offline verification for Batch 6 R6 fixes (1-7): whole-instance PG empty check,
        bucket/key-prefix check, effective app storage rules & --skip-minio, explicit -p compose,
        flock stderr preservation, mid-restore failure hint, and .env.tmp-* decrypt prefix."""
        import tarfile as _tarfile

        def _make_minio_backup(
            name: str,
            *,
            primary_bucket: str = "chat-uploads",
            primary_key_prefix: str = "",
            targets: dict[str, dict[str, str]] | None = None,
            env_content: str | None = None,
            pass_file: Path | None = None,
        ) -> Path:
            bdir = tmp_path / name
            bdir.mkdir(parents=True, exist_ok=True)
            (bdir / "postgres.dump").write_bytes(b"PGDMP")
            manifest_targets = targets if targets is not None else {
                "chat_upload": {"bucket": primary_bucket, "key_prefix": primary_key_prefix}
            }
            mm = {
                "buckets": [primary_bucket] if primary_bucket else [],
                "primary_bucket": primary_bucket,
                "primary_key_prefix": primary_key_prefix,
                "targets": manifest_targets,
                "var_buckets": {"DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME": primary_bucket} if primary_bucket else {},
                "bucket_count": 1 if primary_bucket else 0,
                "object_count": 0,
                "total_bytes": 0,
                "objects": [],
            }
            raw_mm = json.dumps(mm).encode("utf-8")
            with _tarfile.open(bdir / "minio-objects.tar.gz", "w:gz") as tf:
                import io as _io

                ti = _tarfile.TarInfo(name="_minio_manifest.json")
                ti.size = len(raw_mm)
                tf.addfile(ti, _io.BytesIO(raw_mm))
            top_manifest: dict[str, Any] = {
                "schema_version": 2,
                "minio": {
                    "file": "minio-objects.tar.gz",
                    "primary_bucket": primary_bucket,
                    "primary_key_prefix": primary_key_prefix,
                    "targets": manifest_targets,
                    "bucket_count": mm["bucket_count"],
                    "object_count": 0,
                    "total_bytes": 0,
                },
                "env_backup": "excluded",
                "env_encryption": None,
            }
            if env_content is not None and pass_file is not None:
                plain = tmp_path / f"{name}.plain.env"
                plain.write_text(env_content, encoding="utf-8")
                subprocess.run(
                    [
                        "openssl",
                        "enc",
                        "-aes-256-cbc",
                        "-pbkdf2",
                        "-md",
                        "sha256",
                        "-iter",
                        "10000",
                        "-salt",
                        "-in",
                        str(plain),
                        "-out",
                        str(bdir / "env.enc"),
                        "-pass",
                        f"file:{pass_file}",
                    ],
                    check=True,
                    capture_output=True,
                    timeout=15,
                )
                top_manifest["env_backup"] = "encrypted"
                top_manifest["env_encryption"] = {
                    "cipher": "aes-256-cbc",
                    "kdf": "pbkdf2",
                    "digest": "sha256",
                    "iterations": 10000,
                }
            (bdir / "manifest.json").write_text(json.dumps(top_manifest), encoding="utf-8")
            return bdir

        def _make_work(name: str, env_text: str, *, include_agent_yaml: bool = True) -> Path:
            w = tmp_path / name
            w.mkdir(parents=True, exist_ok=True)
            (w / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
            (w / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
            if include_agent_yaml:
                (w / "agent_config.yaml").write_text('storage:\n  provider: "s3"\n', encoding="utf-8")
            (w / ".env").write_text(env_text, encoding="utf-8")
            return w

        def _make_compose_config_docker_stub(
            stub_dir: Path,
            *,
            extra_backend_env: dict[str, str] | None = None,
            extra_bash: str = "",
            log_file: Path | None = None,
        ) -> Path:
            """Create a fake `docker` binary that synthesizes `docker compose config --format json`
            from `--env-file` (plus optional backend overrides) without requiring a real Docker daemon."""
            stub_dir.mkdir(parents=True, exist_ok=True)
            (stub_dir / "flock").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            (stub_dir / "flock").chmod(0o755)
            overrides_json = json.dumps(extra_backend_env or {})
            helper_py = stub_dir / "_render_compose_cfg.py"
            helper_py.write_text(
                "import json, re, sys\n"
                "args = sys.argv[1:]\n"
                "env_path = ''\n"
                "for i, a in enumerate(args):\n"
                "    if a == '--env-file' and i + 1 < len(args):\n"
                "        env_path = args[i + 1]\n"
                "env = {}\n"
                "if env_path:\n"
                "    for raw in open(env_path, encoding='utf-8'):\n"
                "        s = raw.strip()\n"
                "        if not s or s.startswith('#'): continue\n"
                "        m = re.match(r'^\\s*(?:export\\s+)?([A-Za-z_][A-Za-z0-9_]*)\\s*[=:](.*)$', raw.rstrip('\\r\\n'))\n"
                "        if m:\n"
                "            v = m.group(2).strip()\n"
                "            if len(v) >= 2 and v[0] == v[-1] and v[0] in (\"'\", '\"'): v = v[1:-1]\n"
                "            env[m.group(1)] = v\n"
                "if env.get('MINIO_ROOT_USER') or env.get('MINIO_ROOT_PASSWORD') or env.get('DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME') or env.get('DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL'):\n"
                "    env.setdefault('DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL', 'http://minio:9000')\n"
                "    env.setdefault('DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME', 'chat-uploads')\n"
                "    env.setdefault('DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID', env.get('MINIO_ROOT_USER', ''))\n"
                "    env.setdefault('DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY', env.get('MINIO_ROOT_PASSWORD', ''))\n"
                f"env.update(json.loads({overrides_json!r}))\n"
                "print(json.dumps({'services': {'backend': {'environment': env}}}))\n",
                encoding="utf-8",
            )
            log_stmt = f'printf "%s\\n" "$*" >> "{log_file}"\n' if log_file is not None else ""
            docker_bin = stub_dir / "docker"
            docker_bin.write_text(
                "#!/usr/bin/env bash\n"
                + log_stmt
                + f'if [[ "$*" == *" config --format json"* ]]; then {sys.executable} "{helper_py}" "$@"; exit $?; fi\n'
                + extra_bash
                + "\nexit 0\n",
                encoding="utf-8",
            )
            docker_bin.chmod(0o755)
            return stub_dir

        base_stub_bin = _make_compose_config_docker_stub(tmp_path / "r6-base-stub-bin")
        base_stub_env = os.environ.copy()
        base_stub_env["PATH"] = f"{base_stub_bin}:{base_stub_env.get('PATH', '')}"

        # 1. Req 3: YAML default storage.provider="s3" when STORAGE_PROVIDER is unset:
        #    chat-uploads on MinIO + S3_BUCKET_NAME on default AWS S3 MUST be rejected
        #    by BOTH backup.sh and restore.sh.
        w_aws_split = _make_work(
            "work-aws-split",
            f"COMPOSE_PROJECT_NAME=r6-aws-split-{os.getpid()}\n"
            "DB_HOST=postgres\n"
            "MINIO_ROOT_USER=dalton\n"
            "MINIO_ROOT_PASSWORD=secretpass\n"
            "S3_BUCKET_NAME=my-aws-bucket\n",
        )
        bak_default = _make_minio_backup("bak-default")
        res_bak_split = subprocess.run(
            ["bash", str(REPO_ROOT / "scripts/standalone/backup.sh"), "--work-dir", str(w_aws_split)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert res_bak_split.returncode != 0
        assert "Multiple object storage services" in res_bak_split.stderr

        lock_split = Path(f"/tmp/dalton-r6-aws-split-{os.getpid()}.restore.lock")
        try:
            res_res_split = subprocess.run(
                [
                    "bash",
                    str(REPO_ROOT / "scripts/standalone/restore.sh"),
                    "--work-dir",
                    str(w_aws_split),
                    "--backup-dir",
                    str(bak_default),
                ],
                env=base_stub_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert res_res_split.returncode != 0
            assert "Multiple object storage services" in res_res_split.stderr
        finally:
            if lock_split.is_dir():
                lock_split.rmdir()
            elif lock_split.exists():
                lock_split.unlink()

        # 2. Req 3: --skip-minio refused when object storage is configured, allowed ONLY when none is configured.
        w_has_minio = _make_work(
            "work-has-minio",
            f"COMPOSE_PROJECT_NAME=r6-skip-refuse-{os.getpid()}\n"
            "DB_HOST=postgres\n"
            "MINIO_ROOT_USER=dalton\n"
            "MINIO_ROOT_PASSWORD=secretpass\n",
        )
        lock_skip = Path(f"/tmp/dalton-r6-skip-refuse-{os.getpid()}.restore.lock")
        try:
            res_skip_refuse = subprocess.run(
                [
                    "bash",
                    str(REPO_ROOT / "scripts/standalone/restore.sh"),
                    "--work-dir",
                    str(w_has_minio),
                    "--backup-dir",
                    str(bak_default),
                    "--skip-minio",
                ],
                env=base_stub_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert res_skip_refuse.returncode != 0
            assert "--skip-minio" in res_skip_refuse.stderr
        finally:
            if lock_skip.is_dir():
                lock_skip.rmdir()
            elif lock_skip.exists():
                lock_skip.unlink()

        w_no_storage = _make_work(
            "work-no-storage",
            f"COMPOSE_PROJECT_NAME=r6-skip-ok-{os.getpid()}\n"
            "DB_HOST=postgres\n"
            "DB_USER=postgres\n"
            "DB_PASSWORD=postgres\n",
        )
        res_skip_ok = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts/standalone/storage_config.py"),
                "check-env",
                "--env-file",
                str(w_no_storage / ".env"),
                "--work-dir",
                str(w_no_storage),
                "--require-none-for-skip-minio",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert res_skip_ok.returncode == 0, f"--skip-minio should be allowed when no storage is configured: {res_skip_ok.stderr}"

        # 3. Req 2: Bucket name mismatch AND key prefix mismatch refused BEFORE touching Docker or .env
        for mismatch_case, env_extra in (
            ("bucket", "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME=different-bucket\n"),
            ("prefix", "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME=chat-uploads\nDALTON_CHAT_UPLOAD_STORAGE_KEY_PREFIX=wrong-prefix\n"),
        ):
            proj_m = f"r6-mismatch-{mismatch_case}-{os.getpid()}"
            wm = _make_work(
                f"work-mismatch-{mismatch_case}",
                f"COMPOSE_PROJECT_NAME={proj_m}\n"
                "DB_HOST=postgres\n"
                "MINIO_ROOT_USER=dalton\n"
                "MINIO_ROOT_PASSWORD=secretpass\n"
                + env_extra,
            )
            lock_m = Path(f"/tmp/dalton-{proj_m}.restore.lock")
            try:
                res_m = subprocess.run(
                    [
                        "bash",
                        str(REPO_ROOT / "scripts/standalone/restore.sh"),
                        "--work-dir",
                        str(wm),
                        "--backup-dir",
                        str(bak_default),
                    ],
                    env=base_stub_env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                assert res_m.returncode != 0
                assert "--replace-env" in res_m.stderr
                assert "先改 .env 让两者一致" in res_m.stderr
            finally:
                if lock_m.is_dir():
                    lock_m.rmdir()
                elif lock_m.exists():
                    lock_m.unlink()

        # 4. Req 1 + Req 5 + B6R7 Req 1: With `flock` on PATH, non-empty other DB in pg_database
        #    refuses restore, lists the other DB name and DB_NAME, does NOT suggest `down -v`,
        #    while app-DB-only non-empty refusal STILL points to STANDALONE-DEPLOY.md (`down -v`).
        fake_docker_seen = tmp_path / "r6-docker-args.log"
        fake_bin = _make_compose_config_docker_stub(
            tmp_path / "r6-fake-bin",
            log_file=fake_docker_seen,
            extra_bash=(
                'if [[ "$*" == *" ps --status running --services"* ]]; then echo "postgres"; exit 0; fi\n'
                'if [[ "$*" == *" pg_isready "* ]]; then exit 0; fi\n'
                'if [[ "$*" == *" pg_restore --list"* ]]; then exit 0; fi\n'
                'if [[ "$*" == *"current_setting(\'is_superuser\')"* ]]; then echo "on"; exit 0; fi\n'
                'if [[ "$*" == *"FROM pg_database"* ]]; then printf "other_app_db\\npostgres\\n"; exit 0; fi\n'
                'if [[ "$*" == *" -d other_app_db "* ]]; then printf "1\\t0\\t0\\n"; exit 0; fi\n'
                'if [[ "$*" == *" -d postgres "* ]]; then printf "0\\t0\\t0\\n"; exit 0; fi\n'
            ),
        )

        proj_flock = f"r6-flock-nonempty-{os.getpid()}"
        w_flock = _make_work(
            "work-flock-nonempty",
            f"COMPOSE_PROJECT_NAME={proj_flock}\n"
            "DB_HOST=postgres\n"
            "DB_USER=postgres\n"
            "DB_PASSWORD=secretpg\n"
            "DB_NAME=postgres\n"
            "MINIO_ROOT_USER=dalton\n"
            "MINIO_ROOT_PASSWORD=secretpass\n",
        )
        flock_env = os.environ.copy()
        flock_env["PATH"] = f"{fake_bin}:{flock_env.get('PATH', '')}"
        flock_env["COMPOSE_PROJECT_NAME"] = "shell-poison-must-not-appear"
        lock_flock = Path(f"/tmp/dalton-{proj_flock}.restore.lock")
        try:
            res_flock = subprocess.run(
                [
                    "bash",
                    str(REPO_ROOT / "scripts/standalone/restore.sh"),
                    "--work-dir",
                    str(w_flock),
                    "--backup-dir",
                    str(bak_default),
                ],
                env=flock_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert res_flock.returncode != 0
            assert "other_app_db" in res_flock.stderr, f"stderr swallowed or missing DB name: {res_flock.stderr!r}"
            assert "backup.sh 只备份 postgres" in res_flock.stderr, f"missing other-DB backup scope warning: {res_flock.stderr!r}"
            assert "STANDALONE-DEPLOY.md" in res_flock.stderr, f"stderr missing manual path: {res_flock.stderr!r}"
            assert "down -v" not in res_flock.stderr, f"other-DB refusal MUST NOT suggest down -v: {res_flock.stderr!r}"
            docker_calls = fake_docker_seen.read_text(encoding="utf-8")
            assert f"compose -p {proj_flock} " in docker_calls
            assert "shell-poison-must-not-appear" not in docker_calls
            assert "pg_proc" in docker_calls and "pg_type" in docker_calls
        finally:
            if lock_flock.is_dir():
                lock_flock.rmdir()
            elif lock_flock.exists():
                lock_flock.unlink()

        # App-DB-only non-empty refusal STILL prints manual `down -v` hint with project volume filter
        fake_bin_app_only = _make_compose_config_docker_stub(
            tmp_path / "r6-fake-bin-app-only",
            extra_bash=(
                'if [[ "$*" == *" ps --status running --services"* ]]; then echo "postgres"; exit 0; fi\n'
                'if [[ "$*" == *" pg_isready "* ]]; then exit 0; fi\n'
                'if [[ "$*" == *" pg_restore --list"* ]]; then exit 0; fi\n'
                'if [[ "$*" == *"current_setting(\'is_superuser\')"* ]]; then echo "on"; exit 0; fi\n'
                'if [[ "$*" == *"FROM pg_database"* ]]; then printf "postgres\\n"; exit 0; fi\n'
                'if [[ "$*" == *" -d postgres "* ]]; then printf "3\\t0\\t0\\n"; exit 0; fi\n'
            ),
        )
        app_only_env = os.environ.copy()
        app_only_env["PATH"] = f"{fake_bin_app_only}:{app_only_env.get('PATH', '')}"
        try:
            res_app_only = subprocess.run(
                [
                    "bash",
                    str(REPO_ROOT / "scripts/standalone/restore.sh"),
                    "--work-dir",
                    str(w_flock),
                    "--backup-dir",
                    str(bak_default),
                ],
                env=app_only_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert res_app_only.returncode != 0
            assert "STANDALONE-DEPLOY.md" in res_app_only.stderr
            assert "down -v" in res_app_only.stderr
            assert f"label=com.docker.compose.project={proj_flock}" in res_app_only.stderr
        finally:
            if lock_flock.is_dir():
                lock_flock.rmdir()
            elif lock_flock.exists():
                lock_flock.unlink()

        # 5. Req 6 + Req 7.1: Mid-restore failure after writing .env prints half-restored hint,
        #    and decrypt temp file uses .env.tmp-* prefix without self-deletion.
        pass_f = tmp_path / "r6.pass"
        pass_f.write_text("r6-test-passphrase-123456\n", encoding="utf-8")
        pass_f.chmod(0o600)
        proj_mid = f"r6-mid-fail-{os.getpid()}"
        env_mid_text = (
            f"COMPOSE_PROJECT_NAME={proj_mid}\n"
            "DB_HOST=postgres\n"
            "DB_USER=postgres\n"
            "DB_PASSWORD=secretpg\n"
            "DB_NAME=postgres\n"
        )
        bak_mid = _make_minio_backup(
            "bak-mid-fail",
            primary_bucket="",
            targets={},
            env_content=env_mid_text,
            pass_file=pass_f,
        )
        w_mid = tmp_path / "work-mid-fail"
        w_mid.mkdir()
        (w_mid / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (w_mid / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
        (w_mid / "agent_config.yaml").write_text('storage:\n  provider: "none"\n', encoding="utf-8")
        stale_dec = w_mid / ".env.tmp-decrypt-stale1"
        stale_dec.write_text("STALE=1\n", encoding="utf-8")

        fake_bin_mid = _make_compose_config_docker_stub(
            tmp_path / "r6-fake-bin-mid",
            extra_bash=(
                'if [[ "$*" == *" ps --status running --services"* ]]; then echo "postgres"; exit 0; fi\n'
                'if [[ "$*" == *" pg_isready "* ]]; then exit 0; fi\n'
                'if [[ "$*" == *" pg_restore --list"* ]]; then exit 0; fi\n'
                'if [[ "$*" == *"current_setting(\'is_superuser\')"* ]]; then echo "on"; exit 0; fi\n'
                'if [[ "$*" == *"FROM pg_database"* ]]; then echo "postgres"; exit 0; fi\n'
                'if [[ "$*" == *" -d postgres "* && "$*" == *"pg_class"* ]]; then printf "0\\t0\\t0\\n"; exit 0; fi\n'
                'if [[ "$*" == *" pg_restore "* ]]; then echo "simulated pg_restore failure" >&2; exit 1; fi\n'
            ),
        )
        mid_env = os.environ.copy()
        mid_env["PATH"] = f"{fake_bin_mid}:{mid_env.get('PATH', '')}"
        lock_mid = Path(f"/tmp/dalton-{proj_mid}.restore.lock")
        try:
            res_mid = subprocess.run(
                [
                    "bash",
                    str(REPO_ROOT / "scripts/standalone/restore.sh"),
                    "--work-dir",
                    str(w_mid),
                    "--backup-dir",
                    str(bak_mid),
                    "--env-passphrase-file",
                    str(pass_f),
                    "--skip-minio",
                    "--skip-caddy",
                    "--skip-edge-build",
                ],
                env=mid_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert res_mid.returncode != 0
            assert not stale_dec.exists(), "Stale .env.tmp-decrypt-* must be cleaned up on next run"
            assert (w_mid / ".env").is_file(), ".env should have been written before pg_restore failed"
            assert not list(w_mid.glob(".env.tmp-*")), "No leftover .env.tmp-* files after clean exit"
            assert "半成品" in res_mid.stderr and "down -v" in res_mid.stderr and "STANDALONE-DEPLOY.md" in res_mid.stderr, (
                f"Mid-restore failure should print half-restored recovery hint, got:\n{res_mid.stderr}"
            )
            assert "NOT modified" in res_mid.stderr
        finally:
            if lock_mid.is_dir():
                lock_mid.rmdir()
            elif lock_mid.exists():
                lock_mid.unlink()

        # 6. Req 1: PostgreSQL superuser credential connection failure refuses restore and explains why.
        fake_bin_pgfail = _make_compose_config_docker_stub(
            tmp_path / "r6-fake-bin-pgfail",
            extra_bash=(
                'if [[ "$*" == *" ps --status running --services"* ]]; then echo "postgres"; exit 0; fi\n'
                'if [[ "$*" == *" pg_isready "* ]]; then exit 0; fi\n'
                'if [[ "$*" == *" pg_restore --list"* ]]; then exit 0; fi\n'
                'if [[ "$*" == *"current_setting(\'is_superuser\')"* ]]; then echo "psql: error: FATAL: password authentication failed for user postgres" >&2; exit 2; fi\n'
            ),
        )
        proj_pgfail = f"r6-pgfail-{os.getpid()}"
        w_pgfail = _make_work(
            "work-pgfail",
            f"COMPOSE_PROJECT_NAME={proj_pgfail}\n"
            "DB_HOST=postgres\n"
            "DB_USER=postgres\n"
            "DB_PASSWORD=wrongpass\n"
            "DB_NAME=postgres\n"
            "MINIO_ROOT_USER=dalton\n"
            "MINIO_ROOT_PASSWORD=secretpass\n",
        )
        pgfail_env = os.environ.copy()
        pgfail_env["PATH"] = f"{fake_bin_pgfail}:{pgfail_env.get('PATH', '')}"
        lock_pgfail = Path(f"/tmp/dalton-{proj_pgfail}.restore.lock")
        try:
            res_pgfail = subprocess.run(
                [
                    "bash",
                    str(REPO_ROOT / "scripts/standalone/restore.sh"),
                    "--work-dir",
                    str(w_pgfail),
                    "--backup-dir",
                    str(bak_default),
                ],
                env=pgfail_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert res_pgfail.returncode != 0
            assert "Failed to connect to target PostgreSQL instance" in res_pgfail.stderr
            assert "password authentication failed" in res_pgfail.stderr
            assert "wrongpass" not in res_pgfail.stderr, "DB_PASSWORD must never be leaked to stderr"
        finally:
            if lock_pgfail.is_dir():
                lock_pgfail.rmdir()
            elif lock_pgfail.exists():
                lock_pgfail.unlink()

        # 7. B6R7 Req 2: verify-backup-refs fails on missing completed (ready/completing) uploads
        #    with cleanup-race hint, while skipping in-progress 'uploading' records and reporting count.
        mm_refs = tmp_path / "r7-minio-manifest.json"
        mm_refs.write_text(
            json.dumps(
                {
                    "primary_bucket": "chat-uploads",
                    "primary_key_prefix": "pfx",
                    "targets": {
                        "chat_upload": {
                            "endpoint": "http://minio:9000",
                            "bucket": "chat-uploads",
                            "key_prefix": "pfx",
                        }
                    },
                    "objects": [
                        {"bucket": "chat-uploads", "key": "pfx/chat-uploads/ws1/u1/doc.txt", "size": 10},
                    ],
                }
            ),
            encoding="utf-8",
        )
        copy_ok_sql = tmp_path / "r7-copy-ok.sql"
        copy_ok_sql.write_text(
            "COPY public.dalton_chat_file_uploads (upload_id, object_key, state, cleaned_at) FROM stdin;\n"
            "u1\tchat-uploads/ws1/u1/doc.txt\tready\t\\N\n"
            "u2\tchat-uploads/ws1/u2/wip.txt\tuploading\t\\N\n"
            "u3\tchat-uploads/ws1/u3/gone.txt\taborted\t\\N\n"
            "u4\tchat-uploads/ws1/u4/cleaned.txt\tready\t2026-11-23 00:00:00+00\n"
            "\\.\n",
            encoding="utf-8",
        )
        res_ref_ok = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts/standalone/storage_config.py"),
                "verify-backup-refs",
                "--copy-sql",
                str(copy_ok_sql),
                "--minio-manifest",
                str(mm_refs),
                "--caller",
                "backup",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert res_ref_ok.returncode == 0, f"Expected verify-backup-refs to pass: {res_ref_ok.stderr}"
        assert "skipped 1" in res_ref_ok.stdout.lower()

        copy_miss_sql = tmp_path / "r7-copy-miss.sql"
        copy_miss_sql.write_text(
            "COPY public.dalton_chat_file_uploads (upload_id, object_key, state, cleaned_at) FROM stdin;\n"
            "u1\tchat-uploads/ws1/u1/doc.txt\tready\t\\N\n"
            "u5\tchat-uploads/ws1/u5/missing.pdf\tready\t\\N\n"
            "\\.\n",
            encoding="utf-8",
        )
        res_ref_miss = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts/standalone/storage_config.py"),
                "verify-backup-refs",
                "--copy-sql",
                str(copy_miss_sql),
                "--minio-manifest",
                str(mm_refs),
                "--caller",
                "backup",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert res_ref_miss.returncode != 0
        assert "chat-uploads/ws1/u5/missing.pdf" in res_ref_miss.stderr
        assert "可能是备份期间有清理任务运行，请重新备份" in res_ref_miss.stderr

        # 8. B6R7 Req 3: Compose config override on `backend` bucket or endpoint is authoritative
        #    and causes restore.sh to refuse even when `.env` still matches the backup.
        bak_with_ep = _make_minio_backup(
            "bak-with-ep",
            primary_bucket="chat-uploads",
            targets={
                "chat_upload": {
                    "endpoint": "http://minio:9000",
                    "bucket": "chat-uploads",
                    "key_prefix": "",
                }
            },
        )
        for ov_case, ov_env, expected_needle in (
            ("bucket", {"DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME": "override-only-bucket"}, "override-only-bucket"),
            ("endpoint", {"DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL": "http://other-minio:9000"}, "http://other-minio:9000"),
        ):
            proj_ov = f"r7-compose-ov-{ov_case}-{os.getpid()}"
            w_ov = _make_work(
                f"work-compose-ov-{ov_case}",
                f"COMPOSE_PROJECT_NAME={proj_ov}\n"
                "DB_HOST=postgres\n"
                "MINIO_ROOT_USER=dalton\n"
                "MINIO_ROOT_PASSWORD=secretpass\n"
                "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME=chat-uploads\n",
            )
            ov_bin = _make_compose_config_docker_stub(
                tmp_path / f"r7-ov-bin-{ov_case}",
                extra_backend_env=ov_env,
            )
            ov_run_env = os.environ.copy()
            ov_run_env["PATH"] = f"{ov_bin}:{ov_run_env.get('PATH', '')}"
            lock_ov = Path(f"/tmp/dalton-{proj_ov}.restore.lock")
            try:
                res_ov = subprocess.run(
                    [
                        "bash",
                        str(REPO_ROOT / "scripts/standalone/restore.sh"),
                        "--work-dir",
                        str(w_ov),
                        "--backup-dir",
                        str(bak_with_ep),
                    ],
                    env=ov_run_env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                assert res_ov.returncode != 0, f"Expected compose override ({ov_case}) to be refused"
                assert expected_needle in res_ov.stderr, f"Missing {expected_needle!r} in stderr:\n{res_ov.stderr}"
                assert "compose config" in res_ov.stderr.lower()
            finally:
                if lock_ov.is_dir():
                    lock_ov.rmdir()
                elif lock_ov.exists():
                    lock_ov.unlink()

        # Verify restore.sh has zero `exec 9... 2>/dev/null`, skips 'uploading' in SQL, and MISSING_CHECK_PY exits 1
        restore_src = (REPO_ROOT / "scripts/standalone/restore.sh").read_text(encoding="utf-8")
        assert not any(
            "2>/dev/null" in ln for ln in restore_src.splitlines() if "exec 9" in ln
        ), "restore.sh must never use `exec 9... 2>/dev/null`"
        assert "dalton-restore-env." not in restore_src
        assert ".env.tmp-decrypt-" in restore_src
        assert "state IN ('ready', 'completing')" in restore_src
        assert "state IN ('ready', 'completing', 'uploading')" not in restore_src
        assert "sys.exit(1)" in restore_src.split("MISSING_CHECK_PY='", 1)[1].split("'\n", 1)[0]

    def test_b6_r8_explicit_project_name_priority_and_validation(self, tmp_path: Path) -> None:
        """Offline verification for Batch 6 R8: --project-name priority (--project-name > .env > dir),
        shell COMPOSE_PROJECT_NAME still ignored, validation with sanitize_project_name rules, and
        upgrade.sh forwarding --project-name to backup.sh."""
        bak_stub = tmp_path / "bak-stub"
        bak_stub.mkdir()
        (bak_stub / "postgres.dump").write_bytes(b"PGDMP")

        rel_stub = tmp_path / "rel-stub"
        (rel_stub / "scripts/standalone").mkdir(parents=True)
        (rel_stub / "deploy/standalone").mkdir(parents=True)
        for req in (
            "docker-compose.yml",
            "docker-compose.core.yml",
            "scripts/standalone/migrate.sh",
            "scripts/standalone/backup.sh",
            "scripts/standalone/restore.sh",
            "deploy/standalone/Caddyfile",
        ):
            (rel_stub / req).write_text("stub\n", encoding="utf-8")

        work_base = tmp_path / "work-r8"
        work_base.mkdir()
        (work_base / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (work_base / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
        (work_base / ".env").write_text(
            "COMPOSE_PROJECT_NAME=proj-from-env\nDB_HOST=postgres\n",
            encoding="utf-8",
        )

        # 1. Invalid --project-name values and missing argument are rejected with exit 2 by all 3 scripts
        #    (Compose rule `^[a-z0-9][a-z0-9_-]*$` allows trailing `-` / `_`).
        invalid_names = (
            "",
            "UpperCase",
            "-leading",
            "_leading",
            "bad.dot",
            "bad/slash",
            "bad space",
            "../escape",
        )
        script_base_args = (
            ("backup.sh", ["--work-dir", str(work_base)]),
            ("upgrade.sh", ["--work-dir", str(work_base), "--release-dir", str(rel_stub)]),
            ("restore.sh", ["--work-dir", str(work_base), "--backup-dir", str(bak_stub)]),
        )
        for script_name, base_args in script_base_args:
            script_path = str(REPO_ROOT / "scripts/standalone" / script_name)
            res_missing = subprocess.run(
                ["bash", script_path, *base_args, "--project-name"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert res_missing.returncode == 2, (
                f"{script_name} should exit 2 when --project-name has no value: {res_missing.stderr}"
            )
            for bad in invalid_names:
                for flag_args in (["--project-name", bad], [f"--project-name={bad}"]):
                    res_bad = subprocess.run(
                        ["bash", script_path, *base_args, *flag_args],
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    assert res_bad.returncode == 2, (
                        f"{script_name} must reject invalid {flag_args!r} with exit 2 "
                        f"(got rc={res_bad.returncode}, stderr={res_bad.stderr!r})"
                    )

        # 2. backup.sh: --project-name > .env > dir basename; shell COMPOSE_PROJECT_NAME is always ignored
        bak_bin = tmp_path / "r8-bak-bin"
        bak_bin.mkdir()
        bak_docker_log = tmp_path / "r8-bak-docker.log"
        (bak_bin / "docker").write_text(
            "#!/usr/bin/env bash\n"
            f'printf "ENV_PROJ=%s|ARGS=%s\\n" "${{COMPOSE_PROJECT_NAME:-UNSET}}" "$*" > "{bak_docker_log}"\n'
            "exit 99\n",
            encoding="utf-8",
        )
        (bak_bin / "docker").chmod(0o755)
        bak_env = os.environ.copy()
        bak_env["PATH"] = f"{bak_bin}:{bak_env.get('PATH', '')}"
        bak_env["COMPOSE_PROJECT_NAME"] = "shell-poison-proj"

        # 2a. Explicit --project-name overrides .env and shell env (both space and `=` forms)
        for flag_args, want_proj in (
            (["--project-name", "cli-bak-proj-1"], "cli-bak-proj-1"),
            (["--project-name=cli_bak_proj_2"], "cli_bak_proj_2"),
        ):
            bak_docker_log.unlink(missing_ok=True)
            subprocess.run(
                [
                    "bash",
                    str(REPO_ROOT / "scripts/standalone/backup.sh"),
                    "--work-dir",
                    str(work_base),
                    *flag_args,
                ],
                env=bak_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            logged = bak_docker_log.read_text(encoding="utf-8")
            assert "ENV_PROJ=UNSET|" in logged, f"backup.sh leaked shell COMPOSE_PROJECT_NAME: {logged!r}"
            assert f"compose -p {want_proj} " in logged, f"backup.sh did not use {want_proj!r}: {logged!r}"
            assert "proj-from-env" not in logged and "shell-poison-proj" not in logged

        # 2b. Without --project-name, .env wins over shell env; without .env key, sanitized dir name wins
        bak_docker_log.unlink(missing_ok=True)
        subprocess.run(
            ["bash", str(REPO_ROOT / "scripts/standalone/backup.sh"), "--work-dir", str(work_base)],
            env=bak_env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        logged_env = bak_docker_log.read_text(encoding="utf-8")
        assert "ENV_PROJ=UNSET|" in logged_env
        assert "compose -p proj-from-env " in logged_env
        assert "shell-poison-proj" not in logged_env

        work_dir_fallback = tmp_path / "My_Fallback.Dir"
        work_dir_fallback.mkdir()
        (work_dir_fallback / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (work_dir_fallback / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
        (work_dir_fallback / ".env").write_text("DB_HOST=postgres\n", encoding="utf-8")
        bak_docker_log.unlink(missing_ok=True)
        subprocess.run(
            ["bash", str(REPO_ROOT / "scripts/standalone/backup.sh"), "--work-dir", str(work_dir_fallback)],
            env=bak_env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        logged_dir = bak_docker_log.read_text(encoding="utf-8")
        assert "compose -p my_fallback-dir " in logged_dir
        assert "shell-poison-proj" not in logged_dir

        # 3. upgrade.sh: --project-name overrides .env & shell env and is forwarded to backup.sh
        #    (both when calling existing WORK_DIR/backup.sh and new-package RELEASE_SRC/backup.sh)
        for case_name, has_old_backup, extra_upg_flags, want_proj in (
            ("explicit-new-pkg", False, ["--project-name", "cli-upg-proj-1"], "cli-upg-proj-1"),
            ("explicit-existing-bak", True, ["--project-name=cli_upg_proj_2"], "cli_upg_proj_2"),
            ("from-env-new-pkg", False, [], "proj-from-env"),
        ):
            croot = tmp_path / f"r8-upg-{case_name}"
            uwork = croot / "work"
            urel = croot / "rel"
            ubak = croot / "bak"
            uwork.mkdir(parents=True)
            (urel / "scripts/standalone").mkdir(parents=True)
            (urel / "deploy/standalone").mkdir(parents=True)
            ubak.mkdir()
            for req in (
                "docker-compose.yml",
                "docker-compose.core.yml",
                "scripts/standalone/migrate.sh",
                "scripts/standalone/restore.sh",
                "deploy/standalone/Caddyfile",
            ):
                (urel / req).write_text("stub\n", encoding="utf-8")
            (uwork / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
            (uwork / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
            (uwork / ".env").write_text(
                "COMPOSE_PROJECT_NAME=proj-from-env\nDB_HOST=postgres\n",
                encoding="utf-8",
            )
            seen_backup_args = croot / "seen_backup_args.txt"
            backup_capture = (
                "#!/usr/bin/env bash\n"
                f'printf "%s\\n" "$*" > "{seen_backup_args}"\n'
                "exit 1\n"
            )
            (urel / "scripts/standalone/backup.sh").write_text(backup_capture, encoding="utf-8")
            if has_old_backup:
                (uwork / "scripts/standalone").mkdir(parents=True)
                (uwork / "scripts/standalone/backup.sh").write_text(backup_capture, encoding="utf-8")
            upg_env = os.environ.copy()
            upg_env["COMPOSE_PROJECT_NAME"] = "shell-poison-proj"
            res_u = subprocess.run(
                [
                    "bash",
                    str(REPO_ROOT / "scripts/standalone/upgrade.sh"),
                    "--work-dir",
                    str(uwork),
                    "--release-dir",
                    str(urel),
                    "--backup-dir",
                    str(ubak),
                    *extra_upg_flags,
                ],
                env=upg_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert res_u.returncode != 0
            captured = seen_backup_args.read_text(encoding="utf-8")
            assert f"--project-name {want_proj}" in captured, (
                f"{case_name}: upgrade.sh did not pass --project-name {want_proj!r} to backup.sh: {captured!r}"
            )
            assert f"-p {want_proj} " in res_u.stderr, (
                f"{case_name}: upgrade.sh rollback command did not use -p {want_proj!r}: {res_u.stderr!r}"
            )
            assert "shell-poison-proj" not in captured and "shell-poison-proj" not in res_u.stderr

        # 4. restore.sh: --project-name overrides .env and shell env for lock path and docker compose -p
        res_bin = tmp_path / "r8-res-bin"
        res_bin.mkdir()
        res_docker_log = tmp_path / "r8-res-docker.log"
        (res_bin / "docker").write_text(
            "#!/usr/bin/env bash\n"
            f'printf "ENV_PROJ=%s|ARGS=%s\\n" "${{COMPOSE_PROJECT_NAME:-UNSET}}" "$*" > "{res_docker_log}"\n'
            "exit 99\n",
            encoding="utf-8",
        )
        (res_bin / "docker").chmod(0o755)
        res_env = os.environ.copy()
        res_env["PATH"] = f"{res_bin}:{res_env.get('PATH', '')}"
        res_env["COMPOSE_PROJECT_NAME"] = "shell-poison-proj"

        for flag_args, want_proj in (
            (["--project-name", f"cli-res-proj-{os.getpid()}"], f"cli-res-proj-{os.getpid()}"),
            ([f"--project-name=cli_res_proj_eq_{os.getpid()}"], f"cli_res_proj_eq_{os.getpid()}"),
        ):
            lock_p = Path(f"/tmp/dalton-{want_proj}.restore.lock")
            res_docker_log.unlink(missing_ok=True)
            try:
                subprocess.run(
                    [
                        "bash",
                        str(REPO_ROOT / "scripts/standalone/restore.sh"),
                        "--work-dir",
                        str(work_base),
                        "--backup-dir",
                        str(bak_stub),
                        *flag_args,
                    ],
                    env=res_env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                logged_r = res_docker_log.read_text(encoding="utf-8")
                assert "ENV_PROJ=UNSET|" in logged_r, f"restore.sh leaked shell COMPOSE_PROJECT_NAME: {logged_r!r}"
                assert f"compose -p {want_proj} " in logged_r, f"restore.sh did not use {want_proj!r}: {logged_r!r}"
                assert "proj-from-env" not in logged_r and "shell-poison-proj" not in logged_r
            finally:
                if lock_p.is_dir():
                    lock_p.rmdir()
                elif lock_p.exists():
                    lock_p.unlink()

    def test_b6_r9_fixes_pg_restore_sigpipe_override_endpoints_u2028_and_project_names(
        self, tmp_path: Path
    ) -> None:
        """Offline verification for Batch 6 R9 fixes (items 1-9):
        1. backup.sh passes `-f -` to `pg_restore --data-only` (stdout).
        2. backup.sh greps TOC temp file instead of `printf ... | grep -q` (no SIGPIPE skip).
        3. restore.sh keeps the same compose override file (`env_file: !override`) across
           both preflight `compose config` and formal writes when restoring from `env.enc`.
        5. Endpoint comparison lowercases only scheme & hostname while keeping path case-sensitive
           (`/Archive` != `/archive`).
        6. COPY SQL parser splits on `\\n` (not `splitlines()`) so filenames with U+2028 parse cleanly.
        7-8. Compose project name rule `^[a-z0-9][a-z0-9_-]*$` (trailing `-`/`_` allowed, `.env`
             lowercased before validation, `upgrade.sh` passes lowercased name to `backup.sh`,
             and `Dalton.Release` dir fallback produces `dalton-release` in all 3 scripts).
        9. Multiple non-empty other DBs are formatted with `, ` separator in `restore.sh`.
        """
        import re as _re

        spec = importlib.util.spec_from_file_location(
            "standalone_storage_config_r9",
            REPO_ROOT / "scripts/standalone/storage_config.py",
        )
        assert spec is not None and spec.loader is not None
        sc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sc)

        # Item 1 & Item 2: backup.sh uses `pg_restore -f - --data-only` and no `printf/echo ... | grep -q`
        backup_text = (REPO_ROOT / "scripts/standalone/backup.sh").read_text(encoding="utf-8")
        assert "pg_restore -f - --data-only --schema=public --table=dalton_chat_file_uploads" in backup_text
        for script_name in ("backup.sh", "restore.sh", "upgrade.sh"):
            src_text = (REPO_ROOT / "scripts/standalone" / script_name).read_text(encoding="utf-8")
            assert not _re.search(r"\b(?:printf|echo)\b[^\n|]*\|\s*grep\s+-[^\s]*q", src_text), (
                f"{script_name} still pipes printf/echo directly into grep -q (SIGPIPE risk under pipefail)"
            )

        # Item 5: Endpoint normalization lowercases only scheme + hostname, keeping path case-sensitive
        assert sc._norm_ep("https://S3.Example.Test/Archive/") == "https://s3.example.test/Archive"
        assert sc._norm_ep("https://s3.example.test/archive") == "https://s3.example.test/archive"
        assert sc._norm_ep("https://S3.Example.Test/Archive") != sc._norm_ep("https://s3.example.test/archive")
        assert sc._norm_ep("HTTPS://User:Pass@S3.Example.TEST:8443/Archive/") == "https://s3.example.test:8443/Archive"
        assert sc._norm_ep("http://user:pass@[2001:DB8::1]:9000/Archive") == "http://[2001:db8::1]:9000/Archive"

        backup_manifest_archive = {
            "targets": {
                "chat_upload": {
                    "endpoint": "https://s3.example.test/Archive",
                    "bucket": "chat-uploads",
                    "key_prefix": "",
                }
            }
        }
        target_lower_path = sc.resolve_effective_storage(
            {
                "DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL": "https://s3.example.test/archive",
                "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME": "chat-uploads",
                "DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID": "ak",
                "DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY": "sk",
            },
            apply_compose_defaults=False,
        )
        mismatches_case = sc.compare_backup_with_target(target_lower_path, backup_manifest_archive, None)
        assert any("endpoint mismatch" in m for m in mismatches_case), (
            f"Expected /Archive vs /archive to be reported as endpoint mismatch, got {mismatches_case!r}"
        )

        target_same_path_upper_host = sc.resolve_effective_storage(
            {
                "DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL": "HTTPS://S3.EXAMPLE.TEST/Archive/",
                "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME": "chat-uploads",
                "DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID": "ak",
                "DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY": "sk",
            },
            apply_compose_defaults=False,
        )
        assert sc.compare_backup_with_target(target_same_path_upper_host, backup_manifest_archive, None) == []

        # Also verify backup.sh embedded `_norm_ep` matches the same case-sensitive path rule
        m_ep = _re.search(r"(def _norm_ep\(url: str\) -> str:.*?    return raw\n)", backup_text, flags=_re.DOTALL)
        assert m_ep is not None, "Could not locate _norm_ep in backup.sh"
        ep_ns: dict[str, Any] = {}
        exec(m_ep.group(1), ep_ns)
        assert ep_ns["_norm_ep"]("https://S3.Example.Test/Archive") == "https://s3.example.test/Archive"
        assert ep_ns["_norm_ep"]("https://s3.example.test/archive") == "https://s3.example.test/archive"
        assert ep_ns["_norm_ep"]("https://S3.Example.Test/Archive") != ep_ns["_norm_ep"]("https://s3.example.test/archive")

        # Item 6: COPY SQL containing U+2028 (Line Separator) in filename / object_key parses without splitting rows
        u2028_name = "report\u2028q4.pdf"
        u2028_key = f"chat-uploads/ws1/u1/{u2028_name}"
        u2028_wip = "chat-uploads/ws1/u2/draft\u2028wip.pdf"
        copy_u2028 = (
            "COPY public.dalton_chat_file_uploads (upload_id, filename, object_key, state, cleaned_at) FROM stdin;\n"
            f"u1\t{u2028_name}\t{u2028_key}\tready\t\\N\n"
            f"u2\tdraft\u2028wip.pdf\t{u2028_wip}\tuploading\t\\N\n"
            "\\.\n"
        )
        parsed_rows = sc.parse_chat_uploads_copy_sql(copy_u2028)
        assert len(parsed_rows) == 2
        assert parsed_rows[0]["filename"] == u2028_name
        assert parsed_rows[0]["object_key"] == u2028_key
        assert parsed_rows[1]["state"] == "uploading"

        verify_res = sc.verify_backup_chat_upload_refs(
            copy_u2028,
            {
                "primary_bucket": "chat-uploads",
                "targets": {"chat_upload": {"bucket": "chat-uploads", "key_prefix": ""}},
                "objects": [{"bucket": "chat-uploads", "key": u2028_key, "size": 12}],
            },
        )
        assert verify_res["missing"] == []
        assert verify_res["checked"] == 1
        assert verify_res["skipped_uploading"] == 1

        # Item 3 (updated in r11): restore.sh from env.enc swaps .env in place before preflight
        # and uses the exact same regular compose args (`-p`, `-f`, `--env-file "$ENV_FILE"`)
        # across preflight & formal write with NO temporary compose override file.
        pass_f = tmp_path / "r9.pass"
        pass_f.write_text("r9-passphrase-123456\n", encoding="utf-8")
        pass_f.chmod(0o600)
        proj_ov = f"r9-ov-parity-{os.getpid()}"
        bak_ov = tmp_path / "r9-bak-ov"
        bak_ov.mkdir()
        (bak_ov / "postgres.dump").write_bytes(b"PGDMP")
        plain_ov = tmp_path / "r9-ov.plain.env"
        plain_ov.write_text(
            f"COMPOSE_PROJECT_NAME={proj_ov}\n"
            "DB_HOST=postgres\nDB_USER=postgres\nDB_PASSWORD=secretpg\nDB_NAME=postgres\n",
            encoding="utf-8",
        )
        subprocess.run(
            [
                "openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-md", "sha256", "-iter", "10000",
                "-salt", "-in", str(plain_ov), "-out", str(bak_ov / "env.enc"), "-pass", f"file:{pass_f}",
            ],
            check=True,
            capture_output=True,
            timeout=15,
        )
        (bak_ov / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "env_backup": "encrypted",
                    "env_encryption": {
                        "cipher": "aes-256-cbc", "kdf": "pbkdf2", "digest": "sha256", "iterations": 10000,
                    },
                }
            ),
            encoding="utf-8",
        )
        w_ov = tmp_path / "r9-work-ov"
        w_ov.mkdir()
        (w_ov / "docker-compose.yml").write_text(
            "services:\n  backend:\n    env_file: .env\n", encoding="utf-8"
        )
        (w_ov / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
        (w_ov / "agent_config.yaml").write_text('storage:\n  provider: "none"\n', encoding="utf-8")

        ov_stub_dir = tmp_path / "r9-ov-bin"
        ov_stub_dir.mkdir()
        (ov_stub_dir / "flock").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (ov_stub_dir / "flock").chmod(0o755)
        ov_calls_log = tmp_path / "r9-ov-calls.log"
        (ov_stub_dir / "docker").write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{ov_calls_log}"\n'
            'if [[ "$*" == *" config --format json"* ]]; then echo \'{"services":{"backend":{"environment":{}}}}\'; exit 0; fi\n'
            'if [[ "$*" == *" ps --status running --services"* ]]; then echo "postgres"; exit 0; fi\n'
            'if [[ "$*" == *" pg_isready "* ]]; then exit 0; fi\n'
            'if [[ "$*" == *" pg_restore --list"* ]]; then exit 0; fi\n'
            'if [[ "$*" == *"current_setting(\'is_superuser\')"* ]]; then echo "on"; exit 0; fi\n'
            'if [[ "$*" == *"FROM pg_database"* ]]; then echo "postgres"; exit 0; fi\n'
            'if [[ "$*" == *" -d postgres "* && "$*" == *"pg_class"* ]]; then printf "0\\t0\\t0\\n"; exit 0; fi\n'
            'if [[ "$*" == *" up -d postgres redis minio"* ]]; then exit 99; fi\n'
            "exit 0\n",
            encoding="utf-8",
        )
        (ov_stub_dir / "docker").chmod(0o755)
        ov_env = os.environ.copy()
        ov_env["PATH"] = f"{ov_stub_dir}:{ov_env.get('PATH', '')}"
        lock_ov = Path(f"/tmp/dalton-{proj_ov}.restore.lock")
        try:
            res_ov_parity = subprocess.run(
                [
                    "bash",
                    str(REPO_ROOT / "scripts/standalone/restore.sh"),
                    "--work-dir", str(w_ov),
                    "--backup-dir", str(bak_ov),
                    "--env-passphrase-file", str(pass_f),
                    "--skip-minio", "--skip-caddy", "--skip-edge-build",
                ],
                env=ov_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert res_ov_parity.returncode != 0
            assert not (w_ov / ".env").exists(), "Pre-write failure must remove newly placed .env"
            assert "已把 .env 恢复为恢复前的内容" in res_ov_parity.stderr
            calls = ov_calls_log.read_text(encoding="utf-8").splitlines()
            cfg_calls = [c for c in calls if " config --format json" in c]
            write_calls = [c for c in calls if " up -d postgres redis minio" in c]
            assert cfg_calls and write_calls, f"Missing expected compose calls: {calls}"
            assert all("dalton-restore-override" not in c for c in calls), (
                f"restore.sh must NOT generate any temporary compose override file: {calls}"
            )
            cfg_prefix = cfg_calls[0].split(" config --format json", 1)[0]
            write_prefix = write_calls[0].split(" up -d postgres redis minio", 1)[0]
            assert cfg_prefix == write_prefix, (
                f"Preflight and formal write used different compose args: {cfg_prefix!r} != {write_prefix!r}"
            )
            assert f"--env-file {w_ov / '.env'}" in cfg_prefix
        finally:
            if lock_ov.is_dir():
                lock_ov.rmdir()
            elif lock_ov.exists():
                lock_ov.unlink()

        # Item 7 & Item 8: trailing `-`/`_` accepted, uppercase `.env` lowercased & forwarded by upgrade.sh,
        # invalid `.env` project rejected, and `Dalton.Release` dir fallback -> `dalton-release` in all 3 scripts.
        r9_bin = tmp_path / "r9-proj-bin"
        r9_bin.mkdir()
        (r9_bin / "flock").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (r9_bin / "flock").chmod(0o755)
        r9_docker_log = tmp_path / "r9-proj-docker.log"
        (r9_bin / "docker").write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" > "{r9_docker_log}"\n'
            "exit 99\n",
            encoding="utf-8",
        )
        (r9_bin / "docker").chmod(0o755)
        r9_env = os.environ.copy()
        r9_env["PATH"] = f"{r9_bin}:{r9_env.get('PATH', '')}"

        bak_stub = tmp_path / "r9-bak-stub"
        bak_stub.mkdir()
        (bak_stub / "postgres.dump").write_bytes(b"PGDMP")

        # 7a. Trailing `-` and `_` accepted by --project-name and .env across backup.sh, restore.sh, upgrade.sh
        trail_suffix = uuid.uuid4().hex[:12]
        trail_env_proj = f"Dalton_Core_{trail_suffix}-"
        want_trail_proj = f"dalton_core_{trail_suffix}-"
        w_trail = tmp_path / "r9-work-trail"
        w_trail.mkdir()
        (w_trail / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (w_trail / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
        (w_trail / ".env").write_text(f"COMPOSE_PROJECT_NAME={trail_env_proj}\nDB_HOST=postgres\n", encoding="utf-8")

        r9_docker_log.unlink(missing_ok=True)
        subprocess.run(
            ["bash", str(REPO_ROOT / "scripts/standalone/backup.sh"), "--work-dir", str(w_trail)],
            env=r9_env, capture_output=True, text=True, timeout=15,
        )
        assert f"compose -p {want_trail_proj} " in r9_docker_log.read_text(encoding="utf-8")

        r9_docker_log.unlink(missing_ok=True)
        subprocess.run(
            ["bash", str(REPO_ROOT / "scripts/standalone/backup.sh"), "--work-dir", str(w_trail), "--project-name", "dalton_"],
            env=r9_env, capture_output=True, text=True, timeout=15,
        )
        assert "compose -p dalton_ " in r9_docker_log.read_text(encoding="utf-8")

        lock_trail = Path(f"/tmp/dalton-{want_trail_proj}.restore.lock")
        lock_trail_existed = lock_trail.exists()
        r9_docker_log.unlink(missing_ok=True)
        try:
            subprocess.run(
                ["bash", str(REPO_ROOT / "scripts/standalone/restore.sh"), "--work-dir", str(w_trail), "--backup-dir", str(bak_stub)],
                env=r9_env, capture_output=True, text=True, timeout=15,
            )
            assert f"compose -p {want_trail_proj} " in r9_docker_log.read_text(encoding="utf-8")
        finally:
            if not lock_trail_existed:
                if lock_trail.is_dir():
                    lock_trail.rmdir()
                elif lock_trail.exists():
                    lock_trail.unlink()

        # 7b. upgrade.sh lowercases `.env` COMPOSE_PROJECT_NAME (`Dalton_Core_<uuid>-`) before passing `--project-name` to backup.sh
        rel_r9 = tmp_path / "r9-rel"
        (rel_r9 / "scripts/standalone").mkdir(parents=True)
        (rel_r9 / "deploy/standalone").mkdir(parents=True)
        for req in (
            "docker-compose.yml", "docker-compose.core.yml",
            "scripts/standalone/migrate.sh", "scripts/standalone/restore.sh", "deploy/standalone/Caddyfile",
        ):
            (rel_r9 / req).write_text("stub\n", encoding="utf-8")
        upg_bak_args = tmp_path / "r9-upg-bak-args.txt"
        (rel_r9 / "scripts/standalone/backup.sh").write_text(
            f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" > "{upg_bak_args}"\nexit 1\n',
            encoding="utf-8",
        )
        subprocess.run(
            [
                "bash", str(REPO_ROOT / "scripts/standalone/upgrade.sh"),
                "--work-dir", str(w_trail), "--release-dir", str(rel_r9),
            ],
            env=r9_env, capture_output=True, text=True, timeout=15,
        )
        assert f"--project-name {want_trail_proj}" in upg_bak_args.read_text(encoding="utf-8")

        # 7c. Invalid `.env` COMPOSE_PROJECT_NAME (e.g. `Bad.Dot`) rejected with exit 2 by all 3 scripts
        w_bad_env = tmp_path / "r9-work-bad-env"
        w_bad_env.mkdir()
        (w_bad_env / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (w_bad_env / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
        (w_bad_env / ".env").write_text("COMPOSE_PROJECT_NAME=Bad.Dot\nDB_HOST=postgres\n", encoding="utf-8")
        for s_name, s_args in (
            ("backup.sh", ["--work-dir", str(w_bad_env)]),
            ("restore.sh", ["--work-dir", str(w_bad_env), "--backup-dir", str(bak_stub)]),
            ("upgrade.sh", ["--work-dir", str(w_bad_env), "--release-dir", str(rel_r9)]),
        ):
            res_bad_env = subprocess.run(
                ["bash", str(REPO_ROOT / "scripts/standalone" / s_name), *s_args],
                env=r9_env, capture_output=True, text=True, timeout=15,
            )
            assert res_bad_env.returncode == 2, f"{s_name} should reject Bad.Dot in .env with exit 2: {res_bad_env.stderr}"

        # Item 8: Directory name fallback `Dalton.Release-<uuid>` -> `dalton-release-<uuid>` across all 3 scripts
        # (uses a unique random suffix and only removes a lock file created by this test).
        dir_suffix = uuid.uuid4().hex[:12]
        w_dir_rel = tmp_path / f"Dalton.Release-{dir_suffix}"
        want_dir_proj = f"dalton-release-{dir_suffix}"
        w_dir_rel.mkdir()
        (w_dir_rel / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (w_dir_rel / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
        (w_dir_rel / ".env").write_text("DB_HOST=postgres\n", encoding="utf-8")

        r9_docker_log.unlink(missing_ok=True)
        subprocess.run(
            ["bash", str(REPO_ROOT / "scripts/standalone/backup.sh"), "--work-dir", str(w_dir_rel)],
            env=r9_env, capture_output=True, text=True, timeout=15,
        )
        assert f"compose -p {want_dir_proj} " in r9_docker_log.read_text(encoding="utf-8")

        lock_dir_rel = Path(f"/tmp/dalton-{want_dir_proj}.restore.lock")
        lock_dir_rel_existed = lock_dir_rel.exists()
        r9_docker_log.unlink(missing_ok=True)
        try:
            subprocess.run(
                ["bash", str(REPO_ROOT / "scripts/standalone/restore.sh"), "--work-dir", str(w_dir_rel), "--backup-dir", str(bak_stub)],
                env=r9_env, capture_output=True, text=True, timeout=15,
            )
            assert f"compose -p {want_dir_proj} " in r9_docker_log.read_text(encoding="utf-8")
        finally:
            if not lock_dir_rel_existed:
                if lock_dir_rel.is_dir():
                    lock_dir_rel.rmdir()
                elif lock_dir_rel.exists():
                    lock_dir_rel.unlink()

        upg_bak_args.unlink(missing_ok=True)
        subprocess.run(
            [
                "bash", str(REPO_ROOT / "scripts/standalone/upgrade.sh"),
                "--work-dir", str(w_dir_rel), "--release-dir", str(rel_r9),
            ],
            env=r9_env, capture_output=True, text=True, timeout=15,
        )
        assert f"--project-name {want_dir_proj}" in upg_bak_args.read_text(encoding="utf-8")

        # Item 9: Multiple non-empty other databases in restore.sh are joined with `, ` (comma + space)
        proj_multi_db = f"r9-multi-db-{os.getpid()}"
        w_mdb = tmp_path / "r9-work-mdb"
        w_mdb.mkdir()
        (w_mdb / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (w_mdb / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
        (w_mdb / "agent_config.yaml").write_text('storage:\n  provider: "none"\n', encoding="utf-8")
        (w_mdb / ".env").write_text(
            f"COMPOSE_PROJECT_NAME={proj_multi_db}\nDB_HOST=postgres\nDB_USER=postgres\nDB_PASSWORD=pg\nDB_NAME=postgres\n",
            encoding="utf-8",
        )
        mdb_bin = tmp_path / "r9-mdb-bin"
        mdb_bin.mkdir()
        (mdb_bin / "flock").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (mdb_bin / "flock").chmod(0o755)
        (mdb_bin / "docker").write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$*" == *" config --format json"* ]]; then echo \'{"services":{"backend":{"environment":{}}}}\'; exit 0; fi\n'
            'if [[ "$*" == *" ps --status running --services"* ]]; then echo "postgres"; exit 0; fi\n'
            'if [[ "$*" == *" pg_isready "* ]]; then exit 0; fi\n'
            'if [[ "$*" == *" pg_restore --list"* ]]; then exit 0; fi\n'
            'if [[ "$*" == *"current_setting(\'is_superuser\')"* ]]; then echo "on"; exit 0; fi\n'
            'if [[ "$*" == *"FROM pg_database"* ]]; then printf "analytics\\nbilling\\npostgres\\n"; exit 0; fi\n'
            'if [[ "$*" == *" -d analytics "* ]]; then printf "1\\t0\\t0\\n"; exit 0; fi\n'
            'if [[ "$*" == *" -d billing "* ]]; then printf "2\\t0\\t0\\n"; exit 0; fi\n'
            'if [[ "$*" == *" -d postgres "* ]]; then printf "0\\t0\\t0\\n"; exit 0; fi\n'
            "exit 0\n",
            encoding="utf-8",
        )
        (mdb_bin / "docker").chmod(0o755)
        mdb_env = os.environ.copy()
        mdb_env["PATH"] = f"{mdb_bin}:{mdb_env.get('PATH', '')}"
        lock_mdb = Path(f"/tmp/dalton-{proj_multi_db}.restore.lock")
        try:
            res_mdb = subprocess.run(
                [
                    "bash", str(REPO_ROOT / "scripts/standalone/restore.sh"),
                    "--work-dir", str(w_mdb), "--backup-dir", str(bak_stub),
                ],
                env=mdb_env, capture_output=True, text=True, timeout=15,
            )
            assert res_mdb.returncode != 0
            assert "（analytics, billing）" in res_mdb.stderr, (
                f"Expected comma+space joined DB list in stderr, got:\n{res_mdb.stderr}"
            )
            assert "down -v" not in res_mdb.stderr
        finally:
            if lock_mdb.is_dir():
                lock_mdb.rmdir()
            elif lock_mdb.exists():
                lock_mdb.unlink()

    def test_b6_r10_fixes_env_file_override_preservation_and_manual_db_check(
        self, tmp_path: Path
    ) -> None:
        """Offline verification for Batch 6 R10/R11 fixes:
        1. When restoring from env.enc, an extra compose file adding `storage.env`
           with a mismatched key prefix is refused BEFORE writing any data, using
           regular compose args (no temporary compose override), and `.env` is rolled
           back to its exact pre-restore state (removed if absent before, or restored
           byte-for-byte if present before with `--replace-env`).
        2. The database check loop in `STANDALONE-DEPLOY.md` uses `set -eu` + explicit
           status/digit checks (no pipeline exit-code masking), prints the DB name and
           `检查失败，先不要删除卷`, and exits non-zero when a query fails or returns
           empty/non-digit output.
        """
        import re as _re
        import tarfile

        # --- Item 1: Extra compose file adding `storage.env` with mismatched key prefix ---
        pass_f = tmp_path / "r10.pass"
        pass_f.write_text("r10-passphrase-987654\n", encoding="utf-8")
        pass_f.chmod(0o600)

        proj_r10 = f"r10-storage-env-{uuid.uuid4().hex[:10]}"
        bak_r10 = tmp_path / "r10-bak"
        bak_r10.mkdir()
        (bak_r10 / "postgres.dump").write_bytes(b"PGDMP")

        plain_env = tmp_path / "r10.plain.env"
        plain_env.write_text(
            f"COMPOSE_PROJECT_NAME={proj_r10}\n"
            "DB_HOST=postgres\n"
            "DB_USER=postgres\n"
            "DB_PASSWORD=secretpg\n"
            "DB_NAME=postgres\n"
            "MINIO_ROOT_USER=minioadmin\n"
            "MINIO_ROOT_PASSWORD=miniopass\n"
            "DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL=http://minio:9000\n"
            "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME=chat-uploads\n"
            "DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID=minioadmin\n"
            "DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY=miniopass\n"
            "DALTON_CHAT_UPLOAD_STORAGE_KEY_PREFIX=tenant-a\n",
            encoding="utf-8",
        )
        subprocess.run(
            [
                "openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-md", "sha256", "-iter", "10000",
                "-salt", "-in", str(plain_env), "-out", str(bak_r10 / "env.enc"), "-pass", f"file:{pass_f}",
            ],
            check=True,
            capture_output=True,
            timeout=15,
        )
        minio_manifest = {
            "primary_bucket": "chat-uploads",
            "primary_key_prefix": "tenant-a",
            "buckets": ["chat-uploads"],
            "targets": {
                "chat_upload": {
                    "service_type": "s3",
                    "service_id": "s3:http://minio:9000",
                    "endpoint": "http://minio:9000",
                    "bucket": "chat-uploads",
                    "key_prefix": "tenant-a",
                }
            },
            "objects": [],
        }
        mm_path = tmp_path / "_minio_manifest.json"
        mm_path.write_text(json.dumps(minio_manifest), encoding="utf-8")
        with tarfile.open(bak_r10 / "minio-objects.tar.gz", "w:gz") as tf:
            tf.add(mm_path, arcname="_minio_manifest.json")
        (bak_r10 / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "env_backup": "encrypted",
                    "env_encryption": {
                        "cipher": "aes-256-cbc",
                        "kdf": "pbkdf2",
                        "digest": "sha256",
                        "iterations": 10000,
                    },
                    "minio": {
                        "primary_bucket": "chat-uploads",
                        "primary_key_prefix": "tenant-a",
                        "targets": minio_manifest["targets"],
                    },
                }
            ),
            encoding="utf-8",
        )

        work_r10 = tmp_path / "r10-work"
        work_r10.mkdir()
        (work_r10 / "docker-compose.yml").write_text(
            "services:\n  backend:\n    env_file: .env\n", encoding="utf-8"
        )
        (work_r10 / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
        (work_r10 / "docker-compose.extra.yml").write_text(
            "services:\n  backend:\n    env_file:\n      - storage.env\n", encoding="utf-8"
        )
        (work_r10 / "storage.env").write_text(
            "DALTON_CHAT_UPLOAD_STORAGE_KEY_PREFIX=tenant-b\n", encoding="utf-8"
        )
        (work_r10 / "agent_config.yaml").write_text('storage:\n  provider: "none"\n', encoding="utf-8")

        # Stub `docker` that merges `-f` compose files and loads every entry in `backend`'s
        # `env_file` list in exact order relative to `--project-directory`.
        stub_bin = tmp_path / "r10-bin"
        stub_bin.mkdir()
        (stub_bin / "flock").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (stub_bin / "flock").chmod(0o755)
        captured_calls = tmp_path / "r10-docker-calls.log"
        stub_compose_py = tmp_path / "r10_stub_compose.py"
        stub_compose_py.write_text(
            "import json, os, sys, yaml\n"
            "args = sys.argv[1:]\n"
            "files = []\n"
            "proj_dir = os.getcwd()\n"
            "i = 0\n"
            "while i < len(args):\n"
            "    if args[i] == '-f' and i + 1 < len(args):\n"
            "        files.append(args[i + 1]); i += 2\n"
            "    elif args[i] == '--project-directory' and i + 1 < len(args):\n"
            "        proj_dir = args[i + 1]; i += 2\n"
            "    else:\n"
            "        i += 1\n"
            "env_files = []\n"
            "for f in files:\n"
            "    doc = yaml.safe_load(open(f, encoding='utf-8')) or {}\n"
            "    b = (doc.get('services') or {}).get('backend') or {}\n"
            "    if 'env_file' in b:\n"
            "        ef = b['env_file']\n"
            "        items = [ef] if isinstance(ef, str) else list(ef or [])\n"
            "        env_files.extend(items)\n"
            "merged_env = {}\n"
            "for entry in env_files:\n"
            "    p = entry['path'] if isinstance(entry, dict) else entry\n"
            "    full = p if os.path.isabs(p) else os.path.join(proj_dir, p)\n"
            "    for raw in open(full, encoding='utf-8'):\n"
            "        s = raw.strip()\n"
            "        if not s or s.startswith('#') or '=' not in s: continue\n"
            "        k, v = s.split('=', 1)\n"
            "        merged_env[k.strip()] = v.strip()\n"
            "print(json.dumps({'services': {'backend': {'environment': merged_env}}}))\n",
            encoding="utf-8",
        )
        (stub_bin / "docker").write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{captured_calls}"\n'
            'if [[ "$*" == *" config --format json"* ]]; then\n'
            f'  "{sys.executable}" "{stub_compose_py}" "$@"\n'
            "  exit $?\n"
            "fi\n"
            "exit 99\n",
            encoding="utf-8",
        )
        (stub_bin / "docker").chmod(0o755)

        r10_env = os.environ.copy()
        r10_env["PATH"] = f"{stub_bin}:{r10_env.get('PATH', '')}"
        lock_r10 = Path(f"/tmp/dalton-{proj_r10}.restore.lock")
        lock_r10_existed = lock_r10.exists()
        try:
            # 1a. Without pre-existing .env: refused before writing data and newly placed .env is deleted
            res_mismatch = subprocess.run(
                [
                    "bash",
                    str(REPO_ROOT / "scripts/standalone/restore.sh"),
                    "--work-dir", str(work_r10),
                    "--backup-dir", str(bak_r10),
                    "--compose-files", "docker-compose.yml:docker-compose.core.yml:docker-compose.extra.yml",
                    "--env-passphrase-file", str(pass_f),
                    "--skip-caddy", "--skip-edge-build",
                ],
                env=r10_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert res_mismatch.returncode != 0, (
                f"Expected restore.sh to refuse mismatched key_prefix from storage.env, got rc=0:\n{res_mismatch.stdout}"
            )
            assert "tenant-b" in res_mismatch.stderr and "key_prefix" in res_mismatch.stderr.lower(), (
                f"Expected key_prefix mismatch mentioning tenant-b in stderr, got:\n{res_mismatch.stderr}"
            )
            assert "已把 .env 恢复为恢复前的内容" in res_mismatch.stderr
            assert not (work_r10 / ".env").exists(), (
                "restore.sh must remove newly placed .env when refused before writing data!"
            )
            calls_made = captured_calls.read_text(encoding="utf-8").splitlines()
            assert all(" up -d" not in c for c in calls_made), (
                f"restore.sh must refuse BEFORE starting/mutating containers, calls={calls_made}"
            )
            assert all("dalton-restore-override" not in c for c in calls_made), (
                f"restore.sh must NOT pass any temporary compose override file, calls={calls_made}"
            )

            # 1b. With existing .env + --replace-env: refused before writing data and .env is restored byte-for-byte
            orig_env_before = (
                f"COMPOSE_PROJECT_NAME={proj_r10}\n"
                "DB_HOST=postgres\n"
                "SENTINEL=original-before-restore\n"
                "DALTON_CHAT_UPLOAD_STORAGE_KEY_PREFIX=tenant-orig\n"
            )
            (work_r10 / ".env").write_text(orig_env_before, encoding="utf-8")
            (work_r10 / ".env").chmod(0o600)
            captured_calls.unlink(missing_ok=True)
            res_mismatch_existing = subprocess.run(
                [
                    "bash",
                    str(REPO_ROOT / "scripts/standalone/restore.sh"),
                    "--work-dir", str(work_r10),
                    "--backup-dir", str(bak_r10),
                    "--compose-files", "docker-compose.yml:docker-compose.core.yml:docker-compose.extra.yml",
                    "--env-passphrase-file", str(pass_f),
                    "--replace-env",
                    "--skip-caddy", "--skip-edge-build",
                ],
                env=r10_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert res_mismatch_existing.returncode != 0
            assert "tenant-b" in res_mismatch_existing.stderr and "key_prefix" in res_mismatch_existing.stderr.lower()
            assert "已把 .env 恢复为恢复前的内容" in res_mismatch_existing.stderr
            assert (work_r10 / ".env").read_text(encoding="utf-8") == orig_env_before, (
                "restore.sh must restore .env byte-for-byte to pre-restore content on pre-write refusal!"
            )
            assert not list(work_r10.glob(".env.pre-restore-*")), (
                "Pre-restore .env backup must be moved back into .env on pre-write failure"
            )
            calls_made_2 = captured_calls.read_text(encoding="utf-8").splitlines()
            assert all(" up -d" not in c for c in calls_made_2)
            assert all("dalton-restore-override" not in c for c in calls_made_2)
        finally:
            if not lock_r10_existed:
                if lock_r10.is_dir():
                    lock_r10.rmdir()
                elif lock_r10.exists():
                    lock_r10.unlink()

        # --- Item 3: Manual database check loop in STANDALONE-DEPLOY.md fails loudly on query error / non-digit ---
        doc_text = (REPO_ROOT / "docs/dalton/dev-guide/STANDALONE-DEPLOY.md").read_text(encoding="utf-8")
        m_doc_block = _re.search(
            r"```bash\n(\s*cd /opt/dalton\n\s*# 若修改过 DB_USER.*?)\n\s*```",
            doc_text,
            flags=_re.DOTALL,
        )
        assert m_doc_block is not None, "Could not locate DB check bash snippet in STANDALONE-DEPLOY.md"
        # Dedent markdown list indentation (3 spaces) and swap `cd /opt/dalton` to tmp_path
        raw_lines = [ln[3:] if ln.startswith("   ") else ln for ln in m_doc_block.group(1).splitlines()]
        snippet_body = "\n".join(raw_lines).replace("cd /opt/dalton", f'cd "{tmp_path}"', 1)
        assert "set -eu" in snippet_body, "Manual DB check snippet must enable set -eu"

        doc_bin = tmp_path / "r10-doc-bin"
        doc_bin.mkdir()
        doc_docker = doc_bin / "docker"

        def _run_doc_snippet(docker_body: str) -> subprocess.CompletedProcess[str]:
            doc_docker.write_text("#!/usr/bin/env bash\n" + docker_body, encoding="utf-8")
            doc_docker.chmod(0o755)
            d_env = os.environ.copy()
            d_env["PATH"] = f"{doc_bin}:{d_env.get('PATH', '')}"
            d_env["PROJECT"] = "dalton-doc-check"
            return subprocess.run(
                ["bash", "-c", snippet_body],
                env=d_env,
                capture_output=True,
                text=True,
                timeout=15,
            )

        # 3a. Normal case: both DBs return pure digits -> exits 0
        res_ok = _run_doc_snippet(
            'if [[ "$*" == *"FROM pg_database"* ]]; then printf "analytics\\npostgres\\n"; exit 0; fi\n'
            'if [[ "$*" == *" -d analytics "* ]]; then printf "0\\r\\n"; exit 0; fi\n'
            'if [[ "$*" == *" -d postgres "* ]]; then printf "12\\n"; exit 0; fi\n'
            "exit 1\n"
        )
        assert res_ok.returncode == 0, f"Expected exit 0 on valid counts, got {res_ok.returncode}: {res_ok.stderr}"
        assert "analytics\tuser_objects=0" in res_ok.stdout
        assert "postgres\tuser_objects=12" in res_ok.stdout

        # 3b. Query failure on `analytics` -> exits non-zero, prints DB name + warning
        res_fail = _run_doc_snippet(
            'if [[ "$*" == *"FROM pg_database"* ]]; then printf "analytics\\npostgres\\n"; exit 0; fi\n'
            'if [[ "$*" == *" -d analytics "* ]]; then echo "psql: FATAL: permission denied" >&2; exit 2; fi\n'
            'if [[ "$*" == *" -d postgres "* ]]; then printf "0\\n"; exit 0; fi\n'
            "exit 1\n"
        )
        assert res_fail.returncode != 0, f"Expected non-zero exit when psql fails on analytics: {res_fail.stdout}"
        assert "analytics" in res_fail.stderr and "检查失败，先不要删除卷" in res_fail.stderr, (
            f"Expected analytics + warning in stderr, got: {res_fail.stderr!r}"
        )
        assert "postgres\tuser_objects=" not in res_fail.stdout, "Loop must stop immediately on failure"

        # 3c. Non-digit / empty / multi-line / embedded-CR output on `analytics` -> exits non-zero, prints DB name + warning
        for bad_out in ("", "not-a-number", "0\\n1", "1\\r2", "1\\r\\r"):
            res_bad = _run_doc_snippet(
                'if [[ "$*" == *"FROM pg_database"* ]]; then printf "analytics\\npostgres\\n"; exit 0; fi\n'
                f'if [[ "$*" == *" -d analytics "* ]]; then printf "{bad_out}\\n"; exit 0; fi\n'
                'if [[ "$*" == *" -d postgres "* ]]; then printf "0\\n"; exit 0; fi\n'
                "exit 1\n"
            )
            assert res_bad.returncode != 0, f"Expected non-zero exit for bad output {bad_out!r}"
            assert "analytics" in res_bad.stderr and "检查失败，先不要删除卷" in res_bad.stderr, (
                f"Expected analytics + warning for {bad_out!r}, got: {res_bad.stderr!r}"
            )

        # 3d. Outer DB-list query failure -> exits non-zero + warning
        res_list_fail = _run_doc_snippet('echo "psql: cannot connect" >&2; exit 2\n')
        assert res_list_fail.returncode != 0
        assert "检查失败，先不要删除卷" in res_list_fail.stderr

    def test_b6_r11_env_swap_rollback_and_pre_restore_backup(
        self, tmp_path: Path
    ) -> None:
        """Offline verification for Batch 6 R11:
        1. Post-write failure after MUTATION_STARTED=1 with existing .env + --replace-env
           keeps the new .env (mode 0600), preserves .env.pre-restore-<ts> (mode 0600,
           original content), and reports the backup path in stderr without rolling back.
        2. Successful restore with existing .env + --replace-env keeps .env.pre-restore-<ts>
           (mode 0600, original content), prints its path in stdout with a manual cleanup
           hint, and runs every docker compose command with identical regular args.
        3. Restoring with existing .env (without env.enc / --replace-env) creates no
           .env.pre-restore-* backup and leaves .env untouched.
        """
        pass_f = tmp_path / "r11.pass"
        pass_f.write_text("r11-passphrase-123456\n", encoding="utf-8")
        pass_f.chmod(0o600)

        def _make_r11_backup(name: str, proj: str, backup_env_text: str) -> Path:
            bdir = tmp_path / name
            bdir.mkdir()
            (bdir / "postgres.dump").write_bytes(b"PGDMP")
            plain = tmp_path / f"{name}.plain.env"
            plain.write_text(backup_env_text, encoding="utf-8")
            subprocess.run(
                [
                    "openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-md", "sha256", "-iter", "10000",
                    "-salt", "-in", str(plain), "-out", str(bdir / "env.enc"), "-pass", f"file:{pass_f}",
                ],
                check=True,
                capture_output=True,
                timeout=15,
            )
            (bdir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "compose_project": proj,
                        "env_backup": "encrypted",
                        "env_encryption": {
                            "cipher": "aes-256-cbc",
                            "kdf": "pbkdf2",
                            "digest": "sha256",
                            "iterations": 10000,
                        },
                    }
                ),
                encoding="utf-8",
            )
            return bdir

        # --- Case 1: Post-write failure keeps new .env and reports .env.pre-restore-<ts> ---
        proj_post = f"r11-post-fail-{uuid.uuid4().hex[:10]}"
        orig_env_post = (
            f"COMPOSE_PROJECT_NAME={proj_post}\n"
            "DB_HOST=postgres\nDB_USER=postgres\nDB_PASSWORD=oldpg\nDB_NAME=postgres\n"
            "SENTINEL=original-before-post-fail\n"
        )
        new_env_post = (
            f"COMPOSE_PROJECT_NAME={proj_post}\n"
            "DB_HOST=postgres\nDB_USER=postgres\nDB_PASSWORD=newpg\nDB_NAME=postgres\n"
            "SENTINEL=restored-from-backup\n"
        )
        bak_post = _make_r11_backup("r11-bak-post", proj_post, new_env_post)
        w_post = tmp_path / "r11-work-post"
        w_post.mkdir()
        (w_post / "docker-compose.yml").write_text("services:\n  backend:\n    env_file: .env\n", encoding="utf-8")
        (w_post / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
        (w_post / "agent_config.yaml").write_text('storage:\n  provider: "none"\n', encoding="utf-8")
        (w_post / ".env").write_text(orig_env_post, encoding="utf-8")
        (w_post / ".env").chmod(0o640)

        bin_post = tmp_path / "r11-bin-post"
        bin_post.mkdir()
        (bin_post / "flock").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (bin_post / "flock").chmod(0o755)
        (bin_post / "docker").write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$*" == *" config --format json"* ]]; then echo \'{"services":{"backend":{"environment":{}}}}\'; exit 0; fi\n'
            'if [[ "$*" == *" ps --status running --services"* ]]; then echo "postgres"; exit 0; fi\n'
            'if [[ "$*" == *" pg_isready "* ]]; then exit 0; fi\n'
            'if [[ "$*" == *" pg_restore --list"* ]]; then exit 0; fi\n'
            'if [[ "$*" == *"current_setting(\'is_superuser\')"* ]]; then echo "on"; exit 0; fi\n'
            'if [[ "$*" == *"FROM pg_database"* ]]; then echo "postgres"; exit 0; fi\n'
            'if [[ "$*" == *" -d postgres "* && "$*" == *"pg_class"* ]]; then printf "0\\t0\\t0\\n"; exit 0; fi\n'
            'if [[ "$*" == *" pg_restore "* ]]; then echo "simulated post-write pg_restore failure" >&2; exit 1; fi\n'
            "exit 0\n",
            encoding="utf-8",
        )
        (bin_post / "docker").chmod(0o755)
        env_post = os.environ.copy()
        env_post["PATH"] = f"{bin_post}:{env_post.get('PATH', '')}"
        lock_post = Path(f"/tmp/dalton-{proj_post}.restore.lock")
        try:
            res_post = subprocess.run(
                [
                    "bash", str(REPO_ROOT / "scripts/standalone/restore.sh"),
                    "--work-dir", str(w_post),
                    "--backup-dir", str(bak_post),
                    "--env-passphrase-file", str(pass_f),
                    "--replace-env",
                    "--skip-minio", "--skip-caddy", "--skip-edge-build",
                ],
                env=env_post,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert res_post.returncode != 0
            assert "已把 .env 恢复为恢复前的内容" not in res_post.stderr
            assert (w_post / ".env").read_text(encoding="utf-8") == new_env_post
            assert stat.S_IMODE((w_post / ".env").stat().st_mode) == 0o600
            pre_baks = list(w_post.glob(".env.pre-restore-*"))
            assert len(pre_baks) == 1, f"Expected 1 .env.pre-restore-* backup, got {pre_baks}"
            assert pre_baks[0].read_text(encoding="utf-8") == orig_env_post
            assert stat.S_IMODE(pre_baks[0].stat().st_mode) == 0o600
            assert str(pre_baks[0]) in res_post.stderr, (
                f"Post-write failure must print pre-restore .env backup path in stderr:\n{res_post.stderr}"
            )
        finally:
            if lock_post.is_dir():
                lock_post.rmdir()
            elif lock_post.exists():
                lock_post.unlink()

        # --- Case 2: Successful restore retains .env.pre-restore-<ts> and uses identical compose args ---
        proj_ok = f"r11-ok-{uuid.uuid4().hex[:10]}"
        orig_env_ok = (
            f"COMPOSE_PROJECT_NAME={proj_ok}\n"
            "DB_HOST=postgres\nDB_USER=postgres\nDB_PASSWORD=oldpg\nDB_NAME=postgres\n"
            "SENTINEL=original-before-ok\n"
        )
        new_env_ok = (
            f"COMPOSE_PROJECT_NAME={proj_ok}\n"
            "DB_HOST=postgres\nDB_USER=postgres\nDB_PASSWORD=newpg\nDB_NAME=postgres\n"
            "DALTON_TLS_MODE=loopback\nDALTON_EDGE_PORT=8088\n"
            "SENTINEL=restored-ok\n"
        )
        bak_ok = _make_r11_backup("r11-bak-ok", proj_ok, new_env_ok)
        w_ok = tmp_path / "r11-work-ok"
        w_ok.mkdir()
        (w_ok / "docker-compose.yml").write_text("services:\n  backend:\n    env_file: .env\n", encoding="utf-8")
        (w_ok / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
        (w_ok / "agent_config.yaml").write_text('storage:\n  provider: "none"\n', encoding="utf-8")
        (w_ok / ".env").write_text(orig_env_ok, encoding="utf-8")
        (w_ok / ".env").chmod(0o640)

        bin_ok = tmp_path / "r11-bin-ok"
        bin_ok.mkdir()
        (bin_ok / "flock").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (bin_ok / "flock").chmod(0o755)
        (bin_ok / "curl").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (bin_ok / "curl").chmod(0o755)
        ok_calls_log = tmp_path / "r11-ok-calls.log"
        (bin_ok / "docker").write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{ok_calls_log}"\n'
            'if [[ "$*" == *" config --format json"* ]]; then echo \'{"services":{"backend":{"environment":{}}}}\'; exit 0; fi\n'
            'if [[ "$*" == *" ps --status running --services"* ]]; then printf "postgres\\nedge\\n"; exit 0; fi\n'
            'if [[ "$*" == *" pg_isready "* ]]; then exit 0; fi\n'
            'if [[ "$*" == *" pg_restore --list"* ]]; then exit 0; fi\n'
            'if [[ "$*" == *"current_setting(\'is_superuser\')"* ]]; then echo "on"; exit 0; fi\n'
            'if [[ "$*" == *"FROM pg_database"* ]]; then echo "postgres"; exit 0; fi\n'
            'if [[ "$*" == *" -d postgres "* && "$*" == *"pg_class"* ]]; then printf "0\\t0\\t0\\n"; exit 0; fi\n'
            'if [[ "$*" == *" port edge 8088"* ]]; then echo "127.0.0.1:8088"; exit 0; fi\n'
            'if [[ "$*" == *"alembic_version_dalton"* ]]; then echo "dalton_head"; exit 0; fi\n'
            'if [[ "$*" == *"alembic_version"* ]]; then echo "up_head"; exit 0; fi\n'
            "exit 0\n",
            encoding="utf-8",
        )
        (bin_ok / "docker").chmod(0o755)
        env_ok = os.environ.copy()
        env_ok["PATH"] = f"{bin_ok}:{env_ok.get('PATH', '')}"
        lock_ok = Path(f"/tmp/dalton-{proj_ok}.restore.lock")
        try:
            res_ok = subprocess.run(
                [
                    "bash", str(REPO_ROOT / "scripts/standalone/restore.sh"),
                    "--work-dir", str(w_ok),
                    "--backup-dir", str(bak_ok),
                    "--env-passphrase-file", str(pass_f),
                    "--replace-env",
                    "--skip-minio", "--skip-caddy", "--skip-edge-build",
                ],
                env=env_ok,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert res_ok.returncode == 0, f"Expected restore to succeed, stderr:\n{res_ok.stderr}"
            assert (w_ok / ".env").read_text(encoding="utf-8") == new_env_ok
            assert stat.S_IMODE((w_ok / ".env").stat().st_mode) == 0o600
            pre_baks_ok = list(w_ok.glob(".env.pre-restore-*"))
            assert len(pre_baks_ok) == 1, f"Expected retained .env.pre-restore-* backup, got {pre_baks_ok}"
            assert pre_baks_ok[0].read_text(encoding="utf-8") == orig_env_ok
            assert stat.S_IMODE(pre_baks_ok[0].stat().st_mode) == 0o600
            assert str(pre_baks_ok[0]) in res_ok.stdout, (
                f"Success output must print retained .env.pre-restore-* path:\n{res_ok.stdout}"
            )
            assert "自行删除" in res_ok.stdout
            all_calls = ok_calls_log.read_text(encoding="utf-8").splitlines()
            expected_prefix = (
                f"compose -p {proj_ok} "
                f"-f {w_ok / 'docker-compose.yml'} -f {w_ok / 'docker-compose.core.yml'} "
                f"--env-file {w_ok / '.env'} --project-directory {w_ok} "
            )
            assert all_calls and all(c.startswith(expected_prefix) for c in all_calls), (
                f"All docker compose calls must share exact regular prefix {expected_prefix!r}:\n{all_calls}"
            )
        finally:
            if lock_ok.is_dir():
                lock_ok.rmdir()
            elif lock_ok.exists():
                lock_ok.unlink()

        # --- Case 3: Using existing .env (without env.enc) does not create .env.pre-restore-* ---
        proj_keep = f"r11-keep-{uuid.uuid4().hex[:10]}"
        orig_env_keep = (
            f"COMPOSE_PROJECT_NAME={proj_keep}\n"
            "DB_HOST=postgres\nDB_USER=postgres\nDB_PASSWORD=keeppg\nDB_NAME=postgres\n"
            "DALTON_TLS_MODE=loopback\nDALTON_EDGE_PORT=8088\n"
        )
        w_keep = tmp_path / "r11-work-keep"
        w_keep.mkdir()
        (w_keep / "docker-compose.yml").write_text("services:\n  backend:\n    env_file: .env\n", encoding="utf-8")
        (w_keep / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
        (w_keep / "agent_config.yaml").write_text('storage:\n  provider: "none"\n', encoding="utf-8")
        (w_keep / ".env").write_text(orig_env_keep, encoding="utf-8")
        (w_keep / ".env").chmod(0o600)
        lock_keep = Path(f"/tmp/dalton-{proj_keep}.restore.lock")
        try:
            res_keep = subprocess.run(
                [
                    "bash", str(REPO_ROOT / "scripts/standalone/restore.sh"),
                    "--work-dir", str(w_keep),
                    "--backup-dir", str(bak_ok),
                    "--skip-minio", "--skip-caddy", "--skip-edge-build",
                ],
                env=env_ok,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert res_keep.returncode == 0, f"Expected existing-.env restore to succeed, stderr:\n{res_keep.stderr}"
            assert (w_keep / ".env").read_text(encoding="utf-8") == orig_env_keep
            assert not list(w_keep.glob(".env.pre-restore-*"))
            assert "Previous .env" not in res_keep.stdout
        finally:
            if lock_keep.is_dir():
                lock_keep.rmdir()
            elif lock_keep.exists():
                lock_keep.unlink()

    def test_b6_r12_post_lock_env_state_symlink_sigterm_and_mutation_boundary(
        self, tmp_path: Path
    ) -> None:
        """Offline verification for Batch 6 R12 fixes:
        1. `.env` existence/backup state is determined strictly AFTER acquiring the
           project restore lock: if `.env` was absent before lock acquisition and
           created before the lock was obtained, restore.sh treats it as existing
           (backs it up before swapping) and leaves its content unchanged on refusal.
        2. When `.env` is a symlink pointing to a 0600 file, preflight failure restores
           the exact same symlink and leaves the target file untouched at mode 0600.
        3. Sending SIGTERM after `.env` swap and before data writes exits 143 and runs
           EXIT cleanup to roll `.env` back to its pre-restore content and mode.
        4. `MUTATION_STARTED=1` is placed immediately before the first actual write to
           object storage or PostgreSQL (after manifest size / `wc -c` preparation).
        """
        import signal

        pass_f = tmp_path / "r12.pass"
        pass_f.write_text("r12-passphrase-abcdef\n", encoding="utf-8")
        pass_f.chmod(0o600)

        def _make_r12_backup(name: str, proj: str, backup_env_text: str) -> Path:
            bdir = tmp_path / name
            bdir.mkdir()
            (bdir / "postgres.dump").write_bytes(b"PGDMP")
            plain = tmp_path / f"{name}.plain.env"
            plain.write_text(backup_env_text, encoding="utf-8")
            subprocess.run(
                [
                    "openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-md", "sha256", "-iter", "10000",
                    "-salt", "-in", str(plain), "-out", str(bdir / "env.enc"), "-pass", f"file:{pass_f}",
                ],
                check=True,
                capture_output=True,
                timeout=15,
            )
            (bdir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "compose_project": proj,
                        "env_backup": "encrypted",
                        "env_encryption": {
                            "cipher": "aes-256-cbc",
                            "kdf": "pbkdf2",
                            "digest": "sha256",
                            "iterations": 10000,
                        },
                    }
                ),
                encoding="utf-8",
            )
            return bdir

        # --- Case 1: `.env` absent before lock, appears by the time lock is acquired ---
        proj_race = f"r12-race-{uuid.uuid4().hex[:10]}"
        created_env_text = (
            f"COMPOSE_PROJECT_NAME={proj_race}\n"
            "DB_HOST=postgres\nDB_USER=postgres\nDB_PASSWORD=firstpg\nDB_NAME=postgres\n"
            "SENTINEL=created-by-first-restore\n"
        )
        backup_env_race = (
            f"COMPOSE_PROJECT_NAME={proj_race}\n"
            "DB_HOST=postgres\nDB_USER=postgres\nDB_PASSWORD=secondpg\nDB_NAME=postgres\n"
            "SENTINEL=second-restore-backup\n"
        )
        bak_race = _make_r12_backup("r12-bak-race", proj_race, backup_env_race)
        w_race = tmp_path / "r12-work-race"
        w_race.mkdir()
        (w_race / "docker-compose.yml").write_text("services:\n  backend:\n    env_file: .env\n", encoding="utf-8")
        (w_race / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
        (w_race / "agent_config.yaml").write_text('storage:\n  provider: "none"\n', encoding="utf-8")
        assert not (w_race / ".env").exists()

        created_env_src = tmp_path / "r12-created-env.src"
        created_env_src.write_text(created_env_text, encoding="utf-8")
        bak_seen_log = tmp_path / "r12-race-bak-seen.log"

        bin_race = tmp_path / "r12-bin-race"
        bin_race.mkdir()
        # Stub `flock` simulates another restore creating `.env` right before this process acquires the lock.
        (bin_race / "flock").write_text(
            "#!/usr/bin/env bash\n"
            f'if [[ "$1" == "-n" && ! -e "{w_race}/.env" ]]; then\n'
            f'  cp "{created_env_src}" "{w_race}/.env"\n'
            f'  chmod 600 "{w_race}/.env"\n'
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (bin_race / "flock").chmod(0o755)
        (bin_race / "docker").write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$*" == *" config --format json"* ]]; then\n'
            f'  ls -1 "{w_race}"/.env.pre-restore-* > "{bak_seen_log}" 2>/dev/null || true\n'
            '  echo \'{"services":{"backend":{"environment":{}}}}\'\n'
            "  exit 0\n"
            "fi\n"
            'if [[ "$*" == *" ps --status running --services"* ]]; then echo "postgres"; exit 0; fi\n'
            'if [[ "$*" == *" pg_isready "* ]]; then exit 0; fi\n'
            'if [[ "$*" == *" pg_restore --list"* ]]; then exit 0; fi\n'
            'if [[ "$*" == *"current_setting(\'is_superuser\')"* ]]; then echo "on"; exit 0; fi\n'
            'if [[ "$*" == *"FROM pg_database"* ]]; then echo "postgres"; exit 0; fi\n'
            'if [[ "$*" == *" -d postgres "* && "$*" == *"pg_class"* ]]; then printf "1\\t0\\t0\\n"; exit 0; fi\n'
            "exit 0\n",
            encoding="utf-8",
        )
        (bin_race / "docker").chmod(0o755)
        env_race = os.environ.copy()
        env_race["PATH"] = f"{bin_race}:{env_race.get('PATH', '')}"
        lock_race = Path(f"/tmp/dalton-{proj_race}.restore.lock")
        lock_race_existed = lock_race.exists()
        try:
            res_race = subprocess.run(
                [
                    "bash", str(REPO_ROOT / "scripts/standalone/restore.sh"),
                    "--work-dir", str(w_race),
                    "--backup-dir", str(bak_race),
                    "--env-passphrase-file", str(pass_f),
                    "--replace-env",
                    "--skip-minio", "--skip-caddy", "--skip-edge-build",
                ],
                env=env_race,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert res_race.returncode != 0
            assert bak_seen_log.exists() and bak_seen_log.read_text(encoding="utf-8").strip(), (
                "restore.sh must detect post-lock .env and create .env.pre-restore-* backup before swapping"
            )
            assert "已把 .env 恢复为恢复前的内容" in res_race.stderr
            assert (w_race / ".env").is_file(), "restore.sh must NOT delete .env that appeared before lock acquisition!"
            assert (w_race / ".env").read_text(encoding="utf-8") == created_env_text
            assert not list(w_race.glob(".env.pre-restore-*"))
        finally:
            if not lock_race_existed:
                if lock_race.is_dir():
                    lock_race.rmdir()
                elif lock_race.exists():
                    lock_race.unlink()

        # --- Case 2: `.env` is a symlink pointing to a 0600 target file ---
        proj_sym = f"r12-sym-{uuid.uuid4().hex[:10]}"
        sym_target_text = (
            f"COMPOSE_PROJECT_NAME={proj_sym}\n"
            "DB_HOST=postgres\nDB_USER=postgres\nDB_PASSWORD=sympg\nDB_NAME=postgres\n"
            "SENTINEL=symlink-target-secret\n"
        )
        sym_backup_text = (
            f"COMPOSE_PROJECT_NAME={proj_sym}\n"
            "DB_HOST=postgres\nDB_USER=postgres\nDB_PASSWORD=newpg\nDB_NAME=postgres\n"
            "SENTINEL=backup-env-over-symlink\n"
        )
        bak_sym = _make_r12_backup("r12-bak-sym", proj_sym, sym_backup_text)
        sym_target = tmp_path / "r12-secret-target.env"
        sym_target.write_text(sym_target_text, encoding="utf-8")
        sym_target.chmod(0o600)

        w_sym = tmp_path / "r12-work-sym"
        w_sym.mkdir()
        (w_sym / "docker-compose.yml").write_text("services:\n  backend:\n    env_file: .env\n", encoding="utf-8")
        (w_sym / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
        (w_sym / "agent_config.yaml").write_text('storage:\n  provider: "none"\n', encoding="utf-8")
        (w_sym / ".env").symlink_to(sym_target)

        sym_bak_check_log = tmp_path / "r12-sym-bak-check.log"
        bin_sym = tmp_path / "r12-bin-sym"
        bin_sym.mkdir()
        (bin_sym / "flock").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (bin_sym / "flock").chmod(0o755)
        (bin_sym / "docker").write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$*" == *" config --format json"* ]]; then\n'
            f'  for b in "{w_sym}"/.env.pre-restore-*; do\n'
            '    if [[ -L "$b" ]]; then echo "SYMLINK:$(readlink "$b")" > "' + str(sym_bak_check_log) + '"; fi\n'
            "  done\n"
            '  echo \'{"services":{"backend":{"environment":{}}}}\'\n'
            "  exit 0\n"
            "fi\n"
            'if [[ "$*" == *" ps --status running --services"* ]]; then echo "postgres"; exit 0; fi\n'
            'if [[ "$*" == *" pg_isready "* ]]; then exit 0; fi\n'
            'if [[ "$*" == *" pg_restore --list"* ]]; then exit 0; fi\n'
            'if [[ "$*" == *"current_setting(\'is_superuser\')"* ]]; then echo "on"; exit 0; fi\n'
            'if [[ "$*" == *"FROM pg_database"* ]]; then echo "postgres"; exit 0; fi\n'
            'if [[ "$*" == *" -d postgres "* && "$*" == *"pg_class"* ]]; then printf "1\\t0\\t0\\n"; exit 0; fi\n'
            "exit 0\n",
            encoding="utf-8",
        )
        (bin_sym / "docker").chmod(0o755)
        env_sym = os.environ.copy()
        env_sym["PATH"] = f"{bin_sym}:{env_sym.get('PATH', '')}"
        lock_sym = Path(f"/tmp/dalton-{proj_sym}.restore.lock")
        lock_sym_existed = lock_sym.exists()
        try:
            res_sym = subprocess.run(
                [
                    "bash", str(REPO_ROOT / "scripts/standalone/restore.sh"),
                    "--work-dir", str(w_sym),
                    "--backup-dir", str(bak_sym),
                    "--env-passphrase-file", str(pass_f),
                    "--replace-env",
                    "--skip-minio", "--skip-caddy", "--skip-edge-build",
                ],
                env=env_sym,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert res_sym.returncode != 0
            assert "已把 .env 恢复为恢复前的内容" in res_sym.stderr
            assert sym_bak_check_log.exists() and sym_bak_check_log.read_text(encoding="utf-8").strip() == f"SYMLINK:{sym_target}", (
                "Symlink .env must be moved itself as .env.pre-restore-* backup"
            )
            assert (w_sym / ".env").is_symlink(), ".env must remain a symlink after preflight failure!"
            assert os.readlink(w_sym / ".env") == str(sym_target), ".env must point to the exact same target!"
            assert sym_target.read_text(encoding="utf-8") == sym_target_text
            assert stat.S_IMODE(sym_target.stat().st_mode) == 0o600, (
                f"Symlink target permissions must remain 0600, got {oct(stat.S_IMODE(sym_target.stat().st_mode))}"
            )
            assert not list(w_sym.glob(".env.pre-restore-*"))
        finally:
            if not lock_sym_existed:
                if lock_sym.is_dir():
                    lock_sym.rmdir()
                elif lock_sym.exists():
                    lock_sym.unlink()

        # --- Case 3: SIGTERM after `.env` swap and before writes rolls `.env` back and exits 143 ---
        proj_term = f"r12-term-{uuid.uuid4().hex[:10]}"
        orig_term_env = (
            f"COMPOSE_PROJECT_NAME={proj_term}\n"
            "DB_HOST=postgres\nDB_USER=postgres\nDB_PASSWORD=oldpg\nDB_NAME=postgres\n"
            "SENTINEL=original-before-sigterm\n"
        )
        new_term_env = (
            f"COMPOSE_PROJECT_NAME={proj_term}\n"
            "DB_HOST=postgres\nDB_USER=postgres\nDB_PASSWORD=newpg\nDB_NAME=postgres\n"
            "SENTINEL=swapped-during-preflight\n"
        )
        bak_term = _make_r12_backup("r12-bak-term", proj_term, new_term_env)
        w_term = tmp_path / "r12-work-term"
        w_term.mkdir()
        (w_term / "docker-compose.yml").write_text("services:\n  backend:\n    env_file: .env\n", encoding="utf-8")
        (w_term / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
        (w_term / "agent_config.yaml").write_text('storage:\n  provider: "none"\n', encoding="utf-8")
        (w_term / ".env").write_text(orig_term_env, encoding="utf-8")
        (w_term / ".env").chmod(0o640)

        term_ready = tmp_path / "r12-term-ready"
        bin_term = tmp_path / "r12-bin-term"
        bin_term.mkdir()
        (bin_term / "flock").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (bin_term / "flock").chmod(0o755)
        (bin_term / "docker").write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$*" == *" config --format json"* ]]; then echo \'{"services":{"backend":{"environment":{}}}}\'; exit 0; fi\n'
            'if [[ "$*" == *" ps --status running --services"* ]]; then echo "postgres"; exit 0; fi\n'
            'if [[ "$*" == *" pg_isready "* ]]; then exit 0; fi\n'
            'if [[ "$*" == *" pg_restore --list"* ]]; then\n'
            f'  touch "{term_ready}"\n'
            "  sleep 0.4\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (bin_term / "docker").chmod(0o755)
        env_term = os.environ.copy()
        env_term["PATH"] = f"{bin_term}:{env_term.get('PATH', '')}"
        lock_term = Path(f"/tmp/dalton-{proj_term}.restore.lock")
        lock_term_existed = lock_term.exists()
        try:
            proc = subprocess.Popen(
                [
                    "bash", str(REPO_ROOT / "scripts/standalone/restore.sh"),
                    "--work-dir", str(w_term),
                    "--backup-dir", str(bak_term),
                    "--env-passphrase-file", str(pass_f),
                    "--replace-env",
                    "--skip-minio", "--skip-caddy", "--skip-edge-build",
                ],
                env=env_term,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + 10.0
            while not term_ready.exists() and time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                time.sleep(0.02)
            assert term_ready.exists(), "restore.sh did not reach pre-write pg_restore --list stage"
            assert (w_term / ".env").read_text(encoding="utf-8") == new_term_env, (
                "Expected decrypted .env to be swapped in before SIGTERM"
            )
            proc.send_signal(signal.SIGTERM)
            _, term_stderr = proc.communicate(timeout=15)
            assert proc.returncode == 143, f"Expected exit code 143 on SIGTERM, got {proc.returncode}: {term_stderr}"
            assert "已把 .env 恢复为恢复前的内容" in term_stderr, (
                f"SIGTERM before writes must trigger .env rollback warning, got:\n{term_stderr}"
            )
            assert (w_term / ".env").read_text(encoding="utf-8") == orig_term_env
            assert stat.S_IMODE((w_term / ".env").stat().st_mode) == 0o640
            assert not list(w_term.glob(".env.pre-restore-*"))
        finally:
            if not lock_term_existed:
                if lock_term.is_dir():
                    lock_term.rmdir()
                elif lock_term.exists():
                    lock_term.unlink()

        # --- Case 4: MUTATION_STARTED=1 is set after `wc -c` prep, right before actual writes ---
        restore_src = (REPO_ROOT / "scripts/standalone/restore.sh").read_text(encoding="utf-8")
        wc_pos = restore_src.index('manifest_size="$(wc -c < "$MINIO_MANIFEST_TMP"')
        first_mut_pos = restore_src.index("MUTATION_STARTED=1")
        assert first_mut_pos > wc_pos, (
            "MUTATION_STARTED=1 must be set AFTER manifest_size / wc -c preparation, right before writing!"
        )
        assert restore_src.count("MUTATION_STARTED=1") == 2, (
            "MUTATION_STARTED=1 should be set immediately before MinIO upload write and before PG write"
        )
