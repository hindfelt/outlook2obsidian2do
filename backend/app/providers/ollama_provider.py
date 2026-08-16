"""Local Ollama backend.

Nothing leaves the machine. Ollama's `format` parameter takes a JSON Schema and
compiles it to a decoding grammar, but it does not resolve `$ref`, so schemas are
inlined first (see schema_utils). A model can still emit valid JSON of the wrong
shape, so the result is validated with Pydantic before it is returned.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from ..runtime_config import current
from ..schema_utils import cap_strings, json_schema
from .base import ProviderError

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Longest string any agent legitimately produces is a checklist line of a few
# hundred chars; reasons, summaries and evidence quotes are shorter still.
# Keep this well under 2000: llama.cpp expands maxLength into the grammar, and
# at 2000 across the extractor's fields it fails to parse ("failed to
# initialize samplers"). 1500 was measured to work; 1000 leaves margin.
MAX_STRING_CHARS = 1000


class OllamaProvider:
    name = "ollama"

    def _post(self, path: str, payload: dict, timeout: int) -> dict:
        cfg = current()
        url = f"{cfg.ollama_url.rstrip('/')}{path}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:300]
            raise ProviderError(f"Ollama returned {exc.code}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(
                f"Could not reach Ollama at {cfg.ollama_url} ({exc}). "
                "Is `ollama serve` running?"
            ) from exc

    def _chat(self, payload: dict, timeout: int) -> dict:
        """POST /api/chat, dropping the `think` field if this model rejects it.

        Ollama returns 400 for `think` on some non-thinking models. Rather than
        keep a list of which models accept it, retry once without.
        """
        try:
            return self._post("/api/chat", payload, timeout)
        except ProviderError as exc:
            if "think" in payload and "400" in str(exc) and "think" in str(exc).lower():
                log.info("%s rejects the think option - retrying without", payload["model"])
                payload = {k: v for k, v in payload.items() if k != "think"}
                return self._post("/api/chat", payload, timeout)
            raise

    def structured(
        self,
        *,
        model: str,
        system: str,
        user_content: str,
        output_format: type[T],
        max_tokens: int = 16000,
    ) -> T:
        cfg = current()
        payload = {
            "model": model,
            "stream": False,
            "think": cfg.ollama_think,
            "format": cap_strings(json_schema(output_format), MAX_STRING_CHARS),
            "options": {
                "temperature": cfg.ollama_temperature,
                "top_k": cfg.ollama_top_k,
                "top_p": cfg.ollama_top_p,
                "num_ctx": cfg.ollama_num_ctx,
                "num_predict": max_tokens,
            },
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
        }

        # A grammar guarantees well-formed JSON of roughly the right shape, but
        # not that every constraint holds. Rather than discard a run that takes
        # minutes, feed the validation errors back once and let the model fix
        # them.
        last_error: str = ""
        for attempt in (1, 2):
            data = self._chat(payload, cfg.request_timeout_s)
            content = data.get("message", {}).get("content", "")
            if not content.strip():
                last_error = "returned an empty response"
            else:
                try:
                    raw = json.loads(content)
                except json.JSONDecodeError as exc:
                    last_error = f"did not return JSON ({exc}): {content[:200]!r}"
                else:
                    if not isinstance(raw, dict):
                        # Shape is a property of the model, not of this attempt -
                        # retrying will not help.
                        raise ProviderError(
                            f"{model} ignored the schema - returned a "
                            f"{type(raw).__name__}, expected an object. This "
                            f"model is not usable for this pipeline."
                        )
                    try:
                        return output_format.model_validate(raw)
                    except ValidationError as exc:
                        problems = exc.errors(include_url=False)[:3]
                        last_error = (
                            f"returned JSON that does not match the schema: {problems}"
                        )

            if attempt == 1:
                log.warning("%s %s - retrying once", model, last_error)
                payload["messages"] = [
                    *payload["messages"][:2],
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": (
                            f"That response was rejected: {last_error}. "
                            "Return the corrected JSON only, obeying every "
                            "constraint in the schema."
                        ),
                    },
                ]

        raise ProviderError(f"{model} {last_error}")

    def health(self) -> dict:
        try:
            models = self.list_models()
        except ProviderError as exc:
            return {"provider": self.name, "ready": False, "detail": str(exc), "models": []}
        return {
            "provider": self.name,
            "ready": bool(models),
            "detail": f"{len(models)} model(s) installed" if models else "no models pulled",
            "models": models,
        }

    def list_models(self) -> list[dict]:
        data = self._post_get("/api/tags")
        out = []
        for entry in data.get("models", []):
            size_gb = round(entry.get("size", 0) / 1_000_000_000, 1)
            out.append(
                {
                    "id": entry["name"],
                    "label": f"{entry['name']} ({size_gb} GB)",
                    "size": entry.get("size", 0),
                }
            )
        return sorted(out, key=lambda m: m["id"])

    def _post_get(self, path: str) -> dict:
        cfg = current()
        url = f"{cfg.ollama_url.rstrip('/')}{path}"
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(
                f"Could not reach Ollama at {cfg.ollama_url} ({exc}). "
                "Is `ollama serve` running?"
            ) from exc
