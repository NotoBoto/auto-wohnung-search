from pathlib import Path

import pytest

from models import Listing
from sources.base import BlockedError, ListingGoneError, MissingFieldError, SourceBroken
from sources.kleinanzeigen import Kleinanzeigen

FIX = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def detail(html: str | None = None, **kw) -> Listing:
    return Kleinanzeigen.parse_detail(html if html is not None else read("kleinanzeigen/detail.html"),
                                      Listing("kleinanzeigen", "3494091921", "https://www.kleinanzeigen.de/s-anzeige/x/3494091921-203-23690", **kw))


def test_parse_search():
    items = Kleinanzeigen.parse_search(read("kleinanzeigen/search.html"))
    assert len(items) >= 10
    assert all(i.platform == "kleinanzeigen" and i.ext_id and i.url.startswith("https://www.kleinanzeigen.de/s-anzeige/") for i in items)
    assert any(i.cold_rent or i.warm_rent for i in items)
    assert all(i.ext_id in i.url for i in items)


def test_parse_search_first_item():
    first = Kleinanzeigen.parse_search(read("kleinanzeigen/search.html"))[0]
    assert (first.ext_id, first.cold_rent, first.area, first.rooms, first.postcode) == ("3546903271", 708.0, 74.32, 3.0, "53175")
    assert first.warm_rent is None and first.title.startswith("46208/129 Wohnen mit Ausblick")


def test_parse_search_flags_swap():
    items = Kleinanzeigen.parse_search(read("kleinanzeigen/search.html"))
    assert any(i.is_swap for i in items) and not all(i.is_swap for i in items)


def test_parse_detail():
    l = detail()
    assert (l.rooms, l.area, l.warm_rent or l.cold_rent, l.postcode) == (3.0, 73.3, 1321.0, "53119")
    assert (l.cold_rent, l.extra_costs, l.heating_costs) == (854.0, 318.0, 149.0)
    assert (l.street, l.district, l.available_from) == ("Masurenweg 1", "Tannenbusch", "06.01.27")
    assert l.title == "Platz für Familien: Sanierte 3-Zimmer-Wohnung im Erdgeschoss"
    assert l.contact_name == "Max Mustermann"
    assert l.description.startswith("Die gut geschnittene 3-Zimmer Wohnung")
    assert not (l.is_shared_flat or l.is_swap or l.wbs_required or l.furnished_temporary) and l.limited_months is None


def test_parse_detail_missing_title_raises():
    with pytest.raises(SourceBroken):
        detail("<html><body>nichts</body></html>")


def _with_desc(desc: str, title: str = "Wohnung") -> str:
    return (f'<h1 id="viewad-title" itemprop="name">{title}</h1><h2 id="viewad-price">500 €</h2>'
            f'<p id="viewad-description-text" itemprop="description">{desc}</p>')


@pytest.mark.parametrize("desc,field,value", [
    ("Zimmer in netter WG, Mitbewohner gesucht", "is_shared_flat", True),
    ("Tausche gegen größere Wohnung", "is_swap", True),
    ("Zwischenmiete, befristet auf 6 Monate", "limited_months", 6),
    ("Wohnen auf Zeit, möbliert auf Zeit", "furnished_temporary", True),
    ("Nur mit WBS", "wbs_required", True),
])
def test_detail_flags(desc, field, value):
    assert getattr(detail(_with_desc(desc)), field) == value


def test_parse_search_garbage_raises():
    with pytest.raises(SourceBroken):
        Kleinanzeigen.parse_search("<html><body>Wartung</body></html>")


def test_detect_block_login():
    assert Kleinanzeigen.detect_block("", "https://www.kleinanzeigen.de/m-einloggen.html") == "logged_out"


def test_detect_block_captcha_and_ok():
    assert Kleinanzeigen.detect_block("<html><body>Geetest captcha</body></html>", "https://www.kleinanzeigen.de/s-x") == "captcha"
    assert Kleinanzeigen.detect_block(read("kleinanzeigen/search.html"), "https://www.kleinanzeigen.de/s-x") is None
    assert Kleinanzeigen.detect_block(read("kleinanzeigen/detail.html"), "https://www.kleinanzeigen.de/s-anzeige/x") is None


def test_detect_block_ip_range_ban():
    page = ("<html><head><title>IP-Bereich gesperrt bei Kleinanzeigen</title></head><body>"
            "<h1>IP-Bereich vorübergehend gesperrt.</h1>"
            "<p>In deinem IP-Bereich kam es vor Kurzem mehrfach zu unsicheren Versuchen, unsere Plattform zu verwenden.</p>"
            "<a href='https://themen.kleinanzeigen.de/ip-eingeschraenkt/'>Mehr Informationen</a>"
            "<p>Ref#: 0.d96656b8 IP#: 0.0.0.0</p>" + " " * 4000 + "</body></html>")
    assert Kleinanzeigen.detect_block(page, "https://www.kleinanzeigen.de/s-x") == "blocked"


def test_page_url_like_site_link():
    u = Kleinanzeigen().page_url({"max_warm": 900}, 2)
    assert u == "https://www.kleinanzeigen.de/s-wohnung-mieten/bonn/sortierung:neuste/preis::900/seite:2/c203l1038"
    assert "swap" not in u


def test_search_url():
    u = Kleinanzeigen().search_url({"max_warm": 900, "min_rooms": 2})
    assert u.startswith("https://www.kleinanzeigen.de/s-wohnung-mieten/bonn/") and "preis::900" in u and "c203l1038" in u
    assert "sortingField=SORTING_DATE" in u
    assert "preis" not in Kleinanzeigen().search_url({})


def test_parse_search_empty_marker_ok():
    assert Kleinanzeigen.parse_search('<div id="srchrslt"><p>Leider keine Anzeigen gefunden</p></div>') == []


def test_parse_search_srchrslt_without_articles_raises():
    with pytest.raises(SourceBroken):
        Kleinanzeigen.parse_search('<div id="srchrslt-adtable"><div data-adid="1"></div></div>')


def test_parse_search_no_title_all_raises():
    html = '<article data-adid="1" data-href="/s-anzeige/x/1-203-1"><p>x</p></article>'
    with pytest.raises(SourceBroken):
        Kleinanzeigen.parse_search(html)


def test_parse_search_no_title_skipped_with_warning(caplog):
    good = read("kleinanzeigen/search.html")
    bad = '<article data-adid="1" data-href="/s-anzeige/x/1-203-1"><p>x</p></article>'
    items = Kleinanzeigen.parse_search(bad + good)
    assert len(items) >= 10 and "без заголовка" in caplog.text


def test_negated_wbs_in_detail():
    assert not detail(_with_desc("Kein WBS erforderlich")).wbs_required


async def test_is_logged_in_captcha_raises():
    class P:
        url = "https://www.kleinanzeigen.de/"
        async def content(self): return "<html>Geetest captcha</html>"
    with pytest.raises(BlockedError):
        await Kleinanzeigen().is_logged_in(P())


# ---------- send (поддельная страница) ----------

from fake_page import FakePage, fast_type, no_wait   # noqa: E402
from sources.base import NotSubmittedError   # noqa: E402


def test_login_url_keeps_existing_session():
    # m-einloggen.html у вошедшего пользователя сбрасывает access/refresh_token (проверено вживую)
    assert "m-einloggen" not in Kleinanzeigen.login_url


async def test_is_logged_in_by_user_marker_without_full_load():
    page = FakePage(("[data-testid='logged-in-user']",))   # шапка «angemeldet als …» (проверено вживую)
    assert await Kleinanzeigen().is_logged_in(page)
    assert page.wait_until == "domcontentloaded"   # «load» главной ждёт рекламу дольше 30 с


async def test_is_logged_in_false_without_user_marker():
    assert not await Kleinanzeigen().is_logged_in(FakePage())

KA_FORM = ("#viewad-contact-button", "textarea[name='message']", "button[type='submit']:has-text('Senden')")
AD = Listing("kleinanzeigen", "1", "https://www.kleinanzeigen.de/s-anzeige/x/1-203-1")


@pytest.fixture
def fast(monkeypatch):
    import sources.kleinanzeigen as ka
    monkeypatch.setattr(ka, "human_type", fast_type)
    monkeypatch.setattr(ka, "human_pause", no_wait)


async def test_send_ok(fast):
    page = FakePage(KA_FORM)
    await Kleinanzeigen().send(page, AD, "Hallo", {})
    assert page.typed == "Hallo" and page.submitted


async def test_send_form_missing_is_not_submitted(fast):
    page = FakePage(("#viewad-contact-button",))   # кнопка есть, поля сообщения нет
    with pytest.raises(NotSubmittedError):
        await Kleinanzeigen().send(page, AD, "Hallo", {})
    assert not page.submitted


async def test_send_unconfirmed_after_click_is_not_not_submitted(fast):
    page = FakePage(KA_FORM, confirm=False)
    with pytest.raises(RuntimeError) as e:
        await Kleinanzeigen().send(page, AD, "Hallo", {})
    assert not isinstance(e.value, (NotSubmittedError, SourceBroken)) and page.submitted


async def test_send_logged_out_blocks_before_click(fast):
    page = FakePage(KA_FORM + ("[data-testid='login-button']",))   # шапка с «Einloggen» — сессия вышла
    with pytest.raises(BlockedError) as e:
        await Kleinanzeigen().send(page, AD, "Hallo", {})
    assert e.value.kind == "logged_out" and page.clicks == []


async def test_send_ignores_unrelated_textarea(fast):
    page = FakePage(("#viewad-contact-button", "textarea"))   # чужое поле (например, поиск) — не форма сообщения
    with pytest.raises(NotSubmittedError):
        await Kleinanzeigen().send(page, AD, "Hallo", {})
    assert page.typed == "" and not page.submitted


class SlowAdsPage(FakePage):
    """Реклама держит событие «load» дольше таймаута."""
    async def wait_for_load_state(self, state=None, **kw):
        if state == "load":
            raise TimeoutError("Timeout 30000ms exceeded")


async def test_send_does_not_wait_for_ads_load(fast):
    page = SlowAdsPage(KA_FORM)
    await Kleinanzeigen().send(page, AD, "Hallo", {})
    assert page.wait_until == "domcontentloaded" and page.typed == "Hallo" and page.submitted


async def test_send_listing_gone_redirects_to_search(fast):
    # снятое объявление отдаёт 301 на выдачу категории (вживую 2026-10-26)
    page = FakePage(KA_FORM, url="https://www.kleinanzeigen.de/s-wohnung-mieten/weststadt/c203l1042")
    with pytest.raises(ListingGoneError):
        await Kleinanzeigen().send(page, AD, "Hallo", {})
    assert not page.submitted


# форма объявлений Immobilien 2026-10-26: окно <dialog>, поля без name — по подписи, списки — combobox/option
DLG = "dialog[open]"
KA_DIALOG = ("#viewad-contact-button", DLG, f"{DLG} textarea", f"{DLG} >> textarea",
             *(f"{DLG} >> label={t}" for t in ("Vorname", "Nachname", "Straße/Nr.", "PLZ", "Telefonnummer")),
             f"{DLG} >> role=combobox[Anrede]", f"{DLG} >> role=combobox[Schufa]",
             "role=option[Herr]", "role=option[Frau]", "role=option[Keine Angabe]",
             f"{DLG} >> role=button[Nachricht senden]")
KA_PROFILE = {"salutation": "Herr", "first_name": "Max", "last_name": "Muster", "phone": "0170 123",
              "address": "Musterstraße 5a, 53111 Bonn"}


async def test_send_fills_dialog_form(fast):
    page = FakePage(KA_DIALOG)
    await Kleinanzeigen().send(page, AD, "Hallo", KA_PROFILE)
    f = page.filled
    assert f[f"{DLG} >> textarea"] == "Hallo"
    assert f[f"{DLG} >> label=Vorname"] == "Max" and f[f"{DLG} >> label=Nachname"] == "Muster"
    assert f[f"{DLG} >> label=Straße/Nr."] == "Musterstraße 5a" and f[f"{DLG} >> label=PLZ"] == "53111"
    assert f[f"{DLG} >> label=Telefonnummer"] == "0170 123"
    # Anrede из профиля, Schufa в профиле нет — «Keine Angabe»
    assert page.clicks.index(f"{DLG} >> role=combobox[Anrede]") < page.clicks.index("role=option[Herr]")
    assert page.clicks.index(f"{DLG} >> role=combobox[Schufa]") < page.clicks.index("role=option[Keine Angabe]")
    assert page.clicks[-1] == f"{DLG} >> role=button[Nachricht senden]"


async def test_send_dialog_form_without_name_is_missing_field(fast):
    page = FakePage(KA_DIALOG)
    with pytest.raises(MissingFieldError) as e:
        await Kleinanzeigen().send(page, AD, "Hallo", {**KA_PROFILE, "first_name": ""})
    assert e.value.field == "first_name" and not any("Nachricht senden" in c for c in page.clicks)
