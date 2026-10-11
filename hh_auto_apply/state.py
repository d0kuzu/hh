import sqlite3
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from filelock import FileLock

from .models import StopRun, Vacancy


class StateStore:
    """Резервируем попытку ДО отправки; неопределённый исход не повторяем."""

    def __init__(self, path: Path, timezone: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.zone = ZoneInfo(timezone)
        self.applied_ids_path = path.parent / "applied_vacancy_ids.txt"
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS attempts (
                vacancy_id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL,
                day TEXT NOT NULL, status TEXT NOT NULL, letter TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS drafts (
                id INTEGER PRIMARY KEY, vacancy_id TEXT NOT NULL, url TEXT NOT NULL,
                letter TEXT NOT NULL, created_at TEXT NOT NULL
            );
        """)
        self.sync_applied_ids()
        self.sync_unresolved()

    def read_applied_ids(self) -> set[str]:
        if not self.applied_ids_path.exists():
            return set()
        ids = set()
        for line in self.applied_ids_path.read_text(encoding="utf-8-sig").splitlines():
            value = line.strip()
            if not value:
                continue
            if not re.fullmatch(r"[0-9]+", value):
                raise StopRun("В applied_vacancy_ids.txt ожидаются только ID вакансий, по одному в строке")
            ids.add(value)
        return ids

    def sync_applied_ids(self, extra_ids=()):
        """Объединяем файл и подтверждённые отклики БД; замена файла атомарна."""
        with FileLock(str(self.applied_ids_path) + ".lock", timeout=10):
            ids = self.read_applied_ids()
            ids.update(extra_ids)
            ids.update(row[0] for row in self.db.execute("SELECT vacancy_id FROM attempts WHERE status='sent'"))
            text = "".join(value + "\n" for value in sorted(ids, key=int))
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                                 dir=self.applied_ids_path.parent, delete=False) as out:
                    temporary = Path(out.name)
                    out.write(text)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temporary, self.applied_ids_path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def now(self):
        return datetime.now(self.zone)

    def status(self, vacancy_id: str) -> str | None:
        row = self.db.execute("SELECT status FROM attempts WHERE vacancy_id=?", (vacancy_id,)).fetchone()
        return row[0] if row else "sent" if vacancy_id in self.read_applied_ids() else None

    def count_today(self) -> int:
        return self.db.execute("SELECT count(*) FROM attempts WHERE day=?",
                               (self.now().date().isoformat(),)).fetchone()[0]

    def reserve(self, vacancy: Vacancy, letter: str, daily_limit: int):
        # BEGIN IMMEDIATE также защищает лимит при нескольких соединениях.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.status(vacancy.id):
                raise StopRun("Повторная попытка отправки заблокирована историей")
            now = self.now()
            count = self.db.execute("SELECT count(*) FROM attempts WHERE day=?",
                                    (now.date().isoformat(),)).fetchone()[0]
            if count >= daily_limit:
                raise StopRun("Достигнут суточный лимит откликов")
            self.db.execute("INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (vacancy.id, vacancy.url, vacancy.title, now.date().isoformat(),
                             "pending", letter, now.isoformat()))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def finish(self, vacancy_id: str, status: str):
        if status not in {"sent", "unknown"}:
            raise ValueError("Недопустимый статус отправки")
        with self.db:
            self.db.execute("UPDATE attempts SET status=?, updated_at=? WHERE vacancy_id=?",
                            (status, self.now().isoformat(), vacancy_id))
        if status == "sent":
            self.sync_applied_ids()
        self.sync_unresolved()

    def unresolved(self):
        columns = ("vacancy_id", "url", "title", "status", "letter", "updated_at")
        rows = self.db.execute("SELECT vacancy_id,url,title,status,letter,updated_at FROM attempts "
                               "WHERE status IN ('unknown','pending') ORDER BY updated_at,vacancy_id")
        return [dict(zip(columns, row)) for row in rows]

    def all_attempts(self):
        columns = ("vacancy_id", "url", "title", "status", "letter", "updated_at")
        rows = self.db.execute("SELECT vacancy_id,url,title,status,letter,updated_at FROM attempts "
                               "ORDER BY updated_at,vacancy_id")
        return [dict(zip(columns, row)) for row in rows]

    def sync_unresolved(self):
        """Отдельный восстанавливаемый список; SQLite остаётся источником истории."""
        path = self.applied_ids_path.parent / "unknown_applications.json"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                             dir=path.parent, delete=False) as out:
                temporary = Path(out.name)
                json.dump(self.unresolved(), out, ensure_ascii=False, indent=2)
                out.write("\n")
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def discard_unsubmitted(self, vacancy_id: str):
        """Только для распознанной формы, пропущенной до нажатия отправки."""
        with self.db:
            self.db.execute("DELETE FROM attempts WHERE vacancy_id=? AND status='pending'", (vacancy_id,))
        self.sync_unresolved()

    def draft(self, vacancy: Vacancy, letter: str):
        with self.db:
            self.db.execute("INSERT INTO drafts(vacancy_id,url,letter,created_at) VALUES(?,?,?,?)",
                            (vacancy.id, vacancy.url, letter, self.now().isoformat()))

    def close(self):
        self.db.close()
