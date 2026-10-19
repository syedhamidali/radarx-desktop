"""Check the GPU time interpolation against the physics, in a real browser.

A synthetic vortex (a blob on a 22 km ring) is carried from volume A to volume B by a solid-body
rotation of 2e-3 rad/s over 300 s (0.6 rad, an eyewall at 60 m/s). The shader that interpolates
between the two volumes (src/lib/utils/morphPass.ts) must put the echo at the angle
theta0 + s * omega * dt, as ONE sharp echo (a plain cross-fade gives two weaker images: its peak is lower).

    npm run build
    /tmp/clipenv/bin/python tools/storm_clip/verify_morph.py        # needs playwright (see render_clip.py)
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import render_clip


async def main() -> int:
    from playwright.async_api import async_playwright

    dist = render_clip.REPO / "dist"
    with tempfile.TemporaryDirectory() as tmp:
        srv, port = render_clip.serve(dist, Path(tmp))
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                channel="chrome",
                headless=True,
                args=["--use-gl=angle", "--use-angle=metal", "--ignore-gpu-blocklist"],
            )
            page = await browser.new_page()
            await page.goto(f"http://127.0.0.1:{port}/?clip=1")
            await page.wait_for_function("() => !!window.__xrClip", timeout=30000)
            res = await page.evaluate("() => window.__xrClip.morphSelfTest()")
            await browser.close()
        srv.shutdown()
    ok = True
    for r in res:
        good = abs(r["errorDeg"]) < 3.0 and r["peak"] > 0.9 and r["crossfadePeak"] < r["peak"] - 0.2
        ok &= good
        print(
            f"s={r['s']:.2f}: expected {r['expectedDeg']:6.2f} deg, measured {r['measuredDeg']:6.2f} deg "
            f"(error {r['errorDeg']:+.2f}), radius {r['radiusKm']:.1f} km, echo peak {r['peak']:.2f} "
            f"(plain cross-fade {r['crossfadePeak']:.2f})  {'OK' if good else 'FAIL'}"
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
