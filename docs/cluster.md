# Servers and clusters (no screen)

Everything Radarx does to your data can be done from a terminal on a computer that has **no screen**: a server, a
cluster login node, a compute node in a batch job. This page is the short path from nothing to a working
job: install, put the command on your path, sign in, run, and bring the results back. The commands themselves, task by
task, are on {doc}`cli-guide`; every option is on {doc}`CLI`.

```{note}
**What was tested.** The commands that run `rxd` were run for real on a Mac. Installing, the `PATH` steps, sign-in on a
machine without a browser, SLURM and Windows were **not tested on a cluster or on Windows**, and each such place below
says "not tested". Where a statement comes from how `rxd` is built rather than from a test, the page says so.
```

## What you can and cannot do

| | Without a screen |
|---|---|
| Look at a file, check quality, clean, run algorithms, cells and tracks, mosaics, winds, rainfall, soundings | **Yes**, with `rxd` |
| Export NetCDF, CfRadial2, Zarr, CSV and picture files | **Yes** |
| "I have a pile of data, decide what to do" | **Yes**, `rxd auto` |
| Movies and storm clips | Only the **plan** (`--no-render`); drawing the film needs Google Chrome: see [below](#what-needs-a-display-or-chrome) |
| The app's windows (2D, 3D, panels) on data that stays on the cluster | **Not supported yet**: see [the last section](#using-the-apps-screen-with-data-on-a-cluster) |

## Install without a window

You do not need the app. `rxd` comes as a Python package (a "wheel") that is published with every release. It needs Python
3.10 or newer (3.12 or newer for the optional radarx library, which mosaics, winds, rainfall and many algorithms need). Check what you have:

```bash
python3 --version
```

On many clusters Python comes as a module; the name is specific to your site (for example `module load python`), so ask your
administrators or run `module avail python`. This was not tested.

Pick **one** of the three ways. They install the same package from the download host named on {doc}`installation`.

**A virtual environment** (the one to use if you want to add radarx or other packages later):

```bash
python3 -m venv ~/radarx-env && source ~/radarx-env/bin/activate
V=$(curl -fsS https://downloads.labshope.com/radarx/releases.json | python3 -c "import json,sys; print(json.load(sys.stdin)['latest'])")
pip install "https://downloads.labshope.com/radarx/v$V/radarx_desktop_engine-$V-py3-none-any.whl"
pip install -U radarx             # optional: mosaics, winds, rainfall (Python 3.12 or newer)
rxd --version
```

(The page {doc}`installation` reads the version with `jq`; the line above uses Python instead, because `jq` is often missing on servers.)

**pipx** keeps `rxd` in its own environment and puts the command on your path for you:

```bash
pipx install "https://downloads.labshope.com/radarx/v$V/radarx_desktop_engine-$V-py3-none-any.whl"
pipx inject radarx-desktop-engine radarx           # optional: Python 3.12 or newer
```

**`pip install --user`** installs into your home folder without a virtual environment:

```bash
pip install --user "https://downloads.labshope.com/radarx/v$V/radarx_desktop_engine-$V-py3-none-any.whl"
```

What was checked for this page: the file for version 0.3.2 on the download host is a small wheel (under 1 MB) that
declares "Python 3.10 or newer" and provides the `rxd` command; it pulls its dependencies (numpy, scipy, xarray, matplotlib and
others) from the Python package index, so the machine that installs needs network access. The three ways above were not
re-run in a fresh environment, and not on Windows.

**Compute nodes without internet.** Install on a login node, into a folder on the shared file system (the virtual environment
above), and run `~/radarx-env/bin/rxd` from the job. Not tested.

**Updating.** Run the same `pip install` with the new version number.

## Put `rxd` on your PATH

`rxd` is a small program file. Your shell finds it only if its folder is on the `PATH`. If `rxd --version` says "command not found",
the program is installed but its folder is not on the path.

**Always works:** call it by its full path, or activate the environment first.

```bash
~/radarx-env/bin/rxd --version         # a virtual environment: no activation needed
source ~/radarx-env/bin/activate       # or activate it; then plain  rxd  works in this shell
```

**macOS and Linux.** `pipx` and `pip install --user` put the program in `~/.local/bin` (on Linux; on macOS `pip install --user`
uses a folder under `~/Library/Python/`, which `python3 -m site --user-base` shows with `/bin` added). Add the folder to your shell's
start-up file:

```bash
pipx ensurepath                                        # pipx: does this for you; open a new terminal afterwards
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc     # bash (use ~/.zshrc for zsh)
```

Open a new terminal (or `source ~/.bashrc`) and run `rxd --version`. On the Mac used for this page, `rxd` is found in `~/.local/bin`, which is on the path. In a batch job
your start-up file may not be read: put the `export PATH=...` line (or the `source .../activate` line) in the job script, as the
examples below do.

**Windows (PowerShell).** pip puts the program in a `Scripts` folder (`$HOME\radarx-env\Scripts` for the virtual environment on the
installation page; `pip install --user` uses a `Scripts` folder under `%APPDATA%\Python\`). To add a folder to your own `PATH` for good:

```powershell
$p = [Environment]::GetEnvironmentVariable("Path", "User")
[Environment]::SetEnvironmentVariable("Path", "$p;$HOME\radarx-env\Scripts", "User")
```

Open a new PowerShell window afterwards. The same is possible by hand in *Settings → System → About → Advanced system settings →
Environment Variables → Path → Edit → New*. Not tested on Windows.

## Sign in on a machine with no browser

`rxd` needs the same free account as the app ({doc}`ACCOUNTS`). **The account is created once, in the app** (its sign-up screen, where an
e-mail code is entered); the website's account page manages an account that exists. After that, signing in needs no browser: only an
e-mail address and a password, typed in a terminal (an SSH session is fine).

```bash
rxd login            # asks for the e-mail and the password (the password is not shown, never stored, never put on the command line)
rxd whoami           # who is signed in, and until when
```

Without a session, every command except `login`, `logout`, `whoami`, `examples`, `completion`, `--help` and `--version` stops with exit code
**5** and this message (this was run here with an empty session folder):

```text
error: You are not signed in.

radarx-desktop needs a free account, in the app, in the terminal and in Python.
  Sign in:        rxd login
  New here?       open the radarx-desktop app and choose "Create your free account"
  Check status:   rxd whoami
```

Things to know:

* **The password cannot be given to a script.** Sign in by hand once; jobs then use the stored session. An account created with the
  Google, Apple or Facebook button may have no password; whether `rxd login` works for such an account was not tested.
* **`rxd login` needs a network path to the account server** (see [Behind a proxy or firewall](#behind-a-proxy-or-firewall)).
* `rxd logout` ends the session on the server and removes the stored file.

### The session file, and shared home folders

The session is one small file per user, readable only by you. It holds sign-in tokens and your e-mail address, never the password.

| System | Where |
|---|---|
| Linux | `$XDG_CONFIG_HOME/radarx-desktop/session.json`, usually `~/.config/radarx-desktop/session.json` |
| macOS | `~/Library/Application Support/radarx-desktop/session.json` |
| Windows | `%APPDATA%\radarx-desktop\session.json` |

* **A home folder shared by all nodes** (the usual cluster set-up) means one sign-in serves them all: log in once on the login node and
  every node that sees the same home folder is signed in. If your nodes have separate home folders, each one needs its own `rxd login`.
* **To keep the file somewhere else**, set `RADARX_CONFIG_DIR` to a folder before running `rxd`. This was checked here: with
  `RADARX_CONFIG_DIR` pointing to an empty folder, `rxd whoami` answered "You are not signed in" and exit code 5, so the setting is read.
  The same folder must be used for `rxd login` and for the jobs.
* **Do not copy the session file from one computer to another.** That is not supported. Sign in on each computer (or share one home folder).
* Anyone who can run programs as you on that machine can read the file (it is protected by file permissions only), so treat it
  like an SSH key and do not put it in a shared project folder.

### Many jobs at once

The sign-in token lasts one hour and is renewed by `rxd` itself when a command runs while the account server can be reached. The
file is **locked while it is changed**, so many jobs starting at the same moment do not corrupt it; when one job has already renewed the
sign-in, the others use its result. This follows from how `rxd` is built; it was not tested with hundreds of simultaneous jobs, nor on a
network file system (where file locking can behave differently). If you submit a very large array, run `rxd whoami` once first, in the
submit script, so the sign-in is fresh when the jobs start.

### Compute nodes without internet: the 30-day rule

Most clusters give the login node internet access and the compute nodes none. A stored session is still accepted **while the account server
cannot be reached**, for **30 days after the last time the server vouched for the account**. To use this:

1. On a machine that can reach the internet and that sees the same session file, run `rxd login`.
2. Run your jobs on the nodes without internet. They check the session offline.
3. At least once every 30 days, let an `rxd` command that does real work run on a machine with internet, after the one-hour sign-in has
   expired (it is then renewed online), or just run `rxd login` again. The 30 days then start again. Whether a particular command such as
   `rxd whoami` renews the sign-in was not tested; `rxd login` always does.

What is and is not guaranteed ({doc}`ACCOUNTS` has the exact rules):

* **Guaranteed by the design:** an offline node accepts a stored session for 30 days after the server last vouched for the account. A
  sign-in that has not expired yet needs no network at all.
* **Counted from the last time the server vouched for it**, not from the day of `rxd login`. A session that is renewed every week keeps working.
* **Not guaranteed:** the node's clock. A clock set far into the future refuses the session until the account server has vouched for it again;
  a clock that is stuck in the past cannot be told apart from the real one. Check that your nodes keep correct time.
* **Not guaranteed:** an account that was deleted or revoked keeps working offline until the 30 days are over.
* **After 30 days offline** the commands refuse to run until you sign in again. The expiry itself was not tested (it would take 30 days).
* **Not a download of data.** Offline nodes can run everything on this page that does not need the internet; `rxd` itself never downloads
  radar data.
* `rxd` keeps a small, feature-level activity record (command names, never file names; {doc}`ACCOUNTS`). Offline it is queued on the node
  (at most 500 events) and sent later; it never blocks or fails a command.

## Behind a proxy or firewall

`rxd` talks to the account server over HTTPS. This was checked by sending an account request through a test proxy:

* **Proxy variables.** The account requests follow the usual proxy environment variables `HTTPS_PROXY`, `HTTP_PROXY` and `NO_PROXY`:

  ```bash
  export HTTPS_PROXY=http://proxy.example.org:3128
  export NO_PROXY=localhost,127.0.0.1
  rxd login
  ```

  With `HTTPS_PROXY` set, the client asked the proxy to open a tunnel (`CONNECT`) to the account host on port 443.
* **What to allow in a firewall.** For sign-in: the host `bfyimkgvnqiqedcwprpi.supabase.co`, port 443, https only. To install the package: `downloads.labshope.com`
  and the Python package index your tools use (usually `pypi.org` and `files.pythonhosted.org`).
  `rxd` downloads no radar data. (`rxd animate` may fetch map tiles for some looks; `--offline` uses cached tiles only.) The hosts that the **app** downloads data from are named in the
  app's consent dialogs and on {doc}`opening-data`.
* **A proxy that refuses (for example answers 407) or does not answer** counts as "the account server cannot be reached". It is **not** a
  sign-out: the stored session is kept and the 30-day rule above applies.
* **A proxy that opens the encrypted connection itself** ("TLS interception") works when its root certificate is in the system certificate
  store: `rxd` trusts the system store as well as its own bundled list. The standard OpenSSL variables `SSL_CERT_FILE` and `SSL_CERT_DIR` are the
  usual way to point to a certificate file or folder; whether `rxd` honours them was **not tested**.
* **If the proxy blocks the account host for good, the first `rxd login` cannot happen on that machine**, and so `rxd` cannot be used there
  until the host is reachable. Ask your administrators to allow it. Moving a session file from another computer is **not supported**, so do not do it.

## Batch jobs

The rules are the ones of any command-line tool: **one `rxd` command per task**, an input and an output you name, a memory number you give, and
the exit code to decide what happened.

* **Give the memory.** `--budget-gb N` on every command (or `RADARX_BUDGET_GB=N` once in the script). `rxd` measures memory and cores of the
  **whole computer** it runs on; as far as can be told from how it measures, it does not read the limit that a batch system gives your job. On a
  node with 256 GB where your job got 8 GB, an unlimited run could plan for far more than 8 GB. Choose a budget somewhat below what the job requests
  (about two thirds is a safe start), because the estimates are approximate (about 25 %).
* **See what it will need first.** `rxd budget -i FILES --budget-gb 5` prints memory, time and the plan for each operation on your files, and
  `rxd budget` also prints the number of cores it sees (as far as can be told, those of the whole node). A big input decoded is much larger than on disk: one NEXRAD volume of 7.7 MB took 460 MB decoded (`rxd info`).
* **Put big output on fast scratch space, not in your quota.** Results go where you say with `-o`. `rxd animate` caches gridded volumes in
  `~/.cache/radarx-desktop/animate` unless you give `--cache-dir` (or set `XRADAR_ANIMATE_CACHE`): point it to your scratch folder.
* **Exit codes** (full table on {doc}`cli-guide`): 0 success; 1 processing failed; 2 wrong use; 3 file missing or unreadable; 4 an optional package is missing; 5 not signed in;
  70 an unexpected error; 130 interrupted. A scheduler records the exit code of the last command in the job script, so a job that ends with `rxd` shows
  failed when `rxd` failed. An empty input file gave exit code 1 (not 3) in a test; treat 1 and 3 alike for a bad file.
* **Logs.** `rxd` writes results and tables to standard output and messages and progress to standard error. Redirect each task to its own log
  (`2> logs/NAME.log`), use `-q` so that only errors reach it (some commands, `rxd auto` for one, write progress with carriage returns that make a log hard to read), `-v` for
  more detail, `-vv` for the full error trace.
* **Parallel tasks use parallel memory.** Each `rxd` is a separate process with its own memory. Run as many at once as fit in the job's memory,
  and give each its own `--budget-gb`.
* **Nothing is downloaded** by `rxd`: put the files on the cluster first (with the tools of your data provider). {doc}`BULK` covers loops, `xargs`,
  re-running safely and nightly jobs in detail.

### A plain shell loop

This loop was run for real (one input per command, a log per file, a result per file, and a bad file that does not stop the rest):

```bash
#!/bin/bash
mkdir -p results logs
for f in data/KHPX/KHPX20211211_0232*_V06 data/KHPX/KHPX20211211_0238*_V06 badfile_V06; do
  name=$(basename "$f")
  if rxd run storm_detection -i "$f" -P threshold_dbz=45 --budget-gb 4 -q -o "results/$name.csv" 2> "logs/$name.log"; then
    echo "ok    $name"
  else
    echo "FAIL  $name (exit code $?)"
  fi
done
```

```text
ok    KHPX20211211_023231_V06
ok    KHPX20211211_023814_V06
FAIL  badfile_V06 (exit code 1)
```

`badfile_V06` was an empty file; its log holds `error: Could not open badfile_V06: the file is empty (0 bytes). A download or copy probably failed; fetch it again.`

### A SLURM job array (not tested on SLURM)

One list of input files, one array task per file. The `#SBATCH` lines are standard SLURM; the `rxd` line is the tested part. Make the `logs` and `results` folders
first, because SLURM does not create the folder for its own output file.

```bash
ls /scratch/me/data/*_V06 > filelist.txt          # one input file per line
mkdir -p logs results
wc -l filelist.txt                                # = the number for --array below
```

```bash
#!/bin/bash
#SBATCH --job-name=radarx-qc
#SBATCH --array=1-100
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --time=00:30:00
#SBATCH --output=logs/%x_%A_%a.out

export PATH="$HOME/.local/bin:$PATH"        # or: source ~/radarx-env/bin/activate

FILE=$(sed -n "${SLURM_ARRAY_TASK_ID}p" filelist.txt)
NAME=$(basename "$FILE")

rxd pipeline "Reflectivity QC" -i "$FILE" -s 0 --budget-gb 5 -q -o "results/$NAME.json"
```

Submit with `sbatch qc_array.sh`. After the jobs end, `sacct -j JOBID --format=JobID,State,MaxRSS,Elapsed` shows how much memory each task really used
(your site may offer `seff JOBID` too); use it to tune `--mem` and `--budget-gb`. A job that ran out of its memory is stopped by the scheduler, not by `rxd`.

Jobs that need **many files at once** (a mosaic, winds, a track, an animation plan) take several files in one command:

```bash
rxd mosaic -i data/KHPX/KHPX20211211_023231_V06 data/KNQA/KNQA20211211_023037_V06 -o mosaic.nc --budget-gb 5 -q
```

## What needs a display or Chrome

Everything except drawing a movie runs with no screen. Pictures (`rxd plot`, the `-p` figure of any command, mosaics, winds, rain, cells) are
drawn straight into files and no window is opened; this holds in the tests on the Mac and follows from the way the pictures are made, but it was **not
tested on a Linux machine without a display**.

| Task | Headless? |
|---|---|
| `info`, `check`, `plot`, `pipeline`, `run`, `modules`, `cells`, `sounding`, `mosaic`, `winds`, `qpe`, `basin`, `export`, `auto`, `budget` | Yes (as run here) |
| `rxd animate --no-render` (the plan) | Yes (as run here); it needs the radarx library |
| `rxd animate` drawing a film or a still (`-o x.mp4`, `-p still.png`) | **Needs Google Chrome** (the system Chrome draws the 3D scene and encodes the film), the `video` extra of the package (Playwright) and the 3D renderer files of the Radarx app. **Not run here.** |

For movies, from {doc}`ANIMATE`: Chrome is not downloaded for you; the renderer files come with an installed Radarx app (0.3.1 or later) or from a
folder you name with `--app-dist`; `--software` uses Chrome's software drawing when there is no graphics card (works anywhere, very slow); `--encoder png`
writes numbered frames and prints the `ffmpeg` line instead of a film. Whether Chrome and the renderer files can be set up on a particular
cluster was not tested. **The practical advice:** on a cluster, use `rxd animate --no-render` to prepare and check, copy the volumes you need to a
computer with the app and Chrome, and make the film there.

## Moving results back

Results are ordinary files; copy them with the tools you already use (`rsync`, `scp`, a data transfer node). For example, from your own computer:

```bash
rsync -av user@cluster.example.org:/scratch/me/results/ ./results/
```

| What you wrote | Format | Opens in |
|---|---|---|
| a radar volume (`export`, `pipeline -o x.nc`) | NetCDF (CfRadial1), CfRadial2, or Zarr (a **folder**: copy it whole, or pack it with `tar` first) | the app (drop it on the window), Python, other radar tools |
| a table (cells, tracks, basins, one sweep) | CSV, JSON | any spreadsheet or script |
| a picture | PNG, SVG, PDF | anywhere |
| a mosaic image | PNG **with** its `.pgw` and `.prj` files (copy all three) | GIS software, placed on the map |
| a mosaic, winds or rainfall grid | NetCDF | GIS software and Python; **not** the app's radar viewer ({doc}`AUTOPILOT`) |
| an Autopilot folder | results, `run_record.json`, `SUMMARY.md` | read the summary first |
| a film | MP4 (or WebM) | any player |

Zarr and CfRadial2 are written sweep by sweep and are smaller than CfRadial1 NetCDF; a 7.7 MB NEXRAD volume was 15 MB as NetCDF, 15.6 MB as CfRadial2
and 24 MB as Zarr here. All formats carry the software version, input file names and the history of what was done ({doc}`OUTPUT_STANDARDS`).

## Using the app's screen with data on a cluster

**This does not work yet.** Said plainly:

* The desktop app today connects **only to the engine that it starts itself**, on your own computer.
* Pointing the app at an engine running on a cluster, for example through an SSH tunnel (port forwarding), is **not supported**. There is no
  setting for it and no command that starts a remote engine. It is planned; it is not available in any release.
* So there is no way to look at cluster data in the app's windows while the data stays on the cluster.

What to do until then:

1. **Work on the cluster with `rxd`**, as on this page and on {doc}`cli-guide`: check, clean, analyse, make pictures and tables.
2. **Copy the results** (the table in the previous section shows which formats open where) **or a small part of the data** (a few volumes, a cropped area) to a computer that has the
   app, and open them there.
3. For a whole event, run `rxd auto ... --plan-only` on the cluster first to see which analyses fit, and copy only what you want to look at.

The desktop app and `rxd` are the same engine with the same results, so a command that you try in the app's Command Studio ({doc}`command-studio`) and then copy
to a job runs the same on the cluster.
