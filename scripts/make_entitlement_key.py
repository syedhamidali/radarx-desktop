#!/usr/bin/env python3
"""Make the signing key pair for radarx access tokens (docs/ACCOUNTS.md "Owner set-up").

Run it ONCE, on your own computer, as the project owner:

    python scripts/make_entitlement_key.py --out ~/radarx-entitlement-private.pem

It prints the PUBLIC key (safe to paste into python/engine/account/jwks_seed.py and to commit) and
writes the PRIVATE key to the file you name, readable by you alone (mode 0600). It never prints the
private key. Then:

    supabase secrets set ENTITLEMENT_PRIVATE_KEY="$(cat ~/radarx-entitlement-private.pem)"
    rm -P ~/radarx-entitlement-private.pem        # (shred -u on Linux) once the secret is set

Keep a copy of the private key in your password manager if you want to be able to re-set the
secret; the repository must never contain it. The script refuses to write inside this repository,
and refuses to overwrite a file.

Needs only the ``cryptography`` package that the engine already depends on.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def thumbprint(jwk: dict[str, str]) -> str:
    """RFC 7638: SHA-256 of the canonical JSON of the required members (the Edge Function derives
    the same ``kid`` from the private key it holds)."""
    canonical = json.dumps(
        {k: jwk[k] for k in ("crv", "kty", "x", "y")}, separators=(",", ":"), sort_keys=True
    )
    return b64url(hashlib.sha256(canonical.encode("ascii")).digest())


def make_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP256R1())
    numbers = private.public_key().public_numbers()
    jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": b64url(numbers.x.to_bytes(32, "big")),
        "y": b64url(numbers.y.to_bytes(32, "big")),
    }
    jwk["kid"] = thumbprint(jwk)
    jwk.update({"alg": "ES256", "use": "sig", "key_ops": ["verify"]})
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return pem, jwk


def inside_repository(path: Path) -> bool:
    try:
        path.resolve().relative_to(REPOSITORY)
        return True
    except ValueError:
        return False


def tracked_by_git(path: Path) -> bool:
    try:
        out = subprocess.run(
            ["git", "-C", str(path.parent), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.returncode == 0 and out.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def write_private(path: Path, data: bytes) -> None:
    """Create ``path`` (never overwrite), mode 0600 from the start."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path, help="where to write the PRIVATE key (PEM)")
    args = ap.parse_args(argv)
    out: Path = args.out.expanduser()
    if inside_repository(out) or tracked_by_git(out):
        print(
            "refusing: the private key must not be written inside a git repository "
            "(it could be committed). Choose a path in your home folder.",
            file=sys.stderr,
        )
        return 2
    if out.exists():
        print(f"refusing to overwrite {out}", file=sys.stderr)
        return 2
    pem, jwk = make_key()
    write_private(out, pem)
    print(f"Private key written to {out} (mode 0600). It is NOT shown here.\n")
    print("1. Paste this PUBLIC key into ENTITLEMENT_SEED_JWKS in")
    print("   python/engine/account/jwks_seed.py (add it to the list; keep older keys during a rotation):\n")
    print(json.dumps(jwk, indent=4))
    print("\n2. Set the secret, then delete the file:\n")
    print(f'   supabase secrets set ENTITLEMENT_PRIVATE_KEY="$(cat {out})"')
    print(f"   rm -P {out}      # shred -u {out} on Linux")
    print("\n3. Deploy the functions and apply the migration (see docs/ACCOUNTS.md).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
