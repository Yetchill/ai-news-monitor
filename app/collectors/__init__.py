"""Built-in source collectors and registration support."""

from app.collectors.cls_topic import CLSTopicCollector
from app.collectors.github_release import GitHubReleaseCollector
from app.collectors.html_list import HTMLListCollector
from app.collectors.hubs import CaseHubCollector, DocumentHubCollector, HubCollector
from app.collectors.infoq import InfoQAICollector
from app.collectors.public_json import PublicJsonCollector
from app.collectors.registry import CollectorRegistry, default_collector_registry
from app.collectors.rss import RSSCollector
from app.collectors.single_page_changelog import SinglePageChangelogCollector

__all__ = [
    "CLSTopicCollector",
    "CaseHubCollector",
    "CollectorRegistry",
    "DocumentHubCollector",
    "GitHubReleaseCollector",
    "HTMLListCollector",
    "HubCollector",
    "InfoQAICollector",
    "PublicJsonCollector",
    "RSSCollector",
    "SinglePageChangelogCollector",
    "default_collector_registry",
]
