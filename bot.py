"""
Telegram-бот: единственный пульт и канал уведомлений владельца.
Чужие чаты игнорируются полностью (без ответа). Уведомления идут через очередь в памяти с повтором 5/15/60 с.
"""
import asyncio
import logging
from typing import Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

import core
from bot_text import (PROFILE_STEPS, format_digest, format_done, format_draft, format_incident, format_profile, format_reply,
                      format_status, parse_filters_args, parse_profile_answer)
from models import PROFILE_FIELDS, profile_complete
from store import Row, Store

log = logging.getLogger("wohnung")

RETRY_DELAYS = (5, 15, 60)   # пауза перед повтором при сетевой ошибке, сек
MAX_LEN = 4096               # предел длины сообщения Telegram
_STEP_FIELDS = [f for f, _ in PROFILE_STEPS]
_QUESTIONS = dict(PROFILE_STEPS)

HELP = ("Команды:\n/status — состояние\n/pause [площадка], /resume [площадка]\n"
        "/mode auto|confirm [площадка]\n/filters [max_warm 950 min_rooms 3 min_lease_months 12]\n"
        "/profile — заполнить или показать; /profile <поле> [значение]; /profile hide|show <поле>\n"
        "/prompt — показать промпт; ответом на сообщение с текстом — заменить\n/cancel — отменить ввод")


def _scrub(text: str, token: str) -> str:
    return text.replace(token, "<token>") if token else text


def _kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows])


class TelegramNotifier:
    """Реализует Notifier. Всё, кроме draft(), уходит через очередь, чтобы конвейер не ждал сеть."""

    def __init__(self, bot, cfg: dict, delays=RETRY_DELAYS, sleep=asyncio.sleep):
        self.bot, self.cfg, self.delays, self._sleep = bot, cfg, delays, sleep
        self._queue: asyncio.Queue[Callable[[], Awaitable]] = asyncio.Queue()
        self._worker: asyncio.Task | None = None

    # ---------- очередь ----------

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None

    async def drain(self) -> None:
        await self._queue.join()

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await job()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("уведомление Telegram не доставлено")
            finally:
                self._queue.task_done()

    def _enqueue(self, job: Callable[[], Awaitable]) -> None:
        self.start()
        self._queue.put_nowait(job)

    async def _retry(self, call: Callable[[], Awaitable]):
        """Вызов Telegram с повтором при сетевых сбоях; после последней неудачи — None и запись в журнал."""
        for attempt in range(len(self.delays) + 1):
            pause = None
            try:
                return await call()
            except RetryAfter as e:
                pause = float(getattr(e, "retry_after", 1)) + 1
            except BadRequest as e:     # подкласс NetworkError, но повтор не поможет (сообщение удалено и т.п.)
                if "not modified" in str(e).lower():   # текст уже такой — не ошибка
                    return None
                log.warning("Telegram: %s", _scrub(str(e), self.cfg.get("bot_token", "")))
                return None
            except NetworkError as e:   # включает TimedOut
                log.warning("Telegram: сеть недоступна (%s)", type(e).__name__)
            except TelegramError as e:  # Forbidden и т.п. — повтор не поможет
                log.warning("Telegram: %s", _scrub(str(e), self.cfg.get("bot_token", "")))
                return None
            if attempt >= len(self.delays):
                break
            await self._sleep(pause if pause is not None else self.delays[attempt])
        log.error("Telegram: уведомление потеряно после %d попыток", len(self.delays) + 1)
        return None

    async def _send(self, text: str, markup=None, silent: bool = False) -> int:
        chat = self.cfg.get("owner_chat_id", 0)
        if not chat:
            log.warning("Telegram: владелец не привязан (/start), уведомление пропущено")
            return 0
        msg = await self._retry(lambda: self.bot.send_message(
            chat_id=chat, text=text[:MAX_LEN], reply_markup=markup, disable_notification=silent))
        return msg.message_id if msg is not None else 0

    def _push(self, text: str, markup=None, silent: bool = False) -> None:
        self._enqueue(lambda: self._send(text, markup, silent))

    # ---------- Notifier ----------

    async def draft(self, row: Row, text: str, warm_estimated: bool) -> int:
        if row.draft_msg_id:   # новый вариант заменяет старый черновик, а не копится в чате
            await self._delete(row.draft_msg_id)
        kb = _kb([("Отправить", f"send:{row.id}"), ("Изменить", f"edit:{row.id}")],
                 [("Другой вариант", f"regen:{row.id}"), ("Пропустить", f"skip:{row.id}")])
        return await self._send(format_draft(row, text, warm_estimated), kb)

    async def ask_duplicate(self, row: Row, match: Row) -> None:
        text = (f"Похоже на уже знакомое объявление.\nНовое #{row.id} · {row.platform}: {row.title}\n{row.url}\n\n"
                f"Знакомое #{match.id} · {match.platform} ({match.status}): {match.title}\n{match.url}")
        self._push(text, _kb([("Всё равно отправить", f"dup:{row.id}:send"), ("Пропустить", f"dup:{row.id}:skip")]))

    async def ask_stuck(self, row: Row) -> None:
        text = f"Не понял, ушло ли письмо. Проверьте отправленные на сайте.\n{format_done(row, '?')}"
        self._push(text, _kb([("Дошло", f"stuck:{row.id}:sent"), ("Не дошло, отправить снова", f"stuck:{row.id}:resend")]))

    async def sent(self, row: Row) -> None:
        if row.draft_msg_id:   # авто-режим без черновика — без отдельного сообщения, хватит сводки
            mark = "✓ Отправлено" + (" (переписка уже была)" if "переписка" in (row.reason or "") else "")
            self._enqueue(lambda: self._edit(row.draft_msg_id, format_done(row, mark)))

    async def _edit(self, message_id: int, text: str) -> None:
        chat = self.cfg.get("owner_chat_id", 0)
        if chat:
            await self._retry(lambda: self.bot.edit_message_text(
                chat_id=chat, message_id=message_id, text=text[:MAX_LEN], reply_markup=None))

    async def _delete(self, message_id: int) -> None:
        chat = self.cfg.get("owner_chat_id", 0)
        if chat:
            await self._retry(lambda: self.bot.delete_message(chat_id=chat, message_id=message_id))

    async def reply(self, row: Row | None, r) -> None:
        self._push(format_reply(row, r))

    async def incident(self, platform: str, kind: str, detail: str) -> None:
        self._push(format_incident(platform, kind, detail))

    async def missing_profile(self) -> None:
        self._push("Профиль не заполнен: письма не отправляются, подходящие объявления ждут. Заполните: /profile")

    async def digest(self, stats: dict) -> None:
        self._push(format_digest(stats), silent=True)

    async def remove_draft(self, row: Row) -> None:
        if not row.draft_msg_id:
            return
        self._enqueue(lambda: self._delete(row.draft_msg_id))


class OwnerBot:
    """Обработчики команд и кнопок. Каждый проверяет, что пишет владелец."""

    def __init__(self, cfg: dict, engine, store: Store):
        self.cfg, self.engine, self.store = cfg, engine, store
        # chat → (id черновика, его сообщение с кнопками, подсказка «пришлите текст»)
        self.pending_edit: dict[int, tuple[int, object, object]] = {}
        self.wizard: dict[int, list[str]] = {}                # chat → оставшиеся поля профиля
        self._tasks: set[asyncio.Task] = set()                # фоновые отправки (печать письма идёт минуту-две)

    # ---------- общее ----------

    def _is_owner(self, update: Update) -> bool:
        chat = update.effective_chat
        owner = self.cfg.get("owner_chat_id", 0)
        return chat is not None and bool(owner) and chat.id == owner

    @staticmethod
    async def _say(update: Update, text: str):
        return await update.effective_message.reply_text(text[:MAX_LEN])

    @staticmethod
    async def _delete(message) -> None:
        """Служебные сообщения (подсказка, ответ владельца) убираются, чтобы не засорять чат."""
        if message is None:
            return
        try:
            await message.delete()
        except Exception as e:   # нет прав, уже удалено — не важно
            log.debug("не удалось удалить сообщение: %s", type(e).__name__)

    def _save(self) -> None:
        core.save_config(self.cfg)

    def _cancel_input(self, chat: int):
        """Сбрасывает ввод; возвращает подсказку правки, которую стоит удалить."""
        self.wizard.pop(chat, None)
        pending = self.pending_edit.pop(chat, None)
        return pending[2] if pending else None

    async def _collapse(self, message, id: int, outcome: str) -> None:
        """Сворачивает сообщение в строку-итог без кнопок; не вышло — хотя бы убирает кнопки."""
        if message is None:
            return
        try:
            row = self.store.get(id)
        except Exception:
            row = None
        text = format_done(row, outcome) if row else outcome
        try:
            await message.edit_text(text[:MAX_LEN], reply_markup=None)
        except TelegramError as e:
            log.warning("не удалось обновить сообщение: %s", _scrub(str(e), self.cfg.get("bot_token", "")))
            try:
                await message.edit_reply_markup(reply_markup=None)
            except TelegramError as e2:
                log.warning("не удалось убрать кнопки: %s", _scrub(str(e2), self.cfg.get("bot_token", "")))

    async def _profile_changed(self) -> None:
        self._save()
        if profile_complete(self.cfg["profile"]):
            await self.engine.profile_updated()

    def _platform_arg(self, args: list[str]) -> str | None:
        """Площадка из аргументов; ValueError, если такой нет."""
        if not args:
            return None
        if args[0] not in self.cfg["platforms"]:
            raise ValueError("Нет такой площадки. Есть: " + ", ".join(self.cfg["platforms"]))
        return args[0]

    # ---------- команды ----------

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None:
            return
        if not self.cfg.get("owner_chat_id"):
            self.cfg["owner_chat_id"] = chat.id
            self._save()
            await self._say(update, "Привязано: теперь я отвечаю только вам.\n\n" + HELP)
            if not profile_complete(self.cfg["profile"]):
                await self._say(update, "Профиль пока пуст. Заполнить: /profile")
        elif self._is_owner(update):
            await self._say(update, HELP)

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._is_owner(update):
            await self._say(update, HELP)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._is_owner(update):
            await self._say(update, format_status(self.engine.status()))

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._is_owner(update):
            await self._delete(self._cancel_input(update.effective_chat.id))
            await self._delete(update.effective_message)

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._pause_resume(update, context, True)

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._pause_resume(update, context, False)

    async def _pause_resume(self, update, context, pause: bool) -> None:
        if not self._is_owner(update):
            return
        try:
            platform = self._platform_arg(context.args or [])
        except ValueError as e:
            await self._say(update, str(e))
            return
        (self.engine.pause if pause else self.engine.resume)(platform)
        what = platform or "все площадки"
        await self._say(update, f"{'Пауза' if pause else 'Работа возобновлена'}: {what}")

    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        args = context.args or []
        if not args:
            await self._say(update, "Режимы: " + ", ".join(
                f"{p} — {self.engine.mode_for(p)}" for p in self.cfg["platforms"])
                + "\nМеняется: /mode auto|confirm [площадка]")
            return
        if args[0] not in ("auto", "confirm"):
            await self._say(update, "Режим: auto (отправлять сразу) или confirm (с подтверждением).")
            return
        try:
            platform = self._platform_arg(args[1:])
        except ValueError as e:
            await self._say(update, str(e))
            return
        if platform:
            self.cfg["platforms"][platform]["mode"] = args[0]
        else:
            self.cfg["mode"] = args[0]
        self._save()
        await self._say(update, f"Режим {args[0]}: {platform or 'по умолчанию для всех'}")

    async def cmd_filters(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        args = context.args or []
        if args:
            try:
                self.cfg["filters"].update(parse_filters_args(args))   # тот же словарь, что держат площадки
            except ValueError as e:
                await self._say(update, str(e))
                return
            self._save()
        await self._say(update, "Фильтры: " + ", ".join(f"{k} = {v}" for k, v in self.cfg["filters"].items()))

    async def cmd_prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        replied = update.effective_message.reply_to_message
        if replied is not None and replied.text:
            core.prompt_path(self.cfg).write_text(replied.text, encoding="utf-8")
            await self._say(update, "Промпт заменён.")
            return
        for p in (core.prompt_path(self.cfg), core.RES / "prompt.example.txt"):
            if p.exists():
                await self._say(update, p.read_text(encoding="utf-8") or "(пусто)")
                return
        await self._say(update, "Промпт не найден.")

    # ---------- профиль ----------

    async def cmd_profile(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        chat, args = update.effective_chat.id, context.args or []
        self._cancel_input(chat)
        profile = self.cfg["profile"]
        if not args:
            if profile_complete(profile):
                await self._say(update, format_profile(profile) + "\n\nИзменить поле: /profile <поле>")
            else:
                self.wizard[chat] = list(_STEP_FIELDS)
                await self._say(update, "Заполним профиль (/cancel — отмена).\n" + _QUESTIONS[_STEP_FIELDS[0]])
            return
        if args[0] in ("hide", "show"):
            await self._hide_show(update, args)
            return
        if args[0] not in _QUESTIONS:
            await self._say(update, "Нет такого поля. Есть: " + ", ".join(_STEP_FIELDS))
            return
        if len(args) > 1:   # /profile persons 3
            if not await self._store_answer(update, args[0], " ".join(args[1:]), []):
                self.wizard[chat] = [args[0]]   # следующий текст — новая попытка
            return
        self.wizard[chat] = [args[0]]
        await self._say(update, _QUESTIONS[args[0]])

    async def _hide_show(self, update: Update, args: list[str]) -> None:
        if len(args) != 2 or args[1] not in PROFILE_FIELDS or args[1] == "hidden":
            await self._say(update, "Формат: /profile hide|show <поле>")
            return
        h = self.cfg["profile"].get("hidden") or []
        hidden = [x.strip() for x in h.split(",") if x.strip()] if isinstance(h, str) else list(h)
        if args[0] == "hide" and args[1] not in hidden:
            hidden.append(args[1])
        if args[0] == "show":
            hidden = [x for x in hidden if x != args[1]]
        self.cfg["profile"]["hidden"] = hidden
        await self._profile_changed()
        await self._say(update, f"Поле {args[1]}: "
                                f"{'скрыто из первого письма' if args[0] == 'hide' else 'снова используется'}")

    async def _store_answer(self, update: Update, field: str, text: str, rest: list[str]) -> bool:
        """Разбирает и сохраняет ответ; при ошибке повторяет вопрос. True — принято."""
        try:
            value = parse_profile_answer(field, text)
        except ValueError as e:
            await self._say(update, f"{e}\n{_QUESTIONS[field]}")
            return False
        if value is None:
            self.cfg["profile"].pop(field, None)
        else:
            self.cfg["profile"][field] = value
        await self._profile_changed()
        if rest:
            self.wizard[update.effective_chat.id] = rest
            await self._say(update, _QUESTIONS[rest[0]])
        else:
            await self._say(update, "Профиль заполнен." if profile_complete(self.cfg["profile"]) else "Сохранено.")
        return True

    # ---------- текст без команды ----------

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update) or update.effective_message is None:
            return
        chat, text = update.effective_chat.id, (update.effective_message.text or "").strip()
        if chat in self.pending_edit:
            id, draft_msg, prompt_msg = self.pending_edit.pop(chat)
            await self._delete(prompt_msg)
            await self._delete(update.effective_message)
            try:
                if text.startswith("!"):
                    # старый черновик удалится, когда придёт новый
                    await self._collapse(draft_msg, id, "↻ Готовлю новый вариант…")
                    await self.engine.regenerate(id, text[1:].strip())
                else:
                    await self._approve(id, text, draft_msg)
            except Exception as e:
                log.exception("правка черновика #%s", id)
                await self._say(update, f"Ошибка: {type(e).__name__}")
        elif chat in self.wizard:
            fields = self.wizard.pop(chat)
            if not await self._store_answer(update, fields[0], text, fields[1:]):
                self.wizard[chat] = fields   # остаёмся на этом вопросе

    # ---------- кнопки ----------

    async def _approve(self, id: int, text: str | None, message) -> None:
        """Сразу показывает «Отправляю…», а саму отправку ведёт в фоне: бот не молчит минуту, пока печатается письмо."""
        if self.store.get(id).status != "draft":
            await self._collapse(message, id, self._approve_outcome(id))
            return
        await self._collapse(message, id, "⏳ Отправляю…")
        task = asyncio.get_running_loop().create_task(self._finish_approve(id, text, message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _finish_approve(self, id: int, text: str | None, message) -> None:
        try:
            await self.engine.approve(id, text)
        except Exception as e:
            log.exception("отправка #%s", id)
            await self._collapse(message, id, f"Ошибка отправки: {type(e).__name__}, см. журнал")
            return
        await self._collapse(message, id, self._approve_outcome(id))

    async def drain(self) -> None:
        """Дождаться фоновых отправок (тесты, остановка)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _approve_outcome(self, id: int) -> str:
        status = self.store.get(id).status
        return {"sent": "✓ Отправлено", "queued": "⏳ В очереди на отправку (лимит или пауза)",
                "sending": "? Не ясно, ушло ли — см. вопрос ниже",
                "draft": "✎ Нужна правка — см. новый черновик"}.get(status, f"Не отправлено (статус: {status})")

    def _dup_outcome(self, id: int) -> str:
        status = self.store.get(id).status
        return {"queued": "✓ В очереди на отправку", "sending": "✓ Отправляется", "sent": "✓ Отправлено",
                "draft": "✓ Черновик создан, смотрите ниже", "filtered": "Не прошло фильтры",
                "seen": "Ждёт заполнения профиля", "failed": "Не удалось обработать"
                }.get(status, f"Статус: {status}")

    def _skip_outcome(self, id: int) -> str:
        status = self.store.get(id).status
        return "Пропущено" if status == "skipped" else f"Уже обработано (статус: {status})"

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None or not self._is_owner(update):
            return
        await q.answer()
        parts = (q.data or "").split(":")
        try:
            id = int(parts[1])
            outcome = await self._dispatch(update, parts[0], id, parts[2] if len(parts) > 2 else "")
        except Exception as e:
            log.exception("кнопка %s", parts[0])
            await self._say(update, f"Ошибка: {type(e).__name__}")
            return
        if outcome is not None:   # сообщение сворачивается и теряет кнопки, чтобы их нельзя было нажать дважды
            await self._collapse(q.message, id, outcome)

    async def _dispatch(self, update: Update, kind: str, id: int, arg: str) -> str | None:
        """Выполняет действие кнопки; возвращает итог для сообщения или None (кнопки остаются)."""
        e = self.engine
        if kind == "send":
            await self._approve(id, None, update.callback_query.message)
            return None
        if kind == "edit":
            if self.store.get(id).status != "draft":
                return "Уже обработано"
            chat = update.effective_chat.id
            await self._delete(self._cancel_input(chat))   # повторное «Изменить» — старая подсказка не нужна
            prompt = await self._say(update, "Пришлите новый текст сообщением. Если начать с «!», это пожелание "
                                             "для нового варианта («!короче»). /cancel — отмена.")
            self.pending_edit[chat] = (id, update.callback_query.message, prompt)
            return None
        if kind == "regen":
            await self._collapse(update.callback_query.message, id, "↻ Готовлю новый вариант…")
            await e.regenerate(id)
            return None
        if kind == "skip":
            await e.skip(id)
            return self._skip_outcome(id)
        if kind == "dup":
            if arg == "send":
                await e.resolve_duplicate(id, True)
                return self._dup_outcome(id)
            await e.resolve_duplicate(id, False)
            return "Пропущено"
        if kind == "stuck":
            await e.resolve_stuck(id, arg == "sent")
            return "✓ Отмечено как отправленное" if arg == "sent" else "↻ Поставлено в очередь снова"
        return None


def build_application(cfg: dict, engine, store: Store) -> tuple[Application, OwnerBot]:
    proxy = core.system_proxy()
    req, upd_req = (HTTPXRequest(proxy=proxy), HTTPXRequest(proxy=proxy)) if proxy else (HTTPXRequest(), HTTPXRequest())
    app = Application.builder().token(cfg["bot_token"]).request(req).get_updates_request(upd_req).build()
    h = OwnerBot(cfg, engine, store)
    for name in ("start", "help", "status", "cancel", "pause", "resume", "mode", "filters", "prompt", "profile"):
        app.add_handler(CommandHandler(name, getattr(h, f"cmd_{name}")))
    app.add_handler(CallbackQueryHandler(h.on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, h.on_text))

    async def on_error(update, context) -> None:
        log.warning("Telegram: %s", _scrub(f"{type(context.error).__name__}: {context.error}", cfg["bot_token"]))
    app.add_error_handler(on_error)
    return app, h


async def start_bot(cfg: dict, engine, store: Store) -> Application:
    """Запускает опрос Telegram и подключает уведомления к конвейеру."""
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)   # их INFO-записи содержат адрес с токеном
    app, _ = build_application(cfg, engine, store)
    notifier = TelegramNotifier(app.bot, cfg)
    engine.notifier = notifier
    app.bot_data["notifier"] = notifier
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    notifier.start()
    return app


async def stop_bot(app: Application) -> None:
    notifier = app.bot_data.get("notifier")
    if app.updater and app.updater.running:
        await app.updater.stop()
    if app.running:
        await app.stop()
    if notifier:
        await notifier.stop()
    await app.shutdown()
