#!/usr/bin/env python3
"""Deterministic loopback RSS and zero-token OpenAI fixture for packaged tests."""

from __future__ import annotations

import argparse
import json
import os
import threading
from datetime import UTC, datetime
from email.utils import format_datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

FIXTURE_PUBLISHED_AT = format_datetime(
    datetime(2026, 8, 27, 0, 0, tzinfo=UTC),
    usegmt=True,
)


class FixtureState:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self.port = 0
        self.feed_requests = 0
        self.ai_requests = 0

    def mutate(self, field: str) -> int:
        with self._lock:
            value = int(getattr(self, field)) + 1
            setattr(self, field, value)
            self._write_locked()
            return value

    def publish_ready(self, port: int) -> None:
        with self._lock:
            self.port = port
            self._write_locked()

    def payload(self) -> dict[str, object]:
        with self._lock:
            return self._payload_locked()

    def _payload_locked(self) -> dict[str, object]:
        return {
            "ready": self.port > 0,
            "port": self.port,
            "feed_requests": self.feed_requests,
            "ai_requests": self.ai_requests,
            "fake_total_tokens": 0,
            "external_service_requests": 0,
        }

    def _write_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f"{self._path.suffix}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(self._payload_locked(), ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(self._path)


def handler_factory(state: FixtureState) -> type[BaseHTTPRequestHandler]:
    class FixtureHandler(BaseHTTPRequestHandler):
        server_version = "AIMPackagedFixture/1"

        def do_GET(self) -> None:
            if self.path == "/state":
                self._json(HTTPStatus.OK, state.payload())
                return
            if self.path != "/feed.xml":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            request_number = state.mutate("feed_requests")
            if request_number <= 3:
                self._bytes(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    b"deterministic initial outage",
                    "text/plain; charset=utf-8",
                )
                return
            self._bytes(
                HTTPStatus.OK,
                _feed(
                    cast(ThreadingHTTPServer, self.server).server_port,
                    include_beta=request_number >= 5,
                ),
                "application/rss+xml; charset=utf-8",
            )

        def do_POST(self) -> None:
            if self.path == "/shutdown":
                self._json(HTTPStatus.OK, {"stopping": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path != "/v1/chat/completions":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw_payload: object = json.loads(self.rfile.read(length))
                payload = (
                    cast(dict[str, object], raw_payload) if isinstance(raw_payload, dict) else {}
                )
                if payload.get("model") != "packaged-zero-token-fixture":
                    raise ValueError("unexpected fake AI request")
            except (ValueError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid fixture request"})
                return
            state.mutate("ai_requests")
            self._json(
                HTTPStatus.OK,
                {
                    "id": "packaged-fixture-completion",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "打包测试 AI 摘要 (本地假服务, 零外部 Token)",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                },
            )

        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def _json(self, status: HTTPStatus, payload: object) -> None:
            self._bytes(
                status,
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _bytes(self, status: HTTPStatus, content: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

    return FixtureHandler


def _feed(port: int, *, include_beta: bool) -> bytes:
    items = [
        _rss_item(
            "PACKAGED_CRAWLER_ALPHA 人工智能产品正式发布",
            f"http://127.0.0.1:{port}/items/alpha",
            "本地 fixture 的第一条确定性资讯, 用于验证首次新增与后续去重。",
            FIXTURE_PUBLISHED_AT,
        )
    ]
    if include_beta:
        items.append(
            _rss_item(
                "PACKAGED_CRAWLER_BETA 智能体平台正式上线",
                f"http://127.0.0.1:{port}/items/beta",
                "恢复后新增的第二条资讯, 用于验证跨轮次增量采集。",
                FIXTURE_PUBLISHED_AT,
            )
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>Windows packaged fixture</title>'
        f"<link>http://127.0.0.1:{port}/feed.xml</link>"
        "<description>deterministic packaged integration feed</description>"
        f"{''.join(items)}</channel></rss>"
    )
    return xml.encode("utf-8")


def _rss_item(title: str, link: str, description: str, published: str) -> str:
    return (
        "<item>"
        f"<title>{title}</title><link>{link}</link><guid>{link}</guid>"
        f"<description>{description}</description><pubDate>{published}</pubDate>"
        "</item>"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", required=True, type=Path)
    args = parser.parse_args()
    state = FixtureState(args.state_file.resolve())
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(state))
    state.publish_ready(server.server_port)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
