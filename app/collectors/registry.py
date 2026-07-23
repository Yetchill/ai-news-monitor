"""Extensible collector registration and construction."""

from collections.abc import Callable

from app.collectors.cls_topic import CLSTopicCollector
from app.collectors.github_release import GitHubReleaseCollector
from app.collectors.html_list import HTMLListCollector
from app.collectors.hubs import CaseHubCollector, DocumentHubCollector
from app.collectors.huxiu import HuxiuCollector
from app.collectors.infoq import InfoQAICollector
from app.collectors.minimax_news import MiniMaxNewsCollector
from app.collectors.public_json import PublicJsonCollector
from app.collectors.rss import RSSCollector
from app.collectors.single_page_changelog import SinglePageChangelogCollector
from app.domain.collection import Collector, Fetcher
from app.domain.models import Source

CollectorFactory = Callable[[Fetcher], Collector]


class CollectorRegistry:
    """Map stable collector names to factories without branching in collection flows."""

    def __init__(self) -> None:
        self._factories: dict[str, CollectorFactory] = {}

    def register(
        self,
        name: str,
        factory: CollectorFactory,
        *,
        replace: bool = False,
    ) -> None:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("collector name cannot be empty")
        if normalized_name in self._factories and not replace:
            raise ValueError(f"collector already registered: {normalized_name}")
        self._factories[normalized_name] = factory

    def create(self, source: Source, fetcher: Fetcher) -> Collector:
        name = source.collector_name.strip() or source.source_type.value
        try:
            factory = self._factories[name]
        except KeyError as error:
            raise LookupError(f"no collector registered for {name!r}") from error
        return factory(fetcher)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))


def default_collector_registry() -> CollectorRegistry:
    """Build the registry containing the stage-two collectors."""

    registry = CollectorRegistry()
    registry.register(RSSCollector.name, RSSCollector)
    registry.register(HTMLListCollector.name, HTMLListCollector)
    registry.register(GitHubReleaseCollector.name, GitHubReleaseCollector)
    registry.register(SinglePageChangelogCollector.name, SinglePageChangelogCollector)
    registry.register(DocumentHubCollector.name, DocumentHubCollector)
    registry.register(CaseHubCollector.name, CaseHubCollector)
    registry.register(CLSTopicCollector.name, CLSTopicCollector)
    registry.register(MiniMaxNewsCollector.name, MiniMaxNewsCollector)
    registry.register(PublicJsonCollector.name, PublicJsonCollector)
    registry.register(InfoQAICollector.name, InfoQAICollector)
    registry.register(HuxiuCollector.name, HuxiuCollector)
    return registry
