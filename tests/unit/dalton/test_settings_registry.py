"""Guard tests for ``src/dalton/settings_registry.yaml``.

Every user-configurable setting across frontend preference/Zod schemas, backend
``user_preferences`` / ``users`` models, ``ThreadUpdateRequest``, automation
create/update models, deployment setup settings, and Dalton ``PUT``/``PATCH``
routers must be registered in ``src/dalton/settings_registry.yaml`` with an
explicit Chief of Staff handling decision (``tool:<name>``, ``missing``, or
``excluded:<reason>``).
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import APIRouter
from fastapi.routing import APIRoute

from src.dalton.capabilities import CapabilityId
from src.dalton.profile import DeploymentProfile

REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTRY_PATH = REPO_ROOT / "src" / "dalton" / "settings_registry.yaml"
WEB_SRC = REPO_ROOT / "web" / "src"

FAILURE_GUIDANCE = (
    "请在 settings_registry.yaml 登记，并决定研究总监能否修改"
    "（tool:<工具名> / missing / excluded:<原因>）。"
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

# Virtual JSON/Markdown file prefixes editable by Chief of Staff through file tools.
_ALLOWED_VIRTUAL_FILE_PREFIXES: tuple[str, ...] = (
    ".agents/user/profile/",
    ".agents/user/automations/",
    ".agents/user/channels/",
    ".agents/user/agents/",
)


# ---------------------------------------------------------------------------
# Registry loading
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


def _registry_keys() -> set[str]:
    return {str(item["key"]) for item in _load_registry()}


def _assert_all_registered(discovered: set[str], *, source: str) -> None:
    missing = sorted(discovered - _registry_keys())
    assert not missing, (
        f"发现未登记的设置项（来源：{source}）：{missing}\n{FAILURE_GUIDANCE}"
    )


# ---------------------------------------------------------------------------
# Minimal TypeScript lexical scanner (handles comments, strings, nesting)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _TsToken:
    kind: str  # "ident" | "string" | "punct"
    value: str


def _tokenize_ts(source: str) -> list[_TsToken]:
    """Tokenize TypeScript source, stripping comments while preserving strings & delimiters."""
    tokens: list[_TsToken] = []
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if ch.isspace():
            i += 1
            continue
        # Line comment
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            nl = source.find("\n", i + 2)
            i = n if nl == -1 else nl + 1
            continue
        # Block comment
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            end = source.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        # String literal (single, double, or simple template without interpolation)
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            buf: list[str] = []
            while i < n:
                c = source[i]
                if c == "\\" and i + 1 < n:
                    buf.append(source[i + 1])
                    i += 2
                    continue
                if c == quote:
                    i += 1
                    break
                buf.append(c)
                i += 1
            tokens.append(_TsToken("string", "".join(buf)))
            continue
        # Identifier / number
        if ch.isalpha() or ch in ("_", "$"):
            start = i
            i += 1
            while i < n and (source[i].isalnum() or source[i] in ("_", "$")):
                i += 1
            tokens.append(_TsToken("ident", source[start:i]))
            continue
        # Punctuation
        tokens.append(_TsToken("punct", ch))
        i += 1
    return tokens


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
        f"Unclosed {open_ch!r} starting at token index {open_idx}"
    )


def _extract_object_keys(tokens: list[_TsToken], brace_idx: int) -> list[str]:
    """Return top-level property names inside the `{ ... }` literal at ``brace_idx``."""
    end_idx = _find_matching(tokens, brace_idx, "{", "}")
    keys: list[str] = []
    i = brace_idx + 1
    depth = 0
    while i < end_idx:
        tok = tokens[i]
        if tok.kind == "punct":
            if tok.value in ("{", "[", "("):
                depth += 1
            elif tok.value in ("}", "]", ")"):
                depth -= 1
            i += 1
            continue
        if (
            depth == 0
            and tok.kind in ("ident", "string")
            and i + 1 < end_idx
            and tokens[i + 1].kind == "punct"
            and tokens[i + 1].value == ":"
        ):
            keys.append(tok.value)
        i += 1
    return keys


def _find_const_initializer(tokens: list[_TsToken], name: str) -> int:
    """Return token index immediately after `const <name> [: type] =`."""
    for i, tok in enumerate(tokens):
        if tok.kind == "ident" and tok.value == name and i > 0:
            prev = tokens[i - 1]
            if prev.kind == "ident" and prev.value in ("const", "let", "var"):
                j = i + 1
                depth = 0
                while j < len(tokens):
                    t = tokens[j]
                    if t.kind == "punct":
                        if t.value in ("{", "[", "(", "<"):
                            depth += 1
                        elif t.value in ("}", "]", ")", ">"):
                            depth = max(0, depth - 1)
                        elif t.value == "=" and depth == 0:
                            # Skip `=>` inside type annotations.
                            if (
                                j + 1 < len(tokens)
                                and tokens[j + 1].kind == "punct"
                                and tokens[j + 1].value == ">"
                            ):
                                j += 2
                                continue
                            return j + 1
                    j += 1
    raise AssertionError(f"Could not find declaration for {name!r}")


def _extract_zod_object_keys(path: Path, schema_const_name: str) -> set[str]:
    """Extract top-level property keys from `const <schema_const_name> = z.object({ ... })`."""
    tokens = _tokenize_ts(path.read_text(encoding="utf-8"))
    idx = _find_const_initializer(tokens, schema_const_name)
    # Walk until `z . object ( {`
    for i in range(idx, len(tokens) - 4):
        if (
            tokens[i] == _TsToken("ident", "z")
            and tokens[i + 1] == _TsToken("punct", ".")
            and tokens[i + 2].kind == "ident"
            and tokens[i + 2].value in ("object", "looseObject", "strictObject")
            and tokens[i + 3] == _TsToken("punct", "(")
            and tokens[i + 4] == _TsToken("punct", "{")
        ):
            return set(_extract_object_keys(tokens, i + 4))
    raise AssertionError(
        f"Could not locate z.object({{...}}) for {schema_const_name!r} in {path}"
    )


def _extract_string_list_const(path: Path, const_name: str) -> set[str]:
    """Extract string literals inside `[ ... ]` for a `const <name> = ... [ ... ]`."""
    tokens = _tokenize_ts(path.read_text(encoding="utf-8"))
    idx = _find_const_initializer(tokens, const_name)
    for i in range(idx, len(tokens)):
        if tokens[i] == _TsToken("punct", "["):
            end = _find_matching(tokens, i, "[", "]")
            return {
                tok.value
                for tok in tokens[i + 1 : end]
                if tok.kind == "string"
            }
    raise AssertionError(f"Could not locate array literal for {const_name!r} in {path}")


def _extract_string_consts(tokens: list[_TsToken]) -> dict[str, str]:
    """Collect simple `const IDENT = 'literal'` bindings in a TS module."""
    out: dict[str, str] = {}
    for i in range(len(tokens) - 3):
        if (
            tokens[i] == _TsToken("ident", "const")
            and tokens[i + 1].kind == "ident"
            and tokens[i + 2] == _TsToken("punct", "=")
            and tokens[i + 3].kind == "string"
        ):
            out[tokens[i + 1].value] = tokens[i + 3].value
    return out


def _extract_type_string_union(path: Path, type_name: str) -> set[str]:
    """Extract string literal members from `export type <type_name> = 'a' | 'b';`."""
    tokens = _tokenize_ts(path.read_text(encoding="utf-8"))
    for i in range(len(tokens) - 3):
        if (
            tokens[i] == _TsToken("ident", "type")
            and tokens[i + 1] == _TsToken("ident", type_name)
            and tokens[i + 2] == _TsToken("punct", "=")
        ):
            values: set[str] = set()
            j = i + 3
            while j < len(tokens) and tokens[j] != _TsToken("punct", ";"):
                if tokens[j].kind == "string":
                    values.add(tokens[j].value)
                j += 1
            return values
    raise AssertionError(f"Could not locate type union {type_name!r} in {path}")


def _extract_profile_fields(path: Path) -> set[str]:
    """Extract `(section, key)` pairs from `PROFILE_FIELDS` in `welcome/profile.ts`."""
    tokens = _tokenize_ts(path.read_text(encoding="utf-8"))
    idx = _find_const_initializer(tokens, "PROFILE_FIELDS")
    assert tokens[idx] == _TsToken("punct", "{")
    end_idx = _find_matching(tokens, idx, "{", "}")
    discovered: set[str] = set()
    i = idx + 1
    depth = 0
    while i < end_idx:
        tok = tokens[i]
        if tok == _TsToken("punct", "{"):
            if depth == 0:
                entry_end = _find_matching(tokens, i, "{", "}")
                section: str | None = None
                key: str | None = None
                for j in range(i + 1, entry_end - 2):
                    if (
                        tokens[j] == _TsToken("ident", "section")
                        and tokens[j + 1] == _TsToken("punct", ":")
                        and tokens[j + 2].kind == "string"
                    ):
                        section = tokens[j + 2].value
                    elif (
                        tokens[j] == _TsToken("ident", "key")
                        and tokens[j + 1] == _TsToken("punct", ":")
                        and tokens[j + 2].kind == "string"
                    ):
                        key = tokens[j + 2].value
                assert section and key, f"Incomplete PROFILE_FIELDS entry in {path}"
                discovered.add(f"user_preferences.{section}.{key}")
                i = entry_end + 1
                continue
            depth += 1
        elif tok == _TsToken("punct", "}"):
            depth -= 1
        i += 1
    assert discovered, f"No PROFILE_FIELDS entries extracted from {path}"
    return discovered


def _extract_web_other_preference_keys() -> set[str]:
    """Scan frontend preference modules using `readOtherPreference` / `otherPreferencePatch`
    and known preference key constants (`favoriteModels.ts`, `claudeCodePlan.ts`,
    `dashboards/saved.ts`, `ChatPanel.tsx`)."""
    discovered: set[str] = set()

    # 1. Every TS module in web/src/lib/ that calls readOtherPreference / otherPreferencePatch
    for ts_file in sorted((WEB_SRC / "lib").rglob("*.ts")):
        if "__tests__" in ts_file.parts or ".test." in ts_file.name:
            continue
        text = ts_file.read_text(encoding="utf-8")
        if "readOtherPreference" not in text and "otherPreferencePatch" not in text:
            continue
        if ts_file.name == "otherPreference.ts":
            continue
        tokens = _tokenize_ts(text)
        consts = _extract_string_consts(tokens)
        for i, tok in enumerate(tokens):
            if tok.kind != "ident":
                continue
            if tok.value == "readOtherPreference" and i + 5 < len(tokens):
                # readOtherPreference(prefs, KEY)
                if tokens[i + 1] == _TsToken("punct", "("):
                    # find first comma at depth 1
                    end = _find_matching(tokens, i + 1, "(", ")")
                    depth = 0
                    for j in range(i + 2, end):
                        if tokens[j].kind == "punct" and tokens[j].value in ("(", "[", "{"):
                            depth += 1
                        elif tokens[j].kind == "punct" and tokens[j].value in (")", "]", "}"):
                            depth -= 1
                        elif tokens[j] == _TsToken("punct", ",") and depth == 0:
                            arg = tokens[j + 1]
                            if arg.kind == "string":
                                discovered.add(f"user_preferences.other_preference.{arg.value}")
                            elif arg.kind == "ident" and arg.value in consts:
                                discovered.add(
                                    f"user_preferences.other_preference.{consts[arg.value]}"
                                )
                            break
            elif tok.value == "otherPreferencePatch" and i + 2 < len(tokens):
                if tokens[i + 1] == _TsToken("punct", "("):
                    arg = tokens[i + 2]
                    if arg.kind == "string":
                        discovered.add(f"user_preferences.other_preference.{arg.value}")
                    elif arg.kind == "ident" and arg.value in consts:
                        discovered.add(
                            f"user_preferences.other_preference.{consts[arg.value]}"
                        )

    # 2. Exported key constants in favoriteModels.ts, claudeCodePlan.ts, dashboards/saved.ts
    for rel, const_names in (
        ("lib/favoriteModels.ts", ("STARRED_KEY", "HIDDEN_KEY", "STARRED_ONLY_KEY")),
        ("lib/claudeCodePlan.ts", ("PLAN_OUT_KEY",)),
        ("lib/dashboards/saved.ts", ("SAVED_DASHBOARDS_KEY",)),
    ):
        tokens = _tokenize_ts((WEB_SRC / rel).read_text(encoding="utf-8"))
        consts = _extract_string_consts(tokens)
        for cname in const_names:
            assert cname in consts, f"Missing {cname} in {rel}"
            discovered.add(f"user_preferences.other_preference.{consts[cname]}")

    # 3. Direct other_preference key written in ChatPanel.tsx (`voice_input_enabled`)
    chat_panel = WEB_SRC / "pages" / "Settings" / "panels" / "ChatPanel.tsx"
    tokens = _tokenize_ts(chat_panel.read_text(encoding="utf-8"))
    for i in range(len(tokens) - 4):
        if (
            tokens[i] == _TsToken("ident", "other_preference")
            and tokens[i + 1] == _TsToken("punct", ":")
            and tokens[i + 2] == _TsToken("punct", "{")
        ):
            for k in _extract_object_keys(tokens, i + 2):
                discovered.add(f"user_preferences.other_preference.{k}")

    return discovered


# ---------------------------------------------------------------------------
# Dalton router discovery
# ---------------------------------------------------------------------------


def _discover_dalton_put_patch_routes() -> set[tuple[str, str]]:
    """Collect every mounted ``PUT``/``PATCH`` route from ``src/dalton/app/`` and
    ``src/dalton/monitors/router.py`` using FastAPI's ``APIRouter.routes``."""
    import src.dalton.app as dalton_app_pkg
    from src.dalton.app import channels_router as channels_mod
    from src.dalton.app import router as main_router_mod
    from src.dalton.monitors import router as monitors_router_mod

    # Collect standalone / top-level routers. ``channels_router`` is already included
    # into ``src.dalton.app.router.router`` under ``/api/v1/dalton``.
    routers: list[APIRouter] = [main_router_mod.router, monitors_router_mod.router]
    seen_router_ids: set[int] = {
        id(main_router_mod.router),
        id(monitors_router_mod.router),
        id(channels_mod.channels_router),
    }

    for modinfo in sorted(
        pkgutil.iter_modules(dalton_app_pkg.__path__), key=lambda m: m.name
    ):
        mod = importlib.import_module(f"src.dalton.app.{modinfo.name}")
        for attr in dir(mod):
            obj = getattr(mod, attr)
            if isinstance(obj, APIRouter) and id(obj) not in seen_router_ids:
                seen_router_ids.add(id(obj))
                routers.append(obj)

    found: set[tuple[str, str]] = set()
    for rtr in routers:
        for route in rtr.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in route.methods:
                if method in ("PUT", "PATCH"):
                    found.add((method, route.path))
    return found


# ---------------------------------------------------------------------------
# Tests: Registry structure & Chief of Staff tool validity
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
            assert field in item and item[field], (
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


def test_settings_registry_tool_references_exist():
    """Every `tool:<name>` entry names a real Chief of Staff tool or virtual file backend."""
    from src.dalton.tools import DALTON_TOOL_REGISTRY
    from src.tools.secretary.chief_of_staff import CHIEF_OF_STAFF_TOOLS

    cos_tools: set[str] = {t.name for t in CHIEF_OF_STAFF_TOOLS}
    for reg in DALTON_TOOL_REGISTRY:
        if "chief_of_staff" in reg.roles:
            cos_tools.update(reg.tool_names)

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


# ---------------------------------------------------------------------------
# Tests: Frontend Zod preference schemas & thread/other preference keys
# ---------------------------------------------------------------------------


def test_frontend_zod_preference_schemas_are_registered():
    """Scan frontend Zod schemas and preference/thread key registries in `web/src/`."""
    model_prefs_ts = WEB_SRC / "lib" / "modelPreferences.ts"
    model_tuning_ts = WEB_SRC / "lib" / "modelTuning.ts"
    welcome_profile_ts = WEB_SRC / "pages" / "Onboarding" / "welcome" / "profile.ts"
    onboarding_schema_ts = WEB_SRC / "pages" / "Onboarding" / "onboardingPrefsSchema.ts"
    thread_fields_ts = WEB_SRC / "lib" / "threadFieldMutations.ts"

    # 1. DaltonModelPrefsSchema (z.object) + MOVED_KEYS + MODEL_PREF_KEYS
    zod_dalton_keys = _extract_zod_object_keys(model_prefs_ts, "DaltonModelPrefsSchema")
    assert zod_dalton_keys, "DaltonModelPrefsSchema must define keys"
    moved_keys = _extract_string_list_const(model_prefs_ts, "MOVED_KEYS")
    model_pref_keys = _extract_string_list_const(model_prefs_ts, "MODEL_PREF_KEYS")
    tuning_fields = _extract_string_list_const(model_tuning_ts, "TUNING_FIELDS")

    frontend_model_keys = {
        f"user_preferences.model_preference.{k}"
        for k in (zod_dalton_keys | moved_keys | model_pref_keys | tuning_fields)
    }
    _assert_all_registered(
        frontend_model_keys,
        source="web/src/lib/modelPreferences.ts (DaltonModelPrefsSchema / MODEL_PREF_KEYS) & modelTuning.ts",
    )

    # 2. Structured investing/agent profile fields (uses zod/mini validators in welcome/profile.ts)
    profile_keys = _extract_profile_fields(welcome_profile_ts)
    _assert_all_registered(
        profile_keys,
        source="web/src/pages/Onboarding/welcome/profile.ts (PROFILE_FIELDS)",
    )

    # 3. OnboardingPrefsSchema (z.object stored under other_preference.onboarding)
    onboarding_fields = _extract_zod_object_keys(
        onboarding_schema_ts, "OnboardingPrefsSchema"
    )
    assert onboarding_fields, "OnboardingPrefsSchema must define fields"
    _assert_all_registered(
        {"user_preferences.other_preference.onboarding"},
        source="web/src/pages/Onboarding/onboardingPrefsSchema.ts (OnboardingPrefsSchema)",
    )

    # 4. Frontend ThreadField union (web/src/lib/threadFieldMutations.ts)
    thread_fields = {
        f"thread.{f}"
        for f in _extract_type_string_union(thread_fields_ts, "ThreadField")
    }
    assert thread_fields, "ThreadField union must define thread setting fields"
    _assert_all_registered(
        thread_fields,
        source="web/src/lib/threadFieldMutations.ts (ThreadField)",
    )

    # 5. Frontend other_preference readers & writers
    other_pref_keys = _extract_web_other_preference_keys()
    assert other_pref_keys, "Expected frontend other_preference keys"
    _assert_all_registered(
        other_pref_keys,
        source="web/src/lib/* (other_preference readers/writers) & ChatPanel.tsx",
    )


# ---------------------------------------------------------------------------
# Tests: Backend user_preferences & users keys
# ---------------------------------------------------------------------------


def test_backend_user_and_preferences_keys_are_registered():
    """Scan backend Pydantic models, DB update helpers, and canonical preference key tuples."""
    from src.dalton.llm.egress_policy import _TEAM_BASE_URL_CLASSES_PREF_KEY
    from src.dalton.llm.model_prefs import DALTON_MODEL_PREF_KEYS
    from src.llms.preferences import MOVED_MODEL_KEYS, TUNING_FIELDS
    from src.server.database.user import DEFAULT_OTHER_PREFERENCE, update_user
    from src.server.models.onboarding import ACCOUNT_SETUP_KEY
    from src.server.models.profile_vocabulary import STRUCTURED_PREFERENCES
    from src.server.models.user import (
        AgentPreference,
        InvestmentPreference,
        OtherPreference,
        RiskPreference,
        UserUpdate,
    )
    from src.server.services.llm.user_models import _NEVER_MOVED

    discovered: set[str] = set()

    # UserUpdate Pydantic fields + update_user DB helper params
    for field in UserUpdate.model_fields:
        discovered.add(f"user.{field}")
    for param in inspect.signature(update_user).parameters:
        if param not in _USER_UPDATE_INTERNAL_PARAMS:
            discovered.add(f"user.{param}")

    # Typed preference section Pydantic models
    for section, model_cls in (
        ("risk_preference", RiskPreference),
        ("investment_preference", InvestmentPreference),
        ("agent_preference", AgentPreference),
        ("other_preference", OtherPreference),
    ):
        for field in model_cls.model_fields:
            discovered.add(f"user_preferences.{section}.{field}")

    # Structured profile vocabulary
    for section, vocab_map in STRUCTURED_PREFERENCES.items():
        for key in vocab_map:
            discovered.add(f"user_preferences.{section}.{key}")

    # Canonical model_preference keys (upstream + Dalton)
    for key in (*MOVED_MODEL_KEYS, *TUNING_FIELDS, *DALTON_MODEL_PREF_KEYS):
        discovered.add(f"user_preferences.model_preference.{key}")
    for key in ("profiles", "default_model_scope", "background_follows"):
        discovered.add(f"user_preferences.model_preference.{key}")

    # Canonical other_preference keys read/written by backend
    for key in (
        *_NEVER_MOVED,
        *DEFAULT_OTHER_PREFERENCE.keys(),
        ACCOUNT_SETUP_KEY,
        _TEAM_BASE_URL_CLASSES_PREF_KEY,
        "subagents_default",
        "artifact_comment_destination",
        "skills",
    ):
        discovered.add(f"user_preferences.other_preference.{key}")

    _assert_all_registered(
        discovered,
        source="backend users & user_preferences models/constants",
    )


# ---------------------------------------------------------------------------
# Tests: Thread writable fields
# ---------------------------------------------------------------------------


def test_thread_writable_fields_are_registered():
    """Every writable field on ThreadUpdateRequest / visibility / share models is registered."""
    from src.server.models.conversation import (
        ThreadShareRequest,
        ThreadUpdateRequest,
        ThreadVisibilityRequest,
    )

    discovered = {f"thread.{f}" for f in ThreadUpdateRequest.model_fields}
    for f in ThreadVisibilityRequest.model_fields:
        discovered.add(f"thread.{f}")
    assert "is_shared" in ThreadShareRequest.model_fields
    assert "permissions" in ThreadShareRequest.model_fields
    discovered.update({"thread.is_shared", "thread.share_permissions"})

    _assert_all_registered(
        discovered,
        source="src/server/models/conversation.py (ThreadUpdateRequest / ThreadVisibilityRequest / ThreadShareRequest)",
    )


# ---------------------------------------------------------------------------
# Tests: Automation writable fields
# ---------------------------------------------------------------------------


def test_automation_writable_fields_are_registered():
    """Every writable field on AutomationCreate, AutomationUpdate, and CompanyScriptUpdate is registered."""
    from src.server.models.automation import (
        AutomationCreate,
        AutomationUpdate,
        CompanyScriptUpdate,
    )

    fields = (
        set(AutomationCreate.model_fields)
        | set(AutomationUpdate.model_fields)
        | set(CompanyScriptUpdate.model_fields)
    ) - _AUTOMATION_NON_SETTING_FIELDS
    discovered = {f"automation.{f}" for f in fields}

    _assert_all_registered(
        discovered,
        source="src/server/models/automation.py (AutomationCreate / AutomationUpdate / CompanyScriptUpdate)",
    )


# ---------------------------------------------------------------------------
# Tests: Workspace, Computer, and Deployment settings
# ---------------------------------------------------------------------------


def test_workspace_computer_and_deployment_settings_are_registered():
    """Workspace writable metadata, computer browser settings, and deployment_settings catalog keys are registered."""
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

    for field in BrowserChoicesUpdate.model_fields:
        discovered.add(f"computer.browser.{field}")

    for name in (
        catalog.PROVIDER_SETTING,
        catalog.INSTALL_GROUPS_SETTING,
        catalog.COMPLETED_SETTING,
        *(spec.name for spec in catalog.key_specs()),
    ):
        discovered.add(f"deployment_settings.{name}")

    _assert_all_registered(
        discovered,
        source="WorkspaceUpdate / BrowserChoicesUpdate / setup_settings.catalog",
    )


# ---------------------------------------------------------------------------
# Tests: Dalton PUT / PATCH routes
# ---------------------------------------------------------------------------


def test_dalton_put_patch_routes_are_registered():
    """Every PUT/PATCH route under src/dalton/app/ and src/dalton/monitors/ must be
    registered in settings_registry.yaml (or explicitly classified in NON_SETTING_DALTON_ROUTES)."""
    live_routes = _discover_dalton_put_patch_routes()
    assert live_routes, "Expected Dalton PUT/PATCH routes to be discovered"

    registered_routes: set[tuple[str, str]] = set()
    for item in _load_registry():
        for raw_route in item.get("routes") or []:
            parts = str(raw_route).strip().split(" ", 1)
            assert len(parts) == 2 and parts[0] in ("PUT", "PATCH"), (
                f"Entry {item['key']!r} has invalid route spec {raw_route!r}"
            )
            registered_routes.add((parts[0], parts[1]))

    setting_routes = live_routes - NON_SETTING_DALTON_ROUTES
    unregistered = sorted(setting_routes - registered_routes)
    assert not unregistered, (
        f"发现未登记的 Dalton PUT/PATCH 设置接口：{unregistered}\n{FAILURE_GUIDANCE}"
    )

    # Also catch stale routes left behind after an endpoint is removed or renamed.
    stale_registered = sorted(registered_routes - live_routes)
    assert not stale_registered, (
        f"settings_registry.yaml 中存在已失效的 Dalton 路由：{stale_registered}"
    )
    stale_non_setting = sorted(NON_SETTING_DALTON_ROUTES - live_routes)
    assert not stale_non_setting, (
        f"NON_SETTING_DALTON_ROUTES 中存在已失效的路由：{stale_non_setting}"
    )


def test_unregistered_setting_failure_message_guides_developer():
    """Guard failure message clearly directs developers to register in settings_registry.yaml."""
    with pytest.raises(AssertionError) as exc_info:
        _assert_all_registered({"__unregistered_probe_setting__"}, source="probe")
    msg = str(exc_info.value)
    assert "在 settings_registry.yaml 登记，并决定研究总监能否修改" in msg
    assert "__unregistered_probe_setting__" in msg
