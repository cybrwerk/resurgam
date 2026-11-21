from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from resurgam_api.api.v1.router import router as api_v1_router
from resurgam_api.config import get_settings
from resurgam_api.errors import register_exception_handlers
from resurgam_api.middleware import correlation_id_middleware
from resurgam_api.version import running_version


def create_app() -> FastAPI:
    # Docs live under /api/ so the appliance's reverse proxy (frontend/Caddyfile,
    # which only forwards /api/*) serves them on the same origin as the UI --
    # the WebGUI links to /api/docs and downloads /api/openapi.json.
    app = FastAPI(
        title="RESURGAM Control Plane",
        version=running_version(),
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        swagger_ui_parameters={"persistAuthorization": True},
    )

    # Added first so it wraps outermost — CORS headers must be present on
    # every response, including errors, and preflight OPTIONS requests must
    # be answered before any other middleware runs.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[get_settings().frontend_origin],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Signed session cookie for the OIDC login flow only (PKCE verifier +
    # nonce between /oidc/login and /oidc/callback) — reuses SESSION_SECRET
    # since it's the same trust boundary as the JWT sessions it also signs.
    # Settings' own validator (config.py) already refuses to start with an
    # empty session_secret outside dev_mode, so the placeholder below is
    # only ever reachable in dev_mode, where it's an acceptable zero-config
    # default for OIDC testing.
    app.add_middleware(SessionMiddleware, secret_key=get_settings().session_secret or "dev-insecure-placeholder")

    app.middleware("http")(correlation_id_middleware)
    register_exception_handlers(app)

    app.include_router(api_v1_router)

    return app


app = create_app()
