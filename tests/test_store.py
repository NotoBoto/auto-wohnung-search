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
    assert store.contact_count(contact_key("Müller")) == 3
    assert store.contact_count("") == 0


def test_contact_count_includes_pending_rows(store):
    for i, st in enumerate(("sent", "sending", "queued", "draft", "skipped", "failed", "ask_dup")):
        rid = store.add(L(ext_id=str(i), street=f"S {i}", contact_name="Frau Weber"), 800)
        store.set_status(rid, st)
    old = store.add(L(ext_id="old", street="Alt 1", contact_name="Frau Weber"), 800)
    store.set_status(old, "sent", sent_at=NOW - timedelta(days=8))
    assert store.contact_count(contact_key("Weber")) == 4
    assert store.contact_count(contact_key("Weber"), statuses=("sent", "sending"), exclude_id=1) == 1
    assert store.contact_count("") == 0


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


def _sent(store, **kw):
    i = store.add(L(ext_id="1", **kw), 850)
    store.set_status(i, "sent")
    return i


def test_street_without_number_is_similar(store):
    aid = _sent(store, street="Musterstraße 12")
    r = store.find_dedup(L(platform="immowelt", ext_id="2", street="Musterstraße"), 850)
    assert (r.kind, r.match_id) == ("similar", aid)


def test_town_suffix_is_exact(store):
    aid = _sent(store, street="Musterstr. 12")
    r = store.find_dedup(L(platform="immowelt", ext_id="2", street="Musterstr. 12, Bonn"), 850)
    assert (r.kind, r.match_id) == ("exact", aid)
    assert addr_key(L(street="Musterstr. 12, 53115 Bonn")) == "musterstr12|53115"


def test_empty_postcode_is_similar(store):
    aid = _sent(store, street="Musterstr. 12")
    r = store.find_dedup(L(platform="immowelt", ext_id="2", street="Musterstr. 12", postcode=""), 850)
    assert (r.kind, r.match_id) == ("similar", aid)
    # разные непустые PLZ — другая квартира
    assert store.find_dedup(L(ext_id="3", street="Musterstr. 12", postcode="50667"), 850).kind == "new"


def test_sent_at_none_is_filled_from_clock(store):
    i = store.add(L(), 800)
    store.set_status(i, "sent", sent_at=None)
    assert store.get(i).sent_at == NOW
    assert store.sent_count("kleinanzeigen", NOW - timedelta(hours=1)) == 1


def test_meta_roundtrip(tmp_path):
    s = Store(tmp_path / "m.db", clock=lambda: NOW)
    assert s.get_meta("digest_day") is None
    s.set_meta("digest_day", "2026-10-21")
    s.set_meta("digest_day", "2026-10-22")
    assert Store(tmp_path / "m.db").get_meta("digest_day") == "2026-10-22"   # переживает перезапуск


def test_close_incidents_by_kind(store):
    store.open_incident("kleinanzeigen", "send")
    store.open_incident("kleinanzeigen", "captcha")
    store.close_incidents("kleinanzeigen", "send")
    assert store.open_incidents() == [("kleinanzeigen", "captcha")]
    store.close_incidents("kleinanzeigen")
    assert store.open_incidents() == []


def test_street_stored_and_returned(tmp_path):
    s = Store(tmp_path / "s.db")
    id = s.add(Listing("wggesucht", "1", "u", "T", street="Musterstraße 12", postcode="53111", district="Altstadt"), 800)
    r = s.get(id)
    assert (r.street, r.postcode, r.district) == ("Musterstraße 12", "53111", "Altstadt")


def test_old_database_without_street_column_still_opens(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "old.db")
    db.executescript("""CREATE TABLE listings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL, ext_id TEXT NOT NULL,
        url TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'seen',
        letter TEXT NOT NULL DEFAULT '', warm REAL, contact_key TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, sent_at TEXT, reason TEXT NOT NULL DEFAULT '', draft_msg_id INTEGER,
        addr_key TEXT NOT NULL DEFAULT '', has_street INTEGER NOT NULL DEFAULT 0,
        postcode TEXT NOT NULL DEFAULT '', district TEXT NOT NULL DEFAULT '',
        rooms REAL, area REAL, UNIQUE (platform, ext_id));
        INSERT INTO listings (platform, ext_id, created_at, postcode) VALUES ('wggesucht', '9', '2026-10-21T10:00:00.000000', '53111');""")
    db.commit(); db.close()
    s = Store(tmp_path / "old.db")
    assert s.get(1).street == "" and s.get(1).postcode == "53111"


def test_set_status_logs_transition_with_reason(store, caplog):
    caplog.set_level("DEBUG", logger="wohnung")
    i = store.add(L(title="Schöne 2-Zi"), 800)
    store.set_status(i, "filtered", reason="Warmmiete 1100 > 900")
    store.set_status(i, "filtered", reason="Warmmiete 1100 > 900")   # без изменений — без записи
    lines = [r for r in caplog.records if "→" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelname == "DEBUG"
    assert "seen → filtered (Warmmiete 1100 > 900) | Schöne 2-Zi" in lines[0].getMessage()


def test_set_status_draft_and_sent_are_info(store, caplog):
    caplog.set_level("DEBUG", logger="wohnung")
    i = store.add(L(), 800)
    store.set_status(i, "draft", letter="x")
    store.set_status(i, "draft", draft_msg_id=5)   # служебное поле — не переход
    store.set_status(i, "sent")
    moves = [(r.levelname, r.getMessage()) for r in caplog.records if "→" in r.getMessage()]
    assert [lv for lv, _ in moves] == ["INFO", "INFO"]
    assert "seen → draft" in moves[0][1] and "draft → sent" in moves[1][1]
