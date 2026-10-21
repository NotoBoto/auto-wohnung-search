import asyncio
import random
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

OFFSCREEN = {"left": -32000, "top": -32000}
ONSCREEN = {"left": 80, "top": 40}


async def human_type(locator, text: str) -> None:
    """Печать по символу: 30–120 мс между символами, иногда пауза 300–800 мс."""
    for ch in text:
        await locator.press_sequentially(ch)
        await asyncio.sleep(random.uniform(0.03, 0.12))
        if random.random() < 0.03:
            await asyncio.sleep(random.uniform(0.3, 0.8))


async def human_pause(a: float = 0.8, b: float = 2.5) -> None:
    await asyncio.sleep(random.uniform(a, b))


class Browser:
    """Один постоянный контекст msedge на площадку (профиль base/browser_profiles/<platform>), одна сессия."""

    def __init__(self, base: Path):
        self._root = Path(base) / "browser_profiles"
        self._pw = None
        self._ctx: dict[str, BrowserContext] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, platform: str) -> asyncio.Lock:
        """Блокировка площадки: параллельной работы одной площадки нет."""
        return self._locks.setdefault(platform, asyncio.Lock())

    async def page(self, platform: str) -> Page:
        async with self.lock(f"_open_{platform}"):
            ctx = self._ctx.get(platform)
            if ctx is None:
                if self._pw is None:
                    self._pw = await async_playwright().start()
                udir = self._root / platform
                udir.mkdir(parents=True, exist_ok=True)
                ctx = await self._pw.chromium.launch_persistent_context(
                    str(udir), channel="msedge", headless=False, args=["--window-position=-32000,-32000"],
                    locale="de-DE", viewport={"width": 1366, "height": 850})
                self._ctx[platform] = ctx
            return ctx.pages[0] if ctx.pages else await ctx.new_page()

    async def _move(self, platform: str, pos: dict) -> None:
        ctx = self._ctx.get(platform)
        if ctx is None or not ctx.pages:
            return
        cdp = await ctx.new_cdp_session(ctx.pages[0])
        try:
            win = await cdp.send("Browser.getWindowForTarget")
            await cdp.send("Browser.setWindowBounds", {"windowId": win["windowId"],
                                                       "bounds": {"windowState": "normal", **pos}})
        finally:
            await cdp.detach()

    async def show_login(self, platform: str, url: str) -> None:
        page = await self.page(platform)
        await self._move(platform, ONSCREEN)
        await page.goto(url)
        await page.bring_to_front()

    async def hide(self, platform: str) -> None:
        await self._move(platform, OFFSCREEN)

    async def close(self) -> None:
        for ctx in list(self._ctx.values()):
            try:
                await ctx.close()
            except Exception:
                pass
        self._ctx.clear()
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None
