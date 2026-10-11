import random
import time
from urllib.parse import urlparse

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, NoSuchWindowException, StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service

from .config import Config
from .models import RunBlocked, StopRun


def create_driver(config: Config):
    config.profile_dir.mkdir(parents=True, exist_ok=True)
    options = webdriver.ChromeOptions()
    options.add_argument(f"--user-data-dir={config.profile_dir}")
    options.add_argument("--start-maximized")
    options.add_argument("--lang=ru-RU")
    if config.chrome_binary:
        options.binary_location = config.chrome_binary
    service = Service(executable_path=config.chromedriver_path) if config.chromedriver_path else Service()
    driver = webdriver.Chrome(options=options, service=service)
    driver.set_page_load_timeout(60)
    return driver


def visible(root, selector):
    try:
        return [e for e in root.find_elements(By.CSS_SELECTOR, selector) if e.is_displayed()]
    except StaleElementReferenceException:
        return []


def captcha_visible(driver, selectors):
    return bool(visible(driver, selectors["captcha"])) or any(
        text in driver.title.casefold() for text in ("captcha", "капча", "подтвердите")
    )


def check_blocked(driver, selectors, events=None):
    """Ждём ручного решения CAPTCHA без навигации и повторов отправки."""
    waited = False
    if captcha_visible(driver, selectors):
        waited = True
        if events is not None:
            events.emit("CAPTCHA", "@dokuzu, пройдите CAPTCHA в открытом окне Chrome. "
                        "Скрипт на паузе и будет ждать без ограничения по времени.", critical=False)
        while captcha_visible(driver, selectors):
            time.sleep(2)
        if events is not None:
            events.emit("CAPTCHA_SOLVED", "CAPTCHA пройдена. Продолжаю обработку с текущего этапа.", critical=False)
    if "доступ ограничен" in driver.title.casefold():
        raise RunBlocked("HH ограничил доступ. Требуется ручная проверка")
    return waited


def wait_with_captcha(driver, selectors, predicate, timeout, events=None):
    """Время ручного решения не расходует тайм-аут ожидания страницы."""
    deadline = time.monotonic() + timeout
    while True:
        started = time.monotonic()
        if check_blocked(driver, selectors, events):
            deadline += time.monotonic() - started
        try:
            value = predicate(driver)
            if value:
                return value
        except (NoSuchElementException, StaleElementReferenceException):
            pass
        if time.monotonic() >= deadline:
            raise TimeoutException("Не дождались ожидаемого состояния страницы")
        time.sleep(0.5)


def navigate(driver, url, selectors, events=None):
    try:
        driver.get(url)
    except TimeoutException:
        if not check_blocked(driver, selectors, events):
            raise
    check_blocked(driver, selectors, events)


def pause(config: Config):
    time.sleep(random.uniform(config.pause_min, config.pause_max))


def scroll_to(driver, element):
    driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", element)
    time.sleep(0.5)


def applicant_logged_in(driver, selectors, events=None):
    """Подтверждаем вход по меню аккаунта или содержимому личного профиля.

    Одна ссылка «Резюме и профиль» недостаточна: она бывает в гостевом меню.
    """
    parsed = urlparse(driver.current_url)
    if not any(parsed.hostname == host or (parsed.hostname or "").endswith("." + host)
               for host in ("hh.ru", "hh.kz")):
        return False
    check_blocked(driver, selectors, events)
    if visible(driver, selectors["login"]):
        return True
    return parsed.path.rstrip("/") in {"/applicant/profile/me", "/applicant/resumes"} and bool(
        visible(driver, selectors["applicant_profile"])
    )


def wait_for_login(driver, selectors, timeout, events=None):
    try:
        wait_with_captcha(driver, selectors, lambda d: applicant_logged_in(d, selectors, events), timeout, events)
        return True
    except TimeoutException:
        return False


def open_applicant_page(driver, config):
    # HH может открыть вход в новой вкладке и закрыть исходную.
    try:
        driver.current_window_handle
    except NoSuchWindowException:
        if not driver.window_handles:
            raise StopRun("Окно Chrome закрыто. Запустите вход снова")
        driver.switch_to.window(driver.window_handles[-1])
    previous_timeout = driver.timeouts.page_load
    driver.set_page_load_timeout(min(20, config.timeout))
    try:
        # HH сам переводит этот маршрут на /applicant/profile/me в новом интерфейсе.
        driver.get(f"https://{config.host}/applicant/resumes")
    except TimeoutException:
        # Медленный внешний ресурс не отменяет уже загруженный профиль.
        # Успех всё равно требует видимого маркера авторизации ниже.
        pass
    finally:
        driver.set_page_load_timeout(previous_timeout)


def ensure_login(driver, config, selectors, events):
    open_applicant_page(driver, config)
    if wait_for_login(driver, selectors, min(10, config.timeout), events=events):
        events.emit("AUTH", "Используется сохранённая сессия HH")
        return
    events.emit("AUTH", "Войдите в HH вручную в открытом Chrome; затем нажмите Enter в терминале")
    input("Войдите в HH как соискатель и нажмите Enter: ")
    open_applicant_page(driver, config)
    if not wait_for_login(driver, selectors, config.timeout, events=events):
        raise StopRun("Не удалось загрузить личный профиль соискателя. Проверьте вход в открытом Chrome и повторите --login-only; сохранённая сессия не удалена")
    events.emit("AUTH", "Авторизация подтверждена, сессия сохранена")
