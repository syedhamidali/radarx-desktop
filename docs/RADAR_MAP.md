# Radar network map: which radars exist and which data you can load

The **Map** tab of the Cloud data panel (and *Radar network map* in the command palette) opens a world map of
every weather radar in the bundled catalogue: 1005 sites on six continents. Each marker says what the app can do
with that radar's data, and each site's card says where its position comes from, under which licence, and
offers the actions that exist for it.

The catalogue ships inside the app: opening the map downloads nothing. Map tiles follow your basemap setting
(choose *None* to send no request at all), and **every listing or download of radar data asks you first**, with
the host and the size, exactly as the radar-site markers do ({doc}`RADAR_SITES`).

## What a marker means

The marker **shape** carries the access class; the legend and the card spell it out in words, so nothing depends
on colour alone. A number inside a marker is a group of radars: click it to zoom in, or zoom past level 7 to see
every site on its own.

| Marker | Access class | Meaning |
| --- | --- | --- |
| filled circle | **Loadable** | The app opens volumes from an open, anonymous source: *Open latest scan* and *Choose a time (archive)* work. |
| square | **Open data (link)** | Open data exist (often composite products rather than single-site volumes), but the app does not load them yet. The card links to them. |
| triangle | **Key required** | A free key or registration is needed. The app does not embed or ask for keys yet; the card links to the portal. |
| cross | **Restricted** | No open volume data is known. |
| hollow ring | **Unknown** | The position is known; no open data source has been verified for this network. This is the neutral state, not a judgement. |

The access class is a statement about what was *verified* when the catalogue was built, not about the data
policy of the operator: a radar marked *unknown* may well have open data that nobody has checked yet
(see [Asking for more](#asking-for-more)).

## The card

Click a marker (or a search result) for its card:

* id, name, country, network, band and polarisation where the source gives them, declared status;
* **position** and elevation as given by the source (never computed here), WMO and WIGOS ids when known;
* **source**, retrieval date, **licence** and **credit** of the catalogue entry;
* for loadable sites, the **latest volume**: name, scan time (UTC, from the file name), age, size and number
  of files, listed only after you agreed to the listing;
* the actions: **Open latest scan**, **Choose a time (archive)**, **Open the provider's page**, and
  **Set as default radar**, which adds the radar to the default radars of the live map ({doc}`LIVE`). It works
  for NEXRAD and DWD radars, the two networks the live map can fetch; for every other radar the button is
  disabled and its tooltip gives the reason (for example, the Finnish bucket is a fixed sample, not a live
  feed, or no open volume feed is known for that network).

*Choose a time* lists one UTC day of volumes with their time, size and number of sweeps; incomplete volumes
(a sweep still being transmitted) are shown but cannot be opened. NEXRAD sites open the Cloud panel's Archive
tab instead, as the radar-site markers do.

## Which networks are loadable today

Verified from the development machine on 2026-10-21/22 by listing the source and opening a volume in the app:

| Network | Sites | Source | What is loaded | Licence and credit |
| --- | --- | --- | --- | --- |
| NEXRAD (United States) | 156 | Unidata mirror of NOAA NEXRAD Level II on AWS S3, anonymous | the newest Level II volume, or any volume of a day | US government data; credit NOAA/NWS and Unidata |
| DWD (Germany) | 17 | DWD Open Data, `opendata.dwd.de`, anonymous | the last two days: ten sweeps every five minutes, reflectivity (DBZH) and radial velocity (VRADH), one ODIM HDF5 file per sweep and moment; the app downloads the twenty files of a volume and assembles one volume of ten sweeps | CC BY 4.0 with the source acknowledged ("Source: Deutscher Wetterdienst") |
| FMI (Finland) | 10 | FMI open data on AWS, public bucket `fmi-opendata-radar-volume-hdf5`, anonymous | full polar volumes (ODIM HDF5, 16 moments including DBZH, VRADH, ZDR, KDP, RHOHV) of **a two-day sample, 7 and 8 February 2023**. The bucket is not a live feed; the card says so and *latest* means the newest file of the sample | CC BY 4.0; credit the Finnish Meteorological Institute |

The verification opened an Essen (DWD) volume of 21 October 2026 21:50 UTC: 20 files, 1.5 MB, ten sweeps from
0.5 to 25 degrees, 360 rays, 720 gates of 250 m at the low elevations; and an Anjalankoski (FMI) volume of
7 February 2023 10:35 UTC: 13.8 MB, ten sweeps, 16 moments. Both open through the standard ODIM reader; the
DWD assembly joins the reflectivity and velocity files of each sweep, which share the same rays and gates
(if a pair ever differed, the sweep would keep both on their own coordinates and the open would say so).

### Credit, licence and provenance

What you open from a network carries that network's credit line and licence in its provenance (the OBS chip
of the layer, the Layers panel) and in every export (`data_attribution`, `data_licence`,
{doc}`ENVIRONMENT`). Figures made from these data should credit the source as its licence asks.

## Open sources that are not loaded yet

Checked from the development machine; the card of each site links to the portal.

| Network | What exists | Why it is not loaded |
| --- | --- | --- |
| Environment and Climate Change Canada (29 sites) | MSC Datamart and GeoMet: CAPPI, DPQPE, 24 h accumulation and composites as GIF, GeoTIFF and WMS; Open Government Licence - Canada | products, not volume scans |
| Met Office (United Kingdom, 16 sites) | UK radar observations on AWS Open Data, CC BY-SA 4.0 | composite products, not single-site volumes |
| MET Norway (12 sites) | Nordic reflectivity composites on THREDDS | composites, not single-site volumes |
| Australia (66 sites) | the operational radar archive on NCI THREDDS: level 1b ODIM HDF5 in daily archives, CC BY-NC-SA 4.0, months behind real time | daily archive files of hundreds of MB; non-commercial share-alike terms would attach to everything made from them |
| IDEAM (Colombia) | level II IRIS/Sigmet files in a public AWS bucket, CC BY 4.0, updated as data arrive | no radar positions from a source the catalogue uses, so the sites are not on the map yet |
| KNMI (Netherlands, 3 sites) | radar volumes (ODIM HDF5) on the KNMI Data Platform | a free API key is required; the app does not handle keys yet |

Not verified, hence *unknown* on the map: Météo-France, MeteoSwiss, DMI (Denmark), the Japanese, Korean,
Indian and Chinese networks, and the rest of the OSCAR list. OPERA data for all of Europe are available to
external users through MeteoGate under CC BY 4.0, but that service was not exercised.

## The catalogue: sources and licences

| Source | Sites taken | Licence and terms | Attribution |
| --- | --- | --- | --- |
| NOAA NCEI Historical Observing Metadata Repository, NEXRAD station list (downloaded 2026-10-19) | 156 | public domain (US government work); NOAA asks for credit | NOAA National Centers for Environmental Information |
| EUMETNET OPERA radar database (`OPERA_RADARS_DB.json`, downloaded 2026-10-22) | 217 (28 European countries; positions, WMO and WIGOS ids, ODIM codes, band, polarisation, status) | the OPERA page calls the database "public information, location and details of the radars"; the metadata table carries no separate licence text (OPERA radar *data* are CC BY 4.0 via MeteoGate) | EUMETNET OPERA |
| WMO OSCAR/Surface, station class *weather radar* (downloaded 2026-10-22) | 632 of 943 (311 were the same radars as above, within 2 km, and kept their HOMR or OPERA entry) | public WIGOS metadata repository of the WMO; its terms-of-use page could not be reached when the catalogue was built, so the terms are recorded as not verified | World Meteorological Organization, OSCAR/Surface |

Every entry names its source and retrieval date; the licence shown on the card is that source's. The DWD
positions from OPERA were cross-checked against the `where` attributes of each radar's own ODIM file: all 17
agree within 20 m. Nothing was taken from Wikidata or OpenStreetMap.

The catalogue is a transcription for finding radars; it is not an official product of NOAA, EUMETNET or the
WMO, and a position or status may be out of date. Check the operator's own information before relying on it.

## Limits

* Marker state covers only the sites you checked this session; there is no network-wide "which radars are
  up" view, because that would be a thousand requests you did not ask for.
* *Latest* for DWD is the newest **complete** five-minute volume (all ten reflectivity sweeps present); the
  one being transmitted is listed as incomplete. DWD keeps about two days; older volumes are not available
  from that server. The listing itself is two folder indexes of about 1 MB each.
* The FMI bucket is a fixed two-day sample. For live Finnish data the app has no provider yet.
* Live default radars: NEXRAD and DWD only. The South African radars (14 in the catalogue) have no open
  volume feed that was found, and the European OPERA volumes need a registration that was not verified, so
  they cannot be set as default radars ({doc}`LIVE`, *Which radars are live*).
* The DWD provider loads reflectivity and radial velocity; the polarimetric moments DWD also publishes
  (ZDR, RhoHV, PhiDP) are not fetched yet.
* Open-link and key-required sites show a link; the app does not fetch anything from them.
* Positions are the sources' values; an *unknown* access class says only that nothing was verified.

## Asking for more

To add a network, the catalogue needs a public source for the positions and the data need an open, anonymous
listing and files xradar can read (ODIM HDF5, CfRadial, IRIS/Sigmet, Rainbow, NEXRAD Level II, ...). Tell us
the URLs and the licence; a network whose positions and licence can be verified from a plain download can be
added to the next release.
