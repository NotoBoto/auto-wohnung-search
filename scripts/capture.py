"""Сохранить страницу площадки через Browser: python scripts/capture.py <platform> <url> <out.html> [профиль-папка]
Профиль браузера по умолчанию во временной папке, не в папке данных программы."""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from browser import Browser, human_pause  # noqa: E402


async def capture(platform: str, url: str, out: Path, base: Path, accept=None) -> str:
    br = Browser(base)
    try:
        async with br.lock(platform):
            page = await br.page(platform)
            await page.goto(url)
            await page.wait_for_load_state("load")
            await human_pause(2, 4)
            if accept:
                await accept(page)
            html = await page.content()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(html, encoding="utf-8")
            return page.url
    finally:
        await br.close()


def main(argv: list[str]) -> None:
    if len(argv) < 4:
        sys.exit(__doc__)
    platform, url, out = argv[1], argv[2], Path(argv[3])
    base = Path(argv[4]) if len(argv) > 4 else Path(tempfile.gettempdir()) / "aws_capture"
    accept = None
    if platform == "kleinanzeigen":
        from sources.kleinanzeigen import accept_consent as accept
    print(asyncio.run(capture(platform, url, out, base, accept)))


if __name__ == "__main__":
    main(sys.argv)
