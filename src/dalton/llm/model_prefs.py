"""Dalton model preference resolution, defaults, and write-path validation.

Precedence order for all Dalton model settings:
1. Current user's ``user_preferences.model_preference[key]``
2. Team credential holder's (first PM from ``src/dalton/credentials.py``) ``model_preference[key]``
3. ``src/dalton/dalton.yaml`` default (via ``get_dalton_settings()``)

Reads go through ``get_user_preferences`` (Redis-cached); no in-process cache is kept.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import logging
from typing import Any

from src.dalton.credentials import get_team_credential_holder_id
from src.dalton.settings import get_assist_fallback_models, get_dalton_settings
from src.llms.preferences import TuningError
from src.server.database.user import get_user_preferences

logger = logging.getLogger(__name__)

DALTON_SINGLE_MODEL_KEYS: tuple[str, ...] = (
    "dalton_audit_model",
    "dalton_assist_model",
    "dalton_extract_model",
)

DALTON_MODEL_LIST_KEYS: tuple[str, ...] = (
    "dalton_audit_fallback_models",
    "dalton_assist_fallback_models",
    "dalton_reviewer_models",
)

DALTON_TIMEOUT_KEY = "dalton_audit_timeout_s"

DALTON_MODEL_PREF_KEYS: tuple[str, ...] = (
    *DALTON_SINGLE_MODEL_KEYS,
    *DALTON_MODEL_LIST_KEYS,
    DALTON_TIMEOUT_KEY,
)

DALTON_AUDIT_TIMEOUT_MIN = 60
DALTON_AUDIT_TIMEOUT_MAX = 1800


def _dedup_str_list(items: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in items:
        if not isinstance(raw, str):
            continue
        val = raw.strip()
        if val and val not in seen:
            seen.add(val)
            out.append(val)
    return out


def _normalize_stored_value(key: str, raw: Any) -> Any:
    """Normalize a stored preference value; return None when unset/empty/invalid."""
    if raw is None:
        return None
    if key in DALTON_SINGLE_MODEL_KEYS:
        if isinstance(raw, str):
            val = raw.strip()
            return val if val else None
        return None
    if key in DALTON_MODEL_LIST_KEYS:
        if isinstance(raw, list):
            deduped = _dedup_str_list(raw)
            return deduped if deduped else None
        if isinstance(raw, str) and raw.strip():
            return [raw.strip()]
        return None
    if key == DALTON_TIMEOUT_KEY:
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int) and DALTON_AUDIT_TIMEOUT_MIN <= raw <= DALTON_AUDIT_TIMEOUT_MAX:
            return raw
        return None
    return raw


def dalton_model_default(key: str) -> Any:
    """Return the config-file (`dalton.yaml`) default for ``key``, or ``None`` if unconfigured."""
    settings = get_dalton_settings()
    if key == "dalton_audit_model":
        val = (settings.publish.audit_model or "").strip()
        return val or None
    if key == "dalton_audit_fallback_models":
        items = _dedup_str_list(settings.publish.audit_fallback_models or [])
        return items or None
    if key == "dalton_audit_timeout_s":
        timeout_val = getattr(settings.publish, "audit_timeout_s", None)
        if (
            isinstance(timeout_val, (int, float))
            and not isinstance(timeout_val, bool)
            and timeout_val > 0
        ):
            return timeout_val
        return None
    if key == "dalton_assist_model":
        val = (settings.models.assist_model or "").strip()
        return val or None
    if key == "dalton_assist_fallback_models":
        items = _dedup_str_list(get_assist_fallback_models())
        return items or None
    if key == "dalton_reviewer_models":
        items = _dedup_str_list(settings.publish.reviewer_models or [])
        return items or None
    if key == "dalton_extract_model":
        val = (settings.conversations.extractor_model or "").strip()
        return val or None
    return None


def get_dalton_system_defaults() -> dict[str, Any]:
    """Return all Dalton model defaults from ``dalton.yaml`` for the ``system_defaults`` payload."""
    audit_timeout = dalton_model_default("dalton_audit_timeout_s")
    return {
        "dalton_audit_model": dalton_model_default("dalton_audit_model") or "",
        "dalton_audit_fallback_models": list(
            dalton_model_default("dalton_audit_fallback_models") or []
        ),
        "dalton_audit_timeout_s": (
            int(audit_timeout)
            if isinstance(audit_timeout, float) and audit_timeout.is_integer()
            else (
                audit_timeout
                if isinstance(audit_timeout, (int, float))
                and not isinstance(audit_timeout, bool)
                and audit_timeout > 0
                else 300
            )
        ),
        "dalton_assist_model": dalton_model_default("dalton_assist_model") or "",
        "dalton_assist_fallback_models": list(
            dalton_model_default("dalton_assist_fallback_models") or []
        ),
        "dalton_reviewer_models": list(
            dalton_model_default("dalton_reviewer_models") or []
        ),
        "dalton_extract_model": dalton_model_default("dalton_extract_model") or "",
    }


async def _read_user_model_preference_map(user_id: str) -> Mapping[str, Any] | None:
    """Return ``user_id``'s ``model_preference`` mapping via ``get_user_preferences``, or ``None``."""
    try:
        row = await get_user_preferences(user_id)
    except RuntimeError as exc:
        if "pool is not open" in str(exc):
            return None
        raise
    if not isinstance(row, Mapping):
        return None
    mp = row.get("model_preference")
    return mp if isinstance(mp, Mapping) else None


def _filter_shadowed_by_custom_models(
    key: str,
    value: Any,
    custom_set: set[str],
) -> Any:
    """Drop candidates shadowed by ``custom_set`` so Dalton roles never route to a custom model."""
    if not custom_set or value is None:
        return value
    if key in DALTON_SINGLE_MODEL_KEYS:
        if value in custom_set:
            logger.warning("Ignoring %s=%r: shadowed by a custom model", key, value)
            return None
        return value
    if key in DALTON_MODEL_LIST_KEYS and isinstance(value, list):
        kept = [m for m in value if m not in custom_set]
        if len(kept) != len(value):
            logger.warning("Ignoring custom-shadowed entries in %s", key)
        return kept or None
    return value


async def _read_user_model_pref_key(
    user_id: str,
    key: str,
    *,
    caller_custom_set: set[str] | None = None,
) -> Any:
    """Read and normalize ``key`` from ``user_id``'s ``model_preference``, filtering custom-shadowed names."""
    mp = await _read_user_model_preference_map(user_id)
    if mp is None:
        return None
    value = _normalize_stored_value(key, mp.get(key))
    # The LLM call resolves model names as the caller, so only the caller's
    # custom_models can shadow an inherited value; the owner's own are used
    # only when there is no caller (system context).
    custom_set = (
        caller_custom_set
        if caller_custom_set is not None
        else _extract_custom_model_set(mp)
    )
    return _filter_shadowed_by_custom_models(key, value, custom_set)


async def dalton_model_pref(user_id: str | None, key: str) -> Any:
    """Resolve a Dalton model preference with precedence: user -> first PM holder -> dalton.yaml.

    Every candidate (from user, PM holder, or yaml) is checked against the actual
    caller's (`user_id`) `custom_models` so a caller's custom model never shadows
    a system model in `LLMService`.
    """
    uid = (user_id or "").strip()
    caller_custom_set: set[str] = set()
    if uid:
        own_mp = await _read_user_model_preference_map(uid)
        if own_mp is not None:
            caller_custom_set = _extract_custom_model_set(own_mp)
            own_val = _filter_shadowed_by_custom_models(
                key,
                _normalize_stored_value(key, own_mp.get(key)),
                caller_custom_set,
            )
            if own_val is not None:
                return own_val

    holder_id = (get_team_credential_holder_id() or "").strip()
    if holder_id and holder_id != uid:
        holder_val = await _read_user_model_pref_key(
            holder_id,
            key,
            caller_custom_set=caller_custom_set if uid else None,
        )
        if holder_val is not None:
            return holder_val

    return _filter_shadowed_by_custom_models(
        key,
        dalton_model_default(key),
        caller_custom_set,
    )


async def resolve_dalton_audit_model(user_id: str | None = None) -> str:
    """Resolve the figure audit primary model for ``user_id`` (empty string if unconfigured)."""
    val = await dalton_model_pref(user_id, "dalton_audit_model")
    return str(val).strip() if isinstance(val, str) else ""


async def resolve_dalton_audit_fallback_models(user_id: str | None = None) -> list[str]:
    """Resolve the figure audit fallback model list for ``user_id``."""
    val = await dalton_model_pref(user_id, "dalton_audit_fallback_models")
    return list(val) if isinstance(val, list) else []


async def resolve_dalton_audit_timeout_s(user_id: str | None = None) -> int | float:
    """Resolve the figure audit timeout (seconds) for ``user_id``."""
    val = await dalton_model_pref(user_id, "dalton_audit_timeout_s")
    if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
        return val
    return 300.0


async def resolve_dalton_assist_model(user_id: str | None = None) -> str:
    """Resolve the auxiliary JSON task (assist) primary model for ``user_id``."""
    val = await dalton_model_pref(user_id, "dalton_assist_model")
    return str(val).strip() if isinstance(val, str) else ""


async def resolve_dalton_assist_fallback_models(user_id: str | None = None) -> list[str]:
    """Resolve the auxiliary JSON task (assist) fallback model list for ``user_id``."""
    val = await dalton_model_pref(user_id, "dalton_assist_fallback_models")
    return list(val) if isinstance(val, list) else []


async def resolve_dalton_reviewer_models(user_id: str | None = None) -> list[str]:
    """Resolve the ordered independent reviewer candidate model list for ``user_id``."""
    val = await dalton_model_pref(user_id, "dalton_reviewer_models")
    return list(val) if isinstance(val, list) else []


async def resolve_dalton_extract_model(user_id: str | None = None) -> str:
    """Resolve the conversation archive extraction model for ``user_id``."""
    val = await dalton_model_pref(user_id, "dalton_extract_model")
    return str(val).strip() if isinstance(val, str) else ""


resolve_audit_model = resolve_dalton_audit_model
resolve_audit_fallback_models = resolve_dalton_audit_fallback_models
resolve_audit_timeout_s = resolve_dalton_audit_timeout_s
resolve_reviewer_models = resolve_dalton_reviewer_models
resolve_extract_model = resolve_dalton_extract_model


def _build_catalog_model_set(
    catalog_models: Mapping[str, Any] | Iterable[str] | None,
) -> set[str]:
    """Return the set of system catalog model names (excluding user ``custom_models``)."""
    if catalog_models is None:
        from src.llms.llm import ModelConfig

        return set((ModelConfig().llm_config or {}).keys())
    if isinstance(catalog_models, Mapping):
        return {str(k) for k in catalog_models.keys()}
    return {str(k) for k in catalog_models}


def _extract_custom_model_set(
    effective_model_preference: Mapping[str, Any] | None,
) -> set[str]:
    """Extract custom model names from the post-patch ``effective`` ``model_preference``."""
    custom_set: set[str] = set()
    if not isinstance(effective_model_preference, Mapping):
        return custom_set
    custom_list = effective_model_preference.get("custom_models")
    if isinstance(custom_list, list):
        for entry in custom_list:
            if isinstance(entry, Mapping):
                cname = str(entry.get("name") or "").strip()
                if cname:
                    custom_set.add(cname)
    return custom_set


def validate_dalton_model_preferences(
    model_preference: dict[str, Any],
    catalog_models: Mapping[str, Any] | Iterable[str] | None = None,
    effective_model_preference: Mapping[str, Any] | None = None,
) -> None:
    """Validate and normalize ``dalton_*`` keys in ``model_preference`` in-place.

    Uses the post-patch ``effective_model_preference`` (never unioning stale
    stored ``custom_models`` with the incoming patch) and rejects user-defined
    ``custom_models`` with a Chinese reason because team colleagues cannot
    inherit a PM's custom model definition.
    """
    if not isinstance(model_preference, dict):
        return
    present_keys = [k for k in DALTON_MODEL_PREF_KEYS if k in model_preference]
    if not present_keys:
        return
    effective_bag = (
        effective_model_preference
        if isinstance(effective_model_preference, Mapping)
        else model_preference
    )

    from src.dalton.llm.role_policy import is_forbidden_for_roles

    catalog_set = _build_catalog_model_set(catalog_models)
    custom_set = _extract_custom_model_set(effective_bag)

    def _check_system_model(field_key: str, model_name: str) -> None:
        if model_name in custom_set or model_name not in catalog_set:
            if model_name in custom_set:
                raise TuningError(
                    field_key,
                    f"{field_key}: 不支持自定义模型 '{model_name}'（自定义模型无法被团队其他成员继承，请选择系统目录中的模型）",
                )
            raise TuningError(
                field_key,
                f"{field_key}: 模型 '{model_name}' 不在系统模型目录中（不支持自定义模型或未知模型，not in catalog）",
            )
        if is_forbidden_for_roles(model_name):
            raise TuningError(
                field_key,
                f"{field_key}: 模型 '{model_name}' 禁止用于 Dalton 角色（forbidden for Dalton roles）",
            )

    for key in DALTON_SINGLE_MODEL_KEYS:
        if key not in model_preference:
            continue
        raw = model_preference[key]
        if raw is None:
            continue
        if not isinstance(raw, str):
            raise TuningError(key, f"{key} 必须为模型名称字符串或 null")
        val = raw.strip()
        if not val:
            model_preference[key] = None
            continue
        _check_system_model(key, val)
        model_preference[key] = val

    for key in DALTON_MODEL_LIST_KEYS:
        if key not in model_preference:
            continue
        raw = model_preference[key]
        if raw is None:
            continue
        if not isinstance(raw, list):
            raise TuningError(key, f"{key} 必须为模型名称数组或 null")
        deduped: list[str] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                raise TuningError(
                    key, f"{key} 列表项必须为非空模型名称字符串"
                )
            m = item.strip()
            _check_system_model(key, m)
            if m not in seen:
                seen.add(m)
                deduped.append(m)
        model_preference[key] = deduped

    if DALTON_TIMEOUT_KEY in model_preference:
        raw_timeout = model_preference[DALTON_TIMEOUT_KEY]
        if raw_timeout is None or raw_timeout == "":
            model_preference[DALTON_TIMEOUT_KEY] = None
        elif isinstance(raw_timeout, bool) or not isinstance(raw_timeout, int):
            raise TuningError(
                DALTON_TIMEOUT_KEY,
                f"{DALTON_TIMEOUT_KEY} 必须为 {DALTON_AUDIT_TIMEOUT_MIN}–{DALTON_AUDIT_TIMEOUT_MAX} 之间的整数",
            )
        elif not (DALTON_AUDIT_TIMEOUT_MIN <= raw_timeout <= DALTON_AUDIT_TIMEOUT_MAX):
            raise TuningError(
                DALTON_TIMEOUT_KEY,
                f"{DALTON_TIMEOUT_KEY} 必须在 {DALTON_AUDIT_TIMEOUT_MIN}–{DALTON_AUDIT_TIMEOUT_MAX} 秒之间",
            )
