"""Read-side half of multimodal support: what the target model can still see.

An image or PDF block is written into graph state once, by the tool call that
produced it, and then replayed on every later turn. The model reading it is not
the one that wrote it — a user can switch mid-thread, a resilience fallback can
land on a text-only candidate, and a subagent runs its own model entirely — so
whether a block is legal is a question only this side can answer, on the call
where the target is actually known.

Split from ``multimodal`` because the two halves share no state and not every
agent wires both: an agent with no filesystem tool has nothing to intercept,
but still inherits history full of blocks it may not be able to read.
"""

import inspect
from collections.abc import Callable, Collection
from typing import Annotated, Any, Literal

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ExtendedModelResponse, PrivateStateAttr
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command
from typing_extensions import NotRequired

from ptc_agent.agent.middleware._message_utils import order_tool_results_first
# DALTON-HOOK(attachment-native-cap): the byte-total cut's thinking handling.
from ptc_agent.agent.middleware.file_operations.image_fit import fit_images, unsaved_upload_name
from ptc_agent.agent.middleware.thinking_binding import (
    UNSENT_THINKING_KEY,
    UnsentThinkingState,
    _union,
    checked_thinking_end,
    thinking_after,
    unsent_thinking,
    without_thinking,
)
from src.llms.attachment_payload import FILE_BLOCK_TYPES, IMAGE_BLOCK_TYPES, block_type
from src.llms.llm import (
    get_image_types,
    get_input_modalities,
    get_max_image_side,
    get_max_image_side_by_count,
    get_max_pdf_pages,
    get_pdf_page_tokens,
    image_route,
)


def _placeholder_for(
    block: Any,
    has_image: bool,
    has_pdf: bool,
    max_pdf_pages: int | None,
    guidance: str = "",
) -> dict[str, str] | None:
    """The text block replacing one this target can't read, or None to keep it.

    ``guidance`` is what to do about it, and rides inside the placeholder so it
    sits next to the block it explains, on the message the model is reading;
    the caller passes it for the first placeholder of a call and nothing after.
    Appending it to the system prompt instead moved the cached prefix on every
    call where a strip fired, which rewrote the whole history behind it.
    """
    kind = block_type(block)
    tail = f" {guidance}" if guidance else ""

    # Matched on the type alone: this is the only thing standing between a
    # mid-thread switch and a 400, and failing closed costs one placeholder
    # where guessing from the payload keys costs the turn.
    if kind in IMAGE_BLOCK_TYPES and not has_image:
        return {
            "type": "text",
            "text": f"[Image attached in a prior turn, not visible to the current model.{tail}]",
        }

    if kind not in FILE_BLOCK_TYPES:
        return None

    # Same reason as "image" above: `pdf` is the only file-modality flag the
    # manifest carries, so a file block we can't classify — mime absent, null,
    # or non-PDF — is one a model without it has no way to accept.
    if not has_pdf:
        return {
            "type": "text",
            "text": f"[PDF attached in a prior turn, not visible to the current model.{tail}]",
        }

    # Over the target's page ceiling. Unstamped blocks pass: they predate the
    # stamp, and re-deriving the count would mean decoding every PDF in history
    # on every call. They keep the old failure mode; nothing that already worked
    # starts failing here.
    pages = block.get("pages")
    if max_pdf_pages is not None and isinstance(pages, int) and pages > max_pdf_pages:
        if _unsaved(block):
            name = block.get("filename") or "file"
            size = f"{pages} page{'' if pages == 1 else 's'}"
            return {
                "type": "text",
                "text": (
                    f"[PDF {name} ({size}) is over the current model's "
                    f"{max_pdf_pages}-page limit and was not sent.{tail}]"
                ),
            }
        return {
            "type": "text",
            "text": (
                f"[PDF attached in a prior turn, {pages} pages, over the current "
                f"model's {max_pdf_pages}-page limit.{tail}]"
            ),
        }
    return None


def read_sources(messages: list) -> dict[str, str]:
    """The path or URL each Read call in ``messages`` was given, by call id.

    A Read result is the only attachment that knows where its bytes came
    from, and only through its call: the image block that carries them goes
    to the provider as it is, so nothing can be stamped on it.
    """
    return {
        call["id"]: call.get("args", {}).get("file_path") or ""
        for msg in messages
        if isinstance(msg, AIMessage)
        for call in msg.tool_calls or ()
        if call.get("name") == "Read" and call.get("id")
    }


def _unsaved(block: Any) -> bool:
    """A composer PDF inlined because it could not be saved anywhere, so the
    agent has no copy to extract from and no path to give."""
    return isinstance(block, dict) and block.get("unsaved") is True


def _unsaved_image(content: list, index: int, block: Any) -> bool:
    """An uploaded image whose label names no saved path.

    Composer images do not carry ``unsaved=True``. The adjacent
    ``[Attached image: …]`` label is the mark, the same role ``unsaved``
    plays on a PDF. A label that says ``saved to`` is a real copy.
    """
    # DALTON-HOOK(dense-image-reading): unsaved images are labelled, not flagged
    if block_type(block) not in IMAGE_BLOCK_TYPES or index < 1:
        return False
    return unsaved_upload_name(content[index - 1]) is not None


def _pdf_strip_reason(block: Any, has_pdf: bool, max_pdf_pages: int | None) -> str | None:
    """Why a PDF block is replaced: ``unsupported``, ``over_pages``, or None.

    None means this block is not a PDF being stripped for either reason.
    """
    if block_type(block) not in FILE_BLOCK_TYPES:
        return None
    if not has_pdf:
        return "unsupported"
    pages = block.get("pages") if isinstance(block, dict) else None
    if max_pdf_pages is not None and isinstance(pages, int) and pages > max_pdf_pages:
        return "over_pages"
    return None


def _source(msg: Any, sources: dict[str, str]) -> str | None:
    return sources.get(msg.tool_call_id) or None if isinstance(msg, ToolMessage) else None


# DALTON-HOOK(dense-image-reading): the block kind the placeholder guidance branches on
def _content_kind(block: Any) -> str | None:
    """``image``, ``pdf``, or None for a block this strip does not replace."""
    kind = block_type(block)
    if kind in IMAGE_BLOCK_TYPES:
        return "image"
    if kind in FILE_BLOCK_TYPES:
        return "pdf"
    return None


_GUIDANCE_FIELDS = frozenset({"kind", "model_sees_images", "pdf_reason"})


def _guidance_keywords(guidance: Callable[..., Any]) -> frozenset[str]:
    """Which of kind, vision, and PDF reason this callable accepts.

    A callable that only takes the path stays on that path, so a one-argument
    caller does not raise TypeError.
    """
    try:
        params = inspect.signature(guidance).parameters
    except (TypeError, ValueError):
        return frozenset()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return _GUIDANCE_FIELDS
    return frozenset(params) & _GUIDANCE_FIELDS


def strip_unsupported_content_blocks(
    messages: list,
    has_image: bool,
    has_pdf: bool,
    max_pdf_pages: int | None = None,
    guidance: str | Callable[..., str] = "",  # DALTON-HOOK(dense-image-reading): callable may take kind/model_sees_images
    unreadable: Collection[tuple[int, int]] = (),
) -> list:
    """Replace image/file blocks this target can't read with text placeholders.

    ``unreadable`` is the positions (message, block) of images this target
    can't be given though it reads images, as ``fit_images`` reports them.

    ``max_pdf_pages`` is the target's own page ceiling (None = unbounded). A PDF
    over it is dropped here rather than refused at injection, because the ceiling
    is a property of the model that reads the block, not of the tool call that
    created it — the same document is fine on a 1M-context route and rejected on
    a 200K one, and only this side knows which one is being called.

    # DALTON-HOOK(dense-image-reading): guidance can depend on the block kind and the model modality
    ``guidance`` is one text for every block, or a callable. The callable is
    given the path or URL its Read was given (None for any other attachment).
    When it accepts them, it is also given ``kind`` (``image``, ``pdf``, or
    None), ``model_sees_images`` (this call's ``has_image`` flag, the same
    modality check that decides an image is stripped), and ``pdf_reason``
    (``unsupported``, ``over_pages``, or None).

    Returns the original list when nothing changed, and never mutates a message:
    these are checkpoint objects shared with the rest of the graph.
    """
    result: list = []
    modified = False
    sources = read_sources(messages) if callable(guidance) else {}
    # Each distinct guidance is written on the first placeholder that uses it.
    # A later block with the same wording stays bare, so the earlier text does
    # not move when another attachment arrives and the prefix stays cached.
    said: set[str] = set()
    # DALTON-HOOK(dense-image-reading): pass kind, modality, and PDF reason when accepted
    accepted = _guidance_keywords(guidance) if callable(guidance) else frozenset()

    for i, msg in enumerate(messages):
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            result.append(msg)
            continue

        source = _source(msg, sources) if callable(guidance) else None
        new_blocks = []
        changed = False
        for j, block in enumerate(content):
            if not callable(guidance):
                advice = guidance
            elif accepted:  # DALTON-HOOK(dense-image-reading): kind, modality, and PDF reason
                fields = {
                    "kind": _content_kind(block),
                    "model_sees_images": has_image,
                    "pdf_reason": _pdf_strip_reason(block, has_pdf, max_pdf_pages),
                }
                advice = guidance(source, **{key: fields[key] for key in accepted})
            else:
                advice = guidance(source)
            # Extraction advice would send the model after a copy that does
            # not exist. An unsaved image is only the adjacent label; an
            # unsaved PDF carries the flag. Either way, ask for a new upload.
            if advice and _unsaved_image(content, j, block):
                tail = _UNSAVED_IMAGE
            elif advice and _unsaved(block):
                tail = _IF_NOT
            else:
                tail = advice
            placeholder = _placeholder_for(
                block,
                has_image and (i, j) not in unreadable,
                has_pdf,
                max_pdf_pages,
                "" if tail in said else tail,
            )
            if placeholder is not None:
                changed = True
                said.add(tail)
            new_blocks.append(block if placeholder is None else placeholder)

        if not changed:
            result.append(msg)
            continue
        modified = True
        result.append(msg.model_copy(update={"content": new_blocks}))

    return result if modified else messages


# What the model should do about a block it cannot see. Rides inside the
# placeholder rather than in the system prompt, so the advice is decided on the
# call where the target is known (a fallback or a subagent is judged on its own
# model) without the cached prefix moving.
# DALTON-HOOK(dense-image-reading): unsaved blocks have no file to extract.
# The closing line only says what is missing. It does not order a Python extract.
_IF_NOT = (
    "无法取得可读内容时，说明具体缺少的内容，"
    "并请求所需页面或清晰图片；不填写无法读取的数字。"
)
# DALTON-HOOK(dense-image-reading): an unsaved image has no file to extract or crop.
_UNSAVED_IMAGE = "没有保存副本，请用户重新上传。" + _IF_NOT
_UNVERIFIED = (
    "未经原图或页面图像复核的提取结果标为“尚未复核”。" + _IF_NOT
)
# Tools unknown: do not require an operation this call may not have.
PLACEHOLDER_GUIDANCE = (
    "先使用实际可用的文本提取或 OCR（光学字符识别）工具，"
    "或交由具备看图能力的代理读取。" + _UNVERIFIED
)
_CODE_GUIDANCE = (
    "文件仍在工作区：先用 Python 提取"
    "（PDF 文本、页面渲染、OCR（光学字符识别）或图像分析）。" + _UNVERIFIED
)
_NONE_GUIDANCE = (
    "当前环境不能执行代码，也不能查看图片；请交由具备相应能力的代理读取。"
    + _IF_NOT
)
# DALTON-HOOK(dense-image-reading): an image the model cannot see is handed off, even when Read exists.
_IMAGE_NONE = (
    "当前环境不能执行代码，也不能查看图片；请交由具备看图能力的代理读取。"
    + _IF_NOT
)
# DALTON-HOOK(dense-image-reading): pick the unreadable-content guidance by the tools this call can use
_CODE_TOOLS = frozenset({"ExecuteCode", "execute_code", "Bash", "bash"})
_IMAGE_TOOLS = frozenset({"Read"})


def _tool_name(tool: Any) -> str:
    """The name on a tool object or on either dict shape a request may carry."""
    if isinstance(tool, dict):
        name = tool.get("name")
        if not name and isinstance(tool.get("function"), dict):
            name = tool["function"].get("name")
    else:
        name = getattr(tool, "name", None)
    return name if isinstance(name, str) else ""


def _read_capability(tools: Any) -> str | None:
    """``code``, ``image``, ``none``, or None when this call exposes no tool list.

    ``request.tools`` is how this middleware sees the current agent's tools.
    None means the list is absent, not empty: an empty list is ``none``.
    Code wins over Read, because a call that can run code may extract or crop.
    A call that can only Read must not be told to run Python or to crop.
    """
    if tools is None:
        return None
    names = {_tool_name(tool) for tool in tools}
    if names & _CODE_TOOLS:
        return "code"
    if names & _IMAGE_TOOLS:
        return "image"
    return "none"


def _where(located: str) -> str:
    """A workspace file is still there; a URL is only at that URL."""
    return located or "文件仍在工作区："


def _code_extract(located: str) -> str:
    """PDF or untagged block when this call can run code."""
    if located:
        return (
            f"{located}先在沙箱下载并用 Python 提取"
            f"（PDF 文本、页面渲染、OCR（光学字符识别）或图像分析）。{_UNVERIFIED}"
        )
    return _CODE_GUIDANCE


def _image_block_guidance(located: str, capability: str, sees: bool) -> str:
    """Image block. Vision and code are separate: Read cannot crop.

    A model that cannot see the image is told to hand it to an agent that
    can, whether or not this call has Read. Code may also extract text and
    crop. A model that can see, with only Read, reads the original and is
    not told to crop.
    """
    # DALTON-HOOK(dense-image-reading): image guidance follows vision and code, not the tool name alone
    if capability == "code":
        if located:
            extra = (
                "可先在沙箱下载并用 Python 提取文字（含 OCR（光学字符识别）），"
                "也可裁剪后再读。"
            )
        else:
            extra = (
                "可先用 Python 提取文字（含 OCR（光学字符识别）），"
                "也可从原图裁剪后再读。"
            )
        if sees:
            return _where(located) + extra + _UNVERIFIED
        return (
            _where(located)
            + "当前模型不能查看图片，请交由具备看图能力的代理读取。"
            + extra
            + _UNVERIFIED
        )
    if capability == "image":
        if sees:
            return (
                _where(located)
                + "直接读取原图。密集内容读不清时，请交由具备代码执行能力的代理继续读取，"
                "不要自行改动原图。"
                + _IF_NOT
            )
        if located:
            return (
                f"{located}当前环境不能执行代码下载，请交由具备看图能力的代理下载并读取。"
                + _IF_NOT
            )
        return (
            "文件仍在工作区：当前模型不能查看图片，请交由具备看图能力的代理读取。"
            "不要自行查看或改动原图。" + _IF_NOT
        )
    if capability == "none":
        return located + _IMAGE_NONE
    return located + PLACEHOLDER_GUIDANCE


def _pdf_unsupported(located: str, capability: str) -> str:
    """The model cannot accept a PDF. Do not tell it to read by page."""
    # DALTON-HOOK(dense-image-reading): unsupported PDF is a handoff, not a page read
    handoff = "当前模型不支持 PDF，请交由具备 PDF 读取能力的代理读取。"
    if capability == "code":
        return (
            _where(located)
            + handoff
            + "可先用 Python 提取文本，结果标为“尚未复核”。"
            + _IF_NOT
        )
    return (located or "") + handoff + _IF_NOT


def _pdf_over_pages(located: str, capability: str) -> str:
    """The model accepts PDFs, but this one is over its page ceiling."""
    if capability == "code":
        return (
            _where(located)
            + "这份 PDF 超过当前模型的页数上限，请分页读取需要的页面。"
            + "也可以先用 Python 提取文本，结果标为“尚未复核”。"
        )
    if capability == "image":
        return (
            _where(located)
            + "这份 PDF 超过当前模型的页数上限，请分页读取需要的页面。"
            + "不要调用当前没有的代码执行。"
            + _IF_NOT
        )
    if capability == "none":
        return (
            located
            + "这份 PDF 超过当前模型的页数上限，请交由具备 PDF 读取能力的代理分页读取。"
            + _IF_NOT
        )
    return located + PLACEHOLDER_GUIDANCE


def _pdf_block_guidance(located: str, capability: str, reason: str | None = None) -> str:
    """PDF block. Unsupported is a handoff; only an over-page PDF is read by page."""
    if reason == "unsupported":
        return _pdf_unsupported(located, capability)
    if reason == "over_pages":
        return _pdf_over_pages(located, capability)
    if capability == "code":
        return _code_extract(located)
    if capability == "image":
        return (
            _where(located)
            + "用现有的读取工具按页读取。不要调用当前没有的代码执行。"
            + _IF_NOT
        )
    if capability == "none":
        return located + _NONE_GUIDANCE
    return located + PLACEHOLDER_GUIDANCE


def placeholder_guidance(  # DALTON-HOOK(dense-image-reading): kind and vision, not the tool name alone
    source: str | None,
    capability: str | None = None,
    *,
    kind: str | None = None,
    model_sees_images: bool | None = None,
    pdf_reason: str | None = None,
) -> str:
    """The guidance for a block read from ``source``.

    A URL's bytes are fetched by the server and never written to the
    workspace, so telling the model the file is there sends it looking for
    something that does not exist.

    ``capability`` is this call's tool list: ``code`` may extract or crop,
    ``image`` may Read, ``none`` must ask an agent that can, and None (the
    list was not on the request) stays on the generic wording.

    ``kind`` is ``image`` or ``pdf`` for the block being replaced. None keeps
    the tool-only wording. ``model_sees_images`` is the modality flag for this
    call; None is not treated as able to see the image. ``pdf_reason`` is
    ``unsupported`` when the model cannot accept a PDF, ``over_pages`` when
    the file is over that model's page ceiling, and None when the caller
    does not know which.
    """
    # DALTON-HOOK(dense-image-reading): capability, block kind, and vision
    url = source if source and source.startswith(("http://", "https://")) else None
    located = f"文件在 {url}，不在工作区。" if url else ""
    # Tools unknown: do not require an operation this call may not have.
    if capability is None:
        return located + PLACEHOLDER_GUIDANCE
    if kind == "image":
        return _image_block_guidance(located, capability, model_sees_images is True)
    if kind == "pdf":
        return _pdf_block_guidance(located, capability, pdf_reason)
    if capability == "code":
        return _code_extract(located)
    if capability == "image":
        if located:
            return (
                f"{located}当前环境不能执行代码下载，请交由具备相应能力的代理下载并读取。"
                + _IF_NOT
            )
        return (
            "文件仍在工作区：若当前模型支持看图，直接读取原图；"
            "否则请交由具备看图能力的代理读取。"
            "不要调用当前没有的代码执行。" + _IF_NOT
        )
    if capability == "none":
        return located + _NONE_GUIDANCE
    return located + PLACEHOLDER_GUIDANCE


def _removed_placeholder(
    block: Any, name: str | None, pages: int | None, unsaved_image: str | None = None
) -> dict[str, str] | None:
    """The text standing in for an attachment taken out to fit the context
    window, or None for a block that is not one. ``unsaved_image`` names an
    uploaded image whose label says no copy of it was saved."""
    kind = block_type(block)
    if kind in IMAGE_BLOCK_TYPES:
        if unsaved_image is not None:
            # Nothing to Read: the block was its only copy.
            return {
                "type": "text",
                "text": (
                    f"[Image {unsaved_image} was removed from the conversation to fit the "
                    "context window, and no copy was saved. Ask the user to attach it again "
                    "if you need it.]"
                ),
            }
        image = f"Image {name}" if name else "Image"
        return {
            "type": "text",
            "text": f"[{image} was removed from the conversation to fit the context window.]",
        }
    if kind not in FILE_BLOCK_TYPES:
        return None
    name = name or block.get("filename") or "PDF"
    size = f" ({pages} page{'' if pages == 1 else 's'})" if pages else ""
    if _unsaved(block):
        # Nothing to Read: the block was its only copy.
        tail = ", and no copy was saved. Ask the user to attach it again if you need it.]"
    else:
        tail = ". Read it again by page range if you need it.]"
    return {
        "type": "text",
        "text": f"[{name}{size} was removed from the conversation to fit the context window{tail}",
    }


def remove_attachments(
    messages: list,
    ids: Collection[str],
    *,
    pages: Callable[[dict], int | None] = lambda block: block.get("pages"),
) -> list:
    """Replace every attachment in the messages ``ids`` names with the
    placeholder saying it was removed to fit the context window.

    A Read result names its path or URL, which is what reading it again
    takes; another attachment names the file it carries, if any. ``pages``
    gives a PDF block's page count, None when it cannot be read.

    Returns the original list when nothing changed, and never mutates a
    message.
    """
    if not ids:
        return messages
    sources = read_sources(messages)
    result: list = []
    modified = False
    for msg in messages:
        content = getattr(msg, "content", None)
        if getattr(msg, "id", None) not in ids or not isinstance(content, list):
            result.append(msg)
            continue
        name = _source(msg, sources)
        new_blocks = []
        for j, block in enumerate(content):
            placeholder = None
            if isinstance(block, dict):
                count = pages(block) if block_type(block) in FILE_BLOCK_TYPES else None
                # An image block reaches the provider as it is, so whether an
                # upload was saved is read off the label written just before it.
                unsaved = unsaved_upload_name(content[j - 1]) if j else None
                placeholder = _removed_placeholder(block, name, count, unsaved)
            new_blocks.append(block if placeholder is None else placeholder)
        if new_blocks == content:
            result.append(msg)
            continue
        modified = True
        result.append(msg.model_copy(update={"content": new_blocks}))
    return result if modified else messages


#: How a thread's images are fitted to Claude: ``"fixed"``, to the side every
#: image count allows, or ``"count"``, to the side the request's image count
#: allows, as before the fixed side shipped.
IMAGE_SIDE_KEY = "image_side_rule"

# TODO(2026-11-25): delete 30 days after this ships, once the threads written before it have aged out. "count" goes with it.
ImageSideRule = Literal["fixed", "count"]


class ImageSideState(UnsentThinkingState):
    image_side_rule: NotRequired[Annotated[ImageSideRule, PrivateStateAttr]]
    # DALTON-HOOK(attachment-native-cap): declared on the strip's own state
    # schema too so a graph built with ``compaction.enabled=false`` (no
    # ``CompactionMiddleware``) still checkpoints and unions the removal record.
    _removed_attachment_ids: Annotated[NotRequired[set[str]], PrivateStateAttr, _union]


def _image_side_rule(request: Any, route: str) -> tuple[ImageSideRule, ImageSideRule | None]:
    """The thread's rule, and the one to record when it has none yet.

    Chosen once, on the thread's first call to Claude: a thread whose first
    such call already has an image ahead of thinking was sent it by count, and
    fitting it anew would invalidate that thinking, so it keeps the count.
    """
    stored = (getattr(request, "state", None) or {}).get(IMAGE_SIDE_KEY)
    if stored or route != "anthropic":
        return stored or "fixed", None
    messages = request.messages or []
    behind = any(
        isinstance(block, dict) and block_type(block) in IMAGE_BLOCK_TYPES
        for message in messages[: checked_thinking_end(messages)]
        if isinstance(message.content, list)
        for block in message.content
    )
    rule: ImageSideRule = "count" if behind else "fixed"
    return rule, rule


class MultimodalStripMiddleware(AgentMiddleware):
    """Remove attachment blocks the model about to be called cannot read.

    Wired inside the resilience stack so it judges the post-fallback client, and
    shared across the main and subagent stacks: the target is read off each
    request, so one instance serves every model in the graph.
    """

    state_schema = ImageSideState

    def __init__(
        self,
        *,
        model_name: str | None = None,
        custom_modalities: list[str] | None = None,
    ) -> None:
        """
        Args:
            model_name: Configured manifest model, used only to decide whether
                ``custom_modalities`` applies to the target in hand.
            custom_modalities: Per-model modality override from user preferences.
        """
        super().__init__()
        self.model_name = model_name
        self.custom_modalities = custom_modalities

    @classmethod
    def for_config(cls, config: Any) -> "MultimodalStripMiddleware":
        """The strip an agent built from ``config`` runs, which manual
        compaction also judges attachments by when that agent has none."""
        return cls(
            model_name=config.llm.name if config.llm else None,
            custom_modalities=config.input_modalities,
        )

    def _resolve_target(self, request: Any) -> tuple[str | None, list[str]]:
        """Name and modalities of the model this request will actually reach.

        Read off ``request.model`` rather than the configured name so a
        resilience fallback — or a subagent running its own model — is judged on
        the client in hand. The name comes back too because the PDF page ceiling
        is per-model and has to be looked up against the same target.
        """
        return self._target(request.model)

    def _target(self, model: Any) -> tuple[str | None, list[str]]:
        metadata = getattr(model, "metadata", None)
        stamped = metadata.get("manifest_model") if isinstance(metadata, dict) else None

        if not isinstance(stamped, str) or not stamped:
            # Unattributable: the client skipped LLM.get_llm(), which today means
            # a subagent whose config names a bare model string for deepagents to
            # resolve via init_chat_model. Falling back to the configured name
            # would lend a vision parent's modalities to a text-only target and
            # replay exactly the blocks this strip exists to remove, so an
            # unstamped client is judged text-only — the same fail-closed reading
            # LLM.get_llm() documents when it writes the stamp.
            return None, ["text"]
        # ``custom_modalities`` is a per-model override from user preferences, so
        # it describes only the model it was configured for — a fallback is a
        # different model and has to be judged on its own manifest entry.
        overrides = self.custom_modalities if stamped == self.model_name else None
        return stamped, get_input_modalities(stamped, custom_modalities=overrides)

    def receives(self, model: Any) -> Callable[[Any], bool]:
        """Whether a call to ``model`` is sent a block as it is, rather than
        the placeholder this strip writes in its place.

        Compaction, outside the resilience stack, counts only those toward its
        trigger and its fit, judged as here so the two cannot drift. An image
        the call-time fit cannot decode still counts, as the small overcount
        it is.
        """
        stamped, modalities = self._target(model)
        has_image, has_pdf = "image" in modalities, "pdf" in modalities
        max_pdf_pages = get_max_pdf_pages(stamped) if stamped and has_pdf else None
        return lambda block: (
            _placeholder_for(block, has_image, has_pdf, max_pdf_pages, "") is None
        )

    def pdf_page_tokens(self, model: Any) -> int | None:
        """What a call to ``model`` is charged for a PDF page, where its vendor's
        charge was measured; None leaves compaction its default estimate.

        Read off the same client as ``receives``, so the vendor is the one the
        call reaches, a fallback's or a subagent's included.
        """
        return get_pdf_page_tokens(image_route(getattr(model, "metadata", None)))

    async def awrap_model_call(self, request, handler):
        # Runs for every target, not just modality-limited ones: the interleaving
        # only arises where injection succeeded, which is a model that can see it.
        ordered = order_tool_results_first(request.messages)
        messages = ordered

        stamped, modalities = self._resolve_target(request)
        has_image = "image" in modalities
        has_pdf = "pdf" in modalities
        # Looked up even for a fully multimodal target: a model that accepts PDFs
        # can still be handed one past its page ceiling, which is the judgement
        # the tool side deliberately no longer attempts.
        max_pdf_pages = get_max_pdf_pages(stamped) if stamped and has_pdf else None

        unreadable: frozenset[tuple[int, int]] = frozenset()
        # DALTON-HOOK(attachment-native-cap): the ids of the messages whose
        # images the request's byte total left out, to record the upstream way.
        left_out: frozenset[str] = frozenset()
        route = image_route(getattr(request.model, "metadata", None))
        rule, update = _image_side_rule(request, route)
        updates: dict[str, Any] = {}
        if has_image:
            side = get_max_image_side_by_count if rule == "count" else get_max_image_side
            # DALTON-HOOK(dense-image-reading): the downscale note crops only when this call can run code.
            capability = _read_capability(getattr(request, "tools", None))

            def fit(count: int) -> int | None:
                return side(route, count)

            # ``pdf_pages`` is imported here: ``compaction`` imports this module
            # (offloading takes remove_attachments from it), so a module-level
            # import would cycle.
            from ptc_agent.agent.middleware.compaction.utils import pdf_pages

            state = getattr(request, "state", None) or {}
            already = set(state.get("_removed_attachment_ids") or ())
            already_unsent = unsent_thinking(state)
            # DALTON-HOOK(attachment-native-cap): the recorded removal and its
            # unsent thinking are replayed here whatever else applies them: the
            # offload view runs only while compaction is on, and with it off
            # the record must still hold every later call's bytes — an image a
            # recorded message still carried would otherwise ride the byte note
            # the walk leaves, behind thinking whose prefix just moved.
            # Idempotent where the view already applied them: a placeholder is
            # not an attachment block and a stripped answer has no reasoning.
            if already or already_unsent:
                messages = without_thinking(
                    remove_attachments(messages, already, pages=pdf_pages), already_unsent
                )
            messages, unreadable, left_out = await fit_images(
                messages, get_image_types(route), fit, capability=capability
            )
            # DALTON-HOOK(attachment-native-cap): the byte total's cut moves
            # between calls — what fits changes as images arrive — so it is
            # joined to the removal the context window uses rather than left a
            # per-request rewrite: the message goes into
            # ``_removed_attachment_ids`` and every answer after it into
            # ``_unsent_thinking_ids``, and this request already sends the
            # bytes every later one reads. Before, the first trimming request
            # replaced the images with notes but kept sending the thinking
            # behind them, whose prefix had just changed — the API dropped or
            # refused exactly that thinking on every later call. The removal
            # is applied to the pre-fit messages and they are fitted again, so
            # this call's placeholders are the ones the record replays, not
            # the byte note they would otherwise become.
            recorded = already.copy()
            unsent_all: set[str] = set()
            # The refit finds nothing new to leave out — removal only frees
            # budget — so the second pass exits at once; the bound keeps a
            # surprise from looping.
            for _ in range(2):
                newly = left_out - recorded
                if not newly:
                    break
                unsent_all |= thinking_after(ordered, newly)
                recorded |= newly
                messages = without_thinking(
                    remove_attachments(ordered, recorded, pages=pdf_pages),
                    already_unsent | unsent_all,
                )
                messages, unreadable, left_out = await fit_images(
                    messages, get_image_types(route), fit, capability=capability
                )
            if recorded != already:
                updates["_removed_attachment_ids"] = recorded
            if unsent_all:
                updates[UNSENT_THINKING_KEY] = already_unsent | unsent_all
        # Decided on the call where the target is known: a tool-time note judged
        # the configured model and froze that verdict into the transcript, which
        # a subagent on its own model then inherited wrongly. The system prompt
        # is left alone so the cached prefix does not move with the strip, and
        # every placeholder, an image the fit could not use among them, is
        # written in this one pass so each guidance stays on the first it
        # applies to.
        # DALTON-HOOK(dense-image-reading): an image note follows whether this
        # model can see images and whether this call can run code. A model
        # that cannot see the image is told to hand it off, even with Read.
        # A model that can see, with only Read, reads the original and is not
        # told to crop. A PDF the model cannot accept is handed to an agent
        # that can read PDFs; only a PDF over the page ceiling is read by page.
        # No tool list stays generic.
        messages = strip_unsupported_content_blocks(
            messages,
            has_image,
            has_pdf,
            max_pdf_pages,
            lambda source, *, kind=None, model_sees_images=None, pdf_reason=None: (
                placeholder_guidance(
                    source,
                    _read_capability(getattr(request, "tools", None)),
                    kind=kind,
                    model_sees_images=model_sees_images,
                    pdf_reason=pdf_reason,
                )
            ),
            unreadable=unreadable,
        )
        if messages is not request.messages:
            request = request.override(messages=messages)
        response = await handler(request)
        # DALTON-HOOK(attachment-native-cap): the side rule and the byte
        # total's removal record ride one command.
        if update is not None:
            updates[IMAGE_SIDE_KEY] = update
        if updates:
            return ExtendedModelResponse(
                model_response=response, command=Command(update=updates)
            )
        return response


__all__ = [
    "MultimodalStripMiddleware",
    "placeholder_guidance",
    "read_sources",
    "remove_attachments",
    "strip_unsupported_content_blocks",
]
