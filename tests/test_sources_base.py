import random
from datetime import datetime, timedelta

import pytest

from models import Listing
from sources.base import BlockedError, MissingFieldError, RateLimiter, Source, SourceBroken, poll_interval
from store import Store

NOW = datetime(2026, 10, 21, 12, 0, 0)   # naive UTC; местная полночь (CEST, UTC+2) = 20.10 22:00 UTC


@pytest.fixture
def store():
    return Store(":memory:", clock=lambda: NOW)


def _send(store, n, at, platform="kleinanzeigen"):
    for i in range(n):
        store.set_status(store.add(Listing(platform, f"{at.isoformat()}-{i}", "https://x"), 800), "sent", sent_at=at)


def test_rate_limiter_daily_cap(store):
    _send(store, 15, NOW - timedelta(hours=3))
    assert RateLimiter(store, "kleinanzeigen", 15, 120).wait_seconds(NOW) is None


def test_rate_limiter_cap_counts_from_local_midnight(store):
    _send(store, 14, NOW - timedelta(hours=3))
    _send(store, 5, datetime(2026, 10, 20, 21, 0))   # 23:00 по Берлину накануне — не считается
    rl = RateLimiter(store, "kleinanzeigen", 15, 120)
    assert rl.wait_seconds(NOW) == 0
    _send(store, 1, NOW - timedelta(hours=2))
    assert rl.wait_seconds(NOW) is None


def test_rate_limiter_other_platform_not_counted(store):
    _send(store, 15, NOW - timedelta(hours=3), platform="wggesucht")
    assert RateLimiter(store, "kleinanzeigen", 15, 120).wait_seconds(NOW) == 0


def test_rate_limiter_gap(store):
    _send(store, 1, NOW - timedelta(seconds=60))
    assert RateLimiter(store, "kleinanzeigen", 15, 120).wait_seconds(NOW) == 60.0


def test_rate_limiter_free(store):
    assert RateLimiter(store, "kleinanzeigen", 15, 120).wait_seconds(NOW) == 0


def test_rate_limiter_gap_passed(store):
    _send(store, 1, NOW - timedelta(seconds=300))
    assert RateLimiter(store, "kleinanzeigen", 15, 120).wait_seconds(NOW) == 0


def test_poll_interval_night():
    r = random.Random(1)
    assert 900 <= poll_interval(datetime(2026, 10, 21, 3, 0), (180, 360), (900, 1200), r) <= 1200
    assert 180 <= poll_interval(datetime(2026, 10, 21, 7, 0), (180, 360), (900, 1200), r) <= 360
    assert 900 <= poll_interval(datetime(2026, 10, 21, 0, 0), (180, 360), (900, 1200), r) <= 1200
    assert 900 <= poll_interval(datetime(2026, 10, 21, 6, 59), (180, 360), (900, 1200), r) <= 1200


class FakePage:
    def __init__(self, html, url="https://x/s"):
        self._html, self.url, self.calls = html, url, []

    async def goto(self, url, **kw):
        self.calls.append(("goto", url))
        self.url = url

    async def wait_for_load_state(self, state="load", **kw):
        self.calls.append(("wait", state))

    async def content(self):
        return self._html


class FakeSource(Source):
    name, login_url = "fake", "https://x/login"

    def search_url(self, filters):
        return "https://x/search"

    @staticmethod
    def parse_search(html):
        if "empty" in html:
            raise SourceBroken("нет выдачи")
        return [Listing("fake", "1", "https://x/1")]

    @staticmethod
    def parse_detail(html, l):
        l.title = "detail"
        return l

    @staticmethod
    def detect_block(html, url):
        return "captcha" if "captcha" in html else None

    async def send(self, page, l, text, profile): ...
    async def is_logged_in(self, page): return True


async def test_search_default_parses():
    page = FakePage("<ok>")
    res = await FakeSource().search(page)
    assert [l.ext_id for l in res] == ["1"] and ("goto", "https://x/search") in page.calls


async def test_search_blocked_raises_with_kind():
    with pytest.raises(BlockedError) as e:
        await FakeSource().search(FakePage("captcha"))
    assert e.value.kind == "captcha"


async def test_search_broken_propagates():
    with pytest.raises(SourceBroken):
        await FakeSource().search(FakePage("empty"))


async def test_fetch_detail_default():
    l = Listing("fake", "1", "https://x/1")
    page = FakePage("<ok>")
    assert (await FakeSource().fetch_detail(page, l)).title == "detail"
    assert ("goto", "https://x/1") in page.calls
    with pytest.raises(BlockedError):
        await FakeSource().fetch_detail(FakePage("captcha"), l)


def test_missing_field_error():
    assert MissingFieldError("phone").field == "phone"
