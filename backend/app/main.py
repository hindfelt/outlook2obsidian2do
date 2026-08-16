"""FastAPI app.

Serves the API and the add-in's static files on one origin (no CORS, no
mixed-content block inside Outlook), and binds to loopback by default so the
config and credential endpoints are not reachable from the network.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import jobs, keychain
from .config import PROJECT_ROOT, settings
from .models import EmailPayload
from .providers import ProviderError, get_provider
from .runtime_config import AGENTS, PROVIDERS, current, reconcile_models, update
from .vault import route_catalog

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("outlook2obsidian")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # A model listed in config.local.json may have been deleted since it was
    # written. Remap now rather than fail on the first job.
    reconcile_models()
    yield


app = FastAPI(title="outlook2obsidian2do", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

class NoCacheStaticFiles(StaticFiles):
    """Serve the add-in files with caching disabled.

    Outlook's embedded webview caches aggressively and has no reload button, so
    an edited taskpane.js can keep running the old code with no visible sign
    that anything is stale. These files are tiny and served from loopback -
    there is nothing to gain from caching them.
    """

    def is_not_modified(self, response_headers, request_headers) -> bool:
        return False

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


ADDIN_DIR = PROJECT_ROOT / "addin"
if ADDIN_DIR.is_dir():
    app.mount("/addin", NoCacheStaticFiles(directory=ADDIN_DIR, html=True), name="addin")


# --- config / credentials -----------------------------------------------------


class ConfigUpdate(BaseModel):
    provider: str | None = None
    extractor_model: str | None = None
    router_model: str | None = None
    writer_model: str | None = None
    ollama_url: str | None = None
    # Write-only. Never echoed back by any endpoint.
    anthropic_api_key: str | None = None


def _config_payload() -> dict:
    # Opening the pane is a natural moment to re-check: Ollama may not have
    # been up when the backend started (both launch at login).
    cfg = reconcile_models()
    key = keychain.get_api_key()

    providers = {}
    for name in PROVIDERS:
        try:
            providers[name] = get_provider(name).health()
        except ProviderError as exc:
            providers[name] = {"provider": name, "ready": False, "detail": str(exc)}

    return {
        "provider": cfg.provider,
        "providers": PROVIDERS,
        "models": {agent: cfg.model_for(agent) for agent in AGENTS},
        "ollama_url": cfg.ollama_url,
        # Credential state only - the key itself is never sent to the client.
        "api_key_set": bool(key),
        "api_key_hint": keychain.hint(key),
        "credential_store": keychain.describe_store(),
        "health": providers,
        "available_models": {
            name: providers[name].get("models", []) or _safe_models(name)
            for name in PROVIDERS
        },
    }


def _safe_models(name: str) -> list:
    try:
        return get_provider(name).list_models()
    except ProviderError:
        return []


@app.get("/api/config")
def read_config() -> dict:
    return _config_payload()


@app.post("/api/config")
def write_config(body: ConfigUpdate) -> dict:
    data = body.model_dump(exclude_none=True)
    api_key = data.pop("anthropic_api_key", None)

    if api_key is not None:
        api_key = api_key.strip()
        if api_key:
            try:
                store = keychain.set_api_key(api_key)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=500, detail=f"Could not store the API key: {exc}"
                ) from exc
            log.info("API key updated (stored in %s)", store)
        else:
            keychain.clear_api_key()
            log.info("API key cleared")

    if data:
        try:
            update(**data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log.info("config updated: %s", sorted(data))
        # Switching to Ollama without naming models: the carried-over ids are
        # whatever was last configured, which may not be installed here.
        if "provider" in data and not any(f"{a}_model" in data for a in AGENTS):
            reconcile_models()

    return _config_payload()


@app.post("/api/config/test")
def test_config() -> dict:
    """Round-trip the configured provider with a tiny constrained call."""
    from .models import RoutingOutput

    cfg = current()
    try:
        result = get_provider().structured(
            model=cfg.model_for("router"),
            system=(
                "You return JSON only. Produce exactly one decision with "
                "task_index 0, route_id 'admin', confidence 1.0 and a short reason."
            ),
            user_content="Connectivity check.",
            output_format=RoutingOutput,
            max_tokens=2000,
        )
    except ProviderError as exc:
        return {"ok": False, "provider": cfg.provider, "detail": str(exc)}
    return {
        "ok": True,
        "provider": cfg.provider,
        "model": cfg.model_for("router"),
        "detail": f"{len(result.decisions)} decision(s) returned, schema honoured",
    }


# --- health / routes ----------------------------------------------------------


@app.get("/api/health")
def health() -> dict:
    cfg = current()
    return {
        "ok": True,
        "vault": str(settings.vault_path),
        "vault_exists": settings.vault_path.is_dir(),
        "routes": len(settings.routes),
        "provider": cfg.provider,
        "models": {agent: cfg.model_for(agent) for agent in AGENTS},
        "provider_health": _config_payload()["health"][cfg.provider],
    }


@app.get("/api/routes")
def routes() -> dict:
    return {"routes": route_catalog(), "fallback": settings.fallback_route_id}


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Bare https://localhost:8000/ is the obvious thing to type - send it
    somewhere useful instead of a bare 404."""
    return RedirectResponse(url="/addin/taskpane.html")


@app.get("/manifest.xml")
def manifest() -> FileResponse:
    path = ADDIN_DIR / "manifest.xml"
    if not path.exists():
        raise HTTPException(status_code=404, detail="manifest.xml not found")
    return FileResponse(path, media_type="application/xml")


# --- jobs ---------------------------------------------------------------------


@app.post("/api/tasks")
def create_tasks(email: EmailPayload) -> dict:
    """Queue an email. Returns immediately - poll /api/jobs/{id} for the result.

    A local model can take minutes, so this never blocks the task pane.
    """
    if not (email.subject or email.body):
        raise HTTPException(status_code=400, detail="Email has no subject and no body")

    # One cheap /api/tags call; catches a model deleted since startup before it
    # costs a job.
    cfg = reconcile_models()
    job = jobs.submit(email, cfg.provider)
    log.info(
        "%s from=%r subject=%r provider=%s dry_run=%s job=%s",
        "reused" if job.reused else "queued",
        email.sender, email.subject, cfg.provider, email.dry_run, job.id,
    )
    return job.public()


@app.get("/api/jobs/{job_id}")
def read_job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    return job.public()


@app.get("/api/jobs")
def list_jobs(limit: int = 10) -> dict:
    return {"jobs": jobs.recent(limit)}
