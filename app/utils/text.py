"""Conservative text normalization for Chinese classification rules."""

import re
import unicodedata

_WHITESPACE = re.compile(r"\s+")
_CHINESE_PUNCTUATION = str.maketrans(
    {
        "\uff0c": ",",
        "。": ".",
        "\uff01": "!",
        "\uff1f": "?",
        "\uff1a": ":",
        "\uff1b": ";",
        "“": '"',
        "”": '"',
        "\u2018": "'",
        "\u2019": "'",
        "\uff08": "(",
        "\uff09": ")",
        "【": "[",
        "】": "]",
        "《": " ",
        "》": " ",
        "、": " ",
        "…": " ",
        "—": "-",
        "\uff0d": "-",
    }
)


def normalize_text(value: str | None) -> str:
    """Normalize width, case, punctuation, and whitespace without damaging versions."""

    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value).translate(_CHINESE_PUNCTUATION)
    return _WHITESPACE.sub(" ", normalized).strip().casefold()
