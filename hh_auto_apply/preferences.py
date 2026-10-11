"""Мягкие предпочтения: пометки для отчёта, без отбрасывания вакансий."""
import re

from .models import Vacancy


def extract_schedules(vacancy: Vacancy) -> list[str]:
    """Числа в адресе (например, дом 7/5) не являются графиком."""
    source = vacancy.schedule
    if not source:
        source = "\n".join(line for line in vacancy.description.splitlines() if re.search(
            r"график|режим\s+работ|сменност|^\s*[1-7]\s*[/хx]\s*[1-7]\b", line, re.I
        ))
    return list(dict.fromkeys(f"{a}/{b}" for a, b in re.findall(
        r"\b([1-7])\s*[/хx]\s*([1-7])\b(?!\s*/\s*\d)", source
    )))


def assess(vacancy: Vacancy, profile: dict) -> tuple[str, ...]:
    prefs = profile.get("preferences", {})
    notes = []
    text = " ".join((vacancy.description, vacancy.address, vacancy.schedule)).casefold()
    schedules = extract_schedules(vacancy)
    preferred = prefs.get("preferred_schedules", [])
    if preferred:
        good = [s for s in schedules if s in preferred]
        other = [s for s in schedules if s not in preferred]
        if good:
            notes.append("✅ Предпочтительный график: " + ", ".join(good))
        if other:
            notes.append("⚠️ Менее желательный график: " + ", ".join(other))
        if not schedules:
            notes.append("🔎 Точный график не распознан — уточнить")
    if re.search(r"вахт[а-я]*|релокаци[а-я]*|(?:требуется|необходим|предстоит) переезд|работ[аы] в другом городе", text):
        notes.append("⚠️ Упоминается вахта/переезд — менее желательно, уточнить условия")
    city = prefs.get("preferred_city", "")
    if city and vacancy.address and city.casefold() not in vacancy.address.casefold():
        # Адрес без города не доказывает необходимость переезда.
        notes.append(f"🔎 По адресу не подтверждён город {city}; проверить место работы")
    if prefs.get("preferred_bank") == "левый":
        left = bool(re.search(r"лев(?:ый|ого|ом|ому)\s+берег|левобереж", text))
        right = bool(re.search(r"прав(?:ый|ого|ом|ому)\s+берег|правобереж", text))
        if left and right:
            notes.append("🔎 Указаны оба берега — уточнить адрес конкретной смены")
        elif right:
            notes.append("⚠️ Указан правый берег — менее желательный район")
        elif left:
            notes.append("✅ В тексте указан левый берег")
    notes.extend(salary_notes(vacancy.salary, schedules, prefs))
    return tuple(notes)


def salary_notes(salary: str, schedules: list[str], prefs: dict) -> list[str]:
    target = prefs.get("salary_per_shift")
    currency = prefs.get("currency")
    if not target:
        return []
    notes = [f"Ориентир: {target:,.0f} {currency} за смену".replace(",", " ")]
    if not salary:
        return notes + ["🔎 Оплата не извлечена из шапки вакансии; проверить описание"]
    text = salary.casefold().replace("\u202f", " ").replace("\xa0", " ")
    if currency != "KZT" or not re.search(r"₸|тенге|kzt", text):
        return notes + ["🔎 Валюта не совпадает или не распознана; автоматического пересчёта нет"]
    values = [int(v.replace(" ", "")) for v in re.findall(r"\d{1,3}(?: \d{3})+|\d{4,}", text)]
    if not values:
        return notes + ["🔎 Не удалось разобрать сумму оплаты"]
    if "смен" in text:
        threshold = target
    elif "месяц" in text and len(schedules) == 1:
        work, rest = (int(v) for v in schedules[0].split("/"))
        shifts = 30 * work / (work + rest)
        threshold = target * shifts
        amount = f"{threshold:,.0f}".replace(",", " ")
        notes.append(f"При {schedules[0]} ориентир ≈ {amount} KZT/месяц "
                     f"({shifts:.1f} смены за 30 дней, одна смена в рабочий день)")
    else:
        return notes + ["🔎 Для сравнения месячного оклада нужен однозначный график и число смен"]
    lower = None if "до " in text and "от " not in text and len(values) == 1 else min(values)
    upper = None if "от " in text and "до " not in text and len(values) == 1 else max(values)
    if upper is not None and upper < threshold:
        notes.append("⚠️ Указанная оплата ниже ориентира")
    elif lower is not None and lower >= threshold:
        notes.append("✅ Указанная сумма достигает ориентира; уточнить налоги и гарантированную часть")
    return notes
