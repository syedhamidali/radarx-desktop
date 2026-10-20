"""The account gate: ``require_account()`` and the helpers behind ``rxd login`` / the engine.

One decision procedure serves every surface (see docs/ACCOUNTS.md for the rules in prose):

1. Read the shared session file (:mod:`engine.account.store`).
2. Verify the access token's signature, issuer and audience OFFLINE against the project's public
   key (:mod:`engine.account.tokens`). A token that does not verify is never accepted.
3. Ask the entitlement hook (:mod:`engine.account.entitlement`) whether the account may use the
   program (today: any valid account).
4. If the token has not expired, the account is good. If it has: a session made by ``rxd login``
   (it holds a refresh token) is refreshed online; a session handed over by the app is refreshed
   by the app itself, which hands the new token over (two programs must never rotate one refresh
   token).
   A session made with a personal access token (``source == "pat"``, :mod:`engine.account.pat`)
   holds a one-hour *entitlement token* instead of a Supabase access token. It is verified against
   its own keys (:func:`engine.account.tokens.verify_entitlement_token`), renewed by exchanging the
   access token again, and refused at once after the access token's own expiry date.
5. If the server cannot be reached, the session is still accepted for ``OFFLINE_GRACE_DAYS`` (30)
   after the ``iat`` of its newest token, the last time the server vouched for the account. A clock
   set backwards cannot extend that: the latest clock reading ever seen is kept (``high_water``)
   and the later of the two is used.
"""

from __future__ import annotations

import contextlib
import math
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from engine.account import config, gotrue, pat, tokens
from engine.account.entitlement import entitlement_allows
from engine.account.errors import AccountRequired, AccountServerError
from engine.account.store import SessionStore, StoredSession

#: Refresh a ``rxd login`` token this long before it expires (so it does not lapse mid-command).
REFRESH_MARGIN_S = 120.0
#: After the account server was unreachable, do not retry the network for this long (per process).
NETWORK_RETRY_S = 60.0
#: After one process found the account server unreachable, the others that share the session folder
#: skip the network for this long (so a herd of jobs does not wait out the same timeout each).
SHARED_NETDOWN_S = 15.0
#: How long one successful check is reused inside a process (the API calls it on every function).
CACHE_TTL_S = 30.0
#: Network limits (seconds, in total) of the calls a command may make while checking the account.
REFRESH_TIMEOUT_S = 5.0
PROBE_TIMEOUT_S = 2.0
#: A token issued this recently (by the server's clock) agrees with the local clock closely
#: enough to
#: reset the "latest clock reading" mark (see check_stored_session).
FRESH_TOKEN_S = 600.0


def now() -> float:
    """The clock the gate uses (a function so tests can move it)."""
    return time.time()


@dataclass
class Account:
    """A verified signed-in account."""

    user_id: str
    email: str
    source: str  # "cli" | "app" | "pat" | "bypass"
    expires_at: float = 0.0
    issued_at: float = 0.0
    #: True when the access token has expired and the session is carried by the offline grace.
    stale: bool = False
    #: Whole days left of the offline grace (None when the token is current).
    grace_days_left: int | None = None
    claims: Mapping[str, Any] = field(default_factory=dict)
    #: for a token session: where the access token came from, "file" (rxd login --token) or "env"
    origin: str = ""

    @property
    def bypassed(self) -> bool:
        return self.source == "bypass"

    @property
    def is_token(self) -> bool:
        return self.source == "pat"

    @property
    def token_name(self) -> str:
        return str(self.claims.get("pat_name") or "") if self.is_token else ""

    @property
    def token_expires_at(self) -> float | None:
        value = self.claims.get("pat_exp") if self.is_token else None
        return float(value) if isinstance(value, (int, float)) else None


_BYPASS = Account(user_id="", email="", source="bypass")

_state_lock = threading.Lock()
_cache: dict[str, Any] = {"path": None, "until": 0.0, "account": None}
_net_down_until = 0.0


def reset_runtime_state() -> None:
    """Forget cached checks and network back-off (tests; also called by login / logout)."""
    global _net_down_until
    with _state_lock:
        _cache.update(path=None, until=0.0, account=None)
        _net_down_until = 0.0
    with _env_lock:
        _env_state.update(pat=None, token=None, down_until=0.0)
    with contextlib.suppress(Exception):  # the shared "server unreachable" note goes too
        SessionStore().clear_network_down()


def _verify(token: str, at: float, source: str = "cli") -> tokens.Claims:
    """Verify ``token`` as what ``source`` says it is: an entitlement token for a token session,
    a Supabase access token for everything else. Never the other way round."""
    try:
        if source == "pat":
            claims = tokens.verify_entitlement_token(token, now=at)
        else:
            claims = tokens.verify_access_token(token, now=at)
    except tokens.DependencyMissing as exc:
        raise AccountRequired("dependency") from exc
    except tokens.TokenInvalid as exc:
        if source == "pat" and exc.detail == "unknown_key" and not tokens.entitlement_keys():
            raise AccountRequired(
                "unavailable",
                "Access tokens are not set up in this build yet (no entitlement key). "
                "Sign in with `rxd login` instead.",
            ) from exc
        raise AccountRequired("invalid") from exc
    if not entitlement_allows(claims.raw):
        raise AccountRequired("not_entitled")
    return claims


def _account_from(claims: tokens.Claims, source: str, at: float, origin: str = "") -> Account:
    expired = claims.expired(at)
    left: int | None = None
    if expired:
        left = max(
            0,
            math.ceil(
                (config.OFFLINE_GRACE_DAYS * config.DAY_S - (at - min(claims.iat, at)))
                / config.DAY_S
            ),
        )
    return Account(
        user_id=claims.sub,
        email=claims.email,
        source=source,
        expires_at=claims.exp,
        issued_at=min(claims.iat, at),
        stale=expired,
        grace_days_left=left,
        claims=claims.raw,
        origin=origin,
    )


def _within_grace(claims: tokens.Claims, at: float) -> bool:
    return at - min(claims.iat, at) <= config.OFFLINE_GRACE_DAYS * config.DAY_S


# -- the stored session (CLI, API, engine recorder) ---------------------------------------------


def _store_new_session(
    store: SessionStore, session: Mapping[str, Any], at: float
) -> tuple[StoredSession, tokens.Claims]:
    claims = _verify(session["access_token"], at)
    stored = StoredSession(
        access_token=session["access_token"],
        refresh_token=session["refresh_token"],
        user_id=claims.sub,
        email=claims.email,
        source="cli",
        issued_at=min(claims.iat, at),
        high_water=at,
    )
    store.save(stored)
    store.clear_network_down()
    return stored, claims


def _refresh_stored(
    store: SessionStore, seen: StoredSession, at: float, wall: float, timeout: float
) -> tuple[StoredSession, tokens.Claims, bool] | None:
    """Refresh a ``rxd login`` session under the file lock. None = the server was unreachable.

    Raises ``AccountRequired('revoked')`` only when the server itself says the refresh token is
    finished (GoTrue's own error codes); a proxy, portal or outage answering anything else is
    "unreachable" and keeps the session. The rotated tokens are saved BEFORE the new access token
    is verified, so a verification problem can never throw away the only valid refresh token.
    """
    global _net_down_until
    if time.monotonic() < _net_down_until:
        return None
    try:
        with store.refresh_locked():
            current = store.load()
            if current is None:
                raise AccountRequired("no_session")
            if current.access_token != seen.access_token:
                # another process refreshed while we waited for the lock: use its result
                claims = _verify(current.access_token, at)
                if not claims.expired(at) and claims.exp - at > REFRESH_MARGIN_S:
                    return current, claims, False
                seen = current
            if not seen.refresh_token:
                return None
            if store.network_down_recently(now(), SHARED_NETDOWN_S):
                _net_down_until = time.monotonic() + NETWORK_RETRY_S
                return None  # another process just found the server unreachable: do not wait again
            try:
                session = gotrue.refresh(seen.refresh_token, timeout=timeout)
            except AccountServerError as exc:
                if exc.definite_refusal:
                    # the server itself says this refresh token is finished. Delete the file only
                    # while it still holds exactly the token that was refused: if somebody saved
                    # a different one meanwhile (the file system was slow to show it), keep it.
                    latest = store.load()
                    if latest is None or latest.refresh_token == seen.refresh_token:
                        store._unlink()
                        raise AccountRequired("revoked") from exc
                    return None
                _net_down_until = time.monotonic() + NETWORK_RETRY_S
                store.mark_network_down(now())
                return None
            store.clear_network_down()
            # the server just vouched for the account: save the rotated tokens first ...
            provisional = StoredSession(
                access_token=session["access_token"],
                refresh_token=session["refresh_token"],
                user_id=current.user_id,
                email=current.email,
                source="cli",
                issued_at=current.issued_at,
                high_water=current.high_water,
            )
            store._write(provisional)
            # ... then check them
            claims = _verify(session["access_token"], wall)
            stored = StoredSession(
                access_token=session["access_token"],
                refresh_token=session["refresh_token"],
                user_id=claims.sub,
                email=claims.email,
                source="cli",
                issued_at=min(claims.iat, wall),
                high_water=wall,
            )
            store._write(stored)
            return stored, claims, True
    except TimeoutError:
        again = store.load()
        if again is None:
            return None
        return again, _verify(again.access_token, at), False


# -- personal access tokens (rxd login --token, RADARX_TOKEN) -----------------------------------


def _pat_expired_error(claims: tokens.Claims) -> AccountRequired:
    when = claims.raw.get("pat_exp")
    date = (
        time.strftime("%Y-%m-%d", time.gmtime(float(when)))
        if isinstance(when, (int, float))
        else ""
    )
    return AccountRequired(
        "expired",
        f"This access token expired{' on ' + date if date else ''}. Create a new one in the "
        "radarx-desktop app (Account > Access tokens).",
    )


def _check_pat_expiry(claims: tokens.Claims, at: float) -> None:
    """The access token's own expiry date is signed into every entitlement token and holds even
    offline: the 30-day allowance never lets a token live past the date it was given."""
    pat_exp = claims.raw.get("pat_exp")
    if isinstance(pat_exp, (int, float)) and at > float(pat_exp) + config.LEEWAY_S:
        raise _pat_expired_error(claims)


def _revoked_token_error() -> AccountRequired:
    return AccountRequired(
        "revoked",
        "This access token was revoked or has expired. Create a new one in the radarx-desktop "
        "app (Account > Access tokens).",
    )


def _near_end(claims: tokens.Claims, at: float) -> bool:
    return claims.expired(at) or claims.exp - at < REFRESH_MARGIN_S


def _check_token_session(
    store: SessionStore,
    stored: StoredSession,
    claims: tokens.Claims,
    at: float,
    wall: float,
    network: bool,
    refresh_limit: float,
) -> Account:
    if _near_end(claims, at) and network and stored.pat_token:
        refreshed = _refresh_token_session(store, stored, at, wall, refresh_limit)
        if refreshed is not None:
            stored, claims, vouched = refreshed
            if vouched:
                at = wall  # the server just vouched for the account: trust the clock again
    _check_pat_expiry(claims, at)
    if not claims.expired(at) or _within_grace(claims, at):
        return _account_from(claims, "pat", at, "file")
    raise AccountRequired("offline_expired")


def _refresh_token_session(
    store: SessionStore, seen: StoredSession, at: float, wall: float, timeout: float
) -> tuple[StoredSession, tokens.Claims, bool] | None:
    """Exchange the stored access token for a new entitlement token, once for all the processes that
    share the session file (the same lock as the other renewals; the server rate-limits exchanges).
    None = the server could not be reached. Raises ``AccountRequired('revoked')`` only when the
    exchange function itself says the token is unknown, revoked or expired."""
    global _net_down_until
    if time.monotonic() < _net_down_until:
        return None
    try:
        with store.refresh_locked():
            current = store.load()
            if current is None:
                raise AccountRequired("no_session")
            if current.source != "pat" or current.pat_token != seen.pat_token:
                return None  # replaced by another sign-in while we waited: leave it alone
            if current.access_token != seen.access_token:
                claims = _verify(current.access_token, at, "pat")
                if not _near_end(claims, at):
                    return current, claims, False  # somebody else exchanged while we waited
            if store.network_down_recently(now(), SHARED_NETDOWN_S):
                _net_down_until = time.monotonic() + NETWORK_RETRY_S
                return None
            try:
                answer = pat.exchange(str(current.pat_token), timeout=timeout)
            except AccountServerError as exc:
                if pat.definite_refusal(exc):
                    latest = store.load()
                    if latest is None or latest.pat_token == current.pat_token:
                        store._unlink()
                        raise _revoked_token_error() from exc
                    return None
                _net_down_until = time.monotonic() + NETWORK_RETRY_S
                store.mark_network_down(now())
                return None
            store.clear_network_down()
            claims = _verify(answer["entitlement_token"], wall, "pat")  # before it is kept
            stored = StoredSession(
                access_token=answer["entitlement_token"],
                pat_token=current.pat_token,
                user_id=claims.sub,
                email=claims.email,
                source="pat",
                issued_at=min(claims.iat, wall),
                high_water=wall,
            )
            store._write(stored)
            return stored, claims, True
    except TimeoutError:
        again = store.load()
        if again is None or again.source != "pat":
            return None
        return again, _verify(again.access_token, at, "pat"), False


_env_lock = threading.Lock()
_env_state: dict[str, Any] = {"pat": None, "token": None, "down_until": 0.0}


def _env_token_account(token: str) -> Account:
    """``RADARX_TOKEN``: exchange on first use, keep the entitlement token ONLY in memory and
    renew it hourly. Nothing is written to disk, so a CI job leaves no session behind."""
    if not pat.valid_format(token):
        raise AccountRequired(
            "invalid",
            "RADARX_TOKEN is not a radarx access token (it starts with rxd_pat_ and is 51 "
            "characters long). Check the variable; create a token in the app: Account > Access "
            "tokens.",
        )
    with _env_lock:
        wall = now()
        held = _env_state["token"] if _env_state["pat"] == token else None
        claims: tokens.Claims | None = None
        if held:
            try:
                claims = _verify(held, wall, "pat")
            except AccountRequired:
                claims = None
        if claims is not None and not _near_end(claims, wall):
            _check_pat_expiry(claims, wall)
            return _account_from(claims, "pat", wall, "env")
        if time.monotonic() >= _env_state["down_until"]:
            try:
                answer = pat.exchange(token, timeout=REFRESH_TIMEOUT_S)
            except AccountServerError as exc:
                if pat.definite_refusal(exc):
                    _env_state.update(pat=None, token=None)
                    raise _revoked_token_error() from exc
                _env_state["down_until"] = time.monotonic() + NETWORK_RETRY_S
            else:
                new_claims = _verify(answer["entitlement_token"], wall, "pat")
                _env_state.update(pat=token, token=answer["entitlement_token"])
                return _account_from(new_claims, "pat", wall, "env")
        # The account server cannot be reached. A token this process already holds keeps working
        # inside the offline allowance; with none there is nothing to stand on.
        if claims is not None:
            _check_pat_expiry(claims, wall)
            if _within_grace(claims, wall):
                return _account_from(claims, "pat", wall, "env")
        raise AccountRequired(
            "unavailable",
            "RADARX_TOKEN needs the account server once an hour and it cannot be reached. On a "
            "machine without internet, run `rxd login --token` on one that has it: the session "
            "file is then valid offline for 30 days.",
        )


def env_token_credentials() -> tuple[str, str] | None:
    """``(entitlement token, user id)`` of the ``RADARX_TOKEN`` session if this process holds a
    current one, else None. Never touches the network (the activity recorder uses it)."""
    token = pat.from_environment()
    if token is None:
        return None
    with _env_lock:
        held = _env_state["token"] if _env_state["pat"] == token else None
    if not held:
        return None
    try:
        claims = tokens.verify_entitlement_token(held, now=now())
    except Exception:
        return None
    if claims.expired(now()):
        return None
    return held, claims.sub


def server_reachable(timeout: float = PROBE_TIMEOUT_S) -> bool:
    """True only when the account server answers a small public request properly. A proxy or captive
    portal (an error page, HTML, 403 ...) and an outage count as "not reachable"."""
    global _net_down_until
    if time.monotonic() < _net_down_until:
        return False
    try:
        gotrue.settings(timeout=timeout)
        return True
    except AccountServerError:
        _net_down_until = time.monotonic() + NETWORK_RETRY_S
        return False


def check_stored_session(
    store: SessionStore | None = None,
    *,
    allow_network: bool = True,
    net_timeout: float | None = None,
) -> Account:
    """Decide whether the stored session is good. Raises :class:`AccountRequired`.

    ``net_timeout`` caps every network call this check may make (a command that is about to exit
    must not wait for a server that never answers).
    """
    store = store or SessionStore()
    try:
        stored = store.load_strict()
    except OSError as exc:
        raise AccountRequired(
            "unavailable",
            f"The saved sign-in file ({store.path}) cannot be read: {type(exc).__name__}. "
            "Check its permissions, or remove it and sign in again.",
        ) from exc
    if stored is None:
        raise AccountRequired("no_session")
    wall = now()
    at = max(wall, stored.high_water)  # a clock set backwards cannot make anything newer
    store.note_time(wall)
    source = stored.source
    claims = _verify(stored.access_token, at, source)
    if claims.sub != stored.user_id and stored.user_id:
        raise AccountRequired("invalid")
    refresh_limit = (
        REFRESH_TIMEOUT_S if net_timeout is None else min(net_timeout, REFRESH_TIMEOUT_S)
    )
    probe_limit = PROBE_TIMEOUT_S if net_timeout is None else min(net_timeout, PROBE_TIMEOUT_S)
    network = allow_network and (net_timeout is None or net_timeout >= 0.2)
    if source == "pat":
        return _check_token_session(store, stored, claims, at, wall, network, refresh_limit)

    can_refresh = source == "cli" and bool(stored.refresh_token)
    near_end = claims.expired(at) or claims.exp - at < REFRESH_MARGIN_S
    if near_end and network and can_refresh:
        refreshed = _refresh_stored(store, stored, at, wall, refresh_limit)
        if refreshed is not None:
            stored, claims, vouched = refreshed
            if vouched:
                at = wall  # the server just vouched for the account: trust the clock again
    elif claims.expired(at) and network and not can_refresh and server_reachable(probe_limit):
        # Nobody here can renew this token (the app owns its own sign-in) and the server IS
        # reachable: the 30-day offline allowance is for when it is not.
        exc = AccountRequired(
            "expired",
            "Your sign-in was made by the radarx-desktop app and has expired. The app renews "
            "its own sign-in, and it has not been opened since. Open the app or sign in again.",
        )
        exc.app_owned = True  # commands.sign_in_message adds the two ways out
        exc.headline = (
            "Your sign-in was made by the radarx-desktop app and has expired. The app renews "
            "its own sign-in, and it has not been opened since."
        )
        raise exc
    if not claims.expired(at):
        return _account_from(claims, source, at)
    if _within_grace(claims, at):
        return _account_from(claims, source, at)
    raise AccountRequired("offline_expired")


def require_account() -> Account:
    """Raise :class:`AccountRequired` unless a valid signed-in account is present.

    Called at the top of every public function that touches data (the Python API), by the
    command line before any command, and by the engine for every connection. With the development
    bypass (``RADARX_AUTH_DISABLED=1``, never honoured in an installer build) it returns at once.
    """
    if config.auth_disabled():
        return _BYPASS
    env_token = pat.from_environment()
    if env_token is not None:  # an explicit token in the environment wins over a stored session
        return _env_token_account(env_token)
    store = SessionStore()
    key = str(store.path)
    with _state_lock:
        if (
            _cache["path"] == key
            and time.monotonic() < _cache["until"]
            and _cache["account"] is not None
        ):
            return _cache["account"]
    account = check_stored_session(store)
    ttl = CACHE_TTL_S
    if not account.stale:
        ttl = max(0.0, min(ttl, account.expires_at - now() - REFRESH_MARGIN_S))
    with _state_lock:
        _cache.update(path=key, until=time.monotonic() + ttl, account=account)
    return account


def bypass_account() -> Account:
    """The account of a connection that needs none (the development bypass, or a web service whose
    operator switched the gate off with ``RADARX_WEB_REQUIRE_ACCOUNT=0``)."""
    return _BYPASS


def current_account() -> Account | None:
    """The signed-in account, or None (never raises). For ``whoami`` and friendly messages."""
    try:
        return require_account()
    except AccountRequired:
        return None


# -- sign in / out (command line) ---------------------------------------------------------------


def login_with_password(email: str, password: str, store: SessionStore | None = None) -> Account:
    """``rxd login``: sign in and store the session (access + refresh token) for every surface."""
    store = store or SessionStore()
    at = now()
    session = gotrue.password_login(email.strip(), password)  # AccountServerError on failure
    return _finish_login(store, session, at)


def login_with_code(code: str, verifier: str, store: SessionStore | None = None) -> Account:
    """``rxd login --browser``: finish a PKCE sign-in with a provider (Google, Apple, Facebook).

    ``code`` came from the redirect, ``verifier`` never left this process; the server refuses a
    code that was not made for this verifier.
    """
    store = store or SessionStore()
    at = now()
    session = gotrue.exchange_code(code, verifier)  # AccountServerError on failure
    return _finish_login(store, session, at)


def _finish_login(store: SessionStore, session: Mapping[str, Any], at: float) -> Account:
    stored, claims = _store_new_session(store, session, at)
    reset_runtime_state()
    return _account_from(claims, stored.source, at)


def login_with_token(token: str, store: SessionStore | None = None) -> Account:
    """``rxd login --token``: exchange a personal access token once (this checks it) and keep it in
    the private session file; every later run renews the one-hour entitlement token from it."""
    if not pat.valid_format(token):
        raise AccountRequired(
            "invalid",
            "That is not a radarx access token (it starts with rxd_pat_ and is 51 characters).",
        )
    store = store or SessionStore()
    at = now()
    answer = pat.exchange(token)  # AccountServerError on failure
    claims = _verify(answer["entitlement_token"], at, "pat")
    _check_pat_expiry(claims, at)
    store.save(
        StoredSession(
            access_token=answer["entitlement_token"],
            pat_token=token,
            user_id=claims.sub,
            email=claims.email,
            source="pat",
            issued_at=min(claims.iat, at),
            high_water=at,
        )
    )
    store.clear_network_down()
    reset_runtime_state()
    return _account_from(claims, "pat", at, "file")


def logout(store: SessionStore | None = None) -> bool:
    """``rxd logout``: end the session on the server (best effort) and delete the shared file.

    Returns True when a session was removed.
    """
    store = store or SessionStore()
    stored = store.load()
    if stored is None:
        reset_runtime_state()
        return False
    from engine.account import activity  # local import: activity needs this module

    activity.record("sign_out", surface="cli")
    activity.flush(timeout=3.0)
    if stored.source != "pat":  # (a token session has no Supabase session to end)
        with contextlib.suppress(AccountServerError):  # the file goes regardless
            gotrue.logout(stored.access_token)
    store.clear()
    reset_runtime_state()
    return True


# -- the engine's WebSocket (a session presented by the app) -------------------------------------


def verify_presented_token(
    access_token: str,
    *,
    persist: bool,
    store: SessionStore | None = None,
) -> Account:
    """Verify an access token the desktop/web app presents on a WebSocket connection.

    With ``persist`` (the desktop engine, one user per machine) a newer token is written to the
    shared file as an ``app`` session (access token only, no refresh token) so ``rxd`` and the
    activity recorder see the same sign-in; an expired token is accepted inside the offline
    allowance (the app is offline on a ship). The web service (``persist=False``) serves many
    users from a server that is always online: it accepts only a token that has not expired, and
    never persists anything.
    """
    store = store or SessionStore()
    wall = now()
    stored = store.load() if persist else None
    claims = _verify(access_token, wall)
    reset_mark = False
    if stored is not None:
        # A token issued just now (by the server's clock) that is newer than the one on file is
        # proof of the current time: it clears a latest-reading mark that a wrong clock pushed
        # into the future, which would otherwise refuse every valid token.
        previous = _safe_iat(stored.access_token)
        reset_mark = abs(claims.iat - wall) <= FRESH_TOKEN_S and (
            previous is None or claims.iat > previous
        )
    at = wall if (stored is None or reset_mark) else max(wall, stored.high_water)
    account = _account_from(claims, "app", at)
    if account.stale:
        if not persist:
            raise AccountRequired("expired")
        if not _within_grace(claims, at):
            raise AccountRequired("offline_expired")
    if persist:
        _persist_presented(store, access_token, claims, wall, reset_mark)
        reset_runtime_state()
    return account


def _persist_presented(
    store: SessionStore,
    access_token: str,
    claims: tokens.Claims,
    wall: float,
    reset_mark: bool = False,
) -> None:
    def change(cur: StoredSession | None) -> StoredSession | None:
        if cur is not None and cur.source == "pat":
            return cur  # an access-token session is never replaced by the app (it keeps its own)
        if cur is not None and cur.user_id == claims.sub and cur.access_token != access_token:
            cur_claims_iat = _safe_iat(cur.access_token)
            if cur_claims_iat is not None and cur_claims_iat > claims.iat:
                return cur  # the file already holds a newer token of this user
        return StoredSession(
            access_token=access_token,
            refresh_token=None,
            user_id=claims.sub,
            email=claims.email,
            source="app",
            issued_at=min(claims.iat, wall),
            high_water=wall if reset_mark else max(wall, cur.high_water if cur else 0.0),
        )

    with contextlib.suppress(OSError, TimeoutError):  # the connection stays authorised in memory
        store.update(change)


def _safe_iat(token: str) -> float | None:
    try:
        return tokens.verify_access_token(token, now=now()).iat
    except Exception:
        return None


def end_presented_session(store: SessionStore | None = None) -> None:
    """The app signed out: remove the shared file (one credential for every surface), unless it
    holds an access-token session, which the app does not own."""
    store = store or SessionStore()
    stored = store.load()
    if stored is None or stored.source != "pat":
        store.clear()
    reset_runtime_state()
