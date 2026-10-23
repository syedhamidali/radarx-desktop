# Rotation: shear, couplets and tracks

Rotation in Doppler velocity is the core signal of severe convection. Radarx Desktop offers three
steps, each a module built on [radarx](https://radarx.readthedocs.io/en/latest/):

| Module | What it gives | Scope |
|---|---|---|
| **Azimuthal Shear / Divergence (LLSD)** | the azimuthal shear of one sweep, in 1/s | one sweep |
| **Rotation Couplets** | compact maxima of the shear with peak shear, velocity difference and rotational velocity; with *Tilts needed* above 1 the couplet must continue on that many elevation angles (the vertical continuity of a mesocyclone signature) | one sweep or the volume |
| **Rotation Tracks** | the largest low-level shear over a sequence of volumes of one radar, drawn as a swath, with the couplets linked into tracks | many volumes |

```{admonition} An estimate, never a warning
:class: warning
Everything here is **RETRIEVED**: a quantity inferred from observed Doppler velocity by an
algorithm with assumptions ({doc}`SCIENCE`). It is **not a tornado warning, not a tornado detection
and not a damage estimate**. Gust fronts, convergence lines, ground clutter, noise and a wrong
unfolding of the velocity all produce shear. The width and length of a track are not the width and
length of any damage. How often the products are right (hits, misses, false alarms) has **not been
measured** on a case library; do not quote them as skill.
```

## Quick start

```bash
# azimuthal shear of the lowest Doppler sweep
rxd run radarx_shear -i KPAH20211211_030505_V06 --sweep 1 -o shear.nc -p shear.png

# couplets that continue on three tilts
rxd run radarx_couplets -i KPAH20211211_030505_V06 --sweep 1 -P min_tilts=3

# rotation tracks over the volumes of one radar (the open volume plus a folder)
rxd run radarx_rotation_track -i KPAH20211211_030505_V06 --sweep 1 \
    -P files=mayfield/KPAH -o tracks.json
```

In the app the same modules are under *Hazards* and *Kinematics* in the module panel.

## How it is computed

For each gate the radial velocities in a window of fixed size are fitted by least squares with a
plane `v = a + b s + c dr`, where `s` is the distance across the beam and `dr` along it (the linear
least-squares derivative of Smith and Elmore 2004 and Miller et al. 2013). The slope `b` is the
azimuthal shear in 1/s. For solid-body rotation it equals the angular velocity, half the vertical
vorticity. Positive is cyclonic in the Northern Hemisphere. The window is 2500 m across and 750 m
along the beam by default and always holds at least the neighbouring ray and gate on each side.

The velocity is unfolded first (region method; see {doc}`PIPELINES` for its limits and the fold
check). A rotation couplet is an 8-connected region of gates at or above a shear threshold, with its
peak shear, the inbound-outbound velocity difference at the range of the peak (within 5 km of arc)
and the rotational velocity, half that difference.

**Rotation tracks** use the lowest sweep with Doppler velocity (at most 1.5 degrees by default) of
every volume. In each grid cell of a 500 m grid centred on the radar the swath keeps the largest
shear that any volume had at the nearest gate; this is the idea of the maximum-azimuthal-shear
"rotation tracks" of the NSSL MRMS system (Smith et al. 2016), here from one radar and one tilt.
The couplets of successive volumes are linked by nearest neighbour when they lie within the fastest
plausible motion (40 m/s by default) of the last position of the track; a track needs at least three
volumes by default. The longest gap between the volumes is shown with the result.

## Settings that matter

| Setting | Default | Meaning |
|---|---|---|
| Other volumes | none | folder, pattern or comma-separated list of volumes of the same radar; the open volume is always used. A volume of another radar position or with a repeated scan time is skipped and said so. |
| Shear window | 2500 m across, 750 m along | larger windows smooth more and lower the shear of small vortices |
| Couplet threshold | 0.012 1/s | radarx itself uses 0.006 1/s; on five KPAH volumes that gave about 45 couplets per volume, most of them noise, so this module starts higher. The swath does not depend on it. |
| Minimum reflectivity | 10 dBZ | gates without echo are left out of the fit |
| Minimum range | 5 km | closer gates are left out |
| Volumes a track must span | 3 | shorter chains are dropped |
| Fastest plausible motion | 40 m/s | the speed limit of the link |

## Limits you must read with every result

* **Range-dependent resolution.** The beam widens with range (about 1 km at 57 km for a 1 degree
  beam), so the window holds fewer rays and the shear of a vortex narrower than about two beam
  widths is under-estimated, or not seen. The lowest tilt is also higher with range (0.7 km at
  60 km), so far from the radar it is not near the ground.
* **Near the radar.** The window spans a large angle: a vortex 15 km away reads about 20 % above its
  true shear. A plain uniform wind has azimuthal shear of about wind speed divided by range (20 m/s
  at 5 km: 0.004 1/s), which is not rotation; that is why the closest 5 km are left out.
* **Aliasing.** A velocity unfolded to the wrong interval makes fold lines that look like strong
  rotation. Check the dealiasing of the volume.
* **Sampling gaps.** A volume takes four to six minutes and nothing is observed between volumes.
  The swath is a set of snapshots, a short-lived vortex can fall between two, and a track across a
  gap above ten minutes may join different circulations.
* **One radar, one tilt.** Rotation that is only above the lowest tilt is not seen; a volume whose
  lowest velocity tilt is above the chosen elevation is skipped.
* **Opposite-sign lobes.** The flow outside a vortex core has shear of the opposite sign, weaker than
  the core's.

## What was tested

* A Rankine vortex (solid-body core of 3 km radius, 30 m/s at the core edge, so a true core shear of
  0.010 1/s) at 30, 60, 90 and 120 km: the peak shear is within 7 % below to 10 % above the true value
  and lies in the core; at 15 km it reads about 20 % high. With 1 m/s of velocity noise the peak
  stays within 25 %, and a field of noise alone stays below 0.012 1/s.
* A vortex moving east at 15 m/s through five volumes five minutes apart gives one track with a mean
  motion of 15 m/s toward the east within the test tolerance, and a swath along its path.
* Five volumes of the KPAH radar of 2021-12-11 (03:05 to 03:30 UTC) run through the whole module:
  with the settings above it gives a few tracks of three or more volumes. That run shows that the
  module works on real data; it says nothing about whether those tracks belong to tornadoes, and
  no detection skill or false alarm rate has been measured.

## References

* Smith, T. M., and K. L. Elmore, 2004: The use of radial velocity derivative to diagnose rotation
  and divergence. 11th Conf. on Aviation, Range, and Aerospace Meteorology, P5.6.
* Miller, M. L., V. Lakshmanan and T. M. Smith, 2013: An automated method for depicting mesocyclone
  paths and intensities. Wea. Forecasting, 28, 570-585.
* Smith, T. M., and Coauthors, 2016: Multi-Radar Multi-Sensor (MRMS) severe weather and aviation
  products: Initial operating capabilities. Bull. Amer. Meteor. Soc., 97, 1617-1630.
