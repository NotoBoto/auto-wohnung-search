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


@dataclass
class Row:
    id: int; platform: str; ext_id: str; url: str; title: str; status: str
    letter: str; warm: float | None; contact_key: str; created_at: datetime; sent_at: datetime | None
    reason: str; draft_msg_id: int | None


@dataclass
class DedupResult:
    kind: Literal["new", "exact", "similar"]
    match_id: int | None = None


def _street_key(street: str) -> str:
    s = (street or "").lower().replace("straße", "str").replace("strasse", "str").replace("str.", "str")
    return re.sub(r"[\s\-]+", "", s)


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
    rooms REAL, area REAL, UNIQUE (platform, ext_id));
CREATE TABLE IF NOT EXISTS replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT, listing_id INTEGER NOT NULL,
    excerpt TEXT NOT NULL DEFAULT '', link TEXT NOT NULL DEFAULT '', received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL, kind TEXT NOT NULL,
    opened_at TEXT NOT NULL, closed_at TEXT);
"""


class Store:
    def __init__(self, path: Path | str, clock: Callable[[], datetime] = _utcnow):
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._clock = clock
        with self._lock:
            self._db.executescript(_SCHEMA)
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
                   r["reason"], r["draft_msg_id"])

    def add(self, l: Listing, warm: float | None) -> int:
        key = addr_key(l)
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO listings (platform, ext_id, url, title, warm, contact_key, created_at,"
                " addr_key, has_street, postcode, district, rooms, area) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (l.platform, l.ext_id, l.url, l.title, warm, contact_key(l.contact_name), _ts(self._clock()),
                 key, int(not key.startswith("|")), l.postcode, l.district, l.rooms, l.area))
            self._db.commit()
            if cur.rowcount:
                return cur.lastrowid
            return self._db.execute("SELECT id FROM listings WHERE platform=? AND ext_id=?",
                                    (l.platform, l.ext_id)).fetchone()["id"]

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
        sets, args = ["status=?"], [status]
        for k, v in fields.items():
            sets.append(f"{k}=?")
            args.append(_ts(v) if k == "sent_at" else v)
        if status == "sent" and "sent_at" not in fields:   # время отправки фиксируем автоматически
            sets.append("sent_at=COALESCE(sent_at, ?)")
            args.append(_ts(self._clock()))
        self._write(f"UPDATE listings SET {', '.join(sets)} WHERE id=?", (*args, id))

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
                if r["addr_key"] == key:
                    return DedupResult("exact", r["id"])
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

    def contact_sent_count(self, contact_key: str, days: int = 7) -> int:
        if not contact_key:
            return 0
        since = _ts(self._clock() - timedelta(days=days))
        return self._db.execute("SELECT COUNT(*) FROM listings WHERE status='sent' AND contact_key=? AND sent_at>=?",
                                (contact_key, since)).fetchone()[0]

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
        lo = _ts(datetime(day.year, day.month, day.day))
        hi = _ts(datetime(day.year, day.month, day.day) + timedelta(days=1))
        db = self._db

        def count(table: str, col: str, extra: str = "") -> int:
            return db.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}>=? AND {col}<? {extra}", (lo, hi)).fetchone()[0]

        filtered = {r["reason"]: r["n"] for r in db.execute(
            "SELECT reason, COUNT(*) n FROM listings WHERE status='filtered' AND created_at>=? AND created_at<?"
            " GROUP BY reason", (lo, hi))}
        return {"found": count("listings", "created_at"),
                "sent": count("listings", "sent_at", "AND status='sent'"),
                "filtered": filtered,
                "duplicates": count("listings", "created_at", "AND status='duplicate'"),
                "replies": count("replies", "received_at"),
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

    def close_incidents(self, platform: str) -> None:
        self._write("UPDATE incidents SET closed_at=? WHERE platform=? AND closed_at IS NULL",
                    (_ts(self._clock()), platform))

    def open_incidents(self) -> list[tuple[str, str]]:
        return [(r["platform"], r["kind"]) for r in self._db.execute(
            "SELECT platform, kind FROM incidents WHERE closed_at IS NULL ORDER BY id")]
