"""The entitlement hook: any valid account passes today; shows where a subscription is checked."""

from __future__ import annotations

import pytest

from engine.account import core, entitlement
from engine.account.errors import AccountRequired
from engine.account.store import SessionStore


def test_any_valid_account_is_entitled_today():
    assert entitlement.entitlement_allows({"sub": "u", "role": "authenticated"}) is True
    assert entitlement.entitlement_allows({}) is True
    assert entitlement.POLICY is entitlement.free_accounts


def test_a_future_subscription_claim_is_checked_in_this_one_place(gate_on, monkeypatch):
    """What switching on billing looks like: a webhook writes `subscription_status` to app_metadata
    (users cannot edit it; Supabase copies it into the signed token) and the policy reads it."""
    monkeypatch.setattr(entitlement, "POLICY", entitlement.subscription_required)

    paid = gate_on.keys.token(
        extra={
            "app_metadata": {"provider": "email", "plan": "pro", "subscription_status": "active"}
        }
    )
    trial = gate_on.keys.token(extra={"app_metadata": {"subscription_status": "trialing"}})
    free = gate_on.keys.token()
    lapsed = gate_on.keys.token(extra={"app_metadata": {"subscription_status": "canceled"}})
    # a user cannot grant themselves the claim: user_metadata is not looked at
    sneaky = gate_on.keys.token(extra={"user_metadata": {"subscription_status": "active"}})

    for token in (paid, trial):
        assert core.verify_presented_token(token, persist=False).email
    for token in (free, lapsed, sneaky):
        with pytest.raises(AccountRequired) as err:
            core.verify_presented_token(token, persist=False)
        assert err.value.reason == "not_entitled"
        assert "not allowed to use" in str(err.value)


def test_the_stored_session_goes_through_the_same_hook(gate_on, monkeypatch):
    core.login_with_password("ada@example.test", "correct horse 1")
    assert core.require_account()
    monkeypatch.setattr(entitlement, "POLICY", entitlement.subscription_required)
    core.reset_runtime_state()
    with pytest.raises(AccountRequired) as err:
        core.require_account()
    assert err.value.reason == "not_entitled"
    assert SessionStore().path.exists()  # not signed out: just not entitled
