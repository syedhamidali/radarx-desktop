"""Vertical profile instruments at the RaDAPS site (OBSERVED): ``profiles/mrr_<site>.npz`` and ``profiles/rwp_<site>.npz``.

* **MRR** (Micro Rain Radar, 24 GHz, vertically pointing, processed with IMProToo by UAH): Doppler spectra
  based reflectivity, fall velocity, rain rate, LWC, drop size distribution per range gate. Heights are above
  the radar. The MRR sees only rain / melting layer and attenuates in heavy rain (PIA is delivered).
* **RWP** (915 MHz radar wind profiler, 6 beams, 5 min consensus winds): u, v, w, SNR, qc tag.

Not used: a Doppler wind lidar (no lidar files exist in the folder; ``ITAP/.../NSSL_Lidar.ipynb`` is only a notebook).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

UTC = timezone.utc


def _mask_time(t_abs: np.ndarray, start: datetime, end: datetime, margin_s: float) -> np.ndarray:
    return (t_abs >= start.timestamp() - margin_s) & (t_abs <= end.timestamp() + margin_s)


def read_mrr(
    files: list[str | Path], start: datetime, end: datetime, margin_s: float = 300.0
) -> dict[str, np.ndarray] | None:
    import netCDF4

    parts: dict[str, list[np.ndarray]] = {}
    for f in files:
        with netCDF4.Dataset(f) as ds:
            t = np.asarray(ds["time"][:], float)
            m = _mask_time(t, start, end, margin_s)
            if not m.any():
                continue

            def g(name, ds=ds, m=m):
                return np.ma.filled(np.ma.asarray(ds[name][m]).astype(float), np.nan)

            parts.setdefault("t_abs", []).append(t[m])
            parts.setdefault("height_m", []).append(g("MRR rangegate"))
            for k, name in (
                ("dbz", "MRR_Capital_Z"),
                ("dbz_attenuated", "MRR_Small_z"),
                ("fall_speed_ms", "MRR_W"),
                ("rain_rate_mmh", "MRR_RR"),
                ("lwc_gm3", "MRR_LWC"),
                ("pia_db", "MRR_PIA"),
            ):
                parts.setdefault(k, []).append(g(name))
    if not parts:
        return None
    out = {k: np.concatenate(v, axis=0) for k, v in parts.items()}
    out["t_s"] = out.pop("t_abs") - start.timestamp()
    out["height_m"] = np.nanmedian(out["height_m"], axis=0)
    return {
        k: v.astype(np.float32) if v.dtype.kind == "f" and k != "t_s" else v for k, v in out.items()
    }


def read_rwp(
    files: list[str | Path], start: datetime, end: datetime, margin_s: float = 900.0
) -> dict[str, Any] | None:
    import netCDF4

    parts: dict[str, list[np.ndarray]] = {}
    meta: dict[str, Any] = {}
    for f in files:
        with netCDF4.Dataset(f) as ds:
            t = np.asarray(ds["epochTime"][:], float)
            m = _mask_time(t, start, end, margin_s)
            if not m.any():
                continue
            meta = {
                "lat": float(np.ravel(ds["latitude"][:])[0]),
                "lon": float(np.ravel(ds["longitude"][:])[0]),
                "file": Path(f).name,
            }
            parts.setdefault("t_abs", []).append(t[m])
            parts.setdefault("height_m", []).append(np.asarray(ds["height"][:], float))
            for k in ("u", "v", "w", "qcTag"):
                parts.setdefault(k, []).append(
                    np.ma.filled(np.ma.asarray(ds[k][m]).astype(float), np.nan)
                )
            snr = np.stack(
                [
                    np.ma.filled(np.ma.asarray(ds[f"SNR_{i}"][m]).astype(float), np.nan)
                    for i in range(1, 7)
                ]
            )
            parts.setdefault("snr_db", []).append(np.nanmean(snr, axis=0))
    if not parts:
        return None
    out = {k: np.concatenate(v, axis=0) for k, v in parts.items() if k not in ("height_m",)}
    out["t_s"] = out.pop("t_abs") - start.timestamp()
    out["height_m"] = parts["height_m"][0]
    out["speed_ms"] = np.hypot(out["u"], out["v"])
    out["dir_deg"] = (np.degrees(np.arctan2(-out["u"], -out["v"])) + 360.0) % 360.0
    out["meta"] = meta
    return out
