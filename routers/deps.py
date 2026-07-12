import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request

from database import SessionLocal


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- Access control ---------------------------------------------------
#
# This app is single-owner (Robbie's personal library), but it's deployed
# on the open internet so it needs two access levels:
#   - "owner": full read/write access, via a password login that issues a
#     signed bearer token.
#   - "viewer": read-only access via a fixed share token in the URL/header,
#     for sending a link to other people without letting them edit anything.
#
# Enforcement happens here, not in the UI -- the frontend may also hide
# controls for viewers, but the real security boundary is this dependency
# rejecting any non-GET request that isn't from the owner.

AUTH_SECRET_KEY = os.environ.get("AUTH_SECRET_KEY", "dev-insecure-secret-change-me")
OWNER_PASSWORD = os.environ.get("OWNER_PASSWORD")
SHARE_VIEW_TOKEN = os.environ.get("SHARE_VIEW_TOKEN")
JWT_ALGORITHM = "HS256"
OWNER_TOKEN_TTL_DAYS = 30


def create_owner_token() -> str:
    payload = {
        "role": "owner",
        "exp": datetime.now(timezone.utc) + timedelta(days=OWNER_TOKEN_TTL_DAYS),
    }
    return jwt.encode(payload, AUTH_SECRET_KEY, algorithm=JWT_ALGORITHM)


def _is_valid_owner_token(token: str) -> bool:
    try:
        payload = jwt.decode(token, AUTH_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return False
    return payload.get("role") == "owner"


def get_access_level(request: Request) -> str:
    """Returns "owner", "viewer", or "anonymous" for the current request."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[len("bearer "):]
        if _is_valid_owner_token(token):
            return "owner"

    share_token = request.headers.get("x-share-token") or request.query_params.get("share")
    if SHARE_VIEW_TOKEN and share_token and share_token == SHARE_VIEW_TOKEN:
        return "viewer"

    return "anonymous"


def require_owner(request: Request) -> str:
    access = get_access_level(request)
    if access != "owner":
        raise HTTPException(status_code=403, detail="Owner login required for this action")
    return access


def require_reader(request: Request) -> str:
    access = get_access_level(request)
    if access not in ("owner", "viewer"):
        raise HTTPException(status_code=401, detail="Login or a valid share link is required")
    return access


def enforce_access(request: Request) -> str:
    """Router-level dependency: GET needs owner-or-viewer, writes need owner."""
    if request.method == "GET":
        return require_reader(request)
    return require_owner(request)
