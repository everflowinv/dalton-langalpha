"""Workflow run journal, resume admission and progress marker (workflow-resume runtime).

The platform's workflow driver and tool call into this module; every piece of
state it touches lives in workspace files (the run's ``journal/`` directory,
the ``.progress.json`` marker), so any worker can resume any interrupted run
without process-shared state. Nothing here dispatches children or owns run
lifecycle — the driver stays the executor, this is the record.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shlex
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Journal filenames keep the label's letters, digits, CJK and a few
# separators; anything else (slashes above all) collapses to one ``_``.
JOURNAL_NAME_SAFE = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fff.-]+")
JOURNAL_DIR = "journal"


class ResumeRefused(Exception):
    """A run that cannot be resumed, carrying the caller-facing reason."""


def engine_safe(value: Any) -> Any:
    """One value copy with every empty string replaced by ``None``.

    quickjs_rs's async host-call marshaling deadlocks the whole run on an
    empty string anywhere in the returned value (any other falsy value —
    ``False``, ``0``, ``[]``, ``{}``, ``None``, ``" "`` — marshals fine), so
    a child that answers ``""`` for an optional field would kill the run at
    the boundary. ``""`` carries no information ``None`` doesn't, so it is
    normalized here rather than trusted upstream of a known engine bug.
    Parsed schema results are acyclic (they came out of ``json.loads``).
    """
    if isinstance(value, str):
        return None if value == "" else value
    if isinstance(value, list):
        return [engine_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: engine_safe(item) for key, item in value.items()}
    return value


def declared_artifact_paths(result: Any) -> list[str]:
    """The workspace paths a child's result says it wrote.

    The Dalton workflows' children declare artifacts two ways: the shared
    response schemas carry ``files`` (a list of paths, ``path|sha256`` pairs,
    or ``{path}`` objects — the flattened evidence schema uses the pipe form),
    and the singular ``file`` of a single-deliverable child (the weekly screen
    and the topic plan). Anything else declares nothing, and its journal
    entry simply verifies on label+prompt alone.
    """
    if not isinstance(result, dict):
        return []

    def _path_of(item: Any) -> str | None:
        if isinstance(item, str) and item.strip():
            return item.strip().split("|", 1)[0].strip()
        if isinstance(item, dict):
            path = item.get("path") or item.get("file")
            if isinstance(path, str) and path.strip():
                return path.strip()
        return None

    candidates: list[Any] = []
    files = result.get("files")
    if isinstance(files, list):
        candidates.extend(files)
    single = result.get("file")
    if single is not None:
        candidates.append(single)
    return [p for p in (_path_of(item) for item in candidates) if p]


async def _read_json(backend: Any, path: str) -> Any:
    """A journal JSON file, or None when it is confirmed absent.

    A read that fails for any other reason raises: resume decisions must not
    read a dead transport as "nothing recorded". (Callers that merely append
    catch this and skip their update — the best-effort arm.)"""
    try:
        text = await backend.aread_text(path)
    except FileNotFoundError:
        return None
    except TypeError:
        # Fakes and store routes signal absence this way; a real decode
        # failure on an existing file surfaces as None below and is the
        # caller's (resume: refusing) business.
        return None
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError as error:
        raise ResumeRefused(f"journal file unparseable ({path}); refuse to resume") from error


async def _write_json(backend: Any, path: str, value: Any) -> bool:
    """Best-effort JSON write through the run's filesystem backend."""
    try:
        return bool(
            await backend.awrite_text(path, json.dumps(value, ensure_ascii=False, default=str))
        )
    except Exception:
        logger.debug("workflow journal write failed", extra={"path": path}, exc_info=True)
        return False


def _workspace_cd(backend: Any) -> str:
    """``cd <workspace> && `` prefix — aexecute runs at the computer root
    while the paths being hashed are workspace-relative."""
    workspace_dir = getattr(backend, "workspace_dir", None)
    if workspace_dir:
        return f"cd {shlex.quote(str(workspace_dir))} && "
    return ""


async def digest_paths(backend: Any, paths: list[str]) -> dict[str, str | None]:
    """sha256 per path, preferring one sandbox ``sha256sum`` over N reads;
    falls back to a text read where the backend cannot execute."""
    execute = getattr(backend, "aexecute", None)
    if execute is not None and paths:
        try:
            response = await execute(
                _workspace_cd(backend)
                + "sha256sum -- "
                + " ".join(shlex.quote(p) for p in paths)
            )
            if int(getattr(response, "exit_code", 1)) == 0:
                digests: dict[str, str | None] = {path: None for path in paths}
                for line in str(getattr(response, "output", "")).splitlines():
                    parts = line.split(None, 1)
                    if len(parts) != 2:
                        continue
                    digest, echoed = parts[0], parts[1].lstrip("*").strip()
                    for path in paths:
                        if echoed == path or echoed.endswith(f"/{path}"):
                            digests[path] = digest
                return digests
        except Exception:
            logger.debug("workflow journal sha256sum failed", exc_info=True)
    digests = {}
    for path in paths:
        text = None
        try:
            text = await backend.aread_text(path)
        except Exception:
            text = None
        digests[path] = (
            hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None
        )
    return digests


async def artifact_records(
    backend: Any, result: Any
) -> list[dict[str, Any]] | None:
    """``[{path, sha256}]`` for the artifacts a child declared it wrote, or
    ``None`` when a declared artifact cannot be hashed.

    ``None`` is the "this record must never be replayed" verdict: a file the
    child says it wrote but whose digest cannot be computed (binary through a
    text-only backend, a file already moved) means a later resume cannot
    verify the dispatch's outputs, so the entry is journaled as
    non-replayable rather than replaying on a partial pin.
    """
    if (
        isinstance(result, dict)
        and isinstance(result.get("path"), str)
        and "files" not in result
        and "file" not in result
    ):
        # A state query (the final-model resolver's {path, sha256}) reports
        # what is on disk now; a resume must ask again, since a version saved
        # after the interruption would not change the old answer's pin.
        return None
    paths = declared_artifact_paths(result)
    if not paths:
        return []
    digests = await digest_paths(backend, paths)
    if any(digest is None for digest in digests.values()):
        return None
    return [
        {"path": path, "sha256": digest}
        for path, digest in digests.items()
        if digest is not None
    ]


def prompt_digest(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


class WorkflowJournal:
    """One run's dispatch journal plus its workspace progress marker.

    The driver appends a pending record before each dispatch and rewrites it
    with the outcome after; a resumed run (``journal_source`` set) replays a
    recorded dispatch instead of re-dispatching when label, prompt hash and
    every recorded artifact hash still match. All truth is in the files.
    """

    def __init__(
        self,
        backend: Any,
        *,
        base_rel: str,
        run_id: str,
        workflow: str,
        journal_source: str | None = None,
        progress_path: str | None = None,
    ) -> None:
        self.backend = backend
        self.base_rel = base_rel
        self.run_id = run_id
        self.workflow = workflow
        self.journal_source = journal_source
        self.progress_path = progress_path
        self.phases: list[str] = []
        self.dispatched = 0
        self._lock = asyncio.Lock()
        self._resume_index: dict[str, list[dict[str, Any]]] | None = None

    def note_phase(self, title: str) -> None:
        """Best-effort memory of where the run is; persisted at the next
        journal append / progress write rather than here — the driver's
        ``phase`` runs on the QuickJS worker thread and must not touch files."""
        self.phases.append(title[:120])

    def journal_file(self, label: str) -> str:
        safe = JOURNAL_NAME_SAFE.sub("_", label)[:80] or "dispatch"
        return f"{self.base_rel}/{JOURNAL_DIR}/{safe}.json"

    async def append(self, entry: dict[str, Any]) -> None:
        """Append one dispatch record to its label's journal file, best-effort.

        A journal that cannot be written costs resume-ability on this run, not
        the run itself — the failure is logged and the dispatch proceeds.
        """
        path = self.journal_file(str(entry.get("label") or "dispatch"))
        try:
            async with self._lock:
                records: dict[str, Any] = {"label": entry.get("label"), "dispatches": []}
                existing = await _read_json(self.backend, path)
                if isinstance(existing, dict) and isinstance(
                    existing.get("dispatches"), list
                ):
                    records = existing
                records["dispatches"].append(entry)
                await _write_json(self.backend, path, records)
        except Exception:
            logger.debug(
                "workflow journal append failed", extra={"path": path}, exc_info=True
            )
        await self.write_progress("running")

    async def find_replayable(self, label: str, prompt: str) -> dict[str, Any] | None:
        """The journal entry a resume may replay, or None to dispatch.

        A replay demands more than label+prompt: the entry must have been
        recorded replayable (every declared artifact hashed), and every
        recorded artifact must still hash the same — a file someone touched
        since forces the dispatch again rather than resurrecting stale work.
        """
        index = await self._load_resume_index()
        for entry in index.get(label) or []:
            if (
                entry.get("prompt_sha256") != prompt_digest(prompt)
                or entry.get("status") != "ok"
                or entry.get("replayable") is not True
            ):
                continue
            artifacts = entry.get("artifacts") or []
            if artifacts:
                current = await digest_paths(
                    self.backend, [a["path"] for a in artifacts]
                )
                if any(current.get(a["path"]) != a.get("sha256") for a in artifacts):
                    continue
            return entry
        return None

    async def record_replay(
        self, entry: dict[str, Any], *, seq: int, label: str, phase: str | None
    ) -> None:
        """Journal a replayed dispatch into this run, marked with its origin."""
        await self.append(
            {
                "seq": seq,
                "label": label,
                "phase": phase,
                "prompt_sha256": entry["prompt_sha256"],
                "status": "ok",
                "result": entry.get("result"),
                "result_kind": entry.get("result_kind", "parsed"),
                "artifacts": entry.get("artifacts") or [],
                # Replayed entries stay replayable with their artifact pins:
                # a second interruption resumes off THIS run's journal, which
                # now carries the full history forward.
                "replayable": True,
                "resumed_from": self.journal_source,
                "recorded_at": time.time(),
            }
        )

    async def write_progress(self, status: str) -> None:
        """Keep the workspace progress marker current, best-effort.

        Written on every journal append and at finalize, so the marker lags
        the run by at most one dispatch — the launching skill reads it to
        find an interrupted run and its task id to resume.
        """
        if not self.progress_path:
            return
        await _write_json(
            self.backend,
            self.progress_path,
            {
                "workflow": self.workflow,
                "run_id": self.run_id,
                "phases": self.phases,
                "dispatched": self.dispatched,
                "status": status,
                "resumed_from": self.journal_source,
                "updated_at": time.time(),
            },
        )

    async def _load_resume_index(self) -> dict[str, list[dict[str, Any]]]:
        """label → journal entries, read once from the resumed run's dir."""
        if self._resume_index is not None:
            return self._resume_index
        index: dict[str, list[dict[str, Any]]] = {}
        if self.journal_source:
            glob_paths = getattr(self.backend, "aglob_paths", None)
            listing: list[str] = []
            if glob_paths is not None:
                try:
                    listing = [
                        str(p)
                        for p in await glob_paths(
                            f"{self.journal_source}/{JOURNAL_DIR}/*.json", "."
                        )
                    ]
                except Exception as error:
                    # An unlistable source journal must not read as "empty":
                    # that would re-dispatch every finished step.
                    raise ResumeRefused(
                        "could not list the resumed run's journal "
                        f"({self.journal_source}); refuse to resume"
                    ) from error
            for path in listing:
                try:
                    doc = await _read_json(self.backend, path)
                except Exception as error:
                    raise ResumeRefused(
                        f"journal file read failed ({path}); refuse to resume"
                    ) from error
                if doc is None or not isinstance(doc, dict):
                    raise ResumeRefused(
                        f"journal file unreadable ({path}); refuse to resume"
                    )
                for entry in doc.get("dispatches") or []:
                    if isinstance(entry, dict) and isinstance(entry.get("label"), str):
                        index.setdefault(entry["label"], []).append(entry)
        self._resume_index = index
        return index


async def journal_entries(
    backend: Any, run_dir: str
) -> list[dict[str, Any]] | None:
    """Every dispatch entry recorded under one run's journal directory, or
    ``None`` when the directory cannot be listed — admission decisions treat
    that as "cannot verify", never as "nothing recorded"."""
    glob_paths = getattr(backend, "aglob_paths", None)
    if glob_paths is None:
        return None
    try:
        listing = [str(p) for p in await glob_paths(f"{run_dir}/{JOURNAL_DIR}/*.json", ".")]
    except Exception:
        logger.warning("journal glob failed", extra={"run_dir": run_dir}, exc_info=True)
        return None
    entries: list[dict[str, Any]] = []
    for path in listing:
        try:
            doc = await _read_json(backend, path)
        except Exception as error:
            logger.warning(
                "journal entry read failed",
                extra={"path": path},
                exc_info=True,
            )
            raise ResumeRefused(
                f"journal file read failed ({path}); refuse to resume"
            ) from error
        if doc is None:
            # Listed but unreadable-or-absent: at admission a listed journal
            # file cannot legitimately vanish — that is a read failure.
            raise ResumeRefused(
                f"journal file unreadable ({path}); refuse to resume"
            )
        if not isinstance(doc, dict):
            raise ResumeRefused(
                f"journal file is not a journal document ({path}); refuse to resume"
            )
        for entry in doc.get("dispatches") or []:
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


_EXISTS_SENTINEL = "__EXISTS__"
_ABSENT_SENTINEL = "__ABSENT__"


async def _probe_exists(backend: Any, path: str) -> bool:
    """Existence via a sentinel-echoing shell probe in the workspace folder.

    The command echoes exactly one of two sentinels; the answer counts only
    when the command exited 0 AND one sentinel is present. An exception, a
    nonzero exit (a failed ``cd`` included), or output without a sentinel is
    a read failure and refuses — this is admission, and "cannot see" must
    not pass as "not there"."""
    execute = getattr(backend, "aexecute", None)
    if execute is None:
        raise ResumeRefused(
            f"resume marker existence cannot be probed ({path}); refuse to resume"
        )
    command = (
        _workspace_cd(backend)
        + f"if test -e {shlex.quote(path)}; then echo {_EXISTS_SENTINEL}; "
        f"else echo {_ABSENT_SENTINEL}; fi"
    )
    try:
        response = await execute(command)
    except Exception as error:
        raise ResumeRefused(
            f"resume marker existence probe failed ({path}); refuse to resume"
        ) from error
    code = int(getattr(response, "exit_code", 1))
    output = str(getattr(response, "output", "") or "")
    if code != 0:
        raise ResumeRefused(
            f"resume marker existence probe exited {code} ({path}); refuse to resume"
        )
    if _EXISTS_SENTINEL in output and _ABSENT_SENTINEL not in output:
        return True
    if _ABSENT_SENTINEL in output and _EXISTS_SENTINEL not in output:
        return False
    raise ResumeRefused(
        f"resume marker existence probe returned no sentinel ({path}); "
        "refuse to resume"
    )


async def _read_marker(backend: Any, path: str) -> dict[str, Any] | None:
    """The resume marker as a dict, ``None`` only on CONFIRMED absence.

    Everything else refuses: an empty file, undecodable bytes (the generic
    filesystem layer answers those with ``None``, indistinguishable from
    missing without an existence probe), wrong structure, a TypeError, any
    other read error — admission cannot tell "not claimed" from "cannot
    see the claim", so it must not guess."""
    try:
        text = await backend.aread_text(path)
    except FileNotFoundError:
        return None
    except Exception as error:
        raise ResumeRefused(
            f"resume marker read failed ({path}: {type(error).__name__}); "
            "refuse to resume"
        ) from error
    if text is None:
        # None is both "absent" and "unreadable" on the generic layer (empty
        # file, non-UTF-8 bytes). Only a workspace-relative existence probe
        # can tell them apart — a path-membership check against aglob output
        # cannot, because production aglob answers in ABSOLUTE paths.
        exists = await _probe_exists(backend, path)
        if exists:
            raise ResumeRefused(f"resume marker unreadable ({path}); refuse to resume")
        return None
    if text == "":
        raise ResumeRefused(f"resume marker unreadable ({path}); refuse to resume")
    try:
        doc = json.loads(text)
    except ValueError:
        raise ResumeRefused(f"resume marker unreadable ({path}); refuse to resume")
    if not isinstance(doc, dict):
        raise ResumeRefused(f"resume marker unreadable ({path}); refuse to resume")
    return doc


async def resolve_resume_source(
    backend: Any,
    registry: Any,
    *,
    source_dir: str,
    resume: str,
    script: str,
    run_dir_for: "Callable[[str], str] | None" = None,
    claimant: str | None = None,
) -> str:
    """Validate the run being resumed and return its artifact directory.

    ``claimant`` is the launching call's tool_call_id: an open run whose
    resume marker names the same claimant is this call's own earlier start
    (a checkpoint re-execution), not a competing resumer.

    Refuses (``ResumeRefused``) a run still open anywhere — the ledger is the
    cross-worker authority; the local registry is not — a run already being
    resumed by another live run (the first resume produced a NEW task id, so
    the open-rows check alone would miss it; the resume source is recorded in
    each replayed journal entry's ``resumed_from``, which is where this
    reads it from — no schema change), and one whose recorded script differs
    from the script about to run (replaying journal entries against prompts a
    different script never sent).
    """
    run_ledger = getattr(registry, "run_ledger", None)
    if run_ledger is None:
        # No ledger (CLI): this process holds every run, so the registry is
        # the whole truth — a pending task here is a live run here.
        for task in await registry.get_all_tasks():
            if task.task_id == resume and task.is_pending:
                raise ResumeRefused(
                    f"cannot resume {getattr(task, 'display_id', resume)}: it is "
                    "still running. Wait for it to finish or cancel it first."
                )
    else:
        try:
            rows = await run_ledger.list_open_workflow_runs()
        except Exception as error:
            logger.warning("Resume ledger read failed", exc_info=True)
            raise ResumeRefused(
                "could not read the workflow run ledger to verify the "
                "resumed run's state. Try again shortly."
            ) from error
        for row in rows:
            if str(row["task_id"]) == resume:
                raise ResumeRefused(
                    f"cannot resume run {resume}: it is still in progress "
                    "(possibly on another worker). Wait for it to reach a "
                    "terminal state, or cancel it first."
                )
        # The double-resume guard: an open run whose journal says it replays
        # from this source means someone is already resuming it.
        if run_dir_for is not None:
            for row in rows:
                other = str(row["task_id"])
                if other == resume:
                    continue
                entries = await journal_entries(backend, run_dir_for(other))
                if entries is None:
                    raise ResumeRefused(
                        f"could not list run {other}'s journal; refuse to resume. "
                        "Try again shortly."
                    )
                if not any(entry.get("resumed_from") == source_dir for entry in entries):
                    continue
                # Only a run this same call claimed (its marker names our
                # tool_call_id) is exempt; an unreadable marker is not ours.
                try:
                    ours = await _claimed_by(backend, run_dir_for(other), claimant)
                except ResumeRefused:
                    ours = False
                if not ours:
                    raise ResumeRefused(
                        f"cannot resume run {resume}: run {other} is "
                        "already resuming it. Wait for that run to finish "
                        "or cancel it first."
                    )

    # The resumed run's own journal must be listable: an unlistable source
    # would read as "no journal" and re-dispatch every step the dead run
    # already finished.
    if await journal_entries(backend, source_dir) is None:
        raise ResumeRefused(
            f"could not list the journal of run {resume} "
            f"({source_dir}); refuse to resume. Try again shortly."
        )

    old_script = None
    try:
        old_script = await backend.aread_text(f"{source_dir}/workflow.js")
    except Exception as error:
        logger.warning("Resume snapshot read failed", exc_info=True)
        raise ResumeRefused(
            f"could not read the run record for {resume} "
            f"({source_dir}/workflow.js). It cannot be resumed."
        ) from error
    if old_script != script:
        raise ResumeRefused(
            f"cannot resume run {resume}: its recorded workflow.js "
            "differs from the script being run now. Start a fresh run."
        )
    return source_dir


async def _claimed_by(backend: Any, other_dir: str, claimant: str | None) -> bool:
    """Whether ``other_dir``'s resume marker was written by ``claimant``.
    An unreadable marker raises (fail closed); a marker without a claimant
    (or no claimant to compare) is never "ours"."""
    if not claimant:
        return False
    try:
        marker = await _read_marker(backend, f"{other_dir}/resume.json")
    except ResumeRefused:
        raise ResumeRefused(
            "could not read the open runs' resume markers; refuse to "
            "resume. Try again shortly."
        )
    return isinstance(marker, dict) and marker.get("tool_call_id") == claimant


async def _open_run_ids(registry: Any) -> list[str] | None:
    """Open workflow run task ids, ledger first; None when they cannot be
    read (the caller refuses the resume)."""
    run_ledger = getattr(registry, "run_ledger", None)
    if run_ledger is None:
        try:
            return [
                task.task_id
                for task in await registry.get_all_tasks()
                if task.subagent_type == "workflow" and task.is_pending
            ]
        except Exception:
            logger.warning("resume open-run registry read failed", exc_info=True)
            return None
    try:
        rows = await run_ledger.list_open_workflow_runs()
    except Exception:
        logger.warning("resume open-run ledger read failed", exc_info=True)
        return None
    return [str(row["task_id"]) for row in rows]


async def claim_resume(
    backend: Any,
    registry: Any,
    *,
    source_dir: str,
    resume: str,
    marker_path: str,
    run_dir_for: "Callable[[str], str] | None" = None,
    claimant: str | None = None,
    task_id: str | None = None,
) -> None:
    """Atomically claim the resume: under one advisory transaction lock,
    verify no live run is already resuming this source, then write the new
    run's marker immediately — admission time, not first-replay time.

    The marker records the claimant (``tool_call_id``) and the new run's
    ``task_id``; an open run claimed by the same tool_call_id is this call's
    own earlier start, so it is not a conflict.

    Raises ``ResumeRefused`` for any refusal or any check that cannot run
    (fail closed: an unreadable ledger or an unwritable marker means the
    exclusivity cannot be guaranteed, so the resume does not start)."""
    lock_key = f"wf-resume:{resume}"

    async def _critical() -> None:
        open_ids = await _open_run_ids(registry)
        if open_ids is None:
            raise ResumeRefused(
                "could not read the open workflow runs to verify resume "
                "exclusivity. Try again shortly."
            )
        for other in open_ids:
            if other == resume:
                raise ResumeRefused(
                    f"cannot resume run {resume}: it is still in progress. "
                    "Wait for it to reach a terminal state, or cancel it first."
                )
            if run_dir_for is None:
                continue
            other_dir = run_dir_for(other)
            try:
                marker = await _read_marker(backend, f"{other_dir}/resume.json")
            except ResumeRefused:
                raise ResumeRefused(
                    "could not read the open runs' resume markers; refuse to "
                    "resume. Try again shortly."
                )
            if claimant and isinstance(marker, dict) and marker.get("tool_call_id") == claimant:
                continue
            if isinstance(marker, dict) and marker.get("resumed_from") == source_dir:
                raise ResumeRefused(
                    f"cannot resume run {resume}: run {other} is already "
                    "resuming it. Wait for that run to finish or cancel it first."
                )
            entries = await journal_entries(backend, other_dir)
            if entries is None:
                raise ResumeRefused(
                    f"could not list run {other}'s journal; refuse to resume. "
                    "Try again shortly."
                )
            for entry in entries:
                if entry.get("resumed_from") == source_dir:
                    raise ResumeRefused(
                        f"cannot resume run {resume}: run {other} is already "
                        "resuming it. Wait for that run to finish or cancel it first."
                    )
        try:
            written = await backend.awrite_text(
                marker_path,
                json.dumps(
                    {
                        "resumed_from": source_dir,
                        "claimed_at": time.time(),
                        "tool_call_id": claimant,
                        "task_id": task_id,
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception as error:
            raise ResumeRefused(
                "could not write the resume marker; the run cannot start. "
                f"({error})"
            ) from error
        if not written:
            raise ResumeRefused(
                "could not write the resume marker; the run cannot start."
            )

    try:
        from src.server.database.pool import get_db_connection
    except Exception:
        get_db_connection = None  # type: ignore[assignment]

    if get_db_connection is None:
        await _critical()
        return
    try:
        async with get_db_connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,)
                    )
                await _critical()
    except ResumeRefused:
        raise
    except Exception as error:
        logger.warning("resume claim failed", exc_info=True)
        raise ResumeRefused(
            "could not verify resume exclusivity (advisory lock / check "
            f"failed: {type(error).__name__}). Try again shortly."
        ) from error


async def worker_lost_failure(thread_id: str, task_id: str) -> dict[str, Any]:
    """The failure payload for a recovered worker_lost task run.

    A workflow run whose worker died is not lost work — its per-dispatch
    journal lets a follow-up ``RunWorkflow(resume=...)`` replay every
    completed dispatch — so the failure text says so, and it is the text the
    thread's report-back notification and TaskOutput carry. Task identity
    lives on ``subagent_tasks``; a read failure returns the ordinary wording.
    """
    subagent_type = ""
    try:
        from src.server.database.pool import get_db_connection

        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT subagent_type FROM subagent_tasks
                    WHERE thread_id = %s AND task_id = %s
                    """,
                    (thread_id, task_id),
                )
                row = await cur.fetchone()
                subagent_type = str(row[0]) if row and row[0] else ""
    except Exception:
        logger.debug("task type read failed for %s", task_id, exc_info=True)

    if subagent_type == "workflow":
        logger.warning(
            "[RecoveryScanner] workflow run (thread=%s task=%s) recovered as "
            "worker_lost; resumable with RunWorkflow(resume='%s')",
            thread_id,
            task_id,
            task_id,
        )
        return {
            "error": (
                "worker_lost: no live executor holds this task's namespace "
                "fence. The run was interrupted and is RESUMABLE — re-launch "
                f"it with RunWorkflow(workflow=..., resume='{task_id}') to "
                "replay completed dispatches from its journal."
            )
        }
    return {
        "error": (
            "worker_lost: no live executor holds this task's namespace fence"
        )
    }
