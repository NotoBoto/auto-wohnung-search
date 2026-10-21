import random
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from models import Listing
from store import Store

TZ = ZoneInfo("Europe/Berlin")
NIGHT_END_HOUR = 7   # ночь: 00:00–06:59 местного времени


class BlockedError(RuntimeError):
    def __init__(self, kind: Literal["captcha", "logged_out", "blocked"], msg: str = ""):
        super().__init__(msg or kind)
        self.kind = kind


class SourceBroken(RuntimeError):
    """Разметка не распознана: селекторы устарели."""


class MissingFieldError(RuntimeError):
    """Форма требует поле, которого нет в профиле."""
    def __init__(self, field: str):
        super().__init__(field)
        self.field = field


class Source(ABC):
    name: str
    login_url: str

    def __init__(self, filters: dict | None = None):
        self.filters = filters if filters is not None else {}   # фильтры поиска; Engine может менять на экземпляре

    @abstractmethod
    def search_url(self, filters: dict) -> str: ...

    @staticmethod
    @abstractmethod
    def parse_search(html: str) -> list[Listing]:
        """Пустой список без признаков выдачи → SourceBroken."""

    @staticmethod
    @abstractmethod
    def parse_detail(html: str, l: Listing) -> Listing: ...

    @staticmethod
    @abstractmethod
    def detect_block(html: str, url: str) -> str | None:
        """'captcha' | 'logged_out' | 'blocked' | None."""

    async def _load(self, page, url: str) -> str:
        await page.goto(url)
        await page.wait_for_load_state("load")
        html = await page.content()
        kind = self.detect_block(html, page.url)
        if kind:
            raise BlockedError(kind)
        return html

    async def search(self, page) -> list[Listing]:
        return self.parse_search(await self._load(page, self.search_url(self.filters)))

    async def fetch_detail(self, page, l: Listing) -> Listing:
        return self.parse_detail(await self._load(page, l.url), l)

    @abstractmethod
    async def send(self, page, l: Listing, text: str, profile: dict) -> None: ...

    @abstractmethod
    async def is_logged_in(self, page) -> bool: ...


class RateLimiter:
    def __init__(self, store: Store, platform: str, daily_cap: int, min_gap_sec: int):
        self.store, self.platform, self.daily_cap, self.min_gap_sec = store, platform, daily_cap, min_gap_sec

    def wait_seconds(self, now: datetime) -> float | None:
        """now — наивный UTC (как в Store). None — дневной лимит (с местной полуночи Europe/Berlin) исчерпан;
        иначе сколько секунд ждать до следующей отправки (0 — можно сейчас)."""
        local = now.replace(tzinfo=timezone.utc).astimezone(TZ)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        since = midnight.astimezone(timezone.utc).replace(tzinfo=None)
        if self.store.sent_count(self.platform, since) >= self.daily_cap:
            return None
        last = self.store.last_sent_at(self.platform)
        if last is None:
            return 0
        return max(0.0, self.min_gap_sec - (now - last).total_seconds())


def poll_interval(now: datetime, day: tuple[int, int], night: tuple[int, int], rnd=random) -> float:
    """now — МЕСТНОЕ наивное время (Europe/Berlin). Ночью (00:00–06:59) интервал берётся из night, иначе из day."""
    a, b = night if now.hour < NIGHT_END_HOUR else day
    return rnd.uniform(a, b)
