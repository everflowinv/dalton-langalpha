"""`automation_report`: submit or read one original-card automation report.

Server identity comes from the turn config. A saved material batch is
delivered by its saved execution id. Confirmation is the saved delivery
status, never transport acceptance.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from src.dalton.access_tiers import is_internal_or_admin
from src.dalton.database.automation_reports import (
    read_automation_report_status,
    submit_automation_report,
)
from src.dalton.notify.dispatch import dispatch_automation_report

logger = logging.getLogger(__name__)

ReportAction = Literal["submit", "status"]
_DISPATCH_PENDING = "dispatch_pending"
_UNCONFIRMED_STATUSES = frozenset({"pending", "unknown", "failed"})

_IDENTITY_FIELDS = (
    "user_id",
    "workspace_id",
    "thread_id",
    "run_id",
    "turn_origin_automation_id",
    "automation_execution_id",
)


def create_automation_report_tools() -> list[BaseTool]:
    """Build `[automation_report]` for internal and admin members."""

    @tool("automation_report")
    async def automation_report(
        action: ReportAction,
        report_run_id: str,
        material: str | None = None,
        cards: list[dict[str, Any]] | None = None,
        failure_text: str | None = None,
        *,
        config: RunnableConfig,
    ) -> str:
        """Submit Schema 2.0 cards or a failure text for this server-started automation, or read that report's delivery progress.
        Only that run can call it. An external member cannot. Reuse one report_run_id for the same report. no, no_change, and no-change store nothing.

        Args:
            action: "submit" or "status".
            report_run_id: Stable id for this report batch.
            material: "yes", "no", "no_change", or "no-change". Submit only.
            cards: Schema 2.0 cards for a report. Omit for a failure or a quiet report.
            failure_text: Failure text for a failure. Omit when the outcome is cards.

        Returns:
            JSON for the saved batch. delivery_confirmed is true only when every saved recipient is durably sent.

        Commit the collection seen-state only when delivery_confirmed is true.
        """
        chosen = str(action or "").strip()
        identity = _server_identity(config)
        if identity is None:
            return _refuse(
                chosen or "submit",
                "not_automation_run",
                "This turn is not a server-started automation run.",
            )
        if not await is_internal_or_admin(identity["user_id"]):
            return _refuse(
                chosen or "submit",
                "not_allowed",
                "This account cannot use automation reports.",
            )
        if chosen == "status":
            try:
                result = await read_automation_report_status(
                    user_id=identity["user_id"],
                    workspace_id=identity["workspace_id"],
                    thread_id=identity["thread_id"],
                    run_id=identity["run_id"],
                    automation_id=identity["turn_origin_automation_id"],
                    execution_id=identity["automation_execution_id"],
                    report_run_id=report_run_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "automation_report status failed: %s", type(exc).__name__
                )
                return _refuse(
                    "status",
                    "report_unavailable",
                    "The saved report could not be read.",
                )
            return _dump(result, action="status")
        if chosen != "submit":
            return _refuse(
                chosen or "submit",
                "invalid_action",
                "action must be submit or status.",
            )
        if not isinstance(material, str):
            return _refuse(
                "submit",
                "invalid_material",
                "material must be yes, no, no_change, or no-change.",
            )
        try:
            result = await submit_automation_report(
                user_id=identity["user_id"],
                workspace_id=identity["workspace_id"],
                thread_id=identity["thread_id"],
                run_id=identity["run_id"],
                automation_id=identity["turn_origin_automation_id"],
                execution_id=identity["automation_execution_id"],
                report_run_id=report_run_id,
                material=material,
                cards=cards,
                failure_text=failure_text,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("automation_report submit failed: %s", type(exc).__name__)
            return _refuse(
                "submit",
                "report_unavailable",
                "The report batch could not be saved.",
            )
        if not isinstance(result, Mapping):
            return _dump(result, action="submit")
        if result.get("ok") is not True or result.get("quiet") is True:
            return _dump(result, action="submit")
        saved_execution_id = str(result.get("execution_id") or "").strip()
        if not saved_execution_id:
            return _dump(result, action="submit")
        try:
            await dispatch_automation_report(saved_execution_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "automation_report dispatch failed: %s", type(exc).__name__
            )
            return _dump(_retain_saved_submit(result), action="submit")
        try:
            refreshed = await read_automation_report_status(
                user_id=identity["user_id"],
                workspace_id=identity["workspace_id"],
                thread_id=identity["thread_id"],
                run_id=identity["run_id"],
                automation_id=identity["turn_origin_automation_id"],
                execution_id=identity["automation_execution_id"],
                report_run_id=report_run_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "automation_report status refresh failed: %s", type(exc).__name__
            )
            return _dump(_retain_saved_submit(result), action="submit")
        if not isinstance(refreshed, Mapping):
            return _dump(_retain_saved_submit(result), action="submit")
        return _dump(_merge_submit_delivery(result, refreshed), action="submit")

    return [automation_report]


def _server_identity(config: RunnableConfig | None) -> dict[str, str] | None:
    """Return the six server fields, or None when any one is missing.

    ``run_id`` may also be carried as ``response_id``. Nothing is read from
    the database, and caller arguments are not consulted.
    """
    raw_config = config if isinstance(config, Mapping) else {}
    configurable = raw_config.get("configurable")
    cfg = configurable if isinstance(configurable, Mapping) else {}
    identity: dict[str, str] = {}
    for key in _IDENTITY_FIELDS:
        value = cfg.get(key)
        if key == "run_id" and not str(value or "").strip():
            value = cfg.get("response_id")
        text = str(value or "").strip()
        if not text:
            return None
        identity[key] = text
    return identity


def _refuse(action: str, code: str, error: str) -> str:
    return json.dumps(
        {
            "ok": False,
            "action": action,
            "code": code,
            "delivery_confirmed": False,
            "error": error,
        },
        ensure_ascii=False,
    )


def _dump(result: Any, *, action: str) -> str:
    if not isinstance(result, Mapping):
        return _refuse(action, "report_unavailable", "The report result could not be read.")
    try:
        return json.dumps(dict(result), ensure_ascii=False)
    except (TypeError, ValueError):
        logger.warning("automation_report result was not JSON")
        return _refuse(action, "report_unavailable", "The report result could not be read.")


def _retain_saved_submit(saved: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the committed batch when delivery cannot be confirmed yet.

    Storage already succeeded. A later sender or status-read failure must
    not flip that into a storage error or into transport acceptance.
    """
    retained = dict(saved)
    retained["ok"] = True
    retained["action"] = "submit"
    if (
        retained.get("delivery_confirmed") is True
        and str(retained.get("status") or "").strip() == "sent"
    ):
        return retained
    retained["delivery_confirmed"] = False
    status = str(retained.get("status") or "").strip()
    if status not in _UNCONFIRMED_STATUSES:
        retained["status"] = "pending"
    retained["dispatch_code"] = _DISPATCH_PENDING
    return retained


def _merge_submit_delivery(
    saved: Mapping[str, Any], refreshed: Mapping[str, Any]
) -> dict[str, Any]:
    """Copy durable delivery fields from the saved read onto the submit result.

    ``action``, ``created``, and the stored report identity stay on the
    submit result. A read that is not a found batch leaves the saved
    pending view, with a retry code and without transport acceptance.
    """
    if refreshed.get("ok") is not True or refreshed.get("found") is not True:
        return _retain_saved_submit(saved)
    merged = dict(saved)
    merged["action"] = "submit"
    if "created" in saved:
        merged["created"] = saved["created"]
    status = str(refreshed.get("status") or "").strip()
    confirmed = refreshed.get("delivery_confirmed") is True and status == "sent"
    if status in _UNCONFIRMED_STATUSES:
        confirmed = False
    merged["delivery_confirmed"] = confirmed
    if status:
        merged["status"] = status
    targets = refreshed.get("targets")
    if isinstance(targets, list):
        merged["targets"] = list(targets)
    merged.pop("dispatch_code", None)
    return merged


__all__ = ["create_automation_report_tools"]
