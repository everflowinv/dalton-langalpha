"""Hide harness thinking markers that open an assistant message.

The checkpoint keeps the original text. Live frames, checkpoint replay,
and task cards all ask this module what to show. Only a message that
opens with one known marker is changed:

- ``<thinking_mode>…</thinking_mode>``
- ``<thinking>…</thinking>``
- a paragraph that starts with ``NEED MORE THINKING``

Leading whitespace is ignored and the match is case-insensitive. A tag
or the phrase anywhere later in the message stays. A tool call after an
opening marker hides the whole message. A final reply drops only that
opening tag, or only the opening paragraph through its first blank line.

While the opening is still undecided, at most ``OPENING_HOLD_LIMIT``
characters are buffered. Past that limit an unclosed tag is ordinary
text and is shown unchanged. After the opening is decided, later chunks
are not scanned.

A task card cannot see a later tool call, so it uses the final-reply
branch of the same function, decided once at the start of the message.

# DALTON-HOOK(thinking-leak): display filter only; the checkpoint is not rewritten.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# Case-insensitive openings. Longer tag openings are listed first so
# ``<thinking_mode>`` is not classified as ``<thinking>``.
THINKING_LEAK_MARKERS: tuple[str, ...] = (
    "<thinking_mode>",
    "need more thinking",
    "<thinking>",
)

# ``thinking_mode`` is first so its closing tag is not read as ``</thinking>``.
THINKING_LEAK_TAGS: tuple[str, ...] = ("thinking_mode", "thinking")

# Opening classification buffers at most this many characters. Past it,
# an opening that is still unfinished is not a marker.
OPENING_HOLD_LIMIT = 2000

_NEED_PHRASE = next(
    marker for marker in THINKING_LEAK_MARKERS if not marker.startswith("<")
)
_OPEN_TAG = re.compile(
    r"<\s*(thinking_mode|thinking)\s*>",
    re.IGNORECASE,
)
_CLOSERS = {
    name: re.compile(rf"<\s*/\s*{name}\s*>", re.IGNORECASE)
    for name in THINKING_LEAK_TAGS
}
# A blank line is a newline, optional spaces, tabs, or CR, and another
# newline. ``\r`` lets a CRLF break (``\r\n\r\n``) close the paragraph.
_BLANK_LINE = re.compile(r"\n[ \t\r]*\n")


class _Opening(NamedTuple):
    """A decided opening marker. ``end`` is the index after the part to drop."""

    kind: str
    end: int


def decide_opening(text: str) -> _Opening | None:
    """The opening marker in ``text``, or None.

    The decision looks at the first ``OPENING_HOLD_LIMIT`` characters
    only. A tag that has not closed inside that window is not a marker.
    A ``NEED MORE THINKING`` phrase inside the window is a marker; its
    paragraph then runs to the first blank line in the full text, or to
    the end when there is no blank line.
    """
    if not text:
        return None
    window = text[:OPENING_HOLD_LIMIT]
    start = 0
    while start < len(window) and window[start].isspace():
        start += 1
    if start >= len(window):
        return None
    if window[start:].lower().startswith(_NEED_PHRASE):
        return _Opening("need", _need_paragraph_end(text, start))
    tag_end = _closed_tag_end(text, start)
    if tag_end is None:
        return None
    return _Opening("tag", tag_end)


def opening_status(text: str) -> str:
    """``marker``, ``hold``, or ``plain``.

    ``hold`` means the first ``OPENING_HOLD_LIMIT`` characters could
    still grow into one opening marker. Past that limit an unfinished
    opening is ``plain``.
    """
    if decide_opening(text) is not None:
        return "marker"
    if len(text) > OPENING_HOLD_LIMIT:
        return "plain"
    if _could_still_open(text):
        return "hold"
    return "plain"


def visible_assistant_text(text: str, *, followed_by_tool_call: bool) -> str:
    """What of one assistant message to show.

    Live settle and checkpoint replay both use this. A tool call after
    an opening marker hides the message. Any other message is shown
    unchanged, except a final reply that opens with a marker: only that
    opening tag or opening paragraph is removed.
    """
    if not text:
        return text
    opening = decide_opening(text)
    if opening is None:
        return text
    if followed_by_tool_call:
        return ""
    return text[opening.end :]


def visible_card_text(text: str) -> str:
    """What a task card shows.

    A card cannot see a later tool call, so this is the final-reply
    branch of ``visible_assistant_text``.
    """
    if not text:
        return text
    return visible_assistant_text(text, followed_by_tool_call=False)


def _need_paragraph_end(text: str, content_start: int) -> int:
    """Index after the opening paragraph and the blank line that ends it."""
    match = _BLANK_LINE.search(text, content_start)
    if match is None:
        return len(text)
    return match.end()


def _closed_tag_end(text: str, start: int) -> int | None:
    """Index after a complete opening tag, if its closer ends inside the window."""
    if start >= OPENING_HOLD_LIMIT:
        return None
    match = _OPEN_TAG.match(text, start)
    if match is None or match.end() > OPENING_HOLD_LIMIT:
        return None
    name = match.group(1).lower()
    closer = _CLOSERS[name].search(text, match.end(), OPENING_HOLD_LIMIT)
    if closer is None:
        return None
    return closer.end()


def _could_still_open(text: str) -> bool:
    """Whether ``text`` is whitespace or an unfinished opening marker."""
    index = 0
    size = len(text)
    while index < size and text[index].isspace():
        index += 1
    if index >= size:
        return True
    if text[index] != "<":
        return _NEED_PHRASE.startswith(text[index:].lower())
    return _tag_could_still_open(text, index)


def _tag_could_still_open(text: str, start: int) -> bool:
    """Whether ``text[start:]`` begins with ``<`` and can still become a tag."""
    index = start + 1
    size = len(text)
    if index >= size:
        return True
    while index < size and text[index].isspace():
        index += 1
    if index >= size:
        return True
    if text[index] == "/":
        return False
    name_start = index
    while index < size and (text[index].isalpha() or text[index] == "_"):
        index += 1
    name = text[name_start:index].lower()
    name_ended = index < size
    if not name_ended:
        return any(tag.startswith(name) for tag in THINKING_LEAK_TAGS)
    if name not in THINKING_LEAK_TAGS:
        return False
    while index < size and text[index].isspace():
        index += 1
    if index >= size:
        return True
    # The opener is complete and the closer is not (the caller already
    # asked ``decide_opening``). More text can still close it.
    return text[index] == ">"


def _ends_in_open_blank(text: str) -> bool:
    """Whether ``text`` ends in a newline that can still become a blank line.

    Only call this when ``_BLANK_LINE`` does not match. The result is the
    whole tail the card keeps: a newline followed by nothing but spaces,
    tabs, and CR.
    """
    newline = text.rfind("\n")
    if newline < 0:
        return False
    return all(char in " \t\r" for char in text[newline + 1 :])


class CardTextFilter:
    """Streaming form of ``visible_card_text`` for one task-card message.

    The opening is classified once. Later chunks pass through. An opening
    ``NEED MORE THINKING`` paragraph is withheld until its blank line, or
    dropped at ``flush`` when the message ends inside that paragraph.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._phase = "opening"
        # True after a newline while only spaces, tabs, and CR have followed.
        self._blank = False

    def feed(self, text: str) -> str | None:
        if not text:
            return None
        if self._phase == "passthrough":
            return text
        if self._phase == "need":
            return self._feed_need(text)
        self._buf += text
        status = opening_status(self._buf)
        if status == "hold":
            return None
        opening = decide_opening(self._buf) if status == "marker" else None
        if opening is not None and opening.kind == "need":
            # A blank line already in this chunk, including one that ends
            # the chunk, is closed. Show the text after it (empty, when
            # the chunk ends on that line) and pass later chunks through.
            if _BLANK_LINE.search(self._buf) is not None:
                shown = self._buf[opening.end :]
                self._buf = ""
                self._phase = "passthrough"
                return shown or None
            self._phase = "need"
            self._blank = _ends_in_open_blank(self._buf)
            self._buf = ""
            return None
        if opening is not None:
            shown = self._buf[opening.end :]
        else:
            shown = self._buf
        self._buf = ""
        self._phase = "passthrough"
        return shown or None

    def _feed_need(self, text: str) -> str | None:
        """Drop the rest of the opening paragraph.

        ``_blank`` is the only state kept between chunks. This walk looks
        at ``text`` once and does not copy the paragraph.
        """
        seen_newline = self._blank
        for index, char in enumerate(text):
            if seen_newline:
                if char in " \t\r":
                    continue
                if char == "\n":
                    self._phase = "passthrough"
                    self._blank = False
                    return text[index + 1 :] or None
                seen_newline = False
                continue
            if char == "\n":
                seen_newline = True
        self._blank = seen_newline
        return None

    def flush(self) -> str | None:
        if self._phase == "need":
            self._phase = "passthrough"
            self._buf = ""
            self._blank = False
            return None
        if self._phase == "opening" and self._buf:
            shown = visible_assistant_text(self._buf, followed_by_tool_call=False)
            self._buf = ""
            self._phase = "passthrough"
            return shown or None
        return None


class ThinkingLeakGate:
    """Per-message hold for a live stream that learns about tool calls later.

    ``feed`` returns text that is safe to show now. ``None`` means hold or
    drop. The opening is classified once, from at most
    ``OPENING_HOLD_LIMIT`` characters. After that, a plain message passes
    through. A marker-prefixed message is kept as chunks, not scanned,
    and joined once when it is released.

    Held text is released at these moments:

    - ``settle`` when the same message finishes or a tool call is known
      (a marker followed by a tool call stays hidden; a finish uses
      ``visible_assistant_text``);
    - ``release_except`` when a later message starts and this one never
      received finish metadata;
    - ``release_all`` when the turn is interrupted or the stream ends
      with the message still open.

    Cancel and timeout do not flush. What may be dropped then is an
    opening prefix that is not yet decided, or the whole body of a
    message already decided to open with a marker. Replay shows that
    text from the checkpoint with the same rule. A body already dropped
    because a tool call followed stays dropped.
    """

    def __init__(self) -> None:
        # Undecided opening only. Capped by ``OPENING_HOLD_LIMIT``.
        self._buf: dict[str, str] = {}
        # Body held after the opening is a marker, one piece per chunk.
        self._parts: dict[str, list[str]] = {}
        self._mode: dict[str, str] = {}

    def seen(self, message_id: str) -> bool:
        return (
            message_id in self._mode
            or message_id in self._buf
            or message_id in self._parts
        )

    def pending(self, message_id: str) -> bool:
        """Text is held and has not been shown yet."""
        return bool(self._buf.get(message_id)) or bool(self._parts.get(message_id))

    def _pending_ids(self) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()
        for message_id in (*self._buf, *self._parts):
            if message_id not in seen and self.pending(message_id):
                seen.add(message_id)
                ids.append(message_id)
        return ids

    def release_except(self, message_id: str) -> list[tuple[str, str]]:
        """Final-reply flush of every other message that still holds text."""
        released: list[tuple[str, str]] = []
        for mid in self._pending_ids():
            if mid == message_id:
                continue
            text = self.settle(mid, tool_call=False, already_visible=None)
            if text:
                released.append((mid, text))
        return released

    def release_all(self) -> list[tuple[str, str]]:
        """Final-reply flush of every message that still holds text."""
        released: list[tuple[str, str]] = []
        for mid in self._pending_ids():
            text = self.settle(mid, tool_call=False, already_visible=None)
            if text:
                released.append((mid, text))
        return released

    def feed(self, message_id: str, text: str) -> str | None:
        if not text:
            return None
        mode = self._mode.get(message_id)
        if mode == "drop":
            return None
        if mode == "plain":
            return text
        if mode == "marker":
            self._parts[message_id].append(text)
            return None
        buf = self._buf.get(message_id, "") + text
        status = opening_status(buf)
        if status == "hold":
            self._mode[message_id] = "hold"
            self._buf[message_id] = buf
            return None
        if status == "marker":
            self._mode[message_id] = "marker"
            self._buf.pop(message_id, None)
            self._parts[message_id] = [buf]
            return None
        self._mode[message_id] = "plain"
        self._buf.pop(message_id, None)
        return buf or None

    def settle(
        self,
        message_id: str,
        *,
        tool_call: bool,
        already_visible: str | None,
    ) -> str | None:
        """The text this frame should carry after the message resolves.

        ``already_visible`` is text this same frame already attached.
        Text shown on an earlier frame is not passed back in. A plain
        message is returned as already attached: it is not classified
        again.
        """
        mode = self._mode.get(message_id)
        if (
            mode is None
            and message_id not in self._buf
            and message_id not in self._parts
            and not already_visible
        ):
            return None
        if mode == "drop":
            self._buf.pop(message_id, None)
            self._parts.pop(message_id, None)
            return None
        if mode == "plain":
            self._buf.pop(message_id, None)
            self._parts.pop(message_id, None)
            return already_visible or None
        parts = self._parts.pop(message_id, None)
        held = "".join(parts) if parts else self._buf.pop(message_id, "")
        self._buf.pop(message_id, None)
        source = f"{already_visible or ''}{held}"
        if not source:
            if mode is not None:
                self._mode[message_id] = "plain"
            return None
        visible = visible_assistant_text(source, followed_by_tool_call=tool_call)
        if tool_call and not visible:
            self._mode[message_id] = "drop"
            return None
        self._mode[message_id] = "plain"
        return visible or None
