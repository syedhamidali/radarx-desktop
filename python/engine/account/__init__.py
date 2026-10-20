"""Accounts: who may use radarx-desktop (docs/ACCOUNTS.md).

Import this package freely: it only loads the standard library until a check runs.

``require_account()``       raise :class:`AccountRequired` unless a valid account is signed in
``AccountRequired``         the exception (also exported by ``rxd`` / ``engine.api``)
``entitlement_allows()``    the one place a future subscription check goes (:mod:`.entitlement`)
``activity.record()``       the one activity-log writer (:mod:`.activity`)
"""

from __future__ import annotations

from engine.account.core import (
    Account,
    check_stored_session,
    current_account,
    login_with_password,
    logout,
    require_account,
    verify_presented_token,
)
from engine.account.entitlement import entitlement_allows
from engine.account.errors import AccountRequired, AccountServerError

__all__ = [
    "Account",
    "AccountRequired",
    "AccountServerError",
    "check_stored_session",
    "current_account",
    "entitlement_allows",
    "login_with_password",
    "logout",
    "require_account",
    "verify_presented_token",
]
