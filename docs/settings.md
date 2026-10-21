# Settings

```{image} images/ui/settings.png
:alt: The settings dialog
:width: 90%
:align: center
```

The *Appearance* choice switches between the dark theme (*Mission control*), a light theme (*Clean room*) and the
system setting; the accent colour and font size are next to it.

```{image} images/ui/settings-light.png
:alt: The settings dialog in the light theme
:width: 60%
:align: center
```

The gear in the top bar opens the settings: the default data folder, the colour theme, the map background, the
3D quality and what the interface remembers between sessions. Keyboard shortcuts are listed under `?`.

The **Updates** tab (desktop app only) shows the version, checks for a new one (*Check now*) and holds two switches:
*Check automatically on launch* (on by default; at most one check every 24 hours) and *Download in the background*
(off by default; off means the app asks first). A check is one request to the Radarx download server; your version,
system and any identifier are not sent. A new version is announced by a chip in the status bar and installed only when
you choose *Install and restart*; the restart waits while an export, pipeline, module or other job is running. Every update
file is signed, and the app verifies the signature before installing, so a file that was altered is rejected and nothing
is installed. The browser version has no update feature.

The **Data** tab also holds the **Radar sites** switch and the place to load your own list of sites (CSV or JSON); see
{doc}`RADAR_SITES`. Its **Movie components** line (desktop app only) shows whether the optional download the Movie
Studio needs (Playwright and PyAV, about 180 MB on disk) is installed, for which version, how big it is and where it
lives, with a **Remove** button; the download itself is offered in the Movie Studio, never here (see {doc}`ANIMATE`).
