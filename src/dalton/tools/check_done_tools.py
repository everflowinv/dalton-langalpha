"""Direct LangChain tool `dalton_check_done`: verify a workflow/skill's completion contract, read-only (workflow completion contracts).

The tool re-checks what the run claimed to produce — files, markers, sizes,
mtimes, validator scripts, knowledge versions, verdicts, the check_draft
sidecar hash and THIS run's review pass — against the contract on disk. It
never writes: fixing the gaps is the main agent's job.

Fail-closed throughout: anything that cannot be verified (a file the backend
cannot read, an mtime the sandbox will not report, a dead database query, a
glob the environment cannot run) is a ``missing`` entry saying 无法核对，
请重试 — never a warning, never a pass. Only checks whose SKILL marks the
step optional degrade to warnings, and every optional failure mode does.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any

import structlog
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from src.dalton.tools.knowledge_tools import _extract_run_identity
from src.dalton.workflows.contracts import (
    ContractError,
    load_contract,
    missing_params,
    render_command,
    render_template,
)
from src.server.database.pool import get_db_connection
from src.tools.secretary._commands import InjectedToolCallId

logger = structlog.get_logger(__name__)

# `render_audit_md` writes the draft hash as ``- **草稿哈希**：`<16 hex>…```.
_DRAFT_HASH_RE = re.compile(r"草稿哈希[^\n]*?`([0-9a-f]{8,64})")
# A review journal record's echoed draft hash is the full 64-hex digest.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Reviewer dispatch labels across the shipped workflows (审稿 / 独立审稿 /
# `<TICKER> 审稿` / `复审 N`).
_REVIEW_LABEL_RE = re.compile("审稿|复审")

_VALIDATOR_OUTPUT_CHARS = 300

_RUN_STATUS_GLOB = ".agents/threads/*/workflows/*/status.json"


def _thread_prefix(thread_id: str | None) -> str:
    """The journal/status glob prefix narrowed to ONE thread when its id is
    known (run dirs live under the 8-char short id, `agent.py`'s spelling)."""
    if thread_id:
        return f".agents/threads/{str(thread_id)[:8]}/workflows/"
    return ".agents/threads/*/workflows/"


class _CannotVerify(Exception):
    """A check could not run — surfaces as ``missing`` with 无法核对，请重试."""


@dataclass
class _FileInfo:
    exists: bool
    size: int
    text: str | None
    mtime_epoch: int | None = None


def _today_start_utc() -> datetime:
    """The one ``since`` this tool uses: this execution's day, UTC midnight.

    The report's ``as_of``/``release_date`` anchor paths only — a "latest"
    anchor is not a date at all, and a knowledge version written yesterday
    against today's check would be a silent pass."""
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)


async def _read_text(backend: Any, path: str) -> str | None:
    try:
        return await backend.aread_text(path)
    except Exception:
        return None


async def _read_text_diagnosed(backend: Any, path: str) -> tuple[str, str | None]:
    """("ok", text) | ("missing", None) | ("error", None) — a read failure on
    a file the sandbox says exists is a transport error, not an absent file,
    and the two must not share an outcome."""
    text = None
    transport_error = False
    try:
        text = await backend.aread_text(path)
    except Exception:
        transport_error = True
    if text is not None:
        return "ok", text
    exists = await _run_command(backend, f"test -e {shlex.quote(path)}")
    if exists is not None and exists[0] == 0:
        return "error", None  # present but unreadable
    if exists is not None and exists[0] != 0:
        return "missing", None
    if transport_error:
        return "error", None
    return "missing", None


def _workspace_cd(backend: Any) -> str:
    """``cd <workspace> && `` prefix for sandbox commands.

    ``SandboxBackend.aexecute`` runs at the computer root while relative
    file paths resolve against the workspace folder — without this prefix
    every wc/stat/test/validator lands in the wrong directory and binary
    artifacts read as missing."""
    workspace_dir = getattr(backend, "workspace_dir", None)
    if workspace_dir:
        return f"cd {shlex.quote(str(workspace_dir))} && "
    return ""


async def _run_command(backend: Any, command: str) -> tuple[int, str] | None:
    """Run one command in the workspace sandbox; None when this backend
    cannot execute at all (a required check then fails closed)."""
    execute = getattr(backend, "aexecute", None)
    if execute is None:
        return None
    command = _workspace_cd(backend) + command
    try:
        response = await execute(command)
        return int(getattr(response, "exit_code", 1)), str(
            getattr(response, "output", "")
        )
    except Exception as error:
        logger.warning(
            "dalton_check_done command failed", command=command, error=str(error)
        )
        return 1, str(error)


async def _file_info(backend: Any, path: str) -> _FileInfo:
    """Existence + byte size (+ mtime) for one path. Probes prefer the
    sandbox shell because models and contracts are binary (.xlsx); a probe
    the sandbox cannot answer leaves the field unknown, and the caller fails
    closed on whatever it needed."""
    result = await _run_command(backend, f"wc -c < {shlex.quote(path)}")
    mtime: int | None = None
    stat = await _run_command(backend, f"stat -c %Y {shlex.quote(path)}")
    if stat is not None and stat[0] == 0:
        try:
            mtime = int(stat[1].strip().splitlines()[-1])
        except (ValueError, IndexError):
            mtime = None
    if result is not None and result[0] == 0:
        try:
            size = int(result[1].strip().splitlines()[-1])
            return _FileInfo(True, size, None, mtime)
        except (ValueError, IndexError):
            pass
    status, text = await _read_text_diagnosed(backend, path)
    if status != "ok" or text is None:
        return _FileInfo(False, 0, None, mtime)
    return _FileInfo(True, len(text.encode("utf-8")), text, mtime)


async def _glob(backend: Any, pattern: str) -> list[str] | None:
    glob_paths = getattr(backend, "aglob_paths", None)
    if glob_paths is None:
        return None
    try:
        return [str(p) for p in await glob_paths(pattern, ".")]
    except Exception:
        return None


def _version_of(path: str) -> int | None:
    """The trailing ``-vN`` version of a model-style path, else None."""
    match = re.search(r"-v(\d+)(?:\.[^.]+)?$", path)
    return int(match.group(1)) if match else None


def _optional(artifact: dict[str, Any]) -> bool:
    return bool(artifact.get("optional"))


async def _derived_artifact_paths(
    backend: Any, artifact: dict[str, Any], params: dict[str, Any]
) -> tuple[list[str] | None, list[str]]:
    """(paths, problems): expand a ``derive`` artifact into one concrete path
    per JSON entry. Problems are missing entries — a ticker row without its
    field is a gap, not a skip."""
    derive = artifact["derive"]
    doc_path = render_template(derive["json"], params)
    status, text = await _read_text_diagnosed(backend, doc_path)
    problems: list[str] = []
    if status == "error":
        return None, [f"清单文件读取出错，无法核对（{doc_path}），请重试。"]
    if status == "missing" or text is None:
        return None, [f"产物 {artifact['path']} 的清单文件缺失（{doc_path}），无法逐项核对。"]
    try:
        doc = json.loads(text)
    except ValueError:
        return None, [f"产物 {artifact['path']} 的清单不可解析（{doc_path}），请重试。"]
    items = doc.get(derive["pointer"]) if isinstance(doc, dict) else None
    if not isinstance(items, list):
        return None, [f"产物 {artifact['path']} 的清单缺少列表 {derive['pointer']}，请重试。"]
    field = derive["field"]
    paths: list[str] = []
    for index, item in enumerate(items):
        value = item.get(field) if isinstance(item, dict) else None
        if value is None:
            problems.append(
                f"产物 {artifact['path']}：清单第 {index + 1} 项缺少 {field}，无法核对。"
            )
            continue
        paths.append(render_template(artifact["path"], {**params, field: str(value)}))
    return paths, problems


async def _check_artifact(
    backend: Any,
    artifact: dict[str, Any],
    params: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """(missing, warnings) for one declared artifact. Everything required
    that cannot be verified lands in missing; only SKILL-optional artifacts
    degrade their failures to warnings."""
    missing: list[str] = []
    warnings: list[str] = []
    name = artifact["path"]

    def _report(message: str) -> None:
        (warnings if _optional(artifact) else missing).append(message)

    if artifact.get("requires_param") and not params.get(artifact["requires_param"]):
        return missing, warnings

    concrete: list[str] | None
    problems: list[str] = []
    if artifact.get("derive"):
        concrete, problems = await _derived_artifact_paths(backend, artifact, params)
        for problem in problems:
            _report(problem)
        if concrete is None or not concrete:
            return missing, warnings
    else:
        concrete = [render_template(artifact["path"], params)]

    if artifact.get("glob"):
        matches = await _glob(backend, concrete[0])
        if matches is None:
            _report(f"当前环境无法执行 glob（{name}），无法核对，请重试。")
            return missing, warnings
        need = artifact.get("min_count", 1)
        if len(matches) < need:
            _report(
                f"产物 {name} 需要 ≥{need} 个匹配，实际 {len(matches)} 个；"
                "下一步：补齐缺失的支线/清单文件。"
            )
            return missing, warnings
        targets = matches
        versions = [_version_of(path) for path in matches]
        if all(v is not None for v in versions):
            # One final model: the max-VERSION match is the file every check
            # (mtime, size) speaks about — never an mtime-picked one.
            targets = [matches[versions.index(max(versions))]]
        elif artifact.get("modified_on"):
            _report(f"无法从匹配名解析版本号以确定最终文件（{name}），无法核对，请重试。")
            return missing, warnings
    else:
        targets = concrete

    day_floor: int | None = None
    modified_on = artifact.get("modified_on")
    if modified_on == "today":
        # "today" is the EXECUTION day; a release_date among the params must
        # not widen the freshness window backwards.
        day_floor = int(_today_start_utc().timestamp())
    elif modified_on == "as_of":
        anchor = params.get("as_of") or params.get("release_date") or params.get("end")
        day = str(anchor) if anchor else None
        try:
            base = (
                datetime.fromisoformat(f"{day}T00:00:00+00:00")
                if day
                else _today_start_utc()
            )
            day_floor = int(base.timestamp())
        except ValueError:
            day_floor = int(_today_start_utc().timestamp())
    elif modified_on is not None:
        missing.append(f"契约字段 modified_on 非法（{name}：{modified_on!r}），请修契约。")
        return missing, warnings

    for path in targets:
        info = await _file_info(backend, path)
        if not info.exists:
            _report(f"产物文件不存在：{path}。下一步：补齐该阶段交付物后重查。")
            continue
        if artifact.get("min_bytes") and info.size < artifact["min_bytes"]:
            _report(
                f"产物文件过小（{path}，{info.size} 字节 < {artifact['min_bytes']}），"
                "疑似占位。下一步：写满该交付物。"
            )
        if day_floor is not None:
            if info.mtime_epoch is None:
                _report(f"无法读取文件时间（{path}），本轮写入未核对，请重试。")
            elif info.mtime_epoch < day_floor:
                _report(
                    f"产物文件不是本轮写的（{path}，mtime 早于 {modified_on} 锚定日）；"
                    "下一步：重跑该步骤生成本轮文件。"
                )
        marker = artifact.get("end_marker")
        if marker:
            text = info.text if info.text is not None else await _read_text(backend, path)
            if text is None or marker not in text:
                _report(
                    f"产物文件缺少完成标记 {marker}：{path}。下一步：补齐内容并写上标记。"
                )
    return missing, warnings


async def _check_knowledge_versions(
    workspace_id: str, docs: list[str], since: datetime
) -> dict[str, bool]:
    """doc → whether a ``source='tool'`` version exists at/after ``since``.

    Raises on a query failure — the caller reports 无法核对 rather than
    letting a dead database read as "no versions"."""
    found: dict[str, bool] = {doc: False for doc in docs}
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT DISTINCT doc_key
                FROM dalton_knowledge_versions
                WHERE workspace_id = %s
                  AND doc_key = ANY(%s)
                  AND source = 'tool'
                  AND created_at >= %s
                """,
                (workspace_id, docs, since),
            )
            for row in await cur.fetchall():
                found[str(row[0])] = True
    return found


def _verdict_row_matches(
    row: dict[str, Any],
    kinds: list[str] | None,
    triggers: list[str] | None,
) -> bool:
    """Filter against the real ``dalton_verdicts`` columns: ``word`` is the
    five-word verdict, ``trigger_kind`` the trigger class."""
    word = row.get("word")
    if kinds and word not in kinds:
        return False
    if triggers and row.get("trigger_kind") not in triggers:
        return False
    return True


async def _check_verdicts(
    workspace_id: str,
    *,
    kinds: list[str] | None = None,
    triggers: list[str] | None = None,
    since: datetime,
) -> list[dict[str, Any]]:
    from src.dalton.database.verdicts import list_verdicts

    rows = await list_verdicts(workspace_id, limit=200, since=since)
    return [row for row in rows if _verdict_row_matches(row, kinds, triggers)]


def _as_trigger_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


async def _thread_run_dirs(backend: Any, thread_id: str | None) -> tuple[set[str], set[str]]:
    """``(all run dirs, finished run dirs)`` on this thread.

    A run is any directory holding its launch snapshot (workflow.js), a
    journal or a status.json: the snapshot write can fail without stopping
    the run, and status.json only appears when it finalizes, so no single
    file proves a run's presence. Any scan failure raises ``_CannotVerify``."""
    prefix = _thread_prefix(thread_id)
    found: set[str] = set()
    finished: set[str] = set()
    for pattern, suffix in (
        ("*/workflow.js", "/workflow.js"),
        ("*/journal/*.json", None),
        ("*/status.json", "/status.json"),
    ):
        matches = await _glob(backend, f"{prefix}{pattern}")
        if matches is None:
            raise _CannotVerify(f"无法扫描本线程的运行目录（{pattern}）")
        for path in matches:
            if suffix is None:
                run_dir = path.split("/journal/", 1)[0]
            else:
                run_dir = path.rsplit(suffix, 1)[0]
            found.add(run_dir)
            if suffix == "/status.json":
                finished.add(run_dir)
    return found, finished


async def _latest_run_dir(
    backend: Any, workflow: str, thread_id: str | None = None
) -> str | None:
    """The newest run directory of ``workflow`` on this workspace (status.json
    carries name + updated_at), or None when nothing can be established."""
    matches = await _glob(backend, f"{_thread_prefix(thread_id)}*/status.json")
    if matches is None:
        raise _CannotVerify("无法扫描运行目录（status.json）")
    best_dir: str | None = None
    best_stamp = -1.0
    for path in matches:
        # An unreadable status.json may be the newest run's; skipping it would
        # fall back to an older pass, so it blocks the check instead.
        text = await _read_text(backend, path)
        try:
            doc = json.loads(text) if text else None
        except ValueError:
            doc = None
        if not isinstance(doc, dict):
            raise _CannotVerify(f"运行状态文件无法读取或已损坏：{path}，请传 run_id")
        if doc.get("name") != workflow:
            continue
        stamp = doc.get("updated_at")
        if isinstance(stamp, (int, float)) and stamp > best_stamp:
            best_stamp = float(stamp)
            best_dir = path.rsplit("/status.json", 1)[0]
    return best_dir


async def _find_run_review_pass(
    backend: Any,
    workflow: str,
    run_id: str | None,
    draft_path: str | None,
    thread_id: str | None = None,
) -> tuple[bool, str]:
    """(passed, reason): the LAST review record of THIS run must be a pass,
    and its recorded draft hash must equal the CURRENT draft's hash —
    computed here from the file, never taken from the model's word.

    ``run_id`` names the run; without it the newest run directory of the
    same workflow stands in. Any scan failure raises ``_CannotVerify``."""
    if not run_id and not thread_id:
        return False, "缺少线程身份（thread_id 与 run_id 均未提供），无法定位本次 run"
    prefix = _thread_prefix(thread_id)
    if run_id:
        journals = await _glob(backend, f"{prefix}{run_id}/journal/*.json")
        if journals is None:
            raise _CannotVerify("无法扫描本次 run 的运行日志（journal）")
    else:
        all_dirs, finished = await _thread_run_dirs(backend, thread_id)
        if all_dirs - finished:
            # An unfinished run may be the current one; falling back to an
            # older finished run could accept a pass the new run has revised.
            return False, "本线程有未结束的 workflow run，无法确定本次 run；请传 run_id（.progress.json 里的 run_id）"
        run_dir = await _latest_run_dir(backend, workflow, thread_id)
        if run_dir is None:
            return False, "找不到本次 workflow 的 run 目录（无 run_id 且无 status.json），无法核对审稿"
        journals = await _glob(backend, f"{run_dir}/journal/*.json")
        if journals is None:
            raise _CannotVerify("无法扫描运行日志（journal）")

    # Collect EVERY review-labeled record — including failed ones — then
    # judge only the last: a re-review that errored after an earlier pass is
    # the run's final word on the draft.
    records: list[tuple[float, int, dict[str, Any]]] = []
    for path in journals:
        status, text = await _read_text_diagnosed(backend, path)
        if status == "error":
            raise _CannotVerify(f"运行日志读取失败（{path}），请重试")
        if status == "missing" or text is None:
            raise _CannotVerify(f"运行日志消失（{path}），请重试")
        try:
            doc = json.loads(text)
        except ValueError:
            raise _CannotVerify(f"运行日志不可解析（{path}），请重试")
        if not isinstance(doc, dict):
            raise _CannotVerify(f"运行日志结构异常（{path}），请重试")
        for entry in doc.get("dispatches") or []:
            if not isinstance(entry, dict):
                continue
            label = str(entry.get("label") or "")
            if not _REVIEW_LABEL_RE.search(label):
                continue
            records.append(
                (float(entry.get("recorded_at") or 0), int(entry.get("seq") or 0), entry)
            )
    if not records:
        return False, "本次 run 的运行日志里没有任何审稿/复审记录"
    records.sort(key=lambda item: (item[0], item[1]))
    last = records[-1][2]
    if last.get("status") != "ok":
        return False, f"最后一次审稿派发未成功（status={last.get('status')!r}），需重跑"
    result = last.get("result")
    verdict = result.get("verdict") if isinstance(result, dict) else None
    if verdict != "pass":
        summary = result.get("summary") if isinstance(result, dict) else None
        return False, f"本次 run 最后一条审稿结论是 {verdict!r}，不是 pass{('：' + str(summary)) if summary else ''}"

    if draft_path:
        status, text = await _read_text_diagnosed(backend, draft_path)
        if status != "ok" or text is None:
            raise _CannotVerify(f"被审草稿读取失败（{draft_path}），无法核对审稿哈希")
        current = hashlib.sha256(text.encode("utf-8")).hexdigest()
        recorded = result.get("draft_sha256") if isinstance(result, dict) else None
        if not isinstance(recorded, str) or not _SHA256_RE.match(recorded):
            return False, "审稿记录里没有 64 位十六进制的被审草稿哈希（draft_sha256）"
        if recorded != current:
            return False, (
                f"审稿针对的是旧草稿：记录哈希 {recorded[:12]}…，当前 {draft_path} "
                f"哈希 {current[:12]}…，需重新审稿"
            )
    return True, ""


def _is_manual(params: dict[str, Any]) -> bool:
    value = params.get("manual")
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return value is True


async def _find_manual_review_pass(
    backend: Any, sidecar: str, draft_path: str | None
) -> tuple[bool, str]:
    """(passed, reason) for the manual path: the reviewer JSON the analyst
    saved at ``sidecar`` — one object or a list (the last entry is the final
    word) — must be a pass whose ``draft_sha256`` equals the CURRENT draft's
    hash, computed here, exactly as the journal check demands."""
    status, text = await _read_text_diagnosed(backend, sidecar)
    if status == "error":
        raise _CannotVerify(f"手动审稿记录读取失败（{sidecar}），请重试")
    if status == "missing" or text is None:
        return False, (
            f"手动降级路径没有审稿记录 {sidecar}；下一步：派 dalton-reviewer 审最终稿，"
            "把它返回的 JSON（含 verdict 与 draft_sha256）原样保存到该路径"
        )
    try:
        doc = json.loads(text)
    except ValueError:
        return False, f"手动审稿记录不可解析（{sidecar}），需重新保存审稿 JSON"
    if isinstance(doc, list):
        doc = doc[-1] if doc else None
    if not isinstance(doc, dict):
        return False, f"手动审稿记录结构异常（{sidecar}），需为审稿 JSON 对象"
    verdict = doc.get("verdict")
    if verdict != "pass":
        summary = doc.get("summary")
        return False, f"手动审稿记录的最后结论是 {verdict!r}，不是 pass{('：' + str(summary)) if summary else ''}"
    if not draft_path:
        return False, "契约未声明被审草稿，无法核对手动审稿哈希"
    status, draft_text = await _read_text_diagnosed(backend, draft_path)
    if status != "ok" or draft_text is None:
        raise _CannotVerify(f"被审草稿读取失败（{draft_path}），无法核对审稿哈希")
    current = hashlib.sha256(draft_text.encode("utf-8")).hexdigest()
    recorded = doc.get("draft_sha256")
    if not isinstance(recorded, str) or not _SHA256_RE.match(recorded):
        return False, f"手动审稿记录 {sidecar} 里没有 64 位十六进制的被审草稿哈希（draft_sha256）"
    if recorded != current:
        return False, (
            f"手动审稿针对的是旧草稿：记录哈希 {recorded[:12]}…，当前 {draft_path} "
            f"哈希 {current[:12]}…，需重新审稿"
        )
    return True, ""


async def _review_pass(
    backend: Any,
    contract: dict[str, Any],
    review: dict[str, Any],
    params: dict[str, Any],
    run_id: str | None,
    draft_path: str | None,
    thread_id: str | None,
) -> tuple[bool, str]:
    """Route the review check: the run journal whenever a workflow run exists
    for this thread (or run_id names one) — even with ``manual`` set — and
    the manual sidecar only when ``manual`` is set AND no run exists."""
    if _is_manual(params) and not run_id:
        if not thread_id:
            return False, "缺少线程身份（thread_id 与 run_id 均未提供），无法确认本线程没有 workflow run"
        # "No run" must be proven, not inferred from a missing status.json: a
        # run writes workflow.js at launch and status.json only when it
        # finalizes, so an in-flight or crashed run has the former only. Any
        # run directory on this thread routes to the journal.
        try:
            all_dirs, _finished = await _thread_run_dirs(backend, thread_id)
        except _CannotVerify:
            return False, "无法扫描本线程的 workflow 运行目录，手动审稿记录不予采信"
        if not all_dirs:
            template = review.get("manual_sidecar")
            if not template:
                return False, "契约未声明手动审稿记录路径（manual_sidecar），手动路径无法核对审稿"
            return await _find_manual_review_pass(
                backend, render_template(template, params), draft_path
            )
    return await _find_run_review_pass(
        backend, contract["name"], run_id, draft_path, thread_id
    )


def _sidecar_path(draft: str) -> str:
    # Same derivation as `src.dalton.publish.service._derive_sidecar_paths`.
    if draft.endswith(".md"):
        return f"{draft[:-3]}.audit.md"
    return f"{draft}.audit.md"


async def _check_stages(
    contract: dict[str, Any],
    params: dict[str, Any],
    backend: Any,
    run_id: str | None,
    thread_id: str | None = None,
) -> tuple[list[str], list[str], str | None]:
    missing: list[str] = []
    warnings: list[str] = []
    draft_path: str | None = None
    rule = (contract.get("closing") or {}).get("check_draft")
    if rule or contract.get("draft"):
        template = (rule or {}).get("draft") or contract.get("draft")
        if template:
            draft_path = render_template(template, params)

    for stage in contract.get("stages", []):
        stage_name = stage.get("name", "")
        if stage.get("requires_param") and not params.get(stage["requires_param"]):
            continue
        stage_missing = False
        for artifact in stage.get("artifacts", []):
            gap, warn = await _check_artifact(backend, artifact, params)
            missing.extend(gap)
            warnings.extend(warn)
            stage_missing = stage_missing or bool(gap)
        for validator in stage.get("validators", []):
            try:
                command = render_command(validator["command"], params)
            except ContractError as error:
                missing.append(f"{stage_name}：{error}")
                continue
            if stage_missing:
                warnings.append(
                    f"校验命令未重跑（本阶段有产物缺失，先补文件）：{command}"
                )
                continue
            result = await _run_command(backend, command)
            if result is None:
                missing.append(
                    f"校验命令无法在当前环境执行（{stage_name}）：{command}；无法核对，请重试。"
                )
                continue
            code, output = result
            expect = validator.get("expect")
            mode = validator.get("pass_when", "exit_zero")
            passed = code == 0
            if mode == "output_contains":
                passed = expect in output
            elif mode == "output_not_contains":
                passed = expect not in output
            if not passed:
                missing.append(
                    f"校验未通过（{stage_name}）：{command} → 退出码 {code}。"
                    f"输出尾部：{output[-_VALIDATOR_OUTPUT_CHARS:]!r}。"
                    "下一步：按输出修到通过。"
                )
        review = stage.get("review")
        if review:
            try:
                passed, reason = await _review_pass(
                    backend, contract, review, params, run_id, draft_path, thread_id
                )
            except _CannotVerify as error:
                missing.append(
                    f"阶段「{stage_name}」审稿结论无法核对（{error}），请重试。"
                )
                continue
            if not passed:
                missing.append(
                    f"阶段「{stage_name}」要求本次 run 的最终审稿结论为 pass，未通过：{reason}。"
                )
    return missing, warnings, draft_path


async def _conditional_knowledge_docs(
    backend: Any,
    contract: dict[str, Any],
    params: dict[str, Any],
) -> tuple[list[str], list[str], list[str]]:
    """(docs_all, docs_any, problems) demanded by JSON-conditional rules.

    A rule's state file that cannot be read or parsed is a problem (无法
    核对), never a skipped condition."""
    docs_all: list[str] = []
    docs_any: list[str] = []
    problems: list[str] = []
    for rule in (contract.get("closing") or {}).get("knowledge_docs_if_json") or []:
        path = render_template(rule["json"], params)
        status, text = await _read_text_diagnosed(backend, path)
        if status == "error" or (status == "ok" and text is None):
            problems.append(f"条件文件读取出错（{path}），无法核对，请重试。")
            continue
        if status == "missing":
            problems.append(f"条件文件不存在（{path}），无法核对条件，请重跑该步骤。")
            continue
        try:
            doc = json.loads(text or "")
        except ValueError:
            problems.append(f"条件文件不可解析（{path}），请重试。")
            continue
        when = rule.get("when") or {}
        triggered = False
        if "field" in when and "equals" in when:
            triggered = isinstance(doc, dict) and doc.get(when["field"]) == when["equals"]
        elif when.get("any_gt"):
            total = 0
            if isinstance(doc, dict):
                for field in when["any_gt"]:
                    value = doc.get(field)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        total += value
            triggered = total > 0
        if triggered:
            docs_all.extend(rule.get("docs") or [])
            docs_any.extend(rule.get("docs_any") or [])
    return docs_all, docs_any, problems


async def _check_closing(
    contract: dict[str, Any],
    params: dict[str, Any],
    backend: Any,
    workspace_id: str | None,
    user_id: str | None,
) -> tuple[list[str], list[str], str]:
    missing: list[str] = []
    warnings: list[str] = []
    closing = contract.get("closing") or {}
    since = _today_start_utc()
    since_label = since.date().isoformat()

    docs_all = list(closing.get("knowledge_docs", []))
    docs_any = list(closing.get("knowledge_docs_any", []))
    for doc, param in (closing.get("knowledge_docs_requires_param") or {}).items():
        if params.get(param):
            docs_all.append(doc)
    cond_all, cond_any, cond_problems = await _conditional_knowledge_docs(
        backend, contract, params
    )
    docs_all.extend(cond_all)
    docs_any.extend(cond_any)
    missing.extend(cond_problems)

    if (docs_all or docs_any) and not workspace_id:
        missing.append(
            "无 workspace_id，知识层版本无法核对（需在公司工作区线程调用），请重试。"
        )
    elif docs_all or docs_any:
        try:
            found = await _check_knowledge_versions(
                workspace_id, docs_all + docs_any, since
            )
        except Exception as error:
            logger.warning(
                "dalton_check_done knowledge version query failed", error=str(error)
            )
            missing.append(
                "知识层版本无法核对（数据库查询失败），请重试；"
                f"待核对：{'、'.join(dict.fromkeys(docs_all + docs_any))}。"
            )
            found = None
        if found is not None:
            for doc in dict.fromkeys(docs_all):
                if not found[doc]:
                    missing.append(
                        f"知识层 {doc} 自 {since_label} 起没有 source=tool 的新版本；"
                        "下一步：用 knowledge_update 写入。"
                    )
            if docs_any and not any(found[doc] for doc in docs_any):
                missing.append(
                    f"知识层 {'、'.join(docs_any)} 自 {since_label} 起"
                    "均无 source=tool 新版本（至少需要其一）；下一步：用 knowledge_update 写入。"
                )

    kinds = closing.get("verdict_kinds_any")
    verdict_rows: list[dict[str, Any]] = []
    triggers = _as_trigger_list(closing.get("verdict_trigger"))
    if kinds and not workspace_id:
        missing.append("无 workspace_id，verdict 无法核对（需在公司工作区线程调用），请重试。")
    elif kinds:
        try:
            verdict_rows = await _check_verdicts(
                workspace_id, kinds=kinds, triggers=triggers, since=since
            )
            if not verdict_rows:
                trigger_note = (
                    f"（trigger_kind={'/'.join(triggers)}）" if triggers else ""
                )
                missing.append(
                    f"自 {since_label} 起没有符合条件的 verdict{trigger_note}，"
                    f"需 word ∈ {'/'.join(kinds)}；"
                    "下一步：用 knowledge_append_verdict 写入。"
                )
        except Exception as error:
            logger.warning("dalton_check_done verdict query failed", error=str(error))
            missing.append("verdict 无法核对（数据库查询失败），请重试。")

    rule = closing.get("check_draft")
    if rule:
        draft = render_template(rule.get("draft") or contract.get("draft", ""), params)
        required = rule.get("required", True)
        draft_text = await _read_text(backend, draft)
        audit_text = await _read_text(backend, _sidecar_path(draft))
        if draft_text is None:
            message = f"check_draft 目标草稿不存在：{draft}。"
        elif audit_text is None:
            message = (
                f"缺少数字核验侧车 {_sidecar_path(draft)}；"
                f"下一步：对 {draft} 调用 check_draft。"
            )
        else:
            match = _DRAFT_HASH_RE.search(audit_text)
            if match is None:
                message = (
                    f"{_sidecar_path(draft)} 里读不到草稿哈希；"
                    f"重新调用 check_draft(draft_path='{draft}')。"
                )
            else:
                current = hashlib.sha256(draft_text.encode("utf-8")).hexdigest()
                if not current.startswith(match.group(1)):
                    message = (
                        f"check_draft 核验的是旧草稿：侧车 {_sidecar_path(draft)} 记录哈希 "
                        f"{match.group(1)[:12]}…，当前 {draft} 的哈希 "
                        f"{current[:12]}…。下一步：对最终稿重新调用 check_draft。"
                    )
                else:
                    message = ""
        if message:
            # SKILL-optional check_draft: every failure mode stays a warning.
            (missing if required else warnings).append(message)
    return missing, warnings, since_label


async def run_check_done(
    *,
    workflow: str,
    params: dict[str, Any],
    backend: Any,
    workspace_id: str | None,
    user_id: str | None = None,
    run_id: str | None = None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """The whole read-only check; returns ``{done, missing, warnings, checked}``.

    Any internal failure is reported as a ``missing`` entry — the tool never
    raises, never writes, and never lets a failed check read as a pass."""
    missing: list[str] = []
    warnings: list[str] = []
    checked: dict[str, Any] = {
        "workflow": workflow,
        "since": _today_start_utc().date().isoformat(),
        "run_id": run_id,
    }

    try:
        contract = load_contract(workflow)
        gap_params = missing_params(contract, params)
        if gap_params:
            return {
                "done": False,
                "missing": [
                    f"缺少必需参数 '{p}'（params 必须与启动 RunWorkflow/skill 时一致）。"
                    for p in gap_params
                ],
                "warnings": [],
                "checked": checked,
            }
        if backend is None:
            return {
                "done": False,
                "missing": ["当前没有可用的文件后端，无法核对工作区产物。"],
                "warnings": [],
                "checked": checked,
            }
        stage_missing, stage_warnings, _draft = await _check_stages(
            contract, params, backend, run_id, thread_id
        )
        missing.extend(stage_missing)
        warnings.extend(stage_warnings)
        close_missing, close_warnings, since_label = await _check_closing(
            contract, params, backend, workspace_id, user_id
        )
        missing.extend(close_missing)
        warnings.extend(close_warnings)
        checked["since"] = since_label
    except ContractError:
        missing.append(f"{workflow} 尚未定义完成契约")
    except Exception as error:
        logger.warning("dalton_check_done failed", workflow=workflow, error=str(error))
        missing.append(f"核对过程出错（{type(error).__name__}），无法核对，请重试。")

    return {
        "done": not missing,
        "missing": missing,
        "warnings": warnings,
        "checked": checked,
    }


# Home (Chief of Staff) runs only the weekly review; the company workflows'
# contracts need a workspace's knowledge layer, so that role may not check them.
_ROLE_WORKFLOWS: dict[str, frozenset[str]] = {
    "chief_of_staff": frozenset({"weekly-coverage-review"}),
}


def create_check_done_tools(
    filesystem_backend: Any = None, *, role: str = "analyst"
) -> list[BaseTool]:
    """Build ``[dalton_check_done]`` bound to ``filesystem_backend``; a role
    listed in ``_ROLE_WORKFLOWS`` may only check those workflows."""
    allowed = _ROLE_WORKFLOWS.get(role)

    @tool("dalton_check_done")
    async def _dalton_check_done(
        workflow: str,
        params: dict[str, Any],
        run_id: str | None = None,
        *,
        config: RunnableConfig,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> str:
        """对照完成契约核对一次 workflow/多步 skill 是否真的做完，汇报完成前必须调用。

        传 workflow 名（initial-coverage、full-coverage、bullbear-doc、earnings-deep-update、weekly-coverage-review、topic-research 之一），params 用启动时的同一组参数；run_id 传本次运行的 task id（.progress.json 的 run_id），不传则取本线程最近一次同名 workflow 的 run。
        workflow 关闭、按 SKILL 手动降级执行时，params 加 "manual": true，审稿改核对 SKILL 指定的 review.json。
        只读不改：重查文件、哈希、校验脚本、知识层版本、verdict 与本次 run 的审稿 pass。不通过时先补齐 missing 再查；补不了的只能汇报"未完成"。
        Returns: JSON {done, missing, warnings}，missing 为中文缺项与下一步。
        """
        if allowed is not None and workflow not in allowed:
            return json.dumps(
                {
                    "done": False,
                    "missing": [f"该角色只能核对 {'、'.join(sorted(allowed))}"],
                    "warnings": [],
                    "checked": {"workflow": workflow},
                },
                ensure_ascii=False,
                indent=2,
            )
        user_id, workspace_id, thread_id, _ = _extract_run_identity(config)
        if not isinstance(params, dict):
            params = {"as_of": params}
        try:
            result = await run_check_done(
                workflow=workflow,
                params=params,
                backend=filesystem_backend,
                workspace_id=workspace_id,
                user_id=user_id,
                run_id=run_id,
                thread_id=thread_id,
            )
        except Exception as error:
            result = {
                "done": False,
                "missing": [f"工具内部错误（{type(error).__name__}），无法核对，请重试。"],
                "warnings": [],
                "checked": {"workflow": workflow},
            }
        return json.dumps(result, ensure_ascii=False, indent=2)

    return [_dalton_check_done]


_DEFAULT_CHECK_DONE_TOOLS = create_check_done_tools(None)
dalton_check_done = _DEFAULT_CHECK_DONE_TOOLS[0]
