"""
Query Decomposer
"""
import logging
from typing import List

from ..models.client import get_model_client

logger = logging.getLogger(__name__)


class QueryDecomposer:
    def __init__(self):
        self.model_client = get_model_client()

    def decompose(self, query: str, entities: List[str] = None,
                  time_periods: List[str] = None, subject: str = None,
                  chat_history: str = None) -> List[str]:
        if time_periods and len(time_periods) >= 2 and subject:
            return self._decompose_time_compare(query, subject, time_periods, chat_history)
        if entities and len(entities) >= 2:
            return self._decompose_entity_compare(query, entities, chat_history)
        return [query]

    def _decompose_time_compare(self, query: str, subject: str,
                                 time_periods: List[str],
                                 chat_history: str = None) -> List[str]:
        sub_queries = []
        for period in time_periods:
            # OpenLAD: Generic template, not hardcoded Chinese text
            sub_queries.append(f"{subject} data for period {period}")
        logger.info(f"[DECOMPOSER] Time series: {len(sub_queries)} periods")
        return sub_queries

    def _decompose_entity_compare(self, query: str, entities: List[str],
                                   chat_history: str = None) -> List[str]:
        """Entity comparison decomposition. Returns entity list directly; executor searches each entity with the original query."""
        sub_queries = []
        for entity in entities:
            sub_queries.append(f"{entity} {query}")
        logger.info(f"[DECOMPOSER] Entity compare: {len(sub_queries)} entities")
        return sub_queries
