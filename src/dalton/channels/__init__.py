"""Dalton channel-delivery helpers (deliverable-text contract and notices)."""

from src.dalton.channels.deliverable_text import (
    AUTOMATION_EMPTY_AFTER_TOOLS_SUMMARY,
    AUTOMATION_FILES_ONLY_EXCERPT,
    CHANNEL_EMPTY_AFTER_TOOLS_NOTICE,
    CHANNEL_SEND_FAILED_AFTER_TOOLS_NOTICE,
    CONTINUED_FROM_KWARG,
    RETRIED_ATTEMPT_KWARG,
    DeliverableExtractionResult,
    DeliverableTextAccumulator,
    build_channel_empty_delivery_notice,
    extract_deliverable_text_from_events,
    extract_deliverable_text_from_messages,
    resolve_automation_empty_excerpt,
)

__all__ = [
    "AUTOMATION_EMPTY_AFTER_TOOLS_SUMMARY",
    "AUTOMATION_FILES_ONLY_EXCERPT",
    "CHANNEL_EMPTY_AFTER_TOOLS_NOTICE",
    "CHANNEL_SEND_FAILED_AFTER_TOOLS_NOTICE",
    "CONTINUED_FROM_KWARG",
    "RETRIED_ATTEMPT_KWARG",
    "DeliverableExtractionResult",
    "DeliverableTextAccumulator",
    "build_channel_empty_delivery_notice",
    "extract_deliverable_text_from_events",
    "extract_deliverable_text_from_messages",
    "resolve_automation_empty_excerpt",
]
