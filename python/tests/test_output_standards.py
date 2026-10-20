"""Everything the engine writes follows community standards and round-trips.

* NetCDF (CfRadial1 / CfRadial2 / FM-301 style groups), Zarr, mosaic and wind grids are checked
  with the real **CF Checker** when it is installed (``pip install cfchecker cfunits`` + UDUNITS-2
  + the CF standard-name table, see ``tests/cf_check.py``) and always with the strict structural
  checks of that module;
* every written file is read back and the data, masks, flags and times compared;
* CSV, figures, recipes and scripts carry software / version / time / source metadata and never an
  absolute path of this machine.

Real files (``~/Downloads/Others/XRADAR_EXAMPLES``, the Mayfield radars) are used when present and
only ever read.
"""

from __future__ import annotations

import io
import json
import os
import re
import xml.dom.minidom
from pathlib import Path

import netCDF4
import numpy as np
import pytest
import xarray as xr

from engine import cfwrite, derived, mosaic, provenance
from engine import multidoppler as mdop
from engine.exporter import RadarExporter, _netcdf_safe_tree
from engine.reader import RadarReader, sorted_sweep_names
from tests import cf_check
from tests.test_mosaic import GRID, A, B, flat, make_reader, make_volume
from tests.test_multidoppler import GRID as WIND_GRID
from tests.test_multidoppler import pair, vortex_wind

HOME = str(Path.home())
EXAMPLES = Path(os.environ.get("XRADAR_EXAMPLES", Path.home() / "Downloads/Others/XRADAR_EXAMPLES"))
KTLX = EXAMPLES / "KTLX20130520_201643_V06.gz"
CPOL_PPI = EXAMPLES / "PPI_20170825_223108.720_to_20170825_223330.405_CPOLRVP.nc"
CPOL_RHI = EXAMPLES / "RHI_20170825_223408_to_20170825_223445_CPOLRVP_MATT_RHI.nc"
MAYFIELD = Path("/Users/syed44/Downloads/SEVERE_CASES_TEST/mayfield")

needs_checker = pytest.mark.skipif(
    not cf_check.real_checker_available(),
    reason="CF Checker (cfchecker + cfunits + UDUNITS-2 + CF standard-name table) not available",
)
needs_radarx = pytest.mark.skipif(
    __import__("importlib").util.find_spec("radarx") is None, reason="radarx not installed"
)

#: CF's "coordinate variable must be strictly monotonic" is violated by source files whose rays
#: share a time stamp; the data are written unchanged (documented in docs/OUTPUT_STANDARDS.md).
DUPLICATE_RAY_TIMES = ("co-ordinate variable not monotonic",)


# ---------------------------------------------------------------------------
# A realistic synthetic volume (packed NEXRAD-like moments, time, site, "None" attributes)
# ---------------------------------------------------------------------------

N_AZ, N_RNG = 90, 120
NO_ECHO = -33.0


def make_cf_volume(*, with_time: bool = True) -> xr.DataTree:
    """3 sweeps of DBZH/VRADH/ZDR/RHOHV decoded from uint8 codes (code 0 = no echo = -33 dBZ)."""
    rng = np.random.default_rng(7)
    t0 = np.datetime64("2024-05-01T20:00:00", "ns")
    sweeps = {}
    for i, elev in enumerate((0.5, 1.5, 2.4)):
        az = np.arange(N_AZ) * (360.0 / N_AZ) + 0.2
        codes = rng.integers(0, 255, (N_AZ, N_RNG)).astype(np.uint8)
        codes[:, N_RNG // 2 :] = 0  # the outer half of the sweep has no echo
        data = {}
        for name, (scale, offset, units, std) in {
            "DBZH": (0.5, -33.0, "dBZ", "radar_equivalent_reflectivity_factor_h"),
            "VRADH": (
                0.5,
                -64.5,
                "meters per seconds",
                "radial_velocity_of_scatterers_away_from_instrument_h",
            ),
            "ZDR": (0.0625, -8.0, "dB", "radar_differential_reflectivity_hv"),
            "RHOHV": (0.00333, 0.2017, "unitless", "radar_correlation_coefficient_hv"),
        }.items():
            values = offset + scale * codes.astype(np.float64)
            da = xr.DataArray(
                values,
                dims=("azimuth", "range"),
                attrs={"units": units, "standard_name": std, "long_name": name.title()},
            )
            da.encoding = {"scale_factor": scale, "add_offset": offset, "dtype": "uint8"}
            data[name] = da
        ds = xr.Dataset(
            {
                **data,
                "sweep_mode": ((), "azimuth_surveillance"),
                "sweep_number": ((), i),
                "prt_mode": ((), "fixed"),
                "follow_mode": ((), "none"),
                "sweep_fixed_angle": ((), elev),
            },
            coords={
                "azimuth": (
                    "azimuth",
                    az,
                    {"units": "degrees", "axis": "radial_azimuth_coordinate"},
                ),
                "range": (
                    "range",
                    (2125.0 + 250.0 * np.arange(N_RNG)).astype(np.float32),
                    {
                        "units": "meters",
                        "axis": "radial_range_coordinate",
                        "standard_name": "projection_range_coordinate",
                    },
                ),
                "elevation": ("azimuth", np.full(N_AZ, elev), {"units": "degrees"}),
            },
            attrs={"super_resolution": 11, "sails_cut": False},
        )
        if with_time:
            start = t0 + np.timedelta64(i * 40, "s")
            ds = ds.assign_coords(
                time=("azimuth", start + (np.arange(N_AZ) * 400).astype("timedelta64[ms]"))
            )
        sweeps[f"/sweep_{i}"] = ds
    root = xr.Dataset(
        {
            "volume_number": ((), 0),
            "platform_type": ((), "fixed"),
            "instrument_type": ((), "radar"),
            "time_coverage_start": ((), "2024-05-01T20:00:00Z"),
            "time_coverage_end": ((), "2024-05-01T20:01:30Z"),
            "latitude": ((), 35.333, {"units": "degrees_north", "positive": "up"}),
            "longitude": ((), -97.278, {"units": "degrees_east"}),
            "altitude": ((), 370, {"units": "meters"}),
        },
        attrs={
            "Conventions": "None",
            "version": "None",
            "title": "None",
            "institution": "None",
            "references": "None",
            "source": "None",
            "history": "None",
            "comment": "im/exported using xradar",
            "instrument_name": "KSYN",
            "scan_name": "VCP-12",
            "mpda_vcp": False,
            "number_elevation_cuts": 3,
        },
    )
    tree = xr.DataTree.from_dict({"/": root, **sweeps})
    tree.encoding["source_path"] = "/Users/someone/private/data/KSYN20240501_200000_V06.gz"
    return tree


@pytest.fixture(scope="module")
def volume() -> xr.DataTree:
    return make_cf_volume()


def _hydrometeor_store(tree: xr.DataTree) -> derived.DerivedStore:
    store = derived.DerivedStore(max_bytes=50_000_000)
    store.bind(tree)
    shape = (N_AZ, N_RNG)
    rng = np.random.default_rng(3)
    classes = rng.integers(1, 5, shape).astype(np.float32)
    classes[:, N_RNG // 2 :] = np.nan
    store.put_variable(
        "radarx_hid:hydrometeor_class",
        {0: classes},
        {
            "label": "Hydrometeor class (radarx)",
            "units": "",
            "module": "radarx_hid",
            "module_title": "Hydrometeor classification",
            "field": "Hydrometeor class",
            "params": {"band": "S", "source_file": "/Users/someone/private/x.nc"},
            "computed_at": 1_700_000_000.0,
            "categories": [
                {"value": 1, "label": "Drizzle"},
                {"value": 2, "label": "Rain"},
                {"value": 3, "label": "Ice Crystals"},
                {"value": 4, "label": "Wet Snow"},
            ],
        },
    )
    store.put_variable(
        "pipeline:reflectivity_qc",
        {
            0: rng.uniform(0, 60, shape).astype(np.float32),
            1: rng.uniform(0, 60, shape).astype(np.float32),
        },
        {
            "label": "Reflectivity QC [DBZH]",
            "units": "dBZ",
            "module": "pipeline",
            "module_title": "Pipeline",
            "field": "reflectivity",
            "params": {
                "recipe": "Reflectivity QC",
                "target": "DBZH",
                "steps": [{"step": "snr_gate", "params": {"min_snr": 3.0}}],
            },
            "computed_at": 1_700_000_000.0,
        },
    )
    return store


@pytest.fixture(scope="module")
def volume_with_derived(volume) -> xr.DataTree:
    return derived.tree_with_derived(volume, _hydrometeor_store(volume))


def _assert_clean(path: Path, *, allow_cf: tuple[str, ...] = (), version: str = "auto") -> None:
    """Strict structural checks, then the real CF Checker when it is installed."""
    problems = cf_check.structural_problems(path)
    assert not problems, "\n".join(problems)
    report = cf_check.cfchecker_report(path, version)
    if report is not None:
        errors = report.filtered(allow_cf)
        assert not errors, "\n".join(errors)


def _no_private_paths(path: Path) -> None:
    """Neither the home folder, nor the user name, nor the data folder is embedded."""
    blob = Path(path).read_bytes() if Path(path).is_file() else b""
    for needle in (HOME.encode(), b"/Users/someone", b"/private/var/folders"):
        assert needle not in blob, f"{needle!r} found in {path}"


# ---------------------------------------------------------------------------
# provenance.py
# ---------------------------------------------------------------------------


class TestProvenance:
    def test_version_from_env_and_fallback(self, monkeypatch):
        provenance.software_version.cache_clear()
        monkeypatch.setenv("XRADAR_DESKTOP_VERSION", "9.8.7")
        assert provenance.software_version() == "9.8.7"
        assert provenance.software_string() == "radarx-desktop 9.8.7"
        provenance.software_version.cache_clear()
        monkeypatch.delenv("XRADAR_DESKTOP_VERSION")
        monkeypatch.setattr(provenance, "_package_json_version", lambda: None)
        assert provenance.software_version()  # package metadata or "unknown", never an exception
        provenance.software_version.cache_clear()

    def test_package_json_is_the_version_source(self):
        provenance.software_version.cache_clear()
        want = json.loads((Path(__file__).resolve().parents[2] / "package.json").read_text())[
            "version"
        ]
        assert provenance.software_version() == want

    def test_libraries_and_time(self):
        libs = provenance.library_versions()
        assert {"python", "xarray", "xradar", "numpy"} <= set(libs)
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", provenance.utc_stamp())

    @pytest.mark.parametrize(
        ("raw", "want"),
        [
            ("/Users/me/data/x.nc", "x.nc"),
            ("C:\\Users\\me\\x.nc", "x.nc"),
            ("~/data/x.nc", "x.nc"),
            ("opened /Users/me/a b/x.nc ok", "x.nc"),
            ("no path here", "no path here"),
        ],
    )
    def test_redact_path(self, raw, want):
        assert want in provenance.redact_path(raw)
        assert "/Users/me" not in provenance.redact_path(raw)

    @pytest.mark.parametrize(
        ("raw", "want"),
        [
            ("s3://noaa-nexrad-level2/2013/05/20/KTLX/KTLX20130520_201643_V06.gz",) * 2,
            ("https://doi.org/10.1175/x", "https://doi.org/10.1175/x"),
            (
                "from https://host.example/data/x.nc (accessed)",
                "from https://host.example/data/x.nc (accessed)",
            ),
            (
                "https://u:secret@host.example/a/b.nc?X-Amz-Signature=abc#frag",
                "https://host.example/a/b.nc",
            ),
            ("file:///Users/me/data/x.nc", "x.nc"),
            ("/Users/me/a.nc then s3://bucket/k/f.gz", "a.nc then s3://bucket/k/f.gz"),
        ],
    )
    def test_redact_path_keeps_urls(self, raw, want):
        """A URL is the origin of the data: it was cut to ``https:x`` and lost its host."""
        assert provenance.redact_path(raw) == want

    @pytest.mark.parametrize(
        ("raw", "want"),
        [
            ("2013-05-20T20:16:43+02:00", "2013-05-20T18:16:43Z"),
            ("2013-05-20 20:16:43-05:00", "2013-05-21T01:16:43Z"),
            ("2013-05-20T20:16:43.5+0530", "2013-05-20T14:46:43Z"),
            ("2013-05-20T00:30:00+01:00", "2013-05-19T23:30:00Z"),
            ("2013-05-20T20:16:43+00:00", "2013-05-20T20:16:43Z"),
            ("2013-05-20T20:16:43Z", "2013-05-20T20:16:43Z"),
        ],
    )
    def test_iso_utc_converts_a_stated_offset(self, raw, want):
        """A time with an offset is converted to UTC; it was returned unchanged as if it were UTC."""
        assert provenance.iso_utc(raw) == want

    def test_history_and_placeholders(self):
        assert provenance.append_history("None", "x") == "x"
        assert provenance.append_history("a", "b") == "a\nb"
        # xradar's own writer turns an empty history into "None: xradar v... export"
        assert (
            provenance.append_history("None: xradar v0.12 export", "b") == "xradar v0.12 export\nb"
        )
        assert provenance.clean_text("None") == "" and provenance.clean_text(None) == ""

    def test_standard_global_attrs(self):
        attrs = provenance.standard_global_attrs(
            title="T", source_files=["/a/b/c.nc", "/a/d.nc"], action="export", params={"p": 1}
        )
        for key in ("title", "institution", "source", "history", "references", "comment"):
            assert key in attrs and attrs[key] != "None"
        assert attrs["source_files"] == "c.nc, d.nc" and "/a/" not in json.dumps(attrs)
        assert attrs["software"].startswith("radarx-desktop ") and attrs["date_created"].endswith(
            "Z"
        )

    def test_csv_and_figure_blocks(self):
        block = provenance.csv_comment_block(
            title="t", columns={"a": "m", "b": ""}, source_files="/x/y.nc", params={"k": "v"}
        )
        assert all(line.startswith("# ") for line in block.splitlines())
        assert "a [m]" in block and "b [1]" in block and "y.nc" in block and "/x/" not in block
        meta = provenance.figure_metadata(
            title="T", source_files="/x/y.nc", variable="DBZH", sweep=0
        )
        assert meta["Source"] == "y.nc" and meta["Software"].startswith("radarx-desktop")


# ---------------------------------------------------------------------------
# CfRadial1 / CfRadial2 NetCDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cf1_path(tmp_path_factory, volume_with_derived) -> Path:
    path = tmp_path_factory.mktemp("cf1") / "vol_cfradial1.nc"
    RadarExporter().export(volume_with_derived, "netcdf", output_path=str(path))
    return path


@pytest.fixture(scope="module")
def cf2_path(tmp_path_factory, volume_with_derived) -> Path:
    path = tmp_path_factory.mktemp("cf2") / "vol_cfradial2.nc"
    RadarExporter().export(volume_with_derived, "cfradial2", output_path=str(path))
    return path


class TestCfRadial1:
    def test_conformance(self, cf1_path):
        _assert_clean(cf1_path)
        _no_private_paths(cf1_path)

    @needs_checker
    def test_real_cf_checker_has_no_errors(self, cf1_path):
        report = cf_check.cfchecker_report(cf1_path)
        assert report is not None and not report.errors, report.errors

    def test_global_attributes(self, cf1_path):
        with netCDF4.Dataset(cf1_path) as d:
            g = {a: d.getncattr(a) for a in d.ncattrs()}
        assert g["Conventions"].startswith("CF-1.")
        assert g["version"].startswith("CF-Radial")
        for key in ("title", "institution", "references", "source", "history", "comment"):
            assert g[key] != "None", key
        assert g["instrument_name"] == "KSYN" and g["scan_name"] == "VCP-12"
        assert g["time_coverage_start"] == "2024-05-01T20:00:00Z"
        assert g["time_coverage_end"].startswith("2024-05-01T20:01:")
        assert g["source_files"] == "KSYN20240501_200000_V06.gz"
        assert g["software"].startswith("radarx-desktop ") and "xradar" in g["libraries"]
        assert "added derived variables" in g["history"] and "export CfRadial1" in g["history"]
        assert g["ray_times_increase"] == "true" and g["platform_is_mobile"] == "false"
        assert g["mpda_vcp"] == 0  # a boolean attribute became an integer

    def test_moments_are_float32_nan_filled_and_compressed(self, cf1_path):
        with netCDF4.Dataset(cf1_path) as d:
            for name in ("DBZH", "VRADH", "ZDR", "RHOHV"):
                v = d[name]
                assert v.dtype == np.float32 and v.dimensions == ("time", "range")
                assert np.isnan(v.getncattr("_FillValue"))
                assert v.filters()["zlib"] is True
            assert (
                d["DBZH"].units == "dBZ" and d["VRADH"].units == "m s-1" and d["RHOHV"].units == "1"
            )
            assert d["DBZH"].standard_name == "equivalent_reflectivity_factor"
            assert d["VRADH"].standard_name == "radial_velocity_of_scatterers_away_from_instrument"
            assert not hasattr(d["ZDR"], "standard_name")  # CfRadial-only names are not CF names
            assert d["RHOHV"].valid_min == 0.0 and d["RHOHV"].valid_max == pytest.approx(1.05)

    def test_time_axis(self, cf1_path):
        with netCDF4.Dataset(cf1_path) as d:
            t = d["time"]
            assert re.fullmatch(r"seconds since 2024-05-01T20:00:00(Z|\+00:00)", t.units)
            assert t.calendar == "standard" and t.dtype == np.float64
            assert "_FillValue" not in t.ncattrs()
            assert float(t[:].min()) == 0.0

    def test_strings_are_character_arrays(self, cf1_path):
        with netCDF4.Dataset(cf1_path) as d:
            for name in (
                "sweep_mode",
                "prt_mode",
                "follow_mode",
                "platform_type",
                "instrument_type",
            ):
                assert d[name].dtype == np.dtype("S1"), name  # no variable-length strings
            assert str(netCDF4.chartostring(d["platform_type"][:])).strip() == "fixed"

    def test_site_is_scalar_with_cf_attributes(self, cf1_path):
        with netCDF4.Dataset(cf1_path) as d:
            assert d["latitude"].dimensions == () and d["latitude"].units == "degrees_north"
            assert "positive" not in d["latitude"].ncattrs()
            assert d["altitude"].units == "m" and d["altitude"].positive == "up"

    def test_derived_variables(self, cf1_path):
        with netCDF4.Dataset(cf1_path) as d:
            hid = d["radarx_hid__hydrometeor_class"]
            assert hid.dtype == np.int8 and list(hid.flag_values) == [1, 2, 3, 4]
            assert hid.flag_meanings == "Drizzle Rain Ice_Crystals Wet_Snow"
            assert hid.flag_values.dtype == hid.dtype
            assert "radarx_hid" in hid.comment and "Hydrometeor classification" in hid.comment
            assert hid.long_name == "Hydrometeor class (radarx)"
            assert "/Users/" not in hid.comment  # absolute paths in parameters are redacted
            qc = d["pipeline__reflectivity_qc"]
            assert qc.units == "dBZ" and not hasattr(qc, "standard_name")
            assert "snr_gate" in qc.comment and "min_snr=3" in qc.comment

    def test_round_trip_values_masks_flags_times(self, cf1_path, volume_with_derived):
        import xradar as xd

        back = xd.io.open_cfradial1_datatree(str(cf1_path))
        names = sorted_sweep_names(back)
        assert len(names) == 3
        src = volume_with_derived
        for i, name in enumerate(names):
            got = back[name].to_dataset()
            want = src[f"sweep_{i}"].to_dataset()
            order = np.argsort(want["time"].values, kind="stable")
            for var in ("DBZH", "VRADH", "ZDR", "RHOHV"):
                expected = want[var].values[order]
                # no-echo codes (-33 dBZ ...) are masked, everything else is unchanged
                masked = masked_values(want[var])[order]
                np.testing.assert_allclose(got[var].values, masked, rtol=1e-6, equal_nan=True)
                assert np.isnan(got[var].values).any()
                if var == "DBZH":
                    assert not (got[var].values == NO_ECHO).any()
                assert np.isfinite(expected).all()
            np.testing.assert_allclose(got["elevation"].values, want["elevation"].values[order])
            # times survive to the millisecond
            _assert_same_times(got["time"].values, want["time"].values[order])
        # flags: classes come back as the codes that went in, fill as NaN
        hid0 = back[names[0]].to_dataset()["radarx_hid__hydrometeor_class"].values
        want0 = src["sweep_0"].to_dataset()
        order = np.argsort(want0["time"].values, kind="stable")
        np.testing.assert_array_equal(hid0, want0["radarx_hid__hydrometeor_class"].values[order])
        assert np.isnan(hid0[:, N_RNG // 2 :]).all()
        # the sweep without a result is all fill
        assert np.isnan(back[names[2]].to_dataset()["radarx_hid__hydrometeor_class"].values).all()
        # the site is a scalar and survives
        root = back.to_dataset()
        assert float(root["latitude"]) == pytest.approx(35.333)
        assert float(root["longitude"]) == pytest.approx(-97.278)

    def test_original_tree_is_untouched(self, volume_with_derived, cf1_path, cf2_path):
        before = volume_with_derived["sweep_0"].to_dataset()["DBZH"].values.copy()
        attrs = volume_with_derived["sweep_1"].to_dataset()["VRADH"].attrs
        assert attrs["units"] == "meters per seconds"  # the exports never edit the live tree
        assert volume_with_derived.attrs["title"] == "None"
        _netcdf_safe_tree(volume_with_derived, mask=True)
        np.testing.assert_array_equal(
            volume_with_derived["sweep_0"].to_dataset()["DBZH"].values, before
        )

    def test_mask_option_off_keeps_raw_values(self, tmp_path, volume):
        path = tmp_path / "raw.nc"
        RadarExporter().export(
            volume, "netcdf", output_path=str(path), options={"mask_no_echo": False}
        )
        with xr.open_dataset(path) as ds:
            assert (ds["DBZH"].values == NO_ECHO).any()
            assert "stored as _FillValue" not in ds.attrs["comment"]

    def test_fallback_to_the_xradar_writer_keeps_the_global_attributes(
        self, tmp_path, volume, monkeypatch
    ):
        def gone(_tree):
            raise AttributeError("renamed in a newer xradar")

        monkeypatch.setattr(cfwrite, "cfradial1_dataset", gone)
        path = tmp_path / "fallback.nc"
        RadarExporter().export(volume, "netcdf", output_path=str(path))
        with netCDF4.Dataset(path) as d:
            assert d.Conventions == "CF-1.8" and d.title != "None"
            assert d.source_files == "KSYN20240501_200000_V06.gz"
            assert "xradar writer" in d.history and d.time_coverage_start == "2024-05-01T20:00:00Z"

    def test_ray_dimension_comes_from_the_time_variable(self, volume):
        # the ray dimension of the first sweep decides how the volume is stacked
        assert cfwrite._ray_dim(volume["sweep_0"].to_dataset()) == "azimuth"


def _assert_same_times(got, want) -> None:
    """Ray times survive the float-seconds round trip to well below a millisecond."""
    diff = (
        np.asarray(got, "datetime64[ns]") - np.asarray(want, "datetime64[ns]")
    ) / np.timedelta64(1, "s")
    assert np.abs(diff).max() < 1e-5


def masked_values(da: xr.DataArray) -> np.ndarray:
    from engine.reader import mask_variable

    return mask_variable(da).values


class TestCfRadial2:
    def test_conformance(self, cf2_path):
        _assert_clean(cf2_path)
        _no_private_paths(cf2_path)

    @needs_checker
    def test_each_group_passes_the_real_checker(self, cf2_path):
        report = cf_check.cfchecker_report(cf2_path)
        assert report is not None and not report.errors, report.errors
        assert any(w for w in [report.version])

    def test_layout_and_attributes(self, cf2_path):
        with netCDF4.Dataset(cf2_path) as d:
            assert d.Conventions == "CF-1.8, WMO CF-1.0, ACDD-1.3" and d.version == "2.0"
            assert d.title != "None" and d.history.count("\n") >= 1
            assert set(d.groups) == {"sweep_0", "sweep_1", "sweep_2"}
            assert list(d["sweep_group_name"][:]) and d["sweep_fixed_angle"].units == "degrees"
            sw = d["sweep_0"]
            assert sw["DBZH"].dimensions == ("time", "range") and sw["DBZH"].dtype == np.float32
            assert sw["time"].units.startswith("seconds since 2024-05-01T20:00:00")
            # the per-sweep time axis uses the volume start so sweeps share an epoch
            assert sw["time"].units == d["sweep_1"]["time"].units
            assert (
                sw["radarx_hid__hydrometeor_class"].dtype == np.int8
            )  # derived survive the layout
            assert sw.getncattr("super_resolution") == 11  # sweep attributes are kept

    def test_round_trip(self, cf2_path, volume_with_derived):
        back = xr.open_datatree(cf2_path, engine="netcdf4")
        for i in range(3):
            got = back[f"sweep_{i}"].to_dataset()
            want = volume_with_derived[f"sweep_{i}"].to_dataset()
            order = np.argsort(want["time"].values, kind="stable")
            np.testing.assert_allclose(
                got["DBZH"].values, masked_values(want["DBZH"])[order], rtol=1e-6, equal_nan=True
            )
            _assert_same_times(got["time"].values, want["time"].values[order])
        assert back.attrs["instrument_name"] == "KSYN"
        classes = back["sweep_0"].to_dataset()["radarx_hid__hydrometeor_class"].values
        assert set(np.unique(classes[np.isfinite(classes)])) == {1.0, 2.0, 3.0, 4.0}

    def test_xradar_reads_the_file_back(self, cf2_path):
        import xradar as xd

        back = (
            xd.io.open_cfradial2_datatree(str(cf2_path))
            if hasattr(xd.io, "open_cfradial2_datatree")
            else xr.open_datatree(cf2_path)
        )
        assert "sweep_0" in back.children


# ---------------------------------------------------------------------------
# Zarr
# ---------------------------------------------------------------------------


class TestZarr:
    @pytest.fixture(scope="class")
    def store(self, tmp_path_factory, volume_with_derived) -> Path:
        path = tmp_path_factory.mktemp("zarr") / "vol.zarr"
        RadarExporter().export(volume_with_derived, "zarr", output_path=str(path))
        return path

    def test_layout_and_consolidated_metadata(self, store):
        assert (store / ".zmetadata").is_file(), "consolidated metadata (one read opens the tree)"
        meta = json.loads((store / ".zmetadata").read_text())["metadata"]
        assert meta[".zattrs"]["Conventions"].startswith("CF-1.8")
        assert "sweep_0/DBZH/.zarray" in meta
        arr = meta["sweep_0/DBZH/.zarray"]
        assert arr["dtype"] == "<f4" and arr["chunks"] == [N_AZ, N_RNG]
        assert meta["sweep_0/DBZH/.zattrs"]["_ARRAY_DIMENSIONS"] == ["time", "range"]

    def test_round_trip_and_attributes(self, store, volume_with_derived):
        back = xr.open_datatree(store, engine="zarr", consolidated=True)
        assert back.attrs["history"].count("\n") >= 1 and "None" not in back.attrs["title"]
        got = back["sweep_0"].to_dataset()
        want = volume_with_derived["sweep_0"].to_dataset()
        order = np.argsort(want["time"].values, kind="stable")
        np.testing.assert_allclose(
            got["DBZH"].values, masked_values(want["DBZH"])[order], rtol=1e-6, equal_nan=True
        )
        assert got["DBZH"].attrs["units"] == "dBZ"
        hid = got["radarx_hid__hydrometeor_class"]
        assert (
            list(hid.attrs["flag_values"]) == [1, 2, 3, 4] and "Rain" in hid.attrs["flag_meanings"]
        )
        _assert_same_times(got["time"].values, want["time"].values[order])

    def test_no_private_paths(self, store):
        for f in store.rglob("*"):
            if f.is_file() and f.stat().st_size < 1_000_000 and f.name.startswith((".", "zarr")):
                data = f.read_bytes()
                assert HOME.encode() not in data and b"/Users/someone" not in data

    def test_zarr_v3_option(self, tmp_path, volume):
        path = tmp_path / "v3.zarr"
        RadarExporter().export(volume, "zarr", output_path=str(path), options={"zarr_format": 3})
        assert (path / "zarr.json").is_file()
        assert "sweep_0" in xr.open_datatree(path, engine="zarr", consolidated=False).children


# ---------------------------------------------------------------------------
# CSV, figures, GIF
# ---------------------------------------------------------------------------


class TestCsv:
    def test_header_block_and_values(self, tmp_path, volume):
        out = tmp_path / "sweep.csv"
        RadarExporter().export(volume, "csv", output_path=str(out), variable="DBZH", sweep=0)
        text = out.read_text(encoding="utf-8")
        head = [line for line in text.splitlines() if line.startswith("# ")]
        body = [line for line in text.splitlines() if not line.startswith("# ")]
        assert text.startswith("# ") and body[0] == "azimuth_deg,range_m,DBZH"
        joined = "\n".join(head)
        assert "azimuth_deg [degrees clockwise from north]" in joined and "DBZH [dBZ]" in joined
        assert "range_m [m]" in joined
        assert re.search(r"# created: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", joined)
        assert re.search(r"# data_time: 2024-05-01T20:00:\d{2}Z", joined)
        assert "# software: radarx-desktop" in joined and "KSYN20240501_200000_V06.gz" in joined
        assert "# missing_values:" in joined and "UTC" in joined
        assert HOME not in text and "/Users/someone" not in text
        import pandas as pd

        df = pd.read_csv(out, comment="#")
        assert list(df.columns) == ["azimuth_deg", "range_m", "DBZH"]
        assert len(df) > 0 and (df["DBZH"] > NO_ECHO).all()  # no-echo gates are not rows
        assert all(len(f.split(".")[-1]) == 4 for f in body[1:50] if "." in f.split(",")[2])  # %.4f


class TestFigures:
    @pytest.mark.parametrize("fmt", ["png", "pdf", "svg"])
    def test_metadata_embedded_without_private_paths(self, tmp_path, volume, fmt):
        out = tmp_path / f"fig.{fmt}"
        RadarExporter().export(volume, fmt, dpi=72, output_path=str(out), variable="DBZH", sweep=0)
        blob = out.read_bytes()
        assert HOME.encode() not in blob and b"/Users/someone" not in blob
        if fmt == "png":
            from PIL import Image

            info = Image.open(io.BytesIO(blob)).text
            assert info["Software"].startswith("radarx-desktop ")
            assert info["Source"] == "KSYN20240501_200000_V06.gz"
            assert "DBZH" in info["Title"] and "UTC" in info["Title"]
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", info["Creation Time"])
            assert "xradar" in info["Comment"] and "dpi=72" in info["Comment"]
        elif fmt == "pdf":
            assert b"radarx-desktop" in blob and b"/Creator" in blob and b"/Keywords" in blob
            assert b"KSYN20240501_200000_V06.gz" in blob
        else:
            doc = xml.dom.minidom.parseString(blob)
            meta = doc.getElementsByTagName("metadata")
            assert meta and "radarx-desktop" in meta[0].toxml()
            assert "KSYN20240501_200000_V06.gz" in meta[0].toxml()

    def test_gif_comment(self, tmp_path, volume):
        out = tmp_path / "anim.gif"
        RadarExporter().export_animation(
            volume, variable="DBZH", sweeps=[0, 1], output_path=str(out), dpi=72, fig_size=3.0
        )
        from PIL import Image

        comment = Image.open(out).info.get("comment", b"").decode()
        assert "radarx-desktop" in comment and HOME not in comment
        assert "KSYN20240501_200000_V06.gz" in comment and len(comment) <= 255


# ---------------------------------------------------------------------------
# Pipeline recipes and scripts
# ---------------------------------------------------------------------------


class TestRecipeFiles:
    def test_saved_recipe_is_versioned_and_loads_back(self, tmp_path, monkeypatch):
        from engine import pipeline

        monkeypatch.setattr(pipeline, "recipes_dir", lambda: tmp_path)
        recipe = pipeline.save_recipe(
            {"name": "My QC", "target": "DBZH", "steps": [{"step_id": "snr_gate", "params": {}}]}
        )
        doc = json.loads((tmp_path / f"{pipeline._path_for(recipe.name).name}").read_text())
        assert (
            doc["schema"] == "xradar-desktop/recipe" and doc["version"] == pipeline.RECIPE_VERSION
        )
        assert doc["created_with"]["software"] == "radarx-desktop"
        assert doc["created_with"]["software_version"] == provenance.software_version()
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", doc["created_with"]["created"])
        assert pipeline.parse_recipe(doc).name == "My QC"  # the extra keys are ignored on load

    def test_newer_or_foreign_recipes_are_refused(self):
        from engine import pipeline
        from engine.plugins import ModuleError

        base = {"name": "x", "target": "DBZH", "steps": [{"step_id": "snr_gate", "params": {}}]}
        with pytest.raises(ModuleError, match="newer"):
            pipeline.parse_recipe({**base, "version": pipeline.RECIPE_VERSION + 1})
        with pytest.raises(ModuleError, match="Not a radarx-desktop recipe"):
            pipeline.parse_recipe({**base, "schema": "something/else"})

    def test_script_header_and_cf_output(self, tmp_path):
        from engine import pipeline

        recipe = pipeline.parse_recipe(
            {"name": "Clean", "target": "DBZH", "steps": [{"step_id": "snr_gate", "params": {}}]}
        )
        text = pipeline.recipe_to_script(recipe)
        assert f"Generated by radarx-desktop {provenance.software_version()}" in text
        assert "recipe format" in text and "python " in text
        assert 'Conventions": "CF-1.8"' in text and "os.path.basename(src)" in text
        compile(text, "<script>", "exec")
        assert HOME not in text


# ---------------------------------------------------------------------------
# Mosaic (needs radarx to build one)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mosaic_records():
    if __import__("importlib").util.find_spec("radarx") is None:
        pytest.skip("radarx not installed")
    cache = mosaic.MosaicCache()
    lo = make_reader(make_volume(*A, flat(20.0), name="LOW"))
    hi = make_reader(make_volume(*B, flat(40.0), name="HIGH"))
    return {
        "cappi": mosaic.build_mosaic(
            [("a", lo), ("b", hi)], "DBZH", "cappi", GRID, height_m=2000, cache=cache
        ),
        "composite": mosaic.build_mosaic(
            [("a", lo), ("b", hi)], "DBZH", "composite", GRID, cache=cache
        ),
        "echo_top": mosaic.build_mosaic(
            [("a", lo), ("b", hi)], "DBZH", "echo_top", GRID, threshold_dbz=10, cache=cache
        ),
    }


@needs_radarx
class TestMosaicFile:
    @pytest.mark.parametrize("product", ["cappi", "composite", "echo_top"])
    def test_conformance(self, tmp_path, mosaic_records, product):
        path = tmp_path / f"{product}.nc"
        mosaic.write_netcdf(mosaic_records[product], path)
        _assert_clean(path, version="1.8")
        _no_private_paths(path)

    def test_grid_mapping_coordinates_and_time(self, tmp_path, mosaic_records):
        rec = mosaic_records["cappi"]
        path = tmp_path / "m.nc"
        mosaic.write_netcdf(rec, path)
        with netCDF4.Dataset(path) as d:
            assert d.Conventions == "CF-1.10" and "history" in d.ncattrs()
            for key in (
                "merge_average",
                "merge_range_scale_km",
                "radarx_version",
                "radars",
                "software",
            ):
                assert key in d.ncattrs(), key
            v = d["DBZH_cappi"]
            assert v.grid_mapping == "crs" and v.dimensions == ("y", "x")
            assert v.standard_name == "equivalent_reflectivity_factor"
            crs = d["crs"]
            assert (
                crs.grid_mapping_name == "mercator" and crs.semi_major_axis == crs.semi_minor_axis
            )
            import pyproj

            assert pyproj.CRS.from_wkt(crs.crs_wkt).to_epsg() == 3857
            assert d["x"].standard_name == "projection_x_coordinate" and d["x"].axis == "X"
            assert d["y"].units == "m" and d["lat"].dimensions == ("y", "x")
            assert d["z"].positive == "up" and float(d["z"][...]) == 2000.0
            assert d["time"].bounds == "time_bounds" and d["time_bounds"].dimensions == ("bnds",)
            aeqd = d["crs_working_grid"]
            assert aeqd.grid_mapping_name == "azimuthal_equidistant"
            assert aeqd.latitude_of_projection_origin == pytest.approx(rec.origin[0])
            assert d["radar_volume_time"].units.startswith("seconds since 2024-05-01T20:00:")
        with xr.open_dataset(path) as ds:
            np.testing.assert_allclose(ds["DBZH_cappi"].values[::-1], rec.image, equal_nan=True)
            assert list(ds["radar_name"].values) == ["LOW", "HIGH"]
            assert pd_ts(ds["time"].values).startswith("2024-05-01T20:00")
            # the x/y axes are the Web-Mercator metres of the pixel centres
            x = ds["x"].values
            assert np.allclose(np.diff(x), np.diff(x)[0])
            lon = ds["lon"].values[0]
            np.testing.assert_allclose(np.degrees(x / mosaic.MERC_R), lon)

    def test_world_file_and_prj(self, mosaic_records):
        import pyproj

        rec = mosaic_records["cappi"]
        wf = [float(v) for v in mosaic.world_file(rec).split()]
        west = rec.bbox[0]
        xw = mosaic.MERC_R * np.radians(west)
        assert wf[4] == pytest.approx(xw + 0.5 * wf[0], abs=1e-3)  # centre of the first pixel
        assert wf[0] == pytest.approx(-wf[3] * 1.0, rel=1e-6) or wf[0] > 0
        crs = pyproj.CRS.from_wkt(mosaic.projection_wkt())
        assert crs.equals(pyproj.CRS.from_epsg(3857))
        assert pyproj.CRS.from_wkt(mosaic.projection_wkt()).is_projected

    def test_png_has_metadata_and_same_pixels(self, mosaic_records):
        from PIL import Image

        rec = mosaic_records["cappi"]
        data = mosaic.png_bytes(rec)
        with Image.open(io.BytesIO(data)) as im, Image.open(io.BytesIO(rec.png)) as ref:
            assert im.text["Software"].startswith("radarx-desktop ")
            assert "LOW" in im.text["Comment"] and "EPSG:3857" in im.text["Comment"]
            np.testing.assert_array_equal(
                np.asarray(im.convert("RGBA")), np.asarray(ref.convert("RGBA"))
            )
        assert HOME.encode() not in data


def pd_ts(value) -> str:
    return str(np.datetime_as_string(np.asarray(value).astype("datetime64[s]"), unit="s"))


# ---------------------------------------------------------------------------
# Multi-Doppler winds
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def wind_records():
    if __import__("importlib").util.find_spec("radarx") is None:
        pytest.skip("radarx not installed")
    out = {}
    for method in ("analytic", "variational"):
        out[method] = mdop.run_multidoppler(
            pair(vortex_wind),
            WIND_GRID,
            {"method": method, "background": {"mode": "uniform", "u": 3.0, "v": 1.0}},
            cache=None,
        )
    return out


@needs_radarx
class TestWindFile:
    @pytest.mark.parametrize("method", ["analytic", "variational"])
    def test_conformance(self, tmp_path, wind_records, method):
        path = tmp_path / f"{method}.nc"
        mdop.write_netcdf(wind_records[method], path)
        _assert_clean(path)
        _no_private_paths(path)

    def test_standard_names_units_and_si_scaling(self, tmp_path, wind_records):
        rec = wind_records["variational"]
        path = tmp_path / "w.nc"
        mdop.write_netcdf(rec, path)
        with netCDF4.Dataset(path) as d:
            names = {n: getattr(v, "standard_name", None) for n, v in d.variables.items()}
            assert names["u"] == "x_wind" and names["v"] == "y_wind"
            assert (
                names["eastward_wind"] == "eastward_wind"
                and names["northward_wind"] == "northward_wind"
            )
            assert names["w"] == "upward_air_velocity" and names["speed"] == "wind_speed"
            assert names["divergence"] == "divergence_of_wind"
            assert names["vorticity"] == "atmosphere_upward_relative_vorticity"
            assert d["divergence"].units == "s-1" and d["u"].units == "m s-1"
            assert d["z"].positive == "up" and d["crs"].grid_mapping_name == "azimuthal_equidistant"
            assert d["lat"].dimensions == ("y", "x")
        with xr.open_dataset(path) as ds:
            i = mdop.FIELD_ORDER.index
            np.testing.assert_allclose(ds["u"].values, rec.fields[i("u")], equal_nan=True)
            np.testing.assert_allclose(ds["w"].values, rec.fields[i("w")], equal_nan=True)
            np.testing.assert_allclose(
                ds["divergence"].values,
                rec.fields[i("divergence")] * 1e-3,
                rtol=1e-5,
                equal_nan=True,
            )
            # true east/north are the grid components rotated by the (tiny) meridian convergence
            np.testing.assert_allclose(
                np.hypot(ds["eastward_wind"].values, ds["northward_wind"].values),
                ds["speed"].values,
                rtol=1e-4,
                atol=1e-4,
                equal_nan=True,
            )
            origin_col = np.argmin(np.abs(ds["x"].values))
            np.testing.assert_allclose(
                ds["eastward_wind"].values[:, :, origin_col],
                ds["u"].values[:, :, origin_col],
                atol=1e-3,
                equal_nan=True,
            )

    def test_global_attributes_describe_the_method(self, tmp_path, wind_records):
        rec = wind_records["variational"]
        path = tmp_path / "w.nc"
        mdop.write_netcdf(rec, path)
        with netCDF4.Dataset(path) as d:
            g = {a: d.getncattr(a) for a in d.ncattrs()}
            assert g["method"] == "variational"
            assert g["boundary_condition"] == rec.summary["boundary"]
            assert "uniform" in g["background_wind"] and "u=3" in g["background_wind"]
            assert g["min_crossing_angle_deg"] == 30.0 and g["observation_error_ms"] == 1.0
            assert (
                g["valid_cells"] == rec.summary["valid_cells"]
                and g["total_cells"] > g["valid_cells"]
            )
            assert g["radars"] == "WESTR, EASTR" and g["radarx_version"]
            assert g["software"].startswith("radarx-desktop") and "variational" in g["history"]
            assert "de-aliasing" in g["comment"] and g["Conventions"] == "CF-1.8"
            assert list(
                d["radar_name"][:].astype("S").tobytes().decode().replace("\x00", " ").split()
            ) == ["WESTR", "EASTR"]
            assert d["radar_volume_time"].units.startswith("seconds since 2024-05-01")
            assert d["radar_latitude"].standard_name == "latitude"


# ---------------------------------------------------------------------------
# Real data (read-only)
# ---------------------------------------------------------------------------


def _first_sweeps(tree: xr.DataTree, n: int) -> xr.DataTree:
    """The volume cut down to its first *n* sweeps (keeps the real-data checks fast)."""
    out = tree.copy(deep=False)
    for name in sorted_sweep_names(out)[n:]:
        del out[name]
    return out


def _open(path: Path) -> RadarReader:
    reader = RadarReader()
    reader.open_file(str(path))
    return reader


@pytest.mark.skipif(not KTLX.exists(), reason="KTLX example file not available")
class TestRealNexrad:
    @pytest.fixture(scope="class")
    def reader(self):
        return _open(KTLX)

    @pytest.mark.parametrize("fmt", ["netcdf", "cfradial2"])
    def test_cf_conformance_and_masking(self, tmp_path, reader, fmt):
        path = tmp_path / f"{fmt}.nc"
        RadarExporter().export(_first_sweeps(reader.datatree, 4), fmt, output_path=str(path))
        _assert_clean(path)
        _no_private_paths(path)
        with netCDF4.Dataset(path) as d:
            assert d.getncattr("source_files") == "KTLX20130520_201643_V06.gz"
            assert d.getncattr("instrument_name") == "KTLX"
            dbz = d["DBZH"] if fmt == "netcdf" else d["sweep_0"]["DBZH"]
            data = dbz[:]
            assert data.dtype == np.float32 and np.ma.is_masked(data)
            assert float(np.ma.min(data)) > NO_ECHO  # the -33 dBZ "no echo" code is not data
            assert d.getncattr("time_coverage_start") == "2013-05-20T20:16:43Z"

    def test_zarr_and_csv_and_png(self, tmp_path, reader):
        ex = RadarExporter()
        z = ex.export(reader.datatree, "zarr", output_path=str(tmp_path / "k.zarr"))
        assert (Path(z) / ".zmetadata").is_file()
        assert (
            xr.open_datatree(z, engine="zarr", consolidated=True).attrs["instrument_name"] == "KTLX"
        )
        csv = Path(
            ex.export(
                reader.datatree,
                "csv",
                output_path=str(tmp_path / "k.csv"),
                variable="DBZH",
                sweep=0,
            )
        )
        assert csv.read_text().startswith("# title:") and HOME not in csv.read_text()
        png = Path(
            ex.export(
                reader.datatree, "png", dpi=72, output_path=str(tmp_path / "k.png"), variable="DBZH"
            )
        )
        from PIL import Image

        assert Image.open(png).text["Source"] == "KTLX20130520_201643_V06.gz"


@pytest.mark.skipif(
    not (CPOL_PPI.exists() and CPOL_RHI.exists()), reason="CPOL examples not available"
)
class TestRealCpol:
    @pytest.mark.parametrize("path", [CPOL_PPI, CPOL_RHI], ids=["ppi", "rhi"])
    @pytest.mark.parametrize("fmt", ["netcdf", "cfradial2"])
    def test_sector_and_rhi_volumes(self, tmp_path, path, fmt):
        reader = _open(path)
        tree = _first_sweeps(reader.datatree, 4)
        out = tmp_path / f"{fmt}.nc"
        RadarExporter().export(tree, fmt, output_path=str(out))
        _assert_clean(out, allow_cf=DUPLICATE_RAY_TIMES)
        with netCDF4.Dataset(out) as d:
            # dangling references of the source (grid_mapping -> missing variable) are gone
            assert d.getncattr("source_files") == path.name
            assert d.Conventions.startswith("CF-1.")
        back = xr.open_datatree(out, engine="netcdf4") if fmt == "cfradial2" else None
        if back is not None:
            assert len([c for c in back.children if c.startswith("sweep")]) == 4


MAYFIELD_FILES = (
    {
        site: sorted((MAYFIELD / site).glob(f"{site}*_V06"))[:1]
        for site in ("KHPX", "KNQA", "KPAH", "KVWX")
    }
    if MAYFIELD.exists()
    else {}
)


@needs_radarx
@pytest.mark.skipif(
    not MAYFIELD_FILES or not all(MAYFIELD_FILES.values()), reason="Mayfield radars not available"
)
class TestRealMayfield:
    @pytest.fixture(scope="class")
    def readers(self):
        return [(site, _open(files[0])) for site, files in MAYFIELD_FILES.items()]

    def test_mosaic_of_four_radars(self, tmp_path, readers):
        rec = mosaic.build_mosaic(
            readers, "DBZH", "composite", {"resolution_m": 3000}, cache=mosaic.MosaicCache()
        )
        path = tmp_path / "mayfield_mosaic.nc"
        mosaic.write_netcdf(rec, path)
        _assert_clean(path, version="1.8")
        with xr.open_dataset(path) as ds:
            assert list(ds["radar_name"].values) == ["KHPX", "KNQA", "KPAH", "KVWX"]
            np.testing.assert_allclose(ds["DBZH_composite"].values[::-1], rec.image, equal_nan=True)

    def test_winds_of_two_radars(self, tmp_path, readers):
        pair_ = [readers[0], readers[2]]  # KHPX + KPAH
        try:
            rec = mdop.run_multidoppler(
                pair_,
                {"dx_m": 3000, "dz_m": 1000, "z_max_m": 6000, "max_range_km": 150},
                {"method": "analytic"},
                cache=None,
            )
        except mdop.MultiDopplerError as exc:  # the pair may not overlap enough
            pytest.skip(str(exc))
        path = tmp_path / "mayfield_winds.nc"
        mdop.write_netcdf(rec, path)
        _assert_clean(path)
        with netCDF4.Dataset(path) as d:
            assert d.getncattr("radars") == "KHPX, KPAH"
            assert d["u"].units == "m s-1"
