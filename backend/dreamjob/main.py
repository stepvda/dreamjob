"""Dream Job API application factory (CR-407: Python backend, React frontend).

Routers are mounted from a declared list.  Each router owns one slice of the
requirements and is independently testable; the list below doubles as a map
from URL space to specification section.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from dreamjob.config import REPO_ROOT, get_settings
from dreamjob.db.migrator import migrate

log = logging.getLogger(__name__)

# (module under dreamjob.api.routers, url prefix, tag, spec sections)
ROUTERS: list[tuple[str, str, str]] = [
    ("auth",         "/api/auth",         "Authentication"),        # NFR-202
    ("profile",      "/api/profile",      "Profile"),               # FR-101..109
    ("enrichment",   "/api/enrichment",   "Enrichment"),            # FR-121..128
    ("directives",   "/api/directives",   "Directives"),            # FR-141..149
    ("campaigns",    "/api/campaigns",    "Campaigns"),             # FR-161..166, FR-185
    ("browser",      "/api/browser",      "Browser automation"),    # FR-201..208
    ("companies",    "/api/companies",    "Companies"),             # FR-221..246, FR-341..345
    ("opportunities", "/api/opportunities", "Opportunities"),       # FR-261..285, FR-381..383
    ("contacts",     "/api/contacts",     "Contacts"),              # FR-301..306
    ("applications", "/api/applications", "Applications"),          # FR-321..331
    ("mail",         "/api/mail",         "Mail"),                  # FR-325..327
    ("pipeline",     "/api/pipeline",     "Post-application"),      # FR-421..425
    ("monitoring",   "/api/monitoring",   "Monitoring"),            # FR-401..403
    ("intelligence", "/api/intelligence", "Dream-job intelligence"),# FR-381..385, FR-441..444
    ("networking",   "/api/networking",   "Networking & export"),   # FR-461..463
    ("admin",        "/api/admin",        "Administration"),        # FR-361..364
    ("overview",     "/api/overview",     "Journey"),               # workflow map
    ("learning",     "/api/learning",     "Responses & learning"),  # FR-285, FR-425
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.ensure_dirs()
    applied = migrate()
    if applied:
        log.info("Applied migrations: %s", ", ".join(applied))

    # Populate the source catalogue from the adapter registry (FR-161).
    try:
        from dreamjob.adapters import load_all  # noqa: PLC0415

        count = load_all()
        log.info("Source catalogue synchronised: %d adapters", count)
    except Exception:  # noqa: BLE001
        log.exception("Could not synchronise the source catalogue")

    # NFR-401: jobs interrupted by a restart are marked resumable.
    try:
        from dreamjob.jobs.runner import runner  # noqa: PLC0415

        orphans = await runner.resume_orphans()
        if orphans:
            log.info("Marked %d interrupted jobs as resumable", orphans)
    except Exception:  # noqa: BLE001
        log.exception("Could not reconcile interrupted jobs")

    yield


_FRIENDLY_FIELDS = {
    "password": "password",
    "email": "email address",
    "display_name": "name",
    "current_password": "current password",
    "new_password": "new password",
}


def _validation_detail(exc: RequestValidationError) -> str:
    """Flatten a validation error into one human-readable sentence.

    FastAPI/Pydantic return an array of ``{loc, msg, type}`` records. Raw they
    are noise (``body.password: String should have at least 9 characters``); a
    job seeker only needs the fix. The first error is the one that matters for
    a form, and naming the field makes it actionable.
    """
    errors = exc.errors()
    if not errors:
        return "The request could not be validated"
    first = errors[0]
    loc = [str(p) for p in first.get("loc", []) if p not in ("body", "query", "path")]
    field = loc[-1] if loc else None
    msg = first.get("msg", "")
    name = _FRIENDLY_FIELDS.get(field, field.replace("_", " ") if field else "")
    cleaned = msg
    # Pydantic's phrasing is mechanical; make it a sentence a person reads.
    for prefix in ("String should have at least ", "Value should have at least "):
        if cleaned.startswith(prefix):
            cleaned = f"must be at least {cleaned[len(prefix):].split(' character')[0]} characters"
            break
    if cleaned.startswith("value is not a valid email address"):
        cleaned = "is not a valid email address"
    text = f"{name} {cleaned}" if name else cleaned or "The request could not be validated"
    return text[0].upper() + text[1:]


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Dream Job",
        description="AI-assisted job discovery and application platform",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Turn FastAPI's array-of-objects validation detail into one readable line.
    # Without this, a bad field surfaces as a useless "Request failed (422)".
    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request, exc):  # noqa: ANN001
        detail = _validation_detail(exc)
        return JSONResponse(status_code=422, content={"detail": detail})

    for module_name, prefix, tag in ROUTERS:
        try:
            module = importlib.import_module(f"dreamjob.api.routers.{module_name}")
        except ModuleNotFoundError:
            log.warning("Router %s is not implemented yet; skipping", module_name)
            continue
        app.include_router(module.router, prefix=prefix, tags=[tag])

    @app.get("/api/health")
    def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "env": settings.env,
                "llm_provider": settings.llm_provider,
                "llm_configured": bool(settings.deepseek_api_key),
                "mail_backend": settings.mail_backend,
                "database": str(settings.abs_db_path),
            }
        )

    # Serve the built React SPA when present (CR-407).
    dist = REPO_ROOT / "frontend" / "dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{full_path:path}")
        def spa(full_path: str) -> FileResponse:
            candidate = dist / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")

    return app


app = create_app()


def main() -> None:  # pragma: no cover
    import uvicorn

    s = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run("dreamjob.main:app", host=s.host, port=s.port, reload=s.env == "development")


if __name__ == "__main__":  # pragma: no cover
    main()
