from itertools import groupby
import json

from .models import Vacancy
from .preferences import extract_schedules


def unknown_applications_report(items):
    blocks = [f"⚠️ Отклики для ручной проверки: {len(items)}",
              "Неподтверждённые случаи из всех запусков. Повторная отправка заблокирована."]
    for item in items:
        blocks.append(f"Вакансия: {item['title']}\n{item['url']}\n"
                      f"ID: {item['vacancy_id']} | Статус: {item['status']}\n"
                      f"Дата: {item['updated_at']}\n\nПисьмо:\n{item['letter']}")
    if not items:
        blocks.append("Неразобранных случаев нет.")
    return "\n\n".join(blocks)


def vacancy_links_report(vacancies, *, apply, preferred_schedules=("2/2", "5/2"), title=None):
    preferred = list(preferred_schedules)
    def rank(schedule):
        if schedule in preferred:
            return (0, preferred.index(schedule))
        return (1, tuple(int(x) for x in schedule.split("/")))
    def group(vacancy):
        schedules = sorted(extract_schedules(vacancy), key=rank)
        if not schedules:
            return (2, (), "График не указан")
        first = rank(schedules[0])
        return (*first, "График: " + " или ".join(schedules))
    unique = {v.id: v for v in vacancies}
    ordered = sorted(unique.values(), key=lambda v: (group(v), v.id))
    heading = title or ("Вакансии с отправленными откликами" if apply else "Вакансии с подготовленными тестовыми письмами")
    blocks = [heading]
    for (_, _, label), items in groupby(ordered, key=group):
        blocks.append(label + "\n" + "\n".join(v.url for v in items))
    if not ordered:
        blocks.append("В этом запуске таких вакансий нет.")
    return "\n\n".join(blocks)


def load_vacancy_details(events_path):
    details = {}
    if events_path.exists():
        with events_path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue  # Незавершённая строка после прерывания старого запуска.
                vacancy = event.get("vacancy")
                if vacancy and vacancy.get("description"):
                    details[vacancy["id"]] = Vacancy(**vacancy)
    return details


def build_full_report(summary, current_links, attempts, events_path, *, drafts=(), preferred_schedules=("2/2", "5/2")):
    """Локальная копия включает историю прошлых запусков."""
    details = load_vacancy_details(events_path)
    def vacancy_for(row):
        return details.get(row["vacancy_id"], Vacancy(row["vacancy_id"], row["url"], row["title"]))
    blocks = ["ПОЛНЫЙ ОТЧЁТ HH", summary, "ТЕКУЩИЙ ЗАПУСК\n\n" + current_links]
    for status, title in (("sent", "ВСЕ ПОДТВЕРЖДЁННЫЕ ОТКЛИКИ"), ("unknown", "НЕИЗВЕСТНЫЙ ИСХОД"),
                          ("pending", "ПРЕРВАННЫЕ ПОПЫТКИ")):
        rows = [row for row in attempts if row["status"] == status]
        if rows:
            blocks.append(vacancy_links_report([vacancy_for(row) for row in rows], apply=True,
                          preferred_schedules=preferred_schedules, title=f"{title}: {len(rows)}\nСсылки по графикам"))
    blocks.append("ПОДРОБНОСТИ ВСЕЙ СОХРАНЁННОЙ ИСТОРИИ")
    for row in attempts:
        vacancy = vacancy_for(row)
        blocks.append(f"ID: {row['vacancy_id']} | {row['status']} | {row['updated_at']}\n"
                      + _vacancy_details(vacancy, row["letter"]))
    for vacancy, letter in drafts:
        blocks.append("ТЕСТОВОЕ ПИСЬМО — НЕ ОТПРАВЛЕНО\n" + _vacancy_details(vacancy, letter))
    return "\n\n".join(blocks) + "\n"


def _vacancy_details(vacancy, letter):
    return (f"Вакансия: {vacancy.title}\nКомпания: {vacancy.company}\n{vacancy.url}\n"
            f"Адрес: {vacancy.address}\nОплата: {vacancy.salary}\nГрафик: {vacancy.schedule}\n\n"
            f"Описание:\n{vacancy.description or '(не сохранено)'}\n\nПисьмо:\n{letter}")


def build_report_messages(attempts, events_path, *, drafts=(), preferred_schedules=("2/2", "5/2")):
    details = load_vacancy_details(events_path)
    by_id = {row["vacancy_id"]: row for row in attempts}
    def vacancy_for(row):
        return details.get(row["vacancy_id"], Vacancy(row["vacancy_id"], row["url"], row["title"]))
    def ordered(rows):
        # Используем тот же порядок графиков, что и в обычном списке ссылок.
        links = vacancy_links_report([vacancy_for(row) for row in rows], apply=True,
                                     preferred_schedules=preferred_schedules)
        by_url = {row["url"]: row for row in rows}
        return [by_url[line] for line in links.splitlines() if line in by_url]
    messages = []
    sent = ordered([row for row in by_id.values() if row["status"] == "sent"])
    for offset in range(0, len(sent), 8):
        rows = sent[offset:offset + 8]
        messages.append(vacancy_links_report([vacancy_for(row) for row in rows], apply=True,
                        preferred_schedules=preferred_schedules,
                        title=f"✅ Подтверждённые отклики за все запуски — {offset + 1}–{offset + len(rows)} из {len(sent)}"))
    unresolved = ordered([row for row in by_id.values() if row["status"] in {"unknown", "pending"}])
    for index, row in enumerate(unresolved, 1):
        vacancy = vacancy_for(row)
        messages.append(f"⚠️ Для ручной проверки — {index}/{len(unresolved)}\n"
                        f"Вакансия: {vacancy.title}\nКомпания: {vacancy.company}\n{vacancy.url}\n"
                        f"График: {vacancy.schedule or 'не указан'}\nСтатус: {row['status']}\n\n"
                        "Сначала проверьте чат HH: отклик мог отправиться без письма или без подтверждения.\n\n"
                        f"Письмо:\n{row['letter']}")
    if drafts:
        for offset in range(0, len(drafts), 8):
            messages.append(vacancy_links_report([v for v, _ in drafts[offset:offset + 8]], apply=False,
                                                preferred_schedules=preferred_schedules))
    if not messages:
        messages.append("Сохранённых откликов и тестовых писем пока нет.")
    return [f"Отчёт — часть {index}/{len(messages)}\n\n{message}"
            for index, message in enumerate(messages, 1)]
