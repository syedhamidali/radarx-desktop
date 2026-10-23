"""Sounding readers and diagnostics: formats, definitions, and numbers checked against references.

References used (cited where they are used):

* University of Wyoming's own precipitable water for the BMX 2022-03-30 00 UTC sounding is stored in
  the ``pw`` column of ``data/BMX_20220330_00Z.csv`` (22.26 mm); the engine's value must agree.
  The file holds public NWS radiosonde data (levels as archived by the University of Wyoming).
* Analytic hodographs and profiles with exact answers (helicity of a circular hodograph, bulk
  shear of a linear wind, Bunkers deviation of 7.5 m/s, a constant-humidity column, a linear
  temperature profile).
* The lifting condensation level against Lawrence (2005), Bull. Amer. Meteor. Soc., 86, 225-233
  (LCL height ~ 125 m per K of dew-point depression).
* CAPE and CIN of the Weisman and Klemp (1982) analytic sounding against an independent
  numerical parcel integration (moist-adiabatic lapse rate of Rogers and Yau, 1989, A Short Course
  in Cloud Physics, 3rd ed.), which shares no code with radarx.
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import numpy as np
import pytest
from engine import sounding as S  # noqa: N812

pytest.importorskip("radarx")

FIX = Path(__file__).parent / "data" / "BMX_20220330_00Z.csv"
KT = S.KNOT


def bmx_rows() -> list[dict[str, str]]:
    with open(FIX, newline="") as fh:
        return list(csv.DictReader(fh))


def f(x: str) -> float:
    return float(x) if x.strip() else float("nan")


# -- the same sounding written in the four layouts -----------------------------------------------


def to_wyoming_text(rows: list[dict[str, str]]) -> str:
    out = [
        "<HTML>",
        "<TITLE>University of Wyoming - Radiosonde Data</TITLE>",
        "<H2>72230 BMX Birmingham Observations at 00Z 30 Mar 2022</H2>",
        "<PRE>",
        "-" * 77,
        "   PRES   HGHT   TEMP   DWPT   RELH   MIXR   DRCT   SKNT   THTA   THTE   THTV",
        "    hPa     m      C      C      %    g/kg    deg   knot     K      K      K ",
        "-" * 77,
    ]

    def cell(v: str, fmt: str) -> str:
        return (fmt % f(v)).rjust(7) if v.strip() else " " * 7

    for r in rows:
        line = (
            cell(r["pressure"], "%.1f")
            + cell(r["height"], "%.0f")
            + cell(r["temperature"], "%.1f")
            + cell(r["dewpoint"], "%.1f")
            + " " * 14  # RELH, MIXR
            + cell(r["direction"], "%.0f")
            + cell(r["speed"], "%.0f")
            + " " * 21  # THTA THTE THTV
        )
        out.append(line)
    out += [
        "</PRE><H3>Station information and sounding indices</H3><PRE>",
        "                         Station identifier: BMX",
        "                             Station number: 72230",
        "                           Observation time: 220330/0000",
        "                           Station latitude: 33.16",
        "                          Station longitude: -86.77",
        "                          Station elevation: 178.0",
        "</PRE>",
    ]
    return "\n".join(out)


def to_sharppy(rows: list[dict[str, str]]) -> str:
    out = [
        "%TITLE%",
        "BMX   220330/0000",
        "",
        "   LEVEL       HGHT       TEMP       DWPT       WDIR       WSPD",
        "-" * 67,
        "%RAW%",
    ]
    for r in rows:
        vals = [
            r[k] if r[k].strip() else "-9999.00"
            for k in ("pressure", "height", "temperature", "dewpoint", "direction", "speed")
        ]
        out.append(",".join(vals))
    out.append("%END%")
    return "\n".join(out)


def to_iem_json(rows: list[dict[str, str]]) -> str:
    prof = []
    for r in rows:
        prof.append(
            {
                "pres": f(r["pressure"]),
                "hght": f(r["height"]),
                "tmpc": None if not r["temperature"].strip() else f(r["temperature"]),
                "dwpc": None if not r["dewpoint"].strip() else f(r["dewpoint"]),
                "drct": f(r["direction"]),
                "sknt": f(r["speed"]),
            }
        )
    return json.dumps(
        {"profiles": [{"station": "KBMX", "valid": "2022-03-30T00:00:00Z", "profile": prof}]}
    )


@pytest.fixture(scope="module")
def bmx_csv() -> S.Sounding:
    return S.read_sounding_file(FIX, wind_units="kt")


# -- readers --------------------------------------------------------------------------------------


def test_bmx_csv_reads_with_its_header_and_station(bmx_csv: S.Sounding) -> None:
    ds = bmx_csv.profile
    assert bmx_csv.station == "BMX" and bmx_csv.fmt == "csv"
    assert ds.sizes["height"] == 80  # 81 rows, the top one has no temperature
    assert float(ds.pressure[0]) == 99500.0 and float(ds.temperature[0]) == pytest.approx(298.15)
    # a plain "height" column is geopotential (Wyoming/SPC convention, as in radarx): converted to
    # geometric height, 178 * Re / (Re - 178); the file says nothing else, so the note says so
    assert float(ds.height[0]) == pytest.approx(178.0 * S.RE / (S.RE - 178.0))
    assert any("geopotential" in n for n in bmx_csv.notes)
    assert np.all(np.diff(ds.height.values) > 0) and np.all(np.diff(ds.pressure.values) < 0)
    # 170 degrees at 4 kt: wind FROM 170 -> blows towards the north-north-west
    assert float(ds.u[0]) == pytest.approx(-4 * KT * math.sin(math.radians(170)))
    assert float(ds.v[0]) == pytest.approx(-4 * KT * math.cos(math.radians(170)))
    assert "launch hour is not in it" in " ".join(bmx_csv.notes)


def test_unitless_wind_speed_is_flagged_not_guessed() -> None:
    snd = S.read_sounding_file(FIX)  # the header has no unit
    assert any("no unit" in n and "m/s is assumed" in n for n in snd.notes)
    kt = S.read_sounding_file(FIX, wind_units="kt")
    assert float(kt.profile.wind_speed[1]) == pytest.approx(
        float(snd.profile.wind_speed[1]) * KT, rel=1e-6
    )
    assert not any("no unit" in n for n in kt.notes)
    with pytest.raises(S.SoundingError, match="unknown wind speed unit"):
        S.read_sounding_file(FIX, wind_units="furlongs")


def test_all_formats_give_the_same_profile(tmp_path: Path, bmx_csv: S.Sounding) -> None:
    rows = bmx_rows()
    files = {
        "wyoming_text": ("bmx.txt", to_wyoming_text(rows)),
        "sharppy": ("bmx_sharppy.txt", to_sharppy(rows)),
        "iem_json": ("bmx.json", to_iem_json(rows)),
    }
    ref = bmx_csv.profile
    for fmt, (name, text) in files.items():
        path = tmp_path / name
        path.write_text(text)
        assert S.detect_format(text) == fmt
        snd = S.read_sounding_file(path)  # auto-detected
        assert snd.fmt == fmt, fmt
        ds = snd.profile
        assert ds.sizes["height"] == ref.sizes["height"], fmt
        for var in ("pressure", "temperature", "dewpoint", "u", "v"):
            np.testing.assert_allclose(
                ds[var].values, ref[var].values, rtol=1e-9, atol=1e-6, err_msg=f"{fmt} {var}"
            )
        assert snd.launch_time == "2022-03-30T00:00:00Z", fmt


def test_wyoming_text_station_block_and_missing_fields(tmp_path: Path) -> None:
    snd = S.parse_wyoming_text(to_wyoming_text(bmx_rows()), "bmx.txt")
    assert snd.station == "BMX" and snd.launch_time == "2022-03-30T00:00:00Z"
    assert snd.latitude == pytest.approx(33.16) and snd.longitude == pytest.approx(-86.77)
    assert "Birmingham" in snd.station_name
    # the last row of the table has only pressure and wind: skipped, not zero-filled
    assert "without temperature" in " ".join(snd.notes)


def test_sharppy_header_and_missing_values() -> None:
    text = to_sharppy(bmx_rows())
    snd = S.parse_sharppy(text)
    assert snd.station == "BMX" and snd.launch_time == "2022-03-30T00:00:00Z"
    assert "SHARPpy" in snd.source


def test_csv_with_units_in_the_header_and_a_drift_track(tmp_path: Path) -> None:
    """UAH-style file: comment lines, per-row position and time, heights AGL, speed in knots."""
    n = 60
    p = np.linspace(1000, 300, n)
    z = np.linspace(0, 9000, n)
    lines = [
        "# UAH Radiosonde Data",
        "# 20220322, 1359 UTC, Brooksville, MS, 86 m",
        "latitude (deg), longitude (deg), UTC (HH:MM:SS), height (m AGL), pressure(mb), "
        "temp (deg C), RH (%), dewpoint (deg C), wind speed (kts), wind direction (deg)",
    ]
    for i in range(n):
        sec = i * 50
        hh, rem = divmod(13 * 3600 + 59 * 60 + sec, 3600)
        mm, ss = divmod(rem, 60)
        lines.append(
            f"{33.0 + 0.5 * i / n:.4f},{-88.0:.4f},{hh:02d}:{mm:02d}:{ss:02d},"
            f"{z[i]:.1f},{p[i]:.2f},"
            f"{25 - 6.5 * z[i] / 1000:.2f},60,{15 - 7.5 * z[i] / 1000:.2f},"
            f"{10 + i * 0.5:.1f},{200 + i}"
        )
    snd = S.parse_csv("\n".join(lines), "uah.csv")
    assert snd.station == "Brooksville" and snd.launch_time == "2022-03-22T13:59:00Z"
    assert snd.surface_height_m == pytest.approx(86.0)  # AGL + the 86 m of the comment line
    assert float(snd.profile.pressure[0]) == pytest.approx(100000.0)  # mb -> Pa
    assert float(snd.profile.wind_speed[0]) == pytest.approx(10 * KT)  # kts honoured
    assert snd.drift and snd.drift["max_km"] == pytest.approx(0.5 * 111.195 * (59 / 60), rel=0.02)
    assert snd.drift["towards_deg"] == pytest.approx(0.0, abs=1.0)  # moved due north
    assert snd.drift["duration_min"] == pytest.approx(59 * 50 / 60, rel=0.01)
    assert "drifted" in snd.drift_note() and "km" in snd.drift_note()
    assert any("AGL" in n for n in snd.notes)


def test_csv_whose_column_names_are_a_comment_line() -> None:
    """ULM / PERiLS style: the header row itself starts with '#'."""
    lines = [
        "# ULM PERiLS Radiosonde Data",
        "# 20220330, 215800 UTC, Sturgis_MS, 95 m",
        "# latitude (deg), longitude (deg), UTC (HH:MM:SS), height (m AGL), pressure(mb), "
        "temp (deg C), RH (%), dewpoint (deg C), wind speed (m/s), wind direction (deg), "
        "ascent rate (m/s)",
    ]
    for i in range(8):
        lines.append(
            f"33.34,-89.05,21:58:{i * 5:02d},{i * 37.4:.1f},{986.4 - i * 4.2:.1f},"
            f"{25.1 - i * 0.25:.2f},57.0,{16.0 - i * 0.5:.1f},{10.3 + i * 0.1:.1f},{160 - i},6.6"
        )
    snd = S.parse_csv("\n".join(lines), "ulm.txt")
    assert snd.station == "Sturgis_MS" and snd.launch_time == "2022-03-30T21:58:00Z"
    assert snd.profile.sizes["height"] == 8 and snd.surface_height_m == pytest.approx(95.0)
    assert float(snd.profile.wind_speed[0]) == pytest.approx(10.3)  # m/s from the header
    assert not any("no unit" in n for n in snd.notes)


def test_csv_relative_humidity_instead_of_dew_point() -> None:
    lines = ["pres,hght,tmpc,relh,drct,sknt"]
    for p, z, t in (
        (1000, 100, 20.0),
        (900, 1000, 14.0),
        (800, 2000, 8.0),
        (700, 3100, 1.0),
        (600, 4300, -6.0),
    ):
        lines.append(f"{p},{z},{t},50,270,20")
    snd = S.parse_csv("\n".join(lines))
    td = snd.profile.dewpoint.values - S.T0
    # 20 degC at 50 % RH: Bolton (1980) eq. 10 gives a dew point of 9.3 degC
    assert td[0] == pytest.approx(9.3, abs=0.1)
    assert any("derived from relative humidity" in n for n in snd.notes)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "hello world",
        "<html><body>404 Not Found</body></html>",
        "a,b\n1,2\n",
        "%RAW%\n1,2\n%END%",
        "{}",
        '{"profiles": []}',
    ],
)
def test_not_a_sounding_gives_a_clear_error(text: str) -> None:
    with pytest.raises(S.SoundingError):
        S.read_sounding_text(text)


def test_a_spreadsheet_is_refused_plainly(tmp_path: Path) -> None:
    f = tmp_path / "sonde.xlsx"
    f.write_bytes(b"PK\x03\x04" + b"\x00" * 200)
    with pytest.raises(S.SoundingError, match="binary file"):
        S.read_sounding_file(f)


def test_missing_file_and_oversized_file(tmp_path: Path) -> None:
    with pytest.raises(S.SoundingError, match="not found"):
        S.read_sounding_file(tmp_path / "nope.csv")
    big = tmp_path / "big.csv"
    big.write_bytes(b"x" * 10)
    old = S.MAX_FILE_BYTES
    S.MAX_FILE_BYTES = 5
    try:
        with pytest.raises(S.SoundingError, match="larger than"):
            S.read_sounding_file(big)
    finally:
        S.MAX_FILE_BYTES = old


def test_dewpoint_gaps_are_handled_without_inventing_data() -> None:
    p = np.array([1000, 900, 800, 700, 600, 500, 400, 300, 250, 200.0]) * 100
    z = np.array([100, 1000, 1950, 3000, 4200, 5600, 7200, 9200, 10400, 11800.0])
    t = np.array([25, 19, 13, 6, -2, -12, -25, -40, -49, -57.0]) + S.T0
    td = np.array([15, 12, np.nan, 0, -8, np.nan, -35, -50, np.nan, np.nan]) + S.T0
    ds, notes, filled = S.build_profile(pressure_pa=p, height_m=z, temperature_k=t, dewpoint_k=td)
    assert filled.tolist() == [False, False, True, False, False, True, False, False, True, True]
    reported = ds.dewpoint_reported.values
    assert np.isnan(reported[filled]).all() and np.isfinite(reported[~filled]).all()  # not drawn
    assert any("between reported ones" in n for n in notes) and any("very dry" in n for n in notes)
    assert (ds.dewpoint.values <= ds.temperature.values).all()


def test_levels_without_pressure_decrease_are_dropped() -> None:
    p = np.array([1000, 900, 950, 800, 700.0]) * 100  # a sensor glitch at the third level
    z = np.array([100, 1000, 1100, 2000, 3000.0])
    t = np.array([20, 14, 13, 6, 0.0]) + S.T0
    ds, notes, _ = S.build_profile(pressure_pa=p, height_m=z, temperature_k=t, dewpoint_k=t - 5)
    assert ds.sizes["height"] == 4 and any("did not fall with height" in n for n in notes)


# -- kinematics: exact answers --------------------------------------------------------------------


def hodograph(z, u, v):
    z = np.asarray(z, float)
    return z, np.asarray(u, float), np.asarray(v, float)


def test_bulk_shear_of_a_linear_wind_is_exact() -> None:
    z = np.arange(0, 12001, 250.0)
    u, v = 0.003 * z, 0.001 * z  # shear 0.003/0.001 per second
    sh = S.bulk_shear(z, u, v, 0.0, 0.0, 6000.0)
    assert sh["u"] == pytest.approx(18.0) and sh["v"] == pytest.approx(6.0)
    assert sh["speed_ms"] == pytest.approx(math.hypot(18, 6), abs=0.05)
    assert sh["speed_kt"] == pytest.approx(math.hypot(18, 6) / KT, abs=0.5)
    # above the ground: the layer is relative to the launch height
    sh2 = S.bulk_shear(z + 500.0, u, v, 500.0, 1000.0, 3000.0)
    assert sh2["u"] == pytest.approx(6.0)
    assert S.bulk_shear(z, u, v, 0.0, 0.0, 20000.0) is None  # winds do not reach: no number


def test_srh_of_a_circular_hodograph_is_r_squared_times_the_angle() -> None:
    """Davies-Jones et al. (1990): for a hodograph arc of radius R about the storm motion,
    SRH = R^2 * (angle swept clockwise). Quarter circle, R = 10 m/s -> 100 * pi/2 = 157 m2/s2."""
    R = 10.0  # noqa: N806
    z = np.linspace(0, 3000, 3001)
    ang = (np.pi / 2) * z / 3000.0  # veering (clockwise) with height
    u, v = R * np.sin(ang), R * np.cos(ang)
    srh = S.storm_relative_helicity(z, u, v, 0.0, 3000.0, 0.0, 0.0)
    assert srh == pytest.approx(R * R * np.pi / 2, rel=2e-3)
    # backing (counter-clockwise) winds give the opposite sign
    srh_back = S.storm_relative_helicity(z, u[::-1], v[::-1], 0.0, 3000.0, 0.0, 0.0)
    assert srh_back == pytest.approx(-R * R * np.pi / 2, rel=2e-3)
    # a straight, constant wind has no helicity whatever the storm motion
    assert S.storm_relative_helicity(
        z, np.full_like(z, 10.0), np.zeros_like(z), 0.0, 3000.0, 4.0, 2.0
    ) == pytest.approx(0.0, abs=1e-9)
    # a straight shear with the storm moving at the mean wind: zero helicity (no turning)
    assert S.storm_relative_helicity(
        z, 0.01 * z, np.zeros_like(z), 0.0, 3000.0, 15.0, 0.0
    ) == pytest.approx(0.0, abs=1e-9)


def test_bunkers_motion_is_7_5_ms_to_the_right_of_the_shear() -> None:
    """Bunkers et al. (2000): the right mover is the 0-6 km mean wind plus 7.5 m/s perpendicular to
    the 0-6 km shear vector, to its right; the left mover mirrors it."""
    z = np.arange(0, 12001, 100.0)
    u, v = 0.004 * z, 0.002 * z  # a straight hodograph; mean wind = (12, 6) at 3 km
    m = S.bunkers_motion(z, u, v, 0.0)
    mean = m["mean_wind_0_6km"]
    assert (mean["u"], mean["v"]) == pytest.approx((12.0, 6.0), abs=0.05)
    sx, sy = m["shear_0_6km_vector"]["u"], m["shear_0_6km_vector"]["v"]
    # 0-0.5 km mean to 5.5-6 km mean: (0.004, 0.002) * 5500 m
    assert (sx, sy) == pytest.approx((22.0, 11.0), abs=0.05)
    rm, lm = m["right_mover"], m["left_mover"]
    dev = (rm["u"] - mean["u"], rm["v"] - mean["v"])
    assert math.hypot(*dev) == pytest.approx(7.5, abs=0.05)
    assert dev[0] * sx + dev[1] * sy == pytest.approx(0.0, abs=0.2)  # perpendicular to the shear
    assert dev[0] * sy - dev[1] * sx > 0  # shear x deviation: the deviation lies to the right
    assert (lm["u"] + rm["u"]) / 2 == pytest.approx(mean["u"], abs=0.02)  # symmetric about the mean
    # no shear: the deviation direction is undefined, so no storm motion is returned
    assert S.bunkers_motion(z, np.full_like(z, 8.0), np.zeros_like(z), 0.0) is None


def test_precipitable_water_of_a_constant_humidity_column_is_exact() -> None:
    import xarray as xr
    from radarx.io import sounding as rs

    p = np.linspace(100000, 70000, 31)
    td = np.full(p.size, 283.15)  # 10 degC at every level
    q = float(
        rs.specific_humidity_from_dewpoint(xr.DataArray(td[:1]), xr.DataArray(p[:1])).values[0]
    )
    qs = np.asarray(rs.specific_humidity_from_dewpoint(xr.DataArray(td), xr.DataArray(p)).values)
    pw, top = S.precipitable_water_mm(p, td, td)
    exact = float(np.trapezoid(qs, -p)) / S.G0  # (1/g) integral q dp, kg/m2 = mm
    assert pw == pytest.approx(exact, rel=1e-6) and top == pytest.approx(700.0)
    assert 0.9 * q * 30000.0 / S.G0 < pw < 1.2 * q * 30000.0 / S.G0  # q varies a little with p


# -- thermodynamics: references -----------------------------------------------------------------


def test_bmx_precipitable_water_matches_the_university_of_wyoming_value(
    bmx_csv: S.Sounding,
) -> None:
    pw_file = float(bmx_rows()[0]["pw"])
    assert pw_file == pytest.approx(22.26)
    d = S.diagnose(bmx_csv)
    assert d["moisture"]["precipitable_water_mm"] == pytest.approx(pw_file, rel=0.03)


def test_lcl_height_agrees_with_the_125_m_per_kelvin_rule() -> None:
    """Lawrence (2005): LCL height ~ 125 m per K of dew-point depression (within a few percent)."""
    for t0, spread in ((30.0, 10.0), (25.0, 4.0), (35.0, 20.0)):
        z = np.linspace(0, 6000, 61)
        p = 100000.0 * np.exp(-z / 8000.0)
        t = (t0 + S.T0) - 6.5e-3 * z
        td = np.full(z.size, np.nan)
        td[:] = (t0 - spread + S.T0) - 1.8e-3 * z  # Td falls ~1.8 K/km: a mixed layer-ish profile
        ds, _, _ = S.build_profile(pressure_pa=p, height_m=z, temperature_k=t, dewpoint_k=td)
        snd = S.Sounding(profile=ds)
        lcl = S.diagnose(snd)["parcels"]["surface-based"]["lcl_agl_m"]
        assert lcl == pytest.approx(125.0 * spread, rel=0.08), (t0, spread, lcl)


def _reference_parcel(snd: S.Sounding) -> tuple[float, float]:
    """CAPE and CIN of the surface parcel by direct numerical integration (no radarx code).

    Dry adiabat up to saturation, then the moist-adiabatic lapse rate of Rogers and Yau (1989):
    G = g (1 + Lv rs / (Rd T)) / (cp + Lv^2 rs eps / (Rd T^2)); virtual-temperature buoyancy;
    explicit height steps of 5 m. Positive area above the LFC is CAPE, negative area below it CIN.
    """
    g, cp, rd, eps, lv = 9.80665, 1005.7, 287.04, 0.622, 2.501e6
    ds = snd.profile
    z, p, T, Td = (ds[k].values for k in ("height", "pressure", "temperature", "dewpoint"))  # noqa: N806

    def es(t):
        tc = t - 273.15
        return 611.2 * np.exp(17.67 * tc / (tc + 243.5))

    def mixr(e, pp):
        return eps * e / (pp - e)

    def tv(t, r):
        return t * (1 + r / eps) / (1 + r)

    dz = 5.0
    zz = np.arange(z[0], z[-1], dz)
    penv = np.exp(np.interp(zz, z, np.log(p)))
    tenv = np.interp(zz, z, T)
    renv = mixr(es(np.interp(zz, z, Td)), penv)
    tp, pp, rp = np.empty(zz.size), np.empty(zz.size), np.empty(zz.size)
    tp[0], pp[0], rp[0] = T[0], p[0], mixr(es(Td[0]), p[0])
    sat = False
    for k in range(1, zz.size):
        pp[k] = pp[k - 1] - pp[k - 1] / (rd * tv(tp[k - 1], rp[k - 1])) * g * dz
        if not sat:
            tp[k], rp[k] = tp[k - 1] - g / cp * dz, rp[k - 1]
            sat = rp[k] * pp[k] / (eps + rp[k]) >= es(tp[k])
        else:
            rs = mixr(es(tp[k - 1]), pp[k - 1])
            gam = (
                g
                * (1 + lv * rs / (rd * tp[k - 1]))
                / (cp + lv**2 * rs * eps / (rd * tp[k - 1] ** 2))
            )
            tp[k] = tp[k - 1] - gam * dz
            rp[k] = mixr(es(tp[k]), pp[k])
    b = g * (tv(tp, rp) - tv(tenv, renv)) / tv(tenv, renv)
    first = int(np.argmax(b > 0))
    return float(np.sum(b[first:][b[first:] > 0]) * dz), float(
        np.sum(b[:first][b[:first] < 0]) * dz
    )


def test_weisman_klemp_cape_and_cin_against_an_independent_integration() -> None:
    snd = S.weisman_klemp()
    # the profile itself: 1000 hPa surface, theta = 300 K, tropopause at 12 km / 343 K
    ds = snd.profile
    assert float(ds.pressure[0]) == pytest.approx(100000.0)
    theta = ds.temperature.values * (100000.0 / ds.pressure.values) ** (287.04 / 1005.7)
    k12 = int(np.argmin(abs(ds.height.values - 12000.0)))
    assert theta[0] == pytest.approx(300.0, abs=0.01) and theta[k12] == pytest.approx(
        343.0, abs=0.2
    )
    d = S.diagnose(snd)
    sb = d["parcels"]["surface-based"]
    ref_cape, ref_cin = _reference_parcel(snd)
    assert sb["cape"] == pytest.approx(ref_cape, rel=0.15), (sb["cape"], ref_cape)
    assert sb["cin"] == pytest.approx(ref_cin, abs=15.0), (sb["cin"], ref_cin)
    # ordering that must hold for this profile: the most-unstable parcel is the most buoyant
    mu, ml = d["parcels"]["most-unstable"], d["parcels"]["mixed-layer"]
    assert mu["cape"] >= sb["cape"] >= 1000 and mu["cape"] >= ml["cape"]
    assert 0 < sb["lcl_agl_m"] < sb["lfc_agl_m"] < sb["el_agl_m"] <= 12500
    assert sb["lifted_index"] < -4


def test_virtual_temperature_correction_changes_cape_in_the_documented_direction() -> None:
    snd = S.weisman_klemp()
    with_tv = S.diagnose(snd, virtual=True)["parcels"]["surface-based"]["cape"]
    without = S.diagnose(snd, virtual=False)["parcels"]["surface-based"]["cape"]
    assert without < with_tv  # Doswell and Rasmussen (1994): neglecting Tv lowers CAPE
    assert (with_tv - without) / with_tv < 0.15  # by a few percent, not by half


def test_a_stable_profile_has_no_lfc_and_says_so() -> None:
    z = np.linspace(0, 8000, 81)
    p = 100000.0 * np.exp(-z / 8000.0)
    t = 285.0 - 2.5e-3 * z  # a very stable lapse rate
    ds, _, _ = S.build_profile(pressure_pa=p, height_m=z, temperature_k=t, dewpoint_k=t - 8)
    sb = S.diagnose(S.Sounding(profile=ds))["parcels"]["surface-based"]
    assert sb["cape"] == 0 and sb["lfc_agl_m"] is None and sb["el_agl_m"] is None
    assert "never becomes buoyant" in sb["note"]


def test_freezing_level_is_interpolated_between_levels_and_reported_above_ground() -> None:
    z = np.arange(300, 9001, 300.0)  # launch site at 300 m
    t = (10.0 + S.T0) - 6.5e-3 * (z - 300.0)
    p = 98000.0 * np.exp(-(z - 300.0) / 8000.0)
    ds, _, _ = S.build_profile(pressure_pa=p, height_m=z, temperature_k=t, dewpoint_k=t - 10)
    lv = S.diagnose(S.Sounding(profile=ds))["levels"]
    assert lv["freezing_agl_m"] == pytest.approx(10.0 / 6.5e-3, abs=1.0)  # 1538 m AGL
    assert lv["minus20_agl_m"] == pytest.approx(30.0 / 6.5e-3, abs=1.0)
    assert lv["wet_bulb_zero_agl_m"] < lv["freezing_agl_m"]  # wet bulb is colder than the air
    cold = S.build_profile(
        pressure_pa=p,
        height_m=z,
        temperature_k=np.full(z.size, 260.0),
        dewpoint_k=np.full(z.size, 255.0),
    )
    d = S.diagnose(S.Sounding(profile=cold[0]))
    assert d["levels"]["freezing_agl_m"] is None and any("freezing level" in n for n in d["notes"])


def test_missing_winds_leave_the_kinematics_empty_with_a_note() -> None:
    snd = S.weisman_klemp()
    ds = snd.profile.copy()
    ds["u"] = ds["u"] * np.nan
    ds["v"] = ds["v"] * np.nan
    d = S.diagnose(S.Sounding(profile=ds))
    assert d["shear"] is None and d["srh"] is None and d["storm_motion"] is None
    assert any("no winds" in n for n in d["notes"]) and d["parcels"]["surface-based"]["cape"] > 0


# -- what the UI and the CLI receive -------------------------------------------------------------


def test_diagnostics_are_labelled_complete_and_json_safe(bmx_csv: S.Sounding) -> None:
    res = S.analyse(bmx_csv)
    text = json.dumps(res, allow_nan=False)  # no NaN / Infinity anywhere
    assert json.loads(text)["diagnostics"]["parcels"]["surface-based"]["parcel"] == "surface-based"
    d = res["diagnostics"]
    assert set(d["parcels"]) == {"surface-based", "mixed-layer", "most-unstable"}
    for p in d["parcels"].values():
        assert p["label"] and set(p["start"]) == {"pressure_hpa", "temperature_c", "dewpoint_c"}
    for key in (
        "cape",
        "cin",
        "lcl",
        "lfc",
        "el",
        "lifted_index",
        "shear",
        "srh",
        "bunkers",
        "freezing",
        "wet_bulb_zero",
        "pw",
        "lapse_rate",
    ):
        assert d["definitions"][key].strip(), key
    assert "lapse_rates" in d and isinstance(d["lapse_rates"], dict)
    assert d["units"]["height"].startswith("m AGL") and "Bolton" in d["references"]["bolton"]
    assert "launch hour is not in it" in " ".join(
        d["notes"]
    )  # a missing hour is said, not invented
    assert res["drift_note"] and res["launch_time"] == ""  # date only: the hour is not invented
    prov = res["provenance"]
    assert prov["kind"] == "observed" and "drifts" in prov["uncertainty"]
    assert res["levels"]["n_levels"] == 80 and len(res["levels"]["pressure_hpa"]) == 80


def test_a_coarse_sounding_is_flagged_as_approximate() -> None:
    p = np.array([1000, 925, 850, 700, 500, 400, 300, 250, 200.0]) * 100
    z = np.array([100, 780, 1500, 3100, 5700, 7300, 9300, 10500, 11900.0])
    t = np.array([24, 20, 15, 6, -10, -22, -40, -50, -56.0]) + S.T0
    ds, _, _ = S.build_profile(pressure_pa=p, height_m=z, temperature_k=t, dewpoint_k=t - 8)
    d = S.diagnose(S.Sounding(profile=ds))
    assert any("Only 9 levels" in n and "approximate" in n for n in d["notes"])


def test_long_soundings_are_thinned_for_the_ui_only() -> None:
    z = np.linspace(0, 15000, 5000)
    p = 100000.0 * np.exp(-z / 8000.0)
    t = 290.0 - 6.5e-3 * z
    ds, _, _ = S.build_profile(pressure_pa=p, height_m=z, temperature_k=t, dewpoint_k=t - 6)
    snd = S.Sounding(profile=ds)
    lv = snd.levels(900)
    assert lv["thinned"] and len(lv["pressure_hpa"]) <= 900 and lv["n_levels"] == 5000
    assert lv["pressure_hpa"][0] == pytest.approx(1000.0) and lv["height_msl_m"][
        -1
    ] == pytest.approx(15000.0, abs=1)
    assert snd.levels(None)["thinned"] is False


def test_the_example_sounding_is_labelled_simulated() -> None:
    res = S.analyse(S.weisman_klemp())
    assert res["provenance"]["kind"] == "simulated" and "Weisman" in res["provenance"]["source"]
    assert res["station"] == "SYNTHETIC" and res["diagnostics"]["provenance"]["kind"] == "simulated"


def test_summary_lines_name_the_parcels_and_units(bmx_csv: S.Sounding) -> None:
    text = "\n".join(S.summary_lines(S.diagnose(bmx_csv)))
    for token in (
        "surface-based",
        "mixed-layer",
        "most-unstable",
        "J/kg",
        "m AGL",
        "bulk shear 0-6km",
        "SRH 0-3km",
        "precipitable water",
        "lapse rate",
    ):
        assert token in text, token


# -- lapse rates: known-answer tests -----------------------------------------------------------


def test_lapse_rate_of_a_linear_temperature_profile_is_exact() -> None:
    """A temperature that falls linearly at exactly 6.5 K/km gives 6.5 K/km in any layer."""
    z = np.arange(0.0, 12001.0, 50.0)
    gamma = 6.5e-3  # K/m  (standard atmosphere)
    t = 300.0 - gamma * z
    # exact layer
    assert S.lapse_rate(z, t, 0.0, 0.0, 3000.0) == pytest.approx(6.5, rel=1e-6)
    assert S.lapse_rate(z, t, 0.0, 0.0, 6000.0) == pytest.approx(6.5, rel=1e-6)
    # layer above the ground station
    assert S.lapse_rate(z + 500.0, t, 500.0, 0.0, 3000.0) == pytest.approx(6.5, rel=1e-6)
    # layer not spanned: None
    assert S.lapse_rate(z, t, 0.0, 0.0, 20000.0) is None


def test_lapse_rate_of_an_isothermal_layer_is_zero() -> None:
    z = np.arange(0.0, 5001.0, 100.0)
    t = np.full(z.size, 280.0)
    assert S.lapse_rate(z, t, 0.0, 0.0, 3000.0) == pytest.approx(0.0, abs=1e-9)


def test_lapse_rate_flags_a_super_adiabatic_layer_in_notes() -> None:
    """An environmental lapse rate > 9.8 K/km (super-adiabatic) adds a note to diagnose()."""
    z = np.arange(0.0, 5001.0, 10.0)
    # surface temperature inversion then rapidly falling temperature (e.g. near-surface heating)
    t = 305.0 - 12e-3 * z  # 12 K/km: super-adiabatic
    p = 100000.0 * np.exp(-z / 8000.0)
    td = t - 5.0
    ds, _, _ = S.build_profile(pressure_pa=p, height_m=z, temperature_k=t, dewpoint_k=td)
    d = S.diagnose(S.Sounding(profile=ds))
    assert d["lapse_rates"]["0-3km_k_per_km"] == pytest.approx(12.0, rel=1e-3)
    assert any("super-adiabatic" in n for n in d["notes"])


def test_diagnose_includes_lapse_rates_for_weisman_klemp() -> None:
    """Weisman-Klemp: theta increases at 5/4 power of z/ztr so the lapse rate is sub-dry-adiabatic
    everywhere. The 0-3 km value should be between 6 and 9 K/km."""
    snd = S.weisman_klemp()
    d = S.diagnose(snd)
    assert "lapse_rates" in d
    lr_03 = d["lapse_rates"]["0-3km_k_per_km"]
    assert lr_03 is not None and 6.0 < lr_03 < 9.5
    # 700-500 hPa layer should also be available for this deep sounding
    assert d["lapse_rates"]["700_500hPa_k_per_km"] is not None


# -- radarx.retrieve.wind_profile cross-validation ------------------------------------------


def test_radarx_wind_profile_matches_local_implementations_on_weisman_klemp() -> None:
    """radarx.retrieve.wind_profile.bulk_shear, bunkers_storm_motion and storm_relative_helicity
    must agree with the local implementations to within 0.1 m/s and 1 m2/s2, so that either path
    through _wind_diag_radarx produces the same numbers and the local functions are a valid
    fall-back. Implements the cross-validation described in docs/source-diagnostics.md."""
    pytest.importorskip("radarx.retrieve.wind_profile")
    from radarx.retrieve import wind_profile as wp

    snd = S.weisman_klemp()
    ds = snd.profile
    z = np.asarray(ds["height"].values, dtype=float)
    u = np.asarray(ds["u"].values, dtype=float)
    v = np.asarray(ds["v"].values, dtype=float)
    ground = float(z[0])

    # bulk shear 0-6 km
    local_sh6 = S.bulk_shear(z, u, v, ground, 0.0, 6000.0)
    rx_sh6 = wp.bulk_shear(ds, bottom=0.0, top=6000.0, ground=ground)
    assert local_sh6["u"] == pytest.approx(float(rx_sh6["shear_u"].values), abs=0.1)
    assert local_sh6["v"] == pytest.approx(float(rx_sh6["shear_v"].values), abs=0.1)

    # bulk shear 0-1 km
    local_sh1 = S.bulk_shear(z, u, v, ground, 0.0, 1000.0)
    rx_sh1 = wp.bulk_shear(ds, bottom=0.0, top=1000.0, ground=ground)
    assert local_sh1["u"] == pytest.approx(float(rx_sh1["shear_u"].values), abs=0.1)

    # Bunkers right mover
    local_bk = S.bunkers_motion(z, u, v, ground)
    rx_right = wp.bunkers_storm_motion(ds, mover="right", ground=ground)
    assert local_bk["right_mover"]["u"] == pytest.approx(float(rx_right["u"].values), abs=0.2)
    assert local_bk["right_mover"]["v"] == pytest.approx(float(rx_right["v"].values), abs=0.2)

    # SRH 0-3 km for the right mover
    cx, cy = local_bk["right_mover"]["u"], local_bk["right_mover"]["v"]
    local_srh = S.storm_relative_helicity(z, u, v, ground, 3000.0, cx, cy)
    rx_srh = wp.storm_relative_helicity(
        ds, storm_motion="right", bottom=0.0, top=3000.0, ground=ground
    )
    assert local_srh == pytest.approx(float(rx_srh.values), abs=1.0)
