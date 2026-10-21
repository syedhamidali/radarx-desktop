# Radar sites

Radar sites are markers on the map: a RadarScope-like way to browse a network. Switch **Sites** on in the
toolbar above the 2D map (or Settings → Data → Radar sites), click a marker, and a card shows what is known
about that site and offers two actions: **Open latest volume** and **Browse archive**. The same markers appear
on the 3D ground.

The layer is **off by default**, loads its code only when you switch it on, and **never touches the network
without asking you first**.

## What a marker says

| Marker | Meaning |
| --- | --- |
| hollow ring | the age of the latest scan is **not known** (not checked this session). This is the neutral state. |
| solid | the engine reported a latest volume younger than 10 minutes |
| half filled | the engine reported a latest volume 10 minutes old or older ("stale") |
| accent halo | the site of the file on screen |
| diamond | a site from your own list |

A state exists only after you asked for **that site** in this session and the engine answered with an S3
listing. Nothing is guessed from the clock or the network, a site that was never checked stays neutral, and the
states are forgotten when you reload. They are never saved.

The state is never colour alone: hollow / solid / half-filled differ in shape, and the card spells it out in
words ("recent", "older than 10 min", the age in minutes) with the scan time in UTC.

Hovering or focusing a marker shows its id and name. Labels are thinned when sites would overlap (zoom in to see
more); the markers themselves always stay.

## The card

* Position, ground elevation, network and the source of the catalogue entry.
* **Latest scan**: the newest Level II volume name, its scan time (UTC, the volume start parsed from the file
  name), its age, its size, and when the listing was made (the engine's clock). Messages are explicit: not
  checked, listing, *no volume found today or yesterday (UTC)*, *offline: could not reach AWS*, or the engine's
  error text.
* **Open latest volume** opens exactly the file the card shows, through the Cloud data panel (progress,
  Cancel/Retry and the operations tray work as for any cloud open).
* **Browse archive** opens the Cloud data panel on the Archive tab with the site selected and today's folder listed.

Only NEXRAD sites can be fetched. A site from your own list for another network shows its position and details,
with the download buttons disabled and a note.

## Consent: what is asked, what is fetched

Every step that goes to the internet shows a dialog first, with **what**, **from where** and **how big**:

| Step | What is fetched | Size shown |
| --- | --- | --- |
| Check latest scan, Browse archive | A *listing* of `YYYY/MM/DD/<SITE>/` (today, and yesterday if today has none) in the public bucket `unidata-nexrad-level2`: file names, sizes, times. No radar data. | About 0.1 MB at most (an estimate: one entry of a few hundred bytes per volume, roughly 290 volumes a day) |
| Open latest volume | One Level II file, **named in the dialog** | The exact size, taken from the listing |

The choices are **Allow once**, **Allow for this session** and **Cancel**. "This session" lives in memory only and
is forgotten when you quit or reload; Cancel never changes anything and nothing is sent. Escape cancels. The
requests are anonymous and read-only (no account, no credentials). In the desktop app they go from your machine.

## The site catalogue

The built-in list has **156 NEXRAD WSR-88D sites** (id, name, state,
latitude, longitude, ground elevation).

* **Source:** NOAA National Centers for Environmental Information, *Historical Observing Metadata Repository*
  (HOMR) NEXRAD station list, `https://www.ncei.noaa.gov/access/homr/file/nexrad-stations.txt`, downloaded
  **2026-10-19**. The file header repeats the source and date.
* **Licence / usage:** NOAA/NWS information is in the public domain and may be used without charge for any
  lawful purpose, provided you do not claim it is your own, imply NOAA/NWS endorsement, or present modified
  content as official government material ([NWS disclaimer](https://www.weather.gov/disclaimer), US government
  work, 17 U.S.C. 105). The HOMR file itself carries no separate licence statement; it is a NOAA product and is
  used under the same terms. The catalogue here is a transcription, not official NWS material.
* **What was changed:** only the 156 sites the engine can fetch are kept; coordinates are rounded to 5 decimals
  (about 1 m); elevation is converted from feet to metres; names are title-cased, and ten truncated names are
  spelled out (for example `NORFOLK RICH` is `Norfolk/Richmond`).
* **Cross-check:** every site is compared with the engine's own coordinate table (from the same NCEI list and checked
  against the Py-ART table); they must agree.

## Your own sites (other networks)

Settings → Data → **Radar sites** loads a CSV or JSON file of your own sites; it is read in the window and never
uploaded, and is remembered on this computer.

```
id,name,lat,lon,elev_m,network
ESS,Essen,51.4056,6.9672,185,DWD
```

* CSV: a header row, then one site per line (comma, semicolon or tab; double quotes allowed). JSON: a list of
  objects, or `{"network": "DWD", "sites": [...]}`. Aliases accepted: `latitude`, `lng`/`longitude`,
  `elevation`, `site`/`code`.
* Latitude -90..90, longitude -180..360 (values above 180 wrap), id 1-16 characters of `A-Z a-z 0-9 _ . -`.
  A missing elevation stays missing (it is not shown as 0). Up to 5000 sites.
* Rows that do not validate are listed (with the row number) and skipped; the first row of an id wins; a site
  whose id is also a built-in id replaces it.
* *Include the built-in NEXRAD list* can be switched off to show only your sites.

Other networks cannot be downloaded: only AWS NEXRAD Level II can be fetched. Open their files from disk.

## Keyboard

Tab enters the layer (one stop), the arrow keys move to the nearest marker in that direction, Enter or Space
opens the card, Escape closes the card (or the consent dialog). Every marker has an accessible name that
includes its state in words. Motion respects `prefers-reduced-motion`; colours follow the theme.

## 3D

With the ground map on, sites inside the displayed range are drawn on the ground with the same projection as
the echoes and the map under them (hollow / half / solid ring for the same three states, the id as a thinned
label). The 3D markers are display only: click a site on the 2D map for its card.

## Limits

* The map exists while a file is open (it is centred on that radar), so markers cannot be browsed before opening a file.
* Marker state covers only the sites you checked this session; there is no network-wide "which radars are up" view,
  because that would be 156 requests the user did not ask for.
* "Latest" is the newest file in today's or yesterday's UTC folder. A radar that stopped sending shows an old
  (stale) scan, or *no volume found*; the app cannot tell why.
* The age counts from the volume start time in the file name; the time the file reached S3 (`last_modified`)
  is later, by an amount this app does not estimate.
* 3D markers are shown in the polar 3D viewer, not the Cartesian one.
