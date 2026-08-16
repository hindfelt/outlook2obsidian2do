"""Claude API backend, using the Messages API's structured outputs."""

from __future__ import annotations

from typing import TypeVar

import anthropic
from pydantic import BaseModel

from .. import keychain
from ..runtime_config import ANTHROPIC_MODELS
from .base import ProviderError

T = TypeVar("T", bound=BaseModel)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self) -> None:
        self._client: anthropic.Anthropic | None = None
        self._client_key: str | None = None

    def _get_client(self) -> anthropic.Anthropic:
        key = keychain.get_api_key()
        if not key:
            raise ProviderError(
                "No Anthropic API key configured. Add one in the task pane's "
                "Settings section, or set ANTHROPIC_API_KEY."
            )
        # Rebuild when the key changes so a Settings update takes effect at once.
        if self._client is None or self._client_key != key:
            self._client = anthropic.Anthropic(api_key=key)
            self._client_key = key
        return self._client

    def structured(
        self,
        *,
        model: str,
        system: str,
        user_content: str,
        output_format: type[T],
        max_tokens: int = 16000,
    ) -> T:
        try:
            response = self._get_client().messages.parse(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_content}],
                output_format=output_format,
            )
        except anthropic.AuthenticationError as exc:
            raise ProviderError("Anthropic rejected the API key.") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError("Anthropic rate limit hit. Retry shortly.") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"Could not reach the Anthropic API: {exc}") from exc

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise ProviderError(
                f"Claude declined this request (category={getattr(details, 'category', None)})"
            )
        if response.stop_reason == "max_tokens":
            raise ProviderError(
                "Response hit max_tokens before completing - raise max_tokens or "
                "shorten the email body."
            )

        parsed = response.parsed_output
        if parsed is None:
            raise ProviderError("Claude returned no parseable structured output")
        return parsed

    def health(self) -> dict:
        key = keychain.get_api_key()
        return {
            "provider": self.name,
            "ready": bool(key),
            "detail": "API key configured" if key else "no API key",
            "api_key_set": bool(key),
            "api_key_hint": keychain.hint(key),
            "credential_store": keychain.describe_store(),
        }

    def list_models(self) -> list[dict]:
        return list(ANTHROPIC_MODELS)
