"""Everflow profile parity & full-text snapshot lock (Standalone Core §3.3 #4).

Locks BOTH Everflow entry points — ``APP_ENV="dalton"`` and ``APP_ENV=""`` —
against committed baseline files under
``tests/unit/dalton/snapshots/everflow/``:

1. Complete tool-name sets for main agent (``analyst``), Research Director
   (``chief_of_staff``), and every compiled subagent.
2. Visibility of all 51 Dalton skills (``plugins/dalton/skills/*``), merged MCP
   server list, and effective ``src/dalton/dalton.yaml`` switches from
   ``get_dalton_config()`` (plus infrastructure / agent config switches).
3. Verbatim full-text snapshots of:
   - Main agent (``analyst`` & ``chief_of_staff``) and subagent rendered system
     prompts;
   - All 51 Dalton skills' ``SKILL.md`` + ``references/**`` assembled text;
   - All 6 ``workflows/**`` files (``workflow.js`` + ``contract.json``);
   - Rendered ``<data_map>`` markdown (team memory on and off).

Default run compares strictly and never writes files. Pass
``--update-everflow-snapshots`` to pytest to regenerate baselines intentionally.
"""

from __future__ import annotations

import difflib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
from langchain.agents import create_agent as langchain_create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

from ptc_agent.agent.agent import PTCAgent, task_middleware as real_task_middleware
from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
    PrebuiltWorkflowRegistry,
)
from ptc_agent.agent.middleware.compaction import compact as compact_mod
from ptc_agent.agent.middleware.skills.content import load_skill_content, resolve_skill_dir
from ptc_agent.agent.middleware.skills.dalton_registry import (
    DALTON_SKILL_DEFINITIONS,
    DALTON_SKILLS_DIR,
)
from ptc_agent.agent.middleware.skills.registry import (
    get_skill_registry,
    list_skills,
    skills_outside_sandbox,
)
from ptc_agent.agent.prompts.loader import init_loader, reset_loader
from ptc_agent.config.agent import AgentConfig
from ptc_agent.config.file_utils import load_yaml_config
from ptc_agent.config.loaders import load_from_dict
from ptc_agent.core.mcp_registry import MCPRegistry
from ptc_agent.core.paths import SandboxLayout, WorkspaceLayout
from ptc_agent.core.sandbox.livefs_mount import MountHandle
from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
from src.config.core import clear_config_cache, load_infrastructure_config
from src.dalton.data_map import resolve_prompt_data_map, set_turn_data_map
from src.dalton.settings import get_dalton_config
from src.tools.web.fetch import get_fetch_chain
from src.tools.web.manifest import get_web_providers

_ORIG_GET_LLM_CLIENT = AgentConfig.get_llm_client
_ORIG_COMPACT_CREATE_LLM = compact_mod.create_llm

REPO_ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT_ROOT = Path(__file__).resolve().parent / "snapshots" / "everflow"

FIXED_UTC = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)
FIXED_THREAD_ID = "11111111-1111-1111-1111-111111111111"
FIXED_USER_ID = "00000000-0000-0000-0000-000000000001"
FIXED_SANDBOX_ROOT = "/home/workspace"

# Env vars cleared so host .env or developer shell never leaks into parity runs.
_ISOLATED_ENV_VARS: tuple[str, ...] = (
    "DALTON_PROFILE",
    "DALTON_ENABLED",
    "DALTON_COLLEAGUE_DISPATCH_ENABLED",
    "DALTON_COMPUTER_CLI_TOKENS_ENABLED",
    "DALTON_ARTIFACT_PUBLISHING_ENABLED",
    "DALTON_PROBLEM_REPORTS_ENABLED",
    "DALTON_COVERAGE_UNITS_ENABLED",
    "DALTON_DATA_JOBS_ENABLED",
    "DALTON_TRANSCRIPTION_ENABLED",
    "DALTON_PM_USER_IDS",
    "DALTON_BUDGET_GATE",
    "DALTON_AUTH_MODE",
    "DALTON_DISABLED_PROVIDERS",
    "DALTON_ASSIST_FALLBACK_MODEL",
    "DALTON_RESEARCHDATA_URL",
    "DALTON_BRIDGE_URL",
    "DALTON_BRIDGE_TOKEN",
    "DALTON_BRIDGE_ADMIN_TOKEN",
    "INTERNAL_SERVICE_TOKEN",
    "CHANNEL_GATEWAY_URL",
    "SANDBOX_PROVIDER",
    "DOCKER_SANDBOX_IMAGE",
    "DOCKER_SANDBOX_DEV_MODE",
    "DOCKER_SANDBOX_HOST_DIR",
    "DOCKER_SANDBOX_VOLUMES",
    "PTC_CONFIG_FILE",
    "BRAND_NAME",
    "TELEGRAM_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "WHATSAPP_ACCESS_TOKEN",
    "FEISHU_APP_ID",
)

ENTRY_CASES = (
    pytest.param("dalton", "app_env_dalton", id="app_env=dalton"),
    pytest.param("", "app_env_empty", id="app_env=empty"),
)


class _FixedDatetime(datetime):
    """Deterministic ``datetime`` replacement returning ``FIXED_UTC`` from ``now()``."""

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        if tz is None:
            return FIXED_UTC.replace(tzinfo=None)
        return FIXED_UTC.astimezone(tz)


def _normalize_repo_path(value: Any) -> Any:
    """Replace absolute repository root prefix with ``<REPO_ROOT>`` for portability."""
    root_str = str(REPO_ROOT)
    if isinstance(value, str):
        return value.replace(root_str, "<REPO_ROOT>")
    if isinstance(value, list):
        return [_normalize_repo_path(item) for item in value]
    if isinstance(value, dict):
        return {k: _normalize_repo_path(v) for k, v in value.items()}
    return value


def _configure_entry_env(monkeypatch: pytest.MonkeyPatch, app_env: str) -> None:
    """Pin environment and reset all config/prompt/data-map caches for an entry point."""
    for var in _ISOLATED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APP_ENV", app_env)

    clear_config_cache()
    get_dalton_config.cache_clear()
    get_web_providers.cache_clear()
    set_turn_data_map(None)
    reset_loader()
    init_loader(session_start_time=FIXED_UTC)


def _build_agent_config_for_entry() -> AgentConfig:
    """Load real ``agent_config.yaml`` (with active overlay) into an ``AgentConfig``."""
    raw = load_yaml_config(str(REPO_ROOT / "agent_config.yaml"), use_cache=False)
    cfg = load_from_dict(raw)
    cfg.config_file_dir = REPO_ROOT
    return cfg


def _make_sandbox_double(config: AgentConfig) -> Any:
    """Create a deterministic ``PTCSandbox`` double via ``create_autospec``."""
    sandbox = create_autospec(PTCSandbox, instance=True)
    sandbox.config = config.to_core_config()
    sandbox.working_dir = FIXED_SANDBOX_ROOT
    sandbox.layout = SandboxLayout(root=FIXED_SANDBOX_ROOT)
    sandbox.workspace.return_value = WorkspaceLayout(root=FIXED_SANDBOX_ROOT)
    sandbox.livefs = create_autospec(MountHandle, instance=True)
    sandbox.mounts_livefs = True
    sandbox.history = None
    sandbox.skills_manifest = None
    return sandbox


def _make_mcp_registry_double() -> Any:
    """Create a deterministic ``MCPRegistry`` double via ``create_autospec``."""
    registry = create_autospec(MCPRegistry, instance=True)
    registry.connectors = {}
    registry.tools_by_server = {}
    registry.get_all_tools.return_value = {}
    return registry


def _capture_agent_surface(
    monkeypatch: pytest.MonkeyPatch,
    config: AgentConfig,
    *,
    role: str,
) -> dict[str, Any]:
    """Build ``PTCAgent`` for ``role`` and capture main/subagent tools and prompts."""
    import ptc_agent.agent.agent as agent_mod

    fake_llm = GenericFakeChatModel(messages=iter([]))
    captured_main: list[dict[str, Any]] = []
    captured_subagents: list[list[dict[str, Any]]] = []

    def _capture_create_agent(_model: Any, **kwargs: Any) -> Any:
        captured_main.append(kwargs)
        graph = create_autospec(langchain_create_agent(fake_llm, tools=[]), instance=True)
        graph.with_config.return_value = graph
        return graph

    def _spy_task_middleware(subagents: Any, *args: Any, **kwargs: Any) -> Any:
        captured_subagents.append(list(subagents))
        return real_task_middleware(subagents, *args, **kwargs)

    fake_get_llm = create_autospec(_ORIG_GET_LLM_CLIENT, return_value=fake_llm)
    fake_create_llm = create_autospec(_ORIG_COMPACT_CREATE_LLM, return_value=fake_llm)
    fake_create_agent = create_autospec(
        langchain_create_agent, side_effect=_capture_create_agent
    )
    fake_task_mw = create_autospec(
        real_task_middleware, side_effect=_spy_task_middleware
    )

    sandbox = _make_sandbox_double(config)
    mcp_registry = _make_mcp_registry_double()

    monkeypatch.setattr(agent_mod, "datetime", _FixedDatetime)
    monkeypatch.setattr(AgentConfig, "get_llm_client", fake_get_llm)
    monkeypatch.setattr(compact_mod, "create_llm", fake_create_llm)
    monkeypatch.setattr(agent_mod, "create_agent", fake_create_agent)
    monkeypatch.setattr(agent_mod, "task_middleware", fake_task_mw)

    PTCAgent(config).create_agent(
        llm=fake_llm,
        thread_id=FIXED_THREAD_ID,
        sandbox=sandbox,
        mcp_registry=mcp_registry,
        user_id=FIXED_USER_ID,
        tool_summary="",
        role=role,  # type: ignore[arg-type]
    )

    assert len(captured_main) == 1
    assert len(captured_subagents) == 1
    main_kwargs = captured_main[0]
    subagent_specs = captured_subagents[0]

    main_prompt = str(main_kwargs["system_prompt"])
    assert "MagicMock" not in main_prompt and "AsyncMock" not in main_prompt

    subagent_tools: dict[str, list[str]] = {}
    subagent_prompts: dict[str, str] = {}
    for spec in sorted(subagent_specs, key=lambda s: str(s["name"])):
        name = str(spec["name"])
        prompt = str(spec["system_prompt"])
        assert "MagicMock" not in prompt and "AsyncMock" not in prompt
        subagent_tools[name] = sorted(t.name for t in spec.get("tools") or [])
        subagent_prompts[name] = prompt

    return {
        "main_tools": sorted(t.name for t in main_kwargs.get("tools") or []),
        "main_prompt": main_prompt,
        "subagent_tools": subagent_tools,
        "subagent_prompts": subagent_prompts,
    }


def _collect_dalton_skills_visibility() -> dict[str, dict[str, Any]]:
    """Collect visibility metadata for all 51 Dalton skills."""
    disk_skills = sorted(p.name for p in DALTON_SKILLS_DIR.iterdir() if p.is_dir())
    assert len(disk_skills) == 51, f"Expected 51 Dalton skills on disk, got {len(disk_skills)}"
    assert sorted(DALTON_SKILL_DEFINITIONS) == disk_skills

    enabled_registry = get_skill_registry()
    listed_names = {entry["name"] for entry in list_skills()}
    outside = skills_outside_sandbox()

    visibility: dict[str, dict[str, Any]] = {}
    for name in disk_skills:
        defn = DALTON_SKILL_DEFINITIONS[name]
        visibility[name] = {
            "enabled_in_registry": name in enabled_registry,
            "hidden": bool(defn.hidden),
            "in_list_skills": name in listed_names,
            "in_sandbox": name not in outside,
            "command": defn.command,
            "requires": list(defn.requires),
        }
    return visibility


def _assemble_all_dalton_skills_fulltext() -> str:
    """Assemble full text of all 51 Dalton skills' ``SKILL.md`` and ``references/**``."""
    disk_skills = sorted(p.name for p in DALTON_SKILLS_DIR.iterdir() if p.is_dir())
    assert len(disk_skills) == 51

    sections: list[str] = []
    for name in disk_skills:
        skill_dir = resolve_skill_dir(name)
        assert skill_dir is not None, f"Could not resolve skill dir for {name}"
        skill_md = load_skill_content(name)
        assert skill_md is not None, f"Could not load SKILL.md for {name}"

        parts = [f"===== SKILL: {name} =====", f"--- FILE: {name}/SKILL.md ---", skill_md]
        refs_dir = skill_dir / "references"
        if refs_dir.is_dir():
            for ref_file in sorted(p for p in refs_dir.rglob("*") if p.is_file()):
                rel = ref_file.relative_to(skill_dir).as_posix()
                parts.append(f"--- FILE: {name}/{rel} ---")
                parts.append(ref_file.read_text(encoding="utf-8"))
        sections.append("\n".join(parts))
    return "\n\n".join(sections) + "\n"


def _assemble_all_workflows_fulltext() -> str:
    """Assemble full text of all repository-shipped ``workflows/**`` files."""
    registry = PrebuiltWorkflowRegistry(root=REPO_ROOT)
    workflow_names = registry.names()
    assert len(workflow_names) == 6, f"Expected 6 prebuilt workflows, got {workflow_names}"

    workflows_dir = REPO_ROOT / "workflows"
    sections: list[str] = []
    for name in workflow_names:
        wf_dir = workflows_dir / name
        script_text = registry.get(name)
        assert script_text is not None
        parts = [f"===== WORKFLOW: {name} ====="]
        for path in sorted(p for p in wf_dir.rglob("*") if p.is_file()):
            rel = path.relative_to(workflows_dir).as_posix()
            text = script_text if path.name == "workflow.js" else path.read_text(encoding="utf-8")
            parts.append(f"--- FILE: {rel} ---")
            parts.append(text)
        sections.append("\n".join(parts))
    return "\n\n".join(sections) + "\n"


def _collect_mcp_servers(config: AgentConfig) -> list[dict[str, Any]]:
    """Serialize the merged MCP server list deterministically."""
    servers: list[dict[str, Any]] = []
    for srv in config.mcp.servers:
        dump = srv.model_dump(mode="json")
        servers.append(
            {
                "name": dump["name"],
                "enabled": dump["enabled"],
                "transport": dump["transport"],
                "tool_exposure_mode": dump["tool_exposure_mode"],
                "call_timeout_s": dump.get("call_timeout_s"),
                "source": dump.get("source"),
                "command": _normalize_repo_path(dump.get("command")),
                "args": _normalize_repo_path(dump.get("args")),
                "url": dump.get("url"),
                "env_keys": sorted((dump.get("env") or {}).keys()),
                "header_keys": sorted((dump.get("headers") or {}).keys()),
            }
        )
    return servers


def _collect_infra_switches() -> dict[str, Any]:
    """Collect key effective infrastructure switches from ``config.yaml`` (+ overlay)."""
    infra = load_infrastructure_config(str(REPO_ROOT / "config.yaml"))
    return {
        "market_data_providers": [p.model_dump(mode="json") for p in infra.market_data.providers],
        "news_data_providers": [p.model_dump(mode="json") for p in infra.news_data.providers],
        "features": {k: v.model_dump(mode="json") for k, v in sorted(infra.features.items())},
        "mcp_descriptions_enabled": infra.mcp_descriptions.enabled,
        "workflow_child_timeout": infra.workflow.child_timeout,
    }


def _collect_agent_config_switches(config: AgentConfig) -> dict[str, Any]:
    """Collect key effective agent config switches from ``agent_config.yaml`` (+ overlay)."""
    return {
        "llm": {
            "name": config.llm.name,
            "background": config.llm.background,
            "compaction": config.llm.compaction,
            "fetch": config.llm.fetch,
            "fallback": list(config.llm.fallback),
        },
        "search_api": config.search_api,
        "x_search_enabled": bool(config.x_search_enabled),
        "fetch_chain": get_fetch_chain(),
        "sandbox_provider": config.sandbox.provider,
        "sandbox_offered_providers": list(config.sandbox.offered_providers),
        "subagents_enabled": list(config.subagents.enabled),
        "subagents_definition_models": {
            name: defn.model
            for name, defn in sorted(config.subagents.definitions.items())
            if defn.model is not None
        },
    }


def _assert_or_update_text(
    path: Path,
    actual: str,
    *,
    update: bool,
) -> None:
    """Compare ``actual`` verbatim against snapshot ``path`` (or write when ``update``)."""
    if update:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        return

    if not path.is_file():
        pytest.fail(
            f"Missing Everflow baseline snapshot: {path.relative_to(REPO_ROOT)}. "
            "Run pytest with --update-everflow-snapshots to generate it."
        )

    expected = path.read_text(encoding="utf-8")
    if actual != expected:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=f"baseline/{path.relative_to(SNAPSHOT_ROOT)}",
                tofile=f"actual/{path.relative_to(SNAPSHOT_ROOT)}",
                n=3,
            )
        )
        max_chars = 6000
        if len(diff) > max_chars:
            diff = diff[:max_chars] + f"\n... (diff truncated, total {len(diff)} chars)"
        pytest.fail(
            f"Everflow parity snapshot mismatch in {path.relative_to(REPO_ROOT)}:\n{diff}"
        )


def _assert_or_update_json(
    path: Path,
    actual_obj: dict[str, Any],
    *,
    update: bool,
) -> None:
    actual = json.dumps(actual_obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    _assert_or_update_text(path, actual, update=update)


@pytest.mark.parametrize(("app_env", "entry_slug"), ENTRY_CASES)
def test_everflow_profile_manifest_and_prompts_parity(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    app_env: str,
    entry_slug: str,
) -> None:
    """Lock Everflow tools, skills visibility, MCP servers, defaults, prompts, and <data_map>."""
    update = bool(request.config.getoption("--update-everflow-snapshots"))
    _configure_entry_env(monkeypatch, app_env)

    config = _build_agent_config_for_entry()
    analyst_surface = _capture_agent_surface(monkeypatch, config, role="analyst")
    cos_surface = _capture_agent_surface(monkeypatch, config, role="chief_of_staff")

    data_map_on = resolve_prompt_data_map(team_memory_enabled=True)
    data_map_off = resolve_prompt_data_map(team_memory_enabled=False)

    manifest = {
        "app_env": app_env,
        "main_tools_analyst": analyst_surface["main_tools"],
        "main_tools_chief_of_staff": cos_surface["main_tools"],
        "subagent_tools_analyst": analyst_surface["subagent_tools"],
        "subagent_tools_chief_of_staff": cos_surface["subagent_tools"],
        "dalton_skills_visibility": _collect_dalton_skills_visibility(),
        "mcp_servers": _collect_mcp_servers(config),
        "dalton_config": get_dalton_config().model_dump(mode="json"),
        "infra_switches": _collect_infra_switches(),
        "agent_config_switches": _collect_agent_config_switches(config),
    }

    entry_dir = SNAPSHOT_ROOT / entry_slug
    _assert_or_update_json(entry_dir / "manifest.json", manifest, update=update)
    _assert_or_update_text(
        entry_dir / "system_prompt_analyst.md",
        analyst_surface["main_prompt"],
        update=update,
    )
    _assert_or_update_text(
        entry_dir / "system_prompt_chief_of_staff.md",
        cos_surface["main_prompt"],
        update=update,
    )
    for name, prompt in analyst_surface["subagent_prompts"].items():
        _assert_or_update_text(
            entry_dir / f"subagent_analyst_{name}.md",
            prompt,
            update=update,
        )
    for name, prompt in cos_surface["subagent_prompts"].items():
        _assert_or_update_text(
            entry_dir / f"subagent_chief_of_staff_{name}.md",
            prompt,
            update=update,
        )
    _assert_or_update_text(
        entry_dir / "data_map_team_memory_on.md",
        data_map_on,
        update=update,
    )
    _assert_or_update_text(
        entry_dir / "data_map_team_memory_off.md",
        data_map_off,
        update=update,
    )


@pytest.mark.parametrize(("app_env", "_entry_slug"), ENTRY_CASES)
def test_everflow_skills_and_workflows_fulltext_parity(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    app_env: str,
    _entry_slug: str,
) -> None:
    """Lock verbatim assembled full text of all 51 Dalton skills (+ references) and 6 workflows."""
    update = bool(request.config.getoption("--update-everflow-snapshots"))
    _configure_entry_env(monkeypatch, app_env)

    skills_fulltext = _assemble_all_dalton_skills_fulltext()
    workflows_fulltext = _assemble_all_workflows_fulltext()

    _assert_or_update_text(
        SNAPSHOT_ROOT / "skills_51_assembled.txt",
        skills_fulltext,
        update=update,
    )
    _assert_or_update_text(
        SNAPSHOT_ROOT / "workflows_6_fulltext.txt",
        workflows_fulltext,
        update=update,
    )
