"""Determinism checks for the Windows packaged integration fixture."""

# pyright: reportPrivateUsage=false

from scripts.packaged_fixture_server import FIXTURE_PUBLISHED_AT, _feed


def test_packaged_rss_fixture_is_stable_across_repeated_rounds() -> None:
    first = _feed(42123, include_beta=True)
    second = _feed(42123, include_beta=True)

    assert first == second
    assert first.count(FIXTURE_PUBLISHED_AT.encode("utf-8")) == 2
    assert b"PACKAGED_CRAWLER_ALPHA" in first
    assert b"PACKAGED_CRAWLER_BETA" in first
