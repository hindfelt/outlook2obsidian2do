"""Single entry point the agents call. Dispatches to the configured provider."""

from __future__ import annotations

import logging
import time
from typing import TypeVar

from pydantic import BaseModel

from .providers import ProviderError, get_provider
from .runtime_config import current

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def model_for(agent: str) -> str:
    return current().model_for(agent)


# Hosted models have context to spare; the cap there is about cost and about
# not feeding a 400-message thread to the extractor.
HOSTED_BODY_CHARS = 60_000
# Tokens kept free for the system prompt, the XML wrapper and the JSON output.
OLLAMA_RESERVED_TOKENS = 4_000
# Conservative for English prose with names, dates and markup.
CHARS_PER_TOKEN = 3
OLLAMA_MIN_BODY_CHARS = 4_000


def body_char_budget() -> int:
    """How much email body the extractor may send on the configured provider.

    Ollama does not reject a prompt longer than num_ctx - it truncates it and
    says nothing, so an over-long email would be extracted from a fragment. Size
    the body to the configured window instead.
    """
    cfg = current()
    if cfg.provider != "ollama":
        return HOSTED_BODY_CHARS
    usable = max(0, cfg.ollama_num_ctx - OLLAMA_RESERVED_TOKENS) * CHARS_PER_TOKEN
    return min(HOSTED_BODY_CHARS, max(OLLAMA_MIN_BODY_CHARS, usable))


def structured(
    *,
    agent: str,
    system: str,
    user_content: str,
    output_format: type[T],
    max_tokens: int = 16000,
) -> T:
    """One constrained call for `agent`, using whatever provider is configured."""
    cfg = current()
    provider = get_provider()
    model = cfg.model_for(agent)

    start = time.monotonic()
    try:
        result = provider.structured(
            model=model,
            system=system,
            user_content=user_content,
            output_format=output_format,
            max_tokens=max_tokens,
        )
    except ProviderError as exc:
        log.error("%s via %s/%s failed: %s", agent, provider.name, model, exc)
        raise
    log.info(
        "%s via %s/%s ok in %.1fs", agent, provider.name, model, time.monotonic() - start
    )
    return result
