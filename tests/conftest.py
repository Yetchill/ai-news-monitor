"""Shared test fixtures."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.domain.enums import SourceOrigin, SourceType
from app.domain.models import Source
from app.storage.database import Database


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    database_path = tmp_path / "test.db"
    instance = Database(f"sqlite:///{database_path.as_posix()}")
    instance.create_schema()
    try:
        yield instance
    finally:
        instance.dispose()


@pytest.fixture
def source() -> Source:
    return Source(
        name="Example Feed",
        source_type=SourceType.RSS,
        start_url="https://example.com/feed.xml",
        collector_name="rss",
        collector_config={"timeout": 15},
        origin=SourceOrigin.PRESET,
    )
