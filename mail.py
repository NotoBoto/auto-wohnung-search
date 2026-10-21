"""Ответы арендодателей: читаем Gmail по IMAP (только чтение) и разбираем письма-уведомления площадок."""
import email
import html as _html
import imaplib
import logging
import re
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr

log = logging.getLogger(__name__)

HOST, PORT = "imap.gmail.com", 993
SENDERS = {"kleinanzeigen.de": "kleinanzeigen", "wg-gesucht.de": "wggesucht",
           "immowelt.de": "immowelt", "immobilienscout24.de": "immoscout"}
MAX_EXCERPT = 500
MAX_FAILS = 3   # столько опросов подряд письмо может не читаться, потом пропускаем
NO_TEXT = "(текст ответа не найден в письме)"
# страницы сообщений площадок для случая, когда в письме нет ссылки на диалог (адреса — лучшая догадка, вживую не проверены)
INBOX = {"kleinanzeigen": "https://www.kleinanzeigen.de/m-nachrichten.html",
         "wggesucht": "https://www.wg-gesucht.de/nachrichten.html",
         "immowelt": "https://www.immowelt.de/",
         "immoscout": "https://www.immobilienscout24.de/"}

# письмо про новое сообщение: тема или строка «X hat Ihnen geschrieben»; рассылки и поисковые подписки не подходят
_NEW_MSG = re.compile(r"neue\s+nachricht|nachricht\s+(?:zu|von|auf)\b|antwort\s+auf\s+(?:ihre|deine)\b|"
                      r"hat\s+(?:ihnen|dir)\s+(?:geschrieben|geantwortet|eine\s+nachricht)|schreibt\s+(?:ihnen|dir)\b", re.I)
_URL = re.compile(r"https://[^\s<>\"')\]]+")
# ссылки на объявление: ext_id как в sources/*
_AD_URL = {
    "kleinanzeigen": re.compile(r"https://(?:www\.)?kleinanzeigen\.de/s-anzeige/[^/\s?#]+/(\d+)(?:-[\w-]*)?"),
    "wggesucht": re.compile(r"https://(?:www\.)?wg-gesucht\.de/[^\s?#]*?\.(\d+)\.html"),
    "immowelt": re.compile(r"https://(?:www\.)?immowelt\.de/expose/([\w-]+)"),
    "immoscout": re.compile(r"https://(?:www\.)?immobilienscout24\.de/expose/(\d+)"),
}
_CONV_HINT = re.compile(r"nachricht|message|conversation|posteingang|inbox|anfrage|kontakt", re.I)
_BRANDS = re.compile(r"kleinanzeigen|wg-gesucht|immowelt|immobilienscout|immoscout|noreply|no-reply|info", re.I)
_CONTACT = [re.compile(r"(?:Nachricht von|Absender:)\s+(.{2,60}?)(?:\s+(?:erhalten|zu)\b|:?\s*$)", re.M),
            re.compile(r"^\s*(.{2,60}?)\s+(?:hat\s+(?:Ihnen|dir)|schreibt\s+(?:Ihnen|dir))\b", re.M)]
# с этой строки и ниже — цитата нашего письма или подвал
_CUT = re.compile(r"^\s*(?:--+\s*$|_{3,}|-{2,}\s*(?:Urspr|Original)|Am .{5,80} schrieb|On .{5,80} wrote|Von:\s|"
                  r"(?:Ihre|Deine) (?:Nachricht|Anfrage)\s*:|Diese E-Mail wurde automatisch|Sie erhalten diese E-Mail|"
                  r"Du erhältst diese E-Mail|Bitte antworten Sie nicht|Impressum|Datenschutz)", re.I)


@dataclass
class Reply:
    platform: str
    ext_id: str
    url: str
    contact: str
    excerpt: str
    link: str


def _dec(v) -> str:
    try:
        return str(make_header(decode_header(v or "")))
    except Exception:
        return str(v or "")


def _html_to_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style|head|a)\b.*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</(?:p|div|tr|li|h\d)>", "\n", h)
    return _html.unescape(re.sub(r"<[^>]+>", "", h))


def _bodies(msg: Message) -> tuple[str, str]:
    text, html = [], []
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        s = payload.decode(part.get_content_charset() or "utf-8", "replace")
        (text if ctype == "text/plain" else html).append(s)
    return "\n".join(text), "\n".join(html)


def _platform(msg: Message) -> str | None:
    addr = parseaddr(_dec(msg.get("From")))[1].lower()
    domain = addr.rpartition("@")[2]
    for key, platform in SENDERS.items():
        if domain == key or domain.endswith("." + key):
            return platform
    return None


def _is_intro(s: str) -> bool:
    return bool(_NEW_MSG.search(s)) and len(s) < 120 and s.endswith(":")   # «X hat Ihnen geschrieben:»


def _excerpt(text: str) -> str:
    lines = []
    for ln in text.splitlines():
        if _CUT.match(ln):
            break
        lines.append(ln.strip())
    intro = next((i for i, s in enumerate(lines) if _is_intro(s)), None)
    if intro is not None:   # приветствие и вводная строка площадки — не текст ответа
        lines = lines[intro + 1:]
    elif lines and re.match(r"(?i)^(hallo|guten tag|hi)\b[^.!?]{0,60}[,!]?$", next((s for s in lines if s), "")):
        lines = lines[1 + next(i for i, s in enumerate(lines) if s):]
    out = []
    for i, s in enumerate(lines):
        if not s or s.startswith(">") or "http" in s.lower():
            continue
        if s.endswith(":") and any(lines[i + 1:i + 2]) and "http" in lines[i + 1].lower():   # «Nachricht ansehen:» + ссылка
            continue
        out.append(s)
    return re.sub(r"\s+", " ", " ".join(out)).strip()[:MAX_EXCERPT]


def parse_notification(msg: Message) -> Reply | None:
    platform = _platform(msg)
    if not platform:
        return None
    subject = _dec(msg.get("Subject"))
    text, html = _bodies(msg)
    plain = text if text.strip() else _html_to_text(html)
    if not (_NEW_MSG.search(subject) or _NEW_MSG.search(plain)):
        return None
    urls = [_html.unescape(u).rstrip(".,;") for u in _URL.findall(text + "\n" + html)]
    ext_id = url = ""
    for u in urls:
        m = _AD_URL[platform].match(u)
        if m:
            ext_id, url = m.group(1), m.group(0)
            break
    own = [u for u in urls if any(SENDERS[d] == platform and u.split("/")[2].endswith(d) for d in SENDERS)]
    link = next((u for u in own if _CONV_HINT.search(u) and u != url), "") or next((u for u in own if u != url), "") or url
    contact = ""
    for rx in _CONTACT:
        m = rx.search(plain)
        if m:
            contact = m.group(1).strip(" :,")
            break
    if not contact:
        name = parseaddr(_dec(msg.get("From")))[0].strip()
        contact = "" if _BRANDS.search(name) else name
    excerpt = _excerpt(plain)
    # письмо площадки про новое сообщение не теряем никогда: нет текста/ссылки — подставляем запасные
    return Reply(platform, ext_id, url, contact, excerpt or subject.strip() or NO_TEXT, link or INBOX[platform])


def _fallback(msg: Message) -> Reply | None:
    """Минимальный Reply, если разбор письма площадки упал."""
    platform = _platform(msg)
    if not platform:
        return None
    try:
        subject = _dec(msg.get("Subject")).strip()
        name = parseaddr(_dec(msg.get("From")))[0].strip()
    except Exception:
        subject = name = ""
    return Reply(platform, "", "", "" if _BRANDS.search(name) else name, subject or NO_TEXT, INBOX[platform])


class MailWatcher:
    def __init__(self, user: str, app_password: str, state: dict, imap_factory=imaplib.IMAP4_SSL):
        self.user, self._pw, self.state, self._factory = user, app_password, state, imap_factory

    def _open(self):
        imap = self._factory(HOST, PORT)
        try:
            imap.login(self.user, self._pw)
        except Exception:
            self._close(imap)
            raise
        return imap

    @staticmethod
    def _close(imap) -> None:
        try:
            imap.logout()
        except Exception:
            pass

    @staticmethod
    def _uids(imap, criteria: str) -> list[int]:
        typ, data = imap.uid("SEARCH", None, criteria)
        if typ != "OK":
            raise imaplib.IMAP4.error("поиск писем не удался")
        return [int(x) for x in b" ".join(d for d in data if d).split()]

    def fetch_new(self) -> list[Reply]:
        imap = self._open()
        try:
            typ, _ = imap.select("INBOX", readonly=True)
            if typ != "OK":
                raise imaplib.IMAP4.error("не удалось открыть INBOX")
            last = self.state.get("last_uid")
            if last is None:   # первый запуск: старую почту не разбираем
                self.state["last_uid"] = max(self._uids(imap, "ALL"), default=0)
                return []
            last = int(last)
            out = []
            for uid in sorted(u for u in self._uids(imap, f"UID {last + 1}:*") if u > last):
                r, ok = None, False
                try:
                    typ, data = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
                    raw = next((d[1] for d in data or [] if isinstance(d, tuple)), None) if typ == "OK" else None
                    if raw:
                        msg = email.message_from_bytes(raw)
                        try:
                            r = parse_notification(msg)
                        except Exception:
                            log.exception("mail: письмо uid=%s не разобрано", uid)
                            r = _fallback(msg)
                        ok = True
                    else:
                        log.warning("mail: письмо uid=%s не получено", uid)
                except Exception:   # обрыв связи и т.п.: уже разобранное отдаём, это письмо заберём в следующий раз
                    log.exception("mail: сбой при чтении письма uid=%s", uid)
                if ok:
                    self.state.pop("fail_uid", None)
                    self.state.pop("fail_count", None)
                    if r:
                        out.append(r)
                else:
                    n = self.state.get("fail_count", 0) + 1 if self.state.get("fail_uid") == uid else 1
                    if n < MAX_FAILS:
                        self.state["fail_uid"], self.state["fail_count"] = uid, n
                        break
                    log.warning("mail: письмо uid=%s не читается %d раза подряд, пропущено", uid, n)
                    self.state.pop("fail_uid", None)
                    self.state.pop("fail_count", None)
                self.state["last_uid"] = last = uid
            return out
        finally:
            self._close(imap)

    def check_login(self) -> tuple[bool, str]:
        if not self.user or not self._pw:
            return False, "Укажите адрес Gmail и пароль приложения"
        try:
            imap = self._open()
        except imaplib.IMAP4.error:
            return False, "Неверный адрес или пароль приложения Gmail"
        except OSError as e:
            return False, f"Нет соединения с imap.gmail.com: {e}"
        self._close(imap)
        return True, "Вход в Gmail выполнен"
