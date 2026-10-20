"""Where the account server is and how long a verified session may be used offline.

Everything here is PUBLIC by design: the project URL and the publishable (anon) key only identify
the Supabase project; row-level security protects the data. They are the same two values as
``src/lib/auth/config.ts``. Never put the database password or a service-role key anywhere in this
repository.
"""

from __future__ import annotations

import os
import sys

DEFAULT_PROJECT_URL = "https://bfyimkgvnqiqedcwprpi.supabase.co"
DEFAULT_PUBLISHABLE_KEY = "sb_publishable_PiB4d1KKf-BEMEIXS5t6AQ_WfLZXlbA"

#: Same number as ``OFFLINE_GRACE_DAYS`` in ``src/lib/auth/config.ts``: a stored session is accepted
#: this long after the last time the server vouched for it (sign-in or token refresh).
OFFLINE_GRACE_DAYS = 30
DAY_S = 86_400

#: The ``aud`` claim of a signed-in user's access token.
AUDIENCE = "authenticated"

#: Seconds of clock difference tolerated when checking ``exp`` / ``iat``.
LEEWAY_S = 60

#: Network timeout for every call to the account server (a command must never hang on it).
HTTP_TIMEOUT_S = 8.0


def is_frozen() -> bool:
    """True inside an installer build (PyInstaller sets ``sys.frozen``)."""
    return bool(getattr(sys, "frozen", False))


def _override(name: str, default: str) -> str:
    # A developer may point a source checkout at another project (or a local stack). An installer
    # build never reads these, so the shipped engine always talks to the real project.
    if is_frozen():
        return default
    return os.environ.get(name, "").strip() or default


def project_url() -> str:
    return _override("RADARX_SUPABASE_URL", DEFAULT_PROJECT_URL).rstrip("/")


def publishable_key() -> str:
    return _override("RADARX_SUPABASE_KEY", DEFAULT_PUBLISHABLE_KEY)


def issuer() -> str:
    """The ``iss`` claim GoTrue puts in every token."""
    return f"{project_url()}/auth/v1"


def jwks_url() -> str:
    return f"{issuer()}/.well-known/jwks.json"


def auth_disabled() -> bool:
    """The development bypass: ``RADARX_AUTH_DISABLED=1``, honoured ONLY when not running frozen.

    The tests and developers use it. An installer (PyInstaller) build ignores it, so setting the
    variable in front of an installed app changes nothing. (A source checkout can of course be
    edited; see docs/ACCOUNTS.md "What this does not do".)
    """
    if is_frozen():
        return False
    return os.environ.get("RADARX_AUTH_DISABLED", "").strip() == "1"


def web_gate_enabled() -> bool:
    """Web-service mode: the gate is ON unless the operator sets ``RADARX_WEB_REQUIRE_ACCOUNT=0``.

    Like the bypass above, this is ignored in a frozen build.
    """
    if is_frozen():
        return True
    return os.environ.get("RADARX_WEB_REQUIRE_ACCOUNT", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


#: Entitlement tokens (personal access tokens, docs/ACCOUNTS.md): a fixed issuer that is not
#: Supabase's, an audience of its own, and a token lifetime of one hour (set by the function).
ENTITLEMENT_ISSUER = "radarx-desktop/entitlement/v1"
ENTITLEMENT_AUDIENCE = "radarx-cli"
PAT_PREFIX = "rxd_pat_"


def is_default_project() -> bool:
    return project_url() == DEFAULT_PROJECT_URL


def functions_url(name: str) -> str:
    """An Edge Function of the project (``exchange-api-token``, ``log-activity``)."""
    return f"{project_url()}/functions/v1/{name}"


def entitlement_jwks_override() -> str:
    """A key document for the entitlement tokens of ANOTHER project (a developer's local stack or
    the tests): only read in a source checkout, never by an installer build."""
    if is_frozen():
        return ""
    return os.environ.get("RADARX_ENTITLEMENT_JWKS", "").strip()
