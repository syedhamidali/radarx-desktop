# PyInstaller spec for the xradar-desktop Python engine ("sidecar").
#
# Produces dist/xradar-engine/ (one directory: the executable plus its libraries), which the Tauri
# app ships as a resource and launches. Build with ``python packaging/build_engine.py``.
#
# The plugin modules (python/modules/*.py) are loaded by file path at run time, so PyInstaller
# cannot see what they import: every library they (or the engine) import lazily is listed under
# ``hiddenimports`` / collected below.
import importlib.util
import os

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # the python/ directory


def installed(name):
    return importlib.util.find_spec(name) is not None


hiddenimports = []
datas = [(os.path.join(ROOT, "modules"), "modules")]
binaries = []

# Packages that discover parts of themselves at run time (plugins, entry points, data files).
for pkg in (
    "xradar",
    "xarray",
    "radarx",
    "cmweather",
    "colorcet",
    "pyproj",
    "zarr",
    "icechunk",
    "s3fs",
    "fsspec",
    "h5netcdf",
    "netCDF4",
    "numcodecs",
):
    if not installed(pkg):
        continue
    hiddenimports += collect_submodules(pkg)
    datas += collect_data_files(pkg)
    binaries += collect_dynamic_libs(pkg)
    try:
        datas += copy_metadata(pkg)  # entry points (xarray backends, fsspec protocols) live here
    except Exception:
        pass

hiddenimports += ["scipy.spatial", "scipy.ndimage", "scipy.sparse.csgraph", "PIL.Image", "cairo"]

excludes = [
    # Never imported by the engine, and large. (The renderer rasterises with numpy; keeping
    # datashader out also keeps numba + llvmlite, ~130 MB, out.)
    "datashader",
    "numba",
    "llvmlite",
    "bokeh",
    "distributed",
    "IPython",
    "jupyter",
    "notebook",
    "pytest",
    "tkinter",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "sphinx",
]

a = Analysis(
    [os.path.join(ROOT, "server.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)


# ---------------------------------------------------------------------------
# Slimming: drop files the engine never loads. Everything dropped here was checked against the
# smoke test (packaging/build_engine.py) that starts the frozen engine and lists its modules.
# ---------------------------------------------------------------------------
import re

# boto3/botocore ship a JSON model for every AWS service (~20 MB); the engine only talks to S3.
_KEEP_SERVICES = {"s3", "sts", "sso", "sso-oidc", "signin"}
_DROP_PATTERNS = [
    r"(^|/)pyarrow/(libarrow_flight|libarrow_substrait|libgandiva|_flight|_substrait|_gandiva)",
    r"(^|/)pyarrow/include/",
    r"(^|/)pyarrow/(tests|_generated_version)",
    r"\.(pyx|pxd|pxi|h|hpp|c|cc|cpp|md|rst|pyi|a)$",
    r"(^|/)(tests|test)/",  # test suites of numpy / scipy / pandas / xarray / ...
    r"(^|/)matplotlib/mpl-data/(sample_data|images|stylelib|plot_directive)",
    r"(^|/)matplotlib/backends/(web_backend|_backend_tk)",
    r"(^|/)(pandas|scipy|numpy)/.*/tests/",
    r"(^|/)llvmlite/.*\.(h|lib)$",
]
_drop = [re.compile(p) for p in _DROP_PATTERNS]


def _service_dropped(dest):
    m = re.match(r"(?:.*/)?(?:botocore|boto3)/data/([^/]+)/", dest)
    return bool(m) and m.group(1) not in _KEEP_SERVICES and not m.group(1).startswith("_")


def _keep(entry):
    dest = entry[0].replace("\\", "/")
    return not (any(r.search(dest) for r in _drop) or _service_dropped(dest))


a.binaries = [e for e in a.binaries if _keep(e)]
a.datas = [e for e in a.datas if _keep(e)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="xradar-engine",
    debug=False,
    strip=False,
    upx=False,
    # A console program: the app reads READY:<port> from its stdout. The Tauri shell starts it
    # without a visible window on Windows.
    console=True,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="xradar-engine")
