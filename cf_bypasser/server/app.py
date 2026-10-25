import hmac
import logging
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from cf_bypasser.utils.constants import APP_VERSION
from cf_bypasser.utils.security import AccessLogRedactor
from .routes import setup_routes, lifespan


def create_app() -> FastAPI:
    app = FastAPI(
        title="Cloudflare Bypasser",
        description="DrissionPage-based Cloudflare bypasser with request mirroring",
        version=APP_VERSION,
        lifespan=lifespan
    )

    auth_token = os.environ.get("CF_AUTH_TOKEN", "")
    if auth_token:
        expected = auth_token.encode()

        @app.middleware("http")
        async def require_auth_token(request: Request, call_next):
            given = request.headers.get("x-auth-token", "").encode()
            if not hmac.compare_digest(given, expected):
                return JSONResponse(status_code=401, content={"detail": "Missing or invalid x-auth-token header"})
            return await call_next(request)

    # added after the auth middleware so it wraps it: CORS preflights are answered without a token
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] %(levelname)s - %(name)s - %(message)s',
        datefmt='%H:%M:%S'
    )

    access_log = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessLogRedactor) for f in access_log.filters):
        access_log.addFilter(AccessLogRedactor())

    setup_routes(app)
    return app
