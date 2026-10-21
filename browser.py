import asyncio
import ctypes
import random
import sys
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

OFFSCREEN = {"left": -32000, "top": -32000}
ONSCREEN = {"left": 80, "top": 40}


def set_taskbar(pid: int, visible: bool) -> None:
    """Прячет (или возвращает) кнопки окон процесса pid в панели задач Windows: WS_EX_TOOLWINDOW вместо
    WS_EX_APPWINDOW. Стиль применяется только к скрытому окну, поэтому окно на миг скрывается и показывается
    без активации."""
    if sys.platform != "win32" or not pid:
        return
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x80, 0x40000
    SW_HIDE, SW_SHOWNOACTIVATE, GW_OWNER = 0, 4, 4
    hwnds = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def collect(hwnd, _):
        owner = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and u32.IsWindowVisible(hwnd) and not u32.GetWindow(hwnd, GW_OWNER):
            hwnds.append(hwnd)
        return True

    u32.EnumWindows(collect, 0)
    for hwnd in hwnds:
        ex = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        new = (ex & ~WS_EX_TOOLWINDOW | WS_EX_APPWINDOW) if visible else (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        if new != ex:
            u32.ShowWindow(hwnd, SW_HIDE)
            u32.SetWindowLongW(hwnd, GWL_EXSTYLE, new)
            u32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)


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
        self._shown: set[str] = set()   # площадки, чьё окно сейчас на экране (вход)

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

    async def _pid(self, ctx: BrowserContext) -> int:
        """PID главного процесса msedge этого контекста (через CDP SystemInfo)."""
        try:
            cdp = await ctx.browser.new_browser_cdp_session()
            try:
                info = await cdp.send("SystemInfo.getProcessInfo")
            finally:
                await cdp.detach()
            return next(p["id"] for p in info["processInfo"] if p["type"] == "browser")
        except Exception:
            return 0

    async def _taskbar(self, platform: str, visible: bool) -> None:
        ctx = self._ctx.get(platform)
        if ctx is None:
            return
        try:
            set_taskbar(await self._pid(ctx), visible)
        except Exception:
            pass   # не критично: окно просто останется в панели задач

    async def page(self, platform: str) -> Page:
        """Живая страница площадки; закрытый контекст/страница пересоздаются. Не берёт lock(platform):
        работу с возвращённой страницей вызывающий обязан вести под `async with browser.lock(platform)`."""
        async with self._open_locks.setdefault(platform, asyncio.Lock()):
            for _ in range(2):
                ctx = self._ctx.get(platform)
                if ctx is None:
                    ctx = await self._launch(platform)
                    if platform not in self._shown:
                        await self._taskbar(platform, False)
                try:
                    live = [p for p in ctx.pages if not p.is_closed()]
                    if live:
                        return live[0]
                    page = await ctx.new_page()
                    if platform not in self._shown:
                        await self._taskbar(platform, False)   # новое окно за экраном — без кнопки в панели задач
                    return page
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
            self._shown.add(platform)
            page = await self.page(platform)
            await self._taskbar(platform, True)
            await self._move(platform, ONSCREEN)
            await page.goto(url)
            await page.bring_to_front()

    async def hide(self, platform: str) -> None:
        self._shown.discard(platform)
        await self._move(platform, OFFSCREEN)
        await self._taskbar(platform, False)

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
