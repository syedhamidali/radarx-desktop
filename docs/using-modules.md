# Using modules

Algorithms are **modules**. Open the *Modules* panel: they are grouped by category (dual-pol, hazards,
kinematics, storm, quality control, ...), and a module whose input is missing says so instead of failing.

```{image} images/ui/modules-panel.png
:alt: The modules panel
:width: 35%
:align: right
```

- **Run** runs the module on the sweep you are looking at. The result is a table, an overlay on the map, scalars
  and text, and often a new field.
- **All sweeps → 3D** runs it on every sweep that has the inputs and stores **one** derived variable.
- Each module has parameters (click the arrow); defaults are sensible.

## Derived variables

A module run on all sweeps leaves a derived variable that behaves like any measured one: choose it in the
variable list, view it in 2D or 3D, export it with the data, or use it as input for another module.

```{image} images/ui/modules-result.png
:alt: Result of a hydrometeor classification on all sweeps
:width: 35%
:align: right
```

*View in 2D* and *View in 3D* switch the view to it; *Remove* deletes it. Here a hydrometeor classification was run
on 14 of the 17 sweeps of the KTLX volume (the other three have no dual-pol fields) and the legend shows the
classes.

```{image} images/ui/derived-2d.png
:alt: Hydrometeor classes in the 2D view
:width: 100%
```

## What is included

Hydrometeor classification, KDP, rain rate (QPE), hail (SHI/MESH), rotation couplets, azimuthal shear, ZDR arcs and
columns, KDP columns, VVP winds, beam blockage, tornado probability, melting layer, quasi-vertical profiles,
ZDR calibration, quality control (with radarx or with simple thresholds), storm cell detection and size sorting.
Each module, its inputs, parameters and the method behind it are described in {doc}`MODULES`.
