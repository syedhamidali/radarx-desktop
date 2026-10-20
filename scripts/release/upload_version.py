#!/usr/bin/env python3
"""Upload a prepared version folder (see rewrite_latest.py) to `radarx/v<version>/` in the bucket.

It re-checks the folder first (SHA256SUMS against the files, the manifest against the tag), refuses
to touch a version that was already promoted or withdrawn (released folders are immutable), and
uploads in an order that never leaves a half-published candidate looking complete: installers and
signatures first, then `latest.json`, then `SHA256SUMS`. It never writes `radarx/latest.json` or
`radarx/releases.json` (only promote.py does).

Usage:  python scripts/release/upload_version.py --tag v0.3.0 --dir staging [--bucket-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_index import promotion_status
from release_common import (
    BOOKKEEPING,
    CANDIDATE_NAME,
    DEFAULT_REQUIRED_PLATFORMS,
    SUMS_NAME,
    ReleaseError,
    cache_control_of,
    content_type_of,
    open_bucket,
    parse_sums,
    sha256_file,
    validate_manifest,
    version_of,
    version_prefix,
)


def check_folder(folder: Path, version: str) -> dict[str, str]:
    """The folder is exactly what rewrite_latest.py made: files match SHA256SUMS, nothing extra."""
    try:
        sums = parse_sums((folder / SUMS_NAME).read_text(encoding="utf-8"))
        manifest = json.loads((folder / CANDIDATE_NAME).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReleaseError(
            f"{folder}: run rewrite_latest.py first ({exc.filename} is missing)"
        ) from exc
    validate_manifest(manifest, version=version, require=DEFAULT_REQUIRED_PLATFORMS)
    names = {p.name for p in folder.iterdir()}
    extra = sorted(names - set(sums) - BOOKKEEPING)
    if extra:
        raise ReleaseError(f"files not in {SUMS_NAME}: {', '.join(extra)}")
    for name, digest in sums.items():
        if not (folder / name).is_file() or sha256_file(folder / name) != digest:
            raise ReleaseError(f"{name} does not match {SUMS_NAME}")
    return sums


def upload_version(bucket: Any, folder: Path, tag: str) -> list[str]:
    version = version_of(tag)
    prefix = version_prefix(version)
    status = promotion_status(bucket, prefix)
    if status is not None:
        raise ReleaseError(f"v{version} is already {status}: a released folder is immutable")
    sums = check_folder(folder, version)
    last = [CANDIDATE_NAME, SUMS_NAME]
    order = sorted(n for n in sums if n not in last) + last
    for name in order:
        key = prefix + name
        bucket.put_file(
            key,
            folder / name,
            cache_control=cache_control_of(key),
            content_type=content_type_of(name),
        )
    return [prefix + n for n in order]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--dir", required=True, type=Path)
    ap.add_argument("--bucket-dir", help="a local directory standing in for the bucket")
    args = ap.parse_args(argv)
    try:
        keys = upload_version(open_bucket(args.bucket_dir), args.dir, args.tag)
    except ReleaseError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(f"uploaded {len(keys)} files to {version_prefix(args.tag)} (candidate, not live)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
