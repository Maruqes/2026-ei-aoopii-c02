"""Authenticate the Discord bot's model and effort changes."""

import secrets

from fastapi import HTTPException, Request

from .config import Settings


def require_chatgpt_admin(request: Request, settings: Settings) -> None:
    expected = settings.chatgpt_admin_password.encode()
    if not expected:
        raise HTTPException(503, "Configura CHATGPT_ADMIN_PASSWORD no .env.")
    supplied = request.headers.get("x-chatgpt-admin-key", "").encode()
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(401, "Autenticação do administrador necessária.")
