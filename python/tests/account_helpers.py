"""Test doubles for the account gate: a locally generated signing key, a mocked key document and a
mocked account server (GoTrue + the activity table). Nothing here touches the real network, the
hosted Supabase project or any real account; every key and token is made up on the spot."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from engine.account import http as _http
from engine.account.errors import AccountServerError

TEST_URL = "https://test-project.example.invalid"
TEST_ISS = f"{TEST_URL}/auth/v1"
TEST_KID = "test-key-1"


#: the real network function, captured before any test replaces it with the mock
REAL_REQUEST_JSON = _http.request_json


class KeyPair:
    """A P-256 signing key like the project's (ES256), plus the public key document for it."""

    def __init__(self, kid: str = TEST_KID) -> None:
        self.kid = kid
        self.private = ec.generate_private_key(ec.SECP256R1())
        public_jwk = jwt.algorithms.ECAlgorithm(jwt.algorithms.ECAlgorithm.SHA256).to_jwk(
            self.private.public_key(), as_dict=True
        )
        public_jwk.update({"kid": kid, "alg": "ES256", "use": "sig", "key_ops": ["verify"]})
        self.jwks = {"keys": [public_jwk]}

    def token(
        self,
        *,
        sub: str | None = None,
        email: str = "ada@example.test",
        iat: float | None = None,
        lifetime: float = 3600.0,
        aud: str = "authenticated",
        iss: str = TEST_ISS,
        role: str = "authenticated",
        kid: str | None = None,
        alg: str = "ES256",
        key: Any = None,
        extra: dict[str, Any] | None = None,
        drop: tuple[str, ...] = (),
    ) -> str:
        iat = time.time() if iat is None else iat
        claims: dict[str, Any] = {
            "iss": iss,
            "aud": aud,
            "sub": sub or "11111111-2222-3333-4444-555555555555",
            "email": email,
            "role": role,
            "iat": int(iat),
            "exp": int(iat + lifetime),
            "app_metadata": {"provider": "email"},
            "is_anonymous": False,
        }
        claims.update(extra or {})
        for name in drop:
            claims.pop(name, None)
        return jwt.encode(
            claims, key or self.private, algorithm=alg, headers={"kid": kid or self.kid}
        )


@dataclass
class FakeServer:
    """The account server: GoTrue endpoints, the key document and the activity table."""

    keys: KeyPair
    users: dict[str, str] = field(default_factory=lambda: {"ada@example.test": "correct horse 1"})
    #: "ok" | "unreachable" | "rejected" (what the token endpoint does)
    mode: str = "ok"
    #: what POST /rest/v1/activity_events answers: ok | unreachable | bad_request | unauthorized
    activity_mode: str = "ok"
    now: float = field(default_factory=time.time)
    calls: list[tuple[str, str]] = field(default_factory=list)
    activity_rows: list[dict[str, Any]] = field(default_factory=list)
    activity_tokens: list[str] = field(default_factory=list)
    refresh_counter: int = 0
    activity_posts: int = 0
    #: a function that says whether the database refuses a posted activity row (a 400 for the batch)
    reject_row: Any = None
    #: the providers the project has switched on (GET /auth/v1/settings, "external")
    external: dict[str, bool] = field(
        default_factory=lambda: {"email": True, "google": True, "apple": False, "facebook": True}
    )
    #: authorisation codes the "provider" handed out: code -> PKCE challenge (single use)
    pkce_codes: dict[str, str] = field(default_factory=dict)
    #: every body POSTed to the token endpoint, for "the password never leaks" checks
    token_bodies: list[Any] = field(default_factory=list)

    def clock(self) -> float:
        return self.now

    def _session(self, email: str) -> dict[str, Any]:
        self.refresh_counter += 1
        return {
            "access_token": self.keys.token(email=email, iat=self.now),
            "refresh_token": f"refresh-{self.refresh_counter}",
            "expires_in": 3600,
            "token_type": "bearer",
            "user": {"id": "11111111-2222-3333-4444-555555555555", "email": email},
        }

    def request(self, method: str, url: str, *, headers=None, body=None, timeout=0):
        path = urlparse(url).path
        query = urlparse(url).query
        self.calls.append((method, path + ("?" + query if query else "")))
        if not url.startswith(TEST_URL):
            raise AssertionError(f"a test reached for the real network: {url}")
        if path.endswith("/.well-known/jwks.json"):
            return self.keys.jwks
        if path.endswith("/auth/v1/settings"):
            if self.mode == "unreachable":
                raise AccountServerError(0, "unreachable", "OSError")
            return {"external": dict(self.external), "disable_signup": False}
        if self.mode == "unreachable" and "/rest/v1/" not in path:
            raise AccountServerError(0, "unreachable", "OSError")
        if path.endswith("/auth/v1/token"):
            self.token_bodies.append(body)
        if path.endswith("/auth/v1/token") and "grant_type=pkce" in query:
            import base64
            import hashlib

            code, verifier = (body or {}).get("auth_code"), (body or {}).get("code_verifier", "")
            challenge = self.pkce_codes.pop(code, None)  # a code works once
            digest = hashlib.sha256(str(verifier).encode("ascii", "replace")).digest()
            if (
                challenge is None
                or base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != challenge
            ):
                raise AccountServerError(400, "flow_state_not_found", "invalid flow state")
            return self._session("ada@example.test")
        if path.endswith("/auth/v1/token") and "grant_type=password" in query:
            email, password = (body or {}).get("email"), (body or {}).get("password")
            if self.users.get(email) != password:
                raise AccountServerError(400, "invalid_credentials", "Invalid login credentials")
            return self._session(email)
        if path.endswith("/auth/v1/token") and "grant_type=refresh_token" in query:
            if self.mode == "rejected" or not str((body or {}).get("refresh_token", "")).startswith(
                "refresh-"
            ):
                raise AccountServerError(400, "refresh_token_not_found", "Invalid Refresh Token")
            return self._session("ada@example.test")
        if path.endswith("/auth/v1/logout"):
            return None
        if path.endswith("/rest/v1/activity_events"):
            if self.activity_mode == "unreachable":
                raise AccountServerError(0, "unreachable", "OSError")
            if self.activity_mode == "bad_request":
                raise AccountServerError(400, "23514", "event not allowed")
            if self.activity_mode == "unauthorized":
                raise AccountServerError(401, "", "JWT expired")
            if self.reject_row is not None and any(self.reject_row(r) for r in (body or [])):
                self.activity_posts += 1
                raise AccountServerError(400, "23514", "event not allowed")
            self.activity_posts += 1
            self.activity_tokens.append(str((headers or {}).get("Authorization", "")))
            self.activity_rows.extend(body or [])
            return None
        raise AssertionError(f"unexpected request {method} {url}")


def fake_browser(server: FakeServer, hit: dict | None = None, *, error: str | None = None):
    """An ``opener`` for ``oauth_login.login``: plays the browser + provider. Reads the address the
    command printed, then (in a thread) delivers the redirect to its loopback port, like Google
    would after the user clicks "allow". Returns the function; ``hit`` collects what happened."""
    hit = {} if hit is None else hit
    import secrets
    import threading
    import urllib.request
    from urllib.parse import parse_qs

    def opener(url: str) -> bool:
        query = parse_qs(urlparse(url).query)
        hit["url"] = url
        redirect = query["redirect_to"][0]
        challenge = query["code_challenge"][0]
        if error is not None:
            target = f"{redirect}?error={error}&error_description=PROVIDER+TEXT+SHOULD+NOT+SHOW"
        else:
            code = secrets.token_hex(8)
            server.pkce_codes[code] = challenge
            hit["code"] = code
            target = f"{redirect}?code={code}"

        def deliver() -> None:
            try:
                urllib.request.urlopen(target, timeout=5).read()
            except Exception as exc:  # recorded for the test, not raised in a thread
                hit["deliver_error"] = repr(exc)

        threading.Thread(target=deliver, daemon=True).start()
        return True

    return opener


def new_user_id() -> str:
    return str(uuid.uuid4())


def run_python(args, *, env, timeout=120, **kw):
    """Run ``python *args`` in a child process without forking.

    A forked child of a process that has already used pyproj crashes on macOS (segfault, exit -11):
    libproj registers a ``pthread_atfork`` child handler that closes its SQLite handles, and that
    handler fails in the child (crash report: ``_pthread_atfork_child_handlers`` ->
    ``SQLiteHandleCache`` -> ``sqlite3Close`` -> ``os_log``). ``subprocess`` forks unless it can use
    ``posix_spawn``, which it does only with ``close_fds=False`` and no ``cwd``; Python marks file
    descriptors non-inheritable, so nothing leaks into the child.
    """
    import subprocess
    import sys

    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return subprocess.run([sys.executable, *args], env=env, timeout=timeout, close_fds=False, **kw)


def spawn_python(args, *, env, **kw):
    """Like :func:`run_python` but returns the running ``Popen`` (see there for why no fork)."""
    import subprocess
    import sys

    return subprocess.Popen([sys.executable, *args], env=env, close_fds=False, **kw)


class Blackhole:
    """A local TCP server that accepts connections and never answers (a hung account server)."""

    def __init__(self) -> None:
        import socket
        import threading

        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(50)
        self.port = self.sock.getsockname()[1]
        self.held: list = []
        self._stop = False
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        while not self._stop:
            try:
                self.held.append(self.sock.accept())
            except OSError:
                return

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        import socket

        self._stop = True
        with __import__("contextlib").suppress(OSError):
            socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
        self.sock.close()
        for conn, _ in self.held:
            with __import__("contextlib").suppress(OSError):
                conn.close()
