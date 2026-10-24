"""Completion contracts: what a Dalton workflow or multi-step skill owes before it may report done.

A contract is a JSON file validated against ``contract.schema.json`` —
``workflows/<name>/contract.json`` for workflows, ``plugins/dalton/skills/<skill>/contract.json``
for skills. ``dalton_check_done`` renders its path/command templates against the run
params and verifies files, hashes and validators in the workspace; nothing here writes.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = Path(__file__).resolve().parent / "contract.schema.json"
WORKFLOWS_DIR = REPO_ROOT / "workflows"
SKILLS_DIR = REPO_ROOT / "plugins" / "dalton" / "skills"

# ``{key}`` placeholders; keys are plain words so a shell command containing
# brace expansion or JS template literals never collides.
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# ``date`` and ``as_of`` are the two aliases worth having: five of the six
# workflows key their research directory off a date parameter, and the topic
# workflow tolerates its absence — falling back to ``latest`` exactly the way
# its script does.
_DATE_ALIASES = ("as_of", "end", "release_date")
_DATE_KEYS = ("date", "as_of")


class ContractError(ValueError):
    """A contract that cannot be used — missing, unparseable, or invalid."""


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_contract(doc: Any) -> list[str]:
    """Schema-validate one contract document; empty list means valid."""
    validator = Draft202012Validator(load_schema())
    return [
        f"{'/'.join(str(p) for p in error.absolute_path)}: {error.message}"
        for error in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    ]


def workflow_contract_path(name: str) -> Path:
    return WORKFLOWS_DIR / name / "contract.json"


def skill_contract_path(name: str) -> Path:
    # Accept both the bare skill name and its directory spelling.
    directory = name if name.startswith("dalton-") else f"dalton-{name}"
    return SKILLS_DIR / directory / "contract.json"


def load_workflow_contract(name: str) -> dict[str, Any]:
    return _load_contract_at(workflow_contract_path(name), expected_name=name)


def load_skill_contract(name: str) -> dict[str, Any]:
    # The contract names its skill by directory, whichever spelling the caller used.
    path = skill_contract_path(name)
    return _load_contract_at(path, expected_name=path.parent.name)


def load_contract(name: str) -> dict[str, Any]:
    """Resolve ``name`` as a workflow first, then as a skill.

    Workflow names and skill names never collide in practice (workflows are
    bare slugs, skills carry the ``dalton-`` prefix), so order is cosmetic.
    """
    if workflow_contract_path(name).is_file():
        return load_workflow_contract(name)
    if skill_contract_path(name).is_file():
        return load_skill_contract(name)
    raise ContractError(
        f"找不到 '{name}' 的完成契约（尝试过 {workflow_contract_path(name)} 与 "
        f"{skill_contract_path(name)}）。"
    )


def list_workflow_contracts() -> list[str]:
    if not WORKFLOWS_DIR.is_dir():
        return []
    return sorted(p.parent.name for p in WORKFLOWS_DIR.glob("*/contract.json"))


def list_skill_contracts() -> list[str]:
    if not SKILLS_DIR.is_dir():
        return []
    return sorted(p.parent.name for p in SKILLS_DIR.glob("*/contract.json"))


def _load_contract_at(path: Path, *, expected_name: str) -> dict[str, Any]:
    if not path.is_file():
        raise ContractError(f"完成契约不存在：{path}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ContractError(f"完成契约不可读（{path}）：{error}") from error
    errors = validate_contract(doc)
    if errors:
        raise ContractError(
            f"完成契约 {path} 不符合 schema：{'；'.join(errors[:5])}"
        )
    if doc["name"] != expected_name:
        raise ContractError(
            f"完成契约 {path} 的 name 是 '{doc['name']}'，与目录名 '{expected_name}' 不一致。"
        )
    return doc


def render_template(template: str, params: dict[str, Any]) -> str:
    """Substitute ``{key}`` placeholders from ``params`` (raw values — for
    paths and markers, never for commands; see ``render_command``).

    Raises ``ContractError`` naming the missing key — a half-rendered path
    would silently check the wrong file.
    """

    def _substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in params and params[key] is not None:
            return str(params[key])
        if key in _DATE_KEYS:
            for alias in _DATE_ALIASES:
                if params.get(alias):
                    return str(params[alias])
            # The one workflow with an optional date names its directory
            # ``latest`` when none was given; the contract follows it.
            return "latest"
        raise ContractError(
            f"模板 '{template}' 需要参数 '{key}'，但调用参数里没有。"
        )

    return _PLACEHOLDER_RE.sub(_substitute, template)


def render_command(template: str, params: dict[str, Any]) -> str:
    """Render a validator command with every substituted value shell-quoted.

    The command re-runs in the sandbox; an unquoted parameter is an injection
    seam (``$(touch x)`` in a param would execute). Only the fixed command
    text stays as written — the operator-authored contract — while every
    ``{param}`` value arrives as one shell word.
    """

    def _substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in params and params[key] is not None:
            return shlex.quote(str(params[key]))
        if key in _DATE_KEYS:
            for alias in _DATE_ALIASES:
                if params.get(alias):
                    return shlex.quote(str(params[alias]))
            return shlex.quote("latest")
        raise ContractError(
            f"校验命令 '{template}' 需要参数 '{key}'，但调用参数里没有。"
        )

    return _PLACEHOLDER_RE.sub(_substitute, template)


def missing_params(contract: dict[str, Any], params: dict[str, Any]) -> list[str]:
    """Declared-required params that the caller did not supply."""
    return [key for key in contract.get("params_required", []) if not params.get(key)]


def check_draft_path(contract: dict[str, Any], params: dict[str, Any]) -> str | None:
    """The draft path the closing check_draft rule targets, if declared."""
    rule = (contract.get("closing") or {}).get("check_draft")
    if not rule:
        return None
    return render_template(rule["draft"], params)
