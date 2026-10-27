import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal

from models import Listing

STATUSES = ("seen", "filtered", "duplicate", "ask_dup", "draft", "queued", "sending", "sent", "skipped", "failed")
BLOCKING = ("draft", "queued", "sending", "sent", "ask_dup")   # эти статусы блокируют повторное письмо
_FIELDS = ("letter", "reason", "sent_at", "draft_msg_id")
_TS = "%Y-%m-%dT%H:%M:%S.%f"   # фиксированная ширина: строки сравниваются как время
# переходы в эти статусы — в журнал окна (INFO); остальные (отсев, дубли, служебные) — только в log.txt (DEBUG)
_LOG_INFO = ("draft", "queued", "sent", "skipped", "failed", "ask_dup")

log = logging.getLogger("wohnung")


@dataclass
class Row:
    id: int; platform: str; ext_id: str; url: str; title: str; status: str
    letter: str; warm: float | None; contact_key: str; created_at: datetime; sent_at: datetime | None
    reason: str; draft_msg_id: int | None
    street: str = ""; postcode: str = ""; district: str = ""   # для ссылки на карту


@dataclass
class DedupResult:
    kind: Literal["new", "exact", "similar"]
    match_id: int | None = None


def _street_key(street: str) -> str:
    s = (street or "").lower().replace("straße", "str").replace("strasse", "str").replace("str.", "str")
    # хвост с городом/PLZ ("..., Bonn", "53115 Bonn") не часть адреса
    parts = [p for p in re.split(r"[\s,;]+", s) if p and p != "bonn" and not re.fullmatch(r"\d{5}", p)]
    return re.sub(r"[^\w]|_", "", "".join(parts))


def _street_name(street_key: str) -> str:
    return re.sub(r"\d+\w*$", "", street_key)   # без номера дома


def addr_key(l: Listing) -> str:
    street = _street_key(l.street)
    return f"{street}|{l.postcode}" if street else f"|{l.postcode}|{l.district}"


_NOISE = {"gmbh", "ag", "immobilien", "hausverwaltung", "frau", "herr"}


def contact_key(name: str) -> str:
    words = re.sub(r"[,.;]", " ", (name or "").lower()).split()
    return " ".join(w for w in words if w not in _NOISE)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ts(d: datetime | None) -> str | None:
    return d.strftime(_TS) if d else None


def _dt(s: str | None) -> datetime | None:
    return datetime.strptime(s, _TS) if s else None


def _compatible(a, b, tol: float) -> bool:
    return a is None or b is None or abs(a - b) <= tol


_SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL, ext_id TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'seen',
    letter TEXT NOT NULL DEFAULT '', warm REAL, contact_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, sent_at TEXT, reason TEXT NOT NULL DEFAULT '', draft_msg_id INTEGER,
    addr_key TEXT NOT NULL DEFAULT '', has_street INTEGER NOT NULL DEFAULT 0,
    postcode TEXT NOT NULL DEFAULT '', district TEXT NOT NULL DEFAULT '',
    rooms REAL, area REAL, street TEXT NOT NULL DEFAULT '', UNIQUE (platform, ext_id));
CREATE TABLE IF NOT EXISTS replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT, listing_id INTEGER NOT NULL,
    excerpt TEXT NOT NULL DEFAULT '', link TEXT NOT NULL DEFAULT '', received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL, kind TEXT NOT NULL,
    opened_at TEXT NOT NULL, closed_at TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: Path | str, clock: Callable[[], datetime] = _utcnow):
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._clock = clock
        with self._lock:
            self._db.executescript(_SCHEMA)
            # база из прошлой версии: колонки street ещё нет
            cols = {r["name"] for r in self._db.execute("PRAGMA table_info(listings)")}
            if "street" not in cols:
                self._db.execute("ALTER TABLE listings ADD COLUMN street TEXT NOT NULL DEFAULT ''")
            self._db.commit()

    def _write(self, sql: str, args=()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    @staticmethod
    def _row(r: sqlite3.Row) -> Row:
        return Row(r["id"], r["platform"], r["ext_id"], r["url"], r["title"], r["status"], r["letter"],
                   r["warm"], r["contact_key"], _dt(r["created_at"]), _dt(r["sent_at"]),
                   r["reason"], r["draft_msg_id"], r["street"], r["postcode"], r["district"])

    def add(self, l: Listing, warm: float | None) -> int:
        key = addr_key(l)
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO listings (platform, ext_id, url, title, warm, contact_key, created_at,"
                " addr_key, has_street, postcode, district, rooms, area, street) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (l.platform, l.ext_id, l.url, l.title, warm, contact_key(l.contact_name), _ts(self._clock()),
                 key, int(not key.startswith("|")), l.postcode, l.district, l.rooms, l.area, l.street or ""))
            self._db.commit()
            if cur.rowcount:
                log.debug("%s #%s новое: %s | тёплая %s | %s | %s", l.platform, cur.lastrowid, l.title,
                          warm, " ".join(filter(None, (l.street, l.postcode, l.district))) or "адрес ?", l.url)
                return cur.lastrowid
            return self._db.execute("SELECT id FROM listings WHERE platform=? AND ext_id=?",
                                    (l.platform, l.ext_id)).fetchone()["id"]

    def has(self, platform: str, ext_id: str) -> bool:
        return self._db.execute("SELECT 1 FROM listings WHERE platform=? AND ext_id=?",
                                (platform, ext_id)).fetchone() is not None

    def status_of(self, platform: str, ext_id: str) -> str | None:
        r = self._db.execute("SELECT status FROM listings WHERE platform=? AND ext_id=?", (platform, ext_id)).fetchone()
        return r["status"] if r else None

    def get(self, id: int) -> Row:
        r = self._db.execute("SELECT * FROM listings WHERE id=?", (id,)).fetchone()
        if r is None:
            raise KeyError(id)
        return self._row(r)

    def set_status(self, id: int, status: str, **fields) -> None:
        if status not in STATUSES:
            raise ValueError(f"неизвестный статус: {status}")
        bad = set(fields) - set(_FIELDS)
        if bad:
            raise ValueError(f"неизвестные поля: {sorted(bad)}")
        if status == "sent" and fields.get("sent_at") is None:
            fields.pop("sent_at", None)   # sent_at=None трактуем как «не задано»: иначе строка выпадет из лимитов
        sets, args = ["status=?"], [status]
        for k, v in fields.items():
            sets.append(f"{k}=?")
            args.append(_ts(v) if k == "sent_at" else v)
        if status == "sent" and "sent_at" not in fields:   # время отправки фиксируем автоматически
            sets.append("sent_at=COALESCE(sent_at, ?)")
            args.append(_ts(self._clock()))
        old = self._db.execute("SELECT platform, title, status, reason FROM listings WHERE id=?", (id,)).fetchone()
        self._write(f"UPDATE listings SET {', '.join(sets)} WHERE id=?", (*args, id))
        reason = fields.get("reason")
        if old and (old["status"] != status or (reason is not None and reason != old["reason"])):
            level = logging.INFO if status in _LOG_INFO and reason != "generating" else logging.DEBUG
            log.log(level, "%s #%s %s → %s%s | %s", old["platform"], id, old["status"], status,
                    f" ({reason})" if reason else "", old["title"])

    def find_dedup(self, l: Listing, warm: float | None, days: int = 60) -> DedupResult:
        same = self._db.execute("SELECT id FROM listings WHERE platform=? AND ext_id=?",
                                (l.platform, l.ext_id)).fetchone()
        if same:
            return DedupResult("exact", same["id"])
        cutoff = _ts(self._clock() - timedelta(days=days))
        marks = ",".join("?" * len(BLOCKING))
        rows = self._db.execute(
            f"SELECT * FROM listings WHERE status IN ({marks}) AND COALESCE(sent_at, created_at) >= ?"
            " ORDER BY COALESCE(sent_at, created_at) DESC, id DESC", (*BLOCKING, cutoff)).fetchall()
        key = addr_key(l)
        has_street = not key.startswith("|")
        similar: int | None = None
        for r in rows:
            if not (_compatible(l.area, r["area"], 2) and _compatible(warm, r["warm"], 30)
                    and (l.rooms is None or r["rooms"] is None or l.rooms == r["rooms"])):
                continue
            if has_street and r["has_street"]:
                sk, pl = key.split("|")[:2]
                rk, pr = r["addr_key"].split("|")[:2]
                loose = pl == pr or not pl or not pr   # пустой PLZ не опровергает совпадение
                if sk == rk and pl == pr:
                    return DedupResult("exact", r["id"])
                if loose and similar is None and (sk == rk or (_street_name(sk) and _street_name(sk) == _street_name(rk))):
                    similar = r["id"]
            elif l.postcode and l.postcode == r["postcode"] and similar is None:
                similar = r["id"]
        return DedupResult("similar", similar) if similar is not None else DedupResult("new", None)

    def sent_count(self, platform: str, since: datetime) -> int:
        return self._db.execute("SELECT COUNT(*) FROM listings WHERE status='sent' AND platform=? AND sent_at>=?",
                                (platform, _ts(since))).fetchone()[0]

    def last_sent_at(self, platform: str) -> datetime | None:
        r = self._db.execute("SELECT MAX(sent_at) FROM listings WHERE status='sent' AND platform=?",
                             (platform,)).fetchone()
        return _dt(r[0])

    def contact_count(self, contact_key: str, days: int = 7, statuses=("sent", "sending", "queued", "draft"),
                      exclude_id: int | None = None) -> int:
        """Письма контакту за days дней: отправленные и ещё ждущие (очередь, черновик, отправка)."""
        if not contact_key:
            return 0
        since = _ts(self._clock() - timedelta(days=days))
        marks = ",".join("?" * len(statuses))
        return self._db.execute(
            f"SELECT COUNT(*) FROM listings WHERE status IN ({marks}) AND contact_key=?"
            " AND COALESCE(sent_at, created_at)>=? AND id!=?",
            (*statuses, contact_key, since, -1 if exclude_id is None else exclude_id)).fetchone()[0]

    def recent_openings(self, n: int = 30) -> list[str]:
        rows = self._db.execute("SELECT letter FROM listings WHERE status='sent' AND letter!=''"
                                " ORDER BY sent_at DESC, id DESC LIMIT ?", (n,)).fetchall()
        out = []
        for r in rows:
            m = re.search(r"[.!?](?=\s|$)", r["letter"])
            out.append((r["letter"][:m.end()] if m else r["letter"]).strip())
        return out

    def by_status(self, status: str) -> list[Row]:
        return [self._row(r) for r in self._db.execute(
            "SELECT * FROM listings WHERE status=? ORDER BY id", (status,))]

    def match_reply(self, platform: str, ext_id: str = "", url: str = "", contact: str = "") -> Row | None:
        order = " ORDER BY COALESCE(sent_at, created_at) DESC, id DESC LIMIT 1"
        base = "SELECT * FROM listings WHERE status='sent' AND platform=? AND "
        attempts = []
        if ext_id:
            attempts.append(("ext_id=?", ext_id))
        if url:
            attempts.append(("url=?", url))
        if contact_key(contact):
            attempts.append(("contact_key=?", contact_key(contact)))
        for cond, val in attempts:
            r = self._db.execute(base + cond + order, (platform, val)).fetchone()
            if r:
                return self._row(r)
        return None

    def add_reply(self, listing_id: int, excerpt: str, link: str) -> None:
        self._write("INSERT INTO replies (listing_id, excerpt, link, received_at) VALUES (?,?,?,?)",
                    (listing_id, excerpt, link, _ts(self._clock())))

    def day_stats(self, day: date) -> dict:
        start = datetime(day.year, day.month, day.day)
        return self.stats_between(start, start + timedelta(days=1))

    def stats_between(self, start: datetime, end: datetime) -> dict:
        """Сводка за [start, end) в наивном UTC; Engine передаёт границы местных суток."""
        lo, hi = _ts(start), _ts(end)
        db = self._db

        def count(table: str, col: str, extra: str = "") -> int:
            return db.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}>=? AND {col}<? {extra}", (lo, hi)).fetchone()[0]

        def events(name: str) -> int:
            return db.execute("SELECT COUNT(*) FROM meta WHERE key LIKE ? AND value>=? AND value<?",
                              (f"{name}_at:%", lo, hi)).fetchone()[0]

        filtered = {r["reason"]: r["n"] for r in db.execute(
            "SELECT reason, COUNT(*) n FROM listings WHERE status='filtered' AND created_at>=? AND created_at<?"
            " GROUP BY reason", (lo, hi))}
        return {"found": count("listings", "created_at"),
                "sent": count("listings", "sent_at", "AND status='sent'"),
                "filtered": filtered,
                "duplicates": count("listings", "created_at", "AND status='duplicate'"),
                "replies": count("replies", "received_at"),
                "expired": events("expired"), "skipped": events("skipped"),
                "failed": count("listings", "created_at", "AND status='failed'")}

    def open_incident(self, platform: str, kind: str) -> bool:
        with self._lock:
            if self._db.execute("SELECT 1 FROM incidents WHERE platform=? AND kind=? AND closed_at IS NULL",
                                (platform, kind)).fetchone():
                return False
            self._db.execute("INSERT INTO incidents (platform, kind, opened_at) VALUES (?,?,?)",
                             (platform, kind, _ts(self._clock())))
            self._db.commit()
            return True

    def close_incidents(self, platform: str, kind: str | None = None) -> None:
        """Закрывает открытые случаи площадки; с kind — только этого вида."""
        sql = "UPDATE incidents SET closed_at=? WHERE platform=? AND closed_at IS NULL"
        args = (_ts(self._clock()), platform)
        if kind is not None:
            sql, args = sql + " AND kind=?", (*args, kind)
        self._write(sql, args)

    def note(self, event: str, id: int) -> None:
        """Отметка времени события строки (expired, skipped) — для сводки: у статуса своего времени нет."""
        self.set_meta(f"{event}_at:{id}", _ts(self._clock()))

    def get_meta(self, key: str) -> str | None:
        r = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_meta(self, key: str, value: str) -> None:
        self._write("INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value))

    def open_incidents(self) -> list[tuple[str, str]]:
        return [(r["platform"], r["kind"]) for r in self._db.execute(
            "SELECT platform, kind FROM incidents WHERE closed_at IS NULL ORDER BY id")]
