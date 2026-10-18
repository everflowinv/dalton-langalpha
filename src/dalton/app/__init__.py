"""Dalton FastAPI router package."""

from __future__ import annotations

from fastapi import APIRouter

from src.dalton.app.oauth_native import router as _oauth_native_router
from src.dalton.app.router import router as _dalton_api_router
from src.dalton.settings import log_dalton_startup_warnings

log_dalton_startup_warnings()

dalton_router = APIRouter()
dalton_router.include_router(_dalton_api_router)
dalton_router.include_router(_oauth_native_router)

__all__ = ["dalton_router"]
