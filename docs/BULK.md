# Bulk jobs from the command line

Process a whole day of scans, a folder of cases or a nightly feed with the same commands you use
for one file. There is no special "batch mode": `xrd` returns clear exit codes, writes files you
name, and prints machine-readable results with `--json`, so ordinary shell tools do the looping.
Read [the command line page](CLI.md) first for the options used here.

## Which commands take many files

| Command | Several `-i` files | What happens |
|---|---|---|
| `xrd info` | yes | one report per file |
| `xrd cells` | yes | the files are one radar over time and the cells are **tracked** |
| `xrd mosaic`, `xrd winds` | yes | the files are different radars at one time and are **combined** |
| `xrd animate` | yes | the files are one or more radars over time: grouped by radar and time, the nearest volume of each radar per step, one 3D animation (see [ANIMATE.md](ANIMATE.md)) |
| `xrd run`, `xrd pipeline`, `xrd plot`, `xrd export` | one file per call | loop over the files |

So a bulk job over many independent files is a loop around one of the last four commands. Every
example below works the same in a shell script, a cron job or a CI step.

## 1. The basic loop

```bash
mkdir -p out
for f in data/*_V06; do
  xrd run storm_detection -i "$f" -P threshold_dbz=45 -q -o "out/$(basename "$f").csv" \
    || echo "FAILED: $f" >> failed.txt
done
```

`-q` keeps the output to errors only, and `||` records a failure without stopping the job. Always
quote `"$f"`: radar file names often contain characters a shell would otherwise split on.

## 2. Use all the cores

`xargs -P` (or GNU `parallel`) runs several files at once. Each `xrd` is a separate process with its
own memory, so choose `-P` by how much memory one volume needs, not only by the number of cores.

```bash
ls data/*_V06 | xargs -P 4 -I{} sh -c \
  'xrd pipeline "Reflectivity QC" -i "$1" -q -o "out/$(basename "$1").nc"' _ {}
```

A NEXRAD volume is about 330 MB when decoded (see `xrd info`), so `-P 4` needs roughly 1.5 GB.

## 3. Stop one bad file from stopping the job

The exit code says what went wrong, so a script can react to it.

| Exit code | Meaning | Usual reaction |
|---|---|---|
| 0 | success | continue |
| 1 | the algorithm could not run on this data | log the file, continue |
| 2 | the command was used wrongly | fix the script, stop |
| 3 | the input file is missing or unreadable | log the file, continue |
| 4 | a Python package is missing | install it, stop |

```bash
for f in data/*_V06; do
  xrd export -i "$f" -o "out/$(basename "$f").zarr" -q
  case $? in
    0) ;;
    1|3) echo "$f" >> skipped.txt ;;       # a bad or empty file: note it and carry on
    *) echo "stopping: check the command" >&2; exit 1 ;;
  esac
done
```

## 4. Make a job safe to re-run

Skip what is already done, so an interrupted job picks up where it stopped:

```bash
for f in data/*_V06; do
  out="out/$(basename "$f").nc"
  [ -s "$out" ] && continue
  xrd pipeline "Reflectivity QC" -i "$f" -q -o "$out.part" && mv "$out.part" "$out"
done
```

Writing to `.part` and renaming at the end means a crash never leaves a half-written file that looks
finished.

## 5. Collect results in a table

`--json` prints exactly one JSON document on stdout, so it pipes straight into `jq`:

```bash
for f in data/*_V06; do
  xrd run storm_detection -i "$f" -P threshold_dbz=45 --json 2>/dev/null \
    | jq -r --arg f "$(basename "$f")" '[$f, .scalars.Cells] | @tsv'
done > cells_per_scan.tsv
```

An inventory of a whole archive is one command, because `info` takes many files:

```bash
xrd info -i data --json | jq -r '.[] | [.radar, .time, .n_sweeps] | @tsv' > inventory.tsv
```

## 6. One recipe for every file

Put the settings in a file so every run, and every colleague, uses the same ones. Options on the
command line win over the file, so the file is a template and `-i` and `-o` change per file.

```toml
# qc_cells.toml
command = "run"
module = "storm_detection"

[param]
threshold_dbz = 45
```

```bash
for f in data/*_V06; do
  xrd --config qc_cells.toml -i "$f" -q -o "out/$(basename "$f").csv"
done
```

`--print-command` shows the full command that a config file turns into, which is a good line to keep
in a log next to the results.

## 7. Time series and storm tracks

`cells` is the exception to the one-file loop: give it all the scans of one radar and it tracks the
cells through time (files are sorted by time, so the shell glob order does not matter).

```bash
xrd cells -i 'data/KHPX*' --threshold-dbz 45 -o cells_all.csv --tracks tracks.csv -p tracks.png
```

To get one set of tracks per day, loop over the day folders:

```bash
for d in data/2021-12-1*/; do
  xrd cells -i "$d" --threshold-dbz 45 -q -o "out/$(basename "$d")_cells.csv" \
    --tracks "out/$(basename "$d")_tracks.csv"
done
```

## 8. A mosaic for every time step

Each time step has a file from every radar. Group them by a time stamp in the name and loop over the
groups; the same pattern works for `xrd winds`.

```bash
for t in 20211211_0159 20211211_0209 20211211_0219; do
  xrd mosaic -i data/KHPX${t}* data/KNQA${t}* data/KPAH${t}* \
    --product composite -q -o "out/mosaic_${t}.nc" -p "out/mosaic_${t}.png"
done
```

The files of one mosaic do not have to start in the same minute; the table printed by `xrd mosaic`
shows each radar's volume time, so a check of the spread is easy to add with `--json`.

## 9. The same thing from Python

When the loop needs more than a shell gives you, use the Python API. It runs the same engine and has
the same names as the command line (see [the Python API](API.md)).

```python
from pathlib import Path
from engine import api

rows = []
for path in sorted(Path("data").glob("*_V06")):
    try:
        res = api.open(path).run("storm_detection", threshold_dbz=45)
    except Exception as exc:                      # one bad file must not stop the job
        print("skipped", path.name, "-", exc)
        continue
    rows.append((path.name, res.scalars["Cells"]))
```

## 10. Run it every night

A cron job, a systemd timer or a CI schedule just runs your script. Three habits make it reliable:

- use absolute paths, because cron starts in another folder;
- write one log per run (`>> logs/$(date +%F).log 2>&1`) and keep `-q` so only problems appear in it;
- finish with a check that fails loudly when nothing was produced, for example
  `[ "$(ls out | wc -l)" -gt 0 ] || exit 1`.

```text
# 02:30 every day: quality-control yesterday's files
30 2 * * *  /home/me/jobs/nightly_qc.sh >> /home/me/jobs/logs/nightly.log 2>&1
```

## Good to know

- **Output names are chosen for you** when you leave out `-o`: the file lands in the current folder.
  In bulk jobs always give `-o` (or run from a clean folder) so results from different inputs cannot
  overwrite each other.
- **Memory is per process.** A parallel job uses `-P` times the memory of one run.
- **Nothing is downloaded** by the command line. Fetch your files first (for example with `aws s3 sync`
  from the NEXRAD archive), then process them.
- **Disk space:** a derived variable lives inside the file you write, so a pipeline output is about the
  size of the original. Zarr or compressed NetCDF are the smaller choices for large archives.
