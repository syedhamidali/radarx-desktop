"""One source for the citation of the documentation and for its image credits.

The citation formats (plain/APA, BibTeX, RIS) are all made from ``zenodo_metadata.json`` (title, creator,
version, publisher) and ``doi.json`` (the DOIs Zenodo assigned), so they cannot drift from each other or
from the Zenodo record. The image credits are made from ``image_credits.csv`` (one row per image, from the
audit of every picture) and ``credit_sources.csv`` (the credit line and terms of each third-party source).

``docs/conf.py`` calls :func:`write_generated` before every build; it writes Markdown fragments that the
published pages ``cite.md`` and ``image-credits.md`` include. Standard library only.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOCS = HERE.parent

PUBLISHER = "Zenodo"
WEBSITE = "https://radarx.labshope.com"
#: Shown while no DOI has been assigned. Never replaced by an invented value.
DOI_PLACEHOLDER = "10.5281/zenodo.XXXXXXX"


@dataclass(frozen=True)
class Citation:
    title: str
    creator: str
    version: str
    year: str
    concept_doi: str = ""
    version_doi: str = ""
    versions: dict[str, str] = field(default_factory=dict)

    @property
    def concept(self) -> str:
        return self.concept_doi or DOI_PLACEHOLDER

    @property
    def this_version(self) -> str:
        return self.version_doi or self.versions.get(self.version, "") or DOI_PLACEHOLDER


def build_year() -> str:
    """The year of the build: SOURCE_DATE_EPOCH when set (reproducible builds), else today (UTC)."""
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    now = (
        dt.datetime.fromtimestamp(int(epoch), dt.timezone.utc)
        if epoch
        else dt.datetime.now(dt.timezone.utc)
    )
    return str(now.year)


def load(version: str | None = None, folder: Path = HERE) -> Citation:
    meta = json.loads((folder / "zenodo_metadata.json").read_text(encoding="utf-8"))["metadata"]
    dois = json.loads((folder / "doi.json").read_text(encoding="utf-8"))
    for value in [dois.get("concept_doi", ""), *dois.get("versions", {}).values()]:
        if value and not re.fullmatch(r"10\.\d{4,9}/\S+", value):
            raise ValueError(f"doi.json: {value!r} is not a DOI (10.xxxx/...)")
    return Citation(
        title=meta["title"],
        creator=meta["creators"][0]["name"],
        version=version or meta.get("version", ""),
        year=build_year(),
        concept_doi=dois.get("concept_doi", ""),
        versions=dict(dois.get("versions", {})),
    )


def _doi_url(doi: str) -> str:
    return f"https://doi.org/{doi}"


def apa(c: Citation, *, specific: bool = False) -> str:
    """APA 7 style. The concept citation names no version (it always resolves to the latest)."""
    if specific:
        return (
            f"{c.creator}. ({c.year}). *{c.title}* (Version {c.version}) [Software documentation]. "
            f"{PUBLISHER}. {_doi_url(c.this_version)}"
        )
    return f"{c.creator}. ({c.year}). *{c.title}* [Software documentation]. {PUBLISHER}. {_doi_url(c.concept)}"


def bibtex(c: Citation, *, specific: bool = False) -> str:
    doi = c.this_version if specific else c.concept
    key = "radarx_desktop_docs" + (f"_{re.sub(r'[^0-9A-Za-z]+', '_', c.version)}" if specific else "")
    lines = [
        f"@manual{{{key},",
        f"  author       = {{{{{c.creator}}}}},",  # double braces: an organisation, not a person
        f"  title        = {{{{{c.title}}}}},",
        f"  year         = {{{c.year}}},",
        f"  publisher    = {{{PUBLISHER}}},",
    ]
    if specific:
        lines.append(f"  version      = {{{c.version}}},")
    lines += [
        f"  doi          = {{{doi}}},",
        f"  url          = {{{_doi_url(doi)}}},",
        "  note         = {Software documentation}",
        "}",
    ]
    return "\n".join(lines)


def ris(c: Citation, *, specific: bool = False) -> str:
    doi = c.this_version if specific else c.concept
    lines = [
        "TY  - GEN",
        f"AU  - {c.creator}",
        f"TI  - {c.title}",
        f"PY  - {c.year}",
        f"PB  - {PUBLISHER}",
        "M3  - Software documentation",
    ]
    if specific:
        lines.append(f"ET  - {c.version}")
    lines += [f"DO  - {doi}", f"UR  - {_doi_url(doi)}", "LA  - en", "ER  - "]
    return "\n".join(lines)


def citation_markdown(c: Citation) -> str:
    """The generated part of the 'How to cite' page."""
    status = (
        ""
        if c.concept_doi
        else (
            "\n:::{note}\nThe DOI is not assigned yet: `10.5281/zenodo.XXXXXXX` is a placeholder that is replaced by "
            "the real DOI once the documentation is deposited.\n:::\n"
        )
    )
    known = sorted(c.versions.items(), key=lambda kv: [int(p) if p.isdigit() else p for p in re.split(r"[.-]", kv[0])])
    version_rows = "\n".join(f"| {v} | [{d}]({_doi_url(d)}) |" for v, d in known) or "| (none yet) | |"
    return f"""## Recommended citation

DOI: **{c.concept}** (the concept DOI: always the latest version)
{status}
{apa(c)}

### BibTeX

```bibtex
{bibtex(c)}
```

### RIS

```text
{ris(c)}
```

## Citing a specific version

Each release of the documentation is a new version of the same record, with a DOI of its own. To cite the
exact text you read (version {c.version} here), use that version's DOI:

{apa(c, specific=True)}

```bibtex
{bibtex(c, specific=True)}
```

```text
{ris(c, specific=True)}
```

| Version | DOI |
| --- | --- |
{version_rows}
"""


# -- image credits --------------------------------------------------------------------------------


def read_sources(folder: Path = HERE) -> dict[str, dict[str, str]]:
    with (folder / "credit_sources.csv").open(encoding="utf-8", newline="") as fh:
        return {row["source"]: row for row in csv.DictReader(fh)}


def read_images(folder: Path = HERE) -> list[dict[str, str]]:
    with (folder / "image_credits.csv").open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


IMAGE_REF = re.compile(r"images/[\w./-]+\.(?:png|gif|jpg|jpeg|svg)")


def image_pages(docs: Path, internal: set[str]) -> dict[str, list[str]]:
    """Image path -> titles of the published pages that show it."""
    out: dict[str, list[str]] = {}
    for page in sorted(docs.glob("*.md")):
        if page.stem in internal:
            continue
        text = page.read_text(encoding="utf-8")
        m = re.search(r"^# (.+)$", text, re.M)
        title = m.group(1).strip() if m else page.stem
        for ref in IMAGE_REF.findall(text):
            pages = out.setdefault(ref, [])
            if title not in pages:
                pages.append(title)
    return out


def credits_markdown(docs: Path, internal: set[str], folder: Path = HERE) -> str:
    sources = read_sources(folder)
    rows = {r["image"]: r for r in read_images(folder)}
    used = image_pages(docs, internal)
    missing = sorted(set(used) - set(rows))
    if missing:
        raise ValueError(f"image_credits.csv has no row for {missing}")
    for r in rows.values():
        for token in filter(None, r["sources"].split(";")):
            if token not in sources:
                raise ValueError(f"image_credits.csv: unknown source {token!r} for {r['image']}")

    lines = [
        "## Sources and their terms",
        "",
        "| Source | Credit line | Terms |",
        "| --- | --- | --- |",
    ]
    for s in sources.values():
        link = f" ([terms]({s['link']}))" if s["link"] else ""
        lines.append(f"| {s['short']} | {s['credit']} | {s['terms']}{link} |")
    lines += [
        "",
        "## Figures with third-party content",
        "",
        "Figures not listed here contain no third-party map, imagery or data.",
        "",
        "| Figure | Page | Contains | Credit shown in the figure |",
        "| --- | --- | --- | --- |",
    ]
    no = 0
    for image in sorted(used, key=lambda i: (used[i][0], i)):
        tokens = [t for t in rows[image]["sources"].split(";") if t]
        if not tokens:
            continue
        no += 1
        name = image.split("/")[-1].rsplit(".", 1)[0]
        what = "; ".join(sources[t]["short"] for t in tokens)
        shown = {"yes": "yes", "no": "no (credited here)"}.get(rows[image]["attribution_in_frame"], "")
        lines.append(f"| {name} | {', '.join(used[image])} | {what} | {shown} |")
    lines.append("")
    return "\n".join(lines)


def write_generated(out: Path, docs: Path, internal: set[str], version: str) -> None:
    """Write the fragments the published pages include (``generated/`` is not committed)."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "cite-formats.md").write_text(citation_markdown(load(version)), encoding="utf-8")
    (out / "image-credits-table.md").write_text(credits_markdown(docs, internal), encoding="utf-8")
