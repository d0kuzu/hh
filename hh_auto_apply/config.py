import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name}: ожидается true или false")
    return value in {"true", "1"}


@dataclass(frozen=True)
class Config:
    api_key: str = field(default="", repr=False)
    model: str = "gpt-4.1-mini"
    base_url: str = "https://api.openai.com/v1"
    api_mode: str = "responses"
    telegram_token: str = field(default="", repr=False)
    telegram_chat_id: str = ""
    telegram_thread_id: int | None = None
    telegram_required: bool = True
    query: str = "бармен"
    queries: tuple[str, ...] = ()
    host: str = "hh.kz"
    areas: tuple[str, ...] = ("159",)
    salary: int | None = None
    currency: str = "KZT"
    only_with_salary: bool = False
    experience: str = ""
    schedule: str = ""
    extra_params: dict = field(default_factory=lambda: {"search_field": "name"})
    exclude_words: tuple[str, ...] = ()
    required_words: tuple[str, ...] = ()
    resume_id: str = ""
    daily_limit: int = 50
    run_limit: int = 50
    max_pages: int = 10
    max_scanned: int = 200
    timezone: str = "Asia/Qyzylorda"
    pause_min: float = 5.0
    pause_max: float = 12.0
    typing_delay: float = 0.02
    timeout: float = 60.0
    chrome_binary: str = ""
    chromedriver_path: str = ""
    profile_dir: Path = ROOT / "user_data"
    runtime_dir: Path = ROOT / "runtime"
    selectors_file: Path | None = None

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Config":
        load_dotenv(env_file or ROOT.parent / ".env")
        def csv(name):
            return tuple(v.strip() for v in os.getenv(name, "").split(",") if v.strip())
        def path(name, default):
            value = Path(os.getenv(name, str(default))).expanduser()
            return value.resolve() if value.is_absolute() else (ROOT.parent / value).resolve()
        return cls(
            api_key=os.getenv("OPENAI_API_KEY", ""),
            model=os.getenv("LLM_MODEL", "gpt-4.1-mini"),
            base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
            api_mode=os.getenv("LLM_API_MODE", "responses"),
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            telegram_thread_id=int(os.environ["TELEGRAM_THREAD_ID"]) if os.getenv("TELEGRAM_THREAD_ID") else None,
            telegram_required=env_bool("TELEGRAM_REQUIRED", True),
            query=os.getenv("HH_QUERY", "бармен"), host=os.getenv("HH_HOST", "hh.kz"),
            queries=csv("HH_QUERIES"),
            areas=csv("HH_AREAS") if "HH_AREAS" in os.environ else ("159",),
            salary=int(os.environ["HH_SALARY"]) if os.getenv("HH_SALARY") else None,
            currency=os.getenv("HH_CURRENCY", "KZT"),
            only_with_salary=env_bool("HH_ONLY_WITH_SALARY", False),
            experience=os.getenv("HH_EXPERIENCE", ""), schedule=os.getenv("HH_SCHEDULE", ""),
            extra_params=json.loads(os.getenv("HH_EXTRA_PARAMS", '{"search_field":"name"}')),
            exclude_words=csv("HH_EXCLUDE_WORDS"), required_words=csv("HH_REQUIRED_WORDS"),
            resume_id=os.getenv("HH_RESUME_ID", ""),
            daily_limit=int(os.getenv("DAILY_LIMIT", "50")), run_limit=int(os.getenv("RUN_LIMIT", "50")),
            max_pages=int(os.getenv("MAX_PAGES", "10")), max_scanned=int(os.getenv("MAX_SCANNED", "200")),
            timezone=os.getenv("TIMEZONE", "Asia/Qyzylorda"),
            pause_min=float(os.getenv("PAUSE_MIN", "5")), pause_max=float(os.getenv("PAUSE_MAX", "12")),
            typing_delay=float(os.getenv("TYPING_DELAY", "0.02")), timeout=float(os.getenv("WAIT_TIMEOUT", "60")),
            chrome_binary=os.getenv("CHROME_BINARY", ""),
            chromedriver_path=os.getenv("CHROMEDRIVER_PATH", ""),
            profile_dir=path("CHROME_PROFILE_DIR", ROOT / "user_data"),
            runtime_dir=path("RUNTIME_DIR", ROOT / "runtime"),
            selectors_file=path("SELECTORS_FILE", "") if os.getenv("SELECTORS_FILE") else None,
        )

    def validate(self, *, apply: bool = False, login_only: bool = False) -> None:
        ZoneInfo(self.timezone)
        if self.host not in {"hh.ru", "hh.kz"}:
            raise ValueError("HH_HOST: hh.ru или hh.kz")
        if not self.search_queries:
            raise ValueError("Укажите HH_QUERY или HH_QUERIES")
        if min(self.daily_limit, self.run_limit, self.max_pages, self.max_scanned) < 1:
            raise ValueError("Лимиты должны быть положительными")
        if not 0 <= self.pause_min <= self.pause_max or self.typing_delay < 0 or self.timeout <= 0:
            raise ValueError("Некорректные интервалы ожидания")
        if self.salary is not None and self.salary < 0:
            raise ValueError("Зарплата не может быть отрицательной")
        if not isinstance(self.extra_params, dict):
            raise ValueError("HH_EXTRA_PARAMS должен быть JSON-объектом")
        if bool(self.telegram_token) != bool(self.telegram_chat_id):
            raise ValueError("Укажите оба TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
        if not login_only and self.telegram_required and not self.telegram_token:
            raise ValueError("Заполните Telegram в .env или задайте TELEGRAM_REQUIRED=false")
        if not login_only and (not self.api_key or not self.model):
            raise ValueError("Заполните OPENAI_API_KEY и LLM_MODEL в .env")
        if self.api_mode not in {"responses", "chat"}:
            raise ValueError("LLM_API_MODE: responses или chat")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Некорректный LLM_BASE_URL")
        if apply and not self.resume_id:
            raise ValueError("Для отправки укажите HH_RESUME_ID нужного резюме")

    @property
    def search_queries(self) -> tuple[str, ...]:
        unique = {}
        for query in self.queries or (self.query,):
            if query.strip():
                unique.setdefault(query.strip().casefold(), query.strip())
        return tuple(unique.values())

    def search_url(self, page: int = 0, *, query: str | None = None) -> str:
        params = dict(self.extra_params)
        params.update(text=query if query is not None else self.search_queries[0], page=page)
        if self.areas:
            params["area"] = self.areas
        if self.salary is not None:
            params.update(salary=self.salary, currency_code=self.currency)
        if self.only_with_salary:
            params["only_with_salary"] = "true"
        if self.experience:
            params["experience"] = self.experience
        if self.schedule:
            params["schedule"] = self.schedule
        return f"https://{self.host}/search/vacancy?" + urlencode(params, doseq=True)
