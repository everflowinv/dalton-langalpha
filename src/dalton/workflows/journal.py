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
# Reviewer dispatch labels across the shipped workflows (审稿 / 独立审稿 /
# `<TICKER> 审稿` / `复审 N`) — the pre-reviewDraft spelling of identifying a
# reviewer-class journal entry by label alone.
REVIEWER_LABEL_RE = re.compile(r"审稿|复审")
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


def declares_artifact_field(result: Any) -> bool:
    """Whether the result carries a ``files``/``file`` field at all.

    Distinguishes a child that never declares artifacts (a reviewer's
    verdict — its record verifies on label+prompt alone) from one that
    DECLARES the field yet wrote nothing (the timed-out branch's
    schema-valid ``{files: [], summary: "未落盘"}`` ok answer).
    """
    if not isinstance(result, dict):
        return False
    return "files" in result or "file" in result


def _is_live_state_query_result(result: Any) -> bool:
    """Return True for non-replayable live workspace probes (``{path, sha256}`` or ``{run_id}``)."""
    if not isinstance(result, dict):
        return False
    if "files" in result or "file" in result:
        return False
    return isinstance(result.get("path"), str) or "run_id" in result


def _is_model_ledger_artifact(path: str) -> bool:
    norm = path.strip().replace("\\", "/")
    return norm == "model_dump/model-verification.json" or norm.endswith(
        "/model_dump/model-verification.json"
    )


async def artifact_records(
    backend: Any, result: Any
) -> list[dict[str, Any]] | None:
    """``[{path, sha256}]`` for the artifacts a child declared it wrote, or
    ``None`` when the record must never be replayed.

    ``None`` is that verdict in both of its cases: a file the child says it
    wrote but whose digest cannot be computed (binary through a text-only
    backend, a file already moved), and a ``files``/``file`` field that
    parses to no path at all — the child reported writing nothing, and
    replaying it would keep that branch "successfully empty" through every
    resume, so the dispatch has to run again.
    """
    if _is_live_state_query_result(result):
        # A live state query (final-model resolver {path, sha256}, or run_id
        # probe {run_id}) reports current run/disk state; a resume must ask
        # again instead of replaying stale answers from the prior run.
        return None
    paths = declared_artifact_paths(result)
    if not paths:
        return None if declares_artifact_field(result) else []
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


def canonical_prompt_digest(prompt: str, run_id: str | None = None) -> str:
    """Hash ``prompt`` while normalizing this run's server-bound ``--run-id``
    flag so resumed runs match prior modeling dispatches across run_ids."""
    if isinstance(run_id, str) and run_id.strip():
        rid = run_id.strip()
        prompt = prompt.replace(f'--run-id "{rid}"', '--run-id "{{RUN_ID}}"')
    return prompt_digest(prompt)


async def _probe_declared_paths(backend: Any, paths: list[str]) -> dict[str, str]:
    """Each declared path's existence verdict: ``ok`` (exists, non-empty),
    ``missing``, ``empty``, or ``unknown`` — never guessed.

    One shell probe per batch when the backend can execute: ``test -s`` on
    the backend-normalized absolute path, which is the same spelling a Write
    of that declared path resolved to (a virtual leading-``/`` path, a
    ``./``-relative one and a ``../sibling`` workspace-relative one all land
    where the workflow's own file writes did; a path normalization refuses —
    a ``..`` crossing a store mount — is ``unknown``, not missing). Binaries
    answer as well as text: ``test -s`` never decodes. Backends that cannot
    execute fall back to text reads, where only ``FileNotFoundError`` says
    missing — every other failure (decode included) is ``unknown``, and the
    generic layer's ``None`` is both absent and unreadable, so it stays
    ``unknown`` too: an artifact is never called missing on evidence that
    cannot tell.
    """
    verdicts: dict[str, str] = {}
    resolved: list[tuple[str, str]] = []
    normalize = getattr(backend, "normalize_path", None)
    for path in paths:
        try:
            normalized = normalize(path) if normalize is not None else path
            # A normalizer that does not answer a string is not one; the
            # raw declared path is what the read fallback understands.
            absolute = normalized if isinstance(normalized, str) else path
        except Exception:
            verdicts[path] = "unknown"
            continue
        resolved.append((path, absolute))

    execute = getattr(backend, "aexecute", None)
    if execute is not None and resolved:
        script_parts = []
        for index, (_, absolute) in enumerate(resolved):
            script_parts.append(
                f"if test -s {shlex.quote(absolute)}; then echo {index} ok; "
                f"elif test -e {shlex.quote(absolute)}; then echo {index} empty; "
                f"else echo {index} missing; fi"
            )
        try:
            response = await execute("; ".join(script_parts))
            raw_output = getattr(response, "output", None)
            if not isinstance(raw_output, str):
                # An exec answer with nothing string-readable is no answer:
                # say unknown the honest way, through the read fallback.
                raise TypeError("exec returned no readable output")
            answered: dict[str, str] = {}
            for line in raw_output.splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2 and parts[1].strip() in ("ok", "empty", "missing"):
                    answered[parts[0].strip()] = parts[1].strip()
            # The probe echoes each path's index; a path whose line never
            # came back was not confirmed, which is ``unknown``, not missing.
            for index, (path, _) in enumerate(resolved):
                verdicts[path] = answered.get(str(index), "unknown")
            return verdicts
        except Exception:
            logger.debug("workflow artifact existence probe failed", exc_info=True)

    for path, absolute in resolved:
        try:
            text = await backend.aread_text(absolute)
        except FileNotFoundError:
            verdicts[path] = "missing"
            continue
        except Exception:
            verdicts[path] = "unknown"
            continue
        if text is None:
            verdicts[path] = "unknown"
        elif text == "":
            verdicts[path] = "empty"
        else:
            verdicts[path] = "ok"
    return verdicts


async def missing_declared_artifacts(backend: Any, result: Any) -> list[str]:
    """The declared artifacts a child's own ``files``/``file`` names that are
    not on disk (or hold nothing), as human-readable violation strings — the
    dispatch cannot count as done while any of them stands.

    Existence is probed, not hashed: a binary artifact (an .xlsx through a
    text-reading fallback) or an unhashable path is ``unknown`` and never
    counted missing — only a confirmed absence or a zero-byte file is a
    violation. A result that declares the field but parses to no path at all
    is the same verdict by another door: the child reported writing nothing
    (the schema-valid ``{files: [], summary: "write was refused at the time
    limit"}`` answer), which is exactly the "successfully empty" branch the
    replay guard already refuses. A result with no artifact field declares
    nothing and is not judged here.
    """
    if not isinstance(result, dict) or not declares_artifact_field(result):
        return []
    paths = declared_artifact_paths(result)
    if not paths:
        return ["声明了 files/file 但未写出任何文件"]
    verdicts = await _probe_declared_paths(backend, paths)
    violations: list[str] = []
    for path, verdict in verdicts.items():
        if verdict == "missing":
            violations.append(f"{path}（不存在）")
        elif verdict == "empty":
            violations.append(f"{path}（空文件）")
    return violations


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

    def prompt_hash(self, prompt: str) -> str:
        """Stable prompt digest for journal recording & resume matching."""
        return canonical_prompt_digest(prompt, self.run_id)

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

    @staticmethod
    def _match_receipt_chain(
        receipt: dict[str, Any],
        unresolved: list[dict[str, Any]],
        cm_path: str,
        current: dict[str, str | None],
    ) -> tuple[bool, str | None]:
        """Verify ``unresolved`` artifacts against a chained candidate transition
        receipt written before each legitimate publish (``write-snapshot`` and
        subsequent ``--in-place`` rework / recalc publishes).

        Returns ``(matched, migrated_model_sha256)``. External tampering whose
        digest does not match any connected post-publish state is rejected."""
        rec_model_name = str(receipt.get("model_name") or "").strip()
        raw_transitions = receipt.get("transitions")
        transitions: list[dict[str, Any]] = []
        if isinstance(raw_transitions, list) and raw_transitions:
            for item in raw_transitions:
                if isinstance(item, dict):
                    transitions.append(item)
        else:
            pre_m = receipt.get("pre_snapshot_model_sha256")
            post_m = receipt.get("post_snapshot_model_sha256")
            pre_cm = receipt.get("pre_snapshot_coord_map_sha256")
            post_cm = receipt.get("post_snapshot_coord_map_sha256")
            if pre_m and post_m:
                transitions.append(
                    {
                        "op": "write_snapshot",
                        "pre_model_sha256": pre_m,
                        "post_model_sha256": post_m,
                        "pre_coord_map_sha256": pre_cm,
                        "post_coord_map_sha256": post_cm,
                    }
                )
        if not transitions:
            return False, None

        def _reachable_states(
            start_sha: str | None,
            pre_key: str,
            post_key: str,
        ) -> set[tuple[int, str]]:
            """Return ``{(step_idx, sha)}`` reachable from ``start_sha`` along
            contiguous receipt transitions."""
            if not isinstance(start_sha, str) or not start_sha:
                return set()
            reached: set[tuple[int, str]] = set()
            frontier: set[str] = {start_sha}
            for idx, tr in enumerate(transitions):
                pre_v = tr.get(pre_key)
                post_v = tr.get(post_key)
                if isinstance(pre_v, str) and pre_v in frontier:
                    # Allow half-commit state (pre_v still on disk for one file
                    # while the paired file already swapped to post_v).
                    reached.add((idx, pre_v))
                    if isinstance(post_v, str) and post_v:
                        reached.add((idx, post_v))
                        frontier.add(post_v)
            return reached

        model_entry: dict[str, Any] | None = None
        cm_entry: dict[str, Any] | None = None
        for a in unresolved:
            path = str(a.get("path") or "")
            if path == cm_path:
                cm_entry = a
            elif path.lower().endswith(".xlsx"):
                base_name = path.replace("\\", "/").rsplit("/", 1)[-1]
                if rec_model_name and base_name != rec_model_name:
                    return False, None
                model_entry = a
            else:
                return False, None

        matched_steps: set[int] | None = None
        migrated_model_sha: str | None = None

        if model_entry is not None:
            old_m = model_entry.get("sha256")
            cur_m = current.get(model_entry["path"])
            m_states = _reachable_states(old_m, "pre_model_sha256", "post_model_sha256")
            m_steps = {idx for idx, sha in m_states if sha == cur_m and cur_m != old_m}
            if not m_steps:
                return False, None
            matched_steps = m_steps
            migrated_model_sha = cur_m

        if cm_entry is not None:
            old_cm = cm_entry.get("sha256")
            cur_cm = current.get(cm_entry["path"])
            cm_states = _reachable_states(
                old_cm, "pre_coord_map_sha256", "post_coord_map_sha256"
            )
            cm_steps = {idx for idx, sha in cm_states if sha == cur_cm}
            if not cm_steps:
                return False, None
            if matched_steps is not None and not (matched_steps & cm_steps):
                # Also permit coord_map reaching a step at or before the model step
                # when model swapped atomically just before coord_map finished.
                if not any(cs <= ms for cs in cm_steps for ms in matched_steps):
                    return False, None

        return True, migrated_model_sha

    async def _artifacts_match_or_superseded(
        self,
        entry: dict[str, Any],
        artifacts: list[dict[str, Any]],
        current: dict[str, str | None],
        index: dict[str, list[dict[str, Any]]],
    ) -> tuple[bool, str | None]:
        """Verify each recorded artifact matches disk directly, OR was updated
        in-place by a later completed dispatch in the same source run, OR
        matches a verified ``consensus-snapshot-receipt.json`` pre->post
        transition chain written by ``model_consensus.py write-snapshot`` /
        ``model_edit.py apply --in-place``.

        Returns ``(matched, migrated_model_sha256)``."""
        mismatched = [a for a in artifacts if current.get(a["path"]) != a.get("sha256")]
        if not mismatched:
            return True, None
        if any(current.get(a["path"]) is None for a in mismatched):
            return False, None

        entry_seq = entry.get("seq")
        unresolved: list[dict[str, Any]] = []
        for a in mismatched:
            path = a["path"]
            cur_sha = current.get(path)
            superseded = False
            for entries in index.values():
                for later in entries:
                    if later is entry or later.get("status") != "ok":
                        continue
                    later_seq = later.get("seq")
                    if (
                        isinstance(entry_seq, int)
                        and isinstance(later_seq, int)
                        and later_seq <= entry_seq
                    ):
                        continue
                    for la in later.get("artifacts") or ():
                        if la.get("path") == path and la.get("sha256") == cur_sha:
                            superseded = True
                            break
                    if superseded:
                        break
                if superseded:
                    break
            if not superseded:
                unresolved.append(a)

        if not unresolved:
            return True, None

        # Check verified consensus-snapshot-receipt.json transition chain (handles
        # interruption after write-snapshot or subsequent in-place rework/recalc,
        # before the consensus stage finished and wrote an ok journal entry).
        cm_path: str | None = None
        for a in artifacts:
            p = str(a.get("path") or "").replace("\\", "/")
            if p == "coord_map.json" or p.endswith("/coord_map.json"):
                cm_path = a["path"]
                break
        if cm_path is None:
            return False, None
        cm_norm = cm_path.replace("\\", "/")
        receipt_rel = (
            f"{cm_norm.rsplit('/', 1)[0]}/consensus-snapshot-receipt.json"
            if "/" in cm_norm
            else "consensus-snapshot-receipt.json"
        )
        try:
            receipt = await _read_json(self.backend, receipt_rel)
        except Exception:
            return False, None
        if not isinstance(receipt, dict):
            return False, None

        return self._match_receipt_chain(receipt, unresolved, cm_path, current)

    async def find_replayable(
        self, label: str, prompt: str, *, subagent_type: str | None = None
    ) -> dict[str, Any] | None:
        """The journal entry a resume may replay, or None to dispatch.

        A replay demands more than label+prompt: the entry must have been
        recorded replayable (every declared artifact hashed), and every
        recorded artifact must still hash the same — a file someone touched
        since forces the dispatch again rather than resurrecting stale work.
        """
        index = await self._load_resume_index()
        expected_digests = {self.prompt_hash(prompt), prompt_digest(prompt)}
        for entry in index.get(label) or []:
            if (
                entry.get("prompt_sha256") not in expected_digests
                or entry.get("status") != "ok"
                or entry.get("replayable") is not True
            ):
                continue
            # Reviewer-class entries (a reviewDraft binding, or the label a
            # pre-reviewDraft reviewer dispatch carried) are re-validated
            # against the reviewer contract BEFORE reuse (round 4 #2): a
            # reply recorded before the contract existed — missing items,
            # stringly check numbers, a model-echoed hash standing in for the
            # binding — is invalid and re-dispatches the review instead of
            # replaying it.
            result = entry.get("result")
            # The current dispatch's own type is authoritative (a custom
            # workflow may label its reviewer "review"); the label pattern only
            # covers records written before the type was passed.
            if (
                subagent_type == "dalton-reviewer"
                or entry.get("review_draft")
                or REVIEWER_LABEL_RE.search(str(entry.get("label") or ""))
            ):
                from src.dalton.publish.review import reviewer_reply_error

                if reviewer_reply_error(result) is not None:
                    continue
            # The same empty-artifact & live-state-query guards as ``artifact_records``,
            # applied at read time so journals written before those guards never replay
            # stale live queries (such as old ``run_id`` readings).
            result = entry.get("result")
            if _is_live_state_query_result(result):
                continue
            if declares_artifact_field(result) and not declared_artifact_paths(
                result
            ):
                continue
            artifacts = entry.get("artifacts") or []
            migrated_model_sha: str | None = None
            current_digests: dict[str, str | None] | None = None
            if artifacts:
                current_digests = await digest_paths(
                    self.backend, [a["path"] for a in artifacts]
                )
                matched, migrated_model_sha = await self._artifacts_match_or_superseded(
                    entry, artifacts, current_digests, index
                )
                if not matched:
                    continue
                # A terminal model verification ledger is bound to a specific run_id:
                # never replay a prior run's ledger entry on resume so the stage re-runs
                # and re-binds model-verification.json to THIS run's run_id.
                stale_ledger = False
                for a in artifacts:
                    if _is_model_ledger_artifact(str(a.get("path") or "")):
                        try:
                            ledger_doc = await _read_json(self.backend, a["path"])
                        except Exception:
                            ledger_doc = None
                        if not isinstance(ledger_doc, dict) or ledger_doc.get("run_id") != self.run_id:
                            stale_ledger = True
                            break
                if stale_ledger:
                    continue
            # A reviewer dispatch names the draft it reviewed (reviewDraft):
            # replaying it against a CHANGED draft would resurrect a review of
            # something that is no longer there — re-dispatch instead. A record
            # whose binding hash failed to compute (draft_sha256 None) never
            # replays either (prompt review batch 1 round 3 #5).
            review_draft = entry.get("review_draft")
            if review_draft:
                recorded_sha = entry.get("draft_sha256")
                current_sha = (
                    await digest_paths(self.backend, [str(review_draft)])
                ).get(str(review_draft))
                if not isinstance(recorded_sha, str) or current_sha != recorded_sha:
                    continue
            updated_result = result
            if isinstance(updated_result, dict):
                if "run_id" in updated_result and updated_result.get("run_id") not in (None, self.run_id):
                    updated_result = {**updated_result, "run_id": self.run_id}
                if migrated_model_sha and "model_sha256" in updated_result:
                    updated_result = {**updated_result, "model_sha256": migrated_model_sha}
            if updated_result is not result or (migrated_model_sha and current_digests is not None):
                updated_artifacts = (
                    [
                        {**a, "sha256": current_digests.get(a["path"]) or a.get("sha256")}
                        for a in artifacts
                    ]
                    if migrated_model_sha and current_digests is not None
                    else artifacts
                )
                return {**entry, "result": updated_result, "artifacts": updated_artifacts}
            return entry
        return None

    async def record_replay(
        self, entry: dict[str, Any], *, seq: int, label: str, phase: str | None
    ) -> None:
        """Journal a replayed dispatch into this run, marked with its origin."""
        replayed_result = entry.get("result")
        if (
            isinstance(replayed_result, dict)
            and "run_id" in replayed_result
            and replayed_result.get("run_id") not in (None, self.run_id)
        ):
            replayed_result = {**replayed_result, "run_id": self.run_id}
        await self.append(
            {
                "seq": seq,
                "label": label,
                "phase": phase,
                "prompt_sha256": entry["prompt_sha256"],
                "status": "ok",
                "result": replayed_result,
                "result_kind": entry.get("result_kind", "parsed"),
                "artifacts": entry.get("artifacts") or [],
                # The caller-computed draft hash of a reviewer dispatch rides
                # the replay too: a resumed run's journal keeps the binding
                # dalton_check_done verifies (prompt review batch 1 round 2 #1).
                "draft_sha256": entry.get("draft_sha256"),
                "review_draft": entry.get("review_draft"),
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
                "run_dir": self.base_rel,
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


def workflow_run_script_snapshot(thread_id: str, task_id: str) -> str:
    """The run's own ``workflow.js`` snapshot, workspace-relative.

    Every launch snapshots the exact script it runs into the run directory,
    so this file is the byte-exact source an inline run's resume must re-pass.
    """
    from ptc_agent.core.paths import WorkspaceLayout

    return "/".join(
        (
            WorkspaceLayout.thread_subdir(str(thread_id)[:8], "workflows", task_id),
            "workflow.js",
        )
    )


def workflow_resume_call(task_id: str, thread_id: str | None = None) -> str:
    """The RunWorkflow call that resumes an interrupted run, as one phrase.

    A run is resumed with the SAME source it was launched with plus
    ``resume`` — the journal verifies the script against the run's own
    snapshot, so a named run re-passes its name and an inline run re-passes
    the script verbatim. An inline script has no name to pass: the first
    recovery attempt of an interrupted inline run once read
    ``workflow=<name>`` here, answered ``Unknown workflow`` (the name is in
    neither the saved nor the prebuilt lists), and lost the run. With a real
    thread id the phrase also names the snapshot the script must come from
    (anything else — None, a mock — simply omits it).
    """
    snapshot_note = ""
    if isinstance(thread_id, str) and thread_id.strip():
        snapshot_note = (
            " — the run's exact script is snapshotted at "
            f"`{workflow_run_script_snapshot(thread_id, task_id)}`, re-pass it verbatim"
        )
    return (
        f"RunWorkflow(workflow=<same name>, params=<same as before>, "
        f"resume='{task_id}') if it was launched by name, or "
        f"RunWorkflow(script=<the same script verbatim>, params=<same as "
        f"before>, resume='{task_id}') if it was launched with an inline "
        f"script (an inline script has no workflow name to pass){snapshot_note}"
    )


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
                f"it with the SAME source it was started with plus resume='{task_id}' "
                f"({workflow_resume_call(task_id, thread_id)}) to replay "
                "completed dispatches from its journal. When continuing (or "
                "when the user asks to continue / '继续'), you MUST call "
                "RunWorkflow with that same source and MUST NOT manually "
                "re-dispatch Task subagents (such as data-prep or "
                "dalton-evidence)."
            )
        }
    return {
        "error": (
            "worker_lost: no live executor holds this task's namespace fence"
        )
    }
