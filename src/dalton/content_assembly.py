"""Capability-aware conditional content assembly for Dalton skills, agents, and workflows (Standalone Core Batch 3).

Supports inline conditional markers with proper stack-based nesting and strict
validation so malformed or unclosed markers fail loudly with the file path:
- Markdown / text / YAML (`.md`, `.mdx`, `.txt`, `.yaml`, `.yml`):
  ``<!-- DALTON_CAP:<expr> -->...<!-- /DALTON_CAP -->``
  ``<!-- DALTON_CAP:<expr> -->...<!-- DALTON_CAP_ELSE -->...<!-- /DALTON_CAP -->``
- JavaScript (`.js`):
  ``/* DALTON_CAP:<expr> */.../* /DALTON_CAP */``
  ``/* DALTON_CAP:<expr> */.../* DALTON_CAP_ELSE */.../* /DALTON_CAP */``
- JSON (`.json`): keeps valid JSON on disk; applies structured capability pruning for
  ``workflows/topic-research/contract.json``, ``dalton-thesis-delta/contract.json``,
  ``dalton-sector-delta/contract.json``, and ``common.schema.json`` when optional
  capabilities are disabled, returning the exact original bytes when all referenced
  capabilities are enabled.

``<expr>`` evaluates against ``src.dalton.capabilities.effective_capabilities()``:
- ``cap``: capability enabled
- ``a,b``: all listed capabilities enabled (AND)
- ``a|b``: at least one listed capability enabled (OR)
- ``!cap``: capability disabled
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from src.dalton.capabilities import (
    CapabilityId,
    effective_capabilities,
    has_any_research_connector_capability,
    is_bridge_route_enabled,
    is_research_source_enabled,
)


class ContentAssemblyError(ValueError):
    """Raised when a conditional ``DALTON_CAP`` marker is malformed, unclosed, or invalid."""


_VALID_EXPR_CHARS_RE = re.compile(r"^[a-z0-9_,|!]+$")

_HTML_TOKEN_RE = re.compile(
    r"<!--\s*(?P<kind>/DALTON_CAP|DALTON_CAP_ELSE|DALTON_CAP:(?P<expr>.*?))\s*-->",
    re.DOTALL,
)
_JS_TOKEN_RE = re.compile(
    r"/\*\s*(?P<kind>/DALTON_CAP|DALTON_CAP_ELSE|DALTON_CAP:(?P<expr>.*?))\s*\*/",
    re.DOTALL,
)

# Detect stray/malformed marker prefixes that failed to match a valid token
# (e.g., missing closing `-->` / `*/`, or wrong spacing/casing).
_STRAY_MARKER_RE = re.compile(r"(?:/?DALTON_CAP(?:_ELSE|:)?)", re.IGNORECASE)

_ASSEMBLED_TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {".md", ".mdx", ".txt", ".js", ".json", ".yaml", ".yml"}
)

_VALID_CAPABILITY_VALUES: str = ", ".join(sorted(c.value for c in CapabilityId))


def _format_loc(path: Path | str | None, text: str | None = None, pos: int | None = None) -> str:
    label = Path(path).as_posix() if path is not None else "<string>"
    if text is not None and pos is not None:
        line_no = text.count("\n", 0, pos) + 1
        return f"{label}:{line_no}"
    return label


def evaluate_capability_expr(
    expr: str,
    active_caps: frozenset[CapabilityId],
    *,
    path: Path | str | None = None,
    line_no: int | None = None,
) -> bool:
    """Validate and evaluate a capability expression against ``active_caps``.

    Raises :class:`ContentAssemblyError` (including file path) on empty,
    syntactically invalid, uppercase, or unknown capability names.
    """
    loc = Path(path).as_posix() if path is not None else "<string>"
    if line_no is not None:
        loc = f"{loc}:{line_no}"

    raw = expr.strip()
    if not raw:
        raise ContentAssemblyError(f"{loc}: empty DALTON_CAP expression")
    if not _VALID_EXPR_CHARS_RE.fullmatch(raw):
        raise ContentAssemblyError(
            f"{loc}: invalid DALTON_CAP expression {expr!r} "
            "(only lowercase [a-z0-9_], ',', '|', '!' are allowed)"
        )

    or_parts = raw.split("|")
    matched = False
    for or_part in or_parts:
        if not or_part:
            raise ContentAssemblyError(
                f"{loc}: invalid DALTON_CAP expression {expr!r} (empty OR operand)"
            )
        and_terms = or_part.split(",")
        all_ok = True
        for term in and_terms:
            if not term or term == "!":
                raise ContentAssemblyError(
                    f"{loc}: invalid DALTON_CAP expression {expr!r} (empty AND operand)"
                )
            negate = term.startswith("!")
            cap_name = term[1:] if negate else term
            if "!" in cap_name or not cap_name:
                raise ContentAssemblyError(
                    f"{loc}: invalid DALTON_CAP expression {expr!r} (misplaced '!')"
                )
            try:
                cap = CapabilityId(cap_name)
            except ValueError as exc:
                raise ContentAssemblyError(
                    f"{loc}: unknown capability {cap_name!r} in DALTON_CAP expression {expr!r} "
                    f"(valid: {_VALID_CAPABILITY_VALUES})"
                ) from exc
            present = cap in active_caps
            if negate:
                present = not present
            if not present:
                all_ok = False
        if all_ok:
            matched = True
    return matched


@dataclass
class _BlockFrame:
    parent_active: bool
    cond_true: bool
    in_else: bool
    open_line: int

    @property
    def active(self) -> bool:
        if not self.parent_active:
            return False
        return (not self.cond_true) if self.in_else else self.cond_true


def _check_stray_marker(chunk: str, chunk_start: int, full_text: str, path: Path | str | None) -> None:
    m = _STRAY_MARKER_RE.search(chunk)
    if m is not None:
        loc = _format_loc(path, full_text, chunk_start + m.start())
        snippet = chunk[max(0, m.start() - 8) : min(len(chunk), m.end() + 24)].strip()
        raise ContentAssemblyError(
            f"{loc}: malformed or unclosed DALTON_CAP marker near {snippet!r}"
        )


def _assemble_stacked(
    text: str,
    token_re: re.Pattern[str],
    active_caps: frozenset[CapabilityId],
    *,
    path: Path | str | None = None,
) -> str:
    if "DALTON_CAP" not in text and "dalton_cap" not in text.lower():
        return text

    out_parts: list[str] = []
    stack: list[_BlockFrame] = []
    cursor = 0

    for match in token_re.finditer(text):
        start, end = match.span()
        between = text[cursor:start]
        if between:
            _check_stray_marker(between, cursor, text, path)
            if not stack or stack[-1].active:
                out_parts.append(between)

        kind = match.group("kind")
        line_no = text.count("\n", 0, start) + 1
        loc = _format_loc(path, text, start)

        if kind == "/DALTON_CAP":
            if not stack:
                raise ContentAssemblyError(
                    f"{loc}: unexpected closing /DALTON_CAP without matching opening tag"
                )
            stack.pop()
        elif kind == "DALTON_CAP_ELSE":
            if not stack:
                raise ContentAssemblyError(
                    f"{loc}: unexpected DALTON_CAP_ELSE without matching opening tag"
                )
            if stack[-1].in_else:
                raise ContentAssemblyError(
                    f"{loc}: duplicate DALTON_CAP_ELSE for block opened at line {stack[-1].open_line}"
                )
            stack[-1].in_else = True
        else:
            expr = match.group("expr") or ""
            # Always validate every expression even inside an inactive outer branch.
            cond_true = evaluate_capability_expr(
                expr, active_caps, path=path, line_no=line_no
            )
            parent_active = stack[-1].active if stack else True
            stack.append(
                _BlockFrame(
                    parent_active=parent_active,
                    cond_true=cond_true,
                    in_else=False,
                    open_line=line_no,
                )
            )
        cursor = end

    tail = text[cursor:]
    if tail:
        _check_stray_marker(tail, cursor, text, path)
        if not stack or stack[-1].active:
            out_parts.append(tail)

    if stack:
        unclosed = stack[-1]
        label = Path(path).as_posix() if path is not None else "<string>"
        raise ContentAssemblyError(
            f"{label}:{unclosed.open_line}: unclosed DALTON_CAP block (missing closing /DALTON_CAP)"
        )

    return "".join(out_parts)


_PROPRIETARY_RESEARCH_CAPS: tuple[CapabilityId, ...] = (
    CapabilityId.RESEARCHDATA_BRIDGE,
    CapabilityId.ALPHAENGINE,
    CapabilityId.GUIDEPOINT,
    CapabilityId.SALES_NOTES,
    CapabilityId.TMTB,
    CapabilityId.FUNDA_AI,
)


def assemble_markdown_text(
    text: str,
    *,
    path: Path | str | None = None,
    capabilities: frozenset[CapabilityId] | None = None,
) -> str:
    """Assemble Markdown/text content with ``<!-- DALTON_CAP:... -->`` blocks."""
    active_caps = effective_capabilities() if capabilities is None else capabilities
    out = _assemble_stacked(text, _HTML_TOKEN_RE, active_caps, path=path)
    if not any(cap in active_caps for cap in _PROPRIETARY_RESEARCH_CAPS):
        if "、内部研究库与卖方增量" in out:
            out = out.replace("、内部研究库与卖方增量", "、公开市场新闻与卖方动态")
        if "以行业名与成员公司名检索内部研究库" in out:
            out = out.replace(
                "以行业名与成员公司名检索内部研究库",
                "以行业名与成员公司名检索行业公开资讯与增量资料",
            )
        if "searches the fund research library, wiki, SEC and market data" in out:
            out = out.replace(
                "searches the fund research library, wiki, SEC and market data",
                "searches SEC filings, public sources and market data",
            )
    return out


def assemble_js_text(
    text: str,
    *,
    path: Path | str | None = None,
    capabilities: frozenset[CapabilityId] | None = None,
) -> str:
    """Assemble JavaScript workflow content with ``/* DALTON_CAP:... */`` blocks."""
    active_caps = effective_capabilities() if capabilities is None else capabilities
    return _assemble_stacked(text, _JS_TOKEN_RE, active_caps, path=path)


def assemble_json_text(
    text: str,
    *,
    path: Path | str | None = None,
    capabilities: frozenset[CapabilityId] | None = None,
) -> str:
    """Apply structured capability pruning to known JSON contracts/schemas.

    Returns ``text`` verbatim when all referenced capabilities are active so
    Everflow formatting and byte snapshots stay unchanged.
    """
    active_caps = effective_capabilities() if capabilities is None else capabilities
    path_str = Path(path).as_posix() if path is not None else ""

    if path_str.endswith("workflows/topic-research/contract.json") or (
        '"name": "topic-research"' in text and "guidepoint.md" in text
    ):
        internal_on = any(
            cap in active_caps
            for cap in (
                CapabilityId.RESEARCHDATA_BRIDGE,
                CapabilityId.COMPANY_WIKI,
                CapabilityId.SALES_NOTES,
                CapabilityId.TMTB,
                CapabilityId.FUNDA_AI,
            )
        )
        gp_on = CapabilityId.GUIDEPOINT in active_caps
        if internal_on and gp_on:
            return text
        doc: dict[str, Any] = json.loads(text)
        for stage in doc.get("stages") or []:
            if not isinstance(stage, dict) or not isinstance(stage.get("artifacts"), list):
                continue
            filtered = []
            for art in stage["artifacts"]:
                art_path = str((art or {}).get("path") or "") if isinstance(art, dict) else ""
                if not internal_on and art_path.endswith("/internal-memory.md"):
                    continue
                if not gp_on and art_path.endswith("/guidepoint.md"):
                    continue
                filtered.append(art)
            stage["artifacts"] = filtered
        return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"

    if path_str.endswith("dalton-thesis-delta/contract.json") or path_str.endswith(
        "dalton-sector-delta/contract.json"
    ):
        if CapabilityId.RESEARCHDATA_BRIDGE in active_caps:
            return text
        doc = json.loads(text)
        if path_str.endswith("dalton-thesis-delta/contract.json"):
            doc["description"] = (
                "thesis 增量跟踪的完成契约（公开数据源模式）：事件五词 verdict（必带 payoff_assessment）；"
                "存在实质增量时产出增量笔记并更新受影响知识文档，无实质变化时无笔记为合法结果。"
            )
        else:
            doc["description"] = (
                "行业资料增量跟踪的完成契约（公开数据源模式）：行业五词 verdict；"
                "存在实质增量时产出行业增量笔记并更新受影响行业知识文档，无实质变化时无笔记为合法结果。"
            )
        material_artifacts: list[dict[str, Any]] = []
        for stage in doc.get("stages") or []:
            if not isinstance(stage, dict) or not isinstance(stage.get("artifacts"), list):
                continue
            for art in stage["artifacts"]:
                art_path = str((art or {}).get("path") or "") if isinstance(art, dict) else ""
                if art_path in ("research/tracker/last_run.json", "research/tracker/updates.log"):
                    continue
                if isinstance(art, dict):
                    cleaned_art = dict(art)
                    cleaned_art.pop("requires_json", None)
                    material_artifacts.append(cleaned_art)
        doc["stages"] = [
            {
                "name": "增量核对（实质变化时在收尾契约强制核对增量笔记与知识文档）",
                "artifacts": [],
            }
        ]
        closing = doc.get("closing")
        if isinstance(closing, dict):
            closing = dict(closing)
            docs_any: list[str] = []
            for rule in closing.pop("knowledge_docs_if_json", None) or []:
                if isinstance(rule, dict):
                    docs_any.extend(str(d) for d in (rule.get("docs_any") or []) if d)
            material_req: dict[str, Any] = {}
            if material_artifacts:
                material_req["artifacts"] = material_artifacts
            if docs_any:
                material_req["docs_any"] = list(dict.fromkeys(docs_any))
            if material_req:
                closing["material_verdict_requires"] = material_req
            doc["closing"] = closing
        return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"

    if path_str.endswith("common.schema.json") or "AlphaEngine ASR 稿" in text:
        if CapabilityId.ALPHAENGINE in active_caps:
            return text
        updated = text.replace("，含 AlphaEngine ASR 稿", "").replace(
            "，包括 AlphaEngine ASR 稿", ""
        )
        # Validate JSON stays well-formed after string cleanup.
        json.loads(updated)
        return updated

    if path_str.endswith(".json") and (
        "/dalton-bullbear-tracker/config/" in path_str
        or path_str.startswith("dalton-bullbear-tracker/config/")
    ):
        bridge_on = CapabilityId.RESEARCHDATA_BRIDGE in active_caps
        ae_on = CapabilityId.ALPHAENGINE in active_caps
        doc = json.loads(text)
        if isinstance(doc, dict):
            sources = doc.get("sources")
            sources_copy = dict(sources) if isinstance(sources, dict) else {}
            changed = False
            if not bridge_on:
                sources_copy["internal"] = False
                sources_copy["alphaengine"] = False
                sources_copy["sec"] = False
                sources_copy["x"] = False
                doc["_dalton_cap_researchdata_bridge"] = False
                doc["_dalton_cap_alphaengine"] = False
                changed = True
            else:
                if not ae_on:
                    sources_copy["alphaengine"] = False
                    doc["_dalton_cap_alphaengine"] = False
                    changed = True
                disabled_internal_routes: list[str] = []
                raw_routes = sources_copy.get("internal_routes")
                if isinstance(raw_routes, list):
                    filtered_routes = [
                        r
                        for r in raw_routes
                        if is_bridge_route_enabled(str(r), capabilities=active_caps)
                        or is_research_source_enabled(str(r), capabilities=active_caps)
                    ]
                    if len(filtered_routes) != len(raw_routes):
                        for r in raw_routes:
                            rs = str(r)
                            if (
                                not (
                                    is_bridge_route_enabled(rs, capabilities=active_caps)
                                    or is_research_source_enabled(rs, capabilities=active_caps)
                                )
                                and rs not in disabled_internal_routes
                            ):
                                disabled_internal_routes.append(rs)
                        sources_copy["internal_routes"] = filtered_routes
                        changed = True
                top_routes = doc.get("internal_routes")
                if isinstance(top_routes, list):
                    filtered_top = [
                        r
                        for r in top_routes
                        if is_bridge_route_enabled(str(r), capabilities=active_caps)
                        or is_research_source_enabled(str(r), capabilities=active_caps)
                    ]
                    if len(filtered_top) != len(top_routes):
                        for r in top_routes:
                            rs = str(r)
                            if (
                                not (
                                    is_bridge_route_enabled(rs, capabilities=active_caps)
                                    or is_research_source_enabled(rs, capabilities=active_caps)
                                )
                                and rs not in disabled_internal_routes
                            ):
                                disabled_internal_routes.append(rs)
                        doc["internal_routes"] = filtered_top
                        changed = True
                if disabled_internal_routes:
                    doc["_dalton_cap_disabled_internal_routes"] = disabled_internal_routes
            if not changed:
                return text
            doc["sources"] = sources_copy
            return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"

    if path_str.endswith("/dalton-thesis-delta/config/default-thresholds.json") or (
        path_str == "dalton-thesis-delta/config/default-thresholds.json"
    ):
        bridge_on = CapabilityId.RESEARCHDATA_BRIDGE in active_caps
        ae_on = CapabilityId.ALPHAENGINE in active_caps
        if bridge_on and ae_on:
            return text
        doc = json.loads(text)
        if isinstance(doc, dict):
            # Core-visible skill config must stay free of proprietary source names;
            # inject a neutral boolean flag read by collect.py (same backend-assembled
            # config channel as dalton-bullbear-tracker).
            doc["_skip_sellside_library"] = True
            return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"

    return text


def assemble_text(
    text: str,
    *,
    path: Path | str | None = None,
    capabilities: frozenset[CapabilityId] | None = None,
) -> str:
    """Assemble *text* according to file suffix and active capabilities."""
    suffix = Path(path).suffix.lower() if path is not None else ".md"
    if suffix == ".js":
        assembled = assemble_js_text(text, path=path, capabilities=capabilities)
        # When dedicated Batch 3 connectors are active alongside the legacy bridge
        # (post-cutover Everflow), direct `topic-research/workflow.js` to the mounted
        # dedicated research connectors rather than retired legacy bridge routes, while
        # keeping pre-cutover Everflow output byte-for-byte identical.
        path_norm = Path(path).as_posix() if path is not None else ""
        if path_norm.endswith("topic-research/workflow.js"):
            from src.dalton.capabilities import is_batch3_connectors_enabled
            from src.dalton.settings import get_dalton_research_host_base_url

            active_caps = effective_capabilities() if capabilities is None else capabilities
            if (
                CapabilityId.RESEARCHDATA_BRIDGE in active_caps
                and is_batch3_connectors_enabled()
                and bool(get_dalton_research_host_base_url())
                and has_any_research_connector_capability(active_caps)
            ):
                assembled = assembled.replace(
                    "检索 researchdata 的 sales-notes、tmtb、funda-ai、company-wiki（只读）",
                    "检索已启用的内部研究知识库连接器（只读）",
                )
        return assembled
    if suffix == ".json":
        return assemble_json_text(text, path=path, capabilities=capabilities)
    return assemble_markdown_text(text, path=path, capabilities=capabilities)


def read_assembled_text(
    path: Path,
    *,
    capabilities: frozenset[CapabilityId] | None = None,
) -> str:
    """Read UTF-8 file *path* and assemble conditional capability fragments."""
    raw = path.read_text(encoding="utf-8")
    return assemble_text(raw, path=path, capabilities=capabilities)


def assemble_file_bytes(
    path: Path,
    *,
    capabilities: frozenset[CapabilityId] | None = None,
) -> bytes:
    """Return capability-assembled bytes for *path* (raw bytes for non-text files)."""
    if path.suffix.lower() not in _ASSEMBLED_TEXT_EXTENSIONS:
        return path.read_bytes()
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_bytes()
    assembled = assemble_text(raw, path=path, capabilities=capabilities)
    return assembled.encode("utf-8")


def sha256_assembled_file(
    path: Path,
    *,
    capabilities: frozenset[CapabilityId] | None = None,
) -> str:
    """SHA-256 hex digest of *path*'s capability-assembled bytes."""
    return hashlib.sha256(assemble_file_bytes(path, capabilities=capabilities)).hexdigest()
