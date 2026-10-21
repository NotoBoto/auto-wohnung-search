from pathlib import Path

import pytest

from sources.base import SourceBroken
from sources.immoscout import ImmoScout

FIX = Path(__file__).parent / "fixtures" / "immoscout"
URL = "https://www.immobilienscout24.de/Suche/de/nordrhein-westfalen/bonn/wohnung-mieten"


def test_detect_captcha():
    html = (FIX / "captcha.html").read_text(encoding="utf-8")
    assert ImmoScout.detect_block(html, URL) == "captcha"


def test_detect_block_ok_page():
    assert ImmoScout.detect_block("<html><body><h1>Wohnung mieten in Bonn</h1></body></html>", URL) is None


def test_identity_and_search_url():
    s = ImmoScout()
    assert s.name == "immoscout" and "immobilienscout24.de" in s.login_url
    assert "bonn" in s.search_url({}).lower()


def test_parse_search_not_implemented_raises():
    with pytest.raises(SourceBroken):
        ImmoScout.parse_search("<html></html>")


def test_hcaptcha_widget_on_normal_page_is_not_block():
    html = '<html><script src="https://js.hcaptcha.com/1/api.js"></script><div class="h-captcha"></div><a href="/expose/123">x</a></html>'
    assert ImmoScout.detect_block(html, URL) is None
    assert ImmoScout.detect_block('<script src="https://js.hcaptcha.com/1/api.js"></script><form></form>', URL) is None


class _Page:
    url = URL

    def __init__(self, html): self._h = html
    async def content(self): return self._h


async def test_is_logged_in_captcha_raises():
    from sources.base import BlockedError
    with pytest.raises(BlockedError) as e:
        await ImmoScout().is_logged_in(_Page((FIX / "captcha.html").read_text(encoding="utf-8")))
    assert e.value.kind == "captcha"


async def test_parse_detail_and_send_raise_source_broken():
    from models import Listing
    l = Listing("immoscout", "1", "https://www.immobilienscout24.de/expose/1")
    with pytest.raises(SourceBroken):
        ImmoScout.parse_detail("<html></html>", l)
    with pytest.raises(SourceBroken):
        await ImmoScout().send(_Page(""), l, "hi", {})
