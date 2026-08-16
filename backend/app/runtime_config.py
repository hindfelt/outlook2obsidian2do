"""Runtime-mutable configuration: which provider and which model each agent uses.

Static settings (vault path, routes, host/port/TLS) stay in config.py. Anything
the task pane can change lives here and is persisted to config.local.json, which
is gitignored and never contains the API key.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .config import PROJECT_ROOT, settings

log = logging.getLogger(__name__)

CONFIG_FILE = Path(PROJECT_ROOT) / "config.local.json"

PROVIDERS = ("anthropic", "ollama")

# Offered in the task pane dropdown. Free-text is still accepted so a new model
# id works without a code change.
ANTHROPIC_MODELS = [
    {"id": "claude-opus-5", "label": "Claude Opus 5 - best judgment, priciest"},
    {"id": "claude-sonnet-5", "label": "Claude Sonnet 5 - balanced"},
    {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5 - fastest, cheapest"},
]

AGENTS = ("extractor", "router", "writer")


@dataclass(frozen=True)
class RuntimeConfig:
    provider: str = "anthropic"
    extractor_model: str = "claude-sonnet-5"
    router_model: str = "claude-haiku-4-5"
    writer_model: str = "claude-haiku-4-5"
    ollama_url: str = "http://localhost:11434"
    ollama_num_ctx: int = 16384
    # Sampling for local models. Not 0: under a JSON grammar gemma4 at
    # temperature 0 either loops ("workstreams workstreams ...") or emits an
    # empty array; 0.3 with Gemma's own top_k/top_p was measured stable.
    ollama_temperature: float = 0.3
    ollama_top_k: int = 64
    ollama_top_p: float = 0.95
    # Thinking models (gemma4, qwen3) spend the whole budget in a hidden
    # reasoning channel and hand the grammar nothing usable. Off by default.
    ollama_think: bool = False
    request_timeout_s: int = 900

    def model_for(self, agent: str) -> str:
        return getattr(self, f"{agent}_model")


_lock = threading.Lock()
_current: RuntimeConfig | None = None


def _defaults_from_env() -> RuntimeConfig:
    """Seed from .env so an existing setup keeps working with no config file."""
    base = RuntimeConfig()
    return replace(
        base,
        extractor_model=settings.extractor_model or base.extractor_model,
        router_model=settings.router_model or base.router_model,
        writer_model=settings.writer_model or base.writer_model,
    )


def _load() -> RuntimeConfig:
    cfg = _defaults_from_env()
    if CONFIG_FILE.exists():
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("ignoring unreadable %s: %s", CONFIG_FILE, exc)
            return cfg
        known = {f for f in asdict(cfg)}
        raw.pop("anthropic_api_key", None)  # never stored here
        cfg = replace(cfg, **{k: v for k, v in raw.items() if k in known})
    return cfg


def current() -> RuntimeConfig:
    global _current
    with _lock:
        if _current is None:
            _current = _load()
        return _current


def update(**changes) -> RuntimeConfig:
    """Validate, apply and persist. Unknown keys are rejected."""
    global _current
    cfg = current()
    valid = set(asdict(cfg))

    unknown = set(changes) - valid
    if unknown:
        raise ValueError(f"Unknown config field(s): {sorted(unknown)}")

    clean = {k: v for k, v in changes.items() if v is not None and v != ""}

    if "provider" in clean and clean["provider"] not in PROVIDERS:
        raise ValueError(
            f"provider must be one of {PROVIDERS}, got {clean['provider']!r}"
        )
    for agent in AGENTS:
        key = f"{agent}_model"
        if key in clean and not str(clean[key]).strip():
            raise ValueError(f"{key} cannot be blank")
    if "ollama_url" in clean and not str(clean["ollama_url"]).startswith("http"):
        raise ValueError("ollama_url must start with http:// or https://")

    new = replace(cfg, **clean)

    with _lock:
        _current = new
    try:
        CONFIG_FILE.write_text(
            json.dumps(asdict(new), indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        log.warning("could not persist %s: %s", CONFIG_FILE, exc)
    return new


# --- Ollama model reconciliation ---------------------------------------------


def _tagged(model_id: str) -> str:
    """Ollama reports `name:tag`; a config entry may omit the implied `:latest`."""
    return model_id if ":" in model_id else f"{model_id}:latest"


def _family(model_id: str) -> str:
    return model_id.split(":", 1)[0].lower()


def _substitute(missing: str, installed: list[dict]) -> str | None:
    """Largest installed model of the same family, or None.

    Never crosses families: the biggest thing on disk might be an abliterated
    model, and the extractor is the one agent that reads untrusted text.
    """
    same_family = [m for m in installed if _family(m["id"]) == _family(missing)]
    if not same_family:
        return None
    return max(same_family, key=lambda m: m.get("size", 0))["id"]


def reconcile_models() -> RuntimeConfig:
    """Point every agent at a model Ollama actually serves.

    A pulled model can be deleted out from under the config, and the resulting
    404 only surfaces on the first real call - minutes into a job. Checking the
    installed list up front turns that into a startup log line and a remap.
    """
    cfg = current()
    if cfg.provider != "ollama":
        return cfg

    # Local import: providers imports this module.
    from .providers import ProviderError, get_provider

    try:
        installed = get_provider("ollama").list_models()
    except ProviderError as exc:
        log.warning("could not list Ollama models (%s) - keeping configured models", exc)
        return cfg
    if not installed:
        log.warning("Ollama has no models pulled - keeping configured models")
        return cfg

    have = {_tagged(m["id"]) for m in installed}
    changes: dict[str, str] = {}
    unresolved: list[str] = []
    for agent in AGENTS:
        wanted = cfg.model_for(agent)
        if _tagged(wanted) in have:
            continue
        chosen = _substitute(wanted, installed)
        if chosen is None:
            unresolved.append(agent)
            log.warning(
                "%s: %r is not installed and no %s model is - pick one in Settings",
                agent, wanted, _family(wanted),
            )
            continue
        changes[f"{agent}_model"] = chosen
        log.warning("%s: %r is not installed - falling back to %r", agent, wanted, chosen)

    if not changes:
        if not unresolved:
            log.info("Ollama models ok: %s", sorted(have))
        return cfg
    return update(**changes)
