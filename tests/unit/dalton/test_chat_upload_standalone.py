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
from ptc_agent.core.sandbox.providers.docker import DockerProvider
from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
from src.dalton import chat_upload_storage as storage
from src.dalton import transcription_storage as asr_storage
from src.dalton.app.chat_uploads import (
    router as chat_uploads_router,
    sandbox_relay_router,
)
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
        mock_ensure_image = create_autospec(provider._ensure_image, return_value=None)
        mock_start_new = create_autospec(provider._start_new, side_effect=_fake_start_new)
        with (
            patch.object(provider, "_get_client", mock_get_client),
            patch.object(provider, "_ensure_image", mock_ensure_image),
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
            mock_ensure_image = create_autospec(provider._ensure_image, return_value=None)
            mock_start_new = create_autospec(provider._start_new, side_effect=_fake_start_new)
            with (
                patch.object(provider, "_get_client", mock_get_client),
                patch.object(provider, "_ensure_image", mock_ensure_image),
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
            mock_ensure_image = create_autospec(provider._ensure_image, return_value=None)
            mock_start_new = create_autospec(provider._start_new, side_effect=_fake_start_new)
            with (
                patch.object(provider, "_get_client", mock_get_client),
                patch.object(provider, "_ensure_image", mock_ensure_image),
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

        # 3b. The guard reads the auth mode the stack will run with: a stray
        # shell DALTON_AUTH_MODE and a quoted 'oss' in .env both still refuse.
        _, init_shell = self._stage_init_install(tmp_path, "inst-shell-auth")
        shell_env = self._make_fake_docker_env(tmp_path, "28.0.1")
        shell_env["DALTON_AUTH_MODE"] = "supabase"
        res_shell = subprocess.run(
            ["bash", str(init_shell), "--non-interactive", "--domain",
             "dalton.example.com", "--skip-sandbox-build"],
            env=shell_env, capture_output=True, text=True, timeout=15,
        )
        assert res_shell.returncode != 0
        assert "免密单机试用" in res_shell.stderr

        inst_quoted, init_quoted = self._stage_init_install(tmp_path, "inst-quoted-auth")
        (inst_quoted / ".env").write_text("DALTON_AUTH_MODE='oss'\n")
        res_quoted = subprocess.run(
            ["bash", str(init_quoted), "--non-interactive", "--domain",
             "dalton.example.com", "--skip-sandbox-build"],
            env=self._make_fake_docker_env(tmp_path, "28.0.1"),
            capture_output=True, text=True, timeout=15,
        )
        assert res_quoted.returncode != 0
        assert "免密单机试用" in res_quoted.stderr

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
