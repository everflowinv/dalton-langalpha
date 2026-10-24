#!/usr/bin/env python3
"""Preview a Dalton Feishu Card Schema 2.0 notification in ``--dry-run`` mode without sending.

Usage:
    .venv/bin/python scripts/dalton/notify_preview.py --automation dalton-googl-earn-2026q1-t0 --execution exec-123
    .venv/bin/python scripts/dalton/notify_preview.py --automation dalton-googl-earn-2026q1-t0 --execution exec-123 --sample
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.dalton.notify.dispatch import (  # noqa: E402
    build_feishu_notify_card,
    classify_automation_for_notify,
)
from src.dalton.settings import (  # noqa: E402
    FEISHU_SEND_CARD_TIMEOUT_S,
    get_dalton_frontend_url,
)
from src.server.app import setup  # noqa: E402
import src.server.services.automation_excerpt as _excerpt_mod  # noqa: E402
from src.server.services.automation_excerpt import read_run_answer  # noqa: E402
from src.server.services.history.reader import CheckpointHistoryReader  # noqa: E402
from src.server.utils.checkpointer import (  # noqa: E402
    close_checkpointer_pool,
    get_checkpointer,
    open_checkpointer_pool,
)

_ORIG_READ_RUN_ANSWER = read_run_answer
_last_load_used_excerpt: bool = False
TRUNCATED_EXCERPT_WARNING = "使用的是截断摘要，正文可能不完整"

DEFAULT_FEISHU_SCRIPT = Path(
    os.path.expanduser("~/.openclaw/workspace/scripts/feishu_send_card.py")
)

DEFAULT_SAMPLE_TEXT = (
    "## 结论\n"
    "本季核心营收与云业务利润率均超市场一致预期，AI 算力商业化进度得到确认。\n\n"
    "## 要点\n"
    "- 云业务营业利润率环比提升，订单积压保持强劲增长。\n"
    "- 核心广告业务受搜索生成式体验带动，单次点击变现效率稳步改善。\n\n"
    "## 下一步验证\n"
    "重点跟踪下季度资本开支转化率及大客户续约节奏。\n\n"
    "## 来源\n"
    "公司财报公告、业绩电话会纪要及一致预期对比。"
)


async def _try_read_run_answer(
    thread_id: str,
    run_id: str | None,
) -> Any:
    read_fn = (
        read_run_answer
        if read_run_answer is not _ORIG_READ_RUN_ANSWER
        else _excerpt_mod.read_run_answer
    )
    try:
        return await read_fn(thread_id, run_id)
    except Exception:
        return None


async def _load_from_db(
    execution_id: str,
) -> tuple[str | None, str | None, str | None]:
    """Load ``(verdict_word, final_text_or_excerpt, thread_id)`` for ``execution_id`` from Postgres.

    Initializes the LangGraph checkpointer in the same way as ``src/server/app/setup.py``
    (``get_checkpointer`` + ``await open_checkpointer_pool(checkpointer)`` + ``setup.checkpointer``)
    so ``CheckpointHistoryReader.get_instance()`` used by ``read_run_answer(thread_id, run_id)``
    is ready, and closes both the checkpointer pool and app DB pool on exit.
    Raises ``LookupError`` if the execution row is not found.
    """
    global _last_load_used_excerpt
    _last_load_used_excerpt = False

    cleaned_id = (execution_id or "").strip()
    if not cleaned_id:
        raise ValueError("execution_id must be non-empty")

    from psycopg.rows import dict_row
    from src.dalton.database.verdicts import get_verdict_for_execution
    from src.server.database.pool import get_db_connection, get_or_create_pool

    pool = get_or_create_pool()
    await pool.open()
    prev_checkpointer = setup.checkpointer
    checkpointer: Any = None
    try:
        checkpointer = get_checkpointer(
            memory_type=os.getenv("MEMORY_DB_TYPE", "postgres"),
            db_host=os.getenv("DB_HOST", "localhost"),
            db_port=os.getenv("DB_PORT", "5432"),
            db_name=os.getenv("DB_NAME", "postgres"),
            db_user=os.getenv("DB_USER", "postgres"),
            db_password=os.getenv("DB_PASSWORD", "postgres"),
        )
        await open_checkpointer_pool(checkpointer)
        setup.checkpointer = checkpointer
        CheckpointHistoryReader.reset_instance()

        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT conversation_thread_id, conversation_response_id, result_excerpt
                    FROM automation_executions
                    WHERE automation_execution_id::text = %s
                    LIMIT 1
                    """,
                    (cleaned_id,),
                )
                row = await cur.fetchone()
        if not row:
            raise LookupError(
                f"Execution {cleaned_id!r} not found in automation_executions"
            )
        result_excerpt = str(row.get("result_excerpt") or "").strip() or None
        thread_id = (
            str(row.get("conversation_thread_id") or "").strip() or None
        )
        run_id = (
            str(row.get("conversation_response_id") or "").strip() or None
        )

        full_text: str | None = None
        if thread_id and run_id:
            answer = await _try_read_run_answer(thread_id, run_id)
            if answer is not None:
                candidate = str(
                    getattr(answer, "sent", None)
                    or getattr(answer, "text", None)
                    or ""
                ).strip()
                if candidate:
                    full_text = candidate

        if full_text is not None:
            resolved_text: str | None = full_text
        elif result_excerpt is not None:
            resolved_text = result_excerpt
            _last_load_used_excerpt = True
            print(TRUNCATED_EXCERPT_WARNING, file=sys.stderr)
        else:
            resolved_text = None

        verdict_row = await get_verdict_for_execution(
            automation_execution_id=cleaned_id
        )
        verdict_word = (
            str(verdict_row.get("word") or "").strip() or None
            if isinstance(verdict_row, dict)
            else None
        )
        return verdict_word, resolved_text, thread_id
    finally:
        CheckpointHistoryReader.reset_instance()
        setup.checkpointer = prev_checkpointer
        if checkpointer is not None:
            await close_checkpointer_pool(checkpointer)
        await pool.close()


def run_preview(
    *,
    automation_name: str,
    execution_id: str,
    verdict_word: str | None = None,
    final_text: str | None = None,
    thread_id: str | None = None,
    frontend_url: str | None = None,
    sample: bool = False,
    script_path: Path = DEFAULT_FEISHU_SCRIPT,
    timeout_s: float = FEISHU_SEND_CARD_TIMEOUT_S,
) -> dict[str, Any]:
    """Generate Feishu Card 2.0 JSON and validate/render it via ``feishu_send_card.py --dry-run``."""
    classification = classify_automation_for_notify({"name": automation_name})
    if classification is None:
        raise ValueError(
            f"Automation {automation_name!r} is not in the 4 pushable notification categories"
        )

    used_excerpt = False
    if sample:
        eff_verdict = verdict_word if verdict_word is not None else "THESIS_STRENGTHENED"
        eff_text = final_text if final_text is not None else DEFAULT_SAMPLE_TEXT
        eff_thread_id = (
            thread_id
            if thread_id is not None
            else f"preview-{execution_id or 'sample'}"
        )
    else:
        db_verdict, db_text, db_thread_id = asyncio.run(
            _load_from_db(execution_id)
        )
        eff_verdict = verdict_word if verdict_word is not None else db_verdict
        if final_text is not None:
            eff_text = final_text
        else:
            eff_text = db_text
            used_excerpt = _last_load_used_excerpt
        if eff_text is None:
            raise LookupError(
                f"Execution {execution_id!r} has no result_excerpt (pass --sample to preview sample content)"
            )
        eff_thread_id = thread_id if thread_id is not None else db_thread_id

    eff_frontend_url = (
        frontend_url if frontend_url is not None else get_dalton_frontend_url()
    )

    card = build_feishu_notify_card(
        classification,
        verdict_word=eff_verdict,
        final_text=eff_text,
        thread_id=eff_thread_id,
        frontend_base_url=eff_frontend_url,
        is_excerpt=used_excerpt,
    )

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            prefix="dalton_notify_preview_",
            encoding="utf-8",
            delete=False,
        ) as fh:
            json.dump(card, fh, ensure_ascii=False, indent=2)
            tmp_path = Path(fh.name)

        cmd = [
            "python3",
            str(script_path),
            "--chat-id",
            "ou_preview_dry_run",
            "--receive-id-type",
            "open_id",
            "--card-json-file",
            str(tmp_path),
            "--dry-run",
            "--json",
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"feishu_send_card.py --dry-run exited with code {proc.returncode}"
            )
        if proc.stdout and proc.stdout.strip():
            rendered = json.loads(proc.stdout)
        else:
            rendered = dict(card)
        if used_excerpt and isinstance(rendered, dict):
            rendered = {"warning": TRUNCATED_EXCERPT_WARNING, **rendered}
        return rendered
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate and dry-run preview a Dalton Feishu Card 2.0 notification."
    )
    parser.add_argument(
        "--automation",
        required=True,
        help="Automation name (e.g. dalton-googl-earn-2026q1-t0)",
    )
    parser.add_argument(
        "--execution",
        required=True,
        help="Execution ID to preview",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Use built-in sample text instead of loading result_excerpt from database",
    )
    parser.add_argument(
        "--verdict",
        default=None,
        help="Optional verdict word override (e.g. THESIS_STRENGTHENED)",
    )
    parser.add_argument(
        "--text",
        default=None,
        help="Optional markdown summary text override",
    )
    parser.add_argument(
        "--thread-id",
        default=None,
        help="Optional conversation thread ID override",
    )
    parser.add_argument(
        "--frontend-url",
        default=None,
        help="Optional public frontend base URL override",
    )
    parser.add_argument(
        "--script",
        default=str(DEFAULT_FEISHU_SCRIPT),
        help="Path to OpenClaw feishu_send_card.py",
    )
    args = parser.parse_args(argv)

    try:
        rendered = run_preview(
            automation_name=args.automation,
            execution_id=args.execution,
            verdict_word=args.verdict,
            final_text=args.text,
            thread_id=args.thread_id,
            frontend_url=args.frontend_url,
            sample=bool(args.sample),
            script_path=Path(args.script).expanduser(),
        )
    except Exception as exc:
        print(f"notify_preview error: {exc}", file=sys.stderr)
        return 1

    if not args.sample and args.text is None and _last_load_used_excerpt:
        print(TRUNCATED_EXCERPT_WARNING)
    print(json.dumps(rendered, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
