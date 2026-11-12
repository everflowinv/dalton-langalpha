"""Service-token route that returns an automation notification plan for one delivery id."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from src.dalton.app.channels_router import require_channel_service_token
from src.dalton.database.automation_notifications import (
    authorize_automation_notification_plan,
)

automation_notifications_router = APIRouter(
    prefix="/channels/internal/automation-notifications",
    tags=["Dalton Channel Automation Notifications"],
    dependencies=[Depends(require_channel_service_token)],
)


class AutomationNotificationPlanRequest(BaseModel):
    """Strict request identifying only ``delivery_id``. Card JSON and paths are forbidden."""

    model_config = ConfigDict(extra="forbid")

    delivery_id: UUID = Field(...)


@automation_notifications_router.post("/plan")
async def internal_automation_notification_plan_endpoint(
    body: AutomationNotificationPlanRequest,
) -> dict[str, Any]:
    """Authorize ``delivery_id`` and return the saved card or text plan."""
    return await authorize_automation_notification_plan(
        delivery_id=str(body.delivery_id),
    )
