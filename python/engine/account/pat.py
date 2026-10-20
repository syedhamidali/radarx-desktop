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
import random
import re
import time
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


#: How long a rate-limited exchange keeps retrying (seconds in total), and the longest single wait.
RETRY_BUDGET_S = 40.0
RETRY_MAX_WAIT_S = 15.0


def exchange_with_retry(
    token: str,
    *,
    timeout: float = config.HTTP_TIMEOUT_S,
    budget: float = RETRY_BUDGET_S,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """:func:`exchange`, retried while the server answers ``429`` (rate limited) or ``503``: the
    wait is what the server asked for (``Retry-After``) or an exponential back-off, with jitter so
    a herd of jobs does not come back in step, and the whole retrying stays within ``budget``
    seconds. Anything else (a refusal of the token, no network) is raised at once. The last
    ``429`` is raised when the budget is used up: the caller says "rate limited", not "cannot
    reach the server"."""
    started = time.monotonic()
    waited = 0.0
    attempt = 0
    while True:
        try:
            return exchange(token, timeout=timeout)
        except AccountServerError as exc:
            if exc.status not in (429, 503):
                raise
            wait = exc.retry_after if exc.retry_after is not None else 2.0 * (2**attempt)
            wait = min(max(wait, 0.5), RETRY_MAX_WAIT_S) * random.uniform(1.0, 1.4)
            if max(time.monotonic() - started, waited) + wait > budget:
                raise
            attempt += 1
            waited += wait
            sleep(wait)


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
        wait = f" (the server asks to wait {exc.retry_after:.0f} s)" if exc.retry_after else ""
        return (
            "The account server is limiting access-token exchanges right now"
            f"{wait}. Nothing is wrong with the token: wait a little and run the command again."
        )
    if exc.status == 404:
        return "Access tokens are not available on the account server yet."
    return "Cannot reach the account server. Check your internet connection and try again."
