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
from dataclasses import dataclass
from typing import Any

import jsonschema
import yaml

from src.config.env import LOCAL_DEV_USER_ID
from src.dalton.database.provider_events import sanitize_error_text
from src.dalton.llm.complete import dalton_complete
from src.dalton.settings import DALTON_YAML_PATH, REPO_ROOT

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
    status: str = "ok"


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
    4. `'gemini-3.8-flash-low-oauth'` if present in merged model catalog, else `'deepseek-flash'`

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

    try:
        from src.llms.llm import LLM

        catalog = LLM.get_model_config().llm_config or {}
        if "gemini-3.8-flash-low-oauth" in catalog:
            return "gemini-3.8-flash-low-oauth"
    except Exception:
        pass

    return "deepseek-flash"


def resolve_assist_fallback_model(role: str = "assist_fallback") -> str | None:
    """Resolve fallback model for `assist_json` from `llm.assist_fallback` or `models.assist_fallback_model` (P0J.J2)."""
    for cfg in (_read_yaml_dict(REPO_ROOT / "agent_config.dalton.yaml"), _read_yaml_dict(DALTON_YAML_PATH)):
        for block_key, field_key in (("llm", "assist_fallback"), ("models", "assist_fallback_model")):
            block = cfg.get(block_key)
            val = block.get(field_key) if isinstance(block, dict) else None
            if isinstance(val, str) and val.strip():
                return _validate_assist_candidate(role, val)
    return None


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


async def assist_json(
    task: str,
    instructions: str,
    payload: dict[str, Any],
    schema: dict[str, Any],
    *,
    user_id: str = LOCAL_DEV_USER_ID,
    role: str = "assist",
    scope_ref: str | None = None,
    timeout_s: float = 60.0,
    model: str | None = None,
    llm_service: Any = None,
    workspace_id: str | None = None,
    thread_id: str | None = None,
) -> AssistResult:
    """Invoke a cheap/subscription LLM for structured JSON judgment, cached in Postgres.

    Never raises: returns `AssistResult(ok=False, data=None, error=...)` on any error.
    """
    t0 = time.monotonic()
    resolved_model = str(model or "")
    user_id = str(user_id or LOCAL_DEV_USER_ID).strip()

    try:
        resolved_model = resolve_assist_model(role=role, explicit_model=model)
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
            workspace_id=workspace_id,
            thread_id=thread_id,
            raise_on_error=False,
        )
        if not comp.ok and model is None and comp.status != "timeout":
            fb_model = resolve_assist_fallback_model()
            if fb_model and fb_model != resolved_model:
                logger.info(
                    "[DaltonAssist] task=%s primary_model=%s failed (%s); switching to assist_fallback=%s",
                    task,
                    resolved_model,
                    comp.error or comp.status,
                    fb_model,
                )
                fb_cands = (fb_model, "ocb-deepseek-deepseek-flash") if fb_model == "deepseek-flash" else (fb_model,)
                for fb_cand in fb_cands:
                    if fb_cand == resolved_model:
                        continue
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
                        workspace_id=workspace_id,
                        thread_id=thread_id,
                        raise_on_error=False,
                    )
                    if comp_fb.ok or comp_fb.error_kind != "pre_send":
                        comp = comp_fb
                        resolved_model = fb_cand
                        break

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        used_model = comp.model or resolved_model

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
                cached=comp.cached,
                status=comp.status,
            )

        parsed = _extract_json_dict(comp.data, comp.text)
        if isinstance(schema, dict) and schema:
            jsonschema.validate(instance=parsed, schema=schema)

        logger.info(
            "[DaltonAssist] task=%s model=%s elapsed_ms=%d cost_usd=%.6f cached=%s status=ok",
            task,
            used_model,
            elapsed_ms,
            comp.cash_usd,
            comp.cached,
        )
        return AssistResult(
            ok=True,
            data=parsed,
            error=None,
            model=used_model,
            cost_usd=comp.cash_usd,
            cached=comp.cached,
            status="ok",
        )
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        safe_err = sanitize_error_text(exc, max_len=200)
        logger.warning(
            "[DaltonAssist] task=%s model=%s elapsed_ms=%d status=error error=%s",
            task,
            resolved_model,
            elapsed_ms,
            safe_err,
        )
        return AssistResult(
            ok=False,
            data=None,
            error=safe_err,
            model=resolved_model,
            cost_usd=0.0,
            cached=False,
            status="error",
        )
