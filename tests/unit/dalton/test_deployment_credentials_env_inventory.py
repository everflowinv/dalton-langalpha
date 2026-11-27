"""Static reconciliation test for the repository-wide environment variable inventory (§1, §1.6, §7, §5).

Scans:
- ``.env.core.example``, ``.env.example``, ``web/.env.dalton``
- ``scripts/standalone/*.sh`` and ``scripts/dalton/ops/**/*.sh``
- Python ``src/`` (``os.getenv`` / ``os.environ``) and frontend ``web/src/`` (``import.meta.env.VITE_*``)

and asserts that every configuration environment variable is accounted for in
``docs/dalton/features/deployment-credentials.md`` (§1 four-category tables,
§1.6 internal runtime/test exemptions, or §7 Batch 2-4 review families).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from src.dalton.deployment_credentials.catalog import BATCH1_FIELD_SPECS

REPO_ROOT = Path(__file__).resolve().parents[3]
DESIGN_DOC = REPO_ROOT / "docs" / "dalton" / "features" / "deployment-credentials.md"

_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
_TABLE_KEY_RE = re.compile(r"^\|\s*`([A-Z][A-Z0-9_]+)`\s*\|", re.MULTILINE)
_INLINE_TOKEN_RE = re.compile(r"`([A-Z][A-Z0-9_]+(?:\*|_2…N)?)`")
_ENV_LINE_RE = re.compile(r"^\s*#?\s*(?:export\s+)?([A-Z][A-Z0-9_]{2,})\s*=")
_STANDALONE_HELPER_RE = re.compile(
    r"(?:get_env_value(?:_any)?|write_env_key|set_env_if_empty|set_env_maybe_override|"
    r"remove_env_key(?:_all)?|append_env_key_unique|count_env_key_assignments)\s+"
    r"(?:\"\$[^\"]+\"\s+)?\"([A-Z][A-Z0-9_]{2,})\""
)
# Match every shell parameter default expansion `${ENV_KEY:-...}` across standalone and ops scripts
# (including renamed assignments such as `EDGE_COMPOSE_TIMEOUT="${DALTON_EDGE_COMPOSE_TIMEOUT:-120}"`).
_SHELL_OVERRIDE_RE = re.compile(
    r"\$\{((?:DALTON|VITE|DB|REDIS|MINIO|STORAGE|S3|AWS|R2|OSS|PUBLIC|SERVER|DOMAIN|"
    r"APP_ENV|HOST_MODE|BRAND|BYOK|JWT|BOOTSTRAP|INTERNAL|EGRESS|SANDBOX|SEC|EDGAR|"
    r"FMP|TAVILY|SERPER|EXA|FIRECRAWL|OPENAI|ANTHROPIC|GEMINI|GOOGLE|FEISHU|DISCORD|"
    r"WEIXIN|SENTRY|APNS|SUPABASE|CHANNEL)[A-Z0-9_]*):-",
)

# Local temporary shell variables unconditionally initialized inside scripts (never external env inputs).
_SHELL_LOCAL_TEMP_VARS: frozenset[str] = frozenset(
    {
        "MINIO_MANIFEST_TMP",
    }
)

# Standard POSIX / OS / toolchain environment variables (never application config).
_POSIX_AND_TOOL_VARS: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "SHELL",
        "PWD",
        "OLDPWD",
        "TMPDIR",
        "TMP",
        "TEMP",
        "TERM",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "UID",
        "EUID",
        "HOSTNAME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "PYTHONPATH",
        "PYTHONUNBUFFERED",
        "VIRTUAL_ENV",
        "UV_CACHE_DIR",
        "NODE_ENV",
        "CI",
        "GITHUB_ACTIONS",
        "GITHUB_SHA",
        "GITHUB_REF",
        "DOCKER_HOST",
        "DOCKER_BUILDKIT",
        "COMPOSE_PROJECT_NAME",
        "COMPOSE_FILE",
        "COMPOSE_PROFILES",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
        "SSH_AUTH_SOCK",
        "SSH_CLIENT",
        "SSH_CONNECTION",
        "SSH_TTY",
        "DISPLAY",
        "PGPASSWORD",
        "PGUSER",
        "PGHOST",
        "PGPORT",
        "PGDATABASE",
        "PGSSLMODE",
        "AWS_DEFAULT_REGION",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_SHARED_CREDENTIALS_FILE",
    }
)

# Runtime/sibling-branch keys present in src/ since the r7 §1 audit (tracked for Batch 2 §7 item 2).
_PENDING_SRC_REVIEW_KEYS: frozenset[str] = frozenset(
    {
        # Standard python-dotenv disable switch checked in src/dalton/config_env.py
        "PYTHON_DOTENV_DISABLED",
        # Optional external connector bundle directory read in src/dalton/plugin_loader.py
        "DALTON_EXTRA_PLUGINS_DIR",
    }
)

# Ops/upgrade shell-script tuning overrides deferred to §7 item 2 ("脚本变量归档统计").
_OPS_SCRIPT_TUNING_PREFIXES: tuple[str, ...] = (
    "DALTON_ALEMBIC_",
    "DALTON_BACKEND_",
    "DALTON_BACKUP_",
    "DALTON_BRIDGE_",
    "DALTON_CHANNEL_",
    "DALTON_COLIMA_",
    "DALTON_CONFIG_",
    "DALTON_CONTAINER_",
    "DALTON_DATA_",
    "DALTON_DB_",
    "DALTON_DEPLOY_",
    "DALTON_DOCKER_",
    "DALTON_DRAIN_",
    "DALTON_DRIVE_",
    "DALTON_EDGE_",
    "DALTON_ENV_",
    "DALTON_EXTERNAL_",
    "DALTON_FK_",
    "DALTON_GATEWAY_",
    "DALTON_GIT_",
    "DALTON_HEALTH_",
    "DALTON_HOMEBREW_",
    "DALTON_LAUNCH",
    "DALTON_LOGS_",
    "DALTON_MIGRATE_",
    "DALTON_MINIO_",
    "DALTON_NODE_",
    "DALTON_OFFSITE_",
    "DALTON_OPS_",
    "DALTON_PG_",
    "DALTON_PORTWATCH_",
    "DALTON_POST_UPGRADE_",
    "DALTON_PROD_",
    "DALTON_PROJECT_",
    "DALTON_PRUNE_",
    "DALTON_RCLONE_",
    "DALTON_RECOVERY_",
    "DALTON_REDIS_",
    "DALTON_REPO_",
    "DALTON_RH_",
    "DALTON_SANDBOX_",
    "DALTON_TRANSCRIPTION_",
    "DALTON_USAGE_",
    "DALTON_USER_",
    "DALTON_WAIT_",
    "DALTON_WATCHDOG_",
    "DALTON_WEEKLY_",
    "DALTON_WORK_",
    "DB_DRIVE_",
    "RETAIN_",
)


def _parse_design_inventory() -> dict[str, set[str]]:
    doc = DESIGN_DOC.read_text(encoding="utf-8")
    idx_a = doc.index("### 1.2 ")
    idx_b = doc.index("### 1.3 ")
    idx_c = doc.index("### 1.4 ")
    idx_d = doc.index("### 1.5 ")
    idx_16 = doc.index("### 1.6 ")
    idx_2 = doc.index("## 2. ")
    idx_7 = doc.index("## 7. ")
    idx_8 = doc.index("## 8. ")

    idx_b1 = doc.index("#### B1. ")
    idx_b2 = doc.index("#### B2. ")

    return {
        "A": set(_TABLE_KEY_RE.findall(doc[idx_a:idx_b])),
        "B": set(_TABLE_KEY_RE.findall(doc[idx_b:idx_c])),
        "B1": set(_TABLE_KEY_RE.findall(doc[idx_b1:idx_b2])),
        "C": set(_TABLE_KEY_RE.findall(doc[idx_c:idx_d])),
        "D": set(_TABLE_KEY_RE.findall(doc[idx_d:idx_16])),
        "exempt_16": set(_INLINE_TOKEN_RE.findall(doc[idx_16:idx_2])),
        "pending_7": set(_INLINE_TOKEN_RE.findall(doc[idx_7:idx_8])),
    }


def _is_in_inventory(key: str, inv: dict[str, set[str]]) -> bool:
    all_tables = inv["A"] | inv["B"] | inv["C"] | inv["D"]
    if key in all_tables or key in inv["exempt_16"] or key in inv["pending_7"]:
        return True
    for token in inv["exempt_16"] | inv["pending_7"]:
        if token.endswith("*") and key.startswith(token[:-1]):
            return True
    if re.match(r"^FIRECRAWL_API_KEY_\d+$", key):
        return True
    return False


def _parse_env_example(path: Path) -> set[str]:
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _ENV_LINE_RE.match(line)
        if match:
            keys.add(match.group(1))
    return keys


def _scan_python_src_env_keys() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for py_path in sorted((REPO_ROOT / "src").rglob("*.py")):
        try:
            tree = ast.parse(py_path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        rel = str(py_path.relative_to(REPO_ROOT))
        for node in ast.walk(tree):
            key: str | None = None
            if isinstance(node, ast.Call):
                fn = node.func
                if (
                    isinstance(fn, ast.Attribute)
                    and fn.attr == "getenv"
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id == "os"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    key = node.args[0].value
                elif (
                    isinstance(fn, ast.Attribute)
                    and fn.attr in ("get", "pop")
                    and isinstance(fn.value, ast.Attribute)
                    and fn.value.attr == "environ"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    key = node.args[0].value
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "environ"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                key = node.slice.value
            if key and _ENV_NAME_RE.match(key) and key not in _POSIX_AND_TOOL_VARS:
                found.setdefault(key, []).append(rel)
    return found


def _scan_web_src_vite_keys() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    vite_builtins = frozenset({"MODE", "DEV", "PROD", "SSR", "BASE_URL"})
    for ts_path in sorted((REPO_ROOT / "web" / "src").rglob("*")):
        if not ts_path.is_file() or ts_path.suffix not in (".ts", ".tsx"):
            continue
        if "__tests__" in ts_path.parts:
            continue
        rel = str(ts_path.relative_to(REPO_ROOT))
        text = ts_path.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r"import\.meta\.env\.([A-Z][A-Z0-9_]+)", text):
            key = match.group(1)
            if key not in vite_builtins:
                found.setdefault(key, []).append(rel)
    return found


def _scan_shell_env_keys() -> tuple[set[str], set[str]]:
    """Return ``(standalone_env_file_keys, ops_and_standalone_override_keys)``."""
    standalone_env_keys: set[str] = set()
    override_keys: set[str] = set()

    standalone_scripts = sorted((REPO_ROOT / "scripts" / "standalone").glob("*.sh"))
    ops_scripts = sorted((REPO_ROOT / "scripts" / "dalton" / "ops").rglob("*.sh"))

    for sh_path in standalone_scripts:
        text = sh_path.read_text(encoding="utf-8", errors="ignore")
        for match in _STANDALONE_HELPER_RE.finditer(text):
            key = match.group(1)
            if key not in _POSIX_AND_TOOL_VARS:
                standalone_env_keys.add(key)
        for match in _SHELL_OVERRIDE_RE.finditer(text):
            key = match.group(1)
            if key not in _POSIX_AND_TOOL_VARS and key not in _SHELL_LOCAL_TEMP_VARS:
                override_keys.add(key)

    for sh_path in ops_scripts:
        text = sh_path.read_text(encoding="utf-8", errors="ignore")
        for match in _SHELL_OVERRIDE_RE.finditer(text):
            key = match.group(1)
            if key not in _POSIX_AND_TOOL_VARS and key not in _SHELL_LOCAL_TEMP_VARS:
                override_keys.add(key)

    return standalone_env_keys, override_keys


def test_design_doc_four_categories_are_disjoint_and_cover_all_b1_fields() -> None:
    inv = _parse_design_inventory()
    categories = ("A", "B", "C", "D")
    for idx, left in enumerate(categories):
        for right in categories[idx + 1 :]:
            overlap = inv[left] & inv[right]
            assert overlap == set(), f"Categories {left} and {right} overlap: {sorted(overlap)}"

    total = sum(len(inv[c]) for c in categories)
    assert total == 462, f"Expected 462 archived env keys across A/B/C/D, got {total}"

    b1_catalog_names = {spec.name for spec in BATCH1_FIELD_SPECS}
    assert len(b1_catalog_names) == 6
    assert b1_catalog_names == inv["B1"], (
        f"B1 catalog keys {sorted(b1_catalog_names)} differ from design §1.3 B1 {sorted(inv['B1'])}"
    )


def test_env_example_files_are_100_percent_reconciled_with_design_inventory() -> None:
    inv = _parse_design_inventory()
    for rel in (".env.core.example", ".env.example", "web/.env.dalton"):
        keys = _parse_env_example(REPO_ROOT / rel)
        assert len(keys) > 0, f"Expected non-empty env keys in {rel}"
        missing = sorted(k for k in keys if not _is_in_inventory(k, inv))
        assert missing == [], f"Unclassified env keys in {rel}: {missing}"


def test_source_and_shell_env_reads_are_reconciled_with_design_inventory() -> None:
    inv = _parse_design_inventory()

    # 1. Frontend Vite env reads in web/src
    web_keys = _scan_web_src_vite_keys()
    assert len(web_keys) >= 15
    missing_web = sorted(k for k in web_keys if not _is_in_inventory(k, inv))
    assert missing_web == [], f"Unclassified web/src import.meta.env keys: {missing_web}"

    # 2. Python src/ env reads
    py_keys = _scan_python_src_env_keys()
    assert len(py_keys) >= 200
    missing_py = sorted(
        k
        for k in py_keys
        if not _is_in_inventory(k, inv) and k not in _PENDING_SRC_REVIEW_KEYS
    )
    assert missing_py == [], (
        "Unclassified Python src/ env keys (add to §1/§1.6/§7): "
        + ", ".join(f"{k} ({py_keys[k][0]})" for k in missing_py)
    )

    # 3. Standalone shell script .env manipulations (must be 100% in §1/§1.6/§7)
    standalone_env_keys, override_keys = _scan_shell_env_keys()
    assert len(standalone_env_keys) >= 30
    missing_standalone = sorted(k for k in standalone_env_keys if not _is_in_inventory(k, inv))
    assert missing_standalone == [], (
        f"Unclassified scripts/standalone/*.sh .env keys: {missing_standalone}"
    )

    # 4. Top-level shell overrides across scripts/standalone and scripts/dalton/ops
    missing_overrides = sorted(
        k
        for k in override_keys
        if not _is_in_inventory(k, inv)
        and not k.startswith(_OPS_SCRIPT_TUNING_PREFIXES)
    )
    assert missing_overrides == [], (
        f"Unclassified shell override env keys outside §1/§1.6/§7 ops family: {missing_overrides}"
    )
