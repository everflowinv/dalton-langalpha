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
import importlib.util
import io
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

import boto3
import pytest
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage
from uvicorn.logging import AccessFormatter

from ptc_agent.agent.middleware import image_capture as mw_ic
from ptc_agent.core.paths import SandboxLayout
from src.dalton import download_ticket as dl_ticket_mod
from src.dalton import private_bucket_urls as pbu_mod
from src.dalton import share_media as sm_mod
from src.dalton import storage_bucket_rules as sbr_mod
from src.dalton import storage_config
from src.dalton import storage_refs as srefs_mod
from src.dalton import streaming_redactor as sr_mod
from src.dalton import workspace_file_stream as wfs_mod
from src.dalton.auth.dispatch import SensitiveQueryLogFilter
from src.observability.private_query import AccessLogQueryFilter
from src.server.app import share_access, share_files
from src.server.database import workspace_file as wf_db
from src.server.database.blob_keys import RELAY_MAX_BYTES
from src.server.models.additional_context import MultimodalContext
from src.server.models.chat_attachment import (
    StoredSource,
    TurnAttachment,
    TurnAttachments,
)
from src.server.services.history.replay.facts import apply_image_url_map
from src.server.services.history.replay.lines import line_of
from src.server.services.persistence import download_link
from src.server.services.persistence import image_capture as persist_ic
from src.server.services.persistence.resolve import (
    SERVE_MAX_BYTES,
    FileBytesUnavailable,
)
from src.server.services.persistence.transfer import ScanEntry
from src.server.services.served_paths import ShareScope
from src.server.services.workspace_file_containment import FileTooLargeToServe
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
    async def test_middleware_preserves_private_snapshots_for_tmp_and_workspace_and_serves_via_au1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DALTON_WSFILES_PUBLIC_BUCKET_URLS", raising=False)
        monkeypatch.setenv("DALTON_MEDIA_SIGNING_SECRET", "m" * 40)

        tmp_png = b"\x89PNG\r\n\x1a\ntmp-chart-snapshot"
        ws_png = b"\x89PNG\r\n\x1a\nws-chart-v1-snapshot"
        tmp_sha16 = hashlib.sha256(tmp_png).hexdigest()[:16]
        ws_sha16 = hashlib.sha256(ws_png).hexdigest()[:16]

        stored_objects: dict[str, bytes] = {}

        def _fake_upload(key: str, data: bytes, content_type: str | None = None) -> bool:
            stored_objects[key] = data
            return True

        sandbox = create_autospec(_SandboxSpec, instance=True)
        sandbox.normalize_path.side_effect = (
            lambda p, project=None: p if p.startswith("/") else f"/home/workspace/{p.lstrip('/')}"
        )
        sandbox.adownload_file_bytes.side_effect = (
            lambda p: tmp_png if p == "/tmp/chart.png" else ws_png
        )

        mock_enabled = create_autospec(
            mw_ic.is_storage_enabled, spec_set=True, return_value=True
        )
        mock_upload = create_autospec(
            mw_ic.upload_bytes, spec_set=True, side_effect=_fake_upload
        )
        mock_pub = create_autospec(
            mw_ic.get_public_url,
            spec_set=True,
            side_effect=lambda k: f"https://cdn.test/{k}",
        )

        mw = mw_ic.ImageCaptureMiddleware(session=SimpleNamespace(sandbox=sandbox))
        msg = AIMessage(
            content="Tmp: ![tmp](/tmp/chart.png) and Ws: ![ws](charts/rev.png)",
            id="ai-1",
        )

        async def _handler(_req):
            return ModelResponse(result=[msg])

        with (
            patch.object(mw_ic, "is_storage_enabled", mock_enabled),
            patch.object(mw_ic, "upload_bytes", mock_upload),
            patch.object(mw_ic, "get_public_url", mock_pub),
            patch.object(mw_ic.ImageCaptureMiddleware, "_thread_id", return_value="t-1"),
        ):
            out = await mw.awrap_model_call(None, _handler)

        recorded = mw_ic.recorded_images(out.result[0])
        expected_tmp_key = f"private-media/response-images/t-1/{tmp_sha16}/chart.png"
        expected_ws_key = f"private-media/response-images/t-1/{ws_sha16}/rev.png"
        assert recorded == {
            "/tmp/chart.png": expected_tmp_key,
            "charts/rev.png": expected_ws_key,
        }
        # Zero direct bucket URLs emitted.
        for ref in recorded.values():
            assert not ref.startswith("http")
            assert "/storage/" not in ref
        mock_pub.assert_not_called()

        # Backup reconciliation recognizes private-mode response-image refs.
        assert srefs_mod.is_main_bucket_response_image_url(expected_tmp_key, env={}) is True
        assert srefs_mod.extract_response_image_object_key(expected_tmp_key, env={}) == expected_tmp_key

        # Even if sandbox files are later deleted/overwritten, AU1 replay rewrites both
        # snapshots to backend media URLs and serving them returns the exact captured bytes.
        turn_items = [
            {
                "event": "message_chunk",
                "data": {
                    "id": "ai-1",
                    "content_type": "text",
                    "content": msg.content,
                    "turn_index": 0,
                },
            }
        ]
        apply_image_url_map(turn_items, recorded)
        replay_lines = [line_of(turn_items[0])]

        registry = sm_mod._collect_thread_media_registry(
            "t-1",
            owner_id="owner-1",
            response_rows=[{"turn_index": 0, "replay_facts": {"images": recorded}}],
        )
        mock_load_reg = create_autospec(
            sm_mod._load_thread_media_registry_from_db,
            spec_set=True,
            return_value=registry,
        )
        with patch.object(sm_mod, "_load_thread_media_registry_from_db", mock_load_reg):
            bindings = await sm_mod.resolve_thread_media_bindings(
                {"conversation_thread_id": "t-1", "user_id": "owner-1", "workspace_id": "ws-1"},
                [],
                [ln.item() for ln in replay_lines],
            )
        assert bindings is not None
        owner_lines = sm_mod.rewrite_lines_for_owner_media(
            replay_lines,
            thread_id="t-1",
            viewer_id="owner-1",
            bindings=bindings,
        )
        owner_text = owner_lines[0].item()["data"]["content"]
        assert "private-media/" not in owner_text
        assert "https://" not in owner_text
        owner_media_urls = re.findall(r"/api/v1/threads/t-1/media/[^\s)]+", owner_text)
        assert len(owner_media_urls) == 2

        public_items = sm_mod.rewrite_items_for_public_media(
            [ln.item() for ln in replay_lines],
            thread_id="t-1",
            share_token="tok-pub-1",
            bindings=bindings,
        )
        public_text = public_items[0]["data"]["content"]
        assert "private-media/" not in public_text
        public_media_urls = re.findall(r"/api/v1/public/shared/tok-pub-1/media/[^\s)]+", public_text)
        assert len(public_media_urls) == 2

        # Serve both owner and public AU1 media URLs through real FastAPI routers and assert exact PNG bytes!
        app = FastAPI()
        app.include_router(sm_mod.owner_media_router)
        app.include_router(sm_mod.public_media_router, prefix="/api/v1/public")
        client = TestClient(app)

        async def _fake_gate(thread_id: str, media_token: str):
            return sm_mod.verify_owner_media_token(media_token, expected_thread_id=thread_id), "owner-1"

        app.dependency_overrides[sm_mod._owner_media_caller_gate] = _fake_gate

        mock_read_bytes = create_autospec(
            sm_mod.read_controlled_object_bytes,
            spec_set=True,
            side_effect=lambda k: stored_objects.get(k),
        )
        mock_get_thread = create_autospec(
            share_access.get_shared_thread,
            spec_set=True,
            return_value={
                "conversation_thread_id": "t-1",
                "workspace_id": "ws-1",
                "user_id": "owner-1",
                "is_shared": True,
                "share_permissions": {"allow_files": True, "allow_download": True},
            },
        )
        from src.server.database import conversation as conv_db

        mock_db_thread = create_autospec(
            conv_db.get_thread_by_id,
            spec_set=True,
            return_value={"conversation_thread_id": "t-1", "workspace_id": "ws-1", "user_id": "owner-1"},
        )
        with (
            patch.object(sm_mod, "_load_thread_media_registry_from_db", mock_load_reg),
            patch.object(sm_mod, "read_controlled_object_bytes", mock_read_bytes),
            patch.object(conv_db, "get_thread_by_id", mock_db_thread),
            patch.object(share_access, "get_shared_thread", mock_get_thread),
        ):
            owner_bytes = {client.get(u).content for u in owner_media_urls}
            pub_resps = [client.get(u) for u in public_media_urls]

        assert owner_bytes == {tmp_png, ws_png}
        assert all(r.status_code == 200 and r.headers.get("cache-control") == "no-store" for r in pub_resps)
        assert {r.content for r in pub_resps} == {tmp_png, ws_png}

        # Live SSE run_end also rewrites private refs to AU1 owner media URLs.
        raw_frame = (
            "event: run_end\n"
            f"data: {json.dumps({'thread_id': 't-1', 'run_id': 'r-1', 'outcome': 'completed', 'images': recorded, 'images_by_message': {'ai-1': recorded}})}\n\n"
        )
        rewritten_frame = sm_mod.rewrite_live_run_end_sse_frame(
            raw_frame, thread_id="t-1", viewer_id="owner-1"
        )
        assert "private-media/" not in rewritten_frame
        payload = json.loads(rewritten_frame.splitlines()[1][len("data: ") :])
        assert payload["images"]["/tmp/chart.png"].startswith("/api/v1/threads/t-1/media/")
        assert payload["images_by_message"]["ai-1"]["charts/rev.png"].startswith("/api/v1/threads/t-1/media/")

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
    async def test_persistence_capture_images_preserves_snapshots_in_both_modes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sandbox = create_autospec(_SandboxSpec, instance=True)
        mock_enabled = create_autospec(
            persist_ic.is_storage_enabled, spec_set=True, return_value=True
        )
        private_key = f"private-media/response-images/t-1/{PNG_SHA16}/a.png"
        mock_cap = create_autospec(
            persist_ic.capture_sandbox_images,
            spec_set=True,
            side_effect=lambda _sb, _paths, _tid, project=None: (
                {"charts/a.png": f"https://cdn.test/{private_key}"}
                if pbu_mod.is_public_bucket_urls_enabled()
                else {"charts/a.png": private_key}
            ),
        )
        mock_held = create_autospec(
            persist_ic._capture_from_held_folder,
            spec_set=True,
            side_effect=lambda _sb, _paths, _tid, _ws: (
                {"charts/a.png": f"https://cdn.test/{private_key}"}
                if pbu_mod.is_public_bucket_urls_enabled()
                else {"charts/a.png": private_key}
            ),
        )

        # public_bucket_urls=False -> still delegates to capture private snapshots (zero direct URLs)
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
        assert res_direct == {"charts/a.png": private_key}
        assert res_held == {"charts/a.png": private_key}
        assert mock_cap.await_count == 1
        assert mock_held.await_count == 1

        # public_bucket_urls=True -> existing direct-URL delegation preserved
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
        assert res_held_on["charts/a.png"].startswith(
            "https://cdn.test/private-media/response-images/t-1/"
        )


# ---------------------------------------------------------------------------
# 4. Browser external-link containment: multimodal_context.py
# ---------------------------------------------------------------------------


class TestMultimodalContextPrivateBucketContainment:
    @pytest.mark.asyncio
    async def test_private_mode_stores_object_key_and_replay_thumbnails_are_readable_via_au1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DALTON_WSFILES_PUBLIC_BUCKET_URLS", raising=False)
        monkeypatch.setenv("DALTON_MEDIA_SIGNING_SECRET", "m" * 40)

        inline_bytes = b"\x89PNG\r\n\x1a\ninline-thumb-bytes"
        staged_bytes = b"\x89PNG\r\n\x1a\nstaged-thumb-bytes"
        import base64

        inline_att = mm_mod.inline_attachment(
            MultimodalContext(
                type="image",
                data=f"data:image/png;base64,{base64.b64encode(inline_bytes).decode()}",
                description="chart",
            )
        )
        assert inline_att is not None

        async def _load_staged() -> bytes | None:
            return staged_bytes

        staged_key = "private-media/attachments/att-1/report.png"
        staged_att = TurnAttachment(
            name="report.png",
            mime_type="image/png",
            size=len(staged_bytes),
            source=StoredSource(
                attachment_id="att-1",
                object_key=staged_key,
                load=_load_staged,
            ),
        )

        stored_objects: dict[str, bytes] = {staged_key: staged_bytes}

        def _fake_upload(key: str, data: bytes, content_type: str | None = None) -> bool:
            stored_objects[key] = data
            return True

        mock_enabled = create_autospec(
            mm_mod.is_storage_enabled, spec_set=True, return_value=True
        )
        mock_upload = create_autospec(
            mm_mod.upload_bytes, spec_set=True, side_effect=_fake_upload
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

        assert len(meta) == 2
        assert "url" not in meta[0]
        assert "url" not in meta[1]
        assert meta[0]["object_key"].startswith("private-media/attachments/t-42/")
        assert meta[0]["object_key"].endswith("/chart.png")
        assert meta[1]["object_key"] == staged_key
        assert meta[1]["attachment_id"] == "att-1"
        mock_upload.assert_called_once()
        mock_pub.assert_not_called()

        # Backup reconciliation extracts both private-mode attachment object keys.
        assert srefs_mod.extract_attachment_object_key(meta[0]["object_key"], env={}) == meta[0]["object_key"]
        assert srefs_mod.extract_attachment_object_key(meta[1]["object_key"], env={}) == staged_key

        # Replay (both owner & allow_files=True public share) rewrites object_key -> AU1 media URL
        # and strips raw object_key from browser payload.
        query_row = {"turn_index": 0, "user_id": "owner-1", "metadata": {"attachments": meta}}
        user_msg_item = {
            "event": "user_message",
            "data": {
                "thread_id": "t-42",
                "turn_index": 0,
                "content": "Check these charts",
                "metadata": {"attachments": meta},
            },
        }
        registry = sm_mod._collect_thread_media_registry(
            "t-42", owner_id="owner-1", queries=[query_row]
        )
        mock_load_reg = create_autospec(
            sm_mod._load_thread_media_registry_from_db,
            spec_set=True,
            return_value=registry,
        )
        mock_verify_staged = create_autospec(
            sm_mod._verify_staged_attachment_owner,
            spec_set=True,
            return_value=True,
        )
        with (
            patch.object(sm_mod, "_load_thread_media_registry_from_db", mock_load_reg),
            patch.object(sm_mod, "_verify_staged_attachment_owner", mock_verify_staged),
        ):
            bindings = await sm_mod.resolve_thread_media_bindings(
                {"conversation_thread_id": "t-42", "user_id": "owner-1", "workspace_id": "ws-42"},
                [query_row],
                [user_msg_item],
            )
        assert bindings is not None

        owner_lines = sm_mod.rewrite_lines_for_owner_media(
            [line_of(user_msg_item)],
            thread_id="t-42",
            viewer_id="owner-1",
            bindings=bindings,
        )
        owner_atts = owner_lines[0].item()["data"]["metadata"]["attachments"]
        assert all("object_key" not in a for a in owner_atts)
        assert all(a["url"].startswith("/api/v1/threads/t-42/media/") for a in owner_atts)

        pub_items = sm_mod.rewrite_items_for_public_media(
            [user_msg_item],
            thread_id="t-42",
            share_token="tok-pub-42",
            bindings=bindings,
        )
        pub_atts = pub_items[0]["data"]["metadata"]["attachments"]
        assert all("object_key" not in a for a in pub_atts)
        assert all(a["url"].startswith("/api/v1/public/shared/tok-pub-42/media/") for a in pub_atts)

        # Actually fetch every thumbnail URL through AU1 FastAPI routes and assert exact PNG bytes!
        app = FastAPI()
        app.include_router(sm_mod.owner_media_router)
        app.include_router(sm_mod.public_media_router, prefix="/api/v1/public")
        client = TestClient(app)

        async def _fake_gate(thread_id: str, media_token: str):
            return sm_mod.verify_owner_media_token(media_token, expected_thread_id=thread_id), "owner-1"

        app.dependency_overrides[sm_mod._owner_media_caller_gate] = _fake_gate

        mock_read_bytes = create_autospec(
            sm_mod.read_controlled_object_bytes,
            spec_set=True,
            side_effect=lambda k: stored_objects.get(k),
        )
        from src.server.database import conversation as conv_db

        mock_db_thread = create_autospec(
            conv_db.get_thread_by_id,
            spec_set=True,
            return_value={"conversation_thread_id": "t-42", "workspace_id": "ws-42", "user_id": "owner-1"},
        )
        mock_get_shared = create_autospec(
            share_access.get_shared_thread,
            spec_set=True,
            return_value={
                "conversation_thread_id": "t-42",
                "workspace_id": "ws-42",
                "user_id": "owner-1",
                "is_shared": True,
                "share_permissions": {"allow_files": True, "allow_download": True},
            },
        )
        with (
            patch.object(sm_mod, "_load_thread_media_registry_from_db", mock_load_reg),
            patch.object(sm_mod, "_verify_staged_attachment_owner", mock_verify_staged),
            patch.object(sm_mod, "read_controlled_object_bytes", mock_read_bytes),
            patch.object(conv_db, "get_thread_by_id", mock_db_thread),
            patch.object(share_access, "get_shared_thread", mock_get_shared),
        ):
            owner_inline = client.get(owner_atts[0]["url"])
            owner_staged = client.get(owner_atts[1]["url"])
            pub_inline = client.get(pub_atts[0]["url"])
            pub_staged = client.get(pub_atts[1]["url"])

        assert owner_inline.status_code == 200 and owner_inline.content == inline_bytes
        assert owner_staged.status_code == 200 and owner_staged.content == staged_bytes
        assert pub_inline.status_code == 200 and pub_inline.content == inline_bytes
        assert pub_staged.status_code == 200 and pub_staged.content == staged_bytes
        assert owner_inline.headers.get("content-type", "").startswith("image/png")
        assert pub_staged.headers.get("cache-control") == "no-store"

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
    def test_storage_avatars_readonly_proxy_strips_all_query_params_and_uses_configured_bucket_and_prefix(
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
        assert "{$STORAGE_KEY_PREFIX:}" in avatars_block
        assert '/storage/avatars/' in avatars_block
        assert "uri strip_prefix /storage" in avatars_block
        # Trailing `?` clears ALL query params (versionId, response-content-type, etc.).
        assert f"rewrite * /{{$STORAGE_BUCKET_NAME:{expected_bucket}}}{{path}}?" in avatars_block
        assert "uri query -versionId" not in avatars_block
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

        # Access log keeps dl_token redaction and adds path share-token redaction via nested filter
        assert "replace dl_token REDACTED" in text
        assert "wrap filter {" in text

    def test_caddyfile_access_log_regex_redacts_share_tokens_in_both_profiles(self) -> None:
        secret_tok = "tok-live-share-secret-777"
        for caddyfile_rel in ("deploy/dalton/Caddyfile", "deploy/standalone/Caddyfile"):
            text = (REPO_ROOT / caddyfile_rel).read_text(encoding="utf-8")
            uri_match = re.search(
                r"request>uri regexp `([^`]+)` `\$\{1\}REDACTED`", text
            )
            ref_match = re.search(
                r"request>headers>Referer regexp `([^`]+)` `\$\{1\}REDACTED`", text
            )
            assert uri_match is not None, f"missing request>uri regexp in {caddyfile_rel}"
            assert ref_match is not None, f"missing Referer regexp in {caddyfile_rel}"

            uri_re = re.compile(uri_match.group(1))
            ref_re = re.compile(ref_match.group(1))

            for raw_uri, expected in [
                (
                    f"/api/v1/public/shared/{secret_tok}/files/download?path=report.pdf",
                    "/api/v1/public/shared/REDACTED/files/download?path=report.pdf",
                ),
                (f"/api/v1/public/shared/{secret_tok}", "/api/v1/public/shared/REDACTED"),
                (f"/api/v1/public/shared/{secret_tok}/replay", "/api/v1/public/shared/REDACTED/replay"),
                (f"/s/{secret_tok}", "/s/REDACTED"),
                (f"/a/{secret_tok}?as=visitor", "/a/REDACTED?as=visitor"),
                (f"/api/v1/public/og-meta?path=/s/{secret_tok}", "/api/v1/public/og-meta?path=/s/REDACTED"),
                (f"/api/v1/public/og-meta?path=%2Fs%2F{secret_tok}", "/api/v1/public/og-meta?path=%2Fs%2FREDACTED"),
                ("/api/v1/workspaces/ws-1/files/download?path=a.txt", "/api/v1/workspaces/ws-1/files/download?path=a.txt"),
            ]:
                out = uri_re.sub(r"\1REDACTED", raw_uri)
                assert out == expected, f"{caddyfile_rel}: {raw_uri} -> {out}"
                assert secret_tok not in out

            ref_out = ref_re.sub(r"\1REDACTED", f"https://app.example.com/s/{secret_tok}")
            assert ref_out == "https://app.example.com/s/REDACTED"
            assert secret_tok not in ref_out

    def test_standalone_caddyfile_env_placeholders_wired_in_compose(self) -> None:
        caddy_vars: set[str] = set()
        for caddy_path in [
            REPO_ROOT / "deploy/standalone/Caddyfile",
            *(REPO_ROOT / "deploy/standalone/modes").glob("*.caddy"),
        ]:
            content = caddy_path.read_text(encoding="utf-8")
            for m in re.finditer(r"\{\$([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}", content):
                caddy_vars.add(m.group(1))
        assert caddy_vars == {
            "DOMAIN",
            "DALTON_TLS_MODE",
            "DALTON_EDGE_HTTPS_PORT",
            "STORAGE_BUCKET_NAME",
            "STORAGE_KEY_PREFIX",
        }
        core_compose = (REPO_ROOT / "docker-compose.core.yml").read_text(encoding="utf-8")
        dalton_compose = (REPO_ROOT / "docker-compose.dalton.yml").read_text(encoding="utf-8")
        for compose_text in (core_compose, dalton_compose):
            assert "STORAGE_BUCKET_NAME:" in compose_text
            assert "STORAGE_KEY_PREFIX:" in compose_text


class TestBackendAccessLogShareTokenRedaction:
    def test_actual_uvicorn_access_log_output_redacts_share_tokens_across_all_filters(self) -> None:
        secret_tok = "tok-backend-access-secret-888"
        cfg = dl_ticket_mod.build_uvicorn_log_config()
        assert "dalton_redact_dl_token" in cfg["filters"]
        assert "dalton_redact_dl_token" in cfg["loggers"]["uvicorn.access"]["filters"]

        for name, flt in [
            ("uvicorn_dl_filter", dl_ticket_mod.RedactDownloadTicketLogFilter()),
            ("sensitive_query_filter", SensitiveQueryLogFilter()),
            ("private_query_filter", AccessLogQueryFilter()),
        ]:
            buf = io.StringIO()
            log = logging.getLogger(f"test.uvicorn.access.share_tok.{name}")
            log.handlers.clear()
            log.filters.clear()
            log.setLevel(logging.INFO)
            log.propagate = False
            handler = logging.StreamHandler(buf)
            handler.setFormatter(
                AccessFormatter(
                    fmt='%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
                    use_colors=False,
                )
            )
            log.addHandler(handler)
            log.addFilter(flt)

            for target in (
                f"/api/v1/public/shared/{secret_tok}/files/download?path=reports/150m.csv",
                f"/api/v1/public/shared/{secret_tok}/replay",
                f"/api/v1/public/shared/{secret_tok}",
                f"/s/{secret_tok}",
                f"/a/{secret_tok}",
                f"/api/v1/public/og-meta?path=/s/{secret_tok}",
            ):
                log.info(
                    '%s - "%s %s HTTP/%s" %d',
                    "127.0.0.1:41234",
                    "GET",
                    target,
                    "1.1",
                    200,
                )

            rendered = buf.getvalue()
            assert secret_tok not in rendered, f"{name} leaked share token: {rendered}"
            assert "/api/v1/public/shared/REDACTED/files/download?path=reports/150m.csv" in rendered
            assert "/api/v1/public/shared/REDACTED/replay" in rendered
            assert "/s/REDACTED" in rendered
            assert "/a/REDACTED" in rendered
            assert "/api/v1/public/og-meta?path=/s/REDACTED" in rendered


class TestStandaloneMinioInitAvatarsPolicy:
    @staticmethod
    def _load_minio_init():
        spec = importlib.util.spec_from_file_location(
            "minio_init_s4_test", REPO_ROOT / "scripts/standalone/minio_init.py"
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_minio_init_grants_only_current_version_avatar_get_object_for_configured_bucket_and_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        minio_init = self._load_minio_init()
        monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL", "http://minio:9000")
        monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME", "chat-uploads")
        monkeypatch.setenv("MINIO_ROOT_USER", "ak")
        monkeypatch.setenv("MINIO_ROOT_PASSWORD", "sk")
        monkeypatch.setenv("STORAGE_PROVIDER", "s3")
        monkeypatch.setenv("STORAGE_ENDPOINT_URL", "http://minio:9000")
        monkeypatch.setenv("STORAGE_BUCKET_NAME", "corp-custom-main")
        monkeypatch.setenv("STORAGE_KEY_PREFIX", "team-alpha")
        monkeypatch.delenv("DALTON_ASR_STORAGE_BUCKET_NAME", raising=False)

        stored_policies: dict[str, str] = {}
        s3_spec = boto3.client(
            "s3",
            endpoint_url="http://minio:9000",
            aws_access_key_id="ak",
            aws_secret_access_key="sk",
            region_name="us-east-1",
        )
        fake_s3 = create_autospec(s3_spec)
        fake_s3.put_bucket_policy.side_effect = (
            lambda *, Bucket, Policy: stored_policies.__setitem__(Bucket, Policy) or {}
        )
        fake_s3.get_bucket_policy.side_effect = (
            lambda *, Bucket: {"Policy": stored_policies[Bucket]}
        )

        monkeypatch.setattr(
            minio_init.boto3,
            "client",
            create_autospec(boto3.session.Session().client, return_value=fake_s3),
        )
        assert minio_init.main() == 0

        # Chat uploads stays strictly private (no policy put).
        assert "chat-uploads" not in stored_policies
        # Main bucket has single-statement anonymous s3:GetObject ONLY on <prefix>avatars/*
        assert "corp-custom-main" in stored_policies
        policy_doc = json.loads(stored_policies["corp-custom-main"])
        sbr_mod.verify_bucket_policy(
            policy_doc,
            bucket="corp-custom-main",
            role="main",
            key_prefix="team-alpha",
        )
        stmt = policy_doc["Statement"][0]
        assert stmt["Action"] == ["s3:GetObject"]
        assert "s3:GetObjectVersion" not in stmt["Action"]
        assert stmt["Resource"] == ["arn:aws:s3:::corp-custom-main/team-alpha/avatars/*"]

        # Verify uploader key matches the initialized bucket policy and Caddy rewrite path.
        assert f"arn:aws:s3:::corp-custom-main/team-alpha/avatars/u-1.png".startswith(
            stmt["Resource"][0][:-1]
        )

    def test_minio_init_skips_avatars_policy_when_storage_provider_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        minio_init = self._load_minio_init()
        monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL", "http://minio:9000")
        monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME", "chat-uploads")
        monkeypatch.setenv("MINIO_ROOT_USER", "ak")
        monkeypatch.setenv("MINIO_ROOT_PASSWORD", "sk")
        monkeypatch.setenv("STORAGE_PROVIDER", "none")
        monkeypatch.setenv("STORAGE_BUCKET_NAME", "corp-custom-main")
        monkeypatch.delenv("DALTON_ASR_STORAGE_BUCKET_NAME", raising=False)

        s3_spec = boto3.client(
            "s3",
            endpoint_url="http://minio:9000",
            aws_access_key_id="ak",
            aws_secret_access_key="sk",
            region_name="us-east-1",
        )
        fake_s3 = create_autospec(s3_spec)
        monkeypatch.setattr(
            minio_init.boto3,
            "client",
            create_autospec(boto3.session.Session().client, return_value=fake_s3),
        )
        assert minio_init.main() == 0
        fake_s3.put_bucket_policy.assert_not_called()


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

    # --- Warm-sandbox-first semantics (Codex High + Grok High) ---

    def test_warm_sandbox_deleted_file_returns_404_without_falling_back_to_large_mirror(
        self,
    ) -> None:
        app = _make_share_app()
        client = TestClient(app)
        target = self._target()
        sandbox = create_autospec(_SandboxSpec, instance=True)

        mock_resolve = create_autospec(
            share_files.resolve_shared_files, spec_set=True, return_value=target
        )
        mock_warm = create_autospec(
            share_files.warm_sandbox, spec_set=True, return_value=sandbox
        )
        mock_warm_bytes = create_autospec(
            share_files.warm_sandbox_bytes, spec_set=True, return_value=None
        )
        mock_large = create_autospec(wfs_mod.shared_large_download, spec_set=True)

        with (
            patch.object(share_files, "resolve_shared_files", mock_resolve),
            patch.object(share_files, "warm_sandbox", mock_warm),
            patch.object(share_files, "warm_sandbox_bytes", mock_warm_bytes),
            patch.object(wfs_mod, "shared_large_download", mock_large),
        ):
            resp = client.get(
                "/api/v1/public/shared/tok-warm-del/files/download",
                params={"path": "reports/deleted_150m.bin"},
            )

        assert resp.status_code == 404
        assert resp.headers.get("cache-control") == "no-store"
        mock_warm_bytes.assert_awaited_once()
        mock_large.assert_not_awaited()

    def test_warm_sandbox_replaced_small_file_serves_live_bytes_not_stale_large_mirror(
        self,
    ) -> None:
        app = _make_share_app()
        client = TestClient(app)
        target = self._target()
        sandbox = create_autospec(_SandboxSpec, instance=True)

        mock_resolve = create_autospec(
            share_files.resolve_shared_files, spec_set=True, return_value=target
        )
        mock_warm = create_autospec(
            share_files.warm_sandbox, spec_set=True, return_value=sandbox
        )
        mock_warm_bytes = create_autospec(
            share_files.warm_sandbox_bytes,
            spec_set=True,
            return_value=("reports/replaced.txt", b"live replaced SECRET_LIVE_123"),
        )
        mock_unmoved = create_autospec(
            share_files.folder_unmoved, spec_set=True, return_value=True
        )
        mock_large = create_autospec(wfs_mod.shared_large_download, spec_set=True)
        mock_vault = create_autospec(
            share_files.get_vault_secrets_for_redaction,
            spec_set=True,
            return_value={"LIVE_KEY": "SECRET_LIVE_123"},
        )
        mock_redactor = create_autospec(
            share_files.get_redactor, spec_set=True, return_value=_make_redactor([])
        )

        with (
            patch.object(share_files, "resolve_shared_files", mock_resolve),
            patch.object(share_files, "warm_sandbox", mock_warm),
            patch.object(share_files, "warm_sandbox_bytes", mock_warm_bytes),
            patch.object(share_files, "folder_unmoved", mock_unmoved),
            patch.object(wfs_mod, "shared_large_download", mock_large),
            patch.object(share_files, "get_vault_secrets_for_redaction", mock_vault),
            patch.object(share_files, "get_redactor", mock_redactor),
        ):
            resp = client.get(
                "/api/v1/public/shared/tok-warm-rep/files/download",
                params={"path": "reports/replaced.txt"},
            )

        assert resp.status_code == 200
        assert resp.headers.get("cache-control") == "no-store"
        assert resp.content == b"live replaced [REDACTED:LIVE_KEY]"
        mock_large.assert_not_awaited()

    def test_warm_sandbox_replaced_large_file_with_different_size_returns_404(
        self,
    ) -> None:
        app = _make_share_app()
        client = TestClient(app)
        target = self._target()
        sandbox = create_autospec(_SandboxSpec, instance=True)
        live_size = SERVE_MAX_BYTES + (20 << 20)   # 120 MiB on disk
        stale_size = SERVE_MAX_BYTES + (50 << 20)  # 150 MiB in stale mirror

        mock_resolve = create_autospec(
            share_files.resolve_shared_files, spec_set=True, return_value=target
        )
        mock_warm = create_autospec(
            share_files.warm_sandbox, spec_set=True, return_value=sandbox
        )
        mock_warm_bytes = create_autospec(
            share_files.warm_sandbox_bytes,
            spec_set=True,
            side_effect=FileTooLargeToServe(
                "/home/workspace/reports/replaced_150m.bin", live_size
            ),
        )
        mock_unmoved = create_autospec(
            share_files.folder_unmoved, spec_set=True, return_value=True
        )
        mock_meta = create_autospec(
            wfs_mod.get_large_file_meta,
            spec_set=True,
            return_value={
                "workspace_file_id": "wf-stale",
                "file_path": "reports/replaced_150m.bin",
                "file_name": "replaced_150m.bin",
                "file_size": stale_size,
                "blob_sha256": "a" * 64,
                "mime_type": "application/octet-stream",
                "is_binary": True,
                "inline_kind": None,
            },
        )
        mock_range = create_autospec(wfs_mod.resolve_stored_byte_range, spec_set=True)

        with (
            patch.object(share_files, "resolve_shared_files", mock_resolve),
            patch.object(share_files, "warm_sandbox", mock_warm),
            patch.object(share_files, "warm_sandbox_bytes", mock_warm_bytes),
            patch.object(share_files, "folder_unmoved", mock_unmoved),
            patch.object(wfs_mod, "get_large_file_meta", mock_meta),
            patch.object(wfs_mod, "resolve_stored_byte_range", mock_range),
        ):
            resp = client.get(
                "/api/v1/public/shared/tok-warm-rep-lg/files/download",
                params={"path": "reports/replaced_150m.bin"},
            )

        assert resp.status_code == 404
        assert resp.headers.get("cache-control") == "no-store"
        mock_range.assert_not_awaited()

    def test_warm_sandbox_symlink_to_invisible_large_target_returns_404_not_mirror(
        self,
    ) -> None:
        app = _make_share_app()
        client = TestClient(app)
        # Share is scoped to "public/"; symlink public/link_150m.bin -> private/secret_150m.bin.
        scoped_target = self._target(root_path="public")

        class _SymlinkSandboxSpec(_SandboxSpec):
            def validate_path(self, path: str) -> bool:
                return True

        sandbox = create_autospec(_SymlinkSandboxSpec, instance=True)
        sandbox.validate_path.return_value = True

        mock_resolve = create_autospec(
            share_files.resolve_shared_files, spec_set=True, return_value=scoped_target
        )
        mock_warm = create_autospec(
            share_files.warm_sandbox, spec_set=True, return_value=sandbox
        )
        from src.server.services import warm_files as wf_mod

        mock_read_contained = create_autospec(
            wf_mod.read_contained_sandbox_file,
            spec_set=True,
            side_effect=FileTooLargeToServe(
                "/home/workspace/private/secret_150m.bin", SERVE_MAX_BYTES + (50 << 20)
            ),
        )
        mock_large = create_autospec(wfs_mod.shared_large_download, spec_set=True)

        with (
            patch.object(share_files, "resolve_shared_files", mock_resolve),
            patch.object(share_files, "warm_sandbox", mock_warm),
            patch.object(wf_mod, "read_contained_sandbox_file", mock_read_contained),
            patch.object(wfs_mod, "shared_large_download", mock_large),
        ):
            resp = client.get(
                "/api/v1/public/shared/tok-warm-sym/files/download",
                params={"path": "public/link_150m.bin"},
            )

        assert resp.status_code == 404
        assert resp.headers.get("cache-control") == "no-store"
        mock_large.assert_not_awaited()

    def test_warm_sandbox_visible_large_file_streams_matching_mirror_with_redaction(
        self,
    ) -> None:
        app = _make_share_app()
        client = TestClient(app)
        target = self._target()
        sandbox = create_autospec(_SandboxSpec, instance=True)
        raw_payload = b"warm large body SECRET_WARM_999 end"

        mock_resolve = create_autospec(
            share_files.resolve_shared_files, spec_set=True, return_value=target
        )
        mock_warm = create_autospec(
            share_files.warm_sandbox, spec_set=True, return_value=sandbox
        )
        mock_warm_bytes = create_autospec(
            share_files.warm_sandbox_bytes,
            spec_set=True,
            side_effect=FileTooLargeToServe(
                "/home/workspace/reports/ok_150m.txt", len(raw_payload)
            ),
        )
        mock_unmoved = create_autospec(
            share_files.folder_unmoved, spec_set=True, return_value=True
        )
        mock_meta = create_autospec(
            wfs_mod.get_large_file_meta,
            spec_set=True,
            return_value={
                "workspace_file_id": "wf-ok",
                "file_path": "reports/ok_150m.txt",
                "file_name": "ok_150m.txt",
                "file_size": len(raw_payload),
                "blob_sha256": "b" * 64,
                "mime_type": "text/plain",
                "is_binary": False,
                "inline_kind": None,
            },
        )

        async def _exact_range(_ws, _meta, offset: int, length: int, *, user_id: str, conn=None) -> bytes:
            return raw_payload[offset : offset + length]

        mock_range = create_autospec(
            wfs_mod.resolve_stored_byte_range, spec_set=True, side_effect=_exact_range
        )
        mock_vault = create_autospec(
            wfs_mod.get_vault_secrets_for_redaction,
            spec_set=True,
            return_value={"WARM_KEY": "SECRET_WARM_999"},
        )
        mock_redactor = create_autospec(
            sr_mod.get_redactor, spec_set=True, return_value=_make_redactor([])
        )

        with (
            patch.object(share_files, "resolve_shared_files", mock_resolve),
            patch.object(share_files, "warm_sandbox", mock_warm),
            patch.object(share_files, "warm_sandbox_bytes", mock_warm_bytes),
            patch.object(share_files, "folder_unmoved", mock_unmoved),
            patch.object(wfs_mod, "SERVE_MAX_BYTES", 16),
            patch.object(wfs_mod, "get_large_file_meta", mock_meta),
            patch.object(wfs_mod, "resolve_stored_byte_range", mock_range),
            patch.object(wfs_mod, "get_vault_secrets_for_redaction", mock_vault),
            patch.object(sr_mod, "get_redactor", mock_redactor),
        ):
            resp = client.get(
                "/api/v1/public/shared/tok-warm-ok/files/download",
                params={"path": "reports/ok_150m.txt"},
            )

        assert resp.status_code == 200
        assert resp.headers.get("cache-control") == "no-store"
        assert b"SECRET_WARM_999" not in resp.content
        assert b"[REDACTED:WARM_KEY]" in resp.content
