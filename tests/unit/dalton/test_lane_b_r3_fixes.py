"""Lane-B round-3 fixes: the previous-close daily-rows memo (#1), the wiki
`allow_similar` override (#2), the sales-notes canonical duplicate field
(#3), and the pinned-dial image fetch (#4, rebinding).

All doubles use `unittest.mock.create_autospec`; no network, no real
Feishu/FMP/yfinance/OpenClaw calls.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import httpx
import pandas as pd
import pytest
import yfinance as yf

from src.dalton.tools import ingest_tools

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


@pytest.fixture(autouse=True)
def _isolated_prev_close_daily_cache():
    from src.data_client.yfinance import data_source as _ds

    _ds._clear_prev_close_daily_cache()
    yield
    _ds._clear_prev_close_daily_cache()


# ---------------------------------------------------------------------------
# #1: the (symbol, trading date) daily-rows memo
# ---------------------------------------------------------------------------


def _daily_ticker(frame: pd.DataFrame | None) -> Any:
    ticker = create_autospec(yf.Ticker, instance=True)
    ticker.history.side_effect = (
        lambda *a, **kw: frame if kw.get("period") == "1mo" else pd.DataFrame()
    )
    return ticker


def _frame(closes: list[float]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(f"2026-10-{20 + i}") for i in range(len(closes))])
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes,
         "Volume": [1_000_000] * len(closes)},
        index=idx,
    )


class TestDailyRowsMemo:
    def test_second_snapshot_within_ttl_skips_the_history_request(self) -> None:
        from src.data_client.yfinance.data_source import _recent_daily_rows_cached

        frame = _frame([10.0, 10.5, 11.0])
        ticker = _daily_ticker(frame)
        first = _recent_daily_rows_cached("AAA", "2026-10-22", ticker)
        second = _recent_daily_rows_cached("AAA", "2026-10-22", ticker)

        assert first == second == [
            ("2026-10-20", 10.0, 1_000_000),
            ("2026-10-21", 10.5, 1_000_000),
            ("2026-10-22", 11.0, 1_000_000),
        ]
        # One history request served both snapshots.
        assert ticker.history.call_count == 1

    def test_a_new_trading_date_refetches(self) -> None:
        from src.data_client.yfinance.data_source import _recent_daily_rows_cached

        ticker = _daily_ticker(_frame([10.0, 10.5]))
        _recent_daily_rows_cached("AAA", "2026-10-21", ticker)
        _recent_daily_rows_cached("AAA", "2026-10-22", ticker)
        assert ticker.history.call_count == 2

    def test_expired_entry_refetches(self) -> None:
        import time

        from src.data_client.yfinance import data_source as yf_ds

        stale_rows = [("2026-10-20", 10.0, 1_000_000)]
        yf_ds._daily_rows_cache[("AAA", "2026-10-21")] = (
            time.monotonic() - yf_ds._DAILY_ROWS_CACHE_TTL_S - 1.0,
            stale_rows,
        )
        ticker = _daily_ticker(_frame([10.0, 10.5]))
        rows = yf_ds._recent_daily_rows_cached("AAA", "2026-10-21", ticker)
        assert ticker.history.call_count == 1  # the stale entry was not served
        assert rows == [
            ("2026-10-20", 10.0, 1_000_000),
            ("2026-10-21", 10.5, 1_000_000),
        ]

    def test_a_failed_fetch_is_never_memoized(self) -> None:
        from src.data_client.yfinance.data_source import _recent_daily_rows_cached

        ticker = _daily_ticker(None)  # unreadable frame — None rows
        assert _recent_daily_rows_cached("AAA", "2026-10-21", ticker) is None
        assert _recent_daily_rows_cached("AAA", "2026-10-21", ticker) is None
        # An unverifiable quote stays retryable: both calls hit the history API.
        assert ticker.history.call_count == 2


# ---------------------------------------------------------------------------
# #2: allow_similar — a confirmed revision of a similar memo can land
# ---------------------------------------------------------------------------


_BASE_MEMO = "专家表示公司三季度收入增长稳健，资本开支节奏放缓，AI 服务器订单能见度延伸到 2027 年。"
_REVISED_MEMO = _BASE_MEMO + "毛利率环比改善约 1.2 个百分点。"


def _wiki_bridge_with_similar_entry(tmp_path: Path) -> rb.ResearchDataBridge:
    bridge = rb.ResearchDataBridge.__new__(rb.ResearchDataBridge)
    bridge.cfg = create_autospec(rb.BridgeConfig, instance=True)
    bridge.cfg.wiki_ingest_audit_file = tmp_path / "wiki_ingest_audit.jsonl"
    bridge.cfg.ocw_root = tmp_path / "ocw"
    (tmp_path / "wiki_ingest_idempotency.json").write_text(
        json.dumps(
            {
                "req-1": {
                    "request_id": "req-1",
                    "status": "ingested",
                    "sha256": "a" * 64,
                    "target": "NVDA",
                    "date": "2026-10-25",
                    "content_type": "专家访谈",
                    "doc_id": 2048,
                    "filepath": "wiki/companies/NVDA/2026-10-25-专家访谈.md",
                    "_norm_excerpt": _BASE_MEMO,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return bridge


class TestWikiAllowSimilarBridge:
    @pytest.mark.asyncio
    async def test_similar_revision_is_flagged_without_the_flag(self, tmp_path: Path) -> None:
        bridge = _wiki_bridge_with_similar_entry(tmp_path)
        dup = await bridge._check_wiki_duplicate(
            target_type="company",
            resolved_slug="NVDA",
            canonical_content_type="专家访谈",
            date_str="2026-10-25",
            content_sha256="b" * 64,
            norm_text=_REVISED_MEMO,
            wiki_cfg={},
        )
        assert dup is not None and dup["reason"] == "similar_content"

    @pytest.mark.asyncio
    async def test_allow_similar_skips_similarity_but_not_the_fingerprint(
        self, tmp_path: Path
    ) -> None:
        bridge = _wiki_bridge_with_similar_entry(tmp_path)
        dup = await bridge._check_wiki_duplicate(
            target_type="company",
            resolved_slug="NVDA",
            canonical_content_type="专家访谈",
            date_str="2026-10-25",
            content_sha256="b" * 64,
            norm_text=_REVISED_MEMO,
            wiki_cfg={},
            allow_similar=True,
        )
        assert dup is None  # a revision of a similar memo may land

        exact = await bridge._check_wiki_duplicate(
            target_type="company",
            resolved_slug="NVDA",
            canonical_content_type="专家访谈",
            date_str="2026-10-25",
            content_sha256="a" * 64,
            norm_text=_BASE_MEMO,
            wiki_cfg={},
            allow_similar=True,
        )
        assert exact is not None and exact["reason"] == "sha256_match"


class TestIngestResearchAllowSimilar:
    @pytest.fixture(autouse=True)
    def _internal_manual_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        monkeypatch.setattr(
            ingest_tools,
            "get_workspace_auto_ingest",
            create_autospec(ingest_tools.get_workspace_auto_ingest, return_value=False),
        )
        monkeypatch.setattr(
            ingest_tools,
            "check_and_increment_wiki_ingest_rate_limit",
            create_autospec(
                ingest_tools.check_and_increment_wiki_ingest_rate_limit,
                return_value=(True, 1),
            ),
        )

    @pytest.mark.asyncio
    async def test_confirmed_override_rides_only_the_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.tools import wiki_tools

        bridge = create_autospec(
            wiki_tools.call_bridge_wiki_ingest,
            return_value=(200, {"ok": True, "status": "ingested", "doc_id": 2049}),
        )
        monkeypatch.setattr(wiki_tools, "call_bridge_wiki_ingest", bridge)
        tool = ingest_tools.build_ingest_research_tool(None, allow_home_workspace=False)
        args = {
            "speaker": "expert_or_company",
            "target_type": "company",
            "target": "NVDA",
            "content_type": "专家访谈",
            "date": "2026-10-25",
            "content": _REVISED_MEMO,
        }

        # Preview after the duplicate question: the flag never rides a preview.
        await tool.ainvoke({**args, "allow_similar": True}, config=_run_config())
        assert bridge.call_args.args[0].get("allow_similar") is None
        assert bridge.call_args.args[0]["dry_run"] is True

        # The confirmed "ingest anyway" write carries it.
        await tool.ainvoke(
            {**args, "allow_similar": True, "confirm": True}, config=_run_config()
        )
        write_payload = bridge.call_args.args[0]
        assert write_payload["dry_run"] is False
        assert write_payload["allow_similar"] is True

    @pytest.mark.asyncio
    async def test_manual_similar_preview_asks_and_names_the_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Auto-ingest off: a similar-content preview turns into the same
        duplicate_suspected question, so the confirmed retry carries the flag;
        an exact duplicate stays the bridge's plain answer."""
        from src.dalton.tools import wiki_tools

        similar = {"ok": False, "status": "duplicate", "duplicate": True,
                   "duplicate_reason": "similar_content", "existing_filepath": "wiki/a.md"}
        exact = {"ok": False, "status": "duplicate", "duplicate": True,
                 "duplicate_reason": "sha256", "existing_filepath": "wiki/a.md"}
        bridge = create_autospec(wiki_tools.call_bridge_wiki_ingest, return_value=(400, similar))
        monkeypatch.setattr(wiki_tools, "call_bridge_wiki_ingest", bridge)
        tool = ingest_tools.build_ingest_research_tool(None, allow_home_workspace=False)
        args = {
            "speaker": "expert_or_company",
            "target_type": "company",
            "target": "NVDA",
            "content_type": "专家访谈",
            "date": "2026-10-25",
            "content": _REVISED_MEMO,
        }

        out = json.loads(await tool.ainvoke(args, config=_run_config()))
        assert out["status"] == "duplicate_suspected"
        assert "allow_similar=True" in out["question"]

        bridge.return_value = (400, exact)
        out = json.loads(await tool.ainvoke(args, config=_run_config()))
        assert out["status"] == "duplicate"

    @pytest.mark.asyncio
    async def test_auto_ingest_alone_never_carries_the_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Auto-ingest writes without confirm; a model passing allow_similar on
        that path must still get the similarity check (preview and write)."""
        from src.dalton.tools import wiki_tools

        # The class fixture already installed an autospec; flip it to on.
        ingest_tools.get_workspace_auto_ingest.return_value = True
        bridge = create_autospec(
            wiki_tools.call_bridge_wiki_ingest,
            return_value=(200, {"ok": True, "status": "ingested", "doc_id": 2050}),
        )
        monkeypatch.setattr(wiki_tools, "call_bridge_wiki_ingest", bridge)
        tool = ingest_tools.build_ingest_research_tool(None, allow_home_workspace=False)

        await tool.ainvoke(
            {
                "speaker": "expert_or_company",
                "target_type": "company",
                "target": "NVDA",
                "content_type": "专家访谈",
                "date": "2026-10-25",
                "content": _REVISED_MEMO,
                "allow_similar": True,
            },
            config=_run_config(),
        )
        assert bridge.call_count >= 1
        for call in bridge.call_args_list:
            assert "allow_similar" not in call.args[0]

    @pytest.mark.asyncio
    async def test_sales_notes_leg_ignores_the_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DALTON_BRIDGE_URL", "http://127.0.0.1:8960")
        monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", "test-admin-token")
        captured: list[httpx.Request] = []
        responses = [
            httpx.Response(200, json={"ok": True, "status": "ingested", "doc_key": "k1"}),
        ]
        real_cls = httpx.AsyncClient

        def _handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return responses.pop(0)

        def _factory(**kwargs: Any):
            return real_cls(transport=httpx.MockTransport(_handler), timeout=kwargs.get("timeout"))

        monkeypatch.setattr(
            ingest_tools.httpx, "AsyncClient", create_autospec(real_cls, side_effect=_factory)
        )
        tool = ingest_tools.build_ingest_research_tool(None, allow_home_workspace=False)
        result = json.loads(
            await tool.ainvoke(
                {
                    "speaker": "sell_side",
                    "institution": "MS",
                    "title": "T",
                    "content": "color",
                    "allow_similar": True,
                    "confirm": True,
                },
                config=_run_config(),
            )
        )
        assert result["ok"] is True
        sent = json.loads(captured[-1].content.decode("utf-8"))
        assert "allow_similar" not in sent


class TestDuplicateQuestionMentionsOverride:
    def test_question_names_the_confirm_and_override_args(self) -> None:
        question = json.loads(
            ingest_tools._duplicate_question(
                "company-wiki",
                {"status": "duplicate", "duplicate": True,
                 "duplicate_reason": "similar_content", "existing_doc_id": 2048},
            )
        )
        assert "confirm=True" in question["question"]
        assert "allow_similar=True" in question["question"]


# ---------------------------------------------------------------------------
# #3: the sales-notes preview's canonical duplicate verdict
# ---------------------------------------------------------------------------


def _sn_bridge(tmp_path: Path) -> rb.ResearchDataBridge:
    bridge = rb.ResearchDataBridge.__new__(rb.ResearchDataBridge)
    bridge.cfg = create_autospec(rb.BridgeConfig, instance=True)
    bridge.cfg.wiki_ingest_timeout_s = 10.0
    bridge.cfg.wiki_ingest_temp_dir = Path("/tmp/unused-sn-r3")
    bridge.cfg.env_path = Path("/nonexistent-bridge.env")
    bridge.cfg.ocw_root = Path("/tmp/unused-ocw-r3")
    bridge.cfg.subprocess_proxy = ""
    return bridge


async def _run_sn_dry_run(monkeypatch: pytest.MonkeyPatch, cli_stdout: str) -> dict[str, Any]:
    """Run a sales-notes dry run against a stubbed market-digest CLI."""
    from unittest.mock import patch

    bridge = _sn_bridge(Path("/tmp"))
    proc = create_autospec(asyncio.subprocess.Process, instance=True)
    proc.returncode = 0
    proc.communicate.return_value = (cli_stdout.encode("utf-8"), b"")

    async def fake_exec(*cmd, **kwargs):
        return proc

    with (
        patch.object(
            rb.ResearchDataBridge,
            "_resolve_market_digest_run_sh",
            new=create_autospec(
                rb.ResearchDataBridge._resolve_market_digest_run_sh,
                return_value=Path("/nonexistent/run.sh"),
            ),
        ),
        patch.object(
            rb.asyncio,
            "create_subprocess_exec",
            new=create_autospec(rb.asyncio.create_subprocess_exec, side_effect=fake_exec),
        ),
    ):
        _code, payload = await bridge.handle_sales_notes_ingest(
            {
                "institution": "MS",
                "title": "T",
                "content": "正文",
                "ingested_by": {"user_id": USER_ID},
                "dry_run": True,
            }
        )
    return payload


class TestSalesNotesCanonicalDuplicate:
    @pytest.mark.asyncio
    async def test_cli_status_duplicate_sets_the_canonical_field(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = await _run_sn_dry_run(
            monkeypatch, '{"status": "duplicate", "doc_key": "abc123"}'
        )
        assert payload["status"] == "dry_run"
        assert payload["duplicate"] is True

    @pytest.mark.asyncio
    async def test_cli_result_field_and_mixed_case_are_normalized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = await _run_sn_dry_run(monkeypatch, '{"result": "Duplicate", "doc_key": "abc"}')
        assert payload["duplicate"] is True

    @pytest.mark.asyncio
    async def test_clean_preview_is_not_duplicate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = await _run_sn_dry_run(
            monkeypatch, '{"status": "ok", "doc_key": "new456", "chunks": 3}'
        )
        assert payload["duplicate"] is False


class TestSuspectedDuplicateField:
    def test_tool_reads_only_the_canonical_field(self) -> None:
        assert ingest_tools._suspected_duplicate(
            {"ok": True, "status": "dry_run", "duplicate": True}
        )
        assert ingest_tools._suspected_duplicate({"ok": False, "status": "duplicate"})
        assert not ingest_tools._suspected_duplicate(
            {"ok": True, "status": "dry_run", "duplicate": False}
        )
        # The old un-normalized shape is no longer honored: the bridge owns
        # the normalization, and the tool must not replay it.
        assert not ingest_tools._suspected_duplicate(
            {"ok": True, "status": "dry_run", "cli_preview": {"status": "duplicate"}}
        )


# ---------------------------------------------------------------------------
# #4: the pinned dial keeps Host/SNI on the URL's name
# ---------------------------------------------------------------------------


class TestPinnedConnections:
    def test_http_dial_uses_the_checked_ip_and_keeps_the_host_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dialed: list[tuple[str, int]] = []

        def _create_connection(address, timeout=None, source_address=None):
            dialed.append(address)
            sock = create_autospec(rb.socket.socket, instance=True)
            return sock

        monkeypatch.setattr(rb.socket, "create_connection", _create_connection)
        conn = rb._PinnedHTTPConnection("images.example.test", 80, pinned_ip="93.184.216.34")
        assert conn.host == "images.example.test"  # the Host header's source
        conn.connect()
        assert dialed == [("93.184.216.34", 80)]

    def test_https_tls_handshake_uses_the_original_hostname(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ssl

        sock = create_autospec(rb.socket.socket, instance=True)
        monkeypatch.setattr(
            rb.socket,
            "create_connection",
            lambda address, timeout=None, source_address=None: sock,
        )
        conn = rb._PinnedHTTPSConnection(
            "images.example.test", 443, pinned_ip="93.184.216.34"
        )
        context = create_autospec(ssl.SSLContext, instance=True)
        context.wrap_socket.return_value = sock
        conn._context = context
        conn.connect()
        # SNI and certificate hostname validation run against the URL's name.
        _args, kwargs = context.wrap_socket.call_args
        assert kwargs.get("server_hostname") == "images.example.test"
