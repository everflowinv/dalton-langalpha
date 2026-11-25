"""Standalone Dalton transcription service (`dalton-transcription`, default `:8965`).

Note: Keep top-level imports stdlib-only so host bridge scripts running under
minimal system Python (without FastAPI/boto3) can safely import
``src.dalton.transcription_service.owner_lock`` without pulling optional
web/storage dependencies.
"""

from __future__ import annotations

from .owner_lock import (
    DEFAULT_OWNER_LOCK_TIMEOUT_S,
    OWNER_LOCK_FILENAME,
    AsrOwnerLock,
    OwnerLockBusyError,
    OwnerLockMetadata,
    read_owner_lock_metadata,
)

__all__ = [
    "AsrOwnerLock",
    "DEFAULT_OWNER_LOCK_TIMEOUT_S",
    "OWNER_LOCK_FILENAME",
    "OwnerLockBusyError",
    "OwnerLockMetadata",
    "read_owner_lock_metadata",
]
