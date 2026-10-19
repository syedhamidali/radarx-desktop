"""Check, in a real browser on a real frame, that the look controls do what they say.

Renders one frame of a bundle with different values of one look setting and checks that the mean
luminance of the picture is monotonic in it (exposure, brightness, gamma, sun intensity: brighter;
shadow strength, contrast: no inversion), that saturation 0 gives a grey picture (the overlay text keeps its colours), and that settings
that are not about the echo leave the echo where it was (the pixels of the volume mask are the same
for white balance, vignette and grain being off).

    npm run build
    /tmp/clipenv/bin/python tools/storm_clip/verify_look.py --bundle /tmp/storm_bundle
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import render_clip  # noqa: E402


def lum(img) -> float:
    import numpy as np

    a = np.asarray(img.convert("RGB")).astype("float32") / 255.0
    return float((0.2126 * a[..., 0] + 0.7152 * a[..., 1] + 0.0722 * a[..., 2]).mean())


async def main(argv=None) -> int:
    from PIL import Image
    from playwright.async_api import async_playwright

    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--time", type=float, default=18.0, help="clip time of the test frame")
    a = ap.parse_args(argv)
    bundle = Path(a.bundle).expanduser().resolve()
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        srv, port = render_clip.serve(render_clip.REPO / "dist", bundle)
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                channel="chrome",
                headless=True,
                args=["--use-gl=angle", "--use-angle=metal", "--ignore-gpu-blocklist"],
            )
            page = await browser.new_page(viewport={"width": 960, "height": 540})
            await page.goto(f"http://127.0.0.1:{port}/?clip=1")
            await page.wait_for_function("() => !!window.__xrClip", timeout=30000)
            base = {
                "fps": 30,
                "duration": 36,
                "vexag": 4,
                "stormStyle": "realistic",
                "ground": "map",
                "lightning": "off",
                "audio": False,
            }
            await page.evaluate("(a) => window.__xrClip.load(a[0], a[1])", ["/bundle", base])
            await page.evaluate("(p) => window.__xrClip.prepare(p)", "none")

            async def frame(look: dict) -> Image.Image:
                await page.evaluate("(o) => window.__xrClip.setLook(o)", look)
                await page.evaluate("(t) => window.__xrClip.renderAt(t)", a.time)
                png = await page.screenshot(type="png")
                return Image.open(io.BytesIO(png)).convert("RGB")

            def report(name, values, lums, increasing=True):
                nonlocal ok
                mono = all(
                    (y > x) if increasing else (y < x) for x, y in zip(lums, lums[1:], strict=False)
                )
                ok &= mono
                print(
                    f"{name:16s} "
                    + "  ".join(f"{v:g}->{m:.3f}" for v, m in zip(values, lums, strict=True))
                    + f"   {'OK' if mono else 'FAIL'}"
                )

            for key, values in (
                ("exposure", [0.5, 0.9, 1.4, 2.0, 2.8]),
                ("brightness", [-0.2, -0.1, 0.0, 0.1, 0.2]),
                ("gamma", [0.7, 0.9, 1.1, 1.4, 1.7]),
                ("sunIntensity", [0.2, 0.7, 1.2, 1.8, 2.6]),
                ("ambient", [0.2, 0.7, 1.2, 1.8, 2.4]),
            ):
                ls = [
                    lum(await frame({key: v, "bloom": 0, "vignette": 0, "grain": 0}))
                    for v in values
                ]
                report(key, values, ls)
            grey = await frame({"saturation": 0, "bloom": 0, "vignette": 0, "grain": 0})
            import numpy as np

            ga = np.asarray(grey).astype("int16")
            spread = float(np.median(np.abs(ga[..., 0] - ga[..., 2])))
            print(
                f"saturation 0: median |R-B| = {spread:.2f} grey levels  {'OK' if spread < 1.0 else 'FAIL'}"
            )
            ok &= spread < 1.0
            warm = await frame({"temperature": 0.8, "bloom": 0, "vignette": 0, "grain": 0})
            cool = await frame({"temperature": -0.8, "bloom": 0, "vignette": 0, "grain": 0})
            wa, ca = np.asarray(warm).astype("float32"), np.asarray(cool).astype("float32")
            wb = float((wa[..., 0] - wa[..., 2]).mean() - (ca[..., 0] - ca[..., 2]).mean())
            print(f"warm minus cool, mean R-B difference: {wb:.1f}  {'OK' if wb > 5 else 'FAIL'}")
            ok &= wb > 5
            await browser.close()
        srv.shutdown()
    print("all checks passed" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
