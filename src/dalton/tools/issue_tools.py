"""`report_issue`: the controlled bug-report tool (E3).

The old self-improve path submitted straight to GitHub with sandbox
credentials; upstream removed both (upstream #595). This is the replacement
shape: the agent files an internal report *after the user agreed to submit*,
the row lands in `dalton_report_issues` for the team to triage, and nothing
dials GitHub — no `gh`, no token, no external call of any kind.
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from src.dalton.database.issues import create_report_issue
from src.dalton.tools.knowledge_tools import _extract_run_identity

logger = logging.getLogger(__name__)

Severity = Literal["low", "normal", "high"]
_MAX_TITLE_CHARS = 200
_MAX_BODY_CHARS = 8000


def create_issue_tools() -> list[BaseTool]:
    """Build `[report_issue]`, mounted for both roles."""

    @tool("report_issue")
    async def _report_issue(
        title: str,
        body: str,
        severity: Severity = "normal",
        *,
        config: RunnableConfig,
    ) -> str:
        """File an internal bug/feedback report — only after the user has explicitly agreed to submit it.
        Writes to the team's internal issue inbox and returns a tracking ref (like DALTON-20261027-a1b2c3) to quote to the user; nothing is posted to GitHub and no external service is called.
        Use it for product bugs and broken behavior the user wants on record; for data-provider outages prefer naming the source in your answer instead.

        Args:
            title: One-line summary (<=200 chars).
            body: What happened, what was expected, and how to reproduce (<=8000 chars).
            severity: 'low' | 'normal' | 'high' — user-facing impact.

        Returns:
            JSON with `ok`, the tracking `ref` and `issue_id`; relay the ref to the user as the follow-up handle.
        """
        user_id, workspace_id, thread_id, _ = _extract_run_identity(config)
        if not user_id:
            return json.dumps(
                {"ok": False, "error": "report_issue 需要登录用户的会话（RunnableConfig 缺少 user_id）。"},
                ensure_ascii=False,
            )
        title_clean = str(title or "").strip()[:_MAX_TITLE_CHARS]
        body_clean = str(body or "").strip()[:_MAX_BODY_CHARS]
        if not title_clean or not body_clean:
            return json.dumps(
                {"ok": False, "error": "title 和 body 都不能为空。"},
                ensure_ascii=False,
            )
        severity_clean = severity if severity in ("low", "normal", "high") else "normal"
        try:
            row = await create_report_issue(
                user_id=user_id,
                title=title_clean,
                body=body_clean,
                severity=severity_clean,
                workspace_id=workspace_id,
                thread_id=thread_id,
            )
        except Exception as exc:
            logger.warning("report_issue insert failed: %s", type(exc).__name__)
            return json.dumps(
                {"ok": False, "error": f"问题记录写入失败（{type(exc).__name__}），请稍后再试。"},
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "ok": True,
                "ref": row.get("ref"),
                "issue_id": row.get("issue_id"),
                "severity": severity_clean,
                "message": f"已提交，跟踪编号 {row.get('ref')}；请把编号告诉用户，团队会跟进。",
            },
            ensure_ascii=False,
        )

    return [_report_issue]


_DEFAULT_TOOLS = create_issue_tools()
report_issue = _DEFAULT_TOOLS[0]
