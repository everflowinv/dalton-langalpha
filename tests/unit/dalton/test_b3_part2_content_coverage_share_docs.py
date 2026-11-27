"""Unit tests for Standalone Core Batch 3 Part 2:
- Conditional content assembly (skills, workflows, JSON contracts/schemas)
- Core coverage empty default roster
- Share page brand rendering
- Docs-site single-source Core vs Everflow profile filtering
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import subprocess
from typing import Any
from unittest.mock import create_autospec

import pytest

from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
    get_prebuilt_workflows,
)
from ptc_agent.agent.middleware.skills.content import load_skill_content
from ptc_agent.agent.middleware.skills.dalton_registry import DALTON_SKILLS_DIR
from src.config.core import clear_config_cache
from src.dalton.capabilities import CapabilityId
from src.dalton.content_assembly import (
    ContentAssemblyError,
    assemble_js_text,
    assemble_json_text,
    assemble_markdown_text,
    evaluate_capability_expr,
    read_assembled_text,
)
from src.dalton.coverage import (
    DEFAULT_COVERAGE_YAML,
    get_default_coverage_doc,
    load_default_coverage_companies,
    seed_user_coverage_from_yaml,
)
from src.dalton.settings import get_dalton_config
from src.server.app.share_pages import unavailable_page

REPO_ROOT = Path(__file__).resolve().parents[3]


def _set_profile(monkeypatch: pytest.MonkeyPatch, profile: str) -> None:
    for var in ("APP_ENV", "DALTON_PROFILE", "BRAND_NAME", "DALTON_ENABLED"):
        monkeypatch.delenv(var, raising=False)
    if profile == "core":
        monkeypatch.setenv("APP_ENV", "core")
    else:
        monkeypatch.setenv("APP_ENV", "dalton")
    clear_config_cache()
    get_dalton_config.cache_clear()


class TestContentAssembly:
    def test_expression_evaluation_and_or_not(self) -> None:
        caps = frozenset({CapabilityId.ALPHAENGINE, CapabilityId.CHANNEL_FEISHU})
        assert evaluate_capability_expr("alphaengine", caps) is True
        assert evaluate_capability_expr("guidepoint", caps) is False
        assert evaluate_capability_expr("alphaengine,channel_feishu", caps) is True
        assert evaluate_capability_expr("alphaengine,guidepoint", caps) is False
        assert evaluate_capability_expr("guidepoint|alphaengine", caps) is True
        assert evaluate_capability_expr("!guidepoint", caps) is True
        assert evaluate_capability_expr("!alphaengine", caps) is False

    def test_markdown_and_js_conditional_blocks(self) -> None:
        md = (
            "Prefix <!-- DALTON_CAP:alphaengine -->AlphaEngine<!-- DALTON_CAP_ELSE -->SEC<!-- /DALTON_CAP --> suffix"
        )
        assert assemble_markdown_text(md, capabilities=frozenset({CapabilityId.ALPHAENGINE})) == "Prefix AlphaEngine suffix"
        assert assemble_markdown_text(md, capabilities=frozenset()) == "Prefix SEC suffix"

        js = "const x = /* DALTON_CAP:guidepoint */'gp'/* DALTON_CAP_ELSE */'sec'/* /DALTON_CAP */;"
        assert assemble_js_text(js, capabilities=frozenset({CapabilityId.GUIDEPOINT})) == "const x = 'gp';"
        assert assemble_js_text(js, capabilities=frozenset()) == "const x = 'sec';"

    def test_topic_research_contract_pruning_and_everflow_verbatim(self) -> None:
        contract_path = REPO_ROOT / "workflows" / "topic-research" / "contract.json"
        raw = contract_path.read_text(encoding="utf-8")

        everflow_caps = frozenset(CapabilityId) - {CapabilityId.TRANSCRIPTION}
        assert assemble_json_text(raw, path=contract_path, capabilities=everflow_caps) == raw

        core_out = assemble_json_text(raw, path=contract_path, capabilities=frozenset())
        assert "internal-memory.md" not in core_out
        assert "guidepoint.md" not in core_out
        parsed = json.loads(core_out)
        assert parsed["name"] == "topic-research"

    def test_load_skill_content_and_prebuilt_workflows_follow_active_profile(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_profile(monkeypatch, "core")
        content = load_skill_content("dalton-bdc-analyst", [str(DALTON_SKILLS_DIR)])
        assert content is not None
        assert "everflowinv.com" not in content.lower()
        assert "feishu_create_doc" not in content

        reg = get_prebuilt_workflows()
        topic = reg.get("topic-research")
        assert topic is not None
        assert "guidepoint" not in topic.lower()
        assert "researchdata" not in topic.lower()

        _set_profile(monkeypatch, "everflow")
        ef_content = load_skill_content("dalton-bdc-analyst", [str(DALTON_SKILLS_DIR)])
        assert ef_content is not None
        assert "feishu_create_doc" in ef_content

        ef_reg = get_prebuilt_workflows()
        ef_topic = ef_reg.get("topic-research")
        assert ef_topic is not None
        assert "Guidepoint" in ef_topic

    def test_langalpha_doc_settings_map_strips_disabled_channels_in_core(self) -> None:
        fp = (
            REPO_ROOT
            / "plugins"
            / "langalpha_service"
            / "skills"
            / "langalpha-doc"
            / "references"
            / "settings-map.md"
        )
        core_txt = read_assembled_text(fp, capabilities=frozenset())
        assert "DALTON_CAP" not in core_txt
        for forbidden in ("飞书", "微信", "discord", "dalton_notify_settings", "dalton_system_jobs"):
            assert forbidden not in core_txt.lower()

    def test_strict_stack_parser_handles_nesting_and_rejects_malformed_directives(self) -> None:
        # Nested blocks work for both active and inactive outer branches
        nested = (
            "A<!-- DALTON_CAP:researchdata_bridge -->"
            "B<!-- DALTON_CAP:alphaengine -->C<!-- DALTON_CAP_ELSE -->D<!-- /DALTON_CAP -->E"
            "<!-- DALTON_CAP_ELSE -->F<!-- /DALTON_CAP -->G"
        )
        assert (
            assemble_markdown_text(
                nested,
                path="sample.md",
                capabilities=frozenset({CapabilityId.RESEARCHDATA_BRIDGE, CapabilityId.ALPHAENGINE}),
            )
            == "ABCEG"
        )
        assert (
            assemble_markdown_text(
                nested,
                path="sample.md",
                capabilities=frozenset({CapabilityId.RESEARCHDATA_BRIDGE}),
            )
            == "ABDEG"
        )
        assert assemble_markdown_text(nested, path="sample.md", capabilities=frozenset()) == "AFG"

        # Unclosed block fails loudly naming the file
        with pytest.raises(ContentAssemblyError, match=r"broken\.md:1: unclosed"):
            assemble_markdown_text(
                "<!-- DALTON_CAP:alphaengine -->oops",
                path="broken.md",
                capabilities=frozenset(),
            )

        # Stray ELSE fails loudly
        with pytest.raises(ContentAssemblyError, match=r"stray\.md:1: unexpected DALTON_CAP_ELSE"):
            assemble_markdown_text(
                "<!-- DALTON_CAP_ELSE -->oops",
                path="stray.md",
                capabilities=frozenset(),
            )

        # Duplicate ELSE fails loudly
        with pytest.raises(ContentAssemblyError, match=r"dup\.md:1: duplicate DALTON_CAP_ELSE"):
            assemble_markdown_text(
                "<!-- DALTON_CAP:alphaengine -->a<!-- DALTON_CAP_ELSE -->b<!-- DALTON_CAP_ELSE -->c<!-- /DALTON_CAP -->",
                path="dup.md",
                capabilities=frozenset(),
            )

        # Stray close fails loudly
        with pytest.raises(ContentAssemblyError, match=r"close\.md:1: unexpected closing /DALTON_CAP"):
            assemble_markdown_text(
                "<!-- /DALTON_CAP -->",
                path="close.md",
                capabilities=frozenset(),
            )

        # Unknown capability name fails loudly with file and unknown name
        with pytest.raises(ContentAssemblyError, match=r"badcap\.md:1:.*nonexistent_cap"):
            assemble_markdown_text(
                "<!-- DALTON_CAP:nonexistent_cap -->x<!-- /DALTON_CAP -->",
                path="badcap.md",
                capabilities=frozenset(),
            )

        # Uppercase / malformed directive fails loudly instead of passing through verbatim
        with pytest.raises(ContentAssemblyError, match=r"badcase\.md:1: malformed"):
            assemble_markdown_text(
                "<!-- dalton_cap:alphaengine -->x<!-- /DALTON_CAP -->",
                path="badcase.md",
                capabilities=frozenset(),
            )

    def test_disk_folder_browse_and_read_apply_assembly_without_raw_markers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.server.services.user_skills.browse import DiskFolder

        for profile in ("everflow", "core"):
            _set_profile(monkeypatch, profile)
            folder = DiskFolder(DALTON_SKILLS_DIR / "dalton-bdc-analyst", assemble=True)
            files = dict(folder.files)
            raw_bytes = folder.read("SKILL.md")
            assert raw_bytes is not None
            text = raw_bytes.decode("utf-8")
            assert "DALTON_CAP" not in text
            assert files["SKILL.md"] == len(raw_bytes)
            if profile == "core":
                assert "feishu_create_doc" not in text
                assert "everflowinv.com" not in text.lower()
            else:
                assert "feishu_create_doc" in text

    def test_dalton_evidence_subagent_and_live_skill_registry_clean_in_core(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ptc_agent.agent.middleware.skills import registry as skill_reg_mod
        from ptc_agent.agent.subagents.compiler import SubagentCompiler
        from ptc_agent.agent.subagents.merge import BaseSubagent, plugin_definition
        from src.dalton.publish.review import load_agent_body
        from src.server.services.plugins.bundled import bundled_agent_plans
        from src.server.services.subagents.view import render_base_body, value_views

        plans = [plan for _b, plan in bundled_agent_plans() if plan.name == "dalton-evidence"]
        assert len(plans) == 1
        ev_plan = plans[0]
        baseline_ev_body = (
            REPO_ROOT / "tests" / "unit" / "dalton" / "snapshots" / "everflow" / "dalton_evidence_body.md"
        ).read_text(encoding="utf-8").strip()

        # Everflow via real plugin loader -> plugin_definition(bundled=True) -> SubagentCompiler & settings view
        _set_profile(monkeypatch, "everflow")
        ef_defn = plugin_definition(ev_plan.row, bundled=True)
        ef_compiled = SubagentCompiler().compile(ef_defn)
        ef_view_body = render_base_body(ef_defn)
        ef_views = value_views(
            {"dalton-evidence": BaseSubagent("plugin:dalton/dalton-evidence", ef_defn)},
            ("dalton-evidence",),
            [],
        )
        assert ef_view_body == baseline_ev_body
        assert ef_views[0]["instructions"] == baseline_ev_body
        assert baseline_ev_body in ef_compiled["system_prompt"]
        assert "DALTON_CAP" not in ef_compiled["system_prompt"]
        assert "researchdata" in ef_defn.preferred_connectors

        # Core via real plugin loader -> plugin_definition(bundled=True) -> SubagentCompiler & settings view
        _set_profile(monkeypatch, "core")
        core_defn = plugin_definition(ev_plan.row, bundled=True)
        core_compiled = SubagentCompiler().compile(core_defn)
        core_view_body = render_base_body(core_defn)
        core_views = value_views(
            {"dalton-evidence": BaseSubagent("plugin:dalton/dalton-evidence", core_defn)},
            ("dalton-evidence",),
            [],
        )
        ev_body = load_agent_body("dalton-evidence")
        assert ev_body is not None
        assert "researchdata" not in core_defn.preferred_connectors
        for surface_label, surface_text in (
            ("load_agent_body", ev_body),
            ("render_base_body", core_view_body),
            ("value_views.instructions", core_views[0]["instructions"]),
            ("value_views.description", core_views[0]["description"]),
            ("SubagentCompiler.system_prompt", core_compiled["system_prompt"]),
            ("SubagentCompiler.description", core_compiled["description"]),
        ):
            assert "DALTON_CAP" not in surface_text, f"{surface_label} leaked DALTON_CAP"
            for forbidden in (
                "alphaengine",
                "guidepoint",
                "researchdata",
                "sales-notes",
                "sales notes",
                "tmtb",
                "基金 wiki",
                "销售纪要",
            ):
                assert forbidden not in surface_text.lower(), (
                    f"{surface_label} leaked {forbidden!r}"
                )
            assert not re.search(r"\bfunda\b", surface_text, flags=re.IGNORECASE), (
                f"{surface_label} leaked 'FUNDA'"
            )

        # When company_wiki is disabled in Core, "公司 wiki" must also be absent
        monkeypatch.setenv("DALTON_CAP_COMPANY_WIKI", "false")
        no_wiki_defn = plugin_definition(ev_plan.row, bundled=True)
        no_wiki_compiled = SubagentCompiler().compile(no_wiki_defn)
        no_wiki_view_body = render_base_body(no_wiki_defn)
        no_wiki_views = value_views(
            {"dalton-evidence": BaseSubagent("plugin:dalton/dalton-evidence", no_wiki_defn)},
            ("dalton-evidence",),
            [],
        )
        no_wiki_body = load_agent_body("dalton-evidence")
        assert no_wiki_body is not None
        for surface_label, surface_text in (
            ("load_agent_body(no-wiki)", no_wiki_body),
            ("render_base_body(no-wiki)", no_wiki_view_body),
            ("value_views.instructions(no-wiki)", no_wiki_views[0]["instructions"]),
            ("value_views.description(no-wiki)", no_wiki_views[0]["description"]),
            ("SubagentCompiler.system_prompt(no-wiki)", no_wiki_compiled["system_prompt"]),
            ("SubagentCompiler.description(no-wiki)", no_wiki_compiled["description"]),
        ):
            assert "公司 wiki" not in surface_text.lower(), (
                f"{surface_label} leaked '公司 wiki' while DALTON_CAP_COMPANY_WIKI=false"
            )
        monkeypatch.delenv("DALTON_CAP_COMPANY_WIKI", raising=False)

        # Core channel-routing.md has zero DALTON-HOOK developer comments
        routing_fp = (
            REPO_ROOT
            / "plugins"
            / "langalpha_service"
            / "skills"
            / "langalpha-doc"
            / "facts"
            / "channel-routing.md"
        )
        core_routing = read_assembled_text(routing_fp, capabilities=frozenset())
        assert "DALTON-HOOK" not in core_routing
        assert "feishu" not in core_routing.lower()

        # Discord-only langalpha-doc/SKILL.md omits Feishu and WeChat
        doc_skill_fp = (
            REPO_ROOT / "plugins" / "langalpha_service" / "skills" / "langalpha-doc" / "SKILL.md"
        )
        discord_only_doc = read_assembled_text(
            doc_skill_fp,
            capabilities=frozenset({CapabilityId.CHANNEL_GATEWAY, CapabilityId.CHANNEL_DISCORD}),
        )
        assert "Discord" in discord_only_doc
        assert "Feishu" not in discord_only_doc
        assert "WeChat" not in discord_only_doc

        # Discord-only and Core references/chat.md omit DALTON-HOOK developer comments
        chat_ref_fp = (
            REPO_ROOT
            / "plugins"
            / "langalpha_service"
            / "skills"
            / "langalpha-doc"
            / "references"
            / "chat.md"
        )
        for caps in (
            frozenset({CapabilityId.CHANNEL_GATEWAY, CapabilityId.CHANNEL_DISCORD}),
            frozenset({CapabilityId.CHANNEL_DISCORD}),
            frozenset(),
        ):
            assembled_chat = read_assembled_text(chat_ref_fp, capabilities=caps)
            assert "DALTON-HOOK" not in assembled_chat
            assert "DALTON_CAP" not in assembled_chat

        td = skill_reg_mod.get_skill("dalton-thesis-delta")
        sd = skill_reg_mod.get_skill("dalton-sector-delta")
        assert td is not None and "内部研究库" not in td.description
        assert sd is not None and "内部研究库" not in sd.description
        for item in skill_reg_mod.list_skills():
            assert "内部研究库" not in item["description"]

    @pytest.mark.asyncio
    async def test_core_thesis_and_sector_delta_skip_collect_and_enforce_material_closing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from datetime import UTC, datetime

        from src.dalton.tools import check_done_tools as check_done

        _set_profile(monkeypatch, "core")
        for skill_name, script_name in (
            ("dalton-thesis-delta", "collect.py"),
            ("dalton-sector-delta", "collect_sector.py"),
        ):
            md = load_skill_content(skill_name, [str(DALTON_SKILLS_DIR)])
            assert md is not None
            assert f"scripts/{script_name}" not in md
            assert f"{script_name} collect" not in md
            assert f"{script_name} commit" not in md

            contract_path = DALTON_SKILLS_DIR / skill_name / "contract.json"
            assembled = read_assembled_text(contract_path, capabilities=frozenset())
            assert "last_run.json" not in assembled
            assert "updates.log" not in assembled
            parsed = json.loads(assembled)
            assert parsed["stages"]
            assert "knowledge_docs_if_json" not in parsed["closing"]
            assert "material_verdict_requires" in parsed["closing"]

        # Exercise real _check_closing on Core dalton-thesis-delta contract
        td_contract = json.loads(
            read_assembled_text(
                DALTON_SKILLS_DIR / "dalton-thesis-delta" / "contract.json",
                capabilities=frozenset(),
            )
        )
        since_dt = datetime(2026, 11, 23, 0, 0, 0, tzinfo=UTC)
        files_map: dict[str, tuple[str, int]] = {}

        async def _fake_read_diagnosed(_backend: Any, rel_path: str) -> tuple[str, str | None]:
            if rel_path in files_map:
                return "ok", files_map[rel_path][0]
            return "missing", None

        async def _fake_file_info(_backend: Any, rel_path: str) -> check_done._FileInfo:
            if rel_path in files_map:
                text, mtime = files_map[rel_path]
                return check_done._FileInfo(True, len(text.encode("utf-8")), text, mtime)
            return check_done._FileInfo(False, 0, None, None)

        monkeypatch.setattr(
            check_done,
            "_read_text_diagnosed",
            create_autospec(check_done._read_text_diagnosed, side_effect=_fake_read_diagnosed),
        )
        monkeypatch.setattr(
            check_done,
            "_file_info",
            create_autospec(check_done._file_info, side_effect=_fake_file_info),
        )

        # 1) NO_CHANGE + progress=none (real DB row schema: top-level `word`) -> done=True
        verdicts_mock = create_autospec(
            check_done._check_verdicts,
            return_value=[
                {
                    "verdict_id": "VD-20261123-01",
                    "word": "NO_CHANGE",
                    "progress": "none",
                    "payload": {"payoff_assessment": {"changed": False, "reason": "none"}},
                }
            ],
        )
        knowledge_mock = create_autospec(
            check_done._check_knowledge_versions,
            return_value={"thesis": False, "debate": False, "drivers": False, "management": False},
        )
        monkeypatch.setattr(check_done, "_check_verdicts", verdicts_mock)
        monkeypatch.setattr(check_done, "_check_knowledge_versions", knowledge_mock)
        miss_no_change, _warn, _lbl = await check_done._check_closing(
            td_contract,
            {"ticker": "AAPL", "as_of": "2026-11-23"},
            backend=None,
            workspace_id="ws-1",
            user_id="u-1",
            since=since_dt,
        )
        assert miss_no_change == []

        # 2) Material verdict (word="THESIS_STRENGTHENED", progress="none") WITHOUT delta note & WITHOUT knowledge doc -> FAILS!
        verdicts_mock.return_value = [
            {
                "verdict_id": "VD-20261123-02",
                "word": "THESIS_STRENGTHENED",
                "progress": "none",
                "payload": {"payoff_assessment": {"changed": False, "reason": "none"}},
            }
        ]
        miss_material_progress_none, _warn, _lbl = await check_done._check_closing(
            td_contract,
            {"ticker": "AAPL", "as_of": "2026-11-23"},
            backend=None,
            workspace_id="ws-1",
            user_id="u-1",
            since=since_dt,
        )
        assert len(miss_material_progress_none) >= 2
        assert any("delta.md" in m for m in miss_material_progress_none)
        assert any("知识层" in m for m in miss_material_progress_none)

        # 3) Material verdict (word="THESIS_STRENGTHENED", progress="some") WITHOUT delta note & WITHOUT knowledge doc -> FAILS!
        verdicts_mock.return_value = [
            {
                "verdict_id": "VD-20261123-03",
                "word": "THESIS_STRENGTHENED",
                "progress": "some",
                "payload": {"payoff_assessment": {"changed": True, "reason": "scenario_revised"}},
            }
        ]
        miss_material_empty, _warn, _lbl = await check_done._check_closing(
            td_contract,
            {"ticker": "AAPL", "as_of": "2026-11-23"},
            backend=None,
            workspace_id="ws-1",
            user_id="u-1",
            since=since_dt,
        )
        assert len(miss_material_empty) >= 2
        assert any("delta.md" in m for m in miss_material_empty)
        assert any("知识层" in m for m in miss_material_empty)

        # 4) Material verdict WITH fresh delta note + updated thesis doc -> PASSES!
        files_map["research/2026-11-23-delta.md"] = ("# Delta\n" + "内容" * 80, int(since_dt.timestamp()) + 60)
        knowledge_mock.return_value = {
            "thesis": True,
            "debate": False,
            "drivers": False,
            "management": False,
        }
        miss_material_ok, _warn, _lbl = await check_done._check_closing(
            td_contract,
            {"ticker": "AAPL", "as_of": "2026-11-23"},
            backend=None,
            workspace_id="ws-1",
            user_id="u-1",
            since=since_dt,
        )
        assert miss_material_ok == []

    @pytest.mark.asyncio
    async def test_data_job_monitors_return_404_when_data_jobs_capability_disabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from datetime import UTC, datetime
        from fastapi import HTTPException

        from src.dalton.monitors import db as monitor_db
        from src.dalton.monitors import permissions as monitor_perms
        from src.dalton.monitors import service as monitor_svc
        from src.dalton.monitors.models import MonitorRecord

        _set_profile(monkeypatch, "core")
        now = datetime.now(UTC)
        job_monitor = MonitorRecord(
            monitor_id="mon-job-1",
            slug="daily-job-panel",
            artifact_id="art-1",
            artifact_code="code-1",
            workspace_id="ws-1",
            entry_path="daily-job-panel/index.html",
            owner_user_id="system",
            owner_name="System",
            owner_email=None,
            title="Daily Job Panel",
            category="macro",
            sort_order=0,
            visibility="public",
            member_user_ids=(),
            published_seq=1,
            keep_updated=True,
            source_kind="data_job",
            source_ref="market_digest",
            acl_revision=1,
            last_publish_error=None,
            last_publish_failed_at=None,
            data_as_of=None,
            created_at=now,
            updated_at=now,
        )
        ws_monitor = MonitorRecord(**{**job_monitor.__dict__, "monitor_id": "mon-ws-1", "slug": "ws-panel", "source_kind": "workspace", "source_ref": "ws-1:index.html"})

        assert await monitor_perms.can_view_monitor(job_monitor, "u1") is False
        assert await monitor_perms.can_view_monitor(ws_monitor, "u1") is True

        monkeypatch.setattr(
            monitor_db,
            "get_monitor_by_slug",
            create_autospec(monitor_db.get_monitor_by_slug, return_value=job_monitor),
        )
        for coro in (
            monitor_svc.get_monitor_detail("daily-job-panel", viewer_id="u1"),
            monitor_svc.get_public_monitor_detail("daily-job-panel"),
            monitor_svc.mint_grant_for_monitor("daily-job-panel", viewer_id="u1"),
            monitor_svc.serve_public_monitor_file("daily-job-panel", "1", "index.html"),
            monitor_svc.read_public_monitor_data("daily-job-panel", "latest"),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await coro
            assert exc_info.value.status_code == 404

    def test_sandbox_does_not_ship_default_coverage_yaml(self) -> None:
        dockerfile = (REPO_ROOT / "Dockerfile.sandbox").read_text(encoding="utf-8")
        assert "coverage.yaml" not in dockerfile
        for py in (REPO_ROOT / "src" / "ptc_agent").rglob("*.py"):
            assert "config/dalton/coverage.yaml" not in py.read_text(encoding="utf-8", errors="ignore")


class TestCoreCoverageIsolation:
    def test_core_profile_returns_empty_default_coverage_without_reading_yaml(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_profile(monkeypatch, "core")
        assert DEFAULT_COVERAGE_YAML.exists()
        assert load_default_coverage_companies() == []
        assert get_default_coverage_doc().get("companies") == []

        # seed_user_coverage_from_yaml returns an empty plan immediately in Core without DB access
        res = asyncio.run(seed_user_coverage_from_yaml("user-core-1"))
        assert res["ok"] is True
        assert res["planned_count"] == 0

    def test_everflow_profile_reads_default_coverage_yaml(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_profile(monkeypatch, "everflow")
        companies = load_default_coverage_companies()
        assert isinstance(companies, list)
        assert len(companies) > 0
        doc = get_default_coverage_doc()
        assert len(doc["companies"]) == len(companies)


class TestSharePagesBranding:
    def test_share_unavailable_page_uses_active_brand_name(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_profile(monkeypatch, "everflow")
        ef_html = unavailable_page(404, chinese=True)
        assert "Everflow" in ef_html

        _set_profile(monkeypatch, "core")
        core_html = unavailable_page(404, chinese=True)
        assert "everflow" not in core_html.lower()
        assert "Dalton" in core_html

        monkeypatch.setenv("BRAND_NAME", "Acme & Co")
        clear_config_cache()
        get_dalton_config.cache_clear()
        custom_html = unavailable_page(404, chinese=True)
        assert "Acme &amp; Co" in custom_html
        assert "everflow" not in custom_html.lower()


class TestDocsSiteProfileFilter:
    def test_docs_site_core_build_excludes_everflow_and_strips_proprietary_blocks(self) -> None:
        import yaml

        script = r"""
import fs from 'node:fs';
import path from 'node:path';
import {
  CORE_OVERVIEW_SLUG,
  EVERFLOW_OVERVIEW_SLUG,
  isDocIncluded,
  mapDocSlug,
  resolveDocsProfile,
  transformDocSource,
} from './docs-site/src/integrations/profile-filter.mjs';

// 1) Single-source profile resolution + conflict rejection
const fromAppEnv = resolveDocsProfile({ APP_ENV: 'core' });
if (!fromAppEnv.isCore || fromAppEnv.brandName !== 'Dalton') {
  throw new Error(`Expected core profile from APP_ENV=core, got ${JSON.stringify(fromAppEnv)}`);
}
const fromDocsOnly = resolveDocsProfile({ DOCS_PROFILE: 'core' });
if (!fromDocsOnly.isCore || fromDocsOnly.brandName !== 'Dalton') {
  throw new Error(`Expected core profile from DOCS_PROFILE=core, got ${JSON.stringify(fromDocsOnly)}`);
}
const fromViteOnly = resolveDocsProfile({ VITE_DALTON_PROFILE: 'core' });
if (!fromViteOnly.isCore || fromViteOnly.brandName !== 'Dalton') {
  throw new Error(`Expected core profile from VITE_DALTON_PROFILE=core, got ${JSON.stringify(fromViteOnly)}`);
}
const fromDocsAndViteMatch = resolveDocsProfile({ DOCS_PROFILE: 'core', VITE_DALTON_PROFILE: 'core' });
if (!fromDocsAndViteMatch.isCore) {
  throw new Error(`Expected core profile when DOCS_PROFILE and VITE_DALTON_PROFILE agree`);
}
const fromCustom = resolveDocsProfile({ DALTON_PROFILE: 'core', BRAND_NAME: 'Acme: Research #1' });
if (!fromCustom.isCore || fromCustom.brandName !== 'Acme: Research #1') {
  throw new Error(`Expected custom brand, got ${JSON.stringify(fromCustom)}`);
}
for (const [label, conflictEnv] of [
  ['dalton-vs-vite-core', { APP_ENV: 'dalton', VITE_DALTON_PROFILE: 'core' }],
  ['core-vs-vite-everflow', { APP_ENV: 'core', VITE_DALTON_PROFILE: 'everflow' }],
  ['dalton-profile-vs-app-env', { DALTON_PROFILE: 'core', APP_ENV: 'dalton' }],
  ['docs-core-vs-vite-everflow', { DOCS_PROFILE: 'core', VITE_DALTON_PROFILE: 'everflow' }],
  ['docs-everflow-vs-vite-core', { DOCS_PROFILE: 'everflow', VITE_DALTON_PROFILE: 'core' }],
  ['dalton-core-vs-docs-everflow', { DALTON_PROFILE: 'core', DOCS_PROFILE: 'everflow' }],
]) {
  let threw = false;
  try {
    resolveDocsProfile(conflictEnv);
  } catch {
    threw = true;
  }
  if (!threw) throw new Error(`Expected conflict error for ${label}`);
}
if (mapDocSlug(EVERFLOW_OVERVIEW_SLUG, { isCore: true }) !== CORE_OVERVIEW_SLUG) {
  throw new Error('Core overview slug mapping failed');
}
if (mapDocSlug(EVERFLOW_OVERVIEW_SLUG, { isCore: false }) !== EVERFLOW_OVERVIEW_SLUG) {
  throw new Error('Everflow overview slug must remain unchanged');
}

// 1b) Brand replacement uses function form so `$&` is never expanded
const dollarBrand = transformDocSource('---\ntitle: Everflow 是什么\n---\nbody\n', {
  isCore: true,
  brandName: 'A$&B',
  filePath: 'brand.mdx',
});
if (!dollarBrand.includes('"A$&B 是什么"')) {
  throw new Error(`Brand replacement expanded $&: ${dollarBrand}`);
}

// 2) Strict stack-based parser checks (nesting + malformed + unclosed delimiter rejection)
const nested = '{/* DALTON_DOC:everflow */}A{/* DALTON_DOC:core */}B{/* /DALTON_DOC */}C{/* DALTON_DOC_ELSE */}D{/* /DALTON_DOC */}';
const nestedCore = transformDocSource(nested, { isCore: true, brandName: 'Dalton', filePath: 'nested.mdx' });
if (nestedCore !== 'D') throw new Error(`Nested core mismatch: ${nestedCore}`);
const nestedEf = transformDocSource(nested, { isCore: false, brandName: 'Everflow', filePath: 'nested.mdx' });
if (nestedEf !== 'AC') throw new Error(`Nested everflow mismatch: ${nestedEf}`);

for (const [label, badSrc, expectRe] of [
  ['unclosed', '{/* DALTON_DOC:everflow */}oops', /bad\.mdx:1: unclosed/],
  ['stray-else', '{/* DALTON_DOC_ELSE */}oops', /bad\.mdx:1: unexpected DALTON_DOC_ELSE/],
  ['stray-close', '{/* /DALTON_DOC */}', /bad\.mdx:1: unexpected \/DALTON_DOC/],
  ['unknown-target', '{/* DALTON_DOC:staging */}x{/* /DALTON_DOC */}', /bad\.mdx:1: unknown DALTON_DOC target/],
  ['hyphen-typo', '<!-- DALTON_DOC:core-typo -->x<!-- /DALTON_DOC -->', /bad\.mdx:1: malformed/],
  ['malformed-case', '{/* dalton_doc:everflow */}x{/* /DALTON_DOC */}', /bad\.mdx:1: malformed/],
  ['unclosed-html-delim', '<!-- DALTON_DOC:everflow oops', /bad\.mdx:1: unclosed or malformed/],
  ['unclosed-jsx-delim', '{/* DALTON_DOC:core oops', /bad\.mdx:1: unclosed or malformed/],
]) {
  let threw = false;
  try {
    transformDocSource(badSrc, { isCore: true, brandName: 'Dalton', filePath: 'bad.mdx' });
  } catch (err) {
    threw = true;
    if (!expectRe.test(String(err.message || err))) {
      throw new Error(`Case ${label} threw unexpected message: ${err}`);
    }
  }
  if (!threw) throw new Error(`Case ${label} did not throw`);
}

// 3) Full corpus Core scan with expanded blacklist and special BRAND_NAME YAML check
const docsRoot = path.resolve('docs-site/src/content/docs');
const forbidden = /alphaengine|guidepoint|researchdata|sales-notes|tmtb|funda|feishu|飞书|weixin|微信|everflowinv\.com|\/help\/everflow\/|what-is-everflow|公司\s*wiki|销售纪要|自动入库|团队对话归档|\/transcribe/i;
// Per deployment-credentials §4.2, only get-started/admin-integrations.mdx permits neutral
// Feishu personal-connector credential docs while still blocking proprietary bot/wiki terms.
const forbiddenAdminIntegrations = /alphaengine|guidepoint|researchdata|sales-notes|tmtb|funda|飞书文档|知识库|群聊绑定|机器人|weixin|微信|everflowinv\.com|\/help\/everflow\/|what-is-everflow|公司\s*wiki|销售纪要|自动入库|团队对话归档|\/transcribe/i;

function walk(dir) {
  const out = [];
  for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, ent.name);
    if (ent.isDirectory()) out.push(...walk(full));
    else if (/\.(md|mdx)$/i.test(ent.name) && !ent.name.startsWith('_')) out.push(full);
  }
  return out.sort();
}

let included = 0;
let excluded = 0;
const failures = [];
const frontmatters = [];
for (const fp of walk(docsRoot)) {
  const rel = path.relative(docsRoot, fp).replace(/\\/g, '/');
  if (!isDocIncluded(rel, { isCore: true })) {
    excluded += 1;
    continue;
  }
  included += 1;
  const raw = fs.readFileSync(fp, 'utf-8');
  const coreText = transformDocSource(raw, { isCore: true, brandName: 'Dalton', filePath: rel });
  if (/dalton_doc/i.test(coreText)) failures.push(`${rel}: leftover DALTON_DOC marker`);
  const activeForbidden = rel === 'get-started/admin-integrations.mdx' ? forbiddenAdminIntegrations : forbidden;
  if (activeForbidden.test(coreText)) failures.push(`${rel}: matched ${coreText.match(activeForbidden)?.[0]}`);
  if (/\beverflow\b/i.test(coreText)) failures.push(`${rel}: matched Everflow`);

  const specialBrandText = transformDocSource(raw, {
    isCore: true,
    brandName: 'Acme: Research #1',
    filePath: rel,
  });
  const fmMatch = specialBrandText.match(/^---\n([\s\S]*?)\n---\n/);
  if (fmMatch) frontmatters.push({ rel, yaml: fmMatch[1] });
}
if (excluded < 5) failures.push(`expected >=5 excluded docs, got ${excluded}`);
if (failures.length) {
  console.error(failures.join('\n'));
  process.exit(1);
}
console.log(JSON.stringify({ included, excluded, frontmatters }));
"""
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, f"docs-site core filter failed:\n{proc.stderr}\n{proc.stdout}"
        summary = json.loads(proc.stdout.strip())
        assert summary["included"] > 15
        assert summary["excluded"] >= 5
        # Every frontmatter rewritten with `Acme: Research #1` parses as valid YAML
        for item in summary["frontmatters"]:
            parsed = yaml.safe_load(item["yaml"])
            assert isinstance(parsed, dict), f"Invalid YAML frontmatter in {item['rel']}"

        # Core neutral favicon exists and contains zero Everflow branding
        core_favicon = (REPO_ROOT / "docs-site" / "public" / "favicon-core.svg").read_text(
            encoding="utf-8"
        )
        assert "everflow" not in core_favicon.lower()

    def test_admin_integrations_doc_coverage_core_vs_everflow_and_frontend_help_links(
        self,
    ) -> None:
        """Verify admin-integrations.mdx content in Core/Everflow and frontend /help link targets (§4.2, §5)."""
        script = r"""
import fs from 'node:fs';
import path from 'node:path';
import { isDocIncluded, transformDocSource } from './docs-site/src/integrations/profile-filter.mjs';

const adminDocPath = path.resolve('docs-site/src/content/docs/get-started/admin-integrations.mdx');
const raw = fs.readFileSync(adminDocPath, 'utf-8');
const coreOut = transformDocSource(raw, { isCore: true, brandName: 'Dalton', filePath: 'get-started/admin-integrations.mdx' });
const efOut = transformDocSource(raw, { isCore: false, brandName: 'Everflow', filePath: 'get-started/admin-integrations.mdx' });

// Both profiles include Google callback prerequisites & Feishu event verification keys
for (const [label, text] of [['core', coreOut], ['everflow', efOut]]) {
  for (const required of [
    'DALTON_GOOGLE_OAUTH_CLIENT_ID',
    'DALTON_GOOGLE_OAUTH_CLIENT_SECRET',
    'DALTON_FEISHU_APP_ID',
    'DALTON_FEISHU_APP_SECRET',
    'DALTON_FEISHU_ENCRYPT_KEY',
    'DALTON_FEISHU_VERIFICATION_TOKEN',
    'Public Suffix',
    'http://localhost',
    'DALTON_AUTH_MODE=local',
  ]) {
    if (!text.includes(required)) {
      throw new Error(`${label} admin-integrations.mdx missing required content: ${required}`);
    }
  }
}

// Everflow conditional block appears only in Everflow output, never in Core output
if (!efOut.includes('现网飞书机器人使用中（只读 · 第 3 批迁移后可在此修改）')) {
  throw new Error('Everflow admin-integrations.mdx missing Batch 1 Vault read-only guidance');
}
if (coreOut.includes('现网飞书机器人') || /\beverflow\b/i.test(coreOut)) {
  throw new Error('Core admin-integrations.mdx leaked Everflow-only block');
}

// Verify frontend /help links added for deployment credentials resolve to existing docs-site pages
const credsSrc = fs.readFileSync(path.resolve('web/src/lib/dalton/deploymentCredentials.ts'), 'utf-8');
const matches = [...credsSrc.matchAll(/['"`](\/help\/[^'"`\s)]*)['"`]/g)].map((m) => m[1]);
if (!matches.includes('/help/get-started/admin-integrations/')) {
  throw new Error(`Expected /help/get-started/admin-integrations/ in deploymentCredentials.ts, got ${JSON.stringify(matches)}`);
}
for (const url of matches) {
  const relSlug = url.replace(/^\/help\/?/, '').replace(/\/+$/, '');
  const mdxPath = path.resolve('docs-site/src/content/docs', `${relSlug}.mdx`);
  if (!fs.existsSync(mdxPath)) {
    throw new Error(`Missing docs-site source page for ${url}: ${mdxPath}`);
  }
  if (!isDocIncluded(`${relSlug}.mdx`, { isCore: true })) {
    throw new Error(`Core build unexpectedly excluded ${relSlug}.mdx`);
  }
}
console.log(JSON.stringify({ ok: true, links: matches }));
"""
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, f"admin-integrations check failed:\n{proc.stderr}\n{proc.stdout}"
        assert json.loads(proc.stdout.strip())["ok"] is True

    def test_everflow_docs_site_output_matches_pre_change_sha256_verbatim(self) -> None:
        baseline_path = (
            REPO_ROOT / "tests" / "unit" / "dalton" / "snapshots" / "everflow" / "docs_site_everflow_sha256.json"
        )
        expected_hashes: dict[str, str] = json.loads(baseline_path.read_text(encoding="utf-8"))
        script = r"""
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { transformDocSource } from './docs-site/src/integrations/profile-filter.mjs';

const docsRoot = path.resolve('docs-site/src/content/docs');
function walk(dir) {
  const out = [];
  for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, ent.name);
    if (ent.isDirectory()) out.push(...walk(full));
    else if (/\.(md|mdx)$/i.test(ent.name) && !ent.name.startsWith('_')) out.push(full);
  }
  return out.sort();
}
const actual = {};
for (const fp of walk(docsRoot)) {
  const rel = path.relative(docsRoot, fp).replace(/\\/g, '/');
  const raw = fs.readFileSync(fp, 'utf-8');
  const ef = transformDocSource(raw, { isCore: false, brandName: 'Everflow', filePath: rel });
  actual[rel] = crypto.createHash('sha256').update(ef, 'utf-8').digest('hex');
}
console.log(JSON.stringify(actual));
"""
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, f"Everflow docs parity script failed:\n{proc.stderr}"
        actual_hashes = json.loads(proc.stdout.strip())
        assert actual_hashes == expected_hashes

    def test_build_frontend_prod_profile_conflict_fails_loudly(self, tmp_path: Path) -> None:
        # Execute only the profile-resolution prefix of build_frontend_prod.sh via a tiny temp env file
        script_text = (REPO_ROOT / "scripts" / "dalton" / "build_frontend_prod.sh").read_text(
            encoding="utf-8"
        )
        cut_marker = "# 3. Validate auth env consistency"
        assert cut_marker in script_text
        prefix_script = script_text.split(cut_marker, 1)[0] + '\necho "PROFILE=${DALTON_PROFILE}|VITE=${VITE_DALTON_PROFILE}|BRAND=${VITE_BRAND_NAME}"\n'
        harness = tmp_path / "check_profile.sh"
        harness.write_text(prefix_script, encoding="utf-8")

        for label, env_body in (
            ("dalton_vs_vite_core", "APP_ENV=dalton\nVITE_DALTON_PROFILE=core\n"),
            ("core_vs_vite_everflow", "APP_ENV=core\nVITE_DALTON_PROFILE=everflow\n"),
            ("dalton_profile_vs_app_env", "DALTON_PROFILE=core\nAPP_ENV=dalton\n"),
        ):
            env_file = tmp_path / f"{label}.env"
            env_file.write_text(env_body, encoding="utf-8")
            proc = subprocess.run(
                ["bash", str(harness)],
                env={
                    "PATH": "/usr/bin:/bin",
                    "DALTON_ENV_FILE": str(env_file),
                },
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            assert proc.returncode != 0, f"Expected conflict failure for {label}, stdout={proc.stdout}"
            assert "ERROR:" in proc.stderr

        # Consistent APP_ENV=core resolves both DALTON_PROFILE and VITE_DALTON_PROFILE to core
        ok_env = tmp_path / "core_ok.env"
        ok_env.write_text("APP_ENV=core\n", encoding="utf-8")
        proc_ok = subprocess.run(
            ["bash", str(harness)],
            env={"PATH": "/usr/bin:/bin", "DALTON_ENV_FILE": str(ok_env)},
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert proc_ok.returncode == 0, f"APP_ENV=core failed: {proc_ok.stderr}"
        assert "PROFILE=core|VITE=core|BRAND=Dalton" in proc_ok.stdout

        # Leading/trailing whitespace around DALTON_PROFILE / APP_ENV is trimmed (matches profile.py:53)
        empty_env = tmp_path / "empty.env"
        empty_env.write_text("", encoding="utf-8")
        proc_ws = subprocess.run(
            ["bash", str(harness)],
            env={
                "PATH": "/usr/bin:/bin",
                "DALTON_ENV_FILE": str(empty_env),
                "APP_ENV": " dalton ",
                "DALTON_PROFILE": " everflow ",
            },
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert proc_ws.returncode == 0, f"Whitespace-padded profile failed: {proc_ws.stderr}"
        assert "PROFILE=everflow|VITE=everflow|" in proc_ws.stdout

    def test_create_autospec_smoke_for_coverage_seed_skip_in_core(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.dalton.coverage import service as cov_svc

        _set_profile(monkeypatch, "core")
        load_spy = create_autospec(cov_svc.load_default_coverage_companies, return_value=[])
        monkeypatch.setattr(cov_svc, "load_default_coverage_companies", load_spy)
        res = asyncio.run(cov_svc.seed_user_coverage_from_yaml("user-core-2"))
        assert res["ok"] is True
        assert res["planned_count"] == 0
        assert load_spy.call_count == 0


class TestB3StandaloneCheckScript:
    def test_b3_check_script_uses_stdlib_only_and_container_program_executes_cleanly(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import ast
        import importlib.util
        import shutil
        import sys

        check_path = REPO_ROOT / "tests" / "standalone" / "checks" / "b3_skills_prompts_ui.py"
        src_text = check_path.read_text(encoding="utf-8")
        tree = ast.parse(src_text)
        forbidden_roots = {"src", "ptc_agent", "unittest", "httpx", "psycopg"}
        bad_imports = [
            node.names[0].name if isinstance(node, ast.Import) else node.module
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            and (
                (node.module if isinstance(node, ast.ImportFrom) else node.names[0].name) or ""
            ).split(".")[0]
            in forbidden_roots
        ]
        assert bad_imports == [], f"Host check script imported forbidden modules: {bad_imports}"

        spec = importlib.util.spec_from_file_location("b3_skills_prompts_ui_under_test", check_path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Container program compiles cleanly, uses no unittest, and includes all required checks
        container_src: str = mod.BACKEND_CONTAINER_CHECK_SCRIPT
        compile(container_src, "<b3-backend-container-check>", "exec")
        container_tree = ast.parse(container_src)
        container_bad = [
            node.names[0].name if isinstance(node, ast.Import) else node.module
            for node in ast.walk(container_tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            and (
                (node.module if isinstance(node, ast.ImportFrom) else node.names[0].name) or ""
            ).split(".")[0]
            == "unittest"
        ]
        assert container_bad == [], f"Container script must not import unittest: {container_bad}"
        for expected_check in mod.EXPECTED_CONTAINER_CHECKS:
            assert expected_check in container_src

        # Execute the exact container program via stdin under APP_ENV=core / DALTON_PROFILE=core
        env = {
            k: v
            for k, v in __import__("os").environ.items()
            if not k.startswith("DALTON_CAP_")
            and k not in ("DALTON_RESEARCHDATA_URL", "DALTON_BRIDGE_URL", "CHANNEL_GATEWAY_URL", "BRAND_NAME")
        }
        env["APP_ENV"] = "core"
        env["DALTON_PROFILE"] = "core"
        proc = subprocess.run(
            [sys.executable, "-"],
            input=container_src,
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 0, (
            f"Container check script failed (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
        stdout_lines = [line for line in proc.stdout.splitlines() if line.strip()]
        assert len(stdout_lines) == 1, f"Expected single JSON line on stdout, got: {stdout_lines!r}"
        parsed = mod._parse_container_result(proc.stdout, proc.stderr, proc.returncode)
        assert parsed["ok"] is True
        for expected_check in mod.EXPECTED_CONTAINER_CHECKS:
            assert parsed["checks"][expected_check]["ok"] is True

        # Host-side source & docs checks run offline against REPO_ROOT
        assert mod._check_no_everflowinv_in_dist(REPO_ROOT) > 0
        mod._check_frontend_capability_gating(REPO_ROOT)
        monkeypatch.delenv("STANDALONE_BASE_URL", raising=False)
        docs_summary = mod._check_docs_site_core_surface(REPO_ROOT, packaged=False)
        assert docs_summary["included"] > 15 and docs_summary["excluded"] >= 5

        # Missing node executable raises RuntimeError explicitly instead of silently skipping
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        with pytest.raises(RuntimeError, match="node executable not found"):
            mod._check_docs_site_core_surface(REPO_ROOT, packaged=False)
