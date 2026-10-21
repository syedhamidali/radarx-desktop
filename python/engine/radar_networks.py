"""Open radar networks beyond NEXRAD: one provider interface, polite HTTP, no key, no account.

Each provider answers four questions for the radar-network map and the Cloud panel:

* :meth:`NetworkProvider.list_sites`: which sites it can serve (ids, the network, the credit);
* :meth:`NetworkProvider.latest_scan_info`: the newest volume of a site: name, scan time, size
  (a listing, nothing downloaded; never raises: ``status`` says ok / not_found / offline / error);
* :meth:`NetworkProvider.list_scans`: the volumes of a site on one UTC day;
* :meth:`NetworkProvider.open`: download exactly one listed volume and hand it to a
  :class:`~engine.reader.RadarReader` (xradar opens it; nothing is interpolated or invented).

Every request goes through the environment framework's :class:`~engine.environment.net.HttpClient`
(honest ``User-Agent``, at most two connections, a gap between requests to one host, retries with
backoff, offline mode, a byte cap equal to the size the user approved).  Listings are cached for a
short time so the card and the open flow do not list twice.

The user interface asks for consent before each listing and each download, with the host and the
size from here; the engine itself never contacts a network on its own.

Providers
---------
``dwd``  Deutscher Wetterdienst Open Data (opendata.dwd.de): the last two days of volume sweeps of
         the 17 German C-band radars as ODIM HDF5, **one file per sweep and per moment**.  A volume
         is the ten sweeps of one five-minute scan; the provider lists the ``sweep_vol_z`` (DBZH)
         and ``sweep_vol_v`` (VRADH) folders, groups the files by their five-minute slot and, on
         open, downloads the 20 files and assembles one datatree (sweep_0 .. sweep_9).  DBZH and
         VRADH of a sweep share the same rays and gates in DWD's scan strategy, so the merge is a
         plain join; if a pair ever differed the sweep keeps both on their own coordinates and the
         open reports it.  Licence: CC BY 4.0 with the source acknowledged (DWD legal notice).

``fmi``  Finnish Meteorological Institute open data on AWS (the public bucket
         ``fmi-opendata-radar-volume-hdf5``, eu-west-1, anonymous): full polar volumes (ODIM HDF5
         ``PVOL``) of ten Finnish radars.  The
         bucket holds a **two-day sample (7 and 8 February 2023)**, not a live feed; "latest" is
         the newest object in it and the provider says so.  Licence: CC BY 4.0 (AWS Open Data
         registry entry for the bucket).
"""

from __future__ import annotations

import contextlib
import copy
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from engine.environment.core import Environment
    from engine.reader import RadarReader

logger = logging.getLogger(__name__)

_LISTING_TTL_S = 90.0
_LISTING_CACHE_MAX = 64
_LISTING_CACHE: OrderedDict[str, tuple[float, Any]] = OrderedDict()
_LISTING_LOCK = threading.Lock()
_SITE_RE = re.compile(r"^[a-z0-9]{3,8}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _cached(key: str, make: Any, *, ttl: float = _LISTING_TTL_S, use_cache: bool = True) -> Any:
    now = time.monotonic()
    if use_cache:
        with _LISTING_LOCK:
            hit = _LISTING_CACHE.get(key)
            if hit is not None and hit[0] > now:
                _LISTING_CACHE.move_to_end(key)
                return copy.deepcopy(hit[1])
    value = make()
    with _LISTING_LOCK:
        _LISTING_CACHE[key] = (time.monotonic() + ttl, copy.deepcopy(value))
        _LISTING_CACHE.move_to_end(key)
        while len(_LISTING_CACHE) > _LISTING_CACHE_MAX:
            _LISTING_CACHE.popitem(last=False)
    return value


def clear_listing_cache() -> None:
    with _LISTING_LOCK:
        _LISTING_CACHE.clear()


def _env() -> Environment:
    from engine.environment.core import Environment

    return Environment.default()


def _is_offline_error(exc: BaseException) -> bool:
    from engine.environment.errors import OfflineError

    if isinstance(exc, OfflineError):
        return True
    text = str(exc).lower()
    return any(
        w in text
        for w in ("could not download", "timed out", "connection", "unreachable", "name or service")
    )


class NetworkProvider:
    """Base class: fill the attributes, implement the four methods."""

    id: ClassVar[str] = ""
    name: ClassVar[str] = ""
    network: ClassVar[str] = ""
    #: Credit line shown in the card, the consent dialog and the provenance of opened data.
    attribution: ClassVar[str] = ""
    licence: ClassVar[str] = ""
    homepage: ClassVar[str] = ""
    #: Where requests go (shown in the consent dialog).
    host: ClassVar[str] = ""
    #: What a listing costs (shown in the consent dialog before any listing).
    listing_text: ClassVar[str] = ""
    listing_bytes: ClassVar[int] = 0
    #: True when "latest" is the newest object of a static archive, not a live feed.
    archive_only: ClassVar[bool] = False
    #: Day the archive picker should start on ("" = today); a static sample names its last day.
    default_date: ClassVar[str] = ""
    description: ClassVar[str] = ""

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "network": self.network,
            "attribution": self.attribution,
            "licence": self.licence,
            "homepage": self.homepage,
            "host": self.host,
            "listing_text": self.listing_text,
            "listing_bytes": self.listing_bytes,
            "archive_only": self.archive_only,
            "default_date": self.default_date,
            "description": self.description,
            "sites": self.list_sites(),
        }

    def list_sites(self) -> list[str]:  # pragma: no cover - abstract
        raise NotImplementedError

    def check_site(self, site: Any) -> str:
        s = str(site or "").strip().lower()
        if not _SITE_RE.match(s) or s not in self.list_sites():
            raise ValueError(f"{self.name}: unknown site {site!r}")
        return s

    def list_scans(self, site: str, date: str, *, use_cache: bool = True) -> list[dict[str, Any]]:
        """Volumes of *site* on the UTC day *date* (YYYY-MM-DD), oldest first. May raise."""
        raise NotImplementedError  # pragma: no cover

    def latest_scan_info(self, site: str, *, use_cache: bool = True) -> dict[str, Any]:
        """The newest volume of *site*. Never raises; ``status`` tells what happened."""
        try:
            s = self.check_site(site)
        except ValueError as exc:
            return {"status": "error", "provider": self.id, "site": str(site), "message": str(exc)}
        try:
            scan = self._newest(s, use_cache=use_cache)
        except Exception as exc:
            logger.warning("%s: latest scan of %s failed: %s", self.id, s, exc)
            offline = _is_offline_error(exc)
            return {
                "status": "offline" if offline else "error",
                "provider": self.id,
                "site": s,
                "message": (
                    f"Could not reach {self.host.split(' ')[0]} (offline or blocked)."
                    if offline
                    else f"Listing failed: {exc}"
                ),
            }
        if scan is None:
            return {
                "status": "not_found",
                "provider": self.id,
                "site": s,
                "message": f"No volume of {s} in the listing.",
            }
        return _with_age({"status": "ok", "provider": self.id, "site": s, **scan})

    def _newest(self, site: str, *, use_cache: bool) -> dict[str, Any] | None:
        raise NotImplementedError  # pragma: no cover

    def open(self, reader: RadarReader, site: str, key: str) -> dict[str, Any]:
        """Download the volume *key* of *site* (as listed) into the reader. Returns the schema."""
        raise NotImplementedError  # pragma: no cover

    def credit(self) -> dict[str, str]:
        return {"source": self.name, "attribution": self.attribution, "licence": self.licence}


def _with_age(info: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    info["now"] = _iso(now)
    t = info.get("scan_time")
    if t:
        dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        info["age_s"] = max(0.0, (now - dt).total_seconds())
    return info


# ── DWD ───────────────────────────────────────────────────────────────────────────────────────

DWD_BASE = "https://opendata.dwd.de/weather/radar/sites/"
DWD_SITES = (
    "asb", "boo", "drs", "eis", "ess", "fbg", "fld", "hnr", "isn", "mem",
    "neu", "nhb", "oft", "pro", "ros", "tur", "umd",
)  # fmt: skip
DWD_MOMENTS = {"dbzh": "sweep_vol_z", "vradh": "sweep_vol_v"}
DWD_SWEEPS = 10
_DWD_LINE = re.compile(
    r'href="(ras07-[^"]*_sweeph5onem_([a-z]+)_(\d\d)-(\d{14})\d\d-([a-z]{3})-\d+-hd5)"'
    r"[^\n]*?(\d{2}-[A-Za-z]{3}-\d{4} \d{2}:\d{2}:\d{2})\s+(\d+)"
)


def dwd_parse_listing(html: str) -> list[dict[str, Any]]:
    """Rows of a DWD sweep folder index: name, moment, sweep, time (UTC), site, size."""
    rows = []
    for name, moment, sweep, stamp, site, _mod, size in _DWD_LINE.findall(html):
        try:
            t = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        rows.append(
            {
                "name": name,
                "moment": moment,
                "sweep": int(sweep),
                "time": t,
                "site": site,
                "size": int(size),
            }
        )
    return rows


def dwd_group_volumes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group sweep files into five-minute volumes (DWD's ``vol5minng01`` scan: ten sweeps).

    The key of a volume is the start of its five-minute slot (``YYYYMMDDHHMM``); the scan time is
    the time of its first sweep file.  ``complete`` is True when all ten reflectivity sweeps are
    present (the velocity sweeps are added when they are).  Nothing is filled in for a sweep that
    is missing.
    """
    vols: dict[str, dict[str, Any]] = {}
    for r in rows:
        t = r["time"]
        slot = t.replace(minute=t.minute - t.minute % 5, second=0, microsecond=0)
        key = slot.strftime("%Y%m%d%H%M")
        v = vols.setdefault(
            key,
            {"key": key, "slot": slot, "files": {}, "first": t},
        )
        v["files"][(r["moment"], r["sweep"])] = r
        if t < v["first"]:
            v["first"] = t
    out = []
    for key in sorted(vols):
        v = vols[key]
        z = [v["files"][k] for k in sorted(v["files"]) if k[0] == "dbzh"]
        files = [v["files"][k] for k in sorted(v["files"])]
        out.append(
            {
                "key": key,
                "scan_time": _iso(v["first"]),
                "name": f"DWD vol5minng01 {v['slot'].strftime('%Y-%m-%d %H:%M')} UTC",
                "size": sum(f["size"] for f in files),
                "n_files": len(files),
                "sweeps": len(z),
                "complete": len(z) == DWD_SWEEPS,
                "files": [
                    {
                        "name": f["name"],
                        "moment": f["moment"],
                        "sweep": f["sweep"],
                        "size": f["size"],
                    }
                    for f in files
                ],
            }
        )
    return out


class DwdProvider(NetworkProvider):
    id = "dwd"
    name = "DWD Open Data radar volumes"
    network = "DWD"
    attribution = "Source: Deutscher Wetterdienst (DWD), Open Data radar volume sweeps"
    licence = "CC BY 4.0 with the source acknowledged (DWD legal notice)"
    homepage = "https://opendata.dwd.de/weather/radar/sites/"
    host = "opendata.dwd.de (anonymous, read-only)"
    listing_text = (
        "Two folder indexes (reflectivity and radial velocity sweeps of the last two days): "
        "file names, times and sizes only. No radar data."
    )
    listing_bytes = 2_200_000
    description = (
        "The last two days of the 17 German C-band radars: ten sweeps every five minutes, "
        "DBZH and VRADH (ODIM HDF5, one file per sweep). A volume is assembled from 20 files."
    )

    def list_sites(self) -> list[str]:
        return list(DWD_SITES)

    @staticmethod
    def folder(site: str, moment: str) -> str:
        return f"{DWD_BASE}{DWD_MOMENTS[moment]}/{site}/hdf5/filter_simple/"

    def _rows(self, site: str, *, use_cache: bool) -> list[dict[str, Any]]:
        def fetch() -> list[dict[str, Any]]:
            env = _env()
            rows: list[dict[str, Any]] = []
            for moment in DWD_MOMENTS:
                html = env.http.get_bytes(self.folder(site, moment), max_bytes=4_000_000)
                rows += dwd_parse_listing(html.decode("utf-8", "replace"))
            return rows

        return _cached(f"dwd:{site}", fetch, use_cache=use_cache)

    def volumes(self, site: str, *, use_cache: bool = True) -> list[dict[str, Any]]:
        return dwd_group_volumes(self._rows(site, use_cache=use_cache))

    def list_scans(self, site: str, date: str, *, use_cache: bool = True) -> list[dict[str, Any]]:
        s = self.check_site(site)
        if not _DATE_RE.match(str(date)):
            raise ValueError(f"Invalid date {date!r}: expected YYYY-MM-DD")
        day = str(date).replace("-", "")
        return [v for v in self.volumes(s, use_cache=use_cache) if v["key"].startswith(day)]

    def _newest(self, site: str, *, use_cache: bool) -> dict[str, Any] | None:
        complete = [v for v in self.volumes(site, use_cache=use_cache) if v["complete"]]
        return complete[-1] if complete else None

    def open(self, reader: RadarReader, site: str, key: str) -> dict[str, Any]:
        import xarray as xr
        import xradar as xd

        from engine import hdf5_guard

        s = self.check_site(site)
        if not re.fullmatch(r"\d{12}", str(key)):
            raise ValueError(f"Invalid DWD volume key {key!r}")
        vol = next((v for v in self.volumes(s) if v["key"] == key), None)
        if vol is None:
            raise ValueError(f"Volume {key} of {s} is not (or no longer) listed on opendata.dwd.de")
        if not vol["complete"]:
            raise ValueError(f"Volume {key} of {s} is incomplete ({vol['sweeps']} of 10 sweeps)")
        env = _env()
        tmp = Path(reader.tmp_dir) if reader.tmp_dir else Path(env.cache.tmp_dir)
        tmp.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        notes: list[str] = []
        try:
            per_sweep: dict[int, dict[str, Path]] = {}
            for f in vol["files"]:
                dest = tmp / f"dwd-{s}-{key}-{f['moment']}-{f['sweep']:02d}.h5"
                env.http.download(
                    self.folder(s, f["moment"]) + f["name"], dest, max_bytes=f["size"]
                )
                paths.append(dest)
                per_sweep.setdefault(f["sweep"], {})[f["moment"]] = dest
            nodes: dict[str, xr.Dataset] = {}
            root: xr.Dataset | None = None
            for sweep in sorted(per_sweep):
                parts = per_sweep[sweep]
                merged: xr.Dataset | None = None
                for moment in DWD_MOMENTS:
                    p = parts.get(moment)
                    if p is None:
                        continue
                    with hdf5_guard.native_section():
                        tree = xd.io.open_odim_datatree(str(p))
                    ds = tree["sweep_0"].to_dataset().load()
                    if root is None:
                        root = tree.to_dataset().load()
                    if merged is None:
                        merged = ds
                    elif _same_grid(merged, ds):
                        extra = [v for v in ds.data_vars if v not in merged.data_vars]
                        merged = merged.assign({v: ds[v] for v in extra})
                    else:
                        notes.append(f"sweep {sweep}: {moment} has its own rays or gates")
                        merged = xr.merge([merged, ds], compat="override", join="outer")
                if merged is not None:
                    nodes[f"sweep_{sweep}"] = merged
            if not nodes:
                raise ValueError("no sweep could be read")
            dtree = xr.DataTree.from_dict(nodes)
            if root is not None:
                dtree.dataset = root
            # The per-sweep files carry no readable radar name: name the assembled volume so the
            # file tab, the provenance and exports say which radar and which five-minute slot.
            dtree.attrs.update(
                {
                    "instrument_name": f"DWD {s.upper()}",
                    "institution": "Deutscher Wetterdienst (DWD) Open Data",
                    "source": f"opendata.dwd.de vol5minng01 {s} {key}",
                }
            )
            source = f"{self.folder(s, 'dbzh')}dwd-{s}-vol5minng01-{key}"
            schema = reader.adopt_datatree(dtree, source, "odim", source_type="dwd_opendata")
            schema["instrument_name"] = f"DWD {s.upper()}"
            schema["source_name"] = vol["name"]
            if notes:
                schema["notes"] = notes
            reader._source_credit = self.credit()
            return schema
        finally:
            for p in paths:
                with contextlib.suppress(OSError):
                    p.unlink()


def _same_grid(a: Any, b: Any) -> bool:
    import numpy as np

    try:
        return (
            a.sizes.get("azimuth") == b.sizes.get("azimuth")
            and a.sizes.get("range") == b.sizes.get("range")
            and bool(np.array_equal(a["azimuth"].values, b["azimuth"].values))
            and bool(np.array_equal(a["range"].values, b["range"].values))
        )
    except Exception:
        return False


# ── FMI (AWS Open Data sample) ────────────────────────────────────────────────────────────────

FMI_BUCKET_URL = "https://fmi-opendata-radar-volume-hdf5.s3-eu-west-1.amazonaws.com/"
FMI_SITES = (
    "fianj",
    "fikes",
    "fikor",
    "fikuo",
    "filuo",
    "finur",
    "fipet",
    "fiuta",
    "fivih",
    "fivim",
)
_FMI_KEY = re.compile(r"^/?(\d{4})/(\d{2})/(\d{2})/(\d{12})_(fi[a-z]{3})_PVOL\.h5$")
_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def s3_parse_listing(xml_bytes: bytes) -> tuple[list[dict[str, Any]], list[str], str | None]:
    """Keys (key, size, last_modified), common prefixes and the continuation token of a
    ListObjectsV2 answer."""
    root = ET.fromstring(xml_bytes)
    keys = []
    for c in root.iter(f"{_S3_NS}Contents"):
        keys.append(
            {
                "key": c.findtext(f"{_S3_NS}Key", ""),
                "size": int(c.findtext(f"{_S3_NS}Size", "0") or 0),
                "last_modified": c.findtext(f"{_S3_NS}LastModified", ""),
            }
        )
    prefixes = [p.findtext(f"{_S3_NS}Prefix", "") for p in root.iter(f"{_S3_NS}CommonPrefixes")]
    token = root.findtext(f"{_S3_NS}NextContinuationToken") or None
    return keys, prefixes, token


class FmiProvider(NetworkProvider):
    id = "fmi"
    name = "FMI open radar data on AWS (sample)"
    network = "FMI"
    attribution = "Data: Finnish Meteorological Institute (FMI) open data, via AWS Open Data"
    licence = "CC BY 4.0"
    homepage = "https://registry.opendata.aws/fmi-radar/"
    host = "fmi-opendata-radar-volume-hdf5.s3-eu-west-1.amazonaws.com (public bucket, anonymous)"
    listing_text = (
        "An S3 listing of one day's folder (file names, sizes and times only). No radar data."
    )
    listing_bytes = 400_000
    archive_only = True
    default_date = "2023-02-08"
    description = (
        "Full polar volumes (ODIM HDF5) of ten Finnish C-band radars. The public bucket holds a "
        "two-day sample (7 and 8 February 2023), not a live feed."
    )

    def list_sites(self) -> list[str]:
        return list(FMI_SITES)

    def _list_prefix(
        self, prefix: str, *, delimiter: bool
    ) -> tuple[list[dict[str, Any]], list[str]]:
        env = _env()
        keys: list[dict[str, Any]] = []
        prefixes: list[str] = []
        token: str | None = None
        for _ in range(20):  # 20 pages x 1000 keys is far more than one day holds
            url = f"{FMI_BUCKET_URL}?list-type=2&prefix={prefix}&max-keys=1000"
            if delimiter:
                url += "&delimiter=/"
            if token:
                from urllib.parse import quote

                url += "&continuation-token=" + quote(token, safe="")
            k, p, token = s3_parse_listing(env.http.get_bytes(url, max_bytes=4_000_000))
            keys += k
            prefixes += p
            if not token:
                break
        return keys, prefixes

    def _day_keys(self, day: str, *, use_cache: bool) -> list[dict[str, Any]]:
        def fetch() -> list[dict[str, Any]]:
            return self._list_prefix(f"/{day}/", delimiter=False)[0]

        return _cached(f"fmi:{day}", fetch, use_cache=use_cache)

    def _days(self, *, use_cache: bool) -> list[str]:
        """``YYYY/MM/DD`` folders of the bucket, oldest first (three small listings)."""

        def fetch() -> list[str]:
            days: list[str] = []
            for y in self._list_prefix("/", delimiter=True)[1]:
                if not re.fullmatch(r"/\d{4}/", y):
                    continue
                for m in self._list_prefix(y, delimiter=True)[1]:
                    for d in self._list_prefix(m, delimiter=True)[1]:
                        if re.fullmatch(r"/\d{4}/\d{2}/\d{2}/", d):
                            days.append(d.strip("/"))
            return sorted(days)

        return _cached("fmi:days", fetch, ttl=3600.0, use_cache=use_cache)

    @staticmethod
    def _scan(k: dict[str, Any]) -> dict[str, Any] | None:
        m = _FMI_KEY.match(k["key"])
        if not m:
            return None
        stamp, site = m.group(4), m.group(5)
        try:
            t = datetime.strptime(stamp, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return {
            "key": k["key"],
            "site": site,
            "name": k["key"].rsplit("/", 1)[-1],
            "scan_time": _iso(t),
            "size": int(k["size"]),
            "n_files": 1,
            "complete": True,
            "last_modified": k.get("last_modified", ""),
        }

    def list_scans(self, site: str, date: str, *, use_cache: bool = True) -> list[dict[str, Any]]:
        s = self.check_site(site)
        if not _DATE_RE.match(str(date)):
            raise ValueError(f"Invalid date {date!r}: expected YYYY-MM-DD")
        day = str(date).replace("-", "/")
        scans = [self._scan(k) for k in self._day_keys(day, use_cache=use_cache)]
        return sorted(
            (sc for sc in scans if sc is not None and sc["site"] == s), key=lambda sc: sc["key"]
        )

    def days_available(self) -> list[str]:
        return [d.replace("/", "-") for d in self._days(use_cache=True)]

    def _newest(self, site: str, *, use_cache: bool) -> dict[str, Any] | None:
        for day in reversed(self._days(use_cache=use_cache)):
            scans = self.list_scans(site, day.replace("/", "-"), use_cache=use_cache)
            if scans:
                return scans[-1]
        return None

    def open(self, reader: RadarReader, site: str, key: str) -> dict[str, Any]:
        s = self.check_site(site)
        m = _FMI_KEY.match(str(key))
        if not m or m.group(5) != s:
            raise ValueError(f"Invalid FMI object key {key!r}")
        day = f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
        scan = next(
            (sc for sc in self.list_scans(s, day.replace("/", "-")) if sc["key"] == key), None
        )
        if scan is None:
            raise ValueError(f"{key} is not listed in the bucket")
        env = _env()
        tmp = Path(reader.tmp_dir) if reader.tmp_dir else Path(env.cache.tmp_dir)
        tmp.mkdir(parents=True, exist_ok=True)
        dest = tmp / f"fmi-{scan['name']}"
        url = FMI_BUCKET_URL + key.lstrip("/") if not key.startswith("/") else FMI_BUCKET_URL + key
        try:
            env.http.download(url, dest, max_bytes=scan["size"])
            schema = reader.open_file(str(dest))
            reader._path = url
            schema["source"] = url
            schema["source_type"] = "fmi_opendata"
            schema["source_name"] = scan["name"]
            # The sample files carry no readable radar name ("None"): name the radar for the
            # file tab, the provenance and exports.
            schema["instrument_name"] = f"FMI {s.upper()}"
            with contextlib.suppress(Exception):
                reader._dtree.attrs["instrument_name"] = f"FMI {s.upper()}"
            reader._source_credit = self.credit()
            return schema
        finally:
            with contextlib.suppress(OSError):
                dest.unlink()


# ── Registry ──────────────────────────────────────────────────────────────────────────────────

_PROVIDERS: dict[str, NetworkProvider] = {}
_PROVIDERS_LOCK = threading.Lock()


def providers() -> dict[str, NetworkProvider]:
    with _PROVIDERS_LOCK:
        if not _PROVIDERS:
            for p in (DwdProvider(), FmiProvider()):
                _PROVIDERS[p.id] = p
        return dict(_PROVIDERS)


def get_provider(provider_id: Any) -> NetworkProvider:
    p = providers().get(str(provider_id or "").strip().lower())
    if p is None:
        known = ", ".join(sorted(providers()))
        raise ValueError(f"Unknown radar network provider {provider_id!r} (known: {known})")
    return p


def describe_providers() -> list[dict[str, Any]]:
    return [p.describe() for p in providers().values()]


def nearest_scan(
    scans: list[dict[str, Any]], target: datetime, *, max_gap: timedelta | None = None
):
    """The listed scan whose time is closest to *target*; None beyond *max_gap*."""
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    best = None
    for sc in scans:
        t = datetime.fromisoformat(sc["scan_time"].replace("Z", "+00:00"))
        gap = abs((t - target).total_seconds())
        if best is None or gap < best[0]:
            best = (gap, sc)
    if best is None or (max_gap is not None and best[0] > max_gap.total_seconds()):
        return None
    return best[1]
