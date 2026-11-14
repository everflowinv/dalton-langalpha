#!/usr/bin/env python3
"""Publish helper for `dalton-podcast-weekly`, delegating state & path guards to `src.dalton.podcast_weekly_store`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

def _find_repo_root(start: Path) -> Path:
    resolved = start.resolve()
    for parent in resolved.parents:
        if (parent / "src" / "dalton" / "podcast_weekly_store.py").is_file():
            return parent
    return resolved.parents[5]


DEFAULT_SPLIT_CHARS = 15000
CHANNELS_PATH = Path(__file__).resolve().parents[1] / "references" / "channels.json"
_REPO_ROOT = _find_repo_root(Path(__file__))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.dalton.podcast_weekly_store import (  # noqa: E402
    PodcastStoreSecurityError,
    RecordUnreadableError,
    abandon_pending_report,
    atomic_write_text,
    doc_record_path,
    get_status,
    ledger_path,
    mark_reported,
    prepare_doc,
    read_json_dict,
    record_chunk,
    record_failure,
    save_doc,
    save_pending_report,
    validate_video_id,
)


def _extract_feishu_result(raw: Any) -> tuple[bool, str, str, bool, str | None]:
    """Parse `feishu_create_doc` return into `(has_doc, doc_id, doc_url, is_complete, error)`.

    Preserves `document_id` and `doc_url` even on `content_insert_failed` so subsequent turns
    resume `feishu_append` against the existing document rather than creating duplicates.
    """
    if not isinstance(raw, dict):
        return False, "", "", False, "feishu return payload is non-object"

    doc_obj = raw.get("data", {}).get("document") if isinstance(raw.get("data"), dict) else None
    doc_id = str(
        raw.get("document_id")
        or (doc_obj.get("document_id") if isinstance(doc_obj, dict) else "")
        or ""
    ).strip()
    doc_url = str(
        raw.get("doc_url")
        or raw.get("url")
        or (doc_obj.get("url") if isinstance(doc_obj, dict) else "")
        or ""
    ).strip()
    if not doc_url and doc_id:
        doc_url = f"https://feishu.cn/docx/{doc_id}"

    err_val = raw.get("error")
    ok_flag = raw.get("ok") is not False and not err_val
    if ok_flag and doc_url:
        return True, doc_id, doc_url, True, None

    if doc_id or doc_url:
        err_msg = str(err_val or raw.get("detail") or "content_insert_failed")
        return True, doc_id, doc_url, False, err_msg

    err_msg = str(err_val or raw.get("detail") or "feishu_create_doc failed")
    return False, "", "", False, err_msg


def cmd_status(args: argparse.Namespace) -> int:
    ws = Path(args.workspace_dir).resolve()
    payload = get_status(
        ws,
        date_str=args.date,
        pending_only=bool(args.pending),
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("ok") else 1


def cmd_prepare_doc(args: argparse.Namespace) -> int:
    ws = Path(args.workspace_dir).resolve()
    vid = validate_video_id(args.video)
    try:
        payload = prepare_doc(
            ws,
            vid,
            max_chars=args.split_chars,
            date_str=getattr(args, "date", None),
            channels_path=CHANNELS_PATH,
            summary_file=args.summary_file,
            redo=bool(args.redo),
        )
    except (FileNotFoundError, RecordUnreadableError, PodcastStoreSecurityError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "create": False,
                    "video_id": vid,
                    "reason": type(exc).__name__,
                    "error": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 1

    if payload.get("ok") and payload.get("markdown"):
        # Emit convenience workspace chunk files alongside `chunks` list when workspace has temp/.
        out_rel = f"temp/podcast-weekly/publish/{vid}.doc.md"
        out_path = (ws / out_rel).resolve()
        if out_path.is_relative_to(ws):
            atomic_write_text(out_path, payload["markdown"])
            payload["markdown_rel_path"] = out_rel
            for chunk in payload.get("chunks") or []:
                part_rel = f"temp/podcast-weekly/publish/{vid}.part{chunk['part']}.md"
                part_path = (ws / part_rel).resolve()
                if part_path.is_relative_to(ws):
                    atomic_write_text(part_path, chunk["content"])
                    chunk["rel_path"] = part_rel
                    chunk["markdown"] = chunk["content"]

    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("ok") else 1


def cmd_save_doc(args: argparse.Namespace) -> int:
    ws = Path(args.workspace_dir).resolve()
    vid = validate_video_id(args.video)
    ret_path = (ws / args.feishu_return).resolve()
    if not ret_path.is_relative_to(ws) or ret_path.is_symlink():
        print(
            json.dumps(
                {"ok": False, "video_id": vid, "reason": "doc_record_unreadable", "error": "invalid path"},
                ensure_ascii=False,
            )
        )
        return 1
    try:
        ledger_data = read_json_dict(ledger_path(ws), required=True)
        ret_data = read_json_dict(ret_path, required=True)
    except FileNotFoundError as exc:
        print(
            json.dumps(
                {"ok": False, "video_id": vid, "reason": "feishu_return_missing", "error": str(exc)},
                ensure_ascii=False,
            )
        )
        return 1
    except (RecordUnreadableError, PodcastStoreSecurityError, ValueError) as exc:
        print(
            json.dumps(
                {"ok": False, "video_id": vid, "reason": "doc_record_unreadable", "error": str(exc)},
                ensure_ascii=False,
            )
        )
        return 1

    if vid not in ledger_data:
        print(
            json.dumps(
                {
                    "ok": False,
                    "video_id": vid,
                    "reason": "episode_not_in_ledger",
                    "error": f"{vid} missing from episodes.json",
                },
                ensure_ascii=False,
            )
        )
        return 1

    has_doc, doc_id, doc_url, feishu_ok, err = _extract_feishu_result(ret_data)
    if not has_doc:
        fail_payload = record_failure(ws, vid, err or "feishu create failed")
        fail_payload["ok"] = False
        print(json.dumps(fail_payload, ensure_ascii=False))
        return 1

    if not feishu_ok or args.body_incomplete:
        completed_parts: list[int] = []
        body_complete: bool | None = False
    elif args.total_parts > 1:
        completed_parts = [args.part] if args.part > 0 else [1]
        body_complete = False
    else:
        completed_parts = [1]
        body_complete = True

    try:
        saved = save_doc(
            ws,
            vid,
            doc_url=doc_url,
            document_id=doc_id,
            zh_title=args.zh_title or "",
            total_parts=max(1, int(args.total_parts or 1)),
            completed_parts=completed_parts,
            body_complete=body_complete,
        )
    except (RecordUnreadableError, PodcastStoreSecurityError, ValueError) as exc:
        print(
            json.dumps(
                {"ok": False, "video_id": vid, "reason": "doc_record_unreadable", "error": str(exc)},
                ensure_ascii=False,
            )
        )
        return 1

    if not feishu_ok:
        saved["partial_insert_failed"] = True
        saved["insert_error"] = err
    print(json.dumps(saved, ensure_ascii=False))
    return 0


def cmd_save_chunk(args: argparse.Namespace) -> int:
    ws = Path(args.workspace_dir).resolve()
    vid = validate_video_id(args.video)
    try:
        out = record_chunk(
            ws,
            vid,
            part=args.part,
            total_parts=args.total_parts,
            document_id=args.document_id or "",
            doc_url=args.doc_url or "",
        )
    except (FileNotFoundError, RecordUnreadableError, PodcastStoreSecurityError, ValueError) as exc:
        print(json.dumps({"ok": False, "video_id": vid, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_record_failure(args: argparse.Namespace) -> int:
    ws = Path(args.workspace_dir).resolve()
    vid = validate_video_id(args.video)
    try:
        read_json_dict(ledger_path(ws))
        read_json_dict(doc_record_path(ws, vid))
        payload = record_failure(ws, vid, args.error)
    except (RecordUnreadableError, PodcastStoreSecurityError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "video_id": vid,
                    "reason": "doc_record_unreadable",
                    "error": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def cmd_save_pending_report(args: argparse.Namespace) -> int:
    ws = Path(args.workspace_dir).resolve()
    text_val = args.text
    if text_val is None and args.text_file:
        tpath = Path(args.text_file)
        if tpath.is_symlink():
            print(json.dumps({"ok": False, "error": "symlink_text_file"}, ensure_ascii=False))
            return 1
        text_val = tpath.read_text(encoding="utf-8")
    if text_val is None:
        print(json.dumps({"ok": False, "error": "missing_text"}, ensure_ascii=False))
        return 1
    success_vids = [v for v in (args.success_video_ids or []) if v]
    failed_vids = [v for v in (args.failed_video_ids or []) if v]
    try:
        out = save_pending_report(
            ws,
            args.report_run_id,
            text_val,
            success_ids=success_vids or None,
            failed_ids=failed_vids or None,
            empty_date=args.empty_date,
            date_str=args.date,
        )
    except (PodcastStoreSecurityError, RecordUnreadableError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


def cmd_abandon_pending_report(args: argparse.Namespace) -> int:
    ws = Path(args.workspace_dir).resolve()
    try:
        out = abandon_pending_report(
            ws,
            args.report_run_id,
            date_str=args.date,
        )
    except (PodcastStoreSecurityError, RecordUnreadableError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


def cmd_mark_reported(args: argparse.Namespace) -> int:
    ws = Path(args.workspace_dir).resolve()
    success_vids = [v for v in (args.success_video_ids or []) if v]
    failed_vids = [v for v in (args.failed_video_ids or []) if v]
    try:
        out = mark_reported(
            ws,
            args.report_run_id,
            success_ids=success_vids or None,
            failed_ids=failed_vids or None,
            empty_date=args.empty_date,
        )
    except (PodcastStoreSecurityError, RecordUnreadableError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dalton podcast weekly publish helper")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Inspect pending/incomplete episodes and report_run_id")
    p_status.add_argument("--workspace-dir", default=".")
    p_status.add_argument("--date", help="Override HK date YYYY-MM-DD for report_run_id")
    p_status.add_argument("--pending", action="store_true")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_prep = sub.add_parser("prepare-doc", help="Assemble 4-section Markdown for Feishu doc creation")
    p_prep.add_argument("--workspace-dir", default=".")
    p_prep.add_argument("--video", required=True)
    p_prep.add_argument("--summary-file")
    p_prep.add_argument("--date")
    p_prep.add_argument("--redo", action="store_true")
    p_prep.add_argument("--split-chars", type=int, default=DEFAULT_SPLIT_CHARS)
    p_prep.add_argument("--json", action="store_true")
    p_prep.set_defaults(func=cmd_prepare_doc)

    p_save = sub.add_parser("save-doc", help="Save Feishu create result into doc.json and episodes.json")
    p_save.add_argument("--workspace-dir", default=".")
    p_save.add_argument("--video", required=True)
    p_save.add_argument("--feishu-return", required=True)
    p_save.add_argument("--zh-title")
    p_save.add_argument("--total-parts", type=int, default=1)
    p_save.add_argument("--part", type=int, default=1)
    p_save.add_argument("--body-incomplete", action="store_true")
    p_save.add_argument("--json", action="store_true")
    p_save.set_defaults(func=cmd_save_doc)

    p_chunk = sub.add_parser("save-chunk", help="Record an appended chunk for an incomplete Feishu doc")
    p_chunk.add_argument("--workspace-dir", default=".")
    p_chunk.add_argument("--video", required=True)
    p_chunk.add_argument("--part", type=int, required=True)
    p_chunk.add_argument("--total-parts", type=int, required=True)
    p_chunk.add_argument("--document-id")
    p_chunk.add_argument("--doc-url")
    p_chunk.add_argument("--json", action="store_true")
    p_chunk.set_defaults(func=cmd_save_chunk)

    p_fail = sub.add_parser("record-failure", help="Record a failed Feishu doc creation attempt")
    p_fail.add_argument("--workspace-dir", default=".")
    p_fail.add_argument("--video", required=True)
    p_fail.add_argument("--error", required=True)
    p_fail.add_argument("--json", action="store_true")
    p_fail.set_defaults(func=cmd_record_failure)

    p_pend = sub.add_parser(
        "save-pending-report",
        help="Persist pending summary payload into pending_report.json before automation_report",
    )
    p_pend.add_argument("--workspace-dir", default=".")
    p_pend.add_argument("--report-run-id", required=True)
    p_pend.add_argument("--text")
    p_pend.add_argument("--text-file")
    p_pend.add_argument("--success-video-ids", nargs="*")
    p_pend.add_argument("--failed-video-ids", nargs="*")
    p_pend.add_argument("--empty-date")
    p_pend.add_argument("--date")
    p_pend.add_argument("--json", action="store_true")
    p_pend.set_defaults(func=cmd_save_pending_report)

    p_abn = sub.add_parser(
        "abandon-pending-report",
        help="Move expired prior-day pending_report.json to reported.json abandoned without marking episodes",
    )
    p_abn.add_argument("--workspace-dir", default=".")
    p_abn.add_argument("--report-run-id", required=True)
    p_abn.add_argument("--date")
    p_abn.add_argument("--json", action="store_true")
    p_abn.set_defaults(func=cmd_abandon_pending_report)

    p_rep = sub.add_parser("mark-reported", help="Persist reported episode IDs or empty week into reported.json")
    p_rep.add_argument("--workspace-dir", default=".")
    p_rep.add_argument("--report-run-id", required=True)
    p_rep.add_argument("--success-video-ids", nargs="*")
    p_rep.add_argument("--failed-video-ids", nargs="*")
    p_rep.add_argument("--empty-date")
    p_rep.add_argument("--json", action="store_true")
    p_rep.set_defaults(func=cmd_mark_reported)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
