# What's new

```{include} changes/unreleased.md
```

## 0.2.1

- **Open With:** right-click a radar file → *Open With → xradar-desktop*, including numbered copies
  (`file.nc.1`) and, on macOS, files without an extension.
- **Web version:** the engine runs as a secure web service and the app as a static site.
- **Fixed** the 2D azimuth jump on sector scans and rays with gaps; the hover probe no longer reports values
  where nothing is drawn; mosaics no longer show flat plates where no radar saw an echo.

## 0.2.0

- Pipelines, variable mapping, multi-Doppler winds, unified storm-cell tracking, seven more radarx modules,
  reliability work (timeouts, memory budget, no-echo areas in exports).

## 0.1.x

- First installers for macOS, Windows and Linux with the engine bundled; export of cleaned data;
  radarx-based quality control; mosaics; 3D view.
