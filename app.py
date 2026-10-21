"""
Окно Auto Wohnung Search. Запуск: start.bat или python app.py
Здесь пользователь входит на площадки, вводит токен бота, Gmail и настройки нейросети, запускает и
останавливает работу и видит статус, журнал и отчёт. Всё остальное делается через Telegram-бота.

Движок, бот и браузер живут в одном цикле asyncio в фоновом потоке (self.loop). Окно обращается к нему
через run_coroutine_threadsafe (App.submit); tkinter из чужих потоков не трогаем — результат
возвращается в окно через call_soon.
"""
import asyncio
import ctypes
import json
import logging
import logging.handlers
import os
import queue
import socket
import sys
import threading
import time
import webbrowser
import zlib
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, ttk
from tkinter import messagebox as tk_messagebox

import customtkinter as ctk
from PIL import Image

import ai
import core
import report
import updater
from ai import AIClient
from bot import _scrub, start_bot, stop_bot
from browser import Browser
from core import BASE
from mail import MailWatcher
from models import profile_complete
from pipeline import Engine, to_local, to_utc
from sources.kleinanzeigen import Kleinanzeigen
from sources.wggesucht import WgGesucht
from store import Store

GREEN, GREEN_HOVER = "#2e9e5b", "#257f49"
GRAY, GRAY_HOVER = ("#9aa0a6", "#4a4d52"), ("#80868b", "#5f6368")
RED = "#d9534f"
AMBER = "#e0a030"
STATUS = {
    "stopped": ("Остановлен", "#9aa0a6"),
    "starting": ("Запускается…", "#2b7bd0"),
    "running": ("Работает", "#2e9e5b"),
    "problem": ("Требует внимания", "#e0a030"),
}
BTN_COLOR = ctk.ThemeManager.theme["CTkButton"]["fg_color"]
BTN_HOVER = ctk.ThemeManager.theme["CTkButton"]["hover_color"]
LABEL_W = 270  # ширина колонки подписей в блоке «Нейросеть»
BACKENDS = {"Z.ai (бесплатно)": "zai", "Claude Code": "claude_code", "Claude API": "api",
            "Antigravity (Google)": "gemini_cli", "Gemini API": "gemini_api",
            "ChatGPT (Codex)": "codex", "OpenAI API": "openai_api", "Своя модель": "openai_compat"}
DEFAULT_MODEL = "(по умолчанию)"
# Площадки в окне: (ключ в cfg, название, класс источника или None — «скоро»; Engine такие пропускает)
PLATFORM_ROWS = (("kleinanzeigen", "Kleinanzeigen", Kleinanzeigen), ("wggesucht", "WG-Gesucht", WgGesucht),
                 ("immowelt", "Immowelt", None), ("immoscout", "ImmoScout24", None))
PLATFORM_NAMES = {k: n for k, n, _ in PLATFORM_ROWS}
MAIL_HINT = ("Вход выполнен. Один раз включите в настройках аккаунта {name} e-mail-уведомления о новых "
             "сообщениях на адрес Gmail из этой программы — иначе ответы арендодателей не дойдут до бота.")
STOP_WAIT = 40   # сколько ждём, пока циклы движка закончат текущую работу при остановке, сек
KIND_TEXT = {"logged_out": "вход нужен", "captcha": "капча", "blocked": "блокировка", "broken": "сбой разметки",
             "send": "сбой отправки", "ai": "сбой нейросети"}

log = logging.getLogger("wohnung")


class QueueHandler(logging.Handler):
    def __init__(self, q):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put(record)


# модель каждой нейросети в cfg — для строки настроек в журнале при запуске
MODEL_KEYS = {"zai": "zai_model", "claude_code": "model", "api": "api_model", "gemini_cli": "gemini_model",
              "gemini_api": "gemini_api_model", "codex": "codex_model", "openai_api": "openai_api_model",
              "openai_compat": "compat_model"}


def setup_logging() -> queue.Queue:
    """log.txt — всё, включая DEBUG (каждое объявление и его судьба), 5 МБ × 3 файла;
    вкладка «Журнал» — только INFO и выше."""
    q = queue.Queue()
    fmt = logging.Formatter("%(asctime)s  %(levelname)s  %(message)s")
    file_h = logging.handlers.RotatingFileHandler(BASE / "log.txt", maxBytes=5_000_000, backupCount=3,
                                                  encoding="utf-8")
    file_h.setFormatter(fmt)
    gui_h = QueueHandler(q)
    gui_h.setLevel(logging.INFO)
    log.setLevel(logging.DEBUG)
    log.addHandler(file_h)
    log.addHandler(gui_h)
    return q



def log_settings(c: dict, mail_on: bool, names) -> None:
    """Настройки запуска одной строкой на тему — по журналу видно, с чем работала программа (без ключей)."""
    b = c.get("backend")
    log.info("Версия %s; нейросеть %s, модель %s; режим %s; почта %s; профиль %s", core.VERSION, b,
             c.get(MODEL_KEYS.get(b, ""), "") or DEFAULT_MODEL, c["mode"], "да" if mail_on else "нет",
             "заполнен" if profile_complete(c["profile"]) else "НЕ заполнен")
    log.info("Фильтры: %s", ", ".join(f"{k}={v}" for k, v in c["filters"].items()))
    for name in names:   # площадки этого запуска
        pc = c["platforms"][name]
        log.info("%s: режим %s, лимит %s/день, пауза между письмами %s с, опрос %s с", name,
                 pc.get("mode") or c["mode"], pc["daily_cap"], pc["min_gap_sec"], pc["poll"])


def play_sound():
    """Свой тихий деревянный «тук-тук» (assets/notify.wav) вместо системного звука Windows."""
    try:
        import winsound
        winsound.PlaySound(str(core.RES / "assets" / "notify.wav"),
                           winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception as e:   # нет звуковой карты, файла и т. п. — молча без звука
        log.debug("Звук не проигран: %s", e)


class Messages:
    """Замена tkinter.messagebox: системные окна Windows всплывают поверх всего и звучат.
    Сообщения — тихая полоса над вкладками, вопросы — тихое окно в стиле программы."""
    app: "App | None" = None   # окно программы регистрирует себя при создании

    def _say(self, kind, title, text):
        if self.app:
            self.app.say(kind, title, text)

    def showinfo(self, title, text):
        self._say("info", title, text)

    def showwarning(self, title, text):
        self._say("warning", title, text)

    def showerror(self, title, text):
        self._say("error", title, text)

    def askyesno(self, title, text) -> bool:
        return bool(self.app) and self.app.ask(title, text, "Да", "Нет")

    def askokcancel(self, title, text) -> bool:
        return bool(self.app) and self.app.ask(title, text, "ОК", "Отмена")


messagebox = Messages()   # вызывается так же, как tkinter.messagebox


def open_path(p: Path):
    os.startfile(str(p))  # Windows


HOTKEY_ACTIONS = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>", 65: "<<SelectAll>>"}   # V, C, X, A


def hotkey_any_layout(e):
    """Ctrl+V/C/X/A в любой раскладке. tkinter узнаёт сочетание по букве (keysym), а в русской раскладке
    на той же клавише «м», «с», «ч», «ф» — и вставка молча не срабатывает. Смотрим на физическую
    клавишу (keycode — код клавиши Windows) и вызываем нужное действие сами."""
    action = HOTKEY_ACTIONS.get(e.keycode)
    if action and e.keysym.lower() not in ("v", "c", "x", "a"):   # латиница — tkinter справится сам
        e.widget.event_generate(action)
        return "break"


def fix_hotkeys_any_layout(root):
    root.bind_all("<Control-KeyPress>", hotkey_any_layout, add="+")


def repaint_after_scroll(sf: ctk.CTkScrollableFrame):
    """customtkinter на Windows при прокрутке иногда оставляет «огрызки» виджетов (зависит от масштаба
    экрана и видеокарты): виджеты переезжают, а освободившееся место не перерисовывается.
    Через миг после любого сдвига (колесо, ползунок, клавиши) просим Windows перерисовать всю область."""
    if sys.platform != "win32":
        return
    canvas = sf._parent_canvas
    pending = {"id": None}
    flags = 0x0001 | 0x0004 | 0x0080 | 0x0100   # RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW

    def redraw():
        pending["id"] = None
        ctypes.windll.user32.RedrawWindow(canvas.winfo_id(), None, None, flags)

    def on_scroll(*args):
        sf._scrollbar.set(*args)   # как было у customtkinter
        if pending["id"]:
            canvas.after_cancel(pending["id"])
        pending["id"] = canvas.after(40, redraw)
    canvas.configure(yscrollcommand=on_scroll)


# Две копии программы мешали бы друг другу (двойные письма арендодателям).
# Первая копия слушает локальный порт; вторая просит её показать окно и выходит.
INSTANCE_HELLO, INSTANCE_REPLY = b"wohnung:show", b"wohnung:ok"
INSTANCE_PORT = 20000 + zlib.crc32(str(BASE).encode()) % 20000   # своя у каждой папки с программой


def single_instance() -> socket.socket | None | bool:
    """Сокет первой копии; False — программа уже запущена (окно ей показано); None — проверка невозможна."""
    try:
        with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=1) as c:
            c.sendall(INSTANCE_HELLO)
            if c.recv(64) == INSTANCE_REPLY:
                return False
    except OSError:
        pass
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", INSTANCE_PORT))
        srv.listen()
        return srv
    except OSError:   # порт занят чем-то другим — просто работаем без проверки
        srv.close()
        return None


# ======================================================================
#  Значок в трее
# ======================================================================

APP_COLOR = "#2b7bd0"   # цвет значка программы (assets/icon.ico рисуется из tray_image)


def tray_image(color: str, size: int = 64) -> Image.Image:
    """Облачко комментария на круге цвета статуса."""
    from PIL import ImageDraw
    k = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, size - 1, size - 1), fill=color)
    d.rounded_rectangle((14 * k, 17 * k, 50 * k, 41 * k), radius=7 * k, fill="white")
    d.polygon([(20 * k, 40 * k), (20 * k, 50 * k), (30 * k, 40 * k)], fill="white")
    return img


class Tray:
    """Значок в трее: окно можно закрыть, а программа продолжит работать.
    Меню pystray работает в своём потоке — действия передаём в окно через app.call_soon."""

    def __init__(self, app: "App"):
        import pystray
        self.app = app
        item = pystray.MenuItem
        self.icon = pystray.Icon("auto_wohnung_search", tray_image(STATUS["stopped"][1]), "Auto Wohnung Search",
                                 pystray.Menu(
                                     item("Открыть", lambda: app.call_soon(app.show_window), default=True),
                                     item(lambda i: "■  Остановить" if app.status != "stopped" else "▶  Запустить",
                                          lambda: app.call_soon(app.toggle_run)),
                                     pystray.Menu.SEPARATOR,
                                     item("Выход", lambda: app.call_soon(app.quit_app))))
        self.color = STATUS["stopped"][1]
        self.icon.run_detached()

    def update(self, status: str, tip: str):
        color = STATUS[status][1]
        if color != self.color:
            self.icon.icon, self.color = tray_image(color), color
        self.icon.title = tip[:127]   # у Windows ограничение длины подсказки
        self.icon.update_menu()

    def notify(self, text: str):
        if not self.app.conf.get("tray_notify"):   # у уведомлений Windows звук — по умолчанию выключены
            return
        try:
            self.icon.notify(text, "Auto Wohnung Search")
        except Exception:
            pass

    def stop(self):
        self.icon.stop()


# ======================================================================
#  Блоки настроек нейросети (из tg-autocomment)
# ======================================================================

def ai_ready(cfg: dict) -> bool:
    """Нейросеть настроена настолько, чтобы запускаться: есть ключ, адрес или установленная программа
    (вошли ли в аккаунт CLI — проверяет сам блок настроек, здесь это не ждём)."""
    return bool({"zai": ai.zai_key(cfg), "api": ai.api_key(cfg), "gemini_api": ai.gemini_key(cfg),
                 "openai_api": ai.openai_key(cfg), "openai_compat": str(cfg.get("compat_base_url", "")).strip(),
                 "claude_code": ai.find_claude(), "gemini_cli": ai.find_agy(), "codex": ai.find_codex()
                 }.get(cfg.get("backend")))


def describe_claude(st):
    if not st.get("loggedIn"):
        return None
    plan = st.get("subscriptionType") or ""
    text = f"Вход в Claude выполнен ✓  {st.get('email', '')}" + (f"  ·  подписка {plan}" if plan else "")
    if st.get("authMethod") not in (None, "claude.ai"):
        return text + "  ·  ⚠ вход не по подписке — запросы могут оплачиваться по API", "#e0a030"
    return text, GREEN


class CliBlock:
    """Строка «установлена ли программа / выполнен ли вход» + кнопка, которая делает недостающее."""

    def __init__(self, app: "App", parent, row, name, account, status, install, login, describe, hint,
                 relogin_hint=None):
        self.app, self.name, self.account = app, name, account
        self.status_fn, self.install_fn, self.login_fn = status, install, login
        self.describe, self.hint, self.relogin_hint = describe, hint, relogin_hint
        self.waiting = 0
        self.seen_logout = False
        self.state = {"installed": False}
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.grid(row=row, column=0, columnspan=3, sticky="ew", padx=14, pady=(4, 4))
        self.lbl = ctk.CTkLabel(f, text=f"{name}: проверяю…", text_color="gray")
        self.lbl.pack(side="left")
        # Почта аккаунта скрыта, пока не нажать «показать»
        self.reveal_email = False
        self.email_btn = ctk.CTkButton(f, text="показать", width=70, height=24, fg_color="transparent",
                                       text_color=("#1f6aa5", "#5aa9e6"), hover=False,
                                       command=self.toggle_email)
        self.btn = ctk.CTkButton(f, text="", width=200, command=self.action)
        self.btn_visible = False
        app.after(200, self.refresh)

    def toggle_email(self):
        self.reveal_email = not self.reveal_email
        self.show(self.state)

    def refresh(self):
        self.app.in_thread(self.status_fn, self.show)

    def show(self, st):
        self.state = st
        if not st["installed"]:
            text, color, btn = f"{self.name} не установлен", RED, "Установить и войти"
        elif not st.get("loggedIn"):
            text, color, btn = f"{self.name} установлен, но вход не выполнен", "#e0a030", f"Войти в {self.account}"
        else:
            (text, color), btn = self.describe(st), "Войти другим аккаунтом"
        email = st.get("email") if st.get("loggedIn") else ""
        if email and not self.reveal_email:
            text = text.replace(email, "••••••••@••••")
        self.lbl.configure(text=text, text_color=color)
        if email:
            self.email_btn.configure(text="скрыть" if self.reveal_email else "показать")
            self.email_btn.pack(side="left", padx=(4, 0), after=self.lbl)
        else:
            self.email_btn.pack_forget()
        logged = st.get("loggedIn")
        self.btn.configure(text=btn, state="normal",
                           fg_color=GRAY if logged else BTN_COLOR,
                           hover_color=GRAY_HOVER if logged else BTN_HOVER)
        if not self.btn_visible:
            self.btn.pack(side="right")
            self.btn_visible = True

    def action(self):
        self.btn.configure(state="disabled")
        if not self.state["installed"]:
            self.lbl.configure(text=f"Скачиваю и устанавливаю {self.name}… (1–5 минут)", text_color="gray")
            log.info("Устанавливаю %s…", self.name)
            self.app.in_thread(self.install_fn, self.after_install)
        else:
            self.start_login()

    def after_install(self, res):
        ok, out = res
        if not ok:
            log.error("Установка %s не удалась:\n%s", self.name, out[-1500:])
            messagebox.showerror(self.name, f"Не получилось установить {self.name}.\n"
                                            "Подробности во вкладке «Журнал».")
            self.refresh()
            return
        log.info("%s установлен", self.name)
        self.start_login()

    def vpn_ok(self) -> bool:
        """Вход идёт из окна самой CLI — подсказку про VPN нужно дать до него, потом будет поздно."""
        sp = core.system_proxy()
        if sp and sp.startswith("http"):
            return True
        where = (f"Системный прокси {sp} — SOCKS, а {self.name} понимает только HTTP-прокси."
                 if sp else "Системный прокси не найден.")
        return messagebox.askokcancel(f"Вход в {self.account}", (
            f"{where}\n\n{self.account} недоступен в некоторых странах (например, в России), и вход оттуда "
            "без VPN не пройдёт — Google, OpenAI и Anthropic проверяют страну.\n\n"
            "Если вы в такой стране — нажмите «Отмена», включите VPN с сервером в США или Европе "
            "(режим TUN или системный HTTP-прокси) и повторите вход.\n\n"
            "Если VPN в режиме TUN уже включён или ваша страна поддерживается — нажмите «ОК».\n\n"
            "Нет VPN — выберите в «Нейросети» способ «Z.ai (бесплатно)»: он работает в России."))

    def start_login(self):
        relogin = bool(self.state.get("loggedIn"))
        if not self.vpn_ok():
            self.refresh()
            return
        try:
            self.login_fn()
        except Exception as e:
            messagebox.showerror(self.name, f"Не удалось открыть вход: {e}")
            self.refresh()
            return
        messagebox.showinfo(f"Вход в {self.account}",
                            self.relogin_hint if relogin and self.relogin_hint else self.hint)
        self.lbl.configure(text="Жду завершения входа… Когда войдёте — нажмите «Готово — проверить»",
                           text_color="gray")
        self.waiting += 1            # номер текущего ожидания: старые циклы проверки сами завершатся
        self.seen_logout = not relogin
        self.btn.configure(text="Готово — проверить", state="normal", command=self.finish_wait,
                           fg_color=BTN_COLOR, hover_color=BTN_HOVER)
        self.wait_login(self.waiting, tries=100)   # ~5 минут

    def finish_wait(self):
        self.waiting += 1            # останавливаем фоновое ожидание
        self.btn.configure(state="disabled", command=self.action)
        self.lbl.configure(text="Проверяю…", text_color="gray")

        def done(st):
            self.show(st)
            if st.get("loggedIn"):
                log.info("Вход в %s выполнен", self.account)
        self.app.in_thread(self.status_fn, done)

    def wait_login(self, wait_id, tries):
        def done(st):
            if wait_id != self.waiting:
                return               # ожидание уже закончено кнопкой или новым входом
            if not st.get("loggedIn"):
                self.seen_logout = True
            elif self.seen_logout or st != self.state:
                self.waiting += 1
                self.btn.configure(command=self.action)
                self.show(st)
                log.info("Вход в %s выполнен", self.account)
                return
            if tries > 0:
                self.app.after(3000, lambda: self.wait_login(wait_id, tries - 1))
            else:
                self.waiting += 1
                self.btn.configure(command=self.action)
                self.show(st)
        self.app.in_thread(self.status_fn, done)


class KeyBlock:
    """Поле API-ключа + «показать» + «Проверить ключ» + ссылка, где взять ключ."""

    def __init__(self, app: "App", parent, key_field, placeholder, link_text, url,
                 model_widget, model_field, check_fn, autoselect=False):
        """autoselect — после проверки выбрать первую модель из списка, если выбранной там нет."""
        self.app, self.key_field, self.model_field = app, key_field, model_field
        self.model_widget, self.check_fn, self.autoselect = model_widget, check_fn, autoselect
        ctk.CTkLabel(parent, text="API-ключ").grid(row=1, column=0, sticky="w", padx=14, pady=5)
        self.entry = e = ctk.CTkEntry(parent, show="•", placeholder_text=placeholder)
        e.grid(row=1, column=1, sticky="ew", padx=4)
        app.fields[key_field] = e
        kb = ctk.CTkFrame(parent, fg_color="transparent")
        kb.grid(row=1, column=2, sticky="w", padx=6)
        ctk.CTkButton(kb, text="показать", width=80, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=lambda: e.configure(show="" if e.cget("show") else "•")
                      ).pack(side="left", padx=2)
        self.btn = ctk.CTkButton(kb, text="Проверить ключ", width=130, command=self.check)
        self.btn.pack(side="left", padx=2)
        ctk.CTkButton(parent, text=link_text, fg_color="transparent",
                      text_color=("#1f6aa5", "#5aa9e6"), hover=False, anchor="w",
                      command=lambda: webbrowser.open(url)).grid(row=2, column=1, sticky="w")
        self.lbl = ctk.CTkLabel(parent, text="", text_color="gray")
        self.lbl.grid(row=2, column=2, sticky="w", padx=10)

    def check(self):
        cfg = dict(self.app.conf, **{self.key_field: self.entry.get().strip(),
                                    self.model_field: self.model_widget.get().strip()})
        self.btn.configure(state="disabled")
        self.lbl.configure(text="Проверяю…", text_color="gray")

        def done(res):
            ok, msg, *models = res
            self.btn.configure(state="normal")
            self.lbl.configure(text=msg, text_color=GREEN if ok else RED)
            if models and models[0]:   # проверка вернула список доступных моделей
                self.model_widget.configure(values=models[0])
                if self.autoselect and self.model_widget.get().strip() not in models[0]:
                    self.model_widget.set(models[0][0])
        self.app.in_thread(lambda: self.check_fn(cfg), done)


# ======================================================================
#  Главное окно


# ======================================================================
#  Главное окно
# ======================================================================

class App(ctk.CTk):
    UPDATE_EVERY_MS = 6 * 3600 * 1000
    NOTICE_COLORS = {"info": ("#e3f0ff", "#1d2f44"), "warning": ("#fff4d6", "#463a17"),
                     "error": ("#fde3e1", "#4a1f1c")}

    def __init__(self, hidden=False, updated=False, instance: socket.socket | None = None,
                 auto_run: bool = False):
        """hidden — сразу в трей (автозапуск с Windows); updated — запуск после обновления;
        auto_run — начать работу через core.AUTO_RUN_DELAY секунд (автозапуск)."""
        super().__init__()
        if hidden:
            self.withdraw()   # до первой отрисовки — окно не мелькнёт
        icon = core.RES / "assets" / "icon.ico"
        if icon.exists():
            self.iconbitmap(str(icon))
        self.log_q = setup_logging()
        self.ui_q: queue.Queue = queue.Queue()   # задания для окна из фоновых потоков (call_soon)
        # Не self.config: это имя метода tkinter
        self.conf = core.load_config()
        self.conf.setdefault("mail_state", {})   # состояние чтения Gmail (последнее письмо); обновляет MailWatcher
        self._saved_mail = json.dumps(self.conf["mail_state"], sort_keys=True)
        if core.config_backup:   # не затираем сразу: пользователь может восстановить настройки из копии
            log.warning("Файл настроек был повреждён — взяты значения по умолчанию (токен бота, ключи, Gmail и "
                        "профиль пусты). Копия старого файла: %s. Восстановите настройки из неё или введите заново.",
                        core.config_backup)
            if not self.conf.get("owner_chat_id"):
                log.warning("Владелец бота не восстановлен: бот запомнит владельцем первого, кто напишет ему /start. "
                            "Напишите боту /start сами сразу после запуска.")
        else:
            self.save()
        self.status = "stopped"   # нужен сразу — меню трея читает его из своего потока
        self.busy = False         # идёт запуск или остановка — кнопка «Запустить» занята
        # Движок, бот и браузер — в одном цикле asyncio в своём потоке. Браузер и хранилище живут весь сеанс:
        # вход на площадку нужен и при остановленной работе; Engine и бот создаются при каждом запуске.
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="asyncio").start()
        self.store = Store(BASE / "wohnung.db")
        self.browser = Browser(BASE)
        self.engine: Engine | None = None
        self.bot_app = None
        self.run_task: asyncio.Task | None = None
        self.last_poll: dict[str, float] = {}   # когда площадка опрашивалась в последний раз (time.time())
        self.seen_incidents: set[tuple[str, str]] = set()
        self.login_btns: dict[str, ctk.CTkButton] = {}
        self.snap: dict = {}   # последний снимок состояния площадок

        self.title("Auto Wohnung Search")
        self.geometry("1000x700")
        self.minsize(820, 560)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_header()
        self.notice = ctk.CTkFrame(self)
        self.notice_lbl = ctk.CTkLabel(self.notice, text="", anchor="w", justify="left", wraplength=900)
        self.notice_lbl.pack(side="left", fill="x", expand=True, padx=12, pady=6)
        ctk.CTkButton(self.notice, text="✕", width=32, fg_color="transparent", hover_color=GRAY_HOVER,
                      text_color=("gray20", "gray80"), command=self.hide_notice).pack(side="right", padx=6)
        self.notice_timer = None
        messagebox.app = self
        self.tabs = ctk.CTkTabview(self, command=self.on_tab)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        self._build_status_tab(self.tabs.add("Статус"))
        self._build_settings_tab(self.tabs.add("Настройки"))
        self._build_report_tab(self.tabs.add("Отчёт"))
        self._build_log_tab(self.tabs.add("Журнал"))
        fix_hotkeys_any_layout(self)
        self.statusbar = ctk.CTkLabel(self, text="", anchor="w", text_color="gray")
        self.statusbar.pack(fill="x", padx=16, pady=(0, 6))

        self.tray_hinted = False
        try:
            self.tray: Tray | None = Tray(self)
        except Exception as e:   # нет pystray или трей недоступен — крестик просто закрывает программу
            self.tray = None
            log.warning("Значок в трее недоступен: %s", e)
            self.tray_sw.configure(state="disabled")
        self.render_status()
        if instance:
            threading.Thread(target=self.listen_instances, args=(instance,), daemon=True).start()
        # Программу перенесли в другую папку или сменили Python — обновляем команду автозапуска
        if core.get_autostart() not in (None, core.autostart_command()):
            try:
                core.set_autostart(True)
            except OSError as e:
                log.warning("Не удалось обновить автозапуск: %s", e)
        if hidden and not self.tray:
            self.deiconify()
        if updated:
            log.info("Программа обновлена до версии %s", core.VERSION)
        self.update_info: updater.Update | None = None
        self.updating = False
        self.after(15_000, self.check_updates)
        self.after(100, self.poll)
        self.after(1000, self.refresh_status)
        self.after(30_000, self.persist_mail_state)
        if auto_run:
            self.after(core.AUTO_RUN_DELAY * 1000, lambda: self.start_run(auto=True))

    def save(self):
        for _ in range(3):
            try:
                core.save_config(self.conf)
                return
            except RuntimeError:   # словарь изменился во время записи (профиль правит бот) — пробуем ещё раз
                time.sleep(0.05)
        log.warning("Настройки не сохранены: они менялись во время записи")

    def persist_mail_state(self):
        """MailWatcher меняет cfg["mail_state"] в потоке движка; на диск его кладём здесь, раз в 30 секунд."""
        self.after(30_000, self.persist_mail_state)
        try:
            now = json.dumps(self.conf["mail_state"], sort_keys=True)
        except RuntimeError:
            return
        if now != self._saved_mail:
            self._saved_mail = now
            self.save()

    # ------------------------------------------------------------------ фоновый цикл asyncio

    def submit(self, coro, done=None):
        """Запустить корутину в цикле движка; done(result, error) — потом в окне."""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def finished(f):
            try:
                res, err = f.result(), None
            except BaseException as e:   # включая CancelledError
                res, err = None, e
            if done:
                self.call_soon(lambda: done(res, err))
        fut.add_done_callback(finished)
        return fut

    @staticmethod
    async def call_in_loop(fn, *args):
        """Вызвать обычную функцию в потоке цикла (хранилище и движок трогаем только оттуда)."""
        return fn(*args)

    # ------------------------------------------------------------------ шапка

    def _build_header(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(12, 4))
        self.header_top = top
        ctk.CTkLabel(top, text="Auto Wohnung Search",
                     font=ctk.CTkFont(size=20, weight="bold")).pack(side="left")
        ctk.CTkLabel(top, text=f"v{core.VERSION}", text_color="gray").pack(side="left", padx=(6, 0), pady=(6, 0))


        # Полоса «доступна новая версия» — показывается под заголовком, когда есть обновление
        self.update_bar = ctk.CTkFrame(self, fg_color=("#e3f0ff", "#1d2f44"))
        self.update_lbl = ctk.CTkLabel(self.update_bar, text="")
        self.update_lbl.pack(side="left", padx=12, pady=6)
        ctk.CTkButton(self.update_bar, text="Позже", width=80, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=self.update_bar.pack_forget).pack(side="right", padx=(4, 10))
        ctk.CTkButton(self.update_bar, text="Что нового", width=110, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=lambda: webbrowser.open(self.update_info.page)).pack(side="right", padx=4)
        self.update_btn = ctk.CTkButton(self.update_bar, text="Обновить сейчас", width=150, fg_color=GREEN,
                                        hover_color=GREEN_HOVER)
        self.update_btn.pack(side="right", padx=4)

        self.status_dot = ctk.CTkLabel(top, text="●", font=ctk.CTkFont(size=18))
        self.status_dot.pack(side="left", padx=(18, 4))
        self.status_lbl = ctk.CTkLabel(top, text="")
        self.status_lbl.pack(side="left")
        self.run_btn = ctk.CTkButton(top, text="▶ Запустить", width=150, fg_color=GREEN, hover_color=GREEN_HOVER,
                                     command=self.toggle_run)
        self.run_btn.pack(side="right")

    def render_status(self):
        text, color = STATUS[self.status]
        self.status_dot.configure(text_color=color)
        self.status_lbl.configure(text=text)
        stopped = self.status == "stopped"
        self.run_btn.configure(text="▶ Запустить" if stopped else "■ Остановить",
                               fg_color=GREEN if stopped else RED, hover_color=GREEN_HOVER if stopped else "#b94440",
                               state="disabled" if self.busy else "normal")
        self.update_tray()

    def set_status(self, status: str):
        self.status = status
        self.render_status()

    def update_tray(self):
        if self.tray:
            self.tray.update(self.status, f"Auto Wohnung Search — {STATUS[self.status][0]}")

    # ------------------------------------------------------------------ запуск и остановка

    def toggle_run(self):
        if self.busy:
            return
        if self.status == "stopped":
            self.start_run()
        else:
            self.stop_run()

    def prepare_start(self, auto=False, only=None) -> bool:
        """Проверяет, что есть всё нужное для запуска. Gmail и профиль не обязательны — только предупреждаем."""
        if not self.apply_settings(silent=True):
            return False
        c = self.conf
        problems = []
        if not c["bot_token"]:
            problems.append("Укажите токен Telegram-бота во вкладке «Настройки» (его выдаёт @BotFather).")
        if not ai_ready(c):
            problems.append("Настройте нейросеть во вкладке «Настройки»: выберите способ и введите ключ "
                            "или войдите в аккаунт.")
        no_platform = only is None and not any(c["platforms"][k].get("enabled") for k, _, cls in PLATFORM_ROWS if cls)
        if no_platform:
            problems.append("Не выбрано ни одной площадки: включите переключатель у нужных во вкладке «Статус».")
        if problems:
            log.warning("Запуск невозможен: %s", " ".join(problems))
            if not auto:
                self.tabs.set("Статус" if no_platform and len(problems) == 1 else "Настройки")
                messagebox.showerror("Запуск", "\n".join(problems))
            return False
        notes = []
        if not (c["gmail_user"] and c["gmail_app_password"]):
            notes.append("Gmail не указан — ответы арендодателей определяться не будут.")
        if not profile_complete(c["profile"]):
            notes.append("Профиль не заполнен — заполни профиль в боте: /profile.")
        if notes and auto:   # автозапуск без людей: модальное окно заблокировало бы его
            log.warning("Запуск: %s", " ".join(notes))
            if self.tray:
                self.tray.notify(" ".join(notes))
        elif notes:
            messagebox.showwarning("Запуск", " ".join(notes))
        return True

    def start_run(self, auto=False, only=None):
        """only — ключи площадок, запускаемых по одной; None — все, у которых включён переключатель."""
        if self.busy or self.status != "stopped":
            return
        if not self.prepare_start(auto, only):
            return
        self.busy = True
        self.set_status("starting")
        if only:
            log.info("Запускаю только %s…", ", ".join(PLATFORM_NAMES[k] for k in only))
        else:
            log.info("Запускаю…")

        def done(_, err):
            self.busy = False
            if err:
                self.set_status("stopped")
                token = self.conf.get("bot_token", "")
                log.error("Не удалось запустить: %s", _scrub(f"{type(err).__name__}: {err}", token))
                messagebox.showerror("Запуск", "Не удалось запустить. Чаще всего неверный токен бота или нет "
                                               "связи с Telegram. Подробности — во вкладке «Журнал».")
                return
            self.set_status("running")
            log.info("Работа начата")
            self.refresh_status(once=True)
        self.submit(self._start_engine(only), done)

    async def _start_engine(self, only=None):
        """Собирает движок и бот в цикле asyncio. Движку нужен уведомитель, а боту — движок: движок
        создаётся с notifier=None, а start_bot сам создаёт TelegramNotifier и вешает его на engine.notifier
        до первого вызова engine.run()."""
        c = self.conf
        mail = (MailWatcher(c["gmail_user"], c["gmail_app_password"], c["mail_state"])
                if c["gmail_user"] and c["gmail_app_password"] else None)
        sources = {name: cls() for name, _, cls in PLATFORM_ROWS if cls}
        engine = Engine(c, self.store, AIClient(c, log), sources, self.browser, notifier=None, mail=mail,
                        debug_dir=BASE / "debug", only=only)
        log_settings(c, mail is not None, [k for k in sources if engine.active(k)])
        poll_once = engine.poll_once

        async def tracked(platform: str):   # Engine не хранит время опроса — запоминаем здесь
            try:
                await poll_once(platform)
            finally:
                self.last_poll[platform] = time.time()
        engine.poll_once = tracked   # циклы Engine.run берут poll_once с экземпляра при каждом вызове
        for key in list(self.login_btns):   # запуск во время входа: площадку не трогаем до «Готово»
            engine.login_started(key)
        self.bot_app = await start_bot(c, engine, self.store)
        self.engine = engine
        self.run_task = asyncio.get_running_loop().create_task(engine.run())
        self.run_task.add_done_callback(self._engine_ended)

    @staticmethod
    def _engine_ended(task: asyncio.Task):
        if not task.cancelled() and task.exception():
            log.error("Работа движка прервалась: %r", task.exception())

    async def _stop_engine(self):
        """Останавливает движок и бота (браузер не трогает). Каждый шаг — отдельно: сбой одного не мешает остальным."""
        engine, task, bot_app = self.engine, self.run_task, self.bot_app
        if engine:
            await engine.stop()
        if task:
            try:
                await asyncio.wait_for(task, STOP_WAIT)   # по таймауту wait_for сам отменяет задачу
            except (asyncio.TimeoutError, asyncio.CancelledError):
                log.warning("Движок не завершился за %s с — остановлен принудительно", STOP_WAIT)
            except Exception:
                log.exception("Ошибка при остановке движка")
        if bot_app:
            try:
                notifier = bot_app.bot_data.get("notifier")
                if notifier:   # успеть отправить то, что уже в очереди (последнее сообщение владельцу)
                    await asyncio.wait_for(notifier.drain(), 10)
            except Exception:
                pass
            try:
                await stop_bot(bot_app)
            except Exception:
                log.exception("Ошибка при остановке бота")
        self.engine = self.bot_app = self.run_task = None

    async def _close_browser(self):
        """Браузеры живут весь сеанс (окно входа может быть открыто и при остановленной работе) —
        закрываем их только при выходе из программы."""
        try:
            await asyncio.wait_for(self.browser.close(), 15)
        except Exception:
            log.exception("Ошибка при закрытии браузера")

    def stop_run(self):
        if self.busy:
            return
        self.busy = True
        self.render_status()
        log.info("Останавливаю… (ждём, пока закончится текущий шаг)")

        def done(_, err):
            self.busy = False
            self.set_status("stopped")
            self.save()
            if err:
                log.error("Ошибка при остановке: %r", err)
            else:
                log.info("Остановлено")
            self.refresh_status(once=True)
        self.submit(self._stop_engine(), done)

    # ------------------------------------------------------------------ обновления


    def check_updates(self, manual=False):
        """Фоновая проверка GitHub Releases: при запуске и раз в 6 часов; manual — по кнопке."""
        if not manual:
            self.after(self.UPDATE_EVERY_MS, self.check_updates)
        if self.updating:
            return
        if manual:
            self.update_check_lbl.configure(text="Проверяю…", text_color="gray")

        def work():
            try:
                return updater.check(), None
            except Exception as e:
                return None, e

        def done(res):
            u, err = res
            if err:
                log.warning("Не удалось проверить обновления: %s", err)
                if manual:
                    self.update_check_lbl.configure(text="Не удалось проверить — нет связи с GitHub", text_color=RED)
                return
            if not u:
                if manual:
                    self.update_check_lbl.configure(text="У вас последняя версия ✓", text_color=GREEN)
                return
            if manual:
                self.update_check_lbl.configure(text=f"Доступна версия {u.version}", text_color=GREEN)
            fresh = not self.update_info or self.update_info.version != u.version
            self.update_info = u
            if fresh:
                log.info("Доступна новая версия %s", u.version)
            if core.FROZEN and u.url and self.conf["auto_update"] and not manual and self.can_update_quietly():
                self.start_update()
                return
            self.show_update_bar()
            if fresh and self.tray and self.state() == "withdrawn":
                self.tray.notify(f"Доступна версия {u.version} — откройте окно, чтобы обновить")
        self.in_thread(work, done)


    def can_update_quietly(self) -> bool:
        """Сами ставим обновление, только если ничего не прервём (пока прерывать нечего)."""
        return not self.updating and self.status == "stopped"   # на ходу не перезапускаем: письмо может быть в пути


    def show_update_bar(self):
        u = self.update_info
        can_install = core.FROZEN and u.url
        self.update_lbl.configure(text=f"Доступна новая версия {u.version} (у вас {core.VERSION})")
        self.update_btn.configure(text="Обновить сейчас" if can_install else "Скачать",
                                  state="normal", command=self.start_update if can_install
                                  else lambda: webbrowser.open(u.page))
        self.update_bar.pack(fill="x", padx=12, pady=(0, 4), after=self.header_top)

    def start_update(self):
        u = self.update_info
        if not u or self.updating:
            return
        self.updating = True
        self.show_update_bar()
        self.update_btn.configure(state="disabled")
        self.update_lbl.configure(text=f"Скачиваю версию {u.version}…")
        log.info("Скачиваю обновление %s…", u.version)

        def progress(x):
            self.call_soon(lambda: self.update_lbl.configure(text=f"Скачиваю версию {u.version}… {int(x * 100)}%"))

        def work():
            try:
                return updater.download(u, progress), None
            except Exception as e:
                return None, e

        def done(res):
            setup, err = res
            if err:
                self.updating = False
                log.error("Не удалось скачать обновление: %s", err)
                self.update_lbl.configure(text=f"Не удалось скачать обновление: {err}")
                self.update_btn.configure(state="normal", text="Повторить")
                return
            tray = self.state() == "withdrawn"
            log.info("Устанавливаю версию %s — программа перезапустится", u.version)
            self.shutdown()
            try:
                updater.install(setup, run=False, tray=tray)
            except Exception as e:   # антивирус удалил файл и т. п. — возвращаем всё, как было
                log.error("Не удалось запустить установщик обновления: %s", e)
                self.restore_after_failed_update()
                self.update_lbl.configure(text=f"Не удалось установить обновление: {e}")
                return
            self.destroy()
        self.in_thread(work, done)

    def restore_after_failed_update(self):
        self.updating = False
        try:
            self.tray = Tray(self)
        except Exception as e:
            self.tray = None
            log.warning("Значок в трее недоступен: %s", e)
        self.show_update_bar()
        self.update_btn.configure(state="normal", text="Повторить")
        self.render_status()

    def shutdown(self):
        """Убирает значок — перед выходом и перед обновлением (движок остановится здесь же)."""
        if self.tray:
            self.tray.stop()
        try:   # остановить работу, если идёт, и всегда закрыть браузеры: окно входа могло быть открыто и так
            if self.status != "stopped" or self.engine:
                self.submit(self._stop_engine()).result(STOP_WAIT + 20)
            self.submit(self._close_browser()).result(25)
        except Exception as e:
            log.warning("Остановка при выходе: %r", e)
        self.status = "stopped"
        self.save()


    def listen_instances(self, srv: socket.socket):
        """Повторный запуск программы: показываем это окно вместо второй копии."""
        while True:
            try:
                conn, _ = srv.accept()
                with conn:
                    conn.settimeout(2)
                    if conn.recv(64) == INSTANCE_HELLO:
                        conn.sendall(INSTANCE_REPLY)
                        self.call_soon(self.show_window)
            except OSError:
                if srv.fileno() < 0:   # сокет закрыт — слушать больше нечего
                    return
                time.sleep(1)          # временная ошибка — не крутим цикл вхолостую

    # ------------------------------------------------------------------ вкладки

    def _build_status_tab(self, tab):
        """Площадки: состояние, последний опрос, отправлено за сегодня; кнопки «Войти» и «Пауза»."""
        box = ctk.CTkFrame(tab)
        box.pack(fill="x", padx=4, pady=4)
        for col, w in enumerate((150, 190, 190, 170)):
            box.grid_columnconfigure(col, minsize=w)
        for col, title in enumerate(("Площадка", "Состояние", "Последний опрос", "Отправлено сегодня")):
            ctk.CTkLabel(box, text=title, text_color="gray").grid(row=0, column=col, sticky="w", padx=14, pady=(8, 2))
        self.plat_ui: dict[str, dict] = {}
        for i, (key, name, cls) in enumerate(PLATFORM_ROWS, start=1):
            ui = {"state": ctk.CTkLabel(box, text="", anchor="w"), "poll": ctk.CTkLabel(box, text="—", anchor="w"),
                  "sent": ctk.CTkLabel(box, text="—", anchor="w")}
            if cls:   # переключатель — участвует ли площадка в общем запуске (кнопка сверху)
                ui["track"] = ctk.CTkSwitch(box, text=name, font=ctk.CTkFont(weight="bold"),
                                            command=lambda k=key: self.on_track(k))
                if self.conf["platforms"][key].get("enabled"):
                    ui["track"].select()
                ui["track"].grid(row=i, column=0, sticky="w", padx=14, pady=6)
            else:
                ctk.CTkLabel(box, text=name, font=ctk.CTkFont(weight="bold")).grid(row=i, column=0, sticky="w",
                                                                                   padx=14, pady=6)
            for col, k in enumerate(("state", "poll", "sent"), start=1):
                ui[k].grid(row=i, column=col, sticky="w", padx=14)
            if cls:
                btns = ctk.CTkFrame(box, fg_color="transparent")
                btns.grid(row=i, column=4, sticky="e", padx=10)
                ui["run"] = ctk.CTkButton(btns, text="▶ Только эту", width=120, fg_color=GREEN,
                                          hover_color=GREEN_HOVER, command=lambda k=key: self.on_run_one(k))
                ui["run"].pack(side="left", padx=3)
                ui["login"] = ctk.CTkButton(btns, text="Войти", width=90, command=lambda k=key: self.on_login(k))
                ui["login"].pack(side="left", padx=3)
                ui["pause"] = ctk.CTkButton(btns, text="Пауза", width=100, fg_color=GRAY, hover_color=GRAY_HOVER,
                                            command=lambda k=key: self.on_pause(k))
                ui["pause"].pack(side="left", padx=3)
            self.plat_ui[key] = ui
        box.grid_columnconfigure(4, weight=1)
        self.counts_lbl = ctk.CTkLabel(tab, text="", anchor="w", justify="left")
        self.counts_lbl.pack(fill="x", padx=18, pady=(10, 2))
        self.hints_lbl = ctk.CTkLabel(tab, text="", anchor="w", justify="left", wraplength=900, text_color=AMBER)
        self.hints_lbl.pack(fill="x", padx=18, pady=2)
        ctk.CTkLabel(tab, anchor="w", justify="left", wraplength=900, text_color="gray", text=(
            "Переключатель у площадки — участвует ли она, когда нажимаете «▶ Запустить» сверху (во время работы "
            "действует сразу). «▶ Только эту» — запустить одну площадку; во время работы — добавить её к запуску. "
            "Всем остальным (черновики, фильтры, профиль, режим «авто / подтверждать») управляет Telegram-бот. "
            "Immowelt и ImmoScout24 появятся в следующих версиях.")).pack(fill="x", padx=18, pady=(4, 0))

    # ------------------------------------------------------------------ состояние площадок

    async def _snapshot(self) -> dict:
        """Состояние для вкладки «Статус». Считается в потоке цикла: хранилище и движок — только оттуда."""
        e = self.engine
        now = e.clock() if e else datetime.utcnow()
        day = to_local(now).date()
        since = to_utc(datetime(day.year, day.month, day.day))
        if e:
            st = e.status()
        else:
            st = {"paused": [], "paused_all": False, "incidents": self.store.open_incidents(),
                  "drafts": len(self.store.by_status("draft")), "queued": len(self.store.by_status("queued")),
                  "today": {}}
        st["sent"] = {k: self.store.sent_count(k, since) for k, _, cls in PLATFORM_ROWS if cls}
        st["running"] = e is not None
        st["active"] = [k for k in e.sources if e.active(k)] if e else []
        return st

    def refresh_status(self, once=False):
        if not once:
            self.after(3000, self.refresh_status)
        if self.busy:
            return
        self.submit(self._snapshot(), lambda st, err: self.render_platforms(st) if not err else None)

    def platform_state(self, key: str, st: dict) -> tuple[str, str]:
        """(текст, цвет) состояния площадки."""
        kinds = [k for p, k in st["incidents"] if p == key]
        if "logged_out" in kinds:
            return "вход нужен", RED
        blocking = [k for k in kinds if k in ("captcha", "blocked", "broken")]
        if blocking:
            return f"проблема: {KIND_TEXT[blocking[0]]}", AMBER
        if not st["running"]:
            return "не запущено", "gray"
        if key not in st["active"]:
            return "не в этом запуске", "gray"
        if st["paused_all"] or key in st["paused"]:
            return "пауза", AMBER
        return "включена", GREEN

    def render_platforms(self, st: dict):
        running, problem = st["running"], False
        for key, name, cls in PLATFORM_ROWS:
            ui = self.plat_ui[key]
            if not cls:
                ui["state"].configure(text="скоро", text_color="gray")
                continue
            text, color = self.platform_state(key, st)
            problem = problem or (running and color in (RED, AMBER) and text != "пауза")
            ui["state"].configure(text=text, text_color=color)
            ts = self.last_poll.get(key)
            ui["poll"].configure(text=time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "—")
            cap = self.conf["platforms"][key]["daily_cap"]
            ui["sent"].configure(text=f"{st['sent'][key]} из {cap}")
            paused = st["paused_all"] or key in st["paused"]
            ui["pause"].configure(text="Продолжить" if paused else "Пауза", state="normal" if running else "disabled")
            ui["run"].configure(text="▶ Добавить" if running else "▶ Только эту",
                                state="disabled" if self.busy or key in st["active"] else "normal")
            if key not in self.login_btns:   # во время входа кнопка «Войти» занята другим
                ui["login"].configure(text="Войти", state="normal")
            # напоминание в трей о новой проблеме, если окно скрыто
            for p, k in st["incidents"]:
                if p == key and (p, k) not in self.seen_incidents:
                    self.seen_incidents.add((p, k))
                    if self.tray and self.state() == "withdrawn":
                        self.tray.notify(f"{name}: {KIND_TEXT.get(k, k)}")
        self.seen_incidents &= set(st["incidents"])   # закрытый случай может повториться — снова напомним
        today = st.get("today") or {}
        self.counts_lbl.configure(text=(
            f"Черновиков в боте: {st['drafts']}  ·  в очереди на отправку: {st['queued']}" +
            (f"  ·  сегодня найдено {today.get('found', 0)}, отправлено {today.get('sent', 0)}, "
             f"ответов {today.get('replies', 0)}" if today else "")))
        c = self.conf
        hints = []
        if not c["bot_token"]:
            hints.append("Укажите токен бота во вкладке «Настройки».")
        elif running and not c.get("owner_chat_id"):
            hints.append("Напишите своему боту /start в Telegram — он запомнит вас как владельца.")
        if not (c["gmail_user"] and c["gmail_app_password"]):
            hints.append("Gmail не указан — ответы арендодателей определяться не будут.")
        if not profile_complete(c["profile"]):
            hints.append("Профиль не заполнен — заполни профиль в боте: /profile.")
        self.hints_lbl.configure(text="\n".join(hints))
        self.snap = st
        if running and not self.busy and self.status in ("running", "problem"):   # устаревший снимок не «оживляет»
            self.set_status("problem" if problem else "running")

    def on_track(self, key: str):
        """Переключатель площадки: сохраняется в настройках; идущий общий запуск подхватывает его сразу."""
        on = bool(self.plat_ui[key]["track"].get())
        self.conf["platforms"][key]["enabled"] = on
        self.save()
        log.info("%s: %s", PLATFORM_NAMES[key], "отслеживается при общем запуске" if on
                 else "не отслеживается при общем запуске")
        if self.engine:
            self.loop.call_soon_threadsafe(self.engine.wake, key)
        self.after(200, lambda: self.refresh_status(once=True))

    def on_run_one(self, key: str):
        """Остановлено — запуск только этой площадки; работает — площадка добавляется к запуску."""
        if self.busy:
            return
        if self.status == "stopped":
            self.start_run(only={key})
        elif self.engine:
            self.loop.call_soon_threadsafe(self.engine.activate, key)
            log.info("%s: добавлена к запуску", PLATFORM_NAMES[key])
            self.after(200, lambda: self.refresh_status(once=True))

    def on_pause(self, key: str):
        e = self.engine
        if not e:
            return
        paused = bool(self.snap) and (self.snap["paused_all"] or key in self.snap["paused"])

        def flip():
            e.resume(key) if paused else e.pause(key)
        self.loop.call_soon_threadsafe(flip)
        log.info("%s: %s", key, "работа продолжена" if paused else "пауза")
        self.after(200, lambda: self.refresh_status(once=True))

    # ------------------------------------------------------------------ вход на площадку

    def on_login(self, key: str):
        """«Войти» открывает окно браузера площадки; «Готово» проверяет вход и снимает паузу."""
        cls = next(c for k, _, c in PLATFORM_ROWS if k == key)
        btn = self.plat_ui[key]["login"]
        self.login_btns[key] = btn
        btn.configure(text="Открываю…", state="disabled")
        self._login_hold(key, None)   # движок не уводит окно со страницы входа

        def opened(_, err):
            if err:
                self.login_btns.pop(key, None)
                self._login_hold(key, False)
                btn.configure(text="Войти", state="normal")
                messagebox.showerror("Вход", f"Не удалось открыть окно браузера: {err}")
                return
            btn.configure(text="Готово", state="normal", fg_color=GREEN, hover_color=GREEN_HOVER,
                          command=lambda: self.finish_login(key))
            self.say("info", "Вход", "Войдите в аккаунт в открывшемся окне Edge и нажмите «Готово» здесь.")
        self.submit(self.browser.show_login(key, cls.login_url), opened)

    def _login_hold(self, key: str, ok: bool | None):
        """None — вход начат (площадка на паузе); True/False — вход закончен (пауза снята или как до входа)."""
        e = self.engine
        if not e:
            return
        if ok is None:
            self.loop.call_soon_threadsafe(e.login_started, key)
        else:
            self.loop.call_soon_threadsafe(e.login_finished, key, ok)

    async def _check_login(self, key: str, cls) -> bool:
        src = self.engine.sources[key] if self.engine else cls()
        try:
            async with self.browser.lock(key):
                return await src.is_logged_in(await self.browser.page(key))
        finally:
            try:
                await self.browser.hide(key)   # окно снова уезжает за экран
            except Exception:
                pass

    def finish_login(self, key: str):
        cls = next(c for k, _, c in PLATFORM_ROWS if k == key)
        name = next(n for k, n, _ in PLATFORM_ROWS if k == key)
        btn = self.login_btns[key]
        btn.configure(text="Проверяю…", state="disabled")

        def done(ok, err):
            self.login_btns.pop(key, None)
            btn.configure(text="Войти", state="normal", fg_color=BTN_COLOR, hover_color=BTN_HOVER,
                          command=lambda: self.on_login(key))
            if err:
                self._login_hold(key, False)
                log.warning("%s: вход не проверен: %r", name, err)
                messagebox.showerror("Вход", f"Не удалось проверить вход на {name}: {err}")
                return
            if not ok:
                self._login_hold(key, False)
                messagebox.showwarning("Вход", f"{name}: вход не выполнен. Нажмите «Войти» и попробуйте ещё раз.")
                return
            log.info("%s: вход выполнен", name)
            if self.engine:
                self._login_hold(key, True)   # снимает паузу и закрывает случаи
            else:
                self.store.close_incidents(key)
            p = self.conf["platforms"][key]
            if not p.get("mail_hint_shown"):   # одно напоминание на площадку
                p["mail_hint_shown"] = True
                self.save()
                messagebox.showwarning("Ответы по почте", MAIL_HINT.format(name=name))
            self.refresh_status(once=True)
        self.submit(self._check_login(key, cls), done)

    # ------------------------------------------------------------------ настройки

    def _build_settings_tab(self, tab):
        sf = self.settings_sf = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        sf.pack(fill="both", expand=True)
        repaint_after_scroll(sf)
        self.fields: dict[str, ctk.CTkEntry] = {}

        def section(title, hint=None):
            f = ctk.CTkFrame(sf)
            f.pack(fill="x", padx=4, pady=6)
            f.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(f, text=title, font=ctk.CTkFont(size=15, weight="bold")).grid(
                row=0, column=0, columnspan=3, sticky="w", padx=14, pady=(10, 0))
            if hint:
                ctk.CTkLabel(f, text=hint, text_color="gray", justify="left").grid(
                    row=1, column=0, columnspan=3, sticky="w", padx=14)
            return f

        def field(f, row, key, label, hint="", width=None, secret=False, **kw):
            ctk.CTkLabel(f, text=label).grid(row=row, column=0, sticky="w", padx=14, pady=5)
            e = ctk.CTkEntry(f, width=width or 300, show="•" if secret else "", **kw)
            e.grid(row=row, column=1, sticky="w" if width else "ew", padx=4, pady=5)
            cell = ctk.CTkFrame(f, fg_color="transparent")
            cell.grid(row=row, column=2, sticky="w", padx=6)
            if secret:
                ctk.CTkButton(cell, text="показать", width=80, fg_color=GRAY, hover_color=GRAY_HOVER,
                              command=lambda: e.configure(show="" if e.cget("show") else "•")).pack(side="left", padx=2)
            if hint:
                ctk.CTkLabel(cell, text=hint, text_color="gray").pack(side="left", padx=6)
            self.fields[key] = e
            return cell

        # --- Telegram-бот
        tg = section("Telegram-бот", "Токен выдаёт @BotFather. После запуска напишите боту /start — он запомнит вас")
        field(tg, 2, "bot_token", "Токен бота", secret=True, placeholder_text="123456:AA…")
        ctk.CTkFrame(tg, height=6, fg_color="transparent").grid(row=3, column=0)

        # --- Gmail
        gm = section("Gmail (ответы арендодателей)",
                     "Площадки присылают ответы на этот адрес; программа только читает письма. "
                     "Без Gmail ответы определяться не будут")
        field(gm, 2, "gmail_user", "Адрес Gmail", placeholder_text="name@gmail.com")
        cell = field(gm, 3, "gmail_app_password", "Пароль приложения", secret=True,
                     placeholder_text="16 букв из myaccount.google.com/apppasswords")
        self.mail_btn = ctk.CTkButton(cell, text="Проверить", width=100, command=self.check_mail)
        self.mail_btn.pack(side="left", padx=2)
        self.mail_lbl = ctk.CTkLabel(gm, text="", text_color="gray", anchor="w", justify="left", wraplength=800)
        self.mail_lbl.grid(row=4, column=1, columnspan=2, sticky="w", padx=6, pady=(0, 6))
        ctk.CTkButton(gm, text="Как получить пароль приложения → myaccount.google.com/apppasswords",
                      fg_color="transparent", text_color=("#1f6aa5", "#5aa9e6"), hover=False, anchor="w",
                      command=lambda: webbrowser.open("https://myaccount.google.com/apppasswords")
                      ).grid(row=5, column=1, columnspan=2, sticky="w", pady=(0, 8))

        # --- Нейросеть (из tg-autocomment)
        ai_s = section("Нейросеть")
        ai_s.grid_columnconfigure(0, minsize=LABEL_W)
        ctk.CTkLabel(ai_s, text="Как подключаться").grid(row=2, column=0, sticky="w", padx=14, pady=5)
        self.backend = ctk.CTkSegmentedButton(ai_s, values=list(BACKENDS), command=lambda v: self.show_backend())
        self.backend.grid(row=2, column=1, columnspan=2, sticky="w", padx=4)

        def sub(hint):
            f = ctk.CTkFrame(ai_s, fg_color="transparent")
            f.grid_columnconfigure(1, weight=1)
            f.grid_columnconfigure(0, minsize=LABEL_W)
            ctk.CTkLabel(f, text=hint, text_color="gray", justify="left", wraplength=900).grid(
                row=0, column=0, columnspan=3, sticky="w", padx=14)
            return f

        def model_row(f, row, widget, hint):
            ctk.CTkLabel(f, text="Модель").grid(row=row, column=0, sticky="w", padx=14, pady=5)
            widget.grid(row=row, column=1, sticky="w", padx=4)
            lbl = ctk.CTkLabel(f, text=hint, text_color="gray")
            lbl.grid(row=row, column=2, sticky="w", padx=10)
            return lbl

        # Claude Code по подписке
        cc = sub("Работает через программу Claude Code и вашу подписку Pro/Max — за запросы отдельно не платите.")
        self.model = ctk.CTkSegmentedButton(cc, values=ai.MODELS)
        model_row(cc, 1, self.model, "sonnet — оптимально, opus — умнее и медленнее, haiku — быстрее")
        self.claude_block = CliBlock(self, cc, row=2, name="Claude Code", account="Claude",
                                     status=ai.claude_status, install=ai.install_claude,
                                     login=ai.open_claude_login, describe=describe_claude, hint=(
            "Откроется браузер (или окно с ссылкой) — войдите в аккаунт Claude с подпиской "
            "Pro/Max и нажмите «Authorize».\n\n"
            "Если в чёрном окне попросят вставить код — скопируйте его со страницы в браузере, "
            "вставьте туда и нажмите Enter.\n\nПрограмма сама заметит, когда вход завершится."))

        # Anthropic API по ключу
        api = sub("Запросы идут напрямую в API Anthropic и оплачиваются с баланса по токенам. "
                  "Claude Code и подписка не нужны.")
        self.api_model = ctk.CTkOptionMenu(api, values=ai.API_MODELS, width=220, command=lambda v: self.update_price())
        KeyBlock(self, api, "api_key", "sk-ant-…", "Где взять ключ → console.anthropic.com",
                 "https://console.anthropic.com/settings/keys", self.api_model, "api_model", ai.check_api_key)
        self.price_lbl = model_row(api, 3, self.api_model, "")

        # Antigravity CLI через Google-аккаунт
        gc = sub("Работает через Antigravity CLI — новую программу Google вместо Gemini CLI — и ваш "
                 "Google-аккаунт; ключ не нужен. Модели Gemini; лимиты зависят от аккаунта "
                 "(бесплатный, AI Pro или Ultra).")
        self.gemini_model = ctk.CTkComboBox(gc, values=[DEFAULT_MODEL], width=260)
        model_row(gc, 1, self.gemini_model, "список моделей появится после входа")
        self.gemini_block = CliBlock(self, gc, row=2, name="Antigravity CLI", account="Google",
                                     status=ai.agy_status, install=ai.install_agy,
                                     login=ai.open_agy_login, describe=self.describe_agy, hint=(
            "Откроется окно Antigravity CLI и браузер — войдите в Google-аккаунт и разрешите доступ.\n\n"
            "Если браузер не открылся — скопируйте ссылку из чёрного окна в браузер, а полученный "
            "код вставьте обратно в окно и нажмите Enter.\n\n"
            "Когда вход завершится, окно Antigravity можно закрыть и нажать «Готово — проверить»."),
            relogin_hint=(
            "Откроется окно Antigravity — в нём ещё старый аккаунт.\n\n"
            "1. Введите /logout и нажмите Enter.\n"
            "2. Войдите в нужный Google-аккаунт (Antigravity сам предложит вход или откроет браузер).\n"
            "3. Закройте окно и нажмите в программе «Готово — проверить».\n\n"
            "Какой аккаунт сейчас активен, видно в окне Antigravity по команде /usage."))

        # Gemini API по ключу
        ga = sub("Запросы идут в Gemini API по ключу из Google AI Studio. У ключа есть бесплатный "
                 "лимит, сверх него — оплата по токенам. Gemini CLI не нужен.")
        self.gemini_api_model = ctk.CTkComboBox(ga, values=[ai.GEMINI_API_DEFAULT], width=260)
        KeyBlock(self, ga, "gemini_api_key", "AIza…", "Где взять ключ → aistudio.google.com",
                 "https://aistudio.google.com/apikey", self.gemini_api_model, "gemini_api_model",
                 ai.check_gemini_key)
        model_row(ga, 3, self.gemini_api_model, "список моделей подгрузится после «Проверить ключ»")

        # ChatGPT через Codex CLI по подписке
        cx = sub("Работает через Codex CLI — программу OpenAI — и ваш аккаунт ChatGPT с любым тарифом, "
                 "включая бесплатный; ключ не нужен. Расходует лимиты Codex из тарифа: на Free и Go они "
                 "маленькие, для постоянной работы лучше Plus и выше.")
        self.codex_model = ctk.CTkComboBox(cx, values=[DEFAULT_MODEL], width=260)
        model_row(cx, 1, self.codex_model, "список моделей появится после входа")
        self.codex_block = CliBlock(self, cx, row=2, name="Codex CLI", account="ChatGPT",
                                    status=ai.codex_status, install=ai.install_codex,
                                    login=ai.open_codex_login, describe=self.describe_codex, hint=(
            "Откроется браузер — войдите в аккаунт ChatGPT и разрешите доступ для Codex.\n\n"
            "Если браузер не открылся — скопируйте ссылку из чёрного окна в браузер.\n\n"
            "Когда вход завершится, нажмите в программе «Готово — проверить»."),
            relogin_hint=(
            "Программа вышла из текущего аккаунта ChatGPT и открыла вход заново.\n\n"
            "Войдите в браузере в нужный аккаунт, затем нажмите «Готово — проверить»."))

        # OpenAI API по ключу
        oa = sub("Запросы идут напрямую в OpenAI API и оплачиваются с баланса по токенам "
                 "(отдельно от подписки ChatGPT). Codex не нужен.")
        self.openai_api_model = ctk.CTkComboBox(oa, values=[ai.OPENAI_API_DEFAULT], width=260)
        KeyBlock(self, oa, "openai_api_key", "sk-…", "Где взять ключ → platform.openai.com",
                 "https://platform.openai.com/api-keys", self.openai_api_model, "openai_api_model",
                 ai.check_openai_key)
        model_row(oa, 3, self.openai_api_model, "список моделей подгрузится после «Проверить ключ»")

        # Z.ai: бесплатные GLM, работает из России
        za = sub("Бесплатные нейросети GLM от Z.ai. Работают в России без VPN, карта не нужна — только "
                 "бесплатный ключ.")
        self.zai_model = ctk.CTkComboBox(za, values=ai.ZAI_MODELS, width=260)
        self.zai_block = KeyBlock(self, za, "zai_api_key", "вставится сам, когда скопируете",
                                  "Получить бесплатный ключ → z.ai", ai.ZAI_KEYS_URL, self.zai_model,
                                  "zai_model", ai.check_zai_key, autoselect=True)
        ctk.CTkLabel(za, justify="left", anchor="w", text=(
            "1. Нажмите «Получить бесплатный ключ» — откроется сайт Z.ai (на английском).\n"
            "2. Войдите через Google или зарегистрируйтесь по почте (Sign up).\n"
            "3. Нажмите «Create a new API key» (создать ключ), введите любое имя и скопируйте ключ (Copy).\n"
            "4. Вернитесь в программу — ключ вставится и проверится сам. Нажмите «Сохранить».")
        ).grid(row=3, column=1, columnspan=2, sticky="w", padx=4, pady=(4, 6))
        model_row(za, 4, self.zai_model, "менять не нужно — glm-4.7-flash бесплатная и самая новая из бесплатных")

        # своя модель: любой OpenAI-совместимый сервер
        lm = sub("Своя нейросеть на OpenAI-совместимом сервере: локально — LM Studio, Ollama, llama.cpp, vLLM "
                 "(бесплатно, без интернета и VPN), или облачные OpenRouter, DeepSeek и т. п. Промпт "
                 "большой — у модели должен быть контекст от 16–32 тыс. токенов.")
        ctk.CTkLabel(lm, text="Адрес сервера").grid(row=1, column=0, sticky="w", padx=14, pady=5)
        url_e = ctk.CTkEntry(lm, placeholder_text="http://localhost:1234/v1")
        url_e.grid(row=1, column=1, sticky="ew", padx=4)
        self.fields["compat_base_url"] = url_e
        presets = ctk.CTkFrame(lm, fg_color="transparent")
        presets.grid(row=1, column=2, sticky="w", padx=6)

        def preset(u):
            url_e.delete(0, "end")
            url_e.insert(0, u)
        for name, u in ai.COMPAT_PRESETS.items():
            ctk.CTkButton(presets, text=name, width=90, fg_color=GRAY, hover_color=GRAY_HOVER,
                          command=lambda u=u: preset(u)).pack(side="left", padx=2)
        ctk.CTkLabel(lm, text="API-ключ").grid(row=2, column=0, sticky="w", padx=14, pady=5)
        key_e = ctk.CTkEntry(lm, show="•", placeholder_text="необязательно — локальным серверам не нужен")
        key_e.grid(row=2, column=1, sticky="ew", padx=4)
        self.fields["compat_api_key"] = key_e
        self.compat_model = ctk.CTkComboBox(lm, values=[""], width=260)
        model_row(lm, 3, self.compat_model, "список подгрузится после «Проверить подключение»")
        chk = ctk.CTkFrame(lm, fg_color="transparent")
        chk.grid(row=4, column=1, columnspan=2, sticky="w", padx=4, pady=(2, 4))
        self.compat_btn = ctk.CTkButton(chk, text="Проверить подключение", width=180, command=self.check_compat)
        self.compat_btn.pack(side="left")
        self.compat_lbl = ctk.CTkLabel(chk, text="", text_color="gray")
        self.compat_lbl.pack(side="left", padx=10)

        self.backend_frames = {"claude_code": cc, "api": api, "gemini_cli": gc, "gemini_api": ga,
                               "codex": cx, "openai_api": oa, "zai": za, "openai_compat": lm}
        self.bind("<FocusIn>", lambda e: self.zai_paste_key(), add="+")

        self.ai_common = ctk.CTkFrame(ai_s, fg_color="transparent")
        self.ai_common.grid_columnconfigure(0, minsize=LABEL_W)
        self.ai_common.grid(row=4, column=0, columnspan=3, sticky="ew")
        field(self.ai_common, 0, "claude_timeout_sec", "Таймаут ответа, сек", width=80)
        ctk.CTkFrame(ai_s, height=8, fg_color="transparent").grid(row=6, column=0)

        # --- сама программа
        wn = section("Программа")
        self.tray_sw = ctk.CTkSwitch(wn, text="При закрытии окна сворачивать в трей", command=self.on_tray_switch)
        if self.conf.get("close_to_tray", True):
            self.tray_sw.select()
        self.tray_sw.grid(row=2, column=0, columnspan=3, sticky="w", padx=14, pady=(10, 4))
        self.autostart_sw = ctk.CTkSwitch(wn, text="Запускать вместе с Windows (сразу в трей, работа начнётся "
                                                   f"через {core.AUTO_RUN_DELAY} с)", command=self.on_autostart_switch)
        if core.get_autostart():
            self.autostart_sw.select()
        self.autostart_sw.grid(row=3, column=0, columnspan=3, sticky="w", padx=14, pady=4)
        self.auto_update_sw = ctk.CTkSwitch(wn, text="Устанавливать обновления автоматически",
                                            command=self.on_auto_update_switch)
        if self.conf.get("auto_update", True):
            self.auto_update_sw.select()
        self.auto_update_sw.grid(row=4, column=0, columnspan=3, sticky="w", padx=14, pady=4)
        ver = ctk.CTkFrame(wn, fg_color="transparent")
        ver.grid(row=5, column=0, columnspan=3, sticky="w", padx=14, pady=(4, 10))
        ctk.CTkLabel(ver, text=f"Версия {core.VERSION}").pack(side="left")
        ctk.CTkButton(ver, text="Проверить обновления", width=170, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=lambda: self.check_updates(manual=True)).pack(side="left", padx=12)
        self.update_check_lbl = ctk.CTkLabel(ver, text="", text_color="gray")
        self.update_check_lbl.pack(side="left")

        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.pack(fill="x", pady=(6, 0))
        ctk.CTkButton(bar, text="Сохранить", width=140, command=self.apply_settings).pack(side="left")
        self.settings_msg = ctk.CTkLabel(bar, text="", anchor="w")
        self.settings_msg.pack(side="left", padx=12)
        self._settings_msg_job = None
        self.load_settings_form()

    def describe_agy(self, st):
        if st.get("models"):   # заодно подставляем список моделей аккаунта
            self.gemini_model.configure(values=[DEFAULT_MODEL] + st["models"])
        n = len(st.get("models") or [])
        return (f"Вход через Google выполнен ✓  ·  доступно моделей: {n}  ·  "
                "остаток лимита — команда /usage в окне agy"), GREEN

    def describe_codex(self, st):
        if st.get("models"):
            self.codex_model.configure(values=[DEFAULT_MODEL] + st["models"])
        plan = st.get("plan") or ""
        text = f"Вход в ChatGPT выполнен ✓  {st.get('email', '')}" + (f"  ·  тариф {plan}" if plan else "")
        if st.get("authMethod") == "api":
            return text + "  ·  ⚠ вход по API-ключу — запросы оплачиваются с баланса API", AMBER
        return text, GREEN

    def zai_paste_key(self):
        """Ключ Z.ai, скопированный на сайте, сам попадает в поле и проверяется, когда пользователь
        возвращается в окно. Каждое содержимое буфера — один раз: стёртый вручную ключ не вернётся.
        Уже введённый ключ без спроса не заменяем."""
        if BACKENDS.get(self.backend.get()) != "zai":
            return
        try:
            clip = self.clipboard_get().strip()
        except Exception:   # в буфере не текст или он пуст
            return
        if clip == getattr(self, "_zai_clip_seen", None):
            return
        self._zai_clip_seen = clip
        e = self.fields["zai_api_key"]
        old = e.get().strip()
        if not ai.ZAI_KEY_RE.match(clip) or clip == old:
            return
        if old and not messagebox.askyesno("Ключ Z.ai", "В буфере обмена другой ключ Z.ai. Заменить им текущий?"):
            return
        e.delete(0, "end")
        e.insert(0, clip)
        log.info("Ключ Z.ai вставлен из буфера обмена")   # сам ключ в журнал не пишем
        self.zai_block.check()

    def check_compat(self):
        cfg = dict(self.conf, compat_base_url=self.fields["compat_base_url"].get().strip(),
                   compat_api_key=self.fields["compat_api_key"].get().strip(),
                   compat_model=self.compat_model.get().strip())
        self.compat_btn.configure(state="disabled")
        self.compat_lbl.configure(text="Проверяю…", text_color="gray")

        def done(res):
            ok, msg, models = res
            self.compat_btn.configure(state="normal")
            self.compat_lbl.configure(text=msg, text_color=GREEN if ok else RED)
            if models:
                self.compat_model.configure(values=models)
                if self.compat_model.get().strip() not in models:
                    self.compat_model.set(models[0])
        self.in_thread(lambda: ai.check_compat(cfg), done)

    def check_mail(self):
        """Вход в Gmail по паролю приложения (письма не читаем). Пароль нигде не пишем."""
        user = self.fields["gmail_user"].get().strip()
        pw = self.fields["gmail_app_password"].get().replace(" ", "")
        if not user or not pw:
            self.mail_lbl.configure(text="Укажите адрес Gmail и пароль приложения", text_color=RED)
            return
        self.mail_btn.configure(state="disabled")
        self.mail_lbl.configure(text="Проверяю…", text_color="gray")

        def done(res):
            ok, msg = res
            self.mail_btn.configure(state="normal")
            self.mail_lbl.configure(text=msg if not ok else f"✓ {msg}", text_color=GREEN if ok else RED)
        self.in_thread(lambda: MailWatcher(user, pw, {}).check_login(), done)

    def show_backend(self):
        current = BACKENDS.get(self.backend.get(), "zai")
        for key, f in self.backend_frames.items():
            if key == current:
                f.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4, 0))
            else:
                f.grid_forget()

    def update_price(self):
        m = self.api_model.get()
        self.price_lbl.configure(text=f"цена за 1M токенов (вход / выход): {ai.API_PRICES.get(m, '?')}"
                                      "  ·  промпт кэшируется")

    def load_settings_form(self):
        c = self.conf
        for key, e in self.fields.items():
            e.delete(0, "end")
            e.insert(0, str(c.get(key, "")))
        self.model.set(c["model"])
        self.api_model.set(c.get("api_model") or ai.API_MODELS[0])
        self.gemini_model.set(c.get("gemini_model") or DEFAULT_MODEL)
        self.gemini_api_model.set(c.get("gemini_api_model") or ai.GEMINI_API_DEFAULT)
        self.codex_model.set(c.get("codex_model") or DEFAULT_MODEL)
        self.openai_api_model.set(c.get("openai_api_model") or ai.OPENAI_API_DEFAULT)
        self.compat_model.set(c.get("compat_model") or "")
        self.zai_model.set(c.get("zai_model") or ai.ZAI_MODELS[0])
        self.backend.set(next((k for k, v in BACKENDS.items() if v == c.get("backend")), "Z.ai (бесплатно)"))
        self.show_backend()
        self.update_price()

    def apply_settings(self, silent=False) -> bool:
        f = {k: e.get().strip() for k, e in self.fields.items()}
        new = dict(self.conf)
        try:
            new["claude_timeout_sec"] = int(f["claude_timeout_sec"])
            if new["claude_timeout_sec"] <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Настройки", "Таймаут ответа — целое число секунд больше нуля")
            return False
        if f["bot_token"] and ":" not in f["bot_token"]:
            messagebox.showerror("Настройки", "Токен бота выглядит неверно: он вида 123456:AA… (выдаёт @BotFather)")
            return False
        new["bot_token"] = f["bot_token"]
        new["gmail_user"] = f["gmail_user"]
        new["gmail_app_password"] = f["gmail_app_password"].replace(" ", "")   # Google показывает пароль с пробелами
        new["backend"] = BACKENDS.get(self.backend.get(), "zai")
        new["model"] = self.model.get() or "sonnet"
        new["api_key"] = f["api_key"]
        new["api_model"] = self.api_model.get()
        new["gemini_api_key"] = f["gemini_api_key"]
        gm = self.gemini_model.get().strip()
        new["gemini_model"] = "" if gm in ("", DEFAULT_MODEL) else gm
        new["gemini_api_model"] = self.gemini_api_model.get().strip() or ai.GEMINI_API_DEFAULT
        new["openai_api_key"] = f["openai_api_key"]
        cm = self.codex_model.get().strip()
        new["codex_model"] = "" if cm in ("", DEFAULT_MODEL) else cm
        new["openai_api_model"] = self.openai_api_model.get().strip() or ai.OPENAI_API_DEFAULT
        new["compat_base_url"] = f["compat_base_url"]
        new["compat_api_key"] = f["compat_api_key"]
        new["compat_model"] = self.compat_model.get().strip()
        new["zai_api_key"] = f["zai_api_key"]
        new["zai_model"] = self.zai_model.get().strip() or ai.ZAI_MODELS[0]
        # Токен бота и Gmail читаются при запуске; остальное (нейросеть) движок берёт из cfg на ходу
        needs_restart = [] if self.status == "stopped" else [
            label for k, label in (("bot_token", "токен бота"), ("gmail_user", "Gmail"),
                                   ("gmail_app_password", "Gmail")) if new[k] != self.conf[k]]
        # тот же словарь, что у движка и бота, — изменения применяются сразу; ключи, которых нет в форме
        # (профиль, фильтры, platforms), не трогаем
        self.conf.update({k: v for k, v in new.items() if self.conf.get(k) != v})
        self.save()
        self.load_settings_form()
        if not silent:
            if self._settings_msg_job:
                self.after_cancel(self._settings_msg_job)
                self._settings_msg_job = None
            if needs_restart:   # висит до следующего сохранения — легко пропустить
                self.settings_msg.configure(
                    text="Сохранено ✓  Чтобы применить, нажмите «Остановить» и «Запустить»: "
                         + ", ".join(dict.fromkeys(needs_restart)), text_color=RED)
            else:
                self.settings_msg.configure(text="Сохранено ✓", text_color=GREEN)
                self._settings_msg_job = self.after(6000, lambda: self.settings_msg.configure(text=""))
        return True

    # ------------------------------------------------------------------ отчёт

    REPORT_PERIODS = {"Сегодня": 0, "7 дней": 7, "30 дней": 30, "Всё время": None}

    def _build_report_tab(self, tab):
        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.pack(fill="x")
        self.rep_period = ctk.CTkSegmentedButton(top, values=list(self.REPORT_PERIODS),
                                                 command=lambda v: self.load_report())
        self.rep_period.set("7 дней")
        self.rep_period.pack(side="left")
        self.rep_summary = ctk.CTkLabel(top, text="", text_color="gray")
        self.rep_summary.pack(side="left", padx=14)
        ctk.CTkButton(top, text="Выгрузить в Excel", width=150, command=self.export_report).pack(side="right")
        ctk.CTkButton(top, text="Обновить", width=100, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=self.load_report).pack(side="right", padx=8)
        table = ctk.CTkFrame(tab, fg_color="transparent")
        table.pack(fill="both", expand=True, pady=(8, 0))
        cols = {"date": ("Дата", 120), "platform": ("Площадка", 110), "title": ("Заголовок", 380),
                "price": ("Цена, €", 70), "status": ("Статус", 90), "reply": ("Ответ", 60)}
        self.rep_tree = ttk.Treeview(table, columns=list(cols), show="headings", style="Report.Treeview",
                                     selectmode="browse")
        for key, (title, width) in cols.items():
            self.rep_tree.heading(key, text=title, anchor="w")
            self.rep_tree.column(key, width=width, minwidth=50, stretch=key == "title", anchor="w")
        bar = ctk.CTkScrollbar(table, command=self.rep_tree.yview)
        self.rep_tree.configure(yscrollcommand=bar.set)
        self.rep_tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.rep_tree.bind("<Double-1>", lambda e: self.open_report_link())
        self.rep_links: dict[str, str] = {}
        self.style_report()

    def style_report(self):
        """Таблица — ttk, у неё свои цвета: подстраиваем под светлую/тёмную тему окна."""
        dark = ctk.get_appearance_mode() == "Dark"
        bg, fg, head, sel = (("#242424", "#dce4ee", "#333333", "#1f538d") if dark else
                             ("#ffffff", "#1a1a1a", "#e4e4e4", "#3a7ebf"))
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Report.Treeview", background=bg, fieldbackground=bg, foreground=fg,
                        rowheight=int(24 * self._get_window_scaling()), borderwidth=0, font=("Segoe UI", 10),
                        bordercolor=bg, lightcolor=bg, darkcolor=bg)
        style.configure("Report.Treeview.Heading", background=head, foreground=fg, relief="flat",
                        font=("Segoe UI", 10, "bold"))
        style.map("Report.Treeview", background=[("selected", sel)], foreground=[("selected", "white")])
        style.map("Report.Treeview.Heading", background=[("active", head)])

    def on_tab(self):
        if self.tabs.get() == "Отчёт":
            self.style_report()   # тема Windows могла смениться
            self.load_report()

    def report_since(self) -> date | None:
        days = self.REPORT_PERIODS[self.rep_period.get()]
        if days is None:
            return None
        today = to_local(datetime.utcnow()).date()
        return today if days == 0 else date.fromordinal(today.toordinal() - days + 1)

    def load_report(self):
        def done(rows, err):
            if err:
                log.warning("Не удалось прочитать отчёт: %r", err)
                return
            self.rep_tree.delete(*self.rep_tree.get_children())
            self.rep_links.clear()
            for i, r in enumerate(rows):
                self.rep_tree.insert("", "end", iid=str(i), values=r[:6])
                self.rep_links[str(i)] = r[6]
            replied = sum(1 for r in rows if r[5] == "да")
            self.rep_summary.configure(text=f"Отправлено: {len(rows)}  ·  ответов: {replied}")
        self.submit(self.call_in_loop(report.report_rows, self.store, self.report_since()), done)

    def open_report_link(self):
        sel = self.rep_tree.selection()
        if sel and self.rep_links.get(sel[0]):
            webbrowser.open(self.rep_links[sel[0]])

    def export_report(self):
        path = filedialog.asksaveasfilename(
            title="Сохранить отчёт", defaultextension=".xlsx", filetypes=[("Таблица Excel", "*.xlsx")],
            initialfile=f"Отчёт {datetime.now():%Y-%m-%d}.xlsx")
        if not path:
            return

        def done(n, err):
            if err:
                messagebox.showerror("Отчёт", f"Не удалось сохранить: {err}")
            elif not n:
                messagebox.showinfo("Отчёт", "За выбранный период писем нет — выгружен только заголовок.")
            elif messagebox.askyesno("Отчёт", f"Сохранено строк: {n}.\nОткрыть файл?"):
                open_path(Path(path))
        self.submit(self.call_in_loop(report.export_report, self.store, Path(path), self.report_since()), done)

    def on_tray_switch(self):
        self.conf["close_to_tray"] = bool(self.tray_sw.get())
        self.save()

    def on_auto_update_switch(self):
        self.conf["auto_update"] = bool(self.auto_update_sw.get())
        self.save()


    def on_autostart_switch(self):
        on = bool(self.autostart_sw.get())
        try:
            core.set_autostart(on)
        except OSError as e:
            messagebox.showerror("Автозапуск", f"Не удалось изменить автозапуск Windows: {e}")
            self.autostart_sw.toggle()
            return
        log.info("Автозапуск с Windows %s", "включён" if on else "выключен")


    def in_thread(self, work, done):
        """work() в фоне, done(result) — потом в окне."""
        threading.Thread(target=lambda: (r := work(), self.call_soon(lambda: done(r))), daemon=True).start()

    def call_soon(self, fn):
        """Выполнить fn в потоке окна. Можно звать из любого потока: tkinter из чужих потоков
        вызывать нельзя, поэтому кладём в очередь — её разбирает poll()."""
        self.ui_q.put(fn)


    def say(self, kind: str, title: str, text: str):
        """Тихое сообщение над вкладками. Обычное само скрывается, предупреждение и ошибка — висят до ✕."""
        self.notice.configure(fg_color=self.NOTICE_COLORS[kind])
        self.notice_lbl.configure(text=f"{title}:  {text}" if title else text)
        self.notice.pack(fill="x", padx=12, pady=(0, 6), before=self.tabs)
        if self.notice_timer:
            self.after_cancel(self.notice_timer)
        self.notice_timer = self.after(10000, self.hide_notice) if kind == "info" else None

    def hide_notice(self):
        if self.notice_timer:
            self.after_cancel(self.notice_timer)
            self.notice_timer = None
        self.notice.pack_forget()

    def ask(self, title: str, text: str, yes: str, no: str) -> bool:
        """Вопрос «да/нет» своим окном — без звука Windows. Enter — да, Esc — нет."""
        dlg = ctk.CTkToplevel(self)
        dlg.title(title)
        dlg.resizable(False, False)
        dlg.transient(self)
        ctk.CTkLabel(dlg, text=text, justify="left", wraplength=460).pack(padx=20, pady=(18, 12))
        answer = [False]

        def done(value):
            answer[0] = value
            dlg.destroy()
        btns = ctk.CTkFrame(dlg, fg_color="transparent")
        btns.pack(padx=20, pady=(0, 16), fill="x")
        ctk.CTkButton(btns, text=no, width=110, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=lambda: done(False)).pack(side="right")
        ctk.CTkButton(btns, text=yes, width=110, command=lambda: done(True)).pack(side="right", padx=8)
        dlg.bind("<Return>", lambda e: done(True))
        dlg.bind("<Escape>", lambda e: done(False))
        dlg.update_idletasks()
        dlg.geometry(f"+{self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2}"
                     f"+{self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 3}")
        dlg.after(100, lambda: (dlg.grab_set(), dlg.focus_set()))
        self.wait_window(dlg)
        return answer[0]

    def _build_log_tab(self, tab):
        self.log_box = ctk.CTkTextbox(tab, wrap="word", font=ctk.CTkFont(family="Consolas", size=12))
        self.log_box.pack(fill="both", expand=True)
        self.log_box.tag_config("WARNING", foreground="#e0a030")
        self.log_box.tag_config("ERROR", foreground=RED)
        self.log_box.configure(state="disabled")
        bottom = ctk.CTkFrame(tab, fg_color="transparent")
        bottom.pack(fill="x", pady=(6, 0))
        ctk.CTkButton(bottom, text="Очистить", width=110, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=self.clear_log).pack(side="right")
        ctk.CTkButton(bottom, text="Открыть log.txt", width=140, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=lambda: open_path(BASE / "log.txt")).pack(side="right", padx=8)

    def clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def add_log(self, rec: logging.LogRecord):
        import time
        line = f"{time.strftime('%H:%M:%S', time.localtime(rec.created))}  {rec.getMessage()}\n"
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line, rec.levelname if rec.levelno >= logging.WARNING else ())
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        self.statusbar.configure(text=rec.getMessage().splitlines()[0][:160],
                                 text_color=RED if rec.levelno >= logging.ERROR else "gray")


    # ------------------------------------------------------------------ события

    def poll(self):
        """Разбирает задания из фоновых потоков и журнал. Ошибка в одном обработчике
        пишется в журнал и не останавливает остальные — иначе окно застынет."""
        try:
            while not self.ui_q.empty():
                self.guarded(self.ui_q.get_nowait())
            while not self.log_q.empty():
                self.add_log(self.log_q.get_nowait())
        finally:
            self.after(100, self.poll)

    def guarded(self, fn):
        try:
            fn()
        except Exception:
            log.exception("Ошибка в окне программы")

    def report_callback_exception(self, exc, val, tb):
        """Ошибки в обработчиках кнопок tkinter — в журнал и log.txt (в .exe без консоли иначе пропадают)."""
        log.error("Ошибка в окне программы", exc_info=(exc, val, tb))

    def on_close(self):
        """Крестик: в трей (программа продолжает работать) или выход — по настройке."""
        if self.tray and self.conf.get("close_to_tray", True):
            self.withdraw()
            if not self.tray_hinted:
                self.tray_hinted = True
                self.tray.notify("Программа продолжает работать в трее. Открыть — щелчок по значку, "
                                 "выйти — правая кнопка → «Выход».")
            return
        self.quit_app()

    def show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def quit_app(self):
        self.show_window()
        self.shutdown()
        self.destroy()


def run():
    instance = single_instance()
    if instance is False:   # уже запущена — она сама покажет окно
        return
    ctk.set_appearance_mode("system")
    ctk.set_default_color_theme("blue")
    args = sys.argv[1:]
    try:
        App(hidden=core.AUTOSTART_ARG in args or "--tray=1" in args, updated="--updated" in args,
            instance=instance, auto_run=core.AUTOSTART_ARG in args).mainloop()
    except Exception as e:
        logging.getLogger("wohnung").exception("Сбой")
        tk_messagebox.showerror("Auto Wohnung Search", f"Программа упала:\n{e}\n\nПодробности в log.txt")
        raise


if __name__ == "__main__":
    run()
