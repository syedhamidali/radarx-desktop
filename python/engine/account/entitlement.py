"""THE place that decides whether a signed-in account may use radarx-desktop.

Today every valid, signed-in account is enough (the app is free; there is no billing). When
charging starts, change the policy here and nowhere else: every surface (the app, the engine's
WebSocket server, ``rxd``, ``import rxd``) calls :func:`entitlement_allows` through
``engine.account.require_account`` / ``verify_presented_token``.

How a future subscription would be checked
------------------------------------------
A billing webhook (a server-side function holding the service-role key, NOT this repository)
writes the subscription state into the user's ``app_metadata`` (for example
``{"plan": "pro", "subscription_status": "active"}``). Users cannot edit ``app_metadata``; Supabase
copies it into the signed access token, so this function can read it from the verified ``claims``
with no extra network call. See :func:`subscription_required` for the shape of that policy, and
``tests/test_account_entitlement.py`` for a test that switches to it.

Limits to keep in mind then (also said in docs/ACCOUNTS.md): claims are as old as the access token
(up to its one hour lifetime plus the 30-day offline grace), and any check that runs on the user's
machine can be removed from a source-available program. Real enforcement of paid features needs
server-side features (a hosted service, signed downloads, server-side computation).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

Claims = Mapping[str, Any]


def free_accounts(claims: Claims) -> bool:
    """Policy for now: any valid signed-in account is allowed."""
    return True


#: Statuses a billing webhook would write that count as paid up.
_PAID = frozenset({"active", "trialing"})


def subscription_required(claims: Claims) -> bool:
    """Policy for later (NOT used today): the account needs an active or trialing subscription."""
    meta = claims.get("app_metadata")
    status = meta.get("subscription_status") if isinstance(meta, Mapping) else None
    return status in _PAID


#: The active policy. Replace with :func:`subscription_required` when billing exists.
POLICY: Callable[[Claims], bool] = free_accounts


def entitlement_allows(claims: Claims) -> bool:
    """True when the account described by the verified token ``claims`` may use radarx-desktop."""
    return bool(POLICY(claims))
