"""
Конвейер: опрос площадок → дедупликация → фильтр → письмо → отправка или черновик,
ответы из почты, чистка черновиков и вечерняя сводка.

Весь конвейер работает в одном цикле asyncio: участок «find_dedup → add → блокирующий статус» идёт
без await, поэтому две площадки с одной квартирой не получат два письма.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Protocol

import core
from ai import AIError
from filters import check, estimate_warm
from letter import LetterError, generate_letter
from models import Listing, profile_complete
from sources.base import TZ, BlockedError, MissingFieldError, RateLimiter, SourceBroken, poll_interval
from store import Row, Store, contact_key

log = logging.getLogger("wohnung")

SEND_EVERY = 10          # как часто смотреть очередь отправки, сек
MAINTENANCE_EVERY = 60   # чистка черновиков и сводка, сек
MISSING_PROFILE_EVERY = timedelta(hours=24)
GENERATING = "generating"   # reason у строки, зарезервированной на время генерации письма


class Notifier(Protocol):
    async def draft(self, row: Row, text: str, warm_estimated: bool) -> int: ...   # message_id
    async def ask_duplicate(self, row: Row, match: Row) -> None: ...
    async def ask_stuck(self, row: Row) -> None: ...
    async def reply(self, row: Row | None, r) -> None: ...   # None — «не нашёл, к какому объявлению»
    async def incident(self, platform: str, kind: str, detail: str) -> None: ...
    async def missing_profile(self) -> None: ...
    async def digest(self, stats: dict) -> None: ...
    async def remove_draft(self, row: Row) -> None: ...


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
                 mail=None, clock=_utcnow):
        self.cfg, self.store, self.ai, self.sources = cfg, store, ai, sources
        self.browser, self.notifier, self.mail = browser, notifier, mail
        self.clock = clock   # наивный UTC, как в Store
        for src in sources.values():
            src.filters = cfg["filters"]
        self._paused: set[str] = set()
        self._paused_all = False
        self._listings: dict[int, Listing] = {}      # подробности объявления для отправки и перегенерации
        self._draft_at: dict[int, datetime] = {}     # когда показан черновик (для draft_ttl_hours)
        self._asked_stuck: set[int] = set()
        self._missing_profile_at: datetime | None = None
        self._digest_day = None
        self._stop = asyncio.Event()

    # ---------- настройки и состояние ----------

    def mode_for(self, platform: str) -> str:
        return (self.cfg["platforms"].get(platform) or {}).get("mode") or self.cfg["mode"]

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
        else:
            self._paused.discard(platform)
        for p in names:
            self.store.close_incidents(p)

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

    # ---------- обработка объявления ----------

    def _row_listing(self, row: Row) -> Listing:
        return self._listings.get(row.id) or Listing(row.platform, row.ext_id, row.url, row.title)

    async def _detail(self, platform: str, l: Listing) -> Listing:
        async with self.browser.lock(platform):
            page = await self.browser.page(platform)
            return await self.sources[platform].fetch_detail(page, l)

    def _known(self, l: Listing, match_id: int) -> Row | None:
        row = self.store.get(match_id)
        return row if (row.platform, row.ext_id) == (l.platform, l.ext_id) else None

    def _mark_duplicate(self, l: Listing, warm: float | None, match_id: int) -> str:
        id = self.store.add(l, warm)
        self.store.set_status(id, "duplicate", reason=f"как #{match_id}")
        return "duplicate"

    async def process(self, platform: str, l: Listing) -> str:
        days = self.cfg["dedup_days"]
        # 1. до загрузки подробностей: уже знакомое объявление или явный дубль
        d = self.store.find_dedup(l, estimate_warm(l)[0], days)
        if d.kind == "exact":
            known = self._known(l, d.match_id)
            return known.status if known else self._mark_duplicate(l, estimate_warm(l)[0], d.match_id)
        # 2. подробности (BlockedError/SourceBroken уходят в poll_once)
        l = await self._detail(platform, l)
        warm, est = estimate_warm(l)
        # 3. от этой проверки до резерва — ни одного await
        d = self.store.find_dedup(l, warm, days)
        if d.kind == "exact":
            known = self._known(l, d.match_id)
            return known.status if known else self._mark_duplicate(l, warm, d.match_id)
        id = self.store.add(l, warm)
        self._listings[id] = l
        if d.kind == "similar":
            self.store.set_status(id, "ask_dup", reason=f"похоже на #{d.match_id}")
            await self._safe(self.notifier.ask_duplicate(self.store.get(id), self.store.get(d.match_id)))
            return "ask_dup"
        return await self._continue(id, l)

    async def _continue(self, id: int, l: Listing) -> str:
        """Шаги 4–8. До резерва (queued/generating) нет await."""
        profile = self.cfg["profile"]
        r = check(l, self.cfg["filters"], profile)
        if not r.ok:
            self.store.set_status(id, "filtered", reason=r.reason)
            return "filtered"
        if not profile_complete(profile):
            self.store.set_status(id, "seen", reason="профиль не заполнен")
            now = self.clock()
            if self._missing_profile_at is None or now - self._missing_profile_at >= MISSING_PROFILE_EVERY:
                self._missing_profile_at = now
                await self._safe(self.notifier.missing_profile())
            return "seen"
        if self.store.contact_sent_count(contact_key(l.contact_name), 7) >= self.cfg["contact_cap_7d"]:
            self.store.set_status(id, "filtered", reason="лимит контакта")
            return "filtered"
        self.store.set_status(id, "queued", reason=GENERATING)   # резерв: блокирует дубль на другой площадке
        return await self._write_letter(id, l, r.warm_estimated)

    async def _write_letter(self, id: int, l: Listing, warm_estimated: bool, wish: str = "",
                            force_draft: bool = False) -> str:
        profile = self.cfg["profile"]
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
            self.store.set_status(id, "filtered", reason=res.skip_reason or "SKIP")
            return "filtered"
        missing = [f for f in l.required_fields if profile.get(f) in (None, "")]
        if force_draft or missing or self.mode_for(l.platform) == "confirm":
            reason = f"нет полей: {', '.join(missing)}" if missing else ""
            await self._to_draft(id, res.text, warm_estimated, reason)
            return "draft"
        self.store.set_status(id, "queued", letter=res.text, reason="")
        return "queued"

    async def _to_draft(self, id: int, text: str, warm_estimated: bool, reason: str = "") -> None:
        self.store.set_status(id, "draft", letter=text, reason=reason)
        self._draft_at[id] = self.clock()
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
        src = self.sources.get(platform)
        if (row.status != "queued" or row.reason == GENERATING or not row.letter or src is None
                or self._paused_now(platform) or not profile_complete(self.cfg["profile"])):
            return
        async with self.browser.lock(platform):
            wait = self._limiter(platform).wait_seconds(self.clock())
            if wait is None or wait > 0:
                return
            self.store.set_status(id, "sending")
            l = self._row_listing(row)
            try:
                page = await self.browser.page(platform)
                await src.send(page, l, row.letter, self.cfg["profile"])
            except BlockedError as e:
                self.store.set_status(id, "queued")
                await self._block(platform, e.kind, str(e))
            except MissingFieldError as e:
                await self._to_draft(id, row.letter, False, f"нет поля: {e.field}")
            except Exception as e:
                log.exception("%s: отправка #%s не удалась", platform, id)
                self.store.set_status(id, "failed", reason=f"отправка: {e}"[:300])
                await self._incident(platform, "send", str(e))
            else:
                self.store.set_status(id, "sent", sent_at=self.clock())   # до снятия блокировки площадки
                log.info("%s: письмо #%s отправлено", platform, id)

    async def send_queued_once(self) -> None:
        """Очередь по created_at; на площадку — одна попытка за проход."""
        rows = sorted(self.store.by_status("queued"), key=lambda r: (r.created_at, r.id))
        tried: set[str] = set()
        for row in rows:
            if row.platform in tried or row.reason == GENERATING or not row.letter:
                continue
            if row.platform not in self.sources or self._paused_now(row.platform):
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
        if row.status not in ("draft", "failed"):
            return
        self.store.set_status(id, "queued", reason=GENERATING)
        try:
            l = self._listings.get(id) or await self._detail(row.platform, self._row_listing(row))
        except Exception as e:
            self.store.set_status(id, "failed", reason=f"объявление не загрузилось: {e}"[:300])
            return
        self._listings[id] = l
        await self._write_letter(id, l, estimate_warm(l)[1], wish, force_draft=True)

    async def skip(self, id: int) -> None:
        self.store.set_status(id, "skipped")

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
                self.store.set_status(id, "failed", reason=f"объявление не загрузилось: {e}"[:300])
                return
            self._listings[id] = l
        await self._continue(id, l)

    async def resolve_stuck(self, id: int, was_sent: bool) -> None:
        if self.store.get(id).status != "sending":
            return
        if was_sent:
            self.store.set_status(id, "sent", sent_at=self.clock())
        else:
            self.store.set_status(id, "queued")

    # ---------- циклы ----------

    async def startup(self) -> None:
        for platform, _ in self.store.open_incidents():   # открытый случай держит паузу и после перезапуска
            if platform in self.cfg["platforms"]:
                self._paused.add(platform)
        for row in self.store.by_status("queued"):
            if row.reason == GENERATING:
                self.store.set_status(row.id, "failed", reason="прервано при генерации")
        for row in self.store.by_status("sending"):   # не отправляем повторно — спрашиваем владельца
            if row.id not in self._asked_stuck:
                self._asked_stuck.add(row.id)
                await self._safe(self.notifier.ask_stuck(row))

    async def poll_once(self, platform: str) -> None:
        src = self.sources.get(platform)
        if src is None or self._paused_now(platform):
            return
        try:
            async with self.browser.lock(platform):
                listings = await src.search(await self.browser.page(platform))
        except BlockedError as e:
            await self._block(platform, e.kind, str(e))
            return
        except SourceBroken as e:
            await self._block(platform, "broken", str(e))
            return
        except Exception:
            log.exception("%s: поиск не удался", platform)
            return
        for l in listings:
            if self._paused_now(platform):
                break
            try:
                await self.process(platform, l)
            except BlockedError as e:
                await self._block(platform, e.kind, str(e))
                break
            except SourceBroken as e:
                await self._block(platform, "broken", str(e))
                break
            except Exception:
                log.exception("%s: объявление %s не обработано", platform, l.ext_id)

    async def mail_once(self) -> None:
        if self.mail is None:
            return
        for r in await asyncio.to_thread(self.mail.fetch_new):
            row = self.store.match_reply(r.platform, r.ext_id, r.url, r.contact)
            if row:
                self.store.add_reply(row.id, r.excerpt, r.link)
            await self._safe(self.notifier.reply(row, r))

    async def maintenance_once(self) -> None:
        now = self.clock()
        ttl = timedelta(hours=self.cfg["draft_ttl_hours"])
        for row in self.store.by_status("draft"):
            if now - self._draft_at.get(row.id, row.created_at) >= ttl:
                self.store.set_status(row.id, "skipped", reason="черновик устарел")
                self._draft_at.pop(row.id, None)
                await self._safe(self.notifier.remove_draft(row))
        local = to_local(now)
        if local.hour == self.cfg["digest_hour"] and self._digest_day != local.date():
            self._digest_day = local.date()
            await self._safe(self.notifier.digest(self._day_stats(local.date())))

    def _poll_delay(self, platform: str) -> float:
        p = self.cfg["platforms"][platform]
        return poll_interval(to_local(self.clock()), tuple(p["poll"]), tuple(self.cfg["night_poll"]))

    async def _loop(self, name: str, once, delay) -> None:
        while not self._stop.is_set():
            try:
                await once()
            except Exception:
                log.exception("цикл %s: сбой", name)
            try:
                await asyncio.wait_for(self._stop.wait(), delay())
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        self._stop.clear()
        await self.startup()
        loops = []
        for p, pc in self.cfg["platforms"].items():
            if not pc.get("enabled"):
                continue
            if p not in self.sources:
                log.info("%s: площадка ещё не поддерживается — пропущена", p)
                continue
            loops.append(self._loop(p, lambda p=p: self.poll_once(p), lambda p=p: self._poll_delay(p)))
        loops.append(self._loop("send", self.send_queued_once, lambda: SEND_EVERY))
        if self.mail is not None:
            loops.append(self._loop("mail", self.mail_once, lambda: self.cfg["mail_poll_sec"]))
        loops.append(self._loop("maintenance", self.maintenance_once, lambda: MAINTENANCE_EVERY))
        await asyncio.gather(*loops)

    async def stop(self) -> None:
        self._stop.set()
