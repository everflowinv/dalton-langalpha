"""Original AI-finance signal schema. Synthetic rows only; no network or shell."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.dalton import ai_finance_signals as signals
from scripts.dalton.ai_finance.ai_finance_daily_validate import validate_records

EVENT = "2026-11-11T12:00:00+08:00"
DAY = "2026-11-11"
REPO = "acme/alpha"


def _reason(result: dict[str, object] | None) -> str | None:
    if result is None:
        return None
    reason = result["reason"]
    assert isinstance(reason, str)
    return reason


def _disposition(doc_id: str, *, full: bool = True) -> dict[str, object]:
    row: dict[str, object] = {"doc_id": doc_id, "disposition": "signal"}
    if full:
        row.update({
            "entity": "Example Co",
            "signal_direction": "mixed",
            "evidence_status": "actual",
            "reason": "a signed deployment changed the cost line",
            "key_evidence": "management described a production deployment",
            "qa_checked": True,
        })
    return row


def _decision(repo: str = REPO, *, metadata: bool = True) -> dict[str, object]:
    row: dict[str, object] = {
        "repo": repo,
        "decision": "include",
        "reason": "the readme shows a product with a direct spending pool",
        "readme_url": f"https://github.com/{repo}",
        "displayed": True,
    }
    if metadata:
        row["upstream"] = "example-runtime"
        row["upstream_evidence"] = "README names the runtime"
    return row


def _candidate(repo: str = REPO) -> dict[str, object]:
    return {"full_name": repo, "history_verified": True, "report_eligible": True}


def _earnings(doc_id: str, *, empty_qa: bool = False) -> dict[str, object]:
    return {
        "record_key": f"earnings:{doc_id}",
        "source_type": "earnings",
        "external_id": doc_id,
        "event_time": EVENT,
        "title": "Example earnings",
        "entity": "Example Co",
        "industry": "software",
        "url": "",
        "summary": "A production deployment changed the cost line",
        "evidence": "management described the deployment",
        "metrics": [],
        "signal_types": ["cost_productivity"],
        "signal_direction": "positive",
        "evidence_status": "actual",
        "qa_concern": "" if empty_qa else "an analyst asked about margin",
        "management_response": "" if empty_qa else "management gave a direct figure",
        "management_response_status": "" if empty_qa else "direct",
        "tam_mapping": "existing software spend",
        "investment_implication": "track the deployment KPI",
        "tags": ["ai"],
    }


def _github(repo: str = REPO) -> dict[str, object]:
    return {
        "record_key": f"github:{repo}:{DAY}",
        "source_type": "github",
        "external_id": repo,
        "event_time": EVENT,
        "title": "Example repo",
        "entity": repo,
        "industry": "software",
        "url": f"https://github.com/{repo}",
        "summary": "the project packages a runtime other repos adopt",
        "evidence": "README names the runtime",
        "metrics": [],
        "signal_types": ["developer_tooling"],
        "signal_direction": "positive",
        "tam_mapping": "developer tools",
        "investment_implication": "track adoption of the runtime",
        "tags": ["upstream:example-runtime"],
    }


def test_original_disposition_metadata_and_rejections() -> None:
    assert signals._check_dispositions([_disposition("doc-1")]) is None
    assert signals._check_dispositions([
        {"doc_id": "doc-signal", "disposition": "signal", "reason": "material"},
        {"doc_id": "doc-watch", "disposition": "watchlist"},
    ]) is None

    arbitrary = _disposition("doc-1")
    arbitrary["analyst_note"] = "extra"
    assert _reason(signals._check_dispositions([arbitrary])) == "bundle_invalid"

    for key in ("transcript", "path", "full_text"):
        leaked = _disposition("doc-1")
        leaked[key] = "stored call"
        assert _reason(signals._check_dispositions([leaked])) == "transcript_rejected"

    bad_flag = _disposition("doc-1")
    bad_flag["qa_checked"] = "yes"
    assert _reason(signals._check_dispositions([bad_flag])) == "bundle_invalid"
    bad_direction = _disposition("doc-1")
    bad_direction["signal_direction"] = "neutral"
    assert _reason(signals._check_dispositions([bad_direction])) == "bundle_invalid"
    empty_evidence = _disposition("doc-1")
    empty_evidence["key_evidence"] = "  "
    assert _reason(signals._check_dispositions([empty_evidence])) == "bundle_invalid"
    assert _reason(signals._check_dispositions([_disposition(f"d{i:02d}") for i in range(81)])) == "bundle_oversize"


def test_original_decision_metadata_and_rejections() -> None:
    candidates = [_candidate()]
    assert signals._check_decisions([_decision()], candidates) is None
    minimal = {
        "repo": REPO,
        "decision": "include",
        "reason": "eligible",
        "readme_url": f"https://github.com/{REPO}",
    }
    assert signals._check_decisions([minimal], candidates) is None

    arbitrary = _decision()
    arbitrary["score"] = 1
    assert _reason(signals._check_decisions([arbitrary], candidates)) == "github_decision_invalid"
    for value in (["example-runtime"], {"name": "example-runtime"}, ""):
        bad_type = _decision()
        bad_type["upstream"] = value
        assert _reason(signals._check_decisions([bad_type], candidates)) == "github_decision_invalid"
    leaked = _decision()
    leaked["path"] = "temp/readme.txt"
    assert _reason(signals._check_decisions([leaked], candidates)) == "transcript_rejected"


def test_original_identity_and_empty_qa() -> None:
    decisions = [_decision()]
    candidates = [_candidate()]
    earnings = _earnings("doc-1", empty_qa=True)
    github = _github()
    assert signals._check_signal_fields([earnings, github], decisions, candidates) is None

    dated = _earnings("doc-1")
    dated["record_key"] = f"earnings:doc-1:{DAY}"
    assert _reason(signals._check_signal_fields([dated], [], [])) == "record_key_invalid"
    mismatched = _earnings("doc-1")
    mismatched["record_key"] = "earnings:other-doc"
    assert _reason(signals._check_signal_fields([mismatched], [], [])) == "record_key_invalid"
    arbitrary = _earnings("doc-1")
    arbitrary["record_key"] = "note:doc-1"
    assert _reason(signals._check_signal_fields([arbitrary], [], [])) == "record_key_invalid"

    missing_date = _github()
    missing_date["record_key"] = f"github:{REPO}"
    assert _reason(signals._check_signal_fields([missing_date], decisions, candidates)) == "record_key_invalid"
    wrong_date = _github()
    wrong_date["record_key"] = f"github:{REPO}:2026-11-12"
    assert _reason(signals._check_signal_fields([wrong_date], decisions, candidates)) == "record_key_invalid"
    wrong_repo = _github()
    wrong_repo["record_key"] = f"github:other/repo:{DAY}"
    assert _reason(signals._check_signal_fields([wrong_repo], decisions, candidates)) == "record_key_invalid"

    partial_empty = _earnings("doc-1", empty_qa=True)
    partial_empty["qa_concern"] = "an analyst asked about margin"
    assert _reason(signals._check_signal_fields([partial_empty], [], [])) == "signal_fields_invalid"
    missing_response = _earnings("doc-1")
    missing_response["management_response"] = ""
    assert _reason(signals._check_signal_fields([missing_response], [], [])) == "signal_fields_invalid"
    evasive = _earnings("doc-1")
    evasive["management_response"] = ""
    evasive["management_response_status"] = "evasive_or_unanswered"
    assert signals._check_signal_fields([evasive], [], []) is None
    bad_status = _earnings("doc-1")
    bad_status["management_response_status"] = "unclear"
    assert _reason(signals._check_signal_fields([bad_status], [], [])) == "signal_fields_invalid"

    leaked = _earnings("doc-1")
    leaked["metrics"] = [{"transcript": "stored call"}]
    assert _reason(signals._check_signal_fields([leaked], [], [])) == "transcript_rejected"
    extra = _earnings("doc-1")
    extra["analyst_note"] = "extra"
    assert _reason(signals._check_signal_fields([extra], [], [])) == "signal_fields_invalid"


def test_eighty_earnings_plus_github_passes_original_validator(tmp_path: Path) -> None:
    doc_ids = [f"d{index:02d}" for index in range(80)]
    dispositions = [_disposition(doc_id) for doc_id in doc_ids]
    rows = [_earnings(doc_id, empty_qa=index == 0) for index, doc_id in enumerate(doc_ids)]
    rows.append(_github())
    decisions = [_decision()]
    candidates = [_candidate()]

    assert signals._check_dispositions(dispositions) is None
    assert signals._check_decisions(decisions, candidates) is None
    assert signals._check_signal_fields(rows, decisions, candidates) is None
    loaded = signals._records({"records": rows}, "signal_records")
    assert isinstance(loaded, list) and len(loaded) == 81

    scan = tmp_path / "scan.json"
    disposition_path = tmp_path / "dispositions.json"
    signal_path = tmp_path / "signals.json"
    decision_path = tmp_path / "decisions.json"
    scan.write_text(json.dumps({
        "coverage": {"ai_candidates": 80},
        "candidates": [{"doc_id": doc_id} for doc_id in doc_ids],
    }), encoding="utf-8")
    disposition_path.write_text(json.dumps({"records": dispositions}), encoding="utf-8")
    signal_path.write_text(json.dumps({"records": rows}), encoding="utf-8")
    decision_path.write_text(json.dumps({"records": decisions}), encoding="utf-8")
    violations: list[str] = []
    validate_records(scan, disposition_path, signal_path, violations, decision_path)
    assert violations == []

    assert _reason(signals._check_signal_fields(
        [_earnings(f"e{index:02d}") for index in range(81)],
        [],
        [],
    )) == "bundle_oversize"
    oversize = signals._records(
        {"records": [_earnings(f"e{index:03d}") for index in range(257)]},
        "signal_records",
    )
    assert isinstance(oversize, dict) and oversize["reason"] == "bundle_oversize"
