"""Classification orchestration and durable reclassification entry points."""

from collections.abc import Callable
from dataclasses import dataclass

from app.domain.classification import ClassificationResult, Classifier
from app.domain.collection import CollectedItem
from app.domain.enums import Category
from app.domain.models import IntelligenceItem
from app.domain.taxonomy import TaxonomyResult
from app.services.taxonomy_classification import TaxonomyClassificationService
from app.storage.repositories import RepositoryUnitOfWork

UnitOfWorkFactory = Callable[[], RepositoryUnitOfWork]


class ClassificationService:
    def __init__(self, classifier: Classifier, uow_factory: UnitOfWorkFactory) -> None:
        self._classifier = classifier
        self._uow_factory = uow_factory
        self._taxonomy = TaxonomyClassificationService()

    async def classify(
        self,
        item: CollectedItem,
        *,
        source_default: Category | str | None,
    ) -> ClassificationResult:
        return await self._classifier.classify(item, source_default=source_default)

    def classify_v2(self, item: CollectedItem, *, source_role: object) -> TaxonomyResult:
        from app.domain.enums import SourceRole

        return self._taxonomy.classify(item, SourceRole(source_role))

    async def reclassify_item(self, item_id: int) -> ClassificationResult:
        with self._uow_factory() as uow:
            item = uow.items.get(item_id)
            if item is None:
                raise LookupError(f"intelligence item {item_id} does not exist")
            source = uow.sources.get(item.source_id)
            if source is None:
                raise LookupError(f"source {item.source_id} does not exist")
            collected = CollectedItem(
                title=item.title,
                original_url=item.original_url,
                canonical_url=item.canonical_url,
                published_at=item.published_at,
                summary=item.summary,
                extra=item.extra,
            )
            source_default = source.default_category

        result = await self.classify(collected, source_default=source_default)
        with self._uow_factory() as uow:
            current = uow.items.get(item_id)
            if current is None:
                raise LookupError(f"intelligence item {item_id} no longer exists")
            _persist_result(current, result)
        return result

    async def reclassify_all(self, *, source_id: int | None = None) -> int:
        with self._uow_factory() as uow:
            item_ids = (
                [item.id for item in uow.items.list_by_source(source_id)]
                if source_id is not None
                else [item.id for item in uow.items.list()]
            )
        for item_id in item_ids:
            await self.reclassify_item(item_id)
        return len(item_ids)

    def preview_v2_reclassification(
        self, *, source_id: int | None = None
    ) -> "ReclassificationSummary":
        return self._reclassify_v2(source_id=source_id, apply=False)

    def apply_v2_reclassification(
        self, *, source_id: int | None = None
    ) -> "ReclassificationSummary":
        return self._reclassify_v2(source_id=source_id, apply=True)

    def _reclassify_v2(self, *, source_id: int | None, apply: bool) -> "ReclassificationSummary":
        changed = 0
        unclassified = 0
        preserved_manual = 0
        with self._uow_factory() as uow:
            items = (
                uow.items.list_by_source(source_id) if source_id is not None else uow.items.list()
            )
            for item in items:
                source = uow.sources.get(item.source_id)
                if source is None:
                    continue
                collected = CollectedItem(
                    title=item.title,
                    original_url=item.original_url,
                    canonical_url=item.canonical_url,
                    published_at=item.published_at,
                    summary=item.summary,
                    extra=item.extra,
                )
                result = self._taxonomy.classify(collected, source.source_role)
                effective = item.manual_primary_type or result.primary_type
                unclassified += effective.value == "unclassified"
                if item.manual_primary_type is not None:
                    preserved_manual += 1
                differs = any(
                    (
                        item.primary_type != result.primary_type,
                        item.topic_tags != [tag.value for tag in result.topic_tags],
                        item.industry_tags != [tag.value for tag in result.industry_tags],
                        item.case_completeness != result.case_completeness,
                        item.taxonomy_version != result.taxonomy_version,
                    )
                )
                changed += differs
                if apply and differs:
                    _persist_v2_result(item, result)
            if not apply:
                uow.rollback()
        return ReclassificationSummary(len(items), changed, unclassified, preserved_manual, apply)


def _persist_result(item: IntelligenceItem, result: ClassificationResult) -> None:
    item.category = result.category
    item.classification_score = result.score
    item.classification_reason = result.reason
    item.automatic_category_provider = result.provider


def _persist_v2_result(item: IntelligenceItem, result: TaxonomyResult) -> None:
    item.primary_type = result.primary_type
    item.topic_tags = [tag.value for tag in result.topic_tags]
    item.industry_tags = [tag.value for tag in result.industry_tags]
    item.case_completeness = result.case_completeness
    item.taxonomy_version = result.taxonomy_version
    item.taxonomy_matched_rules = list(result.matched_rules)
    item.organizer = result.opportunity.organizer
    item.application_name = result.opportunity.application_name
    item.application_target = result.opportunity.application_target
    item.deadline_at = result.opportunity.deadline_at
    item.application_method = result.opportunity.application_method
    item.application_url = result.opportunity.application_url


@dataclass(frozen=True, slots=True)
class ReclassificationSummary:
    total: int
    changed: int
    unclassified: int
    preserved_manual: int
    applied: bool
