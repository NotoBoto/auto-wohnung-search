"""
Тексты и разбор ввода для Telegram-бота: чистые функции без сети (обычный текст, без parse_mode).
"""
import re
from typing import Any
from urllib.parse import quote_plus

from models import PROFILE_FIELDS

SKIP = "-"   # ответ «пропустить» для необязательных полей профиля


def _money(v: float | None) -> str:
    return "—" if v is None else f"{v:,.0f} €".replace(",", " ")


def map_line(row) -> str | None:
    """«📍 адрес» и ссылка на Google Maps; без номера дома — честно «примерно». Нет адреса — None."""
    street, plz, district = (getattr(row, k, "") or "" for k in ("street", "postcode", "district"))
    city = f"{plz} Bonn" if plz else ("Bonn" if street else "")
    query = ", ".join(x for x in (street, city) if x) or (f"{district}, Bonn" if district else "")
    if not query:
        return None
    shown = query if street or not district else district
    if district and street:
        shown += f" ({district})"
    approx = "" if re.search(r"\d", street) else " (примерно)"
    return f"📍 {shown}{approx}\n🗺 https://maps.google.com/?q={quote_plus(query)}"


def format_draft(row, text: str, warm_estimated: bool) -> str:
    warm = ("≈ " if warm_estimated else "") + _money(row.warm)
    head = [f"Черновик #{row.id} · {row.platform}", row.title, row.url, map_line(row), f"Тёплая аренда: {warm}"]
    if row.reason:
        head.append(f"⚠ {row.reason}")
    return "\n".join(x for x in head if x) + "\n\n" + text


def format_done(row, mark: str) -> str:
    """Черновик после решения: одна строка-итог со ссылкой вместо полного письма."""
    return f"{mark} · #{row.id} · {row.platform} · {row.title}\n{row.url}"


def format_reply(row, r) -> str:
    if row is None:
        head = ["Ответ пришёл, но я не нашёл, к какому объявлению.", f"Площадка: {r.platform}"]
        if r.contact:
            head.append(f"От: {r.contact}")
    else:
        head = [f"Ответ на объявление #{row.id} · {row.platform}", row.title, row.url, map_line(row)]
    return "\n".join(x for x in head if x) + f"\n\n{r.excerpt}\n\n{r.link}"


def format_digest(stats: dict) -> str:
    lines = ["Сводка за день",
             f"Найдено: {stats.get('found', 0)}", f"Отправлено: {stats.get('sent', 0)}",
             f"Дубли: {stats.get('duplicates', 0)}", f"Ответов: {stats.get('replies', 0)}",
             f"Ошибок: {stats.get('failed', 0)}"]
    filtered = stats.get("filtered") or {}
    if filtered:
        lines.append(f"Отфильтровано: {sum(filtered.values())}")
        lines += [f"  {reason}: {n}" for reason, n in sorted(filtered.items(), key=lambda kv: (-kv[1], kv[0]))]
    return "\n".join(lines)


_INCIDENTS = {
    "captcha": "Капча на {p}. Площадка на паузе. Пройдите капчу в окне браузера, затем /resume {p}.",
    "logged_out": "Выход из аккаунта на {p}. Площадка на паузе. Нужен вход заново в окне браузера, затем /resume {p}.",
    "blocked": "{p} ограничила доступ (блокировка). Площадка на паузе. Проверьте сайт вручную, затем /resume {p}.",
    "broken": "Структура страницы {p} изменилась, разбор не работает. Площадка на паузе, нужна правка программы.",
    "send": "Отправка на {p} прервалась, не ясно, ушло ли письмо. Проверьте отправленные на сайте.",
    "ai": "Нейросеть не ответила, письма не создаются. Проверьте ключ и настройки в программе.",
}


def format_incident(platform: str, kind: str, detail: str) -> str:
    head = _INCIDENTS.get(kind, "Случай {p}: " + kind).format(p=platform)
    return f"{head}\n\n{detail}" if detail else head


def format_status(status: dict) -> str:
    lines = ["Пауза: все площадки" if status.get("paused_all") else
             ("Пауза: " + ", ".join(status["paused"]) if status.get("paused") else "Пауза: нет")]
    lines.append("Режимы: " + ", ".join(f"{p} — {m}" for p, m in status.get("modes", {}).items()))
    inc = status.get("incidents") or []
    lines.append("Случаи: " + (", ".join(f"{p}/{k}" for p, k in inc) if inc else "нет"))
    lines.append(f"Черновиков: {status.get('drafts', 0)}, в очереди: {status.get('queued', 0)}")
    t = status.get("today") or {}
    lines.append(f"Сегодня: найдено {t.get('found', 0)}, отправлено {t.get('sent', 0)}, ответов {t.get('replies', 0)}")
    return "\n".join(lines)


def format_profile(profile: dict) -> str:
    h = profile.get("hidden") or []
    hidden = {x.strip() for x in h.split(",")} if isinstance(h, str) else {str(x).strip() for x in h}
    lines = []
    for k in PROFILE_FIELDS:
        if k == "hidden":
            continue
        v = profile.get(k)
        if v in (None, ""):
            continue
        v = ", ".join(v) if isinstance(v, list) else {True: "да", False: "нет"}.get(v, v) if isinstance(v, bool) else v
        lines.append(f"{k}: {v}" + ("  (скрыто)" if k in hidden else ""))
    return "\n".join(lines) or "Профиль пуст."


# ---------- профиль ----------

PROFILE_STEPS: list[tuple[str, str]] = [
    ("salutation", "Обращение в письме: Herr или Frau?"),
    ("first_name", "Имя (латиницей, как в письме)?"),
    ("last_name", "Фамилия?"),
    ("persons", "Сколько человек будет жить (число)?"),
    ("occupation", f"Род занятий, например «Softwareentwickler» ({SKIP} — пропустить)?"),
    ("net_income", f"Чистый доход в месяц, € ({SKIP} — пропустить)?"),
    ("move_in", "Когда готовы въехать, например «ab sofort» или «01.12.2026»?"),
    ("smoker", f"Курите? да/нет ({SKIP} — пропустить)"),
    ("pets", f"Домашние животные, например «keine» ({SKIP} — пропустить)?"),
    ("wbs", f"Есть WBS? да/нет ({SKIP} — пропустить)"),
    ("documents", f"Какие документы готовы приложить, через запятую ({SKIP} — пропустить)?"),
    ("phone", f"Телефон ({SKIP} — пропустить)?"),
    ("about", "Пара предложений о себе для письма (по-немецки или по-русски, нейросеть напишет письмо сама)?"),
]
_REQUIRED = {"salutation", "first_name", "last_name", "persons", "move_in", "about"}
_YES = {"да", "д", "yes", "y", "ja", "true", "1"}
_NO = {"нет", "н", "no", "n", "nein", "false", "0"}


def parse_profile_answer(field: str, text: str) -> Any:
    """Разбирает ответ владельца. Необязательное поле с «-» даёт None (список — None тоже)."""
    if field not in dict(PROFILE_STEPS):
        raise ValueError(f"Неизвестное поле «{field}». Доступны: {', '.join(f for f, _ in PROFILE_STEPS)}")
    s = (text or "").strip()
    if not s:
        raise ValueError("Пустой ответ.")
    if s == SKIP:
        if field in _REQUIRED:
            raise ValueError("Это поле обязательное, пропустить нельзя.")
        return None
    if field == "salutation":
        v = {"herr": "Herr", "frau": "Frau"}.get(s.lower())
        if v is None:
            raise ValueError("Нужно Herr или Frau.")
        return v
    if field in ("persons", "net_income"):
        digits = re.sub(r"[\s€]", "", s)
        if field == "net_income" and re.fullmatch(r"\d{1,3}([.,]\d{3})+", digits):
            digits = re.sub(r"[.,]", "", digits)   # разделитель тысяч
        if not digits.isdigit() or int(digits) <= 0:
            raise ValueError("Нужно целое число больше нуля, например 2.")
        return int(digits)
    if field in ("smoker", "wbs"):
        if s.lower() in _YES:
            return True
        if s.lower() in _NO:
            return False
        raise ValueError("Ответьте «да» или «нет».")
    if field == "documents":
        items = [x.strip() for x in s.split(",") if x.strip()]
        if not items:
            raise ValueError("Перечислите документы через запятую.")
        return items
    return s


# ---------- фильтры ----------

_FILTER_KEYS = {"max_warm": int, "min_rooms": float, "min_lease_months": int}


def _number(raw: str, kind) -> float | int:
    try:
        v = float(raw.replace(",", "."))
    except ValueError:
        raise ValueError(f"«{raw}» — не число.") from None
    if v < 0:
        raise ValueError("Число не может быть отрицательным.")
    return int(v) if kind is int else v


def parse_filters_args(args: list[str]) -> dict:
    """['max_warm', '950', 'min_rooms', '3'] → {'max_warm': 950, 'min_rooms': 3.0}."""
    usage = "Формат: /filters max_warm 950 min_rooms 3 min_lease_months 12 (ключи: " + ", ".join(_FILTER_KEYS) + ")"
    if not args or len(args) % 2:
        raise ValueError(usage)
    out: dict = {}
    for key, raw in zip(args[::2], args[1::2]):
        if key not in _FILTER_KEYS:
            raise ValueError(f"Неизвестный фильтр «{key}». {usage}")
        out[key] = _number(raw, _FILTER_KEYS[key])
    return out
