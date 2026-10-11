from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from hh_auto_apply.config import Config
from hh_auto_apply.models import StopRun, Vacancy
from hh_auto_apply.telegram_logger import EventLogger, TelegramSink, format_event


def reply(result=True, *, status=200, description=""):
    return SimpleNamespace(ok=status == 200, status_code=status, json=lambda: {
        "ok": status == 200, "result": result, "description": description,
    })


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr("hh_auto_apply.telegram_logger.time.sleep", lambda _: None)
    return Config(runtime_dir=tmp_path, telegram_token="123:test", telegram_chat_id="-100")


def sink_for(config, *responses):
    sink = TelegramSink(config)
    sink.session = Mock()
    sink.session.post.side_effect = responses
    return sink


def methods(sink):
    return [call.args[0].rsplit("/", 1)[-1] for call in sink.session.post.call_args_list]


def test_vacancy_uses_same_id_and_unchanged_text_skips_network(config):
    sink = sink_for(config, reply({"message_id": 10}), reply({"message_id": 10}))
    try:
        assert sink.upsert("vacancy:1", "Найдена") == [10]
        assert sink.upsert("vacancy:1", "Письмо готово") == [10]
        sink.upsert("vacancy:1", "Письмо готово")
        assert methods(sink) == ["sendMessage", "editMessageText"]
        assert sink.session.post.call_args.kwargs["json"]["message_id"] == 10
    finally:
        sink.close()


def test_ids_survive_restart(config):
    first = sink_for(config, reply({"message_id": 15}))
    first.upsert("vacancy:1", "Первый запуск")
    first.close()
    second = sink_for(config, reply({"message_id": 15}))
    try:
        assert second.upsert("vacancy:1", "Второй запуск") == [15]
        assert methods(second) == ["editMessageText"]
    finally:
        second.close()


def test_long_message_edits_parts_and_removes_surplus(config):
    sink = sink_for(config, reply({"message_id": 1}), reply({"message_id": 2}), reply(), reply())
    try:
        assert sink.upsert("vacancy:1", "я" * 3600) == [1, 2]
        assert sink.upsert("vacancy:1", "Короткое описание") == [1]
        assert methods(sink) == ["sendMessage", "sendMessage", "editMessageText", "deleteMessage"]
        assert sink.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
    finally:
        sink.close()


def test_adding_letter_only_creates_needed_extra_part(config):
    sink = sink_for(config, reply({"message_id": 1}), reply({"message_id": 2}))
    try:
        sink.upsert("vacancy:1", "я" * 3500)
        assert sink.upsert("vacancy:1", "я" * 3500 + "Письмо") == [1, 2]
        assert methods(sink) == ["sendMessage", "sendMessage"]
    finally:
        sink.close()


def test_deleted_message_replaced_and_new_id_saved(config):
    sink = sink_for(config, reply({"message_id": 10}),
                    reply(status=400, description="Bad Request: message to edit not found"),
                    reply({"message_id": 20}))
    try:
        sink.upsert("vacancy:1", "Старое")
        assert sink.upsert("vacancy:1", "Новое") == [20]
    finally:
        sink.close()


def test_edit_failure_does_not_send_duplicate(config):
    sink = sink_for(config, reply({"message_id": 10}),
                    reply(status=400, description="Bad Request: message can't be edited"))
    try:
        sink.upsert("vacancy:1", "Старое")
        with pytest.raises(StopRun):
            sink.upsert("vacancy:1", "Новое")
        assert methods(sink) == ["sendMessage", "editMessageText"]
    finally:
        sink.close()


def test_not_modified_response_is_success(config):
    sink = sink_for(config, reply({"message_id": 10}),
                    reply(status=400, description="Bad Request: message is not modified"))
    try:
        sink.upsert("vacancy:1", "Старое")
        assert sink.upsert("vacancy:1", "Новое") == [10]
    finally:
        sink.close()


def test_send_timeout_does_not_repeat_send(config):
    sink = sink_for(config, requests.Timeout())
    try:
        with pytest.raises(StopRun):
            sink.upsert("vacancy:1", "Текст")
        assert sink.session.post.call_count == 1
    finally:
        sink.close()


def test_no_cross_chat_message_ids(config):
    from dataclasses import replace
    first = sink_for(config, reply({"message_id": 10}))
    first.upsert("vacancy:1", "Текст")
    first.close()
    second = sink_for(replace(config, telegram_chat_id="-200"), reply({"message_id": 20}))
    try:
        assert second.upsert("vacancy:1", "Текст") == [20]
        assert methods(second) == ["sendMessage"]
    finally:
        second.close()


def test_format_separates_blocks_and_omits_unavailable_letter():
    vacancy = Vacancy("1", "https://hh.kz/vacancy/1", "Бармен", description="Описание вакансии", schedule="График: 2/2")
    early = format_event("PARSED", "Прочитано", vacancy)
    assert "Письмо:" not in early and "ещё не" not in early
    assert "График: График:" not in early
    text = format_event("LETTER", "Готово", vacancy, "Здравствуйте!", "Счётчик: 1/5")
    assert "\n\nОписание:\n" in text
    assert "\n\nПисьмо:\nЗдравствуйте!\n\nСчётчик: 1/5" in text


def test_events_update_vacancy_and_single_status_with_counters(config):
    events = EventLogger(config)
    real_sink = events.telegram
    events.telegram = Mock()
    vacancy = Vacancy("42", "https://hh.kz/vacancy/42", "Бармен")
    try:
        events.emit("START", "Тест")
        events.set_progress(scanned=1, prepared=1, sent=0, skipped=0, target=5, apply=False)
        events.emit("LETTER", "Готово", vacancy, "Письмо")
        events.emit("DRY_RUN", "Без отправки", vacancy, "Письмо")
        calls = events.telegram.upsert.call_args_list
        assert [call.args[0] for call in calls] == ["run:status", "vacancy:42", "vacancy:42", "run:status"]
        assert "Тестовые письма: 1/5" in calls[-1].args[1]
        assert "Отклики HH: 0" in calls[-1].args[1]
        events.set_progress(scanned=2, prepared=2, sent=1, skipped=0, target=5, apply=True)
        events.emit("SUCCESS", "Отправлено", vacancy, "Письмо")
        assert "Отклики HH: 1/5" in events.telegram.upsert.call_args.args[1]
    finally:
        events.close()
        real_sink.close()
