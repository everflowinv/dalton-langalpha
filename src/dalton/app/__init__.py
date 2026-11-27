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
from src.dalton.auth.local_router import register_local_auth_routers
# DALTON-HOOK(monitors): mount monitor panel endpoints under /api/v1/dalton/monitors
from src.dalton.monitors.router import router as _monitors_router
from src.dalton.profile import is_core_profile
from src.dalton.settings import log_dalton_startup_warnings

log_dalton_startup_warnings()

dalton_router = APIRouter()
dalton_router.include_router(_dalton_api_router)
dalton_router.include_router(_watchlist_router)
dalton_router.include_router(_oauth_native_router)
dalton_router.include_router(_public_og_router)
dalton_router.include_router(_transcriptions_router)
if is_core_profile():
    dalton_router.include_router(_chat_uploads_sandbox_relay_router)
dalton_router.include_router(_chat_uploads_router)
# DALTON-HOOK(monitors): include monitor panel router on dalton_router
dalton_router.include_router(_monitors_router)
register_local_auth_routers(dalton_router)

__all__ = ["dalton_router"]
