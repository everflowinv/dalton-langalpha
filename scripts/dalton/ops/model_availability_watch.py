#!/usr/bin/env python3
"""Daily subscription model availability watch (host data task T87).

Calls the backend internal endpoint ``POST /api/v1/dalton/internal/model-availability/check``,
deduplicates identical missing-model / Codex-version alerts for 24 hours in Dalton's state
directory, dispatches ``model_unavailable`` alerts via ``dalton-channel-gateway`` loopback
``POST /internal/ops-alerts``, and writes a JSON summary (``latest.json`` + stdout).

Pure Python 3 standard library only so host execution needs no virtualenv activation.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path
import stat
import time
from typing import Any, Callable
import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BACKEND_URL = "http://127.0.0.1:8000"
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8962"
OPS_ALERT_CODE = "model_unavailable"
RELEASED_ALERT_CODE = "model_released"
DEDUP_WINDOW_SECONDS = 24 * 3600
STATE_RETENTION_SECONDS = 7 * 24 * 3600


def resolve_state_dir(state_dir: str | Path | None = None) -> Path:
    """Resolve the Dalton state directory for model availability watch state."""
    if state_dir is not None and str(state_dir).strip():
        target = Path(state_dir).expanduser()
    elif os.environ.get("DALTON_MODEL_AVAILABILITY_STATE_DIR", "").strip():
        target = Path(os.environ["DALTON_MODEL_AVAILABILITY_STATE_DIR"].strip()).expanduser()
    elif os.environ.get("DALTON_HOST_VAR_DIR", "").strip():
        target = Path(os.environ["DALTON_HOST_VAR_DIR"].strip()).expanduser() / "model-availability"
    else:
        target = REPO_ROOT / "var" / "dalton" / "model-availability"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _atomic_write_json_0600(target: Path, payload: Any) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    os.close(fd)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, target)


def _normalize_missing_map(missing_by_provider: dict[str, list[str]]) -> dict[str, list[str]]:
    return {
        str(provider): sorted({str(k) for k in keys if str(k).strip()})
        for provider, keys in sorted(missing_by_provider.items())
        if keys
    }


def _normalize_error_map(
    error_by_provider: dict[str, str] | None = None,
    providers_report: dict[str, Any] | None = None,
) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for provider, err in sorted((error_by_provider or {}).items()):
        msg = str(err or "").strip()
        if str(provider).strip() and msg:
            normalized[str(provider)] = msg
    if isinstance(providers_report, dict):
        for provider, info in sorted(providers_report.items()):
            if provider in normalized or not isinstance(info, dict):
                continue
            status = str(info.get("status") or "").strip().lower()
            if status in {"error", "fallback"}:
                detail = str(
                    info.get("error") or info.get("reason") or info.get("upstream_status") or status
                ).strip()
                normalized[str(provider)] = detail or status
    return normalized


def build_missing_signature(
    missing_by_provider: dict[str, list[str]],
    error_by_provider: dict[str, str] | None = None,
) -> str | None:
    """Return a SHA-256 signature for missing configured models or supported-provider errors."""
    normalized_missing = _normalize_missing_map(missing_by_provider)
    normalized_errors = _normalize_error_map(error_by_provider)
    if not normalized_missing and not normalized_errors:
        return None
    canonical = json.dumps(
        {
            "kind": "missing_or_error",
            "missing_by_provider": normalized_missing,
            "error_by_provider": normalized_errors,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_upgrade_signature(codex_version_probe: dict[str, Any] | None = None) -> str | None:
    """Return a SHA-256 signature for Codex client_version upgrade hints, or ``None``."""
    probe = codex_version_probe if isinstance(codex_version_probe, dict) else {}
    upgrade_hint = str(probe.get("upgrade_hint") or "").strip()
    if not upgrade_hint:
        return None
    unlocked = sorted({str(k) for k in (probe.get("unlocked_models") or []) if str(k).strip()})
    canonical = json.dumps(
        {
            "kind": "codex_upgrade_hint",
            "upgrade_hint": upgrade_hint,
            "unlocked_models": unlocked,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_alert_signature(
    missing_by_provider: dict[str, list[str]],
    codex_version_probe: dict[str, Any] | None = None,
    error_by_provider: dict[str, str] | None = None,
) -> str | None:
    """Return a combined SHA-256 signature for observability (dedup uses split signatures)."""
    normalized_missing = _normalize_missing_map(missing_by_provider)
    normalized_errors = _normalize_error_map(error_by_provider)
    probe = codex_version_probe if isinstance(codex_version_probe, dict) else {}
    upgrade_hint = str(probe.get("upgrade_hint") or "").strip()
    unlocked = sorted({str(k) for k in (probe.get("unlocked_models") or []) if str(k).strip()})
    if not normalized_missing and not normalized_errors and not upgrade_hint:
        return None
    canonical = json.dumps(
        {
            "missing_by_provider": normalized_missing,
            "error_by_provider": normalized_errors,
            "upgrade_hint": upgrade_hint or None,
            "unlocked_models": unlocked if upgrade_hint else [],
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_missing_detail(
    missing_by_provider: dict[str, list[str]],
    error_by_provider: dict[str, str] | None = None,
) -> str:
    """Format provider/missing-model and provider-error alert details."""
    parts: list[str] = []
    for provider, keys in _normalize_missing_map(missing_by_provider).items():
        if keys:
            parts.append(f"{provider}: {', '.join(keys)}")
    for provider, err_msg in _normalize_error_map(error_by_provider).items():
        parts.append(f"{provider}: 检查失败 ({err_msg})")
    return "；".join(parts)


def build_upgrade_detail(codex_version_probe: dict[str, Any] | None = None) -> str:
    """Format the Codex client_version upgrade hint detail string."""
    probe = codex_version_probe if isinstance(codex_version_probe, dict) else {}
    upgrade_hint = str(probe.get("upgrade_hint") or "").strip()
    if not upgrade_hint:
        return ""
    unlocked = sorted({str(k) for k in (probe.get("unlocked_models") or []) if str(k).strip()})
    if unlocked:
        return f"{upgrade_hint}（解锁: {', '.join(unlocked)}）"
    return upgrade_hint


def build_alert_detail(
    missing_by_provider: dict[str, list[str]],
    codex_version_probe: dict[str, Any] | None = None,
    error_by_provider: dict[str, str] | None = None,
) -> str:
    """Format a concise one-line detail string listing providers, errors, missing keys, and Codex hint."""
    parts = [
        p
        for p in (
            build_missing_detail(missing_by_provider, error_by_provider),
            build_upgrade_detail(codex_version_probe),
        )
        if p
    ]
    return "；".join(parts)


def load_alert_state(state_file: Path) -> dict[str, Any]:
    """Load persisted alert deduplication state from ``state_file``."""
    if not state_file.is_file():
        return {"alerts_by_signature": {}, "released_alerts": {}}
    try:
        raw = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return {"alerts_by_signature": {}, "released_alerts": {}}
    if not isinstance(raw, dict):
        return {"alerts_by_signature": {}, "released_alerts": {}}
    by_sig = raw.get("alerts_by_signature")
    if not isinstance(by_sig, dict):
        by_sig = {}
    released = raw.get("released_alerts")
    if not isinstance(released, dict):
        released = {}
    return {"alerts_by_signature": dict(by_sig), "released_alerts": dict(released)}


def should_send_alert(
    state: dict[str, Any],
    signature: str | None,
    *,
    now_epoch: float,
    dedup_window_s: float = DEDUP_WINDOW_SECONDS,
) -> bool:
    """Return ``True`` when ``signature`` has not been alerted within ``dedup_window_s``."""
    if not signature:
        return False
    entry = (state.get("alerts_by_signature") or {}).get(signature)
    if not isinstance(entry, dict):
        return True
    last_epoch = entry.get("epoch")
    if not isinstance(last_epoch, (int, float)):
        return True
    return (now_epoch - float(last_epoch)) >= dedup_window_s


def record_sent_alert(
    state: dict[str, Any],
    signature: str,
    *,
    now_epoch: float,
    detail: str,
) -> dict[str, Any]:
    """Return updated state recording ``signature`` at ``now_epoch`` and pruning stale entries."""
    cutoff = now_epoch - STATE_RETENTION_SECONDS
    existing = state.get("alerts_by_signature") or {}
    pruned: dict[str, Any] = {}
    for sig, entry in existing.items():
        if isinstance(entry, dict) and isinstance(entry.get("epoch"), (int, float)):
            if float(entry["epoch"]) >= cutoff:
                pruned[str(sig)] = entry
    pruned[signature] = {
        "epoch": float(now_epoch),
        "alerted_at": datetime.fromtimestamp(now_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "detail": detail,
    }
    return {
        "alerts_by_signature": pruned,
        "released_alerts": dict(state.get("released_alerts") or {}),
    }


def record_released_alert(
    state: dict[str, Any],
    *,
    provider: str,
    models: list[str],
    now_epoch: float,
) -> dict[str, Any]:
    """Record one-time ``model_released`` alerts per ``provider:model`` in ``state``."""
    released = dict(state.get("released_alerts") or {})
    alerted_at = datetime.fromtimestamp(now_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for model in models:
        released[f"{provider}:{model}"] = {
            "epoch": float(now_epoch),
            "alerted_at": alerted_at,
            "provider": provider,
            "model": model,
        }
    return {
        "alerts_by_signature": dict(state.get("alerts_by_signature") or {}),
        "released_alerts": released,
    }


def _released_alert_key(provider: str, model: str) -> str:
    return f"{provider}:{model}"


def build_released_detail(provider: str, models: list[str]) -> str:
    models_str = ", ".join(models)
    return f"订阅已开放 {provider} 的 {models_str}，核对档位后将其设为可见并去掉 awaiting_release 标记"


def fetch_backend_check(
    *,
    backend_url: str,
    service_token: str,
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    """Call ``POST {backend_url}/api/v1/dalton/internal/model-availability/check``."""
    if not service_token:
        raise RuntimeError("INTERNAL_SERVICE_TOKEN is required to call backend model availability check")
    url = f"{backend_url.rstrip('/')}/api/v1/dalton/internal/model-availability/check"
    req = urllib.request.Request(
        url,
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Service-Token": service_token,
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"backend check returned HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"backend check failed: {exc.reason}") from exc
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError("backend check returned non-object JSON")
    return payload


def send_ops_alert(
    detail: str,
    *,
    gateway_url: str,
    service_token: str,
    code: str = OPS_ALERT_CODE,
    extra_fields: dict[str, str] | None = None,
    timeout_s: float = 15.0,
) -> tuple[bool, int | None, str | None]:
    """Send a loopback ops alert via ``POST {gateway_url}/internal/ops-alerts``."""
    if not service_token:
        return False, None, "INTERNAL_SERVICE_TOKEN is not configured"
    url = f"{gateway_url.rstrip('/')}/internal/ops-alerts"
    payload: dict[str, Any] = {"code": code, "detail": detail}
    if extra_fields:
        payload.update(extra_fields)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Service-Token": service_token,
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout_s) as resp:
            return 200 <= resp.status < 300, int(resp.status), None
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:200]
        return False, int(exc.code), f"HTTP {exc.code}: {err_body}"
    except urllib.error.URLError as exc:
        return False, None, f"URLError: {exc.reason}"


def _dispatch_alert(
    alert_sender: Callable[..., tuple[bool, int | None, str | None]] | None,
    detail: str,
    *,
    gateway_url: str,
    service_token: str,
    code: str,
    extra_fields: dict[str, str] | None = None,
) -> tuple[bool, int | None, str | None]:
    if alert_sender is None:
        return send_ops_alert(
            detail,
            gateway_url=gateway_url,
            service_token=service_token,
            code=code,
            extra_fields=extra_fields,
        )
    try:
        sig = inspect.signature(alert_sender)
        params = sig.parameters
        accepts_kwargs = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        call_kwargs: dict[str, Any] = {}
        if accepts_kwargs or "code" in params:
            call_kwargs["code"] = code
        if extra_fields and (accepts_kwargs or "extra_fields" in params):
            call_kwargs["extra_fields"] = extra_fields
        return alert_sender(detail, **call_kwargs)
    except (TypeError, ValueError):
        return alert_sender(detail)


def run_watch(
    *,
    state_dir: str | Path | None = None,
    backend_url: str | None = None,
    gateway_url: str | None = None,
    service_token: str | None = None,
    check_report: dict[str, Any] | None = None,
    check_fn: Callable[[], dict[str, Any]] | None = None,
    alert_sender: Callable[..., tuple[bool, int | None, str | None]] | None = None,
    now_epoch: float | None = None,
    dedup_window_s: float = DEDUP_WINDOW_SECONDS,
) -> tuple[dict[str, Any], int]:
    """Execute one model availability watch cycle, handle 24h deduped ops alerts, and save summary."""
    resolved_state_dir = resolve_state_dir(state_dir)
    alert_state_path = resolved_state_dir / "alert_state.json"
    summary_path = resolved_state_dir / "latest.json"

    eff_token = (
        service_token
        if service_token is not None
        else os.environ.get("INTERNAL_SERVICE_TOKEN", "")
    ).strip()
    eff_backend_url = (
        backend_url
        or os.environ.get("DALTON_BACKEND_URL", "").strip()
        or DEFAULT_BACKEND_URL
    )
    eff_gateway_url = (
        gateway_url
        or os.environ.get("DALTON_CHANNEL_GATEWAY_URL", "").strip()
        or DEFAULT_GATEWAY_URL
    )
    eff_now = float(now_epoch) if now_epoch is not None else time.time()

    try:
        if check_report is not None:
            report = dict(check_report)
        elif check_fn is not None:
            report = dict(check_fn())
        else:
            report = fetch_backend_check(
                backend_url=eff_backend_url,
                service_token=eff_token,
            )
    except Exception as exc:
        summary = {
            "ok": False,
            "checked_at": datetime.fromtimestamp(eff_now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "error": str(exc),
            "alert": {"needed": False, "sent": False, "suppressed_24h": False},
        }
        _atomic_write_json_0600(summary_path, summary)
        return summary, 1

    missing_by_provider = report.get("missing_by_provider") or {}
    released_by_provider = _normalize_missing_map(report.get("released_by_provider") or {})
    providers_report = report.get("providers") or {}
    error_by_provider = _normalize_error_map(report.get("error_by_provider"), providers_report)
    codex_probe = report.get("codex_version_probe") or {}

    missing_sig = build_missing_signature(missing_by_provider, error_by_provider)
    upgrade_sig = build_upgrade_signature(codex_probe)
    missing_detail = build_missing_detail(missing_by_provider, error_by_provider)
    upgrade_detail = build_upgrade_detail(codex_probe)
    full_detail = build_alert_detail(missing_by_provider, codex_probe, error_by_provider)
    combined_sig = build_alert_signature(missing_by_provider, codex_probe, error_by_provider)

    state = load_alert_state(alert_state_path)
    missing_due = bool(missing_sig) and should_send_alert(
        state, missing_sig, now_epoch=eff_now, dedup_window_s=dedup_window_s
    )
    upgrade_due = bool(upgrade_sig) and should_send_alert(
        state, upgrade_sig, now_epoch=eff_now, dedup_window_s=dedup_window_s
    )
    due_detail_parts = [
        p
        for due, p in ((missing_due, missing_detail), (upgrade_due, upgrade_detail))
        if due and p
    ]
    outbound_detail = "；".join(due_detail_parts)

    any_needed = bool(missing_sig or upgrade_sig)
    missing_suppressed = bool(missing_sig) and not missing_due
    upgrade_suppressed = bool(upgrade_sig) and not upgrade_due

    alert_info: dict[str, Any] = {
        "needed": any_needed,
        "sent": False,
        "suppressed_24h": bool(any_needed and not due_detail_parts),
        "code": OPS_ALERT_CODE,
        "signature": combined_sig,
        "missing_signature": missing_sig,
        "upgrade_signature": upgrade_sig,
        "missing_sent": False,
        "missing_suppressed_24h": missing_suppressed,
        "upgrade_sent": False,
        "upgrade_suppressed_24h": upgrade_suppressed,
        "detail": outbound_detail or full_detail or None,
        "error": None,
    }

    exit_code = 0
    if due_detail_parts:
        sent_ok, status_code, err = _dispatch_alert(
            alert_sender,
            outbound_detail,
            gateway_url=eff_gateway_url,
            service_token=eff_token,
            code=OPS_ALERT_CODE,
        )
        alert_info["status_code"] = status_code
        if sent_ok:
            alert_info["sent"] = True
            alert_info["missing_sent"] = bool(missing_due)
            alert_info["upgrade_sent"] = bool(upgrade_due)
            if missing_due and missing_sig:
                state = record_sent_alert(
                    state,
                    missing_sig,
                    now_epoch=eff_now,
                    detail=missing_detail,
                )
            if upgrade_due and upgrade_sig:
                state = record_sent_alert(
                    state,
                    upgrade_sig,
                    now_epoch=eff_now,
                    detail=upgrade_detail,
                )
            _atomic_write_json_0600(alert_state_path, state)
        else:
            alert_info["error"] = err or "ops_alert_send_failed"
            exit_code = 1

    released_already = state.get("released_alerts") or {}
    released_due_by_provider: dict[str, list[str]] = {}
    for provider, models in sorted(released_by_provider.items()):
        pending = [m for m in models if _released_alert_key(provider, m) not in released_already]
        if pending:
            released_due_by_provider[provider] = pending

    released_sent_by_provider: dict[str, list[str]] = {}
    released_errors: list[str] = []
    for provider, pending_models in sorted(released_due_by_provider.items()):
        models_str = ", ".join(pending_models)
        released_detail = build_released_detail(provider, pending_models)
        sent_ok, _rel_status, rel_err = _dispatch_alert(
            alert_sender,
            released_detail,
            gateway_url=eff_gateway_url,
            service_token=eff_token,
            code=RELEASED_ALERT_CODE,
            extra_fields={"provider": provider, "models": models_str},
        )
        if sent_ok:
            released_sent_by_provider[provider] = pending_models
            state = record_released_alert(
                state,
                provider=provider,
                models=pending_models,
                now_epoch=eff_now,
            )
            _atomic_write_json_0600(alert_state_path, state)
        else:
            released_errors.append(f"{provider}: {rel_err or 'ops_alert_send_failed'}")
            exit_code = 1

    released_alert_info: dict[str, Any] = {
        "needed": bool(released_by_provider),
        "sent": bool(released_sent_by_provider),
        "suppressed_already_alerted": bool(released_by_provider) and not bool(released_due_by_provider),
        "code": RELEASED_ALERT_CODE,
        "by_provider": released_sent_by_provider or released_due_by_provider or released_by_provider,
        "error": "；".join(released_errors) if released_errors else None,
    }

    has_provider_errors = bool(error_by_provider)
    report_ok = bool(report.get("ok", True)) and not has_provider_errors
    # A provider we could not check is a failed run even when its alert went out or was
    # deduplicated: the data-task evaluator reads the exit code, not the JSON.
    if not report_ok and exit_code == 0:
        exit_code = 1
    summary = {
        "ok": exit_code == 0 and report_ok,
        "checked_at": report.get("checked_at")
        or datetime.fromtimestamp(eff_now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "user_id": report.get("user_id"),
        "providers": providers_report,
        "missing_by_provider": missing_by_provider,
        "released_by_provider": released_by_provider,
        "error_by_provider": error_by_provider,
        "codex_version_probe": codex_probe,
        "alert": alert_info,
        "released_alert": released_alert_info,
        "state_dir": str(resolved_state_dir),
    }
    _atomic_write_json_0600(summary_path, summary)
    return summary, exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily subscription model availability watch.")
    parser.add_argument("--state-dir", default=None, help="Override Dalton state directory.")
    parser.add_argument("--backend-url", default=None, help="Override DALTON_BACKEND_URL.")
    parser.add_argument("--gateway-url", default=None, help="Override DALTON_CHANNEL_GATEWAY_URL.")
    parser.add_argument("--input-json", default=None, help="Read check report from a local JSON file.")
    parser.add_argument("--json", action="store_true", help="Print JSON summary to stdout.")
    args = parser.parse_args(argv)

    loaded_report = None
    if args.input_json:
        loaded_report = json.loads(Path(args.input_json).read_text(encoding="utf-8"))

    summary, exit_code = run_watch(
        state_dir=args.state_dir,
        backend_url=args.backend_url,
        gateway_url=args.gateway_url,
        check_report=loaded_report,
    )
    print(json.dumps(summary, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
