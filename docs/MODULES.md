# Writing modules (plugins)

A module is **one Python file**. Drop it in a modules folder and it shows up in the
**Modules** panel with an auto-generated parameter form, a Run button, result
tables and a map overlay — no server or UI changes.

Built-in examples: `python/modules/storm_detection.py`, `cell_size_sort.py`, `zdr_arc.py`.
Start from `python/modules/_template.py`.

## Where modules are loaded from

| Location | Use |
|---|---|
| `python/modules/` | ships with the app |
| `~/.xradar-desktop/modules/` | personal modules |
| `$XRADAR_MODULES_DIR` (`:`-separated) | project/shared folders |

Later locations override earlier ones with the same `id`. A module that fails to
import is skipped and listed under *Load errors* in the panel; it never stops the
sidecar. Press **Reload** in the panel after editing a file.

> Modules run with the privileges of the app. Only install modules you trust.

## Anatomy

```python
from engine.plugins import module, param, Result

@module(id="hot_gates", title="Hot Gates", category="Demo",
        requires=["DBZH"],
        params=[param("threshold", "number", 50.0, "Threshold (dBZ)", min=0, max=80)])
def run(ctx, threshold):
    z = ctx.field("DBZH").values
    return Result().scalars({"Gates above": int((z >= threshold).sum())})
```

* `requires` — canonical names (`DBZH`, `ZDR`, `PHIDP`, `KDP`, `RHOHV`, `VRADH`, `WRADH`).
  Aliases such as `DBZ`, `DBZHC`, `differential_reflectivity` resolve automatically; the
  Run button is disabled with an explanation when a file lacks one.
* `params` — `number`, `int`, `bool`, `select` (needs `options`), `string`. Values are
  validated and clamped before your function sees them.
* `ctx` — `field(name)` (raises a clear error if missing), `find`/`has` for optional
  fields, `polar_coords()` → `(azimuth_deg, range_m)`, `dataset`, `sweep`, `progress(pct, msg)`.

## Results

Build a `Result` from any mix of blocks:

| Call | Renders as |
|---|---|
| `res.scalars({...})` | summary grid |
| `res.table(title, columns, rows, id_key=...)` | sortable table; clicking a row highlights the overlay feature with the same id |
| `res.overlay(title, [polygon(...), point(...), line(...)])` | drawn over the PPI. `frame="polar"` = `[azimuth_deg, range_m]`, `frame="xy"` = `[x_m, y_m]` |
| `res.text(body)` | notes / caveats |

NaN/Inf and numpy types are cleaned automatically.

## Protocol (for tooling)

```
→ {type:"list_modules", reload?:bool}      ← {type:"modules_list", modules, errors, dirs}
→ {type:"run_module", module, sweep, params, file_id?}
                                           ← {type:"module_result", module, sweep, params, elapsed_ms, blocks}
                                           ← {type:"module_error", module, message}
```

## Notes

* A built-in module that imports `modules/_radar_utils.py` relies on that file being next
  to it. A single-file personal module should copy the few helpers it needs.
* Heavy math should be vectorized NumPy/SciPy. If a kernel is still slow after that, it is
  the right candidate for a native (Rust/PyO3) implementation behind the same module API.
