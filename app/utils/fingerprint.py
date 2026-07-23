"""Stable identity fingerprint generation for source-scoped item deduplication."""

from hashlib import sha256

from app.utils.text import normalize_text


def generate_item_fingerprint(title: str) -> str:
    """Hash a normalized title; database matching always scopes it to one source."""

    normalized_title = normalize_text(title)
    if not normalized_title:
        raise ValueError("cannot fingerprint an empty title")
    return sha256(f"title:v1\x00{normalized_title}".encode()).hexdigest()
