from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from cairn.server import hosted
from cairn.server.auth import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    TokenUser,
    UserInfoResponse,
    get_current_user,
    login_user,
    register_user,
)
from cairn.server.db import get_conn

router = APIRouter(tags=["auth"])


def _accounts_only() -> None:
    """A hosted deployment has one user, signed in by its front door: no Cairn accounts."""

    if hosted.front_door():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


@router.post("/auth/register", response_model=TokenResponse)
def register(body: RegisterRequest):
    _accounts_only()
    return register_user(body)


@router.post("/auth/login", response_model=TokenResponse)
def login(body: LoginRequest):
    _accounts_only()
    return login_user(body)


@router.get("/auth/me", response_model=UserInfoResponse)
def me(user: Annotated[TokenUser, Depends(get_current_user)]):
    with get_conn() as conn:
        row = conn.execute("SELECT id, username, created_at FROM users WHERE id = ?", (user.id,)).fetchone()
    if row is None:
        from fastapi import HTTPException, status

        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    return UserInfoResponse(id=row["id"], username=row["username"], created_at=row["created_at"])
