from pathlib import Path

import pytest

from models import Listing
from sources.base import BlockedError, MissingFieldError, SourceBroken
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
