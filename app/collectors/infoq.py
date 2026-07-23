"""Collect InfoQ AI&LLM topic articles via public POST API with source-locked parameters."""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import cast

from app.domain.collection import CollectContext, CollectedItem, Fetcher, FetchResult
from app.utils.url import canonicalize_url

_INFOQ_TOPIC_URL = "https://www.infoq.cn/public/v1/topic/getInfo"
_INFOQ_ARTICLE_URL = "https://www.infoq.cn/public/v1/article/getList"
_INFOQ_BASE = "https://www.infoq.cn"
_INFOQ_REFERER = "https://www.infoq.cn/"

_DEFAULT_MAX_PAGES = 3
_HARD_MAX_PAGES = 5
_DEFAULT_MAX_ITEMS = 30
_HARD_MAX_ITEMS = 100
_DEFAULT_RESPONSE_LIMIT_BYTES = 2 * 1_048_576
_HARD_RESPONSE_LIMIT_BYTES = 10 * 1_048_576

_ARTICLE_SUB_TYPES = {0}
_EXCLUDED_TOPIC_NAMES = {"课程", "活动", "专题", "直播", "大会", "培训", "招聘"}


class InfoQAICollector:
    """Collect InfoQ topic articles using the locked public POST API.

    The POST endpoint, request body structure, and pagination are source-specific
    and not exposed as a generic POST mechanism.
    """

    name = "infoq_ai"

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher

    async def collect(self, context: CollectContext) -> list[CollectedItem]:
        topic_alias = _text(context.config.get("topic_alias")) or "AI&LLM"
        max_pages = max(
            1,
            min(_integer(context.config.get("max_pages"), _DEFAULT_MAX_PAGES), _HARD_MAX_PAGES),
        )
        max_items = max(
            1,
            min(_integer(context.config.get("max_items"), _DEFAULT_MAX_ITEMS), _HARD_MAX_ITEMS),
        )
        response_limit = _clamp_response_limit(context.config)

        topic_id = await _resolve_topic_id(self._fetcher, topic_alias)
        if topic_id is None:
            raise ValueError(f"Could not resolve topic id for alias {topic_alias!r}")

        collected: list[CollectedItem] = []
        seen: set[str] = set()
        per_page = min(max_items, 20)

        for page in range(1, max_pages + 1):
            body = json.dumps({"id": topic_id, "page": page, "size": per_page})
            response = await _fetcher_post(
                self._fetcher,
                _INFOQ_ARTICLE_URL,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Referer": _INFOQ_REFERER,
                },
            )
            raw = await _read_bounded(response, response_limit)
            data = cast(object, json.loads(raw))
            if not isinstance(data, Mapping):
                break
            wrapper = cast(Mapping[str, object], data)
            if _integer(wrapper.get("code"), -1) != 0:
                break
            articles = wrapper.get("data")
            if not isinstance(articles, Sequence) or isinstance(articles, (str, bytes)):
                break
            article_list = cast(Sequence[object], articles)
            if len(article_list) == 0:
                break

            for value in article_list:
                if len(collected) >= max_items:
                    break
                if not isinstance(value, Mapping):
                    continue
                entry = cast(Mapping[str, object], value)
                if not _is_article(entry):
                    continue
                title = _text(entry.get("article_title"))
                if title is None:
                    continue
                aid = _text(entry.get("aid"))
                if aid is None:
                    continue
                if aid in seen:
                    continue
                seen.add(aid)
                detail_url = canonicalize_url(f"{_INFOQ_BASE}/article/{aid}")
                if detail_url is None:
                    continue
                published_at = _parse_publish_time(entry.get("publish_time"))
                summary = _text(entry.get("article_summary"))
                collected.append(
                    CollectedItem(
                        title=title,
                        original_url=detail_url,
                        canonical_url=detail_url,
                        published_at=published_at,
                        summary=summary,
                        extra={"collector": "infoq_ai", "aid": aid},
                    )
                )
            if len(collected) >= max_items or len(article_list) < per_page:
                break
        return collected


async def _fetcher_post(
    fetcher: Fetcher,
    url: str,
    *,
    body: str,
    headers: Mapping[str, str] | None,
) -> FetchResult:
    post_fn = getattr(fetcher, "post", None)
    if post_fn is None:
        raise NotImplementedError(
            "InfoQ collector requires a fetcher with a post method"
        )
    return await post_fn(url, body=body, headers=headers)


async def _resolve_topic_id(fetcher: Fetcher, topic_alias: str) -> int | None:
    body = json.dumps({"alias": topic_alias})
    response = await _fetcher_post(
        fetcher,
        _INFOQ_TOPIC_URL,
        body=body,
        headers={
            "Content-Type": "application/json",
            "Referer": _INFOQ_REFERER,
        },
    )
    raw = response.text
    if len(raw.encode("utf-8")) > 1_048_576:
        return None
    data = cast(object, json.loads(raw))
    if not isinstance(data, Mapping):
        return None
    wrapper = cast(Mapping[str, object], data)
    if _integer(wrapper.get("code"), -1) != 0:
        return None
    inner = wrapper.get("data")
    if not isinstance(inner, Mapping):
        return None
    topic_data = cast(Mapping[str, object], inner)
    topic_id = topic_data.get("id")
    if isinstance(topic_id, int) and not isinstance(topic_id, bool) and topic_id > 0:
        return topic_id
    if isinstance(topic_id, str) and topic_id.strip().isdecimal():
        return int(topic_id.strip())
    return None


def _is_article(entry: Mapping[str, object]) -> bool:
    sub_type = entry.get("sub_type", 0)
    if isinstance(sub_type, int) and sub_type not in _ARTICLE_SUB_TYPES:
        return False
    if _boolean(entry.get("is_promotion"), False):
        return False
    topics = entry.get("topic")
    if isinstance(topics, Sequence) and not isinstance(topics, (str, bytes)):
        for t_obj in cast(Sequence[object], topics):
            if isinstance(t_obj, Mapping):
                name = _text(cast(Mapping[str, object], t_obj).get("name"))
                if name and name in _EXCLUDED_TOPIC_NAMES:
                    return False
    return True


def _parse_publish_time(value: object) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    if isinstance(value, str) and value.strip().isdecimal():
        ts = int(value.strip())
        if ts > 0:
            return datetime.fromtimestamp(ts / 1000, tz=UTC)
    return None


def _clamp_response_limit(config: Mapping[str, object]) -> int:
    value = _integer(config.get("response_limit_bytes"), _DEFAULT_RESPONSE_LIMIT_BYTES)
    return max(1024, min(value, _HARD_RESPONSE_LIMIT_BYTES))


async def _read_bounded(response: FetchResult, limit_bytes: int) -> str:
    raw = response.text
    byte_size = len(raw.encode("utf-8"))
    if byte_size > limit_bytes:
        raise ValueError(
            f"JSON response body is {byte_size} bytes which exceeds the "
            f"limit of {limit_bytes} bytes"
        )
    return raw


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _integer(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _boolean(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default
