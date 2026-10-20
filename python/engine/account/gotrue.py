"""A minimal client of the Supabase Auth (GoTrue) REST API: password sign-in, refresh, sign-out.

Only the public project URL and the PUBLISHABLE key are used; there is no secret on this machine.
The functions return plain dicts and raise :class:`AccountServerError`. The network call goes
through ``engine.account.http.request_json`` (tests replace it).
"""

from __future__ import annotations

from typing import Any

from engine.account import config, http
from engine.account.errors import AccountServerError


def _headers(access_token: str | None = None) -> dict[str, str]:
    return {
        "apikey": config.publishable_key(),
        "Authorization": f"Bearer {access_token or config.publishable_key()}",
    }


def _session(payload: Any) -> dict[str, Any]:
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("access_token"), str)
        or not isinstance(payload.get("refresh_token"), str)
    ):
        raise AccountServerError(0, "bad_response", "the account server sent something unexpected")
    return payload


def password_login(email: str, password: str) -> dict[str, Any]:
    """Sign in with e-mail and password. Returns the session (access and refresh token, user)."""
    out = http.request_json(
        "POST",
        f"{config.project_url()}/auth/v1/token?grant_type=password",
        headers=_headers(),
        body={"email": email, "password": password},
    )
    return _session(out)


def refresh(refresh_token: str) -> dict[str, Any]:
    """Exchange a refresh token for a new session (the old refresh token is spent)."""
    out = http.request_json(
        "POST",
        f"{config.project_url()}/auth/v1/token?grant_type=refresh_token",
        headers=_headers(),
        body={"refresh_token": refresh_token},
    )
    return _session(out)


def logout(access_token: str) -> None:
    """End this session on the server only (``scope=local``). Errors are the caller's to ignore."""
    http.request_json(
        "POST",
        f"{config.project_url()}/auth/v1/logout?scope=local",
        headers=_headers(access_token),
        body={},
    )


def friendly_login_error(exc: AccountServerError) -> str:
    """A sentence a beginner can act on for a failed sign-in."""
    if exc.status == 0 or exc.status >= 500:
        return "Cannot reach the account server. Check your internet connection and try again."
    code = (exc.code or "").lower()
    text = (exc.message or "").lower()
    if code == "email_not_confirmed" or "not confirmed" in text:
        return (
            "Your e-mail is not confirmed yet. Open the radarx-desktop app once "
            "to enter the code we sent you."
        )
    if code in {"invalid_credentials", "invalid_grant"} or "invalid login" in text:
        return "That e-mail and password do not match."
    if exc.status == 429 or "rate" in code or "rate limit" in text:
        return "Too many attempts. Wait a minute and try again."
    return "Sign-in was refused by the account server."
