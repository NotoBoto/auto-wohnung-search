from datetime import date, datetime, timedelta

import pytest

from models import Listing
from store import Store, addr_key, contact_key

NOW = datetime(2026, 10, 21, 12, 0, 0)


def L(**kw):
    kw.setdefault("platform", "kleinanzeigen")
    kw.setdefault("ext_id", "1")
    kw.setdefault("url", "https://x/1")
    kw.setdefault("postcode", "53115")
    return Listing(**kw)


@pytest.fixture
def store():
    return Store(":memory:", clock=lambda: NOW)


def test_addr_key_and_contact_key():
    assert addr_key(L(street="Musterstraße 12")) == "musterstr12|53115"
    assert addr_key(L(street="Muster-Strasse 12")) == addr_key(L(street="Musterstr. 12"))
    assert addr_key(L(street="", district="Poppelsdorf")) == "|53115|Poppelsdorf"
    assert contact_key("Herr  Müller Immobilien GmbH") == "müller"
    assert contact_key("") == ""


def test_same_flat_on_two_platforms_is_exact(store):
    a = L(platform="kleinanzeigen", ext_id="1", street="Musterstraße 12", rooms=2, area=60)
    aid = store.add(a, 850)
    store.set_status(aid, "sent")
    b = L(platform="immowelt", ext_id="x9", street="Musterstr. 12", rooms=2, area=61.5)
    r = store.find_dedup(b, 870)
    assert r.kind == "exact" and r.match_id == aid


def test_repost_new_id_is_exact(store):
    aid = store.add(L(ext_id="1", street="Musterstr. 12", rooms=2, area=60), 850)
    store.set_status(aid, "sent")
    assert store.find_dedup(L(ext_id="2", street="Musterstraße 12", rooms=2, area=60), 850).kind == "exact"


def test_same_platform_ext_id_is_exact_any_status(store):
    aid = store.add(L(ext_id="1", street="A 1"), 800)
    store.set_status(aid, "filtered", reason="WG")
    r = store.find_dedup(L(ext_id="1", street="Ganz anders 99"), 800)
    assert (r.kind, r.match_id) == ("exact", aid)


def test_no_street_is_similar(store):
    aid = store.add(L(ext_id="1", district="Poppelsdorf", rooms=2, area=60), 850)
    store.set_status(aid, "sent")
    r = store.find_dedup(L(platform="immowelt", ext_id="2", district="Poppelsdorf", rooms=2, area=61), 860)
    assert (r.kind, r.match_id) == ("similar", aid)


def test_one_side_without_street_is_similar(store):
    aid = store.add(L(ext_id="1", street="Musterstr. 12", rooms=2, area=60), 850)
    store.set_status(aid, "queued")
    assert store.find_dedup(L(platform="immowelt", ext_id="2", rooms=2, area=60), 850).kind == "similar"


def test_incompatible_attributes_is_new(store):
    store.set_status(store.add(L(street="Musterstr. 12", rooms=2, area=60), 850), "sent")
    assert store.find_dedup(L(ext_id="2", street="Musterstr. 12", rooms=3, area=60), 850).kind == "new"
    assert store.find_dedup(L(ext_id="2", street="Musterstr. 12", rooms=2, area=65), 850).kind == "new"
    assert store.find_dedup(L(ext_id="2", street="Musterstr. 12", rooms=2, area=60), 900).kind == "new"
    # пропуски считаются совместимыми
    assert store.find_dedup(L(ext_id="2", street="Musterstr. 12"), None).kind == "exact"


def test_different_flat_same_landlord_is_new(store):
    store.set_status(store.add(L(street="Musterstr. 12", contact_name="Müller Immobilien", rooms=2, area=60), 850), "sent")
    b = L(ext_id="2", street="Andere Str. 5", contact_name="Müller Immobilien", rooms=2, area=60)
    assert store.find_dedup(b, 850).kind == "new"


def test_older_than_60_days_is_new(store):
    aid = store.add(L(street="Musterstr. 12"), 850)
    store.set_status(aid, "sent", sent_at=NOW - timedelta(days=61))
    assert store.find_dedup(L(ext_id="2", street="Musterstr. 12"), 850).kind == "new"
    store.set_status(aid, "sent", sent_at=NOW - timedelta(days=59))
    assert store.find_dedup(L(ext_id="2", street="Musterstr. 12"), 850).kind == "exact"


def test_filtered_listing_does_not_block(store):
    for st in ("filtered", "seen", "duplicate", "skipped", "failed"):
        s = Store(":memory:", clock=lambda: NOW)
        s.set_status(s.add(L(street="Musterstr. 12"), 850), st)
        assert s.find_dedup(L(ext_id="2", street="Musterstr. 12"), 850).kind == "new", st
    for st in ("draft", "queued", "sending", "ask_dup"):
        s = Store(":memory:", clock=lambda: NOW)
        s.set_status(s.add(L(street="Musterstr. 12"), 850), st)
        assert s.find_dedup(L(ext_id="2", street="Musterstr. 12"), 850).kind == "exact", st


def test_add_is_idempotent(store):
    a = L(street="Musterstr. 12")
    assert store.add(a, 1) == store.add(a, 1)
    row = store.get(store.add(a, 1))
    assert (row.platform, row.ext_id, row.status, row.warm) == ("kleinanzeigen", "1", "seen", 1)
    assert row.created_at == NOW and row.sent_at is None and row.draft_msg_id is None


def test_set_status_fields(store):
    i = store.add(L(), 800)
    store.set_status(i, "draft", letter="Hallo.", draft_msg_id=77, reason="r")
    r = store.get(i)
    assert (r.status, r.letter, r.draft_msg_id, r.reason) == ("draft", "Hallo.", 77, "r")
    assert [x.id for x in store.by_status("draft")] == [i]
    with pytest.raises(ValueError):
        store.set_status(i, "bogus")


def test_incident_opens_once(store):
    assert store.open_incident("immoscout", "captcha") is True
    assert store.open_incident("immoscout", "captcha") is False
    assert store.open_incidents() == [("immoscout", "captcha")]
    store.close_incidents("immoscout")
    assert store.open_incidents() == []
    assert store.open_incident("immoscout", "captcha") is True


def test_contact_cap_counts_7_days(store):
    for i, days in enumerate((0, 3, 6, 8)):
        rid = store.add(L(ext_id=str(i), street=f"S {i}", contact_name="Herr Müller GmbH"), 800)
        store.set_status(rid, "sent", sent_at=NOW - timedelta(days=days))
    assert store.contact_sent_count(contact_key("Müller")) == 3
    assert store.contact_sent_count("") == 0


def test_sent_count_and_last_sent_at(store):
    assert store.last_sent_at("immowelt") is None
    for i, h in enumerate((1, 5, 30)):
        rid = store.add(L(platform="immowelt", ext_id=str(i)), 800)
        store.set_status(rid, "sent", sent_at=NOW - timedelta(hours=h))
    store.set_status(store.add(L(platform="wggesucht"), 800), "sent", sent_at=NOW)
    assert store.sent_count("immowelt", NOW - timedelta(hours=6)) == 2
    assert store.last_sent_at("immowelt") == NOW - timedelta(hours=1)


def test_recent_openings(store):
    for i, (txt, h) in enumerate([("Guten Tag Frau X! Ich suche. Danke", 3), ("Hallo, ich bin Max. Mehr", 1), ("Ohne Punkt", 2)]):
        rid = store.add(L(ext_id=str(i)), 800)
        store.set_status(rid, "sent", letter=txt, sent_at=NOW - timedelta(hours=h))
    store.set_status(store.add(L(ext_id="d"), 800), "draft", letter="Nicht gesendet.")
    assert store.recent_openings(2) == ["Hallo, ich bin Max.", "Ohne Punkt"]
    assert store.recent_openings() == ["Hallo, ich bin Max.", "Ohne Punkt", "Guten Tag Frau X!"]


def test_match_reply_by_ext_id_url_contact(store):
    a = store.add(L(ext_id="1", url="https://x/1", contact_name="Frau Schmidt Immobilien"), 800)
    b = store.add(L(ext_id="2", url="https://x/2", contact_name="Frau Schmidt Immobilien"), 800)
    store.add(L(ext_id="3", url="https://x/3"), 800)
    for i, h in ((a, 5), (b, 1)):
        store.set_status(i, "sent", sent_at=NOW - timedelta(hours=h))
    assert store.match_reply("kleinanzeigen", ext_id="1").id == a
    assert store.match_reply("kleinanzeigen", url="https://x/2").id == b
    assert store.match_reply("kleinanzeigen", contact="Schmidt").id == b   # новейшее
    assert store.match_reply("immowelt", ext_id="1") is None               # другая площадка
    assert store.match_reply("kleinanzeigen", ext_id="3") is None          # не sent
    assert store.match_reply("kleinanzeigen") is None
    assert store.match_reply("kleinanzeigen", contact="") is None


def test_day_stats(store):
    ids = [store.add(L(ext_id=str(i)), 800) for i in range(6)]
    store.set_status(ids[0], "sent")
    store.set_status(ids[1], "filtered", reason="WG")
    store.set_status(ids[2], "filtered", reason="WG")
    store.set_status(ids[3], "filtered", reason="WBS")
    store.set_status(ids[4], "duplicate")
    store.set_status(ids[5], "failed")
    store.add_reply(ids[0], "Termin?", "https://x/r")
    s = store.day_stats(date(2026, 10, 21))
    assert s == {"found": 6, "sent": 1, "filtered": {"WG": 2, "WBS": 1},
                 "duplicates": 1, "replies": 1, "failed": 1}
    assert store.day_stats(date(2026, 10, 20))["found"] == 0
