"""
Письмо арендодателю: сборка запроса к нейросети, проверки результата (длина, язык, повтор начала)
и одна повторная попытка.
"""
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from ai import AIError  # noqa: F401  (пробрасывается из generate как есть)
from models import FORM_ONLY_FIELDS, Listing

MIN_LEN, MAX_LEN = 600, 1200
DESCRIPTION_LIMIT = 3000


@dataclass
class LetterResult:
    text: str | None
    skip_reason: str | None


class LetterError(RuntimeError):
    pass


_DU_RE = re.compile(r"\b(?:[Dd]u|[Dd]ich|[Dd]ir|[Dd]ein\w*|[Ee]uch|ihr)\b")
_SIE_RE = re.compile(r"\b(?:Sie|Ihnen|Ihr\w*)\b")


def address_form(l: Listing) -> Literal["Sie", "du"]:
    text = l.description or ""
    du = len(_DU_RE.findall(text))
    # «Sie/Ihr…» с заглавной в начале предложения неотличимо от «sie/ihr» — такие не считаем
    sie = sum(1 for m in _SIE_RE.finditer(text)
              if re.search(r"(?:^|[.!?]\s+|\n)\s*$", text[:m.start()]) is None)
    return "du" if du > sie else "Sie"


_GREETING_RE = re.compile(r"^\s*(?:Sehr geehrte|Hallo|Guten Tag|Liebe|Lieber|Hi)\b[^\n]*[,!]\s*$", re.IGNORECASE)
_ABBR_RE = re.compile(r"z\.\s?B\.|\bDr\.|\bca\.|\bNr\.|\bbzw\.|\bevtl\.|\binkl\.", re.IGNORECASE)


def first_sentence(text: str) -> str:
    lines = (text or "").strip().splitlines()
    i = 0
    while i < len(lines) and (not lines[i].strip() or _GREETING_RE.match(lines[i])):
        i += 1
    body = "\n".join(lines[i:]).strip()
    protected = [m.span() for m in _ABBR_RE.finditer(body)]
    for m in re.finditer(r"[.!?](?=\s|$)", body):
        if any(a <= m.start() < b for a, b in protected):
            continue
        body = body[:m.end()]
        break
    return re.sub(r"\s+", " ", body).strip()


_GERMAN_WORDS = {"und", "ich", "die", "der", "das", "nicht", "mit", "für", "wohnung", "gerne",
                 "ihnen", "ist", "eine", "bin", "wir"}
_ENGLISH_WORDS = {"the", "and", "i", "am", "apartment", "with", "would", "you"}


def looks_german(text: str) -> bool:
    words = re.findall(r"\w+", (text or "").lower())
    de = sum(w in _GERMAN_WORDS for w in words)
    en = sum(w in _ENGLISH_WORDS for w in words)
    return de >= 5 and de > 2 * en


def _fmt(v) -> str:
    return "" if v is None else (str(int(v)) if float(v) == int(v) else str(v))


def build_user_message(l: Listing, profile: dict, form: str, wish: str = "",
                       avoid_openings: Sequence[str] = ()) -> str:
    h = profile.get("hidden") or []
    hidden = {x.strip() for x in h.split(",")} if isinstance(h, str) else {str(x).strip() for x in h}
    lines = [f"Titel: {l.title}",
             f"Adresse: {' '.join(p for p in (l.street, l.postcode, l.district) if p)}"]
    for label, v in (("Kaltmiete", l.cold_rent), ("Nebenkosten", l.extra_costs),
                     ("Heizkosten", l.heating_costs), ("Warmmiete", l.warm_rent),
                     ("Zimmer", l.rooms), ("Fläche (m²)", l.area)):
        if v is not None:
            lines.append(f"{label}: {_fmt(v)}")
    lines += [f"Einzug ab: {l.available_from}", f"Ansprechpartner: {l.contact_name}",
              f"Anrede-Form: {form}", "", "Beschreibung:", (l.description or "")[:DESCRIPTION_LIMIT], "",
              "Profil des Mieters:"]
    lines += [f"{k}: {v}" for k, v in profile.items()
              if k != "hidden" and k not in hidden and k not in FORM_ONLY_FIELDS and v not in (None, "")]
    if wish:
        lines += ["", f"Wunsch: {wish}"]
    if avoid_openings:
        lines += ["", "Diese ersten Sätze NICHT verwenden:"] + [f"- {s}" for s in avoid_openings]
    return "\n".join(lines)


def _skip_reason(answer: str) -> str | None:
    m = re.match(r"\s*SKIP\b", answer, re.IGNORECASE)
    return answer[m.end():].lstrip().lstrip(":").strip() if m else None


def _problems(text: str, avoid_openings: Sequence[str]) -> list[tuple[str, str]]:
    """[(причина по-русски, инструкция по-немецки)]"""
    out = []
    n = len(text)
    if not MIN_LEN <= n <= MAX_LEN:
        how = "kurz" if n < MIN_LEN else "lang"
        out.append((f"длина {n} знаков (нужно {MIN_LEN}–{MAX_LEN})",
                    f"Der Text war zu {how} ({n} Zeichen) – schreibe 800–1000 Zeichen (nie unter {MIN_LEN} oder über {MAX_LEN})."))
    if not looks_german(text):
        out.append(("не на немецком", "Schreibe den Brief ausschließlich auf Deutsch."))
    opening = first_sentence(text).casefold()
    if opening and opening in {first_sentence(s).casefold() or s.strip().casefold() for s in avoid_openings}:
        out.append(("повторяет начало прежнего письма",
                    "Beginne mit einem völlig anderen ersten Satz (siehe die verbotenen ersten Sätze)."))
    return out


async def generate_letter(ai, prompt: str, l: Listing, profile: dict, wish: str = "",
                          avoid_openings: Sequence[str] = ()) -> LetterResult:
    form = address_form(l)
    answer = await ai.generate(prompt, build_user_message(l, profile, form, wish, avoid_openings))
    for attempt in (1, 2):
        reason = _skip_reason(answer)
        if reason is not None:
            return LetterResult(None, reason)
        text = answer.strip()
        problems = _problems(text, avoid_openings)
        if not problems:
            return LetterResult(text, None)
        if attempt == 2:
            raise LetterError("; ".join(r for r, _ in problems))
        retry_wish = "\n".join(filter(None, [wish] + [i for _, i in problems]))
        answer = await ai.generate(prompt, build_user_message(l, profile, form, retry_wish, avoid_openings))
