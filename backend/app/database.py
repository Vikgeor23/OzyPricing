"""Database engine and session factory (SQLAlchemy 2.x sync)."""

import logging
from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()


class Base(DeclarativeBase):
    """Declarative base for ORM models."""


engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    pool_timeout=settings.database_pool_timeout_sec,
)


# Apply per-connection guardrails at the raw-connection level so they survive
# across pooled reuse (a transaction-scoped SET would be rolled back on error).
# Enabled only where Compose sets the env — the web/backend process — never the
# celery worker, whose batch queries legitimately run for minutes.
_stmt_timeout_ms = max(0, int(settings.db_statement_timeout_ms))
_work_mem = (settings.db_work_mem or "").strip()
if _stmt_timeout_ms > 0 or _work_mem:
    @event.listens_for(engine, "connect")
    def _set_connection_guardrails(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        try:
            if _stmt_timeout_ms > 0:
                cur.execute(f"SET statement_timeout = {_stmt_timeout_ms}")
            if _work_mem:
                # Quote to reject anything that isn't a plain size literal.
                cur.execute("SET work_mem = %s", (_work_mem,))
        finally:
            cur.close()
        # psycopg2 runs the SETs inside an implicit transaction; without this
        # commit the pool's reset-on-return ROLLBACK reverts them, silently
        # stripping the guardrails from every reused connection.
        dbapi_conn.commit()
    logger.info(
        "DB connection guardrails active: statement_timeout=%sms work_mem=%s",
        _stmt_timeout_ms or "off",
        _work_mem or "default",
    )

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    class_=Session,
)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
