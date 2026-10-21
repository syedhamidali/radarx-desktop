"""Shared pieces of the release scripts (Python 3.10+, standard library only).

The download channel (docs/RELEASING.md): a bucket behind https://downloads.labshope.com holds

    radarx/v<version>/<installers, .sig files, latest.json (candidate), SHA256SUMS, PROMOTION.json>
    radarx/latest.json     the LIVE update manifest the app reads   (written only by promote.py)
    radarx/releases.json   the index the website reads              (written only by promote.py)

This module has the naming rules, version ordering, manifest checks and the bucket abstraction
(a local directory for tests and dry runs, and a thin S3 / Cloudflare R2 implementation that calls
the AWS command line).
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

DEFAULT_BASE_URL = "https://downloads.labshope.com"
PRODUCT = "radarx"
LIVE_MANIFEST_KEY = f"{PRODUCT}/latest.json"
INDEX_KEY = f"{PRODUCT}/releases.json"
CANDIDATE_NAME = "latest.json"
SUMS_NAME = "SHA256SUMS"
PROMOTION_NAME = "PROMOTION.json"
#: Files in a version folder that are bookkeeping, not part of the checksummed release.
BOOKKEEPING = frozenset({SUMS_NAME, PROMOTION_NAME})
#: The update platforms every complete release has (Linux .deb / .rpm are not updater targets).
DEFAULT_REQUIRED_PLATFORMS = ("darwin-aarch64", "darwin-x86_64", "windows-x86_64")

CACHE_IMMUTABLE = "public, max-age=31536000, immutable"
CACHE_NO_CACHE = "no-cache"


class ReleaseError(Exception):
    """A release is not fit to publish. The message says why, in plain words."""


class BucketMissingError(ReleaseError):
    """The requested object does not exist."""


# --- versions ---------------------------------------------------------------------------------

_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def version_of(tag_or_version: str) -> str:
    """'v0.3.0' or '0.3.0' -> '0.3.0'. Refuses anything that is not a plain semantic version."""
    text = tag_or_version.strip()
    if text[:1] == "v":
        text = text[1:]
    if not _SEMVER.match(text):
        raise ReleaseError(f"{tag_or_version!r} is not a version like 0.3.0 or v0.3.0-rc.1")
    return text


def version_key(version: str) -> tuple:
    """Sort key following semantic-version precedence (a pre-release sorts below its release)."""
    m = _SEMVER.match(version_of(version))
    assert m is not None
    major, minor, patch, pre = m.groups()
    ids: tuple = ()
    if pre:
        ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return (int(major), int(minor), int(patch), 0 if pre else 1, ids)


def version_prefix(version: str) -> str:
    return f"{PRODUCT}/v{version_of(version)}/"


# --- checksums --------------------------------------------------------------------------------

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def check_file_name(name: str) -> str:
    if not _SAFE_NAME.match(name) or name in (".", ".."):
        raise ReleaseError(f"unsafe or unexpected file name: {name!r}")
    return name


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def format_sums(sums: dict[str, str]) -> str:
    """`sha256sum`-compatible text ('<hex>  <name>'), sorted by name."""
    return "".join(f"{sums[n]}  {n}\n" for n in sorted(sums))


def parse_sums(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        m = re.match(r"^([0-9a-fA-F]{64}) [ *](.+)$", line)
        if not m:
            raise ReleaseError(f"{SUMS_NAME} line {n} is not '<sha256>  <name>'")
        digest, name = m.group(1).lower(), check_file_name(m.group(2))
        if name in out:
            raise ReleaseError(f"{SUMS_NAME} lists {name} twice")
        out[name] = digest
    return out


# --- the update manifest (Tauri v2 latest.json) -------------------------------------------------


def _parse_date(text: str) -> datetime:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleaseError(f"pub_date {text!r} is not an RFC 3339 date") from exc


def validate_manifest(
    manifest: Any,
    *,
    version: str | None = None,
    require: tuple[str, ...] = (),
) -> None:
    """Check the shape of a Tauri v2 updater manifest; raise ReleaseError with the first problem.

    Needs: version, notes (text), pub_date (RFC 3339) and platforms, each with a url (https) and a
    non-empty base64 signature. `version`, when given, must equal the manifest's version.
    """
    if not isinstance(manifest, dict):
        raise ReleaseError("the manifest is not a JSON object")
    mv = manifest.get("version")
    if not isinstance(mv, str) or not mv:
        raise ReleaseError("the manifest has no version")
    mv_clean = version_of(mv)
    if version is not None and mv_clean != version_of(version):
        raise ReleaseError(f"manifest version {mv!r} does not match the release {version!r}")
    if not isinstance(manifest.get("notes"), str):
        raise ReleaseError("the manifest has no notes (text, may be empty)")
    pub = manifest.get("pub_date")
    if not isinstance(pub, str) or not pub:
        raise ReleaseError("the manifest has no pub_date")
    _parse_date(pub)
    platforms = manifest.get("platforms")
    if not isinstance(platforms, dict) or not platforms:
        raise ReleaseError("the manifest lists no platforms")
    for key, entry in platforms.items():
        if not isinstance(entry, dict):
            raise ReleaseError(f"platform {key}: not an object")
        sig = entry.get("signature")
        if not isinstance(sig, str) or not sig.strip():
            raise ReleaseError(f"platform {key}: the signature is missing")
        try:
            base64.b64decode(sig.strip(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ReleaseError(f"platform {key}: the signature is not base64") from exc
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            raise ReleaseError(f"platform {key}: the url is missing")
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.netloc:
            raise ReleaseError(f"platform {key}: the url is not https: {url!r}")
    missing = [p for p in require if p not in platforms]
    if missing:
        raise ReleaseError("the manifest lacks platform(s): " + ", ".join(missing))


def file_name_of_url(url: str) -> str:
    return check_file_name(unquote(urlsplit(url).path.rsplit("/", 1)[-1]))


def asset_url(base_url: str, version: str, name: str) -> str:
    return f"{base_url.rstrip('/')}/{version_prefix(version)}{quote(check_file_name(name))}"


def rewrite_manifest(manifest: dict, tag: str, base_url: str = DEFAULT_BASE_URL) -> dict:
    """A copy of the manifest whose asset urls point at the download host; nothing else changes.

    Signatures are copied, never recomputed. The file name of each asset is kept.
    """
    version = version_of(tag)
    out = copy.deepcopy(manifest)
    for entry in out["platforms"].values():
        entry["url"] = asset_url(base_url, version, file_name_of_url(entry["url"]))
    return out


def dump_json(obj: Any) -> bytes:
    """Deterministic JSON: sorted keys, two-space indent, UTF-8, trailing newline."""
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


# --- buckets ----------------------------------------------------------------------------------


def content_type_of(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".json"):
        return "application/json"
    if lower.endswith((".sig", ".txt")) or name == SUMS_NAME:
        return "text/plain; charset=utf-8"
    if lower.endswith(".dmg"):
        return "application/x-apple-diskimage"
    if lower.endswith(".gz"):
        return "application/gzip"
    if lower.endswith(".deb"):
        return "application/vnd.debian.binary-package"
    if lower.endswith(".rpm"):
        return "application/x-rpm"
    if lower.endswith(".zip"):
        return "application/zip"
    return "application/octet-stream"


def cache_control_of(key: str) -> str:
    """The two live names are re-checked on every request; everything else never changes."""
    return CACHE_NO_CACHE if key in (LIVE_MANIFEST_KEY, INDEX_KEY) else CACHE_IMMUTABLE


class LocalBucket:
    """A directory standing in for the bucket (tests, dry runs). `meta` remembers Cache-Control."""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.meta: dict[str, dict[str, str]] = {}

    def _path(self, key: str) -> Path:
        if key.startswith("/") or ".." in key.split("/") or not key:
            raise ReleaseError(f"bad object key {key!r}")
        return self.root / key

    def list_objects(self, prefix: str) -> dict[str, int]:
        found = {}
        for p in sorted(self.root.rglob("*")):
            if p.is_file():
                key = p.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    found[key] = p.stat().st_size
        return found

    def get(self, key: str) -> bytes:
        p = self._path(key)
        if not p.is_file():
            raise BucketMissingError(f"missing in the bucket: {key}")
        return p.read_bytes()

    def put(self, key: str, data: bytes, *, cache_control: str, content_type: str) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        self.meta[key] = {"cache_control": cache_control, "content_type": content_type}

    def put_file(self, key: str, path: Path, *, cache_control: str, content_type: str) -> None:
        self.put(
            key, Path(path).read_bytes(), cache_control=cache_control, content_type=content_type
        )

    def sha256(self, key: str) -> str:
        p = self._path(key)
        if not p.is_file():
            raise BucketMissingError(f"missing in the bucket: {key}")
        return sha256_file(p)


Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


class S3Bucket:
    """Cloudflare R2 (or any S3-compatible store) through the AWS command line.

    Credentials come from the environment (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY), never from
    arguments, so nothing secret appears in a process list or a log.
    """

    def __init__(
        self,
        bucket: str,
        endpoint_url: str,
        *,
        runner: Runner = subprocess.run,
        popen: Callable[..., Any] = subprocess.Popen,
    ):
        self.bucket, self.endpoint_url = bucket, endpoint_url
        self._run, self._popen = runner, popen

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> S3Bucket:
        env = dict(os.environ if env is None else env)
        account, bucket = env.get("R2_ACCOUNT_ID", ""), env.get("R2_BUCKET", "")
        if not account or not bucket:
            raise ReleaseError("R2_ACCOUNT_ID and R2_BUCKET must be set")
        return cls(bucket, f"https://{account}.r2.cloudflarestorage.com")

    def _aws(self, *args: str, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        cmd = ["aws", *args, "--endpoint-url", self.endpoint_url]
        res = self._run(cmd, input=data, capture_output=True, check=False)
        if res.returncode != 0:
            detail = (res.stderr or b"").decode(errors="replace").strip()[-400:]
            raise ReleaseError(f"aws {args[0]} {args[1]} failed: {detail}")
        return res

    def list_objects(self, prefix: str) -> dict[str, int]:
        res = self._aws("s3api", "list-objects-v2", "--bucket", self.bucket, "--prefix", prefix)
        body = json.loads(res.stdout.decode() or "{}") or {}
        return {o["Key"]: int(o["Size"]) for o in (body.get("Contents") or [])}

    def get(self, key: str) -> bytes:
        if key not in self.list_objects(key):
            raise BucketMissingError(f"missing in the bucket: {key}")
        return self._aws("s3", "cp", f"s3://{self.bucket}/{key}", "-").stdout

    def put(self, key: str, data: bytes, *, cache_control: str, content_type: str) -> None:
        self._aws(
            "s3", "cp", "-", f"s3://{self.bucket}/{key}",
            "--cache-control", cache_control, "--content-type", content_type,
            data=data,
        )  # fmt: skip

    def put_file(self, key: str, path: Path, *, cache_control: str, content_type: str) -> None:
        """Upload a local file (the AWS CLI streams it and uses multipart for big installers)."""
        self._aws(
            "s3",
            "cp",
            str(path),
            f"s3://{self.bucket}/{key}",
            "--cache-control",
            cache_control,
            "--content-type",
            content_type,
            "--only-show-errors",
        )

    def sha256(self, key: str) -> str:
        if key not in self.list_objects(key):
            raise BucketMissingError(f"missing in the bucket: {key}")
        cmd = [
            "aws",
            "s3",
            "cp",
            f"s3://{self.bucket}/{key}",
            "-",
            "--endpoint-url",
            self.endpoint_url,
        ]
        proc = self._popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        h = hashlib.sha256()
        for chunk in iter(lambda: proc.stdout.read(1 << 20), b""):
            h.update(chunk)
        if proc.wait() != 0:
            raise ReleaseError(f"could not read {key} from the bucket")
        return h.hexdigest()


def open_bucket(local_dir: str | None) -> Any:
    """A LocalBucket when a directory is given, else the R2 bucket from the environment."""
    return LocalBucket(local_dir) if local_dir else S3Bucket.from_env()


def iter_version_prefixes(bucket: Any) -> Iterator[tuple[str, str]]:
    """(version, prefix) for every radarx/v<version>/ folder in the bucket, oldest first."""
    seen: dict[str, str] = {}
    for key in bucket.list_objects(f"{PRODUCT}/v"):
        parts = key.split("/")
        if len(parts) == 3 and parts[1].startswith("v"):
            try:
                seen[version_of(parts[1])] = f"{PRODUCT}/{parts[1]}/"
            except ReleaseError:
                continue
    for v in sorted(seen, key=version_key):
        yield v, seen[v]
