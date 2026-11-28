# auth: oss
"""Standalone Core Batch 2 acceptance checks (`b2_backend_capabilities.py`).

Uses Python standard library + `_lib.py` only so it runs directly on a bare CI
runner alongside `b1_dev_mode.py` (no `httpx`, `psycopg`, or repo `src` imports).

Verifies on a running Standalone Core (`auth: oss`) deployment:
1. Both Alembic migration chains (upstream + Dalton) are at their current
   `head` revisions, the applied Dalton chain includes `d045` and `d047`, and
   `dalton_plugin_disable_overrides` (d045) + `dalton_user_roles` (d047) exist.
2. Backend Docker image manifest layers (and optional `EXPORT_DIR`) contain
   zero `plugins/dalton_data/` files and zero `Proprietary` plugin manifests,
   while shipping `plugins/dalton_sec/`.
3. `GET /api/v1/dalton/capabilities` reports `profile == "core"` with only the
   default Core capability set (`["company_wiki"]`), `GET /api/v1/dalton/system-jobs` lists zero
   host `ops-*` or `data-*` jobs, `PATCH /api/v1/plugins/dalton-data/enabled`
   returns 404, disabled capability routes return 404/403, and backend logs
   show zero outbound attempts to `:8960` / `:8962`.
4. Three-way plugin toggle consistency for `dalton-sec` across
   `GET /api/v1/plugins`, `PATCH /api/v1/plugins/dalton-sec/enabled`,
   `GET /api/v1/mcp/builtin-servers`, and
   `GET /api/v1/workspaces/{ws_id}/mcp/servers` (including inherited legacy
   `('bundle', 'dalton-data')` disable, explicit re-enable via
   `dalton_plugin_disable_overrides`, and subsequent disable).
"""

from __future__ import annotations

import importlib
import os
import re
import secrets
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

# Allow invoking directly as `python3 tests/standalone/checks/b2_backend_capabilities.py`
_CHECKS_DIR = str(Path(__file__).resolve().parent)
if _CHECKS_DIR not in sys.path:
    sys.path.insert(0, _CHECKS_DIR)
_lib = importlib.import_module("_lib")

DEFAULT_USER_ID = "local-dev-user"
HTTP_TIMEOUT_S = 30.0
DOCKER_TIMEOUT_S = 120.0
REQUIRED_DALTON_REVISIONS = ("d045", "d047")
EXPECTED_CORE_DEFAULT_CAPABILITIES: list[str] = ["company_wiki"]
_DALTON_REV_RE = re.compile(r"\b(d\d+)\b")


def log(msg: str) -> None:
    print(f"[b2_backend_capabilities] {msg}", flush=True)


def _sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _check_migrations_at_head(ctx: _lib.ComposeContext) -> None:
    log("1/5 Checking dual Alembic heads, d045/d047 history, and schema tables...")
    expected_upstream, expected_dalton = ctx.get_alembic_heads()
    actual_upstream = ctx.psql_query("SELECT version_num FROM alembic_version;")
    actual_dalton = ctx.psql_query("SELECT version_num FROM alembic_version_dalton;")

    assert actual_upstream == expected_upstream, (
        f"Upstream alembic_version mismatch: actual={actual_upstream!r}, expected={expected_upstream!r}"
    )
    assert actual_dalton == expected_dalton, (
        f"Dalton alembic_version_dalton mismatch: actual={actual_dalton!r}, expected={expected_dalton!r}"
    )

    history_out = ctx.compose(
        "exec",
        "-T",
        "backend",
        "uv",
        "run",
        "--no-sync",
        "alembic",
        "-c",
        "src/dalton/alembic.ini",
        "history",
        "-r",
        f"base:{actual_dalton}",
    ).stdout.decode("utf-8", errors="replace")
    applied_revs: set[str] = {actual_dalton}
    for line in history_out.splitlines():
        head_part = line.split(",", 1)[0]
        applied_revs.update(_DALTON_REV_RE.findall(head_part))
    missing_revs = [rev for rev in REQUIRED_DALTON_REVISIONS if rev not in applied_revs]
    assert not missing_revs, (
        f"Dalton migration head {actual_dalton!r} does not include required revisions "
        f"{missing_revs} (seen={sorted(applied_revs)})"
    )

    d045_reg = ctx.psql_query("SELECT to_regclass('public.dalton_plugin_disable_overrides');")
    assert d045_reg == "dalton_plugin_disable_overrides", (
        f"Missing d045 table dalton_plugin_disable_overrides (got {d045_reg!r})"
    )
    d047_reg = ctx.psql_query("SELECT to_regclass('public.dalton_user_roles');")
    assert d047_reg == "dalton_user_roles", (
        f"Missing d047 table dalton_user_roles (got {d047_reg!r})"
    )
    log(
        f"1/5 OK (upstream={actual_upstream}, dalton={actual_dalton}, "
        f"verified revisions {list(REQUIRED_DALTON_REVISIONS)} and tables)."
    )


def _scan_backend_image_tar(tar_path: Path) -> int:
    """Scan a `docker save` archive strictly by `manifest.json` layers."""
    all_forbidden: list[str] = []
    all_proprietary: list[str] = []
    all_sec_files: set[str] = set()

    def _visit_member(
        layer_name: str,
        layer_tar: tarfile.TarFile,
        member: tarfile.TarInfo,
    ) -> None:
        norm = member.name.lstrip("./")
        if "plugins/dalton_data/" in norm or norm.endswith("plugins/dalton_data"):
            all_forbidden.append(f"{layer_name}:{norm}")
        if norm.endswith("plugins/dalton_sec/plugin.json"):
            all_sec_files.add("plugin.json")
        if norm.endswith("plugins/dalton_sec/dalton_sec_mcp_server.py"):
            all_sec_files.add("dalton_sec_mcp_server.py")
        if (
            member.isfile()
            and norm.endswith("/plugin.json")
            and "plugins/" in norm
            and member.size <= 1_048_576
        ):
            extracted = layer_tar.extractfile(member)
            if extracted is not None:
                text = extracted.read().decode("utf-8", errors="replace")
                if "Proprietary" in text:
                    all_proprietary.append(f"{layer_name}:{norm}")

    layer_count = _lib.visit_docker_save_tar(tar_path, _visit_member)
    assert layer_count > 0, f"No tar layers found in image archive {tar_path}"
    assert not all_forbidden, (
        f"Forbidden plugins/dalton_data/ found in image layers: {all_forbidden}"
    )
    assert not all_proprietary, (
        f"Forbidden Proprietary manifests found in image layers: {all_proprietary}"
    )
    assert {"plugin.json", "dalton_sec_mcp_server.py"} <= all_sec_files, (
        f"plugins/dalton_sec/ incomplete in image layers: found {sorted(all_sec_files)}"
    )
    return layer_count


def _check_image_layers_and_export(ctx: _lib.ComposeContext | None = None) -> None:
    image_tar_env = os.environ.get("BACKEND_IMAGE_TAR", "").strip()
    image_ref = os.environ.get("BACKEND_IMAGE", "").strip()
    export_dir_env = os.environ.get("EXPORT_DIR", "").strip()

    if export_dir_env:
        export_root = Path(export_dir_env)
        assert export_root.is_dir(), f"EXPORT_DIR does not exist: {export_root}"
        assert not (export_root / "plugins" / "dalton_data").exists(), (
            f"Export directory contains forbidden plugins/dalton_data: {export_root}"
        )
        assert (export_root / "plugins" / "dalton_sec" / "plugin.json").is_file(), (
            "Export directory missing plugins/dalton_sec/plugin.json"
        )
        for manifest_path in sorted((export_root / "plugins").glob("*/plugin.json")):
            body = manifest_path.read_text(encoding="utf-8")
            assert "Proprietary" not in body, (
                f"Export directory contains Proprietary plugin manifest: {manifest_path}"
            )

    if image_tar_env:
        layer_count = _scan_backend_image_tar(Path(image_tar_env))
        log(f"2/5 OK (scanned {layer_count} manifest layers from BACKEND_IMAGE_TAR).")
        return

    if image_ref:
        with tempfile.TemporaryDirectory(prefix="b2-image-") as tmpdir:
            tar_path = Path(tmpdir) / "backend.tar"
            proc = subprocess.run(
                ["docker", "save", "-o", str(tar_path), image_ref],
                capture_output=True,
                text=True,
                timeout=DOCKER_TIMEOUT_S,
                check=False,
            )
            assert proc.returncode == 0, f"docker save {image_ref} failed: {proc.stderr}"
            layer_count = _scan_backend_image_tar(tar_path)
        log(f"2/5 OK (scanned {layer_count} manifest layers from BACKEND_IMAGE={image_ref}).")
        return

    if ctx is not None:
        log("2/5 Scanning backend image manifest layers for dalton_data/Proprietary/dalton_sec...")
        with ctx.saved_service_image_tar("backend") as tar_path:
            layer_count = _scan_backend_image_tar(tar_path)
        log(f"2/5 OK (backend image clean: {layer_count} manifest layers scanned, dalton_sec present).")
        return

    ci_mode = os.environ.get("CI", "").strip().lower() in ("1", "true", "yes")
    if ci_mode:
        raise AssertionError(
            "CI=true requires BACKEND_IMAGE or BACKEND_IMAGE_TAR so image layers "
            "are verified to contain zero plugins/dalton_data/ and zero Proprietary manifests"
        )


def _acquire_auth_session(
    ctx: _lib.ComposeContext,
    base_url: str,
) -> tuple[dict[str, str], str]:
    """Obtain request auth headers and caller user_id via the deployment's normal auth flow.

    - In ``local`` auth mode (``GET /api/v1/dalton/bootstrap/status`` returns 200):
      verifies unauthenticated ``GET /api/v1/dalton/capabilities`` returns 401, then
      acquires a Bearer session via ``STANDALONE_ACCESS_TOKEN``, first-admin bootstrap
      (``POST /api/v1/dalton/bootstrap/admin``), or local login
      (``POST /api/v1/dalton/auth/local/login``).
      注意：local 分支会消耗一次性自举，仅供手动排查；请勿在 ``local`` 组中早于
      ``b4_local_auth`` 运行 ``b2``。
    - In ``oss`` auth mode (``bootstrap/status`` returns 404): resolves
      ``X-LangAlpha-Owner`` via ``_lib.oss_owner_headers(ctx)``.
    """
    boot_resp = _lib.http_request(
        "GET", f"{base_url}/api/v1/dalton/bootstrap/status", timeout=HTTP_TIMEOUT_S
    )
    if boot_resp.status == 200:
        unauth_cap = _lib.http_request(
            "GET", f"{base_url}/api/v1/dalton/capabilities", timeout=HTTP_TIMEOUT_S
        )
        assert unauth_cap.status == 401, (
            f"Expected 401 for unauthenticated GET /dalton/capabilities in local auth mode, "
            f"got status={unauth_cap.status}"
        )

        preset_token = os.environ.get("STANDALONE_ACCESS_TOKEN", "").strip()
        if preset_token:
            _lib.record_runtime_secret("LOCAL_ACCESS_TOKEN", preset_token)
            headers = {"Authorization": f"Bearer {preset_token}"}
            me_resp = _lib.http_request(
                "GET", f"{base_url}/api/v1/users/me", headers=headers, timeout=HTTP_TIMEOUT_S
            )
            assert me_resp.status == 200, (
                f"GET /api/v1/users/me with STANDALONE_ACCESS_TOKEN failed: "
                f"status={me_resp.status}, fields={_lib.response_fields(me_resp)}"
            )
            me_data = me_resp.json()
            uid = str((me_data.get("user") or {}).get("user_id") or "").strip()
            assert uid, (
                f"GET /api/v1/users/me returned no user_id "
                f"(status={me_resp.status}, fields={_lib.response_field_names(me_data)})"
            )
            return headers, uid

        needs_bootstrap = bool(boot_resp.json().get("needs_bootstrap"))
        if needs_bootstrap:
            bootstrap_token = (
                ctx.env.get("BOOTSTRAP_SETUP_TOKEN")
                or os.environ.get("BOOTSTRAP_SETUP_TOKEN")
                or ""
            ).strip()
            assert bootstrap_token, (
                "BOOTSTRAP_SETUP_TOKEN is required to bootstrap local auth session in b2"
            )
            boot_password = f"B2AdminPass!{secrets.token_hex(6)}"
            _lib.record_runtime_secret("LOCAL_ADMIN_PASSWORD", boot_password)
            boot_post = _lib.http_request(
                "POST",
                f"{base_url}/api/v1/dalton/bootstrap/admin",
                json_body={
                    "bootstrap_token": bootstrap_token,
                    "email": f"b2-admin-{secrets.token_hex(4)}@core.local",
                    "username": f"b2_admin_{secrets.token_hex(3)}",
                    "name": "B2 Capabilities Admin",
                    "password": boot_password,
                },
                timeout=HTTP_TIMEOUT_S,
            )
            assert boot_post.status == 201, (
                f"POST /api/v1/dalton/bootstrap/admin failed: "
                f"status={boot_post.status}, fields={_lib.response_fields(boot_post)}"
            )
            boot_data = boot_post.json()
            access_token = str(boot_data.get("access_token") or "").strip()
            _lib.record_runtime_secret("LOCAL_ACCESS_TOKEN", access_token)
            admin_uid = str((boot_data.get("user") or {}).get("user_id") or "").strip()
            assert access_token and admin_uid, (
                f"Bootstrap response missing access_token/user_id "
                f"(status={boot_post.status}, fields={_lib.response_field_names(boot_data)})"
            )
            return {"Authorization": f"Bearer {access_token}"}, admin_uid

        identifier = (
            os.environ.get("STANDALONE_ADMIN_IDENTIFIER")
            or os.environ.get("STANDALONE_ADMIN_EMAIL")
            or ""
        ).strip()
        password = os.environ.get("STANDALONE_ADMIN_PASSWORD", "")
        assert identifier and password, (
            "Local auth instance is already bootstrapped; set STANDALONE_ACCESS_TOKEN or "
            "STANDALONE_ADMIN_IDENTIFIER + STANDALONE_ADMIN_PASSWORD to run b2"
        )
        _lib.record_runtime_secret("LOCAL_ADMIN_PASSWORD", password)
        login_resp = _lib.http_request(
            "POST",
            f"{base_url}/api/v1/dalton/auth/local/login",
            json_body={"identifier": identifier, "password": password},
            timeout=HTTP_TIMEOUT_S,
        )
        assert login_resp.status == 200, (
            f"POST /api/v1/dalton/auth/local/login failed: "
            f"status={login_resp.status}, fields={_lib.response_fields(login_resp)}"
        )
        login_data = login_resp.json()
        access_token = str(login_data.get("access_token") or "").strip()
        _lib.record_runtime_secret("LOCAL_ACCESS_TOKEN", access_token)
        uid = str((login_data.get("user") or {}).get("user_id") or "").strip()
        assert access_token and uid, (
            f"Login response missing access_token/user_id "
            f"(status={login_resp.status}, fields={_lib.response_field_names(login_data)})"
        )
        return {"Authorization": f"Bearer {access_token}"}, uid

    headers = _lib.oss_owner_headers(ctx)
    user_id = os.environ.get("TEST_USER_ID", DEFAULT_USER_ID).strip() or DEFAULT_USER_ID
    return headers, user_id


def _check_capabilities_and_disabled_routes(
    base_url: str,
    *,
    headers: dict[str, str] | None = None,
) -> None:
    log("3/5 Checking /dalton/capabilities, /dalton/system-jobs, and disabled capability routes...")
    cap_resp = _lib.http_request(
        "GET",
        f"{base_url}/api/v1/dalton/capabilities",
        headers=headers,
        timeout=HTTP_TIMEOUT_S,
    )
    assert cap_resp.status == 200, (
        f"GET /dalton/capabilities failed: {cap_resp.status} {cap_resp.text}"
    )
    cap_body = cap_resp.json()
    assert cap_body.get("profile") == "core", f"Expected profile='core', got {cap_body!r}"
    assert cap_body.get("capabilities") == EXPECTED_CORE_DEFAULT_CAPABILITIES, (
        f"Expected Core default capabilities {EXPECTED_CORE_DEFAULT_CAPABILITIES!r}, "
        f"got {cap_body!r}"
    )

    jobs_resp = _lib.http_request(
        "GET",
        f"{base_url}/api/v1/dalton/system-jobs",
        headers=headers,
        timeout=HTTP_TIMEOUT_S,
    )
    assert jobs_resp.status == 200, (
        f"GET /dalton/system-jobs failed: {jobs_resp.status} {jobs_resp.text}"
    )
    jobs_payload = jobs_resp.json()
    jobs = jobs_payload.get("jobs", jobs_payload) if isinstance(jobs_payload, dict) else jobs_payload
    assert isinstance(jobs, list), f"Unexpected system-jobs payload: {jobs_payload!r}"
    leaked_ops = [
        j.get("key")
        for j in jobs
        if isinstance(j, dict)
        and str(j.get("key", "")).startswith(("ops-", "ops:", "data-", "data:"))
    ]
    assert not leaked_ops, f"Disabled host/data system jobs appeared in Core: {leaked_ops}"

    for enabled_flag in (False, True):
        toggle_resp = _lib.http_request(
            "PATCH",
            f"{base_url}/api/v1/plugins/dalton-data/enabled",
            headers=headers,
            json_body={"enabled": enabled_flag},
            timeout=HTTP_TIMEOUT_S,
        )
        assert toggle_resp.status == 404, (
            f"PATCH /api/v1/plugins/dalton-data/enabled ({enabled_flag}) returned "
            f"{toggle_resp.status} (expected 404): {toggle_resp.text}"
        )

    disabled_requests: list[tuple[str, str, dict[str, Any] | None]] = [
        ("GET", "/api/v1/dalton/channels/bindings", None),
        ("GET", "/api/v1/dalton/feishu/spaces", None),
        ("POST", "/api/v1/dalton/notify/test", {"target": "feishu:chat:test"}),
        ("GET", "/api/v1/dalton/transcriptions/usage", None),
        ("GET", "/api/v1/dalton/native-models/status", None),
        ("GET", "/api/v1/oauth/muse/status", None),
        ("GET", "/api/v1/oauth/antigravity/status", None),
        ("GET", "/api/v1/oauth/xai/status", None),
    ]
    for method, path, payload in disabled_requests:
        resp = _lib.http_request(
            method,
            f"{base_url}{path}",
            headers=headers,
            json_body=payload,
            timeout=HTTP_TIMEOUT_S,
        )
        assert resp.status in (403, 404), (
            f"Disabled capability route {method} {path} returned {resp.status} "
            f"(expected 403/404): {resp.text}"
        )
    log(
        f"3/5 OK (capabilities='core' {EXPECTED_CORE_DEFAULT_CAPABILITIES}, "
        "0 ops/data jobs, dalton-data toggle 404, disabled routes 403/404)."
    )


def _check_dalton_sec_plugin_toggle_closure(
    ctx: _lib.ComposeContext,
    base_url: str,
    user_id: str,
    *,
    headers: dict[str, str] | None = None,
) -> None:
    log("4/5 Verifying three-way dalton-sec plugin toggle closure (plugins / builtins / workspace MCP)...")
    quoted_uid = _sql_quote(user_id)
    ctx.psql_exec(
        f"""
        INSERT INTO users (user_id, email, name)
        VALUES ({quoted_uid}, 'local-dev@localhost.test', 'Local Dev User')
        ON CONFLICT (user_id) DO NOTHING;
        """
    )

    def _plugin_map() -> dict[str, dict[str, Any]]:
        resp = _lib.http_request(
            "GET", f"{base_url}/api/v1/plugins", headers=headers, timeout=HTTP_TIMEOUT_S
        )
        assert resp.status == 200, f"GET /api/v1/plugins failed: {resp.status} {resp.text}"
        return {p["name"]: p for p in resp.json()["plugins"]}

    def _builtin_map() -> dict[str, dict[str, Any]]:
        resp = _lib.http_request(
            "GET", f"{base_url}/api/v1/mcp/builtin-servers", headers=headers, timeout=HTTP_TIMEOUT_S
        )
        assert resp.status == 200, (
            f"GET /api/v1/mcp/builtin-servers failed: {resp.status} {resp.text}"
        )
        return {s["name"]: s for s in resp.json()["servers"]}

    def _reset_state() -> None:
        ctx.psql_exec(
            f"""
            DELETE FROM user_mcp_builtin_disables
            WHERE user_id = {quoted_uid}
              AND kind = 'bundle'
              AND name IN ('dalton-data', 'dalton-sec');
            DELETE FROM dalton_plugin_disable_overrides
            WHERE user_id = {quoted_uid}
              AND bundle_name = 'dalton-sec';
            """
        )

    def _assert_workspace_mcp_consistent(ws_id: str, *, expected_sec_active: bool) -> None:
        resp = _lib.http_request(
            "GET",
            f"{base_url}/api/v1/workspaces/{ws_id}/mcp/servers",
            headers=headers,
            timeout=HTTP_TIMEOUT_S,
        )
        assert resp.status == 200, (
            f"GET /api/v1/workspaces/{ws_id}/mcp/servers failed: {resp.status} {resp.text}"
        )
        body = resp.json()
        servers = body.get("servers") if isinstance(body, dict) else None
        assert isinstance(servers, list), (
            f"Unexpected workspace MCP servers payload: {body!r}"
        )
        by_name = {s.get("name"): s for s in servers if isinstance(s, dict)}
        assert "researchdata" not in by_name, "researchdata leaked into workspace MCP servers"
        assert "dalton_sec" in by_name, (
            f"dalton_sec missing from workspace MCP servers payload: {sorted(by_name)}"
        )
        entry = by_name["dalton_sec"]
        active_names = {
            s["name"]
            for s in servers
            if isinstance(s, dict)
            and s.get("enabled") is True
            and s.get("status") != "disabled"
        }
        if expected_sec_active:
            assert "dalton_sec" in active_names, (
                f"Expected dalton_sec to be active in workspace MCP servers: {entry!r}"
            )
            assert entry.get("enabled") is True
            assert entry.get("disabled_scope") is None
        else:
            assert "dalton_sec" not in active_names, (
                f"Expected dalton_sec to be withdrawn from active workspace MCP servers: {entry!r}"
            )
            assert entry.get("enabled") is False
            assert entry.get("status") == "disabled"
            assert entry.get("disabled_scope") == "user"

    ws_id: str | None = None
    _reset_state()
    try:
        ws_resp = _lib.http_request(
            "POST",
            f"{base_url}/api/v1/workspaces",
            headers=headers,
            json_body={"name": "b2-plugin-check"},
            timeout=HTTP_TIMEOUT_S,
        )
        assert ws_resp.status in (200, 201), (
            f"POST /api/v1/workspaces failed: {ws_resp.status} {ws_resp.text}"
        )
        ws_body = ws_resp.json()
        ws_id = ws_body.get("workspace_id") or ws_body.get("id")
        assert ws_id, f"Workspace creation returned no workspace_id: {ws_body!r}"

        # Step 1: Fresh state — dalton-data absent, dalton-sec enabled
        plugins_0 = _plugin_map()
        assert "dalton-data" not in plugins_0, "dalton-data must not appear in Core plugin list"
        assert "dalton-sec" in plugins_0 and plugins_0["dalton-sec"]["enabled"] is True
        builtins_0 = _builtin_map()
        assert "researchdata" not in builtins_0, (
            "researchdata must not appear in Core builtin servers"
        )
        assert "dalton_sec" in builtins_0 and builtins_0["dalton_sec"]["plugin_enabled"] is True
        _assert_workspace_mcp_consistent(ws_id, expected_sec_active=True)

        # Step 2: Seed legacy pre-split disable ('bundle', 'dalton-data') with no d045 override
        ctx.psql_exec(
            f"""
            INSERT INTO user_mcp_builtin_disables (user_id, kind, name)
            VALUES ({quoted_uid}, 'bundle', 'dalton-data')
            ON CONFLICT (user_id, kind, name) DO NOTHING;
            """
        )

        plugins_1 = _plugin_map()
        assert "dalton-data" not in plugins_1
        assert plugins_1["dalton-sec"]["enabled"] is False, (
            "dalton-sec must inherit legacy ('bundle', 'dalton-data') disable"
        )
        builtins_1 = _builtin_map()
        assert builtins_1["dalton_sec"]["plugin_enabled"] is False
        _assert_workspace_mcp_consistent(ws_id, expected_sec_active=False)

        # Step 3: Explicit re-enable via PATCH /api/v1/plugins/dalton-sec/enabled
        patch_on = _lib.http_request(
            "PATCH",
            f"{base_url}/api/v1/plugins/dalton-sec/enabled",
            headers=headers,
            json_body={"enabled": True},
            timeout=HTTP_TIMEOUT_S,
        )
        assert patch_on.status == 200, (
            f"PATCH enable dalton-sec failed: {patch_on.status} {patch_on.text}"
        )
        assert patch_on.json() == {"name": "dalton-sec", "enabled": True}

        override_row = ctx.psql_query(
            f"""
            SELECT 1 FROM dalton_plugin_disable_overrides
            WHERE user_id = {quoted_uid} AND bundle_name = 'dalton-sec';
            """
        )
        assert override_row == "1", (
            f"Expected d045 override row after re-enabling dalton-sec (got {override_row!r})"
        )

        plugins_2 = _plugin_map()
        assert plugins_2["dalton-sec"]["enabled"] is True
        builtins_2 = _builtin_map()
        assert builtins_2["dalton_sec"]["plugin_enabled"] is True
        _assert_workspace_mcp_consistent(ws_id, expected_sec_active=True)

        # Step 4: Explicit re-disable via PATCH /api/v1/plugins/dalton-sec/enabled
        patch_off = _lib.http_request(
            "PATCH",
            f"{base_url}/api/v1/plugins/dalton-sec/enabled",
            headers=headers,
            json_body={"enabled": False},
            timeout=HTTP_TIMEOUT_S,
        )
        assert patch_off.status == 200, (
            f"PATCH disable dalton-sec failed: {patch_off.status} {patch_off.text}"
        )
        assert patch_off.json() == {"name": "dalton-sec", "enabled": False}

        cleared_override = ctx.psql_query(
            f"""
            SELECT 1 FROM dalton_plugin_disable_overrides
            WHERE user_id = {quoted_uid} AND bundle_name = 'dalton-sec';
            """
        )
        assert cleared_override == "", (
            f"d045 override row must be cleared on re-disable (got {cleared_override!r})"
        )
        sec_disable = ctx.psql_query(
            f"""
            SELECT 1 FROM user_mcp_builtin_disables
            WHERE user_id = {quoted_uid} AND kind = 'bundle' AND name = 'dalton-sec';
            """
        )
        assert sec_disable == "1", (
            f"Expected ('bundle', 'dalton-sec') disable row on re-disable (got {sec_disable!r})"
        )

        plugins_3 = _plugin_map()
        assert plugins_3["dalton-sec"]["enabled"] is False
        builtins_3 = _builtin_map()
        assert builtins_3["dalton_sec"]["plugin_enabled"] is False
        _assert_workspace_mcp_consistent(ws_id, expected_sec_active=False)
        log("4/5 OK (fresh -> legacy inherit -> d045 override enable -> explicit disable verified).")
    finally:
        _reset_state()
        if ws_id:
            _lib.http_request(
                "DELETE",
                f"{base_url}/api/v1/workspaces/{ws_id}",
                headers=headers,
                timeout=HTTP_TIMEOUT_S,
            )


def _check_no_bridge_or_gateway_calls_in_logs(ctx: _lib.ComposeContext | None = None) -> None:
    log("5/5 Checking backend logs for zero :8960 / :8962 bridge or gateway attempts...")
    container = os.environ.get("BACKEND_CONTAINER", "").strip()
    if container:
        proc = subprocess.run(
            ["docker", "logs", container],
            capture_output=True,
            text=True,
            timeout=30.0,
            check=False,
        )
        assert proc.returncode == 0, f"docker logs {container} failed: {proc.stderr}"
        combined = proc.stdout + "\n" + proc.stderr
    elif ctx is not None:
        proc_bytes = ctx.compose("logs", "--no-color", "backend")
        combined = (
            proc_bytes.stdout.decode("utf-8", errors="replace")
            + "\n"
            + proc_bytes.stderr.decode("utf-8", errors="replace")
        )
    else:
        return

    for forbidden_port in (":8960", ":8962"):
        assert forbidden_port not in combined, (
            f"Backend logs contain forbidden bridge/gateway reference {forbidden_port}"
        )
    log("5/5 OK (0 references to :8960 or :8962 in backend logs).")


def run_checks() -> None:
    t0 = time.monotonic()
    ctx = _lib.ComposeContext.from_environment()
    base_url = (os.environ.get("STANDALONE_BASE_URL") or ctx.edge_url).rstrip("/")

    _lib.wait_for_http_ready(f"{base_url}/api/v1/auth/cli", timeout=60.0)
    auth_headers, user_id = _acquire_auth_session(ctx, base_url)
    _check_migrations_at_head(ctx)
    _check_image_layers_and_export(ctx)
    _check_capabilities_and_disabled_routes(base_url, headers=auth_headers)
    _check_dalton_sec_plugin_toggle_closure(ctx, base_url, user_id, headers=auth_headers)
    _check_no_bridge_or_gateway_calls_in_logs(ctx)

    elapsed = time.monotonic() - t0
    log(f"ALL CHECKS PASSED in {elapsed:.1f}s.")


if __name__ == "__main__":
    try:
        run_checks()
    except AssertionError as exc:
        print(f"[b2_backend_capabilities] FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
