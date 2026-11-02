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
import re
from typing import Any

from src.dalton.credentials import get_team_credential_holder_id
from src.dalton.settings import get_assist_fallback_models, get_dalton_settings
from src.llms.preferences import TuningError
from src.server.database.user import get_user_preferences

logger = logging.getLogger(__name__)

DALTON_SINGLE_MODEL_KEYS: tuple[str, ...] = (
    "dalton_audit_model",
    "dalton_language_model",
    "dalton_assist_model",
    "dalton_extract_model",
)

DALTON_MODEL_LIST_KEYS: tuple[str, ...] = (
    "dalton_audit_fallback_models",
    "dalton_assist_fallback_models",
    "dalton_reviewer_models",
)

DALTON_TIMEOUT_KEY = "dalton_audit_timeout_s"
DALTON_NOTIFY_ENABLED_KEY = "dalton_notify_enabled"
DALTON_NOTIFY_TARGETS_KEY = "dalton_notify_targets"
DALTON_NOTIFY_OVERRIDES_KEY = "dalton_notify_overrides"
DALTON_NOTIFY_TARGET_LABELS_KEY = "dalton_notify_target_labels"
DALTON_NOTIFY_ROUTES_KEY = "dalton_notify_routes"

DALTON_MODEL_PREF_KEYS: tuple[str, ...] = (
    *DALTON_SINGLE_MODEL_KEYS,
    *DALTON_MODEL_LIST_KEYS,
    DALTON_TIMEOUT_KEY,
    DALTON_NOTIFY_ENABLED_KEY,
    DALTON_NOTIFY_TARGETS_KEY,
    DALTON_NOTIFY_OVERRIDES_KEY,
    DALTON_NOTIFY_TARGET_LABELS_KEY,
    DALTON_NOTIFY_ROUTES_KEY,
)

DALTON_AUDIT_TIMEOUT_MIN = 60
DALTON_AUDIT_TIMEOUT_MAX = 1800
DALTON_NOTIFY_LABEL_MAX_CHARS = 40

_FEISHU_NOTIFY_TARGET_RE = re.compile(r"^feishu:user:ou_[A-Za-z0-9_-]+$")
_FEISHU_CHAT_NOTIFY_TARGET_RE = re.compile(r"^feishu:chat:oc_[A-Za-z0-9_-]+$")
_DISCORD_NOTIFY_TARGET_RE = re.compile(r"^discord:user:[0-9]+$")


def is_valid_dalton_notify_target(target: str) -> bool:
    """Return True when ``target`` matches ``feishu:user:ou_<id>``, ``feishu:chat:oc_<id>`` or ``discord:user:<digits>``."""
    if not isinstance(target, str):
        return False
    cleaned = target.strip()
    return bool(
        _FEISHU_NOTIFY_TARGET_RE.match(cleaned)
        or _FEISHU_CHAT_NOTIFY_TARGET_RE.match(cleaned)
        or _DISCORD_NOTIFY_TARGET_RE.match(cleaned)
    )


def is_dalton_chat_target(target: str) -> bool:
    """True when ``target`` is a Feishu group target (``feishu:chat:oc_<id>``)."""
    if not isinstance(target, str):
        return False
    return bool(_FEISHU_CHAT_NOTIFY_TARGET_RE.match(target.strip()))


def is_valid_dalton_personal_target(target: str) -> bool:
    """A target a user may keep in their own list: a personal DM only (``feishu:user:``/``discord:user:``).

    Group targets (``feishu:chat:``) never enter ``dalton_notify_targets``; they
    only ever appear inside an admin's route entries.
    """
    return is_valid_dalton_notify_target(target) and not is_dalton_chat_target(target)


def normalize_dalton_notify_label(raw: Any) -> str:
    """Strip control characters and clamp a target label to 40 chars ("" when nothing remains)."""
    if not isinstance(raw, str):
        return ""
    cleaned = "".join(ch for ch in raw.strip() if ch.isprintable())
    return cleaned[:DALTON_NOTIFY_LABEL_MAX_CHARS]


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
    if key == DALTON_NOTIFY_ENABLED_KEY:
        return raw if isinstance(raw, bool) else None
    if key == DALTON_NOTIFY_TARGETS_KEY:
        if isinstance(raw, list):
            return [t for t in _dedup_str_list(raw) if is_valid_dalton_personal_target(t)]
        if isinstance(raw, str) and is_valid_dalton_personal_target(raw.strip()):
            return [raw.strip()]
        return None
    if key == DALTON_NOTIFY_OVERRIDES_KEY:
        if isinstance(raw, Mapping):
            out_map: dict[str, bool] = {}
            for k, v in raw.items():
                if isinstance(k, str) and k.strip() and isinstance(v, bool):
                    out_map[k.strip()] = v
            return out_map
        return None
    if key == DALTON_NOTIFY_TARGET_LABELS_KEY:
        if isinstance(raw, Mapping):
            labels: dict[str, str] = {}
            for k, v in raw.items():
                if isinstance(k, str) and is_valid_dalton_notify_target(k.strip()):
                    label = normalize_dalton_notify_label(v)
                    if label:
                        labels[k.strip()] = label
            return labels or None
        return None
    if key == DALTON_NOTIFY_ROUTES_KEY:
        if isinstance(raw, Mapping):
            routes: dict[str, list[str]] = {}
            for k, v in raw.items():
                if isinstance(k, str) and k.strip() and isinstance(v, list):
                    kept = _dedup_str_list(v)
                    if all(is_valid_dalton_notify_target(t) for t in kept):
                        # An empty list is kept on purpose: it is an explicit
                        # "deliver nowhere" route, distinct from a missing key
                        # (no route → all personal targets, or the category
                        # route underneath).
                        routes[k.strip()] = kept
            return routes or None
        return None
    return raw


def dalton_model_default(key: str) -> Any:
    """Return the config-file (`dalton.yaml`) default for ``key``, or ``None`` if unconfigured."""
    settings = get_dalton_settings()
    if key == "dalton_audit_model":
        val = (settings.publish.audit_model or "").strip()
        return val or None
    if key == "dalton_language_model":
        # publish.language_model left empty means "same chain as the number
        # audit", so the fallback is resolved here and the Settings hint shows
        # the effective model rather than a blank.
        val = (settings.publish.language_model or "").strip()
        if val:
            return val
        return dalton_model_default("dalton_audit_model")
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
        "dalton_language_model": dalton_model_default("dalton_language_model") or "",
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
    if key == DALTON_NOTIFY_ENABLED_KEY:
        return await resolve_dalton_notify_enabled(user_id)
    if key == DALTON_NOTIFY_TARGETS_KEY:
        return await resolve_dalton_notify_targets(user_id)
    if key == DALTON_NOTIFY_OVERRIDES_KEY:
        return await resolve_dalton_notify_overrides(user_id)
    if key == DALTON_NOTIFY_TARGET_LABELS_KEY:
        return await resolve_dalton_notify_target_labels(user_id)
    if key == DALTON_NOTIFY_ROUTES_KEY:
        return await resolve_dalton_notify_routes(user_id)
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


async def resolve_dalton_language_model(user_id: str | None = None) -> str:
    """Resolve the language-review model for ``user_id`` (empty string if unconfigured)."""
    val = await dalton_model_pref(user_id, "dalton_language_model")
    return str(val).strip() if isinstance(val, str) else ""


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


async def resolve_dalton_notify_enabled(user_id: str | None = None) -> bool:
    """Resolve per-user Feishu/Discord notification master switch (default ``False``, isolated per user)."""
    uid = (user_id or "").strip()
    if not uid:
        return False
    try:
        row = await get_user_preferences(uid)
    except RuntimeError as exc:
        if "pool is not open" in str(exc):
            return False
        raise
    if not isinstance(row, Mapping):
        return False
    for bag_key in ("model_preference", "other_preference"):
        bag = row.get(bag_key)
        if isinstance(bag, Mapping) and DALTON_NOTIFY_ENABLED_KEY in bag:
            normalized = _normalize_stored_value(
                DALTON_NOTIFY_ENABLED_KEY, bag.get(DALTON_NOTIFY_ENABLED_KEY)
            )
            return bool(normalized) if isinstance(normalized, bool) else False
    return False


async def resolve_dalton_notify_targets(user_id: str | None = None) -> list[str]:
    """Resolve per-user notification targets for ``user_id`` (isolated per user, never inherited)."""
    uid = (user_id or "").strip()
    if not uid:
        return []
    try:
        row = await get_user_preferences(uid)
    except RuntimeError as exc:
        if "pool is not open" in str(exc):
            return []
        raise
    if not isinstance(row, Mapping):
        return []
    for bag_key in ("model_preference", "other_preference"):
        bag = row.get(bag_key)
        if isinstance(bag, Mapping) and DALTON_NOTIFY_TARGETS_KEY in bag:
            normalized = _normalize_stored_value(
                DALTON_NOTIFY_TARGETS_KEY, bag.get(DALTON_NOTIFY_TARGETS_KEY)
            )
            return list(normalized) if isinstance(normalized, list) else []
    return []


async def resolve_dalton_notify_overrides(user_id: str | None = None) -> dict[str, bool]:
    """Resolve per-user automation notification overrides ``{automation_name_or_category: bool}``."""
    return await _resolve_notify_pref_map(
        user_id,
        DALTON_NOTIFY_OVERRIDES_KEY,
        # Pre-migration rows keep their copy under other_preference.
        bags=("model_preference", "other_preference"),
    )


async def resolve_dalton_notify_target_labels(user_id: str | None = None) -> dict[str, str]:
    """Resolve per-user notify target labels ``{target: label}``."""
    return await _resolve_notify_pref_map(user_id, DALTON_NOTIFY_TARGET_LABELS_KEY)


async def resolve_dalton_notify_routes(user_id: str | None = None) -> dict[str, list[str]]:
    """Resolve per-task notify routes ``{automation_name_or_category: [target, ...]}``."""
    return await _resolve_notify_pref_map(user_id, DALTON_NOTIFY_ROUTES_KEY)


async def _resolve_notify_pref_map(
    user_id: str | None,
    key: str,
    *,
    bags: tuple[str, ...] = ("model_preference",),
) -> dict[str, Any]:
    """Read a notify mapping preference for ``user_id`` from the given bags (never inherited).

    The routes and labels keys are model_preference-only: a value under
    ``other_preference`` was never written by a legal path (the write side
    refuses it), so reading it back would only honor a bypass. The older
    targets/overrides keys keep their two-bag read for pre-migration rows.
    """
    uid = (user_id or "").strip()
    empty: dict[str, Any] = {}
    if not uid:
        return empty
    try:
        row = await get_user_preferences(uid)
    except RuntimeError as exc:
        if "pool is not open" in str(exc):
            return empty
        raise
    if not isinstance(row, Mapping):
        return empty
    for bag_key in bags:
        bag = row.get(bag_key)
        if isinstance(bag, Mapping) and key in bag:
            normalized = _normalize_stored_value(key, bag.get(key))
            return dict(normalized) if isinstance(normalized, Mapping) else empty
    return empty


resolve_audit_model = resolve_dalton_audit_model
resolve_audit_fallback_models = resolve_dalton_audit_fallback_models
resolve_audit_timeout_s = resolve_dalton_audit_timeout_s
resolve_reviewer_models = resolve_dalton_reviewer_models
resolve_extract_model = resolve_dalton_extract_model
resolve_notify_enabled = resolve_dalton_notify_enabled
resolve_notify_targets = resolve_dalton_notify_targets
resolve_notify_overrides = resolve_dalton_notify_overrides
resolve_notify_target_labels = resolve_dalton_notify_target_labels
resolve_notify_routes = resolve_dalton_notify_routes


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
    stored_model_preference: Mapping[str, Any] | None = None,
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

    has_model_keys = any(
        k in model_preference
        for k in (*DALTON_SINGLE_MODEL_KEYS, *DALTON_MODEL_LIST_KEYS)
    )
    catalog_set = _build_catalog_model_set(catalog_models) if has_model_keys else set()
    custom_set = _extract_custom_model_set(effective_bag) if has_model_keys else set()

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

    if DALTON_NOTIFY_ENABLED_KEY in model_preference:
        raw_enabled = model_preference[DALTON_NOTIFY_ENABLED_KEY]
        if raw_enabled is None:
            model_preference[DALTON_NOTIFY_ENABLED_KEY] = None
        elif not isinstance(raw_enabled, bool):
            raise TuningError(
                DALTON_NOTIFY_ENABLED_KEY,
                f"{DALTON_NOTIFY_ENABLED_KEY} 必须为布尔值（true/false）",
            )

    if DALTON_NOTIFY_TARGETS_KEY in model_preference:
        raw_targets = model_preference[DALTON_NOTIFY_TARGETS_KEY]
        if raw_targets is None:
            model_preference[DALTON_NOTIFY_TARGETS_KEY] = None
        elif not isinstance(raw_targets, list):
            raise TuningError(
                DALTON_NOTIFY_TARGETS_KEY,
                f"{DALTON_NOTIFY_TARGETS_KEY} 必须为字符串列表（如 feishu:user:ou_xxx 或 discord:user:123）",
            )
        else:
            deduped_targets: list[str] = []
            seen_targets: set[str] = set()
            for item in raw_targets:
                if not isinstance(item, str) or not is_valid_dalton_personal_target(item):
                    raise TuningError(
                        DALTON_NOTIFY_TARGETS_KEY,
                        f"{DALTON_NOTIFY_TARGETS_KEY}: 个人目标格式无效 {item!r}，仅支持 feishu:user:<open_id> 或 discord:user:<数字id>（飞书群由管理员在任务路由中选择）",
                    )
                cleaned_target = item.strip()
                if cleaned_target not in seen_targets:
                    seen_targets.add(cleaned_target)
                    deduped_targets.append(cleaned_target)
            model_preference[DALTON_NOTIFY_TARGETS_KEY] = deduped_targets

    if DALTON_NOTIFY_OVERRIDES_KEY in model_preference:
        raw_overrides = model_preference[DALTON_NOTIFY_OVERRIDES_KEY]
        if raw_overrides is None:
            model_preference[DALTON_NOTIFY_OVERRIDES_KEY] = None
        elif not isinstance(raw_overrides, Mapping):
            raise TuningError(
                DALTON_NOTIFY_OVERRIDES_KEY,
                f"{DALTON_NOTIFY_OVERRIDES_KEY} 必须为任务名或类别到布尔值的映射对象",
            )
        else:
            normalized_overrides: dict[str, bool | None] = {}
            for k, v in raw_overrides.items():
                if not isinstance(k, str) or not k.strip() or (v is not None and not isinstance(v, bool)):
                    raise TuningError(
                        DALTON_NOTIFY_OVERRIDES_KEY,
                        f"{DALTON_NOTIFY_OVERRIDES_KEY}: 键必须为非空字符串且值必须为布尔值或 null",
                    )
                normalized_overrides[k.strip()] = v
            model_preference[DALTON_NOTIFY_OVERRIDES_KEY] = normalized_overrides

    if DALTON_NOTIFY_TARGET_LABELS_KEY in model_preference:
        raw_labels = model_preference[DALTON_NOTIFY_TARGET_LABELS_KEY]
        if raw_labels is None:
            model_preference[DALTON_NOTIFY_TARGET_LABELS_KEY] = None
        elif not isinstance(raw_labels, Mapping):
            raise TuningError(
                DALTON_NOTIFY_TARGET_LABELS_KEY,
                f"{DALTON_NOTIFY_TARGET_LABELS_KEY} 必须为推送目标到别名字符串的映射对象",
            )
        else:
            normalized_labels: dict[str, str | None] = {}
            for k, v in raw_labels.items():
                if not isinstance(k, str) or not is_valid_dalton_notify_target(k.strip()):
                    raise TuningError(
                        DALTON_NOTIFY_TARGET_LABELS_KEY,
                        f"{DALTON_NOTIFY_TARGET_LABELS_KEY}: 键必须是合法推送目标（feishu:user:<open_id>、feishu:chat:<chat_id> 或 discord:user:<数字id>）",
                    )
                if v is None:
                    normalized_labels[k.strip()] = None
                    continue
                if not isinstance(v, str):
                    raise TuningError(
                        DALTON_NOTIFY_TARGET_LABELS_KEY,
                        f"{DALTON_NOTIFY_TARGET_LABELS_KEY}: 别名必须为字符串或 null",
                    )
                label = normalize_dalton_notify_label(v)
                # A label that cleans down to "" is the same as no label: null
                # deletes the key under the column's deep-merge semantics.
                normalized_labels[k.strip()] = label or None
            model_preference[DALTON_NOTIFY_TARGET_LABELS_KEY] = normalized_labels

    if DALTON_NOTIFY_ROUTES_KEY in model_preference:
        raw_routes = model_preference[DALTON_NOTIFY_ROUTES_KEY]
        if raw_routes is None:
            model_preference[DALTON_NOTIFY_ROUTES_KEY] = None
        elif not isinstance(raw_routes, Mapping):
            raise TuningError(
                DALTON_NOTIFY_ROUTES_KEY,
                f"{DALTON_NOTIFY_ROUTES_KEY} 必须为任务名或类别到推送目标列表的映射对象",
            )
        else:
            normalized_routes: dict[str, list[str] | None] = {}
            for k, v in raw_routes.items():
                if not isinstance(k, str) or not k.strip() or (v is not None and not isinstance(v, list)):
                    raise TuningError(
                        DALTON_NOTIFY_ROUTES_KEY,
                        f"{DALTON_NOTIFY_ROUTES_KEY}: 键必须为非空字符串且值必须为推送目标列表或 null",
                    )
                if v is None:
                    normalized_routes[k.strip()] = None
                    continue
                kept: list[str] = []
                seen_route_targets: set[str] = set()
                for item in v:
                    if not isinstance(item, str) or not is_valid_dalton_notify_target(item):
                        raise TuningError(
                            DALTON_NOTIFY_ROUTES_KEY,
                            f"{DALTON_NOTIFY_ROUTES_KEY}: 目标格式无效 {item!r}，仅支持 feishu:user:<open_id>、feishu:chat:<chat_id> 或 discord:user:<数字id>",
                        )
                    cleaned_route_target = item.strip()
                    if cleaned_route_target not in seen_route_targets:
                        seen_route_targets.add(cleaned_route_target)
                        kept.append(cleaned_route_target)
                # An empty list is stored as-is: an explicit "deliver nowhere"
                # route. Only null deletes the key (no route → fall back to the
                # category route or all personal targets).
                normalized_routes[k.strip()] = kept
            model_preference[DALTON_NOTIFY_ROUTES_KEY] = normalized_routes

    _validate_and_prune_notify_target_refs(
        model_preference, effective_bag, stored_model_preference
    )


def _validate_and_prune_notify_target_refs(
    model_preference: dict[str, Any],
    effective_bag: Mapping[str, Any],
    stored_bag: Mapping[str, Any] | None = None,
) -> None:
    """Cross-check patch routes' personal targets and drop stale personal refs in-place.

    A route entry may name personal targets and (for admins) group targets. Only
    the personal side is checked here: it must be in the effective target list,
    and when this patch changes that list, deleted personal targets are cleaned
    out of routes and labels as a per-task cleanup patch. A route that cleans
    down to nothing is written as an explicit empty list — the task delivers
    nowhere — because ``null`` would mean "no route" and widen delivery back to
    every personal target (or the category route underneath). Group targets are
    skipped — they never belong to the personal list, and their permission and
    existence checks need the user's identity and the discovery API
    (``validate_dalton_notify_routes_for_user``). Stored routes naming a
    personal target the user dropped are a deletion to clean up, not a refusal
    — otherwise removing a target could never land.
    """
    effective_targets_raw = effective_bag.get(DALTON_NOTIFY_TARGETS_KEY)
    effective_targets = (
        [str(t).strip() for t in effective_targets_raw if isinstance(t, str) and t.strip()]
        if isinstance(effective_targets_raw, list)
        else []
    )
    target_set = set(effective_targets)

    # The merged view of the routes/labels maps the column will hold once this
    # patch lands: the stored sub-keys with the patch's own on top (a `None`
    # sub-key in the patch deletes that entry). The patch side is the
    # *normalized* map from above — its keys are stripped, so `" T "` in the
    # request overrides stored `T` instead of coexisting with it and letting
    # the stale entry win. `effective_bag` alone can't show this when the patch
    # replaces one of these maps wholesale, so the stored bag rides along
    # (users.py passes it, the same hand-off `_clear_unhonored_efforts` uses
    # for profiles); when the patch names no map at all, the effective value is
    # the stored one and serves as the current side.
    normalized_patch_routes = model_preference.get(DALTON_NOTIFY_ROUTES_KEY)
    current_routes = (
        normalized_patch_routes
        if isinstance(normalized_patch_routes, Mapping)
        else effective_bag.get(DALTON_NOTIFY_ROUTES_KEY)
    )
    merged_routes_view = _merged_map_view(
        stored_bag.get(DALTON_NOTIFY_ROUTES_KEY) if isinstance(stored_bag, Mapping) else None,
        current_routes,
    )
    normalized_patch_labels = model_preference.get(DALTON_NOTIFY_TARGET_LABELS_KEY)
    current_labels = (
        normalized_patch_labels
        if isinstance(normalized_patch_labels, Mapping)
        else effective_bag.get(DALTON_NOTIFY_TARGET_LABELS_KEY)
    )
    merged_labels_view = _merged_map_view(
        stored_bag.get(DALTON_NOTIFY_TARGET_LABELS_KEY) if isinstance(stored_bag, Mapping) else None,
        current_labels,
    )

    patched_routes = model_preference.get(DALTON_NOTIFY_ROUTES_KEY)
    if isinstance(patched_routes, Mapping):
        for route_targets in patched_routes.values():
            if not isinstance(route_targets, list):
                continue
            for item in route_targets:
                cleaned_item = item.strip() if isinstance(item, str) else ""
                if (
                    cleaned_item
                    and not is_dalton_chat_target(cleaned_item)
                    and cleaned_item not in target_set
                ):
                    raise TuningError(
                        DALTON_NOTIFY_ROUTES_KEY,
                        f"{DALTON_NOTIFY_ROUTES_KEY}: 路由中的个人目标 {cleaned_item!r} 不在当前推送目标列表中",
                    )

    # Cleanup only applies when this patch changes the target list: without a
    # new list there is nothing removed to clean up after.
    if DALTON_NOTIFY_TARGETS_KEY not in model_preference:
        return

    if isinstance(merged_routes_view, Mapping):
        route_cleanup: dict[str, list[str] | None] = {}
        for route_key, route_targets in merged_routes_view.items():
            if not isinstance(route_key, str) or not route_key.strip() or not isinstance(route_targets, list):
                continue
            original = [
                str(t).strip() for t in route_targets if isinstance(t, str) and t.strip()
            ]
            # Personal entries must survive in the new list; group entries stay
            # untouched whatever the personal list did.
            kept = [
                t
                for t in original
                if is_dalton_chat_target(t) or t in target_set
            ]
            if kept != original:
                # An emptied route is `[]` (deliver nowhere), never null.
                route_cleanup[route_key.strip()] = kept
        if route_cleanup:
            merged = dict(patched_routes) if isinstance(patched_routes, Mapping) else {}
            merged.update(route_cleanup)
            model_preference[DALTON_NOTIFY_ROUTES_KEY] = merged

    if isinstance(merged_labels_view, Mapping):
        label_cleanup: dict[str, None] = {}
        for label_target in merged_labels_view:
            cleaned_key = label_target.strip() if isinstance(label_target, str) else ""
            if (
                cleaned_key
                and not is_dalton_chat_target(cleaned_key)
                and cleaned_key not in target_set
            ):
                label_cleanup[cleaned_key] = None
        if label_cleanup:
            patched_labels = model_preference.get(DALTON_NOTIFY_TARGET_LABELS_KEY)
            merged_labels = (
                dict(patched_labels) if isinstance(patched_labels, Mapping) else {}
            )
            merged_labels.update(label_cleanup)
            model_preference[DALTON_NOTIFY_TARGET_LABELS_KEY] = merged_labels


def _merged_map_view(
    stored: Any,
    current: Any,
) -> Any:
    """The sub-key-merged view of a stored map and the value effective now.

    ``current`` (from the one-level effective bag) is the patch's whole map
    when the patch sets the key, else the stored one. Re-merging here restores
    the entries the patch did not name, and honors a ``None`` sub-entry in the
    patch as a deletion, mirroring the column's jsonb deep merge.
    """
    if not isinstance(stored, Mapping):
        return current
    if not isinstance(current, Mapping):
        return current
    merged = dict(stored)
    for key, value in current.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


async def validate_dalton_notify_routes_for_user(
    user_id: str | None,
    submitted_routes: Mapping[str, Any] | None,
    stored_routes: Mapping[str, Any] | None = None,
) -> None:
    """Enforce the group-target rules a user-context-less validator cannot.

    ``submitted_routes`` is what the caller actually put in the patch — not the
    normalized-and-cleaned version ``validate_dalton_model_preferences``
    produces: entries the server-side cleanup keeps (a group left behind when
    its task's personal target was deleted) are pass-through, not a new
    submission, so a user must stay able to delete their own personal targets
    while discovery is down, the bot left the group, or they lost admin.
    Group targets are admin-only and must be in the bot's discovered chat list
    at write time. Raises ``HTTPException``: 403 for a non-admin writing a
    group target, 503 when the discovery list is unavailable (the write should
    retry later), 400 for a group the bot is not in. A submission that names
    no group target passes without any discovery call.

    Only groups *added* to a route are gated: one already in ``stored_routes``
    under the same key stays as it was, so a route still holding groups the bot
    has since left can be edited one removal at a time (each removal submits
    the rest, and re-checking them would 400 every step).
    """
    from fastapi import HTTPException

    def _stored_chats(key: str) -> set[str]:
        if not isinstance(stored_routes, Mapping):
            return set()
        for stored_key, stored_targets in stored_routes.items():
            if isinstance(stored_key, str) and stored_key.strip() == key and isinstance(stored_targets, list):
                return {t.strip() for t in stored_targets if isinstance(t, str) and t.strip()}
        return set()

    chat_targets: set[str] = set()
    if isinstance(submitted_routes, Mapping):
        for route_key, route_targets in submitted_routes.items():
            if not isinstance(route_targets, list):
                continue
            already = _stored_chats(route_key.strip() if isinstance(route_key, str) else "")
            for item in route_targets:
                cleaned = item.strip() if isinstance(item, str) else ""
                if cleaned and is_dalton_chat_target(cleaned) and cleaned not in already:
                    chat_targets.add(cleaned)
    if not chat_targets:
        return

    from src.dalton.invites import is_invite_pm

    if not is_invite_pm(user_id):
        raise HTTPException(
            status_code=403,
            detail="仅管理员可在推送路由中使用飞书群目标",
        )

    from src.dalton.notify.groups import FeishuGroupsUnavailable, discover_feishu_notify_groups

    try:
        groups = await discover_feishu_notify_groups()
    except FeishuGroupsUnavailable:
        raise HTTPException(
            status_code=503,
            detail="飞书群列表暂时不可用，请稍后再试",
        ) from None
    known = {f"feishu:chat:{g['chat_id']}" for g in groups}
    unknown = sorted(chat_targets - known)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"群 {unknown[0]} 不在机器人已加入的群列表中（请先把机器人拉进群，再刷新群列表）",
        )

