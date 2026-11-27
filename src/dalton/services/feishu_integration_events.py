"""Feishu personal-integration revocation event verification & processor (§4.2(3), §4.5(3), §8.3).

Handles both the standalone public webhook endpoint
``POST /api/v1/dalton/integrations/feishu/events`` (works when ``CHANNEL_GATEWAY`` is
disabled in Standalone Core) and forwarded events from the Feishu channel gateway.
Credentials (``DALTON_FEISHU_ENCRYPT_KEY``, ``DALTON_FEISHU_VERIFICATION_TOKEN``) are
read exclusively via ``get_credential_group("feishu_event")``.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import hmac
import json
import logging
import time
from typing import Any

from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from src.dalton.database import user_integrations as db
from src.dalton.database.user_integrations import TriggerReason
from src.dalton.deployment_credentials.catalog import (
    FEISHU_APP_ID_KEY,
    FEISHU_ENCRYPT_KEY,
    FEISHU_VERIFICATION_TOKEN_KEY,
)
from src.dalton.deployment_credentials.snapshot import get_credential_group
from src.dalton.integrations.constants import FEISHU_REVOKED_EVENT_TYPE
from src.dalton.services.user_integrations_lifecycle import (
    DuplicateWebhookEventError,
    IntegrationNotFoundError,
    begin_disconnect,
    begin_disconnect_all_services,
    finish_disconnect,
)

logger = logging.getLogger(__name__)

FEISHU_EVENT_REPLAY_WINDOW_SECONDS = 300  # 5-minute replay protection (§4.2(3) item 3)

_ALLOWED_USER_REVOKE_VALUES: frozenset[str] = frozenset(
    {
        "user",
        "user_revoked",
        "user_revoke",
        "user_cancel",
        "user_cancelled",
        "user_action",
        "self",
        "self_revoked",
        "self_revoke",
    }
)


class FeishuEventAuthError(PermissionError):
    """Raised when Feishu event signature, timestamp, decryption, or token fails."""

    def __init__(self, code: str, message: str, *, status_code: int = 401) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class FeishuEventNotConfiguredError(RuntimeError):
    """Raised when ``feishu_event`` credentials are not configured (maps to HTTP 503)."""

    code = "feishu_event_not_configured"
    status_code = 503


@dataclass(frozen=True, slots=True)
class FeishuEventProcessResult:
    status: str  # challenge | purged | duplicate | ignored
    challenge: str | None = None
    event_id: str | None = None
    reason: str | None = None
    purge_ids: tuple[str, ...] = field(default_factory=tuple)


def compute_feishu_event_signature(
    *,
    timestamp: str,
    nonce: str,
    encrypt_key: str,
    raw_body: bytes,
) -> str:
    """Compute official Feishu ``X-Lark-Signature`` SHA-256 hex digest."""
    prefix = f"{timestamp}{nonce}{encrypt_key}".encode("utf-8")
    return hashlib.sha256(prefix + raw_body).hexdigest()


def encrypt_feishu_event_payload(plaintext_json: str | bytes, encrypt_key: str) -> str:
    """Encrypt a Feishu event JSON body using official AES-256-CBC + PKCS7 (test & utility helper)."""
    import os

    raw_bytes = (
        plaintext_json.encode("utf-8")
        if isinstance(plaintext_json, str)
        else bytes(plaintext_json)
    )
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    iv = os.urandom(16)
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(raw_bytes) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ct = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(iv + ct).decode("ascii")


def decrypt_feishu_event_payload(encrypt_b64: str, encrypt_key: str) -> dict[str, Any]:
    """Decrypt Feishu AES-256-CBC ``{"encrypt": "..."}`` envelope into a JSON dict."""
    try:
        raw = base64.b64decode(encrypt_b64, validate=True)
        if len(raw) <= 16 or (len(raw) - 16) % 16 != 0:
            raise ValueError("invalid ciphertext length")
        iv = raw[:16]
        ct = raw[16:]
        key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        padded = decryptor.update(ct) + decryptor.finalize()
        unpadder = sym_padding.PKCS7(128).unpadder()
        plain = unpadder.update(padded) + unpadder.finalize()
        parsed = json.loads(plain.decode("utf-8"))
    except Exception:
        raise FeishuEventAuthError(
            "feishu_event_decrypt_failed",
            "Failed to decrypt Feishu event payload.",
            status_code=400,
        ) from None
    if not isinstance(parsed, dict):
        raise FeishuEventAuthError(
            "feishu_event_malformed",
            "Decrypted Feishu event payload is not a JSON object.",
            status_code=400,
        )
    return parsed


def verify_and_decode_feishu_event(
    *,
    raw_body: bytes,
    headers: Mapping[str, str],
    now_epoch_s: float | None = None,
) -> dict[str, Any]:
    """Verify Feishu webhook signature / replay window / decryption / verification token.

    Reads credentials exclusively via ``get_credential_group("feishu_event")``.
    """
    cred = get_credential_group("feishu_event")
    if not cred.ready:
        raise FeishuEventNotConfiguredError(
            "管理员尚未配置飞书事件验签凭据（Encrypt Key / Verification Token）。"
        )

    encrypt_key = cred.values.get(FEISHU_ENCRYPT_KEY)
    verification_token = cred.values.get(FEISHU_VERIFICATION_TOKEN_KEY)

    lower_headers = {str(k).lower(): str(v) for k, v in headers.items()}

    if encrypt_key:
        ts_raw = lower_headers.get("x-lark-request-timestamp", "").strip()
        nonce = lower_headers.get("x-lark-request-nonce", "").strip()
        sig = lower_headers.get("x-lark-signature", "").strip()
        if not ts_raw or not nonce or not sig:
            raise FeishuEventAuthError(
                "missing_signature_headers",
                "Missing required Feishu signature headers.",
                status_code=401,
            )
        try:
            ts_int = int(ts_raw)
        except ValueError:
            raise FeishuEventAuthError(
                "invalid_timestamp",
                "Invalid Feishu request timestamp header.",
                status_code=401,
            ) from None

        now_ts = time.time() if now_epoch_s is None else float(now_epoch_s)
        if abs(now_ts - ts_int) > FEISHU_EVENT_REPLAY_WINDOW_SECONDS:
            raise FeishuEventAuthError(
                "timestamp_out_of_window",
                "Feishu event timestamp is outside the 5-minute replay window.",
                status_code=401,
            )

        expected_sig = compute_feishu_event_signature(
            timestamp=ts_raw,
            nonce=nonce,
            encrypt_key=encrypt_key,
            raw_body=raw_body,
        )
        if not hmac.compare_digest(expected_sig.lower(), sig.lower()):
            raise FeishuEventAuthError(
                "invalid_signature",
                "Feishu event signature verification failed.",
                status_code=401,
            )

    try:
        envelope = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise FeishuEventAuthError(
            "invalid_json",
            "Feishu event body is not valid JSON.",
            status_code=400,
        ) from None
    if not isinstance(envelope, dict):
        raise FeishuEventAuthError(
            "invalid_json",
            "Feishu event body must be a JSON object.",
            status_code=400,
        )

    if "encrypt" in envelope:
        if not encrypt_key:
            raise FeishuEventAuthError(
                "encrypt_key_not_configured",
                "Encrypted Feishu event received without configured Encrypt Key.",
                status_code=400,
            )
        enc_val = envelope.get("encrypt")
        if not isinstance(enc_val, str) or not enc_val.strip():
            raise FeishuEventAuthError(
                "invalid_encrypt_field",
                "Encrypted Feishu event envelope is malformed.",
                status_code=400,
            )
        payload = decrypt_feishu_event_payload(enc_val.strip(), encrypt_key)
    else:
        payload = envelope

    if verification_token:
        header_obj = payload.get("header")
        body_token = (
            header_obj.get("token")
            if isinstance(header_obj, Mapping) and header_obj.get("token")
            else payload.get("token")
        )
        if not isinstance(body_token, str) or not hmac.compare_digest(
            body_token.strip(), verification_token
        ):
            raise FeishuEventAuthError(
                "invalid_verification_token",
                "Feishu event verification token mismatch.",
                status_code=401,
            )

    return payload


def _parse_event_timestamp(payload: Mapping[str, Any]) -> datetime | None:
    event = payload.get("event") if isinstance(payload.get("event"), Mapping) else {}
    header = payload.get("header") if isinstance(payload.get("header"), Mapping) else {}
    raw = (
        event.get("revoke_time")
        or event.get("revoked_at")
        or header.get("create_time")
        or payload.get("ts")
    )
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
    s = str(raw).strip()
    if not s:
        return None
    try:
        num = float(s)
        if num >= 1e12:
            num = num / 1000.0
        return datetime.fromtimestamp(num, tz=UTC)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def _is_user_initiated_revocation(event: Mapping[str, Any]) -> bool:
    """Return True iff the event represents user-initiated revocation (§4.5(3)).

    When the payload includes an explicit reason/operator field, only user-self
    revocation markers are admitted into Scheme 2A purge. When no reason field is
    present on an official ``auth.user_access_token.revoked_v4`` event, default to True.
    """
    for key in ("revoke_reason", "reason", "operator_type", "revoke_type"):
        val = event.get(key)
        if val is None:
            continue
        cleaned = str(val).strip().lower()
        if not cleaned:
            continue
        if cleaned not in _ALLOWED_USER_REVOKE_VALUES:
            return False
    return True


def _extract_feishu_ids(event: Mapping[str, Any]) -> tuple[str | None, str | None]:
    open_id = event.get("open_id")
    union_id = event.get("union_id")
    user_id_obj = event.get("user_id")
    if isinstance(user_id_obj, Mapping):
        open_id = open_id or user_id_obj.get("open_id")
        union_id = union_id or user_id_obj.get("union_id")
    oid = str(open_id).strip() if isinstance(open_id, str) and open_id.strip() else None
    uid = str(union_id).strip() if isinstance(union_id, str) and union_id.strip() else None
    return oid, uid


async def process_feishu_integration_event(
    payload: Mapping[str, Any],
    *,
    execute_purge: bool = True,
) -> FeishuEventProcessResult:
    """Unified processor for decoded Feishu events (standalone endpoint + channel gateway forwarding).

    1. Answers ``url_verification`` challenges.
    2. Ignores events other than ``auth.user_access_token.revoked_v4``.
    3. Filters by user-initiated revocation reason and ``event_time >= row.authorized_at``.
    4. Atomically deduplicates ``event_id`` and registers Phase 1 + Scheme 2A purge in one transaction.
    """
    if str(payload.get("type") or "").strip() == "url_verification":
        challenge = str(payload.get("challenge") or "")
        return FeishuEventProcessResult(status="challenge", challenge=challenge)

    header = payload.get("header") if isinstance(payload.get("header"), Mapping) else {}
    event = payload.get("event") if isinstance(payload.get("event"), Mapping) else {}

    event_type = str(header.get("event_type") or payload.get("event_type") or "").strip()
    event_id = str(header.get("event_id") or payload.get("uuid") or "").strip() or None

    if event_type != FEISHU_REVOKED_EVENT_TYPE:
        return FeishuEventProcessResult(
            status="ignored",
            event_id=event_id,
            reason="unsupported_event_type",
        )

    if not _is_user_initiated_revocation(event):
        logger.info(
            "[feishu-events] ignoring non-user revocation event_id=%s",
            event_id,
        )
        return FeishuEventProcessResult(
            status="ignored",
            event_id=event_id,
            reason="non_user_revocation",
        )

    open_id, union_id = _extract_feishu_ids(event)
    if not open_id and not union_id:
        return FeishuEventProcessResult(
            status="ignored",
            event_id=event_id,
            reason="missing_user_identity",
        )

    app_id = str(header.get("app_id") or event.get("app_id") or "").strip()
    if not app_id:
        feishu_app = get_credential_group("feishu_app")
        if feishu_app.ready:
            app_id = feishu_app.values.get(FEISHU_APP_ID_KEY, "")
    if not app_id:
        return FeishuEventProcessResult(
            status="ignored",
            event_id=event_id,
            reason="missing_app_id",
        )

    matches = await db.list_connections_by_provider_account(
        "feishu",
        app_id,
        provider_account_id=open_id,
        provider_union_id=union_id,
    )
    if not matches:
        return FeishuEventProcessResult(
            status="ignored",
            event_id=event_id,
            reason="no_matching_connection",
        )

    event_time = _parse_event_timestamp(payload)
    eligible: list[db.IntegrationRow] = []
    for row in matches:
        if event_time is not None and row.authorized_at is not None:
            auth_at = (
                row.authorized_at
                if row.authorized_at.tzinfo is not None
                else row.authorized_at.replace(tzinfo=UTC)
            )
            if event_time < auth_at:
                logger.info(
                    "[feishu-events] skipping stale revocation event_id=%s for user=%s service=%s (event_time < authorized_at)",
                    event_id,
                    row.user_id,
                    row.service,
                )
                continue
        eligible.append(row)

    if not eligible:
        return FeishuEventProcessResult(
            status="ignored",
            event_id=event_id,
            reason="stale_before_authorized_at",
        )

    # Group eligible connections by user_id
    by_user: dict[str, list[db.IntegrationRow]] = {}
    for row in eligible:
        by_user.setdefault(row.user_id, []).append(row)

    purge_ids: list[str] = []
    event_id_consumed_in_tx = False
    for uid, rows in by_user.items():
        all_user_feishu = await db.list_user_connections(uid, provider="feishu")
        # Pass event_id on the first user's atomic Phase 1 transaction so dedup + block + ledger commit together
        tx_event_id = event_id if not event_id_consumed_in_tx else None
        try:
            if len(rows) == len(all_user_feishu) and len(rows) > 1:
                pids = await begin_disconnect_all_services(
                    uid,
                    "feishu",
                    trigger_reason=TriggerReason.WEBHOOK_REVOKED,
                    remote_mode="local_only",
                    event_id=tx_event_id,
                )
                purge_ids.extend(pids)
                event_id_consumed_in_tx = True
            else:
                for idx, row in enumerate(rows):
                    pid = await begin_disconnect(
                        uid,
                        "feishu",
                        row.service,
                        trigger_reason=TriggerReason.WEBHOOK_REVOKED,
                        remote_mode="local_only",
                        event_id=tx_event_id if idx == 0 else None,
                    )
                    purge_ids.append(pid)
                    event_id_consumed_in_tx = True
        except DuplicateWebhookEventError:
            return FeishuEventProcessResult(
                status="duplicate",
                event_id=event_id,
                reason="duplicate_event_id",
            )
        except IntegrationNotFoundError:
            continue

    if execute_purge:
        for pid in purge_ids:
            await finish_disconnect(pid, revoker=None)

    return FeishuEventProcessResult(
        status="purged" if purge_ids else "ignored",
        event_id=event_id,
        purge_ids=tuple(purge_ids),
    )


__all__ = [
    "FEISHU_EVENT_REPLAY_WINDOW_SECONDS",
    "FeishuEventAuthError",
    "FeishuEventNotConfiguredError",
    "FeishuEventProcessResult",
    "compute_feishu_event_signature",
    "decrypt_feishu_event_payload",
    "encrypt_feishu_event_payload",
    "process_feishu_integration_event",
    "verify_and_decode_feishu_event",
]
