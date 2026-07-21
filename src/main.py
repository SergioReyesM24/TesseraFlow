from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from api.middleware import request_logging_middleware
from api.routes import router
from bootstrap import build_container
from config import Settings, get_settings
from infrastructure.logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the FastAPI application and bind its process-level lifecycle."""
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.log_json)
    logger = structlog.get_logger(__name__)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Create shared resources on startup and close them during shutdown."""
        container = await build_container(settings)
        app.state.container = container
        container.start()
        logger.info("application_started", app_name=settings.app_name, model=settings.openai_model)
        try:
            yield
        finally:
            await container.close()
            logger.info("application_stopped", app_name=settings.app_name)

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.middleware("http")(request_logging_middleware)
    app.include_router(router)
    return app


app = create_app()
