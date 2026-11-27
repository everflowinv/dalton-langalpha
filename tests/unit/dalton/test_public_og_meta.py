"""Unit tests for public share OpenGraph metadata endpoint and Caddy gateway config."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.dalton import share_redaction
from src.dalton.app import dalton_router, public_og
from src.dalton.settings import get_brand_name
from src.server.app import share_access
from src.server.app.share_access import LinkAccess, shared_thread_allows_files
from src.server.database import artifacts as artifacts_db
from src.server.database.artifacts.records import Artifact
from src.server.database.artifacts.versions import Version
from src.server.database.conversation import get_thread_by_share_token
from src.server.database.share_links import KIND_APP, KIND_FILE, ShareLink
from src.server.services.account import serves_public_links
from src.server.services.share_audience import LINK_NOT_FOUND, ORGANIZATION, MEMBERS_ONLY

REPO_ROOT = Path(__file__).resolve().parents[3]


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(dalton_router)
    return TestClient(app)


def _make_artifact(
    *,
    code: str = "AbCdEf123456",
    path: str = "reports/NVDA_Q3_Memo.md",
    shared: bool = True,
    shared_seq: int = 1,
    audience: str | None = None,
    legacy_refused: str | None = None,
) -> Artifact:
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    return Artifact(kind="file", 
        artifact_id="art-1",
        code=code,
        workspace_id="ws-1",
        path=path,
        created_thread_id=None,
        bound_thread_id=None,
        keep_updated=False,
        latest_seq=shared_seq,
        shared_at=now if shared else None,
        files=(path,) if shared else None,
        revision=1,
        created_at=now,
        legacy_refused=legacy_refused,
        shared_seq=shared_seq,
        audience=audience,
    )


def _make_file_access(
    *,
    code: str = "AbCdEf123456",
    path: str = "reports/NVDA_Q3_Memo.md",
    shared: bool = True,
    shared_seq: int = 1,
    audience: str | None = None,
    legacy_refused: str | None = None,
    kind: str = KIND_FILE,
) -> LinkAccess:
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    artifact = (
        _make_artifact(
            code=code,
            path=path,
            shared=shared,
            shared_seq=shared_seq,
            audience=audience,
            legacy_refused=legacy_refused,
        )
        if kind == KIND_FILE
        else None
    )
    link = ShareLink(
        code=code,
        workspace_id="ws-1",
        kind=kind,  # type: ignore[arg-type]
        path=path if kind == KIND_FILE else None,
        port=None if kind == KIND_FILE else 3000,
        title=None if kind == KIND_FILE else "Private App",
        files=(path,) if shared and kind == KIND_FILE else None,
        shared_at=now if shared and kind == KIND_FILE else None,
        created_at=now,
        revision=1,
        artifact=artifact,
    )
    return LinkAccess(
        link=link, workspace={"workspace_id": "ws-1", "user_id": "owner-1"}, owner=False
    )


def _make_version(
    *,
    seq: int = 1,
    entry_path: str = "reports/NVDA_Q3_Memo.md",
    title: str | None = None,
) -> Version:
    return Version(
        seq=seq,
        entry_path=entry_path,
        origin="owner",
        title=title,
        label=None,
        byte_len=128,
        published_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
    )


def _extract_meta(body: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for prop, content in re.findall(
        r'<meta\s+property="([^"]+)"\s+content="([^"]*)"\s*/?>', body
    ):
        out.setdefault(prop, []).append(content)
    return out


class TestPublicOgMetaEndpoint:
    def test_router_registered_on_dalton_router(self) -> None:
        paths = {getattr(r, "path", None) for r in dalton_router.routes}
        assert "/api/v1/public/og-meta" in paths

    def test_public_thread_allow_files_true_returns_thread_title(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_resolve_link = create_autospec(public_og._resolve_link, return_value=None)
        mock_get_thread = create_autospec(
            get_thread_by_share_token,
            return_value={
                "conversation_thread_id": "t-1",
                "title": "英伟达 Q3 财报深度分析",
                "workspace_name": "NVDA",
                "owner_id": "owner-1",
                "share_permissions": {"allow_files": True},
            },
        )
        mock_serves_public = create_autospec(serves_public_links, return_value=True)
        mock_allows_files = create_autospec(shared_thread_allows_files, return_value=True)
        mock_brand = create_autospec(get_brand_name, return_value="Everflow")

        monkeypatch.setattr(public_og, "_resolve_link", mock_resolve_link)
        monkeypatch.setattr(public_og, "get_thread_by_share_token", mock_get_thread)
        monkeypatch.setattr(public_og, "serves_public_links", mock_serves_public)
        monkeypatch.setattr(share_access, "shared_thread_allows_files", mock_allows_files)
        monkeypatch.setattr(public_og, "get_brand_name", mock_brand)

        client = _make_client()
        resp = client.get("/api/v1/public/og-meta", params={"path": "/s/threadToken123"})

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert resp.headers["cache-control"] == "no-store"
        metas = _extract_meta(resp.text)
        assert metas["og:site_name"] == ["Everflow"]
        assert metas["og:title"] == ["英伟达 Q3 财报深度分析"]
        assert metas["og:description"] == ["来自 Everflow 的分享"]
        mock_resolve_link.assert_awaited_once_with("threadToken123", user_id=None)
        mock_get_thread.assert_awaited_once_with("threadToken123")
        mock_allows_files.assert_awaited_once()

        # Empty thread title with allow_files=True falls back to workspace_name
        mock_get_thread.return_value = {
            "conversation_thread_id": "t-1b",
            "title": "   ",
            "workspace_name": "NVDA 研究空间",
            "owner_id": "owner-1",
            "share_permissions": {"allow_files": True},
        }
        resp_ws = client.get("/api/v1/public/og-meta", params={"path": "/s/threadToken123"})
        assert resp_ws.status_code == 200
        assert _extract_meta(resp_ws.text)["og:title"] == ["NVDA 研究空间"]

    def test_public_thread_allow_files_false_returns_brand_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_resolve_link = create_autospec(public_og._resolve_link, return_value=None)
        mock_get_thread = create_autospec(
            get_thread_by_share_token,
            return_value={
                "conversation_thread_id": "t-2",
                "title": "内部敏感标题-机密",
                "workspace_name": "SecretWS",
                "owner_id": "owner-1",
                "share_permissions": {"allow_files": False},
            },
        )
        mock_serves_public = create_autospec(serves_public_links, return_value=True)
        mock_allows_files = create_autospec(shared_thread_allows_files, return_value=True)
        mock_brand = create_autospec(get_brand_name, return_value="Everflow")

        monkeypatch.setattr(public_og, "_resolve_link", mock_resolve_link)
        monkeypatch.setattr(public_og, "get_thread_by_share_token", mock_get_thread)
        monkeypatch.setattr(public_og, "serves_public_links", mock_serves_public)
        monkeypatch.setattr(share_access, "shared_thread_allows_files", mock_allows_files)
        monkeypatch.setattr(public_og, "get_brand_name", mock_brand)

        client = _make_client()
        resp = client.get("/api/v1/public/og-meta", params={"path": "/s/noFilesTok1"})

        assert resp.status_code == 200
        assert _extract_meta(resp.text)["og:title"] == ["Everflow"]
        assert "内部敏感标题" not in resp.text
        assert "SecretWS" not in resp.text
        mock_allows_files.assert_not_awaited()

    def test_public_thread_perms_allow_files_but_policy_denies_returns_brand_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_resolve_link = create_autospec(public_og._resolve_link, return_value=None)
        mock_get_thread = create_autospec(
            get_thread_by_share_token,
            return_value={
                "conversation_thread_id": "t-3",
                "title": "非管理员历史分享标题",
                "workspace_name": "LegacyWS",
                "owner_id": "owner-non-admin",
                "share_permissions": {"allow_files": True},
            },
        )
        mock_serves_public = create_autospec(serves_public_links, return_value=True)
        mock_allows_files = create_autospec(shared_thread_allows_files, return_value=False)
        mock_brand = create_autospec(get_brand_name, return_value="Everflow")

        monkeypatch.setattr(public_og, "_resolve_link", mock_resolve_link)
        monkeypatch.setattr(public_og, "get_thread_by_share_token", mock_get_thread)
        monkeypatch.setattr(public_og, "serves_public_links", mock_serves_public)
        monkeypatch.setattr(share_access, "shared_thread_allows_files", mock_allows_files)
        monkeypatch.setattr(public_og, "get_brand_name", mock_brand)

        client = _make_client()
        resp = client.get("/api/v1/public/og-meta", params={"path": "/s/nonAdminTok1"})

        assert resp.status_code == 200
        assert _extract_meta(resp.text)["og:title"] == ["Everflow"]
        assert "非管理员历史分享标题" not in resp.text
        assert "LegacyWS" not in resp.text
        mock_allows_files.assert_awaited_once()

    def test_public_text_only_share_displays_au1_safe_title_from_visible_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        thread = {
            "conversation_thread_id": "t-safe-1",
            "title": "内部机密原始标题",
            "workspace_name": "SecretProject",
            "owner_id": "owner-1",
            "share_permissions": {"allow_files": False},
        }
        mock_resolve_link = create_autospec(public_og._resolve_link, return_value=None)
        mock_get_thread = create_autospec(get_thread_by_share_token, return_value=thread)
        mock_serves_public = create_autospec(serves_public_links, return_value=True)
        mock_eff_perms = create_autospec(
            share_redaction.resolve_effective_share_permissions,
            wraps=share_redaction.resolve_effective_share_permissions,
        )
        mock_meta_fields = create_autospec(
            share_redaction.resolve_public_metadata_fields,
            return_value={
                "title": "分析英伟达 Q3 数据中心营收增速",
                "workspace_name": "SecretProject",
                "root_dir": None,
                "previous_root_dirs": [],
                "dir_name": None,
            },
        )
        mock_brand = create_autospec(get_brand_name, return_value="Everflow")

        monkeypatch.setattr(public_og, "_resolve_link", mock_resolve_link)
        monkeypatch.setattr(public_og, "get_thread_by_share_token", mock_get_thread)
        monkeypatch.setattr(public_og, "serves_public_links", mock_serves_public)
        monkeypatch.setattr(
            share_redaction, "resolve_effective_share_permissions", mock_eff_perms
        )
        monkeypatch.setattr(
            share_redaction, "resolve_public_metadata_fields", mock_meta_fields
        )
        monkeypatch.setattr(public_og, "get_brand_name", mock_brand)

        client = _make_client()
        resp = client.get("/api/v1/public/og-meta", params={"path": "/s/textOnlySafe1"})

        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"
        metas = _extract_meta(resp.text)
        assert metas["og:title"] == ["分析英伟达 Q3 数据中心营收增速"]
        assert metas["og:site_name"] == ["Everflow"]
        assert "内部机密原始标题" not in resp.text
        assert "SecretProject" not in resp.text
        mock_eff_perms.assert_awaited_once_with(thread)
        mock_meta_fields.assert_awaited_once_with(thread, allow_files=False)

    def test_disabled_files_share_never_leaks_original_title_and_falls_back_when_au1_has_no_safe_title(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        thread_denied = {
            "conversation_thread_id": "t-denied-1",
            "title": "关闭文件权限的原始线程标题",
            "workspace_name": "PrivateResearchWS",
            "owner_id": "owner-non-admin",
            "share_permissions": {"allow_files": True},
        }
        mock_resolve_link = create_autospec(public_og._resolve_link, return_value=None)
        mock_get_thread = create_autospec(
            get_thread_by_share_token, return_value=thread_denied
        )
        mock_serves_public = create_autospec(serves_public_links, return_value=True)
        mock_allows_files = create_autospec(shared_thread_allows_files, return_value=False)
        mock_eff_perms = create_autospec(
            share_redaction.resolve_effective_share_permissions,
            wraps=share_redaction.resolve_effective_share_permissions,
        )
        mock_meta_fields = create_autospec(
            share_redaction.resolve_public_metadata_fields,
            return_value={
                "title": "对比两家半导体公司毛利率变化",
                "workspace_name": "PrivateResearchWS",
                "root_dir": None,
                "previous_root_dirs": [],
                "dir_name": None,
            },
        )
        mock_brand = create_autospec(get_brand_name, return_value="Everflow")

        monkeypatch.setattr(public_og, "_resolve_link", mock_resolve_link)
        monkeypatch.setattr(public_og, "get_thread_by_share_token", mock_get_thread)
        monkeypatch.setattr(public_og, "serves_public_links", mock_serves_public)
        monkeypatch.setattr(share_access, "shared_thread_allows_files", mock_allows_files)
        monkeypatch.setattr(
            share_redaction, "resolve_effective_share_permissions", mock_eff_perms
        )
        monkeypatch.setattr(
            share_redaction, "resolve_public_metadata_fields", mock_meta_fields
        )
        monkeypatch.setattr(public_og, "get_brand_name", mock_brand)

        client = _make_client()
        resp_safe = client.get("/api/v1/public/og-meta", params={"path": "/s/deniedFiles1"})
        assert resp_safe.status_code == 200
        assert _extract_meta(resp_safe.text)["og:title"] == ["对比两家半导体公司毛利率变化"]
        assert "关闭文件权限的原始线程标题" not in resp_safe.text
        assert "PrivateResearchWS" not in resp_safe.text
        mock_meta_fields.assert_awaited_with(thread_denied, allow_files=False)

        # When AU1 cannot derive a query-based title (returns DEFAULT_SHARED_TITLE), fall back to brand name.
        mock_meta_fields.return_value = {
            "title": share_redaction.DEFAULT_SHARED_TITLE,
            "workspace_name": "PrivateResearchWS",
            "root_dir": None,
            "previous_root_dirs": [],
            "dir_name": None,
        }
        resp_fallback = client.get(
            "/api/v1/public/og-meta", params={"path": "/s/deniedFiles1"}
        )
        assert resp_fallback.status_code == 200
        assert _extract_meta(resp_fallback.text)["og:title"] == ["Everflow"]
        assert "关闭文件权限的原始线程标题" not in resp_fallback.text
        assert "PrivateResearchWS" not in resp_fallback.text
        assert share_redaction.DEFAULT_SHARED_TITLE not in resp_fallback.text

    def test_public_file_share_uses_published_version_title_or_filename_stem(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        access_html = _make_file_access(
            code="HtmlCode1234", path="reports/index.html", shared_seq=3
        )
        mock_resolve_link = create_autospec(public_og._resolve_link, return_value=access_html)
        mock_get_version = create_autospec(
            artifacts_db.get_version,
            return_value=_make_version(
                seq=3, entry_path="reports/index.html", title="2026 半导体行业展望"
            ),
        )
        mock_brand = create_autospec(get_brand_name, return_value="Everflow")

        monkeypatch.setattr(public_og, "_resolve_link", mock_resolve_link)
        monkeypatch.setattr(artifacts_db, "get_version", mock_get_version)
        monkeypatch.setattr(public_og, "get_brand_name", mock_brand)

        client = _make_client()
        resp = client.get("/api/v1/public/og-meta", params={"path": "/a/HtmlCode1234"})
        assert resp.status_code == 200
        metas = _extract_meta(resp.text)
        assert metas["og:title"] == ["2026 半导体行业展望"]
        assert metas["og:site_name"] == ["Everflow"]

        # Markdown / PDF without version title -> filename without extension
        access_md = _make_file_access(
            code="MdCode123456", path="deliverables/NVDA_Q3_Memo.md", shared_seq=2
        )
        mock_resolve_link.return_value = access_md
        mock_get_version.return_value = _make_version(
            seq=2, entry_path="deliverables/NVDA_Q3_Memo.md", title=None
        )
        resp_md = client.get("/api/v1/public/og-meta", params={"path": "/a/MdCode123456"})
        assert resp_md.status_code == 200
        assert _extract_meta(resp_md.text)["og:title"] == ["NVDA_Q3_Memo"]

    @pytest.mark.parametrize("legacy_refused", [None, "too_many_files"])
    def test_public_legacy_file_share_always_returns_brand_name(
        self, monkeypatch: pytest.MonkeyPatch, legacy_refused: str | None
    ) -> None:
        access_legacy = _make_file_access(
            code="LegacyFile01",
            path="notes/季度复盘.pdf",
            shared_seq=0,
            legacy_refused=legacy_refused,
        )
        mock_resolve_link = create_autospec(public_og._resolve_link, return_value=access_legacy)
        mock_get_version = create_autospec(artifacts_db.get_version)
        mock_brand = create_autospec(get_brand_name, return_value="Everflow")

        monkeypatch.setattr(public_og, "_resolve_link", mock_resolve_link)
        monkeypatch.setattr(artifacts_db, "get_version", mock_get_version)
        monkeypatch.setattr(public_og, "get_brand_name", mock_brand)

        client = _make_client()
        resp = client.get("/api/v1/public/og-meta", params={"path": "/a/LegacyFile01"})
        assert resp.status_code == 200
        assert _extract_meta(resp.text)["og:title"] == ["Everflow"]
        assert "季度复盘" not in resp.text
        mock_get_version.assert_not_awaited()

    @pytest.mark.parametrize(
        "raw_path",
        [
            None,
            "",
            "/",
            "/dashboard",
            "/s/",
            "/a/",
            "/s/a/b",
            "/a/../../etc/passwd",
            "/p/something",
            "/s/nonexistentToken",
        ],
    )
    def test_invalid_or_missing_path_returns_brand_fallback_200(
        self, monkeypatch: pytest.MonkeyPatch, raw_path: str | None
    ) -> None:
        mock_resolve_link = create_autospec(public_og._resolve_link, return_value=None)
        mock_get_thread = create_autospec(get_thread_by_share_token, return_value=None)
        mock_brand = create_autospec(get_brand_name, return_value="Everflow")

        monkeypatch.setattr(public_og, "_resolve_link", mock_resolve_link)
        monkeypatch.setattr(public_og, "get_thread_by_share_token", mock_get_thread)
        monkeypatch.setattr(public_og, "get_brand_name", mock_brand)

        client = _make_client()
        params = {} if raw_path is None else {"path": raw_path}
        resp = client.get("/api/v1/public/og-meta", params=params)

        assert resp.status_code == 200
        metas = _extract_meta(resp.text)
        assert metas["og:site_name"] == ["Everflow"]
        assert metas["og:title"] == ["Everflow"]
        assert metas["og:description"] == ["来自 Everflow 的分享"]

    def test_revoked_file_and_revoked_thread_return_brand_fallback_200(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_resolve_link = create_autospec(
            public_og._resolve_link,
            side_effect=HTTPException(status_code=404, detail=LINK_NOT_FOUND),
        )
        mock_get_thread = create_autospec(get_thread_by_share_token)
        mock_brand = create_autospec(get_brand_name, return_value="Everflow")

        monkeypatch.setattr(public_og, "_resolve_link", mock_resolve_link)
        monkeypatch.setattr(public_og, "get_thread_by_share_token", mock_get_thread)
        monkeypatch.setattr(public_og, "get_brand_name", mock_brand)

        client = _make_client()
        resp_file = client.get("/api/v1/public/og-meta", params={"path": "/a/RevokedFile1"})
        assert resp_file.status_code == 200
        assert _extract_meta(resp_file.text)["og:title"] == ["Everflow"]
        # A revoked file link must not fall through to thread lookup.
        mock_get_thread.assert_not_awaited()

        # Revoked thread (is_shared=FALSE -> get_thread_by_share_token returns None)
        mock_resolve_link.side_effect = None
        mock_resolve_link.return_value = None
        mock_get_thread.return_value = None
        resp_thread = client.get("/api/v1/public/og-meta", params={"path": "/s/RevokedThr01"})
        assert resp_thread.status_code == 200
        assert _extract_meta(resp_thread.text)["og:title"] == ["Everflow"]

    def test_members_only_shares_return_brand_fallback_even_with_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 1. Organization-only thread
        mock_resolve_link = create_autospec(public_og._resolve_link, return_value=None)
        mock_get_thread = create_autospec(
            get_thread_by_share_token,
            return_value={
                "conversation_thread_id": "t-org",
                "title": "内部投委会纪要",
                "workspace_name": "Internal",
                "owner_id": "owner-1",
                "share_permissions": {"audience": ORGANIZATION},
            },
        )
        mock_serves_public = create_autospec(serves_public_links, return_value=True)
        mock_brand = create_autospec(get_brand_name, return_value="Everflow")

        monkeypatch.setattr(public_og, "_resolve_link", mock_resolve_link)
        monkeypatch.setattr(public_og, "get_thread_by_share_token", mock_get_thread)
        monkeypatch.setattr(public_og, "serves_public_links", mock_serves_public)
        monkeypatch.setattr(public_og, "get_brand_name", mock_brand)

        client = _make_client()
        auth_headers = {
            "Authorization": "Bearer secret-user-token",
            "Cookie": "session=secret-cookie",
        }
        resp_thread = client.get(
            "/api/v1/public/og-meta",
            params={"path": "/s/OrgThread001"},
            headers=auth_headers,
        )
        assert resp_thread.status_code == 200
        assert _extract_meta(resp_thread.text)["og:title"] == ["Everflow"]
        assert "内部投委会纪要" not in resp_thread.text

        # 2. Organization-only file (resolve_link(user_id=None) raises 401 members_only)
        mock_resolve_link.side_effect = HTTPException(
            status_code=401, detail={"type": MEMBERS_ONLY, "message": "Sign in"}
        )
        resp_file = client.get(
            "/api/v1/public/og-meta",
            params={"path": "/a/OrgFile00001"},
            headers=auth_headers,
        )
        assert resp_file.status_code == 200
        assert _extract_meta(resp_file.text)["og:title"] == ["Everflow"]
        # Always resolved anonymously (user_id=None) regardless of request headers.
        mock_resolve_link.assert_awaited_with("OrgFile00001", user_id=None)

    def test_app_link_and_backend_errors_return_brand_fallback_200(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_resolve_link = create_autospec(
            public_og._resolve_link, return_value=_make_file_access(code="AppCode12345", kind=KIND_APP)
        )
        mock_brand = create_autospec(get_brand_name, return_value="Everflow")
        monkeypatch.setattr(public_og, "_resolve_link", mock_resolve_link)
        monkeypatch.setattr(public_og, "get_brand_name", mock_brand)

        client = _make_client()
        resp_app = client.get("/api/v1/public/og-meta", params={"path": "/a/AppCode12345"})
        assert resp_app.status_code == 200
        assert _extract_meta(resp_app.text)["og:title"] == ["Everflow"]

        # Database / unexpected runtime error -> still HTTP 200 brand fallback
        mock_resolve_link.side_effect = RuntimeError("db connection lost")
        resp_err = client.get("/api/v1/public/og-meta", params={"path": "/s/CrashTok0001"})
        assert resp_err.status_code == 200
        assert _extract_meta(resp_err.text)["og:title"] == ["Everflow"]
        assert resp_err.headers["cache-control"] == "no-store"

    def test_html_escaping_prevents_attribute_breakout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        evil_title = 'Q3 "Report" <script>alert(1)</script> & \'Notes\''
        mock_resolve_link = create_autospec(public_og._resolve_link, return_value=None)
        mock_get_thread = create_autospec(
            get_thread_by_share_token,
            return_value={
                "conversation_thread_id": "t-xss",
                "title": evil_title,
                "workspace_name": "WS",
                "owner_id": "owner-1",
                "share_permissions": {"allow_files": True},
            },
        )
        mock_serves_public = create_autospec(serves_public_links, return_value=True)
        mock_allows_files = create_autospec(shared_thread_allows_files, return_value=True)
        mock_brand = create_autospec(get_brand_name, return_value='Ever"flow <Core>')

        monkeypatch.setattr(public_og, "_resolve_link", mock_resolve_link)
        monkeypatch.setattr(public_og, "get_thread_by_share_token", mock_get_thread)
        monkeypatch.setattr(public_og, "serves_public_links", mock_serves_public)
        monkeypatch.setattr(share_access, "shared_thread_allows_files", mock_allows_files)
        monkeypatch.setattr(public_og, "get_brand_name", mock_brand)

        client = _make_client()
        resp = client.get("/api/v1/public/og-meta", params={"path": "/s/xssToken0001"})
        assert resp.status_code == 200
        assert "<script>" not in resp.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in resp.text
        assert "&quot;Report&quot;" in resp.text
        assert "&#x27;Notes&#x27;" in resp.text
        assert "Ever&quot;flow &lt;Core&gt;" in resp.text

    def test_brand_name_follows_profile_without_hardcoding_everflow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_resolve_link = create_autospec(public_og._resolve_link, return_value=None)
        mock_get_thread = create_autospec(get_thread_by_share_token, return_value=None)
        monkeypatch.setattr(public_og, "_resolve_link", mock_resolve_link)
        monkeypatch.setattr(public_og, "get_thread_by_share_token", mock_get_thread)

        # Core profile without BRAND_NAME -> "Dalton"
        monkeypatch.delenv("BRAND_NAME", raising=False)
        monkeypatch.setenv("DALTON_PROFILE", "core")
        client = _make_client()
        resp_core = client.get("/api/v1/public/og-meta", params={"path": "/s/unknownCore"})
        assert resp_core.status_code == 200
        metas_core = _extract_meta(resp_core.text)
        assert metas_core["og:site_name"] == ["Dalton"]
        assert metas_core["og:title"] == ["Dalton"]
        assert metas_core["og:description"] == ["来自 Dalton 的分享"]
        assert "Everflow" not in resp_core.text

        # Custom BRAND_NAME env override
        monkeypatch.setenv("BRAND_NAME", "Acme Research")
        resp_custom = client.get("/api/v1/public/og-meta", params={"path": "/s/unknownCustom"})
        assert resp_custom.status_code == 200
        assert _extract_meta(resp_custom.text)["og:site_name"] == ["Acme Research"]
        assert _extract_meta(resp_custom.text)["og:description"] == ["来自 Acme Research 的分享"]


class TestCaddyfileOgShareRouting:
    """Static verification that both Dalton and Standalone Caddyfiles wire OG injection identically."""

    @pytest.mark.parametrize(
        "caddyfile_rel",
        [
            "deploy/dalton/Caddyfile",
            "deploy/standalone/Caddyfile",
        ],
    )
    def test_caddyfile_wires_share_templates_and_error_fallback(
        self, caddyfile_rel: str
    ) -> None:
        text = (REPO_ROOT / caddyfile_rel).read_text(encoding="utf-8")

        # 1a. Site-level request_header strips Accept-Encoding for @share_pages
        #     at the same level as encode (outside handle @share_pages) so gzip
        #     never opens before handle_errors fallback runs.
        encode_idx = text.index("encode zstd gzip")
        strip_ae_line = "\n\trequest_header @share_pages -Accept-Encoding\n"
        assert strip_ae_line in text
        share_idx = text.index("@share_pages {")
        assert encode_idx < text.index(strip_ae_line) < share_idx

        # 1b. Dedicated anonymous /api/v1/public/og-meta route precedes @backend_routes
        og_idx = text.index("@public_og_meta path /api/v1/public/og-meta")
        backend_idx = text.index("@backend_routes path /api/* /ws/*")
        assert og_idx < backend_idx

        og_block = text[og_idx:backend_idx]
        for header in ("-Authorization", "-Cookie", "-Accept-Encoding"):
            assert f"request_header {header}" in og_block
            assert f"header_up {header}" in og_block
        assert "dial_timeout" in og_block
        assert "response_header_timeout" in og_block

        # 2. @share_pages precedes @spa_read and matches only /s/<token> and /a/<token>
        spa_idx = text.index("@spa_read {")
        assert share_idx < spa_idx

        share_block = text[share_idx:spa_idx]
        assert "request_header @share_pages -Accept-Encoding" not in share_block
        match = re.search(r"path_regexp share_pages (\S+)", share_block)
        assert match is not None
        share_re = re.compile(match.group(1))
        for valid in ("/s/abc123XYZ", "/a/AbCdEf123456", "/s/tok_1-2/"):
            assert share_re.match(valid), f"{caddyfile_rel} regex missed {valid}"
        for non_share in ("/", "/dashboard", "/chat/t/123", "/assets/index.js", "/s/a/b", "/a/", "/s/"):
            assert not share_re.match(non_share), f"{caddyfile_rel} regex wrongly matched {non_share}"

        # 3. templates uses custom <!--@ @--> delimiters and mime text/html ONLY in @share_pages
        assert "between <!--@ @-->" in share_block
        assert "mime text/html" in share_block
        assert "vars og_share_fallback 1" in share_block
        assert "rewrite * /index.html" in share_block
        assert text.count("templates {") == 1

        # 4. handle_errors falls back to static /index.html with HTTP 200 on backend failure
        errors_idx = text.index("handle_errors {")
        errors_block = text[errors_idx:share_idx]
        assert "@share_og_fallback" in errors_block
        assert "vars og_share_fallback 1" in errors_block
        assert "vars {vars.og_share_fallback}" not in text
        assert "rewrite * /index.html" in errors_block
        assert "status 200" in errors_block

        # 5. Delimiters & endpoint stay aligned with web/src/lib/dalton/brandIndexHtml.ts
        ts_text = (REPO_ROOT / "web/src/lib/dalton/brandIndexHtml.ts").read_text(encoding="utf-8")
        assert "OG_META_ENDPOINT = '/api/v1/public/og-meta'" in ts_text
        assert "OG_TEMPLATE_OPEN = '<!--@'" in ts_text
        assert "OG_TEMPLATE_CLOSE = '@-->'" in ts_text
