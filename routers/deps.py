import hmac
import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

import models
from database import SessionLocal


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- Current profile (library) -----------------------------------------
#
# Profiles are data separation, not a security boundary (see the
# multi-profile plan) -- this is deliberately *not* part of the JWT/auth
# scheme above. It's a plain request header, resolved here so every
# library-scoped router can depend on it the same way they depend on
# get_db. A request with no header at all (any client that predates this
# feature) resolves to the default profile, so nothing breaks during
# rollout.
def get_current_profile_id(request: Request, db: Session = Depends(get_db)) -> str:
    requested_id = request.headers.get("x-profile-id")
    if requested_id:
        profile = db.query(models.Profile).filter(models.Profile.id == requested_id).first()
        if not profile:
            raise HTTPException(status_code=400, detail=f"Unknown profile: {requested_id}")
        return profile.id

    default_profile = db.query(models.Profile).filter(models.Profile.is_default.is_(True)).first()
    if default_profile:
        return default_profile.id

    # Defensive fallback only -- migrations always seed at least one
    # profile, so this should never actually be hit outside of a
    # mid-migration or misconfigured database.
    any_profile = db.query(models.Profile).first()
    if any_profile:
        return any_profile.id

    raise HTTPException(status_code=500, detail="No profiles configured")


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


# --- Backup automation ---------------------------------------------------
#
# A scheduled job (e.g. a GitHub Actions cron) needs to pull a database
# export unattended. Reusing the owner password/JWT for that would mean a
# long-lived credential with full read/write/delete access sitting in a
# third place (CI secrets). Instead, BACKUP_TOKEN is a single-purpose shared
# secret that only ever grants read access to the one export endpoint --
# if it leaks, the worst case is someone can download a copy of the
# library, not edit or delete it.

BACKUP_TOKEN = os.environ.get("BACKUP_TOKEN")


def require_owner_or_backup_token(request: Request) -> str:
    provided = request.headers.get("x-backup-token")
    if provided is not None:
        # .strip() on both sides: a stray trailing newline/space from
        # copy-pasting the token into an env var UI is an easy, silent way
        # to end up with a token that "looks" right but never matches.
        expected = (BACKUP_TOKEN or "").strip()
        if expected and hmac.compare_digest(provided.strip(), expected):
            return "backup"
        # Distinct from the generic "Owner login required" below: this
        # tells you a backup token was actually sent and rejected, instead
        # of silently falling through and leaving you to guess whether the
        # header didn't arrive, the server doesn't have BACKUP_TOKEN set
        # (yet, or at all), or the value just doesn't match.
        raise HTTPException(status_code=403, detail="Invalid backup token")
    return require_owner(request)
