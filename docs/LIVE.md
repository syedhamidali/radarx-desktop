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
:alt: The live map in the dark theme: a composite of three radars, the radar list on the left, the status line, the buttons and the display floor above the map
:width: 90%
:align: center
```

```{image} images/ui/live-map-light.png
:alt: The same live map in the light theme
:width: 90%
:align: center
```

The same picture with *Hide echo below* set to *nothing (all echo)*: the weak blue returns around the radars
(under about 0 dBZ) are what the default floor of 5 dBZ leaves out.

```{image} images/ui/live-map-all-echo.png
:alt: The live map with every echo drawn, including weak returns near the radars
:width: 90%
:align: center
```

* **Status line**: `LIVE` (pulsing while updates are running), the time of the last update, the countdown to
  the next, and the controls: the interval, **Pause** / **Resume**, **Update now**, **Stop**, **Radars…**.
* **One chip per radar**: the start time of its newest volume (UTC, from the file name) and its age, or *no
  recent volume*, *offline* or *failed: …* with the engine's reason. A radar whose volume could not be
  downloaded is left out of the composite and keeps its chip, so a missing radar is never silent. These are
  the facts the listing returned; nothing is guessed from the clock.
* **The left pane** lists the radars: name, the volume each answered with and how old it is, the file name and
  size of the volume the app holds, and buttons to open it (see *Using the data behind the picture*). At the
  bottom it says how many volumes are held and how many are open as files, with the disk they use.
* **The composite**: the column maximum of reflectivity (`DBZH`) of every radar, merged with
  range-dependent weights, exactly as the Mosaic panel's *composite* product ({doc}`MOSAIC`). The grid is
  **1 km for up to four radars**, 1.5 km for five or six and 2 km for seven or eight; it is made coarser
  when the radars are far apart or when your computer's memory or time budget needs it, and the label on the
  map always states the grid that was used. The picture is drawn **smoothed** (several drawn pixels per grid
  cell) so cells do not show as blocks; the values themselves stay on the grid.
* **Hide echo below N dBZ**: reflectivity below 5 dBZ is not drawn, as on the National Weather Service's
  own displays: it is mostly clear-air returns, insects, birds and noise. The number is a visible setting
  (here above the map and in Settings → Data → Live map; *nothing (all echo)* draws everything) and is
  written next to the colour legend and in the label on the map. It only changes what is drawn: the grid
  values, the exports and the hover values are not changed. If the weather is genuinely light, lower it;
  to remove clear-air echo properly, open the volumes and use the cleaning tools on them. A frame of the
  recent loop that the engine can no longer redraw keeps the number it was drawn with and says so.
* **Map**: the basemap is toned down under the echo, and the place names, roads and boundaries are drawn again
  **above** the echo so you can always tell where it is. Each radar's site and 230 km range ring are drawn in
  its colour; the rings are thin and dotted and can be switched off (*Range rings*). The view opens on the
  radars' range discs before the first picture exists, and on the picture afterwards.
* **Provenance**: the composite is **retrieved** (`RETR`: a merged, gridded product), the volumes behind it
  are **observed** Level II data. The layer is listed in the Layers panel with its provenance record, the
  analysis time (the newest volume start among its radars) and the radars it was merged from, and the chip
  at the top left of the map says what the picture is. It is not an official product.

## Using the data behind the picture

The volumes the live map downloaded are ordinary Level II files, and the app keeps them for as long as the
live map holds them. A row of buttons under the radar chips uses them; **none of them downloads anything**
(the download you allowed already fetched the files; the buttons say how many volumes and how many MB).

* **Open these volumes** (and **Open this volume** on each radar chip and in the left pane) opens the volumes
  as normal files: the 2D and 3D viewers, the 3D volume, Mosaic, Multi-Doppler, cells, modules, the cleaning
  tools and export all work on them. Opened volumes **stay when the live map updates** or stops: the app
  keeps its own copy of the file for each, says how much disk they use ("until you close them"), and gives
  the disk back when you close the file. At most 8 live volumes can be open as files at once. A volume that
  is already open is not opened twice.
* **View in 3D** opens the volumes and shows them in the 3D view, as for any opened file (the first radar is
  in front; the file tabs switch).
* **Multi-Doppler winds from these radars** opens the volumes, opens the Multi-Doppler panel with exactly
  these radars ticked, and runs it. If the radars do not overlap (or only one has a volume), the panel and the
  message under the status line show the engine's own explanation. Winds need two radars whose coverage
  overlaps and whose beams cross at a useful angle, so choose neighbouring radars ({doc}`MULTIDOPPLER`).
  The volumes of different radars are a few minutes apart; the panel warns when they are.
* **Save the composite** writes **NetCDF** (the merged grid with its provenance record, the analysis time and
  the data credit; the values are not floored) or a **georeferenced PNG** (as drawn, with the world file and
  projection file that GIS programs read, and the credit in the PNG's text). Both say where the volumes came
  from (NOAA NEXRAD Level II, distributed by Unidata on AWS Open Data, US government data, credit asked for)
  and that the product is not official. The buttons act on the newest frame; go back to it first if you are
  looking at the recent loop.

```{image} images/ui/live-open-3d.png
:alt: The volumes of the live map opened as files and shown in the 3D view
:width: 90%
:align: center
```

```{image} images/ui/live-multidoppler.png
:alt: Multi-Doppler winds from the three live radars, with the three radars ticked in the Wind panel
:width: 90%
:align: center
```
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
*Cancel* stops the live map and nothing is sent. Opening, viewing or analysing the volumes and saving the
composite need no further consent: they use the files the second dialog already fetched. **Offline mode** (Environment data settings) pauses the live
map before any request.

## Limits, bandwidth and memory

* NEXRAD only, from one public bucket; no account, no credentials. A radar of another network cannot be
  chosen.
* **Latency**: a volume appears in the bucket some minutes after the scan started (the age counts from the
  volume start in the file name; the time it reached the bucket is later). The live map is therefore always
  a few minutes behind the radar, and at best as fresh as the bucket.
* **Bandwidth**: one listing per radar per update (about 0.1 MB each) and one Level II file per radar only
  when it is new (typically 3 to 12 MB each). Nothing is prefetched.
* **Memory and disk**: the engine keeps one volume per radar (older ones are dropped, with their files, at
  most 8 radars and 768 MB by the listed sizes), and the app keeps at most 12 composite images. **Stop**
  releases the engine's volumes; closing the window does too. Volumes you opened as files are the exception:
  they stay until you close them (see above). A 1 km composite of three radars took about 2 seconds to merge
  in our test on a laptop and about 1 GB of working memory; the grid is coarsened automatically when a
  computer cannot afford that.
* **Failures**: a radar that has no recent volume or whose download fails is reported on its chip and left
  out; the composite is built from the rest, and only when no radar is usable is the update an error. After
  an error the next try waits the interval, then twice, four times the interval and so on up to an hour, so
  a dead network is not hammered and a radar that comes back is noticed within the hour. A lost engine
  connection fails the running update and the map says so; the frames stay.
* Every update is an operation in the status-bar tray (Cancel stops waiting; the engine's download itself
  cannot be aborted).
* The hover read-out of values is not available on the live composite itself; open the volumes and use the
  Mosaic panel on them for that. The 3D view of the *merged* composite is not offered either: *View in 3D*
  shows the opened radar volumes.
* The picture follows the display floor by redrawing the composite the engine still holds (it keeps the last
  few). An older frame of the loop may keep the floor it was drawn with.
* There is no forecast. The recent loop shows past composites only, each at its own analysis time; the
  frames are kept so that a nowcast layer can be built on them later, and such a layer will be labelled as
  a forecast with its lead time ({doc}`SCIENCE`).
