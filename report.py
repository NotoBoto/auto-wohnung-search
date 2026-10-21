"""Отчёт об отправленных письмах: строки для таблицы окна и выгрузка в Excel."""
from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook

from pipeline import to_local, to_utc
from store import Store, _dt

HEADERS = ("Дата", "Площадка", "Заголовок", "Цена (тёплая), €", "Статус", "Ответ")
PLATFORM_NAMES = {"kleinanzeigen": "Kleinanzeigen", "wggesucht": "WG-Gesucht",
                  "immowelt": "Immowelt", "immoscout": "ImmoScout24"}


def report_rows(store: Store, since: date | None = None) -> list[tuple]:
    """Отправленные письма (новые первыми): (дата, площадка, заголовок, цена, статус, ответ, ссылка).
    since — местная дата Бонна, с которой смотрим."""
    sql = ("SELECT l.sent_at, l.platform, l.title, l.warm, l.url,"
           " EXISTS(SELECT 1 FROM replies r WHERE r.listing_id=l.id) AS replied"
           " FROM listings l WHERE l.status='sent' AND l.sent_at IS NOT NULL")
    args: list = []
    if since:
        sql += " AND l.sent_at >= ?"
        args.append(to_utc(datetime(since.year, since.month, since.day)).strftime("%Y-%m-%dT%H:%M:%S.%f"))
    # у Store нет списка для отчёта; читаем его соединение так же, как его собственные методы чтения
    found = store._db.execute(sql + " ORDER BY l.sent_at DESC, l.id DESC", args).fetchall()
    return [(to_local(_dt(r["sent_at"])).strftime("%d.%m.%Y %H:%M"), PLATFORM_NAMES.get(r["platform"], r["platform"]),
             r["title"], "" if r["warm"] is None else round(r["warm"]), "отправлено",
             "да" if r["replied"] else "нет", r["url"]) for r in found]


def _cell(v):
    # Excel считает формулой всё, что начинается с = + - @: невидимый пробел делает значение текстом
    return "\u200b" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def export_report(store: Store, path: Path, since: date | None) -> int:
    """Сохраняет отправленные письма в .xlsx (по порядку времени); возвращает число строк."""
    rows = report_rows(store, since)
    wb = Workbook()
    ws = wb.active
    ws.title = "Отчёт"
    ws.append(HEADERS)
    for r in reversed(rows):
        ws.append([_cell(v) for v in r[:6]])
    for col, width in zip("ABCDEF", (17, 15, 60, 16, 12, 8)):
        ws.column_dimensions[col].width = width
    wb.save(str(path))
    return len(rows)
