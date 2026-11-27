"""Reading a Daytona SDK error: its status, its code, and what it means.

Shared by the provider, which classifies sandbox failures, and the secret
reconciler, which retries its own calls; neither owns the other.
"""

import threading  # DALTON-HOOK(cos-settings-tools): thread-safe Daytona OTel span guard
from collections.abc import Callable, Iterator  # DALTON-HOOK(cos-settings-tools): guard predicate type
from typing import Any

from daytona import DaytonaConnectionError


def daytona_error_status(exc: Exception) -> int | None:
    """Extract an HTTP status through SDK wrapper exception chains."""

    return _walk_chain_attrs(exc, ("status", "status_code"))


def daytona_error_code(exc: Exception) -> str | None:
    """Extract the SDK's machine-readable ``code`` through wrapper chains.

    Structured and stable where the message text is not — it is what separates a
    per-path ``FILE_NOT_FOUND`` from a sandbox-level ``NOT_FOUND``, both of which
    arrive as HTTP 404. Only a string counts: ``code`` is a common attribute
    name, and an integer on some wrapped exception is not an SDK envelope.
    """

    return _walk_chain_attrs(exc, ("code",))


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """Each exception of the ``__cause__``/``__context__`` chain once, outermost first."""

    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _walk_chain_attrs(exc: Exception, attrs: tuple[str, ...]) -> Any | None:
    """First truthy *attrs* value found walking the ``__cause__``/``__context__`` chain."""

    for current in _chain(exc):
        for attr in attrs:
            value = getattr(current, attr, None)
            if attr in ("status", "status_code"):
                if isinstance(value, int):
                    return value
            elif attr == "code":
                # ``code`` is a common name; an integer on an outer wrapper
                # (HTTPError, SystemExit) must not hide the SDK's string.
                if isinstance(value, str) and value:
                    return value
            elif value:
                return value
    return None


def is_daytona_host_unavailable(exc: Exception) -> bool:
    """A start Daytona refused because the sandbox's host is recovering.

    A 503 alone is any overload, so the wording is the only thing that tells
    this one apart. If Daytona rewords it, nothing matches and the start stays
    an ordinary failure, which never replaces a sandbox.
    """

    if daytona_error_status(exc) != 503:
        return False
    return any("host recovers" in str(current).lower() for current in _chain(exc))


def is_transient_daytona_error(exc: Exception) -> bool:
    """Classify a Daytona SDK error as transient (retryable).

    Status-less transport failures (connection reset, timeouts, a closed
    client) carry no HTTP status, so 429/5xx classification misses them —
    status-BEARING errors are classified by status at the retry sites, never
    here (bare digit markers would misclassify e.g. "400 Bad Request" as
    transient). Execution errors (the command ran and the server answered)
    are not transient even when the server's message mentions a timeout.
    """

    # Transport exception types are transient even with an empty message
    # (a bare ConnectionResetError / TimeoutError carries no text). The SDK maps
    # its transport failures, a read timeout among them, to
    # DaytonaConnectionError and keeps the prefix of the call that failed, so an
    # exec whose reply never came reads "Failed to execute command: " with
    # nothing after it and would otherwise fall to the execution-error guard.
    if isinstance(exc, (ConnectionError, TimeoutError, DaytonaConnectionError)):
        return True
    message = str(exc).lower()
    # The request never reached the command: a closed HTTP client, or a
    # sandbox proxy that cannot find the container. The SDK prefixes both with
    # "Failed to execute command:", so check BEFORE the execution-error guard.
    not_reached = ("session is closed", "client is closed", "no ip address found")
    if any(marker in message for marker in not_reached):
        return True
    if "failed to execute command" in message:
        return False
    # 5xx/429 phrases cover SDK errors stringified without a status attribute;
    # phrases, never bare digits, so "400 Bad Request" stays terminal.
    transient_markers = (
        "remote end closed connection",
        "remotedisconnected",
        "connection aborted",
        "connection reset",
        "broken pipe",
        "timed out",
        "timeout",
        "bad gateway",
        "service unavailable",
        "too many requests",
    )
    return any(marker in message for marker in transient_markers)


# --- DALTON-HOOK(cos-settings-tools): begin ---
_DAYTONA_OTEL_LOCK = threading.Lock()


def install_daytona_otel_guard(is_suppressed: Callable[[], bool]) -> None:
    """Idempotently wrap ``daytona._utils.otel_decorator.get_tracer`` with a gate.

    Locked Daytona SDK ``@with_instrumentation()`` methods record ``str(error)``
    and tracebacks into their own child spans before callers can catch the
    exception. Returning ``NoOpTracer`` only while ``is_suppressed()`` is True
    keeps raw provider bodies out of OTLP spans without affecting other tasks.
    """
    try:
        import daytona._utils.otel_decorator as daytona_otel
        from opentelemetry import trace as otel_trace
    except Exception:
        return
    if getattr(daytona_otel.get_tracer, "_dalton_raw_span_guard", False):
        return
    with _DAYTONA_OTEL_LOCK:
        if getattr(daytona_otel.get_tracer, "_dalton_raw_span_guard", False):
            return
        orig_get_tracer = daytona_otel.get_tracer

        def _guarded_get_tracer():
            if is_suppressed():
                return otel_trace.NoOpTracer()
            return orig_get_tracer()

        _guarded_get_tracer._dalton_raw_span_guard = True  # type: ignore[attr-defined]
        daytona_otel.get_tracer = _guarded_get_tracer


def is_daytona_post_submit_refresh_404(exc: BaseException) -> bool:
    """Detect when Daytona SDK's ``archive()`` submitted successfully and its internal ``refresh_data()`` got 404."""
    from daytona import DaytonaNotFoundError

    if not isinstance(exc, DaytonaNotFoundError):
        return False
    # AsyncSandbox.archive wraps AsyncSandbox.refresh_data's DaytonaNotFoundError
    # via @intercept_errors(message_prefix="Failed to archive sandbox: "), setting
    # __cause__ to refresh_data's DaytonaNotFoundError (message starts with
    # "Failed to refresh sandbox data:").
    if isinstance(exc.__cause__, DaytonaNotFoundError):
        return True
    return "Failed to refresh sandbox data:" in str(exc)


def classify_daytona_archive_error(exc: Exception) -> str | None:
    """Classify a Daytona SDK exception into ``'sandbox_gone'``, ``'sandbox_transient'``, or ``None``."""
    try:
        from daytona import DaytonaError, DaytonaNotFoundError
    except Exception:
        return None
    if isinstance(exc, DaytonaNotFoundError):
        return "sandbox_gone"
    if isinstance(exc, DaytonaError):
        status_code = getattr(exc, "status_code", None)
        if status_code in (408, 429, 500, 502, 503, 504):
            return "sandbox_transient"
    return None
# --- DALTON-HOOK(cos-settings-tools): end ---
