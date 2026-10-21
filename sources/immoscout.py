"""ImmoScout24: частичная реализация (ruling R14).
Поиск закрыт AWS WAF («Ich bin kein Roboter») для чистого профиля — разметку выдачи/объявления/формы
снять не удалось. Нужен профиль, где пользователь один раз прошёл проверку; тогда дописать parse_*/send."""
import re

from models import Listing
from sources.base import BlockedError, Source, SourceBroken

BASE = "https://www.immobilienscout24.de"
_BLOCK_RE = re.compile(r"<title>Ich bin kein Roboter|<awswaf-captcha|id=\"captcha-container\"", re.I)
_NORMAL_MARKERS = ("resultListModel", "__NEXT_DATA__", "/expose/")   # маркеры обычной страницы: не проверено вживую


class ImmoScout(Source):
    name = "immoscout"
    login_url = "https://www.immobilienscout24.de/sso/login.go"   # не проверено вживую

    def __init__(self, filters: dict | None = None, contact_email: str = ""):
        super().__init__(filters)
        self.contact_email = contact_email

    def search_url(self, filters: dict) -> str:
        # без фильтров в URL: разметка выдачи не снята, фильтры применяет Engine
        return f"{BASE}/Suche/de/nordrhein-westfalen/bonn/wohnung-mieten?sorting=2"

    @staticmethod
    def parse_search(html: str) -> list[Listing]:
        raise SourceBroken("immoscout: разметка выдачи не снята (нужен профиль с пройденной проверкой)")

    @staticmethod
    def parse_detail(html: str, l: Listing) -> Listing:
        raise SourceBroken("immoscout: разметка объявления не снята")

    @staticmethod
    def detect_block(html: str, url: str) -> str | None:
        if any(m in html for m in _NORMAL_MARKERS):
            return None
        return "captcha" if _BLOCK_RE.search(html) else None

    async def send(self, page, l: Listing, text: str, profile: dict) -> None:
        raise SourceBroken("immoscout: отправка не реализована (нужен вход пользователя)")

    async def is_logged_in(self, page) -> bool:
        if self.detect_block(await page.content(), page.url):
            raise BlockedError("captcha")
        raise SourceBroken("immoscout: проверка входа не реализована")
