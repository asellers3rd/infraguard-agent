"""Capture a polished screenshot of a website using Playwright.

Usage:
    python scripts/screenshot.py <URL> <output_path> [--width 1280] [--height 800] [--wait-ms 4000]

The script:
- Launches headless Chromium
- Waits for network-idle (best signal that the SPA finished loading)
- Adds an extra wait so animations / chart renders settle
- Saves a PNG at the given path

Requires:
    pip install playwright
    playwright install chromium
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright


async def capture(url: str, output: Path, width: int, height: int, wait_ms: int) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": width, "height": height},
            device_scale_factor=2,  # Retina-quality
        )
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=60000)
        except Exception as exc:
            print(f"Warning: networkidle never reached ({exc}); proceeding anyway")
        await page.wait_for_timeout(wait_ms)
        await page.screenshot(path=str(output), full_page=False)
        await browser.close()
    print(f"Saved screenshot to {output} ({output.stat().st_size // 1024} KB)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture website screenshot via Playwright")
    parser.add_argument("url", help="URL to capture")
    parser.add_argument("output", help="Output PNG path")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument("--wait-ms", type=int, default=4000, help="Extra wait after networkidle")
    args = parser.parse_args()

    asyncio.run(capture(args.url, Path(args.output), args.width, args.height, args.wait_ms))
    return 0


if __name__ == "__main__":
    sys.exit(main())
