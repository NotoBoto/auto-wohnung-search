import email
import imaplib
from pathlib import Path

import pytest

import mail
from mail import INBOX, NO_TEXT, MailWatcher, Reply, parse_notification

FIX = Path(__file__).parent / "fixtures" / "mail"


def load_eml(name: str):
    return email.message_from_bytes((FIX / name).read_bytes())


@pytest.mark.parametrize("name,platform", [("kleinanzeigen_reply.eml", "kleinanzeigen"), ("wggesucht_reply.eml", "wggesucht"),
                                           ("immowelt_reply.eml", "immowelt"), ("immoscout_reply.eml", "immoscout")])
def test_parse_reply(name, platform):
    r = parse_notification(load_eml(name))
    assert r.platform == platform and r.link.startswith("https://") and r.excerpt and (r.ext_id or r.url or r.contact)


def test_newsletter_ignored(): assert parse_notification(load_eml("kleinanzeigen_newsletter.eml")) is None
def test_foreign_sender_ignored(): assert parse_notification(load_eml("other.eml")) is None


def test_kleinanzeigen_fields():
    r = parse_notification(load_eml("kleinanzeigen_reply.eml"))
    assert r.ext_id == "3546903271"
    assert r.url == "https://www.kleinanzeigen.de/s-anzeige/3-zimmer-wohnung-bonn-friesdorf/3546903271-203-23751"
    assert r.contact == "Hans Müller"
    assert r.link == "https://www.kleinanzeigen.de/m-nachrichten.html?conversationId=abc123"
    assert r.excerpt.startswith("Guten Tag Herr Beispiel, vielen Dank für Ihr Interesse.")
    assert r.excerpt.endswith("Schufa-Auskunft mit.")
    assert "Besichtigung möglich?" not in r.excerpt and "automatisch" not in r.excerpt   # цитата и подвал отрезаны


def test_wggesucht_fields():
    r = parse_notification(load_eml("wggesucht_reply.eml"))   # base64
    assert r.ext_id == "14258763" and r.url == "https://www.wg-gesucht.de/wohnungen-in-Bonn-Suedstadt.14258763.html"
    assert r.contact == "Sabine Krämer"
    assert r.link.startswith("https://www.wg-gesucht.de/nachricht.html")
    assert "Besichtigung am Dienstag" in r.excerpt and "schrieb" not in r.excerpt and "interessiere" not in r.excerpt


def test_wggesucht_real_notification():
    # настоящий формат WG-Gesucht (обезличен): ссылки на объявление нет, только «schreibt zu Angebot N», текст между линиями «-----»
    r = parse_notification(load_eml("wggesucht_notification.eml"))
    assert r.platform == "wggesucht" and r.ext_id == "1234567" and r.contact == "Herr Weber"
    assert r.link == "https://www.wg-gesucht.de/nachricht.html?nachrichten-id=112233445"
    assert r.excerpt.startswith("Guten Tag Max, vielen Dank für Ihr Interesse.") and r.excerpt.endswith("M. Weber")
    assert "seit Ihrem letzten Login" not in r.excerpt and "vollständige Unterhaltung" not in r.excerpt

def test_immowelt_and_immoscout_fields():
    iw = parse_notification(load_eml("immowelt_reply.eml"))
    assert iw.ext_id == "2a9bd4c1-7e3f-4a7b-9c11-5d0e8f6a1b22" and iw.contact == "Hausverwaltung Rhein GmbH"
    assert "Ihre Nachricht" not in iw.excerpt and "interessiere" not in iw.excerpt
    sc = parse_notification(load_eml("immoscout_reply.eml"))
    assert sc.ext_id == "158765432" and sc.url == "https://www.immobilienscout24.de/expose/158765432"
    assert sc.link.endswith("/nachrichten/posteingang/abc-777") and "Selbstauskunft" in sc.excerpt and "interessiere" not in sc.excerpt


def test_quoted_printable_and_html_only():
    raw = (FIX / "immowelt_reply.eml").read_bytes()
    assert b"quoted-printable" in raw.lower() and b"=C3=" in raw
    msg = email.message_from_bytes(raw)
    for p in list(msg.walk()):   # оставить только html-часть
        if p.get_content_type() == "text/plain":
            p.set_payload("")
    msg2 = email.message_from_string(
        "From: Immowelt <info@immowelt.de>\nSubject: Neue Nachricht\nContent-Type: text/html; charset=utf-8\n\n"
        "<p>Anna Test hat Ihnen geschrieben:</p><p>Termin am <b>Montag</b>?</p>"
        '<a href="https://www.immowelt.de/expose/abc123">E</a><a href="https://www.immowelt.de/nachrichten/1">N</a>')
    r = parse_notification(msg2)
    assert r.ext_id == "abc123" and r.excerpt == "Termin am Montag?" and r.contact == "Anna Test"


def test_excerpt_capped_and_subdomain_sender():
    body = "Hallo,\n\nMax Mustermann hat Ihnen geschrieben:\n\n" + "wort " * 400 + "\n"
    m = email.message_from_string(f"From: x <a@mail.wg-gesucht.de>\nSubject: Neue Nachricht\nContent-Type: text/plain; charset=utf-8\n\n{body}\n"
                                  "https://www.wg-gesucht.de/nachricht.html?id=1\n")
    r = parse_notification(m)
    assert r.platform == "wggesucht" and len(r.excerpt) == 500


class FakeImap:
    def __init__(self, mails: dict[int, bytes], login_error=None):
        self.mails, self.login_error, self.calls, self.loggedout = mails, login_error, [], False

    def login(self, u, p):
        self.calls.append(("login", u))
        if self.login_error:
            raise self.login_error

    def select(self, box, readonly=False):
        self.calls.append(("select", box, readonly))
        return "OK", [b"1"]

    def uid(self, cmd, *args):
        self.calls.append((cmd, *args))
        if cmd == "SEARCH":
            crit = args[1]
            ids = sorted(self.mails)
            if crit.startswith("UID "):
                lo = int(crit.split()[1].split(":")[0])
                ids = [i for i in ids if i >= lo] or ids[-1:]   # как настоящий IMAP: пустой диапазон с * отдаёт последнее письмо
            return "OK", [b" ".join(str(i).encode() for i in ids)]
        if cmd == "FETCH":
            assert "PEEK" in args[1]   # не помечаем письмо прочитанным
            u = int(args[0])
            return "OK", [(f"{u} (UID {u} BODY[] {{{len(self.mails[u])}}}".encode(), self.mails[u]), b")"]
        raise AssertionError(cmd)

    def logout(self):
        self.loggedout = True


def raw(name):
    return (FIX / name).read_bytes()


def watcher(fake, state):
    made = []

    def factory(host, port, **kw):
        made.append((host, port))
        return fake
    w = MailWatcher("me@gmail.com", "secret-app-pw", state, imap_factory=factory)
    w.made = made
    return w


def test_first_run_records_max_uid_only():
    fake = FakeImap({3: raw("kleinanzeigen_reply.eml"), 7: raw("wggesucht_reply.eml")})
    state = {}
    w = watcher(fake, state)
    assert w.fetch_new() == [] and state["last_uid"] == 7
    assert w.made == [("imap.gmail.com", 993)] and ("select", "INBOX", True) in fake.calls and fake.loggedout
    assert not any(c[0] == "FETCH" for c in fake.calls)


def test_fetch_new_returns_replies_and_advances_uid():
    fake = FakeImap({5: raw("kleinanzeigen_reply.eml"), 6: raw("other.eml"), 7: raw("kleinanzeigen_newsletter.eml"),
                     8: raw("immoscout_reply.eml")})
    state = {"last_uid": 5}
    got = watcher(fake, state).fetch_new()
    assert [r.platform for r in got] == ["immoscout"] and all(isinstance(r, Reply) for r in got)
    assert state["last_uid"] == 8
    assert watcher(fake, state).fetch_new() == [] and state["last_uid"] == 8   # пустой диапазон: старое письмо не обрабатывается повторно


def test_check_login():
    ok, msg = watcher(FakeImap({}), {}).check_login()
    assert ok
    bad = FakeImap({}, login_error=imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials"))
    ok, msg = watcher(bad, {}).check_login()
    assert not ok and msg == "Неверный адрес или пароль приложения Gmail" and "secret-app-pw" not in msg
    assert bad.loggedout


def eml(frm, subj, body):
    return email.message_from_string(f"From: {frm}\nSubject: {subj}\nContent-Type: text/plain; charset=utf-8\n\n{body}")


def test_empty_body_still_yields_reply_with_fallbacks():
    r = parse_notification(eml("x <noreply@kleinanzeigen.de>", "Neue Nachricht zu Ihrer Anfrage", " \n"))
    assert r.excerpt == "Neue Nachricht zu Ihrer Anfrage" and r.link == INBOX["kleinanzeigen"]
    r2 = parse_notification(eml("x <noreply@immowelt.de>", "", "Anna hat Ihnen geschrieben:\n"))
    assert r2.excerpt == NO_TEXT and r2.link == INBOX["immowelt"]


def test_greeting_not_message_text():
    link = "https://www.wg-gesucht.de/nachricht.html?id=1"
    assert parse_notification(eml("x <noreply@wg-gesucht.de>", "Neue Nachricht", f"Hallo Max,\nWann passt es dir?\n{link}\n")).excerpt == "Wann passt es dir?"
    assert parse_notification(eml("x <noreply@wg-gesucht.de>", "Neue Nachricht", f"Hinweis\n{link}\n")).excerpt == "Hinweis"   # «Hi…» без границы слова не приветствие


class FlakyImap(FakeImap):
    def __init__(self, mails, fail_uid, mode):
        super().__init__(mails)
        self.fail_uid, self.mode = fail_uid, mode

    def uid(self, cmd, *args):
        if cmd == "FETCH" and int(args[0]) == self.fail_uid:
            if self.mode == "raise":
                raise imaplib.IMAP4.abort("socket error")
            return "NO", [b"temporary"]
        return super().uid(cmd, *args)


@pytest.mark.parametrize("mode", ["raise", "no"])
def test_mid_batch_failure_keeps_earlier_replies_and_does_not_skip(mode):
    fake = FlakyImap({1: raw("kleinanzeigen_reply.eml"), 2: raw("wggesucht_reply.eml"), 3: raw("immoscout_reply.eml")}, 2, mode)
    state = {"last_uid": 0}
    got = watcher(fake, state).fetch_new()
    assert [r.platform for r in got] == ["kleinanzeigen"] and state["last_uid"] == 1
    fake.fail_uid = None
    assert [r.platform for r in watcher(fake, state).fetch_new()] == ["wggesucht", "immoscout"] and state["last_uid"] == 3


def test_parse_exception_yields_minimal_reply(monkeypatch):
    def boom(msg): raise ValueError("x")
    monkeypatch.setattr(mail, "parse_notification", boom)
    state = {"last_uid": 0}
    got = watcher(FakeImap({1: raw("kleinanzeigen_reply.eml"), 2: raw("other.eml")}), state).fetch_new()
    assert len(got) == 1 and got[0].platform == "kleinanzeigen" and got[0].link == INBOX["kleinanzeigen"]
    assert got[0].excerpt.startswith("Sie haben eine neue Nachricht") and state["last_uid"] == 2


def test_persistently_failing_uid_skipped_on_third_poll():
    fake = FlakyImap({1: raw("kleinanzeigen_reply.eml"), 2: raw("wggesucht_reply.eml"), 3: raw("immoscout_reply.eml")}, 2, "raise")
    state = {"last_uid": 0}
    w = watcher(fake, state)
    assert [r.platform for r in w.fetch_new()] == ["kleinanzeigen"] and state["last_uid"] == 1
    assert w.fetch_new() == [] and state["last_uid"] == 1 and state["fail_count"] == 2
    assert [r.platform for r in w.fetch_new()] == ["immoscout"] and state["last_uid"] == 3
    assert "fail_uid" not in state and "fail_count" not in state


def test_uid_failing_once_then_ok_is_delivered_and_counter_reset():
    fake = FlakyImap({1: raw("wggesucht_reply.eml")}, 1, "no")
    state = {"last_uid": 0}
    assert watcher(fake, state).fetch_new() == [] and state["fail_count"] == 1
    fake.fail_uid = None
    assert [r.platform for r in watcher(fake, state).fetch_new()] == ["wggesucht"]
    assert state["last_uid"] == 1 and "fail_count" not in state


def test_unexpected_error_in_message_body_is_treated_as_fetch_failure(monkeypatch):
    def boom(m): raise RuntimeError("x")
    monkeypatch.setattr(mail, "parse_notification", boom)
    monkeypatch.setattr(mail, "_fallback", boom)
    state = {"last_uid": 0}
    assert watcher(FakeImap({1: raw("kleinanzeigen_reply.eml")}), state).fetch_new() == []
    assert state["last_uid"] == 0 and state["fail_uid"] == 1
