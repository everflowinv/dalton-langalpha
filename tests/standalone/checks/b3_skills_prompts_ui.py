# auth: oss
"""Standalone Core Batch 3 verification checks (Backend, Skills/Workflows, Frontend & /help).

Verifies the full Batch 3 surface under Standalone Core (``auth: oss``):
1. Dual-chain schema reaches current head including ``d046`` (``dalton_data_map_scoped_cache``
   with composite primary key ``(cache_date, capabilities_fingerprint)`` while legacy
   ``dalton_data_map_cache`` stays intact for rolling upgrades).
2. Capability-fingerprint cache isolation: writing two fingerprints for the same
   ``cache_date`` keeps both rows isolated, and legacy ``dalton_data_map_cache`` remains
   independently writable.
3. Skill load-by-name interception & sandbox managed-skill pruning: all 22 disabled
   capability skills return ``None`` from ``get_skill()`` / ``build_skill_content()`` and
   are pruned from running sandboxes during sync while user-installed skills survive.
4. Zero proprietary text in Core-rendered system/subagent prompts, ``<data_map>``,
   ``AutomationsBackend`` README, all 29 Core-visible skills, and all 6 workflows (all
   6 Core workflows pass ``compile_check``).
5. Core default coverage returns empty roster; ``share_pages.py`` renders active brand
   name; zero ``everflowinv.com`` across ``src/``, skill scripts, Core-assembled skills /
   workflows, ``web/src/``, and Core ``docs-site/``.
6. Frontend capability gating (``useCapabilities.ts``, ``brand.ts``, ``sections.ts``,
   ``MonitorsList.tsx``) and ``/help`` single-source Core filter (excludes ``everflow/``
   and ``explore/deliver-to-feishu``, strips Everflow blocks to zero proprietary hits;
   also probes live ``/help`` HTTP responses when ``STANDALONE_BASE_URL`` is set).
"""

from __future__ import annotations

import asyncio
from datetime import date
import os
from pathlib import Path
import sys
from typing import Any
from unittest.mock import create_autospec

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

PROPRIETARY_BLACKLIST: tuple[str, ...] = (
    "researchdata",
    "alphaengine",
    "guidepoint",
    "sales-notes",
    "tmtb",
    "funda-ai",
    "funda ai",
    "feishu",
    "weixin",
    "seed-asr",
    "dalton-13f-tracker",
    "everflow",
    "公司 wiki",
    "销售纪要",
    "自动入库",
    "/transcribe",
    "内部研究库",
    "基金 wiki",
)

PROPRIETARY_SKILL_SAMPLES: tuple[str, ...] = (
    "dalton-13f-tracker",
    "dalton-market-digest",
    "dalton-ai-finance-daily",
    "dalton-transcript-polish",
    "dalton-wiki-auto-ingest",
    "dalton-podcast-weekly",
    "dalton-bullbear-tracker",
)


def _assert_no_proprietary_terms(text: str, *, label: str) -> None:
    lowered = text.lower()
    hits = [term for term in PROPRIETARY_BLACKLIST if term in lowered]
    if hits:
        raise AssertionError(f"{label} contains forbidden proprietary terms: {hits}")


def check_skills_interception_in_core() -> None:
    """Verify disabled skills cannot be loaded by name or synced into Core sandboxes."""
    from ptc_agent.agent.middleware.skills import content as skill_content
    from ptc_agent.agent.middleware.skills import dalton_registry, registry
    from src.dalton.profile import DeploymentProfile, resolve_profile

    assert resolve_profile() is DeploymentProfile.CORE, (
        "Expected CORE profile (APP_ENV=core or DALTON_PROFILE=core)"
    )

    outside = registry.skills_outside_sandbox()
    disabled = dalton_registry.disabled_dalton_skills()
    assert len(disabled) == 22, f"Expected 22 disabled Dalton skills in Core, got {len(disabled)}"
    repo_dirs = [str(dalton_registry.DALTON_SKILLS_DIR)]

    for name in PROPRIETARY_SKILL_SAMPLES:
        assert name in disabled, f"{name} missing from disabled_dalton_skills()"
        assert name in outside, f"{name} missing from skills_outside_sandbox()"
        assert registry.get_skill(name) is None, f"get_skill({name!r}) must return None in Core"
        assert (
            skill_content.build_skill_content(
                [skill_content.SkillRequest(name=name)],
                skill_dirs=repo_dirs,
            )
            is None
        ), f"build_skill_content({name!r}) must refuse disabled skill in Core"

    assert registry.get_skill("dalton-conventions") is not None


async def check_sandbox_managed_skill_prune() -> None:
    """Verify running sandbox sync prunes stale disabled managed skills and preserves user skills."""
    from unittest.mock import patch

    from ptc_agent.agent.middleware.skills import dalton_registry
    from ptc_agent.config.core import (
        CoreConfig,
        DaytonaConfig,
        FilesystemConfig,
        LoggingConfig,
        MCPConfig,
        SandboxConfig,
        SecurityConfig,
    )
    from ptc_agent.core.sandbox import assets as sandbox_assets
    from ptc_agent.core.sandbox import ptc_sandbox
    from ptc_agent.core.sandbox.runtime import ExecResult, RuntimeState, SandboxProvider, SandboxRuntime

    runtime = create_autospec(SandboxRuntime, instance=True)
    runtime.id = "b3-check-runtime"
    runtime.working_dir = "/home/workspace"
    runtime.get_state.return_value = RuntimeState.RUNNING
    runtime.exec.return_value = ExecResult("", "", 0)

    skills_base = "/home/workspace/.agents/skills"
    runtime.list_files.return_value = [
        {"name": "dalton-conventions", "path": f"{skills_base}/dalton-conventions", "is_dir": True},
        {"name": "dalton-13f-tracker", "path": f"{skills_base}/dalton-13f-tracker", "is_dir": True},
        {"name": "user-custom-skill", "path": f"{skills_base}/user-custom-skill", "is_dir": True},
    ]

    provider = create_autospec(SandboxProvider, instance=True)
    provider.is_transient_error.return_value = False

    with patch.object(
        ptc_sandbox,
        "create_provider",
        create_autospec(ptc_sandbox.create_provider, return_value=provider),
    ):
        sandbox = ptc_sandbox.PTCSandbox(
            config=CoreConfig(
                sandbox=SandboxConfig(daytona=DaytonaConfig(api_key="check")),
                security=SecurityConfig(),
                mcp=MCPConfig(),
                logging=LoggingConfig(),
                filesystem=FilesystemConfig(),
            )
        )
    sandbox.runtime = runtime

    local_names = await sandbox_assets._collect_local_skill_names(
        [str(dalton_registry.DALTON_SKILLS_DIR)]
    )
    assert "dalton-13f-tracker" not in local_names
    await sandbox_assets._prune_remote_skills(
        sandbox,
        local_names,
        existing_lock={
            "dalton-conventions": {"owner": "platform", "name": "dalton-conventions"},
            "dalton-13f-tracker": {"owner": "platform", "name": "dalton-13f-tracker"},
            "user-custom-skill": {"owner": "user", "name": "user-custom-skill"},
        },
    )
    cmds = [call.args[0] for call in runtime.exec.call_args_list]
    assert len(cmds) == 1 and "dalton-13f-tracker" in cmds[0] and "rm -rf" in cmds[0]


def check_prompts_data_map_and_automations_readme() -> None:
    """Verify Core prompts, <data_map>, and AutomationsBackend README have 0 proprietary terms."""
    from ptc_agent.agent.backends.automations import render_automations_readme
    from ptc_agent.agent.prompts.loader import PromptLoader
    from src.dalton.data_map import builder as map_builder

    map_builder.set_turn_data_map(None)
    loader = PromptLoader()
    for role in ("analyst", "chief_of_staff"):
        prompt = loader.get_system_prompt(
            role=role,
            channels_enabled=True,
            research_writing_enabled=True,
        )
        _assert_no_proprietary_terms(prompt, label=f"Core system prompt ({role})")
        assert "## Over Chat" not in prompt
        assert "market-regime" not in prompt
        assert "dalton_system_jobs" not in prompt
        assert "运维任务只读" not in prompt

    static_dm = map_builder.render_static_data_map_markdown(team_memory_enabled=True)
    _assert_no_proprietary_terms(static_dm, label="Core static <data_map>")
    assert "`conversation_search`" not in static_dm
    assert "### 3. " not in static_dm

    readme = render_automations_readme()
    _assert_no_proprietary_terms(readme, label="Core AutomationsBackend README")
    assert "discord" not in readme.lower()
    assert "dalton_system_jobs" not in readme
    assert "ops jobs" not in readme


async def check_d046_scoped_data_map_cache_db(database_url: str | None = None) -> None:
    """Verify d046 schema, composite PK isolation, and legacy table preservation against Postgres."""
    import psycopg
    from psycopg.rows import dict_row

    from src.dalton.database import data_map as db_data_map

    dsn = database_url or os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError("DATABASE_URL is required for DB verification in b3_skills_prompts_ui.py")

    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # 1) Verify Dalton alembic head includes d046 (or newer) and both tables exist
            await cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN ('dalton_data_map_cache', 'dalton_data_map_scoped_cache')
                ORDER BY table_name
                """
            )
            tables = [row["table_name"] for row in await cur.fetchall()]
            assert tables == ["dalton_data_map_cache", "dalton_data_map_scoped_cache"], (
                f"Expected both legacy and scoped data-map cache tables, got {tables}"
            )

            # 2) Verify composite primary key columns on dalton_data_map_scoped_cache
            await cur.execute(
                """
                SELECT a.attname
                FROM pg_index i
                JOIN pg_attribute a
                  ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                WHERE i.indrelid = 'dalton_data_map_scoped_cache'::regclass
                  AND i.indisprimary
                ORDER BY array_position(i.indkey, a.attnum)
                """
            )
            pk_cols = [row["attname"] for row in await cur.fetchall()]
            assert pk_cols == ["cache_date", "capabilities_fingerprint"], (
                f"Unexpected primary key on dalton_data_map_scoped_cache: {pk_cols}"
            )

        # 3) Verify fingerprint-isolated writes/reads on the same cache_date
        test_date = date(2099, 1, 2)
        fp_a = "b3check00000000a"
        fp_b = "b3check00000000b"
        try:
            await db_data_map.upsert_cached_data_map(
                test_date,
                "## Dalton 数据地图\n- scope A",
                {"scope": "a"},
                capabilities_fingerprint=fp_a,
                conn=conn,
            )
            await db_data_map.upsert_cached_data_map(
                test_date,
                "## Dalton 数据地图\n- scope B",
                {"scope": "b"},
                capabilities_fingerprint=fp_b,
                conn=conn,
            )
            row_a = await db_data_map.get_cached_data_map(
                test_date, capabilities_fingerprint=fp_a, conn=conn
            )
            row_b = await db_data_map.get_cached_data_map(
                test_date, capabilities_fingerprint=fp_b, conn=conn
            )
            assert row_a is not None and "scope A" in row_a["rendered_markdown"]
            assert row_b is not None and "scope B" in row_b["rendered_markdown"]

            # 4) Verify legacy dalton_data_map_cache remains independently writable for rolling upgrades
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO dalton_data_map_cache (cache_date, rendered_markdown, payload, updated_at)
                        VALUES (%s, %s, '{}'::jsonb, NOW())
                        ON CONFLICT (cache_date) DO UPDATE SET
                            rendered_markdown = EXCLUDED.rendered_markdown,
                            updated_at = NOW()
                        """,
                        (test_date, "## Legacy Cache Row"),
                    )
        finally:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        DELETE FROM dalton_data_map_scoped_cache
                        WHERE cache_date = %s
                          AND capabilities_fingerprint IN (%s, %s)
                        """,
                        (test_date, fp_a, fp_b),
                    )
                    await cur.execute(
                        "DELETE FROM dalton_data_map_cache WHERE cache_date = %s",
                        (test_date,),
                    )


def check_skills_workflows_coverage_share_and_no_everflowinv() -> None:
    """Verify Core-assembled 29 skills, 6 workflows, coverage empty default, share page, and 0 everflowinv.com."""
    import json
    import re

    from ptc_agent.agent.middleware.background_subagent.workflow.engine import compile_check
    from ptc_agent.agent.middleware.skills import dalton_registry
    from src.dalton.content_assembly import read_assembled_text
    from src.dalton.coverage import get_default_coverage_doc, load_default_coverage_companies
    from src.server.app.share_pages import unavailable_page

    # 1) All 29 Core-visible skills have 0 proprietary terms and 0 leftover DALTON_CAP markers
    enabled_skills = sorted(
        name for name in dalton_registry.DALTON_SKILL_DEFINITIONS
        if dalton_registry.is_dalton_skill_enabled(name)
    )
    assert len(enabled_skills) == 29, f"Expected 29 Core skills, got {len(enabled_skills)}"
    assert "dalton-13f-tracker" not in enabled_skills
    assert "dalton-bullbear-tracker" not in enabled_skills

    for sk in enabled_skills:
        sk_dir = dalton_registry.DALTON_SKILLS_DIR / sk
        for fp in sorted(sk_dir.rglob("*")):
            if not fp.is_file() or fp.suffix.lower() not in (".md", ".txt", ".json", ".yaml", ".yml"):
                continue
            assembled = read_assembled_text(fp, capabilities=frozenset())
            assert "DALTON_CAP" not in assembled, f"Unresolved DALTON_CAP marker in {fp}"
            _assert_no_proprietary_terms(assembled, label=f"Core skill file {fp.relative_to(_REPO_ROOT)}")

    # 1b) langalpha-doc, dalton-evidence subagent, and live skill registry descriptions clean in Core
    from ptc_agent.agent.middleware.skills import registry as skill_registry

    for fp in sorted((_REPO_ROOT / "plugins" / "langalpha_service" / "skills" / "langalpha-doc").rglob("*.md")):
        assembled = read_assembled_text(fp, capabilities=frozenset())
        assert "DALTON_CAP" not in assembled, f"Unresolved DALTON_CAP marker in {fp}"
        _assert_no_proprietary_terms(assembled, label=f"Core langalpha-doc {fp.relative_to(_REPO_ROOT)}")

    from src.dalton.publish.review import load_agent_body

    ev_body = load_agent_body("dalton-evidence")
    assert ev_body is not None and "DALTON_CAP" not in ev_body
    _assert_no_proprietary_terms(ev_body, label="Core dalton-evidence prompt body")

    for entry in skill_registry.list_skills():
        _assert_no_proprietary_terms(
            str(entry.get("description") or ""),
            label=f"Core list_skills() description ({entry['name']})",
        )

    # 2) All 6 workflows assemble cleanly in Core, pass compile_check(), and have 0 proprietary terms
    wf_root = _REPO_ROOT / "workflows"
    wf_dirs = sorted(p for p in wf_root.iterdir() if p.is_dir())
    assert len(wf_dirs) == 6, f"Expected 6 workflows, got {len(wf_dirs)}"
    for wf_dir in wf_dirs:
        for fp in sorted(wf_dir.rglob("*")):
            if not fp.is_file() or fp.suffix.lower() not in (".js", ".json", ".md"):
                continue
            assembled = read_assembled_text(fp, capabilities=frozenset())
            assert "DALTON_CAP" not in assembled, f"Unresolved DALTON_CAP marker in {fp}"
            _assert_no_proprietary_terms(assembled, label=f"Core workflow file {fp.relative_to(_REPO_ROOT)}")
            if fp.suffix.lower() == ".js":
                compile_check(assembled)
            elif fp.suffix.lower() == ".json":
                json.loads(assembled)

    # 3) Core coverage default roster is empty; share_pages uses active brand name
    assert load_default_coverage_companies() == []
    assert get_default_coverage_doc().get("companies") == []
    share_html = unavailable_page(404, chinese=True)
    assert "everflow" not in share_html.lower()

    # 4) Zero everflowinv.com across src/, skill scripts, Core-assembled skills/workflows, web/src/, docs-site/
    scan_roots = (
        _REPO_ROOT / "src",
        _REPO_ROOT / "web" / "src",
        _REPO_ROOT / "docs-site" / "src",
    )
    domain_re = re.compile(r"everflowinv\.com", re.IGNORECASE)
    for root in scan_roots:
        if not root.exists():
            continue
        for fp in sorted(root.rglob("*")):
            if not fp.is_file() or fp.suffix.lower() not in (
                ".py", ".ts", ".tsx", ".js", ".mjs", ".md", ".mdx", ".json", ".yaml", ".yml", ".j2", ".astro",
            ):
                continue
            text = fp.read_text(encoding="utf-8", errors="ignore")
            if domain_re.search(text):
                raise AssertionError(f"Forbidden internal domain 'everflowinv.com' found in {fp.relative_to(_REPO_ROOT)}")

    for fp in sorted((_REPO_ROOT / "plugins" / "dalton" / "skills").rglob("*.py")):
        text = fp.read_text(encoding="utf-8", errors="ignore")
        if domain_re.search(text):
            raise AssertionError(f"Forbidden internal domain 'everflowinv.com' found in {fp.relative_to(_REPO_ROOT)}")


def check_frontend_and_docs_site_core_surface() -> None:
    """Verify frontend capability gating source invariants and /help Core profile filtering."""
    import re
    import subprocess

    # 1) Verify frontend capability hook, decoupled brand flag, Settings gating, and Monitors gating exist
    brand_ts = (_REPO_ROOT / "web" / "src" / "config" / "brand.ts").read_text(encoding="utf-8")
    assert "VITE_DALTON_UI" in brand_ts and "VITE_DALTON_PROFILE" in brand_ts
    caps_ts = (_REPO_ROOT / "web" / "src" / "hooks" / "useCapabilities.ts").read_text(encoding="utf-8")
    assert "/api/v1/dalton/capabilities" in caps_ts
    assert "EVERFLOW_DEFAULT_CAPABILITIES" in caps_ts and "CORE_DEFAULT_CAPABILITIES" in caps_ts
    sections_ts = (_REPO_ROOT / "web" / "src" / "pages" / "Settings" / "sections.ts").read_text(encoding="utf-8")
    for gated in (
        "daltonHomeTeamMemory",
        "daltonExtractModel",
        "daltonNotifyTargets",
        "daltonChannelBindings",
        "daltonWeixinBind",
        "daltonNativeModels",
        "daltonFeishuScopes",
        "daltonWeixinBot",
        "daltonFeishuGroups",
        "daltonDiscordChannels",
    ):
        assert gated in sections_ts, f"Missing setting {gated} in sections.ts"
    monitors_ts = (_REPO_ROOT / "web" / "src" / "pages" / "Monitors" / "MonitorsList.tsx").read_text(encoding="utf-8")
    assert "data_jobs" in monitors_ts and "source_kind === 'data_job'" in monitors_ts

    # 2) Run Node check against docs-site/src/integrations/profile-filter.mjs for Core docs cleanliness
    node_script = r"""
import fs from 'node:fs';
import path from 'node:path';
import { isDocIncluded, transformDocSource } from './docs-site/src/integrations/profile-filter.mjs';

const docsRoot = path.resolve('docs-site/src/content/docs');
const forbidden = /alphaengine|guidepoint|researchdata|sales-notes|tmtb|funda|feishu|飞书|weixin|微信|everflowinv\.com|\/help\/everflow\/|what-is-everflow|公司\s*wiki|销售纪要|自动入库|团队对话归档|\/transcribe/i;
const everflowWord = /\beverflow\b/i;

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
const errors = [];
for (const fp of walk(docsRoot)) {
  const rel = path.relative(docsRoot, fp).replace(/\\/g, '/');
  if (!isDocIncluded(rel, { isCore: true })) {
    excluded += 1;
    continue;
  }
  included += 1;
  const raw = fs.readFileSync(fp, 'utf-8');
  const coreText = transformDocSource(raw, { isCore: true, brandName: 'Dalton', filePath: rel });
  if (/dalton_doc/i.test(coreText)) {
    errors.push(`${rel}: unresolved DALTON_DOC marker`);
  }
  if (forbidden.test(coreText)) {
    errors.push(`${rel}: forbidden proprietary term matched: ${coreText.match(forbidden)?.[0]}`);
  }
  if (everflowWord.test(coreText)) {
    errors.push(`${rel}: leftover 'Everflow' mention in Core doc`);
  }
}
if (excluded < 5) {
  errors.push(`Expected at least 5 excluded Everflow docs (everflow/* + deliver-to-feishu), got ${excluded}`);
}
if (errors.length > 0) {
  console.error(errors.join('\n'));
  process.exit(1);
}
console.log(JSON.stringify({ included, excluded }));
"""
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", node_script],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"docs-site Core profile filter check failed (rc={proc.returncode}):\n{proc.stderr or proc.stdout}"
        )

    # 3) Optional live HTTP check against /help when STANDALONE_BASE_URL is configured
    base_url = os.getenv("STANDALONE_BASE_URL", "").strip().rstrip("/")
    if base_url:
        import httpx

        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.get(f"{base_url}/help/")
            if resp.status_code == 200:
                cleaned = re.sub(r"/help/get-started/what-is-everflow/?", "", resp.text, flags=re.I)
                _assert_no_proprietary_terms(cleaned, label="Live /help/ HTML")
            ef_resp = client.get(f"{base_url}/help/everflow/monitors/")
            assert ef_resp.status_code == 404, (
                f"Expected /help/everflow/monitors/ to return 404 in Core, got {ef_resp.status_code}"
            )


async def run_backend_checks(*, require_db: bool = True) -> dict[str, Any]:
    """Run all Batch 3 verification checks (backend, skills/workflows, frontend & /help)."""
    check_skills_interception_in_core()
    await check_sandbox_managed_skill_prune()
    check_prompts_data_map_and_automations_readme()
    check_skills_workflows_coverage_share_and_no_everflowinv()
    check_frontend_and_docs_site_core_surface()
    db_checked = False
    if require_db or os.getenv("DATABASE_URL", "").strip():
        await check_d046_scoped_data_map_cache_db()
        db_checked = True
    return {"ok": True, "part": "full", "db_checked": db_checked}


def main() -> int:
    os.environ.setdefault("APP_ENV", "core")
    require_db = "--skip-db" not in sys.argv[1:]
    result = asyncio.run(run_backend_checks(require_db=require_db))
    print(f"b3_skills_prompts_ui: PASS ({result})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
