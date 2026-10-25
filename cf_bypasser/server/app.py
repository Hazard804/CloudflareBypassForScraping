import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
