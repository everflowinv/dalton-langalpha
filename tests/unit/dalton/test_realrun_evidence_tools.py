"""Unit tests for P0I §2 / P0J §1 real-run evidence tools, v2 envelope, and report claim checker."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import importlib
import inspect
import json
from pathlib import Path

import pytest

from scripts.dalton.check_report_claims import scan_reports
from scripts.dalton.realrun._common import (
    RealRunHttpClient,
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
    compare_legacy_report,
    update_markdown_generated_block,
    verify_generated_evidence_block,
)
from src.config.env import LOCAL_DEV_USER_ID
from src.dalton.services import openclaw_admin
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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
        {
            "kind": "committed_test_hook",
            "entry_point": "scripts/dalton/researchdata_bridge.py:168-190",
            "reason": "Use committed >240s delay hook for G7",
        },
    ]
    validated = validate_synthetic_declarations(valid_synthetic)
    assert len(validated) == 3

    # Bare strings are rejected (Must-Fix #11)
    with pytest.raises((TypeError, ValueError), match="must be a dict"):
        validate_synthetic_declarations(["committed_test_hook:scripts/dalton/researchdata_bridge.py:168-190"])  # type: ignore[list-item]

    with pytest.raises(ValueError, match="not in ALLOWED_SYNTHETIC_KINDS"):
        validate_synthetic_declarations([{"kind": "bridge_delay_hook", "entry_point": "sleep_s=242"}])

    with pytest.raises(ValueError, match="Forbidden synthetic injection"):
        validate_synthetic_declarations([{"kind": "registered_test_hook", "entry_point": "fake"}])

    with pytest.raises(ValueError, match="Forbidden synthetic injection"):
        validate_synthetic_declarations([{"kind": "direct_signal_insert", "entry_point": "dalton_watch_signals"}])

    with pytest.raises(ValueError, match="not registered in COMMITTED_TEST_HOOKS"):
        validate_synthetic_declarations(
            [{"kind": "committed_test_hook", "entry_point": "--simulate-upstream"}]
        )

    # Full-string match on entry_point (Must-Fix #11): trailing garbage rejected
    with pytest.raises(ValueError, match="requires exact entry_point"):
        validate_synthetic_declarations(
            [
                {
                    "kind": "settings_api_budget",
                    "entry_point": "PUT /api/v1/dalton/budget --extra-unregistered-suffix",
                }
            ]
        )

    with pytest.raises(ValueError, match="not registered in COMMITTED_TEST_HOOKS"):
        validate_synthetic_declarations(
            [
                {
                    "kind": "committed_test_hook",
                    "entry_point": "scripts/dalton/researchdata_bridge.py:168-190 (trailing text)",
                }
            ]
        )

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
    failing_env = lambda _url: {  # noqa: E731
        "all_passed": False,
        "deployment": "docker",
        "checks": {"conversation_transport": {"status": "FAIL"}},
    }

    # Debug mode (record=False) allows dirty/unmerged branches and prints DEBUG RUN — NOT EVIDENCE (§8.1 #13)
    debug_health = assert_clean_and_synced_environment(
        base_url="http://127.0.0.1:8000",
        user_id="realrun-g1-20261019-abcd1234",
        record=False,
        git_sha_fn=lambda: "1234abcd",
        git_dirty_fn=lambda: True,
        merged_main_fn=lambda: False,
        main_ancestor_fn=lambda: False,
        health_fn=injected_health,
    )
    assert debug_health["deployment"] == "docker"
    captured = capsys.readouterr()
    assert "DEBUG RUN — NOT EVIDENCE" in captured.out

    # Record mode (record=True) enforces clean tree, merged into origin/dalton/main, and check_env preflight
    with pytest.raises(SystemExit, match="Working tree is dirty"):
        assert_clean_and_synced_environment(
            base_url="http://127.0.0.1:8000",
            user_id="realrun-g1-20261019-abcd1234",
            record=True,
            git_sha_fn=lambda: "1234abcd",
            git_dirty_fn=lambda: True,
            merged_main_fn=lambda: True,
            main_ancestor_fn=lambda: True,
            health_fn=injected_health,
            env_check_fn=injected_env,
        )

    with pytest.raises(SystemExit, match="not merged into origin/dalton/main"):
        assert_clean_and_synced_environment(
            base_url="http://127.0.0.1:8000",
            user_id="realrun-g1-20261019-abcd1234",
            record=True,
            git_sha_fn=lambda: "1234abcd",
            git_dirty_fn=lambda: False,
            merged_main_fn=lambda: False,
            main_ancestor_fn=lambda: True,
            health_fn=injected_health,
            env_check_fn=injected_env,
        )

    with pytest.raises(SystemExit, match="Pre-run check_env failed before family execution"):
        assert_clean_and_synced_environment(
            base_url="http://127.0.0.1:8000",
            user_id="realrun-g1-20261019-abcd1234",
            record=True,
            git_sha_fn=lambda: "1234abcd",
            git_dirty_fn=lambda: False,
            merged_main_fn=lambda: True,
            main_ancestor_fn=lambda: True,
            health_fn=injected_health,
            env_check_fn=failing_env,
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

    # Debug run (record=False and env_check_fn=None) must skip check_env completely
    debug_envelope = build_evidence_envelope(
        script="scripts/dalton/realrun/g1_budget.py",
        started_at="2026-10-19T05:00:00Z",
        ended_at="2026-10-19T05:02:00Z",
        backend="http://127.0.0.1:8000",
        test_user_id="realrun-g1-20261019-abcd1234",
        synthetic=valid_synthetic,
        journal=fake_journal,
        record=False,
        git_sha_fn=lambda: "1234abcd",
        git_dirty_fn=lambda: False,
        merged_main_fn=lambda: True,
        main_ancestor_fn=lambda: True,
        health_fn=injected_health,
    )
    assert debug_envelope["environment"] == {"skipped_in_debug_mode": True, "record": False}
    assert "debug_run_not_record" in debug_envelope["invalid_reasons"]

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

    # Reject raw_json_path or summary_md_path outside var/ (Must-Fix #12)
    with pytest.raises(ValueError, match="Evidence raw_json_path must reside under"):
        write_evidence_and_summary(
            family_code="G1",
            title="预算三连真实运行验证",
            raw_json_path=tmp_path / "docs" / "dalton" / "g1_budget.json",
            summary_md_path=md_path,
            envelope=envelope,
            key_findings=[("日额度阻断", "402 budget_exceeded")],
            record=False,
            repo_root=tmp_path,
        )

    with pytest.raises(ValueError, match="Evidence raw_json_path must reside under"):
        write_evidence_and_summary(
            family_code="G1",
            title="预算三连真实运行验证",
            raw_json_path=raw_path,
            summary_md_path=tmp_path / "outside_var" / "G1.md",
            envelope=envelope,
            key_findings=[("日额度阻断", "402 budget_exceeded")],
            record=False,
            repo_root=tmp_path,
        )

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
        repo_root=tmp_path,
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
        repo_root=tmp_path,
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


def test_for_check_env_probe_caller_guard_and_docker_build_sha_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "test-service-token-value")
    # RealRunHttpClient.__init__ must not expose any preflight_fn bypass parameter
    assert "preflight_fn" not in inspect.signature(RealRunHttpClient.__init__).parameters

    with pytest.raises(RuntimeError, match="reserved exclusively for"):
        RealRunHttpClient._for_check_env_probe(
            base_url="http://127.0.0.1:8000", user_id="realrun-env-probe-123"
        )

    # Docker build-time SHA and dirty flag (Must-Fix #7):
    build_sha_file = tmp_path / ".build_git_sha"
    build_dirty_file = tmp_path / ".build_git_dirty"
    manifest_file = tmp_path / ".build_files.sha256"

    # Missing .build_git_sha -> ("unknown", True)
    sha_missing, dirty_missing = openclaw_admin._compute_startup_git_info(
        in_docker=True,
        app_root=tmp_path,
        build_sha_file=build_sha_file,
        build_dirty_file=build_dirty_file,
        build_files_sha256_file=manifest_file,
    )
    assert sha_missing == "unknown"
    assert dirty_missing is True

    # Baked SHA + dirty=true -> ("94a8d368", True)
    build_sha_file.write_text("94a8d368\n", encoding="utf-8")
    build_dirty_file.write_text("true\n", encoding="utf-8")
    sha_dirty, is_dirty = openclaw_admin._compute_startup_git_info(
        in_docker=True,
        app_root=tmp_path,
        build_sha_file=build_sha_file,
        build_dirty_file=build_dirty_file,
        build_files_sha256_file=manifest_file,
    )
    assert sha_dirty == "94a8d368"
    assert is_dirty is True

    # Baked SHA + dirty=false -> ("94a8d368", False)
    build_dirty_file.write_text("false\n", encoding="utf-8")
    sha_clean, is_clean = openclaw_admin._compute_startup_git_info(
        in_docker=True,
        app_root=tmp_path,
        build_sha_file=build_sha_file,
        build_dirty_file=build_dirty_file,
        build_files_sha256_file=manifest_file,
    )
    assert sha_clean == "94a8d368"
    assert is_clean is False


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
    (sample_repo / "scripts" / "dalton" / "realrun" / "g3_publish.py").write_text(
        "# sample g3_publish.py\n",
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

    # Designated G3 evaluation fields check inside generated block (ignores unrelated ratios in prose)
    g3_json = raw_dir / "g3_publish.json"
    g3_json.write_text(
        json.dumps(
            {
                "evidence_version": 2,
                "git_sha": "1234abcd",
                "git_dirty": False,
                "backend_git_sha": "1234abcd",
                "backend_git_dirty": False,
                "test_user_id": "realrun-pm-a",
                "valid": True,
                "journal": {
                    "totals": {
                        "total_calls": 4,
                        "fresh_calls": 4,
                        "replayed_calls": 0,
                        "conversation_turns": 0,
                        "cash_usd": 0.0,
                        "notional_usd": 0.05,
                        "combined_cash_usd": 0.0,
                    }
                },
                "evaluation": {
                    "true_verified_in_gold_count": 88,
                    "gold_sourced_total": 88,
                    "false_verified_in_gold_count": 0,
                    "false_unverified_in_gold_count": 0,
                    "sourced_uncertain_in_gold_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    ratio_match_md = sample_repo / "docs" / "ratio-match.md"
    ratio_match_md.write_text(
        "无关正文比例 `3 / 4 = 75.0%`\n\n"
        f"{GENERATED_START_MARKER}\n{GENERATED_END_MARKER}\n",
        encoding="utf-8",
    )
    update_markdown_generated_block(ratio_match_md, evidence_dir=raw_dir, repo_root=sample_repo)
    assert (
        compare_legacy_report(ratio_match_md, evidence_dir=raw_dir, repo_root=sample_repo)[
            "g3_evaluation_mismatches"
        ]
        == []
    )
    ratio_mismatch_md = sample_repo / "docs" / "ratio-mismatch.md"
    ratio_mismatch_md.write_text(
        ratio_match_md.read_text(encoding="utf-8").replace(
            "| 金标准真阳核实数 (`true_verified_in_gold_count`) | `88` |",
            "| 金标准真阳核实数 (`true_verified_in_gold_count`) | `61` |",
        ),
        encoding="utf-8",
    )
    mismatches = compare_legacy_report(
        ratio_mismatch_md, evidence_dir=raw_dir, repo_root=sample_repo
    )["g3_evaluation_mismatches"]
    assert len(mismatches) == 1
    assert "true_verified_in_gold_count" in mismatches[0]


def test_static_script_scan_detects_from_src_and_insert_into(tmp_path: Path) -> None:
    bad_script = tmp_path / "g9_bad.py"
    bad_script.write_text(
        "from src.dalton.llm import router\n"
        "cur.execute('INSERT INTO dalton_watch_signals VALUES (1)')\n"
        "RealRunHttpClient._for_check_env_probe(base_url='http://127.0.0.1:8000', user_id='u')\n",
        encoding="utf-8",
    )
    res = scan_realrun_scripts([bad_script])
    assert res["status"] == "FAIL"
    kinds = {v["kind"] for v in res["violations"]}
    assert "from_src_import" in kinds
    assert "insert_into:dalton_watch_signals" in kinds
    assert "forbidden_check_env_probe_call" in kinds


def test_all_realrun_scripts_import_cleanly() -> None:
    """Every `g*.py` (and supporting script) in `scripts/dalton/realrun/` must import cleanly."""
    realrun_dir = Path(__file__).resolve().parents[3] / "scripts" / "dalton" / "realrun"
    g_scripts = sorted(realrun_dir.glob("g*.py"))
    assert len(g_scripts) >= 7
    for script_path in g_scripts:
        mod_name = f"scripts.dalton.realrun.{script_path.stem}"
        mod = importlib.import_module(mod_name)
        assert callable(getattr(mod, "main", None)), f"{mod_name} is missing callable main()"
    g1_mod = importlib.import_module("scripts.dalton.realrun.g1_budget")
    assert callable(getattr(g1_mod, "require_internal_service_token", None))


def test_g3_discover_migrated_workspaces_matches_by_name_with_pagination() -> None:
    from scripts.dalton.realrun import g3_publish

    class _FakeClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def get(
            self,
            path: str,
            *,
            params: dict[str, Any] | None = None,
            expected_status: int | tuple[int, ...] | None = 200,
        ) -> tuple[int, dict[str, Any]]:
            self.calls.append({"path": path, "params": dict(params or {})})
            offset = int((params or {}).get("offset", 0))
            if offset == 0:
                return (
                    200,
                    {
                        "workspaces": [
                            {"workspace_id": "ws-meta", "name": "META", "status": "ready"},
                            {"workspace_id": "ws-amzn", "name": "AMZN", "status": "ready"},
                            {"workspace_id": "ws-del", "name": "GOOGL", "status": "deleted"},
                        ],
                        "total": 5,
                    },
                )
            return (
                200,
                {
                    "workspaces": [
                        {"workspace_id": "ws-googl", "name": "GOOGL", "status": "ready"},
                        {"workspace_id": "ws-msft", "name": "MSFT", "status": "ready"},
                    ],
                    "total": 5,
                },
            )

    fake_client = _FakeClient()
    mapping = g3_publish._discover_migrated_company_workspaces(fake_client)  # type: ignore[arg-type]
    assert mapping == {
        "META": "ws-meta",
        "AMZN": "ws-amzn",
        "GOOGL": "ws-googl",
        "MSFT": "ws-msft",
    }
    assert len(fake_client.calls) == 2
    assert fake_client.calls[0]["params"] == {"limit": 100, "offset": 0}


def test_g6_lenses_inputs_has_multiperiod_provenance_and_no_fwd_total() -> None:
    from scripts.dalton.realrun import g6_lenses

    repo_root = Path(__file__).resolve().parents[3]
    fixture_path = repo_root / "tests" / "fixtures" / "dalton" / "g6" / "lenses_inputs.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    companies = payload["companies"]
    assert len(companies) == 3
    for comp in companies:
        ticker = comp["ticker"]
        assert ticker in g6_lenses.ANALYST_CREDIBILITY_BY_TICKER
        ret = comp["return_score"]
        assert "fwd_total" not in ret
        for section in (comp["netcash"], ret, comp["earnings_quality"]):
            for metric_key, item in section.items():
                assert isinstance(item.get("accession"), str) and item["accession"].strip(), (
                    f"{ticker}.{metric_key} missing accession"
                )
        # Multi-period / prior-year items must not point solely to the single current-quarter accession
        single_current_acc = comp["filing"]["accession"]
        for mp_section, mp_key in (
            (ret, "annual"),
            (ret, "ttm_div"),
            (comp["earnings_quality"], "beginning_assets"),
            (comp["earnings_quality"], "ni"),
        ):
            assert mp_section[mp_key]["accession"] != single_current_acc



def test_pr_baseline_script_exists_and_executable() -> None:
    import os

    repo_root = Path(__file__).resolve().parents[3]
    script = repo_root / "scripts" / "dalton" / "pr_baseline.sh"
    assert script.is_file()
    assert os.access(script, os.X_OK)

