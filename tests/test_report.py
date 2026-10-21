from datetime import date, datetime

import openpyxl

from models import Listing
from report import export_report
from store import Store


def _store(tmp_path):
    clock = {"now": datetime(2026, 10, 20, 12, 0)}
    return Store(tmp_path / "t.db", clock=lambda: clock["now"]), clock


def test_export_report(tmp_path):
    store, clock = _store(tmp_path)
    ids = [store.add(Listing("kleinanzeigen", str(i), f"https://x/{i}", f"=Wohnung {i}"), 700.0 + i)
           for i in range(3)]
    store.set_status(ids[0], "sent", letter="Hallo", sent_at=datetime(2026, 10, 20, 12, 0))
    store.set_status(ids[1], "sent", letter="Hallo", sent_at=datetime(2026, 10, 21, 9, 0))
    store.set_status(ids[2], "filtered", reason="цена")
    store.add_reply(ids[1], "Gerne, Besichtigung am Montag", "https://x/chat")
    path = tmp_path / "out.xlsx"
    assert export_report(store, path, None) == 2
    rows = list(openpyxl.load_workbook(path).active.iter_rows(values_only=True))
    assert len(rows) == 3
    assert rows[0][:2] == ("Дата", "Площадка")
    assert rows[1][-1] == "нет" and rows[2][-1] == "да"
    assert rows[1][2] == "\u200b=Wohnung 0"   # заголовок «=…» не должен стать формулой Excel
    # since: только отправленные не раньше этой даты (местной)
    assert export_report(store, tmp_path / "s.xlsx", date(2026, 10, 21)) == 1
