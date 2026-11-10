"""Data-task report notification transport to the dedicated Dalton channel gateway.

Sends only ``(run_id, target)`` authenticated with ``INTERNAL_SERVICE_TOKEN`` to the
gateway's ``POST /internal/data-task-notifications`` endpoint and preserves uncertain
delivery outcomes so wrapping orchestration never converts an unknown send into ``failed``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import http.cookiejar
import logging
from typing import Any

import httpx

from src.dalton.settings import (
    get_dalton_channel_gateway_url,
    get_internal_service_token,
    get_notify_http_timeout_s,
    redact_managed_tokens,
)

logger = logging.getLogger(__name__)

DATA_TASK_GATEWAY_PATH = "/internal/data-task-notifications"

UNCERTAIN_NOTIFY_ERROR_CODES: frozenset[str] = frozenset(
    {
        "delivery_uncertain",
        "unknown",
        "in_flight",
        "partial",
        "gateway_restarted",
    }
)


def is_uncertain_notify_error_code(error_code: str | None) -> bool:
    """Return ``True`` when ``error_code`` represents an indeterminate send that must not auto-resend."""
    cleaned = str(error_code or "").strip().lower()
    return cleaned in UNCERTAIN_NOTIFY_ERROR_CODES


def extract_target_uncertain_code(target: str, transport_res: Any) -> str | None:
    """Return an uncertain-delivery code from ``transport_res`` for ``target``, or ``None``."""
    if not isinstance(transport_res, Mapping):
        return None
    cleaned_target = str(target or "").strip()
    results = transport_res.get("results")
    if isinstance(results, list):
        for entry in results:
            if not isinstance(entry, Mapping):
                continue
            if str(entry.get("target") or "").strip() != cleaned_target:
                continue
            st = str(entry.get("status") or "").strip().lower()
            err = str(entry.get("error_code") or entry.get("code") or entry.get("error") or "").strip().lower()
            if entry.get("uncertain") is True or st in ("unknown", "partial", "in_flight") or is_uncertain_notify_error_code(err):
                return err if is_uncertain_notify_error_code(err) else "delivery_uncertain"

    top_status = str(transport_res.get("status") or "").strip().lower()
    top_code = str(transport_res.get("error_code") or transport_res.get("code") or "").strip().lower()
    if transport_res.get("uncertain") is True or top_status in ("unknown", "partial", "in_flight") or is_uncertain_notify_error_code(top_code):
        return top_code if is_uncertain_notify_error_code(top_code) else "delivery_uncertain"
    return None


def _build_ephemeral_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=False,
        trust_env=False,
        cookies=http.cookiejar.CookieJar(
            policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[])
        ),
    )


async def dispatch_data_task_target_via_gateway(
    *,
    run_id: str,
    target: str,
    gateway_url: str | None = None,
    service_token: str | None = None,
    timeout_s: float | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Dispatch one ``(run_id, target)`` data-task notification through the Dalton channel gateway."""
    cleaned_run_id = str(run_id or "").strip()
    cleaned_target = str(target or "").strip()
    if not cleaned_run_id or not cleaned_target:
        return {
            "ok": False,
            "status": "failed",
            "uncertain": False,
            "status_code": 400,
            "error_code": "invalid_request",
            "results": [
                {
                    "target": cleaned_target,
                    "ok": False,
                    "status": "failed",
                    "error": "invalid_request",
                    "error_code": "invalid_request",
                }
            ],
        }

    eff_token = (
        service_token if service_token is not None else get_internal_service_token()
    ).strip()
    if not eff_token:
        return {
            "ok": False,
            "status": "failed",
            "uncertain": False,
            "status_code": 503,
            "error_code": "service_token_unconfigured",
            "results": [
                {
                    "target": cleaned_target,
                    "ok": False,
                    "status": "failed",
                    "error": "service_token_unconfigured",
                    "error_code": "service_token_unconfigured",
                }
            ],
        }

    eff_base = (
        gateway_url if gateway_url is not None else get_dalton_channel_gateway_url()
    ).strip().rstrip("/")
    if not eff_base:
        return {
            "ok": False,
            "status": "failed",
            "uncertain": False,
            "status_code": 503,
            "error_code": "gateway_url_unconfigured",
            "results": [
                {
                    "target": cleaned_target,
                    "ok": False,
                    "status": "failed",
                    "error": "gateway_url_unconfigured",
                    "error_code": "gateway_url_unconfigured",
                }
            ],
        }

    eff_timeout_s = (
        max(1.0, float(timeout_s))
        if timeout_s is not None
        else max(45.0, get_notify_http_timeout_s() * 2.5)
    )
    req_timeout = httpx.Timeout(eff_timeout_s, connect=min(5.0, eff_timeout_s))
    endpoint = f"{eff_base}{DATA_TASK_GATEWAY_PATH}"
    headers = {
        "X-Service-Token": eff_token,
        "Content-Type": "application/json",
    }
    body = {
        "run_id": cleaned_run_id,
        "target": cleaned_target,
    }

    owns_client = http_client is None
    client = http_client if http_client is not None else _build_ephemeral_client()
    try:
        resp = await client.post(
            endpoint,
            headers=headers,
            json=body,
            timeout=req_timeout,
        )
    except (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.UnsupportedProtocol,
        httpx.InvalidURL,
    ) as conn_exc:
        code = type(conn_exc).__name__
        logger.warning(
            "Data-task gateway unreachable before send: run_id=%s error_type=%s",
            cleaned_run_id,
            code,
        )
        return {
            "ok": False,
            "status": "failed",
            "uncertain": False,
            "status_code": 0,
            "error_code": code,
            "results": [
                {
                    "target": cleaned_target,
                    "ok": False,
                    "status": "failed",
                    "error": code,
                    "error_code": code,
                }
            ],
        }
    except (httpx.TimeoutException, httpx.TransportError) as post_exc:
        logger.warning(
            "Data-task gateway response lost after request start (uncertain): run_id=%s error_type=%s",
            cleaned_run_id,
            type(post_exc).__name__,
        )
        return {
            "ok": False,
            "status": "unknown",
            "uncertain": True,
            "status_code": 0,
            "error_code": "delivery_uncertain",
            "results": [
                {
                    "target": cleaned_target,
                    "ok": False,
                    "status": "unknown",
                    "uncertain": True,
                    "error": "delivery_uncertain",
                    "error_code": "delivery_uncertain",
                }
            ],
        }
    finally:
        if owns_client:
            await client.aclose()

    try:
        parsed = resp.json()
    except ValueError:
        parsed = {}
    if not isinstance(parsed, Mapping):
        parsed = {}

    gw_status = str(parsed.get("status") or "").strip().lower()
    raw_code = str(parsed.get("code") or parsed.get("error_code") or "").strip()
    safe_code = redact_managed_tokens(raw_code)[:96] if raw_code else ""

    if resp.status_code == 200 and gw_status == "sent":
        return {
            "ok": True,
            "status": "sent",
            "uncertain": False,
            "status_code": resp.status_code,
            "results": [
                {
                    "target": cleaned_target,
                    "ok": True,
                    "status": "sent",
                    "items_sent": parsed.get("items_sent"),
                    "items_total": parsed.get("items_total"),
                }
            ],
        }

    if gw_status == "skipped":
        skip_code = safe_code or "skipped"
        return {
            "ok": True,
            "status": "skipped",
            "uncertain": False,
            "status_code": resp.status_code,
            "results": [
                {
                    "target": cleaned_target,
                    "ok": False,
                    "skipped": True,
                    "status": "skipped",
                    "reason": skip_code,
                    "error_code": skip_code,
                }
            ],
        }

    if gw_status in ("unknown", "partial", "in_flight") or is_uncertain_notify_error_code(safe_code):
        uncertain_code = safe_code if is_uncertain_notify_error_code(safe_code) else "delivery_uncertain"
        return {
            "ok": False,
            "status": "unknown",
            "uncertain": True,
            "status_code": resp.status_code,
            "error_code": uncertain_code,
            "results": [
                {
                    "target": cleaned_target,
                    "ok": False,
                    "status": "unknown",
                    "uncertain": True,
                    "error": uncertain_code,
                    "error_code": uncertain_code,
                }
            ],
        }

    fail_code = safe_code or f"http_{resp.status_code}"
    return {
        "ok": False,
        "status": "failed",
        "uncertain": False,
        "status_code": resp.status_code,
        "error_code": fail_code,
        "results": [
            {
                "target": cleaned_target,
                "ok": False,
                "status": "failed",
                "error": fail_code,
                "error_code": fail_code,
            }
        ],
    }


async def post_data_task_gateway_notify(
    targets: Sequence[str],
    text: str = "",
    card: dict[str, Any] | None = None,
    *,
    images: list[dict[str, Any]] | None = None,
    run_id: str | None = None,
    gateway_url: str | None = None,
    service_token: str | None = None,
    timeout_s: float | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Dispatch data-task report targets via ``POST /internal/data-task-notifications``.

    Caller-provided ``text``, ``card``, and ``images`` arguments are intentionally ignored
    on the wire: the gateway fetches the validated delivery plan directly from the backend
    by ``(run_id, target)``.
    """
    _ = (text, card, images)
    cleaned_run_id = str(run_id or "").strip()
    aggregated_results: list[dict[str, Any]] = []
    all_ok = True
    any_uncertain = False
    last_status_code = 200
    first_error_code: str | None = None

    for raw_target in targets:
        tgt = str(raw_target or "").strip()
        if not tgt:
            continue
        single = await dispatch_data_task_target_via_gateway(
            run_id=cleaned_run_id,
            target=tgt,
            gateway_url=gateway_url,
            service_token=service_token,
            timeout_s=timeout_s,
            http_client=http_client,
        )
        last_status_code = int(single.get("status_code") or 0)
        single_results = single.get("results")
        if isinstance(single_results, list):
            aggregated_results.extend(dict(r) for r in single_results if isinstance(r, Mapping))
        if single.get("uncertain") is True or single.get("status") == "unknown":
            any_uncertain = True
            all_ok = False
            if first_error_code is None:
                first_error_code = str(single.get("error_code") or "delivery_uncertain")
        elif single.get("ok") is not True:
            all_ok = False
            if first_error_code is None:
                first_error_code = str(single.get("error_code") or "target_failed")

    overall_status = "unknown" if any_uncertain else ("sent" if all_ok else "failed")
    out: dict[str, Any] = {
        "ok": all_ok and bool(aggregated_results),
        "status": overall_status,
        "uncertain": any_uncertain,
        "status_code": last_status_code,
        "results": aggregated_results,
    }
    if first_error_code:
        out["error_code"] = first_error_code
        out["error"] = first_error_code
    return out
