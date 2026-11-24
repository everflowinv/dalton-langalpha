"""A thread's own model, and the account default a thread without one follows.

Each thread runs the model a client last named for it
(``conversation_threads.llm_model``); NULL follows the account default for the
thread's mode. The send route reads it, never ``resolve_llm_config``:
automations call the run generators directly with their own model (null means
the account default), so keeping the read at the route keeps it off their turns
without a flag the resolver would have to trust.
"""

from __future__ import annotations

# DALTON-HOOK(agent-switch-model): imports for shared thread model switch and deferred handoff
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeVar
from uuid import uuid4

from fastapi import HTTPException

from src.dalton.database import model_switch as dalton_model_switch_db
from src.server.contracts.turn_kinds import NOTIFICATION_QUERY_TYPES, RUN_ORIGIN_META
from src.server.database.conversation import threads_read, threads_write

from . import logger, user_models
from .availability import model_resolves, raise_model_unavailable
from .config import _SLOT_MODEL_MAP, saved_default_model, system_default_models

#: Values of ``model_preference.default_model_scope``: what a change of the
#: account default does to existing threads.
DEFAULT_MODEL_SCOPES = frozenset({"ask", "new_threads", "existing_threads"})

#: The preference keys an account default is read from.
DEFAULT_MODEL_KEYS = (
    *(pref_key for _field, pref_key in _SLOT_MODEL_MAP.values()),
    "background_follows",
)


@dataclass(frozen=True)
class NamedModel:
    """A model a send named for its thread, with the model the thread held
    when the send read it. The send keeps its model only while the thread
    still holds that one, so a pick saved while the turn was starting wins."""

    name: str
    seen: str | None


async def turn_model(
    user_id: str,
    thread_id: str,
    *,
    named: str | None,
    held: str | None,
    clear_dead: bool = True,
) -> str | None:
    """The model this turn runs: the one it names, else the one the thread
    holds; None runs the account default.

    A dead held model is cleared rather than refused, so the turn runs the
    default instead of every turn on the thread failing until someone picks
    another. A dead named model is returned for the resolver to refuse as
    ``model_removed`` (its test agrees with ``model_resolves``); when the thread
    holds it too, it is cleared with that refusal, so the client's refetch
    shows the default rather than resending it. A clear that loses to a pick
    saved since the send read the thread runs the pick, as the send's own
    write would yield to it.

    A read of what the next turn would run passes ``clear_dead=False``: it
    gets the same answer and leaves the clearing to that turn.
    """
    if named and named != held:
        return named  # nothing of the thread's at stake; the resolver judges it
    model = named or held
    if not model:
        return None
    prefs = await user_models.get_model_preference(user_id)
    if model_resolves(prefs, model):
        return model
    if not clear_dead:
        return named
    if await threads_write.clear_thread_llm_model(thread_id, model):
        logger.info(
            f"[CHAT] Cleared thread {thread_id}'s model {model!r}: no longer available"
        )
    elif not named:
        meta = await threads_read.get_thread_auth_meta(thread_id)
        current = meta.get("llm_model") if meta else None
        if current and model_resolves(prefs, current):
            return current
    return named


#: Refusals of a model the catalog still lists but this user cannot run right
#: now: an account to reconnect, a key to add, a plan that does not cover it,
#: a policy that does not allow it, a base URL the server will not dial.
#: ``model_removed`` is here too, in case
#: the availability check and the resolver ever disagree about a model
#: ``turn_model`` kept.
_UNSERVABLE_REFUSALS = frozenset({
    "oauth_required",
    "oauth_reauth_required",
    "oauth_plan_unsupported",
    "oauth_plan_no_api_access",
    "model_not_on_plan",
    "byok_key_required",
    "model_unavailable",
    "model_removed",
    "base_url_blocked",
    "policy_denied",
})


def _runs_default_instead(exc: HTTPException, *, named: str | None, model: str | None) -> bool:
    """Whether a turn refused on the thread's own model may run the account default.

    Only a turn that named no model, which ran the thread's model because the
    thread holds it, not because anyone asked. The thread keeps its model, so
    turns return to it once the account does.
    """
    if named or not model or exc.status_code != 400:
        return False
    return isinstance(exc.detail, dict) and exc.detail.get("type") in _UNSERVABLE_REFUSALS


_Config = TypeVar("_Config")


async def resolve_turn_config(
    user_id: str,
    thread_id: str,
    *,
    named: str | None,
    held: str | None,
    fall_back: bool,
    resolve: Callable[[str | None], Awaitable[_Config]],
    clear_dead: bool = True,
) -> _Config:
    """Resolve what a turn on this thread runs: ``turn_model``'s choice, or,
    with ``fall_back``, the account default when the thread's own model cannot
    run right now and the turn named none.

    ``fall_back`` is for turns a refusal would drop, with nobody at a client to
    reconnect the account or add the key; a person gets the refusal, which says
    what to fix. A send and a manual compaction both come through here, so a
    summary runs on the model and credential the thread's turns do. ``resolve``
    maps a model name (None for the account default) to the caller's config,
    and ``clear_dead`` is ``turn_model``'s.
    """
    model = await turn_model(user_id, thread_id, named=named, held=held, clear_dead=clear_dead)
    try:
        return await resolve(model)
    except HTTPException as exc:
        if not (fall_back and _runs_default_instead(exc, named=named, model=model)):
            raise
        logger.info(
            f"[CHAT] Thread {thread_id}'s model {model!r} cannot run "
            f"({exc.detail.get('type')}); this turn runs the account default"
        )
        return await resolve(None)


async def require_selectable_model(user_id: str, name: str) -> None:
    """Refuse a name that resolves to no model before a thread keeps it, so
    the failure lands on the pick rather than on every later turn.

    Credentials are left to the turn: a lapsed connection comes back, a model
    missing from the catalog does not.
    """
    pref = await user_models.get_model_preference(user_id)
    if not model_resolves(pref, name):
        raise_model_unavailable(name)


# DALTON-HOOK(agent-switch-model): shared thread model switch (validate -> write -> immediate/deferred handoff)
PENDING_MODEL_HANDOFF_KEY = "dalton_pending_model_handoff"
# Switches between two hand-offs are few; keep the most recent sources.
MAX_HANDOFF_CANDIDATE_MODELS = 8

HandoffOutcome = Literal["completed", "not_needed", "retryable_failure"]


def extract_pending_model_handoff(row: Any) -> dict[str, Any] | None:
    """Return the pending model-handoff marker dict from a thread row, or None."""
    if not isinstance(row, dict):
        return None
    pending = (row.get("metadata") or {}).get(PENDING_MODEL_HANDOFF_KEY)
    return pending if isinstance(pending, dict) else None


@dataclass(frozen=True)
class ThreadModelSwitchPlan:
    """Validated plan before writing ``llm_model`` on ``conversation_threads``."""

    meta: dict[str, Any] | None
    workspace_id: str | None
    previous_model: str | None
    pending_previous_model: str | None
    model: str | None
    changed: bool
    pending_candidate_models: tuple[str, ...] = ()


@dataclass(frozen=True)
class ThreadModelSwitchResult:
    """Outcome of switching a thread's model and running or deferring hand-off."""

    thread: dict[str, Any] | None
    workspace_id: str | None
    previous_model: str | None
    model: str | None
    changed: bool
    brief_handoff: bool | None = None
    summary_handoff: bool | None = None


async def prepare_thread_model_switch(
    user_id: str,
    thread_id: str,
    model: str | None,
    *,
    model_pref: dict[str, Any] | None = None,
    short_circuit_default: bool = False,
    running_model: str | None = None,
) -> ThreadModelSwitchPlan:
    """Validate ``model`` (None returns to account default) and workspace rules
    before persisting ``llm_model``.

    Uses workspace holder identity (``is_holder``) rather than
    ``conversation_threads.is_shared`` so publicly shared personal threads can
    still switch models while shared-workspace threads remain per-message.
    Short-circuits (``changed=False``) when the target matches the thread's
    current model, and — when ``short_circuit_default=True`` (tool entry) —
    when ``llm_model`` is NULL and ``model`` equals the active
    ``effective_default_model``. When ``llm_model`` is NULL, records the
    resolved default model ID as ``pending_previous_model``.
    """
    from src.server.database.shared_workspaces import is_holder

    if model is not None:
        await require_selectable_model(user_id, model)

    meta = await threads_read.get_thread_auth_meta(thread_id)
    if meta is not None and (
        is_holder(meta.get("user_id"))
        or is_holder(meta.get("workspace_owner_id"))
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "type": "shared_thread_setting",
                "message": "In a shared workspace a thread's model and "
                "settings are chosen with each message.",
            },
        )
    switched_from = meta.get("llm_model") if meta is not None else None
    ws_id = str(meta["workspace_id"]) if meta and meta.get("workspace_id") else None

    if meta is not None and switched_from == model:
        return ThreadModelSwitchPlan(
            meta=meta,
            workspace_id=ws_id,
            previous_model=switched_from,
            pending_previous_model=switched_from,
            model=model,
            changed=False,
        )

    pref = model_pref
    resolved_default: str | None = None
    if switched_from is None:
        if pref is None:
            try:
                pref = await user_models.get_model_preference(user_id)
            except RuntimeError:
                # Unit tests that mock require_selectable_model without a DB pool
                # cannot read preferences to resolve the account default.
                pref = None
        resolved_default = effective_default_model(pref if isinstance(pref, dict) else {})

    if (
        short_circuit_default
        and meta is not None
        and switched_from is None
        and model is not None
        and isinstance(pref, dict)
        and resolved_default == model
    ):
        return ThreadModelSwitchPlan(
            meta=meta,
            workspace_id=ws_id,
            previous_model=None,
            pending_previous_model=None,
            model=model,
            changed=False,
        )

    existing_pending = (meta.get("metadata") or {}).get(PENDING_MODEL_HANDOFF_KEY) if meta else None
    existing_prev = (
        existing_pending.get("previous_model")
        if isinstance(existing_pending, dict)
        else None
    )
    # Source of the hand-off, in order: an earlier marker, the thread's stored
    # model (a one-off automation override does not rewrite its summary), and only
    # for a thread that follows the account default, the model this turn was built
    # with (the default may have changed mid-turn) before the current default.
    if existing_prev is not None:
        pending_prev = existing_prev
    elif switched_from is not None:
        pending_prev = switched_from
    else:
        pending_prev = running_model or resolved_default
    # Every model that may have written the summary still owed a hand-off: an
    # earlier marker's candidates, this plan's source, and the model this turn was
    # built with (an automation override may have compacted with it). The hand-off
    # tries each, so none of them has to be guessed right.
    candidates: list[str] = []
    if isinstance(existing_pending, dict):
        listed = existing_pending.get("candidate_models")
        if isinstance(listed, list):
            candidates.extend(str(m) for m in listed if m)
        for key in ("previous_model", "alt_previous_model"):
            if existing_pending.get(key):
                candidates.append(str(existing_pending[key]))
    for m in (pending_prev, running_model):
        if m:
            candidates.append(str(m))
    seen: set[str] = set()
    unique = [m for m in candidates if not (m in seen or seen.add(m))]
    # Drop the oldest when full: the newest turn is the likeliest summary writer.
    pending_candidates = tuple(unique[-MAX_HANDOFF_CANDIDATE_MODELS:])

    return ThreadModelSwitchPlan(
        meta=meta,
        workspace_id=ws_id,
        previous_model=switched_from,
        pending_previous_model=pending_prev,
        pending_candidate_models=pending_candidates,
        model=model,
        changed=True,
    )


_immediate_handoff_runner: (
    Callable[
        [str, str, str | None, ThreadModelSwitchPlan],
        Awaitable[tuple[bool | None, bool | None]],
    ]
    | None
) = None
_pending_handoff_runner: Callable[..., Awaitable[HandoffOutcome]] | None = None


def register_handoff_runners(
    *,
    immediate: Callable[
        [str, str, str | None, ThreadModelSwitchPlan],
        Awaitable[tuple[bool | None, bool | None]],
    ],
    pending: Callable[..., Awaitable[HandoffOutcome]],
) -> None:
    """Register handler-tier handoff executors without inverting tier imports."""
    global _immediate_handoff_runner, _pending_handoff_runner
    _immediate_handoff_runner = immediate
    _pending_handoff_runner = pending


@dataclass(frozen=True)
class PendingModelHandoffContext:
    """Validated pending model-switch marker context read from thread metadata."""

    user_id: str
    workspace_id: str | None
    previous_model: str | None
    current_model: str | None
    version: str | None
    candidate_models: tuple[str, ...] = ()


async def read_pending_model_handoff_context(
    thread_id: str,
    *,
    user_id: str | None = None,
) -> tuple[PendingModelHandoffContext | None, HandoffOutcome]:
    """Read and validate the thread's pending handoff marker."""
    from src.server.database.shared_workspaces import is_holder

    try:
        meta = await threads_read.get_thread_auth_meta(thread_id)
    except Exception:
        logger.warning(
            f"[handoff] thread {thread_id}: failed reading pending model handoff",
            exc_info=True,
        )
        return None, "retryable_failure"
    pending = (meta.get("metadata") or {}).get(PENDING_MODEL_HANDOFF_KEY) if meta else None
    if not isinstance(pending, dict):
        return None, "not_needed"

    version = pending.get("version") if isinstance(pending.get("version"), str) else None
    effective_user_id = user_id or pending.get("user_id") or meta.get("user_id")
    if is_holder(effective_user_id) and meta.get("workspace_owner_id"):
        effective_user_id = meta.get("workspace_owner_id")
    if not effective_user_id or is_holder(effective_user_id):
        logger.warning(
            f"[handoff] thread {thread_id}: cannot run pending handoff without owner user_id"
        )
        return None, "retryable_failure"

    workspace_id = str(meta["workspace_id"]) if meta.get("workspace_id") else None
    return (
        PendingModelHandoffContext(
            user_id=effective_user_id,
            workspace_id=workspace_id,
            previous_model=pending.get("previous_model"),
            current_model=meta.get("llm_model"),
            version=version,
            candidate_models=tuple(
                str(m)
                for m in (
                    pending.get("candidate_models")
                    if isinstance(pending.get("candidate_models"), list)
                    else [pending.get("previous_model"), pending.get("alt_previous_model")]
                )
                if m
            ),
        ),
        "completed",
    )


def build_pending_model_handoff_marker(
    *,
    previous_model: str | None,
    user_id: str,
    version: str | None = None,
    candidate_models: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """Build a versioned pending-handoff metadata payload."""
    marker: dict[str, Any] = {
        "previous_model": previous_model,
        "user_id": user_id,
        "version": version or uuid4().hex,
    }
    if candidate_models:
        marker["candidate_models"] = list(candidate_models)
    return marker


async def clear_pending_model_handoff(
    thread_id: str,
    *,
    version: str | None,
) -> bool:
    """Clear ``PENDING_MODEL_HANDOFF_KEY`` only if its version matches ``version``."""
    return await dalton_model_switch_db.clear_thread_metadata_key(
        thread_id,
        PENDING_MODEL_HANDOFF_KEY,
        expected_version=version,
    )


async def resolve_retried_turn_origin(
    thread_id: str,
    retried_run: Mapping[str, Any] | None,
) -> str | None:
    """Resolve the persisted origin of ``retried_run`` so ``/retry`` keeps non-user provenance."""
    if not isinstance(retried_run, Mapping):
        return None
    meta = retried_run.get("metadata") or {}
    if isinstance(meta, Mapping):
        carried = meta.get("retried_origin")
        if isinstance(carried, str) and carried:
            return carried
        stamped = meta.get(RUN_ORIGIN_META)
        if stamped in ("automation", "report", "wakeup", "market_target"):
            return str(stamped)
        if meta.get("automation_id") or meta.get("automation_execution_id"):
            return "automation"
        qtype = meta.get("query_type")
        if qtype in NOTIFICATION_QUERY_TYPES:
            return str(qtype)
        raw_origin = meta.get("origin")
        if isinstance(raw_origin, Mapping) and raw_origin.get("type"):
            return str(raw_origin["type"])
        if isinstance(raw_origin, str) and raw_origin:
            return raw_origin
    turn_index = retried_run.get("turn_index")
    if turn_index is not None:
        from src.server.database.conversation.queries import get_query_at_turn

        try:
            query = await get_query_at_turn(thread_id, int(turn_index))
        except RuntimeError:
            query = None
        if isinstance(query, Mapping):
            qtype = query.get("type")
            if qtype in NOTIFICATION_QUERY_TYPES:
                return str(qtype)
            qmeta = query.get("metadata") or {}
            q_origin = qmeta.get("origin") if isinstance(qmeta, Mapping) else None
            if isinstance(q_origin, Mapping) and q_origin.get("type"):
                return str(q_origin["type"])
    stamped = meta.get(RUN_ORIGIN_META) if isinstance(meta, Mapping) else None
    return str(stamped) if isinstance(stamped, str) and stamped else "user"


async def run_pending_model_handoff(
    thread_id: str,
    *,
    user_id: str | None = None,
) -> HandoffOutcome:
    """Execute a deferred model-switch handoff via the registered handler runner."""
    # Accepted race: if post-turn handoff acquires T(thread) right before the next
    # message's begin_run, that message may get a retryable contention error rather
    # than wrapping upstream begin_run.
    if _pending_handoff_runner is None:
        return "not_needed"
    return await _pending_handoff_runner(thread_id, user_id=user_id)


async def run_pending_model_handoff_best_effort(thread_id: str) -> None:
    """Run deferred post-turn model handoff without ever failing turn lifecycle."""
    try:
        await run_pending_model_handoff(thread_id)
    except Exception:
        logger.warning(
            f"[RunCoordinator] deferred model handoff failed for thread={thread_id}",
            exc_info=True,
        )


async def switch_thread_model(
    user_id: str,
    thread_id: str,
    model: str | None,
    *,
    defer_handoff: bool = False,
    model_pref: dict[str, Any] | None = None,
    short_circuit_default: bool = False,
    running_model: str | None = None,
) -> ThreadModelSwitchResult:
    """Validate ``model`` (None returns to account default), persist
    ``llm_model`` on ``thread_id``, and either run hand-off immediately or
    atomically store a versioned ``PENDING_MODEL_HANDOFF_KEY`` marker.
    """
    plan = await prepare_thread_model_switch(
        user_id,
        thread_id,
        model,
        model_pref=model_pref,
        short_circuit_default=short_circuit_default,
        running_model=running_model,
    )
    if not plan.changed:
        return ThreadModelSwitchResult(
            thread=None,
            workspace_id=plan.workspace_id,
            previous_model=plan.previous_model,
            model=model,
            changed=False,
        )

    if defer_handoff:
        marker = build_pending_model_handoff_marker(
            previous_model=plan.pending_previous_model,
            user_id=user_id,
            candidate_models=plan.pending_candidate_models,
        )
        updated_thread = await threads_write.update_thread_fields(
            thread_id,
            llm_model=model,
            metadata_merge={PENDING_MODEL_HANDOFF_KEY: marker},
        )
        if not updated_thread:
            raise HTTPException(
                status_code=404, detail=f"Thread not found: {thread_id}"
            )
        workspace_id = (
            str(updated_thread["workspace_id"])
            if updated_thread.get("workspace_id")
            else None
        )
        return ThreadModelSwitchResult(
            thread=updated_thread,
            workspace_id=workspace_id,
            previous_model=plan.pending_previous_model,
            model=model,
            changed=True,
        )

    # DALTON-HOOK(cos-settings-tools): stamp owner model switch after a system-only run so immediate manual compaction uses the owner's selection
    switch_meta_merge: dict[str, Any] | None = None
    try:
        from src.server.database.runs import lifecycle as _runs_lifecycle

        _latest_attempt = await _runs_lifecycle.get_latest_attempt(thread_id)
        if (
            isinstance(_latest_attempt, dict)
            and (_latest_attempt.get("metadata") or {}).get("llm_model_system_only")
            and _latest_attempt.get("conversation_response_id")
        ):
            switch_meta_merge = {
                "owner_model_switch_run_id": str(
                    _latest_attempt["conversation_response_id"]
                )
            }
    except Exception:
        logger.debug(
            "latest run read for system-only switch stamp failed on thread %s",
            thread_id,
            exc_info=True,
        )

    if switch_meta_merge is not None:
        updated_thread = await threads_write.update_thread_fields(
            thread_id, llm_model=model, metadata_merge=switch_meta_merge
        )
    else:
        updated_thread = await threads_write.update_thread_fields(
            thread_id, llm_model=model
        )
    if not updated_thread:
        raise HTTPException(
            status_code=404, detail=f"Thread not found: {thread_id}"
        )

    workspace_id = (
        str(updated_thread["workspace_id"])
        if updated_thread.get("workspace_id")
        else None
    )
    if _immediate_handoff_runner is None:
        logger.warning(
            f"[handoff] thread {thread_id}: immediate handoff runner not registered"
        )
        brief_handoff, summary_handoff = None, None
    else:
        brief_handoff, summary_handoff = await _immediate_handoff_runner(
            user_id, thread_id, workspace_id, plan
        )
    return ThreadModelSwitchResult(
        thread=updated_thread,
        workspace_id=workspace_id,
        previous_model=plan.previous_model,
        model=model,
        changed=True,
        brief_handoff=brief_handoff,
        summary_handoff=summary_handoff,
    )


def effective_default_model(model_pref: dict) -> str | None:
    """The model a thread following the account default runs.

    The composer's rule and the one a turn runs on (``select_model``), so
    "threads still on the old default" means what the user saw on them.
    """
    return (
        saved_default_model(model_pref, "primary")
        or system_default_models().get("default_model")
        or None
    )


async def carry_default_change(
    user_id: str, *, before: dict, after: dict, apply_to: str | None, conn=None
) -> int:
    """Carry a changed account default onto existing threads when asked to;
    returns how many threads moved.

    ``before`` and ``after`` are the model preferences either side of the
    write, and ``conn`` its transaction, so the threads move with it or not at
    all. ``apply_to`` is the write's one-shot answer; without one the saved
    ``default_model_scope`` decides, which is how a writer that cannot ask (a
    channel command, the setup wizard) honours the user's setting. Only
    ``existing_threads`` moves anything: ``ask`` with no one to ask means new
    threads only.
    """
    scope = apply_to or after.get("default_model_scope")
    if scope != "existing_threads":
        return 0
    old, new = effective_default_model(before), effective_default_model(after)
    # With no old default no thread can be on it. A thread moved onto a
    # Claude Code row opens its next turn on the brief its model writes then
    # (``brief_for_turn``).
    if not old or old == new:
        return 0
    moved = await threads_write.reassign_thread_llm_models(user_id, old, new, conn=conn)
    logger.info(
        f"[PREFS] Moved {moved} thread(s) to the new default for user={user_id}: "
        f"{old} -> {new}"
    )
    return moved


async def next_turn_runs_claude_code(user_id: str, workspace_id: str | None, held: str | None) -> bool:
    """Whether the thread's next turn, naming no model, runs on Claude Code:
    on the thread's own model, ``held``, while it still resolves (``turn_model``),
    else on the account default (``effective_default_model``), which the
    Chief of Staff runs only where ``chief_of_staff_default_runs`` lets it,
    as ``resolve_llm_config`` does, and on the deployment's Default
    otherwise. Never while the user's Claude Code models are off
    (``rows_on``): a held one is refused at the send, and a brief written
    for it would be paid for and never read."""
    from src.llms.claude_code import is_claude_code_model
    from src.server.database.home_workspace import is_home_id
    from src.server.services.claude_code.flow import rows_on
    from src.server.services.llm.claude_code import chief_of_staff_default_runs

    prefs = await user_models.get_model_preference(user_id)
    if held and model_resolves(prefs, held):
        return is_claude_code_model(held) and await rows_on(user_id)
    if not is_claude_code_model(effective_default_model(prefs)) or not await rows_on(user_id):
        return False
    return not is_home_id(user_id, workspace_id) or await chief_of_staff_default_runs(user_id, workspace_id) is not None
