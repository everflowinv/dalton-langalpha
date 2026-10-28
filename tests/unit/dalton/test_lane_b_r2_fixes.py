"""Lane-B round-2 fixes: ingest helper contracts (#1/#2), feishu grant scope
(#3), image-fetch SSRF (#4), FMP first-429 quota class (#5), auto-ingest rate
limit + duplicate ask (#7), image-bind failure detail (#9), open_id lookup
shape (#10), and the docx_image `extra` upload field (#11).

Tool-level tests run from the tool entry to the bridge request, replacing
only the HTTP transport (or the bridge-calling seam where noted); the helper
functions under test are never replaced. All doubles use `create_autospec`;
no network, no real Feishu/FMP calls.
"""

from __future__ import annotations

import base64
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import httpx
import pytest
from deepagents.backends.protocol import FileDownloadResponse

from ptc_agent.agent.backends.sandbox import SandboxBackend
from ptc_agent.core.sandbox.jobs import Ended, JobHandle, JobRef

from src.dalton.tools import ingest_tools
from src.data_client.base import FinancialDataSource
from src.data_client import FinancialDataProvider

REPO_ROOT = Path(__file__).resolve().parents[3]
BRIDGE_DIR = REPO_ROOT / "scripts" / "dalton"
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

import researchdata_bridge as rb  # noqa: E402

USER_ID = "u-1"
WS_ID = "ws-1"


def _run_config(**overrides: Any) -> dict[str, Any]:
    configurable = {
        "user_id": USER_ID,
        "workspace_id": WS_ID,
        "thread_id": "th-1",
        "run_id": "r-1",
    }
    configurable.update(overrides)
    return {"configurable": configurable, "metadata": {}, "tags": [], "callbacks": None}


# ---------------------------------------------------------------------------
# Shared doubles
# ---------------------------------------------------------------------------


def _handle(cid: str = "job-1") -> JobHandle:
    return JobHandle(
        JobRef(
            command_id=cid,
            state_dir=f"/.system/jobs/{cid}",
            log_path=f"/tmp/{cid}.log",
            trace_path=f"/tmp/{cid}.jsonl",
            log_relpath=f"{cid}.log",
        ),
        datetime.now(tz=timezone.utc),
    )


def _make_ended(exit_code: int, stdout_text: str) -> Ended:
    output = json.dumps({"exit_code": exit_code, "stdout": stdout_text, "stderr": ""})
    return Ended(_handle(), exit_code=0, output=output)


def _file_backend(ws_dir: str = "/remote/ws/alpha", name: str = "note.md",
                  content: bytes = b"# MS note body") -> Any:
    """A sandbox backend whose `ws_dir/name` reads back as a regular file."""
    backend = create_autospec(SandboxBackend, instance=True)
    backend.workspace_dir = ws_dir
    backend.root_dir = ws_dir
    resolved = f"{ws_dir}/{name}"
    backend.start_code_job.side_effect = [
        _make_ended(0, "directory\nregular file\n"),
        _make_ended(0, f"{ws_dir}\n{resolved}\n"),
    ]
    backend.adownload_files.return_value = [
        FileDownloadResponse(path=resolved, content=content)
    ]
    return backend


def _install_bridge_http(monkeypatch: pytest.MonkeyPatch, module: Any, handler: Any) -> list[httpx.Request]:
    """Route `module`'s bridge calls through a MockTransport; return the capture list."""
    captured: list[httpx.Request] = []
    real_cls = httpx.AsyncClient

    def _capturing_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    def _factory(**kwargs: Any):
        return real_cls(transport=httpx.MockTransport(_capturing_handler), timeout=kwargs.get("timeout"))

    monkeypatch.setattr(module.httpx, "AsyncClient", create_autospec(real_cls, side_effect=_factory))
    return captured


@pytest.fixture
def internal_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ingest_tools,
        "get_user_tier",
        create_autospec(ingest_tools.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        ingest_tools,
        "verify_workspace_access",
        create_autospec(ingest_tools.verify_workspace_access, return_value={"workspace_id": WS_ID}),
    )
    monkeypatch.setenv("DALTON_BRIDGE_URL", "http://127.0.0.1:8960")
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", "test-admin-token")


# ---------------------------------------------------------------------------
# #1/#2: ingest_sales_note reaches the bridge with a file payload
# ---------------------------------------------------------------------------


class TestIngestSalesNotePayload:
    @pytest.mark.asyncio
    async def test_file_path_reaches_bridge_with_base64_payload(
        self, monkeypatch: pytest.MonkeyPatch, internal_caller: None
    ) -> None:
        """Round-2 #1/#2: no unpack crash, and a successfully read file is
        carried into the bridge request instead of an `ERROR: ` answer."""
        body = b"# MS note body"
        captured = _install_bridge_http(
            monkeypatch,
            ingest_tools,
            lambda request: httpx.Response(
                200,
                json={"ok": True, "status": "dry_run", "dry_run": True, "doc_key": None},
            ),
        )
        tool = ingest_tools.build_ingest_sales_note_tool(
            _file_backend(content=body), allow_home_workspace=False
        )
        result = json.loads(
            await tool.ainvoke(
                {
                    "institution": "Morgan Stanley",
                    "title": "ORCL 3Q26 preview",
                    "file_path": "note.md",
                    "confirm": False,
                },
                config=_run_config(),
            )
        )
        assert result["ok"] is True and result["status"] == "dry_run"
        assert len(captured) == 1
        sent = json.loads(captured[0].content.decode("utf-8"))
        assert sent["institution"] == "Morgan Stanley"
        assert sent["dry_run"] is True
        assert sent["file"]["filename"] == "note.md"
        assert base64.b64decode(sent["file"]["content_b64"]) == body
        assert sent["content"] is None

    @pytest.mark.asyncio
    async def test_content_confirm_write_sends_request_id(
        self, monkeypatch: pytest.MonkeyPatch, internal_caller: None
    ) -> None:
        captured = _install_bridge_http(
            monkeypatch,
            ingest_tools,
            lambda request: httpx.Response(
                200, json={"ok": True, "status": "ingested", "doc_key": "k9"}
            ),
        )
        monkeypatch.setattr(
            ingest_tools,
            "check_and_increment_wiki_ingest_rate_limit",
            create_autospec(
                ingest_tools.check_and_increment_wiki_ingest_rate_limit,
                return_value=(True, 1),
            ),
        )
        tool = ingest_tools.build_ingest_sales_note_tool(None, allow_home_workspace=False)
        result = json.loads(
            await tool.ainvoke(
                {
                    "institution": "GS",
                    "title": "T",
                    "content": "sell-side color",
                    "confirm": True,
                },
                config=_run_config(),
            )
        )
        assert result["ok"] is True
        sent = json.loads(captured[0].content.decode("utf-8"))
        assert sent["dry_run"] is False
        assert sent["request_id"]
        assert sent["content"] == "sell-side color"


# ---------------------------------------------------------------------------
# #7: auto-ingest rate limit + duplicate ask
# ---------------------------------------------------------------------------


def _auto_ingest_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ingest_tools,
        "get_workspace_auto_ingest",
        create_autospec(ingest_tools.get_workspace_auto_ingest, return_value=True),
    )


class TestAutoIngestRateLimit:
    @pytest.mark.asyncio
    async def test_auto_write_hits_the_shared_hourly_limiter(
        self, monkeypatch: pytest.MonkeyPatch, internal_caller: None
    ) -> None:
        """An auto-ingest write is a write: it spends the same counter and is
        refused with the manual path's wording — Redis-unavailable included."""
        _auto_ingest_on(monkeypatch)
        captured = _install_bridge_http(
            monkeypatch, ingest_tools, lambda request: httpx.Response(200, json={"ok": True})
        )
        limiter = create_autospec(
            ingest_tools.check_and_increment_wiki_ingest_rate_limit,
            side_effect=[(False, 31), (False, -1)],
        )
        monkeypatch.setattr(ingest_tools, "check_and_increment_wiki_ingest_rate_limit", limiter)
        tool = ingest_tools.build_ingest_research_tool(None, allow_home_workspace=False)

        refused = json.loads(
            await tool.ainvoke(
                {"speaker": "sell_side", "institution": "MS", "title": "T", "content": "x"},
                config=_run_config(),
            )
        )
        assert refused["status"] == "rate_limited"
        assert "入库频率超限" in refused["error"]
        assert refused["limit_per_hour"] == ingest_tools.SALES_NOTES_RATE_LIMIT_PER_HOUR

        unavailable = json.loads(
            await tool.ainvoke(
                {"speaker": "sell_side", "institution": "MS", "title": "T2", "content": "y"},
                config=_run_config(),
            )
        )
        assert unavailable["status"] == "rate_limit_unavailable"
        assert "Redis" in unavailable["error"]
        # No bridge call left the process: the write was refused before it.
        assert captured == []


class TestAutoIngestDuplicateAsk:
    @pytest.mark.asyncio
    async def test_sales_notes_duplicate_preview_asks_instead_of_writing(
        self, monkeypatch: pytest.MonkeyPatch, internal_caller: None
    ) -> None:
        _auto_ingest_on(monkeypatch)
        monkeypatch.setattr(
            ingest_tools,
            "check_and_increment_wiki_ingest_rate_limit",
            create_autospec(
                ingest_tools.check_and_increment_wiki_ingest_rate_limit,
                return_value=(True, 1),
            ),
        )
        captured = _install_bridge_http(
            monkeypatch,
            ingest_tools,
            lambda request: httpx.Response(
                200,
                json={
                    "ok": True,
                    "status": "dry_run",
                    "dry_run": True,
                    # r3 (#3): the bridge's canonical duplicate verdict —
                    # normalized from the CLI's own status/result spelling.
                    "duplicate": True,
                    "cli_preview": {"status": "duplicate", "doc_key": "abc123"},
                },
            ),
        )
        tool = ingest_tools.build_ingest_research_tool(None, allow_home_workspace=False)
        result = json.loads(
            await tool.ainvoke(
                {"speaker": "sell_side", "institution": "MS", "title": "T", "content": "dup"},
                config=_run_config(),
            )
        )
        assert result["status"] == "duplicate_suspected"
        assert "疑似重复" in result["question"]
        assert "confirm=True" in result["question"]
        # Exactly the dry-run preview went out; no second copy was written.
        assert len(captured) == 1
        sent = json.loads(captured[0].content.decode("utf-8"))
        assert sent["dry_run"] is True
        assert "request_id" not in sent

    @pytest.mark.asyncio
    async def test_wiki_duplicate_preview_asks_instead_of_writing(
        self, monkeypatch: pytest.MonkeyPatch, internal_caller: None
    ) -> None:
        from src.dalton.tools import wiki_tools

        _auto_ingest_on(monkeypatch)
        monkeypatch.setattr(
            ingest_tools,
            "check_and_increment_wiki_ingest_rate_limit",
            create_autospec(
                ingest_tools.check_and_increment_wiki_ingest_rate_limit,
                return_value=(True, 1),
            ),
        )
        bridge = create_autospec(
            wiki_tools.call_bridge_wiki_ingest,
            return_value=(
                400,
                {
                    "ok": False,
                    "status": "duplicate",
                    "duplicate": True,
                    "duplicate_reason": "same_content_sha256",
                    "existing_doc_id": 2048,
                    "existing_filepath": "wiki/companies/NVDA/2026-10-25-专家访谈.md",
                },
            ),
        )
        monkeypatch.setattr(wiki_tools, "call_bridge_wiki_ingest", bridge)
        tool = ingest_tools.build_ingest_research_tool(None, allow_home_workspace=False)
        result = json.loads(
            await tool.ainvoke(
                {
                    "speaker": "expert_or_company",
                    "target_type": "company",
                    "target": "NVDA",
                    "content_type": "专家访谈",
                    "date": "2026-10-25",
                    "content": "dup",
                },
                config=_run_config(),
            )
        )
        assert result["status"] == "duplicate_suspected"
        assert "company-wiki" in result["question"]
        assert result["preview"]["duplicate_reason"] == "same_content_sha256"
        # Only the preview; the write never happened.
        assert bridge.call_count == 1
        assert bridge.call_args.args[0]["dry_run"] is True

    @pytest.mark.asyncio
    async def test_clean_preview_auto_writes_once(
        self, monkeypatch: pytest.MonkeyPatch, internal_caller: None
    ) -> None:
        _auto_ingest_on(monkeypatch)
        monkeypatch.setattr(
            ingest_tools,
            "check_and_increment_wiki_ingest_rate_limit",
            create_autospec(
                ingest_tools.check_and_increment_wiki_ingest_rate_limit,
                return_value=(True, 1),
            ),
        )
        responses: list[httpx.Response] = [
            httpx.Response(
                200,
                json={"ok": True, "status": "dry_run", "dry_run": True,
                      "cli_preview": {"status": "ok"}},
            ),
            httpx.Response(
                200,
                json={"ok": True, "status": "ingested", "doc_key": "k1",
                      "document_id": "manual:k1", "title": "T"},
            ),
        ]
        captured = _install_bridge_http(
            monkeypatch, ingest_tools, lambda request: responses.pop(0)
        )
        tool = ingest_tools.build_ingest_research_tool(None, allow_home_workspace=False)
        result = json.loads(
            await tool.ainvoke(
                {"speaker": "sell_side", "institution": "MS", "title": "T", "content": "fresh"},
                config=_run_config(),
            )
        )
        assert result["ok"] is True and result["auto_ingest"] is True
        assert "manual:k1" in result["receipt"]
        assert len(captured) == 2
        preview, write = (json.loads(c.content.decode("utf-8")) for c in captured)
        assert preview["dry_run"] is True
        assert write["dry_run"] is False
        assert write["request_id"]


# ---------------------------------------------------------------------------
# #3: feishu grant scope (bridge side)
# ---------------------------------------------------------------------------


def _grant_bridge(tmp_path: Path) -> rb.ResearchDataBridge:
    bridge = rb.ResearchDataBridge.__new__(rb.ResearchDataBridge)
    bridge.cfg = create_autospec(rb.BridgeConfig, instance=True)
    bridge.cfg.wiki_ingest_audit_file = tmp_path / "wiki_ingest_audit.jsonl"
    bridge._feishu_client_instance = create_autospec(rb.FeishuClient, instance=True)
    bridge._feishu_client_instance.grant_full_access.return_value = "openid:ou_1"
    return bridge


@pytest.fixture
def resolved_docx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rb,
        "resolve_feishu_document",
        create_autospec(rb.resolve_feishu_document, return_value=("docx-g1", {})),
    )


class TestGrantScopeBridge:
    @pytest.mark.asyncio
    async def test_unregistered_document_is_refused_403(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resolved_docx: None
    ) -> None:
        bridge = _grant_bridge(tmp_path)
        code, payload = await bridge.handle_feishu_doc_grant(
            {"token": "docx-g1", "email": "u@corp.com"}
        )
        assert code == 403
        assert payload["error"] == "grant_scope_denied"
        assert "不是通过 Dalton 创建" in payload["message"]
        bridge._feishu_client_instance.grant_full_access.assert_not_called()

    @pytest.mark.asyncio
    async def test_registered_document_grants_creator_email(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resolved_docx: None
    ) -> None:
        bridge = _grant_bridge(tmp_path)
        bridge._register_feishu_created_doc(
            "docx-g1", creator_email="U@Corp.com", title="T"
        )
        code, payload = await bridge.handle_feishu_doc_grant(
            {"token": "docx-g1", "email": "u@corp.com"}
        )
        assert code == 200
        assert payload["ok"] is True
        assert payload["granted"] == "u@corp.com"
        assert payload["url"]
        bridge._feishu_client_instance.grant_full_access.assert_called_once_with(
            "docx-g1", "u@corp.com"
        )
        # The registration itself persisted with the normalized email.
        registry = json.loads(
            (tmp_path / "feishu_doc_registry.json").read_text(encoding="utf-8")
        )
        assert registry["docx-g1"]["creator_email"] == "u@corp.com"

    @pytest.mark.asyncio
    async def test_someone_elses_email_is_refused_403(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resolved_docx: None
    ) -> None:
        bridge = _grant_bridge(tmp_path)
        bridge._register_feishu_created_doc("docx-g1", creator_email="u@corp.com")
        code, payload = await bridge.handle_feishu_doc_grant(
            {"token": "docx-g1", "email": "other@corp.com"}
        )
        assert code == 403
        assert payload["error"] == "grant_scope_denied"
        assert "创建者" in payload["message"]
        bridge._feishu_client_instance.grant_full_access.assert_not_called()


# ---------------------------------------------------------------------------
# #3: feishu_grant_access tool (entry → bridge HTTP only)
# ---------------------------------------------------------------------------


class TestGrantAccessTool:
    def _install_user_email(self, monkeypatch: pytest.MonkeyPatch, email: str) -> None:
        from src.server.database import user as user_db

        async def _fake_get_user(uid: str):
            return {"user_id": uid, "email": email}

        monkeypatch.setattr(
            user_db, "get_user", create_autospec(user_db.get_user, side_effect=_fake_get_user)
        )

    @pytest.mark.asyncio
    async def test_grants_only_the_caller_from_their_account_record(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.dalton.feishu as feishu_mod
        import src.dalton.tools.feishu_tools as ft_mod
        from src.dalton.tools.feishu_tools import create_feishu_tools

        monkeypatch.setenv("DALTON_BRIDGE_URL", "http://127.0.0.1:8960")
        monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", "test-admin-token")
        monkeypatch.setenv("DALTON_INVITE_EMAIL_DOMAIN", "team.dalton.local")
        monkeypatch.setattr(
            ft_mod, "get_user_tier", create_autospec(ft_mod.get_user_tier, return_value="internal")
        )
        self._install_user_email(monkeypatch, "User@Corp.com")

        captured = _install_bridge_http(
            monkeypatch,
            feishu_mod,
            lambda request: httpx.Response(
                200,
                json={"ok": True, "document_id": "docx-g1", "granted": "user@corp.com",
                      "url": "https://feishu.cn/docx/docx-g1", "grant_member": "email:user@corp.com"},
            ),
        )
        grant_tool = create_feishu_tools()[4]
        assert "email" not in grant_tool.args  # self-service only: no email parameter

        result = json.loads(
            await grant_tool.ainvoke({"token": "docx-g1"}, config=_run_config())
        )
        assert result["ok"] is True
        assert result["granted"] == "user@corp.com"
        assert len(captured) == 1
        assert captured[0].url.path == "/feishu/doc/grant"
        sent = json.loads(captured[0].content.decode("utf-8"))
        assert sent == {"token": "docx-g1", "email": "user@corp.com"}

    @pytest.mark.asyncio
    async def test_scope_denial_from_bridge_reaches_the_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.dalton.feishu as feishu_mod
        import src.dalton.tools.feishu_tools as ft_mod
        from src.dalton.tools.feishu_tools import create_feishu_tools

        monkeypatch.setenv("DALTON_BRIDGE_URL", "http://127.0.0.1:8960")
        monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", "test-admin-token")
        monkeypatch.setattr(
            ft_mod, "get_user_tier", create_autospec(ft_mod.get_user_tier, return_value="internal")
        )
        self._install_user_email(monkeypatch, "user@corp.com")
        _install_bridge_http(
            monkeypatch,
            feishu_mod,
            lambda request: httpx.Response(
                403,
                json={
                    "ok": False,
                    "error": "grant_scope_denied",
                    "document_id": "docxOther",
                    "message": "该文档不是通过 Dalton 创建的（未在创建登记中），不能通过本端点补授权；请在飞书客户端手动分享。",
                },
            ),
        )
        result = json.loads(
            await create_feishu_tools()[4].ainvoke(
                {"token": "docxOther"}, config=_run_config()
            )
        )
        assert result["ok"] is False
        assert result["error"] == "grant_scope_denied"
        assert "不是通过 Dalton 创建" in result["message"]


# ---------------------------------------------------------------------------
# #4: image fetch SSRF — DNS resolution, pinned dials, manual redirects.
# The transport seam here is `_feishu_image_open_connection`; the fetch loop,
# address checks and redirect handling under it are the real code.
# ---------------------------------------------------------------------------


class _FakeHTTPResponse:
    """A stdlib response stand-in (a value, not a patched project seam)."""

    def __init__(self, status: int, body: bytes = b"", location: str | None = None) -> None:
        self.status = status
        self._body = body
        self._location = location

    def getheader(self, name: str, default: Any = None) -> Any:
        return self._location if name.lower() == "location" else default

    def read(self, n: int = -1) -> bytes:
        return self._body

    def close(self) -> None:
        pass


def _dns_answers(monkeypatch: pytest.MonkeyPatch, *answers: list[str]) -> Any:
    """Patch getaddrinfo to hand out `answers` per call (the rebinding double
    returns a public address first and a loopback one on the next call)."""
    getaddrinfo = create_autospec(
        rb.socket.getaddrinfo,
        side_effect=[[(2, 1, 6, "", (ip, 443)) for ip in ips] for ips in answers],
    )
    monkeypatch.setattr(rb.socket, "getaddrinfo", getaddrinfo)
    return getaddrinfo


def _public_dns(monkeypatch: pytest.MonkeyPatch, *ips: str) -> Any:
    return _dns_answers(monkeypatch, list(ips))


def _install_conn_factory(monkeypatch: pytest.MonkeyPatch, *conns: Any) -> Any:
    pool = list(conns)

    def _next_conn(parsed: Any, **kwargs: Any) -> Any:
        return pool.pop(0)

    factory = create_autospec(rb._feishu_image_open_connection, side_effect=_next_conn)
    monkeypatch.setattr(rb, "_feishu_image_open_connection", factory)
    return factory


def _conn_with_response(status: int, body: bytes = b"", location: str | None = None) -> Any:
    conn = create_autospec(rb._PinnedHTTPConnection, instance=True)
    conn.getresponse.return_value = _FakeHTTPResponse(status, body, location)
    return conn


class TestImageFetchSSRF:
    def test_hostname_resolving_to_loopback_is_refused_before_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _public_dns(monkeypatch, "127.0.0.1")
        factory = _install_conn_factory(monkeypatch)
        factory.side_effect = AssertionError("must not dial a blocked host")
        with pytest.raises(rb.FeishuApiError) as excinfo:
            rb._feishu_image_fetch("http://images.example.test/a.png")
        assert excinfo.value.error_code == "image_url_blocked"
        assert "127.0.0.1" in str(excinfo.value)

    def test_rebinding_answer_cannot_move_the_dial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The address that passed the check is the address the socket dials:
        a second DNS answer flipping to loopback never reaches the connect."""
        getaddrinfo = _dns_answers(monkeypatch, ["93.184.216.34"], ["127.0.0.1"])
        conn = _conn_with_response(200, body=b"\x89PNG fake bytes")
        factory = _install_conn_factory(monkeypatch, conn)

        data, final_url = rb._feishu_image_fetch("https://images.example.test/a.png")

        assert data == b"\x89PNG fake bytes"
        assert final_url == "https://images.example.test/a.png"
        # Exactly one resolution, and the dial was pinned to its answer.
        assert getaddrinfo.call_count == 1
        assert factory.call_count == 1
        assert factory.call_args.kwargs["pinned_ip"] == "93.184.216.34"
        conn.request.assert_called_once()

    def test_redirect_into_metadata_endpoint_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _public_dns(monkeypatch, "93.184.216.34")
        conn = _conn_with_response(302, location="http://169.254.169.254/latest/meta-data/")
        factory = _install_conn_factory(monkeypatch, conn)
        with pytest.raises(rb.FeishuApiError) as excinfo:
            rb._feishu_image_fetch("https://images.example.test/a.png")
        assert excinfo.value.error_code == "image_url_blocked"
        assert "169.254.169.254" in str(excinfo.value)
        # The first hop was dialed once, then the redirect target was refused
        # before any second connection existed.
        assert factory.call_count == 1

    def test_public_redirect_is_followed_with_a_fresh_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Redirects are followed by hand, never by the HTTP layer: the second
        hop resolves and checks its own host and dials its own pinned address."""
        _dns_answers(monkeypatch, ["93.184.216.34"], ["1.2.3.4"])
        first = _conn_with_response(302, location="https://cdn.example.test/b.png")
        second = _conn_with_response(200, body=b"\x89PNG cdn bytes")
        factory = _install_conn_factory(monkeypatch, first, second)

        data, final_url = rb._feishu_image_fetch("https://images.example.test/a.png")

        assert data == b"\x89PNG cdn bytes"
        assert final_url == "https://cdn.example.test/b.png"
        assert factory.call_count == 2
        second_call = factory.call_args_list[1]
        assert second_call.args[0].hostname == "cdn.example.test"
        assert second_call.kwargs["pinned_ip"] == "1.2.3.4"

    def test_normal_public_image_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _public_dns(monkeypatch, "93.184.216.34")
        conn = _conn_with_response(200, body=b"\x89PNG fake bytes")
        factory = _install_conn_factory(monkeypatch, conn)
        data, final_url = rb._feishu_image_fetch("https://images.example.test/a.png")
        assert data == b"\x89PNG fake bytes"
        assert final_url == "https://images.example.test/a.png"
        assert factory.call_args.kwargs["pinned_ip"] == "93.184.216.34"

    def test_non_http_scheme_is_refused(self) -> None:
        with pytest.raises(rb.FeishuApiError) as excinfo:
            rb._feishu_image_fetch("ftp://images.example.test/a.png")
        assert excinfo.value.error_code == "image_url_blocked"


# ---------------------------------------------------------------------------
# #5: FMP first 429 / Limit Reach raise FmpQuotaExhausted
# ---------------------------------------------------------------------------


def _fmp_client_with_response(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> "Any":
    from src.data_client.fmp import fmp_client as fc
    from src.utils.cache import redis_cache

    monkeypatch.setattr(
        redis_cache,
        "get_cache_client",
        create_autospec(redis_cache.get_cache_client, return_value=None),
    )
    client = fc.FMPClient(api_key="test-key")
    http = create_autospec(httpx.AsyncClient, instance=True)
    http.is_closed = False
    http.get.return_value = response
    monkeypatch.setattr(
        fc.FMPClient,
        "_get_client",
        create_autospec(fc.FMPClient._get_client, return_value=http),
    )
    return client


class TestFmpQuotaClassification:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("use_cache", [True, False])
    async def test_a_normal_200_returns_its_rows_with_or_without_cache(
        self, monkeypatch: pytest.MonkeyPatch, use_cache: bool
    ) -> None:
        """The quota checks sit in front of the normal parse; a plain 200 must
        still come back, also on the uncached quote/DCF calls."""
        rows = [{"symbol": "AAPL", "price": 1.0}]
        client = _fmp_client_with_response(
            monkeypatch,
            httpx.Response(200, json=rows, request=httpx.Request("GET", "https://fmp.example/x")),
        )
        assert await client._make_request("quote/AAPL", use_cache=use_cache) == rows

    @pytest.mark.asyncio
    async def test_first_429_raises_quota_exhausted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.data_client.fmp.fmp_client import FmpQuotaExhausted

        client = _fmp_client_with_response(
            monkeypatch,
            httpx.Response(429, request=httpx.Request("GET", "https://fmp.example/x")),
        )
        with pytest.raises(FmpQuotaExhausted) as excinfo:
            await client.get_sector_performance(target_date="2026-10-27")
        assert excinfo.value.status_code == 429
        assert excinfo.value.reset_at is not None

    @pytest.mark.asyncio
    async def test_200_body_limit_reach_raises_quota_exhausted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.data_client.fmp.fmp_client import FmpQuotaExhausted

        client = _fmp_client_with_response(
            monkeypatch,
            httpx.Response(
                200,
                json={"Error Message": "Limit Reach. Your daily limit is reached."},
                request=httpx.Request("GET", "https://fmp.example/x"),
            ),
        )
        with pytest.raises(FmpQuotaExhausted):
            await client.get_sector_performance(target_date="2026-10-27")

    @pytest.mark.asyncio
    async def test_other_4xx_stays_a_plain_request_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.data_client.fmp.fmp_client import FMPRequestError, FmpQuotaExhausted

        client = _fmp_client_with_response(
            monkeypatch,
            httpx.Response(403, request=httpx.Request("GET", "https://fmp.example/x")),
        )
        with pytest.raises(FMPRequestError) as excinfo:
            await client.get_sector_performance(target_date="2026-10-27")
        assert not isinstance(excinfo.value, FmpQuotaExhausted)
        assert excinfo.value.status_code == 403

    @pytest.mark.asyncio
    async def test_grades_note_names_the_exhausted_quota(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The analyst-data endpoint's grade list explains itself when the
        first FMP call of the day comes back quota-exhausted."""
        from src.data_client import get_financial_data_provider
        from src.data_client.fmp import fmp_client as fc
        from src.data_client.fmp.fmp_client import FMPClient, FmpQuotaExhausted
        from src.server.app import market_data as md_app
        from src.utils.cache import redis_cache
        from src.utils.cache.redis_cache import RedisCacheClient

        cache = create_autospec(RedisCacheClient, instance=True)
        cache.get.return_value = None
        cache.set.return_value = None
        monkeypatch.setattr(
            redis_cache,
            "get_cache_client",
            create_autospec(redis_cache.get_cache_client, return_value=cache),
        )

        provider = create_autospec(FinancialDataProvider, instance=True)
        provider.financial = create_autospec(FinancialDataSource, instance=True)
        provider.financial.get_analyst_price_targets.return_value = []
        monkeypatch.setattr(
            "src.data_client.get_financial_data_provider",
            create_autospec(get_financial_data_provider, return_value=provider),
        )

        fmp_instance = create_autospec(FMPClient, instance=True)
        fmp_instance.get_stock_grades.side_effect = FmpQuotaExhausted(
            datetime(2026, 10, 28, 3, 0, tzinfo=timezone.utc)
        )
        monkeypatch.setattr(
            fc, "FMPClient", create_autospec(FMPClient, return_value=fmp_instance)
        )

        response = await md_app.get_analyst_data("NVDA", USER_ID, grade_limit=5)
        assert response.grades == []
        assert "FMP 当日额度已用完" in (response.gradesNote or "")


# ---------------------------------------------------------------------------
# #9: all images failed — the doc's id/url and per-image reasons survive
# ---------------------------------------------------------------------------

_ALL_IMAGES_FAILED = {
    "ok": False,
    "error": "image_bind_failed",
    "message": "文档已创建，但全部图片都没有绑定成功（见 images.failed），请在文档中手动补图或重试。",
    "document_id": "doccnImgFail",
    "url": "https://feishu.cn/docx/doccnImgFail",
    "images": {
        "total": 2,
        "bound": 0,
        "failed": [
            {
                "ref": "https://example.test/chart.png",
                "block_id": "b9",
                "reason": "image_url_blocked: access to private/reserved address 10.0.0.5",
            },
            {"ref": "local.png", "block_id": "b10", "reason": "素材已上传但图片块未绑定 token（回读校验失败）"},
        ],
    },
}


class TestImageBindFailedDetail:
    @pytest.mark.asyncio
    async def test_create_doc_keeps_doc_location_and_each_failure_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.dalton.tools.feishu_tools as ft_mod
        from src.dalton.feishu import post_bridge_feishu
        from src.dalton.tools.feishu_tools import create_feishu_tools

        async def _failed(route: str, body: dict[str, Any]) -> dict[str, Any]:
            assert route == "doc/create"
            return dict(_ALL_IMAGES_FAILED)

        monkeypatch.setattr(
            ft_mod, "post_bridge_feishu", create_autospec(post_bridge_feishu, side_effect=_failed)
        )
        monkeypatch.setattr(
            ft_mod, "get_user_tier", create_autospec(ft_mod.get_user_tier, return_value="internal")
        )
        payload = json.loads(
            await create_feishu_tools()[2].ainvoke(
                {"title": "T", "markdown": "# hi\n\n![chart](https://example.test/chart.png)"},
                config=_run_config(),
            )
        )
        assert payload["ok"] is False and payload["error"] == "image_bind_failed"
        assert payload["document_id"] == "doccnImgFail"
        assert payload["url"] == "https://feishu.cn/docx/doccnImgFail"
        assert payload["images"]["total"] == 2 and payload["images"]["bound"] == 0
        reasons = [f["reason"] for f in payload["images"]["failed"]]
        assert any("10.0.0.5" in r for r in reasons)
        assert any("回读校验失败" in r for r in reasons)

    @pytest.mark.asyncio
    async def test_append_keeps_doc_location_and_each_failure_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.dalton.tools.feishu_tools as ft_mod
        from src.dalton.feishu import post_bridge_feishu
        from src.dalton.tools.feishu_tools import create_feishu_tools

        async def _failed(route: str, body: dict[str, Any]) -> dict[str, Any]:
            assert route == "doc/append"
            return dict(_ALL_IMAGES_FAILED)

        monkeypatch.setattr(
            ft_mod, "post_bridge_feishu", create_autospec(post_bridge_feishu, side_effect=_failed)
        )
        monkeypatch.setattr(
            ft_mod, "get_user_tier", create_autospec(ft_mod.get_user_tier, return_value="internal")
        )
        payload = json.loads(
            await create_feishu_tools()[3].ainvoke(
                {"token": "doccnImgFail", "markdown": "![x](https://example.test/a.png)"},
                config=_run_config(),
            )
        )
        assert payload["ok"] is False and payload["error"] == "image_bind_failed"
        assert payload["document_id"] == "doccnImgFail"
        assert payload["url"] == "https://feishu.cn/docx/doccnImgFail"
        assert len(payload["images"]["failed"]) == 2


# ---------------------------------------------------------------------------
# #10: open_id lookup uses the real batch_get_id shape
# ---------------------------------------------------------------------------


class TestOpenIdLookup:
    def test_posts_emails_body_and_reads_user_list(self) -> None:
        client = create_autospec(rb.FeishuClient, instance=True)
        client._request.return_value = {
            "user_list": [
                {"email": "a@corp.com", "user_id": "ou_123"},
                {"email": "b@corp.com", "user_id": ""},
            ]
        }
        open_id = rb.FeishuClient.user_open_id_by_email(client, "a@corp.com")
        assert open_id == "ou_123"
        client._request.assert_called_once_with(
            "POST",
            "/open-apis/contact/v3/users/batch_get_id",
            query={"user_id_type": "open_id"},
            json_body={"emails": ["a@corp.com"]},
        )

    def test_unmatched_email_returns_empty_and_grant_falls_back(self) -> None:
        client = create_autospec(rb.FeishuClient, instance=True)
        client._request.return_value = {"user_list": [{"email": "b@corp.com", "user_id": ""}]}
        assert rb.FeishuClient.user_open_id_by_email(client, "a@corp.com") == ""
        # grant_full_access keeps the email spelling as the fallback.
        client.user_open_id_by_email.return_value = ""
        client._request.side_effect = [
            {"code": 0},  # email-form permission grant
        ]
        spelling = rb.FeishuClient.grant_full_access(client, "docx-1", "a@corp.com")
        assert spelling == "email:a@corp.com"


# ---------------------------------------------------------------------------
# #11: docx_image upload carries the `extra` drive_route_token field
# ---------------------------------------------------------------------------


def test_upload_docx_image_sends_extra_drive_route_token(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeHTTPResponse(io.BytesIO):
        status = 200

    captured: dict[str, Any] = {}
    opener_cls = create_autospec(rb.urllib.request.build_opener)
    opener = opener_cls.return_value

    def _open(req: Any, timeout: Any = None) -> _FakeHTTPResponse:
        captured["data"] = req.data
        captured["headers"] = dict(req.header_items())
        return _FakeHTTPResponse(
            json.dumps({"code": 0, "data": {"file_token": "tok_x"}}).encode()
        )

    opener.open.side_effect = _open
    monkeypatch.setattr(rb.urllib.request, "build_opener", opener_cls)

    client = create_autospec(rb.FeishuClient, instance=True)
    client.timeout_s = 30.0
    client._tenant_access_token.return_value = "tenant-token"
    token = rb.FeishuClient.upload_docx_image(
        client,
        document_id="docxRoute1",
        image_block_id="blk1",
        filename="a.png",
        data=b"\x89PNG",
    )
    assert token == "tok_x"
    body: bytes = captured["data"]
    assert b'name="extra"' in body
    assert b'"drive_route_token": "docxRoute1"' in body
    assert b'name="parent_type"\r\n\r\ndocx_image' in body
