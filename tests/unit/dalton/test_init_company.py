"""Company skeleton: schema-legal company.json and exchange inference.

The skill script is loaded the same way as
``scripts/dalton/init_company_workspace.py`` so these tests exercise the file
the agent actually runs.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from src.dalton.knowledge.validate import validate_schema

_REPO_ROOT = Path(__file__).resolve().parents[3]
_INIT_SCRIPT = (
    _REPO_ROOT
    / "plugins"
    / "dalton"
    / "skills"
    / "dalton-init-company"
    / "scripts"
    / "init_company.py"
)
_INIT_DATE = "2026-11-01"


def _load_init_company():
    spec = importlib.util.spec_from_file_location(
        "dalton_init_company_under_test",
        _INIT_SCRIPT,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {_INIT_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


init_company = _load_init_company()


def _render(ticker: str, entry: dict | None = None) -> dict:
    files = init_company.render(ticker, entry, init_date=_INIT_DATE)
    return json.loads(files["data/company.json"])


def _assert_schema(doc: dict) -> None:
    assert validate_schema("company", doc) == []


def test_us_without_exchange_writes_other_and_passes_schema() -> None:
    doc = _render("SPOT", {"name": "Spotify Technology S.A.", "tier": "watch"})
    assert "cik" not in doc
    assert doc["identifiers"]["cik"] == "0001639920"
    assert doc["exchange"] == "OTHER"
    assert "交易所待核" in doc["one_liner"]
    _assert_schema(doc)


def test_explicit_exchange_is_kept_without_pending_note() -> None:
    nyse = _render(
        "SPOT",
        {"name": "Spotify Technology S.A.", "exchange": "NYSE"},
    )
    assert nyse["exchange"] == "NYSE"
    assert "交易所待核" not in nyse["one_liner"]
    _assert_schema(nyse)

    other = _render(
        "SPOT",
        {"name": "Spotify Technology S.A.", "exchange": "OTHER"},
    )
    assert other["exchange"] == "OTHER"
    assert "交易所待核" not in other["one_liner"]
    _assert_schema(other)


@pytest.mark.parametrize(
    ("ticker", "exchange"),
    [
        ("0700.HK", "HKEX"),
        ("600519.SS", "SSE"),
        ("000001.SZ", "SZSE"),
    ],
)
def test_hk_and_cn_exchange_inference_stays(ticker: str, exchange: str) -> None:
    doc = _render(ticker, {"name": ticker})
    assert doc["exchange"] == exchange
    assert "交易所待核" not in doc["one_liner"]
    assert "cik" not in doc
    assert doc["identifiers"] == {}
    _assert_schema(doc)


def test_missing_cik_writes_empty_identifiers_object() -> None:
    doc = _render("RDDT", {"name": "Reddit, Inc."})
    assert "cik" not in doc
    assert doc["identifiers"] == {}
    assert doc["exchange"] == "OTHER"
    assert "交易所待核" in doc["one_liner"]
    _assert_schema(doc)


def test_entry_cik_is_only_identifiers_cik() -> None:
    doc = _render(
        "RDDT",
        {"name": "Reddit, Inc.", "cik": "1713445", "exchange": "NYSE"},
    )
    assert "cik" not in doc
    assert doc["identifiers"] == {"cik": "0001713445"}
    assert doc["exchange"] == "NYSE"
    _assert_schema(doc)


def test_pending_note_survives_one_liner_truncation() -> None:
    doc = _render("RDDT", {"name": "N" * 250})
    assert doc["one_liner"].endswith("（交易所待核）")
    assert len(doc["one_liner"]) <= 200
    _assert_schema(doc)


def _illegal_top_level_cik() -> dict:
    return {
        "schema_version": 1,
        "ticker": "RDDT",
        "exchange": "OTHER",
        "name": "Reddit, Inc.",
        "coverage_tier": "watch",
        "coverage_stage": "onboarding",
        "identifiers": {"cik": "0001713445"},
        "one_liner": "Reddit, Inc. (RDDT) — Watch coverage",
        "covered_since": _INIT_DATE,
        "cik": "0001713445",
    }


def test_validator_rejects_top_level_cik() -> None:
    with pytest.raises(init_company.CompanyDocError, match="cik"):
        init_company.validate_company_doc(_illegal_top_level_cik())


def test_bundled_schemas_match_knowledge_schemas_byte_for_byte() -> None:
    source = _REPO_ROOT / "src" / "dalton" / "knowledge" / "schemas"
    bundled = init_company.SCHEMAS_DIR
    names = sorted(path.name for path in bundled.glob("*.schema.json"))
    assert names == ["common.schema.json", "company.schema.json"]
    for name in names:
        assert (bundled / name).read_bytes() == (source / name).read_bytes()


def test_main_schema_import_failure_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "ws"
    monkeypatch.setattr(
        init_company,
        "_import_jsonschema",
        create_autospec(
            init_company._import_jsonschema,
            side_effect=init_company.CompanySchemaUnavailable(
                "jsonschema import failed"
            ),
        ),
    )
    code = init_company.main(
        [
            "--ticker",
            "SPOT",
            "--name",
            "Spotify Technology S.A.",
            "--coverage",
            str(_coverage_file(tmp_path)),
            "--root",
            str(root),
            "--apply",
        ]
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"] == "company_schema_unavailable"
    assert not (root / "data" / "company.json").exists()
    assert not (root / "agent.md").exists()


def test_main_missing_or_corrupt_schema_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "ws"
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    (schema_dir / "common.schema.json").write_text("{", encoding="utf-8")
    (schema_dir / "company.schema.json").write_text("{", encoding="utf-8")
    monkeypatch.setattr(init_company, "SCHEMAS_DIR", schema_dir)
    code = init_company.main(
        [
            "--ticker",
            "RDDT",
            "--name",
            "Reddit",
            "--coverage",
            str(_coverage_file(tmp_path)),
            "--root",
            str(root),
            "--apply",
        ]
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "company_schema_unavailable"
    assert not (root / "data" / "company.json").exists()

    monkeypatch.setattr(init_company, "SCHEMAS_DIR", tmp_path / "absent")
    code_missing = init_company.main(
        [
            "--ticker",
            "RDDT",
            "--name",
            "Reddit",
            "--coverage",
            str(_coverage_file(tmp_path)),
            "--root",
            str(root),
            "--apply",
        ]
    )
    missing_payload = json.loads(capsys.readouterr().out)
    assert code_missing == 1
    assert missing_payload["error"] == "company_schema_unavailable"
    assert not (root / "agent.md").exists()


def _coverage_file(tmp_path: Path) -> Path:
    path = tmp_path / "coverage.yaml"
    path.write_text(
        "as_of: '2026-11-01'\ncompanies: []\n",
        encoding="utf-8",
    )
    return path


def test_main_prints_pending_note_and_does_not_write(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "ws"
    code = init_company.main(
        [
            "--ticker",
            "RDDT",
            "--name",
            "Reddit",
            "--market",
            "US",
            "--coverage",
            str(_coverage_file(tmp_path)),
            "--root",
            str(root),
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["notes"] == ["交易所待核"]
    assert payload["dry_run"] is True
    assert not (root / "data" / "company.json").exists()
    assert not (root / "agent.md").exists()


def test_main_apply_writes_schema_valid_company_without_top_level_cik(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "ws"
    code = init_company.main(
        [
            "--ticker",
            "SPOT",
            "--name",
            "Spotify Technology S.A.",
            "--coverage",
            str(_coverage_file(tmp_path)),
            "--root",
            str(root),
            "--apply",
        ]
    )
    assert code == 0
    written = root / "data" / "company.json"
    doc = json.loads(written.read_text(encoding="utf-8"))
    assert "cik" not in doc
    assert doc["identifiers"]["cik"] == "0001639920"
    assert doc["exchange"] == "OTHER"
    assert validate_schema("company", doc) == []
    payload = json.loads(capsys.readouterr().out)
    assert payload["notes"] == ["交易所待核"]


def test_main_schema_error_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "ws"
    monkeypatch.setattr(
        init_company,
        "validate_company_doc",
        create_autospec(
            init_company.validate_company_doc,
            side_effect=init_company.CompanyDocError(
                "top-level keys not in schema: cik"
            ),
        ),
    )
    code = init_company.main(
        [
            "--ticker",
            "SPOT",
            "--name",
            "Spotify Technology S.A.",
            "--coverage",
            str(_coverage_file(tmp_path)),
            "--root",
            str(root),
            "--apply",
        ]
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"] == "company_schema_invalid"
    assert "cik" in payload["message"]
    assert not (root / "data" / "company.json").exists()
    assert not (root / "agent.md").exists()


@pytest.mark.parametrize("case", ["bad_utf8", "no_dollar_schema"])
def test_main_undecodable_or_unregistrable_schema_writes_nothing(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "ws"
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    bundled = Path(init_company.__file__).resolve().parents[1] / "references" / "schemas"
    for name in ("common", "company"):
        (schema_dir / f"{name}.schema.json").write_bytes((bundled / f"{name}.schema.json").read_bytes())
    target = schema_dir / "common.schema.json"
    if case == "bad_utf8":
        target.write_bytes(b'{"x": "\xff\xfe"}')
    else:
        doc = json.loads(target.read_text(encoding="utf-8"))
        doc.pop("$schema", None)
        target.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(init_company, "SCHEMAS_DIR", schema_dir)
    code = init_company.main(
        ["--ticker", "RDDT", "--name", "Reddit", "--coverage", str(_coverage_file(tmp_path)),
         "--root", str(root), "--apply"]
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "company_schema_unavailable"
    assert not (root / "data" / "company.json").exists()
