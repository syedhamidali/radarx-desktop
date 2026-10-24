"""The ``rxd report`` command (registered by :mod:`engine.hydro.cli`).

One HTML document of a rainfall event from the radar volumes: accumulation map, basins, gauge
validation,
methods, run record, provenance and credits (``docs/HYDROLOGY.md``). The run record in the document
makes
the same report again with ``--from-record``.
"""

# ruff: noqa: E501
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

EXAMPLES = (
    "examples:\n"
    "  rxd report -i 'mayfield/KHPX/*_V06' -o event.html\n"
    "  rxd report -i 'mayfield/KHPX/*_V06' -b watersheds.geojson --gauges gauges.csv "
    "--title 'Mayfield, 11 Dec 2021' -o event.html --record event.json\n"
    "  rxd report --from-record event.json -o event_again.html\n"
    "\n"
    "One self-contained HTML file (figures embedded, nothing fetched when you open it; print it to PDF\n"
    "from a browser): event totals, basin table and series, gauge validation, methods, the run record\n"
    "and the credits. Every number carries its unit and its class (RETRIEVED or ADJUSTED), never\n"
    "'observed'. --record FILE also writes the run record alone; --from-record FILE makes the report\n"
    "again from it (the radar files must still be at the recorded paths, or give -i)."
)


def register(command: Any, cli: Any) -> None:
    from engine.hydro import cli as hcli

    p = command(
        "report", "One HTML report of a rainfall event, reproducible from its run record.", EXAMPLES
    )
    cli._add_input(p, "radar volumes (several in time); not needed with --from-record")
    cli._add_output(p, "the report, .html (default report_<time>.html)")
    cli._add_param(p, "any QPE option KEY=VALUE (repeatable)")
    hcli._estimator_options(p)
    hcli._grid_time_options(p)
    hcli._gauge_options(p)
    hcli._basin_options(p, required=False)
    g = p.add_argument_group("report")
    g.add_argument(
        "--title", default=None, help="title of the report (default: Rainfall event report)"
    )
    g.add_argument("--bin-minutes", type=float, default=60.0, help="hyetograph bin in minutes (60)")
    g.add_argument("--record", metavar="FILE", help="also write the run record (JSON) to FILE")
    g.add_argument("--from-record", metavar="FILE", help="make the report again from a run record")
    g.add_argument("--no-figures", action="store_true", help="tables and text only")
    p.set_defaults(func=cmd_report, plot=None)


def cmd_report(args: argparse.Namespace, out: Any) -> int:
    import sys

    cl = sys.modules["engine.cli"]
    from engine import api
    from engine import provenance as prov
    from engine.hydro import cli as hcli
    from engine.hydro import report

    if args.from_record:
        rec = _read_record(args.from_record, cl)
        files = cl._inputs(args) if args.input else rec["inputs"]
        kw = rec["qpe"]
        title = args.title or rec["report"]["title"]
        bin_minutes = float(rec["report"]["bin_minutes"])
    else:
        files = cl._inputs(args)
        kw = hcli._collect(args, cl)
        title = args.title or "Rainfall event report"
        bin_minutes = float(args.bin_minutes)
    if len(args.output) > 1 or (args.output and Path(args.output[0]).suffix.lower() != ".html"):
        raise cl.UsageError("-o takes one .html file")
    res = api.qpe([str(f) for f in files], progress=out.progress, **kw)
    out.progress_done()
    record = report.make_record(files, kw, title, bin_minutes)
    model = report.build_model(
        res,
        title=title,
        record=record,
        figures=not args.no_figures,
        bin_minutes=bin_minutes,
    )
    html = report.render_html(model)
    target = args.output[0] if args.output else f"report_{cl._time_tag(prov.iso_utc(res.end))}.html"
    if not args.output:
        out.chose(f"{target} as the output (add -o to choose)", "-o", target)
    path = Path(target).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    out.wrote(path)
    if args.record:
        rp = Path(args.record).expanduser()
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        out.wrote(rp)
    if out.json:
        out.emit_json({"report": str(path), "record": record, "files": out.files})
    return cl.EXIT_OK


def _read_record(path: str, cl: Any) -> dict[str, Any]:
    try:
        rec = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise cl.UsageError(f"--from-record {path!r}: cannot read the run record: {exc}") from exc
    if not (isinstance(rec, dict) and rec.get("type") == "event_report" and "qpe" in rec):
        raise cl.UsageError(
            f"--from-record {path!r} is not a report run record (type event_report)"
        )
    return rec
