"""Personal access tokens (docs/ACCOUNTS.md "Access tokens for automation"): the entitlement-token
verification, ``rxd login --token``, ``RADARX_TOKEN``, offline rules and the activity path.
Mocked account server and a made-up signing key; no real network, account or key."""

from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path

import jwt
import pytest

from engine import cli
from engine.account import activity, core, entitlement, jwks_seed, pat, tokens
from engine.account.errors import AccountRequired
from engine.account.store import SessionStore, StoredSession
from tests.account_helpers import ENT_KID, EntitlementKeys, KeyPair, new_pat

DAY = 86400
NOW = time.time()


def files_left(directory: Path) -> list[str]:
    """What a run left in the config folder that could be a sign-in: not the random install id
    (device_id, no secret), not lock files, and not the one-hour entitlement-token cache of
    RADARX_TOKEN (``env-token-*.json``: named by a hash, never holding the access token)."""
    if not directory.exists():
        return []
    ignore = ("device_id", "session.lock", "session.refresh.lock", "session.netdown")
    return sorted(
        p.name
        for p in directory.rglob("*")
        if p.is_file() and p.name not in ignore and not p.name.startswith("env-token-")
    )


def run(capsys, *argv):
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def stdin(monkeypatch, text: str):
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def login_with(gate_on, capsys, monkeypatch, token=None, **kw):
    token = token or gate_on.create_pat(**kw)
    stdin(monkeypatch, token + "\n")
    code, out, err = run(capsys, "login", "--token")
    return token, code, out, err


# -- the entitlement token and its verification ----------------------------------------------


def test_a_good_entitlement_token_verifies(gate_on):
    token = gate_on.ent.token(name="slurm", pat_expires=NOW + 30 * DAY)
    claims = tokens.verify_entitlement_token(token)
    assert claims.sub == "11111111-2222-3333-4444-555555555555" and claims.email
    assert claims.raw["src"] == "pat" and claims.raw["pat_name"] == "slurm"
    assert claims.exp - claims.iat == 3600 and not claims.expired(NOW)
    assert claims.expired(NOW + 7200)  # reported, not enforced here


def test_the_token_cannot_outlive_its_access_token(gate_on):
    # the function signs exp = min(iat + 1 h, pat_exp); a token claiming more is refused
    bad = gate_on.ent.token(extra={"exp": int(NOW + 5 * DAY)}, pat_expires=NOW + DAY)
    with pytest.raises(tokens.TokenInvalid) as caught:
        tokens.verify_entitlement_token(bad)
    assert caught.value.detail == "claims"


@pytest.mark.parametrize(
    ("make", "detail"),
    [
        (lambda k: k.token(iss="https://evil.example"), "issuer"),
        (lambda k: k.token(aud="authenticated"), "audience"),
        (lambda k: k.token(src="oauth"), "source"),
        (lambda k: k.token(src=None), "source"),
        (lambda k: k.token(drop=("jti",)), "claims"),
        (lambda k: k.token(drop=("pat_exp",)), "claims"),
        (lambda k: k.token(drop=("sub",)), "claims"),
        (lambda k: k.token(kid="somebody-else"), "unknown_key"),
        (lambda k: k.token(kid=""), "unknown_key"),
    ],
    ids=[
        "issuer",
        "audience",
        "src",
        "no-src",
        "no-jti",
        "no-pat_exp",
        "no-sub",
        "kid",
        "empty-kid",
    ],
)
def test_entitlement_claims_are_all_checked(gate_on, make, detail):
    with pytest.raises(tokens.TokenInvalid) as caught:
        tokens.verify_entitlement_token(make(gate_on.ent))
    assert caught.value.detail == detail


def test_the_signature_and_algorithm_are_pinned(gate_on):
    other = EntitlementKeys()  # same kid, a different private key
    with pytest.raises(tokens.TokenInvalid) as caught:
        tokens.verify_entitlement_token(other.token())
    assert caught.value.detail == "signature"
    # alg none, HS256 keyed with the public key (the classic confusion), RS256
    forged = [
        jwt.encode({"iss": "x"}, key=None, algorithm="none", headers={"kid": ENT_KID}),
        jwt.encode(
            gate_on.ent.jwks and {"iss": "radarx-desktop/entitlement/v1", "aud": "radarx-cli"},
            "secret-from-the-public-key-0123456789",
            algorithm="HS256",
            headers={"kid": ENT_KID},
        ),
    ]
    for token in forged:
        with pytest.raises(tokens.TokenInvalid) as caught:
            tokens.verify_entitlement_token(token)
        assert caught.value.detail == "algorithm"
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rs = gate_on.ent.token(alg="RS256", key=rsa_key)
    with pytest.raises(tokens.TokenInvalid) as caught:
        tokens.verify_entitlement_token(rs)
    assert caught.value.detail == "algorithm"
    for junk in ("", "a.b", "a.b.c.d", "x" * 9000, None, 5):
        with pytest.raises(tokens.TokenInvalid):
            tokens.verify_entitlement_token(junk)


def test_a_supabase_token_is_not_an_entitlement_token_and_the_reverse(gate_on):
    supabase = gate_on.keys.token()
    ent = gate_on.ent.token()
    with pytest.raises(tokens.TokenInvalid):
        tokens.verify_entitlement_token(supabase)
    with pytest.raises(tokens.TokenInvalid):
        tokens.verify_access_token(ent)
    assert tokens.verify_access_token(supabase).sub and tokens.verify_entitlement_token(ent).sub


def test_even_one_key_in_both_sets_keeps_the_two_kinds_apart(gate_on, monkeypatch):
    """The worst case: the owner reuses Supabase's private key (they cannot, but suppose). The
    issuer and audience still separate the kinds."""
    shared = KeyPair(kid="shared")
    monkeypatch.setenv("RADARX_ENTITLEMENT_JWKS", json.dumps(shared.jwks))
    tokens.reset_cache()
    gate_on.keys = shared
    entitlement_shaped = EntitlementKeys(kid="shared")
    entitlement_shaped.private = shared.private
    ent = entitlement_shaped.token()
    with pytest.raises(tokens.TokenInvalid) as caught:
        tokens.verify_access_token(ent)
    assert caught.value.detail in ("issuer", "audience")
    # a Supabase-shaped token that carries every claim an entitlement token needs
    supa = shared.token(extra={"jti": "j", "src": "pat", "pat_exp": int(NOW + DAY)})
    with pytest.raises(tokens.TokenInvalid) as caught:
        tokens.verify_entitlement_token(supa)
    assert caught.value.detail in ("issuer", "audience")
    assert tokens.verify_entitlement_token(ent).sub and tokens.verify_access_token(supa).sub


def test_the_entitlement_keys_are_never_fetched_or_read_from_a_file(gate_on):
    gate_on.calls.clear()
    with pytest.raises(tokens.TokenInvalid):
        tokens.verify_entitlement_token(EntitlementKeys(kid="never-seen").token())
    assert not any("jwks" in c[1] for c in gate_on.calls)  # no key document was requested
    SessionStore().directory.mkdir(parents=True, exist_ok=True)
    (SessionStore().directory / "jwks.json").write_text(json.dumps(gate_on.ent.jwks))
    with pytest.raises(tokens.TokenInvalid):
        tokens.verify_entitlement_token(EntitlementKeys(kid="never-seen").token())


def test_keys_come_from_the_seed_for_the_real_project_only(monkeypatch):
    from engine.account import config

    other = EntitlementKeys(kid="seed-key")
    monkeypatch.setattr(jwks_seed, "ENTITLEMENT_SEED_JWKS", other.jwks)
    monkeypatch.setattr(tokens, "ENTITLEMENT_SEED_JWKS", other.jwks)
    monkeypatch.delenv("RADARX_ENTITLEMENT_JWKS", raising=False)
    monkeypatch.delenv("RADARX_SUPABASE_URL", raising=False)
    assert [k["kid"] for k in tokens.entitlement_keys()] == ["seed-key"]
    # another project (a developer's stack) never trusts the production seed
    monkeypatch.setenv("RADARX_SUPABASE_URL", "http://127.0.0.1:54321")
    assert tokens.entitlement_keys() == []
    # an override works in a source checkout, never in an installer build
    monkeypatch.setenv("RADARX_ENTITLEMENT_JWKS", json.dumps(EntitlementKeys(kid="dev").jwks))
    assert [k["kid"] for k in tokens.entitlement_keys()] == ["dev"]
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.delenv("RADARX_SUPABASE_URL", raising=False)
    assert config.is_frozen() and [k["kid"] for k in tokens.entitlement_keys()] == ["seed-key"]
    monkeypatch.setenv("RADARX_ENTITLEMENT_JWKS", "not json")
    assert [k["kid"] for k in tokens.entitlement_keys()] == ["seed-key"]


def test_the_shipped_seed_is_well_formed():
    keys = jwks_seed.ENTITLEMENT_SEED_JWKS["keys"]
    for key in keys:  # empty until the owner adds the public half (docs/ACCOUNTS.md)
        assert key["kty"] == "EC" and key["crv"] == "P-256" and key["kid"]
        assert "d" not in key, "a PRIVATE key must never be in the repository"


def test_without_any_entitlement_key_the_message_says_it_is_not_set_up(gate_on, monkeypatch):
    monkeypatch.setenv("RADARX_ENTITLEMENT_JWKS", "")
    with pytest.raises(AccountRequired) as caught:
        core._verify(gate_on.ent.token(), NOW, "pat")
    assert caught.value.reason == "unavailable" and "not set up" in str(caught.value)


# -- rxd login --token -----------------------------------------------------------------------


def test_login_with_a_token_stores_a_token_session(gate_on, capsys, monkeypatch):
    token, code, out, err = login_with(gate_on, capsys, monkeypatch, name="slurm", days=30)
    assert code == 0, err
    assert "Signed in as ada@example.test with the access token 'slurm'" in out
    assert gate_on.exchange_calls == 1
    stored = SessionStore().load()
    assert stored.source == "pat" and stored.pat_token == token and stored.refresh_token is None
    path = SessionStore().path
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert token not in out + err


def test_a_token_session_runs_commands_and_whoami_says_so(gate_on, capsys, monkeypatch):
    login_with(gate_on, capsys, monkeypatch, name="nightly", days=30)
    code, out, _e = run(capsys, "modules", "--json")
    assert code == 0 and json.loads(out)
    code, out, _e = run(capsys, "whoami")
    assert code == 0 and "Signed in as ada@example.test" in out
    assert (
        "access token 'nightly'" in out and "session file" in out and "revoke it in the app" in out
    )
    info = json.loads(run(capsys, "whoami", "--json")[1])
    assert info["session"] == "pat" and info["token_name"] == "nightly"
    assert info["token_from"] == "session file" and info["renewed_by"] == "rxd"
    assert abs(info["token_expires_at"] - (gate_on.now + 30 * DAY)) < 5


def test_the_exchange_happens_once_per_entitlement_lifetime(gate_on, capsys, monkeypatch):
    login_with(gate_on, capsys, monkeypatch)
    for _ in range(20):
        core.reset_runtime_state()
        core.require_account()
    assert gate_on.exchange_calls == 1
    gate_on.now += 3600 - 60  # inside the renewal margin of the first hour's token
    core.reset_runtime_state()
    core.require_account()
    assert gate_on.exchange_calls == 2
    for _ in range(5):
        core.reset_runtime_state()
        core.require_account()
    assert gate_on.exchange_calls == 2


def test_login_token_input_rules(gate_on, capsys, monkeypatch):
    for text in ("", "\n"):
        stdin(monkeypatch, text)
        code, _o, err = run(capsys, "login", "--token")
        assert code == 2 and ("no password" in err or "readable" in err or "empty" in err)
    stdin(monkeypatch, "not-a-token\n")
    code, _o, err = run(capsys, "login", "--token")
    assert code == 2 and "not a radarx access token" in err and "not-a-token" not in err
    stdin(monkeypatch, "rxd_pat_" + "x" * 5000 + "\n")
    code, out, err = run(capsys, "login", "--token")
    assert code == 2 and "xxxxx" not in out + err
    for extra in (["--email", "a@b.c"], ["--password-stdin"], ["--browser"]):
        stdin(monkeypatch, new_pat() + "\n")
        code, _o, err = run(capsys, "login", "--token", *extra)
        assert code == 2 and "does not go with" in err
    assert gate_on.exchange_calls == 0 and not SessionStore().path.exists()


def test_login_token_with_surrounding_whitespace_and_crlf(gate_on, capsys, monkeypatch):
    token = gate_on.create_pat()
    stdin(monkeypatch, f"  {token}  \r\n")
    code, _o, err = run(capsys, "login", "--token")
    assert code == 0, err


def test_an_unknown_or_revoked_token_is_a_friendly_failure(gate_on, capsys, monkeypatch):
    stdin(monkeypatch, new_pat() + "\n")
    code, out, err = run(capsys, "login", "--token")
    assert code == 1 and "not valid" in err and "Access tokens" in err
    assert "rxd_pat_" not in out + err and not SessionStore().path.exists()
    token = gate_on.create_pat()
    gate_on.pats[token]["revoked"] = True
    stdin(monkeypatch, token + "\n")
    code, _o, err = run(capsys, "login", "--token")
    assert code == 1 and "not valid" in err


def test_the_answers_for_unknown_revoked_and_expired_look_the_same(gate_on, capsys, monkeypatch):
    unknown = new_pat()
    revoked = gate_on.create_pat()
    gate_on.pats[revoked]["revoked"] = True
    expired = gate_on.create_pat(days=-1)
    seen = set()
    for token in (unknown, revoked, expired):
        stdin(monkeypatch, token + "\n")
        code, out, err = run(capsys, "login", "--token")
        seen.add((code, out, err))
    assert len(seen) == 1


def test_a_server_problem_is_not_called_a_bad_token(gate_on, capsys, monkeypatch):
    monkeypatch.setattr(pat, "RETRY_BUDGET_S", 0.0)  # (the retrying has its own tests)
    token = gate_on.create_pat()
    for mode, expect in (
        ("rate_limited", "limiting access-token exchanges"),
        ("bad_gateway", "Cannot reach"),
    ):
        gate_on.exchange_mode = mode
        stdin(monkeypatch, token + "\n")
        code, _o, err = run(capsys, "login", "--token")
        assert code == 1 and expect in err and "not valid" not in err
    gate_on.exchange_mode = "ok"
    gate_on.mode = "unreachable"
    stdin(monkeypatch, token + "\n")
    code, _o, err = run(capsys, "login", "--token")
    assert code == 1 and "Cannot reach" in err


def test_the_token_never_appears_in_output_logs_or_the_print_command_line(
    gate_on, capsys, monkeypatch, caplog
):
    import logging

    caplog.set_level(logging.DEBUG)
    token = gate_on.create_pat()
    stdin(monkeypatch, token + "\n")
    code, out, err = run(capsys, "login", "--token", "--print-command")
    assert code == 0 and "--token" in out and token not in out + err + caplog.text
    for name in ("whoami", "logout"):
        code, out, err = run(capsys, name)
        assert token not in out + err
    assert token not in json.dumps(gate_on.log_rows) + json.dumps(gate_on.activity_rows)


def test_logout_of_a_token_session_removes_it_locally_and_says_how_to_revoke(
    gate_on, capsys, monkeypatch
):
    token, *_ = login_with(gate_on, capsys, monkeypatch)
    gate_on.calls.clear()
    code, out, _e = run(capsys, "logout")
    assert code == 0 and "Signed out" in out and "revoke it in the app" in out
    assert not SessionStore().path.exists()
    assert not any("/auth/v1/logout" in c[1] for c in gate_on.calls)  # no Supabase session to end
    assert gate_on.pats[token]["revoked"] is False  # revoking is done in the app


# -- renewal, offline rules, revocation -----------------------------------------------------


def test_a_revoked_token_ends_the_session_at_the_next_renewal(gate_on, capsys, monkeypatch):
    token, *_ = login_with(gate_on, capsys, monkeypatch)
    gate_on.pats[token]["revoked"] = True
    core.require_account()  # still inside the hour: the signed entitlement token is good
    gate_on.now += 3600
    core.reset_runtime_state()
    with pytest.raises(AccountRequired) as caught:
        core.require_account()
    assert caught.value.reason == "revoked" and "revoked or has expired" in str(caught.value)
    assert not SessionStore().path.exists()


def test_a_gateway_401_or_a_rate_limit_does_not_delete_the_session(gate_on, capsys, monkeypatch):
    token, *_ = login_with(gate_on, capsys, monkeypatch)
    for mode in ("bad_gateway", "rate_limited"):
        gate_on.exchange_mode = mode
        gate_on.now += 3700  # past the end of the hour's token and its small leeway
        core.reset_runtime_state()
        account = core.require_account()  # expired token, inside the offline allowance
        assert account.stale and SessionStore().load().pat_token == token


def test_offline_the_token_session_follows_the_30_day_rule(gate_on, capsys, monkeypatch):
    login_with(gate_on, capsys, monkeypatch, days=365)
    gate_on.mode = "unreachable"
    gate_on.now += 20 * DAY
    core.reset_runtime_state()
    account = core.require_account()
    assert account.stale and account.grace_days_left == 10
    gate_on.now += 11 * DAY
    core.reset_runtime_state()
    with pytest.raises(AccountRequired) as caught:
        core.require_account()
    assert caught.value.reason == "offline_expired"


def test_the_token_own_expiry_beats_the_offline_allowance(gate_on, capsys, monkeypatch):
    """A token that was given 2 days stops at 2 days, offline or not (it is signed into every
    entitlement token). The 30-day allowance is for the sign-in, not for a lapsed token."""
    login_with(gate_on, capsys, monkeypatch, days=2)
    gate_on.mode = "unreachable"
    gate_on.now += 1 * DAY
    core.reset_runtime_state()
    assert core.require_account().stale
    gate_on.now += 2 * DAY
    core.reset_runtime_state()
    with pytest.raises(AccountRequired) as caught:
        core.require_account()
    assert caught.value.reason == "expired" and "access token expired on" in str(caught.value)


def test_a_clock_set_backwards_does_not_revive_a_lapsed_token(gate_on, capsys, monkeypatch):
    login_with(gate_on, capsys, monkeypatch, days=2)
    gate_on.mode = "unreachable"
    gate_on.now += 3 * DAY
    core.reset_runtime_state()
    with pytest.raises(AccountRequired):
        core.require_account()
    gate_on.now -= 2 * DAY  # the clock is put back to when it still looked fine
    core.reset_runtime_state()
    with pytest.raises(AccountRequired):
        core.require_account()


def test_the_entitlement_hook_sees_the_claims_of_a_token_session(gate_on, capsys, monkeypatch):
    seen = []

    def policy(claims):
        seen.append(dict(claims))
        return claims.get("pat_name") != "denied"

    monkeypatch.setattr(entitlement, "POLICY", policy)
    login_with(gate_on, capsys, monkeypatch, name="allowed")
    assert seen and seen[-1]["src"] == "pat" and seen[-1]["pat_name"] == "allowed"
    gate_on.create_pat(name="denied")
    token = gate_on.create_pat(name="denied")
    stdin(monkeypatch, token + "\n")
    code, _o, err = run(capsys, "login", "--token")
    assert code == 1 and "not allowed" in err


def test_the_file_cannot_make_one_kind_of_token_pass_as_the_other(gate_on):
    store = SessionStore()
    base = {
        "user_id": "11111111-2222-3333-4444-555555555555",
        "email": "ada@example.test",
        "issued_at": gate_on.now,
        "high_water": gate_on.now,
    }
    # an entitlement token in a file that says "cli": checked as a Supabase token -> refused
    store.save(StoredSession(access_token=gate_on.ent.token(), source="cli", **base))
    with pytest.raises(AccountRequired) as caught:
        core.check_stored_session(store)
    assert caught.value.reason == "invalid"
    # a Supabase token in a file that says "pat": checked as an entitlement token -> refused
    store.save(
        StoredSession(access_token=gate_on.keys.token(), source="pat", pat_token=new_pat(), **base)
    )
    with pytest.raises(AccountRequired) as caught:
        core.check_stored_session(store)
    assert caught.value.reason == "invalid"


def test_the_app_does_not_replace_or_end_a_token_session(gate_on, capsys, monkeypatch):
    token, *_ = login_with(gate_on, capsys, monkeypatch)
    app_token = gate_on.keys.token()
    core.verify_presented_token(app_token, persist=True)
    stored = SessionStore().load()
    assert stored.source == "pat" and stored.pat_token == token
    core.end_presented_session()
    assert SessionStore().load() is not None  # the app signing out leaves it alone
    # an ordinary session IS ended by the app, as before
    SessionStore().clear()
    core.verify_presented_token(app_token, persist=True)
    core.end_presented_session()
    assert SessionStore().load() is None


def test_signing_in_with_a_password_replaces_a_stored_token(gate_on, capsys, monkeypatch):
    login_with(gate_on, capsys, monkeypatch)
    import getpass

    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: "correct horse 1")
    code, _o, err = run(capsys, "login", "--email", "ada@example.test")
    assert code == 0, err
    stored = SessionStore().load()
    assert stored.source == "cli" and stored.pat_token is None


def test_many_threads_exchange_once(gate_on, capsys, monkeypatch):
    login_with(gate_on, capsys, monkeypatch)
    gate_on.now += 3600
    core.reset_runtime_state()
    gate_on.exchange_calls = 0
    results = []

    def work():
        try:
            core.check_stored_session()
            results.append("ok")
        except Exception as exc:
            results.append(type(exc).__name__)

    threads = [threading.Thread(target=work) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == ["ok"] * 32 and gate_on.exchange_calls == 1


# -- RADARX_TOKEN (CI): nothing on disk ----------------------------------------------------------


def test_the_environment_token_exchanges_once_and_writes_nothing(gate_on, monkeypatch):
    token = gate_on.create_pat(name="ci-job")
    monkeypatch.setenv("RADARX_TOKEN", token)
    account = core.require_account()
    assert account.source == "pat" and account.origin == "env" and account.token_name == "ci-job"
    assert gate_on.exchange_calls == 1
    for _ in range(10):
        core.require_account()
    assert gate_on.exchange_calls == 1
    assert files_left(SessionStore().directory) == [], "CI must leave no session behind"


def test_the_environment_token_renews_hourly_in_memory(gate_on, monkeypatch):
    monkeypatch.setenv("RADARX_TOKEN", gate_on.create_pat())
    core.require_account()
    gate_on.now += 3600
    core.require_account()
    assert gate_on.exchange_calls == 2
    assert files_left(SessionStore().directory) == []


def test_the_environment_token_wins_over_a_stored_session(gate_on, capsys, monkeypatch):
    import getpass

    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: "correct horse 1")
    run(capsys, "login", "--email", "ada@example.test")
    monkeypatch.setenv("RADARX_TOKEN", gate_on.create_pat(name="env-wins"))
    assert core.require_account().token_name == "env-wins"
    code, out, _e = run(capsys, "whoami")
    assert code == 0 and "RADARX_TOKEN" in out and "memory only" in out
    info = json.loads(run(capsys, "whoami", "--json")[1])
    assert info["token_from"] == "RADARX_TOKEN"


@pytest.mark.parametrize(
    "value", ["not-a-token", "rxd_pat_short", "rxd_pat_" + "!" * 43, "x" * 100]
)
def test_a_malformed_environment_token_is_refused_without_a_network_call(
    gate_on, monkeypatch, value
):
    monkeypatch.setenv("RADARX_TOKEN", value)
    with pytest.raises(AccountRequired) as caught:
        core.require_account()
    assert caught.value.reason == "invalid" and "RADARX_TOKEN" in str(caught.value)
    assert value not in str(caught.value) and gate_on.exchange_calls == 0


def test_a_blank_environment_token_is_ignored(gate_on, monkeypatch):
    monkeypatch.setenv("RADARX_TOKEN", "   ")
    with pytest.raises(AccountRequired) as caught:
        core.require_account()
    assert caught.value.reason == "no_session"


def test_a_bad_environment_token_gives_exit_code_5(gate_on, capsys, monkeypatch):
    monkeypatch.setenv("RADARX_TOKEN", new_pat())  # well-formed, unknown to the server
    code, _o, err = run(capsys, "modules")
    assert code == 5 and "revoked or has expired" in err
    monkeypatch.setenv("RADARX_TOKEN", "junk")
    code, _o, err = run(capsys, "modules")
    assert code == 5 and "RADARX_TOKEN is not a radarx access token" in err


def test_the_environment_token_needs_the_server_and_says_what_to_do(gate_on, monkeypatch):
    monkeypatch.setenv("RADARX_TOKEN", gate_on.create_pat())
    gate_on.mode = "unreachable"
    with pytest.raises(AccountRequired) as caught:
        core.require_account()
    assert caught.value.reason == "unavailable"
    assert "once an hour" in str(caught.value) and "rxd login --token" in str(caught.value)


def test_the_environment_token_survives_a_short_outage_inside_a_process(gate_on, monkeypatch):
    monkeypatch.setenv("RADARX_TOKEN", gate_on.create_pat())
    core.require_account()
    gate_on.mode = "unreachable"
    gate_on.now += 3700
    account = core.require_account()
    assert account.stale and account.origin == "env"


def test_the_environment_token_in_an_installer_build_still_works(gate_on, monkeypatch):
    """RADARX_TOKEN is a feature, not a bypass: unlike RADARX_AUTH_DISABLED it is honoured when
    frozen (the keys come from the built-in seed there, so this checks the path, not the keys)."""
    monkeypatch.setenv("RADARX_TOKEN", "junk")
    monkeypatch.setattr("sys.frozen", True, raising=False)
    with pytest.raises(AccountRequired) as caught:
        core.require_account()
    assert caught.value.reason == "invalid"  # it was read and judged, not ignored


def test_many_threads_with_the_environment_token_exchange_once(gate_on, monkeypatch):
    monkeypatch.setenv("RADARX_TOKEN", gate_on.create_pat())
    results = []

    def work():
        results.append(core.require_account().token_name)

    threads = [threading.Thread(target=work) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 32 and gate_on.exchange_calls == 1


def test_logout_with_an_environment_token_says_it_still_signs_you_in(gate_on, capsys, monkeypatch):
    monkeypatch.setenv("RADARX_TOKEN", gate_on.create_pat())
    code, out, _e = run(capsys, "logout")
    assert code == 0 and "RADARX_TOKEN is set" in out


# -- the activity log of a token session ------------------------------------------------------


def test_a_stored_token_session_logs_through_the_function_not_the_table(
    gate_on, capsys, monkeypatch
):
    login_with(gate_on, capsys, monkeypatch)
    gate_on.log_rows.clear()
    gate_on.log_calls.clear()
    activity.record("command", surface="cli", command="info")
    assert activity.flush(timeout=3) >= 1
    assert gate_on.activity_rows == [], "a token session has no database token: no REST insert"
    assert gate_on.log_rows and gate_on.log_rows[-1]["event"] in ("command", "sign_in")
    auth = gate_on.log_calls[-1][0]
    assert (
        auth.startswith("Bearer ")
        and jwt.decode(auth[7:], options={"verify_signature": False})["src"] == "pat"
    )
    row = gate_on.log_rows[-1]
    assert set(row) <= {
        "event",
        "surface",
        "occurred_at",
        "device_id",
        "app_version",
        "os",
        "detail",
        "user_id",
    }
    assert row["detail"] in ({}, {"command": "info"}) and "uid" not in row


def test_the_environment_token_logs_from_memory_only(gate_on, monkeypatch):
    monkeypatch.setenv("RADARX_TOKEN", gate_on.create_pat())
    core.require_account()
    activity.record("command", surface="cli", command="plot")
    activity.record("module_run", surface="api", module="radarx_hid")
    assert files_left(SessionStore().directory) == [], "nothing is queued on disk"
    assert activity.flush(timeout=3) == 2
    assert [r["event"] for r in gate_on.log_rows] == ["command", "module_run"]
    assert gate_on.activity_rows == []
    assert activity.queued_count() == 0


def test_the_environment_token_events_use_the_token_current_at_send_time(gate_on, monkeypatch):
    monkeypatch.setenv("RADARX_TOKEN", gate_on.create_pat())
    core.require_account()
    activity.record("command", surface="cli", command="plot")
    first = core.env_token_credentials()[0]
    gate_on.now += 3600
    core.require_account()  # renewed
    activity.flush(timeout=3)
    sent = gate_on.log_calls[-1][0].removeprefix("Bearer ")
    assert sent != first and sent == core.env_token_credentials()[0]


def test_events_wait_when_the_function_is_not_reachable_or_not_deployed(gate_on, monkeypatch):
    monkeypatch.setenv("RADARX_TOKEN", gate_on.create_pat())
    core.require_account()
    activity.record("command", surface="cli", command="plot")
    gate_on.log_mode = "unreachable"
    assert activity.flush(timeout=2) == 0 and activity.queued_count() == 1
    gate_on.log_mode = "ok"
    assert activity.flush(timeout=2) == 1


def test_a_token_session_event_the_function_refuses_is_dropped_not_retried_forever(
    gate_on, monkeypatch
):
    monkeypatch.setenv("RADARX_TOKEN", gate_on.create_pat())
    core.require_account()
    activity.record("command", surface="cli", command="plot")
    gate_on.log_mode = "bad_request"
    activity.flush(timeout=2)
    assert activity.queued_count() == 0


def test_no_activity_without_a_verified_environment_token(gate_on, monkeypatch):
    monkeypatch.setenv("RADARX_TOKEN", gate_on.create_pat())  # never exchanged in this process
    activity.record("command", surface="cli", command="plot")
    assert activity.queued_count() == 0


# -- a real process against a real HTTP mock -----------------------------------------------------


def test_real_processes_login_with_a_token_and_use_the_environment_token(tmp_path):
    from engine.account import config as _config  # noqa: F401  (import check only)
    from tests.account_helpers import run_python
    from tests.gotrue_http import RotatingGoTrue

    with RotatingGoTrue() as server:
        token = server.create_pat(name="e2e", days=30)
        env = {
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "RADARX_CONFIG_DIR": str(tmp_path / "cfg"),
            "RADARX_SUPABASE_URL": server.url,
            "RADARX_SUPABASE_KEY": "sb_publishable_test_only",
            "RADARX_ENTITLEMENT_JWKS": json.dumps(server.ent.jwks),
        }
        base = ["-W", "ignore", "-m", "engine"]
        # (1) CI style: RADARX_TOKEN, nothing left behind
        ci = run_python([*base, "whoami", "--json"], env={**env, "RADARX_TOKEN": token})
        assert ci.returncode == 0, ci.stderr
        info = json.loads(ci.stdout)
        assert info["token_name"] == "e2e" and info["token_from"] == "RADARX_TOKEN"
        assert token not in ci.stdout + ci.stderr
        assert files_left(tmp_path / "cfg") == []
        for leftover in (tmp_path / "cfg").rglob("*"):
            assert not leftover.is_file() or token not in leftover.read_text(errors="ignore")
        # (2) stored: rxd login --token < file, then commands work with no variable
        login = run_python([*base, "login", "--token"], env=env, input=token + "\n")
        assert login.returncode == 0, login.stderr
        assert token not in login.stdout + login.stderr
        stored = json.loads((tmp_path / "cfg" / "session.json").read_text())
        assert stored["source"] == "pat" and stored["pat_token"] == token
        mods = run_python([*base, "modules", "--json"], env=env)
        assert mods.returncode == 0, mods.stderr
        # (3) a wrong token: exit 5 through the environment
        bad = run_python([*base, "modules"], env={**env, "RADARX_TOKEN": "rxd_pat_" + "A" * 43})
        assert bad.returncode == 5 and "revoked or has expired" in bad.stderr
