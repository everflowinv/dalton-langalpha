"""Batch 3 (Part 1) unit tests: skill load-by-name interception, sandbox skill cleanup,
d046 scoped data-map cache & capability filtering, and capability-gated prompts / automation README.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from typing import Any
from unittest.mock import create_autospec

import psycopg
import pytest
from alembic.operations import Operations

from ptc_agent.agent.backends import automations as automations_backend_mod
from ptc_agent.agent.middleware.runtime_context.profile import answer_preferences
from ptc_agent.agent.middleware.skills import content as skill_content_mod
from ptc_agent.agent.middleware.skills import dalton_registry as dalton_reg_mod
from ptc_agent.agent.middleware.skills import registry as skill_reg_mod
from ptc_agent.agent.prompts import loader as prompt_loader_mod
from ptc_agent.config.core import (
    CoreConfig,
    DaytonaConfig,
    FilesystemConfig,
    LoggingConfig,
    MCPConfig,
    SandboxConfig,
    SecurityConfig,
)
from ptc_agent.core.sandbox import assets as sandbox_assets_mod
from ptc_agent.core.sandbox import ptc_sandbox as ptc_sandbox_mod
from ptc_agent.core.sandbox.runtime import (
    ExecResult,
    RuntimeState,
    SandboxProvider,
    SandboxRuntime,
)
from src.config.core import clear_config_cache
from src.dalton import transcription_storage as transcription_storage_mod
from src.dalton.capabilities import (
    CAPABILITY_SPECS,
    CORE_CAPABILITY,
    CapabilityId,
    capabilities_fingerprint,
)
from src.dalton.data_map import builder as map_builder
from src.dalton.database import data_map as db_data_map
from src.dalton.migrations.versions import d046_scoped_data_map_cache as d046
from src.dalton.services import transcriptions as svc_transcriptions
from src.dalton.settings import get_dalton_config

# §3.4 #2 proprietary text blacklist (case-insensitive)
PROPRIETARY_TEXT_BLACKLIST: tuple[str, ...] = (
    "researchdata",
    "alphaengine",
    "guidepoint",
    "sales-notes",
    "tmtb",
    "funda-ai",
    "feishu",
    "weixin",
    "seed-asr",
    "dalton-13f-tracker",
    "everflow",
)

# Independent list of the 22 Everflow-coupled Dalton skills (not derived from DALTON_SKILL_CAPABILITY_MAP)
EXPLICIT_PROPRIETARY_SKILLS: frozenset[str] = frozenset(
    {
        "dalton-transcript-polish",
        "dalton-bullbear-tracker",
        "dalton-ai-finance-daily",
        "dalton-market-digest",
        "dalton-us-weekly-movers",
        "dalton-x-accounts-daily",
        "dalton-humanoid-robot-weekly",
        "dalton-weekly-trend-impact-scan",
        "dalton-ct-semi-sentiment",
        "dalton-china-consumer-weekly",
        "dalton-global-consumer-weekly",
        "dalton-daily-filing-alert",
        "dalton-tob-industry-weekly",
        "dalton-vip-news-alert",
        "dalton-mark-weekly-newenergy",
        "dalton-tmeet-weekly-minutes",
        "dalton-ai-app-weekly-monitor",
        "dalton-richard-earnings",
        "dalton-richard-coverage-weekly",
        "dalton-13f-tracker",
        "dalton-wiki-auto-ingest",
        "dalton-podcast-weekly",
    }
)

_CLEAR_ENV_VARS: tuple[str, ...] = (
    "APP_ENV",
    "DALTON_PROFILE",
    "DALTON_HOST_PLATFORM",
    "DALTON_ENABLED",
    "DALTON_RESEARCHDATA_URL",
    "DALTON_BRIDGE_URL",
    "CHANNEL_GATEWAY_URL",
    "DALTON_TRANSCRIPTION_ENABLED",
    "DALTON_ASR_STORAGE_ENDPOINT_URL",
    "DALTON_ASR_STORAGE_BUCKET_NAME",
    "DALTON_ASR_STORAGE_ACCESS_KEY_ID",
    "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY",
    "DALTON_DATA_JOBS_ENABLED",
    "BRAND_NAME",
    *(spec.env_var for spec in CAPABILITY_SPECS.values()),
)


def _clean_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    app_env: str | None = None,
    dalton_profile: str | None = None,
) -> None:
    for var in _CLEAR_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    if app_env is not None:
        monkeypatch.setenv("APP_ENV", app_env)
    if dalton_profile is not None:
        monkeypatch.setenv("DALTON_PROFILE", dalton_profile)
    clear_config_cache()
    get_dalton_config.cache_clear()
    map_builder.set_turn_data_map(None)
    prompt_loader_mod.reset_loader()


def _assert_zero_blacklist_hits(text: str, *, label: str) -> None:
    lowered = text.lower()
    hits = [term for term in PROPRIETARY_TEXT_BLACKLIST if term in lowered]
    assert not hits, f"{label} leaked proprietary term(s): {hits}"


def _make_sandbox_config() -> CoreConfig:
    return CoreConfig(
        sandbox=SandboxConfig(daytona=DaytonaConfig(api_key="test-key")),
        security=SecurityConfig(),
        mcp=MCPConfig(),
        logging=LoggingConfig(),
        filesystem=FilesystemConfig(),
    )


# ---------------------------------------------------------------------------
# 1. Skill capability registration, load-by-name interception & sandbox prune
# ---------------------------------------------------------------------------


class TestSkillCapabilityInterceptionAndPrune:
    def test_every_dalton_skill_on_disk_is_explicitly_registered(self) -> None:
        disk_skills = {
            p.name
            for p in dalton_reg_mod.DALTON_SKILLS_DIR.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        }
        assert len(disk_skills) == 52
        assert set(dalton_reg_mod.DALTON_SKILL_CAPABILITY_MAP.keys()) == disk_skills

        for name, req in dalton_reg_mod.DALTON_SKILL_CAPABILITY_MAP.items():
            if req is CORE_CAPABILITY:
                assert name not in EXPLICIT_PROPRIETARY_SKILLS, (
                    f"Proprietary skill {name!r} wrongly marked CORE_CAPABILITY"
                )
            elif isinstance(req, tuple):
                assert req and all(isinstance(c, CapabilityId) for c in req)
            else:
                assert isinstance(req, CapabilityId)

        gated = {
            name
            for name, req in dalton_reg_mod.DALTON_SKILL_CAPABILITY_MAP.items()
            if req is not CORE_CAPABILITY
        }
        assert gated == EXPLICIT_PROPRIETARY_SKILLS

    @pytest.mark.parametrize(
        ("app_env", "dalton_profile"),
        [("core", None), ("", "core")],
    )
    def test_core_profile_intercepts_load_by_name_for_disabled_skills(
        self,
        monkeypatch: pytest.MonkeyPatch,
        app_env: str,
        dalton_profile: str | None,
    ) -> None:
        _clean_env(monkeypatch, app_env=app_env, dalton_profile=dalton_profile)
        repo_skill_dirs = [str(dalton_reg_mod.DALTON_SKILLS_DIR)]

        registry = skill_reg_mod.get_skill_registry()
        listed = {entry["name"] for entry in skill_reg_mod.list_skills()}
        outside = skill_reg_mod.skills_outside_sandbox()
        commands = skill_reg_mod.get_command_to_skill_map()

        assert "transcribe" not in commands
        assert dalton_reg_mod.disabled_dalton_skills() == EXPLICIT_PROPRIETARY_SKILLS

        for skill_name in sorted(EXPLICIT_PROPRIETARY_SKILLS):
            assert skill_reg_mod.get_skill(skill_name) is None
            assert skill_name not in registry
            assert skill_name not in listed
            assert skill_name in outside
            assert (
                skill_content_mod.resolve_skill_dir(skill_name, skill_dirs=repo_skill_dirs)
                is None
            )
            assert (
                skill_content_mod.load_skill_content(skill_name, skill_dirs=repo_skill_dirs)
                is None
            )
            assert (
                skill_content_mod.build_skill_content(
                    [skill_content_mod.SkillRequest(name=skill_name)],
                    skill_dirs=repo_skill_dirs,
                )
                is None
            )

        # Core skeleton skills remain loadable
        for core_skill in (
            "dalton-conventions",
            "dalton-publish",
            "dalton-knowledge",
            "dalton-bdc-analyst",
            "dalton-short-screen",
            "dalton-option-put-skew",
            "dalton-language",
            "dalton-onboarding",
        ):
            assert skill_reg_mod.get_skill(core_skill) is not None
            assert core_skill in registry

    def test_runtime_capability_toggles_update_skill_access_without_reimport(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clean_env(monkeypatch, app_env="core")
        assert skill_reg_mod.get_skill("dalton-13f-tracker") is None
        assert skill_reg_mod.get_skill("dalton-transcript-polish") is None
        assert skill_reg_mod.get_skill("dalton-wiki-auto-ingest") is None

        # Enable FUND_AUTOMATIONS -> unlocks fund automation & 13F skills dynamically
        monkeypatch.setenv("DALTON_CAP_FUND_AUTOMATIONS", "true")
        assert skill_reg_mod.get_skill("dalton-13f-tracker") is not None
        assert "dalton-13f-tracker" not in skill_reg_mod.skills_outside_sandbox()
        assert skill_reg_mod.get_skill("dalton-transcript-polish") is None

        # Enable TRANSCRIPTION (+ bridge + storage) -> unlocks dalton-transcript-polish
        monkeypatch.setenv("DALTON_CAP_RESEARCHDATA_BRIDGE", "true")
        monkeypatch.setenv("DALTON_CAP_TRANSCRIPTION", "true")
        monkeypatch.setattr(
            svc_transcriptions,
            "is_transcription_storage_configured",
            create_autospec(
                transcription_storage_mod.is_transcription_storage_configured,
                return_value=True,
            ),
        )
        tp = skill_reg_mod.get_skill("dalton-transcript-polish")
        assert tp is not None and tp.hidden is False
        assert "dalton-transcript-polish" not in skill_reg_mod.skills_outside_sandbox()
        assert any(s["name"] == "dalton-transcript-polish" for s in skill_reg_mod.list_skills())

        # Everflow: disabling a single capability blocks its skills dynamically
        _clean_env(monkeypatch, app_env="dalton")
        assert skill_reg_mod.get_skill("dalton-wiki-auto-ingest") is not None
        monkeypatch.setenv("DALTON_CAP_COMPANY_WIKI", "false")
        assert skill_reg_mod.get_skill("dalton-wiki-auto-ingest") is None
        assert "dalton-wiki-auto-ingest" in skill_reg_mod.skills_outside_sandbox()

    @pytest.mark.asyncio
    async def test_running_sandbox_sync_prunes_disabled_managed_skills(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clean_env(monkeypatch, app_env="core")

        mock_runtime = create_autospec(SandboxRuntime, instance=True)
        mock_runtime.id = "rt-b3"
        mock_runtime.working_dir = "/home/workspace"
        mock_runtime.get_state.return_value = RuntimeState.RUNNING
        mock_runtime.exec.return_value = ExecResult("", "", 0)

        mock_provider = create_autospec(SandboxProvider, instance=True)
        mock_provider.is_transient_error.return_value = False
        monkeypatch.setattr(
            ptc_sandbox_mod,
            "create_provider",
            create_autospec(ptc_sandbox_mod.create_provider, return_value=mock_provider),
        )

        sandbox = ptc_sandbox_mod.PTCSandbox(config=_make_sandbox_config())
        sandbox.runtime = mock_runtime
        skills_base = "/home/workspace/.agents/skills"

        # Case 1: Existing lock has platform-owned stale disabled skills + active core + user skill
        mock_runtime.list_files.return_value = [
            {"name": "dalton-conventions", "path": f"{skills_base}/dalton-conventions", "is_dir": True},
            {"name": "dalton-13f-tracker", "path": f"{skills_base}/dalton-13f-tracker", "is_dir": True},
            {"name": "dalton-market-digest", "path": f"{skills_base}/dalton-market-digest", "is_dir": True},
            {"name": "user-custom-skill", "path": f"{skills_base}/user-custom-skill", "is_dir": True},
        ]
        lock = {
            "dalton-conventions": {"owner": "platform", "name": "dalton-conventions"},
            "dalton-13f-tracker": {"owner": "platform", "name": "dalton-13f-tracker"},
            "dalton-market-digest": {"owner": "platform", "name": "dalton-market-digest"},
            "user-custom-skill": {"owner": "user", "name": "user-custom-skill"},
        }

        local_names = await sandbox_assets_mod._collect_local_skill_names(
            [str(dalton_reg_mod.DALTON_SKILLS_DIR)]
        )
        assert "dalton-conventions" in local_names
        assert "dalton-13f-tracker" not in local_names
        assert "dalton-market-digest" not in local_names

        await sandbox_assets_mod._prune_remote_skills(
            sandbox, local_names, existing_lock=lock
        )
        removed_cmds = [call.args[0] for call in mock_runtime.exec.call_args_list]
        assert len(removed_cmds) == 2
        assert any("dalton-13f-tracker" in c and "rm -rf" in c for c in removed_cmds)
        assert any("dalton-market-digest" in c and "rm -rf" in c for c in removed_cmds)
        assert all("dalton-conventions" not in c for c in removed_cmds)
        assert all("user-custom-skill" not in c for c in removed_cmds)

        # Case 2: Lock absent/unreadable (None) -> preserves ALL dirs (never guesses ownership by name)
        mock_runtime.exec.reset_mock()
        mock_runtime.list_files.return_value = [
            {"name": "dalton-13f-tracker", "path": f"{skills_base}/dalton-13f-tracker", "is_dir": True},
            {"name": "unknown-agent-skill", "path": f"{skills_base}/unknown-agent-skill", "is_dir": True},
        ]
        failed_no_lock = await sandbox_assets_mod._prune_remote_skills(
            sandbox, local_names, existing_lock=None
        )
        assert failed_no_lock == set()
        assert mock_runtime.exec.call_count == 0

        # Case 3: rm -rf exits non-zero OR exit_code is None -> returns failed skill names
        for bad_exit in (1, None):
            mock_runtime.exec.reset_mock()
            mock_runtime.exec.return_value = ExecResult("", "permission denied", bad_exit)  # type: ignore[arg-type]
            mock_runtime.list_files.return_value = [
                {"name": "dalton-13f-tracker", "path": f"{skills_base}/dalton-13f-tracker", "is_dir": True},
            ]
            failed_bad = await sandbox_assets_mod._prune_remote_skills(
                sandbox,
                local_names,
                existing_lock={"dalton-13f-tracker": {"owner": "platform", "name": "dalton-13f-tracker"}},
            )
            assert failed_bad == {"dalton-13f-tracker"}

        # Case 4: After prune failure, _upload_skills retains the failed platform skill's lock entry
        # so the next sync still knows its ownership and retries pruning instead of skipping forever.
        captured_entries: dict[str, Any] = {}

        async def _fake_install_staged(
            _sb: Any,
            _staging: str,
            _names: Any,
            entries: dict[str, Any],
            *,
            base: str | None = None,
        ) -> tuple[dict[str, Any], list[str]]:
            _ = base
            captured_entries.clear()
            captured_entries.update(entries)
            return {"version": 1, "skills": dict(entries)}, []

        from ptc_agent.core.sandbox import skill_sync as skill_sync_mod

        monkeypatch.setattr(
            skill_sync_mod,
            "install_staged_skills",
            create_autospec(skill_sync_mod.install_staged_skills, side_effect=_fake_install_staged),
        )
        monkeypatch.setattr(
            sandbox_assets_mod,
            "upload_tree",
            create_autospec(sandbox_assets_mod.upload_tree, return_value=None),
        )
        prev_lock = {
            "dalton-conventions": {"owner": "platform", "sourceType": "platform", "name": "dalton-conventions"},
            "dalton-13f-tracker": {"owner": "platform", "sourceType": "platform", "name": "dalton-13f-tracker"},
        }
        mini_manifest = {
            "files": {"dalton-conventions/SKILL.md": "abc"},
            "skills": {
                "dalton-conventions": {
                    "lock_entry": {"owner": "platform", "sourceType": "platform", "name": "dalton-conventions"}
                }
            },
        }
        merged_lock, _collisions = await sandbox_assets_mod._upload_skills(
            sandbox,
            [str(dalton_reg_mod.DALTON_SKILLS_DIR)],
            manifest=mini_manifest,
            existing_lock=prev_lock,
            failed_prunes={"dalton-13f-tracker"},
        )
        assert merged_lock is not None
        assert "dalton-13f-tracker" in captured_entries
        assert captured_entries["dalton-13f-tracker"]["owner"] == "platform"

        # Next sync sees the retained lock entry and successfully retries rm -rf
        mock_runtime.exec.reset_mock()
        mock_runtime.exec.return_value = ExecResult("", "", 0)
        mock_runtime.list_files.return_value = [
            {"name": "dalton-conventions", "path": f"{skills_base}/dalton-conventions", "is_dir": True},
            {"name": "dalton-13f-tracker", "path": f"{skills_base}/dalton-13f-tracker", "is_dir": True},
        ]
        retry_failed = await sandbox_assets_mod._prune_remote_skills(
            sandbox,
            local_names,
            existing_lock=merged_lock["skills"],
        )
        assert retry_failed == set()
        assert mock_runtime.exec.call_count == 1
        assert "dalton-13f-tracker" in mock_runtime.exec.call_args_list[0].args[0]

    def test_workspace_link_unpins_disabled_managed_copies_but_keeps_user_skills(
        self,
        tmp_path: Any,
    ) -> None:
        import json as _json
        import os as _os
        import subprocess as _subprocess
        import sys as _sys

        from ptc_agent.core.sandbox import skill_sync as skill_sync_mod

        user_base = tmp_path / ".agents" / "skills"
        ws_base = tmp_path / "ws-a" / ".agents" / "skills"
        (user_base / "dalton-conventions").mkdir(parents=True)
        (user_base / "dalton-conventions" / "SKILL.md").write_text("# active\n", encoding="utf-8")

        # Restored workspace has stale real copies of both active & disabled platform skills + user skill
        for name in ("dalton-conventions", "dalton-13f-tracker", "my-custom-skill"):
            (ws_base / name).mkdir(parents=True)
            (ws_base / name / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")

        lock_payload = {
            "version": 1,
            "skills": {
                "dalton-conventions": {"owner": "platform", "sourceType": "platform"},
                "dalton-13f-tracker": {"owner": "platform", "sourceType": "platform"},
                "my-custom-skill": {"owner": "user", "sourceType": "agent"},
            },
        }
        (ws_base / "skills-lock.json").write_text(_json.dumps(lock_payload), encoding="utf-8")

        args_payload = {
            "lockVersion": 1,
            "managedSourceType": "langalpha-user",
            "orphanTtl": 3600,
            "internal": str(tmp_path / "_internal"),
            "mode": "link",
            "base": str(ws_base),
            "userBase": str(user_base),
            "targetPrefix": "../../../.agents/skills",
            "disabled": [],
            "disabledManaged": ["dalton-13f-tracker"],
            "own": [],
        }
        rendered_script = skill_sync_mod._SCRIPT.replace(
            "__ARGS_ENV__", repr(skill_sync_mod._ARGS_ENV)
        ).replace("__ARGS_FILE_ENV__", repr(skill_sync_mod._ARGS_FILE_ENV))
        proc = _subprocess.run(
            [_sys.executable, "-I", "-c", rendered_script],
            env={**_os.environ, skill_sync_mod._ARGS_ENV: _json.dumps(args_payload)},
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert proc.returncode == 0, f"skill_sync failed: {proc.stderr}\n{proc.stdout}"
        out = _json.loads(proc.stdout.strip().splitlines()[-1])
        assert out["status"] == "ok"
        assert "dalton-13f-tracker" in out["unpinned"]
        assert "dalton-conventions" in out["unpinned"]
        assert not (ws_base / "dalton-13f-tracker").exists()
        assert (ws_base / "dalton-conventions").is_symlink()
        assert (ws_base / "my-custom-skill" / "SKILL.md").is_file()

    def test_tmeet_weekly_minutes_requires_both_fund_automations_and_feishu_docs(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clean_env(monkeypatch, app_env="core")
        monkeypatch.setenv("DALTON_CAP_RESEARCHDATA_BRIDGE", "true")
        monkeypatch.setenv("DALTON_CAP_FUND_AUTOMATIONS", "true")
        assert skill_reg_mod.get_skill("dalton-tmeet-weekly-minutes") is None

        monkeypatch.setenv("DALTON_CAP_FEISHU_DOCS", "true")
        assert skill_reg_mod.get_skill("dalton-tmeet-weekly-minutes") is not None


# ---------------------------------------------------------------------------
# 2. d046 migration, dual-key cache isolation, sources.yaml & builder filtering
# ---------------------------------------------------------------------------


class TestDataMapD046AndFiltering:
    def test_d046_migration_upgrade_and_downgrade_sql(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        assert d046.revision == "d046"
        assert d046.down_revision == "d048"

        executed_up: list[str] = []
        op_mock = create_autospec(Operations, instance=True)
        op_mock.execute.side_effect = lambda sql: executed_up.append(str(sql))
        monkeypatch.setattr(d046, "op", op_mock)

        d046.upgrade()
        joined_up = "\n".join(executed_up)
        assert "SET LOCAL lock_timeout = '5s'" in joined_up
        assert "CREATE TABLE IF NOT EXISTS dalton_data_map_scoped_cache" in joined_up
        assert "PRIMARY KEY (cache_date, capabilities_fingerprint)" in joined_up
        assert "dalton_data_map_cache" not in joined_up

        executed_down: list[str] = []
        op_mock.execute.side_effect = lambda sql: executed_down.append(str(sql))
        d046.downgrade()
        joined_down = "\n".join(executed_down)
        assert "SET LOCAL lock_timeout = '5s'" in joined_down
        assert "DROP TABLE IF EXISTS dalton_data_map_scoped_cache" in joined_down
        assert "dalton_data_map_cache" not in joined_down

    @pytest.mark.asyncio
    async def test_scoped_cache_read_write_and_fingerprint_isolation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        table: dict[tuple[date, str], dict[str, Any]] = {}
        seen_sql: list[str] = []

        class _InMemoryScopedCursor:
            def __init__(self) -> None:
                self._row: dict[str, Any] | None = None

            async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
                seen_sql.append(sql)
                assert "dalton_data_map_scoped_cache" in sql
                assert "FROM dalton_data_map_cache" not in sql
                assert "INTO dalton_data_map_cache" not in sql
                if "INSERT INTO dalton_data_map_scoped_cache" in sql:
                    cdate, fp, md, payload_jsonb = params
                    payload_val = getattr(payload_jsonb, "obj", payload_jsonb)
                    table[(cdate, fp)] = {
                        "cache_date": cdate,
                        "capabilities_fingerprint": fp,
                        "rendered_markdown": md,
                        "payload": dict(payload_val or {}),
                        "updated_at": "2026-11-23T00:00:00+00:00",
                    }
                    self._row = None
                elif "SELECT cache_date" in sql:
                    cdate, fp = params
                    self._row = table.get((cdate, fp))

            async def fetchone(self) -> dict[str, Any] | None:
                return self._row

        fake_conn = create_autospec(psycopg.AsyncConnection, instance=True)

        @asynccontextmanager
        async def _tx_ctx():
            yield

        @asynccontextmanager
        async def _cur_ctx(*args: Any, **kwargs: Any):
            yield _InMemoryScopedCursor()

        fake_conn.transaction.side_effect = _tx_ctx
        fake_conn.cursor.side_effect = _cur_ctx

        target = date(2026, 11, 23)

        # Write under Everflow fingerprint
        _clean_env(monkeypatch, app_env="dalton")
        fp_everflow = capabilities_fingerprint()
        await db_data_map.upsert_cached_data_map(
            target, "## Everflow Map", {"profile": "everflow"}, conn=fake_conn
        )

        # Write under Core fingerprint on the SAME date
        _clean_env(monkeypatch, app_env="core")
        fp_core = capabilities_fingerprint()
        assert fp_core != fp_everflow
        await db_data_map.upsert_cached_data_map(
            target, "## Core Map", {"profile": "core"}, conn=fake_conn
        )

        # Core reads ONLY Core row
        got_core = await db_data_map.get_cached_data_map(target, conn=fake_conn)
        assert got_core is not None
        assert got_core["rendered_markdown"] == "## Core Map"
        assert got_core["capabilities_fingerprint"] == fp_core

        # Everflow reads ONLY Everflow row
        _clean_env(monkeypatch, app_env="dalton")
        got_ef = await db_data_map.get_cached_data_map(target, conn=fake_conn)
        assert got_ef is not None
        assert got_ef["rendered_markdown"] == "## Everflow Map"
        assert got_ef["capabilities_fingerprint"] == fp_everflow

        # Different capability subset misses cleanly instead of reading Everflow/Core row
        monkeypatch.setenv("DALTON_CAP_GUIDEPOINT", "false")
        fp_subset = capabilities_fingerprint()
        assert fp_subset not in (fp_core, fp_everflow)
        assert await db_data_map.get_cached_data_map(target, conn=fake_conn) is None

        # Explicit fingerprint lookup works regardless of process env
        got_explicit = await db_data_map.get_cached_data_map(
            target, capabilities_fingerprint=fp_core, conn=fake_conn
        )
        assert got_explicit is not None
        assert got_explicit["rendered_markdown"] == "## Core Map"

    def test_sources_yaml_every_entry_declares_capability(self) -> None:
        spec = map_builder.load_sources_spec()
        valid_cap_values = {c.value for c in CapabilityId}

        for section in ("catalog_routes", "direct_tools"):
            entries = spec.get(section)
            assert isinstance(entries, dict) and entries
            for key, entry in entries.items():
                assert "capability" in entry, f"sources.yaml {section}.{key} missing 'capability'"
                cap = entry["capability"]
                if cap is None:
                    continue
                if isinstance(cap, str):
                    assert cap in valid_cap_values
                elif isinstance(cap, list):
                    assert cap and all(c in valid_cap_values for c in cap)
                else:
                    pytest.fail(f"Invalid capability value for {section}.{key}: {cap!r}")

        unavail = spec.get("unavailable_sources")
        assert isinstance(unavail, list) and unavail
        for item in unavail:
            assert "capability" in item, f"unavailable_sources.{item.get('id')} missing 'capability'"

    def test_core_data_map_filters_routes_and_unavailable_sources_with_zero_proprietary_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clean_env(monkeypatch, app_env="core")

        # Live catalog routes are filtered out when researchdata_bridge is off
        live_routes = [
            {"id": "all", "category": "research"},
            {"id": "alphaengine", "category": "research"},
            {"id": "guidepoint", "category": "research"},
            {"id": "sec", "category": "finance"},
            {"id": "custom-bridge-route", "category": "research"},
        ]
        assert map_builder.merge_catalog_routes(catalog_routes=live_routes) == []
        assert map_builder.merge_catalog_routes(catalog_routes=None) == []

        rendered_static = map_builder.render_static_data_map_markdown(
            team_memory_enabled=True,
        )
        _assert_zero_blacklist_hits(rendered_static, label="core static data_map")
        assert "### 1. 直接工具" in rendered_static
        assert "### 2. 明确没有的数据（切勿以为查过）" in rendered_static
        assert "### 3. " not in rendered_static
        assert "`conversation_search`" not in rendered_static
        assert "`market_quotes`" in rendered_static
        assert "`sec_filings`" in rendered_static
        assert "未接通(期权链用get_options_chain)。" in rendered_static
        assert "未接直连，切勿以为已核查。" in rendered_static

        # Partial capability: bridge + alphaengine only -> alt_data names ONLY alphaengine
        monkeypatch.setenv("DALTON_HOST_PLATFORM", "macos")
        monkeypatch.setenv("DALTON_CAP_RESEARCHDATA_BRIDGE", "true")
        monkeypatch.setenv("DALTON_CAP_ALPHAENGINE", "true")
        partial_routes = [r["id"] for r in map_builder.merge_catalog_routes(catalog_routes=None)]
        assert "alphaengine" in partial_routes
        assert "transcript" in partial_routes
        assert "guidepoint" not in partial_routes
        assert "funda-ai" not in partial_routes
        assert "gcal" not in partial_routes

        partial_md = map_builder.render_static_data_map_markdown()
        assert "仅从alphaengine查二手引述" in partial_md
        assert "guidepoint" not in partial_md.lower()
        assert "funda-ai" not in partial_md.lower()
        assert "dalton-13f-tracker" not in partial_md.lower()

        # Partial capability: ONLY researchdata_bridge -> `all` route narrows copy (no AE/GP/四库/专家)
        _clean_env(monkeypatch, app_env="core")
        monkeypatch.setenv("DALTON_CAP_RESEARCHDATA_BRIDGE", "true")
        bridge_only_routes = {
            r["id"]: r for r in map_builder.merge_catalog_routes(catalog_routes=None)
        }
        assert "all" in bridge_only_routes
        all_text = f"{bridge_only_routes['all']['rules']} {bridge_only_routes['all']['description']}"
        for forbidden in ("四库", "AE", "GP", "卖方", "专家"):
            assert forbidden not in all_text, f"`all` route leaked {forbidden!r}: {all_text}"

    @pytest.mark.asyncio
    async def test_d046_upgrade_backfills_scoped_cache_from_recent_health_without_reprobing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from datetime import UTC, datetime, timedelta

        from src.dalton.data_map import health as map_health

        _clean_env(monkeypatch, app_env="dalton")
        fp = capabilities_fingerprint()
        stored_scoped: dict[tuple[date, str], dict[str, Any]] = {}

        async def _fake_get_cached(
            cache_date: date | None = None,
            *,
            capabilities_fingerprint: str | None = None,
            conn: Any = None,
        ) -> dict[str, Any] | None:
            _ = conn
            key = (cache_date or map_builder.today_cache_date(), capabilities_fingerprint or fp)
            return stored_scoped.get(key)

        async def _fake_upsert_cached(
            cache_date: date,
            rendered_markdown: str,
            payload: dict[str, Any] | None = None,
            *,
            capabilities_fingerprint: str | None = None,
            conn: Any = None,
        ) -> None:
            _ = conn
            eff_fp = capabilities_fingerprint or fp
            stored_scoped[(cache_date, eff_fp)] = {
                "cache_date": cache_date,
                "capabilities_fingerprint": eff_fp,
                "rendered_markdown": rendered_markdown,
                "payload": payload or {},
            }

        recent_ts = datetime.now(UTC) - timedelta(hours=2)
        recent_health = {
            "researchdata:alphaengine": {
                "status": "available",
                "latest_date": "2026-11-22",
                "checked_at": recent_ts,
            },
            "x_search": {
                "status": "available",
                "latest_date": "2026-11-23",
                "checked_at": recent_ts,
            },
        }

        monkeypatch.setattr(
            map_health,
            "_is_data_health_paused_by_user",
            create_autospec(map_health._is_data_health_paused_by_user, return_value=False),
        )
        monkeypatch.setattr(
            db_data_map,
            "get_cached_data_map",
            create_autospec(db_data_map.get_cached_data_map, side_effect=_fake_get_cached),
        )
        monkeypatch.setattr(
            db_data_map,
            "upsert_cached_data_map",
            create_autospec(db_data_map.upsert_cached_data_map, side_effect=_fake_upsert_cached),
        )
        monkeypatch.setattr(
            db_data_map,
            "fetch_all_source_health",
            create_autospec(db_data_map.fetch_all_source_health, return_value=recent_health),
        )
        monkeypatch.setattr(
            db_data_map,
            "fetch_conversation_archive_stats",
            create_autospec(
                db_data_map.fetch_conversation_archive_stats,
                return_value={"card_count": 100, "latest_date": "2026-11-18"},
            ),
        )
        saved_catalog_routes = [
            {"id": "alphaengine", "category": "research", "title": "卖方研报"},
            {"id": "dynamic-live-route", "category": "research", "title": "动态新源"},
            {"id": "custom-handoff-route", "category": "research", "title": "转交过滤源"},
        ]
        saved_handoff_routes = {"web-news", "custom-handoff-route"}
        recorded_queries: list[tuple[str, Any]] = []
        scoped_route_rows: list[dict[str, Any]] = [
            {
                "capabilities_fingerprint": fp,
                "cache_date": date(2026, 11, 22),
                "updated_at": datetime(2026, 11, 22, 12, 0, tzinfo=UTC),
                "payload": {
                    "catalog_routes": saved_catalog_routes,
                    "handoff_routes": sorted(saved_handoff_routes),
                },
            }
        ]

        class _RecordingCursor:
            def __init__(self) -> None:
                self._row: dict[str, Any] | None = None

            async def __aenter__(self) -> _RecordingCursor:
                return self

            async def __aexit__(self, *args: Any) -> bool:
                return False

            async def execute(self, sql: str, params: Any = None) -> None:
                recorded_queries.append((sql, params))
                if "dalton_data_map_scoped_cache" in sql:
                    target_fp = params[0] if params else None
                    matching = [
                        r for r in scoped_route_rows if r["capabilities_fingerprint"] == target_fp
                    ]
                    matching.sort(key=lambda r: (r["cache_date"], r["updated_at"]), reverse=True)
                    self._row = {"payload": matching[0]["payload"]} if matching else None
                else:
                    self._row = None

            async def fetchone(self) -> dict[str, Any] | None:
                return self._row

        class _FakeConn:
            def cursor(self, row_factory: Any = None) -> _RecordingCursor:
                _ = row_factory
                return _RecordingCursor()

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _fake_get_db_connection(conn: Any = None):
            _ = conn
            yield _FakeConn()

        monkeypatch.setattr(db_data_map, "get_db_connection", _fake_get_db_connection)
        probe_spy = create_autospec(
            map_health.run_all_health_checks,
            side_effect=AssertionError("Must not re-probe external sources when <24h health exists"),
        )
        monkeypatch.setattr(map_health, "run_all_health_checks", probe_spy)

        # First startup after d046 upgrade: scoped cache empty, <24h health rows exist -> backfill!
        res1 = await map_health.run_startup_health_check_once()
        assert res1 is not None
        assert res1["skipped"] == "backfilled_from_health"
        assert res1["backfilled"] is True
        assert probe_spy.await_count == 0
        assert (map_builder.today_cache_date(), fp) in stored_scoped
        assert any(
            "dalton_data_map_scoped_cache" in sql
            and "capabilities_fingerprint" in sql
            and params == (fp,)
            for sql, params in recorded_queries
        ), f"Expected fingerprint-filtered scoped cache query, got {recorded_queries}"

        # Subsequent turn reads the backfilled scoped cache (with live health status AND dynamic catalog routes)
        dm = await map_builder.get_or_build_data_map_markdown(
            x_search_enabled=True,
            team_memory_enabled=True,
        )
        assert dm == stored_scoped[(map_builder.today_cache_date(), fp)]["rendered_markdown"]
        assert "`x_search`(X社媒检索|可用)" in dm
        assert "`alphaengine`" in dm and "可用" in dm
        assert "`dynamic-live-route`" in dm
        assert "`custom-handoff-route`" not in dm
        assert "Dalton 数据地图" in dm

        # Second startup check sees today's scoped cache -> already_cached
        res2 = await map_health.run_startup_health_check_once()
        assert res2 is not None
        assert res2["skipped"] == "already_cached"
        assert probe_spy.await_count == 0

    @pytest.mark.asyncio
    async def test_get_latest_cached_route_metadata_isolates_by_capabilities_fingerprint(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from contextlib import asynccontextmanager
        from datetime import UTC, datetime

        _clean_env(monkeypatch, app_env="dalton")
        everflow_fp = capabilities_fingerprint()
        _clean_env(monkeypatch, app_env="core")
        core_fp = capabilities_fingerprint()
        assert everflow_fp != core_fp

        # Core row is NEWER than Everflow row in the same shared database
        rows = [
            {
                "capabilities_fingerprint": core_fp,
                "cache_date": date(2026, 11, 23),
                "updated_at": datetime(2026, 11, 23, 10, 0, tzinfo=UTC),
                "payload": {
                    "catalog_routes": [{"id": "core-only-route", "category": "public", "title": "公开源"}],
                    "handoff_routes": ["core-handoff"],
                },
            },
            {
                "capabilities_fingerprint": everflow_fp,
                "cache_date": date(2026, 11, 22),
                "updated_at": datetime(2026, 11, 22, 10, 0, tzinfo=UTC),
                "payload": {
                    "catalog_routes": [{"id": "everflow-route", "category": "research", "title": "卖方研报"}],
                    "handoff_routes": ["everflow-handoff"],
                },
            },
        ]
        recorded: list[tuple[str, Any]] = []

        class _FilteringCursor:
            def __init__(self) -> None:
                self._row: dict[str, Any] | None = None

            async def __aenter__(self) -> _FilteringCursor:
                return self

            async def __aexit__(self, *args: Any) -> bool:
                return False

            async def execute(self, sql: str, params: Any = None) -> None:
                recorded.append((sql, params))
                if "dalton_data_map_scoped_cache" in sql:
                    assert "capabilities_fingerprint" in sql
                    target_fp = params[0] if params else None
                    matching = [r for r in rows if r["capabilities_fingerprint"] == target_fp]
                    matching.sort(key=lambda r: (r["cache_date"], r["updated_at"]), reverse=True)
                    self._row = {"payload": matching[0]["payload"]} if matching else None
                else:
                    self._row = None

            async def fetchone(self) -> dict[str, Any] | None:
                return self._row

        class _FakeConn:
            def cursor(self, row_factory: Any = None) -> _FilteringCursor:
                _ = row_factory
                return _FilteringCursor()

        @asynccontextmanager
        async def _fake_get_db_connection(conn: Any = None):
            _ = conn
            yield _FakeConn()

        monkeypatch.setattr(db_data_map, "get_db_connection", _fake_get_db_connection)

        # In Everflow, must pick the older Everflow row and ignore the newer Core row
        _clean_env(monkeypatch, app_env="dalton")
        ef_catalog, ef_handoff = await db_data_map.get_latest_cached_route_metadata()
        assert ef_catalog == [{"id": "everflow-route", "category": "research", "title": "卖方研报"}]
        assert ef_handoff == {"everflow-handoff"}
        assert recorded[-1][1] == (everflow_fp,)

        # In Core, must pick the Core row and ignore the Everflow row
        _clean_env(monkeypatch, app_env="core")
        core_catalog, core_handoff = await db_data_map.get_latest_cached_route_metadata()
        assert core_catalog == [{"id": "core-only-route", "category": "public", "title": "公开源"}]
        assert core_handoff == {"core-handoff"}
        assert recorded[-1][1] == (core_fp,)

    @pytest.mark.asyncio
    async def test_refresh_daily_data_map_skips_archive_stats_in_core(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clean_env(monkeypatch, app_env="core")

        mock_fetch_health = create_autospec(
            db_data_map.fetch_all_source_health,
            return_value={"market_quotes": {"status": "available"}},
        )
        mock_fetch_stats = create_autospec(
            db_data_map.fetch_conversation_archive_stats,
            side_effect=AssertionError("Must not query conversation archive stats in core"),
        )
        captured_upsert: list[tuple[Any, ...]] = []

        async def _record_upsert(*args: Any, **kwargs: Any) -> None:
            captured_upsert.append((args, kwargs))

        mock_upsert = create_autospec(
            db_data_map.upsert_cached_data_map,
            side_effect=_record_upsert,
        )
        monkeypatch.setattr(db_data_map, "fetch_all_source_health", mock_fetch_health)
        monkeypatch.setattr(db_data_map, "fetch_conversation_archive_stats", mock_fetch_stats)
        monkeypatch.setattr(db_data_map, "upsert_cached_data_map", mock_upsert)

        res = await map_builder.refresh_daily_data_map(cache_date=date(2026, 11, 23))
        assert res["capabilities_fingerprint"] == capabilities_fingerprint()
        assert res["payload"]["capabilities_fingerprint"] == capabilities_fingerprint()
        assert res["payload"]["conversation_stats"] is None
        _assert_zero_blacklist_hits(res["rendered_markdown"], label="core refreshed data_map")
        assert mock_fetch_stats.await_count == 0
        assert len(captured_upsert) == 1


# ---------------------------------------------------------------------------
# 3. Prompt templates & AutomationsBackend README conditional rendering
# ---------------------------------------------------------------------------


class TestPromptsAndAutomationsConditionalRendering:
    @pytest.mark.parametrize(
        ("app_env", "dalton_profile"),
        [("core", None), ("", "core")],
    )
    @pytest.mark.parametrize("role", ["analyst", "chief_of_staff"])
    def test_core_system_prompts_contain_zero_proprietary_references(
        self,
        monkeypatch: pytest.MonkeyPatch,
        app_env: str,
        dalton_profile: str | None,
        role: str,
    ) -> None:
        _clean_env(monkeypatch, app_env=app_env, dalton_profile=dalton_profile)
        loader = prompt_loader_mod.PromptLoader()
        rendered = loader.get_system_prompt(
            role=role,
            channels_enabled=True,  # even if caller passes True, core channel_gateway=False omits Over Chat
            research_writing_enabled=True,
        )
        _assert_zero_blacklist_hits(rendered, label=f"core system prompt ({role})")
        assert rendered.startswith("You are Dalton, an investment research agent")
        assert "## Over Chat" not in rendered
        assert "market-regime" not in rendered
        assert "dalton_system_jobs" not in rendered
        assert "运维任务只读" not in rendered
        assert "conversation_search" not in rendered

        # Custom BRAND_NAME is reflected without leaking Everflow
        monkeypatch.setenv("BRAND_NAME", "Acme Research")
        rendered_custom = loader.get_system_prompt(role=role)
        assert rendered_custom.startswith("You are Dalton, Acme Research's investment research agent")
        _assert_zero_blacklist_hits(rendered_custom, label=f"core custom brand prompt ({role})")

    def test_core_subagent_and_user_profile_prompts_contain_zero_proprietary_references(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clean_env(monkeypatch, app_env="core")
        loader = prompt_loader_mod.PromptLoader()
        for sub in ("research", "general", "data_prep", "report_builder"):
            tpl_path = loader.templates_dir / "subagents" / f"{sub}.md.j2"
            if not tpl_path.is_file():
                continue
            rendered = loader.get_subagent_base_prompt(
                identity_line=f"You are {sub}.",
                role_prompt_template=f"{sub}.md.j2",
            )
            _assert_zero_blacklist_hits(rendered, label=f"core subagent prompt ({sub})")

        profile_md = loader.render(
            "components/user_profile.md.j2",
            user_profile={"name": "Analyst"},
            preferences=answer_preferences(None, steering=True),
            investment_lines=[],
            user_data_counts=None,
            main_agent_steering=True,
            channels_enabled=True,
            wakeups_enabled=True,
        )
        _assert_zero_blacklist_hits(profile_md, label="core user_profile.md.j2")
        assert "dalton_system_jobs" not in profile_md
        assert "运维任务只读" not in profile_md
        assert "## Channels" not in profile_md

    def test_working_with_the_user_partial_channel_rendering(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clean_env(monkeypatch, app_env="core")
        monkeypatch.setenv("DALTON_CAP_CHANNEL_GATEWAY", "true")
        monkeypatch.setenv("DALTON_CAP_CHANNEL_DISCORD", "true")
        loader = prompt_loader_mod.PromptLoader()
        rendered = loader.get_component("working_with_the_user", channels_enabled=True)
        assert "## Over Chat" in rendered
        assert "Discord" in rendered
        assert "Feishu" not in rendered
        assert "WeChat" not in rendered

    @pytest.mark.asyncio
    async def test_automations_readme_everflow_parity_and_core_pruning(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Everflow: byte-for-byte identical to _README_CONTENT
        _clean_env(monkeypatch, app_env="dalton")
        ef_readme = automations_backend_mod.render_automations_readme()
        assert ef_readme == automations_backend_mod._README_CONTENT

        # Core: zero proprietary channels, data_jobs/ops, or Antigravity notes
        _clean_env(monkeypatch, app_env="core")
        core_readme = automations_backend_mod.render_automations_readme()
        _assert_zero_blacklist_hits(core_readme, label="core automations README")
        assert "discord" not in core_readme.lower()
        assert "wechat" not in core_readme.lower()
        assert "dalton_system_jobs" not in core_readme
        assert "ops jobs" not in core_readme
        assert "Antigravity" not in core_readme
        assert ".agents/user/channels/available.json" not in core_readme
        assert '"delivery": []' in core_readme
        assert "dalton_schedule_automation" in core_readme
        assert "dalton_coverage_tracking" in core_readme

        # Verify AutomationsBackend._readme() returns the dynamic Core README
        backend = automations_backend_mod.AutomationsBackend.__new__(
            automations_backend_mod.AutomationsBackend
        )
        assert await backend._readme() == core_readme

        # Partial capability: channel_gateway + discord only -> mentions Discord, no Feishu/WeChat
        monkeypatch.setenv("DALTON_CAP_CHANNEL_GATEWAY", "true")
        monkeypatch.setenv("DALTON_CAP_CHANNEL_DISCORD", "true")
        discord_readme = automations_backend_mod.render_automations_readme()
        assert "Discord" in discord_readme
        assert "discord:@me" in discord_readme
        assert "feishu" not in discord_readme.lower()
        assert "weixin" not in discord_readme.lower()
        assert "wechat" not in discord_readme.lower()
