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
        self._pw_lock = asyncio.Lock()   # запуск драйвера Playwright — один на всех
        self._ctx: dict[str, BrowserContext] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._open_locks: dict[str, asyncio.Lock] = {}

    def lock(self, platform: str) -> asyncio.Lock:
        """Рабочая блокировка площадки. Вызывающий (Engine) обязан держать её на всё время search/fetch/send:
        параллельной работы одной площадки нет. page() её не берёт, show_login() берёт сам."""
        return self._locks.setdefault(platform, asyncio.Lock())

    async def _playwright(self):
        async with self._pw_lock:
            if self._pw is None:
                self._pw = await async_playwright().start()
            return self._pw

    async def _launch(self, platform: str) -> BrowserContext:
        pw = await self._playwright()
        udir = self._root / platform
        udir.mkdir(parents=True, exist_ok=True)
        ctx = await pw.chromium.launch_persistent_context(
            str(udir), channel="msedge", headless=False, args=["--window-position=-32000,-32000"],
            locale="de-DE", viewport={"width": 1366, "height": 850})
        # пользователь закрыл окно Edge: контекст мёртв, следующий page() запустит его заново
        ctx.on("close", lambda *_: self._ctx.get(platform) is ctx and self._ctx.pop(platform, None))
        self._ctx[platform] = ctx
        return ctx

    async def page(self, platform: str) -> Page:
        """Живая страница площадки; закрытый контекст/страница пересоздаются. Не берёт lock(platform):
        работу с возвращённой страницей вызывающий обязан вести под `async with browser.lock(platform)`."""
        async with self._open_locks.setdefault(platform, asyncio.Lock()):
            for _ in range(2):
                ctx = self._ctx.get(platform) or await self._launch(platform)
                try:
                    live = [p for p in ctx.pages if not p.is_closed()]
                    return live[0] if live else await ctx.new_page()
                except Exception:   # контекст закрыт вместе с окном
                    if self._ctx.get(platform) is ctx:
                        del self._ctx[platform]
            raise RuntimeError(f"не удалось открыть браузер для {platform}")

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
        async with self.lock(platform):
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
        async with self._pw_lock:
            if self._pw is not None:
                await self._pw.stop()
                self._pw = None
