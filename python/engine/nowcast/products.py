"""Outputs of a nowcast: a CF NetCDF with provenance, a JSON record, and a figure.

The NetCDF layout is the interface a hydrological model (flash-flood routing) should read:

* dimensions ``lead`` (minutes after the origin), ``y``, ``x`` (metres on the azimuthal-
  equidistant grid; 2-D ``lat``/``lon`` and a ``crs`` grid mapping), ``member`` when an ensemble
  was kept;
* ``rain_rate`` (``lead, y, x``), mm/h, the deterministic member; ``rain_rate_member``
  (``member, lead, y, x``) when kept, member 0 = unperturbed; ``rain_rate_mean`` and
  ``rain_rate_spread`` (ensemble mean and standard deviation);
* ``prob_rate_ge_<t>`` (``lead, y, x``) in 0..1 for every rain-rate threshold;
* ``reflectivity`` / ``prob_dbz_ge_<t>`` when the input was reflectivity;
* ``position_sigma`` (``lead``) km: the position uncertainty implied by the verified motion error;
* ``motion_u`` / ``motion_v`` (``y, x``) m/s: the motion field used;
* global attributes: the ``data_class = forecast`` provenance, the method, lead, skill
  statement, the guidance sentence, every parameter and the seed.

NaN means "not forecastable" (the departure point was not observed), never zero rain.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from engine import provenance as prov

if TYPE_CHECKING:
    from engine.nowcast.run import Nowcast


def _thr_name(prefix: str, t: float) -> str:
    return f"{prefix}_{t:g}".replace(".", "p").replace("-", "m")


def to_dataset(nc: Nowcast) -> Any:
    import xarray as xr

    fr = nc.frames
    ds = xr.Dataset(coords={"lead": nc.leads_min, "y": fr.y, "x": fr.x})
    ds["lead"].attrs = {
        "long_name": "lead time after the nowcast origin",
        "units": "minutes",
        "comment": "forecast valid time = origin_time + lead",
    }
    ds["y"].attrs = {"standard_name": "projection_y_coordinate", "units": "m", "axis": "Y"}
    ds["x"].attrs = {"standard_name": "projection_x_coordinate", "units": "m", "axis": "X"}
    ds = ds.assign_coords(
        valid_time=("lead", nc.valid_times.astype("datetime64[ns]")),
    )
    ds["valid_time"].attrs = {"long_name": "forecast valid time (UTC)"}
    if fr.origin is not None:
        import pyproj

        try:
            tr = pyproj.Transformer.from_crs(
                fr.proj4
                or f"+proj=aeqd +lat_0={fr.origin[0]} +lon_0={fr.origin[1]} +datum=WGS84 +units=m",
                "EPSG:4326",
                always_xy=True,
            )
            xx, yy = np.meshgrid(fr.x, fr.y)
            lon, lat = tr.transform(xx, yy)
            ds["lat"] = (
                ("y", "x"),
                lat.astype(np.float32),
                {"standard_name": "latitude", "units": "degrees_north"},
            )
            ds["lon"] = (
                ("y", "x"),
                lon.astype(np.float32),
                {"standard_name": "longitude", "units": "degrees_east"},
            )
            ds = ds.set_coords(["lat", "lon"])
            crs = pyproj.CRS.from_proj4(fr.proj4) if fr.proj4 else None
            ds["crs"] = xr.DataArray(
                np.int32(0),
                attrs={
                    "grid_mapping_name": "azimuthal_equidistant",
                    "latitude_of_projection_origin": float(fr.origin[0]),
                    "longitude_of_projection_origin": float(fr.origin[1]),
                    "false_easting": 0.0,
                    "false_northing": 0.0,
                    "semi_major_axis": 6378137.0,
                    "inverse_flattening": 298.257223563,
                    "proj4": fr.proj4,
                    **({"crs_wkt": crs.to_wkt()} if crs is not None else {}),
                },
            )
        except Exception:  # a grid without a usable projection: x/y stay
            pass
    layer = nc.provenance()
    base = {
        "provenance_type": "FORECAST",
        "guidance": layer.get("uncertainty", ""),
        "grid_mapping": "crs" if "crs" in ds else "",
    }
    base = {k: v for k, v in base.items() if v}
    ds["rain_rate"] = (
        ("lead", "y", "x"),
        nc.rain_rate,
        {
            "long_name": "nowcast rain rate, deterministic member",
            "standard_name": "lwe_precipitation_rate",
            "units": "mm h-1",
            "comment": "NaN = not forecastable (the air came from outside the observed area)",
            **base,
        },
    )
    if nc.field == "reflectivity":
        ds["reflectivity"] = (
            ("lead", "y", "x"),
            nc.deterministic,
            {
                "long_name": "nowcast composite reflectivity, deterministic member",
                "units": "dBZ",
                **base,
            },
        )
        for t, p in nc.probability.items():
            ds[_thr_name("prob_dbz_ge", t)] = (
                ("lead", "y", "x"),
                p,
                {
                    "long_name": f"probability of reflectivity >= {t:g} dBZ",
                    "units": "1",
                    "threshold": float(t),
                    "threshold_units": "dBZ",
                    **base,
                },
            )
    for t, p in nc.rate_probability.items():
        ds[_thr_name("prob_rate_ge", t)] = (
            ("lead", "y", "x"),
            p,
            {
                "long_name": f"probability of rain rate >= {t:g} mm/h",
                "units": "1",
                "threshold": float(t),
                "threshold_units": "mm h-1",
                **base,
            },
        )
    if nc.ensemble_mean_rate is not None:
        ds["rain_rate_mean"] = (
            ("lead", "y", "x"),
            nc.ensemble_mean_rate,
            {"long_name": "ensemble mean rain rate", "units": "mm h-1", **base},
        )
        ds["rain_rate_spread"] = (
            ("lead", "y", "x"),
            nc.spread_rate,
            {
                "long_name": "ensemble standard deviation of the rain rate",
                "units": "mm h-1",
                **base,
            },
        )
    if nc.members_rate is not None:
        ds = ds.assign_coords(member=np.arange(nc.members_rate.shape[0]))
        ds["member"].attrs = {"long_name": "ensemble member (0 = unperturbed motion)"}
        ds["rain_rate_member"] = (
            ("member", "lead", "y", "x"),
            nc.members_rate,
            {"long_name": "nowcast rain rate per member", "units": "mm h-1", **base},
        )
    ds["position_sigma"] = (
        ("lead",),
        nc.position_sigma_km.astype(np.float32),
        {"long_name": "1-sigma position uncertainty from the verified motion error", "units": "km"},
    )
    ds["motion_u"] = (
        ("y", "x"),
        nc.motion.u,
        {
            "long_name": "motion of the echo pattern, eastward",
            "units": "m s-1",
            "provenance_type": "RETRIEVED",
        },
    )
    ds["motion_v"] = (
        ("y", "x"),
        nc.motion.v,
        {
            "long_name": "motion of the echo pattern, northward",
            "units": "m s-1",
            "provenance_type": "RETRIEVED",
        },
    )
    ds["covered"] = (
        ("y", "x"),
        fr.coverage.astype(np.int8),
        {"long_name": "1 where a radar observed the cell", "units": "1"},
    )
    rec = nc.record()
    ds.attrs = prov.standard_global_attrs(
        title="Radar nowcast (advection extrapolation)",
        source_files=fr.sources,
        action="nowcast",
        params=nc.config,
        conventions="CF-1.10",
        summary=f"{nc.config.get('lead_min'):g} min nowcast in {nc.config.get('step_min'):g} "
        f"min steps from the frame at {prov.iso_utc(nc.origin_time)}",
        comment=layer.get("uncertainty", ""),
        references="; ".join(layer.get("references", [])),
        layer=layer,
        extra={
            "origin_time": prov.iso_utc(nc.origin_time),
            "nowcast_method": rec["method"],
            "nowcast_skill": rec["skill"],
            "nowcast_guidance": rec["guidance"],
            "nowcast_seed": int(nc.seed),
            "nowcast_record": json.dumps(_jsonable(rec), ensure_ascii=False),
        },
    )
    return ds


def _jsonable(obj: Any) -> Any:
    from engine.api_results import jsonable

    return jsonable(obj)


def save(nc: Nowcast, out: Any) -> list[Path]:
    """Write by extension: ``.nc`` (CF NetCDF), ``.json`` (the record), ``.png/.pdf/.svg``
    (figure); a folder gets ``nowcast_<origin>.nc``, ``.json`` and ``.png``."""
    from engine.api_results import save_figure

    targets = [out] if isinstance(out, (str, os.PathLike)) else list(out)
    written: list[Path] = []
    for t in targets:
        p = Path(t).expanduser()
        if p.suffix == "" or p.is_dir():
            p.mkdir(parents=True, exist_ok=True)
            stem = "nowcast_" + prov.iso_utc(nc.origin_time).replace("-", "").replace(":", "")[:13]
            written += save(nc, [p / f"{stem}.nc", p / f"{stem}.json", p / f"{stem}.png"])
            continue
        ext = p.suffix.lower()
        p.parent.mkdir(parents=True, exist_ok=True)
        if ext == ".nc":
            ds = to_dataset(nc)
            enc = {v: {"zlib": True, "complevel": 4} for v in ds.data_vars if ds[v].ndim >= 2}
            ds.to_netcdf(p, encoding=enc)
            written.append(p)
        elif ext == ".json":
            p.write_text(
                json.dumps(_jsonable(nc.record()), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            written.append(p)
        elif ext in (".png", ".pdf", ".svg"):
            written.append(save_figure(figure(nc), p))
        else:
            raise ValueError(f"{p.name}: use .nc, .json, .png, .pdf or .svg (or a folder)")
    return written


# ---------------------------------------------------------------------------
# Figure
# ---------------------------------------------------------------------------

#: colour-blind-safe sequential map for reflectivity / rain rate and for probabilities
CMAP_FIELD = "viridis"
CMAP_PROB = "magma"


def figure(
    nc: Nowcast, *, leads: list[float] | None = None, threshold: float | None = None, dpi: int = 150
) -> Any:
    """The latest frame, the deterministic nowcast at a few leads and the probability of
    exceeding a threshold, all labelled FORECAST with the guidance sentence."""
    import matplotlib

    matplotlib.use("Agg", force=False)
    import matplotlib.pyplot as plt

    fr = nc.frames
    picks = (
        leads
        or [ld for ld in (15.0, 30.0, 60.0) if ld <= nc.leads_min[-1]]
        or [float(nc.leads_min[-1])]
    )
    thr = (
        threshold
        if threshold is not None
        else (nc.thresholds[1] if len(nc.thresholds) > 1 else nc.thresholds[0])
    )
    ext = [
        (fr.x[0] - fr.cell_m / 2) / 1e3,
        (fr.x[-1] + fr.cell_m / 2) / 1e3,
        (fr.y[0] - fr.cell_m / 2) / 1e3,
        (fr.y[-1] + fr.cell_m / 2) / 1e3,
    ]
    n = 1 + len(picks)
    fig, axes = plt.subplots(2, n, figsize=(3.3 * n + 0.8, 6.9), constrained_layout=True)
    fig.get_layout_engine().set(rect=(0, 0.05, 1, 0.96))  # adjusted to the footer below
    axes = np.atleast_2d(axes)
    is_refl = nc.field == "reflectivity"
    origin = nc.config["origin_index"]
    # Three states in every panel: a value (colour scale), observed-but-dry (below the scale,
    # near white) and not observed / not forecastable (NaN, grey). The scale starts at 5 dBZ
    # (0.1 mm/h): below it is "dry" in both the observed and the forecast panels.
    if is_refl:
        lo, hi = 5.0, 65.0
    else:
        top = fr.data[origin]
        hi = float(np.nanpercentile(top, 99.5)) if np.isfinite(top).any() else 10.0
        lo, hi = 0.1, max(1.0, hi)
    cm = plt.get_cmap(CMAP_FIELD).copy()
    cm.set_bad("#d4d8dd")
    cm.set_under("#f6f7f8")
    cmp = plt.get_cmap(CMAP_PROB).copy()
    cmp.set_bad("#d4d8dd")
    unit = nc.units
    latest = fr.data[origin].copy()
    latest[np.isnan(latest) & fr.coverage] = lo - 1.0  # observed, no echo: "dry"
    latest[~fr.coverage] = np.nan

    def draw(ax: Any, arr: np.ndarray, title: str, cmap: Any, vmin: float, vmax: float) -> Any:
        im = ax.imshow(
            np.ma.masked_invalid(arr),
            origin="lower",
            extent=ext,
            cmap=cmap,
            vmin=vmin,
            vmax=vmax,
            interpolation="nearest",
        )
        ax.set_title(title, fontsize=9, loc="left")
        ax.set_aspect("equal")
        ax.tick_params(labelsize=7)
        return im

    hhmm = prov.iso_utc(nc.origin_time)[11:16]
    im0 = draw(axes[0, 0], latest, f"Observed {hhmm} UTC  [RETRIEVED grid]", cm, lo, hi)
    for j, ld in enumerate(picks, start=1):
        k = nc.lead_index(ld)
        valid = prov.iso_utc(nc.valid_times[k])[11:16]
        im0 = draw(
            axes[0, j],
            nc.deterministic[k],
            f"Nowcast +{ld:g} min, {valid} UTC  [FORECAST]",
            cm,
            lo,
            hi,
        )
    cb = fig.colorbar(im0, ax=axes[0, :].tolist(), shrink=0.85, pad=0.01)
    cb.set_label(("Composite reflectivity" if is_refl else "Rain rate") + f" [{unit}]", fontsize=8)
    # motion quiver on the observed panel
    ax = axes[0, 0]
    st = max(1, round(max(fr.shape) / 12))
    xx, yy = np.meshgrid(fr.x[::st] / 1e3, fr.y[::st] / 1e3)
    ax.quiver(
        xx,
        yy,
        nc.motion.u[::st, ::st],
        nc.motion.v[::st, ::st],
        color="white",
        scale=400,
        width=0.004,
        alpha=0.9,
    )
    spd, drc = nc.motion.speed_direction()
    ax.text(
        0.02,
        0.02,
        f"motion {spd:.1f} m/s towards {drc:.0f} deg ({nc.motion.method})",
        transform=ax.transAxes,
        fontsize=7,
        color="white",
        bbox={"facecolor": "black", "alpha": 0.5, "pad": 2},
    )
    # second row: the spread (or the motion speed) and the probability of >= thr
    pr = nc.probability.get(thr)
    if nc.spread_rate is not None:
        k_last = len(nc.leads_min) - 1
        sp = nc.spread_rate[k_last]
        top = float(np.nanpercentile(sp, 99.5)) if np.isfinite(sp).any() else 1.0
        ims = draw(
            axes[1, 0],
            sp,
            f"Ensemble spread +{nc.leads_min[k_last]:g} min  [FORECAST]",
            cm,
            0.0,
            max(top, 0.1),
        )
        cbs = fig.colorbar(ims, ax=[axes[1, 0]], shrink=0.85, pad=0.01)
        cbs.set_label("rain-rate standard deviation [mm h-1]", fontsize=8)
    else:
        ims = draw(
            axes[1, 0],
            np.hypot(nc.motion.u, nc.motion.v),
            "Motion speed  [RETRIEVED]",
            cm,
            0.0,
            max(float(np.nanmax(np.hypot(nc.motion.u, nc.motion.v))), 1.0),
        )
        cbs = fig.colorbar(ims, ax=[axes[1, 0]], shrink=0.85, pad=0.01)
        cbs.set_label("speed [m s-1]", fontsize=8)
    imp = None
    for j, ld in enumerate(picks, start=1):
        k = nc.lead_index(ld)
        if pr is None:
            axes[1, j].axis("off")
            continue
        imp = draw(
            axes[1, j], pr[k], f"P(>= {thr:g} {unit}) +{ld:g} min  [FORECAST]", cmp, 0.0, 1.0
        )
    if imp is not None:
        cbp = fig.colorbar(imp, ax=axes[1, 1:].tolist(), shrink=0.85, pad=0.01)
        cbp.set_label("probability [1]", fontsize=8)
    for ax in axes.ravel():
        if ax.axison:
            ax.set_xlabel("km east", fontsize=7)
            ax.set_ylabel("km north", fontsize=7)
    members = (
        f"{nc.n_members} members (motion perturbations)"
        if nc.n_members
        else "single member (neighbourhood probability)"
    )
    footer = (
        f"Method: Lucas-Kanade motion of the last {nc.config.get('n_frames')} frames, backward "
        "semi-Lagrangian extrapolation, Lagrangian persistence (no growth, decay or initiation). "
        f"{members}; position sigma at +{nc.leads_min[-1]:g} min: "
        f"{nc.position_sigma_km[-1]:.1f} km. {nc.skill_text} "
        "FORECAST: experimental guidance, not an official forecast."
    )
    import textwrap

    wrapped = textwrap.fill(footer, max(100, int(fig.get_figwidth() * 20)))
    n_lines = wrapped.count("\n") + 1
    # room for the footer (about 0.11 inch per line of 6.5 pt) and the title
    fig.get_layout_engine().set(rect=(0, 0.012 + 0.11 * n_lines / fig.get_figheight(), 1, 0.96))
    fig.text(0.01, 0.005, wrapped, fontsize=6.5, color="#444", va="bottom")
    fig.suptitle(
        f"Radar nowcast from {prov.iso_utc(nc.origin_time)}  |  {fr.cell_m / 1e3:g} km grid  |  "
        "FORECAST: experimental guidance, not an official forecast",
        fontsize=9,
        x=0.01,
        ha="left",
    )
    return fig
