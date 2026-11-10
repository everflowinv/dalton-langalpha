"""`automation_report`: store or read one original-card report for this run.

The server identity comes from the turn config. The caller supplies the
report id and the body. This tool does not send.
"""

from __future__ import annotations

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

logger = logging.getLogger(__name__)

ReportAction = Literal["submit", "status"]

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
        """Submit or check one original-card report for the automation run that started this turn.
        Only that server-started run can use it. An external member cannot. This call stores or reads the batch; it does not send.

        Args:
            action: "submit" or "status".
            report_run_id: Stable id for this report batch.
            material: "yes", "no", "no_change", or "no-change". Submit only.
            cards: Schema 2.0 cards for a report. Omit for a failure or a quiet report.
            failure_text: Failure text for a failure. Omit when the outcome is cards.

        Returns:
            JSON for the saved batch. delivery_confirmed is true only when every saved recipient is already durably sent.
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
        except Exception as exc:
            logger.warning("automation_report submit failed: %s", type(exc).__name__)
            return _refuse(
                "submit",
                "report_unavailable",
                "The report batch could not be saved.",
            )
        return _dump(result, action="submit")

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


__all__ = ["create_automation_report_tools"]
