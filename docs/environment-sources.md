# Environment data sources

Each source of environment data has its own page: what it is, which class of data it gives (model, reanalysis, observed
or retrieved), exactly what is downloaded, how a time is matched, what it costs, how it was checked and what it
cannot tell you. They all follow the same rules ({doc}`ENVIRONMENT`): nothing is downloaded before you have seen the
source, the number of files and the exact size and said yes; downloads are kept in a size-limited cache and reused
offline; every layer says where it came from, which run and valid time it has, and under which licence.

```{toctree}
:maxdepth: 1

source-hrrr
```

From the terminal the same sources are reached with `rxd env` ({doc}`ENVIRONMENT`, section "Command line").
