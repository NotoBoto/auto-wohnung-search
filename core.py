"""
Ядро Auto Wohnung Search: настройки, автозапуск, прокси и вспомогательные функции нейросетей.
"""
import base64
import json
import logging
import mimetypes
import os
import random
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

VERSION = "0.1.0"
REPO = "NotoBoto/auto-wohnung-search"   # отсюда берутся обновления (GitHub Releases)

# Установленная версия (.exe из установщика) хранит данные в %APPDATA% — обновление и
# переустановка их не трогают. Запуск из исходников — всё рядом с app.py.
FROZEN = getattr(sys, "frozen", False)
RES = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))   # файлы внутри программы
BASE = (Path(os.environ.get("APPDATA") or Path.home()) / "Auto Wohnung Search") if FROZEN else Path(__file__).parent
BASE.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = BASE / "config.json"

MODELS = ["sonnet", "opus", "haiku"]          # для Claude Code
API_MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"]
API_PRICES = {  # $ за 1M токенов: вход / выход
    "claude-opus-5-5": "$4 / $20", "claude-sonnet-5-5": "$2 / $10", "claude-haiku-4-5": "$1 / $5"}
GEMINI_API_DEFAULT = "gemini-flash-latest"

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

log = logging.getLogger("wohnung")


def load_config() -> dict:
    """Читает config.json; вложенные словари (filters, platforms.*) дополняет недостающими ключами."""
    data = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            log.error("config.json повреждён (%s) — взяты значения по умолчанию", e)
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
    return cfg


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def prompt_path(cfg) -> Path:
    return BASE / cfg["prompt_file"]


NATIVE_CLAUDE = Path.home() / ".local" / "bin" / "claude.exe"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def find_claude() -> str | None:
    # На Windows предпочитаем настоящий .exe, а не обёртку .cmd
    found = shutil.which("claude.exe") or shutil.which("claude")
    if found:
        return found
    return str(NATIVE_CLAUDE) if NATIVE_CLAUDE.exists() else None


def claude_status() -> dict:
    """{'installed', 'loggedIn', 'email', 'authMethod', 'subscriptionType'}"""
    exe = find_claude()
    if not exe:
        return {"installed": False, "loggedIn": False}
    try:
        r = subprocess.run([exe, "auth", "status", "--json"], capture_output=True,
                           timeout=30, env=cli_env(), creationflags=NO_WINDOW)
        info = json.loads(decode_any(r.stdout))
    except Exception:
        info = {"loggedIn": False}
    info["installed"] = True
    return info


def api_key(cfg) -> str | None:
    return str(cfg.get("api_key", "")).strip() or os.environ.get("ANTHROPIC_API_KEY")


def check_api_key(cfg) -> tuple[bool, str]:
    """Проверка ключа без траты токенов: запрашиваем описание выбранной модели."""
    import anthropic
    key = api_key(cfg)
    if not key:
        return False, "Ключ не указан"
    try:
        m = anthropic.Anthropic(api_key=key, timeout=20, max_retries=1).models.retrieve(
            cfg.get("api_model", API_MODELS[0]))
        return True, f"Ключ работает ✓  модель {m.display_name} доступна"
    except anthropic.AuthenticationError:
        return False, "Ключ не подходит"
    except anthropic.NotFoundError:
        return False, "Ключ работает, но модель недоступна для этого аккаунта"
    except anthropic.APIConnectionError:
        return False, "Нет связи с API — проверьте интернет"
    except anthropic.APIStatusError as e:
        return False, f"Ошибка API ({e.status_code}): {e.message}"


def install_claude() -> tuple[bool, str]:
    """Официальный установщик Claude Code для Windows (Node.js не нужен)."""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
         "irm https://claude.ai/install.ps1 | iex"],
        capture_output=True, timeout=600, env=cli_env(), creationflags=NO_WINDOW)
    out = (decode_any(r.stdout) + "\n" + decode_any(r.stderr)).strip()
    return find_claude() is not None, out


def open_claude_login():
    """Открывает вход в отдельном окне консоли: там ссылка/браузер и, если нужно, поле для кода."""
    subprocess.Popen([find_claude(), "auth", "login", "--claudeai"],
                     env=cli_env(), creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))



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



BACKEND_NAMES = {"claude_code": "Claude Code", "api": "Claude API", "gemini_cli": "Antigravity",
                 "gemini_api": "Gemini API", "codex": "Codex", "openai_api": "OpenAI API",
                 "zai": "Z.ai", "openai_compat": "Своя модель"}

# Так сервисы отвечают на запрос из страны, где они не работают
REGION_MARKERS = ("not currently available in your location", "location is not supported",
                  "unsupported_country", "unsupported country", "not available in your country",
                  "region, or territory not supported", "request not allowed")


def explain_ai_error(name: str, msg: str) -> str:
    """Текст ошибки нейросети для журнала и очереди; отказ по стране — с подсказкой про VPN."""
    msg = msg.removeprefix(f"{name}: ").strip()
    text = msg if msg.startswith(f"Ошибка {name}") else f"Ошибка {name}: {msg}"
    if any(m in msg.lower() for m in REGION_MARKERS):
        sp = system_proxy()
        route = f"через системный прокси {sp}" if sp else "без системного прокси (через VPN в режиме TUN или напрямую)"
        text += (f"\n{name} недоступен из страны, откуда пришёл запрос. Включите VPN с сервером там, где сервис "
                 f"работает (например, США или Европа) — в режиме TUN или «системный прокси» — и повторите. "
                 f"Сейчас запросы идут {route}. Не помогает — попробуйте другой сервер VPN или другой "
                 "способ подключения в «Настройках». Без VPN работает «Z.ai (бесплатно)».")
    return text


def decode_any(data: bytes) -> str:
    """Windows-консоль может отдавать ошибки в cp866/cp1251 — пробуем все."""
    for enc in ("utf-8", "cp866", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


# ---------- Antigravity CLI (Google, преемник Gemini CLI) ----------
# С 18.06.2026 Gemini CLI не обслуживает личные Google-аккаунты — вместо него agy.

AGY_DEFAULT = Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe"
AGY_AGENT = "autocomment"


def find_agy() -> str | None:
    found = shutil.which("agy.exe") or shutil.which("agy")
    if found:
        return found
    return str(AGY_DEFAULT) if AGY_DEFAULT.exists() else None


def agy_status() -> dict:
    """`agy models` без входа сразу отвечает «Please sign in», после входа — списком моделей."""
    exe = find_agy()
    if not exe:
        return {"installed": False, "loggedIn": False}
    try:
        r = subprocess.run([exe, "models"], capture_output=True, timeout=40,
                           stdin=subprocess.DEVNULL, env=cli_env(), creationflags=NO_WINDOW)
        out = decode_any(r.stdout) + decode_any(r.stderr)
    except subprocess.TimeoutExpired:
        return {"installed": True, "loggedIn": False}
    if "sign in" in out.lower() or "authentication required" in out.lower():
        return {"installed": True, "loggedIn": False}
    # Строки вида «gemini-3.8-flash-medium<TAB>Gemini 3.8 Flash (Medium)»; есть и Claude, и GPT
    models = [line.split("\t")[0].strip() for line in out.splitlines() if "\t" in line]
    return {"installed": True, "loggedIn": r.returncode == 0, "models": list(dict.fromkeys(models))}


def install_agy() -> tuple[bool, str]:
    """Официальный установщик Antigravity CLI для Windows."""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
         "irm https://antigravity.google/cli/install.ps1 | iex"],
        capture_output=True, timeout=600, env=cli_env(), creationflags=NO_WINDOW)
    out = (decode_any(r.stdout) + "\n" + decode_any(r.stderr)).strip()
    return find_agy() is not None, out


def open_agy_login():
    """Первый запуск agy без аргументов открывает браузер для входа в Google-аккаунт."""
    subprocess.Popen([find_agy()], cwd=str(Path.home()),
                     env=cli_env(), creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))


# ---------- Gemini API ----------

def gemini_key(cfg) -> str | None:
    return (str(cfg.get("gemini_api_key", "")).strip()
            or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


def check_gemini_key(cfg) -> tuple[bool, str, list[str]]:
    """Проверка ключа без траты токенов: получаем список доступных моделей."""
    from google import genai
    from google.genai import errors as gerr
    key = gemini_key(cfg)
    if not key:
        return False, "Ключ не указан", []
    try:
        client = genai.Client(api_key=key)
        models = sorted(m.name.removeprefix("models/") for m in client.models.list()
                        if "generateContent" in (m.supported_actions or []) and "gemini" in m.name)
    except gerr.APIError as e:
        if e.code in (400, 401, 403):
            return False, "Ключ не подходит", []
        return False, f"Ошибка API ({e.code}): {e.message}", []
    except Exception as e:
        return False, f"Нет связи с Gemini API: {e}", []
    model = cfg.get("gemini_api_model") or GEMINI_API_DEFAULT
    if model not in models:
        return True, f"Ключ работает ✓, но модели {model} нет — выберите из списка", models
    return True, f"Ключ работает ✓  доступно моделей: {len(models)}", models


# ---------- ChatGPT: Codex CLI по подписке и OpenAI API по ключу ----------

NPM_GLOBAL = Path(os.environ.get("APPDATA", "")) / "npm"
NODE_DEFAULT = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs"
CODEX_DIR = Path.home() / ".codex"
# Лишние для комментариев возможности Codex: меньше служебного текста — меньше расход лимита
CODEX_FEATURES_OFF = ["apps", "plugins", "multi_agent", "image_generation", "browser_use",
                      "computer_use", "goals", "shell_tool", "unified_exec"]
OPENAI_API_DEFAULT = "gpt-5.5"


def find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    exe = NODE_DEFAULT / "node.exe"
    return str(exe) if exe.exists() else None


def find_codex() -> str | None:
    """Запускаем сам codex.exe из npm-пакета, минуя codex.cmd: cmd.exe портит кириллицу в аргументах."""
    roots = [NPM_GLOBAL / "node_modules" / "@openai"]
    if shutil.which("codex"):
        roots.append(Path(shutil.which("codex")).parent / "node_modules" / "@openai")
    for root in roots:
        for pattern in ("codex/node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe",
                        "codex-win32-*/vendor/*/bin/codex.exe"):
            found = sorted(root.glob(pattern))
            if found:
                return str(found[0])
    return None


def _codex_account() -> dict:
    """Почта и тариф из id_token в ~/.codex/auth.json (сам токен никуда не передаём)."""
    try:
        auth = json.loads((CODEX_DIR / "auth.json").read_text(encoding="utf-8"))
        payload = auth["tokens"]["id_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        plan = claims.get("https://api.openai.com/auth", {}).get("chatgpt_plan_type", "")
        return {"email": claims.get("email", ""), "plan": plan}
    except Exception:
        return {}


def codex_status() -> dict:
    exe = find_codex()
    if not exe:
        return {"installed": False, "loggedIn": False}
    try:
        r = subprocess.run([exe, "login", "status"], capture_output=True, timeout=30,
                           stdin=subprocess.DEVNULL, env=cli_env(), creationflags=NO_WINDOW)
        out = (decode_any(r.stdout) + decode_any(r.stderr)).lower()
    except Exception:
        return {"installed": True, "loggedIn": False}
    if r.returncode != 0 or "not logged in" in out:
        return {"installed": True, "loggedIn": False}
    st = {"installed": True, "loggedIn": True,
          "authMethod": "chatgpt" if "chatgpt" in out else "api", **_codex_account()}
    try:   # модели, доступные этому аккаунту
        r = subprocess.run([exe, "debug", "models"], capture_output=True, timeout=30,
                           stdin=subprocess.DEVNULL, env=cli_env(), creationflags=NO_WINDOW)
        data = json.loads(decode_any(r.stdout))
        st["models"] = [m["slug"] for m in data.get("models", data)
                        if m.get("visibility") == "list" and "image" in (m.get("input_modalities") or [])]
    except Exception:
        pass
    return st


def install_codex() -> tuple[bool, str]:
    """Node.js (если нет) через winget, затем Codex CLI через npm."""
    out = ""
    if not find_node():
        r = subprocess.run(["winget", "install", "-e", "--id", "OpenJS.NodeJS.LTS", "--silent",
                            "--accept-package-agreements", "--accept-source-agreements"],
                           capture_output=True, timeout=900, env=cli_env(), creationflags=NO_WINDOW)
        out += decode_any(r.stdout) + decode_any(r.stderr)
        if not find_node():
            return False, out + "\nНе удалось установить Node.js"
    npm = Path(find_node()).parent / "npm.cmd"
    r = subprocess.run([str(npm), "install", "-g", "@openai/codex"], capture_output=True,
                       timeout=900, env=cli_env(), creationflags=NO_WINDOW)
    out += decode_any(r.stdout) + decode_any(r.stderr)
    return find_codex() is not None, out.strip()


def open_codex_login():
    """Вход через аккаунт ChatGPT. Если уже вошли — сначала выходим, чтобы сменить аккаунт."""
    exe = find_codex()
    if codex_status().get("loggedIn"):
        subprocess.run([exe, "logout"], capture_output=True, timeout=30, env=cli_env(), creationflags=NO_WINDOW)
    subprocess.Popen([exe, "login"], cwd=str(Path.home()),
                     env=cli_env(), creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))


def openai_key(cfg) -> str | None:
    return str(cfg.get("openai_api_key", "")).strip() or os.environ.get("OPENAI_API_KEY")


def check_openai_key(cfg) -> tuple[bool, str, list[str]]:
    """Проверка ключа без траты токенов: получаем список доступных моделей."""
    import openai
    key = openai_key(cfg)
    if not key:
        return False, "Ключ не указан", []
    try:
        client = openai.OpenAI(api_key=key, timeout=20, max_retries=1)
        models = sorted(m.id for m in client.models.list()
                        if m.id.startswith(("gpt-", "o")) and not any(
                            x in m.id for x in ("audio", "realtime", "tts", "transcribe", "image", "embedding", "search")))
    except openai.AuthenticationError:
        return False, "Ключ не подходит", []
    except openai.APIConnectionError:
        return False, "Нет связи с OpenAI API — проверьте интернет", []
    except openai.APIStatusError as e:
        return False, f"Ошибка API ({e.status_code}): {e.message}", []
    model = cfg.get("openai_api_model") or OPENAI_API_DEFAULT
    if model not in models:
        return True, f"Ключ работает ✓, но модели {model} нет — выберите из списка", models
    return True, f"Ключ работает ✓  доступно моделей: {len(models)}", models


# ---------- своя модель: любой OpenAI-совместимый сервер ----------
# LM Studio, Ollama, llama.cpp, vLLM — локально; OpenRouter, DeepSeek и т. п. — в облаке.
# Запросы — классическим chat/completions: его понимают все такие серверы.

COMPAT_PRESETS = {"LM Studio": "http://localhost:1234/v1", "Ollama": "http://localhost:11434/v1"}


def is_local_url(url: str) -> bool:
    """Сервер на этом компьютере или в домашней сети — к нему ходим мимо системного прокси (VPN)."""
    import ipaddress
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").lower()
    if host in ("localhost", "") or host.endswith((".local", ".lan")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def compat_client(cfg, asynchronous=False):
    import httpx
    import openai
    url = str(cfg.get("compat_base_url", "")).strip().rstrip("/")
    key = str(cfg.get("compat_api_key", "")).strip() or "not-needed"   # локальным серверам ключ не нужен
    local = is_local_url(url)
    if asynchronous:
        http = httpx.AsyncClient(trust_env=False) if local else None
        return openai.AsyncOpenAI(base_url=url, api_key=key, http_client=http, max_retries=1)
    http = httpx.Client(trust_env=False) if local else None
    return openai.OpenAI(base_url=url, api_key=key, http_client=http, timeout=20, max_retries=0)


def chat_content(user_msg: str, images: list[Path]) -> str | list:
    """Сообщение для chat/completions: текст, а с картинками — текст и картинки в data:-URL."""
    if not images:
        return user_msg
    content = [{"type": "text", "text": user_msg + "\n\nК посту приложены картинки — учти, что на них. "
                                                   "Выведи только сам комментарий."}]
    for img in images:
        mime = mimetypes.guess_type(img.name)[0] or "image/jpeg"
        content.append({"type": "image_url", "image_url": {
            "url": f"data:{mime};base64,{base64.b64encode(img.read_bytes()).decode()}"}})
    return content


def compat_connection_hint(url: str) -> str:
    if is_local_url(url):
        return (f"Нет связи с {url} — запущен ли сервер? LM Studio: вкладка Developer → Start Server; "
                "Ollama: программа должна быть запущена")
    return f"Нет связи с {url} — проверьте адрес и интернет (или VPN)"


def check_compat(cfg) -> tuple[bool, str, list[str]]:
    """Проверка подключения без генерации: запрашиваем список моделей сервера."""
    import openai
    url = str(cfg.get("compat_base_url", "")).strip()
    if not url:
        return False, "Адрес сервера не указан", []
    try:
        with compat_client(cfg) as client:
            models = sorted(m.id for m in client.models.list())
    except openai.APIConnectionError:
        return False, compat_connection_hint(url), []
    except openai.AuthenticationError:
        return False, "Сервер требует API-ключ (или ключ не подходит)", []
    except openai.NotFoundError:
        return False, "По этому адресу нет OpenAI-совместимого API — адрес обычно оканчивается на /v1", []
    except openai.APIStatusError as e:
        return False, f"Ошибка сервера ({e.status_code}): {e.message}", []
    except Exception as e:
        return False, f"Не удалось подключиться: {e}", []
    if not models:
        return True, "Сервер работает ✓, но моделей нет — загрузите модель (в LM Studio — «Load model»)", []
    model = str(cfg.get("compat_model", "")).strip()
    if model and model not in models:
        return True, f"Сервер работает ✓, но модели {model} нет — выберите из списка", models
    return True, f"Сервер работает ✓  моделей: {len(models)}", models


# ---------- Z.ai (GLM): бесплатные модели, работает из России без VPN и без карты ----------
# API OpenAI-совместимый. Бесплатные модели отвечают по одному запросу за раз на ключ.

ZAI_URL = "https://api.z.ai/api/paas/v4"
ZAI_KEYS_URL = "https://z.ai/manage-apikey/apikey-list"
ZAI_MODELS = ["glm-4.7-flash", "glm-4.5-flash"]   # бесплатные текстовые — по порядку предпочтения
ZAI_VISION_MODEL = "glm-4.6v-flash"              # бесплатная, видит картинки поста
ZAI_KEY_RE = re.compile(r"^[0-9A-Za-z]{24,64}\.[0-9A-Za-z]{8,32}$")   # так выглядит ключ: «a1b2…f9.XyZ…»
ZAI_RETRY_DELAYS = (5, 15, 30)   # паузы перед повтором, когда Z.ai занят или сбоит
ZAI_CHECK_WAIT = 120             # сколько проверка ключа ждёт, пока аккаунты допишут свои запросы, сек
_zai_locks: dict[str, threading.Lock] = {}   # по ключу: профили работают каждый в своём потоке
_zai_locks_guard = threading.Lock()
_zai_gone: set[str] = set()          # модели, которые Z.ai перестал узнавать (код 1211) — дальше их пропускаем
_zai_no_thinking: set[str] = set()   # модели, которые не приняли параметр thinking — шлём без него


def zai_key(cfg) -> str | None:
    return str(cfg.get("zai_api_key", "")).strip() or os.environ.get("ZAI_API_KEY")


def zai_lock(key: str) -> threading.Lock:
    with _zai_locks_guard:
        return _zai_locks.setdefault(key, threading.Lock())


def zai_client(key: str, asynchronous=False):
    import openai
    if asynchronous:
        return openai.AsyncOpenAI(base_url=ZAI_URL, api_key=key, max_retries=0)   # повторы — свои, с паузами
    return openai.OpenAI(base_url=ZAI_URL, api_key=key, timeout=30, max_retries=0)


def zai_extra(model: str) -> dict | None:
    """Для комментария думать незачем — без «размышлений» быстрее. Не принявшим параметр — не шлём."""
    return None if model in _zai_no_thinking else {"thinking": {"type": "disabled"}}


def zai_thinking_rejected(model: str, status: int, msg: str) -> bool:
    """Модель не приняла thinking — запоминаем, чтобы повторить без него. True — стоит повторить."""
    if status == 400 and "thinking" in msg.lower() and model not in _zai_no_thinking:
        _zai_no_thinking.add(model)
        return True
    return False


def zai_error(e) -> tuple[str, str]:
    """Код и текст ошибки Z.ai: {"error": {"code": "1302", "message": "…"}}."""
    body = getattr(e, "body", None)
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body = body["error"]
    if not isinstance(body, dict):
        body = {}
    return str(body.get("code", "")), str(body.get("message") or getattr(e, "message", "") or e)


ZAI_BAD_KEY = "Ключ Z.ai не подходит — скопируйте его заново на z.ai («Получить бесплатный ключ»)"
ZAI_BUSY_CODES = {"1302", "1305", "1313"}            # «слишком часто» и «перегружен» — подождать и повторить
ZAI_LIMIT_CODES = {"1308", "1310", "1316", "1317"}   # исчерпан лимит за период — повтор не поможет


def zai_busy(status: int, code: str) -> bool:
    """Временная занятость или сбой Z.ai — стоит подождать и повторить."""
    return code in ZAI_BUSY_CODES or status >= 500 or (status == 429 and not code)


def zai_error_text(status: int, code: str, msg: str, model: str) -> str:
    if code in ZAI_LIMIT_CODES:
        return f"Исчерпан бесплатный лимит Z.ai — {msg}"
    choose = (f"выберите бесплатную {ZAI_MODELS[0]} в «Настройках»" if model not in ZAI_MODELS
              else "похоже, бесплатной она больше не является — выберите другую модель в «Настройках»")
    if code == "1113":
        return f"Z.ai просит пополнить баланс для модели {model} — {choose}"
    if code == "1311":
        return f"Модель {model} не входит в ваш тариф Z.ai — {choose}"
    if code == "1211":
        return f"Z.ai не знает модель {model} — выберите другую в «Настройках»"
    if code == "1301":
        return "Z.ai отказался отвечать: посчитал пост или промпт недопустимым"
    return f"Ошибка Z.ai ({status}): {msg}"


def zai_models_order(model: str) -> list[str]:
    """Выбранная модель, за ней остальные бесплатные — на случай, если Z.ai какую-то отключил."""
    return [m for m in dict.fromkeys([model, *ZAI_MODELS]) if m not in _zai_gone] or [model]


def check_zai_key(cfg) -> tuple[bool, str, list[str]]:
    """Проверка ключа крошечным запросом к модели — для бесплатной ничего не стоит.
    Возвращает список моделей, первой — рабочую (её и выберет окно)."""
    import openai
    key = zai_key(cfg)
    if not key:
        return False, "Ключ не указан", []
    model = str(cfg.get("zai_model", "")).strip() or ZAI_MODELS[0]
    models = zai_models_order(model)
    lock = zai_lock(key)   # бесплатно — по одному запросу на ключ: не мешаем работающим аккаунтам
    if not lock.acquire(timeout=ZAI_CHECK_WAIT):
        return False, "Z.ai занят запросами аккаунтов — проверьте ключ чуть позже", []
    try:
        with zai_client(key) as client:
            while True:
                try:
                    client.chat.completions.create(
                        model=models[0], max_tokens=1, messages=[{"role": "user", "content": "Привет"}],
                        extra_body=zai_extra(models[0]))
                    break
                except openai.BadRequestError as e:
                    code, msg = zai_error(e)
                    if code == "1211" and len(models) > 1:
                        _zai_gone.add(models.pop(0))
                    elif not zai_thinking_rejected(models[0], e.status_code, msg):
                        raise
    except openai.AuthenticationError:
        return False, ZAI_BAD_KEY, []
    except openai.APIConnectionError:
        return False, "Нет связи с Z.ai — проверьте интернет", []
    except openai.APIStatusError as e:
        code, msg = zai_error(e)
        if code in ZAI_BUSY_CODES:   # до лимита дошли — значит, ключ рабочий
            return True, "Ключ работает ✓ (Z.ai сейчас занят) — нажмите «Сохранить»", models
        return False, zai_error_text(e.status_code, code, msg, models[0]), []
    finally:
        lock.release()
    if models[0] != model:
        return True, f"Ключ работает ✓, но модели {model} больше нет — выбрана {models[0]}. " \
                     "Нажмите «Сохранить»", models
    return True, "Ключ работает ✓ — нажмите «Сохранить»", models


VARIANTS_RE = re.compile(r"\{\{(.+?)\}\}", re.S)


def pick_variants(prompt: str) -> str:
    """{{вариант 1 | вариант 2 | …}} в промпте — к каждому запросу случайный вариант. Слабые модели
    из списка приёмов всегда берут один и тот же, а так комментарии получаются разными."""
    return VARIANTS_RE.sub(lambda m: random.choice(m.group(1).split("|")).strip(), prompt)


# Символы, которых нет на клавиатуре телефона, — сразу выдают нейросеть. Меняем на то, что напечатал бы человек.
HUMAN_TYPOGRAPHY = str.maketrans({"—": "-", "–": "-", "−": "-", "«": '"', "»": '"', "„": '"', "“": '"',
                                  "”": '"', "’": "'", "‘": "'", "…": "...", "×": "x",
                                  "\u00a0": " ", "\u202f": " ", "\u2009": " "})


def human_typography(text: str) -> str:
    """Длинные тире, ёлочки, многоточие одним знаком, × и неразрывные пробелы — в обычные символы;
    жирный markdown (**…**) Telegram показал бы звёздочками — убираем."""
    return text.translate(HUMAN_TYPOGRAPHY).replace("**", "") if text else text


def copies_post(text: str, post: str) -> bool:
    """Ответ — в основном куски поста дословно (от 20 символов подряд), а не свой комментарий.
    Считаем каждый символ ответа, попавший в такой кусок: модель бывает повторяет пост и дважды."""
    a, b = " ".join(text.lower().split()), " ".join(post.lower().split())
    n = 20
    if len(a) < 2 * n or not b:
        return False
    covered = bytearray(len(a))
    for i in range(len(a) - n + 1):
        if a[i:i + n] in b:
            covered[i:i + n] = b"\1" * n
    return sum(covered) > len(a) / 2


THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)   # «размышления» DeepSeek-R1, Qwen3 и т. п.


def strip_reasoning(text: str) -> str:
    """Убирает «размышления» модели из ответа. Незакрытый <think> — ответ обрезан, публиковать нельзя."""
    text = THINK_RE.sub("", text)
    low = text.lower()
    if "</think>" in low:   # открывающий тег бывает в шаблоне модели, а в ответе только закрывающий
        text = text[low.rindex("</think>") + len("</think>"):]
    elif "<think>" in low:
        raise RuntimeError("Модель не закончила размышлять — ответ обрезан. Увеличьте лимит длины ответа "
                           "и контекст модели или возьмите модель без режима размышлений")
    return text.strip()


