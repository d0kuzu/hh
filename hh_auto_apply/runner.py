from dataclasses import dataclass, replace

from .browser_driver import pause
from .models import RunBlocked, SkipVacancy, StopRun
from .parser_applier import filter_reason
from .preferences import assess
from .reports import build_full_report, build_report_messages, unknown_applications_report, vacancy_links_report


@dataclass
class RunResult:
    scanned: int = 0
    prepared: int = 0
    sent: int = 0
    skipped: int = 0
    errors: int = 0
    unknown: int = 0
    end_reason: str = ""


def run(config, profile, browser, llm, state, events, *, apply=False, limit=5):
    result = RunResult()
    completed_vacancies = []
    current_drafts = []
    fast_skipped = 0
    target = min(limit, config.run_limit) if apply else limit
    def processed():
        return result.sent + result.unknown if apply else result.prepared
    def report_progress():
        events.set_progress(scanned=result.scanned, prepared=result.prepared, sent=result.sent,
                            skipped=result.skipped, target=target, apply=apply, unknown=result.unknown)
    report_progress()
    items = iter(browser.discover())
    while result.scanned < config.max_scanned and processed() < target:
        if apply and state.count_today() >= config.daily_limit:
            result.end_reason = f"Достигнут суточный лимит попыток: {config.daily_limit}"
            events.emit("LIMIT", "Достигнут суточный лимит откликов")
            break
        try:
            vacancy, already_applied = next(items)
        except StopIteration:
            reason = getattr(browser, "end_reason", "")
            result.end_reason = reason if isinstance(reason, str) and reason else "Выдача поиска закончилась"
            break
        result.scanned += 1
        report_progress()
        letter = ""
        try:
            status = state.status(vacancy.id) if not already_applied else None
            if already_applied or status:
                reason = ("На карточке HH указан отправленный отклик" if already_applied
                          else f"В истории уже есть попытка: {status}")
                result.skipped += 1
                fast_skipped += 1
                report_progress()
                events.emit("SKIP", reason, vacancy, notify=False)
                # Карточки уже собраны со страницы: здесь нет запросов к HH,
                # поэтому пауза между действиями и правки Telegram не нужны.
                continue
            events.emit("FOUND", "Найдена вакансия", vacancy)
            vacancy = browser.read(vacancy)
            vacancy = replace(vacancy, notes=assess(vacancy, profile))
            events.emit("PARSED", "Загружено описание вакансии", vacancy)
            reason = filter_reason(vacancy, config)
            if reason:
                raise SkipVacancy(reason)
            browser.precheck(vacancy)
            events.emit("GENERATE", "Генерация сопроводительного письма", vacancy)
            letter = llm.generate(profile, vacancy)
            state.draft(vacancy, letter)
            result.prepared += 1
            report_progress()
            events.emit("LETTER", "Сопроводительное письмо подготовлено", vacancy, letter)
            if not apply:
                events.emit("DRY_RUN", "Тестовый режим: форма отклика не открывается, отправки нет", vacancy, letter)
                completed_vacancies.append(vacancy)
                current_drafts.append((vacancy, letter))
            else:
                events.emit("SUBMIT", "Начинается отправка отклика", vacancy, letter)
                # Переход на vacancy_response тоже может отправить отклик.
                state.reserve(vacancy, letter, config.daily_limit)
                try:
                    button = browser.prepare(vacancy, letter)
                    browser.submit(button)
                except BaseException as exc:
                    if isinstance(exc, SkipVacancy) and getattr(browser, "unsubmitted_form", False) is True:
                        state.discard_unsubmitted(vacancy.id)
                        raise
                    state.finish(vacancy.id, "unknown")
                    result.unknown += 1
                    report_progress()
                    events.emit("UNKNOWN", "Исход отправки неизвестен. Сохранено для итоговой проверки; повтор заблокирован. "
                                f"Причина: {type(exc).__name__}",
                                vacancy, letter, critical=False)
                    if not isinstance(exc, Exception) or isinstance(exc, RunBlocked):
                        raise
                    browser.recover_after_unknown()
                else:
                    state.finish(vacancy.id, "sent")
                    result.sent += 1
                    completed_vacancies.append(vacancy)
                    report_progress()
                    events.emit("SUCCESS", "Отклик подтверждён HH", vacancy, letter)
        except SkipVacancy as exc:
            result.skipped += 1
            report_progress()
            events.emit("SKIP", str(exc), vacancy, letter)
        except StopRun:
            raise
        except Exception as exc:
            # Не продолжаем сотни запросов при недействительном API-ключе/поломке браузера.
            result.errors += 1
            events.emit("ERROR", f"Ошибка {type(exc).__name__}; запуск остановлен", vacancy, letter, critical=False)
            raise StopRun(f"Ошибка {type(exc).__name__}. Проверьте настройки API/браузера и локальные логи") from exc
        if processed() < target:
            pause(config)
    count = processed()
    if count >= target:
        result.end_reason = f"Достигнут лимит запуска: {target} {'попыток отклика' if apply else 'тестовых писем'}"
        events.emit("LIMIT", f"Достигнут лимит запуска: {target} {'попыток отклика' if apply else 'тестовых писем'}")
    elif not result.end_reason and result.scanned >= config.max_scanned:
        result.end_reason = f"Достигнут предел просмотренных вакансий MAX_SCANNED={config.max_scanned}"
    if fast_skipped:
        events.emit("SKIP_SUMMARY", f"Быстро пропущено уже обработанных вакансий: {fast_skipped}. "
                    "Подробности каждой записаны в skipped.log; страницы вакансий не открывались.")
    events.emit("DONE", f"Просмотрено: {result.scanned}; писем: {result.prepared}; отправлено: {result.sent}; "
                f"неизвестный исход: {result.unknown}; пропущено: {result.skipped}. Цель запуска: {target}\n"
                f"Причина завершения: {result.end_reason}")
    if not apply and result.prepared < target:
        events.emit("SHORTFALL", "Подходящих вакансий оказалось меньше тестового лимита; проверьте фильтры и MAX_PAGES/MAX_SCANNED")
    preferred = profile.get("preferences", {}).get("preferred_schedules", ["2/2", "5/2"])
    links = vacancy_links_report(completed_vacancies, apply=apply, preferred_schedules=preferred)
    unresolved = state.unresolved()
    attempts = state.all_attempts()
    events.emit("REPORT", links, notify=False)
    events.emit("UNKNOWN_REPORT", unknown_applications_report(unresolved), notify=False)
    summary = (f"Итог прохода: просмотрено {result.scanned}, пропущено {result.skipped}, "
               f"подтверждено {result.sent}, неизвестный исход {result.unknown}.\n"
               f"{result.end_reason}\n"
               f"За все запуски: подтверждено {sum(r['status'] == 'sent' for r in attempts)}, "
               f"для проверки {len(unresolved)}.")
    full_report = build_full_report(summary, links, attempts, config.runtime_dir / "events.jsonl",
                                   drafts=current_drafts, preferred_schedules=preferred)
    messages = build_report_messages(attempts, config.runtime_dir / "events.jsonl",
                                     drafts=current_drafts, preferred_schedules=preferred)
    events.final_report(summary, full_report, unresolved_count=len(unresolved), messages=messages)
    return result
