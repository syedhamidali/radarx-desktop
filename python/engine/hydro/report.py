"""The event report: one self-contained HTML document from a QPE result and what was computed on it.

``rxd report`` runs the rainfall estimate (and, when asked, the basins, gauge validation, the error
ensemble and
the flood indicators) and writes **one HTML file**: rainfall accumulation map, basin table, map and
series,
gauge validation, ensemble and indicator layers, the methods used, the run record, the provenance
and the
credits. Every number has its unit, its class (RETRIEVED / ADJUSTED / indicator) and, where the
engine has one,
its uncertainty. Nothing in it is typed by hand and nothing is downloaded.

Two layers, so the document can be tested and regenerated:

* :func:`build_model` turns a result into a plain dictionary (numbers, texts, figures as PNG bytes);
* :func:`render_html` turns that dictionary into HTML. It is deterministic: the same model gives the
  same
  bytes (the figures are embedded as data URIs; the creation time and the software versions are part
  of the
  model, so a golden-file test fixes them).

The run record (``record``) holds everything that is needed to make the same report again from the
same input
files: the options of the estimate and of every step, the seeds, the file paths. ``rxd report
--from-record`` reads it.
The report is HTML on purpose: it prints to PDF from any browser (the page has a print style); no
PDF is
written by the program.
"""

# ruff: noqa: E501
from __future__ import annotations

import base64
import html
import io
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from engine import provenance as prov

PRODUCT_CREDIT = "Radarx Desktop by LabShope Technologies"
LICENCE = "PolyForm Noncommercial License 1.0.0"
GUIDANCE_TEXT = (
    "Experimental guidance, not an official warning. Follow your official weather service "
    "(in the US, the National Weather Service)."
)

# short method references, one line each, for the methods the report names
REFERENCES = {
    "z": "Fulton, R. A., et al., 1998: The WSR-88D rainfall algorithm. Wea. Forecasting, 13, 377-395.",
    "hybrid": "Giangrande, S. E., and A. V. Ryzhkov, 2008: Estimation of rainfall based on the results of "
    "polarimetric echo classification. J. Appl. Meteor. Climatol., 47, 2445-2462.",
    "advection": "Gal-Chen, T., 1982: Errors in fixed and moving frame of references. J. Atmos. Sci., 39, "
    "2279-2300; Rinehart, R. E., and E. T. Garvey, 1978: Nature, 273, 287-289.",
    "gauges": "Smith, J. A., and W. F. Krajewski, 1991: Estimation of the mean field bias of radar rainfall "
    "estimates. J. Appl. Meteor., 30, 397-412; Gupta, H. V., et al., 2009: J. Hydrol., 377, 80-91.",
    "ensemble": "Germann, U., et al., 2009: REAL - Ensemble radar precipitation estimation for hydrology in a "
    "mountainous region. Q. J. R. Meteorol. Soc., 135, 445-456 (the idea of a log-domain error ensemble).",
    "flash": "Flash-flood lab report of Radarx Desktop (2026-10-22): 1-h rainfall relative to the cell's own "
    "climatology is the reference rule; a 36-predictor machine-learning model did not beat it.",
}


def esc(x: Any) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def num(v: Any, nd: int = 1, none: str = "not available") -> str:
    """A number with a fixed number of decimals, or ``none`` for a missing value (never NaN)."""
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return none
    return f"{v:,.{nd}f}"


def fig_png(fig: Any, dpi: int = 110) -> bytes:
    """PNG bytes of a matplotlib figure; the figure is closed."""
    import matplotlib.pyplot as plt

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# The run record
# ---------------------------------------------------------------------------

#: option names whose value is a file path
_PATH_KEYS = ("basins", "gauges", "dem", "frequency", "stations")


def _json_safe(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str))


def make_record(
    files: Any, qpe_options: dict[str, Any], title: str, bin_minutes: float
) -> dict[str, Any]:
    """Everything needed to make the same report again from the same files (paths as given).

    No time stamp: the record of the same run is the same bytes.
    """
    return {
        "record_version": 1,
        "type": "event_report",
        "software": prov.software_string(),
        "inputs": [str(f) for f in files],
        "qpe": _json_safe(qpe_options),
        "report": {"title": title, "bin_minutes": float(bin_minutes)},
    }


def redact_record(record: dict[str, Any]) -> dict[str, Any]:
    """The record for the document: file paths reduced to file names (the policy of the outputs)."""

    def name(v: Any) -> Any:
        return Path(v).name if isinstance(v, str) else v

    def walk(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: name(v) if k in _PATH_KEYS else walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [walk(v) for v in obj]
        return obj

    out = walk(record)
    out["inputs"] = [Path(f).name for f in record["inputs"]]
    return out


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


def build_model(
    result: Any,
    *,
    title: str = "Rainfall event report",
    record: dict[str, Any] | None = None,
    ensemble: Any = None,
    indicators: Any = None,
    figures: bool = True,
    bin_minutes: float = 60.0,
    exceed_threshold_mm: float | None = None,
) -> dict[str, Any]:
    """Everything the report shows, as plain data (figures as PNG bytes under ``figures``)."""
    from engine.hydro import products

    adjusted = result.accumulation_adjusted is not None
    st = result.stats()
    p = result.provenance(adjusted=adjusted)
    area = result.grid.cell_areas_km2()
    base = result.accumulation_adjusted if adjusted else result.accumulation
    fin = np.isfinite(base)
    model: dict[str, Any] = {
        "title": title,
        "created": p["created"],
        "software": p["software"],
        "label": "ADJUSTED" if adjusted else "RETRIEVED",
        "period": [prov.iso_utc(result.start), prov.iso_utc(result.end)],
        "duration_min": result.duration_s / 60.0,
        "inputs": p["inputs"],
        "radars": [
            {
                "name": r["name"],
                "latitude": r["latitude"],
                "longitude": r["longitude"],
                "scans": r["scans"],
                "first": r["first"],
                "last": r["last"],
            }
            for r in result.radars
        ],
        "method": str(p["method"]),
        "parameters": p["parameters"],
        "uncertainty": p["uncertainty"],
        "notes": list(result.notes),
        "grid": {
            "cell_m": result.grid.cell_m,
            "ny": result.grid.shape[0],
            "nx": result.grid.shape[1],
        },
        "stats": {
            "area_km2": st["area_km2"],
            "max_mm": float(np.nanmax(base)) if fin.any() else None,
            "mean_mm": float(np.average(base[fin], weights=area[fin])) if fin.any() else None,
            "wet_area_km2": st["wet_area_km2"],
            "max_rate_mm_h": st["max_rate_mm_h"],
            "mean_valid_fraction": st["mean_valid_fraction"],
            "uncertainty_median_rel": st["uncertainty_median_rel"],
        },
        "record": redact_record(record) if record else None,
        "guidance_text": GUIDANCE_TEXT,
        "figures": {},
        "references": [REFERENCES["z"], REFERENCES["advection"]],
    }
    if "hybrid" in model["method"].lower():
        model["references"].append(REFERENCES["hybrid"])
    bs = result.basin_series
    if bs is not None:
        model["basins"] = {
            "adjusted": bool(bs.adjusted),
            "rows": [
                {
                    k: s.get(k)
                    for k in (
                        "id",
                        "name",
                        "area_km2",
                        "coverage",
                        "accumulation_mean_mm",
                        "accumulation_adjusted_mean_mm",
                        "accumulation_max_mm",
                        "accumulation_sigma_mm",
                        "peak_mean_rate_mm_h",
                        "peak_mean_rate_time",
                        "max_cell_rate_mm_h",
                    )
                }
                for s in bs.summary()
            ],
            "thresholds": list(bs.thresholds),
            "areas": [
                {f"{t:g}": s.get(f"area_acc_ge_{t:g}mm_km2") for t in bs.thresholds}
                for s in bs.summary()
            ],
        }
        if result.exceedance:
            model["basins"]["exceedance"] = result.exceedance
    if result.validation:
        v = result.validation
        model["gauges"] = {
            "pairs": v["pairs"],
            "method": result.adjusted["method"] if result.adjusted else None,
            "before": v["validation_before"]["overall"],
            "after_in_sample": v["validation_after_in_sample"]["overall"],
            "cross_validation": (v.get("cross_validation") or {}).get("adjusted"),
            "window_mismatch_s": v.get("window_mismatch_s"),
        }
        model["references"].append(REFERENCES["gauges"])
    if ensemble is not None:
        model["ensemble"] = ensemble.summary()
        model["references"].append(REFERENCES["ensemble"])
    if indicators is not None:
        model["indicators"] = indicators.summary()
        model["references"].append(REFERENCES["flash"])
    if figures:
        model["figures"] = _figures(
            result, ensemble, indicators, bin_minutes, exceed_threshold_mm, products
        )
    return model


def _figures(
    result: Any,
    ensemble: Any,
    indicators: Any,
    bin_minutes: float,
    threshold: float | None,
    products: Any,
) -> dict[str, Any]:
    figs: dict[str, Any] = {}
    name = products.default_field(result)
    figs["accumulation"] = {
        "caption": f"{products.FIELDS[name][0]}, {prov.iso_utc(result.start)} to {prov.iso_utc(result.end)} "
        "(mm, liquid-water equivalent). An estimate, not an observation.",
        "png": fig_png(products.figure(result, name)),
    }
    if result.basin_series is not None:
        figs["basin_map"] = {
            "caption": "Each basin is filled with its area-weighted mean accumulation (mm); the "
            "background is the accumulation of the cells.",
            "png": fig_png(products.basin_map_figure(result)),
        }
        figs["basin_series"] = {
            "caption": f"Basin-mean rain rate (line), {bin_minutes:g}-minute mean intensity (bars) and "
            "cumulative basin-mean depth (right axis).",
            "png": fig_png(products.series_figure(result, bin_minutes)),
        }
    if ensemble is not None:
        names = sorted(k for k in ensemble.arrays if k.startswith("prob_ge_"))
        pick = threshold and f"prob_ge_{threshold:g}mm"
        field = pick if pick in names else (names[len(names) // 2] if names else None)
        if field:
            figs["ensemble"] = {
                "caption": f"{products.field_spec(field)[0]}. An ensemble under an assumed error "
                "model, not calibrated unless the reliability section says so.",
                "png": fig_png(products.figure(result, field)),
            }
    if indicators is not None:
        from engine.hydro import indicators as hi

        for key, lay in indicators.layers.items():
            figs[f"indicator_{key}"] = {
                "caption": f"{lay.label} ({indicators.input_class} indicator). {GUIDANCE_TEXT}",
                "png": fig_png(hi.figure(indicators, key)),
            }
    return figs


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

CSS = """
:root { color-scheme: light; --fg:#1d2733; --muted:#5b6876; --line:#d5dbe3; --bg:#fff;
--soft:#f4f6f9;
  --accent:#0b5cad; --warn:#8a4b00; --warnbg:#fff4e0; }
* { box-sizing: border-box; }
body { margin: 0; padding: 24px 16px 48px; background: var(--bg); color: var(--fg);
  font: 15px/1.5 -apple-system, "Segoe UI", "IBM Plex Sans", Roboto, Helvetica, Arial, sans-serif; }
main { max-width: 980px; margin: 0 auto; }
h1 { font-size: 26px; margin: 0 0 4px; }
h2 { font-size: 19px; margin: 32px 0 8px; padding-top: 12px; border-top: 1px solid var(--line); }
h3 { font-size: 15px; margin: 18px 0 6px; }
.sub { color: var(--muted); margin: 0 0 12px; }
.banner { background: var(--warnbg); border: 1px solid #e8c88f; color: var(--warn); border-radius:
6px;
  padding: 8px 12px; margin: 12px 0; font-size: 14px; }
.chip { display: inline-block; padding: 0 8px; border: 1px solid var(--accent); color:
var(--accent);
  border-radius: 10px; font-size: 12px; font-weight: 600; letter-spacing: .04em; vertical-align:
  middle; }
.chip.warn { border-color: var(--warn); color: var(--warn); }
table { border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 14px; }
th, td { border-bottom: 1px solid var(--line); padding: 4px 8px; text-align: left; vertical-align:
top; }
th { background: var(--soft); font-weight: 600; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; }
figure { margin: 12px 0; }
figure img { max-width: 100%; height: auto; border: 1px solid var(--line); }
figcaption { color: var(--muted); font-size: 13px; margin-top: 4px; }
pre { background: var(--soft); border: 1px solid var(--line); padding: 10px; overflow-x: auto; font-
size: 12px;
  white-space: pre-wrap; word-break: break-word; }
code { font-family: ui-monospace, "SF Mono", "IBM Plex Mono", Menlo, monospace; font-size: 13px; }
.small { font-size: 13px; color: var(--muted); }
ul.refs { padding-left: 18px; font-size: 13px; }
footer { margin-top: 36px; padding-top: 12px; border-top: 1px solid var(--line); color:
var(--muted); font-size: 13px; }
@media print { body { padding: 0; } h2 { break-after: avoid; } figure, table { break-inside: avoid;
}
  details > summary { display: none; } details > * { display: block; } }
"""


def _figure(model: dict[str, Any], key: str, alt: str) -> str:
    f = model["figures"].get(key)
    if not f:
        return ""
    b64 = base64.b64encode(f["png"]).decode("ascii")
    return (
        f'<figure><img alt="{esc(alt)}" src="data:image/png;base64,{b64}">'
        f"<figcaption>{esc(f['caption'])}</figcaption></figure>"
    )


def _table(head: list[tuple[str, bool]], rows: list[list[str]]) -> str:
    h = "".join(f'<th class="{"n" if n else ""}">{esc(t)}</th>' for t, n in head)
    body = "".join(
        "<tr>"
        + "".join(f'<td class="{"n" if head[i][1] else ""}">{c}</td>' for i, c in enumerate(r))
        + "</tr>"
        for r in rows
    )
    return f"<table><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table>"


def render_html(model: dict[str, Any]) -> str:
    """The report as one HTML string. Everything inserted is escaped; nothing is fetched at view
    time."""
    m = model
    out: list[str] = []
    a = out.append
    label = m["label"]
    a("<!doctype html>")
    a('<html lang="en"><head><meta charset="utf-8">')
    a('<meta name="viewport" content="width=device-width, initial-scale=1">')
    a(f"<title>{esc(m['title'])}</title><style>{CSS}</style></head><body><main>")
    a(f"<h1>{esc(m['title'])}</h1>")
    a(
        f'<p class="sub">{esc(m["period"][0])} to {esc(m["period"][1])} (UTC) &middot; '
        f"{num(m['duration_min'], 1)} min &middot; "
        f'<span class="chip">{esc(label)}</span> radar rainfall estimate</p>'
    )
    a(
        '<div class="banner"><strong>Estimates, not observations.</strong> Radar rainfall is a retrieval; '
        f"every number below is labelled with its class and, where the engine has one, its uncertainty. "
        f"{esc(m['guidance_text'])}</div>"
    )

    # -- summary ------------------------------------------------------------------------
    s = m["stats"]
    a("<h2>Event summary</h2>")
    rel = s["uncertainty_median_rel"]
    rows = [
        [
            "Largest accumulation in a cell",
            f"{num(s['max_mm'], 1)} mm",
            esc(label),
            "see the uncertainty row",
        ],
        [
            "Mean accumulation over the area with an estimate",
            f"{num(s['mean_mm'], 2)} mm",
            esc(label),
            f"area {num(s['area_km2'], 0)} km&sup2;",
        ],
        ["Area with at least 0.1 mm", f"{num(s['wet_area_km2'], 0)} km&sup2;", esc(label), ""],
        [
            "Largest rain rate",
            f"{num(s['max_rate_mm_h'], 0)} mm/h",
            esc(label),
            "one cell, one instant",
        ],
        [
            "Share of the period with data (mean over cells)",
            "not available"
            if s["mean_valid_fraction"] is None
            else f"{100 * s['mean_valid_fraction']:.0f} %",
            "",
            "time without data is not counted as zero",
        ],
        [
            "Median relative uncertainty (1 sigma, cells with at least 1 mm)",
            "not available" if rel is None else f"{100 * rel:.0f} %",
            "",
            "relation scatter and calibration; errors correlated in time",
        ],
    ]
    a(_table([("Quantity", False), ("Value", True), ("Class", False), ("Note", False)], rows))
    a(_figure(m, "accumulation", "Map of the rainfall accumulation"))
    rrows = [
        [
            esc(r["name"]),
            f"{r['latitude']:.3f}, {r['longitude']:.3f}",
            str(r["scans"]),
            esc((r["first"] or "")[:19]),
            esc((r["last"] or "")[:19]),
        ]
        for r in m["radars"]
    ]
    a(
        _table(
            [
                ("Radar", False),
                ("Latitude, longitude (deg)", False),
                ("Volumes", True),
                ("First (UTC)", False),
                ("Last (UTC)", False),
            ],
            rrows,
        )
    )

    # -- basins -------------------------------------------------------------------------
    b = m.get("basins")
    if b:
        a("<h2>Basins</h2>")
        bl = "ADJUSTED" if b["adjusted"] else "RETRIEVED"
        a(
            f"<p>Area-weighted rainfall over each basin polygon (exact cell fractions, true cell area). "
            f'<span class="chip">{bl}</span></p>'
        )
        rows = []
        for r in b["rows"]:
            mean = (
                r["accumulation_adjusted_mean_mm"]
                if b["adjusted"] and r.get("accumulation_adjusted_mean_mm") is not None
                else r["accumulation_mean_mm"]
            )
            rows.append(
                [
                    esc(r["id"]),
                    num(r["area_km2"], 0),
                    num(mean, 1),
                    num(r["accumulation_sigma_mm"], 1),
                    num(r["accumulation_max_mm"], 1),
                    num(r["peak_mean_rate_mm_h"], 1),
                    "not available" if r["coverage"] is None else f"{100 * r['coverage']:.0f} %",
                ]
            )
        a(
            _table(
                [
                    ("Basin", False),
                    ("Area (km²)", True),
                    ("Mean depth (mm)", True),
                    ("1 sigma (mm)", True),
                    ("Largest cell (mm)", True),
                    ("Peak mean rate (mm/h)", True),
                    ("Covered", True),
                ],
                rows,
            )
        )
        a(
            '<p class="small">1 sigma: the basin mean of the cell accumulation uncertainty, errors fully correlated '
            "in time (an upper bound for the sum); it contains the relation scatter and the calibration only. "
            "Covered: the share of the basin area with an estimate; the mean is over that part.</p>"
        )
        thr = b["thresholds"]
        if thr:
            rows = [
                [esc(r["id"])] + [num(b["areas"][i].get(f"{t:g}"), 0) for t in thr]
                for i, r in enumerate(b["rows"])
            ]
            a("<h3>Area at or above a depth (km&sup2;)</h3>")
            a(_table([("Basin", False)] + [(f">= {t:g} mm", True) for t in thr], rows))
        ex = b.get("exceedance")
        if ex:
            rows = []
            for e in ex:
                rp = e.get("return_period_years")
                flag = e.get("flag") or ""
                sign = "<" if flag.startswith("below") else ">" if flag.startswith("above") else ""
                rows.append(
                    [
                        esc(e["id"]),
                        f"{e['duration_h']:g}",
                        num(e["max_depth_mm"], 1),
                        "not available" if rp is None else esc(f"{sign}{rp:g}"),
                        esc(e.get("note") or flag),
                    ]
                )
            a("<h3>Return period of the largest basin-mean depth</h3>")
            a(
                _table(
                    [
                        ("Basin", False),
                        ("Duration (h)", True),
                        ("Depth (mm)", True),
                        ("Return period (years)", True),
                        ("Note", False),
                    ],
                    rows,
                )
            )
            a(
                '<p class="small">From a precipitation-frequency table supplied by the user (never downloaded); '
                "point depths compared with an area mean, see the note column for the areal reduction.</p>"
            )
        a(_figure(m, "basin_map", "Basins coloured by their mean accumulation"))
        a(_figure(m, "basin_series", "Basin rain rate and cumulative depth"))

    # -- gauges -------------------------------------------------------------------------
    g = m.get("gauges")
    if g:
        a("<h2>Validation against rain gauges</h2>")
        a(
            f"<p>{g['pairs']} gauges paired with the radar accumulation (3 &times; 3 cells). Adjustment: "
            f"{esc(g['method'])}. Gauges are point measurements with errors of their own.</p>"
        )

        def mrow(name: str, d: dict[str, Any] | None) -> list[str]:
            if not d:
                return [esc(name), "", "", "", "", ""]
            rb = d.get("relative_bias")
            return [
                esc(name),
                str(d.get("n", "")),
                "not available" if rb is None else f"{100 * rb:+.1f} %",
                num(d.get("rmse_mm"), 2),
                num(d.get("correlation"), 2),
                num(d.get("kge"), 2),
            ]

        a(
            _table(
                [
                    ("", False),
                    ("n", True),
                    ("Relative bias", True),
                    ("RMSE (mm)", True),
                    ("Correlation", True),
                    ("KGE", True),
                ],
                [
                    mrow("radar before adjustment", g["before"]),
                    mrow("adjusted, in sample", g["after_in_sample"]),
                    mrow("adjusted, leave-one-gauge-out", g["cross_validation"]),
                ],
            )
        )
        a(
            '<p class="small">The in-sample row is the fit, not a skill; the leave-one-gauge-out row is the honest one.</p>'
        )

    # -- ensemble -----------------------------------------------------------------------
    e = m.get("ensemble")
    if e:
        a("<h2>Probabilistic rainfall: ensemble of the estimate</h2>")
        a(f'<div class="banner">{esc(e["disclaimer"])}</div>')
        em = e["error_model"]
        a(
            f"<p>{e['n_members']} members, seed {e['seed']}; multiplicative lognormal error, sigma of ln = "
            f"{em['sigma_ln']:.3f}, correlation length {em['corr_length_km']:.0f} km, variance shares "
            f"{em['shares']['common']:.2f} common / {em['shares']['spatial']:.2f} spatial / "
            f"{em['shares']['cell_to_cell']:.2f} cell to cell.</p>"
        )
        rows = [[esc(k), esc(v)] for k, v in em["sources"].items()]
        a(_table([("Parameter", False), ("Where it came from", False)], rows))
        for n in em.get("notes", []):
            a(f'<p class="small">{esc(n)}</p>')
        a(_figure(m, "ensemble", "Ensemble spread and exceedance probability"))
        brows = e.get("basins") or []
        if brows:
            ths = e["thresholds_mm"]
            rows = []
            for r in brows:
                bm = r.get("basin_mean_mm")
                if not bm:
                    continue
                rows.append(
                    [
                        esc(r["id"]),
                        num(bm["mean"], 1),
                        num(bm.get("p05"), 1),
                        num(bm.get("p95"), 1),
                    ]
                    + [f"{100 * r['prob_basin_mean_ge'][f'{t:g}']:.0f} %" for t in ths]
                )
            a("<h3>Basin-mean depth over the members</h3>")
            a(
                _table(
                    [
                        ("Basin", False),
                        ("Mean (mm)", True),
                        ("5th pct (mm)", True),
                        ("95th pct (mm)", True),
                    ]
                    + [(f"P(>= {t:g} mm)", True) for t in ths],
                    rows,
                )
            )
        rel = e.get("reliability")
        a("<h3>Reliability</h3>")
        if not rel:
            a(
                "<p>No gauges were given: the probabilities are <strong>not validated</strong> against observations.</p>"
            )
        elif not rel.get("validated"):
            a(f"<p><strong>{esc(rel['note'])}</strong></p>")
        else:
            pool = rel["pooled"]
            a(
                f"<p>At {rel['n_gauges']} gauges: Brier skill {num(pool['brier_skill_score'], 2)} against the "
                f"climatological frequency, reliability slope {num(pool['reliability_slope'], 2)} "
                f"(1 is perfect; below 1 overconfident). {esc(rel['note'])}</p>"
            )

    # -- indicators ---------------------------------------------------------------------
    ind = m.get("indicators")
    if ind:
        a("<h2>Flash-flood indicators</h2>")
        a(
            f'<div class="banner"><strong>{esc(ind["label"])}.</strong> {esc(ind["product_text"])} {esc(ind["not_modelled"])}</div>'
        )
        for key, lay in ind["layers"].items():
            a(f'<h3>{esc(lay["label"])} <span class="chip warn">{esc(ind["label"])}</span></h3>')
            rows = [
                [esc(k), str(v["cells"]), num(v["area_km2"], 0)] for k, v in lay["classes"].items()
            ]
            a(_table([("Class", False), ("Cells", True), ("Area (km²)", True)], rows))
            pv = lay["provenance"]
            srcs = [pv.get("guidance"), pv.get("frequency"), pv.get("climatology")]
            for sdesc in srcs:
                if sdesc:
                    a(
                        f'<p class="small">Input: {esc(sdesc.get("file"))}; vintage: {esc(sdesc.get("vintage", "not given"))}; '
                        f"units in file: {esc(sdesc.get('units_in_file', 'n/a'))}.</p>"
                    )
            if pv.get("lab_result"):
                a(f'<p class="small">{esc(pv["lab_result"])}</p>')
            a(_figure(m, f"indicator_{key}", lay["label"]))
        for n in ind["notes"]:
            a(f'<p class="small">{esc(n)}</p>')

    # -- methods ------------------------------------------------------------------------
    a("<h2>Methods</h2>")
    a(f"<p>{esc(m['method'])}</p>")
    a(
        f"<p>Grid: {m['grid']['nx']} &times; {m['grid']['ny']} cells of {m['grid']['cell_m']:g} m "
        "(azimuthal-equidistant, true cell area). Rain rates (mm/h) are integrated over time with "
        "advection-corrected interpolation between scans; a gap longer than the limit is not bridged, and time "
        "without data is not counted as zero. Reflectivity in dBZ is never added or averaged as a "
        "decibel quantity.</p>"
    )
    a(f'<p class="small">Uncertainty model: {esc(m["uncertainty"]["model"])}</p>')
    for n in m["notes"]:
        a(f'<p class="small">Note: {esc(n)}</p>')
    a(
        '<h3>References</h3><ul class="refs">'
        + "".join(f"<li>{esc(r)}</li>" for r in m["references"])
        + "</ul>"
    )

    # -- record -------------------------------------------------------------------------
    a("<h2>Run record and provenance</h2>")
    a(f"<p>Software: {esc(m['software'])}. Made {esc(m['created'])}.</p>")
    a("<p>Input files: " + ", ".join(f"<code>{esc(x)}</code>" for x in m["inputs"]) + "</p>")
    if m.get("record"):
        a(
            "<details open><summary>Run record (JSON, file paths reduced to names): the report is "
            "made again from the record file with <code>rxd report --from-record</code> "
            "(<code>--record</code> writes it with the paths)</summary><pre>"
            + esc(json.dumps(m["record"], indent=2, sort_keys=True))
            + "</pre></details>"
        )
    a(
        f"<footer>{esc(PRODUCT_CREDIT)}. Licence of the program: {esc(LICENCE)}. Built on radarx, xradar, xarray, "
        "NumPy and matplotlib (their licences are in the third-party notices of the program). "
        "Basin polygons, gauges and frequency tables are the user's own data and stay under their own licences.</footer>"
    )
    a("</main></body></html>")
    return "\n".join(out) + "\n"
