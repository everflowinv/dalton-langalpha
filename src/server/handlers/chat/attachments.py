"""Turning a turn's attachments into something the model can act on.

Every file the user attached takes one of three routes, decided by what this
turn's model accepts: a native content block, a sandbox path the agent opens
with Python, or a note saying the file exists and cannot be read.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from ptc_agent.agent.middleware.file_operations.image_fit import usable_image
from ptc_agent.core.project_context import ProjectContext

from src.llms.llm import get_image_types, get_input_modalities, image_route
from src.server.models.chat_attachment import (
    NATIVE_TURN_MAX_BYTES,
    InlineSource,
    TurnAttachment,
    TurnAttachments,
)
from src.server.services.chat_attachment_turns import (
    pull_attachments,
    read_for_model,
    write_unsaved,
)
from src.server.utils.multimodal_context import (
    INLINE_UNSAVED_PDF_MAX_BYTES,  # DALTON-HOOK(attachment-native-cap)
    build_file_reminder,
    build_unsupported_reminder,
    data_url_decoded_size,  # DALTON-HOOK(attachment-native-cap)
    file_note,
    inline_attachment,
    inject_multimodal_context,
    missing_note,
    parse_multimodal_contexts,
    pdf_page_count,
    route_for,
    upload_rel_path,
    wants_bytes,
)

from .request_prep import _append_to_last_user_message, logger


def _modalities(effective_model: str | None, config: Any) -> list[str]:
    """What this turn's model accepts natively, text alone when it is unresolved."""
    return (
        get_input_modalities(
            effective_model, custom_modalities=config.input_modalities
        )
        if effective_model
        else ["text"]
    )


def _accepted_images(effective_model: str | None, config: Any) -> frozenset[str]:
    """The image types the route being called takes, read off its client."""
    client = getattr(config, "llm_client", None)
    return get_image_types(
        image_route(getattr(client, "metadata", None) or {"manifest_model": effective_model})
    )


def _check(
    read: list[tuple[TurnAttachment, bytes | None]],
    modalities: list[str],
    accepted: frozenset[str],
) -> None:
    """Route the files read for the model by what their bytes allow, and count
    a PDF's pages.

    Blocking, since each file is decoded in full, and in one thread for the
    lot: a thread per file would take as many of the shared executor's
    workers as a request has files.
    """
    for att, raw in read:
        if raw is not None and att.kind == "pdf":
            att.pages = pdf_page_count(raw)
        readable = raw is not None and (att.kind != "image" or usable_image(raw, accepted))
        att.route = route_for(att, modalities, readable=readable)


async def upload_to_sandbox(
    multimodal_contexts: list[Any],
    sandbox: Any,
    *,
    project: ProjectContext | None = None,
) -> list[str | None]:
    """Upload inline MultimodalContext items to sandbox, returning saved paths or None."""
    results: list[str | None] = []
    for ctx in multimodal_contexts:
        data = ctx.data or ""
        header, sep, payload = data.partition(",")
        if not sep:
            results.append(None)
            continue
        mime = header.split(":")[1].split(";")[0] if ":" in header else ""
        try:
            raw = base64.b64decode(payload)
        except Exception:
            results.append(None)
            continue
        rel_path = upload_rel_path(ctx.description or "file", mime)
        try:
            abs_path = sandbox.normalize_path(rel_path, project=project)
            written = await sandbox.aupload_file_bytes(abs_path, raw)
        except Exception:
            written = False
        results.append(rel_path if written else None)
    return results


def _legacy_file_note(ctx: Any, fpath: str | None, *, inline: bool = False) -> str:
    """Explicit save-failure / workspace-path wording expected by attachment-native-cap."""
    desc = ctx.description or "file"
    data = ctx.data or ""
    mime = data.split(":")[1].split(";")[0] if ":" in data else "unknown"
    if not fpath:
        if inline:
            return (
                f"The user attached a file ({desc}, {mime}). It is shown "
                "inline above, but saving it to the workspace failed, so "
                "it cannot be processed with Python or read here later. "
                "Tell the user to upload it again from the file panel if "
                "the file itself is needed."
            )
        return (
            f"The user attached a file ({desc}, {mime}), but saving it "
            "to the workspace failed, so it cannot be read here. Tell the "
            "user to upload it again from the file panel."
        )
    return (
        f"The user attached a file ({desc}, {mime}). "
        f"It has been saved to {fpath}. "
        f"Use Python to process it."
    )


async def _materialize_chat_uploads_for_turn(
    raw_contexts: list[Any] | None,
    messages: list[dict],
    session: Any | None,
    *,
    project: ProjectContext | None,
    user_id: str | None,
    workspace_id: str | None,
    is_existing_colleague_thread: bool,
) -> list[dict]:
    """DALTON-HOOK(chat-large-upload): materialize server-stored chat_upload items into sandbox."""
    from src.dalton.services.chat_uploads import (
        extract_chat_upload_contexts,
        materialize_chat_uploads,
    )

    chat_upload_contexts = extract_chat_upload_contexts(raw_contexts)
    if not chat_upload_contexts:
        return messages
    resolved_ws = workspace_id or (
        str(project.workspace_id) if project and project.workspace_id else None
    )
    upload_notes = await materialize_chat_uploads(
        chat_upload_contexts,
        sandbox=session.sandbox if session else None,
        user_id=user_id,
        workspace_id=resolved_ws,
        project=project,
        is_existing_colleague_thread=is_existing_colleague_thread,
    )
    if upload_notes:
        _append_to_last_user_message(messages, build_file_reminder(upload_notes))
    return messages


async def _attach_legacy_chat_request(
    messages: list[dict],
    request: Any,
    session: Any | None,
    effective_model: str | None,
    config: Any,
    *,
    project: ProjectContext | None,
    user_id: str | None,
    workspace_id: str | None,
    is_existing_colleague_thread: bool,
) -> list[dict]:
    """Compatibility path for legacy ChatRequest callers (DALTON-HOOK(attachment-native-cap), DALTON-HOOK(chat-large-upload))."""
    if getattr(request, "hitl_response", None):
        return messages
    raw_contexts = getattr(request, "additional_context", None)
    multimodal_contexts = parse_multimodal_contexts(raw_contexts)
    if not multimodal_contexts:
        return await _materialize_chat_uploads_for_turn(
            raw_contexts,
            messages,
            session,
            project=project,
            user_id=user_id,
            workspace_id=workspace_id,
            is_existing_colleague_thread=is_existing_colleague_thread,
        )

    file_paths: list[str | None] = []
    if session and session.sandbox:
        file_paths = await upload_to_sandbox(
            multimodal_contexts, session.sandbox, project=project
        )
        logger.info(
            f"[PTC_CHAT] Uploaded {len(multimodal_contexts)} attachment(s) to sandbox"
        )

    modalities = _modalities(effective_model, config)
    accepted = _accepted_images(effective_model, config)

    # DALTON-HOOK(attachment-native-cap): route inline items through TurnAttachment
    # routing while keeping unsaved PDF ceiling and explicit save-failure wording.
    atts: list[TurnAttachment | None] = []
    for idx, ctx in enumerate(multimodal_contexts):
        att = inline_attachment(ctx) if ctx.data else None
        if att is None:
            atts.append(None)
            continue
        if file_paths and idx < len(file_paths):
            att.path = file_paths[idx]
        if wants_bytes(att, modalities):
            raw = att.source.data if isinstance(att.source, InlineSource) else None
            if raw is not None and att.kind == "pdf":
                att.pages = pdf_page_count(raw)
            readable = raw is not None and (
                att.kind != "image" or usable_image(raw, accepted)
            )
            att.route = route_for(att, modalities, readable=readable)
        else:
            att.route = route_for(att, modalities)
        data_str = ctx.data or ""
        if (
            att.route == "native"
            and data_str.startswith("data:application/pdf")
            and not att.path
            and data_url_decoded_size(data_str) > INLINE_UNSAVED_PDF_MAX_BYTES
        ):
            att.route = "file"
        atts.append(att)

    native_atts = [a for a in atts if a is not None and a.route == "native"]
    if native_atts:
        messages = inject_multimodal_context(messages, native_atts)
        unsaved_inline = [
            (ctx, file_paths[i] if file_paths and i < len(file_paths) else None)
            for i, (ctx, att) in enumerate(zip(multimodal_contexts, atts))
            if att is not None and att.route == "native" and not att.path
        ]
        if unsaved_inline:
            _append_to_last_user_message(
                messages,
                build_file_reminder(
                    [_legacy_file_note(ctx, fp, inline=True) for ctx, fp in unsaved_inline]
                ),
            )

    unsupported_notes = [
        _legacy_file_note(ctx, file_paths[i] if file_paths and i < len(file_paths) else None)
        for i, (ctx, att) in enumerate(zip(multimodal_contexts, atts))
        if att is not None and att.route == "unsupported"
    ]
    if unsupported_notes:
        _append_to_last_user_message(
            messages, build_unsupported_reminder(unsupported_notes)
        )

    file_notes = [
        _legacy_file_note(ctx, file_paths[i] if file_paths and i < len(file_paths) else None)
        for i, (ctx, att) in enumerate(zip(multimodal_contexts, atts))
        if att is None or att.route == "file"
    ]
    if file_notes:
        _append_to_last_user_message(messages, build_file_reminder(file_notes))

    return await _materialize_chat_uploads_for_turn(
        raw_contexts,
        messages,
        session,
        project=project,
        user_id=user_id,
        workspace_id=workspace_id,
        is_existing_colleague_thread=is_existing_colleague_thread,
    )


async def attach_request_files(
    messages: list[dict],
    attachments: TurnAttachments | Any,
    session: Any | None,
    effective_model: str | None,
    config: Any,
    *,
    project: ProjectContext | None = None,
    # DALTON-HOOK(chat-large-upload): caller and workspace identity for verified chat_upload materialization
    user_id: str | None = None,
    workspace_id: str | None = None,
    is_existing_colleague_thread: bool = False,
    additional_context: list[Any] | None = None,
) -> list[dict]:
    """Save the turn's attachments to the sandbox and describe them to the PTC agent.

    All attachments are saved to sandbox (when available) so the agent always
    has file access. Images the model accepts also go in as native content
    blocks; a PDF it accepts goes in as a note with its page count once it is
    saved, and inline only when it is not. ``attachments`` are the request's,
    resolved for its user (``resolve_turn_attachments``).
    """
    if not isinstance(attachments, TurnAttachments):
        return await _attach_legacy_chat_request(
            messages,
            attachments,
            session,
            effective_model,
            config,
            project=project,
            user_id=user_id,
            workspace_id=workspace_id,
            is_existing_colleague_thread=is_existing_colleague_thread,
        )

    files = attachments.files
    if files:
        await _route_and_save(files, session, effective_model, config, project)
        # DALTON-HOOK(attachment-native-cap): an unsaved PDF over the inline
        # ceiling is never inlined whole across turns in the checkpoint.
        for att in files:
            if (
                att.kind == "pdf"
                and att.route == "native"
                and not att.path
                and att.size > INLINE_UNSAVED_PDF_MAX_BYTES
            ):
                att.route = "file"
        messages = inject_multimodal_context(messages, files)
        for att in files:
            att.release()
        native = sum(1 for a in files if a.route == "native")
        if native:
            logger.info(f"[PTC_CHAT] Multimodal context injected: {native} supported attachment(s)")
        unsupported = [file_note(a) for a in files if a.route == "unsupported"]
        if unsupported:
            _append_to_last_user_message(messages, build_unsupported_reminder(unsupported))
        file_only = [file_note(a) for a in files if a.route == "file"]
        if file_only:
            _append_to_last_user_message(messages, build_file_reminder(file_only))
            logger.info(
                f"[PTC_CHAT] {len(file_only)} file-only attachment(s) for {effective_model}"
            )
    if attachments.missing:
        _append_to_last_user_message(
            messages, build_file_reminder([missing_note(m) for m in attachments.missing])
        )
    # DALTON-HOOK(chat-large-upload): materialize chunked chat_upload items into sandbox/multimodal context
    if additional_context:
        messages = await _materialize_chat_uploads_for_turn(
            additional_context,
            messages,
            session,
            project=project,
            user_id=user_id,
            workspace_id=workspace_id,
            is_existing_colleague_thread=is_existing_colleague_thread,
        )
    return messages


async def _route_and_save(
    files: list[TurnAttachment],
    session: Any | None,
    effective_model: str | None,
    config: Any,
    project: ProjectContext | None,
) -> None:
    """Set each file's route and, with a sandbox, its saved path.

    The files whose route depends on their bytes are read and kept, up to
    the turn's budget for content blocks, while the sandbox fetches the
    uploaded ones from the store. The rest are then written from here, which
    the kept bytes save a second read for, beside the check.
    """
    modalities = _modalities(effective_model, config)
    for att in files:
        att.route = route_for(att, modalities)
    wanted: list[TurnAttachment] = []
    budget = NATIVE_TURN_MAX_BYTES
    for att in files:
        if not wants_bytes(att, modalities):
            continue
        if att.size > budget:
            att.route = "file"
            continue
        budget -= att.size
        wanted.append(att)
    reading = asyncio.gather(*(read_for_model(a) for a in wanted))
    sandbox = session.sandbox if session else None
    if sandbox is None:
        read = await reading
    else:
        read, _ = await asyncio.gather(
            reading, pull_attachments(files, sandbox, project=project)
        )
    checking = asyncio.to_thread(
        _check, list(zip(wanted, read)), modalities, _accepted_images(effective_model, config)
    )
    if sandbox is None:
        await checking
        return
    await asyncio.gather(write_unsaved(files, sandbox, project=project), checking)
    logger.info(f"[PTC_CHAT] Saved {sum(1 for a in files if a.path)} of {len(files)} attachment(s) to sandbox")
