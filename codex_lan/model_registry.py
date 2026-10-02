"""CODEX: Read the worker's model catalog without assuming account entitlements."""

from __future__ import annotations


MAX_MODELS = 100
MAX_MODEL_PAGES = 10
MODELS_URL = "https://learn.chatgpt.com/docs/models"
_REASONING_DESCRIPTIONS = {
    "low": "Lighter reasoning for quick tasks.",
    "medium": "Balanced reasoning for everyday tasks.",
    "high": "Deeper reasoning for complex tasks.",
    "xhigh": "Extra-high reasoning for difficult tasks.",
    "max": "Maximum reasoning depth.",
    "ultra": "Maximum reasoning with automatic task delegation where available.",
}
# CODEX: The user requested these documented IDs explicitly; documentation is not account entitlement.
_DOCUMENTED_GPT6 = (
    ("gpt-6-astra", "GPT-6 Astra", ("low", "medium", "high", "xhigh", "max", "ultra")),
    ("gpt-6.1-sol", "GPT-6.1 Sol", ("low", "medium", "high", "xhigh", "max", "ultra")),
    ("gpt-6-sol", "GPT-6 Sol", ("low", "medium", "high", "xhigh", "max", "ultra")),
    ("gpt-6-luna", "GPT-6 Luna", ("low", "medium", "high", "xhigh", "max")),
)


def model_catalog(provider_models):
    """CODEX: Append bounded, explicitly selectable official options; the provider remains authoritative."""
    models = [{**item, "availabilityVerified": True, "catalogSource": "provider"}
              for item in provider_models if item.get("hidden") is not True]
    advertised = {value for item in provider_models for value in (item.get("id"), item.get("model"))
                  if isinstance(value, str)}
    for identifier, display_name, efforts in _DOCUMENTED_GPT6:
        if identifier in advertised:
            continue
        models.append({
            "id": identifier,
            "model": identifier,
            "displayName": display_name,
            "description": (
                "Documented by OpenAI, but not advertised by this worker/account yet. "
                "Manual selection does not grant access; availability depends on your plan, "
                "workspace, client, and rollout."
            ),
            "hidden": False,
            "isDefault": False,
            "defaultReasoningEffort": "medium",
            "supportedReasoningEfforts": [
                {"reasoningEffort": effort, "description": _REASONING_DESCRIPTIONS[effort]}
                for effort in efforts
            ],
            "availabilityVerified": False,
            "catalogSource": "official",
            "catalogSourceUrl": MODELS_URL,
        })
    return models


class ModelRegistryError(ValueError):
    """CODEX: Reject incomplete or malformed catalogs without replacing known-good data."""


async def read_model_registry(request, *, timeout=30):
    """CODEX: Preserve provider capabilities and ordering across bounded model/list pages."""
    models = []
    identifiers = set()
    cursors = set()
    cursor = None
    for _page in range(MAX_MODEL_PAGES):
        # CODEX: Retain hidden IDs internally so official fallbacks cannot bypass an explicit exclusion.
        params = {"limit": MAX_MODELS, "includeHidden": True}
        if cursor is not None:
            params["cursor"] = cursor
        response = await request("model/list", params, timeout=timeout)
        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, list) or len(identifiers) + len(data) > MAX_MODELS:
            raise ModelRegistryError("Codex returned an invalid model registry.")
        for item in data:
            if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                    or not item["id"] or len(item["id"]) > 200
                    or item["id"] in identifiers):
                raise ModelRegistryError("Codex returned an invalid model registry.")
            identifiers.add(item["id"])
            models.append(dict(item))
        cursor = response.get("nextCursor")
        if cursor is None:
            return models
        if (not isinstance(cursor, str) or not cursor or len(cursor) > 4096
                or cursor in cursors):
            raise ModelRegistryError("Codex returned an invalid model registry cursor.")
        cursors.add(cursor)
    raise ModelRegistryError("Codex returned too many model registry pages.")
