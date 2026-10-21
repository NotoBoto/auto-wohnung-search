import pytest

from ai import AIError
from letter import (LetterError, LetterResult, address_form, build_user_message,
                    first_sentence, generate_letter, looks_german)
from models import Listing

PROFILE = {"salutation": "Herr", "first_name": "Max", "last_name": "Muster", "persons": 1,
           "occupation": "Softwareentwickler", "move_in": "01.12.2026",
           "about": "Ich bin ruhig und zuverlässig."}

GOOD_DE_800 = (
    "Sehr geehrte Frau Schmidt,\n\n"
    "Ihre Wohnung in der Poppelsdorfer Allee hat mich sofort angesprochen, weil ich genau diese ruhige Lage "
    "in der Nähe des Botanischen Gartens suche. Ich bin Softwareentwickler, 32 Jahre alt und arbeite seit "
    "vielen Jahren fest angestellt bei einem Unternehmen in Bonn. Ich bin Nichtraucher, habe keine Haustiere "
    "und lege großen Wert auf ein gutes Verhältnis zu den Nachbarn. Die Wohnung wäre für mich allein ideal, "
    "und ich würde sie gerne langfristig mieten. Alle Unterlagen, also Schufa-Auskunft, Gehaltsnachweise und "
    "eine Mietschuldenfreiheitsbescheinigung, habe ich bereits vorbereitet und kann sie Ihnen sofort zusenden. "
    "Einziehen könnte ich zum nächsten Ersten oder nach Absprache auch später. Über eine Einladung zu einem "
    "Besichtigungstermin würde ich mich sehr freuen, und ich richte mich gerne nach Ihrem Zeitplan. "
    "Vielen Dank für Ihre Zeit und freundliche Grüße\n\nMax Muster"
)
OTHER_DE_800 = (
    "Guten Tag Frau Schmidt,\n\n"
    "Ein Balkon mit Blick ins Grüne und die Straßenbahn direkt vor der Tür, das passt genau zu meinem "
    "Alltag. Ich heiße Max Muster, bin Softwareentwickler "
    "und seit Jahren unbefristet bei einer Firma in Bonn beschäftigt. Ich rauche nicht, habe keine Tiere und "
    "bin ein ruhiger, ordentlicher Mieter, der pünktlich zahlt. Meine Unterlagen sind vollständig und liegen "
    "bereit: Schufa, Einkommensnachweise und eine Bescheinigung des bisherigen Vermieters. Gerne würde ich "
    "die Wohnung zum ersten Dezember beziehen, ein späterer Termin ist für mich aber ebenfalls kein Problem. "
    "Falls die Wohnung noch frei ist, bitte ich Sie um einen Termin für eine Besichtigung, den ich nach "
    "Ihren Wünschen wahrnehmen werde. Ich danke Ihnen für Ihre Mühe und verbleibe mit freundlichen Grüßen\n\n"
    "Max Muster"
)
EN_800 = (
    "Dear Mrs Schmidt,\n\n"
    "I am writing to you because the apartment on Poppelsdorfer Allee would be a perfect fit and I am sure "
    "that you will find me to be a reliable tenant. I am a software developer with a permanent contract and "
    "I would like to rent the apartment for a long time. I am a non-smoker and I have no pets, and the "
    "neighbours would not even notice me. All the documents are ready and I can send them to you at once, "
    "and the move would be possible with the start of next month. I would be glad to see the apartment with "
    "you at a time that suits you, and I thank you for the time you take to read this letter. "
    "Kind regards and thank you again for your help with the apartment\n\nMax Muster"
)


def L(**kw) -> Listing:
    return Listing(platform="kleinanzeigen", ext_id="1", url="u", title="Schöne 2-Zimmer-Wohnung",
                   street="Poppelsdorfer Allee 1", postcode="53115", district="Poppelsdorf",
                   cold_rent=700, warm_rent=900, rooms=2, area=55, contact_name="Frau Schmidt", **kw)


class FakeAI:
    def __init__(self, answers):
        self.answers = list(answers); self.calls = []

    async def generate(self, system, user):
        self.calls.append((system, user))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def test_fixtures_sane():
    for t in (GOOD_DE_800, OTHER_DE_800, EN_800):
        assert 600 <= len(t.strip()) <= 1200
    assert looks_german(GOOD_DE_800) and looks_german(OTHER_DE_800) and not looks_german(EN_800)
    assert first_sentence(GOOD_DE_800) != first_sentence(OTHER_DE_800)


async def test_skip():
    r = await generate_letter(FakeAI(["SKIP: это WG"]), "p", L(), PROFILE)
    assert r == LetterResult(None, "это WG")


async def test_skip_without_reason_no_retry():
    ai = FakeAI(["skip"])
    r = await generate_letter(ai, "p", L(), PROFILE)
    assert r == LetterResult(None, "") and len(ai.calls) == 1


async def test_ok_first_try_uses_prompt_as_system():
    ai = FakeAI([GOOD_DE_800])
    r = await generate_letter(ai, "SYS", L(), PROFILE)
    assert r.text == GOOD_DE_800 and r.skip_reason is None
    assert ai.calls[0][0] == "SYS"


async def test_retry_on_short_then_ok():
    ai = FakeAI(["Hallo", GOOD_DE_800])
    assert (await generate_letter(ai, "p", L(), PROFILE)).text == GOOD_DE_800
    assert "600" in ai.calls[1][1]


async def test_short_twice_raises_with_length():
    with pytest.raises(LetterError, match=r"длина 5 знаков \(нужно 600–1200\)"):
        await generate_letter(FakeAI(["Hallo", "Hallo"]), "p", L(), PROFILE)


async def test_english_twice_raises():
    with pytest.raises(LetterError, match="не на немецком"):
        await generate_letter(FakeAI([EN_800, EN_800]), "p", L(), PROFILE)


async def test_repeated_opening_retried():
    ai = FakeAI([GOOD_DE_800, OTHER_DE_800])
    r = await generate_letter(ai, "p", L(), PROFILE, avoid_openings=[first_sentence(GOOD_DE_800)])
    assert r.text == OTHER_DE_800


async def test_repeated_opening_twice_raises():
    with pytest.raises(LetterError, match="повторяет начало прежнего письма"):
        await generate_letter(FakeAI([GOOD_DE_800, GOOD_DE_800]), "p", L(), PROFILE,
                              avoid_openings=[first_sentence(GOOD_DE_800).upper()])


async def test_combined_reasons_single_retry():
    ai = FakeAI(["Hallo", "Hallo"])
    with pytest.raises(LetterError) as e:
        await generate_letter(ai, "p", L(), PROFILE)
    assert len(ai.calls) == 2
    assert "длина" in str(e.value) and "не на немецком" in str(e.value) and "; " in str(e.value)


async def test_skip_on_retry_is_honoured():
    r = await generate_letter(FakeAI(["Hallo", "SKIP: WG"]), "p", L(), PROFILE)
    assert r == LetterResult(None, "WG")


async def test_ai_error_propagates():
    with pytest.raises(AIError):
        await generate_letter(FakeAI([AIError("boom")]), "p", L(), PROFILE)


def test_first_sentence():
    assert first_sentence("Sehr geehrte Frau Schmidt,\n\nIch suche z. B. eine Wohnung. Danke.") == \
        "Ich suche z. B. eine Wohnung."
    assert first_sentence("Hallo!\nDie Wohnung ca. 55 m² gefällt mir. Weiter") == "Die Wohnung ca. 55 m² gefällt mir."
    assert first_sentence("Nur ein Satz") == "Nur ein Satz"


def test_hidden_profile_fields_not_in_prompt():
    msg = build_user_message(L(), {**PROFILE, "net_income": "3200", "hidden": ["net_income"]}, "Sie")
    assert "3200" not in msg
    assert "Softwareentwickler" in msg


def test_build_user_message_contents():
    msg = build_user_message(L(description="x" * 5000), PROFILE, "du", wish="Erwähne das Haustier.",
                             avoid_openings=["Ihre Wohnung hat mich angesprochen."])
    assert "Schöne 2-Zimmer-Wohnung" in msg and "Poppelsdorf" in msg and "Frau Schmidt" in msg
    assert "du" in msg and "Erwähne das Haustier." in msg
    assert "Ihre Wohnung hat mich angesprochen." in msg
    assert "x" * 3000 in msg and "x" * 3001 not in msg


def test_address_form():
    assert address_form(L(description="Hast du Lust? Melde dich gerne bei mir, dann zeige ich dir alles.")) == "du"
    assert address_form(L(description="Wir freuen uns auf Ihre Anfrage. Bitte senden Sie uns Ihre Unterlagen.")) == "Sie"
    assert address_form(L(description="")) == "Sie"


def test_hidden_as_string_hides_fields():
    msg = build_user_message(L(), {**PROFILE, "net_income": "3200", "phone": "0170", "hidden": "net_income, phone"}, "Sie")
    assert "3200" not in msg and "0170" not in msg


async def test_skizze_is_not_skip():
    r = await generate_letter(FakeAI(["Skizze " + GOOD_DE_800]), "p", L(), PROFILE)
    assert r.skip_reason is None and r.text.startswith("Skizze")


async def test_length_wish_says_too_short():
    ai = FakeAI(["Hallo", GOOD_DE_800])
    await generate_letter(ai, "p", L(), PROFILE)
    assert "zu kurz (5 Zeichen)" in ai.calls[1][1] and "Entwurf" not in ai.calls[1][1]


def test_example_prompt_mentions_documents_only_from_profile():
    import core
    text = (core.RES / "prompt.example.txt").read_text(encoding="utf-8")
    line = next(s for s in text.splitlines() if "Unterlagen" in s)
    assert "documents" in line   # документы — только если они есть в профиле, иначе это выдумка
