"""RunWorkflow unlock: Dalton orchestration skills unlock it on their own load,
and a SKILL.md read through Bash / ExecuteCode auto-loads like a Read does.

Regression: an analyst read run-workflow's SKILL.md with Bash ``cat``, never
got RunWorkflow, and hand-dispatched Task subagents instead.
"""

from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from langchain.agents.middleware.types import ModelRequest, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from ptc_agent.agent.middleware.skills.middleware import SkillsMiddleware
from ptc_agent.agent.middleware.skills.registry import (
    SKILL_REGISTRY,
    get_skill_registry,
    resolve_disabled_skills,
)
from src.dalton.skill_reads import match_skills_from_shell_read

TOOL = "RunWorkflow"
WORKFLOW_SKILLS = (
    "dalton-initial-coverage",
    "dalton-full-coverage",
    "dalton-bullbear",
    "dalton-earnings-deep",
    "dalton-weekly-review",
    "dalton-topic-research",
)
RW_MD = ".agents/skills/run-workflow/SKILL.md"


def _visible_tools(middleware: SkillsMiddleware, loaded: list[str]) -> list[str]:
    request = create_autospec(ModelRequest, instance=True)
    request.tools = [SimpleNamespace(name=TOOL), SimpleNamespace(name="Bash")]
    request.state = {"loaded_skills": loaded}
    request.override.side_effect = lambda **kw: SimpleNamespace(**kw)
    middleware._inject_manifest = False
    return [t.name for t in middleware._filter_tools(request).tools]


@pytest.mark.parametrize("name", WORKFLOW_SKILLS)
def test_workflow_skill_requires_run_workflow(name):
    assert "run-workflow" in SKILL_REGISTRY[name].requires


@pytest.mark.parametrize("name", WORKFLOW_SKILLS)
def test_loading_workflow_skill_unlocks_run_workflow(name):
    middleware = SkillsMiddleware()
    assert _visible_tools(middleware, []) == ["Bash"]
    assert _visible_tools(middleware, [name]) == [TOOL, "Bash"]


def test_non_workflow_skill_does_not_unlock():
    assert _visible_tools(SkillsMiddleware(), ["dalton-stock-move"]) == ["Bash"]


def test_build_without_run_workflow_unlocks_nothing():
    build = get_skill_registry()
    build.pop("run-workflow")
    middleware = SkillsMiddleware(skill_registry=build)
    assert TOOL not in middleware._tool_to_skills


def test_run_workflow_survives_its_own_disable_while_dependents_on():
    assert "run-workflow" not in resolve_disabled_skills({"run-workflow"})


@pytest.mark.parametrize(
    "command",
    [
        f"cat {RW_MD}",
        f"cat /home/daytona/ws/{RW_MD} | head -200",
        f"sed -n '1,200p' ./{RW_MD}",
        f"cd /tmp && head -n 80 {RW_MD}",
        f"less < {RW_MD}",
        f"echo start\ncat {RW_MD}",
        f"cat {RW_MD} 2>/dev/null > /tmp/copy.md",
    ],
)
def test_bash_read_matches(command):
    mapping = SkillsMiddleware()._skill_md_to_name
    assert match_skills_from_shell_read("Bash", {"command": command}, mapping) == [
        "run-workflow"
    ]


@pytest.mark.parametrize(
    "command",
    [
        "ls .agents/skills/run-workflow/",
        f"ls -la {RW_MD}",
        f"grep -n RunWorkflow {RW_MD}",
        "cat .agents/skills/*/SKILL.md",
        "cat skills/run-workflow/SKILL.md",
        "cat .agents/skills/not-a-skill/SKILL.md",
        f"echo {RW_MD}",
        "cat 'unbalanced",
        f"cat /dev/null > {RW_MD}",
        f"cat notes.md >> {RW_MD}",
        f"cat notes.md 2> {RW_MD}",
        f"cat notes.md 2>{RW_MD}",
        f"cat > out.md <<'EOF'\ncat {RW_MD}\nEOF",
        f"cat <<EOF > out.md\n{RW_MD}\nEOF",
        f"echo start\nls {RW_MD}",
    ],
)
def test_bash_non_reads_do_not_match(command):
    mapping = SkillsMiddleware()._skill_md_to_name
    assert match_skills_from_shell_read("Bash", {"command": command}, mapping) == []


def test_bash_reads_two_skills():
    mapping = SkillsMiddleware()._skill_md_to_name
    command = (
        "cat .agents/skills/dalton-initial-coverage/SKILL.md; "
        f"cat {RW_MD}"
    )
    assert match_skills_from_shell_read("Bash", {"command": command}, mapping) == [
        "dalton-initial-coverage",
        "run-workflow",
    ]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (f"print(open('{RW_MD}').read())", ["run-workflow"]),
        (
            f"from pathlib import Path\nprint(Path('{RW_MD}').read_text())",
            ["run-workflow"],
        ),
        (f"print(open(file='{RW_MD}').read())", ["run-workflow"]),
        (f"print(open('{RW_MD}', mode='rb').read())", ["run-workflow"]),
        (f"import io\nprint(io.open('{RW_MD}', 'r').read())", ["run-workflow"]),
        (f"from pathlib import Path\nPath('{RW_MD}').open().read()", ["run-workflow"]),
        (f"open('{RW_MD}', 'w').write('x')", []),
        (f"open('{RW_MD}', 'a').write('x')", []),
        (f"open('{RW_MD}', 'r+').read()", []),
        (f"open('{RW_MD}', mode='x')", []),
        (f"m = 'r'\nopen('{RW_MD}', m)", []),
        (f"from pathlib import Path\nPath('{RW_MD}').open('w')", []),
        (f"import os\nprint(os.path.exists('{RW_MD}'))", []),
        (f"p = '{RW_MD}'\nprint(open(p).read())", []),
        ("this is not python (", []),
    ],
)
def test_execute_code_reads(code, expected):
    mapping = SkillsMiddleware()._skill_md_to_name
    assert match_skills_from_shell_read("ExecuteCode", {"code": code}, mapping) == expected


def test_other_tools_ignored():
    mapping = SkillsMiddleware()._skill_md_to_name
    assert match_skills_from_shell_read("Grep", {"command": f"cat {RW_MD}"}, mapping) == []


async def _call(
    middleware: SkillsMiddleware,
    name: str,
    args: dict,
    content="body",
    status="success",
):
    request = create_autospec(ToolCallRequest, instance=True)
    request.tool_call = {"name": name, "args": args, "id": "call-1"}
    message = ToolMessage(content=content, tool_call_id="call-1", status=status)

    async def handler(_request):
        return message

    return await middleware.awrap_tool_call(request, handler), message


@pytest.mark.asyncio
async def test_bash_cat_of_run_workflow_skill_loads_it():
    middleware = SkillsMiddleware()
    result, message = await _call(middleware, "Bash", {"command": f"cat {RW_MD}"})
    assert isinstance(result, Command)
    assert result.update["loaded_skills"] == ["run-workflow"]
    assert result.update["messages"] == [message]
    assert _visible_tools(middleware, result.update["loaded_skills"]) == [TOOL, "Bash"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "args", "content", "status"),
    [
        ("Bash", {"command": f"cat {RW_MD}"}, "ERROR: Command failed (exit code 1)\nno such file", "success"),
        ("Bash", {"command": f"cat {RW_MD}"}, "body", "error"),
        ("Bash", {"command": f"cat {RW_MD}"}, [{"type": "text", "text": "ERROR: boom"}], "success"),
        ("ExecuteCode", {"code": f"open('{RW_MD}').read()"}, "ERROR\nFileNotFoundError", "success"),
    ],
)
async def test_failed_shell_read_does_not_load(name, args, content, status):
    middleware = SkillsMiddleware()
    result, message = await _call(middleware, name, args, content, status)
    assert result is message


@pytest.mark.asyncio
async def test_execute_code_read_loads():
    middleware = SkillsMiddleware()
    result, _ = await _call(
        middleware, "ExecuteCode", {"code": f"print(open('{RW_MD}').read())"}, "SUCCESS\nbody"
    )
    assert isinstance(result, Command)
    assert result.update["loaded_skills"] == ["run-workflow"]


@pytest.mark.asyncio
async def test_bash_ls_does_not_load():
    middleware = SkillsMiddleware()
    result, message = await _call(
        middleware, "Bash", {"command": "ls .agents/skills/run-workflow"}
    )
    assert result is message


@pytest.mark.asyncio
async def test_read_tool_still_loads():
    middleware = SkillsMiddleware()
    result, _ = await _call(middleware, "Read", {"file_path": RW_MD})
    assert isinstance(result, Command)
    assert result.update["loaded_skills"] == ["run-workflow"]


def test_manifest_tells_agent_to_use_read_tool():
    manifest = SkillsMiddleware().build_manifest({})
    assert "with the Read tool" in manifest
