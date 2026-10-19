"""Figures of a multi-Doppler wind analysis (:mod:`engine.multidoppler`).

``plot_wind(record, level_km)`` draws one height of an analysis:

* ``kind="diagnostic"``: six panels - reflectivity, wind speed with vectors, beam-crossing angle,
  radars used (with range rings), vertical velocity and vertical vorticity;
* ``kind="publication"``: three clean panels - reflectivity with vectors, wind speed, vertical
  velocity (change them with ``panels=[...]``).

Style rules (so the figure reads on any background):

* the wind vectors contrast with the panel: **white** arrows on the dark theme, **dark** arrows
  on the light theme, and every arrow has an **edge** of the opposite colour (dark edge on white
  arrows, light edge on dark arrows), so they stay legible over any colour map as well;
* reflectivity uses the ``cmweather`` colour maps (``ChaseSpectral`` / ``NWSRef``);
* vorticity is lightly smoothed for display (``smooth_px``): a 1 km finite difference of radial
  velocities is dominated by gate-scale noise otherwise.

The input is a :class:`engine.multidoppler.WindRecord`, or the ``xarray.Dataset`` that
:func:`engine.multidoppler.to_dataset` / a saved NetCDF gives.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

from engine import multidoppler as mdop

THEMES: dict[str, dict[str, str]] = {
    "dark": {
        "bg": "#10151c",
        "panel": "#161c25",
        "fg": "#e8edf3",
        "muted": "#9aa7b6",
        "grid": "#2a3441",
        "arrow": "#ffffff",
        "arrow_edge": "#10151c",
        "radar": "#ffd24a",
    },
    "light": {
        "bg": "#ffffff",
        "panel": "#f6f8fa",
        "fg": "#1b232c",
        "muted": "#5d6b7a",
        "grid": "#d6dde5",
        "arrow": "#10151c",
        "arrow_edge": "#ffffff",
        "radar": "#b3261e",
    },
}
DIAGNOSTIC_PANELS = ("dbz", "speed", "crossing", "radars", "w", "vorticity")
PUBLICATION_PANELS = ("dbz_vectors", "speed_plain", "w")


def arrow_colors(theme: str) -> tuple[str, str]:
    """``(fill, edge)`` of the wind arrows for a theme: opposite colours, contrasting the panel."""
    t = THEMES[theme]
    return t["arrow"], t["arrow_edge"]


def _dataset(obj: Any) -> xr.Dataset:
    if isinstance(obj, mdop.WindRecord):
        return mdop.to_dataset(obj)
    if isinstance(obj, xr.Dataset):
        return obj
    if isinstance(obj, str | Path):
        return xr.open_dataset(obj)
    raise TypeError("plot_wind needs a WindRecord or an xarray.Dataset of a wind analysis")


def _smooth(a: np.ndarray, sigma: float) -> np.ndarray:
    """NaN-aware Gaussian smoothing (weights renormalised over the valid neighbours)."""
    if sigma <= 0:
        return a
    from scipy.ndimage import gaussian_filter

    ok = np.isfinite(a)
    num = gaussian_filter(np.where(ok, a, 0.0), sigma, mode="nearest")
    den = gaussian_filter(ok.astype(float), sigma, mode="nearest")
    with np.errstate(invalid="ignore", divide="ignore"):
        out = num / den
    return np.where(ok & (den > 0.3), out, np.nan)


def _radar_xy(ds: xr.Dataset) -> list[tuple[str, float, float]]:
    lat = np.atleast_1d(ds.attrs.get("radar_latitude", []))
    lon = np.atleast_1d(ds.attrs.get("radar_longitude", []))
    names = list(np.atleast_1d(ds.attrs.get("radar_names", [])))
    if not lat.size or lat.size != lon.size:
        return []
    proj = mdop._aeqd(float(ds.attrs["origin_latitude"]), float(ds.attrs["origin_longitude"]))
    out = []
    for i, (la, lo) in enumerate(zip(lat, lon, strict=True)):
        x, y = proj(float(lo), float(la))
        out.append((str(names[i]) if i < len(names) else f"R{i + 1}", x / 1e3, y / 1e3))
    return out


class _Level:
    """The 2-D fields of one height, with the plotting extent in km."""

    def __init__(self, ds: xr.Dataset, level_km: float, smooth_px: float):
        z = ds["z"].values.astype(float)
        self.k = int(np.argmin(np.abs(z - level_km * 1000.0)))
        self.z_km = float(z[self.k]) / 1e3
        self.x = ds["x"].values / 1e3
        self.y = ds["y"].values / 1e3
        dx = float(self.x[1] - self.x[0])
        self.extent = (
            float(self.x[0] - dx / 2),
            float(self.x[-1] + dx / 2),
            float(self.y[0] - dx / 2),
            float(self.y[-1] + dx / 2),
        )
        self.f = {
            n: ds[n].isel(z=self.k).values.astype(float) for n in ds.data_vars if "z" in ds[n].dims
        }
        self.smooth_px = smooth_px
        self.radars = _radar_xy(ds)
        self.names = [n for n, _, _ in self.radars]
        self.time = str(ds.attrs.get("analysis_time", "") or "")
        self.dz = float(z[1] - z[0]) / 1e3 if z.size > 1 else float("nan")


def _style_axes(ax, t: dict[str, str], lv: _Level, title: str) -> None:
    ax.set_facecolor(t["panel"])
    ax.set_title(title, color=t["fg"], fontsize=10, loc="left", pad=6)
    ax.set_xlim(lv.extent[0], lv.extent[1])
    ax.set_ylim(lv.extent[2], lv.extent[3])
    ax.set_aspect("equal")
    ax.tick_params(colors=t["muted"], labelsize=8, length=3)
    for sp in ax.spines.values():
        sp.set_color(t["grid"])
    ax.grid(True, color=t["grid"], lw=0.4, alpha=0.7)
    ax.set_xlabel("km east", color=t["muted"], fontsize=8)
    ax.set_ylabel("km north", color=t["muted"], fontsize=8)


def _colorbar(fig, ax, mappable, t: dict[str, str], label: str, **kw):
    cb = fig.colorbar(mappable, ax=ax, fraction=0.046, pad=0.02, **kw)
    cb.set_label(label, color=t["fg"], fontsize=8)
    cb.ax.tick_params(colors=t["muted"], labelsize=7)
    cb.outline.set_edgecolor(t["grid"])
    return cb


def _show(ax, lv: _Level, a: np.ndarray, **kw):
    return ax.imshow(
        np.ma.masked_invalid(a), origin="lower", extent=lv.extent, interpolation="nearest", **kw
    )


def _draw_radars(ax, lv: _Level, t: dict[str, str], rings_km: tuple[float, ...] = ()) -> None:
    import matplotlib.patheffects as pe

    for name, rx, ry in lv.radars:
        for r in rings_km:
            ax.add_patch(_circle(rx, ry, r, t["radar"], alpha=0.35 if r != rings_km[-1] else 0.6))
        ax.plot(
            rx,
            ry,
            marker="^",
            ms=7,
            mfc=t["radar"],
            mec=t["arrow_edge"],
            mew=0.8,
            ls="none",
            clip_on=True,
        )
        if lv.extent[0] <= rx <= lv.extent[1] and lv.extent[2] <= ry <= lv.extent[3]:
            ax.annotate(
                name,
                (rx, ry),
                xytext=(4, 5),
                textcoords="offset points",
                fontsize=8,
                color=t["fg"],
                path_effects=[pe.withStroke(linewidth=2, foreground=t["panel"])],
            )


def _circle(cx: float, cy: float, r: float, color: str, alpha: float):
    from matplotlib.patches import Circle

    return Circle((cx, cy), r, fill=False, ec=color, lw=0.7, ls=":", alpha=alpha)


def _vectors(ax, lv: _Level, theme: str, target: int = 24, ref: float | None = None) -> None:
    """Wind arrows: fill contrasting the panel, edge in the opposite colour, centred on the cell."""
    fill, edge = arrow_colors(theme)
    u, v = lv.f["u"], lv.f["v"]
    ny, nx = u.shape
    step = max(1, round(max(nx, ny) / target))
    xs, ys = np.meshgrid(lv.x, lv.y)
    sl = (slice(step // 2, None, step), slice(step // 2, None, step))
    uu, vv, xx, yy = u[sl], v[sl], xs[sl], ys[sl]
    ok = np.isfinite(uu) & np.isfinite(vv)
    if not ok.any():
        return
    spd = np.hypot(uu[ok], vv[ok])
    ref = ref or _nice(float(np.nanpercentile(spd, 90)))
    span = lv.extent[1] - lv.extent[0]
    # an arrow of `ref` m/s is about 1.4 arrow spacings long (km per m/s -> data units / arrow)
    scale = ref / (1.4 * step * (span / nx))
    q = ax.quiver(
        xx[ok], yy[ok], uu[ok], vv[ok],
        angles="xy", scale_units="xy", scale=scale, pivot="mid",
        width=0.0052, headwidth=3.4, headlength=3.8, headaxislength=3.3,
        facecolor=fill, edgecolors=edge, linewidths=0.5, zorder=5,
    )  # fmt: skip
    keyfmt = f"{ref:g} m/s"
    ax.quiverkey(
        q, 0.84, 1.045, ref, keyfmt, labelpos="E", coordinates="axes",
        color=fill, labelcolor=THEMES[theme]["fg"], fontproperties={"size": 8},
    )  # fmt: skip


def _nice(v: float) -> float:
    if not math.isfinite(v) or v <= 0:
        return 10.0
    for c in (5, 10, 15, 20, 25, 30, 40, 50, 60, 80):
        if v <= c:
            return float(c)
    return 100.0


def _outline_valid(ax, lv: _Level, t: dict[str, str]) -> None:
    valid = np.isfinite(lv.f["u"]).astype(float)
    if 0 < valid.sum() < valid.size:
        ax.contour(lv.x, lv.y, valid, levels=[0.5], colors=[t["muted"]], linewidths=0.6, zorder=4)


def _panel_dbz(fig, ax, lv, t, theme):
    from matplotlib import colormaps

    dbz = np.where(lv.f["dbz"] >= 0.0, lv.f["dbz"], np.nan)  # clear-air noise is not shown
    _show(ax, lv, dbz, cmap=colormaps["ChaseSpectral"], vmin=0, vmax=70)
    _colorbar(fig, ax, ax.images[-1], t, "reflectivity (dBZ)")
    _style_axes(ax, t, lv, "Reflectivity")
    _draw_radars(ax, lv, t)


def _panel_speed(fig, ax, lv, t, theme, vectors=True):
    s = lv.f["speed"]
    hi = float(np.nanpercentile(s, 99)) if np.isfinite(s).any() else 40.0
    _show(ax, lv, s, cmap="viridis", vmin=0, vmax=max(10.0, math.ceil(hi / 5) * 5))
    _colorbar(fig, ax, ax.images[-1], t, "horizontal wind speed (m/s)")
    _style_axes(ax, t, lv, "Wind speed and vectors" if vectors else "Horizontal wind speed")
    if vectors:
        _vectors(ax, lv, theme)
    _draw_radars(ax, lv, t)


def _panel_dbz_vectors(fig, ax, lv, t, theme):
    _panel_dbz(fig, ax, lv, t, theme)
    ax.set_title("Reflectivity and wind vectors", color=t["fg"], fontsize=10, loc="left", pad=6)
    _vectors(ax, lv, theme)


def _panel_crossing(fig, ax, lv, t, theme):
    _show(ax, lv, lv.f["crossing_angle"], cmap="cividis", vmin=0, vmax=90)
    _colorbar(fig, ax, ax.images[-1], t, "beam crossing angle (deg)")
    _style_axes(ax, t, lv, "Beam-crossing angle")
    _draw_radars(ax, lv, t)


def _panel_radars(fig, ax, lv, t, theme):
    from matplotlib.colors import BoundaryNorm, ListedColormap

    n = lv.f["n_radars"]
    top = int(np.nanmax(n)) if np.isfinite(n).any() else 2
    top = max(top, 2)
    cols = [
        t["panel"],
        "#5b6b7d",
        "#2f9e8f",
        "#e0b13a",
        "#d9643a",
        "#b04ea0",
        "#4e79c4",
        "#8fbf4a",
    ]
    cmap = ListedColormap(cols[: top + 1])
    _show(
        ax,
        lv,
        np.where(np.isfinite(n), n, 0.0),
        cmap=cmap,
        norm=BoundaryNorm(np.arange(-0.5, top + 1), top + 1),
    )
    cb = _colorbar(fig, ax, ax.images[-1], t, "radars seeing the cell", ticks=np.arange(0, top + 1))
    cb.ax.set_yticklabels([str(i) for i in range(top + 1)])
    _style_axes(ax, t, lv, "Radars used (rings: 50 km steps)")
    _draw_radars(ax, lv, t, rings_km=(50.0, 100.0, 150.0, 200.0))


def _panel_w(fig, ax, lv, t, theme):
    w = lv.f["w"]
    lim = float(np.nanpercentile(np.abs(w), 95)) if np.isfinite(w).any() else 5.0
    lim = max(2.0, math.ceil(lim))
    _show(ax, lv, w, cmap="RdBu_r", vmin=-lim, vmax=lim)
    _colorbar(fig, ax, ax.images[-1], t, "vertical velocity w (m/s, up positive)")
    _style_axes(ax, t, lv, "Vertical velocity")
    _outline_valid(ax, lv, t)
    _draw_radars(ax, lv, t)
    if not np.isfinite(w).any():
        ax.text(
            0.5, 0.5, "no vertical velocity at this height", transform=ax.transAxes,
            ha="center", va="center", color=t["muted"], fontsize=9,
        )  # fmt: skip


def _panel_vorticity(fig, ax, lv, t, theme):
    z = _smooth(lv.f["vorticity"], lv.smooth_px)
    lim = float(np.nanpercentile(np.abs(z), 98)) if np.isfinite(z).any() else 5.0
    lim = max(2.0, math.ceil(lim))
    _show(ax, lv, z, cmap="PuOr_r", vmin=-lim, vmax=lim)
    sm = f", smoothed {lv.smooth_px:g} px" if lv.smooth_px > 0 else ""
    _colorbar(fig, ax, ax.images[-1], t, "vertical vorticity (1e-3 1/s)")
    _style_axes(ax, t, lv, f"Vertical vorticity{sm}")
    _draw_radars(ax, lv, t)


_PANELS = {
    "dbz": _panel_dbz,
    "dbz_vectors": _panel_dbz_vectors,
    "speed": _panel_speed,
    "speed_plain": lambda fig, ax, lv, t, theme: _panel_speed(fig, ax, lv, t, theme, vectors=False),
    "crossing": _panel_crossing,
    "radars": _panel_radars,
    "w": _panel_w,
    "vorticity": _panel_vorticity,
}


def plot_wind(
    record_or_dataset: Any,
    level_km: float,
    out_path: str | Path | None = None,
    *,
    theme: str = "dark",
    kind: str = "diagnostic",
    panels: tuple[str, ...] | list[str] | None = None,
    smooth_px: float = 1.0,
    title: str | None = None,
    dpi: int = 100,
):
    """Figure of the wind analysis at ``level_km`` (nearest level). Returns the figure.

    ``theme`` "dark" or "light" sets the background and the arrow colours (white / dark with an
    edge of the opposite colour). ``kind`` "diagnostic" (6 panels, 2 x 3) or "publication"
    (3 panels in a row); ``panels`` picks any of ``dbz``, ``dbz_vectors``, ``speed`` (with vectors),
    ``speed_plain``, ``crossing``, ``radars``, ``w``, ``vorticity``. The figure is saved to
    ``out_path`` (PNG, SVG or PDF by the extension) when given.
    """
    import cmweather  # noqa: F401  (registers ChaseSpectral, NWSRef...)
    from matplotlib.figure import Figure

    if theme not in THEMES:
        raise ValueError("theme must be 'dark' or 'light'")
    if kind not in ("diagnostic", "publication"):
        raise ValueError("kind must be 'diagnostic' or 'publication'")
    ds = _dataset(record_or_dataset)
    lv = _Level(ds, level_km, smooth_px)
    names = list(panels or (DIAGNOSTIC_PANELS if kind == "diagnostic" else PUBLICATION_PANELS))
    for n in names:
        if n not in _PANELS:
            raise ValueError(f"unknown panel '{n}' (choose from {', '.join(_PANELS)})")
    t = THEMES[theme]
    ncol = 3 if kind == "diagnostic" or len(names) > 3 else len(names)
    nrow = math.ceil(len(names) / ncol)
    size = (5.4 * ncol, 4.9 * nrow + 0.5) if kind == "diagnostic" else (5.2 * ncol, 5.1)
    fig = Figure(figsize=size, facecolor=t["bg"])  # no pyplot: no global backend state
    axes = fig.subplots(nrow, ncol, squeeze=False)
    flat = list(axes.ravel())
    for ax, name in zip(flat, names, strict=False):
        _PANELS[name](fig, ax, lv, t, theme)
    for ax in flat[len(names) :]:
        ax.set_visible(False)
    head = title or (
        f"Multi-Doppler wind  |  z = {lv.z_km:g} km"
        + (f"  |  {', '.join(lv.names)}" if lv.names else "")
        + (f"  |  {lv.time}" if lv.time else "")
    )
    fig.suptitle(head, color=t["fg"], fontsize=12, x=0.01, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    if out_path is not None:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out, dpi=dpi, facecolor=fig.get_facecolor())
    return fig
