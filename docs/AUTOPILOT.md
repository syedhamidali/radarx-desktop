# Autopilot (Analyst)

Give Autopilot any set of inputs - one file, a folder, hundreds of volumes, several radars,
several formats, a time series, RHI scans, dual-pol or not - and it decides what can be done with
exactly that data and how, then runs only what you approve.

* **App:** drop three or more files (or a folder) on the window, or *Command palette > Autopilot*.
* **Command line:** `rxd auto INPUT... [--plan-only] [--yes] [--steps IDS] [--budget-gb N] [-o DIR]`.

To do the same in the app on a real case, four radars and 44 volumes, follow the tutorial
{doc}`tutorial-bulk-data`.

## What it does, in order

1. **Inspects** cheaply: file names and sizes first; then a sample of files per radar is opened for
   its metadata (format, site, scan kinds, fields, band, Nyquist, range); one representative volume
   per radar gets the {doc}`data check <DATA_CHECK>`. No sweep data is loaded beyond that, nothing is
   downloaded, nothing is written. Every sampling decision is listed.
2. **Classifies** the situation: one volume, a time series of one radar, several radars at one time,
   several radars over time, RHI scans, or a mix; with the evidence (counts, times, cadence, gaps,
   distances and coverage overlap between radars, beam-crossing geometry for dual Doppler).
3. **Plans** a ranked list of analyses, each with *why*, *what it needs*, an *estimate of time and
   memory for this data* (marked ESTIMATE), *what it produces*, its *provenance class*
   ({doc}`science-and-trust`) and the equivalent `rxd` command. What the data cannot support is
   refused with what is missing (no mosaic without overlap in space and time, no winds without two
   radars with velocity and a wide beam-crossing area, no accumulation without a series, no
   hydrometeor classification without dual-pol fields).
4. **Governs** resources: a memory budget (60 % of RAM, or `--budget-gb`), one volume at a time
   (one per radar for mosaics and winds), and long series subsampled for the heavy per-step products
   with the reason stated. A 2 GB input and a 200 GB input both get a sensible plan.
5. **Runs** the approved steps with progress and cancel, then writes a run record (inputs, software
   and library versions, every decision and its reason, every reduction, the commands) and a
   one-page summary next to the results. A re-run skips the steps that finished.

## The rules are visible

The decisions are a short table of rules, each with the sentences of evidence it fired on (or did
not). The panel shows it under *Show the decision rules and their evidence*; `rxd auto --plan-only`
prints it. There is no hidden scoring.

## Limits

* Estimates are rough coefficients scaled by the size of your files, not measurements of your run;
  the run record holds the measured times.
* The animation step is planned and its command given, but not rendered inside a run (it needs the
  system browser and a video encoder; see {doc}`ANIMATE`).
* Files whose names carry no radar and time are opened to find out; with very many such files only
  a sample is opened and the plan says so.
