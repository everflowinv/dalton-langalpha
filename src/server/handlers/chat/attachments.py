"""Turning a turn's attachments into something the model can act on.

Every file the user attached takes one of three routes, decided by what this
turn's model accepts: a native content block, a sandbox path the agent opens
with Python, or a note saying the file exists and cannot be read.
"""

from __future__ import annotations

from typing import Any

from ptc_agent.core.project_context import ProjectContext

from src.llms.llm import get_input_modalities
from src.server.models.chat import ChatRequest
from src.server.utils.multimodal_context import (
    build_file_reminder,
    build_unsupported_reminder,
    filter_multimodal_by_capability,
    inject_multimodal_context,
    parse_multimodal_contexts,
    upload_to_sandbox,
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


async def attach_request_files(
    messages: list[dict],
    request: ChatRequest,
    session: Any | None,
    effective_model: str | None,
    config: Any,
    *,
    project: ProjectContext | None = None,
) -> list[dict]:
    """Upload the turn's attachments and describe them to the PTC agent.

    All attachments are uploaded to sandbox (when available) so the agent always
    has file access. Model-supported modalities also get native content blocks
    merged into the user message.
    """
    multimodal_contexts = parse_multimodal_contexts(request.additional_context)
    if not multimodal_contexts or request.hitl_response:
        return messages

    # 1. Upload ALL files to sandbox
    file_paths: list = []
    if session and session.sandbox:
        file_paths = await upload_to_sandbox(
            multimodal_contexts, session.sandbox, project=project
        )
        logger.info(
            f"[PTC_CHAT] Uploaded {len(multimodal_contexts)} attachment(s) to sandbox"
        )

    # 2. Filter by model capability for native content blocks
    modalities = _modalities(effective_model, config)
    supported, unsupported, file_only = filter_multimodal_by_capability(
        multimodal_contexts, modalities
    )
    # DALTON-HOOK(attachment-native-cap): an image or PDF above what a provider
    # takes inline rides the sandbox path instead; inlined, it stays in the
    # thread's history and fails every later turn on the request size.
    from src.dalton.attachments import split_oversized

    supported, oversized = split_oversized(supported)
    file_only = file_only + oversized

    # 3. Inject supported as native content blocks (merged into user message)
    if supported:
        supported_paths = [
            file_paths[i]
            for i, ctx in enumerate(multimodal_contexts)
            if ctx in supported
        ] if file_paths else None
        messages = inject_multimodal_context(
            messages, supported, file_paths=supported_paths
        )
        logger.info(
            f"[PTC_CHAT] Multimodal context injected: "
            f"{len(supported)} supported attachment(s)"
        )

    # Helper to build per-file path notes
    # DALTON-HOOK(attachment-native-cap): oversized or unsaved attachments get
    # an explicit note — never a bare filename the model would ignore.
    def _file_note(ctx, idx, *, oversized_ctx: bool = False):
        desc = ctx.description or "file"
        data = ctx.data
        mime = data.split(":")[1].split(";")[0] if ":" in data else "unknown"
        fpath = file_paths[idx] if file_paths and idx < len(file_paths) else None
        if not fpath:
            # Never just a bare filename: the model must say so and point the
            # user at the file panel, not silently drop the attachment.
            if oversized_ctx:
                why = "It exceeds the size that can be viewed inline"
            else:
                why = "This model cannot view it natively"
            return (
                f"The user attached a file ({desc}, {mime}). {why}, and saving it "
                "to the workspace failed, so it cannot be read here. Tell the "
                "user to upload it again from the file panel."
            )
        note = f"The user attached a file ({desc}, {mime}). It has been saved to {fpath}."
        if oversized_ctx and mime.startswith("image/"):
            return note + (
                " The image is over 5 MB — the Read tool cannot open it; shrink "
                "it below 5 MB with Python (PIL) first, then view it with Read."
            )
        if oversized_ctx and mime == "application/pdf":
            return note + (
                " The PDF is over 5 MB — the Read tool cannot open it; extract "
                "its text with Python (pypdf / pdfplumber), or split it into "
                "< 5 MB page files / render pages to images before viewing with Read."
            )
        return note + " Use Python to process it."

    # 4. Unsupported image/PDF: "cannot view" warning + file paths
    # DALTON-HOOK(attachment-native-cap): the explicit-note contract above.
    if unsupported:
        notes = [
            _file_note(ctx, i)
            for i, ctx in enumerate(multimodal_contexts)
            if ctx in unsupported
        ]
        _append_to_last_user_message(messages, build_unsupported_reminder(notes))

    # 5. File-only (xlsx, csv, etc.) + oversized image/PDF: path notes only
    # DALTON-HOOK(attachment-native-cap): oversized ones get their reading path.
    if file_only:
        notes = [
            _file_note(ctx, i, oversized_ctx=ctx in oversized)
            for i, ctx in enumerate(multimodal_contexts)
            if ctx in file_only
        ]
        _append_to_last_user_message(messages, build_file_reminder(notes))
        logger.info(
            f"[PTC_CHAT] {len(file_only)} file-only attachment(s) "
            f"uploaded to sandbox for {effective_model}"
        )

    return messages

