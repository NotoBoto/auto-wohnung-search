import asyncio

import browser as B


class FakePage:
    def __init__(self): self.closed = False
    def is_closed(self): return self.closed


class FakeCtx:
    def __init__(self):
        self.pages, self.handlers, self.dead = [FakePage()], {}, False
    def on(self, ev, cb): self.handlers[ev] = cb
    async def new_page(self):
        if self.dead:
            raise RuntimeError("closed")
        p = FakePage(); self.pages.append(p); return p
    async def close(self): pass


class FakeChromium:
    def __init__(self): self.launches = []
    async def launch_persistent_context(self, udir, **kw):
        await asyncio.sleep(0.01)
        ctx = FakeCtx(); self.launches.append((udir, ctx)); return ctx


class FakePW:
    def __init__(self): self.chromium = FakeChromium()
    async def stop(self): pass


def patch(monkeypatch):
    starts = []

    class Starter:
        async def start(self):
            await asyncio.sleep(0.01)
            pw = FakePW(); starts.append(pw); return pw

    monkeypatch.setattr(B, "async_playwright", lambda: Starter())
    return starts


async def test_concurrent_page_starts_playwright_once(monkeypatch, tmp_path):
    starts = patch(monkeypatch)
    b = B.Browser(tmp_path)
    p1, p2 = await asyncio.gather(b.page("kleinanzeigen"), b.page("wggesucht"))
    assert len(starts) == 1 and p1 is not p2
    assert len(starts[0].chromium.launches) == 2
    assert await b.page("kleinanzeigen") is p1   # тот же контекст, без перезапуска
    await b.close()


async def test_closed_context_relaunched(monkeypatch, tmp_path):
    starts = patch(monkeypatch)
    b = B.Browser(tmp_path)
    p1 = await b.page("kleinanzeigen")
    ctx = starts[0].chromium.launches[0][1]
    ctx.dead = True; p1.closed = True           # окно закрыто пользователем, события close ещё нет
    p2 = await b.page("kleinanzeigen")
    assert p2 is not p1 and len(starts[0].chromium.launches) == 2
    ctx2 = starts[0].chromium.launches[1][1]
    ctx2.handlers["close"]()                     # событие close: следующий page() запускает заново
    await b.page("kleinanzeigen")
    assert len(starts[0].chromium.launches) == 3


async def test_show_login_holds_platform_lock(monkeypatch, tmp_path):
    patch(monkeypatch)
    b = B.Browser(tmp_path)
    async def move(*a): pass
    b._move = move
    pg = await b.page("kleinanzeigen")
    async def goto(url): pass
    async def front(): pass
    pg.goto, pg.bring_to_front = goto, front
    async with b.lock("kleinanzeigen"):
        t = asyncio.create_task(b.show_login("kleinanzeigen", "https://x"))
        await asyncio.sleep(0.05)
        assert not t.done()
    await asyncio.wait_for(t, 1)


async def test_launch_hides_automation_markers(monkeypatch, tmp_path):
    starts = patch(monkeypatch)
    seen = {}

    async def launch(udir, **kw):
        seen.update(kw)
        return FakeCtx()

    b = B.Browser(tmp_path)
    await b._playwright()
    starts[0].chromium.launch_persistent_context = launch
    await b.page("kleinanzeigen")
    assert "--enable-automation" in seen["ignore_default_args"]
    assert "--disable-blink-features=AutomationControlled" in seen["args"]
    assert seen.get("no_viewport") is True and "viewport" not in seen
    await b.close()
