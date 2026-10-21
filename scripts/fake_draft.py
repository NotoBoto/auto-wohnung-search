"""Ручная проверка бота без настоящих площадок: python scripts/fake_draft.py
Берёт bot_token из config.json, запускает бота с заглушкой вместо конвейера и шлёт владельцу фальшивый черновик
с кнопками. Ничего никуда не отправляется. Сначала напишите боту /start (так запоминается owner_chat_id).
Остановка: Ctrl+C."""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402
from bot import start_bot, stop_bot  # noqa: E402
from models import Listing  # noqa: E402
from store import Store  # noqa: E402


class FakeEngine:
    """Те же методы, что у Engine, но только меняют статус в тестовой базе."""

    def __init__(self, cfg, store):
        self.cfg, self.store, self.notifier = cfg, store, None
        self.paused: set = set()

    def mode_for(self, platform): return (self.cfg["platforms"].get(platform) or {}).get("mode") or self.cfg["mode"]
    def pause(self, platform): self.paused.add(platform or "*")
    def resume(self, platform): self.paused.discard(platform or "*")
    async def profile_updated(self): print("profile_updated")

    def status(self):
        return {"paused": sorted(p for p in self.paused if p != "*"), "paused_all": "*" in self.paused,
                "modes": {p: self.mode_for(p) for p in self.cfg["platforms"]}, "incidents": [],
                "drafts": len(self.store.by_status("draft")), "queued": 0,
                "today": self.store.stats_between(*_today())}

    async def approve(self, id, text=None):
        print("approve", id, repr(text))
        self.store.set_status(id, "sent", letter=text or self.store.get(id).letter)

    async def regenerate(self, id, wish=""):
        print("regenerate", id, repr(wish))
        row = self.store.get(id)
        await self.notifier.draft(row, f"(новый вариант, пожелание: {wish or 'нет'})\n" + row.letter, False)

    async def skip(self, id): print("skip", id); self.store.set_status(id, "skipped")
    async def resolve_duplicate(self, id, send): print("dup", id, send); self.store.set_status(id, "skipped")
    async def resolve_stuck(self, id, was_sent): print("stuck", id, was_sent)


def _today():
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    return now - timedelta(days=1), now + timedelta(days=1)


async def main() -> None:
    cfg = core.load_config()
    if not cfg["bot_token"]:
        sys.exit("В config.json пустой bot_token")
    store = Store(Path(tempfile.mkdtemp()) / "fake.db")
    engine = FakeEngine(cfg, store)
    app = await start_bot(cfg, engine, store)
    if not cfg["owner_chat_id"]:
        print("Напишите боту /start, затем запустите скрипт снова.")
        await stop_bot(app)
        return
    l = Listing("kleinanzeigen", "fake-1", "https://example.test/fake-1", "Schöne 3-Zimmer-Wohnung in Bonn",
                postcode="53111", warm_rent=880, rooms=3, area=70)
    id = store.add(l, 880)
    text = "Sehr geehrte Damen und Herren,\n\nich interessiere mich für Ihre Wohnung (тестовый черновик)."
    store.set_status(id, "draft", letter=text)
    msg_id = await engine.notifier.draft(store.get(id), text, warm_estimated=True)
    store.set_status(id, "draft", draft_msg_id=msg_id)
    print("Черновик отправлен, message_id =", msg_id, "— нажимайте кнопки. Ctrl+C — выход.")
    try:
        await asyncio.Event().wait()
    finally:
        await stop_bot(app)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
