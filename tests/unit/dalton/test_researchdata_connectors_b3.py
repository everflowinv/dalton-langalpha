"""Unit tests for Research Data Bridge Split — Batch 3 (researchdata-connectors.md §6.4).

Covers:
- `dalton_sec` 11 operations parameter & response-shape parity vs legacy bridge fixtures
- `dalton_macro` FRED (incl. ALFRED `vintage`) & Treasury (`tga`, `debt`) + partial pagination
- `research-host` 3-class credential isolation & HKDF per-request caller token verification
- Five research connectors (`company-wiki`, `guidepoint`, `sales-notes`, `tmtb`, `funda-ai`)
- Federated `all` & `sales_mail` per-source capability enforcement, `"partial"` timeout handling,
  and Batch 3 AlphaEngine proxy leg
- Ingest idempotency (`request_id` replay, `sha256` duplicate, `dry_run`) + pre/post-cutover routing
- Everflow pre-cutover parity (new connectors off until `DALTON_RESEARCH_HOST_URL` is set) and
  post-cutover single-authority cutoff on legacy `researchdata_bridge`
- Dual-profile capability gating (Core vs Everflow), `DALTON_EXTRA_PLUGINS_DIR`, `docker-compose.core.yml`,
  `package_release.sh` allowlist/denylist, `connector_transport.py`, `workflow.js`, host Guidepoint scripts,
  `bridge_subprocess_env.py` profile resolution, and `switch_research_host.sh`
"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import httpx
import pytest
import yaml

import src.dalton.settings as dalton_settings
from src.dalton.capabilities import (
    BUNDLED_PLUGIN_CAPABILITY_MAP,
    EXTRA_ONLY_BUNDLED_PLUGINS,
    MCP_SERVER_CAPABILITY_MAP,
    CapabilityId,
)
from src.dalton.content_assembly import read_assembled_text
from src.dalton.data_map import builder as dm_builder
from src.dalton.profile import DeploymentProfile
from src.dalton.provenance.declared_sources import expand_declared_sources
from src.dalton.research_host.app import ResearchHostConfig, ResearchHostRuntime, create_app
from src.dalton.research_host.auth import verify_caller_token
from src.dalton.research_host.connector_registry import ConnectorRegistry
from src.dalton.research_host.federated import FederatedResearchConnector
from src.dalton.research_host.wiki_store import CompanyWikiConnector, OpenWikiStore
from src.dalton.sandbox_caller_identity import sign_caller_token
from src.dalton.tools import build_dalton_tools
from src.dalton.tools import ingest_tools as ingest_tools_mod
from src.dalton.tools import wiki_tools as wiki_tools_mod

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_module_from_path(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _clean_profile_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: str = "core",
) -> None:
    for key in list(os.environ):
        if key.startswith(
            (
                "DALTON_CAP_",
                "DALTON_RESEARCH_",
                "DALTON_EXTRA_PLUGINS",
                "DALTON_BRIDGE_B3_",
                "DALTON_SEC_FULL_TOOLS",
                "DALTON_SEC_EXTENDED_TOOLS",
            )
        ):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("APP_ENV", "core" if profile == "core" else "dalton")
    monkeypatch.setenv("DALTON_PROFILE", profile)
    monkeypatch.setenv("DALTON_ENABLED", "true")
    monkeypatch.setenv("DALTON_HOST_PLATFORM", "linux" if profile == "core" else "macos")


# ---------------------------------------------------------------------------
# 1. dalton_sec: 11 operations parameter & shape parity
# ---------------------------------------------------------------------------


def _load_dalton_sec_module(mod_name: str = "_test_dalton_sec_b3") -> Any:
    return _load_module_from_path(
        mod_name,
        REPO_ROOT / "plugins" / "dalton_sec" / "dalton_sec_mcp_server.py",
    )


def _sample_company_facts_payload() -> dict[str, Any]:
    return {
        "cik": 320193,
        "entityName": "Apple Inc.",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "label": "Revenues",
                    "description": "Total revenue from sales",
                    "units": {
                        "USD": [
                            {
                                "start": "2026-04-01",
                                "end": "2026-06-30",
                                "val": 95000000000,
                                "accn": "0000320193-26-000050",
                                "fy": 2026,
                                "fp": "Q3",
                                "form": "10-Q",
                                "filed": "2026-08-01",
                            }
                        ]
                    },
                },
                "GrossProfit": {
                    "label": "Gross Profit",
                    "description": "Gross profit",
                    "units": {
                        "USD": [
                            {
                                "start": "2026-04-01",
                                "end": "2026-06-30",
                                "val": 43700000000,
                                "accn": "0000320193-26-000050",
                                "fy": 2026,
                                "fp": "Q3",
                                "form": "10-Q",
                                "filed": "2026-08-01",
                            }
                        ]
                    },
                },
            }
        },
    }


def _sample_submissions_payload() -> dict[str, Any]:
    return {
        "cik": "0000320193",
        "name": "Apple Inc.",
        "filings": {
            "recent": {
                "accessionNumber": ["0000320193-26-000050", "0000320193-26-000044"],
                "form": ["10-Q", "8-K"],
                "filingDate": ["2026-08-01", "2026-07-30"],
                "reportDate": ["2026-06-30", "2026-07-30"],
                "primaryDocument": ["aapl-20260630.htm", "aapl-8k.htm"],
            }
        },
    }


def test_dalton_sec_list_filings_single_form_override_and_concept_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_SEC_EXTENDED_TOOLS", "1")
    mod = _load_dalton_sec_module()
    monkeypatch.setattr(
        mod,
        "resolve_cik",
        create_autospec(mod.resolve_cik, return_value=("0000320193", "Apple Inc.")),
    )
    monkeypatch.setattr(
        mod,
        "fetch_submissions",
        create_autospec(mod.fetch_submissions, return_value=_sample_submissions_payload()),
    )
    monkeypatch.setattr(
        mod,
        "fetch_company_facts",
        create_autospec(mod.fetch_company_facts, return_value=_sample_company_facts_payload()),
    )

    # Single-value `form` overrides default multi-form `forms`
    res_8k = mod.list_filings(ticker="AAPL", form="8-K", limit=3)
    assert res_8k["ok"] is True
    assert res_8k["status"] == "ok"
    assert res_8k["ticker"] == "AAPL"
    assert len(res_8k["rows"]) == 1
    assert res_8k["rows"][0]["form"] == "8-K"
    assert res_8k["_sources"]
    assert res_8k["_declared_sources"]

    # Omitting `form` uses default multi-form filter (10-K,10-Q,20-F,6-K)
    res_default = mod.list_filings(ticker="AAPL", limit=5)
    assert res_default["ok"] is True
    assert [r["form"] for r in res_default["rows"]] == ["10-Q"]

    # Raw concept facts, concept search, smart facts
    r_cf = mod.get_concept_facts(ticker="AAPL", concept="Revenues", form="10-Q", limit=5)
    assert r_cf["ok"] is True
    assert r_cf["rows"][0]["numeric_value"] == 95000000000

    r_sc = mod.search_concepts(ticker="AAPL", query="revenue")
    assert r_sc["ok"] is True
    assert any(m["concept"].endswith("Revenues") for m in r_sc["concepts"])

    r_sf = mod.search_smart_facts(ticker="AAPL", query="revenue", form="10-Q", limit=10)
    assert r_sf["ok"] is True
    assert r_sf["rows"]


def test_dalton_sec_financials_ratios_text_attachments_events_insiders_and_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_dalton_sec_module()
    monkeypatch.setattr(
        mod,
        "resolve_cik",
        create_autospec(mod.resolve_cik, return_value=("0000320193", "Apple Inc.")),
    )

    class _FakeStatement:
        def __init__(self, text: str, fail: bool = False) -> None:
            self._text = text
            self._fail = fail

        def __str__(self) -> str:
            if self._fail:
                raise RuntimeError("cashflow parse error")
            return self._text

    class _FakeAttachment:
        def __init__(self, doc: str, dtype: str, desc: str, body: str) -> None:
            self.document = doc
            self.document_type = dtype
            self.description = desc
            self._body = body

        def text(self) -> str:
            return self._body

    class _FakeFilingObj:
        def __init__(self, financials: Any) -> None:
            self.financials = financials
            self.items = ["2.02"]

        def __getitem__(self, item: str) -> str:
            return f"Item {item} MD&A: gross margin expanded 120 bps."

        def __str__(self) -> str:
            return "Tim Cook sold 50,000 shares (Code S)"

    class _FakeFiling:
        def __init__(self, form: str, fdate: str, accn: str, fail_obj: bool = False) -> None:
            self.form = form
            self.filing_date = fdate
            self.report_date = "2026-06-30"
            self.accession_no = accn
            self._fail_obj = fail_obj
            self.attachments = [
                _FakeAttachment("ex99-1.htm", "EX-99.1", "Earnings Release", "Q3 revenue $95B"),
            ]

        def text(self) -> str:
            return "Overview: gross margin expanded 120 bps in Q3 due to services mix."

        def obj(self) -> Any:
            if self._fail_obj:
                raise RuntimeError("corrupt filing XBRL")
            fin = SimpleNamespace(
                income_statement=_FakeStatement("Revenue  95,000\nGross Profit  43,700\nNet Income  24,000"),
                balance_sheet=_FakeStatement("Total Assets  350,000\nTotal Equity  65,000"),
                cash_flow_statement=_FakeStatement("Operating Cash Flow  30,000"),
            )
            return _FakeFilingObj(fin)

    class _FakeFilingsCollection:
        def __init__(self, items: list[Any]) -> None:
            self._items = items

        def __len__(self) -> int:
            return len(self._items)

        def __iter__(self):
            return iter(self._items)

        def head(self, n: int) -> list[Any]:
            return self._items[:n]

        def filter(self, *, accession_number: str | None = None) -> _FakeFilingsCollection:
            if not accession_number:
                return self
            return _FakeFilingsCollection(
                [item for item in self._items if getattr(item, "accession_no", None) == accession_number]
            )

    def _fake_edgar_filings(
        company: Any,
        *,
        form: str | None = None,
        as_of: str | None = None,
    ) -> _FakeFilingsCollection:
        _ = (company, as_of)
        if form == "8-K":
            return _FakeFilingsCollection([_FakeFiling("8-K", "2026-07-30", "0000320193-26-000044")])
        if form == "4":
            return _FakeFilingsCollection([_FakeFiling("4", "2026-08-05", "0000320193-26-000055")])
        return _FakeFilingsCollection(
            [
                _FakeFiling("10-Q", "2026-08-01", "0000320193-26-000050"),
                _FakeFiling("10-Q", "2026-05-01", "0000320193-26-000020", fail_obj=True),
            ]
        )

    monkeypatch.setattr(
        mod,
        "_get_edgar_company",
        create_autospec(mod._get_edgar_company, return_value=SimpleNamespace(ticker="AAPL")),
    )
    monkeypatch.setattr(
        mod,
        "_edgar_filings",
        create_autospec(mod._edgar_filings, side_effect=_fake_edgar_filings),
    )

    # Financials: partial failure across 2 filings -> ok=True, status="partial", warnings non-empty
    r_fin = mod.get_financials(ticker="AAPL", form="10-Q", limit=2, statement="all")
    assert r_fin["ok"] is True
    assert r_fin["status"] == "partial"
    assert r_fin["warnings"]

    # Invalid statement rejected
    assert mod.get_financials(ticker="AAPL", statement="bogus")["ok"] is False

    # Ratios
    r_rat = mod.get_ratios(ticker="AAPL", form="10-Q")
    assert r_rat["ok"] is True

    # Bound validations
    assert mod.search_filing_text(ticker="AAPL", query="margin", window=0)["ok"] is False
    assert mod.search_filing_text(ticker="AAPL", query="margin", max_matches=101)["ok"] is False
    assert mod.read_filing_item(ticker="AAPL", item="7", max_chars=500001)["ok"] is False
    assert mod.get_filing_attachments(ticker="AAPL", accession="bad-acc")["ok"] is False
    assert mod.get_filing_attachments(
        ticker="AAPL",
        accession="0000320193-26-000050",
        document="ex99-1.htm",
        document_type="EX-99.1",
    )["ok"] is False

    # Valid text search, attachments list & read, 8-K events, Form 4 insiders
    r_txt = mod.search_filing_text(ticker="AAPL", query="margin")
    assert r_txt["ok"] is True and r_txt["filings"][0]["matches"]

    r_att = mod.get_filing_attachments(ticker="AAPL", accession="0000320193-26-000050")
    assert r_att["ok"] is True and r_att["attachments"][0]["document_type"] == "EX-99.1"

    r_att_read = mod.get_filing_attachments(
        ticker="AAPL", accession="0000320193-26-000050", document="ex99-1.htm"
    )
    assert r_att_read["ok"] is True and "Q3 revenue" in r_att_read["text"]

    r_8k = mod.list_eight_k_events(ticker="AAPL", limit=3)
    assert r_8k["ok"] is True and "2.02" in r_8k["rows"][0]["items"]

    r_ins = mod.list_insider_transactions(ticker="AAPL", limit=3)
    assert r_ins["ok"] is True and "Tim Cook" in r_ins["rows"][0]["content"]


# ---------------------------------------------------------------------------
# 2. dalton_macro: FRED (live + ALFRED vintage) & Treasury (tga / debt)
# ---------------------------------------------------------------------------


def _load_dalton_macro_module() -> Any:
    return _load_module_from_path(
        "_test_dalton_macro_b3",
        REPO_ROOT / "plugins" / "dalton_macro" / "dalton_macro_mcp_server.py",
    )


def test_dalton_macro_fred_live_and_vintage_and_treasury_tga_debt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_dalton_macro_module()

    def _fake_http_get_text(url: str, *, timeout: float = 25.0) -> str:
        _ = timeout
        if "fredgraph.csv" in url and "vintage_date=" in url:
            return "observation_date,CPIAUCSL_20260315\n2026-01-01,318.2\n2026-02-01,.\n"
        if "fredgraph.csv" in url:
            return "observation_date,FEDFUNDS\n2026-05-01,4.25\n2026-06-01,4.00\n"
        if "operating_cash_balance" in url:
            return json.dumps(
                {
                    "data": [
                        {
                            "record_date": "2026-08-01",
                            "account_type": "Treasury General Account (TGA) Closing Balance",
                            "open_today_bal": "752300",
                            "close_today_bal": None,
                        }
                    ],
                    "meta": {"total-pages": 1, "total-count": 1},
                }
            )
        if "debt_to_penny" in url:
            return json.dumps(
                {
                    "data": [
                        {
                            "record_date": "2026-08-01",
                            "debt_held_public_amt": "28000000000000.00",
                            "intragov_hold_amt": "7000000000000.00",
                            "tot_pub_debt_out_amt": "35000000000000.00",
                        }
                    ],
                    "meta": {"total-pages": 3, "total-count": 3},
                }
            )
        raise AssertionError(f"unexpected URL: {url}")

    spy_fetch = create_autospec(mod._http_get_text, side_effect=_fake_http_get_text)
    monkeypatch.setattr(mod, "_http_get_text", spy_fetch)

    # Live FRED via fetch_fred_series (uses explicit fetch=) and MCP wrapper get_fred_series
    r_live = mod.fetch_fred_series(
        series="FEDFUNDS", start="2026-05-01", end="2026-06-01", fetch=spy_fetch
    )
    assert r_live["ok"] is True
    assert r_live["series"] == "FEDFUNDS"
    assert len(r_live["rows"]) == 2
    assert r_live["_sources"]

    # ALFRED vintage preserves "." missing observations as value=None
    r_vint = mod.fetch_fred_series(
        series="CPIAUCSL",
        start="2026-01-01",
        end="2026-03-01",
        vintage="2026-03-15",
        fetch=spy_fetch,
    )
    assert r_vint["ok"] is True
    assert r_vint["vintage"] == "2026-03-15"
    assert len(r_vint["rows"]) == 2
    assert r_vint["rows"][0] == {"observation_date": "2026-01-01", "value": "318.2"}
    assert r_vint["rows"][1] == {"observation_date": "2026-02-01", "value": None}

    # Invalid date rejected by MCP wrapper
    assert mod.get_fred_series(series="FEDFUNDS", start="not-a-date", end="2026-06-01")["ok"] is False

    # Treasury TGA
    r_tga = mod.fetch_treasury_data(
        dataset="tga", start="2026-08-01", end="2026-08-05", fetch=spy_fetch
    )
    assert r_tga["ok"] is True
    assert r_tga["status"] == "ok"
    assert r_tga["rows"][0]["value"] == "752300"

    # Treasury Debt with max_pages=1 while total-pages=3 -> partial status + reconciliation check
    r_debt = mod.fetch_treasury_data(
        dataset="debt", start="2026-08-01", end="2026-08-05", max_pages=1, fetch=spy_fetch
    )
    assert r_debt["ok"] is True
    assert r_debt["status"] == "partial"
    assert r_debt["complete"] is False
    assert r_debt["warnings"]

    cat = mod.list_macro_catalog()
    assert "FRED" in json.dumps(cat) or "DGS10" in json.dumps(cat)


# ---------------------------------------------------------------------------
# 3. research-host: 3-class credential isolation, wiki store, ingest idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_research_host_credential_isolation_and_wiki_ingest_idempotency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_profile_env(monkeypatch, profile="core")
    monkeypatch.setenv("DALTON_CAP_COMPANY_WIKI", "true")
    monkeypatch.setenv("DALTON_RESEARCH_HOST_TOKEN", "host-sandbox-token-aaa")
    monkeypatch.setenv("DALTON_RESEARCH_ADMIN_TOKEN", "admin-backend-token-bbb")
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", "hmac-caller-secret-ccc")

    assert "admin-backend-token-bbb" in dalton_settings.managed_secret_token_values()
    assert dalton_settings.redact_managed_tokens("Bearer admin-backend-token-bbb") == "Bearer ***"

    cfg = ResearchHostConfig(
        data_root=tmp_path / "wiki_root",
        mcp_token="host-sandbox-token-aaa",
        admin_token="admin-backend-token-bbb",
        extra_caller_admin_secrets=("hmac-caller-secret-ccc",),
        profile=DeploymentProfile.CORE,
    )
    def _wiki_only_caps() -> frozenset[CapabilityId]:
        return frozenset({CapabilityId.COMPANY_WIKI})

    app = create_app(cfg, capabilities_provider=_wiki_only_caps)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://research-host:8951") as client:
        hz = await client.get("/healthz")
        assert hz.status_code == 200
        assert "company-wiki" in hz.json()["connectors"]
        assert "federated" in hz.json()["connectors"]

        # Sandbox MCP token rejected on /admin/drain and /ingest/wiki
        assert (
            await client.get("/admin/drain", headers={"Authorization": "Bearer host-sandbox-token-aaa"})
        ).status_code == 401
        assert (
            await client.post(
                "/ingest/wiki",
                headers={"Authorization": "Bearer host-sandbox-token-aaa"},
                json={"request_id": "req-1", "content": "hi"},
            )
        ).status_code == 401

        # Admin token rejected on /mcp/company-wiki
        assert (
            await client.post(
                "/mcp/company-wiki",
                headers={"Authorization": "Bearer admin-backend-token-bbb"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
        ).status_code == 401

        # dry_run preview
        dry = await client.post(
            "/ingest/wiki",
            headers={"Authorization": "Bearer admin-backend-token-bbb"},
            json={
                "request_id": "req-dry-1",
                "target_type": "company",
                "target": "NVDA",
                "content_type": "研究笔记",
                "date": "2026-08-15",
                "content": "Rack yield improved to 92% in Q3.",
                "dry_run": True,
            },
        )
        assert dry.status_code == 200
        assert dry.json()["status"] == "preview"

        # Real ingest -> created; identical request_id -> idempotent_replay; new request_id same content -> duplicate
        payload = {
            "request_id": "req-wiki-001",
            "target_type": "company",
            "target": "NVDA",
            "content_type": "研究笔记",
            "date": "2026-08-15",
            "content": "Rack yield improved to 92% in Q3.",
        }
        r1 = await client.post(
            "/ingest/wiki",
            headers={"Authorization": "Bearer admin-backend-token-bbb"},
            json=payload,
        )
        assert r1.status_code == 200
        d1 = r1.json()
        assert d1["ok"] is True and d1["doc_id"]

        r2 = await client.post(
            "/ingest/wiki",
            headers={"Authorization": "Bearer admin-backend-token-bbb"},
            json=payload,
        )
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["doc_id"] == d1["doc_id"]
        assert d2["idempotent_replay"] is True

        r3 = await client.post(
            "/ingest/wiki",
            headers={"Authorization": "Bearer admin-backend-token-bbb"},
            json={**payload, "request_id": "req-wiki-002"},
        )
        assert r3.status_code == 200
        d3 = r3.json()
        assert d3["duplicate"] is True
        assert d3["existing_doc_id"] == d1["doc_id"]

        # Search via /mcp/company-wiki with sandbox MCP token + signed per-request caller token
        caller_tok = sign_caller_token(
            {"user": "user-42", "workspace": "ws-1", "thread": "th-1"},
            admin_token="hmac-caller-secret-ccc",
        )
        assert caller_tok is not None
        mcp_resp = await client.post(
            "/mcp/company-wiki",
            headers={
                "Authorization": "Bearer host-sandbox-token-aaa",
                "X-Dalton-Caller-Token": caller_tok,
                "X-Dalton-Caller-Scope": "per-request",
            },
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "search", "arguments": {"query": "Rack yield"}},
            },
        )
        assert mcp_resp.status_code == 200
        body = json.loads(mcp_resp.json()["result"]["content"][0]["text"])
        assert body["ok"] is True
        assert any(r["document_id"] == d1["doc_id"] for r in body["results"])


def test_caller_token_hkdf_roundtrip() -> None:
    tok = sign_caller_token(
        {"user": "user-abc", "workspace": "ws-1", "thread": "th-1"},
        admin_token="shared-secret-123",
    )
    assert tok is not None
    ok_id = verify_caller_token(tok, admin_secrets=("shared-secret-123",))
    assert ok_id.verified is True
    assert ok_id.user == "user-abc"
    assert ok_id.caller_key == "user:user-abc"

    bad_secret = verify_caller_token(tok, admin_secrets=("wrong-secret",))
    assert bad_secret.verified is False
    assert bad_secret.caller_key == "anonymous"

    malformed = verify_caller_token("malformed", admin_secrets=("shared-secret-123",))
    assert malformed.verified is False


# ---------------------------------------------------------------------------
# 4. Five commercial connectors + Federated `all` / `sales_mail`
# ---------------------------------------------------------------------------


def _load_commercial_connectors() -> tuple[Any, Any]:
    gp_mod = _load_module_from_path(
        "_test_gp_connector_b3",
        REPO_ROOT / "scripts" / "dalton" / "research_runtime" / "host_connectors" / "guidepoint.py",
    )
    lib_mod = _load_module_from_path(
        "_test_lib_connector_b3",
        REPO_ROOT / "scripts" / "dalton" / "research_runtime" / "host_connectors" / "library_connectors.py",
    )
    return gp_mod, lib_mod


def _seed_market_digest_db(store: Any) -> None:
    store.ensure_schema()
    with sqlite3.connect(store.mail_db) as conn:
        conn.execute(
            """
            INSERT INTO email_chunks (date, email_id, message_id, sender, subject, chunk_index, content, created_at)
            VALUES
              ('2026-08-01', 'em-1', 'mail-gs-001', 'GS', 'GS Semi Check', 0, 'CoWoS capacity rising fast', '2026-08-01T12:00:00Z'),
              ('2026-08-02', 'em-2', 'alphaengine:tmtb-001', 'TMTB', 'TMTB Cloud Note', 0, 'Hyperscaler capex +35% YoY', '2026-08-02T12:00:00Z')
            """
        )
        conn.commit()


def _seed_funda_db(data_root: Path) -> None:
    """Seed FUNDA AI using the real `funda_ai.Store.connect(write=True)` schema (`documents` table)."""
    scripts_dir = str(REPO_ROOT / "scripts" / "dalton" / "research_runtime" / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import funda_ai

    store = funda_ai.Store(data_root)
    with store.connect(write=True) as conn:
        conn.execute(
            """
            INSERT INTO documents (
                id, title, sender, date, published_at, body, body_sha,
                url, original_path, created_at, indexed_at, status
            )
            VALUES (
                1, 'TSMC N2 Yield Update', 'FUNDA AI', '2026-08-03', '2026-08-03',
                'N2 ramp ahead of schedule', 'sha-tsmc-n2-1',
                'https://funda.example/1', 'state/funda-ai/1.md',
                '2026-08-03T08:00:00Z', '2026-08-03T08:00:00Z', 'indexed'
            )
            """
        )
        conn.commit()


@pytest.mark.asyncio
async def test_sales_notes_tmtb_partition_isolation_funda_and_guidepoint_lock(
    tmp_path: Path,
) -> None:
    gp_mod, lib_mod = _load_commercial_connectors()

    store = lib_mod.MarketDigestPartitionStore(tmp_path)
    _seed_market_digest_db(store)
    _seed_funda_db(tmp_path)

    sales_conn = lib_mod.SalesNotesConnector(store)
    tmtb_conn = lib_mod.TmtbConnector(store)
    funda_conn = lib_mod.FundaAiConnector(tmp_path)

    # SQL partition invariant: sales-notes never returns alphaengine:%; tmtb only returns alphaengine:%
    sn_res = await sales_conn.call_tool(
        "search",
        {"query": "CoWoS"},
        active_caps=frozenset({CapabilityId.SALES_NOTES}),
    )
    assert sn_res["ok"] is True
    assert len(sn_res["results"]) == 1
    assert all(not r["original_document_id"].startswith("alphaengine:") for r in sn_res["results"])

    sn_no_tmtb = await sales_conn.call_tool(
        "search",
        {"query": "Hyperscaler"},
        active_caps=frozenset({CapabilityId.SALES_NOTES}),
    )
    assert sn_no_tmtb["results"] == []

    tm_res = await tmtb_conn.call_tool(
        "search",
        {"query": "Hyperscaler"},
        active_caps=frozenset({CapabilityId.TMTB}),
    )
    assert tm_res["ok"] is True
    assert len(tm_res["results"]) == 1
    assert tm_res["results"][0]["document_id"].startswith("tmtb:")

    # Ingest into sales-notes is idempotent by request_id
    code1, ing1 = await store.ingest_sales_note(
        {
            "request_id": "sn-req-1",
            "institution": "Morgan Stanley",
            "title": "MS Memory Update",
            "content": "HBM3E pricing firm through 2027",
            "date": "2026-08-10",
        }
    )
    code2, ing2 = await store.ingest_sales_note(
        {
            "request_id": "sn-req-1",
            "institution": "Morgan Stanley",
            "title": "MS Memory Update",
            "content": "HBM3E pricing firm through 2027",
            "date": "2026-08-10",
        }
    )
    assert code1 == 200 and ing1["ok"] is True
    assert code2 == 200 and ing2["idempotent_replay"] is True
    assert ing2["document_id"] == ing1["document_id"]

    # Funda-AI search & read
    fa_search = await funda_conn.call_tool(
        "search", {"query": "N2"}, active_caps=frozenset({CapabilityId.FUNDA_AI})
    )
    assert fa_search["ok"] is True and len(fa_search["results"]) == 1
    fa_read = await funda_conn.call_tool(
        "read", {"document": "funda:1"}, active_caps=frozenset({CapabilityId.FUNDA_AI})
    )
    assert fa_read["ok"] is True and "N2 ramp" in fa_read["document"]["content"]

    # Guidepoint exclusive flock & structuredContent.data preservation
    token_file = tmp_path / "gp-token.json"
    token_file.write_text(
        json.dumps(
            {
                "access_token": "gp-access-live",
                "refresh_token": "gp-refresh-1",
                "obtained_at": datetime.now(timezone.utc).timestamp(),
                "expires_in": 3600,
            }
        ),
        encoding="utf-8",
    )
    tm = gp_mod.GuidepointTokenManager(token_file=token_file)
    tm.acquire_owner_lock()
    try:
        lock_path = token_file.parent / ".guidepoint.owner.lock"
        with open(lock_path, "a+", encoding="utf-8") as fh:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        def _gp_handler(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("authorization") == "Bearer gp-access-live"
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "structuredContent": {
                            "data": [{"id": "gp-1", "transcript_name": "Cloud Expert Call", "date": "2026-08-09"}]
                        }
                    },
                },
            )

        orig_client = httpx.AsyncClient

        def _gp_client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(_gp_handler)
            return orig_client(*args, **kwargs)

        gp_conn = gp_mod.GuidepointConnector(
            token_manager=tm,
            http_client_factory=create_autospec(httpx.AsyncClient, side_effect=_gp_client_factory),
        )
        gp_out = await gp_conn.call_tool(
            "search_library",
            {"query": "NVDA cloud"},
            active_caps=frozenset({CapabilityId.GUIDEPOINT}),
        )
        assert gp_out["ok"] is True
        assert gp_out["data"]["structuredContent"]["data"][0]["id"] == "gp-1"
    finally:
        tm.release_owner_lock()


@pytest.mark.asyncio
async def test_federated_all_per_source_capabilities_partial_timeout_and_alphaengine_proxy(
    tmp_path: Path,
) -> None:
    _, lib_mod = _load_commercial_connectors()
    md_store = lib_mod.MarketDigestPartitionStore(tmp_path)
    _seed_market_digest_db(md_store)

    wiki_store = OpenWikiStore(tmp_path)
    code, _ = await wiki_store.ingest(
        {
            "request_id": "w-fed-1",
            "target_type": "company",
            "target": "NVDA",
            "content_type": "研究笔记",
            "date": "2026-08-05",
            "content": "Hyperscaler demand strong",
        }
    )
    assert code == 200

    async def _wiki_search(
        q: str, s: str | None, e: str | None, l: int, p: dict[str, Any], _c: str
    ) -> dict[str, Any]:
        return wiki_store.query("search", query=q, start=s, end=e, limit=l, params=p)

    async def _sn_search(
        q: str, s: str | None, e: str | None, l: int, p: dict[str, Any], _c: str
    ) -> dict[str, Any]:
        return md_store.query("sales-notes", "search", query=q, start=s, end=e, limit=l, params=p)

    async def _tm_search(
        q: str, s: str | None, e: str | None, l: int, p: dict[str, Any], _c: str
    ) -> dict[str, Any]:
        return md_store.query("tmtb", "search", query=q, start=s, end=e, limit=l, params=p)

    async def _sn_read(doc: str, params: dict[str, Any]) -> dict[str, Any]:
        return md_store.query("sales-notes", "read", document=doc, params=params)

    registry = ConnectorRegistry()
    registry.register_connector(CompanyWikiConnector(wiki_store))
    registry.register_federated_source("company-wiki", CapabilityId.COMPANY_WIKI, search_fn=_wiki_search)
    registry.register_federated_source(
        "sales-notes",
        CapabilityId.SALES_NOTES,
        search_fn=_sn_search,
        allowed_params=frozenset({"sender", "layer", "search_mode", "media"}),
        prefixes=("mail", "digest"),
    )
    registry.register_federated_source(
        "tmtb",
        CapabilityId.TMTB,
        search_fn=_tm_search,
        allowed_params=frozenset({"sender", "search_mode", "media"}),
        prefixes=("tmtb",),
    )
    registry.register_prefix_reader("mail", CapabilityId.SALES_NOTES, _sn_read, source_id="sales-notes")

    async def _slow_gp(
        q: str, s: str | None, e: str | None, l: int, p: dict[str, Any], _c: str
    ) -> dict[str, Any]:
        _ = (q, s, e, l, p)
        await asyncio.sleep(0.5)
        return {"ok": True, "results": []}

    registry.register_federated_source(
        "guidepoint",
        CapabilityId.GUIDEPOINT,
        search_fn=_slow_gp,
        top_level_block=True,
        fuse_local=False,
        provenance_identity="expert",
    )

    def _ae_handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://127.0.0.1:8960/mcp"
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "structuredContent": {
                        "ok": True,
                        "status": "ok",
                        "results": [{"id": "ae-99", "title": "AlphaEngine Note"}],
                    }
                },
            },
        )

    orig_async_client = httpx.AsyncClient

    def _ae_client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(_ae_handler)
        return orig_async_client(*args, **kwargs)

    runtime_dir = str(REPO_ROOT / "scripts" / "dalton" / "research_runtime")
    if runtime_dir not in sys.path:
        sys.path.insert(0, runtime_dir)
    from host_connectors.bridge_alphaengine_proxy import (
        LegacyBridgeAlphaEngineProxy,
        register_alphaengine_bridge_proxy,
    )

    ae_proxy = LegacyBridgeAlphaEngineProxy(
        bridge_mcp_url="http://127.0.0.1:8960/mcp",
        client_factory=create_autospec(httpx.AsyncClient, side_effect=_ae_client_factory),
    )
    register_alphaengine_bridge_proxy(registry, bridge_mcp_url="", proxy=ae_proxy)
    registry.register_federated_alias("sales_mail", ("sales-notes", "tmtb"))

    fed = FederatedResearchConnector(
        registry,
        wiki_store,
        fanout_grace_s=0.05,
    )

    active = frozenset(
        {
            CapabilityId.RESEARCHDATA_BRIDGE,
            CapabilityId.COMPANY_WIKI,
            CapabilityId.TMTB,
            CapabilityId.GUIDEPOINT,
            CapabilityId.ALPHAENGINE,
        }
    )

    # 1. `all.read("mail:1")` rejected when sales_notes is disabled
    denied = await fed.call_tool("read", {"document": "mail:1"}, active_caps=active)
    assert denied["ok"] is False
    assert denied["error_code"] == "capability_disabled"

    # 2. `sales_mail` with tmtb ON + sales_notes OFF queries ONLY tmtb
    sm = await fed.call_tool("sales_mail", {"query": "Hyperscaler"}, active_caps=active)
    assert sm["ok"] is True
    assert "tmtb" in sm["source_status"]
    assert "sales-notes" not in sm["source_status"]

    # 3. `all.search` returns "partial" when guidepoint exceeds grace timeout while wiki/tmtb/alphaengine succeed
    res = await fed.call_tool("search", {"query": "Hyperscaler"}, active_caps=active)
    assert res["ok"] is True
    assert res["status"] == "partial"
    assert res["source_status"]["guidepoint"]["status"] == "timeout"
    assert res["source_status"]["company-wiki"]["status"] == "ok"
    assert res["alphaengine"]["results"][0]["id"] == "ae-99"
    assert "guidepoint" in res

    # 4. `all.read("image:1")` enforces owner source capability (seeded via real research_images.ImageStore schema)
    rt_scripts = str(REPO_ROOT / "scripts" / "dalton" / "research_runtime" / "scripts")
    if rt_scripts not in sys.path:
        sys.path.insert(0, rt_scripts)
    import research_images as ri_mod

    img_store = ri_mod.ImageStore(tmp_path)
    with img_store.connect(write=True) as conn:
        conn.execute(
            "INSERT INTO assets (sha, path, preview_path, width, height, mime, bytes, created_at) "
            "VALUES ('abc', 'state/research-images/assets/a.png', 'state/research-images/assets/a.png', 100, 100, 'image/png', 10, '2026-08-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO refs (id, source, doc_key, asset_sha, title, date, metadata, created_at) "
            "VALUES (1, 'company-wiki', 'wiki:1', 'abc', 'Chart', '2026-08-01', '{}', '2026-08-01T00:00:00Z')"
        )
        conn.commit()
    img_denied = await fed.call_tool(
        "read",
        {"document": "image:1"},
        active_caps=frozenset({CapabilityId.TMTB}),
    )
    assert img_denied["ok"] is False
    assert img_denied["error_code"] == "capability_disabled"


# ---------------------------------------------------------------------------
# 5. Everflow pre-cutover parity, cutover env activation, legacy bridge cutoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_everflow_cutover_env_gate_and_legacy_bridge_single_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ptc_agent.config.plugins import bundled_mcp_servers

    # 1. Pre-cutover Everflow (DALTON_RESEARCH_HOST_URL unset): new Batch 3 servers MUST NOT appear
    _clean_profile_env(monkeypatch, profile="everflow")
    pre_servers = {s.name: s for s in bundled_mcp_servers()}
    pre_names = set(pre_servers.keys())
    assert "researchdata" in pre_names
    assert "dalton_sec" in pre_names
    assert "DALTON_SEC_EXTENDED_TOOLS" not in (pre_servers["dalton_sec"].env or {})
    for b3_srv in (
        "dalton_macro",
        "dalton_company_wiki",
        "dalton_research_all",
        "dalton_guidepoint",
        "dalton_sales_notes",
        "dalton_tmtb",
        "dalton_funda_ai",
    ):
        assert b3_srv not in pre_names

    # Pre-cutover Everflow `dalton_sec` registers ONLY its 2 baseline tools (no dual SEC authority)
    sec_pre = _load_dalton_sec_module("_test_dalton_sec_pre_cutover")
    pre_sec_tools = {t.name for t in await sec_pre.mcp.list_tools()}
    assert pre_sec_tools == {"get_company_facts", "list_filings"}

    # 2. Post-cutover Everflow (DALTON_RESEARCH_HOST_URL + DALTON_EXTRA_PLUGINS_DIR set):
    #    new Batch 3 servers appear with URL expanded from env (never hardcoded), backend injects
    #    DALTON_SEC_EXTENDED_TOOLS=1 so dalton_sec exposes 12 tools, and legacy researchdata_bridge
    #    refuses cut-over Batch 3 routes (single authoritative handler!).
    monkeypatch.setenv("DALTON_RESEARCH_HOST_URL", "http://host.docker.internal:8951")
    monkeypatch.setenv("DALTON_RESEARCH_HOST_TOKEN", "tok-ef")
    monkeypatch.setenv(
        "DALTON_EXTRA_PLUGINS_DIR",
        str(REPO_ROOT / "scripts" / "dalton" / "research_runtime" / "extra_plugins"),
    )
    post_servers = {s.name: s for s in bundled_mcp_servers()}
    assert post_servers["dalton_sec"].env.get("DALTON_SEC_EXTENDED_TOOLS") == "1"
    assert "dalton_macro" in post_servers
    assert post_servers["dalton_company_wiki"].url == "http://host.docker.internal:8951/mcp/company-wiki"
    assert post_servers["dalton_research_all"].url == "http://host.docker.internal:8951/mcp/federated"
    for extra in ("dalton_guidepoint", "dalton_sales_notes", "dalton_tmtb", "dalton_funda_ai"):
        assert extra in post_servers
        assert post_servers[extra].url.startswith("http://host.docker.internal:8951/mcp/")

    monkeypatch.setenv("DALTON_SEC_EXTENDED_TOOLS", "1")
    sec_post = _load_dalton_sec_module("_test_dalton_sec_post_cutover")
    post_sec_tools = {t.name for t in await sec_post.mcp.list_tools()}
    assert len(post_sec_tools) == 12
    assert {"get_company_facts", "list_filings", "get_financials", "list_insider_transactions"}.issubset(
        post_sec_tools
    )

    # 3. Core profile enables dalton_sec (12 tools) + dalton_macro + dalton_company_wiki + dalton_research_all by default
    _clean_profile_env(monkeypatch, profile="core")
    monkeypatch.setenv("DALTON_RESEARCH_HOST_TOKEN", "tok-core")
    core_servers = {s.name: s for s in bundled_mcp_servers()}
    assert "dalton_sec" in core_servers
    assert core_servers["dalton_sec"].env.get("DALTON_SEC_EXTENDED_TOOLS") == "1"
    assert "dalton_macro" in core_servers
    assert core_servers["dalton_company_wiki"].url == "http://research-host:8951/mcp/company-wiki"
    assert core_servers["dalton_research_all"].url == "http://research-host:8951/mcp/federated"

    # Explicitly disabling company_wiki unmounts wiki & research_all in Core
    monkeypatch.setenv("DALTON_CAP_COMPANY_WIKI", "false")
    core_no_wiki = {s.name for s in bundled_mcp_servers()}
    assert "dalton_company_wiki" not in core_no_wiki
    assert "dalton_research_all" not in core_no_wiki


@pytest.mark.asyncio
async def test_ingest_tools_pre_cutover_vs_post_cutover_routing_and_or_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Core default enables COMPANY_WIKI -> ingest_research is mounted; disabling COMPANY_WIKI removes it
    _clean_profile_env(monkeypatch, profile="core")
    assert "ingest_research" in {t.name for t in build_dalton_tools("analyst", None, user_tier="internal")}

    monkeypatch.setenv("DALTON_CAP_COMPANY_WIKI", "false")
    assert "ingest_research" not in {t.name for t in build_dalton_tools("analyst", None, user_tier="internal")}

    monkeypatch.setenv("DALTON_CAP_SALES_NOTES", "true")
    assert "ingest_research" in {t.name for t in build_dalton_tools("analyst", None, user_tier="internal")}

    # Pre-cutover Everflow -> legacy bridge /wiki/ingest & /sales-notes/ingest
    _clean_profile_env(monkeypatch, profile="everflow")
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", "bridge-admin-1")
    monkeypatch.setenv("DALTON_RESEARCH_ADMIN_TOKEN", "host-admin-2")

    seen_calls: list[tuple[str, str]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_calls.append((str(request.url), request.headers.get("authorization", "")))
        return httpx.Response(200, json={"ok": True, "doc_id": "wiki:1", "status": "ingested"})

    orig_async_client = httpx.AsyncClient

    def _mock_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(_handler)
        return orig_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client)

    code_pre_wiki, res_pre_wiki = await wiki_tools_mod.call_bridge_wiki_ingest({"request_id": "r-pre-1"})
    assert code_pre_wiki == 200 and res_pre_wiki["ok"] is True
    assert seen_calls[-1][0].endswith("/wiki/ingest")
    assert seen_calls[-1][1] == "Bearer bridge-admin-1"

    code_pre_sn, res_pre_sn = await ingest_tools_mod.call_bridge_sales_notes_ingest({"request_id": "r-pre-2"})
    assert code_pre_sn == 200 and res_pre_sn["ok"] is True
    assert seen_calls[-1][0].endswith("/sales-notes/ingest")
    assert seen_calls[-1][1] == "Bearer bridge-admin-1"

    # Post-cutover Everflow (DALTON_RESEARCH_HOST_URL set) -> research-host /ingest/* + DALTON_RESEARCH_ADMIN_TOKEN
    monkeypatch.setenv("DALTON_RESEARCH_HOST_URL", "http://host.docker.internal:8951")
    code_post_wiki, res_post_wiki = await wiki_tools_mod.call_bridge_wiki_ingest({"request_id": "r-post-1"})
    assert code_post_wiki == 200 and res_post_wiki["ok"] is True
    assert seen_calls[-1][0] == "http://host.docker.internal:8951/ingest/wiki"
    assert seen_calls[-1][1] == "Bearer host-admin-2"

    code_post_sn, res_post_sn = await ingest_tools_mod.call_bridge_sales_notes_ingest({"request_id": "r-post-2"})
    assert code_post_sn == 200 and res_post_sn["ok"] is True
    assert seen_calls[-1][0] == "http://host.docker.internal:8951/ingest/sales-notes"
    assert seen_calls[-1][1] == "Bearer host-admin-2"


def test_provenance_allows_all_batch3_servers_and_data_map_renders_core_wiki(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps(
        {
            "_sources": [
                {
                    "identifier": "doc:1",
                    "title": "Sample Source",
                    "source_type": "research_doc",
                    "identity": "internal_note",
                    "as_of": "2026-08-10",
                }
            ]
        }
    )
    for srv in (
        "dalton_sec",
        "dalton_macro",
        "dalton_company_wiki",
        "dalton_research_all",
        "dalton_guidepoint",
        "dalton_sales_notes",
        "dalton_tmtb",
        "dalton_funda_ai",
    ):
        assert srv in MCP_SERVER_CAPABILITY_MAP
        entries = expand_declared_sources(srv, "search", body, timestamp="2026-08-10T00:00:00Z")
        assert len(entries) == 1
        assert entries[0].identifier == "doc:1"

    # Non-whitelisted server returns empty list
    assert expand_declared_sources("untrusted_server", "search", body) == []

    # Core with company_wiki only renders company-wiki in static data map
    _clean_profile_env(monkeypatch, profile="core")
    monkeypatch.setenv("DALTON_CAP_COMPANY_WIKI", "true")
    md = dm_builder.render_static_data_map_markdown(team_memory_enabled=False)
    assert "company-wiki" in md


# ---------------------------------------------------------------------------
# 6. Separation fix (Item 5): bridge_subprocess_env uses src.dalton.profile.resolve_profile
# ---------------------------------------------------------------------------


def test_bridge_subprocess_env_uses_resolve_profile_and_reports_conflicts() -> None:
    scripts_dir = str(REPO_ROOT / "scripts" / "dalton")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import bridge_subprocess_env as bse

    # Core via APP_ENV=core defaults to openai
    issues_core = bse._validate_direct_embedding_env({"APP_ENV": "core", "OPENAI_API_KEY": "sk-test"})
    assert issues_core == []

    # Core via DALTON_PROFILE=core defaults to openai
    issues_core_prof = bse._validate_direct_embedding_env({"DALTON_PROFILE": "core", "OPENAI_API_KEY": "sk-test"})
    assert issues_core_prof == []

    # Everflow defaults to gemini
    issues_ef = bse._validate_direct_embedding_env({"APP_ENV": "dalton", "GEMINI_API_KEY": "gem-test"})
    assert issues_ef == []

    # Conflicting APP_ENV=dalton + DALTON_PROFILE=core reports clear config issue instead of crashing
    issues_conflict = bse._validate_direct_embedding_env(
        {"APP_ENV": "dalton", "DALTON_PROFILE": "core", "OPENAI_API_KEY": "sk-test"}
    )
    assert any("invalid deployment profile configuration" in msg for msg in issues_conflict)
    assert any("Conflicting profile" in msg for msg in issues_conflict)

    # Invalid DALTON_PROFILE value reports clear config issue
    issues_invalid = bse._validate_direct_embedding_env(
        {"DALTON_PROFILE": "bogus", "OPENAI_API_KEY": "sk-test"}
    )
    assert any("invalid deployment profile configuration" in msg for msg in issues_invalid)


# ---------------------------------------------------------------------------
# 7. Packaging, docker-compose.core.yml, transport, workflow.js, host scripts, switch script
# ---------------------------------------------------------------------------


def test_package_release_allowlist_and_docker_compose_core_research_host() -> None:
    pkg_text = (REPO_ROOT / "scripts" / "standalone" / "package_release.sh").read_text(encoding="utf-8")
    for expected in (
        '"plugins/dalton_sec"',
        '"plugins/dalton_macro"',
        '"plugins/dalton_company_wiki"',
        '"plugins/dalton_research_all"',
    ):
        assert expected in pkg_text
    for forbidden in (
        "extra_plugins",
        "host_connectors",
        "dalton-guidepoint",
        "dalton-sales-notes",
        "dalton-tmtb",
        "dalton-funda-ai",
    ):
        assert forbidden not in pkg_text

    public_plugin_names = {
        json.loads((p / "plugin.json").read_text(encoding="utf-8"))["name"]
        for p in (REPO_ROOT / "plugins").iterdir()
        if p.is_dir() and (p / "plugin.json").is_file()
    }
    assert EXTRA_ONLY_BUNDLED_PLUGINS.isdisjoint(public_plugin_names)
    for pub in ("dalton-sec", "dalton-macro", "dalton-company-wiki", "dalton-research-all"):
        assert pub in public_plugin_names
        assert pub in BUNDLED_PLUGIN_CAPABILITY_MAP

    compose_raw = (REPO_ROOT / "docker-compose.core.yml").read_text(encoding="utf-8")
    cleaned = compose_raw.replace("!reset ", "").replace("!override", "")
    doc = yaml.safe_load(cleaned)
    rh = doc["services"]["research-host"]
    assert "profiles" not in rh
    assert rh["entrypoint"] == ["/usr/bin/tini", "-s", "--"]
    assert "src.dalton.research_host" in rh["command"]
    assert set(rh["networks"]) == {"core_internal", "sandbox-net"}
    assert "dalton-wiki-data" in doc["volumes"]
    assert "TOKEN" not in json.dumps(rh.get("environment") or {})

    # Core init.sh & .env.core.example enable company_wiki and generate distinct research-host tokens
    init_text = (REPO_ROOT / "scripts" / "standalone" / "init.sh").read_text(encoding="utf-8")
    for expected_key in (
        '"DALTON_CAP_COMPANY_WIKI"',
        '"DALTON_RESEARCH_HOST_URL"',
        '"DALTON_RESEARCH_HOST_TOKEN"',
        '"DALTON_RESEARCH_ADMIN_TOKEN"',
    ):
        assert expected_key in init_text


def test_connector_transport_workflow_js_and_host_scripts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ct_mod = _load_module_from_path(
        "_test_connector_transport_b3",
        REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-conventions" / "scripts" / "connector_transport.py",
    )
    cfg_file = tmp_path / "mcp_client_config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "servers": {
                    "dalton_sec": {
                        "enabled": True,
                        "tools": [
                            {
                                "name": "list_filings",
                                "inputSchema": {"properties": {"ticker": {}, "form": {}, "forms": {}, "limit": {}, "as_of": {}}},
                            }
                        ],
                    },
                    "dalton_company_wiki": {
                        "enabled": True,
                        "tools": [{"name": "search", "inputSchema": {"properties": {"query": {}, "params": {}}}}],
                    },
                    "researchdata": {"enabled": True},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("DALTON_RESEARCHDATA_URL", raising=False)
    monkeypatch.setenv("PTC_WORKSPACE_CONFIG", str(cfg_file))

    def _fake_mcp_call(server: str, tool: str, args: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        return (False, {"ok": True, "server": server, "tool": tool, "args": args})

    fake_mcp_mod = type(sys)("mcp_client")
    fake_mcp_mod.call_mcp_tool_with_error_flag = _fake_mcp_call  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mcp_client", fake_mcp_mod)

    sec_res = ct_mod.call_sec_operation("list-filings", {"ticker": "AAPL", "form": "8-K"})
    assert sec_res["ok"] is True
    assert sec_res["data"]["server"] == "dalton_sec"
    assert sec_res["data"]["tool"] == "list_filings"
    assert sec_res["data"]["args"]["form"] == "8-K"

    wiki_res = ct_mod.call_research_route("company-wiki", "search", {"query": "NVDA"})
    assert wiki_res["ok"] is True
    assert wiki_res["data"]["server"] == "dalton_company_wiki"

    from ptc_agent.agent.middleware.background_subagent.workflow.engine import compile_check

    wf_path = REPO_ROOT / "workflows" / "topic-research" / "workflow.js"
    assembled_core = read_assembled_text(wf_path, capabilities=frozenset())
    assert "internal-memory.md" not in assembled_core
    compile_check(assembled_core)

    assembled_wiki = read_assembled_text(wf_path, capabilities=frozenset({CapabilityId.COMPANY_WIKI}))
    assert "internal-memory.md" in assembled_wiki
    assert "researchdata" not in assembled_wiki
    compile_check(assembled_wiki)

    monkeypatch.setenv("DALTON_GUIDEPOINT_MCP_URL", "http://127.0.0.1:8951/mcp/guidepoint")
    gp_cli = _load_module_from_path(
        "_test_gp_search_b3",
        REPO_ROOT / "scripts" / "dalton" / "research_runtime" / "skills" / "guidepoint-transcript-search" / "scripts" / "gp_search.py",
    )
    assert gp_cli._resolve_base_url() == "http://127.0.0.1:8951/mcp/guidepoint"
    dg_mod = _load_module_from_path(
        "_test_data_gateway_b3",
        REPO_ROOT / "scripts" / "dalton" / "research_runtime" / "scripts" / "data_gateway.py",
    )
    assert dg_mod.NATIVE_SOURCES["guidepoint"] == "http://127.0.0.1:8951/mcp/guidepoint"


# ---------------------------------------------------------------------------
# 8. r4 Review Fixes: SEC fail-closed subprocess, real schemas, DB-first ingest,
#    input/token checks, 2-leg partial timeout, mixed callers, & switch script tests
# ---------------------------------------------------------------------------


def test_dalton_sec_subprocess_fail_closed_without_src_on_pythonpath(tmp_path: Path) -> None:
    """Decision #3: dalton_sec launched WITHOUT src on PYTHONPATH exposes ONLY 2 baseline tools & schemas."""
    sec_script = REPO_ROOT / "plugins" / "dalton_sec" / "dalton_sec_mcp_server.py"
    # Stage ONLY mcp_servers in an isolated directory (no src/ package reachable!)
    isolated_root = tmp_path / "sandbox_py"
    isolated_root.mkdir()
    (isolated_root / "mcp_servers").symlink_to(REPO_ROOT / "mcp_servers")

    probe_code = (
        "import asyncio, importlib.util, json, sys\n"
        "try:\n"
        "    import src.dalton\n"
        "    raise SystemExit('ERROR: src.dalton must not be importable in isolated sandbox test')\n"
        "except ModuleNotFoundError:\n"
        "    pass\n"
        f"spec = importlib.util.spec_from_file_location('sec_sub', {str(sec_script)!r})\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "tools = asyncio.run(mod.mcp.list_tools())\n"
        "out = {\n"
        "    t.name: {\n"
        "        'inputSchema': getattr(t, 'inputSchema', None) or getattr(t, 'input_schema', None),\n"
        "        'outputSchema': getattr(t, 'outputSchema', None) or getattr(t, 'output_schema', None),\n"
        "    }\n"
        "    for t in tools\n"
        "}\n"
        "print(json.dumps(out, sort_keys=True))\n"
    )
    base_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(isolated_root),
    }
    proc_off = subprocess.run(
        [sys.executable, "-c", probe_code],
        env=base_env,
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_off.returncode == 0, proc_off.stderr
    tools_off = json.loads(proc_off.stdout.strip())
    assert set(tools_off.keys()) == {"get_company_facts", "list_filings"}
    lf_props = sorted((tools_off["list_filings"]["inputSchema"].get("properties") or {}).keys())
    assert lf_props == ["as_of", "forms", "limit", "ticker"]
    assert "form" not in lf_props
    assert tools_off["list_filings"]["inputSchema"].get("required") == ["ticker"]
    assert tools_off["list_filings"]["outputSchema"] is not None
    assert tools_off["get_company_facts"]["outputSchema"] is not None

    env_on = {**base_env, "DALTON_SEC_EXTENDED_TOOLS": "1"}
    proc_on = subprocess.run(
        [sys.executable, "-c", probe_code],
        env=env_on,
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_on.returncode == 0, proc_on.stderr
    tools_on = json.loads(proc_on.stdout.strip())
    assert len(tools_on) == 12
    assert "form" in (tools_on["list_filings"]["inputSchema"].get("properties") or {})


@pytest.mark.asyncio
async def test_wiki_ingest_real_company_wiki_cli_schema_db_first_and_no_overwrite(
    tmp_path: Path,
) -> None:
    """Decisions #1 & #2: wiki ingest matches company_wiki_cli.init_db schema, DB-first rollback, no file overwrite."""
    cli_path = (
        REPO_ROOT
        / "scripts"
        / "dalton"
        / "research_runtime"
        / "skills"
        / "company-wiki"
        / "scripts"
        / "company_wiki_cli.py"
    )
    cli_mod = _load_module_from_path("_test_company_wiki_cli_r4", cli_path)

    # Seed vectors.db using real company_wiki_cli.init_db() (has content_type NOT NULL + chunks + chunks_fts triggers)
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    cli_mod.WIKI_DIR = wiki_dir
    cli_mod.DB_PATH = wiki_dir / "vectors.db"
    conn = cli_mod.init_db()
    conn.close()

    store = OpenWikiStore(tmp_path)

    # 1. Ingest into legacy schema succeeds & populates content_type + chunks + chunks_fts
    code, res = await store.ingest(
        {
            "request_id": "cli-schema-1",
            "target_type": "company",
            "target": "NVDA",
            "content_type": "研报笔记",
            "date": "2026-08-12",
            "file": {
                "filename": "note.md",
                "content_b64": base64.b64encode(b"Original Blackwell supply note").decode("ascii"),
            },
        }
    )
    assert code == 200 and res["ok"] is True
    first_path = tmp_path / res["filepath"]
    assert first_path.is_file()
    first_bytes = first_path.read_bytes()

    with sqlite3.connect(store.wiki_db) as check_conn:
        doc_row = check_conn.execute(
            "SELECT content_type, filepath FROM documents WHERE id = 1"
        ).fetchone()
        assert doc_row == ("研报笔记", res["filepath"])
        chunk_cnt = check_conn.execute("SELECT COUNT(*) FROM chunks WHERE doc_id = 1").fetchone()[0]
        assert chunk_cnt == 1

    # 2. Same filename with DIFFERENT content must NOT overwrite original file
    code2, res2 = await store.ingest(
        {
            "request_id": "cli-schema-2",
            "target_type": "company",
            "target": "NVDA",
            "content_type": "研报笔记",
            "date": "2026-08-12",
            "file": {
                "filename": "note.md",
                "content_b64": base64.b64encode(b"Updated Rubin architecture note").decode("ascii"),
            },
        }
    )
    assert code2 == 200 and res2["ok"] is True
    assert res2["filepath"] != res["filepath"]
    assert first_path.read_bytes() == first_bytes
    assert (tmp_path / res2["filepath"]).is_file()

    # 3. DB rejection leaves disk completely untouched (DB-first atomic ingest)
    with sqlite3.connect(store.wiki_db) as trig_conn:
        trig_conn.execute(
            "CREATE TRIGGER reject_fail_insert BEFORE INSERT ON documents "
            "WHEN NEW.category_name = 'FAILCO' "
            "BEGIN SELECT RAISE(ABORT, 'simulated db rejection'); END;"
        )
        trig_conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="simulated db rejection"):
        await store.ingest(
            {
                "request_id": "cli-schema-fail",
                "target_type": "company",
                "target": "FAILCO",
                "content_type": "研报笔记",
                "date": "2026-08-12",
                "content": "Should never hit disk on DB error",
            }
        )
    assert not (tmp_path / "wiki" / "companies" / "FAILCO").exists()


@pytest.mark.asyncio
async def test_sales_notes_validation_path_safety_and_db_first_rollback(tmp_path: Path) -> None:
    """Decisions #2 & #9: sales-notes rejects invalid calendar dates, path escapes, and rolls back on DB error."""
    _, lib_mod = _load_commercial_connectors()
    store = lib_mod.MarketDigestPartitionStore(tmp_path)

    for bad_date in ("2026-02-30", "../../etc/passwd", "not-a-date"):
        code, res = await store.ingest_sales_note(
            {
                "institution": "GS",
                "title": "Bad Date Check",
                "content": "Body",
                "date": bad_date,
            }
        )
        assert code == 400
        assert res["error_code"] == "invalid_date"

    # DB rejection leaves sales-notes directory untouched
    store.ensure_schema()
    with sqlite3.connect(store.mail_db) as conn:
        conn.execute(
            "CREATE TRIGGER reject_sales_insert BEFORE INSERT ON email_chunks "
            "WHEN NEW.sender = 'FAILBANK' "
            "BEGIN SELECT RAISE(ABORT, 'simulated sales db error'); END;"
        )
        conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="simulated sales db error"):
        await store.ingest_sales_note(
            {
                "institution": "FAILBANK",
                "title": "Rollback Note",
                "content": "Must not remain on disk",
                "date": "2026-08-15",
            }
        )
    md_files = list(store.notes_dir.glob("*.md")) if store.notes_dir.exists() else []
    assert md_files == []


@pytest.mark.asyncio
async def test_research_host_distinct_tokens_in_flight_and_non_ascii_caller(tmp_path: Path) -> None:
    """Decisions #5 & #9: distinct tokens enforced at startup, in_flight reported, non-ASCII caller handled safely."""
    from src.dalton.research_host.auth import verify_caller_token

    with pytest.raises(ValueError, match="distinct"):
        ResearchHostRuntime(
            ResearchHostConfig(
                data_root=tmp_path,
                mcp_token="same-secret",
                admin_token="same-secret",
                profile=DeploymentProfile.CORE,
            )
        )

    # Non-ASCII malformed caller token must return anonymous without raising UnicodeEncodeError
    ident = verify_caller_token("非ASCII载荷.c2ln", admin_secrets=("admin-secret",))
    assert ident.verified is False
    assert ident.caller_key == "anonymous"

    # /admin/drain exposes real integer in_flight
    def _core_wiki() -> frozenset[CapabilityId]:
        return frozenset({CapabilityId.COMPANY_WIKI})

    app = create_app(
        ResearchHostConfig(
            data_root=tmp_path / "rh",
            mcp_token="mcp-tok-1",
            admin_token="adm-tok-2",
            profile=DeploymentProfile.CORE,
        ),
        capabilities_provider=_core_wiki,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:8951",
    ) as client:
        r_get = await client.get("/admin/drain", headers={"Authorization": "Bearer adm-tok-2"})
        assert r_get.status_code == 200
        assert r_get.json()["in_flight"] == 0
        r_post = await client.post(
            "/admin/drain",
            headers={"Authorization": "Bearer adm-tok-2"},
            json={"drain": True},
        )
        assert r_post.status_code == 200
        assert r_post.json()["draining"] is True
        assert r_post.json()["in_flight"] == 0


@pytest.mark.asyncio
async def test_guidepoint_shared_owner_lock_and_caller_propagation(tmp_path: Path) -> None:
    """Decisions #4 & #9: shared flock blocks concurrent owners; caller_key is propagated."""
    gp_mod, _ = _load_commercial_connectors()
    token_file = tmp_path / "config" / "guidepoint_mcp_tokens.json"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(
        json.dumps(
            {
                "access_token": "gp-access-1",
                "refresh_token": "gp-refresh-1",
                "obtained_at": 9999999999.0,
                "expires_in": 3600,
            }
        ),
        encoding="utf-8",
    )

    # Legacy proxy owner lock blocks host_connectors GuidepointTokenManager
    legacy_lock = gp_mod.GuidepointOwnerLock(token_file, service_name="guidepoint-mcp-proxy")
    legacy_lock.acquire(timeout_s=0.1)
    try:
        mgr = gp_mod.GuidepointTokenManager(token_file)
        with pytest.raises(gp_mod.GuidepointTokenLockBusyError):
            mgr.acquire_owner_lock(timeout_s=0.05)
    finally:
        legacy_lock.release()

    # Once legacy releases, host_connectors acquires cleanly and propagates caller_key
    mgr = gp_mod.GuidepointTokenManager(token_file)
    mgr.acquire_owner_lock(timeout_s=0.2)
    seen_headers: list[dict[str, str]] = []

    def _gp_handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "structuredContent": {
                        "data": [{"transcript_id": "t1", "transcript_name": "NVDA Call", "date": "2026-08-10"}]
                    }
                },
            },
        )

    orig_client = httpx.AsyncClient

    def _gp_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(_gp_handler)
        return orig_client(*args, **kwargs)

    try:
        conn = gp_mod.GuidepointConnector(
            mgr,
            http_client_factory=create_autospec(httpx.AsyncClient, side_effect=_gp_client),
        )
        res = await conn.call_tool(
            "search_library",
            {"query": "NVDA"},
            active_caps=frozenset({CapabilityId.GUIDEPOINT}),
            caller_key="user:bob",
        )
        assert res["ok"] is True
        assert res["caller_key"] == "user:bob"
        assert seen_headers[0]["x-dalton-caller-key"] == "user:bob"
    finally:
        mgr.release_owner_lock()


@pytest.mark.asyncio
async def test_federated_all_two_leg_partial_timeout_and_sources_filter_stripping(tmp_path: Path) -> None:
    """Decision #8: 2-leg 1-ok/1-timeout returns partial within grace; explicit sources=['company-wiki'] works for search & read."""
    wiki_store = OpenWikiStore(tmp_path)
    code, ing = await wiki_store.ingest(
        {
            "request_id": "fed-r4-1",
            "target_type": "company",
            "target": "AAPL",
            "content_type": "研报笔记",
            "date": "2026-08-10",
            "content": "Apple services margin expanded in Q3",
        }
    )
    assert code == 200
    doc_id = ing["doc_id"]

    async def _wiki_search(
        q: str, s: str | None, e: str | None, l: int, p: dict[str, Any], _c: str
    ) -> dict[str, Any]:
        return wiki_store.query("search", query=q, start=s, end=e, limit=l, params=p)

    async def _wiki_read(doc: str, params: dict[str, Any]) -> dict[str, Any]:
        return wiki_store.query("read", document=doc, params=params)

    async def _hung_leg(
        q: str, s: str | None, e: str | None, l: int, p: dict[str, Any], _c: str
    ) -> dict[str, Any]:
        _ = (q, s, e, l, p)
        await asyncio.sleep(5.0)
        return {"ok": True, "results": []}

    registry = ConnectorRegistry()
    registry.register_connector(CompanyWikiConnector(wiki_store))
    registry.register_federated_source("company-wiki", CapabilityId.COMPANY_WIKI, search_fn=_wiki_search)
    registry.register_federated_source("tmtb", CapabilityId.TMTB, search_fn=_hung_leg)
    registry.register_prefix_reader("wiki", CapabilityId.COMPANY_WIKI, _wiki_read)

    fed = FederatedResearchConnector(registry, wiki_store, fanout_grace_s=0.05)
    active = frozenset({CapabilityId.COMPANY_WIKI, CapabilityId.TMTB})

    # 1. Two-leg fanout: 1 succeeds fast + 1 hangs -> returns partial within grace window (no hang!)
    res_partial = await fed.call_tool("search", {"query": "Apple"}, active_caps=active)
    assert res_partial["ok"] is True
    assert res_partial["status"] == "partial"
    assert res_partial["source_status"]["company-wiki"]["status"] == "ok"
    assert res_partial["source_status"]["tmtb"]["status"] == "timeout"

    # 2. Explicit sources=['company-wiki'] search strips federated-only `sources` before hitting OpenWikiStore
    res_wiki_only = await fed.call_tool(
        "search",
        {"query": "Apple", "params": {"sources": ["company-wiki"]}},
        active_caps=active,
    )
    assert res_wiki_only["ok"] is True
    assert res_wiki_only["status"] == "ok"
    assert len(res_wiki_only["results"]) == 1

    # 3. `all.read("wiki:1", params={"sources": ["company-wiki"]})` strips `sources` and succeeds
    res_read = await fed.call_tool(
        "read",
        {"document": doc_id, "params": {"sources": ["company-wiki"]}},
        active_caps=active,
    )
    assert res_read["ok"] is True
    assert "Apple services margin" in res_read["document"]["content"]


def test_callers_mixed_mcp_config_gp_search_new_shape_and_data_map_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision #7: mixed MCP config routes per source; gp_search parses new shape; data_map/health route per source."""
    from src.dalton.data_map import health as dm_health

    cfg_file = tmp_path / "mcp_client_config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "servers": {
                    "dalton_company_wiki": {
                        "enabled": True,
                        "tools": [{"name": "search", "inputSchema": {"properties": {"query": {}, "params": {}}}}],
                    },
                    "dalton_sec": {"enabled": True},
                    "researchdata": {"enabled": True},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("DALTON_RESEARCHDATA_URL", raising=False)
    monkeypatch.setenv("PTC_WORKSPACE_CONFIG", str(cfg_file))

    calls: list[tuple[str, str, dict[str, Any]]] = []

    def _record_call(server: str, tool: str, args: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        calls.append((server, tool, dict(args)))
        return (False, {"ok": True, "status": "ok", "results": []})

    fake_mcp_mod = type(sys)("mcp_client")
    fake_mcp_mod.call_mcp_tool_with_error_flag = _record_call  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mcp_client", fake_mcp_mod)

    fake_tools_pkg = type(sys)("tools")
    fake_rd_wrapper = type(sys)("tools.researchdata")
    fake_rd_wrapper._call_mcp_tool = _record_call  # type: ignore[attr-defined]
    fake_tools_pkg.researchdata = fake_rd_wrapper  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tools", fake_tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.researchdata", fake_rd_wrapper)

    # collect.py routes company-wiki -> dalton_company_wiki, alphaengine -> researchdata
    collect_mod = _load_module_from_path(
        "_test_collect_mixed_r4",
        REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-thesis-delta" / "scripts" / "collect.py",
    )
    assert collect_mod.call_bridge_jsonrpc("research", {"route": "company-wiki", "operation": "search", "query": "NVDA"})["ok"] is True
    assert calls[-1][0] == "dalton_company_wiki"
    assert collect_mod.call_bridge_jsonrpc("research", {"route": "alphaengine", "operation": "search_library", "query": "NVDA"})["ok"] is True
    assert calls[-1][0] == "researchdata"

    # rd_transport.py routes company-wiki -> dalton_company_wiki, alphaengine -> researchdata
    rd_mod = _load_module_from_path(
        "_test_rd_transport_mixed_r4",
        REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-mark-weekly-newenergy" / "scripts" / "rd_transport.py",
    )
    assert rd_mod.call_bridge_jsonrpc("research", {"route": "company-wiki", "operation": "search", "query": "TSLA"})["ok"] is True
    assert calls[-1][0] == "dalton_company_wiki"
    assert rd_mod.call_bridge_jsonrpc("research", {"route": "alphaengine", "operation": "search_library", "query": "TSLA"})["ok"] is True
    assert calls[-1][0] == "researchdata"

    # gp_search.py parses new research-host nested `data.structuredContent.data` shape
    gp_cli = _load_module_from_path(
        "_test_gp_search_shape_r4",
        REPO_ROOT / "scripts" / "dalton" / "research_runtime" / "skills" / "guidepoint-transcript-search" / "scripts" / "gp_search.py",
    )
    new_host_result = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "ok": True,
                        "data": {
                            "structuredContent": {
                                "data": [{"transcript_id": "gp-1", "transcript_name": "AI Capex"}]
                            }
                        },
                    }
                ),
            }
        ]
    }
    extracted = gp_cli._extract_transcripts(new_host_result)
    assert len(extracted) == 1 and extracted[0]["transcript_id"] == "gp-1"

    # data_map/health.py & builder.py route per source when Batch 3 is enabled vs pre-cutover
    _clean_profile_env(monkeypatch, profile="everflow")
    assert dm_health.resolve_route_probe_target("company-wiki") == "bridge"
    assert dm_health.resolve_route_probe_target("fred") == "bridge"
    monkeypatch.setenv("DALTON_RESEARCH_HOST_URL", "http://host.docker.internal:8951")
    assert dm_health.resolve_route_probe_target("company-wiki") == "research_host"
    assert dm_health.resolve_route_probe_target("fred") == "dalton_macro"
    assert dm_health.resolve_route_probe_target("sec") == "dalton_sec"
    assert dm_health.resolve_route_probe_target("alphaengine") == "bridge"
    md_post = dm_builder.render_static_data_map_markdown(team_memory_enabled=False)
    assert "`dalton_company_wiki`" in md_post
    assert "`researchdata`" in md_post


# ---------------------------------------------------------------------------
# 9. r5 Review Fixes (Batch 3a): tool-level routing, real binary ingest &
#    atomic receipts, shared capability env, dynamic federated tools,
#    non-blocking SQLite, SEC alignment, Core data map probing & cache key
# ---------------------------------------------------------------------------


def test_pre_cutover_everflow_mcp_config_preserves_legacy_calls_and_nested_params(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision #2: pre-cutover Everflow (researchdata + 2-tool dalton_sec) routes SEC & research unchanged;
    dedicated callers preserve nested params, sales_mail.operation, and default form='10-Q'."""
    ct_mod = _load_module_from_path(
        "_test_ct_r5",
        REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-conventions" / "scripts" / "connector_transport.py",
    )
    collect_mod = _load_module_from_path(
        "_test_collect_r5",
        REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-thesis-delta" / "scripts" / "collect.py",
    )
    rd_mod = _load_module_from_path(
        "_test_rd_r5",
        REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-mark-weekly-newenergy" / "scripts" / "rd_transport.py",
    )

    # 1. Real pre-cutover Everflow MCP config: researchdata + baseline 2-tool dalton_sec (NO `form` param!)
    cfg_pre = tmp_path / "pre_cutover_mcp.json"
    cfg_pre.write_text(
        json.dumps({"servers": {"researchdata": {"enabled": True}, "dalton_sec": {"enabled": True}}}),
        encoding="utf-8",
    )
    monkeypatch.delenv("DALTON_RESEARCHDATA_URL", raising=False)
    monkeypatch.delenv("DALTON_SEC_EXTENDED_TOOLS", raising=False)
    monkeypatch.setenv("PTC_WORKSPACE_CONFIG", str(cfg_pre))

    recorded: list[tuple[str, str, dict[str, Any]]] = []

    def _record(server: str, tool: str, args: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        recorded.append((server, tool, dict(args)))
        return (False, {"ok": True, "status": "ok", "results": []})

    fake_mcp = type(sys)("mcp_client")
    fake_mcp.call_mcp_tool_with_error_flag = _record  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mcp_client", fake_mcp)

    fake_tools = type(sys)("tools")
    fake_rd = type(sys)("tools.researchdata")
    fake_rd._call_mcp_tool = _record  # type: ignore[attr-defined]
    fake_sec_baseline = type(sys)("tools.dalton_sec")

    def _baseline_list_filings(ticker: str, forms: str = "10-K,10-Q,20-F,6-K", limit: int = 10, as_of: str | None = None) -> dict[str, Any]:
        return {"ticker": ticker, "forms": forms, "limit": limit, "as_of": as_of}

    fake_sec_baseline.list_filings = _baseline_list_filings  # type: ignore[attr-defined]
    fake_tools.researchdata = fake_rd  # type: ignore[attr-defined]
    fake_tools.dalton_sec = fake_sec_baseline  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tools", fake_tools)
    monkeypatch.setitem(sys.modules, "tools.researchdata", fake_rd)
    monkeypatch.setitem(sys.modules, "tools.dalton_sec", fake_sec_baseline)

    # Every caller must route SEC list-filings(form=...) & financials to legacy `researchdata` unchanged!
    sec_args = {"route": "sec", "operation": "list-filings", "ticker": "NVDA", "form": "8-K", "limit": 5}
    assert collect_mod.call_bridge_jsonrpc("finance", sec_args)["ok"] is True
    assert recorded[-1] == ("researchdata", "finance", sec_args)

    fin_args = {"route": "sec", "operation": "financials", "ticker": "NVDA", "form": "10-Q"}
    assert rd_mod.call_bridge_jsonrpc("finance", fin_args)["ok"] is True
    assert recorded[-1] == ("researchdata", "finance", fin_args)

    ct_res = ct_mod.call_sec_operation("list-filings", {"ticker": "NVDA", "form": "8-K"})
    assert ct_res["ok"] is True
    assert recorded[-1] == ("researchdata", "finance", {"route": "sec", "operation": "list-filings", "ticker": "NVDA", "form": "8-K"})

    # 2. Post-cutover config with discovered tools on dalton_sec + dedicated research connectors
    cfg_post = tmp_path / "post_cutover_mcp.json"
    cfg_post.write_text(
        json.dumps(
            {
                "servers": {
                    "dalton_sec": {
                        "enabled": True,
                        "tools": [
                            {
                                "name": "list_filings",
                                "inputSchema": {"properties": {"ticker": {}, "form": {}, "forms": {}, "limit": {}, "as_of": {}}},
                            }
                        ],
                    },
                    "dalton_sales_notes": {
                        "enabled": True,
                        "tools": [
                            {
                                "name": "search",
                                "inputSchema": {"properties": {"query": {}, "limit": {}, "params": {}}},
                            }
                        ],
                    },
                    "dalton_research_all": {
                        "enabled": True,
                        "tools": [
                            {
                                "name": "sales_mail",
                                "inputSchema": {"properties": {"operation": {}, "document": {}, "params": {}}},
                            }
                        ],
                    },
                    "dalton_company_wiki": {"enabled": True},
                    "researchdata": {"enabled": True},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PTC_WORKSPACE_CONFIG", str(cfg_post))

    # Unverified server (no wrapper, no `tools` manifest) alongside `researchdata` falls back to `researchdata`!
    assert ct_mod.call_research_route("company-wiki", "search", {"query": "NVDA"})["ok"] is True
    assert recorded[-1][0] == "researchdata"

    # list-filings with omitted `form` defaults to "10-Q"
    assert ct_mod.call_sec_operation("list-filings", {"ticker": "MSFT", "limit": 3})["ok"] is True
    assert recorded[-1] == ("dalton_sec", "list_filings", {"ticker": "MSFT", "limit": 3, "form": "10-Q"})

    # sales-notes preserves nested `params` dict instead of flattening it
    assert ct_mod.call_research_route(
        "sales-notes",
        "search",
        {"query": "GPU", "limit": 5, "params": {"sender": "Morgan Stanley", "layer": "digest"}},
    )["ok"] is True
    assert recorded[-1] == (
        "dalton_sales_notes",
        "search",
        {"query": "GPU", "limit": 5, "params": {"sender": "Morgan Stanley", "layer": "digest"}},
    )

    # sales-mail preserves `operation: "read"` when calling `dalton_research_all.sales_mail`
    assert ct_mod.call_research_route(
        "sales-mail",
        "read",
        {"document": "mail:42", "params": {"max_chars": 2000}},
    )["ok"] is True
    assert recorded[-1] == (
        "dalton_research_all",
        "sales_mail",
        {"operation": "read", "document": "mail:42", "params": {"max_chars": 2000}},
    )

    # workflow.js keeps exact Everflow legacy wording pre-cutover, switches when Batch 3 connectors active
    wf_path = REPO_ROOT / "workflows" / "topic-research" / "workflow.js"
    _clean_profile_env(monkeypatch, profile="everflow")
    pre_wf = read_assembled_text(wf_path)
    assert "检索 researchdata 的 sales-notes、tmtb、funda-ai、company-wiki（只读）" in pre_wf
    monkeypatch.setenv("DALTON_RESEARCH_HOST_URL", "http://host.docker.internal:8951")
    post_wf = read_assembled_text(wf_path)
    assert "检索已启用的内部研究知识库连接器（只读）" in post_wf
    assert "检索 researchdata 的 sales-notes" not in post_wf


def _make_minimal_pdf_bytes(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 10 10 Td ({text}) Tj ET".encode("latin-1")
    return (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >> endobj\n"
        + f"4 0 obj << /Length {len(stream)} >>\nstream\n".encode("ascii")
        + stream
        + b"\nendstream\nendobj\n%%EOF\n"
    )


def _make_minimal_docx_bytes(text: str) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "word/document.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
            ),
        )
    return buf.getvalue()


def _make_minimal_png_bytes() -> bytes:
    import struct
    import zlib

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", 2, 3, 8, 2, 0, 0, 0)
    raw_scanlines = b"".join(b"\x00" + b"\xff\x00\x00" * 2 for _ in range(3))
    idat = zlib.compress(raw_scanlines)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


@pytest.mark.asyncio
async def test_binary_attachments_pdf_docx_png_and_atomic_audit_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision #3: PDF/DOCX/PNG attachments preserve originals, index extracted text, and recover cleanly from audit/manifest failures."""
    pdf_bytes = _make_minimal_pdf_bytes("Blackwell Revenue Guidance 42B")
    docx_bytes = _make_minimal_docx_bytes("Rubin Supply Chain Memo")
    png_bytes = _make_minimal_png_bytes()

    # 1. OpenWikiStore: PDF, DOCX, PNG
    wiki = OpenWikiStore(tmp_path / "wiki_root")
    for req_id, fname, payload, expected_text in (
        ("bin-pdf-1", "blackwell.pdf", pdf_bytes, "Blackwell Revenue Guidance 42B"),
        ("bin-docx-1", "rubin.docx", docx_bytes, "Rubin Supply Chain Memo"),
        ("bin-png-1", "chart.png", png_bytes, "2x3"),
    ):
        code, res = await wiki.ingest(
            {
                "request_id": req_id,
                "target_type": "company",
                "target": "NVDA",
                "content_type": "研报笔记",
                "date": "2026-08-15",
                "file": {
                    "filename": fname,
                    "content_b64": base64.b64encode(payload).decode("ascii"),
                },
            }
        )
        assert code == 200 and res["ok"] is True
        orig_path = (tmp_path / "wiki_root") / res["original_filepath"]
        md_path = (tmp_path / "wiki_root") / res["filepath"]
        assert orig_path.read_bytes() == payload
        md_text = md_path.read_text(encoding="utf-8")
        assert expected_text in md_text
        assert "%PDF-1.4" not in md_text and "IHDR" not in md_text

    # Search hits extracted PDF & DOCX text
    s_pdf = wiki.query("search", query="Blackwell")
    assert s_pdf["ok"] is True and s_pdf["result_count"] == 1
    s_docx = wiki.query("search", query="Rubin")
    assert s_docx["ok"] is True and s_docx["result_count"] == 1

    # Audit append failure on first attempt -> retry with same request_id recovers with zero duplicate rows!
    orig_append = wiki._append_audit
    fail_once = {"flag": True}

    def _flaky_audit(entry: dict[str, Any]) -> None:
        if fail_once["flag"]:
            fail_once["flag"] = False
            raise OSError("simulated disk full on audit append")
        orig_append(entry)

    monkeypatch.setattr(wiki, "_append_audit", _flaky_audit)
    retry_body = {
        "request_id": "wiki-audit-retry-1",
        "target_type": "company",
        "target": "MSFT",
        "content_type": "研报笔记",
        "date": "2026-08-16",
        "content": "Azure AI capacity expansion note",
    }
    with pytest.raises(OSError, match="simulated disk full"):
        await wiki.ingest(retry_body)

    code_retry, res_retry = await wiki.ingest(retry_body)
    assert code_retry == 200 and res_retry["ok"] is True
    assert res_retry.get("idempotent_replay") is True
    assert wiki._find_audit_by_request_id("wiki-audit-retry-1") is not None
    with sqlite3.connect(wiki.wiki_db) as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM documents WHERE category_name = 'MSFT'").fetchone()[0]
        assert cnt == 1

    # 2. Sales Notes: PDF + manifest/audit failure retry recovery
    _, lib_mod = _load_commercial_connectors()
    sn_store = lib_mod.MarketDigestPartitionStore(tmp_path / "sn_root")
    code_sn, res_sn = await sn_store.ingest_sales_note(
        {
            "request_id": "sn-pdf-1",
            "institution": "Goldman Sachs",
            "title": "Blackwell Rack Preview",
            "date": "2026-08-15",
            "file": {
                "filename": "gs_note.pdf",
                "content_b64": base64.b64encode(pdf_bytes).decode("ascii"),
            },
        }
    )
    assert code_sn == 200 and res_sn["ok"] is True
    assert ((tmp_path / "sn_root") / res_sn["original_filepath"]).read_bytes() == pdf_bytes
    assert "Blackwell Revenue Guidance 42B" in ((tmp_path / "sn_root") / res_sn["filepath"]).read_text(encoding="utf-8")

    # Simulate manifest append failure -> retry backfills manifest & audit without duplicating email_chunks
    sn_fail = {"flag": True}
    orig_append_manifest = lib_mod.sales_notes_store_mod.append_manifest

    def _flaky_manifest(root: Path, entry: dict[str, Any]) -> None:
        if sn_fail["flag"]:
            sn_fail["flag"] = False
            raise OSError("simulated manifest write failure")
        orig_append_manifest(root, entry)

    monkeypatch.setattr(lib_mod.sales_notes_store_mod, "append_manifest", _flaky_manifest)
    sn_retry_body = {
        "request_id": "sn-retry-1",
        "institution": "Morgan Stanley",
        "title": "Cloud Capex Check",
        "date": "2026-08-16",
        "content": "Hyperscaler capex tracking ahead of guidance",
    }
    with pytest.raises(OSError, match="simulated manifest write failure"):
        await sn_store.ingest_sales_note(sn_retry_body)

    code_sn2, res_sn2 = await sn_store.ingest_sales_note(sn_retry_body)
    assert code_sn2 == 200 and res_sn2["ok"] is True and res_sn2.get("idempotent_replay") is True
    assert res_sn2["doc_key"] in sn_store._load_manifest()
    with sqlite3.connect(sn_store.mail_db) as conn:
        cnt_sn = conn.execute(
            "SELECT COUNT(*) FROM email_chunks WHERE sender = 'Morgan Stanley'"
        ).fetchone()[0]
        assert cnt_sn == 1


@pytest.mark.asyncio
async def test_shared_capability_env_disables_backend_and_research_host_together(
    tmp_path: Path,
) -> None:
    """Decisions #4 & #5 (r6): backend (`src.config`) and `research_host.__main__` parse `.env` identically
    (including inline comments), host env cannot inject/override capabilities, and zero env leaks to process."""
    from src.dalton.research_host.__main__ import build_config_from_env

    shared_env = tmp_path / ".env"
    shared_env.write_text(
        "APP_ENV=core # core deployment\n"
        "DALTON_PROFILE=core # inline comment\n"
        "DALTON_CAP_COMPANY_WIKI=false # disabled explicitly\n",
        encoding="utf-8",
    )
    host_env = tmp_path / "research-host.env"
    host_env.write_text(
        f"DALTON_RESEARCH_HOME={tmp_path / 'data'}\n"
        "DALTON_RESEARCH_HOST_TOKEN=tok-mcp-shared\n"
        "DALTON_RESEARCH_ADMIN_TOKEN=tok-adm-shared\n"
        "APP_ENV=dalton\n"
        "DALTON_PROFILE=everflow\n"
        "DALTON_CAP_COMPANY_WIKI=true\n"
        "DALTON_CAP_SALES_NOTES=true\n",
        encoding="utf-8",
    )

    # 1. Isolated subprocess parity check: both backend and research-host parse the exact same capabilities
    probe = (
        "import json\n"
        "from src.config import load_shared_dotenv\n"
        "from src.dalton.capabilities import effective_capabilities\n"
        "from src.dalton.profile import resolve_profile\n"
        f"load_shared_dotenv({str(shared_env)!r})\n"
        "print(json.dumps({'profile': resolve_profile().value, 'caps': sorted(c.value for c in effective_capabilities())}))\n"
    )
    probe_rh = (
        "import json\n"
        "from src.dalton.capabilities import effective_capabilities\n"
        "from src.dalton.profile import resolve_profile\n"
        "from src.dalton.research_host.__main__ import load_shared_and_host_env\n"
        f"load_shared_and_host_env(env_file={str(host_env)!r}, repo_env_file={str(shared_env)!r})\n"
        "print(json.dumps({'profile': resolve_profile().value, 'caps': sorted(c.value for c in effective_capabilities())}))\n"
    )
    clean_sub_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(REPO_ROOT),
    }
    p_be = subprocess.run([sys.executable, "-c", probe], env=clean_sub_env, capture_output=True, text=True, check=False)
    assert p_be.returncode == 0, p_be.stderr
    p_rh = subprocess.run([sys.executable, "-c", probe_rh], env=clean_sub_env, capture_output=True, text=True, check=False)
    assert p_rh.returncode == 0, p_rh.stderr
    res_be = json.loads(p_be.stdout.strip())
    res_rh = json.loads(p_rh.stdout.strip())
    assert res_be == res_rh == {"profile": "core", "caps": []}

    # 2. HTTP enforcement via isolated environ dict (never mutates global os.environ)
    cfg = build_config_from_env(env_file=str(host_env), repo_env_file=str(shared_env), environ={})
    assert cfg.profile is DeploymentProfile.CORE
    assert cfg.mcp_token == "tok-mcp-shared"
    app = create_app(cfg, capabilities_provider=lambda: frozenset())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:8951",
    ) as client:
        r_mcp = await client.post(
            "/mcp/company-wiki",
            headers={"Authorization": "Bearer tok-mcp-shared"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert r_mcp.status_code == 403
        assert r_mcp.json()["error_code"] == "capability_disabled"

        r_ing = await client.post(
            "/ingest/wiki",
            headers={"Authorization": "Bearer tok-adm-shared"},
            json={"target_type": "company", "target": "NVDA", "content_type": "note", "date": "2026-08-16", "content": "x"},
        )
        assert r_ing.status_code == 403
        assert r_ing.json()["error_code"] == "capability_disabled"


@pytest.mark.asyncio
async def test_federated_dynamic_tools_list_param_filtering_and_nonblocking_sqlite(
    tmp_path: Path,
) -> None:
    """Decisions #5 & #6: federated tools/list reflects ONLY enabled sources; params filtered per source; blocking SQLite offloaded."""
    import time

    wiki_store = OpenWikiStore(tmp_path)
    await wiki_store.ingest(
        {
            "request_id": "fed-dyn-1",
            "target_type": "company",
            "target": "NVDA",
            "content_type": "研究笔记",
            "date": "2026-08-15",
            "content": "AI infrastructure demand",
        }
    )

    # 1. Core default HTTP `tools/list` on `/mcp/federated` exposes ONLY company-wiki (no sales_mail/tmtb/funda)
    app = create_app(
        ResearchHostConfig(
            data_root=tmp_path,
            mcp_token="mcp-dyn-1",
            admin_token="adm-dyn-2",
            profile=DeploymentProfile.CORE,
        ),
        capabilities_provider=lambda: frozenset({CapabilityId.COMPANY_WIKI}),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:8951",
    ) as client:
        r_list = await client.post(
            "/mcp/federated",
            headers={"Authorization": "Bearer mcp-dyn-1"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert r_list.status_code == 200
        tools = r_list.json()["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        assert tool_names == ["search", "read", "get_document", "list"]
        assert "sales_mail" not in tool_names
        blob = json.dumps(tools)
        for forbidden in ("sales_mail", "sales-notes", "mail:", "digest:", "tmtb:", "funda:"):
            assert forbidden not in blob
        assert "wiki:" in blob

    # 2. Per-source param allowlist & non-blocking `asyncio.to_thread` slow SQLite leg
    received_params: dict[str, dict[str, Any]] = {}

    async def _fast_wiki_leg(
        q: str, s: str | None, e: str | None, l: int, p: dict[str, Any], _c: str
    ) -> dict[str, Any]:
        received_params["company-wiki"] = dict(p)
        return await asyncio.to_thread(wiki_store.query, "search", query=q, start=s, end=e, limit=l, params=p)

    def _blocking_slow_sqlite(p: dict[str, Any]) -> dict[str, Any]:
        received_params["sales-notes"] = dict(p)
        time.sleep(0.35)
        return {"ok": True, "status": "ok", "results": []}

    async def _slow_sales_leg(
        q: str, s: str | None, e: str | None, l: int, p: dict[str, Any], _c: str
    ) -> dict[str, Any]:
        _ = (q, s, e, l)
        return await asyncio.to_thread(_blocking_slow_sqlite, p)

    reg = ConnectorRegistry()
    reg.register_federated_source(
        "company-wiki",
        CapabilityId.COMPANY_WIKI,
        search_fn=_fast_wiki_leg,
        allowed_params=frozenset({"company", "sector", "tag", "search_mode", "no_decay", "media", "indexed_since"}),
        prefixes=("wiki",),
    )
    reg.register_federated_source(
        "sales-notes",
        CapabilityId.SALES_NOTES,
        search_fn=_slow_sales_leg,
        allowed_params=frozenset({"sender", "layer", "search_mode", "media"}),
        prefixes=("mail", "digest"),
    )
    reg.register_federated_alias("sales_mail", ("sales-notes", "tmtb"))
    fed = FederatedResearchConnector(reg, wiki_store, fanout_grace_s=0.05)

    # With sales-notes enabled, `sales_mail` IS advertised
    tools_both = fed.list_tools(active_caps=frozenset({CapabilityId.COMPANY_WIKI, CapabilityId.SALES_NOTES}))
    assert "sales_mail" in [t["name"] for t in tools_both]

    t0 = time.monotonic()
    res = await fed.call_tool(
        "search",
        {"query": "AI", "params": {"sender": "GS", "company": "NVDA", "layer": "digest"}},
        active_caps=frozenset({CapabilityId.COMPANY_WIKI, CapabilityId.SALES_NOTES}),
    )
    elapsed = time.monotonic() - t0
    assert res["ok"] is True and res["status"] == "partial"
    assert res["source_status"]["company-wiki"]["status"] == "ok"
    assert res["source_status"]["sales-notes"]["status"] == "timeout"
    assert elapsed < 0.25, f"Slow thread leg blocked event loop ({elapsed:.2f}s)"
    assert received_params["company-wiki"] == {"company": "NVDA"}
    assert received_params["sales-notes"] == {"sender": "GS", "layer": "digest"}


def test_sec_alignment_regex_no_as_of_facts_yfinance_fallback_and_early_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision #7: align SEC facts(as_of=None), regex search-concepts, search-text early stop, & yfinance fallback."""
    sec_mod = _load_dalton_sec_module()
    facts_json = _sample_company_facts_payload()
    facts_json["facts"]["us-gaap"]["Revenues"]["units"]["USD"].append(
        {
            "start": "2024-10-01",
            "end": "2025-09-30",
            "val": 391000000000,
            "accn": "0000320193-25-000100",
            "fy": 2025,
            "fp": "FY",
            "form": "10-K",
            "filed": "2025-11-01",
        }
    )

    # 1. `select_raw_concept_rows` with as_of=None & form='10-Q' preserves 10-K periods (matches point_in_time.py)
    _, rows_no_asof = sec_mod.select_raw_concept_rows(
        facts_json, concept="us-gaap:Revenues", form="10-Q", limit=10, as_of=None
    )
    assert any(r["form_type"] == "10-K" for r in rows_no_asof)
    assert any(r["form_type"] == "10-Q" for r in rows_no_asof)

    # 2. `search_concepts_in_facts` supports regex matching and safe invalid-regex fallback
    regex_hits = sec_mod.search_concepts_in_facts(facts_json, query="Rev.*|Gross")
    assert any(h["concept"] == "us-gaap:Revenues" for h in regex_hits)
    # Invalid regex does not raise
    assert sec_mod.search_concepts_in_facts(facts_json, query="[unclosed") == []

    # 3. `search_filing_text` stops early after max_matches
    class _FakeFiling:
        form = "10-Q"
        filing_date = "2026-08-01"
        report_date = "2026-06-30"
        accession_no = "0001045810-26-000010"

        def text(self) -> str:
            return " ".join(["Blackwell demand strong"] * 10)

    class _FakeFilings:
        def __init__(self, items: list[Any]) -> None:
            self._items = items

        def __len__(self) -> int:
            return len(self._items)

        def head(self, n: int) -> list[Any]:
            return self._items[:n]

        def filter(self, **_kw: Any) -> _FakeFilings:
            return self

    class _FakeCompany:
        def __init__(self, items: list[Any]) -> None:
            self._items = items

        def get_filings(self, form: str | None = None) -> _FakeFilings:
            return _FakeFilings(self._items)

    monkeypatch.setattr(sec_mod, "_get_edgar_company", lambda _t: _FakeCompany([_FakeFiling()]))
    st_res = sec_mod.search_filing_text("NVDA", "Blackwell", form="10-Q", max_matches=2)
    assert st_res["ok"] is True
    assert len(st_res["filings"][0]["matches"]) == 2
    assert st_res["filings"][0]["total_matches"] == 3  # stopped immediately at max_matches + 1

    # 4. `get_financials` & `get_ratios` fallback to yfinance ONLY when as_of is None
    monkeypatch.setattr(sec_mod, "_get_edgar_company", lambda _t: _FakeCompany([]))
    monkeypatch.setattr(
        sec_mod,
        "_yf_financials_fallback",
        lambda t, form, stmt: {"ok": True, "status": "ok", "source": "yfinance", "fallback_used": True, "ticker": t, "form": form, "rows": [{"statements": {"income": []}}]},
    )
    monkeypatch.setattr(
        sec_mod,
        "_yf_ratios_fallback",
        lambda t, form: {"ok": True, "status": "ok", "source": "yfinance", "fallback_used": True, "ticker": t, "form": form, "ratios": [{"gross_margin": 75.0}]},
    )
    fin_fb = sec_mod.get_financials("NVDA", form="10-Q", as_of=None)
    assert fin_fb["ok"] is True and fin_fb.get("fallback_used") is True
    fin_pit = sec_mod.get_financials("NVDA", form="10-Q", as_of="2026-08-01")
    assert fin_pit["ok"] is False  # point-in-time query never uses yfinance fallback

    rat_fb = sec_mod.get_ratios("NVDA", form="10-Q", as_of=None)
    assert rat_fb["ok"] is True and rat_fb.get("fallback_used") is True
    rat_pit = sec_mod.get_ratios("NVDA", form="10-Q", as_of="2026-08-01")
    assert rat_pit["ok"] is False


@pytest.mark.asyncio
async def test_core_data_map_probing_and_transport_aware_cache_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision #8: Core probes standalone company-wiki without bridge; cache key distinguishes transport mode."""
    from src.dalton.data_map import health as dm_health
    from src.dalton.database.data_map import data_map_cache_fingerprint

    # 1. Core probes `company-wiki` via research_host_caller even with bridge disabled
    _clean_profile_env(monkeypatch, profile="core")
    probed: list[tuple[str, str]] = []

    async def _rh_caller(route_id: str, tool_name: str, arguments: dict[str, Any], *, timeout_s: float = 10.0) -> dict[str, Any]:
        _ = (arguments, timeout_s)
        probed.append((route_id, tool_name))
        return {"ok": True, "results": [{"date": "2026-08-18"}]}

    bridge_never = create_autospec(
        dm_health.default_bridge_rpc_call,
        side_effect=AssertionError("Bridge must not be called in Core"),
    )
    market_ok = create_autospec(dm_health.default_market_quote_probe, return_value={"latest_date": "2026-08-18"})
    sec_ok = create_autospec(dm_health.default_sec_filings_probe, return_value={"latest_date": "2026-08-18"})

    summary = await dm_health.run_all_health_checks(
        bridge_caller=bridge_never,
        market_quote_prober=market_ok,
        sec_filings_prober=sec_ok,
        research_host_caller=_rh_caller,
        persist=False,
    )
    assert ("company-wiki", "list") in probed
    source_ids = {r["source_id"] for r in summary["results"]}
    assert "company-wiki" in source_ids

    # 2. Transport-aware data map cache fingerprint distinguishes pre-cutover vs post-cutover Everflow
    _clean_profile_env(monkeypatch, profile="everflow")
    fp_pre = data_map_cache_fingerprint()
    monkeypatch.setenv("DALTON_RESEARCH_HOST_URL", "http://host.docker.internal:8951")
    fp_post = data_map_cache_fingerprint()
    assert fp_pre != fp_post
    assert fp_post.endswith(":b3")


# ---------------------------------------------------------------------------
# 10. r6 Review Fixes: compressed pypdf extraction, chunking, image index,
#     credential leak rejection, omitted request_id retry, Core leakage checks,
#     legacy bridge error wording, SEC yfinance/multi-word alignment, &
#     Core default host probe + backup/restore Wiki volume round-trip
# ---------------------------------------------------------------------------


def _make_compressed_pdf_bytes(text: str) -> bytes:
    """Create a real FlateDecode-compressed PDF readable by `pypdf` (not raw ASCII fallback)."""
    from pypdf import PdfWriter
    from pypdf.generic import ContentStream, DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=400)
    font_dict = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font_dict)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 20 350 Td ({text}) Tj ET".encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    page.compress_content_streams()
    assert isinstance(page.get_contents(), ContentStream) or b"/FlateDecode" in _dump_writer_bytes(writer)
    return _dump_writer_bytes(writer)


def _dump_writer_bytes(writer: Any) -> bytes:
    import io

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_empty_scanned_pdf_bytes() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    return _dump_writer_bytes(writer)


@pytest.mark.asyncio
async def test_r6_compressed_pdf_pypdf_chunking_image_index_secret_check_and_omitted_request_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Items #4 & #8: compressed PDF via pypdf, empty PDF rejection, multi-chunking, image indexing,
    credential leak rejection, and omitted request_id crash-retry backfill."""
    compressed_pdf = _make_compressed_pdf_bytes("Compressed Rubin Optical Interconnect 2027")
    assert b"Compressed Rubin" not in compressed_pdf  # genuinely FlateDecode-compressed!

    # Force Core-only path (no company_wiki_cli, no fitz) so pypdf is exercised directly
    wiki = OpenWikiStore(tmp_path / "wiki_r6")
    wiki._cli_checked = True
    wiki._cli_module = None
    monkeypatch.setitem(sys.modules, "fitz", None)

    code_pdf, res_pdf = await wiki.ingest(
        {
            "request_id": "r6-cpdf-1",
            "target_type": "company",
            "target": "NVDA",
            "content_type": "研报笔记",
            "date": "2026-08-20",
            "file": {
                "filename": "compressed_rubin.pdf",
                "content_b64": base64.b64encode(compressed_pdf).decode("ascii"),
            },
        }
    )
    assert code_pdf == 200 and res_pdf["ok"] is True
    s_hit = wiki.query("search", query="Interconnect")
    assert s_hit["ok"] is True and s_hit["result_count"] == 1

    # Empty/scanned PDF with no text layer MUST fail with 400 (never silently index placeholder)
    empty_pdf = _make_empty_scanned_pdf_bytes()
    code_empty, res_empty = await wiki.ingest(
        {
            "request_id": "r6-empty-pdf",
            "target_type": "company",
            "target": "NVDA",
            "content_type": "研报笔记",
            "date": "2026-08-20",
            "file": {
                "filename": "scan.pdf",
                "content_b64": base64.b64encode(empty_pdf).decode("ascii"),
            },
        }
    )
    assert code_empty == 400 and res_empty["error_code"] == "empty_pdf_text"

    # Long document splits into multiple chunks (not a single giant chunk)
    long_text = "\n\n".join(f"Paragraph {i}: Blackwell rack density metrics and cooling analysis." for i in range(60))
    code_long, res_long = await wiki.ingest(
        {
            "request_id": "r6-long-1",
            "target_type": "company",
            "target": "NVDA",
            "content_type": "研报笔记",
            "date": "2026-08-20",
            "content": long_text,
        }
    )
    assert code_long == 200 and res_long["chunk_count"] >= 2
    with sqlite3.connect(wiki.wiki_db) as conn:
        n_chunks = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE doc_id = ?",
            (int(res_long["doc_id"].split(":")[1]),),
        ).fetchone()[0]
        assert n_chunks >= 2

    # Image ingest indexes into state/research-images/index.sqlite and is searchable & readable
    png_bytes = _make_minimal_png_bytes()
    code_img, res_img = await wiki.ingest(
        {
            "request_id": "r6-img-1",
            "target_type": "company",
            "target": "NVDA",
            "content_type": "研报笔记",
            "date": "2026-08-20",
            "file": {
                "filename": "co_packaged_optics_roadmap.png",
                "content_b64": base64.b64encode(png_bytes).decode("ascii"),
            },
        }
    )
    assert code_img == 200 and res_img["ok"] is True
    assert str(res_img.get("image_id") or "").startswith("image:")
    img_hits = wiki.query("search", query="optics", params={"media": "images"})
    assert img_hits["ok"] is True and img_hits["result_count"] == 1
    assert img_hits["results"][0]["document_id"] == res_img["image_id"]
    img_code, img_doc = wiki.read_image(
        res_img["image_id"], active_caps=frozenset({CapabilityId.COMPANY_WIKI})
    )
    assert img_code == 200 and img_doc["ok"] is True

    # Credential / secret leak is rejected before DB or disk writes
    code_sec, res_sec = await wiki.ingest(
        {
            "target_type": "company",
            "target": "NVDA",
            "content_type": "研报笔记",
            "date": "2026-08-20",
            "content": "Leaked key sk-proj-abcdefghijklmnopqrstuvwxyz123456 in transcript",
        }
    )
    assert code_sec == 400 and res_sec["error_code"] == "secret_detected"

    # Omitted request_id crash-recovery: audit append fails -> retry WITHOUT request_id backfills audit & zero duplicate DB rows
    orig_append = wiki._append_audit
    fail_once = {"flag": True}

    def _flaky_audit(entry: dict[str, Any]) -> None:
        if fail_once["flag"]:
            fail_once["flag"] = False
            raise OSError("simulated audit failure")
        orig_append(entry)

    monkeypatch.setattr(wiki, "_append_audit", _flaky_audit)
    no_req_body = {
        "target_type": "company",
        "target": "AMZN",
        "content_type": "研报笔记",
        "date": "2026-08-20",
        "content": "AWS Trainium3 deployment update without request_id",
    }
    with pytest.raises(OSError, match="simulated audit failure"):
        await wiki.ingest(no_req_body)
    code_rec, res_rec = await wiki.ingest(no_req_body)
    assert code_rec == 200 and res_rec["ok"] is True and res_rec.get("idempotent_replay") is True
    with sqlite3.connect(wiki.wiki_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents WHERE category_name = 'AMZN'").fetchone()[0] == 1

    # Sales notes: multi-chunking, secret rejection, and omitted request_id manifest backfill
    _, lib_mod = _load_commercial_connectors()
    sn_store = lib_mod.MarketDigestPartitionStore(tmp_path / "sn_r6")
    code_sn_sec, res_sn_sec = await sn_store.ingest_sales_note(
        {
            "institution": "GS",
            "title": "Secret Note",
            "date": "2026-08-20",
            "content": "Authorization: Bearer secret_token_value_1234567890",
        }
    )
    assert code_sn_sec == 400 and res_sn_sec["error_code"] == "secret_detected"

    code_sn_long, res_sn_long = await sn_store.ingest_sales_note(
        {
            "institution": "Morgan Stanley",
            "title": "Long Semi Check",
            "date": "2026-08-20",
            "content": long_text,
        }
    )
    assert code_sn_long == 200 and res_sn_long["chunk_count"] >= 2

    sn_fail = {"flag": True}
    orig_manifest = lib_mod.sales_notes_store_mod.append_manifest

    def _flaky_manifest(root: Path, entry: dict[str, Any]) -> None:
        if sn_fail["flag"]:
            sn_fail["flag"] = False
            raise OSError("simulated manifest failure")
        orig_manifest(root, entry)

    monkeypatch.setattr(lib_mod.sales_notes_store_mod, "append_manifest", _flaky_manifest)
    sn_no_req = {
        "institution": "JPM",
        "title": "No RequestId Retry",
        "date": "2026-08-20",
        "content": "Hyperscaler optical transceiver demand note",
    }
    with pytest.raises(OSError, match="simulated manifest failure"):
        await sn_store.ingest_sales_note(sn_no_req)
    code_sn_rec, res_sn_rec = await sn_store.ingest_sales_note(sn_no_req)
    assert code_sn_rec == 200 and res_sn_rec["ok"] is True and res_sn_rec.get("idempotent_replay") is True
    assert res_sn_rec["doc_key"] in sn_store._load_manifest()
    with sqlite3.connect(sn_store.mail_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM email_chunks WHERE sender = 'JPM'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_r6_core_isolation_error_copy_sec_yfinance_and_default_core_health_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Items #6, #7, #9 + Codex SEC alignment: Core federated error omits private prefixes, README has no Everflow URL,
    legacy bridge error text preserved, SEC multi-word & yfinance aligned, Core default host probed."""
    from src.dalton.data_map import health as dm_health

    # 1. Core README has no Everflow host URL
    readme_text = (REPO_ROOT / "plugins" / "dalton_company_wiki" / "README.md").read_text(encoding="utf-8")
    assert "host.docker.internal" not in readme_text
    assert "Everflow" not in readme_text

    # 2. Core `/mcp/federated` read error omits Everflow-private prefixes
    app = create_app(
        ResearchHostConfig(
            data_root=tmp_path / "rh_core",
            mcp_token="tok-core-fed",
            admin_token="tok-core-adm",
            profile=DeploymentProfile.CORE,
        ),
        capabilities_provider=lambda: frozenset({CapabilityId.COMPANY_WIKI}),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8951") as client:
        r_bad = await client.post(
            "/mcp/federated",
            headers={"Authorization": "Bearer tok-core-fed"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "read", "arguments": {"document": ""}}},
        )
        assert r_bad.status_code == 200
        err_text = r_bad.json()["result"]["content"][0]["text"]
        assert "wiki:" in err_text
        for forbidden in ("mail:", "digest:", "tmtb:", "funda:"):
            assert forbidden not in err_text

    # 3. Legacy bridge HTTP 500 error copy preserved verbatim on wiki_tools & ingest_tools
    _clean_profile_env(monkeypatch, profile="everflow")
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", "bridge-adm")
    monkeypatch.setenv("DALTON_RESEARCH_ADMIN_TOKEN", "host-adm")
    orig_client = httpx.AsyncClient

    def _err500_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(lambda _req: httpx.Response(500, text="<html>boom</html>"))
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _err500_client)
    st_w, data_w = await wiki_tools_mod.call_bridge_wiki_ingest({"target": "NVDA"})
    assert st_w == 500 and data_w["error"] == "Bridge returned HTTP 500"
    st_s, data_s = await ingest_tools_mod.call_bridge_sales_notes_ingest({"title": "Note"})
    assert st_s == 500 and data_s["error"] == "Bridge returned HTTP 500"

    # 4. SEC multi-word search-concepts & exact yfinance fallback structure
    sec_mod = _load_dalton_sec_module()
    facts_json = _sample_company_facts_payload()
    assert sec_mod.search_concepts_in_facts(facts_json, query="Revenues Profit") == []

    class _FakeDf:
        empty = False

        def to_dict(self) -> dict[str, Any]:
            return {"2026-06-30": {"Total Revenue": 95000000000}}

    class _FakeTicker:
        def __init__(self, _t: str) -> None:
            self.quarterly_income_stmt = _FakeDf()
            self.quarterly_balance_sheet = None
            self.quarterly_cashflow = None
            self.income_stmt = _FakeDf()
            self.balance_sheet = None
            self.cashflow = None
            self.info = {"grossMargins": 0.72, "totalRevenue": 95000000000}

    fake_yf = type(sys)("yfinance")
    fake_yf.Ticker = _FakeTicker  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    yf_fin = sec_mod._yf_financials_fallback("NVDA", "10-Q")
    assert yf_fin is not None and yf_fin["rows"][0]["filing_date"] == "yfinance"
    assert isinstance(yf_fin["rows"][0]["statements"]["income"], dict)
    yf_rat = sec_mod._yf_ratios_fallback("NVDA", "10-Q")
    assert yf_rat is not None and yf_rat["ratios"][0]["filing_date"] == "yfinance-latest"

    # 5. Core default (NO explicit DALTON_RESEARCH_HOST_URL) probes default http://research-host:8951/mcp/company-wiki
    #    with tools/list discovery + tools/call
    _clean_profile_env(monkeypatch, profile="core")
    monkeypatch.delenv("DALTON_RESEARCH_HOST_URL", raising=False)
    monkeypatch.delenv("DALTON_RESEARCH_HOST_BASE_URL", raising=False)
    seen_methods: list[tuple[str, str]] = []

    def _rh_default_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen_methods.append((str(request.url), str(body.get("method"))))
        if body.get("method") == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 0, "result": {"tools": [{"name": "list"}]}})
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"ok": True, "results": [{"date": "2026-08-20"}]}}},
        )

    def _rh_mock_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(_rh_default_handler)
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _rh_mock_client)
    summary = await dm_health.run_all_health_checks(
        bridge_caller=create_autospec(dm_health.default_bridge_rpc_call, side_effect=AssertionError("no bridge in core")),
        market_quote_prober=create_autospec(dm_health.default_market_quote_probe, return_value={"latest_date": "2026-08-20"}),
        sec_filings_prober=create_autospec(dm_health.default_sec_filings_probe, return_value={"latest_date": "2026-08-20"}),
        persist=False,
    )
    assert ("http://research-host:8951/mcp/company-wiki", "tools/list") in seen_methods
    assert ("http://research-host:8951/mcp/company-wiki", "tools/call") in seen_methods
    wiki_rows = [r for r in summary["results"] if r["source_id"] == "company-wiki"]
    assert len(wiki_rows) == 1 and wiki_rows[0]["status"] == "available"


# ---------------------------------------------------------------------------
# r7: Wiki pause-writes, shared .env interpolation & host allowlist, Core
#     release/healthz/lock isolation, raw-byte attachment secret scan,
#     unified host URL, SEC strict JSON fallback, and 3b deferred test patch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r7_wiki_pause_writes_shared_dotenv_interpolation_host_allowlist_and_core_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Items #1 (API), #3, #4: pause-writes admin API, shared .env variable interpolation parity,
    host env allowlist with warnings, Core /healthz & package_release.sh & commercial lock gating."""
    import fcntl
    import logging

    from src.config import load_shared_dotenv as backend_load_shared_dotenv
    from src.dalton.capabilities import effective_capabilities
    from src.dalton.research_host.__main__ import load_shared_and_host_env

    # 1. Item #1: `/admin/wiki/pause-writes` pauses `/ingest/wiki` with 503 and resumes cleanly
    app = create_app(
        ResearchHostConfig(
            data_root=tmp_path / "rh_pause",
            mcp_token="tok-r7-mcp",
            admin_token="tok-r7-adm",
            profile=DeploymentProfile.CORE,
        ),
        capabilities_provider=lambda: frozenset({CapabilityId.COMPANY_WIKI}),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8951") as client:
        # Item #4: Core default `/healthz` MUST NOT expose Everflow-only `alphaengine_usage` or `extra_modules`
        hz = (await client.get("/healthz")).json()
        assert hz["ok"] is True
        assert "alphaengine_usage" not in hz
        assert "extra_modules" not in hz
        assert hz["connectors"]["company-wiki"] is True

        # Unauthenticated pause-writes rejected
        assert (await client.post("/admin/wiki/pause-writes", json={"paused": True})).status_code == 401
        p_on = await client.post(
            "/admin/wiki/pause-writes",
            headers={"Authorization": "Bearer tok-r7-adm"},
            json={"paused": True},
        )
        assert p_on.status_code == 200 and p_on.json()["paused"] is True and p_on.json()["in_flight"] == 0

        # Wiki ingest returns 503 while paused
        blocked = await client.post(
            "/ingest/wiki",
            headers={"Authorization": "Bearer tok-r7-adm"},
            json={
                "request_id": "r7-paused-1",
                "target_type": "company",
                "target": "NVDA",
                "content_type": "研报笔记",
                "date": "2026-11-26",
                "content": "Should be rejected while paused",
            },
        )
        assert blocked.status_code == 503 and blocked.json()["error_code"] == "wiki_writes_paused"

        # Resume writes -> ingest succeeds
        p_off = await client.post(
            "/admin/wiki/pause-writes",
            headers={"Authorization": "Bearer tok-r7-adm"},
            json={"paused": False},
        )
        assert p_off.status_code == 200 and p_off.json()["paused"] is False
        ok_resp = await client.post(
            "/ingest/wiki",
            headers={"Authorization": "Bearer tok-r7-adm"},
            json={
                "request_id": "r7-resumed-1",
                "target_type": "company",
                "target": "NVDA",
                "content_type": "研报笔记",
                "date": "2026-11-26",
                "content": "Ingested cleanly after resume",
            },
        )
        assert ok_resp.status_code == 200 and ok_resp.json()["ok"] is True

    # 2. Item #3: Same shared `.env` with `${VAR}` interpolation + process env yields IDENTICAL capabilities
    #    on both backend (`src.config.load_shared_dotenv`) and research-host (`load_shared_and_host_env`),
    #    and host-only env file ignores non-allowlisted capability keys (`CHANNEL_GATEWAY_URL`,
    #    `DALTON_TRANSCRIPTION_ENABLED`, `DALTON_CAP_*`, `DALTON_RESEARCHDATA_URL`) with warning logs.
    shared_env_file = tmp_path / "shared_interp.env"
    shared_env_file.write_text(
        "\n".join(
            [
                "APP_ENV=core",
                "WIKI_TOGGLE=true",
                "DALTON_CAP_COMPANY_WIKI=${WIKI_TOGGLE} # interpolated from process env first",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    host_only_env_file = tmp_path / "host_only.env"
    host_only_env_file.write_text(
        "\n".join(
            [
                "DALTON_RESEARCH_HOST_PORT=8999",
                "DALTON_RESEARCH_HOST_TOKEN=host-tok-allow",
                "CHANNEL_GATEWAY_URL=http://rogue-gw:8962",
                "DALTON_TRANSCRIPTION_ENABLED=true",
                "DALTON_CAP_COMPANY_WIKI=true",
                "DALTON_RESEARCHDATA_URL=http://rogue-bridge:8960/mcp",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    proc_env_base = {"WIKI_TOGGLE": "false"}
    backend_env = dict(proc_env_base)
    backend_load_shared_dotenv(shared_env_file, environ=backend_env)
    backend_caps = effective_capabilities(profile=DeploymentProfile.CORE, environ=backend_env)

    rh_env = dict(proc_env_base)
    with caplog.at_level(logging.WARNING, logger="dalton-research-host"):
        load_shared_and_host_env(
            repo_env_file=str(shared_env_file),
            env_file=str(host_only_env_file),
            environ=rh_env,
        )
    rh_caps = effective_capabilities(profile=DeploymentProfile.CORE, environ=rh_env)

    assert backend_env["DALTON_CAP_COMPANY_WIKI"] == "false"
    assert rh_env["DALTON_CAP_COMPANY_WIKI"] == "false"
    assert backend_caps == rh_caps
    assert CapabilityId.COMPANY_WIKI not in rh_caps
    assert CapabilityId.CHANNEL_GATEWAY not in rh_caps
    assert CapabilityId.TRANSCRIPTION not in rh_caps
    assert CapabilityId.RESEARCHDATA_BRIDGE not in rh_caps
    # Allowlisted host keys WERE loaded; forbidden ones were NOT loaded and emitted warnings
    assert rh_env["DALTON_RESEARCH_HOST_PORT"] == "8999"
    assert rh_env["DALTON_RESEARCH_HOST_TOKEN"] == "host-tok-allow"
    assert "CHANNEL_GATEWAY_URL" not in rh_env
    assert "DALTON_TRANSCRIPTION_ENABLED" not in rh_env
    assert "DALTON_RESEARCHDATA_URL" not in rh_env
    warn_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "CHANNEL_GATEWAY_URL" in warn_text
    assert "DALTON_TRANSCRIPTION_ENABLED" in warn_text

    # Also verify backend vs research-host parity in real isolated subprocesses with process env WIKI_TOGGLE=false
    sub_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("DALTON_", "APP_ENV", "CHANNEL_GATEWAY"))
    }
    sub_env["WIKI_TOGGLE"] = "false"
    sub_env["DALTON_DOTENV_PATH"] = str(shared_env_file)
    sub_env["DALTON_RESEARCH_HOST_ENV_FILE"] = str(host_only_env_file)
    sub_backend = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; import src.config; from src.dalton.capabilities import effective_capabilities; "
            "print(json.dumps(sorted(c.value for c in effective_capabilities())))",
        ],
        cwd=str(REPO_ROOT),
        env=sub_env,
        capture_output=True,
        text=True,
        check=True,
    )
    sub_rh = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from src.dalton.research_host.__main__ import load_shared_and_host_env; "
            "from src.dalton.capabilities import effective_capabilities; "
            "load_shared_and_host_env(); "
            "print(json.dumps(sorted(c.value for c in effective_capabilities())))",
        ],
        cwd=str(REPO_ROOT),
        env=sub_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(sub_backend.stdout) == json.loads(sub_rh.stdout)
    assert "company_wiki" not in json.loads(sub_rh.stdout)

    # 3. Item #4: Commercial host_connectors checks capability BEFORE taking Guidepoint's exclusive lock
    gp_root = tmp_path / "gp_lock_isolated"
    gp_tok_dir = gp_root / "state" / "guidepoint"
    gp_tok_dir.mkdir(parents=True, exist_ok=True)
    gp_tok_file = gp_tok_dir / "tokens.json"
    gp_tok_file.write_text("{}", encoding="utf-8")
    gp_lock_file = gp_tok_dir / "tokens.json.lock"
    monkeypatch.setenv("DALTON_GUIDEPOINT_TOKENS_FILE", str(gp_tok_file))
    with gp_lock_file.open("a+") as held_fh:
        fcntl.flock(held_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            rt_no_gp = ResearchHostRuntime(
                ResearchHostConfig(
                    data_root=gp_root,
                    mcp_token="tok-nogp-mcp",
                    admin_token="tok-nogp-adm",
                    profile=DeploymentProfile.EVERFLOW,
                    extra_modules=["host_connectors"],
                    extra_python_paths=[str(REPO_ROOT / "scripts" / "dalton" / "research_runtime")],
                ),
                capabilities_provider=lambda: frozenset({CapabilityId.COMPANY_WIKI, CapabilityId.SALES_NOTES}),
            )
            assert "guidepoint" not in rt_no_gp.registry.connectors
            assert "guidepoint" not in rt_no_gp.registry.health_probes
            assert "sales-notes" in rt_no_gp.registry.connectors
            assert "tmtb" not in rt_no_gp.registry.connectors
            assert "funda-ai" not in rt_no_gp.registry.connectors
        finally:
            fcntl.flock(held_fh.fileno(), fcntl.LOCK_UN)

    # 4. Item #4: package_release.sh excludes docs-site/src/content/docs/everflow/
    pkg_src = (REPO_ROOT / "scripts" / "standalone" / "package_release.sh").read_text(encoding="utf-8")
    assert '"docs-site/src/content/docs/everflow"' in pkg_src
    # Run package_release.sh against current repo HEAD (using working-tree script) to verify exported dir
    pkg_out = tmp_path / "core_release_export"
    pkg_res = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "scripts" / "standalone" / "package_release.sh"),
            "--repo",
            str(REPO_ROOT),
            "--ref",
            "HEAD",
            "--output",
            str(pkg_out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert pkg_res.returncode == 0, f"package_release stderr:\n{pkg_res.stderr}"
    assert not (pkg_out / "docs-site" / "src" / "content" / "docs" / "everflow").exists()
    assert (pkg_out / "docs-site" / "astro.config.mjs").is_file()
    assert (pkg_out / "plugins" / "dalton_company_wiki" / "mcp.json").is_file()
    assert not (pkg_out / "scripts" / "dalton" / "research_runtime").exists()


@pytest.mark.asyncio
async def test_r7_raw_byte_attachment_secrets_unified_host_url_sec_strict_json_and_b3b_patch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Items #5, #6, #7, #8: raw-byte secret scan in PNG metadata & PDF /Info for Wiki & Sales Notes,
    unified DALTON_RESEARCH_HOST_BASE_URL across plugin/health/ingest, SEC yfinance strict JSON, and 3b patch."""
    import struct
    import zlib

    import pandas as pd
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    from src.dalton.plugin_loader import build_bundled_server_transport_kwargs

    # Helper: build a valid 1x1 PNG containing a secret inside a PNG `tEXt` metadata chunk
    base_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII="
    )
    secret_tok = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    text_data = b"Comment\x00" + secret_tok.encode("ascii")
    text_chunk = (
        struct.pack(">I", len(text_data))
        + b"tEXt"
        + text_data
        + struct.pack(">I", zlib.crc32(b"tEXt" + text_data) & 0xFFFFFFFF)
    )
    # Insert tEXt chunk right before IEND (last 12 bytes)
    png_with_secret = base_png[:-12] + text_chunk + base_png[-12:]

    # Helper: build a compressed PDF with CLEAN body text and secret hidden in PDF /Info metadata
    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=400)
    font_dict = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font_dict)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 350 Td (Clean public earnings commentary) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    page.compress_content_streams()
    writer.add_metadata({"/Author": "sk-" + "9x8y7z6w5v4u3t2s1r0q9p8o7n6m5l4k"})
    pdf_with_meta_secret = _dump_writer_bytes(writer)

    # 1. Item #5: Wiki rejects BOTH PNG metadata secret and PDF /Info secret (zero files saved)
    wiki_root = tmp_path / "wiki_secret_r7"
    wiki = OpenWikiStore(wiki_root)
    wiki._cli_checked = True
    wiki._cli_module = None
    for fname, raw_b in (("chart_meta.png", png_with_secret), ("memo_meta.pdf", pdf_with_meta_secret)):
        code, res = await wiki.ingest(
            {
                "request_id": f"r7-wiki-sec-{fname}",
                "target_type": "company",
                "target": "NVDA",
                "content_type": "研报笔记",
                "date": "2026-11-26",
                "file": {"filename": fname, "content_b64": base64.b64encode(raw_b).decode("ascii")},
            }
        )
        assert code == 400 and res["error_code"] == "secret_detected", (fname, code, res)
    assert not (wiki_root / "originals").exists() or not list((wiki_root / "originals").rglob("*"))

    # 2. Item #5: Sales Notes rejects BOTH PNG metadata secret and PDF /Info secret (zero files saved)
    runtime_dir = str(REPO_ROOT / "scripts" / "dalton" / "research_runtime")
    if runtime_dir not in sys.path:
        sys.path.insert(0, runtime_dir)
    from host_connectors.library_connectors import MarketDigestPartitionStore

    sn_root = tmp_path / "sn_secret_r7"
    sn_store = MarketDigestPartitionStore(sn_root)
    for fname, raw_b in (("chart_meta.png", png_with_secret), ("memo_meta.pdf", pdf_with_meta_secret)):
        code, res = await sn_store.ingest_sales_note(
            {
                "request_id": f"r7-sn-sec-{fname}",
                "institution": "TestSec",
                "title": "Clean Title",
                "kind": "研报",
                "date": "2026-11-26",
                "file": {"filename": fname, "content_b64": base64.b64encode(raw_b).decode("ascii")},
            }
        )
        assert code == 400 and res["error_code"] == "secret_detected", (fname, code, res)

    # 3. Item #6: Setting ONLY `DALTON_RESEARCH_HOST_BASE_URL` resolves plugin MCP URL,
    #    health check, and ingest to the exact same custom address (not `http://research-host:8951`).
    _clean_profile_env(monkeypatch, profile="core")
    monkeypatch.delenv("DALTON_RESEARCH_HOST_URL", raising=False)
    monkeypatch.setenv("DALTON_RESEARCH_HOST_BASE_URL", "http://custom-research-host:9951/")
    monkeypatch.setenv("DALTON_RESEARCH_HOST_TOKEN", "tok-unified")

    assert dalton_settings.get_dalton_research_host_base_url() == "http://custom-research-host:9951"
    wiki_mcp_cfg = json.loads((REPO_ROOT / "plugins" / "dalton_company_wiki" / "mcp.json").read_text(encoding="utf-8"))
    wiki_kwargs = build_bundled_server_transport_kwargs(
        "dalton_company_wiki",
        "streamable-http",
        wiki_mcp_cfg["mcpServers"]["dalton_company_wiki"],
    )
    assert wiki_kwargs["url"] == "http://custom-research-host:9951/mcp/company-wiki"
    all_mcp_cfg = json.loads((REPO_ROOT / "plugins" / "dalton_research_all" / "mcp.json").read_text(encoding="utf-8"))
    all_kwargs = build_bundled_server_transport_kwargs(
        "dalton_research_all",
        "streamable-http",
        all_mcp_cfg["mcpServers"]["dalton_research_all"],
    )
    assert all_kwargs["url"] == "http://custom-research-host:9951/mcp/federated"

    # 4. Item #7: SEC yfinance financials fallback converts pandas.Timestamp keys -> ISO strings
    #    and NaN/Inf -> None so strict JSON (allow_nan=False) always succeeds.
    sec_mod = _load_dalton_sec_module()
    real_df = pd.DataFrame(
        {
            pd.Timestamp("2026-06-30"): [95000000000.0, float("nan"), float("inf")],
            pd.Timestamp("2026-03-31"): [88000000000.0, 12345.0, float("-inf")],
        },
        index=["Total Revenue", "Missing Metric", "Infinite Metric"],
    )

    class _PandasTicker:
        def __init__(self, _t: str) -> None:
            self.quarterly_income_stmt = real_df
            self.quarterly_balance_sheet = None
            self.quarterly_cashflow = None
            self.income_stmt = real_df
            self.balance_sheet = None
            self.cashflow = None
            self.info = {"grossMargins": float("nan"), "totalRevenue": 95000000000}

    fake_yf = type(sys)("yfinance")
    fake_yf.Ticker = _PandasTicker  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    yf_fin = sec_mod._yf_financials_fallback("NVDA", "10-Q")
    assert yf_fin is not None
    strict_text = json.dumps(yf_fin, allow_nan=False)
    def _reject_const(c: str) -> Any:
        raise ValueError(f"Non-standard JSON constant: {c}")
    parsed_fin = json.loads(strict_text, parse_constant=_reject_const)
    inc = parsed_fin["rows"][0]["statements"]["income"]
    assert "2026-06-30T00:00:00" in inc
    assert inc["2026-06-30T00:00:00"]["Total Revenue"] == 95000000000.0
    assert inc["2026-06-30T00:00:00"]["Missing Metric"] is None
    assert inc["2026-06-30T00:00:00"]["Infinite Metric"] is None

    yf_rat = sec_mod._yf_ratios_fallback("NVDA", "10-Q")
    assert yf_rat is not None
    strict_rat = json.dumps(yf_rat, allow_nan=False)
    parsed_rat = json.loads(strict_rat, parse_constant=_reject_const)
    assert "gross_margin" not in parsed_rat["ratios"][0]



@pytest.mark.asyncio
async def test_r8_wiki_pause_lease_owner_ttl_dotenv_disabled_feishu_pkg_and_sec_nat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r8 closing checks:
    1. Pause lease ownership (409 on foreign pause/resume) and TTL auto-expiry;
    3. Core release package excludes deliver-to-feishu.mdx and b3_skills_prompts_ui check asserts absence;
    4. PYTHON_DOTENV_DISABLED=1 disables shared .env loading in both process env and explicit environ dict;
    5. pandas.NaT serializes to strict JSON null (never 'NaT') in SEC yfinance fallback.
    """
    import time

    import pandas as pd

    from src.dalton.config_env import load_shared_dotenv

    # 1. r8 #1: Pause lease ownership + TTL auto-expiry
    app = create_app(
        ResearchHostConfig(
            data_root=tmp_path / "rh_lease_r8",
            mcp_token="tok-r8-mcp",
            admin_token="tok-r8-adm",
            profile=DeploymentProfile.CORE,
        ),
        capabilities_provider=lambda: frozenset({CapabilityId.COMPANY_WIKI}),
    )
    adm_hdr = {"Authorization": "Bearer tok-r8-adm"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8951") as client:
        r_a = await client.post(
            "/admin/wiki/pause-writes",
            headers=adm_hdr,
            json={"paused": True, "owner": "backup-A", "ttl_s": 60},
        )
        assert r_a.status_code == 200 and r_a.json()["owner"] == "backup-A"
        lease_1 = r_a.json()["lease_id"]
        assert isinstance(lease_1, int) and lease_1 >= 1

        # r9 #1: Heartbeat renewal with matching lease_id keeps the SAME lease_id and expired=False
        r_renew = await client.post(
            "/admin/wiki/pause-writes",
            headers=adm_hdr,
            json={"paused": True, "owner": "backup-A", "lease_id": lease_1, "ttl_s": 60},
        )
        assert r_renew.status_code == 200
        assert r_renew.json()["lease_id"] == lease_1 and r_renew.json()["expired"] is False

        # Foreign owner cannot take over pause or prematurely resume
        r_b_pause = await client.post(
            "/admin/wiki/pause-writes",
            headers=adm_hdr,
            json={"paused": True, "owner": "backup-B", "ttl_s": 60},
        )
        assert r_b_pause.status_code == 409 and r_b_pause.json()["error_code"] == "wiki_pause_held_by_other"

        r_b_resume = await client.post(
            "/admin/wiki/pause-writes",
            headers=adm_hdr,
            json={"paused": False, "owner": "backup-B"},
        )
        assert r_b_resume.status_code == 409 and r_b_resume.json()["error_code"] == "wiki_pause_owner_mismatch"

        # Wiki is STILL paused after foreign resume attempt
        r_blocked = await client.post(
            "/ingest/wiki",
            headers=adm_hdr,
            json={
                "request_id": "r8-still-paused",
                "target_type": "company",
                "target": "NVDA",
                "content_type": "研报笔记",
                "date": "2026-11-26",
                "content": "Must still be blocked",
            },
        )
        assert r_blocked.status_code == 503 and r_blocked.json()["error_code"] == "wiki_writes_paused"

        # Owner A renews with short TTL -> auto-expires and unblocks Wiki writes automatically
        r_short = await client.post(
            "/admin/wiki/pause-writes",
            headers=adm_hdr,
            json={"paused": True, "owner": "backup-A", "lease_id": lease_1, "ttl_s": 0.05},
        )
        assert r_short.status_code == 200 and r_short.json()["lease_id"] == lease_1
        time.sleep(0.08)

        # r9 #1: Attempting to renew expired lease_1 returns 409 expired=True; resuming reports expired=True, continuous=False
        r_renew_exp = await client.post(
            "/admin/wiki/pause-writes",
            headers=adm_hdr,
            json={"paused": True, "owner": "backup-A", "lease_id": lease_1, "ttl_s": 60},
        )
        assert r_renew_exp.status_code == 409 and r_renew_exp.json()["expired"] is True

        r_res_exp = await client.post(
            "/admin/wiki/pause-writes",
            headers=adm_hdr,
            json={"paused": False, "owner": "backup-A", "lease_id": lease_1},
        )
        assert r_res_exp.status_code == 200
        assert r_res_exp.json()["paused"] is False
        assert r_res_exp.json()["expired"] is True
        assert r_res_exp.json()["continuous"] is False

        # Acquiring a fresh pause issues a strictly greater monotonic lease_id
        r_next = await client.post(
            "/admin/wiki/pause-writes",
            headers=adm_hdr,
            json={"paused": True, "owner": "backup-A", "ttl_s": 60},
        )
        assert r_next.status_code == 200 and r_next.json()["lease_id"] > lease_1
        r_next_res = await client.post(
            "/admin/wiki/pause-writes",
            headers=adm_hdr,
            json={"paused": False, "owner": "backup-A", "lease_id": r_next.json()["lease_id"]},
        )
        assert r_next_res.status_code == 200 and r_next_res.json()["continuous"] is True and r_next_res.json()["expired"] is False

        r_status = await client.get("/admin/wiki/pause-writes", headers=adm_hdr)
        assert r_status.status_code == 200 and r_status.json()["paused"] is False and r_status.json()["owner"] is None

        r_unblocked = await client.post(
            "/ingest/wiki",
            headers=adm_hdr,
            json={
                "request_id": "r8-after-expiry",
                "target_type": "company",
                "target": "NVDA",
                "content_type": "研报笔记",
                "date": "2026-11-26",
                "content": "Succeeds automatically once pause lease expires",
            },
        )
        assert r_unblocked.status_code == 200 and r_unblocked.json()["ok"] is True

    # 2. r8 #3: Core release package excludes deliver-to-feishu.mdx & everflow/ AND b3_skills_prompts_ui check passes
    pkg_out = tmp_path / "core_release_r8"
    pkg_res = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "scripts" / "standalone" / "package_release.sh"),
            "--repo",
            str(REPO_ROOT),
            "--ref",
            "HEAD",
            "--output",
            str(pkg_out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert pkg_res.returncode == 0, pkg_res.stderr
    assert not (pkg_out / "docs-site" / "src" / "content" / "docs" / "everflow").exists()
    assert not (pkg_out / "docs-site" / "src" / "content" / "docs" / "explore" / "deliver-to-feishu.mdx").exists()
    assert not (pkg_out / "docs-site" / "src" / "content" / "docs" / "explore" / "deliver-to-feishu.md").exists()

    b3_check_src = (REPO_ROOT / "tests" / "standalone" / "checks" / "b3_skills_prompts_ui.py").read_text(encoding="utf-8")
    assert "deliver-to-feishu.mdx" in b3_check_src
    assert "excluded < 5" not in b3_check_src

    # 3. r8 #4: PYTHON_DOTENV_DISABLED=1 respected in BOTH process env and explicit environ dict
    env_f = tmp_path / "disabled_check.env"
    env_f.write_text("DALTON_PROFILE=everflow\nDALTON_CAP_COMPANY_WIKI=false\n", encoding="utf-8")

    # 3a. Explicit dict with PYTHON_DOTENV_DISABLED=1
    explicit_disabled: dict[str, str] = {"PYTHON_DOTENV_DISABLED": "1"}
    assert load_shared_dotenv(env_f, environ=explicit_disabled) is None
    assert "DALTON_PROFILE" not in explicit_disabled

    # 3b. Process env with PYTHON_DOTENV_DISABLED=1 (isolated subprocess)
    sub_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("DALTON_", "APP_ENV", "PYTHON_DOTENV"))
    }
    sub_env["PYTHON_DOTENV_DISABLED"] = "1"
    sub_env["DALTON_DOTENV_PATH"] = str(env_f)
    sub_proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, json; import src.config; "
            "print(json.dumps({'profile': os.environ.get('DALTON_PROFILE'), 'wiki': os.environ.get('DALTON_CAP_COMPANY_WIKI')}))",
        ],
        cwd=str(REPO_ROOT),
        env=sub_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(sub_proc.stdout) == {"profile": None, "wiki": None}

    # 3c. Process env PYTHON_DOTENV_DISABLED=1 also disables explicit dict unless explicitly overridden to "0"
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    dict_no_key: dict[str, str] = {}
    assert load_shared_dotenv(env_f, environ=dict_no_key) is None
    assert "DALTON_PROFILE" not in dict_no_key

    dict_reenabled: dict[str, str] = {"PYTHON_DOTENV_DISABLED": "0"}
    assert load_shared_dotenv(env_f, environ=dict_reenabled) == env_f.resolve()
    assert dict_reenabled["DALTON_PROFILE"] == "everflow"

    # 4. r8 #5: pandas.NaT serializes to strict JSON null (not "NaT") in SEC yfinance fallback
    sec_mod = _load_dalton_sec_module()
    nat_df = pd.DataFrame(
        {
            pd.Timestamp("2026-06-30"): [95000000000.0, pd.NaT, pd.NA],
        },
        index=["Total Revenue", "Missing Date", "Missing NA"],
    )

    class _NaTTicker:
        def __init__(self, _t: str) -> None:
            self.quarterly_income_stmt = nat_df
            self.quarterly_balance_sheet = None
            self.quarterly_cashflow = None
            self.income_stmt = nat_df
            self.balance_sheet = None
            self.cashflow = None
            self.info = {"grossMargins": pd.NaT, "totalRevenue": 95000000000}

    fake_yf = type(sys)("yfinance")
    fake_yf.Ticker = _NaTTicker  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    yf_fin = sec_mod._yf_financials_fallback("NVDA", "10-Q")
    assert yf_fin is not None
    strict_text = json.dumps(yf_fin, allow_nan=False)
    assert '"NaT"' not in strict_text
    parsed = json.loads(strict_text)
    inc = parsed["rows"][0]["statements"]["income"]["2026-06-30T00:00:00"]
    assert inc["Missing Date"] is None
    assert inc["Missing NA"] is None
    assert inc["Total Revenue"] == 95000000000.0


# ---------------------------------------------------------------------------
# 11. Small Fix Pack 3: BZ1 (wiki image index tx), BZ2 (sales-notes vectors &
#     hybrid merge), BZ3 (discovered-tool routing fallbacks & clear errors)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bz1_wiki_image_index_rolls_back_with_document_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BZ1: When wiki ingest fails after document insert (before commit), image index & new asset file roll back too."""
    wiki = OpenWikiStore(tmp_path / "wiki_bz1")
    wiki._cli_checked = True
    wiki._cli_module = None
    wiki.ensure_schema()

    png_bytes = _make_minimal_png_bytes()

    # Case 1: DB failure on ingest_receipts AFTER documents/chunks/image insert -> images_db & asset file must roll back
    with sqlite3.connect(wiki.wiki_db) as conn:
        conn.execute(
            "CREATE TRIGGER fail_receipt_for_bz1 BEFORE INSERT ON ingest_receipts "
            "WHEN NEW.request_id = 'bz1-fail-receipt' "
            "BEGIN SELECT RAISE(ABORT, 'simulated receipt failure'); END;"
        )
        conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="simulated receipt failure"):
        await wiki.ingest(
            {
                "request_id": "bz1-fail-receipt",
                "target_type": "company",
                "target": "NVDA",
                "content_type": "研报笔记",
                "date": "2026-11-26",
                "file": {
                    "filename": "architecture_diagram.png",
                    "content_b64": base64.b64encode(png_bytes).decode("ascii"),
                },
            }
        )

    with sqlite3.connect(wiki.wiki_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    if wiki.images_db.is_file():
        with sqlite3.connect(wiki.images_db) as img_conn:
            assert img_conn.execute("SELECT COUNT(*) FROM refs").fetchone()[0] == 0
            assert img_conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0
    assets_dir = wiki.images_dir / "assets"
    if assets_dir.exists():
        assert [p for p in assets_dir.rglob("*") if p.is_file()] == []

    # Case 2: Disk write failure on original binary file (os.open) -> images_db & documents both roll back cleanly
    orig_os_open = os.open

    def _fail_original_open(path: Any, flags: int, *a: Any, **kw: Any) -> int:
        if "originals" in str(path):
            raise OSError("simulated disk full on original write")
        return orig_os_open(path, flags, *a, **kw)

    monkeypatch.setattr(os, "open", create_autospec(os.open, side_effect=_fail_original_open))
    with pytest.raises(OSError, match="simulated disk full on original write"):
        await wiki.ingest(
            {
                "request_id": "bz1-fail-orig",
                "target_type": "company",
                "target": "NVDA",
                "content_type": "研报笔记",
                "date": "2026-11-26",
                "file": {
                    "filename": "rack_layout.png",
                    "content_b64": base64.b64encode(png_bytes).decode("ascii"),
                },
            }
        )
    monkeypatch.undo()

    with sqlite3.connect(wiki.wiki_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    if wiki.images_db.is_file():
        with sqlite3.connect(wiki.images_db) as img_conn:
            assert img_conn.execute("SELECT COUNT(*) FROM refs").fetchone()[0] == 0
            assert img_conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0

    # Case 3: Subsequent valid image ingest commits both wiki_db and images_db atomically and is searchable/readable
    code_ok, res_ok = await wiki.ingest(
        {
            "request_id": "bz1-ok-1",
            "target_type": "company",
            "target": "NVDA",
            "content_type": "研报笔记",
            "date": "2026-11-26",
            "file": {
                "filename": "rack_layout.png",
                "content_b64": base64.b64encode(png_bytes).decode("ascii"),
            },
        }
    )
    assert code_ok == 200 and res_ok["ok"] is True
    assert str(res_ok.get("image_id") or "").startswith("image:")
    img_hits = wiki.query("search", query="rack_layout", params={"media": "images"})
    assert img_hits["ok"] is True and img_hits["result_count"] == 1
    assert img_hits["results"][0]["document_id"] == res_ok["image_id"]


@pytest.mark.asyncio
async def test_bz2_sales_notes_ingest_writes_embeddings_and_search_merges_keyword_and_semantic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BZ2: Sales-notes ingest persists chunk embeddings and default search merges keyword + semantic hits."""
    import numpy as np

    _, lib_mod = _load_commercial_connectors()
    store = lib_mod.MarketDigestPartitionStore(tmp_path / "sn_bz2")
    store.ensure_schema()

    # Deterministic 4-dim semantic space:
    # - AI/accelerator topic: [1, 0, 0, 0]
    # - Consumer retail topic: [0, 1, 0, 0]
    def _vec_for(text: str) -> list[float]:
        low = text.lower()
        if any(tok in low for tok in ("cowos", "packaging", "accelerator", "gpu", "rubin")):
            return [1.0, 0.0, 0.0, 0.0]
        if any(tok in low for tok in ("retail", "store", "apparel")):
            return [0.0, 1.0, 0.0, 0.0]
        return [0.0, 0.0, 1.0, 0.0]

    md_cli = store._library().module("sales-notes")
    spy_embed_texts = create_autospec(
        md_cli.embed_texts,
        side_effect=lambda texts, config: [_vec_for(t) for t in texts],
    )
    spy_embed_query = create_autospec(
        md_cli.embed_query,
        side_effect=lambda text, config: _vec_for(text),
    )
    monkeypatch.setattr(md_cli, "embed_texts", spy_embed_texts)
    monkeypatch.setattr(md_cli, "embed_query", spy_embed_query)
    # Reuse the patched module inside store._library() calls
    orig_lib = store._library

    def _patched_lib() -> Any:
        lib = orig_lib()
        lib._modules["market-digest"] = md_cli
        return lib

    monkeypatch.setattr(
        store,
        "_library",
        create_autospec(orig_lib, side_effect=_patched_lib),
    )

    # Seed one older unindexed note (NULL embedding, matches keyword 'accelerator' directly)
    # and one embedded unrelated retail note (to prove keyword hits aren't buried by embedded noise)
    retail_blob = np.asarray([0.0, 1.0, 0.0, 0.0], dtype=np.float32).tobytes()
    with sqlite3.connect(store.mail_db) as conn:
        conn.execute(
            "INSERT INTO email_chunks (date, email_id, message_id, sender, subject, chunk_index, content, embedding, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-11-20",
                "em-kw",
                "mail-kw-only",
                "GS",
                "Legacy Accelerator Note",
                0,
                "Legacy accelerator backlog note without vector",
                None,
                "2026-11-20T10:00:00Z",
            ),
        )
        conn.execute(
            "INSERT INTO email_chunks (date, email_id, message_id, sender, subject, chunk_index, content, embedding, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-11-21",
                "em-ret",
                "mail-retail",
                "MS",
                "Retail Foot Traffic",
                0,
                "Mall retail store apparel sales update",
                retail_blob,
                "2026-11-21T10:00:00Z",
            ),
        )
        conn.commit()

    # 1. Ingest a new sales note that uses "CoWoS packaging" (NO literal word "accelerator")
    code, res = await store.ingest_sales_note(
        {
            "request_id": "bz2-note-1",
            "institution": "Morgan Stanley",
            "title": "Taiwan Foundry Check",
            "date": "2026-11-25",
            "content": "CoWoS packaging ramp continues strongly into 2027.",
        }
    )
    assert code == 200 and res["ok"] is True
    assert spy_embed_texts.call_count == 1

    # Verify embedding BLOB was actually persisted in email_chunks
    with sqlite3.connect(store.mail_db) as conn:
        emb_row = conn.execute(
            "SELECT embedding FROM email_chunks WHERE message_id = ?",
            (res["doc_key"],),
        ).fetchone()
    assert emb_row is not None and emb_row[0] is not None
    stored_vec = np.frombuffer(emb_row[0], dtype=np.float32)
    assert stored_vec.shape == (4,)
    assert np.allclose(stored_vec, [1.0, 0.0, 0.0, 0.0])

    # 2. Semantic-only query ("GPU") has zero keyword match on the new note, yet default search MUST return it
    sales_conn = lib_mod.SalesNotesConnector(store)
    sem_only = await sales_conn.call_tool(
        "search",
        {"query": "GPU", "limit": 5},
        active_caps=frozenset({CapabilityId.SALES_NOTES}),
    )
    assert sem_only["ok"] is True
    assert any(r["original_document_id"] == res["doc_key"] for r in sem_only["results"])
    assert sem_only["results"][0]["original_document_id"] == res["doc_key"]

    # 3. Query matching keyword on unindexed note AND semantic on newly ingested note returns BOTH merged
    merged = await sales_conn.call_tool(
        "search",
        {"query": "accelerator", "limit": 2},
        active_caps=frozenset({CapabilityId.SALES_NOTES}),
    )
    assert merged["ok"] is True
    merged_keys = {r["original_document_id"] for r in merged["results"]}
    assert res["doc_key"] in merged_keys, f"Semantic hit missing from merged results: {merged['results']}"
    assert "mail-kw-only" in merged_keys, f"Keyword hit buried in merged results: {merged['results']}"

    # 4. Explicit search_mode="keyword" skips query embedding and returns only keyword matches
    prev_q_calls = spy_embed_query.call_count
    kw_only = await sales_conn.call_tool(
        "search",
        {"query": "GPU", "params": {"search_mode": "keyword"}},
        active_caps=frozenset({CapabilityId.SALES_NOTES}),
    )
    assert kw_only["ok"] is True
    assert kw_only["results"] == []
    assert spy_embed_query.call_count == prev_q_calls


@pytest.mark.asyncio
async def test_bz3_discovered_tool_selection_unimportable_wrapper_health_missing_tools_and_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BZ3: Handle unimportable wrapper in connector_transport, missing `tools` in health check,
    and discovered-tool awareness in content_assembly + topic-research/workflow.js."""
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
        WorkflowHost,
        WorkflowLimits,
        compile_check,
        run_workflow_script,
    )
    from src.dalton.data_map import health as dm_health

    ct_mod = _load_module_from_path(
        "_test_ct_bz3",
        REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-conventions" / "scripts" / "connector_transport.py",
    )

    # 1. connector_transport.py: unimportable wrapper + no `tools` metadata MUST fail closed (not guess True!)
    cfg_dedicated_only = tmp_path / "mcp_dedicated_only.json"
    cfg_dedicated_only.write_text(
        json.dumps({"servers": {"dalton_company_wiki": {"enabled": True}}}),
        encoding="utf-8",
    )
    monkeypatch.delenv("DALTON_RESEARCHDATA_URL", raising=False)
    monkeypatch.setenv("PTC_WORKSPACE_CONFIG", str(cfg_dedicated_only))
    monkeypatch.delitem(sys.modules, "tools.dalton_company_wiki", raising=False)
    monkeypatch.setitem(sys.modules, "tools", type(sys)("tools"))

    from ptc_agent.core.sandbox import mcp_client_runtime

    fake_mcp_call = create_autospec(
        mcp_client_runtime.call_mcp_tool_with_error_flag,
        side_effect=lambda server_name, tool_name, arguments: (
            False,
            {"ok": True, "server": server_name, "tool": tool_name, "args": arguments},
        ),
    )
    fake_legacy_call = create_autospec(
        mcp_client_runtime._call_mcp_tool,
        side_effect=lambda server_name, tool_name, arguments: {
            "ok": True,
            "server": server_name,
            "tool": tool_name,
            "args": arguments,
        },
    )

    fake_mcp_flagged = type(sys)("mcp_client")
    fake_mcp_flagged.call_mcp_tool_with_error_flag = fake_mcp_call  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mcp_client", fake_mcp_flagged)

    assert ct_mod.server_supports_tool(
        "dalton_company_wiki",
        "search",
        servers_cfg={"dalton_company_wiki": {"enabled": True}},
    ) is False
    tr_unverified = ct_mod.resolve_connector_transport("company-wiki", tool_name="search")
    assert tr_unverified["mode"] is None
    assert "dalton_company_wiki" in tr_unverified["error"]
    assert "tools" in tr_unverified["error"]

    # 1b. Mixed workspace where dedicated server passes config `tools`, mcp_client lacks flagged helper,
    #     and `tools.dalton_company_wiki` fails to import -> MUST fall back to `researchdata`!
    cfg_mixed_broken = tmp_path / "mcp_mixed_broken.json"
    cfg_mixed_broken.write_text(
        json.dumps(
            {
                "servers": {
                    "dalton_company_wiki": {
                        "enabled": True,
                        "tools": [{"name": "search", "inputSchema": {"properties": {"query": {}}}}],
                    },
                    "researchdata": {"enabled": True},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PTC_WORKSPACE_CONFIG", str(cfg_mixed_broken))
    fake_mcp_unflagged = type(sys)("mcp_client")
    monkeypatch.setitem(sys.modules, "mcp_client", fake_mcp_unflagged)
    fake_tools_pkg = type(sys)("tools")
    fake_rd = type(sys)("tools.researchdata")
    fake_rd._call_mcp_tool = fake_legacy_call  # type: ignore[attr-defined]
    fake_tools_pkg.researchdata = fake_rd  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tools", fake_tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.researchdata", fake_rd)
    monkeypatch.delitem(sys.modules, "tools.dalton_company_wiki", raising=False)

    tr_fb = ct_mod.resolve_connector_transport("company-wiki", tool_name="search")
    assert tr_fb["mode"] == "legacy", f"Expected fallback to legacy researchdata when wrapper unimportable: {tr_fb}"
    assert tr_fb["server"] == "researchdata"

    # 2. data_map/health.py: tools/list response missing `tools` MUST fail closed and never invoke tools/call
    _clean_profile_env(monkeypatch, profile="core")
    monkeypatch.setenv("DALTON_RESEARCH_HOST_URL", "http://research-host:8951")
    monkeypatch.setenv("DALTON_RESEARCH_HOST_TOKEN", "tok-bz3")
    seen_rpc_methods: list[str] = []

    def _missing_tools_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen_rpc_methods.append(str(body.get("method")))
        if body.get("method") == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 0, "result": {}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"ok": True}}})

    orig_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        create_autospec(
            httpx.AsyncClient,
            side_effect=lambda *a, **kw: orig_async_client(*a, **{**kw, "transport": httpx.MockTransport(_missing_tools_handler)}),
        ),
    )
    with pytest.raises(RuntimeError, match="tools"):
        await dm_health.default_call_research_host_connector("company-wiki", "list", {"limit": 1})
    assert seen_rpc_methods == ["tools/list"], f"tools/call must not run when tools/list omits `tools`: {seen_rpc_methods}"

    # 3. content_assembly.py: discovered_servers overrides deployment-only assumptions
    wf_path = REPO_ROOT / "workflows" / "topic-research" / "workflow.js"
    # 3a. Core with COMPANY_WIKI deployment cap, but workspace discovered NO internal research server
    wf_core_no_server = read_assembled_text(
        wf_path,
        capabilities=frozenset({CapabilityId.COMPANY_WIKI}),
        discovered_servers={"dalton_sec": {"enabled": True}},
    )
    assert "internal-memory.md" not in wf_core_no_server
    compile_check(wf_core_no_server)

    # 3b. Core with COMPANY_WIKI deployment cap, server present in config but `tools: []` (no usable tool)
    wf_core_empty_tools = read_assembled_text(
        wf_path,
        capabilities=frozenset({CapabilityId.COMPANY_WIKI}),
        discovered_servers={"dalton_company_wiki": {"enabled": True, "tools": []}},
    )
    assert "internal-memory.md" not in wf_core_empty_tools
    compile_check(wf_core_empty_tools)

    # 3c. Post-cutover Everflow (RESEARCHDATA_BRIDGE + COMPANY_WIKI + DALTON_RESEARCH_HOST_URL),
    #     workspace only discovered `researchdata` with `research` tool (no dedicated connector) -> keeps researchdata fallback!
    _clean_profile_env(monkeypatch, profile="everflow")
    monkeypatch.setenv("DALTON_RESEARCH_HOST_URL", "http://host.docker.internal:8951")
    wf_ef_fallback = read_assembled_text(
        wf_path,
        discovered_servers={"researchdata": {"enabled": True, "tools": ["research"]}},
    )
    assert "检索 researchdata 的 sales-notes、tmtb、funda-ai、company-wiki（只读）" in wf_ef_fallback
    compile_check(wf_ef_fallback)

    # 3d. Name + enabled WITHOUT `tools` is NOT usable (in content_assembly and in workflow.js)
    wf_core_name_only = read_assembled_text(
        wf_path,
        capabilities=frozenset({CapabilityId.COMPANY_WIKI}),
        discovered_servers={"dalton_company_wiki": {"enabled": True}},
    )
    assert "internal-memory.md" not in wf_core_name_only
    compile_check(wf_core_name_only)

    # 4. workflows/topic-research/workflow.js runtime: prebuilt Core workflow (assembled from deployment caps only)
    #    checks args.discovered_mcp_servers at runtime so missing connectors don't hard-block at line 265!
    wf_core_prebuilt = read_assembled_text(wf_path, capabilities=frozenset({CapabilityId.COMPANY_WIKI}))
    compile_check(wf_core_prebuilt)

    async def _run_topic(script_src: str, extra_args: dict[str, Any], *, fail_internal: bool = False) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        dispatches: list[tuple[str, str]] = []

        async def _agent(prompt: str, opts: dict[str, Any] | None = None) -> Any:
            label = str((opts or {}).get("label") or "")
            dispatches.append((label, prompt))
            if label == "规划":
                return {
                    "file": "./research/2026-11-26-topic-ai/plan.md",
                    "clusters": [
                        {"id": "a", "name": "簇 A", "queries": ["q1"]},
                        {"id": "b", "name": "簇 B", "queries": ["q2"]},
                        {"id": "c", "name": "簇 C", "queries": ["q3"]},
                    ],
                    "questions": ["Q1"],
                    "summary": "plan ok",
                }
            if label == "支线核验":
                paths = [
                    ln[2:].split("（")[0].strip()
                    for ln in prompt.splitlines()
                    if ln.startswith("- ") and "|" not in ln
                ]
                return {"files": [f"{p}|ok" for p in paths]}
            if label == "内部记忆" and fail_internal:
                return None
            if label == "产物核验":
                paths = [
                    ln[2:].strip()
                    for ln in prompt.splitlines()
                    if ln.startswith("- ") and "|" not in ln
                ]
                return {"files": [f"{p}|{'e' * 64}" for p in paths]}
            if (opts or {}).get("agentType") == "dalton-reviewer":
                return {
                    "verdict": "pass",
                    "summary": "ok",
                    "must_fix": [],
                    "items": [{"check": n, "result": "pass", "reason": "ok"} for n in range(1, 10)],
                }
            return {"files": ["./research/2026-11-26-topic-ai/out.md"], "summary": "done", "sha256": "a" * 64}

        host = create_autospec(WorkflowHost, instance=True)
        host.agent.side_effect = _agent
        host.phase.side_effect = lambda _t: None
        host.log.side_effect = lambda _m: None
        params = {
            "topic": "AI Capex",
            "slug": "ai",
            "workspace_dir": ".",
            "as_of": "2026-11-26",
            **extra_args,
        }
        outcome = await run_workflow_script(
            script_src,
            params,
            host,
            WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0),
        )
        assert outcome.status == "completed", outcome.error
        return outcome.result, dispatches

    # 4a. No internal research server discovered at runtime -> skips 内部记忆 branch & does NOT block at line 265
    res_skip, disp_skip = await _run_topic(
        wf_core_prebuilt,
        {"discovered_mcp_servers": {"dalton_sec": {"enabled": True}}},
    )
    assert res_skip["ok"] is True, res_skip
    assert not any(lbl == "内部记忆" for lbl, _ in disp_skip)

    # 4b. Direct connector missing, `dalton_research_all` discovered -> falls back to `dalton_research_all`
    res_rd, disp_rd = await _run_topic(
        wf_core_prebuilt,
        {"discovered_mcp_servers": {"dalton_research_all": {"enabled": True, "tools": [{"name": "search"}]}}},
    )
    assert res_rd["ok"] is True, res_rd
    internal_prompts = [p for lbl, p in disp_rd if lbl == "内部记忆"]
    assert internal_prompts and "dalton_research_all" in internal_prompts[0]

    # 4c. Dedicated server discovered with `search` tool, but subagent fails -> blocks cleanly at line 265
    res_blk, _ = await _run_topic(
        wf_core_prebuilt,
        {"discovered_mcp_servers": {"dalton_company_wiki": {"enabled": True, "tools": [{"name": "search"}]}}},
        fail_internal=True,
    )
    assert res_blk["ok"] is False
    assert "内部记忆检索" in str(res_blk.get("blocked") or "")

    # 4d. Direct server has only {enabled: true} (no tools), federated `dalton_research_all` has `search`
    #     -> direct server MUST NOT suppress federated fallback!
    res_name_only_fb, disp_name_only_fb = await _run_topic(
        wf_core_prebuilt,
        {
            "discovered_mcp_servers": {
                "dalton_company_wiki": {"enabled": True},
                "dalton_research_all": {"enabled": True, "tools": ["search"]},
            }
        },
    )
    assert res_name_only_fb["ok"] is True, res_name_only_fb
    fb_prompts = [p for lbl, p in disp_name_only_fb if lbl == "内部记忆"]
    assert fb_prompts and "dalton_research_all" in fb_prompts[0]


@pytest.mark.asyncio
async def test_r2_workflow_server_discovery_overwrites_caller_and_aligns_check_done(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2 items 2, 3 & 5:
    - Core default capabilities (`COMPANY_WIKI`) assemble `topic-research/workflow.js` without proprietary names.
    - `resolve_launch_discovered_mcp_servers` builds minimal tools strictly from server `mcp_registry`,
      overwrites caller `discovered_mcp_servers`/`mcp_servers`/`internal_memory_decision`, rejects
      tool-less servers, and explicitly falls back on discovery error.
    - `dalton_check_done` honors the same server-recorded decision from `args.json` (and ignores caller spoofing).
    """
    from ptc_agent.core.mcp_registry import MCPRegistry, MCPToolInfo
    from src.dalton.capabilities import effective_capabilities
    from src.dalton.content_assembly import (
        read_assembled_text,
        resolve_launch_discovered_mcp_servers,
    )
    from src.dalton.tools.check_done_tools import run_check_done

    _clean_profile_env(monkeypatch, profile="core")
    core_caps = effective_capabilities()
    assert core_caps == frozenset({CapabilityId.COMPANY_WIKI})

    # Item 5: Core default assembled workflow.js has zero proprietary connector names
    wf_js = read_assembled_text(
        REPO_ROOT / "workflows" / "topic-research" / "workflow.js",
        capabilities=core_caps,
    )
    assert "dalton_company_wiki" in wf_js
    for banned in ("dalton_tmtb", "tmtb", "dalton_sales_notes", "dalton_funda_ai", "sales_mail"):
        assert banned not in wf_js.lower(), f"Core default workflow.js leaked {banned!r}"

    # Item 2a: Server discovery overwrites caller-supplied discovered_mcp_servers / mcp_servers / decision
    reg_empty = create_autospec(MCPRegistry, instance=True)
    reg_empty.config = None
    reg_empty.get_all_tools.return_value = {
        "dalton_company_wiki": [],  # enabled name with zero tools -> NOT usable
        "dalton_sec": [MCPToolInfo(name="search_filings", description="sec", input_schema={}, server_name="dalton_sec")],
    }
    spoofed_params = {
        "topic": "AI Capex",
        "slug": "ai",
        "workspace_dir": ".",
        "as_of": "2026-11-26",
        "discovered_mcp_servers": {"dalton_company_wiki": {"enabled": True, "tools": ["search"]}},
        "mcp_servers": {"dalton_company_wiki": {"enabled": True, "tools": ["search"]}},
        "internal_memory_decision": {"run": True, "mode": "direct"},
    }
    resolved_skip = await resolve_launch_discovered_mcp_servers(
        None, "topic-research", spoofed_params, mcp_registry=reg_empty
    )
    assert "mcp_servers" not in resolved_skip
    assert "dalton_company_wiki" not in resolved_skip["discovered_mcp_servers"]
    assert resolved_skip["discovered_mcp_servers"] == {"dalton_sec": {"enabled": True, "tools": ["search_filings"]}}
    assert resolved_skip["internal_memory_decision"] == {
        "run": False,
        "mode": "skip",
        "discovery_status": "ok",
    }

    # Item 2b: Discovery exception -> explicit fallback (empty servers, skip internal memory, status="error")
    reg_err = create_autospec(MCPRegistry, instance=True)
    reg_err.config = None
    reg_err.get_all_tools.side_effect = RuntimeError("registry broken")
    resolved_err = await resolve_launch_discovered_mcp_servers(
        None, "topic-research", spoofed_params, mcp_registry=reg_err
    )
    assert resolved_err["discovered_mcp_servers"] == {}
    assert resolved_err["internal_memory_decision"] == {
        "run": False,
        "mode": "skip",
        "discovery_status": "error",
    }

    # Item 2c: Direct connector empty, federated connector usable -> explicit federated fallback
    reg_fed = create_autospec(MCPRegistry, instance=True)
    reg_fed.config = None
    reg_fed.get_all_tools.return_value = {
        "dalton_company_wiki": [],
        "dalton_research_all": [MCPToolInfo(name="search", description="s", input_schema={}, server_name="dalton_research_all")],
    }
    resolved_fed = await resolve_launch_discovered_mcp_servers(
        None, "topic-research", {"topic": "AI", "slug": "ai", "workspace_dir": "."}, mcp_registry=reg_fed
    )
    assert resolved_fed["internal_memory_decision"] == {
        "run": True,
        "mode": "federated",
        "discovery_status": "ok",
    }

    # Item 3: dalton_check_done uses the SAME server-signed decision from args.json
    # Build a minimal backend exposing files for topic-research WITHOUT internal-memory.md
    from ptc_agent.agent.backends.sandbox import SandboxBackend
    from src.dalton.content_assembly import sign_topic_research_internal_memory_decision

    signed_skip = sign_topic_research_internal_memory_decision(
        resolved_skip,
        short_thread_id="th123456",
        run_id="wf-skip",
    )
    signed_fed = sign_topic_research_internal_memory_decision(
        resolved_fed,
        short_thread_id="th123456",
        run_id="wf-run",
    )
    files_map: dict[str, str] = {
        ".agents/threads/th123456/workflows/wf-skip/args.json": json.dumps(signed_skip, ensure_ascii=False),
        ".agents/threads/th123456/workflows/wf-skip/status.json": json.dumps(
            {"run_id": "wf-skip", "name": "topic-research", "status": "completed", "updated_at": 1.0}
        ),
        ".agents/threads/th123456/workflows/wf-run/args.json": json.dumps(signed_fed, ensure_ascii=False),
        ".agents/threads/th123456/workflows/wf-run/status.json": json.dumps(
            {"run_id": "wf-run", "name": "topic-research", "status": "completed", "updated_at": 2.0}
        ),
        "research/2026-11-26-topic-ai/plan.md": "p" * 120,
        "research/2026-11-26-topic-ai/primary.md": "p" * 120,
        "research/2026-11-26-topic-ai/cluster-a.md": "c" * 120,
        "research/2026-11-26-topic-ai/cluster-b.md": "c" * 120,
        "research/2026-11-26-topic-ai/cluster-c.md": "c" * 120,
        "research/2026-11-26-topic-ai/cross-check.md": ("x" * 120) + "\n盲区检查完成\n",
        "research/2026-11-26-topic-ai/number-audit.md": "n" * 80,
        "research/2026-11-26-topic-ai/knowledge-revision.json": json.dumps({"ok": True, "pad": "x" * 50}),
        "drafts/2026-11-26-topic-ai.md": "d" * 600,
    }
    backend = create_autospec(SandboxBackend, instance=True)
    backend.workspace_dir = None

    async def _fake_aread(path: str) -> str | None:
        norm = path[2:] if path.startswith("./") else path.lstrip("/")
        return files_map.get(norm)

    async def _fake_aglob(pattern: str, _root: str = ".") -> list[str]:
        import fnmatch

        return sorted(k for k in files_map if fnmatch.fnmatch(k, pattern))

    backend.aread_text.side_effect = _fake_aread
    backend.aglob_paths.side_effect = _fake_aglob
    backend.aexecute = None

    check_skip = await run_check_done(
        workflow="topic-research",
        params={"topic": "AI Capex", "slug": "ai", "workspace_dir": ".", "as_of": "2026-11-26"},
        backend=backend,
        workspace_id=None,
        run_id="wf-skip",
        thread_id="th123456-full",
    )
    assert not any("internal-memory.md" in m for m in check_skip["missing"]), check_skip["missing"]

    # Negative: when args.json recorded run=True, caller CANNOT spoof params to skip internal-memory.md
    check_run = await run_check_done(
        workflow="topic-research",
        params={
            "topic": "AI Capex",
            "slug": "ai",
            "workspace_dir": ".",
            "as_of": "2026-11-26",
            "internal_memory_decision": {"run": False, "mode": "skip"},
        },
        backend=backend,
        workspace_id=None,
        run_id="wf-run",
        thread_id="th123456-full",
    )
    assert any("internal-memory.md" in m for m in check_run["missing"]), check_run["missing"]


@pytest.mark.asyncio
async def test_r2_sales_notes_streamed_search_tmtb_keyword_default_and_embedding_retry_backfill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2 items 4 & 7:
    - TMTB keeps default `keyword` search (never invokes query embedding unless explicitly requested).
    - Sales-notes hybrid search avoids `SELECT *` and never loads full-table embedding BLOBs on keyword pass;
      semantic pass only selects `(id, doc_key, embedding)` filtered to `embedding IS NOT NULL`.
    - Embedding calls never use global `contextlib.redirect_stdout`.
    - Transient embedding failure during ingest marks `embedding_pending=True`; retrying with same `request_id`
      after recovery backfills `email_chunks.embedding` so semantic search immediately finds the note.
    """
    import numpy as np

    _, lib_mod = _load_commercial_connectors()
    store = lib_mod.MarketDigestPartitionStore(tmp_path / "sn_r2")
    store.ensure_schema()

    def _vec(text: str) -> list[float]:
        low = text.lower()
        if any(k in low for k in ("cowos", "gpu", "accelerator")):
            return [1.0, 0.0, 0.0, 0.0]
        return [0.0, 1.0, 0.0, 0.0]

    md_cli = store._library().module("sales-notes")
    fail_once = {"fail": True}

    def _flaky_embed_texts(texts: list[str], config: dict[str, Any]) -> list[list[float]]:
        if fail_once["fail"]:
            fail_once["fail"] = False
            raise RuntimeError("transient 503 embedding outage")
        return [_vec(t) for t in texts]

    spy_texts = create_autospec(md_cli.embed_texts, side_effect=_flaky_embed_texts)
    spy_query = create_autospec(md_cli.embed_query, side_effect=lambda q, c: _vec(q))
    monkeypatch.setattr(md_cli, "embed_texts", spy_texts)
    monkeypatch.setattr(md_cli, "embed_query", spy_query)
    orig_lib = store._library

    def _patched_lib() -> Any:
        lib = orig_lib()
        lib._modules["market-digest"] = md_cli
        return lib

    monkeypatch.setattr(
        store,
        "_library",
        create_autospec(orig_lib, side_effect=_patched_lib),
    )

    # Seed one TMTB row + one unindexed sales-note row
    with sqlite3.connect(store.mail_db) as conn:
        conn.execute(
            "INSERT INTO email_chunks (date, email_id, message_id, sender, subject, chunk_index, content, embedding, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-11-20", "ae-1", "alphaengine:doc1", "TMTB", "Cloud Capex", 0, "Hyperscaler capex rising", None, "2026-11-20T00:00:00Z"),
        )
        conn.commit()

    # 1. TMTB search defaults to keyword -> zero embed_query calls!
    tmtb_conn = lib_mod.TmtbConnector(store)
    tm_res = await tmtb_conn.call_tool(
        "search",
        {"query": "Hyperscaler", "limit": 5},
        active_caps=frozenset({CapabilityId.TMTB}),
    )
    assert tm_res["ok"] is True and len(tm_res["results"]) == 1
    assert spy_query.call_count == 0, "TMTB default search must stay keyword and not invoke embed_query"

    # 2. Item 7: Attempt 1 embedding fails -> records embedding_pending=True, NOT permanently lost!
    note_payload = {
        "request_id": "r2-emb-retry-1",
        "institution": "Goldman Sachs",
        "title": "Foundry Supply Check",
        "date": "2026-11-25",
        "content": "CoWoS wafer capacity expanding sharply.",
    }
    code1, res1 = await store.ingest_sales_note(note_payload)
    assert code1 == 200 and res1["ok"] is True
    assert res1["embedding_indexed"] is False
    assert res1["embedding_pending"] is True

    # Retry with same request_id once embedding recovers -> backfills vectors!
    code2, res2 = await store.ingest_sales_note(note_payload)
    assert code2 == 200 and res2["ok"] is True
    assert res2["idempotent_replay"] is True
    assert res2["embedding_indexed"] is True
    assert res2["embedding_pending"] is False
    assert res2.get("embedding_backfilled") is True

    # Verify DB now holds the embedding and semantic-only query ("GPU") finds the note!
    with sqlite3.connect(store.mail_db) as conn:
        emb = conn.execute("SELECT embedding FROM email_chunks WHERE message_id = ?", (res2["doc_key"],)).fetchone()[0]
    assert emb is not None and np.allclose(np.frombuffer(emb, dtype=np.float32), [1.0, 0.0, 0.0, 0.0])

    # 3. Item 4: Trace SQL executed during sales-notes hybrid search -> zero `SELECT *`,
    #    semantic query selects only minimal columns with `embedding IS NOT NULL`!
    traced_sql: list[str] = []
    orig_readonly = lib_mod.research_library_mod.readonly

    import contextlib as _cl

    @_cl.contextmanager
    def _tracing_readonly(path: Path) -> Any:
        with orig_readonly(path) as c:
            c.set_trace_callback(traced_sql.append)
            yield c

    monkeypatch.setattr(
        lib_mod.research_library_mod,
        "readonly",
        create_autospec(orig_readonly, side_effect=_tracing_readonly),
    )
    sales_conn = lib_mod.SalesNotesConnector(store)
    sem_hit = await sales_conn.call_tool(
        "search",
        {"query": "GPU", "limit": 5},
        active_caps=frozenset({CapabilityId.SALES_NOTES}),
    )
    assert sem_hit["ok"] is True
    assert any(r["original_document_id"] == res2["doc_key"] for r in sem_hit["results"])
    assert traced_sql, "Expected SQL trace statements"
    assert not any("SELECT *" in q.upper() for q in traced_sql), f"Must never run SELECT *: {traced_sql}"
    emb_queries = [q for q in traced_sql if "embedding" in q.lower() and "count(" not in q.lower()]
    assert emb_queries, f"Expected semantic embedding query in {traced_sql}"
    for q in emb_queries:
        assert "embedding is not null" in q.lower(), f"Semantic query must filter non-NULL embeddings: {q}"
        assert "content" not in q.lower(), f"Semantic vector scan must not fetch content column: {q}"


@pytest.mark.asyncio
async def test_r2_shared_image_asset_rollback_preserves_concurrently_referenced_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2 item 6: Exclusive image asset creation + images_db write-lock cleanup on rollback.

    When Wiki creates a shared image file and rolls back while Sales Notes concurrently commits
    a `refs` row pointing to that same image SHA, Wiki's rollback MUST NOT delete the file.
    When zero `refs` remain, rollback cleans up the newly created file.
    """
    from src.dalton.research_host.wiki_store import OpenWikiStore

    _, lib_mod = _load_commercial_connectors()
    data_root = tmp_path / "shared_img_r2"
    wiki = OpenWikiStore(data_root)
    sn_store = lib_mod.MarketDigestPartitionStore(data_root)
    wiki.ensure_schema()
    sn_store.ensure_schema()

    png_bytes = _make_minimal_png_bytes()
    _, _, _, sha, _, asset_abs = wiki._prepare_image_asset(image_bytes=png_bytes, suffix=".png")

    # 1. Exclusive creation: first writer returns True, second returns False
    assert wiki._persist_image_asset_file(asset_abs, png_bytes) is True
    assert wiki._persist_image_asset_file(asset_abs, png_bytes) is False
    asset_abs.unlink()

    # 2. Simulate Wiki rollback after Sales Notes concurrently commits a reference to the same SHA:
    #    Inject failure when Wiki writes the final markdown file (after asset_abs was created),
    #    and hook `_cleanup_unreferenced_image_asset` to first let Sales Notes commit a ref for `sha`!
    orig_cleanup = wiki._cleanup_unreferenced_image_asset
    cleanup_results: list[bool] = []

    def _race_cleanup(path_arg: Path, sha_arg: str) -> bool:
        # Simulate Sales Notes committing its reference between Wiki's ROLLBACK and Wiki's cleanup
        wiki.register_image_asset(
            image_bytes=png_bytes,
            suffix=".png",
            source="sales-notes",
            doc_key="sn-concurrent-1",
            title="Concurrent Sales Note Chart",
            doc_date="2026-11-25",
            metadata={"caption": "concurrent"},
            created_at="2026-11-25T10:00:00Z",
            write_asset_file=False,
        )
        deleted = orig_cleanup(path_arg, sha_arg)
        cleanup_results.append(deleted)
        return deleted

    monkeypatch.setattr(
        wiki,
        "_cleanup_unreferenced_image_asset",
        create_autospec(orig_cleanup, side_effect=_race_cleanup),
    )

    real_os_open = os.open

    def _fail_md_open(path: Any, flags: int, mode: int = 0o777) -> int:
        if str(path).endswith(".md"):
            raise OSError("simulated markdown disk failure")
        return real_os_open(path, flags, mode)

    fail_open_spec = create_autospec(real_os_open, side_effect=_fail_md_open)
    monkeypatch.setattr(os, "open", fail_open_spec)
    with pytest.raises(OSError, match="simulated markdown disk failure"):
        await wiki.ingest(
            {
                "request_id": "wiki-race-1",
                "target_type": "company",
                "target": "NVDA",
                "content_type": "memo",
                "date": "2026-11-25",
                "file": {
                    "filename": "shared_chart.png",
                    "content_b64": base64.b64encode(png_bytes).decode("ascii"),
                },
            }
        )
    monkeypatch.setattr(os, "open", real_os_open)

    assert cleanup_results == [False], "Rollback must NOT unlink asset still referenced by Sales Notes"
    assert asset_abs.is_file(), "Shared image file referenced by Sales Notes was erroneously deleted!"

    # 3. Contrast: unreferenced image asset IS cleaned up on rollback
    png_orphan = _make_minimal_png_bytes() + b"\x00orphan"
    _, _, _, _, _, orphan_abs = wiki._prepare_image_asset(image_bytes=png_orphan, suffix=".png")
    monkeypatch.setattr(wiki, "_cleanup_unreferenced_image_asset", orig_cleanup)
    monkeypatch.setattr(os, "open", fail_open_spec)
    with pytest.raises(OSError, match="simulated markdown disk failure"):
        await wiki.ingest(
            {
                "request_id": "wiki-orphan-1",
                "target_type": "company",
                "target": "NVDA",
                "content_type": "memo",
                "date": "2026-11-25",
                "file": {
                    "filename": "orphan_chart.png",
                    "content_b64": base64.b64encode(png_orphan).decode("ascii"),
                },
            }
        )
    monkeypatch.setattr(os, "open", real_os_open)
    assert not orphan_abs.exists(), "Unreferenced newly created image asset should be removed on rollback"


@pytest.mark.asyncio
async def test_r3_internal_memory_decision_forged_and_snapshot_write_failure_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 item 1:
    - Everflow legacy bridge enabled: caller-supplied `internal_memory_decision` /
      `discovered_mcp_servers` / `mcp_servers` are stripped, and `dalton_check_done`
      still requires `internal-memory.md` even if `{run: false}` appears in `args.json`.
    - Core (bridge disabled): unsigned, tampered, or cross-run replayed `{run: false}`
      in `args.json` is rejected and `internal-memory.md` is still required; deleting
      `internal_memory_decision` and spoofing `discovered_mcp_servers: {}` is also rejected.
    - `RunWorkflow` refuses launch (aborting the admitted run without spawning driver)
      when `args.json` snapshot write fails (exception OR returning False).
    """
    from ptc_agent.agent.backends.sandbox import SandboxBackend
    from ptc_agent.agent.middleware.background_subagent.dispatch import SubagentDispatcher
    from ptc_agent.agent.middleware.background_subagent.middleware import (
        BackgroundSubagentMiddleware,
    )
    from ptc_agent.agent.middleware.background_subagent.registry import (
        BackgroundTask,
        BackgroundTaskRegistry,
    )
    from ptc_agent.agent.middleware.background_subagent.workflow import tool as wf_tool_mod
    from ptc_agent.core.mcp_registry import MCPRegistry
    from src.config.models import WorkflowOrchestrationConfig
    from src.dalton.content_assembly import (
        resolve_launch_discovered_mcp_servers,
        sign_topic_research_internal_memory_decision,
    )
    from src.dalton.tools.check_done_tools import run_check_done

    # 1. Everflow (legacy researchdata_bridge enabled): strip spoofed decision + require internal-memory.md
    monkeypatch.setenv("DALTON_PROFILE", "everflow")
    monkeypatch.delenv("DALTON_CAPABILITIES", raising=False)
    monkeypatch.delenv("DALTON_CAPABILITIES_DISABLE", raising=False)

    spoofed_everflow = {
        "topic": "AI Capex",
        "slug": "ai",
        "workspace_dir": ".",
        "as_of": "2026-11-26",
        "internal_memory_decision": {"run": False, "mode": "skip"},
        "discovered_mcp_servers": {},
        "mcp_servers": {},
    }
    cleaned_ef = await resolve_launch_discovered_mcp_servers(
        None, "topic-research", spoofed_everflow, mcp_registry=None
    )
    assert "internal_memory_decision" not in cleaned_ef
    assert "discovered_mcp_servers" not in cleaned_ef
    assert "mcp_servers" not in cleaned_ef

    base_files: dict[str, str] = {
        "research/2026-11-26-topic-ai/plan.md": "p" * 120,
        "research/2026-11-26-topic-ai/primary.md": "p" * 120,
        "research/2026-11-26-topic-ai/cluster-a.md": "c" * 120,
        "research/2026-11-26-topic-ai/cluster-b.md": "c" * 120,
        "research/2026-11-26-topic-ai/cluster-c.md": "c" * 120,
        "research/2026-11-26-topic-ai/cross-check.md": ("x" * 120) + "\n盲区检查完成\n",
        "research/2026-11-26-topic-ai/number-audit.md": "n" * 80,
        "research/2026-11-26-topic-ai/knowledge-revision.json": json.dumps({"ok": True, "pad": "x" * 50}),
        "drafts/2026-11-26-topic-ai.md": "d" * 600,
    }

    def _make_backend(extra_files: dict[str, str]) -> Any:
        files_map = {**base_files, **extra_files}
        be = create_autospec(SandboxBackend, instance=True)
        be.workspace_dir = None

        async def _aread(path: str) -> str | None:
            norm = path[2:] if path.startswith("./") else path.lstrip("/")
            return files_map.get(norm)

        async def _aglob(pattern: str, _root: str = ".") -> list[str]:
            import fnmatch

            return sorted(k for k in files_map if fnmatch.fnmatch(k, pattern))

        be.aread_text.side_effect = _aread
        be.aglob_paths.side_effect = _aglob
        be.aexecute = None
        return be

    # Even if args.json on Everflow holds forged `{run: false}`, internal-memory.md is still required
    ef_forged_signed = sign_topic_research_internal_memory_decision(
        {"internal_memory_decision": {"run": False, "mode": "skip", "discovery_status": "ok"}},
        short_thread_id="ef123456",
        run_id="wf-ef",
    )
    be_ef = _make_backend(
        {".agents/threads/ef123456/workflows/wf-ef/args.json": json.dumps(ef_forged_signed)}
    )
    res_ef = await run_check_done(
        workflow="topic-research",
        params={"topic": "AI Capex", "slug": "ai", "workspace_dir": ".", "as_of": "2026-11-26"},
        backend=be_ef,
        workspace_id=None,
        run_id="wf-ef",
        thread_id="ef123456-full",
    )
    assert any("internal-memory.md" in m for m in res_ef["missing"]), res_ef["missing"]

    # 2. Core (bridge disabled): unsigned, tampered, cross-run replayed, or stripped-decision args.json MUST NOT skip
    monkeypatch.setenv("DALTON_PROFILE", "core")
    monkeypatch.delenv("DALTON_CAPABILITIES", raising=False)
    monkeypatch.delenv("DALTON_CAPABILITIES_DISABLE", raising=False)

    valid_other_run = sign_topic_research_internal_memory_decision(
        {"internal_memory_decision": {"run": False, "mode": "skip", "discovery_status": "ok"}},
        short_thread_id="co123456",
        run_id="wf-other",
    )
    tampered_run = sign_topic_research_internal_memory_decision(
        {"internal_memory_decision": {"run": True, "mode": "direct", "discovery_status": "ok"}},
        short_thread_id="co123456",
        run_id="wf-tampered",
    )
    tampered_run["internal_memory_decision"]["run"] = False

    be_core_forged = _make_backend(
        {
            ".agents/threads/co123456/workflows/wf-unsigned/args.json": json.dumps(
                {"internal_memory_decision": {"run": False, "mode": "skip"}}
            ),
            ".agents/threads/co123456/workflows/wf-replayed/args.json": json.dumps(
                valid_other_run
            ),
            ".agents/threads/co123456/workflows/wf-tampered/args.json": json.dumps(
                tampered_run
            ),
            ".agents/threads/co123456/workflows/wf-no-decision/args.json": json.dumps(
                {"discovered_mcp_servers": {}}
            ),
        }
    )
    for forged_run_id in ("wf-unsigned", "wf-replayed", "wf-tampered", "wf-no-decision"):
        res_forged = await run_check_done(
            workflow="topic-research",
            params={"topic": "AI Capex", "slug": "ai", "workspace_dir": ".", "as_of": "2026-11-26"},
            backend=be_core_forged,
            workspace_id=None,
            run_id=forged_run_id,
            thread_id="co123456-full",
        )
        assert any("internal-memory.md" in m for m in res_forged["missing"]), (
            f"Forged run {forged_run_id} bypassed internal-memory.md check: {res_forged['missing']}"
        )

    # 3. Snapshot write failure (exception AND False return) refuses workflow launch & aborts admitted run
    reg_empty = create_autospec(MCPRegistry, instance=True)
    reg_empty.config = None
    reg_empty.get_all_tools.return_value = {}

    minimal_topic_script = (
        "const meta = { name: 'topic-research', description: 'topic' };\n"
        "async function run() { return { ok: true }; }\n"
    )
    real_spawn_task_writer = wf_tool_mod.spawn_task_writer

    async def _attempt_launch(write_behavior: Any) -> tuple[Any, Any, Any, dict[str, str]]:
        written: dict[str, str] = {}
        be_launch = create_autospec(SandboxBackend, instance=True)
        be_launch.workspace_dir = None

        async def _awrite(path: str, content: str) -> bool:
            if path.endswith("/args.json"):
                if isinstance(write_behavior, Exception):
                    raise write_behavior
                if write_behavior is False:
                    return False
            written[path] = content
            return True

        be_launch.awrite_text.side_effect = _awrite

        mw = create_autospec(BackgroundSubagentMiddleware, instance=True)
        mw.workflow_config = WorkflowOrchestrationConfig()
        mw.checkpointer = None
        mw.admit_task_run.return_value = "admitted-row-1"
        mw._abort_admitted_run.return_value = None

        task_reg = create_autospec(BackgroundTaskRegistry, instance=True)
        task_reg.run_ledger = None
        task_reg.get_all_tasks.return_value = []
        task_reg.get_by_tool_call_id.return_value = None
        run_task = BackgroundTask(
            task_id="wf-snap-1",
            description="topic",
            prompt="p",
            subagent_type="workflow",
            tool_call_id="tc-snap-1",
        )
        task_reg.register.return_value = run_task
        task_reg.discard_unstarted.return_value = None

        dispatcher = create_autospec(SubagentDispatcher, instance=True)
        dispatcher.middleware = mw
        dispatcher.registry = task_reg

        spawn_spy = create_autospec(real_spawn_task_writer, return_value=None)
        monkeypatch.setattr(wf_tool_mod, "spawn_task_writer", spawn_spy)

        wf_tool = wf_tool_mod.create_run_workflow_tool(
            dispatcher=dispatcher,
            backend=be_launch,
            thread_id="co123456-full",
            short_thread_id="co123456",
            mcp_registry=reg_empty,
        )
        reply = await wf_tool.ainvoke(
            {
                "name": "RunWorkflow",
                "id": "tc-snap-1",
                "type": "tool_call",
                "args": {
                    "script": minimal_topic_script,
                    "params": {
                        "topic": "AI Capex",
                        "slug": "ai",
                        "subject_kind": "theme",
                        "workspace_dir": ".",
                        "as_of": "2026-11-26",
                        "internal_memory_decision": {"run": True, "mode": "direct"},
                    },
                },
            }
        )
        return reply, mw, spawn_spy, written

    # 3a. awrite_text raises OSError on args.json -> refused + admitted run aborted + driver never spawned
    reply_exc, mw_exc, spawn_exc, _ = await _attempt_launch(OSError("ENOSPC"))
    assert "failed to snapshot launch args" in str(reply_exc).lower()
    assert mw_exc._abort_admitted_run.await_count == 1
    assert spawn_exc.await_count == 0

    # 3b. awrite_text returns False on args.json -> refused + admitted run aborted + driver never spawned
    reply_false, mw_false, spawn_false, _ = await _attempt_launch(False)
    assert "failed to snapshot launch args" in str(reply_false).lower()
    assert mw_false._abort_admitted_run.await_count == 1
    assert spawn_false.await_count == 0

    # 3c. Positive control: awrite_text succeeds -> signed args.json written and accepted by run_check_done
    reply_ok, mw_ok, spawn_ok, written_ok = await _attempt_launch(True)
    assert "Workflow run started" in str(reply_ok)
    assert mw_ok._abort_admitted_run.await_count == 0
    assert spawn_ok.await_count == 1
    args_path = ".agents/threads/co123456/workflows/wf-snap-1/args.json"
    assert args_path in written_ok
    saved_args = json.loads(written_ok[args_path])
    assert saved_args["internal_memory_decision"]["run"] is False
    assert saved_args["internal_memory_decision"]["signature"]
    be_verified = _make_backend(
        {
            args_path: written_ok[args_path],
            ".agents/threads/co123456/workflows/wf-snap-1/status.json": json.dumps(
                {"run_id": "wf-snap-1", "name": "topic-research", "status": "completed", "updated_at": 1.0}
            ),
        }
    )
    res_verified = await run_check_done(
        workflow="topic-research",
        params={"topic": "AI Capex", "slug": "ai", "workspace_dir": ".", "as_of": "2026-11-26"},
        backend=be_verified,
        workspace_id=None,
        run_id="wf-snap-1",
        thread_id="co123456-full",
    )
    assert not any("internal-memory.md" in m for m in res_verified["missing"]), res_verified["missing"]


@pytest.mark.asyncio
async def test_r3_sales_notes_embedding_retry_pending_not_duplicate_and_legacy_partial_backfill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 item 2:
    - Retrying without `request_id` while embeddings still fail returns a retryable
      `embedding_pending=True` response (NEVER `status="duplicate"`).
    - Once embeddings recover, retry backfills missing vectors and subsequent retry
      returns `duplicate`.
    - Legacy r1 partial-success receipts (`embedding_indexed=True`, no `embedding_pending`,
      one chunk embedded and one chunk NULL) still check actual NULL chunks on retry and
      backfill only the missing chunks.
    """
    import re
    import numpy as np

    _, lib_mod = _load_commercial_connectors()
    store = lib_mod.MarketDigestPartitionStore(tmp_path / "sn_r3_retry")
    store.ensure_schema()

    def _vec(text: str) -> list[float]:
        return [1.0, 0.0, 0.0, 0.0] if "cowos" in text.lower() else [0.0, 1.0, 0.0, 0.0]

    md_cli = store._library().module("sales-notes")
    embed_state = {"fail": True}
    seen_embed_batches: list[list[str]] = []

    def _controlled_embed(texts: list[str], config: dict[str, Any]) -> list[list[float]]:
        seen_embed_batches.append(list(texts))
        if embed_state["fail"]:
            raise RuntimeError("embedding provider down")
        return [_vec(t) for t in texts]

    monkeypatch.setattr(
        md_cli,
        "embed_texts",
        create_autospec(md_cli.embed_texts, side_effect=_controlled_embed),
    )
    orig_lib = store._library

    def _patched_lib() -> Any:
        lib = orig_lib()
        lib._modules["market-digest"] = md_cli
        return lib

    monkeypatch.setattr(store, "_library", create_autospec(orig_lib, side_effect=_patched_lib))

    # 1. No request_id + repeated embedding failure stays retryable (never falls through to duplicate)
    no_req_payload = {
        "institution": "Morgan Stanley",
        "title": "Packaging Capacity Check",
        "date": "2026-11-26",
        "content": "CoWoS supply remains tight across 2026.",
    }
    code1, res1 = await store.ingest_sales_note(no_req_payload)
    assert code1 == 200 and res1["ok"] is True
    assert res1["embedding_pending"] is True and res1["embedding_indexed"] is False

    # Retry while still failing (NO request_id) -> retryable, NOT duplicate!
    code2, res2 = await store.ingest_sales_note(no_req_payload)
    assert code2 == 200 and res2["ok"] is True
    assert res2["status"] != "duplicate"
    assert res2["embedding_pending"] is True
    assert res2.get("retryable") is True

    # Recover embedding -> retry backfills NULL chunk
    embed_state["fail"] = False
    code3, res3 = await store.ingest_sales_note(no_req_payload)
    assert code3 == 200 and res3["ok"] is True
    assert res3["embedding_indexed"] is True
    assert res3["embedding_pending"] is False
    assert res3.get("embedding_backfilled") is True

    # Subsequent retry after all chunks are indexed -> duplicate
    code4, res4 = await store.ingest_sales_note(no_req_payload)
    assert code4 == 200 and res4["ok"] is False and res4["status"] == "duplicate"

    # 2. Legacy r1 partial-success receipt (embedding_indexed=True, no embedding_pending, chunk 1 NULL)
    legacy_body = "CoWoS chunk zero content.\n\n" + ("Second chunk detail. " * 90)
    legacy_payload = {
        "institution": "UBS",
        "title": "Legacy Partial Note",
        "date": "2026-11-26",
        "content": legacy_body,
    }
    digest_bytes = re.sub(r"\s+", " ", legacy_body).strip().encode("utf-8")
    legacy_doc_key = lib_mod.sales_notes_store_mod.content_key(digest_bytes)
    chunks = store._chunk_sales_note(legacy_body, "[UBS] Legacy Partial Note\n\n", max_chunk_chars=1500)
    assert len(chunks) >= 2
    emb0 = np.asarray([1.0, 0.0, 0.0, 0.0], dtype=np.float32).tobytes()
    legacy_manifest_entry = {
        "doc_key": legacy_doc_key,
        "institution": "UBS",
        "title": "Legacy Partial Note",
        "date": "2026-11-26",
        "path": "state/sales-notes/legacy-partial.md",
        "origin": "manual",
        "created_at": "2026-11-26T00:00:00Z",
    }
    # Legacy r1 receipt claimed embedding_indexed=True with NO embedding_pending key!
    legacy_receipt = {
        "ok": True,
        "status": "ingested",
        "doc_key": legacy_doc_key,
        "document_id": "mail:999",
        "filepath": "state/sales-notes/legacy-partial.md",
        "chunk_count": len(chunks),
        "embedding_indexed": True,
    }
    lib_mod.sales_notes_store_mod.append_manifest(store.data_root, legacy_manifest_entry)
    store._append_audit_entry(legacy_receipt)
    with sqlite3.connect(store.mail_db) as conn:
        for idx, body_chunk in enumerate(chunks):
            conn.execute(
                "INSERT INTO email_chunks (date, email_id, message_id, sender, subject, chunk_index, content, embedding, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "2026-11-26",
                    legacy_doc_key,
                    legacy_doc_key,
                    "UBS",
                    "Legacy Partial Note",
                    idx,
                    body_chunk,
                    emb0 if idx == 0 else None,
                    "2026-11-26T00:00:00Z",
                ),
            )
        conn.execute(
            "INSERT INTO sales_ingest_receipts (request_id, doc_key, document_id, filepath, manifest_json, receipt_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                f"doc:{legacy_doc_key}",
                legacy_doc_key,
                "mail:999",
                "state/sales-notes/legacy-partial.md",
                json.dumps(legacy_manifest_entry, ensure_ascii=False),
                json.dumps(legacy_receipt, ensure_ascii=False),
                "2026-11-26T00:00:00Z",
            ),
        )
        conn.commit()

    # Retry legacy partial receipt while embedding still fails -> detects NULL chunk, returns retryable (not duplicate!)
    embed_state["fail"] = True
    seen_embed_batches.clear()
    l_code1, l_res1 = await store.ingest_sales_note(legacy_payload)
    assert l_code1 == 200 and l_res1["ok"] is True
    assert l_res1["status"] != "duplicate"
    assert l_res1["embedding_pending"] is True
    assert l_res1.get("retryable") is True
    assert len(seen_embed_batches) == 1 and len(seen_embed_batches[0]) == len(chunks) - 1

    # Retry legacy partial receipt once embedding recovers -> backfills ONLY missing chunks!
    embed_state["fail"] = False
    seen_embed_batches.clear()
    l_code2, l_res2 = await store.ingest_sales_note(legacy_payload)
    assert l_code2 == 200 and l_res2["ok"] is True
    assert l_res2["embedding_indexed"] is True
    assert l_res2["embedding_pending"] is False
    assert l_res2.get("embedding_backfilled") is True
    assert len(seen_embed_batches) == 1 and len(seen_embed_batches[0]) == len(chunks) - 1

    with sqlite3.connect(store.mail_db) as conn:
        null_cnt = conn.execute(
            "SELECT COUNT(*) FROM email_chunks WHERE message_id = ? AND embedding IS NULL",
            (legacy_doc_key,),
        ).fetchone()[0]
    assert null_cnt == 0, "All missing chunks on legacy partial receipt must be backfilled"


@pytest.mark.asyncio
async def test_r3_sales_notes_layer_all_cross_layer_ranking_before_rrf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 item 3: When `layer='all'`, semantic and keyword arms each rank across layers
    BEFORE RRF fusion, so with `limit=1` a high-similarity digest (0.99) ranks ahead of
    a low-similarity raw email (0.01)."""
    import numpy as np

    _, lib_mod = _load_commercial_connectors()
    store = lib_mod.MarketDigestPartitionStore(tmp_path / "sn_r3_rrf")
    store.ensure_schema()

    raw_vec = np.asarray([0.01, 0.99995, 0.0, 0.0], dtype=np.float32)
    digest_vec = np.asarray([0.99, 0.141067, 0.0, 0.0], dtype=np.float32)
    query_vec = [1.0, 0.0, 0.0, 0.0]

    with sqlite3.connect(store.mail_db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chunks ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "date TEXT, period TEXT, section_id TEXT, section_label TEXT, "
            "content TEXT, source_emails TEXT, embedding BLOB, created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO email_chunks (date, email_id, message_id, sender, subject, chunk_index, content, embedding, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-11-26",
                "raw-low-1",
                "msg-raw-low-1",
                "BrokerA",
                "Unrelated Macro Wrap",
                0,
                "General macro overview with no target keywords.",
                raw_vec.tobytes(),
                "2026-11-26T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT INTO chunks (date, period, section_id, section_label, content, source_emails, embedding, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-11-26",
                "daily",
                "sec-ai",
                "AI Supply Chain Digest",
                "Digest summary covering high-similarity accelerator demand.",
                "[]",
                digest_vec.tobytes(),
                "2026-11-26T00:00:00Z",
            ),
        )
        conn.commit()

    md_cli = store._library().module("sales-notes")
    monkeypatch.setattr(
        md_cli,
        "embed_query",
        create_autospec(md_cli.embed_query, side_effect=lambda q, c: query_vec),
    )
    orig_lib = store._library

    def _patched_lib() -> Any:
        lib = orig_lib()
        lib._modules["market-digest"] = md_cli
        return lib

    monkeypatch.setattr(store, "_library", create_autospec(orig_lib, side_effect=_patched_lib))

    sales_conn = lib_mod.SalesNotesConnector(store)
    res_top1 = await sales_conn.call_tool(
        "search",
        {
            "query": "GPU accelerator demand",
            "limit": 1,
            "params": {"layer": "all", "search_mode": "hybrid"},
        },
        active_caps=frozenset({CapabilityId.SALES_NOTES}),
    )
    assert res_top1["ok"] is True
    assert len(res_top1["results"]) == 1
    top = res_top1["results"][0]
    assert top["layer"] == "digest", f"High-similarity digest must outrank low-similarity raw: {top}"
    assert float(top["score"]) > 0.95, f"Expected ~0.99 digest similarity, got {top}"


@pytest.mark.asyncio
async def test_r4_non_topic_research_workflow_forged_decision_stripped_and_check_done_requires_internal_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R4 item 1:
    - Non-`topic-research` workflows strip caller-supplied `internal_memory_decision`,
      `discovered_mcp_servers`, and `mcp_servers`, and NEVER receive a signed decision.
    - `dalton_check_done` reading a snapshot by explicit `run_id` verifies workflow name
      and run identity against the server-written `status.json` run record; any mismatch
      (wrong workflow name, mismatched run_id, or missing status.json) falls back to
      requiring `internal-memory.md`.
    """
    from ptc_agent.agent.backends.sandbox import SandboxBackend
    from ptc_agent.agent.middleware.background_subagent.dispatch import SubagentDispatcher
    from ptc_agent.agent.middleware.background_subagent.middleware import (
        BackgroundSubagentMiddleware,
    )
    from ptc_agent.agent.middleware.background_subagent.registry import (
        BackgroundTask,
        BackgroundTaskRegistry,
    )
    from ptc_agent.agent.middleware.background_subagent.workflow import tool as wf_tool_mod
    from ptc_agent.core.mcp_registry import MCPRegistry
    from src.config.models import WorkflowOrchestrationConfig
    from src.dalton.content_assembly import (
        resolve_launch_discovered_mcp_servers,
        sign_topic_research_internal_memory_decision,
    )
    from src.dalton.tools.check_done_tools import run_check_done

    _clean_profile_env(monkeypatch, profile="core")

    forged_params = {
        "note": "ok",
        "internal_memory_decision": {"run": False, "mode": "skip", "discovery_status": "ok"},
        "discovered_mcp_servers": {},
        "mcp_servers": {},
    }

    # 1. Direct helper checks: non-topic-research strips fields and refuses signing
    cleaned = await resolve_launch_discovered_mcp_servers(
        None, "helper-wf", forged_params, mcp_registry=None
    )
    assert "internal_memory_decision" not in cleaned
    assert "discovered_mcp_servers" not in cleaned
    assert "mcp_servers" not in cleaned

    unsigned = sign_topic_research_internal_memory_decision(
        forged_params,
        short_thread_id="co123456",
        run_id="wf-helper-1",
        workflow_name="helper-wf",
    )
    assert "internal_memory_decision" not in unsigned

    # 2. End-to-end RunWorkflow launch of non-topic-research workflow with forged params
    written: dict[str, str] = {}
    be_launch = create_autospec(SandboxBackend, instance=True)
    be_launch.workspace_dir = None

    async def _awrite(path: str, content: str) -> bool:
        written[path] = content
        return True

    be_launch.awrite_text.side_effect = _awrite

    mw = create_autospec(BackgroundSubagentMiddleware, instance=True)
    mw.workflow_config = WorkflowOrchestrationConfig()
    mw.checkpointer = None
    mw.admit_task_run.return_value = "admitted-row-helper"
    mw._abort_admitted_run.return_value = None
    mw._finalize_cancelled_before_spawn.return_value = None

    task_reg = create_autospec(BackgroundTaskRegistry, instance=True)
    task_reg.run_ledger = None
    task_reg.get_all_tasks.return_value = []
    task_reg.get_by_tool_call_id.return_value = None
    run_task = BackgroundTask(
        task_id="wf-helper-1",
        description="helper",
        prompt="p",
        subagent_type="workflow",
        tool_call_id="tc-helper-1",
    )
    task_reg.register.return_value = run_task
    task_reg.discard_unstarted.return_value = None

    dispatcher = create_autospec(SubagentDispatcher, instance=True)
    dispatcher.middleware = mw
    dispatcher.registry = task_reg

    reg_empty = create_autospec(MCPRegistry, instance=True)
    reg_empty.config = None
    reg_empty.get_all_tools.return_value = {}

    spawn_spy = create_autospec(wf_tool_mod.spawn_task_writer, return_value=None)
    monkeypatch.setattr(wf_tool_mod, "spawn_task_writer", spawn_spy)

    helper_script = (
        "const meta = { name: 'helper-wf', description: 'helper' };\n"
        "async function run() { return { ok: true }; }\n"
    )
    wf_tool = wf_tool_mod.create_run_workflow_tool(
        dispatcher=dispatcher,
        backend=be_launch,
        thread_id="co123456-full",
        short_thread_id="co123456",
        mcp_registry=reg_empty,
    )
    reply = await wf_tool.ainvoke(
        {
            "name": "RunWorkflow",
            "id": "tc-helper-1",
            "type": "tool_call",
            "args": {"script": helper_script, "params": forged_params},
        }
    )
    assert "Workflow run started" in str(reply)
    helper_args_path = ".agents/threads/co123456/workflows/wf-helper-1/args.json"
    assert helper_args_path in written
    saved_helper_args = json.loads(written[helper_args_path])
    assert "internal_memory_decision" not in saved_helper_args
    assert "discovered_mcp_servers" not in saved_helper_args
    assert "mcp_servers" not in saved_helper_args

    # 3. check_done on topic-research pointing at helper-wf run_id (and mismatched server run records)
    #    MUST still require internal-memory.md even if args.json carries a signed skip payload!
    base_files: dict[str, str] = {
        "research/2026-11-26-topic-ai/plan.md": "p" * 120,
        "research/2026-11-26-topic-ai/primary.md": "p" * 120,
        "research/2026-11-26-topic-ai/cluster-a.md": "c" * 120,
        "research/2026-11-26-topic-ai/cluster-b.md": "c" * 120,
        "research/2026-11-26-topic-ai/cluster-c.md": "c" * 120,
        "research/2026-11-26-topic-ai/cross-check.md": ("x" * 120) + "\n盲区检查完成\n",
        "research/2026-11-26-topic-ai/number-audit.md": "n" * 80,
        "research/2026-11-26-topic-ai/knowledge-revision.json": json.dumps({"ok": True, "pad": "x" * 50}),
        "drafts/2026-11-26-topic-ai.md": "d" * 600,
    }

    def _make_check_backend(extra_files: dict[str, str]) -> Any:
        files_map = {**base_files, **extra_files}
        be = create_autospec(SandboxBackend, instance=True)
        be.workspace_dir = None

        async def _aread(path: str) -> str | None:
            norm = path[2:] if path.startswith("./") else path.lstrip("/")
            return files_map.get(norm)

        async def _aglob(pattern: str, _root: str = ".") -> list[str]:
            import fnmatch

            return sorted(k for k in files_map if fnmatch.fnmatch(k, pattern))

        be.aread_text.side_effect = _aread
        be.aglob_paths.side_effect = _aglob
        be.aexecute = None
        return be

    signed_wrong_wf = sign_topic_research_internal_memory_decision(
        {"internal_memory_decision": {"run": False, "mode": "skip", "discovery_status": "ok"}},
        short_thread_id="co123456",
        run_id="wf-wrong-wf",
    )
    signed_wrong_runid = sign_topic_research_internal_memory_decision(
        {"internal_memory_decision": {"run": False, "mode": "skip", "discovery_status": "ok"}},
        short_thread_id="co123456",
        run_id="wf-wrong-id",
    )
    signed_no_status = sign_topic_research_internal_memory_decision(
        {"internal_memory_decision": {"run": False, "mode": "skip", "discovery_status": "ok"}},
        short_thread_id="co123456",
        run_id="wf-no-status",
    )
    signed_valid = sign_topic_research_internal_memory_decision(
        {"internal_memory_decision": {"run": False, "mode": "skip", "discovery_status": "ok"}},
        short_thread_id="co123456",
        run_id="wf-valid-tr",
    )

    be_check = _make_check_backend(
        {
            helper_args_path: written[helper_args_path],
            ".agents/threads/co123456/workflows/wf-helper-1/status.json": json.dumps(
                {"run_id": "wf-helper-1", "name": "helper-wf", "status": "completed", "updated_at": 1.0}
            ),
            ".agents/threads/co123456/workflows/wf-wrong-wf/args.json": json.dumps(signed_wrong_wf),
            ".agents/threads/co123456/workflows/wf-wrong-wf/status.json": json.dumps(
                {"run_id": "wf-wrong-wf", "name": "helper-wf", "status": "completed", "updated_at": 2.0}
            ),
            ".agents/threads/co123456/workflows/wf-wrong-id/args.json": json.dumps(signed_wrong_runid),
            ".agents/threads/co123456/workflows/wf-wrong-id/status.json": json.dumps(
                {"run_id": "wf-different", "name": "topic-research", "status": "completed", "updated_at": 3.0}
            ),
            ".agents/threads/co123456/workflows/wf-no-status/args.json": json.dumps(signed_no_status),
            ".agents/threads/co123456/workflows/wf-valid-tr/args.json": json.dumps(signed_valid),
            ".agents/threads/co123456/workflows/wf-valid-tr/status.json": json.dumps(
                {"run_id": "wf-valid-tr", "name": "topic-research", "status": "completed", "updated_at": 4.0}
            ),
        }
    )

    for bad_run_id in ("wf-helper-1", "wf-wrong-wf", "wf-wrong-id", "wf-no-status"):
        res_bad = await run_check_done(
            workflow="topic-research",
            params={"topic": "AI Capex", "slug": "ai", "workspace_dir": ".", "as_of": "2026-11-26"},
            backend=be_check,
            workspace_id=None,
            run_id=bad_run_id,
            thread_id="co123456-full",
        )
        assert any("internal-memory.md" in m for m in res_bad["missing"]), (
            f"Run {bad_run_id} must NOT bypass internal-memory.md check: {res_bad['missing']}"
        )

    # Positive control: matching server status.json + signed topic-research decision skips internal-memory.md
    res_ok = await run_check_done(
        workflow="topic-research",
        params={"topic": "AI Capex", "slug": "ai", "workspace_dir": ".", "as_of": "2026-11-26"},
        backend=be_check,
        workspace_id=None,
        run_id="wf-valid-tr",
        thread_id="co123456-full",
    )
    assert not any("internal-memory.md" in m for m in res_ok["missing"]), res_ok["missing"]


@pytest.mark.asyncio
async def test_r4_workflow_snapshot_cancelled_after_admission_finalizes_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R4 item 2:
    When `asyncio.CancelledError` is raised during post-admission artifact snapshotting
    (`workflow.js` or `args.json`), `RunWorkflow` runs terminal cancellation cleanup
    (`_finalize_cancelled_before_spawn`, marks task cancelled, never spawns writer) and
    re-raises `CancelledError`.
    """
    import asyncio

    from ptc_agent.agent.backends.sandbox import SandboxBackend
    from ptc_agent.agent.middleware.background_subagent.dispatch import SubagentDispatcher
    from ptc_agent.agent.middleware.background_subagent.middleware import (
        BackgroundSubagentMiddleware,
    )
    from ptc_agent.agent.middleware.background_subagent.registry import (
        BackgroundTask,
        BackgroundTaskRegistry,
    )
    from ptc_agent.agent.middleware.background_subagent.workflow import tool as wf_tool_mod
    from ptc_agent.core.mcp_registry import MCPRegistry
    from src.config.models import WorkflowOrchestrationConfig

    _clean_profile_env(monkeypatch, profile="core")

    reg_empty = create_autospec(MCPRegistry, instance=True)
    reg_empty.config = None
    reg_empty.get_all_tools.return_value = {}
    real_spawn_task_writer = wf_tool_mod.spawn_task_writer

    for cancel_suffix, wf_name, params in (
        (
            "/args.json",
            "topic-research",
            {
                "topic": "AI Capex",
                "slug": "ai",
                "subject_kind": "theme",
                "workspace_dir": ".",
                "as_of": "2026-11-26",
            },
        ),
        (
            "/workflow.js",
            "helper-wf",
            {"note": "x"},
        ),
    ):
        be = create_autospec(SandboxBackend, instance=True)
        be.workspace_dir = None

        async def _awrite_cancel(path: str, _content: str, *, _suffix: str = cancel_suffix) -> bool:
            if path.endswith(_suffix):
                raise asyncio.CancelledError()
            return True

        be.awrite_text.side_effect = _awrite_cancel

        mw = create_autospec(BackgroundSubagentMiddleware, instance=True)
        mw.workflow_config = WorkflowOrchestrationConfig()
        mw.checkpointer = None
        mw.admit_task_run.return_value = f"admitted-{wf_name}"
        mw._abort_admitted_run.return_value = None
        mw._finalize_cancelled_before_spawn.return_value = None

        task_reg = create_autospec(BackgroundTaskRegistry, instance=True)
        task_reg.run_ledger = None
        task_reg.get_all_tasks.return_value = []
        task_reg.get_by_tool_call_id.return_value = None
        run_task = BackgroundTask(
            task_id=f"wf-cancel-{wf_name}",
            description=wf_name,
            prompt="p",
            subagent_type="workflow",
            tool_call_id=f"tc-cancel-{wf_name}",
        )
        task_reg.register.return_value = run_task
        task_reg.discard_unstarted.return_value = None

        dispatcher = create_autospec(SubagentDispatcher, instance=True)
        dispatcher.middleware = mw
        dispatcher.registry = task_reg

        spawn_spy = create_autospec(real_spawn_task_writer, return_value=None)
        monkeypatch.setattr(wf_tool_mod, "spawn_task_writer", spawn_spy)

        script = (
            f"const meta = {{ name: '{wf_name}', description: '{wf_name}' }};\n"
            "async function run() { return { ok: true }; }\n"
        )
        wf_tool = wf_tool_mod.create_run_workflow_tool(
            dispatcher=dispatcher,
            backend=be,
            thread_id="co123456-full",
            short_thread_id="co123456",
            mcp_registry=reg_empty,
        )
        with pytest.raises(asyncio.CancelledError):
            await wf_tool.ainvoke(
                {
                    "name": "RunWorkflow",
                    "id": f"tc-cancel-{wf_name}",
                    "type": "tool_call",
                    "args": {"script": script, "params": params},
                }
            )

        mw._finalize_cancelled_before_spawn.assert_awaited_once_with(run_task)
        assert mw._abort_admitted_run.await_count == 0
        assert task_reg.discard_unstarted.await_count == 0
        assert spawn_spy.await_count == 0
        assert run_task.completed is True
        assert run_task.terminal_status == "cancelled"


class _R5FilesBackend:
    """Minimal read-only backend over an in-memory file map (aread_text only)."""

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files

    async def aread_text(self, path: str) -> str:
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path]


@pytest.mark.asyncio
async def test_r5_as_of_inherited_when_status_json_missing_but_rejected_on_mismatch() -> None:
    from src.dalton.tools import check_done_tools as cdt

    thread_id = "abcdef12-0000-0000-0000-000000000000"
    run_dir = ".agents/threads/abcdef12/workflows/wf-r5"
    args = json.dumps({"as_of": "2026-11-20"})

    # status.json never written: explicit run keeps its as_of.
    backend = _R5FilesBackend({f"{run_dir}/args.json": args})
    assert await cdt._run_args_as_of(backend, "topic-research", "wf-r5", thread_id) == "2026-11-20"
    # The strict loader used for the internal-memory skip still refuses it.
    doc, _ = await cdt._load_run_args_with_dir(backend, "topic-research", "wf-r5", thread_id)
    assert doc is None

    # A present status.json naming another workflow is still rejected.
    backend = _R5FilesBackend(
        {
            f"{run_dir}/args.json": args,
            f"{run_dir}/status.json": json.dumps({"name": "other-workflow", "run_id": "wf-r5"}),
        }
    )
    assert await cdt._run_args_as_of(backend, "topic-research", "wf-r5", thread_id) is None
