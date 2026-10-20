"""Personal access tokens: the client side of ``exchange-api-token`` (docs/ACCOUNTS.md).

A token (``rxd_pat_`` + 43 URL-safe characters) is created in the app and is the long-lived secret
of a batch job, a cron entry, CI or a cluster. It never goes to the database or to Supabase Auth:
it is shown to the Edge Function ``exchange-api-token``, which answers with a one-hour
*entitlement token* (a signed claim, not a database token) that this program verifies offline
(:func:`engine.account.tokens.verify_entitlement_token`).

Nothing here logs, prints or stores the token. It lives in the session file (mode 0600) only for
a session made by ``rxd login --token``, and only in memory for ``RADARX_TOKEN``.
"""

from __future__ import annotations

import os
import re
from typing import Any

from engine.account import config, http
from engine.account.errors import AccountServerError

#: ``rxd_pat_`` and 43 characters of URL-safe base64 (256 random bits).
_FORMAT = re.compile(r"rxd_pat_[A-Za-z0-9_-]{43}")

ENV_VARIABLE = "RADARX_TOKEN"


def valid_format(token: Any) -> bool:
    """True when ``token`` looks like an access token (the server decides whether it is one)."""
    return isinstance(token, str) and _FORMAT.fullmatch(token) is not None


def from_environment() -> str | None:
    """The token in ``RADARX_TOKEN``, or None when the variable is absent or blank."""
    value = os.environ.get(ENV_VARIABLE, "").strip()
    return value or None


def exchange(token: str, *, timeout: float = config.HTTP_TIMEOUT_S) -> dict[str, Any]:
    """Trade the access token for an entitlement token. Returns the server's answer
    (``entitlement_token``, ``expires_at``, ``token_name``, ``token_expires_at``).

    Raises :class:`AccountServerError`. :func:`definite_refusal` is True when the server itself
    said the token is unknown, revoked or expired; anything else (no network, 429, 5xx, a proxy
    page) is trouble to retry later and says nothing about the token.
    """
    out = http.request_json(
        "POST",
        config.functions_url("exchange-api-token"),
        headers={"apikey": config.publishable_key()},
        body={"token": token},
        timeout=timeout,
    )
    if (
        not isinstance(out, dict)
        or not isinstance(out.get("entitlement_token"), str)
        or not out["entitlement_token"]
    ):
        raise AccountServerError(0, "bad_response", "the account server sent something unexpected")
    return out


def definite_refusal(exc: AccountServerError) -> bool:
    """The exchange function itself answered ``401 invalid_token``: this token is finished. A 401
    from a gateway in front of it (a different body) is not this."""
    return exc.status == 401 and exc.code == "invalid_token"


def friendly_error(exc: AccountServerError) -> str:
    """A sentence a beginner can act on for a failed exchange."""
    if definite_refusal(exc):
        return (
            "That access token is not valid: it is unknown, revoked or has expired. "
            "Create a new one in the radarx-desktop app (Account > Access tokens)."
        )
    if exc.status == 429:
        return "Too many requests with this token or from this address. Wait a minute and retry."
    if exc.status == 404:
        return "Access tokens are not available on the account server yet."
    return "Cannot reach the account server. Check your internet connection and try again."
