"""workspace_file_copy_cos：CoS 把同事共享工作区里的二进制文件拷到自己能发送的位置。

线上线程 346b50a9：用户要同事共享工作区里的最新 Excel，Chief of Staff 找到了文件，
但 read_file 只支持文本，.xlsx 报 "cannot read binary file as text"，最后只能让用户
自己去下载。这里的用例锁定：有读权限时镜像（stopped）与在线两种来源都能拷进
Home；无读权限被拒；超过 50 MB 如实告知；文本文件仍走 read_file 的原路径。
"""

from __future__ import annotations

import io
import json
import zipfile
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import create_autospec

import pytest

from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
import src.server.database.workspace as ws_db
from src.server.utils.secret_redactor import SecretRedactor
from src.tools.secretary import tools as secretary_tools
from src.tools.secretary.tools import (
    workspace_file_copy_cos,
    workspace_file_read_cos,
)

OWNER_SECRET = "sk-proj-owner-vault-secret-9999"


def _make_xlsx_bytes(cell_text: str = "Anthropic FY26 revenue model") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?><Types/>',
        )
        zf.writestr(
            "xl/sharedStrings.xml",
            f'<?xml version="1.0" encoding="UTF-8"?><sst><si><t>{cell_text}</t></si></sst>',
        )
    return buf.getvalue()


XLSX_BYTES = _make_xlsx_bytes()

COLLEAGUE_ROW = {
    "workspace_id": "ws-anthropic",
    "user_id": "u-cy",
    "name": "Anthropic",
    "dir_name": "Anthropic",
    "computer_id": "comp-cy",
    "status": "stopped",
    "computer_root_dir": "/root",
}

HOME_ROW = {
    "workspace_id": "ws-home",
    "user_id": "u-1",
    "name": "Home",
    "dir_name": "Home",
    "computer_id": "comp-1",
    "status": "running",
    "computer_root_dir": "/root",
}


def _payload(command: Any) -> dict[str, Any]:
    message = command.update["messages"][0]
    return json.loads(message.content)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    source_row: dict[str, Any],
    dest_row: dict[str, Any] = HOME_ROW,
    allowed: bool = True,
) -> PTCSandbox:
    """Patch the copy path's seams; returns the spec'd destination sandbox."""
    if allowed:
        verify = create_autospec(
            secretary_tools.verify_workspace_owner, return_value=None
        )
    else:
        verify = create_autospec(
            secretary_tools.verify_workspace_owner,
            return_value=secretary_tools.error_command(
                "workspace not found", "tc-1"
            ),
        )
    monkeypatch.setattr(secretary_tools, "verify_workspace_owner", verify)

    rows = {source_row["workspace_id"]: source_row, dest_row["workspace_id"]: dest_row}
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            side_effect=lambda ws_id: rows.get(str(ws_id)),
        ),
    )
    monkeypatch.setattr(
        ws_db,
        "get_live_workspace_folders_for_computer",
        create_autospec(
            ws_db.get_live_workspace_folders_for_computer, return_value=[]
        ),
    )

    # The default destination is the user's Home row above, so the id lookup
    # must answer it (the real helper derives a uuid5 id from the user id).
    import src.server.database.home_workspace as home_ws_db

    monkeypatch.setattr(
        home_ws_db,
        "get_home_workspace_id",
        create_autospec(
            home_ws_db.get_home_workspace_id,
            return_value=dest_row["workspace_id"],
        ),
    )

    sandbox = create_autospec(PTCSandbox, instance=True)
    sandbox.aupload_file_bytes.return_value = True
    sandbox.als_directory.return_value = []

    import src.server.services.workspace_files as workspace_files_service
    from src.server.app.workspace_files import crud as files_crud

    async def _contained_target(sandbox_: Any, path: str, work_dir: str, previous_dirs=()):
        return f"{work_dir.rstrip('/')}/{path.lstrip('/')}"

    monkeypatch.setattr(
        workspace_files_service,
        "contained_target",
        create_autospec(
            workspace_files_service.contained_target,
            side_effect=_contained_target,
        ),
    )

    async def _read_contained_target(sandbox_: Any, path: str, work_dir: str, previous_dirs=()):
        return f"{work_dir.rstrip('/')}/{path.lstrip('/')}", XLSX_BYTES

    monkeypatch.setattr(
        files_crud,
        "_read_contained_target",
        create_autospec(
            files_crud._read_contained_target,
            side_effect=_read_contained_target,
        ),
    )

    @asynccontextmanager
    async def _acquire(workspace_id: str, user_id: str):
        yield sandbox, rows.get(str(workspace_id)) or dest_row

    monkeypatch.setattr(
        workspace_files_service,
        "acquire_sandbox_to_change",
        create_autospec(
            workspace_files_service.acquire_sandbox_to_change,
            side_effect=_acquire,
        ),
    )

    from src.server.utils import secret_redactor

    monkeypatch.setattr(
        secret_redactor,
        "get_vault_secrets_for_redaction",
        create_autospec(
            secret_redactor.get_vault_secrets_for_redaction,
            return_value={"OPENAI_API_KEY": OWNER_SECRET},
        ),
    )
    return sandbox


def _install_mirror_source(
    monkeypatch: pytest.MonkeyPatch,
    *,
    file_size: int = len(XLSX_BYTES),
):
    from src.server.services.persistence import file as persistence_file
    from src.server.app.workspace_files import _shared

    record = {
        "path": "ant_sheet_update/Anthropic_sheet.xlsx",
        "is_binary": True,
        "file_size": file_size,
    }
    monkeypatch.setattr(
        persistence_file.FilePersistenceService,
        "get_file_content",
        staticmethod(
            create_autospec(
                persistence_file.FilePersistenceService.get_file_content,
                return_value=record,
            )
        ),
    )
    http_bytes = create_autospec(
        _shared.http_file_bytes, return_value=XLSX_BYTES
    )
    monkeypatch.setattr(_shared, "http_file_bytes", http_bytes)
    return http_bytes


@pytest.mark.asyncio
async def test_mirror_binary_copy_lands_in_home_shared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The incident's shape: a stopped colleague workspace's .xlsx copies into
    Home at shared/<filename>, the exact bytes intact, ready for send_message."""
    sandbox = _install(monkeypatch, source_row=COLLEAGUE_ROW)
    _install_mirror_source(monkeypatch)

    command = await workspace_file_copy_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path="ant_sheet_update/Anthropic_sheet.xlsx",
        dest_path=None,
        tool_call_id="tc-copy-1",
    )
    payload = _payload(command)
    assert payload["success"] is True, payload
    assert payload["workspace_id"] == "ws-home"
    assert payload["path"] == "shared/Anthropic_sheet.xlsx"
    assert payload["folder_path"] == "/root/Home"
    assert payload["size"] == len(XLSX_BYTES)
    assert payload["source"] == {
        "workspace_id": "ws-anthropic",
        "path": "ant_sheet_update/Anthropic_sheet.xlsx",
    }
    sandbox.aupload_file_bytes.assert_called_once_with(
        "/root/Home/shared/Anthropic_sheet.xlsx", XLSX_BYTES
    )


@pytest.mark.asyncio
async def test_live_sandbox_binary_copy_uses_read_contained_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A running colleague workspace copies through the sandbox read path with
    the same result path in Home."""
    live_row = {**COLLEAGUE_ROW, "status": "running"}
    sandbox = _install(monkeypatch, source_row=live_row)

    command = await workspace_file_copy_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path="cash_model/ANT_cash_model.xlsx",
        dest_path="anthropic/ANT_cash_model.xlsx",
        tool_call_id="tc-copy-2",
    )
    payload = _payload(command)
    assert payload["success"] is True, payload
    assert payload["path"] == "anthropic/ANT_cash_model.xlsx"
    sandbox.aupload_file_bytes.assert_called_once_with(
        "/root/Home/anthropic/ANT_cash_model.xlsx", XLSX_BYTES
    )


@pytest.mark.asyncio
async def test_no_read_access_is_refused_and_nothing_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """verify_workspace_owner's refusal stops the copy before any read or write."""
    sandbox = _install(monkeypatch, source_row=COLLEAGUE_ROW, allowed=False)

    command = await workspace_file_copy_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path="ant_sheet_update/Anthropic_sheet.xlsx",
        dest_path=None,
        tool_call_id="tc-copy-3",
    )
    payload = _payload(command)
    assert payload.get("success") is False
    assert "workspace not found" in payload.get("error", "")
    sandbox.aupload_file_bytes.assert_not_called()


@pytest.mark.asyncio
async def test_oversize_file_is_refused_with_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source past the 50 MB cap is refused with the real size, and the
    bytes are never fetched."""
    _install(monkeypatch, source_row=COLLEAGUE_ROW)

    from src.server.services.persistence import file as persistence_file
    from src.server.app.workspace_files import _shared

    record = {"path": "big.xlsx", "is_binary": True, "file_size": 60 * 1024 * 1024}
    monkeypatch.setattr(
        persistence_file.FilePersistenceService,
        "get_file_content",
        staticmethod(
            create_autospec(
                persistence_file.FilePersistenceService.get_file_content,
                return_value=record,
            )
        ),
    )
    http_bytes = create_autospec(_shared.http_file_bytes, return_value=b"x")
    monkeypatch.setattr(_shared, "http_file_bytes", http_bytes)

    command = await workspace_file_copy_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path="big.xlsx",
        dest_path=None,
        tool_call_id="tc-copy-4",
    )
    payload = _payload(command)
    assert payload.get("success") is False
    assert "50 MB" in payload.get("error", "")
    http_bytes.assert_not_called()


@pytest.mark.asyncio
async def test_mirror_text_file_copy_is_refused_nothing_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A text file is never byte-copied: the copy would bypass read_file's
    redaction for the source user. Refused with the read_file hint, and the
    bytes are not even fetched."""
    sandbox = _install(monkeypatch, source_row=COLLEAGUE_ROW)

    from src.server.services.persistence import file as persistence_file
    from src.server.app.workspace_files import _shared

    record = {
        "path": "logs/ANT_run.log",
        "is_binary": False,
        "file_size": 120,
    }
    monkeypatch.setattr(
        persistence_file.FilePersistenceService,
        "get_file_content",
        staticmethod(
            create_autospec(
                persistence_file.FilePersistenceService.get_file_content,
                return_value=record,
            )
        ),
    )
    http_bytes = create_autospec(_shared.http_file_bytes, return_value=b"log")
    monkeypatch.setattr(_shared, "http_file_bytes", http_bytes)

    command = await workspace_file_copy_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path="logs/ANT_run.log",
        dest_path=None,
        tool_call_id="tc-copy-6",
    )
    payload = _payload(command)
    assert payload.get("success") is False
    assert "read_file" in payload.get("error", "")
    assert "binary" in payload.get("error", "")
    http_bytes.assert_not_called()
    sandbox.aupload_file_bytes.assert_not_called()


@pytest.mark.asyncio
async def test_live_text_file_copy_is_refused_nothing_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same refusal on the live path: a .md source is refused by extension
    (the read_file criterion) before anything is written."""
    sandbox = _install(monkeypatch, source_row={**COLLEAGUE_ROW, "status": "running"})

    command = await workspace_file_copy_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path="cash_model/ANT_cash_memo.md",
        dest_path=None,
        tool_call_id="tc-copy-7",
    )
    payload = _payload(command)
    assert payload.get("success") is False
    assert "read_file" in payload.get("error", "")
    sandbox.aupload_file_bytes.assert_not_called()


@pytest.mark.asyncio
async def test_live_read_413_names_the_channel_limit_not_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whole-read 413 is a capacity fact, not a missing file: the error
    names the channel's read ceiling instead of "file not found"."""
    sandbox = _install(monkeypatch, source_row={**COLLEAGUE_ROW, "status": "running"})

    from fastapi import HTTPException
    from src.server.app.workspace_files import crud as files_crud

    async def _too_large(sandbox_: Any, path: str, work_dir: str, previous_dirs=()):
        raise HTTPException(status_code=413, detail="too large")

    files_crud._read_contained_target.side_effect = _too_large

    command = await workspace_file_copy_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path="cash_model/ANT_cash_model.xlsx",
        dest_path=None,
        tool_call_id="tc-copy-8",
    )
    payload = _payload(command)
    assert payload.get("success") is False
    error = payload.get("error", "")
    assert "file not found" not in error
    assert "read limit" in error
    assert "64" in error
    sandbox.aupload_file_bytes.assert_not_called()


@pytest.mark.asyncio
async def test_live_read_413_on_ssh_names_the_lower_channel_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An SSH machine keeps only 32 MiB of exec output, so its whole-read
    ceiling is below 64 MB; the refusal states the real one for that channel."""
    sandbox = _install(monkeypatch, source_row={**COLLEAGUE_ROW, "status": "running"})
    from ptc_agent.core.sandbox.runtime import SandboxRuntime

    runtime = create_autospec(SandboxRuntime, instance=True)
    runtime.exec_output_cap = 32 * 1024 * 1024
    sandbox.runtime = runtime

    from fastapi import HTTPException
    from src.server.app.workspace_files import crud as files_crud

    async def _too_large(sandbox_: Any, path: str, work_dir: str, previous_dirs=()):
        raise HTTPException(status_code=413, detail="too large")

    files_crud._read_contained_target.side_effect = _too_large

    command = await workspace_file_copy_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path="cash_model/ANT_cash_model.xlsx",
        dest_path=None,
        tool_call_id="tc-copy-9",
    )
    payload = _payload(command)
    error = payload.get("error", "")
    assert payload.get("success") is False
    assert "24" in error  # (32 MiB cap - headroom) after base64 ≈ 24 MB
    assert "64" not in error


@pytest.mark.asyncio
async def test_mirror_missing_file_size_rechecks_after_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mirror record without file_size is judged again by the bytes actually
    read, so a 60 MB payload cannot slip past the 50 MB copy limit."""
    sandbox = _install(monkeypatch, source_row=COLLEAGUE_ROW)
    http_bytes = _install_mirror_source(monkeypatch, file_size=0)
    big = b"x" * (60 * 1024 * 1024)
    http_bytes.return_value = big

    command = await workspace_file_copy_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path="ant_sheet_update/Anthropic_sheet.xlsx",
        dest_path=None,
        tool_call_id="tc-copy-10",
    )
    payload = _payload(command)
    assert payload.get("success") is False
    assert "50 MB" in payload.get("error", "")
    sandbox.aupload_file_bytes.assert_not_called()


@pytest.mark.asyncio
async def test_colleague_workspace_is_not_a_copy_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The copy lands only in the user's own workspaces: naming a colleague's
    workspace as the destination is refused, not written into."""
    sandbox = _install(monkeypatch, source_row={**COLLEAGUE_ROW, "status": "running"})

    command = await workspace_file_copy_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path="cash_model/ANT_cash_model.xlsx",
        dest_path="inbox/ANT_cash_model.xlsx",
        tool_call_id="tc-copy-5",
        dest_workspace_id="ws-anthropic",
    )
    payload = _payload(command)
    assert payload.get("success") is False
    assert "the user's own workspaces" in payload.get("error", "")
    sandbox.aupload_file_bytes.assert_not_called()


@pytest.mark.asyncio
async def test_text_file_still_reads_through_read_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """read_file's text path is unchanged: a text row still comes back as
    redacted content, with no copy involved."""
    _install(monkeypatch, source_row=COLLEAGUE_ROW)

    from src.server.services.persistence import file as persistence_file
    from src.server.app.workspace_files import _shared
    from src.server.utils import secret_redactor

    record = {
        "path": "cash_model/ANT_cash_memo.md",
        "is_binary": False,
        "file_size": 24,
    }
    monkeypatch.setattr(
        persistence_file.FilePersistenceService,
        "get_file_content",
        staticmethod(
            create_autospec(
                persistence_file.FilePersistenceService.get_file_content,
                return_value=record,
            )
        ),
    )
    monkeypatch.setattr(
        _shared,
        "http_file_text",
        create_autospec(
            _shared.http_file_text, return_value="# Cash memo\n\n正文\n"
        ),
    )
    redactor = create_autospec(SecretRedactor, instance=True)
    redactor.redact.side_effect = lambda text, vault_secrets=None: text
    monkeypatch.setattr(
        secret_redactor, "get_redactor", create_autospec(
            secret_redactor.get_redactor, return_value=redactor
        )
    )

    secret_redactor.get_vault_secrets_for_redaction.return_value = {}

    command = await workspace_file_read_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path="cash_model/ANT_cash_memo.md",
        tool_call_id="tc-read-1",
    )
    payload = _payload(command)
    assert payload["success"] is True, payload
    assert payload["path"] == "cash_model/ANT_cash_memo.md"
    assert "# Cash memo" in payload["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("source_status", ["stopped", "running"])
async def test_gbk_csv_is_refused_on_mirror_and_live_nothing_written(
    monkeypatch: pytest.MonkeyPatch,
    source_status: str,
) -> None:
    """A GBK-encoded .csv is marked is_binary=True on the mirror because it is
    not valid UTF-8, yet its extension is not in BINARY_EXTENSIONS: both mirror
    and live paths must refuse it and write no copy."""
    gbk_csv = f"账户,密钥\n主账户,{OWNER_SECRET}\n".encode("gbk")
    sandbox = _install(
        monkeypatch, source_row={**COLLEAGUE_ROW, "status": source_status}
    )

    from src.server.services.persistence import file as persistence_file
    from src.server.app.workspace_files import _shared, crud as files_crud

    record = {
        "path": "exports/holdings_gbk.csv",
        "is_binary": True,
        "file_size": len(gbk_csv),
    }
    monkeypatch.setattr(
        persistence_file.FilePersistenceService,
        "get_file_content",
        staticmethod(
            create_autospec(
                persistence_file.FilePersistenceService.get_file_content,
                return_value=record,
            )
        ),
    )
    http_bytes = create_autospec(_shared.http_file_bytes, return_value=gbk_csv)
    monkeypatch.setattr(_shared, "http_file_bytes", http_bytes)

    async def _read_gbk(sandbox_: Any, path: str, work_dir: str, previous_dirs=()):
        return f"{work_dir.rstrip('/')}/{path.lstrip('/')}", gbk_csv

    files_crud._read_contained_target.side_effect = _read_gbk

    cmd = await workspace_file_copy_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path="exports/holdings_gbk.csv",
        dest_path=None,
        tool_call_id=f"tc-copy-gbk-{source_status}",
    )
    payload = _payload(cmd)
    assert payload.get("success") is False
    assert "read_file" in payload.get("error", "")
    http_bytes.assert_not_called()
    sandbox.aupload_file_bytes.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_status", ["stopped", "running"])
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le", "utf-16-be"])
async def test_pdf_with_owner_vault_secret_is_refused_on_mirror_and_live(
    monkeypatch: pytest.MonkeyPatch,
    source_status: str,
    encoding: str,
) -> None:
    """A .pdf carrying the source owner's vault secret in raw bytes (UTF-8,
    UTF-16-LE, or UTF-16-BE) is refused on both mirror and live paths without
    echoing the secret."""
    pdf_bytes = b"%PDF-1.4\n" + OWNER_SECRET.encode(encoding) + b"\n%%EOF"
    sandbox = _install(
        monkeypatch, source_row={**COLLEAGUE_ROW, "status": source_status}
    )
    from src.server.services.persistence import file as persistence_file
    from src.server.app.workspace_files import _shared, crud as files_crud

    record = {
        "path": "memo/secret_deck.pdf",
        "is_binary": True,
        "file_size": len(pdf_bytes),
    }
    monkeypatch.setattr(
        persistence_file.FilePersistenceService,
        "get_file_content",
        staticmethod(
            create_autospec(
                persistence_file.FilePersistenceService.get_file_content,
                return_value=record,
            )
        ),
    )
    monkeypatch.setattr(
        _shared,
        "http_file_bytes",
        create_autospec(_shared.http_file_bytes, return_value=pdf_bytes),
    )

    async def _read_pdf(sandbox_: Any, path: str, work_dir: str, previous_dirs=()):
        return f"{work_dir.rstrip('/')}/{path.lstrip('/')}", pdf_bytes

    files_crud._read_contained_target.side_effect = _read_pdf

    cmd = await workspace_file_copy_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path="memo/secret_deck.pdf",
        dest_path=None,
        tool_call_id=f"tc-pdf-{source_status}-{encoding}",
    )
    payload = _payload(cmd)
    assert payload.get("success") is False
    assert "凭据" in payload.get("error", "")
    assert OWNER_SECRET not in payload.get("error", "")
    sandbox.aupload_file_bytes.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_status", ["stopped", "running"])
async def test_xlsx_with_owner_vault_secret_in_zip_member_is_refused_on_mirror_and_live(
    monkeypatch: pytest.MonkeyPatch,
    source_status: str,
) -> None:
    """An .xlsx whose compressed XML member contains the source owner's vault
    secret (so the raw zip bytes do not contain the literal secret substring)
    is decompressed, caught, and refused on both mirror and live paths."""
    secret_xlsx = _make_xlsx_bytes(
        cell_text=f"token={OWNER_SECRET} (" + "padding " * 20 + ")"
    )
    assert OWNER_SECRET.encode("utf-8") not in secret_xlsx

    sandbox = _install(
        monkeypatch, source_row={**COLLEAGUE_ROW, "status": source_status}
    )
    from src.server.services.persistence import file as persistence_file
    from src.server.app.workspace_files import _shared, crud as files_crud

    record = {
        "path": "ant_sheet_update/Anthropic_sheet.xlsx",
        "is_binary": True,
        "file_size": len(secret_xlsx),
    }
    monkeypatch.setattr(
        persistence_file.FilePersistenceService,
        "get_file_content",
        staticmethod(
            create_autospec(
                persistence_file.FilePersistenceService.get_file_content,
                return_value=record,
            )
        ),
    )
    monkeypatch.setattr(
        _shared,
        "http_file_bytes",
        create_autospec(_shared.http_file_bytes, return_value=secret_xlsx),
    )

    async def _read_secret_xlsx(sandbox_: Any, path: str, work_dir: str, previous_dirs=()):
        return f"{work_dir.rstrip('/')}/{path.lstrip('/')}", secret_xlsx

    files_crud._read_contained_target.side_effect = _read_secret_xlsx

    cmd = await workspace_file_copy_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path="ant_sheet_update/Anthropic_sheet.xlsx",
        dest_path=None,
        tool_call_id=f"tc-xlsx-secret-{source_status}",
    )
    payload = _payload(cmd)
    assert payload.get("success") is False
    assert "凭据" in payload.get("error", "")
    assert OWNER_SECRET not in payload.get("error", "")
    sandbox.aupload_file_bytes.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_status", ["stopped", "running"])
@pytest.mark.parametrize(
    "bad_path",
    [
        "exports/secret.csv.gz",
        "exports/bundle.zip",
        "models/weights.pkl",
    ],
)
async def test_disallowed_archive_and_pickle_extensions_are_refused_on_mirror_and_live(
    monkeypatch: pytest.MonkeyPatch,
    source_status: str,
    bad_path: str,
) -> None:
    """Round 4 item 1: .gz, .zip, and .pkl are outside the narrowed copy
    allowlist and are refused on both mirror and live paths without reading or
    writing bytes."""
    sandbox = _install(
        monkeypatch, source_row={**COLLEAGUE_ROW, "status": source_status}
    )
    from src.server.services.persistence import file as persistence_file
    from src.server.app.workspace_files import _shared

    record = {"path": bad_path, "is_binary": True, "file_size": 256}
    monkeypatch.setattr(
        persistence_file.FilePersistenceService,
        "get_file_content",
        staticmethod(
            create_autospec(
                persistence_file.FilePersistenceService.get_file_content,
                return_value=record,
            )
        ),
    )
    http_bytes = create_autospec(_shared.http_file_bytes, return_value=b"raw")
    monkeypatch.setattr(_shared, "http_file_bytes", http_bytes)

    cmd = await workspace_file_copy_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path=bad_path,
        dest_path=None,
        tool_call_id=f"tc-bad-ext-{source_status}-{bad_path}",
    )
    payload = _payload(cmd)
    assert payload.get("success") is False
    error = payload.get("error", "")
    assert "Office" in error
    assert "PDF" in error
    assert "image" in error
    assert "read_file" in error
    http_bytes.assert_not_called()
    sandbox.aupload_file_bytes.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_status", ["stopped", "running"])
async def test_xlsx_with_xml_escaped_secret_is_refused_on_mirror_and_live(
    monkeypatch: pytest.MonkeyPatch,
    source_status: str,
) -> None:
    """Round 4 item 2: a secret containing '&' ('R&D-password-12345') serialized
    in xlsx XML as 'R&amp;D-password-12345' is caught after html.unescape and
    refused on both mirror and live paths."""
    raw_secret = "R&D-password-12345"
    escaped_xlsx = _make_xlsx_bytes(
        cell_text="prefix R&amp;D-password-12345 suffix " + ("pad " * 15)
    )
    assert raw_secret.encode("utf-8") not in escaped_xlsx

    sandbox = _install(
        monkeypatch, source_row={**COLLEAGUE_ROW, "status": source_status}
    )
    from src.server.services.persistence import file as persistence_file
    from src.server.app.workspace_files import _shared, crud as files_crud
    from src.server.utils import secret_redactor

    secret_redactor.get_vault_secrets_for_redaction.return_value = {
        "RD_PASS": raw_secret
    }
    record = {
        "path": "ant_sheet_update/Anthropic_sheet.xlsx",
        "is_binary": True,
        "file_size": len(escaped_xlsx),
    }
    monkeypatch.setattr(
        persistence_file.FilePersistenceService,
        "get_file_content",
        staticmethod(
            create_autospec(
                persistence_file.FilePersistenceService.get_file_content,
                return_value=record,
            )
        ),
    )
    monkeypatch.setattr(
        _shared,
        "http_file_bytes",
        create_autospec(_shared.http_file_bytes, return_value=escaped_xlsx),
    )

    async def _read_escaped_xlsx(sandbox_: Any, path: str, work_dir: str, previous_dirs=()):
        return f"{work_dir.rstrip('/')}/{path.lstrip('/')}", escaped_xlsx

    files_crud._read_contained_target.side_effect = _read_escaped_xlsx

    cmd = await workspace_file_copy_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path="ant_sheet_update/Anthropic_sheet.xlsx",
        dest_path=None,
        tool_call_id=f"tc-xlsx-amp-{source_status}",
    )
    payload = _payload(cmd)
    assert payload.get("success") is False
    assert "凭据" in payload.get("error", "")
    assert raw_secret not in payload.get("error", "")
    sandbox.aupload_file_bytes.assert_not_called()


def _build_valid_pdf(
    stream_payload: bytes, filter_spec: str = "/FlateDecode"
) -> bytes:
    objs = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>\nendobj\n",
        f"4 0 obj\n<< /Length {len(stream_payload)} /Filter {filter_spec} >>\nstream\n".encode(
            "ascii"
        )
        + stream_payload
        + b"\nendstream\nendobj\n",
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for obj in objs:
        offsets.append(len(out))
        out.extend(obj)
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for off in offsets:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return bytes(out)


def _make_flate_pdf_bytes(stream_text: str) -> bytes:
    import zlib

    compressed = zlib.compress(
        f"BT /F1 12 Tf 72 712 Td ({stream_text}) Tj ET".encode("utf-8")
    )
    return _build_valid_pdf(compressed, "/FlateDecode")


@pytest.mark.asyncio
@pytest.mark.parametrize("source_status", ["stopped", "running"])
async def test_flate_compressed_pdf_with_secret_is_refused_and_clean_pdf_copies(
    monkeypatch: pytest.MonkeyPatch,
    source_status: str,
) -> None:
    """Round 4 item 2: a PDF whose FlateDecode stream contains a vault secret is
    decompressed and refused; a clean FlateDecode PDF copies normally."""
    secret_pdf = _make_flate_pdf_bytes(
        f"Confidential key: {OWNER_SECRET} (" + "padding " * 15 + ")"
    )
    clean_pdf = _make_flate_pdf_bytes("Quarterly revenue overview and margin notes")
    assert OWNER_SECRET.encode("utf-8") not in secret_pdf

    sandbox = _install(
        monkeypatch, source_row={**COLLEAGUE_ROW, "status": source_status}
    )
    from src.server.services.persistence import file as persistence_file
    from src.server.app.workspace_files import _shared, crud as files_crud

    current_bytes = {"data": secret_pdf}

    async def _get_record(workspace_id: str, path: str):
        return {
            "path": path,
            "is_binary": True,
            "file_size": len(current_bytes["data"]),
        }

    monkeypatch.setattr(
        persistence_file.FilePersistenceService,
        "get_file_content",
        staticmethod(
            create_autospec(
                persistence_file.FilePersistenceService.get_file_content,
                side_effect=_get_record,
            )
        ),
    )

    async def _http_bytes(file_record: dict[str, Any], *, user_id: str | None = None):
        return current_bytes["data"]

    monkeypatch.setattr(
        _shared,
        "http_file_bytes",
        create_autospec(_shared.http_file_bytes, side_effect=_http_bytes),
    )

    async def _read_pdf(sandbox_: Any, path: str, work_dir: str, previous_dirs=()):
        return f"{work_dir.rstrip('/')}/{path.lstrip('/')}", current_bytes["data"]

    files_crud._read_contained_target.side_effect = _read_pdf

    cmd_bad = await workspace_file_copy_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path="memo/flate_secret.pdf",
        dest_path=None,
        tool_call_id=f"tc-flate-bad-{source_status}",
    )
    payload_bad = _payload(cmd_bad)
    assert payload_bad.get("success") is False
    assert "凭据" in payload_bad.get("error", "")
    assert OWNER_SECRET not in payload_bad.get("error", "")
    sandbox.aupload_file_bytes.assert_not_called()

    current_bytes["data"] = clean_pdf
    cmd_ok = await workspace_file_copy_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path="memo/flate_clean.pdf",
        dest_path=None,
        tool_call_id=f"tc-flate-ok-{source_status}",
    )
    payload_ok = _payload(cmd_ok)
    assert payload_ok.get("success") is True, payload_ok
    sandbox.aupload_file_bytes.assert_called_once_with(
        "/root/Home/shared/flate_clean.pdf", clean_pdf
    )


@pytest.mark.asyncio
async def test_short_vault_values_under_8_chars_do_not_false_positive_on_pdf_png_xlsx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 item 2: vault values of 1-7 characters ('a', 'PDF', '1234567')
    are below MIN_REDACT_SECRET_LEN (8) and do not cause normal PDF, PNG, or
    xlsx files containing those substrings to be rejected."""
    sandbox = _install(monkeypatch, source_row=COLLEAGUE_ROW)
    from src.server.services.persistence import file as persistence_file
    from src.server.app.workspace_files import _shared
    from src.server.utils import secret_redactor

    secret_redactor.get_vault_secrets_for_redaction.return_value = {
        "ONE_CHAR": "a",
        "THREE_CHAR": "PDF",
        "SEVEN_CHAR": "1234567",
        "LONG_SECRET": OWNER_SECRET,
    }

    samples = {
        "memo/report.pdf": _make_flate_pdf_bytes("Normal PDF 1234567 content"),
        "charts/figure.png": b"\x89PNG\r\n\x1a\n" + b"a" * 32 + b"1234567",
        "models/sheet.xlsx": _make_xlsx_bytes("Row 1234567 value a"),
    }

    async def _get_record(workspace_id: str, path: str):
        data = samples[path]
        return {"path": path, "is_binary": True, "file_size": len(data)}

    monkeypatch.setattr(
        persistence_file.FilePersistenceService,
        "get_file_content",
        staticmethod(
            create_autospec(
                persistence_file.FilePersistenceService.get_file_content,
                side_effect=_get_record,
            )
        ),
    )

    async def _http_bytes(file_record: dict[str, Any], *, user_id: str | None = None):
        return samples[file_record["path"]]

    monkeypatch.setattr(
        _shared,
        "http_file_bytes",
        create_autospec(_shared.http_file_bytes, side_effect=_http_bytes),
    )

    for rel_path in samples:
        cmd = await workspace_file_copy_cos(
            user_id="u-1",
            workspace_id="ws-anthropic",
            path=rel_path,
            dest_path=None,
            tool_call_id=f"tc-short-{rel_path}",
        )
        payload = _payload(cmd)
        assert payload.get("success") is True, payload

    assert sandbox.aupload_file_bytes.call_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("source_status", ["stopped", "running"])
async def test_docx_with_nested_zip_member_is_refused_on_mirror_and_live(
    monkeypatch: pytest.MonkeyPatch,
    source_status: str,
) -> None:
    """Round 4 item 2: a .docx containing an embedded .zip archive inside its
    zip members is refused on both mirror and live paths."""
    inner_buf = io.BytesIO()
    with zipfile.ZipFile(inner_buf, "w", compression=zipfile.ZIP_DEFLATED) as inner_zf:
        inner_zf.writestr("secret.txt", f"key={OWNER_SECRET}")
    outer_buf = io.BytesIO()
    with zipfile.ZipFile(outer_buf, "w", compression=zipfile.ZIP_DEFLATED) as outer_zf:
        outer_zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?><Types/>',
        )
        outer_zf.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?><w:document/>',
        )
        outer_zf.writestr("word/embeddings/payload.zip", inner_buf.getvalue())
    docx_bytes = outer_buf.getvalue()

    sandbox = _install(
        monkeypatch, source_row={**COLLEAGUE_ROW, "status": source_status}
    )
    from src.server.services.persistence import file as persistence_file
    from src.server.app.workspace_files import _shared, crud as files_crud

    record = {
        "path": "memo/briefing.docx",
        "is_binary": True,
        "file_size": len(docx_bytes),
    }
    monkeypatch.setattr(
        persistence_file.FilePersistenceService,
        "get_file_content",
        staticmethod(
            create_autospec(
                persistence_file.FilePersistenceService.get_file_content,
                return_value=record,
            )
        ),
    )
    monkeypatch.setattr(
        _shared,
        "http_file_bytes",
        create_autospec(_shared.http_file_bytes, return_value=docx_bytes),
    )

    async def _read_docx(sandbox_: Any, path: str, work_dir: str, previous_dirs=()):
        return f"{work_dir.rstrip('/')}/{path.lstrip('/')}", docx_bytes

    files_crud._read_contained_target.side_effect = _read_docx

    cmd = await workspace_file_copy_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path="memo/briefing.docx",
        dest_path=None,
        tool_call_id=f"tc-docx-nested-{source_status}",
    )
    payload = _payload(cmd)
    assert payload.get("success") is False
    assert "压缩包" in payload.get("error", "")
    sandbox.aupload_file_bytes.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_status", ["stopped", "running"])
async def test_gzip_with_secret_renamed_to_png_is_refused_on_mirror_and_live(
    monkeypatch: pytest.MonkeyPatch,
    source_status: str,
) -> None:
    """Round 5 item 1: a gzip archive containing a vault secret renamed to
    'a.png' is rejected by header verification on both mirror and live paths."""
    import gzip

    disguised_gz = gzip.compress(f"token={OWNER_SECRET}\n".encode("utf-8"))
    sandbox = _install(
        monkeypatch, source_row={**COLLEAGUE_ROW, "status": source_status}
    )
    from src.server.services.persistence import file as persistence_file
    from src.server.app.workspace_files import _shared, crud as files_crud

    record = {
        "path": "charts/a.png",
        "is_binary": True,
        "file_size": len(disguised_gz),
    }
    monkeypatch.setattr(
        persistence_file.FilePersistenceService,
        "get_file_content",
        staticmethod(
            create_autospec(
                persistence_file.FilePersistenceService.get_file_content,
                return_value=record,
            )
        ),
    )
    monkeypatch.setattr(
        _shared,
        "http_file_bytes",
        create_autospec(_shared.http_file_bytes, return_value=disguised_gz),
    )

    async def _read_disguised(sandbox_: Any, path: str, work_dir: str, previous_dirs=()):
        return f"{work_dir.rstrip('/')}/{path.lstrip('/')}", disguised_gz

    files_crud._read_contained_target.side_effect = _read_disguised

    cmd = await workspace_file_copy_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path="charts/a.png",
        dest_path=None,
        tool_call_id=f"tc-gz-png-{source_status}",
    )
    payload = _payload(cmd)
    assert payload.get("success") is False
    assert "文件内容和扩展名不一致" in payload.get("error", "")
    sandbox.aupload_file_bytes.assert_not_called()


@pytest.mark.asyncio
async def test_normal_png_jpeg_xlsx_and_pdf_copy_successfully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 5 item 3: normal PNG, JPEG, xlsx, and PDF files with matching
    headers and no secrets copy cleanly."""
    sandbox = _install(monkeypatch, source_row=COLLEAGUE_ROW)
    from src.server.services.persistence import file as persistence_file
    from src.server.app.workspace_files import _shared

    samples = {
        "charts/plot.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
        "charts/photo.jpg": b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01",
        "models/model.xlsx": _make_xlsx_bytes("Clean financial projection"),
        "memo/summary.pdf": _make_flate_pdf_bytes("Clean executive summary"),
    }

    async def _get_record(workspace_id: str, path: str):
        data = samples[path]
        return {"path": path, "is_binary": True, "file_size": len(data)}

    monkeypatch.setattr(
        persistence_file.FilePersistenceService,
        "get_file_content",
        staticmethod(
            create_autospec(
                persistence_file.FilePersistenceService.get_file_content,
                side_effect=_get_record,
            )
        ),
    )

    async def _http_bytes(file_record: dict[str, Any], *, user_id: str | None = None):
        return samples[file_record["path"]]

    monkeypatch.setattr(
        _shared,
        "http_file_bytes",
        create_autospec(_shared.http_file_bytes, side_effect=_http_bytes),
    )

    for rel_path in samples:
        cmd = await workspace_file_copy_cos(
            user_id="u-1",
            workspace_id="ws-anthropic",
            path=rel_path,
            dest_path=None,
            tool_call_id=f"tc-valid-{rel_path}",
        )
        payload = _payload(cmd)
        assert payload.get("success") is True, payload

    assert sandbox.aupload_file_bytes.call_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("source_status", ["stopped", "running"])
async def test_pdf_with_ascii85_flate_filter_or_escaped_parens_secret_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    source_status: str,
) -> None:
    """Round 5 item 2: (1) a PDF using [/ASCII85Decode /FlateDecode] filter chain
    and (2) a FlateDecode PDF containing 'R&D-\\(password\\)-12345' (for secret
    'R&D-(password)-12345') are both decoded via pypdf and refused."""
    import base64
    import zlib

    parens_secret = "R&D-(password)-12345"
    a85_flate_payload = base64.a85encode(
        zlib.compress(
            f"BT /F1 12 Tf 72 712 Td (Secret: {OWNER_SECRET}) Tj ET".encode(
                "utf-8"
            )
        ),
        adobe=True,
    )
    pdf_a85 = _build_valid_pdf(
        a85_flate_payload, "[/ASCII85Decode /FlateDecode]"
    )
    assert OWNER_SECRET.encode("utf-8") not in pdf_a85

    escaped_parens_payload = zlib.compress(
        b"BT /F1 12 Tf 72 712 Td (R&D-\\(password\\)-12345) Tj ET"
    )
    pdf_parens = _build_valid_pdf(escaped_parens_payload, "/FlateDecode")
    assert parens_secret.encode("utf-8") not in pdf_parens

    sandbox = _install(
        monkeypatch, source_row={**COLLEAGUE_ROW, "status": source_status}
    )
    from src.server.services.persistence import file as persistence_file
    from src.server.app.workspace_files import _shared, crud as files_crud
    from src.server.utils import secret_redactor

    secret_redactor.get_vault_secrets_for_redaction.return_value = {
        "OPENAI_API_KEY": OWNER_SECRET,
        "PARENS_SECRET": parens_secret,
    }

    current_bytes = {"data": pdf_a85}

    async def _get_record(workspace_id: str, path: str):
        return {
            "path": path,
            "is_binary": True,
            "file_size": len(current_bytes["data"]),
        }

    monkeypatch.setattr(
        persistence_file.FilePersistenceService,
        "get_file_content",
        staticmethod(
            create_autospec(
                persistence_file.FilePersistenceService.get_file_content,
                side_effect=_get_record,
            )
        ),
    )

    async def _http_bytes(file_record: dict[str, Any], *, user_id: str | None = None):
        return current_bytes["data"]

    monkeypatch.setattr(
        _shared,
        "http_file_bytes",
        create_autospec(_shared.http_file_bytes, side_effect=_http_bytes),
    )

    async def _read_pdf(sandbox_: Any, path: str, work_dir: str, previous_dirs=()):
        return f"{work_dir.rstrip('/')}/{path.lstrip('/')}", current_bytes["data"]

    files_crud._read_contained_target.side_effect = _read_pdf

    for label, blob in (("a85", pdf_a85), ("parens", pdf_parens)):
        current_bytes["data"] = blob
        cmd = await workspace_file_copy_cos(
            user_id="u-1",
            workspace_id="ws-anthropic",
            path=f"memo/{label}.pdf",
            dest_path=None,
            tool_call_id=f"tc-pdf-{label}-{source_status}",
        )
        payload = _payload(cmd)
        assert payload.get("success") is False
        assert "凭据" in payload.get("error", "")
        assert OWNER_SECRET not in payload.get("error", "")
        assert parens_secret not in payload.get("error", "")

    sandbox.aupload_file_bytes.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_status", ["stopped", "running"])
async def test_corrupt_pdf_is_refused_with_unreadable_error(
    monkeypatch: pytest.MonkeyPatch,
    source_status: str,
) -> None:
    """Round 5 item 2 & 3: a corrupt PDF that starts with %PDF- but cannot be
    parsed by pypdf is refused with '无法检查这个 PDF 的内容，不能拷贝'."""
    corrupt_pdf = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Broken\n%%EOF"
    sandbox = _install(
        monkeypatch, source_row={**COLLEAGUE_ROW, "status": source_status}
    )
    from src.server.services.persistence import file as persistence_file
    from src.server.app.workspace_files import _shared, crud as files_crud

    record = {
        "path": "memo/corrupt.pdf",
        "is_binary": True,
        "file_size": len(corrupt_pdf),
    }
    monkeypatch.setattr(
        persistence_file.FilePersistenceService,
        "get_file_content",
        staticmethod(
            create_autospec(
                persistence_file.FilePersistenceService.get_file_content,
                return_value=record,
            )
        ),
    )
    monkeypatch.setattr(
        _shared,
        "http_file_bytes",
        create_autospec(_shared.http_file_bytes, return_value=corrupt_pdf),
    )

    async def _read_corrupt(sandbox_: Any, path: str, work_dir: str, previous_dirs=()):
        return f"{work_dir.rstrip('/')}/{path.lstrip('/')}", corrupt_pdf

    files_crud._read_contained_target.side_effect = _read_corrupt

    cmd = await workspace_file_copy_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path="memo/corrupt.pdf",
        dest_path=None,
        tool_call_id=f"tc-corrupt-pdf-{source_status}",
    )
    payload = _payload(cmd)
    assert payload.get("success") is False
    assert "无法检查这个 PDF 的内容，不能拷贝" in payload.get("error", "")
    sandbox.aupload_file_bytes.assert_not_called()


def _make_ooxml_with_embedded_xlsx(
    outer_xml_path: str,
    embedding_path: str,
    embedded_xlsx_bytes: bytes,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?><Types/>',
        )
        zf.writestr(
            outer_xml_path,
            '<?xml version="1.0" encoding="UTF-8"?><root/>',
        )
        zf.writestr(embedding_path, embedded_xlsx_bytes)
    return buf.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_status", ["stopped", "running"])
async def test_pptx_and_docx_with_clean_embedded_xlsx_chart_copy_successfully(
    monkeypatch: pytest.MonkeyPatch,
    source_status: str,
) -> None:
    """Round 6 item 1: pptx and docx containing an embedded chart .xlsx under
    ppt/embeddings/ or word/embeddings/ with no secrets are recursively scanned
    and copied normally."""
    clean_xlsx = _make_xlsx_bytes("Chart series Q1-Q4 revenue")
    pptx_bytes = _make_ooxml_with_embedded_xlsx(
        "ppt/presentation.xml",
        "ppt/embeddings/Microsoft_Excel_Worksheet.xlsx",
        clean_xlsx,
    )
    docx_bytes = _make_ooxml_with_embedded_xlsx(
        "word/document.xml",
        "word/embeddings/Microsoft_Excel_Worksheet.xlsx",
        clean_xlsx,
    )

    sandbox = _install(
        monkeypatch, source_row={**COLLEAGUE_ROW, "status": source_status}
    )
    from src.server.services.persistence import file as persistence_file
    from src.server.app.workspace_files import _shared, crud as files_crud

    samples = {
        "decks/q4_review.pptx": pptx_bytes,
        "memo/q4_memo.docx": docx_bytes,
    }

    async def _get_record(workspace_id: str, path: str):
        data = samples[path]
        return {"path": path, "is_binary": True, "file_size": len(data)}

    monkeypatch.setattr(
        persistence_file.FilePersistenceService,
        "get_file_content",
        staticmethod(
            create_autospec(
                persistence_file.FilePersistenceService.get_file_content,
                side_effect=_get_record,
            )
        ),
    )

    async def _http_bytes(file_record: dict[str, Any], *, user_id: str | None = None):
        return samples[file_record["path"]]

    monkeypatch.setattr(
        _shared,
        "http_file_bytes",
        create_autospec(_shared.http_file_bytes, side_effect=_http_bytes),
    )

    async def _read_sample(sandbox_: Any, path: str, work_dir: str, previous_dirs=()):
        return f"{work_dir.rstrip('/')}/{path.lstrip('/')}", samples[path]

    files_crud._read_contained_target.side_effect = _read_sample

    for rel_path in samples:
        cmd = await workspace_file_copy_cos(
            user_id="u-1",
            workspace_id="ws-anthropic",
            path=rel_path,
            dest_path=None,
            tool_call_id=f"tc-embed-clean-{source_status}-{rel_path}",
        )
        payload = _payload(cmd)
        assert payload.get("success") is True, payload

    assert sandbox.aupload_file_bytes.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("source_status", ["stopped", "running"])
async def test_pptx_or_docx_with_secret_in_embedded_xlsx_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    source_status: str,
) -> None:
    """Round 6 item 1: when an embedded chart .xlsx inside a pptx/docx contains
    a vault secret, recursive OOXML scanning catches it and refuses the copy."""
    secret_xlsx = _make_xlsx_bytes(
        cell_text=f"chart_key={OWNER_SECRET} (" + "padding " * 20 + ")"
    )
    pptx_bytes = _make_ooxml_with_embedded_xlsx(
        "ppt/presentation.xml",
        "ppt/embeddings/Microsoft_Excel_Worksheet.xlsx",
        secret_xlsx,
    )
    assert OWNER_SECRET.encode("utf-8") not in pptx_bytes

    sandbox = _install(
        monkeypatch, source_row={**COLLEAGUE_ROW, "status": source_status}
    )
    from src.server.services.persistence import file as persistence_file
    from src.server.app.workspace_files import _shared, crud as files_crud

    record = {
        "path": "decks/secret_chart.pptx",
        "is_binary": True,
        "file_size": len(pptx_bytes),
    }
    monkeypatch.setattr(
        persistence_file.FilePersistenceService,
        "get_file_content",
        staticmethod(
            create_autospec(
                persistence_file.FilePersistenceService.get_file_content,
                return_value=record,
            )
        ),
    )
    monkeypatch.setattr(
        _shared,
        "http_file_bytes",
        create_autospec(_shared.http_file_bytes, return_value=pptx_bytes),
    )

    async def _read_pptx(sandbox_: Any, path: str, work_dir: str, previous_dirs=()):
        return f"{work_dir.rstrip('/')}/{path.lstrip('/')}", pptx_bytes

    files_crud._read_contained_target.side_effect = _read_pptx

    cmd = await workspace_file_copy_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path="decks/secret_chart.pptx",
        dest_path=None,
        tool_call_id=f"tc-embed-secret-{source_status}",
    )
    payload = _payload(cmd)
    assert payload.get("success") is False
    assert "凭据" in payload.get("error", "")
    assert OWNER_SECRET not in payload.get("error", "")
    sandbox.aupload_file_bytes.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_status", ["stopped", "running"])
async def test_docx_with_embedded_ole_object_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    source_status: str,
) -> None:
    """Round 6 item 2: a docx containing an embedded OLE compound document
    (word/embeddings/oleObject1.bin with header D0 CF 11 E0 A1 B1 1A E1) is
    refused with '文件里嵌有无法检查的对象，不能拷贝'."""
    ole_member = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
    docx_bytes = _make_ooxml_with_embedded_xlsx(
        "word/document.xml",
        "word/embeddings/oleObject1.bin",
        ole_member,
    )

    sandbox = _install(
        monkeypatch, source_row={**COLLEAGUE_ROW, "status": source_status}
    )
    from src.server.services.persistence import file as persistence_file
    from src.server.app.workspace_files import _shared, crud as files_crud

    record = {
        "path": "memo/with_ole.docx",
        "is_binary": True,
        "file_size": len(docx_bytes),
    }
    monkeypatch.setattr(
        persistence_file.FilePersistenceService,
        "get_file_content",
        staticmethod(
            create_autospec(
                persistence_file.FilePersistenceService.get_file_content,
                return_value=record,
            )
        ),
    )
    monkeypatch.setattr(
        _shared,
        "http_file_bytes",
        create_autospec(_shared.http_file_bytes, return_value=docx_bytes),
    )

    async def _read_docx(sandbox_: Any, path: str, work_dir: str, previous_dirs=()):
        return f"{work_dir.rstrip('/')}/{path.lstrip('/')}", docx_bytes

    files_crud._read_contained_target.side_effect = _read_docx

    cmd = await workspace_file_copy_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path="memo/with_ole.docx",
        dest_path=None,
        tool_call_id=f"tc-ole-docx-{source_status}",
    )
    payload = _payload(cmd)
    assert payload.get("success") is False
    assert "文件里嵌有无法检查的对象，不能拷贝" in payload.get("error", "")
    sandbox.aupload_file_bytes.assert_not_called()







# --- Round 7 (lead): macro parts, PDF side streams, stream budget, scan child -----

_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _ooxml_with_member(member_name: str, member_bytes: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("xl/workbook.xml", "<workbook/>")
        zf.writestr(member_name, member_bytes)
    return buf.getvalue()


def test_macro_project_and_activex_parts_are_scanned_not_refused() -> None:
    """A normal .xlsm carries xl/vbaProject.bin (an OLE file); it copies when it
    holds no secret, and is refused when the raw bytes carry one."""
    vault = {"k": OWNER_SECRET}
    clean = _ooxml_with_member("xl/vbaProject.bin", _OLE + b"\x00" * 512)
    assert secretary_tools._scan_copy_bytes_for_secrets("book.xlsm", clean, vault) is None
    activex = _ooxml_with_member("xl/activeX/activeX1.bin", _OLE + b"\x00" * 64)
    assert secretary_tools._scan_copy_bytes_for_secrets("book.xlsx", activex, vault) is None
    dirty = _ooxml_with_member("xl/vbaProject.bin", _OLE + OWNER_SECRET.encode())
    assert "凭据" in (secretary_tools._scan_copy_bytes_for_secrets("book.xlsm", dirty, vault) or "")


def test_embedded_ole_object_is_still_refused() -> None:
    embedded = _ooxml_with_member("xl/embeddings/oleObject1.bin", _OLE + b"\x00" * 64)
    assert (
        secretary_tools._scan_copy_bytes_for_secrets("book.xlsx", embedded, {"k": OWNER_SECRET})
        == secretary_tools._COS_EMBEDDED_OLE_ERROR
    )


def _pdf_with_side_stream(page_text: str, side_payload: bytes) -> bytes:
    """A one-page PDF plus an unreferenced Flate stream, the shape of an
    attachment or annotation body that page text extraction never returns."""
    import zlib

    base = _make_flate_pdf_bytes(page_text)
    side = zlib.compress(side_payload)
    extra = (
        f"6 0 obj\n<< /Type /EmbeddedFile /Length {len(side)} /Filter /FlateDecode >>\nstream\n".encode("ascii")
        + side
        + b"\nendstream\nendobj\n"
    )
    head, sep, tail = base.partition(b"xref\n")
    return head + extra + sep + tail


def test_pdf_side_stream_with_secret_is_refused() -> None:
    pdf = _pdf_with_side_stream("Quarterly notes", f"api_key={OWNER_SECRET}".encode())
    assert OWNER_SECRET.encode() not in pdf
    # The hand-spliced object leaves the xref stale, so pypdf may refuse the
    # file as unreadable rather than name the secret; either way it does not copy.
    result = secretary_tools._scan_copy_bytes_for_secrets("memo/notes.pdf", pdf, {"k": OWNER_SECRET})
    assert result in (secretary_tools._COS_PDF_UNREADABLE_ERROR,) or "凭据" in (result or "")


def test_pdf_streams_past_the_decode_budget_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secretary_tools, "_COS_PDF_STREAM_TOTAL_MAX_BYTES", 1024)
    pdf = _pdf_with_side_stream("Quarterly notes", b"0" * 4096)
    assert (
        secretary_tools._scan_copy_bytes_for_secrets("memo/big.pdf", pdf, {"k": OWNER_SECRET})
        == secretary_tools._COS_PDF_UNREADABLE_ERROR
    )


@pytest.mark.asyncio
async def test_scan_child_timeout_refuses_the_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scan runs in a killable child; a timeout refuses the copy and writes nothing."""
    from src.dalton import heavy_compute

    sandbox = _install(monkeypatch, source_row=COLLEAGUE_ROW)
    _install_mirror_source(monkeypatch)
    monkeypatch.setattr(
        heavy_compute,
        "run_in_heavy_subprocess",
        create_autospec(
            heavy_compute.run_in_heavy_subprocess,
            side_effect=heavy_compute.HeavyComputeTimeout("copy_file_scan", 60.0),
        ),
    )
    command = await workspace_file_copy_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path="memo/report.pdf",
        dest_path=None,
        tool_call_id="tc-copy-timeout",
    )
    payload = _payload(command)
    assert payload.get("success") is False
    assert payload.get("error") == secretary_tools._COS_SCAN_FAILED_ERROR
    sandbox.aupload_file_bytes.assert_not_called()

    # Office files scan in a thread: a busy child pool never refuses them.
    command = await workspace_file_copy_cos(
        user_id="u-1",
        workspace_id="ws-anthropic",
        path="ant_sheet_update/Anthropic_sheet.xlsx",
        dest_path=None,
        tool_call_id="tc-copy-xlsx-thread",
    )
    assert _payload(command).get("success") is True
    assert heavy_compute.run_in_heavy_subprocess.call_count == 1


def _pdf_with_attachment(payload: bytes, *, owner_password: str | None = None, ascii85: bool = False) -> bytes:
    import base64
    import zlib

    import pypdf
    from pypdf.generic import ArrayObject, NameObject

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_attachment("notes.txt", payload)
    if ascii85:
        for obj in writer._objects:
            if isinstance(obj, pypdf.generic.StreamObject) and obj.get("/Type") == "/EmbeddedFile":
                raw = obj.get_data()
                encoded = base64.a85encode(zlib.compress(raw), adobe=True)
                obj._data = encoded
                obj[NameObject("/Filter")] = ArrayObject([NameObject("/ASCII85Decode"), NameObject("/FlateDecode")])
                obj[NameObject("/Length")] = pypdf.generic.NumberObject(len(encoded))
    if owner_password is not None:
        writer.encrypt(user_password="", owner_password=owner_password, algorithm="RC4-128")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_pdf_attachment_with_filter_chain_is_refused() -> None:
    pdf = _pdf_with_attachment(f"api_key={OWNER_SECRET}".encode(), ascii85=True)
    assert OWNER_SECRET.encode() not in pdf
    result = secretary_tools._scan_copy_bytes_for_secrets("memo/a85.pdf", pdf, {"k": OWNER_SECRET})
    assert "凭据" in (result or "")


def test_owner_password_only_pdf_attachment_is_refused() -> None:
    pdf = _pdf_with_attachment(f"api_key={OWNER_SECRET}".encode(), owner_password="owner-only-pw")
    assert OWNER_SECRET.encode() not in pdf
    result = secretary_tools._scan_copy_bytes_for_secrets("memo/enc.pdf", pdf, {"k": OWNER_SECRET})
    assert "凭据" in (result or "")


def test_clean_pdf_with_attachment_copies() -> None:
    pdf = _pdf_with_attachment(b"meeting notes, nothing sensitive")
    assert secretary_tools._scan_copy_bytes_for_secrets("memo/ok.pdf", pdf, {"k": OWNER_SECRET}) is None


def test_ole_renamed_as_macro_under_embeddings_is_refused() -> None:
    for name in ("word/embeddings/vbaProject.bin", "word/embeddings/activeX/control.bin", "xl/activeX/control.bin"):
        doc = _ooxml_with_member(name, _OLE + b"\x00" * 64)
        assert (
            secretary_tools._scan_copy_bytes_for_secrets("doc.docx", doc, {"k": OWNER_SECRET})
            == secretary_tools._COS_EMBEDDED_OLE_ERROR
        ), name


def _pdf_from_objects(objs: list[tuple[int, int, bytes]], root: str = "1 0 R") -> bytes:
    """Minimal PDF with a correct xref; each entry is (number, generation, body)."""
    out = bytearray(b"%PDF-1.7\n")
    offsets: dict[int, tuple[int, int]] = {}
    for num, gen, body in objs:
        offsets[num] = (len(out), gen)
        out.extend(f"{num} {gen} obj\n".encode("ascii") + body + b"\nendobj\n")
    size = max(offsets) + 1
    xref_pos = len(out)
    out.extend(f"xref\n0 {size}\n0000000000 65535 f \n".encode("ascii"))
    for num in range(1, size):
        if num in offsets:
            off, gen = offsets[num]
            out.extend(f"{off:010d} {gen:05d} n \n".encode("ascii"))
        else:
            out.extend(b"0000000000 65535 f \n")
    out.extend(f"trailer\n<< /Size {size} /Root {root} >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii"))
    return bytes(out)


def _flate_stream(extra_dict: str, payload: bytes) -> bytes:
    import zlib

    data = zlib.compress(payload)
    return f"<< {extra_dict} /Length {len(data)} /Filter /FlateDecode >>\nstream\n".encode("ascii") + data + b"\nendstream"


def _attachment_pdf(attachment_ref: str, attachment: tuple[int, int, bytes]) -> bytes:
    return _pdf_from_objects(
        [
            (1, 0, f"<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles << /Names [(n.txt) 4 0 R] >> >> >>".encode()),
            (2, 0, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
            (3, 0, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>"),
            (4, 0, f"<< /Type /Filespec /F (n.txt) /EF << /F {attachment_ref} >> >>".encode()),
            attachment,
        ]
    )


def test_attachment_in_a_nonzero_generation_is_scanned() -> None:
    pdf = _attachment_pdf(
        "5 1 R", (5, 1, _flate_stream("/Type /EmbeddedFile", f"api_key={OWNER_SECRET}".encode()))
    )
    assert OWNER_SECRET.encode() not in pdf
    result = secretary_tools._scan_copy_bytes_for_secrets("memo/gen1.pdf", pdf, {"k": OWNER_SECRET})
    assert "凭据" in (result or "")


def test_attachment_claiming_to_be_an_image_is_still_scanned() -> None:
    pdf = _attachment_pdf(
        "5 0 R",
        (5, 0, _flate_stream("/Type /EmbeddedFile /Subtype /Image", f"api_key={OWNER_SECRET}".encode())),
    )
    result = secretary_tools._scan_copy_bytes_for_secrets("memo/img.pdf", pdf, {"k": OWNER_SECRET})
    assert "凭据" in (result or "")


def test_jpeg_image_streams_are_not_decoded() -> None:
    """A scanned page's JPEG stream is skipped, so a normal scan copies."""
    jpeg_like = b"\xff\xd8\xff\xe0" + b"\x00" * 256 + b"\xff\xd9"
    pdf = _pdf_from_objects(
        [
            (1, 0, b"<< /Type /Catalog /Pages 2 0 R >>"),
            (2, 0, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
            (3, 0, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << /XObject << /Im0 4 0 R >> >> >>"),
            (
                4,
                0,
                f"<< /Type /XObject /Subtype /Image /Width 10 /Height 10 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpeg_like)} >>\nstream\n".encode()
                + jpeg_like
                + b"\nendstream",
            ),
        ]
    )
    assert secretary_tools._scan_copy_bytes_for_secrets("scan/page.pdf", pdf, {"k": OWNER_SECRET}) is None


def _jpeg_with_comment(comment: bytes) -> bytes:
    # SOI, COM segment carrying the text, minimal EOI: enough for the scan.
    seg = b"\xff\xfe" + (len(comment) + 2).to_bytes(2, "big") + comment
    return b"\xff\xd8" + seg + b"\xff\xd9"


def test_attachment_referenced_by_ef_is_scanned_even_in_an_image_codec() -> None:
    """An /EF target is decoded even when it omits /Type or uses an image codec."""
    import base64

    jpeg = _jpeg_with_comment(f"api_key={OWNER_SECRET}".encode())
    a85 = base64.a85encode(jpeg, adobe=True)
    for type_entry in ("", "/Type 6 0 R"):
        stream = (
            f"<< {type_entry} /Subtype /Image /Width 1 /Height 1 /Filter [/ASCII85Decode /DCTDecode] /Length {len(a85)} >>\nstream\n".encode()
            + a85
            + b"\nendstream"
        )
        objs = [
            (1, 0, b"<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles << /Names [(n.jpg) 4 0 R] >> >> >>"),
            (2, 0, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
            (3, 0, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>"),
            (4, 0, b"<< /Type /Filespec /F (n.jpg) /EF << /F 5 0 R >> >>"),
            (5, 0, stream),
            (6, 0, b"/EmbeddedFile"),
        ]
        pdf = _pdf_from_objects(objs)
        assert OWNER_SECRET.encode() not in pdf
        result = secretary_tools._scan_copy_bytes_for_secrets("memo/att.pdf", pdf, {"k": OWNER_SECRET})
        assert "凭据" in (result or ""), type_entry


def test_attachment_behind_a_direct_filespec_in_an_image_codec_is_scanned() -> None:
    """No structural exemption: an image-codec stream is scanned through its text
    filters even when the file specification is a direct dictionary in the name tree."""
    import base64

    jpeg = _jpeg_with_comment(f"api_key={OWNER_SECRET}".encode())
    a85 = base64.a85encode(jpeg, adobe=True)
    stream = (
        f"<< /Subtype /Image /Width 1 /Height 1 /Filter [/ASCII85Decode /DCTDecode] /Length {len(a85)} >>\nstream\n".encode()
        + a85
        + b"\nendstream"
    )
    pdf = _pdf_from_objects(
        [
            (1, 0, b"<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles << /Names [(n.jpg) << /Type /Filespec /F (n.jpg) /EF << /F 4 0 R >> >>] >> >> >>"),
            (2, 0, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
            (3, 0, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>"),
            (4, 0, stream),
        ]
    )
    assert OWNER_SECRET.encode() not in pdf
    result = secretary_tools._scan_copy_bytes_for_secrets("memo/direct.pdf", pdf, {"k": OWNER_SECRET})
    assert "凭据" in (result or "")


def test_encrypted_pdf_metadata_and_annotation_strings_are_scanned() -> None:
    import pypdf
    from pypdf.annotations import Text

    for where in ("metadata", "annotation"):
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=200, height=200)
        if where == "metadata":
            writer.add_metadata({"/Title": f"key {OWNER_SECRET}"})
        else:
            writer.add_annotation(0, Text(rect=(10, 10, 50, 50), text=f"key {OWNER_SECRET}"))
        writer.encrypt(user_password="", owner_password="owner-only-pw", algorithm="RC4-128")
        buf = io.BytesIO()
        writer.write(buf)
        pdf = buf.getvalue()
        assert OWNER_SECRET.encode() not in pdf
        result = secretary_tools._scan_copy_bytes_for_secrets("memo/enc.pdf", pdf, {"k": OWNER_SECRET})
        assert "凭据" in (result or ""), where


def _pdf_with_file_attachment(name: str, payload: bytes) -> bytes:
    import pypdf

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_attachment(name, payload)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_workbook_or_pdf_attached_inside_a_pdf_is_scanned_by_its_own_format() -> None:
    inner_xlsx = _make_xlsx_bytes(f"api key {OWNER_SECRET}")
    inner_pdf = _make_flate_pdf_bytes(f"api key {OWNER_SECRET}")
    for name, payload in (("model.xlsx", inner_xlsx), ("memo.pdf", inner_pdf)):
        assert OWNER_SECRET.encode() not in payload
        pdf = _pdf_with_file_attachment(name, payload)
        result = secretary_tools._scan_copy_bytes_for_secrets("memo/outer.pdf", pdf, {"k": OWNER_SECRET})
        assert "凭据" in (result or ""), name


def test_clean_workbook_attached_inside_a_pdf_copies_and_archive_is_refused() -> None:
    clean = _pdf_with_file_attachment("model.xlsx", _make_xlsx_bytes("revenue model"))
    assert secretary_tools._scan_copy_bytes_for_secrets("memo/outer.pdf", clean, {"k": OWNER_SECRET}) is None
    import gzip

    archived = _pdf_with_file_attachment("dump.gz", gzip.compress(b"anything"))
    assert "压缩包" in (secretary_tools._scan_copy_bytes_for_secrets("memo/outer.pdf", archived, {"k": OWNER_SECRET}) or "")


def test_workbook_hidden_under_an_image_codec_label_is_still_opened() -> None:
    inner = _make_xlsx_bytes(f"api key {OWNER_SECRET}")
    stream = (
        f"<< /Type /EmbeddedFile /Filter /DCTDecode /Length {len(inner)} >>\nstream\n".encode() + inner + b"\nendstream"
    )
    pdf = _attachment_pdf("5 0 R", (5, 0, stream))
    result = secretary_tools._scan_copy_bytes_for_secrets("memo/dct.pdf", pdf, {"k": OWNER_SECRET})
    assert "凭据" in (result or "")


def test_inner_pdf_with_leading_bytes_is_scanned_like_a_direct_copy() -> None:
    inner = b"\n" + _make_flate_pdf_bytes(f"api key {OWNER_SECRET}")
    pdf = _pdf_with_file_attachment("memo.pdf", inner)
    result = secretary_tools._scan_copy_bytes_for_secrets("memo/outer.pdf", pdf, {"k": OWNER_SECRET})
    assert "凭据" in (result or "")


def test_flate_image_whose_pixels_start_like_gzip_is_not_refused() -> None:
    import zlib

    pixels = bytes([31, 139, 0]) * 64  # an 8x8 RGB image in one colour
    data = zlib.compress(pixels)
    stream = (
        f"<< /Type /XObject /Subtype /Image /Width 8 /Height 8 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length {len(data)} >>\nstream\n".encode()
        + data
        + b"\nendstream"
    )
    pdf = _pdf_from_objects(
        [
            (1, 0, b"<< /Type /Catalog /Pages 2 0 R >>"),
            (2, 0, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
            (3, 0, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << /XObject << /Im0 4 0 R >> >> >>"),
            (4, 0, stream),
        ]
    )
    assert secretary_tools._scan_copy_bytes_for_secrets("scan/px.pdf", pdf, {"k": OWNER_SECRET}) is None


def test_text_pdf_mentioning_the_pdf_header_copies() -> None:
    pdf = _make_flate_pdf_bytes("The PDF header is %PDF-1.7")
    assert secretary_tools._scan_copy_bytes_for_secrets("memo/header.pdf", pdf, {"k": OWNER_SECRET}) is None


def test_one_pixel_images_matching_archive_signatures_copy() -> None:
    import zlib

    for pixel in (bytes([31, 139, 8]), b"BZh"):
        data = zlib.compress(pixel)
        stream = (
            f"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length {len(data)} >>\nstream\n".encode()
            + data
            + b"\nendstream"
        )
        pdf = _pdf_from_objects(
            [
                (1, 0, b"<< /Type /Catalog /Pages 2 0 R >>"),
                (2, 0, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
                (3, 0, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << /XObject << /Im0 4 0 R >> >> >>"),
                (4, 0, stream),
            ]
        )
        assert secretary_tools._scan_copy_bytes_for_secrets("scan/px1.pdf", pdf, {"k": OWNER_SECRET}) is None, pixel


def test_zip_without_central_directory_inside_a_pdf_is_refused() -> None:
    whole = _make_xlsx_bytes("anything")
    local_only = whole[: whole.rfind(b"PK\x01\x02")] if b"PK\x01\x02" in whole else whole
    pdf = _pdf_with_file_attachment("partial.zip", local_only)
    result = secretary_tools._scan_copy_bytes_for_secrets("memo/outer.pdf", pdf, {"k": OWNER_SECRET})
    assert result is not None


def test_text_naming_both_pdf_keywords_copies() -> None:
    pdf = _make_flate_pdf_bytes("The PDF header is %PDF-1.7 and its trailer uses startxref.")
    assert secretary_tools._scan_copy_bytes_for_secrets("memo/kw.pdf", pdf, {"k": OWNER_SECRET}) is None


def test_inner_pdf_padded_past_its_trailer_window_is_still_scanned() -> None:
    inner = _make_flate_pdf_bytes(f"api key {OWNER_SECRET}") + b"\n" * 2048
    pdf = _pdf_with_file_attachment("memo.pdf", inner)
    result = secretary_tools._scan_copy_bytes_for_secrets("memo/outer.pdf", pdf, {"k": OWNER_SECRET})
    assert "凭据" in (result or "")


def test_stream_starting_with_a_pdf_comment_is_ordinary_content() -> None:
    """A page content stream may begin with a "%PDF-" comment: it copies, and a
    secret written in such a stream is still caught by the byte scan."""
    clean = _make_flate_pdf_bytes("Quarterly revenue overview")
    page = b"%PDF-1.7\nBT /F1 12 Tf 72 712 Td (Quarterly revenue overview) Tj ET"
    import zlib

    commented = _build_valid_pdf(zlib.compress(page), "/FlateDecode")
    assert secretary_tools._scan_copy_bytes_for_secrets("memo/c.pdf", commented, {"k": OWNER_SECRET}) is None
    assert secretary_tools._scan_copy_bytes_for_secrets("memo/ok.pdf", clean, {"k": OWNER_SECRET}) is None
    leaked = _pdf_with_file_attachment("broken.pdf", b"%PDF-1.7\n" + f"key {OWNER_SECRET}".encode())
    assert "凭据" in (secretary_tools._scan_copy_bytes_for_secrets("memo/outer.pdf", leaked, {"k": OWNER_SECRET}) or "")


def test_archives_that_need_more_than_one_chunk_before_output_are_detected() -> None:
    import bz2
    import os
    import struct
    import zlib

    big_bz2 = bz2.compress(os.urandom(200_000))
    assert secretary_tools._is_real_archive(big_bz2)
    payload = b"hello world" * 10
    extra = b"x" * 65535
    header = b"\x1f\x8b\x08\x04" + b"\x00" * 4 + b"\x00\xff" + struct.pack("<H", len(extra)) + extra
    deflater = zlib.compressobj(9, zlib.DEFLATED, -15)
    body = deflater.compress(payload) + deflater.flush()
    long_gzip = header + body + struct.pack("<II", zlib.crc32(payload), len(payload))
    assert secretary_tools._is_real_archive(long_gzip)
    assert not secretary_tools._is_real_archive(bytes([31, 139, 8]) * 64)


def test_password_protected_inner_pdf_with_a_prefix_is_refused() -> None:
    import pypdf

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Title": f"key {OWNER_SECRET}"})
    writer.encrypt(user_password="user-pw", owner_password="owner-pw", algorithm="RC4-128")
    buf = io.BytesIO()
    writer.write(buf)
    inner = b"X" + buf.getvalue()
    pdf = _pdf_with_file_attachment("locked.pdf", inner)
    result = secretary_tools._scan_copy_bytes_for_secrets("memo/outer.pdf", pdf, {"k": OWNER_SECRET})
    assert result == secretary_tools._COS_PDF_UNREADABLE_ERROR


def test_inner_pdf_with_an_unsupported_encryption_is_refused() -> None:
    import re

    import pypdf

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Title": f"key {OWNER_SECRET}"})
    writer.encrypt(user_password="user-pw", owner_password="owner-pw", algorithm="RC4-128")
    buf = io.BytesIO()
    writer.write(buf)
    inner = re.sub(rb"/V 2", b"/V 7", buf.getvalue(), count=1)
    pdf = _pdf_with_file_attachment("locked.pdf", inner)
    result = secretary_tools._scan_copy_bytes_for_secrets("memo/outer.pdf", pdf, {"k": OWNER_SECRET})
    assert result == secretary_tools._COS_PDF_UNREADABLE_ERROR
