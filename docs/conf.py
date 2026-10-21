"""Sphinx configuration for the Radarx Desktop documentation.

The published documentation is the user documentation only: how to install and use the app and the
command line, the science and the trust information. Pages for maintainers (building, releasing,
packaging, testing) stay in the source tree but are not published (``INTERNAL_PAGES`` below), and no
source code or API reference is generated.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# Builds honour SOURCE_DATE_EPOCH (set by scripts/release/build_docs_deposit.py to the commit time) so the
# dates printed in the documentation come from the sources, not from the moment of the build.
_EPOCH = os.environ.get("SOURCE_DATE_EPOCH")
NOW = (
    dt.datetime.fromtimestamp(int(_EPOCH), dt.timezone.utc) if _EPOCH else dt.datetime.now(dt.timezone.utc)
)

# -- project information -----------------------------------------------------------------------
project = "Radarx Desktop"
author = "Radarx Desktop by LabShope Technologies"
copyright = f"2025-{NOW.year}, {author}"
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
    "sphinx.ext.intersphinx",
    "sphinx.ext.mathjax",
    "sphinx_copybutton",
    "myst_nb",
]
myst_enable_extensions = ["colon_fence", "deflist", "dollarmath", "substitution", "attrs_inline"]
myst_heading_anchors = 3
source_suffix = {".rst": "restructuredtext", ".md": "myst-nb", ".ipynb": "myst-nb", ".myst": "myst-nb"}
myst_substitutions = {
    "release": release,
    "today": NOW.strftime("%Y-%m-%d"),
}

intersphinx_mapping = {
    "python": ("https://docs.python.org/3/", None),
    "numpy": ("https://numpy.org/doc/stable/", None),
    "xarray": ("https://docs.xarray.dev/en/stable/", None),
    "pandas": ("https://pandas.pydata.org/docs/", None),
    "radarx": ("https://radarx.readthedocs.io/en/latest/", None),
    "xradar": ("https://docs.openradarscience.org/projects/xradar/en/stable/", None),
}

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
    # The source repository is private: no repository links or "edit this page" buttons. The way back to
    # the product is its website (downloads, report an issue).
    "icon_links": [
        {"name": "Radarx", "url": "https://radarx.labshope.com", "icon": "fa-solid fa-house"},
        {"name": "Report an issue", "url": "https://radarx.labshope.com/report", "icon": "fa-solid fa-bug"},
    ],
    "show_toc_level": 2,
    "navigation_with_keys": False,
    "header_links_before_dropdown": 6,
    "secondary_sidebar_items": ["page-toc"],
}

# Pages for maintainers and operators: building, packaging, releasing, updating, testing, performance
# measurements, design notes, running it as a service. They stay in the source tree (scripts/check_links.py
# reads this list) and are not part of the published documentation.
INTERNAL_PAGES = [
    "API",
    "BUNDLING",
    "DESIGN",
    "PACKAGING_AUDIT",
    "PERFORMANCE",
    "PERFORMANCE_FRONTEND",
    "RELEASING",
    "RELIABILITY",
    "RENDER",
    "RESPONSIVENESS",
    "STABILITY",
    "UPDATES",
    "WEB",
    "contributing",
]

exclude_patterns = [
    "_build",
    "_api",
    "Thumbs.db",
    ".DS_Store",
    "**.ipynb_checkpoints",
    "changes",
    "generated",
    "render",
    "zenodo",
    "images/README*",
    *(f"{name}.md" for name in INTERNAL_PAGES),
]


# -- citation and image credits -------------------------------------------------------------------
# "How to cite" (cite.md) and "Images and third-party credits" (image-credits.md) include fragments made
# from one source each: docs/zenodo/zenodo_metadata.json + doi.json, and docs/zenodo/image_credits.csv +
# credit_sources.csv (see docs/zenodo/README.md, a maintainer page that is not published).
def _write_citation_and_credits() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("_radarx_citation", HERE / "zenodo" / "citation.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses look the module up while the class is made
    spec.loader.exec_module(mod)
    mod.write_generated(HERE / "generated", HERE, set(INTERNAL_PAGES), release.lstrip("v"))


_write_citation_and_credits()

# -- PDF (LaTeX) ------------------------------------------------------------------------------------
# The archived PDF (docs/zenodo/README.md): ``sphinx -b latex`` then latexmk with XeLaTeX, which handles the
# Unicode in the pages (arrows, Greek letters, box drawing) with the GNU FreeFont fonts of TeX Live.
latex_engine = "xelatex"
PDF_TITLE = "Radarx Desktop: User Documentation"
latex_documents = [(master_doc, "radarx-desktop-documentation.tex", PDF_TITLE, author, "manual", False)]
latex_show_urls = "footnote"
latex_show_pagerefs = False
latex_domain_indices = False
today = NOW.strftime("%Y-%m-%d")  # the date on the title page
latex_elements = {
    "papersize": "a4paper",
    "pointsize": "10pt",
    "extraclassoptions": "openany,oneside",
    "releasename": "Version",
    "printindex": "",
    "fvset": r"\fvset{fontsize=\footnotesize}",
    "sphinxsetup": "verbatimwrapslines=true, verbatimforcewraps=true, verbatimmaxunderfull=2, TitleColor={rgb}{0.05,0.2,0.35}",
    "preamble": r"""
\setcounter{tocdepth}{1}
% Tables: a smaller font so that columns with code names fit the page width.
\usepackage{etoolbox}
\AtBeginEnvironment{tabulary}{\footnotesize}
\AtBeginEnvironment{longtable}{\footnotesize}
\AtBeginEnvironment{tabular}{\footnotesize}
% Inline code (variable and option names) may break after an underscore, so narrow columns do not cut it.
\let\spxcodeorig\sphinxcode
\let\spxunderscoreorig\_
\renewcommand{\sphinxcode}[1]{\begingroup\def\_{\spxunderscoreorig\allowbreak}\spxcodeorig{#1}\endgroup}
\hypersetup{pdfauthor={Radarx Desktop by LabShope Technologies}, pdftitle={Radarx Desktop: User Documentation}, pdfcreator={Sphinx}, pdfsubject={User documentation}}
""",
}
