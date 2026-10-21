import logging

import pytest

import core
from ai import AIClient, AIError, human_typography


def make(backend: str) -> AIClient:
    return AIClient({**core.DEFAULTS, "backend": backend}, logging.getLogger("t"))


async def test_generate_dispatches_by_backend(monkeypatch):
    c = make("openai_compat")

    async def fake(system, user):
        return "Hallo — Welt…"
    monkeypatch.setattr(c, "_gen_compat", fake)
    assert await c.generate("s", "u") == "Hallo - Welt..."


async def test_generate_wraps_errors(monkeypatch):
    c = make("zai")

    async def boom(system, user):
        raise RuntimeError("Ключ Z.ai не подходит")
    monkeypatch.setattr(c, "_gen_zai", boom)
    with pytest.raises(AIError, match="Ключ Z.ai"):
        await c.generate("s", "u")


async def test_generate_strips_reasoning(monkeypatch):
    c = make("openai_compat")

    async def fake(system, user):
        return "<think>hm</think>Guten Tag"
    monkeypatch.setattr(c, "_gen_compat", fake)
    assert await c.generate("s", "u") == "Guten Tag"


async def test_unfinished_reasoning_is_aierror(monkeypatch):
    c = make("openai_compat")

    async def fake(system, user):
        return "<think>endlos"
    monkeypatch.setattr(c, "_gen_compat", fake)
    with pytest.raises(AIError, match="размышлять"):
        await c.generate("s", "u")


async def test_error_text_is_human_readable(monkeypatch):
    c = make("api")

    async def boom(system, user):
        raise RuntimeError("Нет связи")
    monkeypatch.setattr(c, "_gen_api", boom)
    with pytest.raises(AIError) as e:
        await c.generate("s", "u")
    assert str(e.value).startswith("Ошибка Claude API: Нет связи")


async def test_requests_are_serialized(monkeypatch):
    import asyncio
    c = make("openai_compat")
    running, peak = 0, 0

    async def fake(system, user):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return "x"
    monkeypatch.setattr(c, "_gen_compat", fake)
    await asyncio.gather(*(c.generate("s", "u") for _ in range(4)))
    assert peak == 1


def test_human_typography():
    assert human_typography("«A» – **b**") == '"A" - b'
