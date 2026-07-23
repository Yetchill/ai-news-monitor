"""Repository CRUD and relationship tests."""

from app.domain.enums import Category, CrawlStatus
from app.domain.models import CrawlRun, IntelligenceItem, ItemRevision, Source
from app.storage.database import Database
from app.storage.repositories import RepositoryUnitOfWork


def test_source_create_and_query(database: Database, source: Source) -> None:
    with RepositoryUnitOfWork(database) as repositories:
        created = repositories.sources.add(source)
        assert created.id > 0

    with RepositoryUnitOfWork(database) as repositories:
        loaded = repositories.sources.get_by_start_url("https://example.com/feed.xml")
        assert loaded is not None
        assert loaded.name == "Example Feed"
        assert loaded.collector_config == {"timeout": 15}


def test_intelligence_item_create_and_query(database: Database, source: Source) -> None:
    with RepositoryUnitOfWork(database) as repositories:
        repositories.sources.add(source)
        created = repositories.items.add(
            IntelligenceItem(
                source_id=source.id,
                title="A new model release",
                original_url="https://example.com/releases/model-v1",
                canonical_url="https://example.com/releases/model-v1",
                summary="Release notes",
                category=Category.MODEL_TECHNOLOGY,
                fingerprint="1" * 64,
                extra={"language": "en"},
            )
        )
        item_id = created.id

    with RepositoryUnitOfWork(database) as repositories:
        loaded = repositories.items.get(item_id)
        by_url = repositories.items.get_by_canonical_url("https://example.com/releases/model-v1")
        assert loaded is not None
        assert by_url is not None
        assert loaded.title == "A new model release"
        assert loaded.source_id == source.id
        assert by_url.extra == {"language": "en"}


def test_crawl_run_and_revision_relationship(database: Database, source: Source) -> None:
    with RepositoryUnitOfWork(database) as repositories:
        repositories.sources.add(source)
        item = repositories.items.add(
            IntelligenceItem(
                source_id=source.id,
                title="Original title",
                original_url="https://example.com/item",
                canonical_url="https://example.com/item",
                fingerprint="2" * 64,
            )
        )
        crawl_run = repositories.crawl_runs.add(
            CrawlRun(status=CrawlStatus.SUCCEEDED, source_total=1, source_success=1)
        )
        revision = repositories.revisions.add(
            ItemRevision(
                item_id=item.id,
                crawl_run_id=crawl_run.id,
                old_data={"title": "Original title"},
                new_data={"title": "Updated title"},
            )
        )
        revision_id = revision.id
        run_id = crawl_run.id

    with RepositoryUnitOfWork(database) as repositories:
        loaded_run = repositories.crawl_runs.get(run_id)
        loaded_revision = repositories.revisions.get(revision_id)
        assert loaded_run is not None
        assert loaded_revision is not None
        assert [entry.id for entry in loaded_run.revisions] == [revision_id]
        assert loaded_revision.crawl_run is not None
        assert loaded_revision.crawl_run.id == run_id
        assert loaded_revision.item.title == "Original title"

    with RepositoryUnitOfWork(database) as repositories:
        assert repositories.crawl_runs.delete(run_id) is True

    with RepositoryUnitOfWork(database) as repositories:
        retained_revision = repositories.revisions.get(revision_id)
        assert retained_revision is not None
        assert retained_revision.crawl_run_id is None


def test_repository_crud(database: Database, source: Source) -> None:
    with RepositoryUnitOfWork(database) as repositories:
        created = repositories.sources.add(source)
        source_id = created.id

    with RepositoryUnitOfWork(database) as repositories:
        assert [entry.id for entry in repositories.sources.list()] == [source_id]
        updated = repositories.sources.update(
            source_id,
            {"name": "Updated Feed", "enabled": False},
        )
        assert updated is not None
        assert updated.name == "Updated Feed"
        assert updated.enabled is False

    with RepositoryUnitOfWork(database) as repositories:
        assert repositories.sources.delete(source_id) is True
        assert repositories.sources.delete(999_999) is False

    with RepositoryUnitOfWork(database) as repositories:
        assert repositories.sources.get(source_id) is None
        assert repositories.sources.list() == []
