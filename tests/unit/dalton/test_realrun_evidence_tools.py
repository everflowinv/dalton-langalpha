"""Unit tests for P0I §2 / P0J §1 real-run evidence tools, v2 envelope, and report claim checker."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest

from scripts.dalton.check_report_claims import scan_reports
from scripts.dalton.realrun._common import (
    assert_clean_and_synced_environment,
    assert_no_credentials_in_obj,
    build_evidence_envelope,
    compute_file_sha256,
    render_summaries_from_var_to_docs,
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


def test_common_synthetic_validation_and_artifact_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "test-service-token-value")
    valid_synthetic = [
        {
            "kind": "settings_api_budget",
            "entry_point": "PUT /api/v1/dalton/budget",
            "description": "Set daily_cash_cap_usd=1.0 for G1 test user",
        },
        {
            "kind": "knowledge_api_import_fixture",
            "entry_point": "POST /api/v1/dalton/knowledge/ws-123/evidence/import (tests/fixtures/dalton/g3/googl_sec_evidence.json)",
            "description": "Import committed G3 evidence fixture",
        },
        "committed_test_hook:scripts/dalton/researchdata_bridge.py:168-190",
    ]
    validated = validate_synthetic_declarations(valid_synthetic)
    assert len(validated) == 3

    with pytest.raises(ValueError, match="not in ALLOWED_SYNTHETIC_KINDS"):
        validate_synthetic_declarations(["bridge_delay_hook:sleep_s=242"])

    with pytest.raises(ValueError, match="Forbidden synthetic injection"):
        validate_synthetic_declarations(["registered_test_hook:fake"])

    with pytest.raises(ValueError, match="Forbidden synthetic injection"):
        validate_synthetic_declarations(["direct_signal_insert:dalton_watch_signals"])

    # Exact category-to-entry_point matching (Item 13):
    with pytest.raises(ValueError, match="requires exact knowledge agenda endpoint"):
        validate_synthetic_declarations(
            [
                {
                    "kind": "knowledge_api_agenda",
                    "entry_point": "POST /api/v1/dalton/knowledge/ws-123/evidence/import",
                }
            ]
        )

    with pytest.raises(ValueError, match="must specify a committed file under 'tests/fixtures/'"):
        validate_synthetic_declarations(
            [
                {
                    "kind": "knowledge_api_import_fixture",
                    "entry_point": "POST /api/v1/dalton/knowledge/ws-123/evidence/import",
                }
            ]
        )

    with pytest.raises(ValueError, match="not registered in COMMITTED_TEST_HOOKS"):
        validate_synthetic_declarations(
            [{"kind": "committed_test_hook", "entry_point": "src/dalton/unregistered.py"}]
        )

    with pytest.raises(ValueError, match="secret token pattern"):
        assert_no_credentials_in_obj({"token": "sk-1234567890abcdefghijklmnop"})

    with pytest.raises(ValueError, match="email address pattern"):
        assert_no_credentials_in_obj({"user_email": "analyst@example.com"})

    # Preflight rejection checks via explicit callable injection (Item 5)
    with pytest.raises(SystemExit, match="Working tree is dirty"):
        assert_clean_and_synced_environment(
            base_url="http://127.0.0.1:8000",
            user_id="realrun-g1-20261019-abcd1234",
            git_sha_fn=lambda: "1234abcd",
            git_dirty_fn=lambda: True,
            merged_main_fn=lambda: True,
        )

    with pytest.raises(SystemExit, match="not merged into origin/dalton/main"):
        assert_clean_and_synced_environment(
            base_url="http://127.0.0.1:8000",
            user_id="realrun-g1-20261019-abcd1234",
            git_sha_fn=lambda: "1234abcd",
            git_dirty_fn=lambda: False,
            merged_main_fn=lambda: False,
        )

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

    injected_health = lambda _url, uid: {  # noqa: E731
        "git_sha": "1234abcd",
        "git_dirty": False,
        "deployment": "docker",
        "user_id": uid,
    }
    injected_env = lambda _url: {  # noqa: E731
        "all_passed": True,
        "deployment": "docker",
        "checks": {"identity": {"status": "PASS"}},
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
        git_sha_fn=lambda: "1234abcd",
        git_dirty_fn=lambda: False,
        merged_main_fn=lambda: True,
        main_ancestor_fn=lambda: True,
        health_fn=injected_health,
        env_check_fn=injected_env,
    )
    assert envelope["evidence_version"] == 2
    assert envelope["identity_mode"] == "service_token"
    assert envelope["valid"] is True
    assert envelope["invalid_reasons"] == []
    assert len(envelope["script_sha256"]) == 64

    # Verify conversation_usages_unavailable, local-dev-user, or <NULL> user_id marks valid=False (Item 20)
    bad_journal = {
        "rows": [
            {"invocation_id": "inv-bad", "user_id": "local-dev-user"},
            {"invocation_id": "inv-null", "user_id": "<NULL>"},
        ],
        "conversation_usages": [{"response_id": "resp-null", "user_id": "<NULL>"}],
        "totals": {
            "conversation_usages_unavailable": True,
            "unexpected_user_ids": ["<NULL>", "local-dev-user"],
        },
    }
    bad_env = build_evidence_envelope(
        script="scripts/dalton/realrun/g1_budget.py",
        started_at="2026-10-19T05:00:00Z",
        ended_at="2026-10-19T05:02:00Z",
        backend="http://127.0.0.1:8000",
        test_user_id="realrun-g1-20261019-abcd1234",
        synthetic=[],
        journal=bad_journal,
        git_sha_fn=lambda: "1234abcd",
        git_dirty_fn=lambda: False,
        merged_main_fn=lambda: True,
        main_ancestor_fn=lambda: True,
        health_fn=injected_health,
        env_check_fn=injected_env,
    )
    assert bad_env["valid"] is False
    assert "conversation_usages_unavailable" in bad_env["invalid_reasons"]
    assert "journal_row_owned_by_local_dev_user" in bad_env["invalid_reasons"]
    assert "conversation_usage_null_user" in bad_env["invalid_reasons"]
    assert any("unexpected_window_user_ids" in r for r in bad_env["invalid_reasons"])

    raw_path = tmp_path / "var" / "dalton" / "trial-runs" / "realrun-20261019" / "g1_budget.json"
    md_path = tmp_path / "var" / "dalton" / "trial-runs" / "realrun-20261019" / "G1.md"

    # Dry-run (record=False) must NOT write any files to disk (Item 15)
    dry_raw, dry_md, dry_sha = write_evidence_and_summary(
        family_code="G1",
        title="预算三连真实运行验证",
        raw_json_path=raw_path,
        summary_md_path=md_path,
        envelope=envelope,
        key_findings=[("日额度阻断", "402 budget_exceeded")],
        notes=["全部通过真实后端接口执行"],
        record=False,
        git_dirty_fn=lambda: False,
        merged_main_fn=lambda: True,
    )
    assert not dry_raw.exists()
    assert not dry_md.exists()
    assert len(dry_sha) == 64

    # record=True writes under var/ and persists summary_title/key_findings/notes in JSON for J5 rendering
    written_raw, written_md, sha256_hex = write_evidence_and_summary(
        family_code="G1",
        title="预算三连真实运行验证",
        raw_json_path=raw_path,
        summary_md_path=md_path,
        envelope=envelope,
        key_findings=[("日额度阻断", "402 budget_exceeded")],
        notes=["全部通过真实后端接口执行"],
        record=True,
        git_dirty_fn=lambda: False,
        merged_main_fn=lambda: True,
    )
    assert written_raw.is_file()
    assert written_md.is_file()
    assert sha256_hex == compute_file_sha256(written_raw)
    md_text = written_md.read_text(encoding="utf-8")
    assert sha256_hex in md_text
    assert "`settings_api_budget`" in md_text

    # Render from var/ to docs/ (J5/J6 separation, Item 15)
    rendered_files = render_summaries_from_var_to_docs(
        date_str="20261019", repo_root=tmp_path
    )
    assert len(rendered_files) == 1
    expected_doc = tmp_path / "docs" / "dalton" / "dev-guide" / "trial-runs" / "realrun-20261019" / "G1.md"
    assert expected_doc.is_file()
    assert sha256_hex in expected_doc.read_text(encoding="utf-8")


def test_check_report_claims_and_generated_evidence_table_on_sample_files(
    tmp_path: Path,
) -> None:
    # Fixed sample repository for both negative and positive assertions (Item 16, R4-families #11)
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

    bad_sample_md = sample_repo / "docs" / "bad-sample-report.md"
    bad_sample_md.parent.mkdir(parents=True)
    bad_sample_md.write_text(
        "# Bad Sample Report\n\n"
        "- Non-existent symbol: `NonExistentSymbolInSampleRepo` in `src/dalton/sample_mod.py`\n"
        "- Non-existent var artifact: `var/dalton/missing_artifact.json`\n",
        encoding="utf-8",
    )

    bad_result = scan_reports([bad_sample_md], repo_root=sample_repo)
    assert bad_result["summary"]["all_clean"] is False
    bad_entry = bad_result["reports"][0]
    missing_ids = {item["identifier"] for item in bad_entry["missing_identifiers"]}
    assert "NonExistentSymbolInSampleRepo" in missing_ids
    missing_vars = {item["path"] for item in bad_entry["missing_var_paths"]}
    assert "var/dalton/missing_artifact.json" in missing_vars

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
    res = scan_realrun_scripts([bad_script])
    assert res["status"] == "FAIL"
    kinds = {v["kind"] for v in res["violations"]}
    assert "from_src_import" in kinds
    assert "insert_into:dalton_watch_signals" in kinds

