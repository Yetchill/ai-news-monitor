"""Application dependencies and process-local update exclusion."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from app.classifiers.rule_based import RuleBasedClassifier
from app.collectors.registry import default_collector_registry
from app.domain.collection import Fetcher
from app.domain.enums import RunTrigger
from app.domain.update import SourcePreviewResult, UpdateResult
from app.services.ai_operation_service import AIOperationService
from app.services.ai_settings_service import AISettingsService
from app.services.application_factory import build_export_service, update_pipeline_context
from app.services.export_service import ExportService
from app.services.schedule_settings_service import ScheduleSettingsService
from app.services.scheduler_service import SchedulerClock, SchedulerService
from app.services.source_discovery import (
    DiscoveryTokenStore,
    SourceDiscoveryService,
    SourcePreviewService,
)
from app.services.source_lifecycle_service import SourceLifecycleService
from app.services.source_management import SourceManagementService, SourceOnboardingService
from app.services.source_seed_service import SourceSeedService
from app.services.source_url_security import SafeHttpFetcher, SourceUrlGuard
from app.services.update_execution_service import (
    PipelineContextFactory,
    UpdateExecutionService,
    UpdateInProgressError,
    UpdateLock,
)
from app.services.web_data_service import WebDataService
from app.storage.database import Database
from app.storage.repositories import RepositoryUnitOfWork


class WebUpdateService:
    def __init__(
        self,
        database: Database,
        pipeline_context_factory: PipelineContextFactory = update_pipeline_context,
        update_lock: UpdateLock | None = None,
    ) -> None:
        self._execution = UpdateExecutionService(
            database, pipeline_context_factory, update_lock or UpdateLock()
        )
        self._database = database

    async def update(self, *, source_id: int | None = None) -> UpdateResult:
        result = await self._execution.update(
            trigger=RunTrigger.MANUAL_WEB,
            source_id=source_id,
            formal_only=False,
        )
        asyncio.create_task(self._run_auto_ai_if_enabled())  # noqa: RUF006
        return result

    async def _run_auto_ai_if_enabled(self) -> None:
        try:
            from app.services.ai_operation_service import AIOperationService
            from app.services.ai_settings_service import AISettingsService

            settings = AISettingsService(self._database)
            config = settings.get_config()
            ops = AIOperationService(self._database)

            if config.classifier_mode == "auto":
                await ops.classify_all_unclassified(trigger="auto")

            if config.summarizer_mode == "auto":
                await ops.summarize_all_unsummarized(trigger="auto")
        except Exception:
            logging.getLogger(__name__).exception("Auto AI failed during update")

    async def preview(self, source_id: int) -> SourcePreviewResult:
        return await self._execution.preview(source_id)

    async def try_scheduled_update(
        self, *, before_update: Callable[[], None] | None = None
    ) -> UpdateResult | None:
        return await self._execution.try_scheduled_update(before_update=before_update)


@dataclass(slots=True)
class WebServices:
    database: Database
    data: WebDataService
    exports: ExportService
    updates: WebUpdateService
    schedule_settings: ScheduleSettingsService
    scheduler: SchedulerService
    onboarding: SourceOnboardingService
    sources: SourceManagementService
    source_seed: SourceSeedService
    source_lifecycle: SourceLifecycleService
    token_store: DiscoveryTokenStore
    ai_settings: AISettingsService
    ai_ops: AIOperationService
    _owned_source_fetcher: SafeHttpFetcher | None = None

    @classmethod
    def build(
        cls,
        database: Database,
        *,
        pipeline_context_factory: PipelineContextFactory = update_pipeline_context,
        source_fetcher: Fetcher | None = None,
        source_url_guard: SourceUrlGuard | None = None,
        token_store: DiscoveryTokenStore | None = None,
        scheduler_clock: SchedulerClock | None = None,
    ) -> "WebServices":
        def uow_factory() -> RepositoryUnitOfWork:
            return RepositoryUnitOfWork(database)

        guard = source_url_guard or SourceUrlGuard()
        owned_fetcher = SafeHttpFetcher(guard) if source_fetcher is None else None
        fetcher: Fetcher = source_fetcher or owned_fetcher  # type: ignore[assignment]
        store = token_store or DiscoveryTokenStore()
        discovery = SourceDiscoveryService(fetcher, guard)
        preview = SourcePreviewService(
            default_collector_registry(), fetcher, RuleBasedClassifier.from_yaml()
        )
        update_lock = UpdateLock()
        updates = WebUpdateService(database, pipeline_context_factory, update_lock)
        schedule_settings = ScheduleSettingsService(uow_factory)
        scheduler = SchedulerService(
            schedule_settings,
            updates,
            clock=scheduler_clock,
        )
        return cls(
            database=database,
            data=WebDataService(uow_factory),
            exports=build_export_service(database),
            updates=updates,
            schedule_settings=schedule_settings,
            scheduler=scheduler,
            onboarding=SourceOnboardingService(discovery, preview, store),
            sources=SourceManagementService(uow_factory, store),
            source_seed=SourceSeedService(uow_factory),
            source_lifecycle=SourceLifecycleService(
                uow_factory, default_collector_registry().names()
            ),
            token_store=store,
            ai_settings=AISettingsService(database),
            ai_ops=AIOperationService(database),
            _owned_source_fetcher=owned_fetcher,
        )

    async def aclose(self) -> None:
        await self.scheduler.stop()
        if self._owned_source_fetcher is not None:
            await self._owned_source_fetcher.aclose()


__all__ = [
    "PipelineContextFactory",
    "UpdateInProgressError",
    "WebServices",
    "WebUpdateService",
]
