"""Sphinx configuration for the xradar-desktop documentation.

Mirrors the setup of radarx: only sources live in the repository. The HTML, the API pages written by
this file (``docs/_api/``) and executed notebook outputs are build products and are git-ignored.
"""

from __future__ import annotations

import datetime as dt
import importlib
import os
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "python"))  # the engine package, importable without installing

# -- project information -----------------------------------------------------------------------
project = "xradar-desktop"
author = "Hamid Ali Syed"
copyright = f"2025-{dt.datetime.now(dt.timezone.utc).year}, {author}"
html_title = project
master_doc = "index"

# One version everywhere: package.json (kept in step with tauri.conf.json and Cargo.toml by the
# release bump). On Read the Docs tag builds use the tag itself.
release = os.environ.get("READTHEDOCS_GIT_IDENTIFIER", "") if os.environ.get(
    "READTHEDOCS_VERSION_TYPE"
) == "tag" else ""
if not release:
    import json

    release = json.loads((ROOT / "package.json").read_text())["version"]
version = ".".join(release.lstrip("v").split(".")[:2])

# -- extensions ---------------------------------------------------------------------------------
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.viewcode",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.mathjax",
    "sphinx.ext.extlinks",
    "sphinx_copybutton",
    "myst_nb",
]
myst_enable_extensions = ["colon_fence", "deflist", "dollarmath", "substitution", "attrs_inline"]
myst_heading_anchors = 3
source_suffix = {".rst": "restructuredtext", ".md": "myst-nb", ".ipynb": "myst-nb", ".myst": "myst-nb"}
myst_substitutions = {
    "release": release,
    "today": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"),
}

extlinks = {
    "issue": ("https://github.com/syedhamidali/xradar-desktop/issues/%s", "GH %s"),
    "pull": ("https://github.com/syedhamidali/xradar-desktop/pull/%s", "PR %s"),
}
intersphinx_mapping = {
    "python": ("https://docs.python.org/3/", None),
    "numpy": ("https://numpy.org/doc/stable/", None),
    "xarray": ("https://docs.xarray.dev/en/stable/", None),
    "pandas": ("https://pandas.pydata.org/docs/", None),
    "radarx": ("https://radarx.readthedocs.io/en/latest/", None),
    "xradar": ("https://docs.openradar.io/projects/xradar/en/stable/", None),
}

# -- API reference (written at build time, git-ignored) ------------------------------------------
# Public modules of the engine, in reading order. Only those that import are documented, so the build
# also works while a module is being added or when an optional dependency is missing.
API_MODULES = [
    ("engine.api", "Python API"),
    ("engine.plugins", "Writing modules (plug-in SDK)"),
    ("engine.reader", "Reading radar files"),
    ("engine.varmap", "Variable mapping"),
    ("engine.pipeline", "Processing pipelines"),
    ("engine.derived", "Derived variables"),
    ("engine.mosaic", "Mosaics"),
    ("engine.multidoppler", "Multi-Doppler winds"),
    ("engine.cells", "Storm cells and tracking"),
    ("engine.exporter", "Figures and export"),
    ("engine.cli", "Command line"),
]
autoclass_content = "both"
autodoc_member_order = "bysource"
autodoc_default_options = {"members": True, "show-inheritance": True}
autodoc_mock_imports = ["radarx", "icechunk", "boto3"]
napoleon_numpy_docstring = True
napoleon_google_docstring = False


def _write_api_pages() -> None:
    out = HERE / "_api"
    out.mkdir(exist_ok=True)
    listed: list[str] = []
    for mod, title in API_MODULES:
        try:
            importlib.import_module(mod)
        except Exception as exc:  # noqa: BLE001 - optional modules / missing optional deps
            print(f"[docs] skipping API page for {mod}: {type(exc).__name__}: {exc}")
            continue
        name = mod.split(".")[-1]
        (out / f"{name}.rst").write_text(
            f"{title}\n{'=' * len(title)}\n\n.. automodule:: {mod}\n"
        )
        listed.append(name)
    index = "Library reference\n=================\n\n.. toctree::\n   :maxdepth: 2\n\n"
    index += "".join(f"   {n}\n" for n in listed)
    (out / "index.rst").write_text(index)


_write_api_pages()


def _write_unreleased_changes() -> None:
    """Collect the changelog fragments ``changes/<PR>.md`` into ``changes/unreleased.md``."""
    frag = sorted((HERE / "changes").glob("[0-9]*.md"), key=lambda p: int(p.stem.split("-")[0]))
    lines = [ln.rstrip() for p in frag for ln in p.read_text().splitlines() if ln.strip()]
    (HERE / "changes").mkdir(exist_ok=True)
    (HERE / "changes" / "unreleased.md").write_text(
        "\n".join(lines) + "\n" if lines else "No changes yet.\n"
    )


_write_unreleased_changes()

# -- notebooks ----------------------------------------------------------------------------------
# Same scheme as radarx: executed outputs are NOT in the repository. The "Docs" workflow executes the
# notebooks and publishes a jupyter-cache on the orphan branch ``docs-cache``; Read the Docs reads
# that cache and only executes notebooks whose code changed.
nb_execution_mode = "cache"
nb_execution_cache_path = os.environ.get(
    "XRADAR_DOCS_JUPYTER_CACHE", str(HERE / "_build" / "jupyter_cache")
)
nb_execution_kernel_name = "python3"
nb_execution_in_temp = True
nb_execution_timeout = 600
nb_execution_raise_on_error = True

# -- HTML ---------------------------------------------------------------------------------------
html_theme = "pydata_sphinx_theme"
html_static_path = ["_static"] if (HERE / "_static").exists() else []
html_theme_options = {
    "github_url": "https://github.com/syedhamidali/xradar-desktop",
    "show_toc_level": 2,
    "navigation_with_keys": False,
    "secondary_sidebar_items": ["page-toc", "edit-this-page"],
}
html_context = {
    "github_user": "syedhamidali",
    "github_repo": "xradar-desktop",
    "github_version": "main",
    "doc_path": "docs",
}

exclude_patterns = [
    "_build",
    "Thumbs.db",
    ".DS_Store",
    "**.ipynb_checkpoints",
    "changes",
    "render",
    "images/README*",
]
# pyproject (read for the package metadata page only)
_pyproject = tomllib.loads((ROOT / "python" / "pyproject.toml").read_text())
