import re
from dataclasses import dataclass, field

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
                  "phone", "address", "about", "hidden")   # hidden: поля, которые не раскрывать в первом письме
FORM_ONLY_FIELDS = ("address",)   # только для полей формы площадки, в текст письма не идут
PROFILE_REQUIRED = ("salutation", "first_name", "last_name", "persons", "move_in", "about")


def profile_complete(profile: dict) -> bool:
    # 0, None и пустая строка считаются незаполненным значением; остальные значения засчитываются
    return all(profile.get(k) not in (None, "", 0) for k in PROFILE_REQUIRED)


_ADDRESS_RE = re.compile(r"^\s*(.+?)\s+(\d+\s*[a-zA-Z]?(?:\s*[-/]\s*\d+\s*[a-zA-Z]?)?)\s*,\s*(\d{5})\s+(.+?)\s*$")


def parse_address(s: str) -> tuple[str, str, str, str] | None:
    """«Musterstraße 5a, 53111 Bonn» → (улица, дом, индекс, город); None, если адрес не в этом виде."""
    m = _ADDRESS_RE.match(s or "")
    return tuple(re.sub(r"\s+", "", g) if i == 1 else g.strip() for i, g in enumerate(m.groups())) if m else None


_MONEY_RE = re.compile(r"\d+(?:\.\d{3})*(?:,\d+)?")   # точка — разделитель тысяч только перед тремя цифрами
_DECIMAL_RE = re.compile(r"\d+(?:[.,]\d+)?")
_LEASE_RE = re.compile(r"(\d+)\s*(Monate|Monaten|Monat|Jahre|Jahren|Jahr)\b", re.IGNORECASE)
_LEASE_KEYWORD_RE = re.compile(r"befristet|zwischenmiete|auf zeit|für|mietdauer|laufzeit|bis zu", re.IGNORECASE)
_LEASE_EXCLUDE_RE = re.compile(r"kündigungsfrist|kaution", re.IGNORECASE)


def _first_number(s: str, pattern: re.Pattern) -> str | None:
    m = pattern.search(s or "")
    return m.group(0) if m else None


def parse_money(s: str) -> float | None:
    # «VB» (Verhandlungsbasis) и пустая строка — не число
    m = _first_number(s, _MONEY_RE)
    return float(m.replace(".", "").replace(",", ".")) if m else None


def parse_rooms(s: str) -> float | None:
    m = _first_number(s, _DECIMAL_RE)
    return float(m.replace(",", ".")) if m else None


def parse_area(s: str) -> float | None:
    m = _first_number(s, _DECIMAL_RE)
    return float(m.replace(",", ".")) if m else None


def parse_lease_months(text: str) -> int | None:
    text = text or ""
    if re.search(r"unbefristet", text, re.IGNORECASE):
        return None
    for m in _LEASE_RE.finditer(text):
        # срок учитываем только если рядом есть ключевое слово аренды; Kündigungsfrist и Kaution — не срок аренды
        window = text[max(0, m.start() - 40):m.start()]
        if _LEASE_EXCLUDE_RE.search(window) or not _LEASE_KEYWORD_RE.search(window):
            continue
        n = int(m.group(1))
        return n if m.group(2).lower().startswith("monat") else 12 * n
    return None
