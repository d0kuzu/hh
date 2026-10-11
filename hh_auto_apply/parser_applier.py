import re
import time
from urllib.parse import parse_qs, urljoin, urlparse

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By

from .browser_driver import check_blocked, navigate, pause, scroll_to, visible, wait_with_captcha
from .models import SkipVacancy, StopRun, Vacancy


def vacancy_id(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not is_hh_host(parsed.hostname):
        return None
    match = re.fullmatch(r"/vacancy/(\d+)/?", parsed.path)
    return match.group(1) if match else None


def is_hh_host(host):
    return any(host == base or (host or "").endswith("." + base) for base in ("hh.ru", "hh.kz"))


def filter_reason(vacancy: Vacancy, config) -> str | None:
    text = f"{vacancy.title}\n{vacancy.company}\n{vacancy.description}".casefold()
    excluded = next((word for word in config.exclude_words if word.casefold() in text), None)
    if excluded:
        return f"Исключающее слово: {excluded}"
    missing = [word for word in config.required_words if word.casefold() not in text]
    return "Не найдены обязательные слова: " + ", ".join(missing) if missing else None


def has_test_text(text: str) -> bool:
    return bool(re.search(
        r"ответьте на вопросы|заполните (?:анкету|опросник)|пройти тест|пройдите тест|"
        r"для отклика.{0,80}(?:тест|анкет)|отклик.{0,40}после.{0,40}тест",
        text, re.I | re.S,
    ))


class HHBrowser:
    def __init__(self, driver, config, selectors, events, vacancy_id=None):
        self.driver, self.config, self.s, self.events = driver, config, selectors, events
        self.target_vacancy_id = vacancy_id
        self.end_reason = ""

    def wait_for(self, key, root=None):
        root = root or self.driver
        return self.until(
            lambda _: visible(root, self.s[key])
        )[0]

    def until(self, predicate):
        return wait_with_captcha(self.driver, self.s, predicate, self.config.timeout, self.events)

    def has_next_page(self, page):
        if visible(self.driver, self.s["next"]):
            return True
        for link in visible(self.driver, self.s["pages"]):
            parsed = urlparse(urljoin(self.driver.current_url, link.get_attribute("href") or ""))
            if (parsed.scheme == "https" and is_hh_host(parsed.hostname)
                    and parsed.path == "/search/vacancy"
                    and parse_qs(parsed.query).get("page") == [str(page + 1)]):
                return True
        return False

    def settled_search_cards(self):
        # HH сначала может отрисовать 20 карточек, затем догрузить остальные.
        self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
        last_count = -1
        stable_since = time.monotonic()
        stable_for = min(2.0, self.config.timeout / 2)
        def settled(driver):
            nonlocal last_count, stable_since
            count = len(visible(driver, self.s["cards"]))
            if count != last_count:
                last_count, stable_since = count, time.monotonic()
                return False
            return time.monotonic() - stable_since >= stable_for
        self.until(settled)
        return visible(self.driver, self.s["cards"])

    def discover(self):
        self.end_reason = ""
        if self.target_vacancy_id:
            yield Vacancy(self.target_vacancy_id,
                          f"https://{self.config.host}/vacancy/{self.target_vacancy_id}"), False
            self.end_reason = "Обработка выбранной вакансии завершена"
            return
        seen = set()
        capped = []
        for index, query in enumerate(self.config.search_queries):
            if index:
                pause(self.config)
            if (yield from self._discover_query(query, seen)):
                capped.append(query)
        queries = ", ".join(f"«{query}»" for query in self.config.search_queries)
        if capped:
            self.end_reason = (f"Обход запросов {queries} завершён. Предел MAX_PAGES={self.config.max_pages} "
                               f"достигнут для: {', '.join(capped)}. Уникальных вакансий: {len(seen)}")
        else:
            self.end_reason = f"Выдача HH закончилась по запросам {queries}. Уникальных вакансий: {len(seen)}"

    def _discover_query(self, query, seen):
        query_seen = set()
        for page in range(self.config.max_pages):
            url = self.config.search_url(page, query=query)
            self.events.emit("SEARCH", f"Поиск «{query}», страница {page + 1}: {url}")
            navigate(self.driver, url, self.s, self.events)
            try:
                self.until(
                    lambda d: visible(d, self.s["cards"]) or visible(d, self.s["empty"])
                )
            except TimeoutException as exc:
                check_blocked(self.driver, self.s, self.events)
                if not self.driver.find_element(By.TAG_NAME, "body").text.strip():
                    raise StopRun("HH загрузил пустую страницу. Проверьте сеть и страницу в Chrome, затем повторите запуск") from exc
                raise StopRun("Не найдены карточки или маркер пустого поиска. Проверьте разметку HH") from exc
            cards = self.settled_search_cards()
            has_next = self.has_next_page(page)
            # Сохраняем значения до переходов; WebElement не переживает навигацию.
            batch = []
            parsed_ids = set()
            for card in cards:
                links = card.find_elements(By.CSS_SELECTOR, self.s["card_link"])
                if not links:
                    continue
                link = links[0]
                raw_url = urljoin(self.driver.current_url, link.get_attribute("href") or "")
                vid = vacancy_id(raw_url)
                if not vid:
                    continue
                parsed_ids.add(vid)
                if vid in seen:
                    continue
                seen.add(vid)
                applied = bool(visible(card, self.s["applied"])) or bool(
                    re.search(r"\b(?:вы откликнулись|отклик отправлен|вы уже откликнулись)\b", card.text, re.I)
                )
                batch.append((Vacancy(vid, f"https://{self.config.host}/vacancy/{vid}", link.text), applied))
            if cards and (not parsed_ids or not parsed_ids - query_seen):
                raise StopRun(f"Поиск «{query}»: страница содержит только повторы предыдущих страниц "
                              "этого запроса или нераспознанные ссылки")
            query_seen.update(parsed_ids)
            yield from batch
            if not has_next:
                return False
            if page + 1 < self.config.max_pages:
                pause(self.config)
        return True

    def read(self, item: Vacancy) -> Vacancy:
        navigate(self.driver, item.url, self.s, self.events)
        if visible(self.driver, self.s["applied"]):
            raise SkipVacancy("HH уже показывает отправленный отклик")
        try:
            title = self.wait_for("title").text.strip()
            description = self.wait_for("description").text.strip()
        except TimeoutException as exc:
            check_blocked(self.driver, self.s, self.events)
            raise SkipVacancy("Нет описания вакансии: возможно, она закрыта или разметка изменилась") from exc
        if not title or not description:
            raise SkipVacancy("Пустое название или описание вакансии")
        def optional(key):
            elements = visible(self.driver, self.s[key])
            return elements[0].text.strip() if elements else ""
        return Vacancy(item.id, item.url, title, optional("company"), description, optional("contact"),
                       optional("address"), optional("salary"), optional("schedule"))

    def precheck(self, vacancy):
        check_blocked(self.driver, self.s, self.events)
        if visible(self.driver, self.s["test"]) or has_test_text(vacancy.description):
            raise SkipVacancy("Требуется тест или опросник")

    def prepare(self, vacancy: Vacancy, letter: str):
        """Открывает известную страницу формы, заполняет, возвращает финальную кнопку.

        HH может отправить отклик даже при переходе по href. В этом случае
        прикладываем письмо через действие «Добавить сопроводительное» в чате.
        """
        self.unsubmitted_form = False
        self._chat_letter = None
        links = visible(self.driver, self.s["response_link"])
        if not links:
            raise SkipVacancy("Нет распознанной ссылки на форму отклика")
        target = urljoin(self.driver.current_url, links[0].get_attribute("href") or "")
        parsed = urlparse(target)
        valid_host = is_hh_host(parsed.hostname)
        if not (parsed.scheme == "https" and valid_host
                and parsed.path.rstrip("/") == "/applicant/vacancy_response"
                and parse_qs(parsed.query).get("vacancyId") == [vacancy.id]):
            raise SkipVacancy("Ссылка не ведёт на известную форму отклика; требуется адаптация локаторов")
        self.events.emit("FORM", "Открытие формы отклика", vacancy, letter)
        pause(self.config)
        navigate(self.driver, target, self.s, self.events)
        try:
            self.until(
                lambda d: visible(d, self.s["form"]) or visible(d, self.s["test"])
                or visible(d, self.s["applied"])
            )
        except TimeoutException as exc:
            check_blocked(self.driver, self.s, self.events)
            raise SkipVacancy("Форма отклика не распознана") from exc
        if visible(self.driver, self.s["applied"]):
            self.events.emit("FORM", "HH создал отклик; прикладываем сопроводительное через чат", vacancy, letter)
            return self.prepare_existing_letter(vacancy, letter)
        # Известная форма ещё не отправлялась; при её явном отказе можно пропустить.
        self.unsubmitted_form = bool(visible(self.driver, self.s["form"]) or visible(self.driver, self.s["test"]))
        if visible(self.driver, self.s["test"]) or has_test_text(self.driver.find_element(By.TAG_NAME, "body").text):
            raise SkipVacancy("Форма требует тест или опросник")
        form = self.wait_for("form")
        self.select_resume(form)
        self.events.emit("RESUME", "Выбрано резюме из HH_RESUME_ID", vacancy, letter)
        if not visible(form, self.s["letter"]):
            toggles = visible(form, self.s["letter_toggle"])
            if not toggles:
                raise SkipVacancy("Нет поля письма или известной кнопки добавления письма")
            pause(self.config)
            scroll_to(self.driver, toggles[0])
            toggles[0].click()
        try:
            field = self.wait_for("letter", form)
        except TimeoutException as exc:
            raise SkipVacancy("Поле сопроводительного письма не появилось") from exc
        # Любые дополнительные видимые поля, кроме выбора резюме/письма, требуют разбора.
        for element in form.find_elements(By.CSS_SELECTOR, "input, textarea, select"):
            kind = (element.get_attribute("type") or "").lower()
            if element == field or kind in {"hidden", "submit", "button"} or not element.is_displayed():
                continue
            if kind == "radio" and element.get_attribute("name") in {"resumeId", "resume_id"}:
                continue
            raise SkipVacancy("В форме есть дополнительные вопросы или неподдерживаемые поля")
        scroll_to(self.driver, field)
        field.clear()
        if self.config.typing_delay:
            for char in letter:
                field.send_keys(char)
                time.sleep(self.config.typing_delay)
        else:
            field.send_keys(letter)
        if field.get_attribute("value").strip() != letter.strip():
            raise SkipVacancy("Текст в поле письма отличается от подготовленного")
        if not self.resume_selected(form):
            raise SkipVacancy("Не удалось подтвердить выбранное резюме")
        buttons = visible(form, self.s["submit"])
        if len(buttons) != 1 or not buttons[0].is_enabled():
            raise SkipVacancy("Финальная кнопка отправки неоднозначна или недоступна")
        if visible(self.driver, self.s["success"]) or visible(self.driver, self.s["applied"]):
            raise SkipVacancy("На странице уже присутствует статус отклика")
        self.events.emit("READY", "Письмо вставлено и проверено; форма готова к отправке", vacancy, letter)
        return buttons[0]

    def prepare_existing_letter(self, vacancy: Vacancy, letter: str):
        """Добавляет письмо к уже созданному отклику; новый отклик не создаёт."""
        closers = visible(self.driver, self.s["preferences_close"])
        if closers:
            closers[0].click()
        self.wait_for("applied").click()
        frame = self.wait_for("chat_frame")
        self.driver.switch_to.frame(frame)
        header = self.wait_for("chat_vacancy")
        if vacancy_id(header.get_attribute("href")) != vacancy.id:
            raise StopRun("Открыт чат другой вакансии; письмо не отправлено")
        self.wait_for("chat_input")
        actions = [e for e in visible(self.driver, self.s["chat_add_letter"])
                   if e.text.strip() == "Добавить сопроводительное"]
        if len(actions) != 1:
            raise StopRun("Нет однозначного действия добавления письма; проверьте чат HH")
        actions[0].click()
        field = self.wait_for("chat_input")
        existing = field.get_attribute("value") or ""
        if existing.strip() and existing.strip() != letter.strip():
            raise StopRun("В чате есть другой черновик; автоматическая замена остановлена")
        field.clear()
        field.send_keys(letter)
        if field.get_attribute("value").strip() != letter.strip():
            raise StopRun("Текст письма в чате отличается от подготовленного")
        self._chat_letter = letter
        self.events.emit("READY", "Сопроводительное вставлено в чат и проверено", vacancy, letter)
        return self.wait_for("chat_send")

    def resume_selected(self, form):
        return any(
            e.get_attribute("value") == self.config.resume_id
            and (e.get_attribute("type") == "hidden" or e.is_selected())
            for e in form.find_elements(By.CSS_SELECTOR, self.s["resume"])
        )

    def select_resume(self, form):
        inputs = form.find_elements(By.CSS_SELECTOR, self.s["resume"])
        target = next((e for e in inputs if e.get_attribute("value") == self.config.resume_id), None)
        if target is None:
            raise SkipVacancy("Указанное HH_RESUME_ID не найдено в форме")
        if target.get_attribute("type") == "hidden":
            return
        if not target.is_selected():
            labels = target.find_elements(By.XPATH, "./ancestor::label[1]")
            labels += [e for e in form.find_elements(By.TAG_NAME, "label")
                       if target.get_attribute("id") and e.get_attribute("for") == target.get_attribute("id")]
            click_target = target if target.is_displayed() else next((e for e in labels if e.is_displayed()), None)
            if click_target is None:
                raise SkipVacancy("Не найден доступный переключатель нужного резюме")
            scroll_to(self.driver, click_target)
            click_target.click()
            try:
                self.until(lambda _: self.resume_selected(form))
            except TimeoutException as exc:
                raise SkipVacancy("Не удалось выбрать нужное резюме") from exc

    def submit(self, button):
        pause(self.config)
        check_blocked(self.driver, self.s, self.events)
        scroll_to(self.driver, button)
        button.click()
        if getattr(self, "_chat_letter", None):
            letter = self._chat_letter
            self.until(
                lambda d: letter in d.find_element(By.TAG_NAME, "body").text
                and any(not e.get_attribute("value") for e in visible(d, self.s["chat_input"]))
                and not any(e.text.strip() == "Добавить сопроводительное"
                            for e in visible(d, self.s["chat_add_letter"]))
            )
            self._chat_letter = None
            self.driver.switch_to.default_content()
            return
        try:
            self.until(
                lambda d: visible(d, self.s["success"]) or visible(d, self.s["applied"])
            )
        except TimeoutException as exc:
            raise StopRun("HH не подтвердил отправку; проверьте отклики вручную, повтор заблокирован") from exc

    def recover_after_unknown(self):
        # Письмо могло отправляться внутри iframe чата. Следующая вакансия
        # должна обрабатываться в основном документе, без повторного submit.
        self.driver.switch_to.default_content()
        check_blocked(self.driver, self.s, self.events)
