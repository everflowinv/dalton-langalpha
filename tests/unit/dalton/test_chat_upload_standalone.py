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
from ptc_agent.core.sandbox.providers import docker as docker_provider_mod
from ptc_agent.core.sandbox.providers.docker import DockerProvider
from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
from src.dalton import chat_upload_storage as storage
from src.dalton import transcription_storage as asr_storage
from src.dalton.app.chat_uploads import (
    router as chat_uploads_router,
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

        # 2. Public domain with oss auth is rejected with exact message
        inst_domain, init_domain = self._stage_init_install(tmp_path, "inst-domain")
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
            timeout=15,
        )
        assert res_domain.returncode != 0
        assert (
            "当前核心版是免密单机试用，请先用 localhost 安装；正式对外部署等本地登录（Batch 4b）上线后再配置域名。"
            in res_domain.stderr
        )
        assert not (inst_domain / ".env").exists()

        # 3. ::1 is allowed as loopback under oss auth
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

        # 3b. Any non-loopback domain is refused, whatever .env or the shell say,
        # until Batch 4b writes the local auth mode itself.
        for name, env_text, shell_mode in (
            ("inst-shell-supabase", None, "supabase"),
            ("inst-env-local", "DALTON_AUTH_MODE=local\n", None),
            ("inst-env-quoted-oss", "DALTON_AUTH_MODE='oss'\n", None),
            ("inst-env-expansion", "DALTON_AUTH_MODE=${LOCAL}\n", None),
        ):
            inst_case, init_case = self._stage_init_install(tmp_path, name)
            if env_text is not None:
                (inst_case / ".env").write_text(env_text)
            case_env = self._make_fake_docker_env(tmp_path, "28.0.1")
            case_env.pop("DALTON_AUTH_MODE", None)
            if shell_mode is not None:
                case_env["DALTON_AUTH_MODE"] = shell_mode
            res_case = subprocess.run(
                ["bash", str(init_case), "--non-interactive", "--domain",
                 "dalton.example.com", "--skip-sandbox-build"],
                env=case_env, capture_output=True, text=True, timeout=15,
            )
            assert res_case.returncode != 0, name
            assert "免密单机试用" in res_case.stderr, name

        # 3c. A public DOMAIN already in .env, with no --domain, is refused too.
        inst_prev, init_prev = self._stage_init_install(tmp_path, "inst-env-domain")
        (inst_prev / ".env").write_text("DOMAIN=dalton.example.com\n")
        res_prev = subprocess.run(
            ["bash", str(init_prev), "--non-interactive", "--skip-sandbox-build"],
            env=self._make_fake_docker_env(tmp_path, "28.0.1"),
            capture_output=True, text=True, timeout=15,
        )
        assert res_prev.returncode != 0
        assert "免密单机试用" in res_prev.stderr
        assert "dalton.example.com" in res_prev.stderr

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
        assert "sanitize_project_name" not in restore_text, "restore.sh must compare COMPOSE_PROJECT_NAME verbatim"
        assert "--replace-data" not in restore_text, "restore.sh must remove --replace-data"
        assert "ALTER DATABASE" not in restore_text, "restore.sh must remove DB rename / swap logic"
        assert "_pre_restore_" not in restore_text, "restore.sh must remove preserved DB logic"
        assert ".env.pre-restore-" not in restore_text, "restore.sh must remove pre-restore .env copy logic"
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

        # upgrade.sh rollback hint relies strictly on comparing successfully read pre/post Alembic versions:
        #   - unchanged versions -> restore files & images only (do NOT wipe volumes)
        #   - changed versions   -> point to manual §6.2 steps (`down -v` + `restore.sh`, no `--replace-data`)
        #   - unreadable pre-migration version -> UNCERTAIN (prompt manual version check first)
        for scenario, psql_behavior, migrate_body, expected_mode in (
            (
                "fail-versions-unchanged",
                'echo "rev1"; exit 0',
                'echo "[migrate] Waiting for PostgreSQL..."; echo "connection refused" >&2; exit 1',
                "before",
            ),
            (
                "fail-versions-changed",
                'state_f="${UPG_STATE_FILE}"; if [[ -f "$state_f" ]]; then echo "rev2"; else echo "rev1"; fi; exit 0',
                'touch "${UPG_STATE_FILE}"; echo "[migrate] partial upgrade failed" >&2; exit 1',
                "migrated",
            ),
            (
                "fail-pre-version-unreadable",
                'state_f="${UPG_STATE_FILE}"; if [[ -f "$state_f" ]]; then echo "rev1"; exit 0; else exit 1; fi',
                'touch "${UPG_STATE_FILE}"; echo "migrate failed" >&2; exit 1',
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
            (uwork / ".env").write_text("DB_HOST=postgres\nDB_USER=postgres\nDB_NAME=postgres\n", encoding="utf-8")
            fake_bak_dir = ubak / "dalton-backup-20260101-000000Z-abc123"
            fake_bak_dir.mkdir()
            backup_stub_script = (
                "#!/usr/bin/env bash\n"
                f"printf '  Backup directory : %s\\n' '{fake_bak_dir}'\n"
            )
            (uwork / "scripts/standalone/backup.sh").write_text(backup_stub_script, encoding="utf-8")
            (urel / "scripts/standalone/backup.sh").write_text(backup_stub_script, encoding="utf-8")
            docker_stub = fake_bin / "docker"
            docker_stub.write_text(
                "#!/usr/bin/env bash\n"
                'if [[ "$*" == *" images -q "* ]]; then echo "img123"; exit 0; fi\n'
                'if [[ "$1" == "tag" ]]; then exit 0; fi\n'
                f'if [[ "$*" == *" psql "* ]]; then {psql_behavior}; fi\n'
                f'if [[ "$*" == *" run --rm migrate"* ]]; then {migrate_body}; fi\n'
                "exit 0\n",
                encoding="utf-8",
            )
            docker_stub.chmod(0o755)
            stub_env = os.environ.copy()
            stub_env["PATH"] = f"{fake_bin}:{stub_env.get('PATH', '')}"
            stub_env["UPG_STATE_FILE"] = str(sc_root / "migrated.flag")
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
                    "--skip-build",
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
            if expected_mode == "migrated":
                assert "STANDALONE-DEPLOY.md" in res_sc.stderr and "down -v" in res_sc.stderr, (
                    f"{scenario}: expected manual §6.2 reset-and-restore guide, got:\n{res_sc.stderr}"
                )
            elif expected_mode == "unknown":
                assert "UNCERTAIN" in res_sc.stderr and "Do NOT assume migrations already ran" in res_sc.stderr, (
                    f"{scenario}: expected uncertain-state manual version check guide, got:\n{res_sc.stderr}"
                )
            else:
                assert "Do NOT wipe volumes or restore data" in res_sc.stderr, (
                    f"{scenario}: pre-migration failure must not suggest wiping data, got:\n{res_sc.stderr}"
                )
