"""Value masks: the rules (same as the viewer's stores/valueMask.ts) and their use in exports."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from engine import valuemask
from engine.exporter import RadarExporter, render_publication_figure


def mk(mode: str, lo: float | None = None, hi: float | None = None, inclusive: bool = False) -> dict:
    return {"mode": mode, "min": lo, "max": hi, "inclusive": inclusive}


# ---------------------------------------------------------------------------
# The rules. The same table is asserted by src/lib/utils/valueMask.test.mjs.
# ---------------------------------------------------------------------------

CASES = [
    # (mask, value, hidden)
    (mk("below", 0), -1, True),
    (mk("below", 0), 0, False),
    (mk("below", 0, None, True), 0, True),
    (mk("above", None, 10), 10, False),
    (mk("above", None, 10), 10.5, True),
    (mk("above", None, 10, True), 10, True),
    (mk("between", -2, 2), 0, True),
    (mk("between", -2, 2), 2, False),
    (mk("between", 2, -2), 0, True),  # swapped limits
    (mk("between", -2, 2, True), 2, True),
    (mk("outside", -2, 6), 7, True),
    (mk("outside", -2, 6), 6, False),
    (mk("outside", -2, 6, True), 6, True),
    (mk("outside", -2, 6), 0, False),
    (mk("off", 0, 1), 0.5, False),
    (mk("below", None), -50, False),  # incomplete: hides nothing
    (mk("between", 1, None), 5, False),
]


@pytest.mark.parametrize(("mask", "value", "expected"), CASES)
def test_rules(mask: dict, value: float, expected: bool) -> None:
    h = valuemask.hidden(np.array([value], dtype=np.float32), mask)
    assert (False if h is None else bool(h[0])) is expected


def test_nan_is_never_masked_and_input_untouched() -> None:
    v = np.array([np.nan, -5, 5], dtype=np.float32)
    out, n = valuemask.apply(v, mk("below", 0))
    assert n == 1
    assert np.isnan(out[0]) and np.isnan(out[1]) and out[2] == 5
    assert v[1] == -5  # a copy
    h = valuemask.hidden(v, mk("outside", 0, 1))
    assert h is not None and not h[0]


def test_float32_domain_comparison() -> None:
    """The float32 form of a limit is not hidden by an exclusive mask (as in the shader)."""
    v = np.array([0.7], dtype=np.float32)
    out, n = valuemask.apply(v, mk("below", 0.7))
    assert n == 0 and out[0] == v[0]
    out, n = valuemask.apply(v, mk("below", 0.7, None, True))
    assert n == 1


def test_integer_input_becomes_float() -> None:
    out, n = valuemask.apply(np.array([1, 5, 9]), mk("above", None, 4))
    assert n == 2 and out.dtype.kind == "f" and out[0] == 1


def test_normalize_and_pick() -> None:
    assert valuemask.normalize(None) is None
    assert valuemask.normalize({"mode": "nonsense"}) is None
    assert valuemask.normalize({"mode": "below", "min": "x"}) is None
    assert valuemask.normalize({"mode": "below", "min": True}) is None
    assert valuemask.normalize(mk("below", float("nan"))) is None
    assert valuemask.normalize(mk("below", 1))["inclusive"] is False
    opts = {"value_masks": {"DBZH": mk("below", 5)}, "value_mask": mk("above", None, 1)}
    assert valuemask.pick(opts, "DBZH")["mode"] == "below"
    assert valuemask.pick(opts, "VRADH")["mode"] == "above"  # single mask, no variable named
    assert valuemask.pick({**opts, "value_mask_variable": "ZDR"}, "VRADH") is None
    assert valuemask.pick(None, "DBZH") is None


def test_describe_matches_viewer_wording() -> None:
    assert valuemask.describe(mk("below", 0), "dBZ") == "hide below 0 dBZ"
    assert valuemask.describe(mk("below", 0.7, None, True)) == "hide 0.7 and below"
    assert valuemask.describe(mk("between", 2, -2), "m/s") == "hide between -2 and 2 m/s"
    assert valuemask.describe(mk("outside", -2, 6, True), "dB") == (
        "hide outside -2 to 6 dB, and those limits"
    )
    assert valuemask.describe(mk("above", None, 8.12345)) == "hide above 8.123"
    assert valuemask.describe(mk("off")) is None


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------


def _hidden_fraction(tree: xr.DataTree, sweep: int, below: float) -> float:
    v = tree[f"sweep_{sweep}"]["DBZH"].values
    return float(np.mean(v < below))


def test_figure_has_footnote_and_hides_gates(synthetic_datatree: xr.DataTree) -> None:
    fig = render_publication_figure(
        synthetic_datatree, "DBZH", 0, dpi=72, value_mask=mk("below", 20), show_title=False
    )
    try:
        texts = [t.get_text() for t in fig.texts]
        assert any(t.startswith("Value mask: hide below 20") for t in texts)
        assert any("gates hidden" in t for t in texts)
        mesh = fig.axes[0].collections[0]
        arr = np.ma.getdata(mesh.get_array()).ravel()
        finite = arr[np.isfinite(arr)]
        assert finite.size and finite.min() >= 20
    finally:
        import matplotlib.pyplot as plt

        plt.close(fig)
    plain = render_publication_figure(synthetic_datatree, "DBZH", 0, dpi=72, show_title=False)
    try:
        assert not plain.texts
    finally:
        import matplotlib.pyplot as plt

        plt.close(plain)


@pytest.mark.parametrize("fmt", ["png", "svg", "pdf"])
def test_image_exports_apply_mask(
    synthetic_datatree: xr.DataTree, tmp_path: Path, fmt: str
) -> None:
    out = tmp_path / f"m.{fmt}"
    path = RadarExporter().export(
        synthetic_datatree,
        fmt=fmt,
        dpi=72,
        output_path=str(out),
        variable="DBZH",
        sweep=0,
        options={"value_masks": {"DBZH": mk("below", 30)}},
    )
    data = Path(path).read_bytes()
    assert len(data) > 1000
    if fmt == "svg":
        assert b"Value mask: hide below 30" in data


def test_csv_omits_masked_gates_and_says_so(synthetic_datatree: xr.DataTree, tmp_path: Path) -> None:
    exp = RadarExporter()
    full = exp.export(synthetic_datatree, fmt="csv", output_path=str(tmp_path / "a.csv"), variable="DBZH", sweep=0)
    masked = exp.export(
        synthetic_datatree,
        fmt="csv",
        output_path=str(tmp_path / "b.csv"),
        variable="DBZH",
        sweep=0,
        options={"value_masks": {"DBZH": mk("below", 40)}},
    )
    import pandas as pd

    a = pd.read_csv(full, comment="#")
    b = pd.read_csv(masked, comment="#")
    assert len(b) < len(a)
    assert b["DBZH"].min() >= 40
    assert (a["DBZH"] >= 40).sum() == len(b)
    header = Path(masked).read_text().split("azimuth_deg,", 1)[0]
    assert "value_mask" in header and "hide below 40" in header
    assert "value_mask" not in Path(full).read_text().split("azimuth_deg,", 1)[0]


def test_volume_exports_mask_only_the_selected_variable(
    synthetic_datatree: xr.DataTree, tmp_path: Path
) -> None:
    """NetCDF / CfRadial2 / Zarr all write ``_netcdf_safe_tree(...)``: check it and that it stores."""
    from engine.exporter import _netcdf_safe_tree, _volume_value_mask

    options = {"value_mask": mk("below", 40), "value_mask_variable": "DBZH"}
    vm, params = _volume_value_mask(options, "DBZH", synthetic_datatree)
    assert vm is not None and vm[0] == "DBZH" and "hide below 40" in vm[2]
    assert "value_mask" in params
    assert _volume_value_mask(options, "VRADH", synthetic_datatree) == (None, {})

    safe = _netcdf_safe_tree(synthetic_datatree, value_mask=vm)
    for i in range(3):
        ds = safe[f"sweep_{i}"].to_dataset()
        dbz = ds["DBZH"].values
        assert np.isnan(dbz).any() and np.nanmin(dbz) >= 40
        assert "hide below 40" in ds["DBZH"].attrs["value_mask"]
        assert not np.isnan(ds["VRADH"].values).any()
        assert "value_mask" not in ds["VRADH"].attrs
    # the reader's live tree is never modified
    assert not np.isnan(synthetic_datatree["sweep_0"]["DBZH"].values).any()
    assert "value_mask" not in synthetic_datatree["sweep_0"]["DBZH"].attrs
    safe["sweep_0"].to_dataset().to_netcdf(tmp_path / "s0.nc", engine="netcdf4")
    with xr.open_dataset(tmp_path / "s0.nc") as back:
        assert np.isnan(back["DBZH"].values).any()


def test_batch_uses_each_variables_own_mask(synthetic_datatree: xr.DataTree, tmp_path: Path) -> None:
    saved = RadarExporter().batch_export(
        synthetic_datatree,
        variables=["DBZH", "VRADH"],
        sweeps=[0],
        fmt="svg",
        dpi=72,
        output_dir=str(tmp_path),
        options={"value_masks": {"DBZH": mk("below", 30)}},
    )
    texts = {Path(p).name: Path(p).read_bytes() for p in saved}
    assert b"Value mask: hide below 30" in texts["DBZH_sweep00.svg"]
    assert b"Value mask" not in texts["VRADH_sweep00.svg"]

