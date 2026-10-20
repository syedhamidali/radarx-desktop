"""Hydrology tools: radar rainfall estimation, accumulation, gauges and basins.

Modules (import them explicitly; nothing heavy is imported here):

``qpe``         rain-rate relations, hybrid, uncertainty and quality of one sweep (on radarx)
``grid``        sweeps on a common map grid, best-beam / weighted compositing of radars
``accumulate``  accumulation over a period with advection-corrected interpolation between scans
``gauges``      gauge files, quality control, bias adjustment, validation metrics
``basins``      basin polygons, exact area-weighted statistics, basin series, return-period hook
``products``    CF NetCDF, map PNG, figure and tables with provenance (RETRIEVED / ADJUSTED)
``cli``         the ``rxd qpe`` and ``rxd basin`` commands
``ws``          the WebSocket messages of the QPE panel

See ``docs/HYDROLOGY.md`` for the equations, the references and the limits.
"""
