import json
import logging
import re
import sqlite3
import time
from uuid import uuid4
from dataclasses import asdict
from datetime import datetime, timezone

import requests

from .config import Config
from .models import RunBlocked, StopRun, Vacancy


def message_chunks(text: str, limit: int = 3500):
    """Запас до 4096, учитывая emoji как две UTF-16 code units."""
    chunk, size = [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > limit:
            yield "".join(chunk)
            chunk, size = [], 0
        chunk.append(char)
        size += width
    if chunk:
        yield "".join(chunk)


class TelegramSink:
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.last_sent = 0.0
        config.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(config.runtime_dir / "telegram.sqlite3")
        self.db.execute("""CREATE TABLE IF NOT EXISTS messages (
            scope TEXT NOT NULL, event_key TEXT NOT NULL, part INTEGER NOT NULL,
            message_id INTEGER NOT NULL, text TEXT NOT NULL,
            PRIMARY KEY(scope, event_key, part)
        )""")
        # Токен не сохраняем. Смена бота/группы/темы создаёт независимый набор ID.
        self.scope = f"{config.telegram_token.split(':')[0]}:{config.telegram_chat_id}:{config.telegram_thread_id or 0}"

    def send(self, text: str):
        return [self._send_chunk(chunk) for chunk in message_chunks(text)]

    def upsert(self, key: str, text: str):
        """Сохраняем ID каждой части сразу, в том числе при частичном сбое."""
        rows = self.db.execute(
            "SELECT part,message_id,text FROM messages WHERE scope=? AND event_key=? ORDER BY part",
            (self.scope, key),
        ).fetchall()
        existing = {part: (mid, old_text) for part, mid, old_text in rows}
        chunks = list(message_chunks(text))
        ids = []
        for part, chunk in enumerate(chunks):
            old = existing.get(part)
            if old:
                mid, old_text = old
                if old_text != chunk:
                    result = self._request("editMessageText", {
                        "chat_id": self.config.telegram_chat_id, "message_id": mid,
                        "text": chunk, "link_preview_options": {"is_disabled": True},
                    })
                    if result is None:  # Сообщение было удалено из Telegram.
                        mid = self._send_chunk(chunk)
            else:
                mid = self._send_chunk(chunk)
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?)",
                                (self.scope, key, part, mid, chunk))
            ids.append(mid)
        # При сокращении текста убираем только собственные лишние части этой карточки.
        for part, mid, _ in reversed(rows):
            if part >= len(chunks):
                self._request("deleteMessage", {"chat_id": self.config.telegram_chat_id, "message_id": mid})
                with self.db:
                    self.db.execute("DELETE FROM messages WHERE scope=? AND event_key=? AND part=?",
                                    (self.scope, key, part))
        return ids

    def _send_chunk(self, chunk: str):
        payload = {"chat_id": self.config.telegram_chat_id, "text": chunk,
                   "link_preview_options": {"is_disabled": True}}
        if self.config.telegram_thread_id:
            payload["message_thread_id"] = self.config.telegram_thread_id
        result = self._request("sendMessage", payload)
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
            raise StopRun("Telegram не вернул message_id; повторная отправка остановлена")
        return result["message_id"]

    def _request(self, method: str, payload: dict):
        for attempt in range(3):
            # Групповые лимиты Telegram; сохраняем порядок событий.
            time.sleep(max(0.0, 3.2 - (time.monotonic() - self.last_sent)))
            try:
                response = self.session.post(
                    f"https://api.telegram.org/bot{self.config.telegram_token}/{method}",
                    json=payload, timeout=(10, 20),
                )
                self.last_sent = time.monotonic()
                data = response.json()
                if response.ok and data.get("ok"):
                    return data.get("result", True)
                description = str(data.get("description", "")).casefold()
                if response.status_code == 400:
                    if method == "editMessageText" and "message is not modified" in description:
                        return True
                    if method == "editMessageText" and "message to edit not found" in description:
                        return None
                    if method == "deleteMessage" and "message to delete not found" in description:
                        return True
                if response.status_code == 429:
                    wait = float(data.get("parameters", {}).get("retry_after", 5))
                    if wait > 60:
                        break
                    time.sleep(max(0, wait))
                elif response.status_code >= 500 and method != "sendMessage":
                    time.sleep(2 ** attempt)
                else:
                    break
            except (requests.RequestException, ValueError):
                # URL исключения requests может содержать токен; не логируем его.
                # У sendMessage нет ключа идемпотентности: потерянный ответ
                # не означает недоставку. Автоповтор мог бы создать дубль.
                if method == "sendMessage":
                    break
                time.sleep(2 ** attempt)
        raise StopRun("Telegram не подтвердил доставку. Проверьте токен, chat_id и права бота; событие сохранено локально")

    def close(self):
        self.session.close()
        self.db.close()


def format_event(step, message, vacancy=None, letter="", progress=""):
    blocks = [f"[{step}] {message}"]
    if vacancy:
        fields = [f"Вакансия: {vacancy.title}", f"Компания: {vacancy.company}", f"Ссылка: {vacancy.url}"]
        if vacancy.address:
            fields.append(f"Адрес: {vacancy.address}")
        if vacancy.salary:
            fields.append(f"Оплата: {vacancy.salary}")
        if vacancy.schedule:
            schedule = re.sub(r"^(?:график\s*:\s*)+", "", vacancy.schedule.strip(), flags=re.I)
            fields.append(f"График: {schedule}")
        blocks.append("\n".join(fields))
        if vacancy.notes:
            blocks.append("\n".join(vacancy.notes))
        if vacancy.description:
            blocks.append(f"Описание:\n{vacancy.description}")
        if letter:
            blocks.append(f"Письмо:\n{letter}")
    if progress:
        blocks.append(progress)
    return "\n\n".join(blocks)


class EventLogger:
    def __init__(self, config: Config):
        self.config = config
        config.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger(f"hh_auto_apply.{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        formatter = logging.Formatter("%(asctime)s %(message)s")
        for handler in (logging.StreamHandler(), logging.FileHandler(
            config.runtime_dir / "app.log", encoding="utf-8"
        )):
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
        self.telegram = TelegramSink(config) if config.telegram_token else None
        self.progress = ""
        self.started_at = datetime.now().astimezone().strftime("%d.%m.%Y %H:%M")
        self.status = "Запуск начат"
        self.captcha_key = None
        self.report_key = "report:" + uuid4().hex

    def set_progress(self, *, scanned, prepared, sent, skipped, target, apply, unknown=0):
        if apply:
            count = f"📊 Отклики HH: {sent}/{target} | Писем подготовлено: {prepared}"
        else:
            count = f"📊 Тестовые письма: {prepared}/{target} | Отклики HH: {sent}"
        self.progress = count + f"\nПросмотрено: {scanned} | Пропущено: {skipped}"
        if unknown:
            self.progress += f"\n⚠️ Неизвестный исход: {unknown} | Учтено попыток: {sent + unknown}/{target}"

    def emit(self, step: str, message: str, vacancy: Vacancy | None = None,
             letter: str = "", *, critical: bool = True, notify: bool = True):
        text = self.redact(format_event(step, message, vacancy, letter, self.progress))
        self.logger.info(text)
        event = {"time": datetime.now(timezone.utc).isoformat(), "step": step, "message": message,
                 "vacancy": asdict(vacancy) if vacancy else None, "letter": letter, "progress": self.progress}
        with (self.config.runtime_dir / "events.jsonl").open("a", encoding="utf-8") as out:
            out.write(self.redact(json.dumps(event, ensure_ascii=False)) + "\n")
        if step == "SKIP":
            with (self.config.runtime_dir / "skipped.log").open("a", encoding="utf-8") as out:
                out.write(text + "\n\n")
        if step == "REPORT":
            (self.config.runtime_dir / "last_vacancy_links.txt").write_text(text + "\n", encoding="utf-8")
        if step == "UNKNOWN_REPORT":
            (self.config.runtime_dir / "unknown_applications.txt").write_text(text + "\n", encoding="utf-8")
        if self.telegram and notify:
            try:
                if vacancy:
                    self.telegram.upsert(f"vacancy:{vacancy.id}", text)
                    if step in {"DRY_RUN", "SUCCESS", "SKIP", "UNKNOWN", "ERROR"}:
                        self.status = f"[{step}] {vacancy.title}: {message}"
                        self._update_status()
                elif step == "REPORT":
                    self.telegram.upsert("run:links", text)
                elif step == "UNKNOWN_REPORT":
                    self.telegram.upsert("run:unknown", text)
                elif step == "CAPTCHA":
                    # Новый эпизод — новое уведомление с упоминанием. Во время
                    # ожидания сообщений нет; после решения редактируем это же.
                    self.captcha_key = "captcha:" + uuid4().hex
                    self.telegram.upsert(self.captcha_key, text)
                    self.status = message
                    self._update_status()
                elif step == "CAPTCHA_SOLVED":
                    if self.captcha_key:
                        self.telegram.upsert(self.captcha_key, text)
                    self.status = message
                    self._update_status()
                else:
                    self.status = f"[{step}] {message}"
                    self._update_status()
            except StopRun as exc:
                self.logger.error("[TELEGRAM_ERROR] %s", exc)
                if critical and self.config.telegram_required:
                    raise RunBlocked(str(exc)) from exc

    def _update_status(self):
        self.telegram.upsert("run:status", self.redact("\n\n".join(filter(None, (
            f"Запуск: {self.started_at}", self.status, self.progress,
        )))))

    def final_report(self, summary, full_text, *, unresolved_count, messages):
        text = self.redact(full_text)
        summary = self.redact(summary)
        directory = self.config.runtime_dir / "reports"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ("hh-report-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".txt")
        path.write_text(text, encoding="utf-8")
        (self.config.runtime_dir / "last_report.txt").write_text(text, encoding="utf-8")
        self.emit("REPORT_FILE", summary + f"\nПолный отчёт: {path.name}", notify=False)
        if self.telegram:
            try:
                self.telegram.upsert(self.report_key + ":summary", summary +
                                     f"\n\nПолный список ниже: {len(messages)} частей. "
                                     "Письма доступны в карточках вакансий; повторно отправляйте только после проверки HH.")
                for index, message in enumerate(messages, 1):
                    self.telegram.upsert(f"{self.report_key}:part:{index}", self.redact(message))
                self.emit("REPORT_DELIVERED", f"Полный отчёт доставлен сообщениями: {len(messages)} частей; "
                          f"для ручной проверки: {unresolved_count}", notify=False)
            except StopRun as exc:
                self.logger.error("[TELEGRAM_ERROR] %s", exc)
                if self.config.telegram_required:
                    raise RunBlocked(str(exc)) from exc
        return path

    def redact(self, text: str) -> str:
        for secret in (self.config.api_key, self.config.telegram_token):
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return text

    def close(self):
        if self.telegram:
            self.telegram.close()
        for handler in self.logger.handlers[:]:
            handler.close()
            self.logger.removeHandler(handler)
