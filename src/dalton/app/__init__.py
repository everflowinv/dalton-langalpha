"""Dalton FastAPI router package."""

from __future__ import annotations

from fastapi import APIRouter

from src.dalton.app.chat_uploads import (
    router as _chat_uploads_router,
    sandbox_relay_router as _chat_uploads_sandbox_relay_router,
)
from src.dalton.app.oauth_native import router as _oauth_native_router
from src.dalton.app.public_og import router as _public_og_router
from src.dalton.app.router import router as _dalton_api_router
from src.dalton.app.transcriptions import router as _transcriptions_router
from src.dalton.app.watchlist import router as _watchlist_router
from src.dalton.app.workspace_file_download import (
    router as _workspace_file_download_router,
)
from src.dalton.auth.local_router import register_local_auth_routers
from src.dalton.deployment_credentials.router import (
    router as _deployment_credentials_router,
)
# DALTON-HOOK(monitors): mount monitor panel endpoints under /api/v1/dalton/monitors
from src.dalton.monitors.router import router as _monitors_router
from src.dalton.download_ticket import install_download_ticket_log_filter
# DALTON-HOOK(personal-integrations): mount personal calendar/mail OAuth & revocation endpoints
from src.dalton.app.integrations_router import router as _integrations_router
from src.dalton.app.tracker_calendar_router import router as _tracker_calendar_router
from src.dalton.profile import is_core_profile
from src.dalton.settings import log_dalton_startup_warnings

log_dalton_startup_warnings()
install_download_ticket_log_filter()

dalton_router = APIRouter()
dalton_router.include_router(_dalton_api_router)
dalton_router.include_router(_watchlist_router)
dalton_router.include_router(_oauth_native_router)
dalton_router.include_router(_public_og_router)
dalton_router.include_router(_transcriptions_router)
if is_core_profile():
    dalton_router.include_router(_chat_uploads_sandbox_relay_router)
dalton_router.include_router(_chat_uploads_router)
dalton_router.include_router(_workspace_file_download_router)
# DALTON-HOOK(monitors): include monitor panel router on dalton_router
dalton_router.include_router(_monitors_router)
dalton_router.include_router(_deployment_credentials_router)
# DALTON-HOOK(personal-integrations): include personal integrations router on dalton_router
dalton_router.include_router(_integrations_router)
dalton_router.include_router(_tracker_calendar_router)
register_local_auth_routers(dalton_router)

__all__ = ["dalton_router"]
