"""Open radar networks (engine.radar_networks): DWD and FMI providers against a local fake
server, the catalogue they are listed in, and the server handlers. No network."""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import numpy as np
import pytest

import engine.radar_networks as rn
from engine import layer_provenance
from engine.environment.core import Environment
from engine.environment.net import HttpClient
from engine.environment.settings import EnvSettings
from engine.reader import RadarReader

ROOT = Path(__file__).resolve().parents[2]
CATALOGUE = ROOT / "src" / "lib" / "data" / "radarCatalogue.json"


# ── a tiny ODIM writer (xradar reads it) ─────────────────────────────────────────────────────


def write_odim(
    path: Path,
    *,
    moments: dict[str, float],
    elangles: list[float],
    lat: float = 51.4056,
    lon: float = 6.9671,
    height: float = 185.0,
    t0: datetime | None = None,
    nrays: int = 36,
    nbins: int = 20,
    obj: str = "SCAN",
) -> None:
    import h5py

    t0 = t0 or datetime(2026, 10, 21, 21, 50, 35, tzinfo=timezone.utc)
    with h5py.File(path, "w") as h:
        h.attrs["Conventions"] = np.bytes_(b"ODIM_H5/V2_2")
        what = h.create_group("what")
        what.attrs["object"] = np.bytes_(obj.encode())
        what.attrs["version"] = np.bytes_(b"H5rad 2.2")
        what.attrs["date"] = np.bytes_(t0.strftime("%Y%m%d").encode())
        what.attrs["time"] = np.bytes_(t0.strftime("%H%M%S").encode())
        what.attrs["source"] = np.bytes_(b"WMO:10410,NOD:deess")
        where = h.create_group("where")
        where.attrs["lat"], where.attrs["lon"], where.attrs["height"] = lat, lon, height
        h.create_group("how").attrs["beamwidth"] = 1.0
        for i, el in enumerate(elangles, start=1):
            ts = t0 + timedelta(seconds=23 * (i - 1))
            ds = h.create_group(f"dataset{i}")
            dw = ds.create_group("where")
            dw.attrs["elangle"] = float(el)
            dw.attrs["nbins"] = nbins
            dw.attrs["rstart"] = 0.0
            dw.attrs["rscale"] = 250.0
            dw.attrs["nrays"] = nrays
            dw.attrs["a1gate"] = 0
            dwh = ds.create_group("what")
            dwh.attrs["product"] = np.bytes_(b"SCAN")
            dwh.attrs["startdate"] = np.bytes_(ts.strftime("%Y%m%d").encode())
            dwh.attrs["starttime"] = np.bytes_(ts.strftime("%H%M%S").encode())
            te = ts + timedelta(seconds=20)
            dwh.attrs["enddate"] = np.bytes_(te.strftime("%Y%m%d").encode())
            dwh.attrs["endtime"] = np.bytes_(te.strftime("%H%M%S").encode())
            how = ds.create_group("how")
            az = np.arange(nrays) * 360.0 / nrays
            how.attrs["startazA"] = az
            how.attrs["stopazA"] = az + 360.0 / nrays
            secs = ts.timestamp() + np.arange(nrays) * 20.0 / nrays
            how.attrs["startazT"] = secs
            how.attrs["stopazT"] = secs + 20.0 / nrays
            for j, (q, value) in enumerate(moments.items(), start=1):
                d = ds.create_group(f"data{j}")
                w = d.create_group("what")
                w.attrs["quantity"] = np.bytes_(q.upper().encode())
                w.attrs["gain"] = 0.5
                w.attrs["offset"] = -32.0
                w.attrs["nodata"] = 255.0
                w.attrs["undetect"] = 0.0
                raw = np.full((nrays, nbins), int((value + 32.0) / 0.5), dtype=np.uint8)
                d.create_dataset("data", data=raw)


# ── a fake HTTP transport for the environment's HttpClient ─────────────────────────────────


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body, self._pos = body, 0
        self.headers = {"Content-Length": str(len(body))}

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = len(self._body) - self._pos
        chunk = self._body[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk

    def close(self) -> None:
        pass


class FakeWeb:
    """URL -> bytes; records every request and its User-Agent."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)
        self.requests: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def opener(self, req: urllib.request.Request, timeout: float) -> Any:
        url = req.full_url
        with self.lock:
            self.requests.append((url, req.get_header("User-agent") or ""))
        body = self.files.get(url)
        if body is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", Message(), None)  # type: ignore[arg-type]
        return _Resp(body)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    web = FakeWeb({})
    e = Environment(EnvSettings(cache_dir=str(tmp_path / "cache")))
    e.http = HttpClient(
        offline=lambda: e.offline, opener=web.opener, sleep=lambda s: None, min_interval_s=0
    )
    monkeypatch.setattr(rn, "_env", lambda: e)
    rn.clear_listing_cache()
    yield e, web
    rn.clear_listing_cache()


# ── DWD ───────────────────────────────────────────────────────────────────────────────────────

DWD_T0 = datetime(2026, 10, 21, 21, 50, 57, tzinfo=timezone.utc)


def dwd_name(moment: str, sweep: int, t: datetime, site: str = "ess") -> str:
    return (
        f"ras07-stqual-vol5minng01_sweeph5onem_{moment}_{sweep:02d}-"
        f"{t.strftime('%Y%m%d%H%M%S')}00-{site}-10410-hd5"
    )


def dwd_index(entries: list[tuple[str, int]]) -> bytes:
    lines = ['<html><body><pre><a href="../">../</a>']
    for name, size in entries:
        lines.append(f'<a href="{name}">{name[:40]}..&gt;</a> 21-Oct-2026 21:51:22 {size}')
    lines.append("</pre></body></html>")
    return "\n".join(lines).encode()


def make_dwd(tmp_path: Path, web: FakeWeb, *, sweeps: int = 10, missing_last: bool = False) -> dict:
    """Two complete volumes (21:45, 21:50) plus a partial one (21:55); files on the fake server."""
    z_entries, v_entries = [], []
    files: dict[str, bytes] = {}
    slots = [DWD_T0 - timedelta(minutes=5), DWD_T0, DWD_T0 + timedelta(minutes=5)]
    for k, slot in enumerate(slots):
        n = 3 if k == 2 else sweeps
        if missing_last and k == 1:
            n = sweeps - 1
        for s in range(n):
            t = slot + timedelta(seconds=23 * s)
            for moment, entries in (("dbzh", z_entries), ("vradh", v_entries)):
                name = dwd_name(moment, s, t)
                p = tmp_path / name
                write_odim(
                    p,
                    moments={moment: 20.0 + s if moment == "dbzh" else -5.0},
                    elangles=[5.5 - s * 0.5],
                    t0=t,
                )
                body = p.read_bytes()
                entries.append((name, len(body)))
                files[rn.DwdProvider.folder("ess", moment) + name] = body
    web.files.update(files)
    web.files[rn.DwdProvider.folder("ess", "dbzh")] = dwd_index(z_entries)
    web.files[rn.DwdProvider.folder("ess", "vradh")] = dwd_index(v_entries)
    return {"z": z_entries, "v": v_entries}


def test_dwd_listing_groups_sweeps_into_five_minute_volumes(tmp_path: Path, env) -> None:
    _, web = env
    make_dwd(tmp_path, web)
    rows = rn.dwd_parse_listing(web.files[rn.DwdProvider.folder("ess", "dbzh")].decode())
    assert len(rows) == 23 and rows[0]["moment"] == "dbzh" and rows[0]["site"] == "ess"
    vols = rn.dwd_group_volumes(rows)
    assert [v["key"] for v in vols] == ["202610212145", "202610212150", "202610212155"]
    assert [v["complete"] for v in vols] == [True, True, False]
    assert vols[1]["scan_time"] == "2026-10-21T21:50:57Z"
    assert vols[1]["size"] == sum(f["size"] for f in vols[1]["files"])
    assert vols[2]["sweeps"] == 3


def test_dwd_latest_is_the_newest_complete_volume_and_lists_once(tmp_path: Path, env) -> None:
    _, web = env
    make_dwd(tmp_path, web)
    p = rn.DwdProvider()
    info = p.latest_scan_info("ess")
    assert info["status"] == "ok" and info["key"] == "202610212150"
    assert info["n_files"] == 20 and info["complete"] and info["sweeps"] == 10
    assert info["size"] > 0 and "age_s" in info and info["now"].endswith("Z")
    listings = [u for u, _ in web.requests]
    assert len(listings) == 2  # the two folder indexes, nothing else
    assert all(ua.startswith("radarx-desktop/") for _, ua in web.requests)
    p.latest_scan_info("ess")
    assert len(web.requests) == 2  # cached


def test_dwd_incomplete_newest_volume_is_skipped(tmp_path: Path, env) -> None:
    _, web = env
    make_dwd(tmp_path, web, missing_last=True)
    info = rn.DwdProvider().latest_scan_info("ess")
    assert info["key"] == "202610212145"


def test_dwd_unknown_site_offline_and_errors_become_statuses(tmp_path: Path, env) -> None:
    e, _web = env
    p = rn.DwdProvider()
    assert p.latest_scan_info("xyz")["status"] == "error"
    assert p.latest_scan_info("../ess")["status"] == "error"
    assert p.latest_scan_info("ess")["status"] in ("offline", "error")  # 404 from the fake server
    e.settings = e.settings.updated({"offline": True})
    rn.clear_listing_cache()
    assert p.latest_scan_info("ess")["status"] == "offline"


def test_dwd_open_assembles_ten_sweeps_with_both_moments(tmp_path: Path, env) -> None:
    _, web = env
    make_dwd(tmp_path, web)
    p = rn.DwdProvider()
    reader = RadarReader()
    reader.tmp_dir = str(tmp_path / "rtmp")
    schema = p.open(reader, "ess", "202610212150")
    assert len(schema["sweeps"]) == 10
    assert {"DBZH", "VRADH"} <= set(schema["variables"])
    assert schema["source_type"] == "dwd_opendata" and "opendata.dwd.de" in schema["source"]
    ds = reader.get_sweep_dataset(3, mask=False)
    assert float(ds["DBZH"].values.mean()) == pytest.approx(23.0)
    assert float(ds["VRADH"].values.mean()) == pytest.approx(-5.0)
    # Twenty downloads, each capped at its listed size, then every temporary file removed.
    downloads = [u for u, _ in web.requests if u.endswith("-hd5")]
    assert len(downloads) == 20
    assert not list((tmp_path / "rtmp").glob("dwd-*.h5"))
    # The credit travels with the layer and into exports.
    prov = layer_provenance.describe_reader(reader, schema)
    assert (
        prov["licence"].startswith("CC BY 4.0") and "Deutscher Wetterdienst" in prov["attribution"]
    )
    assert "DWD" in prov["source"]
    attrs = layer_provenance.attrs(prov)
    assert (
        attrs["data_licence"].startswith("CC BY 4.0")
        and "Deutscher Wetterdienst" in attrs["data_attribution"]
    )


def test_dwd_open_refuses_unlisted_or_incomplete_volumes(tmp_path: Path, env) -> None:
    _, web = env
    make_dwd(tmp_path, web)
    p = rn.DwdProvider()
    reader = RadarReader()
    with pytest.raises(ValueError, match=r"not .*listed"):
        p.open(reader, "ess", "202610210000")
    with pytest.raises(ValueError, match="incomplete"):
        p.open(reader, "ess", "202610212155")
    with pytest.raises(ValueError, match="Invalid"):
        p.open(reader, "ess", "../x")


# ── FMI ───────────────────────────────────────────────────────────────────────────────────────


def s3_xml(
    keys: list[tuple[str, int]] = (), prefixes: list[str] = (), token: str | None = None
) -> bytes:
    ns = "http://s3.amazonaws.com/doc/2006-03-01/"
    parts = [f'<?xml version="1.0" encoding="UTF-8"?><ListBucketResult xmlns="{ns}"><Name>b</Name>']
    for k, size in keys:
        parts.append(
            f"<Contents><Key>{k}</Key><LastModified>2023-02-07T10:40:37.000Z</LastModified><Size>{size}</Size></Contents>"
        )
    for p in prefixes:
        parts.append(f"<CommonPrefixes><Prefix>{p}</Prefix></CommonPrefixes>")
    if token:
        parts.append(f"<NextContinuationToken>{token}</NextContinuationToken>")
    parts.append("</ListBucketResult>")
    return "".join(parts).encode()


def make_fmi(tmp_path: Path, web: FakeWeb) -> bytes:
    pvol = tmp_path / "pvol.h5"
    write_odim(
        pvol,
        moments={"dbzh": 30.0, "vradh": 2.0},
        elangles=[0.3, 0.7],
        lat=60.9039,
        lon=27.1081,
        height=139.0,
        obj="PVOL",
    )
    body = pvol.read_bytes()
    b = rn.FMI_BUCKET_URL
    web.files[f"{b}?list-type=2&prefix=/&max-keys=1000&delimiter=/"] = s3_xml(
        prefixes=["/2023/", "/test/"]
    )
    web.files[f"{b}?list-type=2&prefix=/2023/&max-keys=1000&delimiter=/"] = s3_xml(
        prefixes=["/2023/02/"]
    )
    web.files[f"{b}?list-type=2&prefix=/2023/02/&max-keys=1000&delimiter=/"] = s3_xml(
        prefixes=["/2023/02/07/", "/2023/02/08/"]
    )
    web.files[f"{b}?list-type=2&prefix=/2023/02/07/&max-keys=1000"] = s3_xml(
        [
            ("/2023/02/07/202302071035_fianj_PVOL.h5", len(body)),
            ("/2023/02/07/202302071035_fikes_PVOL.h5", 5),
        ]
    )
    web.files[f"{b}?list-type=2&prefix=/2023/02/08/&max-keys=1000"] = s3_xml(
        [
            ("/2023/02/08/202302080005_fianj_PVOL.h5", len(body)),
            ("/2023/02/08/202302081200_fikes_PVOL.h5", 5),
        ]
    )
    web.files[f"{b}/2023/02/08/202302080005_fianj_PVOL.h5"] = body
    return body


def test_s3_listing_parser() -> None:
    keys, prefixes, token = rn.s3_parse_listing(s3_xml([("/a/b.h5", 7)], ["/2023/"], "tok"))
    assert keys == [{"key": "/a/b.h5", "size": 7, "last_modified": "2023-02-07T10:40:37.000Z"}]
    assert prefixes == ["/2023/"] and token == "tok"


def test_fmi_latest_is_the_newest_object_of_the_sample(tmp_path: Path, env) -> None:
    _, web = env
    make_fmi(tmp_path, web)
    p = rn.FmiProvider()
    assert p.archive_only
    info = p.latest_scan_info("fianj")
    assert info["status"] == "ok" and info["key"] == "/2023/02/08/202302080005_fianj_PVOL.h5"
    assert info["scan_time"] == "2023-02-08T00:05:00Z" and info["size"] > 100
    assert p.days_available() == ["2023-02-07", "2023-02-08"]
    assert [s["name"] for s in p.list_scans("fikes", "2023-02-07")] == [
        "202302071035_fikes_PVOL.h5"
    ]
    assert p.latest_scan_info("nope")["status"] == "error"


def test_fmi_open_downloads_one_volume_and_credits_it(tmp_path: Path, env) -> None:
    _, web = env
    make_fmi(tmp_path, web)
    p = rn.FmiProvider()
    reader = RadarReader()
    reader.tmp_dir = str(tmp_path / "rtmp")
    schema = p.open(reader, "fianj", "/2023/02/08/202302080005_fianj_PVOL.h5")
    assert len(schema["sweeps"]) == 2 and "DBZH" in schema["variables"]
    assert schema["source_type"] == "fmi_opendata" and schema["source"].startswith("https://")
    assert not list((tmp_path / "rtmp").glob("fmi-*"))
    prov = layer_provenance.describe_reader(reader, schema)
    assert (
        prov["licence"] == "CC BY 4.0" and "Finnish Meteorological Institute" in prov["attribution"]
    )
    with pytest.raises(ValueError):
        p.open(reader, "fianj", "/2023/02/08/202302081200_fikes_PVOL.h5")  # another site's file


# ── the catalogue the map ships ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def catalogue() -> dict:
    return json.loads(CATALOGUE.read_text(encoding="utf-8"))


def test_catalogue_entries_have_a_source_a_date_and_an_access_class(catalogue: dict) -> None:
    sites = catalogue["sites"]
    assert len(sites) >= 900
    ids = [s["id"] for s in sites]
    assert len(ids) == len(set(ids))
    access = {"open-loadable", "open-link", "key-required", "restricted", "unknown"}
    for s in sites:
        assert s["source"] in catalogue["sources"], s["id"]
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", s["retrieved"]), s["id"]
        assert -90 <= s["lat"] <= 90 and -180 <= s["lon"] <= 180, s["id"]
        assert s["data_access"] in access, s["id"]
        assert s["name"].strip() and s["country"].strip(), s["id"]
        if s["data_access"] == "open-loadable":
            assert s["provider"] in ("nexrad", "dwd", "fmi") and s["provider_site"], s["id"]
        if s["data_access"] in ("open-link", "key-required"):
            assert s["url"].startswith("https://"), s["id"]
    for src in catalogue["sources"].values():
        assert src["licence"] and src["attribution"] and src["url"].startswith("https://")


def test_catalogue_loadable_sites_match_the_engine_providers(catalogue: dict) -> None:
    import engine.cloud as cloud

    by_provider: dict[str, set[str]] = {}
    for s in catalogue["sites"]:
        if s["data_access"] == "open-loadable":
            by_provider.setdefault(s["provider"], set()).add(s["provider_site"])
    assert by_provider["nexrad"] == set(cloud._NEXRAD_STATIONS)
    assert by_provider["dwd"] == set(rn.DWD_SITES)
    assert by_provider["fmi"] == set(rn.FMI_SITES)
    # DWD positions were cross-checked against the radars' own files when the catalogue was built.
    assert len(catalogue["dwd_position_check"]) == len(rn.DWD_SITES)
    for line in catalogue["dwd_position_check"]:
        assert float(line.rsplit(" ", 2)[-2]) < 1.0, line


# ── server handlers ───────────────────────────────────────────────────────────────────────────


def make_handler():
    from server import ConnectionHandler

    ws = AsyncMock()
    ws.remote_address = ("127.0.0.1", 1)
    sent: list[dict] = []

    async def fake_send(raw) -> None:
        if isinstance(raw, str):
            sent.append(json.loads(raw))

    ws.send = fake_send
    handler = ConnectionHandler(ws)
    handler._sent = sent  # type: ignore[attr-defined]
    return handler


async def ask(handler, **msg) -> list[dict]:
    start = len(handler._sent)
    await handler.handle_message(json.dumps(msg))
    return handler._sent[start:]


@pytest.mark.asyncio
async def test_server_lists_providers_and_answers_latest_info(tmp_path: Path, env) -> None:
    _, web = env
    make_dwd(tmp_path, web)
    h = make_handler()
    out = await ask(h, type="list_network_providers")
    assert out[-1]["type"] == "network_providers"
    ids = {p["id"]: p for p in out[-1]["providers"]}
    assert set(ids) == {"dwd", "fmi"}
    assert (
        ids["dwd"]["listing_bytes"] > 0 and ids["dwd"]["licence"] and "ess" in ids["dwd"]["sites"]
    )
    out = await ask(h, type="get_network_latest_info", provider="dwd", site="ess")
    assert out[-1]["type"] == "network_latest_info" and out[-1]["status"] == "ok"
    out = await ask(h, type="get_network_latest_info", provider="nope", site="ess")
    assert out[-1]["status"] == "error"
    out = await ask(h, type="list_network_scans", provider="dwd", site="ess", date="2026-10-21")
    assert out[-1]["type"] == "network_scans" and len(out[-1]["scans"]) == 3


@pytest.mark.asyncio
async def test_server_opens_a_network_scan_as_a_new_file(tmp_path: Path, env) -> None:
    _, web = env
    make_dwd(tmp_path, web)
    h = make_handler()
    out = await ask(h, type="open_network_scan", provider="dwd", site="ess", key="202610212150")
    opened = [m for m in out if m["type"] == "file_opened"]
    assert opened, out[-1]
    assert len(opened[0]["data"]["sweeps"]) == 10
    assert opened[0]["provenance"]["licence"].startswith("CC BY 4.0")
    out = await ask(h, type="open_network_scan", provider="dwd", site="ess", key="../x")
    assert out[-1]["type"] == "error"
