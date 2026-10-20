#!/usr/bin/env python3
"""Make a version the LIVE one: copy its candidate manifest to `radarx/latest.json` (what the app
reads) and rebuild `radarx/releases.json` (what the website reads). Nothing else writes those two.

Before anything is written it checks the version folder `radarx/v<version>/`:

* `latest.json` (the candidate manifest) and `SHA256SUMS` exist,
* the manifest is a valid Tauri v2 manifest for exactly this version, every url points into this
  folder on the download host, every referenced file and its `.sig` exist, each `.sig` equals the
  manifest signature,
* every file in the folder is listed in SHA256SUMS and its SHA-256 matches (a tampered or partly
  uploaded file refuses the promotion, and nothing changes).

Promoting a version OLDER than the live one is refused unless `--rollback` is given; a rollback
withdraws every newer version from the index (their files stay) so that `latest.json` and
`releases.json` agree. Installers are never deleted. Running it twice gives the same bytes.

Usage:  python scripts/release/promote.py promote --tag v0.3.0 [--rollback] [--dry-run]
        python scripts/release/promote.py verify --tag v0.3.0          (check only, writes nothing)
        python scripts/release/promote.py is-promoted --tag v0.3.0     (exit 0 when promoted)
        add --bucket-dir DIR to use a local directory instead of R2 (R2_ACCOUNT_ID, R2_BUCKET)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_index import build_index, promotion_status
from release_common import (
    BOOKKEEPING,
    CANDIDATE_NAME,
    DEFAULT_BASE_URL,
    DEFAULT_REQUIRED_PLATFORMS,
    INDEX_KEY,
    LIVE_MANIFEST_KEY,
    PROMOTION_NAME,
    SUMS_NAME,
    BucketMissingError,
    ReleaseError,
    cache_control_of,
    content_type_of,
    dump_json,
    file_name_of_url,
    iter_version_prefixes,
    open_bucket,
    parse_sums,
    validate_manifest,
    version_key,
    version_of,
    version_prefix,
)


@dataclass
class PromoteResult:
    version: str
    live_before: str | None
    rollback: bool
    withdrawn: list[str] = field(default_factory=list)
    wrote: list[str] = field(default_factory=list)
    dry_run: bool = False


def live_version(bucket: Any) -> str | None:
    try:
        return version_of(json.loads(bucket.get(LIVE_MANIFEST_KEY))["version"])
    except BucketMissingError:
        return None


def verify_candidate(
    bucket: Any,
    version: str,
    base_url: str = DEFAULT_BASE_URL,
    require: tuple[str, ...] = DEFAULT_REQUIRED_PLATFORMS,
) -> bytes:
    """Check the version folder; return the candidate manifest bytes. Raises ReleaseError."""
    prefix = version_prefix(version)
    try:
        manifest_bytes = bucket.get(prefix + CANDIDATE_NAME)
        sums = parse_sums(bucket.get(prefix + SUMS_NAME).decode("utf-8"))
    except BucketMissingError as exc:
        raise ReleaseError(f"v{version} is not ready to promote: {exc}") from exc
    try:
        manifest = json.loads(manifest_bytes)
    except ValueError as exc:
        raise ReleaseError(f"v{version}: the candidate manifest is not valid JSON") from exc
    validate_manifest(manifest, version=version, require=require)

    objects = bucket.list_objects(prefix)
    names = {k[len(prefix) :] for k in objects}
    unknown = sorted(n for n in names if n not in sums and n not in BOOKKEEPING)
    if unknown:
        raise ReleaseError(f"v{version}: files not listed in {SUMS_NAME}: {', '.join(unknown)}")
    for name, digest in sorted(sums.items()):
        if name not in names:
            raise ReleaseError(f"v{version}: {name} is listed in {SUMS_NAME} but missing")
        actual = bucket.sha256(prefix + name)
        if actual != digest:
            raise ReleaseError(f"v{version}: {name} does not match its SHA-256 in {SUMS_NAME}")

    folder_url = f"{base_url.rstrip('/')}/{prefix}"
    for key, entry in manifest["platforms"].items():
        if not entry["url"].startswith(folder_url):
            raise ReleaseError(f"v{version}: platform {key} url is not under {folder_url}")
        name = file_name_of_url(entry["url"])
        if name not in sums or name + ".sig" not in sums:
            raise ReleaseError(f"v{version}: platform {key}: {name} or its .sig is not released")
        sig = bucket.get(prefix + name + ".sig").decode("utf-8").strip()
        if sig != entry["signature"].strip():
            raise ReleaseError(f"v{version}: {name}.sig differs from the manifest signature")
    return manifest_bytes


def _put(bucket: Any, key: str, data: bytes) -> None:
    bucket.put(key, data, cache_control=cache_control_of(key), content_type=content_type_of(key))


def _marker(version: str, status: str) -> bytes:
    return dump_json({"status": status, "version": version})


def promote(
    bucket: Any,
    tag: str,
    *,
    rollback: bool = False,
    dry_run: bool = False,
    base_url: str = DEFAULT_BASE_URL,
    require: tuple[str, ...] = DEFAULT_REQUIRED_PLATFORMS,
) -> PromoteResult:
    version = version_of(tag)
    live = live_version(bucket)
    result = PromoteResult(version, live, rollback, dry_run=dry_run)

    if live is not None:
        if rollback and version_key(version) >= version_key(live):
            raise ReleaseError(f"--rollback needs a version older than the live v{live}")
        if not rollback and version_key(version) < version_key(live):
            raise ReleaseError(
                f"v{version} is older than the live v{live}: use the rollback option to go back"
            )
    elif rollback:
        raise ReleaseError("--rollback needs a live version to go back from; none is live")

    manifest_bytes = verify_candidate(bucket, version, base_url, require)  # nothing written yet

    if rollback:
        result.withdrawn = [
            v
            for v, prefix in iter_version_prefixes(bucket)
            if version_key(v) > version_key(version)
            and promotion_status(bucket, prefix) == "promoted"
        ]
    if dry_run:
        result.wrote = [version_prefix(version) + PROMOTION_NAME, INDEX_KEY, LIVE_MANIFEST_KEY]
        return result

    # 1. markers, 2. the index (built from them), 3. the live manifest last: apps flip at the end.
    for v in result.withdrawn:
        key = version_prefix(v) + PROMOTION_NAME
        _put(bucket, key, _marker(v, "withdrawn"))
        result.wrote.append(key)
    key = version_prefix(version) + PROMOTION_NAME
    _put(bucket, key, _marker(version, "promoted"))
    result.wrote.append(key)
    _put(bucket, INDEX_KEY, dump_json(build_index(bucket, base_url)))
    result.wrote.append(INDEX_KEY)
    _put(bucket, LIVE_MANIFEST_KEY, manifest_bytes)
    result.wrote.append(LIVE_MANIFEST_KEY)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("promote", "is-promoted", "verify"):
        sp = sub.add_parser(name)
        sp.add_argument("--tag", required=True)
        sp.add_argument("--bucket-dir", help="a local directory standing in for the bucket")
        sp.add_argument("--base-url", default=DEFAULT_BASE_URL)
        if name == "promote":
            sp.add_argument("--rollback", action="store_true", help="go back to an older version")
            sp.add_argument(
                "--dry-run", action="store_true", help="check everything, write nothing"
            )
    args = ap.parse_args(argv)
    try:
        bucket = open_bucket(args.bucket_dir)
        if args.cmd == "verify":
            verify_candidate(bucket, version_of(args.tag), args.base_url)
            print(f"v{version_of(args.tag)}: the candidate in the bucket is complete and intact")
            return 0
        if args.cmd == "is-promoted":
            version = version_of(args.tag)
            return 0 if promotion_status(bucket, version_prefix(version)) == "promoted" else 1
        res = promote(
            bucket, args.tag, rollback=args.rollback, dry_run=args.dry_run, base_url=args.base_url
        )
    except ReleaseError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    verb = "would promote" if res.dry_run else "promoted"
    print(f"{verb} v{res.version} (live before: {res.live_before or 'none'})")
    if res.withdrawn:
        print("withdrawn from the index: " + ", ".join("v" + v for v in res.withdrawn))
    for key in res.wrote:
        print(f"  {'(not written) ' if res.dry_run else ''}{key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
