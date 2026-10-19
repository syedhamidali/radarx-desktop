"""Rain, wind (with the gust-front pressure rumble) and hail, driven by the surface observations.

Physically grounded: the rain rate, the drop-size spectrum (number flux per size), Dm, the wind speed /
direction / gust, the pressure jump, the hail diameters / counts, the station distances.
Artistic: the timbre of every impact, the curve constants. All of it is synthesised from the data,
not a recording.
"""

from __future__ import annotations

import numpy as np
from scipy.ndimage import uniform_filter1d
from scipy.signal import butter, lfilter, sosfilt

from .dsd import dm_from_fall_speed, fall_speed_ms
from .dsp import (
    banded_stereo,
    convolve_events,
    ctl_to_audio,
    damped_sine,
    impulse_train,
    local_rms_normalise,
    pan_gains,
    sum_kernels,
)
from .geo import rel_azimuth_deg
from .layers import (
    rain_distance_weight,
    rain_dm_offset_db,
    rain_gain,
    wind_cutoff_hz,
    wind_gain,
)
from .scene import CTL_RATE, DROP_CLASS_MM, Scene

RADAR_SWITCH_M = 8000.0  # a rain gauge / disdrometer nearer than this is the rain at the listener; farther, the radar is
MRR_REACH_M = 3000.0  # an MRR / lidar site only drives the rain character when the listener is over it


def _db(x):
    return float(20 * np.log10(max(x, 1e-9)))


# --------------------------------------------------------------------------- rain


def _rain_kernels(sr: int, rng, variants: int = 2):
    """Impact kernels per drop class: a damped band of noise whose centre frequency falls and whose
    ring lengthens with the drop size, plus a hard click for large drops (sharper, brighter onset).
    Amplitude ~ mass^(2/3) ~ D^2."""
    ks = []
    for D in DROP_CLASS_MM:
        f0 = float(np.clip(6500.0 / D**0.7, 1100.0, 9000.0))
        tau = 0.0008 + 0.0014 * D
        L = int(8 * tau * sr)
        t = np.arange(L) / sr
        sos = butter(2, [f0 / 1.6, min(f0 * 1.6, 0.45 * sr)], btype="band", fs=sr, output="sos")
        vs = []
        for _ in range(variants):
            k = sosfilt(sos, rng.standard_normal(L)) * np.exp(-t / tau)
            click = np.zeros(L)
            click[:3] = np.array([1.0, -1.0, 0.4]) * min(D / 4.0, 1.0) * 2.0 * np.std(k) * 3
            k = k + click
            vs.append(k / np.sqrt(np.sum(k**2)) * D**2)
        ks.append(vs)
    return ks


def _fallback_shares(dm: np.ndarray) -> np.ndarray:
    """Number-flux share per drop class of an exponential spectrum with median volume diameter Dm
    (Lambda = 3.67 / Dm), flux ~ N(D) v(D) dD (dD ~ D for the log-spaced classes)."""
    lam = 3.67 / np.maximum(dm, 0.3)
    f = np.exp(-np.outer(DROP_CLASS_MM, lam)) * (fall_speed_ms(DROP_CLASS_MM) * DROP_CLASS_MM)[:, None]
    return f / f.sum(axis=0, keepdims=True)


def rain_plan(scene: Scene):
    """The data side of the rain layer: everything the sound needs per control step, and where it
    came from. Returns a dict (arrays at the control rate)."""
    n = scene.n
    R, d_r, ok_r = scene.blend("rain_rate_mmh")
    dsd = scene.dsd_state()
    dm_st, _, ok_dm = scene.blend("dm_mm")
    mrr_dm, d_mrr, ok_mrr_dm = scene.blend("mrr_dm_mm")
    mrr_vt, d_mrr_v, ok_mrr_vt = scene.blend("mrr_fall_speed_ms")
    mrr_r, _, ok_mrr_r = scene.blend("mrr_rain_rate_mmh")
    mrr_near = (ok_mrr_dm & (d_mrr < MRR_REACH_M)) | (ok_mrr_vt & (d_mrr_v < MRR_REACH_M))
    mrr_dm_eff = np.where(ok_mrr_dm, mrr_dm, dm_from_fall_speed(mrr_vt))

    use_dsd = dsd["ok"] if dsd is not None else np.zeros(n, bool)
    ra = scene.radar_at()
    near_station = ok_r & (d_r < RADAR_SWITCH_M)
    use_radar = (ra["ok"] & ~near_station) if ra is not None else np.zeros(n, bool)
    R_eff = np.where(ok_r, np.maximum(R, 0.0), 0.0)
    rate_src = np.where(ok_r, "station", "none").astype(object)
    if ra is not None:
        # no instrument under the listener: the radar echo over the listener (Z-R estimate, 0 = no echo)
        R_eff = np.where(use_radar, ra["rain_mmh"], R_eff)
        rate_src = np.where(use_radar, "radar_zr", rate_src)
        use_dsd = use_dsd & ~use_radar
    if dsd is not None:
        fill = ~ok_r & use_dsd
        R_eff = np.where(fill, dsd["rr"], R_eff)
        rate_src = np.where(fill, "dsd", rate_src)
    fill = (R_eff <= 0) & ~ok_r & ok_mrr_r & mrr_near
    R_eff = np.where(fill, np.maximum(mrr_r, 0.0), R_eff)
    rate_src = np.where(fill, "mrr", rate_src)
    covered = (R_eff > 0) | ok_r | use_dsd | use_radar

    dm_assumed = np.clip(0.9 * np.maximum(R_eff, 0.01) ** 0.21, 0.5, 3.0)
    dm = np.where(use_dsd, dsd["dm"] if dsd is not None else 0.0,
                  np.where(mrr_near, mrr_dm_eff, np.where(ok_dm & (dm_st > 0), dm_st, dm_assumed)))
    dm_src = np.where(use_dsd, "dsd", np.where(mrr_near, "mrr", np.where(ok_dm & (dm_st > 0), "station", "assumed")))
    shares = _fallback_shares(dm)
    if dsd is not None:
        tot = dsd["flux"].sum(axis=0)
        ok = use_dsd & (tot > 0)
        shares = np.where(ok[None, :], dsd["flux"] / np.maximum(tot, 1e-12)[None, :], shares)
    d = np.where(ok_r, d_r, np.inf)
    if dsd is not None:
        d = np.where(~ok_r & use_dsd, dsd["dist"], d)
    d = np.where(use_radar, 0.0, d)  # the radar sees the rain over the listener
    wd = np.where(covered & np.isfinite(d), rain_distance_weight(np.where(np.isfinite(d), d, 1e9)), 0.0)
    lam_ctl = np.where(use_dsd, 4.0 * np.minimum(dsd["F"], 5000.0) ** 0.7 if dsd is not None else 0.0,
                       60.0 * np.maximum(R_eff, 0.0) ** 0.6)
    lam_ctl = np.minimum(lam_ctl, 3000.0) * wd**2 * (R_eff > 0)
    return {"R": R_eff, "dm": dm, "dm_src": dm_src, "rate_src": rate_src, "shares": shares, "wd": wd,
            "lam": lam_ctl, "covered": covered, "dsd_used": use_dsd, "mrr_used": mrr_near, "dist": d, "radar_used": use_radar}


def _mostly(mask: np.ndarray, scene: Scene, clip_span) -> bool:
    i0, i1 = int(clip_span[0] * CTL_RATE), int(clip_span[1] * CTL_RATE) + 1
    return bool(mask[i0:i1].mean() > 0.5) if i1 > i0 else False


def render_rain(scene: Scene, sr: int, n: int, seed: int):
    rng = np.random.default_rng(seed)
    p = rain_plan(scene)
    R, dm, wd = p["R"], p["dm"], p["wd"]
    level = rain_gain(R) * 10 ** (rain_dm_offset_db(dm) / 20.0) * wd
    lam = ctl_to_audio(p["lam"], n, sr) / sr
    idx, amps = impulse_train(rng, n, lam, 0.5)
    cum = np.cumsum(p["shares"], axis=0)
    ci = np.clip((idx / sr * CTL_RATE).astype(int), 0, cum.shape[1] - 1)
    u = rng.random(len(idx))
    cls = (u[None, :] > cum[:, ci]).sum(axis=0).clip(0, len(DROP_CLASS_MM) - 1)
    ear = rng.random(len(idx)) < 0.5  # each impact is heard mostly at one ear
    var = rng.integers(0, 2, len(idx))
    kernels = _rain_kernels(sr, rng)
    patter = np.zeros((2, n))
    for c in range(len(DROP_CLASS_MM)):
        for v in range(2):
            for ch in (0, 1):
                sel = (cls == c) & (var == v) & (ear == (ch == 0))
                if sel.any():
                    patter[ch] += convolve_events(n, idx[sel], amps[sel], kernels[c][v])
    hb, ha = butter(1, 2500.0, btype="high", fs=sr)
    hiss = lfilter(hb, ha, rng.standard_normal((2, n)), axis=1)
    small = p["shares"][:2].sum(axis=0)  # drops < 1 mm
    pw = np.clip(1.1 - 1.4 * small, 0.1, 0.9) * wd**2
    pw_a = ctl_to_audio(pw, n, sr)
    lv_a = ctl_to_audio(level, n, sr)
    out = np.zeros((2, n))
    for ch in (0, 1):
        pn = local_rms_normalise(patter[ch], sr, window_s=3.0, rel_floor=0.15)
        hn = local_rms_normalise(hiss[ch], sr, floor=1e-2)
        pn = 3.0 * np.tanh(pn / 3.0)  # the rare big drop stays fat but does not spike
        out[ch] = (np.sqrt(pw_a) * pn + np.sqrt(1.0 - pw_a) * hn) * lv_a
    cov = p["covered"]
    segs = []
    w, _ = scene.station_weights("rain_rate_mmh")
    segs += scene.dominant_segments(w, scene.bundle.stations, ["rain_rate_mmh"])
    dsd = scene.dsd_state()
    dsd_segs = []
    if dsd is not None:
        dsd_segs = scene.dominant_segments(dsd["weights"], dsd["stations"], ["drop-size spectrum"])
    info = {
        "rain_rate_mmh": {"min": float(R.min()), "mean": float(R.mean()), "max": float(R.max())},
        "dm_mm": {"mean": float(dm.mean()), "max": float(dm.max())},
        "sources": {
            "rain_rate": {k: float((p["rate_src"] == k).mean()) for k in ("station", "dsd", "mrr", "radar_zr", "none")},
            "dm": {k: float((p["dm_src"] == k).mean()) for k in ("dsd", "mrr", "station", "assumed")},
            "assumed_dm_fraction": float((p["dm_src"] == "assumed").mean()),
        },
        "drop_spectrum_fraction": float(p["dsd_used"].mean()),
        "mrr_fraction": float(p["mrr_used"].mean()),
        "coverage": float(cov.mean()),
        "mean_station_distance_m": float(np.mean(p["dist"][cov & np.isfinite(p["dist"])])) if (cov & np.isfinite(p["dist"])).any() else None,
        "level_db_premaster": {"min": _db(level.min()), "max": _db(level.max())},
        "drivers": [s for s in segs if not _mostly(p["radar_used"], scene, s["clip_s"])],
        "radar_drivers": scene.runs(p["radar_used"] & (R > 0), "radar volumes: lowest echo over the listener (Z = 300 R^1.4)"),
        "radar_fraction": float(p["radar_used"].mean()),
        "drop_spectrum_drivers": dsd_segs,
        "note": "impacts: rate ~ number flux per drop size, amplitude ~ mass^(2/3); hiss from small drops",
    }
    return out, info


# --------------------------------------------------------------------------- wind


def _station_ti(scene: Scene):
    """Turbulence intensity of the synthetic gust modulation. From the gust column when a station has
    one (gust factor), else std / mean of the observed speed over +-2 samples. A series sampled
    faster than ~30 s already carries its gusts: the synthetic part is scaled down to a quarter at
    <= 7 s sampling. Blended like the speed."""
    w, _ = scene.station_weights("wind_speed_ms")
    rows = []
    for s in scene.bundle.stations:
        if "wind_speed_ms" not in s.series:
            rows.append(np.full(scene.n, np.nan))
            continue
        t, v = s.series["wind_speed_ms"]
        m = np.isfinite(v)
        t, v = t[m], v[m]
        if len(t) < 3:
            rows.append(np.full(scene.n, 0.15))
            continue
        gf = None
        gk = next((k for k in ("wind_gust_ms", "gust_ms") if k in s.series), None)
        if gk is not None:
            gt, gv = s.series[gk]
            gm = np.isfinite(gv)
            if gm.any():
                gf = np.interp(t, gt[gm], gv[gm]) / np.maximum(v, 0.5)
        if gf is not None:
            ti = np.clip((gf - 1.0) / 2.5, 0.05, 0.5)
        else:
            k = 2
            ti = np.array([
                np.std(v[max(0, i - k): i + k + 1]) / max(np.mean(v[max(0, i - k): i + k + 1]), 1.0)
                for i in range(len(v))
            ])
            ti = np.clip(ti, 0.05, 0.5)
        resolved = float(np.clip(np.median(np.diff(t)) / 30.0, 0.25, 1.0))
        rows.append(np.interp(scene.tw, t, ti) * resolved)
    ti = np.array(rows) if rows else np.zeros((0, scene.n))
    sw = np.maximum(w.sum(axis=0), 1e-12)
    return np.clip((w * np.nan_to_num(ti, nan=0.15)).sum(axis=0) / sw, 0.02, 0.45)


def render_wind(scene: Scene, sr: int, n: int, seed: int, gust_hz: float = 0.8):
    rng = np.random.default_rng(seed)
    v, d, ok = scene.blend("wind_speed_ms")
    v = np.where(ok, np.maximum(v, 0.0), 0.0)
    # observed high-rate series are used directly (smoothed over 0.2 s of clip time)
    v = uniform_filter1d(v, 20, mode="nearest")
    ti = _station_ti(scene)
    gk = "wind_gust_ms" if any("wind_gust_ms" in s.series for s in scene.bundle.stations) else "gust_ms"
    gust, _, okg = scene.blend(gk)
    sos = butter(2, gust_hz, fs=CTL_RATE, output="sos")
    g = sosfilt(sos, rng.standard_normal(scene.n + 400))[400:]
    g = g / max(g.std(), 1e-9)
    if okg.any():
        # the observed gust is the top of the excursions: g = +2.2 reaches it, the mean stays v
        veff = np.where(okg & (gust > v), v + (gust - v) * np.clip(g, -1.0, 2.2) / 2.2, v * (1.0 + ti * g))
    else:
        veff = v * (1.0 + ti * g)
    veff = np.maximum(veff, 0.0)
    wd = np.where(ok, np.maximum(1.0 / (1.0 + (np.where(np.isfinite(d), d, 1e9) / 20000.0) ** 2), 0.5), 0.0)
    level = wind_gain(veff) * wd
    dirn, okd = scene.blend_wind_vector()
    rel = rel_azimuth_deg(dirn, scene.heading)  # wind FROM this side
    out = banded_stereo(n, sr, level, wind_cutoff_hz(veff), seed + 1, rho=0.5, pan_ctl=np.where(okd, rel, 0.0))
    # the pressure wave of a gust front: a low rumble, only where the station's pressure trace jumps
    jump, p_events = scene.pressure_jump()
    if (jump > 0).any():
        out += banded_stereo(n, sr, jump * 10 ** (-38.0 / 20.0), np.full(scene.n, 90.0), seed + 2, rho=0.8,
                             centers=(31.5, 63.0, 125.0))
    w, _ = scene.station_weights("wind_speed_ms")
    med_dt = [float(np.median(np.diff(s.series["wind_speed_ms"][0]))) for s in scene.bundle.stations
              if "wind_speed_ms" in s.series and len(s.series["wind_speed_ms"][0]) > 1]
    info = {
        "wind_speed_ms": {"min": float(v.min()), "mean": float(v.mean()), "max": float(v.max())},
        "effective_speed_ms_max": float(veff.max()),
        "turbulence_intensity_synthetic_mean": float(ti.mean()),
        "observed_gust_used": bool(okg.any()),
        "station_sampling_s_median": float(np.median(med_dt)) if med_dt else None,
        "coverage": float(ok.mean()),
        "level_db_premaster": {"min": _db(level.min()), "max": _db(level.max())},
        "gust_fronts": scene.gust_fronts(),
        "pressure_jumps": p_events,
        "pressure_rumble_active_fraction": float((jump > 0.02).mean()),
        "drivers": scene.dominant_segments(w, scene.bundle.stations, ["wind_speed_ms", "wind_gust_ms", "wind_dir_deg"]),
        "note": "level ~ speed^1.5, tilt follows speed; gust fine structure synthetic, amplitude observed",
    }
    return out, info


# --------------------------------------------------------------------------- hail

HAIL_REF_MM = np.array([6.0, 10.0, 16.0, 25.0, 40.0])


def hail_rate_per_s(flux_m2s):
    """Audio impact rate vs the observed large-particle flux (per m2 per s): 6 * F^0.6, capped.
    Monotonic."""
    return np.minimum(6.0 * np.maximum(np.asarray(flux_m2s, float), 0.0) ** 0.6, 400.0)


def hail_ring_hz(d_mm):
    """Pitch of the hard impact: down as the stone gets bigger."""
    return np.clip(3600.0 * (10.0 / np.maximum(d_mm, 3.0)) ** 0.6, 500.0, 5000.0)


def _hail_kernels(sr: int):
    ks = []
    for D in HAIL_REF_MM:
        f = float(hail_ring_hz(D))
        ping = damped_sine(sr, f, 0.0025 + 0.00045 * D)
        thud = 0.5 * damped_sine(sr, f / 3.0, 0.006 + 0.001 * D)
        click = np.zeros(8)
        click[:3] = [1.0, -0.7, 0.3]
        k = sum_kernels(ping, thud, click * 0.8)
        ks.append(k / np.sqrt(np.sum(k**2)) * (D / 10.0) ** 2)
    return ks


def render_hail(scene: Scene, sr: int, n: int, seed: int):
    """Hard impacts (ping / clack with a short resonant ring, pitch down with size) only where hail
    evidence (station hail events, events.json, radar hail class) is near the listener. Rate from the
    observed large-particle flux (event counts or the tail of the drop spectrum), amplitude from the
    maximum diameter (mass^(2/3)). Without evidence: silence."""
    h = scene.hail()
    if not (h["w"] > 0).any():
        return np.zeros((2, n)), {"evidence": [], "rows": [], "active_seconds": 0.0, "events": 0,
                                  "note": "no hail evidence: silent"}
    rng = np.random.default_rng(seed)
    w, dmax = h["w"], np.maximum(h["dmax_mm"], 4.0)
    lam_ctl = hail_rate_per_s(h["flux"]) * np.sqrt(w) * (w > 0)
    idx, amps = impulse_train(rng, n, ctl_to_audio(lam_ctl, n, sr) / sr, 0.6)
    d_ev = ctl_to_audio(dmax, n, sr)[idx] * rng.uniform(0.4, 1.0, len(idx))
    amps = amps * 0.35 * ctl_to_audio(w, n, sr)[idx]
    kern = _hail_kernels(sr)
    ref = np.abs(np.log(d_ev[:, None]) - np.log(HAIL_REF_MM)[None, :]).argmin(axis=1) if len(idx) else np.zeros(0, int)
    pans = rng.uniform(-0.9, 0.9, len(idx))
    gl, gr = pan_gains(np.degrees(np.arcsin(pans)))
    out = np.zeros((2, n))
    for c in range(len(HAIL_REF_MM)):
        sel = ref == c
        if sel.any():
            out[0] += convolve_events(n, idx[sel], (amps * gl)[sel], kern[c])
            out[1] += convolve_events(n, idx[sel], (amps * gr)[sel], kern[c])
    out[np.abs(out) < 1e-12] = 0.0  # FFT round-off outside the events
    act = w > 0.02
    info = {
        "evidence": h["sources"], "rows": h["rows"], "events": len(idx),
        "evidence_items": h["n_events"],
        "active_seconds": float(act.mean() * scene.duration),
        "max_diameter_mm": float(dmax[act].max()) if act.any() else None,
        "impact_rate_per_s_max": float(lam_ctl.max()),
        "note": "synthesised from the data, not a recording; impacts only where hail was reported",
    }
    return out, info
