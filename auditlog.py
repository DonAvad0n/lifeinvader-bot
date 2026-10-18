from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

log = logging.getLogger("lifeinvader.audit")

HEADER = (
    "Дата и время",
    "Заявка",
    "Решение",
    "Имя в заявке",
    "Статик",
    "Ник на сервере",
    "Discord заявителя",
    "ID заявителя",
    "Обработал",
    "ID обработавшего",
)


class AuditLog:
    def __init__(self, path: Path, tz: ZoneInfo) -> None:
        self.path = path
        self.tz = tz
        self._lock = Lock()

    def append(
        self,
        *,
        application_id: int,
        decision: str,
        name: str,
        static: str,
        nickname: str,
        applicant: str,
        applicant_id: int,
        reviewer: str,
        reviewer_id: int,
    ) -> None:
        row = (
            datetime.now(self.tz).strftime("%d.%m.%Y %H:%M"),
            application_id,
            decision,
            name,
            static,
            nickname,
            applicant,
            applicant_id,
            reviewer,
            reviewer_id,
        )
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                is_new = not self.path.exists()
                with self.path.open("a", newline="", encoding="utf-8") as handle:
                    if is_new:
                        handle.write("\ufeff")
                    writer = csv.writer(handle, delimiter=";")
                    if is_new:
                        writer.writerow(HEADER)
                    writer.writerow(row)
            except OSError as exc:
                log.warning("Не удалось записать журнал приёма: %s", exc)

    def exists(self) -> bool:
        return self.path.is_file() and self.path.stat().st_size > 0
