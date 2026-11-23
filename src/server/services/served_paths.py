"""What may leave a workspace as a served file, and which bytes are text.

The file routes, the share routes and the artifact versions all judge a path
and redact a body by these rules. They sit below ``app/`` so the agent's own
tools and the artifact services read the same rules without importing a
router.
"""

from __future__ import annotations

from dataclasses import dataclass

from ptc_agent.core.paths import (
    AGENT_SYSTEM_DIRS,
    ALWAYS_HIDDEN_BASENAMES as _SHARED_BASENAMES,
    ALWAYS_HIDDEN_DIR_NAMES,
    ALWAYS_HIDDEN_PATH_SEGMENTS,
    ALWAYS_HIDDEN_SUFFIXES,
    HIDDEN_DIR_NAMES,
    TRANSIENT_ROOT_PREFIX,
    SandboxLayout,
    is_agent_notes_path,
)
from src.server.utils.pg_sanitize import like_escape

# Derived from shared constants (source of truth: ptc_agent.core.paths)
SYSTEM_DIR_PREFIXES = tuple(f"{d}/" for d in sorted(AGENT_SYSTEM_DIRS))
_HIDDEN_DIR_PREFIXES = tuple(f"{d}/" for d in sorted(HIDDEN_DIR_NAMES))
_ALWAYS_HIDDEN_SEGMENTS = ALWAYS_HIDDEN_PATH_SEGMENTS
_ALWAYS_HIDDEN_BASENAMES = _SHARED_BASENAMES + (".file_sync_marker",)
_ALWAYS_HIDDEN_SUFFIXES = ALWAYS_HIDDEN_SUFFIXES

_ALWAYS_HIDDEN_DIR_SEGMENTS = tuple(f"/{d}/" for d in ALWAYS_HIDDEN_DIR_NAMES)

# Hidden only to keep a listing tidy, and where a page keeps its libraries and
# build output. The rest of the always-hidden folders hold a repository's
# history, an environment and tool caches, which no page needs and which can
# carry credentials.
PAGE_LIBRARY_DIR_NAMES: frozenset[str] = frozenset(
    {"node_modules", "vendor", ".next", ".nuxt"}
)
_UNLOADABLE_DIR_SEGMENTS = tuple(
    f"/{d}/" for d in sorted(ALWAYS_HIDDEN_DIR_NAMES - PAGE_LIBRARY_DIR_NAMES)
)


def is_hidden_path(client_path: str) -> bool:
    if client_path == SandboxLayout.INTERNAL_DIR:
        return True
    return any(client_path.startswith(prefix) for prefix in _HIDDEN_DIR_PREFIXES)


# --- DALTON-HOOK(colleague-access): begin ---
# Staging prefix for server-side copy_file temp files under ``.agents/tmp/``.
# Kept out of the file panel (even with include_system=True) and backups, and
# isolated from the transfer runtime's root ``.wsfiles-*`` orphan sweep.
COPY_STAGING_PREFIX: str = f"{SandboxLayout.TMP_DIR}/colleague-copy-"

_NON_OWNER_SENSITIVE_SUFFIXES: tuple[str, ...] = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".ppk",
    ".kdbx",
    ".ovpn",
    ".env",
)
_NON_OWNER_SENSITIVE_KEY_PREFIXES: tuple[str, ...] = (
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
)
_NON_OWNER_SENSITIVE_JSON_PREFIXES: tuple[str, ...] = (
    "credentials",
    "client_secret",
    "service_account",
    "service-account",
)
_NON_OWNER_SENSITIVE_CONFIG_SUFFIXES: tuple[str, ...] = (
    ".json",
    ".yaml",
    ".yml",
    ".toml",
)
_NON_OWNER_SENSITIVE_SUBSTRINGS: tuple[str, ...] = (
    "credential",
    "secret",
    "token",
    "private_key",
    "deploy_key",
    "id_rsa",
    "id_ed25519",
)
_NON_OWNER_SENSITIVE_EXACT_BASENAMES: frozenset[str] = frozenset(
    {
        ".env",
        "authorized_keys",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "secrets.toml",
        "secrets.env",
        "secret.env",
        "credentials",
        "netrc",
        "pgpass",
        "htpasswd",
    }
)


def _is_non_owner_sensitive_basename(basename: str) -> bool:
    lower = basename.strip().lower()
    if not lower:
        return True
    if lower == ".env" or lower.startswith(".env.") or ".env." in lower:
        return True
    if lower in _NON_OWNER_SENSITIVE_EXACT_BASENAMES:
        return True
    if lower.endswith(_NON_OWNER_SENSITIVE_SUFFIXES):
        return True
    if any(lower.startswith(prefix) for prefix in _NON_OWNER_SENSITIVE_KEY_PREFIXES):
        return True
    if lower.endswith(".json") and any(
        lower.startswith(prefix) for prefix in _NON_OWNER_SENSITIVE_JSON_PREFIXES
    ):
        return True
    is_config_or_extensionless = (
        "." not in lower
        or lower.endswith(_NON_OWNER_SENSITIVE_CONFIG_SUFFIXES)
        or lower.startswith(".env")
        or lower.endswith(".env")
        or ".env." in lower
    )
    if is_config_or_extensionless and any(
        sub in lower for sub in _NON_OWNER_SENSITIVE_SUBSTRINGS
    ):
        return True
    return False


def is_non_owner_sensitive_path(client_path: str) -> bool:
    """Unified path filter shared by Chief of Staff, Analyst, and Web non-owner routes.

    Blocks:
    - empty / parent-traversal (``..``) paths (standalone ``.`` is treated as the
      workspace root directory and allowed);
    - always-hidden and ``_internal`` / system paths (``.agents/`` is never
      shared with non-owners);
    - ``.env``, ``.env.*``, and ``*.env`` files anywhere in the path;
    - dot-prefixed hidden files or directories at any depth;
    - credential / key / secret filenames (``*.pem``, ``*.key``, ``id_rsa*``,
      ``*.json``/``*.yaml``/``*.yml``/``*.toml``/``.env*`` or extensionless
      files containing ``credential``, ``secret``, ``token``, ``private_key``,
      ``deploy_key``, ``id_rsa``, or ``id_ed25519``).
    """
    if not client_path:
        return True
    cleaned = client_path.replace("\\", "/").strip("/")
    if not cleaned:
        return True
    if cleaned == ".":
        return False
    raw_parts = [part for part in cleaned.split("/") if part != ""]
    if not raw_parts or ".." in raw_parts:
        return True
    parts = [part for part in raw_parts if part != "."]
    if not parts:
        return False

    normalized = "/".join(parts)
    with_slash = f"{normalized}/"

    if (
        is_always_hidden_path(normalized)
        or is_hidden_path(normalized)
        or with_slash.startswith(_HIDDEN_DIR_PREFIXES)
    ):
        return True

    if with_slash.startswith(SYSTEM_DIR_PREFIXES):
        return True
    if any(part.startswith(".") for part in parts):
        return True

    if _is_non_owner_sensitive_basename(parts[-1]):
        return True

    return False
# --- DALTON-HOOK(colleague-access): end ---


def _is_transient_path(normalized: str) -> bool:
    # A transfer's partial or staged file, whose bytes are not yet the file
    # they will become; one a user edits or removes fails the transfer.
    if normalized.rfind("/") == 0 and normalized[1:].startswith(
        TRANSIENT_ROOT_PREFIX
    ):
        return True
    # DALTON-HOOK(colleague-access): hide copy_file staging temp files under .agents/tmp/
    return normalized.startswith(f"/{COPY_STAGING_PREFIX}")


def is_always_hidden_path(client_path: str) -> bool:
    normalized = f"/{client_path.lstrip('/')}"

    if _is_transient_path(normalized):
        return True

    if normalized.endswith(_ALWAYS_HIDDEN_BASENAMES):
        return True

    if normalized.endswith(_ALWAYS_HIDDEN_SUFFIXES):
        return True

    if any(seg in normalized for seg in _ALWAYS_HIDDEN_SEGMENTS):
        return True

    if any(seg in normalized for seg in _ALWAYS_HIDDEN_DIR_SEGMENTS):
        return True

    return False


def panel_lists_stored_file(
    client_path: str, *, include_system: bool = False, include_hidden: bool = False
) -> bool:
    """Whether the file panel lists a backed-up file at this path, as its
    listing of a stopped workspace does.

    Without ``include_system`` every system directory is out whole: the
    carve-out ``_is_system_path`` makes for the user's profile, automation and
    subagent files does not reach a backup row, since their rows are the only
    copy and the panel splices them in from there, dropping any stored row at
    their paths.
    """
    return not (
        is_always_hidden_path(client_path)
        or (not include_hidden and is_hidden_path(client_path))
        or (not include_system and client_path.startswith(SYSTEM_DIR_PREFIXES))
    )


# ``panel_lists_stored_file``'s default rules as LIKE patterns over the path
# with a leading ``/`` (as ``is_always_hidden_path`` reads it), any match
# leaving the file out, for a SQL reader that has to drop the same files
# before it pages.
STORED_FILE_EXCLUDED_LIKES: tuple[str, ...] = (
    *(f"/{like_escape(p)}%" for p in (*SYSTEM_DIR_PREFIXES, *_HIDDEN_DIR_PREFIXES)),
    f"/{like_escape(SandboxLayout.INTERNAL_DIR)}",
    *(
        f"%{like_escape(s)}%"
        for s in (*_ALWAYS_HIDDEN_SEGMENTS, *_ALWAYS_HIDDEN_DIR_SEGMENTS)
    ),
    *(f"%{like_escape(e)}" for e in (*_ALWAYS_HIDDEN_BASENAMES, *_ALWAYS_HIDDEN_SUFFIXES)),
)
# The transient root names, which a LIKE cannot keep to the root: it pairs
# with the path holding no ``/``.
STORED_FILE_ROOT_EXCLUDED_LIKE = f"{like_escape(TRANSIENT_ROOT_PREFIX)}%"


def is_agent_machinery_path(client_path: str) -> bool:
    """True for what no page has a use for and an owner's page must not read.

    The agent's own directories hold its tool wrappers and the user's profile,
    memory and holdings, which the file mount links into every workspace
    folder, and a page that runs a stranger's script has the reach of whatever
    this lets through.
    """
    return (
        _is_transient_path(f"/{client_path.lstrip('/')}")
        or is_hidden_path(client_path)
        or client_path.startswith(SYSTEM_DIR_PREFIXES)
    )


def page_may_load(client_path: str) -> bool:
    """Whether the owner's view of a page, and what the page publishes, may
    hold this file: anything but the agent's machinery and the always-hidden
    files, except the library folders (``PAGE_LIBRARY_DIR_NAMES``) a page
    loads its scripts and styles from."""
    if is_agent_machinery_path(client_path):
        return False
    normalized = f"/{client_path.lstrip('/')}"
    return not (
        normalized.endswith(_ALWAYS_HIDDEN_BASENAMES)
        or normalized.endswith(_ALWAYS_HIDDEN_SUFFIXES)
        or any(seg in normalized for seg in _ALWAYS_HIDDEN_SEGMENTS)
        or any(seg in normalized for seg in _UNLOADABLE_DIR_SEGMENTS)
    )


def is_text_content_type(content_type: str) -> bool:
    """True for content types whose bytes should be redacted / theme-injected."""
    ct = content_type.split(";", 1)[0].strip().lower()
    return (
        ct.startswith("text/")
        or ct in ("application/json", "application/xml", "image/svg+xml")
        or ct.endswith("+json")
        or ct.endswith("+xml")
    )


def is_utf8(data: bytes) -> bool:
    """True when bytes decode cleanly as UTF-8 (i.e. text, whatever the MIME)."""
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


@dataclass(frozen=True)
class ShareScope:
    """What a share token resolves to: a workspace and the subtree it opens.

    The pair is the unit, not the workspace alone. A token issued for one report
    serves that path and its subtree; nothing else in the workspace is reachable
    through it, however the URL is spelled.
    """

    workspace_id: str
    root_path: str

    def contains(self, relative_path: str) -> bool:
        if not self.root_path:
            return True
        return relative_path == self.root_path or relative_path.startswith(
            f"{self.root_path}/"
        )


def shared_path_visible(scope: ShareScope, client_path: str) -> bool:
    """Whether a canonical workspace path may leave through this share token.

    Run on the path a sandbox read resolved to, not on the one that was asked
    for: a symlink is how an in-scope request turns into an out-of-scope file.

    Stricter than the panel: it skips ``_is_system_path``'s carve-out for the
    user's profile, automation and subagent files. Their owner reads them through
    the file routes, and the file mount links them into every workspace folder,
    so only containment would stand between a share link and the user's holdings.

    The workspace's notes file stays out too, whatever the scope: it is the
    agent's own runtime context, and a shared page cannot tell it from a
    deliverable because the share carries no folder name to recognise it by.
    """
    return (
        scope.contains(client_path)
        and not is_always_hidden_path(client_path)
        and not is_agent_machinery_path(client_path)
        and not is_agent_notes_path(client_path)
    )


# What a comment's and a version file's path columns hold, in characters.
_MAX_ARTIFACT_PATH = 1024


def may_be_artifact(path: str) -> bool:
    """Whether a workspace-relative path may have an artifact record and go out
    with a publish, in a length its comments and versions can store. Wider than
    a share token's gate because the owner reviews each file a publish lists."""
    return (
        len(path) <= _MAX_ARTIFACT_PATH
        and page_may_load(path)
        and not is_agent_notes_path(path)
    )
