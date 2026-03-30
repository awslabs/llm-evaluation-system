"""Local deployment ASGI entrypoint.

Wraps the production FastAPI app with middleware that injects
authentication headers, replacing oauth2-proxy/Cognito for local use.

Usage:
    uvicorn local.entrypoint:app --host 0.0.0.0 --port 8080
"""

from backend.api.main import app as production_app


class LocalAuthMiddleware:
    """ASGI middleware that injects X-Forwarded-User and X-Forwarded-Email headers.

    In production, oauth2-proxy sets these after Cognito authentication.
    In local mode, we simulate a single "local-user" identity.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope["headers"] = list(scope["headers"]) + [
                (b"x-forwarded-user", b"local-user"),
                (b"x-forwarded-email", b"local@localhost"),
            ]
        await self.app(scope, receive, send)


app = LocalAuthMiddleware(production_app)
