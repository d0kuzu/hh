from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import pytest

from hh_auto_apply.config import Config
from hh_auto_apply.llm_service import LLMService, validate_letter
from hh_auto_apply.main import arguments
from hh_auto_apply.models import RunBlocked, SkipVacancy, StopRun, Vacancy
from hh_auto_apply.parser_applier import filter_reason, has_test_text, vacancy_id
from hh_auto_apply.preferences import assess, salary_notes
from hh_auto_apply.runner import run
from hh_auto_apply.state import StateStore
from hh_auto_apply.telegram_logger import EventLogger, TelegramSink, message_chunks
from hh_auto_apply.user_profile import PROFILE, validate_profile

LETTER = "Здравствуйте! Откликаюсь на вакансию бармена. Работала в TomYumBar и CoffeeBoom, знаю кофе и базовые коктейли. Буду рада обсудить задачи и график."


@pytest.fixture
def config(tmp_path):
    return Config(api_key="test-key", telegram_required=False, pause_min=0, pause_max=0,
                  runtime_dir=tmp_path, profile_dir=tmp_path / "chrome", resume_id="resume-123")


@pytest.fixture
def state(config):
    store = StateStore(config.runtime_dir / "state.sqlite3", config.timezone)
    yield store
    store.close()


def vacancy(i=1, **kwargs):
    return Vacancy(str(i), f"https://hh.kz/vacancy/{i}", "Бармен", description="Базовые коктейли и кофе", **kwargs)


def harness(count=8):
    browser, llm, events = Mock(), Mock(), Mock()
    browser.discover.return_value = iter((vacancy(i), False) for i in range(count))
    browser.read.side_effect = lambda item: item
    llm.generate.return_value = LETTER
    return browser, llm, events


def test_dry_run_exactly_five_and_never_opens_form(config, state):
    browser, llm, events = harness()
    result = run(config, PROFILE, browser, llm, state, events)
    assert result.prepared == 5
    assert result.sent == state.count_today() == 0
    assert llm.generate.call_count == 5
    browser.prepare.assert_not_called()
    browser.submit.assert_not_called()
    assert state.db.execute("SELECT count(*) FROM drafts").fetchone()[0] == 5


def test_skip_applied_and_test_continues_to_five(config, state):
    browser, llm, events = harness()
    browser.discover.return_value = iter((vacancy(i), i == 0) for i in range(8))
    browser.precheck.side_effect = [SkipVacancy("Тест")] + [None] * 6
    result = run(config, PROFILE, browser, llm, state, events)
    assert result.prepared == 5 and result.skipped == 2 and result.scanned == 7


@pytest.mark.parametrize("source", ["hh", "history"])
def test_known_skips_have_no_pause_or_per_vacancy_telegram(config, state, monkeypatch, source):
    browser, llm, _ = harness(28)
    browser.end_reason = "Выдача HH закончилась: 28 вакансий"
    browser.discover.return_value = iter((vacancy(i), source == "hh") for i in range(28))
    if source == "history":
        state.applied_ids_path.write_text("\n".join(str(i) for i in range(28)), encoding="utf-8")
    pause = Mock()
    monkeypatch.setattr("hh_auto_apply.runner.pause", pause)
    events = EventLogger(config)
    events.telegram = Mock()
    try:
        result = run(config, PROFILE, browser, llm, state, events, apply=True, limit=50)
        assert result.skipped == result.scanned == 28
        pause.assert_not_called()
        browser.read.assert_not_called()
        browser.submit.assert_not_called()
        llm.generate.assert_not_called()
        assert not any(c.args[0].startswith("vacancy:") for c in events.telegram.upsert.call_args_list)
        assert (config.runtime_dir / "skipped.log").read_text(encoding="utf-8").count("[SKIP]") == 28
        assert "28 вакансий" in events.status
        assert result.end_reason == browser.end_reason
    finally:
        events.close()


def test_fast_skip_does_not_prevent_processing_new_vacancy(config, state, monkeypatch):
    browser, llm, events = harness(2)
    browser.discover.return_value = iter([(vacancy(0), True), (vacancy(1), False)])
    pause = Mock()
    monkeypatch.setattr("hh_auto_apply.runner.pause", pause)
    result = run(config, PROFILE, browser, llm, state, events, apply=True, limit=1)
    assert result.skipped == result.sent == 1
    browser.read.assert_called_once_with(vacancy(1))
    pause.assert_not_called()
    assert "лимит запуска" in result.end_reason


def test_scan_limit_has_explicit_completion_reason(config, state):
    browser, llm, events = harness(2)
    browser.discover.return_value = iter([(vacancy(0), True), (vacancy(1), False)])
    result = run(replace(config, max_scanned=1), PROFILE, browser, llm, state, events, apply=True)
    assert result.end_reason == "Достигнут предел просмотренных вакансий MAX_SCANNED=1"
    llm.generate.assert_not_called()


def test_apply_daily_limit_persists_across_runs(config, state):
    config = replace(config, daily_limit=2)
    for _ in range(2):
        browser, llm, events = harness()
        run(config, PROFILE, browser, llm, state, events, apply=True, limit=5)
    assert state.count_today() == 2
    assert state.status("0") == "sent"
    browser.submit.assert_not_called()


def test_unknown_submission_continues_and_blocks_retry(config, state):
    browser, llm, events = harness()
    browser.submit.side_effect = [TimeoutError(), None]
    result = run(config, PROFILE, browser, llm, state, events, apply=True, limit=2)
    assert state.status("0") == "unknown"
    assert state.status("1") == "sent"
    assert state.count_today() == 2
    assert result.unknown == result.sent == 1
    assert browser.submit.call_count == 2
    browser.recover_after_unknown.assert_called_once()
    reports = [call.args for call in events.emit.call_args_list if call.args[0] == "UNKNOWN_REPORT"]
    assert len(reports) == 1 and vacancy(0).url in reports[0][1] and LETTER in reports[0][1]
    assert vacancy(1).url not in reports[0][1]
    with pytest.raises(StopRun):
        state.reserve(vacancy(0), LETTER, 5)


def test_failure_after_response_navigation_counts_towards_run_limit(config, state):
    browser, llm, events = harness()
    def prepare(*args):
        assert state.status("0") == "pending"
        raise SkipVacancy("Форма не распознана после перехода")
    browser.prepare.side_effect = prepare
    result = run(config, PROFILE, browser, llm, state, events, apply=True, limit=1)
    assert result.unknown == 1 and result.sent == 0
    assert state.status("0") == "unknown"
    assert browser.prepare.call_count == 1
    browser.submit.assert_not_called()


@pytest.mark.parametrize("error", [KeyboardInterrupt(), RunBlocked("CAPTCHA")])
def test_interrupts_and_global_blocks_still_stop_and_preserve_unknown(config, state, error):
    browser, llm, events = harness()
    browser.submit.side_effect = error
    with pytest.raises(type(error)):
        run(config, PROFILE, browser, llm, state, events, apply=True)
    assert state.status("0") == "unknown"
    assert browser.submit.call_count == 1
    browser.recover_after_unknown.assert_not_called()


def test_unknown_attempts_consume_daily_limit(config, state):
    browser, llm, events = harness()
    browser.submit.side_effect = TimeoutError()
    result = run(replace(config, daily_limit=2), PROFILE, browser, llm, state, events, apply=True, limit=5)
    assert result.sent == 0 and result.unknown == 2
    assert browser.submit.call_count == state.count_today() == 2


def test_recognized_questionnaire_releases_reservation_and_skips(config, state):
    browser, llm, events = harness(1)
    browser.unsubmitted_form = True
    browser.prepare.side_effect = SkipVacancy("Форма требует тест")
    result = run(config, PROFILE, browser, llm, state, events, apply=True, limit=1)
    assert result.skipped == 1 and result.sent == 0
    assert state.status("0") is None
    assert state.count_today() == 0
    browser.submit.assert_not_called()


def test_pre_submit_telegram_failure_cannot_send(config, state):
    browser, llm, events = harness()
    def emit(step, *args, **kwargs):
        if step == "SUBMIT":
            raise StopRun("Telegram недоступен")
    events.emit.side_effect = emit
    with pytest.raises(StopRun):
        run(config, PROFILE, browser, llm, state, events, apply=True)
    browser.submit.assert_not_called()
    assert state.count_today() == 0


def test_crash_pending_blocks_duplicate_and_counts_today(state):
    state.reserve(vacancy(), LETTER, 1)
    with pytest.raises(StopRun):
        state.reserve(vacancy(2), LETTER, 1)
    second = StateStore(Path(state.db.execute("PRAGMA database_list").fetchone()[2]), "Asia/Qyzylorda")
    try:
        assert second.status("1") == "pending"
        with pytest.raises(StopRun):
            second.reserve(vacancy(), LETTER, 10)
    finally:
        second.close()
    assert state.status("1") == "pending"


def test_day_rollover_resets_count_but_not_duplicate_guard(state):
    now = state.now()
    state.now = lambda: now - timedelta(days=1)
    state.reserve(vacancy(), LETTER, 1)
    state.finish("1", "sent")
    state.now = lambda: now
    assert state.count_today() == 0
    with pytest.raises(StopRun):
        state.reserve(vacancy(), LETTER, 1)
    state.reserve(vacancy(2), LETTER, 1)


def test_search_url_escapes_query_and_keeps_areas():
    config = Config(query="бармен & бариста", areas=("159", "160"))
    params = parse_qs(urlparse(config.search_url(2)).query)
    assert params["text"] == ["бармен & бариста"]
    assert params["area"] == ["159", "160"]
    assert params["page"] == ["2"]
    assert params["search_field"] == ["name"]


@pytest.mark.parametrize("url", ["https://evil.com/vacancy/12", "https://hh.kz.evil.com/vacancy/12", "javascript:alert(1)"])
def test_reject_non_hh_urls(url):
    assert vacancy_id(url) is None


def test_region_vacancy_id():
    assert vacancy_id("https://astana.hh.kz/vacancy/12?q=bar") == "12"


def test_style_validator():
    assert not validate_letter(LETTER)
    assert validate_letter("Я идеальный кандидат. " + LETTER)
    assert validate_letter("слово " * 101)
    assert validate_letter("")


def test_llm_retries_style_and_uses_candidate_facts(config):
    service = LLMService(config)
    service.client = Mock()
    service.client.responses.create.side_effect = [SimpleNamespace(output_text=""), SimpleNamespace(output_text=LETTER)]
    assert service.generate(PROFILE, vacancy()) == LETTER
    payload = service.client.responses.create.call_args.kwargs
    assert payload["store"] is False
    assert "Наргиз" in payload["input"]
    assert "женский" in payload["input"]


def test_chunks_preserve_text_and_fit_utf16():
    text = ("⚠️ Вакансия 🍸\n" * 1500)
    chunks = list(message_chunks(text))
    assert "".join(chunks) == text
    assert all(len(c.encode("utf-16-le")) // 2 <= 3500 for c in chunks)


def test_telegram_429_retry(config, monkeypatch):
    monkeypatch.setattr("hh_auto_apply.telegram_logger.time.sleep", lambda _: None)
    sink = TelegramSink(config)
    sink.session = Mock()
    sink.session.post.side_effect = [
        SimpleNamespace(ok=False, status_code=429, json=lambda: {"parameters": {"retry_after": 1}}),
        SimpleNamespace(ok=True, status_code=200, json=lambda: {"ok": True, "result": {"message_id": 42}}),
    ]
    sink.send(LETTER)
    assert sink.session.post.call_count == 2


def test_log_redacts_secrets_and_retains_context(config):
    events = EventLogger(config)
    try:
        events.emit("SKIP", "test-key", vacancy(), LETTER)
    finally:
        events.close()
    text = (config.runtime_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "test-key" not in text and "REDACTED" in text and LETTER in text
    assert (config.runtime_dir / "skipped.log").exists()


def test_soft_preferences_do_not_skip(config):
    item = vacancy(schedule="6/1", address="Астана, правый берег", salary="14 000 ₸ за смену, на руки")
    notes = assess(item, PROFILE)
    assert sum("⚠️" in n for n in notes) == 3
    assert filter_reason(item, config) is None


def test_monthly_comparison_depends_on_schedule():
    prefs = PROFILE["preferences"]
    assert any("достигает" in n for n in salary_notes("300 000 ₸ за месяц", ["2/2"], prefs))
    assert any("ниже" in n for n in salary_notes("300 000 ₸ за месяц", ["5/2"], prefs))
    assert any("однозначный" in n for n in salary_notes("300 000 ₸ за месяц", ["2/2", "5/2"], prefs))


def test_salary_range_and_unknown_bank():
    notes = assess(vacancy(salary="от 12 000 ₸ за смену", address="Астана, Туран, 20"), PROFILE)
    assert not any("может быть ниже" in n for n in notes)
    assert not any("Берег не указан" in n for n in notes)
    assert not any("Сравнение ориентировочное" in n for n in notes)


def test_profile_valid_and_default_mode():
    validate_profile(PROFILE)
    assert not arguments([]).apply


def test_test_detection_not_generic_job_skill():
    assert has_test_text("Для отклика нужно пройти тест")
    assert not has_test_text("Тестирование новых напитков и рецептур")
