"""Field metadata, credential groups, and KeySpec projection for deployment credentials.

All UI metadata, secrecy flags, group pairing rules, and write allowlists live
in Dalton here; upstream ``setup_settings/catalog.py`` receives only a thin
projection of ``KeySpec(name, group, provider)``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from src.dalton.profile import DeploymentProfile, resolve_profile

if TYPE_CHECKING:
    from src.server.services.setup_settings.catalog import KeySpec

InputKind = Literal["secret", "text", "url", "boolean", "boolean_one", "select", "textarea"]
RestartKind = Literal["none", "next_turn", "sandbox_hint", "gateway_hint", "transcription_hint"]
BatchNumber = Literal[1, 2, 3, 4]

ALL_PROFILES: frozenset[DeploymentProfile] = frozenset(
    {DeploymentProfile.CORE, DeploymentProfile.EVERFLOW}
)

GOOGLE_OAUTH_CLIENT_ID_KEY = "DALTON_GOOGLE_OAUTH_CLIENT_ID"
GOOGLE_OAUTH_CLIENT_SECRET_KEY = "DALTON_GOOGLE_OAUTH_CLIENT_SECRET"
FEISHU_APP_ID_KEY = "DALTON_FEISHU_APP_ID"
FEISHU_APP_SECRET_KEY = "DALTON_FEISHU_APP_SECRET"
FEISHU_ENCRYPT_KEY = "DALTON_FEISHU_ENCRYPT_KEY"
FEISHU_VERIFICATION_TOKEN_KEY = "DALTON_FEISHU_VERIFICATION_TOKEN"

DEPLOYMENT_SECRET_ENV_NAMES: tuple[str, ...] = (
    GOOGLE_OAUTH_CLIENT_SECRET_KEY,
    FEISHU_APP_SECRET_KEY,
    FEISHU_ENCRYPT_KEY,
    FEISHU_VERIFICATION_TOKEN_KEY,
)


@dataclass(frozen=True)
class DaltonDeploymentFieldSpec:
    """Declarative metadata for one deployment-level credential or setting."""

    name: str
    group: str
    provider: str
    section: str
    label_zh: str
    label_en: str
    secret: bool
    credential_group: str | None = None
    pair_with: str | None = None
    input_kind: InputKind = "text"
    profiles: frozenset[DeploymentProfile] = ALL_PROFILES
    requires_restart: RestartKind = "none"
    test_kind: str | None = None
    batch: BatchNumber = 1


@dataclass(frozen=True)
class CredentialGroupSpec:
    """Atomicity and source-resolution rules for a logical credential group."""

    group: str
    keys: tuple[str, ...]
    pair_required: bool
    vault_fallback: bool = False


CREDENTIAL_GROUP_SPECS: dict[str, CredentialGroupSpec] = {
    "google_oauth": CredentialGroupSpec(
        group="google_oauth",
        keys=(GOOGLE_OAUTH_CLIENT_ID_KEY, GOOGLE_OAUTH_CLIENT_SECRET_KEY),
        pair_required=True,
        vault_fallback=False,
    ),
    "feishu_app": CredentialGroupSpec(
        group="feishu_app",
        keys=(FEISHU_APP_ID_KEY, FEISHU_APP_SECRET_KEY),
        pair_required=True,
        vault_fallback=True,
    ),
    "feishu_event": CredentialGroupSpec(
        group="feishu_event",
        keys=(FEISHU_ENCRYPT_KEY, FEISHU_VERIFICATION_TOKEN_KEY),
        pair_required=False,
        vault_fallback=False,
    ),
}

BATCH1_FIELD_SPECS: tuple[DaltonDeploymentFieldSpec, ...] = (
    DaltonDeploymentFieldSpec(
        name=GOOGLE_OAUTH_CLIENT_ID_KEY,
        group="oauth",
        provider="google",
        section="calendar_mail",
        label_zh="Google OAuth 客户端 ID",
        label_en="Google OAuth Client ID",
        secret=False,
        credential_group="google_oauth",
        pair_with=GOOGLE_OAUTH_CLIENT_SECRET_KEY,
        input_kind="text",
        profiles=ALL_PROFILES,
        requires_restart="none",
        test_kind="google_oauth",
        batch=1,
    ),
    DaltonDeploymentFieldSpec(
        name=GOOGLE_OAUTH_CLIENT_SECRET_KEY,
        group="oauth",
        provider="google",
        section="calendar_mail",
        label_zh="Google OAuth 客户端密钥",
        label_en="Google OAuth Client Secret",
        secret=True,
        credential_group="google_oauth",
        pair_with=GOOGLE_OAUTH_CLIENT_ID_KEY,
        input_kind="secret",
        profiles=ALL_PROFILES,
        requires_restart="none",
        test_kind="google_oauth",
        batch=1,
    ),
    DaltonDeploymentFieldSpec(
        name=FEISHU_APP_ID_KEY,
        group="feishu",
        provider="feishu",
        section="feishu_app",
        label_zh="飞书应用 App ID",
        label_en="Feishu App ID",
        secret=False,
        credential_group="feishu_app",
        pair_with=FEISHU_APP_SECRET_KEY,
        input_kind="text",
        profiles=ALL_PROFILES,
        requires_restart="gateway_hint",
        test_kind="feishu_app",
        batch=1,
    ),
    DaltonDeploymentFieldSpec(
        name=FEISHU_APP_SECRET_KEY,
        group="feishu",
        provider="feishu",
        section="feishu_app",
        label_zh="飞书应用 App Secret",
        label_en="Feishu App Secret",
        secret=True,
        credential_group="feishu_app",
        pair_with=FEISHU_APP_ID_KEY,
        input_kind="secret",
        profiles=ALL_PROFILES,
        requires_restart="gateway_hint",
        test_kind="feishu_app",
        batch=1,
    ),
    DaltonDeploymentFieldSpec(
        name=FEISHU_ENCRYPT_KEY,
        group="feishu",
        provider="feishu",
        section="feishu_app",
        label_zh="飞书事件加密密钥（Encrypt Key）",
        label_en="Feishu Event Encrypt Key",
        secret=True,
        credential_group="feishu_event",
        pair_with=FEISHU_VERIFICATION_TOKEN_KEY,
        input_kind="secret",
        profiles=ALL_PROFILES,
        requires_restart="none",
        test_kind=None,
        batch=1,
    ),
    DaltonDeploymentFieldSpec(
        name=FEISHU_VERIFICATION_TOKEN_KEY,
        group="feishu",
        provider="feishu",
        section="feishu_app",
        label_zh="飞书事件验证令牌（Verification Token）",
        label_en="Feishu Event Verification Token",
        secret=True,
        credential_group="feishu_event",
        pair_with=FEISHU_ENCRYPT_KEY,
        input_kind="secret",
        profiles=ALL_PROFILES,
        requires_restart="none",
        test_kind=None,
        batch=1,
    ),
)

# Active field registry tuple; tests may monkeypatch this to register temporary fields
# (for example, a temporary tri-state boolean field in unit tests).
_ACTIVE_FIELD_SPECS: tuple[DaltonDeploymentFieldSpec, ...] = BATCH1_FIELD_SPECS


def get_field_specs(
    *,
    profile: DeploymentProfile | None = None,
    max_batch: int = 1,
) -> tuple[DaltonDeploymentFieldSpec, ...]:
    """Return registered field specs filtered by batch and deployment profile."""
    specs = [s for s in _ACTIVE_FIELD_SPECS if s.batch <= max_batch]
    if profile is not None:
        specs = [s for s in specs if profile in s.profiles]
    return tuple(specs)


def get_field_spec(
    name: str,
    *,
    profile: DeploymentProfile | None = None,
    max_batch: int = 1,
) -> DaltonDeploymentFieldSpec | None:
    """Return the field spec matching ``name`` for the requested profile and batch."""
    for spec in get_field_specs(profile=profile, max_batch=max_batch):
        if spec.name == name:
            return spec
    return None


def writable_field_names(
    *,
    profile: DeploymentProfile | None = None,
    max_batch: int = 1,
) -> frozenset[str]:
    """Return the allowlisted setting names writable via the admin API for ``profile``."""
    active_profile = resolve_profile() if profile is None else profile
    return frozenset(
        spec.name
        for spec in get_field_specs(profile=active_profile, max_batch=max_batch)
    )


def get_credential_group_spec(group: str) -> CredentialGroupSpec | None:
    """Return the group specification for ``group``, or ``None`` if unknown."""
    return CREDENTIAL_GROUP_SPECS.get(group)


def group_keys(group: str) -> tuple[str, ...]:
    """Return the ordered environment variable keys belonging to ``group``."""
    spec = CREDENTIAL_GROUP_SPECS.get(group)
    if spec is None:
        raise ValueError(f"Unknown credential group: {group!r}")
    return spec.keys


def group_for_key(name: str) -> str | None:
    """Return the ``credential_group`` owning ``name``, or ``None`` for standalone keys."""
    for group_name, spec in CREDENTIAL_GROUP_SPECS.items():
        if name in spec.keys:
            return group_name
    for field in _ACTIVE_FIELD_SPECS:
        if field.name == name:
            return field.credential_group
    return None


def secret_field_names() -> frozenset[str]:
    """Return all registered field names marked ``secret=True``."""
    return frozenset(spec.name for spec in _ACTIVE_FIELD_SPECS if spec.secret)


def merge_dalton_key_specs(base_specs: Sequence[KeySpec]) -> tuple[KeySpec, ...]:
    """Merge Dalton field specs into upstream ``KeySpec`` tuples without re-entering ``key_specs()``.

    When Dalton mode is disabled, returns ``tuple(base_specs)`` unchanged so upstream
    OSS deployments keep their exact catalog. When enabled, projects each active
    ``DaltonDeploymentFieldSpec`` to ``KeySpec(name, group, provider)`` and deduplicates
    by ``name`` while preserving first-seen order.
    """
    from src.dalton.settings import is_dalton_enabled

    merged: list[KeySpec] = []
    seen: set[str] = set()
    for spec in base_specs:
        if spec.name in seen:
            continue
        seen.add(spec.name)
        merged.append(spec)

    if not is_dalton_enabled():
        return tuple(merged)

    from src.server.services.setup_settings.catalog import KeySpec as _KeySpec

    for field in get_field_specs():
        if field.name in seen:
            continue
        seen.add(field.name)
        merged.append(
            _KeySpec(name=field.name, group=field.group, provider=field.provider)
        )
    return tuple(merged)
