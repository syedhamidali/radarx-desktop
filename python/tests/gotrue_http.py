"""A real HTTP mock of the account server (GoTrue) for tests that need several PROCESSES.

``FakeServer`` in ``account_helpers.py`` replaces the network function inside one process; child
processes cannot share that. This is a small ``http.server`` on 127.0.0.1 (the engine allows plain
http to this machine only) that implements what the stress tests need:

* password and PKCE sign-in, the public key document and the auth settings;
* refresh-token **rotation with reuse detection**, as Supabase does it: every refresh token may be
  used once; using a spent one revokes the whole family (the session), unless it is inside
  ``reuse_interval`` seconds of the first use (Supabase's ``refresh_token_reuse_interval``);
* counters: how many refresh calls arrived, how many were reuses, which families were revoked.

Every key and token is made up on the spot. Nothing here touches a real network.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from tests.account_helpers import KeyPair

USER_ID = "11111111-2222-3333-4444-555555555555"


class RotatingGoTrue:
    def __init__(
        self,
        keys: KeyPair | None = None,
        *,
        users: dict[str, str] | None = None,
        lifetime: float = 3600.0,
        latency: float = 0.0,
        reuse_interval: float = 0.0,
    ) -> None:
        self.keys = keys or KeyPair()
        self.users = users or {"ada@example.test": "correct horse 1"}
        self.lifetime = lifetime
        self.latency = latency
        self.reuse_interval = reuse_interval
        self._lock = threading.Lock()
        # refresh token -> {"family", "used_at", "child"}
        self._tokens: dict[str, dict[str, Any]] = {}
        self.revoked: set[str] = set()
        self.refresh_calls = 0
        self.reuse_events: list[str] = []
        self.password_calls = 0
        self.refused: list[str] = []  # refresh tokens answered with an error
        self.offline = False  # answer every call with 503
        self.pkce_codes: dict[str, str] = {}
        self.external = {"email": True, "google": True, "apple": False, "facebook": True}
        self.log: list[str] = []
        self._httpd: ThreadingHTTPServer | None = None
        self.port = 0

    # -- lifecycle ---------------------------------------------------------------------------
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def issuer(self) -> str:
        return f"{self.url}/auth/v1"

    def start(self) -> RotatingGoTrue:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args: Any) -> None:  # silence
                pass

            def _send(self, status: int, payload: Any) -> None:
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                status, payload = server.handle("GET", self.path, None)
                self._send(status, payload)

            def do_POST(self) -> None:
                size = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(size) or b"{}")
                except ValueError:
                    body = {}
                status, payload = server.handle("POST", self.path, body)
                self._send(status, payload)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def __enter__(self) -> RotatingGoTrue:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- tokens ------------------------------------------------------------------------------
    def access_token(self, email: str, *, iat: float | None = None, lifetime: float | None = None):
        return self.keys.token(
            email=email,
            sub=USER_ID,
            iss=self.issuer,
            iat=time.time() if iat is None else iat,
            lifetime=self.lifetime if lifetime is None else lifetime,
        )

    def new_session(self, email: str = "ada@example.test", *, iat: float | None = None) -> dict:
        """A fresh family: used by tests to seed the session file as if ``rxd login`` had run."""
        with self._lock:
            return self._mint(secrets.token_hex(6), email, iat=iat)

    def _mint(self, family: str, email: str, *, iat: float | None = None) -> dict[str, Any]:
        refresh = f"rt-{family}-{len(self._tokens) + 1}"
        self._tokens[refresh] = {"family": family, "used_at": None, "child": None}
        return {
            "access_token": self.access_token(email, iat=iat),
            "refresh_token": refresh,
            "expires_in": int(self.lifetime),
            "token_type": "bearer",
            "user": {"id": USER_ID, "email": email},
        }

    # -- requests ----------------------------------------------------------------------------
    def handle(self, method: str, raw_path: str, body: Any) -> tuple[int, Any]:
        if self.latency:
            time.sleep(self.latency)
        parsed = urlparse(raw_path)
        path, query = parsed.path, parse_qs(parsed.query)
        if self.offline and not path.endswith("jwks.json"):
            return 503, {"msg": "offline"}
        if path.endswith("/.well-known/jwks.json"):
            return 200, self.keys.jwks
        if path.endswith("/auth/v1/settings"):
            return 200, {"external": dict(self.external), "disable_signup": False}
        if path.endswith("/auth/v1/logout"):
            return 204, None
        if path.endswith("/auth/v1/token"):
            grant = (query.get("grant_type") or [""])[0]
            if grant == "password":
                return self._password(body or {})
            if grant == "refresh_token":
                return self._refresh(str((body or {}).get("refresh_token", "")))
        return 404, {"msg": f"unexpected {method} {path}"}

    def _password(self, body: dict[str, Any]) -> tuple[int, Any]:
        with self._lock:
            self.password_calls += 1
            email, password = body.get("email"), body.get("password")
            if self.users.get(email) != password:
                return 400, {"error_code": "invalid_credentials", "msg": "Invalid login"}
            return 200, self._mint(secrets.token_hex(6), email)

    def _refresh(self, token: str) -> tuple[int, Any]:
        with self._lock:
            self.refresh_calls += 1
            rec = self._tokens.get(token)
            if rec is None:
                self.refused.append(token)
                return 400, {
                    "error_code": "refresh_token_not_found",
                    "msg": "Invalid Refresh Token",
                }
            family = rec["family"]
            if family in self.revoked:
                self.refused.append(token)
                return 400, {"error_code": "refresh_token_already_used", "msg": "revoked"}
            now = time.time()
            if rec["used_at"] is not None:
                within = now - rec["used_at"] <= self.reuse_interval
                if within and rec["child"] is not None:
                    child = rec["child"]  # Supabase hands the same child back inside the interval
                    return 200, {
                        "access_token": self.access_token("ada@example.test"),
                        "refresh_token": child,
                        "expires_in": int(self.lifetime),
                        "token_type": "bearer",
                        "user": {"id": USER_ID, "email": "ada@example.test"},
                    }
                self.revoked.add(family)  # reuse detected: the whole session ends
                self.reuse_events.append(token)
                self.refused.append(token)
                return 400, {"error_code": "refresh_token_already_used", "msg": "reuse detected"}
            rec["used_at"] = now
            session = self._mint(family, "ada@example.test")
            rec["child"] = session["refresh_token"]
            return 200, session
