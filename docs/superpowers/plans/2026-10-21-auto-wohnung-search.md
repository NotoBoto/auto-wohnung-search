# AutoWohnungSearch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Windows-приложение, которое следит за квартирами в Бонне на Kleinanzeigen, WG-Gesucht, Immowelt и ImmoScout24, пишет арендодателю письмо через нейросеть, отправляет его (авто или после подтверждения в Telegram) и сообщает в Telegram об ответах из Gmail.

**Architecture:** Форк tg-autocomment. Окно (customtkinter), трей, автозапуск, автообновление и подключение нейросетей переносятся. Telethon-часть удаляется. В фоновом потоке крутится один asyncio-цикл. В нём работают `Engine` (опрос площадок, фильтр, дубли, письмо, отправка), Telegram-бот (python-telegram-bot) и проверка почты по IMAP. Площадки открываются через Playwright: у каждой свой постоянный профиль браузера Edge (`channel="msedge"`, он есть в каждой Windows 11, поэтому ничего скачивать не надо). Состояние хранится в SQLite.

**Tech Stack:** Python 3.12 (CI) / 3.12+ локально, customtkinter, pystray, playwright (msedge), python-telegram-bot 21.x, imaplib (stdlib), sqlite3 (stdlib), anthropic / openai / google-genai (как в образце), openpyxl (выгрузка в Excel), pytest + pytest-asyncio, PyInstaller + Inno Setup.

**Spec:** `docs/superpowers/specs/2026-10-21-auto-wohnung-search-design.md`

**Образец:** https://github.com/NotoBoto/tg-autocomment. Пользователь разрешил брать его код. Клонировать во временную папку: `git clone --depth 1 https://github.com/NotoBoto/tg-autocomment <tmp>/tg-autocomment`.

## Global Constraints

- Город Бонн. Фильтр по умолчанию: Warmmiete ≤ 900 €, комнат ≥ 2, только отдельная квартира. Всё меняется в боте.
- Если указана только Kaltmiete: Warm = Kalt + Nebenkosten (+ Heizkosten, если они указаны отдельно). Если Nebenkosten нет: Warm ≈ Kalt + 2,5 €/м², с пометкой «warm оценена».
- Отсекаются: Wohnungstausch, «nur mit WBS» (если в профиле нет WBS), аренда на срок меньше 12 месяцев, «möbliert auf Zeit».
- По одной квартире пишем один раз за 60 дней, на всех площадках вместе. Одному контакту — не больше 3 писем за 7 дней.
- Лимиты по умолчанию: 15 писем в день и одно письмо в 120 с на площадку, опрос раз в 180–360 с. ImmoScout24: 10 писем в день, одно в 240 с, опрос раз в 300–480 с. С 00:00 до 07:00 опрос раз в 900–1200 с. Всё настраивается отдельно для каждой площадки.
- Письмо на немецком, 600–1200 знаков. Обращение на «Sie», но «du», если объявление написано на «du». Первые предложения писем не повторяются.
- Режимы: `auto` и `confirm`. Режим задаётся глобально, его можно переопределить для отдельной площадки. *(решение плана)* По умолчанию `confirm`.
- Уведомления со звуком: ответ, черновик, проблема (одна на случай). В режиме `auto` отдельных уведомлений об отправке нет, только сводка в 20:00.
- Черновик, на который не ответили за 12 часов, тихо удаляется.
- Бот отвечает только владельцу: chat id фиксируется при первом `/start`, остальным он молчит.
- Капчи не решаем. При капче или выходе из аккаунта площадка ставится на паузу, в бот уходит одно сообщение.
- Пока профиль не заполнен, отправка выключена.
- Тексты бота и окна на русском, письма арендодателям на немецком.
- `REPO = "NotoBoto/auto-wohnung-search"`, `NAME = "Auto Wohnung Search"`, `VERSION = "0.1.0"`, папка данных `%APPDATA%\Auto Wohnung Search`.
- Секреты (токен бота, пароль приложения Gmail, ключи нейросетей) хранятся только в `config.json` в папке данных, никогда не попадают в git и в логи.

## Review Focus

1. **Квартира перевыложена с новым id или выложена на второй площадке.** Второго письма быть не должно: при уверенном совпадении объявление молча помечается дублем, при неуверенном в бот уходит вопрос. → тесты в Task 5 и Task 12.
2. **Программа упала или ПК выключили посреди отправки.** После перезапуска повтора нет: объявление в статусе `sending` уходит владельцу вопросом «дошло ли?». → тест в Task 12.
3. **Капча появилась несколько раз подряд на одной площадке.** В бот приходит одно сообщение, площадка на паузе, остальные работают дальше. После `/resume` новая капча — это новый случай. → тест в Task 12.
4. **Немецкие форматы чисел и неполные данные:** «1.250,00 €», «850 € warm», «2,5 Zimmer», «ca. 65 m²», нет Nebenkosten, нет площади. Разбор не должен падать и не должен пропускать неподходящее. → тесты в Task 3 и Task 4.
5. **Нейросеть недоступна, вернула пусто, ответила по-английски или повторила прежнее начало.** Письмо не уходит: повторяем один раз, затем одно уведомление о проблеме, а объявление остаётся в статусе `failed` и его можно перегенерировать. → тесты в Task 6 и Task 12.

## Что пользователь делает сам (до задач, где это нужно)

- **До Task 8:** создать Gmail, включить 2FA и сделать пароль приложения. Зарегистрироваться на Kleinanzeigen, WG-Gesucht, Immowelt и ImmoScout24 на этот Gmail и включить на площадках e-mail-уведомления о новых сообщениях.
- **До Task 11:** создать бота через @BotFather и получить токен.
- **Task 8–10, 13:** проверить отправку вживую на одном реальном объявлении по каждой площадке. Исполнитель не отправляет настоящие письма без разрешения пользователя.

## File Structure

```
core.py              # из образца, сокращён: версия, пути, конфиг, автозапуск, прокси/cli_env
ai.py                # из Engine образца: AIClient + проверки/установка нейросетей, human_typography
models.py            # Listing, Profile, Filters, parse_money/parse_rooms/parse_area
filters.py           # estimate_warm, check()
store.py             # SQLite: объявления, статусы, дубли, лимиты, ответы, случаи-проблемы
letter.py            # промпт, генерация письма, проверки
prompt.example.txt   # промпт письма по умолчанию (немецкий)
browser.py           # Playwright: постоянные профили msedge, окно входа
sources/base.py      # Source, RateLimiter, poll_interval, BlockedError, human_type
sources/kleinanzeigen.py, sources/wggesucht.py, sources/immowelt.py, sources/immoscout.py
mail.py              # IMAP Gmail: разбор писем-уведомлений в Reply
bot_text.py          # чистые функции: тексты сообщений, разбор команд, шаги профиля
bot.py               # python-telegram-bot: команды, кнопки, уведомления
pipeline.py          # Engine: циклы опроса, обработка объявления, отправка, сводка
app.py               # окно из образца, переделанное
updater.py, build.py, installer.iss, .github/workflows/release.yml   # из образца, переименованы
tests/fixtures/<platform>/*.html, tests/fixtures/mail/*.eml
tests/test_*.py
```

---

### Task 1: Форк образца и очистка от Telethon

**Files:**
- Create (копия из образца): `core.py`, `app.py`, `main.py`, `updater.py`, `build.py`, `installer.iss`, `start.bat`, `assets/`, `.github/workflows/release.yml`, `requirements.txt`, `RELEASE_NOTES.md`, `README.md`
- Create: `tests/test_core.py`, `pytest.ini`
- Modify: `.gitignore` (добавить `_builtin_api.py` не нужно: файла больше нет)

**Interfaces:**
- Produces: `core.VERSION`, `core.REPO`, `core.BASE: Path`, `core.CONFIG_FILE`, `core.DEFAULTS: dict`, `core.load_config() -> dict`, `core.save_config(cfg: dict) -> None`, `core.cli_env(drop=()) -> dict`, `core.system_proxy() -> str | None`, `core.get_autostart/set_autostart`, `core.RUN_NAME = "Auto Wohnung Search"`.

- [ ] **Step 1:** Скопировать файлы из образца. В `core.py` удалить всё, что связано с Telethon и каналами: импорты `telethon`, `Pending`, `Engine`, отчёт `comments.jsonl`, `channels`, `parse_post_link`, `telegram_app`, `telegram_proxy`, `validate`, быстрый пароль (`FAST_*`, `check_fast_password`, `restrict_fast`), картинки и пасты. Генераторы нейросетей (`_gen_*`, `_generate`) переносятся в Task 2, пока оставить их копию в `<tmp>`. Профили (`profiles`) убрать: конфиг один, плоский.
- [ ] **Step 2:** Задать константы из Global Constraints и `DEFAULTS`:

```python
DEFAULTS = {
    "backend": "zai", "model": "sonnet", "api_key": "", "api_model": "claude-opus-5-5",
    "gemini_model": "", "gemini_api_key": "", "gemini_api_model": "gemini-flash-latest",
    "codex_model": "", "openai_api_key": "", "openai_api_model": "gpt-5.5",
    "compat_base_url": "http://localhost:1234/v1", "compat_api_key": "", "compat_model": "",
    "zai_api_key": "", "zai_model": "glm-4.7-flash", "claude_timeout_sec": 120,
    "prompt_file": "prompt.txt",
    "bot_token": "", "owner_chat_id": 0,
    "gmail_user": "", "gmail_app_password": "", "mail_poll_sec": 90,
    "mode": "confirm",
    "digest_hour": 20, "draft_ttl_hours": 12,
    "filters": {"city": "Bonn", "max_warm": 900, "min_rooms": 2.0, "min_lease_months": 12},
    "profile": {},
    "platforms": {
        "kleinanzeigen": {"enabled": True, "mode": None, "daily_cap": 15, "min_gap_sec": 120, "poll": [180, 360]},
        "wggesucht":     {"enabled": True, "mode": None, "daily_cap": 15, "min_gap_sec": 120, "poll": [180, 360]},
        "immowelt":      {"enabled": True, "mode": None, "daily_cap": 15, "min_gap_sec": 120, "poll": [180, 360]},
        "immoscout":     {"enabled": True, "mode": None, "daily_cap": 10, "min_gap_sec": 240, "poll": [300, 480]},
    },
    "night_poll": [900, 1200], "contact_cap_7d": 3, "dedup_days": 60,
    "auto_update": True, "autostart": False,
}
```

`load_config()` дополняет вложенные словари (`filters`, `platforms.*`) недостающими ключами из `DEFAULTS`, ничего не затирая.
- [ ] **Step 3:** В `app.py` временно оставить только каркас: окно, трей, автозапуск, проверку обновлений, вкладку «Журнал» и пустую вкладку «Настройки». Всё про профили, каналы, очередь и вход в Telegram удалить, чтобы `python main.py` открывал окно без ошибок. В `build.py` и CI убрать встроенный ключ Telegram (`write_builtin_key`, `TG_API_*`) и переименовать `NAME`. В `updater.py` поменять `ASSET_SUFFIX`-пример и `User-Agent` на `auto-wohnung-search`. `requirements.txt`: убрать `telethon`, `cryptg`, `python-socks`, `qrcode`, добавить `playwright>=1.48`, `python-telegram-bot>=21.6,<22`, `openpyxl>=3.1`, `pytest`, `pytest-asyncio`.
- [ ] **Step 4: Failing test** `tests/test_core.py`:

```python
def test_load_config_fills_nested_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "CONFIG_FILE", tmp_path / "config.json")
    (tmp_path / "config.json").write_text('{"filters": {"max_warm": 950}, "platforms": {"immoscout": {"daily_cap": 5}}}', encoding="utf-8")
    cfg = core.load_config()
    assert cfg["filters"] == {"city": "Bonn", "max_warm": 950, "min_rooms": 2.0, "min_lease_months": 12}
    assert cfg["platforms"]["immoscout"]["daily_cap"] == 5
    assert cfg["platforms"]["immoscout"]["min_gap_sec"] == 240
    assert cfg["mode"] == "confirm"

def test_identity():
    assert core.REPO == "NotoBoto/auto-wohnung-search" and core.VERSION == "0.1.0"
```
- [ ] **Step 5:** `pytest tests/test_core.py -v` → PASS. `python -c "import app"` без ошибок. `grep -ri telethon *.py` ничего не находит.
- [ ] **Step 6: Commit** `feat: fork tg-autocomment base without Telethon`

---

### Task 2: AIClient из генераторов образца

**Files:**
- Create: `ai.py`, `tests/test_ai.py`
- Modify: `core.py` (перенести в `ai.py` всё про нейросети: `find_claude` … `check_zai_key`, `BACKEND_NAMES`, `explain_ai_error`, `human_typography`, `strip_reasoning`, `pick_variants`)

**Interfaces:**
- Produces:
  - `class AIClient: __init__(self, cfg: dict, log: logging.Logger)`, `async generate(self, system: str, user: str) -> str`. Отдаёт текст после `human_typography` и `strip_reasoning`. При недоступности бросает `AIError(str)`, где текст понятен человеку (из `explain_ai_error`).
  - `class AIError(RuntimeError)`
  - `ai.human_typography(text: str) -> str`, `ai.BACKEND_NAMES`, функции `check_*`, `install_*`, `*_status`, `open_*_login` с теми же сигнатурами, что в образце.

- [ ] **Step 1: Failing tests:**

```python
async def test_generate_dispatches_by_backend(monkeypatch):
    c = AIClient({**core.DEFAULTS, "backend": "openai_compat"}, logging.getLogger("t"))
    async def fake(system, user): return "Hallo — Welt…"
    monkeypatch.setattr(c, "_gen_compat", fake)
    assert await c.generate("s", "u") == "Hallo - Welt..."

async def test_generate_wraps_errors(monkeypatch):
    c = AIClient({**core.DEFAULTS, "backend": "zai"}, logging.getLogger("t"))
    async def boom(system, user): raise RuntimeError("Ключ Z.ai не подходит")
    monkeypatch.setattr(c, "_gen_zai", boom)
    with pytest.raises(AIError, match="Ключ Z.ai"):
        await c.generate("s", "u")
```
- [ ] **Step 2:** Run → FAIL (`ModuleNotFoundError: ai`).
- [ ] **Step 3:** Перенести `_gen_claude_code`, `_gen_gemini_cli`, `_gen_gemini_api`, `_gen_codex`, `_gen_compat`, `_gen_zai`, `_gen_openai_api`, `_gen_api` в `AIClient` как есть, но с сигнатурой `(self, system, user_msg)`: параметр `images` и ветки с картинками убрать. `self.system_prompt_file` = `core.BASE / "_system_prompt.txt"`. Ветку `pastes` и проверку `copies_post` не переносить. Добавить `self._lock = asyncio.Lock()`: в каждый момент к нейросети идёт только один запрос.
- [ ] **Step 4:** `pytest tests/test_ai.py -v` → PASS.
- [ ] **Step 5: Commit** `feat: extract AI backends into AIClient`

---

### Task 3: Модели и разбор немецких чисел

**Files:**
- Create: `models.py`, `tests/test_models.py`

**Interfaces:**
- Produces:

```python
PLATFORMS = ("kleinanzeigen", "wggesucht", "immowelt", "immoscout")

@dataclass
class Listing:
    platform: str; ext_id: str; url: str; title: str = ""
    street: str = ""; postcode: str = ""; district: str = ""
    cold_rent: float | None = None; extra_costs: float | None = None
    heating_costs: float | None = None; warm_rent: float | None = None
    rooms: float | None = None; area: float | None = None
    available_from: str = ""; description: str = ""; contact_name: str = ""
    is_shared_flat: bool = False; is_swap: bool = False; wbs_required: bool = False
    limited_months: int | None = None; furnished_temporary: bool = False
    required_fields: list[str] = field(default_factory=list)   # обязательные поля формы

PROFILE_FIELDS = ("salutation", "first_name", "last_name", "persons", "occupation",
                  "net_income", "move_in", "smoker", "pets", "wbs", "documents",
                  "phone", "about", "hidden")   # hidden: поля, которые не раскрывать в первом письме
def profile_complete(profile: dict) -> bool   # заполнены salutation, first_name, last_name, persons, move_in, about

def parse_money(s: str) -> float | None
def parse_rooms(s: str) -> float | None
def parse_area(s: str) -> float | None
def parse_lease_months(text: str) -> int | None
```
- [ ] **Step 1: Failing tests:**

```python
@pytest.mark.parametrize("s,v", [("1.250,00 €", 1250.0), ("850 € warm", 850.0), ("650€", 650.0),
                                 ("ca. 720,50 EUR", 720.5), ("VB", None), ("", None)])
def test_parse_money(s, v): assert parse_money(s) == v

@pytest.mark.parametrize("s,v", [("2 Zimmer", 2.0), ("2,5 Zi.", 2.5), ("3", 3.0), ("Zimmer: k.A.", None)])
def test_parse_rooms(s, v): assert parse_rooms(s) == v

@pytest.mark.parametrize("s,v", [("ca. 65 m²", 65.0), ("58,4 qm", 58.4), ("", None)])
def test_parse_area(s, v): assert parse_area(s) == v

@pytest.mark.parametrize("t,v", [("befristet auf 6 Monate", 6), ("Zwischenmiete für 3 Monate", 3),
                                 ("befristet bis 1 Jahr", 12), ("unbefristet", None)])
def test_parse_lease_months(t, v): assert parse_lease_months(t) == v

def test_profile_complete():
    p = {"salutation": "Herr", "first_name": "A", "last_name": "B", "persons": 2, "move_in": "01.01.2027", "about": "x"}
    assert profile_complete(p) and not profile_complete({**p, "about": ""})
```
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Реализовать. Точка — разделитель тысяч, если после неё ровно 3 цифры; запятая — десятичный разделитель.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat: listing model and German number parsing`

---

### Task 4: Фильтр

**Files:**
- Create: `filters.py`, `tests/test_filters.py`

**Interfaces:**
- Consumes: `Listing`, `profile: dict`, `cfg["filters"]`.
- Produces:
  - `estimate_warm(l: Listing) -> tuple[float | None, bool]`, где bool означает, что warm оценена.
  - `@dataclass FilterResult: ok: bool; reason: str; warm: float | None; warm_estimated: bool`
  - `check(l: Listing, filters: dict, profile: dict) -> FilterResult`. `reason` — короткая причина на русском для сводки: «дороже 900 €», «меньше 2 комнат», «WG», «Tausch», «нужен WBS», «срок 6 мес.», «möbliert auf Zeit», «не Бонн», «нет цены», «нет числа комнат».

- [ ] **Step 1: Failing tests:**

```python
def L(**kw): return Listing(platform="kleinanzeigen", ext_id="1", url="u", postcode="53115", **kw)

def test_estimate_warm():
    assert estimate_warm(L(warm_rent=880)) == (880, False)
    assert estimate_warm(L(cold_rent=600, extra_costs=150, heating_costs=60)) == (810, False)
    assert estimate_warm(L(cold_rent=700, area=60)) == (850, True)
    assert estimate_warm(L(cold_rent=700)) == (None, False)

@pytest.mark.parametrize("kw,ok,reason", [
    (dict(warm_rent=900, rooms=2), True, ""),
    (dict(warm_rent=901, rooms=2), False, "дороже 900 €"),
    (dict(warm_rent=800, rooms=1.5), False, "меньше 2 комнат"),
    (dict(warm_rent=800, rooms=3), True, ""),
    (dict(warm_rent=800, rooms=2, is_shared_flat=True), False, "WG"),
    (dict(warm_rent=800, rooms=2, is_swap=True), False, "Tausch"),
    (dict(warm_rent=800, rooms=2, wbs_required=True), False, "нужен WBS"),
    (dict(warm_rent=800, rooms=2, limited_months=6), False, "срок 6 мес."),
    (dict(warm_rent=800, rooms=2, furnished_temporary=True), False, "möbliert auf Zeit"),
    (dict(rooms=2), False, "нет цены"),
    (dict(warm_rent=800), False, "нет числа комнат"),
])
def test_check(kw, ok, reason):
    r = check(L(**kw), {"city": "Bonn", "max_warm": 900, "min_rooms": 2.0, "min_lease_months": 12}, {})
    assert (r.ok, r.reason) == (ok, reason)

def test_wbs_ok_when_profile_has_wbs():
    assert check(L(warm_rent=800, rooms=2, wbs_required=True), FILTERS, {"wbs": True}).ok

def test_not_bonn_postcode():
    assert check(L(warm_rent=800, rooms=2, postcode="50667"), FILTERS, {}).reason == "не Бонн"
```

Бонн определяется по PLZ 53111–53229. Если PLZ нет, объявление считается боннским: поиск на площадках уже ограничен Бонном.
- [ ] **Step 2–4:** Run → FAIL. Реализовать. Run → PASS.
- [ ] **Step 5: Commit** `feat: listing filter`

---

### Task 5: Хранилище и защита от дублей

**Files:**
- Create: `store.py`, `tests/test_store.py`

**Interfaces:**
- Consumes: `Listing`.
- Produces:

```python
STATUSES = ("seen", "filtered", "duplicate", "ask_dup", "draft", "queued", "sending", "sent", "skipped", "failed")
@dataclass Row: id: int; platform: str; ext_id: str; url: str; title: str; status: str
                letter: str; warm: float | None; contact_key: str; created_at: datetime; sent_at: datetime | None
                reason: str; draft_msg_id: int | None
@dataclass DedupResult: kind: Literal["new", "exact", "similar"]; match_id: int | None

class Store:
    def __init__(self, path: Path)                     # ":memory:" допустим в тестах
    def add(self, l: Listing, warm: float | None) -> int   # повторный add того же (platform, ext_id) отдаёт старый id
    def get(self, id: int) -> Row
    def set_status(self, id: int, status: str, **fields) -> None   # fields: letter, reason, sent_at, draft_msg_id
    def find_dedup(self, l: Listing, warm: float | None, days: int = 60) -> DedupResult
    def sent_count(self, platform: str, since: datetime) -> int
    def last_sent_at(self, platform: str) -> datetime | None
    def contact_sent_count(self, contact_key: str, days: int = 7) -> int
    def recent_openings(self, n: int = 30) -> list[str]   # первые предложения отправленных писем
    def by_status(self, status: str) -> list[Row]
    def match_reply(self, platform: str, ext_id: str = "", url: str = "", contact: str = "") -> Row | None
    def add_reply(self, listing_id: int, excerpt: str, link: str) -> None
    def day_stats(self, day: date) -> dict   # {"found", "sent", "filtered": {reason: n}, "duplicates", "replies", "failed"}
    def open_incident(self, platform: str, kind: str) -> bool   # True, если случай новый
    def close_incidents(self, platform: str) -> None
    def open_incidents(self) -> list[tuple[str, str]]

def addr_key(l: Listing) -> str        # "musterstr 12|53115" из нормализованных street+postcode, иначе "|53115|<district>"
def contact_key(name: str) -> str      # lower, без "gmbh", "ag", "immobilien", "hausverwaltung", "frau", "herr", пробелы схлопнуты
```

Нормализация улицы: lower, `straße`/`strasse`/`str.` → `str`, убрать пробелы и дефисы.
`find_dedup` работает так. Тот же `(platform, ext_id)` → `exact`. Есть отправленная или ждущая (`draft`, `queued`, `sending`, `sent`, `ask_dup`) запись за `days` дней с тем же `addr_key` (улица есть), площадь ±2 м², warm ±30 €, те же комнаты → `exact`. Если совпадает всё это, но улицы нет ни у одной из двух записей (ключ по PLZ/району) → `similar`. Иначе `new`.

- [ ] **Step 1: Failing tests:**

```python
def test_same_flat_on_two_platforms_is_exact(store):
    a = L(platform="kleinanzeigen", ext_id="1", street="Musterstraße 12", rooms=2, area=60)
    store.set_status(store.add(a, 850), "sent")
    b = L(platform="immowelt", ext_id="x9", street="Musterstr. 12", rooms=2, area=61.5)
    assert store.find_dedup(b, 870).kind == "exact"

def test_repost_new_id_is_exact(store): ...  # та же площадка, новый ext_id, тот же адрес → exact
def test_no_street_is_similar(store):         # обе без улицы, PLZ 53115, тот же район/площадь/цена → similar
def test_different_flat_same_landlord_is_new(store):  # тот же contact_name, другой адрес → new
def test_older_than_60_days_is_new(store):     # sent_at 61 день назад → new
def test_filtered_listing_does_not_block(store):  # статус filtered → новое объявление по тому же адресу new
def test_add_is_idempotent(store): assert store.add(a, 1) == store.add(a, 1)
def test_incident_opens_once(store):
    assert store.open_incident("immoscout", "captcha") is True
    assert store.open_incident("immoscout", "captcha") is False
    store.close_incidents("immoscout")
    assert store.open_incident("immoscout", "captcha") is True
def test_contact_cap_counts_7_days(store): ...  # 3 отправки за 6 дней → 3; отправка 8 дней назад не считается
def test_match_reply_by_ext_id_url_contact(store): ...
```
- [ ] **Step 2–4:** Run → FAIL. Реализовать (sqlite3, `check_same_thread=False`, таблицы `listings`, `replies`, `incidents`, время в UTC ISO). Run → PASS.
- [ ] **Step 5: Commit** `feat: sqlite store with dedup and incidents`

---

### Task 6: Письмо

**Files:**
- Create: `letter.py`, `prompt.example.txt`, `tests/test_letter.py`

**Interfaces:**
- Consumes: `AIClient.generate(system, user) -> str`, `AIError`, `Listing`, `profile`.
- Produces:

```python
@dataclass LetterResult: text: str | None; skip_reason: str | None
class LetterError(RuntimeError)
def address_form(l: Listing) -> Literal["Sie", "du"]
def first_sentence(text: str) -> str
def looks_german(text: str) -> bool
def build_user_message(l: Listing, profile: dict, form: str, wish: str = "", avoid_openings: Sequence[str] = ()) -> str
async def generate_letter(ai, prompt: str, l: Listing, profile: dict, wish: str = "",
                          avoid_openings: Sequence[str] = ()) -> LetterResult
```

`address_form` считает в описании слова `du|dich|dir|dein\w*|euch|ihr` и `Sie|Ihnen|Ihr\w*` (с заглавной, не в начале предложения). Если «du»-слов больше → `"du"`, иначе `"Sie"`.
`build_user_message` включает: заголовок, адрес/район, цены, комнаты, площадь, дату въезда, описание (обрезанное до 3000 знаков), имя контакта, форму обращения, поля профиля без тех, что перечислены в `profile["hidden"]`, пожелание, список запрещённых первых предложений.
`generate_letter`:
1. Ответ начинается с `SKIP` → `LetterResult(None, <текст после SKIP:>)`.
2. Длина вне 600–1200, `looks_german` ложно или первое предложение есть в `avoid_openings` → одна повторная попытка с добавленным пожеланием о причине. Если и вторая не прошла → `LetterError` с причиной на русском.
3. `AIError` пробрасывается как есть.

`prompt.example.txt` (немецкий, кратко): ты пишешь как сам арендатор, не как ассистент; одна-две конкретные детали из объявления; коротко о себе только из данных профиля, ничего не выдумывать; упомянуть готовые документы; попросить о просмотре; 600–1200 знаков; без маркированных списков и подписи «KI»; подпись — имя из профиля; если это WG, комната, Tausch, Zwischenmiete короче 12 месяцев или не Бонн — ответить `SKIP: <причина по-русски>`.

- [ ] **Step 1: Failing tests** (с `FakeAI`, который отдаёт ответы по очереди и записывает запросы):

```python
async def test_skip():
    r = await generate_letter(FakeAI(["SKIP: это WG"]), "p", L(), PROFILE)
    assert r == LetterResult(None, "это WG")

async def test_retry_on_short_then_ok():
    ai = FakeAI(["Hallo", GOOD_DE_800])
    assert (await generate_letter(ai, "p", L(), PROFILE)).text == GOOD_DE_800
    assert "600" in ai.calls[1][1]

async def test_english_twice_raises():
    with pytest.raises(LetterError, match="не на немецком"):
        await generate_letter(FakeAI([EN_800, EN_800]), "p", L(), PROFILE)

async def test_repeated_opening_retried():
    ai = FakeAI([GOOD_DE_800, OTHER_DE_800])
    r = await generate_letter(ai, "p", L(), PROFILE, avoid_openings=[first_sentence(GOOD_DE_800)])
    assert r.text == OTHER_DE_800

def test_hidden_profile_fields_not_in_prompt():
    msg = build_user_message(L(), {**PROFILE, "net_income": "3200", "hidden": ["net_income"]}, "Sie")
    assert "3200" not in msg

def test_address_form():
    assert address_form(L(description="Hast du Lust? Melde dich gerne bei mir, dann zeige ich dir alles.")) == "du"
    assert address_form(L(description="Wir freuen uns auf Ihre Anfrage. Bitte senden Sie uns Ihre Unterlagen.")) == "Sie"
```
- [ ] **Step 2–4:** Run → FAIL. Реализовать. Run → PASS.
- [ ] **Step 5: Commit** `feat: letter generation with checks`

---

### Task 7: Браузер и общая основа источников

**Files:**
- Create: `browser.py`, `sources/__init__.py`, `sources/base.py`, `tests/test_sources_base.py`

**Interfaces:**
- Consumes: `Store.sent_count`, `Store.last_sent_at`.
- Produces:

```python
class BlockedError(RuntimeError): kind: Literal["captcha", "logged_out", "blocked"]
class SourceBroken(RuntimeError)          # разметка не распознана: селекторы устарели
class MissingFieldError(RuntimeError): field: str   # форма требует поле, которого нет в профиле

class Source(ABC):
    name: str; login_url: str
    def search_url(self, filters: dict) -> str
    @staticmethod
    def parse_search(html: str) -> list[Listing]           # пустой список без признаков выдачи → SourceBroken
    @staticmethod
    def parse_detail(html: str, l: Listing) -> Listing
    @staticmethod
    def detect_block(html: str, url: str) -> str | None    # "captcha" | "logged_out" | "blocked" | None
    async def search(self, page) -> list[Listing]          # goto search_url, detect_block → BlockedError, parse_search
    async def fetch_detail(self, page, l: Listing) -> Listing
    async def send(self, page, l: Listing, text: str, profile: dict) -> None
    async def is_logged_in(self, page) -> bool

class RateLimiter:
    def __init__(self, store: Store, platform: str, daily_cap: int, min_gap_sec: int)
    def wait_seconds(self, now: datetime) -> float | None   # None = лимит на сегодня исчерпан; 0 = можно сейчас

def poll_interval(now: datetime, day: tuple[int, int], night: tuple[int, int], rnd=random) -> float  # ночь 00:00–07:00 местного времени
async def human_type(locator, text: str) -> None   # по символу, 30–120 мс, иногда пауза 300–800 мс
async def human_pause(a: float = 0.8, b: float = 2.5) -> None

class Browser:
    def __init__(self, base: Path)                  # профили в base / "browser_profiles" / <platform>
    async def page(self, platform: str) -> Page      # постоянный контекст msedge; окно за краем экрана
    async def show_login(self, platform: str, url: str) -> None   # то же окно, видимое, на url
    async def hide(self, platform: str) -> None
    async def close(self) -> None
```

`Browser.page` открывает `async_playwright().chromium.launch_persistent_context(user_data_dir, channel="msedge", headless=False, args=["--window-position=-32000,-32000"], locale="de-DE", viewport={"width": 1366, "height": 850})`. На каждую площадку свой `asyncio.Lock`, параллельной работы одной площадки нет.

- [ ] **Step 1: Failing tests:**

```python
def test_rate_limiter_daily_cap(store):  # 15 отправок сегодня → wait_seconds is None
def test_rate_limiter_gap(store):        # последняя 60 с назад, gap 120 → 60.0
def test_rate_limiter_free(store):       # нет отправок → 0
def test_poll_interval_night():
    r = random.Random(1)
    assert 900 <= poll_interval(datetime(2026, 10, 21, 3, 0), (180, 360), (900, 1200), r) <= 1200
    assert 180 <= poll_interval(datetime(2026, 10, 21, 7, 0), (180, 360), (900, 1200), r) <= 360
```
- [ ] **Step 2–4:** Run → FAIL. Реализовать. Run → PASS. Ручная проверка: `python -c "import asyncio, browser, core; ..."` открывает Edge с профилем `kleinanzeigen` на `https://www.kleinanzeigen.de`, закрывается без ошибок.
- [ ] **Step 5: Commit** `feat: browser profiles and source base`

---

### Task 8: Kleinanzeigen

**Files:**
- Create: `sources/kleinanzeigen.py`, `tests/test_kleinanzeigen.py`, `tests/fixtures/kleinanzeigen/search.html`, `detail.html`, `captcha.html` (если удастся получить), `scripts/capture.py`

**Interfaces:**
- Consumes: `Source`, `Listing`, парсеры из `models`.
- Produces: `class Kleinanzeigen(Source)`, `name = "kleinanzeigen"`. `scripts/capture.py <platform> <url> <out.html>` — сохранить страницу через `Browser` (пригодится и для остальных площадок).

- [ ] **Step 1:** Через `scripts/capture.py` сохранить реальную выдачу «Mietwohnungen in Bonn» (сортировка по новизне, цена до 900, от 2 комнат, если площадка даёт такие параметры в URL) и одно объявление. Убрать из фикстур персональные данные (имена, телефоны) заменой на `Max Mustermann`.
- [ ] **Step 2: Failing tests:**

```python
def test_parse_search():
    items = Kleinanzeigen.parse_search(read("kleinanzeigen/search.html"))
    assert len(items) >= 10
    assert all(i.platform == "kleinanzeigen" and i.ext_id and i.url.startswith("https://www.kleinanzeigen.de/s-anzeige/") for i in items)
    assert any(i.cold_rent or i.warm_rent for i in items)

def test_parse_detail():   # значения сверить с сохранённым объявлением и вписать в тест буквально
    l = Kleinanzeigen.parse_detail(read("kleinanzeigen/detail.html"), Listing("kleinanzeigen", "<id>", "<url>"))
    assert (l.rooms, l.area, l.warm_rent or l.cold_rent, l.postcode) == (<rooms>, <area>, <rent>, "<plz>")
    assert l.description

def test_parse_search_garbage_raises():
    with pytest.raises(SourceBroken): Kleinanzeigen.parse_search("<html><body>Wartung</body></html>")

def test_detect_block_login():
    assert Kleinanzeigen.detect_block("", "https://www.kleinanzeigen.de/m-einloggen.html") == "logged_out"
```
`is_shared_flat`, `is_swap`, `limited_months`, `furnished_temporary`, `wbs_required` выставлять по ключевым словам в заголовке и описании: `WG|Mitbewohner`, `Tausch`, `Zwischenmiete|befristet` → `parse_lease_months`, `möbliert auf Zeit|Wohnen auf Zeit`, `WBS`.
- [ ] **Step 3:** Run → FAIL. Реализовать `parse_*`, `detect_block`, `search`, `fetch_detail`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Реализовать `send`: на странице объявления кнопка «Nachricht schreiben», поле сообщения через `human_type`, `human_pause` перед кликом «Senden». Успех — признак отправки на странице. Иначе `SourceBroken`. Обязательные поля, которых нет в профиле → `MissingFieldError`.
- [ ] **Step 6: Ручная проверка с пользователем:** войти через `Browser.show_login`, затем отправить одно письмо на реальное объявление, которое пользователь выберет сам, с текстом, который он одобрит. Отправка идёт только после явного «да» пользователя.
- [ ] **Step 7: Commit** `feat: Kleinanzeigen source`

---

### Task 9: WG-Gesucht (раздел «Wohnungen»)

Как Task 8. Файлы: `sources/wggesucht.py`, `tests/test_wggesucht.py`, `tests/fixtures/wggesucht/*`. `class WgGesucht(Source)`, `name = "wggesucht"`. Ищем только категорию «Wohnungen» (не «WG-Zimmer» и не «1-Zimmer-Wohnungen»), город Бонн, новые сверху. В `test_parse_search` дополнительно проверить, что URL всех объявлений относятся к разделу «wohnungen-in-Bonn». В `send` есть особенность площадки: сообщение может требовать шаблон или подтверждение. Если открывается окно со шаблоном, его закрыть и вставить свой текст. Коммит `feat: WG-Gesucht source`.

### Task 10: Immowelt

Как Task 8. Файлы: `sources/immowelt.py`, `tests/test_immowelt.py`, `tests/fixtures/immowelt/*`. `class Immowelt(Source)`, `name = "immowelt"`. Форма контакта: Anrede, Vorname, Nachname, E-Mail, Telefon, Nachricht и иногда «Anzahl Personen» / «Einzugstermin». Поля заполняются из профиля, e-mail берётся из `cfg["gmail_user"]`. Тест `test_required_fields_detected` проверяет, что по фикстуре формы `required_fields` содержит все поля, помеченные обязательными. Коммит `feat: Immowelt source`.

---

### Task 11: Почта: ответы из Gmail

**Files:**
- Create: `mail.py`, `tests/test_mail.py`, `tests/fixtures/mail/*.eml`

**Interfaces:**
- Produces:

```python
@dataclass Reply: platform: str; ext_id: str; url: str; contact: str; excerpt: str; link: str
SENDERS = {"kleinanzeigen.de": "kleinanzeigen", "wg-gesucht.de": "wggesucht",
           "immowelt.de": "immowelt", "immobilienscout24.de": "immoscout"}
def parse_notification(msg: email.message.Message) -> Reply | None
class MailWatcher:
    def __init__(self, user: str, app_password: str, state: dict)   # state["last_uid"] хранится в config
    def fetch_new(self) -> list[Reply]                                # синхронно; вызывается через asyncio.to_thread
    def check_login(self) -> tuple[bool, str]                         # для кнопки «Проверить» в окне
```

`fetch_new` подключается к `imap.gmail.com:993`, открывает `INBOX` в режиме readonly (флаги «прочитано» пользователя не трогаем) и ищет `UID {last_uid+1}:*`. Первый запуск только запоминает текущий максимальный UID. Письма от отправителей не из `SENDERS` пропускаются. Письма площадок, которые не про новое сообщение (рассылки, поисковые подписки), дают `None`. `excerpt` — текст ответа из письма, не длиннее 500 знаков, без цитаты нашего письма.

- [ ] **Step 1:** Фикстуры. Пока реальных писем нет, сделать по одному синтетическому `.eml` на площадку по известному формату («Sie haben eine neue Nachricht», «Neue Nachricht zu Ihrer Anzeige» и т. п.), плюс одно рекламное письмо площадки и одно письмо от постороннего отправителя. Когда придут настоящие ответы (после Task 8–10), заменить синтетику реальными письмами без персональных данных.
- [ ] **Step 2: Failing tests:**

```python
@pytest.mark.parametrize("name,platform", [("kleinanzeigen_reply.eml", "kleinanzeigen"), ("wggesucht_reply.eml", "wggesucht"),
                                           ("immowelt_reply.eml", "immowelt"), ("immoscout_reply.eml", "immoscout")])
def test_parse_reply(name, platform):
    r = parse_notification(load_eml(name))
    assert r.platform == platform and r.link.startswith("https://") and r.excerpt and (r.ext_id or r.url or r.contact)

def test_newsletter_ignored(): assert parse_notification(load_eml("kleinanzeigen_newsletter.eml")) is None
def test_foreign_sender_ignored(): assert parse_notification(load_eml("other.eml")) is None
```
- [ ] **Step 3–4:** Run → FAIL. Реализовать. Run → PASS.
- [ ] **Step 5: Commit** `feat: Gmail reply detection`

---

### Task 12: Engine: весь конвейер

**Files:**
- Create: `pipeline.py`, `tests/test_pipeline.py`

**Interfaces:**
- Consumes: всё выше. Бот передаётся через протокол (в тестах — фейк):

```python
class Notifier(Protocol):
    async def draft(self, row: Row, text: str, warm_estimated: bool) -> int          # message_id
    async def ask_duplicate(self, row: Row, match: Row) -> None
    async def ask_stuck(self, row: Row) -> None
    async def reply(self, row: Row, r: Reply) -> None
    async def incident(self, platform: str, kind: str, detail: str) -> None
    async def missing_profile(self) -> None
    async def digest(self, stats: dict) -> None
    async def remove_draft(self, row: Row) -> None
```
- Produces:

```python
class Engine:
    def __init__(self, cfg: dict, store: Store, ai: AIClient, sources: dict[str, Source],
                 browser: Browser, notifier: Notifier, mail: MailWatcher | None, clock=datetime.now)
    async def run(self) -> None                   # все циклы до stop()
    async def stop(self) -> None
    async def process(self, platform: str, l: Listing) -> str     # итоговый статус; основа тестов
    async def approve(self, id: int, text: str | None = None) -> None   # «Отправить» (text — после «Изменить»)
    async def regenerate(self, id: int, wish: str = "") -> None
    async def skip(self, id: int) -> None
    async def resolve_duplicate(self, id: int, send: bool) -> None
    async def resolve_stuck(self, id: int, was_sent: bool) -> None
    def pause(self, platform: str | None) -> None
    def resume(self, platform: str | None) -> None   # закрывает случаи площадки
    def status(self) -> dict                         # для /status и окна
    def mode_for(self, platform: str) -> str          # platforms[p]["mode"] or cfg["mode"]
```

`process(platform, l)` выполняет шаги по порядку:
1. `find_dedup` (до загрузки подробностей; если `exact` по `(platform, ext_id)`, дальше ничего не делать).
2. `fetch_detail` → `estimate_warm` → `add`.
3. Снова `find_dedup` (теперь известен адрес). `exact` → `duplicate`. `similar` → `ask_dup` + `notifier.ask_duplicate`.
4. `filters.check` → `filtered` с `reason`.
5. Профиль не заполнен → статус остаётся `seen`, `notifier.missing_profile()` раз в сутки.
6. `contact_sent_count >= contact_cap_7d` → `filtered`, reason «лимит контакта».
7. `generate_letter`. SKIP → `filtered` с причиной. `LetterError`/`AIError` → `failed` + incident `("ai", ...)`.
8. Если не хватает обязательных полей формы (`required_fields` vs профиль) или режим `confirm` → `draft` + `notifier.draft`. Иначе (`auto`) → `queued`.

Отправка (`_send(id)`): `RateLimiter.wait_seconds` → None: остаётся `queued` до завтра. Иначе ждём, затем `set_status(sending)` → `source.send` → `sent` (с `sent_at`). `BlockedError` → обратно `queued` + `pause(platform)` + incident (уведомление только если `open_incident` вернул True). `MissingFieldError` → `draft` + `notifier.draft`. Другое исключение → `failed` + incident `("send", platform)`.

Циклы в `run()`:
- По одному на включённую площадку: `search` → `process` каждого нового, затем `poll_interval`. `SourceBroken` → incident `"broken"`, площадка на паузе.
- Очередь отправки: `queued` по порядку `created_at`.
- Почта раз в `mail_poll_sec`: `fetch_new` → `match_reply` → `add_reply` → `notifier.reply`. Ответ без совпадения всё равно уходит в бот с пометкой «не нашёл, к какому объявлению».
- Обслуживание раз в 60 с: черновики старше `draft_ttl_hours` → `skipped` + `notifier.remove_draft`. В `digest_hour` один раз за день → `notifier.digest(store.day_stats(today))`.
- При старте: все `sending` → `notifier.ask_stuck` (повторно не отправлять). *(решение плана)* Площадку не проверяем, спрашиваем владельца. Это проще и надёжнее, чем искать письмо в «Gesendete Nachrichten».

- [ ] **Step 1: Failing tests** (фейки: `FakeSource` с заданными listings/исключениями, `FakeAI`, `FakeNotifier` пишет вызовы, `clock` управляемый, `Store(":memory:")`):

```python
async def test_auto_mode_sends_and_no_per_send_notification(eng):     # mode auto → status sent, notifier.calls без "draft"
async def test_confirm_mode_creates_draft(eng):                      # → draft, notifier.draft вызван 1 раз
async def test_same_flat_other_platform_not_sent_twice(eng):         # Review Focus 1
async def test_similar_asks_once(eng):
async def test_restart_with_sending_asks_and_does_not_resend(eng):   # Review Focus 2: статус sending в БД → ask_stuck, FakeSource.sent пуст
async def test_captcha_twice_one_incident_others_continue(eng):      # Review Focus 3
async def test_resume_reopens_incidents(eng):                        # после resume новая капча → второе уведомление
async def test_ai_failure_marks_failed_single_incident(eng):         # Review Focus 5: два объявления подряд, AIError → 2× failed, 1× incident
async def test_daily_cap_keeps_queued(eng):                          # 15 sent сегодня → queued, send не вызван
async def test_missing_required_field_becomes_draft_in_auto(eng):
async def test_profile_incomplete_no_letter(eng):                    # ai.calls пуст, missing_profile 1 раз за 2 объявления
async def test_contact_cap(eng):
async def test_draft_expires_after_12h(eng):
async def test_digest_once_at_20(eng):                               # clock 19:59 → нет; 20:00 → да; 20:01 → нет повторно
async def test_unmatched_reply_still_notified(eng):
async def test_approve_with_edited_text_sends_that_text(eng):
```
- [ ] **Step 2–4:** Run → FAIL. Реализовать. Run → PASS.
- [ ] **Step 5: Commit** `feat: engine pipeline`

---

### Task 13: Telegram-бот

**Files:**
- Create: `bot_text.py`, `bot.py`, `tests/test_bot_text.py`

**Interfaces:**
- Consumes: `Engine` (методы из Task 12), `Store`, `core.save_config`.
- Produces:
  - `bot_text`: `format_draft(row, text, warm_estimated) -> str`, `format_reply(row, r) -> str`, `format_digest(stats) -> str`, `format_incident(platform, kind, detail) -> str`, `format_status(status) -> str`, `PROFILE_STEPS: list[tuple[str, str]]` (поле, вопрос по-русски), `parse_profile_answer(field, text) -> Any` (бросает `ValueError` с подсказкой), `parse_filters_args(args: list[str]) -> dict` (`/filters max_warm 950`, `/filters min_rooms 3`).
  - `class TelegramNotifier(Notifier)` в `bot.py`; `async def start_bot(cfg, engine, store) -> Application`.
  - Callback data: `send:<id>`, `edit:<id>`, `regen:<id>`, `skip:<id>`, `dup:<id>:send|skip`, `stuck:<id>:sent|resend`.

Поведение:
- `/start`: если `owner_chat_id == 0`, запомнить chat id и сохранить конфиг. Чужие chat id игнорируются всеми обработчиками, включая кнопки.
- `/profile` проводит по шагам `PROFILE_STEPS`. `/profile <поле>` меняет одно поле. `/profile hide <поле>` / `show <поле>` переключает `hidden`. `/profile` без аргументов, когда профиль заполнен, показывает текущий профиль.
- `/filters`, `/mode auto|confirm [платформа]`, `/pause [платформа]`, `/resume [платформа]`, `/status`, `/prompt` (показать) и `/prompt` с текстом ответом на сообщение (заменить).
- Кнопка «Изменить»: бот просит ответить на черновик новым текстом. Если ответ начинается с `!`, это пожелание для перегенерации («!короче»), иначе это готовый текст, который отправляется через `engine.approve(id, text)`.
- Звук: `draft`, `reply`, `incident`, `ask_*` со звуком. `digest` и `remove_draft` с `disable_notification=True`. `remove_draft` удаляет сообщение черновика.
- Сетевые ошибки Telegram: повтор с паузой 5/15/60 с, потом в журнал. Уведомление не теряется, пока программа работает (очередь в памяти).

- [ ] **Step 1: Failing tests** для `bot_text`:

```python
def test_format_draft_contains_essentials():
    s = format_draft(ROW, "Sehr geehrte...", warm_estimated=True)
    assert ROW.url in s and "≈" in s and "Sehr geehrte" in s

def test_parse_filters_args():
    assert parse_filters_args(["max_warm", "950"]) == {"max_warm": 950}
    with pytest.raises(ValueError): parse_filters_args(["max_warm", "много"])

def test_parse_profile_answer():
    assert parse_profile_answer("persons", "2") == 2
    assert parse_profile_answer("wbs", "нет") is False
    assert parse_profile_answer("documents", "Schufa, Gehaltsnachweise") == ["Schufa", "Gehaltsnachweise"]

def test_digest_lists_filter_reasons():
    s = format_digest({"found": 12, "sent": 3, "filtered": {"дороже 900 €": 7}, "duplicates": 1, "replies": 1, "failed": 0})
    assert "12" in s and "дороже 900 €: 7" in s
```
- [ ] **Step 2–4:** Run → FAIL. Реализовать `bot_text` и `bot.py`. Run → PASS.
- [ ] **Step 5: Ручная проверка с пользователем:** токен из BotFather, `/start`, заполнить `/profile`, `/status`. Черновик из фейкового объявления (через `scripts/fake_draft.py`, который вызывает `notifier.draft` с тестовой строкой) с кнопками «Изменить» и «Пропустить». Сообщение от второго аккаунта Telegram остаётся без ответа.
- [ ] **Step 6: Commit** `feat: telegram bot`

---

### Task 14: Окно программы

**Files:**
- Modify: `app.py`, `main.py`
- Create: `tests/test_report.py`

**Interfaces:**
- Consumes: `Engine`, `Store`, `Browser.show_login`, `MailWatcher.check_login`, `start_bot`, блоки нейросетей из образца (`CliBlock`, `KeyBlock`, `zai_paste_key`, `check_compat`).
- Produces: `export_report(store: Store, path: Path, since: date | None) -> int` (в `app.py` или `report.py`).

Вкладки:
- **«Статус»**: по каждой площадке «включена / пауза / проблема / вход нужен», когда был последний опрос, сколько отправлено сегодня из лимита. Кнопки «Войти» (`show_login`, после закрытия окна площадки `is_logged_in` → `resume`) и «Пауза». После первого успешного входа на площадку — одно напоминание: включить в её настройках e-mail-уведомления о новых сообщениях (иначе ответы не дойдут до бота); флаг `platforms[p]["mail_hint_shown"]`. Общая кнопка «▶ Запустить / ■ Остановить».
- **«Настройки»**: блок «Нейросеть» из образца без изменений, токен бота, Gmail и пароль приложения с кнопкой «Проверить», автозапуск, автообновление.
- **«Журнал»**: как в образце.
- **«Отчёт»**: таблица отправленных (дата, площадка, заголовок, цена, статус, ответ да/нет) и «Выгрузить в Excel».

Цикл asyncio в фоновом потоке, как в образце (`Engine._submit`). При запуске с `--autostart` работа начинается через 30 с. Значок трея показывает статус.

- [ ] **Step 1: Failing test:** `test_export_report` — 3 записи в Store (2 `sent`, 1 `filtered`) → `export_report` возвращает 2, `openpyxl` читает 3 строки (заголовок + 2).
- [ ] **Step 2–4:** Run → FAIL. Реализовать окно и выгрузку. Run → PASS.
- [ ] **Step 5: Ручная проверка с пользователем:** `python main.py`: вход на Kleinanzeigen через кнопку, Gmail «Проверить» → OK, «Запустить» → в журнале видны опросы, черновик приходит в бот, «Отправить» отправляет письмо (на объявление, которое выбрал пользователь).
- [ ] **Step 6: Commit** `feat: desktop window for wohnung search`

---

### Task 15: ImmoScout24

Как Task 8. Файлы: `sources/immoscout.py`, `tests/test_immoscout.py`, `tests/fixtures/immoscout/*`. `class ImmoScout(Source)`, `name = "immoscout"`. Особенности:
- `detect_block`: страница защиты от ботов («Ich bin kein Roboter», «Du bist ein Mensch?», страницы Imperva/hCaptcha) → `"captcha"`. Сохранить такую страницу в фикстуру, если она попадётся при захвате, и покрыть тестом `test_detect_captcha`.
- Объявления «nur für MieterPlus»: `send` бросает `MissingFieldError("MieterPlus")`, и объявление уходит черновиком с пометкой. Пользователь решает сам.
- Лимиты из Global Constraints уже в `DEFAULTS`.
- Ручная проверка с пользователем, как в Task 8, Step 6.
- Коммит `feat: ImmoScout24 source`.

---

### Task 16: Сборка и первый релиз

**Files:**
- Modify: `build.py`, `installer.iss`, `.github/workflows/release.yml`, `README.md`, `RELEASE_NOTES.md`

- [ ] **Step 1:** `build.py`: `NAME = "Auto Wohnung Search"`, в PyInstaller добавить `prompt.example.txt` и `--collect-all playwright` (драйвер Playwright; браузер не нужен — msedge). `installer.iss`: AppName, AppId (новый GUID), имя `Auto-Wohnung-Search-<версия>-Setup.exe`. В CI убрать секреты TG и переименовать заголовок релиза. `updater.ASSET_SUFFIX = "-Setup.exe"` без изменений.
- [ ] **Step 2:** В CI перед сборкой добавить шаг `pytest -q`.
- [ ] **Step 3:** `README.md` на русском: что делает программа, установка, первый запуск (Gmail и пароль приложения, BotFather, вход на площадки), команды бота, предупреждение о правилах площадок и риске бана.
- [ ] **Step 4:** Локально: `python build.py` → `dist/Auto-Wohnung-Search-0.1.0-Setup.exe`. Установить, запустить, убедиться, что окно открывается и Edge стартует.
- [ ] **Step 5: Commit** `build: installer and release workflow`
- [ ] **Step 6:** Только после явного «да» пользователя: `git tag v0.1.0 && git push origin main v0.1.0`. Проверить, что Actions собрал релиз и в нём есть `*-Setup.exe`.

---

## Не входит в этот план

Сайты VEBOWAG, Vonovia, LEG и Vivawest (вторая очередь по спеке) — отдельный план после v0.1.0. Каждый станет ещё одним `Source` по образцу Task 8.
