"""Configuration loaded from environment / .env."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Route:
    """A destination for extracted tasks inside the Obsidian vault."""

    id: str
    file: str  # vault-relative path, e.g. "00 Inbox/TODO.md"
    section: str  # markdown H2 heading the tasks are appended under
    description: str  # shown to the Router agent


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None
    vault_path: Path
    routes_file: Path
    routes: dict[str, Route]
    fallback_route_id: str
    # One line about whose mailbox this is, from routes.json. Interpolated into
    # the Extractor and Router system prompts, so relevance is judged against
    # the actual owner rather than a generic office worker.
    owner_context: str
    # What the Writer puts in bold when a task belongs to the mailbox owner.
    owner_name: str
    # Optional env seeds. None means "use runtime_config's per-agent default"
    # (Sonnet for the extractor, Haiku for router and writer).
    extractor_model: str | None
    router_model: str | None
    writer_model: str | None
    host: str
    port: int
    ssl_certfile: str | None
    ssl_keyfile: str | None
    allowed_origins: list[str] = field(default_factory=list)

    @property
    def public_base_url(self) -> str:
        scheme = "https" if self.ssl_certfile and self.ssl_keyfile else "http"
        return f"{scheme}://localhost:{self.port}"


DEFAULT_OWNER_CONTEXT = "a busy professional"
DEFAULT_OWNER_NAME = "Me"


def _load_routes(routes_file: Path) -> tuple[dict[str, Route], str, str, str]:
    with routes_file.open(encoding="utf-8") as fh:
        raw = json.load(fh)

    routes: dict[str, Route] = {}
    for entry in raw["routes"]:
        route = Route(
            id=entry["id"],
            file=entry["file"],
            section=entry["section"],
            description=entry["description"],
        )
        routes[route.id] = route

    fallback = raw.get("fallback_route_id", "unsorted")
    if fallback not in routes:
        raise ValueError(
            f"fallback_route_id {fallback!r} is not present in {routes_file}"
        )
    owner_context = str(raw.get("owner_context") or DEFAULT_OWNER_CONTEXT).strip()
    owner_name = str(raw.get("owner_name") or DEFAULT_OWNER_NAME).strip()
    return routes, fallback, owner_context, owner_name


def load_settings() -> Settings:
    vault_path = Path(
        os.environ.get("OBSIDIAN_VAULT_PATH", str(Path.home() / "Obsidian"))
    ).expanduser()

    routes_file = Path(
        os.environ.get("ROUTES_FILE", str(PROJECT_ROOT / "routes.json"))
    ).expanduser()

    routes, fallback, owner_context, owner_name = _load_routes(routes_file)

    port = int(os.environ.get("PORT", "8000"))
    certfile = os.environ.get("SSL_CERTFILE") or None
    keyfile = os.environ.get("SSL_KEYFILE") or None

    origins = [
        o.strip()
        for o in os.environ.get(
            "ALLOWED_ORIGINS",
            "https://outlook.office.com,https://outlook.office365.com,https://outlook.live.com",
        ).split(",")
        if o.strip()
    ]
    origins.append(f"https://localhost:{port}")
    origins.append(f"http://localhost:{port}")

    return Settings(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        vault_path=vault_path,
        routes_file=routes_file,
        routes=routes,
        fallback_route_id=fallback,
        owner_context=owner_context,
        owner_name=owner_name,
        extractor_model=os.environ.get("EXTRACTOR_MODEL") or None,
        router_model=os.environ.get("ROUTER_MODEL") or None,
        writer_model=os.environ.get("WRITER_MODEL") or None,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=port,
        ssl_certfile=certfile,
        ssl_keyfile=keyfile,
        allowed_origins=origins,
    )


settings = load_settings()
