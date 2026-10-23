"""ImmoScout24. Разметка выдачи, объявления и формы снята 2026-10-23 из профиля, где пользователь прошёл
проверку AWS WAF и вошёл в аккаунт. Выдача — JSON resultListModel в странице, объявление — поля is24qa-*,
форма — поля с data-testid."""
import html as _html
import json
import logging
import re

from browser import human_pause, human_type
from models import Listing, parse_address, parse_area, parse_money, parse_rooms
from sources.base import BlockedError, MissingFieldError, NotSubmittedError, Source, SourceBroken, listing_flags

log = logging.getLogger(__name__)

BASE = "https://www.immobilienscout24.de"
_BLOCK_RE = re.compile(r"<title>Ich bin kein Roboter|<awswaf-captcha|id=\"captcha-container\"", re.I)
_MODEL_KEY = "resultListModel:"
_TAG_RE = re.compile(r"<[^>]+>")
_SALUTATION = {"Herr": "MALE", "Frau": "FEMALE"}
_NO_PETS_RE = re.compile(r"^\s*(?:keine?|nein|нет|no|-)\s*$", re.I)
# после «Abschicken»: текст подтверждения не проверен вживую
_SENT_RE = re.compile(r"(Nachricht|Anfrage|Kontaktanfrage) (wurde )?(erfolgreich )?(gesendet|versendet|verschickt|übermittelt)|"
                      r"erfolgreich (gesendet|versendet|verschickt|übermittelt)", re.I)


def _text(s: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", _html.unescape(_TAG_RE.sub("", s)).replace("\xa0", " ")).strip()


def _model(html: str) -> dict:
    i = html.find(_MODEL_KEY)
    if i < 0:
        raise SourceBroken("immoscout: выдача не распознана (нет resultListModel)")
    try:
        d, _ = json.JSONDecoder().raw_decode(html[i + len(_MODEL_KEY):].lstrip())
        return d["searchResponseModel"]["resultlist.resultlist"]
    except (ValueError, KeyError, TypeError) as e:
        raise SourceBroken(f"immoscout: выдача не распознана: {e}") from e


def _value(d: dict | None) -> float | None:
    v = (d or {}).get("value")
    return float(v) if isinstance(v, (int, float)) else None


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None


def _flags(l: Listing, text: str) -> None:
    for k, v in listing_flags(text).items():
        if v and not getattr(l, k):
            setattr(l, k, v)


async def accept_consent(page) -> None:
    """Cookie-баннер (Usercentrics): нажать «Alle akzeptieren», если он есть. Не проверено вживую."""
    for sel in ("button[data-testid='uc-accept-all-button']", "button:has-text('Alle akzeptieren')"):
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1500):
                await btn.click()
                await page.wait_for_timeout(800)
                return
        except Exception:
            continue


class ImmoScout(Source):
    name = "immoscout"
    # кнопка «Anmelden» ведёт сюда (проверено вживую)
    login_url = ("https://sso.immobilienscout24.de/sso/login?appName=is24main"
                 "&sso_return=https://www.immobilienscout24.de/sso/login.go?returnUrl%3D/geschlossenerbereich/start.html")

    def search_url(self, filters: dict) -> str:
        # sorting=2 — «Aktualität (neueste zuerst)»; pricetype=calculatedtotalrent — фильтр по Gesamtmiete (проверено вживую)
        price = f"price=-{int(filters['max_warm'])}.0&pricetype=calculatedtotalrent&" if filters.get("max_warm") else ""
        return f"{BASE}/Suche/de/nordrhein-westfalen/bonn/wohnung-mieten?{price}sorting=2"

    @staticmethod
    def parse_search(html: str) -> list[Listing]:
        rl = _model(html)
        groups = rl.get("resultlistEntries") or []
        entries = groups[0].get("resultlistEntry", []) if groups else []
        if isinstance(entries, dict):   # одно объявление API отдаёт объектом, а не списком
            entries = [entries]
        if not entries:
            if (rl.get("paging") or {}).get("numberOfHits") == 0:   # настоящая пустая выдача
                return []
            raise SourceBroken("immoscout: выдача без объявлений")
        out, paywalled = [], 0
        for e in entries:
            x = e.get("resultlist.realEstate") or {}
            ad_id = str(e.get("@id") or x.get("@id") or "")
            title = (x.get("title") or "").strip()
            if not ad_id or not title:
                log.warning("immoscout: объявление без id или заголовка пропущено")
                continue
            # первые дни объявление видно только с MieterPlus (страница оплаты вместо формы): возьмём позже,
            # когда пометка снимется, — в базу не пишем
            if (x.get("paywallListing") or {}).get("active") == "true":
                paywalled += 1
                continue
            l = Listing("immoscout", ad_id, f"{BASE}/expose/{ad_id}", title=title)
            a = x.get("address") or {}
            l.street = " ".join(p for p in (a.get("street"), a.get("houseNumber")) if p)
            l.postcode = a.get("postcode") or ""
            l.district = re.sub(r"^Bonn-", "", a.get("quarter") or "") or a.get("city") or ""
            l.cold_rent = _value(x.get("price"))
            l.warm_rent = _value((x.get("calculatedTotalRent") or {}).get("totalRent"))
            l.rooms = _num(x.get("numberOfRooms"))
            l.area = _num(x.get("livingSpace"))
            c = x.get("contactDetails") or {}
            l.contact_name = " ".join(p for p in (c.get("firstname"), c.get("lastname")) if p) or c.get("company") or ""
            _flags(l, l.title)
            out.append(l)
        if paywalled:
            log.info("immoscout: %s объявлений пока только для MieterPlus — пропущены", paywalled)
        if not out and not paywalled:
            raise SourceBroken("immoscout: ни одно объявление выдачи не разобрано")
        return out

    @staticmethod
    def _dd(html: str, name: str) -> str:
        m = re.search(rf'<dd class="is24qa-{name}\b[^"]*"[^>]*>(.*?)</dd>', html, re.S)
        return _text(re.sub(r'<span class="is24-operator">.*?</span>', "", m.group(1), flags=re.S)) if m else ""

    @staticmethod
    def parse_detail(html: str, l: Listing) -> Listing:
        h1 = re.search(r'<h1\b[^>]*data-qa="expose-title"[^>]*>(.*?)</h1>', html, re.S)
        if not h1 or not _text(h1.group(1)):
            raise SourceBroken("immoscout: объявление не распознано")
        l.title = _text(h1.group(1))
        dd = ImmoScout._dd
        if v := parse_money(dd(html, "kaltmiete")): l.cold_rent = v
        if v := parse_money(dd(html, "nebenkosten")): l.extra_costs = v
        heat = dd(html, "heizkosten")
        if "enthalten" in heat:
            l.heating_costs = 0.0   # «in Nebenkosten enthalten»: Kalt + NK = Gesamtmiete
        elif v := parse_money(heat):
            l.heating_costs = v
        if v := parse_money(dd(html, "gesamtmiete")): l.warm_rent = v
        if v := parse_area(dd(html, "wohnflaeche-ca")): l.area = v
        if v := parse_rooms(dd(html, "zimmer")): l.rooms = v
        l.available_from = dd(html, "bezugsfrei-ab") or l.available_from
        zipcode = re.search(r'"obj_zipCode":"(\d{5})"', html)
        if zipcode and not l.postcode:
            l.postcode = zipcode.group(1)
        parts = []
        for head, body in re.findall(r'<span class="expose-description-h3">(.*?)</span>\s*</h3>\s*'
                                     r'<span class="expose-description-body">(.*?)</span>', html, re.S):
            t = re.sub(r"\n\s*\n+", "\n", _text(re.sub(r"<br\s*/?>", "\n", body)))
            if t:
                parts.append(f"{_text(head)}:\n{t}")
        loc = re.search(r'<span class="expose-location-description">(.*?)</span>', html, re.S)
        if loc and (t := re.sub(r"\n\s*\n+", "\n", _text(re.sub(r"<br\s*/?>", "\n", loc.group(1))))):
            parts.append(f"Lage:\n{t}")
        l.description = "\n".join(parts)
        _flags(l, f"{l.title}\n{l.description}")
        return l

    @staticmethod
    def detect_block(html: str, url: str) -> str | None:
        if "sso.immobilienscout24.de" in url or "login.immobilienscout24.de" in url:
            return "logged_out"
        return "captcha" if _BLOCK_RE.search(html) else None

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

    async def is_logged_in(self, page) -> bool:
        """В странице выдачи есть "loggedIn":true (проверено вживую на аккаунте с входом)."""
        if not page.url.startswith(BASE):
            await page.goto(self.search_url({}))
            await page.wait_for_load_state("load")
            await accept_consent(page)
        html = await page.content()
        if kind := self.detect_block(html, page.url):
            if kind != "logged_out":
                raise BlockedError(kind)
            return False
        return bool(re.search(r'"loggedIn":\s*true|userLoggedIn:\s*true', html))

    async def send(self, page, l: Listing, text: str, profile: dict) -> None:
        try:
            await self._fill_form(page, l, text, profile)
        except (BlockedError, MissingFieldError, NotSubmittedError):
            raise
        except Exception as e:   # до нажатия «Abschicken» письмо точно не ушло
            raise NotSubmittedError(f"immoscout: форма не заполнена: {e}") from e
        await page.locator("form[data-testid='contact-form'] button[type='submit']:has-text('Abschicken')").last.click()
        try:
            await page.get_by_text(_SENT_RE).first.wait_for(timeout=20000)
        except Exception as e:   # после нажатия исход неизвестен — не SourceBroken/NotSubmittedError
            raise RuntimeError("immoscout: подтверждение отправки не найдено") from e

    async def _field(self, page, testid: str, value) -> None:
        """Текстовое поле формы: печатаем, только если площадка не подставила значение сама."""
        loc = page.locator(f"form[data-testid='contact-form'] input[data-testid='{testid}']").first
        if value in (None, "") or not await loc.count() or await loc.input_value():
            return
        await loc.click()
        await human_type(loc, str(value))

    async def _fill_form(self, page, l: Listing, text: str, profile: dict) -> None:
        """Всё до нажатия «Abschicken»."""
        html = await self._open(page, l.url)
        if re.search(r"userLoggedIn:\s*false", html):
            raise BlockedError("logged_out", "immoscout: вход в аккаунт не выполнен")
        msg = page.locator("form[data-testid='contact-form'] textarea[data-testid='message']").first
        try:
            await page.locator("button[data-testid='contact-button']").first.click(timeout=8000)
            await msg.wait_for(state="visible", timeout=8000)
        except Exception as e:
            raise NotSubmittedError("immoscout: форма сообщения не найдена") from e
        # обязательные поля формы (Straße, Hausnummer, PLZ, Ort) — из profile.address, если площадка их не подставила
        addr = parse_address(profile.get("address") or "")
        for testid in ("street", "houseNumber", "postcode", "city"):
            loc = page.locator(f"form[data-testid='contact-form'] input[data-testid='{testid}']").first
            if await loc.count() and not await loc.input_value() and not addr:
                raise MissingFieldError("address")
        if sal := _SALUTATION.get(profile.get("salutation") or ""):
            sel = page.locator("form[data-testid='contact-form'] select[data-testid='salutation']").first
            if await sel.count() and not await sel.input_value():
                await sel.select_option(sal)
        await self._field(page, "firstName", profile.get("first_name"))
        await self._field(page, "lastName", profile.get("last_name"))
        await self._field(page, "phoneNumber", profile.get("phone"))
        if addr:
            for testid, v in zip(("street", "houseNumber", "postcode", "city"), addr):
                await self._field(page, testid, v)
        if profile.get("persons"):   # площадка ставит «1» по умолчанию — заменяем своим числом
            adults = page.locator("form[data-testid='contact-form'] input[data-testid='numberOfAdults']").first
            if await adults.count():
                await adults.fill(str(profile["persons"]))
        pets = profile.get("pets")
        if pets:
            sel = page.locator("form[data-testid='contact-form'] select[data-testid='hasPets']").first
            if await sel.count() and not await sel.input_value():
                await sel.select_option("FALSE" if _NO_PETS_RE.match(str(pets)) else "TRUE")
        await self._field(page, "incomeAmount", profile.get("net_income"))
        await msg.click()
        await msg.fill("")   # убрать текст, подставленный площадкой
        await human_type(msg, text)
        await human_pause()
