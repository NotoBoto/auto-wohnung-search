"""
Нейросети: бэкенды (Claude Code, Claude API, Antigravity, Gemini, Codex, OpenAI, Z.ai, своя модель)
и AIClient — единая точка, через которую программа просит нейросеть написать письмо.
"""
import asyncio
import base64
import json
import logging
import os
import random
import re
import shutil
import subprocess
import threading
from pathlib import Path

import core
from core import cli_env, system_proxy

MODELS = ["sonnet", "opus", "haiku"]          # для Claude Code
API_MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"]
API_PRICES = {  # $ за 1M токенов: вход / выход
    "claude-opus-5-5": "$4 / $20", "claude-sonnet-5-5": "$2 / $10", "claude-haiku-4-5": "$1 / $5"}
GEMINI_API_DEFAULT = "gemini-flash-latest"


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
AGY_AGENT = "wohnung"


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
# Лишние для писем возможности Codex: меньше служебного текста — меньше расход лимита
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
ZAI_KEY_RE = re.compile(r"^[0-9A-Za-z]{24,64}\.[0-9A-Za-z]{8,32}$")   # так выглядит ключ: «a1b2…f9.XyZ…»
ZAI_RETRY_DELAYS = (5, 15, 30)   # паузы перед повтором, когда Z.ai занят или сбоит
ZAI_CHECK_WAIT = 120             # сколько проверка ключа ждёт, пока аккаунты допишут свои запросы, сек
_zai_locks: dict[str, threading.Lock] = {}   # по ключу: запросы могут идти из разных потоков
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
    """Для письма думать незачем — без «размышлений» быстрее. Не принявшим параметр — не шлём."""
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
        return "Z.ai отказался отвечать: посчитал объявление или промпт недопустимым"
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
    из списка приёмов всегда берут один и тот же, а так письма получаются разными."""
    return VARIANTS_RE.sub(lambda m: random.choice(m.group(1).split("|")).strip(), prompt)


# Символы, которых нет на клавиатуре телефона, — сразу выдают нейросеть. Меняем на то, что напечатал бы человек.
HUMAN_TYPOGRAPHY = str.maketrans({"—": "-", "–": "-", "−": "-", "«": '"', "»": '"', "„": '"', "“": '"',
                                  "”": '"', "’": "'", "‘": "'", "…": "...", "×": "x",
                                  "\u00a0": " ", "\u202f": " ", "\u2009": " "})


def human_typography(text: str) -> str:
    """Длинные тире, ёлочки, многоточие одним знаком, × и неразрывные пробелы — в обычные символы;
    жирный markdown (**…**) в письме остался бы звёздочками — убираем."""
    return text.translate(HUMAN_TYPOGRAPHY).replace("**", "") if text else text


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


class AIError(RuntimeError):
    """Нейросеть недоступна или ответила ошибкой; текст понятен человеку и годится для бота."""


class AIClient:
    """Один клиент на программу: генерация идёт по одному запросу за раз."""

    def __init__(self, cfg: dict, log: logging.Logger):
        self.cfg = cfg
        self.log = log
        # Промпт передаём файлом: многострочный текст в аргументах ломается в cmd.exe
        self.system_prompt_file = core.BASE / "_system_prompt.txt"
        # Пустые рабочие папки: agy и Codex не видят файлы проекта
        self.agy_workspace = core.BASE / "_agy_workspace"
        self.codex_workspace = core.BASE / "_codex_workspace"
        self._lock = asyncio.Lock()   # в каждый момент к нейросети идёт только один запрос
        self._api_client = None
        self._api_client_key = None
        self._gemini_client = None
        self._gemini_client_key = None
        self._openai_client = None
        self._openai_client_key = None

    async def generate(self, system: str, user: str) -> str:
        """Текст от нейросети после human_typography и strip_reasoning. Сбой — AIError."""
        backend = self.cfg.get("backend")
        name = BACKEND_NAMES.get(backend, str(backend))
        gen = {"api": self._gen_api, "gemini_cli": self._gen_gemini_cli,
               "gemini_api": self._gen_gemini_api, "codex": self._gen_codex,
               "openai_api": self._gen_openai_api, "zai": self._gen_zai,
               "openai_compat": self._gen_compat}.get(backend, self._gen_claude_code)
        async with self._lock:
            try:
                return strip_reasoning(human_typography(await gen(system, user)))
            except AIError:
                raise
            except Exception as e:
                raise AIError(explain_ai_error(name, str(e))) from e

    async def _gen_claude_code(self, system: str, user_msg: str) -> str:
        cfg = self.cfg
        self.system_prompt_file.write_text(system, encoding="utf-8")
        args = [find_claude(), "-p",
                "--system-prompt-file", str(self.system_prompt_file),
                "--model", cfg.get("model", "sonnet"),
                "--output-format", "text"]
        args += ["--max-turns", "1"]

        # Чтобы Claude Code не ушёл на платный API вместо подписки
        env = cli_env(drop=("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"))
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            creationflags=NO_WINDOW,  # без чёрного окна
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(user_msg.encode("utf-8")),
                timeout=cfg.get("claude_timeout_sec", 120),
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("Claude Code не ответил вовремя")
        if proc.returncode != 0:
            msg = (decode_any(err) or decode_any(out)).strip()
            raise RuntimeError(msg or f"код выхода {proc.returncode}")
        return decode_any(out).strip()

    async def _gen_gemini_cli(self, system: str, user_msg: str) -> str:
        """Antigravity CLI: наш промпт кладём как отдельного агента в пустую рабочую папку."""
        cfg = self.cfg
        ws = self.agy_workspace
        shutil.rmtree(ws, ignore_errors=True)
        agent_dir = ws / ".agents" / "agents" / AGY_AGENT
        agent_dir.mkdir(parents=True)
        (agent_dir / "agent.md").write_text(
            f"---\nname: {AGY_AGENT}\ndescription: Пишет одно письмо арендодателю\n---\n"
            + system, encoding="utf-8")
        prompt = user_msg
        prompt += "\n\nВыведи только сам текст письма, без пояснений."

        timeout = cfg.get("claude_timeout_sec", 120)
        args = [find_agy(), "--agent", AGY_AGENT, "--output-format", "json",
                "--disable-slash-commands", "--print-timeout", f"{timeout}s"]
        if cfg.get("gemini_model"):
            args += ["--model", cfg["gemini_model"]]
        args += ["-p", prompt]
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=cli_env(), cwd=str(ws), creationflags=NO_WINDOW)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout + 15)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("Antigravity не ответил вовремя")
        finally:
            shutil.rmtree(ws, ignore_errors=True)
        text_out, text_err = decode_any(out), decode_any(err)
        if "sign in" in (text_out + text_err).lower() or "authentication required" in (text_out + text_err).lower():
            raise RuntimeError("Не выполнен вход в Google для Antigravity — «Настройки» → «Войти в Google»")
        try:
            data = json.loads(text_out[text_out.index("{"):])
        except ValueError:
            raise RuntimeError((text_err or text_out).strip()[-500:] or f"код выхода {proc.returncode}")
        if data.get("status") != "SUCCESS":
            raise RuntimeError(f"Antigravity: {data.get('error') or data.get('status')}")
        return (data.get("response") or "").strip()

    async def _gen_gemini_api(self, system: str, user_msg: str) -> str:
        from google import genai
        from google.genai import errors as gerr, types
        cfg = self.cfg
        key = gemini_key(cfg)
        if self._gemini_client is None or self._gemini_client_key != key:
            self._gemini_client = genai.Client(api_key=key)
            self._gemini_client_key = key
        model = cfg.get("gemini_api_model") or GEMINI_API_DEFAULT
        try:
            resp = await asyncio.wait_for(
                self._gemini_client.aio.models.generate_content(
                    model=model, contents=user_msg,
                    config=types.GenerateContentConfig(
                        system_instruction=system, max_output_tokens=8192,
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))),
                timeout=cfg.get("claude_timeout_sec", 120))
        except asyncio.TimeoutError:
            raise RuntimeError("Gemini API не ответил вовремя")
        except gerr.APIError as e:
            if e.code in (401, 403) or "API key" in str(e.message):
                raise RuntimeError("Ключ Gemini не подходит — проверьте его в «Настройках»")
            if e.code == 429:
                raise RuntimeError("Исчерпан лимит запросов Gemini API — подождите или проверьте тариф")
            if e.code == 404:
                raise RuntimeError(f"Модель {model} не найдена — выберите другую в «Настройках»")
            raise RuntimeError(f"Ошибка Gemini API ({e.code}): {e.message}")
        except Exception as e:
            raise RuntimeError(f"Нет связи с Gemini API: {e}")
        u = resp.usage_metadata
        if u:
            self.log.info("Gemini %s: вход %s ток., выход %s ток.", model,
                     u.prompt_token_count, (u.candidates_token_count or 0) + (u.thoughts_token_count or 0))
        if not resp.text:
            self.log.info("Gemini не вернул текст (сработал фильтр безопасности или пустой ответ)")
            return ""
        return resp.text.strip()

    async def _gen_codex(self, system: str, user_msg: str) -> str:
        """Codex CLI по подписке ChatGPT: наш промпт заменяет встроенные инструкции Codex."""
        cfg = self.cfg
        ws = self.codex_workspace
        shutil.rmtree(ws, ignore_errors=True)
        ws.mkdir(parents=True)
        instr, last = ws / "instructions.md", ws / "last_message.txt"
        instr.write_text(system, encoding="utf-8")
        args = [find_codex(), "exec", "--skip-git-repo-check", "--ephemeral",
                "--sandbox", "read-only", "--color", "never", "-C", str(ws),
                "-c", f"model_instructions_file='{instr}'",
                "-c", "project_doc_max_bytes=0",          # не читать AGENTS.md
                "-c", "skills.include_instructions=false",
                "-c", 'web_search="disabled"',
                "-c", 'forced_login_method="chatgpt"',    # только подписка, не API-ключ
                "-o", str(last)]
        for feature in CODEX_FEATURES_OFF:
            args += ["--disable", feature]
        if cfg.get("codex_model"):
            args += ["-m", cfg["codex_model"]]
        user_msg += "\n\nВыведи только сам текст письма, без пояснений."
        env = cli_env(drop=("OPENAI_API_KEY", "CODEX_API_KEY"))
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, cwd=str(ws), creationflags=NO_WINDOW)
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(user_msg.encode("utf-8")),
                timeout=cfg.get("claude_timeout_sec", 120))
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("Codex не ответил вовремя")
        try:
            text = last.read_text(encoding="utf-8").strip() if last.exists() else ""
        finally:
            shutil.rmtree(ws, ignore_errors=True)
        if proc.returncode != 0 or not text:
            msg = (decode_any(err) + decode_any(out)).strip()
            low = msg.lower()
            if "not logged in" in low or "401 unauthorized" in low:
                raise RuntimeError("Не выполнен вход в ChatGPT — «Настройки» → «Войти в ChatGPT»")
            if "usage limit" in low or "rate limit" in low:
                raise RuntimeError("Исчерпан лимит подписки ChatGPT для Codex — подождите сброса")
            lines = [l for l in msg.splitlines() if "error" in l.lower()]
            raise RuntimeError("Codex: " + (lines[-1] if lines else msg[-400:] or f"код выхода {proc.returncode}"))
        return text

    async def _gen_compat(self, system: str, user_msg: str) -> str:
        """Своя модель на OpenAI-совместимом сервере (LM Studio, Ollama…) через chat/completions."""
        import openai
        cfg = self.cfg
        url = str(cfg.get("compat_base_url", "")).strip().rstrip("/")
        sig = (url, cfg.get("compat_api_key", ""))
        if getattr(self, "_compat_sig", None) != sig:
            self._compat_client, self._compat_sig = compat_client(cfg, asynchronous=True), sig
        model = str(cfg.get("compat_model", "")).strip()
        try:
            resp = await self._compat_client.with_options(
                timeout=float(cfg.get("claude_timeout_sec", 120))).chat.completions.create(
                model=model, messages=[{"role": "system", "content": system},
                                       {"role": "user", "content": user_msg}])
        except openai.APIConnectionError:
            raise RuntimeError(compat_connection_hint(url))
        except openai.APITimeoutError:
            raise RuntimeError("Модель не ответила вовремя — увеличьте «Таймаут ответа» или возьмите модель поменьше")
        except openai.AuthenticationError:
            raise RuntimeError("Сервер требует API-ключ (или ключ не подходит)")
        except openai.NotFoundError:
            raise RuntimeError(f"Модель {model} не найдена или не загружена — «Проверить подключение» в «Настройках»")
        except openai.APIStatusError as e:
            low = str(e.message).lower()
            if "context" in low and any(w in low for w in ("length", "window", "overflow", "exceed", "too long")):
                raise RuntimeError("Промпт не помещается в контекст модели — увеличьте длину контекста "
                                   "(LM Studio: настройки модели → Context Length, нужно 16–32 тыс. токенов и больше) "
                                   "или сократите промпт")
            raise RuntimeError(f"Ошибка сервера ({e.status_code}): {e.message}")
        u = resp.usage
        if u:
            self.log.info("Своя модель %s: вход %s ток., выход %s ток.", model, u.prompt_tokens, u.completion_tokens)
        text = (resp.choices[0].message.content or "") if resp.choices else ""
        return strip_reasoning(text)

    async def _gen_zai(self, system: str, user_msg: str) -> str:
        """Z.ai: бесплатные GLM. Запросы всех профилей с одним ключом — по очереди, занятость — пауза и повтор."""
        import openai
        cfg = self.cfg
        key = zai_key(cfg)
        if not key:
            raise RuntimeError("Не указан ключ Z.ai — «Настройки» → «Нейросеть»")
        if getattr(self, "_zai_client_key", None) != key:
            self._zai_client, self._zai_client_key = zai_client(key, asynchronous=True), key
        # Попытки по порядку: выбранная модель и запасные бесплатные
        attempts = [(m, user_msg) for m in zai_models_order(str(cfg.get("zai_model", "")).strip() or ZAI_MODELS[0])]
        lock = zai_lock(key)
        if not lock.acquire(blocking=False):
            self.log.info("Z.ai: жду, пока закончится другой запрос (бесплатно — по одному за раз)")
            while not lock.acquire(blocking=False):   # не to_thread: при отмене задачи замок не повиснет
                await asyncio.sleep(0.3)
        try:
            (model, content), resp, delays = attempts.pop(0), None, list(ZAI_RETRY_DELAYS)
            while resp is None:
                try:
                    resp = await self._zai_client.with_options(
                        timeout=float(cfg.get("claude_timeout_sec", 120))).chat.completions.create(
                        model=model, messages=[{"role": "system", "content": system},
                                               {"role": "user", "content": content}],
                        extra_body=zai_extra(model))
                    continue
                except openai.AuthenticationError:
                    raise RuntimeError(ZAI_BAD_KEY)
                except openai.APITimeoutError:   # до APIConnectionError: это её подкласс
                    raise RuntimeError("Z.ai не ответил вовремя — увеличьте «Таймаут ответа» в «Настройках»")
                except openai.APIConnectionError:
                    why = "нет связи"
                except openai.APIStatusError as e:
                    code, msg = zai_error(e)
                    if code == "1211" and attempts:   # модель отключили — берём следующую
                        _zai_gone.add(model)
                        nxt, content = attempts.pop(0)
                        self.log.warning("Z.ai больше не знает модель %s — перехожу на %s. Выберите её "
                                         "в «Настройках», чтобы не видеть это сообщение", model, nxt)
                        model = nxt
                        continue
                    if zai_thinking_rejected(model, e.status_code, msg):
                        continue
                    if not zai_busy(e.status_code, code):
                        raise RuntimeError(zai_error_text(e.status_code, code, msg, model))
                    why = msg
                if not delays:
                    raise RuntimeError(f"Z.ai перегружен или недоступен ({why}) — попробуйте позже")
                d = delays.pop(0)
                self.log.info("Z.ai занят (%s) — повторю через %s с", why, d)
                await asyncio.sleep(d)
        finally:
            lock.release()
        u = resp.usage
        if u:
            self.log.info("Z.ai %s: вход %s ток., выход %s ток.", model, u.prompt_tokens, u.completion_tokens)
        text = (resp.choices[0].message.content or "") if resp.choices else ""
        return strip_reasoning(text)

    async def _gen_openai_api(self, system: str, user_msg: str) -> str:
        import openai
        cfg = self.cfg
        key = openai_key(cfg)
        if self._openai_client is None or self._openai_client_key != key:
            self._openai_client = openai.AsyncOpenAI(api_key=key)
            self._openai_client_key = key
        content = [{"type": "input_text", "text": user_msg}]
        model = cfg.get("openai_api_model") or OPENAI_API_DEFAULT
        try:
            resp = await self._openai_client.with_options(
                timeout=float(cfg.get("claude_timeout_sec", 120))).responses.create(
                model=model, instructions=system, input=[{"role": "user", "content": content}])
        except openai.AuthenticationError:
            raise RuntimeError("Ключ OpenAI не подходит — проверьте его в «Настройках»")
        except openai.RateLimitError as e:
            raise RuntimeError(f"OpenAI: лимит или нет средств на балансе — {e.message}")
        except openai.NotFoundError:
            raise RuntimeError(f"Модель {model} недоступна — выберите другую в «Настройках»")
        except openai.APITimeoutError:
            raise RuntimeError("OpenAI API не ответил вовремя")
        except openai.APIConnectionError:
            raise RuntimeError("Нет связи с OpenAI API — проверьте интернет")
        except openai.APIStatusError as e:
            raise RuntimeError(f"Ошибка OpenAI API ({e.status_code}): {e.message}")
        u = resp.usage
        if u:
            cached = getattr(u.input_tokens_details, "cached_tokens", 0) if u.input_tokens_details else 0
            self.log.info("OpenAI %s: вход %s ток. (из кэша %s), выход %s ток.", model,
                     u.input_tokens, cached, u.output_tokens)
        return (resp.output_text or "").strip()

    async def _gen_api(self, system: str, user_msg: str) -> str:
        import anthropic
        cfg = self.cfg
        key = api_key(cfg)
        if self._api_client is None or self._api_client_key != key:
            self._api_client = anthropic.AsyncAnthropic(api_key=key)
            self._api_client_key = key
        content = [{"type": "text", "text": user_msg}]
        model = cfg.get("api_model", API_MODELS[0])
        kw = dict(
            model=model, max_tokens=16000,
            # Промпт большой и одинаковый — кэшируем, повторные запросы в разы дешевле
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
        )
        client = self._api_client.with_options(timeout=float(cfg.get("claude_timeout_sec", 120)))
        try:
            if model.startswith("claude-haiku"):
                resp = await client.messages.create(**kw)
            else:
                kw["output_config"] = {"effort": "medium"}
                try:
                    # Если фильтр модели откажет, API сам повторит запрос на подходящей модели
                    resp = await client.beta.messages.create(
                        **kw, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
                except anthropic.BadRequestError as e:
                    if "fallback" not in str(e.message).lower():
                        raise
                    resp = await client.messages.create(**kw)
        except anthropic.AuthenticationError:
            raise RuntimeError("API-ключ не подходит — проверьте его в «Настройках»")
        except anthropic.PermissionDeniedError as e:
            raise RuntimeError(f"Нет доступа к модели {model}: {e.message}")
        except anthropic.RateLimitError:
            raise RuntimeError("Слишком много запросов к API — попробуйте чуть позже")
        except anthropic.BadRequestError as e:
            raise RuntimeError(f"API отклонил запрос: {e.message}")
        except anthropic.APITimeoutError:
            raise RuntimeError("API не ответил вовремя")
        except anthropic.APIConnectionError:
            raise RuntimeError("Нет связи с API Anthropic — проверьте интернет")
        except anthropic.APIStatusError as e:
            raise RuntimeError(f"Ошибка API ({e.status_code}): {e.message}")

        u = resp.usage
        self.log.info("API %s: вход %s ток. (из кэша %s), выход %s ток.", resp.model,
                 u.input_tokens + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0),
                 u.cache_read_input_tokens or 0, u.output_tokens)
        if resp.stop_reason == "refusal":
            self.log.info("Модель отказалась отвечать на этот запрос")
            return ""
        return "".join(b.text for b in resp.content if b.type == "text").strip()
