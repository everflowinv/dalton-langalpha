"""Guard test verifying tool calls in `plugins/dalton/skills/**/*.md` against real tool signatures."""

from __future__ import annotations

import ast
import importlib.util
import inspect
import re
import sys
from pathlib import Path
from typing import Any

from src.dalton.tools import build_dalton_tools
from src.dalton.web_search.provider import build_web_search_tool
from src.dalton.web_search.x_search import build_x_search_tool
from src.tools.secretary.chief_of_staff import CHIEF_OF_STAFF_TOOLS

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS_ROOT = REPO_ROOT / "plugins" / "dalton" / "skills"
SEC_MCP_PATH = REPO_ROOT / "plugins" / "dalton_data" / "dalton_sec_mcp_server.py"
BRIDGE_DIR = REPO_ROOT / "scripts" / "dalton"

BATCH2_SKILL_DIRS: frozenset[str] = frozenset(
    {
        "dalton-initial-coverage",
        "dalton-full-coverage",
        "dalton-earnings-deep",
        "dalton-topic-research",
        "dalton-bullbear",
        "dalton-earnings-calendar",
    }
)

# Explicit known exceptions in Batch 2 skills (all resolved in Batch 2).
KNOWN_BATCH2_EXCEPTIONS: dict[tuple[str, int, str], dict[str, str]] = {}

_CALL_START_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_.]*)\s*\(")
_KWARG_NAME_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)")


def _is_mcp_tool_decorator(dec: ast.expr) -> bool:
    """Return True if AST decorator node is `@mcp.tool()` or `@mcp.tool`."""
    target = dec.func if isinstance(dec, ast.Call) else dec
    return (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "mcp"
        and target.attr == "tool"
    )


def _collect_sec_mcp_functions() -> tuple[dict[str, set[str]], set[str]]:
    """Return `(registered_sec_tools, disallowed_sec_helper_names)` from `dalton_sec_mcp_server.py`.

    Only functions decorated with `@mcp.tool()` are registered, and only with the `dalton_sec.`
    prefix. Helper functions (e.g., `resolve_cik`, `fetch_company_facts`) and bare unprefixed
    tool names are returned in `disallowed_sec_helper_names` so any call to them fails validation.
    """
    tree = ast.parse(SEC_MCP_PATH.read_text(encoding="utf-8"), filename=str(SEC_MCP_PATH))
    decorated_names: set[str] = set()
    all_fn_names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            all_fn_names.add(node.name)
            if any(_is_mcp_tool_decorator(d) for d in node.decorator_list):
                decorated_names.add(node.name)

    spec = importlib.util.spec_from_file_location("dalton_sec_mcp_server", SEC_MCP_PATH)
    assert spec is not None and spec.loader is not None
    sec_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sec_mod)

    registered: dict[str, set[str]] = {}
    for fn_name in decorated_names:
        fn_obj = getattr(sec_mod, fn_name)
        params = set(inspect.signature(fn_obj).parameters.keys())
        registered[f"dalton_sec.{fn_name}"] = params

    disallowed: set[str] = set()
    for fn_name in all_fn_names:
        if not fn_name.startswith("_"):
            disallowed.add(fn_name)
            if fn_name not in decorated_names:
                disallowed.add(f"dalton_sec.{fn_name}")
    return registered, disallowed


SEC_REGISTERED_TOOLS, SEC_DISALLOWED_FN_NAMES = _collect_sec_mcp_functions()


def build_tool_signature_registry() -> dict[str, set[str]]:
    """Collect parameter names for Dalton direct tools, `dalton_sec.*` MCP tools, and peer tools."""
    registry: dict[str, set[str]] = {}

    # 1. Dalton direct tools across roles (`build_dalton_tools`, from `@tool` `args_schema`)
    for role in ("analyst", "chief_of_staff"):
        for t in build_dalton_tools(role):
            fields = set(t.args_schema.model_fields.keys()) if t.args_schema else set()
            registry.setdefault(t.name, set()).update(fields)

    # 2. `dalton_sec.*` MCP tools (`@mcp.tool()` functions in `dalton_sec_mcp_server.py`, prefixed only)
    for tool_name, params in SEC_REGISTERED_TOOLS.items():
        registry[tool_name] = set(params)

    # 3. Chief of Staff coordination tools (`delegate_to_analyst`, `manage_workspaces`, etc.)
    for t in CHIEF_OF_STAFF_TOOLS:
        fields = set(t.args_schema.model_fields.keys()) if t.args_schema else set()
        registry.setdefault(t.name, set()).update(fields)

    # 4. Direct search tools (`web_search`, `x_search`)
    for t in (build_web_search_tool(), build_x_search_tool()):
        fields = set(t.args_schema.model_fields.keys()) if t.args_schema else set()
        registry[t.name] = fields

    # 5. Workflow tool (`RunWorkflow`)
    registry["RunWorkflow"] = {
        "script",
        "script_path",
        "workflow",
        "params",
        "description",
        "resume",
        "tool_call_id",
    }

    # 6. Five `researchdata` tools (`catalog`, `finance`, `macro`, `research`, `community`),
    # each with its own inputSchema parameter set from `scripts/dalton/researchdata_bridge.py`.
    if str(BRIDGE_DIR) not in sys.path:
        sys.path.insert(0, str(BRIDGE_DIR))
    import researchdata_bridge as rb

    per_tool_params: dict[str, set[str]] = {
        "catalog": {"category", "route"},
        "finance": {"route", "operation", "timeout", "mode"},
        "macro": {"route", "operation", "timeout", "mode"},
        "research": {"route", "operation", "timeout", "mode"},
        "community": {"route", "operation", "timeout", "mode"},
    }
    for route_spec in rb.ROUTE_CONTRACT_SPECS.values():
        tool_short = route_spec["tool"]
        per_tool_params[tool_short].update(route_spec.get("accepted_params", []))
    # The aliases the bridge adds to each tool's published inputSchema are legal too.
    for tool_short, aliases in rb.TOOL_SCHEMA_ALIAS_PROPERTIES.items():
        if tool_short in per_tool_params:
            per_tool_params[tool_short].update(aliases)

    for tool_short, params in per_tool_params.items():
        registry[f"researchdata.{tool_short}"] = set(params)
        registry[tool_short] = set(params)

    return registry


def _split_top_level_args(arg_str: str) -> list[str]:
    """Split a call's argument string on top-level commas, ignoring commas inside brackets or quotes."""
    parts: list[str] = []
    cur: list[str] = []
    depth = 0
    in_str: str | None = None
    i = 0
    while i < len(arg_str):
        ch = arg_str[i]
        if in_str is not None:
            cur.append(ch)
            if ch == "\\" and i + 1 < len(arg_str):
                i += 1
                cur.append(arg_str[i])
            elif ch == in_str:
                in_str = None
        else:
            if ch in ('"', "'"):
                in_str = ch
                cur.append(ch)
            elif ch in ("(", "[", "{"):
                depth += 1
                cur.append(ch)
            elif ch in (")", "]", "}"):
                depth = max(0, depth - 1)
                cur.append(ch)
            elif ch == "," and depth == 0:
                parts.append("".join(cur).strip())
                cur = []
            else:
                cur.append(ch)
        i += 1
    if cur:
        parts.append("".join(cur).strip())
    return parts


def _extract_dict_literal_keys(expr: str) -> list[str] | None:
    """If `expr` is a `{...}` dict literal, return its top-level keys; otherwise return None."""
    s = expr.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return None
    inner = s[1:-1].strip()
    if not inner:
        return []
    keys: list[str] = []
    for entry in _split_top_level_args(inner):
        if not entry or entry in ("...", "…") or entry.startswith("**"):
            continue
        m_key = re.match(
            r'^[\s]*(?:"([^"]+)"|\'([^\']+)\'|([A-Za-z_][A-Za-z0-9_]*))\s*:',
            entry,
        )
        if m_key:
            key = m_key.group(1) or m_key.group(2) or m_key.group(3)
            if key:
                keys.append(key)
    return keys


def _extract_quoted_literals(expr: str) -> list[str]:
    """Return non-placeholder string literals from an argument value expression (e.g. `\"a\"|\"b\"`)."""
    vals: list[str] = []
    for m in re.finditer(r'"([^"\n]*)"|\'([^\'\n]*)\'', expr):
        v = (m.group(1) if m.group(1) is not None else m.group(2)).strip()
        if not v or "<" in v or ">" in v or "..." in v or "…" in v:
            continue
        vals.append(v)
    return vals


def extract_tool_calls_from_markdown(
    text: str,
    registry: dict[str, set[str]],
) -> list[dict[str, Any]]:
    """Extract `tool_name(arg=..., arg2=...)` and registered tool calls from markdown text."""
    calls: list[dict[str, Any]] = []
    for m in _CALL_START_RE.finditer(text):
        name = m.group(1)
        start = m.end()
        depth = 1
        pos = start
        in_str: str | None = None
        while pos < len(text) and depth > 0:
            ch = text[pos]
            if in_str is not None:
                if ch == "\\" and pos + 1 < len(text):
                    pos += 2
                    continue
                if ch == in_str or ch == "\n":
                    in_str = None
            else:
                if ch in ('"', "'"):
                    in_str = ch
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
            pos += 1
        if depth != 0:
            continue
        arg_str = text[start : pos - 1]
        parts = _split_top_level_args(arg_str)
        kwargs: list[str] = []
        kwarg_values: dict[str, str] = {}
        for part in parts:
            if not part or part in ("...", "…") or part.startswith("**"):
                continue
            km = _KWARG_NAME_RE.match(part)
            if km:
                k_name = km.group(1)
                kwargs.append(k_name)
                kwarg_values[k_name] = part[km.end() :].strip()

        # "name (…)" with a space reads as prose unless the name itself is a tool:
        # only a tight "name(" counts as a call on the strength of its kwargs.
        spaced = m.group(0)[len(name):-1] != ""
        is_target = (
            (bool(kwargs) and not spaced)
            or name in registry
            or name in SEC_DISALLOWED_FN_NAMES
            or name.startswith(("dalton_", "knowledge_", "dalton_sec.", "researchdata."))
        )
        if not is_target:
            continue
        line_no = text.count("\n", 0, m.start()) + 1
        calls.append(
            {
                "tool_name": name,
                "kwargs": kwargs,
                "kwarg_values": kwarg_values,
                "line_no": line_no,
                "raw": f"{name}({arg_str.strip()})",
            }
        )
    return calls


def _validate_researchdata_bridge_call(
    rel_path: str,
    call: dict[str, Any],
) -> list[tuple[str, int, str, str]]:
    """Validate `researchdata.*` calls (including nested `params`) via `researchdata_bridge`."""
    if str(BRIDGE_DIR) not in sys.path:
        sys.path.insert(0, str(BRIDGE_DIR))
    import researchdata_bridge as rb

    name = call["tool_name"]
    line_no = call["line_no"]
    tool_short = name.split(".", 1)[1] if name.startswith("researchdata.") else name
    if tool_short not in ("research", "finance", "macro", "community"):
        return []

    kwarg_values: dict[str, str] = call.get("kwarg_values") or {}
    route_expr = (
        kwarg_values.get("route")
        or kwarg_values.get("source")
        or kwarg_values.get("channel")
        or kwarg_values.get("provider")
        or ""
    )
    routes = _extract_quoted_literals(route_expr)
    if not routes:
        return []

    op_expr = kwarg_values.get("operation") or kwarg_values.get("action") or ""
    ops = _extract_quoted_literals(op_expr)

    nested_keys: list[str] | None = None
    if "params" in kwarg_values:
        nested_keys = _extract_dict_literal_keys(kwarg_values["params"])

    errors: list[tuple[str, int, str, str]] = []
    for route in routes:
        spec = rb.ROUTE_CONTRACT_SPECS.get(route)
        if spec is None:
            errors.append(
                (
                    rel_path,
                    line_no,
                    name,
                    f"Unknown researchdata route {route!r} in {call['raw']}",
                )
            )
            continue
        if spec.get("tool") != tool_short:
            errors.append(
                (
                    rel_path,
                    line_no,
                    name,
                    f"Route {route!r} belongs to tool {spec.get('tool')!r}, not {tool_short!r}, in {call['raw']}",
                )
            )
            continue

        if nested_keys is not None:
            accepted_nested = set(spec.get("accepted_nested_params") or [])
            bad_nested = sorted(k for k in nested_keys if k not in accepted_nested)
            if bad_nested:
                errors.append(
                    (
                        rel_path,
                        line_no,
                        name,
                        f"Unknown nested params {bad_nested} for route={route!r} "
                        f"(accepted_nested_params: {sorted(accepted_nested)}) in {call['raw']}",
                    )
                )

        eff_ops = ops or [str(spec.get("default_operation") or "")]
        for op in eff_ops:
            synth_args: dict[str, Any] = {"route": route}
            if op:
                synth_args["operation"] = op
            for k in call["kwargs"]:
                if k in ("route", "source", "channel", "provider", "operation", "action", "params"):
                    continue
                lits = _extract_quoted_literals(kwarg_values.get(k, ""))
                synth_args[k] = lits[0] if lits else ("2026-01-01" if k in ("start", "end", "since", "until") else "META")
            if nested_keys is not None:
                synth_args["params"] = {k: "1" for k in nested_keys}
            # Populate route-level required fields when omitted in prose shorthand so
            # `_normalize_and_validate_call_args` reaches top-level, operation, and nested `params` checks.
            canon_present = {rb.PARAM_ALIAS_TABLE.get(k, k) for k in synth_args}
            # Fill the operation's own required fields when the route declares
            # them; filling every accepted field would hand e.g. cn-hk filings
            # a query it refuses.
            canon_op = (spec.get("operation_aliases") or {}).get(op, op)
            op_required = (spec.get("required_params") or {}).get(canon_op)
            fill_keys = (
                tuple(op_required)
                if op_required is not None
                else ("ticker", "query", "start", "end", "concept", "item", "series", "dataset")
            )
            for req_k in fill_keys:
                if req_k in (spec.get("accepted_params") or []) and req_k not in canon_present:
                    synth_args[req_k] = "2026-01-01" if req_k in ("start", "end") else "META"

            _, _, bridge_err = rb._normalize_and_validate_call_args(tool_short, synth_args)
            if bridge_err is not None:
                errors.append(
                    (
                        rel_path,
                        line_no,
                        name,
                        f"Bridge rejected call for route={route!r}, operation={op!r}: "
                        f"{bridge_err.get('error')} in {call['raw']}",
                    )
                )
            elif op == "list_filters" and route == "alphaengine" and "params" in kwarg_values:
                errors.append(
                    (
                        rel_path,
                        line_no,
                        name,
                        f"alphaengine operation='list_filters' takes no params, got params={kwarg_values['params']} in {call['raw']}",
                    )
                )
    return errors


def validate_extracted_calls(
    rel_path: str,
    calls: list[dict[str, Any]],
    registry: dict[str, set[str]],
) -> list[tuple[str, int, str, str]]:
    """Validate extracted calls against `registry`; return `(rel_path, line_no, tool_name, message)` errors."""
    errors: list[tuple[str, int, str, str]] = []
    for call in calls:
        name = call["tool_name"]
        line_no = call["line_no"]
        kwargs = call["kwargs"]
        if name not in registry:
            errors.append(
                (rel_path, line_no, name, f"Unknown tool {name!r} in {call['raw']}")
            )
            continue
        allowed = registry[name]
        bad_kwargs = [k for k in kwargs if k not in allowed]
        if bad_kwargs:
            errors.append(
                (
                    rel_path,
                    line_no,
                    name,
                    f"Unknown keyword argument(s) {bad_kwargs} for {name!r} "
                    f"(allowed: {sorted(allowed)}) in {call['raw']}",
                )
            )
            continue
        errors.extend(_validate_researchdata_bridge_call(rel_path, call))
    return errors


def test_tool_registry_loads_dalton_direct_and_sec_mcp_signatures() -> None:
    registry = build_tool_signature_registry()
    for expected_direct in (
        "knowledge_update",
        "knowledge_append_verdict",
        "check_draft",
        "publish_report",
        "conversation_search",
        "dalton_check_done",
        "dalton_coverage_board",
        "dalton_record_plan",
        "dalton_agenda_note",
        "dalton_schedule_automation",
        "dalton_coverage_tracking",
        "dalton_system_jobs",
    ):
        assert expected_direct in registry, f"Missing Dalton direct tool {expected_direct}"

    assert {"dispatches", "skipped", "pm_attention", "plan_date"} <= registry["dalton_record_plan"]
    assert "tier" not in registry["dalton_coverage_tracking"]
    assert {"verdict", "agenda_ops"} <= registry["knowledge_append_verdict"]
    assert "trigger" not in registry["knowledge_append_verdict"]

    # Only `@mcp.tool()` functions with `dalton_sec.` prefix are registered
    assert "dalton_sec.get_company_facts" in registry
    assert {
        "ticker",
        "concepts",
        "period",
        "as_of",
        "restatement",
        "fiscal_year_end_month",
    } == registry["dalton_sec.get_company_facts"]
    assert "dalton_sec.list_filings" in registry
    assert {"ticker", "forms", "limit", "as_of"} == registry["dalton_sec.list_filings"]
    for disallowed_sec in (
        "get_company_facts",
        "list_filings",
        "resolve_cik",
        "dalton_sec.resolve_cik",
        "fetch_company_facts",
        "dalton_sec.fetch_company_facts",
    ):
        assert disallowed_sec not in registry

    # Each of the 5 researchdata tools has its own inputSchema parameters, not a shared union
    assert registry["researchdata.catalog"] == {"category", "route", "source", "channel", "provider"}
    assert "form" in registry["researchdata.finance"]
    assert "form" not in registry["researchdata.research"]
    assert "series" in registry["researchdata.macro"]
    assert "series" not in registry["researchdata.finance"]
    assert "pages" in registry["researchdata.community"]
    assert "pages" not in registry["researchdata.research"]


def test_skill_markdown_tool_calls_match_signatures() -> None:
    registry = build_tool_signature_registry()
    md_files = sorted(SKILLS_ROOT.rglob("*.md"))
    assert md_files, f"No skill markdown files found under {SKILLS_ROOT}"

    for (rel_path, _, _), meta in KNOWN_BATCH2_EXCEPTIONS.items():
        skill_dir = rel_path.split("/", 1)[0]
        assert skill_dir in BATCH2_SKILL_DIRS, (
            f"Exception {rel_path} is not in a Batch 2 skill ({BATCH2_SKILL_DIRS})"
        )
        assert meta.get("reason") == "待第 2 批修复"

    unexpected_errors: list[str] = []
    seen_exceptions: set[tuple[str, int, str]] = set()
    total_calls = 0

    for md_path in md_files:
        rel_path = md_path.relative_to(SKILLS_ROOT).as_posix()
        text = md_path.read_text(encoding="utf-8")
        calls = extract_tool_calls_from_markdown(text, registry)
        total_calls += len(calls)
        for err_path, line_no, tool_name, msg in validate_extracted_calls(
            rel_path, calls, registry
        ):
            key = (err_path, line_no, tool_name)
            if key in KNOWN_BATCH2_EXCEPTIONS:
                seen_exceptions.add(key)
            else:
                unexpected_errors.append(f"{err_path}:{line_no}: {msg}")

    print(
        f"\n[skill-tool-call-guard] Scanned {total_calls} tool calls across {len(md_files)} skill markdown files "
        f"(known_batch2_exceptions={len(seen_exceptions)})"
    )
    assert total_calls >= 200, f"Expected to scan >=200 tool calls, got {total_calls}"
    assert not unexpected_errors, "Skill tool-call signature mismatches:\n" + "\n".join(
        unexpected_errors
    )
    stale_exceptions = set(KNOWN_BATCH2_EXCEPTIONS.keys()) - seen_exceptions
    assert not stale_exceptions, f"Stale entries in KNOWN_BATCH2_EXCEPTIONS: {stale_exceptions}"


def test_scanner_rejects_bad_tool_name_and_bad_kwarg() -> None:
    registry = build_tool_signature_registry()
    sample = """
    1. `dalton_coverage_tracking(action="enable", workspace_id=..., ticker=..., tier="Core")`
    2. `dalton_sec.get_company_facts(ticker=..., concepts=..., periods=..., forms=["10-K"])`
    3. `knowledge_append_verdict(trigger="pm_question")`
    4. `dalton_nonexistent_tool(foo=1, ...)`
    5. `knowledge_update(doc="agenda", ...)`
    """
    calls = extract_tool_calls_from_markdown(sample, registry)
    errors = validate_extracted_calls("synthetic/SKILL.md", calls, registry)
    assert len(errors) == 4
    assert ("synthetic/SKILL.md", 2, "dalton_coverage_tracking") == errors[0][:3]
    assert "tier" in errors[0][3]
    assert ("synthetic/SKILL.md", 3, "dalton_sec.get_company_facts") == errors[1][:3]
    assert "periods" in errors[1][3] and "forms" in errors[1][3]
    assert ("synthetic/SKILL.md", 4, "knowledge_append_verdict") == errors[2][:3]
    assert "trigger" in errors[2][3]
    assert ("synthetic/SKILL.md", 5, "dalton_nonexistent_tool") == errors[3][:3]
    assert "Unknown tool" in errors[3][3]


def test_scanner_rejects_sec_helper_functions_and_unprefixed_sec_tools() -> None:
    registry = build_tool_signature_registry()
    sample = """
    1. `resolve_cik("AAPL")`
    2. `dalton_sec.fetch_company_facts(cik="0000320193")`
    3. `get_company_facts(ticker="AAPL", concepts=["Revenues"])`
    """
    calls = extract_tool_calls_from_markdown(sample, registry)
    errors = validate_extracted_calls("synthetic/SKILL.md", calls, registry)
    assert [e[:3] for e in errors] == [
        ("synthetic/SKILL.md", 2, "resolve_cik"),
        ("synthetic/SKILL.md", 3, "dalton_sec.fetch_company_facts"),
        ("synthetic/SKILL.md", 4, "get_company_facts"),
    ]
    assert all("Unknown tool" in e[3] for e in errors)


def test_scanner_rejects_cross_tool_researchdata_exclusive_params() -> None:
    registry = build_tool_signature_registry()
    sample = """
    1. `researchdata.research(route="alphaengine", operation="search_library", query="META", form="10-K")`
    2. `researchdata.finance(route="sec", operation="financials", ticker="META", pages=2)`
    3. `researchdata.macro(route="fred", operation="macro", series="DGS10", ticker="META")`
    4. `researchdata.catalog(route="sec", query="filings")`
    """
    calls = extract_tool_calls_from_markdown(sample, registry)
    errors = validate_extracted_calls("synthetic/SKILL.md", calls, registry)
    assert len(errors) == 4
    assert ("synthetic/SKILL.md", 2, "researchdata.research") == errors[0][:3]
    assert "['form']" in errors[0][3]
    assert ("synthetic/SKILL.md", 3, "researchdata.finance") == errors[1][:3]
    assert "['pages']" in errors[1][3]
    assert ("synthetic/SKILL.md", 4, "researchdata.macro") == errors[2][:3]
    assert "['ticker']" in errors[2][3]
    assert ("synthetic/SKILL.md", 5, "researchdata.catalog") == errors[3][:3]
    assert "['query']" in errors[3][3]


def test_scanner_catches_camelcase_kwarg_and_parens_inside_quoted_strings() -> None:
    registry = build_tool_signature_registry()
    sample = """
    1. `dalton_coverage_tracking(action="enable", badArg=1)`
    2. `dalton_coverage_tracking(action="enable", source=")", bad_arg=1)`
    3. `dalton_coverage_tracking(action="enable", source="escaped \\") paren", AnotherBad=2)`
    """
    calls = extract_tool_calls_from_markdown(sample, registry)
    errors = validate_extracted_calls("synthetic/SKILL.md", calls, registry)
    assert len(errors) == 3
    assert ("synthetic/SKILL.md", 2, "dalton_coverage_tracking") == errors[0][:3]
    assert "['badArg']" in errors[0][3]
    assert ("synthetic/SKILL.md", 3, "dalton_coverage_tracking") == errors[1][:3]
    assert "['bad_arg']" in errors[1][3]
    assert ("synthetic/SKILL.md", 4, "dalton_coverage_tracking") == errors[2][:3]
    assert "['AnotherBad']" in errors[2][3]


def test_a_space_before_the_paren_does_not_hide_a_tool_call():
    registry = build_tool_signature_registry()
    calls = extract_tool_calls_from_markdown(
        '`dalton_sec.fetch_company_facts (ticker="X")` and `dalton_coverage_tracking (action="enable", badArg=1)`',
        registry,
    )
    errors = validate_extracted_calls("x.md", calls, registry)
    assert any("fetch_company_facts" in e[3] for e in errors)
    assert any("badArg" in e[3] for e in errors)


def test_prose_with_a_space_before_the_paren_is_not_a_call():
    registry = build_tool_signature_registry()
    assert extract_tool_calls_from_markdown("汇率取 yfinance (CNYUSD=X) 的收盘价", registry) == []


def test_researchdata_schema_aliases_are_legal():
    registry = build_tool_signature_registry()
    calls = extract_tool_calls_from_markdown('`finance(source="sec", symbol="META")`', registry)
    assert validate_extracted_calls("x.md", calls, registry) == []


def test_scanner_rejects_invalid_nested_bridge_params():
    registry = build_tool_signature_registry()
    sample = """
    1. `researchdata.research(route="alphaengine", operation="list_filters", params={"filter_type": "document_types", "query": "专家"})`
    2. `researchdata.research(route="guidepoint", operation="search_library", query="META", params={"bad_nested": 1})`
    3. `researchdata.research(route="alphaengine", operation="search_library", query="META", params={"type_ids": [1], "sort": "relevance"})`
    """
    calls = extract_tool_calls_from_markdown(sample, registry)
    errors = validate_extracted_calls("synthetic/SKILL.md", calls, registry)
    assert any(e[1] == 2 and "filter_type" in e[3] for e in errors)
    assert any(e[1] == 3 and "bad_nested" in e[3] for e in errors)
    assert not any(e[1] == 4 for e in errors)

