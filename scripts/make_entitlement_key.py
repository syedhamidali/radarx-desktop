#!/usr/bin/env python3
"""Make the signing key pair for radarx access tokens (docs/ACCOUNTS.md "Owner set-up").

Run it ONCE, on your own computer, as the project owner:

    python scripts/make_entitlement_key.py --out ~/radarx-entitlement-private.pem

It prints the PUBLIC key (safe to paste into python/engine/account/jwks_seed.py and to commit) and
writes two files that only you can read (mode 0600): the PRIVATE key (``--out``, PEM) and, next to
it, ``<out>.env`` with the two Supabase secrets ``ENTITLEMENT_PRIVATE_KEY`` (used by the function
that signs) and ``ENTITLEMENT_PUBLIC_JWK`` (the only one the activity function needs). It never
prints the private key. Then:

    supabase secrets set --env-file ~/radarx-entitlement-private.pem.env

(an env file keeps the key out of the command line, where other programs of yours can read it
while the command runs; ``supabase secrets set NAME="$(cat file)"`` would not), then delete both
files. Deleting is not erasing: on APFS, SSDs and journaling file systems the blocks may survive
(``rm -P`` / ``shred`` do not help there), so keep the files on an encrypted disk, and rotate the
key (docs/ACCOUNTS.md) if you ever suspect it was exposed. Keep a copy of the private key in your
password manager if you want to be able to set the secret again; the repository must never
contain it. The script refuses to write inside a git repository, and refuses to overwrite a file
or to follow a link.

Needs only the ``cryptography`` package that the engine already depends on.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
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
    """Create ``path`` (never overwrite, never follow a link), mode 0600 from the start."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def refuse(message: str) -> int:
    print(f"refusing: {message}", file=sys.stderr)
    return 2


def env_file_text(pem: bytes, jwk: dict[str, object]) -> bytes:
    """The two secrets in dotenv form (double quotes, ``\\n`` for the PEM's line ends)."""
    private = pem.decode("ascii").strip().replace("\n", "\\n")
    public = json.dumps({k: jwk[k] for k in ("kty", "crv", "x", "y", "kid")}, separators=(",", ":"))
    return (
        f'ENTITLEMENT_PRIVATE_KEY="{private}"\nENTITLEMENT_PUBLIC_JWK=\'{public}\'\n'
    ).encode("ascii")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path, help="where to write the PRIVATE key (PEM)")
    args = ap.parse_args(argv)
    out: Path = args.out.expanduser()
    env_out = out.with_name(out.name + ".env")
    for target in (out, env_out):
        if inside_repository(target) or tracked_by_git(target):
            return refuse(
                "the private key must not be written inside a git repository (it could be "
                "committed). Choose a path in your home folder."
            )
        if os.path.lexists(target):  # also a link, even one that points nowhere
            return refuse(f"{target} exists already (a file or a link): will not overwrite it")
    if not out.parent.is_dir():
        return refuse(f"the folder {out.parent} does not exist: create it first")
    pem, jwk = make_key()
    try:
        write_private(out, pem)
        write_private(env_out, env_file_text(pem, jwk))
    except OSError as exc:
        for target in (out, env_out):
            with contextlib.suppress(OSError):
                if target.exists() and not target.is_symlink():
                    target.unlink()
        return refuse(f"could not write {out} ({exc.strerror or type(exc).__name__})")
    print(f"Private key written to {out} and the secrets to {env_out} (mode 0600).")
    print("The private key is NOT shown here.\n")
    print("1. Paste this PUBLIC key into ENTITLEMENT_SEED_JWKS in")
    print("   python/engine/account/jwks_seed.py (add it to the list; keep the old ones):")
    print()
    print(json.dumps(jwk, indent=4))
    print("\n2. Set the secrets from the env file (not the command line), then delete both:\n")
    print(f"   supabase secrets set --env-file {env_out}")
    print(f"   rm {out} {env_out}   # not erasing on an SSD: see the notes in this script")
    print("\n3. Deploy the functions and apply the migration (see docs/ACCOUNTS.md).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
