"""Guard tests for ``src/dalton/settings_registry.yaml``.

Checks that user-configurable settings discovered from:
- frontend preference / thread Zod schemas (including nested sub-schema field paths),
  TypeScript preference interfaces, ``PROFILE_FIELDS``, ``ThreadField``, and
  ``other_preference`` / ``model_preference`` read/write sites under ``web/src/``;
- desktop local notification toggles in ``desktop/src/desktopSettings.js``;
- backend ``UserUpdate``, ``update_user``, ``UserPreferencesUpdate`` (including
  ``ModelPreference.model_fields``), and ``user_preferences`` JSON read/write/merge
  paths in ``src/``;
- thread writable models (``ThreadUpdateRequest``, ``ThreadVisibilityRequest``,
  ``ThreadShareRequest``, ``SharePermissions``);
- automation writable models (``AutomationCreate``, ``AutomationUpdate``,
  ``CompanyScriptUpdate``, ``DeliveryConfig``, and independently validated
  sub-models such as ``PriceTriggerConfig``), ``.agents/user/automations/*.json``
  editable fields, and pause/resume endpoints;
- workspace (including ``WorkspaceTrackingUpdateRequest``), computer browser,
  and self-hosted ``deployment_settings`` catalogs;
- Dalton ``PUT``/``PATCH`` routes mounted on ``src.server.app.setup.app`` across
  every :class:`DeploymentProfile`, including recursive Pydantic request body
  fields from both route signatures and in-handler ``model_validate()`` calls;

are registered in ``src/dalton/settings_registry.yaml`` with an explicit Chief
of Staff decision (``tool:<name>``, ``missing``, or ``excluded:<reason>``).
"""

from __future__ import annotations

import ast
import importlib
import inspect
import os
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args, get_origin

import pytest
import yaml
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel

from src.dalton.capabilities import CapabilityId
from src.dalton.profile import DeploymentProfile

REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTRY_PATH = REPO_ROOT / "src" / "dalton" / "settings_registry.yaml"
WEB_SRC = REPO_ROOT / "web" / "src"
DESKTOP_SRC = REPO_ROOT / "desktop" / "src"
PY_SRC = REPO_ROOT / "src"

FAILURE_GUIDANCE = (
    "请在 settings_registry.yaml 登记，并决定研究总监能否修改"
    "（tool:<工具名> / missing / excluded:<原因>）。"
)

# Preference JSONB column names on ``user_preferences``.
_PREFERENCE_SECTIONS: tuple[str, ...] = (
    "risk_preference",
    "investment_preference",
    "agent_preference",
    "other_preference",
    "model_preference",
)

# Transient wire-control fields on ``UserPreferencesUpdate`` that are never stored.
_PREF_UPDATE_TRANSIENT_FIELDS: frozenset[str] = frozenset(
    {
        "apply_default_to",
        "expect_other_preference",
    }
)

# Non-setting wire-control or concurrency tokens on automation request models.
_AUTOMATION_NON_SETTING_FIELDS: frozenset[str] = frozenset(
    {
        "report_delivery_confirmation_token",  # one-time web confirmation credential (exclude=True)
        "expected_config_version",  # optimistic concurrency version token
        "delivery_config_raw",  # internal raw copy derived from delivery_config (exclude=True)
    }
)

# Non-user-setting parameters on ``src.server.database.user.update_user``.
_USER_UPDATE_INTERNAL_PARAMS: frozenset[str] = frozenset(
    {
        "user_id",  # target primary key
        "last_login_at",  # server auth timestamp
        "auth_provider",  # auth provider backfill from identity token
    }
)

# Dalton PUT/PATCH routes that are pure binary transport or internal service-to-service
# state callbacks rather than user-configurable settings.
NON_SETTING_DALTON_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        # Multipart binary upload part transfer.
        ("PUT", "/api/v1/dalton/chat-uploads/{upload_id}/parts/{part_number}"),
        # Internal gateway QR login state machine callback (X-Service-Token only).
        (
            "PATCH",
            "/api/v1/dalton/channels/internal/weixin/login-sessions/{session_key}",
        ),
        # Internal data-job monitor dataset ingestion (X-Service-Token only).
        ("PUT", "/api/v1/dalton/monitors/internal/{slug}/data/{key}"),
    }
)

# Pure protocol / CAS / target-identity fields on Dalton PUT/PATCH Pydantic body models
# that are wire-level concurrency or audit-comment tokens rather than stored settings.
_DALTON_ROUTE_PROTOCOL_FIELDS: dict[tuple[str, str], frozenset[str]] = {
    ("PUT", "/api/v1/dalton/channels/settings"): frozenset({"user_id", "version"}),
    ("PUT", "/api/v1/dalton/channels/internal/settings"): frozenset(
        {"user_id", "version"}
    ),
    ("PUT", "/api/v1/dalton/watchlist/tier-policies/{tier}"): frozenset(
        {"snapshot_id", "expected_policy_version", "expected_roster_hash"}
    ),
    ("PUT", "/api/v1/dalton/team-models/{model_key}"): frozenset(
        {"expected_revision", "comment"}
    ),
    ("PUT", "/api/v1/dalton/team-models/provider-variants/{provider_slug}"): frozenset(
        {"expected_revision", "comment"}
    ),
    ("PUT", "/api/v1/dalton/team-models/curated"): frozenset(
        {"expected_revision", "comment"}
    ),
    ("PUT", "/api/v1/dalton/deployment-settings"): frozenset({"expected_revision"}),
}

# Compare-and-set tokens on ThreadUpdateRequest: the client sends the value it last
# saw so a background autosave cannot overwrite a newer pick; they are not settings.
_THREAD_NON_SETTING_FIELDS: frozenset[str] = frozenset(
    {"expected_llm_model", "expected_reasoning_effort", "expected_fast_mode"}
)

# Virtual JSON/Markdown file prefixes editable by Chief of Staff through file tools.
_ALLOWED_VIRTUAL_FILE_PREFIXES: tuple[str, ...] = (
    ".agents/user/profile/",
    ".agents/user/automations/",
    ".agents/user/channels/",
    ".agents/user/agents/",
)

# Frontend Zod files that validate preference or thread settings and how each maps.
# Any newly added Zod file under ``web/src/`` referencing preference/thread storage
# must be handled here so new preference schemas cannot escape the guard.
_KNOWN_FRONTEND_PREF_ZOD_FILES: dict[str, str] = {
    "lib/modelPreferences.ts": "model_preference_schemas",
    "pages/Onboarding/welcome/profile.ts": "profile_fields",
    "pages/Onboarding/onboardingPrefsSchema.ts": "other_preference_subschema:onboarding",
    "lib/dashboards/saved.ts": "other_preference_subschema:custom_dashboards",
    "lib/appBridge/useAppScopes.ts": "other_preference_subschema:app_scopes",
}


# ---------------------------------------------------------------------------
# Registry loading & assertions
# ---------------------------------------------------------------------------


def _load_registry() -> list[dict[str, Any]]:
    assert REGISTRY_PATH.is_file(), f"Missing settings registry: {REGISTRY_PATH}"
    raw = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        items = raw.get("settings")
    else:
        items = raw
    assert isinstance(items, list) and items, (
        "settings_registry.yaml must contain a non-empty 'settings' list"
    )
    return items


def _registry_keys(items: list[dict[str, Any]] | None = None) -> set[str]:
    entries = _load_registry() if items is None else items
    return {str(item["key"]) for item in entries}


def _assert_all_registered(
    discovered: set[str],
    *,
    source: str,
    registered_keys: set[str] | None = None,
) -> None:
    known = _registry_keys() if registered_keys is None else registered_keys
    missing = sorted(discovered - known)
    assert not missing, (
        f"发现未登记的设置项（来源：{source}）：{missing}\n{FAILURE_GUIDANCE}"
    )


# ---------------------------------------------------------------------------
# TypeScript lexical scanner (handles comments, strings, templates, regexes)
# ---------------------------------------------------------------------------

_REGEX_PRECEDING_KEYWORDS: frozenset[str] = frozenset(
    {
        "return",
        "case",
        "throw",
        "in",
        "of",
        "typeof",
        "void",
        "delete",
        "new",
        "else",
        "do",
        "yield",
        "await",
    }
)

_REGEX_PRECEDING_PUNCTS: frozenset[str] = frozenset(
    {
        "(",
        "[",
        "{",
        ",",
        ";",
        ":",
        "=",
        "!",
        "?",
        "&",
        "|",
        "^",
        "~",
        "+",
        "-",
        "*",
        "/",
        "%",
        "<",
        ">",
        "=>",
    }
)


@dataclass(frozen=True)
class _TsToken:
    kind: str  # "ident" | "string" | "number" | "regex" | "punct"
    value: str
    line: int = 0


def _can_start_regex(prev: _TsToken | None) -> bool:
    if prev is None:
        return True
    if prev.kind == "punct" and prev.value in _REGEX_PRECEDING_PUNCTS:
        return True
    return prev.kind == "ident" and prev.value in _REGEX_PRECEDING_KEYWORDS


def _tokenize_ts(source: str) -> list[_TsToken]:
    """Tokenize TypeScript source, stripping comments while handling strings,
    template literals (including ``${...}`` interpolation), and regex literals."""
    tokens: list[_TsToken] = []
    i = 0
    n = len(source)
    line = 1
    # Stack of open brace depths for template literal `${ ... }` interpolations.
    template_brace_stack: list[int] = []
    brace_depth = 0

    while i < n:
        ch = source[i]
        if ch == "\n":
            line += 1
            i += 1
            continue
        if ch.isspace():
            i += 1
            continue
        # Line comment
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            nl = source.find("\n", i + 2)
            i = n if nl == -1 else nl
            continue
        # Block comment
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            end = source.find("*/", i + 2)
            if end == -1:
                line += source[i:].count("\n")
                i = n
            else:
                line += source[i : end + 2].count("\n")
                i = end + 2
            continue
        # Regex literal (when `/` cannot be division)
        if ch == "/" and _can_start_regex(tokens[-1] if tokens else None):
            start_line = line
            i += 1
            buf: list[str] = ["/"]
            in_class = False
            closed = False
            while i < n:
                c = source[i]
                if c == "\n":
                    break
                if c == "\\" and i + 1 < n:
                    buf.append(c)
                    buf.append(source[i + 1])
                    i += 2
                    continue
                if c == "[":
                    in_class = True
                elif c == "]" and in_class:
                    in_class = False
                elif c == "/" and not in_class:
                    buf.append(c)
                    i += 1
                    closed = True
                    break
                buf.append(c)
                i += 1
            if closed:
                while i < n and source[i].isalpha():
                    buf.append(source[i])
                    i += 1
                tokens.append(_TsToken("regex", "".join(buf), start_line))
                continue
            # Fallback if not a closed regex literal
            tokens.append(_TsToken("punct", "/", start_line))
            continue
        # Quoted string literal
        if ch in ("'", '"'):
            quote = ch
            start_line = line
            i += 1
            buf = []
            while i < n:
                c = source[i]
                if c == "\n":
                    line += 1
                if c == "\\" and i + 1 < n:
                    if source[i + 1] == "\n":
                        line += 1
                    buf.append(source[i + 1])
                    i += 2
                    continue
                if c == quote:
                    i += 1
                    break
                buf.append(c)
                i += 1
            tokens.append(_TsToken("string", "".join(buf), start_line))
            continue
        # Template literal (supports `${ ... }` nesting)
        if ch == "`":
            start_line = line
            i += 1
            buf = []
            interpolated = False
            while i < n:
                c = source[i]
                if c == "\n":
                    line += 1
                if c == "\\" and i + 1 < n:
                    if source[i + 1] == "\n":
                        line += 1
                    buf.append(source[i + 1])
                    i += 2
                    continue
                if c == "$" and i + 1 < n and source[i + 1] == "{":
                    interpolated = True
                    template_brace_stack.append(brace_depth)
                    brace_depth += 1
                    i += 2
                    break
                if c == "`":
                    i += 1
                    break
                buf.append(c)
                i += 1
            if not interpolated:
                tokens.append(_TsToken("string", "".join(buf), start_line))
            else:
                tokens.append(_TsToken("punct", "${", start_line))
            continue
        # Closing brace of a template `${ ... }` resumes scanning template body
        if (
            ch == "}"
            and template_brace_stack
            and brace_depth - 1 == template_brace_stack[-1]
        ):
            brace_depth -= 1
            template_brace_stack.pop()
            tokens.append(_TsToken("punct", "}", line))
            i += 1
            while i < n:
                c = source[i]
                if c == "\n":
                    line += 1
                if c == "\\" and i + 1 < n:
                    if source[i + 1] == "\n":
                        line += 1
                    i += 2
                    continue
                if c == "$" and i + 1 < n and source[i + 1] == "{":
                    template_brace_stack.append(brace_depth)
                    brace_depth += 1
                    tokens.append(_TsToken("punct", "${", line))
                    i += 2
                    break
                if c == "`":
                    i += 1
                    break
                i += 1
            continue
        # Multi-character operators
        if source.startswith("...", i):
            tokens.append(_TsToken("punct", "...", line))
            i += 3
            continue
        if source.startswith("=>", i):
            tokens.append(_TsToken("punct", "=>", line))
            i += 2
            continue
        if source.startswith("?.", i):
            tokens.append(_TsToken("punct", "?.", line))
            i += 2
            continue
        # Identifier
        if ch.isalpha() or ch in ("_", "$"):
            start = i
            i += 1
            while i < n and (source[i].isalnum() or source[i] in ("_", "$")):
                i += 1
            tokens.append(_TsToken("ident", source[start:i], line))
            continue
        # Number literal
        if ch.isdigit():
            start = i
            i += 1
            while i < n and (source[i].isalnum() or source[i] in ("_", ".")):
                i += 1
            tokens.append(_TsToken("number", source[start:i], line))
            continue
        # Single-char punctuation
        if ch == "{":
            brace_depth += 1
        elif ch == "}":
            brace_depth = max(0, brace_depth - 1)
        tokens.append(_TsToken("punct", ch, line))
        i += 1
    return tokens


def _tok_is(tok: _TsToken, kind: str, value: str | None = None) -> bool:
    return tok.kind == kind and (value is None or tok.value == value)


def _find_matching(
    tokens: list[_TsToken], open_idx: int, open_ch: str, close_ch: str
) -> int:
    depth = 0
    for idx in range(open_idx, len(tokens)):
        tok = tokens[idx]
        if tok.kind == "punct":
            if tok.value == open_ch:
                depth += 1
            elif tok.value == close_ch:
                depth -= 1
                if depth == 0:
                    return idx
    raise AssertionError(
        f"Unclosed {open_ch!r} starting at token index {open_idx} (line {tokens[open_idx].line})"
    )


def _split_top_level(
    tokens: list[_TsToken], start: int, end: int, sep: str = ","
) -> list[tuple[int, int]]:
    """Split ``tokens[start:end]`` by top-level ``sep`` punctuation."""
    parts: list[tuple[int, int]] = []
    cur = start
    depth = 0
    i = start
    while i < end:
        t = tokens[i]
        if t.kind == "punct":
            if t.value in ("{", "[", "(", "${"):
                depth += 1
            elif t.value in ("}", "]", ")"):
                depth = max(0, depth - 1)
            elif t.value == sep and depth == 0:
                if cur < i:
                    parts.append((cur, i))
                cur = i + 1
        i += 1
    if cur < end:
        parts.append((cur, end))
    return parts


def _skip_ts_type_and_find_eq(tokens: list[_TsToken], start: int) -> int | None:
    """From after `const <name>`, skip optional `: Type` and return index after `=`."""
    j = start
    depth = 0
    angle_depth = 0
    while j < len(tokens):
        t = tokens[j]
        if t.kind == "punct":
            if t.value in ("{", "[", "("):
                depth += 1
            elif t.value in ("}", "]", ")"):
                depth = max(0, depth - 1)
            elif t.value == "<" and depth == 0:
                angle_depth += 1
            elif t.value == ">" and depth == 0 and angle_depth > 0:
                angle_depth -= 1
            elif t.value == "=" and depth == 0 and angle_depth == 0:
                return j + 1
            elif t.value == ";" and depth == 0:
                return None
        j += 1
    return None


def _statement_end(tokens: list[_TsToken], start: int) -> int:
    """Find end token index of a top-level TS initializer expression."""
    depth = 0
    i = start
    while i < len(tokens):
        t = tokens[i]
        if t.kind == "punct":
            if t.value in ("{", "[", "(", "${"):
                depth += 1
            elif t.value in ("}", "]", ")"):
                if depth == 0:
                    return i
                depth -= 1
            elif t.value == ";" and depth == 0:
                return i
        elif (
            depth == 0
            and i > start
            and t.line > tokens[i - 1].line
            and t.kind == "ident"
            and t.value in ("export", "const", "let", "var", "function", "type", "interface", "class")
        ):
            # Automatic Semicolon Insertion boundary
            prev = tokens[i - 1]
            if prev.kind != "punct" or prev.value in (")", "}", "]"):
                return i
        i += 1
    return len(tokens)


def _collect_ts_const_inits(
    tokens: list[_TsToken],
) -> dict[str, tuple[int, int]]:
    """Map top-level/module ``const NAME = <expr>`` to ``(expr_start, expr_end)``."""
    inits: dict[str, tuple[int, int]] = {}
    depth = 0
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.kind == "punct":
            if t.value in ("{", "[", "(", "${"):
                depth += 1
            elif t.value in ("}", "]", ")"):
                depth = max(0, depth - 1)
            i += 1
            continue
        if (
            depth == 0
            and t.kind == "ident"
            and t.value in ("const", "let", "var")
            and i + 1 < len(tokens)
            and tokens[i + 1].kind == "ident"
        ):
            name = tokens[i + 1].value
            eq_next = _skip_ts_type_and_find_eq(tokens, i + 2)
            if eq_next is not None:
                end = _statement_end(tokens, eq_next)
                inits[name] = (eq_next, end)
                i = end
                continue
        i += 1
    return inits


def _extract_string_consts_from_inits(
    tokens: list[_TsToken], inits: dict[str, tuple[int, int]]
) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, (start, end) in inits.items():
        # Handle `'literal'` or `'literal' as const`
        if start < end and tokens[start].kind == "string":
            rest = tokens[start + 1 : end]
            if not rest or (
                len(rest) == 2
                and _tok_is(rest[0], "ident", "as")
                and _tok_is(rest[1], "ident", "const")
            ):
                out[name] = tokens[start].value
    return out


def _collect_web_exported_string_consts() -> dict[str, str]:
    """Collect ``export const NAME = 'literal'`` bindings across ``web/src/``."""
    out: dict[str, str] = {}
    for ts_file in sorted(WEB_SRC.rglob("*.ts*")):
        if "__tests__" in ts_file.parts or ".test." in ts_file.name:
            continue
        text = ts_file.read_text(encoding="utf-8")
        if "export const " not in text:
            continue
        tokens = _tokenize_ts(text)
        inits = _collect_ts_const_inits(tokens)
        out.update(_extract_string_consts_from_inits(tokens, inits))
    return out


def _unwrap_parens_and_casts(
    tokens: list[_TsToken], start: int, end: int
) -> tuple[int, int]:
    """Strip outer parentheses and trailing `as ...` / `satisfies ...` type assertions."""
    while start < end:
        if _tok_is(tokens[start], "punct", "("):
            close = _find_matching(tokens, start, "(", ")")
            if close == end - 1:
                start += 1
                end -= 1
                continue
        break
    # Strip trailing top-level `as Type` or `satisfies Type`
    depth = 0
    for idx in range(start, end):
        t = tokens[idx]
        if t.kind == "punct":
            if t.value in ("{", "[", "(", "${"):
                depth += 1
            elif t.value in ("}", "]", ")"):
                depth = max(0, depth - 1)
        elif depth == 0 and t.kind == "ident" and t.value in ("as", "satisfies"):
            end = idx
            break
    while start < end and _tok_is(tokens[start], "punct", "("):
        close = _find_matching(tokens, start, "(", ")")
        if close == end - 1:
            start += 1
            end -= 1
        else:
            break
    return start, end


def _resolve_ts_string_expr(
    tokens: list[_TsToken],
    start: int,
    end: int,
    string_consts: dict[str, str],
) -> str | None:
    start, end = _unwrap_parens_and_casts(tokens, start, end)
    if start + 1 == end:
        tok = tokens[start]
        if tok.kind == "string":
            return tok.value
        if tok.kind == "ident" and tok.value in string_consts:
            return string_consts[tok.value]
    return None


def _find_top_level_ternary(
    tokens: list[_TsToken], start: int, end: int
) -> tuple[int, int] | None:
    """Return ``(q_idx, colon_idx)`` if ``tokens[start:end]`` is a top-level ternary."""
    depth = 0
    q_idx: int | None = None
    ternary_nest = 0
    for i in range(start, end):
        t = tokens[i]
        if t.kind == "punct":
            if t.value in ("{", "[", "(", "${"):
                depth += 1
            elif t.value in ("}", "]", ")"):
                depth = max(0, depth - 1)
            elif depth == 0:
                if t.value == "?":
                    if q_idx is None:
                        q_idx = i
                    else:
                        ternary_nest += 1
                elif t.value == ":" and q_idx is not None:
                    if ternary_nest == 0:
                        return q_idx, i
                    ternary_nest -= 1
    return None


def _extract_object_literal_entries(
    tokens: list[_TsToken],
    brace_idx: int,
    *,
    inits: dict[str, tuple[int, int]],
    string_consts: dict[str, str],
    strict: bool,
    seen_consts: frozenset[str] = frozenset(),
) -> list[tuple[str, tuple[int, int] | None]]:
    """Extract ``(key, value_token_range | None)`` from a TS object literal ``{ ... }``.

    Handles literal keys, computed keys ``[CONST]``, shorthand properties, spreads
    ``...OtherObj`` / ``...OtherSchema.shape``, and ternary spreads
    ``...(cond ? { a: 1 } : {})``. When ``strict=True``, raises ``AssertionError``
    on any unresolvable spread or computed property key.
    """
    end_idx = _find_matching(tokens, brace_idx, "{", "}")
    entries: list[tuple[str, tuple[int, int] | None]] = []

    for part_start, part_end in _split_top_level(tokens, brace_idx + 1, end_idx, ","):
        if part_start >= part_end:
            continue
        first = tokens[part_start]

        # Spread: `...expr`
        if _tok_is(first, "punct", "..."):
            expr_start, expr_end = _unwrap_parens_and_casts(
                tokens, part_start + 1, part_end
            )
            # Conditional spread: `...(cond ? { ... } : { ... })`
            tern = _find_top_level_ternary(tokens, expr_start, expr_end)
            if tern is not None:
                q_idx, colon_idx = tern
                for b_start, b_end in ((q_idx + 1, colon_idx), (colon_idx + 1, expr_end)):
                    bs, be = _unwrap_parens_and_casts(tokens, b_start, b_end)
                    if bs < be and _tok_is(tokens[bs], "punct", "{"):
                        entries.extend(
                            _extract_object_literal_entries(
                                tokens,
                                bs,
                                inits=inits,
                                string_consts=string_consts,
                                strict=strict,
                                seen_consts=seen_consts,
                            )
                        )
                    elif strict:
                        snippet = " ".join(t.value for t in tokens[bs:be])
                        raise AssertionError(
                            f"无法静态解析对象展开的条件分支：{snippet!r}（line {first.line}）"
                        )
                continue

            # Inline object literal spread: `...{ a: ... }`
            if expr_start < expr_end and _tok_is(tokens[expr_start], "punct", "{"):
                entries.extend(
                    _extract_object_literal_entries(
                        tokens,
                        expr_start,
                        inits=inits,
                        string_consts=string_consts,
                        strict=strict,
                        seen_consts=seen_consts,
                    )
                )
                continue

            # Identifier spread: `...ExtraFields`
            if expr_start + 1 == expr_end and tokens[expr_start].kind == "ident":
                ref_name = tokens[expr_start].value
                if ref_name in inits and ref_name not in seen_consts:
                    rs, re_ = _unwrap_parens_and_casts(tokens, *inits[ref_name])
                    if rs < re_ and _tok_is(tokens[rs], "punct", "{"):
                        entries.extend(
                            _extract_object_literal_entries(
                                tokens,
                                rs,
                                inits=inits,
                                string_consts=string_consts,
                                strict=strict,
                                seen_consts=seen_consts | {ref_name},
                            )
                        )
                        continue
                if strict:
                    raise AssertionError(
                        f"无法静态解析 Zod/对象展开引用 `...{ref_name}`（line {first.line}）"
                    )
                continue

            # Schema shape spread: `...BaseSchema.shape`
            if (
                expr_start + 3 == expr_end
                and tokens[expr_start].kind == "ident"
                and _tok_is(tokens[expr_start + 1], "punct", ".")
                and _tok_is(tokens[expr_start + 2], "ident", "shape")
            ):
                ref_name = tokens[expr_start].value
                if ref_name in inits and ref_name not in seen_consts:
                    shape_fields = _extract_zod_expr_fields(
                        tokens,
                        *inits[ref_name],
                        inits=inits,
                        string_consts=string_consts,
                        recursive=False,
                        seen_consts=seen_consts | {ref_name},
                    )
                    if shape_fields is not None:
                        for f in sorted(shape_fields):
                            entries.append((f, None))
                        continue
                if strict:
                    raise AssertionError(
                        f"无法静态解析 Zod 展开引用 `...{ref_name}.shape`（line {first.line}）"
                    )
                continue

            if strict:
                snippet = " ".join(t.value for t in tokens[expr_start:expr_end])
                raise AssertionError(
                    f"无法静态解析的对象展开结构：`...{snippet}`（line {first.line}）"
                )
            continue

        # Computed property key: `[EXPR]: value`
        if _tok_is(first, "punct", "["):
            close_br = _find_matching(tokens, part_start, "[", "]")
            if close_br + 1 < part_end and _tok_is(tokens[close_br + 1], "punct", ":"):
                resolved = _resolve_ts_string_expr(
                    tokens, part_start + 1, close_br, string_consts
                )
                if resolved is not None:
                    entries.append((resolved, (close_br + 2, part_end)))
                    continue
                if strict:
                    snippet = " ".join(
                        t.value for t in tokens[part_start + 1 : close_br]
                    )
                    raise AssertionError(
                        f"无法静态解析计算属性键 `[{snippet}]`（line {first.line}）"
                    )
                continue
            if strict:
                raise AssertionError(
                    f"无法静态解析对象字面量成员（line {first.line}）"
                )
            continue

        # Standard property: `ident: value` or `'str': value`
        if (
            first.kind in ("ident", "string")
            and part_start + 1 < part_end
            and _tok_is(tokens[part_start + 1], "punct", ":")
        ):
            entries.append((first.value, (part_start + 2, part_end)))
            continue

        # Shorthand property: `{ foo }`
        if first.kind == "ident" and part_start + 1 == part_end:
            entries.append((first.value, None))
            continue

        if strict:
            snippet = " ".join(t.value for t in tokens[part_start:part_end])
            raise AssertionError(
                f"无法静态解析对象字面量成员：{snippet!r}（line {first.line}）"
            )

    return entries


_ZOD_OBJECT_CTORS: frozenset[str] = frozenset({"object", "looseObject", "strictObject"})
_ZOD_WRAPPER_FNS: frozenset[str] = frozenset(
    {"catch", "optional", "nullable", "pipe", "readonly", "default", "prefault", "array"}
)
_ZOD_PASSTHROUGH_METHODS: frozenset[str] = frozenset(
    {
        "check",
        "refine",
        "superRefine",
        "transform",
        "catch",
        "optional",
        "nullable",
        "nullish",
        "readonly",
        "partial",
        "required",
        "strict",
        "passthrough",
        "strip",
        "default",
        "describe",
        "brand",
        "pipe",
    }
)


def _extract_zod_expr_fields(
    tokens: list[_TsToken],
    start: int,
    end: int,
    *,
    inits: dict[str, tuple[int, int]],
    string_consts: dict[str, str],
    recursive: bool = True,
    seen_consts: frozenset[str] = frozenset(),
) -> set[str] | None:
    """Recursively extract field paths from a Zod object schema expression.

    Returns ``None`` when ``tokens[start:end]`` is NOT an object schema (e.g.
    ``z.string()``, ``z.array(...)``, ``z.record(...)``). Raises ``AssertionError``
    when an object schema uses an unresolvable spread, computed key, or modifier.
    """
    start, end = _unwrap_parens_and_casts(tokens, start, end)
    if start >= end:
        return None

    # Identifier referencing another const in the same module
    if start + 1 == end and tokens[start].kind == "ident":
        ref = tokens[start].value
        if ref in inits and ref not in seen_consts:
            return _extract_zod_expr_fields(
                tokens,
                *inits[ref],
                inits=inits,
                string_consts=string_consts,
                recursive=recursive,
                seen_consts=seen_consts | {ref},
            )
        return None

    # Parse base expression and any chained `.method(...)` suffixes.
    fields: set[str] | None = None
    pos = start

    # Case 1: `z.<fn>(...)`
    if (
        pos + 3 < end
        and _tok_is(tokens[pos], "ident", "z")
        and _tok_is(tokens[pos + 1], "punct", ".")
        and tokens[pos + 2].kind == "ident"
        and _tok_is(tokens[pos + 3], "punct", "(")
    ):
        fn_name = tokens[pos + 2].value
        close_paren = _find_matching(tokens, pos + 3, "(", ")")
        args = _split_top_level(tokens, pos + 4, close_paren, ",")
        if fn_name in _ZOD_OBJECT_CTORS:
            if not args:
                fields = set()
            else:
                a0_s, a0_e = _unwrap_parens_and_casts(tokens, *args[0])
                fields = _fields_from_zod_shape_arg(
                    tokens,
                    a0_s,
                    a0_e,
                    inits=inits,
                    string_consts=string_consts,
                    recursive=recursive,
                    seen_consts=seen_consts,
                )
        elif fn_name in _ZOD_WRAPPER_FNS and args:
            fields = _extract_zod_expr_fields(
                tokens,
                *args[0],
                inits=inits,
                string_consts=string_consts,
                recursive=recursive,
                seen_consts=seen_consts,
            )
        elif fn_name == "intersection" and len(args) >= 2:
            left = _extract_zod_expr_fields(
                tokens,
                *args[0],
                inits=inits,
                string_consts=string_consts,
                recursive=recursive,
                seen_consts=seen_consts,
            )
            right = _extract_zod_expr_fields(
                tokens,
                *args[1],
                inits=inits,
                string_consts=string_consts,
                recursive=recursive,
                seen_consts=seen_consts,
            )
            if left is not None or right is not None:
                fields = (left or set()) | (right or set())
        elif fn_name in ("union", "discriminatedUnion"):
            arr_arg = args[1] if fn_name == "discriminatedUnion" and len(args) >= 2 else (args[0] if args else None)
            if arr_arg is not None:
                as_, ae_ = _unwrap_parens_and_casts(tokens, *arr_arg)
                if as_ < ae_ and _tok_is(tokens[as_], "punct", "["):
                    arr_end = _find_matching(tokens, as_, "[", "]")
                    union_fields: set[str] = set()
                    any_obj = False
                    for item_s, item_e in _split_top_level(tokens, as_ + 1, arr_end, ","):
                        sub = _extract_zod_expr_fields(
                            tokens,
                            item_s,
                            item_e,
                            inits=inits,
                            string_consts=string_consts,
                            recursive=recursive,
                            seen_consts=seen_consts,
                        )
                        if sub is not None:
                            any_obj = True
                            union_fields |= sub
                    if any_obj:
                        fields = union_fields
        pos = close_paren + 1
    # Case 2: `BaseSchema.extend(...)` / `BaseSchema.merge(...)`
    elif tokens[pos].kind == "ident" and tokens[pos].value in inits and tokens[pos].value not in seen_consts:
        ref = tokens[pos].value
        fields = _extract_zod_expr_fields(
            tokens,
            *inits[ref],
            inits=inits,
            string_consts=string_consts,
            recursive=recursive,
            seen_consts=seen_consts | {ref},
        )
        pos += 1
    else:
        return None

    # Walk chained `.method(...)` calls
    while pos < end and _tok_is(tokens[pos], "punct", "."):
        if pos + 2 >= end or tokens[pos + 1].kind != "ident" or not _tok_is(tokens[pos + 2], "punct", "("):
            if fields is not None:
                snippet = " ".join(t.value for t in tokens[pos:end])
                raise AssertionError(f"无法静态解析 Zod 链式表达式：{snippet!r}")
            return None
        method = tokens[pos + 1].value
        close_paren = _find_matching(tokens, pos + 2, "(", ")")
        args = _split_top_level(tokens, pos + 3, close_paren, ",")

        if method == "extend":
            if not args:
                raise AssertionError(f"Zod .extend() 缺少参数（line {tokens[pos].line}）")
            a0_s, a0_e = _unwrap_parens_and_casts(tokens, *args[0])
            extra = _fields_from_zod_shape_arg(
                tokens,
                a0_s,
                a0_e,
                inits=inits,
                string_consts=string_consts,
                recursive=recursive,
                seen_consts=seen_consts,
            )
            fields = (fields or set()) | extra
        elif method in ("merge", "and"):
            if not args:
                raise AssertionError(f"Zod .{method}() 缺少参数（line {tokens[pos].line}）")
            other = _extract_zod_expr_fields(
                tokens,
                *args[0],
                inits=inits,
                string_consts=string_consts,
                recursive=recursive,
                seen_consts=seen_consts,
            )
            if other is None:
                snippet = " ".join(t.value for t in tokens[args[0][0] : args[0][1]])
                raise AssertionError(
                    f"无法静态解析 Zod .{method}({snippet}) 的目标对象模式"
                )
            fields = (fields or set()) | other
        elif method in _ZOD_PASSTHROUGH_METHODS:
            pass
        elif fields is not None:
            raise AssertionError(
                f"无法静态解析 Zod 对象修饰符 `.{method}(...)`（line {tokens[pos].line}）"
            )
        pos = close_paren + 1

    return fields


def _fields_from_zod_shape_arg(
    tokens: list[_TsToken],
    start: int,
    end: int,
    *,
    inits: dict[str, tuple[int, int]],
    string_consts: dict[str, str],
    recursive: bool,
    seen_consts: frozenset[str],
) -> set[str]:
    if start < end and _tok_is(tokens[start], "punct", "{"):
        entries = _extract_object_literal_entries(
            tokens,
            start,
            inits=inits,
            string_consts=string_consts,
            strict=True,
            seen_consts=seen_consts,
        )
    elif start + 1 == end and tokens[start].kind == "ident":
        ref = tokens[start].value
        if ref in inits and ref not in seen_consts:
            rs, re_ = _unwrap_parens_and_casts(tokens, *inits[ref])
            if rs < re_ and _tok_is(tokens[rs], "punct", "{"):
                entries = _extract_object_literal_entries(
                    tokens,
                    rs,
                    inits=inits,
                    string_consts=string_consts,
                    strict=True,
                    seen_consts=seen_consts | {ref},
                )
            else:
                raise AssertionError(
                    f"无法静态解析 Zod shape 引用 {ref!r}（line {tokens[start].line}）"
                )
        else:
            raise AssertionError(
                f"无法静态解析 Zod shape 引用 {ref!r}（line {tokens[start].line}）"
            )
    else:
        snippet = " ".join(t.value for t in tokens[start:end])
        raise AssertionError(f"无法静态解析 Zod z.object(...) 参数：{snippet!r}")

    out: set[str] = set()
    for key, val_range in entries:
        if recursive and val_range is not None:
            nested = _extract_zod_expr_fields(
                tokens,
                val_range[0],
                val_range[1],
                inits=inits,
                string_consts=string_consts,
                recursive=True,
                seen_consts=seen_consts,
            )
            if nested:
                for sub in nested:
                    out.add(f"{key}.{sub}")
                continue
        out.add(key)
    return out


def _extract_all_zod_object_fields_in_source(
    source: str,
    *,
    extra_string_consts: dict[str, str] | None = None,
    schema_names: set[str] | None = None,
    recursive: bool = True,
) -> dict[str, set[str]]:
    """Extract fields for every (or named) top-level Zod object schema in ``source``."""
    tokens = _tokenize_ts(source)
    inits = _collect_ts_const_inits(tokens)
    string_consts = dict(extra_string_consts or {})
    string_consts.update(_extract_string_consts_from_inits(tokens, inits))

    targets = sorted(schema_names) if schema_names is not None else sorted(inits)
    out: dict[str, set[str]] = {}
    for name in targets:
        if name not in inits:
            raise AssertionError(f"找不到 Zod 模式声明 {name!r}")
        fields = _extract_zod_expr_fields(
            tokens,
            *inits[name],
            inits=inits,
            string_consts=string_consts,
            recursive=recursive,
        )
        if fields is not None:
            out[name] = fields
        elif schema_names is not None:
            raise AssertionError(f"{name!r} 不是可解析的 Zod 对象模式")
    return out


def _extract_string_list_const(
    path: Path,
    const_name: str,
    *,
    extra_string_consts: dict[str, str] | None = None,
) -> set[str]:
    """Extract string elements from a TS array or ``new Set([...])`` constant,
    recursively resolving ``...OTHER_ARRAY`` spreads."""
    tokens = _tokenize_ts(path.read_text(encoding="utf-8"))
    inits = _collect_ts_const_inits(tokens)
    string_consts = dict(extra_string_consts or {})
    string_consts.update(_extract_string_consts_from_inits(tokens, inits))

    def _from_const(name: str, seen: frozenset[str]) -> set[str]:
        if name not in inits:
            raise AssertionError(f"找不到数组常量 {name!r}（{path}）")
        start, end = inits[name]
        # Locate first `[` inside initializer (handles `[...]` and `new Set<string>([...])`)
        arr_idx = None
        for idx in range(start, end):
            if _tok_is(tokens[idx], "punct", "["):
                arr_idx = idx
                break
        if arr_idx is None:
            raise AssertionError(f"在 {name!r} 初始化表达式中未找到数组字面量（{path}）")
        arr_end = _find_matching(tokens, arr_idx, "[", "]")
        result: set[str] = set()
        for ps, pe in _split_top_level(tokens, arr_idx + 1, arr_end, ","):
            if _tok_is(tokens[ps], "punct", "..."):
                ref_s, ref_e = _unwrap_parens_and_casts(tokens, ps + 1, pe)
                if ref_s + 1 == ref_e and tokens[ref_s].kind == "ident":
                    ref = tokens[ref_s].value
                    if ref not in seen:
                        result |= _from_const(ref, seen | {ref})
                        continue
                snippet = " ".join(t.value for t in tokens[ps:pe])
                raise AssertionError(f"无法静态解析数组展开 {snippet!r}（{path}）")
            resolved = _resolve_ts_string_expr(tokens, ps, pe, string_consts)
            if resolved is None:
                snippet = " ".join(t.value for t in tokens[ps:pe])
                raise AssertionError(f"无法静态解析数组元素 {snippet!r}（{path}）")
            result.add(resolved)
        return result

    return _from_const(const_name, frozenset({const_name}))


def _extract_ts_interface_fields(
    sources: dict[str, str], interface_name: str
) -> set[str]:
    """Extract property names of ``interface <Name> [extends Base, ...] { ... }``."""
    interfaces: dict[str, tuple[list[str], list[_TsToken], int]] = {}
    for src in sources.values():
        tokens = _tokenize_ts(src)
        i = 0
        while i < len(tokens) - 2:
            if _tok_is(tokens[i], "ident", "interface") and tokens[i + 1].kind == "ident":
                name = tokens[i + 1].value
                parents: list[str] = []
                j = i + 2
                if j < len(tokens) and _tok_is(tokens[j], "ident", "extends"):
                    j += 1
                    while j < len(tokens) and not _tok_is(tokens[j], "punct", "{"):
                        if tokens[j].kind == "ident":
                            parents.append(tokens[j].value)
                        j += 1
                if j < len(tokens) and _tok_is(tokens[j], "punct", "{"):
                    interfaces[name] = (parents, tokens, j)
                    i = _find_matching(tokens, j, "{", "}") + 1
                    continue
            i += 1

    def _collect(name: str, seen: frozenset[str]) -> set[str]:
        if name not in interfaces:
            raise AssertionError(f"找不到 TypeScript interface 声明 {name!r}")
        parents, tokens, brace_idx = interfaces[name]
        end_idx = _find_matching(tokens, brace_idx, "{", "}")
        keys: set[str] = set()
        for parent in parents:
            if parent in interfaces and parent not in seen:
                keys |= _collect(parent, seen | {parent})
        depth = 0
        idx = brace_idx + 1
        while idx < end_idx:
            t = tokens[idx]
            if t.kind == "punct":
                if t.value in ("{", "[", "("):
                    depth += 1
                elif t.value in ("}", "]", ")"):
                    depth = max(0, depth - 1)
                idx += 1
                continue
            if depth == 0 and t.kind in ("ident", "string"):
                nxt = idx + 1
                if nxt < end_idx and _tok_is(tokens[nxt], "punct", "?"):
                    nxt += 1
                if nxt < end_idx and _tok_is(tokens[nxt], "punct", ":"):
                    keys.add(t.value)
                    # Skip property type until top-level `;` / `,` or newline boundary
                    # starting the next property (valid TS interfaces may omit semicolons).
                    idx = nxt + 1
                    angle_depth = 0
                    while idx < end_idx:
                        pt = tokens[idx]
                        if (
                            depth == 0
                            and angle_depth == 0
                            and idx > nxt + 1
                            and pt.line > tokens[idx - 1].line
                            and pt.kind in ("ident", "string")
                        ):
                            look = idx + 1
                            if look < end_idx and _tok_is(tokens[look], "punct", "?"):
                                look += 1
                            if look < end_idx and _tok_is(tokens[look], "punct", ":"):
                                break
                        if pt.kind == "punct":
                            if pt.value in ("{", "[", "("):
                                depth += 1
                            elif pt.value in ("}", "]", ")"):
                                depth = max(0, depth - 1)
                            elif pt.value == "<" and depth == 0:
                                angle_depth += 1
                            elif pt.value == ">" and depth == 0 and angle_depth > 0:
                                angle_depth -= 1
                            elif pt.value in (";", ",") and depth == 0 and angle_depth == 0:
                                idx += 1
                                break
                        idx += 1
                    continue
            idx += 1
        return keys

    return _collect(interface_name, frozenset({interface_name}))


def _extract_type_string_union_from_source(source: str, type_name: str) -> set[str]:
    """Extract string literal members from ``type <type_name> = 'a' | 'b'``
    (independent of whether a trailing semicolon is present)."""
    tokens = _tokenize_ts(source)
    for i in range(len(tokens) - 3):
        if (
            _tok_is(tokens[i], "ident", "type")
            and _tok_is(tokens[i + 1], "ident", type_name)
            and _tok_is(tokens[i + 2], "punct", "=")
        ):
            j = i + 3
            if j < len(tokens) and _tok_is(tokens[j], "punct", "|"):
                j += 1
            if j >= len(tokens) or tokens[j].kind != "string":
                got = tokens[j].value if j < len(tokens) else "<EOF>"
                raise AssertionError(
                    f"类型联合 {type_name!r} 包含无法解析的非字符串成员：{got!r}"
                )
            values: set[str] = {tokens[j].value}
            j += 1
            while j < len(tokens) and _tok_is(tokens[j], "punct", "|"):
                j += 1
                if j >= len(tokens) or tokens[j].kind != "string":
                    got = tokens[j].value if j < len(tokens) else "<EOF>"
                    raise AssertionError(
                        f"类型联合 {type_name!r} 包含无法解析的非字符串成员：{got!r}"
                    )
                values.add(tokens[j].value)
                j += 1
            return values
    raise AssertionError(f"Could not locate type union {type_name!r}")


def _extract_profile_fields_from_source(source: str, label: str) -> set[str]:
    """Extract ``(section, key)`` pairs from ``PROFILE_FIELDS`` in ``welcome/profile.ts``."""
    tokens = _tokenize_ts(source)
    inits = _collect_ts_const_inits(tokens)
    assert "PROFILE_FIELDS" in inits, f"Missing PROFILE_FIELDS in {label}"
    idx, _end = inits["PROFILE_FIELDS"]
    assert _tok_is(tokens[idx], "punct", "{")
    end_idx = _find_matching(tokens, idx, "{", "}")
    discovered: set[str] = set()
    i = idx + 1
    depth = 0
    while i < end_idx:
        tok = tokens[i]
        if _tok_is(tok, "punct", "{"):
            if depth == 0:
                entry_end = _find_matching(tokens, i, "{", "}")
                section: str | None = None
                key: str | None = None
                for j in range(i + 1, entry_end - 2):
                    if (
                        _tok_is(tokens[j], "ident", "section")
                        and _tok_is(tokens[j + 1], "punct", ":")
                        and tokens[j + 2].kind == "string"
                    ):
                        section = tokens[j + 2].value
                    elif (
                        _tok_is(tokens[j], "ident", "key")
                        and _tok_is(tokens[j + 1], "punct", ":")
                        and tokens[j + 2].kind == "string"
                    ):
                        key = tokens[j + 2].value
                assert section and key, f"Incomplete PROFILE_FIELDS entry in {label}"
                discovered.add(f"user_preferences.{section}.{key}")
                i = entry_end + 1
                continue
            depth += 1
        elif _tok_is(tok, "punct", "}"):
            depth -= 1
        i += 1
    assert discovered, f"No PROFILE_FIELDS entries extracted from {label}"
    return discovered


def _scan_ts_preference_calls_and_literals(
    source: str,
    *,
    global_string_consts: dict[str, str],
    filename: str = "<memory>",
) -> tuple[set[str], set[str]]:
    """Scan a single TS/TSX source for ``other_preference`` and ``model_preference`` keys.

    Detects:
    - ``readOtherPreference(prefs, KEY)``
    - ``otherPreferencePatch(KEY, val)``
    - ``usePreferenceKey(KEY, ...)``
    - ``{ other_preference: { ... } }`` and ``{ model_preference: { ... } }``
      object literals (including computed keys and ternary spreads)
    - ``const AUTO_APPROVE = { ... }`` value strings (passed dynamically to ``other_preference``)
    - ``other[KEY_CONST]`` / ``other_preference?.<key>`` accesses
    """
    other_keys: set[str] = set()
    model_keys: set[str] = set()

    tokens = _tokenize_ts(source)
    inits = _collect_ts_const_inits(tokens)
    consts = dict(global_string_consts)
    consts.update(_extract_string_consts_from_inits(tokens, inits))

    # Helper definition files whose `key` / spread is a generic forwarding parameter.
    is_generic_helper = filename.endswith(
        (
            "otherPreference.ts",
            "usePreferenceKey.ts",
            "useUpdatePreferences.ts",
            "useAutoApprove.ts",
            "appearancePreference.ts",
        )
    )

    for i, tok in enumerate(tokens):
        # 1. Function call helpers: readOtherPreference / otherPreferencePatch / usePreferenceKey
        if (
            tok.kind == "ident"
            and tok.value in ("readOtherPreference", "otherPreferencePatch", "usePreferenceKey")
            and i + 1 < len(tokens)
            and _tok_is(tokens[i + 1], "punct", "(")
        ):
            # Skip function definitions (`function readOtherPreference(...)`)
            if i > 0 and _tok_is(tokens[i - 1], "ident", "function"):
                continue
            close = _find_matching(tokens, i + 1, "(", ")")
            args = _split_top_level(tokens, i + 2, close, ",")
            key_arg_idx = 1 if tok.value == "readOtherPreference" else 0
            if len(args) > key_arg_idx:
                resolved = _resolve_ts_string_expr(
                    tokens, *args[key_arg_idx], consts
                )
                if resolved is not None:
                    other_keys.add(resolved)
                elif not is_generic_helper:
                    snippet = " ".join(
                        t.value for t in tokens[args[key_arg_idx][0] : args[key_arg_idx][1]]
                    )
                    raise AssertionError(
                        f"无法静态解析 {tok.value}(...) 的偏好键参数 {snippet!r}（{filename}:{tok.line}）"
                    )
            continue

        # 2. Object literals: `{ other_preference: { ... } }` & `{ model_preference: { ... } }`
        if (
            tok.kind == "ident"
            and tok.value in ("other_preference", "model_preference")
            and i + 2 < len(tokens)
            and _tok_is(tokens[i + 1], "punct", ":")
            and _tok_is(tokens[i + 2], "punct", "{")
        ):
            # Ignore TypeScript type annotations (e.g. `other_preference?: { dashboard?: ... }`
            # already has `?` before `:`; check object literal values inside expressions).
            if i > 0 and _tok_is(tokens[i - 1], "punct", "?"):
                continue
            entries = _extract_object_literal_entries(
                tokens,
                i + 2,
                inits=inits,
                string_consts=consts,
                strict=not is_generic_helper,
            )
            target_set = other_keys if tok.value == "other_preference" else model_keys
            for key_name, _val_range in entries:
                target_set.add(key_name)
            continue

        # 3. Direct member read: `other_preference?.foo` or `other_preference.foo`
        if (
            tok.kind == "ident"
            and tok.value == "other_preference"
            and i + 2 < len(tokens)
            and tokens[i + 1].kind == "punct"
            and tokens[i + 1].value in (".", "?.")
            and tokens[i + 2].kind == "ident"
        ):
            prop = tokens[i + 2].value
            # Ignore JS prototype/builtin helpers if any
            if prop not in ("constructor", "hasOwnProperty", "toString", "valueOf"):
                other_keys.add(prop)

    # 4. AUTO_APPROVE lookup object (keys written via `{ other_preference: { [key]: ... } }`)
    if "AUTO_APPROVE" in inits:
        s, e = _unwrap_parens_and_casts(tokens, *inits["AUTO_APPROVE"])
        if s < e and _tok_is(tokens[s], "punct", "{"):
            for _k, val_range in _extract_object_literal_entries(
                tokens, s, inits=inits, string_consts=consts, strict=True
            ):
                if val_range is not None:
                    val = _resolve_ts_string_expr(tokens, *val_range, consts)
                    if val is not None:
                        other_keys.add(val)

    # 5. Indexed `other[KEY_CONST]` reads (e.g. `favoriteModels.ts` reading STARRED_KEY / HIDDEN_KEY / STARRED_ONLY_KEY)
    if "favoriteModels.ts" in filename:
        for i in range(len(tokens) - 3):
            if (
                _tok_is(tokens[i], "ident", "other")
                and _tok_is(tokens[i + 1], "punct", "[")
            ):
                close = _find_matching(tokens, i + 1, "[", "]")
                resolved = _resolve_ts_string_expr(tokens, i + 2, close, consts)
                if resolved is not None:
                    other_keys.add(resolved)

    return other_keys, model_keys


def _discover_web_preference_keys() -> tuple[set[str], set[str]]:
    """Scan all non-test TS/TSX files under ``web/src/`` for preference reads/writes."""
    global_consts = _collect_web_exported_string_consts()
    other_discovered: set[str] = set()
    model_discovered: set[str] = set()

    markers = (
        "other_preference",
        "model_preference",
        "readOtherPreference",
        "otherPreferencePatch",
        "usePreferenceKey",
        "favoriteModels",
    )
    for ts_file in sorted(WEB_SRC.rglob("*.ts*")):
        if "__tests__" in ts_file.parts or ".test." in ts_file.name:
            continue
        text = ts_file.read_text(encoding="utf-8")
        if not any(m in text for m in markers):
            continue
        o_keys, m_keys = _scan_ts_preference_calls_and_literals(
            text,
            global_string_consts=global_consts,
            filename=str(ts_file.relative_to(WEB_SRC)),
        )
        other_discovered |= {f"user_preferences.other_preference.{k}" for k in o_keys}
        model_discovered |= {f"user_preferences.model_preference.{k}" for k in m_keys}

    return other_discovered, model_discovered


_PREF_ZOD_MARKERS: frozenset[str] = frozenset(
    {
        "model_preference",
        "other_preference",
        "risk_preference",
        "investment_preference",
        "agent_preference",
        "usePreferenceKey",
        "readOtherPreference",
        "otherPreferencePatch",
        "ModelPreferences",
        "OnboardingPrefs",
        "ThreadField",
    }
)


def _is_pref_zod_source(source: str) -> bool:
    """Return True iff TS/TSX ``source`` imports ``zod`` (or ``zod/*``) AND contains
    at least one non-comment identifier/string token referencing preference or thread
    setting storage (ignoring comments)."""
    tokens = _tokenize_ts(source)
    imports_zod = False
    for i, tok in enumerate(tokens):
        if tok.kind == "string" and (tok.value == "zod" or tok.value.startswith("zod/")):
            if i > 0 and _tok_is(tokens[i - 1], "ident", "from"):
                imports_zod = True
                break
            if (
                i >= 2
                and _tok_is(tokens[i - 2], "ident", "require")
                and _tok_is(tokens[i - 1], "punct", "(")
            ):
                imports_zod = True
                break
    if not imports_zod:
        return False
    return any(
        tok.kind in ("ident", "string") and tok.value in _PREF_ZOD_MARKERS
        for tok in tokens
    )


def _discover_web_pref_zod_files() -> set[str]:
    """Find every non-test file in ``web/src/`` that imports ``zod`` and references
    preference or thread setting storage in non-comment code."""
    found: set[str] = set()
    for ts_file in sorted(WEB_SRC.rglob("*.ts*")):
        if "__tests__" in ts_file.parts or ".test." in ts_file.name:
            continue
        text = ts_file.read_text(encoding="utf-8")
        if "zod" not in text:
            continue
        if _is_pref_zod_source(text):
            found.add(str(ts_file.relative_to(WEB_SRC)))
    return found


def _verify_frontend_pref_zod_file(
    rel_path: str,
    kind: str,
    source: str,
    *,
    global_consts: dict[str, str],
    registry_items: list[dict[str, Any]],
) -> None:
    """Extract and verify every field declared by one file in
    ``_KNOWN_FRONTEND_PREF_ZOD_FILES``. Fails explicitly on any unimplemented
    ``kind`` or unregistered sub-schema field path."""
    registered_keys = _registry_keys(registry_items)
    items_by_key = {str(item["key"]): item for item in registry_items}

    if kind == "model_preference_schemas":
        zod_schemas = _extract_all_zod_object_fields_in_source(
            source, extra_string_consts=global_consts, recursive=True
        )
        assert zod_schemas, f"Expected parsed Zod object schemas in {rel_path}"
        extracted = {
            f"user_preferences.model_preference.{f}"
            for fields in zod_schemas.values()
            for f in fields
        }
        _assert_all_registered(
            extracted,
            source=f"web/src/{rel_path} (Zod model_preference schemas)",
            registered_keys=registered_keys,
        )
        return

    if kind == "profile_fields":
        profile_keys = _extract_profile_fields_from_source(source, rel_path)
        _assert_all_registered(
            profile_keys,
            source=f"web/src/{rel_path} (PROFILE_FIELDS)",
            registered_keys=registered_keys,
        )
        extra_zod = _extract_all_zod_object_fields_in_source(
            source, extra_string_consts=global_consts, recursive=True
        )
        assert not extra_zod, (
            f"web/src/{rel_path} 新增了未纳入校验的 Zod 对象模式：{sorted(extra_zod)}\n"
            f"{FAILURE_GUIDANCE}"
        )
        return

    if kind.startswith("other_preference_subschema:"):
        pref_key = kind.split(":", 1)[1].strip()
        assert pref_key, f"Empty pref_key in kind={kind!r} ({rel_path})"
        parent_key = f"user_preferences.other_preference.{pref_key}"
        _assert_all_registered(
            {parent_key},
            source=f"web/src/{rel_path}",
            registered_keys=registered_keys,
        )
        schemas = _extract_all_zod_object_fields_in_source(
            source, extra_string_consts=global_consts, recursive=True
        )
        extracted_subfields = {
            field for fields in schemas.values() for field in fields
        }
        entry = items_by_key[parent_key]
        raw_declared = entry.get("schema_fields")
        if raw_declared is None:
            declared_subfields: set[str] = set()
        else:
            assert isinstance(raw_declared, list), (
                f"Entry {parent_key!r}: schema_fields must be a list"
            )
            declared_subfields = {
                str(f).strip() for f in raw_declared if str(f).strip()
            }

        missing_sub = sorted(extracted_subfields - declared_subfields)
        assert not missing_sub, (
            f"发现未登记的 Zod 子模式字段（来源：web/src/{rel_path} -> {parent_key}）："
            f"{missing_sub}\n{FAILURE_GUIDANCE}"
        )
        stale_sub = sorted(declared_subfields - extracted_subfields)
        assert not stale_sub, (
            f"settings_registry.yaml 中 {parent_key} 存在已失效的 schema_fields：{stale_sub}"
        )
        return

    raise AssertionError(
        f"_KNOWN_FRONTEND_PREF_ZOD_FILES 中存在未实现提取逻辑的 kind={kind!r}（{rel_path}）"
    )


# ---------------------------------------------------------------------------
# Backend user_preferences Pydantic + AST JSON read/write scanner
# ---------------------------------------------------------------------------

_SQL_JSON_OP_RE = re.compile(
    r"\b(risk_preference|investment_preference|agent_preference|other_preference|model_preference)"
    r"\s*(?:->>|->)\s*'([A-Za-z0-9_]+)'"
)
_SQL_JSON_PATH_RE = re.compile(
    r"\b(risk_preference|investment_preference|agent_preference|other_preference|model_preference)"
    r"\s*(?:#>>|#>)\s*'\{([A-Za-z0-9_]+)"
)
_SQL_JSON_PARAM_RE = re.compile(
    r"\b(risk_preference|investment_preference|agent_preference|other_preference|model_preference)"
    r"\s*(?:->>|->)\s*%s"
)


def _unwrap_pydantic_model_types(annotation: Any) -> list[type[BaseModel]]:
    """Recursively collect every Pydantic ``BaseModel`` subclass inside an annotation
    (``Model``, ``Optional[Model]``, ``Annotated[Model, ...]``, ``list[Model]``,
    ``dict[str, Model]``, ``Union[A, B]``, etc.)."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    out: list[type[BaseModel]] = []
    origin = get_origin(annotation)
    if origin is not None:
        for arg in get_args(annotation):
            for sub in _unwrap_pydantic_model_types(arg):
                if sub not in out:
                    out.append(sub)
    return out


def _unwrap_pydantic_model_type(annotation: Any) -> type[BaseModel] | None:
    """Recursively unwrap the first Pydantic ``BaseModel`` in ``annotation``."""
    found = _unwrap_pydantic_model_types(annotation)
    return found[0] if found else None


def _extract_pydantic_model_fields_recursive(
    model_cls: type[BaseModel],
    *,
    prefix: str = "",
    seen: frozenset[type[BaseModel]] = frozenset(),
    skip_fields: frozenset[str] = frozenset(),
) -> set[str]:
    """Recursively extract dot-separated writable field paths from ``model_cls``,
    expanding nested ``BaseModel`` fields (including inside ``Optional`` / ``list``
    / ``dict`` / ``Union`` containers)."""
    if model_cls in seen:
        return set()
    next_seen = seen | {model_cls}
    discovered: set[str] = set()
    for field_name, field_info in model_cls.model_fields.items():
        if not prefix and field_name in skip_fields:
            continue
        path = f"{prefix}.{field_name}" if prefix else field_name
        sub_models = _unwrap_pydantic_model_types(field_info.annotation)
        if sub_models:
            for sub in sub_models:
                discovered |= _extract_pydantic_model_fields_recursive(
                    sub, prefix=path, seen=next_seen
                )
        else:
            discovered.add(path)
    return discovered


def _extract_pydantic_preference_fields(
    update_model: type[BaseModel],
    *,
    transient_fields: frozenset[str] = _PREF_UPDATE_TRANSIENT_FIELDS,
) -> set[str]:
    """Dynamically extract ``user_preferences.<section>.<field>`` from every
    section model declared on ``update_model`` (including ``ModelPreference``)."""
    discovered: set[str] = set()
    for section, field_info in update_model.model_fields.items():
        if section in transient_fields:
            continue
        sub_model = _unwrap_pydantic_model_type(field_info.annotation)
        assert sub_model is not None, (
            f"{update_model.__name__}.{section} 缺少可解析的 Pydantic 模型类型"
        )
        for sub_field in sub_model.model_fields:
            discovered.add(f"user_preferences.{section}.{sub_field}")
    return discovered


def _resolve_py_module_str_consts(tree: ast.Module) -> dict[str, str]:
    """Resolve module-level string constants and imported string constants."""
    consts: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            consts[node.targets[0].id] = node.value.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            consts[node.target.id] = node.value.value
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            # Resolve imported ALL_CAPS / _PREF_KEY string constants from local modules
            if not node.module.startswith("src."):
                continue
            interesting = [
                alias
                for alias in node.names
                if alias.name != "*"
                and (alias.name.isupper() or alias.name.endswith("_KEY"))
            ]
            if not interesting:
                continue
            try:
                mod = importlib.import_module(node.module)
            except (ImportError, OSError, RuntimeError):
                mod = None
            if mod is None:
                continue
            for alias in interesting:
                val = getattr(mod, alias.name, None)
                if isinstance(val, str):
                    consts[alias.asname or alias.name] = val
    return consts


def _py_expr_section(node: ast.AST, var_sec: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return var_sec.get(node.id)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        for v in node.values:
            sec = _py_expr_section(v, var_sec)
            if sec:
                return sec
        return None
    if isinstance(node, ast.IfExp):
        return _py_expr_section(node.body, var_sec) or _py_expr_section(
            node.orelse, var_sec
        )
    if isinstance(node, ast.Await):
        return _py_expr_section(node.value, var_sec)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _py_expr_section(node.left, var_sec) or _py_expr_section(
            node.right, var_sec
        )
    if isinstance(node, ast.Dict):
        for dk, dv in zip(node.keys, node.values):
            if dk is None:
                sec = _py_expr_section(dv, var_sec)
                if sec:
                    return sec
        return None
    if isinstance(node, ast.Call):
        fn = node.func
        fname = (
            fn.attr
            if isinstance(fn, ast.Attribute)
            else (fn.id if isinstance(fn, ast.Name) else "")
        )
        if fname in (
            "get_model_preference",
            "model_preference_of",
            "without_removed_providers",
            "with_borrowed_ladders",
        ):
            return "model_preference"
        if fname == "get" and node.args:
            arg0 = node.args[0]
            if isinstance(arg0, ast.Constant) and arg0.value in _PREFERENCE_SECTIONS:
                return str(arg0.value)
        if fname in ("dict", "deepcopy", "copy") and node.args:
            return _py_expr_section(node.args[0], var_sec)
        if fname == "copy" and not node.args and isinstance(fn, ast.Attribute):
            return _py_expr_section(fn.value, var_sec)
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value in _PREFERENCE_SECTIONS
    ):
        return str(node.slice.value)
    return None


def _collect_py_dict_source_names(node: ast.AST) -> list[str]:
    """Collect local variable names whose dict contents flow into ``node``."""
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Call):
        fn = node.func
        fname = (
            fn.attr
            if isinstance(fn, ast.Attribute)
            else (fn.id if isinstance(fn, ast.Name) else "")
        )
        if fname in ("dict", "deepcopy", "copy") and node.args:
            return _collect_py_dict_source_names(node.args[0])
        if fname == "copy" and not node.args and isinstance(fn, ast.Attribute):
            return _collect_py_dict_source_names(fn.value)
        return []
    if isinstance(node, ast.Dict):
        out: list[str] = []
        for dk, dv in zip(node.keys, node.values):
            if dk is None:
                out.extend(_collect_py_dict_source_names(dv))
        return out
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _collect_py_dict_source_names(node.left) + _collect_py_dict_source_names(
            node.right
        )
    if isinstance(node, ast.IfExp):
        return _collect_py_dict_source_names(node.body) + _collect_py_dict_source_names(
            node.orelse
        )
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        out = []
        for v in node.values:
            out.extend(_collect_py_dict_source_names(v))
        return out
    return []


def _resolve_py_str(node: ast.AST, consts: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in consts:
        return consts[node.id]
    return None


def _scan_python_preference_json_keys(
    source: str,
    *,
    filename: str = "<memory>",
    never_moved: frozenset[str] = frozenset(),
    dalton_model_keys: frozenset[str] = frozenset(),
) -> set[str]:
    """Extract ``user_preferences.<section>.<key>`` read/write paths from Python AST."""
    if not any(sec in source for sec in _PREFERENCE_SECTIONS):
        return set()

    tree = ast.parse(source, filename=filename)
    consts = _resolve_py_module_str_consts(tree)
    discovered: set[str] = set()

    def _record(section: str, key: str) -> None:
        if not key or key.startswith("__"):
            return
        # Normalize legacy cross-column fallback reads back to canonical storage column.
        if section == "model_preference" and key in never_moved:
            section = "other_preference"
        elif section == "other_preference" and key in dalton_model_keys:
            section = "model_preference"
        discovered.add(f"user_preferences.{section}.{key}")

    # 1. SQL JSON literal operators (`other_preference ->> 'key'`, `#>` path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for m in _SQL_JSON_OP_RE.finditer(node.value):
                _record(m.group(1), m.group(2))
            for m in _SQL_JSON_PATH_RE.finditer(node.value):
                _record(m.group(1), m.group(2))

        # Parameterized SQL `other_preference->%s::text` with a constant parameter
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            sql = node.args[0].value
            sql_matches = list(_SQL_JSON_PARAM_RE.finditer(sql))
            if sql_matches and isinstance(node.args[1], (ast.Tuple, ast.List)):
                for param in node.args[1].elts:
                    key_val = _resolve_py_str(param, consts)
                    if key_val:
                        _record(sql_matches[0].group(1), key_val)

    # 2. Function-level preference bag tracking & upsert/update dict literals
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        var_sec: dict[str, str] = {}
        for arg in (*fn.args.args, *fn.args.kwonlyargs):
            for sec in _PREFERENCE_SECTIONS:
                short = sec.replace("_preference", "_pref")
                if (
                    arg.arg == sec
                    or arg.arg == short
                    or arg.arg.endswith("_" + sec)
                    or arg.arg.endswith("_" + short)
                ):
                    var_sec[arg.arg] = sec

        # Collect tuple-loop string key bindings, e.g.
        # `for key, allowed in (("default_model_scope", ...), ("background_follows", ...)):`
        loop_key_sets: dict[str, set[str]] = {}
        for sub in ast.walk(fn):
            if isinstance(sub, ast.For) and isinstance(sub.iter, (ast.Tuple, ast.List)):
                if isinstance(sub.target, ast.Tuple) and sub.target.elts and isinstance(sub.target.elts[0], ast.Name):
                    kvar = sub.target.elts[0].id
                    vals: set[str] = set()
                    for elt in sub.iter.elts:
                        if isinstance(elt, (ast.Tuple, ast.List)) and elt.elts:
                            kv = _resolve_py_str(elt.elts[0], consts)
                            if kv:
                                vals.add(kv)
                    if vals:
                        loop_key_sets.setdefault(kvar, set()).update(vals)
                elif isinstance(sub.target, ast.Name):
                    kvar = sub.target.id
                    vals = set()
                    for elt in sub.iter.elts:
                        kv = _resolve_py_str(elt, consts)
                        if kv:
                            vals.add(kv)
                    if vals:
                        loop_key_sets.setdefault(kvar, set()).update(vals)

        # Fixed-point forward + backward propagation inside the function:
        # - forward from preference reads / copies / conditional expressions / merges
        # - backward from upsert_user_preferences(..., <sec>=expr), update(), and `|=`
        changed = True
        while changed:
            changed = False
            for sub in ast.walk(fn):
                if (
                    isinstance(sub, ast.Assign)
                    and len(sub.targets) == 1
                    and isinstance(sub.targets[0], ast.Name)
                ):
                    sec = _py_expr_section(sub.value, var_sec)
                    if sec and sub.targets[0].id not in var_sec:
                        var_sec[sub.targets[0].id] = sec
                        changed = True
                elif (
                    isinstance(sub, ast.AnnAssign)
                    and isinstance(sub.target, ast.Name)
                    and sub.value is not None
                ):
                    sec = _py_expr_section(sub.value, var_sec)
                    if sec and sub.target.id not in var_sec:
                        var_sec[sub.target.id] = sec
                        changed = True
                elif isinstance(sub, ast.Call):
                    fname = (
                        sub.func.attr
                        if isinstance(sub.func, ast.Attribute)
                        else (sub.func.id if isinstance(sub.func, ast.Name) else "")
                    )
                    if fname in ("upsert_user_preferences", "update_user_preferences"):
                        for kw in sub.keywords:
                            if kw.arg in _PREFERENCE_SECTIONS:
                                for nm in _collect_py_dict_source_names(kw.value):
                                    if nm not in var_sec:
                                        var_sec[nm] = kw.arg
                                        changed = True
                    elif (
                        fname == "update"
                        and isinstance(sub.func, ast.Attribute)
                        and sub.args
                    ):
                        sec = _py_expr_section(sub.func.value, var_sec)
                        if sec:
                            for nm in _collect_py_dict_source_names(sub.args[0]):
                                if nm not in var_sec:
                                    var_sec[nm] = sec
                                    changed = True
                elif isinstance(sub, ast.AugAssign) and isinstance(sub.op, ast.BitOr):
                    sec = _py_expr_section(sub.target, var_sec)
                    if sec:
                        for nm in _collect_py_dict_source_names(sub.value):
                            if nm not in var_sec:
                                var_sec[nm] = sec
                                changed = True

        def _keys_of(
            arg_node: ast.AST,
            _loop_sets: dict[str, set[str]] = loop_key_sets,
        ) -> set[str]:
            single = _resolve_py_str(arg_node, consts)
            if single is not None:
                return {single}
            if isinstance(arg_node, ast.Name) and arg_node.id in _loop_sets:
                return set(_loop_sets[arg_node.id])
            return set()

        def _record_dict_expr(sec: str, expr: ast.AST) -> None:
            if isinstance(expr, ast.Dict):
                for dk, dv in zip(expr.keys, expr.values):
                    if dk is not None:
                        for k in _keys_of(dk):
                            _record(sec, k)
                    else:
                        _record_dict_expr(sec, dv)
            elif isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
                _record_dict_expr(sec, expr.left)
                _record_dict_expr(sec, expr.right)
            elif isinstance(expr, ast.IfExp):
                _record_dict_expr(sec, expr.body)
                _record_dict_expr(sec, expr.orelse)
            elif isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.Or):
                for v in expr.values:
                    _record_dict_expr(sec, v)
            elif isinstance(expr, (ast.List, ast.Tuple)):
                # e.g. `model_pref.update([("new_key", val)])`
                for elt in expr.elts:
                    if isinstance(elt, (ast.Tuple, ast.List)) and len(elt.elts) == 2:
                        for k in _keys_of(elt.elts[0]):
                            _record(sec, k)
            elif isinstance(expr, ast.Call):
                efn = expr.func
                efname = (
                    efn.attr
                    if isinstance(efn, ast.Attribute)
                    else (efn.id if isinstance(efn, ast.Name) else "")
                )
                if efname == "dict":
                    if expr.args:
                        _record_dict_expr(sec, expr.args[0])
                    for kw in expr.keywords:
                        if kw.arg:
                            _record(sec, kw.arg)
                elif efname in ("deepcopy", "copy") and expr.args:
                    _record_dict_expr(sec, expr.args[0])

        for sub in ast.walk(fn):
            if (
                isinstance(sub, ast.Assign)
                and len(sub.targets) == 1
                and isinstance(sub.targets[0], ast.Name)
            ):
                sec = var_sec.get(sub.targets[0].id)
                if sec:
                    _record_dict_expr(sec, sub.value)
            elif (
                isinstance(sub, ast.AnnAssign)
                and isinstance(sub.target, ast.Name)
                and sub.value is not None
            ):
                sec = var_sec.get(sub.target.id)
                if sec:
                    _record_dict_expr(sec, sub.value)
            elif isinstance(sub, ast.AugAssign) and isinstance(sub.op, ast.BitOr):
                sec = _py_expr_section(sub.target, var_sec)
                if sec:
                    _record_dict_expr(sec, sub.value)
            elif (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr in ("get", "pop", "setdefault")
                and sub.args
            ):
                sec = _py_expr_section(sub.func.value, var_sec)
                if sec:
                    for k in _keys_of(sub.args[0]):
                        _record(sec, k)
            elif (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "update"
            ):
                sec = _py_expr_section(sub.func.value, var_sec)
                if sec:
                    if sub.args:
                        _record_dict_expr(sec, sub.args[0])
                    for kw in sub.keywords:
                        if kw.arg:
                            _record(sec, kw.arg)
            elif isinstance(sub, ast.Subscript):
                sec = _py_expr_section(sub.value, var_sec)
                if sec:
                    for k in _keys_of(sub.slice):
                        _record(sec, k)
            elif (
                isinstance(sub, ast.Compare)
                and len(sub.ops) == 1
                and isinstance(sub.ops[0], (ast.In, ast.NotIn))
            ):
                sec = _py_expr_section(sub.comparators[0], var_sec)
                if sec:
                    for k in _keys_of(sub.left):
                        _record(sec, k)
            elif isinstance(sub, ast.Call):
                fname = (
                    sub.func.attr
                    if isinstance(sub.func, ast.Attribute)
                    else (sub.func.id if isinstance(sub.func, ast.Name) else "")
                )
                if fname in ("upsert_user_preferences", "update_user_preferences"):
                    for kw in sub.keywords:
                        if kw.arg in _PREFERENCE_SECTIONS:
                            _record_dict_expr(kw.arg, kw.value)

    return discovered


def _discover_backend_preference_json_keys(
    src_root: Path = PY_SRC,
) -> set[str]:
    """Scan Python source under ``src_root`` for ``user_preferences`` JSON read/write keys."""
    from src.dalton.llm.model_prefs import DALTON_MODEL_PREF_KEYS
    from src.server.services.llm.user_models import _NEVER_MOVED

    discovered: set[str] = set()
    for py_file in sorted(src_root.rglob("*.py")):
        # Legacy read-only model-reference protection scan for pre-migration agent_preference rows.
        if py_file.as_posix().endswith("src/dalton/database/openclaw_sync.py"):
            continue
        text = py_file.read_text(encoding="utf-8")
        discovered |= _scan_python_preference_json_keys(
            text,
            filename=str(py_file),
            never_moved=frozenset(_NEVER_MOVED),
            dalton_model_keys=frozenset(DALTON_MODEL_PREF_KEYS),
        )
    return discovered


# ---------------------------------------------------------------------------
# Mounted FastAPI route & Pydantic request-body field discovery
# ---------------------------------------------------------------------------


def _discover_endpoint_validated_models(endpoint: Any) -> list[type[BaseModel]]:
    """Inspect an unwrapped FastAPI endpoint's AST for ``Model.model_validate(...)``
    (or ``Model.parse_obj(...)``) calls so ``dict[str, Any]`` request bodies that
    validate through a Pydantic model are guarded identically to typed signatures."""
    fn = inspect.unwrap(endpoint)
    try:
        src = textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError):
        return []
    tree = ast.parse(src)
    local_imports: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name != "*":
                    local_imports[alias.asname or alias.name] = (
                        node.module,
                        alias.name,
                    )
    try:
        closure = inspect.getclosurevars(fn)
        nonlocals = closure.nonlocals
    except (TypeError, ValueError):
        nonlocals = {}
    fn_globals = getattr(fn, "__globals__", {})

    models: list[type[BaseModel]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("model_validate", "parse_obj")
            and isinstance(node.func.value, ast.Name)
        ):
            name = node.func.value.id
            obj: Any = None
            if name in local_imports:
                mod_name, attr_name = local_imports[name]
                obj = getattr(importlib.import_module(mod_name), attr_name, None)
            elif name in nonlocals:
                obj = nonlocals[name]
            elif name in fn_globals:
                obj = fn_globals[name]
            if isinstance(obj, type) and issubclass(obj, BaseModel) and obj not in models:
                models.append(obj)
    return models


def _extract_dalton_put_patch_route_fields(
    app: FastAPI,
) -> dict[tuple[str, str], set[str] | None]:
    """Extract mounted Dalton ``PUT``/``PATCH`` routes and their recursive Pydantic
    request body fields — covering both typed signature models and in-handler
    ``Model.model_validate(...)`` calls on ``dict`` bodies.

    Returns ``{(method, path): set_of_body_field_paths | None}``, where ``None``
    indicates the endpoint accepts no Pydantic ``BaseModel`` body (e.g. raw binary
    upload).
    """
    found: dict[tuple[str, str], set[str] | None] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not (route.path.startswith("/api/v1/dalton") or "/dalton/" in route.path):
            continue
        methods = set(route.methods) & {"PUT", "PATCH"}
        if not methods:
            continue
        models: list[type[BaseModel]] = []
        for param in route.dependant.body_params:
            for model_cls in _unwrap_pydantic_model_types(param.field_info.annotation):
                if model_cls not in models:
                    models.append(model_cls)
        for model_cls in _discover_endpoint_validated_models(route.endpoint):
            if model_cls not in models:
                models.append(model_cls)

        body_fields: set[str] | None = None
        if models:
            body_fields = set()
            for model_cls in models:
                body_fields |= _extract_pydantic_model_fields_recursive(model_cls)

        for method in methods:
            key = (method, route.path)
            if key in found and found[key] is not None and body_fields is not None:
                found[key] = found[key] | body_fields
            else:
                found[key] = body_fields
    return found


def _discover_field_validator_submodels(
    model_cls: type[BaseModel],
) -> dict[str, list[type[BaseModel]]]:
    """Inspect ``@field_validator`` methods on ``model_cls`` (and its bases) to find
    independently validated Pydantic models such as ``PriceTriggerConfig(**v)``."""
    found: dict[str, list[type[BaseModel]]] = {}
    for cls in model_cls.__mro__:
        if not (isinstance(cls, type) and issubclass(cls, BaseModel)) or cls is BaseModel:
            continue
        mod = inspect.getmodule(cls)
        mod_globals = vars(mod) if mod is not None else {}
        for attr_val in cls.__dict__.values():
            fn = getattr(attr_val, "__func__", attr_val)
            if not inspect.isfunction(fn):
                continue
            try:
                src = textwrap.dedent(inspect.getsource(fn))
            except (OSError, TypeError):
                continue
            tree = ast.parse(src)
            if not tree.body or not isinstance(tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            fdef = tree.body[0]
            validated_fields: list[str] = []
            for dec in fdef.decorator_list:
                if isinstance(dec, ast.Call):
                    df = dec.func
                    dname = (
                        df.attr
                        if isinstance(df, ast.Attribute)
                        else (df.id if isinstance(df, ast.Name) else "")
                    )
                    if dname == "field_validator":
                        for arg in dec.args:
                            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                                validated_fields.append(arg.value)
            if not validated_fields:
                continue
            fn_globals = {**mod_globals, **getattr(fn, "__globals__", {})}
            try:
                nonlocals = inspect.getclosurevars(fn).nonlocals
            except (TypeError, ValueError):
                nonlocals = {}
            sub_models: list[type[BaseModel]] = []
            for node in ast.walk(fdef):
                if isinstance(node, ast.Call):
                    target_name: str | None = None
                    if isinstance(node.func, ast.Name):
                        target_name = node.func.id
                    elif (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("model_validate", "parse_obj")
                        and isinstance(node.func.value, ast.Name)
                    ):
                        target_name = node.func.value.id
                    if target_name is None:
                        continue
                    obj = nonlocals.get(target_name, fn_globals.get(target_name))
                    if (
                        isinstance(obj, type)
                        and issubclass(obj, BaseModel)
                        and obj not in sub_models
                    ):
                        sub_models.append(obj)
            for vf in validated_fields:
                if sub_models:
                    bucket = found.setdefault(vf, [])
                    for sm in sub_models:
                        if sm not in bucket:
                            bucket.append(sm)
    return found


def _extract_automation_writable_fields(
    request_models: tuple[type[BaseModel], ...],
    *,
    editable_fields: tuple[str, ...] = (),
    file_field_aliases: dict[str, str] | None = None,
    extra_field_submodels: dict[str, type[BaseModel]] | None = None,
    skip_fields: frozenset[str] = _AUTOMATION_NON_SETTING_FIELDS,
) -> set[str]:
    """Extract canonical ``automation.<storage_path>`` keys from automation request
    models, their nested Pydantic fields (e.g. ``DeliveryConfig``), independently
    validated sub-models (e.g. ``PriceTriggerConfig``), and virtual file editable
    fields."""
    validator_submodels: dict[str, list[type[BaseModel]]] = {}
    for m in request_models:
        for fname, sms in _discover_field_validator_submodels(m).items():
            bucket = validator_submodels.setdefault(fname, [])
            for sm in sms:
                if sm not in bucket:
                    bucket.append(sm)
    for fname, sm in (extra_field_submodels or {}).items():
        bucket = validator_submodels.setdefault(fname, [])
        if sm not in bucket:
            bucket.append(sm)

    expanded_containers: set[str] = set()
    fields: set[str] = set()
    for m in request_models:
        for fname, finfo in m.model_fields.items():
            if fname in skip_fields:
                continue
            sub_models = list(_unwrap_pydantic_model_types(finfo.annotation))
            for sm in validator_submodels.get(fname, ()):
                if sm not in sub_models:
                    sub_models.append(sm)
            if sub_models:
                expanded_containers.add(fname)
                for sm in sub_models:
                    fields |= _extract_pydantic_model_fields_recursive(sm, prefix=fname)
            else:
                fields.add(fname)

    # Map virtual file aliases (`thread`, `delivery`) back to canonical model fields.
    file_alias_to_model: dict[str, set[str]] = {}
    for model_field, file_field in (file_field_aliases or {}).items():
        file_alias_to_model.setdefault(file_field, set()).add(model_field)

    for ef in editable_fields:
        mapped = file_alias_to_model.get(ef, {ef})
        for mf in mapped:
            if mf in skip_fields or mf in expanded_containers:
                continue
            fields.add(mf)

    return {f"automation.{f}" for f in fields}


def _discover_mounted_dalton_routes_across_profiles() -> (
    dict[tuple[str, str], set[str] | None]
):
    """Assemble ``src.server.app.setup.app`` under every :class:`DeploymentProfile`
    and union the final mounted Dalton ``PUT``/``PATCH`` routes and body fields."""
    import src.dalton.app as dalton_app_mod
    import src.server.app.setup as setup_mod

    orig_app_env = os.environ.get("APP_ENV")
    orig_profile = os.environ.get("DALTON_PROFILE")
    merged: dict[tuple[str, str], set[str] | None] = {}
    try:
        for profile in DeploymentProfile:
            os.environ.pop("APP_ENV", None)
            os.environ["DALTON_PROFILE"] = profile.value
            importlib.reload(dalton_app_mod)
            importlib.reload(setup_mod)
            per_profile = _extract_dalton_put_patch_route_fields(setup_mod.app)
            for route_key, fields in per_profile.items():
                if route_key not in merged:
                    merged[route_key] = set(fields) if fields is not None else None
                elif fields is not None:
                    if merged[route_key] is None:
                        merged[route_key] = set(fields)
                    else:
                        merged[route_key] |= fields
    finally:
        if orig_app_env is None:
            os.environ.pop("APP_ENV", None)
        else:
            os.environ["APP_ENV"] = orig_app_env
        if orig_profile is None:
            os.environ.pop("DALTON_PROFILE", None)
        else:
            os.environ["DALTON_PROFILE"] = orig_profile
        importlib.reload(dalton_app_mod)
        importlib.reload(setup_mod)
    return merged


def _assert_dalton_routes_and_fields_registered(
    live_route_fields: dict[tuple[str, str], set[str] | None],
    registry_items: list[dict[str, Any]],
) -> None:
    """Verify every mounted Dalton PUT/PATCH route AND every Pydantic request body field
    on those routes is registered in ``settings_registry.yaml``."""
    live_routes = set(live_route_fields)
    registered_routes: set[tuple[str, str]] = set()
    registered_fields_by_route: dict[tuple[str, str], set[str]] = {}

    for item in registry_items:
        raw_routes = item.get("routes") or []
        raw_route_fields = item.get("route_fields")
        item_fields: set[str] = set()
        if raw_route_fields is not None:
            assert isinstance(raw_route_fields, list), (
                f"Entry {item['key']!r}: route_fields must be a list"
            )
            item_fields = {str(f).strip() for f in raw_route_fields if str(f).strip()}

        for raw_route in raw_routes:
            parts = str(raw_route).strip().split(" ", 1)
            assert len(parts) == 2 and parts[0] in ("PUT", "PATCH"), (
                f"Entry {item['key']!r} has invalid route spec {raw_route!r}"
            )
            rkey = (parts[0], parts[1])
            registered_routes.add(rkey)
            registered_fields_by_route.setdefault(rkey, set()).update(item_fields)

    setting_routes = live_routes - NON_SETTING_DALTON_ROUTES
    unregistered = sorted(setting_routes - registered_routes)
    assert not unregistered, (
        f"发现未登记的 Dalton PUT/PATCH 设置接口：{unregistered}\n{FAILURE_GUIDANCE}"
    )

    stale_registered = sorted(registered_routes - live_routes)
    assert not stale_registered, (
        f"settings_registry.yaml 中存在已失效的 Dalton 路由：{stale_registered}"
    )
    stale_non_setting = sorted(NON_SETTING_DALTON_ROUTES - live_routes)
    assert not stale_non_setting, (
        f"NON_SETTING_DALTON_ROUTES 中存在已失效的路由：{stale_non_setting}"
    )

    # Check request body fields on every setting route that uses a Pydantic model.
    for rkey in sorted(setting_routes):
        raw_fields = live_route_fields[rkey]
        reg_fields = registered_fields_by_route.get(rkey, set())
        if raw_fields is None:
            assert not reg_fields, (
                f"路由 {rkey[0]} {rkey[1]} 无 Pydantic 请求体，不应声明 route_fields={sorted(reg_fields)}"
            )
            continue
        protocol_fields = _DALTON_ROUTE_PROTOCOL_FIELDS.get(rkey, frozenset())
        setting_fields = raw_fields - protocol_fields
        missing_fields = sorted(setting_fields - reg_fields)
        assert not missing_fields, (
            f"路由 {rkey[0]} {rkey[1]} 发现未登记的请求体可写字段：{missing_fields}\n"
            f"{FAILURE_GUIDANCE}"
        )
        stale_fields = sorted(reg_fields - setting_fields)
        assert not stale_fields, (
            f"路由 {rkey[0]} {rkey[1]} 在 settings_registry.yaml 中存在失效的 route_fields：{stale_fields}"
        )


# ---------------------------------------------------------------------------
# Tests: Registry schema & Chief of Staff tool validity
# ---------------------------------------------------------------------------


def test_settings_registry_schema_and_profile_capability_contract():
    """Every entry in settings_registry.yaml has valid schema, unique key,
    valid cos decision, and valid DeploymentProfile / CapabilityId names."""
    items = _load_registry()
    valid_profiles = {p.value for p in DeploymentProfile}
    valid_capabilities = {c.value for c in CapabilityId}

    seen_keys: set[str] = set()
    for idx, item in enumerate(items):
        assert isinstance(item, dict), f"Item #{idx} must be a mapping"
        for field in ("key", "stored_in", "web_entry", "cos", "profiles"):
            assert item.get(field), (
                f"Registry entry {item.get('key', f'#{idx}')!r} missing required field {field!r}"
            )

        key = str(item["key"]).strip()
        assert key == item["key"] and " " not in key, f"Invalid setting key: {key!r}"
        assert key not in seen_keys, f"Duplicate setting key in settings_registry.yaml: {key!r}"
        seen_keys.add(key)

        cos = str(item["cos"]).strip()
        if cos == "missing":
            pass
        elif cos.startswith("tool:"):
            assert len(cos.split(":", 1)[1].strip()) > 0, (
                f"Entry {key!r}: 'tool:' must name a Chief of Staff tool"
            )
        elif cos.startswith("excluded:"):
            assert len(cos.split(":", 1)[1].strip()) > 0, (
                f"Entry {key!r}: 'excluded:' must state a reason"
            )
        else:
            pytest.fail(
                f"Entry {key!r} has invalid cos={cos!r}; "
                "must be 'tool:<name>', 'missing', or 'excluded:<reason>'"
            )

        profiles = item["profiles"]
        assert isinstance(profiles, list) and profiles, (
            f"Entry {key!r}: profiles must be a non-empty list"
        )
        unknown_profiles = set(profiles) - valid_profiles
        assert not unknown_profiles, (
            f"Entry {key!r}: unknown profiles {sorted(unknown_profiles)}; "
            f"allowed: {sorted(valid_profiles)}"
        )

        if "capability" in item and item["capability"] is not None:
            cap = item["capability"]
            caps = [cap] if isinstance(cap, str) else list(cap)
            unknown_caps = set(caps) - valid_capabilities
            assert not unknown_caps, (
                f"Entry {key!r}: unknown capability {sorted(unknown_caps)}; "
                f"allowed: {sorted(valid_capabilities)}"
            )


def test_settings_registry_tool_references_and_capabilities_match_runtime():
    """Every `tool:<name>` entry names a real Chief of Staff tool or virtual file backend
    and declares any CapabilityId required by ``DALTON_TOOL_REGISTRY``."""
    from src.dalton.tools import DALTON_TOOL_REGISTRY
    from src.tools.secretary.chief_of_staff import CHIEF_OF_STAFF_TOOLS

    cos_tools: set[str] = {t.name for t in CHIEF_OF_STAFF_TOOLS}
    tool_caps: dict[str, set[str]] = {}
    tool_min_tiers: dict[str, str] = {}
    for reg in DALTON_TOOL_REGISTRY:
        if "chief_of_staff" in reg.roles:
            for tname in reg.tool_names:
                cos_tools.add(tname)
                tool_min_tiers[tname] = reg.min_tier
                if reg.capability is not None:
                    req = (
                        reg.capability
                        if isinstance(reg.capability, tuple)
                        else (reg.capability,)
                    )
                    tool_caps.setdefault(tname, set()).update(c.value for c in req)

    for item in _load_registry():
        cos = str(item["cos"])
        if not cos.startswith("tool:"):
            continue
        target = cos.split(":", 1)[1].strip()
        if target.startswith(("edit_file(", "write_file(")) and target.endswith(")"):
            inner_path = target[target.index("(") + 1 : -1]
            assert inner_path.startswith(_ALLOWED_VIRTUAL_FILE_PREFIXES), (
                f"Entry {item['key']!r} references unknown virtual file path {inner_path!r}"
            )
            continue
        assert target in cos_tools, (
            f"Entry {item['key']!r} references unknown Chief of Staff tool {target!r}; "
            f"known CoS tools: {sorted(cos_tools)}"
        )
        required_caps = tool_caps.get(target, set())
        if required_caps:
            declared = item.get("capability")
            declared_set = (
                {declared}
                if isinstance(declared, str)
                else set(declared or ())
            )
            assert required_caps & declared_set, (
                f"Entry {item['key']!r} uses tool {target!r} which requires capability "
                f"{sorted(required_caps)}, but entry declares capability={declared!r}"
            )
        min_tier = tool_min_tiers.get(target, "any")
        if min_tier != "any":
            note_text = str(item.get("note") or "")
            assert note_text.strip(), (
                f"Entry {item['key']!r} uses tool {target!r} with min_tier={min_tier!r}; "
                "please document member scope in 'note'"
            )


# ---------------------------------------------------------------------------
# Tests: Frontend Zod preference schemas & thread/other preference keys
# ---------------------------------------------------------------------------


def test_frontend_zod_preference_schemas_are_registered():
    """Scan frontend Zod preference/thread schemas (including sub-schema field paths),
    preference interfaces, and all ``other_preference`` / ``model_preference`` call
    sites in ``web/src/``."""
    registry_items = _load_registry()

    # 1. Auto-discover every Zod file under web/src/ touching preference/thread storage
    #    in non-comment code.
    discovered_zod_files = _discover_web_pref_zod_files()
    unknown_zod_files = sorted(discovered_zod_files - set(_KNOWN_FRONTEND_PREF_ZOD_FILES))
    assert not unknown_zod_files, (
        f"发现未纳入守卫的前端偏好/线程 Zod 文件：{unknown_zod_files}\n"
        "请将其纳入 _KNOWN_FRONTEND_PREF_ZOD_FILES 并在 settings_registry.yaml 登记相关设置。"
    )

    global_consts = _collect_web_exported_string_consts()

    # 2. Every entry in _KNOWN_FRONTEND_PREF_ZOD_FILES must run field extraction and
    #    verify its fields (including Zod sub-schema field paths) against the registry.
    for rel_path, kind in _KNOWN_FRONTEND_PREF_ZOD_FILES.items():
        file_path = WEB_SRC / rel_path
        assert file_path.is_file(), f"Missing known frontend Zod file: {rel_path}"
        _verify_frontend_pref_zod_file(
            rel_path,
            kind,
            file_path.read_text(encoding="utf-8"),
            global_consts=global_consts,
            registry_items=registry_items,
        )

    # 3. ModelPreferences / AccountTuning / ModelProfile interfaces + MOVED_KEYS /
    #    MODEL_PREF_KEYS / TUNING_FIELDS
    model_prefs_ts = WEB_SRC / "lib" / "modelPreferences.ts"
    model_tuning_ts = WEB_SRC / "lib" / "modelTuning.ts"
    model_prefs_src = model_prefs_ts.read_text(encoding="utf-8")
    model_tuning_src = model_tuning_ts.read_text(encoding="utf-8")

    zod_schemas = _extract_all_zod_object_fields_in_source(
        model_prefs_src, extra_string_consts=global_consts, recursive=True
    )
    assert zod_schemas.get("DaltonModelPrefsSchema"), (
        "DaltonModelPrefsSchema must define keys"
    )
    zod_model_fields: set[str] = set()
    for fields in zod_schemas.values():
        zod_model_fields |= fields

    iface_fields = _extract_ts_interface_fields(
        {"modelPreferences.ts": model_prefs_src, "modelTuning.ts": model_tuning_src},
        "ModelPreferences",
    )
    assert "preferred_model" in iface_fields
    assert "dalton_notify_routes" in iface_fields
    assert "prompt_guidance" in iface_fields
    assert "cold_compaction" in iface_fields

    moved_keys = _extract_string_list_const(
        model_prefs_ts, "MOVED_KEYS", extra_string_consts=global_consts
    )
    model_pref_keys = _extract_string_list_const(
        model_prefs_ts, "MODEL_PREF_KEYS", extra_string_consts=global_consts
    )
    tuning_fields = _extract_string_list_const(
        model_tuning_ts, "TUNING_FIELDS", extra_string_consts=global_consts
    )

    frontend_model_keys = {
        f"user_preferences.model_preference.{k}"
        for k in (zod_model_fields | iface_fields | moved_keys | model_pref_keys | tuning_fields)
    }
    _assert_all_registered(
        frontend_model_keys,
        source="web/src/lib/modelPreferences.ts (Zod schemas / ModelPreferences / MODEL_PREF_KEYS) & modelTuning.ts",
    )

    # 4. Frontend ThreadField union (and any Zod object schema in threadFieldMutations.ts)
    thread_fields_ts = WEB_SRC / "lib" / "threadFieldMutations.ts"
    thread_fields_src = thread_fields_ts.read_text(encoding="utf-8")
    thread_field_names = _extract_type_string_union_from_source(
        thread_fields_src, "ThreadField"
    )
    for schema_fields in _extract_all_zod_object_fields_in_source(
        thread_fields_src, extra_string_consts=global_consts, recursive=True
    ).values():
        thread_field_names |= schema_fields
    thread_fields = {f"thread.{f}" for f in thread_field_names}
    assert thread_fields, "ThreadField union must define thread setting fields"
    _assert_all_registered(
        thread_fields,
        source="web/src/lib/threadFieldMutations.ts (ThreadField)",
    )

    # 5. Full web/src/ scan for other_preference & model_preference readers/writers
    other_pref_keys, direct_model_keys = _discover_web_preference_keys()
    assert "user_preferences.other_preference.app_scopes" in other_pref_keys
    assert "user_preferences.other_preference.search_provider" in other_pref_keys
    assert "user_preferences.other_preference.search_depth" in other_pref_keys
    assert "user_preferences.other_preference.voice_input_enabled" in other_pref_keys
    _assert_all_registered(
        other_pref_keys,
        source="web/src/** (usePreferenceKey / readOtherPreference / otherPreferencePatch / other_preference literals)",
    )
    _assert_all_registered(
        direct_model_keys,
        source="web/src/** (model_preference literals)",
    )


# ---------------------------------------------------------------------------
# Tests: Backend user_preferences & users keys
# ---------------------------------------------------------------------------


def test_backend_user_and_preferences_keys_are_registered():
    """Scan backend Pydantic models (including ``ModelPreference.model_fields``),
    ``update_user`` params, canonical runtime key sets, and ``user_preferences``
    JSON read/write/merge paths in ``src/`` without hand-written key lists."""
    from src.dalton.llm.model_prefs import DALTON_MODEL_PREF_KEYS
    from src.llms.preferences import MOVED_MODEL_KEYS, TUNING_FIELDS
    from src.server.database.user import DEFAULT_OTHER_PREFERENCE, update_user
    from src.server.models.profile_vocabulary import STRUCTURED_PREFERENCES
    from src.server.models.user import UserPreferencesUpdate, UserUpdate
    from src.server.services.llm.user_models import _NEVER_MOVED

    discovered: set[str] = set()

    # 1. UserUpdate Pydantic fields + update_user DB helper params
    for field in UserUpdate.model_fields:
        discovered.add(f"user.{field}")
    for param in inspect.signature(update_user).parameters:
        if param not in _USER_UPDATE_INTERNAL_PARAMS:
            discovered.add(f"user.{param}")

    # 2. Every preference section Pydantic model on UserPreferencesUpdate (including ModelPreference)
    discovered |= _extract_pydantic_preference_fields(UserPreferencesUpdate)

    # 3. Canonical runtime collections defined in production modules
    for section, vocab_map in STRUCTURED_PREFERENCES.items():
        for key in vocab_map:
            discovered.add(f"user_preferences.{section}.{key}")
    for key in (*MOVED_MODEL_KEYS, *TUNING_FIELDS, *DALTON_MODEL_PREF_KEYS):
        discovered.add(f"user_preferences.model_preference.{key}")
    for key in (*_NEVER_MOVED, *DEFAULT_OTHER_PREFERENCE.keys()):
        discovered.add(f"user_preferences.other_preference.{key}")

    # 4. AST scan of all user_preferences JSON read/write/merge paths in src/
    json_path_keys = _discover_backend_preference_json_keys(PY_SRC)
    # Sanity-check that open JSON keys are discovered directly from code paths (not hardcoded)
    assert "user_preferences.model_preference.profiles" in json_path_keys
    assert "user_preferences.model_preference.default_model_scope" in json_path_keys
    assert "user_preferences.model_preference.background_follows" in json_path_keys
    assert "user_preferences.agent_preference.subagent_models" in json_path_keys
    assert "user_preferences.other_preference.subagents_default" in json_path_keys
    assert "user_preferences.other_preference.artifact_comment_destination" in json_path_keys
    assert "user_preferences.other_preference.skills" in json_path_keys
    assert "user_preferences.other_preference.account_setup_completed_at" in json_path_keys
    assert "user_preferences.other_preference.dalton_team_base_url_classes" in json_path_keys
    discovered |= json_path_keys

    _assert_all_registered(
        discovered,
        source="backend UserUpdate / UserPreferencesUpdate (incl. ModelPreference) & src/ user_preferences JSON paths",
    )


# ---------------------------------------------------------------------------
# Tests: Thread writable fields
# ---------------------------------------------------------------------------


def test_thread_writable_fields_are_registered():
    """Every writable field on ThreadUpdateRequest, ThreadVisibilityRequest,
    ThreadShareRequest, and SharePermissions is registered."""
    from src.server.models.conversation import (
        ThreadShareRequest,
        ThreadUpdateRequest,
        ThreadVisibilityRequest,
    )

    discovered = {
        f"thread.{f}"
        for f in ThreadUpdateRequest.model_fields
        if f not in _THREAD_NON_SETTING_FIELDS
    }
    for f in ThreadVisibilityRequest.model_fields:
        discovered.add(f"thread.{f}")

    for field_name, field_info in ThreadShareRequest.model_fields.items():
        sub_model = _unwrap_pydantic_model_type(field_info.annotation)
        if field_name == "permissions" and sub_model is not None:
            for perm_field in sub_model.model_fields:
                discovered.add(f"thread.share_permissions.{perm_field}")
        else:
            discovered.add(f"thread.{field_name}")

    _assert_all_registered(
        discovered,
        source="src/server/models/conversation.py (ThreadUpdateRequest / ThreadVisibilityRequest / ThreadShareRequest / SharePermissions)",
    )


# ---------------------------------------------------------------------------
# Tests: Automation writable fields & virtual file / pause-resume status
# ---------------------------------------------------------------------------


def test_automation_writable_fields_are_registered():
    """Every writable field on AutomationCreate, AutomationUpdate, CompanyScriptUpdate,
    nested/independently validated sub-models (``DeliveryConfig``, ``PriceTriggerConfig``),
    and ``.agents/user/automations/*.json`` (including ``status`` for pause/resume)
    is registered by storage path."""
    from src.server.app import automations as automations_router_mod
    from src.server.models.automation import (
        AutomationCreate,
        AutomationUpdate,
        CompanyScriptUpdate,
    )
    from src.server.services.automations import file as automation_file

    discovered = _extract_automation_writable_fields(
        (AutomationCreate, AutomationUpdate, CompanyScriptUpdate),
        editable_fields=automation_file._EDITABLE,
        file_field_aliases=automation_file._FILE_FIELD,
    )

    # Sanity-check independently validated PriceTriggerConfig & DeliveryConfig sub-paths
    assert "automation.trigger_config.symbol" in discovered
    assert "automation.trigger_config.conditions.type" in discovered
    assert "automation.trigger_config.retrigger.mode" in discovered
    assert "automation.delivery_config.methods" in discovered

    # Verify pause / resume HTTP endpoints are present and represented by automation.status
    post_paths = {
        r.path
        for r in automations_router_mod.router.routes
        if isinstance(r, APIRoute) and "POST" in r.methods
    }
    assert any(p.endswith("/automations/{automation_id}/pause") for p in post_paths)
    assert any(p.endswith("/automations/{automation_id}/resume") for p in post_paths)
    assert "automation.status" in discovered

    _assert_all_registered(
        discovered,
        source="src/server/models/automation.py (incl. PriceTriggerConfig / DeliveryConfig) & file.py (_EDITABLE)",
    )


# ---------------------------------------------------------------------------
# Tests: Workspace, Computer, Desktop local, and Deployment settings
# ---------------------------------------------------------------------------


def test_workspace_computer_and_deployment_settings_are_registered():
    """Workspace writable metadata (including coverage tracking), computer browser
    settings, desktop local notification toggles, and deployment_settings catalog
    keys are registered."""
    from src.dalton.app.router import WorkspaceTrackingUpdateRequest
    from src.dalton.tools.ingest_tools import AUTO_INGEST_CONFIG_KEY
    from src.server.models.computer import BrowserChoicesUpdate
    from src.server.models.workspace import WorkspaceUpdate
    from src.server.services.setup_settings import catalog

    discovered: set[str] = set()

    for field in WorkspaceUpdate.model_fields:
        if field == "config":
            # Generic config bag; specific named user-facing keys are registered individually.
            continue
        discovered.add(f"workspace.{field}")
    discovered.add(f"workspace.{AUTO_INGEST_CONFIG_KEY}")

    # Workspace coverage tracking (POST /api/v1/dalton/tracking/{workspace_id})
    tracking_fields = set(WorkspaceTrackingUpdateRequest.model_fields) - {
        "override_pause_versions"
    }
    assert tracking_fields == {"enabled", "tier"}, (
        f"WorkspaceTrackingUpdateRequest 新增了未核对字段：{sorted(tracking_fields)}\n"
        f"{FAILURE_GUIDANCE}"
    )
    discovered.add("dalton.workspace.coverage_tracking")

    for field in BrowserChoicesUpdate.model_fields:
        discovered.add(f"computer.browser.{field}")

    # Desktop local notification toggles (desktop/src/desktopSettings.js)
    desktop_settings_js = DESKTOP_SRC / "desktopSettings.js"
    assert desktop_settings_js.is_file(), f"Missing {desktop_settings_js}"
    notif_kinds = _extract_string_list_const(desktop_settings_js, "NOTIFICATION_KINDS")
    assert notif_kinds, "NOTIFICATION_KINDS must define desktop notification toggles"
    for kind in notif_kinds:
        discovered.add(f"desktop.notifications.{kind}")

    for name in (
        catalog.PROVIDER_SETTING,
        catalog.INSTALL_GROUPS_SETTING,
        catalog.COMPLETED_SETTING,
        *(spec.name for spec in catalog.key_specs()),
    ):
        discovered.add(f"deployment_settings.{name}")

    _assert_all_registered(
        discovered,
        source="WorkspaceUpdate / WorkspaceTrackingUpdateRequest / BrowserChoicesUpdate / desktopSettings.js / setup_settings.catalog",
    )


# ---------------------------------------------------------------------------
# Tests: Dalton mounted PUT / PATCH routes & request body fields across profiles
# ---------------------------------------------------------------------------


def test_dalton_put_patch_routes_and_body_fields_are_registered():
    """Every Dalton PUT/PATCH route mounted on ``src.server.app.setup.app`` across
    all deployment profiles — and every recursive Pydantic request body field on
    each setting route (signature models AND in-handler ``model_validate()``) —
    must be registered in ``settings_registry.yaml``."""
    live_route_fields = _discover_mounted_dalton_routes_across_profiles()
    assert live_route_fields, "Expected mounted Dalton PUT/PATCH routes to be discovered"
    # Sanity-check that dict+model_validate() routes and nested fields were extracted.
    assert "curated_setup" in (
        live_route_fields.get(("PUT", "/api/v1/dalton/team-models/curated")) or set()
    )
    assert "base_url" in (
        live_route_fields.get(
            ("PUT", "/api/v1/dalton/team-models/provider-variants/{provider_slug}")
        )
        or set()
    )
    assert "pricing.input_tiers.rate" in (
        live_route_fields.get(("PUT", "/api/v1/dalton/team-models/{model_key}"))
        or set()
    )
    assert "scopes.space_id" in (
        live_route_fields.get(("PUT", "/api/v1/dalton/feishu/read-scopes")) or set()
    )
    _assert_dalton_routes_and_fields_registered(live_route_fields, _load_registry())


# ---------------------------------------------------------------------------
# Meta-tests: Prove every guard actually fails on newly added unregistered settings
# ---------------------------------------------------------------------------


def test_guard_fails_on_unregistered_pydantic_model_preference_field():
    """Adding a field to ``ModelPreference`` (or another preference model) fails the guard."""

    class FakeModelPreference(BaseModel):
        unregistered_model_knob: str | None = None

    class FakeUserPreferencesUpdate(BaseModel):
        model_preference: FakeModelPreference | None = None

    discovered = _extract_pydantic_preference_fields(FakeUserPreferencesUpdate)
    assert discovered == {"user_preferences.model_preference.unregistered_model_knob"}

    with pytest.raises(AssertionError) as exc_info:
        _assert_all_registered(discovered, source="fake ModelPreference")
    msg = str(exc_info.value)
    assert "unregistered_model_knob" in msg
    assert "在 settings_registry.yaml 登记，并决定研究总监能否修改" in msg


def test_guard_fails_on_unregistered_backend_preference_json_paths():
    """Adding a backend read, inline write, ``dict.update()`` merge, ``copy.deepcopy()``,
    conditional expression, or variable ``upsert_user_preferences`` write fails the guard."""
    fake_py = """
import copy
from src.server.database.user import upsert_user_preferences

def read_pref(prefs: dict):
    other = prefs.get("other_preference") or {}
    ap = copy.deepcopy(
        prefs.get("agent_preference") if isinstance(prefs.get("agent_preference"), dict) else {}
    )
    return other.get("unregistered_backend_read_key"), ap.get("unregistered_deepcopy_key")

async def write_pref(user_id: str, model_pref: dict):
    model_pref.update({"unregistered_update_dict_key": True})
    model_pref.update(unregistered_update_kw_key="v")
    model_pref |= {"unregistered_bitor_key": 1}
    extra_patch = {"unregistered_var_upsert_key": "x"}
    await upsert_user_preferences(
        user_id,
        model_preference=model_pref,
        other_preference=extra_patch,
    )
"""
    discovered = _scan_python_preference_json_keys(fake_py, filename="fake_pref.py")
    expected = {
        "user_preferences.other_preference.unregistered_backend_read_key",
        "user_preferences.agent_preference.unregistered_deepcopy_key",
        "user_preferences.model_preference.unregistered_update_dict_key",
        "user_preferences.model_preference.unregistered_update_kw_key",
        "user_preferences.model_preference.unregistered_bitor_key",
        "user_preferences.other_preference.unregistered_var_upsert_key",
    }
    assert expected <= discovered

    with pytest.raises(AssertionError) as exc_info:
        _assert_all_registered(discovered, source="fake backend JSON paths")
    msg = str(exc_info.value)
    for key in expected:
        assert key in msg


def test_guard_fails_on_unregistered_frontend_zod_schema_and_rejects_unparseable():
    """Frontend Zod scanner catches direct, spread, computed, extended, and nested fields,
    fails the guard on unregistered ones, and errors explicitly on unparseable constructs."""
    fake_ts = """
import * as z from 'zod/mini';
const EXTRA_KEY = 'unregistered_computed_pref';
const ExtraFields = {
  unregistered_spread_pref: z.boolean(),
};
const NestedGroup = z.object({
  unregistered_nested_leaf: z.string(),
});
export const FakePrefsSchema = z.object({
  ...ExtraFields,
  [EXTRA_KEY]: z.string(),
  unregistered_direct_pref: z.number(),
  nested_group: NestedGroup,
}).extend({
  unregistered_extended_pref: z.boolean(),
});
"""
    schemas = _extract_all_zod_object_fields_in_source(fake_ts, recursive=True)
    fields = schemas["FakePrefsSchema"]
    assert fields == {
        "unregistered_spread_pref",
        "unregistered_computed_pref",
        "unregistered_direct_pref",
        "nested_group.unregistered_nested_leaf",
        "unregistered_extended_pref",
    }
    discovered = {f"user_preferences.model_preference.{f}" for f in fields}
    with pytest.raises(AssertionError) as exc_info:
        _assert_all_registered(discovered, source="fake Zod schema")
    for expected in fields:
        assert expected in str(exc_info.value)

    unparseable_ts = """
import * as z from 'zod/mini';
export const BrokenSchema = z.object({
  ...buildDynamicFieldsAtRuntime(),
});
"""
    with pytest.raises(AssertionError, match="无法静态解析"):
        _extract_all_zod_object_fields_in_source(unparseable_ts, recursive=True)


def test_guard_fails_on_unregistered_frontend_subschema_field_and_ignores_comments():
    """Adding a field to a real Zod sub-schema (``custom_dashboards`` or ``onboarding``)
    fails the guard; adding a comment mentioning ``other_preference`` to an unrelated
    dashboard Zod schema file does NOT trigger a false positive; unknown whitelist
    ``kind`` fails explicitly."""
    registry_items = _load_registry()

    # 1. Adding a field to the real custom_dashboards Zod schema fails the guard
    saved_src = (WEB_SRC / "lib" / "dashboards" / "saved.ts").read_text(encoding="utf-8")
    mutated_saved = saved_src.replace(
        "artifactId: id });",
        "artifactId: id, unregistered_dashboard_flag: z.boolean() });",
        1,
    )
    assert mutated_saved != saved_src
    with pytest.raises(AssertionError, match="unregistered_dashboard_flag"):
        _verify_frontend_pref_zod_file(
            "lib/dashboards/saved.ts",
            "other_preference_subschema:custom_dashboards",
            mutated_saved,
            global_consts={},
            registry_items=registry_items,
        )

    # 2. Adding a field to the real OnboardingPrefsSchema fails the guard
    onboarding_src = (
        WEB_SRC / "pages" / "Onboarding" / "onboardingPrefsSchema.ts"
    ).read_text(encoding="utf-8")
    mutated_onboarding = onboarding_src.replace(
        "firstRunAt: epochOrNull,",
        "firstRunAt: epochOrNull,\n  unregistered_onboarding_flag: epochOrNull,",
        1,
    )
    assert mutated_onboarding != onboarding_src
    with pytest.raises(AssertionError, match="unregistered_onboarding_flag"):
        _verify_frontend_pref_zod_file(
            "pages/Onboarding/onboardingPrefsSchema.ts",
            "other_preference_subschema:onboarding",
            mutated_onboarding,
            global_consts={},
            registry_items=registry_items,
        )

    # 3. Adding a comment mentioning `other_preference` to a widget schema file does NOT
    #    falsely flag it as a preference storage Zod file.
    widget_schema_src = (
        WEB_SRC / "pages" / "Dashboard" / "widgets" / "framework" / "configSchemas.ts"
    ).read_text(encoding="utf-8")
    commented_widget_src = (
        "// Stored inside user_preferences.other_preference.dashboard\n"
        + widget_schema_src
    )
    assert not _is_pref_zod_source(commented_widget_src)

    # 4. Unimplemented whitelist `kind` raises explicitly instead of silently skipping
    with pytest.raises(AssertionError, match="未实现提取逻辑"):
        _verify_frontend_pref_zod_file(
            "lib/dashboards/saved.ts",
            "unimplemented_custom_kind",
            saved_src,
            global_consts={},
            registry_items=registry_items,
        )


def test_guard_fails_on_semicolonless_ts_interface_field():
    """TypeScript interface properties without trailing semicolons are all extracted,
    and an unregistered field fails the guard."""
    fake_tuning = """
export interface ModelProfile {
  prompt_guidance?: string | null
  unregistered_profile_knob?: boolean | null
}
export interface AccountTuning extends ModelProfile {
  cold_compaction?: boolean | null
}
"""
    fake_prefs = """
export interface ModelPreferences extends AccountTuning {
  preferred_model?: string | null
  profiles?: Record<
    string,
    ModelProfile | null
  > | null
  unregistered_model_iface_knob?: string | null
}
"""
    fields = _extract_ts_interface_fields(
        {"modelTuning.ts": fake_tuning, "modelPreferences.ts": fake_prefs},
        "ModelPreferences",
    )
    assert {
        "prompt_guidance",
        "unregistered_profile_knob",
        "cold_compaction",
        "preferred_model",
        "profiles",
        "unregistered_model_iface_knob",
    } <= fields

    with pytest.raises(AssertionError) as exc_info:
        _assert_all_registered(
            {f"user_preferences.model_preference.{f}" for f in fields},
            source="fake semicolonless ModelPreferences",
        )
    msg = str(exc_info.value)
    assert "unregistered_profile_knob" in msg
    assert "unregistered_model_iface_knob" in msg


def test_guard_handles_semicolonless_thread_field_and_catches_unregistered():
    """Omitting the semicolon after ``ThreadField`` never leaks subsequent strings,
    while an added unregistered member fails the guard."""
    fake_ts = """
export type ThreadField = 'llm_model' | 'unregistered_thread_opt'

export function threadFieldMutationKey(threadId: string, field?: ThreadField) {
  return field ? ['thread-field', threadId, field] : ['thread-field', threadId];
}
"""
    members = _extract_type_string_union_from_source(fake_ts, "ThreadField")
    assert members == {"llm_model", "unregistered_thread_opt"}
    assert "thread-field" not in members

    with pytest.raises(AssertionError) as exc_info:
        _assert_all_registered({f"thread.{m}" for m in members}, source="fake ThreadField")
    assert "thread.unregistered_thread_opt" in str(exc_info.value)


def test_guard_fails_on_unregistered_frontend_other_preference_hooks_literals_and_regex():
    """Frontend scanner tolerates regex literals with braces and catches ``usePreferenceKey``
    as well as literal ``{ other_preference: { ... } }`` writes."""
    fake_ts = r"""
const BRACE_RE = /[{}()]/g;
const HOOK_KEY = 'unregistered_hook_pref';
export function useDemo() {
  const { value } = usePreferenceKey(HOOK_KEY, (x) => x);
  const patch = { other_preference: { unregistered_literal_pref: true } };
  return { value, patch, BRACE_RE };
}
"""
    other_keys, _ = _scan_ts_preference_calls_and_literals(
        fake_ts, global_string_consts={}, filename="fakeHook.ts"
    )
    assert other_keys == {"unregistered_hook_pref", "unregistered_literal_pref"}

    with pytest.raises(AssertionError) as exc_info:
        _assert_all_registered(
            {f"user_preferences.other_preference.{k}" for k in other_keys},
            source="fake frontend other_preference",
        )
    assert "unregistered_hook_pref" in str(exc_info.value)
    assert "unregistered_literal_pref" in str(exc_info.value)


class _FakeNestedRouteOption(BaseModel):
    unregistered_nested_route_field: str | None = None


class _FakeMonitorPatchBody(BaseModel):
    title: str | None = None
    unregistered_monitor_field: str | None = None
    nested_opt: _FakeNestedRouteOption | None = None


class _FakeDictValidatedBody(BaseModel):
    curated_setup: list[str] | None = None
    unregistered_validated_field: str | None = None
    nested_opt: _FakeNestedRouteOption | None = None


def test_guard_fails_on_unregistered_dalton_route_field_and_ignores_unmounted_routers():
    """Route discovery reads only mounted final paths from FastAPI app (ignoring
    unmounted draft routers and pre-mount sub-router paths), extracts recursive
    nested Pydantic fields as well as in-handler ``model_validate()`` models on
    ``dict`` bodies, and fails on any unregistered writable field."""
    app = FastAPI()
    parent = APIRouter(prefix="/api/v1/dalton")
    sub = APIRouter(prefix="/monitors")
    unmounted_draft = APIRouter(prefix="/api/v1/dalton/draft")

    @sub.patch("/{slug}")
    async def _patch_monitor(slug: str, body: _FakeMonitorPatchBody):
        return {"slug": slug, "body": body}

    @parent.put("/team-models/curated")
    async def _put_curated(body: dict[str, Any]):
        parsed = _FakeDictValidatedBody.model_validate(body)
        return {"parsed": parsed}

    @unmounted_draft.put("/ignored")
    async def _draft_put():
        return {}

    parent.include_router(sub)
    app.include_router(parent)

    discovered = _extract_dalton_put_patch_route_fields(app)
    # Only mounted final paths exist; neither pre-mount `/monitors/{slug}` nor
    # unmounted `/api/v1/dalton/draft/ignored` is collected.
    assert set(discovered) == {
        ("PATCH", "/api/v1/dalton/monitors/{slug}"),
        ("PUT", "/api/v1/dalton/team-models/curated"),
    }
    assert discovered[("PATCH", "/api/v1/dalton/monitors/{slug}")] == {
        "title",
        "unregistered_monitor_field",
        "nested_opt.unregistered_nested_route_field",
    }
    assert discovered[("PUT", "/api/v1/dalton/team-models/curated")] == {
        "curated_setup",
        "unregistered_validated_field",
        "nested_opt.unregistered_nested_route_field",
    }

    fake_registry = [
        {
            "key": "dalton.monitors.settings",
            "routes": ["PATCH /api/v1/dalton/monitors/{slug}"],
            "route_fields": ["title"],
        },
        {
            "key": "dalton.team_models.curated",
            "routes": ["PUT /api/v1/dalton/team-models/curated"],
            "route_fields": ["curated_setup"],
        },
    ]
    live_with_non_settings = dict(discovered)
    for nr in NON_SETTING_DALTON_ROUTES:
        live_with_non_settings[nr] = None

    with pytest.raises(AssertionError) as exc_info:
        _assert_dalton_routes_and_fields_registered(
            live_with_non_settings, fake_registry
        )
    msg = str(exc_info.value)
    assert "unregistered_monitor_field" in msg
    assert "nested_opt.unregistered_nested_route_field" in msg

    # Verify the dict+model_validate() route also fails on its own unregistered fields
    fake_registry[0]["route_fields"] = [
        "title",
        "unregistered_monitor_field",
        "nested_opt.unregistered_nested_route_field",
    ]
    with pytest.raises(AssertionError) as exc_info_2:
        _assert_dalton_routes_and_fields_registered(
            live_with_non_settings, fake_registry
        )
    msg2 = str(exc_info_2.value)
    assert "unregistered_validated_field" in msg2
    assert "nested_opt.unregistered_nested_route_field" in msg2


def test_guard_fails_on_unregistered_automation_price_trigger_config_field():
    """Adding a field to ``PriceTriggerConfig`` (an independently validated sub-model
    checked inside ``@field_validator('trigger_config')``) fails the guard."""
    from pydantic import field_validator

    from src.server.models.automation import PriceTriggerConfig

    class FakePriceTriggerConfig(PriceTriggerConfig):
        unregistered_price_knob: float | None = None

    class FakeAutomationCreate(BaseModel):
        trigger_config: dict[str, Any] | None = None

        @field_validator("trigger_config")
        @classmethod
        def _validate_tc(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
            if v is not None:
                FakePriceTriggerConfig(**v)
            return v

    discovered = _extract_automation_writable_fields((FakeAutomationCreate,))
    assert "automation.trigger_config.unregistered_price_knob" in discovered

    with pytest.raises(AssertionError) as exc_info:
        _assert_all_registered(discovered, source="fake PriceTriggerConfig")
    assert "automation.trigger_config.unregistered_price_knob" in str(exc_info.value)


def test_guard_fails_on_unregistered_desktop_notification_kind(tmp_path: Path):
    """Adding a new notification kind to ``NOTIFICATION_KINDS`` in
    ``desktop/src/desktopSettings.js`` fails the guard."""
    fake_js = tmp_path / "desktopSettings.js"
    fake_js.write_text(
        "const NOTIFICATION_KINDS = ['turn', 'unregistered_desktop_alert'];\n",
        encoding="utf-8",
    )
    kinds = _extract_string_list_const(fake_js, "NOTIFICATION_KINDS")
    assert kinds == {"turn", "unregistered_desktop_alert"}
    with pytest.raises(AssertionError) as exc_info:
        _assert_all_registered(
            {f"desktop.notifications.{k}" for k in kinds},
            source="fake desktopSettings.js",
        )
    assert "desktop.notifications.unregistered_desktop_alert" in str(exc_info.value)
