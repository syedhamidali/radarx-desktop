"""Check that the swirl on screen turns at omega = v / r.

The page can draw a top-down slice of the flow-advected noise that makes the funnel's streaks (``renderSwirlSlice``,
the same ``flowNoise`` the volume shader uses, at z = 60 m). This script draws the slice at two times, samples rings of
different radius around the axis in polar form, finds the azimuthal shift between the two images by circular
cross-correlation (sub-pixel) and compares the angular velocity it gives with the field's omega(r, z) (tornado vortex
plus the couplet, i.e. what the shader advects with). Needs the same scratch environment as render_tornado.py
(playwright, pillow, numpy)::

    npm run build
    /path/to/venv_clip/bin/python tools/tornado_sim/check_rotation.py
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_tornado import serve  # noqa: E402

from engine.animate_tiles import TileCache  # noqa: E402,I001


async def main_async(a: argparse.Namespace) -> int:
    import numpy as np
    from PIL import Image
    from playwright.async_api import async_playwright

    seed = json.loads(Path(a.seed).expanduser().read_text())["seed"]
    srv, port = serve(Path(a.app_dist).expanduser().resolve(), TileCache(None, offline=True))
    size = 900
    z = 60.0
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel="chrome",
            headless=True,
            args=["--no-proxy-server", "--use-gl=angle", "--use-angle=metal", "--ignore-gpu-blocklist"],
        )
        page = await browser.new_page(viewport={"width": size, "height": size}, device_scale_factor=1)
        await page.goto(f"http://127.0.0.1:{port}/?tornado=1")
        await page.wait_for_function("() => !!window.__tornado")
        await page.evaluate(
            "(c) => window.__tornado.init(c)",
            {"seed": seed, "width": size, "height": size, "tiles": False, "debrisCount": 10, "renderScale": 0.25},
        )

        async def grab(t: float):
            await page.evaluate("([t, single]) => window.__tornado.slice(t, single)", [t, a.single])
            png = await page.screenshot(type="png")
            return np.asarray(Image.open(io.BytesIO(png)).convert("L"), dtype=np.float64)

        half_m = 600.0  # the slice is orthographic over +-600 m

        def ring(img, r_m, n=720):
            ang = np.arange(n) * 2 * np.pi / n
            px = (r_m * np.cos(ang) / half_m * 0.5 + 0.5) * (size - 1)
            py = (0.5 - r_m * np.sin(ang) / half_m * 0.5) * (size - 1)
            x0 = np.floor(px).astype(int)
            y0 = np.floor(py).astype(int)
            fx = px - x0
            fy = py - y0
            v = (
                img[y0, x0] * (1 - fx) * (1 - fy)
                + img[y0, x0 + 1] * fx * (1 - fy)
                + img[y0 + 1, x0] * (1 - fx) * fy
                + img[y0 + 1, x0 + 1] * fx * fy
            )
            return v - v.mean()

        results = []
        worst = 0.0
        for t in a.times:
            im0 = await grab(t)
            for r in a.radii:
                om = await page.evaluate("([r, z]) => window.__tornado.omega(r, z)", [r, z])
                # the step: about 0.3 rad of turn (at most 1.2 s), inside one re-seeding cycle of the flow noise (3 s)
                dt = min(1.2, 0.3 / abs(om["total"]))
                im1 = await grab(t + dt)
                f0, f1 = ring(im0, r), ring(im1, r)
                n = len(f0)
                # circular cross correlation via FFT: the shift of f1 relative to f0 (positive = counter-clockwise)
                c = np.fft.irfft(np.conj(np.fft.rfft(f0)) * np.fft.rfft(f1), n)
                k = int(np.argmax(c))
                y0_, y1_, y2_ = c[(k - 1) % n], c[k], c[(k + 1) % n]
                den = y0_ - 2 * y1_ + y2_
                frac = 0.5 * (y0_ - y2_) / den if den != 0 else 0.0
                shift = (k + frac) if k <= n // 2 else (k + frac - n)
                om_meas = shift * 2 * np.pi / n / dt
                exp = om["total"]
                err = (om_meas - exp) / exp * 100
                worst = max(worst, abs(err))
                results.append(
                    {
                        "t": t,
                        "r_m": r,
                        "omega_measured": om_meas,
                        "omega_field": exp,
                        "omega_tornado": om["tornado"],
                        "omega_meso": om["meso"],
                        "error_pct": err,
                    }
                )
                print(
                    f"t={t:6.1f} r={r:5.0f} m  measured {om_meas:7.4f} rad/s  field {exp:7.4f} rad/s "
                    f"({om['tornado']:.4f} + {om['meso']:.4f})  error {err:+5.1f} %  period {2 * np.pi / om_meas:6.1f} s"
                )
        await browser.close()
    srv.shutdown()
    print(f"worst error {worst:.1f} %")
    if a.out:
        Path(a.out).write_text(json.dumps(results, indent=1))
    return 0 if worst < 5.0 else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--seed", default=str(Path.home() / "Downloads/xradar-exports/perils_movie/tornado_sim/tornado_seed.json")
    )
    ap.add_argument("--app-dist", default=str(REPO / "dist"))
    ap.add_argument("--times", type=float, nargs="+", default=[450.75, 459.75, 468.75], help="data times; t mod 3 s = 0.75 keeps the step inside one flow-noise cycle")
    ap.add_argument("--radii", type=float, nargs="+", default=[45.0, 70.0, 110.0, 160.0, 240.0])
    ap.add_argument("--single", action="store_true", help="one phase of the flow noise only (exact rigid transport), instead of the blend the volume shader draws")
    ap.add_argument("--out", default=None)
    raise SystemExit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()
