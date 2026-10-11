"""Просмотр истории и ручное разрешение неопределённых отправок после проверки HH."""
import argparse

from filelock import FileLock

from .config import Config
from .state import StateStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolve", metavar="VACANCY_ID")
    parser.add_argument("--outcome", choices=("sent", "not-sent"))
    args = parser.parse_args()
    if bool(args.resolve) != bool(args.outcome):
        parser.error("Укажите --resolve и --outcome вместе")
    config = Config.from_env()
    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(config.runtime_dir / "run.lock"), timeout=0)
    lock.acquire()
    state = None
    try:
        state = StateStore(config.runtime_dir / "state.sqlite3", config.timezone)
        if args.resolve:
            state.db.execute("BEGIN IMMEDIATE")
            try:
                if state.status(args.resolve) not in {"pending", "unknown"}:
                    raise ValueError("Разрешать можно только pending/unknown")
                if args.outcome == "sent":
                    state.db.execute("UPDATE attempts SET status='sent', updated_at=? WHERE vacancy_id=?",
                                     (state.now().isoformat(), args.resolve))
                else:
                    state.db.execute("DELETE FROM attempts WHERE vacancy_id=?", (args.resolve,))
                state.db.commit()
                state.sync_applied_ids()
                state.sync_unresolved()
            except BaseException:
                state.db.rollback()
                raise
            print("История обновлена:", args.resolve, args.outcome)
        for row in state.db.execute("SELECT vacancy_id, day, status, title FROM attempts ORDER BY updated_at DESC"):
            print(" | ".join(row))
    finally:
        if state:
            state.close()
        lock.release()


if __name__ == "__main__":
    main()
