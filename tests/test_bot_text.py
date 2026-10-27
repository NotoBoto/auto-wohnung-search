import pytest

from bot_text import (PROFILE_STEPS, format_digest, format_draft, format_incident, format_profile,
                      format_reply, format_status, parse_filters_args, parse_profile_answer)
from mail import Reply
from models import PROFILE_FIELDS
from store import Row

ROW = Row(id=7, platform="kleinanzeigen", ext_id="1", url="https://example.test/a/1", title="2 Zimmer, Bonn",
          status="draft", letter="", warm=850.0, contact_key="", created_at=None, sent_at=None,
          reason="", draft_msg_id=None)
REPLY = Reply(platform="wggesucht", ext_id="9", url="https://example.test/9", contact="Frau Schmidt",
              excerpt="Besichtigung am Montag möglich", link="https://mail.example/x")


def test_format_draft_contains_essentials():
    s = format_draft(ROW, "Sehr geehrte...", warm_estimated=True)
    assert ROW.url in s and "≈" in s and "Sehr geehrte" in s


def test_format_draft_exact_warm_has_no_approx_and_shows_reason():
    import dataclasses
    s = format_draft(dataclasses.replace(ROW, reason="нет полей: phone"), "Text", warm_estimated=False)
    assert "≈" not in s and "850" in s and "нет полей: phone" in s


def test_format_reply_with_row():
    s = format_reply(ROW, REPLY)
    assert ROW.url in s and "Besichtigung am Montag" in s and REPLY.link in s


def test_format_reply_without_row():
    s = format_reply(None, REPLY)
    assert "не нашёл, к какому объявлению" in s
    assert "wggesucht" in s and "Besichtigung am Montag" in s and REPLY.link in s


@pytest.mark.parametrize("kind,word", [
    ("captcha", "проверк"), ("logged_out", "аккаунт"), ("blocked", "блокир"),
    ("broken", "разобрать"), ("slow", "медленно"), ("send", "отправ"), ("ai", "нейросет")])
def test_format_incident_per_kind(kind, word):
    s = format_incident("kleinanzeigen", kind, "детали XYZ")
    assert word in s.lower() and "детали XYZ" in s
    if kind != "ai":
        assert "kleinanzeigen" in s


def test_format_incident_unknown_kind():
    assert "weird" in format_incident("immowelt", "weird", "d")


def test_parse_filters_args():
    assert parse_filters_args(["max_warm", "950"]) == {"max_warm": 950}
    assert parse_filters_args(["min_rooms", "3"]) == {"min_rooms": 3.0}
    assert parse_filters_args(["min_rooms", "2,5", "max_warm", "800"]) == {"min_rooms": 2.5, "max_warm": 800}
    with pytest.raises(ValueError):
        parse_filters_args(["max_warm", "много"])
    with pytest.raises(ValueError):
        parse_filters_args(["colour", "1"])
    with pytest.raises(ValueError):
        parse_filters_args(["max_warm"])


def test_parse_profile_answer():
    assert parse_profile_answer("persons", "2") == 2
    assert parse_profile_answer("wbs", "нет") is False
    assert parse_profile_answer("wbs", "да") is True
    assert parse_profile_answer("documents", "Schufa, Gehaltsnachweise") == ["Schufa", "Gehaltsnachweise"]
    assert parse_profile_answer("salutation", "herr") == "Herr"
    assert parse_profile_answer("net_income", "3 200") == 3200
    assert parse_profile_answer("phone", "-") is None
    assert parse_profile_answer("address", "Musterstraße 5, 53111 Bonn") == "Musterstraße 5, 53111 Bonn"
    assert parse_profile_answer("address", "-") is None
    with pytest.raises(ValueError):
        parse_profile_answer("address", "Bonn")


@pytest.mark.parametrize("field,text", [("persons", "много"), ("persons", "0"), ("wbs", "наверное"),
                                        ("salutation", "Дядя"), ("first_name", "-"), ("nope", "x")])
def test_parse_profile_answer_errors(field, text):
    with pytest.raises(ValueError):
        parse_profile_answer(field, text)


def test_profile_steps_cover_fields():
    names = [f for f, _ in PROFILE_STEPS]
    assert names == [f for f in PROFILE_FIELDS if f != "hidden"]
    assert all(q for _, q in PROFILE_STEPS)


def test_digest_lists_filter_reasons():
    s = format_digest({"found": 12, "sent": 3, "filtered": {"дороже 900 €": 7}, "duplicates": 1, "replies": 1, "failed": 0})
    assert "12" in s and "дороже 900 €: 7" in s


def test_digest_lists_missed_drafts():
    s = format_digest({"found": 1, "sent": 0, "filtered": {}, "duplicates": 0, "replies": 2, "failed": 0,
                       "skipped": 1, "expired": 3, "drafts_waiting": 2})
    assert "Пропущено вами: 1" in s and "устарело без ответа: 3" in s and "Ждут решения сейчас: 2" in s
    assert "Ответов: 2" in s
    assert "устарело" not in format_digest({"found": 0, "expired": 0})


def test_format_status():
    s = format_status({"paused": ["wggesucht"], "paused_all": False,
                       "modes": {"kleinanzeigen": "confirm", "wggesucht": "auto"},
                       "incidents": [("wggesucht", "captcha")], "drafts": 2, "queued": 1,
                       "today": {"found": 5, "sent": 1, "filtered": {}, "duplicates": 0, "replies": 0, "failed": 0}})
    assert "wggesucht" in s and "captcha" in s and "auto" in s and "2" in s


def test_format_profile_marks_hidden():
    s = format_profile({"first_name": "Max", "about": "x", "hidden": ["about"]})
    assert "Max" in s and "скрыто" in s


def test_parse_profile_answer_number_formats():
    assert parse_profile_answer("net_income", "3.200 €") == 3200
    with pytest.raises(ValueError):
        parse_profile_answer("persons", "2,5")


# --- ссылка на карту ---

def test_map_line_exact_address():
    import dataclasses
    from bot_text import map_line
    r = dataclasses.replace(ROW, street="Musterstraße 12", postcode="53111", district="Altstadt")
    line = map_line(r)
    assert line.startswith("📍 Musterstraße 12, 53111 Bonn (Altstadt)")
    assert "https://maps.google.com/?q=Musterstra%C3%9Fe+12%2C+53111+Bonn" in line and "примерно" not in line


def test_map_line_street_without_number_is_approximate():
    import dataclasses
    from bot_text import map_line
    line = map_line(dataclasses.replace(ROW, street="Kaiserstraße", postcode="53113"))
    assert "Kaiserstraße, 53113 Bonn" in line and "примерно" in line


def test_map_line_only_district():
    import dataclasses
    from bot_text import map_line
    line = map_line(dataclasses.replace(ROW, district="Bonn-Beuel"))
    assert "Bonn-Beuel" in line and "примерно" in line


def test_map_line_none_without_address():
    from bot_text import map_line
    assert map_line(ROW) is None


def test_draft_and_reply_show_map():
    import dataclasses
    r = dataclasses.replace(ROW, street="Musterstraße 12", postcode="53111")
    assert "📍" in format_draft(r, "Text", False) and "📍" in format_reply(r, REPLY)
    assert "📍" not in format_draft(ROW, "Text", False)
