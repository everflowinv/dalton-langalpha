# auth: oss
"""Standalone Core Batch 2 acceptance checks (`b2_backend_capabilities.py`).

Verifies on a running Standalone Core (`auth: oss`) deployment:
1. Both Alembic migration chains (upstream + Dalton) are at their current repo
   ``head`` revisions and ``dalton_plugin_disable_overrides`` (d045) exists.
2. Exported release package / Docker image layers (every ``layer.tar`` inside a
   ``docker save`` archive) contain zero ``plugins/dalton_data/`` files and zero
   ``Proprietary`` plugin manifests, while shipping ``plugins/dalton_sec/``.
3. ``GET /api/v1/dalton/capabilities`` reports ``profile == "core"`` with zero
   enabled optional capabilities, ``GET /api/v1/dalton/system-jobs`` lists zero
   host ``ops-*`` or ``data-*`` jobs, disabled capability routes return 404/403,
   and backend logs show zero outbound attempts to ``:8960`` / ``:8962``.
4. Three-way plugin toggle consistency for ``dalton-sec`` across
   ``GET /api/v1/plugins``, ``PATCH /api/v1/plugins/dalton-sec/enabled``,
   ``GET /api/v1/mcp/builtin-servers``, and workspace MCP effective resolution
   (including inherited legacy ``('bundle', 'dalton-data')`` disable, explicit
   re-enable via ``dalton_plugin_disable_overrides``, and subsequent disable).

Environment variables:
- ``STANDALONE_BASE_URL`` (default ``http://127.0.0.1:8088``): edge/backend base URL
- ``DATABASE_URL``: PostgreSQL DSN for direct DB & migration assertions
- ``TEST_USER_ID`` (default ``local-dev-user``): OSS test user id
- ``BACKEND_IMAGE`` / ``BACKEND_IMAGE_TAR``: Docker image tag or saved tar path for layer scan
- ``EXPORT_DIR``: optional exported clean release directory to scan alongside image layers
- ``BACKEND_CONTAINER``: optional container name whose logs are scanned for ``:8960`` / ``:8962``
"""

from __future__ import annotations

import ast
import io
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

import httpx
import psycopg

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BASE_URL = "http://127.0.0.1:8088"
DEFAULT_USER_ID = "local-dev-user"
HTTP_TIMEOUT_S = 30.0
DOCKER_TIMEOUT_S = 120.0


def _discover_alembic_heads(versions_dir: Path) -> set[str]:
    """Parse ``revision`` and ``down_revision`` from Alembic migration files via AST."""
    revisions: dict[str, set[str]] = {}
    for py_file in sorted(versions_dir.glob("*.py")):
        if py_file.name.startswith("__"):
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        rev: str | None = None
        down: set[str] = set()
        for node in tree.body:
            target_name: str | None = None
            value_node: ast.AST | None = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                target_name = node.targets[0].id
                value_node = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target_name = node.target.id
                value_node = node.value
            if target_name == "revision" and isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
                rev = value_node.value
            elif target_name == "down_revision" and value_node is not None:
                if isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
                    down.add(value_node.value)
                elif isinstance(value_node, (ast.Tuple, ast.List)):
                    for elt in value_node.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            down.add(elt.value)
        if rev is not None:
            revisions[rev] = down
    parents = {p for deps in revisions.values() for p in deps}
    return {r for r in revisions if r not in parents}


def _check_migrations_at_head(database_url: str) -> None:
    expected_upstream = _discover_alembic_heads(REPO_ROOT / "migrations" / "versions")
    expected_dalton = _discover_alembic_heads(REPO_ROOT / "src" / "dalton" / "migrations" / "versions")
    assert expected_upstream, "Failed to discover upstream Alembic heads"
    assert expected_dalton, "Failed to discover Dalton Alembic heads"

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version_num FROM alembic_version")
            actual_upstream = {row[0] for row in cur.fetchall()}
            assert actual_upstream == expected_upstream, (
                f"Upstream alembic_version mismatch: actual={actual_upstream}, expected={expected_upstream}"
            )

            cur.execute("SELECT version_num FROM alembic_version_dalton")
            actual_dalton = {row[0] for row in cur.fetchall()}
            assert actual_dalton == expected_dalton, (
                f"Dalton alembic_version_dalton mismatch: actual={actual_dalton}, expected={expected_dalton}"
            )

            cur.execute("SELECT to_regclass('public.dalton_plugin_disable_overrides')")
            reg = cur.fetchone()
            assert reg is not None and reg[0] == "dalton_plugin_disable_overrides", (
                "Missing d045 table dalton_plugin_disable_overrides"
            )


def _scan_tar_stream(tar: tarfile.TarFile, *, label: str) -> tuple[list[str], list[str], set[str]]:
    """Scan a tar archive for forbidden ``plugins/dalton_data/`` paths and ``Proprietary`` manifests."""
    forbidden_paths: list[str] = []
    proprietary_manifests: list[str] = []
    seen_sec_files: set[str] = set()

    for member in tar.getmembers():
        norm = member.name.lstrip("./")
        if "plugins/dalton_data/" in norm or norm.endswith("plugins/dalton_data"):
            forbidden_paths.append(f"{label}:{norm}")
        if norm.endswith("plugins/dalton_sec/plugin.json"):
            seen_sec_files.add("plugin.json")
        if norm.endswith("plugins/dalton_sec/dalton_sec_mcp_server.py"):
            seen_sec_files.add("dalton_sec_mcp_server.py")
        if (
            member.isfile()
            and norm.endswith("/plugin.json")
            and "plugins/" in norm
            and member.size <= 1_048_576
        ):
            extracted = tar.extractfile(member)
            if extracted is not None:
                text = extracted.read().decode("utf-8", errors="replace")
                if "Proprietary" in text:
                    proprietary_manifests.append(f"{label}:{norm}")
    return forbidden_paths, proprietary_manifests, seen_sec_files


def _check_image_layers_and_export() -> None:
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

    ci_mode = os.environ.get("CI", "").strip().lower() in ("1", "true", "yes")
    if not image_tar_env and not image_ref:
        if ci_mode:
            raise AssertionError(
                "CI=true requires BACKEND_IMAGE or BACKEND_IMAGE_TAR so image layers "
                "are verified to contain zero plugins/dalton_data/ and zero Proprietary manifests"
            )
        return

    tmp_file: Path | None = None
    try:
        if image_tar_env:
            tar_path = Path(image_tar_env)
        else:
            fd, raw_tmp = tempfile.mkstemp(prefix="b2-image-", suffix=".tar")
            os.close(fd)
            tmp_file = Path(raw_tmp)
            proc = subprocess.run(
                ["docker", "save", "-o", str(tmp_file), image_ref],
                capture_output=True,
                text=True,
                timeout=DOCKER_TIMEOUT_S,
                check=False,
            )
            assert proc.returncode == 0, f"docker save {image_ref} failed: {proc.stderr}"
            tar_path = tmp_file

        all_forbidden: list[str] = []
        all_proprietary: list[str] = []
        all_sec_files: set[str] = set()
        layer_count = 0

        with tarfile.open(tar_path, mode="r:*") as outer:
            for member in outer.getmembers():
                if not member.isfile():
                    continue
                is_layer = member.name.endswith("layer.tar") or member.name.startswith("blobs/sha256/")
                if not is_layer:
                    continue
                blob = outer.extractfile(member)
                if blob is None:
                    continue
                raw_bytes = blob.read()
                try:
                    with tarfile.open(fileobj=io.BytesIO(raw_bytes), mode="r:*") as layer_tar:
                        layer_count += 1
                        f_paths, p_manifests, sec_files = _scan_tar_stream(
                            layer_tar, label=member.name
                        )
                        all_forbidden.extend(f_paths)
                        all_proprietary.extend(p_manifests)
                        all_sec_files.update(sec_files)
                except tarfile.ReadError:
                    # Non-tar OCI manifest/config JSON blob
                    continue

        assert layer_count > 0, f"No tar layers found in image archive {tar_path}"
        assert not all_forbidden, f"Forbidden plugins/dalton_data/ found in image layers: {all_forbidden}"
        assert not all_proprietary, f"Forbidden Proprietary manifests found in image layers: {all_proprietary}"
        assert {"plugin.json", "dalton_sec_mcp_server.py"} <= all_sec_files, (
            f"plugins/dalton_sec/ incomplete in image layers: found {sorted(all_sec_files)}"
        )
    finally:
        if tmp_file is not None and tmp_file.exists():
            tmp_file.unlink(missing_ok=True)


def _check_capabilities_and_disabled_routes(client: httpx.Client) -> None:
    cap_resp = client.get("/api/v1/dalton/capabilities")
    assert cap_resp.status_code == 200, f"GET /dalton/capabilities failed: {cap_resp.status_code} {cap_resp.text}"
    cap_body = cap_resp.json()
    assert cap_body.get("profile") == "core", f"Expected profile='core', got {cap_body!r}"
    assert cap_body.get("capabilities") == [], f"Expected zero optional capabilities in Core, got {cap_body!r}"

    jobs_resp = client.get("/api/v1/dalton/system-jobs")
    assert jobs_resp.status_code == 200, f"GET /dalton/system-jobs failed: {jobs_resp.status_code} {jobs_resp.text}"
    jobs_payload = jobs_resp.json()
    jobs = jobs_payload.get("jobs", jobs_payload) if isinstance(jobs_payload, dict) else jobs_payload
    assert isinstance(jobs, list)
    leaked_ops = [j.get("key") for j in jobs if isinstance(j, dict) and str(j.get("key", "")).startswith(("ops-", "data-"))]
    assert not leaked_ops, f"Disabled host/data system jobs appeared in Core: {leaked_ops}"

    disabled_requests: list[tuple[str, str, dict[str, Any] | None]] = [
        ("GET", "/api/v1/dalton/channels/bindings", None),
        ("GET", "/api/v1/dalton/feishu/spaces", None),
        ("POST", "/api/v1/dalton/notify/test", {"target": "feishu:chat:test"}),
        ("GET", "/api/v1/dalton/transcriptions/usage", None),
        ("GET", "/api/v1/dalton/native-models/status", None),
        ("GET", "/api/v1/oauth/muse/status", None),
        ("GET", "/api/v1/oauth/antigravity/status", None),
        ("GET", "/api/v1/oauth/xai/status", None),
        ("PATCH", "/api/v1/plugins/dalton-data/enabled", {"enabled": False}),
        ("PATCH", "/api/v1/plugins/dalton-data/enabled", {"enabled": True}),
    ]
    for method, path, payload in disabled_requests:
        resp = client.request(method, path, json=payload)
        assert resp.status_code in (403, 404), (
            f"Disabled capability route {method} {path} returned {resp.status_code} (expected 403/404): {resp.text}"
        )


def _check_no_bridge_or_gateway_calls_in_logs() -> None:
    container = os.environ.get("BACKEND_CONTAINER", "").strip()
    if not container:
        return
    proc = subprocess.run(
        ["docker", "logs", container],
        capture_output=True,
        text=True,
        timeout=30.0,
        check=False,
    )
    assert proc.returncode == 0, f"docker logs {container} failed: {proc.stderr}"
    combined = proc.stdout + "\n" + proc.stderr
    for forbidden_port in (":8960", ":8962"):
        assert forbidden_port not in combined, (
            f"Backend logs contain forbidden bridge/gateway reference {forbidden_port}"
        )


def _check_dalton_sec_plugin_toggle_closure(
    client: httpx.Client,
    database_url: str,
    user_id: str,
) -> None:
    def _plugin_map() -> dict[str, dict[str, Any]]:
        resp = client.get("/api/v1/plugins")
        assert resp.status_code == 200, f"GET /api/v1/plugins failed: {resp.status_code} {resp.text}"
        return {p["name"]: p for p in resp.json()["plugins"]}

    def _builtin_map() -> dict[str, dict[str, Any]]:
        resp = client.get("/api/v1/mcp/builtin-servers")
        assert resp.status_code == 200, f"GET /api/v1/mcp/builtin-servers failed: {resp.status_code} {resp.text}"
        return {s["name"]: s for s in resp.json()["servers"]}

    def _reset_state() -> None:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM user_mcp_builtin_disables WHERE user_id = %s AND kind = 'bundle' AND name IN ('dalton-data', 'dalton-sec')",
                    (user_id,),
                )
                cur.execute(
                    "DELETE FROM dalton_plugin_disable_overrides WHERE user_id = %s AND bundle_name = 'dalton-sec'",
                    (user_id,),
                )
            conn.commit()

    def _assert_workspace_mcp_consistent(ws_id: str, *, expected_sec_active: bool) -> None:
        resp = client.get(f"/api/v1/workspaces/{ws_id}/mcp/servers")
        assert resp.status_code == 200, (
            f"GET /api/v1/workspaces/{ws_id}/mcp/servers failed: {resp.status_code} {resp.text}"
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
        # Create workspace to verify workspace MCP resolution endpoint alongside plugin/builtin routes
        ws_resp = client.post("/api/v1/workspaces", json={"name": "b2-plugin-check"})
        assert ws_resp.status_code in (200, 201), (
            f"POST /api/v1/workspaces failed: {ws_resp.status_code} {ws_resp.text}"
        )
        ws_body = ws_resp.json()
        ws_id = ws_body.get("workspace_id") or ws_body.get("id")
        assert ws_id, f"Workspace creation returned no workspace_id: {ws_body!r}"

        # Step 1: Fresh state — dalton-data absent, dalton-sec enabled
        plugins_0 = _plugin_map()
        assert "dalton-data" not in plugins_0, "dalton-data must not appear in Core plugin list"
        assert "dalton-sec" in plugins_0 and plugins_0["dalton-sec"]["enabled"] is True
        builtins_0 = _builtin_map()
        assert "researchdata" not in builtins_0, "researchdata must not appear in Core builtin servers"
        assert "dalton_sec" in builtins_0 and builtins_0["dalton_sec"]["plugin_enabled"] is True
        _assert_workspace_mcp_consistent(ws_id, expected_sec_active=True)

        # Step 2: Seed legacy pre-split disable ('bundle', 'dalton-data') with no d045 override
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO user_mcp_builtin_disables (user_id, kind, name)
                    VALUES (%s, 'bundle', 'dalton-data')
                    ON CONFLICT (user_id, kind, name) DO NOTHING
                    """,
                    (user_id,),
                )
            conn.commit()

        plugins_1 = _plugin_map()
        assert "dalton-data" not in plugins_1
        assert plugins_1["dalton-sec"]["enabled"] is False, (
            "dalton-sec must inherit legacy ('bundle', 'dalton-data') disable"
        )
        builtins_1 = _builtin_map()
        assert builtins_1["dalton_sec"]["plugin_enabled"] is False
        _assert_workspace_mcp_consistent(ws_id, expected_sec_active=False)

        # Step 3: Explicit re-enable via PATCH /api/v1/plugins/dalton-sec/enabled
        patch_on = client.patch("/api/v1/plugins/dalton-sec/enabled", json={"enabled": True})
        assert patch_on.status_code == 200, f"PATCH enable dalton-sec failed: {patch_on.status_code} {patch_on.text}"
        assert patch_on.json() == {"name": "dalton-sec", "enabled": True}

        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM dalton_plugin_disable_overrides WHERE user_id = %s AND bundle_name = 'dalton-sec'",
                    (user_id,),
                )
                assert cur.fetchone() is not None, "Expected d045 override row after re-enabling dalton-sec"

        plugins_2 = _plugin_map()
        assert plugins_2["dalton-sec"]["enabled"] is True
        builtins_2 = _builtin_map()
        assert builtins_2["dalton_sec"]["plugin_enabled"] is True
        _assert_workspace_mcp_consistent(ws_id, expected_sec_active=True)

        # Step 4: Explicit re-disable via PATCH /api/v1/plugins/dalton-sec/enabled
        patch_off = client.patch("/api/v1/plugins/dalton-sec/enabled", json={"enabled": False})
        assert patch_off.status_code == 200, f"PATCH disable dalton-sec failed: {patch_off.status_code} {patch_off.text}"
        assert patch_off.json() == {"name": "dalton-sec", "enabled": False}

        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM dalton_plugin_disable_overrides WHERE user_id = %s AND bundle_name = 'dalton-sec'",
                    (user_id,),
                )
                assert cur.fetchone() is None, "d045 override row must be cleared on re-disable"
                cur.execute(
                    "SELECT 1 FROM user_mcp_builtin_disables WHERE user_id = %s AND kind = 'bundle' AND name = 'dalton-sec'",
                    (user_id,),
                )
                assert cur.fetchone() is not None, "Expected ('bundle', 'dalton-sec') disable row on re-disable"

        plugins_3 = _plugin_map()
        assert plugins_3["dalton-sec"]["enabled"] is False
        builtins_3 = _builtin_map()
        assert builtins_3["dalton_sec"]["plugin_enabled"] is False
        _assert_workspace_mcp_consistent(ws_id, expected_sec_active=False)
    finally:
        _reset_state()
        if ws_id:
            client.delete(f"/api/v1/workspaces/{ws_id}")


def run_checks() -> None:
    base_url = os.environ.get("STANDALONE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    database_url = os.environ.get("DATABASE_URL", "").strip()
    user_id = os.environ.get("TEST_USER_ID", DEFAULT_USER_ID).strip() or DEFAULT_USER_ID

    assert database_url, "DATABASE_URL must be set to run b2_backend_capabilities checks"

    _check_migrations_at_head(database_url)
    _check_image_layers_and_export()

    with httpx.Client(base_url=base_url, timeout=HTTP_TIMEOUT_S) as client:
        _check_capabilities_and_disabled_routes(client)
        _check_dalton_sec_plugin_toggle_closure(client, database_url, user_id)

    _check_no_bridge_or_gateway_calls_in_logs()
    print("[b2_backend_capabilities] ALL CHECKS PASSED")


if __name__ == "__main__":
    try:
        run_checks()
    except AssertionError as exc:
        print(f"[b2_backend_capabilities] FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
