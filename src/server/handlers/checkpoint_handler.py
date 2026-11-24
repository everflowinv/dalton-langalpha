"""
Checkpoint Handler — Business logic for checkpoint history and thread turn operations.

Provides endpoints for:
- Listing turn-boundary checkpoints (for edit/regenerate/retry)
- Naming the turn an edit/regenerate replaces, from the checkpoint it forks at
- Retrying failed/interrupted threads from the appropriate checkpoint
"""

import inspect  # DALTON-HOOK(cos-settings-tools)
import logging
import os  # DALTON-HOOK(cos-settings-tools)
from typing import Any, NamedTuple  # DALTON-HOOK(cos-settings-tools)

from fastapi import HTTPException

from src.server.database.conversation import threads_read
from src.server.services.history.branch import branch_turns, stale_fork_detail
from src.server.utils.checkpoint_helpers import (
    FORK_ROOT_CHECKPOINT_ID,
    build_checkpoint_config,
    get_checkpointer,
    interrupt_records,  # DALTON-HOOK(cos-settings-tools)
    turn_base_checkpoint_id,
)
from src.server.utils.pg_sanitize import normalize_uuid
from src.server.models.chat import ChatRequest
from src.server.models.workflow import (
    ThreadTurnsResponse,
)

logger = logging.getLogger(__name__)


async def get_thread_turns(
    thread_id: str, branch_tip_checkpoint_id: str | None = None
) -> ThreadTurnsResponse:
    """Turn boundaries on the thread's current branch, for edit/regenerate.

    Edit/regenerate fork the checkpoint graph, so only ancestors of the branch
    tip count (tip: ``branch_tip_checkpoint_id`` when on the graph, else the
    newest checkpoint). Each turn is named by its persisted ``turn_index``
    (``pair_turns``), the number its rows and replayed bubbles carry; a turn
    with no persisted turn to name is left out. Turns past the last boundary
    share one last entry at the tip, or at the empty fork root on a thread
    with no checkpoint, which is also where the first turn's edit forks.
    """
    listing = await branch_turns(thread_id, branch_tip_checkpoint_id)
    return ThreadTurnsResponse(
        thread_id=thread_id,
        turns=listing.turns,
        retry_checkpoint_id=listing.tip_id,
    )


class ForkPoint(NamedTuple):
    """The turn a fork replaces, the branch tip it was read off, and the
    newest run's ``run_seq`` read with that tip. ``unstarted`` marks a turn
    that never reached the graph, whose input no checkpoint holds."""

    turn_index: int
    branch_tip: str | None
    run_horizon: int | None = None
    unstarted: bool = False


async def resolve_fork_turn(
    thread_id: str,
    checkpoint_id: str,
    *,
    regenerate: bool,
    requested: int | None = None,
) -> ForkPoint:
    """The turn an edit or regenerate at ``checkpoint_id`` replaces.

    The fork deletes rows from this turn onward, so the turn is read off the
    current branch by the checkpoint it forks at (an edit at the parent of the
    turn's input checkpoint, a regenerate at its ``regenerate_checkpoint_id``),
    not taken from the client's count. An edit keeps the client's
    ``requested`` turn only when nothing but turns that never checkpointed lie
    between it and the forked one, so rewriting a failed message works and no
    kept turn is deleted. A checkpoint that forks no turn of the current
    branch is refused: the client read a branch that has since been replaced.

    The answer holds only while the branch does, and this runs outside the
    transaction that admits the fork, so the tip it read travels with it, and
    the run horizon read with it: START refuses the fork once a run started
    after this read or the fork's checkpoint left the thread's branch.
    """
    branch = await threads_read.get_thread_branch_read(thread_id)
    branch_tip, horizon = branch.checkpoint_id, branch.run_horizon
    listing = await branch_turns(thread_id, branch_tip)
    turns = listing.turns
    if (
        regenerate
        and listing.unstarted is not None
        and requested == listing.unstarted.turn_index
    ):
        # A last turn whose graph wrote no more than its input checkpoint ends
        # on it, so the unstarted turn after it forks from the same tip; the
        # client's turn decides between the two.
        turns = [listing.unstarted, *turns]
    kept: int | None = None
    for turn in turns:
        forks_at = (
            turn.regenerate_checkpoint_id if regenerate else turn.edit_checkpoint_id
        )
        if forks_at == checkpoint_id:
            unstarted = turn is listing.unstarted
            if (
                not regenerate
                and requested is not None
                and (kept is None or requested > kept)
                and requested <= turn.turn_index
            ):
                return ForkPoint(requested, branch_tip, horizon, unstarted)
            return ForkPoint(turn.turn_index, branch_tip, horizon, unstarted)
        kept = turn.turn_index if kept is None else max(kept, turn.turn_index)
    raise HTTPException(status_code=409, detail=stale_fork_detail())


async def first_turn_dropped(thread_id: str, checkpoint_id: str) -> int | None:
    """The first turn a run from ``checkpoint_id`` leaves off the thread's
    branch, or None when the run only adds to it (the branch's tip, or a
    newer checkpoint built on it, as a failed run leaves).

    On the branch, the turn whose span holds the checkpoint is replaced and
    every later one with it. Off the branch, or with no branch read, the
    turns it drops are not known, so every turn counts (0).
    """
    from src.server.utils.checkpoint_helpers import checkpoint_on_branch

    tip = (await threads_read.get_thread_branch_read(thread_id)).checkpoint_id
    if tip is None:
        return 0
    if checkpoint_id == tip or await checkpoint_on_branch(thread_id, tip, checkpoint_id):
        return None
    if not await checkpoint_on_branch(thread_id, checkpoint_id, tip):
        return 0
    dropped = 0
    # Checkpoint ids are uuid6, ordered by time, the fork root first.
    for turn in (await branch_turns(thread_id, tip)).turns:
        start = turn.edit_checkpoint_id
        if start is not None and start <= checkpoint_id:
            dropped = max(dropped, turn.turn_index)
    return dropped


async def first_turn_replaced(thread_id: str, request: ChatRequest) -> int | None:
    """The first turn ``request`` leaves off the thread's branch, or None
    when it only adds to it. An answer to a paused turn names the checkpoint
    it resumes, which replaces nothing."""
    if request.fork_from_turn is not None:
        return int(request.fork_from_turn)
    if request.checkpoint_id and not request.hitl_response:
        return await first_turn_dropped(thread_id, str(request.checkpoint_id))
    return None


def _is_run(run: dict | None, run_id: str) -> bool:
    expected = normalize_uuid(run_id)
    return (
        run is not None
        and expected is not None
        and normalize_uuid(run["conversation_response_id"]) == expected
    )


async def settle_fork_request(thread_id: str, request: ChatRequest) -> ChatRequest:
    """The fork ``request`` with its turn read off the current branch and the
    branch read START holds it to.

    A regenerate of a turn that never reached the graph has no input on the
    branch to replay, so it runs as the edit that sends the turn's stored
    message again.
    """
    fork = await resolve_fork_turn(
        thread_id,
        request.checkpoint_id,
        regenerate=not request.messages,
        requested=request.fork_from_turn,
    )
    if fork.turn_index != request.fork_from_turn:
        logger.warning(
            f"[CHAT] fork turn {request.fork_from_turn} from the client "
            f"is turn {fork.turn_index} on the branch thread_id={thread_id}"
        )
    unstarted_regenerate = fork.unstarted and not request.messages
    latest = None
    if request.expected_latest_run_id is not None or unstarted_regenerate:
        from src.server.database.runs.lifecycle import get_latest_attempt

        latest = await get_latest_attempt(thread_id)
    # The branch was read as it is now, a turn this client never saw on it
    # included, so only the client's own last run tells a fork made from a
    # stale view apart.
    if request.expected_latest_run_id is not None and not _is_run(
        latest, request.expected_latest_run_id
    ):
        raise HTTPException(status_code=409, detail=stale_fork_detail())
    if unstarted_regenerate:
        from src.server.handlers.chat.admission_gate import admission_conflict_detail
        from src.server.services.runs.pending_input import (
            unstarted_regenerate_request,
        )

        # An open run is the unstarted turn still starting: refused as any
        # fork into a running turn is.
        if latest is not None and latest["status"] == "in_progress":
            raise HTTPException(
                status_code=409, detail=admission_conflict_detail("running")
            )
        request = await unstarted_regenerate_request(
            thread_id, request, fork.turn_index, latest
        )
    return request.model_copy(
        update={
            "fork_from_turn": fork.turn_index,
            "fork_branch_tip": fork.branch_tip,
            "fork_run_horizon": fork.run_horizon,
        }
    )


async def get_retry_checkpoint(thread_id: str, checkpoint_id: str | None = None) -> str:
    """
    Determine the appropriate checkpoint ID for retrying a failed/interrupted thread.

    If checkpoint_id is provided, validates it exists and returns it.
    Otherwise, auto-detects the latest checkpoint of the thread's branch
    (``turn_base_checkpoint_id``). The empty fork root is never one: it holds
    no input to replay, and a first message's edit that failed before its
    graph took it reruns from its ``pending_input`` stamp, so one left without
    a stamp has nothing a retry can run.

    Args:
        thread_id: Thread identifier
        checkpoint_id: Optional explicit checkpoint ID

    Returns:
        The checkpoint ID to retry from

    Raises:
        HTTPException: If no checkpoint is found, or 409 ``not_retryable`` at
            the empty fork root
    """
    checkpointer = get_checkpointer()

    if checkpoint_id:
        # Validate the provided checkpoint exists
        config = build_checkpoint_config(thread_id, checkpoint_id)
        cp_tuple = await checkpointer.aget_tuple(config)
        if not cp_tuple:
            raise HTTPException(
                status_code=404,
                detail=f"Checkpoint {checkpoint_id} not found for thread {thread_id}",
            )
        replay = checkpoint_id
    else:
        replay = await turn_base_checkpoint_id(checkpointer, thread_id)
        if replay is None:
            config = build_checkpoint_config(thread_id)
            cp_tuple = await checkpointer.aget_tuple(config)
            if not cp_tuple:
                raise HTTPException(
                    status_code=404,
                    detail=f"No checkpoints found for thread {thread_id}",
                )
            replay = cp_tuple.config["configurable"]["checkpoint_id"]

    if replay == FORK_ROOT_CHECKPOINT_ID:
        from src.server.services.runs.pending_input import _message_gone

        raise _message_gone()
    return replay


# --- DALTON-HOOK(cos-settings-tools): begin ---
WEB_CONFIRM_ACTION_TYPES: frozenset[str] = frozenset(
    {"create_workspace", "archive_workspace"}
)


def _interrupt_action_request(record: dict[str, Any]) -> dict[str, Any] | None:
    value = record.get("value")
    if not isinstance(value, dict):
        return None
    action_requests = value.get("action_requests")
    if not isinstance(action_requests, list) or not action_requests:
        return None
    first = action_requests[0]
    return first if isinstance(first, dict) else None


def _is_web_confirm_interrupt(record: dict[str, Any]) -> bool:
    action_req = _interrupt_action_request(record)
    return bool(action_req and action_req.get("type") in WEB_CONFIRM_ACTION_TYPES)


def _verify_oss_loopback_web_caller(raw_request: Any) -> bool:
    """Verify caller headers in ``DALTON_AUTH_MODE == 'oss'``.

    - When ``LANGALPHA_OWNER_SECRET`` is configured (desktop app), requires a
      valid ``x-langalpha-owner`` HMAC header via ``owner_requests.is_owner``.
    - When ``LANGALPHA_OWNER_SECRET`` is unset (passwordless loopback trial),
      no user authentication is active and local processes share the owner
      identity. Reject cross-site browser requests (``Sec-Fetch-Site: cross-site``)
      alongside the ``Authorization``, ``X-Service-Token``, and channel checks
      in ``_verify_web_confirm_resume``; avoid pseudo-security checks on
      loopback ``Origin`` or ``Sec-Fetch-Site: same-origin`` that a local
      process can freely set. Use ``DALTON_AUTH_MODE=local`` or ``supabase``
      when personal-click assurance is required.
    """
    from src.server.auth import owner_requests

    if os.environ.get(owner_requests.OWNER_SECRET_ENV, ""):
        return owner_requests.is_owner(raw_request.scope)

    headers = getattr(raw_request, "headers", None)
    if headers is None:
        return False
    sec_fetch_site = (headers.get("sec-fetch-site") or "").strip().lower()
    if sec_fetch_site == "cross-site":
        return False
    return True


def _is_reject_answer(answer: Any) -> bool:
    """True when the caller explicitly declined the pending confirmation."""
    decisions = (
        answer.get("decisions")
        if isinstance(answer, dict)
        else getattr(answer, "decisions", None)
    )
    if not decisions:
        return False
    first = decisions[0]
    decision_type = (
        first.get("type") if isinstance(first, dict) else getattr(first, "type", None)
    )
    return decision_type == "reject"


def _snapshot_matches_action(
    snapshot: dict[str, Any], action_req: dict[str, Any]
) -> bool:
    action_type = action_req.get("type")
    if snapshot.get("action") != action_type:
        return False
    if action_type == "create_workspace":
        return (
            snapshot.get("workspace_name") == action_req.get("workspace_name")
            and (snapshot.get("workspace_description") or "")
            == (action_req.get("workspace_description") or "")
            and snapshot.get("computer_id") == action_req.get("computer_id")
            and snapshot.get("computer_name") == action_req.get("computer_name")
            and bool(snapshot.get("bootstrap_cloud_primary"))
            == bool(action_req.get("bootstrap_cloud_primary"))
            and bool(snapshot.get("computer_is_own_machine"))
            == bool(action_req.get("computer_is_own_machine"))
            and snapshot.get("duplicate_workspace_id")
            == action_req.get("duplicate_workspace_id")
            and snapshot.get("duplicate_workspace_name")
            == action_req.get("duplicate_workspace_name")
            and bool(snapshot.get("duplicate_check_unavailable"))
            == bool(action_req.get("duplicate_check_unavailable"))
        )
    if action_type == "archive_workspace":
        return (
            snapshot.get("workspace_id") == action_req.get("workspace_id")
            and snapshot.get("computer_id") == action_req.get("computer_id")
        )
    return False


async def _verify_web_confirm_resume(
    *,
    thread_id: str,
    request: ChatRequest,
    newest: Any,
    pending_by_id: dict[str, dict[str, Any]],
    guarded_by_id: dict[str, dict[str, Any]],
    principal: Any,
    raw_request: Any,
    thread_meta: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Enforce first-party session confirmation authenticity for ``create_workspace``
    and ``archive_workspace`` HITL resumes.

    Returns an updated ``hitl_response`` map when a guarded pending interrupt
    has a missing (e.g. pre-upgrade card) or mismatched snapshot so the graph
    consumes the interrupt and emits a terminal ``scope_changed`` error tool
    result instead of leaving the interrupt stranded via HTTP 409.
    """
    from src.config import settings as app_settings
    from src.dalton.auth import is_dalton_supabase_auth_enabled
    from src.dalton.settings import get_dalton_auth_mode
    from src.server.database.conversation import senders
    from src.server.database.shared_workspaces import is_holder
    from src.server.models.chat import HITLDecision, HITLResponse

    def _forbid(code: str, message: str) -> HTTPException:
        return HTTPException(status_code=403, detail={"code": code, "message": message})

    # 1. First-party web session check (rejects service, oauth, pat, computer, channel).
    if principal is None or raw_request is None:
        raise _forbid(
            "web_session_required",
            "This confirmation requires an active first-party web session.",
        )
    if getattr(principal, "auth", None) != "session":
        raise _forbid(
            "web_session_required",
            "This confirmation requires an active first-party web session.",
        )
    headers = getattr(raw_request, "headers", None)
    if headers is None or (headers.get("X-Service-Token") or "").strip():
        raise _forbid(
            "web_session_required",
            "This confirmation requires an active first-party web session.",
        )
    if request.platform or request.external_thread_id:
        raise _forbid(
            "web_session_required",
            "This confirmation must be completed in the web app.",
        )

    is_oss_mode = (
        app_settings.HOST_MODE == "oss"
        and not is_dalton_supabase_auth_enabled()
        and get_dalton_auth_mode() == "oss"
    )
    if is_oss_mode:
        if (headers.get("Authorization") or "").strip():
            raise _forbid(
                "web_session_required",
                "Token-authenticated API clients cannot confirm this action in OSS mode.",
            )
        if not _verify_oss_loopback_web_caller(raw_request):
            raise _forbid(
                "web_session_required",
                "Loopback web confirmation requires a first-party browser or desktop app session.",
            )

    # 2. Initiator identity check.
    if not thread_meta:
        raise _forbid(
            "confirm_initiator_mismatch",
            "Only the initiator of this confirmation can answer it.",
        )
    owner_id = thread_meta.get("user_id")
    ws_owner_id = thread_meta.get("workspace_owner_id")
    is_multi_actor_thread = is_holder(owner_id) or (
        ws_owner_id is not None and str(ws_owner_id) != str(owner_id)
    )
    if is_multi_actor_thread:
        initiator = await senders.latest_turn_sender(thread_id)
    else:
        initiator = owner_id
    if not initiator or str(initiator) != str(principal.user_id):
        raise _forbid(
            "confirm_initiator_mismatch",
            "Only the initiator of this confirmation can answer it.",
        )

    # 3. Current pending interrupt_id check.
    answers = request.hitl_response or {}
    if not answers or any(iid not in pending_by_id for iid in answers):
        raise HTTPException(
            status_code=409,
            detail={"code": "scope_changed", "message": "情况已变化，请重新发起"},
        )

    # 4. Persisted approval_snapshots consistency check.
    # Missing (e.g. pre-upgrade card) or mismatched snapshots consume the
    # interrupt via a server-stamped ``scope_changed`` verdict so the tool
    # emits a terminal error result and reload never re-arms the stale card.
    checkpoint_data = (newest.checkpoint or {}) if newest is not None else {}
    channel_values = checkpoint_data.get("channel_values") or {}
    snapshots = channel_values.get("approval_snapshots")
    rewritten: dict[str, Any] | None = None
    for iid in answers:
        if iid not in guarded_by_id:
            continue
        # An explicit decline on an expired or pre-upgrade card settles as
        # rejected rather than being rewritten to approve + scope_changed, so
        # live display and history replay agree.
        if _is_reject_answer(answers[iid]):
            continue
        action_req = _interrupt_action_request(guarded_by_id[iid]) or {}
        tool_call_id = action_req.get("tool_call_id")
        snap = (
            snapshots.get(tool_call_id)
            if isinstance(snapshots, dict) and isinstance(tool_call_id, str)
            else None
        )
        if not isinstance(snap, dict) or not _snapshot_matches_action(snap, action_req):
            if rewritten is None:
                rewritten = dict(answers)
            rewritten[iid] = HITLResponse(
                decisions=[HITLDecision(type="approve")],
                scope_changed=True,
            )
    return rewritten
# --- DALTON-HOOK(cos-settings-tools): end ---


async def settle_resume_request(
    thread_id: str,
    request: ChatRequest,
    *,
    # DALTON-HOOK(cos-settings-tools): optional caller context for web-only HITL confirmation checks
    principal: Any = None,
    raw_request: Any = None,
    thread_meta: dict[str, Any] | None = None,
) -> ChatRequest:
    """The HITL answer ``request``, refused unless it resumes the pause the
    thread would resume next, else stamped with the run horizon START holds it
    to.

    A resume that names no checkpoint loads the newest, and after a fork
    whose run failed before its first step that is the replaced branch's
    pause, which no reader shows any more (``turn_base_checkpoint_id``). One
    that names its checkpoint must name that newest one. LangGraph resumes a
    named checkpoint wherever it sits, so an older pause would resume a card a
    later run already went past, putting its call through (an order
    included), and one under a flush or a compaction would run its step's
    finished calls again and drop what was written on top. A fork admitted
    after this check is caught at START, which refuses the answer if a run
    started after the horizon; read first, so no fork can fall between the
    check and the horizon.
    """
    branch = await threads_read.get_thread_branch_read(thread_id)
    checkpointer = get_checkpointer()
    stale = await turn_base_checkpoint_id(checkpointer, thread_id) is not None
    newest = None
    if not stale and (request.checkpoint_id or request.hitl_response):
        # DALTON-HOOK(cos-settings-tools): load newest checkpoint only when checkpoint_id or hitl_response needs it
        if request.checkpoint_id:
            newest = await checkpointer.aget_tuple(build_checkpoint_config(thread_id))
            stale = (
                newest is None
                or newest.config["configurable"]["checkpoint_id"] != request.checkpoint_id
            )
        else:
            maybe_newest = checkpointer.aget_tuple(build_checkpoint_config(thread_id))
            if inspect.isawaitable(maybe_newest):
                newest = await maybe_newest
    if stale:
        raise HTTPException(status_code=409, detail=stale_fork_detail())
    # --- DALTON-HOOK(cos-settings-tools): begin ---
    if request.hitl_response and newest is not None:
        pending_records = interrupt_records(getattr(newest, "pending_writes", None))
        pending_by_id = {
            str(rec["id"]): rec
            for rec in pending_records
            if rec.get("id") is not None
        }
        guarded_by_id = {
            iid: rec
            for iid, rec in pending_by_id.items()
            if _is_web_confirm_interrupt(rec)
        }
        rewritten_hitl = None
        if guarded_by_id and (
            any(iid in guarded_by_id for iid in request.hitl_response)
            or not set(request.hitl_response).issubset(set(pending_by_id))
        ):
            rewritten_hitl = await _verify_web_confirm_resume(
                thread_id=thread_id,
                request=request,
                newest=newest,
                pending_by_id=pending_by_id,
                guarded_by_id=guarded_by_id,
                principal=principal,
                raw_request=raw_request,
                thread_meta=thread_meta,
            )
        if rewritten_hitl is not None:
            return request.model_copy(
                update={
                    "resume_run_horizon": branch.run_horizon,
                    "hitl_response": rewritten_hitl,
                }
            )
    # --- DALTON-HOOK(cos-settings-tools): end ---
    return request.model_copy(update={"resume_run_horizon": branch.run_horizon})
