## What and why

<!-- What does this change for a user? Link the issue ("Closes #N" / "Part of #N"). -->

## Screenshots

<!-- Anything visible: dark AND light, from the real app. Delete if not applicable. -->

## Checklist

### Science ([docs/SCIENCE.md](https://github.com/syedhamidali/xradar-desktop/blob/main/docs/SCIENCE.md))
- [ ] Not scientific / not visible to users (skip this block), **or**
- [ ] The output has a **provenance class** (OBSERVED, MODEL/ANALYSIS, RETRIEVED, SIMULATED/STYLISED) and shows source, run/valid time, resolution, method, parameters and uncertainty as that class requires
- [ ] The method is **cited** (docstring and docs); departures from the reference are written down
- [ ] **Limits** are documented and visible next to the result
- [ ] **Nothing is invented**: gaps stay gaps; any interpolation, advection, smoothing or model is declared in the UI, the export and the docs
- [ ] A stylised visual is opt-in, **labelled on the image**, and does not change the data
- [ ] Units, UTC time, height reference and projection are stated
- [ ] The result is **reproducible**: inputs, versions, parameters and time are written to the output
- [ ] **No download without consent**; new network hosts are documented (and in the content-security policy)

### Testing
- [ ] **Tests against known answers** (analytic, published, invariants, real files) are included; a regression test for every bug fixed
- [ ] `ruff check`, `ruff format --check` and `pytest` pass (python/)
- [ ] `svelte-check`, `tsc --noEmit`, `npm run test:unit` and `npm run build` pass
- [ ] `cargo check` / `clippy` pass (if src-tauri changed)
- [ ] Docs build with no new warnings; `python scripts/check_links.py` passes; new pages are in a `toctree`
- [ ] I ran the app and **looked at it** (dark and light) with a real file

### Performance and stability
- [ ] Measured, not assumed (numbers below, see docs/PERFORMANCE.md and docs/PERFORMANCE_FRONTEND.md), or not on a hot path
- [ ] No new work on the main thread for large data; long jobs are cancellable and report progress
- [ ] Errors are shown to the user in words; nothing fails silently

### Licences and housekeeping
- [ ] New dependencies, fonts, colour maps, data or imagery are in `THIRD_PARTY_NOTICES.md` (`python scripts/licenses/generate_notices.py`) and pass `--check`; anything non-commercial or share-alike is flagged
- [ ] Commits are signed off (`git commit -s`, see [DCO](https://github.com/syedhamidali/xradar-desktop/blob/main/DCO)) and carry **no AI attribution trailers**
- [ ] Changelog fragment `docs/changes/<PR number>.md` added
- [ ] No secrets, no radar data, no absolute local paths committed

## Performance numbers (if relevant)

<!-- before / after, machine, file, how measured -->

## Notes for the reviewer

<!-- Anything you are unsure about, trade-offs, follow-ups. -->
