import pytest

from models import parse_area, parse_lease_months, parse_money, parse_rooms, profile_complete


@pytest.mark.parametrize("s,v", [("1.250,00 €", 1250.0), ("850 € warm", 850.0), ("650€", 650.0),
                                 ("ca. 720,50 EUR", 720.5), ("VB", None), ("", None)])
def test_parse_money(s, v): assert parse_money(s) == v

@pytest.mark.parametrize("s,v", [("2 Zimmer", 2.0), ("2,5 Zi.", 2.5), ("3", 3.0), ("Zimmer: k.A.", None)])
def test_parse_rooms(s, v): assert parse_rooms(s) == v

@pytest.mark.parametrize("s,v", [("ca. 65 m²", 65.0), ("58,4 qm", 58.4), ("", None)])
def test_parse_area(s, v): assert parse_area(s) == v

@pytest.mark.parametrize("t,v", [("befristet auf 6 Monate", 6), ("Zwischenmiete für 3 Monate", 3),
                                 ("befristet bis 1 Jahr", 12), ("unbefristet", None),
                                 ("Mietdauer: 2 Jahre", 24)])
def test_parse_lease_months(t, v): assert parse_lease_months(t) == v

@pytest.mark.parametrize("t", ["unbefristet, Kündigungsfrist 3 Monate", "3 Monatsmieten Kaution",
                               "seit 2 Jahren vermietet"])
def test_parse_lease_months_no_false_positive(t): assert parse_lease_months(t) is None

def test_profile_complete():
    p = {"salutation": "Herr", "first_name": "A", "last_name": "B", "persons": 2, "move_in": "01.01.2027", "about": "x"}
    assert profile_complete(p) and not profile_complete({**p, "about": ""})


def test_parse_address():
    from models import parse_address
    assert parse_address("Musterstraße 5a, 53111 Bonn") == ("Musterstraße", "5a", "53111", "Bonn")
    assert parse_address("Am Hof 12 - 14, 53113 Bonn") == ("Am Hof", "12-14", "53113", "Bonn")
    assert parse_address("Bonn") is None and parse_address("") is None
