"""Offline verification of a Supabase access token (a JWT) against the project's public keys.

* Algorithms: ``ES256`` and ``RS256`` only, chosen from the *key*, never from what the token
  claims (so ``none`` and the HS256/public-key confusion attack are refused).
* Checked: signature, issuer, audience, required claims, role, not anonymous. Expiry is *reported*
  (``Claims.expired``) and not enforced here, because the offline rule (docs/ACCOUNTS.md) decides
  what an expired token is still good for.
* Keys: the project's public key is built in; a token with another ``kid`` triggers a live fetch of
  the project's JWKS (in memory only, at most every five minutes, never a file in the user's folder:
  that is data the user controls).
  The PyJWT / cryptography imports are lazy so ``import rxd`` and ``rxd --help`` stay instant.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any

from engine.account import config, http
from engine.account.errors import AccountServerError
from engine.account.jwks_seed import ENTITLEMENT_SEED_JWKS, SEED_JWKS, SEED_URL

ALLOWED_ALGS = ("ES256", "RS256")
JWKS_REFETCH_MIN_S = 300.0
_lock = threading.Lock()
_memory: dict[str, Any] = {"url": None, "jwks": None, "fetched_at": 0.0, "tried_at": 0.0}


class TokenInvalid(Exception):  # noqa: N818 - reads better than TokenInvalidError
    """The token is not a genuine, current-project, signed-in-user access token."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class DependencyMissing(Exception):  # noqa: N818 - reads better than DependencyMissingError
    """PyJWT or cryptography is not installed."""


@dataclass
class Claims:
    sub: str
    email: str
    exp: float
    iat: float
    raw: dict[str, Any]

    @property
    def app_metadata(self) -> dict[str, Any]:
        value = self.raw.get("app_metadata")
        return value if isinstance(value, dict) else {}

    def expired(self, now: float) -> bool:
        return now > self.exp + config.LEEWAY_S


# -- key documents ----------------------------------------------------------------------------


def _valid_jwks(doc: Any) -> bool:
    return (
        isinstance(doc, dict)
        and isinstance(doc.get("keys"), list)
        and all(isinstance(k, dict) for k in doc["keys"])
    )


def current_jwks(*, refetch: bool = False, now: float | None = None) -> dict[str, Any]:
    """The keys to verify with: the built-in public key first, then keys fetched this run.

    Nothing is read from or written to disk: a file in the user's folder is data the user controls,
    and a token verified against such a file would prove nothing. A token whose ``kid`` is not in
    the built-in document triggers (``refetch=True``) one live HTTPS download of the project's
    document, kept in memory for this process and tried at most every five minutes; with no
    network the unknown key is simply refused. (A key rotation therefore needs one online run or
    an app update.)
    """
    url = config.jwks_url()
    now = time.time() if now is None else now
    with _lock:
        if _memory["url"] != url:
            _memory.update(url=url, jwks=None, fetched_at=0.0, tried_at=0.0)
        if refetch and now - _memory["tried_at"] >= JWKS_REFETCH_MIN_S:
            _memory["tried_at"] = now
            try:
                doc = http.request_json(
                    "GET", url, headers={"apikey": config.publishable_key()}, timeout=5.0
                )
                if _valid_jwks(doc) and doc["keys"]:
                    _memory.update(jwks=doc, fetched_at=now)
            except AccountServerError:
                pass
        keys: list[dict[str, Any]] = []
        if url == SEED_URL:
            keys += list(SEED_JWKS["keys"])
        if _memory["jwks"] is not None:
            seen = {k.get("kid") for k in keys}
            keys += [k for k in _memory["jwks"]["keys"] if k.get("kid") not in seen]
        return {"keys": keys}


def reset_cache() -> None:
    """Forget the in-memory key document (tests)."""
    with _lock:
        _memory.update(url=None, jwks=None, fetched_at=0.0, tried_at=0.0)


def _find_key(jwt_mod: Any, kid: str | None, alg: str, now: float):
    for attempt in (0, 1):
        doc = current_jwks(refetch=attempt == 1, now=now)
        try:
            keyset = jwt_mod.PyJWKSet.from_dict(doc)
        except Exception:  # a malformed document is "no key"
            keyset = None
        if keyset is not None:
            for key in keyset.keys:
                if (kid is None or key.key_id == kid) and (key.algorithm_name or alg) == alg:
                    return key.key
    return None


# -- verification -----------------------------------------------------------------------------


def verify_access_token(token: str, *, now: float | None = None) -> Claims:
    """Verify ``token`` offline. Raises :class:`TokenInvalid` (or :class:`DependencyMissing`)."""
    now = time.time() if now is None else now
    try:
        import jwt  # PyJWT
        from jwt import exceptions as jwt_exc
    except ImportError as exc:  # pragma: no cover - dependencies are declared
        raise DependencyMissing(str(exc)) from exc

    if not isinstance(token, str) or token.count(".") != 2 or len(token) > 8192:
        raise TokenInvalid("format")
    try:
        header = jwt.get_unverified_header(token)
    except jwt_exc.PyJWTError:
        raise TokenInvalid("format") from None
    alg = header.get("alg")
    if alg not in ALLOWED_ALGS:
        raise TokenInvalid("algorithm")
    kid = header.get("kid")
    key = _find_key(jwt, kid if isinstance(kid, str) else None, alg, now)
    if key is None:
        raise TokenInvalid("unknown_key")
    try:
        payload = jwt.decode(
            token,
            key=key,
            algorithms=[alg],
            audience=config.AUDIENCE,
            issuer=config.issuer(),
            options={
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
                "require": ["exp", "iat", "sub", "aud", "iss"],
            },
        )
    except jwt_exc.InvalidSignatureError:
        raise TokenInvalid("signature") from None
    except jwt_exc.InvalidAudienceError:
        raise TokenInvalid("audience") from None
    except jwt_exc.InvalidIssuerError:
        raise TokenInvalid("issuer") from None
    except jwt_exc.MissingRequiredClaimError:
        raise TokenInvalid("claims") from None
    except jwt_exc.PyJWTError:
        raise TokenInvalid("format") from None

    if payload.get("role") != "authenticated" or payload.get("is_anonymous") is True:
        raise TokenInvalid("not_a_user")
    try:
        exp = float(payload["exp"])
        iat = float(payload["iat"])
    except (TypeError, ValueError):
        raise TokenInvalid("claims") from None
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        raise TokenInvalid("claims")
    email = payload.get("email")
    return Claims(
        sub=sub, email=email if isinstance(email, str) else "", exp=exp, iat=iat, raw=payload
    )


# -- entitlement tokens (personal access tokens) -------------------------------------------------
#
# Strictly separate from the Supabase access tokens above: a different issuer, a different
# audience, a different key set (never fetched from the network, never read from a file), and the
# algorithm is pinned to ES256 before any key is looked at. A token of one kind cannot pass as the
# other even if both were signed by the same key.

ENTITLEMENT_ALGS = ("ES256",)


def entitlement_keys() -> list[dict[str, Any]]:
    """The public keys entitlement tokens are checked against: the built-in set for the real
    project; in a source checkout pointed at another project, only ``RADARX_ENTITLEMENT_JWKS``."""
    keys: list[dict[str, Any]] = []
    if config.is_default_project():
        keys += list(ENTITLEMENT_SEED_JWKS["keys"])
    override = config.entitlement_jwks_override()
    if override:
        try:
            doc = json.loads(override)
        except ValueError:
            doc = None
        if _valid_jwks(doc):
            known = {k.get("kid") for k in keys}
            keys += [k for k in doc["keys"] if k.get("kid") not in known]
    return keys


def verify_entitlement_token(token: str, *, now: float | None = None) -> Claims:
    """Verify an entitlement token offline (:class:`TokenInvalid`, :class:`DependencyMissing`).

    Checked: ES256 signature with one of :func:`entitlement_keys` (chosen by ``kid``), issuer,
    audience, the claims ``exp iat sub jti``, ``src == "pat"`` and the token's own expiry date
    ``pat_exp``. Like :func:`verify_access_token` it *reports* expiry (``Claims.expired``) and
    leaves the offline rule to the caller.
    """
    now = time.time() if now is None else now
    try:
        import jwt  # PyJWT
        from jwt import exceptions as jwt_exc
    except ImportError as exc:  # pragma: no cover - dependencies are declared
        raise DependencyMissing(str(exc)) from exc

    if not isinstance(token, str) or token.count(".") != 2 or len(token) > 8192:
        raise TokenInvalid("format")
    try:
        header = jwt.get_unverified_header(token)
    except jwt_exc.PyJWTError:
        raise TokenInvalid("format") from None
    if header.get("alg") not in ENTITLEMENT_ALGS:
        raise TokenInvalid("algorithm")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise TokenInvalid("unknown_key")
    keys = [k for k in entitlement_keys() if k.get("kid") == kid]
    if not keys:
        raise TokenInvalid("unknown_key")
    key = None
    for entry in keys:
        try:
            candidate = jwt.PyJWK.from_dict(entry)
        except Exception:  # a malformed key entry is "no key"
            continue
        if entry.get("kty") == "EC" and entry.get("crv") == "P-256":
            key = candidate.key
            break
    if key is None:
        raise TokenInvalid("unknown_key")
    try:
        payload = jwt.decode(
            token,
            key=key,
            algorithms=list(ENTITLEMENT_ALGS),
            audience=config.ENTITLEMENT_AUDIENCE,
            issuer=config.ENTITLEMENT_ISSUER,
            options={
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
                "require": ["exp", "iat", "sub", "aud", "iss", "jti"],
            },
        )
    except jwt_exc.InvalidSignatureError:
        raise TokenInvalid("signature") from None
    except jwt_exc.InvalidAudienceError:
        raise TokenInvalid("audience") from None
    except jwt_exc.InvalidIssuerError:
        raise TokenInvalid("issuer") from None
    except jwt_exc.MissingRequiredClaimError:
        raise TokenInvalid("claims") from None
    except jwt_exc.PyJWTError:
        raise TokenInvalid("format") from None

    if payload.get("src") != "pat":
        raise TokenInvalid("source")
    try:
        exp = float(payload["exp"])
        iat = float(payload["iat"])
        pat_exp = float(payload["pat_exp"])
    except (KeyError, TypeError, ValueError):
        raise TokenInvalid("claims") from None
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        raise TokenInvalid("claims")
    if exp > pat_exp + 1:  # a token can never outlive the access token it came from
        raise TokenInvalid("claims")
    email = payload.get("email")
    return Claims(
        sub=sub, email=email if isinstance(email, str) else "", exp=exp, iat=iat, raw=payload
    )
