"""The methods text names radarx and its version for every step radarx computes (issue #96)."""

from __future__ import annotations

import server
from engine import runrecord


def _step(**kw):
    return runrecord.make_step("module", kw.pop("id"), kw.pop("title"), {"a": 1}, **kw)


def test_a_radarx_step_names_radarx_and_its_version() -> None:
    steps = [
        _step(id="radarx_dealias", title="Dealias", uses={"radarx": "0.5.2"}),
        _step(id="hot_gates", title="Hot gates"),
    ]
    paragraphs = runrecord.methods_text(runrecord.new_record(steps)).split("\n\n")
    assert "radarx 0.5.2" in paragraphs[1]
    assert "radarx" not in paragraphs[2]  # a step radarx did not compute does not claim it


def test_the_server_marks_radarx_modules_only() -> None:
    assert server._radarx_use("hot_gates") == {}
    assert "radarx" in server._radarx_use("radarx_hid")


def test_rxd_methods_and_project_commands(tmp_path, capsys) -> None:
    from engine import cli, project

    step = _step(id="radarx_dealias", title="Dealias", uses={"radarx": "0.5.2"})
    record_path = runrecord.save_record(runrecord.new_record([step]), tmp_path / "run.json")
    code = cli.main(["methods", record_path])
    out = capsys.readouterr().out
    assert code == 0 and "Step 1 of 1." in out and "radarx 0.5.2" in out

    data = tmp_path / "KAAA_V06"
    data.write_bytes(b"1234")
    proj = project.save_project(
        tmp_path / "p.radarxproj", files=[str(data)], run_record=runrecord.load_record(record_path)
    )
    assert cli.main(["methods", proj]) == 0
    assert "Step 1 of 1." in capsys.readouterr().out
    assert cli.main(["project", "check", proj]) == 0
    data.unlink()
    assert cli.main(["project", "check", proj]) == cli.EXIT_INPUT  # a missing file is exit 3
    assert "missing" in capsys.readouterr().out
    assert cli.main(["methods", str(tmp_path / "nothing.json")]) == cli.EXIT_INPUT
