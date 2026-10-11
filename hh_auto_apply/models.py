from dataclasses import dataclass


@dataclass(frozen=True)
class Vacancy:
    id: str
    url: str
    title: str = ""
    company: str = ""
    description: str = ""
    contact: str = ""
    address: str = ""
    salary: str = ""
    schedule: str = ""
    notes: tuple[str, ...] = ()


class SkipVacancy(Exception):
    """Известная причина пропустить вакансию."""


class StopRun(Exception):
    """Нужна ручная проверка перед продолжением."""


class RunBlocked(StopRun):
    """Общая проблема запуска: ограничение HH или недоступная обязательная интеграция."""
