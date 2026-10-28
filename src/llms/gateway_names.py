"""How a gateway's model ids meet the manifest's: OpenRouter names a model
``vendor/name.with.dots``, the manifest ``name-with-dots``. The model list
matches by it to call a listed model known, and a custom entry saved from that
list is tuned as the model it matched."""

from __future__ import annotations


def gateway_slug(model_id: str) -> str:
    """``vendor/name.with.dots:suffix`` -> ``name-with-dots``, lowercased.

    How a gateway's id meets a manifest one: manifest ids go through the same
    mapping, so ``anthropic/claude-sonnet-5.5`` meets ``claude-sonnet-5-5`` and
    ``openai/gpt-6.1-sol`` meets ``gpt-6.1-sol``.
    """
    return model_id.split(":", 1)[0].rsplit("/", 1)[-1].lower().replace(".", "-")


def manifest_slug_index(mc) -> dict[str, str]:
    """``gateway_slug -> manifest key`` over the models a user can pick,
    preferring a vendor's own entry over a regional or plan variant of it."""
    candidates = []
    for order, (key, entry) in enumerate(mc.llm_config.items()):
        if not entry or not entry.get("visible"):
            continue
        info = mc.get_provider_info(entry.get("provider"))
        if not info or info.get("platform") or info.get("access_type") == "oauth":
            continue
        rank = 1 if info.get("parent_provider") else 0
        candidates.append((rank, order, gateway_slug(entry.get("model_id") or key), key))
    index: dict[str, str] = {}
    for _rank, _order, slug, key in sorted(candidates):
        index.setdefault(slug, key)
    return index
