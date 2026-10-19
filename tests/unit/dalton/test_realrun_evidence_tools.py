"""Unit tests for P0I §2 real-run evidence tools and report claim checker."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.dalton.check_report_claims import REPO_ROOT, scan_reports
from scripts.dalton.realrun._common import (
    assert_no_credentials_in_obj,
    build_evidence_envelope,
    compute_file_sha256,
    validate_synthetic_declarations,
    write_evidence_and_summary,
)
from scripts.dalton.realrun.journal_dump import (
    normalize_journal_rows,
    parse_iso_utc,
    sanitize_usage_metadata,
)


def test_journal_dump_normalize_rows_and_strip_body_fields() -> None:
    since_dt = parse_iso_utc("2026-10-19T05:00:00Z")
    raw_rows = [
        {
            "invocation_id": "inv-001",
            "user_id": "realrun-g1-user",
            "scope_ref": "user:realrun-g1-user",
            "purpose": "audit.judge",
            "model_key": "gpt-6-astra",
            "provider": "openclaw-broker",
            "billing_type": "subscription",
            "state": "done",
            "attempt": 1,
            "replayed": False,
            "cached": False,
            "usage": {
                "input_tokens": 1200,
                "output_tokens": 350,
                "total_tokens": 1550,
                "raw_response_body": "MUST_BE_STRIPPED",
            },
            "cash_usd": Decimal("0.000000"),
            "notional_usd": Decimal("0.014200"),
            "error_kind": None,
            "claimed_at": datetime(2026, 10, 19, 5, 1, 0, tzinfo=timezone.utc),
            "completed_at": datetime(2026, 10, 19, 5, 1, 2, tzinfo=timezone.utc),
            "created_at": datetime(2026, 10, 19, 5, 1, 0, tzinfo=timezone.utc),
        },
        {
            "invocation_id": "inv-002-stale",
            "user_id": "realrun-g1-user",
            "scope_ref": "user:realrun-g1-user",
            "purpose": "audit.label_figures",
            "model_key": "ocb-deepseek-deepseek-flash",
            "provider": "openclaw-broker",
            "billing_type": "cash",
            "state": "done",
            "attempt": 1,
            "replayed": False,
            "cached": False,
            "usage": {"prompt_tokens": 400, "completion_tokens": 100},
            "cash_usd": Decimal("0.001200"),
            "notional_usd": Decimal("0.001200"),
            "error_kind": None,
            "claimed_at": datetime(2026, 10, 19, 4, 50, 0, tzinfo=timezone.utc),
            "completed_at": datetime(2026, 10, 19, 4, 50, 1, tzinfo=timezone.utc),
            "created_at": datetime(2026, 10, 19, 4, 50, 0, tzinfo=timezone.utc),
        },
    ]

    cleaned_usage = sanitize_usage_metadata(raw_rows[0]["usage"])
    assert "raw_response_body" not in cleaned_usage
    assert cleaned_usage["total_tokens"] == 1550

    normalized = normalize_journal_rows(raw_rows, since_dt=since_dt)
    assert normalized["totals"]["total_calls"] == 2
    assert normalized["totals"]["fresh_calls"] == 1
    assert normalized["totals"]["replayed_calls"] == 1
    assert normalized["totals"]["done_calls"] == 2
    assert normalized["totals"]["cash_usd"] == pytest.approx(0.0012)
    assert normalized["totals"]["notional_usd"] == pytest.approx(0.0154)
    assert normalized["rows"][0]["replayed"] is False
    assert normalized["rows"][1]["replayed"] is True


def test_common_synthetic_validation_and_artifact_writing(tmp_path: Path) -> None:
    valid_synthetic = [
        {
            "kind": "settings_api_budget",
            "entry_point": "PUT /api/v1/dalton/settings",
            "description": "Set daily_cash_cap_usd=1.0 for G1 test user",
        },
        "bridge_delay_hook:sleep_s=242",
    ]
    validated = validate_synthetic_declarations(valid_synthetic)
    assert len(validated) == 2

    with pytest.raises(ValueError, match="not in ALLOWED_SYNTHETIC_KINDS"):
        validate_synthetic_declarations(["cached_llm_replay:fake"])

    with pytest.raises(ValueError, match="Forbidden synthetic injection"):
        validate_synthetic_declarations(["direct_signal_insert:dalton_watch_signals"])

    with pytest.raises(ValueError, match="secret token pattern"):
        assert_no_credentials_in_obj({"token": "sk-1234567890abcdefghijklmnop"})

    fake_journal = {
        "rows": [
            {
                "invocation_id": "inv-001",
                "purpose": "audit.judge",
                "model_key": "gpt-6-astra",
                "state": "done",
                "replayed": False,
                "total_tokens": 1550,
                "cash_usd": 0.0,
                "notional_usd": 0.0142,
                "created_at": "2026-10-19T05:01:00Z",
            }
        ],
        "totals": {
            "total_calls": 1,
            "fresh_calls": 1,
            "replayed_calls": 0,
            "done_calls": 1,
            "failed_calls": 0,
            "indeterminate_calls": 0,
            "cash_usd": 0.0,
            "notional_usd": 0.0142,
            "conversation_turns": 0,
            "conversation_cash_usd": 0.0,
            "combined_cash_usd": 0.0,
        },
    }

    envelope = build_evidence_envelope(
        script="scripts/dalton/realrun/g1_budget.py",
        started_at="2026-10-19T05:00:00Z",
        ended_at="2026-10-19T05:02:00Z",
        backend="http://127.0.0.1:8000",
        test_user_id="realrun-g1-20261019-abcd1234",
        synthetic=valid_synthetic,
        journal=fake_journal,
        extra_fields={"scenarios": {"cap_enforced": True}},
    )

    raw_path = tmp_path / "var" / "g1_budget.json"
    md_path = tmp_path / "docs" / "G1.md"
    written_raw, written_md, sha256_hex = write_evidence_and_summary(
        family_code="G1",
        title="预算三连真实运行验证",
        raw_json_path=raw_path,
        summary_md_path=md_path,
        envelope=envelope,
        key_findings=[("日额度阻断", "402 budget_exceeded")],
        notes=["全部通过真实后端接口执行"],
    )
    assert written_raw.is_file()
    assert written_md.is_file()
    assert sha256_hex == compute_file_sha256(written_raw)
    md_text = written_md.read_text(encoding="utf-8")
    assert sha256_hex in md_text
    assert "`settings_api_budget`" in md_text


def test_check_report_claims_catches_p0h_and_progress_violations(
    tmp_path: Path,
) -> None:
    p0h_report = REPO_ROOT / "docs/dalton/dev-guide/P0H-round2-dev-report.md"
    bad_var_md = tmp_path / "bad-progress.md"
    bad_var_md.write_text(
        "# Bad Progress\n\n"
        "- Old missing path 1: `var/dalton/catalog_openclaw.json`\n"
        "- Old missing path 2: `var/dalton/catalog_native.json`\n",
        encoding="utf-8",
    )

    result = scan_reports([p0h_report, bad_var_md], repo_root=REPO_ROOT)
    assert result["summary"]["all_clean"] is False

    p0h_entry = result["reports"][0]
    missing_ids = {item["identifier"] for item in p0h_entry["missing_identifiers"]}
    assert "_METRIC_SYNONYM_GROUPS" in missing_ids
    assert "_LLM_JUDGE_SCHEMA" in missing_ids
    assert "JournalCashQueryError" in missing_ids
    assert "_CODE_FENCE_RE" in missing_ids

    bad_var_entry = result["reports"][1]
    missing_vars = {item["path"] for item in bad_var_entry["missing_var_paths"]}
    assert "var/dalton/catalog_openclaw.json" in missing_vars
    assert "var/dalton/catalog_native.json" in missing_vars

    progress_md = REPO_ROOT / "docs/dalton/dev-guide/PROGRESS.md"
    p0i_report = REPO_ROOT / "docs/dalton/dev-guide/P0I-round3-dev-report.md"
    clean_res = scan_reports([progress_md, p0i_report], repo_root=REPO_ROOT)
    assert clean_res["summary"]["all_clean"] is True
