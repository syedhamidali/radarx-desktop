#!/usr/bin/env python3
"""Build `radarx/releases.json`, the index the website reads, from the version folders.

Only PROMOTED versions are listed (a folder whose PROMOTION.json says "promoted"; a version taken
back by a rollback says "withdrawn" and disappears from the index, its files stay in the bucket).
Newest first. The output is deterministic: the same bucket gives the same bytes (no "generated at"
time; a release's date is the `pub_date` of its manifest).

Schema (also in docs/RELEASING.md):

    {"schema": 1, "product": "radarx", "latest": "0.3.0",
     "releases": [{"version": "0.3.0", "date": "2026-10-20T10:00:00Z", "notes": "...",
                   "assets": [{"os": "macos", "arch": "aarch64", "kind": "dmg",
                               "name": "radarx-desktop_0.3.0_aarch64.dmg", "size": 123,
                               "sha256": "<64 hex>", "url": "https://downloads.../x.dmg"}]}]}

Assets are the installers a person downloads. The updater payloads (`*.app.tar.gz`), signatures,
latest.json and SHA256SUMS are not listed.

Usage:  python scripts/release/build_index.py --bucket-dir DIR [--out FILE]
        (without --bucket-dir the R2 bucket from R2_ACCOUNT_ID / R2_BUCKET is read)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from release_common import (
    CANDIDATE_NAME,
    DEFAULT_BASE_URL,
    PROMOTION_NAME,
    SUMS_NAME,
    BucketMissingError,
    ReleaseError,
    asset_url,
    dump_json,
    iter_version_prefixes,
    open_bucket,
    parse_sums,
    version_key,
)

SCHEMA = 1
OSES = ("macos", "windows", "linux")
ARCHES = ("aarch64", "x86_64")

_KIND_BY_SUFFIX = (
    (".appimage", "appimage"),
    (".dmg", "dmg"),
    (".msi", "msi"),
    (".exe", "exe"),
    (".deb", "deb"),
    (".rpm", "rpm"),
    (".zip", "zip"),
)
_OS_BY_KIND = {
    "dmg": "macos",
    "exe": "windows",
    "msi": "windows",
    "deb": "linux",
    "rpm": "linux",
    "appimage": "linux",
}


def _arch_of(name: str) -> str | None:
    low = name.lower().replace("-", "_")
    tokens = set(low.replace(".", "_").split("_"))
    if tokens & {"aarch64", "arm64"}:
        return "aarch64"
    if tokens & {"x64", "amd64"} or "x86_64" in low:
        return "x86_64"
    return None


def _os_of_zip(name: str) -> str | None:
    low = name.lower()
    for words, os_ in ((("macos", "darwin", "mac"), "macos"), (("windows", "win"), "windows"),
                       (("linux",), "linux")):  # fmt: skip
        if any(w in low for w in words):
            return os_
    return None


def classify_asset(name: str) -> dict[str, str] | None:
    """{os, arch, kind} for an installer file name, None for files that are not installers.

    An installer whose system or architecture cannot be read from its name is an error: the
    website would not know where to offer it.
    """
    low = name.lower()
    if low.endswith((".sig", ".json", ".app.tar.gz")) or name == SUMS_NAME:
        return None
    kind = next((k for suffix, k in _KIND_BY_SUFFIX if low.endswith(suffix)), None)
    if kind is None:
        return None
    os_ = _OS_BY_KIND.get(kind) or _os_of_zip(name)
    arch = _arch_of(name)
    if os_ is None or arch is None:
        raise ReleaseError(f"cannot tell the system and architecture of {name}")
    return {"os": os_, "arch": arch, "kind": kind}


def release_entry(bucket: Any, version: str, prefix: str, base_url: str) -> dict[str, Any]:
    """The index entry of one version folder (reads its manifest, SHA256SUMS and file sizes)."""
    try:
        manifest = json.loads(bucket.get(prefix + CANDIDATE_NAME))
        sums = parse_sums(bucket.get(prefix + SUMS_NAME).decode("utf-8"))
    except BucketMissingError as exc:
        raise ReleaseError(f"v{version}: {exc}") from exc
    sizes = bucket.list_objects(prefix)
    assets = []
    for name in sorted(sums):
        info = classify_asset(name)
        if info is None:
            continue
        if prefix + name not in sizes:
            raise ReleaseError(f"v{version}: {name} is listed in {SUMS_NAME} but not in the bucket")
        assets.append(
            {
                **info,
                "name": name,
                "size": sizes[prefix + name],
                "sha256": sums[name],
                "url": asset_url(base_url, version, name),
            }
        )
    assets.sort(key=lambda a: (OSES.index(a["os"]), a["arch"], a["kind"], a["name"]))
    return {
        "version": version,
        "date": manifest["pub_date"],
        "notes": manifest["notes"],
        "assets": assets,
    }


def promotion_status(bucket: Any, prefix: str) -> str | None:
    try:
        return json.loads(bucket.get(prefix + PROMOTION_NAME)).get("status")
    except BucketMissingError:
        return None


def build_index(bucket: Any, base_url: str = DEFAULT_BASE_URL) -> dict[str, Any]:
    releases = [
        release_entry(bucket, v, prefix, base_url)
        for v, prefix in iter_version_prefixes(bucket)
        if promotion_status(bucket, prefix) == "promoted"
    ]
    releases.sort(key=lambda r: version_key(r["version"]), reverse=True)
    return {
        "schema": SCHEMA,
        "product": "radarx",
        "latest": releases[0]["version"] if releases else None,
        "releases": releases,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--bucket-dir", help="a local directory standing in for the bucket")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--out", type=Path, help="write here instead of standard output")
    args = ap.parse_args(argv)
    try:
        data = dump_json(build_index(open_bucket(args.bucket_dir), args.base_url))
    except ReleaseError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    if args.out:
        args.out.write_bytes(data)
    else:
        sys.stdout.write(data.decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
