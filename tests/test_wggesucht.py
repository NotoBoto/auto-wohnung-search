from pathlib import Path

import pytest

from models import Listing
from sources.base import BlockedError, SourceBroken
from sources.wggesucht import WgGesucht, months_between

FIX = Path(__file__).parent / "fixtures"
URL = "https://www.wg-gesucht.de/wohnungen-in-Bonn-Suedstadt.14258763.html"


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def detail(html: str | None = None) -> Listing:
    return WgGesucht.parse_detail(html if html is not None else read("wggesucht/detail.html"), Listing("wggesucht", "14258763", URL))


def test_parse_search():
    items = WgGesucht.parse_search(read("wggesucht/search.html"))
    assert len(items) >= 10
    assert all(i.platform == "wggesucht" and i.ext_id and i.ext_id in i.url for i in items)
    assert all(i.url.startswith("https://www.wg-gesucht.de/wohnungen-in-Bonn") for i in items)   # раздел «Wohnungen», не WG-Zimmer
    assert all(i.warm_rent and i.area and i.rooms and i.title for i in items)
    assert not any(i.cold_rent for i in items)


def test_parse_search_first_item():
    first = WgGesucht.parse_search(read("wggesucht/search.html"))[0]
    assert (first.ext_id, first.warm_rent, first.area, first.rooms, first.district) == ("14241324", 1200.0, 67.0, 2.0, "Zentrum")
    assert first.url == "https://www.wg-gesucht.de/wohnungen-in-Bonn-Zentrum.14241324.html"
    assert (first.street, first.available_from, first.limited_months) == ("An der Evangelischen Kirche", "29.11.2026", 2)
    assert first.contact_name == "Max Mustermann" and first.title.startswith("Voll möblierte 2-Zimmer Wohnung")


def test_parse_search_half_rooms_and_open_end():
    items = {i.ext_id: i for i in WgGesucht.parse_search(read("wggesucht/search.html"))}
    assert items["14251819"].rooms == 2.5
    assert items["14262303"].limited_months is None and items["14262303"].available_from == "01.11.2026"


def test_parse_detail():
    l = detail()
    assert (l.rooms, l.area, l.warm_rent, l.postcode) == (3.0, 60.0, 750.0, "53113")
    assert (l.cold_rent, l.extra_costs, l.heating_costs) == (600.0, 150.0, None)
    assert (l.street, l.district, l.available_from) == ("Kaiserstraße", "Südstadt", "04.11.2026")
    assert l.title == "Furnished Apartment Available for Temporary Rent"
    assert l.description.startswith("Ground-floor apartment") and "SCHUFA" not in l.description
    assert l.limited_months == 4   # frei ab 04.11.2026 bis 31.03.2027


def test_parse_detail_other_costs_and_text_lease():
    l = detail(read("wggesucht/detail_limited.html"))
    assert (l.warm_rent, l.cold_rent, l.extra_costs, l.heating_costs) == (1200.0, 1000.0, 150.0, 50.0)
    assert (l.rooms, l.area, l.postcode, l.district, l.street) == (2.0, 67.0, "53113", "Zentrum", "An der Evangelischen Kirche")
    assert l.limited_months == 2   # даты 29.11.2026–24.02.2027 важнее текста «dreimonatige Zwischenmiete»
    assert not l.is_swap and not l.wbs_required


def test_parse_detail_open_ended():
    l = detail(read("wggesucht/detail_open.html"))
    assert (l.warm_rent, l.cold_rent, l.extra_costs, l.available_from) == (895.0, 710.0, 185.0, "15.04.2027")
    assert l.limited_months is None
    assert not (l.is_shared_flat or l.is_swap or l.wbs_required or l.furnished_temporary)


def test_parse_detail_missing_title_raises():
    with pytest.raises(SourceBroken):
        detail("<html><body>nichts</body></html>")


def _with_desc(desc: str, extra: str = "") -> str:
    return ('<h1 class="detailed-view-title"><span class="">Wohnung</span></h1>'
            '<span class="key_fact_detail">Gesamtmiete</span><b class="key_fact_value"> 500€ </b>' + extra +
            f'<div id="ad_description_text"><div id="freitext_0" class="wordWrap section_freetext"><p class="">{desc}</p></div></div>')


@pytest.mark.parametrize("desc,field,value", [
    ("Zimmer in netter WG, Mitbewohner gesucht", "is_shared_flat", True),
    ("Tausche gegen größere Wohnung", "is_swap", True),
    ("Zwischenmiete, befristet auf 6 Monate", "limited_months", 6),
    ("Wohnen auf Zeit, möbliert auf Zeit", "furnished_temporary", True),
    ("Nur mit WBS", "wbs_required", True),
    ("Kein WBS erforderlich", "wbs_required", False),
])
def test_detail_flags(desc, field, value):
    assert getattr(detail(_with_desc(desc)), field) == value


def test_detail_dates_win_over_text():
    dates = ('<span class="section_panel_detail">frei ab:</span><span class="section_panel_value"> 01.01.2027 </span>'
             '<span class="section_panel_detail">frei bis:</span><span class="section_panel_value"> 01.07.2027 </span>')
    assert detail(_with_desc("befristet auf 12 Monate", dates)).limited_months == 6


@pytest.mark.parametrize("a,b,n", [
    ("04.11.2026", "31.03.2027", 4), ("01.11.2026", "01.04.2027", 5), ("29.11.2026", "24.02.2027", 2),
    ("05.02.2027", "26.02.2027", 0), ("01.03.2027", "01.03.2029", 24),
])
def test_months_between(a, b, n):
    assert months_between(a, b) == n


def test_months_between_invalid():
    assert months_between("", "31.03.2027") is None
    assert months_between("01.05.2027", "01.04.2027") is None
    assert months_between("quatsch", "01.04.2027") is None


def test_search_url():
    u = WgGesucht().search_url({"max_warm": 900, "min_rooms": 2})
    assert u.startswith("https://www.wg-gesucht.de/wohnungen-in-Bonn.13.2.1.0.html?") and "rMax=900" in u
    assert "categories%5B0%5D=2" in u and "sort_column=0" in u and "sort_order=0" in u
    assert "rMax" not in WgGesucht().search_url({})


def test_parse_search_garbage_raises():
    with pytest.raises(SourceBroken):
        WgGesucht.parse_search("<html><body>Wartung</body></html>")


def test_parse_search_empty_marker_ok():
    assert WgGesucht.parse_search('<div id="main_column"><div class="alert">Leider keine Anzeigen gefunden</div></div>') == []


def test_parse_search_cards_without_parse_raises():
    with pytest.raises(SourceBroken):
        WgGesucht.parse_search('<a class="wgg-list-card offer_list_item" data-id="1" href="/wohnungen-in-Bonn-Zentrum.1.html"><p>x</p></a>')


def test_parse_search_no_title_skipped_with_warning(caplog):
    bad = '<a class="wgg-list-card offer_list_item  " data-id="1" href="/wohnungen-in-Bonn-Zentrum.1.html"><p>x</p></a>'
    items = WgGesucht.parse_search(bad + read("wggesucht/search.html"))
    assert len(items) >= 10 and "без заголовка" in caplog.text


def test_detect_block():
    assert WgGesucht.detect_block("", "https://www.wg-gesucht.de/?modal=sign_in") == "logged_out"
    assert WgGesucht.detect_block("<html><body>Bitte lösen Sie das Captcha</body></html>", "https://www.wg-gesucht.de/x") == "captcha"
    assert WgGesucht.detect_block(read("wggesucht/search.html"), "https://www.wg-gesucht.de/wohnungen-in-Bonn.13.2.1.0.html") is None
    assert WgGesucht.detect_block(read("wggesucht/detail.html"), URL) is None


@pytest.mark.parametrize("html", [
    '<div id="main_column"><h2>Bitte lösen Sie das Captcha</h2></div>',
    '<div id="main_column">Zugriff verweigert</div>',
    '<html><head><title>Just a moment...</title></head><body><div id="main_column"></div></body></html>',
    '<div id="main_column"><script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1?ray=1&cf-chl=x"></script></div>',
    '<div id="main_column">Checking your browser before accessing wg-gesucht.de</div>',
])
def test_detect_block_inside_site_layout(html):
    # страница проверки внутри обычного макета сайта (main_column есть везде) — всё равно капча
    assert WgGesucht.detect_block(html, "https://www.wg-gesucht.de/x") == "captcha"


@pytest.mark.parametrize("name", ["search.html", "detail.html", "detail_limited.html", "detail_open.html"])
def test_detect_block_fixtures_ok(name):
    assert WgGesucht.detect_block(read(f"wggesucht/{name}"), "https://www.wg-gesucht.de/x") is None


def test_detect_block_ignores_captcha_input_attribute():
    html = '<div id="main_column"><form><label data-input="captcha">Sicherheitscode</label></form></div>'
    assert WgGesucht.detect_block(html, "https://www.wg-gesucht.de/x") is None


def test_detect_block_ignores_invisible_recaptcha_on_normal_page():
    # обычная главная после входа: невидимая reCAPTCHA и список доменов cookie-баннера — не проверка
    html = ('<script>window.cmp_block_ignoredomains = ["recaptcha.net", "meineschufa.de"];</script>'
            '<div id="main_column">Willkommen</div>'
            '<iframe src="https://www.google.com/recaptcha/api2/aframe" width="0" height="0"></iframe>')
    assert WgGesucht.detect_block(html, "https://www.wg-gesucht.de/") is None


async def test_is_logged_in_captcha_raises():
    class P:
        url = "https://www.wg-gesucht.de/"
        async def content(self): return "<html>Bitte lösen Sie das Captcha</html>"
    with pytest.raises(BlockedError):
        await WgGesucht().is_logged_in(P())


# ---------- send (поддельная страница) ----------

from fake_page import FakePage, fast_type, no_wait   # noqa: E402
from sources.base import NotSubmittedError   # noqa: E402

WG_FORM = ("textarea#message_input", "button:has-text('Nachricht senden')")


@pytest.fixture
def fast(monkeypatch):
    import sources.wggesucht as wg
    monkeypatch.setattr(wg, "human_type", fast_type)
    monkeypatch.setattr(wg, "human_pause", no_wait)


async def test_send_ok(fast):
    page = FakePage(WG_FORM)
    await WgGesucht().send(page, Listing("wggesucht", "14258763", URL), "Hallo", {})
    assert page.typed == "Hallo" and page.submitted and "/nachricht-senden/" in page.url


async def test_send_form_missing_is_not_submitted(fast):
    page = FakePage(())
    with pytest.raises(NotSubmittedError):
        await WgGesucht().send(page, Listing("wggesucht", "14258763", URL), "Hallo", {})
    assert not page.submitted


async def test_send_unconfirmed_after_click_is_not_not_submitted(fast):
    page = FakePage(WG_FORM, confirm=False)
    with pytest.raises(RuntimeError) as e:
        await WgGesucht().send(page, Listing("wggesucht", "14258763", URL), "Hallo", {})
    assert not isinstance(e.value, (NotSubmittedError, SourceBroken)) and page.submitted
