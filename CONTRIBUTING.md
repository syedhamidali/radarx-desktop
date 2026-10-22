# Contributing to radarx-desktop

Thank you for helping. This page is the short version: how to set up, what to run before a pull request, how
commits are written, how to add a module, and the terms your contribution comes under. The aim of the project is
radar software that is fast, stable and **faithful to the science**; the science rules are in
[docs/SCIENCE.md](docs/SCIENCE.md) and every pull request is checked against them.

## Ground rules

1. **Follow the science.** Read [docs/SCIENCE.md](docs/SCIENCE.md). Everything shown or written is observed,
   model/analysis, retrieved or stylised, says so, cites its method, shows its limits, and invents nothing.
2. **Measure, do not guess.** Speed and stability claims come with numbers
   ([docs/PERFORMANCE.md](docs/PERFORMANCE.md), [docs/PERFORMANCE_FRONTEND.md](docs/PERFORMANCE_FRONTEND.md)).
3. **Test against known answers.** A scientific change comes with a test whose expected value does not come from
   running the code under test.
4. **Small pull requests** that do one thing. Open an issue first for anything large or that changes behaviour.
5. **No downloads behind the user's back**, no secrets in the repository, no radar data committed.

## Set up

You need Node.js 20+, Rust (stable), Python 3.12+ and, on Linux, the WebKit/GTK development
packages (see the [README](README.md#build-from-source)).

```bash
git clone https://github.com/syedhamidali/radarx-desktop.git
cd radarx-desktop
npm ci
cd python && python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev,radarx]" && cd ..
npm run tauri dev          # the desktop app (uses python/.venv automatically)
npm run dev:web            # the web build against an engine you started yourself
```

Test radar files come from [open-radar-data](https://github.com/openradar/open-radar-data); the tests skip when
they are not available, and CI downloads them (`XRADAR_TEST_DOWNLOAD=1`). Never commit radar data.

## Checks to run before you push

```bash
# Python engine
(cd python && ruff check --config ../ruff.toml . && ruff format --config ../ruff.toml --check . && python -m pytest tests/)
# Frontend
npx svelte-check --tsconfig ./tsconfig.json && npx tsc --noEmit && npm run test:unit && npm run build
# Rust shell (when you touched src-tauri)
(cd src-tauri && cargo check && cargo clippy -- -D warnings)
# Documentation (when you touched docs/, README or a docstring that is documented)
(cd docs && python -m sphinx -T -b html -d _build/doctrees -w _build/warnings.txt . _build/html)   # no new warnings
python scripts/check_links.py                                                                  # Markdown links
# Licences (when you added or changed a dependency, font, colour map or imagery source)
python scripts/licenses/generate_notices.py && python scripts/licenses/generate_notices.py --check
```

CI runs the same checks on Linux and macOS ([.github/workflows](.github/workflows)). For anything that looks
different on screen, run the app and **look at it**, in dark and light, and attach a screenshot to the pull
request.

## Commits and pull requests

* One logical change per commit; the subject says what changed and why it matters to a user, in the imperative
  or plain past tense, with no trailing full stop needed ("RHI: keep the noise menu inside the window").
* **Sign off your commits** (`git commit -s`), which adds a `Signed-off-by: Your Name <you@example.org>` line. It
  states that you wrote the change or have the right to submit it, as in the
  [Developer Certificate of Origin](DCO). Sign-off is recommended; a pull request that is not signed off may be
  asked to add it.
* **No AI attribution trailers** (no `Co-Authored-By: <an AI tool>` lines, no "Generated with ..." footers) in
  commit messages: this is the maintainer's rule for this repository. The commit's authors and the sign-off are
  people who answer for the change.
* Add a changelog fragment `docs/changes/<PR number>.md` (one or two lines) with the pull request. Fragments are
  gathered into the history page at build time.
* Fill in the [pull request template](.github/pull_request_template.md): it carries the science, testing and
  performance checklists.
* Do not bump the version, tag or publish releases in a pull request; the maintainer does that.

## Code made with the help of generated-code tools

You are responsible for everything you submit, however it was written. Read it, understand it, run it, test it.
Do not submit text or code you cannot show you have the right to contribute: generated output can resemble
third-party code under a licence this project cannot accept, so check anything non-trivial. Generated
algorithms get the same science review as hand-written ones: a method is cited, tested against known answers and
shows its limits. Whether and how to mention the use of such tools in the pull request description is left to
you (the maintainer may ask). *This section is to be reviewed by a lawyer.*

## Licence and contributor terms

radarx-desktop is source-available under the
[PolyForm Noncommercial License 1.0.0](LICENSE); commercial use needs a separate written licence from the
copyright holder (see [NOTICE](NOTICE)). By submitting a contribution you agree that it is licensed under the
same terms as the project, and you confirm the [Developer Certificate of Origin](DCO) for it by signing off.

> **To be reviewed by a lawyer.** The DCO text is the standard one and refers to an "open source license"; the
> project licence is a source-available, noncommercial one, and the maintainer reserves the right to license the
> software commercially. Whether sign-off is enough for that, or whether a contributor licence agreement is
> needed, has not been decided. Until it is, contributions are accepted on the terms above and nothing on this
> page is legal advice.

Dependencies, fonts, colour maps and imagery you add come under their own licences; they must pass the check in
[docs/LICENSES.md](docs/LICENSES.md), and non-commercial or share-alike sources are flagged there and in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## How to add a module (an algorithm)

A module is one Python file with a decorator; it appears in the Modules panel with a generated form and result
views, with no server or UI changes. Full guide: [docs/MODULES.md](docs/MODULES.md).

1. Copy `python/modules/_template.py` to `python/modules/my_module.py` (or to `~/.xradar-desktop/modules/` while
   you experiment: it loads at the next start or after **Reload** in the panel).
2. Declare `id`, `title`, a `description` that states the method **and its limits**, the fields it `requires`
   (canonical names such as `DBZH`, `ZDR`, `KDP`, `RHOHV`, `VRADH`) and its `params`, then write `run(ctx, ...)`.
   Return a `Result` (scalars, tables, an overlay, a gridded field, notes).
3. Meet the science rules for a **RETRIEVED** product: the method is cited in the docstring and in the docs, the
   parameters and defaults are recorded in the result, gaps stay gaps, units are stated.
4. Add a test in `python/tests/` (see `test_radarx_modules.py`, `test_plugins.py`) with a synthetic case whose
   answer you know and, if behaviour depends on real data, a real file from open-radar-data.
5. Mention it in the docs ([docs/MODULES.md](docs/MODULES.md) or its own page, added to the `toctree`), run the
   checks above, and open a pull request with the checklist filled in.

## Documentation

Only sources are in the repository (`docs/*.md`, `docs/conf.py`); HTML and the API pages written by `conf.py` are
build products. See [docs/contributing.md](docs/contributing.md) for how the documentation is built and
published. A new page must be added to a `toctree` reachable from `docs/index.md` (a section page such as `docs/user-guide.md`), or the build warns.

## Reporting problems

Use *Report an issue* in the app (About, the command palette or the Account dialog) or the form at
<https://radarx.labshope.com/report>: choose *Bug report*, *Feature request* or *Science question* (for "is this
the right physics / the right number?"). Do not attach data you do not have the right to share, and never post
tokens or keys. A security problem goes through the same form; say that it is a security problem and do not
publish details.
