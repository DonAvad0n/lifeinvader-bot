from __future__ import annotations

import asyncio
import csv
import io
import re
import time
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote

import aiohttp

WEEKDAYS: tuple[str, ...] = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)

EXPORT_BY_GID = "https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
EXPORT_BY_NAME = "https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&sheet={name}"

TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
DATE_RE = re.compile(r"^(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?$")
EMPTY_MARKERS = {"-", "—", "–", "−", "нет", "н/д", "/", "\\"}


class SheetError(RuntimeError):
    pass


class NoDayError(SheetError):
    pass


@dataclass(frozen=True)
class Slot:
    time: str
    title: str


@dataclass(frozen=True)
class DayGrid:
    day: date
    weekday: str
    slots: tuple[Slot, ...]


def _clean(value: str) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


def parse_cell_date(cell: str, near: date) -> date | None:
    match = DATE_RE.match(_clean(cell))
    if not match:
        return None
    day_number = int(match.group(1))
    month_number = int(match.group(2))
    if match.group(3):
        year = int(match.group(3))
        if year < 100:
            year += 2000
        try:
            return date(year, month_number, day_number)
        except ValueError:
            return None
    best: date | None = None
    for year in (near.year - 1, near.year, near.year + 1):
        try:
            candidate = date(year, month_number, day_number)
        except ValueError:
            continue
        if best is None or abs(candidate - near) < abs(best - near):
            best = candidate
    return best


def parse_day(csv_text: str, target: date) -> DayGrid | None:
    rows = list(csv.reader(io.StringIO(csv_text)))
    if len(rows) < 3:
        return None

    header = rows[0]
    column = None
    for index, cell in enumerate(header):
        if parse_cell_date(cell, target) == target:
            column = index
            break
    if column is None:
        return None

    weekday_row = rows[1] if len(rows) > 1 else []
    weekday_cell = _clean(weekday_row[column]) if column < len(weekday_row) else ""
    weekday = weekday_cell or WEEKDAYS[target.weekday()]

    slots: list[Slot] = []
    for row in rows[2:]:
        if not row:
            continue
        time_cell = _clean(row[0])
        if not TIME_RE.fullmatch(time_cell):
            continue
        title = _clean(row[column]) if column < len(row) else ""
        if not title or title in EMPTY_MARKERS:
            continue
        slots.append(Slot(time=time_cell, title=title))

    if not slots:
        return None
    return DayGrid(day=target, weekday=weekday, slots=tuple(slots))


def available_dates(csv_text: str, near: date) -> list[date]:
    rows = list(csv.reader(io.StringIO(csv_text)))
    if not rows:
        return []
    result: list[date] = []
    for cell in rows[0]:
        parsed = parse_cell_date(cell, near)
        if parsed is not None and parsed not in result:
            result.append(parsed)
    return result


async def download_csv(
    session: aiohttp.ClientSession,
    sheet_id: str,
    gid: str,
    sheet_name: str,
) -> str:
    urls = [
        EXPORT_BY_GID.format(sheet_id=sheet_id, gid=gid),
        EXPORT_BY_NAME.format(sheet_id=sheet_id, name=quote(sheet_name)),
    ]
    last_error: Exception | None = None
    for url in urls:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as response:
                if response.status != 200:
                    last_error = SheetError(f"Google ответил HTTP {response.status}")
                    continue
                text = await response.text()
                if not text.strip():
                    last_error = SheetError("Google вернул пустой ответ")
                    continue
                if text.lstrip()[:1] == "<":
                    last_error = SheetError(
                        "Вместо данных пришла HTML-страница: проверьте, что доступ по ссылке открыт"
                    )
                    continue
                return text
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = SheetError(f"Сеть недоступна: {exc}")
    raise last_error or SheetError("Не удалось скачать таблицу")


class SheetCache:
    def __init__(self, sheet_id: str, gid: str, sheet_name: str, ttl: int = 300) -> None:
        self._sheet_id = sheet_id
        self._gid = gid
        self._sheet_name = sheet_name
        self._ttl = ttl
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._days: dict[date, tuple[DayGrid, float]] = {}
        self._last_text: str | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def raw(self, force: bool = False) -> str:
        async with self._lock:
            if self._last_text is not None and not force:
                return self._last_text
            text = await download_csv(
                await self._get_session(), self._sheet_id, self._gid, self._sheet_name
            )
            self._last_text = text
            return text

    async def day(self, target: date, force: bool = False) -> DayGrid:
        async with self._lock:
            cached = self._days.get(target)
            if cached is not None and not force and time.monotonic() - cached[1] < self._ttl:
                return cached[0]
            text = await download_csv(
                await self._get_session(), self._sheet_id, self._gid, self._sheet_name
            )
            self._last_text = text
            grid = parse_day(text, target)
            if grid is None:
                dates = available_dates(text, target)
                window = ""
                if dates:
                    window = f" В таблице есть даты {dates[0]:%d.%m}–{dates[-1]:%d.%m}."
                raise NoDayError(
                    f"На {target:%d.%m.%Y} в листе «{self._sheet_name}» нет колонки с эфирами.{window}"
                )
            self._days = {target: (grid, time.monotonic())}
            return grid

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
