"""Unit tests for workspace-file-storage Batch 1 Step 4.

Covers:
1. Browser external-link containment (``DALTON-HOOK(storage-private-bucket-urls)``):
   - ``workspace_files.public_bucket_urls`` defaults to ``False`` in both
     ``dalton.yaml`` and ``dalton.core.yaml`` and supports live env/YAML override.
   - ``download_link.py`` (``_sign``, ``mirror_download_link``, ``live_download_link``):
     returns ``None`` when ``public_bucket_urls=False`` (even if
     ``STORAGE_BROWSER_ENDPOINT_URL`` is configured) so ``/files/download-url``
     issues a 60s ``dl_token``; preserves existing behavior when
     ``public_bucket_urls=True``.
   - ``src/ptc_agent/agent/middleware/image_capture.py`` (``capture_sandbox_images``
     and ``ImageCaptureMiddleware._arecord``): emits zero direct bucket URLs when
     ``public_bucket_urls=False`` so frontend loads images via backend proxy;
     preserves AU1 ``private-media/response-images/`` capture when ``True``.
   - ``src/server/services/persistence/image_capture.py`` (``capture_images``):
     returns ``{}`` immediately without acquiring folder hold when
     ``public_bucket_urls=False``; preserves existing delegation when ``True``.
   - ``src/server/utils/multimodal_context.py`` (``build_attachment_metadata``):
     omits ``url`` for staged objects and skips upload + ``url`` for inline
     attachments when ``public_bucket_urls=False``; preserves AU1
     ``private-media/attachments/`` upload and ``url`` when ``True``.
2. Dual-profile ``Caddyfile`` (``deploy/dalton/Caddyfile`` and
   ``deploy/standalone/Caddyfile``):
   - Read-only ``GET``/``HEAD`` proxy for ``/storage/avatars/*`` (excluding bare
     ``/storage/avatars/``), stripping ``versionId`` query params, rewriting to
     ``/dalton-main{path}`` / ``/dalton-storage{path}``, stripping auth headers,
     and returning ``404`` for every other ``/storage`` / ``/storage/*`` route or
     non-read method; zero new ``{$VAR}`` placeholders in standalone Caddyfile.
3. Public share streaming integration in ``src/server/app/share_files.py``
   (``download_shared_file``):
   - Placed strictly after ``resolve_shared_files(share_token, require_download=True)``
     and ``target.visible(normalized_path)``.
   - ``(100 MiB, 256 MiB]`` text and binary files both stream through
     ``StreamingSecretRedactor`` with ``Cache-Control: no-store``.
   - ``<= 100 MiB`` files fall through to ``_shared_file_bytes`` with
     ``Cache-Control: no-store``.
   - Downgraded ``allow_files=false`` (including legacy ``allow_download=true``)
     returns ``403`` with ``Cache-Control: no-store`` and
     ``Clear-Site-Data: "cache"`` without touching ``shared_large_download``.
   - Closed share or invalid token returns ``404`` with ``Cache-Control: no-store``.
   - First-chunk storage error collapses to ``404`` before headers with
     ``Cache-Control: no-store``.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

import pytest
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage

from ptc_agent.agent.middleware import image_capture as mw_ic
from ptc_agent.core.paths import SandboxLayout
from src.dalton import private_bucket_urls as pbu_mod
from src.dalton import storage_config
from src.dalton import streaming_redactor as sr_mod
from src.dalton import workspace_file_stream as wfs_mod
from src.server.app import share_access, share_files
from src.server.database import workspace_file as wf_db
from src.server.database.blob_keys import RELAY_MAX_BYTES
from src.server.models.additional_context import MultimodalContext
from src.server.models.chat_attachment import (
    StoredSource,
    TurnAttachment,
    TurnAttachments,
)
from src.server.services.persistence import download_link
from src.server.services.persistence import image_capture as persist_ic
from src.server.services.persistence.resolve import (
    SERVE_MAX_BYTES,
    FileBytesUnavailable,
)
from src.server.services.persistence.transfer import ScanEntry
from src.server.services.served_paths import ShareScope
from src.server.utils import multimodal_context as mm_mod
from src.server.utils.secret_redactor import SecretRedactor

REPO_ROOT = Path(__file__).resolve().parents[3]
PNG = b"\x89PNG\r\n\x1a\npng-bytes"
PNG_SHA16 = hashlib.sha256(PNG).hexdigest()[:16]


def _make_redactor(secrets: list[tuple[str, str]]) -> SecretRedactor:
    inst = SecretRedactor.__new__(SecretRedactor)
    inst._secrets = sorted(secrets, key=lambda kv: len(kv[1]), reverse=True)
    return inst


def _make_share_app() -> FastAPI:
    app = FastAPI()
    app.include_router(share_files.share_files_router, prefix="/api/v1/public")
    return app


class _SandboxSpec:
    working_dir = "/home/workspace"

    def normalize_path(self, path: str, *, project=None) -> str:
        return f"/home/workspace/{path.lstrip('/')}"

    async def adownload_file_bytes(self, filepath: str) -> bytes | None:
        return PNG


# ---------------------------------------------------------------------------
# 1. Config: workspace_files.public_bucket_urls
# ---------------------------------------------------------------------------


class TestPublicBucketUrlsConfig:
    def test_yaml_defaults_false_in_both_profiles(self) -> None:
        raw_everflow = yaml.safe_load(
            storage_config.DALTON_YAML_PATH.read_text(encoding="utf-8")
        )
        raw_core = yaml.safe_load(
            storage_config.DALTON_CORE_YAML_PATH.read_text(encoding="utf-8")
        )
        assert raw_everflow["workspace_files"]["public_bucket_urls"] is False
        assert raw_core["workspace_files"]["public_bucket_urls"] is False
        assert storage_config.is_wsfiles_public_bucket_urls(env={}) is False
        assert (
            storage_config.is_wsfiles_public_bucket_urls(
                env={"DALTON_PROFILE": "core"}
            )
            is False
        )

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " True "])
    def test_env_truthy_values(self, raw: str) -> None:
        assert (
            storage_config.is_wsfiles_public_bucket_urls(
                env={"DALTON_WSFILES_PUBLIC_BUCKET_URLS": raw}
            )
            is True
        )

    @pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", " False "])
    def test_env_falsy_values(self, raw: str) -> None:
        assert (
            storage_config.is_wsfiles_public_bucket_urls(
                env={"DALTON_WSFILES_PUBLIC_BUCKET_URLS": raw}
            )
            is False
        )

    def test_invalid_env_logs_warning_and_returns_false(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING"):
            val = storage_config.is_wsfiles_public_bucket_urls(
                env={"DALTON_WSFILES_PUBLIC_BUCKET_URLS": "maybe"}
            )
        assert val is False
        assert "DALTON_WSFILES_PUBLIC_BUCKET_URLS" in caplog.text

    def test_yaml_override_and_live_reread(self) -> None:
        loader_stub = create_autospec(
            storage_config._load_dalton_workspace_files_raw,
            spec_set=True,
            return_value={"public_bucket_urls": True},
        )
        with patch.object(
            storage_config, "_load_dalton_workspace_files_raw", loader_stub
        ):
            assert storage_config.is_wsfiles_public_bucket_urls(env={}) is True
            assert (
                storage_config.is_wsfiles_public_bucket_urls(
                    env={"DALTON_WSFILES_PUBLIC_BUCKET_URLS": "0"}
                )
                is False
            )
        assert pbu_mod.is_public_bucket_urls_enabled(env={}) is False


# ---------------------------------------------------------------------------
# 2. Browser external-link containment: download_link.py
# ---------------------------------------------------------------------------


class TestDownloadLinkPrivateBucketContainment:
    @pytest.mark.asyncio
    async def test_sign_returns_none_when_public_bucket_urls_false_even_with_browser_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DALTON_WSFILES_PUBLIC_BUCKET_URLS", raising=False)
        monkeypatch.setenv("STORAGE_BROWSER_ENDPOINT_URL", "https://s3.browser.example")
        mock_signed = create_autospec(
            download_link.get_signed_url,
            spec_set=True,
            return_value="https://s3.browser.example/signed",
        )
        with patch.object(download_link, "get_signed_url", mock_signed):
            url = await download_link._sign("u-1", "a" * 64, "big.bin")
        assert url is None
        mock_signed.assert_not_called()

    @pytest.mark.asyncio
    async def test_mirror_and_live_download_link_return_none_and_fallback_to_dl_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton import download_ticket

        monkeypatch.delenv("DALTON_WSFILES_PUBLIC_BUCKET_URLS", raising=False)
        monkeypatch.setenv("STORAGE_BROWSER_ENDPOINT_URL", "https://s3.browser.example")
        monkeypatch.setenv("JWT_SIGNING_SECRET", "s" * 40)

        mock_locator = create_autospec(
            download_link.get_file_locator,
            spec_set=True,
            return_value={
                "blob_sha256": "b" * 64,
                "file_name": "big.bin",
                "file_size": (12 << 20),
            },
        )
        mock_hash = create_autospec(download_link.hash_one_file, spec_set=True)
        mock_persist = create_autospec(download_link._persist_blobs, spec_set=True)
        mock_enabled = create_autospec(
            download_link.is_storage_enabled, spec_set=True, return_value=True
        )
        ws = {"workspace_id": "ws-1", "user_id": "u-1"}

        with (
            patch.object(download_link, "is_storage_enabled", mock_enabled),
            patch.object(download_link, "get_file_locator", mock_locator),
            patch.object(download_link, "hash_one_file", mock_hash),
            patch.object(download_link, "_persist_blobs", mock_persist),
        ):
            mirror_url = await download_link.mirror_download_link(ws, "big.bin")
            live_url = await download_link.live_download_link(
                ws, create_autospec(_SandboxSpec, instance=True), "big.bin", layout=SandboxLayout()
            )

        assert mirror_url is None
        assert live_url is None
        mock_locator.assert_not_awaited()
        mock_hash.assert_not_awaited()
        mock_persist.assert_not_awaited()

        # Backend download-url route then hands out a 60s dl_token instead of a bucket URL.
        ticket_locator = create_autospec(
            wf_db.get_file_locator,
            spec_set=True,
            return_value={"file_size": 12 << 20},
        )
        with patch.object(download_ticket, "get_file_locator", ticket_locator):
            payload = await download_ticket.maybe_attach_backup_download_ticket(
                {"url": mirror_url},
                user_id="u-1",
                workspace_id="ws-1",
                path="big.bin",
                normalized_path="big.bin",
                backup_ok=True,
            )
        assert payload["url"] is None
        assert payload.get("dl_token")
        claims = download_ticket.verify_download_ticket(payload["dl_token"])
        assert claims.workspace_id == "ws-1"
        assert claims.path == "big.bin"

    @pytest.mark.asyncio
    async def test_download_link_preserves_existing_behavior_when_public_bucket_urls_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DALTON_WSFILES_PUBLIC_BUCKET_URLS", "1")
        monkeypatch.setenv("STORAGE_BROWSER_ENDPOINT_URL", "https://s3.browser.example")

        ws = {"workspace_id": "ws-1", "user_id": "u-1"}
        mock_enabled = create_autospec(
            download_link.is_storage_enabled, spec_set=True, return_value=True
        )
        mock_signed = create_autospec(
            download_link.get_signed_url,
            spec_set=True,
            return_value="https://s3.browser.example/signed-url",
        )
        mock_locator = create_autospec(
            download_link.get_file_locator,
            spec_set=True,
            return_value={"blob_sha256": "c" * 64, "file_name": "big.bin"},
        )
        entry = ScanEntry("big.bin", "file", 20 << 20, 1, 0o644, "d" * 64, None, True)
        mock_hash = create_autospec(
            download_link.hash_one_file, spec_set=True, return_value=entry
        )
        mock_cap = create_autospec(
            download_link.scan_cap_bytes, spec_set=True, return_value=None
        )
        mock_persist = create_autospec(
            download_link._persist_blobs,
            spec_set=True,
            return_value=([{"file_path": "big.bin"}], []),
        )

        with (
            patch.object(download_link, "is_storage_enabled", mock_enabled),
            patch.object(download_link, "get_signed_url", mock_signed),
            patch.object(download_link, "get_file_locator", mock_locator),
            patch.object(download_link, "hash_one_file", mock_hash),
            patch.object(download_link, "scan_cap_bytes", mock_cap),
            patch.object(download_link, "_persist_blobs", mock_persist),
        ):
            mirror_url = await download_link.mirror_download_link(ws, "big.bin")
            live_url = await download_link.live_download_link(
                ws, create_autospec(_SandboxSpec, instance=True), "big.bin", layout=SandboxLayout()
            )

        assert mirror_url == "https://s3.browser.example/signed-url"
        assert live_url == "https://s3.browser.example/signed-url"
        assert mock_signed.call_count == 2

        # When browser endpoint is unset, Step 2 gate still returns None even with public_bucket_urls=true.
        monkeypatch.setenv("STORAGE_BROWSER_ENDPOINT_URL", "")
        with patch.object(download_link, "get_signed_url", mock_signed):
            assert await download_link._sign("u-1", "c" * 64, "big.bin") is None


# ---------------------------------------------------------------------------
# 3. Browser external-link containment: middleware & persistence image_capture
# ---------------------------------------------------------------------------


class TestImageCapturePrivateBucketContainment:
    @pytest.mark.asyncio
    async def test_middleware_and_capture_sandbox_images_skip_direct_urls_when_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DALTON_WSFILES_PUBLIC_BUCKET_URLS", raising=False)
        sandbox = create_autospec(_SandboxSpec, instance=True)
        sandbox.normalize_path.return_value = "/home/workspace/charts/rev.png"
        sandbox.adownload_file_bytes.return_value = PNG

        mock_enabled = create_autospec(
            mw_ic.is_storage_enabled, spec_set=True, return_value=True
        )
        mock_upload = create_autospec(
            mw_ic.upload_bytes, spec_set=True, return_value=True
        )
        mock_pub = create_autospec(
            mw_ic.get_public_url,
            spec_set=True,
            side_effect=lambda k: f"https://cdn.test/{k}",
        )

        mw = mw_ic.ImageCaptureMiddleware(session=SimpleNamespace(sandbox=sandbox))
        msg = AIMessage(content="See ![rev](charts/rev.png)", id="ai-1")

        async def _handler(_req):
            return ModelResponse(result=[msg])

        with (
            patch.object(mw_ic, "is_storage_enabled", mock_enabled),
            patch.object(mw_ic, "upload_bytes", mock_upload),
            patch.object(mw_ic, "get_public_url", mock_pub),
        ):
            direct = await mw_ic.capture_sandbox_images(
                sandbox, {"charts/rev.png"}, thread_id="t-1"
            )
            out = await mw.awrap_model_call(None, _handler)

        assert direct == {}
        assert out.result[0] is msg
        assert mw_ic.recorded_images(out.result[0]) == {}
        assert out.result[0].content == "See ![rev](charts/rev.png)"
        sandbox.adownload_file_bytes.assert_not_awaited()
        mock_upload.assert_not_called()
        mock_pub.assert_not_called()

    @pytest.mark.asyncio
    async def test_middleware_preserves_au1_private_media_capture_when_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DALTON_WSFILES_PUBLIC_BUCKET_URLS", "1")
        sandbox = create_autospec(_SandboxSpec, instance=True)
        sandbox.normalize_path.return_value = "/home/workspace/charts/rev.png"
        sandbox.adownload_file_bytes.return_value = PNG

        mock_enabled = create_autospec(
            mw_ic.is_storage_enabled, spec_set=True, return_value=True
        )
        mock_upload = create_autospec(
            mw_ic.upload_bytes, spec_set=True, return_value=True
        )
        mock_pub = create_autospec(
            mw_ic.get_public_url,
            spec_set=True,
            side_effect=lambda k: f"https://cdn.test/{k}",
        )

        mw = mw_ic.ImageCaptureMiddleware(session=SimpleNamespace(sandbox=sandbox))
        msg = AIMessage(content="See ![rev](charts/rev.png)", id="ai-1")

        async def _handler(_req):
            return ModelResponse(result=[msg])

        with (
            patch.object(mw_ic, "is_storage_enabled", mock_enabled),
            patch.object(mw_ic, "upload_bytes", mock_upload),
            patch.object(mw_ic, "get_public_url", mock_pub),
            patch.object(mw_ic.ImageCaptureMiddleware, "_thread_id", return_value="t-1"),
        ):
            out = await mw.awrap_model_call(None, _handler)

        expected_key = f"private-media/response-images/t-1/{PNG_SHA16}/rev.png"
        assert mw_ic.recorded_images(out.result[0]) == {
            "charts/rev.png": f"https://cdn.test/{expected_key}"
        }
        mock_upload.assert_called_once_with(expected_key, PNG, "image/png")

    @pytest.mark.asyncio
    async def test_persistence_capture_images_skips_when_false_and_delegates_when_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sandbox = create_autospec(_SandboxSpec, instance=True)
        mock_enabled = create_autospec(
            persist_ic.is_storage_enabled, spec_set=True, return_value=True
        )
        mock_cap = create_autospec(
            persist_ic.capture_sandbox_images,
            spec_set=True,
            return_value={
                "charts/a.png": f"https://cdn.test/private-media/response-images/t-1/{PNG_SHA16}/a.png"
            },
        )
        mock_held = create_autospec(
            persist_ic._capture_from_held_folder,
            spec_set=True,
            return_value={"charts/a.png": "https://cdn.test/held"},
        )

        # public_bucket_urls=False -> immediate {} without folder hold or sandbox capture
        monkeypatch.delenv("DALTON_WSFILES_PUBLIC_BUCKET_URLS", raising=False)
        with (
            patch.object(persist_ic, "is_storage_enabled", mock_enabled),
            patch.object(persist_ic, "capture_sandbox_images", mock_cap),
            patch.object(persist_ic, "_capture_from_held_folder", mock_held),
        ):
            res_direct = await persist_ic.capture_images(
                {"charts/a.png"}, sandbox, thread_id="t-1"
            )
            res_held = await persist_ic.capture_images(
                {"charts/a.png"}, sandbox, thread_id="t-1", workspace_id="ws-1"
            )
        assert res_direct == {}
        assert res_held == {}
        mock_cap.assert_not_awaited()
        mock_held.assert_not_awaited()

        # public_bucket_urls=True -> existing delegation preserved
        monkeypatch.setenv("DALTON_WSFILES_PUBLIC_BUCKET_URLS", "1")
        with (
            patch.object(persist_ic, "is_storage_enabled", mock_enabled),
            patch.object(persist_ic, "capture_sandbox_images", mock_cap),
            patch.object(persist_ic, "_capture_from_held_folder", mock_held),
        ):
            res_direct_on = await persist_ic.capture_images(
                {"charts/a.png"}, sandbox, thread_id="t-1"
            )
            res_held_on = await persist_ic.capture_images(
                {"charts/a.png"}, sandbox, thread_id="t-1", workspace_id="ws-1"
            )
        assert res_direct_on["charts/a.png"].startswith(
            "https://cdn.test/private-media/response-images/t-1/"
        )
        assert res_held_on == {"charts/a.png": "https://cdn.test/held"}
        mock_cap.assert_awaited_once()
        mock_held.assert_awaited_once()


# ---------------------------------------------------------------------------
# 4. Browser external-link containment: multimodal_context.py
# ---------------------------------------------------------------------------


class TestMultimodalContextPrivateBucketContainment:
    @pytest.mark.asyncio
    async def test_omits_urls_and_skips_inline_upload_when_public_bucket_urls_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DALTON_WSFILES_PUBLIC_BUCKET_URLS", raising=False)

        inline_att = mm_mod.inline_attachment(
            MultimodalContext(
                type="image",
                data="data:image/png;base64,YWJj",
                description="chart",
            )
        )
        assert inline_att is not None
        async def _load_staged() -> bytes | None:
            return PNG

        staged_att = TurnAttachment(
            name="report.png",
            mime_type="image/png",
            size=42,
            source=StoredSource(
                attachment_id="att-1",
                object_key="private-media/attachments/att-1/report.png",
                load=_load_staged,
            ),
        )

        mock_enabled = create_autospec(
            mm_mod.is_storage_enabled, spec_set=True, return_value=True
        )
        mock_upload = create_autospec(
            mm_mod.upload_bytes, spec_set=True, return_value=True
        )
        mock_pub = create_autospec(
            mm_mod.get_public_url,
            spec_set=True,
            side_effect=lambda k: f"https://cdn.test/{k}",
        )

        with (
            patch.object(mm_mod, "is_storage_enabled", mock_enabled),
            patch.object(mm_mod, "upload_bytes", mock_upload),
            patch.object(mm_mod, "get_public_url", mock_pub),
        ):
            meta = await mm_mod.build_attachment_metadata(
                TurnAttachments([inline_att, staged_att]), thread_id="t-42"
            )

        assert meta == [
            {"name": "chart", "type": "image", "size": 3},
            {"name": "report.png", "type": "image", "size": 42},
        ]
        mock_upload.assert_not_called()
        mock_pub.assert_not_called()

    @pytest.mark.asyncio
    async def test_emits_au1_private_media_urls_when_public_bucket_urls_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DALTON_WSFILES_PUBLIC_BUCKET_URLS", "1")

        inline_att = mm_mod.inline_attachment(
            MultimodalContext(
                type="image",
                data="data:image/png;base64,YWJj",
                description="chart",
            )
        )
        assert inline_att is not None
        async def _load_staged() -> bytes | None:
            return PNG

        staged_att = TurnAttachment(
            name="report.png",
            mime_type="image/png",
            size=42,
            source=StoredSource(
                attachment_id="att-1",
                object_key="private-media/attachments/att-1/report.png",
                load=_load_staged,
            ),
        )

        mock_enabled = create_autospec(
            mm_mod.is_storage_enabled, spec_set=True, return_value=True
        )
        mock_upload = create_autospec(
            mm_mod.upload_bytes, spec_set=True, return_value=True
        )
        mock_pub = create_autospec(
            mm_mod.get_public_url,
            spec_set=True,
            side_effect=lambda k: f"https://cdn.test/{k}",
        )

        with (
            patch.object(mm_mod, "is_storage_enabled", mock_enabled),
            patch.object(mm_mod, "upload_bytes", mock_upload),
            patch.object(mm_mod, "get_public_url", mock_pub),
        ):
            meta = await mm_mod.build_attachment_metadata(
                TurnAttachments([inline_att, staged_att]), thread_id="t-42"
            )

        assert meta[0]["url"].startswith(
            "https://cdn.test/private-media/attachments/t-42/"
        )
        assert meta[0]["url"].endswith("/chart.png")
        assert (
            meta[1]["url"]
            == "https://cdn.test/private-media/attachments/att-1/report.png"
        )
        mock_upload.assert_called_once()


# ---------------------------------------------------------------------------
# 5. Dual-profile Caddyfile (/storage/avatars/* read-only proxy, rest 404)
# ---------------------------------------------------------------------------


class TestDualProfileCaddyfileStorageProxy:
    @pytest.mark.parametrize(
        ("caddyfile_rel", "expected_bucket"),
        [
            ("deploy/dalton/Caddyfile", "dalton-main"),
            ("deploy/standalone/Caddyfile", "dalton-storage"),
        ],
    )
    def test_storage_avatars_readonly_proxy_and_rest_404(
        self, caddyfile_rel: str, expected_bucket: str
    ) -> None:
        text = (REPO_ROOT / caddyfile_rel).read_text(encoding="utf-8")

        avatars_idx = text.index("@storage_avatars {")
        blocked_idx = text.index("@storage_blocked path /storage /storage/*")
        backend_idx = text.index("@backend_routes path /api/* /ws/*")
        spa_idx = text.index("@spa_read {")

        # Ordering: after /api/*, before SPA fallback, and @storage_avatars precedes @storage_blocked.
        assert backend_idx < avatars_idx < blocked_idx < spa_idx

        avatars_block = text[avatars_idx:blocked_idx]
        assert "method GET HEAD" in avatars_block
        assert "path /storage/avatars/*" in avatars_block
        assert "not path /storage/avatars/" in avatars_block
        assert "uri query -versionId" in avatars_block
        assert "uri strip_prefix /storage" in avatars_block
        assert f"rewrite * /{expected_bucket}{{path}}" in avatars_block
        assert "reverse_proxy minio:9000" in avatars_block
        assert "request_header -Authorization" in avatars_block
        assert "request_header -Cookie" in avatars_block
        assert "header_up -Authorization" in avatars_block
        assert "header_up -Cookie" in avatars_block

        # Blocked handler responds 404 for all other /storage/* paths & methods
        next_comment_idx = text.index("# 2.", blocked_idx)
        blocked_block = text[blocked_idx:next_comment_idx]
        assert "handle @storage_blocked" in blocked_block
        assert "respond 404" in blocked_block

        # Access log keeps dl_token redaction from Step 2
        assert "replace dl_token REDACTED" in text

    def test_standalone_caddyfile_introduces_no_new_env_placeholders(self) -> None:
        caddy_vars: set[str] = set()
        for caddy_path in [
            REPO_ROOT / "deploy/standalone/Caddyfile",
            *(REPO_ROOT / "deploy/standalone/modes").glob("*.caddy"),
        ]:
            content = caddy_path.read_text(encoding="utf-8")
            for m in re.finditer(r"\{\$([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}", content):
                caddy_vars.add(m.group(1))
        assert caddy_vars == {"DOMAIN", "DALTON_TLS_MODE", "DALTON_EDGE_HTTPS_PORT"}


# ---------------------------------------------------------------------------
# 6. Public share streaming integration in share_files.py
# ---------------------------------------------------------------------------


class TestPublicShareStreamingRoute:
    def _target(
        self,
        *,
        workspace_id: str = "ws-share-1",
        owner_id: str = "owner-1",
        root_path: str = "",
    ) -> share_access.SharedFileTarget:
        return share_access.SharedFileTarget(
            workspace={
                "workspace_id": workspace_id,
                "user_id": owner_id,
                "dir_name": None,
            },
            scope=ShareScope(workspace_id, root_path),
            work_dir="/home/workspace",
        )

    @pytest.mark.parametrize(
        ("filename", "mime", "raw_payload"),
        [
            (
                "report_150m.txt",
                "text/plain",
                b"header secret=SUPER_SECRET_TOKEN_999 tail",
            ),
            (
                "dump_150m.bin",
                "application/octet-stream",
                b"\x00\xff\xfeBIN_HEAD SUPER_SECRET_TOKEN_999 \x80\x81BIN_TAIL",
            ),
        ],
    )
    def test_large_text_and_binary_files_stream_through_redactor_with_no_store(
        self, filename: str, mime: str, raw_payload: bytes
    ) -> None:
        app = _make_share_app()
        client = TestClient(app)
        target = self._target()
        file_size = SERVE_MAX_BYTES + (50 << 20)  # 150 MiB, within (100 MiB, 256 MiB]
        assert SERVE_MAX_BYTES < file_size <= RELAY_MAX_BYTES

        file_meta = {
            "workspace_file_id": "wf-1",
            "file_path": f"reports/{filename}",
            "file_name": filename,
            "file_size": file_size,
            "content_hash": "e" * 64,
            "blob_sha256": "e" * 64,
            "pack_sha256": None,
            "pack_offset": None,
            "mime_type": mime,
            "is_binary": mime == "application/octet-stream",
            "inline_kind": None,
        }

        # Patch SERVE_MAX_BYTES to 16 so stream_stored_file executes its real
        # 8 MiB slice loop and size verification without allocating 150 MiB.
        small_meta = {**file_meta, "file_size": len(raw_payload)}

        async def _exact_range(_ws, _meta, offset: int, length: int, *, user_id: str, conn=None) -> bytes:
            assert user_id == "owner-1"
            return raw_payload[offset : offset + length]

        mock_resolve = create_autospec(
            share_files.resolve_shared_files, spec_set=True, return_value=target
        )
        mock_meta = create_autospec(
            wfs_mod.get_large_file_meta, spec_set=True, return_value=small_meta
        )
        mock_range = create_autospec(
            wfs_mod.resolve_stored_byte_range, spec_set=True, side_effect=_exact_range
        )
        mock_vault = create_autospec(
            wfs_mod.get_vault_secrets_for_redaction,
            spec_set=True,
            return_value={"API_SECRET": "SUPER_SECRET_TOKEN_999"},
        )
        mock_redactor = create_autospec(
            sr_mod.get_redactor, spec_set=True, return_value=_make_redactor([])
        )
        mock_shared_bytes = create_autospec(
            share_files._shared_file_bytes, spec_set=True
        )

        with (
            patch.object(share_files, "resolve_shared_files", mock_resolve),
            patch.object(share_files, "_shared_file_bytes", mock_shared_bytes),
            patch.object(wfs_mod, "SERVE_MAX_BYTES", 16),
            patch.object(wfs_mod, "get_large_file_meta", mock_meta),
            patch.object(wfs_mod, "resolve_stored_byte_range", mock_range),
            patch.object(wfs_mod, "get_vault_secrets_for_redaction", mock_vault),
            patch.object(sr_mod, "get_redactor", mock_redactor),
        ):
            resp = client.get(
                "/api/v1/public/shared/tok-ok/files/download",
                params={"path": f"reports/{filename}"},
            )

        assert resp.status_code == 200
        assert resp.headers.get("cache-control") == "no-store"
        assert filename in resp.headers.get("content-disposition", "")
        assert b"SUPER_SECRET_TOKEN_999" not in resp.content
        assert b"[REDACTED:API_SECRET]" in resp.content
        mock_resolve.assert_awaited_once_with("tok-ok", require_download=True)
        mock_shared_bytes.assert_not_awaited()

    def test_small_file_falls_through_to_upstream_shared_file_bytes(self) -> None:
        app = _make_share_app()
        client = TestClient(app)
        target = self._target()

        mock_resolve = create_autospec(
            share_files.resolve_shared_files, spec_set=True, return_value=target
        )
        mock_large = create_autospec(
            wfs_mod.shared_large_download, spec_set=True, return_value=None
        )
        mock_shared_bytes = create_autospec(
            share_files._shared_file_bytes,
            spec_set=True,
            return_value=share_files.SharedBytes(
                "notes.txt", b"hello SECRET_SMALL_999", "text/plain", "database"
            ),
        )
        mock_vault = create_autospec(
            share_files.get_vault_secrets_for_redaction,
            spec_set=True,
            return_value={"SMALL_KEY": "SECRET_SMALL_999"},
        )
        mock_redactor = create_autospec(
            share_files.get_redactor, spec_set=True, return_value=_make_redactor([])
        )

        with (
            patch.object(share_files, "resolve_shared_files", mock_resolve),
            patch.object(wfs_mod, "shared_large_download", mock_large),
            patch.object(share_files, "_shared_file_bytes", mock_shared_bytes),
            patch.object(share_files, "get_vault_secrets_for_redaction", mock_vault),
            patch.object(share_files, "get_redactor", mock_redactor),
        ):
            resp = client.get(
                "/api/v1/public/shared/tok-small/files/download",
                params={"path": "notes.txt"},
            )

        assert resp.status_code == 200
        assert resp.headers.get("cache-control") == "no-store"
        assert resp.content == b"hello [REDACTED:SMALL_KEY]"
        mock_large.assert_awaited_once_with(
            "ws-share-1",
            "owner-1",
            "notes.txt",
            not_found_detail=share_files._NOT_FOUND,
        )
        mock_shared_bytes.assert_awaited_once()

    def test_downgraded_allow_files_false_returns_403_with_no_store_and_clear_site_data(
        self,
    ) -> None:
        app = _make_share_app()
        client = TestClient(app)

        # Simulate legacy conflicting permissions {"allow_files": False, "allow_download": True}
        downgraded_thread = {
            "conversation_thread_id": "th-down-1",
            "workspace_id": "ws-share-1",
            "user_id": "owner-1",
            "is_shared": True,
            "share_permissions": {"allow_files": False, "allow_download": True},
        }
        mock_get_thread = create_autospec(
            share_access.get_shared_thread,
            spec_set=True,
            return_value=downgraded_thread,
        )
        mock_large = create_autospec(wfs_mod.shared_large_download, spec_set=True)

        with (
            patch.object(share_access, "get_shared_thread", mock_get_thread),
            patch.object(wfs_mod, "shared_large_download", mock_large),
        ):
            resp = client.get(
                "/api/v1/public/shared/tok-downgraded/files/download",
                params={"path": "reports/large_150m.bin"},
            )

        assert resp.status_code == 403
        assert resp.headers.get("cache-control") == "no-store"
        assert resp.headers.get("clear-site-data") == '"cache"'
        mock_large.assert_not_awaited()

    def test_revoked_or_invalid_token_returns_404_with_no_store(self) -> None:
        app = _make_share_app()
        client = TestClient(app)

        mock_get_thread = create_autospec(
            share_access.get_shared_thread,
            spec_set=True,
            side_effect=HTTPException(status_code=404, detail="Shared link not found"),
        )
        mock_large = create_autospec(wfs_mod.shared_large_download, spec_set=True)

        with (
            patch.object(share_access, "get_shared_thread", mock_get_thread),
            patch.object(wfs_mod, "shared_large_download", mock_large),
        ):
            resp = client.get(
                "/api/v1/public/shared/tok-revoked/files/download",
                params={"path": "reports/large_150m.bin"},
            )

        assert resp.status_code == 404
        assert resp.headers.get("cache-control") == "no-store"
        mock_large.assert_not_awaited()

    def test_invisible_or_traversal_path_rejected_before_streaming(self) -> None:
        app = _make_share_app()
        client = TestClient(app)
        scoped_target = self._target(root_path="public")

        mock_resolve = create_autospec(
            share_files.resolve_shared_files,
            spec_set=True,
            return_value=scoped_target,
        )
        mock_large = create_autospec(wfs_mod.shared_large_download, spec_set=True)

        with (
            patch.object(share_files, "resolve_shared_files", mock_resolve),
            patch.object(wfs_mod, "shared_large_download", mock_large),
        ):
            resp_hidden = client.get(
                "/api/v1/public/shared/tok-scoped/files/download",
                params={"path": "private/secret_150m.bin"},
            )
            resp_trav = client.get(
                "/api/v1/public/shared/tok-scoped/files/download",
                params={"path": "../etc/passwd"},
            )

        assert resp_hidden.status_code == 404
        assert resp_hidden.headers.get("cache-control") == "no-store"
        assert resp_trav.status_code == 404
        assert resp_trav.headers.get("cache-control") == "no-store"
        mock_large.assert_not_awaited()

    def test_first_chunk_storage_error_collapses_to_404_before_headers(self) -> None:
        app = _make_share_app()
        client = TestClient(app)
        target = self._target()
        file_size = SERVE_MAX_BYTES + (20 << 20)

        file_meta = {
            "workspace_file_id": "wf-err",
            "file_path": "reports/broken_120m.bin",
            "file_name": "broken_120m.bin",
            "file_size": file_size,
            "content_hash": "f" * 64,
            "blob_sha256": "f" * 64,
            "pack_sha256": None,
            "pack_offset": None,
            "mime_type": "application/octet-stream",
            "is_binary": True,
            "inline_kind": None,
        }

        mock_resolve = create_autospec(
            share_files.resolve_shared_files, spec_set=True, return_value=target
        )
        mock_meta = create_autospec(
            wfs_mod.get_large_file_meta, spec_set=True, return_value=file_meta
        )
        mock_range = create_autospec(
            wfs_mod.resolve_stored_byte_range,
            spec_set=True,
            side_effect=FileBytesUnavailable("MinIO read timeout"),
        )

        with (
            patch.object(share_files, "resolve_shared_files", mock_resolve),
            patch.object(wfs_mod, "get_large_file_meta", mock_meta),
            patch.object(wfs_mod, "resolve_stored_byte_range", mock_range),
        ):
            resp = client.get(
                "/api/v1/public/shared/tok-err/files/download",
                params={"path": "reports/broken_120m.bin"},
            )

        assert resp.status_code == 404
        assert resp.headers.get("cache-control") == "no-store"
        assert resp.json() == {"detail": share_files._NOT_FOUND}
