import html as _html
import logging
import re

from browser import human_pause, human_type
from models import Listing, parse_area, parse_money, parse_rooms
from sources.base import BlockedError, MissingFieldError, Source, SourceBroken, listing_flags

log = logging.getLogger(__name__)

BASE = "https://www.kleinanzeigen.de"
BONN_LOC = "c203l1038"   # c203 = Mietwohnungen, l1038 = Bonn

_ARTICLE_RE = re.compile(r'<article\b[^>]*\bdata-adid="(\d+)"[^>]*\bdata-href="([^"]+)"[^>]*>(.*?)</article>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_PLZ_RE = re.compile(r"\b(\d{5})\b\s*(?:Bonn\s*-\s*)?(.*)")
_EMPTY_RE = re.compile(r"srchrslt-noresult|keine\s+(?:Anzeigen|Ergebnisse|Treffer)\s+gefunden", re.I)   # маркер пустой выдачи: не проверено вживую
_BLOCK_RE = re.compile(r"geetest|captcha|Zugriff verweigert|Access Denied", re.I)
_NORMAL_MARKERS = ('id="viewad-title"', "srchrslt", "data-adid=")


def _text(s: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", _html.unescape(_TAG_RE.sub("", s)).replace("\xa0", " ")).strip()


def _flags(l: Listing, text: str) -> None:
    """Признаки из заголовка и описания (общий хелпер base.listing_flags)."""
    for k, v in listing_flags(text).items():
        if v and not getattr(l, k):
            setattr(l, k, v)


def _locality(s: str, l: Listing) -> None:
    m = _PLZ_RE.search(s)
    if m:
        l.postcode, l.district = m.group(1), m.group(2).strip()


async def accept_consent(page) -> None:
    """Cookie-баннер: нажать «Alle akzeptieren», если он есть."""
    for sel in ("#gdpr-banner-accept", "button:has-text('Alle akzeptieren')"):
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1500):
                await btn.click()
                await page.wait_for_timeout(800)
                return
        except Exception:
            continue


class Kleinanzeigen(Source):
    name = "kleinanzeigen"
    login_url = f"{BASE}/m-einloggen.html"

    def search_url(self, filters: dict) -> str:
        # фильтр по комнатам в URL не сработал (проверено вживую): min_rooms применяет Engine
        price = f"preis::{int(filters['max_warm'])}/" if filters.get("max_warm") else ""
        return f"{BASE}/s-wohnung-mieten/bonn/{price}{BONN_LOC}?sortingField=SORTING_DATE"

    @staticmethod
    def parse_search(html: str) -> list[Listing]:
        arts = _ARTICLE_RE.findall(html)
        if not arts:
            if "data-adid" not in html and _EMPTY_RE.search(html):   # настоящая пустая выдача
                return []
            raise SourceBroken("kleinanzeigen: выдача не распознана")
        out = []
        for ad_id, href, body in arts:
            url = _html.unescape(href)
            url = url if url.startswith("http") else BASE + url
            h3 = re.search(r"<h3\b.*?</h3>", body, re.S)
            if not h3:
                log.warning("kleinanzeigen: объявление %s без заголовка пропущено", ad_id)
                continue
            l = Listing("kleinanzeigen", ad_id, url, title=_text(h3.group(0)))
            loc = re.search(r"<span>(\d{5}[^<]*)</span>", body)
            if loc:
                _locality(_text(loc.group(1)), l)
            for p in (_text(p) for p in re.findall(r"<p\b[^>]*>(.*?)</p>", body, re.S)):
                if "m²" in p or " Zi." in p:
                    m = re.search(r"([\d.,]+)\s*m²", p)
                    z = re.search(r"([\d.,]+)\s*Zi\.", p)
                    l.area = parse_area(m.group(1)) if m else None
                    l.rooms = parse_rooms(z.group(1)) if z else None
                elif "€" in p and l.cold_rent is None:
                    l.cold_rent = parse_money(p)   # цена в выдаче — Kaltmiete
            _flags(l, l.title)
            out.append(l)
        if not out:
            raise SourceBroken("kleinanzeigen: ни одно объявление выдачи не разобрано")
        return out

    @staticmethod
    def parse_detail(html: str, l: Listing) -> Listing:
        h1 = re.search(r'<h1\b[^>]*id="viewad-title"[^>]*>(.*?)</h1>', html, re.S)
        if not h1:
            raise SourceBroken("kleinanzeigen: объявление не распознано")
        l.title = _text(re.sub(r"<span\b.*?</span>", "", h1.group(1), flags=re.S))
        price = re.search(r'itemprop="price"\s+content="([\d.]+)"', html)
        if price and float(price.group(1)) > 0:
            l.cold_rent = float(price.group(1))
        street = re.search(r'id="street-address"[^>]*>(.*?)</span>', html, re.S)
        if street:
            l.street = _text(street.group(1)).rstrip(",").strip()
        loc = re.search(r'id="viewad-locality"[^>]*>(.*?)</span>', html, re.S)
        if loc:
            _locality(_text(loc.group(1)), l)
        det = re.search(r'id="viewad-details"(.*?)(?=id="viewad-configuration"|id="viewad-map")', html, re.S)
        for label, val in re.findall(r"<li\b[^>]*>\s*([^<]+?)\s*<span\b[^>]*>(.*?)</span>", det.group(1) if det else "", re.S):
            label, val = label.strip(), _text(val)
            if label == "Wohnfläche": l.area = parse_area(val)
            elif label == "Zimmer": l.rooms = parse_rooms(val)
            elif label == "Kaltmiete": l.cold_rent = parse_money(val)
            elif label == "Nebenkosten": l.extra_costs = parse_money(val)
            elif label == "Heizkosten": l.heating_costs = parse_money(val)
            elif label == "Warmmiete": l.warm_rent = parse_money(val)
        desc = re.search(r'id="viewad-description-text"[^>]*>(.*?)(?=<h2\b|<section\b|<aside\b|id="viewad-|\Z)', html, re.S)
        if desc:
            body = re.sub(r"</(?:p|h1|li|div|ul)>|<br\s*/?>", "\n", desc.group(1))
            l.description = re.sub(r"\n\s*\n+", "\n", _text(body).replace(" \n", "\n"))
        seller = re.search(r'id="viewad-contact"(.*?)<hr', html, re.S)
        name = re.search(r'<a\b[^>]*href="/s-bestandsliste[^"]*"[^>]*>\s*([^<]{3,}?)\s*</a>', seller.group(1) if seller else "")
        if name:
            l.contact_name = _text(name.group(1))
        avail = re.search(r"Verfügbar ab:\s*([\d.]+)", l.description)
        if avail:
            l.available_from = avail.group(1)
        _flags(l, f"{l.title}\n{l.description}")
        return l

    @staticmethod
    def detect_block(html: str, url: str) -> str | None:
        if "m-einloggen" in url:
            return "logged_out"
        if not any(m in html for m in _NORMAL_MARKERS):
            if _BLOCK_RE.search(html):
                return "captcha"
            if len(html) < 3000 and re.search(r"\b(403|429)\b", html):
                return "blocked"
        return None

    async def _open(self, page, url: str) -> str:
        await page.goto(url)
        await page.wait_for_load_state("load")
        await accept_consent(page)
        html = await page.content()
        if kind := self.detect_block(html, page.url):
            raise BlockedError(kind)
        return html

    async def search(self, page) -> list[Listing]:
        return self.parse_search(await self._open(page, self.search_url(self.filters)))

    async def fetch_detail(self, page, l: Listing) -> Listing:
        return self.parse_detail(await self._open(page, l.url), l)

    # не проверено вживую — проверить после входа пользователя
    async def is_logged_in(self, page) -> bool:
        """Выход из аккаунта: в шапке есть кнопка «Einloggen» (data-testid=login-button)."""
        if "m-einloggen" in page.url or not page.url.startswith(BASE):
            await page.goto(BASE)
            await page.wait_for_load_state("load")
            await accept_consent(page)
        if kind := self.detect_block(await page.content(), page.url):
            raise BlockedError(kind)
        return await page.locator("[data-testid='login-button']").count() == 0

    # не проверено вживую — проверить после входа пользователя
    async def send(self, page, l: Listing, text: str, profile: dict) -> None:
        await self._open(page, l.url)
        btn = page.locator("#viewad-contact-button-login-modal, #viewad-contact-button, button:has-text('Nachricht schreiben')").first
        msg = page.locator("textarea[name='message'], #viewad-contact-message, textarea").first
        try:
            await btn.click(timeout=8000)
            await msg.wait_for(state="visible", timeout=8000)
        except Exception as e:
            raise SourceBroken("kleinanzeigen: форма сообщения не найдена") from e
        # обязательное поле «Telefon» (если форма его требует), а в профиле его нет
        phone = page.locator("input[name='phoneNumber'], input[type='tel']").first
        if await phone.count() and await phone.is_visible() and not await phone.input_value():
            if not profile.get("phone"):
                raise MissingFieldError("phone")
            await human_type(phone, str(profile["phone"]))
        await msg.click()
        await human_type(msg, text)
        await human_pause()
        await page.locator("button[type='submit']:has-text('Senden'), button:has-text('Nachricht senden')").last.click()
        ok = page.get_by_text(re.compile(r"Nachricht (wurde )?(erfolgreich )?(gesendet|verschickt)|erfolgreich (gesendet|verschickt)", re.I))
        try:
            await ok.first.wait_for(timeout=15000)
        except Exception as e:
            raise SourceBroken("kleinanzeigen: подтверждение отправки не найдено") from e
