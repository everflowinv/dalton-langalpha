"""Internal backend endpoint for the daily subscription model availability watch task."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from src.dalton.app.channels_router import require_channel_service_token
from src.dalton.services.model_availability_watch import run_model_availability_check

logger = logging.getLogger(__name__)

internal_model_availability_router = APIRouter(
    prefix="/internal/model-availability",
    tags=["Dalton Internal Model Availability"],
)


@internal_model_availability_router.post("/check")
async def internal_model_availability_check(
    _: None = Depends(require_channel_service_token),
) -> dict[str, Any]:
    """Run ``available_models(..., refresh=True)`` across connected subscriptions and probe Codex version."""
    try:
        return await run_model_availability_check()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
