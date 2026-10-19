# Contributing

## Set up

```bash
git clone https://github.com/syedhamidali/xradar-desktop.git
cd xradar-desktop
npm install
cd python && python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev,radarx]" && cd ..
npm run tauri dev                 # run the desktop app
```

## Checks

```bash
(cd python && ruff check --config ../ruff.toml . && ruff format --config ../ruff.toml --check . && python -m pytest tests/)
npx svelte-check --threshold error && npx tsc --noEmit && npm run test:unit
```

## The documentation

Only the sources are in the repository (`docs/*.md`, `docs/conf.py`). HTML, the API pages written by `conf.py`
(`docs/_api/`) and executed notebook outputs are build products and are git-ignored, exactly as in
[radarx](https://github.com/syedhamidali/radarx).

```bash
pip install sphinx pydata-sphinx-theme myst-nb sphinx-copybutton ipykernel
cd docs && python -m sphinx -T -b html -d _build/doctrees . _build/html
```

Read the Docs builds `docs/conf.py` with the conda environment in `environment.yml` (`.readthedocs.yaml`).
Notebook outputs, when notebooks are added, are produced by the *Docs* GitHub Actions workflow and published on the
orphan branch `docs-cache`; the Read the Docs build downloads that cache and only runs notebooks whose code
changed, so its build stays short.

## Changelog

Add a fragment `docs/changes/<PR number>.md` (one or two lines) with your pull request; the fragments are
collected into {doc}`history` at build time.
