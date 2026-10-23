"""``rxd env``: environment data (model, reanalysis, radar products) from the command line.

Registered by :func:`engine.cli.build_parser`. Light to import (argparse only); the engine and
the providers are imported by the handlers.

Nothing is downloaded without an explicit yes. ``fetch``, ``show`` and ``profile`` print the
estimate first (the servers by name, the number of files and the exact bytes, the cache folder,
the credit and the licence) and then either ask (on a terminal), or need ``--yes`` (scripts).
``--yes`` approves exactly the estimate that was printed.
"""

from __future__ import annotations

import argparse
import sys
import urllib.parse
from pathlib import Path
from typing import Any

ACTIONS = ("providers", "inventory", "estimate", "fetch", "show", "profile", "cache")

EXAMPLES = (
    "examples:\n"
    "  rxd env providers                                  the sources, their class and licence\n"
    "  rxd env inventory hrrr                             what HRRR offers\n"
    "  rxd env estimate hrrr --time 2021-12-11T03:00Z -V cape -V srh_0_3km\n"
    "  rxd env fetch hrrr --time 2021-12-11T03:00Z --bbox=-92.5,34,-85,39 -V @storm_environment\n"
    "  rxd env show hrrr --time 2021-12-11T03:00Z --bbox=-92.5,34,-85,39 -V cape -o cape.nc\n"
    "  rxd env profile hrrr --time 2021-12-11T03:00Z --lat 36.74 --lon -88.64\n"
    "  rxd env cache                                      folder, size, limit\n"
    "  rxd env cache --max-mb 4096 --offline              set the cache limit; no network\n"
    "\n"
    "estimate shows what a download would cost and downloads nothing (a small index or listing\n"
    "may be read to know the exact size). fetch, show and profile ask before downloading; in a\n"
    "script add --yes to approve exactly the estimate that was printed. Cached data is reused\n"
    "with no question and no network; --offline (or XRADAR_OFFLINE=1) never touches the network.\n"
    "The cache folder, its limit and the offline switch are shared with the app\n"
    "(docs/ENVIRONMENT.md)."
)


def register(command: Any, cli: Any) -> None:
    p = command(
        "env",
        "Environment data: model analyses, reanalysis, radar products (downloads need consent).",
        EXAMPLES,
    )
    p.add_argument("action", choices=ACTIONS, metavar="ACTION", help="|".join(ACTIONS))
    p.add_argument("provider", nargs="?", metavar="PROVIDER", help="source id (rxd env providers)")
    p.add_argument("--product", metavar="NAME", help="the provider's product (see inventory)")
    p.add_argument("--time", metavar="UTC", help="the time wanted, e.g. 2021-12-11T03:00Z")
    p.add_argument("--time-end", metavar="UTC", help="end of a time window")
    p.add_argument(
        "--bbox",
        metavar="W,S,E,N",
        help="area in degrees; with a negative west write --bbox=W,S,E,N",
    )
    p.add_argument(
        "-V",
        "--variable",
        action="append",
        default=[],
        metavar="NAME",
        help="variable (repeatable)",
    )
    cli._add_param(p, "provider option KEY=VALUE (repeatable), e.g. -P fxx=2 -P levels_step_hpa=25")
    cli._add_output(p, "show: .nc or .json; profile: .json or .csv")
    p.add_argument("--lat", type=float, metavar="DEG", help="profile: latitude")
    p.add_argument("--lon", type=float, metavar="DEG", help="profile: longitude")
    p.add_argument("--yes", "-y", action="store_true", help="approve the printed estimate")
    g = p.add_argument_group("cache and network (shared with the app)")
    g.add_argument("--dir", metavar="FOLDER", help="cache: set the cache folder")
    g.add_argument("--max-mb", type=int, metavar="MB", help="cache: set the size limit in MB")
    g.add_argument("--offline", action="store_true", help="cache: never use the network")
    g.add_argument("--online", action="store_true", help="cache: allow the network again")
    g.add_argument("--clear", action="store_true", help="cache: remove everything in the cache")
    p.set_defaults(func=cmd_env)


# -- text -------------------------------------------------------------------------------------


def human_bytes(n: float | None) -> str:
    if n is None:
        return "unknown size"
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000.0
    return f"{n:.1f} TB"  # pragma: no cover


def hosts_of(est: Any) -> list[str]:
    seen: list[str] = []
    for f in est.files:
        host = urllib.parse.urlparse(f.url or "").netloc
        if host and host not in seen:
            seen.append(host)
    return seen


def estimate_lines(est: Any) -> list[str]:
    """The consent text: the same facts as the app's dialog."""
    hosts = hosts_of(est)
    need = est.network_files
    exact = "" if est.size_exact else "at least "
    lines = [
        f"{est.provider_name}  [{_kind(est)}]",
        f"  From      {', '.join(hosts) if hosts else est.source}",
        f"  Download  {len(need)} file(s), {exact}{human_bytes(est.network_bytes)}"
        + ("" if est.size_exact else f" (a limit of {human_bytes(est.max_network_bytes)} applies)"),
    ]
    if len(need) != len(est.files):
        lines.append(
            f"  Cached    {len(est.files) - len(need)} of {len(est.files)} file(s) already in the "
            f"cache ({human_bytes(est.cached_bytes)}), not downloaded again"
        )
    lines += [
        f"  Saved to  {est.destination}",
        f"  Credit    {est.attribution}",
        f"  Licence   {est.licence}",
    ]
    lines += [f"  Note      {n}" for n in est.notes]
    if not est.can_fetch:
        lines.append(f"  Cannot    {est.reason}")
    return lines


def _kind(est: Any) -> str:
    from engine.environment import Environment

    try:
        return Environment.default().provider(est.provider_id).kind.upper()
    except Exception:
        return "?"


# -- the command ------------------------------------------------------------------------------


def _request(args: argparse.Namespace, cli: Any) -> Any:
    from engine.environment import Request
    from engine.environment.errors import EnvironmentDataError

    if not args.provider:
        raise cli.UsageError("name the source: rxd env providers lists them")
    variables: list[str] = []
    for item in args.variable:
        variables += [v.strip() for v in item.split(",") if v.strip()]
    bbox = tuple(cli._numbers(args.bbox, 4, "--bbox")) if args.bbox else None
    params = {k: cli._json_or_text(v) for k, v in cli._split_kv(args.param, "-P").items()}
    try:
        return Request(
            provider=args.provider,
            product=args.product or "",
            bbox=bbox,  # type: ignore[arg-type]
            time_start=args.time,
            time_end=args.time_end,
            variables=tuple(variables),
            params=params,
        )
    except EnvironmentDataError as exc:
        raise cli.UsageError(str(exc)) from exc


def cmd_env(args: argparse.Namespace, out: Any) -> int:
    from engine import cli
    from engine.api_results import XrdArgumentError, XrdDependencyError, XrdInputError
    from engine.environment import Environment
    from engine.environment.errors import EnvironmentDataError, OfflineError
    from engine.environment.gribio import GribUnavailable

    env = Environment.default()
    try:
        if args.action == "providers":
            return _providers(env, out)
        if args.action == "cache":
            return _cache(env, args, out)
        if args.action == "inventory":
            return _inventory(env, args, out, cli)
        request = _request(args, cli)
        if args.action == "estimate":
            est = env.estimate(request)
            return _print_estimate(est, out)
        return _download_and_use(env, request, args, out, cli)
    except GribUnavailable as exc:
        raise XrdDependencyError(str(exc)) from exc
    except NotImplementedError as exc:  # "<source> does not serve gridded fields / profiles"
        raise XrdArgumentError(str(exc)) from exc
    except OfflineError as exc:
        raise XrdInputError(str(exc)) from exc
    except EnvironmentDataError as exc:
        raise XrdArgumentError(str(exc)) from exc


def _providers(env: Any, out: Any) -> int:
    rows = [p.describe() for p in env.registry.all()]
    if out.json:
        out.emit_json({"providers": rows})
        return 0
    from engine import cli

    out.result(
        cli._table(
            ("id", "class", "name", "licence"),
            [(r["id"], r["kind"], r["name"], r["licence"].split(".")[0][:60]) for r in rows],
        )
    )
    return 0


def _inventory(env: Any, args: argparse.Namespace, out: Any, cli: Any) -> int:
    if not args.provider:
        raise cli.UsageError("name the source: rxd env providers lists them")
    provider = env.provider(args.provider)
    items = [i.to_dict() for i in provider.inventory()]
    if out.json:
        out.emit_json({"provider": provider.describe(), "inventory": items})
        return 0
    out.result(f"{provider.name}  [{provider.kind}]\n  {provider.description}\n")
    for it in items:
        out.result(f"  product {it['product']}: {it['title']}")
        for key in ("description", "resolution", "coverage", "cadence"):
            if it.get(key):
                out.result(f"    {key:<11}{it[key]}")
        if it["variables"]:
            out.result(f"    variables  {', '.join(it['variables'])}")
    return 0


def _print_estimate(est: Any, out: Any) -> int:
    if out.json:
        out.emit_json({"estimate": est.to_dict()})
    else:
        for line in estimate_lines(est):
            out.result(line)
    return 0 if est.can_fetch else 1


def _approve(est: Any, args: argparse.Namespace, out: Any, cli: Any) -> Any:
    """The explicit yes. Returns an Approval, or None when nothing needs downloading."""
    if not est.requires_approval:
        return None
    if not est.can_fetch:
        raise cli.UsageError(est.reason or "this request cannot be fetched")
    if not (out.json or out.quiet):
        for line in estimate_lines(est):
            print(line, file=sys.stderr)
    if args.yes:
        return est.approve()
    if sys.stdin.isatty() and sys.stderr.isatty() and not out.json:
        exact = "" if est.size_exact else "at least "
        answer = input(
            f"Download {exact}{human_bytes(est.network_bytes)} from "
            f"{', '.join(hosts_of(est)) or est.source}? [y/N] "
        )
        if answer.strip().lower() in ("y", "yes"):
            return est.approve()
        raise cli.UsageError("Nothing was downloaded (you did not approve).")
    raise cli.UsageError(
        f"Nothing was downloaded: this needs {exact_text(est)} from "
        f"{', '.join(hosts_of(est)) or est.source}. Run again with --yes to approve exactly this "
        "estimate (use 'rxd env estimate' to see it first)."
    )


def exact_text(est: Any) -> str:
    return ("" if est.size_exact else "at least ") + human_bytes(est.network_bytes)


def _download_and_use(env: Any, request: Any, args: argparse.Namespace, out: Any, cli: Any) -> int:
    est = env.estimate(request)
    approval = _approve(est, args, out, cli)
    result = env.fetch(
        request, approval, progress=lambda p: out.progress(p["percent"], p["message"])
    )
    out.progress_done()
    provider = env.provider(request.provider)
    if args.action == "fetch":
        return _report_fetch(result, est, out)
    if args.action == "show":
        return _show(provider, result, env, args, out, cli)
    return _profile(provider, result, env, args, out, cli)


def _report_fetch(result: Any, est: Any, out: Any) -> int:
    if out.json:
        out.emit_json({"result": result.to_dict()})
        return 0
    got = sum(1 for c in result.from_cache if not c)
    out.result(
        f"{len(result.paths)} file(s) ready ({got} downloaded, "
        f"{human_bytes(result.bytes_downloaded)}; "
        f"{len(result.paths) - got} from the cache) in {est.destination}"
    )
    out.result(f"  {result.provenance.get('title', '')}  [{result.provenance.get('kind', '')}]")
    return 0


def _show(
    provider: Any, result: Any, env: Any, args: argparse.Namespace, out: Any, cli: Any
) -> int:
    import numpy as np

    ds = provider.open(result, env)
    rows = []
    for name, var in ds.data_vars.items():
        a = np.asarray(var.values, dtype=float)
        ok = np.isfinite(a)
        rows.append(
            (
                name,
                var.attrs.get("units", ""),
                "x".join(map(str, var.shape)),
                f"{np.nanmin(a):.4g}" if ok.any() else "n/a",
                f"{np.nanmax(a):.4g}" if ok.any() else "n/a",
                f"{np.nanmean(a):.4g}" if ok.any() else "n/a",
            )
        )
    for target in args.output:
        suffix = Path(target).suffix.lower()
        if suffix == ".nc":
            ds.to_netcdf(target)
        elif suffix == ".json":
            Path(target).write_text(cli.dumps({"attrs": dict(ds.attrs), "variables": [
                {"name": r[0], "units": r[1], "shape": r[2], "min": r[3], "max": r[4], "mean": r[5]}
                for r in rows]}) + "\n", encoding="utf-8")  # fmt: skip
        else:
            raise cli.UsageError(f"-o for show must end in .nc or .json, not {suffix or target!r}")
        out.wrote(target)
    if out.json:
        out.emit_json(
            {"attrs": dict(ds.attrs), "variables": [list(r) for r in rows], "files": out.files}
        )
        return 0
    out.result(f"{ds.attrs.get('title', '')}  [{ds.attrs.get('data_class', '')}]")
    out.result(f"  {ds.attrs.get('time_selection', '')}")
    out.result(f"  {ds.attrs.get('data_resolution', '')}")
    out.result(cli._table(("variable", "units", "shape", "min", "max", "mean"), rows))
    out.result(f"  Credit: {ds.attrs.get('data_attribution', '')}")
    out.result(f"  {ds.attrs.get('data_class_definition', '')}")
    return 0


def _profile(
    provider: Any, result: Any, env: Any, args: argparse.Namespace, out: Any, cli: Any
) -> int:
    from engine import sounding
    from engine.api_results import XrdDependencyError

    if args.lat is None or args.lon is None:
        raise cli.UsageError("profile needs --lat and --lon")
    snd = provider.profile_at(result, args.lat, args.lon, env)
    try:
        res = sounding.analyse(snd)
    except sounding.RadarxMissing as exc:
        raise XrdDependencyError(str(exc)) from exc
    for target in args.output:
        suffix = Path(target).suffix.lower()
        if suffix == ".json":
            Path(target).write_text(cli.dumps(res) + "\n", encoding="utf-8")
        elif suffix == ".csv":
            lv = snd.levels(None)
            keys = ("pressure_hpa", "height_msl_m", "temperature_c", "dewpoint_c", "u_ms", "v_ms")
            lines = [f"# {k}: {v}" for k, v in _csv_header(res).items()] + [",".join(keys)]
            for row in zip(*(lv[k] for k in keys), strict=True):
                lines.append(",".join("" if x is None else f"{x:g}" for x in row))
            Path(target).write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            raise cli.UsageError(
                f"-o for profile must end in .json or .csv, not {suffix or target!r}"
            )
        out.wrote(target)
    if out.json:
        payload = dict(res)
        payload["levels"] = {"n_levels": res["levels"]["n_levels"]}
        payload["files"] = out.files
        out.emit_json(payload)
        return 0
    out.result(f"{snd.station}  ({snd.fmt}, {res['levels']['n_levels']} levels)")
    out.result(f"data class: {res['provenance']['kind']}; {res['drift_note']}")
    out.result("")
    for line in sounding.summary_lines(res["diagnostics"]):
        out.result(line)
    for note in snd.notes:
        out.result(f"note: {note}")
    return 0


def _csv_header(res: dict[str, Any]) -> dict[str, str]:
    from engine import layer_provenance

    return layer_provenance.attrs(res["provenance"])


def _cache(env: Any, args: argparse.Namespace, out: Any) -> int:
    changes: dict[str, Any] = {}
    if args.dir:
        changes["cache_dir"] = args.dir
    if args.max_mb:
        changes["cache_max_mb"] = args.max_mb
    if args.offline and args.online:
        from engine import cli

        raise cli.UsageError("--offline and --online together make no sense")
    if args.offline:
        changes["offline"] = True
    if args.online:
        changes["offline"] = False
    if changes:
        env.apply_settings(changes)
    freed = env.cache.clear() if args.clear else None
    status = env.status()
    if out.json:
        out.emit_json({**status, "freed_bytes": freed})
        return 0
    c, s = status["cache"], status["settings"]
    out.result(f"cache folder  {c['root']}")
    out.result(
        f"in use        {human_bytes(c['bytes'])} in {c['files']} file(s) of "
        f"{human_bytes(c['max_bytes'])}"
    )
    out.result(f"offline mode  {'on' if s['effective_offline'] else 'off'}")
    if freed is not None:
        out.result(f"removed       {human_bytes(freed)}")
    return 0
