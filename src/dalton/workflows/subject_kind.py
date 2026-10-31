"""``subject_kind`` comes from the workspace's coverage identity, not the caller.

topic-research branches on ``args.subject_kind`` (sector vs company) and its
completion contract requires sector docs for sector workspaces. Codex r1 #1:
a caller-supplied value could be omitted, forgotten at check time, or
contradictory — each silently running the wrong branch. The identity is
already decided by P0's registry (``knowledge/profiles.resolve_workspace_profile``),
so this module is the one launch seam that fills the parameter from it:

- omitted / blank → filled with the identity (``sector`` / ``company``);
- normalizable to the identity ("SECTOR", " sector ") → the CANONICAL lowercase
  value is written back into the params (the workflow script branches with a
  strict comparison against the lowercase literal — an accepted-but-unnormalized
  value would run the wrong branch, codex r2 #2);
- contradicting the identity, or not one of the two values → ``SubjectKindRefused``
  — an explicit refusal, never a silent correction;
- identity cannot be resolved → refusal too (fail closed: better to retry the
  launch than to run the wrong branch end to end).

Only the workflows whose branch actually follows ``subject_kind`` are touched;
everything else passes through. Threads without a workspace identity are left
alone (the workflow then runs as before, and the completion check — which
resolves identity itself — refuses where it matters).
"""

from __future__ import annotations

import logging
from typing import Any

from src.dalton.knowledge.profiles import (
    PROFILE_COMPANY,
    PROFILE_SECTOR,
    resolve_workspace_profile,
)

logger = logging.getLogger(__name__)

# The workflows whose script branches on ``subject_kind``; each of them has a
# completion contract that branches the same way.
SUBJECT_KIND_WORKFLOWS = frozenset({"topic-research"})

_VALID_KINDS = ("sector", "company")


class SubjectKindRefused(ValueError):
    """The launch must not run: ``subject_kind`` conflicts with (or cannot be
    resolved for) the workspace's coverage identity. The launch path turns
    this into a refusal reply, leaving no run behind."""


def _claimed_kind(params: dict[str, Any]) -> str | None:
    """The caller's ``subject_kind`` normalized to lowercase (codex r2 #2) —
    ``workflow.js`` compares with JS strict ``=== 'sector'``, so an accepted
    but un-normalized ``"SECTOR"`` / ``" sector "`` would silently run the
    company branch. None when omitted/blank; non-string junk keeps a marker for
    the refusal message."""
    raw = params.get("subject_kind")
    if raw is None:
        return None
    if isinstance(raw, str):
        cleaned = raw.strip().lower()
        return cleaned or None
    return f"<{type(raw).__name__}>"


async def resolve_launch_subject_kind(
    workflow: str,
    params: Any,
    configurable: dict[str, Any] | None,
) -> Any:
    """Fill or validate ``params.subject_kind`` from the workspace identity.

    Returns the (possibly updated) params; raises ``SubjectKindRefused`` when
    the claim contradicts the identity, is not a valid value, or the identity
    itself cannot be resolved. Non-``topic-research`` workflows and params
    without a workspace identity pass through untouched.
    """
    if workflow not in SUBJECT_KIND_WORKFLOWS or not isinstance(params, dict):
        return params
    workspace_id = str((configurable or {}).get("workspace_id") or "").strip()
    if not workspace_id:
        return params

    try:
        profile = await resolve_workspace_profile(workspace_id)
    except Exception as error:
        raise SubjectKindRefused(
            f"工作区覆盖身份查询失败，无法确定 subject_kind（{error}）；"
            "请稍后重试 RunWorkflow，不要手动指定 subject_kind 代替身份。"
        ) from error
    actual = PROFILE_SECTOR if profile == PROFILE_SECTOR else PROFILE_COMPANY

    claimed = _claimed_kind(params)
    if claimed is None:
        logger.info(
            "subject_kind filled from coverage identity for %s in %s",
            workspace_id,
            workflow,
        )
        return {**params, "subject_kind": actual}
    if claimed not in _VALID_KINDS:
        raise SubjectKindRefused(
            f"subject_kind 只能是 sector 或 company，收到 {claimed!r}；"
            "本参数由工作区覆盖身份决定，可省略。"
        )
    if claimed != actual:
        raise SubjectKindRefused(
            f"subject_kind={claimed!r} 与本工作区的覆盖身份（{actual}）冲突；"
            "工作流分支与完成契约按身份执行，不接受调用方改写。"
            "如确需更换身份，先在覆盖单元注册表变更绑定。"
        )
    # Write the CANONICAL value back: the workflow script's branch is a strict
    # comparison against the lowercase literal, so "SECTOR"/" sector " must
    # reach args.json as "sector" (codex r2 #2).
    return {**params, "subject_kind": claimed}
