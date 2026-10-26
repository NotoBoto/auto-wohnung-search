import asyncio
import json
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, NetworkError

import core
from bot import OwnerBot, TelegramNotifier
from mail import Reply
from models import Listing
from store import Store

OWNER, STRANGER = 111, 222


class FakeBot:
    def __init__(self, fail_times=0, exc=NetworkError):
        self.sent, self.deleted, self.fail, self.exc, self.calls = [], [], fail_times, exc, 0

    async def send_message(self, **kw):
        self.calls += 1
        if self.fail > 0:
            self.fail -= 1
            raise self.exc("boom")
        self.sent.append(kw)
        return SimpleNamespace(message_id=100 + len(self.sent))

    async def delete_message(self, **kw):
        self.deleted.append(kw)

    async def edit_message_text(self, **kw):
        self.edited = getattr(self, "edited", []) + [kw]


def make_store(tmp_path):
    s = Store(tmp_path / "t.db")
    id = s.add(Listing("kleinanzeigen", "1", "https://x.test/1", "Titel"), 800)
    s.set_status(id, "draft", letter="Hallo")
    return s, id


def notifier(bot, cfg=None):
    sleeps = []

    async def sleep(s):
        sleeps.append(s)
    n = TelegramNotifier(bot, cfg or {"owner_chat_id": OWNER, "bot_token": "T"}, sleep=sleep)
    n.slept = sleeps
    return n


def buttons(msg):
    return [b.callback_data for r in msg["reply_markup"].inline_keyboard for b in r]


# ---------- TelegramNotifier ----------

async def test_draft_buttons_and_message_id(tmp_path):
    store, id = make_store(tmp_path)
    bot = FakeBot()
    n = notifier(bot)
    mid = await n.draft(store.get(id), "Hallo", True)
    assert mid == 101
    assert buttons(bot.sent[0]) == [f"send:{id}", f"edit:{id}", f"regen:{id}", f"skip:{id}"]
    assert bot.sent[0]["chat_id"] == OWNER and bot.sent[0]["disable_notification"] is False


async def test_retry_5_15_60_then_gives_up_returns_zero(tmp_path):
    store, id = make_store(tmp_path)
    bot = FakeBot(fail_times=99)
    n = notifier(bot)
    assert await n.draft(store.get(id), "Hallo", False) == 0
    assert n.slept == [5, 15, 60] and bot.calls == 4


async def test_retry_succeeds_after_failures(tmp_path):
    store, id = make_store(tmp_path)
    bot = FakeBot(fail_times=2)
    n = notifier(bot)
    assert await n.draft(store.get(id), "Hallo", False) == 101
    assert n.slept == [5, 15]


async def test_non_network_error_not_retried(tmp_path):
    store, id = make_store(tmp_path)
    bot = FakeBot(fail_times=5, exc=BadRequest)
    n = notifier(bot)
    assert await n.draft(store.get(id), "Hallo", False) == 0
    assert bot.calls == 1 and n.slept == []


async def test_unbound_owner_sends_nothing(tmp_path):
    store, id = make_store(tmp_path)
    bot = FakeBot()
    n = notifier(bot, {"owner_chat_id": 0, "bot_token": "T"})
    assert await n.draft(store.get(id), "Hallo", False) == 0 and bot.calls == 0


async def test_queued_notifications_sound_and_digest_silent(tmp_path):
    store, id = make_store(tmp_path)
    row = store.get(id)
    bot = FakeBot()
    n = notifier(bot)
    await n.incident("wggesucht", "captcha", "d")
    await n.digest({"found": 1, "sent": 0, "filtered": {}, "duplicates": 0, "replies": 0, "failed": 0})
    await n.missing_profile()
    await n.ask_duplicate(row, row)
    await n.ask_stuck(row)
    await n.reply(None, Reply("wggesucht", "9", "u", "c", "exc", "l"))
    await n.drain()
    await n.stop()
    assert [m["disable_notification"] for m in bot.sent] == [False, True, False, False, False, False]
    assert buttons(bot.sent[3]) == [f"dup:{id}:send", f"dup:{id}:skip"]
    assert buttons(bot.sent[4]) == [f"stuck:{id}:sent", f"stuck:{id}:resend"]
    assert "не нашёл, к какому объявлению" in bot.sent[5]["text"]


async def test_queued_survives_network_outage():
    bot = FakeBot(fail_times=2)
    n = notifier(bot)
    await n.incident("kleinanzeigen", "blocked", "d")
    await n.drain()
    await n.stop()
    assert len(bot.sent) == 1 and n.slept == [5, 15]


async def test_remove_draft_deletes_message(tmp_path):
    store, id = make_store(tmp_path)
    store.set_status(id, "draft", draft_msg_id=555)
    bot = FakeBot()
    n = notifier(bot)
    await n.remove_draft(store.get(id))
    await n.drain()
    await n.stop()
    assert bot.deleted == [{"chat_id": OWNER, "message_id": 555}]


# ---------- OwnerBot ----------

class FakeEngine:
    def __init__(self, store):
        self.store, self.calls = store, []

    def mode_for(self, p): return "confirm"
    def pause(self, p): self.calls.append(("pause", p))
    def resume(self, p): self.calls.append(("resume", p))
    def status(self): return {"paused": [], "paused_all": False, "modes": {}, "incidents": [], "drafts": 0,
                              "queued": 0, "today": {}}

    async def profile_updated(self): self.calls.append(("profile_updated",))

    async def approve(self, id, text=None):
        self.calls.append(("approve", id, text))
        self.store.set_status(id, "queued")

    async def regenerate(self, id, wish=""): self.calls.append(("regenerate", id, wish))

    async def skip(self, id):
        self.calls.append(("skip", id))
        self.store.set_status(id, "skipped")

    async def resolve_duplicate(self, id, send): self.calls.append(("dup", id, send))
    async def resolve_stuck(self, id, was_sent): self.calls.append(("stuck", id, was_sent))


class Msg:
    def __init__(self, text="", reply_to=None):
        self.text, self.reply_to_message, self.replies, self.deleted, self.sent_msgs = text, reply_to, [], False, []

    async def reply_text(self, t):
        self.replies.append(t)
        m = Msg(t)
        self.sent_msgs.append(m)
        return m

    async def delete(self): self.deleted = True


class Message:
    def __init__(self, text, q):
        self.text, self.q = text, q

    async def edit_text(self, text, reply_markup=None):
        if self.q.fail_text:
            raise BadRequest("too long")
        self.q.edits.append((text, reply_markup))

    async def edit_reply_markup(self, reply_markup=None):
        self.q.markup_only += 1


class Query:
    def __init__(self, data, text="draft text", fail_text=False):
        self.data, self.edits, self.answered, self.fail_text, self.markup_only = data, [], False, fail_text, 0
        self.message = Message(text, self)

    async def answer(self): self.answered = True


def upd(chat, text="", query=None, reply_to=None):
    m = Msg(text, reply_to)
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat), effective_message=m, callback_query=query), m


def ctx(*args):
    return SimpleNamespace(args=list(args))


@pytest.fixture
def env(tmp_path, monkeypatch):
    saved = []
    monkeypatch.setattr(core, "save_config", lambda cfg: saved.append(json.loads(json.dumps(cfg))))
    monkeypatch.setattr(core, "BASE", tmp_path)
    cfg = json.loads(json.dumps(core.DEFAULTS))
    cfg["owner_chat_id"] = OWNER
    store, id = make_store(tmp_path)
    engine = FakeEngine(store)
    return SimpleNamespace(cfg=cfg, store=store, id=id, engine=engine, saved=saved, bot=OwnerBot(cfg, engine, store))


async def test_stranger_gets_no_reply_from_any_handler(env):
    for name, args in [("cmd_start", []), ("cmd_status", []), ("cmd_pause", []), ("cmd_mode", ["auto"]),
                       ("cmd_filters", ["max_warm", "1"]), ("cmd_profile", []), ("cmd_prompt", []),
                       ("cmd_cancel", []), ("on_text", [])]:
        u, m = upd(STRANGER, "hi")
        await getattr(env.bot, name)(u, ctx(*args))
        assert m.replies == [], name
    q = Query(f"send:{env.id}")
    u, m = upd(STRANGER, query=q)
    await env.bot.on_callback(u, ctx())
    assert env.engine.calls == [] and not q.answered and q.edits == [] and m.replies == []
    assert env.cfg["mode"] == "confirm" and env.cfg["owner_chat_id"] == OWNER and env.saved == []


async def test_first_start_binds_owner_then_stranger_ignored(env):
    env.cfg["owner_chat_id"] = 0
    u, m = upd(OWNER)
    await env.bot.cmd_start(u, ctx())
    assert env.cfg["owner_chat_id"] == OWNER and env.saved[-1]["owner_chat_id"] == OWNER and m.replies
    u2, m2 = upd(STRANGER)
    await env.bot.cmd_start(u2, ctx())
    assert env.cfg["owner_chat_id"] == OWNER and m2.replies == []


async def test_unbound_other_handlers_ignored(env):
    env.cfg["owner_chat_id"] = 0
    u, m = upd(STRANGER)
    await env.bot.cmd_status(u, ctx())
    assert m.replies == []


async def test_send_button_approves_and_removes_keyboard(env):
    q = Query(f"send:{env.id}")
    u, _ = upd(OWNER, query=q)
    await env.bot.on_callback(u, ctx())
    # сразу видно, что отправка идёт, ещё до конца медленной отправки
    assert q.answered and q.edits[0][1] is None and "Отправляю" in q.edits[0][0]
    await env.bot.drain()
    assert env.engine.calls == [("approve", env.id, None)]
    text, markup = q.edits[-1]   # черновик свёрнут в одну строку-итог
    assert markup is None and "draft text" not in text and "Titel" in text and f"#{env.id}" in text
    assert "очереди" in text or "Отправл" in text


async def test_skip_button(env):
    q = Query(f"skip:{env.id}")
    await env.bot.on_callback(upd(OWNER, query=q)[0], ctx())
    assert env.engine.calls == [("skip", env.id)] and "Пропущено" in q.edits[0][0] and q.edits[0][1] is None
    assert "draft text" not in q.edits[0][0]


@pytest.mark.parametrize("data,expected", [
    ("dup:{id}:send", ("dup", True)), ("dup:{id}:skip", ("dup", False)),
    ("stuck:{id}:sent", ("stuck", True)), ("stuck:{id}:resend", ("stuck", False)),
    ("regen:{id}", ("regenerate", ""))])
async def test_other_buttons_route(env, data, expected):
    q = Query(data.format(id=env.id))
    await env.bot.on_callback(upd(OWNER, query=q)[0], ctx())
    assert env.engine.calls == [(expected[0], env.id, expected[1])]
    assert q.edits and q.edits[0][1] is None


async def test_button_error_replies_short(env):
    q = Query("send:9999")   # такой строки нет
    u, m = upd(OWNER, query=q)
    await env.bot.on_callback(u, ctx())
    assert m.replies and m.replies[0].startswith("Ошибка") and q.edits == []


async def test_edit_flow_plain_text_approves(env):
    q = Query(f"edit:{env.id}")
    u, m = upd(OWNER, query=q)
    await env.bot.on_callback(u, ctx())
    prompt = m.sent_msgs[0]
    assert env.bot.pending_edit[OWNER][:2] == (env.id, q.message) and q.edits == []   # кнопки остаются до итога
    u2, answer = upd(OWNER, "Neuer Text")
    await env.bot.on_text(u2, ctx())
    assert "Отправляю" in q.edits[0][0] and env.bot.pending_edit == {}
    await env.bot.drain()
    assert env.engine.calls == [("approve", env.id, "Neuer Text")]
    text, markup = q.edits[-1]   # исходный черновик свёрнут в строку-итог
    assert markup is None and "draft text" not in text and "очереди" in text
    # подсказка бота и ответ владельца удалены, лишних ответов нет
    assert prompt.deleted and answer.deleted and answer.replies == []


async def test_edit_flow_bang_regenerates_and_cancel(env):
    q = Query(f"edit:{env.id}")
    u, m = upd(OWNER, query=q)
    await env.bot.on_callback(u, ctx())
    u2, answer = upd(OWNER, "!короче")
    await env.bot.on_text(u2, ctx())
    assert env.engine.calls == [("regenerate", env.id, "короче")]
    assert q.edits[0][1] is None and "новый вариант" in q.edits[0][0]
    assert answer.replies == [] and answer.deleted and m.sent_msgs[0].deleted   # без «Готовлю новый вариант.»
    await env.bot.on_callback(upd(OWNER, query=Query(f"edit:{env.id}"))[0], ctx())
    await env.bot.cmd_cancel(upd(OWNER)[0], ctx())
    await env.bot.on_text(upd(OWNER, "ignored")[0], ctx())
    assert len(env.engine.calls) == 1


async def test_mode_global_and_platform(env):
    await env.bot.cmd_mode(upd(OWNER)[0], ctx("auto"))
    assert env.cfg["mode"] == "auto" and env.saved[-1]["mode"] == "auto"
    await env.bot.cmd_mode(upd(OWNER)[0], ctx("confirm", "wggesucht"))
    assert env.cfg["platforms"]["wggesucht"]["mode"] == "confirm" and env.cfg["mode"] == "auto"
    u, m = upd(OWNER)
    await env.bot.cmd_mode(u, ctx("auto", "nowhere"))
    assert "Нет такой площадки" in m.replies[0]
    u, m = upd(OWNER)
    await env.bot.cmd_mode(u, ctx("weird"))
    assert env.cfg["mode"] == "auto" and m.replies


async def test_filters_show_and_update(env):
    u, m = upd(OWNER)
    await env.bot.cmd_filters(u, ctx())
    assert "max_warm = 900" in m.replies[0] and env.saved == []
    same = env.cfg["filters"]
    await env.bot.cmd_filters(upd(OWNER)[0], ctx("max_warm", "950"))
    assert env.cfg["filters"] is same and same["max_warm"] == 950 and env.saved
    u, m = upd(OWNER)
    await env.bot.cmd_filters(u, ctx("max_warm", "много"))
    assert same["max_warm"] == 950 and "не число" in m.replies[0]


async def test_pause_resume(env):
    await env.bot.cmd_pause(upd(OWNER)[0], ctx())
    await env.bot.cmd_resume(upd(OWNER)[0], ctx("wggesucht"))
    assert env.engine.calls == [("pause", None), ("resume", "wggesucht")]


async def test_profile_wizard_completes_and_notifies_engine(env):
    answers = ["Herr", "Max", "Muster", "2", "-", "-", "sofort", "-", "-", "-", "-", "-", "-", "Ingenieur"]
    u, m = upd(OWNER)
    await env.bot.cmd_profile(u, ctx())
    assert "Herr или Frau" in m.replies[0]
    for a in answers[:-1]:
        await env.bot.on_text(upd(OWNER, a)[0], ctx())
    assert env.engine.calls == []   # профиль ещё не полный
    u, m = upd(OWNER, answers[-1])
    await env.bot.on_text(u, ctx())
    assert env.cfg["profile"]["persons"] == 2 and "occupation" not in env.cfg["profile"]
    assert "заполнен" in m.replies[-1] and ("profile_updated",) in env.engine.calls
    assert env.bot.wizard == {}


async def test_profile_wizard_repeats_question_on_bad_answer(env):
    await env.bot.cmd_profile(upd(OWNER)[0], ctx())
    u, m = upd(OWNER, "Дядя")
    await env.bot.on_text(u, ctx())
    assert "Herr" in m.replies[0] and env.cfg["profile"] == {}
    await env.bot.on_text(upd(OWNER, "Frau")[0], ctx())
    assert env.cfg["profile"]["salutation"] == "Frau"


async def test_profile_single_field_hide_show_and_view(env):
    env.cfg["profile"] = {"salutation": "Herr", "first_name": "Max", "last_name": "M", "persons": 2,
                          "move_in": "sofort", "about": "x"}
    u, m = upd(OWNER)
    await env.bot.cmd_profile(u, ctx())
    assert "Max" in m.replies[0]   # заполнен — показываем
    await env.bot.cmd_profile(upd(OWNER)[0], ctx("persons", "3"))
    assert env.cfg["profile"]["persons"] == 3
    await env.bot.cmd_profile(upd(OWNER)[0], ctx("hide", "about"))
    assert env.cfg["profile"]["hidden"] == ["about"]
    await env.bot.cmd_profile(upd(OWNER)[0], ctx("show", "about"))
    assert env.cfg["profile"]["hidden"] == []
    await env.bot.cmd_profile(upd(OWNER)[0], ctx("phone"))
    await env.bot.on_text(upd(OWNER, "0228 123")[0], ctx())
    assert env.cfg["profile"]["phone"] == "0228 123"
    assert ("profile_updated",) in env.engine.calls


async def test_prompt_show_and_replace(env, tmp_path):
    (tmp_path / env.cfg["prompt_file"]).write_text("alt", encoding="utf-8")
    u, m = upd(OWNER)
    await env.bot.cmd_prompt(u, ctx())
    assert m.replies == ["alt"]
    u, m = upd(OWNER, reply_to=SimpleNamespace(text="neu"))
    await env.bot.cmd_prompt(u, ctx())
    assert (tmp_path / env.cfg["prompt_file"]).read_text(encoding="utf-8") == "neu"


async def test_edit_text_failure_falls_back_to_removing_markup(env):
    q = Query(f"send:{env.id}", fail_text=True)
    await env.bot.on_callback(upd(OWNER, query=q)[0], ctx())
    assert q.edits == [] and q.markup_only == 1


async def test_callback_without_message_does_not_crash(env):
    q = Query(f"skip:{env.id}")
    q.message = None
    await env.bot.on_callback(upd(OWNER, query=q)[0], ctx())
    assert env.engine.calls == [("skip", env.id)]


async def test_profile_bad_inline_value_keeps_field_pending(env):
    u, m = upd(OWNER)
    await env.bot.cmd_profile(u, ctx("persons", "abc"))
    assert env.bot.wizard == {OWNER: ["persons"]} and env.cfg["profile"] == {}
    await env.bot.on_text(upd(OWNER, "3")[0], ctx())
    assert env.cfg["profile"]["persons"] == 3


async def test_dup_send_outcome_is_readable(env):
    env.store.set_status(env.id, "queued")
    q = Query(f"dup:{env.id}:send")
    await env.bot.on_callback(upd(OWNER, query=q)[0], ctx())
    assert "очереди" in q.edits[0][0] and "queued" not in q.edits[0][0]


async def test_new_draft_replaces_old_draft_message(tmp_path):
    store, id = make_store(tmp_path)
    store.set_status(id, "draft", draft_msg_id=555)
    bot = FakeBot()
    await notifier(bot).draft(store.get(id), "Neu", False)
    assert bot.deleted == [{"chat_id": OWNER, "message_id": 555}] and len(bot.sent) == 1


async def test_sent_collapses_draft_to_one_line(tmp_path):
    store, id = make_store(tmp_path)
    store.set_status(id, "sent", draft_msg_id=555)
    bot = FakeBot()
    n = notifier(bot)
    await n.sent(store.get(id))
    await n.drain()
    await n.stop()
    (e,) = bot.edited
    assert e["message_id"] == 555 and "Отправлено" in e["text"] and "Titel" in e["text"] and "Hallo" not in e["text"]


async def test_sent_without_draft_message_sends_nothing(tmp_path):
    store, id = make_store(tmp_path)
    bot = FakeBot()
    n = notifier(bot)
    await n.sent(store.get(id))   # авто-режим: черновика не было — отдельного сообщения нет
    await n.drain()
    await n.stop()
    assert bot.sent == [] and not getattr(bot, "edited", [])


async def test_send_runs_in_background_and_reports_result(env):
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_approve(id, text=None):
        started.set()
        await release.wait()   # медленная печать письма на сайте
        env.store.set_status(id, "sent")
    env.engine.approve = slow_approve
    q = Query(f"send:{env.id}")
    await env.bot.on_callback(upd(OWNER, query=q)[0], ctx())   # не ждёт конца отправки
    await asyncio.sleep(0)
    assert started.is_set() and len(q.edits) == 1 and "Отправляю" in q.edits[0][0]
    release.set()
    await env.bot.drain()
    assert "Отправлено" in q.edits[-1][0]


async def test_send_button_on_processed_draft_does_nothing(env):
    env.store.set_status(env.id, "sent")
    q = Query(f"send:{env.id}")
    await env.bot.on_callback(upd(OWNER, query=q)[0], ctx())
    await env.bot.drain()
    assert env.engine.calls == [] and "Отправлено" in q.edits[-1][0]
