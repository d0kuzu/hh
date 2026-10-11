from unittest.mock import Mock

import pytest
from selenium.common.exceptions import NoSuchWindowException, TimeoutException

from hh_auto_apply.browser_driver import applicant_logged_in, ensure_login, open_applicant_page
from hh_auto_apply.config import Config
from hh_auto_apply.models import StopRun
from hh_auto_apply.selectors import load_selectors


def driver_with_markers(url, *keys):
    driver = Mock()
    driver.current_url = url
    driver.title = "Профиль"
    driver.timeouts.page_load = 60
    selectors = load_selectors()
    element = Mock()
    element.is_displayed.return_value = True
    driver.find_elements.side_effect = lambda by, selector: [element] if selector in {
        selectors[key] for key in keys
    } else []
    return driver


def test_profile_without_legacy_header_is_authenticated():
    driver = driver_with_markers("https://astana.hh.kz/applicant/profile/me", "applicant_profile")
    assert applicant_logged_in(driver, load_selectors())


@pytest.mark.parametrize("url", [
    "https://hh.kz/account/login", "https://hh.kz/resume/public-id", "https://unrelated.test/applicant/profile/me",
])
def test_profile_marker_outside_private_page_is_not_authentication(url):
    assert not applicant_logged_in(driver_with_markers(url, "applicant_profile"), load_selectors())


def test_private_url_alone_is_not_authentication():
    assert not applicant_logged_in(driver_with_markers("https://hh.kz/applicant/profile/me"), load_selectors())


def test_slow_resource_does_not_invalidate_saved_login(monkeypatch):
    driver = driver_with_markers("https://hh.kz/applicant/profile/me", "login")
    driver.get.side_effect = TimeoutException()
    prompt = Mock(side_effect=AssertionError("Saved session must not ask for login"))
    monkeypatch.setattr("builtins.input", prompt)
    ensure_login(driver, Config(timeout=0.1), load_selectors(), Mock())
    prompt.assert_not_called()
    assert driver.set_page_load_timeout.call_args.args == (60,)


def test_manual_login_reopens_private_page_and_waits(monkeypatch):
    driver = Mock()
    events = Mock()
    navigate = Mock()
    wait = Mock(side_effect=[False, True])
    monkeypatch.setattr("hh_auto_apply.browser_driver.open_applicant_page", navigate)
    monkeypatch.setattr("hh_auto_apply.browser_driver.wait_for_login", wait)
    prompt = Mock()
    monkeypatch.setattr("builtins.input", prompt)
    ensure_login(driver, Config(timeout=30), load_selectors(), events)
    assert navigate.call_count == 2
    assert wait.call_args.args[-1] == 30
    prompt.assert_called_once()


def test_closed_initial_tab_switches_to_remaining_login_tab():
    driver = Mock()
    from unittest.mock import PropertyMock
    type(driver).current_window_handle = PropertyMock(side_effect=NoSuchWindowException())
    driver.window_handles = ["login-tab"]
    driver.timeouts.page_load = 60
    open_applicant_page(driver, Config())
    driver.switch_to.window.assert_called_once_with("login-tab")


def test_failed_login_still_stops(monkeypatch):
    monkeypatch.setattr("hh_auto_apply.browser_driver.open_applicant_page", Mock())
    monkeypatch.setattr("hh_auto_apply.browser_driver.wait_for_login", Mock(return_value=False))
    monkeypatch.setattr("builtins.input", Mock())
    with pytest.raises(StopRun):
        ensure_login(Mock(), Config(timeout=0.1), load_selectors(), Mock())

