from hh_auto_apply.models import Vacancy
from hh_auto_apply.preferences import extract_schedules
from hh_auto_apply.reports import vacancy_links_report
from unittest.mock import Mock

import pytest


def vacancy(vid, schedule="", description="", address=""):
    return Vacancy(vid, f"https://hh.kz/vacancy/{vid}", schedule=schedule, description=description, address=address)


def test_report_prefers_two_two_then_five_two_and_unknown_last():
    items = [vacancy("1", "6/1"), vacancy("2"), vacancy("3", "5/2"), vacancy("4", "2/2"), vacancy("5", "2/2")]
    report = vacancy_links_report(items, apply=True)
    urls = [line for line in report.splitlines() if line.startswith("https://")]
    assert urls == [items[i].url for i in (3, 4, 2, 0, 1)]


def test_address_fraction_is_not_a_schedule():
    v = vacancy("1", description="Где предстоит работать\nАстана, Аль-Фараби, 7/5", address="Астана, Аль-Фараби, 7/5")
    assert extract_schedules(v) == []


def test_multiple_schedules_have_preferred_first_and_no_duplicate_urls():
    v = vacancy("1", "График: 6/1 или 5/2")
    report = vacancy_links_report([v, v], apply=False)
    assert "График: 5/2 или 6/1" in report
    assert report.count(v.url) == 1


def test_schedule_can_be_read_from_description():
    assert extract_schedules(vacancy("1", description="Условия\nГрафик работы 2/2\nАдрес дом 7/5")) == ["2/2"]


@pytest.mark.parametrize("apply", [False, True])
def test_completed_run_reports_only_processed_vacancies(tmp_path, apply):
    from hh_auto_apply.config import Config
    from hh_auto_apply.runner import run
    from hh_auto_apply.state import StateStore
    from hh_auto_apply.telegram_logger import EventLogger

    config = Config(runtime_dir=tmp_path, pause_min=0, pause_max=0, telegram_required=False)
    browser, llm = Mock(), Mock()
    items = [vacancy("1", "6/1"), vacancy("2", "2/2"), vacancy("3", "5/2")]
    browser.discover.return_value = iter([(items[0], False), (items[1], True), (items[2], False)])
    browser.read.side_effect = lambda item: item
    llm.generate.return_value = "Письмо"
    state = StateStore(tmp_path / "state.sqlite3", config.timezone)
    events = EventLogger(config)
    events.telegram = Mock()
    try:
        run(config, {}, browser, llm, state, events, apply=apply, limit=2)
        text = (tmp_path / "last_vacancy_links.txt").read_text(encoding="utf-8")
        assert [line for line in text.splitlines() if line.startswith("https://")] == [items[2].url, items[0].url]
        assert any(":part:" in c.args[0] for c in events.telegram.upsert.call_args_list)
        report = (tmp_path / "last_report.txt").read_text(encoding="utf-8")
        assert items[0].url in report and items[2].url in report
        assert "Письмо" in report
    finally:
        events.close()
        state.close()


def test_previous_unknown_is_in_final_report_without_retry(tmp_path):
    from hh_auto_apply.config import Config
    from hh_auto_apply.runner import run
    from hh_auto_apply.state import StateStore
    from hh_auto_apply.telegram_logger import EventLogger

    config = Config(runtime_dir=tmp_path, pause_min=0, pause_max=0, telegram_required=False)
    state = StateStore(tmp_path / "state.sqlite3", config.timezone)
    state.reserve(vacancy("1"), "Старое письмо", 10)
    state.finish("1", "unknown")
    browser, llm = Mock(), Mock()
    browser.discover.return_value = iter([(vacancy("1"), False)])
    events = EventLogger(config)
    events.telegram = Mock()
    try:
        run(config, {}, browser, llm, state, events, apply=True, limit=1)
        text = (tmp_path / "unknown_applications.txt").read_text(encoding="utf-8")
        assert vacancy("1").url in text and "Старое письмо" in text
        assert any(":part:" in c.args[0] for c in events.telegram.upsert.call_args_list)
        report = (tmp_path / "last_report.txt").read_text(encoding="utf-8")
        assert vacancy("1").url in report and "Старое письмо" in report
        manual = [c.args[1] for c in events.telegram.upsert.call_args_list if ":part:" in c.args[0]]
        assert any("Старое письмо" in message and vacancy("1").url in message for message in manual)
        browser.prepare.assert_not_called()
        llm.generate.assert_not_called()
    finally:
        events.close()
        state.close()


def test_large_report_preserves_every_link_and_separate_unknown_letters(tmp_path):
    from hh_auto_apply.reports import build_report_messages
    rows = [{"vacancy_id": str(i), "url": f"https://hh.kz/vacancy/{i}", "title": f"Вакансия {i}",
             "status": "sent" if i < 80 else "unknown", "letter": f"Письмо {i}", "updated_at": "2026-09-29"}
            for i in range(100)]
    messages = build_report_messages(rows, tmp_path / "absent.jsonl")
    links = [line for message in messages for line in message.splitlines() if line.startswith("https://")]
    assert len(links) == len(set(links)) == 100
    assert set(links) == {row["url"] for row in rows}
    for row in rows[80:]:
        matching = [message for message in messages if row["url"] in message.splitlines()]
        assert len(matching) == 1 and row["letter"] in matching[0]
        assert "Для ручной проверки" in matching[0]
    assert all(len(m.encode("utf-16-le")) // 2 < 3500 for m in messages)
    assert all(f"часть {index}/{len(messages)}" in message for index, message in enumerate(messages, 1))
