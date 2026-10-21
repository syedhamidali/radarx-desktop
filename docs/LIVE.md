# Live map and default radars

The **Live** button in the toolbar shows the newest scans of your *default radars* as one picture on a map and
keeps it up to date: every few minutes the app looks for a newer volume of each radar, downloads only what is
new, merges the volumes into one composite and keeps the last few composites so you can play the recent past.
Nothing is listed or downloaded before you agree, and a line above the map always says what is on screen,
when it was measured and which radar did not answer.

The live map is a feature of the desktop app and works with **NEXRAD (WSR-88D) radars only**, read from the
public `unidata-nexrad-level2` bucket; see {doc}`opening-data` for the other ways to fetch radar data.

## Default radars

Settings → Data → **Live map** lists your default radars in order; **Choose on the map…** opens the chooser.
The first press of **Live** opens the same chooser when no radar has been chosen yet.

* Click radars on the map (the radar-sites layer in picker mode: a chosen radar is a filled dot with its order
  number), or type an id, a name or a state in the search box and click a match.
* Up to 8 radars. The order is the order of the status line; ↑ ↓ reorder, × removes.
* **Use my location** appears only when the browser or system offers geolocation, does nothing until you press
  it, asks the system for permission, picks the nearest 3 radars within 300 km, and forgets the position.
* **Update every**: 2 to 15 minutes, 5 by default (a NEXRAD volume takes 4 to 10 minutes, so a shorter
  interval rarely finds anything new).

The choice is saved with the other settings on this computer.

## What you see

```{image} images/ui/live-map.png
:alt: The live map: a composite of three radars on a map with the LIVE status line above it
:width: 90%
:align: center
```

* **Status line**: `LIVE` (pulsing while updates are running), the time of the last update, the countdown to
  the next, and the controls: the interval, **Pause** / **Resume**, **Update now**, **Stop**, **Radars…**.
* **One chip per radar**: the start time of its newest volume (UTC, from the file name) and its age, or *no
  recent volume*, *offline* or *failed: …* with the engine's reason. A radar whose volume could not be
  downloaded is left out of the composite and keeps its chip, so a missing radar is never silent. These are
  the facts the listing returned; nothing is guessed from the clock.
* **The composite**: the column maximum of reflectivity (`DBZH`) of every radar, gridded at 2 km and merged
  with range-dependent weights, exactly as the Mosaic panel's *composite* product ({doc}`MOSAIC`). Each
  radar's site and 230 km range ring are drawn in its colour; the legend is at the bottom.
* **Provenance**: the composite is **retrieved** (`RETR`: a merged, gridded product), the volumes behind it
  are **observed** Level II data. The layer is listed in the Layers panel with its provenance record and the
  analysis time (the newest volume start among its radars), and the chip at the top left of the map says
  what the picture is. It is not an official product.
* **Recent loop** (bottom): the last 12 composites are kept in memory (about an hour at a 5 minute interval);
  ◀ ▶ step, the play button loops them, the slider jumps, `,` and `.` step from the keyboard. The line says
  which frame is shown, its analysis time and the radars in it; **Newest** returns to the live frame. A frame
  that would repeat the newest one (no radar had a newer volume) is not added, so each frame is a change.
  **Clear** forgets them.
* **Fit** pans and zooms to the composite; scroll zooms about the cursor, drag pans, double-click fits.
  *Hide the map* (Settings → Display, or the command palette) keeps the composite on a plain background.

## Consent

The live map follows the same rule as everything else that reaches the internet ({doc}`SCIENCE`,
{doc}`RADAR_SITES`): a dialog first, with *what*, *from where* and *how big*.

1. **Start the live map**: a listing of today's folder (and yesterday's if today has none) for each radar,
   file names, sizes and times, about 0.1 MB per radar per update. No radar data.
2. **Download N Level II volumes**: the exact file of each radar with its exact size from the listing, and
   the sum. Only these files are downloaded.

The choices are **Allow once**, **Allow for this session** and **Cancel**. *Allow once* makes one update and
then leaves the timer off (**Resume** or **Update now** ask again). *Allow for this session* lets the timer
run while the app is open: every update repeats the listing and downloads only a radar's new volume (about
the size shown, each time a radar has one). Both answers are kept in memory only and forgotten when you quit
or reload; allowing the listings never covers a download, so the sizes are always shown at least once.
*Cancel* stops the live map and nothing is sent. **Offline mode** (Environment data settings) pauses the live
map before any request.

## Limits, bandwidth and memory

* NEXRAD only, from one public bucket; no account, no credentials. A radar of another network cannot be
  chosen.
* **Latency**: a volume appears in the bucket some minutes after the scan started (the age counts from the
  volume start in the file name; the time it reached the bucket is later). The live map is therefore always
  a few minutes behind the radar, and at best as fresh as the bucket.
* **Bandwidth**: one listing per radar per update (about 0.1 MB each) and one Level II file per radar only
  when it is new (typically 3 to 12 MB each). Nothing is prefetched.
* **Memory**: the engine keeps one volume per radar (older ones are dropped, at most 8 radars and 768 MB by
  the listed sizes), and the app keeps at most 12 composite images. **Stop** releases the engine's volumes;
  closing the window does too.
* **Failures**: a radar that has no recent volume or whose download fails is reported on its chip and left
  out; the composite is built from the rest, and only when no radar is usable is the update an error. After
  an error the next try waits the interval, then twice, four times the interval and so on up to an hour, so
  a dead network is not hammered and a radar that comes back is noticed within the hour. A lost engine
  connection fails the running update and the map says so; the frames stay.
* Every update is an operation in the status-bar tray (Cancel stops waiting; the engine's download itself
  cannot be aborted).
* The hover read-out of values, exports and the 3D view are not available for the live composite in this
  version: use the Mosaic panel on files opened through the Cloud data panel for those.
* There is no forecast. The recent loop shows past composites only, each at its own analysis time; the
  frames are kept so that a nowcast layer can be built on them later, and such a layer will be labelled as
  a forecast with its lead time ({doc}`SCIENCE`).
