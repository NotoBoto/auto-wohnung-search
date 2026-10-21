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
