"""LLM backends. Selected at runtime by runtime_config.provider."""

from __future__ import annotations

from ..runtime_config import current
from .anthropic_provider import AnthropicProvider
from .base import Provider, ProviderError
from .ollama_provider import OllamaProvider

_PROVIDERS: dict[str, Provider] = {
    "anthropic": AnthropicProvider(),
    "ollama": OllamaProvider(),
}


def get_provider(name: str | None = None) -> Provider:
    key = name or current().provider
    try:
        return _PROVIDERS[key]
    except KeyError:
        raise ProviderError(f"Unknown provider {key!r}") from None


__all__ = ["get_provider", "Provider", "ProviderError"]
