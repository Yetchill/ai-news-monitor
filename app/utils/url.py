"""Safe URL resolution and canonicalization helpers."""

from collections.abc import Collection
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

TRACKING_QUERY_KEYS = {
    "dclid",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "msclkid",
}


def is_http_url(url: str) -> bool:
    """Return whether a URL is an absolute HTTP(S) URL."""

    try:
        parts = urlsplit(url.strip())
        _ = parts.port
        return parts.scheme.lower() in {"http", "https"} and bool(parts.hostname)
    except ValueError:
        return False


def canonicalize_url(
    url: str,
    *,
    base_url: str | None = None,
    keep_query_params: Collection[str] | None = None,
) -> str | None:
    """Resolve and normalize a web URL, returning ``None`` for unsafe schemes.

    When ``keep_query_params`` is omitted, non-tracking parameters are retained. When it is
    supplied, only the named parameters are retained, including explicitly named tracking-like
    parameters that are required by a source.
    """

    try:
        candidate = urljoin(base_url, url) if base_url else url
        candidate = candidate.strip()
        parts = urlsplit(candidate)
        hostname_value = parts.hostname
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not hostname_value:
        return None

    hostname = hostname_value.lower().rstrip(".")
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return None

    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    display_hostname = f"[{hostname}]" if ":" in hostname else hostname
    netloc = display_hostname if port is None or default_port else f"{display_hostname}:{port}"

    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"

    allowed = set(keep_query_params) if keep_query_params is not None else None
    query_items: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        normalized_key = key.lower()
        if allowed is None:
            if normalized_key.startswith("utm_") or normalized_key in TRACKING_QUERY_KEYS:
                continue
        elif key not in allowed:
            continue
        query_items.append((key, value))
    query_items.sort()

    return urlunsplit((scheme, netloc, path, urlencode(query_items, doseq=True), ""))


def resolve_url(
    base_url: str,
    link: str,
    *,
    keep_query_params: Collection[str] | None = None,
) -> str | None:
    """Resolve a possibly-relative link and apply canonicalization."""

    return canonicalize_url(link, base_url=base_url, keep_query_params=keep_query_params)
