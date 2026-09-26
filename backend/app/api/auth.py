import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User
from app.middleware.auth import get_current_user
from app.schemas.auth import (
    RegisterRequest, LoginRequest, RefreshRequest, TokenResponse, AuthUser,
    ForgotPasswordRequest, ForgotPasswordResponse, ResetPasswordRequest, ChangePasswordRequest,
)
from app.services.security import (
    hash_password, verify_password, create_access_token, create_refresh_token, decode_token,
    create_reset_token,
)
from app.services.email import email_enabled, send_password_reset

router = APIRouter(prefix="/auth", tags=["auth"])
log = logging.getLogger(__name__)


def _tokens(user: User, is_new: bool) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(str(user.id)),
        refresh_token=create_refresh_token(str(user.id)),
        user=AuthUser(
            id=str(user.id), username=user.username,
            display_name=user.display_name, is_new_user=is_new,
        ),
    )


@router.post("/register", response_model=TokenResponse, status_code=201)
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    if db.scalar(select(User.id).where(User.email == body.email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    if db.scalar(select(User.id).where(User.username == body.username)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Username taken")
    user = User(
        email=body.email,
        username=body.username,
        display_name=body.display_name,
        password_hash=hash_password(body.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _tokens(user, is_new=True)


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == body.email))
    if not user or not user.password_hash or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    return _tokens(user, is_new=False)


@router.post("/refresh", response_model=TokenResponse)
def refresh(body: RefreshRequest, db: Session = Depends(get_db)):
    payload = decode_token(body.refresh_token, expected_type="refresh")
    if not payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")
    user = db.get(User, uuid.UUID(payload["sub"]))
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    return _tokens(user, is_new=False)


@router.post("/forgot-password", response_model=ForgotPasswordResponse)
def forgot_password(body: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """Begin a password reset.

    Always responds 200 with the same body so the endpoint can't be used to
    probe which emails are registered. The reset token is only ever delivered
    by email — returning it in the response would let anyone who knows an
    email address reset that account's password.
    """
    user = db.scalar(select(User).where(User.email == body.email))
    generic = "If an account exists for that email, password reset instructions have been sent."
    if not user:
        return ForgotPasswordResponse(message=generic)

    token = create_reset_token(str(user.id))
    if email_enabled():
        send_password_reset(user.email, token)
    elif settings.environment != "production":
        # Local dev without an email service: the link goes to the server log
        # (never the HTTP response) so the reset flow can still be tested.
        log.warning("Password reset for %s (email not configured): %s/?reset_token=%s",
                    user.email, settings.frontend_base_url, token)
    return ForgotPasswordResponse(message=generic)


@router.post("/reset-password", response_model=TokenResponse)
def reset_password(body: ResetPasswordRequest, db: Session = Depends(get_db)):
    payload = decode_token(body.reset_token, expected_type="reset")
    if not payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired reset token")
    user = db.get(User, uuid.UUID(payload["sub"]))
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    user.password_hash = hash_password(body.new_password)
    db.commit()
    db.refresh(user)
    return _tokens(user, is_new=False)


@router.post("/change-password", response_model=TokenResponse)
def change_password(body: ChangePasswordRequest,
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Change password for a logged-in user (requires the current password)."""
    if not user.password_hash or not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Current password is incorrect")
    user.password_hash = hash_password(body.new_password)
    db.commit()
    db.refresh(user)
    return _tokens(user, is_new=False)


@router.delete("/account", status_code=204)
def delete_account(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """GDPR: delete the user and all cascading data."""
    db.delete(user)
    db.commit()
