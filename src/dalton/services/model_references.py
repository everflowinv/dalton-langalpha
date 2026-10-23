"""Where every runtime model reference lives, collected one source at a time.

Each collector raises `ReferenceSourceUnavailable` instead of swallowing its
errors: retiring a model must fail closed (an unreadable source could be hiding
the one reference that matters), while display paths catch the error and
degrade to fewer sources. `agent_config_refs` reads the merged raw YAML rather
than the loaded `AgentConfig`, because the loader drops `llm:` keys it has no
field for — a model named in the file is a reference even before the loader
learns the key. The same goes for `src/dalton/dalton.yaml`'s raw `llm:` block,
which pydantic's `DaltonConfig` drops but `resolve_assist_model` still reads.
"""

from __future__ import annotations

import logging
from typing import Any

import yaml
from pydantic import BaseModel

from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)


class ReferenceSourceUnavailable(RuntimeError):
    """A reference source could not be read; retiring must not proceed."""

    def __init__(self, source: str, cause: BaseException) -> None:
        super().__init__(f"{source}: {type(cause).__name__}: {cause}")
        self.source = source
        self.cause = cause


def _add(refs: dict[str, list[str]], model: Any, source: str) -> None:
    if isinstance(model, str) and model.strip():
        locations = refs.setdefault(model.strip(), [])
        if source not in locations:
            locations.append(source)


def _collect_llm_block(llm: Any, prefix: str, refs: dict[str, list[str]]) -> None:
    """Every key of an `llm:` block — a str names one model, a list a chain.

    Generic on purpose: a key added to the YAML is covered before any code here
    learns about it (the loader itself may never grow a field for it).
    """
    if not isinstance(llm, dict):
        return
    for key, value in llm.items():
        if isinstance(value, str):
            _add(refs, value, f"{prefix}.{key}")
        elif isinstance(value, list):
            for item in value:
                _add(refs, item, f"{prefix}.{key}")


def agent_config_refs() -> dict[str, list[str]]:
    """`llm:` keys and subagent definitions from agent_config, two reads unioned.

    One is the merge startup role policy uses
    (`role_policy._load_default_agent_config`): base file, APP_ENV overlay,
    plus `agent_config.dalton.yaml` when APP_ENV is unset. The other is
    `REPO_ROOT/agent_config.dalton.yaml` read directly, because
    `resolve_assist_model` reads exactly that file whatever APP_ENV says —
    under APP_ENV=production only the direct read sees its `llm:` block.
    Either read failing, or coming back empty, is a failure, not "no
    references": the deployment's defaults live in these files.
    """
    refs: dict[str, list[str]] = {}
    try:
        from src.dalton.llm.role_policy import DaltonStartupConfigError, _load_default_agent_config

        try:
            config = _load_default_agent_config()
        except DaltonStartupConfigError as exc:
            raise ReferenceSourceUnavailable("agent_config", exc) from exc
        if not isinstance(config, dict) or not config:
            raise ReferenceSourceUnavailable(
                "agent_config",
                RuntimeError("agent config loaded empty; expected at least an llm block"),
            )
        _collect_llm_block(config.get("llm"), "agent_config.llm", refs)
        subagents = config.get("subagents")
        definitions = subagents.get("definitions") if isinstance(subagents, dict) else None
        if isinstance(definitions, dict):
            for name, definition in definitions.items():
                if isinstance(definition, dict):
                    _add(refs, definition.get("model"), f"agent_config.subagents.{name}")
    except ReferenceSourceUnavailable:
        raise
    except Exception as exc:
        raise ReferenceSourceUnavailable("agent_config", exc) from exc

    _assist_agent_config_llm_refs(refs)
    return refs


def _assist_agent_config_llm_refs(refs: dict[str, list[str]]) -> None:
    """The `llm:` block of `REPO_ROOT/agent_config.dalton.yaml`, read directly.

    `resolve_assist_model` reads this exact file regardless of APP_ENV, so its
    block is collected on its own — the role-policy merge only includes it when
    APP_ENV is unset. Missing, unreadable, or empty is a failure: the file is
    part of the deployment's shipped configuration.
    """
    from src.dalton.settings import REPO_ROOT

    path = REPO_ROOT / "agent_config.dalton.yaml"
    try:
        if not path.is_file():
            raise FileNotFoundError(f"{path} does not exist")
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ReferenceSourceUnavailable("agent_config", exc) from exc
    if not isinstance(raw, dict) or not raw:
        raise ReferenceSourceUnavailable(
            "agent_config",
            RuntimeError(f"{path} is empty; expected the deployment's llm block"),
        )
    _collect_llm_block(raw.get("llm"), "agent_config.llm", refs)


def _dalton_yaml_llm_refs(refs: dict[str, list[str]]) -> None:
    """The raw `llm:` block of `src/dalton/dalton.yaml`, as `resolve_assist_model` reads it.

    Pydantic's `DaltonConfig` drops the block, so the tree walk never sees it;
    it is a live reference for as long as assist resolution reads the raw file.
    A missing, unreadable, or empty file is a failure — the settings source
    must not quietly answer "no references" — while a valid mapping without an
    `llm:` block is the normal case (#104 may remove it) and collects nothing.
    """
    from src.dalton.settings import DALTON_YAML_PATH

    try:
        if not DALTON_YAML_PATH.is_file():
            raise FileNotFoundError(f"{DALTON_YAML_PATH} does not exist")
        raw = yaml.safe_load(DALTON_YAML_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ReferenceSourceUnavailable("dalton_settings", exc) from exc
    if not isinstance(raw, dict) or not raw:
        raise ReferenceSourceUnavailable(
            "dalton_settings",
            RuntimeError(f"{DALTON_YAML_PATH} is empty; expected the Dalton settings mapping"),
        )
    _collect_llm_block(raw.get("llm"), "dalton.llm", refs)


def _collect_value(value: Any, label: str, refs: dict[str, list[str]]) -> None:
    """A str names one model; a list of strs names several; anything else is skipped."""
    if isinstance(value, str):
        _add(refs, value, label)
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        for item in value:
            _add(refs, item, label)


def _walk_settings(node: Any, path: str, refs: dict[str, list[str]]) -> None:
    """Collect every `*_model`/`*_models` field down the settings tree.

    Recognizing a model field and descending are independent: a hit still
    recurses into the value, and lists/dicts/nested objects are entered with or
    without a hit — future fields (`publish.audit_model`) are covered with no
    code change, and so is a model field that ever grows structure.
    """
    if isinstance(node, BaseModel):
        for name in type(node).model_fields:
            value = getattr(node, name, None)
            key = f"{path}.{name}" if path else name
            if name.endswith("_model") or name.endswith("_models"):
                _collect_value(value, key, refs)
            _walk_settings(value, key, refs)
    elif isinstance(node, dict):
        for name, value in node.items():
            key = f"{path}.{name}" if path else str(name)
            if str(name).endswith("_model") or str(name).endswith("_models"):
                _collect_value(value, key, refs)
            _walk_settings(value, key, refs)
    elif isinstance(node, list):
        for item in node:
            _walk_settings(item, path, refs)


def dalton_settings_refs() -> dict[str, list[str]]:
    """Model fields across the whole Dalton settings tree, labeled by dotted path."""
    from src.dalton.settings import get_assist_fallback_models, get_dalton_config

    refs: dict[str, list[str]] = {}
    try:
        _walk_settings(get_dalton_config(), "", refs)
        _dalton_yaml_llm_refs(refs)
        # The assist fallback chain also reaches the config through the env
        # var, which the tree walk cannot see.
        for fallback in get_assist_fallback_models():
            _add(refs, fallback, "models.assist_fallback_model")
    except ReferenceSourceUnavailable:
        raise
    except Exception as exc:
        raise ReferenceSourceUnavailable("dalton_settings", exc) from exc
    return refs


async def user_preference_refs() -> dict[str, list[str]]:
    """Saved model preferences for every user, via OpenClaw's shared query."""
    from src.dalton.database.openclaw_sync import find_user_referenced_models

    try:
        return await find_user_referenced_models()
    except Exception as exc:
        raise ReferenceSourceUnavailable("user_preferences", exc) from exc


async def user_subagent_refs() -> dict[str, list[str]]:
    """Each user's custom subagents and plugin overrides (`user_subagents.fields.model`)."""
    refs: dict[str, list[str]] = {}
    try:
        from psycopg.rows import dict_row

        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT user_id, name, fields FROM user_subagents"
                )
                for row in await cur.fetchall():
                    fields = row.get("fields") or {}
                    if isinstance(fields, dict):
                        _add(
                            refs,
                            fields.get("model"),
                            f"user_subagent:{row.get('user_id')}:{row.get('name')}",
                        )
    except Exception as exc:
        raise ReferenceSourceUnavailable("user_subagents", exc) from exc
    return refs


def _bundled_agent_model_refs(refs: dict[str, list[str]]) -> None:
    """Models pinned in shipped bundle agents (`plugins/*/agents/*.md`).

    Read from disk here rather than through `bundled._agent_plans`: that path
    answers an unreadable directory with `[]` behind an `lru_cache`, which is
    "no references" — exactly the answer retire must not trust. The same
    per-file parser (`read_agent`) runs, so what counts as a subagent and what
    its `model:` means stay identical to the runtime's own reading.
    """
    from ptc_agent.config.plugins import bundles
    from src.server.services.plugins.agents import AGENTS_DIR, read_agent

    for bundle in bundles():
        agents_dir = bundle.path / AGENTS_DIR.rstrip("/")
        if not agents_dir.is_dir():
            continue
        # OSError below (permissions, a bundle replaced mid-read) propagates:
        # the strict caller refuses rather than assumes no models are pinned.
        files = {
            p.name.removesuffix(".md"): p.read_bytes()
            for p in sorted(agents_dir.iterdir())
            if p.is_file() and p.suffix == ".md"
        }
        for name, raw in files.items():
            plan = read_agent(name, raw)
            if plan.skip_code is not None:
                continue
            fields = plan.fields or {}
            if isinstance(fields, dict):
                _add(refs, fields.get("model"), f"plugin_subagent:{plan.name}")


async def plugin_subagent_refs() -> dict[str, list[str]]:
    """Models pinned in shipped bundle agents and installed plugin subagents.

    Bundles ship in the image and apply to every user; installed plugins' rows
    are per user. A switched-off plugin's subagent still counts: it is one
    toggle away from running on the model it names.
    """
    refs: dict[str, list[str]] = {}
    try:
        _bundled_agent_model_refs(refs)
    except ReferenceSourceUnavailable:
        raise
    except Exception as exc:
        raise ReferenceSourceUnavailable("plugin_subagents", exc) from exc

    try:
        from psycopg.rows import dict_row

        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT s.name, s.fields FROM plugin_subagents s "
                    "JOIN user_plugins p "
                    "ON p.user_id = s.user_id AND p.user_plugin_id = s.plugin_id"
                )
                for row in await cur.fetchall():
                    fields = row.get("fields") or {}
                    if isinstance(fields, dict):
                        _add(refs, fields.get("model"), f"plugin_subagent:{row.get('name')}")
    except Exception as exc:
        raise ReferenceSourceUnavailable("plugin_subagents", exc) from exc
    return refs


async def automation_refs() -> dict[str, list[str]]:
    """The model each automation names (`automations.llm_model`)."""
    refs: dict[str, list[str]] = {}
    try:
        from psycopg.rows import dict_row

        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT file_name, automation_id, llm_model FROM automations "
                    "WHERE llm_model IS NOT NULL AND llm_model <> ''"
                )
                for row in await cur.fetchall():
                    model = str(row.get("llm_model") or "").strip()
                    if model:
                        refs.setdefault(model, []).append(
                            f"automation:{row.get('file_name') or row.get('automation_id')}"
                        )
    except Exception as exc:
        raise ReferenceSourceUnavailable("automations", exc) from exc
    return refs
