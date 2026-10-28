"""
Конвейер: опрос площадок → дедупликация → фильтр → письмо → отправка или черновик,
ответы из почты, чистка черновиков и вечерняя сводка.

Весь конвейер работает в одном цикле asyncio: участок «find_dedup → add → блокирующий статус» идёт
без await, поэтому две площадки с одной квартирой не получат два письма.
"""
import asyncio
import dataclasses
import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import core
from ai import AIError
from browser import human_pause
from filters import check, estimate_warm, prefilter
from letter import LetterError, generate_letter
from models import Listing, profile_complete
from sources.base import (TZ, AlreadyContactedError, BlockedError, ListingGoneError, MissingFieldError,
                          NotSubmittedError,
                          RateLimiter, SourceBroken, poll_interval)
from store import Row, Store, contact_key

log = logging.getLogger("wohnung")

SEND_EVERY = 10          # как часто смотреть очередь отправки, сек
MAINTENANCE_EVERY = 60   # чистка черновиков и сводка, сек
MISSING_PROFILE_EVERY = timedelta(hours=24)
GENERATING = "generating"   # reason у строки, зарезервированной на время генерации письма
NO_PROFILE = "профиль не заполнен"   # reason у строки seen: ждёт профиля
RECHECK_WINDOW = timedelta(hours=24)  # такие seen обрабатываем, когда профиль заполнен; столько же живёт повтор failed
EXPIRED = "черновик устарел"   # reason у skipped: владелец не ответил за draft_ttl_hours
GONE = "объявление снято с сайта"   # reason у failed: отправлять было уже некуда
LOAD_FAILED = "объявление не загрузилось"   # начало reason у failed без сбоя письма — такие не повторяем
# «Пройти заново»: письмо по объявлению не ушло и не стоит в очереди — объявление проходит конвейер ещё раз
REDO_STATUSES = ("seen", "filtered", "duplicate", "ask_dup", "draft", "skipped", "failed")
MAX_PAGES = 3   # сколько страниц выдачи читать, если на странице все объявления новые (или принудительный проход)
SLOW_RETRY = timedelta(minutes=5)   # страница не загрузилась при отправке — повтор не раньше
SLOW_NOTIFY_AFTER = 3   # столько таймаутов подряд — сообщение владельцу (без паузы площадки)
BLOCKING_KINDS = ("captcha", "logged_out", "blocked", "broken")   # эти случаи держат площадку на паузе
SNAPSHOTS_KEEP = 40   # сколько файлов снимков страниц (png + html) хранить в debug_dir


class Paused(Exception):
    """Площадку поставили на паузу, пока ждали её блокировку (например, владелец входит в аккаунт)."""


class Notifier(Protocol):
    async def draft(self, row: Row, text: str, warm_estimated: bool) -> int: ...   # message_id
    async def ask_duplicate(self, row: Row, match: Row) -> None: ...
    async def ask_stuck(self, row: Row) -> None: ...
    async def sent(self, row: Row) -> None: ...   # черновик (если был) сворачивается в строку «✓ Отправлено»
    async def reply(self, row: Row | None, r) -> None: ...   # None — «не нашёл, к какому объявлению»
    async def incident(self, platform: str, kind: str, detail: str) -> None: ...
    async def missing_profile(self) -> None: ...
    async def digest(self, stats: dict) -> None: ...
    async def remove_draft(self, row: Row) -> None: ...
    async def expire_draft(self, row: Row) -> None: ...   # черновик сворачивается в строку с кнопкой «Вернуть»
    async def draft_reminder(self, rows: list[Row], hours: int) -> None: ...   # «через N ч устареют»


def _is_timeout(e: BaseException | None) -> bool:
    """Сбой из-за долгой загрузки страницы (TimeoutError Playwright или встроенный) — сам по себе или как причина."""
    while e is not None:
        if isinstance(e, TimeoutError) or type(e).__name__ == "TimeoutError":
            return True
        e = e.__cause__
    return False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_local(utc: datetime) -> datetime:
    """Наивный UTC → наивное местное время Бонна."""
    return utc.replace(tzinfo=timezone.utc).astimezone(TZ).replace(tzinfo=None)


def to_utc(local: datetime) -> datetime:
    """Наивное местное время Бонна → наивный UTC."""
    return local.replace(tzinfo=TZ).astimezone(timezone.utc).replace(tzinfo=None)


class Engine:
    def __init__(self, cfg: dict, store: Store, ai, sources: dict, browser, notifier: Notifier,
                 mail=None, clock=_utcnow, debug_dir: Path | None = None, only=None):
        """only — запуск отдельных площадок (опрашиваются только они); None — все включённые в настройках."""
        self.cfg, self.store, self.ai, self.sources = cfg, store, ai, sources
        self.selective = only is not None
        self._extra: set[str] = set(only or ())   # площадки, запущенные по одной (опрашиваются и без «enabled»)
        self._wake: dict[str, asyncio.Event] = {}   # площадка → «опросить сейчас», не дожидаясь интервала
        self.debug_dir = debug_dir   # сюда — снимок страницы при сбое площадки; None — не снимать
        self.browser, self.notifier, self.mail = browser, notifier, mail
        self.clock = clock   # наивный UTC, как в Store
        for src in sources.values():
            src.filters = cfg["filters"]
        self._paused: set[str] = set()
        self._paused_all = False
        self._listings: dict[int, Listing] = {}      # подробности объявления для отправки и перегенерации
        self._asked_stuck: set[int] = set()
        self._stuck_paused: set[str] = set()   # площадки на паузе из-за неясной отправки (снимает resolve_stuck)
        self._login_prev: dict[str, bool] = {}   # была ли площадка на паузе до входа владельца
        self._paced: dict[str, bool] = {}   # идёт опрос площадки: грузили ли уже объявление (пауза перед следующим)
        self._recheck_lock = asyncio.Lock()   # profile_updated: один проход за раз
        self._rescan: set[str] = set()   # площадки, у которых следующий опрос — принудительный проход
        self._slow: dict[str, int] = {}   # площадка → таймаутов страницы подряд при отправке
        self._slow_until: dict[str, datetime] = {}   # площадка → раньше этого отправку не повторяем
        self._cap_logged: dict[str, str] = {}   # площадка → день, когда записали «дневной лимит исчерпан»
        self._stop = asyncio.Event()

    # ---------- настройки и состояние ----------

    def mode_for(self, platform: str) -> str:
        return (self.cfg["platforms"].get(platform) or {}).get("mode") or self.cfg["mode"]

    def active(self, platform: str) -> bool:
        """Площадка участвует в этом запуске: запущена по одной или (в общем запуске) включена в настройках.
        Настройку читаем на ходу — переключатель в окне действует без перезапуска."""
        if platform not in self.sources:
            return False
        if platform in self._extra:
            return True
        return not self.selective and bool((self.cfg["platforms"].get(platform) or {}).get("enabled"))

    def activate(self, platform: str) -> None:
        """Добавить площадку в идущий запуск и сразу опросить."""
        self._extra.add(platform)
        self.wake(platform)

    def wake(self, platform: str) -> None:
        """Цикл площадки проснётся сейчас: активная опросится, выключенная снова уснёт."""
        ev = self._wake.get(platform)
        if ev:
            ev.set()

    def rescan(self) -> list[str]:
        """Кнопка «Пройти заново»: следующий опрос каждой работающей площадки (сейчас же) заново пишет черновики
        по всем подходящим объявлениям выдачи, по которым письмо не ушло. Возвращает эти площадки."""
        ps = [p for p in self.sources if self.active(p) and not self._paused_now(p)]
        self._rescan.update(ps)
        for p in ps:
            self.wake(p)
        return ps

    def _paused_now(self, platform: str) -> bool:
        return self._paused_all or platform in self._paused

    def pause(self, platform: str | None) -> None:
        if platform is None:
            self._paused_all = True
        else:
            self._paused.add(platform)

    def resume(self, platform: str | None) -> None:
        """Снимает паузу и закрывает случаи: новая капча после этого — новый случай."""
        names = [platform] if platform else sorted(set(self._paused) | set(self.cfg["platforms"]))
        if platform is None:
            self._paused_all = False
            self._paused.clear()
            self._stuck_paused.clear()
        else:
            self._paused.discard(platform)
            self._stuck_paused.discard(platform)
        for p in names:
            self.store.close_incidents(p)

    def login_started(self, platform: str) -> None:
        """Владелец входит в аккаунт в окне площадки: конвейер её не трогает (окно не уводим со страницы входа)."""
        self._login_prev.setdefault(platform, platform in self._paused)
        self._paused.add(platform)

    def login_finished(self, platform: str, ok: bool) -> None:
        """Вход выполнен — пауза и случаи сняты; не выполнен — пауза как до входа."""
        was_paused = self._login_prev.pop(platform, True)
        if ok:
            self.resume(platform)
        elif not was_paused:
            self._paused.discard(platform)

    def status(self) -> dict:
        today = to_local(self.clock()).date()
        return {"paused": sorted(self._paused), "paused_all": self._paused_all,
                "modes": {p: self.mode_for(p) for p in self.cfg["platforms"]},
                "incidents": self.store.open_incidents(),
                "drafts": len(self.store.by_status("draft")), "queued": len(self.store.by_status("queued")),
                "today": self._day_stats(today)}

    def _day_stats(self, day) -> dict:
        start = to_utc(datetime(day.year, day.month, day.day))
        end = to_utc(datetime(day.year, day.month, day.day) + timedelta(days=1))
        return self.store.stats_between(start, end)

    def _prompt(self) -> str:
        for p in (core.prompt_path(self.cfg), core.RES / "prompt.example.txt"):
            if p.exists():
                return p.read_text(encoding="utf-8")
        return ""

    async def _safe(self, coro):
        """Сбой бота (Telegram недоступен) не должен ломать конвейер."""
        try:
            return await coro
        except Exception:
            log.exception("уведомление не отправлено")
            return None

    async def _incident(self, platform: str, kind: str, detail: str) -> None:
        if self.store.open_incident(platform, kind):   # одно сообщение на случай
            await self._safe(self.notifier.incident(platform, kind, detail))

    async def _block(self, platform: str, kind: str, detail: str) -> None:
        self.pause(platform)
        log.warning("%s: %s — площадка на паузе (%s)", platform, kind, detail)
        await self._incident(platform, kind, detail)

    async def _snapshot(self, platform: str, tag: str, page=None) -> None:
        """Скриншот и HTML текущей страницы площадки в debug_dir — чтобы потом понять, что было на экране."""
        if self.debug_dir is None:
            return
        try:
            page = page or await self.browser.page(platform)
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            base = self.debug_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{platform}-{tag}"
            log.info("%s: страница при сбое: %s — снимок debug/%s.png/.html", platform, page.url, base.name)
            base.with_suffix(".html").write_text(await page.content(), encoding="utf-8")
            await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        except Exception as e:
            log.debug("%s: снимок страницы не сделан: %s", platform, e)
        try:
            for old in sorted(self.debug_dir.iterdir())[:-SNAPSHOTS_KEEP]:
                old.unlink()
        except OSError:
            pass

    # ---------- обработка объявления ----------

    def _row_listing(self, row: Row) -> Listing:
        return self._listings.get(row.id) or Listing(row.platform, row.ext_id, row.url, row.title)

    async def _detail(self, platform: str, l: Listing, check_pause: bool = False) -> Listing:
        async with self.browser.lock(platform):
            if check_pause and self._paused_now(platform):   # пауза пришла, пока ждали блокировку
                raise Paused(platform)
            page = await self.browser.page(platform)
            return await self.sources[platform].fetch_detail(page, l)

    def _known(self, l: Listing, match_id: int) -> Row | None:
        row = self.store.get(match_id)
        return row if (row.platform, row.ext_id) == (l.platform, l.ext_id) else None

    def _mark_duplicate(self, l: Listing, warm: float | None, match_id: int) -> str:
        id = self.store.add(l, warm)
        self.store.set_status(id, "duplicate", reason=f"как #{match_id}")
        return "duplicate"

    async def _pace(self, platform: str) -> None:
        """Между загрузками объявлений в одном опросе — пауза как у человека."""
        if self._paced.get(platform):
            await human_pause(3, 8)
        if platform in self._paced:
            self._paced[platform] = True

    async def process(self, platform: str, l: Listing, force: bool = False) -> str:
        """force — принудительный проход: знакомое объявление без отправленного письма обрабатывается заново,
        письмо всегда идёт черновиком."""
        days = self.cfg["dedup_days"]
        # 1. до загрузки подробностей: уже знакомое объявление или явный дубль
        d = self.store.find_dedup(l, estimate_warm(l)[0], days)
        if d.kind == "exact":
            known = self._known(l, d.match_id)
            if known and force and known.status in REDO_STATUSES:
                return await self._redo(platform, known, l)
            return known.status if known else self._mark_duplicate(l, estimate_warm(l)[0], d.match_id)
        # 2. отсев по данным выдачи — объявление не открываем (строка filtered не блокирует дубли)
        if reason := prefilter(l, self.cfg["filters"]):
            self.store.set_status(self.store.add(l, estimate_warm(l)[0]), "filtered", reason=reason)
            return "filtered"
        # 3. подробности (BlockedError/SourceBroken/Paused уходят в poll_once); между объявлениями — пауза как у человека
        await self._pace(platform)
        l = await self._detail(platform, l, check_pause=True)
        warm, est = estimate_warm(l)
        # 4. от этой проверки до резерва — ни одного await
        d = self.store.find_dedup(l, warm, days)
        if d.kind == "exact":
            known = self._known(l, d.match_id)
            return known.status if known else self._mark_duplicate(l, warm, d.match_id)
        id = self.store.add(l, warm)
        self._listings[id] = l
        if d.kind == "similar" and self._filtered(id, l):   # не спрашиваем про дубль того, что всё равно отсеется
            return "filtered"
        if d.kind == "similar":
            self.store.set_status(id, "ask_dup", reason=f"похоже на #{d.match_id}")
            await self._safe(self.notifier.ask_duplicate(self.store.get(id), self.store.get(d.match_id)))
            return "ask_dup"
        return await self._continue(id, l, force_draft=force)

    async def _redo(self, platform: str, row: Row, l: Listing) -> str:
        """Принудительный проход по знакомому объявлению: прежнее сообщение в Telegram удаляется,
        дальше — как у строки, ждавшей профиля (_recheck): дубли, фильтры, лимит контакта, черновик."""
        if prefilter(l, self.cfg["filters"]) and row.status == "filtered":
            return "filtered"   # по выдаче видно, что не подходит, — объявление не открываем
        await self._pace(platform)
        l = await self._detail(platform, l, check_pause=True)   # сбой — строка остаётся как была
        row = self.store.get(row.id)   # пока грузили, владелец мог нажать кнопку на старом черновике
        if row.status not in REDO_STATUSES:
            return row.status
        if row.draft_msg_id:
            await self._safe(self.notifier.remove_draft(row))
        self.store.set_status(row.id, "seen", reason="", draft_msg_id=None)
        self._listings[row.id] = l
        return await self._recheck(row.id, l, force_draft=True)

    async def _continue(self, id: int, l: Listing, force_draft: bool = False) -> str:
        """Шаги 4–8. До резерва (queued/generating) нет await."""
        profile = self.cfg["profile"]
        r = check(l, self.cfg["filters"], profile)
        if not r.ok:
            self.store.set_status(id, "filtered", reason=r.reason)
            return "filtered"
        if not profile_complete(profile):
            self.store.set_status(id, "seen", reason=NO_PROFILE)
            now = self.clock()
            last = self.store.get_meta("missing_profile_at")   # переживает перезапуск
            if last is None or now - datetime.fromisoformat(last) >= MISSING_PROFILE_EVERY:
                self.store.set_meta("missing_profile_at", now.isoformat())
                await self._safe(self.notifier.missing_profile())
            return "seen"
        # считаем и ждущие письма (очередь, черновик): иначе одна выдача ставит в очередь пять писем одному контакту
        if self.store.contact_count(contact_key(l.contact_name), 7) >= self.cfg["contact_cap_7d"]:
            self.store.set_status(id, "filtered", reason="лимит контакта")
            return "filtered"
        self.store.set_status(id, "queued", reason=GENERATING)   # резерв: блокирует дубль на другой площадке
        return await self._write_letter(id, l, r.warm_estimated, force_draft=force_draft)

    async def _write_letter(self, id: int, l: Listing, warm_estimated: bool, wish: str = "",
                            force_draft: bool = False) -> str:
        profile = self.cfg["profile"]
        t0 = time.monotonic()
        try:
            res = await generate_letter(self.ai, self._prompt(), l, profile, wish, self.store.recent_openings())
        except (LetterError, AIError) as e:
            self.store.set_status(id, "failed", reason=str(e)[:300])
            await self._incident("ai", "ai", str(e))
            return "failed"
        except Exception as e:
            log.exception("письмо для #%s не создано", id)
            self.store.set_status(id, "failed", reason=str(e)[:300])
            return "failed"
        self.store.close_incidents("ai")
        if res.text is None:
            log.info("%s #%s: нейросеть отказалась писать (%s)", l.platform, id, res.skip_reason or "SKIP")
            self.store.set_status(id, "filtered", reason=res.skip_reason or "SKIP")
            return "filtered"
        log.info("%s #%s: письмо готово за %.0f с, %d симв.%s", l.platform, id, time.monotonic() - t0,
                 len(res.text), f" (пожелание: {wish})" if wish else "")
        missing = [f for f in l.required_fields if profile.get(f) in (None, "")]
        if force_draft or missing or self.mode_for(l.platform) == "confirm":
            reason = f"нет полей: {', '.join(missing)}" if missing else ""
            await self._to_draft(id, res.text, warm_estimated, reason)
            return "draft"
        self.store.set_status(id, "queued", letter=res.text, reason="")
        return "queued"

    async def _to_draft(self, id: int, text: str, warm_estimated: bool, reason: str = "") -> None:
        self.store.set_status(id, "draft", letter=text, reason=reason)
        # когда показан (срок жизни и напоминание считаются от него) — в базе, чтобы пережить перезапуск
        self.store.set_meta(f"draft_at:{id}", self.clock().isoformat())
        self.store.set_meta(f"reminded:{id}", "")
        msg_id = await self._safe(self.notifier.draft(self.store.get(id), text, warm_estimated))
        if msg_id is not None:
            self.store.set_status(id, "draft", draft_msg_id=msg_id)

    # ---------- отправка ----------

    def _limiter(self, platform: str) -> RateLimiter:
        p = self.cfg["platforms"][platform]
        return RateLimiter(self.store, platform, p["daily_cap"], p["min_gap_sec"])

    async def _send(self, id: int) -> None:
        """Одна попытка. Не ждёт: если рано или лимит дня исчерпан, строка остаётся queued."""
        row = self.store.get(id)
        platform = row.platform
        if not self._sendable(row):
            return
        async with self.browser.lock(platform):
            # пока ждали блокировку, строку мог отправить или вернуть в очередь другой вызов — проверяем заново
            row = self.store.get(id)
            if not self._sendable(row):
                return
            wait = self._limiter(platform).wait_seconds(self.clock())
            if wait is None:
                day = to_local(self.clock()).date().isoformat()
                if self._cap_logged.get(platform) != day:   # одна запись в день, а не каждые SEND_EVERY сек
                    self._cap_logged[platform] = day
                    log.info("%s: дневной лимит %s писем исчерпан — очередь ждёт до полуночи",
                             platform, self.cfg["platforms"][platform]["daily_cap"])
                return
            if wait > 0 or self.clock() < self._slow_until.get(platform, datetime.min):
                return
            if self.store.contact_count(row.contact_key, 7, ("sent", "sending"), exclude_id=id) >= self.cfg["contact_cap_7d"]:
                self.store.set_status(id, "filtered", reason="лимит контакта")
                log.info("%s: #%s не отправлено — лимит писем контакту", platform, id)
                return
            src = self.sources[platform]
            self.store.set_status(id, "sending")
            l = self._row_listing(row)
            log.info("%s: отправляю #%s «%s» %s", platform, id, row.title, row.url)
            t0 = time.monotonic()
            page = None
            try:
                page = await self.browser.page(platform)
                await src.send(page, l, row.letter, self.cfg["profile"])
            except AlreadyContactedError as e:
                # переписка с арендодателем уже есть — второе письмо не пишем, считаем отправленным
                self.store.set_status(id, "sent", sent_at=self.clock(), reason="переписка уже была")
                log.info("%s: #%s — %s", platform, id, e)
                await self._safe(self.notifier.sent(self.store.get(id)))
            except BlockedError as e:
                self.store.set_status(id, "queued")
                await self._block(platform, e.kind, str(e))
            except ListingGoneError:
                self.store.set_status(id, "failed", reason=GONE)
                log.info("%s: #%s не отправлено — объявление снято с сайта", platform, id)
                await self._safe(self.notifier.gone(self.store.get(id)))
            except MissingFieldError as e:
                await self._to_draft(id, row.letter, False, f"нет поля: {e.field} — заполните: /profile {e.field}")
            except (NotSubmittedError, SourceBroken) as e:
                # до нажатия «Отправить»: письмо точно не ушло — назад в очередь
                self.store.set_status(id, "queued")
                await self._snapshot(platform, f"send-{id}", page)
                if _is_timeout(e):   # сайт медленный, а не сломан: без паузы, повтор позже
                    n = self._slow[platform] = self._slow.get(platform, 0) + 1
                    self._slow_until[platform] = self.clock() + SLOW_RETRY
                    log.warning("%s: #%s — страница не загрузилась (%d раз подряд), повтор через %d мин: %s",
                                platform, id, n, SLOW_RETRY.seconds // 60, e)
                    if n >= SLOW_NOTIFY_AFTER:
                        await self._incident(platform, "slow", str(e))
                else:
                    await self._block(platform, "broken", str(e))
            except Exception as e:
                # после нажатия исход неизвестен (форма могла уйти): строка остаётся sending, решает владелец
                # через resolve_stuck; площадка на паузе, иначе следующая строка застрянет так же
                log.exception("%s: отправка #%s прервана через %.0f с", platform, id, time.monotonic() - t0)
                await self._snapshot(platform, f"send-{id}", page)
                if not self._paused_now(platform):
                    self._stuck_paused.add(platform)
                self.pause(platform)
                self.store.open_incident(platform, "send")   # без отдельного сообщения: хватит вопроса «дошло ли?»
                await self._ask_stuck(self.store.get(id))
            else:
                self.store.set_status(id, "sent", sent_at=self.clock())   # до снятия блокировки площадки
                self.store.close_incidents(platform, "send")
                self.store.close_incidents(platform, "slow")
                self._slow.pop(platform, None)
                self._slow_until.pop(platform, None)
                log.info("%s: письмо #%s отправлено за %.0f с", platform, id, time.monotonic() - t0)
                await self._safe(self.notifier.sent(self.store.get(id)))

    def _sendable(self, row: Row) -> bool:
        return (row.status == "queued" and row.reason != GENERATING and bool(row.letter)
                and row.platform in self.sources and not self._paused_now(row.platform)
                and profile_complete(self.cfg["profile"]))

    async def _ask_stuck(self, row: Row) -> None:
        if row.id not in self._asked_stuck:   # один вопрос на строку за сеанс
            self._asked_stuck.add(row.id)
            await self._safe(self.notifier.ask_stuck(row))

    async def send_queued_once(self) -> None:
        """Очередь по created_at; на площадку — одна попытка за проход."""
        rows = sorted(self.store.by_status("queued"), key=lambda r: (r.created_at, r.id))
        tried: set[str] = set()
        for row in rows:
            if row.platform in tried or row.reason == GENERATING or not row.letter:
                continue
            if not self.active(row.platform) or self._paused_now(row.platform):
                continue
            tried.add(row.platform)
            await self._send(row.id)

    # ---------- действия владельца ----------

    async def approve(self, id: int, text: str | None = None) -> None:
        row = self.store.get(id)
        if row.status != "draft":   # повторное нажатие или черновик уже удалён
            return
        self.store.set_status(id, "queued", letter=text if text else row.letter, reason="")
        await self._send(id)

    async def regenerate(self, id: int, wish: str = "") -> None:
        row = self.store.get(id)
        if row.status not in ("draft", "failed"):   # sending/sent — никогда
            return
        self.store.set_status(id, "queued", reason=GENERATING)
        try:
            l = self._listings.get(id) or await self._detail(row.platform, self._row_listing(row))
        except Exception as e:
            self.store.set_status(id, "failed", reason=f"{LOAD_FAILED}: {e}"[:300])
            return
        self._listings[id] = l
        await self._write_letter(id, l, estimate_warm(l)[1], wish, force_draft=True)

    async def skip(self, id: int) -> None:
        row = self.store.get(id)   # старая кнопка на отправленном письме ничего не меняет
        if row.status in ("draft", "ask_dup", "failed") or (row.status == "queued" and row.reason != GENERATING):
            self.store.set_status(id, "skipped")
            self.store.note("skipped", id)

    async def restore(self, id: int) -> str:
        """Кнопка «Вернуть черновик» у устаревшего (или пропущенного) черновика: объявление загружается заново и
        проходит дубли, фильтры и лимит контакта; письмо — снова черновиком. Возвращает итоговый статус строки
        или "load_failed" — объявление не загрузилось (сняли с сайта, сбой), строка остаётся пропущенной."""
        row = self.store.get(id)
        if row.status != "skipped":
            return row.status
        try:
            l = self._listings.get(id) or await self._detail(row.platform, self._row_listing(row), check_pause=True)
        except (Paused, BlockedError) as e:
            if isinstance(e, BlockedError):   # капча или выход: площадка на паузе, как при опросе
                await self._block(row.platform, e.kind, str(e))
            return "blocked"
        except Exception as e:
            log.info("%s #%s: черновик не вернуть — объявление не загрузилось: %r", row.platform, id, e)
            return "load_failed"
        if self.store.get(id).status != "skipped":   # пока грузили, строку уже обработали
            return self.store.get(id).status
        self._listings[id] = l
        # draft_msg_id остаётся: новый черновик заменит строку «устарел» в чате
        self.store.set_status(id, "seen", reason="")
        return await self._recheck(id, l, force_draft=True)

    async def resolve_duplicate(self, id: int, send: bool) -> None:
        row = self.store.get(id)
        if row.status != "ask_dup":
            return
        if not send:
            self.store.set_status(id, "duplicate", reason=row.reason)
            return
        l = self._listings.get(id)
        if l is None:
            try:
                l = await self._detail(row.platform, self._row_listing(row))
            except Exception as e:
                self.store.set_status(id, "failed", reason=f"{LOAD_FAILED}: {e}"[:300])
                return
            self._listings[id] = l
        await self._continue(id, l)

    async def resolve_stuck(self, id: int, was_sent: bool) -> None:
        row = self.store.get(id)
        if row.status != "sending":
            return
        self._asked_stuck.discard(id)   # новый неясный сбой этой строки — снова вопрос
        if was_sent:
            self.store.set_status(id, "sent", sent_at=self.clock())
            await self._safe(self.notifier.sent(self.store.get(id)))
        else:
            self.store.set_status(id, "queued")
        # пауза из-за неясной отправки снимается, когда владелец ответил по всем зависшим строкам площадки
        # и нет других блокирующих случаев (капча, выход из аккаунта и т.п.)
        p = row.platform
        if not any(r.platform == p for r in self.store.by_status("sending")):
            self.store.close_incidents(p, "send")
            if p in self._stuck_paused and not any(ip == p and k in BLOCKING_KINDS for ip, k in self.store.open_incidents()):
                self._paused.discard(p)
            self._stuck_paused.discard(p)

    # ---------- циклы ----------

    async def startup(self) -> None:
        for platform, kind in self.store.open_incidents():   # капча и т.п. держат паузу и после перезапуска
            if platform in self.cfg["platforms"] and kind in BLOCKING_KINDS:
                self._paused.add(platform)
        for row in self.store.by_status("queued"):
            if row.reason == GENERATING:
                self.store.set_status(row.id, "failed", reason="прервано при генерации")
        for row in self.store.by_status("sending"):   # не отправляем повторно — спрашиваем владельца
            await self._ask_stuck(row)

    async def poll_once(self, platform: str) -> None:
        src = self.sources.get(platform)
        if src is None or self._paused_now(platform):
            return
        t0 = time.monotonic()
        try:
            async with self.browser.lock(platform):
                if self._paused_now(platform):   # пауза пришла, пока ждали блокировку
                    return
                pg = await self.browser.page(platform)
                listings = await src.search(pg)
                force = platform in self._rescan   # флаг снимаем, только когда выдача получена
                self._rescan.discard(platform)
                listings += await self._more_pages(platform, src, pg, listings, force)
        except BlockedError as e:
            await self._block(platform, e.kind, str(e))
            return
        except SourceBroken as e:
            await self._snapshot(platform, "search")
            await self._block(platform, "broken", str(e))
            return
        except Exception:
            log.exception("%s: поиск не удался", platform)
            return
        self._paced[platform] = False
        counts: Counter = Counter()
        try:
            if force:
                log.info("%s: принудительный проход по %d объявлениям выдачи", platform, len(listings))
            await self._process_all(platform, listings, counts, force)
        finally:
            self._paced.pop(platform, None)
            # «известные» — были в базе до опроса; остальные — чем закончилась обработка (filtered, draft, …)
            done = ", ".join(f"{k} {v}" for k, v in counts.most_common())
            log.info("%s: опрос за %.0f с — в выдаче %d%s", platform, time.monotonic() - t0, len(listings),
                     f": {done}" if done else "")

    async def _more_pages(self, platform: str, src, pg, first: list[Listing], force: bool) -> list[Listing]:
        """Следующие страницы выдачи (если площадка умеет): когда на странице нет ни одного знакомого объявления —
        значит, за время простоя новых больше страницы; при принудительном проходе — всегда. Сбой — не страшно."""
        if not hasattr(src, "search_more"):
            return []
        seen, out, batch = {l.ext_id for l in first}, [], first
        for n in range(2, MAX_PAGES + 1):
            if not batch or not (force or all(not self.store.has(l.platform, l.ext_id) for l in batch)):
                break
            await human_pause(2, 5)
            try:
                batch = [l for l in await src.search_more(pg, n) if l.ext_id not in seen]
            except BlockedError:   # капча или выход — как на первой странице: пауза в poll_once
                raise
            except Exception as e:   # первая страница уже есть — обработаем её
                log.warning("%s: страница %d выдачи не загрузилась: %r", platform, n, e)
                break
            seen.update(l.ext_id for l in batch)
            out += batch
        return out

    async def _process_all(self, platform: str, listings: list[Listing], counts: Counter | None = None,
                           force: bool = False) -> None:
        counts = Counter() if counts is None else counts
        for i, l in enumerate(listings):
            if self._paused_now(platform) or not self.active(platform):   # площадку выключили во время опроса
                counts["пропущены (пауза)"] += len(listings) - i
                break
            before = self.store.status_of(l.platform, l.ext_id)
            redo = force and before in REDO_STATUSES
            try:
                status = await self.process(platform, l, force)
                counts[f"заново: {status}" if redo else "известные" if before else status] += 1
            except Paused:
                counts["пропущены (пауза)"] += len(listings) - i
                break
            except BlockedError as e:
                await self._block(platform, e.kind, str(e))
                break
            except SourceBroken as e:
                await self._snapshot(platform, f"detail-{l.ext_id}")
                await self._block(platform, "broken", str(e))
                break
            except Exception:
                counts["ошибка"] += 1
                log.exception("%s: объявление %s не обработано (%s)", platform, l.ext_id, l.url)

    async def mail_once(self) -> None:
        if self.mail is None:
            return
        for r in await asyncio.to_thread(self.mail.fetch_new):
            row = self.store.match_reply(r.platform, r.ext_id, r.url, r.contact)
            if row:
                self.store.add_reply(row.id, r.excerpt, r.link)
                log.info("mail: ответ по %s #%s от %s | %s", row.platform, row.id, r.contact or "?", row.title)
            else:
                log.info("mail: ответ от %s (%s) — объявление не найдено", r.contact or "?", r.platform or "?")
            await self._safe(self.notifier.reply(row, r))

    async def maintenance_once(self) -> None:
        now = self.clock()
        ttl = timedelta(hours=self.cfg["draft_ttl_hours"])
        remind = self.cfg["draft_remind_hours"]
        due = []
        for row in self.store.by_status("draft"):
            shown = self.store.get_meta(f"draft_at:{row.id}")
            age = now - (datetime.fromisoformat(shown) if shown else row.created_at)
            if age >= ttl:
                self.store.set_status(row.id, "skipped", reason=EXPIRED)
                self.store.note("expired", row.id)
                await self._safe(self.notifier.expire_draft(self.store.get(row.id)))
            elif age >= ttl - timedelta(hours=remind) and not self.store.get_meta(f"reminded:{row.id}"):
                self.store.set_meta(f"reminded:{row.id}", now.isoformat())
                due.append(row)
        if due:
            await self._safe(self.notifier.draft_reminder(due, remind))
        local = to_local(now)
        day = local.date().isoformat()
        if local.hour == self.cfg["digest_hour"] and self.store.get_meta("digest_day") != day:
            self.store.set_meta("digest_day", day)   # переживает перезапуск
            stats = self._day_stats(local.date())
            stats["drafts_waiting"] = len(self.store.by_status("draft"))
            await self._safe(self.notifier.digest(stats))
        await self.profile_updated()
        await self._retry_failed()

    def _ai_down(self) -> bool:
        return any(p == "ai" for p, _ in self.store.open_incidents())

    async def _retry_failed(self) -> None:
        """R21: строки, не получившие письмо из-за сбоя нейросети/письма или прерванной генерации за последние
        24 ч, один раз проходят конвейер заново (дедупликация и резерв — как в _recheck), когда нейросеть жива."""
        since = self.clock() - RECHECK_WINDOW
        for snap in self.store.by_status("failed"):
            if self._ai_down():
                return
            row = self.store.get(snap.id)
            key = f"retried:{row.id}"
            if (row.status != "failed" or row.created_at < since or row.reason.startswith(LOAD_FAILED)
                    or row.reason == GONE or self.store.get_meta(key) or self._paused_now(row.platform)):
                continue
            if row.id not in self._listings and row.platform not in self.sources:
                continue
            try:
                l = self._listings.get(row.id) or await self._detail(row.platform, self._row_listing(row), check_pause=True)
            except Paused:
                continue
            except ListingGoneError:   # сняли, пока ждали повтора: площадка исправна, повторять нечего
                self.store.set_status(row.id, "failed", reason=GONE)
                log.info("%s: #%s не повторяем — объявление снято с сайта", row.platform, row.id)
                continue
            except BlockedError as e:   # площадка на паузе; повторим после неё, если 24 ч не прошли
                await self._block(row.platform, e.kind, str(e))
                continue
            except SourceBroken as e:
                await self._block(row.platform, "broken", str(e))
                continue
            except Exception:
                self.store.set_meta(key, self.clock().isoformat())
                log.exception("%s: объявление #%s не загрузилось", row.platform, row.id)
                continue
            self.store.set_meta(key, self.clock().isoformat())   # один раз, даже если повтор снова не удастся
            self._listings[row.id] = l
            await self._recheck(row.id, l, expect="failed")

    async def profile_updated(self) -> None:
        """Профиль заполнен: объявления, отложенные за последние 24 ч, один раз проходят конвейер."""
        if not profile_complete(self.cfg["profile"]) or self._recheck_lock.locked():
            return   # уже идёт (цикл обслуживания или бот) — второй проход не нужен
        async with self._recheck_lock:
            await self._profile_updated()

    async def _profile_updated(self) -> None:
        since = self.clock() - RECHECK_WINDOW
        for snap in self.store.by_status("seen"):
            row = self.store.get(snap.id)   # список устарел, пока шли await — проверяем строку заново
            if (row.status != "seen" or row.reason != NO_PROFILE or row.created_at < since
                    or self._paused_now(row.platform)):
                continue
            if row.id not in self._listings and row.platform not in self.sources:
                continue
            self.store.set_status(row.id, "seen", reason="")   # забрали: один раз, даже если дальше сбой
            try:
                l = self._listings.get(row.id) or await self._detail(row.platform, self._row_listing(row), check_pause=True)
            except Paused:
                self.store.set_status(row.id, "seen", reason=NO_PROFILE)   # вернуть: заберём в следующий раз
                continue
            except BlockedError as e:
                await self._block(row.platform, e.kind, str(e))
                continue
            except SourceBroken as e:
                await self._block(row.platform, "broken", str(e))
                continue
            except Exception:
                log.exception("%s: объявление #%s не загрузилось", row.platform, row.id)
                continue
            self._listings[row.id] = l
            await self._recheck(row.id, l)

    async def _recheck(self, id: int, l: Listing, expect: str = "seen", force_draft: bool = False) -> str:
        """Дедупликация уже записанной строки и резерв — как шаг 3 в process(), без await до резерва."""
        row = self.store.get(id)
        if row.status != expect:   # строку уже обработал другой вызов — не трогаем
            return row.status
        warm = estimate_warm(l)[0]
        # find_dedup сначала находит саму строку по (platform, ext_id) — ищем по адресу с подменённым ext_id;
        # сама строка в seen/failed не блокирующая, поэтому совпасть с собой не может
        probe = dataclasses.replace(l, ext_id=l.ext_id + "#recheck")
        d = self.store.find_dedup(probe, warm, self.cfg["dedup_days"])
        if d.match_id == id:   # на всякий случай: совпадение с собой — не дубль
            log.error("recheck #%s совпал сам с собой — пропущен", id)
            return row.status
        if d.kind == "exact":
            self.store.set_status(id, "duplicate", reason=f"как #{d.match_id}")
            return "duplicate"
        if d.kind == "similar" and self._filtered(id, l):
            return "filtered"
        if d.kind == "similar":
            self.store.set_status(id, "ask_dup", reason=f"похоже на #{d.match_id}")
            await self._safe(self.notifier.ask_duplicate(self.store.get(id), self.store.get(d.match_id)))
            return "ask_dup"
        return await self._continue(id, l, force_draft)

    def _filtered(self, id: int, l: Listing) -> bool:
        r = check(l, self.cfg["filters"], self.cfg["profile"])
        if not r.ok:
            self.store.set_status(id, "filtered", reason=r.reason)
        return not r.ok

    def _poll_delay(self, platform: str) -> float:
        p = self.cfg["platforms"][platform]
        return poll_interval(to_local(self.clock()), tuple(p["poll"]), tuple(self.cfg["night_poll"]))

    async def _loop(self, name: str, once, delay) -> None:
        wake = self._wake.get(name)
        while not self._stop.is_set():
            if wake:
                wake.clear()
            try:
                await once()
            except Exception:
                log.exception("цикл %s: сбой", name)
            d = delay()
            if self.active(name):
                log.debug("%s: следующий опрос через %.0f с", name, d)
            waits = [asyncio.ensure_future(self._stop.wait())] + ([asyncio.ensure_future(wake.wait())] if wake else [])
            _, pending = await asyncio.wait(waits, timeout=d, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()

    async def _poll_if_active(self, platform: str) -> None:
        if self.active(platform):
            await self.poll_once(platform)   # с экземпляра: окно подменяет poll_once, чтобы запомнить время

    async def run(self) -> None:
        self._stop.clear()
        await self.startup()
        loops = []
        # цикл у каждой поддерживаемой площадки: выключенная спит и оживает, когда её включат (wake)
        for p, pc in self.cfg["platforms"].items():
            if p not in self.sources:
                if pc.get("enabled") and not self.selective:
                    log.info("%s: площадка ещё не поддерживается — пропущена", p)
                continue
            self._wake[p] = asyncio.Event()
            loops.append(self._loop(p, lambda p=p: self._poll_if_active(p), lambda p=p: self._poll_delay(p)))
        loops.append(self._loop("send", self.send_queued_once, lambda: SEND_EVERY))
        if self.mail is not None:
            loops.append(self._loop("mail", self.mail_once, lambda: self.cfg["mail_poll_sec"]))
        loops.append(self._loop("maintenance", self.maintenance_once, lambda: MAINTENANCE_EVERY))
        await asyncio.gather(*loops)

    async def stop(self) -> None:
        self._stop.set()
