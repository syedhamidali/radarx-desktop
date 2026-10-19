"""User-supplied files (model output, soundings, gauge tables ...) used in place.

No download, no copy and no network: the provider only checks the files exist and records their
size and checksum, so the same code path (request -> fetch -> provenance) serves local and remote
data. Because the origin of such a file is unknown to the app, **the user declares what kind of
data it is** (``params["kind"]``); the default is ``observed`` and the provenance text says the
class was declared by the user.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from engine import layer_provenance, provenance
from engine.environment.errors import InvalidRequest
from engine.environment.provider import Provider
from engine.environment.request import FileSpec, InventoryItem, Request

MAX_FILES = 256


class LocalFilesProvider(Provider):
    id: ClassVar[str] = "local_files"
    name: ClassVar[str] = "Files on this computer"
    kind: ClassVar[str] = "observed"
    attribution: ClassVar[str] = "User-supplied files (origin as declared by the user)."
    licence: ClassVar[str] = "Whatever applies to your files."
    homepage: ClassVar[str] = ""
    description: ClassVar[str] = "Use files you already have: nothing is copied or uploaded."
    requires_network: ClassVar[bool] = False

    def inventory(self, query: dict[str, Any] | None = None) -> list[InventoryItem]:
        return [
            InventoryItem(
                product="file",
                title="Local file(s)",
                description="Pass absolute paths in params.paths and say what the data is "
                "with params.kind (observed, model, retrieved, simulated, stylised).",
            )
        ]

    @staticmethod
    def _paths(request: Request) -> list[Path]:
        raw = request.params.get("paths")
        if not isinstance(raw, list) or not raw:
            raise InvalidRequest("Local files: give params.paths (a list of absolute file paths).")
        if len(raw) > MAX_FILES:
            raise InvalidRequest(f"Local files: at most {MAX_FILES} files per request.")
        out: list[Path] = []
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                raise InvalidRequest("Local files: every path must be a non-empty string.")
            p = Path(item).expanduser()
            if not p.is_absolute() or ".." in p.parts:
                raise InvalidRequest(f"Local files: {item!r} is not an absolute path.")
            if not p.is_file():
                raise InvalidRequest(f"Local files: {p.name} was not found or is not a file.")
            out.append(p)
        return out

    def plan(self, request: Request, env: Any) -> list[FileSpec]:
        kind = request.params.get("kind", self.kind)
        if kind not in layer_provenance.CLASSES:
            raise InvalidRequest(f"Local files: kind must be one of {layer_provenance.CLASSES}.")
        return [
            FileSpec(
                key=str(p), name=p.name, url=None, size=p.stat().st_size, meta={"path": str(p)}
            )
            for p in self._paths(request)
        ]

    def local_path(self, spec: FileSpec) -> Path | None:
        path = spec.meta.get("path")
        return Path(path) if path else None

    def provenance(self, request: Request, specs: list[FileSpec]) -> dict[str, Any]:
        kind = str(request.params.get("kind", self.kind))
        names = provenance.source_names([s.name for s in specs])
        return layer_provenance.LayerProvenance(
            kind=kind,
            source="User-supplied file(s): "
            + ", ".join(names[:5])
            + ("..." if len(names) > 5 else ""),
            title=str(request.params.get("title") or "") or "Local data",
            valid_time=request.time_start or "",
            valid_end=request.time_end or "",
            method="Read in place from your computer. The data class was declared by the user; "
            "radarx-desktop cannot verify it.",
            attribution=str(request.params.get("attribution") or self.attribution),
            licence=str(request.params.get("licence") or ""),
        ).to_dict()
