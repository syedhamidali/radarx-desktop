"""A copy of the project's PUBLIC signing key document (the same JSON anyone can GET from
``<project>/auth/v1/.well-known/jwks.json``), so the first check works with no network at all.

It holds a public key only. If the project rotates its key, a token with an unknown ``kid`` makes
the engine fetch the current document (see :mod:`engine.account.tokens`), so this copy going stale
does not lock anyone out. Fetched 2026-10-20: ES256 / P-256, so no shared secret is involved.
"""

SEED_URL = "https://bfyimkgvnqiqedcwprpi.supabase.co/auth/v1/.well-known/jwks.json"

SEED_JWKS = {
    "keys": [
        {
            "alg": "ES256",
            "crv": "P-256",
            "ext": True,
            "key_ops": ["verify"],
            "kid": "a87a7630-4b20-46b5-87b1-56c24eb27295",
            "kty": "EC",
            "use": "sig",
            "x": "sLZQ0lXwHhqS4M2gCoF77hF8Qd1cuKlndt69JjdFQdY",
            "y": "6J8Becv5rvb17Lz10uLTZLdLLjeP1kDI7pW_-KQG_fA",
        }
    ]
}
