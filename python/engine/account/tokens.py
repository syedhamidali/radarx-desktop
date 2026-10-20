"""Offline verification of a Supabase access token (a JWT) against the project's public keys.

* Algorithms: ``ES256`` and ``RS256`` only, chosen from the *key*, never from what the token
  claims (so ``none`` and the HS256/public-key confusion attack are refused).
* Checked: signature, issuer, audience, required claims, role, not anonymous. Expiry is *reported*
  (``Claims.expired``) and not enforced here, because the offline rule (docs/ACCOUNTS.md) decides
  what an expired token is still good for.
* Keys: the JWKS document of the project, cached in the user config folder, with a built-in copy of
  the public key. A token whose ``kid`` is unknown triggers at most one fetch per five minutes.
  The PyJWT / cryptography imports are lazy so ``import rxd`` and ``rxd --help`` stay instant.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any

from engine.account import config, http
from engine.account.errors import AccountServerError
from engine.account.jwks_seed import SEED_JWKS, SEED_URL
from engine.account.store import config_dir, write_private

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


def _cache_path():
    return config_dir() / "jwks.json"


def _valid_jwks(doc: Any) -> bool:
    return (
        isinstance(doc, dict)
        and isinstance(doc.get("keys"), list)
        and all(isinstance(k, dict) for k in doc["keys"])
    )


def _load_cached(url: str) -> dict[str, Any] | None:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("url") == url and _valid_jwks(data.get("jwks")):
        return data["jwks"]
    return None


def current_jwks(*, refetch: bool = False, now: float | None = None) -> dict[str, Any]:
    """The key document to verify with: memory, then disk cache, then the built-in public copy.

    ``refetch=True`` (an unknown ``kid``) downloads the project's document first, at most once per
    five minutes. A failed download is silent: the cached / built-in document is used.
    """
    url = config.jwks_url()
    now = time.time() if now is None else now
    with _lock:
        if _memory["url"] != url:
            _memory.update(url=url, jwks=None, fetched_at=0.0, tried_at=0.0)
        if refetch and now - _memory["tried_at"] >= JWKS_REFETCH_MIN_S:
            _memory["tried_at"] = now
            try:
                doc = http.request_json("GET", url, headers={"apikey": config.publishable_key()})
                if _valid_jwks(doc) and doc["keys"]:
                    _memory.update(jwks=doc, fetched_at=now)
                    with contextlib.suppress(OSError):
                        write_private(
                            _cache_path(),
                            json.dumps({"url": url, "fetched_at": now, "jwks": doc}),
                        )
            except AccountServerError:
                pass
        if _memory["jwks"] is None:
            _memory["jwks"] = _load_cached(url) or (SEED_JWKS if url == SEED_URL else {"keys": []})
        return _memory["jwks"]


def reset_cache() -> None:
    """Forget the in-memory key document (tests)."""
    with _lock:
        _memory.update(url=None, jwks=None, fetched_at=0.0, tried_at=0.0)


def _find_key(jwt_mod: Any, kid: str | None, alg: str, now: float):
    for attempt in (0, 1):
        doc = current_jwks(refetch=attempt == 1, now=now)
        try:
            keyset = jwt_mod.PyJWKSet.from_dict(doc)
        except Exception:
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
