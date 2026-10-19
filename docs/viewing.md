# Viewing data

## The 2D view

A GPU-rendered PPI on a map. Pick the variable and the sweep in the bar above the map; the colour scale, range
rings, geographic overlay and the map opacity are one click away. Scroll to zoom, drag to pan. The probe reads the
value under the pointer; sector scans and rays with gaps are drawn as measured, and the probe reports nothing
where nothing is drawn.

```{image} images/ui/2d-view.png
:alt: 2D view of a squall line
:width: 100%
```

The colour map of a variable can be changed with *Edit* next to the colour bar (the same choice is used in the
3D view). The timeline under the map steps through the sweeps (`Home`/`End`, the play button and 0.5×/1×/2×
speed).

## The 3D view

*3D* ray-marches the whole volume on the GPU. Orbit with the mouse, zoom with the wheel, pan with `Shift` + drag,
reset with `R`. The toolbar sets the step, opacity, the value below which gates are hidden, the vertical
exaggeration and the range; *TF* opens the transfer-function editor that shapes colour and opacity per value.
The ground can show the map.

```{image} images/ui/3d-view.png
:alt: 3D view of the same squall line
:width: 100%
```

To focus on one storm, go back to 2D, use the box-select tool to draw a box and switch to 3D: the view clips to
that column. The 3D view lowers its quality on its own if it gets slow (see {doc}`RESPONSIVENESS`).

## Variable mapping

Open *Variable mapping* in the left panel to see which variable plays each role (reflectivity, velocity, ZDR, ...)
for the modules, and to override it for this file.

```{image} images/ui/variable-mapping.png
:alt: The variable mapping section
:width: 35%
:align: center
```

Here the velocity role resolves to `VRADH` on sweep 1: NEXRAD split cuts put velocity on the sweep next to the
reflectivity one, and the mapping finds it there. {doc}`VARMAP` has the details.
