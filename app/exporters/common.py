"""Shared safe text, labels, filenames, and formatting for office documents."""

import re
import unicodedata
from datetime import datetime

from app.domain.enums import Category, PrimaryType
from app.utils.url import is_http_url

CATEGORY_LABELS = {
    Category.MODEL_TECHNOLOGY: "大模型与技术",
    Category.AGENT_PRODUCT: "智能体与产品",
    Category.ENTERPRISE_CASE: "企业成果与案例",
    Category.AWARD_CASE: "奖项与优秀案例",
    Category.SOLICITATION: "征集与申报",
    Category.POLICY_INDUSTRY: "政策、标准与行业",
    Category.UNCLASSIFIED: "待分类",
}
CATEGORY_ORDER = tuple(CATEGORY_LABELS)
PRIMARY_TYPE_LABELS = {
    PrimaryType.PRODUCT_UPDATE: "产品与模型更新",
    PrimaryType.POLICY_STANDARD: "政策与标准",
    PrimaryType.APPLICATION_OPPORTUNITY: "申报与征集机会",
    PrimaryType.AWARD_RESULT: "奖项与入选结果",
    PrimaryType.REPORT_RELEASE: "报告发布",
    PrimaryType.CASE_ANALYSIS: "案例分析",
    PrimaryType.INDUSTRY_SIGNAL: "行业线索",
    PrimaryType.UNCLASSIFIED: "待确认",
}
PRIMARY_TYPE_ORDER = tuple(PRIMARY_TYPE_LABELS)

_ILLEGAL_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_FORMULA_PREFIXES = ("=", "+", "-", "@")
_PLAIN_NEGATIVE_NUMBER = re.compile(r"-(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?%?")
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_INVALID_SHEET = re.compile(r"[\\/*?:\[\]]")


def clean_text(value: object, *, limit: int = 32_767) -> str:
    """Return XML-safe bounded plain text without interpreting HTML."""

    if limit <= 0:
        return ""
    text = _ILLEGAL_XML.sub("", str(value))
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def excel_safe_text(value: object, *, limit: int = 32_767) -> str:
    """Keep untrusted text from being interpreted as an Excel formula."""

    text = clean_text(value, limit=limit)
    if _has_excel_formula_prefix(text):
        return "'" + clean_text(value, limit=limit - 1)
    return text


def _has_excel_formula_prefix(value: str) -> bool:
    index = 0
    while index < len(value):
        character = value[index]
        if not (character.isspace() or unicodedata.category(character) in {"Cc", "Cf"}):
            break
        index += 1
    candidate = value[index:]
    if not candidate.startswith(_FORMULA_PREFIXES):
        return False
    return _PLAIN_NEGATIVE_NUMBER.fullmatch(candidate.strip()) is None


def safe_hyperlink(value: str | None) -> str | None:
    if value is None or len(value) > 2_048 or not is_http_url(value):
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    return value.strip()


def format_time(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value is not None else ""


def safe_filename(value: str, *, extension: str) -> str:
    stem = _INVALID_FILENAME.sub("-", clean_text(value, limit=120)).strip(" .-")
    if not stem:
        stem = "export"
    normalized_extension = extension.lstrip(".").lower()
    return f"{stem}.{normalized_extension}"


def safe_sheet_name(value: str) -> str:
    name = _INVALID_SHEET.sub("-", clean_text(value, limit=31)).strip("'")
    return name or "导出"
