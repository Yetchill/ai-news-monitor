"""Compatibility facade for the stage-eight-B source catalog sync."""

from pathlib import Path

from app.services.source_catalog_service import (
    CATALOG_PATH,
    SourceCatalogService,
    SourceCatalogSyncResult,
)
from app.storage.repositories import RepositoryUnitOfWork

DEFAULT_PRESET_PATH = CATALOG_PATH
SourceSeedResult = SourceCatalogSyncResult


class SourceSeedService(SourceCatalogService):
    """Deprecated name retained for existing Web composition and integrations."""

    def seed(
        self,
        path: Path = DEFAULT_PRESET_PATH,
        *,
        reconcile: bool = False,
    ) -> SourceSeedResult:
        return self.sync(path, reconcile=reconcile)


__all__ = ["DEFAULT_PRESET_PATH", "RepositoryUnitOfWork", "SourceSeedResult", "SourceSeedService"]
