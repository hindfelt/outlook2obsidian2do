"""Provider interface.

Every backend takes a system prompt, a user message and a Pydantic model, and
returns a validated instance of that model. Constrained decoding is mandatory -
no backend is allowed to hand back prose for the caller to parse.
"""

from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class ProviderError(RuntimeError):
    """Backend could not produce a valid structured response."""


class Provider(Protocol):
    name: str

    def structured(
        self,
        *,
        model: str,
        system: str,
        user_content: str,
        output_format: type[T],
        max_tokens: int = 16000,
    ) -> T: ...

    def health(self) -> dict:
        """Cheap reachability / configuration check. No model call."""
        ...

    def list_models(self) -> list[dict]:
        """Models this backend can currently serve."""
        ...
