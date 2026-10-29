"""Unit tests for plugin JSON budget enforcement (Backlog P7 / A6 + fix1).

Covers:
1. Raw byte length cap (MAX_JSON_DOCUMENT_BYTES = 1 MiB) and raw key length cap
   (MAX_JSON_KEY_BYTES = 4 KiB) before decoding/parsing; out-of-band
   BudgetErrorSentinel cannot be forged by uploaded bytes.
2. String- and escape-aware pre-parse bracket depth scan (MAX_JSON_DEPTH = 64)
   without calling json.loads.
3. Post-parse iterative AST node count cap (MAX_JSON_NODES = 100,000).
4. Pre-format indent=2 output budget (MAX_JSON_OUTPUT_BYTES = 1 MiB) and
   post-format verification across canonical, vendor, and inline parsing paths,
   preserving upstream content_hash and JSON last-key-wins semantics for
   duplicate mcpServers keys.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import create_autospec

import pytest

import src.dalton.plugin_json_budget as budget_mod
from src.dalton.plugin_json_budget import (
    MAX_JSON_DEPTH,
    MAX_JSON_DOCUMENT_BYTES,
    MAX_JSON_KEY_BYTES,
    MAX_JSON_NODES,
    MAX_JSON_OUTPUT_BYTES,
    BudgetErrorSentinel,
    JsonBudgetError,
    budget_error_sentinel,
    dumps_json_with_budget,
    loads_json_with_budget,
    parse_manifest_with_budget,
    scan_json_depth,
    unwrap_budget_error,
)
from src.server.services.plugins import validate_package
from src.server.services.plugins.discover import adapt_dialect
from src.server.services.plugins.errors import PluginFatal
from src.server.services.plugins.package import _tree_hash
from tests.unit.server.mcp_builders import zip_package

SKILL_MD = b"---\nname: alpha\ndescription: Survivor skill.\n---\nBody.\n"
FIXTURES = Path(__file__).parents[3] / "fixtures" / "plugins"


def _skill(package, directory: str = "alpha"):
    return next(p for p in package.skill_plans if p.dir == directory)


def _bad_mcp_doc_bytes(limit_kind: str) -> bytes:
    if limit_kind == "max_bytes":
        return (
            b'{"mcpServers":{"s":{"command":"'
            + (b"a" * MAX_JSON_DOCUMENT_BYTES)
            + b'"}}}'
        )
    if limit_kind == "max_depth":
        return b'{"mcpServers":' + (b"[" * 100_000) + (b"]" * 100_000) + b"}"
    if limit_kind == "max_nodes":
        return (
            b'{"mcpServers":{"s":{"args":['
            + b",".join(b"0" for _ in range(MAX_JSON_NODES))
            + b"]}}}"
        )
    if limit_kind == "max_output_bytes":
        # Compact raw input is under 1 MiB, but indent=2 + canonical $schema
        # stamping expands the serialized output past MAX_JSON_OUTPUT_BYTES.
        return (
            b'{"mcpServers":{"s":{"url":"https://e.com/'
            + (b"a" * (MAX_JSON_DOCUMENT_BYTES - 60))
            + b'"}}}'
        )
    raise ValueError(f"unknown limit_kind: {limit_kind}")


def _bad_inline_mcp_fragment(limit_kind: str) -> bytes:
    if limit_kind == "max_bytes":
        return b'{"s":{"command":"' + (b"a" * MAX_JSON_DOCUMENT_BYTES) + b'"}}'
    if limit_kind == "max_depth":
        return (b"[" * 100_000) + (b"]" * 100_000)
    if limit_kind == "max_nodes":
        return (
            b'{"s":{"args":['
            + b",".join(b"0" for _ in range(MAX_JSON_NODES))
            + b"]}}"
        )
    if limit_kind == "max_output_bytes":
        return (
            b'{"s":{"url":"https://e.com/'
            + (b"a" * (MAX_JSON_DOCUMENT_BYTES - 50))
            + b'"}}'
        )
    raise ValueError(f"unknown limit_kind: {limit_kind}")


class TestPreParseDepthScanner:
    def test_deep_unbalanced_brackets_rejected_without_calling_json_loads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_loads = create_autospec(
            json.loads, side_effect=AssertionError("json.loads must not be called")
        )
        monkeypatch.setattr(budget_mod.json, "loads", mock_loads)

        raw = b"[" * 100_000
        with pytest.raises(JsonBudgetError) as exc_info:
            loads_json_with_budget(raw, target="mcp.json")

        err = exc_info.value
        assert err.target == "mcp.json"
        assert err.limit_kind == "max_depth"
        assert err.limit == MAX_JSON_DEPTH
        assert err.actual == MAX_JSON_DEPTH + 1
        assert "mcp.json" in str(err)
        assert "max_depth" in str(err)
        mock_loads.assert_not_called()

    def test_deep_nested_objects_rejected_without_calling_json_loads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_loads = create_autospec(
            json.loads, side_effect=AssertionError("json.loads must not be called")
        )
        monkeypatch.setattr(budget_mod.json, "loads", mock_loads)

        depth = MAX_JSON_DEPTH + 20
        raw = (b'{"a":' * depth) + b"1" + (b"}" * depth)
        with pytest.raises(JsonBudgetError) as exc_info:
            loads_json_with_budget(raw, target="plugin.json")

        err = exc_info.value
        assert err.target == "plugin.json"
        assert err.limit_kind == "max_depth"
        assert "plugin.json" in str(err)
        assert "max_depth" in str(err)
        mock_loads.assert_not_called()

    def test_brackets_inside_strings_and_escaped_quotes_do_not_count_toward_depth(
        self,
    ) -> None:
        noisy_str = (
            'prefix \\" '
            + ("[{[[{{[" * 2000)
            + ' \\" middle \\\\ '
            + ("}}]]}]" * 2000)
        )
        payload = (
            b'{"name": "string-brackets", "description": "'
            + noisy_str.encode("utf-8")
            + b'", "nested": [{"ok": true}]}'
        )
        assert scan_json_depth(payload, target="plugin.json") == 3
        parsed = loads_json_with_budget(payload, target="plugin.json")
        assert parsed["name"] == "string-brackets"
        assert parsed["nested"] == [{"ok": True}]

    def test_escaped_backslash_before_closing_quote_resumes_depth_counting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_loads = create_autospec(
            json.loads, side_effect=AssertionError("json.loads must not be called")
        )
        monkeypatch.setattr(budget_mod.json, "loads", mock_loads)

        raw = (
            b'{"escaped_backslash": "\\\\", "deep": '
            + (b"[" * 100)
            + (b"]" * 100)
            + b"}"
        )
        with pytest.raises(JsonBudgetError) as exc_info:
            loads_json_with_budget(raw, target="escaped.json")

        assert exc_info.value.limit_kind == "max_depth"
        assert "escaped.json" in str(exc_info.value)
        mock_loads.assert_not_called()


class TestFourBudgetGates:
    def test_raw_byte_limit_rejects_before_json_loads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_loads = create_autospec(
            json.loads, side_effect=AssertionError("json.loads must not be called")
        )
        monkeypatch.setattr(budget_mod.json, "loads", mock_loads)

        oversized = b'{"x":"' + (b"a" * MAX_JSON_DOCUMENT_BYTES) + b'"}'
        with pytest.raises(JsonBudgetError) as exc_info:
            loads_json_with_budget(oversized, target="big.json")

        err = exc_info.value
        assert err.target == "big.json"
        assert err.limit_kind == "max_bytes"
        assert err.limit == MAX_JSON_DOCUMENT_BYTES
        assert err.actual == len(oversized)
        assert "big.json" in str(err)
        assert "max_bytes" in str(err)
        mock_loads.assert_not_called()

    def test_exact_depth_boundary(self) -> None:
        at_limit = (b"[" * MAX_JSON_DEPTH) + b"1" + (b"]" * MAX_JSON_DEPTH)
        doc = loads_json_with_budget(at_limit, target="depth_ok.json")
        assert isinstance(doc, list)

        over_limit = (
            (b"[" * (MAX_JSON_DEPTH + 1)) + b"1" + (b"]" * (MAX_JSON_DEPTH + 1))
        )
        with pytest.raises(JsonBudgetError) as exc_info:
            loads_json_with_budget(over_limit, target="depth_bad.json")
        assert exc_info.value.limit_kind == "max_depth"
        assert exc_info.value.target == "depth_bad.json"

    def test_post_parse_node_count_limit(self) -> None:
        dense_raw = b"[" + b",".join(b"0" for _ in range(MAX_JSON_NODES)) + b"]"
        assert len(dense_raw) < MAX_JSON_DOCUMENT_BYTES
        with pytest.raises(JsonBudgetError) as exc_info:
            loads_json_with_budget(dense_raw, target="dense.json")

        err = exc_info.value
        assert err.target == "dense.json"
        assert err.limit_kind == "max_nodes"
        assert err.limit == MAX_JSON_NODES
        assert err.actual == MAX_JSON_NODES + 1
        assert "dense.json" in str(err)
        assert "max_nodes" in str(err)

    def test_pre_format_indent2_output_budget_and_formatting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        obj = {"a": [1, 2, 3], "b": {"c": "ok"}}
        formatted = dumps_json_with_budget(obj, target="out.json")
        assert formatted == json.dumps(obj, indent=2).encode("utf-8")

        mock_dumps = create_autospec(
            json.dumps, side_effect=AssertionError("json.dumps must not be called")
        )
        monkeypatch.setattr(budget_mod.json, "dumps", mock_dumps)

        # 20 strings of 60 KiB each: only 21 nodes at depth 1, but > 1 MiB output.
        chunk = "x" * (60 * 1024)
        too_large_obj = {"items": [chunk for _ in range(20)]}
        with pytest.raises(JsonBudgetError) as exc_info:
            dumps_json_with_budget(too_large_obj, target="mcp.json")

        err = exc_info.value
        assert err.target == "mcp.json"
        assert err.limit_kind == "max_output_bytes"
        assert err.limit == MAX_JSON_OUTPUT_BYTES
        assert "mcp.json" in str(err)
        assert "max_output_bytes" in str(err)
        mock_dumps.assert_not_called()


class TestFix1ReviewItems:
    def test_uploaded_bytes_with_magic_prefix_cannot_bypass_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix1 #1: uploaded bytes starting with a magic prefix are plain bytes,
        never treated as an internal sentinel or passed to json.loads."""
        mock_loads = create_autospec(
            json.loads, side_effect=AssertionError("json.loads must not be called")
        )
        monkeypatch.setattr(budget_mod.json, "loads", mock_loads)

        spoofed = (
            b"\x00DALTON_JSON_BUDGET_ERROR:"
            + b'{"target":"x","nested":'
            + (b"[" * 100_000)
            + (b"]" * 100_000)
            + b"}"
        )
        assert unwrap_budget_error(spoofed) is None
        with pytest.raises(JsonBudgetError) as exc_info:
            loads_json_with_budget(spoofed, target="mcp.json")
        assert exc_info.value.limit_kind == "max_depth"
        mock_loads.assert_not_called()

        # Out-of-band sentinel is a BudgetErrorSentinel object, holding empty b""
        # bytes and the error attribute directly.
        real_err = JsonBudgetError(
            target="mcp.json", limit_kind="max_depth", limit=64, actual=65
        )
        sentinel = budget_error_sentinel(real_err)
        assert isinstance(sentinel, BudgetErrorSentinel)
        assert bytes(sentinel) == b""
        assert unwrap_budget_error(sentinel) is real_err

    def test_oversized_escaped_key_in_splitter_rejected_before_json_loads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix1 #2: a 1,050,022-byte manifest with a 1,050,002-byte escaped key
        (or a >4 KiB escaped key) is rejected by key/byte budget before json.loads."""
        mock_loads = create_autospec(
            json.loads, side_effect=AssertionError("json.loads must not be called")
        )
        monkeypatch.setattr(budget_mod.json, "loads", mock_loads)

        # Exact reproduction from review: 1,050,022-byte manifest with a
        # 1,050,002-byte key containing a backslash escape and trailing mcpServers.
        huge_key_manifest = (
            b'{"\\u0061'
            + (b"a" * 1_049_994)
            + b'":1,"mcpServers":{}}'
        )
        assert len(huge_key_manifest) == 1_050_022
        manifest, manifest_err, inline_err = parse_manifest_with_budget(
            huge_key_manifest, manifest_path="plugin.json"
        )
        assert manifest is None
        assert inline_err is None
        unwrapped = unwrap_budget_error(manifest_err)
        assert unwrapped is not None
        assert unwrapped.limit_kind == "max_bytes"
        assert unwrapped.limit == MAX_JSON_DOCUMENT_BYTES
        mock_loads.assert_not_called()

        # Also test a manifest < 1 MiB whose escaped key exceeds MAX_JSON_KEY_BYTES (4 KiB).
        over_key_manifest = (
            b'{"\\u0061'
            + (b"k" * MAX_JSON_KEY_BYTES)
            + b'":1,"mcpServers":{}}'
        )
        assert len(over_key_manifest) < MAX_JSON_DOCUMENT_BYTES
        manifest2, manifest_err2, _ = parse_manifest_with_budget(
            over_key_manifest, manifest_path="plugin.json"
        )
        assert manifest2 is None
        unwrapped2 = unwrap_budget_error(manifest_err2)
        assert unwrapped2 is not None
        assert unwrapped2.limit_kind == "max_bytes"
        assert unwrapped2.limit == MAX_JSON_KEY_BYTES
        mock_loads.assert_not_called()

    def test_duplicate_inline_mcp_servers_last_key_wins(self) -> None:
        """Fix1 #3: when mcpServers appears multiple times, the last value wins."""
        manifest_raw = (
            b'{"name": "dup-mcp", "version": "1.0.0", '
            b'"mcpServers": {"old": {"command": "npx", "args": ["old-pkg"]}}, '
            b'"mcpServers": {"new": {"command": "npx", "args": ["new-pkg"]}}}'
        )
        package = validate_package(
            zip_package(
                {
                    "plugin.json": manifest_raw,
                    "skills/alpha/SKILL.md": SKILL_MD,
                }
            )
        )
        assert [p.key for p in package.entry_plans] == ["new"]
        assert package.entry_plans[0].config["args"] == ["new-pkg"]

    def test_duplicate_inline_mcp_servers_last_null_means_no_servers(self) -> None:
        """Fix1 #3: when the last mcpServers is null, earlier mcpServers do not resurrect."""
        manifest_raw = (
            b'{"name": "dup-null", "version": "1.0.0", '
            b'"mcpServers": {"old": {"command": "npx", "args": ["old-pkg"]}}, '
            b'"mcpServers": null}'
        )
        package = validate_package(
            zip_package(
                {
                    "plugin.json": manifest_raw,
                    "skills/alpha/SKILL.md": SKILL_MD,
                }
            )
        )
        assert package.mcp_document is None
        assert package.mcp_document_invalid is False
        assert package.entry_plans == []
        assert _skill(package, "alpha").skip_code is None

    @pytest.mark.parametrize("fixture_name", ["valid-full", "dialect-cursor"])
    def test_indent2_output_preserves_upstream_content_hash(
        self, fixture_name: str
    ) -> None:
        """Fix1 #4: indent=2 formatting keeps adapted file bytes and content_hash
        identical to upstream json.dumps(..., indent=2)."""
        root = FIXTURES / fixture_name
        files = {
            str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }
        dialect = (
            "cursor" if ".cursor-plugin/plugin.json" in files else "canonical"
        )
        adapted, _ = adapt_dialect(files, dialect)

        expected_files = dict(adapted)
        expected_files["plugin.json"] = json.dumps(
            json.loads(adapted["plugin.json"].decode("utf-8")), indent=2
        ).encode("utf-8")
        if "mcp.json" in adapted:
            expected_files["mcp.json"] = json.dumps(
                json.loads(adapted["mcp.json"].decode("utf-8")), indent=2
            ).encode("utf-8")

        assert adapted["plugin.json"] == expected_files["plugin.json"]
        if "mcp.json" in adapted:
            assert adapted["mcp.json"] == expected_files["mcp.json"]

        package = validate_package(zip_package(files))
        assert package.content_hash == _tree_hash(expected_files)


class TestThreePathsAndComponentIsolation:
    @pytest.mark.parametrize(
        "limit_expected",
        ["max_bytes", "max_depth", "max_nodes", "max_output_bytes"],
    )
    def test_canonical_mcp_budget_violation_drops_only_mcp_component(
        self, limit_expected: str
    ) -> None:
        raw = zip_package(
            {
                "plugin.json": b'{"name": "canonical-isolated", "version": "1.0.0"}',
                "mcp.json": _bad_mcp_doc_bytes(limit_expected),
                "skills/alpha/SKILL.md": SKILL_MD,
            }
        )
        package = validate_package(raw)
        assert package.name == "canonical-isolated"
        assert package.mcp_document is None
        assert package.mcp_document_invalid is True
        assert package.entry_plans == []
        assert _skill(package, "alpha").skip_code is None

        mcp_errors = [
            d for d in package.diagnostics if d.scope == "mcp" and d.level == "error"
        ]
        assert len(mcp_errors) == 1
        assert "mcp.json" in mcp_errors[0].message
        assert limit_expected in mcp_errors[0].message

    @pytest.mark.parametrize(
        ("vendor_mcp_path", "manifest_extra"),
        [
            (".mcp.json", {}),
            (".claude-plugin/.mcp.json", {}),
            ("config/servers.json", {"mcpServers": "./config/servers.json"}),
        ],
    )
    @pytest.mark.parametrize(
        "limit_expected",
        ["max_bytes", "max_depth", "max_nodes", "max_output_bytes"],
    )
    def test_vendor_mcp_budget_violation_drops_only_mcp_component_and_names_file(
        self,
        vendor_mcp_path: str,
        manifest_extra: dict[str, str],
        limit_expected: str,
    ) -> None:
        raw = zip_package(
            {
                ".claude-plugin/plugin.json": json.dumps(
                    {"name": "vendor-isolated", "version": "1.0.0", **manifest_extra}
                ).encode("utf-8"),
                vendor_mcp_path: _bad_mcp_doc_bytes(limit_expected),
                "skills/alpha/SKILL.md": SKILL_MD,
            }
        )
        package = validate_package(raw)
        assert package.name == "vendor-isolated"
        assert package.mcp_document is None
        assert package.mcp_document_invalid is True
        assert package.entry_plans == []
        assert _skill(package, "alpha").skip_code is None

        mcp_errors = [
            d for d in package.diagnostics if d.scope == "mcp" and d.level == "error"
        ]
        assert len(mcp_errors) == 1
        assert vendor_mcp_path in mcp_errors[0].message
        assert limit_expected in mcp_errors[0].message

    @pytest.mark.parametrize(
        "manifest_path",
        ["plugin.json", ".claude-plugin/plugin.json"],
    )
    @pytest.mark.parametrize(
        "limit_expected",
        ["max_bytes", "max_depth", "max_nodes", "max_output_bytes"],
    )
    def test_inline_mcp_budget_violation_drops_only_mcp_component_and_keeps_skills(
        self,
        manifest_path: str,
        limit_expected: str,
    ) -> None:
        manifest_bytes = (
            b'{"name": "inline-isolated", "version": "1.0.0", "mcpServers": '
            + _bad_inline_mcp_fragment(limit_expected)
            + b"}"
        )
        raw = zip_package(
            {
                manifest_path: manifest_bytes,
                "skills/alpha/SKILL.md": SKILL_MD,
            }
        )
        package = validate_package(raw)
        assert package.name == "inline-isolated"
        assert package.mcp_document is None
        assert package.mcp_document_invalid is True
        assert package.entry_plans == []
        assert _skill(package, "alpha").skip_code is None

        mcp_errors = [
            d for d in package.diagnostics if d.scope == "mcp" and d.level == "error"
        ]
        assert len(mcp_errors) == 1
        assert f"{manifest_path} (mcpServers)" in mcp_errors[0].message
        assert limit_expected in mcp_errors[0].message

    def test_manifest_budget_violation_outside_mcp_names_file_and_limit(self) -> None:
        bad_manifest = (
            b'{"name": "bad-manifest", "extra": ' + (b"[" * 100) + (b"]" * 100) + b"}"
        )
        with pytest.raises(PluginFatal) as exc_info:
            validate_package(
                zip_package(
                    {
                        ".claude-plugin/plugin.json": bad_manifest,
                        "skills/alpha/SKILL.md": SKILL_MD,
                    }
                )
            )
        assert ".claude-plugin/plugin.json" in str(exc_info.value)
        assert "max_depth" in str(exc_info.value)

    def test_normal_plugins_across_all_three_paths_remain_unaffected(self) -> None:
        server = {"command": "npx", "args": ["-y", "pkg", "--filter=[a,{b}]"]}
        for manifest_path, manifest_extra, extra_files in [
            (
                "plugin.json",
                {},
                {
                    "mcp.json": json.dumps({"mcpServers": {"srv": server}}).encode(
                        "utf-8"
                    )
                },
            ),
            (
                ".claude-plugin/plugin.json",
                {},
                {
                    ".mcp.json": json.dumps({"mcpServers": {"srv": server}}).encode(
                        "utf-8"
                    )
                },
            ),
            (
                ".claude-plugin/plugin.json",
                {"mcpServers": {"srv": server}},
                {},
            ),
        ]:
            raw = zip_package(
                {
                    manifest_path: json.dumps(
                        {"name": "normal-ok", "version": "1.0.0", **manifest_extra}
                    ).encode("utf-8"),
                    "skills/alpha/SKILL.md": SKILL_MD,
                    **extra_files,
                }
            )
            package = validate_package(raw)
            assert package.name == "normal-ok"
            assert not package.mcp_document_invalid
            assert len(package.entry_plans) == 1
            assert package.entry_plans[0].installable
            assert _skill(package, "alpha").skip_code is None
