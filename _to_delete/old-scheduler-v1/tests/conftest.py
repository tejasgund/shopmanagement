"""
Fixtures for the scheduler's own test suite.

Runs entirely against a throwaway SQLite database and a throwaway log
directory, with the application nowhere in sight - which is the point: if
these tests pass, the scheduler works without the app being installed.

    pytest scheduler/tests

Both throwaway locations are set through the SAME environment variables an
operator would use in production (DATABASE_URL, SCHEDULER_LOG_DIR), so the
tests exercise the real configuration path rather than a special test one.
"""

import os
import tempfile

import pytest

# Must be set BEFORE scheduler.config is imported for the first time - it reads
# its configuration once, at import, exactly as a cron process does.
# setdefault, not assignment: pytest can import this file under more than one
# module name, and a second mkdtemp would leave the tests reading one directory
# while the scheduler wrote to another.
_TMP_DB = os.path.join(tempfile.gettempdir(), "scheduler_test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP_DB}")
os.environ.setdefault("SCHEDULER_LOG_DIR", tempfile.mkdtemp(prefix="scheduler_logs_"))
os.environ.setdefault("SCHEDULER_LOG_CONSOLE", "false")
_TMP_LOGS = os.environ["SCHEDULER_LOG_DIR"]

from sqlalchemy import create_engine                       # noqa: E402
from sqlalchemy.orm import sessionmaker                    # noqa: E402

from scheduler import config                              # noqa: E402
from scheduler import settings as scheduler_settings       # noqa: E402
from scheduler.models import (                             # noqa: E402
    AppSetting, Base, Bill, Shop, User, UserShop,
)


class LogReader:
    """
    Reads only what a test itself caused to be written.

    Log files are opened once per process and appended to, so truncating them
    between tests would leave the open handles writing past a hole. Recording
    each file's length at the start of a test and reading from there gives a
    clean slate without touching the files.
    """

    def __init__(self, directory: str):
        self.directory = directory
        self._offsets = {
            name: os.path.getsize(os.path.join(directory, name))
            for name in os.listdir(directory)
            if name.endswith(".log")
        }

    def path(self, task: str) -> str:
        return os.path.join(self.directory, f"{task}.log")

    def read(self, task: str) -> str:
        path = self.path(task)
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as handle:
            handle.seek(self._offsets.get(f"{task}.log", 0))
            return handle.read()


@pytest.fixture
def logs() -> LogReader:
    """Per-task log output produced by THIS test."""
    directory = config.log_dir()
    os.makedirs(directory, exist_ok=True)
    return LogReader(directory)


@pytest.fixture
def engine():
    eng = create_engine(f"sqlite:///{_TMP_DB}", future=True)
    # The scheduler's own Base creates every table it maps. In production it
    # only ever creates scheduler_tasks (see models.ensure_schema); here it
    # stands in for the application having created the rest.
    Base.metadata.drop_all(bind=eng)
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def cfg(db):
    """Settings as the scheduler resolves them, with nothing customised."""
    return scheduler_settings.get_all(db)


def set_settings(db, values: dict) -> None:
    """Write settings the way the app's Scheduler screen would."""
    for key, value in values.items():
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        stored = "true" if value is True else "false" if value is False else str(value)
        if row:
            row.value = stored
        else:
            db.add(AppSetting(key=key, value=stored))
    db.commit()


@pytest.fixture
def tenant(db) -> User:
    user = User(name="Tenant One", mobile="9000000002", is_active=True,
                auto_rent_bill_enabled=True, rent_bill_date=1)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def shop(db, tenant) -> Shop:
    s = Shop(shop_number="A-101", shop_rent=10000)
    db.add(s)
    db.commit()
    db.refresh(s)
    db.add(UserShop(user_id=tenant.id, shop_id=s.id))
    db.commit()
    return s


@pytest.fixture
def overdue_bill(db, tenant, shop) -> Bill:
    """The worked example from the spec: 10,000 due 10 Aug 2026, unpaid."""
    from datetime import datetime
    bill = Bill(
        user_id=tenant.id, shop_id=shop.id, bill_type="Rent",
        amount=10000, paid_amount=0, pending_amount=10000,
        bill_date=datetime(2026, 8, 1), due_date=datetime(2026, 8, 10),
        status="pending",
    )
    db.add(bill)
    db.commit()
    db.refresh(bill)
    return bill
