from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from routers.deps import (
    OWNER_PASSWORD,
    SHARE_VIEW_TOKEN,
    create_owner_token,
    get_access_level,
    require_owner,
)
from fastapi import Request

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str


@router.post("/login")
def login(payload: LoginRequest):
    if not OWNER_PASSWORD:
        raise HTTPException(
            status_code=500,
            detail="Server has no OWNER_PASSWORD configured -- set it in the environment.",
        )
    if payload.password != OWNER_PASSWORD:
        raise HTTPException(status_code=401, detail="Incorrect password")
    return {"token": create_owner_token(), "role": "owner"}


@router.get("/me")
def me(request: Request):
    return {"role": get_access_level(request)}


@router.get("/share_link", dependencies=[Depends(require_owner)])
def get_share_link():
    """Owner-only: returns the raw share token so the UI can build a
    shareable read-only URL like https://<frontend>/?share=<token>."""
    return {"share_token": SHARE_VIEW_TOKEN, "enabled": bool(SHARE_VIEW_TOKEN)}
