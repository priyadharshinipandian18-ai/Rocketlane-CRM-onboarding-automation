from fastapi import FastAPI

from src.api.routes.gmail_router import router as gmail_router
from src.api.routes.health_router import router as health_router
from src.api.routes.onboarding_router import router as onboarding_router
from src.api.routes.pubsub_router import router as pubsub_router
from src.api.routes.webhook_router import router as webhook_router


def register_routers(app: FastAPI) -> None:
    app.include_router(health_router)
    app.include_router(webhook_router)
    app.include_router(onboarding_router)
    app.include_router(gmail_router)
    app.include_router(pubsub_router)
