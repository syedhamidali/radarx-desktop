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

from engine.account import config, gotrue, tokens
from engine.account.entitlement import entitlement_allows
from engine.account.errors import AccountRequired, AccountServerError
from engine.account.store import SessionStore, StoredSession

#: Refresh a ``rxd login`` token this long before it expires (so it does not lapse mid-command).
REFRESH_MARGIN_S = 120.0
#: After the account server was unreachable, do not retry the network for this long (per process).
NETWORK_RETRY_S = 60.0
#: How long one successful check is reused inside a process (the API calls it on every function).
CACHE_TTL_S = 30.0


def now() -> float:
    """The clock the gate uses (a function so tests can move it)."""
    return time.time()


@dataclass
class Account:
    """A verified signed-in account."""

    user_id: str
    email: str
    source: str  # "cli" | "app" | "bypass"
    expires_at: float = 0.0
    issued_at: float = 0.0
    #: True when the access token has expired and the session is carried by the offline grace.
    stale: bool = False
    #: Whole days left of the offline grace (None when the token is current).
    grace_days_left: int | None = None
    claims: Mapping[str, Any] = field(default_factory=dict)

    @property
    def bypassed(self) -> bool:
        return self.source == "bypass"


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


def _verify(token: str, at: float) -> tokens.Claims:
    try:
        claims = tokens.verify_access_token(token, now=at)
    except tokens.DependencyMissing as exc:
        raise AccountRequired("dependency") from exc
    except tokens.TokenInvalid as exc:
        raise AccountRequired("invalid") from exc
    if not entitlement_allows(claims.raw):
        raise AccountRequired("not_entitled")
    return claims


def _account_from(claims: tokens.Claims, source: str, at: float) -> Account:
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
    return stored, claims


def _refresh_stored(
    store: SessionStore, seen: StoredSession, at: float, wall: float
) -> tuple[StoredSession, tokens.Claims, bool] | None:
    """Refresh a ``rxd login`` session under the file lock. None = the server was unreachable.

    Raises ``AccountRequired('revoked')`` when the server refuses the refresh token (and removes
    the useless file).
    """
    global _net_down_until
    if time.monotonic() < _net_down_until:
        return None
    try:
        with store.locked():
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
            try:
                session = gotrue.refresh(seen.refresh_token)
            except AccountServerError as exc:
                if exc.unreachable:
                    _net_down_until = time.monotonic() + NETWORK_RETRY_S
                    return None
                store._unlink()
                raise AccountRequired("revoked") from exc
            # the server just vouched for the account: the clock reading is trusted again
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


def check_stored_session(
    store: SessionStore | None = None, *, allow_network: bool = True
) -> Account:
    """Decide whether the stored session is good. Raises :class:`AccountRequired`."""
    store = store or SessionStore()
    stored = store.load()
    if stored is None:
        raise AccountRequired("no_session")
    wall = now()
    at = max(wall, stored.high_water)  # a clock set backwards cannot make anything newer
    store.note_time(wall)
    claims = _verify(stored.access_token, at)
    if claims.sub != stored.user_id and stored.user_id:
        raise AccountRequired("invalid")
    source = stored.source

    needs_refresh = claims.expired(at) or claims.exp - at < REFRESH_MARGIN_S
    if needs_refresh and allow_network and source == "cli" and stored.refresh_token:
        refreshed = _refresh_stored(store, stored, at, wall)
        if refreshed is not None:
            stored, claims, vouched = refreshed
            if vouched:
                at = wall  # the server just vouched for the account: trust the clock again
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
    stored, claims = _store_new_session(store, session, at)
    reset_runtime_state()
    return _account_from(claims, stored.source, at)


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
    with contextlib.suppress(AccountServerError):  # the file goes regardless; the session lapses
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
    activity recorder see the same sign-in. The web service never persists: it serves many users.
    """
    store = store or SessionStore()
    wall = now()
    stored = store.load() if persist else None
    at = max(wall, stored.high_water) if stored else wall
    claims = _verify(access_token, at)
    account = _account_from(claims, "app", at)
    if account.stale and not _within_grace(claims, at):
        raise AccountRequired("offline_expired")
    if persist:
        _persist_presented(store, access_token, claims, wall)
        reset_runtime_state()
    return account


def _persist_presented(
    store: SessionStore, access_token: str, claims: tokens.Claims, wall: float
) -> None:
    def change(cur: StoredSession | None) -> StoredSession | None:
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
            high_water=max(wall, cur.high_water if cur else 0.0),
        )

    with contextlib.suppress(OSError, TimeoutError):  # the connection stays authorised in memory
        store.update(change)


def _safe_iat(token: str) -> float | None:
    try:
        return tokens.verify_access_token(token, now=now()).iat
    except Exception:
        return None


def end_presented_session(store: SessionStore | None = None) -> None:
    """The app signed out: remove the shared file (one credential for every surface)."""
    (store or SessionStore()).clear()
    reset_runtime_state()
