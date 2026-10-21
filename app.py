"""
Окно Auto Wohnung Search. Запуск: start.bat или python app.py
Пока это каркас: окно, трей, автозапуск, обновления и журнал. Остальное добавляется в следующих задачах.
"""
import ctypes
import logging
import os
import queue
import socket
import sys
import threading
import time
import webbrowser
import zlib
from pathlib import Path
from tkinter import messagebox as tk_messagebox

import customtkinter as ctk
from PIL import Image

import core
import updater
from core import BASE

GREEN, GREEN_HOVER = "#2e9e5b", "#257f49"
GRAY, GRAY_HOVER = ("#9aa0a6", "#4a4d52"), ("#80868b", "#5f6368")
RED = "#d9534f"
STATUS = {
    "stopped": ("Остановлен", "#9aa0a6"),
    "running": ("Работает", "#2e9e5b"),
}

log = logging.getLogger("wohnung")


class QueueHandler(logging.Handler):
    def __init__(self, q):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put(record)


def setup_logging() -> queue.Queue:
    q = queue.Queue()
    fmt = logging.Formatter("%(asctime)s  %(levelname)s  %(message)s")
    file_h = logging.FileHandler(BASE / "log.txt", encoding="utf-8")
    file_h.setFormatter(fmt)
    gui_h = QueueHandler(q)
    log.setLevel(logging.INFO)
    log.addHandler(file_h)
    log.addHandler(gui_h)
    return q



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
#  Окно входа в Telegram



class App(ctk.CTk):
    UPDATE_EVERY_MS = 6 * 3600 * 1000
    NOTICE_COLORS = {"info": ("#e3f0ff", "#1d2f44"), "warning": ("#fff4d6", "#463a17"),
                     "error": ("#fde3e1", "#4a1f1c")}

    def __init__(self, hidden=False, updated=False, instance: socket.socket | None = None):
        """hidden — сразу в трей (автозапуск с Windows); updated — запуск после обновления."""
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
        self.save()
        self.status = "stopped"   # нужен сразу — меню трея читает его из своего потока

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
        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        self._build_settings_tab(self.tabs.add("Настройки"))
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

    def save(self):
        core.save_config(self.conf)

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

    def render_status(self):
        text, color = STATUS[self.status]
        self.status_dot.configure(text_color=color)
        self.status_lbl.configure(text=text)
        self.update_tray()

    def update_tray(self):
        if self.tray:
            self.tray.update(self.status, f"Auto Wohnung Search — {STATUS[self.status][0]}")

    def toggle_run(self):
        """Запуск и остановка опроса появятся вместе с движком."""

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
        return not self.updating


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

    def _build_settings_tab(self, tab):
        """Пока только настройки самой программы; остальное добавляется в следующих задачах."""
        wn = ctk.CTkFrame(tab)
        wn.pack(fill="x", padx=4, pady=4)
        self.tray_sw = ctk.CTkSwitch(wn, text="При закрытии окна сворачивать в трей",
                                     command=self.on_tray_switch)
        if self.conf.get("close_to_tray", True):
            self.tray_sw.select()
        self.tray_sw.pack(anchor="w", padx=14, pady=(10, 4))
        self.autostart_sw = ctk.CTkSwitch(wn, text="Запускать вместе с Windows (сразу в трей)",
                                          command=self.on_autostart_switch)
        if core.get_autostart():
            self.autostart_sw.select()
        self.autostart_sw.pack(anchor="w", padx=14, pady=4)
        self.auto_update_sw = ctk.CTkSwitch(wn, text="Устанавливать обновления автоматически",
                                            command=self.on_auto_update_switch)
        if self.conf.get("auto_update", True):
            self.auto_update_sw.select()
        self.auto_update_sw.pack(anchor="w", padx=14, pady=4)
        ver = ctk.CTkFrame(wn, fg_color="transparent")
        ver.pack(fill="x", padx=14, pady=(4, 10))
        ctk.CTkLabel(ver, text=f"Версия {core.VERSION}").pack(side="left")
        ctk.CTkButton(ver, text="Проверить обновления", width=170, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=lambda: self.check_updates(manual=True)).pack(side="left", padx=12)
        self.update_check_lbl = ctk.CTkLabel(ver, text="", text_color="gray")
        self.update_check_lbl.pack(side="left")

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
        App(hidden=core.AUTOSTART_ARG in args or "--tray=1" in args,
            updated="--updated" in args, instance=instance).mainloop()
    except Exception as e:
        logging.getLogger("wohnung").exception("Сбой")
        tk_messagebox.showerror("Auto Wohnung Search", f"Программа упала:\n{e}\n\nПодробности в log.txt")
        raise


if __name__ == "__main__":
    run()
