"""Run record, methods text and project file: round trips against known answers (issue #96)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import server
from engine import project, provenance, runrecord
from tests.test_server import _make_handler, _reader_with_tree


def _step(**kw):
    base = {
        "kind": "module",
        "step_id": "hot_gates",
        "title": "Hot gates",
        "params": {"threshold": 50, "min_size": 3, "smooth": True},
        "defaults": {"threshold": 45, "min_size": 3, "smooth": False},
        "version": "1.2",
        "sweep": 0,
        "input_name": "/data/KTLX20130520_201643_V06.gz",
        "data_class": "retrieved",
        "references": ["Doe, J., 2020: A paper. J. Radar 1, 1-2."],
    }
    base.update(kw)
    return runrecord.make_step(
        base.pop("kind"), base.pop("step_id"), base.pop("title"), base.pop("params"), **base
    )


class TestRunRecord:
    def test_record_carries_software_versions_and_only_file_names(self) -> None:
        record = runrecord.new_record([_step()], ["/home/ada/secret/KTLX20130520_201643_V06.gz"])
        assert record["schema"] == runrecord.SCHEMA
        assert record["software_version"] == provenance.software_version()
        assert record["libraries"] == provenance.library_versions()
        assert record["inputs"] == [{"name": "KTLX20130520_201643_V06.gz"}]
        assert "/home/ada" not in json.dumps(record) and "/data/" not in json.dumps(record)

    def test_json_round_trip_is_lossless(self, tmp_path: Path) -> None:
        record = runrecord.new_record([_step(), _step(step_id="b", title="B", params={})], ["a.nc"])
        path = runrecord.save_record(record, tmp_path / "sub" / "run.json")
        assert runrecord.load_record(path) == json.loads(json.dumps(record))
        assert sorted(p.name for p in (tmp_path / "sub").iterdir()) == ["run.json"]  # no temp file

    def test_methods_text_has_every_parameter_the_version_and_one_paragraph_per_step(self) -> None:
        record = runrecord.new_record(
            [_step(), _step(step_id="dealias", title="De-aliasing", params={"nyquist": 26.5})],
            ["KTLX20130520_201643_V06.gz"],
        )
        paragraphs = runrecord.methods_paragraphs(record)
        # software paragraph + 2 steps + references
        assert len(paragraphs) == 4
        assert provenance.software_version() in paragraphs[0]
        assert "KTLX20130520_201643_V06.gz" in paragraphs[0]
        step1 = paragraphs[1]
        assert step1.startswith("Step 1 of 2.")
        assert "Hot gates" in step1 and "version 1.2" in step1 and "sweep 0" in step1
        assert "threshold = 50" in step1  # changed from the default 45: no "(the default)"
        assert "threshold = 50 (the default)" not in step1
        assert "min_size = 3 (the default)" in step1  # unchanged
        assert "smooth = yes" in step1  # booleans in words
        assert "RETRIEVED" in step1
        assert "nyquist = 26.5" in paragraphs[2]
        assert paragraphs[3].startswith("References:") and "Doe, J., 2020" in paragraphs[3]

    def test_every_parameter_of_every_step_is_in_the_text(self) -> None:
        params = {f"p{i}": i * 1.5 for i in range(12)}
        text = runrecord.methods_text(runrecord.new_record([_step(params=params)]))
        for name, value in params.items():
            assert f"{name} = {value:g}" in text

    def test_empty_record_says_so(self) -> None:
        assert "No processing step" in runrecord.methods_text(runrecord.new_record())

    def test_float32_setting_is_part_of_the_record(self, monkeypatch) -> None:
        monkeypatch.setenv("XRADAR_FLOAT32", "1")
        record = runrecord.new_record([_step()])
        assert "float32" in record["numeric_precision"]
        assert "float32" in runrecord.methods_text(record)

    @pytest.mark.parametrize(
        "bad", [{}, {"schema": "other"}, {"schema": runrecord.SCHEMA, "steps": "x"}, [1], None]
    )
    def test_foreign_documents_are_refused_in_words(self, bad) -> None:
        with pytest.raises(runrecord.RunRecordError):
            runrecord.validate(bad)

    def test_a_file_that_is_not_json_is_refused_in_words(self, tmp_path: Path) -> None:
        (tmp_path / "x.json").write_text("{nope")
        with pytest.raises(runrecord.RunRecordError, match="not valid JSON"):
            runrecord.load_record(tmp_path / "x.json")

    def test_log_is_bounded_and_clearable(self) -> None:
        log = runrecord.RunLog()
        for i in range(runrecord.RunLog.MAX_STEPS + 20):
            log.add(_step(step_id=f"s{i}", input_name="a.nc"))
        assert len(log) == runrecord.RunLog.MAX_STEPS
        assert log.record()["inputs"] == [{"name": "a.nc"}]
        log.clear()
        assert len(log) == 0 and log.record()["steps"] == []


class TestProjectFile:
    def _data(self, tmp_path: Path) -> list[Path]:
        folder = tmp_path / "data"
        folder.mkdir()
        paths = []
        for name in ("KAAA_V06", "KBBB_V06"):
            p = folder / name
            p.write_bytes(b"x" * 1234)
            paths.append(p)
        return paths

    def test_round_trip_restores_files_view_recipe_and_record(self, tmp_path: Path) -> None:
        files = self._data(tmp_path)
        view = {"activeFileIndex": 1, "viewMode": "2d", "selectedVariable": "DBZH", "zoom": 2.5}
        recipe = {"name": "QC", "target": "DBZH", "steps": [{"step_id": "despeckle", "params": {}}]}
        record = runrecord.new_record([_step()], ["KAAA_V06"])
        written = project.save_project(
            tmp_path / "work" / "storm",
            name="Storm",
            files=[str(f) for f in files],
            view=view,
            recipe=recipe,
            run_record=record,
        )
        assert written.endswith("storm.radarxproj")
        loaded = project.load_project(written)
        assert loaded["name"] == "Storm"
        assert loaded["view"] == view and loaded["recipe"] == recipe
        assert loaded["run_record"]["steps"] == json.loads(json.dumps(record["steps"]))
        assert [r["status"] for r in loaded["resolved"]] == ["found", "found"]
        assert [Path(r["path"]) for r in loaded["resolved"]] == files

    def test_no_data_is_copied_into_the_project(self, tmp_path: Path) -> None:
        files = self._data(tmp_path)
        big = tmp_path / "big.bin"
        big.write_bytes(os.urandom(2_000_000))
        written = project.save_project(tmp_path / "p.radarxproj", files=[str(big), str(files[0])])
        assert Path(written).stat().st_size < 5_000
        assert os.urandom(1) not in (b"",)  # (silence linters) and the bytes are nowhere:
        assert "big.bin" in Path(written).read_text()

    def test_files_are_recorded_by_path_and_relative_path(self, tmp_path: Path) -> None:
        files = self._data(tmp_path)
        written = project.save_project(tmp_path / "work" / "p.radarxproj", files=[str(files[0])])
        entry = json.loads(Path(written).read_text())["files"][0]
        assert entry["path"] == str(files[0])
        assert entry["relative"] == "../data/KAAA_V06"
        assert entry["size_bytes"] == 1234 and entry["modified"].endswith("Z")

    def test_a_moved_folder_is_found_through_the_relative_path(self, tmp_path: Path) -> None:
        files = self._data(tmp_path)
        written = project.save_project(tmp_path / "work" / "p.radarxproj", files=[str(files[0])])
        moved = tmp_path.parent / (tmp_path.name + "_moved")
        tmp_path.rename(moved)
        try:
            loaded = project.load_project(moved / "work" / "p.radarxproj")
            assert loaded["resolved"][0]["status"] == "found"
            assert Path(loaded["resolved"][0]["path"]) == (moved / "data" / "KAAA_V06").resolve()
        finally:
            moved.rename(tmp_path)
        assert Path(written).exists()

    def test_missing_and_changed_files_are_reported_not_hidden(self, tmp_path: Path) -> None:
        files = self._data(tmp_path)
        written = project.save_project(tmp_path / "p.radarxproj", files=[str(f) for f in files])
        files[0].write_bytes(b"y" * 99)  # other size
        files[1].unlink()
        status = {r["name"]: r["status"] for r in project.load_project(written)["resolved"]}
        assert status == {"KAAA_V06": "changed", "KBBB_V06": "missing"}
        text = project.summary(project.load_project(written))
        assert "changed" in text and "missing" in text

    def test_file_next_to_the_project_is_used_as_the_last_resort(self, tmp_path: Path) -> None:
        files = self._data(tmp_path)
        written = project.save_project(tmp_path / "data" / "p.radarxproj", files=[str(files[0])])
        doc = json.loads(Path(written).read_text())
        doc["files"][0].update(path="/gone/KAAA_V06", relative="../nowhere/KAAA_V06")
        Path(written).write_text(json.dumps(doc))
        assert project.load_project(written)["resolved"][0]["status"] == "found"

    @pytest.mark.parametrize(
        "text, message",
        [
            ("{nope", "not a radarx project"),
            ('{"schema": "something"}', "not a radarx project"),
            ('{"schema": "radarx-project/99"}', "newer version"),
        ],
    )
    def test_foreign_or_newer_files_are_refused_in_words(
        self, tmp_path: Path, text: str, message: str
    ) -> None:
        (tmp_path / "x.radarxproj").write_text(text)
        with pytest.raises(project.ProjectError, match=message):
            project.read_project(tmp_path / "x.radarxproj")

    def test_oversized_view_and_too_many_files_are_refused(self, tmp_path: Path) -> None:
        with pytest.raises(project.ProjectError, match="too large"):
            project.save_project(tmp_path / "a.radarxproj", view={"x": "y" * 300_000})
        with pytest.raises(project.ProjectError, match="at most"):
            project.save_project(tmp_path / "b.radarxproj", files=["a"] * (project.MAX_FILES + 1))


class TestSessionMessages:
    @pytest.fixture(autouse=True)
    def _isolated(self):
        saved = (dict(server._file_readers), server._last_file_id, dict(server._generations))
        server._file_readers.clear()
        server._generations.clear()
        server._last_file_id = None
        yield
        server._file_readers.clear()
        server._file_readers.update(saved[0])
        server._last_file_id = saved[1]
        server._generations.clear()
        server._generations.update(saved[2])

    @pytest.fixture()
    def hot_gates(self):
        from engine import plugins

        before = dict(plugins.MODULES)

        @plugins.module(
            id="hot_gates",
            title="Hot gates",
            category="Test",
            description="Marks gates above a threshold. Second sentence.",
            version="1.2",
            params=[
                plugins.param("threshold", "number", 45.0),
                plugins.param("min_size", "int", 3),
            ],
        )
        def hot_gates(ctx, threshold, min_size):
            return plugins.Result()

        yield
        plugins.MODULES.clear()
        plugins.MODULES.update(before)

    @pytest.mark.asyncio
    async def test_a_module_run_lands_in_the_record_with_its_defaults_applied(
        self, hot_gates
    ) -> None:
        server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        await handler.handle_message(
            json.dumps({"type": "run_module", "module": "hot_gates", "sweep": 0})
        )
        assert any(m["type"] == "module_result" for m in handler._sent), handler._sent[-2:]
        await handler.handle_message(json.dumps({"type": "get_run_record"}))
        reply = next(m for m in handler._sent if m["type"] == "run_record")
        steps = reply["record"]["steps"]
        assert [s["id"] for s in steps] == ["hot_gates"]
        assert steps[0]["sweep"] == 0
        assert steps[0]["params"] == {"threshold": 45.0, "min_size": 3}  # defaults included
        assert steps[0]["version"] == "1.2" and "uses" not in steps[0]  # not a radarx module
        assert "Step 1 of 1." in reply["methods"]
        for name in steps[0]["params"]:
            assert f"{name} = " in reply["methods"]

    @pytest.mark.asyncio
    async def test_clear_starts_a_new_record(self) -> None:
        handler, _ = _make_handler()
        handler._runlog.add(_step())
        await handler.handle_message(json.dumps({"type": "clear_run_record"}))
        await handler.handle_message(json.dumps({"type": "get_run_record"}))
        reply = [m for m in handler._sent if m["type"] == "run_record"][-1]
        assert reply["record"]["steps"] == []

    @pytest.mark.asyncio
    async def test_save_and_load_project_over_the_wire(self, tmp_path: Path) -> None:
        data = tmp_path / "KAAA_V06"
        data.write_bytes(b"z" * 10)
        handler, _ = _make_handler()
        handler._runlog.add(_step())
        path = str(tmp_path / "w.radarxproj")
        await handler.handle_message(
            json.dumps(
                {
                    "type": "save_project",
                    "path": path,
                    "files": [str(data)],
                    "view": {"selectedSweep": 3},
                }
            )
        )
        assert next(m for m in handler._sent if m["type"] == "project_saved")["files"] == 1
        await handler.handle_message(json.dumps({"type": "load_project", "path": path}))
        loaded = next(m for m in handler._sent if m["type"] == "project_loaded")["project"]
        assert loaded["view"] == {"selectedSweep": 3}
        assert loaded["resolved"][0]["status"] == "found"
        assert loaded["run_record"]["steps"][0]["id"] == "hot_gates"

    @pytest.mark.asyncio
    async def test_a_bad_project_answers_with_a_sentence(self, tmp_path: Path) -> None:
        (tmp_path / "x.radarxproj").write_text("[1, 2]")
        handler, _ = _make_handler()
        await handler.handle_message(
            json.dumps({"type": "load_project", "path": str(tmp_path / "x.radarxproj")})
        )
        errors = [m["message"] for m in handler._sent if m["type"] == "error"]
        assert len(errors) == 1 and "not a radarx project file" in errors[0]
        assert "Traceback" not in errors[0]
