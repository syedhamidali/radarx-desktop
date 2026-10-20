# PyInstaller spec for the radarx-desktop Python engine ("sidecar").
#
# Produces dist/xradar-engine/ (one directory: the executable plus its libraries), which the Tauri
# app ships as a resource and launches. Build with ``python packaging/build_engine.py``.
#
# The plugin modules (python/modules/*.py) are loaded by file path at run time, so PyInstaller
# cannot see what they import: every library they (or the engine) import lazily is listed under
# ``hiddenimports`` / collected below.
import importlib.util
import os
import sys

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # the python/ directory


def installed(name):
    return importlib.util.find_spec(name) is not None


def _not_a_test(name):
    """collect_submodules() filter: the packages' own test suites are never imported at run time."""
    return not {"tests", "test"} & set(name.split("."))


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
    hiddenimports += collect_submodules(pkg, filter=_not_a_test)
    datas += collect_data_files(pkg)
    binaries += collect_dynamic_libs(pkg)
    try:
        datas += copy_metadata(pkg)  # entry points (xarray backends, fsspec protocols) live here
    except Exception:
        pass

hiddenimports += ["scipy.spatial", "scipy.ndimage", "scipy.sparse.csgraph", "PIL.Image"]
# The account gate (engine/account/tokens.py) imports PyJWT and, through it, cryptography on first use;
# certifi's CA bundle is what the sign-in HTTPS calls trust.
hiddenimports += collect_submodules("jwt")
hiddenimports += ["cryptography.hazmat.primitives.asymmetric.ec", "cryptography.hazmat.primitives.asymmetric.rsa"]
datas += collect_data_files("certifi")
# matplotlib picks its output backend by name at savefig() time, which PyInstaller cannot see: without
# these, SVG and PDF export failed in the packaged engine ("No module named backend_svg").
hiddenimports += [
    "matplotlib.backends.backend_agg",
    "matplotlib.backends.backend_svg",
    "matplotlib.backends.backend_pdf",
    "matplotlib.backends.backend_ps",
]

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
    # Packaging tools and stdlib doc data that matplotlib/pyinstaller hooks drag in.
    "setuptools",
    "pkg_resources",
    "_distutils_hack",
    "pydoc_data",
    # GUI toolkits for matplotlib's interactive backends: the engine only ever uses Agg
    # (exporter.py), which draws to memory and writes PNG/SVG/PDF.
    "matplotlib.backends.backend_qt",
    "matplotlib.backends.backend_qt5",
    "matplotlib.backends.backend_qtagg",
    "matplotlib.backends.backend_qt5agg",
    "matplotlib.backends.backend_qtcairo",
    "matplotlib.backends.backend_qt5cairo",
    "matplotlib.backends.qt_compat",
    "matplotlib.backends.qt_editor",
    "matplotlib.backends.backend_tkagg",
    "matplotlib.backends.backend_tkcairo",
    "matplotlib.backends._backend_tk",
    "matplotlib.backends.backend_gtk3",
    "matplotlib.backends.backend_gtk3agg",
    "matplotlib.backends.backend_gtk3cairo",
    "matplotlib.backends.backend_gtk4",
    "matplotlib.backends.backend_gtk4agg",
    "matplotlib.backends.backend_gtk4cairo",
    "matplotlib.backends.backend_wx",
    "matplotlib.backends.backend_wxagg",
    "matplotlib.backends.backend_wxcairo",
    "matplotlib.backends.backend_webagg",
    "matplotlib.backends.backend_webagg_core",
    "matplotlib.backends.backend_nbagg",
    # pyarrow (~55 MB): only engine/arrow_bridge.py uses it, and the app never imports that (the
    # WebSocket sends raw binary chunks). pandas treats it as optional.
    "pyarrow",
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
    r"\.(pyx|pxd|pxi|h|hpp|c|cc|cpp|md|rst|pyi|a)$",
    r"(^|/)(tests|test)/",  # test suites of numpy / scipy / pandas / xarray / ...
    r"(^|/)matplotlib/mpl-data/(sample_data|images|stylelib|plot_directive)",
    r"(^|/)matplotlib/backends/(web_backend|_backend_tk)",
    r"(^|/)(pandas|scipy|numpy)/.*/tests/",
    r"(^|/)llvmlite/.*\.(h|lib)$",
    # Bytecode caches that the developer's python/modules/ folder may contain.
    r"(^|/)__pycache__/",
    r"\.py[co]$",
    # Pillow plugins for formats the engine never reads or writes (it makes PNG / GIF; matplotlib
    # writes PNG / SVG / PDF) and the libraries only they use. Pillow tolerates their absence.
    r"(^|/)PIL/_(avif|webp|imagingcms|imagingtk)\.",
    r"(^|/)(PIL/\.dylibs|pillow\.libs)/(lib)?(avif|webp|webpmux|webpdemux|lcms2)[.-]",
    # Computer Modern fonts (only for mathtext.fontset = 'cm') and DejaVu Serif (font.family = serif);
    # the default sans-serif DejaVu Sans, Mono and the STIX fonts stay.
    r"(^|/)matplotlib/mpl-data/fonts/ttf/(cm[a-z]+10|DejaVuSerif[A-Za-z-]*)\.ttf$",
]
_drop = [re.compile(p) for p in _DROP_PATTERNS]


def _service_dropped(dest):
    m = re.match(r"(?:.*/)?(?:botocore|boto3)/data/([^/]+)/", dest)
    return bool(m) and m.group(1) not in _KEEP_SERVICES and not m.group(1).startswith("_")


def _keep(entry):
    dest = entry[0].replace("\\", "/")
    return not (any(r.search(dest) for r in _drop) or _service_dropped(dest))


def _link_duplicates(entries):
    """Turn byte-identical shared libraries in the same folder into symlinks to the first one.

    Some wheels ship ``libfoo.dylib``, ``libfoo.2600.dylib`` and ``libfoo.2600.0.0.dylib`` as three
    full copies (pyarrow: libarrow_python, 3 x 1.9 MB). PyInstaller re-signs every copy separately on
    macOS, so this has to happen on the TOC, before COLLECT, not afterwards. Not on Windows.
    """
    if os.name == "nt":
        return entries
    import hashlib

    seen, out = {}, []
    for dest, src, typecode in sorted(entries, key=lambda e: e[0]):
        folder = os.path.dirname(dest)
        if typecode in ("BINARY", "EXTENSION") and os.path.isfile(src) and os.path.getsize(src) >= 64 * 1024:
            with open(src, "rb") as f:
                key = (folder, hashlib.sha256(f.read()).hexdigest())
            if key in seen:
                out.append((dest, os.path.basename(seen[key]), "SYMLINK"))
                continue
            seen[key] = dest
        out.append((dest, src, typecode))
    return out


def _is_test_module(name):
    return bool({"tests", "test"} & set(name.split(".")))


a.binaries = _link_duplicates([e for e in a.binaries if _keep(e)])
a.datas = [e for e in a.datas if _keep(e)]
# PyInstaller's own hooks collect whole packages including their test suites (xarray.tests alone
# is 3 MB of source); nothing imports those at run time.
a.pure = [e for e in a.pure if not _is_test_module(e[0])]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="xradar-engine",
    debug=False,
    strip=False,  # (the executable is only the bootloader plus the archive: nothing to strip)
    upx=False,
    # A console program: the app reads READY:<port> from its stdout. The Tauri shell starts it
    # without a visible window on Windows.
    console=True,
)
# strip drops local symbols from the bundled shared libraries (-20..25 % on the large ones, e.g.
# icechunk). macOS only: on Linux it corrupts numpy's bundled OpenBLAS ("ELF load command
# address/offset not page-aligned") and on Windows it breaks python312.dll. PyInstaller re-signs
# the result on macOS.
coll = COLLECT(
    exe, a.binaries, a.datas, strip=(sys.platform == "darwin"), upx=False, name="xradar-engine"
)
