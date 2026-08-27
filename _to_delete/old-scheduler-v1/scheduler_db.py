"""
scheduler/db.py - database access for the scheduler process.

Replaces the old db_config.py, and no longer borrows anything from the
application: the connection details come from scheduler/config.py and the ORM
mapping from scheduler/models.py.

This process is not the API. It is short-lived, single-threaded, runs a
handful of statements and exits, so it wants its own small engine rather than
a long-lived connection pool. Keeping the two apart also means the scheduler
can be pointed at a replica, a different credential or a different host
without touching the app.
"""

from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from scheduler import config, models
from scheduler.errors import DatabaseUnavailable


def make_engine():
    """
    A fresh engine for this run.

    pool_pre_ping matters more here than in the API: a job that fires once a
    day will routinely find the server has closed an idle connection, and
    without it the first statement of the night fails.
    """
    return create_engine(config.database_url(), pool_pre_ping=True, future=True)


def make_session_factory():
    """An engine plus its session factory, for a caller that manages both."""
    engine = make_engine()
    return engine, sessionmaker(bind=engine, autocommit=False, autoflush=False)


@contextmanager
def session_scope(logger=None):
    """
    A session for one run, with the engine disposed on the way out.

    Also the one place startup checks live, so every entry point gets them
    without having to remember:

      * the connection actually works (a clear error beats a stack trace from
        deep inside the first query)
      * the scheduler's own table exists, created here if not - a fresh
        install needs nothing run by hand
      * the application's tables still look the way this process expects,
        reported as warnings rather than raised (see models.verify_schema)
    """
    engine, Session = make_session_factory()
    try:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:
            raise DatabaseUnavailable(
                f"cannot reach {config.safe_database_url()}: {exc}"
            ) from exc

        created = models.ensure_schema(engine)
        if created and logger:
            logger.info("Created scheduler table(s): %s", ", ".join(created))

        if logger:
            for problem in models.verify_schema(engine):
                logger.warning("Schema check: %s", problem)

        db = Session()
        try:
            yield db
        finally:
            db.close()
    finally:
        engine.dispose()
