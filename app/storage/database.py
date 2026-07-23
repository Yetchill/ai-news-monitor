"""SQLAlchemy engine and transaction infrastructure."""

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from sqlite3 import Connection as SQLiteConnection
from typing import cast

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.domain.models import Base


class Database:
    """Own the SQLAlchemy engine and create short-lived sessions."""

    def __init__(self, database_url: str | None = None, *, echo: bool = False) -> None:
        self.database_url = database_url or get_settings().database_url
        self._ensure_sqlite_parent(self.database_url)
        connect_args = (
            # Python 3.12's modern SQLite transaction mode makes SELECT and
            # SAVEPOINT participate in the outer source transaction.
            {"check_same_thread": False, "autocommit": False}
            if self.database_url.startswith("sqlite")
            else {}
        )
        self.engine: Engine = create_engine(
            self.database_url,
            echo=echo,
            future=True,
            connect_args=connect_args,
        )
        if self.database_url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._enable_sqlite_foreign_keys)
        self.session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            autoflush=False,
            expire_on_commit=False,
        )

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "Database":
        """Construct a database from application settings."""

        resolved = settings or get_settings()
        return cls(resolved.database_url)

    @contextmanager
    def session(self) -> Generator[Session]:
        """Yield a session and atomically commit or roll it back."""

        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def create_schema(self) -> None:
        """Create mapped tables, primarily for isolated tests.

        Normal application initialization uses ``alembic upgrade head`` so the
        migration history remains authoritative.
        """

        Base.metadata.create_all(self.engine)

    def dispose(self) -> None:
        """Release pooled database connections."""

        self.engine.dispose()

    @staticmethod
    def _ensure_sqlite_parent(database_url: str) -> None:
        url = make_url(database_url)
        if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
            return
        Path(url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _enable_sqlite_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
        connection = cast(SQLiteConnection, dbapi_connection)
        previous_autocommit = connection.autocommit
        connection.autocommit = True
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()
            connection.autocommit = previous_autocommit
