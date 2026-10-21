from dataclasses import dataclass

from models import Listing

BONN_PLZ = (53111, 53229)   # почтовые индексы Бонна включительно


@dataclass
class FilterResult:
    ok: bool
    reason: str   # короткая причина на русском для сводки; пустая, если ok
    warm: float | None
    warm_estimated: bool


def estimate_warm(l: Listing) -> tuple[float | None, bool]:
    # warm, если указана; иначе Kalt + Nebenkosten (+ Heizkosten); иначе Kalt + 2,5 €/м² (оценка)
    if l.warm_rent is not None:
        return round(l.warm_rent, 2), False
    if l.cold_rent is None:
        return None, False
    if l.extra_costs is not None:
        return round(l.cold_rent + l.extra_costs + (l.heating_costs or 0), 2), False
    if l.area is not None:
        return round(l.cold_rent + 2.5 * l.area, 2), True
    return None, False


def _in_bonn(plz: str) -> bool:
    # пустой PLZ — объявление считаем боннским: поиск на площадках уже ограничен Бонном
    return not plz or (plz.isdigit() and BONN_PLZ[0] <= int(plz) <= BONN_PLZ[1])


def prefilter(l: Listing, filters: dict) -> str | None:
    """Отсев по данным выдачи, до загрузки объявления: причина (как у check) или None — смотрим подробности."""
    if l.is_shared_flat: return "WG"
    if l.is_swap: return "Tausch"
    price = l.warm_rent if l.warm_rent is not None else l.cold_rent   # тёплая не меньше холодной
    if price is not None and price > filters["max_warm"]: return f"дороже {filters['max_warm']:.0f} €"
    if l.rooms is not None and l.rooms < filters["min_rooms"]: return f"меньше {filters['min_rooms']:.0f} комнат"
    return None


def check(l: Listing, filters: dict, profile: dict) -> FilterResult:
    warm, est = estimate_warm(l)

    def fail(reason: str) -> FilterResult:
        return FilterResult(False, reason, warm, est)

    if not _in_bonn(l.postcode): return fail("не Бонн")
    if l.is_shared_flat: return fail("WG")
    if l.is_swap: return fail("Tausch")
    if l.furnished_temporary: return fail("möbliert auf Zeit")
    if l.wbs_required and not profile.get("wbs"): return fail("нужен WBS")
    if l.limited_months is not None and l.limited_months < filters["min_lease_months"]:
        return fail(f"срок {l.limited_months} мес.")
    if warm is None: return fail("нет цены")
    if warm > filters["max_warm"]: return fail(f"дороже {filters['max_warm']:.0f} €")
    if l.rooms is None: return fail("нет числа комнат")
    if l.rooms < filters["min_rooms"]: return fail(f"меньше {filters['min_rooms']:.0f} комнат")
    return FilterResult(True, "", warm, est)
