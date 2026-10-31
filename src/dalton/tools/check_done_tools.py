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
import posixpath
import re
import shlex
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import structlog
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from src.dalton.knowledge.profiles import (
    PROFILE_SECTOR,
    resolve_workspace_profile,
)
from src.dalton.publish.language import (
    _REVIEWABLE_ROOTS,
    SIDECAR_SUFFIXES,
    is_reviewable_deliverable,
)
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

# Freshness tolerance shared by every "written this run" judgment: absorbs
# Postgres `::bigint` rounding, 1s `stat -c %Y` granularity, and minor
# host/sandbox clock skew (a sandbox clock 2s behind must not fail its own
# just-written file).
MTIME_TOLERANCE_SECONDS = 5

# The one gap a named-but-undatable turn reports: no silent fallback window.
_RUN_START_UNAVAILABLE_MESSAGE = (
    "无法核对本轮完成情况（读不到本轮开始时间），请稍后重试 dalton_check_done。"
)


def _thread_prefix(thread_id: str | None) -> str:
    """The journal/status glob prefix narrowed to ONE thread when its id is
    known (run dirs live under the 8-char short id, `agent.py`'s spelling)."""
    if thread_id:
        return f".agents/threads/{str(thread_id)[:8]}/workflows/"
    return ".agents/threads/*/workflows/"


class _CannotVerify(Exception):
    """A check could not run — surfaces as ``missing`` with 无法核对，请重试."""


class _RunStartUnavailable(_CannotVerify):
    """The current turn is named (``conversation_run_id``) but its start could
    not be read: the check cannot be anchored to the run, and falling back to a
    calendar-day window would silently admit whatever else the workspace
    holds, so the caller reports a retryable gap instead."""


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


# US research anchors its freshness window in America/New_York: the covered
# markets' own clock, independent of the caller's timezone (audit #20 — a
# UTC-midnight window drops every version written before an Asia-morning
# crossing).
_US_EASTERN = ZoneInfo("America/New_York")


def _day_start_eastern(day: str) -> datetime | None:
    """Midnight of ``day`` (``YYYY-MM-DD``) in US/Eastern, or None when ``day``
    is not a parseable date. The window's day boundary is the market's, not
    UTC's and not the viewer's."""
    try:
        base = datetime.fromisoformat(f"{day}T00:00:00")
    except ValueError:
        return None
    return base.replace(tzinfo=_US_EASTERN)


async def _read_text(backend: Any, path: str) -> str | None:
    try:
        return await backend.aread_text(path)
    except Exception:
        return None


# The existence probe prints exactly one of these; an output with neither
# (an error string, or nothing) means the probe could not run.
_PROBE_PRESENT = "__DALTON_EXISTS__"
_PROBE_ABSENT = "__DALTON_ABSENT__"


async def _read_text_diagnosed(backend: Any, path: str) -> tuple[str, str | None]:
    """("ok", text) | ("missing", None) | ("error", None).

    ``missing`` demands an affirmative absence: the shell probe RAN and
    printed its ABSENT marker — nothing else qualifies. A None read proves
    nothing either way: the read layer answers None for an empty file and a
    UTF-8 decode failure just as for a missing one (``sandbox files.py``).
    No marker in the probe output — ``SandboxBackend.aexecute`` turns a dead
    sandbox into exit code 1 with an error string — leaves existence
    unknown, and unknown must never read as absent."""
    try:
        text = await backend.aread_text(path)
        if text is not None:
            return "ok", text
    except Exception:
        pass
    probe = await _run_command(
        backend,
        # Grouped so a failed `cd <workspace>` prints no marker at all
        # instead of falling into the ABSENT arm of `||`.
        f"{{ test -e {shlex.quote(path)} && echo {_PROBE_PRESENT} "
        f"|| echo {_PROBE_ABSENT}; }}",
    )
    if probe is not None:
        if _PROBE_PRESENT in probe[1]:
            return "error", None  # present but unreadable
        if _PROBE_ABSENT in probe[1]:
            return "missing", None
    # No marker (or no command runner at all): the probe never answered, so
    # existence stays unknown no matter how the read came back None.
    return "error", None


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


_FILENAME_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _parse_iso_day(text: str) -> str | None:
    """Canonical ``YYYY-MM-DD`` when ``text`` is a real calendar date, else None."""
    if not _FILENAME_DATE_RE.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _relevant_variant_days(
    params: dict[str, Any], since_epoch: int | None
) -> set[str] | None:
    """The calendar days a variant's FILENAME may name: the run's ``as_of``
    day and the Eastern business day of the window start (the same two a
    caller legitimately spells). None when neither parses — then no variant
    can pass the date test at all."""
    days: set[str] = set()
    anchor = str(
        params.get("as_of") or params.get("release_date") or params.get("end") or ""
    ).strip()
    parsed_anchor = _parse_iso_day(anchor)
    if parsed_anchor is not None:
        days.add(parsed_anchor)
    if since_epoch is not None:
        days.add(
            datetime.fromtimestamp(since_epoch, tz=_US_EASTERN).date().isoformat()
        )
    return days or None


async def _run_written_variant(
    backend: Any,
    artifact: dict[str, Any],
    params: dict[str, Any],
    missing_path: str,
    since_epoch: int | None,
) -> tuple[str, _FileInfo] | None:
    """The file this run actually wrote where the dated template found nothing.

    A dated deliverable (``research/{as_of}-delta.md``) is conventionally named
    for the business day, but a caller crossing a midnight boundary may spell a
    different calendar day than the template rendered. ``variant_glob`` widens
    the dated segment to ``*``; a match counts only when its FILENAME names a
    day this run could legitimately spell (``as_of`` or the window start's
    Eastern business day) AND it is provably written this run (mtime at/after
    the window start) — a re-saved old note must not stand in for this run's
    deliverable. Newest qualifying match wins. None when the contract declares
    no variant, the glob cannot run, or nothing qualifies — an unreadable mtime
    or an undated filename proves nothing and reads as no match."""
    pattern = artifact.get("variant_glob")
    if not pattern or since_epoch is None:
        return None
    days = _relevant_variant_days(params, since_epoch)
    matches = await _glob(backend, render_template(pattern, params))
    if matches is None:
        return None
    best: tuple[str, _FileInfo] | None = None
    for candidate in (m for m in matches if not m.endswith(SIDECAR_SUFFIXES)):
        name_day = _FILENAME_DATE_RE.search(posixpath.basename(candidate))
        parsed_day = _parse_iso_day(name_day.group(0)) if name_day else None
        if days is None or parsed_day is None or parsed_day not in days:
            continue
        info = await _file_info(backend, candidate)
        if not info.exists or info.mtime_epoch is None:
            continue
        # The same tolerance as the artifact floor: a variant written 2s
        # before the recorded window start (clock skew) is this run's.
        if info.mtime_epoch < since_epoch - MTIME_TOLERANCE_SECONDS:
            continue
        if best is None or info.mtime_epoch > best[1].mtime_epoch:
            best = (candidate, info)
    return best


async def _resolve_real_path(
    backend: Any, raw_path: str
) -> tuple[str | None, str | None]:
    """``(resolved_file, resolved_root)`` — the RAW path as the caller spelled
    it (NOT normpath'd first: ``research/hop/../report.md`` must resolve
    through the symlink, which a pre-collapsed spelling would hide — audit r2
    #4) and the workspace root's real path, both resolved IN the sandbox
    (``realpath`` under the ``cd <workspace>`` prefix, so symlinks are
    followed; no ``-e`` — the dev mac's BSD realpath lacks it, and existence
    is the artifact check's own job anyway). ``(None, None)`` when the backend
    cannot execute or the resolution fails — the caller then falls back to
    string validation and says so."""
    file_out = await _run_command(backend, f"realpath -- {shlex.quote(raw_path)}")
    root_out = await _run_command(backend, "realpath -- .")

    def _last_line(out: tuple[int, str]) -> str | None:
        lines = [ln.strip() for ln in out[1].splitlines() if ln.strip()]
        return lines[-1] if lines else None

    if file_out is None or file_out[0] != 0:
        return None, None
    if root_out is None or root_out[0] != 0:
        return None, None
    return _last_line(file_out), _last_line(root_out)


def _anchor_real_path(
    resolved_file: str, resolved_root: str, workspace_dir: str | None
) -> str:
    """A spelling of the resolved REAL file that later checks (``wc``/``stat``/
    reads) use, so validation and every subsequent check speak about the same
    file. Anchored under the workspace_dir spelling when one is known (the
    same file even when workspace_dir itself contains symlinked segments);
    the resolved absolute path otherwise."""
    if not workspace_dir:
        return resolved_file
    if resolved_file == resolved_root:
        return workspace_dir
    if resolved_file.startswith(resolved_root + "/"):
        return posixpath.join(workspace_dir, resolved_file[len(resolved_root) + 1 :])
    return resolved_file


async def _validate_explicit_artifact_path(
    explicit: str, template: str, backend: Any
) -> tuple[str | None, str | None, str | None]:
    """``(gap, note, checked_path)`` for an explicitly passed ``path_param``
    value; gap and note both None means the path is fit to be judged, and
    ``checked_path`` is the spelling every later check of this artifact uses.

    An explicit path names the file the agent says it actually wrote, so it
    must be the same kind of product the contract declares. The string checks
    (inside the workspace, the template's top-level directory, the template's
    suffix) run once on the ORIGINAL spelling; the real path is then resolved
    IN the sandbox from that original spelling (symlinks and ``..`` followed)
    and the same containment/shape checks run again on the resolved path
    against the workspace's REAL root — anything else is a gap, never a
    silent fallback to the dated template. When the sandbox cannot resolve,
    the string checks stand, the note says so, and ``checked_path`` is the
    original spelling (no root to anchor a real-path spelling against)."""
    workspace_dir = getattr(backend, "workspace_dir", None)
    if not isinstance(workspace_dir, str):
        workspace_dir = None
    rel = _workspace_relative(explicit, workspace_dir)
    template_root = template.split("/", 1)[0] if "/" in template else ""
    template_suffix = posixpath.splitext(template)[1]
    if rel.startswith("/") or rel == ".." or rel.startswith("../"):
        return (
            f"显式交付路径越出工作区（{explicit}），无法核对；"
            "下一步：传入当前工作区内的实际交付文件。",
            None,
            None,
        )
    explicit_root = rel.split("/", 1)[0] if "/" in rel else ""
    if template_root and explicit_root != template_root:
        return (
            f"显式交付路径 {explicit} 不在契约声明的 {template_root}/ 下，无法核对；"
            f"下一步：传入 {template_root}/ 下的实际交付文件。",
            None,
            None,
        )
    if template_suffix and posixpath.splitext(rel)[1] != template_suffix:
        return (
            f"显式交付路径 {explicit} 的扩展名须为 {template_suffix}"
            "（与契约声明一致），无法核对。",
            None,
            None,
        )

    resolved_file, resolved_root = await _resolve_real_path(backend, explicit)
    if resolved_file is None or resolved_root is None:
        return (
            None,
            f"显式交付路径 {explicit} 未能解析真实路径（沙箱无法执行 realpath 或"
            "文件不存在），仅做了字符串校验。",
            explicit,
        )
    if resolved_file != resolved_root and not resolved_file.startswith(
        resolved_root + "/"
    ):
        return (
            f"显式交付路径 {explicit} 解析后的真实路径越出当前工作区"
            f"（符号链接指向 {resolved_file}），无法核对；"
            "下一步：传入工作区内的实际交付文件。",
            None,
            None,
        )
    resolved_rel = (
        resolved_file[len(resolved_root) + 1 :] if resolved_file != resolved_root else ""
    )
    resolved_top = resolved_rel.split("/", 1)[0] if resolved_rel else ""
    if template_root and resolved_top != template_root:
        return (
            f"显式交付路径 {explicit} 解析后的真实文件不在契约声明的 {template_root}/ 下"
            f"（指向 {resolved_file}），无法核对。",
            None,
            None,
        )
    if template_suffix and posixpath.splitext(resolved_rel)[1] != template_suffix:
        return (
            f"显式交付路径 {explicit} 解析后的真实文件扩展名须为 {template_suffix}"
            f"（指向 {resolved_file}），无法核对。",
            None,
            None,
        )
    # 无 workspace_dir 可锚定真实路径拼写时（真实后端总有），退回原始拼写——
    # 校验解析的就是这个拼写，仍是同一个文件。
    checked = (
        _anchor_real_path(resolved_file, resolved_root, workspace_dir)
        if workspace_dir
        else explicit
    )
    return None, None, checked or explicit


async def _check_artifact(
    backend: Any,
    artifact: dict[str, Any],
    params: dict[str, Any],
    since_epoch: int | None = None,
) -> tuple[list[str], list[str], dict[str, str]]:
    """(missing, warnings, resolved) for one declared artifact. Everything
    required that cannot be verified lands in missing; only SKILL-optional
    artifacts degrade their failures to warnings. ``resolved`` maps the
    TEMPLATE-rendered path to the concrete file the check actually judged
    (explicit ``path_param`` spellings and variant matches included), so the
    closing checks can speak about the same file."""
    missing: list[str] = []
    warnings: list[str] = []
    resolved: dict[str, str] = {}
    name = artifact["path"]

    def _report(message: str) -> None:
        (warnings if _optional(artifact) else missing).append(message)

    if artifact.get("requires_param") and not params.get(artifact["requires_param"]):
        return missing, warnings, resolved

    condition = artifact.get("requires_json")
    if condition:
        # An unreadable state file is 无法核对, never "condition not met":
        # skipping would let a lost material=yes pass without its note.
        state_path = render_template(condition["json"], params)
        status, state_text = await _read_text_diagnosed(backend, state_path)
        if status == "missing":
            _report(f"条件文件不存在（{state_path}），无法判定是否要求 {name}；下一步：重跑写它的步骤。")
            return missing, warnings, resolved
        if status != "ok" or state_text is None:
            _report(f"条件文件读取出错（{state_path}），无法核对 {name}，请重试。")
            return missing, warnings, resolved
        try:
            state = json.loads(state_text)
        except ValueError:
            _report(f"条件文件不可解析（{state_path}），无法核对 {name}，请重试。")
            return missing, warnings, resolved
        if not isinstance(state, dict):
            _report(f"条件文件结构异常（{state_path}），无法核对 {name}，请重试。")
            return missing, warnings, resolved
        # A missing or non-string field is 无法核对, never "condition not met":
        # {"run_id": ...} without material would otherwise skip the note and
        # still read as done.
        field = condition["field"]
        value = state.get(field)
        if not isinstance(value, str):
            _report(
                f"条件文件缺少字符串字段 {field}（{state_path}），"
                f"无法判定是否要求 {name}；下一步：重跑写它的步骤。"
            )
            return missing, warnings, resolved
        allowed = condition.get("allowed_values")
        if allowed and value not in allowed:
            _report(
                f"条件文件字段 {field}={value!r} 不在允许值 {'/'.join(allowed)}"
                f"（{state_path}），无法判定是否要求 {name}；下一步：重跑写它的步骤。"
            )
            return missing, warnings, resolved
        if value != condition["equals"]:
            return missing, warnings, resolved

    concrete: list[str] | None
    problems: list[str] = []
    # The template spelling the closing checks render themselves; the file
    # judged here may be an explicit ``path_param`` path or a variant match.
    template_path: str | None = None
    explicit_used = False
    if artifact.get("derive"):
        concrete, problems = await _derived_artifact_paths(backend, artifact, params)
        for problem in problems:
            _report(problem)
        if concrete is None or not concrete:
            return missing, warnings, resolved
    else:
        template_path = render_template(artifact["path"], params)
        # An explicitly passed path (``path_param``) is what the agent says it
        # actually wrote; the dated template is only the conventional spelling.
        # Its shape is validated against the template first — a blessing of an
        # unrelated file must fail, not silently check the template instead.
        explicit_key = artifact.get("path_param")
        explicit = str(params.get(explicit_key) or "").strip() if explicit_key else ""
        if explicit:
            explicit_used = True
            concrete = [render_template(explicit, params)]
            explicit_problem, explicit_note, checked_path = (
                await _validate_explicit_artifact_path(concrete[0], template_path, backend)
            )
            if explicit_problem is not None:
                _report(explicit_problem)
                return missing, warnings, resolved
            if explicit_note is not None:
                warnings.append(explicit_note)
            if checked_path:
                # 校验解析的是这个文件，后续 wc/stat 等检查就说同一个文件
                #（解析后的真实路径的锚定拼写）。
                concrete = [checked_path]
        else:
            concrete = [template_path]

    if artifact.get("glob"):
        matches = await _glob(backend, concrete[0])
        if matches is None:
            _report(f"当前环境无法执行 glob（{name}），无法核对，请重试。")
            return missing, warnings, resolved
        # Sidecars ride beside each target by the tools' stem convention, and
        # a ``cluster-*.md`` pattern matches ``cluster-a.md.language.md`` just
        # as well — they never count toward a deliverable's own minimum.
        matches = [m for m in matches if not m.endswith(SIDECAR_SUFFIXES)]
        need = artifact.get("min_count", 1)
        if len(matches) < need:
            _report(
                f"产物 {name} 需要 ≥{need} 个匹配，实际 {len(matches)} 个；"
                "下一步：补齐缺失的支线/清单文件。"
            )
            return missing, warnings, resolved
        targets = matches
        versions = [_version_of(path) for path in matches]
        if all(v is not None for v in versions):
            # One final model: the max-VERSION match is the file every check
            # (mtime, size) speaks about — never an mtime-picked one.
            targets = [matches[versions.index(max(versions))]]
        elif artifact.get("modified_on"):
            _report(f"无法从匹配名解析版本号以确定最终文件（{name}），无法核对，请重试。")
            return missing, warnings, resolved
    else:
        targets = concrete

    # Freshness, two regimes:
    # - ``fresh_this_run`` artifacts (a wake skill's note / draft /
    #   predictions.json / last_run.json — the run's own deliverables) must be
    #   written inside this run's window however their path resolved, or a
    #   same-day rerun passes on the earlier round's files. ``modified_on:
    #   "today"`` on such an artifact IS the window start (UTC midnight only
    #   when even the window is unknown); ``"as_of"`` keeps its calendar day
    #   when it starts later.
    # - Everything else keeps the pre-window rules (existence / min_bytes /
    #   day anchors): a legitimately reused file — dalton-publish's approved
    #   draft, a workflow-resume carry-over — is judged on its content, not on
    #   which round last touched it.
    modified_on = artifact.get("modified_on")
    if modified_on not in (None, "today", "as_of"):
        missing.append(f"契约字段 modified_on 非法（{name}：{modified_on!r}），请修契约。")
        return missing, warnings, resolved
    fresh = bool(artifact.get("fresh_this_run"))
    run_floor = (
        since_epoch - MTIME_TOLERANCE_SECONDS
        if (fresh and since_epoch is not None)
        else None
    )
    floor_epoch, floor_label = run_floor, "本轮开始时间"
    if modified_on == "today" and floor_epoch is None:
        floor_epoch, floor_label = (
            int(_today_start_utc().timestamp()),
            "today 锚定日",
        )
    elif modified_on == "as_of":
        anchor = params.get("as_of") or params.get("release_date") or params.get("end")
        day = str(anchor) if anchor else None
        try:
            base = (
                datetime.fromisoformat(f"{day}T00:00:00+00:00")
                if day
                else _today_start_utc()
            )
            as_of_floor = int(base.timestamp())
        except ValueError:
            as_of_floor = int(_today_start_utc().timestamp())
        if as_of_floor > (floor_epoch if floor_epoch is not None else -1):
            floor_epoch, floor_label = as_of_floor, "as_of 锚定日"

    for path in targets:
        if explicit_used and template_path is not None:
            resolved[template_path] = path
        info = await _file_info(backend, path)
        # A stale template file is not this run's writing either: the run may
        # have written the variant spelling instead, so the variant search
        # runs for an out-of-window template file exactly as for a missing one.
        stale_template = bool(
            fresh
            and run_floor is not None
            and artifact.get("variant_glob")
            and info.exists
            and (info.mtime_epoch is None or info.mtime_epoch < run_floor)
        )
        if (not info.exists or stale_template) and not explicit_used:
            variant = await _run_written_variant(
                backend, artifact, params, path, since_epoch
            )
            if variant is not None:
                # The dated template guessed a different day than the file the
                # run wrote; judge the file that exists, by this run's window.
                path, info = variant
            elif not info.exists:
                if artifact.get("variant_glob"):
                    _report(
                        f"产物文件不存在：{path}，也未找到符合条件的 "
                        f"{artifact['variant_glob']}（文件名日期须为本轮 as_of 或"
                        "美东业务日、mtime 须在本轮窗口内）；"
                        "下一步：写入该阶段交付物后重查。"
                    )
                else:
                    _report(f"产物文件不存在：{path}。下一步：补齐该阶段交付物后重查。")
                continue
            # Stale template, no qualifying variant: the floor check below
            # reports it with the expected path.
        if template_path is not None:
            # Later checks (check_draft, language review, sidecars) render the
            # template again; point them at the file this run actually wrote.
            resolved[template_path] = path
        if artifact.get("min_bytes") and info.size < artifact["min_bytes"]:
            _report(
                f"产物文件过小（{path}，{info.size} 字节 < {artifact['min_bytes']}），"
                "疑似占位。下一步：写满该交付物。"
            )
        if floor_epoch is not None:
            if info.mtime_epoch is None:
                _report(f"无法读取文件时间（{path}），本轮写入未核对，请重试。")
            elif info.mtime_epoch < floor_epoch:
                _report(
                    f"产物文件不是本轮写的（{path}，mtime 早于{floor_label}）；"
                    f"期望路径：{template_path or path}；"
                    "下一步：本轮重写该文件后重查。"
                )
        marker = artifact.get("end_marker")
        if marker:
            text = info.text if info.text is not None else await _read_text(backend, path)
            if text is None or marker not in text:
                _report(
                    f"产物文件缺少完成标记 {marker}：{path}。下一步：补齐内容并写上标记。"
                )
        # Sidecars live beside each target the way check_draft writes them
        # (draft stem + suffix), so a contract names only the draft path.
        for sidecar in artifact.get("sidecars") or []:
            sidecar_path = _derived_sidecar(path, sidecar["suffix"])
            sidecar_info = await _file_info(backend, sidecar_path)
            if not sidecar_info.exists:
                _report(f"产物文件不存在：{sidecar_path}。下一步：补齐该阶段交付物后重查。")
            elif sidecar_info.size < sidecar.get("min_bytes", 1):
                _report(
                    f"产物文件过小（{sidecar_path}，{sidecar_info.size} 字节 "
                    f"< {sidecar['min_bytes']}），疑似占位。下一步：写满该交付物。"
                )
    return missing, warnings, resolved


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
    thread_id: str | None = None,
) -> list[dict[str, Any]]:
    """This run's own verdicts, not the workspace's.

    ``dalton_verdicts`` records ``conversation_thread_id``/``run_id`` on every
    insert (see ``insert_verdict``), so when the matched rows carry ids only
    THIS thread's verdict closes this check — a sibling task's same-window
    verdict on the same workspace must not. Rows with no ids at all (legacy
    writes) fall back to the contract's declared ``verdict_trigger`` kinds,
    the only attribution that data supports; a contract declaring none keeps
    the kinds-only match for them."""
    from src.dalton.database import verdicts as verdicts_db

    rows = await verdicts_db.list_verdicts(workspace_id, limit=200, since=since)
    matched = [row for row in rows if _verdict_row_matches(row, kinds, triggers)]
    if thread_id:
        if any(row.get("conversation_thread_id") for row in matched):
            own_thread = str(thread_id)
            return [
                row
                for row in matched
                if str(row.get("conversation_thread_id") or "") == own_thread
            ]
        if triggers:
            return [
                row for row in matched if row.get("trigger_kind") in set(triggers)
            ]
    return matched


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
        # An unreadable, unparsable or empty status.json may be the newest
        # run's; skipping it would fall back to an older pass, so it blocks
        # the check. Only a readable file whose ``name`` names a DIFFERENT
        # workflow is safely skipped.
        text = await _read_text(backend, path)
        try:
            doc = json.loads(text) if text else None
        except ValueError:
            raise _CannotVerify(f"运行状态文件无法读取或已损坏：{path}，请传 run_id")
        if not isinstance(doc, dict):
            raise _CannotVerify(f"运行状态文件无法读取或已损坏：{path}，请传 run_id")
        name = doc.get("name")
        if not isinstance(name, str):
            raise _CannotVerify(
                f"运行状态文件缺少 name，无法判断是否本次 workflow 的 run：{path}，请传 run_id"
            )
        if name != workflow:
            continue
        stamp = doc.get("updated_at")
        if isinstance(stamp, (int, float)) and stamp > best_stamp:
            best_stamp = float(stamp)
            best_dir = path.rsplit("/status.json", 1)[0]
    return best_dir


async def _run_args_as_of(
    backend: Any, workflow: str, run_id: str | None, thread_id: str | None
) -> str | None:
    """The effective ``as_of`` recorded in this run's ``args.json``.

    Resolution mirrors the run checks: the named run when ``run_id`` was
    passed, else the thread's latest run of the same workflow. The date is
    read, never recomputed — the launch path's trading-day default is what
    the run's artifacts are keyed on. An unreadable record inherits nothing;
    the missing-parameter check then names the gap honestly.
    """
    dirs: list[str]
    if run_id:
        dirs = [f"{_thread_prefix(thread_id)}{run_id}"]
    else:
        if not thread_id:
            return None
        try:
            latest = await _latest_run_dir(backend, workflow, thread_id)
        except _CannotVerify:
            return None
        dirs = [latest] if latest else []
    for run_dir in dirs:
        text = await _read_text(backend, f"{run_dir}/args.json")
        if not text:
            continue
        try:
            doc = json.loads(text)
        except ValueError:
            continue
        if not isinstance(doc, dict):
            continue
        as_of = doc.get("as_of")
        if isinstance(as_of, str) and as_of.strip() and as_of.strip() != "latest":
            return as_of.strip()
    return None


async def _review_records_from_journals(
    backend: Any, journals: list[str]
) -> list[dict[str, Any]]:
    """Every review-labeled dispatch record across ``journals``, oldest first
    (``recorded_at`` then ``seq``); raises ``_CannotVerify`` on any read or
    parse failure."""
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
    records.sort(key=lambda item: (item[0], item[1]))
    return [entry for _, _, entry in records]


async def _collect_review_records(
    backend: Any,
    workflow: str,
    run_id: str | None,
    thread_id: str | None,
) -> list[dict[str, Any]]:
    """Every review-labeled dispatch record of THIS run, oldest first. Any
    scan/parse failure raises ``_CannotVerify``; locating the run follows
    ``_find_run_review_pass`` (including its unfinished-run refusal)."""
    if not run_id and not thread_id:
        raise _CannotVerify("缺少线程身份（thread_id 与 run_id 均未提供），无法定位本次 run")
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
            raise _CannotVerify(
                "本线程有未结束的 workflow run，无法确定本次 run；请传 run_id（.progress.json 里的 run_id）"
            )
        run_dir = await _latest_run_dir(backend, workflow, thread_id)
        if run_dir is None:
            raise _CannotVerify("找不到本次 workflow 的 run 目录（无 run_id 且无 status.json）")
        journals = await _glob(backend, f"{run_dir}/journal/*.json")
        if journals is None:
            raise _CannotVerify("无法扫描运行日志（journal）")
    return await _review_records_from_journals(backend, journals)


async def _find_run_review_pass(
    backend: Any,
    workflow: str,
    run_id: str | None,
    draft_path: str | None,
    thread_id: str | None = None,
) -> tuple[bool, str]:
    """(passed, reason): the LAST review record of THIS run must be a pass,
    and its recorded draft hash must equal the CURRENT draft's hash —
    computed here from the file, never taken from the model's word."""
    if not run_id and not thread_id:
        return False, "缺少线程身份（thread_id 与 run_id 均未提供），无法定位本次 run"
    if not run_id:
        # The two unlocatable-run outcomes return False (the caller names the
        # next step); every scan/parse failure below still raises, exactly as
        # before the helper existed.
        all_dirs, _finished = await _thread_run_dirs(backend, thread_id)
        if all_dirs - _finished:
            return False, "本线程有未结束的 workflow run，无法确定本次 run；请传 run_id（.progress.json 里的 run_id）"
        run_dir = await _latest_run_dir(backend, workflow, thread_id)
        if run_dir is None:
            return False, "找不到本次 workflow 的 run 目录（无 run_id 且无 status.json），无法核对审稿"
    records = await _collect_review_records(backend, workflow, run_id, thread_id)
    if not records:
        return False, "本次 run 的运行日志里没有任何审稿/复审记录"
    last = records[-1]
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


def _derived_sidecar(path: str, suffix: str) -> str:
    # Same stem convention as `src.dalton.publish.service._derive_sidecar_paths`.
    if path.endswith(".md"):
        return f"{path[:-3]}{suffix}"
    return f"{path}{suffix}"


def _sidecar_path(draft: str) -> str:
    return _derived_sidecar(draft, ".audit.md")


async def _check_stages(
    contract: dict[str, Any],
    params: dict[str, Any],
    backend: Any,
    run_id: str | None,
    thread_id: str | None = None,
    since_epoch: int | None = None,
) -> tuple[list[str], list[str], str | None, dict[str, str]]:
    """(missing, warnings, draft, resolved): ``resolved`` maps each artifact's
    template-rendered path to the concrete file the stage check judged, for
    the closing checks to speak about the same file."""
    missing: list[str] = []
    warnings: list[str] = []
    resolved: dict[str, str] = {}
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
            gap, warn, artifact_resolved = await _check_artifact(
                backend, artifact, params, since_epoch
            )
            missing.extend(gap)
            warnings.extend(warn)
            resolved.update(artifact_resolved)
            stage_missing = stage_missing or bool(gap)
        if draft_path is not None and draft_path in resolved:
            draft_path = resolved[draft_path]
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
    return missing, warnings, draft_path, resolved


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
            field = when["field"]
            value = doc.get(field) if isinstance(doc, dict) else None
            # Same fail-closed rule as a requires_json artifact condition: a
            # missing or out-of-domain value cannot stand in for "not met".
            if not isinstance(value, str):
                problems.append(
                    f"条件文件缺少字符串字段 {field}（{path}），无法核对条件，请重跑该步骤。"
                )
                continue
            allowed = when.get("allowed_values")
            if allowed and value not in allowed:
                problems.append(
                    f"条件文件字段 {field}={value!r} 不在允许值 {'/'.join(allowed)}"
                    f"（{path}），无法核对条件，请重跑该步骤。"
                )
                continue
            triggered = value == when["equals"]
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


async def _closing_since(
    backend: Any,
    workflow: str,
    params: dict[str, Any],
    run_id: str | None,
    thread_id: str | None,
    conversation_run_id: str | None = None,
) -> tuple[datetime, list[str]]:
    """The closing checks' freshness window and any gap from fixing it.

    Audit #20: the window starts at THIS run's first dispatch (the earliest
    journal ``recorded_at``), so versions written during the run always count,
    whatever timezone it crossed; the manual path (no run) falls back to the
    current turn's start, else the ``as_of`` day's start in America/New_York.
    A journal that exists but cannot be read is a gap (fail-closed), with the
    manual window as the best-effort floor the remaining checks still run
    against.
    """
    try:
        _files, run_start = await _collect_run_dispatch_files(
            backend, workflow, run_id, thread_id
        )
    except _CannotVerify as error:
        manual_epoch, gap = await _manual_window_or_gap(
            params, thread_id, conversation_run_id
        )
        if gap:
            return datetime.fromtimestamp(manual_epoch, tz=timezone.utc), gap
        since = datetime.fromtimestamp(manual_epoch, tz=timezone.utc)
        return since, [f"知识层时间窗口无法核对（{error}），请重试。"]
    if run_start is not None:
        return datetime.fromtimestamp(run_start, tz=timezone.utc), []
    manual_epoch, gap = await _manual_window_or_gap(
        params, thread_id, conversation_run_id
    )
    since = datetime.fromtimestamp(manual_epoch, tz=timezone.utc)
    return since, gap or []


async def _check_closing(
    contract: dict[str, Any],
    params: dict[str, Any],
    backend: Any,
    workspace_id: str | None,
    user_id: str | None,
    run_id: str | None = None,
    thread_id: str | None = None,
    *,
    conversation_run_id: str | None = None,
    since: datetime | None = None,
    since_gaps: list[str] | None = None,
    resolved: dict[str, str] | None = None,
) -> tuple[list[str], list[str], str]:
    missing: list[str] = []
    warnings: list[str] = []
    resolved_map = resolved or {}
    closing = contract.get("closing") or {}
    if since is None:
        since, since_gaps = await _closing_since(
            backend, contract["name"], params, run_id, thread_id, conversation_run_id
        )
    missing.extend(since_gaps or [])
    since_label = since.date().isoformat()

    docs_all = list(closing.get("knowledge_docs", []))
    docs_any = list(closing.get("knowledge_docs_any", []))
    for doc, param in (closing.get("knowledge_docs_requires_param") or {}).items():
        if params.get(param):
            docs_all.append(doc)
    # Sector-branch docs (codex r1 #1/#5): decided by the workspace's coverage
    # identity, never by params.subject_kind — a caller could omit or lie about
    # it and dodge the sector deliverables. A claimed value that CONTRADICTS
    # the identity is an explicit failure, not a silent correction; an
    # identity that cannot be resolved fails closed (无法核对, retry).
    sector_docs = closing.get("knowledge_docs_if_sector") or []
    if sector_docs:
        if not workspace_id:
            missing.append(
                "无 workspace_id，无法按工作区覆盖身份核对行业分支知识层，请重试。"
            )
        else:
            try:
                profile = await resolve_workspace_profile(workspace_id)
            except Exception as error:
                logger.warning(
                    "dalton_check_done coverage identity query failed",
                    workspace_id=workspace_id,
                    error=str(error),
                )
                profile = None
                missing.append(
                    "工作区覆盖身份无法核对（行业分支要求无法判定），请重试；"
                    f"待核对：{'、'.join(sector_docs)}。"
                )
            if profile is not None:
                claimed = str(params.get("subject_kind") or "").strip().lower()
                if claimed in ("sector", "company"):
                    actual = PROFILE_SECTOR if profile == PROFILE_SECTOR else "company"
                    if claimed != actual:
                        missing.append(
                            f"params.subject_kind={claimed!r} 与本工作区覆盖身份"
                            f"（{actual}）冲突；完成契约按身份核对，请更正参数后重查。"
                        )
                if profile == PROFILE_SECTOR:
                    docs_all.extend(sector_docs)
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
                workspace_id,
                kinds=kinds,
                triggers=triggers,
                since=since,
                thread_id=thread_id,
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

    conditional_docs = closing.get("knowledge_docs_any_unless_no_change") or []
    if conditional_docs and verdict_rows:
        # Only reachable once this round's verdicts were read: a failed or
        # empty verdict query is already its own missing entry above.
        missing.extend(
            await _check_knowledge_unless_no_change(
                workspace_id, conditional_docs, verdict_rows, since
            )
        )

    gate = closing.get("gated_draft")
    if gate:
        missing.extend(
            await _check_gated_draft(
                backend,
                gate,
                params,
                workspace_id,
                since,
                thread_id=thread_id,
                resolved=resolved_map,
            )
        )

    publication = closing.get("published_or_awaiting_pm")
    if publication:
        missing.extend(
            await _check_published_or_awaiting(
                backend,
                publication,
                params,
                workspace_id,
                user_id,
                resolved=resolved_map,
            )
        )

    rule = closing.get("check_draft")
    if rule:
        draft_template = render_template(
            rule.get("draft") or contract.get("draft", ""), params
        )
        draft = resolved_map.get(draft_template, draft_template)
        required = rule.get("required", True)
        message = await _check_draft_sidecar(backend, draft)
        if message:
            # SKILL-optional check_draft: every failure mode stays a warning.
            (missing if required else warnings).append(message)

    lang_rule = closing.get("language_review")
    if lang_rule:
        required = lang_rule.get("required", True)
        gaps = await _check_language_review(
            backend,
            lang_rule,
            params,
            contract,
            run_id,
            thread_id,
            conversation_run_id,
            resolved=resolved_map,
        )
        for gap in gaps:
            (missing if required else warnings).append(gap)

    push_rule = closing.get("push_draft")
    if push_rule:
        missing.extend(
            await _check_push_draft(
                backend,
                push_rule,
                params,
                contract,
                run_id,
                thread_id,
                conversation_run_id=conversation_run_id,
                resolved=resolved_map,
            )
        )
    return missing, warnings, since_label


async def _locate_run_journals(
    backend: Any,
    workflow: str,
    run_id: str | None,
    thread_id: str | None,
) -> list[str] | None:
    """This workflow's run journal files; ``None`` when no same-name run
    exists (the manual path). Locating never consults the review check's
    "any unfinished run on the thread" rule — only runs of the same workflow
    count. Raises ``_CannotVerify`` when a located run cannot be scanned."""
    if not run_id and not thread_id:
        return None
    prefix = _thread_prefix(thread_id)
    if run_id:
        journals = await _glob(backend, f"{prefix}{run_id}/journal/*.json")
        if journals is None:
            raise _CannotVerify("无法扫描本次 run 的运行日志（journal）")
        return journals
    run_dir = await _latest_run_dir(backend, workflow, thread_id)
    if run_dir is None:
        # No finished run of THIS workflow: manual semantics, whatever other
        # (possibly crashed) run directories sit on the thread.
        return None
    journals = await _glob(backend, f"{run_dir}/journal/*.json")
    if journals is None:
        raise _CannotVerify("无法扫描运行日志（journal）")
    return journals


async def _collect_run_dispatch_files(
    backend: Any,
    workflow: str,
    run_id: str | None,
    thread_id: str | None,
) -> tuple[list[str] | None, float | None]:
    """(files, run_start): every file path this run's dispatches reported
    writing, as the dispatches themselves spelled them (``files`` entries; the
    evidence child's ``path|sha256`` strings included), plus the run's start
    (the earliest journal ``recorded_at``). ``(None, None)`` when this
    workflow has no run on the thread — the manual path."""
    journals = await _locate_run_journals(backend, workflow, run_id, thread_id)
    if not journals:
        return None, None

    files: set[str] = set()
    started: float | None = None
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
            stamp = entry.get("recorded_at")
            if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
                started = stamp if started is None else min(started, float(stamp))
            result = entry.get("result")
            if not isinstance(result, dict):
                continue
            for item in result.get("files") or []:
                if isinstance(item, str) and item.strip():
                    files.add(item.split("|", 1)[0].strip())
    return sorted(files), started


async def _run_start_epoch(run_id: str | None) -> int | None:
    """The run's ``created_at`` as epoch seconds from ``conversation_responses``;
    None when it cannot be read (no id, or the database is out of reach in this
    context)."""
    if not run_id:
        return None
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT EXTRACT(EPOCH FROM created_at)::bigint "
                    "FROM conversation_responses WHERE conversation_response_id = %s",
                    (run_id,),
                )
                row = await cur.fetchone()
    except Exception as error:
        logger.warning(
            "dalton_check_done run start query failed", error=str(error)
        )
        return None
    if not row or isinstance(row[0], bool) or not isinstance(row[0], (int, float)):
        return None
    return int(row[0])


async def _thread_start_epoch(thread_id: str | None) -> int | None:
    """The thread's ``created_at`` as epoch seconds; None when it cannot be
    read (no id, or the database is out of reach in this context)."""
    if not thread_id:
        return None
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT EXTRACT(EPOCH FROM created_at)::bigint "
                    "FROM conversation_threads WHERE conversation_thread_id = %s",
                    (thread_id,),
                )
                row = await cur.fetchone()
    except Exception as error:
        logger.warning(
            "dalton_check_done thread start query failed", error=str(error)
        )
        return None
    if not row or isinstance(row[0], bool) or not isinstance(row[0], (int, float)):
        # Anything but a real epoch (a stub cursor's mock, a NULL) reads as
        # "thread start unknown": the manual window then falls back to 24h,
        # the push-draft check fails closed.
        return None
    return int(row[0])


async def _push_draft_since_epoch(
    conversation_run_id: str | None,
    thread_id: str | None,
) -> int | None:
    """Start epoch of the current conversation turn for push-draft freshness:
    1. ``_run_start_epoch(conversation_run_id)`` from ``conversation_responses``,
    2. ``_thread_start_epoch(thread_id)`` from ``conversation_threads``.
    Never consults workflow journals or ``as_of`` midnight; returns ``None``
    when neither can be read so the caller fails closed."""
    started = await _run_start_epoch(conversation_run_id)
    if started is not None:
        return started
    return await _thread_start_epoch(thread_id)


async def _manual_since_epoch(
    params: dict[str, Any],
    thread_id: str | None,
    conversation_run_id: str | None = None,
) -> int:
    """The manual path's window start: THIS turn's start (the run's
    ``created_at``) first, because an auto-wake fired before an Eastern
    midnight crossing would otherwise anchor the window after everything the
    turn wrote; then the run's ``as_of`` day at midnight America/New_York
    (release_date/end are the aliases the contracts render); then the thread's
    start; and only when even that is unreadable, the last 24 hours — a window
    ending at today's midnight would drop notes written before a midnight
    crossing.

    A turn that is NAMED (``conversation_run_id``) but whose start cannot be
    read raises ``_RunStartUnavailable`` instead of falling back: every
    fallback window is a guess the workspace's older files could satisfy."""
    run_start = await _run_start_epoch(conversation_run_id)
    if run_start is not None:
        return run_start
    if conversation_run_id:
        raise _RunStartUnavailable(
            f"本轮运行（{conversation_run_id}）的开始时间未能读取"
        )
    day = str(
        params.get("as_of") or params.get("release_date") or params.get("end") or ""
    ).strip()
    if day and day != "latest":
        base = _day_start_eastern(day)
        if base is not None:
            return int(base.timestamp())
    thread_start = await _thread_start_epoch(thread_id)
    if thread_start is not None:
        return thread_start
    return int(time.time()) - 24 * 3600


async def _manual_window_or_gap(
    params: dict[str, Any],
    thread_id: str | None,
    conversation_run_id: str | None,
) -> tuple[int, list[str] | None]:
    """``(epoch, None)`` from the manual window, or ``(epoch, gap)`` when the
    turn is named but its start is unreadable — the 24h floor keeps the
    remaining checks running while the gap forces ``done=false``."""
    try:
        return await _manual_since_epoch(params, thread_id, conversation_run_id), None
    except _RunStartUnavailable:
        return int(time.time()) - 24 * 3600, [_RUN_START_UNAVAILABLE_MESSAGE]


_SCAN_RC_RE = re.compile(r"^__DALTON_SCAN_RC__=(\d+)\s*$")


async def _scan_reviewable_since(backend: Any, since_epoch: int) -> list[str] | None:
    """Reviewable ``.md`` files under the existing scan roots modified at/after
    ``since_epoch`` — mtime via the same ``stat -c %Y`` the ``modified_on``
    checks use. ``None`` when the scan cannot run (fail-closed at the caller):
    a dead executor or failed ``cd`` prints no rc marker, and a nonzero find
    exit (say an unreadable directory) is a failed scan, not an empty one.
    A root that does not exist is simply not scanned. A candidate whose mtime
    cannot be read stays IN the list: the file sits under a reviewable root,
    and unprovable age must not read as out-of-window."""
    roots = []
    for root in _REVIEWABLE_ROOTS:
        # The probe answers one of two markers whenever the shell ran — a dead
        # executor (or failed cd) prints neither, and that is a failed scan,
        # not a missing root.
        probe = await _run_command(
            backend,
            f"{{ test -d {root} && echo __DALTON_DIR_{root} "
            f"|| echo __DALTON_DIR_NONE; }}",
        )
        if probe is None:
            return None
        if f"__DALTON_DIR_{root}" in probe[1]:
            roots.append(root)
        elif "__DALTON_DIR_NONE" not in probe[1]:
            return None
    if not roots:
        return []
    listing = await _run_command(
        backend,
        f"{{ find {' '.join(roots)} -type f -name '*.md' 2>/dev/null; "
        f'echo "__DALTON_SCAN_RC__=$?"; }}',
    )
    if listing is None:
        return None
    output = listing[1]
    rc: int | None = None
    path_lines: list[str] = []
    for line in output.splitlines():
        match = _SCAN_RC_RE.match(line.strip())
        if match:
            rc = int(match.group(1))
            continue
        if line.strip():
            path_lines.append(line.strip())
    if rc != 0:
        # find itself failed (unreadable directory, sandbox error): the
        # listing is not trustworthy either way.
        return None
    kept: list[str] = []
    for path in path_lines:
        stat = await _run_command(backend, f"stat -c %Y {shlex.quote(path)}")
        if stat is not None and stat[0] == 0:
            try:
                mtime = int(stat[1].strip().splitlines()[-1])
            except (ValueError, IndexError):
                mtime = None
            if mtime is not None and mtime < since_epoch:
                continue
        kept.append(path)
    return kept


async def _last_review_pass_sha256(
    backend: Any,
    workflow: str,
    run_id: str | None,
    thread_id: str | None,
) -> tuple[bool, str | None]:
    """(found_run, sha): whether a same-name run exists, and its last PASSING
    review record's ``draft_sha256`` (None when the run has no pass record).
    Reads the located journals directly — never ``_collect_review_records``,
    whose unfinished-run refusal would let a neighbour workflow's crashed run
    fail this lookup. Raises ``_CannotVerify`` when a located run cannot be
    scanned."""
    journals = await _locate_run_journals(backend, workflow, run_id, thread_id)
    if journals is None:
        return False, None
    for entry in reversed(await _review_records_from_journals(backend, journals)):
        if entry.get("status") != "ok":
            continue
        result = entry.get("result")
        sha = result.get("draft_sha256") if isinstance(result, dict) else None
        if isinstance(result, dict) and result.get("verdict") == "pass":
            return True, sha if isinstance(sha, str) and _SHA256_RE.match(sha) else None
    return True, None


async def _check_language_record(
    backend: Any,
    path: str,
    *,
    accept_review_sha: bool,
    workflow: str,
    run_id: str | None,
    thread_id: str | None,
) -> str:
    """``''`` when ``path``'s ``<path>.language.json`` carries rounds 1 and 2
    with the LAST round-2 hash equal to the CURRENT file's hash (or, when
    ``accept_review_sha``, the last passing review's hash); else the gap."""
    json_path = f"{path}.language.json"
    status, text = await _read_text_diagnosed(backend, json_path)
    if status == "error":
        return f"语言审查记录读取出错（{json_path}），无法核对，请重试。"
    if status == "missing" or text is None:
        return (
            f"缺少语言审查记录 {json_path}；下一步：对 {path} 调用 "
            "check_language(round=1) 并按清单修改，内容定稿后再调用 check_language(round=2)。"
        )
    try:
        doc = json.loads(text)
    except ValueError:
        return f"语言审查记录不可解析（{json_path}），请重跑 check_language。"
    rounds = doc.get("rounds") if isinstance(doc, dict) else None
    if not isinstance(rounds, list):
        return f"语言审查记录结构异常（{json_path}，缺 rounds 数组），请重跑 check_language。"
    round1 = next((r for r in rounds if isinstance(r, dict) and r.get("round") == 1), None)
    round2 = next(
        (r for r in reversed(rounds) if isinstance(r, dict) and r.get("round") == 2), None
    )
    if round1 is None or round2 is None:
        return (
            f"{path} 的语言审查记录 {json_path} 缺少第 {'1' if round1 is None else '2'} 轮；"
            "下一步：初稿阶段调用 check_language(round=1) 并按清单修改，"
            "内容定稿后调用 check_language(round=2)，改稿后再调用一次 round=2 复核。"
        )
    last2_sha = round2.get("draft_sha256")
    if not isinstance(last2_sha, str) or not _SHA256_RE.match(last2_sha):
        return (
            f"{path} 的语言审查第 2 轮记录里没有 64 位十六进制的草稿哈希（{json_path}），"
            "请重跑 check_language(round=2)。"
        )

    status, file_text = await _read_text_diagnosed(backend, path)
    if status != "ok" or file_text is None:
        return f"被审文件读取失败（{path}），无法核对语言审查哈希，请重试。"
    current = hashlib.sha256(file_text.encode("utf-8")).hexdigest()
    if last2_sha == current:
        return ""
    if accept_review_sha:
        try:
            found_run, review_sha = await _last_review_pass_sha256(
                backend, workflow, run_id, thread_id
            )
        except _CannotVerify as error:
            return (
                f"{path} 的第 2 轮语言审查针对的可能不是终稿（记录哈希 {last2_sha[:12]}…，"
                f"当前哈希 {current[:12]}…），且审稿哈希无法核对（{error}），请重试。"
            )
        if not found_run:
            # A manual close (no same-name run): there is no review journal to
            # reconcile against, so the only reading is that the draft changed
            # after round 2 — the fix is another round 2, not a run_id hunt.
            return (
                f"{path} 的终稿在第 2 轮语言审查后被改过：{json_path} 最后一条第 2 轮记录哈希 "
                f"{last2_sha[:12]}…，当前哈希 {current[:12]}…；"
                "请对当前稿再跑 check_language(round=2)。"
            )
        if review_sha is not None and last2_sha == review_sha:
            return ""
    return (
        f"第 2 轮语言审查针对的是旧草稿：{json_path} 最后一条第 2 轮记录哈希 "
        f"{last2_sha[:12]}…，当前 {path} 哈希 {current[:12]}…；"
        "下一步：对终稿再调用一次 check_language(round=2) 复核后再送审。"
    )


async def _contract_declared_paths(
    backend: Any,
    contract: dict[str, Any],
    params: dict[str, Any],
    resolved: dict[str, str] | None = None,
) -> list[str]:
    """Concrete paths the contract's stage artifacts declare (each with its
    ``fresh_this_run`` flag), expanded the way ``_check_artifact`` gates them
    (requires_param, requires_json, derive, glob). A declared note joins the
    language-review targets regardless of mtime unless it is fresh_this_run —
    a reused note owes its records too (the same-hash two-round pass satisfies
    it; no re-review is demanded), while a fresh one must be this run's
    writing."""
    resolved_map = resolved or {}
    workspace_dir = getattr(backend, "workspace_dir", None)
    if not isinstance(workspace_dir, str):
        workspace_dir = None
    paths: list[tuple[str, bool]] = []
    for stage in contract.get("stages") or []:
        if stage.get("requires_param") and not params.get(stage["requires_param"]):
            continue
        for artifact in stage.get("artifacts") or []:
            if artifact.get("requires_param") and not params.get(
                artifact["requires_param"]
            ):
                continue
            condition = artifact.get("requires_json")
            if condition:
                state_path = render_template(condition["json"], params)
                status, state_text = await _read_text_diagnosed(backend, state_path)
                met = False
                if status == "ok" and state_text is not None:
                    try:
                        state = json.loads(state_text)
                    except ValueError:
                        state = None
                    value = state.get(condition["field"]) if isinstance(state, dict) else None
                    met = isinstance(value, str) and value == condition["equals"]
                if not met:
                    # The artifact check itself reports unreadable conditions;
                    # for target collection, not-met (or unreadable) is a skip.
                    continue
            if artifact.get("derive"):
                derive = artifact["derive"]
                doc_path = render_template(derive["json"], params)
                status, text = await _read_text_diagnosed(backend, doc_path)
                items = None
                if status == "ok" and text is not None:
                    try:
                        doc = json.loads(text)
                    except ValueError:
                        doc = None
                    items = doc.get(derive["pointer"]) if isinstance(doc, dict) else None
                for item in items or []:
                    value = item.get(derive["field"]) if isinstance(item, dict) else None
                    if value is not None:
                        paths.append(
                            (
                                render_template(
                                    artifact["path"],
                                    {**params, derive["field"]: str(value)},
                                ),
                                False,
                            )
                        )
                continue
            template = render_template(artifact["path"], params)
            explicit_key = artifact.get("path_param")
            explicit = (
                str(params.get(explicit_key) or "").strip() if explicit_key else ""
            )
            if explicit:
                explicit_path = render_template(explicit, params)
                explicit_gap, _note, checked_path = await _validate_explicit_artifact_path(
                    explicit_path, template, backend
                )
                if explicit_gap is not None:
                    # Shape-invalid explicit path: the artifact check reports
                    # it; it is nobody's language target.
                    continue
                concrete_path = checked_path or explicit_path
            else:
                concrete_path = resolved_map.get(template, template)
            if artifact.get("glob"):
                matches = await _glob(backend, concrete_path)
                paths.extend(
                    (m, False)
                    for m in (matches or [])
                    if not m.endswith(SIDECAR_SUFFIXES)
                )
            else:
                paths.append(
                    (
                        resolved_map.get(template, concrete_path),
                        bool(artifact.get("fresh_this_run")),
                    )
                )
    return [
        (rel, fresh)
        for rel, fresh in (
            (_workspace_relative(p, workspace_dir), fresh) for p, fresh in paths
        )
        if is_reviewable_deliverable(rel, workspace_dir)
    ]


async def _check_language_review(
    backend: Any,
    rule: dict[str, Any],
    params: dict[str, Any],
    contract: dict[str, Any],
    run_id: str | None,
    thread_id: str | None,
    conversation_run_id: str | None = None,
    *,
    resolved: dict[str, str] | None = None,
) -> list[str]:
    """Every formal deliverable this run wrote owes two language-review rounds:
    the contract's declared draft and notes — ``fresh_this_run`` ones only
    when this run wrote them (a reused draft, e.g. dalton-publish's approved
    original, rides its existing same-hash records) — plus each reviewable
    file the run's dispatches reported, plus what the mtime window scan of the
    workspace finds (the dispatch list is not a complete write ledger — the
    main agent's own writes never go through a dispatch). Fail-closed
    throughout. ``published/`` archives are nobody's language target (see
    is_reviewable_deliverable): their review is the draft's."""
    resolved_map = resolved or {}
    draft_template = render_template(rule["draft"], params)
    draft = resolved_map.get(draft_template, draft_template)
    if (contract.get("closing") or {}).get("gated_draft"):
        # A gated contract (dalton-call) legitimately has no draft when the
        # gate failed; that branch is judged by gated_draft itself.
        status, _ = await _read_text_diagnosed(backend, draft)
        if status == "missing":
            return []
    # An autospec'd backend answers `workspace_dir` with a Mock — only a real
    # string root can anchor an absolute spelling of a file.
    workspace_dir = getattr(backend, "workspace_dir", None)
    if not isinstance(workspace_dir, str):
        workspace_dir = None

    draft_rel = _workspace_relative(draft, workspace_dir)
    targets: list[str] = [draft]
    seen = {draft_rel}

    def _add(path: str) -> None:
        rel = _workspace_relative(path, workspace_dir)
        if rel in seen or not is_reviewable_deliverable(path, workspace_dir):
            return
        seen.add(rel)
        targets.append(rel)

    # 0. The run's window first — every target below must be this run's
    # writing, a reused note's old records included.
    try:
        run_files, run_start = await _collect_run_dispatch_files(
            backend, contract["name"], run_id, thread_id
        )
    except _CannotVerify as error:
        return [f"本次运行写入的正式产物无法核对（{error}），请重试。"]
    if run_start is not None:
        since_epoch = int(run_start)
    else:
        since_epoch, gap = await _manual_window_or_gap(
            params, thread_id, conversation_run_id
        )
        if gap:
            return gap

    # 1. What the contract declares — only ``fresh_this_run`` files must be
    # this run's writing; a reused draft (dalton-publish's approved original,
    # a resume carry-over) stays a language target on its existing same-hash
    # records. A declared path whose file is affirmatively absent is skipped —
    # the artifact check reports the missing file itself.
    for declared, fresh_path in await _contract_declared_paths(
        backend, contract, params, resolved=resolved_map
    ):
        status, _ = await _read_text_diagnosed(backend, declared)
        if status == "missing":
            continue
        if fresh_path:
            info = await _file_info(backend, declared)
            if (
                info.mtime_epoch is not None
                and info.mtime_epoch < since_epoch - MTIME_TOLERANCE_SECONDS
            ):
                continue
        _add(declared)

    # 2. What the dispatches reported writing.
    for path in run_files or []:
        _add(path)

    # 3. The deterministic mtime manifest: dispatch reports can miss files (a
    # manual flow, a subagent that never echoed a path — and the MAIN agent's
    # own writes, which never go through a dispatch), so the workspace itself
    # is scanned for reviewable files written inside the run's window — the
    # run's start when there is one, else the manual window. A scan that
    # cannot run is a gap, never a pass. (The dispatch ledger is NOT a write
    # ledger: audit r1 #5 — a main-agent note outside the contract would
    # otherwise skip review; reviewing a neighbour flow's in-window file is
    # extra cost, not an error. Machine collection packets are kept out by
    # is_reviewable_deliverable, not by this scan.)
    scanned = await _scan_reviewable_since(backend, since_epoch)
    if scanned is None:
        return ["无法扫描工作区的正式产物（find/stat），无法核对，请重试。"]
    for path in scanned:
        _add(path)

    gaps: list[str] = []
    for target in targets:
        gap = await _check_language_record(
            backend,
            target,
            accept_review_sha=_workspace_relative(target, workspace_dir) == draft_rel,
            workflow=contract["name"],
            run_id=run_id,
            thread_id=thread_id,
        )
        if gap:
            gaps.append(gap)
    return gaps


# The push-draft freshness check shares the module's one tolerance (see
# MTIME_TOLERANCE_SECONDS at the top); the alias keeps its call sites reading.
_PUSH_DRAFT_MTIME_TOLERANCE_SECONDS = MTIME_TOLERANCE_SECONDS


async def _is_current_run_no_change(conversation_run_id: str | None) -> bool:
    """True only when the current conversation run recorded a ``NO_CHANGE``
    verdict via ``get_verdict_for_execution(run_id=conversation_run_id)``."""
    try:
        from src.dalton.database.verdicts import get_verdict_for_execution

        row = await get_verdict_for_execution(run_id=conversation_run_id)
    except Exception as error:
        logger.warning(
            "dalton_check_done execution verdict query failed", error=str(error)
        )
        return False
    return isinstance(row, dict) and row.get("word") == "NO_CHANGE"


async def _check_push_draft(
    backend: Any,
    rule: dict[str, Any],
    params: dict[str, Any],
    contract: dict[str, Any],
    run_id: str | None,
    thread_id: str | None,
    *,
    conversation_run_id: str | None = None,
    resolved: dict[str, str] | None = None,
) -> list[str]:
    """Check ``closing.push_draft``: file existence, written in the current
    round (mtime >= run/thread start epoch - 5s tolerance, fail-closed when
    unreadable), and two-round language review matching current file sha256."""
    path_template = render_template(rule["path"], params)
    path = (resolved or {}).get(path_template, path_template)
    skip_no_change = (
        await _is_current_run_no_change(conversation_run_id)
        if rule.get("skip_when_no_change")
        else False
    )
    status, _ = await _read_text_diagnosed(backend, path)
    if status == "error":
        return [f"推送稿读取出错（{path}），无法核对，请重试。"]
    if status == "missing":
        if skip_no_change:
            return []
        return [
            f"缺少推送稿 {path}（文件不存在）；"
            f"下一步：写入本轮推送稿 {path}，调用 check_language(round=1) 并按清单修改，"
            "内容定稿后再调用 check_language(round=2)。"
        ]

    since_epoch = await _push_draft_since_epoch(conversation_run_id, thread_id)
    if since_epoch is None:
        return [
            f"无法读取本轮运行开始时间（run_id 与 thread_id 均未读到开始时间），"
            f"无法核对推送稿 {path} 是否为本轮写入，请重试。"
        ]

    info = await _file_info(backend, path)
    if info.mtime_epoch is None:
        return [f"无法读取推送稿修改时间（{path}），无法核对是否为本轮写入，请重试。"]
    if info.mtime_epoch < since_epoch - _PUSH_DRAFT_MTIME_TOLERANCE_SECONDS:
        if skip_no_change:
            return []
        return [
            f"推送稿 {path} 修改时间（{info.mtime_epoch}）早于本轮运行开始时间（{since_epoch}），"
            f"非本轮新写；下一步：用本轮内容重写 {path}，并重新完成 check_language(round=1) 与 check_language(round=2)。"
        ]

    gap = await _check_language_record(
        backend,
        path,
        accept_review_sha=False,
        workflow=contract["name"],
        run_id=run_id,
        thread_id=thread_id,
    )
    return [gap] if gap else []


async def _check_draft_sidecar(backend: Any, draft: str) -> str:
    """"" when ``draft``'s check_draft sidecar records the CURRENT draft's
    hash, else the gap."""
    draft_text = await _read_text(backend, draft)
    audit_text = await _read_text(backend, _sidecar_path(draft))
    if draft_text is None:
        return f"check_draft 目标草稿不存在：{draft}。"
    if audit_text is None:
        return (
            f"缺少数字核验侧车 {_sidecar_path(draft)}；"
            f"下一步：对 {draft} 调用 check_draft。"
        )
    match = _DRAFT_HASH_RE.search(audit_text)
    if match is None:
        return (
            f"{_sidecar_path(draft)} 里读不到草稿哈希；"
            f"重新调用 check_draft(draft_path='{draft}')。"
        )
    current = hashlib.sha256(draft_text.encode("utf-8")).hexdigest()
    if not current.startswith(match.group(1)):
        return (
            f"check_draft 核验的是旧草稿：侧车 {_sidecar_path(draft)} 记录哈希 "
            f"{match.group(1)[:12]}…，当前 {draft} 的哈希 "
            f"{current[:12]}…。下一步：对最终稿重新调用 check_draft。"
        )
    return ""


async def _check_knowledge_unless_no_change(
    workspace_id: str | None,
    docs: list[str],
    verdict_rows: list[dict[str, Any]],
    since: datetime,
) -> list[str]:
    """A non-NO_CHANGE word among this round's verdicts pulls at least one
    knowledge update behind it. Any such row counts — the stricter reading
    when one day carries several rounds."""
    changed = sorted(
        {str(row.get("word")) for row in verdict_rows if row.get("word") != "NO_CHANGE"}
    )
    if not changed:
        return []
    if not workspace_id:
        return ["无 workspace_id，条件知识更新无法核对（需在公司工作区线程调用），请重试。"]
    try:
        found = await _check_knowledge_versions(workspace_id, docs, since)
    except Exception as error:
        logger.warning(
            "dalton_check_done conditional knowledge query failed", error=str(error)
        )
        return [
            (
                "条件知识更新无法核对（数据库查询失败），请重试；"
                f"待核对：{'、'.join(docs)}。"
            )
        ]
    if any(found.get(doc) for doc in docs):
        return []
    return [
        (
            f"本轮 verdict 为 {'/'.join(changed)}（非 NO_CHANGE），但知识层 {'、'.join(docs)} "
            f"自 {since.date().isoformat()} 起均无 source=tool 新版本（至少需要其一）；"
            "下一步：用 knowledge_update 写入受影响的文档。"
        )
    ]


async def _check_gated_draft(
    backend: Any,
    gate: dict[str, Any],
    params: dict[str, Any],
    workspace_id: str | None,
    since: datetime,
    *,
    thread_id: str | None = None,
    resolved: dict[str, str] | None = None,
) -> list[str]:
    """The two gate branches, each with its own signature: a formal draft
    (gate passed) owes size + a current check_draft sidecar; no draft (gate
    failed) owes this round's verdict word. Neither signature readable is
    无法判定, never a pass."""
    draft_template = render_template(gate["draft"], params)
    draft = (resolved or {}).get(draft_template, draft_template)
    status, text = await _read_text_diagnosed(backend, draft)
    if status == "error" or (status == "ok" and text is None):
        return [f"正式草稿读取出错（{draft}），无法判定门禁结果，请重试。"]
    if status == "ok":
        gaps: list[str] = []
        need = gate.get("min_bytes")
        size = len((text or "").encode("utf-8"))
        if need and size < need:
            gaps.append(
                f"正式草稿过小（{draft}，{size} 字节 < {need}），疑似占位。下一步：写满该交付物。"
            )
        message = await _check_draft_sidecar(backend, draft)
        if message:
            gaps.append(message)
        return gaps

    kinds = list(gate["absent_verdict_kinds_any"])
    if not workspace_id:
        return [
            (
                f"没有正式草稿 {draft}，且无 workspace_id 无法核对门禁未过分支的 verdict；"
                "无法判定门禁结果，请在公司工作区线程重试。"
            )
        ]
    try:
        rows = await _check_verdicts(
            workspace_id, kinds=kinds, since=since, thread_id=thread_id
        )
    except Exception as error:
        logger.warning("dalton_check_done gate verdict query failed", error=str(error))
        return [
            (
                f"没有正式草稿 {draft}，门禁未过分支的 verdict 无法核对（数据库查询失败），"
                "无法判定门禁结果，请重试。"
            )
        ]
    if rows:
        return []
    return [
        (
            f"无法判定门禁结果：既没有正式草稿 {draft}（门禁通过分支），"
            f"自 {since.date().isoformat()} 起也没有 word ∈ {'/'.join(kinds)} 的 verdict"
            "（门禁未过分支）；下一步：门禁全过就写草稿并 check_draft，"
            "门禁未过就用 knowledge_append_verdict 写 NO_CHANGE。"
        )
    ]


def _workspace_relative(path: str, workspace_dir: str | None) -> str:
    """One canonical form for a path as its writer spelled it. With a root,
    both spellings FIRST resolve to an absolute path — a relative one joins
    onto the workspace root — and only THEN ``posixpath.normpath`` collapses
    ``..``, so ``/root/../root/drafts/x.md`` and ``drafts/x.md`` meet as the
    same file; the root is compared away last. Without a root only the
    relative form is canonicalizable, so absolute spellings never match."""
    cleaned = path.strip()
    root = str(workspace_dir).rstrip("/") if workspace_dir else ""
    if not root:
        cleaned = posixpath.normpath(cleaned)
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        return cleaned
    absolute = cleaned if cleaned.startswith("/") else posixpath.join(root, cleaned)
    normalized = posixpath.normpath(absolute)
    if normalized == root or normalized.startswith(root + "/"):
        return normalized[len(root) :].lstrip("/") or "."
    return normalized  # resolves outside the workspace: a different file


def _same_workspace_path(
    recorded: Any, expected: str, workspace_dir: str | None = None
) -> bool:
    """``publish_report`` keeps the draft_path as the caller spelled it —
    relative, ``./``-prefixed, or absolute inside the workspace. Both sides
    normalize to workspace-relative and must match in full: a path merely
    ENDING in the draft (``archive/drafts/x.md``) or escaping it
    (``../Other/drafts/x.md``) is a different file."""
    if not isinstance(recorded, str):
        return False
    return _workspace_relative(recorded, workspace_dir) == _workspace_relative(
        expected, workspace_dir
    )


async def _check_published_or_awaiting(
    backend: Any,
    rule: dict[str, Any],
    params: dict[str, Any],
    workspace_id: str | None,
    user_id: str | None,
    *,
    resolved: dict[str, str] | None = None,
) -> list[str]:
    """One of the two publication states must exist: the immutable
    ``published/`` copy, or this workspace's ``awaiting_pm`` request for the
    draft. A query that cannot run is 无法核对, never a warning."""
    draft_template = render_template(rule["draft"], params)
    draft = (resolved or {}).get(draft_template, draft_template)
    published = render_template(rule["published"], params)
    info = await _file_info(backend, published)
    if info.exists and info.size > 0:
        return []
    if not user_id or not workspace_id:
        return [
            (
                f"没有 published/ 副本 {published}，且缺少 user_id/workspace_id 无法查询本工作区的 "
                "awaiting_pm 发布请求；发布状态无法核对，请在公司工作区线程重试。"
            )
        ]
    try:
        from src.dalton.database.publications import list_pending

        requests = await list_pending(user_id, "awaiting_pm")
    except Exception as error:
        logger.warning("dalton_check_done awaiting_pm query failed", error=str(error))
        return ["发布状态无法核对（awaiting_pm 查询失败），请重试。"]
    # An autospec'd backend answers `workspace_dir` with a Mock — only a real
    # string root can anchor an absolute spelling of the draft.
    workspace_dir = getattr(backend, "workspace_dir", None)
    if not isinstance(workspace_dir, str):
        workspace_dir = None
    for request in requests:
        if not isinstance(request, dict):
            continue
        if str(request.get("workspace_id") or "") != str(workspace_id):
            continue
        if _same_workspace_path(request.get("draft_path"), draft, workspace_dir):
            return []
    return [
        (
            f"既没有 published/ 副本 {published}，也没有本工作区 draft_path={draft} 的 "
            "awaiting_pm 发布请求；下一步：调用 publish_report 提交发布，或按其返回处理"
            "（audit_failed / review_revise 修稿重提），不得汇报发布完成。"
        )
    ]


async def run_check_done(
    *,
    workflow: str,
    params: dict[str, Any],
    backend: Any,
    workspace_id: str | None,
    user_id: str | None = None,
    run_id: str | None = None,
    thread_id: str | None = None,
    conversation_run_id: str | None = None,
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
        # The launch path resolves ``as_of`` to a concrete trading day before
        # the script runs; the closing check inherits that effective date from
        # the run's own ``args.json`` so the caller's original params (which
        # may omit as_of or say "latest") still find the run's artifacts —
        # never recomputed here, the run's record is the truth.
        if backend is not None and isinstance(params, dict):
            current = params.get("as_of")
            if not (
                isinstance(current, str)
                and current.strip()
                and current.strip() != "latest"
            ):
                inherited = await _run_args_as_of(backend, workflow, run_id, thread_id)
                if inherited:
                    params = {**params, "as_of": inherited}
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
        # One window feeds both the stage artifact variants and the closing
        # checks, so a variant accepted by mtime is the same run the verdict
        # window counts.
        since, since_gaps = await _closing_since(
            backend, contract["name"], params, run_id, thread_id, conversation_run_id
        )
        stage_missing, stage_warnings, _draft, resolved = await _check_stages(
            contract,
            params,
            backend,
            run_id,
            thread_id,
            since_epoch=int(since.timestamp()),
        )
        missing.extend(stage_missing)
        warnings.extend(stage_warnings)
        close_missing, close_warnings, since_label = await _check_closing(
            contract,
            params,
            backend,
            workspace_id,
            user_id,
            run_id,
            thread_id,
            conversation_run_id=conversation_run_id,
            since=since,
            since_gaps=since_gaps,
            resolved=resolved,
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


# Home (Chief of Staff) runs only the weekly review and earnings calendar; the
# company workflows' contracts need a workspace's knowledge layer, so that role
# may not check them.
_ROLE_WORKFLOWS: dict[str, frozenset[str]] = {
    "chief_of_staff": frozenset({"weekly-coverage-review", "dalton-earnings-calendar"}),
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

        传 workflow 名（initial-coverage、full-coverage、bullbear-doc、earnings-deep-update、weekly-coverage-review、topic-research 之一）或多步 skill 名（dalton-earnings-preview、dalton-earnings-flash、dalton-stock-move、dalton-filing-review、dalton-thesis-delta、dalton-call、dalton-publish、dalton-earnings-calendar 之一，params 按该 SKILL 收尾段所列），params 用启动时的同一组参数；run_id 传本次运行的 task id（.progress.json 的 run_id），不传则取本线程最近一次同名 workflow 的 run。
        workflow 关闭、按 SKILL 手动降级执行时，params 加 "manual": true，审稿改核对 SKILL 指定的 review.json。
        只读不改：重查文件、哈希、校验脚本、知识层版本、verdict、本次 run 的审稿 pass 与语言审查两轮记录。不通过时先补齐 missing 再查；补不了的只能汇报"未完成"。
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
        user_id, workspace_id, thread_id, conversation_run_id = _extract_run_identity(
            config
        )
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
                conversation_run_id=conversation_run_id,
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
