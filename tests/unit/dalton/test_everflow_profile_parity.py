"""Everflow profile parity & full-text snapshot lock (Standalone Core §3.3 #4).

Locks BOTH Everflow entry points — ``APP_ENV="dalton"`` and ``APP_ENV=""`` —
against committed baseline files under
``tests/unit/dalton/snapshots/everflow/``.

Each entry is captured in a **fresh isolated subprocess**
(``tests.unit.dalton._everflow_parity_capture``) with clean environment
variables, disabled ``.env`` auto-loading, empty ``HOME`` (never reads
``~/.ptc-agent/skills``), deterministic offline data-map token estimation
(``max(1, len(md) // 2)`` — zero dependency on ``tiktoken`` vocabulary cache),
and the real production graph builder ``build_ptc_graph_with_session`` (with
``research_writing_enabled=True``, ``tool_summary=None`` against fixed
``MCPToolInfo`` definitions, a fixed PostgreSQL cached data-map record, and a
directly-bound MCP tool).

Default run compares strictly and never writes files. Pass
``--update-everflow-snapshots`` to pytest to regenerate baselines intentionally.
"""

from __future__ import annotations

import difflib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
import tiktoken

from src.dalton.data_map import builder as data_map_builder

REPO_ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT_ROOT = Path(__file__).resolve().parent / "snapshots" / "everflow"

ENTRY_CASES = (
    pytest.param("dalton", "app_env_dalton", id="app_env=dalton"),
    pytest.param("", "app_env_empty", id="app_env=empty"),
)


def _isolated_subprocess_env(app_env: str, empty_home: Path) -> dict[str, str]:
    """Minimal environment for the parity subprocess — blocks all host leaks."""
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(empty_home),
        "TMPDIR": str(empty_home),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "PYTHONPATH": f"{REPO_ROOT}{os.pathsep}{REPO_ROOT / 'src'}",
        "APP_ENV": app_env,
        "AUTH_USER_ID": "local-dev-user",
        "LANGSMITH_TRACING": "false",
        "LANGCHAIN_TRACING_V2": "false",
        "LANGCHAIN_TRACING": "false",
    }
    return env


def _run_isolated_capture(app_env: str, tmp_path: Path) -> dict[str, Any]:
    """Execute ``_everflow_parity_capture`` in a fresh subprocess and load its JSON output."""
    empty_home = tmp_path / f"home_{app_env or 'empty'}"
    empty_home.mkdir(parents=True, exist_ok=True)
    out_file = tmp_path / f"capture_{app_env or 'empty'}.json"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.unit.dalton._everflow_parity_capture",
            "--app-env",
            app_env,
            "--out",
            str(out_file),
        ],
        cwd=str(REPO_ROOT),
        env=_isolated_subprocess_env(app_env, empty_home),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"Isolated Everflow parity capture failed for APP_ENV={app_env!r} "
            f"(rc={proc.returncode}):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return json.loads(out_file.read_text(encoding="utf-8"))


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
    tmp_path: Path,
    request: pytest.FixtureRequest,
    app_env: str,
    entry_slug: str,
) -> None:
    """Lock Everflow tools, skills visibility, MCP servers/baseline, prompts, and <data_map>."""
    update = bool(request.config.getoption("--update-everflow-snapshots"))
    captured = _run_isolated_capture(app_env, tmp_path)

    entry_dir = SNAPSHOT_ROOT / entry_slug
    _assert_or_update_json(entry_dir / "manifest.json", captured["manifest"], update=update)
    _assert_or_update_text(
        entry_dir / "system_prompt_analyst.md",
        captured["system_prompt_analyst"],
        update=update,
    )
    _assert_or_update_text(
        entry_dir / "system_prompt_chief_of_staff.md",
        captured["system_prompt_chief_of_staff"],
        update=update,
    )
    _assert_or_update_text(
        entry_dir / "baseline_mcp_servers_analyst.md",
        captured["baseline_mcp_servers_analyst"],
        update=update,
    )
    _assert_or_update_text(
        entry_dir / "baseline_mcp_servers_chief_of_staff.md",
        captured["baseline_mcp_servers_chief_of_staff"],
        update=update,
    )
    for name, prompt in captured["subagent_prompts_analyst"].items():
        _assert_or_update_text(
            entry_dir / f"subagent_analyst_{name}.md",
            prompt,
            update=update,
        )
    for name, prompt in captured["subagent_prompts_chief_of_staff"].items():
        _assert_or_update_text(
            entry_dir / f"subagent_chief_of_staff_{name}.md",
            prompt,
            update=update,
        )
    _assert_or_update_text(
        entry_dir / "data_map_cached_team_memory_on.md",
        captured["data_map_cached_team_memory_on"],
        update=update,
    )
    _assert_or_update_text(
        entry_dir / "data_map_cached_team_memory_off.md",
        captured["data_map_cached_team_memory_off"],
        update=update,
    )
    _assert_or_update_text(
        entry_dir / "data_map_team_memory_on.md",
        captured["data_map_team_memory_on"],
        update=update,
    )
    _assert_or_update_text(
        entry_dir / "data_map_team_memory_off.md",
        captured["data_map_team_memory_off"],
        update=update,
    )
    _assert_or_update_text(
        SNAPSHOT_ROOT / "skills_51_assembled.txt",
        captured["skills_51_assembled"],
        update=update,
    )
    _assert_or_update_text(
        SNAPSHOT_ROOT / "workflows_6_fulltext.txt",
        captured["workflows_6_fulltext"],
        update=update,
    )


def test_data_map_render_when_tiktoken_vocab_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When tiktoken cannot load o200k_base (offline/no cache), data-map rendering falls back cleanly."""
    failing_get_encoding = create_autospec(
        tiktoken.get_encoding,
        side_effect=OSError("offline: o200k_base vocab unavailable"),
    )
    monkeypatch.setattr(tiktoken, "get_encoding", failing_get_encoding)

    sample = "hello world" * 10
    assert data_map_builder.estimate_data_map_tokens(sample) == max(1, len(sample) // 2)
    assert failing_get_encoding.call_count == 1

    rendered_on = data_map_builder.render_static_data_map_markdown(
        search_api="gemini_grounding",
        x_search_enabled=True,
        team_memory_enabled=True,
    )
    rendered_off = data_map_builder.render_static_data_map_markdown(
        search_api="gemini_grounding",
        x_search_enabled=True,
        team_memory_enabled=False,
    )
    assert rendered_on == (
        SNAPSHOT_ROOT / "app_env_dalton" / "data_map_team_memory_on.md"
    ).read_text(encoding="utf-8")
    assert rendered_off == (
        SNAPSHOT_ROOT / "app_env_dalton" / "data_map_team_memory_off.md"
    ).read_text(encoding="utf-8")
    assert "`conversation_search`" in rendered_on
    assert "`conversation_search`" not in rendered_off


def test_parity_subprocess_ignores_host_transcription_env_and_home_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parent env (DALTON_TRANSCRIPTION_ENABLED=1, AUTH_USER_ID) and ~/.ptc-agent/skills cannot pollute capture."""
    fake_parent_home = tmp_path / "polluted_home"
    polluted_skill = fake_parent_home / ".ptc-agent" / "skills" / "dalton-conventions"
    polluted_skill.mkdir(parents=True)
    (polluted_skill / "SKILL.md").write_text("POLLUTED_HOST_SKILL", encoding="utf-8")

    monkeypatch.setenv("HOME", str(fake_parent_home))
    monkeypatch.setenv("DALTON_TRANSCRIPTION_ENABLED", "1")
    monkeypatch.setenv("AUTH_USER_ID", "host-real-user-uuid")

    captured = _run_isolated_capture("dalton", tmp_path)
    vis = captured["manifest"]["dalton_skills_visibility"]["dalton-transcript-polish"]
    assert vis["hidden"] is True
    assert vis["in_list_skills"] is False
    assert captured["manifest"]["dalton_config"]["pm_user_ids"] == ["local-dev-user"]
    assert "POLLUTED_HOST_SKILL" not in captured["skills_51_assembled"]
