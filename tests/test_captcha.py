from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from selenium.common.exceptions import TimeoutException

from hh_auto_apply.browser_driver import check_blocked, navigate, wait_with_captcha
from hh_auto_apply.config import Config
from hh_auto_apply.models import RunBlocked
from hh_auto_apply.parser_applier import HHBrowser
from hh_auto_apply.selectors import load_selectors
from hh_auto_apply.telegram_logger import EventLogger


def test_hour_long_captcha_does_not_expire_page_wait_or_repeat_alert(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    driver, events = Mock(), Mock()
    driver.title = "HH"
    monkeypatch.setattr("hh_auto_apply.browser_driver.time.monotonic", lambda: clock.now)
    monkeypatch.setattr("hh_auto_apply.browser_driver.time.sleep", lambda seconds: setattr(clock, "now", clock.now + seconds))
    monkeypatch.setattr("hh_auto_apply.browser_driver.captcha_visible", lambda *_: clock.now < 3600)
    predicate = Mock(side_effect=[False, "loaded"])
    assert wait_with_captcha(driver, load_selectors(), predicate, 1, events) == "loaded"
    assert clock.now >= 3600
    assert [c.args[0] for c in events.emit.call_args_list] == ["CAPTCHA", "CAPTCHA_SOLVED"]
    assert "@dokuzu" in events.emit.call_args_list[0].args[1]
    driver.get.assert_not_called()
    driver.refresh.assert_not_called()


def test_captcha_wait_can_be_interrupted(monkeypatch):
    monkeypatch.setattr("hh_auto_apply.browser_driver.captcha_visible", lambda *_: True)
    def interrupt(_):
        raise KeyboardInterrupt()
    monkeypatch.setattr("hh_auto_apply.browser_driver.time.sleep", interrupt)
    events = Mock()
    with pytest.raises(KeyboardInterrupt):
        check_blocked(Mock(), load_selectors(), events)
    assert events.emit.call_count == 1


def test_navigation_timeout_waits_on_captcha_without_reopening_response(monkeypatch):
    driver = Mock()
    driver.get.side_effect = TimeoutException()
    check = Mock(side_effect=[True, False])
    monkeypatch.setattr("hh_auto_apply.browser_driver.check_blocked", check)
    navigate(driver, "https://hh.kz/applicant/vacancy_response?vacancyId=123", load_selectors(), Mock())
    driver.get.assert_called_once()


def test_navigation_timeout_without_captcha_still_propagates(monkeypatch):
    driver = Mock()
    driver.get.side_effect = TimeoutException()
    monkeypatch.setattr("hh_auto_apply.browser_driver.check_blocked", Mock(return_value=False))
    with pytest.raises(TimeoutException):
        navigate(driver, "https://hh.kz/vacancy/123", load_selectors(), Mock())


def test_submit_waits_after_captcha_and_never_clicks_twice(monkeypatch):
    driver, button, events = Mock(), Mock(), Mock()
    driver.title = "HH"
    selectors = load_selectors()
    state = {"captcha": False, "sent": False}
    def clicked():
        state["captcha"] = True
    button.click.side_effect = clicked
    element = Mock()
    element.is_displayed.return_value = True
    driver.find_elements.side_effect = lambda by, selector: [element] if state["sent"] and selector == selectors["success"] else []
    monkeypatch.setattr("hh_auto_apply.parser_applier.pause", lambda _: None)
    monkeypatch.setattr("hh_auto_apply.parser_applier.scroll_to", lambda *_: None)
    monkeypatch.setattr("hh_auto_apply.browser_driver.captcha_visible", lambda *_: state["captcha"])
    def solve(_):
        state.update(captcha=False, sent=True)
    monkeypatch.setattr("hh_auto_apply.browser_driver.time.sleep", solve)
    browser = HHBrowser(driver, Config(timeout=1), selectors, events)
    browser.submit(button)
    button.click.assert_called_once()
    assert [c.args[0] for c in events.emit.call_args_list] == ["CAPTCHA", "CAPTCHA_SOLVED"]


def test_access_denied_without_captcha_still_stops():
    driver = Mock()
    driver.title = "Доступ ограничен"
    driver.find_elements.return_value = []
    with pytest.raises(RunBlocked):
        check_blocked(driver, load_selectors(), Mock())


def test_captcha_episode_has_new_telegram_alert_and_edits_on_resolution(tmp_path):
    events = EventLogger(Config(runtime_dir=tmp_path))
    events.telegram = Mock()
    try:
        events.emit("CAPTCHA", "@dokuzu, пройдите CAPTCHA")
        first = events.captcha_key
        events.emit("CAPTCHA_SOLVED", "Продолжаю")
        events.emit("CAPTCHA", "@dokuzu, пройдите CAPTCHA")
        second = events.captcha_key
        calls = [c for c in events.telegram.upsert.call_args_list if c.args[0].startswith("captcha:")]
        assert [c.args[0] for c in calls] == [first, first, second]
        assert first != second
    finally:
        events.close()
