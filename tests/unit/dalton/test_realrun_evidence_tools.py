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
    assert normalized["totals"]["cash_usd"] == pytest.approx(0.0)
    assert normalized["totals"]["notional_usd"] == pytest.approx(0.0142)
    assert normalized["rows"][0]["replayed"] is False
    assert normalized["rows"][1]["replayed"] is True
    assert normalized["rows"][1]["replayed_from_before_window"] is True
    assert normalized["rows"][1]["cash_usd"] == pytest.approx(0.0012)
    assert normalized["rows"][1]["notional_usd"] == pytest.approx(0.0012)


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

    from unittest.mock import create_autospec
    from scripts.dalton.realrun import _common as _common_mod

    def _health_side_effect(_url: str, uid: str) -> dict[str, object]:
        return {
            "git_sha": "1234abcd",
            "git_dirty": False,
            "deployment": "docker",
            "user_id": uid,
        }

    injected_health = create_autospec(_common_mod._probe_backend_health_sync, side_effect=_health_side_effect)
    injected_env = create_autospec(_common_mod._default_live_env_check)
    injected_env.return_value = {
        "all_passed": True,
        "deployment": "docker",
        "checks": {"identity": {"status": "PASS"}},
    }
    failing_env = create_autospec(_common_mod._default_live_env_check)
    failing_env.return_value = {
        "all_passed": False,
        "deployment": "docker",
        "checks": {"conversation_transport": {"status": "FAIL"}},
    }
    sha_fn = create_autospec(_common_mod.get_git_sha)
    sha_fn.return_value = "1234abcd"
    dirty_true = create_autospec(_common_mod.is_git_dirty)
    dirty_true.return_value = True
    dirty_false = create_autospec(_common_mod.is_git_dirty)
    dirty_false.return_value = False
    merged_true = create_autospec(_common_mod.is_head_merged_into_main)
    merged_true.return_value = True
    merged_false = create_autospec(_common_mod.is_head_merged_into_main)
    merged_false.return_value = False
    anc_true = create_autospec(_common_mod.is_main_ancestor_of_head)
    anc_true.return_value = True
    anc_false = create_autospec(_common_mod.is_main_ancestor_of_head)
    anc_false.return_value = False

    # Debug mode (record=False) allows dirty/unmerged branches and prints DEBUG RUN — NOT EVIDENCE (§8.1 #13)
    debug_health = assert_clean_and_synced_environment(
        base_url="http://127.0.0.1:8000",
        user_id="realrun-g1-20261019-abcd1234",
        record=False,
        _git_sha_fn=sha_fn,
        _git_dirty_fn=dirty_true,
        _merged_main_fn=merged_false,
        _main_ancestor_fn=anc_false,
        _health_fn=injected_health,
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
            _git_sha_fn=sha_fn,
            _git_dirty_fn=dirty_true,
            _merged_main_fn=merged_true,
            _main_ancestor_fn=anc_true,
            _health_fn=injected_health,
            _env_check_fn=injected_env,
        )

    with pytest.raises(SystemExit, match="not merged into origin/dalton/main"):
        assert_clean_and_synced_environment(
            base_url="http://127.0.0.1:8000",
            user_id="realrun-g1-20261019-abcd1234",
            record=True,
            _git_sha_fn=sha_fn,
            _git_dirty_fn=dirty_false,
            _merged_main_fn=merged_false,
            _main_ancestor_fn=anc_true,
            _health_fn=injected_health,
            _env_check_fn=injected_env,
        )

    with pytest.raises(SystemExit, match="Pre-run check_env failed before family execution"):
        assert_clean_and_synced_environment(
            base_url="http://127.0.0.1:8000",
            user_id="realrun-g1-20261019-abcd1234",
            record=True,
            _git_sha_fn=sha_fn,
            _git_dirty_fn=dirty_false,
            _merged_main_fn=merged_true,
            _main_ancestor_fn=anc_true,
            _health_fn=injected_health,
            _env_check_fn=failing_env,
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

    # Debug run (record=False and _env_check_fn=None) must skip check_env completely
    debug_envelope = build_evidence_envelope(
        script="scripts/dalton/realrun/g1_budget.py",
        started_at="2026-10-19T05:00:00Z",
        ended_at="2026-10-19T05:02:00Z",
        backend="http://127.0.0.1:8000",
        test_user_id="realrun-g1-20261019-abcd1234",
        synthetic=valid_synthetic,
        journal=fake_journal,
        record=False,
        _git_sha_fn=sha_fn,
        _git_dirty_fn=dirty_false,
        _merged_main_fn=merged_true,
        _main_ancestor_fn=anc_true,
        _health_fn=injected_health,
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
        _git_sha_fn=sha_fn,
        _git_dirty_fn=dirty_false,
        _merged_main_fn=merged_true,
        _main_ancestor_fn=anc_true,
        _health_fn=injected_health,
        _env_check_fn=injected_env,
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
        _git_sha_fn=sha_fn,
        _git_dirty_fn=dirty_false,
        _merged_main_fn=merged_true,
        _main_ancestor_fn=anc_true,
        _health_fn=injected_health,
        _env_check_fn=injected_env,
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
        _git_dirty_fn=dirty_false,
        _merged_main_fn=merged_true,
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
        _git_dirty_fn=dirty_false,
        _merged_main_fn=merged_true,
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

    # Docker build-time SHA and dirty flag (Must-Fix #7, Review Item 9):
    docker_app = tmp_path / "docker_app"
    (docker_app / "src").mkdir(parents=True)
    sample_py = docker_app / "src" / "main.py"
    sample_py.write_text("print('ok')\n", encoding="utf-8")
    sample_sha = compute_file_sha256(sample_py)

    build_sha_file = docker_app / ".build_git_sha"
    build_dirty_file = docker_app / ".build_git_dirty"
    manifest_file = docker_app / ".build_files.sha256"

    # Missing .build_git_sha -> ("unknown", True)
    sha_missing, dirty_missing = openclaw_admin._compute_startup_git_info(
        in_docker=True,
        app_root=docker_app,
        build_sha_file=build_sha_file,
        build_dirty_file=build_dirty_file,
        build_files_sha256_file=manifest_file,
    )
    assert sha_missing == "unknown"
    assert dirty_missing is True

    # Baked SHA + dirty=false BUT missing .build_files.sha256 -> must still be dirty=True (Review Item 9)
    build_sha_file.write_text("94a8d368\n", encoding="utf-8")
    build_dirty_file.write_text("false\n", encoding="utf-8")
    sha_no_manifest, dirty_no_manifest = openclaw_admin._compute_startup_git_info(
        in_docker=True,
        app_root=docker_app,
        build_sha_file=build_sha_file,
        build_dirty_file=build_dirty_file,
        build_files_sha256_file=manifest_file,
    )
    assert sha_no_manifest == "94a8d368"
    assert dirty_no_manifest is True

    # Write matching .build_files.sha256 -> ("94a8d368", False)
    manifest_file.write_text(f"{sample_sha}  src/main.py\n", encoding="utf-8")
    sha_clean, is_clean = openclaw_admin._compute_startup_git_info(
        in_docker=True,
        app_root=docker_app,
        build_sha_file=build_sha_file,
        build_dirty_file=build_dirty_file,
        build_files_sha256_file=manifest_file,
    )
    assert sha_clean == "94a8d368"
    assert is_clean is False

    # Adding an untracked file under src/ or config/dalton/ after build -> marks dirty=True (Review Item 9)
    added_file = docker_app / "src" / "untracked_extra.py"
    added_file.write_text("x = 1\n", encoding="utf-8")
    _, dirty_after_add = openclaw_admin._compute_startup_git_info(
        in_docker=True,
        app_root=docker_app,
        build_sha_file=build_sha_file,
        build_dirty_file=build_dirty_file,
        build_files_sha256_file=manifest_file,
    )
    assert dirty_after_add is True
    added_file.unlink()

    # Modifying tracked file in src/ after build -> marks dirty=True
    sample_py.write_text("print('modified')\n", encoding="utf-8")
    _, dirty_after_mod = openclaw_admin._compute_startup_git_info(
        in_docker=True,
        app_root=docker_app,
        build_sha_file=build_sha_file,
        build_dirty_file=build_dirty_file,
        build_files_sha256_file=manifest_file,
    )
    assert dirty_after_mod is True


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
        "RealRunHttpClient._for_check_env_probe(base_url='http://127.0.0.1:8000', user_id='u')\n"
        "assert_clean_and_synced_environment(_git_dirty_fn=lambda: False)\n",
        encoding="utf-8",
    )
    res = scan_realrun_scripts([bad_script])
    assert res["status"] == "FAIL"
    kinds = {v["kind"] for v in res["violations"]}
    assert "from_src_import" in kinds
    assert "insert_into:dalton_watch_signals" in kinds
    assert "forbidden_check_env_probe_call" in kinds
    assert "forbidden_preflight_fn_override" in kinds


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
    from unittest.mock import create_autospec
    from scripts.dalton.realrun import _common, g3_publish

    calls: list[dict[str, object]] = []

    def _paginated_get(
        path: str,
        *,
        params: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        calls.append({"path": path, "params": dict(params or {})})
        offset = int((params or {}).get("offset", 0))
        if offset == 0:
            return (
                200,
                {
                    "workspaces": [
                        {
                            "workspace_id": _common.KNOWN_MIGRATED_WORKSPACES["META"],
                            "name": "META",
                            "status": "ready",
                        },
                        {
                            "workspace_id": _common.KNOWN_MIGRATED_WORKSPACES["AMZN"],
                            "name": "AMZN",
                            "status": "ready",
                        },
                        {"workspace_id": "ws-del", "name": "GOOGL", "status": "deleted"},
                    ],
                    "total": 5,
                },
            )
        return (
            200,
            {
                "workspaces": [
                    {
                        "workspace_id": _common.KNOWN_MIGRATED_WORKSPACES["GOOGL"],
                        "name": "GOOGL",
                        "status": "ready",
                    },
                    {
                        "workspace_id": _common.KNOWN_MIGRATED_WORKSPACES["MSFT"],
                        "name": "MSFT",
                        "status": "ready",
                    },
                ],
                "total": 5,
            },
        )

    fake_client = create_autospec(RealRunHttpClient, instance=True)
    fake_client.get.side_effect = _paginated_get
    mapping = g3_publish._discover_migrated_company_workspaces(fake_client)
    assert mapping == dict(_common.KNOWN_MIGRATED_WORKSPACES)
    assert len(calls) == 2
    assert calls[0]["params"] == {"limit": 100, "offset": 0}

    # Duplicate non-deleted workspaces matching same ticker must raise RuntimeError (Requirement 5)
    dup_client = create_autospec(RealRunHttpClient, instance=True)
    dup_client.get.return_value = (
        200,
        {
            "workspaces": [
                {
                    "workspace_id": _common.KNOWN_MIGRATED_WORKSPACES["META"],
                    "name": "META",
                    "status": "ready",
                },
                {
                    "workspace_id": "22222222-3333-4444-5555-666666666666",
                    "name": "META",
                    "status": "ready",
                },
            ],
            "total": 2,
        },
    )
    with pytest.raises(RuntimeError, match="Multiple non-deleted workspaces"):
        g3_publish._discover_migrated_company_workspaces(dup_client)

    # Workspace matching by name whose ID is not in KNOWN_MIGRATED_WORKSPACES must raise RuntimeError (Requirement 5)
    wrong_id_client = create_autospec(RealRunHttpClient, instance=True)
    wrong_id_client.get.return_value = (
        200,
        {
            "workspaces": [
                {"workspace_id": "wrong-meta-id", "name": "META", "status": "ready"},
            ],
            "total": 1,
        },
    )
    with pytest.raises(RuntimeError, match="does not match KNOWN_MIGRATED_WORKSPACES"):
        g3_publish._discover_migrated_company_workspaces(wrong_id_client)


def test_g6_lenses_inputs_is_explicitly_illustrative_and_has_no_sec_tags() -> None:
    from scripts.dalton.realrun import g6_lenses

    repo_root = Path(__file__).resolve().parents[3]
    fixture_path = repo_root / "tests" / "fixtures" / "dalton" / "g6" / "lenses_inputs.json"
    raw_text = fixture_path.read_text(encoding="utf-8")
    payload = json.loads(raw_text)
    assert payload.get("illustrative") is True
    assert payload.get("description") == "示意输入，用于验证镜头命令链路和锚点一致性，不是公司财报数据"
    assert '"accession"' not in raw_text
    assert '"url"' not in raw_text
    assert '"filing"' not in raw_text
    companies = payload["companies"]
    assert len(companies) == 3
    assert callable(g6_lenses.render_comparison_markdown)
    for comp in companies:
        ticker = comp["ticker"]
        assert ticker in {"BABA", "0700.HK", "META"}
        ret = comp["return_score"]
        assert "示意参数" in ret["credibility_reason"]


def test_g1_playwright_route_scopes_auth_headers_to_local_origins_only() -> None:
    from scripts.dalton.realrun import g1_budget

    assert (
        g1_budget._is_allowed_auth_header_origin(
            "http://localhost:5173/api/v1/dalton/budget/today",
            frontend_url="http://localhost:5173",
            backend_url="http://127.0.0.1:8000",
        )
        is True
    )
    assert (
        g1_budget._is_allowed_auth_header_origin(
            "http://127.0.0.1:8000/api/v1/dalton/budget",
            frontend_url="http://localhost:5173",
            backend_url="http://127.0.0.1:8000",
        )
        is True
    )
    assert (
        g1_budget._is_allowed_auth_header_origin(
            "https://fonts.googleapis.com/css2?family=Inter",
            frontend_url="http://localhost:5173",
            backend_url="http://127.0.0.1:8000",
        )
        is False
    )

    from unittest.mock import create_autospec
    from playwright.sync_api import Request, Route

    local_req = create_autospec(Request, instance=True)
    local_req.url = "http://localhost:5173/api/v1/dalton/budget/today"
    local_req.headers = {"Accept": "application/json"}
    local_route = create_autospec(Route, instance=True)
    g1_budget._handle_scoped_auth_route(
        local_route,
        local_req,
        frontend_url="http://localhost:5173",
        backend_url="http://127.0.0.1:8000",
        user_id="u1",
        svc_token="tok-secret",
    )
    local_route.continue_.assert_called_once()
    continued_headers = local_route.continue_.call_args.kwargs.get("headers")
    assert isinstance(continued_headers, dict)
    assert continued_headers["X-Service-Token"] == "tok-secret"

    ext_req = create_autospec(Request, instance=True)
    ext_req.url = "https://fonts.googleapis.com/css2?family=Inter"
    ext_req.headers = {"Accept": "text/css"}
    ext_route = create_autospec(Route, instance=True)
    g1_budget._handle_scoped_auth_route(
        ext_route,
        ext_req,
        frontend_url="http://localhost:5173",
        backend_url="http://127.0.0.1:8000",
        user_id="u1",
        svc_token="tok-secret",
    )
    ext_route.continue_.assert_called_once_with()


def test_pr_baseline_script_exists_and_executable() -> None:
    import os

    repo_root = Path(__file__).resolve().parents[3]
    script = repo_root / "scripts" / "dalton" / "pr_baseline.sh"
    assert script.is_file()
    assert os.access(script, os.X_OK)
    build_script = repo_root / "scripts" / "dalton" / "docker_build_backend.sh"
    assert build_script.is_file()
    assert os.access(build_script, os.X_OK)


def test_check_env_oauth_provenance_budget_restore_and_g6_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timezone
    from unittest.mock import create_autospec
    import psycopg

    from scripts.dalton.realrun import check_env, g6_lenses

    def _build_autospec_db_conn(
        row: dict[str, object] | None,
        rows: list[dict[str, object]],
    ) -> tuple[psycopg.Connection[object], psycopg.Cursor[object], list[tuple[str, object]]]:
        executed_sql: list[tuple[str, object]] = []
        cur = create_autospec(psycopg.Cursor, instance=True)
        cur.__enter__.return_value = cur

        def _rec_execute(query: str, params: object = None, **kwargs: object) -> object:
            executed_sql.append((str(query), params))
            return cur

        cur.execute.side_effect = _rec_execute
        cur.fetchone.return_value = row
        cur.fetchall.return_value = rows
        conn = create_autospec(psycopg.Connection, instance=True)
        conn.__enter__.return_value = conn
        conn.cursor.return_value = cur
        return conn, cur, executed_sql

    t_login = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
    t_token = datetime(2026, 10, 20, 12, 0, 15, tzinfo=timezone.utc)

    mock_connect = create_autospec(psycopg.connect)
    monkeypatch.setattr(check_env.psycopg, "connect", mock_connect)

    # 1. check_oauth_token_provenance: PASS when prior login_start exists and no unverified realrun-* rows
    conn_pass, _, _ = _build_autospec_db_conn(
        {
            "user_id": "realrun-pm-a",
            "provider": "antigravity-oauth",
            "created_at": t_token,
            "updated_at": t_token,
            "earliest_prior_login_start": t_login,
        },
        [],
    )
    mock_connect.return_value = conn_pass
    res_pass = check_env.check_oauth_token_provenance("realrun-pm-a", "antigravity-oauth", dsn="postgresql://x")
    assert res_pass["status"] == "PASS"
    assert res_pass["has_prior_oauth_login_start"] is True

    # FAIL when token exists but earliest_prior_login_start is None (copied token)
    conn_fail, _, _ = _build_autospec_db_conn(
        {
            "user_id": "realrun-pm-a",
            "provider": "antigravity-oauth",
            "created_at": t_token,
            "updated_at": t_token,
            "earliest_prior_login_start": None,
        },
        [{"user_id": "realrun-pm-a", "provider": "antigravity-oauth", "created_at": t_token}],
    )
    mock_connect.return_value = conn_fail
    res_fail = check_env.check_oauth_token_provenance("realrun-pm-a", "antigravity-oauth", dsn="postgresql://x")
    assert res_fail["status"] == "FAIL"
    assert res_fail["reason"] == "missing_prior_oauth_login_start_event"

    # Exception sanitization (Requirement 9): FAIL error summary must not leak DSNs, passwords, or keys
    mock_connect.side_effect = RuntimeError(
        "conn failed for postgresql://admin:mySecretPass123@db.local:5432/dalton with sk-1234567890abcdefghijklmnop"
    )
    res_err = check_env.check_oauth_token_provenance(
        "realrun-pm-a",
        "antigravity-oauth",
        dsn="postgresql://admin:mySecretPass123@db.local:5432/dalton",
    )
    assert res_err["status"] == "FAIL"
    assert "mySecretPass123" not in json.dumps(res_err)
    assert "sk-1234567890abcdefghijklmnop" not in json.dumps(res_err)
    mock_connect.side_effect = None

    # check_conversation_transport fails immediately when oauth_provenance is FAIL
    conv_fail = check_env.check_conversation_transport(
        "http://127.0.0.1:8000",
        model="gemini-3.8-flash-low-oauth",
        oauth_provenance=res_fail,
    )
    assert conv_fail["status"] == "FAIL"
    assert "oauth_token_provenance_failed" in str(conv_fail.get("reason"))

    # 2. _restore_budget_state: unconfigured state deletes DB row and verifies configured == False
    conn_unconf, _, executed_sql = _build_autospec_db_conn(None, [])
    mock_connect.return_value = conn_unconf

    http_unconf = create_autospec(RealRunHttpClient, instance=True)
    http_unconf.get.return_value = (200, {"configured": False})
    err_unconf = check_env._restore_budget_state(
        http_unconf,
        user_id="realrun-pm-a",
        was_configured=False,
        prior_budget=None,
        dsn="postgresql://x",
    )
    assert err_unconf is None
    conn_unconf.commit.assert_called_once()
    assert any("DELETE FROM dalton_budget_config" in sql for sql, _ in executed_sql)

    # Restore failure when configured remains True after delete
    http_unconf_fail = create_autospec(RealRunHttpClient, instance=True)
    http_unconf_fail.get.return_value = (200, {"configured": True})
    err_unconf_fail = check_env._restore_budget_state(
        http_unconf_fail,
        user_id="realrun-pm-a",
        was_configured=False,
        prior_budget=None,
        dsn="postgresql://x",
    )
    assert err_unconf_fail is not None

    # Restore failure when PUT fails for previously configured budget
    http_conf_fail = create_autospec(RealRunHttpClient, instance=True)
    http_conf_fail.get.return_value = (200, {"configured": True})
    http_conf_fail.put.return_value = (500, {})
    err_conf_fail = check_env._restore_budget_state(
        http_conf_fail,
        user_id="realrun-pm-a",
        was_configured=True,
        prior_budget={"daily_cap_usd": 12.0, "default_task_cap_usd": 3.0, "timezone": "Asia/Shanghai"},
        dsn="postgresql://x",
    )
    assert err_conf_fail == "budget_restore_put_http_500"

    # 3. G6 render_comparison_markdown has no Section 2 static table or hand-written legacy comparison claims
    sample_ticker = {
        "market_cap_usd_mn": 1000.0,
        "strict_net_cash_usd": 200.0,
        "strict_net_cash_to_mcap_pct": 20.0,
        "fx_rate_to_usd": 0.14,
        "listing_fx_rate": 0.128,
        "basic_net_cash_usd": 250.0,
        "basic_net_cash_to_mcap_pct": 25.0,
        "restricted_cash_usd_equiv": 50.0,
        "return_final_score": 15.0,
        "overall_quality": "HIGH",
        "operating_sloan_pct": -1.2,
        "cfo_to_ni_pct": 120.0,
    }
    md_text = g6_lenses.render_comparison_markdown(
        {"BABA": sample_ticker, "0700.HK": sample_ticker, "META": sample_ticker},
        run_date="20261020",
    )
    assert "示意数据" in md_text
    assert "## 2." not in md_text
    assert "与原脚本一致" not in md_text
    assert "偶发漏扣" not in md_text


def test_j5a_realrun_script_fixes_g5_callback_ordering_and_g2_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import create_autospec
    from scripts.dalton.realrun import (
        g1_budget,
        g2_providers,
        g3_publish,
        g4_autonomy,
        g5_broker_and_antigravity,
    )

    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "test-service-token-value")

    # 1. Static scan on all realrun g*.py scripts must pass with 0 violations
    scan_res = scan_realrun_scripts()
    assert scan_res["status"] == "PASS", f"Static scan failed: {scan_res}"
    assert scan_res["violations"] == []

    # 2. All budget-modifying realrun scripts import _restore_budget_state
    for mod in (g1_budget, g2_providers, g3_publish, g4_autonomy, g5_broker_and_antigravity):
        assert callable(getattr(mod, "_restore_budget_state", None))

    # 3. G5 pre-callback gate rejects dirty working tree, mismatched backend SHA, or pre-callback check_env failure before touching callback URL
    mock_dirty = create_autospec(g5_broker_and_antigravity.is_git_dirty, return_value=True)
    mock_merged_true = create_autospec(g5_broker_and_antigravity.is_head_merged_into_main, return_value=True)
    mock_anc_true = create_autospec(g5_broker_and_antigravity.is_main_ancestor_of_head, return_value=True)
    mock_sha = create_autospec(g5_broker_and_antigravity.get_git_sha, return_value="deadbeef")
    mock_health = create_autospec(
        g5_broker_and_antigravity._probe_backend_health_sync,
        return_value={"git_sha": "cafebabe", "git_dirty": False, "deployment": "docker"},
    )
    monkeypatch.setattr(g5_broker_and_antigravity, "is_git_dirty", mock_dirty)
    with pytest.raises(SystemExit, match="Working tree is dirty"):
        g5_broker_and_antigravity._assert_pre_callback_git_and_backend_gate(
            base_url="http://127.0.0.1:8000", user_id="realrun-pm-a", record=True
        )

    mock_dirty.return_value = False
    monkeypatch.setattr(g5_broker_and_antigravity, "is_head_merged_into_main", mock_merged_true)
    monkeypatch.setattr(g5_broker_and_antigravity, "is_main_ancestor_of_head", mock_anc_true)
    monkeypatch.setattr(g5_broker_and_antigravity, "get_git_sha", mock_sha)
    monkeypatch.setattr(
        g5_broker_and_antigravity,
        "_probe_backend_health_sync",
        mock_health,
    )
    with pytest.raises(SystemExit, match="Backend git_sha"):
        g5_broker_and_antigravity._assert_pre_callback_git_and_backend_gate(
            base_url="http://127.0.0.1:8000", user_id="realrun-pm-a", record=True
        )

    # Pre-callback check_env (skip_oauth_and_transport=True) runs before consuming callback URL (Requirement 7)
    mock_health.return_value = {"git_sha": "deadbeef", "git_dirty": False, "deployment": "docker"}
    mock_pre_checks_fail = create_autospec(
        g5_broker_and_antigravity.run_all_checks,
        return_value={
            "all_passed": False,
            "checks": {
                "migrated_workspace_ownership": {"status": "FAIL"},
                "oauth_token_provenance": {"status": "SKIPPED"},
                "conversation_transport": {"status": "SKIPPED"},
            },
        },
    )
    monkeypatch.setattr(g5_broker_and_antigravity, "run_all_checks", mock_pre_checks_fail)
    with pytest.raises(SystemExit, match="Pre-callback check_env failed before consuming single-use OAuth callback URL"):
        g5_broker_and_antigravity._assert_pre_callback_git_and_backend_gate(
            base_url="http://127.0.0.1:8000", user_id="realrun-pm-a", record=False
        )
    mock_pre_checks_fail.assert_called_once_with(
        backend_url="http://127.0.0.1:8000",
        skip_oauth_and_transport=True,
    )

    # 4. G5 --mode g5.2 exchanges callback URL BEFORE preflight_realrun_script runs
    call_order: list[str] = []

    def _rec_pre_gate(*, base_url: str, user_id: str, record: bool | None = None) -> dict[str, object]:
        call_order.append("pre_gate")
        return {"deployment": "docker"}

    def _rec_exchange(
        base_url: str,
        user_a: str,
        callback_url: str | None,
        *,
        record: bool = False,
    ) -> dict[str, object]:
        call_order.append("exchange_callback")
        return {"deployment": "docker"}

    def _rec_preflight(
        family_code: str,
        *,
        record: bool | None = None,
        backend: str = "http://127.0.0.1:8000",
        user_id: str = "realrun-pm-a",
    ) -> dict[str, object]:
        call_order.append("preflight")
        return {}

    def _rec_run_g5_2(
        base_url: str,
        user_a: str,
        user_b: str,
        callback_url: str | None = None,
        *,
        record: bool = False,
        pre_exchanged_callback_flow: dict[str, object] | None = None,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        call_order.append("run_g5_2")
        return (
            {
                "deployment": "docker",
                "oauth_status_and_isolation": {
                    "user_a": {"status_body": {"connected": True}},
                    "user_b_isolation": {"status_body": {"connected": False}},
                },
                "gemini_4_round_tool_calling": {
                    "tool_calls_count": 4,
                    "tool_call_results_count": 4,
                    "gemini_model_invocations_count": 5,
                },
                "docker_callback_paste_flow": {
                    "callback_missing_state": {"http_status": 400},
                    "callback_wrong_state": {"http_status": 400},
                    "callback_real_paste_exchange": {"http_status": 200},
                },
            },
            [{"passed": True}],
        )

    def _rec_post_checks(*, backend_url: str = "http://127.0.0.1:8000", dsn: str | None = None, skip_oauth_and_transport: bool = False) -> dict[str, object]:
        call_order.append(f"post_callback_check_env:skip={skip_oauth_and_transport}")
        return {"all_passed": True, "checks": {}}

    mock_dotenv = create_autospec(g5_broker_and_antigravity.load_repo_dotenv, return_value=False)
    mock_pre_gate = create_autospec(
        g5_broker_and_antigravity._assert_pre_callback_git_and_backend_gate,
        side_effect=_rec_pre_gate,
    )
    mock_exchange = create_autospec(
        g5_broker_and_antigravity._exchange_g5_2_docker_callback,
        side_effect=_rec_exchange,
    )
    from scripts.dalton.realrun import check_env as _check_env_mod

    mock_post_checks = create_autospec(
        _check_env_mod.run_all_checks,
        side_effect=_rec_post_checks,
    )
    mock_preflight = create_autospec(
        g5_broker_and_antigravity.preflight_realrun_script,
        side_effect=_rec_preflight,
    )
    mock_run_g5_2 = create_autospec(
        g5_broker_and_antigravity._run_g5_2_antigravity,
        side_effect=_rec_run_g5_2,
    )
    mock_build_env = create_autospec(
        g5_broker_and_antigravity.build_evidence_envelope,
        return_value={"deployment": "docker", "assertions": [{"passed": True}]},
    )
    mock_write_ev = create_autospec(
        g5_broker_and_antigravity.write_evidence_and_summary,
        return_value=(Path("/tmp/g5.json"), Path("/tmp/G5.md"), "abc123"),
    )
    monkeypatch.setattr(g5_broker_and_antigravity, "load_repo_dotenv", mock_dotenv)
    monkeypatch.setattr(
        g5_broker_and_antigravity,
        "_assert_pre_callback_git_and_backend_gate",
        mock_pre_gate,
    )
    monkeypatch.setattr(
        g5_broker_and_antigravity,
        "_exchange_g5_2_docker_callback",
        mock_exchange,
    )
    monkeypatch.setattr(
        g5_broker_and_antigravity,
        "run_all_checks",
        mock_post_checks,
    )
    monkeypatch.setattr(
        g5_broker_and_antigravity,
        "preflight_realrun_script",
        mock_preflight,
    )
    monkeypatch.setattr(
        g5_broker_and_antigravity,
        "_run_g5_2_antigravity",
        mock_run_g5_2,
    )
    monkeypatch.setattr(
        g5_broker_and_antigravity,
        "build_evidence_envelope",
        mock_build_env,
    )
    monkeypatch.setattr(
        g5_broker_and_antigravity,
        "write_evidence_and_summary",
        mock_write_ev,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "g5_broker_and_antigravity.py",
            "--mode",
            "g5.2",
            "--callback-url",
            "http://localhost:51121/oauth-callback?code=c1&state=s1",
        ],
    )
    rc = g5_broker_and_antigravity.main()
    assert rc == 0
    assert call_order == [
        "pre_gate",
        "exchange_callback",
        "post_callback_check_env:skip=False",
        "preflight",
        "run_g5_2",
    ]

    # Post-callback check_env failure aborts even without --record
    mock_post_checks.side_effect = None
    mock_post_checks.return_value = {
        "all_passed": False,
        "checks": {"oauth_token_provenance": {"status": "FAIL", "reason": "missing_prior_oauth_login_start_event"}},
    }
    with pytest.raises(SystemExit, match="Post-callback check_env failed after exchanging OAuth callback URL"):
        g5_broker_and_antigravity.main()

    # 5. G2 thesis payloads conform to src/dalton/knowledge/schemas/thesis.schema.json
    from src.dalton.knowledge.validate import validate_schema

    repo_root = Path(__file__).resolve().parents[3]
    g2_src = (repo_root / "scripts" / "dalton" / "realrun" / "g2_providers.py").read_text(encoding="utf-8")
    assert "/api/v1/dalton/budget/task-caps/workspace/{sample_ws_id}" in g2_src
    assert 'json_body={"dry_run": True}' in g2_src
    assert "claude-sonnet-5-5-low-oauth" in g2_src
    sample_thesis = {
        "schema_version": 1,
        "as_of": "2026-10-20T00:00:00Z",
        "one_liner": "Alphabet Inc. G2 路由与权限测试初始观点 v1",
        "aggregate_status": "intact",
        "house_view": {
            "stance": "long",
            "conviction": "high",
            "horizon_months": 18,
            "summary": "G2 路由与权限测试初始观点 v1",
            "variant_perception": "Search and Cloud operating margin durability exceeds consensus.",
            "updated_at": "2026-10-20T00:00:00Z",
        },
        "house_view_history": [],
        "pillars": [
            {
                "id": "P1",
                "claim": "GOOGL core revenue growth and operating margin trajectory.",
                "priority": "core",
                "baseline": {"text": "2Q26 SEC 10-Q baseline.", "as_of": "2026-06-30"},
                "expected_path": "Maintain double-digit revenue growth and margin expansion.",
                "thresholds": {"confirm": [], "warn": [], "break": []},
                "status": "intact",
                "provenance": "approved",
                "created_at": "2026-10-20T00:00:00Z",
            }
        ],
        "evidence": [],
        "official": None,
    }
    assert validate_schema("thesis", sample_thesis) == []


def test_r2_g3_owner_read_g4_idle_user_check_env_and_g5_initiate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import create_autospec
    import uuid
    import psycopg

    from scripts.dalton.realrun import (
        _common,
        check_env,
        g3_publish,
        g4_autonomy,
        g5_broker_and_antigravity,
    )

    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "test-service-token-value")
    monkeypatch.delenv("AUTH_USER_ID", raising=False)

    # 1. G3 migrated check must read as local-dev-user (P0J §8.1 #20) and reject non-owner or non-string user_id (Requirement 5)
    wrong_client = create_autospec(RealRunHttpClient, instance=True)
    wrong_client.user_id = "realrun-pm-a"
    with pytest.raises(RuntimeError, match="must read as 'local-dev-user'"):
        g3_publish._verify_legacy_migrated_theses_via_http(wrong_client)

    non_str_client = create_autospec(RealRunHttpClient, instance=True)
    non_str_client.user_id = None  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="to be a non-empty string"):
        g3_publish._verify_legacy_migrated_theses_via_http(non_str_client)

    owner_client = create_autospec(RealRunHttpClient, instance=True)
    owner_client.user_id = "local-dev-user"

    def _owner_get(
        path: str,
        *,
        params: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        if path == "/api/v1/workspaces":
            offset = int((params or {}).get("offset", 0))
            if offset == 0:
                return (
                    200,
                    {
                        "workspaces": [
                            {
                                "workspace_id": _common.KNOWN_MIGRATED_WORKSPACES["META"],
                                "name": "META",
                                "status": "ready",
                            },
                            {
                                "workspace_id": _common.KNOWN_MIGRATED_WORKSPACES["AMZN"],
                                "name": "AMZN",
                                "status": "ready",
                            },
                        ],
                        "total": 4,
                    },
                )
            return (
                200,
                {
                    "workspaces": [
                        {
                            "workspace_id": _common.KNOWN_MIGRATED_WORKSPACES["GOOGL"],
                            "name": "GOOGL",
                            "status": "ready",
                        },
                        {
                            "workspace_id": _common.KNOWN_MIGRATED_WORKSPACES["MSFT"],
                            "name": "MSFT",
                            "status": "ready",
                        },
                    ],
                    "total": 4,
                },
            )
        if path.endswith("/thesis/versions"):
            return (
                200,
                {
                    "items": [
                        {"version_no": 1, "source": "migration", "reason": "Initial migration", "content_sha256": "s1"},
                        {
                            "version_no": 2,
                            "source": "import",
                            "reason": "迁移的旧认知不作为核实依据",
                            "content_sha256": "s2",
                        },
                    ]
                },
            )
        if path.endswith("/thesis/versions/2"):
            return (
                200,
                {
                    "version_no": 2,
                    "content": {"evidence": [{"id": "EV-1", "source": {"type": "legacy_thesis"}}]},
                },
            )
        raise AssertionError(f"Unexpected GET path: {path}")

    owner_client.get.side_effect = _owner_get
    histories = g3_publish._verify_legacy_migrated_theses_via_http(owner_client)
    assert set(histories.keys()) == {"META", "AMZN", "GOOGL", "MSFT"}
    for ticker, item in histories.items():
        assert item["read_as_user_id"] == "local-dev-user"
        assert item["workspace_id"] == _common.KNOWN_MIGRATED_WORKSPACES[ticker]
        assert item["latest_version_no"] == 2
        assert "迁移的旧认知不作为核实依据" in item["latest_version_reason"]

    # 2. G4 Scenario 5 uses dedicated realrun-idle-c, verifies only Home + new ws exist, puts workspace_id at top level, and requires action == "created"
    repo_root = Path(__file__).resolve().parents[3]
    g4_src = (repo_root / "scripts" / "dalton" / "realrun" / "g4_autonomy.py").read_text(encoding="utf-8")
    assert "saved_preexisting_agendas" not in g4_src
    assert "Temporary clean agenda import" not in g4_src
    assert 'default="realrun-idle-c"' in g4_src

    idle_home_id = str(uuid.uuid5(_common.HOME_WORKSPACE_NAMESPACE, "realrun-idle-c"))
    idle_client = create_autospec(RealRunHttpClient, instance=True)
    idle_requests: list[tuple[str, str, object]] = []

    def _idle_request(
        method: str,
        path: str,
        *,
        json_body: object = None,
        params: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        idle_requests.append((method, path, json_body))
        if method == "POST" and path == "/api/v1/workspaces":
            return 201, {"workspace_id": "ws-idle-c"}
        if method == "POST" and path == "/api/v1/workspaces/ws-idle-c/start":
            return 200, {"status": "running"}
        if method == "GET" and path == "/api/v1/workspaces":
            return (
                200,
                {
                    "workspaces": [
                        {"workspace_id": idle_home_id, "name": "Home", "status": "stopped"},
                        {"workspace_id": "ws-idle-c", "name": "G4-IDLE", "status": "ready"},
                    ],
                    "total": 2,
                },
            )
        if method == "GET" and path == "/api/v1/automations":
            return 200, {"automations": [], "total": 0}
        if method == "PUT" and path == "/api/v1/dalton/automations/dalton-director-daily":
            return 200, {"ok": True, "action": "created", "automation_id": "auto-dd-idle-c"}
        if method == "GET" and path == "/api/v1/dalton/budget/today":
            return 200, {"spent_usd": 0.0}
        if method == "POST" and path == "/api/v1/automations/auto-dd-idle-c/trigger":
            return 200, {"execution_id": "exec-dd-idle-c"}
        if method == "GET" and path == "/api/v1/automations/auto-dd-idle-c/executions":
            return (
                200,
                {
                    "executions": [
                        {
                            "automation_execution_id": "exec-dd-idle-c",
                            "status": "skipped",
                            "skip_reason": "precheck",
                        }
                    ]
                },
            )
        raise AssertionError(f"Unexpected request in Scenario 5: {method} {path}")

    idle_client.request.side_effect = _idle_request
    mock_query_pf = create_autospec(g4_autonomy._query_preflight_and_signals)
    mock_query_pf.return_value = {
        "preflight_log": {"decision": "skip", "reason": "no_new_signals"},
        "signals": [],
    }
    monkeypatch.setattr(g4_autonomy, "_query_preflight_and_signals", mock_query_pf)

    created_ws_c: list[str] = []
    created_auto_c: list[str] = []
    s5_out = g4_autonomy._run_scenario_5_director_daily_zero_token_skip(
        idle_client,
        idle_user_id="realrun-idle-c",
        created_ws_ids=created_ws_c,
        created_auto_ids=created_auto_c,
    )
    assert s5_out["user_id"] == "realrun-idle-c"
    assert s5_out["idle_workspace_id"] == "ws-idle-c"
    assert s5_out["execution_status"] == "skipped"
    assert s5_out["preflight_reason"] == "no_new_signals"
    assert s5_out["cash_spend_usd"] == 0.0
    assert created_ws_c == ["ws-idle-c"]
    assert created_auto_c == ["auto-dd-idle-c"]
    ws_create_body = next(b for m, p, b in idle_requests if m == "POST" and p == "/api/v1/workspaces")
    assert isinstance(ws_create_body, dict)
    assert ws_create_body.get("config") == {
        "realrun_ephemeral": True,
        "realrun_script": "g4_autonomy.py",
    }
    dd_put_body = next(
        b for m, p, b in idle_requests if m == "PUT" and p == "/api/v1/dalton/automations/dalton-director-daily"
    )
    assert isinstance(dd_put_body, dict)
    assert dd_put_body.get("workspace_id") == "ws-idle-c"
    assert not any("/agenda/import" in p for _, p, _ in idle_requests)

    # Scenario 5 fails if realrun-idle-c owns any unexpected active workspace or if automation was updated instead of created
    def _idle_request_updated(
        method: str,
        path: str,
        *,
        json_body: object = None,
        params: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        if method == "POST" and path == "/api/v1/workspaces":
            return 201, {"workspace_id": "ws-idle-c-2"}
        if method == "POST" and path == "/api/v1/workspaces/ws-idle-c-2/start":
            return 200, {"status": "running"}
        if method == "GET" and path == "/api/v1/workspaces":
            return (
                200,
                {
                    "workspaces": [
                        {"workspace_id": idle_home_id, "name": "Home", "status": "stopped"},
                        {"workspace_id": "ws-idle-c-2", "name": "G4-IDLE", "status": "ready"},
                    ],
                    "total": 2,
                },
            )
        if method == "GET" and path == "/api/v1/automations":
            return (
                200,
                {
                    "automations": [
                        {
                            "automation_id": "auto-existing",
                            "name": "dalton-director-daily",
                            "workspace_id": "ws-idle-c-2",
                        }
                    ],
                    "total": 1,
                },
            )
        if method == "PUT" and path == "/api/v1/dalton/automations/dalton-director-daily":
            raise AssertionError("PUT must not be sent when a conflicting automation already exists (D7)")
        raise AssertionError(f"Unexpected request: {method} {path}")

    idle_client_upd = create_autospec(RealRunHttpClient, instance=True)
    idle_client_upd.request.side_effect = _idle_request_updated
    created_auto_upd: list[str] = []
    with pytest.raises(RuntimeError, match="Refusing to PUT /api/v1/dalton/automations/dalton-director-daily"):
        g4_autonomy._run_scenario_5_director_daily_zero_token_skip(
            idle_client_upd,
            idle_user_id="realrun-idle-c",
            created_ws_ids=[],
            created_auto_ids=created_auto_upd,
        )
    assert created_auto_upd == []

    # 3. D1 & check_env: KNOWN_MIGRATED_WORKSPACES exact 4 UUIDs, migrated ownership & realrun-* provenance
    assert not hasattr(check_env, "load_migrated_workspace_ids")
    assert _common.KNOWN_MIGRATED_WORKSPACES == {
        "META": "0d9c7b91-2db8-587e-903c-5a98ac8deee1",
        "AMZN": "83d1d873-beb4-5fd1-9655-ffb39af506d2",
        "GOOGL": "f0725330-1d25-52c2-9052-8f95537e2eb4",
        "MSFT": "1ee3520c-1fa9-5f19-9c0a-1625d3e27ee4",
    }

    def _make_autospec_conn(rows: list[dict[str, object]]) -> psycopg.Connection[object]:
        cur = create_autospec(psycopg.Cursor, instance=True)
        cur.__enter__.return_value = cur
        cur.fetchall.return_value = rows
        conn = create_autospec(psycopg.Connection, instance=True)
        conn.__enter__.return_value = conn
        conn.cursor.return_value = cur
        return conn

    mock_db_connect = create_autospec(psycopg.connect)
    monkeypatch.setattr(check_env.psycopg, "connect", mock_db_connect)

    # 3a. Migrated workspaces all owned by local-dev-user -> PASS
    pass_mig_rows = [
        {"workspace_id": wid, "name": ticker, "user_id": "local-dev-user", "status": "ready"}
        for ticker, wid in _common.KNOWN_MIGRATED_WORKSPACES.items()
    ]
    conn_mig_pass = _make_autospec_conn(pass_mig_rows)
    mock_db_connect.return_value = conn_mig_pass
    res_mig_pass = check_env.check_migrated_workspace_ownership(dsn="postgresql://x")
    assert res_mig_pass["status"] == "PASS"
    assert res_mig_pass["expected_owner"] == "local-dev-user"

    # Migrated workspace reassigned to realrun-pm-a -> FAIL
    fail_mig_rows = [
        {
            "workspace_id": wid,
            "name": ticker,
            "user_id": "realrun-pm-a" if ticker == "META" else "local-dev-user",
            "status": "ready",
        }
        for ticker, wid in _common.KNOWN_MIGRATED_WORKSPACES.items()
    ]
    conn_mig_fail = _make_autospec_conn(fail_mig_rows)
    mock_db_connect.return_value = conn_mig_fail
    res_mig_fail = check_env.check_migrated_workspace_ownership(dsn="postgresql://x")
    assert res_mig_fail["status"] == "FAIL"
    assert res_mig_fail["wrong_owner_workspaces"][0]["ticker"] == "META"

    # 3b. realrun-* workspace provenance: valid marker + valid UUIDv5 Home -> PASS; unmarked -> FAIL
    valid_home_id = str(uuid.uuid5(_common.HOME_WORKSPACE_NAMESPACE, "realrun-pm-a"))
    prov_pass_rows = [
        {
            "workspace_id": valid_home_id,
            "user_id": "realrun-pm-a",
            "name": "Home",
            "status": "stopped",
            "config": {},
        },
        {
            "workspace_id": "11111111-2222-3333-4444-555555555555",
            "user_id": "realrun-idle-c",
            "name": "G4-IDLE",
            "status": "ready",
            "config": {"realrun_ephemeral": True, "realrun_script": "g4_autonomy.py"},
        },
    ]
    conn_prov_pass = _make_autospec_conn(prov_pass_rows)
    mock_db_connect.return_value = conn_prov_pass
    res_prov_pass = check_env.check_realrun_workspace_provenance(dsn="postgresql://x")
    assert res_prov_pass["status"] == "PASS"

    prov_fail_rows = [
        {
            "workspace_id": "9f649354-7bf0-4a84-b178-9d2576b4c805",
            "user_id": "realrun-pm-a",
            "name": "META",
            "status": "ready",
            "config": {},
        }
    ]
    conn_prov_fail = _make_autospec_conn(prov_fail_rows)
    mock_db_connect.return_value = conn_prov_fail
    res_prov_fail = check_env.check_realrun_workspace_provenance(dsn="postgresql://x")
    assert res_prov_fail["status"] == "FAIL"
    assert len(res_prov_fail["invalid_workspaces"]) == 1

    # 3c. BYOK and subagent snapshot/restore helpers (Requirement 3)
    restore_client = create_autospec(RealRunHttpClient, instance=True)
    restore_client.get.return_value = (
        200,
        {"byok_enabled": False, "providers": [{"provider": "deepseek", "masked_key": "(server)"}]},
    )
    restore_client.put.return_value = (
        200,
        {"byok_enabled": False, "providers": [{"provider": "deepseek", "masked_key": "(server)"}]},
    )
    restore_client.delete.return_value = (200, {})
    snap_keys = check_env._snapshot_api_keys_state(restore_client)
    assert snap_keys["byok_enabled"] is False
    assert snap_keys["had_user_deepseek_key"] is False
    assert check_env._restore_api_keys_state(restore_client, prior_state=snap_keys) is None
    restore_client.delete.assert_called_once_with("/api/v1/users/me/api-keys/deepseek")

    restore_sub_client = create_autospec(RealRunHttpClient, instance=True)
    restore_sub_client.get.return_value = (
        200,
        {
            "subagents": [
                {
                    "name": "general-purpose",
                    "version": "v1",
                    "edited": False,
                    "user_fields": [],
                    "model": None,
                }
            ]
        },
    )
    restore_sub_client.put.return_value = (
        200,
        {"name": "general-purpose", "version": "v2", "user_fields": [], "model": None},
    )
    snap_sub = check_env._snapshot_subagent_state(restore_sub_client, "general-purpose")
    assert snap_sub["edited"] is False
    assert check_env._restore_subagent_state(restore_sub_client, prior_state=snap_sub) is None
    restore_sub_client.put.assert_called_once_with(
        "/api/v1/subagents/general-purpose",
        json_body={"version": "v1", "fields": {"model": None}},
    )

    # 4. G5 --mode g5.2-initiate: rejects --record (non-zero exit) and raises RuntimeError if any file is written under var/dalton/trial-runs/
    mock_dotenv_init = create_autospec(g5_broker_and_antigravity.load_repo_dotenv, return_value=False)
    monkeypatch.setattr(g5_broker_and_antigravity, "load_repo_dotenv", mock_dotenv_init)

    with pytest.raises(SystemExit, match="cannot be combined with --record"):
        g5_broker_and_antigravity.main(["--mode", "g5.2-initiate", "--record"])

    mock_health = create_autospec(g5_broker_and_antigravity._probe_backend_health_sync)
    mock_health.return_value = {"status": "ok", "deployment": "docker"}
    mock_init = create_autospec(g5_broker_and_antigravity._initiate_g5_2_for_user)
    mock_init.return_value = {
        "user_id": "realrun-pm-a",
        "initiate_http_status": 200,
        "port": 51121,
        "state_prefix": "abc1234567",
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth?state=abc1234567",
    }
    monkeypatch.setattr(g5_broker_and_antigravity, "_probe_backend_health_sync", mock_health)
    monkeypatch.setattr(g5_broker_and_antigravity, "_initiate_g5_2_for_user", mock_init)
    monkeypatch.setattr(g5_broker_and_antigravity, "REPO_ROOT", tmp_path)

    rc_init = g5_broker_and_antigravity.main(["--mode", "g5.2-initiate", "--date", "20261020"])
    assert rc_init == 0
    assert not (tmp_path / "var" / "dalton" / "trial-runs").exists()

    # Recursive check across entire var/dalton/trial-runs/ tree raises RuntimeError (not assert)
    def _dirty_init(base_url: str, user_id: str, *, record: bool = False) -> dict[str, object]:
        out_dir = tmp_path / "var" / "dalton" / "trial-runs" / "realrun-19990101"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "other_artifact.txt").write_text("{}", encoding="utf-8")
        return {"user_id": user_id}

    mock_init.side_effect = _dirty_init
    with pytest.raises(RuntimeError, match="must not write or modify any evidence files"):
        g5_broker_and_antigravity.main(["--mode", "g5.2-initiate", "--date", "20261020"])


def test_r2_round2_cleanup_autospec_d5_d6_d7(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import create_autospec

    from scripts.dalton.realrun import (
        _common,
        check_env,
        g1_budget,
        g4_autonomy,
    )

    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "test-service-token-value")

    # 1. Execute g1_budget._cleanup_g1_state directly with create_autospec on RealRunHttpClient and restore functions (Items 1 & 4)
    deleted_paths_g1: list[tuple[str, str]] = []

    def _make_g1_autospec_client(
        base_url: str = "http://127.0.0.1:8000",
        *,
        user_id: str,
        timeout_s: float = 120.0,
        extra_headers: dict[str, str] | None = None,
        record: bool | None = None,
    ) -> _common.RealRunHttpClient:
        inst = create_autospec(_common.RealRunHttpClient, instance=True)
        inst.__enter__.return_value = inst
        inst.__exit__.return_value = None

        def _del(path: str) -> tuple[int, dict[str, object]]:
            deleted_paths_g1.append((user_id, path))
            return 200, {}

        inst.delete.side_effect = _del
        return inst

    mock_g1_client_cls = create_autospec(_common.RealRunHttpClient, side_effect=_make_g1_autospec_client)
    mock_restore_sub_g1 = create_autospec(check_env._restore_subagent_state, return_value=None)
    mock_restore_keys_g1 = create_autospec(check_env._restore_api_keys_state, return_value=None)
    mock_restore_budget_g1 = create_autospec(check_env._restore_budget_state, return_value=None)
    monkeypatch.setattr(g1_budget, "RealRunHttpClient", mock_g1_client_cls)
    monkeypatch.setattr(g1_budget, "_restore_subagent_state", mock_restore_sub_g1)
    monkeypatch.setattr(g1_budget, "_restore_api_keys_state", mock_restore_keys_g1)
    monkeypatch.setattr(g1_budget, "_restore_budget_state", mock_restore_budget_g1)

    prior_sub_a = {"name": "general-purpose", "version": "v1", "edited": False, "user_fields": [], "model": None}
    prior_keys_a = {"byok_enabled": False, "had_user_deepseek_key": False}
    prior_budget_a = {"configured": True, "daily_cap_usd": 10.0}
    prior_keys_b = {"byok_enabled": False, "had_user_deepseek_key": False}
    prior_budget_b = {"configured": False}

    res_g1_ok = g1_budget._cleanup_g1_state(
        backend_url="http://127.0.0.1:8000",
        user_a="realrun-pm-a",
        workspace_id="ws-a-1",
        subagent_modified_a=True,
        prior_subagent_a=prior_sub_a,
        key_modified_a=True,
        prior_keys_a=prior_keys_a,
        budget_modified_a=True,
        was_configured_a=True,
        prior_budget_a=prior_budget_a,
        user_b="realrun-user-b",
        ws_b_id="ws-b-1",
        key_modified_b=True,
        prior_keys_b=prior_keys_b,
        budget_modified_b=True,
        was_configured_b=False,
        prior_budget_b=prior_budget_b,
    )
    assert all(v is None for v in res_g1_ok.values())
    assert deleted_paths_g1 == [
        ("realrun-pm-a", "/api/v1/workspaces/ws-a-1"),
        ("realrun-user-b", "/api/v1/workspaces/ws-b-1"),
    ]
    assert mock_restore_sub_g1.call_count == 1
    assert mock_restore_sub_g1.call_args.kwargs == {"prior_state": prior_sub_a}
    assert mock_restore_keys_g1.call_count == 2
    assert mock_restore_keys_g1.call_args_list[0].kwargs == {"prior_state": prior_keys_a}
    assert mock_restore_keys_g1.call_args_list[1].kwargs == {"prior_state": prior_keys_b}
    assert mock_restore_budget_g1.call_count == 2

    # Verify fault isolation in _cleanup_g1_state: if user_a subagent restore raises RuntimeError,
    # user_a key/budget and user_b workspace/key/budget still execute before RuntimeError is raised
    deleted_paths_g1.clear()
    mock_restore_sub_g1.reset_mock()
    mock_restore_keys_g1.reset_mock()
    mock_restore_budget_g1.reset_mock()
    mock_restore_sub_g1.side_effect = RuntimeError("boom_subagent")
    with pytest.raises(RuntimeError, match="subagent_a_restore_exception:RuntimeError"):
        g1_budget._cleanup_g1_state(
            backend_url="http://127.0.0.1:8000",
            user_a="realrun-pm-a",
            workspace_id="ws-a-2",
            subagent_modified_a=True,
            prior_subagent_a=prior_sub_a,
            key_modified_a=True,
            prior_keys_a=prior_keys_a,
            budget_modified_a=True,
            was_configured_a=True,
            prior_budget_a=prior_budget_a,
            user_b="realrun-user-b",
            ws_b_id="ws-b-2",
            key_modified_b=True,
            prior_keys_b=prior_keys_b,
            budget_modified_b=True,
            was_configured_b=False,
            prior_budget_b=prior_budget_b,
        )
    assert deleted_paths_g1 == [
        ("realrun-pm-a", "/api/v1/workspaces/ws-a-2"),
        ("realrun-user-b", "/api/v1/workspaces/ws-b-2"),
    ]
    assert mock_restore_keys_g1.call_count == 2
    assert mock_restore_budget_g1.call_count == 2

    # 2. Execute g4_autonomy._cleanup_g4_state directly with create_autospec on RealRunHttpClient and restore functions (Items 1 & 4)
    deleted_paths_g4: list[tuple[str, str]] = []

    def _make_g4_autospec_client(
        base_url: str = "http://127.0.0.1:8000",
        *,
        user_id: str,
        timeout_s: float = 120.0,
        extra_headers: dict[str, str] | None = None,
        record: bool | None = None,
    ) -> _common.RealRunHttpClient:
        inst = create_autospec(_common.RealRunHttpClient, instance=True)
        inst.__enter__.return_value = inst
        inst.__exit__.return_value = None

        def _del(path: str) -> tuple[int, dict[str, object]]:
            deleted_paths_g4.append((user_id, path))
            return 200, {}

        inst.delete.side_effect = _del
        return inst

    mock_g4_client_cls = create_autospec(_common.RealRunHttpClient, side_effect=_make_g4_autospec_client)
    mock_restore_sub_g4 = create_autospec(check_env._restore_subagent_state, return_value=None)
    mock_restore_keys_g4 = create_autospec(check_env._restore_api_keys_state, return_value=None)
    mock_restore_budget_g4 = create_autospec(check_env._restore_budget_state, return_value=None)
    monkeypatch.setattr(g4_autonomy, "RealRunHttpClient", mock_g4_client_cls)
    monkeypatch.setattr(g4_autonomy, "_restore_subagent_state", mock_restore_sub_g4)
    monkeypatch.setattr(g4_autonomy, "_restore_api_keys_state", mock_restore_keys_g4)
    monkeypatch.setattr(g4_autonomy, "_restore_budget_state", mock_restore_budget_g4)

    errs_g4 = g4_autonomy._cleanup_g4_state(
        backend_url="http://127.0.0.1:8000",
        idle_user="realrun-idle-c",
        created_idle_auto_ids=["auto-idle-1"],
        created_idle_ws_ids=["ws-idle-1"],
        pm_user="realrun-pm-a",
        created_auto_ids=["auto-pm-1"],
        created_ws_ids=["ws-pm-1"],
        subagent_modified_pm=True,
        prior_subagent_pm=prior_sub_a,
        key_modified_pm=True,
        prior_keys_pm=prior_keys_a,
        budget_modified_pm=True,
        was_configured_pm=True,
        prior_budget_pm=prior_budget_a,
    )
    assert errs_g4 == []
    # realrun-idle-c resources are deleted first
    assert deleted_paths_g4 == [
        ("realrun-idle-c", "/api/v1/automations/auto-idle-1"),
        ("realrun-idle-c", "/api/v1/workspaces/ws-idle-1"),
        ("realrun-pm-a", "/api/v1/automations/auto-pm-1"),
        ("realrun-pm-a", "/api/v1/workspaces/ws-pm-1"),
    ]
    assert mock_restore_sub_g4.call_args.kwargs == {"prior_state": prior_sub_a}
    assert mock_restore_keys_g4.call_args.kwargs == {"prior_state": prior_keys_a}

    # Verify fault isolation in _cleanup_g4_state: an exception in subagent restore does not skip key/budget or idle cleanup
    deleted_paths_g4.clear()
    mock_restore_sub_g4.side_effect = RuntimeError("subagent_fail")
    with pytest.raises(RuntimeError, match="restore_subagent_pm_exception:RuntimeError"):
        g4_autonomy._cleanup_g4_state(
            backend_url="http://127.0.0.1:8000",
            idle_user="realrun-idle-c",
            created_idle_auto_ids=["auto-idle-2"],
            created_idle_ws_ids=["ws-idle-2"],
            pm_user="realrun-pm-a",
            created_auto_ids=["auto-pm-2"],
            created_ws_ids=["ws-pm-2"],
            subagent_modified_pm=True,
            prior_subagent_pm=prior_sub_a,
            key_modified_pm=True,
            prior_keys_pm=prior_keys_a,
            budget_modified_pm=True,
            was_configured_pm=True,
            prior_budget_pm=prior_budget_a,
        )
    assert ("realrun-idle-c", "/api/v1/automations/auto-idle-2") in deleted_paths_g4
    assert ("realrun-idle-c", "/api/v1/workspaces/ws-idle-2") in deleted_paths_g4
    assert mock_restore_keys_g4.call_count == 2
    assert mock_restore_budget_g4.call_count == 2

    # 3. D6: Setup and cleanup behavior for both had_user_deepseek_key=True and had_user_deepseek_key=False
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek-key")

    # 3a. had_user_deepseek_key=True -> neither _common/G1/G2 nor G4 setup calls PUT, and _restore_api_keys_state is a no-op
    existing_key_client = create_autospec(RealRunHttpClient, instance=True)
    assert (
        _common.ensure_temporary_deepseek_byok(
            existing_key_client,
            {"byok_enabled": True, "had_user_deepseek_key": True},
        )
        is False
    )
    assert (
        g4_autonomy._ensure_deepseek_byok_for_g4(
            existing_key_client,
            {"byok_enabled": True, "had_user_deepseek_key": True},
        )
        is False
    )
    assert (
        check_env._restore_api_keys_state(
            existing_key_client,
            prior_state={"byok_enabled": True, "had_user_deepseek_key": True},
        )
        is None
    )
    existing_key_client.delete.assert_not_called()
    existing_key_client.put.assert_not_called()
    existing_key_client.request.assert_not_called()

    # Verify G1 evidence fields truthfully reflect preserved pre-existing keys (no PUT, no delete)
    ev_preserved = g1_budget._build_g1_byok_evidence_fields(
        key_modified_a=False,
        key_modified_b=False,
        cleanup_result={
            "restore_err_a": None,
            "restore_key_err_a": None,
            "restore_err_b": None,
            "delete_key_err_b": None,
        },
    )
    assert ev_preserved["scenario_1_api_keys_restored"] == "not_applicable"
    assert ev_preserved["scenario_2_settings_endpoint"] == "GET /api/v1/users/me/api-keys (preexisting key preserved)"
    assert ev_preserved["scenario_2_temporary_key_written"] is False
    assert ev_preserved["scenario_2_temporary_key_deleted"] is False
    assert ev_preserved["scenario_2_key_write_skipped_reason"] == "preexisting_user_deepseek_key_preserved"
    assert ev_preserved["user_b_temporary_key_written"] is False
    assert ev_preserved["user_b_key_deleted"] is False
    assert ev_preserved["user_b_key_write_skipped_reason"] == "preexisting_user_deepseek_key_preserved"
    assert "保留未删除" in ev_preserved["user_b_cleanup_note"]
    assert "已删除用户 B" not in ev_preserved["user_b_cleanup_note"]

    # 3b. had_user_deepseek_key=False -> _common/G1/G2 and G4 setup write temporary key via PUT, and evidence fields record write + delete
    from scripts.dalton.realrun import g2_providers

    no_key_client_g1 = create_autospec(RealRunHttpClient, instance=True)
    with pytest.raises(ValueError, match="prior_keys_state is required"):
        _common.ensure_temporary_deepseek_byok(no_key_client_g1, None)
    no_key_client_g1.put.assert_not_called()

    no_key_client_g1.put.return_value = (200, {"ok": True})
    assert (
        _common.ensure_temporary_deepseek_byok(
            no_key_client_g1,
            {"byok_enabled": False, "had_user_deepseek_key": False},
        )
        is True
    )
    no_key_client_g1.put.assert_called_once_with(
        "/api/v1/users/me/api-keys",
        json_body={"byok_enabled": True, "api_keys": {"deepseek": "sk-test-deepseek-key"}},
    )
    g2_ev_written = g2_providers._build_g2_byok_evidence_fields(
        pm_user="realrun-pm-a",
        key_modified_pm=True,
        restore_key_err_pm=None,
        restore_budget_err_pm=None,
    )
    assert g2_ev_written["temporary_key_written"] is True
    assert g2_ev_written["temporary_key_deleted"] is True
    assert g2_ev_written["api_keys_restored"] is True
    assert g2_ev_written["key_write_skipped_reason"] is None
    g2_ev_preserved = g2_providers._build_g2_byok_evidence_fields(
        pm_user="realrun-pm-a",
        key_modified_pm=False,
        restore_key_err_pm=None,
        restore_budget_err_pm=None,
    )
    assert g2_ev_preserved["temporary_key_written"] is False
    assert g2_ev_preserved["temporary_key_deleted"] is False
    assert g2_ev_preserved["api_keys_restored"] == "not_applicable"
    assert g2_ev_preserved["key_write_skipped_reason"] == "preexisting_user_deepseek_key_preserved"

    no_key_client_g4 = create_autospec(RealRunHttpClient, instance=True)
    no_key_client_g4.request.return_value = (200, {"ok": True})
    assert (
        g4_autonomy._ensure_deepseek_byok_for_g4(
            no_key_client_g4,
            {"byok_enabled": False, "had_user_deepseek_key": False},
        )
        is True
    )
    no_key_client_g4.request.assert_called_once_with(
        "PUT",
        "/api/v1/users/me/api-keys",
        json_body={"byok_enabled": True, "api_keys": {"deepseek": "sk-test-deepseek-key"}},
    )

    ev_written = g1_budget._build_g1_byok_evidence_fields(
        key_modified_a=True,
        key_modified_b=True,
        cleanup_result={
            "restore_err_a": None,
            "restore_key_err_a": None,
            "restore_err_b": None,
            "delete_key_err_b": None,
        },
    )
    assert ev_written["scenario_1_api_keys_restored"] is True
    assert ev_written["scenario_2_settings_endpoint"] == "PUT /api/v1/users/me/api-keys"
    assert ev_written["scenario_2_temporary_key_written"] is True
    assert ev_written["scenario_2_temporary_key_deleted"] is True
    assert ev_written["scenario_2_key_write_skipped_reason"] is None
    assert ev_written["user_b_temporary_key_written"] is True
    assert ev_written["user_b_key_deleted"] is True
    assert ev_written["user_b_key_write_skipped_reason"] is None
    assert "已删除用户 B 本轮写入的临时 key" in ev_written["user_b_cleanup_note"]

    ev_del_failed = g1_budget._build_g1_byok_evidence_fields(
        key_modified_a=True,
        key_modified_b=True,
        cleanup_result={
            "restore_err_a": None,
            "restore_key_err_a": "api_keys_restore_http_failed:del=500,put=500",
            "restore_err_b": None,
            "delete_key_err_b": "api_keys_restore_http_failed:del=500,put=500",
        },
    )
    assert ev_del_failed["scenario_1_api_keys_restored"] is False
    assert ev_del_failed["user_b_key_deleted"] is False
    assert "删除用户 B 本轮写入的临时 key 失败" in ev_del_failed["user_b_cleanup_note"]

    # 4. D7: assert_no_conflicting_dalton_automation detects conflicts before PUT
    auto_check_client = create_autospec(RealRunHttpClient, instance=True)
    auto_check_client.request.return_value = (
        200,
        {
            "automations": [
                {
                    "automation_id": "auto-other-ws",
                    "name": "dalton-meta-watch",
                    "workspace_id": "ws-other",
                }
            ],
            "total": 1,
        },
    )
    # Different non-null workspace_id -> allowed
    _common.assert_no_conflicting_dalton_automation(
        auto_check_client,
        name="dalton-meta-watch",
        workspace_id="ws-new",
    )
    # Same workspace_id -> raises RuntimeError before PUT
    with pytest.raises(RuntimeError, match="Refusing to PUT /api/v1/dalton/automations/dalton-meta-watch"):
        _common.assert_no_conflicting_dalton_automation(
            auto_check_client,
            name="dalton-meta-watch",
            workspace_id="ws-other",
        )
    # Existing automation has workspace_id=None -> matches any workspace_id on server, so raises RuntimeError
    auto_check_client.request.return_value = (
        200,
        {
            "automations": [
                {
                    "automation_id": "auto-global",
                    "name": "dalton-director-daily",
                    "workspace_id": None,
                }
            ],
            "total": 1,
        },
    )
    with pytest.raises(RuntimeError, match="Refusing to PUT /api/v1/dalton/automations/dalton-director-daily"):
        _common.assert_no_conflicting_dalton_automation(
            auto_check_client,
            name="dalton-director-daily",
            workspace_id="ws-idle-c",
        )

    # 5. D5: Call g1_budget._assert_g1_added_cash_spend directly with 1 passing dataset and 2 failing datasets
    added_a, added_b, total_added = g1_budget._assert_g1_added_cash_spend(
        baseline_spent_a=1.00,
        final_spent_a=1.30,
        baseline_spent_b=0.20,
        final_spent_b=0.23,
    )
    assert (added_a, added_b, total_added) == (0.30, 0.03, 0.33)

    # Failing dataset 1: user_b added spend > $0.05
    with pytest.raises(AssertionError, match=r"user_b newly added cash spend <= 0\.05 USD"):
        g1_budget._assert_g1_added_cash_spend(
            baseline_spent_a=1.00,
            final_spent_a=1.20,
            baseline_spent_b=0.20,
            final_spent_b=0.26,
        )

    # Failing dataset 2: total added spend > $0.50 (while user_b added spend <= $0.05)
    with pytest.raises(AssertionError, match=r"user_a \+ user_b newly added cash spend <= 0\.50 USD"):
        g1_budget._assert_g1_added_cash_spend(
            baseline_spent_a=1.00,
            final_spent_a=1.48,
            baseline_spent_b=0.20,
            final_spent_b=0.24,
        )

    # 6. Item 3 & Round 4 Items 1-2 & 5: check_env.check_conversation_transport does not discard restore_error
    # when workspace creation returns 500, raises an exception, or stream_sse raises an exception
    probe_client = create_autospec(_common.RealRunHttpClient, instance=True)
    probe_client.__enter__.return_value = probe_client
    probe_client.__exit__.return_value = None
    probe_client.get.side_effect = [
        (200, {"configured": True, "daily_cap_usd": 10.0, "default_task_cap_usd": 5.0}),
        (200, {"remaining_usd": 0.0, "spent_usd": 10.0}),
    ]
    probe_client.put.return_value = (200, {"ok": True})
    probe_client.post.return_value = (500, {"detail": "ws_create_boom"})

    cleanup_probe_client = create_autospec(_common.RealRunHttpClient, instance=True)
    cleanup_probe_client.__enter__.return_value = cleanup_probe_client
    cleanup_probe_client.__exit__.return_value = None
    probe_clients_iter = iter([probe_client, cleanup_probe_client])

    def _next_probe_client_ws_fail(
        base_url: str = "http://127.0.0.1:8000",
        *,
        user_id: str,
        timeout_s: float = 30.0,
        extra_headers: dict[str, str] | None = None,
    ) -> _common.RealRunHttpClient:
        return next(probe_clients_iter)

    mock_for_check_env_probe = create_autospec(
        _common.RealRunHttpClient._for_check_env_probe,
        side_effect=_next_probe_client_ws_fail,
    )
    monkeypatch.setattr(
        check_env.RealRunHttpClient,
        "_for_check_env_probe",
        mock_for_check_env_probe,
    )
    mock_probe_restore = create_autospec(
        check_env._restore_budget_state,
        return_value="budget_restore_put_http_503",
    )
    monkeypatch.setattr(check_env, "_restore_budget_state", mock_probe_restore)

    ws_fail_res = check_env.check_conversation_transport(
        base_url="http://127.0.0.1:8000",
        oauth_provenance={"status": "PASS"},
    )
    assert ws_fail_res["status"] == "FAIL"
    assert ws_fail_res["http_status"] == 500
    assert ws_fail_res["budget_restored"] is False
    assert ws_fail_res["budget_restore_error"] == "budget_restore_put_http_503"
    assert ws_fail_res["workspace_cleaned"] is True
    assert ws_fail_res["workspace_cleanup_error"] is None
    assert ws_fail_res["restore_error"] == "budget_restore_put_http_503"
    assert "workspace_create_failed:500" in ws_fail_res["reason"]
    assert "restore_error=budget_restore_put_http_503" in ws_fail_res["reason"]

    # Verify client.post raising an exception AND budget restoration failing preserves both in returned dict
    probe_client_post_exc = create_autospec(_common.RealRunHttpClient, instance=True)
    probe_client_post_exc.__enter__.return_value = probe_client_post_exc
    probe_client_post_exc.__exit__.return_value = None
    probe_client_post_exc.get.side_effect = [
        (200, {"configured": True, "daily_cap_usd": 10.0, "default_task_cap_usd": 5.0}),
        (200, {"remaining_usd": 0.0, "spent_usd": 10.0}),
    ]
    probe_client_post_exc.put.return_value = (200, {"ok": True})
    probe_client_post_exc.post.side_effect = RuntimeError("sensitive_post_network_boom")

    cleanup_probe_post_exc = create_autospec(_common.RealRunHttpClient, instance=True)
    cleanup_probe_post_exc.__enter__.return_value = cleanup_probe_post_exc
    cleanup_probe_post_exc.__exit__.return_value = None
    probe_post_exc_iter = iter([probe_client_post_exc, cleanup_probe_post_exc])

    def _next_probe_client_post_exc(
        base_url: str = "http://127.0.0.1:8000",
        *,
        user_id: str,
        timeout_s: float = 30.0,
        extra_headers: dict[str, str] | None = None,
    ) -> _common.RealRunHttpClient:
        return next(probe_post_exc_iter)

    mock_for_check_env_probe.side_effect = _next_probe_client_post_exc

    post_exc_res = check_env.check_conversation_transport(
        base_url="http://127.0.0.1:8000",
        oauth_provenance={"status": "PASS"},
    )
    assert post_exc_res["status"] == "FAIL"
    assert post_exc_res["budget_restored"] is False
    assert post_exc_res["budget_restore_error"] == "budget_restore_put_http_503"
    assert post_exc_res["restore_error"] == "budget_restore_put_http_503"
    assert "probe_request_exception:RuntimeError" in post_exc_res["reason"]
    assert "restore_error=budget_restore_put_http_503" in post_exc_res["reason"]
    assert post_exc_res["error"] == "RuntimeError: conversation_transport request failed"
    assert "sensitive_post_network_boom" not in str(post_exc_res)

    # Also verify stream_sse raising an exception + workspace delete failing while budget restore succeeds
    # records budget_restored=True and workspace_cleaned=False separately
    probe_client_sse = create_autospec(_common.RealRunHttpClient, instance=True)
    probe_client_sse.__enter__.return_value = probe_client_sse
    probe_client_sse.__exit__.return_value = None
    probe_client_sse.get.side_effect = [
        (200, {"configured": True, "daily_cap_usd": 10.0, "default_task_cap_usd": 5.0}),
        (200, {"remaining_usd": 0.0, "spent_usd": 10.0}),
    ]
    probe_client_sse.put.return_value = (200, {"ok": True})
    probe_client_sse.post.return_value = (201, {"workspace_id": "ws-probe-sse"})
    probe_client_sse.stream_sse.side_effect = RuntimeError("sse_stream_broken")

    cleanup_probe_sse = create_autospec(_common.RealRunHttpClient, instance=True)
    cleanup_probe_sse.__enter__.return_value = cleanup_probe_sse
    cleanup_probe_sse.__exit__.return_value = None
    cleanup_probe_sse.delete.return_value = (500, {})
    probe_sse_iter = iter([probe_client_sse, cleanup_probe_sse])

    def _next_probe_client_sse(
        base_url: str = "http://127.0.0.1:8000",
        *,
        user_id: str,
        timeout_s: float = 30.0,
        extra_headers: dict[str, str] | None = None,
    ) -> _common.RealRunHttpClient:
        return next(probe_sse_iter)

    mock_for_check_env_probe.side_effect = _next_probe_client_sse
    mock_probe_restore.return_value = None

    sse_fail_res = check_env.check_conversation_transport(
        base_url="http://127.0.0.1:8000",
        oauth_provenance={"status": "PASS"},
    )
    assert sse_fail_res["status"] == "FAIL"
    assert sse_fail_res["budget_restored"] is True
    assert sse_fail_res["budget_restore_error"] is None
    assert sse_fail_res["workspace_cleaned"] is False
    assert sse_fail_res["workspace_cleanup_error"] == "workspace_delete_http_500"
    assert sse_fail_res["restore_error"] == "workspace_delete_http_500"
    assert "stream_sse_exception:RuntimeError" in sse_fail_res["reason"]
    assert "restore_error=workspace_delete_http_500" in sse_fail_res["reason"]
    assert sse_fail_res["error"] == "RuntimeError: conversation_transport stream_sse failed"
    assert "sse_stream_broken" not in str(sse_fail_res)
    cleanup_probe_sse.delete.assert_called_once_with("/api/v1/workspaces/ws-probe-sse")


def test_g5_2_record_mode_uses_record_false_client_for_callback_exchange_and_record_true_preflight_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import create_autospec

    import httpx

    from scripts.dalton.realrun import (
        _common,
        check_env as _check_env_mod,
        g5_broker_and_antigravity,
    )

    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "test-service-token-value")

    client_record_args: list[tuple[str, bool | None]] = []
    preflight_record_args: list[tuple[str | None, bool | None]] = []

    def _make_g5_autospec_client(
        base_url: str = "http://127.0.0.1:8000",
        *,
        user_id: str,
        timeout_s: float = 120.0,
        extra_headers: dict[str, str] | None = None,
        record: bool | None = None,
    ) -> _common.RealRunHttpClient:
        client_record_args.append((user_id, record))
        inst = create_autospec(_common.RealRunHttpClient, instance=True)
        inst.__enter__.return_value = inst
        inst.__exit__.return_value = None
        inst.get.return_value = (200, {"deployment": "docker"})

        def _post(
            path: str,
            *,
            json_body: object = None,
            headers: dict[str, str] | None = None,
        ) -> tuple[int, dict[str, object]]:
            if path == "/api/v1/oauth/antigravity/initiate":
                return (
                    200,
                    {
                        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth?state=s1234567890",
                        "port": 51121,
                    },
                )
            if path == "/api/v1/oauth/antigravity/poll":
                return 200, {"pending": True}
            if path == "/api/v1/oauth/antigravity/callback":
                cb_in = str((json_body or {}).get("callback_input", "")) if isinstance(json_body, dict) else ""
                if "state=s1" in cb_in:
                    return 200, {"success": True, "connected": True, "email": "u@example.com"}
                return 400, {"detail": "invalid state"}
            raise AssertionError(f"Unexpected POST path: {path}")

        inst.post.side_effect = _post
        return inst

    mock_client_cls = create_autospec(_common.RealRunHttpClient, side_effect=_make_g5_autospec_client)
    mock_httpx_get = create_autospec(
        httpx.get,
        side_effect=httpx.ConnectError("Connection refused"),
    )
    mock_dotenv = create_autospec(g5_broker_and_antigravity.load_repo_dotenv, return_value=False)
    mock_pre_gate = create_autospec(
        g5_broker_and_antigravity._assert_pre_callback_git_and_backend_gate,
        return_value={"deployment": "docker"},
    )
    mock_post_checks = create_autospec(
        _check_env_mod.run_all_checks,
        return_value={"all_passed": True, "checks": {}},
    )

    def _rec_preflight(
        family: str | None = None,
        *,
        record: bool | None = None,
        base_url: str = "http://127.0.0.1:8000",
        backend: str | None = None,
        user_id: str = "realrun-pm-a",
    ) -> dict[str, object]:
        preflight_record_args.append((family, record))
        return {"deployment": "docker"}

    mock_preflight = create_autospec(
        g5_broker_and_antigravity.preflight_realrun_script,
        side_effect=_rec_preflight,
    )
    mock_run_g5_2 = create_autospec(
        g5_broker_and_antigravity._run_g5_2_antigravity,
        return_value=(
            {
                "deployment": "docker",
                "oauth_status_and_isolation": {
                    "user_a": {"status_body": {"connected": True}},
                    "user_b_isolation": {"status_body": {"connected": False}},
                },
                "gemini_4_round_tool_calling": {
                    "tool_calls_count": 4,
                    "tool_call_results_count": 4,
                    "gemini_model_invocations_count": 5,
                },
                "docker_callback_paste_flow": {
                    "callback_missing_state": {"http_status": 400},
                    "callback_wrong_state": {"http_status": 400},
                    "callback_real_paste_exchange": {"http_status": 200},
                },
            },
            [{"passed": True}],
        ),
    )
    mock_build_env = create_autospec(
        g5_broker_and_antigravity.build_evidence_envelope,
        return_value={"deployment": "docker", "assertions": [{"passed": True}]},
    )
    mock_write_ev = create_autospec(
        g5_broker_and_antigravity.write_evidence_and_summary,
        return_value=(Path("/tmp/g5.json"), Path("/tmp/G5.md"), "abc123"),
    )
    mock_sleep = create_autospec(g5_broker_and_antigravity.time.sleep, return_value=None)

    monkeypatch.setattr(g5_broker_and_antigravity, "RealRunHttpClient", mock_client_cls)
    monkeypatch.setattr(g5_broker_and_antigravity.httpx, "get", mock_httpx_get)
    monkeypatch.setattr(g5_broker_and_antigravity, "load_repo_dotenv", mock_dotenv)
    monkeypatch.setattr(
        g5_broker_and_antigravity,
        "_assert_pre_callback_git_and_backend_gate",
        mock_pre_gate,
    )
    monkeypatch.setattr(g5_broker_and_antigravity, "run_all_checks", mock_post_checks)
    monkeypatch.setattr(
        g5_broker_and_antigravity,
        "preflight_realrun_script",
        mock_preflight,
    )
    monkeypatch.setattr(
        g5_broker_and_antigravity,
        "_run_g5_2_antigravity",
        mock_run_g5_2,
    )
    monkeypatch.setattr(
        g5_broker_and_antigravity,
        "build_evidence_envelope",
        mock_build_env,
    )
    monkeypatch.setattr(
        g5_broker_and_antigravity,
        "write_evidence_and_summary",
        mock_write_ev,
    )
    monkeypatch.setattr(g5_broker_and_antigravity.time, "sleep", mock_sleep)

    # Pre-exchange _initiate_g5_2_for_user always creates its client with record=False
    g5_broker_and_antigravity._initiate_g5_2_for_user(
        "http://127.0.0.1:8000", "realrun-pm-a", record=True
    )
    assert client_record_args == [("realrun-pm-a", False)]
    client_record_args.clear()

    rc = g5_broker_and_antigravity.main(
        [
            "--mode",
            "g5.2",
            "--record",
            "--callback-url",
            "http://localhost:51121/oauth-callback?code=c1&state=s1",
        ]
    )
    assert rc == 0
    # Callback exchange client is created with record=False, while post-exchange preflight runs with record=True
    assert client_record_args == [("realrun-pm-a", False)]
    assert preflight_record_args == [("G5", True)]



@pytest.mark.parametrize(
    ("today", "refused"),
    [
        # cap - spent == 0.250000 exactly: the gate admits (float subtraction would say 0.2499...)
        ({"configured": True, "cap_usd": 0.251096, "spent_usd": 0.001096}, False),
        ({"configured": True, "cap_usd": 0.30, "spent_usd": 0.076475}, True),
        ({"configured": True, "cap_usd": 0.30, "spent_usd": 0.003}, False),
        ({"configured": False, "cap_usd": 0.0, "spent_usd": 0.0}, True),
        ({"configured": True, "cap_usd": 0.30, "spent_usd": 0.31}, True),
    ],
)
def test_g1_gate_would_refuse_mirrors_admission_gate(today: dict, refused: bool) -> None:
    from src.dalton.settings import get_dalton_settings

    g1_mod = importlib.import_module("scripts.dalton.realrun.g1_budget")
    floor = g1_mod._admission_floor_usd()
    assert floor == Decimal(str(get_dalton_settings().budget.admission_floor_usd or 0.25))
    assert g1_mod._gate_would_refuse(today, floor) is refused


def test_delete_workspace_when_idle_retries_busy_409_but_not_published_409() -> None:
    from unittest.mock import create_autospec

    from scripts.dalton.realrun._common import RealRunHttpClient, delete_workspace_when_idle

    busy = (409, {"detail": "Workspace has runs in progress"})

    client = create_autospec(RealRunHttpClient, instance=True)
    client.delete.side_effect = [busy, busy, (204, None)]
    assert delete_workspace_when_idle(client, "ws-1", timeout_s=5.0, poll_s=0.0) == 204
    assert client.delete.call_count == 3

    published = create_autospec(RealRunHttpClient, instance=True)
    published.delete.return_value = (409, {"detail": "该工作区含已发布研报，禁止删除；请归档。"})
    assert delete_workspace_when_idle(published, "ws-2", timeout_s=5.0, poll_s=0.0) == 409
    assert published.delete.call_count == 1

    stuck = create_autospec(RealRunHttpClient, instance=True)
    stuck.delete.return_value = busy
    assert delete_workspace_when_idle(stuck, "ws-3", timeout_s=0.0, poll_s=0.0) == 409
    assert stuck.delete.call_count == 1


def test_journal_dump_replayed_row_with_last_replayed_at_in_window() -> None:
    since_dt = parse_iso_utc("2026-10-21T10:00:00Z")
    raw_rows = [
        {
            "invocation_id": "inv-replayed-in-window",
            "user_id": "realrun-pm-a",
            "scope_ref": "assist:realrun-pm-a:audit.label_figures:abc",
            "purpose": "assist.audit.label_figures",
            "model_key": "gemini-3.8-flash-low-oauth",
            "provider": "antigravity-oauth",
            "billing_type": "cash",
            "state": "done",
            "attempt": 1,
            "usage": {
                "input_tokens": 500,
                "output_tokens": 120,
                "replay_count": 2,
                "last_replayed_at": "2026-10-21T10:02:15.123456Z",
            },
            "cash_usd": Decimal("0.045000"),
            "notional_usd": Decimal("0.045000"),
            "error_kind": None,
            "claimed_at": datetime(2026, 10, 20, 8, 0, 0, tzinfo=timezone.utc),
            "completed_at": datetime(2026, 10, 20, 8, 0, 2, tzinfo=timezone.utc),
            "created_at": datetime(2026, 10, 20, 8, 0, 0, tzinfo=timezone.utc),
        }
    ]
    normalized = normalize_journal_rows(raw_rows, since_dt=since_dt)
    assert len(normalized["rows"]) == 1
    assert normalized["rows"][0]["replayed"] is True
    assert normalized["rows"][0]["cached"] is True
    assert normalized["rows"][0]["replayed_from_before_window"] is True
    assert normalized["rows"][0]["cash_usd"] == pytest.approx(0.045)
    assert normalized["rows"][0]["notional_usd"] == pytest.approx(0.045)
    assert normalized["rows"][0]["usage"]["last_replayed_at"] == "2026-10-21T10:02:15.123456Z"
    assert normalized["totals"]["replayed_calls"] == 1
    assert normalized["totals"]["fresh_calls"] == 0
    assert normalized["totals"]["cash_usd"] == pytest.approx(0.0)
    assert normalized["totals"]["notional_usd"] == pytest.approx(0.0)
    assert normalized["totals"]["by_purpose"]["assist.audit.label_figures"]["cash_usd"] == pytest.approx(0.0)
    assert normalized["totals"]["by_purpose"]["assist.audit.label_figures"]["notional_usd"] == pytest.approx(0.0)
    assert normalized["totals"]["by_model"]["gemini-3.8-flash-low-oauth"]["cash_usd"] == pytest.approx(0.0)
    assert normalized["totals"]["by_model"]["gemini-3.8-flash-low-oauth"]["notional_usd"] == pytest.approx(0.0)


def test_g3_make_unique_alpha_tag_and_cache_window_counts() -> None:
    from scripts.dalton.realrun import g3_publish

    tags = [g3_publish._make_unique_alpha_tag() for _ in range(20)]
    for t in tags:
        assert len(t) == 8
        assert t.isalpha() and t.islower()
        assert not any(ch.isdigit() for ch in t)
    assert len(set(tags)) == len(tags)

    since_dt = parse_iso_utc("2026-10-21T10:00:00.000000Z")
    # 1. Row created in run (Step 1, 10:01:00Z, outside Step 6 window), replay_count=3,
    #    last_replayed_at inside Step 6 window [10:05:00.000000Z, 10:05:20.000000Z] -> fresh=0, replayed=1
    normalized_step1_replayed = normalize_journal_rows(
        [
            {
                "invocation_id": "inv-created-in-step1-replayed-3x",
                "user_id": "realrun-pm-a",
                "scope_ref": "assist:realrun-pm-a:audit.label_figures:sec2",
                "purpose": "assist.audit.label_figures",
                "model_key": "gemini-3.8-flash-low-oauth",
                "provider": "antigravity-oauth",
                "billing_type": "subscription",
                "state": "done",
                "attempt": 1,
                "usage": {
                    "input_tokens": 500,
                    "output_tokens": 100,
                    "replay_count": 3,
                    "last_replayed_at": "2026-10-21T10:05:10.500000Z",
                },
                "cash_usd": Decimal("0"),
                "notional_usd": Decimal("0.001"),
                "created_at": datetime(2026, 10, 21, 10, 1, 0, tzinfo=timezone.utc),
            }
        ],
        since_dt=since_dt,
    )
    assert len(normalized_step1_replayed["rows"]) == 4  # 1 fresh + 3 expanded replay items
    assert g3_publish._count_window_fresh_and_replayed(
        normalized_step1_replayed["rows"],
        "2026-10-21T10:05:00.000000Z",
        "2026-10-21T10:05:20.000000Z",
    ) == (0, 1)

    # 2. Row with last_replayed_at outside Step 6 window -> (0, 0)
    # 3. publish.review row inside Step 6 window -> excluded from audit counts -> (0, 0)
    normalized_outside_and_review = normalize_journal_rows(
        [
            {
                "invocation_id": "inv-replayed-outside-step6",
                "user_id": "realrun-pm-a",
                "scope_ref": "assist:realrun-pm-a:audit.judge:sec3",
                "purpose": "assist.audit.judge",
                "model_key": "gemini-3.8-flash-low-oauth",
                "provider": "antigravity-oauth",
                "billing_type": "subscription",
                "state": "done",
                "attempt": 1,
                "usage": {
                    "input_tokens": 300,
                    "output_tokens": 80,
                    "replay_count": 1,
                    "last_replayed_at": "2026-10-21T10:03:00.000000Z",
                },
                "cash_usd": Decimal("0"),
                "notional_usd": Decimal("0.001"),
                "created_at": datetime(2026, 10, 20, 8, 0, 0, tzinfo=timezone.utc),
            },
            {
                "invocation_id": "inv-publish-review-in-step6",
                "user_id": "realrun-pm-a",
                "scope_ref": "review:googl-v2",
                "purpose": "publish.review",
                "model_key": "claude-sonnet-5-5-low-oauth",
                "provider": "anthropic-oauth",
                "billing_type": "subscription",
                "state": "done",
                "attempt": 1,
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 200,
                    "replay_count": 1,
                    "last_replayed_at": "2026-10-21T10:05:15.000000Z",
                },
                "cash_usd": Decimal("0"),
                "notional_usd": Decimal("0.01"),
                "created_at": datetime(2026, 10, 21, 10, 5, 14, tzinfo=timezone.utc),
            },
        ],
        since_dt=since_dt,
    )
    assert g3_publish._count_window_fresh_and_replayed(
        normalized_outside_and_review["rows"],
        "2026-10-21T10:05:00.000000Z",
        "2026-10-21T10:05:20.000000Z",
    ) == (0, 0)

    # Combined: 1 prior-replayed in Step 6 + 1 fresh audit.judge in Step 6 -> (1, 1)
    normalized_combined = normalize_journal_rows(
        [
            {
                "invocation_id": "inv-prior-replayed-in-step6",
                "user_id": "realrun-pm-a",
                "scope_ref": "assist:realrun-pm-a:audit.label_figures:sec2",
                "purpose": "assist.audit.label_figures",
                "model_key": "gemini-3.8-flash-low-oauth",
                "provider": "antigravity-oauth",
                "billing_type": "subscription",
                "state": "done",
                "attempt": 1,
                "usage": {
                    "input_tokens": 500,
                    "output_tokens": 100,
                    "replay_count": 3,
                    "last_replayed_at": "2026-10-21T10:05:10.500000Z",
                },
                "cash_usd": Decimal("0"),
                "notional_usd": Decimal("0.001"),
                "created_at": datetime(2026, 10, 20, 8, 0, 0, tzinfo=timezone.utc),
            },
            {
                "invocation_id": "inv-fresh-in-step6",
                "user_id": "realrun-pm-a",
                "scope_ref": "assist:realrun-pm-a:audit.judge:sec1-v2",
                "purpose": "assist.audit.judge",
                "model_key": "gemini-3.8-flash-low-oauth",
                "provider": "antigravity-oauth",
                "billing_type": "subscription",
                "state": "done",
                "attempt": 1,
                "usage": {"input_tokens": 510, "output_tokens": 105},
                "cash_usd": Decimal("0"),
                "notional_usd": Decimal("0.001"),
                "created_at": datetime(2026, 10, 21, 10, 5, 12, 250000, tzinfo=timezone.utc),
            },
        ],
        since_dt=since_dt,
    )
    assert g3_publish._count_window_fresh_and_replayed(
        normalized_combined["rows"],
        "2026-10-21T10:05:00.000000Z",
        "2026-10-21T10:05:20.000000Z",
    ) == (1, 1)
