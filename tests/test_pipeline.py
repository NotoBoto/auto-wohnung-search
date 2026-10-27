import asyncio
import copy
from datetime import datetime, timedelta

import pytest

import core
from ai import AIError
from mail import Reply
from models import Listing
from pipeline import Engine
from sources.base import BlockedError, ListingGoneError, MissingFieldError, NotSubmittedError, SourceBroken
from store import Store

T0 = datetime(2026, 10, 21, 10, 0, 0)   # наивный UTC = 12:00 в Бонне (CEST)
PROFILE = {"salutation": "Herr", "first_name": "Max", "last_name": "Muster", "persons": 2,
           "move_in": "sofort", "about": "Ingenieur, ruhig"}
FILLER = ("Ich bin ruhig, zuverlässig und arbeite mit Freude in Bonn. Die Wohnung gefällt mir sehr, "
          "und ich würde mich über einen Besichtigungstermin freuen. ")


def letter(n: int) -> str:
    text = f"Ich interessiere mich sehr für Ihre Wohnung, Anfrage Nummer {n}.\n" + FILLER * 6
    return text[:900]


class Clock:
    def __init__(self, now: datetime = T0):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeSource:
    def __init__(self, name: str, listings=(), details=None):
        self.name = name
        self.filters = None
        self.listings = list(listings)
        self.details = details or {}
        self.search_exc = None
        self.send_exc = None
        self.searches = 0
        self.sent: list[tuple[str, str]] = []

    async def search(self, page):
        self.searches += 1
        await asyncio.sleep(0)
        if self.search_exc:
            raise self.search_exc
        return list(self.listings)

    async def fetch_detail(self, page, l):
        await asyncio.sleep(0)
        return self.details.get(l.ext_id, l)

    async def send(self, page, l, text, profile):
        await asyncio.sleep(0)
        if self.send_exc:
            raise self.send_exc
        self.sent.append((l.ext_id, text))


class FakeAI:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.exc = None
        self.answer = None

    async def generate(self, system, user):
        self.calls.append((system, user))
        await asyncio.sleep(0)
        if self.exc:
            raise self.exc
        return self.answer if self.answer is not None else letter(len(self.calls))


class FakeNotifier:
    def __init__(self):
        self.calls: list[tuple] = []

    def names(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]

    async def draft(self, row, text, warm_estimated):
        self.calls.append(("draft", row.id, text, warm_estimated))
        return 100 + len(self.calls)

    async def ask_duplicate(self, row, match):
        self.calls.append(("ask_duplicate", row.id, match.id))

    async def ask_stuck(self, row):
        self.calls.append(("ask_stuck", row.id))

    async def sent(self, row):
        self.calls.append(("sent", row.id))

    async def reply(self, row, r):
        self.calls.append(("reply", row.id if row else None, r))

    async def incident(self, platform, kind, detail):
        self.calls.append(("incident", platform, kind))

    async def missing_profile(self):
        self.calls.append(("missing_profile",))

    async def digest(self, stats):
        self.calls.append(("digest", stats))

    async def remove_draft(self, row):
        self.calls.append(("remove_draft", row.id))

    async def expire_draft(self, row):
        self.calls.append(("expire_draft", row.id))

    async def draft_reminder(self, rows, hours):
        self.calls.append(("draft_reminder", [r.id for r in rows], hours))

    async def gone(self, row):
        self.calls.append(("gone", row.id))


class FakeBrowser:
    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}

    async def page(self, platform):
        return object()

    def lock(self, platform):
        return self._locks.setdefault(platform, asyncio.Lock())


class FakeMail:
    def __init__(self, replies):
        self.replies = replies

    def fetch_new(self):
        out, self.replies = self.replies, []
        return out


def L(platform="kleinanzeigen", ext_id="1", street="Musterstraße 12", **kw):
    kw.setdefault("postcode", "53115")
    kw.setdefault("warm_rent", 800)
    kw.setdefault("rooms", 2)
    kw.setdefault("area", 60)
    kw.setdefault("contact_name", "Herr Müller")
    kw.setdefault("description", "Schöne Wohnung in Bonn.")
    return Listing(platform=platform, ext_id=ext_id, url=f"https://{platform}/{ext_id}", title="Wohnung",
                   street=street, **kw)


def make_cfg(mode="confirm", profile=PROFILE) -> dict:
    cfg = copy.deepcopy(core.DEFAULTS)
    cfg["mode"] = mode
    cfg["profile"] = dict(profile)
    return cfg


class Env:
    def __init__(self, mode="confirm", profile=PROFILE, store=None, clock=None, mail=None):
        self.clock = clock or Clock()
        self.cfg = make_cfg(mode, profile)
        self.store = store or Store(":memory:", clock=self.clock)
        self.ai = FakeAI()
        self.ka = FakeSource("kleinanzeigen")
        self.wg = FakeSource("wggesucht")
        self.notifier = FakeNotifier()
        self.browser = FakeBrowser()
        self.engine = Engine(self.cfg, self.store, self.ai, {"kleinanzeigen": self.ka, "wggesucht": self.wg},
                             self.browser, self.notifier, mail, clock=self.clock)


@pytest.fixture
def eng():
    return Env()


def sent_row(env, ext_id, street, when, platform="kleinanzeigen", contact="Herr Schmidt"):
    i = env.store.add(L(platform, ext_id, street, contact_name=contact), 800)
    env.store.set_status(i, "sent", letter=f"Erster Satz {ext_id}. Rest.", sent_at=when)
    return i


# ---------- process ----------

async def test_auto_mode_sends_and_no_per_send_notification():
    env = Env(mode="auto")
    assert await env.engine.process("kleinanzeigen", L()) == "queued"
    await env.engine.send_queued_once()
    row = env.store.by_status("sent")[0]
    assert env.ka.sent == [("1", row.letter)] and row.sent_at == T0
    # только отметка «отправлено» (в Telegram без черновика ничего не приходит)
    assert not env.notifier.names("draft") and env.notifier.calls == [("sent", row.id)]


async def test_confirm_mode_creates_draft(eng):
    assert await eng.engine.process("kleinanzeigen", L()) == "draft"
    drafts = eng.notifier.names("draft")
    assert len(drafts) == 1
    row = eng.store.by_status("draft")[0]
    assert row.letter == drafts[0][2] and row.draft_msg_id == 101 and drafts[0][3] is False
    await eng.engine.send_queued_once()
    assert eng.ka.sent == []   # черновик сам не уходит


async def test_platform_mode_overrides_global():
    env = Env(mode="confirm")
    env.cfg["platforms"]["wggesucht"]["mode"] = "auto"
    assert env.engine.mode_for("wggesucht") == "auto" and env.engine.mode_for("kleinanzeigen") == "confirm"
    assert await env.engine.process("wggesucht", L("wggesucht")) == "queued"


async def test_same_flat_other_platform_not_sent_twice():
    env = Env(mode="auto")
    assert await env.engine.process("kleinanzeigen", L()) == "queued"
    await env.engine.send_queued_once()
    # та же квартира на другой площадке, адрес виден только в подробностях
    teaser = L("wggesucht", "w1", street="", warm_rent=None)
    env.wg.details["w1"] = L("wggesucht", "w1", street="Musterstr. 12", warm_rent=810)
    assert await env.engine.process("wggesucht", teaser) == "duplicate"
    # перевыложена с новым id на той же площадке
    assert await env.engine.process("kleinanzeigen", L(ext_id="2")) == "duplicate"
    await env.engine.send_queued_once()
    assert len(env.ai.calls) == 1 and len(env.ka.sent) == 1 and env.wg.sent == []


async def test_same_flat_two_platforms_concurrently_one_letter():
    env = Env(mode="auto")
    env.wg.details["w1"] = L("wggesucht", "w1")
    res = await asyncio.gather(env.engine.process("kleinanzeigen", L()),
                               env.engine.process("wggesucht", L("wggesucht", "w1", street="")))
    assert sorted(res) == ["duplicate", "queued"]
    assert len(env.ai.calls) == 1
    await env.engine.send_queued_once()
    assert len(env.ka.sent) + len(env.wg.sent) == 1


async def test_known_listing_is_not_processed_again(eng):
    assert await eng.engine.process("kleinanzeigen", L()) == "draft"
    assert await eng.engine.process("kleinanzeigen", L()) == "draft"
    assert len(eng.ai.calls) == 1 and len(eng.notifier.names("draft")) == 1


async def test_similar_asks_once(eng):
    assert await eng.engine.process("kleinanzeigen", L()) == "draft"
    other = L("wggesucht", "w1", street="Musterstraße 14")
    assert await eng.engine.process("wggesucht", other) == "ask_dup"
    assert await eng.engine.process("wggesucht", other) == "ask_dup"
    asks = eng.notifier.names("ask_duplicate")
    assert len(asks) == 1 and len(eng.ai.calls) == 1
    first = eng.store.by_status("draft")[0]
    assert asks[0][2] == first.id


async def test_similar_but_filtered_is_not_asked(eng):
    assert await eng.engine.process("kleinanzeigen", L()) == "draft"
    eng.wg.details["w1"] = L("wggesucht", "w1", street="Musterstraße 14", warm_rent=810, limited_months=6)
    assert await eng.engine.process("wggesucht", L("wggesucht", "w1", street="", warm_rent=None)) == "filtered"
    assert eng.notifier.names("ask_duplicate") == [] and eng.store.get(2).reason == "срок 6 мес."


async def test_resolve_duplicate(eng):
    await eng.engine.process("kleinanzeigen", L())
    await eng.engine.process("wggesucht", L("wggesucht", "w1", street="Musterstraße 14"))
    await eng.engine.process("wggesucht", L("wggesucht", "w2", street="Musterstraße 16"))
    a, b = (r.id for r in eng.store.by_status("ask_dup"))
    await eng.engine.resolve_duplicate(a, send=False)
    assert eng.store.get(a).status == "duplicate"
    await eng.engine.resolve_duplicate(b, send=True)
    assert eng.store.get(b).status == "draft" and len(eng.ai.calls) == 2


async def test_filtered_with_reason(eng):
    assert await eng.engine.process("kleinanzeigen", L(warm_rent=1200)) == "filtered"
    assert eng.store.by_status("filtered")[0].reason == "дороже 900 €"
    assert eng.ai.calls == []


async def test_skip_answer_filters(eng):
    eng.ai.answer = "SKIP: nur für Studenten"
    assert await eng.engine.process("kleinanzeigen", L()) == "filtered"
    assert eng.store.by_status("filtered")[0].reason == "nur für Studenten"


@pytest.fixture
def pauses(monkeypatch):
    import pipeline
    calls = []

    async def fake(a=0.8, b=2.5):
        calls.append((a, b))
    monkeypatch.setattr(pipeline, "human_pause", fake)
    return calls


class CountingSource(FakeSource):
    def __init__(self, name, listings=()):
        super().__init__(name, listings)
        self.detail_calls: list[str] = []

    async def fetch_detail(self, page, l):
        self.detail_calls.append(l.ext_id)
        return await super().fetch_detail(page, l)


async def test_human_pause_between_detail_loads(pauses):
    env = Env()
    env.ka = env.engine.sources["kleinanzeigen"] = CountingSource("kleinanzeigen", [
        L(ext_id=str(i), street=f"Gasse {i}", contact_name=f"K {i}") for i in range(3)])
    await env.engine.poll_once("kleinanzeigen")
    assert env.ka.detail_calls == ["0", "1", "2"] and pauses == [(3, 8), (3, 8)]
    await env.engine.poll_once("kleinanzeigen")   # всё знакомо: подробности не грузим и не ждём
    assert len(env.ka.detail_calls) == 3 and len(pauses) == 2


async def test_prefilter_without_detail_load(pauses):
    env = Env()
    teaser = [L(ext_id="a", warm_rent=1500), L(ext_id="b", rooms=1, street="B 1"),
              Listing("kleinanzeigen", "c", "https://k/c", title="WG-Zimmer", is_shared_flat=True),
              L(ext_id="d", street="D 4")]
    env.ka = env.engine.sources["kleinanzeigen"] = CountingSource("kleinanzeigen", teaser)
    await env.engine.poll_once("kleinanzeigen")
    await env.engine.poll_once("kleinanzeigen")
    assert env.ka.detail_calls == ["d"] and pauses == []
    assert {r.ext_id: r.reason for r in env.store.by_status("filtered")} == {
        "a": "дороже 900 €", "b": "меньше 2 комнат", "c": "WG"}


async def test_pause_during_human_pause_stops_detail_load(monkeypatch):
    import pipeline
    env = Env()
    env.ka = env.engine.sources["kleinanzeigen"] = CountingSource("kleinanzeigen", [
        L(ext_id=str(i), street=f"Gasse {i}", contact_name=f"K {i}") for i in range(2)])

    async def login_meanwhile(a=0.8, b=2.5):   # владелец нажал «Войти», пока движок ждал
        env.engine.login_started("kleinanzeigen")
    monkeypatch.setattr(pipeline, "human_pause", login_meanwhile)
    await env.engine.poll_once("kleinanzeigen")
    assert env.ka.detail_calls == ["0"] and len(env.store.by_status("draft")) == 1


# ---------- перезапуск ----------

async def test_restart_with_sending_asks_and_does_not_resend():
    env = Env(mode="auto")
    i = env.store.add(L(), 800)
    env.store.set_status(i, "sending", letter=letter(1))
    await env.engine.startup()
    await env.engine.send_queued_once()
    await env.engine.maintenance_once()
    assert env.notifier.names("ask_stuck") == [("ask_stuck", i)]
    assert env.ka.sent == [] and env.store.get(i).status == "sending"
    await env.engine.resolve_stuck(i, was_sent=True)
    assert env.store.get(i).status == "sent" and env.ka.sent == []
    j = env.store.add(L(ext_id="2", street="Andere Str. 1"), 800)
    env.store.set_status(j, "sending", letter=letter(2))
    await env.engine.resolve_stuck(j, was_sent=False)
    assert env.store.get(j).status == "queued"


async def test_stuck_again_after_resolve_asks_again():
    env = Env(mode="auto")
    await env.engine.process("kleinanzeigen", L())
    env.ka.send_exc = RuntimeError("сбой")
    await env.engine.send_queued_once()
    await env.engine.resolve_stuck(1, was_sent=False)
    env.clock.now += timedelta(minutes=5)
    await env.engine.send_queued_once()
    assert env.store.get(1).status == "sending" and len(env.notifier.names("ask_stuck")) == 2
    await env.engine.resolve_stuck(1, was_sent=True)
    assert env.store.get(1).status == "sent" and env.store.open_incidents() == []


async def test_never_sends_row_without_letter_or_generating():
    env = Env(mode="auto")
    i = env.store.add(L(), 800)
    env.store.set_status(i, "queued", reason="generating")
    j = env.store.add(L(ext_id="2", street="Andere Str. 1"), 800)
    env.store.set_status(j, "queued")   # пустое письмо
    await env.engine.send_queued_once()
    assert env.ka.sent == []
    await env.engine.startup()   # прерванная генерация → failed, можно перегенерировать
    assert env.store.get(i).status == "failed" and env.store.get(i).reason == "прервано при генерации"
    await env.engine.send_queued_once()
    assert env.ka.sent == []


# ---------- случаи (incidents) ----------

async def test_captcha_twice_one_incident_others_continue():
    env = Env(mode="auto")
    env.ka.search_exc = BlockedError("captcha")
    env.wg.listings = [L("wggesucht", "w1")]
    await env.engine.poll_once("kleinanzeigen")
    await env.engine.poll_once("kleinanzeigen")
    await env.engine.poll_once("wggesucht")
    assert env.notifier.names("incident") == [("incident", "kleinanzeigen", "captcha")]
    assert env.engine.status()["paused"] == ["kleinanzeigen"]
    assert env.ka.searches == 1                         # на паузе площадку не трогаем
    assert env.store.by_status("queued")[0].platform == "wggesucht"
    # после перезапуска открытый случай держит паузу, повторной капчи и сообщения нет
    env2 = Env(mode="auto", store=env.store, clock=env.clock)
    env2.ka.search_exc = BlockedError("captcha")
    await env2.engine.startup()
    await env2.engine.poll_once("kleinanzeigen")
    assert env2.ka.searches == 0 and env2.notifier.names("incident") == []


async def test_captcha_during_send_requeues_and_pauses():
    env = Env(mode="auto")
    await env.engine.process("kleinanzeigen", L())
    env.ka.send_exc = BlockedError("logged_out")
    await env.engine.send_queued_once()
    await env.engine.send_queued_once()
    assert env.store.by_status("queued") and env.notifier.names("incident") == [("incident", "kleinanzeigen", "logged_out")]
    assert "kleinanzeigen" in env.engine.status()["paused"]


async def test_source_broken_pauses_with_incident():
    env = Env()
    env.ka.search_exc = SourceBroken("селекторы")
    await env.engine.poll_once("kleinanzeigen")
    assert env.notifier.names("incident") == [("incident", "kleinanzeigen", "broken")]
    assert "kleinanzeigen" in env.engine.status()["paused"]


async def test_resume_reopens_incidents():
    env = Env()
    env.ka.search_exc = BlockedError("captcha")
    await env.engine.poll_once("kleinanzeigen")
    env.engine.resume("kleinanzeigen")
    assert env.engine.status()["paused"] == [] and env.store.open_incidents() == []
    await env.engine.poll_once("kleinanzeigen")
    assert len(env.notifier.names("incident")) == 2


async def test_pause_all_and_resume_all():
    env = Env()
    env.engine.pause(None)
    env.ka.listings = [L()]
    await env.engine.poll_once("kleinanzeigen")
    assert env.ka.searches == 0
    env.engine.resume(None)
    await env.engine.poll_once("kleinanzeigen")
    assert env.ka.searches == 1


async def test_login_pauses_platform_and_restores_previous_state():
    env = Env()
    e = env.engine
    e.login_started("kleinanzeigen")
    assert e.status()["paused"] == ["kleinanzeigen"]
    e.login_finished("kleinanzeigen", ok=False)   # вход не удался — как было до входа
    assert e.status()["paused"] == []
    e.pause("wggesucht")
    e.login_started("wggesucht")
    e.login_finished("wggesucht", ok=False)
    assert e.status()["paused"] == ["wggesucht"]   # пауза владельца сохраняется
    env.ka.search_exc = BlockedError("logged_out")
    await e.poll_once("kleinanzeigen")
    e.login_started("kleinanzeigen")
    e.login_finished("kleinanzeigen", ok=True)   # вход выполнен — пауза и случай сняты
    assert e.status()["paused"] == ["wggesucht"] and env.store.open_incidents() == []


async def test_poll_waiting_for_lock_does_not_search_after_login_started():
    env = Env()
    lock = env.browser.lock("kleinanzeigen")
    await lock.acquire()   # окно входа (show_login) держит площадку
    task = asyncio.create_task(env.engine.poll_once("kleinanzeigen"))
    for _ in range(5):
        await asyncio.sleep(0)
    env.engine.login_started("kleinanzeigen")
    lock.release()
    await task
    assert env.ka.searches == 0


async def test_ai_failure_marks_failed_single_incident(eng):
    eng.ai.exc = AIError("нейросеть недоступна")
    assert await eng.engine.process("kleinanzeigen", L()) == "failed"
    assert await eng.engine.process("kleinanzeigen", L(ext_id="2", street="Andere Str. 1")) == "failed"
    assert len(eng.store.by_status("failed")) == 2
    assert eng.notifier.names("incident") == [("incident", "ai", "ai")]
    eng.ai.exc = None   # нейросеть ожила: следующее письмо закрывает случай
    assert await eng.engine.process("kleinanzeigen", L(ext_id="3", street="Dritte Str. 3")) == "draft"
    assert eng.store.open_incidents() == []
    failed = eng.store.by_status("failed")[0]
    await eng.engine.regenerate(failed.id)
    assert eng.store.get(failed.id).status == "draft"


async def test_bad_letter_twice_is_failed(eng):
    eng.ai.answer = "Hello, I would like the apartment."   # английский и короткий → LetterError после повтора
    assert await eng.engine.process("kleinanzeigen", L()) == "failed"
    assert len(eng.ai.calls) == 2 and len(eng.notifier.names("incident")) == 1


# ---------- лимиты ----------

async def test_daily_cap_keeps_queued():
    env = Env(mode="auto")
    for k in range(15):
        sent_row(env, f"s{k}", f"Gasse {k}", T0 - timedelta(minutes=10 + k), contact=f"Kontakt {k}")
    assert await env.engine.process("kleinanzeigen", L()) == "queued"
    await env.engine.send_queued_once()
    assert env.ka.sent == [] and len(env.store.by_status("queued")) == 1


async def test_min_gap_between_sends():
    env = Env(mode="auto")
    await env.engine.process("kleinanzeigen", L())
    await env.engine.process("kleinanzeigen", L(ext_id="2", street="Andere Str. 1", contact_name="Frau Weber"))
    await env.engine.send_queued_once()
    assert len(env.ka.sent) == 1
    env.clock.now += timedelta(seconds=60)
    await env.engine.send_queued_once()
    assert len(env.ka.sent) == 1
    env.clock.now += timedelta(seconds=61)
    await env.engine.send_queued_once()
    assert len(env.ka.sent) == 2


async def test_missing_required_field_becomes_draft_in_auto():
    env = Env(mode="auto")
    assert await env.engine.process("kleinanzeigen", L(required_fields=["phone"])) == "draft"
    assert len(env.notifier.names("draft")) == 1
    # форма потребовала поле при отправке
    env.ka.send_exc = MissingFieldError("phone")
    assert await env.engine.process("kleinanzeigen", L(ext_id="2", street="Andere Str. 1")) == "queued"
    await env.engine.send_queued_once()
    assert env.store.get(2).status == "draft" and len(env.notifier.names("draft")) == 2
    assert env.store.get(2).reason == "нет поля: phone — заполните: /profile phone"


async def test_listing_gone_on_send_fails_row_without_pause():
    env = Env(mode="auto")
    await env.engine.process("kleinanzeigen", L())
    env.ka.send_exc = ListingGoneError("снято")
    await env.engine.send_queued_once()
    # объявление сняли: письмо не ушло и не уйдёт; площадка работает дальше
    row = env.store.get(1)
    assert row.status == "failed" and row.reason == "объявление снято с сайта"
    assert env.notifier.names("gone") == [("gone", 1)] and env.notifier.names("incident") == []
    assert not env.engine._paused_now("kleinanzeigen")


async def test_send_error_keeps_sending_and_asks_owner():
    env = Env(mode="auto")
    await env.engine.process("kleinanzeigen", L())
    env.ka.send_exc = RuntimeError("кнопка не найдена")
    await env.engine.send_queued_once()
    # исход неизвестен (форма могла уйти): не failed, а вопрос владельцу
    assert env.store.get(1).status == "sending" and env.store.by_status("failed") == []
    assert env.notifier.names("ask_stuck") == [("ask_stuck", 1)]
    assert env.notifier.names("incident") == []   # один вопрос вместо двух сообщений
    assert "kleinanzeigen" in env.engine.status()["paused"]
    await env.engine.regenerate(1)
    await env.engine.send_queued_once()
    await env.engine.startup()   # тот же сеанс: повторно не спрашиваем
    assert env.store.get(1).status == "sending" and len(env.ai.calls) == 1
    assert len(env.notifier.names("ask_stuck")) == 1


def five_queued(env):
    for i in range(5):
        env.store.add(L(ext_id=str(i), street=f"Gasse {i}", contact_name=f"Kontakt {i}"), 800)
        env.store.set_status(i + 1, "queued", letter=letter(i))


@pytest.mark.parametrize("exc", [NotSubmittedError("форма не найдена"), SourceBroken("форма не найдена")])
async def test_not_submitted_requeues_pauses_once(exc):
    env = Env(mode="auto")
    five_queued(env)
    calls = []

    async def send(page, l, text, profile):
        calls.append(l.ext_id)
        raise exc
    env.ka.send = send
    for _ in range(5):
        await env.engine.send_queued_once()
        env.clock.now += timedelta(seconds=10)
    assert calls == ["0"] and "kleinanzeigen" in env.engine.status()["paused"]
    assert [r.status for r in map(env.store.get, range(1, 6))] == ["queued"] * 5
    assert env.store.get(1).letter == letter(0)
    assert env.notifier.calls == [("incident", "kleinanzeigen", "broken")]


async def test_unknown_send_error_pauses_platform_one_stuck_row():
    env = Env(mode="auto")
    five_queued(env)
    calls = []

    async def send(page, l, text, profile):
        calls.append(l.ext_id)
        raise RuntimeError("после нажатия")
    env.ka.send = send
    for _ in range(5):
        await env.engine.send_queued_once()
        env.clock.now += timedelta(seconds=10)
    assert calls == ["0"] and "kleinanzeigen" in env.engine.status()["paused"]
    assert len(env.store.by_status("sending")) == 1 and len(env.store.by_status("queued")) == 4
    # один вопрос «дошло ли?», без отдельного сообщения о сбое (чат не засоряется)
    assert [c[0] for c in env.notifier.calls] == ["ask_stuck"]
    assert ("kleinanzeigen", "send") in env.store.open_incidents()


async def test_resolve_stuck_lifts_only_its_own_pause():
    env = Env(mode="auto")
    five_queued(env)
    env.ka.send_exc = RuntimeError("после нажатия")
    await env.engine.send_queued_once()
    await env.engine.resolve_stuck(1, was_sent=True)
    assert env.engine.status()["paused"] == [] and env.store.open_incidents() == []
    env.engine.pause("kleinanzeigen")   # пауза владельца ответом на вопрос не снимается
    env.store.set_status(2, "sending")
    await env.engine.resolve_stuck(2, was_sent=False)
    assert env.engine.status()["paused"] == ["kleinanzeigen"]


async def test_send_incident_closes_after_success_and_does_not_pause_on_restart():
    env = Env(mode="auto")
    await env.engine.process("kleinanzeigen", L())
    env.ka.send_exc = RuntimeError("сбой")
    await env.engine.send_queued_once()
    assert env.store.open_incidents() == [("kleinanzeigen", "send")]
    env2 = Env(mode="auto", store=env.store, clock=env.clock)   # перезапуск
    await env2.engine.startup()
    assert env2.engine.status()["paused"] == []   # «send» не блокирует площадку
    await env2.engine.process("kleinanzeigen", L(ext_id="2", street="Andere Str. 1", contact_name="Frau Weber"))
    await env2.engine.send_queued_once()
    assert len(env2.ka.sent) == 1 and env2.store.open_incidents() == []


async def test_concurrent_send_same_row_after_block_sends_once():
    env = Env(mode="auto")
    await env.engine.process("kleinanzeigen", L())
    env.ka.send_exc = BlockedError("captcha")
    calls = []
    orig = env.ka.send

    async def send(page, l, text, profile):
        calls.append(l.ext_id)
        await orig(page, l, text, profile)
    env.ka.send = send

    async def both(*coros):
        # площадка занята (идёт поиск): оба отправителя проходят проверку и ждут блокировку
        lock = env.browser.lock("kleinanzeigen")
        await lock.acquire()
        tasks = [asyncio.create_task(c) for c in coros]
        for _ in range(5):
            await asyncio.sleep(0)
        lock.release()
        await asyncio.gather(*tasks)

    await both(env.engine._send(1), env.engine._send(1))
    assert calls == ["1"] and env.store.get(1).status == "queued"
    env.ka.send_exc = None
    env.engine.resume("kleinanzeigen")
    await both(env.engine._send(1), env.engine.send_queued_once())
    assert len(calls) == 2 and len(env.ka.sent) == 1 and env.store.get(1).status == "sent"


async def test_skip_only_from_open_states(eng):
    i = sent_row(eng, "s1", "Gasse 1", T0)
    await eng.engine.skip(i)
    assert eng.store.get(i).status == "sent"
    j = eng.store.add(L(ext_id="2", street="Andere Str. 1"), 800)
    eng.store.set_status(j, "queued", reason="generating")
    await eng.engine.skip(j)
    assert eng.store.get(j).status == "queued"
    await eng.engine.process("kleinanzeigen", L(ext_id="3", street="Dritte Str. 3"))
    await eng.engine.skip(3)
    assert eng.store.get(3).status == "skipped"


async def test_profile_incomplete_no_letter():
    env = Env(mode="auto", profile={})
    assert await env.engine.process("kleinanzeigen", L()) == "seen"
    assert await env.engine.process("kleinanzeigen", L(ext_id="2", street="Andere Str. 1")) == "seen"
    assert env.ai.calls == [] and len(env.notifier.names("missing_profile")) == 1
    env.clock.now += timedelta(hours=24)
    await env.engine.process("kleinanzeigen", L(ext_id="3", street="Dritte Str. 3"))
    assert len(env.notifier.names("missing_profile")) == 2


async def test_seen_rows_processed_once_after_profile_filled():
    env = Env(mode="confirm", profile={})
    env.clock.now = T0 - timedelta(hours=2)
    old = env.store.add(L(ext_id="4", street="Alte Str. 4"), 800)                    # #1 — старше 24 ч
    env.store.set_status(old, "seen", reason="профиль не заполнен")
    env.clock.now = T0
    await env.engine.process("kleinanzeigen", L())                                   # #2
    await env.engine.process("wggesucht", L("wggesucht", "w1", street="Musterstr. 12"))  # #3 та же квартира
    env.clock.now = T0 + timedelta(hours=1)
    await env.engine.process("kleinanzeigen", L(ext_id="3", street="Dritte Str. 3"))  # #4
    env.clock.now = T0 + timedelta(hours=23)
    await env.engine.maintenance_once()          # профиль всё ещё пуст — ничего
    assert env.ai.calls == []
    env.cfg["profile"].update(PROFILE)
    await env.engine.profile_updated()
    await env.engine.maintenance_once()
    await env.engine.profile_updated()
    st = {i: env.store.get(i).status for i in (1, 2, 3, 4)}
    assert st[4] == "draft" and st[1] == "seen"
    assert sorted([st[2], st[3]]) == ["draft", "duplicate"]   # одна квартира — одно письмо
    assert len(env.ai.calls) == 2 and len(env.notifier.names("draft")) == 2


async def test_profile_updated_concurrent_calls_one_letter_per_row():
    env = Env(mode="confirm", profile={})
    await env.engine.process("kleinanzeigen", L())
    await env.engine.process("kleinanzeigen", L(ext_id="2", street="Andere Str. 1", contact_name="Frau Weber"))
    env.cfg["profile"].update(PROFILE)
    await asyncio.gather(env.engine.profile_updated(), env.engine.profile_updated(), env.engine.maintenance_once())
    assert [env.store.get(i).status for i in (1, 2)] == ["draft", "draft"]
    assert len(env.ai.calls) == 2 and len(env.notifier.names("draft")) == 2


async def test_profile_updated_recheck_never_matches_itself():
    env = Env(mode="confirm", profile={})
    await env.engine.process("kleinanzeigen", L())
    env.cfg["profile"].update(PROFILE)
    env.store.set_status(1, "queued", reason="generating")   # строку уже зарезервировал другой вызов
    assert await env.engine._recheck(1, env.engine._listings[1]) == "queued"
    row = env.store.get(1)
    assert (row.status, row.reason) == ("queued", "generating") and env.ai.calls == []


async def test_missing_profile_throttle_survives_restart():
    env = Env(profile={})
    await env.engine.process("kleinanzeigen", L())
    env2 = Env(profile={}, store=env.store, clock=env.clock)
    await env2.engine.process("kleinanzeigen", L(ext_id="2", street="Andere Str. 1"))
    assert env2.notifier.names("missing_profile") == []


async def test_digest_not_repeated_after_restart():
    env = Env()
    env.clock.now = datetime(2026, 10, 21, 18, 0)
    await env.engine.maintenance_once()
    env2 = Env(store=env.store, clock=env.clock)
    env2.clock.now = datetime(2026, 10, 21, 18, 30)
    await env2.engine.maintenance_once()
    assert len(env.notifier.names("digest")) == 1 and env2.notifier.names("digest") == []


async def test_contact_cap(eng):
    for k in range(3):
        sent_row(eng, f"s{k}", f"Gasse {k}", T0 - timedelta(days=k + 1), contact="Herr Müller")
    assert await eng.engine.process("kleinanzeigen", L()) == "filtered"
    assert eng.store.get(4).reason == "лимит контакта" and eng.ai.calls == []


async def test_contact_cap_counts_rows_queued_in_same_poll():
    env = Env(mode="auto")
    for i in range(5):
        await env.engine.process("kleinanzeigen", L(ext_id=str(i), street=f"Gasse {i}", contact_name="Vonovia GmbH",
                                                     area=50 + i * 10, warm_rent=600 + i * 50))
    for _ in range(5):
        await env.engine.send_queued_once()
        env.clock.now += timedelta(minutes=3)
    assert len(env.ai.calls) == 3 and len(env.ka.sent) == 3
    assert [r.reason for r in env.store.by_status("filtered")] == ["лимит контакта"] * 2


async def test_contact_cap_rechecked_at_send():
    env = Env(mode="auto")
    await env.engine.process("kleinanzeigen", L(contact_name="Frau Weber"))   # в очереди
    for k in range(3):   # тем временем владелец сам отправил этому контакту (например, одобрил черновики)
        sent_row(env, f"s{k}", f"Gasse {k}", T0 - timedelta(hours=k + 1), contact="Frau Weber")
    await env.engine.send_queued_once()
    row = env.store.get(1)
    assert env.ka.sent == [] and (row.status, row.reason) == ("filtered", "лимит контакта")


# ---------- обслуживание ----------

async def test_interrupted_generation_retried_once_by_maintenance():
    env = Env()
    env.ka.details = {"1": L(), "2": L(ext_id="2", street="Andere Str. 1")}
    env.clock.now = T0 - timedelta(hours=25)
    old = env.store.add(L(ext_id="2", street="Andere Str. 1"), 800)        # старше 24 ч — не трогаем
    env.store.set_status(old, "queued", reason="generating")
    env.clock.now = T0
    i = env.store.add(L(), 800)
    env.store.set_status(i, "queued", reason="generating")
    await env.engine.startup()                       # прервано при генерации → failed
    await env.engine.maintenance_once()
    await env.engine.maintenance_once()
    assert env.store.get(i).status == "draft" and env.store.get(old).status == "failed"
    assert len(env.ai.calls) == 1 and len(env.notifier.names("draft")) == 1


async def test_ai_failed_rows_retried_once_after_ai_incident_closed():
    env = Env()
    env.ai.exc = AIError("нейросеть недоступна")
    await env.engine.process("kleinanzeigen", L())                       # #1 failed, случай «ai» открыт
    await env.engine.maintenance_once()
    assert len(env.ai.calls) == 1                     # случай открыт — не повторяем
    env.ai.exc = None
    await env.engine.process("kleinanzeigen", L(ext_id="2", street="Andere Str. 1", contact_name="Frau Weber"))
    assert env.store.open_incidents() == []           # нейросеть ожила
    env.ai.exc = AIError("снова сбой")
    await env.engine.maintenance_once()               # повтор #1 — снова сбой
    assert env.store.get(1).status == "failed" and len(env.ai.calls) == 3
    env.ai.exc = None
    env.store.close_incidents("ai")
    await env.engine.maintenance_once()               # второй раз не повторяем
    assert env.store.get(1).status == "failed" and len(env.ai.calls) == 3


async def test_failed_retry_does_not_write_second_letter_for_same_flat():
    env = Env()
    env.ai.exc = AIError("нейросеть недоступна")
    await env.engine.process("kleinanzeigen", L())                      # #1 failed
    env.ai.exc = None
    env.wg.details["w1"] = L("wggesucht", "w1")
    assert await env.engine.process("wggesucht", L("wggesucht", "w1")) == "draft"   # та же квартира
    await env.engine.maintenance_once()
    assert env.store.get(1).status == "duplicate" and len(env.notifier.names("draft")) == 1


async def test_draft_reminded_once_then_expires_after_24h(eng):
    await eng.engine.process("kleinanzeigen", L())
    eng.clock.now = T0 + timedelta(hours=21, minutes=59)
    await eng.engine.maintenance_once()
    assert not eng.notifier.names("draft_reminder")
    eng.clock.now = T0 + timedelta(hours=22)
    await eng.engine.maintenance_once()
    await eng.engine.maintenance_once()
    assert eng.notifier.names("draft_reminder") == [("draft_reminder", [1], 2)]
    eng.clock.now = T0 + timedelta(hours=23, minutes=59)
    await eng.engine.maintenance_once()
    assert eng.store.get(1).status == "draft" and not eng.notifier.names("expire_draft")
    eng.clock.now = T0 + timedelta(hours=24)
    await eng.engine.maintenance_once()
    await eng.engine.maintenance_once()
    row = eng.store.get(1)
    assert row.status == "skipped" and row.reason == "черновик устарел"
    assert eng.notifier.names("expire_draft") == [("expire_draft", 1)] and not eng.notifier.names("remove_draft")


async def test_draft_age_survives_restart():
    env = Env()
    await env.engine.process("kleinanzeigen", L())
    env.clock.now = T0 + timedelta(hours=20)
    await env.engine.regenerate(1)   # новый вариант — срок с начала
    again = Engine(env.cfg, env.store, env.ai, {"kleinanzeigen": env.ka}, env.browser, env.notifier, clock=env.clock)
    env.clock.now = T0 + timedelta(hours=30)
    await again.maintenance_once()
    assert env.store.get(1).status == "draft"


async def test_restore_expired_draft():
    env = Env()
    await env.engine.process("kleinanzeigen", L())
    env.clock.now = T0 + timedelta(hours=24)
    await env.engine.maintenance_once()
    assert await env.engine.restore(1) == "draft"
    row = env.store.get(1)
    assert row.status == "draft" and len(env.ai.calls) == 2 and len(env.notifier.names("draft")) == 2
    env.clock.now = T0 + timedelta(hours=47)
    await env.engine.maintenance_once()   # новый срок — от возврата
    assert env.store.get(1).status == "draft"
    assert env.notifier.names("draft_reminder") == [("draft_reminder", [1], 2)]   # напоминание и для нового черновика


async def test_restore_checks_filters_and_sent_flat():
    env = Env(mode="auto")
    await env.engine.process("kleinanzeigen", L())
    env.store.set_status(1, "skipped", reason="черновик устарел")
    sent_row(env, "w1", "Musterstraße 12", T0, platform="wggesucht")
    assert await env.engine.restore(1) == "duplicate" and env.ka.sent == []


async def test_restore_load_failure_keeps_row():
    env = Env()
    await env.engine.process("kleinanzeigen", L())
    env.store.set_status(1, "skipped", reason="черновик устарел")
    env.engine._listings.clear()

    async def boom(page, l):
        raise RuntimeError("404")
    env.ka.fetch_detail = boom
    assert await env.engine.restore(1) == "load_failed"
    assert env.store.get(1).status == "skipped"


async def test_restore_only_skipped(eng):
    await eng.engine.process("kleinanzeigen", L())
    assert await eng.engine.restore(1) == "draft" and len(eng.ai.calls) == 1   # черновик и так есть — не трогаем


async def test_digest_once_at_20(eng):
    eng.store.add(L(), 800)
    eng.clock.now = datetime(2026, 10, 21, 17, 59)   # 19:59 в Бонне
    await eng.engine.maintenance_once()
    assert not eng.notifier.names("digest")
    eng.clock.now = datetime(2026, 10, 21, 18, 0)    # 20:00
    await eng.engine.maintenance_once()
    eng.clock.now = datetime(2026, 10, 21, 18, 1)    # 20:01
    await eng.engine.maintenance_once()
    digests = eng.notifier.names("digest")
    assert len(digests) == 1 and digests[0][1]["found"] == 1
    eng.clock.now = datetime(2026, 10, 22, 18, 0)    # следующий день
    await eng.engine.maintenance_once()
    assert len(eng.notifier.names("digest")) == 2


async def test_digest_counts_local_day():
    env = Env()
    env.clock.now = datetime(2026, 10, 20, 22, 30)   # 00:30 21.10 в Бонне — уже «сегодня»
    env.store.add(L(), 800)
    env.clock.now = datetime(2026, 10, 21, 18, 0)
    await env.engine.maintenance_once()
    assert env.notifier.names("digest")[0][1]["found"] == 1


# ---------- почта ----------

async def test_unmatched_reply_still_notified():
    unknown = Reply("kleinanzeigen", "999", "", "Frau X", "Hallo", "https://k/m")
    env = Env(mode="auto", mail=None)
    env.engine.mail = FakeMail([unknown])
    await env.engine.process("kleinanzeigen", L())
    await env.engine.send_queued_once()
    known = Reply("kleinanzeigen", "1", "", "", "Termin am Montag?", "https://k/m2")
    env.engine.mail.replies.append(known)
    await env.engine.mail_once()
    replies = env.notifier.names("reply")
    assert replies == [("reply", None, unknown), ("reply", 1, known)]
    assert env.store.day_stats(T0.date())["replies"] == 1


# ---------- действия из бота ----------

async def test_approve_with_edited_text_sends_that_text(eng):
    await eng.engine.process("kleinanzeigen", L())
    await eng.engine.approve(1, "Mein eigener Text.")
    assert eng.ka.sent == [("1", "Mein eigener Text.")]
    row = eng.store.get(1)
    assert row.status == "sent" and row.letter == "Mein eigener Text."
    await eng.engine.approve(1)   # повторное нажатие ничего не шлёт
    assert len(eng.ka.sent) == 1


async def test_approve_over_cap_stays_queued(eng):
    for k in range(15):
        sent_row(eng, f"s{k}", f"Gasse {k}", T0 - timedelta(minutes=10 + k), contact=f"Kontakt {k}")
    await eng.engine.process("kleinanzeigen", L())
    n = len(eng.notifier.calls)
    await eng.engine.approve(16)
    assert eng.store.get(16).status == "queued" and eng.ka.sent == [] and len(eng.notifier.calls) == n


async def test_regenerate_and_skip(eng):
    await eng.engine.process("kleinanzeigen", L())
    await eng.engine.regenerate(1, "kürzer")
    assert "Wunsch: kürzer" in eng.ai.calls[-1][1]
    assert len(eng.notifier.names("draft")) == 2 and eng.store.get(1).status == "draft"
    await eng.engine.skip(1)
    assert eng.store.get(1).status == "skipped"


async def test_platform_without_source_is_skipped():
    env = Env()
    env.cfg["platforms"]["immowelt"]["enabled"] = True
    env.ka.listings = [L()]
    task = asyncio.create_task(env.engine.run())
    for _ in range(50):
        await asyncio.sleep(0)
    await env.engine.stop()
    await asyncio.wait_for(task, 1)
    assert env.ka.searches == 1 and env.ka.filters is env.cfg["filters"]


async def spin(n=50):
    for _ in range(n):
        await asyncio.sleep(0)


async def run_briefly(env, steps=50):
    task = asyncio.create_task(env.engine.run())
    await spin(steps)
    return task


async def stop_run(env, task):
    await env.engine.stop()
    await asyncio.wait_for(task, 1)


def selective_env(only):
    env = Env(mode="auto")
    env.engine = Engine(env.cfg, env.store, env.ai, {"kleinanzeigen": env.ka, "wggesucht": env.wg},
                        env.browser, env.notifier, clock=env.clock, only=only)
    return env


async def test_selective_run_polls_and_sends_only_chosen_platform():
    env = selective_env({"wggesucht"})
    env.store.add(L(ext_id="q"), 800)
    env.store.set_status(1, "queued", letter=letter(0))   # письмо Kleinanzeigen ждёт — его площадку не трогаем
    task = await run_briefly(env)
    await stop_run(env, task)
    assert env.wg.searches == 1 and env.ka.searches == 0
    assert env.ka.sent == [] and env.store.get(1).status == "queued"


async def test_disabled_platform_sleeps_until_switched_on():
    env = Env()
    env.cfg["platforms"]["kleinanzeigen"]["enabled"] = False
    task = await run_briefly(env)
    assert env.ka.searches == 0 and env.wg.searches == 1
    env.cfg["platforms"]["kleinanzeigen"]["enabled"] = True
    env.engine.wake("kleinanzeigen")   # переключатель в окне: опрос сразу, без ожидания интервала
    await spin()
    await stop_run(env, task)
    assert env.ka.searches == 1 and env.wg.searches == 1


async def test_activate_adds_platform_to_selective_run():
    env = selective_env({"wggesucht"})
    env.cfg["platforms"]["kleinanzeigen"]["enabled"] = False   # по одной запускается и выключенная
    task = await run_briefly(env)
    env.engine.activate("kleinanzeigen")
    await spin()
    await stop_run(env, task)
    assert env.ka.searches == 1 and env.engine.active("kleinanzeigen")


async def test_switch_off_during_poll_stops_processing():
    env = Env()
    env.ka.listings = [L(ext_id="1"), L(ext_id="2", street="Gasse 2")]
    env.cfg["platforms"]["kleinanzeigen"]["enabled"] = False
    await env.engine._process_all("kleinanzeigen", env.ka.listings)
    assert not env.store.has("kleinanzeigen", "1")


async def test_successful_send_notifies_sent_once():
    env = Env(mode="auto")
    five_queued(env)
    await env.engine.send_queued_once()
    assert env.store.get(1).status == "sent" and env.notifier.names("sent") == [("sent", 1)]


async def test_already_contacted_marks_sent_without_typing():
    from sources.base import AlreadyContactedError
    env = Env(mode="auto")
    five_queued(env)
    env.ka.send_exc = AlreadyContactedError("переписка уже есть")
    await env.engine.send_queued_once()
    row = env.store.get(1)
    assert row.status == "sent" and "переписка" in row.reason
    assert env.notifier.names("sent") == [("sent", 1)] and env.notifier.names("ask_stuck") == []
    assert env.engine.status()["paused"] == []


async def test_resolve_stuck_sent_notifies_sent():
    env = Env(mode="auto")
    five_queued(env)
    env.ka.send_exc = RuntimeError("после нажатия")
    await env.engine.send_queued_once()
    await env.engine.resolve_stuck(1, was_sent=True)
    assert env.notifier.names("sent") == [("sent", 1)]


# ---------- журнал ----------

async def test_poll_logs_summary(caplog):
    caplog.set_level("INFO", logger="wohnung")
    env = Env()
    env.ka.listings = [L(ext_id="1"), L(ext_id="2", street="Andere Str. 5", warm_rent=2000)]
    await env.engine.poll_once("kleinanzeigen")
    caplog.clear()
    env.ka.listings.append(L(ext_id="3", street="Dritte Str. 7"))
    await env.engine.poll_once("kleinanzeigen")
    summary = [r.getMessage() for r in caplog.records if "опрос за" in r.getMessage()]
    assert len(summary) == 1
    assert "в выдаче 3: известные 2, draft 1" in summary[0]


class SnapPage:
    url = "https://kleinanzeigen/fail"

    async def content(self):
        return "<html>fail</html>"

    async def screenshot(self, path, full_page):
        open(path, "wb").write(b"png")


async def test_unknown_send_error_saves_page_snapshot(tmp_path, caplog):
    caplog.set_level("INFO", logger="wohnung")
    env = Env(mode="auto")
    env.engine.debug_dir = tmp_path

    async def page(platform):
        return SnapPage()
    env.browser.page = page
    env.ka.send_exc = RuntimeError("после нажатия")
    await env.engine.process("kleinanzeigen", L())
    await env.engine.send_queued_once()
    names = sorted(p.suffix for p in tmp_path.iterdir())
    assert names == [".html", ".png"]
    assert any("https://kleinanzeigen/fail" in r.getMessage() for r in caplog.records)


async def test_daily_cap_logged_once_per_day(caplog):
    caplog.set_level("INFO", logger="wohnung")
    env = Env(mode="auto")
    env.cfg["platforms"]["kleinanzeigen"]["daily_cap"] = 0
    await env.engine.process("kleinanzeigen", L())
    for _ in range(3):
        await env.engine.send_queued_once()
        env.clock.now += timedelta(seconds=10)
    assert sum("дневной лимит" in r.getMessage() for r in caplog.records) == 1


# ---------- принудительный проход (кнопка «Пройти заново») ----------

def rescan_env(mode="confirm", listings=None):
    env = Env(mode)
    env.ka.listings = listings if listings is not None else [L()]
    return env


async def test_rescan_redrafts_skipped_and_removes_old_message():
    env = rescan_env()
    await env.engine.poll_once("kleinanzeigen")
    await env.engine.skip(1)
    await env.engine.poll_once("kleinanzeigen")   # обычный опрос: пропущенное не трогаем
    assert env.store.get(1).status == "skipped" and len(env.ai.calls) == 1
    assert env.engine.rescan() == ["kleinanzeigen", "wggesucht"]
    await env.engine.poll_once("kleinanzeigen")
    row = env.store.get(1)
    assert row.status == "draft" and len(env.ai.calls) == 2
    assert env.notifier.names("remove_draft") == [("remove_draft", 1)]
    assert row.draft_msg_id   # новое сообщение с кнопками
    await env.engine.poll_once("kleinanzeigen")   # флаг одноразовый
    assert len(env.ai.calls) == 2


async def test_rescan_replaces_pending_draft():
    env = rescan_env()
    await env.engine.poll_once("kleinanzeigen")
    old = env.store.get(1).draft_msg_id
    env.engine.rescan()
    await env.engine.poll_once("kleinanzeigen")
    row = env.store.get(1)
    assert row.status == "draft" and row.draft_msg_id != old and len(env.notifier.names("draft")) == 2
    assert env.notifier.names("remove_draft") == [("remove_draft", 1)]


async def test_rescan_always_drafts_even_in_auto_mode():
    env = rescan_env("auto")
    await env.engine.poll_once("kleinanzeigen")
    env.store.set_status(1, "skipped")
    env.engine.rescan()
    await env.engine.poll_once("kleinanzeigen")
    assert env.store.get(1).status == "draft" and env.ka.sent == []


async def test_rescan_new_listing_is_drafted_in_auto_mode():
    env = rescan_env("auto")
    env.engine.rescan()
    await env.engine.poll_once("kleinanzeigen")
    assert env.store.get(1).status == "draft"


async def test_rescan_never_touches_sent_or_queued():
    env = rescan_env(listings=[L(ext_id="s"), L(ext_id="q", street="Gasse 2", contact_name="K")])
    sent_row(env, "s", "Musterstraße 12", T0 - timedelta(days=1))
    q = env.store.add(L(ext_id="q", street="Gasse 2", contact_name="K"), 800)
    env.store.set_status(q, "queued", letter="Text")
    env.engine.rescan()
    await env.engine.poll_once("kleinanzeigen")
    assert env.store.get(1).status == "sent" and env.store.get(q).status == "queued"
    assert env.ai.calls == [] and env.notifier.calls == []


async def test_rescan_skips_flat_already_sent_on_other_platform():
    env = rescan_env()
    sent_row(env, "w1", "Musterstraße 12", T0 - timedelta(days=1), platform="wggesucht")
    await env.engine.poll_once("kleinanzeigen")
    assert env.store.get(2).status == "duplicate"
    env.engine.rescan()
    await env.engine.poll_once("kleinanzeigen")
    assert env.store.get(2).status == "duplicate" and env.ai.calls == []


async def test_rescan_applies_current_filters():
    env = rescan_env(listings=[L(warm_rent=950)])
    await env.engine.poll_once("kleinanzeigen")
    assert env.store.get(1).status == "filtered"
    env.engine.rescan()
    await env.engine.poll_once("kleinanzeigen")   # фильтры прежние — снова отсеяно, без письма
    assert env.store.get(1).status == "filtered" and env.ai.calls == []
    env.cfg["filters"]["max_warm"] = 1000
    env.engine.rescan()
    await env.engine.poll_once("kleinanzeigen")
    assert env.store.get(1).status == "draft"


async def test_rescan_detail_failure_restores_status():
    env = rescan_env()
    await env.engine.poll_once("kleinanzeigen")
    await env.engine.skip(1)

    async def boom(page, l):
        raise RuntimeError("сеть")
    env.ka.fetch_detail = boom
    env.engine.rescan()
    await env.engine.poll_once("kleinanzeigen")
    assert env.store.get(1).status == "skipped"


async def test_rescan_skips_paused_and_inactive_platforms():
    env = rescan_env()
    env.cfg["platforms"]["wggesucht"]["enabled"] = False
    env.engine.pause("kleinanzeigen")
    assert env.engine.rescan() == []


# ---------- медленная страница при отправке ----------

def timeout_error():
    try:
        try:
            raise TimeoutError("Page.goto: Timeout 30000ms exceeded.")
        except TimeoutError as e:
            raise NotSubmittedError(f"kleinanzeigen: форма не заполнена: {e}") from e
    except NotSubmittedError as e:
        return e


async def test_send_timeout_retries_later_without_pause():
    env = Env(mode="auto")
    five_queued(env)
    calls, fail = [], [3]

    async def send(page, l, text, profile):
        calls.append(l.ext_id)
        if fail[0]:
            fail[0] -= 1
            raise timeout_error()
    env.ka.send = send
    await env.engine.send_queued_once()
    assert calls == ["0"] and env.store.get(1).status == "queued"
    assert env.engine.status()["paused"] == [] and env.notifier.calls == []
    env.clock.now += timedelta(minutes=4)
    await env.engine.send_queued_once()   # повтор не раньше чем через 5 мин
    assert calls == ["0"]
    for _ in range(2):
        env.clock.now += timedelta(minutes=5)
        await env.engine.send_queued_once()
    assert calls == ["0", "0", "0"] and env.engine.status()["paused"] == []
    assert env.notifier.calls == [("incident", "kleinanzeigen", "slow")]   # после 3 подряд — одно сообщение
    env.clock.now += timedelta(minutes=5)
    await env.engine.send_queued_once()
    assert env.store.get(1).status == "sent"
    assert ("kleinanzeigen", "slow") not in env.store.open_incidents()


async def test_digest_counts_expired_and_skipped_by_event_time(pauses):
    env = Env()
    env.ka.listings = [L(), L(ext_id="2", street="Gasse 2", contact_name="K2"), L(ext_id="3", street="Gasse 3", contact_name="K3")]
    env.clock.now = datetime(2026, 10, 20, 15, 0)   # черновики созданы вчера
    await env.engine.poll_once("kleinanzeigen")
    await env.engine.skip(3)
    env.clock.now = datetime(2026, 10, 21, 15, 0)   # 17:00 в Бонне: вчерашние устарели сегодня
    await env.engine.skip(2)
    await env.engine.maintenance_once()
    env.clock.now = datetime(2026, 10, 21, 18, 0)   # 20:00 в Бонне — сводка
    await env.engine.maintenance_once()
    stats = env.notifier.names("digest")[0][1]
    assert stats["expired"] == 1 and stats["skipped"] == 1 and stats["drafts_waiting"] == 0


# ---------- несколько страниц выдачи ----------

class PagedSource(FakeSource):
    def __init__(self, name, pages):
        super().__init__(name, pages[0])
        self.pages, self.asked = pages, []

    async def search_more(self, page, n):
        self.asked.append(n)
        return list(self.pages[n - 1]) if n <= len(self.pages) else list(self.pages[0])   # за концом — снова первая


STREETS = ("Ahornweg", "Birkenweg", "Eichenweg", "Lindenweg", "Buchenweg", "Erlenweg", "Tannenweg", "Kiefernweg")


def pages_of(*sizes):
    out, k = [], 0
    for size in sizes:
        out.append([L(ext_id=str(k + i), street=f"{STREETS[k + i]} 5", contact_name=f"K {k + i}")
                    for i in range(size)])
        k += size
    return out


async def test_next_pages_only_while_all_new(pauses):
    env = Env()
    env.ka = env.engine.sources["kleinanzeigen"] = PagedSource("kleinanzeigen", pages_of(2, 2, 2, 2))
    await env.engine.poll_once("kleinanzeigen")   # первый запуск: все новые — 3 страницы, не больше
    assert env.ka.asked == [2, 3] and len(env.store.by_status("draft")) == 6
    await env.engine.poll_once("kleinanzeigen")   # первая страница знакома — дальше не идём
    assert env.ka.asked == [2, 3]


async def test_rescan_reads_next_pages(pauses):
    env = Env()
    env.ka = env.engine.sources["kleinanzeigen"] = PagedSource("kleinanzeigen", pages_of(2, 2))
    await env.engine.poll_once("kleinanzeigen")
    assert env.ka.asked == [2, 3]   # третьей нет — сайт вернул первую, новых нет
    env.engine.rescan()
    await env.engine.poll_once("kleinanzeigen")
    assert env.ka.asked == [2, 3, 2, 3]


async def test_next_page_failure_keeps_first(pauses):
    env = Env()
    env.ka = env.engine.sources["kleinanzeigen"] = PagedSource("kleinanzeigen", pages_of(2, 2))

    async def boom(page, n):
        raise RuntimeError("timeout")
    env.ka.search_more = boom
    await env.engine.poll_once("kleinanzeigen")
    assert len(env.store.by_status("draft")) == 2


async def test_restore_on_captcha_pauses_and_keeps_row():
    env = Env()
    await env.engine.process("kleinanzeigen", L())
    env.store.set_status(1, "skipped", reason="черновик устарел")
    env.engine._listings.clear()

    async def captcha(page, l):
        raise BlockedError("captcha")
    env.ka.fetch_detail = captcha
    assert await env.engine.restore(1) == "blocked"
    assert env.store.get(1).status == "skipped" and "kleinanzeigen" in env.engine.status()["paused"]


async def test_captcha_on_next_page_pauses(pauses):
    env = Env()
    env.ka = env.engine.sources["kleinanzeigen"] = PagedSource("kleinanzeigen", pages_of(2, 2))

    async def captcha(page, n):
        raise BlockedError("captcha")
    env.ka.search_more = captcha
    await env.engine.poll_once("kleinanzeigen")
    assert "kleinanzeigen" in env.engine.status()["paused"] and env.store.by_status("draft") == []
