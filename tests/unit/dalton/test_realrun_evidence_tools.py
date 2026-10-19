"""Unit tests for P0I §2 / P0J §1 real-run evidence tools, v2 envelope, and report claim checker."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
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
from scripts.dalton.realrun.check_env import scan_realrun_scripts
from scripts.dalton.realrun.journal_dump import (
    normalize_journal_rows,
    parse_iso_utc,
    sanitize_usage_metadata,
)
from scripts.dalton.realrun.render_report_tables import (
    GENERATED_END_MARKER,
    GENERATED_START_MARKER,
    compare_legacy_report,
    render_evidence_table_block,
    update_markdown_generated_block,
    verify_generated_evidence_block,
)
from src.config.env import LOCAL_DEV_USER_ID
from src.dalton.settings import DaltonConfig, get_dalton_config, get_pm_user_ids_source


def test_pm_user_ids_single_definition_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DALTON_PM_USER_IDS", raising=False)
    default_cfg = DaltonConfig()
    assert default_cfg.pm_user_ids == [LOCAL_DEV_USER_ID]
    assert "local_dev_user" not in default_cfg.pm_user_ids
    assert "default" not in default_cfg.pm_user_ids

    monkeypatch.setenv("DALTON_PM_USER_IDS", "realrun-pm-a, another-pm ")
    cfg = get_dalton_config()
    assert cfg.pm_user_ids == ["realrun-pm-a", "another-pm"]
    assert get_pm_user_ids_source() == "env"


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
            "entry_point": "PUT /api/v1/dalton/budget",
            "description": "Set daily_cash_cap_usd=1.0 for G1 test user",
        },
        "committed_test_hook:scripts/dalton/researchdata_bridge.py",
    ]
    validated = validate_synthetic_declarations(valid_synthetic)
    assert len(validated) == 2

    with pytest.raises(ValueError, match="not in ALLOWED_SYNTHETIC_KINDS"):
        validate_synthetic_declarations(["bridge_delay_hook:sleep_s=242"])

    with pytest.raises(ValueError, match="Forbidden synthetic injection"):
        validate_synthetic_declarations(["registered_test_hook:fake"])

    with pytest.raises(ValueError, match="Forbidden synthetic injection"):
        validate_synthetic_declarations(["direct_signal_insert:dalton_watch_signals"])

    with pytest.raises(ValueError, match="not registered in COMMITTED_TEST_HOOKS"):
        validate_synthetic_declarations(
            [{"kind": "committed_test_hook", "entry_point": "src/dalton/unregistered.py"}]
        )

    with pytest.raises(ValueError, match="secret token pattern"):
        assert_no_credentials_in_obj({"token": "sk-1234567890abcdefghijklmnop"})

    with pytest.raises(ValueError, match="email address pattern"):
        assert_no_credentials_in_obj({"user_email": "analyst@example.com"})

    fake_journal = {
        "rows": [
            {
                "invocation_id": "inv-001",
                "user_id": "realrun-g1-20261019-abcd1234",
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
        "conversation_usages": [],
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
            "conversation_usages_unavailable": False,
            "unexpected_user_ids": [],
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
    assert envelope["evidence_version"] == 2
    assert envelope["identity_mode"] == "service_token"
    assert envelope["valid"] is True
    assert envelope["invalid_reasons"] == []
    assert len(envelope["script_sha256"]) == 64

    # Verify conversation_usages_unavailable or local-dev-user marks valid=False
    bad_journal = {
        "rows": [{"invocation_id": "inv-bad", "user_id": "local-dev-user"}],
        "conversation_usages": [],
        "totals": {"conversation_usages_unavailable": True},
    }
    bad_env = build_evidence_envelope(
        script="scripts/dalton/realrun/g1_budget.py",
        started_at="2026-10-19T05:00:00Z",
        ended_at="2026-10-19T05:02:00Z",
        backend="http://127.0.0.1:8000",
        test_user_id="realrun-g1-20261019-abcd1234",
        synthetic=[],
        journal=bad_journal,
    )
    assert bad_env["valid"] is False
    assert "conversation_usages_unavailable" in bad_env["invalid_reasons"]
    assert "journal_row_owned_by_local_dev_user" in bad_env["invalid_reasons"]

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


def test_check_report_claims_and_generated_evidence_table_on_sample_files(
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

    # Fixed clean sample file (not bound to live PROGRESS.md or P0I-round3-dev-report.md, R4-families #11)
    sample_repo = tmp_path / "sample_repo"
    (sample_repo / "src" / "dalton").mkdir(parents=True)
    (sample_repo / "src" / "dalton" / "sample_mod.py").write_text(
        "def verify_sample_claim() -> bool:\n"
        "    git_sha = test_user_id = combined_cash_usd = notional_usd = 1\n"
        "    return bool(git_sha and test_user_id and combined_cash_usd and notional_usd)\n",
        encoding="utf-8",
    )
    (sample_repo / "scripts" / "dalton" / "realrun").mkdir(parents=True)
    (sample_repo / "scripts" / "dalton" / "realrun" / "g1_budget.py").write_text(
        "# sample g1_budget.py\n",
        encoding="utf-8",
    )
    raw_dir = sample_repo / "var" / "dalton" / "trial-runs" / "realrun-20261019"
    raw_dir.mkdir(parents=True)
    g1_json = raw_dir / "g1_budget.json"
    g1_payload = {
        "evidence_version": 2,
        "git_sha": "1234abcd",
        "git_dirty": False,
        "backend_git_sha": "1234abcd",
        "backend_git_dirty": False,
        "test_user_id": "realrun-g1-a,realrun-g1-b",
        "valid": True,
        "journal": {
            "totals": {
                "total_calls": 3,
                "fresh_calls": 3,
                "replayed_calls": 0,
                "conversation_turns": 1,
                "cash_usd": 0.0015,
                "notional_usd": 0.012,
                "combined_cash_usd": 0.0015,
            }
        },
        "synthetic": [{"kind": "settings_api_budget", "entry_point": "PUT /api/v1/dalton/budget"}],
    }
    g1_json.write_text(json.dumps(g1_payload, indent=2) + "\n", encoding="utf-8")
    g1_sha = compute_file_sha256(g1_json)

    clean_sample_md = sample_repo / "docs" / "sample-clean-report.md"
    clean_sample_md.parent.mkdir(parents=True)
    clean_sample_md.write_text(
        "# Clean Sample Report\n\n"
        "- Verified symbol: `verify_sample_claim` in `src/dalton/sample_mod.py`\n"
        f"- Raw JSON: `var/dalton/trial-runs/realrun-20261019/g1_budget.json` (`{g1_sha}`)\n\n"
        f"{GENERATED_START_MARKER}\n{GENERATED_END_MARKER}\n",
        encoding="utf-8",
    )
    update_markdown_generated_block(clean_sample_md, evidence_dir=raw_dir, repo_root=sample_repo)
    assert (
        verify_generated_evidence_block(
            clean_sample_md.read_text(encoding="utf-8"), evidence_dir=raw_dir, repo_root=sample_repo
        )
        == []
    )

    clean_res = scan_reports([clean_sample_md], repo_root=sample_repo)
    assert clean_res["summary"]["all_clean"] is True

    # Tamper with sha256 and generated block -> check_report_claims catches both
    tampered_md = sample_repo / "docs" / "tampered-report.md"
    tampered_md.write_text(
        clean_sample_md.read_text(encoding="utf-8")
        .replace(g1_sha, "0" * 64)
        .replace("| **G1 预算** |", "| **G1 预算 (tampered)** |"),
        encoding="utf-8",
    )
    tampered_res = scan_reports([tampered_md], repo_root=sample_repo)
    assert tampered_res["summary"]["all_clean"] is False
    assert tampered_res["summary"]["sha256_mismatches_count"] == 2
    assert tampered_res["summary"]["generated_block_mismatches_count"] == 1


def test_static_script_scan_detects_from_src_and_insert_into(tmp_path: Path) -> None:
    bad_script = tmp_path / "g9_bad.py"
    bad_script.write_text(
        "from src.dalton.llm import router\n"
        "cur.execute('INSERT INTO dalton_watch_signals VALUES (1)')\n",
        encoding="utf-8",
    )
    res = scan_realrun_scripts([bad_script], allow_pre_j3_baseline=False)
    assert res["status"] == "FAIL"
    kinds = {v["kind"] for v in res["violations"]}
    assert "from_src_import" in kinds
    assert "insert_into:dalton_watch_signals" in kinds
