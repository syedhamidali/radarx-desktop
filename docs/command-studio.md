# Everything from the command line, in the app

Whatever the `rxd` command line can do ({doc}`CLI`), the app can do interactively, in one of two ways: a
**dedicated panel** made for the task (with a map, a picture, sliders), or the **Command Studio**, a form
generated from the command itself, for the commands and options that have no panel of their own. The two
run the same engine and give the same results.

## The Command Studio

Open the command palette (Ctrl/Cmd+K) and choose **Command Studio**. On the left, every `rxd` command with a
one-line purpose and a search box (type `rain`, `winds`, `picture`...). On the right, a form for the chosen
command:

* **Files** are picked with the usual file and folder dialogs; a file that is already open in the app is one
  click away. A pattern such as `data/*_V06` or a whole folder works like in the terminal.
* **Numbers** show their unit (m, km, dBZ, dpi), **choices** are dropdowns, **switches** are toggles, and a
  `KEY=VALUE` parameter list is one line per parameter.
* Rarely used options (output control, figure resolution, reproducibility) are folded under **Advanced**.
* The **working folder** is where a result without a full path is written; by default it is the folder of
  the first input file.
* The **equivalent terminal command** is shown live and can be copied, so a run made in the app can be
  repeated in a script, a cron job or on another machine, exactly.
* **Run** shows the engine's progress and can be **cancelled**; the command stops at its next step.
* The **result** is the command's verdict in plain words (never a traceback), its text output, the files it
  wrote with *Show in folder* and *Copy path*, thumbnails of the pictures (click to view), and
  **Open in the app** for a NetCDF / CfRadial result, which opens it as a new file in the viewers.

The account is the one the app is signed in with; the terminal and the app share it.

## Where each command lives

| Command | Purpose | In the app |
|---|---|---|
| `rxd info` | what is in a file | the Data Inspector (opens with the file); Command Studio for several files or JSON |
| `rxd check` | data-quality report, suggested QC recipe | the Data check chip after opening a file; Command Studio for the fix plan as a file |
| `rxd plot` | a PPI picture of one variable | the Export panel (quick export); Command Studio for themes, scale limits, titles |
| `rxd pipeline` | QC recipes: list, show, run, script | the Pipeline panel; Command Studio for the Python script and recipe files |
| `rxd run` | an algorithm, one sweep or all sweeps | the Modules panel; Command Studio for tables, folders of results and time-outs |
| `rxd modules` | list and describe the algorithms | the Modules panel; Command Studio for the text listing |
| `rxd cells` | storm cells, tracked through several files | the Storm panel; Command Studio for tables and tracking across files |
| `rxd sounding` | a radiosonde file: CAPE, CIN, shear, helicity | the Sounding panel; Command Studio for CSV / JSON output |
| `rxd mosaic` | several radars on one map | the Mosaic panel; Command Studio for GIS images with world files |
| `rxd winds` | 3-D winds from two or more radars | the Wind panel; Command Studio for the variational method and grid options |
| `rxd animate` | a storm-centred 3D animation | Command Studio (the only interactive way; the Storm clip view makes single clips) |
| `rxd qpe` | rain rate and accumulation with uncertainty | the Rainfall (QPE) panel; Command Studio for gauges, basins and files |
| `rxd basin` | area-weighted rainfall over basins | Command Studio |
| `rxd export` | NetCDF, CfRadial2, Zarr, CSV or an image | the Export panel; Command Studio for `--pipeline` / `--run` before writing |
| `rxd examples` | a guided tour of commands | Command Studio (prints the tour) |
| `rxd completion` | shell completion script | Command Studio (prints the script; copy it to your shell) |
| `rxd login`, `rxd logout`, `rxd whoami` | the account | the account dialog (sign in, sign out, who is signed in); `whoami` also runs in Command Studio |

A new command or option added to `rxd` appears in the Command Studio by itself: the form is read from the
command line's own description, not written by hand.

## Limits

* The Command Studio is part of the desktop app; the web version has no access to your computer's files.
* A file list on standard input (`-i -`) has no equivalent in a form: pick the files instead.
* Cancel asks the command to stop at its next progress step; a step that reports no progress (a long file
  read) finishes first.
* One Command Studio command runs at a time; the app's own panels keep working meanwhile.
