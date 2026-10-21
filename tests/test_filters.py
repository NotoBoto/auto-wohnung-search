import pytest

from filters import check, estimate_warm
from models import Listing

FILTERS = {"city": "Bonn", "max_warm": 900, "min_rooms": 2.0, "min_lease_months": 12}


def L(**kw):
    kw.setdefault("postcode", "53115")
    return Listing(platform="kleinanzeigen", ext_id="1", url="u", **kw)


def test_estimate_warm():
    assert estimate_warm(L(warm_rent=880)) == (880, False)
    assert estimate_warm(L(cold_rent=600, extra_costs=150, heating_costs=60)) == (810, False)
    assert estimate_warm(L(cold_rent=700, area=60)) == (850, True)
    assert estimate_warm(L(cold_rent=700)) == (None, False)


@pytest.mark.parametrize("kw,ok,reason", [
    (dict(warm_rent=900, rooms=2), True, ""),
    (dict(warm_rent=901, rooms=2), False, "дороже 900 €"),
    (dict(warm_rent=800, rooms=1.5), False, "меньше 2 комнат"),
    (dict(warm_rent=800, rooms=3), True, ""),
    (dict(warm_rent=800, rooms=2, is_shared_flat=True), False, "WG"),
    (dict(warm_rent=800, rooms=2, is_swap=True), False, "Tausch"),
    (dict(warm_rent=800, rooms=2, wbs_required=True), False, "нужен WBS"),
    (dict(warm_rent=800, rooms=2, limited_months=6), False, "срок 6 мес."),
    (dict(warm_rent=800, rooms=2, furnished_temporary=True), False, "möbliert auf Zeit"),
    (dict(rooms=2), False, "нет цены"),
    (dict(warm_rent=800), False, "нет числа комнат"),
])
def test_check(kw, ok, reason):
    r = check(L(**kw), FILTERS, {})
    assert (r.ok, r.reason) == (ok, reason)


def test_wbs_ok_when_profile_has_wbs():
    assert check(L(warm_rent=800, rooms=2, wbs_required=True), FILTERS, {"wbs": True}).ok


def test_not_bonn_postcode():
    assert check(L(warm_rent=800, rooms=2, postcode="50667"), FILTERS, {}).reason == "не Бонн"


def test_prefilter_on_search_data():
    from filters import prefilter
    f = {"city": "Bonn", "max_warm": 900, "min_rooms": 2.0, "min_lease_months": 12}
    mk = lambda **kw: Listing("kleinanzeigen", "1", "u", title="Wohnung", **kw)
    assert prefilter(mk(warm_rent=1200), f) == "дороже 900 €"
    assert prefilter(mk(cold_rent=950), f) == "дороже 900 €"            # тёплая не меньше холодной
    assert prefilter(mk(cold_rent=800, rooms=1), f) == "меньше 2 комнат"
    assert prefilter(mk(is_shared_flat=True), f) == "WG"
    assert prefilter(mk(is_swap=True), f) == "Tausch"
    assert prefilter(mk(cold_rent=800), f) is None                      # тёплая неизвестна — смотрим подробности
    assert prefilter(mk(), f) is None
