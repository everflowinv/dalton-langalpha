"""Internal LLM completion endpoint for Dalton host-side data tasks.

Allows trusted host scripts (without direct OpenClaw gateway access) to call
subscription/OAuth models through ``dalton_complete`` under the PM user's
background budget gate and journal.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, create_model

from src.dalton.app.channels_router import require_channel_service_token
from src.dalton.llm.complete import dalton_complete
from src.dalton.settings import get_configured_pm_user_ids, get_dalton_config

logger = logging.getLogger(__name__)

internal_llm_router = APIRouter(
    prefix="/internal/llm",
    tags=["Dalton Internal LLM"],
)


def get_allowed_host_oauth_models() -> frozenset[str]:
    """Return OAuth model allowlist configured under `internal_llm.allowed_models` in `dalton.yaml`."""
    raw = get_dalton_config().internal_llm.allowed_models
    return frozenset(str(m).strip() for m in raw if str(m).strip())


class InternalLLMCompleteRequest(BaseModel):
    """Request body for ``POST /api/v1/dalton/internal/llm/complete``."""

    model: str = Field(..., min_length=1)
    system_prompt: str | None = None
    user_prompt: str = Field(..., min_length=1)
    response_schema: dict[str, Any] | None = None
    purpose: str = Field(default="host_data_task", min_length=1, max_length=128)
    timeout_s: float = Field(default=180.0, gt=0.0, le=1800.0)


class InternalLLMCompleteResponse(BaseModel):
    """Normalized completion envelope returned to host-side data tasks."""

    ok: bool
    text: str | None = None
    model: str
    status: Literal[
        "done",
        "indeterminate",
        "in_flight",
        "budget_exhausted",
        "failed",
        "conflict",
    ]
    error: str | None = None


def _json_schema_node_to_type(
    schema: dict[str, Any], *, model_name: str
) -> Any:
    """Recursively map JSON Schema (lowercase or Gemini uppercase) to Python types."""
    schema_type = str(schema.get("type", "")).strip().lower()
    if schema_type == "string":
        return str
    if schema_type == "integer":
        return int
    if schema_type == "number":
        return float
    if schema_type == "boolean":
        return bool
    if schema_type == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            item_type = _json_schema_node_to_type(
                items, model_name=f"{model_name}Item"
            )
            return list[item_type]  # type: ignore[valid-type]
        return list[Any]
    if schema_type == "object" or isinstance(schema.get("properties"), dict):
        props = schema.get("properties")
        if not isinstance(props, dict) or not props:
            return dict[str, Any]
        required_raw = schema.get("required")
        required = set(required_raw) if isinstance(required_raw, list) else set()
        fields: dict[str, tuple[Any, Any]] = {}
        for key, prop_schema in props.items():
            if not isinstance(key, str) or not isinstance(prop_schema, dict):
                raise ValueError(f"Invalid property definition in response_schema: {key!r}")
            field_type = _json_schema_node_to_type(
                prop_schema, model_name=f"{model_name}_{key}"
            )
            if key in required:
                fields[key] = (field_type, ...)
            else:
                fields[key] = (field_type | None, None)
        return create_model(model_name, **fields)  # type: ignore[call-overload]
    return Any


def json_schema_to_pydantic_model(
    schema: dict[str, Any],
) -> type[BaseModel]:
    """Build a dynamic Pydantic BaseModel from a root object JSON Schema."""
    if not isinstance(schema, dict):
        raise ValueError("response_schema must be a JSON object")
    built = _json_schema_node_to_type(schema, model_name="HostTaskResponse")
    if isinstance(built, type) and issubclass(built, BaseModel):
        return built
    raise ValueError("response_schema root must define an object with properties")


def _format_error_message(error: str | None, error_kind: str | None, status: str) -> str:
    """Include error category in the returned error string when a call fails."""
    category = (error_kind or status or "failed").strip()
    detail = (error or "").strip()
    if detail and category.lower() in detail.lower():
        return detail
    if detail:
        return f"{category}: {detail}"
    return category


def _serialize_result_text(text: str | None, data: Any) -> str | None:
    if isinstance(text, str) and text.strip():
        return text
    if data is not None:
        if isinstance(data, BaseModel):
            return data.model_dump_json()
        return json.dumps(data, ensure_ascii=False)
    return text


@internal_llm_router.post("/complete", response_model=InternalLLMCompleteResponse)
async def internal_llm_complete(
    payload: InternalLLMCompleteRequest,
    _: None = Depends(require_channel_service_token),
) -> InternalLLMCompleteResponse:
    """Run a host data task completion via ``dalton_complete`` for whitelisted OAuth models."""
    requested_model = payload.model.strip()
    allowed_models = get_allowed_host_oauth_models()
    if requested_model not in allowed_models:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model {requested_model!r} is not allowed on internal host LLM endpoint. "
                f"Allowed: {sorted(allowed_models)}"
            ),
        )

    schema_model: type[BaseModel] | None = None
    if payload.response_schema is not None:
        try:
            schema_model = json_schema_to_pydantic_model(payload.response_schema)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    pm_user_ids = get_configured_pm_user_ids()
    if not pm_user_ids:
        raise HTTPException(
            status_code=503,
            detail="DALTON_PM_USER_IDS is not configured for internal LLM calls.",
        )

    result = await dalton_complete(
        user_id=pm_user_ids[0],
        system_prompt=payload.system_prompt,
        user_prompt=payload.user_prompt,
        model=requested_model,
        response_schema=schema_model,
        slot="background",
        purpose=payload.purpose,
        timeout_s=payload.timeout_s,
    )

    if result.ok:
        return InternalLLMCompleteResponse(
            ok=True,
            text=_serialize_result_text(result.text, result.data),
            model=result.model or requested_model,
            status=result.status,  # type: ignore[arg-type]
            error=None,
        )

    return InternalLLMCompleteResponse(
        ok=False,
        text=_serialize_result_text(result.text, result.data),
        model=result.model or requested_model,
        status=result.status,  # type: ignore[arg-type]
        error=_format_error_message(result.error, result.error_kind, result.status),
    )
