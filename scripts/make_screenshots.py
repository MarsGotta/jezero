"""Write the app screenshots in docs/app/ from the demo data (no server, no key).

    uv run python scripts/make_screenshots.py

Textual renders the real app headless and exports each view as SVG, so the README always
shows what the code draws.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from jezero.ui.app import JezeroApp  # noqa: E402
from jezero.ui.data import DemoBackend  # noqa: E402

OUT = ROOT / "docs" / "app"
SIZE = (150, 44)


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    app = JezeroApp(backend=DemoBackend(), demo=True)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.6)
        app.save_screenshot(str(OUT / "home.svg"))
        await pilot.press("2")
        for key in "retry backoff":
            await pilot.press("space" if key == " " else key)
        await pilot.press("enter")
        await pilot.pause(0.4)
        await pilot.press("l")
        await pilot.pause(0.4)
        app.save_screenshot(str(OUT / "search.svg"))
        await pilot.press("4")
        await pilot.pause(0.4)
        app.save_screenshot(str(OUT / "agents.svg"))
        await pilot.press("5")
        app.query_one("#ctx-project").value = "atlas"  # type: ignore[attr-defined]
        app.query_one("#ctx-prompt").value = "uploads stall, retry backoff"  # type: ignore[attr-defined]
        app.refresh_context()
        await pilot.pause(0.4)
        app.save_screenshot(str(OUT / "agent-view.svg"))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
