"""The NEXRAD cases of the nowcast verification (docs/NOWCAST.md) and their download.

Not part of the installed package. Run by a developer, on purpose, with ``--download``: it
fetches Level II volumes of the public ``unidata-nexrad-level2`` archive (anonymous, free of
charge, NOAA Big Data Program) for the windows below into one folder per event and records the
sizes. Nothing is fetched unless asked.

Two kinds of event:

* ``tuning``: used to measure the motion-error statistics that drive the ensemble
  (``engine/nowcast/motion_error_default.json``); **never** scored;
* ``verification``: scored against persistence; never used to tune anything.

    python benchmarks/nowcast_events.py --list
    python benchmarks/nowcast_events.py --download --dir ~/nowcast_cases [--only V1,V2]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BUCKET = "unidata-nexrad-level2"

#: id, role, radar, start (UTC), hours, season / storm mode (the description is the owner's
#: expectation; what the data really shows is reported by the harness from the echo fraction)
EVENTS = [
    {
        "id": "T1",
        "role": "tuning",
        "radar": "KPAH",
        "start": "2021-12-11T00:30",
        "hours": 3.0,
        "season": "winter (Dec)",
        "mode": "supercells / QLCS (Mayfield outbreak)",
    },
    {
        "id": "T2",
        "role": "tuning",
        "radar": "KFWS",
        "start": "2019-10-21T01:00",
        "hours": 3.0,
        "season": "autumn (Oct)",
        "mode": "night supercell (Dallas tornado)",
    },
    {
        "id": "V1",
        "role": "verification",
        "radar": "KTLX",
        "start": "2013-05-20T19:00",
        "hours": 3.0,
        "season": "spring (May)",
        "mode": "supercell (Moore tornado)",
    },
    {
        "id": "V2",
        "role": "verification",
        "radar": "KDMX",
        "start": "2020-08-10T15:30",
        "hours": 3.0,
        "season": "summer (Aug)",
        "mode": "squall line / derecho (Iowa)",
    },
    {
        "id": "V3",
        "role": "verification",
        "radar": "KHGX",
        "start": "2017-08-27T06:00",
        "hours": 3.0,
        "season": "summer (Aug)",
        "mode": "tropical rain bands (Harvey, Houston)",
    },
    {
        "id": "V4",
        "role": "verification",
        "radar": "KTBW",
        "start": "2022-09-28T14:00",
        "hours": 3.0,
        "season": "autumn (Sep)",
        "mode": "hurricane eyewall and bands (Ian): rotating motion",
    },
    {
        "id": "V5",
        "role": "verification",
        "radar": "KBUF",
        "start": "2022-11-18T12:00",
        "hours": 3.0,
        "season": "late autumn (Nov)",
        "mode": "lake-effect snow band: quasi-stationary",
    },
    {
        "id": "V6",
        "role": "verification",
        "radar": "KDAX",
        "start": "2017-02-07T12:00",
        "hours": 3.0,
        "season": "winter (Feb)",
        "mode": "atmospheric river, stratiform rain (California)",
    },
    {
        "id": "V7",
        "role": "verification",
        "radar": "KDDC",
        "start": "2016-05-24T22:00",
        "hours": 3.0,
        "season": "spring (May)",
        "mode": "supercells (Dodge City)",
    },
    {
        "id": "V8",
        "role": "verification",
        "radar": "KFTG",
        "start": "2021-01-08T18:00",
        "hours": 3.0,
        "season": "winter (Jan)",
        "mode": "quiet / clear-air (Denver)",
    },
]


def window(ev: dict) -> tuple[datetime, datetime]:
    t0 = datetime.fromisoformat(ev["start"]).replace(tzinfo=timezone.utc)
    return t0, t0 + timedelta(hours=float(ev["hours"]))


def _s3():
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    return boto3.client("s3", config=Config(signature_version=UNSIGNED, max_pool_connections=16))


def list_keys(ev: dict) -> list[dict]:
    """Objects of the event window (name, key, size)."""
    s3 = _s3()
    t0, t1 = window(ev)
    days = {t0.date(), (t1 - timedelta(seconds=1)).date()}
    out = []
    for day in sorted(days):
        prefix = f"{day.year}/{day.month:02d}/{day.day:02d}/{ev['radar']}/"
        pag = s3.get_paginator("list_objects_v2")
        for page in pag.paginate(Bucket=BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                name = obj["Key"].rsplit("/", 1)[-1]
                if name.endswith("_MDM") or not name.startswith(ev["radar"]):
                    continue
                try:
                    stamp = datetime.strptime(name[4:19], "%Y%m%d_%H%M%S").replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    continue
                if t0 <= stamp < t1:
                    out.append({"name": name, "key": obj["Key"], "size": int(obj.get("Size", 0))})
    out.sort(key=lambda o: o["name"])
    return out


def download(ev: dict, root: Path, *, max_mb: float = 600.0) -> dict:
    """Fetch the event's files into ``root/<id>_<radar>/``; returns the record (sizes, count)."""
    folder = root / f"{ev['id']}_{ev['radar']}"
    folder.mkdir(parents=True, exist_ok=True)
    keys = list_keys(ev)
    total = sum(k["size"] for k in keys)
    if total > max_mb * 1e6:
        # keep every other file only if it is still over budget: say so in the record
        print(
            f"  {ev['id']}: {total / 1e6:.0f} MB is over the {max_mb:.0f} MB budget; "
            "trimming the end",
            file=sys.stderr,
        )
        acc = 0
        kept = []
        for k in keys:
            if acc + k["size"] > max_mb * 1e6:
                break
            kept.append(k)
            acc += k["size"]
        keys = kept
    s3 = _s3()
    t0 = time.perf_counter()

    def fetch(k: dict) -> int:
        dest = folder / k["name"]
        if dest.exists() and dest.stat().st_size == k["size"]:
            return k["size"]
        s3.download_file(BUCKET, k["key"], str(dest))
        return k["size"]

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=8) as pool:  # small files: latency-bound, so parallel
        got = sum(pool.map(fetch, keys))
    rec = {
        **ev,
        "folder": folder.name,
        "n_files": len(keys),
        "bytes": got,
        "mb": round(got / 1e6, 1),
        "first": keys[0]["name"] if keys else None,
        "last": keys[-1]["name"] if keys else None,
        "download_s": round(time.perf_counter() - t0, 1),
        "source": f"s3://{BUCKET} (anonymous; NOAA NEXRAD Level II, public domain)",
    }
    (folder / "download_record.json").write_text(json.dumps(rec, indent=2) + "\n")
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--list", action="store_true", help="list the events (no network)")
    ap.add_argument(
        "--download", action="store_true", help="fetch the volumes (network, consented)"
    )
    ap.add_argument("--dir", default="~/nowcast_cases", help="folder for the events")
    ap.add_argument("--only", default="", help="comma-separated event ids")
    ap.add_argument("--max-mb", type=float, default=600.0, help="size budget per event")
    args = ap.parse_args()
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    events = [e for e in EVENTS if not only or e["id"] in only]
    if args.list or not args.download:
        for e in events:
            t0, t1 = window(e)
            print(
                f"{e['id']}  {e['role']:<12} {e['radar']}  {t0:%Y-%m-%d %H:%M}-{t1:%H:%M} UTC  "
                f"{e['season']}; {e['mode']}"
            )
        return 0
    root = Path(args.dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    records = []
    for e in events:
        print(f"{e['id']} {e['radar']} {e['start']} ...", file=sys.stderr, flush=True)
        try:
            rec = download(e, root, max_mb=args.max_mb)
        except Exception as exc:  # one failed event must not stop the others
            rec = {**e, "error": str(exc)}
            print(f"  failed: {exc}", file=sys.stderr)
        records.append(rec)
        print(
            f"  {rec.get('n_files', 0)} files, {rec.get('mb', 0)} MB", file=sys.stderr, flush=True
        )
    (root / "events_record.json").write_text(json.dumps(records, indent=2) + "\n")
    print(json.dumps(records, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
