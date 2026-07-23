"""Pure classification implementations and future extension points."""

from app.classifiers.hybrid import HybridClassifier
from app.classifiers.llm import LLMClassifier, create_llm_classifier
from app.classifiers.manual import FinalCategoryResolver, ManualCategoryError, ManualClassifier
from app.classifiers.providers import (
    DeepSeekProvider,
    LLMConfigError,
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    LLMResponseError,
    LLMTimeoutError,
    OpenAICompatibleProvider,
    build_prompt,
    parse_response,
)
from app.classifiers.rule_based import DEFAULT_RULE_PATH, RuleBasedClassifier
from app.classifiers.rules import RuleConfigError, load_classification_rules

__all__ = [
    "DEFAULT_RULE_PATH",
    "DeepSeekProvider",
    "FinalCategoryResolver",
    "HybridClassifier",
    "LLMClassifier",
    "LLMConfigError",
    "LLMProvider",
    "LLMProviderError",
    "LLMResponse",
    "LLMResponseError",
    "LLMTimeoutError",
    "ManualCategoryError",
    "ManualClassifier",
    "OpenAICompatibleProvider",
    "RuleBasedClassifier",
    "RuleConfigError",
    "build_prompt",
    "create_llm_classifier",
    "load_classification_rules",
    "parse_response",
]
