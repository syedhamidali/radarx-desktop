"""``xrd sounding``: the command-line front end of the sounding reader and diagnostics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("radarx")

from engine import cli

FIX = Path(__file__).parent / "data" / "BMX_20220330_00Z.csv"


def run_cli(capsys, *argv):
    code = cli.main([str(a) for a in argv])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_prints_labelled_numbers(capsys) -> None:
    code, out, _ = run_cli(capsys, "sounding", "-i", FIX, "--wind-units", "kt")
    assert code == 0
    for token in (
        "surface-based",
        "mixed-layer",
        "most-unstable",
        "CAPE",
        "m AGL",
        "SRH 0-3km",
        "data class: observed",
    ):
        assert token in out, token
    assert "launch time not in the file" in out and "note: The file gives only the date" in out
    code, out, _ = run_cli(capsys, "sounding", "-i", FIX, "-v", "--wind-units", "kt")
    assert "definitions" in out and "Bunkers" in out and "references:" in out
    assert "libraries:" in out


def test_json_and_output_files(capsys, tmp_path: Path) -> None:
    code, out, _ = run_cli(
        capsys,
        "sounding",
        "-i",
        FIX,
        "--json",
        "--wind-units",
        "kt",
        "-o",
        tmp_path / "r.json",
        "-o",
        tmp_path / "p.csv",
    )
    assert code == 0
    doc = json.loads(out)
    assert doc["diagnostics"]["parcels"]["surface-based"]["cape"] is not None
    assert doc["provenance"]["kind"] == "observed"
    assert doc["files"] == [str(tmp_path / "r.json"), str(tmp_path / "p.csv")]
    assert json.loads((tmp_path / "r.json").read_text())["station"] == "BMX"
    lines = (tmp_path / "p.csv").read_text().splitlines()
    assert any(ln.startswith("# data_class: observed") for ln in lines)  # says what it is
    header = next(i for i, ln in enumerate(lines) if ln.startswith("pressure_hpa"))
    assert len(lines) - header - 1 == 80 and lines[header + 1].startswith("995,")


def test_errors_are_plain(capsys, tmp_path: Path) -> None:
    code, _, err = run_cli(capsys, "sounding", "-i", tmp_path / "missing.csv")
    assert code == 3 and "not found" in err
    bad = tmp_path / "x.csv"
    bad.write_text("a,b\n1,2\n3,4\n5,6\n7,8\n")
    code, _, err = run_cli(capsys, "sounding", "-i", bad)
    assert code == 3 and "temperature" in err
    code, _, err = run_cli(capsys, "sounding", "-i", FIX, "-o", tmp_path / "x.nc")
    assert code == 2 and ".json or .csv" in err
    code, _, err = run_cli(capsys, "sounding", "-i", FIX, "--wind-units", "furlongs")
    assert code == 3 and "unknown wind speed unit" in err
    code, _, err = run_cli(capsys, "sounding")
    assert code == 2


def test_python_api_function_matches_the_command() -> None:
    import xrd
    from engine import api

    d = api.sounding(FIX, wind_units="kt")
    assert d["station"] == "BMX" and d["provenance"]["kind"] == "observed"
    assert d["diagnostics"]["parcels"]["most-unstable"]["cape"] is not None
    assert xrd.sounding is api.sounding and "sounding" in api.__all__
    no_tv = api.sounding(FIX, wind_units="kt", virtual_temperature=False)
    assert no_tv["diagnostics"]["virtual_temperature"] is False
    assert (
        no_tv["diagnostics"]["parcels"]["surface-based"]["cape"]
        <= d["diagnostics"]["parcels"]["surface-based"]["cape"]
    )
    with pytest.raises(api.XrdInputError, match="no data rows|not found"):
        api.sounding(FIX.parent / "missing.csv")


def test_help_lists_the_command_with_the_shared_options() -> None:
    assert "sounding" in cli.COMMANDS and "sounding" in cli.overview()
    text = cli._subparsers(cli.build_parser())["sounding"].format_help()
    for flag in (
        "-i, --input",
        "--json",
        "-v, --verbose",
        "-q, --quiet",
        "--wind-units",
        "-o, --output",
    ):
        assert flag in text, flag
    sample = [a for n, a in cli.help_examples() if n == "sounding"]
    assert len(sample) >= 3
