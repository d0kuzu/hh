"""Локальные Selenium-тесты. RUN_BROWSER_TESTS=1 включает настоящий Chrome.

Формы являются тестовыми HTML-файлами; HH и работодатели не получают запросов.
"""
import os
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock

import pytest
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service

from hh_auto_apply.config import Config
from hh_auto_apply.browser_driver import visible
from hh_auto_apply.models import SkipVacancy, StopRun, Vacancy
from hh_auto_apply.parser_applier import HHBrowser
from hh_auto_apply.selectors import load_selectors

pytestmark = pytest.mark.skipif(os.getenv("RUN_BROWSER_TESTS") != "1", reason="Set RUN_BROWSER_TESTS=1 for local Chrome tests")

LETTER = "Здравствуйте! Откликаюсь на вакансию бармена. Знаю кофе и базовые коктейли. Буду рада обсудить детали."
VACANCY = Vacancy("123", "https://hh.kz/vacancy/123", "Бармен", description="Кофе и коктейли")
FORM = '''<!doctype html><html lang="ru"><meta charset="utf-8"><body>
<form data-qa="vacancy-response-popup-form" onsubmit="event.preventDefault(); document.getElementById('ok').hidden=false; this.hidden=true;">
<label><input name="resumeId" type="radio" value="wrong" checked>Неверное резюме</label>
<label><input name="resumeId" type="radio" value="correct">Нужное резюме</label>
<button type="button" data-qa="vacancy-response-letter-toggle" onclick="document.getElementById('letter').hidden=false">Добавить письмо</button>
<textarea id="letter" name="letter" hidden></textarea>
EXTRA_FIELD
<button data-qa="vacancy-response-submit-popup" type="submit">Отправить</button>
</form>
<div id="ok" data-qa="vacancy-response-success" hidden>Отклик отправлен</div>
</body></html>'''


@pytest.fixture(scope="module")
def driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1280,1000")
    if os.getenv("CHROME_BINARY"):
        options.binary_location = os.environ["CHROME_BINARY"]
    service = Service(os.environ["CHROMEDRIVER_PATH"]) if os.getenv("CHROMEDRIVER_PATH") else Service()
    browser = webdriver.Chrome(options=options, service=service)
    yield browser
    browser.quit()


def prepare_fixture(driver, tmp_path, monkeypatch, extra="", resume="correct"):
    config = Config(resume_id=resume, pause_min=0, pause_max=0, typing_delay=0, timeout=1)
    form_path = tmp_path / "form.html"
    form_path.write_text(FORM.replace("EXTRA_FIELD", extra), encoding="utf-8")
    vacancy_path = tmp_path / "vacancy.html"
    vacancy_path.write_text('''<html><a data-qa="vacancy-response-link-top"
        href="https://hh.kz/applicant/vacancy_response?vacancyId=123">Откликнуться</a></html>''', encoding="utf-8")
    original_get = driver.get
    original_get(vacancy_path.as_uri())
    monkeypatch.setattr(driver, "get", lambda _: original_get(form_path.as_uri()))
    return HHBrowser(driver, config, load_selectors(), Mock())


def test_select_correct_resume_fill_letter_and_confirm(driver, tmp_path, monkeypatch):
    browser = prepare_fixture(driver, tmp_path, monkeypatch)
    button = browser.prepare(VACANCY, LETTER)
    assert driver.find_element(By.ID, "letter").get_attribute("value") == LETTER
    assert not driver.find_element(By.ID, "ok").is_displayed()
    assert driver.find_element(By.CSS_SELECTOR, 'input[value="correct"]').is_selected()
    browser.submit(button)
    assert driver.find_element(By.ID, "ok").is_displayed()


@pytest.mark.parametrize("extra", [
    '<input required name="answer" placeholder="Расскажите о себе">',
    '<div data-qa="vacancy-response-popup-test">Пройдите тест</div>',
])
def test_questionnaires_never_submit(driver, tmp_path, monkeypatch, extra):
    browser = prepare_fixture(driver, tmp_path, monkeypatch, extra)
    with pytest.raises(SkipVacancy):
        browser.prepare(VACANCY, LETTER)
    assert not driver.find_element(By.ID, "ok").is_displayed()


def test_missing_resume_never_submits(driver, tmp_path, monkeypatch):
    browser = prepare_fixture(driver, tmp_path, monkeypatch, resume="absent")
    with pytest.raises(SkipVacancy):
        browser.prepare(VACANCY, LETTER)


def test_no_success_means_unknown(driver, tmp_path, monkeypatch):
    browser = prepare_fixture(driver, tmp_path, monkeypatch)
    button = browser.prepare(VACANCY, LETTER)
    driver.execute_script("document.querySelector('form').onsubmit = e => e.preventDefault();")
    with pytest.raises(StopRun):
        browser.submit(button)


def test_card_apply_button_is_not_already_applied(driver, tmp_path, monkeypatch):
    path = tmp_path / "search.html"
    path.write_text('''<html><meta charset="utf-8"><div data-qa="vacancy-serp__vacancy">
    <a data-qa="serp-item__title" href="https://hh.kz/vacancy/123">Бармен</a>
    <a data-qa="vacancy-serp__vacancy_response">Откликнуться</a></div></html>''', encoding="utf-8")
    original_get = driver.get
    monkeypatch.setattr(driver, "get", lambda _: original_get(path.as_uri()))
    browser = HHBrowser(driver, Config(timeout=1), load_selectors(), Mock())
    items = list(browser.discover())
    assert len(items) == 1 and items[0][1] is False


@pytest.mark.parametrize("qa", ["mainmenu_applicantProfileDesktopDrop", "applicantProfileMobileDrop-button"])
def test_current_hh_account_menu_is_recognized(driver, tmp_path, qa):
    path = tmp_path / "account.html"
    path.write_text(f'<html><button data-qa="{qa}">Account</button></html>', encoding="utf-8")
    driver.get(path.as_uri())
    assert visible(driver, load_selectors()["login"])


def test_guest_profile_link_is_not_login_proof(driver, tmp_path):
    path = tmp_path / "guest.html"
    path.write_text('<html><a data-qa="profileAndResumes-button" href="/account/login">Profile</a></html>', encoding="utf-8")
    driver.get(path.as_uri())
    assert not visible(driver, load_selectors()["login"])


def test_immediate_application_attaches_letter_in_verified_chat(driver, tmp_path, monkeypatch):
    chat_path = tmp_path / "chat.html"
    chat_path.write_text('''<html><meta charset="utf-8">
    <a data-qa="chatik-header-vacancy-link" href="https://hh.kz/vacancy/123">Бармен</a>
    <a id="add" data-qa="chatik-chat-message-applicant-action" onclick="document.getElementById('send').hidden=false">Добавить сопроводительное</a>
    <textarea data-qa="text-input" id="letter"></textarea>
    <button hidden id="send" data-qa="chatik-do-send-message" onclick="document.getElementById('delivered').textContent=document.getElementById('letter').value; document.getElementById('letter').value=''; document.getElementById('add').remove()">Отправить</button>
    <p id="delivered"></p></html>''', encoding="utf-8")
    applied = tmp_path / "applied.html"
    applied.write_text(f'''<html><button data-qa="vacancy-response-link-view-topic">Чат</button>
        <iframe id="chat" src="{chat_path.as_uri()}"></iframe></html>''', encoding="utf-8")
    start = tmp_path / "vacancy.html"
    start.write_text('<a data-qa="vacancy-response-link-top" href="https://hh.kz/applicant/vacancy_response?vacancyId=123">Apply</a>', encoding="utf-8")
    original_get = driver.get
    original_get(start.as_uri())
    monkeypatch.setattr(driver, "get", lambda _: original_get(applied.as_uri()))
    selectors = load_selectors()
    selectors["chat_frame"] = "iframe#chat"
    config = Config(timeout=2, pause_min=0, pause_max=0, typing_delay=0)
    browser = HHBrowser(driver, config, selectors, Mock())
    button = browser.prepare(VACANCY, LETTER)
    assert driver.find_element(By.ID, "delivered").text == ""
    browser.submit(button)
    driver.switch_to.frame(driver.find_element(By.ID, "chat"))
    assert driver.find_element(By.ID, "delivered").text == LETTER
    driver.switch_to.default_content()


def test_local_captcha_after_submit_waits_without_second_click(driver, tmp_path):
    path = tmp_path / "captcha_fixture.html"
    path.write_text('''<html><meta charset="utf-8"><script>window.clicks=0;</script>
    <button id="send" onclick="window.clicks++; document.getElementById('challenge').hidden=false;
      setTimeout(() => {document.getElementById('challenge').hidden=true;
      document.getElementById('ok').hidden=false;}, 1500)">Send</button>
    <div id="challenge" data-qa="captcha" hidden>Локальная имитация ожидания ручного ввода</div>
    <div id="ok" data-qa="vacancy-response-success" hidden>Sent</div></html>''', encoding="utf-8")
    driver.get(path.as_uri())
    events = Mock()
    browser = HHBrowser(driver, Config(timeout=0.5, pause_min=0, pause_max=0), load_selectors(), events)
    browser.submit(driver.find_element(By.ID, "send"))
    assert driver.execute_script("return window.clicks") == 1
    assert [call.args[0] for call in events.emit.call_args_list] == ["CAPTCHA", "CAPTCHA_SOLVED"]


def test_real_browser_continues_second_search_with_duplicate_page(driver, tmp_path, monkeypatch):
    pages = {}
    for query, page, vid, has_next in [("бармен", 0, "1", False), ("бариста", 0, "1", True), ("бариста", 1, "2", False)]:
        path = tmp_path / f"{query}-{page}.html"
        html = f'<html><meta charset="utf-8"><div data-qa="vacancy-serp__vacancy"><a data-qa="serp-item__title" href="https://hh.kz/vacancy/{vid}">Бариста</a></div>'
        if has_next:
            html += '<a data-qa="pager-page" href="https://hh.kz/search/vacancy?page=1">2</a>'
        path.write_text(html + '</html>', encoding="utf-8")
        pages[(query, page)] = path.as_uri()
    visited = []
    original_get = driver.get
    def navigate(url):
        qs = parse_qs(urlparse(url).query)
        key = (qs['text'][0], int(qs['page'][0]))
        visited.append(key)
        original_get(pages[key])
    monkeypatch.setattr(driver, "get", navigate)
    browser = HHBrowser(driver, Config(queries=("бармен", "бариста"), pause_min=0, pause_max=0, timeout=2), load_selectors(), Mock())
    assert [v.id for v, _ in browser.discover()] == ["1", "2"]
    assert visited == [("бармен", 0), ("бариста", 0), ("бариста", 1)]


def test_search_waits_for_later_rendered_cards(driver, tmp_path, monkeypatch):
    path = tmp_path / "delayed_cards.html"
    path.write_text('''<html><div data-qa="vacancy-serp__vacancy"><a data-qa="serp-item__title" href="https://hh.kz/vacancy/1">Bar</a></div>
    <script>setTimeout(() => { const card=document.querySelector('div').cloneNode(true);
    card.querySelector('a').href='https://hh.kz/vacancy/2'; document.body.appendChild(card); }, 400);</script></html>''', encoding="utf-8")
    original_get = driver.get
    monkeypatch.setattr(driver, "get", lambda _: original_get(path.as_uri()))
    browser = HHBrowser(driver, Config(timeout=3, pause_min=0, pause_max=0), load_selectors(), Mock())
    assert [v.id for v, _ in browser.discover()] == ["1", "2"]
