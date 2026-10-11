import pytest
import json
from unittest.mock import Mock

from hh_auto_apply.models import StopRun, Vacancy
from hh_auto_apply.state import StateStore


@pytest.fixture
def state(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3", "Asia/Qyzylorda")
    yield store
    store.close()


def vacancy(vid="123"):
    return Vacancy(vid, f"https://hh.kz/vacancy/{vid}", "Бармен")


def test_only_confirmed_submission_is_written(state):
    assert state.applied_ids_path.read_text() == ""
    state.reserve(vacancy(), "Письмо", 10)
    state.finish("123", "unknown")
    assert state.applied_ids_path.read_text() == ""
    state.finish("123", "sent")
    state.finish("123", "sent")
    assert state.applied_ids_path.read_text() == "123\n"


def test_file_blocks_submission_without_database_record(state):
    state.applied_ids_path.write_text("123\n", encoding="utf-8")
    assert state.status("123") == "sent"
    with pytest.raises(StopRun):
        state.reserve(vacancy(), "Письмо", 10)


def test_restart_restores_export_and_preserves_manual_ids(state):
    state.reserve(vacancy(), "Письмо", 10)
    state.finish("123", "sent")
    state.applied_ids_path.write_text("456\n", encoding="utf-8")
    second = StateStore(state.applied_ids_path.parent / "state.sqlite3", "Asia/Qyzylorda")
    try:
        assert second.applied_ids_path.read_text() == "123\n456\n"
        assert second.status("123") == second.status("456") == "sent"
    finally:
        second.close()


def test_invalid_file_stops_instead_of_ignoring_history(state):
    state.applied_ids_path.write_text("not-an-id\n", encoding="utf-8")
    with pytest.raises(StopRun):
        state.status("123")


def test_failed_export_keeps_database_duplicate_protection(state, monkeypatch):
    state.reserve(vacancy(), "Письмо", 10)
    monkeypatch.setattr("hh_auto_apply.state.os.replace", lambda *args: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        state.finish("123", "sent")
    assert state.status("123") == "sent"


def test_file_skip_happens_before_reading_or_generating(state):
    from hh_auto_apply.config import Config
    from hh_auto_apply.runner import run
    state.applied_ids_path.write_text("123\n", encoding="utf-8")
    browser, llm = Mock(), Mock()
    browser.discover.return_value = iter([(vacancy(), False)])
    result = run(Config(pause_min=0, pause_max=0), {}, browser, llm, state, Mock(), apply=True, limit=1)
    assert result.skipped == 1 and result.sent == 0
    browser.read.assert_not_called()
    browser.submit.assert_not_called()
    llm.generate.assert_not_called()


def test_single_vacancy_mode_does_not_fall_back_to_search():
    from hh_auto_apply.config import Config
    from hh_auto_apply.parser_applier import HHBrowser
    from hh_auto_apply.selectors import load_selectors
    driver = Mock()
    browser = HHBrowser(driver, Config(), load_selectors(), Mock(), vacancy_id="123")
    result = list(browser.discover())
    assert len(result) == 1 and result[0][0].id == "123"
    driver.get.assert_not_called()


def test_unresolved_export_survives_restart_and_clears_when_resolved(state):
    state.reserve(vacancy(), "Письмо для ручной проверки", 10)
    state.finish("123", "unknown")
    path = state.applied_ids_path.parent / "unknown_applications.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert rows[0]["vacancy_id"] == "123"
    assert rows[0]["letter"] == "Письмо для ручной проверки"
    path.unlink()
    second = StateStore(path.parent / "state.sqlite3", "Asia/Qyzylorda")
    try:
        assert second.unresolved() == rows
        assert json.loads(path.read_text(encoding="utf-8")) == rows
        second.finish("123", "sent")
        assert json.loads(path.read_text(encoding="utf-8")) == []
    finally:
        second.close()
