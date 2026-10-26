import json
import re
from pathlib import Path

import pytest

from fake_page import FakePage, fast_type, no_wait
from models import Listing
from sources.base import BlockedError, MissingFieldError, NotSubmittedError, SourceBroken
from sources.immoscout import BASE, ImmoScout

FIX = Path(__file__).parent / "fixtures" / "immoscout"
URL = "https://www.immobilienscout24.de/Suche/de/nordrhein-westfalen/bonn/wohnung-mieten"
SEARCH = (FIX / "search.html").read_text(encoding="utf-8")
EXPOSE = (FIX / "expose.html").read_text(encoding="utf-8")
FORM = "form[data-testid='contact-form']"
PROFILE = {"salutation": "Herr", "first_name": "Max", "last_name": "Muster", "persons": 2, "pets": "keine",
           "net_income": 3200, "phone": "0170 123", "address": "Musterstraße 5a, 53111 Bonn"}


def _search_with(entries) -> str:
    model = {"searchResponseModel": {"resultlist.resultlist": {
        "paging": {"numberOfHits": len(entries) if isinstance(entries, list) else 1},
        "resultlistEntries": [{"resultlistEntry": entries}]}}}
    return f"<script>IS24.resultList = {{ resultListModel: {json.dumps(model)} }};</script>"


def _entry(id_, title="Schöne Wohnung", paywall=None) -> dict:
    x = {"@id": id_, "title": title, "price": {"value": 700}, "calculatedTotalRent": {"totalRent": {"value": 900}}}
    if paywall is not None:
        x["paywallListing"] = {"active": paywall}
    return {"@id": id_, "resultlist.realEstate": x}


# ---------- выдача ----------

def test_parse_search_fixture():
    ls = ImmoScout.parse_search(SEARCH)
    ids = [l.ext_id for l in ls]
    assert len(ls) == 7 and "172001456" in ids
    l = next(l for l in ls if l.ext_id == "172001456")
    assert l.url == "https://www.immobilienscout24.de/expose/172001456"
    assert l.title == "4-Zimmer-Wohnung in Bonn Castell (5311_32)"
    assert (l.street, l.postcode, l.district) == ("Graurheindorfer Str. 6", "53111", "Castell")
    assert (l.cold_rent, l.warm_rent, l.rooms, l.area) == (1076.82, 1491.82, 4.0, 98.34)
    assert l.contact_name == "Daniela Nuber"


def test_parse_search_skips_mieterplus_listings():
    # первые дни объявление только для MieterPlus: вместо формы — страница оплаты
    assert "172005218" not in [l.ext_id for l in ImmoScout.parse_search(SEARCH)]
    ls = ImmoScout.parse_search(_search_with([_entry("1", paywall="true"), _entry("2", paywall="false"), _entry("3")]))
    assert [l.ext_id for l in ls] == ["2", "3"]


def test_parse_search_only_mieterplus_is_empty_not_broken():
    assert ImmoScout.parse_search(_search_with([_entry("1", paywall="true")])) == []


def test_parse_search_single_entry_object():
    assert [l.ext_id for l in ImmoScout.parse_search(_search_with(_entry("5")))] == ["5"]


def test_parse_search_swap_flag_from_title():
    ls = ImmoScout.parse_search(SEARCH)
    assert next(l for l in ls if l.ext_id == "172005594").is_swap


def test_parse_search_empty_result():
    html = _search_with([]).replace('"numberOfHits": 0', '"numberOfHits": 0')
    assert ImmoScout.parse_search(html) == []


def test_parse_search_unknown_markup_raises():
    with pytest.raises(SourceBroken):
        ImmoScout.parse_search("<html><body>Wohnung mieten</body></html>")
    with pytest.raises(SourceBroken):
        ImmoScout.parse_search("<script>resultListModel: {broken</script>")


def test_search_url_price_filter():
    s = ImmoScout()
    assert s.search_url({}) == f"{URL}?sorting=2"
    assert s.search_url({"max_warm": 950}) == f"{URL}?price=-950.0&pricetype=calculatedtotalrent&sorting=2"


def test_search_url_rooms_filter():
    s = ImmoScout()
    assert s.search_url({"min_rooms": 2}) == f"{URL}?numberofrooms=2.0-&sorting=2"
    assert s.search_url({"min_rooms": 2.5, "max_warm": 900}) == \
        f"{URL}?numberofrooms=2.5-&price=-900.0&pricetype=calculatedtotalrent&sorting=2"


# ---------- объявление ----------

def test_parse_detail_fixture():
    l = ImmoScout.parse_detail(EXPOSE, Listing("immoscout", "171546247", "u"))
    assert l.title == "Barrierefreie 4-Zimmer-Wohnung – flexibel für jede Lebensphase"
    assert (l.cold_rent, l.extra_costs, l.heating_costs, l.warm_rent) == (1750.0, 430.0, 0.0, 2180.0)
    assert (l.rooms, l.area, l.available_from, l.postcode) == (4.0, 124.0, "sofort", "53179")
    assert l.description.startswith("Objektbeschreibung:\nDiese hochwertig ausgestattete Etagenwohnung")
    assert "Ausstattung:\n- großzügiges" in l.description and "\nLage:\nDie Wohnung befindet sich" in l.description


def test_parse_detail_keeps_search_postcode():
    l = ImmoScout.parse_detail(EXPOSE, Listing("immoscout", "1", "u", postcode="53173"))
    assert l.postcode == "53173"


def test_parse_detail_unknown_markup_raises():
    with pytest.raises(SourceBroken):
        ImmoScout.parse_detail("<html></html>", Listing("immoscout", "1", "u"))


# ---------- блокировки и вход ----------

def test_detect_captcha():
    assert ImmoScout.detect_block((FIX / "captcha.html").read_text(encoding="utf-8"), URL) == "captcha"


def test_detect_block_normal_pages():
    assert ImmoScout.detect_block(SEARCH, URL) is None
    assert ImmoScout.detect_block(EXPOSE, "https://www.immobilienscout24.de/expose/171546247") is None


def test_detect_logged_out_on_sso_page():
    assert ImmoScout.detect_block("<html></html>", ImmoScout.login_url) == "logged_out"


def test_hcaptcha_widget_on_normal_page_is_not_block():
    html = '<html><script src="https://js.hcaptcha.com/1/api.js"></script><div class="h-captcha"></div><a href="/expose/123">x</a></html>'
    assert ImmoScout.detect_block(html, URL) is None


def test_challenge_page_with_expose_link_is_captcha():
    html = (FIX / "captcha.html").read_text(encoding="utf-8").replace("</body>", '<a href="/expose/123">x</a></body>')
    assert ImmoScout.detect_block(html, URL) == "captcha"


class _Page:
    def __init__(self, html, url=URL):
        self._h, self.url = html, url

    async def content(self):
        return self._h


async def test_is_logged_in():
    assert await ImmoScout().is_logged_in(_Page(SEARCH)) is True
    assert await ImmoScout().is_logged_in(_Page(SEARCH.replace('"loggedIn":true', '"loggedIn":false')
                                                 .replace("loggedIn: true", "loggedIn: false"))) is False
    # окно осталось на странице входа: переход на выдачу снова приводит туда
    assert await ImmoScout().is_logged_in(FakePage(url=ImmoScout.login_url)) is False


class _DashboardPage(FakePage):
    """После входа окно стоит на «Mein Konto»: признака входа там нет, на выдаче — есть."""
    async def goto(self, url, wait_until=None):
        await super().goto(url, wait_until)
        if "/Suche/" in url:
            self.html = SEARCH


async def test_is_logged_in_checks_search_page_when_marker_missing():
    page = _DashboardPage(html="<html>angemeldet als …</html>")
    page.url = f"{BASE}/meinkonto/dashboard/"
    assert await ImmoScout().is_logged_in(page) is True and "/Suche/" in page.url


async def test_is_logged_in_captcha_raises():
    with pytest.raises(BlockedError) as e:
        await ImmoScout().is_logged_in(_Page((FIX / "captcha.html").read_text(encoding="utf-8")))
    assert e.value.kind == "captcha"


# ---------- отправка ----------

FORM_FIELDS = [f"{FORM} input[data-testid='{t}']" for t in
               ("firstName", "lastName", "phoneNumber", "street", "houseNumber", "postcode", "city",
                "numberOfAdults", "incomeAmount")]
FORM_PRESENT = {"button[data-testid='contact-button']", f"{FORM} textarea[data-testid='message']",
                f"{FORM} select[data-testid='salutation']", f"{FORM} select[data-testid='hasPets']",
                f"{FORM} button[type='submit']:has-text('Abschicken')", *FORM_FIELDS}
# форма 2026-10-26: жильцы, доход, срок въезда — списки; при «Haustiere: Ja» появляется «Anzahl und Tierart»
FORM_2026 = {"button[data-testid='contact-button']", f"{FORM} textarea[data-testid='message']",
             f"{FORM} button[type='submit']:has-text('Abschicken')",
             *(f"{FORM} select[data-testid='{t}']" for t in
               ("salutation", "hasPets", "numberOfPersons", "income", "moveInDateType")),
             *(f"{FORM} input[data-testid='{t}']" for t in
               ("firstName", "lastName", "phoneNumber", "street", "houseNumber", "postcode", "city", "petsInHousehold"))}
LISTING = Listing("immoscout", "171546247", "https://www.immobilienscout24.de/expose/171546247")


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    import sources.immoscout as m
    monkeypatch.setattr(m, "human_type", fast_type)
    monkeypatch.setattr(m, "human_pause", no_wait)


async def test_send_fills_form_and_submits():
    page = FakePage(FORM_PRESENT, html=EXPOSE)
    await ImmoScout().send(page, LISTING, "Hallo", PROFILE)
    f = page.filled
    assert f[f"{FORM} textarea[data-testid='message']"] == "Hallo"
    assert f[f"{FORM} input[data-testid='firstName']"] == "Max"
    assert f[f"{FORM} input[data-testid='lastName']"] == "Muster"
    assert f[f"{FORM} input[data-testid='street']"] == "Musterstraße"
    assert f[f"{FORM} input[data-testid='houseNumber']"] == "5a"
    assert f[f"{FORM} input[data-testid='postcode']"] == "53111"
    assert f[f"{FORM} input[data-testid='city']"] == "Bonn"
    assert f[f"{FORM} input[data-testid='numberOfAdults']"] == "2"
    assert f[f"{FORM} input[data-testid='incomeAmount']"] == "3200"
    assert page.selected == {f"{FORM} select[data-testid='salutation']": "MALE",
                             f"{FORM} select[data-testid='hasPets']": "FALSE"}
    assert page.clicks[0] == "button[data-testid='contact-button']"
    assert page.submitted


async def test_send_without_address_is_missing_field_before_submit():
    page = FakePage(FORM_PRESENT, html=EXPOSE)
    with pytest.raises(MissingFieldError) as e:
        await ImmoScout().send(page, LISTING, "Hallo", {**PROFILE, "address": None})
    assert e.value.field == "address" and not page.submitted


async def test_send_without_form_is_not_submitted():
    page = FakePage(set(), html=EXPOSE)
    with pytest.raises(NotSubmittedError):
        await ImmoScout().send(page, LISTING, "Hallo", PROFILE)
    assert not page.submitted


async def test_send_logged_out_raises_blocked():
    page = FakePage(FORM_PRESENT, html=EXPOSE.replace("userLoggedIn: true", "userLoggedIn: false"))
    with pytest.raises(BlockedError) as e:
        await ImmoScout().send(page, LISTING, "Hallo", PROFILE)
    assert e.value.kind == "logged_out" and not page.submitted


async def test_send_without_confirmation_is_unknown_outcome():
    page = FakePage(FORM_PRESENT, html=EXPOSE, confirm=False)
    with pytest.raises(RuntimeError) as e:
        await ImmoScout().send(page, LISTING, "Hallo", PROFILE)
    assert not isinstance(e.value, (NotSubmittedError, SourceBroken, BlockedError)) and page.submitted


async def test_send_fills_2026_form_selects_and_pets():
    page = FakePage(FORM_2026, html=EXPOSE)
    profile = {**PROFILE, "pets": "Katze 5 Jahre alt", "net_income": 2400, "move_in": "ab sofort"}
    await ImmoScout().send(page, LISTING, "Hallo", profile)
    sel = lambda t: page.selected[f"{FORM} select[data-testid='{t}']"]
    assert sel("hasPets") == "TRUE" and sel("numberOfPersons") == "TWO_PERSON"
    assert sel("income") == "OVER_2000_UPTO_3000" and sel("moveInDateType") == "FROM_NOW"
    assert page.filled[f"{FORM} input[data-testid='petsInHousehold']"] == "Katze 5 Jahre alt"
    assert page.submitted


@pytest.mark.parametrize("persons, income, value, bucket", [
    (1, 499, "ONE_PERSON", "BELOW_500"), (1, 500, "ONE_PERSON", "OVER_500_UPTO_1000"),
    (2, 3000, "TWO_PERSON", "OVER_2000_UPTO_3000"), (2, 6000, "TWO_PERSON", "OVER_5000")])
async def test_send_maps_persons_and_income(persons, income, value, bucket):
    page = FakePage(FORM_2026, html=EXPOSE)
    await ImmoScout().send(page, LISTING, "Hallo", {**PROFILE, "persons": persons, "net_income": income})
    assert page.selected[f"{FORM} select[data-testid='numberOfPersons']"] == value
    assert page.selected[f"{FORM} select[data-testid='income']"] == bucket


async def test_send_rejected_by_form_validation_is_not_submitted():
    # сайт подсветил поле (aria-invalid) и не отправил: письмо точно не ушло
    page = FakePage(FORM_2026 | {f"{FORM} [aria-invalid='true']"}, html=EXPOSE, confirm=False)
    with pytest.raises(NotSubmittedError):
        await ImmoScout().send(page, LISTING, "Hallo", PROFILE)


def test_fixture_has_no_personal_data():
    for f in FIX.glob("*.html"):
        assert not re.search(r"@gmail\.com|yarbol", f.read_text(encoding="utf-8"), re.I), f.name
