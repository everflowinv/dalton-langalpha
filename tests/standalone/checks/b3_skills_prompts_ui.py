# auth: oss
"""Standalone Core Batch 3 verification checks (`b3_skills_prompts_ui.py`).

Uses Python standard library + `_lib.py` only so it runs directly on a bare CI
runner alongside `b1_dev_mode.py` and `b2_backend_capabilities.py` (no `src`,
`ptc_agent`, `unittest`, `httpx`, or `psycopg` imports on the host).

Verifies the full Batch 3 surface under Standalone Core (`auth: oss`):
1. Host -> Postgres (`psql`): dual Alembic chains reach current head including
   `d046`; both `dalton_data_map_scoped_cache` (composite PK
   `(cache_date, capabilities_fingerprint)`) and legacy `dalton_data_map_cache`
   exist; per-fingerprint writes/reads stay isolated and legacy table remains
   independently writable (temporary rows cleaned up on exit).
2. Host -> `backend` container (`uv run --no-sync python -` under
   `APP_ENV=core`, `DALTON_PROFILE=core`):
   - Skill load-by-name interception (all 22 disabled skills refused by
     `get_skill()` / `build_skill_content()`, Core skills stay loadable).
   - Managed-skill pruning against a real temporary managed-skills directory
     (disabled platform skill removed, active Core + user skills preserved).
   - Zero proprietary terms in Core system/subagent prompts, static `<data_map>`,
     and `AutomationsBackend` README.
   - All 30 Core-visible Dalton skills, `langalpha-doc`, `dalton-evidence`,
     live `list_skills()` descriptions, and all 6 workflows assemble cleanly
     with zero proprietary terms and pass `compile_check()` / `json.loads()`.
   - Core default coverage roster is empty; `share_pages.unavailable_page()`
     renders the active brand name with zero Everflow references.
3. Host source-tree scan: zero `everflowinv.com` references across `src/`,
   `web/src/`, `docs-site/src/`, and `plugins/dalton/skills/**/*.py`.
4. Host frontend source invariants: capability gating in `brand.ts`,
   `useCapabilities.ts`, `sections.ts`, and `MonitorsList.tsx`.
5. Host `/help` docs-site Core profile filter via Node (fails explicitly if
   `node` is unavailable) plus optional live `/help` HTTP probe when
   `STANDALONE_BASE_URL` is configured.
"""

from __future__ import annotations

from datetime import date, timedelta
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any
import uuid

# Allow invoking directly as `python3 tests/standalone/checks/b3_skills_prompts_ui.py`
_CHECKS_DIR = str(Path(__file__).resolve().parent)
if _CHECKS_DIR not in sys.path:
    sys.path.insert(0, _CHECKS_DIR)
_lib = importlib.import_module("_lib")

_DIST_ROOT = Path(__file__).resolve().parents[3]
REQUIRED_DALTON_REVISIONS = ("d046",)
_DALTON_REV_RE = re.compile(r"\b(d\d+)\b")
HTTP_TIMEOUT_S = 15.0
NODE_TIMEOUT_S = 30.0

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

EXPECTED_CONTAINER_CHECKS: tuple[str, ...] = (
    "skills_interception",
    "managed_skill_prune",
    "prompts_data_map_automations",
    "skills_and_workflows",
    "coverage_and_share_pages",
)

BACKEND_CONTAINER_CHECK_SCRIPT = r'''
from __future__ import annotations

import asyncio
import html
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
from typing import Any

# Enforce Standalone Core profile before importing project modules.
os.environ["APP_ENV"] = "core"
os.environ["DALTON_PROFILE"] = "core"

_cwd = Path.cwd().resolve()
for _candidate in (_cwd, _cwd / "src"):
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

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


def _reset_caches() -> None:
    from ptc_agent.agent.prompts import loader as prompt_loader_mod
    from src.config.core import clear_config_cache
    from src.dalton.data_map import builder as map_builder
    from src.dalton.settings import get_dalton_config

    clear_config_cache()
    get_dalton_config.cache_clear()
    map_builder.set_turn_data_map(None)
    prompt_loader_mod.reset_loader()


def check_skills_interception() -> dict[str, Any]:
    from ptc_agent.agent.middleware.skills import content as skill_content
    from ptc_agent.agent.middleware.skills import dalton_registry, registry
    from src.dalton.profile import DeploymentProfile, resolve_profile

    assert resolve_profile() is DeploymentProfile.CORE, (
        "Expected CORE profile (APP_ENV=core, DALTON_PROFILE=core)"
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
    return {
        "ok": True,
        "disabled_count": len(disabled),
        "samples_checked": len(PROPRIETARY_SKILL_SAMPLES),
    }


async def check_managed_skill_prune() -> dict[str, Any]:
    from ptc_agent.agent.middleware.skills import dalton_registry
    from ptc_agent.core.paths import SandboxLayout
    from ptc_agent.core.sandbox import assets as sandbox_assets
    from ptc_agent.core.sandbox.runtime import ExecResult

    class _TempDirRuntime:
        def __init__(self, root: Path) -> None:
            self.root = root.resolve()
            self.executed_cmds: list[str] = []

        async def exec(self, cmd: str) -> ExecResult:
            self.executed_cmds.append(cmd)
            parts = shlex.split(cmd)
            if len(parts) == 3 and parts[0] == "rm" and parts[1] == "-rf":
                target = Path(parts[2]).resolve()
                if self.root in target.parents and target.exists():
                    shutil.rmtree(target)
                    return ExecResult("", "", 0)
                return ExecResult("", f"refused or missing target: {target}", 1)
            return ExecResult("", f"unexpected command: {cmd}", 1)

    class _TempDirSandbox:
        def __init__(self, work_dir: Path) -> None:
            self.layout = SandboxLayout(str(work_dir))
            self.runtime = _TempDirRuntime(work_dir)

        async def als_directory(self, path: str) -> list[dict[str, Any]]:
            base = Path(path)
            if not base.is_dir():
                return []
            return [
                {"name": p.name, "path": str(p), "is_dir": p.is_dir()}
                for p in sorted(base.iterdir())
            ]

        async def _runtime_call(self, fn: Any, *args: Any, **_kwargs: Any) -> Any:
            return await fn(*args)

    with tempfile.TemporaryDirectory(prefix="b3-managed-skills-") as tmp_dir:
        work_root = Path(tmp_dir)
        sandbox = _TempDirSandbox(work_root)
        skills_dir = Path(sandbox.layout.skills)
        skills_dir.mkdir(parents=True, exist_ok=True)

        for name in ("dalton-conventions", "dalton-13f-tracker", "user-custom-skill"):
            sdir = skills_dir / name
            sdir.mkdir(parents=True, exist_ok=True)
            (sdir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")

        local_names = await sandbox_assets._collect_local_skill_names(
            [str(dalton_registry.DALTON_SKILLS_DIR)]
        )
        assert "dalton-conventions" in local_names, "dalton-conventions missing from local Core skills"
        assert "dalton-13f-tracker" not in local_names, "dalton-13f-tracker leaked into local Core skills"

        failed = await sandbox_assets._prune_remote_skills(
            sandbox,
            local_names,
            existing_lock={
                "dalton-conventions": {"owner": "platform", "name": "dalton-conventions"},
                "dalton-13f-tracker": {"owner": "platform", "name": "dalton-13f-tracker"},
                "user-custom-skill": {"owner": "user", "name": "user-custom-skill"},
            },
        )
        assert failed == set(), f"Unexpected prune failures: {failed}"
        assert not (skills_dir / "dalton-13f-tracker").exists(), (
            "Disabled platform skill dalton-13f-tracker was not removed from temp skills dir"
        )
        assert (skills_dir / "dalton-conventions").is_dir(), (
            "Active Core skill dalton-conventions was unexpectedly removed"
        )
        assert (skills_dir / "user-custom-skill").is_dir(), (
            "User-installed skill user-custom-skill was unexpectedly removed"
        )
        cmds = sandbox.runtime.executed_cmds
        assert len(cmds) == 1 and "dalton-13f-tracker" in cmds[0] and "rm -rf" in cmds[0], (
            f"Unexpected prune commands: {cmds}"
        )

    return {
        "ok": True,
        "pruned": ["dalton-13f-tracker"],
        "preserved": ["dalton-conventions", "user-custom-skill"],
    }


def check_prompts_data_map_automations() -> dict[str, Any]:
    from ptc_agent.agent.backends.automations import render_automations_readme
    from ptc_agent.agent.prompts.loader import PromptLoader
    from src.dalton.data_map import builder as map_builder

    map_builder.set_turn_data_map(None)
    loader = PromptLoader()

    roles_checked: list[str] = []
    for role in ("analyst", "chief_of_staff"):
        prompt = loader.get_system_prompt(
            role=role,
            channels_enabled=True,
            research_writing_enabled=True,
        )
        _assert_no_proprietary_terms(prompt, label=f"Core system prompt ({role})")
        assert "## Over Chat" not in prompt, f"Core system prompt ({role}) leaked '## Over Chat'"
        assert "market-regime" not in prompt, f"Core system prompt ({role}) leaked 'market-regime'"
        assert "dalton_system_jobs" not in prompt, (
            f"Core system prompt ({role}) leaked 'dalton_system_jobs'"
        )
        assert "运维任务只读" not in prompt, f"Core system prompt ({role}) leaked '运维任务只读'"
        roles_checked.append(role)

    subagents_checked: list[str] = []
    for sub in ("research", "general", "data_prep", "report_builder"):
        tpl_path = loader.templates_dir / "subagents" / f"{sub}.md.j2"
        if not tpl_path.is_file():
            continue
        rendered_sub = loader.get_subagent_base_prompt(
            identity_line=f"You are {sub}.",
            role_prompt_template=f"{sub}.md.j2",
        )
        _assert_no_proprietary_terms(rendered_sub, label=f"Core subagent prompt ({sub})")
        subagents_checked.append(sub)

    static_dm = map_builder.render_static_data_map_markdown(team_memory_enabled=True)
    _assert_no_proprietary_terms(static_dm, label="Core static <data_map>")
    assert "`conversation_search`" not in static_dm
    assert "`company-wiki`" in static_dm
    assert "### 2. 已启用的研究连接器" in static_dm
    assert "### 3. 明确没有的数据（切勿以为查过）" in static_dm

    static_dm_no_connectors = map_builder.render_static_data_map_markdown(
        team_memory_enabled=True,
        capabilities=frozenset(),
    )
    _assert_no_proprietary_terms(static_dm_no_connectors, label="Core no-connector static <data_map>")
    assert "`conversation_search`" not in static_dm_no_connectors
    assert "### 3. " not in static_dm_no_connectors

    readme = render_automations_readme()
    _assert_no_proprietary_terms(readme, label="Core AutomationsBackend README")
    assert "discord" not in readme.lower()
    assert "dalton_system_jobs" not in readme
    assert "ops jobs" not in readme

    return {
        "ok": True,
        "roles": roles_checked,
        "subagents": subagents_checked,
    }


def check_skills_and_workflows() -> dict[str, Any]:
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import compile_check
    from ptc_agent.agent.middleware.skills import dalton_registry
    from ptc_agent.agent.middleware.skills import registry as skill_registry
    from src.dalton.capabilities import effective_capabilities
    from src.dalton.content_assembly import read_assembled_text
    from src.dalton.publish.review import load_agent_body
    from src.dalton.settings import REPO_ROOT as app_root

    default_core_caps = effective_capabilities()
    core_default_proprietary_terms = tuple(
        t for t in PROPRIETARY_BLACKLIST if t not in ("公司 wiki", "内部研究库", "基金 wiki")
    )
    enabled_skills = sorted(
        name
        for name in dalton_registry.DALTON_SKILL_DEFINITIONS
        if dalton_registry.is_dalton_skill_enabled(name)
    )
    assert len(enabled_skills) == 30, f"Expected 30 Core skills, got {len(enabled_skills)}"
    assert "dalton-13f-tracker" not in enabled_skills
    assert "dalton-bullbear-tracker" not in enabled_skills

    skill_files_checked = 0
    for sk in enabled_skills:
        sk_dir = dalton_registry.DALTON_SKILLS_DIR / sk
        assert sk_dir.is_dir(), f"Missing enabled Core skill directory: {sk_dir}"
        for fp in sorted(sk_dir.rglob("*")):
            if not fp.is_file() or fp.suffix.lower() not in (".md", ".txt", ".json", ".yaml", ".yml"):
                continue
            assembled = read_assembled_text(fp, capabilities=frozenset())
            assert "DALTON_CAP" not in assembled, f"Unresolved DALTON_CAP marker in {fp}"
            _assert_no_proprietary_terms(assembled, label=f"Core skill file {fp.relative_to(app_root)}")
            assembled_default = read_assembled_text(fp, capabilities=default_core_caps)
            assert "DALTON_CAP" not in assembled_default, f"Unresolved DALTON_CAP marker in {fp}"
            hits_default = [t for t in core_default_proprietary_terms if t in assembled_default.lower()]
            assert not hits_default, f"Core default skill file {fp.relative_to(app_root)} leaked {hits_default}"
            skill_files_checked += 1

    langalpha_doc_dir = app_root / "plugins" / "langalpha_service" / "skills" / "langalpha-doc"
    doc_files = sorted(langalpha_doc_dir.rglob("*.md"))
    assert doc_files, f"Expected langalpha-doc markdown files under {langalpha_doc_dir}"
    for fp in doc_files:
        assembled = read_assembled_text(fp, capabilities=frozenset())
        assert "DALTON_CAP" not in assembled, f"Unresolved DALTON_CAP marker in {fp}"
        _assert_no_proprietary_terms(assembled, label=f"Core langalpha-doc {fp.relative_to(app_root)}")

    ev_body = load_agent_body("dalton-evidence")
    assert ev_body is not None and "DALTON_CAP" not in ev_body
    _assert_no_proprietary_terms(ev_body, label="Core dalton-evidence prompt body")

    listed_skills = skill_registry.list_skills()
    for entry in listed_skills:
        _assert_no_proprietary_terms(
            str(entry.get("description") or ""),
            label=f"Core list_skills() description ({entry['name']})",
        )

    wf_root = app_root / "workflows"
    wf_dirs = sorted(p for p in wf_root.iterdir() if p.is_dir())
    wf_named = [p for p in wf_dirs if not p.name.startswith("_")]
    assert len(wf_named) == 7, f"Expected 7 workflows, got {len(wf_named)}"
    js_compiled = 0
    for wf_dir in wf_dirs:
        for fp in sorted(wf_dir.rglob("*")):
            if not fp.is_file() or fp.suffix.lower() not in (".js", ".json", ".md"):
                continue
            assembled = read_assembled_text(fp, capabilities=frozenset())
            assert "DALTON_CAP" not in assembled, f"Unresolved DALTON_CAP marker in {fp}"
            _assert_no_proprietary_terms(assembled, label=f"Core workflow file {fp.relative_to(app_root)}")
            assembled_default = read_assembled_text(fp, capabilities=default_core_caps)
            assert "DALTON_CAP" not in assembled_default, f"Unresolved DALTON_CAP marker in {fp}"
            hits_default = [t for t in core_default_proprietary_terms if t in assembled_default.lower()]
            assert not hits_default, f"Core default workflow file {fp.relative_to(app_root)} leaked {hits_default}"
            if fp.suffix.lower() == ".js" and not wf_dir.name.startswith("_"):
                compile_check(assembled)
                compile_check(assembled_default)
                js_compiled += 1
            elif fp.suffix.lower() == ".json":
                json.loads(assembled)
                json.loads(assembled_default)

    assert js_compiled > 0, "Expected at least one workflow .js file to pass compile_check()"
    return {
        "ok": True,
        "enabled_skills": len(enabled_skills),
        "skill_files_checked": skill_files_checked,
        "workflows": len(wf_dirs),
        "js_compiled": js_compiled,
        "listed_skills": len(listed_skills),
    }


def check_coverage_and_share_pages() -> dict[str, Any]:
    from src.dalton.coverage import get_default_coverage_doc, load_default_coverage_companies
    from src.dalton.settings import get_brand_name
    from src.server.app.share_pages import unavailable_page

    assert load_default_coverage_companies() == [], "Core default coverage companies must be empty"
    assert get_default_coverage_doc().get("companies") == [], (
        "Core default coverage doc must have empty companies list"
    )
    brand = get_brand_name()
    assert "everflow" not in brand.lower(), f"Active Core brand name leaked Everflow: {brand!r}"
    escaped_brand = html.escape(brand)
    for chinese in (True, False):
        share_html = unavailable_page(404, chinese=chinese)
        assert "everflow" not in share_html.lower(), "share_pages leaked Everflow in Core"
        assert escaped_brand in share_html, (
            f"share_pages missing active brand name {escaped_brand!r}"
        )

    return {"ok": True, "brand": brand, "default_coverage_count": 0}


def main() -> int:
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    results: dict[str, dict[str, Any]] = {}
    overall_ok = True
    try:
        _reset_caches()
        steps: list[tuple[str, Any, bool]] = [
            ("skills_interception", check_skills_interception, False),
            ("managed_skill_prune", check_managed_skill_prune, True),
            ("prompts_data_map_automations", check_prompts_data_map_automations, False),
            ("skills_and_workflows", check_skills_and_workflows, False),
            ("coverage_and_share_pages", check_coverage_and_share_pages, False),
        ]
        for name, fn, is_async in steps:
            try:
                results[name] = asyncio.run(fn()) if is_async else fn()
            except Exception as exc:  # noqa: BLE001
                overall_ok = False
                results[name] = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
    finally:
        sys.stdout = real_stdout

    print(json.dumps({"ok": overall_ok, "checks": results}, ensure_ascii=False), flush=True)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


def log(msg: str) -> None:
    print(f"[b3_skills_prompts_ui] {msg}", flush=True)


def _sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _assert_no_proprietary_terms(text: str, *, label: str) -> None:
    lowered = text.lower()
    hits = [term for term in PROPRIETARY_BLACKLIST if term in lowered]
    if hits:
        raise AssertionError(f"{label} contains forbidden proprietary terms: {hits}")


def _check_d046_scoped_data_map_cache_db(ctx: _lib.ComposeContext) -> None:
    """1/5 Verify dual Alembic heads, d046 schema, composite PK isolation, and legacy table."""
    log("1/5 Checking dual Alembic heads, d046 schema, and scoped data-map cache isolation...")
    expected_upstream, expected_dalton = ctx.get_alembic_heads()
    actual_upstream, actual_dalton = ctx.get_db_alembic_versions()

    assert actual_upstream == expected_upstream, (
        f"Upstream alembic_version mismatch: actual={actual_upstream!r}, expected={expected_upstream!r}"
    )
    assert actual_dalton == expected_dalton, (
        f"Dalton alembic_version_dalton mismatch: actual={actual_dalton!r}, expected={expected_dalton!r}"
    )

    history_out = ctx.compose(
        "exec",
        "-T",
        "backend",
        "uv",
        "run",
        "--no-sync",
        "alembic",
        "-c",
        "src/dalton/alembic.ini",
        "history",
        "-r",
        f"base:{actual_dalton}",
    ).stdout.decode("utf-8", errors="replace")
    applied_revs: set[str] = {actual_dalton}
    for line in history_out.splitlines():
        head_part = line.split(",", 1)[0]
        applied_revs.update(_DALTON_REV_RE.findall(head_part))
    missing_revs = [rev for rev in REQUIRED_DALTON_REVISIONS if rev not in applied_revs]
    assert not missing_revs, (
        f"Dalton migration head {actual_dalton!r} does not include required revisions "
        f"{missing_revs} (seen={sorted(applied_revs)})"
    )

    tables_raw = ctx.psql_query(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name IN ('dalton_data_map_cache', 'dalton_data_map_scoped_cache')
        ORDER BY table_name;
        """
    )
    tables = [line.strip() for line in tables_raw.splitlines() if line.strip()]
    assert tables == ["dalton_data_map_cache", "dalton_data_map_scoped_cache"], (
        f"Expected both legacy and scoped data-map cache tables, got {tables!r}"
    )

    pk_raw = ctx.psql_query(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a
          ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = 'dalton_data_map_scoped_cache'::regclass
          AND i.indisprimary
        ORDER BY array_position(i.indkey, a.attnum);
        """
    )
    pk_cols = [line.strip() for line in pk_raw.splitlines() if line.strip()]
    assert pk_cols == ["cache_date", "capabilities_fingerprint"], (
        f"Unexpected primary key on dalton_data_map_scoped_cache: {pk_cols!r}"
    )

    run_token = uuid.uuid4().hex[:12]
    test_date = (date(2095, 1, 1) + timedelta(days=int(run_token[:4], 16) % 1800)).isoformat()
    fp_a = f"b3check-{run_token}-a"
    fp_b = f"b3check-{run_token}-b"
    q_date = _sql_quote(test_date)
    q_fp_a = _sql_quote(fp_a)
    q_fp_b = _sql_quote(fp_b)
    q_md_a = _sql_quote(f"## Dalton 数据地图\n- scope A ({run_token})")
    q_md_b = _sql_quote(f"## Dalton 数据地图\n- scope B ({run_token})")
    q_legacy_md = _sql_quote(f"## Legacy Cache Row ({run_token})")

    try:
        ctx.psql_exec(
            f"""
            INSERT INTO dalton_data_map_scoped_cache (
                cache_date, capabilities_fingerprint, rendered_markdown, payload, updated_at
            )
            VALUES
                ({q_date}, {q_fp_a}, {q_md_a}, '{{"scope":"a"}}'::jsonb, NOW()),
                ({q_date}, {q_fp_b}, {q_md_b}, '{{"scope":"b"}}'::jsonb, NOW())
            ON CONFLICT (cache_date, capabilities_fingerprint) DO UPDATE SET
                rendered_markdown = EXCLUDED.rendered_markdown,
                payload = EXCLUDED.payload,
                updated_at = NOW();
            """
        )

        row_a = ctx.psql_query(
            f"""
            SELECT rendered_markdown
            FROM dalton_data_map_scoped_cache
            WHERE cache_date = {q_date} AND capabilities_fingerprint = {q_fp_a};
            """
        )
        row_b = ctx.psql_query(
            f"""
            SELECT rendered_markdown
            FROM dalton_data_map_scoped_cache
            WHERE cache_date = {q_date} AND capabilities_fingerprint = {q_fp_b};
            """
        )
        assert f"scope A ({run_token})" in row_a, f"Unexpected scoped row A: {row_a!r}"
        assert f"scope B ({run_token})" in row_b, f"Unexpected scoped row B: {row_b!r}"

        scoped_count = ctx.psql_query(
            f"""
            SELECT COUNT(*)
            FROM dalton_data_map_scoped_cache
            WHERE cache_date = {q_date}
              AND capabilities_fingerprint IN ({q_fp_a}, {q_fp_b});
            """
        )
        assert scoped_count == "2", f"Expected 2 isolated scoped cache rows, got {scoped_count!r}"

        ctx.psql_exec(
            f"""
            INSERT INTO dalton_data_map_cache (cache_date, rendered_markdown, payload, updated_at)
            VALUES ({q_date}, {q_legacy_md}, '{{}}'::jsonb, NOW())
            ON CONFLICT (cache_date) DO UPDATE SET
                rendered_markdown = EXCLUDED.rendered_markdown,
                updated_at = NOW();
            """
        )
        legacy_row = ctx.psql_query(
            f"SELECT rendered_markdown FROM dalton_data_map_cache WHERE cache_date = {q_date};"
        )
        assert f"Legacy Cache Row ({run_token})" in legacy_row, (
            f"Unexpected legacy cache row: {legacy_row!r}"
        )
    finally:
        ctx.psql_exec(
            f"""
            DELETE FROM dalton_data_map_scoped_cache
            WHERE cache_date = {q_date}
              AND capabilities_fingerprint IN ({q_fp_a}, {q_fp_b});
            DELETE FROM dalton_data_map_cache
            WHERE cache_date = {q_date};
            """
        )

    log(
        f"1/5 OK (upstream={actual_upstream}, dalton={actual_dalton}, d046 verified, "
        f"isolated writes on {test_date} cleaned up)."
    )


def _parse_container_result(stdout_text: str, stderr_text: str, returncode: int) -> dict[str, Any]:
    lines = [line.strip() for line in stdout_text.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(
            f"Backend container check produced no JSON output (rc={returncode})\n"
            f"STDERR:\n{stderr_text[-2000:]}"
        )
    last_line = lines[-1]
    try:
        payload = json.loads(last_line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Backend container check emitted invalid JSON (rc={returncode}): {exc}\n"
            f"STDOUT:\n{stdout_text[-2000:]}\nSTDERR:\n{stderr_text[-2000:]}"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("checks"), dict):
        raise RuntimeError(
            f"Backend container check returned unexpected payload (rc={returncode}): {payload!r}"
        )
    return payload


def _check_backend_container_core_surface(ctx: _lib.ComposeContext) -> dict[str, Any]:
    """2/5 Run project-code Core verification inside the backend container."""
    log(
        "2/5 Running Core skills, managed-skill prune, prompts, workflows, coverage & share checks "
        "inside backend container..."
    )
    proc = ctx.compose(
        "exec",
        "-T",
        "-e",
        "APP_ENV=core",
        "-e",
        "DALTON_PROFILE=core",
        "backend",
        "uv",
        "run",
        "--no-sync",
        "python",
        "-",
        check=False,
        input_bytes=BACKEND_CONTAINER_CHECK_SCRIPT.encode("utf-8"),
    )
    stdout_text = (proc.stdout or b"").decode("utf-8", errors="replace")
    stderr_text = (proc.stderr or b"").decode("utf-8", errors="replace")
    payload = _parse_container_result(stdout_text, stderr_text, proc.returncode)
    checks: dict[str, Any] = payload["checks"]

    failures: list[str] = []
    for name in EXPECTED_CONTAINER_CHECKS:
        item = checks.get(name)
        if not isinstance(item, dict) or item.get("ok") is not True:
            err = item.get("error") if isinstance(item, dict) else f"missing result ({item!r})"
            failures.append(f"{name}: {err}")

    if payload.get("ok") is not True or failures:
        detail = "\n  - ".join(failures) if failures else json.dumps(checks, ensure_ascii=False)
        raise AssertionError(
            f"Backend container Core checks failed (rc={proc.returncode}):\n  - {detail}"
        )
    assert proc.returncode == 0, (
        f"Backend container check returned non-zero exit code {proc.returncode} despite ok=True\n"
        f"STDERR:\n{stderr_text[-2000:]}"
    )

    sw = checks["skills_and_workflows"]
    si = checks["skills_interception"]
    cov = checks["coverage_and_share_pages"]
    log(
        "2/5 OK "
        f"(disabled_skills={si['disabled_count']}, core_skills={sw['enabled_skills']}, "
        f"workflows={sw['workflows']}, js_compiled={sw['js_compiled']}, "
        f"brand={cov['brand']!r})."
    )
    return payload


def _check_no_everflowinv_in_dist(dist_root: Path) -> int:
    """3/5 Scan host release source tree for zero `everflowinv.com` references."""
    log(f"3/5 Scanning release source tree ({dist_root}) for zero 'everflowinv.com'...")
    scan_roots = (
        dist_root / "src",
        dist_root / "web" / "src",
        dist_root / "docs-site" / "src",
    )
    skills_py_root = dist_root / "plugins" / "dalton" / "skills"
    for req_root in (*scan_roots, skills_py_root):
        assert req_root.is_dir(), f"Expected source directory missing in release tree: {req_root}"

    domain_re = re.compile(r"everflowinv\.com", re.IGNORECASE)
    scanned = 0
    for root in scan_roots:
        for fp in sorted(root.rglob("*")):
            if not fp.is_file() or fp.suffix.lower() not in (
                ".py",
                ".ts",
                ".tsx",
                ".js",
                ".mjs",
                ".md",
                ".mdx",
                ".json",
                ".yaml",
                ".yml",
                ".j2",
                ".astro",
            ):
                continue
            scanned += 1
            text = fp.read_text(encoding="utf-8", errors="ignore")
            if domain_re.search(text):
                raise AssertionError(
                    f"Forbidden internal domain 'everflowinv.com' found in {fp.relative_to(dist_root)}"
                )

    for fp in sorted(skills_py_root.rglob("*.py")):
        scanned += 1
        text = fp.read_text(encoding="utf-8", errors="ignore")
        if domain_re.search(text):
            raise AssertionError(
                f"Forbidden internal domain 'everflowinv.com' found in {fp.relative_to(dist_root)}"
            )

    assert scanned > 0, f"No files scanned under {dist_root}"
    log(f"3/5 OK (scanned {scanned} files across src/, web/src/, docs-site/src/, skills/*.py; 0 hits).")
    return scanned


def _check_frontend_capability_gating(dist_root: Path) -> None:
    """4/5 Verify frontend capability gating source invariants in the host release tree."""
    log("4/5 Checking frontend capability gating invariants (brand/capabilities/Settings/Monitors)...")
    brand_ts = (dist_root / "web" / "src" / "config" / "brand.ts").read_text(encoding="utf-8")
    assert "VITE_DALTON_UI" in brand_ts and "VITE_DALTON_PROFILE" in brand_ts

    caps_ts = (dist_root / "web" / "src" / "hooks" / "useCapabilities.ts").read_text(
        encoding="utf-8"
    )
    assert "/api/v1/dalton/capabilities" in caps_ts
    assert "EVERFLOW_DEFAULT_CAPABILITIES" in caps_ts and "CORE_DEFAULT_CAPABILITIES" in caps_ts

    sections_ts = (dist_root / "web" / "src" / "pages" / "Settings" / "sections.ts").read_text(
        encoding="utf-8"
    )
    for gated in (
        "daltonHomeTeamMemory",
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

    monitors_ts = (
        dist_root / "web" / "src" / "pages" / "Monitors" / "MonitorsList.tsx"
    ).read_text(encoding="utf-8")
    assert "data_jobs" in monitors_ts and "source_kind === 'data_job'" in monitors_ts
    log("4/5 OK (brand.ts, useCapabilities.ts, sections.ts, MonitorsList.tsx verified).")


def _check_docs_site_core_surface(dist_root: Path, *, packaged: bool = True) -> dict[str, int]:
    """5/5 Verify `/help` Core profile filter via Node and optional live `/help` HTTP probe.

    ``packaged=True`` (the release check) expects an exported Core package: the
    Everflow-only docs must already be absent. ``packaged=False`` runs against the
    source tree, where those docs still exist and the profile filter must exclude
    at least five of them.
    """
    log("5/5 Checking /help docs-site Core profile filter via Node (and optional live /help)...")
    node_bin = shutil.which("node")
    if not node_bin:
        raise RuntimeError(
            "node executable not found on PATH; Node.js is required to verify "
            "docs-site/src/integrations/profile-filter.mjs"
        )

    node_script = r"""
import fs from 'node:fs';
import path from 'node:path';
import { isDocIncluded, transformDocSource } from './docs-site/src/integrations/profile-filter.mjs';

const docsRoot = path.resolve('docs-site/src/content/docs');
const forbidden = /alphaengine|guidepoint|researchdata|sales-notes|tmtb|funda|feishu|飞书|weixin|微信|everflowinv\.com|\/help\/everflow\/|what-is-everflow|公司\s*wiki|销售纪要|自动入库|团队对话归档|\/transcribe/i;
const forbiddenAdminIntegrations = /alphaengine|guidepoint|researchdata|sales-notes|tmtb|funda|飞书文档|知识库|群聊绑定|机器人|weixin|微信|everflowinv\.com|\/help\/everflow\/|what-is-everflow|公司\s*wiki|销售纪要|自动入库|团队对话归档|\/transcribe/i;
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
  const activeForbidden = rel === 'get-started/admin-integrations.mdx' ? forbiddenAdminIntegrations : forbidden;
  if (activeForbidden.test(coreText)) {
    errors.push(`${rel}: forbidden proprietary term matched: ${coreText.match(activeForbidden)?.[0]}`);
  }
  if (everflowWord.test(coreText)) {
    errors.push(`${rel}: leftover 'Everflow' mention in Core doc`);
  }
}
const packaged = process.env.DALTON_DOCS_CHECK_PACKAGED === '1';
const forbiddenPackagedPaths = [
  'everflow',
  'explore/deliver-to-feishu.mdx',
  'explore/deliver-to-feishu.md',
];
for (const relBad of packaged ? forbiddenPackagedPaths : []) {
  if (fs.existsSync(path.join(docsRoot, relBad))) {
    errors.push(`Core release package must exclude Everflow/Feishu doc path: docs-site/src/content/docs/${relBad}`);
  }
}
if (!packaged && !(excluded >= 5)) {
  errors.push(`Source tree: expected the Core profile filter to exclude at least 5 Everflow-only docs, got ${excluded}`);
}
if (packaged && excluded > 0) {
  errors.push(`Core release package still contains ${excluded} Everflow-only doc(s) that should be excluded at package time`);
}
if (included === 0) {
  errors.push('Expected Core release package to include at least one public doc');
}
if (errors.length > 0) {
  console.error(errors.join('\n'));
  process.exit(1);
}
console.log(JSON.stringify({ included, excluded }));
"""
    proc = subprocess.run(
        [node_bin, "--input-type=module", "-e", node_script],
        cwd=str(dist_root),
        env={**os.environ, "DALTON_DOCS_CHECK_PACKAGED": "1" if packaged else "0"},
        capture_output=True,
        text=True,
        timeout=NODE_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"docs-site Core profile filter check failed (rc={proc.returncode}):\n"
            f"{proc.stderr or proc.stdout}"
        )
    summary = json.loads(proc.stdout.strip().splitlines()[-1])

    base_url = os.getenv("STANDALONE_BASE_URL", "").strip().rstrip("/")
    live_checked = False
    if base_url:
        resp = _lib.http_request("GET", f"{base_url}/help/", timeout=HTTP_TIMEOUT_S)
        if resp.status == 200:
            cleaned = re.sub(r"/help/get-started/what-is-everflow/?", "", resp.text, flags=re.I)
            _assert_no_proprietary_terms(cleaned, label="Live /help/ HTML")
        ef_resp = _lib.http_request(
            "GET", f"{base_url}/help/everflow/monitors/", timeout=HTTP_TIMEOUT_S
        )
        assert ef_resp.status == 404, (
            f"Expected /help/everflow/monitors/ to return 404 in Core, got {ef_resp.status}"
        )
        live_checked = True

    log(
        f"5/5 OK (docs-site Core filter: included={summary['included']}, "
        f"excluded={summary['excluded']}, live_help_probed={live_checked})."
    )
    return summary


def run_checks() -> None:
    t0 = time.monotonic()
    ctx = _lib.ComposeContext.from_environment()
    dist_root = _DIST_ROOT

    _check_d046_scoped_data_map_cache_db(ctx)
    _check_backend_container_core_surface(ctx)
    _check_no_everflowinv_in_dist(dist_root)
    _check_frontend_capability_gating(dist_root)
    _check_docs_site_core_surface(dist_root)

    elapsed = time.monotonic() - t0
    log(f"ALL CHECKS PASSED in {elapsed:.1f}s.")


if __name__ == "__main__":
    try:
        run_checks()
    except (AssertionError, RuntimeError) as exc:
        print(f"[b3_skills_prompts_ui] FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
