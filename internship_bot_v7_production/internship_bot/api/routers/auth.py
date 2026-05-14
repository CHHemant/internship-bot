"""
Auth — JWT-based user accounts for the API.

Why add this: the bot uses your portal credentials and submits real applications.
Multi-user without auth = anyone on the same server can trigger submissions under your name.

Flow:
  POST /auth/register  → create account (email + password)
  POST /auth/login     → returns JWT access token (24h) + refresh token (30d)
  GET  /auth/me        → current user info
  All /api/* routes    → require Bearer token in Authorization header

Passwords: bcrypt hashed, never stored plain.
JWT secret: from JWT_SECRET env var — must be set, no default.

NOTE: single-user deployments can skip this entirely by setting
REQUIRE_AUTH=false in .env. All routes become open (fine for local use).

TODO: email verification before allowing submissions
TODO: password reset flow
"""

from __future__ import annotations
import os
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr

router = APIRouter(prefix="/auth", tags=["auth"])

# ── JWT setup ─────────────────────────────────────────────────────────────────
# import lazily so the whole app doesn't crash if python-jose not installed
def _get_jwt():
    try:
        from jose import JWTError, jwt
        return jwt, JWTError
    except ImportError:
        raise RuntimeError("python-jose not installed — run: pip install python-jose[cryptography]")

def _get_bcrypt():
    try:
        from passlib.context import CryptContext
        return CryptContext(schemes=["bcrypt"], deprecated="auto")
    except ImportError:
        raise RuntimeError("passlib not installed — run: pip install passlib[bcrypt]")

JWT_SECRET = os.environ.get("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24      # 24h
REFRESH_TOKEN_EXPIRE_DAYS = 30

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

# ── In-memory user store (swap for DB in production) ─────────────────────────
# key = email, value = {email, hashed_password, id, created_at}
# TODO: replace with SQLAlchemy UserORM table
_users: dict[str, dict] = {}


# ── Schemas ───────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"

class UserInfo(BaseModel):
    id: str
    email: str
    created_at: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _create_token(data: dict, expires_delta: timedelta) -> str:
    jwt, _ = _get_jwt()
    if not JWT_SECRET:
        raise RuntimeError("JWT_SECRET env var not set — cannot issue tokens")
    payload = {**data, "exp": datetime.now(timezone.utc) + expires_delta}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def _decode_token(token: str) -> dict:
    jwt, JWTError = _get_jwt()
    if not JWT_SECRET:
        raise HTTPException(500, "JWT_SECRET not configured")
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


# ── Dependency: get current user ──────────────────────────────────────────────

def require_auth() -> bool:
    """Returns False if REQUIRE_AUTH=false — open access for local dev."""
    return os.environ.get("REQUIRE_AUTH", "true").lower() != "false"

async def get_current_user(token: str = Depends(oauth2_scheme)) -> dict | None:
    """Dependency: inject into any route that needs auth."""
    if not require_auth():
        return {"id": "local", "email": "local@localhost"}  # dev bypass

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = _decode_token(token)
    email = payload.get("sub")
    user = _users.get(email)
    if not user:
        raise HTTPException(401, "User not found")
    return user

# shorthand for route params
CurrentUser = Annotated[dict, Depends(get_current_user)]


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/register", status_code=201)
async def register(req: RegisterRequest):
    if req.email in _users:
        raise HTTPException(400, "Email already registered")
    if len(req.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

    pwd_ctx = _get_bcrypt()
    import uuid
    user = {
        "id": str(uuid.uuid4())[:8],
        "email": req.email,
        "hashed_password": pwd_ctx.hash(req.password),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _users[req.email] = user
    return {"ok": True, "message": "Account created — now POST /auth/login"}


@router.post("/login", response_model=TokenResponse)
async def login(form: OAuth2PasswordRequestForm = Depends()):
    """Standard OAuth2 password flow. Returns JWT tokens."""
    pwd_ctx = _get_bcrypt()
    user = _users.get(form.username)
    if not user or not pwd_ctx.verify(form.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Wrong email or password",
        )
    access = _create_token(
        {"sub": user["email"], "uid": user["id"]},
        timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    refresh = _create_token(
        {"sub": user["email"], "uid": user["id"], "type": "refresh"},
        timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
    )
    return TokenResponse(access_token=access, refresh_token=refresh)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(token: str):
    """Swap refresh token for new access token."""
    payload = _decode_token(token)
    if payload.get("type") != "refresh":
        raise HTTPException(400, "Not a refresh token")
    access = _create_token(
        {"sub": payload["sub"], "uid": payload["uid"]},
        timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    return TokenResponse(access_token=access, refresh_token=token)


@router.get("/me", response_model=UserInfo)
async def me(user: CurrentUser):
    return UserInfo(id=user["id"], email=user["email"], created_at=user.get("created_at", ""))
