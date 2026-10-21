"""Поддельная страница Playwright для тестов send(): элементы задаются списком селекторов."""


class FakeLocator:
    def __init__(self, page: "FakePage", sel: str):
        self.page, self.sel = page, sel

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    def _has(self) -> bool:
        return self.page.has(self.sel)

    async def click(self, timeout=None):
        if not self._has():
            raise TimeoutError(f"нет элемента {self.sel}")
        self.page.clicks.append(self.sel)

    async def wait_for(self, state=None, timeout=None):
        if not self._has():
            raise TimeoutError(f"нет элемента {self.sel}")

    async def count(self) -> int:
        return int(self._has())

    async def is_visible(self, timeout=None) -> bool:
        return self._has()

    async def input_value(self) -> str:
        return ""

    async def fill(self, value: str):
        self.page.typed = value

    async def press_sequentially(self, ch: str):
        self.page.typed += ch


class FakePage:
    def __init__(self, present=(), html: str = "<html></html>", confirm: bool = True, url: str | None = None):
        self.present = set(present)   # селекторы, которые есть на странице (точное совпадение части «a, b»)
        self.html, self.confirm, self.fixed_url = html, confirm, url
        self.url = ""
        self.clicks: list[str] = []
        self.typed = ""

    def has(self, sel: str) -> bool:
        if sel == "__confirm__":
            return self.confirm and bool(self.submitted)
        return any(part.strip() in self.present for part in sel.split(","))

    @property
    def submitted(self) -> bool:
        return any("Senden" in c or "senden" in c for c in self.clicks)

    async def goto(self, url: str):
        self.url = self.fixed_url or url

    async def wait_for_load_state(self, state=None):
        pass

    async def wait_for_timeout(self, ms):
        pass

    async def content(self) -> str:
        return self.html

    def locator(self, sel: str) -> FakeLocator:
        return FakeLocator(self, sel)

    def get_by_text(self, pattern) -> FakeLocator:
        return FakeLocator(self, "__confirm__")


async def no_wait(*a, **kw):
    pass


async def fast_type(locator, text: str):
    await locator.press_sequentially(text)
