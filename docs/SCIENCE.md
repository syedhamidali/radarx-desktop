# Science policy

xradar-desktop is a research tool. People read numbers, shapes and colours off the screen and put them in
papers, forecast discussions and reports. So there is one rule above all others:

> **Everything the app shows or writes must follow the science: it is either data with a known source,
> or a clearly labelled method applied to such data, or a clearly labelled picture. Nothing is
> invented, and nothing that is not measured is dressed up as measured.**

This page is the policy that every feature, module, pull request and screenshot has to meet. It is short
on purpose: if a rule here is hard to follow, change the feature, not the rule. The checklist at the end is
copied into the pull request template.

Where a rule is already implemented, the page says where. Where it is a goal that the code does not yet
meet (the provenance in the hover read-out and a generated citation block are tracked in {issue}`63`), the page says that too: a policy that
pretends to be finished is the kind of claim this page forbids.

## 1. Provenance classes

Every layer, field, number and file belongs to exactly one of four classes. The class decides what the
reader must be told.

| Class | What it is | Examples |
| --- | --- | --- |
| **OBSERVED** | A measurement, as the instrument reported it (after only the corrections the file states). | Radar moments in a NEXRAD, ODIM or CfRadial file: reflectivity, Doppler velocity, ZDR, RhoHV. Lightning detections from a network. |
| **MODEL / ANALYSIS** | Output of a numerical model or an analysis that blends observations and a model. | HRRR or ERA5 fields, a sounding-derived freezing level taken from an analysis, a background wind in a multi-Doppler synthesis. |
| **RETRIEVED** | A quantity inferred from observations by an algorithm with assumptions. | Hydrometeor classification, KDP, rainfall rate (QPE), hail size (MESH), VVP winds, multi-Doppler winds, storm cells, de-aliased velocity. |
| **SIMULATED / STYLISED** | Something computed or drawn to look like something, not to measure it. | The "Realistic (stylised)" storm look, sun light and shadows, the sound of the storm, simulated test fields, flow-line animation between two scans. |

What each class must show wherever it appears (on screen, in an export, in a file's metadata, in a
figure caption):

| | Source | Run / valid time | Resolution | Method | Parameters | Uncertainty |
| --- | --- | --- | --- | --- | --- | --- |
| **OBSERVED** | instrument, site, file name | scan start (UTC) | gate spacing, beam width, elevation | none, or the corrections the file states | n/a | stated by the producer, or "not given" |
| **MODEL / ANALYSIS** | model or analysis name and version | **run (initialisation) time and valid time**, UTC | grid spacing, levels | what was interpolated to what | any regridding or time interpolation | "model field: not an observation"; ensemble spread if used |
| **RETRIEVED** | the observed fields it was computed from | scan time of the inputs | of the output grid | named algorithm with a reference | every parameter, with the defaults used | known limits and failure modes; a number where one exists |
| **SIMULATED / STYLISED** | what it is based on, if anything | the time of the data it follows | n/a | what the picture does and does not mean | the style settings | "not a measurement" in plain words |

"Not given" and "not known" are acceptable answers. A blank is not: the reader must be able to tell *missing
information* from *nothing to say*.

### What exists today

- Files the engine writes carry source file *names* (never paths), software and library versions, the UTC
  time, a `history` line per step, and for derived variables the module, parameters and radarx version
  (`python/engine/provenance.py`, {doc}`OUTPUT_STANDARDS`).
- Stylised looks are labelled on screen and in clips, and never change the data underneath
  ({doc}`REALISTIC_3D`, {doc}`STORM_CLIP`).
- Imagery and terrain show their attribution while they are on screen.
- **On screen:** every layer carries a provenance chip (OBS, MODEL, RETR, SIM, STYLE; the app shows
  *simulated* and *stylised* as two chips) and the Layers panel lists each layer with its source, valid
  time and the time offset to the active radar scan; exports carry `data_class`, `data_source`,
  `data_valid_time`, ... ({doc}`ENVIRONMENT`).
- **Not yet done:** provenance in the hover read-out and a generated citation block ({issue}`63`). Features
  that bring new data into the app must describe their provenance in their docs page, give their layers a
  `provenance` object (`python/engine/layer_provenance.py`) and carry it into the export metadata.

## 2. Rules

### 2.1 Cite the method

A method that is not obvious gets a reference in its docstring and in its docs page: author, year, journal
or report, and the equation or table used. "Standard" is not a reference. If the implementation departs from
the paper (a different threshold, a simplification, a fix), say how and why. Algorithms taken from radarx or
xradar name the library and version.

### 2.2 Show the limits

Every retrieval and every derived field has a "limits" paragraph in its docs: the assumptions (wavelength,
beam filling, attenuation, calibration), where it is known to fail (ground clutter, melting layer, range
folding, biological echoes), and what the uncertainty is when it is known. Put the short version in the
module's description so it appears next to the result in the app.

### 2.3 No invented data

- Never fill, extend or smooth data and show the result as if it were measured. Gaps stay gaps; sectors that
  were not scanned stay empty (the cross-section does not stretch a sector over the part of the circle that
  was not scanned).
- No default that changes the numbers silently. A correction (noise, attenuation, calibration offset) is a
  visible, switchable step, recorded in the output.
- Test fixtures and demo fields that are synthetic say so, in the file (`simulated_data` where the format has
  it), in the name, and on screen.
- Colour scales, transfer functions and exaggeration change how data looks, never what it is. The data
  value under the cursor is always the real value.

### 2.4 Interpolation, advection and modelling are declared

Whenever the app creates values that were not measured at that place or time, it says so and says how:

- **Spatial interpolation** (gridding, mosaic, cross-section resampling): scheme, radius of influence,
  weighting, the grid, and how overlap between radars is merged.
- **Temporal interpolation or advection** (morphing or flow animation between two scans, nowcast
  extrapolation): that it is not an observation, the method, the motion field and where it comes from, the
  time between the two real scans. Frames between scans carry a visible "interpolated" mark. Extrapolating
  beyond the last scan is a forecast and is labelled as one, with its lead time.
- **Models** (anything solving or parameterising physics): name, version, domain, resolution, initial and
  boundary data, and whether it was run by the app or downloaded.
- **Projection and geometry**: the beam model used for height and range (effective earth radius), the map
  projection, and the vertical exaggeration, which is always shown on screen.

### 2.5 Test against known answers

A scientific feature ships with tests whose expected values do not come from running the feature:

- an analytic case (a uniform wind seen by two radars must be recovered; a solid-body rotation must give the
  known vorticity; the beam height equation against a published table),
- a published example or a hand-computed value, with its source in the test,
- invariants (a mask never changes unmasked values; export then re-open gives the same array; units
  conversions round-trip),
- real files from `open-radar-data` or the project's test set where the behaviour depends on real
  messiness (missing rays, odd sweep order, sector scans).

"The output looks right" is a review comment, not a test. A bug found with real data gets a regression test
that fails without the fix.

### 2.6 Reproducibility: run records

Anything the app computes can be repeated. An output is accompanied by a record that is enough to rerun it:
the input file names (and their time stamps), the software and library versions, every parameter (including
defaults that were applied), the order of steps, and the time it was made. In files this is the global
attributes, the `history` and the comment of the derived variable; for pipelines it is the recipe JSON and
the script the app can write ({doc}`PIPELINES`, {doc}`OUTPUT_STANDARDS`); for the command line it is the
command line itself ({doc}`CLI`). A result that cannot be explained by its record is a bug.

Randomness is seeded, and the seed is part of the record. Results do not depend on the machine's locale,
time zone (all times are UTC) or window size.

### 2.7 Downloads only with consent

The app never fetches data behind the user's back and never uploads the user's data.

- A feature that needs data from the network asks first, says what and from where, how big it is where
  that is known, and what licence it comes under. Opening a file and viewing a scan download no radar,
  model or environment data.
- Map and terrain tiles are the standing exception: the OpenStreetMap basemap is requested when the map is
  shown (choose "None" in the basemap list to stop that), and Realistic ground fetches imagery and elevation
  only when the user switches it on. The hosts are named in {doc}`REALISTIC_3D` and in
  `THIRD_PARTY_NOTICES.md`, tiles are cached and rate limited and shown with their attribution, and the app
  must keep working offline.
- Update checks are the user's choice and send nothing about their data ({doc}`UPDATES`).
- Tests and CI may download public test data; the app and the engine never do so by themselves. Automated
  agents and contributors do not commit radar data.

### 2.8 Stylised visuals are labelled

A picture that is made to look good (lighting, shadows, fractal detail, haze, a sky, sound) is allowed when:

1. it is **opt-in**, with the scientific rendering as the default and one click away;
2. it is labelled **on the image**, including in exports, clips and screenshots ("Stylised: echo position and
   strength are radar data; light, shadow and detail are not");
3. it **never changes the data underneath** (values, masks, positions): it may change how bright and what
   colour, not where an echo is or how strong it is;
4. it never adds echo where there is none and never moves a core;
5. anything non-physical it uses (a fixed sun, noise) is deterministic and listed in the parameters.

### 2.9 Units, time and place

SI units and the units of the source are stated (dBZ, m s-1, deg, km, mm h-1). All times are UTC and say
so. Heights say whether they are above ground level, above mean sea level or above the radar. Coordinates
say their datum and projection. A number without a unit is a bug.

### 2.10 Credit and licence

Data, imagery, colour maps, fonts and code from others are credited where they are used and listed in
`THIRD_PARTY_NOTICES.md` (generated by `scripts/licenses/generate_notices.py`; see {doc}`LICENSES`). New
sources that are non-commercial or share-alike are flagged in their docs page and in the notices.

## 3. Checklist

Copy this into a pull request that adds or changes anything scientific. It is also in
`.github/pull_request_template.md`.

- [ ] The new output has a **provenance class** (OBSERVED, MODEL/ANALYSIS, RETRIEVED, SIMULATED/STYLISED) and
      shows what that class requires: source, run/valid time, resolution, method, parameters, uncertainty.
- [ ] The method is **cited** (docstring and docs page), and departures from the reference are written down.
- [ ] The **limits** are documented and visible next to the result.
- [ ] **Nothing is invented**: gaps stay gaps; any interpolation, advection, smoothing or model is declared
      in the UI, the export and the docs.
- [ ] A stylised visual is **opt-in, labelled on the image** and does not change the data.
- [ ] **Tests against known answers** (analytic, published, invariants, real files) are included, and a
      regression test for every bug fixed.
- [ ] The result is **reproducible**: a record (inputs, versions, parameters, time) is written to the output.
- [ ] **No download without consent**; new network hosts are listed in the docs and the content-security
      policy, and cached.
- [ ] Units, UTC time, height reference and projection are stated.
- [ ] New dependencies, data or imagery are in `THIRD_PARTY_NOTICES.md` (run the generator) and pass the
      licence check; anything non-commercial or share-alike is flagged.
- [ ] Performance was measured, not assumed (see {doc}`PERFORMANCE` and {doc}`PERFORMANCE_FRONTEND`).
- [ ] Screenshots were looked at, in dark and light.

## 4. Where this is going

The roadmap items that make this policy visible in the app (provenance chips on every layer and the layers
panel are done; a generated methods citation and provenance in the hover read-out remain) are
tracked in {issue}`63` and the environment-data roadmap ({issue}`112`). The master list is {issue}`120`.
