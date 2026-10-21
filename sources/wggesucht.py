import html as _html
import logging
import re
from datetime import datetime

from browser import human_pause, human_type
from models import Listing, parse_area, parse_money, parse_rooms
from sources.base import (AlreadyContactedError, BlockedError, MissingFieldError, NotSubmittedError, Source,
                          SourceBroken, listing_flags)

log = logging.getLogger(__name__)

BASE = "https://www.wg-gesucht.de"
BONN_ID = 13      # город Бонн (проверено вживую); категория 2 = «Wohnungen» (не WG-Zimmer 0 и не 1-Zimmer-Wohnungen 1)

_CARD_RE = re.compile(r'(<a\b[^>]*\bclass="wgg-list-card offer_list_item[^"]*"[^>]*>)(.*?)</a>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")
_EMPTY_RE = re.compile(r"keine\s+(?:Anzeigen|Angebote|Ergebnisse|Treffer)\s+gefunden", re.I)   # маркер пустой выдачи: не проверено вживую
# (?<!re)captcha: невидимая reCAPTCHA (google.com/recaptcha, recaptcha.net) есть на обычных страницах сайта
_BLOCK_RE = re.compile(r"(?<!re)captcha|Zugriff verweigert|Access Denied|not a robot|Just a moment|cf-chl|"
                       r"Checking your browser", re.I)
_CONV_URL_RE = re.compile(r"nachricht\.html\?nachrichten-id=\d+")
_HARMLESS_RE = re.compile(r'data-input="captcha"', re.I)   # поле формы входа/регистрации — не проверка
# только признаки конкретных страниц (выдача, объявление, форма сообщения): main_column есть на любой странице сайта
_NORMAL_MARKERS = ("offer_list_item", "detailed-view-title", 'id="message_input"')


def _text(s: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", _html.unescape(_TAG_RE.sub("", s)).replace("\xa0", " ")).strip()


def months_between(start: str, end: str) -> int | None:
    """Полных месяцев между датами дд.мм.гггг (округление вниз); None, если даты неверны или конец раньше начала."""
    try:
        a, b = (datetime.strptime(s.strip(), "%d.%m.%Y") for s in (start, end))
    except ValueError:
        return None
    if b < a:
        return None
    return (b.year - a.year) * 12 + (b.month - a.month) - (1 if b.day < a.day else 0)


def _div_inner(html: str, pos: int) -> str:
    """Содержимое <div>, открывающий тег которого закончился на pos (с учётом вложенных div)."""
    depth = 1
    for t in re.finditer(r"<div\b|</div>", html[pos:]):
        depth += 1 if t.group(0) != "</div>" else -1
        if depth == 0:
            return html[pos:pos + t.start()]
    return html[pos:]


def _flags(l: Listing, text: str) -> None:
    """Признаки из заголовка и описания (общий хелпер base.listing_flags); явные даты «frei bis» уже в limited_months и важнее текста."""
    for k, v in listing_flags(text).items():
        if v and not getattr(l, k):
            setattr(l, k, v)


def _locality(s: str, l: Listing) -> None:
    """«53113 Bonn Südstadt» или «Bonn Südstadt» → индекс и район."""
    m = re.match(r"(?:(\d{5})\s+)?(?:Bonn\s*)?(.*)", s.strip())
    if m:
        l.postcode = m.group(1) or l.postcode
        l.district = m.group(2).strip() or ("Bonn" if "Bonn" in s else "")


async def accept_consent(page) -> None:
    """Cookie-баннер (consentmanager): нажать «Alle akzeptieren», если он есть."""
    for sel in ("a.cmpboxbtnyes", "button:has-text('Alle akzeptieren')"):
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1500):
                await btn.click()
                await page.wait_for_timeout(800)
                return
        except Exception:
            continue


class WgGesucht(Source):
    name = "wggesucht"
    login_url = f"{BASE}/?modal=sign_in"

    def search_url(self, filters: dict) -> str:
        # комнаты в URL не фильтруем: min_rooms применяет Engine; sort_column=0 — по дате, новые сверху
        price = f"&rMax={int(filters['max_warm'])}" if filters.get("max_warm") else ""   # цена на площадке — Gesamtmiete (warm)
        return (f"{BASE}/wohnungen-in-Bonn.{BONN_ID}.2.1.0.html?offer_filter=1&city_id={BONN_ID}"
                f"&categories%5B0%5D=2&sort_column=0&sort_order=0&noDeact=1{price}")

    @staticmethod
    def parse_search(html: str) -> list[Listing]:
        cards = _CARD_RE.findall(html)
        if not cards:
            if "offer_list_item" not in html and _EMPTY_RE.search(html):   # настоящая пустая выдача
                return []
            raise SourceBroken("wggesucht: выдача не распознана")
        out = []
        for tag, body in cards:
            ad_id = re.search(r'data-id="(\d+)"', tag)
            href = re.search(r'href="([^"]+)"', tag)
            if not ad_id or not href:
                log.warning("wggesucht: карточка без id или ссылки пропущена")
                continue
            url = _html.unescape(href.group(1))
            url = url if url.startswith("http") else BASE + url
            title = re.search(r'class="[^"]*\bad-title\b[^"]*"[^>]*>(.*?)</span>', body, re.S)
            if not title or not _text(title.group(1)):
                log.warning("wggesucht: объявление %s без заголовка пропущено", ad_id.group(1))
                continue
            l = Listing("wggesucht", ad_id.group(1), url, title=_text(title.group(1)))
            price = re.search(r'price-row.*?card-main-text">\s*([^<]*€[^<]*)<', body, re.S)
            if price:
                l.warm_rent = parse_money(_text(price.group(1)))   # цена в выдаче — Gesamtmiete
            sub = re.search(r'class="card-subtitle ad-subtitle[^"]*"\s+title="([^"]*)"', body)
            parts = [p.strip() for p in _html.unescape(sub.group(1)).replace("\xa0", " ").split("·")] if sub else []
            if parts:
                z = re.match(r"([\d.,]+)-Zimmer", parts[0])
                l.rooms = parse_rooms(z.group(1)) if z else None
            if len(parts) > 1:
                _locality(parts[1], l)
            if len(parts) > 2:
                l.street = parts[2]
            size = re.search(r'size-value">\s*([^<]*m²)', body)
            if size:
                l.area = parse_area(size.group(1))
            row = re.search(r'middle-row">(.*?)<div class="text-right', body, re.S)
            dates = _DATE_RE.findall(_text(row.group(1))) if row else []
            if dates:
                l.available_from = dates[0]
                if len(dates) > 1:
                    l.limited_months = months_between(dates[0], dates[1])
            name = re.search(r'class="card-subtitle user-name">([^<]*)<', body)
            if name:
                l.contact_name = _text(name.group(1))
            _flags(l, l.title)
            out.append(l)
        if not out:
            raise SourceBroken("wggesucht: ни одно объявление выдачи не разобрано")
        return out

    @staticmethod
    def _val(html: str, label: str) -> str:
        """Значение пары «label: значение» в панелях объявления (не выходя за следующую подпись)."""
        m = re.search(rf'section_panel_detail">\s*{label}\s*:?\s*</span>(?:(?!section_panel_detail).)*?section_panel_value[^>]*>(.*?)</span>', html, re.S)
        return _text(m.group(1)) if m else ""

    @staticmethod
    def parse_detail(html: str, l: Listing) -> Listing:
        h1 = re.search(r'<h1\b[^>]*detailed-view-title[^>]*>(.*?)</h1>', html, re.S)
        if not h1 or not _text(h1.group(1)):
            raise SourceBroken("wggesucht: объявление не распознано")
        l.title = _text(h1.group(1))
        facts = {_text(k): _text(v) for k, v in re.findall(r'key_fact_detail">(.*?)</span>.*?key_fact_value">(.*?)</b>', html, re.S)}
        if "Größe" in facts: l.area = parse_area(facts["Größe"])
        if "Zimmer" in facts: l.rooms = parse_rooms(facts["Zimmer"])
        if "Gesamtmiete" in facts: l.warm_rent = parse_money(facts["Gesamtmiete"])
        val = WgGesucht._val
        if v := parse_money(val(html, "Miete")): l.cold_rent = v
        if v := parse_money(val(html, "Nebenkosten")): l.extra_costs = v
        # «Sonstige Kosten» входят в Gesamtmiete; кладём в heating_costs, чтобы Kalt + NK + heating = Warm
        if v := parse_money(val(html, "Sonstige Kosten")): l.heating_costs = v
        adr = re.search(r'Adresse\s*</h2>.*?section_panel_detail">(.*?)</span>', html, re.S)
        if adr:
            lines = [_text(x) for x in re.split(r"<br\s*/?>", adr.group(1)) if _text(x)]
            if lines:
                _locality(lines[-1], l)
                if len(lines) > 1:
                    l.street = lines[0]
        l.available_from = val(html, "frei ab") or l.available_from
        end = val(html, "frei bis")
        if end and l.available_from:
            l.limited_months = months_between(l.available_from, end)
        paras = []
        for blk in (_div_inner(html, m.end()) for m in re.finditer(r'<div id="freitext_\d+"[^>]*>', html)):
            for p in re.findall(r"<p\b[^>]*>(.*?)</p>", blk, re.S):
                t = re.sub(r"[ \t]*\n[ \t]*", " ", re.sub(r"<br\s*/?>", "\n", p))
                t = re.sub(r"\n\s*\n+", "\n", _text(t).replace(" \n", "\n"))
                if t:
                    paras.append(t)
        l.description = "\n".join(paras)
        _flags(l, f"{l.title}\n{l.description}")
        return l

    @staticmethod
    def detect_block(html: str, url: str) -> str | None:
        if "modal=sign_in" in url:
            return "logged_out"
        if not any(m in html for m in _NORMAL_MARKERS):
            if _BLOCK_RE.search(_HARMLESS_RE.sub("", html)):
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
        """Выход из аккаунта: на странице есть форма входа #login_form (модальное окно «Anmelden»)."""
        if not page.url.startswith(BASE):
            await page.goto(BASE)
            await page.wait_for_load_state("load")
            await accept_consent(page)
        if kind := self.detect_block(await page.content(), page.url):
            if kind != "logged_out":
                raise BlockedError(kind)
            return False
        return await page.locator("#login_form").count() == 0

    @staticmethod
    def is_conversation(url: str, html: str) -> bool:
        """Страница переписки: после отправки (и при повторном открытии формы) сайт ведёт сюда (проверено вживую)."""
        return "nachrichten-id=" in url or "Unterhaltung mit" in html

    async def send(self, page, l: Listing, text: str, profile: dict) -> None:
        try:
            await self._fill_form(page, l, text)
        except (AlreadyContactedError, BlockedError, MissingFieldError, NotSubmittedError):
            raise
        except Exception as e:   # до нажатия «Отправить» письмо точно не ушло
            raise NotSubmittedError(f"wggesucht: форма не заполнена: {e}") from e
        # кнопка отправки не проверена вживую; успех — переход в переписку nachricht.html?nachrichten-id=…
        await page.locator("button:has-text('Nachricht senden'), button[type='submit']:has-text('Senden')").last.click()
        try:
            await page.wait_for_url(_CONV_URL_RE, timeout=20000)
        except Exception as e:
            try:
                if self.is_conversation(page.url, await page.content()):
                    return
            except Exception:
                pass
            # после нажатия исход неизвестен — не SourceBroken/NotSubmittedError
            raise RuntimeError("wggesucht: подтверждение отправки не найдено") from e

    async def _fill_form(self, page, l: Listing, text: str) -> None:
        """Всё до нажатия «Отправить»."""
        # форма сообщения — отдельная страница /nachricht-senden/<slug>.<id>.html
        html = await self._open(page, l.url.replace(f"{BASE}/", f"{BASE}/nachricht-senden/", 1))
        if self.is_conversation(page.url, html):
            # переписка уже есть: в поле чата не печатаем, иначе ушло бы второе письмо
            raise AlreadyContactedError("wggesucht: переписка с арендодателем уже есть")
        msg = page.locator("textarea#message_input, textarea[name='content'], textarea").first
        try:
            await msg.wait_for(state="visible", timeout=8000)
        except Exception as e:
            raise NotSubmittedError("wggesucht: форма сообщения не найдена") from e
        # площадка может открыть окно с шаблоном/подтверждением: закрыть его и вставить свой текст
        for sel in (".modal.in button.close", ".modal.in [data-dismiss='modal']", "button:has-text('Vorlage ignorieren')"):
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1000):
                    await btn.click()
                    await page.wait_for_timeout(500)
            except Exception:
                continue
        await msg.click()
        await msg.fill("")   # убрать шаблон, подставленный площадкой
        await human_type(msg, text)
        await human_pause()
