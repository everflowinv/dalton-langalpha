"""Offline unit tests for dalton-richard-coverage-weekly skill, screening, internal search, and idempotency."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pandas as pd
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-richard-coverage-weekly"
SCRIPTS_DIR = SKILL_DIR / "scripts"
SKILL_NAME = "dalton-richard-coverage-weekly"
PROFILE = "richard-coverage-weekly"


def _load_script(mod_name: str, filename: str):
    cached = sys.modules.get(mod_name)
    if cached is not None:
        return cached
    target = SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(mod_name, target)
    if spec is None or spec.loader is None:
        raise RuntimeError(target)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


cli = _load_script("dalton_richard_cov_cli_under_test", "richard_coverage_weekly_cli.py")
sns = _load_script("dalton_richard_cov_sales_notes_search_under_test", "sales_notes_search.py")
screen_movers = cli.screen_movers


BANNED_PROSE = ("不代表", "并非", "而不是", "不能", "不构成", "不应")


def _build_mock_download_panel(
    tickers: list[str],
    *,
    dates: pd.DatetimeIndex,
    base_idx: int,
    overrides: dict[str, dict[int, tuple[float, float]]] | None = None,
    truncate_after_idx: dict[str, int] | None = None,
) -> pd.DataFrame:
    """Construct a MultiIndex DataFrame matching ``yfinance.download(..., group_by='ticker')``."""
    overrides = overrides or {}
    truncate_after_idx = truncate_after_idx or {}
    cols: dict[tuple[str, str], list[float]] = {}
    for tk_i, tk in enumerate(tickers):
        close_vals = [100.0] * len(dates)
        vol_vals = [1_000_000.0] * len(dates)
        # Distinct ~1% daily swings per ticker before base_idx yield realistic 60d excess std (~1%)
        amp = 0.01 + 0.002 * ((tk_i % 4) + 1)
        sign = 1 if tk_i % 2 == 0 else -1
        for i in range(1, base_idx):
            close_vals[i] = close_vals[i - 1] * (1.0 + amp * sign if i % 2 == 0 else 1.0 / (1.0 + amp * sign))
        for i in range(base_idx, len(dates)):
            close_vals[i] = 100.0
        for idx, (c, v) in overrides.get(tk, {}).items():
            close_vals[idx] = c
            vol_vals[idx] = v
        cutoff = truncate_after_idx.get(tk)
        if cutoff is not None:
            for idx in range(cutoff + 1, len(dates)):
                close_vals[idx] = float("nan")
                vol_vals[idx] = float("nan")
        cols[(tk, "Adj Close")] = close_vals
        cols[(tk, "Volume")] = vol_vals
    df = pd.DataFrame(cols, index=dates)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    return df


# ---------------------------------------------------------------------------
# Universe & registration
# ---------------------------------------------------------------------------


def test_universe_counts_and_core_sync() -> None:
    cov = screen_movers.covered(screen_movers.TICKERS)
    core = screen_movers.covered(screen_movers.TICKERS, "Core")
    watch = screen_movers.covered(screen_movers.TICKERS, "Watch")
    assert len(cov) == 106
    assert len(core) == 28
    assert len(watch) == 78
    assert len(cli.CORE) == 28
    assert {c["name"] for c in cli.CORE} == set(core)
    raw_tickers = [v[0] for v in screen_movers.TICKERS.values()]
    assert len(raw_tickers) == len(set(raw_tickers))


def test_parse_sheet_retags_tiers_and_reports_unmapped(tmp_path: Path) -> None:
    csv_path = tmp_path / "sheet.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Header,Col",
                "Sub,Col2",
                "US Mega Tech,,,,,,",
                "Apple,Core,x,x,x,x",
                "Microsoft,Watch,x,x,x,x",
                "OpenAI,,,,",
                "UnknownCo,Watch,x,x,x,x",
            ]
        ),
        encoding="utf-8",
    )
    uni, unmapped = screen_movers.build_universe(csv_path)
    assert uni["Apple"][3] == "Core"
    assert uni["Microsoft"][3] == "Watch"
    assert uni["Google"][3] == ""
    assert unmapped == [{"name": "UnknownCo", "section": "US Mega Tech", "tier": "Watch"}]


def test_registry_report_profile_paths_and_skill_tree() -> None:
    from ptc_agent.agent.middleware.skills.registry import SKILL_REGISTRY
    from ptc_agent.agent.middleware.skills.dalton_registry import (
        DALTON_SKILL_DEFINITIONS,
        _DALTON_SKILL_SPECS,
        read_skill_description,
    )
    from src.dalton.database.automation_reports import REPORT_PROFILES

    assert (SKILL_NAME, None, ()) in _DALTON_SKILL_SPECS
    assert len(_DALTON_SKILL_SPECS) == 52
    assert SKILL_NAME in DALTON_SKILL_DEFINITIONS
    assert SKILL_NAME in SKILL_REGISTRY
    assert PROFILE in REPORT_PROFILES

    dalton_paths = (REPO_ROOT / "scripts" / "dalton" / "dalton_paths.txt").read_text(encoding="utf-8")
    assert f"plugins/dalton/skills/{SKILL_NAME}/" in dalton_paths
    assert "tests/unit/dalton/test_richard_coverage_weekly.py" in dalton_paths

    skill_md = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert skill_md.startswith("---\n")
    end_idx = skill_md.find("\n---\n", 4)
    meta = yaml.safe_load(skill_md[4:end_idx])
    assert set(meta) == {"name", "description"}
    assert meta["name"] == SKILL_NAME
    desc = meta["description"].strip()
    assert len(desc) <= 120
    assert "触发词" not in desc
    assert desc == read_skill_description(SKILL_NAME)

    body, heading, rules = skill_md.partition("## 撰写硬规则")
    assert heading == "## 撰写硬规则"
    assert skill_md.count("## 撰写硬规则") == 1
    for phrase in BANNED_PROSE:
        assert phrase not in body
        assert phrase in rules

    recipient_re = re.compile(r"\b(?:oc|ou|on)_[A-Za-z0-9]{10,}|discord:|\b[\w.-]+@[\w.-]+\.\w+\b")
    for path in SKILL_DIR.rglob("*"):
        if path.is_file() and path.suffix in {".md", ".py", ".yaml", ".json"}:
            txt = path.read_text(encoding="utf-8")
            assert not recipient_re.search(txt), f"Unexpected recipient-like pattern in {path}"


# ---------------------------------------------------------------------------
# screen_movers hit thresholds and ok:false guard
# ---------------------------------------------------------------------------


def test_screen_movers_flags_window_day_volume_and_stale_warning() -> None:
    dates = pd.bdate_range("2026-07-01", "2026-10-16")
    base_str = "2026-10-09"
    end_str = "2026-10-16"
    base_idx = int(dates.get_loc(pd.Timestamp(base_str)))
    last_idx = len(dates) - 1

    # Mini universe: Core + Watch examples across peer groups
    mini_uni = {
        "Google": ("GOOGL", "megacap", "Live Position", "Core"),
        "Microsoft": ("MSFT", "megacap", "US Mega Tech", ""),
        "Amazon": ("AMZN", "megacap", "Live Position", ""),
        "Meta": ("META", "megacap", "US Mega Tech", ""),
        "Apple": ("AAPL", "megacap", "US Mega Tech", "Watch"),  # window excess +8% -> flag window
        "Tesla": ("TSLA", "megacap", "US Mega Tech", "Watch"),  # calm vs 0% peer median -> no flag
        "Netflix": ("NFLX", "streaming_media", "US Internet", "Watch"),  # single-day +6% jump -> flag day
        "Spotify": ("SPOT", "streaming_media", "US Internet", "Watch"),  # calm price, 3.0x volume -> flag volume
        "Disney": ("DIS", "streaming_media", "US Internet", ""),
        "EchoStar": ("ECHO", "space", "Live Position", "Watch"),  # calm Live Position Watch -> kept in targets
        "Rocket Lab": ("RKLB", "space", "US Space", "Watch"),
    }

    overrides = {
        # Google is Core: even +10% excess should NOT add Watch flags
        "GOOGL": {last_idx: (110.0, 1_000_000.0)},
        # Apple rises +8% on end day relative to peers (~0%)
        "AAPL": {base_idx: (100.0, 1_000_000.0), last_idx: (108.0, 1_000_000.0)},
        # Netflix spikes +6% on day after base, returns flat before last_idx -> day flag, calm on last_idx
        "NFLX": {
            base_idx: (100.0, 1_000_000.0),
            base_idx + 1: (106.0, 1_000_000.0),
            base_idx + 2: (100.0, 1_000_000.0),
            base_idx + 3: (100.0, 1_000_000.0),
            base_idx + 4: (100.0, 1_000_000.0),
            last_idx: (100.0, 1_000_000.0),
        },
        # Spotify has flat prices across window/days but 3.0x volume spike
        "SPOT": {base_idx: (100.0, 1_000_000.0), last_idx: (100.0, 3_000_000.0)},
        "AAPL_NOOP": {},
    }
    overrides.pop("AAPL_NOOP", None)

    def fake_dl(tickers: list[str], start: str, end: str) -> pd.DataFrame:
        return _build_mock_download_panel(tickers, dates=dates, base_idx=base_idx, overrides=overrides)

    rows, missing, _bench = screen_movers.run_screen(
        mini_uni, base_str, end_str, "2026-07-01", downloader=fake_dl
    )
    assert missing == []
    by_name = {r["name"]: r for r in rows}

    assert by_name["Google"]["deep"] is True
    assert by_name["Google"]["flags"] == []

    assert any(f["type"] == "window" for f in by_name["Apple"]["flags"])
    assert any(f["type"] == "day" for f in by_name["Netflix"]["flags"])
    assert any(f["type"] == "volume" and f["vol_ratio"] >= 2.5 for f in by_name["Spotify"]["flags"])
    assert by_name["Tesla"]["flags"] == []
    assert by_name["EchoStar"]["flags"] == []

    ok_res = screen_movers.build_screen_result(rows, missing, {}, [], base_str, end_str)
    assert ok_res["ok"] is True
    assert ok_res["warning"] is None

    targets = cli.build_targets_from_screen(ok_res)
    target_names = {t["name"] for t in targets}
    assert "Google" in target_names
    assert {"Apple", "Netflix", "Spotify"} <= target_names
    assert "EchoStar" in target_names  # unflagged Live Position Watch kept
    assert "Tesla" not in target_names

    # Now test stale US bar -> ok=False warning & targets rejection
    stale_trunc = {tk: last_idx - 1 for tk in ("GOOGL", "AAPL", "TSLA", "NFLX", "SPOT", "ECHO", "RKLB")}

    def stale_dl(tickers: list[str], start: str, end: str) -> pd.DataFrame:
        return _build_mock_download_panel(
            tickers, dates=dates, base_idx=base_idx, truncate_after_idx=stale_trunc
        )

    stale_rows, stale_missing, _ = screen_movers.run_screen(
        mini_uni, base_str, end_str, "2026-07-01", downloader=stale_dl
    )
    bad_res = screen_movers.build_screen_result(stale_rows, stale_missing, {}, [], base_str, end_str)
    assert bad_res["ok"] is False
    assert "missing the latest daily bar" in bad_res["warning"]
    with pytest.raises(ValueError, match="ok=false"):
        cli.build_targets_from_screen(bad_res)


# ---------------------------------------------------------------------------
# sales_notes_search with create_autospec stub
# ---------------------------------------------------------------------------


def _caller_sig(arguments: dict) -> dict:
    return {}


def test_sales_notes_search_keyword_precision_tiers_cap_and_limit_hit() -> None:
    caller = create_autospec(_caller_sig, spec_set=True)

    # 13 docs for DoorDash across tiers + false-positive lowercase "dash" + wiki docs
    docs_content: dict[str, dict] = {
        # False positive: lowercase 'dash' must NOT match DoorDash / DASH
        "mail:fp1": {
            "title": "Macro dash note",
            "date": "2026-10-15",
            "original_source": "gs@example.com",
            "content": "Traders made a mad dash toward defensive assets.",
        },
        # False positive: lowercase 'echo' must NOT match EchoStar / ECHO
        "mail:fp2": {
            "title": "Echoes of 2022",
            "date": "2026-10-14",
            "original_source": "bofa@example.com",
            "content": "Market sentiment echoed earlier cycles without satellite news.",
        },
        # 8 sales-notes + 4 tmtb = 12 top tier docs for DoorDash -> fills cap=12
        **{
            f"mail:sn{i}": {
                "title": f"DoorDash GS Note {i}",
                "date": f"2026-10-{10 + (i % 6):02d}",
                "original_source": "gs@example.com",
                "content": f"DoorDash order volume acceleration #{i}. DASH remains Top Pick.",
            }
            for i in range(8)
        },
        **{
            f"tmtb:t{i}": {
                "title": f"TMTB Digest {i}",
                "date": f"2026-10-{11 + (i % 5):02d}",
                "original_source": "tmtb",
                "content": f"DoorDash (DASH) courier supply tight in Q3 item {i}.",
            }
            for i in range(4)
        },
        # 1 funda-ai (tier 1) and 1 high-hit wiki (tier 2) -> both MUST be omitted behind 12 tier-0 docs!
        "funda:f1": {
            "title": "FUNDA DoorDash review",
            "date": "2026-10-16",
            "original_source": "funda-ai",
            "content": "DoorDash DoorDash DoorDash margin model updated.",
        },
        "wiki:w_own": {
            "title": "DoorDash deep dive archive",
            "date": "2026-08-01",
            "original_source": "company-wiki",
            "content": " ".join(["DoorDash DASH unit economics"] * 10),
        },
        "wiki:w_sector_once": {
            "title": "Broad delivery sector memo",
            "date": "2026-10-15",
            "original_source": "company-wiki",
            "content": "Focus on Uber Eats; DoorDash mentioned once briefly.",
        },
        "wiki:w_echo_own": {
            "title": "EchoStar own folder summary",
            "date": "2026-10-15",
            "original_source": "company-wiki",
            "content": "Portfolio checklist updated.",  # 0 kw hits, but company==ECHO -> kept!
        },
    }

    dash_search_hits = (
        [{"document_id": "mail:fp1", "date": "2026-10-15", "title": "Macro dash note"}]
        + [{"document_id": f"mail:sn{i}", "date": "2026-10-12", "title": f"DoorDash GS Note {i}"} for i in range(8)]
    )
    tmtb_hits = [{"document_id": f"tmtb:t{i}", "date": "2026-10-12", "title": f"TMTB Digest {i}"} for i in range(4)]

    # Simulate SEARCH_LIMIT=100 limit_hit on EchoStar in sales-notes
    overflow_rows = [{"document_id": "mail:fp2", "date": "2026-10-14", "title": "Echoes"}] + [
        {"document_id": f"mail:noise{i}", "date": "2026-10-10", "title": f"Noise {i}"} for i in range(99)
    ]

    def fake_research(arguments: dict) -> dict:
        route = arguments.get("route")
        op = arguments.get("operation")
        if route == "company-wiki" and op == "list":
            return {
                "ok": True,
                "results": [
                    {"document_id": "wiki:w_own", "company": "DASH", "date": "2026-08-01", "title": "DoorDash deep dive archive"},
                    {"document_id": "wiki:w_sector_once", "company": "SECTOR", "date": "2026-10-15", "title": "Broad delivery sector memo"},
                    {"document_id": "wiki:w_echo_own", "company": "ECHO", "date": "2026-10-15", "title": "EchoStar own folder summary"},
                ],
            }
        if route == "all" and op == "read":
            did = arguments["document"]
            doc = docs_content.get(
                did,
                {
                    "title": f"Unrelated {did}",
                    "date": "2026-10-10",
                    "original_source": "gs@example.com",
                    "content": "Unrelated macro update without coverage mentions.",
                },
            )
            return {"ok": True, "document": doc}
        if route == "sales-notes" and op == "search":
            q = arguments.get("query", "")
            if "echostar" in q.lower() or "echo" in q.lower():
                return {"ok": True, "results": overflow_rows}
            return {"ok": True, "results": dash_search_hits}
        if route == "tmtb" and op == "search":
            if "dash" in arguments.get("query", "").lower() or "doordash" in arguments.get("query", "").lower():
                return {"ok": True, "results": tmtb_hits}
            return {"ok": True, "results": []}
        if route == "funda-ai" and op == "search":
            if "doordash" in arguments.get("query", "").lower():
                return {"ok": True, "results": [{"document_id": "funda:f1", "date": "2026-10-16", "title": "FUNDA DoorDash review"}]}
            return {"ok": True, "results": []}
        return {"ok": False, "error": f"unhandled route={route}"}

    caller.side_effect = fake_research

    targets = [
        {"name": "DoorDash", "ticker": "DASH", "kw": ["DoorDash", "DASH"]},
        {"name": "EchoStar", "ticker": "ECHO", "kw": ["EchoStar", "ECHO"]},
    ]
    results, stats, warnings = sns.unified_scan(
        targets, "2026-10-08", "2026-10-17", per_company=12, caller=caller
    )
    assert warnings == []
    assert caller.call_count >= 8
    assert stats["wiki_indexed_in_window"] == 3

    by_co = {r["name"]: r for r in results}
    dash = by_co["DoorDash"]
    picked_ids = {p["document_id"] for p in dash["picks"]}
    omitted_ids = {o["document_id"] for o in dash["omitted_list"]}
    assert len(dash["picks"]) == 12
    assert dash["omitted"] == 2  # funda:f1 + wiki:w_own pushed behind 12 tier-0 docs
    assert "mail:fp1" not in picked_ids and "mail:fp1" not in omitted_ids
    assert "wiki:w_sector_once" not in picked_ids and "wiki:w_sector_once" not in omitted_ids
    assert {"funda:f1", "wiki:w_own"} == omitted_ids
    assert all("excerpt" not in o for o in dash["omitted_list"])
    assert dash["limit_hit"] == []

    echo = by_co["EchoStar"]
    assert "sales-notes" in echo["limit_hit"]
    echo_ids = {p["document_id"] for p in echo["picks"]}
    assert "mail:fp2" not in echo_ids  # lowercase 'echoed' rejected
    assert "wiki:w_echo_own" in echo_ids  # own-company wiki retained even with 0 kw hits


# ---------------------------------------------------------------------------
# Window, idempotent Feishu doc receipt state, and code mapping
# ---------------------------------------------------------------------------


def _spy_bar_downloader(trading_days: list[str]):
    def _dl(tickers: list[str], start: str, end: str) -> pd.DataFrame:
        idx = pd.DatetimeIndex([pd.Timestamp(d) for d in trading_days])
        cols = pd.MultiIndex.from_tuples([("SPY", "Adj Close"), ("SPY", "Volume")])
        return pd.DataFrame(
            [[500.0 + i, 1_000_000.0] for i in range(len(idx))],
            index=idx,
            columns=cols,
        )

    return _dl


def test_window_xq_code_and_doc_idempotency(tmp_path: Path) -> None:
    # Sunday 2026-10-18 Asia/Shanghai -> Friday 2026-10-09 to Friday 2026-10-16 (confirmed by SPY bars)
    spy_dl = _spy_bar_downloader(["2026-10-09", "2026-10-15", "2026-10-16"])
    win = cli.compute_window(now_date="2026-10-18", downloader=spy_dl)
    assert win["base"] == "2026-10-09"
    assert win["end"] == "2026-10-16"
    assert win["search_from"] == "2026-10-08"
    assert win["search_to"] == "2026-10-17"
    assert win["report_profile"] == "richard-coverage-weekly"
    assert win["report_run_id"] == "richard-coverage-weekly-2026-10-16"
    # The failure notice has its own id so a failed attempt never takes the weekly id.
    assert win["failure_report_run_id"].startswith("richard-coverage-weekly-2026-10-16-failed-")
    assert win["failure_report_run_id"] != win["report_run_id"]

    assert cli.xq_code("3690.HK") == "03690"
    assert cli.xq_code("601100.SS") == "SH601100"
    assert cli.xq_code("300413.SZ") == "SZ300413"
    assert cli.xq_code("GOOGL") == "GOOGL"

    end_d = dt.date(2026, 10, 16)
    status0 = cli.doc_status_for(tmp_path, end_d)
    assert status0["exists"] is False and status0["create"] is True and status0["attempt_id"]

    # Two distinct attempts failing -> blocked
    f1 = cli.save_doc_record(
        tmp_path,
        end_d,
        json.dumps({"ok": False, "error": "timeout"}).encode("utf-8"),
        total_parts=1,
        attempt_id=status0["attempt_id"],
    )
    assert f1["stored"] is True and f1["create"] is True and f1["attempts"] == 1
    status_retry = cli.doc_status_for(tmp_path, end_d)
    assert status_retry["create"] is True and status_retry["attempt_id"] != status0["attempt_id"]
    f2 = cli.save_doc_record(
        tmp_path,
        end_d,
        json.dumps({"ok": False, "error": "bridge_down"}).encode("utf-8"),
        total_parts=1,
        attempt_id=status_retry["attempt_id"],
    )
    assert f2["stored"] is True and f2["create"] is False and f2["reason"] == "create_failed_twice"
    assert cli.doc_status_for(tmp_path, end_d)["reason"] == "create_failed_twice"

    # Separate run date with successful doc creation -> atomic identity+progress & idempotent skip
    end_ok = dt.date(2026, 10, 23)
    raw_ok = json.dumps({"ok": True, "document_id": "DoxCreatedOnce"}).encode("utf-8")
    saved = cli.save_doc_record(tmp_path, end_ok, raw_ok, total_parts=1, attempt_id="att-ok-1")
    assert saved["stored"] is True and saved["skipped"] is False and saved["body_incomplete"] is False
    assert saved["url"] == "https://feishu.cn/docx/DoxCreatedOnce"

    status1 = cli.doc_status_for(tmp_path, end_ok)
    assert status1["exists"] is True and status1["create"] is False and status1["body_incomplete"] is False
    assert status1["url"] == "https://feishu.cn/docx/DoxCreatedOnce"

    # Re-saving with a second return payload skips and never overwrites doc.json
    dup = cli.save_doc_record(
        tmp_path,
        end_ok,
        json.dumps({"ok": True, "document_id": "DoxShouldNeverOverwrite"}).encode("utf-8"),
        total_parts=1,
        attempt_id="att-ok-2",
    )
    assert dup["skipped"] is True and dup["stored"] is False and dup["create"] is False
    assert dup["url"] == "https://feishu.cn/docx/DoxCreatedOnce"
    doc_file = tmp_path / "state" / "richard-coverage-weekly" / "runs" / "2026-10-23" / "doc.json"
    saved_doc_json = json.loads(doc_file.read_text(encoding="utf-8"))
    assert saved_doc_json["document_id"] == "DoxCreatedOnce"
    assert saved_doc_json["_progress"]["total_parts"] == 1
    assert saved_doc_json["_progress"]["body_incomplete"] is False

    # Corrupt doc.json -> unreadable
    end_bad = dt.date(2026, 10, 30)
    bad_file = tmp_path / "state" / "richard-coverage-weekly" / "runs" / "2026-10-30" / "doc.json"
    bad_file.parent.mkdir(parents=True, exist_ok=True)
    bad_file.write_text("{broken json", encoding="utf-8")
    assert cli.doc_status_for(tmp_path, end_bad)["reason"] == "doc_record_unreadable"


# ---------------------------------------------------------------------------
# Round 3 fixes: H1, H2, H3, M1, M2, M3, M5, M7
# ---------------------------------------------------------------------------


def _write_targets_and_run_dir(ws: Path, targets: list[dict], end: str = "2026-10-16") -> tuple[str, str]:
    run_rel = f"state/richard-coverage-weekly/runs/{end}"
    run_dir = ws / run_rel
    run_dir.mkdir(parents=True, exist_ok=True)
    targets_rel = f"{run_rel}/targets.json"
    (ws / targets_rel).write_text(json.dumps(targets, ensure_ascii=False), encoding="utf-8")
    return targets_rel, run_rel


def test_ae_nested_bridge_shape_digest_and_alt_trigger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    targets = [
        {"name": "Google", "ticker": "GOOGL", "kw": ["Alphabet", "Google", "谷歌", "GOOGL"], "tier": "Core"},
        {"name": "Amazon", "ticker": "AMZN", "kw": ["Amazon", "亚马逊", "AMZN"], "tier": "Core"},
    ]
    targets_rel, run_rel = _write_targets_and_run_dir(tmp_path, targets)
    calls: list[dict] = []

    def fake_caller(tool: str, req: dict) -> dict:
        calls.append({"tool": tool, "req": req})
        q = req.get("query", "")
        cats = (req.get("params") or {}).get("document_categories", [])
        if q == "Alphabet":
            if "news" in cats:
                results = [
                    {
                        "doc_id": f"gn{i}",
                        "title": f"Google cloud headline {i}",
                        "companies": ["Alphabet"],
                        "snippet": "Alphabet Q3 update.",
                        "publish_time": "2026-10-15T10:00:00Z",
                        "sources": ["Reuters"],
                    }
                    for i in range(3)
                ]
            else:
                results = [
                    {
                        "doc_id": f"gr{i}",
                        "title": f"Alphabet AI note {i}",
                        "companies": ["Google"],
                        "snippet": "Search resilient.",
                        "publish_time": "2026-10-14T08:00:00Z",
                        "sources": ["Morgan Stanley"],
                    }
                    for i in range(3)
                ]
            return {
                "ok": True,
                "data": {"structuredContent": {"results": results, "total_upstream": 12}},
            }
        if q == "Amazon":
            # Only 2 genuine Amazon hits (+ 1 off-topic noise) -> MUST trigger alt query "亚马逊"
            results = [
                {
                    "doc_id": "ar0",
                    "title": "Amazon AWS capacity expansion",
                    "companies": ["Amazon"],
                    "snippet": "AWS backlog expands.",
                    "publish_time": "2026-10-15T09:00:00Z",
                    "sources": ["GS"],
                },
                {
                    "doc_id": "ar1",
                    "title": "AMZN retail margin tracking",
                    "companies": ["AMZN"],
                    "snippet": "Retail fulfillment steady.",
                    "publish_time": "2026-10-14T09:00:00Z",
                    "sources": ["BofA"],
                },
                {
                    "doc_id": "ar_noise",
                    "title": "Broad logistics tracker",
                    "companies": ["UPS"],
                    "snippet": "Parcel volumes only.",
                    "publish_time": "2026-10-14T09:00:00Z",
                    "sources": ["JPM"],
                },
            ]
            return {
                "ok": True,
                "data": {"structuredContent": {"results": results, "total_upstream": 8}},
            }
        if q == "亚马逊":
            return {
                "ok": True,
                "data": {
                    "structuredContent": {
                        "results": [
                            {
                                "doc_id": "ar_alt1",
                                "title": "亚马逊跨境招商纪要",
                                "companies": ["Amazon"],
                                "snippet": "Prime Big Deal Days 销量稳健。",
                                "publish_time": "2026-10-15T11:00:00Z",
                                "sources": ["CICC"],
                            }
                        ],
                        "total_upstream": 3,
                    }
                },
            }
        return {"ok": True, "data": {"structuredContent": {"results": [], "total_upstream": 0}}}

    args = SimpleNamespace(
        targets=targets_rel,
        run_dir=run_rel,
        date_from="2026-10-08",
        date_to="2026-10-17",
        tier="Core",
        only=None,
        force=False,
        workspace_dir=str(tmp_path),
    )
    cli.cmd_ae(args, caller=fake_caller)
    ae_out = capsys.readouterr().out
    # Alphabet has 3 rel hits on res and news -> "谷歌" alt must NOT be called
    assert all(c["req"]["query"] != "谷歌" for c in calls)
    # Amazon had only 2 rel hits -> "亚马逊" alt MUST be called
    assert any(c["req"]["query"] == "亚马逊" for c in calls)
    assert "GOOGL|res|Alphabet|up=12|ret=3|rel=3" in ae_out
    assert "AMZN|res|Amazon|up=8|ret=3|rel=2 || alt 亚马逊 up=3 ret=1 rel=1" in ae_out

    digest_args = SimpleNamespace(
        run_dir=run_rel,
        tickers="GOOGL,AMZN",
        n=10,
        news=4,
        snip=260,
        workspace_dir=str(tmp_path),
    )
    cli.cmd_ae_digest(digest_args)
    dig_out = capsys.readouterr().out
    assert "===== GOOGL\n-- res 3" in dig_out
    assert "[2026-10-14|Morgan Stanley|gr0] Alphabet AI note 0" in dig_out
    assert "===== AMZN\n-- res 3" in dig_out
    assert "[2026-10-15|CICC|ar_alt1] 亚马逊跨境招商纪要" in dig_out
    assert "ar_noise" not in dig_out


def test_ae_quota_hit_recognizes_error_category(tmp_path: Path) -> None:
    assert cli._ae_quota_hit({"ok": False, "error": "rate_limit", "error_category": "权限或额度"})
    assert cli._ae_quota_hit({"ok": False, "data": {"error_category": "权限或额度"}})
    assert cli._ae_quota_hit({"ok": False, "error": "每日额度已达上限"})

    targets_rel, run_rel = _write_targets_and_run_dir(
        tmp_path,
        [{"name": "Google", "ticker": "GOOGL", "kw": ["Alphabet"], "tier": "Core"}],
    )

    def quota_caller(tool: str, req: dict) -> dict:
        return {"ok": False, "error": "upstream_limit", "error_category": "权限或额度"}

    args = SimpleNamespace(
        targets=targets_rel,
        run_dir=run_rel,
        date_from="2026-10-08",
        date_to="2026-10-17",
        tier="Core",
        only=None,
        force=False,
        workspace_dir=str(tmp_path),
    )
    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_ae(args, caller=quota_caller)
    assert exc_info.value.code == 3


def test_x_timeline_nested_items_metrics_sort_and_repost_filter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.time, "sleep", lambda _sec: None)
    targets_rel, run_rel = _write_targets_and_run_dir(
        tmp_path,
        [{"name": "Google", "ticker": "GOOGL", "kw": ["Alphabet", "GOOGL"], "tier": "Core"}],
    )

    def fake_caller(tool: str, req: dict) -> dict:
        assert req["route"] == "x-timeline"
        return {
            "ok": True,
            "status": "ok",
            "data": {
                "items": [
                    {
                        "id": "rep1",
                        "url": "https://x.com/a/status/rep1",
                        "created_at": "2026-10-15T12:00:00Z",
                        "text": "Repost should be skipped",
                        "metrics": {"views": 99999, "likes": 500},
                        "is_repost": True,
                    },
                    {
                        "id": "ret2",
                        "url": "https://x.com/a/status/ret2",
                        "created_at": "2026-10-15T12:00:00Z",
                        "text": "Legacy retweet skipped",
                        "viewCount": 88888,
                        "likeCount": 400,
                        "isRetweet": True,
                    },
                    {
                        "id": "p_low_likes",
                        "url": "https://x.com/a/status/p_low_likes",
                        "created_at": "2026-10-15T10:00:00Z",
                        "text": "Equal views lower likes",
                        "metrics": {"views": 1000, "likes": 20},
                        "is_repost": False,
                    },
                    {
                        "id": "p_high_likes",
                        "url": "https://x.com/a/status/p_high_likes",
                        "created_at": "2026-10-15T11:00:00Z",
                        "text": "Equal views higher likes",
                        "metrics": {"views": 1000, "likes": 80},
                        "is_repost": False,
                    },
                    {
                        "id": "p_top",
                        "url": "https://x.com/a/status/p_top",
                        "created_at": "2026-10-16T08:00:00Z",
                        "text": "Top post on TPU demand",
                        "metrics": {"views": 5000, "likes": 120},
                        "is_repost": False,
                    },
                ]
            },
        }

    cli.cmd_x_timeline(
        SimpleNamespace(
            targets=targets_rel,
            run_dir=run_rel,
            since="2026-10-08",
            until="2026-10-17",
            only="GOOGL",
            tier="Core",
            n=40,
            workspace_dir=str(tmp_path),
        ),
        caller=fake_caller,
    )
    tl_out = capsys.readouterr().out
    assert "GOOGL ok=True items=5" in tl_out

    cli.cmd_x_digest(
        SimpleNamespace(run_dir=run_rel, keys="GOOGL", n=8, len=220, workspace_dir=str(tmp_path))
    )
    dig_out = capsys.readouterr().out
    assert "===== GOOGL 3" in dig_out
    assert "rep1" not in dig_out and "ret2" not in dig_out
    idx_top = dig_out.index("p_top")
    idx_hi = dig_out.index("p_high_likes")
    idx_lo = dig_out.index("p_low_likes")
    assert idx_top < idx_hi < idx_lo
    assert "[2026-10-16|v5000|l120]" in dig_out


def test_xq_nested_posts_preserves_raw_writes_summary_and_surfaces_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.time, "sleep", lambda _sec: None)
    targets_rel, run_rel = _write_targets_and_run_dir(
        tmp_path,
        [
            {"name": "美团", "ticker": "3690.HK", "kw": ["美团", "Meituan"], "tier": "Core"},
            {"name": "阿里巴巴", "ticker": "9988.HK", "kw": ["阿里巴巴"], "tier": "Core"},
        ],
    )
    raw_meituan = {
        "ok": True,
        "status": "ok",
        "data": {
            "posts": [
                {
                    "id": "old1",
                    "created_at": "2026-10-01T09:00:00",
                    "view_count": 99999,
                    "like_count": 500,
                    "author": "old_user",
                    "url": "https://xueqiu.com/1/old1",
                    "title": "Out of window post",
                    "text": "Should stay in raw JSON only",
                },
                {
                    "id": "new_low",
                    "created_at": "2026-10-14T09:00:00",
                    "view_count": 300,
                    "like_count": 10,
                    "author": "u2",
                    "url": "https://xueqiu.com/1/new_low",
                    "title": "美团骑手激励",
                    "text": "补贴变化平稳",
                },
                {
                    "id": "new_high",
                    "created_at": "2026-10-15T10:00:00",
                    "view_count": 4200,
                    "like_count": 90,
                    "author": "u1",
                    "url": "https://xueqiu.com/1/new_high",
                    "title": "美团闪购增速",
                    "text": "即时零售客单价提升",
                },
            ]
        },
    }

    def fake_caller(tool: str, req: dict) -> dict:
        if req["ticker"] == "03690":
            return raw_meituan
        return {"ok": False, "error": "upstream_timeout"}

    cli.cmd_xq(
        SimpleNamespace(
            targets=targets_rel,
            run_dir=run_rel,
            since="2026-10-08",
            only=None,
            tier="Core",
            pages=2,
            workspace_dir=str(tmp_path),
        ),
        caller=fake_caller,
    )
    xq_out = capsys.readouterr().out
    assert "3690.HK 03690 ok=True raw=3 in_window=2" in xq_out
    assert "ERR 9988.HK 09988 ok=False error=upstream_timeout" in xq_out

    xq_dir = tmp_path / run_rel / "xq"
    saved_raw = json.loads((xq_dir / "03690.json").read_text(encoding="utf-8"))
    assert saved_raw == raw_meituan  # Raw response preserved untouched
    saved_summary = json.loads((xq_dir / "03690.summary.json").read_text(encoding="utf-8"))
    assert saved_summary["raw_count"] == 3 and saved_summary["in_window"] == 2
    assert [p["id"] for p in saved_summary["posts"]] == ["new_high", "new_low"]

    cli.cmd_xq_digest(
        SimpleNamespace(
            run_dir=run_rel, codes="03690,09988", n=6, len=160, since=None, workspace_dir=str(tmp_path)
        )
    )
    dig_out = capsys.readouterr().out
    assert "===== 03690 2" in dig_out
    assert dig_out.index("new_high") < dig_out.index("new_low")
    assert "old1" not in dig_out
    assert "== 09988 ERR ok=False upstream_timeout" in dig_out


def test_feishu_partial_create_chunked_append_and_failure_dedup(tmp_path: Path) -> None:
    # 1. Partial failure (ok=False WITH document_id) -> save doc.json, body_incomplete=True, create=False
    end_partial = dt.date(2026, 11, 6)
    partial_rec = {
        "ok": False,
        "error": "content_insert_failed",
        "document_id": "DoxPartial1",
        "url": "https://feishu.cn/docx/DoxPartial1",
    }
    saved = cli.save_doc_record(
        tmp_path, end_partial, json.dumps(partial_rec).encode("utf-8"), total_parts=1, attempt_id="att-p1"
    )
    assert saved["stored"] is True and saved["create"] is False and saved["body_incomplete"] is True
    assert saved["pending_parts"] == [1]
    assert saved["unverified_parts"] == [1] and saved["needs_verify"] is True  # G4
    st = cli.doc_status_for(tmp_path, end_partial)
    assert st["exists"] is True and st["create"] is False and st["body_incomplete"] is True
    assert st["unverified_parts"] == [1] and st["needs_verify"] is True

    # Verify part 1 against empty untruncated feishu_read JSON before repairing
    v_p1 = cli.verify_doc_append(
        tmp_path,
        end_partial,
        1,
        {"ok": True, "title": "Weekly", "content": "", "truncated": False, "total_chars": 0},
        part_text="**窗口与基准**\n区间 SPY +1.0%",
    )
    assert v_p1["ok"] is True and v_p1["present"] is False and v_p1["should_append"] is True
    repaired = cli.record_doc_append(tmp_path, end_partial, 1, json.dumps({"ok": True}).encode("utf-8"))
    assert repaired["ok"] is True and repaired["body_incomplete"] is False
    assert repaired["pending_parts"] == [] and repaired["unverified_parts"] == []
    assert cli.doc_status_for(tmp_path, end_partial)["body_incomplete"] is False

    # 2. Long doc 3-part create + append with interruption & idempotent resume
    end_multi = dt.date(2026, 11, 13)
    res1 = cli.save_doc_record(
        tmp_path,
        end_multi,
        json.dumps({"ok": True, "document_id": "DoxMulti3"}).encode("utf-8"),
        total_parts=3,
        attempt_id="att-m1",
    )
    assert res1["body_incomplete"] is True and res1["completed_parts"] == [1] and res1["pending_parts"] == [2, 3]
    p2 = cli.record_doc_append(tmp_path, end_multi, 2, json.dumps({"ok": True}).encode("utf-8"))
    assert p2["pending_parts"] == [3] and p2["body_incomplete"] is True
    resumed = cli.doc_status_for(tmp_path, end_multi)
    assert resumed["create"] is False and resumed["body_incomplete"] is True and resumed["pending_parts"] == [3]
    p2_dup = cli.record_doc_append(tmp_path, end_multi, 2, json.dumps({"ok": True}).encode("utf-8"))
    assert p2_dup["skipped"] is True and p2_dup["pending_parts"] == [3]
    p3 = cli.record_doc_append(tmp_path, end_multi, 3, json.dumps({"ok": True}).encode("utf-8"))
    assert p3["body_incomplete"] is False and p3["completed_parts"] == [1, 2, 3] and p3["pending_parts"] == []

    # 3. F3: Fail attempt counting by attempt_id (same attempt twice = 1; two attempts with identical bridge_unreachable = 2 & stop)
    end_fail = dt.date(2026, 11, 20)
    st_f1 = cli.doc_status_for(tmp_path, end_fail)
    assert st_f1["create"] is True and st_f1["attempt_id"]
    err_same = json.dumps({"ok": False, "error": "bridge_unreachable"}).encode("utf-8")
    a1 = cli.save_doc_record(tmp_path, end_fail, err_same, total_parts=2, attempt_id=st_f1["attempt_id"])
    a1_dup = cli.save_doc_record(tmp_path, end_fail, err_same, total_parts=2, attempt_id=st_f1["attempt_id"])
    assert a1["attempts"] == 1 and a1["duplicate"] is False
    assert a1_dup["attempts"] == 1 and a1_dup["duplicate"] is True and a1_dup["create"] is True
    st_f2 = cli.doc_status_for(tmp_path, end_fail)
    assert st_f2["create"] is True and st_f2["attempt_id"] != st_f1["attempt_id"]
    a2 = cli.save_doc_record(tmp_path, end_fail, err_same, total_parts=2, attempt_id=st_f2["attempt_id"])
    assert a2["attempts"] == 2 and a2["create"] is False and a2["reason"] == "create_failed_twice"
    assert cli.doc_status_for(tmp_path, end_fail)["create"] is False


def test_targets_excludes_uncovered_peers_and_wiki_matches_canonical_folders(tmp_path: Path) -> None:
    # M1: uncovered peer in Live Position with flags MUST NOT enter targets
    screen = {
        "ok": True,
        "rows": [
            {"name": "Google", "ticker": "GOOGL", "tier": "Core", "section": "Live Position", "flags": []},
            {"name": "UncoveredPeer", "ticker": "PEER1", "tier": "", "section": "Live Position", "flags": [{"type": "window"}]},
            {"name": "EchoStar", "ticker": "ECHO", "tier": "Watch", "section": "Live Position", "flags": []},
        ],
    }
    tg = cli.build_targets_from_screen(screen)
    assert [t["ticker"] for t in tg] == ["GOOGL", "ECHO"]

    # M2: wiki company TENCENT for 0700.HK & POP_MART for Pop Mart even when kw hits == 0
    caller = create_autospec(_caller_sig, spec_set=True)

    def fake_research(arguments: dict) -> dict:
        route, op = arguments.get("route"), arguments.get("operation")
        if route == "company-wiki" and op == "list":
            return {
                "ok": True,
                "data": {
                    "structuredContent": {
                        "results": [
                            {"document_id": "wiki:tencent_own", "company": "TENCENT", "date": "2026-10-15", "title": "Tencent memo"},
                            {"document_id": "wiki:pop_own", "company": "POP_MART", "date": "2026-10-15", "title": "Pop Mart memo"},
                            {"document_id": "wiki:other_co", "company": "OTHER_CO", "date": "2026-10-15", "title": "Other memo"},
                        ]
                    }
                },
            }
        if route == "all" and op == "read":
            return {"ok": True, "document": {"title": "Own archive", "date": "2026-10-15", "original_source": "company-wiki", "content": "Checklist without ticker token."}}
        return {"ok": True, "results": []}

    caller.side_effect = fake_research
    out, _, _ = sns.unified_scan(
        [
            {"name": "腾讯", "ticker": "0700.HK", "kw": ["腾讯", "0700.HK"]},
            {"name": "Pop Mart", "ticker": "9992.HK", "kw": ["Pop Mart", "9992.HK"]},
        ],
        "2026-10-08",
        "2026-10-17",
        caller=caller,
    )
    by_name = {r["name"]: r for r in out}
    assert [p["document_id"] for p in by_name["腾讯"]["picks"]] == ["wiki:tencent_own"]
    assert [p["document_id"] for p in by_name["Pop Mart"]["picks"]] == ["wiki:pop_own"]


def test_workspace_path_bounds_and_compute_window_et_modes(tmp_path: Path) -> None:
    # M3: reject relative traversal, outside absolute paths, and workspace symlinks; allow inside absolute paths
    ws = tmp_path / "ws"
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    ws.mkdir()
    (ws / "link_out").symlink_to(outside)

    with pytest.raises(ValueError, match="escapes workspace root"):
        cli._resolve_ws_path("../outside.json", str(ws))
    with pytest.raises(ValueError, match="escapes workspace root"):
        cli._resolve_ws_path(str(outside), str(ws))
    with pytest.raises(ValueError, match="symlink"):
        cli._resolve_ws_path("link_out", str(ws))
    assert cli._resolve_ws_path(str(ws / "state" / "ok.json"), str(ws)) == (ws / "state" / "ok.json").resolve()
    assert screen_movers._resolve_sheet_path(str(ws / "sheet.csv"), str(ws)) == (ws / "sheet.csv").resolve()
    with pytest.raises(ValueError, match="escapes workspace root"):
        screen_movers._resolve_sheet_path(str(outside), str(ws))

    # M7 + F4: Friday before 16:00 ET close in auto mode -> last Friday (never unclosed Friday); manual mode -> latest completed SPY bar
    spy_oct = _spy_bar_downloader(["2026-10-09", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"])
    win_fri_pre_auto = cli.compute_window(now_date="2026-10-16T10:30:00-04:00", mode="auto", downloader=spy_oct)
    assert win_fri_pre_auto["end"] == "2026-10-09" and win_fri_pre_auto["base"] == "2026-10-02"
    win_bj_fri_pre = cli.compute_window(now_date="2026-10-17T03:00:00+08:00", mode="auto", downloader=spy_oct)
    assert win_bj_fri_pre["end"] == "2026-10-09"
    win_fri_post_auto = cli.compute_window(now_date="2026-10-16T16:05:00-04:00", mode="auto", downloader=spy_oct)
    assert win_fri_post_auto["end"] == "2026-10-16" and win_fri_post_auto["base"] == "2026-10-09"

    win_wed_pre_manual = cli.compute_window(now_date="2026-10-14T11:00:00-04:00", mode="manual", downloader=spy_oct)
    assert win_wed_pre_manual["end"] == "2026-10-13" and win_wed_pre_manual["mode"] == "manual"
    win_wed_post_manual = cli.compute_window(now_date="2026-10-14T16:15:00-04:00", mode="manual", downloader=spy_oct)
    assert win_wed_post_manual["end"] == "2026-10-14"
    win_fri_pre_manual = cli.compute_window(now_date="2026-10-16T11:00:00-04:00", mode="manual", downloader=spy_oct)
    assert win_fri_pre_manual["end"] == "2026-10-15"
    win_mon_pre_manual = cli.compute_window(now_date="2026-10-19T09:00:00-04:00", mode="manual", downloader=spy_oct)
    assert win_mon_pre_manual["end"] == "2026-10-16"


def test_verify_append_readback_hit_and_miss_and_atomic_progress_f1_f2(tmp_path: Path) -> None:
    # F2: save-doc requires total_parts >= 1 explicitly; missing progress in doc.json forces body_incomplete=True
    end_d = dt.date(2026, 11, 27)
    raw_ok = json.dumps({"ok": True, "document_id": "DoxVerify1"}).encode("utf-8")
    no_parts = cli.save_doc_record(tmp_path, end_d, raw_ok, attempt_id="att-v1")
    assert no_parts["ok"] is False and no_parts["stored"] is False and "total_parts" in no_parts["error"]

    # Missing progress record -> body_incomplete=True, needs_verify=True (never guessed as 1-part done)
    end_legacy = dt.date(2026, 12, 4)
    legacy_file = tmp_path / cli.run_rel(end_legacy, cli.DOC_FILE)
    legacy_file.parent.mkdir(parents=True, exist_ok=True)
    legacy_file.write_text(json.dumps({"ok": True, "document_id": "DoxNoProgress"}), encoding="utf-8")
    st_legacy = cli.doc_status_for(tmp_path, end_legacy)
    assert st_legacy["exists"] is True and st_legacy["create"] is False
    assert st_legacy["body_incomplete"] is True and st_legacy["progress_missing"] is True
    assert st_legacy["needs_verify"] is True and st_legacy["reason"] == "progress_missing"

    # F1: outcome="unknown" puts part 2 in unverified_parts; verify-append handles HIT vs MISS
    res = cli.save_doc_record(tmp_path, end_d, raw_ok, total_parts=3, attempt_id="att-v2")
    assert res["completed_parts"] == [1] and res["pending_parts"] == [2, 3]
    unk = cli.record_doc_append(
        tmp_path, end_d, 2, json.dumps({"ok": False, "outcome": "unknown", "error": "timeout"}).encode("utf-8")
    )
    assert unk["ok"] is False and unk["unverified"] is True and unk["unverified_parts"] == [2]
    st_mid = cli.doc_status_for(tmp_path, end_d)
    assert st_mid["unverified_parts"] == [2] and st_mid["needs_verify"] is True

    part2_md = (
        "**Watch 异动汇总**\n"
        + "行业事件详细梳理：多家云厂商资本开支指引超预期，半导体与光模块板块联动上行。"
        + " 结尾结论：下周跟踪季报与定价政策确认。"
    )
    part3_md = (
        "**其余 Watch 公司行情对比表**\n"
        + "EchoStar 与 D.R. Horton 持仓跟踪列于表首，其余 Watch 标的列区间涨跌与超额收益。\n"
        + "**口径与缺口**：雪球为近期帖子抽样。"
    )
    # Simulated feishu_read JSON already contains part 2 (even without markdown **) and lacks part 3
    readback_doc = (
        "窗口与基准：SPY +1.2%\n"
        "Watch 异动汇总\n"
        "行业事件详细梳理：多家云厂商资本开支指引超预期，半导体与光模块板块联动上行。 "
        "结尾结论：下周跟踪季报与定价政策确认。\n"
    )
    readback_json = {
        "ok": True,
        "title": "Richard 覆盖周报",
        "content": readback_doc,
        "truncated": False,
        "total_chars": len(readback_doc),
    }
    hit = cli.verify_doc_append(tmp_path, end_d, 2, readback_json, part_text=part2_md)
    assert hit["ok"] is True and hit["present"] is True and hit["should_append"] is False
    assert hit["action"] == "marked_completed"
    assert hit["completed_parts"] == [1, 2] and hit["unverified_parts"] == [] and hit["pending_parts"] == [3]

    miss = cli.verify_doc_append(tmp_path, end_d, 3, readback_json, part_text=part3_md)
    assert miss["ok"] is True and miss["present"] is False and miss["should_append"] is True
    assert miss["action"] == "append_required"
    assert miss["pending_parts"] == [3] and miss["body_incomplete"] is True

    # Confirm CLI parser enforces required --total-parts and --attempt-id on save-doc
    with pytest.raises(SystemExit) as exc:
        monkey_argv = [
            "prog",
            "save-doc",
            "--end",
            "2026-11-27",
            "--workspace-dir",
            str(tmp_path),
            "--json-str",
            '{"ok":true,"document_id":"X"}',
        ]
        old_argv = sys.argv
        try:
            sys.argv = monkey_argv
            cli.main()
        finally:
            sys.argv = old_argv
    assert exc.value.code == 2


def test_verify_append_truncation_partial_middle_and_invalid_record_g1_g2_g3(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    end_d = dt.date(2026, 12, 11)
    cli.save_doc_record(
        tmp_path,
        end_d,
        json.dumps({"ok": True, "document_id": "DoxG123"}).encode("utf-8"),
        total_parts=4,
        attempt_id="att-g1",
    )

    # G3: unparseable JSON and non-dict JSON in record_doc_append must mark parts unverified (not ordinary pending)
    bad_syntax = cli.record_doc_append(tmp_path, end_d, 2, b"{not-valid-json")
    assert bad_syntax["ok"] is False and bad_syntax["stored"] is True and bad_syntax["unverified"] is True
    assert 2 in bad_syntax["unverified_parts"] and bad_syntax["needs_verify"] is True

    non_dict = cli.record_doc_append(tmp_path, end_d, 3, b'["unexpected", "list"]')
    assert non_dict["ok"] is False and non_dict["stored"] is True and non_dict["unverified"] is True
    st = cli.doc_status_for(tmp_path, end_d)
    assert st["unverified_parts"] == [2, 3] and st["needs_verify"] is True

    # G2: Head and tail match identically while middle differs/missing -> partial match -> indeterminate, never completed or re-appended
    part2_full = (
        "**Watch 异动板块深度复盘与行业主线梳理**\n"
        "第一部分说明云厂商三季度资本开支维持高增速，算力芯片需求旺盛。\n"
        "中间独有关键段落：某某光学模块大厂上调全年 800G 出货指引 35% 并锁定长约价格，推动板块盈利预期显著抬升。\n"
        "结尾追踪下周龙头业绩电话会与管理层指引变化，保持重点关注。\n"
    )
    doc_missing_middle = (
        "窗口与基准：SPY +0.8%\n"
        "Watch 异动板块深度复盘与行业主线梳理\n"
        "第一部分说明云厂商三季度资本开支维持高增速，算力芯片需求旺盛。\n"
        "【此处中间正文缺失】\n"
        "结尾追踪下周龙头业绩电话会与管理层指引变化，保持重点关注。\n"
    )
    partial_res = cli.verify_doc_append(
        tmp_path,
        end_d,
        2,
        {
            "ok": True,
            "title": "Richard 覆盖周报",
            "content": doc_missing_middle,
            "truncated": False,
            "total_chars": len(doc_missing_middle),
        },
        part_text=part2_full,
    )
    assert partial_res["ok"] is False
    assert partial_res["indeterminate"] is True and partial_res["partial_match"] is True
    assert partial_res["should_append"] is False and partial_res["present"] is None
    assert partial_res["error_code"] == "readback_partial_match"
    assert partial_res["action"] == "stop_and_manual_check"
    assert 2 in partial_res["unverified_parts"] and 2 not in partial_res["completed_parts"]

    # G1: truncated=True and segment not found -> indeterminate with explicit error_code & non-zero CLI exit
    part3_text = (
        "**其余 Watch 公司对比表与未检索持仓跟踪说明**\n"
        "EchoStar、D.R. Horton 与唯品会列于表首。\n"
        "口径与缺口：本周雪球均为近期帖子抽样。\n"
    )
    read_file = tmp_path / cli.run_rel(end_d, "feishu-read.json")
    read_file.parent.mkdir(parents=True, exist_ok=True)
    read_file.write_text(
        json.dumps(
            {
                "ok": True,
                "title": "Richard 覆盖周报",
                "content": doc_missing_middle,
                "truncated": True,
                "total_chars": 65432,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    part3_file = tmp_path / cli.run_rel(end_d, "part_3.md")
    part3_file.write_text(part3_text, encoding="utf-8")

    capsys.readouterr()
    with pytest.raises(SystemExit) as exit_err:
        old_argv = sys.argv
        try:
            sys.argv = [
                "prog",
                "verify-append",
                "--workspace-dir",
                str(tmp_path),
                "--end",
                end_d.isoformat(),
                "--part",
                "3",
                "--part-file",
                cli.run_rel(end_d, "part_3.md"),
                "--doc-json-file",
                cli.run_rel(end_d, "feishu-read.json"),
            ]
            cli.main()
        finally:
            sys.argv = old_argv
    assert exit_err.value.code != 0
    cli_out = json.loads(capsys.readouterr().out)
    assert cli_out["ok"] is False and cli_out["indeterminate"] is True
    assert cli_out["should_append"] is False and cli_out["error_code"] == "readback_truncated_not_found"
    assert 3 in cli_out["unverified_parts"]

    # G1: even when truncated=True, if full normalized segment IS completely present in expected content -> completed!
    full_hit_even_truncated = cli.verify_doc_append(
        tmp_path,
        end_d,
        4,
        {
            "ok": True,
            "title": "Richard 覆盖周报",
            "content": "前置内容\n其余 Watch 公司对比表与未检索持仓跟踪说明\nEchoStar、D.R. Horton 与唯品会列于表首。\n口径与缺口：本周雪球均为近期帖子抽样。",
            "truncated": True,
            "total_chars": 58000,
        },
        part_text=part3_text,
    )
    assert full_hit_even_truncated["ok"] is True and full_hit_even_truncated["present"] is True
    assert full_hit_even_truncated["should_append"] is False and full_hit_even_truncated["action"] == "marked_completed"


def test_compute_window_uses_spy_bars_skips_holidays_and_fails_closed_f4() -> None:
    # Holiday 2026-01-01 (Thu): manual on 2026-01-02 morning (<16:00 ET) must roll back to 2025-12-31, never 2026-01-01
    spy_ny = _spy_bar_downloader(["2025-12-24", "2025-12-30", "2025-12-31", "2026-01-02"])
    win_ny = cli.compute_window(now_date="2026-01-02T10:00:00-05:00", mode="manual", downloader=spy_ny)
    assert win_ny["ok"] is True and win_ny["end"] == "2025-12-31"

    # Good Friday 2026-04-03 holiday: auto mode on Sunday 2026-04-05 must pick Thursday 2026-04-02
    spy_gf = _spy_bar_downloader(["2026-03-27", "2026-03-31", "2026-04-01", "2026-04-02"])
    win_gf = cli.compute_window(now_date="2026-04-05", mode="auto", downloader=spy_gf)
    assert win_gf["ok"] is True and win_gf["end"] == "2026-04-02" and win_gf["base"] == "2026-03-26"

    # Failed / empty SPY download must return ok=False without guessing
    def broken_dl(tickers: list[str], start: str, end: str) -> pd.DataFrame:
        raise RuntimeError("yahoo unreachable")

    err_win = cli.compute_window(now_date="2026-10-18", mode="auto", downloader=broken_dl)
    assert err_win["ok"] is False and "yahoo unreachable" in err_win["error"]

    empty_win = cli.compute_window(
        now_date="2026-10-18",
        mode="manual",
        downloader=lambda t, s, e: pd.DataFrame(),
    )
    assert empty_win["ok"] is False and "error" in empty_win


def test_transport_mcp_client_fallback_uses_call_mcp_tool_with_error_flag_f5(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = cli._transport
    calls: list[tuple] = []

    class FakeMCPClient:
        @staticmethod
        def call_mcp_tool_with_error_flag(server: str, tool_name: str, arguments: dict) -> tuple[bool, object]:
            calls.append((server, tool_name, arguments))
            if arguments.get("fail"):
                return True, json.dumps({"message": "bridge timeout"})
            return False, json.dumps({"ok": True, "data": {"items": [{"id": "42"}]}})

    monkeypatch.setattr(transport, "resolve_researchdata_transport", lambda: ("mcp_client", FakeMCPClient))
    ok_res = transport.call_bridge_jsonrpc("community", {"route": "x-timeline"})
    assert ok_res["ok"] is True and transport.extract_route_items(ok_res) == [{"id": "42"}]
    err_res = transport.call_bridge_jsonrpc("community", {"fail": True})
    assert err_res["ok"] is False and err_res["error"] == "bridge timeout"
    assert calls[0][0] == "researchdata" and calls[0][1] == "community"
