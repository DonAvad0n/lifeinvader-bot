from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp

import main as B
import config
from sheet import available_dates, download_csv, parse_day
from store import Store


async def main() -> int:
    tz = ZoneInfo(config.TIMEZONE)
    now = datetime.now(tz)
    today = now.date()

    async with aiohttp.ClientSession() as session:
        text = await download_csv(
            session, config.SHEET_ID, config.SHEET_GID, config.SHEET_NAME
        )

    dates = available_dates(text, today)
    print(f"Часовой пояс: {config.TIMEZONE}, сейчас {now:%d.%m.%Y %H:%M}")
    print(f"Дат в шапке листа: {len(dates)} — {dates[0]:%d.%m}..{dates[-1]:%d.%m}")

    grid = parse_day(text, today)
    if grid is None:
        probe = next((d for d in dates if d >= today), dates[-1] if dates else None)
        print(f"На {today:%d.%m.%Y} колонки нет, проверяю {probe:%d.%m.%Y}")
        grid = parse_day(text, probe) if probe else None
    if grid is None:
        print("Не удалось разобрать ни один день")
        return 1

    total = len(grid.slots)
    print(f"\n{grid.day:%d.%m.%Y} ({grid.weekday}): непустых слотов {total}")
    for index, slot in enumerate(grid.slots):
        print(f"  #{index + 1:<2} {slot.time} · {B.shorten(slot.title, 90)}")

    taken: set[int] = {2, 5, 17}
    page = B.align_page(total, {i: None for i in taken})  # type: ignore[dict-item]
    print(
        f"\nЗаняты {sorted(i + 1 for i in taken)} -> страница {page + 1}"
        f" из {B.pages_count(total)} (кнопки {page * B.SLOTS_PER_PAGE + 1}"
        f"–{min((page + 1) * B.SLOTS_PER_PAGE, total)})"
    )
    embed = B.describe(grid, {i: None for i in taken}, page, now, tz)  # type: ignore[dict-item]
    print("\n--- текст сообщения ---")
    print(embed.description)
    print("---")
    print("FOOTER:", embed.footer.text)

    service = B.EfiryService(Store(":memory:"), None, tz)  # type: ignore[arg-type]
    view = B.PanelView(service, page=page, grid=grid, claims={})  # type: ignore[arg-type]
    rows: dict[int, int] = {}
    for child in view.children:
        rows[child.row] = rows.get(child.row, 0) + 1
    print(f"\nКнопок всего: {len(view.children)} (лимит Discord — 25), по рядам: {rows}")

    print("\nПроверка напоминаний (за 5 минут до начала):")
    probe_time = datetime(grid.day.year, grid.day.month, grid.day.day, 13, 0, tzinfo=tz)
    for minutes in (10, 6, 5, 3, 1):
        moment = probe_time - timedelta(minutes=minutes)
        hits = []
        for index, slot in enumerate(grid.slots):
            start = B.slot_start(grid.day, slot.time, tz)
            if start is None:
                continue
            seconds = (start - moment).total_seconds()
            if 0 < seconds <= config.REMINDER_LEAD_SECONDS:
                hits.append(f"#{index + 1} {slot.time}")
        print(f"  если сейчас {moment:%H:%M} -> тег за: {hits or 'ничего'}")

    store = Store(config.DB_PATH)
    day = date(2026, 10, 18)
    store.add_claim(1, day, 4, 42, "Тест")
    assert store.claims(1, day)[4].user_name == "Тест"
    assert not store.notice_sent(1, day, 4)
    store.mark_notice(1, day, 4)
    assert store.notice_sent(1, day, 4)
    store.remove_claim(1, day, 4)
    store.clear_day(1, day)
    assert not store.notice_sent(1, day, 4)
    store.close()
    print("\nSQLite: отметки занятости и напоминания работают")

    from store import Claim

    demo = {
        0: Claim(101, "Kim", "x"),
        4: Claim(101, "Kim", "x"),
        7: Claim(202, "Alex", "x"),
    }
    service = B.EfiryService(Store(":memory:"), None, tz)  # type: ignore[arg-type]
    print(f"\n--- итог дня (публикуется в {config.SUMMARY_HOUR:02d}:{config.SUMMARY_MINUTE:02d}) ---")
    print(service.summary_text(grid.day, grid, demo))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(asyncio.run(main()))
