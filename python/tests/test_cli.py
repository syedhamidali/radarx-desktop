"""Tests of the ``rxd`` command line (``engine.cli``).

Commands are run through ``main(argv)`` (fast, output captured) and, for the packaging checks,
as real subprocesses. Synthetic volumes are written as small NetCDF files; the real KTLX and
Mayfield volumes are optional and skipped when absent. Every sub-command has a test, and so do
the error paths (missing file, unknown module, bad ``-P``).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from engine import api, cli
from tests.conftest import make_synthetic_datatree

KTLX = Path.home() / "Downloads/Others/XRADAR_EXAMPLES/KTLX20130520_201643_V06.gz"
MAYFIELD = Path("/Users/syed44/Downloads/SEVERE_CASES_TEST/mayfield")
PYTHON_DIR = Path(__file__).resolve().parents[1]


def _csv_header(path: Path) -> str:
    """The column header line: exported CSV files start with a ``# `` metadata block."""
    return next(ln for ln in path.read_text().splitlines() if not ln.startswith("#"))


def run_cli(capsys, *argv: str | Path) -> tuple[int, str, str]:
    code = cli.main([str(a) for a in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture(autouse=True)
def _work_in_tmp_dir(tmp_path: Path, monkeypatch):
    """Commands without -o write named files to the current directory: keep that out of the repo."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def synthetic_files(monkeypatch):
    """Files named ``*.syn`` / ``*.radar`` open as in-memory synthetic volumes.

    The reader is real in every other respect (path checks, schema, errors). A volume written
    with ``DataTree.to_netcdf`` comes back with a ``time`` dimension instead of ``azimuth`` (it is
    not a real CfRadial file), so the synthetic trees are injected where the opener would run.
    ``*.radar`` holds the longitude offset of a radar carrying one convective core (radarx tests).
    """
    from engine.reader import RadarReader

    real_open = RadarReader.open_file

    def fake_open(self, path: str):
        if path.endswith(".syn"):
            tree = make_synthetic_datatree(n_azimuth=90, n_range=120)
        elif path.endswith(".split"):  # velocity only on sweep 1 and 2, like a NEXRAD split cut
            tree = make_synthetic_datatree(n_azimuth=90, n_range=120)
            node = tree["sweep_0"]
            node.dataset = node.to_dataset().drop_vars("VRADH")
        elif path.endswith(".radar"):
            pytest.importorskip("radarx")
            from tests.test_radarx_modules import SITE, storm_volume

            tree = storm_volume()
            dlon = float(Path(path).read_text())
            tree.dataset = tree.to_dataset().assign_coords(
                latitude=np.float64(SITE["latitude"]),
                longitude=np.float64(SITE["longitude"] + dlon),
                altitude=np.float64(100.0),
            )
        else:
            return real_open(self, path)
        if not Path(path).is_file():
            raise FileNotFoundError(path)
        self._commit(tree, path, "cfradial1")
        return self.get_schema()

    monkeypatch.setattr(RadarReader, "open_file", fake_open)


@pytest.fixture()
def nc(tmp_path: Path) -> Path:
    """A small synthetic volume (3 sweeps with DBZH and VRADH)."""
    path = tmp_path / "volume.syn"
    path.write_text("synthetic")
    return path


@pytest.fixture()
def nc2(tmp_path: Path) -> Path:
    path = tmp_path / "volume2.syn"
    path.write_text("synthetic")
    return path


@pytest.fixture()
def split_file(tmp_path: Path) -> Path:
    path = tmp_path / "split.split"
    path.write_text("synthetic")
    return path


@pytest.fixture()
def real_nc(tmp_path: Path) -> Path:
    """A real NetCDF file (CfRadial2 layout, opened by the real reader)."""
    path = tmp_path / "real.nc"
    make_synthetic_datatree(n_azimuth=90, n_range=120).to_netcdf(path)
    return path


# ---------------------------------------------------------------------------
# help, version, usage
# ---------------------------------------------------------------------------


def test_main_help_and_version(capsys) -> None:
    code, out, _ = run_cli(capsys, "--help")
    assert code == 0 and "-i, --input" in out and "exit codes" in out
    code, out, _ = run_cli(capsys, "--version")
    assert code == 0 and out.startswith("rxd ")
    code, out, _ = run_cli(capsys)  # no arguments: a friendly overview, not an error
    assert code == 0 and "Look" in out and "Combine" in out and "rxd examples" in out


@pytest.mark.parametrize(
    "command", ["info", "modules", "run", "pipeline", "mosaic", "winds", "cells", "plot", "export"]
)
def test_every_command_has_help_with_the_shared_options(capsys, command: str) -> None:
    code, out, _ = run_cli(capsys, command, "--help")
    assert code == 0
    assert "examples:" in out or command == "modules"
    for flag in ("--json", "--verbose", "--quiet"):
        assert flag in out
    if command not in ("modules", "pipeline"):
        assert "-i, --input" in out


@pytest.mark.parametrize(
    "command", ["run", "pipeline", "mosaic", "winds", "cells", "plot", "export"]
)
def test_output_flags_are_the_same_everywhere(command: str) -> None:
    help_text = cli.build_parser()._subparsers._group_actions[0].choices[command].format_help()
    assert "-o, --output" in help_text
    if command in ("run", "pipeline", "mosaic", "winds", "cells", "plot"):
        assert "-p, --plot" in help_text
    if command not in ("plot", "export"):
        assert "-P, --param" in help_text or command in ("cells",)


def test_every_help_example_parses() -> None:
    """The examples in --help are real command lines: they parse against the parser."""
    examples = cli.help_examples()
    assert len(examples) >= 30
    commands_seen = {name for name, _ in examples}
    assert {"info", "modules", "run", "pipeline", "mosaic", "winds", "cells", "plot", "export"} <= (
        commands_seen
    )
    parser = cli.build_parser()
    for name, argv in examples:
        args, extras = parser.parse_known_args(cli.prepare_argv(argv))
        assert not [e for e in extras if e.startswith("-")], (name, argv, extras)
        expected = "run" if argv[:2] == ["modules", "run"] else name  # 'modules run' = 'run'
        assert args.command == (expected if name != "rxd" else args.command), (name, argv)


def test_documented_final_form_parses() -> None:
    parser = cli.build_parser()
    args, extras = parser.parse_known_args(
        ["mosaic", "-i", "f1.nc", "f2.nc", "f3.nc", "-o", "out.nc", "-p", "img.png", "--verbose"]
    )
    assert args.input == ["f1.nc", "f2.nc", "f3.nc"] and args.output == ["out.nc"]
    assert args.plot == "img.png" and args.verbose == 1 and not extras
    args, extras = parser.parse_known_args(
        [
            "winds",
            "-i",
            "k1",
            "k2",
            "k3",
            "k4",
            "--method",
            "variational",
            "-o",
            "winds.nc",
            "-p",
            "winds.png",
            "--level-km",
            "4.5",
            "-v",
        ]
    )
    assert args.input == ["k1", "k2", "k3", "k4"] and args.level_km == 4.5 and args.verbose == 1
    args, extras = parser.parse_known_args(
        ["run", "radarx_hid", "-i", "a.nc", "-i", "b.nc", "-P", "x=1", "-P", "y=2", "-vv"]
    )
    assert args.input == ["a.nc", "b.nc"] and args.param == ["x=1", "y=2"] and args.verbose == 2
    args, extras = parser.parse_known_args(["cells", "-V", "DBZH", "-s", "2", "a.nc", "b.nc"])
    assert (args.variable, args.sweep, extras) == ("DBZH", 2, ["a.nc", "b.nc"])


def test_unknown_option_and_missing_command_are_usage_errors(capsys, nc: Path) -> None:
    code, _, err = run_cli(capsys, "info", "-i", nc, "--bogus")
    assert code == 2 and "unrecognized" in err
    code, _, err = run_cli(capsys, "zzqx")
    assert code == 2 and "invalid choice" in err
    code, _, err = run_cli(capsys, "run", "-i", nc)
    assert code == 2 and "needs a module" in err
    code, _, err = run_cli(capsys, "info")
    assert code == 2 and "needs a radar file" in err and "rxd info -i" in err


def test_no_color_is_honoured(monkeypatch) -> None:
    class Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert cli._color_enabled(Tty())
    assert "\033[" in cli._err_label(Tty())
    monkeypatch.setenv("NO_COLOR", "1")
    assert not cli._color_enabled(Tty())
    assert cli._err_label(Tty()) == "error:"
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("TERM", "dumb")
    assert not cli._color_enabled(Tty())
    assert not cli._color_enabled(sys.stdout) or sys.stdout.isatty()


def test_main_does_not_leak_global_state(capsys, nc: Path) -> None:
    import logging

    before = logging.root.manager.disable
    run_cli(capsys, "info", "-i", nc, "--no-color")
    assert logging.root.manager.disable == before
    assert cli._STATE["no_color"] is False


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------


def test_info_of_a_real_netcdf_file(capsys, real_nc: Path) -> None:
    code, out, _ = run_cli(capsys, "info", "-i", real_nc, "--json")
    data = json.loads(out)
    assert code == 0 and data["format"] == "cfradial2" and data["n_sweeps"] == 3


def test_info_text_and_json_and_positional_alias(capsys, nc: Path, nc2: Path) -> None:
    code, out, _ = run_cli(capsys, "info", "-i", nc)
    assert code == 0
    assert "format     cfradial1" in out and "sweeps     3" in out and "DBZH VRADH" in out
    same, out2, _ = run_cli(capsys, "info", nc)  # bare FILE is accepted too
    assert same == 0 and out2 == out
    code, out, _ = run_cli(capsys, "info", "-i", nc, "--json")
    data = json.loads(out)
    assert code == 0 and data["n_sweeps"] == 3 and data["variables"] == ["DBZH", "VRADH"]
    code, out, _ = run_cli(capsys, "info", "-i", nc, nc2, "--json")
    assert [d["path"] for d in json.loads(out)] == [str(nc), str(nc2)]
    code, out, _ = run_cli(capsys, "info", "-i", nc, "-i", nc2, "-q")
    assert code == 0  # -q only silences chatter, results are still printed


def test_info_errors(capsys, tmp_path: Path) -> None:
    code, out, err = run_cli(capsys, "info", "-i", tmp_path / "missing.nc")
    assert code == 3 and out == "" and "File not found" in err and "error:" in err
    code, _, err = run_cli(capsys, "info", "-i", tmp_path)
    assert code == 3 and "No radar files in folder" in err
    bad = tmp_path / "bad.nc"
    bad.write_bytes(b"garbage" * 50)
    code, _, err = run_cli(capsys, "info", "-i", bad)
    assert code == 1 and "Could not open" in err


# ---------------------------------------------------------------------------
# modules
# ---------------------------------------------------------------------------


def test_modules_list_filter_json_and_show(capsys) -> None:
    code, out, _ = run_cli(capsys, "modules")
    assert code == 0 and "storm_detection" in out and "module(s)" in out
    code, out, _ = run_cli(capsys, "modules", "--category", "qc")
    assert code == 0 and "qc_clean" in out and "storm_detection" not in out
    code, out, _ = run_cli(capsys, "modules", "--json")
    ids = {m["id"] for m in json.loads(out)["modules"]}
    assert code == 0 and {"storm_detection", "radarx_hid"} <= ids
    code, out, _ = run_cli(capsys, "modules", "show", "storm_detection")
    assert code == 0 and "threshold_dbz" in out and "default 35.0" in out
    code, out, _ = run_cli(capsys, "modules", "show", "storm_detection", "--json")
    assert json.loads(out)["id"] == "storm_detection"


def test_modules_errors(capsys) -> None:
    code, _, err = run_cli(capsys, "modules", "show", "storm_detectio")
    assert code == 2 and "Did you mean storm_detection" in err
    code, _, err = run_cli(capsys, "modules", "show")
    assert code == 2 and "needs a module id" in err
    code, _, err = run_cli(capsys, "modules", "--category", "Nope")
    assert code == 2 and "Categories" in err


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_run_prints_summary_and_table(capsys, nc: Path) -> None:
    code, out, _ = run_cli(
        capsys, "run", "storm_detection", "-i", nc, "-s", "0", "-P", "threshold_dbz=40"
    )
    assert code == 0
    assert "storm_detection" in out and "Cells:" in out and "table: Storm cells" in out
    code, out2, _ = run_cli(
        capsys, "run", "storm_detection", nc, "--sweep", "0", "-P", "threshold_dbz=40"
    )
    assert code == 0 and out2.splitlines()[2:] == out.splitlines()[2:]  # positional file works


def test_run_json_output(capsys, nc: Path) -> None:
    code, out, _ = run_cli(
        capsys, "run", "storm_detection", "-i", nc, "--json", "-P", "min_size_km2=5"
    )
    data = json.loads(out)
    assert code == 0
    assert data["module"] == "storm_detection" and data["params"]["min_size_km2"] == 5.0
    # the table is capped at 200 rows for very noisy sweeps; Cells is the true count
    assert min(data["scalars"]["Cells"], 200) == len(next(iter(data["tables"].values())))
    assert data["files"] == [] and data["sweep"] == 0


def test_run_output_by_extension(capsys, nc: Path, tmp_path: Path) -> None:
    csv = tmp_path / "cells.csv"
    code, out, _ = run_cli(capsys, "run", "storm_detection", "-i", nc, "-o", csv)
    assert code == 0 and csv.read_text().startswith("id,") and f"wrote {csv.name}" in out
    js = tmp_path / "deep" / "summary.json"
    run_cli(capsys, "run", "storm_detection", "-i", nc, "-o", js)
    assert json.loads(js.read_text())["module"] == "storm_detection"
    cnc = tmp_path / "qc.nc"
    code, _, _ = run_cli(capsys, "run", "qc_clean", "-i", nc, "-o", cnc, "-P", "use_snr=false")
    assert code == 0 and cnc.stat().st_size > 0
    png = tmp_path / "qc.png"
    code, _, _ = run_cli(capsys, "run", "qc_clean", "-i", nc, "-o", png, "-P", "use_snr=false")
    assert code == 0 and png.stat().st_size > 1000


def test_run_output_folder_and_format(capsys, nc: Path, tmp_path: Path) -> None:
    folder = tmp_path / "results"
    code, _, _ = run_cli(capsys, "run", "storm_detection", "-i", nc, "-o", folder)
    assert code == 0
    assert {p.suffix for p in folder.iterdir()} == {".json", ".csv"}  # no field -> no .nc
    other = tmp_path / "other"
    code, _out, _ = run_cli(capsys, "run", "qc_clean", "-i", nc, "-o", other, "-f", "json,png")
    assert code == 0 and {p.suffix for p in other.iterdir()} == {".json", ".png"}
    # -f alone writes into the current directory
    code, _, err = run_cli(capsys, "run", "storm_detection", "-i", nc, "-o", other, "-f", "nc")
    assert code == 0 and "no gridded field" in err  # explicit request that cannot be met: warned
    code, _, err = run_cli(capsys, "run", "storm_detection", "-i", nc, "-f", "docx")
    assert code == 2 and "--format" in err


def test_run_plot_and_all_sweeps(capsys, nc: Path, tmp_path: Path) -> None:
    fig = tmp_path / "f.png"
    code, out, _ = run_cli(capsys, "run", "qc_clean", "-i", nc, "-p", fig, "-P", "use_snr=false")
    assert code == 0 and fig.stat().st_size > 1000
    code, out, _ = run_cli(
        capsys, "run", "qc_clean", "-i", nc, "--all-sweeps", "-P", "use_snr=false",
        "-p", tmp_path / "all.png", "--plot-sweep", "1", "-o", tmp_path / "all.json",
    )  # fmt: skip
    assert code == 0 and "derived variable(s): qc_clean:" in out
    assert json.loads((tmp_path / "all.json").read_text())["all_sweeps"] is True
    assert (tmp_path / "all.png").stat().st_size > 1000
    code, _, err = run_cli(capsys, "run", "storm_detection", "-i", nc, "-p", tmp_path / "none.png")
    assert code == 0 and "no gridded field" in err and not (tmp_path / "none.png").exists()


def test_run_verbose_shows_progress_and_detail(capsys, nc: Path) -> None:
    code, out, err = run_cli(capsys, "run", "qc_clean", "-i", nc, "-v", "-P", "use_snr=false")
    assert code == 0 and "parameters:" in out and "[" in err and "s]" in err
    code, out, err = run_cli(capsys, "run", "qc_clean", "-i", nc, "-P", "use_snr=false")
    assert "parameters:" not in out and "[" not in err  # no progress unless -v


def test_run_errors(capsys, nc: Path, nc2: Path, tmp_path: Path) -> None:
    code, _, err = run_cli(capsys, "run", "nosuchmodule", "-i", nc)
    assert code == 2 and "Unknown module 'nosuchmodule'" in err
    code, _, err = run_cli(capsys, "run", "storm_detection", "-i", tmp_path / "nofile.nc")
    assert code == 3 and "File not found" in err
    code, _, err = run_cli(capsys, "run", "storm_detection", "-i", nc, "-P", "threshold=3")
    assert code == 2 and "no parameter 'threshold'" in err and "Did you mean" in err
    code, _, err = run_cli(capsys, "run", "storm_detection", "-i", nc, "-P", "threshold_dbz")
    assert code == 2 and "KEY=VALUE" in err
    code, _, err = run_cli(capsys, "run", "storm_detection", "-i", nc, "-P", "threshold_dbz=abc")
    assert code == 2 and "threshold_dbz" in err
    code, _, err = run_cli(capsys, "run", "storm_detection", "-i", nc, "-s", "9")
    assert code == 2 and "out of range" in err
    code, _, err = run_cli(capsys, "run", "storm_detection", "-i", nc, nc2)
    assert code == 2 and "one input file" in err
    code, _, err = run_cli(capsys, "run", "storm_detection", "-i", nc, "-s", "0", "--all-sweeps")
    assert code == 2 and "not allowed" in err


def test_run_module_failure_is_exit_1(capsys, nc: Path, monkeypatch) -> None:
    from engine import plugins

    def fail(*a, **k):
        raise plugins.ModuleError("the module could not run on this data")

    monkeypatch.setattr(api.Volume, "run", fail)
    code, _, err = run_cli(capsys, "run", "storm_detection", "-i", nc)
    assert code == 1 and "could not run" in err and "Traceback" not in err


def test_missing_field_says_where_it_is(capsys, split_file: Path) -> None:
    """Velocity is only on sweep 1: the error names the fix; without -s the right sweep is used."""
    code, _, err = run_cli(capsys, "plot", "-i", split_file, "-V", "velocity", "-s", "0")
    assert code == 2 and "Sweeps that have it: 1, 2" in err and "-s 1" in err


def test_exit_codes_by_error_class(capsys, nc: Path, monkeypatch) -> None:
    def dependency(*a, **k):
        raise api.XrdDependencyError("X needs the Python package radarx: pip install -U radarx")

    monkeypatch.setattr(api.Volume, "info", dependency)
    code, _, err = run_cli(capsys, "info", "-i", nc)
    assert code == 4 and "pip install -U radarx" in err

    def pip_message(*a, **k):
        from engine import plugins

        raise plugins.ModuleError("this module needs radarx (pip install radarx)")

    monkeypatch.setattr(api.Volume, "info", pip_message)
    code, _, _ = run_cli(capsys, "info", "-i", nc)
    assert code == 4  # an engine message that says "pip install" is a missing-package error


def test_internal_errors_exit_70_with_hint(capsys, nc: Path, monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(api.Volume, "info", boom)
    code, _, err = run_cli(capsys, "info", "-i", nc)
    assert code == 70 and "unexpected RuntimeError: kaboom" in err and "-vv" in err
    with pytest.raises(RuntimeError):
        cli.main(["info", "-i", str(nc), "-vv"])  # -vv re-raises for the traceback


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------


def test_pipeline_list_script_and_api_snippet(capsys) -> None:
    code, out, _ = run_cli(capsys, "pipeline", "list")
    assert code == 0 and "Reflectivity QC" in out and "built-in" in out
    code, out, _ = run_cli(capsys, "pipeline", "list", "--json")
    assert {r["name"] for r in json.loads(out)["recipes"]} >= {"Reflectivity QC", "Dual-pol QC"}
    code, out, _ = run_cli(capsys, "pipeline", "script", "Reflectivity QC")
    assert code == 0 and "def process_sweep" in out
    code, out, _ = run_cli(capsys, "pipeline", "script", "reflectivity qc", "--api")
    assert code == 0 and "vol.pipeline('Reflectivity QC'" in out
    compile(out.replace('"FILE"', '"x"'), "snippet", "exec")


def test_pipeline_run_outputs(capsys, nc: Path, tmp_path: Path) -> None:
    folder = tmp_path / "qc"
    code, out, _ = run_cli(
        capsys, "pipeline", "reflectivity qc", "-i", nc, "-o", folder / "stats.json",
        "-p", tmp_path / "ba.png", "-P", "speckle.min_size=3", "-P", "gap_fill=off",
    )  # fmt: skip
    assert code == 0 and "Remove speckle" in out and "derived variable: pipeline:" in out
    data = json.loads((folder / "stats.json").read_text())
    assert data["sweeps"] == [0, 1, 2] and data["recipe"]["name"] == "Reflectivity QC"
    assert any(s["enabled"] is False for s in data["recipe"]["steps"])
    assert (tmp_path / "ba.png").stat().st_size > 1000
    code, out, _ = run_cli(
        capsys,
        *["pipeline", "Reflectivity QC", nc, "-s", "1"],
        *["-o", tmp_path / "f2", "-f", "json,png"],
    )
    assert code == 0 and {p.suffix for p in (tmp_path / "f2").iterdir()} == {".json", ".png"}
    code, out, _ = run_cli(
        capsys, "pipeline", "Reflectivity QC", "-i", nc, "-o", tmp_path / "s.json", "--json"
    )
    assert json.loads(out)["name"].startswith("pipeline:")


def test_pipeline_from_json_file_and_target(capsys, nc: Path, tmp_path: Path) -> None:
    recipe = {"name": "velocity only", "target": "DBZH", "steps": [{"step_id": "speckle"}]}
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(recipe))
    code, out, _ = run_cli(
        capsys, "pipeline", path, "-i", nc, "--target", "VRADH", "-o", tmp_path / "s.json"
    )
    assert code == 0 and "target VRADH" in out


def test_pipeline_errors(capsys, nc: Path) -> None:
    code, _, err = run_cli(capsys, "pipeline")
    assert code == 2 and "needs a RECIPE" in err
    code, _, err = run_cli(capsys, "pipeline", "no such recipe", "-i", nc)
    assert code == 2 and "Known recipes" in err
    code, _, err = run_cli(capsys, "pipeline", "Reflectivity QC")
    assert code == 2 and "needs a radar file" in err
    code, _, err = run_cli(capsys, "pipeline", "Reflectivity QC", "-i", nc, "-P", "nostep.x=1")
    assert code == 2 and "no step 'nostep'" in err
    code, _, err = run_cli(capsys, "pipeline", "script")
    assert code == 2 and "usage: rxd pipeline script" in err
    code, _, err = run_cli(capsys, "pipeline", "Dual-pol QC", "-i", nc)
    assert code == 1 and "ZDR" in err  # no ZDR in the synthetic file: processing failed


# ---------------------------------------------------------------------------
# cells
# ---------------------------------------------------------------------------


def test_cells_single_file(capsys, nc: Path, tmp_path: Path) -> None:
    code, out, _ = run_cli(
        capsys,
        *["cells", "-i", nc, "--threshold-dbz", "30", "--min-size-km2", "1"],
        *["--backend", "numpy", "-o", tmp_path / "cells.csv", "-o", tmp_path / "cells.json"],
        *["-p", tmp_path / "cells.png"],
    )
    assert code == 0 and "cell(s)" in out and "backend numpy" in out
    assert (tmp_path / "cells.csv").read_text().startswith("id,")
    assert json.loads((tmp_path / "cells.json").read_text())["cells"]
    assert (tmp_path / "cells.png").stat().st_size > 1000
    code, out, _ = run_cli(
        capsys, "cells", "-i", nc, "--min-size-km2", "1", "--json", "--backend", "numpy"
    )
    assert json.loads(out)["backend"] == "numpy"


def test_cells_tracking_several_files(capsys, nc: Path, nc2: Path, tmp_path: Path) -> None:
    code, out, _ = run_cli(
        capsys, "cells", "-i", nc, nc2, "--threshold-dbz", "30", "--min-size-km2", "1",
        "--backend", "numpy", "--dt-seconds", "60", "-o", tmp_path / "all.csv",
        "--tracks", tmp_path / "tracks.csv", "-p", tmp_path / "tracks.png",
    )  # fmt: skip
    assert code == 0 and "track(s)" in out and "2 scans" in out
    assert "scan,time,id" in _csv_header(tmp_path / "all.csv")
    assert (tmp_path / "tracks.csv").read_text().startswith("track_id,")
    assert (tmp_path / "tracks.png").stat().st_size > 1000
    code, out, _ = run_cli(capsys, "cells", "-i", nc, nc2, "--backend", "numpy", "--json")
    assert set(json.loads(out)) >= {"tracks", "cells", "backend", "times_s"}


def test_cells_errors(capsys, nc: Path, tmp_path: Path) -> None:
    code, _, err = run_cli(capsys, "cells", "-i", nc, "-o", tmp_path / "x.xlsx")
    assert code == 2 and "Cannot write cells" in err
    code, _, err = run_cli(capsys, "cells", "-i", nc, "-V", "ZDR")
    assert code == 2 and "ZDR" in err
    code, _, err = run_cli(capsys, "cells", "-i", nc, "--backend", "magic")
    assert code == 2 and "invalid choice" in err


# ---------------------------------------------------------------------------
# plot
# ---------------------------------------------------------------------------


def test_plot_variants(capsys, nc: Path, tmp_path: Path, monkeypatch) -> None:
    out_png = tmp_path / "ppi.png"
    code, out, _ = run_cli(
        capsys, "plot", "-i", nc, "-V", "velocity", "-s", "1", "-p", out_png, "--dpi", "80"
    )
    assert code == 0 and out_png.stat().st_size > 1000 and f"wrote {out_png.name}" in out
    other = tmp_path / "o.pdf"
    code, _, _ = run_cli(
        capsys,
        *["plot", "-i", nc, "-o", other, "--theme", "dark", "--vmin", "0", "--vmax", "50"],
        *["--title", "hello", "--no-rings", "--dpi", "60"],
    )
    assert code == 0 and other.read_bytes().startswith(b"%PDF")
    monkeypatch.chdir(tmp_path)
    code, out, _ = run_cli(
        capsys, "plot", "-i", nc, "--dpi", "60"
    )  # default name, default variable
    assert (
        code == 0
        and "volume_DBZH_sweep0.png" in out
        and (tmp_path / "volume_DBZH_sweep0.png").exists()
    )
    code, out, _ = run_cli(
        capsys, "plot", "-i", nc, "-p", tmp_path / "noext", "-f", "svg", "--dpi", "60", "--json"
    )
    assert json.loads(out)["files"] == [str(tmp_path / "noext.svg")]


def test_plot_errors(capsys, nc: Path, tmp_path: Path) -> None:
    code, _, err = run_cli(capsys, "plot", "-i", nc, "-V", "ZDR")
    assert code == 2 and "ZDR" in err
    code, _, err = run_cli(capsys, "plot", "-i", nc, "-s", "5")
    assert code == 2 and "out of range" in err
    code, _, err = run_cli(capsys, "plot", "-i", nc, "-p", tmp_path / "x.txt")
    assert code == 2 and ".png, .svg or .pdf" in err
    code, _, err = run_cli(
        capsys, "plot", "-i", nc, "-p", tmp_path / "a.png", "-o", tmp_path / "b.png"
    )
    assert code == 2 and "one figure" in err


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def test_export_formats_by_extension(capsys, nc: Path, tmp_path: Path) -> None:
    csv = tmp_path / "d.csv"
    code, out, _ = run_cli(capsys, "export", "-i", nc, "-o", csv, "-V", "reflectivity", "-s", "1")
    assert code == 0 and _csv_header(csv) == "azimuth_deg,range_m,DBZH"
    zarr = tmp_path / "v.zarr"
    code, _, _ = run_cli(capsys, "export", "-i", nc, "-o", zarr)
    assert code == 0 and zarr.is_dir()
    png = tmp_path / "v.png"
    code, _, _ = run_cli(capsys, "export", "-i", nc, "-o", png, "--dpi", "72")
    assert code == 0 and png.stat().st_size > 1000
    code, _, _err = run_cli(
        capsys, "export", "-i", nc, "-f", "cfradial2", "-o", tmp_path / "cf2.nc"
    )
    assert code in (0, 1)  # synthetic metadata may be too thin for CfRadial2; never a crash
    code, out, _ = run_cli(
        capsys, "export", "-i", nc, "-f", "csv", "-o", tmp_path / "x.dat", "--json"
    )
    assert code == 0 and json.loads(out)["files"][0].endswith("x.dat")


def test_export_derived_via_pipeline_and_run(capsys, nc: Path, tmp_path: Path) -> None:
    out = tmp_path / "with_derived.zarr"
    code, _text, _ = run_cli(
        capsys, "export", "-i", nc, "-o", out, "--pipeline", "Reflectivity QC",
        "--run", "qc_clean:use_snr=false", "--include-derived",
    )  # fmt: skip
    assert code == 0
    import xarray as xr

    tree = xr.open_datatree(out, engine="zarr")
    names = set(tree["sweep_0"].to_dataset().data_vars)
    assert any(n.startswith("pipeline__") for n in names)
    assert any(n.startswith("qc_clean__") for n in names)


def test_export_errors(capsys, nc: Path, tmp_path: Path) -> None:
    code, _, err = run_cli(capsys, "export", "-i", nc, "-o", tmp_path / "x.bin")
    assert code == 2 and "Cannot tell" in err
    code, _, err = run_cli(capsys, "export", "-i", nc, "-o", tmp_path / "x.nc", "--include-derived")
    assert code == 2 and "--pipeline" in err
    code, _, err = run_cli(
        capsys, "export", "-i", nc, "-o", tmp_path / "a.csv", "-o", tmp_path / "b.csv"
    )
    assert code == 2 and "single -o" in err
    code, _, err = run_cli(capsys, "export", "-i", nc, "-o", tmp_path / "x.nc")
    assert code == 1 and "lacks metadata" in err  # synthetic file: not a valid CfRadial1 volume
    code, _, err = run_cli(capsys, "export", "-i", nc, "-f", "docx", "-o", tmp_path / "x.docx")
    assert code == 2 and "Unknown export format 'docx'" in err


# ---------------------------------------------------------------------------
# mosaic and winds
# ---------------------------------------------------------------------------


@pytest.fixture()
def radar_files(tmp_path: Path) -> list[Path]:
    """Two radar files 0.8 degrees apart holding one convective core (needs radarx)."""
    pytest.importorskip("radarx")
    paths = []
    for i, dlon in enumerate((0.0, 0.8)):
        path = tmp_path / f"radar{i}.radar"
        path.write_text(str(dlon))
        paths.append(path)
    return paths


def test_mosaic_command(capsys, radar_files: list[Path], tmp_path: Path, monkeypatch) -> None:
    a, b = radar_files
    code, out, err = run_cli(
        capsys, "mosaic", "-i", a, b, "--resolution-m", "2000", "-o", tmp_path / "m.nc",
        "-o", tmp_path / "m.png", "-p", tmp_path / "map.png", "-v",
    )  # fmt: skip
    assert code == 0 and "composite of DBZH" in out and "2 radar(s)" in out
    assert "[" in err  # -v progress
    names = {p.name for p in tmp_path.iterdir()}
    assert {"m.nc", "m.png", "m.pgw", "m.prj", "map.png"} <= names
    code, out, _ = run_cli(
        capsys,
        *["mosaic", a, b, "--product", "cappi", "--height-m", "1500", "--resolution-m", "2000"],
        *["--no-merge", "-V", "reflectivity", "-P", "range_scale_km=40", "--json"],
    )
    data = json.loads(out)
    assert code == 0 and data["product"] == "cappi" and data["merge"]["range_scale_km"] == 40
    monkeypatch.chdir(tmp_path)  # no -o / -p: named files are written to the current directory
    code, out, err = run_cli(capsys, "mosaic", "-i", a, "--resolution-m", "2000")
    made = sorted(p.name for p in tmp_path.glob("mosaic_composite_DBZH_*"))
    assert code == 0 and "as the outputs" in out and len(made) == 2
    assert (
        sum(n.endswith("_map.png") for n in made) == 1 and sum(n.endswith(".nc") for n in made) == 1
    )
    assert all(f"wrote {n}" in out for n in made)


def test_mosaic_errors(capsys, radar_files: list[Path], tmp_path: Path) -> None:
    a, b = radar_files
    code, _, err = run_cli(capsys, "mosaic", "-i", a, b, "-o", tmp_path / "m.tif")
    assert code == 2 and ".nc" in err and ".png" in err
    code, _, err = run_cli(capsys, "mosaic", "-o", tmp_path / "m.nc")
    assert code == 2 and "needs a radar file" in err
    code, _, err = run_cli(capsys, "mosaic", "-i", a, "--extent", "1,2,3")
    assert code == 2 and "--extent" in err
    code, _, err = run_cli(capsys, "mosaic", "-i", a, "-P", "bogus_option=1")
    assert code == 2 and "unknown option" in err
    code, _, err = run_cli(capsys, "mosaic", "-i", a, "--resolution-m", "50")
    assert code == 1 and "resolution_m" in err


def test_winds_command_validation(capsys, nc: Path, nc2: Path, tmp_path: Path) -> None:
    code, _, err = run_cli(capsys, "winds", "-i", nc)
    assert code == 2 and "at least two radar files" in err and "FILE1 FILE2" in err
    code, _, err = run_cli(capsys, "winds", "-i", nc, nc2, "-o", tmp_path / "w.tif")
    assert code == 2 and ".nc" in err
    code, _, err = run_cli(capsys, "winds", "-i", nc, nc2, "--origin", "x")
    assert code == 2 and "--origin" in err
    code, _, err = run_cli(capsys, "winds", "-i", nc, nc2, "--level-km", "3")
    assert code == 2 and "-p winds.png" in err
    code, _, err = run_cli(capsys, "winds", "-i", nc, nc2, "-P", "min_crossing_angel=1")
    assert code == 2 and "unknown option" in err
    code, _, err = run_cli(capsys, "winds", "-i", nc, nc2, "--method", "magic")
    assert code == 2 and "invalid choice" in err


@pytest.mark.skipif(not MAYFIELD.is_dir(), reason="Mayfield volumes not available")
def test_winds_command_on_real_radars(capsys, tmp_path: Path) -> None:
    pytest.importorskip("radarx")
    files = [sorted((MAYFIELD / r).glob("*_V06"))[0] for r in ("KHPX", "KPAH")]
    code, out, _ = run_cli(
        capsys, "winds", "-i", files[0], files[1], "--extent", "60", "--dz-m", "1000",
        "--zmin-m", "500", "--zmax-m", "5000", "-o", tmp_path / "w.nc", "-p", tmp_path / "w.png",
        "--level-km", "2.5", "-P", "min_crossing_angle=20", "-v",
    )  # fmt: skip
    assert code == 0 and "analytic winds" in out and "2 radar(s)" in out
    assert (tmp_path / "w.nc").exists() and (tmp_path / "w.png").stat().st_size > 1000
    code, out, _ = run_cli(capsys, "winds", "-i", files[0], files[1], "--extent", "60", "--json")
    assert json.loads(out)["method"] == "analytic"


# ---------------------------------------------------------------------------
# real volumes (optional)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not KTLX.exists(), reason="KTLX example volume not available")
def test_real_ktlx_info_run_plot(capsys, tmp_path: Path) -> None:
    code, out, _ = run_cli(capsys, "info", "-i", KTLX)
    assert code == 0 and "KTLX" in out and "nexrad_level2" in out and "~" in out
    code, out, _ = run_cli(
        capsys,
        "plot",
        "-i",
        KTLX,
        "-V",
        "velocity",
        "-s",
        "1",
        "-p",
        tmp_path / "v.png",
        "--dpi",
        "60",
    )
    assert code == 0 and (tmp_path / "v.png").exists()
    code, out, _ = run_cli(
        capsys,
        "pipeline",
        "Doppler velocity",
        "-i",
        KTLX,
        "--target",
        "VRADH",
        "-o",
        tmp_path / "p.json",
    )
    assert code == 0 and "Dealias velocity" in out
    code, _, err = run_cli(capsys, "plot", "-i", KTLX, "-V", "ZDR", "-s", "1")
    assert code == 2 and "-s 0" in err  # tells where ZDR is


# ---------------------------------------------------------------------------
# packaging: entry point, python -m, imports stay lazy
# ---------------------------------------------------------------------------


def test_console_script_is_declared() -> None:
    tomllib = pytest.importorskip("tomllib")
    data = tomllib.loads((PYTHON_DIR / "pyproject.toml").read_text())
    assert data["project"]["scripts"]["rxd"] == "engine.cli:main"
    assert "rxd*" in data["tool"]["setuptools"]["packages"]["find"]["include"]
    assert callable(cli.main)


def _python(*args: str, **kw) -> subprocess.CompletedProcess:
    env = {
        "PYTHONPATH": str(PYTHON_DIR),
        "PATH": "/usr/bin:/bin",
        "HOME": str(Path.home()),
        "RADARX_AUTH_DISABLED": "1",  # the development bypass (docs/ACCOUNTS.md)
    }
    return subprocess.run(
        [sys.executable, "-W", "ignore", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        close_fds=False,  # no fork: see tests/account_helpers.run_python (pyproj's atfork handler)
        **kw,
    )


def test_python_dash_m_engine_and_xrd(real_nc: Path) -> None:
    for module in ("engine", "rxd"):
        proc = _python("-m", module, "info", "-i", str(real_nc), "--json")
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)["n_sweeps"] == 3
    proc = _python("-m", "engine", "info", "-i", "/does/not/exist.nc")
    assert proc.returncode == 3 and "File not found" in proc.stderr and proc.stdout == ""
    proc = _python("-m", "engine", "--help")
    assert proc.returncode == 0 and "usage: rxd" in proc.stdout


def test_cli_import_is_light_and_server_never_imports_it() -> None:
    code = (
        "import sys; import engine.cli; "
        "assert 'numpy' not in sys.modules and 'xarray' not in sys.modules, "
        "'cli imports the engine eagerly'"
    )
    proc = _python("-c", code)
    assert proc.returncode == 0, proc.stderr
    code = (
        "import sys; import server; "
        "assert 'engine.cli' not in sys.modules and 'engine.api' not in sys.modules, "
        "[m for m in sys.modules if m.startswith('engine.')]"
    )
    proc = _python("-c", code)
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# usability: discoverability, defaults, input flexibility, reproducibility
# ---------------------------------------------------------------------------


def test_typos_get_suggestions(capsys, nc: Path) -> None:
    code, _, err = run_cli(capsys, "mosiac", "-i", nc)
    assert code == 2 and "Did you mean 'mosaic'" in err
    code, _, err = run_cli(capsys, "info", "-i", nc, "--jsno")
    assert code == 2 and "Did you mean --json" in err
    code, _, err = run_cli(capsys, "examples", "mosiac")
    assert code == 2 and "Did you mean 'mosaic'" in err
    code, _, err = run_cli(capsys, "pipeline", "reflectivty", "-i", nc)
    assert code == 2 and "Known recipes" in err


def test_examples_tour_and_topics(capsys) -> None:
    code, out, _ = run_cli(capsys, "examples")
    assert code == 0 and "# LOOK" in out and "# COMBINE" in out and "--config run.toml" in out
    code, out, _ = run_cli(capsys, "tour", "combine")  # alias
    assert code == 0 and out.startswith("# COMBINE") and "rxd mosaic" in out
    parser = cli.build_parser()
    commands = cli.tour_commands()
    assert len(commands) >= 20
    for argv in commands:
        if "--config" in argv:  # needs the file named in the example
            continue
        _, extras = parser.parse_known_args(cli.prepare_argv(argv))
        assert not [e for e in extras if e.startswith("-")], argv


def test_command_tree_aliases() -> None:
    assert cli.prepare_argv(["modules", "run", "radarx_hid", "-i", "f"]) == [
        "run", "radarx_hid", "-i", "f",
    ]  # fmt: skip
    assert cli.prepare_argv(["modules", "list", "--category", "QC"]) == [
        "modules", "--category", "QC",
    ]  # fmt: skip
    assert cli.prepare_argv(["tour"]) == ["examples"]
    parser = cli.build_parser()
    args, _ = parser.parse_known_args(cli.prepare_argv(["pipeline", "run", "QC", "-i", "f"]))
    assert args.command == "pipeline" and args.args == ["run", "QC"]


def test_modules_list_show_run_pattern(capsys, nc: Path) -> None:
    code, out, _ = run_cli(capsys, "modules", "list", "--category", "QC")
    assert code == 0 and "qc_clean" in out
    code, out, _ = run_cli(capsys, "modules", "show", "storm_detection")
    assert "Run it:  rxd run storm_detection -i FILE --all-sweeps" in out
    code, out, _ = run_cli(capsys, "modules", "run", "storm_detection", "-i", nc)
    assert code == 0 and "Cells:" in out
    code, out, _ = run_cli(capsys, "pipeline", "show", "reflectivity qc")
    assert code == 0 and "snr_gate" in out and "min_snr=3.0" in out and "-P STEP.PARAMETER" in out
    code, out, _ = run_cli(capsys, "pipeline", "show", "Reflectivity QC", "--json")
    assert json.loads(out)["target"] == "DBZH"
    code, out, _ = run_cli(capsys, "pipeline", "run", "Reflectivity QC", "-i", nc, "-o", "-q")
    assert code == 2 or code == 0  # "-o -q": -q taken as the path is a usage slip, never a crash


def test_defaults_name_the_output_and_say_what_was_chosen(
    capsys, nc: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code, out, _ = run_cli(capsys, "plot", "-i", nc, "--dpi", "50")
    assert code == 0
    assert "using variable DBZH" in out and "wrote volume_DBZH_sweep0.png" in out
    code, out, _ = run_cli(capsys, "plot", "-i", nc, "--dpi", "50", "-q")
    assert out == "" and (tmp_path / "volume_DBZH_sweep0.png").exists()  # -q silences all lines
    code, out, _ = run_cli(capsys, "export", "-i", nc, "-f", "csv", "-V", "DBZH")
    assert code == 0 and (tmp_path / "volume_DBZH_sweep0.csv").exists()
    code, out, _ = run_cli(capsys, "export", "-i", nc, "--pipeline", "Reflectivity QC", "-q")
    assert code == 1  # default format is NetCDF, which a synthetic volume cannot be


def test_sweep_is_chosen_where_the_data_is(
    capsys, split_file: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code, out, _ = run_cli(capsys, "plot", "-i", split_file, "-V", "velocity", "--dpi", "50")
    assert code == 0 and "using sweep 1 (the lowest sweep with velocity" in out
    assert (tmp_path / "split_VRADH_sweep1.png").exists()
    code, out, _ = run_cli(capsys, "cells", "-i", split_file, "-V", "velocity", "--json")
    assert json.loads(out)["sweep"] == 1


def test_files_globs_folders_and_stdin(
    capsys, tmp_path: Path, monkeypatch, nc: Path, nc2: Path
) -> None:
    import io

    (tmp_path / "notes.txt").write_text("not a radar file")
    (tmp_path / ".hidden.syn").write_text("x")
    code, out, _ = run_cli(capsys, "info", "-i", tmp_path, "--json")
    assert [Path(d["path"]).name for d in json.loads(out)] == ["volume.syn", "volume2.syn"] or [
        Path(d["path"]).name for d in json.loads(out)
    ] == sorted(["volume.syn", "volume2.syn"])
    code, out, _ = run_cli(capsys, "info", "-i", str(tmp_path / "*.syn"), "--json")
    assert len(json.loads(out)) == 2
    code, out, _ = run_cli(capsys, "info", str(tmp_path / "volume?.syn"), "--json")
    assert [Path(json.loads(out)["path"]).name] == ["volume2.syn"]
    monkeypatch.setattr("sys.stdin", io.StringIO(f"# list\n{nc}\n\n{nc2}\n"))
    code, out, _ = run_cli(capsys, "info", "-i", "-", "--json")
    assert len(json.loads(out)) == 2
    code, out, _ = run_cli(capsys, "info", "-i", nc, nc, "--json")  # duplicates are dropped
    assert json.loads(out)["path"] == str(nc)
    code, _, err = run_cli(capsys, "info", "-i", str(tmp_path / "*.nope"))
    assert code == 3 and "matches no files" in err and "Quote the pattern" in err
    empty = tmp_path / "empty"
    empty.mkdir()
    code, _, err = run_cli(capsys, "info", "-i", empty)
    assert code == 3 and "No radar files in folder" in err
    code, _, err = run_cli(capsys, "plot", "-i", tmp_path / "*.syn")
    assert code == 2 and "one input file" in err


def test_config_file_holds_the_same_options(capsys, nc: Path, tmp_path: Path, monkeypatch) -> None:
    tomllib = pytest.importorskip("tomllib")
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "run.toml"
    cfg.write_text(
        f"""
command = "run"
module = "storm_detection"
input = ["{nc}"]
output = "cells.csv"

[param]
threshold_dbz = 30
min_size_km2 = 1
"""
    )
    assert tomllib.loads(cfg.read_text())["param"]["threshold_dbz"] == 30
    code, out, _ = run_cli(capsys, "--config", cfg)
    assert code == 0 and "Cells:" in out and (tmp_path / "cells.csv").exists()
    # options given on the command line win over the file
    code, out, _ = run_cli(capsys, "--config", cfg, "-P", "threshold_dbz=60", "-o", "other.csv")
    assert code == 0 and (tmp_path / "other.csv").exists()
    code, out, _ = run_cli(capsys, "run", "--config", cfg, "--json")
    assert json.loads(out)["params"]["threshold_dbz"] == 30.0
    js = tmp_path / "plot.json"
    js.write_text(json.dumps({"command": "plot", "input": [str(nc)], "plot": "cfg.png", "dpi": 50}))
    code, out, _ = run_cli(capsys, "--config", js, "-q")
    assert code == 0 and (tmp_path / "cfg.png").exists()


def test_config_errors(capsys, tmp_path: Path) -> None:
    code, _, err = run_cli(capsys, "--config", tmp_path / "missing.toml", "info")
    assert code == 2 and "config file not found" in err
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]")
    code, _, err = run_cli(capsys, "info", "--config", bad)
    assert code == 2 and "key = value" in err
    nocmd = tmp_path / "nocmd.json"
    nocmd.write_text("{}")
    code, _, err = run_cli(capsys, "--config", nocmd)
    assert code == 2 and "command" in err
    odd = tmp_path / "odd.ini"
    odd.write_text("x")
    code, _, err = run_cli(capsys, "info", "--config", odd)
    assert code == 2 and ".toml, .yaml or .json" in err


def test_print_command_gives_the_equivalent_command(
    capsys, split_file: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code, out, _ = run_cli(
        capsys, "plot", "-i", split_file, "-V", "velocity", "--dpi", "50", "--print-command"
    )
    assert code == 0
    line = next(ln for ln in out.splitlines() if ln.startswith("command: "))
    assert (
        line.startswith("command: rxd plot -i ")
        and " -s 1" in line
        and "--print-command" not in line
    )
    code, out, _ = run_cli(capsys, "info", "-i", split_file, "--print-command")
    assert "command: rxd info -i " in out


def test_completion_scripts(tmp_path: Path, capsys) -> None:
    import shutil

    for shell in ("bash", "zsh", "fish"):
        code, out, _ = run_cli(capsys, "completion", shell)
        assert code == 0 and "mosaic" in out and "output" in out
    text = cli.completion_script("bash")
    assert (
        "radarx_hid" in text
        and "composite cappi echo_top" in text
        and "complete -o default" in text
    )
    assert "compdef" not in text and "bashcompinit" in cli.completion_script("zsh")
    assert "complete -c rxd -n __fish_use_subcommand -a info" in cli.completion_script("fish")
    if shutil.which("bash"):
        script = tmp_path / "rxd.bash"
        script.write_text(text)
        assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
        probe = subprocess.run(
            [
                "bash",
                "-c",
                f"source {script}; COMP_WORDS=(rxd mos); COMP_CWORD=1; _xrd; "
                'echo "${COMPREPLY[@]}"',
            ],
            capture_output=True,
            text=True,
        )
        assert "mosaic" in probe.stdout
    code, _, err = run_cli(capsys, "completion", "powershell")
    assert code == 2 and "invalid choice" in err


def test_equivalent_python_api_names() -> None:
    """A notebook user finds the same names as on the command line."""
    for name in (
        "open",
        "info",
        "plot",
        "run",
        "pipeline",
        "mosaic",
        "winds",
        "export",
        "cells",
        "track",
    ):
        assert callable(getattr(api, name)), name
    for name in ("modules", "module", "recipes", "recipe"):
        assert callable(getattr(api, name)), name
    doc = api.__doc__
    assert "Look" in doc and "Combine" in doc and "rxd mosaic" in doc


def test_main_does_not_leak_log_filters(capsys, tmp_path) -> None:
    import logging

    f = tmp_path / "x.nc"
    make_synthetic_datatree(n_azimuth=30, n_range=40).to_netcdf(f)
    saved = logging.lastResort.filters[:]
    run_cli(capsys, "info", "-i", str(f))
    assert logging.lastResort.filters == saved  # nothing leaks out of main()


# --- the former name, `xrd`, keeps working (deprecated alias of `rxd`) ---------------------------


def test_xrd_alias_is_the_same_program(capsys, monkeypatch) -> None:
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
    assert cli.main_xrd(["--version"]) == cli.main(["--version"]) == 0
    out = capsys.readouterr()
    assert out.err == ""  # scripts and pipes see no extra output


def test_xrd_alias_says_so_in_a_terminal(capsys, monkeypatch) -> None:
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    assert cli.main_xrd(["--version"]) == 0
    assert "now called 'rxd'" in capsys.readouterr().err


def test_both_commands_are_installed_entry_points() -> None:
    text = (PYTHON_DIR / "pyproject.toml").read_text()
    assert 'rxd = "engine.cli:main"' in text
    assert 'xrd = "engine.cli:main_xrd"' in text
    assert cli.PROG == "rxd"


def test_import_xrd_is_import_rxd_and_warns() -> None:
    code = (
        "import warnings; warnings.simplefilter('error', DeprecationWarning)\n"
        "import rxd\n"
        "try:\n"
        "    import xrd\n"
        "except DeprecationWarning as w:\n"
        "    assert 'rxd' in str(w); print('warned')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=PYTHON_DIR, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr
    assert "warned" in done.stdout
    same = (
        "import warnings; warnings.simplefilter('ignore'); import rxd, xrd; "
        "assert xrd.open is rxd.open and xrd.__all__ == rxd.__all__"
    )
    done = subprocess.run(
        [sys.executable, "-W", "ignore", "-c", same],
        cwd=PYTHON_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("module", ["rxd", "xrd"])
def test_python_dash_m_runs_both_names(module: str) -> None:
    done = subprocess.run(
        [sys.executable, "-m", module, "--version"],
        cwd=PYTHON_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip()


def test_version_is_the_one_stamped_into_files(capsys) -> None:
    """``rxd --version`` and ``rxd.__version__`` follow the app version, not a stale constant."""
    import rxd
    from engine import provenance

    version = provenance.software_version()
    assert version != "0.1.0"  # the old hard-coded fallback
    code, out, _ = run_cli(capsys, "--version")
    assert code == 0 and out.strip() == f"rxd {version}"
    assert rxd.__version__ == version


def test_xrd_alias_reports_the_same_version() -> None:
    code = (
        "import warnings; warnings.simplefilter('ignore'); import rxd, xrd; "
        "assert xrd.__version__ == rxd.__version__"
    )
    done = subprocess.run(
        [sys.executable, "-W", "ignore", "-c", code],
        cwd=PYTHON_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
