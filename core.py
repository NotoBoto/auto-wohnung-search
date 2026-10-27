"""
Ядро Auto Wohnung Search: настройки, автозапуск и прокси.
"""
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

VERSION = "0.2.0"
REPO = "NotoBoto/auto-wohnung-search"   # отсюда берутся обновления (GitHub Releases)

# Установленная версия (.exe из установщика) хранит данные в %APPDATA% — обновление и
# переустановка их не трогают. Запуск из исходников — всё рядом с app.py.
FROZEN = getattr(sys, "frozen", False)
RES = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))   # файлы внутри программы
BASE = (Path(os.environ.get("APPDATA") or Path.home()) / "Auto Wohnung Search") if FROZEN else Path(__file__).parent
BASE.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = BASE / "config.json"


DEFAULTS = {
    "backend": "zai",              # zai | claude_code | api | gemini_cli | gemini_api | codex | openai_api | openai_compat
    "model": "sonnet", "api_key": "", "api_model": "claude-opus-5-5",
    "gemini_model": "", "gemini_api_key": "", "gemini_api_model": "gemini-flash-latest",
    "codex_model": "", "openai_api_key": "", "openai_api_model": "gpt-5.5",
    "compat_base_url": "http://localhost:1234/v1", "compat_api_key": "", "compat_model": "",
    "zai_api_key": "", "zai_model": "glm-4.7-flash", "claude_timeout_sec": 120,
    "prompt_file": "prompt.txt",
    "bot_token": "", "owner_chat_id": 0,
    "gmail_user": "", "gmail_app_password": "", "mail_poll_sec": 90,
    "mode": "confirm",             # confirm | auto — глобально, площадка может переопределить
    "digest_hour": 20, "draft_ttl_hours": 24, "draft_remind_hours": 2,
    "filters": {"city": "Bonn", "max_warm": 900, "min_rooms": 2.0, "min_lease_months": 12},
    "profile": {},
    "platforms": {
        "kleinanzeigen": {"enabled": True, "mode": None, "daily_cap": 15, "min_gap_sec": 120, "poll": [180, 360]},
        "wggesucht":     {"enabled": True, "mode": None, "daily_cap": 15, "min_gap_sec": 120, "poll": [180, 360]},
        "immowelt":      {"enabled": True, "mode": None, "daily_cap": 15, "min_gap_sec": 120, "poll": [180, 360]},
        # реже остальных: при частом опросе ImmoScout показывает «Ich bin kein Roboter» (вживую 2026-10-27)
        "immoscout":     {"enabled": True, "mode": None, "daily_cap": 10, "min_gap_sec": 240, "poll": [600, 1200]},
    },
    "night_poll": [900, 1200], "contact_cap_7d": 3, "dedup_days": 60,
    "auto_update": True, "autostart": False,
}

# прежние значения по умолчанию → новые: config.json хранит все ключи, поэтому новое значение само не придёт.
# Меняем, только если значение равно прежнему по умолчанию: в окне и боте эти ключи не настраиваются, так что
# такое значение — почти наверняка не выбор владельца (вписанное вручную в config.json другое число не трогаем).
_RETIRED_DEFAULTS = (
    (("draft_ttl_hours",), 12, 24),
    (("platforms", "immoscout", "poll"), [300, 480], [600, 1200]),
)

log = logging.getLogger("wohnung")


_save_lock = threading.Lock()   # config.json пишут окно и бот (поток asyncio)
config_backup: Path | None = None   # копия повреждённого config.json, сделанная последним load_config()


def _backup_corrupt(text: str) -> Path:
    path = CONFIG_FILE.with_name(f"{CONFIG_FILE.name}.bad-{datetime.now():%Y%m%d-%H%M%S}")
    path.write_text(text, encoding="utf-8")
    return path


def load_config() -> dict:
    """Читает config.json; вложенные словари (filters, platforms.*) дополняет недостающими ключами.
    Повреждённый файл копируется в config.json.bad-<время> (секреты остаются там), берутся значения по
    умолчанию, но владелец бота (owner_chat_id) достаётся из текста: иначе бота займёт первый написавший."""
    global config_backup
    config_backup = None
    data = {}
    if CONFIG_FILE.exists():
        text = CONFIG_FILE.read_text(encoding="utf-8")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            config_backup = _backup_corrupt(text)
            log.error("config.json повреждён (%s) — взяты значения по умолчанию, копия: %s", e, config_backup)
            m = re.search(r'"owner_chat_id"\s*:\s*(-?\d+)', text)
            data = {"owner_chat_id": int(m.group(1))} if m else {}
    cfg = json.loads(json.dumps(DEFAULTS))   # глубокая копия
    for k, v in data.items():
        if k == "filters" and isinstance(v, dict):
            cfg["filters"].update(v)
        elif k == "platforms" and isinstance(v, dict):
            for name, pv in v.items():
                if isinstance(pv, dict):
                    cfg["platforms"].setdefault(name, {}).update(pv)
        else:
            cfg[k] = v
    for path, old, new in _RETIRED_DEFAULTS:
        *parents, key = path
        d = cfg
        for p in parents:
            d = d.get(p) if isinstance(d, dict) else None
        if isinstance(d, dict) and d.get(key) == old:
            d[key] = new
    return cfg


def save_config(cfg: dict):
    """Атомарно: временный файл и os.replace — оборванная запись не портит config.json."""
    text = json.dumps(cfg, ensure_ascii=False, indent=2)   # до записи: сбой сериализации файл не трогает
    with _save_lock:
        tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        for attempt in range(3):
            try:
                os.replace(tmp, CONFIG_FILE)
                return
            except PermissionError:   # Windows: файл на миг открыт другим (антивирус, редактор)
                if attempt == 2:
                    raise
                time.sleep(0.1)


def prompt_path(cfg) -> Path:
    return BASE / cfg["prompt_file"]


# ---------- прокси ----------

def system_proxy() -> str | None:
    """Прокси из переменных окружения или настроек Windows (его выставляет VPN-клиент)."""
    from urllib.request import getproxies
    p = getproxies()
    return p.get("https") or p.get("http") or p.get("socks") or p.get("all")



def cli_env(drop=()) -> dict:
    """Окружение для CLI нейросетей и установщиков: системный HTTP-прокси передаём переменными."""
    env = {k: v for k, v in os.environ.items() if k not in drop}
    if not any(k.upper() == "HTTPS_PROXY" for k in env):
        p = system_proxy()
        if p and p.startswith("http"):
            env["HTTPS_PROXY"] = env["HTTP_PROXY"] = p
            env.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")   # вход через браузер идёт на localhost
    return env


# ---------- автозапуск с Windows ----------
# Запись в HKCU\…\Run: только для текущего пользователя, прав администратора не нужно.

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Auto Wohnung Search"
AUTOSTART_ARG = "--autostart"    # с ним окно стартует в трее и через AUTO_RUN_DELAY сек начинает работу
AUTO_RUN_DELAY = 30              # время на подключение VPN и сети после входа в Windows


def autostart_command() -> str:
    if FROZEN:
        return f'"{sys.executable}" {AUTOSTART_ARG}'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")   # без чёрного окна консоли
    return f'"{pyw if pyw.exists() else exe}" "{Path(__file__).parent / "app.py"}" {AUTOSTART_ARG}'


def get_autostart() -> str | None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, RUN_NAME)[0]
    except OSError:
        return None


def set_autostart(on: bool):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass
