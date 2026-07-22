"""
Intent Router
"""
import logging
import re
from typing import Dict, Any, Optional, List
from enum import Enum

from ..models.client import get_model_client
from ..config import settings

logger = logging.getLogger(__name__)


class IntentType(Enum):
    EXACT_LOOKUP = "exact_lookup"
    FEATURE_SEARCH = "feature_search"
    RELATION_QUERY = "relation_query"
    MACRO_QA = "macro_qa"
    VERSION_COMPARE = "version_compare"
    CROSS_REFERENCE = "cross_reference"
    TIME_SERIES_COMPARE = "time_series_compare"


class QueryPlan:
    def __init__(self, intent: IntentType, raw_query: str,
                 target_entity_type: str = None, entities: List[str] = None,
                 conditions: Dict[str, Any] = None, requires_comparison: bool = False,
                 deep_explore: bool = False, explore_reason: str = "",
                 compare_targets: List[str] = None, reference_marks: List[str] = None,
                 source_doc_hint: str = None,
                 time_periods: List[str] = None, subject: str = None,
                 time_range: Dict[str, Any] = None,
                 industry_hint: str = None):
        self.intent = intent
        self.raw_query = raw_query
        self.target_entity_type = target_entity_type
        self.entities = entities or []
        self.conditions = conditions or {}
        self.requires_comparison = requires_comparison
        self.deep_explore = deep_explore
        self.explore_reason = explore_reason
        self.compare_targets = compare_targets or []
        self.reference_marks = reference_marks or []
        self.source_doc_hint = source_doc_hint
        self.time_periods = time_periods or []
        self.subject = subject
        self.time_range = time_range or {}
        self.industry_hint = industry_hint

    def to_dict(self) -> Dict:
        return {
            "intent": self.intent.value, "raw_query": self.raw_query,
            "target_entity_type": self.target_entity_type, "entities": self.entities,
            "conditions": self.conditions, "requires_comparison": self.requires_comparison,
            "deep_explore": self.deep_explore, "explore_reason": self.explore_reason,
            "compare_targets": self.compare_targets, "reference_marks": self.reference_marks,
            "source_doc_hint": self.source_doc_hint,
            "time_periods": self.time_periods, "subject": self.subject,
            "time_range": self.time_range,
        }

    def get_max_results(self, base_max: int = 20) -> int:
        if self.deep_explore:
            return min(max(base_max, max(len(self.entities), 1) * IntentRouter.DEEP_EXPLORE_MULTIPLIER), IntentRouter.DEEP_EXPLORE_MAX_RESULTS)
        return base_max


class IntentRouter:
    DEEP_EXPLORE_MULTIPLIER = settings.ROUTER_CONFIG.get("deep_explore_multiplier", 15)
    DEEP_EXPLORE_MAX_RESULTS = settings.ROUTER_CONFIG.get("deep_explore_max_results", 500)

    def __init__(self):
        self.model_client = get_model_client()

    def route(self, query: str) -> QueryPlan:
        """Directly call LLM for intent recognition; no content-specific hardcoded rules in code."""
        return self._llm_based_route(query)

    def _llm_based_route(self, query: str) -> QueryPlan:
        system_prompt = """You are a query intent analysis expert. Analyze the user query and output JSON.

Intent types: exact_lookup (look up specific model parameters) / feature_search (filter by criteria) / relation_query (cross-entity horizontal comparison) / macro_qa (summarize or overview data query) / version_compare (version/contract comparison) / cross_reference (cross-document reference)

Important: Time-series data comparisons such as "compared to last year" or "YoY growth" belong to macro_qa. Only use relation_query for horizontal comparisons involving two or more distinct entities.

JSON format: {"intent":"","target_entity_type":"","entities":[],"conditions":{},"requires_comparison":false,"compare_targets":[],"reference_marks":[]}"""
        try:
            result = self.model_client.generate_json(f"User query: {query}\n\nPlease output JSON.", system_prompt=system_prompt, max_tokens=1024, temperature=0.2)
            intent_str = result.get("intent", "macro_qa")
            try:
                intent = IntentType(intent_str)
            except ValueError:
                intent = IntentType.MACRO_QA
            return QueryPlan(intent=intent, raw_query=query,
                             target_entity_type=result.get("target_entity_type"),
                             entities=result.get("entities", []),
                             conditions=result.get("conditions", {}),
                             requires_comparison=result.get("requires_comparison", False),
                             compare_targets=result.get("compare_targets", []),
                             reference_marks=result.get("reference_marks", []))
        except Exception as e:
            logger.error(f"LLM routing failed: {e}")
            return QueryPlan(intent=IntentType.MACRO_QA, raw_query=query)

    def extract_search_keywords(self, query: str) -> List[str]:
        """Directly return entity words from the query; no hardcoded filtering."""
        import re
        # Only extract words that look like model/product names (letter+digit combinations)
        entities = []
        for pattern in [r'[A-Z]{2,}\d+[A-Z]*\d*', r'[A-Z]+\d+[A-Z\d\-]*', r'(?<![A-Za-z0-9])([A-Z]{1,}\d+[A-Z0-9\-]*)(?![A-Za-z0-9])']:
            entities.extend(re.findall(pattern, query))
        return list(set(entities))
