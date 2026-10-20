"""Exceptions of the account gate."""

from __future__ import annotations

SIGN_IN_HINT = "Sign in with `{prog} login` (or open the radarx-desktop app and sign in)."

#: Reason codes (stable; the app and the tests use them).
REASONS = {
    "no_session": "You are not signed in.",
    "invalid": "The saved sign-in could not be verified. Please sign in again.",
    "expired": "The saved sign-in has expired. Please sign in again.",
    "offline_expired": (
        "radarx could not reach the account server and the last successful check is more than "
        "{days} days ago. Connect to the internet and sign in again."
    ),
    "revoked": "This account session was ended on the server. Please sign in again.",
    "not_entitled": "This account is not allowed to use radarx-desktop.",
    "dependency": "The account check needs the Python packages PyJWT and cryptography.",
    "unavailable": "The account check could not be completed.",
}


class AccountRequired(Exception):  # noqa: N818 - the documented public name
    """Raised when radarx-desktop is used without a valid signed-in account.

    ``reason`` is a short code (``no_session``, ``invalid``, ``expired``, ``offline_expired``,
    ``revoked``, ``not_entitled``); ``str(exc)`` is a complete, friendly sentence.
    """

    #: True when the session that expired belongs to the desktop app (which renews it itself).
    app_owned: bool = False
    #: A shorter first sentence for a terminal that adds its own instructions below it.
    headline: str | None = None

    def __init__(self, reason: str = "no_session", message: str | None = None) -> None:
        self.reason = reason
        text = message or REASONS.get(reason, REASONS["unavailable"])
        super().__init__(text.format(days=_days()))
        self.message = str(self)


def _days() -> int:
    from engine.account.config import OFFLINE_GRACE_DAYS

    return OFFLINE_GRACE_DAYS


#: GoTrue error codes that mean "this refresh token / session / user is finished", as opposed to a
#: proxy, portal or outage answering with some other status.
REFUSAL_CODES = frozenset(
    {
        "invalid_grant",
        "refresh_token_not_found",
        "refresh_token_already_used",
        "session_not_found",
        "session_expired",
        "user_not_found",
        "user_banned",
    }
)


class AccountServerError(Exception):
    """The account server answered with an error (``status``: HTTP status, 0 = unreachable)."""

    def __init__(
        self, status: int, code: str = "", message: str = "", retry_after: float | None = None
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        #: seconds the server asked us to wait (``Retry-After``), when it said so
        self.retry_after = retry_after
        super().__init__(message or code or f"HTTP {status}")

    @property
    def unreachable(self) -> bool:
        """Not a definite answer from the account server: no connection, a server error, rate
        limiting, or anything a proxy or captive portal may say (403, 404, 407, 408 ...)."""
        return not self.definite_refusal

    @property
    def definite_refusal(self) -> bool:
        """The server itself said the refresh token or session is no good (GoTrue's own codes)."""
        return self.status in (400, 401, 403) and self.code.lower() in REFUSAL_CODES
