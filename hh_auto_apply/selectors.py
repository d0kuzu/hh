"""CSS локаторы HH. Можно переопределять через SELECTORS_FILE (JSON).

Разметка HH меняется: неизвестную форму пропускаем, универсальный submit не ищем.
"""
import json

SELECTORS = {
    "cards": '[data-qa="vacancy-serp__vacancy"], [data-qa="vacancy-serp__vacancy-item"], .vacancy-serp-item',
    "card_link": 'a[data-qa="serp-item__title"], a[data-qa="vacancy-serp__vacancy-title"]',
    "applied": '[data-qa="vacancy-response-link-view-topic"], [data-qa="vacancy-response-link-already-applied"]',
    "empty": '[data-qa="vacancy-serp__vacancy-not-found"], [data-qa="vacancy-serp__no-results"]',
    "next": 'a[data-qa="pager-next"]',
    "pages": 'a[data-qa="pager-page"]',
    "title": '[data-qa="vacancy-title"]',
    "company": '[data-qa="vacancy-company-name"]',
    "description": '[data-qa="vacancy-description"], .vacancy-description',
    "contact": '[data-qa="vacancy-contacts__fio"]',
    "address": '[data-qa="vacancy-view-raw-address"], [data-qa="vacancy-view-location"]',
    "salary": '[data-qa="vacancy-salary"], [data-qa="vacancy-salary-compensation-type-net"], [data-qa="vacancy-salary-compensation-type-gross"]',
    "schedule": '[data-qa="work-schedule-by-days-text"], [data-qa="work-schedule-by-days"], [data-qa="vacancy-view-work-schedule"]',
    "login": '[data-qa="mainmenu_applicantProfile"], [data-qa="mainmenu_myResumes"], [data-qa="mainmenu_applicantProfileDesktopDrop"], [data-qa="mainmenu_applicantProfileMobileDrop"], [data-qa="applicantProfileDesktopDrop-button"], [data-qa="applicantProfileMobileDrop-button"]',
    "applicant_profile": '[data-qa="applicant-profile-common-name"], [data-qa="compact-resume-title"]',
    "captcha": 'iframe[src*="captcha"], [data-qa="captcha"], input[name="captcha"], #captcha',
    "response_link": 'a[data-qa="vacancy-response-link-top"], a[data-qa="vacancy-response-link-bottom"]',
    "form": '[data-qa="vacancy-response-popup-form"], form[action*="vacancy_response"]',
    "letter": 'textarea[data-qa="vacancy-response-popup-form-letter-input"], textarea[name="letter"]',
    "letter_toggle": '[data-qa="vacancy-response-letter-toggle"], [data-qa="vacancy-response-popup-form-letter-toggle"]',
    "resume": 'input[type="radio"][name="resumeId"], input[type="radio"][name="resume_id"], input[type="hidden"][name="resumeId"]',
    "submit": '[data-qa="vacancy-response-submit-popup"], [data-qa="vacancy-response-submit"]',
    "test": '[data-qa="vacancy-response-popup-test"], [data-qa="vacancy-response-test"], [data-qa="task-body"], [data-qa="vacancy-response-questionnaire"]',
    "success": '[data-qa="vacancy-response-success"], [data-qa="vacancy-response-popup-success"]',
    "chat_frame": 'iframe[src*="chatik.hh.kz/chat/"], iframe[src*="chatik.hh.ru/chat/"]',
    "chat_vacancy": 'a[data-qa="chatik-header-vacancy-link"]',
    "chat_add_letter": '[data-qa="chatik-chat-message-applicant-action"]',
    "chat_input": 'textarea[data-qa="text-input"]',
    "chat_send": 'button[data-qa="chatik-do-send-message"]',
    "preferences_close": '[data-qa="additional-data-collector__popup-close"]',
}


def load_selectors(path=None):
    selectors = dict(SELECTORS)
    if path:
        overrides = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(overrides, dict) or any(
            k not in selectors or not isinstance(v, str) or not v.strip() for k, v in overrides.items()
        ):
            raise ValueError("SELECTORS_FILE должен содержать известные имена и непустые CSS-локаторы")
        selectors.update(overrides)
    return selectors
