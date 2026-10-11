import argparse
import sys
from contextlib import ExitStack
from pathlib import Path

from filelock import FileLock, Timeout

from .browser_driver import create_driver, ensure_login
from .config import Config
from .llm_service import LLMService
from .models import StopRun
from .parser_applier import HHBrowser
from .runner import run
from .selectors import load_selectors
from .state import StateStore
from .telegram_logger import EventLogger
from .user_profile import PROFILE, validate_profile


def arguments(argv=None):
    parser = argparse.ArgumentParser(description="HH: персональные письма и отклики. По умолчанию — 5 писем без отправки.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Тест без открытия формы отклика (по умолчанию)")
    mode.add_argument("--apply", action="store_true", help="Отправлять отклики с письмами")
    mode.add_argument("--login-only", action="store_true", help="Только ручной вход с сохранением сессии Chrome")
    parser.add_argument("--limit", type=int, help="Число писем/откликов: по умолчанию 5 в тесте, RUN_LIMIT при отправке")
    parser.add_argument("--env-file", type=Path, help="Путь к файлу настроек .env")
    parser.add_argument("--vacancy-id", help="Обработать только указанную вакансию, удобно для проверки одной отправки")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit должен быть больше нуля")
    if args.vacancy_id and (not args.vacancy_id.isascii() or not args.vacancy_id.isdigit()):
        parser.error("--vacancy-id должен содержать только цифры")
    return args


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = arguments(argv)
    events = None
    try:
        config = Config.from_env(args.env_file)
        config.validate(apply=args.apply, login_only=args.login_only)
        if not args.login_only:
            validate_profile(PROFILE)
        selectors = load_selectors(config.selectors_file)
        config.runtime_dir.mkdir(parents=True, exist_ok=True)
        config.profile_dir.mkdir(parents=True, exist_ok=True)
        with ExitStack() as stack:
            # Блокируем и профиль Chrome, и БД; повторный процесс завершится сразу.
            stack.enter_context(FileLock(str(config.runtime_dir / "run.lock"), timeout=0))
            stack.enter_context(FileLock(str(config.profile_dir / "automation.lock"), timeout=0))
            events = EventLogger(config)
            stack.callback(events.close)
            try:
                events.emit("START", "Режим: " + ("авторизация" if args.login_only else "ОТПРАВКА" if args.apply else "ТЕСТ БЕЗ ОТПРАВКИ"))
                driver = create_driver(config)
                stack.callback(driver.quit)
                ensure_login(driver, config, selectors, events)
                if args.login_only:
                    return 0
                state = StateStore(config.runtime_dir / "state.sqlite3", config.timezone)
                stack.callback(state.close)
                llm = LLMService(config)
                stack.callback(llm.close)
                run(config, PROFILE, HHBrowser(driver, config, selectors, events, args.vacancy_id), llm, state, events,
                    apply=args.apply, limit=args.limit or (config.run_limit if args.apply else 5))
                return 0
            except (KeyboardInterrupt, EOFError):
                events.emit("STOP", "Запуск прерван пользователем", critical=False)
                return 130
            except Exception as exc:
                detail = str(exc) if isinstance(exc, (StopRun, ValueError)) else type(exc).__name__
                events.emit("STOP", detail, critical=False)
                return 1
    except Timeout:
        print("Уже работает другой процесс с этой историей или профилем Chrome", file=sys.stderr)
        return 1
    except Exception as exc:
        # Не печатаем исключения сетевых SDK с URL/ключами.
        print(str(exc) if isinstance(exc, ValueError) else f"Ошибка запуска: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
