"""Unified structured-output LLM helper (`assist_json`, P0G §0.3 / G0).

Rules:
- Always routes through `dalton_complete` (budget-gated, journal-cached in Postgres).
- Validates JSON output against caller-provided `schema` via `jsonschema.validate`.
- Never raises and never blocks research: on timeout, parse error, schema validation
  error, or budget exhaustion, logs a concise warning (task, model, elapsed_ms, error)
  without full payload and returns `AssistResult(ok=False, data=None, error=...)`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import jsonschema
import yaml

from src.dalton.database.provider_events import sanitize_error_text
from src.dalton.llm.complete import dalton_complete
from src.dalton.settings import DALTON_YAML_PATH, REPO_ROOT, get_assist_fallback_models

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


@dataclass
class AssistResult:
    """Envelope returned by `assist_json`."""

    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None
    model: str | None = None
    cost_usd: float = 0.0
    cached: bool = False
    replayed: bool = False
    status: str = "ok"
    usage: dict[str, Any] = field(default_factory=dict)
    error_kind: str | None = None


def _read_yaml_dict(path: Any) -> dict[str, Any]:
    try:
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _validate_assist_candidate(role: str, candidate: str) -> str:
    from src.dalton.llm.role_policy import RolePolicyError, is_forbidden_for_roles

    cand = candidate.strip()
    if is_forbidden_for_roles(cand):
        raise RolePolicyError(
            f"角色 {role} 配置了禁用变体 {cand}（contributor 变体可能用于训练，L21 严禁用于任何基金研究角色）。"
        )
    return cand


def resolve_assist_model(role: str = "assist", explicit_model: str | None = None) -> str:
    """Resolve model key for `role` (default `'assist'`).

    Priority:
    1. `explicit_model` if provided
    2. `agent_config.dalton.yaml` `llm.<role>` (e.g. `llm.assist`)
    3. `src/dalton/dalton.yaml` `llm.<role>` or `publish.extract_model`
    4. `src/dalton/dalton.yaml` `models.assist_model`

    Raises `RolePolicyError` if the resolved model is marked `forbidden_for_roles` (L21).
    """
    if explicit_model and explicit_model.strip():
        return _validate_assist_candidate(role, explicit_model)

    agent_cfg_path = REPO_ROOT / "agent_config.dalton.yaml"
    agent_cfg = _read_yaml_dict(agent_cfg_path)
    llm_block = agent_cfg.get("llm")
    if isinstance(llm_block, dict):
        role_val = llm_block.get(role)
        if isinstance(role_val, str) and role_val.strip():
            return _validate_assist_candidate(role, role_val)

    dalton_cfg = _read_yaml_dict(DALTON_YAML_PATH)
    dalton_llm = dalton_cfg.get("llm")
    if isinstance(dalton_llm, dict):
        role_val = dalton_llm.get(role)
        if isinstance(role_val, str) and role_val.strip():
            return _validate_assist_candidate(role, role_val)
    publish_block = dalton_cfg.get("publish")
    if isinstance(publish_block, dict) and role in ("assist", "extract"):
        ext_val = publish_block.get("extract_model")
        if isinstance(ext_val, str) and ext_val.strip():
            return _validate_assist_candidate(role, ext_val)

    # Last resort is the configured `models.assist_model` (dalton.yaml), never a name in code.
    from src.dalton.settings import get_dalton_config

    return _validate_assist_candidate(role, get_dalton_config().models.assist_model)


def resolve_assist_fallback_models(role: str = "assist_fallback") -> list[str]:
    """Resolve fallback model list for `assist_json` from `settings.get_assist_fallback_models()` (P0J.J2)."""
    return [
        _validate_assist_candidate(role, m)
        for m in get_assist_fallback_models()
        if m and m.strip()
    ]


def resolve_assist_fallback_model(role: str = "assist_fallback") -> str | None:
    """Return the first configured assist fallback model, if any."""
    models = resolve_assist_fallback_models(role=role)
    return models[0] if models else None


def _has_stream_output_started(comp: Any) -> bool:
    """Return True if the call already started streaming or produced output before failing."""
    if getattr(comp, "stream_started", False):
        return True
    if int(getattr(comp, "chunks_received", 0) or 0) > 0:
        return True
    if getattr(comp, "data", None) is not None:
        return True
    if str(getattr(comp, "text", None) or ""):
        return True
    usage = getattr(comp, "usage", None)
    if isinstance(usage, dict) and (
        int(usage.get("output_tokens") or 0) > 0
        or int(usage.get("chunks_received") or 0) > 0
        or bool(usage.get("stream_started"))
    ):
        return True
    return False


def _is_safe_to_fallback(comp: Any) -> bool:
    """Return True only when `dalton_complete` failed safely without charge (`pre_send`, pre-stream `billing`, or `4xx`)."""
    if comp.ok or getattr(comp, "status", None) != "failed":
        return False
    ek = str(getattr(comp, "error_kind", None) or "").strip().lower()
    if ek == "billing":
        return not _has_stream_output_started(comp)
    return ek == "pre_send" or (ek.startswith("http_4") and ek != "http_429")


def _is_timeout_or_rate_limit(comp: Any) -> bool:
    if comp.ok:
        return False
    text = f"{getattr(comp, 'error_kind', '') or ''} {getattr(comp, 'error', '') or ''}".lower()
    return "timeout" in text or "http_429" in text or "429" in text or "rate_limit" in text


def _extract_json_dict(raw_data: Any, raw_text: str | None) -> dict[str, Any]:
    """Parse a JSON object from `raw_data` or `raw_text`, stripping markdown code fences if present."""
    if isinstance(raw_data, dict):
        return raw_data
    if isinstance(raw_data, BaseException):
        raise raw_data

    from src.dalton.llm.complete import _unwrap_content_blocks

    text = _unwrap_content_blocks(raw_text if raw_text is not None else raw_data).strip()
    if not text:
        raise ValueError("Empty response from model")

    m = _FENCE_RE.match(text)
    if m:
        text = m.group(1).strip()
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]

    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object, got {type(parsed).__name__}")
    return parsed


def _normalize_schema_enums(instance: Any, schema: Any) -> Any:
    """Strip surrounding whitespace and match string enum values case-insensitively when unique."""
    if not isinstance(schema, dict):
        return instance
    enum_vals = schema.get("enum")
    if isinstance(enum_vals, list) and isinstance(instance, str):
        stripped = instance.strip()
        if stripped in enum_vals:
            return stripped
        lowered = stripped.lower()
        matches = [
            member
            for member in enum_vals
            if isinstance(member, str) and member.lower() == lowered
        ]
        if len(matches) == 1:
            return matches[0]
        return instance
    if isinstance(instance, dict):
        props = schema.get("properties")
        if isinstance(props, dict):
            for k, prop_schema in props.items():
                if k in instance:
                    instance[k] = _normalize_schema_enums(instance[k], prop_schema)
    elif isinstance(instance, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for idx, item in enumerate(instance):
                instance[idx] = _normalize_schema_enums(item, item_schema)
    return instance


async def assist_json(
    task: str,
    instructions: str,
    payload: dict[str, Any],
    schema: dict[str, Any],
    *,
    user_id: str,
    role: str = "assist",
    scope_ref: str | None = None,
    timeout_s: float = 60.0,
    model: str | None = None,
    llm_service: Any = None,
    workspace_id: str | None = None,
    thread_id: str | None = None,
    resend_indeterminate: bool = False,
    persist_raw: bool = True,
    skip_journal: bool = False,
    fallback_models: list[str] | None = None,
    deadline: float | None = None,
) -> AssistResult:
    """Invoke a cheap/subscription LLM for structured JSON judgment, cached in Postgres.

    ``fallback_models`` replaces the shared assist fallbacks for a task that names its
    own model chain. For such a chain each candidate is judged by its own billing: a
    subscription-billed model may be re-sent after an indeterminate call and may be
    left for the next candidate after a timeout or 429, since neither can charge cash
    twice; a cash model keeps the double-charge guard. ``deadline`` (``time.monotonic``)
    is handed to every call: past it a journalled result still replays, but nothing new
    is sent, and each send's timeout is capped to the time left at the moment of sending.

    Never raises: returns `AssistResult(ok=False, data=None, error=...)` on any error.
    """
    t0 = time.monotonic()
    resolved_model = str(model or "")
    if not user_id or not str(user_id).strip():
        raise ValueError("user_id is required")
    user_id = str(user_id).strip()
    comp: Any = None

    try:
        resolved_model = resolve_assist_model(role=role, explicit_model=model)
        own_chain = fallback_models is not None

        def _is_subscription(m: str) -> bool:
            from src.dalton.llm.complete import resolve_model_billing_and_provider

            return resolve_model_billing_and_provider(m)[0] == "oauth"

        def _resend_for(m: str) -> bool:
            return bool(resend_indeterminate) or (own_chain and _is_subscription(m))

        # Every candidate passes the policy check (L21) before anything is sent, so a
        # forbidden fallback fails fast rather than after a primary result is discarded.
        fb_list = (
            [_validate_assist_candidate(f"{role}_fallback", m) for m in fallback_models if m and m.strip()]
            if own_chain
            else (resolve_assist_fallback_models() if model is None else [])
        )

        payload_json = json.dumps(
            payload if isinstance(payload, dict) else {"value": payload},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        payload_sha = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()[:24]
        eff_scope = scope_ref or f"assist:{user_id}:{task}:{payload_sha}"

        schema_json = json.dumps(schema, ensure_ascii=False, indent=2)
        system_prompt = (
            f"{instructions.strip()}\n\n"
            "请严格只输出符合以下 JSON Schema 的 JSON 对象（不要输出 Markdown 代码块或额外解释）：\n"
            f"{schema_json}"
        )

        comp = await dalton_complete(
            llm_service=llm_service,
            user_id=user_id,
            system_prompt=system_prompt,
            user_prompt=payload_json,
            scope_ref=eff_scope,
            purpose=f"assist.{task}",
            request_model=resolved_model,
            response_schema=schema,
            slot="background",
            timeout_s=timeout_s,
            deadline=deadline,
            workspace_id=workspace_id,
            thread_id=thread_id,
            resend_indeterminate=_resend_for(resolved_model),
            raise_on_error=False,
            persist_raw=persist_raw,
            skip_journal=skip_journal,
        )
        def _should_fall_back(c: Any, m: str) -> bool:
            if c.ok:
                return False
            if _is_safe_to_fallback(c):
                return True
            # Past the deadline nothing new can be sent, but a fallback may still have a
            # journalled answer from an earlier run: let the chain look (replay only).
            if own_chain and deadline is not None and time.monotonic() >= deadline:
                return True
            return own_chain and _is_subscription(m) and _is_timeout_or_rate_limit(c)

        if fb_list and _should_fall_back(comp, resolved_model):
            primary_model = resolved_model
            for fb_cand in fb_list:
                if fb_cand == primary_model:
                    continue
                logger.info(
                    "[DaltonAssist] task=%s primary_model=%s failed (%s); switching to assist_fallback=%s",
                    task,
                    resolved_model,
                    comp.error or comp.status,
                    fb_cand,
                )
                comp_fb = await dalton_complete(
                    llm_service=llm_service,
                    user_id=user_id,
                    system_prompt=system_prompt,
                    user_prompt=payload_json,
                    scope_ref=f"{eff_scope}:fb:{fb_cand}",
                    purpose=f"assist.{task}",
                    request_model=fb_cand,
                    response_schema=schema,
                    slot="background",
                    timeout_s=timeout_s,
                    deadline=deadline,
                    workspace_id=workspace_id,
                    thread_id=thread_id,
                    resend_indeterminate=_resend_for(fb_cand),
                    raise_on_error=False,
                    persist_raw=persist_raw,
                    skip_journal=skip_journal,
                )
                comp = comp_fb
                resolved_model = fb_cand
                if comp_fb.ok or not _should_fall_back(comp_fb, fb_cand):
                    break

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        used_model = comp.model or resolved_model
        is_cached = bool(getattr(comp, "cached", False) or getattr(comp, "replayed", False))
        is_replayed = bool(getattr(comp, "replayed", False) or getattr(comp, "cached", False))

        if not comp.ok:
            logger.warning(
                "[DaltonAssist] task=%s model=%s elapsed_ms=%d cost_usd=%.6f status=%s error=%s",
                task,
                used_model,
                elapsed_ms,
                comp.cash_usd,
                comp.status,
                comp.error,
            )
            return AssistResult(
                ok=False,
                data=None,
                error=comp.error or comp.status,
                model=used_model,
                cost_usd=comp.cash_usd,
                cached=is_cached,
                replayed=is_replayed,
                status=comp.status,
                usage=dict(getattr(comp, "usage", None) or {}),
                error_kind=getattr(comp, "error_kind", None),
            )

        parsed = _extract_json_dict(comp.data, comp.text)
        if isinstance(schema, dict) and schema:
            parsed = _normalize_schema_enums(parsed, schema)
            jsonschema.validate(instance=parsed, schema=schema)

        logger.info(
            "[DaltonAssist] task=%s model=%s elapsed_ms=%d cost_usd=%.6f cached=%s status=ok",
            task,
            used_model,
            elapsed_ms,
            comp.cash_usd,
            is_cached,
        )
        return AssistResult(
            ok=True,
            data=parsed,
            error=None,
            model=used_model,
            cost_usd=comp.cash_usd,
            cached=is_cached,
            replayed=is_replayed,
            status="ok",
            usage=dict(getattr(comp, "usage", None) or {}),
            error_kind=getattr(comp, "error_kind", None),
        )
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        safe_err = sanitize_error_text(exc, max_len=200)
        err_model = (
            (comp.model if comp is not None and getattr(comp, "model", None) else resolved_model)
            or resolved_model
        )
        err_cached = (
            bool(getattr(comp, "cached", False) or getattr(comp, "replayed", False))
            if comp is not None
            else False
        )
        err_replayed = (
            bool(getattr(comp, "replayed", False) or getattr(comp, "cached", False))
            if comp is not None
            else False
        )
        err_usage = dict(getattr(comp, "usage", None) or {}) if comp is not None else {}
        err_cost = float(getattr(comp, "cash_usd", 0.0) or 0.0) if comp is not None else 0.0
        logger.warning(
            "[DaltonAssist] task=%s model=%s elapsed_ms=%d status=error error=%s",
            task,
            err_model,
            elapsed_ms,
            safe_err,
        )
        return AssistResult(
            ok=False,
            data=None,
            error=safe_err,
            model=err_model,
            cost_usd=err_cost,
            cached=err_cached,
            replayed=err_replayed,
            status="error",
            usage=err_usage,
            error_kind=type(exc).__name__,
        )
