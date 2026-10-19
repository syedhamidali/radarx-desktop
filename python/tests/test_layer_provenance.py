"""Layer provenance: the classes, serialisation, time offsets, wire messages and exports."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import numpy as np
import pytest

import server
from engine import derived, provenance
from engine import layer_provenance as lp
from engine.reader import RadarReader
from tests.conftest import make_synthetic_datatree
from tests.test_server import _binary, _make_handler, _reader_with_tree  # noqa: F401


def test_the_five_classes_each_have_a_definition_and_a_short_label() -> None:
    assert lp.CLASSES == ("observed", "model", "retrieved", "simulated", "stylised")
    assert set(lp.DEFINITIONS) == set(lp.SHORT) == set(lp.CLASSES)
    assert all(len(t) > 40 for t in lp.DEFINITIONS.values())


def test_unknown_class_is_refused() -> None:
    with pytest.raises(ValueError, match="one of"):
        lp.LayerProvenance(kind="measured-ish", source="x")


def test_serialisation_is_json_safe_and_drops_empty_fields() -> None:
    p = lp.LayerProvenance(
        kind="model",
        source="HRRR",
        run_time=datetime(2022, 3, 30, 0, tzinfo=timezone.utc),
        valid_time="2022-03-30 01:00:00",
        parameters={"level": 850, "nan": float("nan"), "path": "/Users/me/x.nc", "none": None},
        references=["Dowell et al. 2022", ""],
    )
    d = p.to_dict()
    assert d["kind"] == "model" and d["run_time"] == "2022-03-30T00:00:00Z"
    assert d["valid_time"] == "2022-03-30T01:00:00Z"
    assert d["parameters"] == {"level": 850, "path": "x.nc"}  # no NaN, no folders
    assert (
        d["references"] == ["Dowell et al. 2022"]
        and "uncertainty" not in d
        and "resolution" not in d
    )
    json.dumps(d, allow_nan=False)
    assert lp.from_dict(d).to_dict() == d  # round trip


def test_time_offset_to_the_radar_scan() -> None:
    p = lp.LayerProvenance(kind="model", source="m", valid_time="2022-03-30T01:00:00Z")
    assert p.time_offset_s("2022-03-30T00:48:00Z") == 720.0  # model valid 12 min after the scan
    assert p.time_offset_s("2022-03-30T01:30:00Z") == -1800.0
    assert (
        p.time_offset_s("") is None
        and lp.LayerProvenance(kind="model", source="m").time_offset_s("2022-03-30T00:00:00Z")
        is None
    )
    assert lp.time_offset_s("garbage", "2022-03-30T00:00:00Z") is None


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(None, "unknown"), (0, "same time"), (20, "same time"), (-300, "-5 min"), (720, "+12 min"),
     (3900, "+1 h 05 min"), (-7200, "-2 h"), (86400 * 3, "+3 d")],
)  # fmt: skip
def test_offset_text(seconds, text) -> None:
    assert lp.format_offset(seconds) == text


def test_builders_choose_the_right_class() -> None:
    assert lp.observed_radar(radar="KTLX", scan_time="2013-05-20T20:16:43Z").kind == "observed"
    assert lp.observed_radar(radar="x", simulated=True).kind == "simulated"
    d = lp.derived_field(name="HID", module="radarx_hid", params={"fz": 4000.0}, inputs=["DBZH"])
    assert d.kind == "retrieved" and d.inputs == ["DBZH"] and d.parameters == {"fz": 4000.0}
    assert lp.mosaic(radars=["KHPX", "KNQA"], valid_time="", method="m").kind == "retrieved"
    assert lp.winds(radars=["a", "b"], valid_time="", method="m").kind == "retrieved"
    assert lp.storm_cells(valid_time="").kind == "retrieved"
    assert lp.cross_section(valid_time="", variable="DBZH").kind == "observed"
    assert "interpolated" in lp.cross_section(valid_time="").method
    assert lp.volume_3d(valid_time="").kind == "observed"
    snd = lp.sounding(station="KJAN", launch_time="2022-03-30T00:00:00Z", source="IEM")
    assert snd.kind == "observed" and "drifts" in snd.uncertainty
    assert lp.for_variable("DBZH", radar="KTLX").kind == "observed"
    assert lp.for_variable("radarx_hid:hydrometeor_class").kind == "retrieved"
    assert lp.for_variable("radarx_hid:hydrometeor_class").source.startswith(
        "xradar-desktop module radarx_hid"
    )


def test_unknown_values_stay_unknown() -> None:
    d = lp.observed_radar(radar="", scan_time="").to_dict()
    assert d["source"] == "radar file" and "valid_time" not in d  # no invented time


def test_export_attributes_say_what_kind_of_numbers_these_are() -> None:
    attrs = lp.attrs(
        lp.derived_field(
            name="HID", module="radarx_hid", params={"a": 1}, valid_time="2022-03-30T00:00:00Z"
        )
    )
    assert attrs["data_class"] == "retrieved" and attrs["data_valid_time"] == "2022-03-30T00:00:00Z"
    assert attrs["data_parameters"] == "a=1" and "algorithm" in attrs["data_class_definition"]
    assert lp.attrs(None) == {}
    ga = provenance.standard_global_attrs(
        title="t", layer=lp.observed_radar(radar="KTLX").to_dict()
    )
    assert ga["data_class"] == "observed" and ga["title"] == "t"
    block = provenance.csv_comment_block(title="t", layer=lp.observed_radar(radar="KTLX").to_dict())
    assert "# data_class: observed" in block and "# data_source: KTLX" in block
    fig = provenance.figure_metadata(title="t", layer=lp.observed_radar(radar="KTLX").to_dict())
    assert "data class: observed" in fig["Comment"]


def test_derived_variable_attributes_in_exported_files_carry_the_class() -> None:
    tree = make_synthetic_datatree(n_azimuth=8, n_range=9)
    store = derived.DerivedStore()
    for i, _name in enumerate(tree.children):
        store.put(
            i,
            "radarx_hid:cls",
            np.zeros((8, 9), dtype=np.float32),
            {
                "label": "Class",
                "units": "1",
                "module": "radarx_hid",
                "field": "cls",
                "params": {"x": 1},
            },
        )
    out = derived.tree_with_derived(tree, store)
    first = next(iter(out.children.values())).to_dataset()
    attrs = first["radarx_hid__cls"].attrs
    assert attrs["data_class"] == "retrieved" and "radarx_hid" in attrs["data_source"]
    assert "Derived by" in attrs["comment"]  # the existing comment is kept


# -- what the sidecar sends -----------------------------------------------------------------------


def test_describe_reader_reads_scan_time_and_gate_spacing() -> None:
    reader = _reader_with_tree(make_synthetic_datatree(n_azimuth=20, n_range=30))
    ds = reader.get_sweep_dataset(0, mask=False)
    ds_time = np.datetime64("2022-03-30T00:05:00")
    reader._dtree["sweep_0"].dataset = ds.assign(time=("azimuth", np.full(20, ds_time)))
    prov = lp.describe_reader(
        reader,
        {"attributes": {"instrument_name": "KTST"}, "format": "synthetic", "sweeps": [1, 2, 3]},
    )
    assert (
        prov["kind"] == "observed"
        and prov["source"] == "KTST"
        and prov["valid_time"] == "2022-03-30T00:05:00Z"
    )
    assert "gate spacing" in prov["resolution"] and "3 sweeps" in prov["resolution"]
    assert lp.reader_context(reader) == {"source": "KTST", "scan_time": "2022-03-30T00:05:00Z"}


def test_describe_reader_never_raises() -> None:
    prov = lp.describe_reader(object(), None)  # nothing to read
    assert prov["kind"] == "observed" and prov["source"] == "radar file"


@pytest.mark.asyncio
async def test_sweep_data_header_carries_provenance_observed_and_derived() -> None:
    reader = _reader_with_tree(make_synthetic_datatree(n_azimuth=20, n_range=30))
    lp.describe_reader(reader, {"attributes": {"instrument_name": "KTST"}, "sweeps": [1, 2, 3]})
    file_id = server._register_reader(reader)
    try:
        handler, _ = _make_handler()
        await handler.handle_message(
            json.dumps(
                {"type": "get_sweep_data", "variable": "DBZH", "sweep": 0, "file_id": file_id}
            )
        )
        prov = handler._sent[0]["provenance"]
        assert prov["kind"] == "observed" and prov["source"] == "KTST"
        # a derived variable: the header says it was computed, by which module, with which settings
        store = derived.store_for(reader)
        for i in range(3):
            store.put(i, "demo:field", np.ones((20, 30), dtype=np.float32), {
                "label": "Demo", "units": "1", "module": "demo", "module_title": "Demo module",
                "field": "field", "params": {"k": 2}, "computed_at": 1_700_000_000.0,
            })  # fmt: skip
        handler2, _ = _make_handler()
        await handler2.handle_message(
            json.dumps(
                {"type": "get_sweep_data", "variable": "demo:field", "sweep": 0, "file_id": file_id}
            )
        )
        h = handler2._sent[0]
        assert h["derived"] is True and h["provenance"]["kind"] == "retrieved"
        assert (
            h["provenance"]["parameters"] == {"k": 2}
            and h["provenance"]["run_time"] == "2023-11-14T22:13:20Z"
        )
        handler3, _ = _make_handler()
        await handler3.handle_message(json.dumps({"type": "list_derived", "file_id": file_id}))
        (var,) = handler3._sent[0]["variables"]
        assert var["provenance"]["kind"] == "retrieved" and var["name"] == "demo:field"
    finally:
        server._file_readers.pop(file_id, None)


@pytest.mark.asyncio
async def test_volume_and_cross_section_replies_carry_provenance() -> None:
    reader = _reader_with_tree(make_synthetic_datatree(n_azimuth=36, n_range=40))
    lp.describe_reader(reader, {"attributes": {"instrument_name": "KTST"}})
    file_id = server._register_reader(reader)
    try:
        h, _ = _make_handler()
        await h.handle_message(
            json.dumps({"type": "get_volume", "variable": "DBZH", "file_id": file_id,
                        "box": {"x_min": -50000, "x_max": 50000, "y_min": -50000, "y_max": 50000},
                        "resolution": 8, "z_max": 8000})
        )  # fmt: skip
        vol = next(m for m in h._sent if m["type"] == "volume_data")
        assert vol["provenance"]["kind"] == "observed" and "4/3" in vol["provenance"]["method"]
    finally:
        server._file_readers.pop(file_id, None)


def test_mosaic_and_wind_headers_name_their_radars_and_say_retrieved() -> None:
    m = lp.for_mosaic_header(
        {"radars": [{"name": "KHPX"}, {"name": "KNQA"}], "analysis_time": "2021-12-11T01:59:00Z",
         "product": "composite", "variable": "DBZH", "height_m": 3000.0, "merge": {"applied": True}}
    )  # fmt: skip
    assert (
        m["kind"] == "retrieved"
        and m["inputs"] == ["KHPX", "KNQA"]
        and m["valid_time"] == "2021-12-11T01:59:00Z"
    )
    assert "3000 m" in m["method"] and "KHPX" in m["source"]
    w = lp.for_winds_header(
        {"radars": [{"name": "A"}, {"name": "B"}], "method": "variational", "background": {"x": 1}}
    )
    assert w["kind"] == "retrieved" and "variational" in w["method"] and "background" in w["method"]
    json.dumps([m, w], allow_nan=False)


def test_a_reader_class_attribute_does_not_leak_between_files() -> None:
    a, b = RadarReader(), RadarReader()
    lp.describe_reader(a, {"attributes": {"instrument_name": "A"}})
    assert lp.reader_context(b)["source"] == "radar file" and math.isfinite(1.0)
